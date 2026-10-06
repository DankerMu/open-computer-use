# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Publication and crash recovery under the canonical chat lock."""
from __future__ import annotations

import errno
import json
import math
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

from . import sessions, versions, workspace
from .store import OfficeStore, StateCorruptError, StateDurabilityError, _DATA_ROOT_FLAGS, _DIRECTORY_FLAGS, _FILE_FLAGS, _WRITE_FLAGS

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
    """Unresolved ownership or fencing prevents safe publication."""


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
    _prepared_fields(entry)
    return entry, selected, baseline


def _identity(info):
    return info.st_dev, info.st_ino


def _parents(chat, parts):
    """Keep the original parent alive even if its pathname is renamed."""
    return _open_parents((chat, "outputs", *parts[:-1]))


def _open_parents(components):
    held = [os.open(str(docker_manager.BASE_DATA_DIR), _DATA_ROOT_FLAGS)]
    try:
        for component in components:
            held.append(os.open(component, _DIRECTORY_FLAGS, dir_fd=held[-1]))
        return held
    except BaseException:
        for fd in reversed(held):
            os.close(fd)
        raise


def _verify_parent_prefix(components, held):
    fresh = _open_parents(components)
    try:
        if len(fresh) != len(held) or any(
            _identity(os.fstat(old)) != _identity(os.fstat(new))
            for old, new in zip(held, fresh)
        ):
            raise RecoveryRequiredError("publish parent identity changed")
    finally:
        for fd in reversed(fresh):
            os.close(fd)


@dataclass(frozen=True)
class _AbsentParent:
    prefix: tuple[str, ...]
    held: tuple[int, ...]
    missing: str

    def confirm(self):
        _verify_parent_prefix(self.prefix, self.held)
        try:
            os.lstat(self.missing, dir_fd=self.held[-1])
        except FileNotFoundError:
            os.fsync(self.held[-1])
            return
        raise RecoveryRequiredError("publish parent absence is unconfirmed")


def _prepared_parents(chat, parts):
    components = (chat, "outputs", *parts[:-1])
    held = [os.open(str(docker_manager.BASE_DATA_DIR), _DATA_ROOT_FLAGS)]
    try:
        for index, component in enumerate(components):
            try:
                fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=held[-1])
            except FileNotFoundError:
                absent = _AbsentParent(components[:index], tuple(held), component)
                absent.confirm()
                return held, absent
            held.append(fd)
        _verify_parent_prefix(components, held)
        return held, None
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
        return True
    if _identity(current) != identity:
        return False
    os.unlink(name, dir_fd=parent_fd)
    return True


def _prepared_fields(entry):
    prepared = "target_path" in entry or "temporary_name" in entry
    if not prepared:
        if "staging" in entry:
            raise StateCorruptError("publish staging has no target binding")
        return
    try:
        workspace._parts(entry["target_path"])
    except (KeyError, ValueError, TypeError, workspace.UnsafePathError) as exc:
        raise StateCorruptError("publish target binding is invalid") from exc
    if not isinstance(entry.get("temporary_name"), str) or not re.fullmatch(
        r"\.office-publish\.[0-9a-f]{32}\.tmp", entry["temporary_name"],
    ):
        raise StateCorruptError("publish temporary binding is invalid")
    binding = entry.get("staging")
    if "staging" not in entry:
        return
    if (not isinstance(binding, dict) or binding.get("schema_version") != 1
            or type(binding.get("schema_version")) is not int
            or type(binding.get("retired", False)) is not bool
            or any(type(binding.get(key)) is not int or binding[key] < 0
                   for key in ("device", "inode"))
            or binding.get("anchor_name") != entry["temporary_name"]
            or not isinstance(binding.get("witness_name"), str)
            or not re.fullmatch(r"\.publish-owner\.[0-9a-f]{32}", binding["witness_name"])):
        raise StateCorruptError("publish ownership binding is invalid")


