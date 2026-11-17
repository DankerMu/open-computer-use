# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Evaluation-only adapter between OCU, WebUI and dmxapi. Not part of any release.

``POST /v1/audio/speech`` takes OCU's OpenAI-style speech request (the one
``computer-use-server/speech.py`` sends) and calls dmxapi ``chat/completions``
with ``mimo-v2.5-tts``, which returns the audio base64-encoded in the requested
format.

``POST /v1/images/generations`` (JSON) and ``POST /v1/images/edits``
(multipart) take WebUI's ``openai`` image-engine requests and call dmxapi
``/responses``, which serves the Seedream model, with ``prompt`` as ``input``,
edit files as ``data:`` URLs, no ``n`` and ``watermark`` false. The answer is
always ``data[].b64_json``, never a ``url``: WebUI fetches a returned URL with
its image Authorization header, which holds the adapter token.

The adapter holds the provider key; callers present ``ADAPTER_TOKEN``, checked
before the body is read. Every upstream failure is a 502 whose JSON body fits
inside speech.py's 200-character error excerpt; any other exception is a 502
naming only its class. Every response body and log line passes the redactor,
and the upstream call never follows a redirect, so the provider key leaves only
toward ``UPSTREAM_BASE_URL`` in the Authorization header.

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
from python_multipart.exceptions import FormParserError
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

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
# Inbound caps, enforced while reading and after the token check. 1 MiB holds any
# JSON request WebUI or OCU sends; an edit carries its images.
JSON_BODY_CAP_BYTES = 1024 * 1024
EDIT_BODY_CAP_BYTES = 64 * 1024 * 1024
MAX_IMAGE_BYTES = 50 * 1024 * 1024
# WebUI's image session has no total timeout when AIOHTTP_CLIENT_TIMEOUT is
# unset, as in the stack, so the adapter bounds the call; measured 9-21 s.
IMAGE_TIMEOUT_SECONDS = 180
GENERATIONS_ROUTE = "/v1/images/generations"
EDITS_ROUTE = "/v1/images/edits"
IMAGE_FIELDS = {"model", "prompt", "n", "size", "response_format"}
# background is accepted and not forwarded: the upstream behavior is unmeasured.
EDIT_TEXT_FIELDS = IMAGE_FIELDS | {"background"}
EDIT_FILE_FIELDS = {"image", "image[]"}
EDIT_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp"}
IMAGE_RESPONSE_FORMATS = {"b64_json", "url"}
# Fields the adapter sets in the upstream request; a configured extra may not.
ADAPTER_SET_FIELDS = {"input", "image", "watermark"}
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
        # A traceback holds the exception text. Formatters print a set exc_text and
        # format exc_info only when exc_text is empty, so replace one with the other.
        if record.exc_info:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
        record.exc_text = self._clean(record.exc_text)
        record.stack_info = self._clean(record.stack_info)
        return True


class UpstreamFailure(Exception):
    """A failure the caller sees as 502; ``message`` is not yet redacted or cut."""

    def __init__(self, message: str, upstream_status: int | None = None):
        super().__init__(message)
        self.message = message
        self.upstream_status = upstream_status


