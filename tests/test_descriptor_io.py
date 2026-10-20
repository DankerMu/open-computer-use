# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Real-filesystem acquisition, containment and descriptor ownership contracts."""
from __future__ import annotations

import errno
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1] / "computer-use-server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import descriptor_io


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "child").mkdir()
    (tmp_path / "payload").write_bytes(b"workspace bytes")
    os.mkfifo(tmp_path / "pipe")
    (tmp_path / "file-link").symlink_to(tmp_path / "payload")
    (tmp_path / "directory-link").symlink_to(tmp_path / "child", target_is_directory=True)
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield tmp_path, root_fd
    finally:
        os.close(root_fd)


def assert_closed(fd):
    with pytest.raises(OSError) as failure:
        os.fstat(fd)
    assert failure.value.errno == errno.EBADF


def test_acquired_descriptors_survive_path_replacement_and_leave_root_borrowed(tree):
    root, root_fd = tree
    directory_fd = descriptor_io.open_directory("child", dir_fd=root_fd)
    file_fd = descriptor_io.open_regular("payload", dir_fd=root_fd)
    try:
        (root / "child").rename(root / "moved-child")
        (root / "child").mkdir()
        (root / "payload").unlink()
        (root / "payload").write_bytes(b"replacement bytes")
        assert os.fstat(directory_fd).st_ino == (root / "moved-child").stat().st_ino
        assert os.read(file_fd, 100) == b"workspace bytes"
        assert os.fstat(root_fd).st_ino == root.stat().st_ino
    finally:
        os.close(file_fd)
        os.close(directory_fd)


def test_absolute_directory_acquisition_returns_owned_directory(tree):
    root, root_fd = tree
    fd = descriptor_io.open_directory(root / "child")
    try:
        assert os.fstat(fd).st_ino == (root / "child").stat().st_ino
    finally:
        os.close(fd)
    assert os.fstat(root_fd).st_ino == root.stat().st_ino


@pytest.mark.parametrize("op,name", [
    ("open_regular", "file-link"),
    ("open_directory", "directory-link"),
])
def test_symlink_acquisition_never_returns_target_descriptor(tree, op, name):
    root, root_fd = tree
    with pytest.raises(descriptor_io.DescriptorOpenError) as failure:
        getattr(descriptor_io, op)(name, dir_fd=root_fd)
    assert failure.value.error.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR)
    assert os.fstat(root_fd).st_ino == root.stat().st_ino


@pytest.mark.parametrize("op", ["open_directory", "open_regular"])
@pytest.mark.parametrize("number", [errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.EMLINK,
                                    errno.EIO, errno.EMFILE, errno.ENFILE])
def test_open_failures_retain_original_error_and_borrowed_root(tree, monkeypatch, op, number):
    root, root_fd = tree
    error = OSError(number, "injected open failure")

    def fail_open(*args, **kwargs):
        raise error

    with monkeypatch.context() as patches:
        patches.setattr(os, "open", fail_open)
        with pytest.raises(descriptor_io.DescriptorOpenError) as failure:
            getattr(descriptor_io, op)("payload", dir_fd=root_fd)
    assert failure.value.error is error
    assert descriptor_io.is_missing(error) == (number in (errno.ENOENT, errno.ENOTDIR))
    assert descriptor_io.is_nofollow_error(error) == (number in (errno.ELOOP, errno.EMLINK))
    assert os.fstat(root_fd).st_ino == root.stat().st_ino


@pytest.mark.parametrize("op,name,number", [
    ("open_directory", "absent", errno.ENOENT),
    ("open_regular", "absent", errno.ENOENT),
    ("open_directory", "payload", errno.ENOTDIR),
])
def test_real_missing_and_non_directory_paths_preserve_open_phase(tree, op, name, number):
    root, root_fd = tree
    with pytest.raises(descriptor_io.DescriptorOpenError) as failure:
        getattr(descriptor_io, op)(name, dir_fd=root_fd)
    assert failure.value.error.errno == number
    assert os.fstat(root_fd).st_ino == root.stat().st_ino


