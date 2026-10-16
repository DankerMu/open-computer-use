# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Serialize callback-bearing observations through their canonical admission."""
from __future__ import annotations

import errno
import fcntl
import os
import stat
from contextlib import ExitStack, contextmanager

import descriptor_io
import docker_manager

from .store import StateCorruptError

_LOCK_NAME = ".office-lookup.lock"


def _identity(info):
    return info.st_dev, info.st_ino


def _acquire(owned, chat, create):
    base = docker_manager.BASE_DATA_DIR
    if create:
        base.mkdir(parents=True, exist_ok=True)
    try:
        # The configured data root may itself be a trusted deployment symlink.
        base_fd = os.open(base, descriptor_io.DIRECTORY_FLAGS & ~descriptor_io.NO_FOLLOW)
    except FileNotFoundError:
        if not create:
            return None
        raise
    owned.callback(os.close, base_fd)
    if not stat.S_ISDIR(os.fstat(base_fd).st_mode):
        raise StateCorruptError("office lookup data root is not a directory")
    if create:
        try:
            os.mkdir(chat, dir_fd=base_fd)
        except FileExistsError:
            pass
    try:
        root_fd = descriptor_io.open_directory(chat, dir_fd=base_fd)
    except descriptor_io.DescriptorOpenError as exc:
        if not create and exc.error.errno == errno.ENOENT:
            return None
        raise
    owned.callback(os.close, root_fd)
    if create:
        os.fsync(base_fd)
    flags = (os.O_RDWR | os.O_CREAT | descriptor_io.NO_FOLLOW
             | descriptor_io.NONBLOCK | descriptor_io.CLOSE_ON_EXEC)
    lock_fd = os.open(_LOCK_NAME, flags, 0o666, dir_fd=root_fd)
    owned.callback(os.close, lock_fd)
    lock_info = os.fstat(lock_fd)
    if not stat.S_ISREG(lock_info.st_mode):
        raise StateCorruptError("office lookup lock is not a regular file")
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    owned.callback(fcntl.flock, lock_fd, fcntl.LOCK_UN)
    # A waiter must not admit on a detached root or a retired lock inode.
    base_info = os.stat(base)
    root_info = os.stat(chat, dir_fd=base_fd, follow_symlinks=False)
    current_lock = os.stat(_LOCK_NAME, dir_fd=root_fd, follow_symlinks=False)
    if (
        not stat.S_ISDIR(base_info.st_mode)
        or _identity(base_info) != _identity(os.fstat(base_fd))
        or not stat.S_ISDIR(root_info.st_mode)
        or _identity(root_info) != _identity(os.fstat(root_fd))
        or not stat.S_ISREG(current_lock.st_mode)
        or _identity(current_lock) != _identity(lock_info)
    ):
        raise StateCorruptError("office lookup root or lock identity changed")
    return lock_fd


@contextmanager
def lookup_admission(chat_id: str, *, create: bool = False):
    """Acquire before canonical locking; callbacks never acquire this gate.

    Each call opens its own flock descriptor, coordinating threads and processes
    without a process-local cache. Missing noncreating roots yield None; unsafe
    roots/locks refuse admission. The stable lock file is never unlinked here.
    """
    chat = docker_manager.canonical_lock_chat_id(chat_id)
    if not descriptor_io.NO_FOLLOW or not descriptor_io.DIRECTORY:
        raise RuntimeError("office lookup admission requires no-follow directories")
    with ExitStack() as owned:
        try:
            acquired = _acquire(owned, chat, create)
        except descriptor_io.DescriptorOpenError as exc:
            raise StateCorruptError("cannot safely open office lookup root") from exc.error
        except descriptor_io.DescriptorTypeError as exc:
            exc.raise_as(StateCorruptError("office lookup root is not a directory"))
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise
            raise StateCorruptError("cannot safely acquire office lookup admission") from exc
        yield None if acquired is None else True
