# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Consume a persisted publish obligation only for a confirmed absent/exited sandbox.

Running fences, retained-obligation recovery and lifecycle mapping have separate
owners. IO interruption propagates: replacement is not a rollback boundary.
"""
from __future__ import annotations

import errno
import os
import re
import stat
import uuid
from dataclasses import dataclass

import docker_manager
from outputs_broker import FileIdNotFoundError, OutputsBroker, OutputsBrokerError

from . import versions, workspace
from .store import OfficeStore, StateCorruptError, _DATA_ROOT_FLAGS, _DIRECTORY_FLAGS, _WRITE_FLAGS

_HASH = re.compile(r"[0-9a-f]{64}\Z")
_PATH_ERRORS = {errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.EMLINK}


@dataclass(frozen=True)
class PublishResult:
    outcome: str
    reason: str | None = None


class SandboxStateError(RuntimeError):
    """Observed sandbox state does not admit the stopped-only operation."""


class RecoveryRequiredError(RuntimeError):
    """A prepared obligation belongs to recovery, not a new publish attempt."""


def _binding(state, journal_id):
    entry = state["journal"].get(journal_id)
    if not isinstance(entry, dict):
        raise StateCorruptError("publish obligation is missing or invalid")
    file_id, number = entry.get("file_id"), entry.get("version")
    requester = entry.get("requester")
    if not isinstance(file_id, str) or not file_id or type(number) is not int or number < 1:
        raise StateCorruptError("publish document/version binding is invalid")
    if not isinstance(requester, str) or requester not in {"save", "final", "resolve", "restore"}:
        raise StateCorruptError("publish requester is invalid")
    document = state["documents"].get(file_id)
    if not isinstance(document, dict) or document.get("file_id") != file_id:
        raise StateCorruptError("publish document binding is invalid")
    listed = versions._document_versions(state, file_id)
    if number > len(listed):
        raise StateCorruptError("publish version binding is invalid")
    selected = listed[number - 1]
    session_id, seq = entry.get("session_id"), entry.get("save_seq")
    if session_id is None:
        if requester not in {"resolve", "restore"} or seq is not None:
            raise StateCorruptError("sessionless publish binding is invalid")
        baseline = document.get("published_sha256")
    else:
        session = state["sessions"].get(session_id) if isinstance(session_id, str) else None
        if (not isinstance(session, dict) or session.get("session_id") != session_id
                or session.get("file_id") != file_id or type(seq) is not int or seq < 1
                or type(session.get("save_seq")) is not int or seq > session["save_seq"]):
            raise StateCorruptError("publish session/sequence binding is invalid")
        baseline = session.get("baseline_sha256")
    if not isinstance(baseline, str) or not _HASH.fullmatch(baseline):
        raise StateCorruptError("publish baseline is invalid")
    if "target_path" in entry or "temporary_name" in entry:
        raise RecoveryRequiredError("prepared publish obligation requires recovery")
    return entry, selected, baseline


def _identity(info):
    return info.st_dev, info.st_ino


def _parents(chat, parts):
    """Keep the original parent alive even if its pathname is renamed."""
    held = [os.open(str(docker_manager.BASE_DATA_DIR), _DATA_ROOT_FLAGS)]
    try:
        for component in (chat, "outputs", *parts[:-1]):
            held.append(os.open(component, _DIRECTORY_FLAGS, dir_fd=held[-1]))
        return held
    except BaseException:
        for fd in reversed(held):
            os.close(fd)
        raise


def _revalidate(chat, parts, held, target_identity):
    fresh = _parents(chat, parts)
    try:
        if any(_identity(os.fstat(old)) != _identity(os.fstat(new))
               for old, new in zip(held, fresh)):
            raise workspace.UnsafePathError("publish parent identity changed")
        target = os.lstat(parts[-1], dir_fd=fresh[-1])
        if not stat.S_ISREG(target.st_mode) or _identity(target) != target_identity:
            raise workspace.UnsafePathError("publish target identity changed")
    finally:
        for fd in reversed(fresh):
            os.close(fd)


def _remove_owned(parent_fd, name, identity):
    try:
        current = os.lstat(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    if _identity(current) == identity:
        os.unlink(name, dir_fd=parent_fd)


def _finish(store, chat, journal_id, result, entry, selected):
    def complete(state):
        if result.outcome == "published":
            document = state["documents"][entry["file_id"]]
            document["versions"][selected["number"] - 1]["published"] = True
            document["published_version"] = selected["number"]
            document["published_sha256"] = selected["sha256"]
            if entry.get("session_id") is not None:
                state["sessions"][entry["session_id"]]["baseline_sha256"] = selected["sha256"]
        del state["journal"][journal_id]
    store.update(chat, complete)
    return result


def publish_stopped(chat_id: str, journal_id: str) -> PublishResult:
    """Publish one unprepared persisted obligation; never create a missing chat.

    Engine uncertainty and unsupported states propagate without changing the
    obligation. Expected prewrite outcomes consume it without lifecycle changes;
    all interrupted IO retains it unless the final Office successor is visible.
    """
    chat = docker_manager.canonical_lock_chat_id(chat_id)
    if not isinstance(journal_id, str) or not journal_id:
        raise ValueError("journal_id must be a non-empty string")
    store, broker = OfficeStore(), OutputsBroker()
    with docker_manager._combined_lock(chat, create=False) as lock:
        if lock is None:
            raise StateCorruptError("publish chat is missing or unsafe")
        state = store.read(chat)
        entry, selected, baseline = _binding(state, journal_id)
        container = docker_manager._lookup_container(chat)
        if container is not None and container.status != "exited":
            raise SandboxStateError(f"sandbox does not admit stopped publish: {container.status}")
        try:
            path = broker.resolve_file_id(chat, entry["file_id"])
        except FileIdNotFoundError:
            return _finish(store, chat, journal_id, PublishResult("conflict", "path_missing"), entry, selected)
        except OutputsBrokerError:
            return _finish(store, chat, journal_id, PublishResult("failed", "index_unavailable"), entry, selected)
        parts = workspace._parts(path)
        temporary = f".office-publish.{uuid.uuid4().hex}.tmp"

        def prepare(working):
            working["journal"][journal_id].update(target_path=path, temporary_name=temporary)
        store.update(chat, prepare)

        held = []
        owned = None
        temporary_fd = None
        replaced = False
        result = None
        try:
            try:
                held = _parents(chat, parts)
                target = os.lstat(parts[-1], dir_fd=held[-1])
                _body, digest = store.read_workspace_file(chat, path, max_bytes=broker.max_file_size)
                del _body
            except workspace.FileTooLargeError:
                result = PublishResult("conflict", "baseline_mismatch")
            except workspace.UnsafePathError as exc:
                cause = exc.__cause__
                if isinstance(cause, OSError) and cause.errno not in _PATH_ERRORS:
                    raise
                reason = "path_missing" if isinstance(cause, FileNotFoundError) else "baseline_mismatch"
                result = PublishResult("conflict", reason)
            except OSError as exc:
                if exc.errno not in _PATH_ERRORS:
                    raise
                result = PublishResult("conflict", "path_missing" if exc.errno == errno.ENOENT else "baseline_mismatch")
            if result is None and digest != baseline:
                result = PublishResult("conflict", "baseline_mismatch")
            if result is None:
                content = versions.read_version_bytes(store, chat, selected["sha256"])
                if len(content) != selected["size"]:
                    raise StateCorruptError("publish version size differs from its blob")
                temporary_fd = os.open(temporary, _WRITE_FLAGS, 0o600, dir_fd=held[-1])
                owned = _identity(os.fstat(temporary_fd))
                store._write_all(temporary_fd, content)
                os.fchmod(temporary_fd, 0o666)
                os.fsync(temporary_fd)
                try:
                    _revalidate(chat, parts, held, _identity(target))
                    staged = os.lstat(temporary, dir_fd=held[-1])
                    if not stat.S_ISREG(staged.st_mode) or _identity(staged) != owned:
                        raise workspace.UnsafePathError("publish temporary identity changed")
                except workspace.UnsafePathError:
                    result = PublishResult("failed", "unsafe_path")
                except OSError as exc:
                    if exc.errno not in _PATH_ERRORS:
                        raise
                    result = PublishResult("failed", "unsafe_path")
                if result is None:
                    os.replace(temporary, parts[-1], src_dir_fd=held[-1], dst_dir_fd=held[-1])
                    replaced = True
                    os.fsync(held[-1])
                    registered = broker.register_host_write(chat, path)
                    if (registered["file_id"] != entry["file_id"]
                            or registered["hash"] != selected["sha256"]
                            or registered["size"] != selected["size"]):
                        raise StateCorruptError("registered publish differs from its version")
                    result = PublishResult("published")
        finally:
            try:
                if owned is not None and not replaced:
                    _remove_owned(held[-1], temporary, owned)
            finally:
                if temporary_fd is not None:
                    os.close(temporary_fd)
                for fd in reversed(held):
                    os.close(fd)
        return _finish(store, chat, journal_id, result, entry, selected)
