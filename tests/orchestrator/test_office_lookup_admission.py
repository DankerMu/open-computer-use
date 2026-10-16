# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Office lookup admission at its real filesystem, thread and process boundary."""
from __future__ import annotations

import errno
import fcntl
import importlib
import os
import subprocess
import sys
import threading
from contextlib import contextmanager

import pytest

from tests.orchestrator._office_store import (
    CHAT, OTHER_CHAT, SERVER_DIR, _child_env, _inode, _stop_child, _wait_marker,
    world as world,
)

LOCK_NAME = ".office-lookup.lock"


@pytest.fixture(autouse=True)
def _reload_lookup_admission(world):
    from office import lookup_admission

    importlib.reload(lookup_admission)


@contextmanager
def _descriptors(monkeypatch):
    real_open, real_close = os.open, os.close
    live = set()

    def opened(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        live.add(fd)
        return fd

    def closed(fd):
        result = real_close(fd)
        live.discard(fd)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", opened)
        patch.setattr(os, "close", closed)
        try:
            yield live
        finally:
            leaked = tuple(live)
            for fd in leaked:
                real_close(fd)
            assert leaked == (), f"lookup admission leaked descriptors: {leaked}"


@pytest.mark.parametrize("missing", ("base", "chat"))
def test_missing_lookup_root_is_not_created(world, missing):
    from office.lookup_admission import lookup_admission

    _store, _manager, data = world
    if missing == "chat":
        data.mkdir()
    with lookup_admission(CHAT) as admitted:
        assert admitted is None
    assert not (data / CHAT).exists()
    if missing == "base":
        assert not data.exists()
    else:
        assert list(data.iterdir()) == []


@pytest.mark.parametrize("configured_symlink", (False, True))
def test_creating_lookup_admission_preserves_stable_control_file(world, configured_symlink):
    from office.lookup_admission import lookup_admission

    _store, _manager, data = world
    if configured_symlink:
        target = data.parent / "configured-data"
        target.mkdir()
        data.symlink_to(target, target_is_directory=True)
    with lookup_admission(CHAT, create=True) as admitted:
        assert admitted is True
        inode = _inode(data / CHAT / LOCK_NAME)
    with lookup_admission(CHAT) as admitted:
        assert admitted is True
        assert _inode(data / CHAT / LOCK_NAME) == inode
    assert (data / CHAT / LOCK_NAME).read_bytes() == b""
    assert {path.name for path in (data / CHAT).iterdir()} == {LOCK_NAME}


def test_existing_chat_can_create_lookup_lock_without_creating_workspace(world):
    from office.lookup_admission import lookup_admission

    _store, _manager, data = world
    root = data / CHAT
    root.mkdir(parents=True)
    canary = root / "keep"
    canary.write_bytes(b"CHAT-CANARY")
    with lookup_admission(CHAT) as admitted:
        assert admitted is True
    assert {path.name for path in root.iterdir()} == {LOCK_NAME, "keep"}
    assert canary.read_bytes() == b"CHAT-CANARY"


@pytest.mark.parametrize("kind", ("root-link", "root-file", "lock-link", "lock-directory", "lock-fifo"))
@pytest.mark.parametrize("create", (False, True))
def test_unsafe_lookup_paths_refuse_admission_without_touching_canary(world, kind, create, monkeypatch):
    from office.lookup_admission import lookup_admission

    store, _manager, data = world
    data.mkdir()
    root = data / CHAT
    outside = data.parent / "outside"
    outside.mkdir()
    canary = outside / LOCK_NAME
    canary.write_bytes(b"EXTERNAL-CANARY")
    inode = _inode(canary)
    if kind == "root-link":
        root.symlink_to(outside, target_is_directory=True)
    elif kind == "root-file":
        root.write_bytes(b"NOT-A-CHAT")
    else:
        root.mkdir()
        leaf = root / LOCK_NAME
        if kind == "lock-link":
            leaf.symlink_to(canary)
        elif kind == "lock-directory":
            leaf.mkdir()
        else:
            os.mkfifo(leaf)
    entered = []
    with _descriptors(monkeypatch):
        with pytest.raises(store.StateCorruptError):
            with lookup_admission(CHAT, create=create):
                entered.append(True)
    assert entered == []
    assert canary.read_bytes() == b"EXTERNAL-CANARY"
    assert _inode(canary) == inode
    assert {path.name for path in outside.iterdir()} == {LOCK_NAME}


def test_lookup_waiter_leaves_callbacks_and_other_chats_independent(world, monkeypatch):
    from office.lookup_admission import lookup_admission

    _store, manager, data = world
    denied, entered, callback_done = (threading.Event() for _ in range(3))
    failures = []
    real_flock = fcntl.flock

    def observe_wait(fd, operation):
        if operation == fcntl.LOCK_EX and threading.current_thread().name == "lookup-waiter":
            try:
                real_flock(fd, operation | fcntl.LOCK_NB)
            except BlockingIOError:
                denied.set()
                return real_flock(fd, operation)
            raise AssertionError("same-chat observation bypassed the held admission")
        return real_flock(fd, operation)

    def waiter():
        try:
            with lookup_admission(CHAT) as admitted:
                assert admitted is True
                entered.set()
        except BaseException as exc:
            failures.append(exc)

    def callback():
        try:
            with manager._combined_lock(CHAT):
                callback_done.set()
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=waiter, name="lookup-waiter")
    callback_worker = threading.Thread(target=callback, name="lookup-callback")
    with _descriptors(monkeypatch), monkeypatch.context() as patch:
        patch.setattr(fcntl, "flock", observe_wait)
        try:
            with lookup_admission(CHAT, create=True):
                worker.start()
                assert denied.wait(2), failures
                assert not entered.is_set()
                callback_worker.start()
                assert callback_done.wait(2), "lookup waiter blocked canonical callback admission"
                with lookup_admission(OTHER_CHAT, create=True) as other:
                    assert other is True
                assert not entered.is_set()
            worker.join(2)
            callback_worker.join(2)
            assert not worker.is_alive() and not callback_worker.is_alive()
            assert entered.is_set() and failures == []
        finally:
            if worker.ident is not None:
                worker.join(5)
            if callback_worker.ident is not None:
                callback_worker.join(5)
            assert not worker.is_alive() and not callback_worker.is_alive()
    assert (data / CHAT / LOCK_NAME).is_file()


