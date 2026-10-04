# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Real-filesystem contract tests for the Office per-chat state file.

The public seam is ``office.store.OfficeStore``.  Tests use real paths,
descriptors, atomic replacement and separate processes.  Docker is forbidden.
"""
from __future__ import annotations

import errno
import json
import os
import stat
from pathlib import Path

import pytest

from tests.orchestrator._office_store import (
    CHAT,
    EMPTY,
    OTHER_CHAT,
    _inode,
    _outputs,
    _role_inodes,
    _run_child,
    _seed,
    _start_child,
    _state,
    _stop_child,
    _wait_marker,
)


def test_missing_state_reads_empty_schema_without_creating_state_file(world):
    store_mod, _docker_manager, data = world
    snapshot = store_mod.OfficeStore().read(CHAT)
    assert snapshot == EMPTY
    snapshot["documents"]["x"] = 1
    assert store_mod.OfficeStore().read(CHAT) == EMPTY
    assert not _state(data).exists()
    assert not (_outputs(data)).exists()


def test_first_update_is_confined_to_own_chat_and_absent_from_workspace(world):
    store_mod, _docker_manager, data = world
    foreign = data / OTHER_CHAT / "keep.txt"
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"FOREIGN")
    workspace = _outputs(data)
    workspace.mkdir(parents=True)
    listed = workspace / "brief.docx"
    listed.write_bytes(b"WORKSPACE")

    snapshot = _seed(store_mod, "doc-1")
    assert snapshot["schema_version"] == 1
    assert snapshot["documents"]["doc-1"] == {"id": "doc-1"}
    assert snapshot["sessions"] == {}
    assert _state(data).is_file()
    assert not _state(data, OTHER_CHAT).exists()
    assert foreign.read_bytes() == b"FOREIGN"
    assert listed.read_bytes() == b"WORKSPACE"
    assert [path.name for path in workspace.iterdir()] == ["brief.docx"]
    assert ".ocu" not in {path.name for path in workspace.rglob("*")}


@pytest.mark.parametrize("order", [("documents", "sessions"), ("sessions", "documents")])
def test_two_workers_retain_independent_updates_in_both_orders(world, order):
    store_mod, _docker_manager, data = world
    first, second = order
    hold = data.parent / f"office-overlap-hold-{first}"
    entered = data.parent / f"office-overlap-entered-{first}"
    contended = data.parent / f"office-overlap-contended-{first}"
    os.mkfifo(hold)
    holder = waiter = None
    try:
        holder = _start_child(
            data,
            OCU_CHILD_OP="hold-lock",
            OCU_COLLECTION=first,
            OCU_TOKEN="one",
            OCU_ENTERED=str(entered),
            OCU_HOLD=str(hold),
        )
        _wait_marker(entered, holder, "holder did not enter the mutator under the lock")
        waiter = _start_child(
            data,
            OCU_CHILD_OP="wait-lock",
            OCU_COLLECTION=second,
            OCU_TOKEN="two",
            OCU_CONTENDED=str(contended),
            OCU_LOCK_DENIAL="office lock was granted without contention",
        )
        _wait_marker(contended, waiter, "waiter did not observe LOCK_NB denial")
        assert holder.poll() is None
        assert waiter.poll() is None
        os.close(os.open(hold, os.O_WRONLY))
        holder_out, holder_err = holder.communicate(timeout=10)
        waiter_out, waiter_err = waiter.communicate(timeout=10)
        assert holder.returncode == 0, (holder_out, holder_err)
        assert waiter.returncode == 0, (waiter_out, waiter_err)
        first_state = json.loads(holder_out.strip().splitlines()[-1])
        second_state = json.loads(waiter_out.strip().splitlines()[-1])
        assert first_state[first]["one"] == {"id": "one"}
        assert "two" not in first_state.get(second, {})
        assert second_state[first]["one"] == {"id": "one"}
        assert second_state[second]["two"] == {"id": "two"}
        persisted = _run_child(data, OCU_CHILD_OP="read")
        assert persisted[first]["one"] == {"id": "one"}
        assert persisted[second]["two"] == {"id": "two"}
        assert store_mod.OfficeStore().read(CHAT) == persisted
    finally:
        _stop_child(holder)
        _stop_child(waiter)


def test_fresh_process_reads_exact_committed_state(world):
    store_mod, _docker_manager, data = world
    committed = _seed(store_mod, "exact")
    assert _run_child(data, OCU_CHILD_OP="read") == committed
    assert committed["schema_version"] == 1


def test_nested_same_thread_lock_can_read_and_update(world):
    store_mod, docker_manager, data = world
    store = store_mod.OfficeStore()
    with docker_manager._combined_lock(CHAT):
        assert docker_manager._FLOCK_DEPTH[CHAT] == 1
        assert store.read(CHAT) == EMPTY
        snapshot = store.update(CHAT, lambda state: state["journal"].__setitem__("j1", {"n": 1}))
        assert docker_manager._FLOCK_DEPTH[CHAT] == 1
        assert snapshot["journal"]["j1"] == {"n": 1}
    assert CHAT not in docker_manager._FLOCK_DEPTH
    assert store.read(CHAT)["journal"]["j1"] == {"n": 1}


def test_update_waits_for_canonical_lock_after_real_lock_nb_denial(world):
    store_mod, docker_manager, data = world
    _seed(store_mod, "held")
    contended = data.parent / "office-contended"
    child = None
    try:
        with docker_manager._combined_lock(CHAT):
            child = _start_child(
                data,
                OCU_CHILD_OP="wait-lock",
                OCU_COLLECTION="sessions",
                OCU_TOKEN="waiter",
                OCU_CONTENDED=str(contended),
                OCU_LOCK_DENIAL="office lock was granted without contention",
            )
            _wait_marker(contended, child, "update did not contend for the lifecycle flock")
            assert child.poll() is None
            store_mod.OfficeStore().update(
                CHAT, lambda state: state["receipts"].__setitem__("r1", {"ok": True})
            )
            assert child.poll() is None
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, (stdout, stderr)
        persisted = json.loads(stdout.strip().splitlines()[-1])
        assert persisted["documents"]["held"] == {"id": "held"}
        assert persisted["receipts"]["r1"] == {"ok": True}
        assert persisted["sessions"]["waiter"] == {"id": "waiter"}
    finally:
        _stop_child(child)



@pytest.mark.parametrize(
    "payload",
    [
        b"{not-json",
        json.dumps({**EMPTY, "schema_version": True}).encode("utf-8"),
        json.dumps({**EMPTY, "schema_version": 2}).encode("utf-8"),
        json.dumps({k: v for k, v in EMPTY.items() if k != "journal"}).encode("utf-8"),
        json.dumps({**EMPTY, "documents": []}).encode("utf-8"),
        json.dumps({**EMPTY, "extra": {}}).encode("utf-8"),
    ],
)
def test_malformed_top_shape_and_version_fail_closed_on_read_and_update(world, payload):
    store_mod, _docker_manager, data = world
    path = _state(data)
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    store = store_mod.OfficeStore()
    with pytest.raises(store_mod.StateCorruptError):
        store.read(CHAT)
    assert path.read_bytes() == payload
    with pytest.raises(store_mod.StateCorruptError):
        store.update(CHAT, lambda state: state["sessions"].__setitem__("x", 1))
    assert path.read_bytes() == payload


def test_eacces_on_state_file_is_corrupt_for_read_and_update(world, monkeypatch):
    store_mod, _docker_manager, data = world
    _seed(store_mod, "deny")
    path = _state(data)
    before = path.read_bytes()
    identity = (path.stat().st_dev, path.stat().st_ino)
    original_open = store_mod.os.open

    def deny_state_open(name, flags, *args, **kwargs):
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
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(store_mod.os, "open", deny_state_open)
    store = store_mod.OfficeStore()
    with pytest.raises(store_mod.StateCorruptError):
        store.read(CHAT)
    with pytest.raises(store_mod.StateCorruptError):
        store.update(CHAT, lambda state: None)
    assert path.read_bytes() == before


def _assert_storage_oserror_is_corrupt(store_mod, data, inject):
    _seed(store_mod, "deny")
    path = _state(data)
    before = path.read_bytes()
    inject()
    store = store_mod.OfficeStore()
    with pytest.raises(store_mod.StateCorruptError) as read_error:
        store.read(CHAT)
    assert isinstance(read_error.value.__cause__, OSError)
    assert read_error.value.__cause__.errno == errno.EIO
    with pytest.raises(store_mod.StateCorruptError) as update_error:
        store.update(CHAT, lambda state: state["sessions"].__setitem__("lost", {"id": "lost"}))
    assert isinstance(update_error.value.__cause__, OSError)
    assert update_error.value.__cause__.errno == errno.EIO
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp"))


def test_eio_on_state_read_is_corrupt_for_read_and_update(world, monkeypatch):
    store_mod, _docker_manager, data = world
    path = _state(data)
    original_read = store_mod.os.read

    def fail_state_read(fd, n):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == (path.stat().st_dev, path.stat().st_ino):
            raise OSError(errno.EIO, "injected state read fault")
        return original_read(fd, n)

    def inject():
        monkeypatch.setattr(store_mod.os, "read", fail_state_read)

    _assert_storage_oserror_is_corrupt(store_mod, data, inject)


def test_eio_on_state_lstat_is_corrupt_for_read_and_update(world, monkeypatch):
    store_mod, _docker_manager, data = world
    path = _state(data)
    original_lstat = store_mod.os.lstat

    def fail_state_lstat(name, *args, dir_fd=None, **kwargs):
        if dir_fd is not None and name == "state.json":
            raise OSError(errno.EIO, "injected state lstat fault")
        return original_lstat(name, *args, dir_fd=dir_fd, **kwargs)

    def inject():
        monkeypatch.setattr(store_mod.os, "lstat", fail_state_lstat)

    _assert_storage_oserror_is_corrupt(store_mod, data, inject)


def test_eio_on_state_fstat_is_corrupt_for_read_and_update(world, monkeypatch):
    store_mod, _docker_manager, data = world
    path = _state(data)
    original_fstat = store_mod.os.fstat

    def fail_state_fstat(fd):
        info = original_fstat(fd)
        if (info.st_dev, info.st_ino) == (path.stat().st_dev, path.stat().st_ino):
            raise OSError(errno.EIO, "injected state fstat fault")
        return info

    def inject():
        monkeypatch.setattr(store_mod.os, "fstat", fail_state_fstat)

    _assert_storage_oserror_is_corrupt(store_mod, data, inject)


def test_eio_on_root_lstat_is_corrupt_for_read_and_update(world, monkeypatch):
    store_mod, _docker_manager, data = world
    root = data / CHAT
    original_lstat = store_mod.os.lstat

    def fail_root_lstat(name, *args, dir_fd=None, **kwargs):
        if dir_fd is None and Path(name) == root:
            raise OSError(errno.EIO, "injected root lstat fault")
        return original_lstat(name, *args, dir_fd=dir_fd, **kwargs)

    def inject():
        monkeypatch.setattr(store_mod.os, "lstat", fail_root_lstat)

    _assert_storage_oserror_is_corrupt(store_mod, data, inject)


def test_eio_on_control_lstat_is_corrupt_for_read_and_update(world, monkeypatch):
    store_mod, _docker_manager, data = world
    original_lstat = store_mod.os.lstat

    def fail_control_lstat(name, *args, dir_fd=None, **kwargs):
        if dir_fd is not None and name in {".ocu", "office"}:
            raise OSError(errno.EIO, "injected control lstat fault")
        return original_lstat(name, *args, dir_fd=dir_fd, **kwargs)

    def inject():
        monkeypatch.setattr(store_mod.os, "lstat", fail_control_lstat)

    _assert_storage_oserror_is_corrupt(store_mod, data, inject)


def test_eio_on_data_root_open_is_corrupt_for_read_and_update(world, monkeypatch):
    store_mod, _docker_manager, data = world
    original_open = store_mod.os.open

    def fail_data_root_open(name, flags, *args, **kwargs):
        if kwargs.get("dir_fd") is None and Path(name) == data:
            raise OSError(errno.EIO, "injected data root open fault")
        return original_open(name, flags, *args, **kwargs)

    def inject():
        monkeypatch.setattr(store_mod.os, "open", fail_data_root_open)

    _assert_storage_oserror_is_corrupt(store_mod, data, inject)



def test_invalid_and_raising_mutators_leave_predecessor_intact(world):
    store_mod, _docker_manager, data = world
    _seed(store_mod, "keep")
    before = _state(data).read_bytes()
    store = store_mod.OfficeStore()

    def returns_value(state):
        state["documents"]["lost"] = {"id": "lost"}
        return state

    def nan_value(state):
        state["documents"]["lost"] = float("nan")

    def bool_version(state):
        state["schema_version"] = True

    def boom(state):
        state["documents"]["lost"] = {"id": "lost"}
        raise RuntimeError("mutator exploded")

    with pytest.raises(ValueError):
        store.update(CHAT, returns_value)
    with pytest.raises(ValueError):
        store.update(CHAT, nan_value)
    with pytest.raises(ValueError):
        store.update(CHAT, bool_version)
    with pytest.raises(RuntimeError, match="mutator exploded"):
        store.update(CHAT, boom)
    assert _state(data).read_bytes() == before
    assert store.read(CHAT)["documents"] == {"keep": {"id": "keep"}}


def test_pre_replace_failure_keeps_predecessor_and_cleans_temporary_files(world, monkeypatch):
    store_mod, _docker_manager, data = world
    _seed(store_mod, "keep")
    before = _state(data).read_bytes()

    def rejected_replace(*_args, **_kwargs):
        raise OSError("controlled atomic replacement failure")

    monkeypatch.setattr(store_mod.os, "replace", rejected_replace)
    with pytest.raises(OSError, match="controlled atomic replacement failure"):
        store_mod.OfficeStore().update(
            CHAT, lambda state: state["sessions"].__setitem__("x", {"id": "x"})
        )
    assert _state(data).read_bytes() == before
    assert not list(_state(data).parent.glob("*.tmp"))


def test_killed_child_after_successor_fsync_before_replace_retains_predecessor(world):
    store_mod, _docker_manager, data = world
    _seed(store_mod, "keep")
    before = _state(data).read_bytes()
    hold = data.parent / "office-hold"
    os.mkfifo(hold)
    seam = data.parent / "office-seam"
    child = None
    try:
        child = _start_child(
            data,
            OCU_CHILD_OP="crash-before-replace",
            OCU_SEAM=str(seam),
            OCU_HOLD=str(hold),
        )
        _wait_marker(seam, child, "child did not reach pre-replace seam")
        assert child.poll() is None
        child.kill()
        child.wait(timeout=5)
        assert _state(data).read_bytes() == before
        assert store_mod.OfficeStore().read(CHAT)["documents"] == {"keep": {"id": "keep"}}
    finally:
        _stop_child(child)



def test_post_replace_directory_fsync_failure_reports_complete_successor(world, monkeypatch):
    store_mod, _docker_manager, data = world
    _seed(store_mod, "keep")
    before = _state(data).read_bytes()
    office = _inode(_state(data).parent)
    original_fsync = store_mod.os.fsync

    def fail_leaf_directory_sync(fd):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == office:
            raise OSError("controlled directory fsync failure")
        return original_fsync(fd)

    with monkeypatch.context() as patches:
        patches.setattr(store_mod.os, "fsync", fail_leaf_directory_sync)
        with pytest.raises(store_mod.StateDurabilityError, match="committed"):
            store_mod.OfficeStore().update(
                CHAT, lambda state: state["sessions"].__setitem__("live", {"id": "live"})
            )
    assert _state(data).read_bytes() != before
    persisted = store_mod.OfficeStore().read(CHAT)
    assert persisted["documents"]["keep"] == {"id": "keep"}
    assert persisted["sessions"]["live"] == {"id": "live"}


def test_symlinked_state_and_control_paths_leave_external_sentinel_unchanged(world):
    store_mod, _docker_manager, data = world
    sentinel = data.parent / "sentinel"
    sentinel.write_bytes(b"SECRET")
    outside = data.parent / "outside-office"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"SECRET-DIR")

    linked_chat = "c3d4e5f6-a7b8-9012-cdef-345678901234"
    (data / linked_chat).mkdir(parents=True)
    control = data / linked_chat / ".ocu" / "office"
    control.mkdir(parents=True)
    os.symlink(sentinel, control / "state.json")
    with pytest.raises(store_mod.StateCorruptError):
        store_mod.OfficeStore().read(linked_chat)
    with pytest.raises(store_mod.StateCorruptError):
        store_mod.OfficeStore().update(linked_chat, lambda state: None)
    assert sentinel.read_bytes() == b"SECRET"
    assert (control / "state.json").is_symlink()

    dir_chat = "d4e5f6a7-b8c9-0123-defa-456789012345"
    (data / dir_chat / ".ocu").mkdir(parents=True)
    os.symlink(outside, data / dir_chat / ".ocu" / "office")
    with pytest.raises(store_mod.StateCorruptError):
        store_mod.OfficeStore().read(dir_chat)
    with pytest.raises(store_mod.StateCorruptError):
        store_mod.OfficeStore().update(dir_chat, lambda state: None)
    assert (outside / "secret.txt").read_bytes() == b"SECRET-DIR"
    assert list(outside.iterdir()) == [outside / "secret.txt"]


def _record_fsync_roles(store_mod, data, original_fsync, fail_role=None):
    observed = []

    def spy(fd):
        info = os.fstat(fd)
        identity = (info.st_dev, info.st_ino)
        if not stat.S_ISDIR(info.st_mode):
            observed.append("temp")
            return original_fsync(fd)
        roles = _role_inodes(data)
        for role, inode in roles.items():
            if identity == inode:
                observed.append(role)
                if role == fail_role:
                    raise OSError(errno.EIO, f"injected {role} fsync fault")
                return original_fsync(fd)
        raise AssertionError(("unexpected fsync descriptor", identity, roles))

    return observed, spy


def test_first_update_syncs_owned_directory_ancestry_before_success(world, monkeypatch):
    store_mod, _docker_manager, data = world
    data.mkdir(exist_ok=True)
    original_fsync = store_mod.os.fsync
    observed, spy = _record_fsync_roles(store_mod, data, original_fsync)
    monkeypatch.setattr(store_mod.os, "fsync", spy)
    store_mod.OfficeStore().update(CHAT, lambda state: state["documents"].__setitem__("first", {"id": "first"}))
    assert observed == ["temp", "base", "root", "ocu", "office"]
    persisted = _run_child(data, OCU_CHILD_OP="read")
    assert persisted["documents"]["first"] == {"id": "first"}


@pytest.mark.parametrize("role", ["base", "root", "ocu"])
def test_pre_replace_ancestor_sync_failure_keeps_predecessor_and_retries_existing_dirs(world, monkeypatch, role):
    store_mod, _docker_manager, data = world
    _seed(store_mod, "keep")
    before = _state(data).read_bytes()
    original_fsync = store_mod.os.fsync
    failed, fail_spy = _record_fsync_roles(store_mod, data, original_fsync, fail_role=role)
    with monkeypatch.context() as patches:
        patches.setattr(store_mod.os, "fsync", fail_spy)
        with pytest.raises(OSError) as error:
            store_mod.OfficeStore().update(
                CHAT, lambda state: state["sessions"].__setitem__("lost", {"id": "lost"})
            )
    assert error.value.errno == errno.EIO
    assert role in failed
    assert "office" not in failed
    assert _state(data).read_bytes() == before
    assert not list(_state(data).parent.glob("*.tmp"))

    retried, retry_spy = _record_fsync_roles(store_mod, data, original_fsync)
    monkeypatch.setattr(store_mod.os, "fsync", retry_spy)
    snapshot = store_mod.OfficeStore().update(
        CHAT, lambda state: state["sessions"].__setitem__("kept", {"id": "kept"})
    )
    assert retried == ["temp", "base", "root", "ocu", "office"]
    assert snapshot["documents"]["keep"] == {"id": "keep"}
    assert snapshot["sessions"]["kept"] == {"id": "kept"}
    assert _run_child(data, OCU_CHILD_OP="read") == snapshot


def test_symlinked_and_regular_file_chat_roots_leave_outside_lock_and_sentinel_unchanged(world):
    store_mod, _docker_manager, data = world
    sentinel = data.parent / "root-sentinel"
    sentinel.write_bytes(b"ROOT-SECRET")
    outside = data.parent / "outside-chat-root"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"ROOT-DIR")

    linked_chat = "e5f6a7b8-c9d0-1234-efab-567890123456"
    data.mkdir(exist_ok=True)
    os.symlink(outside, data / linked_chat)
    store = store_mod.OfficeStore()
    with pytest.raises(store_mod.StateCorruptError):
        store.read(linked_chat)
    with pytest.raises(store_mod.StateCorruptError):
        store.update(linked_chat, lambda state: None)
    assert not (outside / ".lifecycle.lock").exists()
    assert (outside / "secret.txt").read_bytes() == b"ROOT-DIR"
    assert list(outside.iterdir()) == [outside / "secret.txt"]
    assert sentinel.read_bytes() == b"ROOT-SECRET"

    file_chat = "f6a7b8c9-d0e1-2345-fabc-678901234567"
    target = data / file_chat
    target.write_bytes(b"NOT-A-DIRECTORY")
    with pytest.raises(store_mod.StateCorruptError):
        store.read(file_chat)
    with pytest.raises(store_mod.StateCorruptError):
        store.update(file_chat, lambda state: None)
    assert target.read_bytes() == b"NOT-A-DIRECTORY"
    assert not target.is_dir()
    assert not (data / ".lifecycle.lock").exists()
    assert sentinel.read_bytes() == b"ROOT-SECRET"


def test_generic_read_and_update_still_accept_empty_and_previous_fixtures(world):
    store_mod, _docker_manager, data = world
    store = store_mod.OfficeStore()
    assert store.read(CHAT) == EMPTY
    snapshot = store.update(CHAT, lambda state: state["receipts"].__setitem__("r1", {"ok": True}))
    assert snapshot["receipts"]["r1"] == {"ok": True}
    assert snapshot["documents"] == {}
    later = store.update(CHAT, lambda state: state["documents"].__setitem__("keep", {"id": "keep"}))
    assert later["documents"]["keep"] == {"id": "keep"}
    assert later["receipts"]["r1"] == {"ok": True}
