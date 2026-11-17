# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Evaluation-only adapter between OCU and dmxapi. Not part of any release.

``POST /v1/audio/speech`` takes OCU's OpenAI-style speech request (the one
``computer-use-server/speech.py`` sends) and calls dmxapi ``chat/completions``
with ``mimo-v2.5-tts``, which returns the audio base64-encoded in the requested
format. The adapter holds the provider key; callers present ``ADAPTER_TOKEN``.

Every failure upstream is a 502 whose JSON body fits inside speech.py's
200-character error excerpt. Every response body and log line passes the
redactor, and the upstream call never follows a redirect, so the provider key
leaves only toward ``UPSTREAM_BASE_URL`` in the Authorization header.

Environment: ``PROVIDER_KEY``, ``ADAPTER_TOKEN`` and ``UPSTREAM_BASE_URL``,
read and checked at startup. The module does nothing at import beyond
building the app, so tests can load it and point it at a fake upstream.
"""

import asyncio
import base64
import binascii
import contextlib
import hmac
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import aiohttp
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

# Equal to speech.MAX_AUDIO_BYTES; the tests pin the two together.
MAX_AUDIO_BYTES = 50 * 1024 * 1024
# Below OCU's 120-second default, so OCU receives the adapter's 502 instead of
# timing out itself.
UPSTREAM_TIMEOUT_SECONDS = 90
# With the JSON wrapper the whole 502 body stays inside speech.py's 200-character
# excerpt, so the model sees the upstream reason, such as the voice list.
ERROR_CHARS = 160
LOGGER_NAME = "ocu.eval_adapter"
SPEECH_ROUTE = "/v1/audio/speech"
SPEECH_FIELDS = {"model", "input", "response_format", "voice", "instructions"}
CONTENT_TYPES = {"mp3": "audio/mpeg", "wav": "audio/wav"}
_CHUNK_BYTES = 64 * 1024
_ENVELOPE_SLACK_BYTES = 64 * 1024
_REDACTED = "[redacted]"
_LOG = logging.getLogger(LOGGER_NAME)


@dataclass(frozen=True)
class Settings:
    upstream_base_url: str
    provider_key: str = field(repr=False)
    adapter_token: str = field(repr=False)

    def redact(self, text: str) -> str:
        for secret in (self.provider_key, self.adapter_token):
            if secret:
                text = text.replace(secret, _REDACTED)
        return text


class _RedactingFilter(logging.Filter):
    """Removes both secrets from a record's message and string arguments.

    The arguments keep their shape: uvicorn's access formatter unpacks them.
    """

    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings

    def _clean(self, value):
        return self.settings.redact(value) if isinstance(value, str) else value

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._clean(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self._clean(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: self._clean(value) for key, value in record.args.items()}
        return True


class UpstreamFailure(Exception):
    """A failure the caller sees as 502; ``message`` is not yet redacted or cut."""

    def __init__(self, message: str, upstream_status: int | None = None):
        super().__init__(message)
        self.message = message
        self.upstream_status = upstream_status


def _settings_from_environment() -> Settings:
    values = {name: os.environ.get(name, "")
              for name in ("PROVIDER_KEY", "ADAPTER_TOKEN", "UPSTREAM_BASE_URL")}
    for name, value in values.items():
        if not value or any(character.isspace() for character in value):
            raise RuntimeError(f"{name} must be set, non-empty and without whitespace")
    parts = urlsplit(values["UPSTREAM_BASE_URL"])
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise RuntimeError("UPSTREAM_BASE_URL must be an http(s):// URL")
    return Settings(upstream_base_url=values["UPSTREAM_BASE_URL"].rstrip("/"),
                    provider_key=values["PROVIDER_KEY"], adapter_token=values["ADAPTER_TOKEN"])


def _install_log_handler(settings: Settings) -> None:
    """INFO lines to stderr, so they reach docker logs under uvicorn's log config."""
    _LOG.setLevel(logging.INFO)
    _LOG.propagate = False
    for existing in list(_LOG.filters):
        _LOG.removeFilter(existing)
    _LOG.addFilter(_RedactingFilter(settings))
    if not any(getattr(handler, "_eval_adapter", False) for handler in _LOG.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        handler._eval_adapter = True
        _LOG.addHandler(handler)
    # uvicorn's own lines (access, errors) pass the same redactor.
    for name in ("uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        for existing in [item for item in logger.filters if isinstance(item, _RedactingFilter)]:
            logger.removeFilter(existing)
        logger.addFilter(_RedactingFilter(settings))


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    settings = _settings_from_environment()
    _install_log_handler(settings)
    app.state.settings = settings
    yield


app = FastAPI(lifespan=_lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def _error(settings: Settings, status: int, message: str) -> JSONResponse:
    # Starlette renders JSON as UTF-8 without \u escapes.
    return JSONResponse({"error": settings.redact(message)[:ERROR_CHARS]}, status_code=status)


def _authorized(request: Request, settings: Settings) -> bool:
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer":
        return False
    # Bytes, so a non-ASCII header compares instead of raising; the token is never empty.
    return hmac.compare_digest(presented.encode("utf-8"), settings.adapter_token.encode("utf-8"))


def _read_cap() -> int:
    """Base64 of the largest accepted audio plus room for the JSON envelope."""
    return math.ceil(MAX_AUDIO_BYTES * 4 / 3) + _ENVELOPE_SLACK_BYTES


async def _read_capped(response: aiohttp.ClientResponse) -> bytes:
    cap = _read_cap()
    if response.content_length is not None and response.content_length > cap:
        raise UpstreamFailure(f"upstream body exceeds {cap} bytes", response.status)
    data = bytearray()
    async for chunk in response.content.iter_chunked(_CHUNK_BYTES):
        data.extend(chunk)
        if len(data) > cap:
            raise UpstreamFailure(f"upstream body exceeds {cap} bytes", response.status)
    return bytes(data)


def _upstream_reason(body: bytes) -> str:
    """``error.param``, else ``error.message``, else the body text."""
    text = body.decode("utf-8", "replace")
    try:
        document = json.loads(text)
    except ValueError:
        document = None
    error = document.get("error") if isinstance(document, dict) else None
    if isinstance(error, dict):
        for key in ("param", "message"):
            if isinstance(error.get(key), str) and error[key]:
                return error[key]
    return " ".join(text.split()) or "empty body"


async def post_upstream(settings: Settings, path: str, payload: dict) -> tuple[int, bytes]:
    """POST JSON to the provider; return (status, body) of a 2xx or raise UpstreamFailure."""
    headers = {"Authorization": f"Bearer {settings.provider_key}"}
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=UPSTREAM_TIMEOUT_SECONDS)
        ) as session:
            # A redirect would carry the provider key to another origin.
            async with session.post(f"{settings.upstream_base_url}{path}", json=payload,
                                    headers=headers, allow_redirects=False) as response:
                status = response.status
                if 300 <= status < 400:
                    raise UpstreamFailure(f"upstream HTTP {status}: redirect not followed", status)
                body = await _read_capped(response)
    except UpstreamFailure:
        raise
    except asyncio.TimeoutError:
        raise UpstreamFailure(f"upstream timed out after {UPSTREAM_TIMEOUT_SECONDS}s") from None
    except aiohttp.ClientError as exc:
        # aiohttp messages name the host and port; report the class only.
        raise UpstreamFailure(f"upstream request failed ({type(exc).__name__})") from None
    if not 200 <= status < 300:
        raise UpstreamFailure(f"upstream HTTP {status}: {_upstream_reason(body)}", status)
    return status, body


def _matches_format(audio: bytes, fmt: str) -> bool:
    if fmt == "wav":
        return audio[:4] == b"RIFF" and audio[8:12] == b"WAVE"
    # An ID3 tag, or an MPEG frame sync: 0xFF, then the top three bits set.
    return audio[:3] == b"ID3" or (len(audio) >= 2 and audio[0] == 0xFF and audio[1] & 0xE0 == 0xE0)


def _decode_audio(body: bytes, fmt: str) -> bytes:
    try:
        document = json.loads(body)
    except ValueError:
        raise UpstreamFailure("upstream response is not JSON", 200) from None
    try:
        data = document["choices"][0]["message"]["audio"]["data"]
    except (KeyError, IndexError, TypeError):
        data = None
    if not isinstance(data, str):
        raise UpstreamFailure("upstream returned no audio", 200)
    try:
        audio = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise UpstreamFailure("upstream audio is not valid base64", 200) from None
    if not audio:
        raise UpstreamFailure("upstream audio is empty", 200)
    if len(audio) > MAX_AUDIO_BYTES:
        raise UpstreamFailure(f"decoded audio exceeds {MAX_AUDIO_BYTES} bytes", 200)
    if not _matches_format(audio, fmt):
        raise UpstreamFailure(f"upstream audio does not start like {fmt}", 200)
    return audio


def _speech_request(document) -> dict | str:
    """The validated request fields, or the reason it is refused."""
    if not isinstance(document, dict):
        return "the body must be a JSON object"
    unknown = sorted(set(document) - SPEECH_FIELDS)
    if unknown:
        return f"unsupported fields: {', '.join(unknown)}"
    for name in ("model", "input"):
        if not isinstance(document.get(name), str) or not document[name]:
            return f"{name} must be a non-empty string"
    fmt = document.get("response_format")
    if not isinstance(fmt, str) or fmt not in CONTENT_TYPES:
        return "response_format must be mp3 or wav"
    for name in ("voice", "instructions"):
        if name in document and not isinstance(document[name], str):
            return f"{name} must be a string"
    return document


def _log(settings: Settings, route: str, status: int, started: float, upstream_status) -> None:
    elapsed = int((time.monotonic() - started) * 1000)
    _LOG.info("route=%s status=%d elapsed_ms=%d upstream=%s",
              route, status, elapsed, upstream_status if upstream_status is not None else "-")


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.post(SPEECH_ROUTE)
async def speech(request: Request):
    settings: Settings = request.app.state.settings
    started = time.monotonic()
    upstream_status = None
    try:
        if not _authorized(request, settings):
            response = _error(settings, 401, "a valid adapter token is required")
        else:
            try:
                document = json.loads(await request.body())
            except ValueError:
                document = None
            fields = _speech_request(document)
            if isinstance(fields, str):
                response = _error(settings, 400, fields)
            else:
                fmt = fields["response_format"]
                messages = []
                if fields.get("instructions"):
                    messages.append({"role": "user", "content": fields["instructions"]})
                messages.append({"role": "assistant", "content": fields["input"]})
                audio_options = {"format": fmt}
                if fields.get("voice"):
                    audio_options["voice"] = fields["voice"]
                payload = {"model": fields["model"], "messages": messages, "audio": audio_options}
                try:
                    upstream_status, body = await post_upstream(settings, "/chat/completions",
                                                                 payload)
                    audio = _decode_audio(body, fmt)
                except UpstreamFailure as failure:
                    upstream_status = failure.upstream_status
                    response = _error(settings, 502, failure.message)
                else:
                    response = Response(content=audio, media_type=CONTENT_TYPES[fmt])
    except Exception as exc:  # any other failure: no traceback may bypass the redactor
        response = _error(settings, 502, f"adapter error ({type(exc).__name__})")
    _log(settings, SPEECH_ROUTE, response.status_code, started, upstream_status)
    return response
