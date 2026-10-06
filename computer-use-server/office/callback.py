# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Persist an authenticated DocumentServer callback under the chat lock."""
from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
import os
import uuid
from typing import Any

from fastapi.responses import JSONResponse

from . import config, download, epoch, ooxml, publish, sessions, store as store_module, versions
from .download import DownloadFailed, DownloadRejected, FileTooLarge
from .epoch import RestoreEpochError
from .store import OfficeStore, StateCorruptError, StateDurabilityError
from .versions import StorageLowError

_LOG = logging.getLogger("ocu.office")
_SUCCESS = {"error": 0}
_FINAL = frozenset({2, 3, 4})
_FORCESAVE = frozenset({6, 7})
_KNOWN = frozenset({1, 2, 3, 4, 6, 7})
_INTENTS = frozenset({"publish", "persist"})
_OPEN = sessions.OPEN_STATES
_ENDED = sessions.FINAL_STATES


class CallbackRefusal(Exception):
    def __init__(self, status: int, reason: str, *, reported=None) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.reported = reported


def process_authenticated_callback(
    store: OfficeStore,
    chat: str,
    session_id: str,
    payload: dict[str, Any],
    loop: asyncio.AbstractEventLoop,
) -> JSONResponse:
    try:
        return _process(store, chat, session_id, payload, loop)
    except CallbackRefusal as extra:
        _reject(chat, session_id, extra.reason, extra.reported)
        return JSONResponse(status_code=extra.status, content={"reason": extra.reason})
    except RestoreEpochError:
        _reject(chat, session_id, "state_corrupt")
        return JSONResponse(status_code=500, content={"reason": "state_corrupt"})
    except StateDurabilityError:
        return JSONResponse(status_code=500, content={"reason": "state_durability"})
    except StateCorruptError:
        _reject(chat, session_id, "state_corrupt")
        return JSONResponse(status_code=500, content={"reason": "state_corrupt"})
    except StorageLowError:
        return JSONResponse(status_code=503, content={"reason": "storage_low"})
    except FileTooLarge:
        return JSONResponse(status_code=413, content={"reason": "file_too_large"})
    except DownloadRejected:
        return JSONResponse(status_code=422, content={"reason": "download_url_rejected"})
    except DownloadFailed:
        return JSONResponse(status_code=502, content={"reason": "download_failed"})
    except ooxml.CorruptDocumentError:
        return JSONResponse(status_code=422, content={"reason": "invalid_content"})
    except OSError as extra:
        if extra.errno == errno.ENOSPC:
            return JSONResponse(status_code=503, content={"reason": "storage_low"})
        return JSONResponse(status_code=500, content={"reason": "state_corrupt"})


def _process(
    store: OfficeStore,
    chat: str,
    session_id: str,
    payload: dict[str, Any],
    loop: asyncio.AbstractEventLoop,
) -> JSONResponse:
    state = store._snapshot(store._load(chat, create=False))
    record = sessions._persisted_session(session_id, state["sessions"][session_id])
    if record["state"] in _OPEN and record["restore_epoch"] != epoch.current_epoch():
        sessions._orphan_session(store, chat, session_id, "restore_epoch_changed")
        raise CallbackRefusal(409, "session_not_open")
    status = payload.get("status")
    if type(status) is not int:
        if record["state"] in _ENDED:
            raise CallbackRefusal(409, "session_not_open")
        raise CallbackRefusal(422, "unknown_status", reported=status)
    if status in _FINAL:
        found = sessions._final_receipt(state, session_id)
        if found is not None:
            sequence, final = found
            answer = sessions._successful_answer(final)
            _replay_barrier(store, chat)
            _drive_receipt_publication(store, chat, session_id, sequence, final)
            return JSONResponse(status_code=200, content=answer)
    userdata = _forcesave_userdata(payload) if status in _FORCESAVE else None
    if status in _FORCESAVE:
        response = _forcesave_order(store, chat, session_id, record, status, userdata, payload, loop)
        if response is not None:
            return response
    if record["state"] in _ENDED:
        raise CallbackRefusal(409, "session_not_open")
    if status not in _KNOWN:
        raise CallbackRefusal(422, "unknown_status", reported=status)
    if status == 1:
        _apply_status_1(store, chat, session_id, payload)
        return JSONResponse(status_code=200, content=_SUCCESS)
    if status == 3:
        _apply_no_content(store, chat, session_id, 3, _end_error)
        return JSONResponse(status_code=200, content=_SUCCESS)
    if status == 7:
        assert userdata is not None
        _apply_no_content(store, chat, session_id, 7, _apply_status_7, save_seq=userdata[0])
        return JSONResponse(status_code=200, content=_SUCCESS)
    if status == 4:
        _apply_no_content(store, chat, session_id, 4, _apply_status_4)
        return JSONResponse(status_code=200, content=_SUCCESS)
    _persist_content(store, chat, session_id, record, status, payload, userdata, loop)
    return JSONResponse(status_code=200, content=_SUCCESS)


