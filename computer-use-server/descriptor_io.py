# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""No-follow read acquisition without workspace or broker error policy."""
from __future__ import annotations

import errno
import os
import stat
from typing import NoReturn

NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0)
DIRECTORY = getattr(os, "O_DIRECTORY", 0)
CLOSE_ON_EXEC = getattr(os, "O_CLOEXEC", 0)
NONBLOCK = getattr(os, "O_NONBLOCK", 0)
DIRECTORY_FLAGS = os.O_RDONLY | DIRECTORY | NO_FOLLOW | CLOSE_ON_EXEC
FILE_FLAGS = os.O_RDONLY | NO_FOLLOW | NONBLOCK | CLOSE_ON_EXEC


class DescriptorOpenError(Exception):
    """Only os.open failed; callers translate the original OSError."""

    def __init__(self, error: OSError):
        self.error = error
        super().__init__(str(error))


class DescriptorTypeError(Exception):
    """An acquired descriptor did not have the requested filesystem type."""

    def __init__(self):
        self.cleanup_error: OSError | None = None
        super().__init__()

    def raise_as(self, error: BaseException) -> NoReturn:
        """Translate rejection before a deferred close error, preserving context.

        Strict cleanup failure supersedes the caller rejection, which remains
        its exception context instead of the neutral DescriptorTypeError.
        """
        try:
            raise error
        except BaseException:
            if self.cleanup_error is not None:
                raise self.cleanup_error
            raise


def is_missing(error: OSError) -> bool:
    return error.errno in (errno.ENOENT, errno.ENOTDIR)


def is_nofollow_error(error: OSError) -> bool:
    return error.errno in (errno.ELOOP, errno.EMLINK)


def _open_verified(
    path: str | os.PathLike[str], *, directory: bool, dir_fd: int | None,
    ignore_close_errors: bool,
) -> int:
    try:
        if dir_fd is None:
            fd = os.open(path, DIRECTORY_FLAGS if directory else FILE_FLAGS)
        else:
            fd = os.open(path, DIRECTORY_FLAGS if directory else FILE_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        raise DescriptorOpenError(exc) from exc
    try:
        mode = os.fstat(fd).st_mode
        if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
            raise DescriptorTypeError()
        return fd
    except BaseException as error:
        try:
            os.close(fd)
        except OSError as cleanup_error:
            if not ignore_close_errors:
                if isinstance(error, DescriptorTypeError):
                    error.cleanup_error = cleanup_error
                else:
                    raise
        raise


def open_directory(
    path: str | os.PathLike[str], *, dir_fd: int | None = None,
    ignore_close_errors: bool = False,
) -> int:
    """Return an owned verified directory; dir_fd remains borrowed.

    Open errors are neutral wrappers, type rejection is DescriptorTypeError,
    and fstat exceptions propagate unchanged. Every post-open failure closes
    the acquired fd; ignore_close_errors retains a caller's best-effort cleanup.
    """
    return _open_verified(path, directory=True, dir_fd=dir_fd,
                          ignore_close_errors=ignore_close_errors)


def open_regular(
    path: str | os.PathLike[str], *, dir_fd: int | None = None,
    ignore_close_errors: bool = False,
) -> int:
    """Return an owned verified regular file under the same failure contract."""
    return _open_verified(path, directory=False, dir_fd=dir_fd,
                          ignore_close_errors=ignore_close_errors)
