# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Create, join, reopen, and project sessions under the canonical chat lock."""
from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any
from urllib.parse import quote

import docker_manager
from fastapi import Request
from fastapi.responses import JSONResponse
from outputs_broker import CorruptIndexError, FileIdNotFoundError, OutputsBroker, OutputsBrokerError

from . import commands, config, epoch, ooxml, tokens, versions
from .epoch import RestoreEpochError
from .store import OfficeStore, StateCorruptError, StateDurabilityError
from .versions import StorageLowError
from .workspace import FileTooLargeError, UnsafePathError

OPEN_STATES = frozenset({"opening", "editing", "saving", "closing", "conflict"})
FINAL_STATES = frozenset({"closed", "error", "orphaned"})
KNOWN_STATES = OPEN_STATES | FINAL_STATES
DOCUMENT_TYPES = {"docx": "word", "xlsx": "cell", "pptx": "slide"}


class UnpublishedVersionError(RuntimeError):
    reason = "unpublished_version"


class DocumentServerUnavailableError(RuntimeError):
    reason = "documentserver_unavailable"


class UnknownSessionError(RuntimeError):
    reason = "unknown_session"


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
    except UnpublishedVersionError:
        return _error(409, "unpublished_version")
    except DocumentServerUnavailableError:
        return _error(502, "documentserver_unavailable")
    except (RestoreEpochError, StateCorruptError, CorruptIndexError):
        return _error(500, "state_corrupt")
    except StateDurabilityError:
        return _error(500, "state_durability")
    except (CreationFailedError, tokens.MissingSigningKeyError, OutputsBrokerError, OSError):
        return _error(500, "creation_failed")
    return JSONResponse(status_code=200 if created["joined"] else 201, content=created)


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
        active = _open_session(state, file_id)
        restore_epoch = epoch.current_epoch()
        orphaned_here = False
        if active is not None:
            _status_projection(active)
            latest = _active_versions(state, file_id)[-1]
            ended = active["state"] == "conflict" and _has_final_receipt(state, active["session_id"])
            reason = None
            if active["restore_epoch"] != restore_epoch:
                reason = "restore_epoch_changed"
            elif active["state"] != "opening" and not ended:
                # FastAPI dispatches this synchronous handler on a worker thread.
                # Keep the blocking chat lock off the application event loop.
                outcome = asyncio.run(commands.lookup_key(active["document_key"]))
                if outcome is commands.KeyLookupOutcome.UNREACHABLE:
                    raise DocumentServerUnavailableError()
                if outcome is commands.KeyLookupOutcome.KEY_UNKNOWN:
                    reason = "editor_state_lost"
            if reason is not None:
                state = _orphan_session(store, chat, active["session_id"], reason)
                orphaned_here = True
            else:
                content, _digest = store.read_workspace_file(
                    chat, relative_path, max_bytes=broker.max_file_size
                )
                ooxml.validate_ooxml(content, document_type)
                editor_config = None if ended else _signed_editor_config(
                    chat, file_id, relative_path, document_type, active["document_key"],
                    active["session_id"], latest["number"],
                )
                return {
                    "session_id": active["session_id"],
                    "file_id": file_id,
                    "document_key": active["document_key"],
                    "state": active["state"],
                    "joined": True,
                    "editor_config": editor_config,
                }
        if orphaned_here and not _active_versions(state, file_id)[-1]["published"]:
            raise UnpublishedVersionError()
        content, digest = store.read_workspace_file(chat, relative_path, max_bytes=broker.max_file_size)
        ooxml.validate_ooxml(content, document_type)
        store.check_free_space(chat, config.MIN_FREE_BYTES)
        # Reuse the epoch checked before any bounded command lookup.
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
            if _open_session(working, file_id) is not None:
                raise StateCorruptError("office document has an unexpected open session")
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
                "reason": None,
                "last_committed_seq": 0,
                "last_published_seq": 0,
                "workspace_changed": False,
                "saved_as": None,
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
    if "restore_epoch" not in record or (
        record["restore_epoch"] is not None and type(record["restore_epoch"]) is not str
    ):
        raise StateCorruptError("office session restore epoch is invalid")
    if type(record.get("save_seq")) is not int or record["save_seq"] < 0:
        raise StateCorruptError("office session save sequence is invalid")
    return record


