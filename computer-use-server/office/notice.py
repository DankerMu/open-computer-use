# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Targeted workspace_changed observation for an open Office session status."""
from __future__ import annotations

import os
import stat
from typing import Any

from outputs_broker import FileIdNotFoundError, OutputsBroker

from .store import OfficeStore, StateCorruptError
from .versions import _HASH
from .workspace import FileTooLargeError, UnsafePathError, _hash_regular, _open_file, _parts

_CACHE_FIELDS = ("last_checked_size", "last_checked_mtime_ns")
_UNAVAILABLE = object()


def refresh_workspace_notice(store: OfficeStore, chat: str, session_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """Observe the edited workspace file and persist only notice bookkeeping."""
    from .sessions import _status_projection

    prior = _status_projection(record)
    observation = _observe(chat, record)
    if observation is _UNAVAILABLE:
        changed = True
        size = None
        mtime_ns = None
    else:
        changed, size, mtime_ns = observation
    if (
        changed is prior["workspace_changed"]
        and size == record.get("last_checked_size")
        and mtime_ns == record.get("last_checked_mtime_ns")
    ):
        return prior

    def mutate(working):
        current = working["sessions"].get(session_id)
        if not isinstance(current, dict) or current.get("session_id") != session_id:
            raise StateCorruptError("office session identity is invalid")
        current["workspace_changed"] = changed
        current["last_checked_size"] = size
        current["last_checked_mtime_ns"] = mtime_ns

    updated = store.update(chat, mutate)
    return _status_projection(updated["sessions"][session_id])


def _observe(chat: str, record: dict[str, Any]) -> object:
    cached = _cached_pair(record)
    baseline = record.get("baseline_sha256")
    if not isinstance(baseline, str) or not _HASH.fullmatch(baseline):
        raise StateCorruptError("office session baseline is invalid")
    broker = OutputsBroker()
    try:
        relative_path = broker.resolve_file_id(chat, record["file_id"])
    except FileIdNotFoundError:
        return _UNAVAILABLE
    try:
        path_parts = _parts(relative_path)
    except UnsafePathError:
        return _UNAVAILABLE
    file_fd = None
    try:
        file_fd = _open_file(chat, path_parts)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            return _UNAVAILABLE
        sample = (before.st_size, before.st_mtime_ns)
        if cached is not None and sample == cached:
            changed = record.get("workspace_changed")
            if type(changed) is not bool:
                raise StateCorruptError("office session change notice is invalid")
            return (changed, cached[0], cached[1])
        _digest = _hash_regular(
            file_fd, "/".join(path_parts), max_bytes=broker.max_file_size
        )[1]
        after = os.fstat(file_fd)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_ino != before.st_ino
            or after.st_dev != before.st_dev
        ):
            return _UNAVAILABLE
        return (_digest != baseline, after.st_size, after.st_mtime_ns)
    except (UnsafePathError, FileTooLargeError, OSError):
        return _UNAVAILABLE
    finally:
        if file_fd is not None:
            os.close(file_fd)


def _cached_pair(record: dict[str, Any]) -> tuple[int, int] | None:
    present = [field in record for field in _CACHE_FIELDS]
    if not any(present):
        return None
    if not all(present):
        raise StateCorruptError("office session notice cache is invalid")
    size = record["last_checked_size"]
    mtime_ns = record["last_checked_mtime_ns"]
    if size is None and mtime_ns is None:
        return None
    if type(size) is not int or size < 0 or type(mtime_ns) is not int:
        raise StateCorruptError("office session notice cache is invalid")
    return (size, mtime_ns)
