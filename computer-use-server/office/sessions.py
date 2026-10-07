# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Create, join, reopen, save, close, and project sessions under the canonical chat lock."""
from __future__ import annotations

import asyncio
import errno
import json
import math
import os
import uuid
import time
from typing import Any
from urllib.parse import quote

import docker_manager
from fastapi import Request
from fastapi.responses import JSONResponse
from outputs_broker import CorruptIndexError, FileIdNotFoundError, OutputsBroker, OutputsBrokerError

from . import commands, config, epoch, notice, ooxml, tokens, versions
from .epoch import RestoreEpochError
from .store import OfficeStore, StateCorruptError, StateDurabilityError
from .versions import StorageLowError
from .workspace import FileTooLargeError, UnsafePathError

OPEN_STATES = frozenset({"opening", "editing", "saving", "closing", "conflict"})
FINAL_STATES = frozenset({"closed", "error", "orphaned"})
KNOWN_STATES = OPEN_STATES | FINAL_STATES
DOCUMENT_TYPES = {"docx": "word", "xlsx": "cell", "pptx": "slide"}
_SAVE_INTENTS = frozenset({"publish", "persist"})
_PENDING_FIELDS = ("pending_save_seq", "pending_close_seq")


class UnpublishedVersionError(RuntimeError):
    reason = "unpublished_version"


class DocumentServerUnavailableError(RuntimeError):
    reason = "documentserver_unavailable"


class CommandCompletionPendingError(RuntimeError):
    reason = "publish_pending"


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


class SessionNotEditingError(RuntimeError):
    reason = "session_not_editing"


class SessionNotOpenError(RuntimeError):
    reason = "session_not_open"


class InvalidRequestError(ValueError):
    reason = "invalid_request"


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



async def save_session(request: Request) -> JSONResponse:
    try:
        intent = _save_intent(await request.body())
        saved = await asyncio.to_thread(
            _save_session,
            request.scope["ocu_chat_id"],
            request.path_params["session_id"],
            intent,
        )
    except InvalidRequestError:
        return _error(422, "invalid_request")
    except UnknownSessionError:
        return _error(404, "unknown_session")
    except SessionNotEditingError:
        return _error(409, "session_not_editing")
    except DocumentServerUnavailableError:
        return _error(502, "documentserver_unavailable")
    except CommandCompletionPendingError:
        return _error(503, "publish_pending")
    except StorageLowError:
        return _error(503, "storage_low")
    except (RestoreEpochError, StateCorruptError):
        return _error(500, "state_corrupt")
    except StateDurabilityError:
        return _error(500, "state_durability")
    except OSError as extra:
        if extra.errno == errno.ENOSPC:
            return _error(503, "storage_low")
        return _error(500, "state_corrupt")
    return JSONResponse(status_code=202, content=saved)


def close_session(request: Request) -> JSONResponse:
    try:
        closed = _close_session(
            request.scope["ocu_chat_id"], request.path_params["session_id"]
        )
    except UnknownSessionError:
        return _error(404, "unknown_session")
    except SessionNotOpenError:
        return _error(409, "session_not_open")
    except (RestoreEpochError, StateCorruptError):
        return _error(500, "state_corrupt")
    except StateDurabilityError:
        return _error(500, "state_durability")
    except OSError as extra:
        if extra.errno == errno.ENOSPC:
            return _error(503, "storage_low")
        return _error(500, "state_corrupt")
    return JSONResponse(status_code=202, content=closed)


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
            ended = active["state"] == "conflict" and _final_receipt(state, active["session_id"]) is not None
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
                active = _require_session(state, active["session_id"])
                orphaned_here = active["state"] == "orphaned"
            if active["state"] in OPEN_STATES:
                latest = _active_versions(state, file_id)[-1]
                ended = active["state"] == "conflict" and _final_receipt(state, active["session_id"]) is not None
                content, _digest = store.read_workspace_file(
                    chat, relative_path, max_bytes=broker.max_file_size
                )
                ooxml.validate_ooxml(content, document_type)
                editor_config = None if ended else _signed_editor_config(
                    chat, file_id, relative_path, document_type, active["document_key"],
                    active["session_id"], latest["number"],
                )
                if not ended:
                    _refresh_activity(store, chat, active["session_id"])
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
                "last_activity_at": time.time(),
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
    _allocation_metadata(record)
    for field in ("last_activity_at", "saving_started_at"):
        if field in record:
            value = record[field]
            if type(value) not in (int, float) or value < 0 or (
                type(value) is float and not math.isfinite(value)
            ):
                raise StateCorruptError("office session timestamp is invalid")
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