def _forcesave_order(
    store: OfficeStore,
    chat: str,
    session_id: str,
    record: dict[str, Any],
    status: int,
    userdata: tuple[int, str] | None,
    payload: dict[str, Any],
    loop: asyncio.AbstractEventLoop,
) -> JSONResponse | None:
    if userdata is None:
        if record["state"] in _ENDED:
            raise CallbackRefusal(409, "session_not_open")
        raise CallbackRefusal(422, "invalid_userdata")
    save_seq, presented_intent = userdata
    existing = _receipt_at(store._snapshot(store._load(chat, create=False)), session_id, save_seq)
    if existing is not None:
        response = _replay_forcesave(store, chat, status, existing, payload, loop)
        _drive_receipt_publication(store, chat, session_id, save_seq, existing)
        return response
    issued = _issued_intent(record, save_seq)
    if issued is None or save_seq > record["save_seq"] or presented_intent != issued:
        if record["state"] in _ENDED:
            raise CallbackRefusal(409, "session_not_open")
        raise CallbackRefusal(422, "invalid_userdata")
    committed = record.get("last_committed_seq", 0)
    if type(committed) is not int or committed < 0:
        raise StateCorruptError("office session sequence is invalid")
    if save_seq < committed:
        if record["state"] in _ENDED:
            raise CallbackRefusal(409, "session_not_open")
        raise CallbackRefusal(409, "stale_save_seq")
    return None


def _persist_content(
    store: OfficeStore,
    chat: str,
    session_id: str,
    record: dict[str, Any],
    status: int,
    payload: dict[str, Any],
    userdata: tuple[int, str] | None,
    loop: asyncio.AbstractEventLoop,
) -> None:
    save_seq = userdata[0] if userdata is not None else None
    intent = userdata[1] if userdata is not None else None
    try:
        body = download.fetch_callback_content(payload.get("url"), loop)
        _validate_content(store, chat, session_id, body)
        if status == 6:
            assert save_seq is not None and intent is not None
            source = "save" if intent == "publish" else "autosave"
            journal_id = _store_content(store, chat, session_id, status, save_seq, source, body)
        else:
            journal_id = _store_content(store, chat, session_id, status, None, "close", body)
    except StateDurabilityError:
        raise
    except (
        DownloadRejected,
        DownloadFailed,
        FileTooLarge,
        ooxml.CorruptDocumentError,
        StorageLowError,
    ) as extra:
        if isinstance(extra, StorageLowError):
            reason = "storage_low"
        elif isinstance(extra, ooxml.CorruptDocumentError):
            reason = "invalid_content"
        else:
            reason = extra.reason
        if status == 6 and save_seq is not None:
            _recover_outstanding(store, chat, session_id, save_seq, reason)
        raise
    if journal_id is not None:
        _publish_callback_obligation(chat, journal_id)


def _replay_forcesave(
    store: OfficeStore,
    chat: str,
    status: int,
    existing: dict[str, Any],
    payload: dict[str, Any],
    loop: asyncio.AbstractEventLoop,
) -> JSONResponse:
    if existing["status"] != status:
        raise CallbackRefusal(409, "stale_save_seq")
    answer = sessions._successful_answer(existing)
    _replay_barrier(store, chat)
    if status == 7 or existing["sha256"] is None:
        return JSONResponse(status_code=200, content=answer)
    body = download.fetch_callback_content(payload.get("url"), loop)
    digest = hashlib.sha256(body).hexdigest()
    if existing["sha256"] != digest:
        raise CallbackRefusal(409, "stale_save_seq")
    return JSONResponse(status_code=200, content=answer)


