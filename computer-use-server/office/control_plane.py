# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""DocumentServer source delivery and authenticated callback persist."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from typing import Any
from urllib.parse import unquote

from fastapi import Request
from fastapi.responses import JSONResponse, Response

import docker_manager
from auth_guard import AuthGuardError, canonical_chat_id

from . import callback, sessions, tokens, versions
from .store import OfficeStore, StateCorruptError

_LOG = logging.getLogger("ocu.office")
_BEARER = "bearer "


class InvalidTicketError(ValueError):
    reason = "invalid_ticket"


class UnknownCallbackSessionError(ValueError):
    reason = "unknown_session"


class InvalidCallbackTokenError(ValueError):
    reason = "invalid_token"


def _error(status: int, reason: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"reason": reason})


def _sanitize(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ")


def _log_callback_rejection(chat_id: str, session_id: str, reason: str) -> None:
    _LOG.error(
        "office callback rejected chat=%s session=%s reason=%s",
        _sanitize(chat_id),
        _sanitize(session_id),
        _sanitize(reason),
    )


def _chat_directory_exists(chat: str) -> bool:
    root = docker_manager._control_dir(chat)
    try:
        info = os.lstat(root)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return not stat.S_ISLNK(info.st_mode) and stat.S_ISDIR(info.st_mode)


def serve_source(request: Request) -> Response:
    try:
        binding = tokens.verify_source_ticket(unquote(request.path_params["ticket"]))
    except tokens.InvalidTokenError:
        return _error(401, "invalid_ticket")
    except tokens.MissingSigningKeyError:
        return _error(401, "invalid_ticket")
    chat = docker_manager.canonical_lock_chat_id(binding["chat_id"])
    if not _chat_directory_exists(chat):
        return _error(401, "invalid_ticket")
    store = OfficeStore()
    with docker_manager._combined_lock(chat, create=False) as lock:
        if lock is None:
            return _error(401, "invalid_ticket")
        try:
            store._assert_chat_root_safe(chat, allow_missing=False)
            state = store._snapshot(store._load(chat, create=False))
            content = _bound_version_bytes(store, chat, state, binding)
        except InvalidTicketError:
            return _error(401, "invalid_ticket")
        except StateCorruptError:
            return _error(500, "state_corrupt")
    return Response(content=content, media_type="application/octet-stream")


async def admit_callback(request: Request) -> JSONResponse:
    presented_chat = request.path_params["chat_id"]
    presented_session = request.path_params["session_id"]
    try:
        chat = canonical_chat_id(presented_chat)
    except AuthGuardError:
        return _error(400, "invalid_chat_id")
    body = await request.body()
    payload, reason = _verified_callback_object(request, body)
    if payload is None:
        _log_callback_rejection(chat, presented_session, reason)
        return _error(401, "invalid_token")
    return await asyncio.to_thread(_admit_authenticated_callback, chat, presented_session, payload)


def _admit_authenticated_callback(chat: str, presented_session: str, payload: dict[str, Any]) -> JSONResponse:
    if not _chat_directory_exists(chat):
        _log_callback_rejection(chat, presented_session, "unknown_session")
        return _error(404, "unknown_session")
    store = OfficeStore()
    with docker_manager._combined_lock(chat, create=False) as lock:
        if lock is None:
            _log_callback_rejection(chat, presented_session, "unknown_session")
            return _error(404, "unknown_session")
        try:
            store._assert_chat_root_safe(chat, allow_missing=False)
            state = store._snapshot(store._load(chat, create=False))
            _require_callback_binding(state, presented_session, payload)
            return callback.process_authenticated_callback(store, chat, presented_session, payload)
        except UnknownCallbackSessionError:
            _log_callback_rejection(chat, presented_session, "unknown_session")
            return _error(404, "unknown_session")
        except InvalidCallbackTokenError:
            _log_callback_rejection(chat, presented_session, "invalid_token")
            return _error(401, "invalid_token")
        except StateCorruptError:
            _log_callback_rejection(chat, presented_session, "state_corrupt")
            return _error(500, "state_corrupt")



def _bound_version_bytes(store: OfficeStore, chat: str, state: dict[str, Any], binding: dict[str, Any]) -> bytes:
    file_id = binding["file_id"]
    session_id = binding["session_id"]
    number = binding["version"]
    documents = state.get("documents")
    sessions_map = state.get("sessions")
    if not isinstance(documents, dict) or file_id not in documents:
        raise InvalidTicketError()
    document = documents[file_id]
    if not isinstance(document, dict):
        raise StateCorruptError(f"office document {file_id} is invalid")
    listed = document.get("versions")
    if not isinstance(listed, list):
        raise StateCorruptError(f"office document {file_id} versions are invalid")
    record = None
    for item in listed:
        persisted = versions._persisted_version(item)
        if persisted["number"] == number:
            record = persisted
            break
    if record is None:
        raise InvalidTicketError()
    if not isinstance(sessions_map, dict) or session_id not in sessions_map:
        raise InvalidTicketError()
    session = sessions._persisted_session(session_id, sessions_map[session_id])
    if session["file_id"] != file_id:
        raise InvalidTicketError()
    return versions.read_version_bytes(store, chat, record["sha256"])


def _require_callback_binding(state: dict[str, Any], session_id: str, payload: dict[str, Any]) -> None:
    sessions_map = state.get("sessions")
    if not isinstance(sessions_map, dict) or session_id not in sessions_map:
        raise UnknownCallbackSessionError()
    session = sessions._persisted_session(session_id, sessions_map[session_id])
    key = payload.get("key")
    if not isinstance(key, str) or not key or key != session["document_key"]:
        raise InvalidCallbackTokenError()


def _verified_callback_object(request: Request, body: bytes) -> tuple[dict[str, Any] | None, str]:
    headers = {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in request.scope.get("headers") or []
    }
    if "authorization" in headers:
        presented = headers["authorization"]
        if len(presented) < 7 or presented[:7].lower() != _BEARER:
            return None, "invalid_token"
        try:
            verified = tokens.verify_jwt(presented[7:])
        except (tokens.InvalidTokenError, tokens.MissingSigningKeyError):
            return None, "invalid_token"
        nested = verified.get("payload")
        if not isinstance(nested, dict):
            return None, "invalid_token"
        return nested, "invalid_token"
    if not body:
        return None, "invalid_token"
    try:
        parsed = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        return None, "invalid_token"
    if not isinstance(parsed, dict) or "token" not in parsed:
        return None, "invalid_token"
    try:
        verified = tokens.verify_jwt(parsed.get("token"))
    except (tokens.InvalidTokenError, tokens.MissingSigningKeyError):
        return None, "invalid_token"
    if not isinstance(verified, dict):
        return None, "invalid_token"
    return verified, "invalid_token"


def install_source_access_log_filter() -> None:
    logger = logging.getLogger("uvicorn.access")
    for existing in logger.filters:
        if isinstance(existing, SourceTicketAccessFilter):
            return
    logger.addFilter(SourceTicketAccessFilter())


class SourceTicketAccessFilter(logging.Filter):
    """Redact source tickets in Uvicorn access records without changing paths."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 3:
            return True
        path = args[2]
        if not isinstance(path, str) or "/office/source/" not in path:
            return True
        redacted = _redact_source_path(path)
        if redacted == path:
            return True
        record.args = args[:2] + (redacted,) + args[3:]
        return True


def _redact_source_path(path: str) -> str:
    prefix, remainder = path.split("/office/source/", 1)
    ticket, separator, query = remainder.partition("?")
    ticket = _redact_segment(ticket)
    if not separator:
        return f"{prefix}/office/source/{ticket}"
    pairs = []
    for part in query.split("&"):
        if not part:
            continue
        name, eq, value = part.partition("=")
        if name == "ticket" and eq:
            pairs.append(f"ticket={_redact_segment(value)}")
        else:
            pairs.append(part)
    return f"{prefix}/office/source/{ticket}?{'&'.join(pairs)}"


def _redact_segment(value: str) -> str:
    return "*" * len(value) if value else value