def _successful_answer(receipt: dict[str, Any]) -> dict[str, Any]:
    answer = receipt.get("answer")
    if not isinstance(answer, dict) or set(answer) != {"error"}:
        raise StateCorruptError("office callback receipt answer is invalid")
    if type(answer.get("error")) is not int or answer.get("error") != 0:
        raise StateCorruptError("office callback receipt answer is invalid")
    return {"error": 0}


def _final_receipt(state, session_id) -> tuple[int, dict[str, Any]] | None:
    slot = versions._session_receipts(state, session_id, create=False)
    found = None
    for sequence, record in (slot or {}).items():
        if not isinstance(sequence, str) or not sequence.isascii() or not sequence.isdigit():
            raise StateCorruptError("office receipt sequence is invalid")
        if sequence.startswith("0"):
            raise StateCorruptError("office receipt sequence is invalid")
        receipt = versions._persisted_receipt(record)
        if receipt["status"] in (2, 3, 4):
            if found is not None:
                raise StateCorruptError("office session has more than one final receipt")
            try:
                key = int(sequence)
            except ValueError as extra:
                raise StateCorruptError("office receipt sequence is invalid") from extra
            if type(key) is not int or key < 1:
                raise StateCorruptError("office receipt sequence is invalid")
            found = (key, receipt)
    if found is not None:
        _successful_answer(found[1])
    return found


def _recover_session_publications(store, chat, session_id):
    state = store.read(chat)
    if any(entry.get("session_id") == session_id for entry in state["journal"].values()):
        from .publish import recover_publications
        recover_publications(chat)
        state = store.read(chat)
    return state


def _settle_session_resolve(store, chat, session_id):
    """Settle accepted identity/lineage before a new mutation binds responsibility."""
    from . import publish

    state = store.read(chat)
    if any(
        entry.get("session_id") == session_id and entry.get("requester") == "resolve"
        for entry in state["journal"].values()
    ):
        state = _recover_session_publications(store, chat, session_id)
        if any(entry.get("session_id") == session_id for entry in state["journal"].values()):
            raise publish.RecoveryRequiredError("session resolve remains pending")
    return state


def _orphan_session(store, chat, session_id, reason):
    before = store.read(chat)
    recovering_final = any(
        entry.get("session_id") == session_id and entry.get("requester") == "final"
        for entry in before["journal"].values()
    )
    protect_final_conflict = reason != "restore_epoch_changed" or recovering_final
    state = _recover_session_publications(store, chat, session_id)
    record = _require_session(state, session_id)
    ended = record["state"] == "conflict" and _final_receipt(state, session_id) is not None
    if record["state"] not in OPEN_STATES or ended and protect_final_conflict:
        return state
    def mutate(working):
        record = _persisted_session(session_id, working["sessions"].get(session_id))
        ended = record["state"] == "conflict" and _final_receipt(working, session_id) is not None
        if record["state"] in OPEN_STATES and not (ended and protect_final_conflict):
            record["state"] = "orphaned"
            record["reason"] = reason
    return store.update(chat, mutate)


def session_status(request: Request) -> JSONResponse:
    try:
        status = _session_status(request.scope["ocu_chat_id"], request.path_params["session_id"])
    except UnknownSessionError:
        return _error(404, "unknown_session")
    except (RestoreEpochError, StateCorruptError, OutputsBrokerError):
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
        elif record["state"] in OPEN_STATES:
            ended = record["state"] == "conflict" and _final_receipt(state, session_id) is not None
            status = notice.refresh_workspace_notice(
                store, chat, session_id, record, activity_at=None if ended else time.time(),
            )
        return status


def _status_projection(record):
    _allocation_metadata(record)
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
                "customization": {"forcesave": False},
            },
        }
        token = tokens.sign_jwt(payload)
    except (KeyError, ValueError, RuntimeError, OSError):
        raise CreationFailedError() from None
    return {**payload, "token": token}


