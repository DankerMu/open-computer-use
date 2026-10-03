# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Per-chat Office state file: locked read-modify-write of ``state.json``."""
from __future__ import annotations

import json
import os
import stat
import uuid
from typing import Any, Callable

import docker_manager

SCHEMA_VERSION = 1
_TOP_KEYS = ("schema_version", "documents", "sessions", "receipts", "journal")
_COLLECTIONS = ("documents", "sessions", "receipts", "journal")
_CHUNK = 1024 * 1024

_NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CLOSE_ON_EXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | _DIRECTORY | _NO_FOLLOW | _CLOSE_ON_EXEC
_DATA_ROOT_FLAGS = os.O_RDONLY | _DIRECTORY | _CLOSE_ON_EXEC
_FILE_FLAGS = os.O_RDONLY | _NO_FOLLOW | _NONBLOCK | _CLOSE_ON_EXEC
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NO_FOLLOW | _CLOSE_ON_EXEC


class StateCorruptError(RuntimeError):
    """Persisted Office state is unreadable, malformed, or unsupported."""


class StateDurabilityError(RuntimeError):
    """The atomic successor is visible but its containing directory was not synced."""


class OfficeStore:
    """Read and replace one chat's Office ``state.json`` under ``_combined_lock``."""

    def __init__(self) -> None:
        if not _NO_FOLLOW or not _DIRECTORY:
            raise RuntimeError("office store requires O_NOFOLLOW and O_DIRECTORY support")

    def read(self, chat_id: str) -> dict[str, Any]:
        chat = docker_manager.canonical_lock_chat_id(chat_id)
        self._assert_chat_root_safe(chat, allow_missing=True)
        with docker_manager._combined_lock(chat):
            self._assert_chat_root_safe(chat, allow_missing=False)
            return self._snapshot(self._load(chat, create=False))

    def update(self, chat_id: str, mutate: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
        chat = docker_manager.canonical_lock_chat_id(chat_id)
        self._assert_chat_root_safe(chat, allow_missing=True)
        with docker_manager._combined_lock(chat):
            self._assert_chat_root_safe(chat, allow_missing=False)
            working = self._snapshot(self._load(chat, create=False))
            returned = mutate(working)
            if returned is not None:
                raise ValueError("office state mutator must return None")
            encoded = self._encode(working)
            self._write_state(chat, encoded)
            return json.loads(encoded.decode("utf-8"))

    def _chat_root(self, chat: str) -> str:
        return os.path.join(str(docker_manager.BASE_DATA_DIR), chat)

    def _assert_chat_root_safe(self, chat: str, *, allow_missing: bool) -> None:
        root = self._chat_root(chat)
        try:
            info = os.lstat(root)
        except FileNotFoundError:
            if allow_missing:
                return
            raise StateCorruptError(f"chat control root is missing: {root}") from None
        except OSError as exc:
            raise StateCorruptError(f"cannot safely inspect chat control root: {root}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise StateCorruptError(f"chat control root is not a safe directory: {root}")

    def _load(self, chat: str, *, create: bool) -> dict[str, Any] | None:
        opened = self._open_tree(chat, create=create)
        if opened is None:
            return None
        base_fd, root_fd, ocu_fd, office_fd = opened
        try:
            try:
                info = os.lstat("state.json", dir_fd=office_fd)
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise StateCorruptError("office state is unreadable") from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise StateCorruptError("office state is not a regular file")
            try:
                state_fd = os.open("state.json", _FILE_FLAGS, dir_fd=office_fd)
            except OSError as extra:
                raise StateCorruptError("office state is unreadable") from extra
            try:
                try:
                    opened_info = os.fstat(state_fd)
                    if not stat.S_ISREG(opened_info.st_mode):
                        raise StateCorruptError("office state is not a regular file")
                    encoded = self._read_all(state_fd)
                except OSError as extra:
                    raise StateCorruptError("office state is unreadable") from extra
            finally:
                os.close(state_fd)
        finally:
            os.close(office_fd)
            os.close(ocu_fd)
            os.close(root_fd)
            os.close(base_fd)
        try:
            parsed = json.loads(encoded.decode("utf-8"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as extra:
            raise StateCorruptError("office state is not valid UTF-8 JSON") from extra
        try:
            self._validate(parsed)
        except ValueError as extra:
            raise StateCorruptError(str(extra)) from extra
        return parsed

    def _open_tree(self, chat: str, *, create: bool) -> tuple[int, int, int, int] | None:
        base = str(docker_manager.BASE_DATA_DIR)
        try:
            base_fd = os.open(base, _DATA_ROOT_FLAGS)
        except OSError as extra:
            raise StateCorruptError(f"cannot safely open data root: {base}") from extra
        root_fd = ocu_fd = office_fd = None
        try:
            root_fd = self._open_dir(chat, dir_fd=base_fd, create=False, label="chat control root")
            if root_fd is None:
                raise StateCorruptError(f"chat control root is missing: {self._chat_root(chat)}") from None
            ocu_fd = self._open_dir(
                ".ocu", dir_fd=root_fd, create=create, label="office control directory"
            )
            if ocu_fd is None:
                return None
            office_fd = self._open_dir(
                "office", dir_fd=ocu_fd, create=create, label="office state directory"
            )
            if office_fd is None:
                return None
            held = (base_fd, root_fd, ocu_fd, office_fd)
            base_fd = root_fd = ocu_fd = office_fd = None
            return held
        finally:
            for fd in (office_fd, ocu_fd, root_fd, base_fd):
                if fd is not None:
                    os.close(fd)

    def _open_dir(self, name: str, *, dir_fd: int, create: bool, label: str) -> int | None:
        try:
            info = os.lstat(name, dir_fd=dir_fd)
        except FileNotFoundError:
            if not create:
                return None
            try:
                os.mkdir(name, 0o700, dir_fd=dir_fd)
            except FileExistsError:
                pass
            try:
                info = os.lstat(name, dir_fd=dir_fd)
            except OSError as extra:
                raise StateCorruptError(f"{label} is not a safe directory") from extra
        except OSError as extra:
            raise StateCorruptError(f"{label} is not a safe directory") from extra
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise StateCorruptError(f"{label} is not a safe directory")
        try:
            return os.open(name, _DIRECTORY_FLAGS, dir_fd=dir_fd)
        except OSError as extra:
            raise StateCorruptError(f"{label} is not a safe directory") from extra

    def _write_state(self, chat: str, encoded: bytes) -> None:
        opened = self._open_tree(chat, create=True)
        assert opened is not None
        base_fd, root_fd, ocu_fd, office_fd = opened
        temporary = f".state.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        committed = False
        try:
            try:
                existing = os.lstat("state.json", dir_fd=office_fd)
            except FileNotFoundError:
                existing = None
            except OSError as extra:
                raise StateCorruptError("office state is unreadable") from extra
            if existing is not None and (stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)):
                raise StateCorruptError("office state is not a regular file")
            temporary_fd = os.open(temporary, _WRITE_FLAGS, 0o600, dir_fd=office_fd)
            try:
                self._write_all(temporary_fd, encoded)
                os.fsync(temporary_fd)
            finally:
                os.close(temporary_fd)
            os.fsync(base_fd)
            os.fsync(root_fd)
            os.fsync(ocu_fd)
            os.replace(temporary, "state.json", src_dir_fd=office_fd, dst_dir_fd=office_fd)
            committed = True
            try:
                os.fsync(office_fd)
            except OSError as extra:
                raise StateDurabilityError(
                    "office state replacement committed but directory durability sync failed"
                ) from extra
        except BaseException:
            if not committed:
                try:
                    os.unlink(temporary, dir_fd=office_fd)
                except FileNotFoundError:
                    pass
            raise
        finally:
            os.close(office_fd)
            os.close(ocu_fd)
            os.close(root_fd)
            os.close(base_fd)

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "documents": {},
            "sessions": {},
            "receipts": {},
            "journal": {},
        }

    def _snapshot(self, state: dict[str, Any] | None) -> dict[str, Any]:
        if state is None:
            return self._empty()
        return json.loads(self._encode(state).decode("utf-8"))

    def _encode(self, state: dict[str, Any]) -> bytes:
        self._validate(state)
        return json.dumps(
            state,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")

    @staticmethod
    def _validate(state: Any) -> None:
        if not isinstance(state, dict) or set(state) != set(_TOP_KEYS):
            raise ValueError("office state has an invalid schema")
        if type(state["schema_version"]) is not int or state["schema_version"] != SCHEMA_VERSION:
            raise ValueError("office state schema version is unsupported")
        if not all(isinstance(state[name], dict) for name in _COLLECTIONS):
            raise ValueError("office state collections are invalid")

    @staticmethod
    def _read_all(fd: int) -> bytes:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, _CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _write_all(fd: int, encoded: bytes) -> None:
        offset = 0
        while offset < len(encoded):
            written = os.write(fd, encoded[offset:])
            if written <= 0:
                raise OSError("short write while persisting office state")
            offset += written


def _reject_constant(token: str) -> None:
    raise ValueError(f"nonstandard JSON constant {token}")
