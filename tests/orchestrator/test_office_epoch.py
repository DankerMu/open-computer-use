# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Public-seam tests for the restore-epoch marker reader."""
from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = ROOT / "computer-use-server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import docker_manager
import office.epoch as epoch_mod

MARKER = ".office-restore-epoch"
NO_DOCKER_SOCKET = "unix:///tmp/ocu-acceptance-no-docker.sock"


@pytest.fixture
def epoch_world(monkeypatch, tmp_path):
    data = tmp_path / "data"
    monkeypatch.setenv("BASE_DATA_DIR", str(data))
    monkeypatch.setenv("DOCKER_HOST", NO_DOCKER_SOCKET)
    monkeypatch.setenv("DOCKER_SOCKET", NO_DOCKER_SOCKET)
    prior = docker_manager.BASE_DATA_DIR
    docker_manager.BASE_DATA_DIR = data
    try:
        yield epoch_mod, docker_manager, data
    finally:
        docker_manager.BASE_DATA_DIR = prior


def _marker(data: Path) -> Path:
    return data / MARKER


def _track_fds(module, monkeypatch, *, real_open, real_close, real_read=None, fail_read=None):
    opened: list[int] = []
    closed: list[int] = []

    def tracking_open(name, flags, *args, **kwargs):
        fd = real_open(name, flags, *args, **kwargs)
        opened.append(fd)
        return fd

    def tracking_close(fd):
        closed.append(fd)
        return real_close(fd)

    monkeypatch.setattr(module.os, "open", tracking_open)
    monkeypatch.setattr(module.os, "close", tracking_close)
    if fail_read is not None:
        monkeypatch.setattr(module.os, "read", fail_read)
    elif real_read is not None:
        monkeypatch.setattr(module.os, "read", real_read)
    return opened, closed


