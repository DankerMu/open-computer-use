# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Server-side speech synthesis for the generate_speech MCP tool (#703).

The orchestrator calls an OpenAI-compatible ``POST {TTS_BASE_URL}/audio/speech``
so the TTS key never enters a sandbox. ``configuration()`` reads the TTS_*
variables at call time; the lifespan calls it once so an invalid value refuses
startup. ``synthesize()`` raises ``SpeechError`` whose message has no key and
no URL, so callers may return and log it as is.
"""

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

import aiohttp

MAX_AUDIO_BYTES = 50 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 120
CONTENT_TYPES = {
    "mp3": ("audio/mpeg",),
    "wav": ("audio/wav", "audio/x-wav"),
}
_ERROR_BODY_CHARS = 200
# Enough bytes to redact a key anywhere in the first 200 characters.
_ERROR_BODY_READ_BYTES = 4096
_CHUNK_BYTES = 64 * 1024
_REDACTED = "[redacted]"
_LOG = logging.getLogger("ocu.speech")


class SpeechConfigError(RuntimeError):
    """A TTS_* variable is invalid; raised at startup so the server refuses to start."""


class SpeechError(Exception):
    """Synthesis failed; the message is safe to return and log."""


@dataclass(frozen=True)
class SpeechConfig:
    base_url: str
    model: str
    voice: str
    api_key: str = field(repr=False)
    timeout: int


def _timeout() -> int:
    """Only an absent TTS_TIMEOUT_SECONDS means 120.

    Compose passes ``${TTS_TIMEOUT_SECONDS-120}``, so an explicitly empty value
    reaches the server and must refuse startup rather than default.
    """
    raw = os.environ.get("TTS_TIMEOUT_SECONDS")
    if raw is None:
        return DEFAULT_TIMEOUT_SECONDS
    if not re.fullmatch(r"[0-9]+", raw) or int(raw) <= 0:
        raise SpeechConfigError(
            f"TTS_TIMEOUT_SECONDS ({raw!r}) must be a positive integer; refusing to start"
        )
    return int(raw)


def configuration() -> Optional[SpeechConfig]:
    """The speech configuration, or None when TTS_BASE_URL is unset or empty."""
    timeout = _timeout()
    base_url = os.environ.get("TTS_BASE_URL", "")
    if not base_url:
        return None
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.hostname or any(c.isspace() for c in base_url):
        raise SpeechConfigError("TTS_BASE_URL must be an http(s):// URL; refusing to start")
    model = os.environ.get("TTS_MODEL", "")
    if not model:
        raise SpeechConfigError("TTS_MODEL is required when TTS_BASE_URL is set; refusing to start")
    return SpeechConfig(
        base_url=base_url.rstrip("/"),
        model=model,
        voice=os.environ.get("TTS_VOICE", ""),
        api_key=os.environ.get("TTS_API_KEY", ""),
        timeout=timeout,
    )


def response_format(filename: str) -> Optional[str]:
    """``mp3`` or ``wav`` from the filename extension, else None."""
    lowered = filename.lower()
    for name in CONTENT_TYPES:
        if lowered.endswith("." + name):
            return name
    return None


def _redact(text: str, key: str) -> str:
    return text.replace(key, _REDACTED) if key else text


def _failure(message: str, key: str) -> SpeechError:
    safe = _redact(message, key)
    _LOG.warning("speech synthesis failed: %s", safe)
    return SpeechError(safe)


async def _read_prefix(response, limit: int) -> bytes:
    data = bytearray()
    async for chunk in response.content.iter_chunked(_CHUNK_BYTES):
        data.extend(chunk)
        if len(data) >= limit:
            break
    return bytes(data[:limit])


async def _read_capped(response) -> Optional[bytes]:
    """The whole body, or None as soon as it passes MAX_AUDIO_BYTES."""
    data = bytearray()
    async for chunk in response.content.iter_chunked(_CHUNK_BYTES):
        data.extend(chunk)
        if len(data) > MAX_AUDIO_BYTES:
            return None
    return bytes(data)


async def synthesize(
    config: SpeechConfig, text: str, fmt: str, instructions: str = "", voice: str = "",
) -> bytes:
    """Return the audio bytes, or raise SpeechError with a key-free, URL-free message."""
    body = {"model": config.model, "input": text, "response_format": fmt}
    resolved_voice = voice or config.voice
    if resolved_voice:
        body["voice"] = resolved_voice
    if instructions:
        body["instructions"] = instructions
    headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    key = config.api_key
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=config.timeout)
        ) as session:
            # A redirect could carry the Bearer header to another origin.
            async with session.post(
                f"{config.base_url}/audio/speech", json=body, headers=headers,
                allow_redirects=False,
            ) as response:
                if not 200 <= response.status < 300:
                    raw = await _read_prefix(response, _ERROR_BODY_READ_BYTES)
                    detail = _redact(raw.decode("utf-8", "replace"), key)[:_ERROR_BODY_CHARS]
                    raise _failure(f"speech synthesis failed: HTTP {response.status}: {detail}", key)
                expected = CONTENT_TYPES[fmt]
                if response.content_type not in expected:
                    raise _failure(
                        f"speech synthesis returned {response.content_type or 'no content type'}, "
                        f"expected {' or '.join(expected)} for .{fmt}", key)
                if response.content_length is not None and response.content_length > MAX_AUDIO_BYTES:
                    raise _failure(f"speech synthesis response exceeds {MAX_AUDIO_BYTES} bytes", key)
                audio = await _read_capped(response)
    except SpeechError:
        raise
    except asyncio.TimeoutError:
        raise _failure(f"speech synthesis timed out after {config.timeout}s", key) from None
    except aiohttp.ClientError as exc:
        # aiohttp messages can name the host and path; report the class only.
        raise _failure(f"speech synthesis request failed ({type(exc).__name__})", key) from None
    if audio is None:
        raise _failure(f"speech synthesis response exceeds {MAX_AUDIO_BYTES} bytes", key)
    if not audio:
        raise _failure("speech synthesis returned no audio", key)
    return audio
