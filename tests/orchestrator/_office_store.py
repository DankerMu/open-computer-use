# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Shared child protocol and process helpers for Office store tests."""
from __future__ import annotations

import errno
import importlib
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
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
import office.versions as versions_mod
import office.workspace as workspace_mod
import docker_manager

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
elif op == "hold-lock":
    def hold_then_put(state):
        state[os.environ["OCU_COLLECTION"]][os.environ["OCU_TOKEN"]] = {"id": os.environ["OCU_TOKEN"]}
        Path(os.environ["OCU_ENTERED"]).write_text("1", encoding="utf-8")
        fd = os.open(os.environ["OCU_HOLD"], os.O_RDONLY)
        os.close(fd)
    print(json.dumps(store.update(chat, hold_then_put)))
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
elif op == "store-version":
    receipt = json.loads(os.environ["OCU_RECEIPT"]) if os.environ.get("OCU_RECEIPT") else None
    parent = json.loads(os.environ["OCU_PARENT"]) if "OCU_PARENT" in os.environ else None
    record = store.store_version(
        chat,
        os.environ["OCU_FILE_ID"],
        os.environ["OCU_CONTENT"].encode("utf-8"),
        source=os.environ["OCU_SOURCE"],
        parent=parent,
        published=os.environ.get("OCU_PUBLISHED", "0") == "1",
        min_free_bytes=int(os.environ.get("OCU_FLOOR", "0")),
        receipt=receipt,
    )
    print(json.dumps({"record": record, "state": store.read(chat)}))
elif op == "get-receipt":
    expected = os.environ.get("OCU_EXPECTED_HASH") or None
    print(json.dumps(store.get_receipt(
        chat,
        os.environ["OCU_SESSION"],
        int(os.environ["OCU_SEQ"]),
        expected,
    )))
elif op == "hold-then-store":
    receipt = json.loads(os.environ["OCU_RECEIPT"]) if os.environ.get("OCU_RECEIPT") else None
    parent = json.loads(os.environ["OCU_PARENT"]) if "OCU_PARENT" in os.environ else None
    with docker_manager._combined_lock(chat):
        Path(os.environ["OCU_ENTERED"]).write_text("1", encoding="utf-8")
        fd = os.open(os.environ["OCU_HOLD"], os.O_RDONLY)
        os.close(fd)
        record = store.store_version(
            chat,
            os.environ["OCU_FILE_ID"],
            os.environ["OCU_CONTENT"].encode("utf-8"),
            source=os.environ["OCU_SOURCE"],
            parent=parent,
            published=os.environ.get("OCU_PUBLISHED", "0") == "1",
            min_free_bytes=int(os.environ.get("OCU_FLOOR", "0")),
            receipt=receipt,
        )
        snapshot = store.read(chat)
    print(json.dumps({"record": record, "state": snapshot}))
elif op == "wait-then-store":
    original_flock = fcntl.flock

    def contend_then_block(fd, operation):
        if operation == fcntl.LOCK_EX:
            try:
                original_flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                Path(os.environ["OCU_CONTENDED"]).write_text("1", encoding="utf-8")
                fcntl.flock = original_flock
                return original_flock(fd, fcntl.LOCK_EX)
            try:
                original_flock(fd, fcntl.LOCK_UN)
            finally:
                raise AssertionError(os.environ["OCU_LOCK_DENIAL"])
        return original_flock(fd, operation)

    fcntl.flock = contend_then_block
    receipt = json.loads(os.environ["OCU_RECEIPT"]) if os.environ.get("OCU_RECEIPT") else None
    parent = json.loads(os.environ["OCU_PARENT"]) if "OCU_PARENT" in os.environ else None
    record = store.store_version(
        chat,
        os.environ["OCU_FILE_ID"],
        os.environ["OCU_CONTENT"].encode("utf-8"),
        source=os.environ["OCU_SOURCE"],
        parent=parent,
        published=os.environ.get("OCU_PUBLISHED", "0") == "1",
        min_free_bytes=int(os.environ.get("OCU_FLOOR", "0")),
        receipt=receipt,
    )
    print(json.dumps({"record": record, "state": store.read(chat)}))
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
    import office.versions as versions_mod
    import office.workspace as workspace_mod

    prior_base = docker_manager.BASE_DATA_DIR
    importlib.reload(docker_manager)
    importlib.reload(store_mod)
    importlib.reload(versions_mod)
    importlib.reload(workspace_mod)
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


