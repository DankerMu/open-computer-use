# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Real filesystem readiness contract for the embedded crash-worker writer."""
from __future__ import annotations

import json
import os
import select
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tests.orchestrator._office_store import CHAT, SERVER_DIR, _MARKER_WRITER, _stop_child
from tests.orchestrator import test_office_control_plane_concurrency as contention
from tests.orchestrator.test_office_control_plane_concurrency import (
    _MARKER_NAMESPACE,
    _PROCESS_HOLDER,
    _record_contention,
)
from tests.orchestrator.test_office_sessions import office_world as office_world


_HELD_IO = r'''
import io, os, sys
from pathlib import Path

test_open, test_text_open, test_write = os.open, io.open, os.write

def held():
    print("opened", flush=True)
    assert sys.stdin.readline() == "release\n"

marker_parent, marker_name = os.path.split(sys.argv[1])

def marker_open(path):
    # Integer fds (os.fdopen) and unrelated paths, such as lock files, pass through.
    if not isinstance(path, (str, os.PathLike)):
        return False
    path = os.fspath(path)
    if not isinstance(path, str):
        return False
    parent, name = os.path.split(path)
    return parent == marker_parent and (
        name == marker_name or name.startswith("." + marker_name + "."))

def opened(*args, **kwargs):
    fd = test_open(*args, **kwargs)
    if marker_open(args[0]):
        held()
    return fd

def text_opened(*args, **kwargs):
    stream = test_text_open(*args, **kwargs)
    if marker_open(args[0]):
        held()
    return stream

def short_write(fd, body):
    return test_write(fd, body[:int(sys.argv[3])])

os.open, io.open, os.write = opened, text_opened, short_write
'''