def _publish_callback_obligation(chat, journal_id):
    try:
        publish.publish(chat, journal_id)
    except (publish.SandboxStateError, publish.RecoveryRequiredError) as extra:
        _LOG.warning(
            "Office callback publication remains pending",
            extra={"chat_id": chat, "publication_refusal": type(extra).__name__},
        )


def _drive_receipt_publication(store, chat, session_id, save_seq, receipt):
    if receipt["status"] not in (2, 6) or receipt["version"] is None:
        return
    state = store.read(chat)
    record = sessions._persisted_session(session_id, state["sessions"][session_id])
    requester = "final" if receipt["status"] == 2 else "save"
    matching = [
        journal_id for journal_id, entry in state["journal"].items()
        if entry.get("session_id") == session_id
        and entry.get("save_seq") == save_seq
        and entry.get("file_id") == record["file_id"]
        and entry.get("version") == receipt["version"]
        and entry.get("requester") == requester
    ]
    if len(matching) > 1:
        raise StateCorruptError("callback receipt owns more than one publication")
    if matching:
        _publish_callback_obligation(chat, matching[0])


def _validate_content(store: OfficeStore, chat: str, session_id: str, body: bytes) -> None:
    state = store._snapshot(store._load(chat, create=False))
    record = sessions._persisted_session(session_id, state["sessions"][session_id])
    document = state["documents"].get(record["file_id"])
    if not isinstance(document, dict) or type(document.get("type")) is not str:
        raise StateCorruptError("office document type is invalid")
    ooxml.validate_ooxml(body, document["type"])
    store.check_free_space(chat, config.MIN_FREE_BYTES)


def _store_content(
    store: OfficeStore,
    chat: str,
    session_id: str,
    status: int,
    save_seq: int | None,
    source: str,
    body: bytes,
) -> str | None:
    state = store._snapshot(store._load(chat, create=False))
    record = sessions._persisted_session(session_id, state["sessions"][session_id])
    file_id = record["file_id"]
    listed = sessions._active_versions(state, file_id)
    parent = listed[-1]["number"]
    allocated = save_seq if save_seq is not None else _next_final_seq(record)
    journal_id = uuid.uuid4().hex if source in ("save", "close") else None

    def mutate_state(working: dict[str, Any], selected: dict[str, Any]) -> None:
        current = sessions._persisted_session(session_id, working["sessions"][session_id])
        if save_seq is None:
            _commit_final_seq(current, allocated)
        if journal_id is not None:
            working["journal"][journal_id] = {
                "file_id": file_id, "version": selected["number"],
                "session_id": session_id, "save_seq": allocated,
                "requester": "final" if status == 2 else "save",
            }
        else:
            _apply_status_6(working, current, allocated, selected)
        _advance_committed(current, allocated, selected)

    receipt = {
        "session_id": session_id,
        "save_seq": allocated,
        "status": status,
        "sha256": None,
        "version": None,
        "answer": dict(_SUCCESS),
    }
    store.store_version(
        chat,
        file_id,
        body,
        source=source,
        parent=parent,
        published=False,
        min_free_bytes=config.MIN_FREE_BYTES,
        receipt=receipt,
        mutate_state=mutate_state,
    )
    return journal_id


def _apply_no_content(store: OfficeStore, chat: str, session_id: str, status: int, apply, save_seq=None) -> None:
    def mutate(working: dict[str, Any]) -> None:
        current = sessions._persisted_session(session_id, working["sessions"][session_id])
        allocated = save_seq if save_seq is not None else _next_final_seq(current)
        if save_seq is None:
            _commit_final_seq(current, allocated)
        apply(working, current, allocated)
        versions._put_receipt(
            working,
            session_id,
            allocated,
            {
                "status": status,
                "sha256": None,
                "version": None,
                "answer": dict(_SUCCESS),
            },
        )

    store.update(chat, mutate)


def _apply_status_1(store: OfficeStore, chat: str, session_id: str, payload: dict[str, Any]) -> None:
    users = payload.get("users")

    def mutate(working: dict[str, Any]) -> None:
        current = sessions._persisted_session(session_id, working["sessions"][session_id])
        if isinstance(users, list):
            current["participants"] = list(users)
        if current["state"] == "opening":
            current["state"] = "editing"
        elif current["state"] == "closing" and isinstance(users, list) and len(users) > 0:
            current["state"] = "editing"
            current["pending_close_seq"] = None

    store.update(chat, mutate)


