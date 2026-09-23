# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Persisted, filesystem-confined output identity reconciliation.

The broker owns a per-chat monotonic revision and stable UUID identities.  It
uses the lifecycle module's canonical RLock+flock transaction, but never asks
Docker for a client or creates/resumes a sandbox.  Polling cannot detect a
same-size in-place edit or a delete/recreate completed between observations;
the cached hash from that stale observation can consequently miss a later
rename's continuity too.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import docker_manager

SCHEMA_VERSION = 1
DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 1_000
MAX_ACTIVE_FILES = 10_000
MAX_FILE_SIZE = 100 * 1024 * 1024
MAX_INDEX_SIZE = 64 * 1024 * 1024
HASH_CHUNK_SIZE = 1024 * 1024

_NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CLOSE_ON_EXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | _DIRECTORY | _NO_FOLLOW | _CLOSE_ON_EXEC
_FILE_FLAGS = os.O_RDONLY | _NO_FOLLOW | _NONBLOCK | _CLOSE_ON_EXEC
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NO_FOLLOW | _CLOSE_ON_EXEC
_CURSOR = re.compile(r"(0|[1-9][0-9]*):(0|[1-9][0-9]*)\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class OutputsBrokerError(RuntimeError):
    """Base failure for the pure outputs broker."""


class CorruptIndexError(OutputsBrokerError):
    """A persisted index is malformed or violates its schema."""


class LimitExceededError(OutputsBrokerError):
    """A configured filesystem, page, or index bound was exceeded."""


class CursorError(OutputsBrokerError):
    """A listing cursor is malformed or outside the current listing."""


class StaleCursorError(CursorError):
    """A listing cursor belongs to an older listing revision."""


class UnsafePathError(OutputsBrokerError):
    """A symlink or non-directory control/output path was encountered."""


class UnstableReadError(OutputsBrokerError):
    """A file or directory changed while it was being safely observed."""


class CommitDurabilityError(OutputsBrokerError):
    """The atomic successor is visible but its containing directory was not synced."""


class UnsupportedNameError(OutputsBrokerError):
    """A live filesystem name cannot be persisted as a relative POSIX path."""


@dataclass(frozen=True)
class _Observation:
    path: str
    name: str
    size: int
    mtime_ns: int
    signature: tuple[int, int, int, int, int, int]


class OutputsBroker:
    """Reconcile regular files below one chat's ``outputs`` directory.

    Limits can only be lowered from the approved ceilings.  This keeps later
    endpoint configuration from silently widening the resource contract.
    """

    def __init__(
        self,
        *,
        max_page_limit: int = MAX_PAGE_LIMIT,
        max_active_files: int = MAX_ACTIVE_FILES,
        max_file_size: int = MAX_FILE_SIZE,
        max_index_size: int = MAX_INDEX_SIZE,
    ) -> None:
        self.max_page_limit = self._positive("max_page_limit", max_page_limit, MAX_PAGE_LIMIT)
        self.max_active_files = self._positive("max_active_files", max_active_files, MAX_ACTIVE_FILES)
        self.max_file_size = self._positive("max_file_size", max_file_size, MAX_FILE_SIZE)
        self.max_index_size = self._positive("max_index_size", max_index_size, MAX_INDEX_SIZE)
        if not _NO_FOLLOW or not _DIRECTORY or not _NONBLOCK:
            raise RuntimeError("outputs broker requires O_NOFOLLOW, O_DIRECTORY and O_NONBLOCK support")

    @staticmethod
    def _positive(name: str, value: int, ceiling: int) -> int:
        if type(value) is not int or value <= 0 or value > ceiling:
            raise ValueError(f"{name} must be a positive integer no greater than {ceiling}")
        return value

    def current_revision(self, chat_id: str) -> int:
        """Read and validate the persisted counter without creating an index."""
        chat = self._canonical_chat(chat_id)
        self._assert_chat_root_safe(chat, allow_missing=True)
        with docker_manager._combined_lock(chat):
            self._assert_chat_root_safe(chat, allow_missing=False)
            index = self._read_index(chat)
            return 0 if index is None else index["counter"]

    def reconcile(self, chat_id: str, *, cursor: str | None = None, limit: int = DEFAULT_PAGE_LIMIT) -> dict[str, Any]:
        """Safely reconcile one chat and return a revision-bound sorted page."""
        page_limit = self._page_limit(limit)
        chat = self._canonical_chat(chat_id)
        self._assert_chat_root_safe(chat, allow_missing=True)
        with docker_manager._combined_lock(chat):
            self._assert_chat_root_safe(chat, allow_missing=False)
            index = self._read_index(chat)
            index_missing = index is None
            if index is None:
                index = self._empty_index()

            observations = self._scan_metadata(chat, has_active=bool(index["active"]))
            successor, changed = self._reconcile_index(chat, index, observations)
            if index_missing or changed:
                self._write_index(chat, successor)

            paths = sorted(successor["active"])
            return self._page(successor, observations, paths, successor["counter"], cursor, page_limit, unchanged=not changed)

    @staticmethod
    def _canonical_chat(chat_id: str) -> str:
        return docker_manager.canonical_lock_chat_id(chat_id)

    @staticmethod
    def _empty_index() -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "counter": 0,
            "active": {},
            "fingerprints": {},
            "tombstones": {},
        }

    @staticmethod
    def _signature(file_stat: os.stat_result) -> tuple[int, int, int, int, int, int]:
        return (
            file_stat.st_dev,
            file_stat.st_ino,
            file_stat.st_mode,
            file_stat.st_size,
            file_stat.st_mtime_ns,
            file_stat.st_ctime_ns,
        )

    @staticmethod
    def _is_missing(error: OSError) -> bool:
        return error.errno in (errno.ENOENT, errno.ENOTDIR)

    @staticmethod
    def _is_nofollow_error(error: OSError) -> bool:
        return error.errno in (errno.ELOOP, errno.EMLINK)

    def _chat_root(self, chat: str) -> Path:
        return Path(docker_manager.BASE_DATA_DIR) / chat

    def _assert_chat_root_safe(self, chat: str, *, allow_missing: bool) -> None:
        root = self._chat_root(chat)
        try:
            root_stat = os.lstat(root)
        except FileNotFoundError:
            if allow_missing:
                return
            raise UnsafePathError(f"chat control root is missing: {root}")
        if stat.S_ISLNK(root_stat.st_mode):
            raise UnsafePathError(f"chat control root is a symlink: {root}")
        if not stat.S_ISDIR(root_stat.st_mode):
            raise UnsafePathError(f"chat control root is not a directory: {root}")

    def _control_fd(self, chat: str, *, create: bool) -> int | None:
        control = self._chat_root(chat) / ".ocu"
        try:
            control_stat = os.lstat(control)
        except FileNotFoundError:
            if not create:
                return None
            try:
                os.mkdir(control, 0o700)
            except FileExistsError:
                pass
            control_stat = os.lstat(control)
        if stat.S_ISLNK(control_stat.st_mode):
            raise UnsafePathError(f"broker control directory is a symlink: {control}")
        if not stat.S_ISDIR(control_stat.st_mode):
            raise UnsafePathError(f"broker control path is not a directory: {control}")
        try:
            return os.open(control, _DIRECTORY_FLAGS)
        except OSError as exc:
            if self._is_nofollow_error(exc):
                raise UnsafePathError(f"broker control directory became a symlink: {control}") from exc
            raise UnstableReadError(f"cannot safely open broker control directory: {control}") from exc

    def _read_index(self, chat: str) -> dict[str, Any] | None:
        control_fd = self._control_fd(chat, create=False)
        if control_fd is None:
            return None
        try:
            try:
                index_stat = os.lstat("index.json", dir_fd=control_fd)
            except FileNotFoundError:
                return None
            if stat.S_ISLNK(index_stat.st_mode):
                raise UnsafePathError("broker index is a symlink")
            if not stat.S_ISREG(index_stat.st_mode):
                raise UnsafePathError("broker index is not a regular file")
            if index_stat.st_size > self.max_index_size:
                raise LimitExceededError("persisted broker index exceeds configured size limit")
            try:
                index_fd = self._open_regular("index.json", dir_fd=control_fd, label="broker index")
            except OSError as exc:
                if self._is_nofollow_error(exc):
                    raise UnsafePathError("broker index became a symlink") from exc
                if self._is_missing(exc):
                    raise UnstableReadError("broker index disappeared while opening") from exc
                raise UnstableReadError("cannot safely open broker index") from exc
            try:
                opened = os.fstat(index_fd)
                if not stat.S_ISREG(opened.st_mode):
                    raise UnsafePathError("broker index is not a regular file")
                if opened.st_size > self.max_index_size:
                    raise LimitExceededError("persisted broker index exceeds configured size limit")
                encoded = self._read_bounded(index_fd, self.max_index_size)
                closed = os.fstat(index_fd)
            finally:
                os.close(index_fd)
            if self._signature(opened) != self._signature(closed):
                raise CorruptIndexError("broker index changed while being read")
        finally:
            os.close(control_fd)

        try:
            decoded = encoded.decode("utf-8")
            parsed = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CorruptIndexError("broker index is not valid UTF-8 JSON") from exc
        self._validate_index(parsed)
        return parsed

    def _read_bounded(self, fd: int, maximum: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(HASH_CHUNK_SIZE, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise LimitExceededError("broker index exceeds configured size limit")
        return b"".join(chunks)

    def _validate_index(self, index: Any) -> None:
        if not isinstance(index, dict) or set(index) != {
            "schema_version", "counter", "active", "fingerprints", "tombstones"
        }:
            raise CorruptIndexError("broker index has an invalid schema")
        if type(index["schema_version"]) is not int or index["schema_version"] != SCHEMA_VERSION:
            raise CorruptIndexError("broker index schema version is unsupported")
        counter = index["counter"]
        if type(counter) is not int or counter < 0:
            raise CorruptIndexError("broker counter is invalid")
        active = index["active"]
        fingerprints = index["fingerprints"]
        tombstones = index["tombstones"]
        if not all(isinstance(value, dict) for value in (active, fingerprints, tombstones)):
            raise CorruptIndexError("broker index collections are invalid")
        if len(active) > self.max_active_files:
            raise LimitExceededError("persisted active file count exceeds configured limit")

        live_ids: set[str] = set()
        expected_fingerprints: dict[str, list[str]] = {}
        for path, entry in active.items():
            self._validate_entry(entry, path, counter, "active")
            file_id = entry["file_id"]
            if file_id in live_ids:
                raise CorruptIndexError("broker active entries reuse a file_id")
            live_ids.add(file_id)
            fingerprint = self._fingerprint(entry)
            expected_fingerprints.setdefault(fingerprint, []).append(file_id)
        for file_ids in expected_fingerprints.values():
            file_ids.sort()
        if fingerprints != expected_fingerprints:
            raise CorruptIndexError("broker fingerprint index does not match active entries")

        tombstone_ids: set[str] = set()
        for file_id, entry in tombstones.items():
            if not isinstance(entry, dict) or type(file_id) is not str or entry.get("file_id") != file_id:
                raise CorruptIndexError("broker tombstone key does not match its file_id")
            self._validate_entry(entry, entry.get("path"), counter, "tombstone")
            if file_id in live_ids or file_id in tombstone_ids:
                raise CorruptIndexError("broker tombstone reuses a live or duplicate file_id")
            tombstone_ids.add(file_id)

    def _validate_entry(self, entry: Any, path: Any, counter: int, collection: str) -> None:
        required = {"file_id", "path", "name", "size", "mtime_ns", "revision", "hash"}
        if not isinstance(entry, dict) or set(entry) != required:
            raise CorruptIndexError(f"broker {collection} entry has an invalid schema")
        if type(path) is not str or entry["path"] != path or not self._valid_relative_path(path):
            raise CorruptIndexError(f"broker {collection} path is invalid")
        if entry["name"] != path.rsplit("/", 1)[-1]:
            raise CorruptIndexError(f"broker {collection} name does not match path")
        if type(entry["size"]) is not int or entry["size"] < 0 or entry["size"] > self.max_file_size:
            raise CorruptIndexError(f"broker {collection} size is invalid")
        if type(entry["mtime_ns"]) is not int:
            raise CorruptIndexError(f"broker {collection} mtime_ns is invalid")
        revision = entry["revision"]
        if type(revision) is not int or revision <= 0 or revision > counter:
            raise CorruptIndexError(f"broker {collection} revision is invalid")
        if type(entry["hash"]) is not str or not _HASH.fullmatch(entry["hash"]):
            raise CorruptIndexError(f"broker {collection} hash is invalid")
        if type(entry["file_id"]) is not str:
            raise CorruptIndexError(f"broker {collection} file_id is invalid")
        try:
            parsed = uuid.UUID(entry["file_id"])
        except (ValueError, AttributeError) as exc:
            raise CorruptIndexError(f"broker {collection} file_id is invalid") from exc
        if parsed.version != 4 or str(parsed) != entry["file_id"]:
            raise CorruptIndexError(f"broker {collection} file_id is invalid")

    @staticmethod
    def _valid_relative_path(path: str) -> bool:
        if not path or path.startswith("/") or "\\" in path or "\x00" in path:
            return False
        try:
            path.encode("utf-8")
        except UnicodeEncodeError:
            return False
        pieces = path.split("/")
        return all(piece and piece not in (".", "..") and not piece.startswith(".") for piece in pieces)

    @classmethod
    def _require_representable_name(cls, name: str, relative_path: str) -> None:
        if "\x00" in name or "\\" in name:
            raise UnsupportedNameError(
                f"unsupported output name {relative_path.encode('utf-8', 'backslashreplace')!r}"
            )
        try:
            name.encode("utf-8")
            relative_path.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise UnsupportedNameError(
                f"unsupported output name {relative_path.encode('utf-8', 'backslashreplace')!r}"
            ) from exc
        if not cls._valid_relative_path(relative_path):
            raise UnsupportedNameError(
                f"unsupported output name {relative_path.encode('utf-8', 'backslashreplace')!r}"
            )

    @staticmethod
    def _fingerprint(entry: dict[str, Any]) -> str:
        return f"{entry['size']}:{entry['hash']}"

    def _scan_metadata(self, chat: str, *, has_active: bool) -> dict[str, _Observation]:
        output_root = self._chat_root(chat) / "outputs"
        try:
            output_stat = os.lstat(output_root)
        except FileNotFoundError:
            if has_active:
                raise UnstableReadError(f"outputs root disappeared while active entries exist: {output_root}")
            return {}
        if stat.S_ISLNK(output_stat.st_mode):
            raise UnsafePathError(f"outputs root is a symlink: {output_root}")
        if not stat.S_ISDIR(output_stat.st_mode):
            raise UnsafePathError(f"outputs root is not a directory: {output_root}")
        root_fd = self._open_directory_path(output_root, "outputs root")
        observations: dict[str, _Observation] = {}
        try:
            self._scan_tree(root_fd, observations)
        finally:
            os.close(root_fd)
        return observations

    def _open_directory_path(self, path: Path, label: str) -> int:
        try:
            directory_fd = os.open(path, _DIRECTORY_FLAGS)
        except OSError as exc:
            if self._is_nofollow_error(exc):
                raise UnsafePathError(f"{label} is a symlink: {path}") from exc
            if self._is_missing(exc):
                raise UnstableReadError(f"{label} disappeared while opening: {path}") from exc
            raise UnstableReadError(f"cannot safely open {label}: {path}") from exc
        if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
            os.close(directory_fd)
            raise UnsafePathError(f"{label} is not a directory: {path}")
        return directory_fd

    def _open_child_directory(self, parent_fd: int, name: str) -> int:
        try:
            child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            if self._is_nofollow_error(exc) or self._is_missing(exc):
                raise UnstableReadError(f"output directory changed while traversing: {name}") from exc
            raise UnstableReadError(f"cannot safely traverse output directory: {name}") from exc
        if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
            os.close(child_fd)
            raise UnstableReadError(f"output directory changed while traversing: {name}")
        return child_fd

    def _scan_tree(self, root_fd: int, observations: dict[str, _Observation]) -> None:
        stack: list[tuple[int, tuple[str, ...], bool]] = [(root_fd, (), False)]
        try:
            while stack:
                directory_fd, prefix, owned = stack.pop()
                try:
                    with os.scandir(directory_fd) as listing:
                        children = []
                        for entry in listing:
                            name = entry.name
                            if name.startswith("."):
                                continue
                            relative_path = "/".join((*prefix, name))
                            self._require_representable_name(name, relative_path)
                            try:
                                item_stat = entry.stat(follow_symlinks=False)
                            except OSError as exc:
                                if self._is_missing(exc):
                                    raise UnstableReadError(f"output disappeared while enumerating: {relative_path}") from exc
                                raise UnstableReadError(f"cannot inspect output entry: {relative_path}") from exc
                            mode = item_stat.st_mode
                            if stat.S_ISLNK(mode):
                                continue
                            if stat.S_ISDIR(mode):
                                children.append((name, relative_path, True, item_stat))
                                continue
                            if not stat.S_ISREG(mode):
                                continue
                            if item_stat.st_size > self.max_file_size:
                                raise LimitExceededError(f"output file exceeds configured size limit: {relative_path}")
                            observations[relative_path] = _Observation(
                                path=relative_path,
                                name=name,
                                size=item_stat.st_size,
                                mtime_ns=item_stat.st_mtime_ns,
                                signature=self._signature(item_stat),
                            )
                            if len(observations) > self.max_active_files:
                                raise LimitExceededError("output file count exceeds configured active-file limit")
                        for name, relative_path, _is_dir, _stat in reversed(children):
                            child_fd = self._open_child_directory(directory_fd, name)
                            stack.append((child_fd, (*prefix, name), True))
                except OSError as exc:
                    raise UnstableReadError("outputs directory changed while enumerating") from exc
                finally:
                    if owned:
                        os.close(directory_fd)
        except BaseException:
            while stack:
                directory_fd, _prefix, owned = stack.pop()
                if owned:
                    os.close(directory_fd)
            raise

    def _hash_observation(self, chat: str, observation: _Observation) -> str:
        output_root = self._chat_root(chat) / "outputs"
        root_fd = self._open_directory_path(output_root, "outputs root")
        parent_fd = root_fd
        try:
            components = observation.path.split("/")
            for component in components[:-1]:
                next_fd = self._open_child_directory(parent_fd, component)
                if parent_fd != root_fd:
                    os.close(parent_fd)
                parent_fd = next_fd
            try:
                file_fd = self._open_regular(components[-1], dir_fd=parent_fd, label=observation.path)
            except OSError as exc:
                if self._is_nofollow_error(exc) or self._is_missing(exc) or exc.errno in (errno.ENXIO, errno.EAGAIN, errno.EWOULDBLOCK):
                    raise UnstableReadError(f"output changed before safe read: {observation.path}") from exc
                raise UnstableReadError(f"cannot safely open output: {observation.path}") from exc
            try:
                before = os.fstat(file_fd)
                if not stat.S_ISREG(before.st_mode) or self._signature(before) != observation.signature:
                    raise UnstableReadError(f"output changed before hashing: {observation.path}")
                digest = hashlib.sha256()
                bytes_read = 0
                while True:
                    chunk = os.read(file_fd, HASH_CHUNK_SIZE)
                    if not chunk:
                        break
                    bytes_read += len(chunk)
                    if bytes_read > self.max_file_size:
                        raise LimitExceededError(f"output file exceeds configured size limit: {observation.path}")
                    digest.update(chunk)
                after = os.fstat(file_fd)
            finally:
                os.close(file_fd)
        finally:
            if parent_fd != root_fd:
                os.close(parent_fd)
            os.close(root_fd)
        if (
            bytes_read != observation.size
            or self._signature(before) != observation.signature
            or self._signature(after) != observation.signature
        ):
            raise UnstableReadError(f"output changed while hashing: {observation.path}")
        return digest.hexdigest()

    def _reconcile_index(
        self,
        chat: str,
        index: dict[str, Any],
        observations: dict[str, _Observation],
    ) -> tuple[dict[str, Any], bool]:
        active = {path: dict(entry) for path, entry in index["active"].items()}
        tombstones = {file_id: dict(entry) for file_id, entry in index["tombstones"].items()}
        current_paths = set(observations)
        active_paths = set(active)
        size_changes = sorted(
            path for path in active_paths & current_paths if active[path]["size"] != observations[path].size
        )
        removals = sorted(active_paths - current_paths)
        additions = sorted(current_paths - active_paths)

        refreshed_hashes = {
            path: self._hash_observation(chat, observations[path])
            for path in [*size_changes, *additions]
        }
        counter = index["counter"]
        changed = False

        def event() -> int:
            nonlocal counter, changed
            counter += 1
            changed = True
            return counter

        for path in size_changes:
            observation = observations[path]
            entry = active[path]
            entry.update(
                {
                    "name": observation.name,
                    "size": observation.size,
                    "mtime_ns": observation.mtime_ns,
                    "revision": event(),
                    "hash": refreshed_hashes[path],
                }
            )

        removed_by_fingerprint: dict[str, list[str]] = {}
        added_by_fingerprint: dict[str, list[str]] = {}
        for path in removals:
            removed_by_fingerprint.setdefault(self._fingerprint(active[path]), []).append(path)
        for path in additions:
            observation = observations[path]
            fingerprint = f"{observation.size}:{refreshed_hashes[path]}"
            added_by_fingerprint.setdefault(fingerprint, []).append(path)

        matched_removals: set[str] = set()
        matched_additions: set[str] = set()
        for fingerprint in sorted(set(removed_by_fingerprint) & set(added_by_fingerprint)):
            for old_path, new_path in zip(
                sorted(removed_by_fingerprint[fingerprint]),
                sorted(added_by_fingerprint[fingerprint]),
            ):
                old_entry = active.pop(old_path)
                observation = observations[new_path]
                active[new_path] = {
                    "file_id": old_entry["file_id"],
                    "path": new_path,
                    "name": observation.name,
                    "size": observation.size,
                    "mtime_ns": observation.mtime_ns,
                    "revision": event(),
                    "hash": refreshed_hashes[new_path],
                }
                matched_removals.add(old_path)
                matched_additions.add(new_path)

        for path in removals:
            if path in matched_removals:
                continue
            removed = active.pop(path)
            tombstone = dict(removed)
            tombstone["revision"] = event()
            tombstones[tombstone["file_id"]] = tombstone

        for path in additions:
            if path in matched_additions:
                continue
            observation = observations[path]
            active[path] = {
                "file_id": str(uuid.uuid4()),
                "path": path,
                "name": observation.name,
                "size": observation.size,
                "mtime_ns": observation.mtime_ns,
                "revision": event(),
                "hash": refreshed_hashes[path],
            }

        successor = {
            "schema_version": SCHEMA_VERSION,
            "counter": counter,
            "active": active,
            "fingerprints": self._fingerprints(active),
            "tombstones": tombstones,
        }
        self._validate_index(successor)
        return successor, changed

    @staticmethod
    def _fingerprints(active: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
        fingerprints: dict[str, list[str]] = {}
        for entry in active.values():
            fingerprint = f"{entry['size']}:{entry['hash']}"
            fingerprints.setdefault(fingerprint, []).append(entry["file_id"])
        for file_ids in fingerprints.values():
            file_ids.sort()
        return fingerprints

    @staticmethod
    def _listing_entry(entry: dict[str, Any], observation: _Observation) -> dict[str, Any]:
        return {
            "file_id": entry["file_id"],
            "path": entry["path"],
            "name": entry["name"],
            "size": entry["size"],
            "mtime_ns": observation.mtime_ns,
            "revision": entry["revision"],
            "hash": entry["hash"],
        }

    def _write_index(self, chat: str, index: dict[str, Any]) -> None:
        encoded = json.dumps(index, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        if len(encoded) > self.max_index_size:
            raise LimitExceededError("proposed broker index exceeds configured size limit")
        control_fd = self._control_fd(chat, create=True)
        assert control_fd is not None
        temporary = f".index.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        committed = False
        try:
            try:
                existing = os.lstat("index.json", dir_fd=control_fd)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if stat.S_ISLNK(existing.st_mode):
                    raise UnsafePathError("broker index is a symlink")
                if not stat.S_ISREG(existing.st_mode):
                    raise UnsafePathError("broker index is not a regular file")

            temporary_fd = os.open(temporary, _WRITE_FLAGS, 0o600, dir_fd=control_fd)
            try:
                self._write_all(temporary_fd, encoded)
                os.fsync(temporary_fd)
            finally:
                os.close(temporary_fd)
            os.replace(temporary, "index.json", src_dir_fd=control_fd, dst_dir_fd=control_fd)
            committed = True
            try:
                os.fsync(control_fd)
            except OSError as exc:
                raise CommitDurabilityError(
                    "broker index replacement committed but directory durability sync failed"
                ) from exc
        except BaseException:
            if not committed:
                try:
                    os.unlink(temporary, dir_fd=control_fd)
                except FileNotFoundError:
                    pass
            raise
        finally:
            os.close(control_fd)

    @staticmethod
    def _write_all(fd: int, encoded: bytes) -> None:
        offset = 0
        while offset < len(encoded):
            written = os.write(fd, encoded[offset:])
            if written <= 0:
                raise OSError("short write while persisting broker index")
            offset += written

    def _page(
        self,
        index: dict[str, Any],
        observations: dict[str, _Observation],
        paths: list[str],
        revision: int,
        cursor: str | None,
        limit: int,
        *,
        unchanged: bool,
    ) -> dict[str, Any]:
        total = len(paths)
        offset = 0
        if cursor is not None:
            if type(cursor) is not str:
                raise CursorError("cursor must be a revision:offset string")
            parsed = _CURSOR.fullmatch(cursor)
            if parsed is None:
                raise CursorError("cursor must be a revision:offset string")
            try:
                cursor_revision = int(parsed.group(1))
                offset = int(parsed.group(2))
            except ValueError as exc:
                raise CursorError("cursor must be a revision:offset string") from exc
            if cursor_revision != revision:
                raise StaleCursorError("cursor belongs to a stale listing revision")
            if offset >= total:
                raise CursorError("cursor offset is outside the current listing")
        end = min(offset + limit, total)
        return {
            "revision": revision,
            "entries": [
                self._listing_entry(index["active"][path], observations[path])
                for path in paths[offset:end]
            ],
            "total": total,
            "next_cursor": f"{revision}:{end}" if end < total else None,
            "unchanged": unchanged,
        }

    def _page_limit(self, limit: int) -> int:
        if type(limit) is not int or limit <= 0 or limit > self.max_page_limit:
            raise LimitExceededError(f"limit must be a positive integer no greater than {self.max_page_limit}")
        return limit

    def _open_regular(self, name: str, *, dir_fd: int, label: str) -> int:
        try:
            file_fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno in (errno.ENXIO, errno.EAGAIN, errno.EWOULDBLOCK):
                raise UnstableReadError(f"{label} is not a stable regular file") from exc
            raise
        try:
            opened = os.fstat(file_fd)
            if not stat.S_ISREG(opened.st_mode):
                raise UnstableReadError(f"{label} is not a stable regular file")
            return file_fd
        except BaseException:
            os.close(file_fd)
            raise