class _Stage:
    """Private links pin active ownership; retirement precedes the final unlink.

    Unbound and retired private leftovers are never adopted or removed on replay.
    A retry uses fresh names and cannot expose an unbound allocation.
    """

    def __init__(self, store, chat, journal_id, entry):
        self.store, self.chat, self.journal_id, self.entry = store, chat, journal_id, entry
        self.opened = store._open_tree(chat, create=False)
        if self.opened is None:
            raise StateCorruptError("publish office directory is missing")
        self.directory = None
        try:
            self.directory = store._open_dir(
                "staging", dir_fd=self.opened[-1], create=True,
                label="office staging directory",
            )
            if os.fstat(self.directory).st_mode & 0o077:
                raise StateCorruptError("publish staging directory is not private")
            os.fsync(self.opened[-1])
        except BaseException:
            self.close()
            raise

    def inspect(self):
        fresh = self.store._open_tree(self.chat, create=False)
        staging = None
        try:
            if fresh is None or any(_identity(os.fstat(old)) != _identity(os.fstat(new))
                                    for old, new in zip(self.opened, fresh)):
                raise RecoveryRequiredError("publish control directory changed")
            staging = self.store._open_dir(
                "staging", dir_fd=fresh[-1], create=False, label="office staging directory",
            )
            if staging is None or _identity(os.fstat(staging)) != _identity(os.fstat(self.directory)):
                raise RecoveryRequiredError("publish staging directory changed")
        finally:
            if staging is not None:
                os.close(staging)
            if fresh is not None:
                for fd in reversed(fresh):
                    os.close(fd)
        binding = self.entry.get("staging")
        if binding is None:
            return None
        identity = binding["device"], binding["inode"]
        found = False
        for name in (binding["anchor_name"], binding["witness_name"]):
            try:
                info = os.lstat(name, dir_fd=self.directory)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or _identity(info) != identity:
                raise RecoveryRequiredError("publish private ownership changed")
            found = True
        if binding.get("retired", False):
            return None
        return identity if found else None

    def remove_shared(self, parent):
        identity = self.inspect()
        try:
            info = os.lstat(self.entry["temporary_name"], dir_fd=parent)
        except FileNotFoundError:
            os.fsync(parent)
            return
        if identity is None or not stat.S_ISREG(info.st_mode) or _identity(info) != identity:
            raise RecoveryRequiredError("publish shared ownership is unproven")
        if not _remove_owned(parent, self.entry["temporary_name"], identity):
            raise RecoveryRequiredError("publish shared ownership changed during cleanup")
        try:
            os.lstat(self.entry["temporary_name"], dir_fd=parent)
        except FileNotFoundError:
            pass
        else:
            raise RecoveryRequiredError("publish shared entry reappeared during cleanup")
        os.fsync(parent)

    def cleanup(self, parent, *, absent_parent=None):
        if parent is None and absent_parent is None:
            raise RecoveryRequiredError("publish shared absence is unverified")
        identity = self.inspect()
        binding = self.entry.get("staging")
        pin = None
        try:
            if identity is not None:
                for name in (binding["anchor_name"], binding["witness_name"]):
                    try:
                        pin = os.open(name, _FILE_FLAGS, dir_fd=self.directory)
                    except FileNotFoundError:
                        continue
                    if not stat.S_ISREG(os.fstat(pin).st_mode) or _identity(os.fstat(pin)) != identity:
                        raise RecoveryRequiredError("publish private ownership changed")
                    break
                if pin is None:
                    raise RecoveryRequiredError("publish ownership disappeared")
            if absent_parent is None:
                self.remove_shared(parent)
            else:
                absent_parent.confirm()
            if binding is not None and not binding.get("retired", False):
                def retire(state):
                    state["journal"][self.journal_id]["staging"]["retired"] = True
                try:
                    self.entry = self.store.update(self.chat, retire)["journal"][self.journal_id]
                except StateDurabilityError:
                    self.entry = self.store.read(self.chat)["journal"][self.journal_id]
                    raise
                if identity is not None:
                    for name in (binding["anchor_name"], binding["witness_name"]):
                        if not _remove_owned(self.directory, name, identity):
                            raise RecoveryRequiredError("publish private ownership changed during retirement")
                        os.fsync(self.directory)
            self.inspect()
            os.fsync(self.directory)
        finally:
            if pin is not None:
                try:
                    os.close(pin)
                except OSError:
                    _record("Office publication workspace cleanup failed", chat_id=self.chat)

    def allocate(self, journal_id, content, fence):
        name = self.entry["temporary_name"]
        witness = f".publish-owner.{uuid.uuid4().hex}"
        fd = os.open(name, _WRITE_FLAGS, 0o600, dir_fd=self.directory)
        try:
            identity = _identity(os.fstat(fd))
            fence.checkpoint()
            os.fsync(fd)
            fence.checkpoint()
            os.link(name, witness, src_dir_fd=self.directory,
                    dst_dir_fd=self.directory, follow_symlinks=False)
            fence.checkpoint()
            os.fsync(self.directory)
            fence.checkpoint()
            binding = {
                "schema_version": 1, "anchor_name": name, "witness_name": witness,
                "device": identity[0], "inode": identity[1], "retired": False,
            }
            def bind(state):
                state["journal"][journal_id]["staging"] = binding
            self.store.update(self.chat, bind)
            self.entry["staging"] = binding
            fence.checkpoint()
            self.store._write_all(fd, content)
            fence.checkpoint()
            os.fchmod(fd, 0o666)
            fence.checkpoint()
            os.fsync(fd)
            fence.checkpoint()
        finally:
            primary = sys.exc_info()[1]
            try:
                os.close(fd)
            except OSError:
                if primary is None:
                    raise
        return identity

    def expose(self, parent, fence):
        identity = self.inspect()
        fence.checkpoint()
        if identity is None:
            raise RecoveryRequiredError("publish anchor is missing")
        if os.fstat(parent).st_dev != identity[0]:
            raise OSError(errno.EXDEV, "publish staging and workspace differ")
        os.link(self.entry["staging"]["anchor_name"], self.entry["temporary_name"],
                src_dir_fd=self.directory, dst_dir_fd=parent, follow_symlinks=False)
        fence.checkpoint()
        installed = os.lstat(self.entry["temporary_name"], dir_fd=parent)
        if not stat.S_ISREG(installed.st_mode) or _identity(installed) != identity:
            raise RecoveryRequiredError("publish shared ownership changed")
        fence.checkpoint()
        os.fsync(parent)
        fence.checkpoint()
        return identity

    def close(self):
        primary = sys.exc_info()[1]
        error = None
        fds = ([self.directory] if self.directory is not None else []) + list(reversed(self.opened or ()))
        self.directory, self.opened = None, None
        for fd in fds:
            try:
                os.close(fd)
            except OSError as exc:
                error = error or exc
        if error is not None and primary is None:
            raise error