def _save_intent(body: bytes) -> str:
    if not body:
        raise InvalidRequestError()
    try:
        parsed = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise InvalidRequestError() from None
    if not isinstance(parsed, dict):
        raise InvalidRequestError()
    intent = parsed.get("intent")
    if type(intent) is not str or intent not in _SAVE_INTENTS:
        raise InvalidRequestError()
    return intent


def _require_session(state, session_id) -> dict[str, Any]:
    if session_id not in state["sessions"]:
        raise UnknownSessionError()
    return _persisted_session(session_id, state["sessions"][session_id])


def _allocation_metadata(record) -> None:
    if "save_intents" in record and record["save_intents"] is None:
        raise StateCorruptError("office session save intents are invalid")
    intents = record.get("save_intents")
    if intents is not None:
        if not isinstance(intents, dict):
            raise StateCorruptError("office session save intents are invalid")
        for key, intent in intents.items():
            if type(key) is not str or not key.isascii() or not key.isdigit() or key.startswith("0"):
                raise StateCorruptError("office session save intent sequence is invalid")
            if type(intent) is not str or intent not in _SAVE_INTENTS:
                raise StateCorruptError("office session save intent is invalid")
            sequence = int(key)
            if sequence < 1 or sequence > record["save_seq"]:
                raise StateCorruptError("office session save intent sequence is invalid")
    for field in _PENDING_FIELDS:
        if field not in record:
            continue
        value = record[field]
        if value is None:
            continue
        if type(value) is not int or value < 1 or value > record["save_seq"]:
            raise StateCorruptError("office session pending allocation is invalid")
        if field == "pending_save_seq":
            mapping = record.get("save_intents")
            if not isinstance(mapping, dict) or mapping.get(str(value)) not in _SAVE_INTENTS:
                raise StateCorruptError("office session pending save allocation is invalid")


def _refresh_activity(store, chat, session_id) -> None:
    def mutate(working):
        current = _require_session(working, session_id)
        if current["state"] in OPEN_STATES and not (
            current["state"] == "conflict" and _final_receipt(working, session_id) is not None
        ):
            current["last_activity_at"] = time.time()
    store.update(chat, mutate)


def _advance_committed(record, save_seq, selected):
    committed = record.get("last_committed_seq", 0)
    if type(committed) is not int or committed < 0:
        raise StateCorruptError("office session sequence is invalid")
    if save_seq > committed:
        record["last_committed_seq"] = save_seq
    if selected is None:
        return
    published = record.get("last_published_seq", 0)
    if type(published) is not int or published < 0:
        raise StateCorruptError("office session sequence is invalid")
    if selected.get("published") is True and save_seq > published:
        record["last_published_seq"] = save_seq


def _allocate_sequence(record) -> int:
    current = record["save_seq"]
    if type(current) is not int or current < 0:
        raise StateCorruptError("office session save sequence is invalid")
    allocated = current + 1
    record["save_seq"] = allocated
    return allocated


def _record_save_intent(record, save_seq, intent) -> None:
    mapping = record.get("save_intents")
    if mapping is None:
        if "save_intents" in record:
            raise StateCorruptError("office session save intents are invalid")
        mapping = {}
        record["save_intents"] = mapping
    mapping[str(save_seq)] = intent


def _load_session(chat_id, session_id):
    chat = docker_manager.canonical_lock_chat_id(chat_id)
    if not isinstance(session_id, str) or not session_id or "\x00" in session_id:
        raise UnknownSessionError()
    store = OfficeStore()
    store._assert_chat_root_safe(chat, allow_missing=False)
    return chat, store


def _maybe_orphan_epoch(store, chat, record, session_id):
    current_epoch = epoch.current_epoch()
    if record["state"] in OPEN_STATES and record["restore_epoch"] != current_epoch:
        return _orphan_session(store, chat, session_id, "restore_epoch_changed")
    return None