@pytest.mark.parametrize("replacement", ("lock", "root", "base"))
def test_waiting_lookup_refuses_replaced_identity(world, monkeypatch, replacement):
    from office.lookup_admission import lookup_admission

    store, _manager, data = world
    denied = threading.Event()
    failures, admissions = [], []
    real_flock = fcntl.flock

    def observe_wait(fd, operation):
        if operation == fcntl.LOCK_EX and threading.current_thread().name == "retired-lookup":
            try:
                real_flock(fd, operation | fcntl.LOCK_NB)
            except BlockingIOError:
                denied.set()
                return real_flock(fd, operation)
            raise AssertionError("replacement test did not contend on the original inode")
        return real_flock(fd, operation)

    def waiter():
        try:
            with lookup_admission(CHAT):
                admissions.append(True)
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=waiter, name="retired-lookup")
    with _descriptors(monkeypatch), monkeypatch.context() as patch:
        patch.setattr(fcntl, "flock", observe_wait)
        try:
            with lookup_admission(CHAT, create=True):
                worker.start()
                assert denied.wait(2), failures
                target = data / CHAT / LOCK_NAME if replacement == "lock" else (
                    data / CHAT if replacement == "root" else data
                )
                target.rename(target.with_name(target.name + "-retired"))
                if replacement == "lock":
                    target.write_bytes(b"REPLACEMENT-LOCK")
                else:
                    target.mkdir()
            worker.join(2)
            assert not worker.is_alive()
            assert admissions == []
            assert len(failures) == 1 and isinstance(failures[0], store.StateCorruptError)
            # The failed waiter must not poison the new lock domain or unlink it.
            with lookup_admission(CHAT, create=True) as admitted:
                assert admitted is True
            if replacement == "lock":
                assert target.read_bytes() == b"REPLACEMENT-LOCK"
        finally:
            if worker.ident is not None:
                worker.join(5)
            assert not worker.is_alive()