def _recover_fence(store, chat, now):
    opened = store._open_tree(chat, create=False)
    if opened is None:
        raise StateCorruptError("publish office directory is missing")
    marker_fd = None
    try:
        try:
            info = os.lstat("fence.json", dir_fd=opened[-1])
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode):
            raise RecoveryRequiredError("publication marker is not regular")
        marker_fd = os.open("fence.json", _FILE_FLAGS, dir_fd=opened[-1])
        if _identity(os.fstat(marker_fd)) != _identity(info):
            raise RecoveryRequiredError("publication marker changed")
        body = store._read_all(marker_fd)
        try:
            marker = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise RecoveryRequiredError("publication marker is corrupt") from exc
        if (not isinstance(marker, dict) or type(marker.get("schema_version")) is not int
                or marker["schema_version"] != 1
                or type(marker.get("pause_started_at")) not in (int, float)
                or not math.isfinite(marker["pause_started_at"])
                or marker["pause_started_at"] < 0
                or not isinstance(marker.get("container_id"), str)
                or not marker["container_id"]):
            raise RecoveryRequiredError("publication marker is invalid")
        if now - marker["pause_started_at"] <= _PAUSE_BUDGET_SECONDS:
            raise RecoveryRequiredError("publication marker is young")
        try:
            container = docker_manager.get_docker_client().containers.get(marker["container_id"])
        except NotFound:
            container = None
        if container is not None:
            observer = _Fence.__new__(_Fence)
            observer.container, observer.container_id = container, marker["container_id"]
            observed = observer.observe()
            if observed == "paused":
                try:
                    container.unpause()
                except Exception:
                    pass
                observed = observer.observe()
            if observed not in {"released", "absent"}:
                raise RecoveryRequiredError("publication release is uncertain")
        current = os.lstat("fence.json", dir_fd=opened[-1])
        if not stat.S_ISREG(current.st_mode) or _identity(current) != _identity(info):
            raise RecoveryRequiredError("publication marker changed")
        if not _remove_owned(opened[-1], "fence.json", _identity(info)):
            raise RecoveryRequiredError("publication marker changed during cleanup")
        try:
            os.lstat("fence.json", dir_fd=opened[-1])
        except FileNotFoundError:
            pass
        else:
            raise RecoveryRequiredError("publication marker reappeared during cleanup")
        os.fsync(opened[-1])
    finally:
        if marker_fd is not None:
            os.close(marker_fd)
        for fd in reversed(opened):
            os.close(fd)