def _save_session(chat_id, session_id, intent) -> dict[str, Any]:
    from .publish import RecoveryRequiredError, SandboxStateError

    chat, store = _load_session(chat_id, session_id)
    prepared = None
    with docker_manager._combined_lock(chat):
        state = store.read(chat)
        record = _require_session(state, session_id)
        _status_projection(record)
        orphaned = _maybe_orphan_epoch(store, chat, record, session_id)
        if orphaned is not None:
            raise SessionNotEditingError()
        state = _recover_session_publications(store, chat, session_id)
        record = _require_session(state, session_id)
        _status_projection(record)
        if record["state"] != "editing":
            raise SessionNotEditingError()
        document_key = record["document_key"]

        def mutate(working):
            current = _require_session(working, session_id)
            _status_projection(current)
            if current["state"] != "editing":
                raise SessionNotEditingError()
            allocated = _allocate_sequence(current)
            _record_save_intent(current, allocated, intent)
            current["pending_save_seq"] = allocated
            current["state"] = "saving"
            current["last_activity_at"] = current["saving_started_at"] = time.time()
            prepared["save_seq"] = allocated
            prepared["document_key"] = current["document_key"]

        prepared = {}
        store.update(chat, mutate)
    outcome = asyncio.run(commands.forcesave(prepared["document_key"], prepared["save_seq"], intent))
    try:
        return _reconcile_save(store, chat, session_id, prepared["save_seq"], intent, document_key, outcome)
    except (RecoveryRequiredError, SandboxStateError) as extra:
        raise CommandCompletionPendingError("command completion publication remains pending") from extra


def _reconcile_save(store, chat, session_id, save_seq, intent, document_key, outcome) -> dict[str, Any]:
    from . import publish

    accepted = {"session_id": session_id, "save_seq": save_seq, "intent": intent}
    if outcome is commands.ForceSaveOutcome.ACCEPTED:
        return accepted
    changed = {"value": False}
    journal_id = None
    obligation = {"selected": None}

    def eligible(working):
        current = _require_session(working, session_id)
        _status_projection(current)
        return (
            current["document_key"] == document_key
            and current.get("pending_save_seq") == save_seq
            and current.get("save_intents", {}).get(str(save_seq)) == intent
            and current["state"] not in FINAL_STATES
            and _final_receipt(working, session_id) is None
        )

    def mutate(working):
        obligation["selected"] = None
        if not eligible(working):
            return
        current = _require_session(working, session_id)
        if current["restore_epoch"] != epoch.current_epoch():
            return
        if outcome is not commands.ForceSaveOutcome.NOTHING_TO_SAVE:
            current["pending_save_seq"] = None
        changed["value"] = True
        if outcome is commands.ForceSaveOutcome.KEY_UNKNOWN:
            current["state"] = "orphaned"
            current["reason"] = "editor_state_lost"
            return
        if outcome is commands.ForceSaveOutcome.NOTHING_TO_SAVE:
            selected = _active_versions(working, current["file_id"])[-1]
            _advance_committed(current, save_seq, selected)
            if intent == "publish" and not selected["published"]:
                obligation["selected"] = selected
                if journal_id is not None:
                    publish.add_obligation(working, journal_id, current, selected, save_seq, "save")
            else:
                current["pending_save_seq"] = None
                if current["state"] == "saving":
                    current["state"] = "editing"
            return
        if current["state"] == "saving":
            current["state"] = "editing"

    with docker_manager._combined_lock(chat):
        preview = store.read(chat)
        if eligible(preview):
            record = _require_session(preview, session_id)
            if _maybe_orphan_epoch(store, chat, record, session_id) is not None:
                preview = store.read(chat)
            elif outcome is commands.ForceSaveOutcome.NOTHING_TO_SAVE:
                preview = _settle_session_resolve(store, chat, session_id)
                record = _require_session(preview, session_id)
                if _maybe_orphan_epoch(store, chat, record, session_id) is not None:
                    preview = store.read(chat)
        mutate(preview)
        if changed["value"] and outcome is commands.ForceSaveOutcome.KEY_UNKNOWN:
            allocation = preview["sessions"][session_id]["save_seq"]
            survived = any(
                entry.get("session_id") == session_id
                and entry.get("save_seq") == save_seq and entry.get("requester") == "save"
                and entry.get("file_id") == preview["sessions"][session_id]["file_id"]
                for entry in preview["journal"].values()
            )
            recovered = _recover_session_publications(store, chat, session_id)
            live = _require_session(recovered, session_id)
            orphaned = _maybe_orphan_epoch(store, chat, live, session_id)
            if orphaned is not None:
                recovered = orphaned
                live = _require_session(recovered, session_id)
            changed["value"] = False
            if (
                survived and live["document_key"] == document_key
                and live["save_seq"] == allocation and live.get("pending_save_seq") is None
            ):
                _orphan_session(store, chat, session_id, "editor_state_lost")
            else:
                mutate(recovered)
        if changed["value"]:
            if obligation["selected"] is not None:
                journal_id = uuid.uuid4().hex
            store.update(chat, mutate)
        if journal_id is not None and obligation["selected"] is not None:
            publish.drive_obligation(chat, journal_id)
    if outcome is commands.ForceSaveOutcome.NOTHING_TO_SAVE:
        return accepted
    if outcome is commands.ForceSaveOutcome.KEY_UNKNOWN:
        raise SessionNotEditingError()
    raise DocumentServerUnavailableError()