class RequestRefused(Exception):
    """A request refused before any upstream call (400 or 413)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


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


class _CatchAll:
    """Turns any exception a route lets through into a redacted 502 naming only its class.

    Pure ASGI around the routes, so no route can bypass it and no traceback
    reaches uvicorn's error log.
    """

    def __init__(self, app):  # Starlette passes the wrapped app as app=
        self.inner = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return
        started = time.monotonic()
        response_started = False

        async def watched(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.inner(scope, receive, watched)
        except Exception as exc:
            _log(scope["path"], 502, started, None)
            # A started response cannot be replaced; the connection just ends.
            if not response_started:
                settings = scope["app"].state.settings
                response = _error(settings, 502, f"adapter error ({type(exc).__name__})")
                await response(scope, receive, send)


app.add_middleware(_CatchAll)


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


def _read_cap(max_decoded_bytes: int) -> int:
    """Base64 of the largest accepted payload plus room for the JSON envelope."""
    return math.ceil(max_decoded_bytes * 4 / 3) + _ENVELOPE_SLACK_BYTES


async def _read_body(request: Request, cap: int) -> bytes:
    """The request body, refused with 413 once a declared or read length passes ``cap``."""
    refused = RequestRefused(413, f"request body exceeds {cap} bytes")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > cap:
        raise refused
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > cap:
            raise refused
    return bytes(data)


async def _read_capped(response: aiohttp.ClientResponse, cap: int) -> bytes:
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


async def post_upstream(settings: Settings, path: str, payload: dict, *, read_cap: int,
                        timeout: float) -> tuple[int, bytes]:
    """POST JSON to the provider; return (status, body) of a 2xx or raise UpstreamFailure."""
    headers = {"Authorization": f"Bearer {settings.provider_key}"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            # A redirect would carry the provider key to another origin.
            async with session.post(f"{settings.upstream_base_url}{path}", json=payload,
                                    headers=headers, allow_redirects=False) as response:
                status = response.status
                if 300 <= status < 400:
                    raise UpstreamFailure(f"upstream HTTP {status}: redirect not followed", status)
                body = await _read_capped(response, read_cap)
    except UpstreamFailure:
        raise
    except asyncio.TimeoutError:
        raise UpstreamFailure(f"upstream timed out after {timeout}s") from None
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


def _json_document(body: bytes):
    try:
        return json.loads(body)
    except ValueError:
        return None


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


async def _speech(settings: Settings, request: Request) -> tuple[int, Response]:
    fields = _speech_request(_json_document(await _read_body(request, JSON_BODY_CAP_BYTES)))
    if isinstance(fields, str):
        raise RequestRefused(400, fields)
    fmt = fields["response_format"]
    messages = []
    if fields.get("instructions"):
        messages.append({"role": "user", "content": fields["instructions"]})
    messages.append({"role": "assistant", "content": fields["input"]})
    audio_options = {"format": fmt}
    if fields.get("voice"):
        audio_options["voice"] = fields["voice"]
    payload = {"model": fields["model"], "messages": messages, "audio": audio_options}
    status, body = await post_upstream(settings, "/chat/completions", payload,
                                       read_cap=_read_cap(MAX_AUDIO_BYTES),
                                       timeout=UPSTREAM_TIMEOUT_SECONDS)
    return status, Response(content=_decode_audio(body, fmt), media_type=CONTENT_TYPES[fmt])


def _image_fields(fields: dict) -> dict:
    """The common image fields, checked; ``n`` is checked by each route."""
    for name in ("model", "prompt"):
        if not isinstance(fields.get(name), str) or not fields[name]:
            raise RequestRefused(400, f"{name} must be a non-empty string")
    if "size" in fields and not isinstance(fields["size"], str):
        raise RequestRefused(400, "size must be a string")
    fmt = fields.get("response_format", "b64_json")
    if not isinstance(fmt, str) or fmt not in IMAGE_RESPONSE_FORMATS:
        raise RequestRefused(400, "response_format must be b64_json or url")
    return fields


def _image_payload(fields: dict, extras: dict, images: list[str] | None = None) -> dict:
    """The /responses request: prompt as input, never n, always b64_json."""
    payload = {"model": fields["model"], "input": fields["prompt"]}
    if "size" in fields:
        payload["size"] = fields["size"]
    payload.update(extras)
    payload["watermark"] = False
    payload["response_format"] = "b64_json"
    if images is not None:
        payload["image"] = images
    return payload


def _is_image(image: bytes) -> bool:
    return (image[:3] == b"\xff\xd8\xff" or image[:4] == b"\x89PNG"
            or (image[:4] == b"RIFF" and image[8:12] == b"WEBP"))


def _b64_image(item) -> dict:
    """One response item: only the checked ``b64_json``, so no ``url`` or other field passes."""
    encoded = item.get("b64_json") if isinstance(item, dict) else None
    if not isinstance(encoded, str) or not encoded:
        raise UpstreamFailure("upstream image item has no b64_json", 200)
    try:
        image = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise UpstreamFailure("upstream image is not valid base64", 200) from None
    if len(image) > MAX_IMAGE_BYTES:
        raise UpstreamFailure(f"decoded image exceeds {MAX_IMAGE_BYTES} bytes", 200)
    if not _is_image(image):
        raise UpstreamFailure("upstream image is not JPEG, PNG or WebP", 200)
    return {"b64_json": encoded}


def _images_response(body: bytes) -> dict:
    document = _json_document(body)
    if document is None:
        raise UpstreamFailure("upstream response is not JSON", 200)
    items = document.get("data") if isinstance(document, dict) else None
    if not isinstance(items, list) or not items:
        raise UpstreamFailure("upstream returned no images", 200)
    data = [_b64_image(item) for item in items]
    created = document.get("created")
    if type(created) is not int:
        created = int(time.time())
    return {"created": created, "data": data}


async def _call_images(settings: Settings, payload: dict) -> tuple[int, Response]:
    status, body = await post_upstream(settings, "/responses", payload,
                                       read_cap=_read_cap(MAX_IMAGE_BYTES),
                                       timeout=IMAGE_TIMEOUT_SECONDS)
    return status, JSONResponse(_images_response(body))


async def _generation(settings: Settings, request: Request) -> tuple[int, Response]:
    document = _json_document(await _read_body(request, JSON_BODY_CAP_BYTES))
    if not isinstance(document, dict):
        raise RequestRefused(400, "the body must be a JSON object")
    # bool is an int subclass: true is not 1.
    if "n" in document and (type(document["n"]) is not int or document["n"] != 1):
        raise RequestRefused(400, "n must be 1")
    fields = _image_fields({key: value for key, value in document.items() if key in IMAGE_FIELDS})
    extras = {key: value for key, value in document.items() if key not in IMAGE_FIELDS}
    taken = sorted(set(extras) & ADAPTER_SET_FIELDS)
    if taken:
        raise RequestRefused(400, f"fields set by the adapter: {', '.join(taken)}")
    return await _call_images(settings, _image_payload(fields, extras))


async def _edit_form(request: Request, body: bytes):
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise RequestRefused(400, "the body must be multipart/form-data")

    async def once():
        yield body

    try:
        return await MultiPartParser(request.headers, once()).parse()
    except (MultiPartException, FormParserError) as exc:
        # Starlette wraps only some of python-multipart's parse errors.
        raise RequestRefused(400, f"invalid multipart body ({type(exc).__name__})") from None


async def _edit_parts(form) -> tuple[dict, list[str]]:
    """Text fields and one ``data:`` URL per file, in upload order."""
    fields, images = {}, []
    for name, value in form.multi_items():
        if not isinstance(value, UploadFile):
            if name not in EDIT_TEXT_FIELDS or name in fields:
                raise RequestRefused(400, f"unsupported or repeated field: {name}")
            fields[name] = value
            continue
        if name not in EDIT_FILE_FIELDS:
            raise RequestRefused(400, f"unsupported file field: {name}")
        # Forwarded as declared, not sniffed: WebUI labels a stored b64_json image
        # image/png whatever its bytes, and that image is what an edit sends back.
        if value.content_type not in EDIT_IMAGE_TYPES:
            raise RequestRefused(400, "each file must be declared image/png, image/jpeg or image/webp")
        data = await value.read()
        if not data:
            raise RequestRefused(400, "each file must be non-empty")
        images.append(f"data:{value.content_type};base64,{base64.b64encode(data).decode('ascii')}")
    return fields, images


async def _edit(settings: Settings, request: Request) -> tuple[int, Response]:
    form = await _edit_form(request, await _read_body(request, EDIT_BODY_CAP_BYTES))
    try:
        fields, images = await _edit_parts(form)
    finally:
        await form.close()
    if not images:
        raise RequestRefused(400, "at least one file under image or image[] is required")
    if fields.get("n", "1") != "1":
        raise RequestRefused(400, "n must be 1")
    fields = _image_fields({key: value for key, value in fields.items() if key in IMAGE_FIELDS})
    return await _call_images(settings, _image_payload(fields, {}, images))


def _log(route: str, status: int, started: float, upstream_status) -> None:
    elapsed = int((time.monotonic() - started) * 1000)
    _LOG.info("route=%s status=%d elapsed_ms=%d upstream=%s",
              route, status, elapsed, upstream_status if upstream_status is not None else "-")


async def _serve(request: Request, route: str, handle) -> Response:
    """Token first, then the route; refusals and upstream failures become JSON errors.

    Any other exception propagates to ``_CatchAll``.
    """
    settings: Settings = request.app.state.settings
    started = time.monotonic()
    upstream_status = None
    if not _authorized(request, settings):
        response = _error(settings, 401, "a valid adapter token is required")
    else:
        try:
            upstream_status, response = await handle(settings, request)
        except RequestRefused as refused:
            response = _error(settings, refused.status, refused.message)
        except UpstreamFailure as failure:
            upstream_status = failure.upstream_status
            response = _error(settings, 502, failure.message)
    _log(route, response.status_code, started, upstream_status)
    return response


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.post(SPEECH_ROUTE)
async def speech(request: Request):
    return await _serve(request, SPEECH_ROUTE, _speech)


@app.post(GENERATIONS_ROUTE)
async def image_generations(request: Request):
    return await _serve(request, GENERATIONS_ROUTE, _generation)


@app.post(EDITS_ROUTE)
async def image_edits(request: Request):
    return await _serve(request, EDITS_ROUTE, _edit)