def _ordered_obligations(state):
    validated = {}
    for journal_id in state["journal"]:
        if not isinstance(journal_id, str) or not journal_id:
            raise StateCorruptError("publish journal identity is invalid")
        entry, selected, _baseline = _binding(state, journal_id)
        validated[journal_id] = (entry["file_id"], selected["number"],
                                 entry.get("save_seq") or 0, journal_id)
    return sorted(validated, key=validated.__getitem__)


def _recover_locked(store, broker, chat, now):
    _recover_fence(store, chat, now)
    state = store.read(chat)
    for journal_id in _ordered_obligations(state):
        result = _publish_locked(store, broker, chat, journal_id, recovering=True)
        if result.outcome == "interrupted":
            raise RecoveryRequiredError("publication recovery was interrupted")
        _recover_fence(store, chat, now)


def recover_publications(chat_id: str, now: float | None = None) -> None:
    """Drive surviving obligations; uncertainty retains responsibility and raises."""
    chat = docker_manager.canonical_lock_chat_id(chat_id)
    store, broker = OfficeStore(), OutputsBroker()
    with docker_manager._combined_lock(chat, create=False) as lock:
        if lock is None:
            raise StateCorruptError("publish chat is missing or unsafe")
        _recover_locked(store, broker, chat, time.time() if now is None else now)

def _finish(store, chat, journal_id, result, entry, selected):
    def complete(state):
        session_id = entry.get("session_id")
        record = sessions._require_session(state, session_id) if session_id is not None else None
        if record is not None:
            sessions._status_projection(record)
        if result.outcome == "published":
            document = state["documents"][entry["file_id"]]
            document["versions"][selected["number"] - 1]["published"] = True
            document["published_version"] = selected["number"]
            document["published_sha256"] = selected["sha256"]
            if record is not None:
                record["baseline_sha256"] = selected["sha256"]
                record["last_published_seq"] = max(record.get("last_published_seq", 0), entry["save_seq"])
        if record is not None and entry["requester"] in ("save", "final"):
            owns_save = record.get("pending_save_seq") == entry["save_seq"]
            if entry["requester"] == "final":
                record["state"] = {
                    "published": "closed", "conflict": "conflict", "failed": "error",
                }[result.outcome]
                record["reason"] = result.reason
                record["pending_close_seq"] = None
            elif record["state"] not in sessions.FINAL_STATES and record["state"] != "closing":
                if result.outcome == "conflict":
                    record["state"] = "conflict"
                    record["reason"] = result.reason
                elif record["state"] != "conflict":
                    record["reason"] = result.reason
                    if owns_save:
                        record["state"] = "editing"
            if owns_save:
                record["pending_save_seq"] = None
        del state["journal"][journal_id]
    store.update(chat, complete)
    return result


def publish(chat_id: str, journal_id: str) -> PublishResult:
    """Recover older obligations before consuming the requested publication."""
    chat = docker_manager.canonical_lock_chat_id(chat_id)
    if not isinstance(journal_id, str) or not journal_id:
        raise ValueError("journal_id must be a non-empty string")
    store, broker = OfficeStore(), OutputsBroker()
    with docker_manager._combined_lock(chat, create=False) as lock:
        if lock is None:
            raise StateCorruptError("publish chat is missing or unsafe")
        state = store.read(chat)
        requested_entry, requested_version, _baseline = _binding(state, journal_id)
        _recover_fence(store, chat, time.time())
        ordered = _ordered_obligations(state)
        preceding, succeeding = [], []
        for obligation in ordered:
            if obligation == journal_id:
                continue
            entry = state["journal"][obligation]
            if (entry["file_id"] == requested_entry["file_id"]
                    and entry["version"] > requested_version["number"]):
                succeeding.append(obligation)
            else:
                preceding.append(obligation)
        result = None
        for obligation in (*preceding, journal_id, *succeeding):
            requested = obligation == journal_id
            try:
                outcome = _publish_locked(
                    store, broker, chat, obligation,
                    recovering=not requested or "target_path" in state["journal"][obligation],
                )
                if requested:
                    result = outcome
                if outcome.outcome == "interrupted":
                    if requested:
                        return outcome
                    raise RecoveryRequiredError("publication recovery was interrupted")
                if not requested:
                    _recover_fence(store, chat, time.time())
            except RecoveryRequiredError:
                if result is None:
                    raise
                return result
        return result


