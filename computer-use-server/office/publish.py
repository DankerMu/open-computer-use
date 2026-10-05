# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Publish a persisted obligation inside an observed, narrowly owned writer fence.

Recovery and lifecycle mapping have separate owners. IO interruption propagates:
replacement is not a rollback boundary.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import re
import stat
import sys
import time
import uuid
from dataclasses import dataclass

import docker_manager
from docker.errors import NotFound
from outputs_broker import FileIdNotFoundError, OutputsBroker, OutputsBrokerError

from . import versions, workspace
from .store import OfficeStore, StateCorruptError, _DATA_ROOT_FLAGS, _DIRECTORY_FLAGS, _WRITE_FLAGS

_HASH = re.compile(r"[0-9a-f]{64}\Z")
_PATH_ERRORS = {errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.EMLINK}
_LOG = logging.getLogger(__name__)
_PAUSE_BUDGET_SECONDS = 5


@dataclass(frozen=True)
class PublishResult:
    outcome: str
    reason: str | None = None


class SandboxStateError(RuntimeError):
    """Observed sandbox state does not admit a safe publication."""


class RecoveryRequiredError(RuntimeError):
    """A prepared obligation belongs to recovery, not a new publish attempt."""


class _BudgetExpired(Exception):
    pass


def _record(message, **facts):
    try:
        _LOG.info(message, extra=facts)
    except Exception:
        # A logging transport cannot change a durable publication outcome.
        pass


