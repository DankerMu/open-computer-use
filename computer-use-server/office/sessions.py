# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Validate, capture, and sign one opening session under the canonical chat lock."""
from __future__ import annotations

import os
import uuid
from typing import Any
from urllib.parse import quote

import docker_manager
from fastapi import Request
from fastapi.responses import JSONResponse
from outputs_broker import CorruptIndexError, FileIdNotFoundError, OutputsBroker, OutputsBrokerError

from . import config, epoch, ooxml, tokens, versions
from .epoch import RestoreEpochError
from .store import OfficeStore, StateCorruptError, StateDurabilityError
from .versions import StorageLowError
from .workspace import FileTooLargeError, UnsafePathError

OPEN_STATES = frozenset({"opening", "editing", "saving", "closing", "conflict"})
FINAL_STATES = frozenset({"closed", "error", "orphaned"})
KNOWN_STATES = OPEN_STATES | FINAL_STATES
DOCUMENT_TYPES = {"docx": "word", "xlsx": "cell", "pptx": "slide"}


class SessionAlreadyOpenError(RuntimeError):
    reason = "session_already_open"

    def __init__(self) -> None:
        super().__init__("document already has an open session")


class CreationFailedError(RuntimeError):
    reason = "creation_failed"

    def __init__(self) -> None:
        super().__init__("session creation failed")


class UnsupportedTypeError(ValueError):
    reason = "unsupported_type"

    def __init__(self) -> None:
        super().__init__("file name is not a supported Office type")


def create_session(request: Request) -> JSONResponse:
    try:
        created = _create_session(request.scope["ocu_chat_id"], request.path_params["file_id"])
    except FileIdNotFoundError:
        return _error(404, "unknown_file")
    except UnsupportedTypeError:
        return _error(415, "unsupported_type")
    except ooxml.CorruptDocumentError:
        return _error(422, "corrupt_document")
    except UnsafePathError:
        return _error(422, "unsafe_path")
    except FileTooLargeError:
        return _error(413, "file_too_large")
    except StorageLowError:
        return _error(503, "storage_low")
    except SessionAlreadyOpenError:
        return _error(409, "session_already_open")
    except (RestoreEpochError, StateCorruptError, CorruptIndexError):
        return _error(500, "state_corrupt")
    except StateDurabilityError:
        return _error(500, "state_durability")
    except (CreationFailedError, tokens.MissingSigningKeyError, OutputsBrokerError, OSError):
        return _error(500, "creation_failed")
    return JSONResponse(status_code=201, content=created)


def _create_session(chat_id: str, file_id: str) -> dict[str, Any]:
    chat = docker_manager.canonical_lock_chat_id(chat_id)
    if not isinstance(file_id, str) or not file_id or "\x00" in file_id:
        raise FileIdNotFoundError("file_id is not an active persisted identity")
    store = OfficeStore()
    broker = OutputsBroker()
    store._assert_chat_root_safe(chat, allow_missing=True)
    with docker_manager._combined_lock(chat):
        store._assert_chat_root_safe(chat, allow_missing=False)
        relative_path = broker.resolve_file_id(chat, file_id)
        document_type = _document_type(relative_path)
        if document_type is None:
            raise UnsupportedTypeError()
        state = store.read(chat)
        _refuse_open_session(state, file_id)
        content, digest = store.read_workspace_file(chat, relative_path, max_bytes=broker.max_file_size)
        ooxml.validate_ooxml(content, document_type)
        store.check_free_space(chat, config.MIN_FREE_BYTES)
        restore_epoch = epoch.current_epoch()
        listed = versions._document_versions(state, file_id)
        latest = listed[-1] if listed else None
        parent = latest["number"] if latest else None
        expected_number = (
            latest["number"] if latest and latest["sha256"] == digest else (parent or 0) + 1
        )
        session_id = str(uuid.uuid4())
        document_key = uuid.uuid4().hex
        _require_unique_session_identity(state, session_id, document_key)
        editor_config = _signed_editor_config(
            chat, file_id, relative_path, document_type, document_key, session_id, expected_number
        )

        def mutate_state(working, selected):
            if selected["number"] != expected_number:
                raise CreationFailedError()
            _refuse_open_session(working, file_id)
            _require_unique_session_identity(working, session_id, document_key)
            working["documents"][file_id].update({
                "file_id": file_id,
                "type": document_type,
                "path": relative_path,
                "published_version": selected["number"],
                "published_sha256": selected["sha256"],
            })
            working["sessions"][session_id] = {
                "session_id": session_id,
                "file_id": file_id,
                "document_key": document_key,
                "baseline_sha256": digest,
                "restore_epoch": restore_epoch,
                "state": "opening",
                "save_seq": 0,
            }

        store.store_version(
            chat, file_id, content, source="workspace", parent=parent, published=True,
            min_free_bytes=config.MIN_FREE_BYTES, mutate_state=mutate_state,
        )
        return {
            "session_id": session_id,
            "file_id": file_id,
            "document_key": document_key,
            "state": "opening",
            "joined": False,
            "editor_config": editor_config,
        }


