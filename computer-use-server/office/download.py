# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Confined DocumentServer content fetch for callback persist."""
from __future__ import annotations

import asyncio
import os
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp
from outputs_broker import MAX_FILE_SIZE

from . import commands, config

MAX_REDIRECTS = 5
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_IDENTITY = {"Accept-Encoding": "identity"}


class DownloadRejected(ValueError):
    status = 422
    reason = "download_url_rejected"


class DownloadFailed(RuntimeError):
    status = 502
    reason = "download_failed"


class FileTooLarge(ValueError):
    status = 413
    reason = "file_too_large"


def fetch_callback_content(url: str, loop: asyncio.AbstractEventLoop) -> bytes:
    target = confined_internal_url(url)
    if loop.is_closed() or loop.is_running() is False:
        raise DownloadFailed("callback download loop is unavailable")
    future = asyncio.run_coroutine_threadsafe(_download(target), loop)
    try:
        return future.result(timeout=commands.HTTP_TIMEOUT_SECONDS)
    except FutureTimeoutError as extra:
        future.cancel()
        raise DownloadFailed("callback download transfer failed") from extra
    except FutureCancelledError as extra:
        raise DownloadFailed("callback download transfer failed") from extra


def confined_internal_url(url: str) -> str:
    presented = _parse_http_url(url)
    if presented is None:
        raise DownloadRejected("callback download address is not an accepted origin")
    browser = _origin_of(os.environ.get(config.OCU_OFFICE_DOCSERVER_ORIGIN, "").strip())
    internal_raw = os.environ.get(config.OCU_OFFICE_DOCSERVER_URL, "").strip()
    internal = _origin_of(internal_raw)
    if internal is None or _origin(presented) not in {browser, internal}:
        raise DownloadRejected("callback download address is not an accepted origin")
    rebuilt = _rebuild(internal_raw, presented)
    if rebuilt is None:
        raise DownloadRejected("callback download address is not an accepted origin")
    return rebuilt


def _parse_http_url(url: Any):
    if not isinstance(url, str) or not url or "\x00" in url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"}:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    try:
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if host is None or host == "":
        return None
    if port is not None and (type(port) is not int or port < 1 or port > 65535):
        return None
    return parts


def _origin(parts) -> tuple[str, str, int] | None:
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        return None
    try:
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not isinstance(host, str) or not host:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    elif type(port) is not int or port < 1 or port > 65535:
        return None
    return (scheme, host.lower(), port)


def _origin_of(url: str) -> tuple[str, str, int] | None:
    parts = _parse_http_url(url)
    if parts is None:
        return None
    return _origin(parts)


def _rebuild(internal_url: str, presented) -> str | None:
    internal = _parse_http_url(internal_url)
    if internal is None:
        return None
    path = presented.path if presented.path else "/"
    if path.startswith("//"):
        return None
    return urlunsplit((internal.scheme, internal.netloc, path, presented.query, ""))


def _redirect_target(current: str, location: str | None) -> str:
    if not isinstance(location, str) or not location:
        raise DownloadFailed("callback download redirect is invalid")
    try:
        joined = urljoin(current, location)
    except ValueError as extra:
        raise DownloadFailed("callback download redirect is invalid") from extra
    parts = _parse_http_url(joined)
    if parts is None:
        raise DownloadFailed("callback download redirect is invalid")
    origin = _origin(parts)
    internal = _origin_of(os.environ.get(config.OCU_OFFICE_DOCSERVER_URL, "").strip())
    if origin is None or internal is None or origin != internal:
        raise DownloadFailed("callback download redirected off origin")
    rebuilt = _rebuild(os.environ.get(config.OCU_OFFICE_DOCSERVER_URL, "").strip(), parts)
    if rebuilt is None:
        raise DownloadFailed("callback download redirect is invalid")
    return rebuilt


async def _download(url: str) -> bytes:
    timeout = aiohttp.ClientTimeout(total=commands.HTTP_TIMEOUT_SECONDS)
    current = url
    try:
        async with aiohttp.ClientSession(
            timeout=timeout,
            trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            auto_decompress=False,
            headers=_IDENTITY,
        ) as session:
            async with asyncio.timeout(commands.HTTP_TIMEOUT_SECONDS):
                for _hop in range(MAX_REDIRECTS + 1):
                    async with session.get(
                        current,
                        allow_redirects=False,
                        timeout=timeout,
                        headers=_IDENTITY,
                    ) as response:
                        if response.status in _REDIRECTS:
                            current = _redirect_target(current, response.headers.get("Location"))
                            continue
                        if response.status < 200 or response.status >= 300:
                            raise DownloadFailed("callback download transfer failed")
                        length = response.headers.get("Content-Length")
                        if length is not None:
                            try:
                                advertised = int(length)
                            except ValueError:
                                advertised = -1
                            if advertised > MAX_FILE_SIZE:
                                raise FileTooLarge("callback download exceeds the per-file limit")
                        return await _read_limited(response.content)
                raise DownloadFailed("callback download redirected too many times")
    except FileTooLarge:
        raise
    except DownloadFailed:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, OSError) as extra:
        raise DownloadFailed("callback download transfer failed") from extra


async def _read_limited(stream: aiohttp.StreamReader) -> bytes:
    chunks: list[bytes] = []
    total = 0
    limit = MAX_FILE_SIZE + 1
    while total < limit:
        chunk = await stream.read(limit - total)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
    raise FileTooLarge("callback download exceeds the per-file limit")