@pytest.mark.parametrize("op,name", [("open_directory", "child"), ("open_regular", "payload")])
@pytest.mark.parametrize("fault", ["fstat", "interrupt", "type"])
def test_post_open_failure_closes_owned_descriptor_not_borrowed_root(tree, monkeypatch, op, name, fault):
    root, root_fd = tree
    original_open = os.open
    acquired = []
    error = KeyboardInterrupt() if fault == "interrupt" else OSError(errno.EIO, "injected fstat failure")

    def track_open(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        acquired.append(fd)
        return fd

    def fail_fstat(fd):
        if fault == "type":
            return SimpleNamespace(st_mode=stat.S_IFLNK)
        raise error

    with monkeypatch.context() as patches:
        patches.setattr(os, "open", track_open)
        patches.setattr(os, "fstat", fail_fstat)
        expected = descriptor_io.DescriptorTypeError if fault == "type" else type(error)
        with pytest.raises(expected) as failure:
            getattr(descriptor_io, op)(name, dir_fd=root_fd)
    if fault != "type":
        assert failure.value is error
    assert len(acquired) == 1
    assert_closed(acquired[0])
    assert os.fstat(root_fd).st_ino == root.stat().st_ino
    recovered = getattr(descriptor_io, op)(name, dir_fd=root_fd)
    os.close(recovered)


@pytest.mark.parametrize("name", ["child", "pipe"])
def test_directory_and_fifo_are_rejected_as_regular_files_without_leaks(tree, monkeypatch, name):
    root, root_fd = tree
    original_open = os.open
    acquired = []

    def track_open(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        acquired.append(fd)
        return fd

    with monkeypatch.context() as patches:
        patches.setattr(os, "open", track_open)
        with pytest.raises(descriptor_io.DescriptorTypeError):
            descriptor_io.open_regular(name, dir_fd=root_fd)
    assert len(acquired) == 1
    assert_closed(acquired[0])
    assert os.fstat(root_fd).st_ino == root.stat().st_ino


@pytest.mark.parametrize("ignore_close_errors", [False, True])
def test_cleanup_failure_policy_preserves_original_metadata_error_when_requested(
        tree, monkeypatch, ignore_close_errors):
    root, root_fd = tree
    original_close = os.close
    metadata_error = OSError(errno.EIO, "metadata failed")
    close_error = OSError(errno.EBADF, "close failed")

    def fail_fstat(fd):
        raise metadata_error

    def close_then_fail(fd):
        original_close(fd)
        raise close_error

    with monkeypatch.context() as patches:
        patches.setattr(os, "fstat", fail_fstat)
        patches.setattr(os, "close", close_then_fail)
        with pytest.raises(OSError) as failure:
            descriptor_io.open_regular("payload", dir_fd=root_fd,
                                       ignore_close_errors=ignore_close_errors)
    assert failure.value is (metadata_error if ignore_close_errors else close_error)
    assert os.fstat(root_fd).st_ino == root.stat().st_ino


def test_record_cleanup_error_first_record_sets_tuple_and_exact_note():
    primary = OSError(errno.ENOSPC, "replace refused")
    cleanup = PermissionError(errno.EACCES, "cleanup refused")
    descriptor_io.record_cleanup_error(primary, cleanup, what="Temporary receipt")
    assert getattr(primary, "cleanup_errors", None) == (cleanup,)
    assert getattr(primary, "__notes__", []) == [
        "Temporary receipt cleanup also failed: PermissionError: [Errno 13] cleanup refused"]


def test_record_cleanup_error_accumulates_errors_and_notes_in_order():
    primary = OSError(errno.ENOSPC, "versions fsync refused")
    claimed = OSError(errno.EIO, "claimed blob unlink refused")
    temporary = OSError(errno.EIO, "temporary blob unlink refused")
    descriptor_io.record_cleanup_error(primary, claimed, what="Claimed version blob")
    descriptor_io.record_cleanup_error(primary, temporary, what="Temporary version blob")
    assert getattr(primary, "cleanup_errors", None) == (claimed, temporary)
    assert getattr(primary, "__notes__", []) == [
        "Claimed version blob cleanup also failed: OSError: [Errno 5] claimed blob unlink refused",
        "Temporary version blob cleanup also failed: OSError: [Errno 5] temporary blob unlink refused",
    ]


def test_record_cleanup_error_is_independent_of_close_errors():
    close_error = OSError(errno.EBADF, "close failed")
    carrying = OSError(errno.ENOSPC, "fsync refused")
    carrying.close_errors = (close_error,)
    cleanup = OSError(errno.EIO, "unlink refused")
    descriptor_io.record_cleanup_error(carrying, cleanup, what="Temporary version blob")
    assert getattr(carrying, "cleanup_errors", None) == (cleanup,)
    assert getattr(carrying, "__notes__", []) == [
        "Temporary version blob cleanup also failed: OSError: [Errno 5] unlink refused"]
    assert carrying.close_errors == (close_error,)

    bare = OSError(errno.ENOSPC, "fsync refused")
    other = OSError(errno.EIO, "unlink refused again")
    descriptor_io.record_cleanup_error(bare, other, what="Claimed version blob")
    assert getattr(bare, "cleanup_errors", None) == (other,)
    assert getattr(bare, "__notes__", []) == [
        "Claimed version blob cleanup also failed: OSError: [Errno 5] unlink refused again"]
    assert not hasattr(bare, "close_errors")