def _child_token(data: Path) -> object:
    environment = os.environ.copy()
    pythonpath = environment.get("PYTHONPATH", "")
    environment.update(
        {
            "BASE_DATA_DIR": str(data),
            "DOCKER_HOST": NO_DOCKER_SOCKET,
            "DOCKER_SOCKET": NO_DOCKER_SOCKET,
            "PYTHONPATH": str(SERVER_DIR) + (os.pathsep + pythonpath if pythonpath else ""),
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from office.epoch import current_epoch; print(json.dumps(current_epoch()))",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_absent_marker_and_missing_base_return_none_and_create_nothing(epoch_world):
    module, docker_mgr, data = epoch_world
    assert not data.exists()
    assert module.current_epoch() is None
    assert not data.exists()
    data.mkdir()
    before = list(data.iterdir())
    assert module.current_epoch() is None
    assert list(data.iterdir()) == before
    assert not _marker(data).exists()
    missing = data.parent / "missing-base"
    docker_mgr.BASE_DATA_DIR = missing
    assert module.current_epoch() is None
    assert not missing.exists()


def test_same_process_observes_absent_then_a_then_b_and_native_equality(epoch_world):
    module, _docker_mgr, data = epoch_world
    data.mkdir()
    absent = module.current_epoch()
    assert absent is None
    _marker(data).write_text("token-a", encoding="utf-8")
    a = module.current_epoch()
    assert a == "token-a"
    assert a == module.current_epoch()
    _marker(data).write_text("token-b", encoding="utf-8")
    b = module.current_epoch()
    assert b == "token-b"
    assert a != b
    assert a == "token-a"
    assert b != absent
    assert not (a == b)


def test_whitespace_is_stripped_and_readable_empty_is_distinct_from_none(epoch_world):
    module, _docker_mgr, data = epoch_world
    data.mkdir()
    marker = _marker(data)
    marker.write_bytes(b"  token-a\n\t")
    assert module.current_epoch() == "token-a"
    marker.write_bytes(b"")
    empty = module.current_epoch()
    assert empty == ""
    assert empty is not None
    assert empty != None  # noqa: E711 — native equality with initial epoch
    assert json.loads(json.dumps(empty)) == ""
    marker.write_bytes(b" \n\t")
    assert module.current_epoch() == ""
    marker.unlink()
    assert module.current_epoch() is None
    assert module.current_epoch() != ""


def test_fresh_process_reads_unchanged_token(epoch_world):
    module, _docker_mgr, data = epoch_world
    data.mkdir()
    _marker(data).write_text("token-a\n", encoding="utf-8")
    assert module.current_epoch() == "token-a"
    assert _child_token(data) == "token-a"
    assert _marker(data).read_text(encoding="utf-8") == "token-a\n"


def test_directory_fifo_and_symlink_are_refused_without_external_or_blocking_read(
    epoch_world, monkeypatch
):
    module, _docker_mgr, data = epoch_world
    data.mkdir()
    marker = _marker(data)
    outside = data.parent / "secret.bin"
    outside.write_bytes(b"EXTERNAL-EPOCH")
    original_open = module.os.open
    original_read = module.os.read
    seen_flags: list[int] = []

    def spy_open(name, flags, *args, **kwargs):
        if os.path.basename(os.fspath(name)) == MARKER:
            seen_flags.append(flags)
            assert flags & os.O_NOFOLLOW
            assert flags & os.O_NONBLOCK
        return original_open(name, flags, *args, **kwargs)

    def spy_read(fd, n):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == (outside.stat().st_dev, outside.stat().st_ino):
            raise AssertionError("external target read")
        if stat.S_ISFIFO(info.st_mode) or stat.S_ISDIR(info.st_mode):
            raise AssertionError("nonregular marker read")
        return original_read(fd, n)

    monkeypatch.setattr(module.os, "open", spy_open)
    monkeypatch.setattr(module.os, "read", spy_read)

    marker.mkdir()
    with pytest.raises(module.RestoreEpochError):
        module.current_epoch()
    marker.rmdir()

    os.mkfifo(marker)
    with pytest.raises(module.RestoreEpochError):
        module.current_epoch()
    marker.unlink()

    os.symlink(outside, marker)
    with pytest.raises(module.RestoreEpochError) as error:
        module.current_epoch()
    assert isinstance(error.value.__cause__, OSError)
    assert error.value.__cause__.errno in (errno.ELOOP, errno.EPERM)
    marker.unlink()

    assert outside.read_bytes() == b"EXTERNAL-EPOCH"
    assert not (data / MARKER).exists()
    assert seen_flags


def test_eacces_eio_and_invalid_utf8_raise_with_cause_and_close_fd(epoch_world, monkeypatch):
    module, _docker_mgr, data = epoch_world
    data.mkdir()
    marker = _marker(data)
    marker.write_bytes(b"token-a")
    identity = (marker.stat().st_dev, marker.stat().st_ino)
    real_open = module.os.open
    real_read = module.os.read
    real_close = module.os.close

    def deny_open(name, flags, *args, **kwargs):
        dir_fd = kwargs.get("dir_fd")
        try:
            info = (
                os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                if dir_fd is not None
                else os.lstat(name)
            )
        except (OSError, TypeError, ValueError):
            info = None
        if info is not None and (info.st_dev, info.st_ino) == identity:
            raise OSError(errno.EACCES, "Permission denied")
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", deny_open)
    with pytest.raises(module.RestoreEpochError) as denied:
        module.current_epoch()
    assert isinstance(denied.value.__cause__, OSError)
    assert denied.value.__cause__.errno == errno.EACCES
    assert marker.read_bytes() == b"token-a"

    def fail_read(fd, n):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == identity:
            raise OSError(errno.EIO, "injected epoch read fault")
        return real_read(fd, n)

    opened, closed = _track_fds(module, monkeypatch, real_open=real_open, real_close=real_close, fail_read=fail_read)
    with pytest.raises(module.RestoreEpochError) as io_error:
        module.current_epoch()
    assert isinstance(io_error.value.__cause__, OSError)
    assert io_error.value.__cause__.errno == errno.EIO
    assert opened
    assert set(opened) <= set(closed)

    marker.write_bytes(b"\xff\xfe")
    opened_utf, closed_utf = _track_fds(
        module, monkeypatch, real_open=real_open, real_close=real_close, real_read=real_read
    )
    with pytest.raises(module.RestoreEpochError) as utf_error:
        module.current_epoch()
    assert isinstance(utf_error.value.__cause__, UnicodeDecodeError)
    assert opened_utf
    assert set(opened_utf) <= set(closed_utf)
    assert marker.read_bytes() == b"\xff\xfe"

    marker.unlink()
    marker.mkdir()
    opened_dir, closed_dir = _track_fds(
        module, monkeypatch, real_open=real_open, real_close=real_close, real_read=real_read
    )
    with pytest.raises(module.RestoreEpochError):
        module.current_epoch()
    assert opened_dir
    assert set(opened_dir) <= set(closed_dir)

