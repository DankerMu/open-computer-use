# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Shared lifecycle lock safety through the public Office sweep."""
from __future__ import annotations

import asyncio
import errno
import importlib
import io
import json
import os
import stat
import subprocess
import sys
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from anyio.from_thread import start_blocking_portal
from tests.orchestrator._office_store import CHAT, ROOT, _child_env, _inode, _seed, _stop_child
from tests.orchestrator.test_lifecycle import _container, _docker


def test_office_sweep_never_opens_or_flocks_canary_after_lock_precheck(world, monkeypatch):
    store_mod, manager, data = world
    monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", "http://documentserver.invalid")
    _seed(store_mod)
    lock_path = data / CHAT / ".lifecycle.lock"
    assert stat.S_ISREG(lock_path.lstat().st_mode)

    import office.sweep as sweep

    importlib.reload(sweep)
    external = data.parent / "external"
    external.mkdir()
    canary = external / "canary"
    canary.write_bytes(b"EXTERNAL-LIFECYCLE-CANARY")
    canary_inode = _inode(canary)
    canary_mode = canary.stat().st_mode
    external_entries = {path.name: _inode(path) for path in external.iterdir()}

    real_has_state = sweep._has_state
    events = []

    def swap_after_precheck(store, chat):
        present = real_has_state(store, chat)
        if chat == CHAT and present and not events:
            events.append("real-precheck-succeeded")
            lock_path.unlink()
            lock_path.symlink_to(canary)
            events.append("canary-symlink-installed")
        return present

    try:
        with _lock_observations(manager, monkeypatch) as observed:
            with monkeypatch.context() as boundary, start_blocking_portal() as portal:
                boundary.setattr(sweep, "_has_state", swap_after_precheck)
                sweep.sweep_office_sessions(
                    now=1_700_000_000.25, loop=portal.call(asyncio.get_running_loop),
                )

            # Keep the oracle outside the sweep's per-chat Exception isolation.
            assert events == ["real-precheck-succeeded", "canary-symlink-installed"]
            assert lock_path.is_symlink()
            assert lock_path.readlink() == canary
            assert observed.lock_attempts, "sweep never reached lifecycle lock acquisition after the swap"
            observations = {
                "canary": canary_inode, "opened": observed.opened, "flocked": observed.flocked,
            }
            assert canary_inode not in observed.opened, observations
            assert canary_inode not in [identity for _operation, identity in observed.flocked], observations
        assert canary.read_bytes() == b"EXTERNAL-LIFECYCLE-CANARY"
        assert _inode(canary) == canary_inode
        assert canary.stat().st_mode == canary_mode
        assert {path.name: _inode(path) for path in external.iterdir()} == external_entries
    finally:
        lock_path.unlink(missing_ok=True)


@contextmanager
def _lock_observations(manager, monkeypatch):
    real_open, real_io_open, real_fstat, real_close = os.open, io.open, os.fstat, os.close
    real_flock = manager.fcntl.flock
    observed = SimpleNamespace(
        opened=[], flocked=[], flock_attempts=[], lock_attempts=[], handles=[], fds={},
    )

    def identity(fd):
        info = real_fstat(fd)
        return info.st_dev, info.st_ino

    def lock_attempt(route, name):
        if not isinstance(name, int) and os.path.basename(os.fsdecode(name)) == ".lifecycle.lock":
            observed.lock_attempts.append(route)

    def opened(name, *args, **kwargs):
        lock_attempt("os.open", name)
        fd = real_open(name, *args, **kwargs)
        observed.fds[fd] = identity(fd)
        observed.opened.append(observed.fds[fd])
        return fd

    def io_opened(name, *args, **kwargs):
        lock_attempt("io.open", name)
        handle = real_io_open(name, *args, **kwargs)
        observed.handles.append(handle)
        observed.opened.append(identity(handle.fileno()))
        return handle

    def flocked(fd, operation):
        inode = identity(fd)
        observed.flock_attempts.append((operation, inode))
        result = real_flock(fd, operation)
        observed.flocked.append((operation, inode))
        return result

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "open", opened)
            patch.setattr(io, "open", io_opened)
            patch.setattr(manager.fcntl, "flock", flocked)
            yield observed
    finally:
        for handle in observed.handles:
            if not handle.closed:
                handle.close()
        for fd, inode in observed.fds.items():
            try:
                current = identity(fd)
            except OSError as exc:
                if exc.errno == errno.EBADF:
                    continue
                raise
            if current == inode:
                real_close(fd)