def _open_session(state, file_id) -> dict[str, Any] | None:
    sessions = state["sessions"]
    if not isinstance(sessions, dict):
        raise StateCorruptError("office sessions collection is invalid")
    active = None
    for session_id, record in sessions.items():
        persisted = _persisted_session(session_id, record)
        if persisted["file_id"] == file_id and persisted["state"] in OPEN_STATES:
            if active is not None:
                raise StateCorruptError("office document has multiple open sessions")
            active = persisted
    return active


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


def _active_versions(state, file_id):
    document = state["documents"].get(file_id)
    if not isinstance(document, dict) or not document.get("versions"):
        raise StateCorruptError("active office session has no document history")
    return versions._document_versions(state, file_id)


def _has_final_receipt(state, session_id) -> bool:
    slot = versions._session_receipts(state, session_id, create=False)
    ended = False
    for sequence, record in (slot or {}).items():
        if not isinstance(sequence, str) or not sequence.isascii() or not sequence.isdigit():
            raise StateCorruptError("office receipt sequence is invalid")
        if sequence.startswith("0"):
            raise StateCorruptError("office receipt sequence is invalid")
        receipt = versions._persisted_receipt(record)
        ended = ended or receipt["status"] in (2, 3, 4)
    return ended


def _orphan_session(store, chat, session_id, reason):
    def mutate(working):
        record = _persisted_session(session_id, working["sessions"].get(session_id))
        if record["state"] in OPEN_STATES:
            record["state"] = "orphaned"
            record["reason"] = reason
    return store.update(chat, mutate)


def session_status(request: Request) -> JSONResponse:
    try:
        status = _session_status(request.scope["ocu_chat_id"], request.path_params["session_id"])
    except UnknownSessionError:
        return _error(404, "unknown_session")
    except (RestoreEpochError, StateCorruptError):
        return _error(500, "state_corrupt")
    except StateDurabilityError:
        return _error(500, "state_durability")
    except OSError:
        return _error(500, "state_corrupt")
    return JSONResponse(status_code=200, content=status)


def _session_status(chat_id, session_id):
    chat = docker_manager.canonical_lock_chat_id(chat_id)
    if not isinstance(session_id, str) or not session_id or "\x00" in session_id:
        raise UnknownSessionError()
    store = OfficeStore()
    store._assert_chat_root_safe(chat, allow_missing=False)
    with docker_manager._combined_lock(chat):
        state = store.read(chat)
        if session_id not in state["sessions"]:
            raise UnknownSessionError()
        record = _persisted_session(session_id, state["sessions"][session_id])
        status = _status_projection(record)
        current_epoch = epoch.current_epoch()
        if record["state"] in OPEN_STATES and record["restore_epoch"] != current_epoch:
            state = _orphan_session(store, chat, session_id, "restore_epoch_changed")
            status = _status_projection(state["sessions"][session_id])
        return status


def _status_projection(record):
    reason = record.get("reason")
    if reason is not None and (type(reason) is not str or not reason):
        raise StateCorruptError("office session reason is invalid")
    counters = {}
    for field in ("save_seq", "last_committed_seq", "last_published_seq"):
        value = record.get(field, 0)
        if type(value) is not int or value < 0:
            raise StateCorruptError("office session sequence is invalid")
        counters[field] = value
    if not (
        counters["last_published_seq"] <= counters["last_committed_seq"] <= counters["save_seq"]
    ):
        raise StateCorruptError("office session sequence ordering is invalid")
    changed = record.get("workspace_changed", False)
    if type(changed) is not bool:
        raise StateCorruptError("office session change notice is invalid")
    saved_as = record.get("saved_as")
    if saved_as is not None:
        if not isinstance(saved_as, dict) or any(
            type(saved_as.get(field)) is not str or not saved_as[field]
            for field in ("file_id", "path")
        ):
            raise StateCorruptError("office session saved-as value is invalid")
    return {
        "session_id": record["session_id"],
        "file_id": record["file_id"],
        "document_key": record["document_key"],
        "state": record["state"],
        "reason": reason,
        **counters,
        "workspace_changed": changed,
        "saved_as": saved_as,
    }


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
