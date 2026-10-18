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

import pytest

from tests.orchestrator._office_store import _MARKER_WRITER, _stop_child


_HELD_IO = r'''
import io, os, sys
from pathlib import Path

test_open, test_text_open, test_write = os.open, io.open, os.write

def held():
    print("opened", flush=True)
    assert sys.stdin.readline() == "release\n"

def opened(*args, **kwargs):
    fd = test_open(*args, **kwargs)
    held()
    return fd

def text_opened(*args, **kwargs):
    stream = test_text_open(*args, **kwargs)
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