@pytest.mark.parametrize("write_limit", (1, 7))
def test_incomplete_marker_is_not_ready_then_publishes_exact_text(tmp_path, write_limit):
    marker = tmp_path / "ready"
    cut = "retirement-durability-after"
    child = subprocess.Popen(
        [sys.executable, "-c", _HELD_IO + _MARKER_WRITER + '\n_publish_marker(Path(sys.argv[1]), sys.argv[2])\n',
         str(marker), cut, str(write_limit)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        readable, _, _ = select.select([child.stdout], [], [], 5)
        assert readable, "marker writer did not reach the held-open boundary"
        assert os.read(child.stdout.fileno(), 7) == b"opened\n"
        assert not marker.exists(), "an incomplete cut marker must not satisfy readiness"
        stages = list(tmp_path.iterdir())
        assert len(stages) == 1
        stage = stages[0]
        assert stage.parent == marker.parent
        assert stage.read_bytes() == b""
        assert stat.S_IMODE(stage.stat().st_mode) == 0o600
        out, err = child.communicate("release\n", timeout=5)
        assert child.returncode == 0, (out, err)
        assert marker.read_text() == "retirement-durability-after"
        assert set(tmp_path.iterdir()) == {marker}
    finally:
        _stop_child(child)


def test_process_holder_entered_marker_is_absent_until_complete(tmp_path):
    markers = tmp_path / "markers"
    markers.mkdir()
    entered = markers / "callback-process-entered"
    release_path = tmp_path / "callback-process-release"
    env = os.environ.copy()
    pythonpath = env.get("PYTHONPATH", "")
    env.update(
        {
            "PYTHONPATH": str(SERVER_DIR) + (os.pathsep + pythonpath if pythonpath else ""),
            "BASE_DATA_DIR": str(tmp_path / "data"),
            "OCU_CHAT": CHAT,
            "ENTERED": str(entered),
            "RELEASE": str(release_path),
        }
    )
    child = subprocess.Popen(
        [sys.executable, "-c", _HELD_IO + _PROCESS_HOLDER, str(entered), "held", "1"],
        cwd=str(SERVER_DIR), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        readable, _, _ = select.select([child.stdout], [], [], 5)
        assert readable, "process holder did not reach the held-open boundary"
        assert os.read(child.stdout.fileno(), 7) == b"opened\n"
        assert not entered.exists(), "an incomplete holder marker must not satisfy readiness"
        stages = list(markers.iterdir())
        assert len(stages) == 1
        stage = stages[0]
        assert stage.name.startswith(".callback-process-entered.") and stage.suffix == ".stage"
        assert stage.read_bytes() == b""
        assert stat.S_IMODE(stage.stat().st_mode) == 0o600
        release_path.write_text("release", encoding="utf-8")
        out, err = child.communicate("release\n", timeout=10)
        assert child.returncode == 0, (out, err)
        assert out == "released\n"
        assert entered.read_text(encoding="utf-8") == "held"
        assert set(markers.iterdir()) == {entered}
    finally:
        _stop_child(child)


def test_callback_contention_marker_is_absent_until_complete(tmp_path, monkeypatch):
    markers = tmp_path / "markers"
    markers.mkdir()
    contended = markers / "callback-process-contended"
    reached = threading.Event()
    release = threading.Event()
    errors = []
    original_write = _MARKER_NAMESPACE["_marker_write"]

    def held_write(fd, body):
        reached.set()
        if not release.wait(10):
            raise AssertionError("contention marker hold was never released")
        return original_write(fd, body)

    def publish():
        try:
            _record_contention(contended, "denied")
        except BaseException as error:
            errors.append(error)
        finally:
            reached.set()

    monkeypatch.setitem(_MARKER_NAMESPACE, "_marker_write", held_write)
    writer = threading.Thread(target=publish, daemon=True)
    writer.start()
    try:
        assert reached.wait(5), "contention marker writer did not reach the held write"
        assert not contended.exists(), "an incomplete contention marker must not satisfy readiness"
        stages = list(markers.iterdir())
        assert len(stages) == 1
        stage = stages[0]
        assert stage.name.startswith(".callback-process-contended.") and stage.suffix == ".stage"
        assert stage.read_bytes() == b""
        assert stat.S_IMODE(stage.stat().st_mode) == 0o600
        release.set()
        writer.join(timeout=5)
        assert not writer.is_alive()
        assert errors == []
        assert contended.read_text(encoding="utf-8") == "denied"
        assert set(markers.iterdir()) == {contended}
    finally:
        release.set()
        writer.join(timeout=5)


def test_callback_contention_owner_publishes_denial_through_marker_writer(office_world, monkeypatch):
    original_record = contention._record_contention
    recorded = []

    def recording(path, outcome):
        recorded.append((Path(path).name, outcome))
        return original_record(path, outcome)

    monkeypatch.setattr(contention, "_record_contention", recording)
    contention.test_callback_waits_for_ordinary_process_holder_while_health_progresses(office_world)
    assert recorded == [("callback-process-contended", "denied")]


_SAVED_IO = r'''
import errno, io, json, os, stat, sys
from pathlib import Path

marker = Path(sys.argv[1])
trace, hooks = [], []
test_open, test_sync = os.open, os.fsync
test_close, test_replace, test_text_open = os.close, os.replace, io.open
stage, descriptor = None, None

def opened(path, flags, mode):
    global stage, descriptor
    stage = Path(path)
    descriptor = test_open(path, flags, mode)
    return descriptor

def stage_text():
    with test_text_open(stage, encoding="utf-8") as stream:
        return stream.read()

def synced(fd):
    test_sync(fd)
    trace.append({"operation": "sync", "text": stage_text(),
                  "mode": stat.S_IMODE(os.fstat(fd).st_mode), "ready": marker.exists()})

def closed(fd):
    test_close(fd)
    trace.append({"operation": "close", "ready": marker.exists()})

def replaced(source, destination):
    try:
        os.fstat(descriptor)
    except OSError as error:
        assert error.errno == errno.EBADF
    else:
        raise AssertionError("marker descriptor must close before publication")
    trace.append({"operation": "replace", "text": stage_text(),
                  "ready": marker.exists(), "same_directory": Path(source).parent == marker.parent})
    return test_replace(source, destination)

os.open, os.fsync, os.close, os.replace = opened, synced, closed, replaced
'''

_ARMED_IO = r'''
def armed(name, original):
    def observed(*args, **kwargs):
        hooks.append(name)
        return original(*args, **kwargs)
    return observed

for name in ("open", "write", "fsync", "close", "replace", "unlink"):
    setattr(os, name, armed(name, getattr(os, name)))
io.open = armed("text-open", io.open)
_publish_marker(marker, "lineage-before")
print(json.dumps({"trace": trace, "hooks": hooks}))
'''


def test_marker_bypasses_armed_hooks_and_syncs_private_text_before_close_and_publish(tmp_path):
    marker = tmp_path / "resolve-ready"
    completed = subprocess.run(
        [sys.executable, "-c", _SAVED_IO + _MARKER_WRITER + _ARMED_IO, str(marker)],
        capture_output=True, text=True, timeout=5,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert json.loads(completed.stdout) == {
        "trace": [
            {"operation": "sync", "text": "lineage-before", "mode": 0o600, "ready": False},
            {"operation": "close", "ready": False},
            {"operation": "replace", "text": "lineage-before", "ready": False, "same_directory": True},
        ],
        "hooks": [],
    }
    assert marker.read_text() == "lineage-before"
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert set(tmp_path.iterdir()) == {marker}


_FAILED_IO = r'''
import errno, json, os, sys
from pathlib import Path

marker = Path(sys.argv[1])
failure = sys.argv[2]
test_write, test_sync, test_replace = os.write, os.fsync, os.replace
wrote_prefix = False

def written(fd, body):
    global wrote_prefix
    if failure == "zero-write":
        return 0
    if failure == "partial-write":
        if wrote_prefix:
            raise OSError(errno.EIO, "marker write refused after partial text")
        wrote_prefix = True
        return test_write(fd, body[:3])
    return test_write(fd, body)

def synced(fd):
    if failure == "sync":
        raise OSError(errno.EIO, "marker sync refused")
    return test_sync(fd)

def replaced(source, destination):
    if failure == "replace":
        raise OSError(errno.EIO, "marker publication refused")
    return test_replace(source, destination)

os.write, os.fsync, os.replace = written, synced, replaced
'''

_REPORT_FAILURE = r'''
try:
    _publish_marker(marker, "registration-durability-after")
except OSError as error:
    result = error.errno
else:
    result = None
print(json.dumps({"error": result, "ready": marker.exists(),
                  "remaining": sorted(path.name for path in marker.parent.iterdir())}))
'''


@pytest.mark.parametrize("failure", ("zero-write", "partial-write", "sync", "replace"))
def test_marker_io_failure_never_announces_readiness_and_reclaims_its_stage(tmp_path, failure):
    marker = tmp_path / "ready"
    unrelated = tmp_path / "caller-owned"
    unrelated.write_bytes(b"must survive failed marker publication")
    completed = subprocess.run(
        [sys.executable, "-c", _FAILED_IO + _MARKER_WRITER + _REPORT_FAILURE, str(marker), failure],
        capture_output=True, text=True, timeout=5,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert json.loads(completed.stdout) == {"error": 5, "ready": False, "remaining": ["caller-owned"]}
    assert unrelated.read_bytes() == b"must survive failed marker publication"