def _publish_locked(store, broker, chat, journal_id, *, recovering):
    state = store.read(chat)
    entry, selected, baseline = _binding(state, journal_id)
    container = docker_manager._lookup_container(chat)
    paused = False
    if container is not None:
        paused = container.attrs.get("State", {}).get("Paused")
        if (container.status not in {"running", "paused", "exited"} or type(paused) is not bool
                or container.status == "paused" and not paused
                or container.status == "exited" and paused):
            raise SandboxStateError(f"sandbox does not admit publish: {container.status}")
    fence = _Fence(store, chat, container)
    held, stage = [], None
    replaced, result = False, None
    writer_excluded = container is None or container.status == "exited" or paused
    try:
        # Recorded paths remain obligations even if the current index moved them.
        if "target_path" in entry:
            if not writer_excluded:
                if not fence.acquire():
                    raise RecoveryRequiredError("publication recovery could not exclude the writer")
                writer_excluded = True
            fence.checkpoint()
            previous, absent = _prepared_parents(chat, workspace._parts(entry["target_path"]))
            try:
                stage = _Stage(store, chat, journal_id, entry)
                stage.cleanup(previous[-1] if absent is None else None, absent_parent=absent)
                stage.close()
                stage = None
                fence.checkpoint()
            finally:
                for fd in reversed(previous):
                    os.close(fd)
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
                live = working["journal"][journal_id]
                live.update(target_path=path, temporary_name=temporary)
                live.pop("staging", None)
            fence.checkpoint()
            entry = store.update(chat, prepare)["journal"][journal_id]
            try:
                held = _parents(chat, parts)
                target = os.lstat(parts[-1], dir_fd=held[-1])
                if not stat.S_ISREG(target.st_mode):
                    result = PublishResult("conflict", "baseline_mismatch")
            except OSError as exc:
                if exc.errno not in _PATH_ERRORS:
                    raise
                result = PublishResult("conflict", "path_missing" if exc.errno == errno.ENOENT else "baseline_mismatch")
            if result is None and not writer_excluded:
                if fence.acquire():
                    writer_excluded = True
                elif recovering:
                    raise RecoveryRequiredError("publication recovery could not exclude the writer")
                else:
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
            if (result is None and digest != baseline
                    and not (recovering and digest == selected["sha256"])):
                result = PublishResult("conflict", "baseline_mismatch")
            if result is None:
                content = versions.read_version_bytes(store, chat, selected["sha256"])
                fence.checkpoint()
                if len(content) != selected["size"]:
                    raise StateCorruptError("publish version size differs from its blob")
                already_replaced = recovering and digest == selected["sha256"]
                if not already_replaced:
                    stage = _Stage(store, chat, journal_id, entry)
                    fence.checkpoint()
                    owned = stage.allocate(journal_id, content, fence)
                    fence.checkpoint()
                    try:
                        _revalidate(chat, parts, held, _identity(target))
                        stage.expose(held[-1], fence)
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
                if result is None:
                    registered = broker.register_host_write(chat, path)
                    fence.checkpoint()
                    if (registered["file_id"] != entry["file_id"]
                            or registered["hash"] != selected["sha256"]
                            or registered["size"] != selected["size"]):
                        raise StateCorruptError("registered publish differs from its version")
                    result = PublishResult("published")
    except _BudgetExpired:
        result = PublishResult("interrupted" if replaced or recovering else "failed", "publish_timeout")
    finally:
        primary = sys.exc_info()[1]
        cleanup_error = None
        retirement_error = None
        try:
            if stage is not None and held and writer_excluded:
                persisted = store.read(chat)["journal"].get(journal_id)
                if persisted is not None:
                    stage.entry = persisted
                stage.cleanup(held[-1])
        except BaseException as exc:
            retirement_error = exc
        try:
            if stage is not None:
                stage.close()
        except OSError as exc:
            cleanup_error = cleanup_error or exc
        for fd in reversed(held):
            try:
                os.close(fd)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        fence.release(result.outcome if result is not None else "exception")
        if retirement_error is not None and primary is None:
            raise retirement_error
        if cleanup_error is not None:
            if primary is None and result is None:
                raise cleanup_error
            _record("Office publication workspace cleanup failed", chat_id=chat)
    if result.outcome == "interrupted":
        return result
    return _finish(store, chat, journal_id, result, entry, selected)
