# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""DocumentServer JWT and source-ticket signing with standard-library HS256."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import time
from typing import Any

from . import config

_HEADER = {"alg": "HS256", "typ": "JWT"}
_TICKET_PURPOSE = b"ocu-office-source-ticket"
_B64URL_ALPHABET = re.compile(r"^[A-Za-z0-9_-]*\Z")


class InvalidTokenError(ValueError):
    """Token is missing, malformed, expired, or not signed with the expected key."""

    def __init__(self) -> None:
        super().__init__("invalid token")


class MissingSigningKeyError(ValueError):
    """A required signing secret is absent; the value is never included."""

    def __init__(self, name: str) -> None:
        super().__init__(f"{name} is not configured")


def sign_jwt(payload: dict[str, Any]) -> str:
    return _encode(payload, _jwt_secret())


def verify_jwt(token: str | None) -> dict[str, Any]:
    return _decode(token, _jwt_secret(), require_exp=False)


def sign_source_ticket(chat_id: str, file_id: str, version: int, session_id: str) -> str:
    _require_identity("chat_id", chat_id)
    _require_identity("file_id", file_id)
    _require_identity("session_id", session_id)
    if type(version) is not int or version < 1:
        raise ValueError("version must be a positive integer")
    payload = {
        "chat_id": chat_id,
        "file_id": file_id,
        "version": version,
        "session_id": session_id,
        "exp": int(time.time()) + config.SOURCE_TICKET_TTL_SECONDS,
        "jti": secrets.token_hex(16),
    }
    return _encode(payload, _ticket_secret())


def verify_source_ticket(token: str | None) -> dict[str, Any]:
    payload = _decode(token, _ticket_secret(), require_exp=True)
    chat_id = payload.get("chat_id")
    file_id = payload.get("file_id")
    session_id = payload.get("session_id")
    version = payload.get("version")
    if (
        not isinstance(chat_id, str)
        or not chat_id
        or not isinstance(file_id, str)
        or not file_id
        or not isinstance(session_id, str)
        or not session_id
        or type(version) is not int
        or version < 1
    ):
        raise InvalidTokenError()
    return {
        "chat_id": chat_id,
        "file_id": file_id,
        "version": version,
        "session_id": session_id,
    }


def _encode(payload: dict[str, Any], secret: bytes) -> str:
    if not isinstance(payload, dict):
        raise ValueError("JWT payload must be an object")
    header = _b64url(json.dumps(_HEADER, separators=(",", ":")).encode("utf-8"))
    body = _b64url(json.dumps(dict(payload), separators=(",", ":")).encode("utf-8"))
    signing = f"{header}.{body}".encode("ascii")
    signature = _b64url(hmac.new(secret, signing, hashlib.sha256).digest())
    return f"{header}.{body}.{signature}"


def _decode(token: str | None, secret: bytes, *, require_exp: bool) -> dict[str, Any]:
    if not isinstance(token, str) or token.count(".") != 2:
        raise InvalidTokenError()
    header_b64, payload_b64, signature_b64 = token.split(".")
    try:
        signing = f"{header_b64}.{payload_b64}".encode("ascii")
    except UnicodeEncodeError:
        raise InvalidTokenError() from None
    try:
        header = json.loads(_b64url_decode(header_b64))
    except (ValueError, json.JSONDecodeError, RecursionError):
        raise InvalidTokenError() from None
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        raise InvalidTokenError()
    expected = hmac.new(secret, signing, hashlib.sha256).digest()
    try:
        presented = _b64url_decode(signature_b64)
    except ValueError:
        raise InvalidTokenError() from None
    if not hmac.compare_digest(presented, expected):
        raise InvalidTokenError()
    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, json.JSONDecodeError, RecursionError):
        raise InvalidTokenError() from None
    if not isinstance(payload, dict):
        raise InvalidTokenError()
    now = time.time()
    if "exp" in payload or require_exp:
        if "exp" not in payload or not _usable_numeric_date(payload["exp"]) or now >= payload["exp"]:
            raise InvalidTokenError()
    if "nbf" in payload:
        if not _usable_numeric_date(payload["nbf"]) or now < payload["nbf"]:
            raise InvalidTokenError()
    return payload


def _jwt_secret() -> bytes:
    return _configured_secret(config.OCU_OFFICE_JWT_SECRET)


def _ticket_secret() -> bytes:
    internal = _configured_secret("OCU_INTERNAL_TOKEN")
    return hmac.new(internal, _TICKET_PURPOSE, hashlib.sha256).digest()


def _configured_secret(name: str) -> bytes:
    raw = os.environ.get(name, "")
    if not raw.strip():
        raise MissingSigningKeyError(name)
    return raw.encode("utf-8")


def _require_identity(name: str, value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a nonempty string")


def _usable_numeric_date(value: Any) -> bool:
    if type(value) is bool or type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    if not isinstance(segment, str) or not _B64URL_ALPHABET.fullmatch(segment):
        raise ValueError("invalid token encoding")
    padding = "=" * ((4 - len(segment) % 4) % 4)
    try:
        decoded = base64.urlsafe_b64decode(segment + padding)
    except (ValueError, TypeError) as extra:
        raise ValueError("invalid token encoding") from extra
    if _b64url(decoded) != segment:
        raise ValueError("invalid token encoding")
    return decoded