def _start_child(data: Path, **extra: str):
    return subprocess.Popen(
        [sys.executable, "-c", _PROCESS],
        cwd=str(SERVER_DIR),
        env=_child_env(data, **extra),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_marker(path: Path, child, label: str) -> None:
    deadline = time.monotonic() + 5
    while not path.exists():
        if time.monotonic() >= deadline or child.poll() is not None:
            raise AssertionError((label, child.poll()))
        time.sleep(0.005)


def _inode(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino


def _role_inodes(data: Path) -> dict[str, tuple[int, int]]:
    office = _state(data).parent
    return {
        "base": _inode(data),
        "root": _inode(data / CHAT),
        "ocu": _inode(data / CHAT / ".ocu"),
        "office": _inode(office),
    }


def _seed(store_mod, token: str = "seed") -> dict:
    def mutate(state):
        state["documents"][token] = {"id": token}

    return store_mod.OfficeStore().update(CHAT, mutate)


@contextmanager
def _state_write_fault(
    monkeypatch, data, close_errors, *, primary_stage=None, primary=None,
    cause=None, select_successor=None,
):
    """Fault real writer IO and snapshot ownership before emergency cleanup."""
    roles = _role_inodes(data)
    opened, written, replaced, synced, closed = os.open, os.write, os.replace, os.fsync, os.close
    latest = {}
    candidate = {}
    temporary_fd = None
    selected = False
    faulted = False
    trace = {"owned": {}, "attempts": [], "retired": [], "replaced": False, "synced": False}

    def choose():
        nonlocal selected
        selected = True
        trace["owned"] = dict(candidate)

    def fail():
        nonlocal faulted
        faulted = True
        raise primary from cause

    def watch_open(path, flags, *args, **kwargs):
        nonlocal candidate, temporary_fd
        fd = opened(path, flags, *args, **kwargs)
        if not selected:
            info = os.fstat(fd)
            for role, identity in roles.items():
                if (info.st_dev, info.st_ino) == identity:
                    latest[role] = fd
            if str(path).startswith(".state.") and str(path).endswith(".tmp"):
                candidate = dict(latest)
                temporary_fd = fd
                if select_successor is None:
                    choose()
        return fd

    def watch_write(fd, content):
        if selected and primary_stage == "write" and fd == temporary_fd:
            fail()
        return written(fd, content)

    def watch_replace(src, dst, *args, **kwargs):
        if dst != "state.json":
            return replaced(src, dst, *args, **kwargs)
        if not selected and select_successor is not None:
            successor = json.loads((_state(data).parent / src).read_bytes())
            if select_successor(successor):
                choose()
        if selected and primary_stage == "replace":
            fail()
        result = replaced(src, dst, *args, **kwargs)
        if selected:
            trace["replaced"] = True
            trace["successor"] = json.loads(_state(data).read_bytes())
        return result

    def watch_sync(fd):
        if selected:
            role = next((role for role, owned in trace["owned"].items() if owned == fd), None)
            if primary_stage == role and role is not None:
                fail()
        result = synced(fd)
        if selected and trace["replaced"] and fd == trace["owned"]["office"]:
            trace["synced"] = True
        return result

    def watch_close(fd):
        if not selected or not (faulted or trace["synced"]):
            return closed(fd)
        role = next((
            role for role, owned in trace["owned"].items()
            if owned == fd and role not in trace["retired"]
        ), None)
        if role is None:
            return closed(fd)
        info = os.fstat(fd)
        assert (info.st_dev, info.st_ino) == roles[role]
        trace["attempts"].append(role)
        closed(fd)
        with pytest.raises(OSError) as retired:
            os.fstat(fd)
        assert retired.value.errno == errno.EBADF
        trace["retired"].append(role)
        if role in close_errors:
            raise close_errors[role]

    try:
        with monkeypatch.context() as patches:
            patches.setattr(os, "open", watch_open)
            patches.setattr(os, "write", watch_write)
            patches.setattr(os, "replace", watch_replace)
            patches.setattr(os, "fsync", watch_sync)
            patches.setattr(os, "close", watch_close)
            yield trace
    finally:
        trace["still_owned"] = []
        for role, fd in trace["owned"].items():
            if role in trace["retired"]:
                continue
            try:
                info = os.fstat(fd)
            except OSError:
                continue
            if (info.st_dev, info.st_ino) == roles[role]:
                trace["still_owned"].append(role)
        for role in trace["still_owned"]:
            closed(trace["owned"][role])