def _persisted_session(session_id, record) -> dict[str, Any]:
    if not isinstance(session_id, str) or not session_id:
        raise StateCorruptError("office session identity is invalid")
    if not isinstance(record, dict):
        raise StateCorruptError("office session record is invalid")
    session_id_field = record.get("session_id")
    file_id = record.get("file_id")
    document_key = record.get("document_key")
    state = record.get("state")
    if session_id_field != session_id:
        raise StateCorruptError("office session identity is invalid")
    if not isinstance(file_id, str) or not file_id:
        raise StateCorruptError("office session file_id is invalid")
    if not isinstance(document_key, str) or not document_key:
        raise StateCorruptError("office session document_key is invalid")
    if type(state) is not str or state not in KNOWN_STATES:
        raise StateCorruptError("office session state is invalid")
    return record


def _refuse_open_session(state, file_id) -> None:
    sessions = state["sessions"]
    if not isinstance(sessions, dict):
        raise StateCorruptError("office sessions collection is invalid")
    for session_id, record in sessions.items():
        persisted = _persisted_session(session_id, record)
        if persisted["file_id"] == file_id and persisted["state"] in OPEN_STATES:
            raise SessionAlreadyOpenError()


def _require_unique_session_identity(state, session_id, document_key) -> None:
    sessions = state["sessions"]
    if not isinstance(sessions, dict):
        raise StateCorruptError("office sessions collection is invalid")
    if session_id in sessions:
        raise CreationFailedError()
    for persisted_id, record in sessions.items():
        persisted = _persisted_session(persisted_id, record)
        if persisted["document_key"] == document_key:
            raise CreationFailedError()


def _document_type(relative_path: str) -> str | None:
    name = relative_path.rsplit("/", 1)[-1]
    if "." not in name:
        return None
    suffix = name.rsplit(".", 1)[-1].lower()
    if suffix in DOCUMENT_TYPES:
        return suffix
    return None


def _signed_editor_config(chat_id, file_id, relative_path, document_type, document_key,
                          session_id, version) -> dict[str, Any]:
    try:
        ticket = tokens.sign_source_ticket(chat_id, file_id, version, session_id)
        self_url = os.environ[config.OCU_OFFICE_SELF_URL].strip().rstrip("/")
        payload = {
            "document": {
                "fileType": document_type,
                "key": document_key,
                "title": relative_path.rsplit("/", 1)[-1],
                "url": f"{self_url}/office/source/{quote(ticket, safe='')}",
            },
            "documentType": DOCUMENT_TYPES[document_type],
            "editorConfig": {
                "callbackUrl": f"{self_url}/office/callback/{quote(chat_id, safe='')}/{quote(session_id, safe='')}",
                "mode": "edit",
            },
        }
        token = tokens.sign_jwt(payload)
    except (KeyError, ValueError, RuntimeError, OSError):
        raise CreationFailedError() from None
    return {**payload, "token": token}


def _error(status: int, reason: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"reason": reason})