def _assert_retired(observed):
    assert all(handle.closed for handle in observed.handles)
    for fd in observed.fds:
        with pytest.raises(OSError) as closed:
            os.fstat(fd)
        assert closed.value.errno == errno.EBADF


def _assert_another_thread_acquires(manager):
    acquired = threading.Event()
    errors = []

    def acquire():
        try:
            with manager._combined_lock(CHAT) as held:
                assert held is not None
                acquired.set()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=acquire, daemon=True)
    thread.start()
    try:
        thread.join(timeout=2)
        assert not thread.is_alive(), "acquisition stranded the canonical process lock"
        assert errors == []
        assert acquired.is_set()
        assert CHAT not in manager._FLOCK_DEPTH
    finally:
        # A failing owner can strand the calling thread's RLock; release only our ownership.
        lock = manager.get_chat_lock(CHAT)
        for _ in range(8):
            if not lock._is_owned():
                break
            lock.release()
        thread.join(timeout=2)


def _unsafe_leaf(path, kind, canary):
    if kind == "symlink":
        path.symlink_to(canary)
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)


@pytest.mark.parametrize("create", (True, False))
@pytest.mark.parametrize("kind", ("symlink", "directory"))
def test_direct_lock_refuses_unsafe_leaf_before_flock(world, monkeypatch, create, kind):
    _store, manager, data = world
    root = data / CHAT
    root.mkdir(parents=True)
    canary = data.parent / "direct-canary"
    canary.write_bytes(b"DIRECT-CANARY")
    canary_inode = _inode(canary)
    leaf = root / ".lifecycle.lock"
    _unsafe_leaf(leaf, kind, canary)
    leaf_inode = (leaf.lstat().st_dev, leaf.lstat().st_ino)
    admitted = []
    with _lock_observations(manager, monkeypatch) as observed:
        with pytest.raises(OSError):
            with manager._combined_lock(CHAT, create=create):
                admitted.append(True)
        assert admitted == []
        assert observed.flock_attempts == []
        assert canary_inode not in observed.opened
        assert CHAT not in manager._FLOCK_DEPTH
        _assert_retired(observed)
    assert canary.read_bytes() == b"DIRECT-CANARY"
    assert _inode(canary) == canary_inode
    assert (leaf.lstat().st_dev, leaf.lstat().st_ino) == leaf_inode
    if kind == "directory":
        leaf.rmdir()
    else:
        leaf.unlink()
    _assert_another_thread_acquires(manager)


def _prepare_reap(manager):
    container = _container(manager._container_name(CHAT), container_id="lock-canary-container")
    manager._docker_client = _docker([container])
    manager.mark_sleeper_retired(CHAT, container, now=1_700_000_000 - 601)
    state = manager.read_idle_state(CHAT)
    manager.write_idle_state(CHAT, {
        **state, "idle_expiry": 1_700_000_000 - 1, "observed_at": 1_700_000_000 - 30,
    })
    return container