class _Fence:
    def __init__(self, store, chat, container):
        self.store, self.chat, self.container = store, chat, container
        self.container_id = container.id if container is not None else None
        self.opened = store._open_tree(chat, create=False)
        if self.opened is None:
            raise StateCorruptError("publish office directory is missing")
        self.marker_identity = None
        self.marker_retained = False
        self.started = None
        self.pause_requested_at = None
        self.attempted = False
        self.released = False
        self.cleanup_failed = False
        try:
            os.lstat("fence.json", dir_fd=self.opened[-1])
        except FileNotFoundError:
            return
        except BaseException:
            self.close()
            raise
        self.close()
        raise RecoveryRequiredError("existing publication fence requires recovery")

    def checkpoint(self):
        if self.started is not None and time.monotonic() - self.started >= _PAUSE_BUDGET_SECONDS:
            raise _BudgetExpired()

    def acquire(self):
        self.started = time.monotonic()
        encoded = json.dumps({
            "schema_version": 1, "container_id": self.container_id,
            "pause_started_at": time.time(),
        }, allow_nan=False, separators=(",", ":")).encode("utf-8")
        temporary = f".fence.{uuid.uuid4().hex}.tmp"
        fd = os.open(temporary, _WRITE_FLAGS, 0o600, dir_fd=self.opened[-1])
        identity = None
        try:
            identity = _identity(os.fstat(fd))
            os.fchmod(fd, 0o600)
            self.store._write_all(fd, encoded)
            os.fsync(fd)
            for directory in self.opened:
                os.fsync(directory)
            # link installs complete bytes atomically, refusing any existing name.
            os.link(temporary, "fence.json", src_dir_fd=self.opened[-1],
                    dst_dir_fd=self.opened[-1], follow_symlinks=False)
            self.marker_identity = identity
            self.marker_retained = True
            installed = os.lstat("fence.json", dir_fd=self.opened[-1])
            if not stat.S_ISREG(installed.st_mode) or _identity(installed) != identity:
                raise StateCorruptError("publication marker identity changed")
            os.fsync(self.opened[-1])
        finally:
            primary = sys.exc_info()[1]
            cleanup_error = None
            try:
                if identity is not None:
                    _remove_owned(self.opened[-1], temporary, identity)
                    os.fsync(self.opened[-1])
            except OSError as exc:
                cleanup_error = exc
            try:
                os.close(fd)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
            if cleanup_error is not None and primary is None:
                raise cleanup_error
        fresh = self.store._open_tree(self.chat, create=False)
        try:
            if fresh is None or any(_identity(os.fstat(old)) != _identity(os.fstat(new))
                                    for old, new in zip(self.opened, fresh)):
                raise StateCorruptError("publication marker directory identity changed")
        finally:
            primary = sys.exc_info()[1]
            cleanup_error = None
            if fresh is not None:
                for directory in reversed(fresh):
                    try:
                        os.close(directory)
                    except OSError as exc:
                        cleanup_error = cleanup_error or exc
            if cleanup_error is not None and primary is None:
                raise cleanup_error
        self.checkpoint()
        self.pause_requested_at = time.monotonic()
        self.attempted = True
        try:
            self.container.pause()
            observed = self.observe()
        except Exception:
            self.checkpoint()
            return False
        self.checkpoint()
        return observed == "paused"

    def observe(self):
        # SDK reload addresses the original object by ID, not its reusable name.
        if self.container.id != self.container_id:
            return "unknown"
        try:
            self.container.reload()
        except NotFound:
            return "absent"
        if self.container.id != self.container_id:
            return "unknown"
        state = self.container.attrs.get("State", {})
        if state.get("Paused") is True and self.container.status in {"running", "paused"}:
            return "paused"
        if state.get("Paused") is False and self.container.status in {"running", "exited", "dead", "created"}:
            return "released"
        return "unknown"

    def release(self, outcome):
        try:
            if self.marker_identity is not None:
                if not self.attempted:
                    self.released = True
                else:
                    observed = self.observe()
                    if observed == "paused":
                        try:
                            self.container.unpause()
                        except Exception:
                            self.cleanup_failed = True
                        observed = self.observe()
                    self.released = observed in {"released", "absent"}
                if self.released:
                    _remove_owned(self.opened[-1], "fence.json", self.marker_identity)
                    try:
                        os.lstat("fence.json", dir_fd=self.opened[-1])
                    except FileNotFoundError:
                        self.marker_retained = False
                    os.fsync(self.opened[-1])
        except Exception:
            self.cleanup_failed = True
        finally:
            self.close()
            try:
                if self.attempted:
                    finished = time.monotonic()
                    _record("Office publication paused window",
                            chat_id=self.chat,
                            paused_duration_seconds=finished - self.pause_requested_at,
                            duration_basis="pause-request-through-release-handling",
                            publication_elapsed_seconds=finished - self.started,
                            release_observed=self.released,
                            marker_retained=self.marker_retained,
                            cleanup_failed=self.cleanup_failed,
                            publish_outcome=outcome)
            except Exception:
                pass

    def close(self):
        opened, self.opened = self.opened, None
        if opened is not None:
            for fd in reversed(opened):
                try:
                    os.close(fd)
                except OSError:
                    self.cleanup_failed = True


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


