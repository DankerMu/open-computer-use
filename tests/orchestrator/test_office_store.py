# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Real-filesystem contract tests for the Office per-chat state file.

The public seam is ``office.store.OfficeStore``.  Tests use real paths,
descriptors, atomic replacement and separate processes.  Docker is forbidden.
"""
from __future__ import annotations

import errno
import importlib
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = ROOT / "computer-use-server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

CHAT = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
OTHER_CHAT = "b2c3d4e5-f6a7-8901-bcde-f12345678901"
NO_DOCKER_SOCKET = "unix:///tmp/ocu-acceptance-no-docker.sock"
EMPTY = {
    "schema_version": 1,
    "documents": {},
    "sessions": {},
    "receipts": {},
    "journal": {},
}

# Shared child protocol for this module only.  Do not import the broker tests.
_PROCESS = r'''
import fcntl
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ["OCU_SERVER_DIR"])
os.environ["BASE_DATA_DIR"] = os.environ["OCU_BASE"]
os.environ["DOCKER_HOST"] = "unix:///tmp/ocu-acceptance-no-docker.sock"
os.environ["DOCKER_SOCKET"] = "unix:///tmp/ocu-acceptance-no-docker.sock"

from office.store import OfficeStore
import office.store as store_mod

chat = os.environ["OCU_CHAT"]
op = os.environ["OCU_CHILD_OP"]
store = OfficeStore()

def put(collection, token):
    def mutate(state):
        state[collection][token] = {"id": token}
    return store.update(chat, mutate)

if op == "update":
    print(json.dumps(put(os.environ["OCU_COLLECTION"], os.environ["OCU_TOKEN"])))
elif op == "read":
    print(json.dumps(store.read(chat)))
elif op == "crash-before-replace":
    original = store_mod.os.replace

    def hold(src, dst, *args, **kwargs):
        Path(os.environ["OCU_SEAM"]).write_text("1", encoding="utf-8")
        fd = os.open(os.environ["OCU_HOLD"], os.O_RDONLY)
        os.close(fd)
        return original(src, dst, *args, **kwargs)

    store_mod.os.replace = hold
    print(json.dumps(put("documents", "lost")))
elif op == "wait-lock":
    original_flock = fcntl.flock

    def contend_then_block(fd, operation):
        if operation == fcntl.LOCK_EX:
            try:
                original_flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                Path(os.environ["OCU_CONTENDED"]).write_text("1", encoding="utf-8")
                return original_flock(fd, fcntl.LOCK_EX)
            try:
                original_flock(fd, fcntl.LOCK_UN)
            finally:
                raise AssertionError(os.environ["OCU_LOCK_DENIAL"])
        return original_flock(fd, operation)

    fcntl.flock = contend_then_block
    print(json.dumps(put(os.environ["OCU_COLLECTION"], os.environ["OCU_TOKEN"])))
else:
    raise SystemExit("unknown child op")
'''


@pytest.fixture
def world(monkeypatch, tmp_path):
    data = tmp_path / "data"
    monkeypatch.setenv("BASE_DATA_DIR", str(data))
    monkeypatch.setenv("DOCKER_HOST", NO_DOCKER_SOCKET)
    monkeypatch.setenv("DOCKER_SOCKET", NO_DOCKER_SOCKET)

    import docker_manager
    import office.store as store_mod

    prior_base = docker_manager.BASE_DATA_DIR
    importlib.reload(docker_manager)
    importlib.reload(store_mod)
    docker_manager._chat_locks.clear()
    docker_manager._FLOCK_DEPTH.clear()
    docker_manager._docker_client = None
    try:
        yield store_mod, docker_manager, data
    finally:
        docker_manager._FLOCK_DEPTH.clear()
        docker_manager._chat_locks.clear()
        docker_manager._docker_client = None
        docker_manager.BASE_DATA_DIR = prior_base


def _state(data: Path, chat: str = CHAT) -> Path:
    return data / chat / ".ocu" / "office" / "state.json"


def _outputs(data: Path, chat: str = CHAT) -> Path:
    return data / chat / "outputs"


def _child_env(data: Path, **extra: str) -> dict[str, str]:
    environment = os.environ.copy()
    pythonpath = environment.get("PYTHONPATH", "")
    environment.update(
        {
            "OCU_SERVER_DIR": str(SERVER_DIR),
            "OCU_BASE": str(data),
            "OCU_CHAT": CHAT,
            "DOCKER_HOST": NO_DOCKER_SOCKET,
            "DOCKER_SOCKET": NO_DOCKER_SOCKET,
            "PYTHONPATH": str(SERVER_DIR) + (os.pathsep + pythonpath if pythonpath else ""),
        }
    )
    environment.update(extra)
    return environment


def _run_child(data: Path, **extra: str) -> dict:
    completed = subprocess.run(
        [sys.executable, "-c", _PROCESS],
        cwd=str(SERVER_DIR),
        env=_child_env(data, **extra),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _stop_child(child) -> None:
    if child is None:
        return
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
    try:
        child.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)


def _seed(store_mod, token: str = "seed") -> dict:
    def mutate(state):
        state["documents"][token] = {"id": token}

    return store_mod.OfficeStore().update(CHAT, mutate)


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
    _run_child(data, OCU_CHILD_OP="update", OCU_COLLECTION=first, OCU_TOKEN="one")
    _run_child(data, OCU_CHILD_OP="update", OCU_COLLECTION=second, OCU_TOKEN="two")
    persisted = _run_child(data, OCU_CHILD_OP="read")
    assert persisted[first]["one"] == {"id": "one"}
    assert persisted[second]["two"] == {"id": "two"}
    assert store_mod.OfficeStore().read(CHAT) == persisted


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
            child = subprocess.Popen(
                [sys.executable, "-c", _PROCESS],
                cwd=str(SERVER_DIR),
                env=_child_env(
                    data,
                    OCU_CHILD_OP="wait-lock",
                    OCU_COLLECTION="sessions",
                    OCU_TOKEN="waiter",
                    OCU_CONTENDED=str(contended),
                    OCU_LOCK_DENIAL="office lock was granted without contention",
                ),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 5
            while not contended.exists():
                if time.monotonic() >= deadline or child.poll() is not None:
                    raise AssertionError(
                        ("update did not contend for the lifecycle flock", child.poll())
                    )
                time.sleep(0.005)
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
        child = subprocess.Popen(
            [sys.executable, "-c", _PROCESS],
            cwd=str(SERVER_DIR),
            env=_child_env(
                data,
                OCU_CHILD_OP="crash-before-replace",
                OCU_SEAM=str(seam),
                OCU_HOLD=str(hold),
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 5
        while not seam.exists():
            if time.monotonic() >= deadline or child.poll() is not None:
                raise AssertionError(("child did not reach pre-replace seam", child.poll()))
            time.sleep(0.005)
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
    original_fsync = store_mod.os.fsync

    def fail_directory_sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("controlled directory fsync failure")
        return original_fsync(fd)

    with monkeypatch.context() as patches:
        patches.setattr(store_mod.os, "fsync", fail_directory_sync)
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