@pytest.mark.parametrize("stage", ("root-open", "lock-open", "lock-stat", "flock", "identity"))
def test_failed_lookup_acquisition_closes_every_owned_descriptor(world, monkeypatch, stage):
    from office.lookup_admission import lookup_admission

    store, _manager, data = world
    root = data / CHAT
    root.mkdir(parents=True)
    leaf = root / LOCK_NAME
    leaf.write_bytes(b"KEEP-LOCK")
    inode = _inode(leaf)
    entered, faults = [], []
    real_open, real_flock, real_stat = os.open, fcntl.flock, os.stat
    real_fstat = os.fstat

    def opened(name, *args, **kwargs):
        if (stage == "root-open" and name == CHAT) or (stage == "lock-open" and name == LOCK_NAME):
            faults.append(stage)
            raise OSError(errno.EIO, "injected acquisition failure")
        return real_open(name, *args, **kwargs)

    def fstated(fd):
        info = real_fstat(fd)
        if stage == "lock-stat" and (info.st_dev, info.st_ino) == inode:
            faults.append(stage)
            raise OSError(errno.EIO, "injected descriptor verification failure")
        return info

    def flocked(fd, operation):
        if stage == "flock" and operation == fcntl.LOCK_EX:
            faults.append(stage)
            raise OSError(errno.EIO, "injected flock failure")
        return real_flock(fd, operation)

    def stated(name, *args, **kwargs):
        if stage == "identity" and name == LOCK_NAME:
            faults.append(stage)
            raise OSError(errno.EIO, "injected identity failure")
        return real_stat(name, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(os, "open", opened)
        fault.setattr(os, "fstat", fstated)
        fault.setattr(fcntl, "flock", flocked)
        fault.setattr(os, "stat", stated)
        with _descriptors(monkeypatch):
            with pytest.raises(store.StateCorruptError):
                with lookup_admission(CHAT):
                    entered.append(True)
    assert entered == [] and faults == [stage]
    assert leaf.read_bytes() == b"KEEP-LOCK" and _inode(leaf) == inode
    with lookup_admission(CHAT) as admitted:
        assert admitted is True


@pytest.mark.parametrize("exit_kind", ("body-error", "unlock-error", "close-error"))
def test_lookup_exit_releases_lock_and_all_descriptors(world, monkeypatch, exit_kind):
    from office.lookup_admission import lookup_admission

    _store, _manager, data = world
    with lookup_admission(CHAT, create=True):
        pass
    leaf = data / CHAT / LOCK_NAME
    inode = _inode(leaf)
    real_flock, real_fstat = fcntl.flock, os.fstat
    faults, entered = [], []

    def flocked(fd, operation):
        if exit_kind == "unlock-error" and operation == fcntl.LOCK_UN:
            faults.append(exit_kind)
            raise OSError(errno.EIO, "injected unlock failure")
        return real_flock(fd, operation)

    expected = ValueError if exit_kind == "body-error" else OSError
    with _descriptors(monkeypatch), monkeypatch.context() as fault:
        # Keep descriptor tracking as the outer close wrapper, including injected failures.
        tracked_close = os.close

        def close_and_track(fd):
            info = real_fstat(fd)
            result = tracked_close(fd)
            if exit_kind == "close-error" and (info.st_dev, info.st_ino) == inode:
                faults.append(exit_kind)
                raise OSError(errno.EIO, "injected close failure after retirement")
            return result

        fault.setattr(fcntl, "flock", flocked)
        fault.setattr(os, "close", close_and_track)
        with pytest.raises(expected):
            with lookup_admission(CHAT):
                entered.append(True)
                if exit_kind == "body-error":
                    raise ValueError("caller failed")
    assert entered == [True]
    assert faults == ([] if exit_kind == "body-error" else [exit_kind])
    assert _inode(leaf) == inode
    with lookup_admission(CHAT) as admitted:
        assert admitted is True


_PROCESS = r'''
import fcntl
import os
import sys
from pathlib import Path

os.environ["BASE_DATA_DIR"] = os.environ["OCU_BASE"]
from office.lookup_admission import lookup_admission

op = os.environ["OCU_GATE_OP"]
if op == "wait":
    original = fcntl.flock
    def contend(fd, operation):
        if operation == fcntl.LOCK_EX:
            try:
                original(fd, operation | fcntl.LOCK_NB)
            except BlockingIOError:
                Path(os.environ["OCU_CONTENDED"]).write_text("1")
                return original(fd, operation)
            raise AssertionError("cross-process lookup bypassed held admission")
        return original(fd, operation)
    fcntl.flock = contend
with lookup_admission(os.environ["OCU_CHAT"], create=False) as admitted:
    assert admitted is True
    Path(os.environ["OCU_ENTERED"]).write_text("1")
    if op == "hold":
        assert sys.stdin.readline() == "release\n"
'''


@pytest.mark.parametrize("release", ("normal", "process-death"))
def test_process_lookup_waiter_resumes_on_release_or_owner_death(world, release):
    from office.lookup_admission import lookup_admission

    _store, _manager, data = world
    with lookup_admission(CHAT, create=True):
        pass
    inode = _inode(data / CHAT / LOCK_NAME)
    held, entered, contended = (data.parent / name for name in ("held", "entered", "contended"))
    holder = waiter = None

    def child(op, marker):
        return subprocess.Popen(
            [sys.executable, "-c", _PROCESS], cwd=str(SERVER_DIR),
            env=_child_env(data, OCU_GATE_OP=op, OCU_ENTERED=str(marker), OCU_CONTENDED=str(contended)),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    try:
        holder = child("hold", held)
        _wait_marker(held, holder, "lookup owner did not acquire")
        waiter = child("wait", entered)
        _wait_marker(contended, waiter, "lookup waiter did not observe cross-process contention")
        assert not entered.exists()
        if release == "normal":
            holder.stdin.write("release\n")
            holder.stdin.flush()
            stdout, stderr = holder.communicate(timeout=5)
            assert holder.returncode == 0, (stdout, stderr)
        else:
            holder.kill()
            holder.wait(timeout=5)
            assert holder.returncode < 0
        stdout, stderr = waiter.communicate(timeout=5)
        assert waiter.returncode == 0, (stdout, stderr)
        assert entered.read_text() == "1"
        assert _inode(data / CHAT / LOCK_NAME) == inode
    finally:
        _stop_child(holder)
        _stop_child(waiter)
