# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Office session recovery on the existing idle poll's synchronous worker."""
from __future__ import annotations

import asyncio
import os
import stat
import time

import docker_manager

from . import commands, config
from .sessions import _persisted_session, _status_projection
from .store import OfficeStore, StateCorruptError, StateDurabilityError

_ELIGIBLE = frozenset({"opening", "editing", "saving", "closing"})


def sweep_office_sessions(now: float | None = None) -> None:
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
                    _sweep_chat(store, chat, current)
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


def _sweep_chat(store: OfficeStore, chat: str, current: float) -> None:
    store._assert_chat_root_safe(chat, allow_missing=False)
    with docker_manager._combined_lock(chat):
        if not _has_state(store, chat):
            return
        state = store.read(chat)
        for session_id, stored in state["sessions"].items():
            record = _persisted_session(session_id, stored)
            _status_projection(record)
            if record["state"] not in _ELIGIBLE:
                continue
            overdue_save = record["state"] == "saving" and _expired(
                record, "saving_started_at", current, config.SAVE_CALLBACK_TIMEOUT_SECONDS,
            )
            if not overdue_save and not _expired(
                record, "last_activity_at", current, config.SESSION_LIVENESS_INTERVAL_SECONDS,
            ):
                continue
            outcome = asyncio.run(commands.lookup_key(record["document_key"]))
            if outcome is commands.KeyLookupOutcome.KEY_UNKNOWN:
                next_state, reason = "orphaned", "editor_state_lost"
            elif outcome is commands.KeyLookupOutcome.KNOWN and overdue_save:
                next_state, reason = "editing", "save_timeout"
            else:
                continue

            def mutate(working):
                live = _persisted_session(session_id, working["sessions"].get(session_id))
                live["state"] = next_state
                live["reason"] = reason

            store.update(chat, mutate)