def _close_session(chat_id, session_id) -> dict[str, Any]:
    chat, store = _load_session(chat_id, session_id)
    with docker_manager._combined_lock(chat):
        state = store.read(chat)
        record = _require_session(state, session_id)
        _status_projection(record)
        orphaned = _maybe_orphan_epoch(store, chat, record, session_id)
        if orphaned is not None:
            raise SessionNotOpenError()
        state = _recover_session_publications(store, chat, session_id)
        record = _require_session(state, session_id)
        _status_projection(record)
        if record["state"] in FINAL_STATES:
            raise SessionNotOpenError()
        if record["state"] == "opening":
            return _commit_opening_close(store, chat, session_id)
        if record["state"] == "closing":
            pending = record.get("pending_close_seq")
            if type(pending) is int:
                if pending < 1:
                    raise StateCorruptError("office session pending allocation is invalid")
                _refresh_activity(store, chat, session_id)
                return {"session_id": session_id, "save_seq": pending, "state": "closing"}
            if pending is not None:
                raise StateCorruptError("office session pending allocation is invalid")
            found = _final_receipt(state, session_id)
            if found is None:
                raise StateCorruptError("office session pending allocation is invalid")
            sequence, _receipt = found
            return {"session_id": session_id, "save_seq": sequence, "state": "closing"}
        ended_conflict = record["state"] == "conflict" and _final_receipt(state, session_id) is not None
        if ended_conflict:
            return {
                "session_id": session_id,
                "save_seq": record["save_seq"],
                "state": "conflict",
            }
        if record["state"] in ("editing", "saving"):
            outcome = asyncio.run(commands.lookup_key(record["document_key"]))
            if outcome is commands.KeyLookupOutcome.KEY_UNKNOWN:
                _orphan_session(store, chat, session_id, "editor_state_lost")
                raise SessionNotOpenError()
        return _commit_close_allocation(store, chat, session_id, record["state"])


def _commit_opening_close(store, chat, session_id) -> dict[str, Any]:
    allocated = {"save_seq": None}

    def mutate(working):
        current = _require_session(working, session_id)
        _status_projection(current)
        if current["state"] != "opening":
            raise StateCorruptError("office session close admission raced")
        allocated["save_seq"] = _allocate_sequence(current)
        current["state"] = "closed"
        current["last_activity_at"] = time.time()

    store.update(chat, mutate)
    return {"session_id": session_id, "save_seq": allocated["save_seq"], "state": "closed"}


def _commit_close_allocation(store, chat, session_id, expected_state) -> dict[str, Any]:
    allocated = {"save_seq": None, "state": expected_state}

    def mutate(working):
        current = _require_session(working, session_id)
        _status_projection(current)
        current["last_activity_at"] = time.time()
        pending = current.get("pending_close_seq")
        if type(pending) is int:
            allocated["save_seq"] = pending
            allocated["state"] = current["state"]
            return
        if current["state"] != expected_state:
            raise StateCorruptError("office session close admission raced")
        allocated["save_seq"] = _allocate_sequence(current)
        current["pending_close_seq"] = allocated["save_seq"]
        if current["state"] in ("editing", "saving"):
            current["state"] = "closing"
            allocated["state"] = "closing"
        else:
            allocated["state"] = current["state"]

    store.update(chat, mutate)
    return {
        "session_id": session_id,
        "save_seq": allocated["save_seq"],
        "state": allocated["state"],
    }


def _error(status: int, reason: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"reason": reason})