@pytest.mark.parametrize("kind", ("symlink", "directory"))
def test_reap_refuses_unsafe_lock_without_stopping_container(world, monkeypatch, kind):
    _store, manager, data = world
    container = _prepare_reap(manager)
    leaf = data / CHAT / ".lifecycle.lock"
    leaf.unlink(missing_ok=True)
    canary = data.parent / "reap-canary"
    canary.write_bytes(b"REAP-CANARY")
    canary_inode = _inode(canary)
    _unsafe_leaf(leaf, kind, canary)
    with _lock_observations(manager, monkeypatch) as observed:
        manager.reap_known_sandboxes(now=1_700_000_000)
        assert canary_inode not in observed.opened
        assert observed.flock_attempts == []
        container.stop.assert_not_called()
        assert container.status == "running"
        assert CHAT not in manager._FLOCK_DEPTH
        _assert_retired(observed)
    assert canary.read_bytes() == b"REAP-CANARY"
    assert _inode(canary) == canary_inode
    if kind == "directory":
        leaf.rmdir()
    else:
        leaf.unlink()
    manager.reap_known_sandboxes(now=1_700_000_000)
    container.stop.assert_called_once()
    assert container.status == "exited"


def _fifo_child(route, create):
    import docker_manager as manager

    data = manager.BASE_DATA_DIR
    root = data / CHAT
    root.mkdir(parents=True, exist_ok=True)
    container = _prepare_reap(manager) if route == "reap" else None
    leaf = root / ".lifecycle.lock"
    leaf.unlink(missing_ok=True)
    os.mkfifo(leaf)
    admitted = []
    with pytest.MonkeyPatch.context() as patch:
        with _lock_observations(manager, patch) as observed:
            if route == "reap":
                manager.reap_known_sandboxes(now=1_700_000_000)
                container.stop.assert_not_called()
            else:
                with pytest.raises(OSError):
                    with manager._combined_lock(CHAT, create=create):
                        admitted.append(True)
            assert admitted == []
            assert observed.flock_attempts == []
            assert CHAT not in manager._FLOCK_DEPTH
            _assert_retired(observed)
    assert stat.S_ISFIFO(leaf.lstat().st_mode)
    leaf.unlink()
    _assert_another_thread_acquires(manager)
    print(json.dumps({"rejected": True, "reacquired": True, "route": route, "create": create}))


