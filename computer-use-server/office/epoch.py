# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Read the deployment restore-epoch marker without changing filesystem state."""
from __future__ import annotations

import os
import stat

import docker_manager

from .store import _CHUNK, _FILE_FLAGS

_MARKER = ".office-restore-epoch"


class RestoreEpochError(RuntimeError):
    """The restore-epoch marker exists but cannot be read as a regular UTF-8 token."""


def current_epoch() -> str | None:
    path = os.path.join(str(docker_manager.BASE_DATA_DIR), _MARKER)
    try:
        fd = os.open(path, _FILE_FLAGS)
    except FileNotFoundError:
        return None
    except OSError as extra:
        raise RestoreEpochError("restore epoch marker is unreadable") from extra
    try:
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise RestoreEpochError("restore epoch marker is not a regular file")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, _CHUNK)
                if not chunk:
                    break
                chunks.append(chunk)
            encoded = b"".join(chunks)
        except OSError as extra:
            raise RestoreEpochError("restore epoch marker is unreadable") from extra
    finally:
        os.close(fd)
    try:
        return encoded.decode("utf-8").strip()
    except UnicodeDecodeError as extra:
        raise RestoreEpochError("restore epoch marker is not valid UTF-8") from extra
