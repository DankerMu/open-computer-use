# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""No-follow workspace file reads for the Office store."""
from __future__ import annotations

import hashlib
import os
import stat
from typing import TYPE_CHECKING

import docker_manager

from .store import _DATA_ROOT_FLAGS, _DIRECTORY_FLAGS, _FILE_FLAGS, _CHUNK

if TYPE_CHECKING:
    from .store import OfficeStore


class UnsafePathError(RuntimeError):
    """Workspace path is unsafe and was not read."""

    reason = "unsafe_path"

    def __init__(self, message: str = "workspace path is unsafe") -> None:
        super().__init__(message)
        self.reason = "unsafe_path"


class FileTooLargeError(RuntimeError):
    """Workspace file exceeds the caller-supplied byte limit and was not fully read."""

    reason = "file_too_large"

    def __init__(self, message: str = "workspace file exceeds the size limit") -> None:
        super().__init__(message)
        self.reason = "file_too_large"


def read_workspace_file(
    store: OfficeStore,
    chat_id: str,
    relative_path: str,
    *,
    max_bytes: int | None = None,
) -> tuple[bytes, str]:
    chat = docker_manager.canonical_lock_chat_id(chat_id)
    path_parts = _parts(relative_path)
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0):
        raise ValueError("max_bytes must be a nonnegative integer")
    store._assert_chat_root_safe(chat, allow_missing=True)
    with docker_manager._combined_lock(chat):
        store._assert_chat_root_safe(chat, allow_missing=False)
        file_fd = _open_file(chat, path_parts)
        try:
            return _hash_regular(file_fd, "/".join(path_parts), max_bytes=max_bytes)
        finally:
            os.close(file_fd)


def _parts(relative_path: str) -> tuple[str, ...]:
    if not isinstance(relative_path, str) or not relative_path or "\x00" in relative_path:
        raise UnsafePathError("workspace path is unsafe")
    if relative_path.startswith("/") or relative_path.endswith("/") or "\\" in relative_path:
        raise UnsafePathError("workspace path is unsafe")
    split = tuple(relative_path.split("/"))
    if any(part in {"", ".", ".."} or part.startswith(".") for part in split):
        raise UnsafePathError("workspace path is unsafe")
    return split


def _open_file(chat: str, path_parts: tuple[str, ...]) -> int:
    base = str(docker_manager.BASE_DATA_DIR)
    try:
        base_fd = os.open(base, _DATA_ROOT_FLAGS)
    except OSError as extra:
        raise UnsafePathError("workspace path is unsafe") from extra
    root_fd = outputs_fd = None
    try:
        try:
            root_fd = os.open(chat, _DIRECTORY_FLAGS, dir_fd=base_fd)
        except OSError as extra:
            raise UnsafePathError("workspace path is unsafe") from extra
        if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
            raise UnsafePathError("workspace path is unsafe")
        try:
            info = os.lstat("outputs", dir_fd=root_fd)
        except OSError as extra:
            raise UnsafePathError("workspace path is unsafe") from extra
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise UnsafePathError("workspace path is unsafe")
        try:
            outputs_fd = os.open("outputs", _DIRECTORY_FLAGS, dir_fd=root_fd)
        except OSError as extra:
            raise UnsafePathError("workspace path is unsafe") from extra
        if not stat.S_ISDIR(os.fstat(outputs_fd).st_mode):
            raise UnsafePathError("workspace path is unsafe")
        return _open_regular_relative(outputs_fd, path_parts)
    finally:
        if outputs_fd is not None:
            os.close(outputs_fd)
        if root_fd is not None:
            os.close(root_fd)
        os.close(base_fd)


def _open_regular_relative(root_fd: int, path_parts: tuple[str, ...]) -> int:
    parent_fd = os.dup(root_fd)
    try:
        for component in path_parts[:-1]:
            try:
                info = os.lstat(component, dir_fd=parent_fd)
            except OSError as extra:
                raise UnsafePathError("workspace path is unsafe") from extra
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise UnsafePathError("workspace path is unsafe")
            try:
                next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            except OSError as extra:
                raise UnsafePathError("workspace path is unsafe") from extra
            os.close(parent_fd)
            parent_fd = next_fd
        name = path_parts[-1]
        try:
            info = os.lstat(name, dir_fd=parent_fd)
        except OSError as extra:
            raise UnsafePathError("workspace path is unsafe") from extra
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise UnsafePathError("workspace path is unsafe")
        try:
            file_fd = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
        except OSError as extra:
            raise UnsafePathError("workspace path is unsafe") from extra
        try:
            opened = os.fstat(file_fd)
            if not stat.S_ISREG(opened.st_mode):
                raise UnsafePathError("workspace path is unsafe")
            owned = file_fd
            file_fd = -1
            return owned
        finally:
            if file_fd >= 0:
                os.close(file_fd)
    finally:
        os.close(parent_fd)


def _hash_regular(
    file_fd: int, label: str, *, max_bytes: int | None = None
) -> tuple[bytes, str]:
    try:
        before = os.fstat(file_fd)
    except OSError as extra:
        raise UnsafePathError(f"workspace file is unreadable: {label}") from extra
    if not stat.S_ISREG(before.st_mode):
        raise UnsafePathError("workspace path is unsafe")
    if max_bytes is not None and before.st_size > max_bytes:
        raise FileTooLargeError("workspace file exceeds the size limit")
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    remaining = None if max_bytes is None else max_bytes + 1
    try:
        while remaining is None or remaining > 0:
            chunk_size = _CHUNK if remaining is None else min(_CHUNK, remaining)
            chunk = os.read(file_fd, chunk_size)
            if not chunk:
                break
            chunks.append(chunk)
            digest.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
        after = os.fstat(file_fd)
    except OSError as extra:
        raise UnsafePathError(f"workspace file is unreadable: {label}") from extra
    body = b"".join(chunks)
    if max_bytes is not None and len(body) > max_bytes:
        raise FileTooLargeError("workspace file exceeds the size limit")
    if (
        not stat.S_ISREG(after.st_mode)
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_ino != before.st_ino
        or after.st_dev != before.st_dev
        or len(body) != before.st_size
    ):
        raise UnsafePathError(f"workspace file changed while reading: {label}")
    return body, digest.hexdigest()