@pytest.mark.parametrize("route,create", (("direct", True), ("direct", False), ("reap", True)))
def test_fifo_lock_rejection_is_bounded_without_flock(world, route, create):
    _store, _manager, data = world
    child = subprocess.Popen(
        [sys.executable, "-c",
         "from tests.orchestrator.test_lifecycle_lock import _fifo_child; "
         f"_fifo_child({route!r}, {create!r})"],
        env=_child_env(data, BASE_DATA_DIR=str(data)), cwd=str(ROOT),
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = child.communicate(timeout=8)
        assert child.returncode == 0, (stdout, stderr)
        assert json.loads(stdout.strip().splitlines()[-1]) == {
            "rejected": True, "reacquired": True, "route": route, "create": create,
        }
    finally:
        _stop_child(child)


@pytest.mark.parametrize("create", (True, False))
def test_chat_replacement_after_pin_cannot_redirect_lock(world, monkeypatch, create):
    _store, manager, data = world
    root = data / CHAT
    root.mkdir(parents=True)
    leaf = root / ".lifecycle.lock"
    leaf.write_bytes(b"PINNED-LOCK")
    pinned_inode = _inode(leaf)
    external = data.parent / "replacement"
    external.mkdir()
    canary = external / ".lifecycle.lock"
    canary.write_bytes(b"EXTERNAL-PARENT-CANARY")
    canary_inode = _inode(canary)
    detached = data.parent / "pinned-chat"
    swapped = []
    with _lock_observations(manager, monkeypatch) as observed:
        real_open = os.open

        def replace_after_pin(name, *args, **kwargs):
            fd = real_open(name, *args, **kwargs)
            if os.fsdecode(name) == CHAT and not swapped:
                root.rename(detached)
                root.symlink_to(external, target_is_directory=True)
                swapped.append(True)
            return fd

        with monkeypatch.context() as fault:
            fault.setattr(os, "open", replace_after_pin)
            with manager._combined_lock(CHAT, create=create) as held:
                assert held is not None
                assert (os.fstat(held._file.fileno()).st_dev,
                        os.fstat(held._file.fileno()).st_ino) == pinned_inode
        assert swapped == [True]
        assert canary_inode not in observed.opened
        assert observed.flocked == [
            (manager.fcntl.LOCK_EX, pinned_inode), (manager.fcntl.LOCK_UN, pinned_inode),
        ]
        _assert_retired(observed)
    assert canary.read_bytes() == b"EXTERNAL-PARENT-CANARY"
    assert _inode(canary) == canary_inode
    assert {path.name for path in external.iterdir()} == {".lifecycle.lock"}
    assert (detached / ".lifecycle.lock").read_bytes() == b"PINNED-LOCK"


@pytest.mark.parametrize("alias", (False, True))
@pytest.mark.parametrize("create", (False, True))
def test_missing_lock_creation_preserves_umask_and_trusted_base(world, alias, create):
    _store, manager, data = world
    if alias:
        target = data.parent / "trusted-data"
        target.mkdir()
        data.symlink_to(target, target_is_directory=True)
    root = data / CHAT
    root.mkdir(parents=True)
    previous = os.umask(0o027)
    try:
        with manager._combined_lock(CHAT, create=create) as held:
            assert held is not None
            assert stat.S_IMODE((root / ".lifecycle.lock").stat().st_mode) == 0o640
            assert stat.S_ISREG(os.fstat(held._file.fileno()).st_mode)
    finally:
        os.umask(previous)
    if alias:
        assert data.is_symlink()
        assert data.readlink() == target
    assert CHAT not in manager._FLOCK_DEPTH


@pytest.mark.parametrize("root_kind", ("missing-base", "missing-chat", "symlink", "file"))
def test_noncreating_missing_or_unsafe_chat_does_not_create_lock(world, monkeypatch, root_kind):
    _store, manager, data = world
    root = data / CHAT
    external = data.parent / "unsafe-root"
    if root_kind != "missing-base":
        data.mkdir()
    if root_kind == "symlink":
        external.mkdir()
        root.symlink_to(external, target_is_directory=True)
    elif root_kind == "file":
        root.write_bytes(b"NOT-A-CHAT")
    with _lock_observations(manager, monkeypatch) as observed:
        with manager._combined_lock(CHAT, create=False) as held:
            assert held is None
        assert observed.flock_attempts == []
        assert CHAT not in manager._FLOCK_DEPTH
        _assert_retired(observed)
    if root_kind == "missing-base":
        assert not data.exists()
    elif root_kind == "missing-chat":
        assert list(data.iterdir()) == []
    elif root_kind == "symlink":
        assert root.is_symlink()
        assert list(external.iterdir()) == []
        root.unlink()
    else:
        assert root.read_bytes() == b"NOT-A-CHAT"
        root.unlink()
    _assert_another_thread_acquires(manager)


@pytest.mark.parametrize("create", (True, False))
@pytest.mark.parametrize("stage", (
    "base-open", "chat-open", "leaf-open", "fstat", "fdopen", "flock",
    "root-close", "base-close", "fstat-root-close", "fstat-base-close",
))
def test_failed_acquisition_preserves_error_and_retires_resources(world, monkeypatch, create, stage):
    _store, manager, data = world
    root = data / CHAT
    root.mkdir(parents=True)
    leaf = root / ".lifecycle.lock"
    leaf.write_bytes(b"KEEP-LOCK-BYTES")
    identities = {"base": _inode(data), "root": _inode(root), "leaf": _inode(leaf)}
    error = OSError(errno.EIO, f"{stage} acquisition failed")
    cleanup_error = OSError(errno.EIO, "directory cleanup failed")
    faults = []
    admitted = []
    with _lock_observations(manager, monkeypatch) as observed:
        real_open, real_fstat, real_fdopen = os.open, os.fstat, os.fdopen
        real_flock, real_close = manager.fcntl.flock, os.close

        def inode(fd):
            info = real_fstat(fd)
            return info.st_dev, info.st_ino

        def failing_open(name, *args, **kwargs):
            target = {"base-open": str(data), "chat-open": CHAT, "leaf-open": ".lifecycle.lock"}
            if stage in target and os.fsdecode(name) == target[stage]:
                faults.append(stage)
                raise error
            return real_open(name, *args, **kwargs)

        def failing_fstat(fd):
            if stage.startswith("fstat") and inode(fd) == identities["leaf"]:
                faults.append("fstat")
                raise error
            return real_fstat(fd)

        def failing_fdopen(fd, *args, **kwargs):
            if stage == "fdopen":
                faults.append(stage)
                raise error
            return real_fdopen(fd, *args, **kwargs)

        def failing_flock(fd, operation):
            if stage == "flock" and operation == manager.fcntl.LOCK_EX:
                faults.append(stage)
                raise error
            return real_flock(fd, operation)

        def failing_close(fd):
            identity = inode(fd)
            result = real_close(fd)
            if stage.endswith("root-close") and identity == identities["root"]:
                faults.append("root-close")
                raise cleanup_error if stage.startswith("fstat") else error
            if stage.endswith("base-close") and identity == identities["base"]:
                faults.append("base-close")
                raise cleanup_error if stage.startswith("fstat") else error
            return result

        with monkeypatch.context() as fault:
            fault.setattr(os, "open", failing_open)
            fault.setattr(os, "fstat", failing_fstat)
            fault.setattr(os, "fdopen", failing_fdopen)
            fault.setattr(manager.fcntl, "flock", failing_flock)
            fault.setattr(os, "close", failing_close)
            with pytest.raises(OSError) as failure:
                with manager._combined_lock(CHAT, create=create):
                    admitted.append(True)
        assert failure.value is error
        assert faults == (
            ["fstat", stage.removeprefix("fstat-")] if stage.startswith("fstat-") else [stage]
        )
        assert admitted == []
        assert observed.flocked == []
        assert CHAT not in manager._FLOCK_DEPTH
        _assert_retired(observed)
    assert leaf.read_bytes() == b"KEEP-LOCK-BYTES"
    assert _inode(leaf) == identities["leaf"]
    _assert_another_thread_acquires(manager)


@pytest.mark.parametrize("directory", ("root", "base"))
def test_directory_close_error_inside_handled_exception_still_refuses_admission(
    world, monkeypatch, directory,
):
    _store, manager, data = world
    root = data / CHAT
    root.mkdir(parents=True)
    expected_inode = _inode(root if directory == "root" else data)
    error = OSError(errno.EIO, "directory close failed inside caller exception handler")
    faults = []
    admitted = []
    with _lock_observations(manager, monkeypatch) as observed:
        real_close = os.close
        real_fstat = os.fstat

        def failing_close(fd):
            info = real_fstat(fd)
            result = real_close(fd)
            if (info.st_dev, info.st_ino) == expected_inode:
                faults.append(directory)
                raise error
            return result

        with monkeypatch.context() as fault:
            fault.setattr(os, "close", failing_close)
            try:
                raise ValueError("already handled caller error")
            except ValueError:
                with pytest.raises(OSError) as failure:
                    with manager._combined_lock(CHAT):
                        admitted.append(True)
        assert failure.value is error
        assert faults == [directory]
        assert admitted == []
        assert observed.flock_attempts == []
        assert CHAT not in manager._FLOCK_DEPTH
        _assert_retired(observed)
    _assert_another_thread_acquires(manager)


def test_directory_close_error_during_missing_chat_return_is_visible(world, monkeypatch):
    _store, manager, data = world
    data.mkdir()
    expected_inode = _inode(data)
    error = OSError(errno.EIO, "base close failed before missing-chat return")
    faults = []
    admitted = []
    with _lock_observations(manager, monkeypatch) as observed:
        real_close = os.close
        real_fstat = os.fstat

        def failing_close(fd):
            info = real_fstat(fd)
            result = real_close(fd)
            if (info.st_dev, info.st_ino) == expected_inode:
                faults.append(True)
                raise error
            return result

        with monkeypatch.context() as fault:
            fault.setattr(os, "close", failing_close)
            with pytest.raises(OSError) as failure:
                with manager._combined_lock(CHAT, create=False):
                    admitted.append(True)
        assert failure.value is error
        assert faults == [True]
        assert admitted == []
        assert observed.flock_attempts == []
        assert CHAT not in manager._FLOCK_DEPTH
        _assert_retired(observed)
    assert list(data.iterdir()) == []
    _assert_another_thread_acquires(manager)


def test_removed_pinned_chat_returns_none_without_recreation(world, monkeypatch):
    _store, manager, data = world
    root = data / CHAT
    root.mkdir(parents=True)
    removed = []
    with _lock_observations(manager, monkeypatch) as observed:
        real_open = os.open

        def remove_after_pin(name, *args, **kwargs):
            fd = real_open(name, *args, **kwargs)
            if os.fsdecode(name) == CHAT:
                root.rmdir()
                removed.append(True)
            return fd

        with monkeypatch.context() as fault:
            fault.setattr(os, "open", remove_after_pin)
            with manager._combined_lock(CHAT, create=False) as held:
                assert held is None
        assert removed == [True]
        assert observed.flock_attempts == []
        assert CHAT not in manager._FLOCK_DEPTH
        _assert_retired(observed)
    assert not root.exists()
    assert list(data.iterdir()) == []
    _assert_another_thread_acquires(manager)


@pytest.mark.parametrize("create", (True, False))
@pytest.mark.parametrize("replacement", ("regular", "symlink"))
def test_concurrent_lock_creator_winner_is_verified_before_flock(
    world, monkeypatch, create, replacement,
):
    _store, manager, data = world
    root = data / CHAT
    root.mkdir(parents=True)
    leaf = root / ".lifecycle.lock"
    canary = data.parent / "creation-winner-canary"
    canary.write_bytes(b"EXTERNAL-CREATION-CANARY")
    canary_inode = _inode(canary)
    winner_inode = []
    conflicts = []
    admitted = []
    with _lock_observations(manager, monkeypatch) as observed:
        real_open = os.open
        real_close = os.close

        def install_winner(name, flags, *args, **kwargs):
            if os.fsdecode(name) == ".lifecycle.lock" and flags & os.O_EXCL and not winner_inode:
                # An independent real creator wins before the owner's exclusive create.
                creator_fd = real_open(
                    name, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o666, **kwargs)
                try:
                    os.write(creator_fd, b"CONCURRENT-WINNER")
                    info = os.fstat(creator_fd)
                    winner_inode.append((info.st_dev, info.st_ino))
                finally:
                    real_close(creator_fd)
                try:
                    return real_open(name, flags, *args, **kwargs)
                except FileExistsError:
                    conflicts.append(True)
                    if replacement == "symlink":
                        leaf.unlink()
                        leaf.symlink_to(canary)
                    raise
            return real_open(name, flags, *args, **kwargs)

        with monkeypatch.context() as fault:
            fault.setattr(os, "open", install_winner)
            if replacement == "regular":
                with manager._combined_lock(CHAT, create=create) as held:
                    assert held is not None
                    info = os.fstat(held._file.fileno())
                    admitted.append((info.st_dev, info.st_ino))
            else:
                with pytest.raises(OSError):
                    with manager._combined_lock(CHAT, create=create):
                        admitted.append(True)
        assert conflicts == [True]
        assert len(winner_inode) == 1
        assert canary_inode not in observed.opened
        if replacement == "regular":
            assert admitted == winner_inode
            assert observed.flocked == [
                (manager.fcntl.LOCK_EX, winner_inode[0]),
                (manager.fcntl.LOCK_UN, winner_inode[0]),
            ]
            assert leaf.read_bytes() == b"CONCURRENT-WINNER"
        else:
            assert admitted == []
            assert observed.flock_attempts == []
            assert leaf.is_symlink()
        assert CHAT not in manager._FLOCK_DEPTH
        _assert_retired(observed)
    assert canary.read_bytes() == b"EXTERNAL-CREATION-CANARY"
    assert _inode(canary) == canary_inode
    if replacement == "symlink":
        leaf.unlink()
    _assert_another_thread_acquires(manager)