def publish(chat_id: str, journal_id: str) -> PublishResult:
    """Consume one obligation, or return interrupted/publish_timeout with it retained.

    A postreplace interruption leaves complete successor bytes and prepared journal
    metadata for recovery. It is not a terminal failed outcome or an acknowledgement.
    Final Office durability exceptions retain the store's visible-successor rules.
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
        if container is not None:
            paused = container.attrs.get("State", {}).get("Paused")
            if (container.status not in {"running", "paused", "exited"} or type(paused) is not bool
                    or container.status == "paused" and not paused
                    or container.status == "exited" and paused):
                raise SandboxStateError(f"sandbox does not admit publish: {container.status}")
        fence = _Fence(store, chat, container)
        held, owned, temporary_fd = [], None, None
        replaced = False
        result = None
        try:
            try:
                path = broker.resolve_file_id(chat, entry["file_id"])
            except FileIdNotFoundError:
                result = PublishResult("conflict", "path_missing")
            except OutputsBrokerError:
                result = PublishResult("failed", "index_unavailable")
            if result is None:
                parts = workspace._parts(path)
                temporary = f".office-publish.{uuid.uuid4().hex}.tmp"

                def prepare(working):
                    working["journal"][journal_id].update(target_path=path, temporary_name=temporary)
                store.update(chat, prepare)
                try:
                    # Resolve missing/unsafe paths without reading workspace bytes.
                    held = _parents(chat, parts)
                    target = os.lstat(parts[-1], dir_fd=held[-1])
                    if not stat.S_ISREG(target.st_mode):
                        result = PublishResult("conflict", "baseline_mismatch")
                except OSError as exc:
                    if exc.errno not in _PATH_ERRORS:
                        raise
                    result = PublishResult("conflict", "path_missing" if exc.errno == errno.ENOENT else "baseline_mismatch")
                if result is None and container is not None and container.status == "running" and not paused:
                    if not fence.acquire():
                        result = PublishResult("failed", "pause_failed")
                try:
                    if result is None:
                        fence.checkpoint()
                        target = os.lstat(parts[-1], dir_fd=held[-1])
                        _body, digest = store.read_workspace_file(chat, path, max_bytes=broker.max_file_size)
                        del _body
                        fence.checkpoint()
                except workspace.FileTooLargeError:
                    fence.checkpoint()
                    result = PublishResult("conflict", "baseline_mismatch")
                except workspace.UnsafePathError as exc:
                    cause = exc.__cause__
                    if isinstance(cause, OSError) and cause.errno not in _PATH_ERRORS:
                        raise
                    fence.checkpoint()
                    reason = "path_missing" if isinstance(cause, FileNotFoundError) else "baseline_mismatch"
                    result = PublishResult("conflict", reason)
                except OSError as exc:
                    if exc.errno not in _PATH_ERRORS:
                        raise
                    fence.checkpoint()
                    result = PublishResult("conflict", "path_missing" if exc.errno == errno.ENOENT else "baseline_mismatch")
                if result is None and digest != baseline:
                    result = PublishResult("conflict", "baseline_mismatch")
                if result is None:
                    fence.checkpoint()
                    content = versions.read_version_bytes(store, chat, selected["sha256"])
                    fence.checkpoint()
                    if len(content) != selected["size"]:
                        raise StateCorruptError("publish version size differs from its blob")
                    temporary_fd = os.open(temporary, _WRITE_FLAGS, 0o600, dir_fd=held[-1])
                    owned = _identity(os.fstat(temporary_fd))
                    fence.checkpoint()
                    store._write_all(temporary_fd, content)
                    fence.checkpoint()
                    os.fchmod(temporary_fd, 0o666)
                    fence.checkpoint()
                    os.fsync(temporary_fd)
                    fence.checkpoint()
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
                    fence.checkpoint()
                    if result is None:
                        os.replace(temporary, parts[-1], src_dir_fd=held[-1], dst_dir_fd=held[-1])
                        replaced = True
                        fence.checkpoint()
                        os.fsync(held[-1])
                        fence.checkpoint()
                        registered = broker.register_host_write(chat, path)
                        fence.checkpoint()
                        if (registered["file_id"] != entry["file_id"]
                                or registered["hash"] != selected["sha256"]
                                or registered["size"] != selected["size"]):
                            raise StateCorruptError("registered publish differs from its version")
                        result = PublishResult("published")
        except _BudgetExpired:
            result = PublishResult("interrupted" if replaced else "failed", "publish_timeout")
        finally:
            primary = sys.exc_info()[1]
            cleanup_error = None
            try:
                if owned is not None and not replaced:
                    _remove_owned(held[-1], temporary, owned)
            except OSError as exc:
                cleanup_error = exc
            for fd in ([temporary_fd] if temporary_fd is not None else []) + list(reversed(held)):
                try:
                    os.close(fd)
                except OSError as exc:
                    cleanup_error = cleanup_error or exc
            fence.release(result.outcome if result is not None else "exception")
            if cleanup_error is not None:
                if primary is None and result is None:
                    raise cleanup_error
                _record("Office publication workspace cleanup failed", chat_id=chat)
        if result.outcome == "interrupted":
            return result
        return _finish(store, chat, journal_id, result, entry, selected)