def _end_error(_working: dict[str, Any], record: dict[str, Any], _save_seq: int) -> None:
    record["state"] = "error"
    record["reason"] = "final_save_failed"
    record["pending_close_seq"] = None


def _apply_status_7(_working: dict[str, Any], record: dict[str, Any], save_seq: int) -> None:
    if record.get("pending_save_seq") == save_seq:
        record["pending_save_seq"] = None
        if record["state"] == "saving":
            record["state"] = "editing"
            record["reason"] = "forcesave_failed"


def _apply_status_6(_working, record: dict[str, Any], save_seq: int, _selected) -> None:
    if record.get("pending_save_seq") == save_seq:
        record["pending_save_seq"] = None
        if record["state"] == "saving":
            record["state"] = "editing"


def _apply_status_4(working: dict[str, Any], record: dict[str, Any], save_seq: int) -> None:
    listed = sessions._active_versions(working, record["file_id"])
    if listed[-1]["published"]:
        record["state"] = "closed"
        record["pending_close_seq"] = None
    _advance_committed(record, save_seq, None)



def _advance_committed(record: dict[str, Any], save_seq: int, selected: dict[str, Any] | None) -> None:
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


def _next_final_seq(record: dict[str, Any]) -> int:
    pending = record.get("pending_close_seq")
    if type(pending) is int and pending >= 1:
        return pending
    return sessions._allocate_sequence(record)


def _commit_final_seq(record: dict[str, Any], allocated: int) -> None:
    if record.get("pending_close_seq") == allocated:
        record["pending_close_seq"] = None
    elif record["save_seq"] < allocated:
        record["save_seq"] = allocated


def _recover_outstanding(store: OfficeStore, chat: str, session_id: str, save_seq: int, reason: str) -> None:
    def mutate(working: dict[str, Any]) -> None:
        current = sessions._persisted_session(session_id, working["sessions"][session_id])
        if current.get("pending_save_seq") != save_seq:
            return
        current["pending_save_seq"] = None
        if current["state"] == "saving":
            current["state"] = "editing"
            current["reason"] = reason

    store.update(chat, mutate)


def _replay_barrier(store: OfficeStore, chat: str) -> None:
    opened = store._open_tree(chat, create=False)
    if opened is None:
        raise CallbackRefusal(500, "state_durability")
    base_fd, root_fd, ocu_fd, office_fd = opened
    try:
        store_module.os.fsync(office_fd)
    except OSError as extra:
        raise CallbackRefusal(500, "state_durability") from extra
    finally:
        os.close(office_fd)
        os.close(ocu_fd)
        os.close(root_fd)
        os.close(base_fd)


def _receipt_at(state: dict[str, Any], session_id: str, save_seq: int) -> dict[str, Any] | None:
    slot = versions._session_receipts(state, session_id, create=False)
    if slot is None:
        return None
    key = str(save_seq)
    if key not in slot:
        return None
    return versions._persisted_receipt(slot[key])


def _forcesave_userdata(payload: dict[str, Any]) -> tuple[int, str] | None:
    raw = payload.get("userdata")
    if type(raw) is not str or not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(parsed, dict):
        return None
    save_seq = parsed.get("save_seq")
    intent = parsed.get("intent")
    if type(save_seq) is not int or save_seq < 1:
        return None
    if type(intent) is not str or intent not in _INTENTS:
        return None
    return save_seq, intent


def _issued_intent(record: dict[str, Any], save_seq: int) -> str | None:
    mapping = record.get("save_intents")
    if not isinstance(mapping, dict):
        return None
    intent = mapping.get(str(save_seq))
    if type(intent) is not str or intent not in _INTENTS:
        return None
    return intent


def _reject(chat_id: str, session_id: str, reason: str, reported=None) -> None:
    fields = [
        "office callback rejected chat=%s session=%s reason=%s",
        chat_id.replace("\r", " ").replace("\n", " "),
        session_id.replace("\r", " ").replace("\n", " "),
        reason.replace("\r", " ").replace("\n", " "),
    ]
    if reason == "unknown_status":
        fields[0] = "office callback rejected chat=%s session=%s reason=%s status=%s"
        fields.append(_safe_status(reported))
    _LOG.error(*fields)


def _safe_status(value) -> str:
    if type(value) is int:
        return str(value)
    if type(value) is str:
        text = value.replace("\r", " ").replace("\n", " ")
        return text[:32]
    return type(value).__name__
