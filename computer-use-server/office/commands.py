# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""DocumentServer command-service client on the control-plane address."""
from __future__ import annotations

import asyncio
import json
import os
from enum import Enum
from typing import Any

import aiohttp

from . import config, tokens

HTTP_TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 64 * 1024
_INTENTS = frozenset({"publish", "persist"})


class ForceSaveOutcome(Enum):
    ACCEPTED = "accepted"
    KEY_UNKNOWN = "key_unknown"
    NOTHING_TO_SAVE = "nothing_to_save"
    REJECTED = "rejected"
    UNREACHABLE = "unreachable"


class KeyLookupOutcome(Enum):
    KNOWN = "known"
    KEY_UNKNOWN = "key_unknown"
    UNREACHABLE = "unreachable"


async def forcesave(document_key: str, save_seq: int, intent: str) -> ForceSaveOutcome:
    if type(save_seq) is not int or save_seq < 1:
        raise ValueError("save_seq must be a positive integer")
    if intent not in _INTENTS:
        raise ValueError("intent must be publish or persist")
    userdata = json.dumps({"save_seq": save_seq, "intent": intent}, separators=(",", ":"))
    payload = {"c": "forcesave", "key": document_key, "userdata": userdata}
    status, body = await _post(payload)
    if status is None:
        return ForceSaveOutcome.UNREACHABLE
    if status < 200 or status >= 300:
        return ForceSaveOutcome.REJECTED
    code = _integer_error(body)
    if code == 0:
        return ForceSaveOutcome.ACCEPTED
    if code == 1:
        return ForceSaveOutcome.KEY_UNKNOWN
    if code == 4:
        return ForceSaveOutcome.NOTHING_TO_SAVE
    return ForceSaveOutcome.REJECTED


async def lookup_key(document_key: str) -> KeyLookupOutcome:
    status, body = await _post({"c": "info", "key": document_key})
    if status is None or status < 200 or status >= 300:
        return KeyLookupOutcome.UNREACHABLE
    code = _integer_error(body)
    if code == 0:
        return KeyLookupOutcome.KNOWN
    if code == 1:
        return KeyLookupOutcome.KEY_UNKNOWN
    return KeyLookupOutcome.UNREACHABLE


async def _post(payload: dict[str, Any]) -> tuple[int | None, bytes | None]:
    url = _command_url()
    token = tokens.sign_jwt(payload)
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(
            timeout=timeout,
            trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(),
        ) as session:
            async with session.post(
                url,
                json={"token": token},
                allow_redirects=False,
            ) as response:
                body = await _read_limited(response.content)
                return response.status, body
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None, None


async def _read_limited(stream: aiohttp.StreamReader) -> bytes | None:
    chunks: list[bytes] = []
    total = 0
    limit = MAX_RESPONSE_BYTES + 1
    while total < limit:
        chunk = await stream.read(limit - total)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
    return None


def _integer_error(body: bytes | None) -> int | None:
    if body is None:
        return None
    try:
        parsed = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    code = parsed.get("error")
    if type(code) is not int:
        return None
    return code


def _command_url() -> str:
    raw = os.environ.get(config.OCU_OFFICE_DOCSERVER_URL, "").strip().rstrip("/")
    if not raw:
        raise ValueError("OCU_OFFICE_DOCSERVER_URL is not configured")
    return f"{raw}/command"
