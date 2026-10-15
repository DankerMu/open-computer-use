# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Office session recovery on the lifespan-owned transaction workers."""
from __future__ import annotations

import asyncio
from functools import partial
import os
import stat
import time

import docker_manager

from . import commands, config, epoch
from .sessions import (
    _final_receipt, _lookup_authority, _orphan_session, _persisted_session,
    _session_identity, _status_projection,
)
from .publish import recover_publications
from .store import OfficeStore, StateCorruptError, StateDurabilityError

_ELIGIBLE = frozenset({"opening", "editing", "saving", "closing"})


def sweep_office_sessions(
    now: float | None = None, *, loop: asyncio.AbstractEventLoop,
) -> None:
    _sweep(partial(_sweep_chat, loop=loop), now)


def sweep_office_publications(now: float | None = None) -> None:
    _sweep(_recover_chat, now)


def _sweep(visit, now):
    if not config.enabled():
        return
    current = time.time() if now is None else now
    store = OfficeStore()
    try:
        with os.scandir(docker_manager.BASE_DATA_DIR) as children:
            for child in children:
                try:
                    chat = docker_manager.canonical_lock_chat_id(child.name)
                except Exception:
                    continue
                if chat != child.name:
                    continue
                try:
                    if not child.is_dir(follow_symlinks=False) or not _has_state(store, chat):
                        continue
                    visit(store, chat, current)
                except StateDurabilityError:
                    print("[OFFICE] session sweep state durability failed")
                except Exception:
                    # Persisted identities and exception text can contain secrets.
                    print("[OFFICE] session sweep chat failed")
    except FileNotFoundError:
        return


def _has_state(store: OfficeStore, chat: str) -> bool:
    opened = store._open_tree(chat, create=False)
    if opened is None:
        return False
    try:
        try:
            lock_info = os.lstat(".lifecycle.lock", dir_fd=opened[1])
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(lock_info.st_mode):
                raise StateCorruptError("office chat lock is not a regular file")
        try:
            info = os.lstat("state.json", dir_fd=opened[-1])
        except FileNotFoundError:
            return False
        return stat.S_ISREG(info.st_mode)
    finally:
        for fd in reversed(opened):
            os.close(fd)


def _expired(record, field, current, timeout) -> bool:
    return field in record and current - record[field] > timeout


def _recover_chat(store, chat, current):
    recover_publications(chat, now=current)


def _sweep_chat(
    store: OfficeStore, chat: str, current: float, *, loop: asyncio.AbstractEventLoop,
) -> None:
    store._assert_chat_root_safe(chat, allow_missing=False)
    with docker_manager._combined_lock(chat, create=False) as lock:
        if lock is None or not _has_state(store, chat):
            return
        before = store.read(chat)
        due = {}
        for session_id, stored in before["sessions"].items():
            record = _persisted_session(session_id, stored)
            _status_projection(record)
            if record["state"] not in _ELIGIBLE:
                continue
            overdue_save = record["state"] == "saving" and _expired(
                record, "saving_started_at", current, config.SAVE_CALLBACK_TIMEOUT_SECONDS,
            )
            if overdue_save or _expired(
                record, "last_activity_at", current, config.SESSION_LIVENESS_INTERVAL_SECONDS,
            ):
                due[session_id] = (
                    _session_identity(record),
                    (record.get("pending_save_seq"), record.get("saving_started_at")) if overdue_save else None,
                )
        recovering_saves = {
            entry.get("session_id") for entry in before["journal"].values()
            if entry.get("requester") == "save"
        }
        recover_publications(chat, now=current)
    for session_id, (identity, overdue_save) in due.items():
        with docker_manager._combined_lock(chat, create=False) as lock:
            if lock is None or not _has_state(store, chat):
                return
            state = store.read(chat)
            stored = state["sessions"].get(session_id)
            if stored is None:
                continue
            record = _persisted_session(session_id, stored)
            _status_projection(record)
            recovered_conflict = (
                session_id in recovering_saves and record["state"] == "conflict"
                and _final_receipt(state, session_id) is None
            )
            if _session_identity(record) != identity:
                continue
            if record["state"] not in _ELIGIBLE and not recovered_conflict:
                continue
            save_due = _same_overdue_save(record, overdue_save, current)
            if not recovered_conflict and not save_due and not _expired(
                record, "last_activity_at", current, config.SESSION_LIVENESS_INTERVAL_SECONDS,
            ):
                continue
            authority = _lookup_authority(state, record, epoch.current_epoch())
            document_key = record["document_key"]
        outcome = commands.run_command(
            commands.lookup_key(document_key), loop,
            unreachable=commands.KeyLookupOutcome.UNREACHABLE,
        )
        with docker_manager._combined_lock(chat, create=False) as lock:
            if lock is None or not _has_state(store, chat):
                return
            recover_publications(chat, now=current)
            state = store.read(chat)
            stored = state["sessions"].get(session_id)
            if stored is None:
                continue
            record = _persisted_session(session_id, stored)
            _status_projection(record)
            if _lookup_authority(state, record, epoch.current_epoch()) != authority:
                continue
            save_due = _same_overdue_save(record, overdue_save, current)
            if not recovered_conflict and not save_due and not _expired(
                record, "last_activity_at", current, config.SESSION_LIVENESS_INTERVAL_SECONDS,
            ):
                continue
            if outcome is commands.KeyLookupOutcome.KEY_UNKNOWN:
                _orphan_session(
                    store, chat, session_id, "editor_state_lost", expected_authority=authority,
                )
            elif outcome is commands.KeyLookupOutcome.KNOWN and save_due:
                def mutate(working):
                    live = _persisted_session(session_id, working["sessions"].get(session_id))
                    live["state"] = "editing"
                    live["reason"] = "save_timeout"
                store.update(chat, mutate)


def _same_overdue_save(record, observed, current):
    return (
        observed is not None and record["state"] == "saving"
        and (record.get("pending_save_seq"), record.get("saving_started_at")) == observed
        and _expired(record, "saving_started_at", current, config.SAVE_CALLBACK_TIMEOUT_SECONDS)
    )
