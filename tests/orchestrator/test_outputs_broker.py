# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Real-filesystem contract tests for the persisted OCU outputs broker.

The public seam is ``OutputsBroker``.  Tests deliberately use real paths,
file descriptors, atomic replacement, and separate Python processes; Docker is
forbidden by the fixture and by the tests.
"""
from __future__ import annotations

import errno
import hashlib
import importlib
import json
import os
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = ROOT / "computer-use-server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

CHAT = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
OTHER_CHAT = "b2c3d4e5-f6a7-8901-bcde-f12345678901"
NO_DOCKER_SOCKET = "unix:///tmp/ocu-acceptance-no-docker.sock"


@pytest.fixture
def world(monkeypatch, tmp_path):
    """Reload lifecycle configuration into one isolated real filesystem root."""
    data = tmp_path / "data"
    monkeypatch.setenv("BASE_DATA_DIR", str(data))
    monkeypatch.setenv("DOCKER_HOST", NO_DOCKER_SOCKET)
    monkeypatch.setenv("DOCKER_SOCKET", NO_DOCKER_SOCKET)

    import docker_manager
    import outputs_broker

    prior_base = docker_manager.BASE_DATA_DIR
    importlib.reload(docker_manager)
    # outputs_broker must retain the docker_manager module object, not aliases
    # captured before this reload.
    importlib.reload(outputs_broker)
    docker_manager._chat_locks.clear()
    docker_manager._FLOCK_DEPTH.clear()
    docker_manager._docker_client = None
    try:
        yield outputs_broker, docker_manager, data
    finally:
        docker_manager._FLOCK_DEPTH.clear()
        docker_manager._chat_locks.clear()
        docker_manager._docker_client = None
        docker_manager.BASE_DATA_DIR = prior_base


def _outputs(data: Path, chat: str = CHAT) -> Path:
    return data / chat / "outputs"


def _index(data: Path, chat: str = CHAT) -> Path:
    return data / chat / ".ocu" / "index.json"


def _put(data: Path, relative_path: str, body: bytes, chat: str = CHAT) -> Path:
    path = _outputs(data, chat) / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _entry_by_path(listing: dict, path: str) -> dict:
    return next(entry for entry in listing["entries"] if entry["path"] == path)


def _ids(listing: dict) -> dict[str, str]:
    return {entry["path"]: entry["file_id"] for entry in listing["entries"]}


def _read_index(data: Path, chat: str = CHAT) -> dict:
    return json.loads(_index(data, chat).read_text(encoding="utf-8"))


def _assert_uuid(value: str) -> None:
    assert str(uuid.UUID(value)) == value


def _child_environment(data: Path, shared: Path, *, disabled_lock: bool) -> dict[str, str]:
    environment = os.environ.copy()
    pythonpath = environment.get("PYTHONPATH", "")
    environment.update(
        {
            "OCU_SERVER_DIR": str(SERVER_DIR),
            "OCU_BASE": str(data),
            "OCU_SHARED": str(shared),
            "OCU_CHAT": CHAT,
            "DOCKER_HOST": NO_DOCKER_SOCKET,
            "DOCKER_SOCKET": NO_DOCKER_SOCKET,
            "OCU_DISABLE_BROKER_LOCK": "1" if disabled_lock else "",
            "OCU_SYNCHRONIZE_REPLACE": "1"
            if disabled_lock or environment.get("OCU_SYNCHRONIZE_REPLACE") == "1"
            else "",
            "PYTHONPATH": str(SERVER_DIR) + (os.pathsep + pythonpath if pythonpath else ""),
        }
    )
    return environment


# This is a process program, not extracted from this test file's source.  The
# negative control disables only the production lifecycle lock and uses an
# os.replace barrier to make simultaneous prepared successors observable.
_PROCESS_RECONCILE = r'''
import contextlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.environ["OCU_SERVER_DIR"])
os.environ["BASE_DATA_DIR"] = os.environ["OCU_BASE"]
os.environ["DOCKER_HOST"] = "unix:///tmp/ocu-acceptance-no-docker.sock"
os.environ["DOCKER_SOCKET"] = "unix:///tmp/ocu-acceptance-no-docker.sock"

import docker_manager
import outputs_broker

shared = Path(os.environ["OCU_SHARED"])
pid = str(os.getpid())
ready = shared / ("ready-" + pid)
ready.write_text("1")
deadline = time.monotonic() + 15
while len(list(shared.glob("ready-*"))) < 2:
    if time.monotonic() >= deadline:
        raise RuntimeError("process start barrier timed out")
    time.sleep(0.005)

if os.environ.get("OCU_DISABLE_BROKER_LOCK") == "1":
    docker_manager._combined_lock = lambda _chat_id: contextlib.nullcontext()

if os.environ.get("OCU_SYNCHRONIZE_REPLACE") == "1":
    original_replace = outputs_broker.os.replace

    def synchronized_replace(src, dst, *args, **kwargs):
        marker = shared / ("replace-" + pid)
        marker.write_text("1")
        while len(list(shared.glob("replace-*"))) < 2:
            if time.monotonic() >= deadline:
                raise RuntimeError("replace barrier timed out")
            time.sleep(0.005)
        return original_replace(src, dst, *args, **kwargs)

    outputs_broker.os.replace = synchronized_replace

listing = outputs_broker.OutputsBroker().reconcile(os.environ["OCU_CHAT"])
entry = listing["entries"][0]
print(json.dumps({"file_id": entry["file_id"], "revision": listing["revision"]}))
'''


def _run_concurrent_first_observation(data: Path, *, disabled_lock: bool) -> tuple[list[dict], list[tuple[str, str]]]:
    _put(data, "shared.txt", b"same initial bytes")
    shared = data.parent / ("race-disabled" if disabled_lock else "race-production")
    shared.mkdir()
    environment = _child_environment(data, shared, disabled_lock=disabled_lock)
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", _PROCESS_RECONCILE],
            cwd=str(SERVER_DIR),
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    outputs = [process.communicate(timeout=30) for process in processes]
    assert [process.returncode for process in processes] == [0, 0], outputs
    return [json.loads(stdout.strip().splitlines()[-1]) for stdout, _ in outputs], outputs


def test_first_observation_assigns_uuid_hashes_and_uses_no_docker(world, monkeypatch):
    broker_module, docker_manager, data = world
    calls = {"count": 0}

    def forbidden_docker():
        calls["count"] += 1
        raise AssertionError("outputs broker must not contact Docker")

    monkeypatch.setattr(docker_manager, "get_docker_client", forbidden_docker)
    _put(data, "nested/report.txt", b"first output")

    listing = broker_module.OutputsBroker().reconcile(CHAT)

    assert listing["revision"] == 1
    assert listing["total"] == 1
    assert listing["unchanged"] is False
    entry = listing["entries"] == [] and None or listing["entries"][0]
    _assert_uuid(entry["file_id"])
    assert entry == {
        "file_id": entry["file_id"],
        "path": "nested/report.txt",
        "name": "report.txt",
        "size": len(b"first output"),
        "mtime_ns": entry["mtime_ns"],
        "revision": 1,
        "hash": hashlib.sha256(b"first output").hexdigest(),
    }
    assert calls["count"] == 0
    persisted = _read_index(data)
    assert persisted["counter"] == 1
    assert persisted["active"]["nested/report.txt"]["file_id"] == entry["file_id"]


def test_size_change_with_forged_mtime_keeps_id_and_only_stamps_changed_entry(world):
    broker_module, _docker_manager, data = world
    first = _put(data, "a.html", b"one")
    _put(data, "b.html", b"stay")
    broker = broker_module.OutputsBroker()
    before = broker.reconcile(CHAT)
    before_a = _entry_by_path(before, "a.html")
    before_b = _entry_by_path(before, "b.html")
    old_times = first.stat()

    first.write_bytes(b"larger")
    os.utime(first, ns=(old_times.st_atime_ns, old_times.st_mtime_ns))
    after = broker.reconcile(CHAT)
    after_a = _entry_by_path(after, "a.html")
    after_b = _entry_by_path(after, "b.html")

    assert after["revision"] == before["revision"] + 1
    assert after_a["file_id"] == before_a["file_id"]
    assert after_a["revision"] == after["revision"]
    assert after_a["hash"] == hashlib.sha256(b"larger").hexdigest()
    assert after_b["file_id"] == before_b["file_id"]
    assert after_b["revision"] == before_b["revision"]


def test_rename_retains_id_once_without_tombstoning_live_entry(world):
    broker_module, _docker_manager, data = world
    old = _put(data, "report.html", b"unchanged content")
    broker = broker_module.OutputsBroker()
    before = broker.reconcile(CHAT)
    before_entry = _entry_by_path(before, "report.html")

    old.rename(old.with_name("final.html"))
    after = broker.reconcile(CHAT)
    entry = _entry_by_path(after, "final.html")

    assert after["revision"] == before["revision"] + 1
    assert entry["file_id"] == before_entry["file_id"]
    assert entry["revision"] == after["revision"]
    assert before_entry["file_id"] not in _read_index(data)["tombstones"]


def test_duplicate_fingerprints_match_removed_and_added_paths_one_to_one_deterministically(world):
    broker_module, _docker_manager, data = world
    _put(data, "old-a.txt", b"duplicate")
    _put(data, "old-b.txt", b"duplicate")
    broker = broker_module.OutputsBroker()
    before = broker.reconcile(CHAT)
    old_ids = _ids(before)

    for path in _outputs(data).glob("old-*.txt"):
        path.unlink()
    _put(data, "new-b.txt", b"duplicate")
    _put(data, "new-a.txt", b"duplicate")
    after = broker.reconcile(CHAT)

    assert _ids(after) == {
        "new-a.txt": old_ids["old-a.txt"],
        "new-b.txt": old_ids["old-b.txt"],
    }
    assert len(set(_ids(after).values())) == 2
    assert _read_index(data)["tombstones"] == {}


def test_same_size_different_content_replacement_tombstones_old_identity(world):
    broker_module, _docker_manager, data = world
    old = _put(data, "old.txt", b"abc")
    broker = broker_module.OutputsBroker()
    initial = broker.reconcile(CHAT)
    old_entry = _entry_by_path(initial, "old.txt")

    old.unlink()
    _put(data, "new.txt", b"xyz")
    replaced = broker.reconcile(CHAT)
    new_entry = _entry_by_path(replaced, "new.txt")

    assert replaced["revision"] == initial["revision"] + 2
    assert new_entry["file_id"] != old_entry["file_id"]
    assert _read_index(data)["tombstones"][old_entry["file_id"]]["revision"] == initial["revision"] + 1


def test_observed_deletion_then_path_reuse_never_resurrects_tombstone(world):
    broker_module, _docker_manager, data = world
    path = _put(data, "a.txt", b"same bytes")
    broker = broker_module.OutputsBroker()
    initial = broker.reconcile(CHAT)
    old_entry = _entry_by_path(initial, "a.txt")

    path.unlink()
    missing = broker.reconcile(CHAT)
    assert missing["entries"] == []
    assert missing["revision"] == initial["revision"] + 1

    _put(data, "a.txt", b"same bytes")
    reused = broker.reconcile(CHAT)
    new_entry = _entry_by_path(reused, "a.txt")

    assert new_entry["file_id"] != old_entry["file_id"]
    assert old_entry["file_id"] in _read_index(data)["tombstones"]
    assert reused["revision"] == missing["revision"] + 1


def test_hidden_components_are_excluded_but_visible_nested_paths_are_sorted(world):
    broker_module, _docker_manager, data = world
    _put(data, ".root-hidden.txt", b"hidden")
    _put(data, "visible/.nested-hidden.txt", b"hidden")
    _put(data, ".hidden-dir/inside.txt", b"hidden")
    _put(data, "z.txt", b"z")
    _put(data, "a/visible.txt", b"a")

    listing = broker_module.OutputsBroker().reconcile(CHAT)

    assert [entry["path"] for entry in listing["entries"]] == ["a/visible.txt", "z.txt"]


def test_unchanged_five_thousand_file_scan_reads_no_contents_and_rewrites_no_index(world, monkeypatch):
    broker_module, _docker_manager, data = world
    output_dir = _outputs(data)
    output_dir.mkdir(parents=True)
    for number in range(5_000):
        (output_dir / f"file-{number:04d}.txt").write_bytes(b"x")

    broker = broker_module.OutputsBroker()
    initial = broker.reconcile(CHAT, limit=37)
    index_path = _index(data)
    before_bytes = index_path.read_bytes()
    before_mtime = index_path.stat().st_mtime_ns
    output_identities = {
        (item.stat().st_dev, item.stat().st_ino)
        for item in output_dir.iterdir()
        if item.is_file()
    }
    original_read = broker_module.os.read
    content_reads = {"count": 0}

    def count_output_reads(fd, count):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) in output_identities:
            content_reads["count"] += 1
        return original_read(fd, count)

    monkeypatch.setattr(broker_module.os, "read", count_output_reads)
    for path in output_dir.iterdir():
        os.chmod(path, 0)
    try:
        unchanged = broker.reconcile(CHAT, limit=37)
    finally:
        for path in output_dir.iterdir():
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)

    assert initial["revision"] == 5_000
    assert unchanged["unchanged"] is True
    assert unchanged["revision"] == initial["revision"]
    assert unchanged["total"] == 5_000
    assert len(unchanged["entries"]) == 37
    assert index_path.read_bytes() == before_bytes
    assert index_path.stat().st_mtime_ns == before_mtime
    assert content_reads["count"] == 0


def test_pages_are_sorted_and_reject_stale_malformed_and_out_of_range_cursors(world):
    broker_module, _docker_manager, data = world
    for name in ("z.txt", "a.txt", "m.txt"):
        _put(data, name, name.encode())
    broker = broker_module.OutputsBroker()

    first = broker.reconcile(CHAT, limit=2)
    assert [entry["path"] for entry in first["entries"]] == ["a.txt", "m.txt"]
    assert first["total"] == 3
    assert first["next_cursor"]

    second = broker.reconcile(CHAT, cursor=first["next_cursor"], limit=2)
    assert [entry["path"] for entry in second["entries"]] == ["z.txt"]
    assert second["next_cursor"] is None

    _put(data, "new.txt", b"new")
    with pytest.raises(broker_module.StaleCursorError):
        broker.reconcile(CHAT, cursor=first["next_cursor"], limit=2)
    assert broker.current_revision(CHAT) == first["revision"] + 1

    huge = "9" * 5000
    for cursor in ("", "not-a-cursor", "1:-1", f"{broker.current_revision(CHAT)}:99", f"1:{huge}", f"{huge}:0"):
        with pytest.raises(broker_module.CursorError):
            broker.reconcile(CHAT, cursor=cursor, limit=2)

def test_default_page_is_one_hundred_entries_with_a_revision_bound_cursor(world):
    broker_module, _docker_manager, data = world
    for number in range(101):
        _put(data, f"page-{number:03d}.txt", b"x")

    listing = broker_module.OutputsBroker().reconcile(CHAT)

    assert listing["revision"] == 101
    assert listing["total"] == 101
    assert len(listing["entries"]) == 100
    assert listing["next_cursor"] == "101:100"


def test_page_and_constructor_limits_are_strict_and_positive(world):
    broker_module, _docker_manager, data = world
    with pytest.raises(ValueError):
        broker_module.OutputsBroker(max_active_files=0)
    with pytest.raises(ValueError):
        broker_module.OutputsBroker(max_page_limit=1_001)
    broker = broker_module.OutputsBroker(max_page_limit=2)
    _put(data, "a.txt", b"a")
    with pytest.raises(broker_module.LimitExceededError):
        broker.reconcile(CHAT, limit=3)


def test_active_file_and_file_size_limits_preserve_prior_index_bytes(world):
    broker_module, _docker_manager, data = world
    active_limited = broker_module.OutputsBroker(max_active_files=1)
    _put(data, "a.txt", b"a")
    active_limited.reconcile(CHAT)
    before_active = _index(data).read_bytes()
    _put(data, "b.txt", b"b")
    with pytest.raises(broker_module.LimitExceededError):
        active_limited.reconcile(CHAT)
    assert _index(data).read_bytes() == before_active

    sized_limited = broker_module.OutputsBroker(max_file_size=3)
    sized_limited.reconcile(OTHER_CHAT)
    before_size = _index(data, OTHER_CHAT).read_bytes()
    _put(data, "large.txt", b"four", OTHER_CHAT)
    with pytest.raises(broker_module.LimitExceededError):
        sized_limited.reconcile(OTHER_CHAT)
    assert _index(data, OTHER_CHAT).read_bytes() == before_size


def test_wide_tree_beyond_default_scan_descriptor_budget_preserves_predecessor(world):
    broker_module, _docker_manager, data = world
    _put(data, "keep.txt", b"keep original identity")
    broker = broker_module.OutputsBroker()
    initial = broker.reconcile(CHAT)
    initial_entry = _entry_by_path(initial, "keep.txt")
    before_bytes = _index(data).read_bytes()
    before_index = _read_index(data)

    excess_directories = [_outputs(data) / f"wide-{number:03d}" for number in range(500)]
    for directory in excess_directories:
        directory.mkdir()

    with pytest.raises(broker_module.LimitExceededError) as failure:
        broker.reconcile(CHAT)

    message = str(failure.value).lower()
    assert "scan" in message
    assert "descriptor" in message
    assert _index(data).read_bytes() == before_bytes
    preserved = _read_index(data)
    assert preserved["counter"] == before_index["counter"]
    assert preserved["active"]["keep.txt"] == before_index["active"]["keep.txt"]
    assert broker.current_revision(CHAT) == initial["revision"]

    for directory in excess_directories:
        directory.rmdir()

    restored = broker.reconcile(CHAT)
    restored_entry = _entry_by_path(restored, "keep.txt")
    assert restored["unchanged"] is True
    assert restored["total"] == 1
    assert restored["revision"] == initial["revision"]
    assert restored_entry["file_id"] == initial_entry["file_id"]
    assert restored_entry["revision"] == initial_entry["revision"]
    assert _read_index(data)["counter"] == before_index["counter"]
    assert _index(data).read_bytes() == before_bytes


@pytest.mark.parametrize(
    "name,ceiling",
    [
        ("max_scan_entries", 50_000),
        ("max_scan_directories", 10_000),
        ("max_scan_directory_fds", 256),
    ],
)
@pytest.mark.parametrize("invalid", [0, -1, True, False, 1.5, "1", None, "above-ceiling"])
def test_scan_constructor_rejects_invalid_or_widened_bounds(world, name, ceiling, invalid):
    broker_module, _docker_manager, _data = world
    value = ceiling + 1 if invalid == "above-ceiling" else invalid
    with pytest.raises(ValueError, match=name):
        broker_module.OutputsBroker(**{name: value})


@pytest.mark.parametrize("kind", ["file", "directory", "hidden-file", "hidden-directory", "symlink", "fifo"])
def test_scan_entry_budget_counts_filtered_entries_and_excludes_hidden_subtrees(world, kind):
    broker_module, _docker_manager, data = world
    _put(data, "keep.txt", b"keep")
    broker = broker_module.OutputsBroker(max_scan_entries=2)
    initial = broker.reconcile(CHAT)
    extra = _outputs(data) / "extra"
    if kind == "file":
        extra.write_bytes(b"extra")
    elif kind == "directory":
        extra.mkdir()
    elif kind == "hidden-file":
        extra = _outputs(data) / ".hidden"
        extra.write_bytes(b"hidden")
    elif kind == "hidden-directory":
        extra = _outputs(data) / ".hidden"
        extra.mkdir()
        for number in range(3):
            (extra / f"unvisited-{number}").write_bytes(b"hidden")
    elif kind == "symlink":
        extra.symlink_to(_outputs(data) / "keep.txt")
    else:
        os.mkfifo(extra)

    exact = broker.reconcile(CHAT)
    assert [entry["path"] for entry in exact["entries"]] == (
        ["extra", "keep.txt"] if kind == "file" else ["keep.txt"]
    )
    before = _index(data).read_bytes()
    excess = _outputs(data) / ".excess"
    excess.write_bytes(b"invisible but counted")
    with pytest.raises(broker_module.LimitExceededError, match="scan.*entr"):
        broker.reconcile(CHAT)
    assert _index(data).read_bytes() == before
    excess.unlink()
    restored = broker.reconcile(CHAT)
    assert restored["unchanged"] is True
    assert restored["revision"] == exact["revision"]
    assert _entry_by_path(restored, "keep.txt")["file_id"] == _entry_by_path(initial, "keep.txt")["file_id"]
    assert _index(data).read_bytes() == before


def test_excess_scan_entry_is_not_inspected_or_opened(world, monkeypatch):
    broker_module, _docker_manager, data = world
    _put(data, "keep.txt", b"keep")
    broker = broker_module.OutputsBroker(max_scan_entries=1)
    broker.reconcile(CHAT)
    before = _index(data).read_bytes()
    original_scandir = os.scandir
    original_open = os.open
    output_root = _outputs(data)
    root_identity = (output_root.stat().st_dev, output_root.stat().st_ino)
    opened_children = []

    class ExcessEntry:
        name = "excess"

        def stat(self, **_kwargs):
            raise AssertionError("the excess entry must not be inspected")

    class OrderedListing:
        def __init__(self, fd):
            self.listing = original_scandir(fd)

        def __enter__(self):
            self.listing.__enter__()
            return self

        def __iter__(self):
            yield from self.listing
            yield ExcessEntry()

        def __exit__(self, *args):
            return self.listing.__exit__(*args)

    def ordered_scandir(fd):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == root_identity:
            return OrderedListing(fd)
        return original_scandir(fd)

    def observe_open(path, flags, *args, **kwargs):
        if path == "excess":
            opened_children.append(path)
            raise AssertionError("the excess entry must not be opened")
        return original_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as patches:
        patches.setattr(broker_module.os, "scandir", ordered_scandir)
        patches.setattr(broker_module.os, "open", observe_open)
        with pytest.raises(broker_module.LimitExceededError, match="scan.*entr"):
            broker.reconcile(CHAT)
    assert opened_children == []
    assert _index(data).read_bytes() == before
    assert broker.reconcile(CHAT)["unchanged"] is True


def test_scan_directory_budget_includes_root_and_refuses_before_child_open(world, monkeypatch):
    broker_module, _docker_manager, data = world
    output_root = _outputs(data)
    output_root.mkdir(parents=True)
    broker = broker_module.OutputsBroker(max_scan_directories=1)
    initial = broker.reconcile(CHAT)
    assert initial["entries"] == []
    before = _index(data).read_bytes()
    child = output_root / "child"
    child.mkdir()
    original_open = os.open

    def forbid_child_open(path, flags, *args, **kwargs):
        if path == "child" and kwargs.get("dir_fd") is not None:
            raise AssertionError("directory budget must be checked before opening the child")
        return original_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as patches:
        patches.setattr(broker_module.os, "open", forbid_child_open)
        with pytest.raises(broker_module.LimitExceededError, match="scan.*director"):
            broker.reconcile(CHAT)
    assert _index(data).read_bytes() == before
    exact = broker_module.OutputsBroker(max_scan_directories=2).reconcile(CHAT)
    assert exact["unchanged"] is True
    child.rmdir()
    assert broker.reconcile(CHAT)["unchanged"] is True
    assert _index(data).read_bytes() == before


@pytest.mark.parametrize(
    "shape,paths,exact_fds",
    [
        ("wide", ["a", "b", "c"], 5),
        ("deep", ["a/b/c"], 3),
        ("mixed", ["a/leaf", "b/leaf"], 4),
    ],
)
def test_scan_descriptor_budget_allows_exact_shape_and_releases_owned_fds(
    world, monkeypatch, shape, paths, exact_fds
):
    broker_module, _docker_manager, data = world
    output_root = _outputs(data)
    output_root.mkdir(parents=True)
    for path in paths:
        (output_root / path).mkdir(parents=True)
    original_open = os.open
    acquired = []

    def track_open(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if flags & os.O_DIRECTORY:
            acquired.append(fd)
        return fd

    root_fd = original_open(output_root, broker_module._DIRECTORY_FLAGS)
    try:
        with monkeypatch.context() as patches:
            patches.setattr(broker_module.os, "open", track_open)
            broker_module.OutputsBroker(max_scan_directory_fds=exact_fds)._scan_tree(root_fd, {})
        os.fstat(root_fd)
        for fd in acquired:
            with pytest.raises(OSError) as failure:
                os.fstat(fd)
            assert failure.value.errno == errno.EBADF
        acquired.clear()
        with monkeypatch.context() as patches:
            patches.setattr(broker_module.os, "open", track_open)
            with pytest.raises(broker_module.LimitExceededError, match="scan.*descriptor"):
                broker_module.OutputsBroker(max_scan_directory_fds=exact_fds - 1)._scan_tree(root_fd, {})
        os.fstat(root_fd)
        for fd in acquired:
            with pytest.raises(OSError) as failure:
                os.fstat(fd)
            assert failure.value.errno == errno.EBADF
    finally:
        os.close(root_fd)


def test_scan_descriptor_budget_checks_iterator_duplicate_before_acquisition(world, monkeypatch):
    broker_module, _docker_manager, data = world
    _put(data, "keep.txt", b"keep")
    broker = broker_module.OutputsBroker()
    initial = broker.reconcile(CHAT)
    before = _index(data).read_bytes()

    def forbid_iterator(_fd):
        raise AssertionError("the duplicate iterator descriptor exceeds the budget")

    with monkeypatch.context() as patches:
        patches.setattr(broker_module.os, "scandir", forbid_iterator)
        with pytest.raises(broker_module.LimitExceededError, match="scan.*descriptor"):
            broker_module.OutputsBroker(max_scan_directory_fds=1).reconcile(CHAT)
    assert _index(data).read_bytes() == before
    restored = broker_module.OutputsBroker(max_scan_directory_fds=2).reconcile(CHAT)
    assert restored["unchanged"] is True
    assert restored["entries"] == initial["entries"]


def test_scan_descriptor_budget_refuses_frontier_acquisition_before_open(world, monkeypatch):
    broker_module, _docker_manager, data = world
    output_root = _outputs(data)
    output_root.mkdir(parents=True)
    for name in ("a", "b", "c"):
        (output_root / name).mkdir()
    original_open = os.open
    original_close = os.close
    owned = set()

    def track_open(path, flags, *args, **kwargs):
        if kwargs.get("dir_fd") is not None and flags & os.O_DIRECTORY:
            assert len(owned) < 2, "the third child must be refused before acquisition"
            fd = original_open(path, flags, *args, **kwargs)
            owned.add(fd)
            return fd
        return original_open(path, flags, *args, **kwargs)

    def track_close(fd):
        original_close(fd)
        owned.discard(fd)

    root_fd = original_open(output_root, broker_module._DIRECTORY_FLAGS)
    try:
        with monkeypatch.context() as patches:
            patches.setattr(broker_module.os, "open", track_open)
            patches.setattr(broker_module.os, "close", track_close)
            with pytest.raises(broker_module.LimitExceededError, match="scan.*descriptor"):
                broker_module.OutputsBroker(max_scan_directory_fds=3)._scan_tree(root_fd, {})
        assert owned == set()
        os.fstat(root_fd)
    finally:
        original_close(root_fd)


@pytest.mark.parametrize("error_number", [errno.EMFILE, errno.ENFILE])
@pytest.mark.parametrize("stage", ["root", "child", "iterator-create", "iterator-advance"])
def test_scan_os_descriptor_exhaustion_preserves_authority_and_recovers(
    world, monkeypatch, stage, error_number
):
    broker_module, _docker_manager, data = world
    _put(data, "keep.txt", b"keep")
    broker = broker_module.OutputsBroker()
    initial = broker.reconcile(CHAT)
    before = _index(data).read_bytes()
    output_root = _outputs(data)
    (output_root / "child").mkdir()
    original_open = os.open
    original_scandir = os.scandir
    acquired = []

    def failing_open(path, flags, *args, **kwargs):
        if (stage == "root" and path == output_root) or (
            stage == "child" and path == "child" and kwargs.get("dir_fd") is not None
        ):
            raise OSError(error_number, "injected directory descriptor exhaustion")
        fd = original_open(path, flags, *args, **kwargs)
        if flags & os.O_DIRECTORY:
            acquired.append(fd)
        return fd

    class FailingListing:
        def __init__(self, fd):
            self.listing = original_scandir(fd)

        def __enter__(self):
            self.listing.__enter__()
            return self

        def __iter__(self):
            raise OSError(error_number, "injected enumeration descriptor exhaustion")
            yield

        def __exit__(self, *args):
            return self.listing.__exit__(*args)

    def failing_scandir(fd):
        if stage == "iterator-create":
            raise OSError(error_number, "injected iterator descriptor exhaustion")
        if stage == "iterator-advance":
            return FailingListing(fd)
        return original_scandir(fd)

    with monkeypatch.context() as patches:
        patches.setattr(broker_module.os, "open", failing_open)
        patches.setattr(broker_module.os, "scandir", failing_scandir)
        with pytest.raises(broker_module.LimitExceededError, match="scan.*descriptor"):
            broker.reconcile(CHAT)
        for fd in acquired:
            with pytest.raises(OSError) as failure:
                os.fstat(fd)
            assert failure.value.errno == errno.EBADF
    assert _index(data).read_bytes() == before
    assert broker.current_revision(CHAT) == initial["revision"]
    restored = broker.reconcile(CHAT)
    assert restored["unchanged"] is True
    assert restored["entries"] == initial["entries"]
    assert _index(data).read_bytes() == before


@pytest.mark.parametrize("error_number", [errno.EMFILE, errno.ENFILE])
@pytest.mark.parametrize("stage", ["root", "child"])
def test_registration_directory_errors_remain_unstable_outside_scan(
    world, monkeypatch, stage, error_number
):
    broker_module, _docker_manager, data = world
    _put(data, "child/keep.txt", b"keep")
    broker = broker_module.OutputsBroker(max_scan_entries=1, max_scan_directories=1, max_scan_directory_fds=1)
    initial = broker.register_host_write(CHAT, "child/keep.txt")
    before = _index(data).read_bytes()
    output_root = _outputs(data)
    original_open = os.open

    def failing_open(path, flags, *args, **kwargs):
        if (stage == "root" and path == output_root) or (
            stage == "child" and path == "child" and kwargs.get("dir_fd") is not None
        ):
            raise OSError(error_number, "injected non-scan directory exhaustion")
        return original_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as patches:
        patches.setattr(broker_module.os, "open", failing_open)
        with pytest.raises(broker_module.UnstableReadError):
            broker.register_host_write(CHAT, "child/keep.txt")
    assert _index(data).read_bytes() == before
    recovered = broker.register_host_write(CHAT, "child/keep.txt")
    assert recovered["file_id"] == initial["file_id"]


def test_absent_outputs_root_does_not_consume_scan_directory_or_iterator_budget(world):
    broker_module, _docker_manager, _data = world
    listing = broker_module.OutputsBroker(
        max_scan_entries=1, max_scan_directories=1, max_scan_directory_fds=1
    ).reconcile(CHAT)
    assert listing["entries"] == []
    assert listing["revision"] == 0


@pytest.mark.parametrize("failure_kind", ["os-error", "base-exception"])
def test_child_iterator_failure_releases_current_frontier_and_duplicate_but_not_root(
    world, monkeypatch, failure_kind
):
    broker_module, _docker_manager, data = world
    output_root = _outputs(data)
    output_root.mkdir(parents=True)
    for name in ("a", "b", "c"):
        (output_root / name).mkdir()
    original_open = os.open
    original_scandir = os.scandir
    acquired = []
    iterator_fds = []
    root_fd = original_open(output_root, broker_module._DIRECTORY_FLAGS)

    class ScanInterrupted(BaseException):
        pass

    class FailingChildListing:
        def __init__(self, fd):
            identity = os.fstat(fd)
            before = set()
            for candidate in range(max([root_fd, *acquired]) + 2):
                try:
                    os.fstat(candidate)
                    before.add(candidate)
                except OSError:
                    pass
            self.listing = original_scandir(fd)
            for candidate in range(max([root_fd, *acquired]) + 2):
                if candidate in before:
                    continue
                try:
                    info = os.fstat(candidate)
                except OSError:
                    continue
                if (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
                    iterator_fds.append(candidate)

        def __enter__(self):
            self.listing.__enter__()
            return self

        def __iter__(self):
            if failure_kind == "os-error":
                raise OSError(errno.EMFILE, "child enumeration descriptor exhaustion")
            raise ScanInterrupted()
            yield

        def __exit__(self, *args):
            return self.listing.__exit__(*args)

    def track_open(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        acquired.append(fd)
        return fd

    def failing_scandir(fd):
        return original_scandir(fd) if fd == root_fd else FailingChildListing(fd)

    try:
        with monkeypatch.context() as patches:
            patches.setattr(broker_module.os, "open", track_open)
            patches.setattr(broker_module.os, "scandir", failing_scandir)
            expected = broker_module.LimitExceededError if failure_kind == "os-error" else ScanInterrupted
            with pytest.raises(expected):
                broker_module.OutputsBroker()._scan_tree(root_fd, {})
        assert len(iterator_fds) == 1
        for fd in [*acquired, *iterator_fds]:
            with pytest.raises(OSError) as failure:
                os.fstat(fd)
            assert failure.value.errno == errno.EBADF
        os.fstat(root_fd)
        observations = {}
        broker_module.OutputsBroker()._scan_tree(root_fd, observations)
        assert observations == {}
    finally:
        os.close(root_fd)


_SCAN_RLIMIT_PROBE = r'''
import errno
import fcntl
import json
import os
import resource
import sys
from pathlib import Path

sys.path.insert(0, os.environ["OCU_SERVER_DIR"])
os.environ["BASE_DATA_DIR"] = os.environ["OCU_BASE"]
os.environ["DOCKER_HOST"] = "unix:///tmp/ocu-acceptance-no-docker.sock"
os.environ["DOCKER_SOCKET"] = "unix:///tmp/ocu-acceptance-no-docker.sock"
import outputs_broker

chat = os.environ["OCU_CHAT"]
root = Path(os.environ["OCU_BASE"]) / chat / "outputs"
root.mkdir(parents=True)
(root / "keep.txt").write_bytes(b"keep")
broker = outputs_broker.OutputsBroker()
initial = broker.reconcile(chat)
index = root.parent / ".ocu" / "index.json"
before_bytes = index.read_bytes()
directories = [root / ("wide-%03d" % number) for number in range(100)]
for directory in directories:
    directory.mkdir()
_soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (64, hard))
mode = os.environ["OCU_SCAN_PROBE_MODE"]
if mode == "policy":
    broker = outputs_broker.OutputsBroker(max_scan_directory_fds=8)
identities = {(path.stat().st_dev, path.stat().st_ino) for path in [root, *directories]}
peak = 0

def live_fds():
    live = set()
    for fd in range(64):
        try:
            fcntl.fcntl(fd, fcntl.F_GETFD)
            live.add(fd)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise
    return live

def sample_scan_fds():
    global peak
    count = 0
    for fd in live_fds():
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) in identities:
            count += 1
    peak = max(peak, count)

original_open = os.open
original_scandir = os.scandir

def sampled_open(*args, **kwargs):
    fd = original_open(*args, **kwargs)
    sample_scan_fds()
    return fd

def sampled_scandir(*args, **kwargs):
    listing = original_scandir(*args, **kwargs)
    sample_scan_fds()
    return listing

before_fds = live_fds()
assert len(before_fds) < 64
os.open = sampled_open
os.scandir = sampled_scandir
try:
    broker.reconcile(chat)
except outputs_broker.LimitExceededError as exc:
    cause = exc.__cause__
    if mode == "os":
        assert isinstance(cause, OSError)
        assert cause.errno == errno.EMFILE
        assert 8 < peak <= 64
    else:
        assert cause is None
        assert peak == 8
    assert "scan" in str(exc).lower() and "descriptor" in str(exc).lower()
else:
    raise AssertionError("native descriptor exhaustion must refuse reconciliation")
finally:
    os.open = original_open
    os.scandir = original_scandir
assert live_fds() == before_fds
assert index.read_bytes() == before_bytes
assert broker.current_revision(chat) == initial["revision"]
for directory in directories:
    directory.rmdir()
restored = broker.reconcile(chat)
assert restored["unchanged"] is True
assert restored["entries"] == initial["entries"]
assert index.read_bytes() == before_bytes
assert live_fds() == before_fds
print(json.dumps({"error": "LimitExceededError", "errno": errno.EMFILE if mode == "os" else None, "preserved": True, "recovered": True}))
'''


@pytest.mark.parametrize("mode", ["policy", "os"])
def test_native_low_nofile_scan_exhaustion_releases_resources_and_preserves_identity(world, mode):
    _broker_module, _docker_manager, data = world
    environment = os.environ.copy()
    environment.update(
        {
            "OCU_SERVER_DIR": str(SERVER_DIR),
            "OCU_BASE": str(data),
            "OCU_CHAT": CHAT,
            "OCU_SCAN_PROBE_MODE": mode,
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", _SCAN_RLIMIT_PROBE],
        cwd=str(SERVER_DIR),
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        "error": "LimitExceededError",
        "errno": errno.EMFILE if mode == "os" else None,
        "preserved": True,
        "recovered": True,
    }


def test_successor_index_size_limit_preserves_valid_predecessor(world):
    broker_module, _docker_manager, data = world
    broker_module.OutputsBroker().reconcile(CHAT)
    before = _index(data).read_bytes()
    constrained = broker_module.OutputsBroker(max_index_size=len(before) + 1)
    _put(data, "entry.txt", b"entry")

    with pytest.raises(broker_module.LimitExceededError):
        constrained.reconcile(CHAT)

    assert _index(data).read_bytes() == before


def test_corrupt_or_wrong_schema_index_fails_closed_without_reset(world):
    broker_module, _docker_manager, data = world
    broker = broker_module.OutputsBroker()
    _put(data, "a.txt", b"a")
    broker.reconcile(CHAT)
    index_path = _index(data)

    for bad in (b"{broken-json",):
        index_path.write_bytes(bad)
        with pytest.raises(broker_module.CorruptIndexError):
            broker.reconcile(CHAT)
        assert index_path.read_bytes() == bad
    wrong_version = {
        "schema_version": 999,
        "counter": 0,
        "active": {},
        "fingerprints": {},
        "tombstones": {},
    }
    encoded_wrong_version = json.dumps(wrong_version)
    index_path.write_text(encoded_wrong_version, encoding="utf-8")
    with pytest.raises(broker_module.CorruptIndexError, match="schema version"):
        broker.current_revision(CHAT)
    assert index_path.read_text(encoding="utf-8") == encoded_wrong_version
    malformed = {
        "schema_version": 1,
        "counter": 1,
        "active": {},
        "fingerprints": {},
        "tombstones": {"00000000-0000-4000-8000-000000000000": []},
    }
    index_path.write_text(json.dumps(malformed), encoding="utf-8")
    with pytest.raises(broker_module.CorruptIndexError):
        broker.current_revision(CHAT)
    assert index_path.read_text(encoding="utf-8") == json.dumps(malformed)
    index_path.unlink()
    broker.reconcile(CHAT)
    valid = json.loads(_index(data).read_text(encoding="utf-8"))
    file_id = next(iter(valid["active"].values()))["file_id"]
    digest = next(iter(valid["active"].values()))["hash"]
    for bad_path, encoder in (
        ("nul\x00name.txt", lambda payload: json.dumps(payload, ensure_ascii=False)),
        ("bad\udcff.txt", lambda payload: json.dumps(payload, ensure_ascii=True)),
    ):
        poisoned = {
            "schema_version": 1,
            "counter": 1,
            "active": {
                bad_path: {
                    "file_id": file_id,
                    "path": bad_path,
                    "name": bad_path.rsplit("/", 1)[-1],
                    "size": 1,
                    "mtime_ns": 1,
                    "revision": 1,
                    "hash": digest,
                }
            },
            "fingerprints": {f"1:{digest}": [file_id]},
            "tombstones": {},
        }
        encoded = encoder(poisoned)
        index_path.write_text(encoded, encoding="utf-8")
        with pytest.raises(broker_module.CorruptIndexError, match="path is invalid"):
            broker.current_revision(CHAT)
        assert index_path.read_text(encoding="utf-8") == encoded



def test_precommit_replace_failure_keeps_predecessor_and_cleans_temporary_files(world, monkeypatch):
    broker_module, _docker_manager, data = world
    broker = broker_module.OutputsBroker()
    _put(data, "a.txt", b"a")
    broker.reconcile(CHAT)
    before = _index(data).read_bytes()
    _put(data, "b.txt", b"b")

    def rejected_replace(*_args, **_kwargs):
        raise OSError("controlled atomic replacement failure")

    monkeypatch.setattr(broker_module.os, "replace", rejected_replace)
    with pytest.raises(OSError, match="controlled atomic replacement failure"):
        broker.reconcile(CHAT)

    assert _index(data).read_bytes() == before
    assert not list(_index(data).parent.glob("*.tmp"))


def test_postcommit_durability_failure_reports_complete_successor(world, monkeypatch):
    broker_module, _docker_manager, data = world
    broker = broker_module.OutputsBroker()
    _put(data, "a.txt", b"a")
    broker.reconcile(CHAT)
    before = _index(data).read_bytes()
    _put(data, "b.txt", b"b")
    original_fsync = broker_module.os.fsync

    def fail_directory_sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("controlled directory fsync failure")
        return original_fsync(fd)

    with monkeypatch.context() as patches:
        patches.setattr(broker_module.os, "fsync", fail_directory_sync)
        with pytest.raises(broker_module.CommitDurabilityError, match="committed"):
            broker.reconcile(CHAT)

    assert _index(data).read_bytes() != before
    complete = broker.reconcile(CHAT)
    assert complete["total"] == 2
    assert complete["revision"] == 2


def test_symlinked_entries_are_excluded_and_unsafe_roots_controls_and_indexes_fail(world):
    broker_module, _docker_manager, data = world
    outside = data.parent / "outside.txt"
    outside.write_bytes(b"outside")
    outside_dir = data.parent / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_bytes(b"secret")
    _put(data, "ordinary.txt", b"ordinary")
    os.symlink(outside, _outputs(data) / "file-link.txt")
    os.symlink(outside_dir, _outputs(data) / "directory-link")

    safe_listing = broker_module.OutputsBroker().reconcile(CHAT)
    assert [entry["path"] for entry in safe_listing["entries"]] == ["ordinary.txt"]

    root_chat = OTHER_CHAT
    (data / root_chat).mkdir(parents=True)
    os.symlink(outside_dir, _outputs(data, root_chat))
    with pytest.raises(broker_module.UnsafePathError):
        broker_module.OutputsBroker().reconcile(root_chat)

    control_chat = "c3d4e5f6-a7b8-9012-cdef-345678901234"
    (data / control_chat).mkdir(parents=True)
    os.symlink(outside_dir, data / control_chat / ".ocu")
    with pytest.raises(broker_module.UnsafePathError):
        broker_module.OutputsBroker().reconcile(control_chat)

    index_chat = "d4e5f6a7-b8c9-0123-defa-456789012345"
    control = data / index_chat / ".ocu"
    control.mkdir(parents=True)
    os.symlink(outside, control / "index.json")
    with pytest.raises(broker_module.UnsafePathError):
        broker_module.OutputsBroker().current_revision(index_chat)


def test_unstable_hash_read_rejects_successor_without_partial_index(world, monkeypatch):
    broker_module, _docker_manager, data = world
    broker = broker_module.OutputsBroker()
    broker.reconcile(CHAT)
    before = _index(data).read_bytes()
    target = _put(data, "changing.txt", b"abcdefgh")
    target_stat = target.stat()
    original_read = broker_module.os.read
    changed = {"done": False}

    def mutate_after_first_chunk(fd, count):
        chunk = original_read(fd, count)
        reading = os.fstat(fd)
        if (
            chunk
            and not changed["done"]
            and reading.st_dev == target_stat.st_dev
            and reading.st_ino == target_stat.st_ino
        ):
            changed["done"] = True
            target.write_bytes(b"ABCDEFGH")
        return chunk

    monkeypatch.setattr(broker_module.os, "read", mutate_after_first_chunk)
    with pytest.raises(broker_module.UnstableReadError):
        broker.reconcile(CHAT)

    assert changed["done"] is True
    assert _index(data).read_bytes() == before


def test_current_revision_is_read_only_validated_and_never_contacts_docker(world, monkeypatch):
    broker_module, docker_manager, data = world
    broker = broker_module.OutputsBroker()

    def forbidden_docker():
        raise AssertionError("read-only revision access must not contact Docker")

    monkeypatch.setattr(docker_manager, "get_docker_client", forbidden_docker)
    assert broker.current_revision(CHAT) == 0
    assert not _index(data).exists()

    _put(data, "a.txt", b"a")
    listing = broker.reconcile(CHAT)
    assert broker.current_revision(CHAT) == listing["revision"]
    _index(data).write_text("not json", encoding="utf-8")
    with pytest.raises(broker_module.CorruptIndexError):
        broker.current_revision(CHAT)


def test_process_restart_preserves_ids_and_next_event_counter(world):
    broker_module, _docker_manager, data = world
    _put(data, "a.txt", b"a")
    first = broker_module.OutputsBroker().reconcile(CHAT)
    first_entry = _entry_by_path(first, "a.txt")
    script = r'''
import json
import os
import sys
sys.path.insert(0, os.environ["OCU_SERVER_DIR"])
os.environ["BASE_DATA_DIR"] = os.environ["OCU_BASE"]
os.environ["DOCKER_HOST"] = "unix:///tmp/ocu-acceptance-no-docker.sock"
os.environ["DOCKER_SOCKET"] = "unix:///tmp/ocu-acceptance-no-docker.sock"
import outputs_broker
listing = outputs_broker.OutputsBroker().reconcile(os.environ["OCU_CHAT"])
print(json.dumps({"revision": listing["revision"], "file_id": listing["entries"][0]["file_id"]}))
'''
    environment = os.environ.copy()
    environment.update(
        {
            "OCU_SERVER_DIR": str(SERVER_DIR),
            "OCU_BASE": str(data),
            "OCU_CHAT": CHAT,
            "DOCKER_HOST": NO_DOCKER_SOCKET,
            "DOCKER_SOCKET": NO_DOCKER_SOCKET,
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(SERVER_DIR),
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    restarted = json.loads(completed.stdout.strip().splitlines()[-1])
    assert restarted == {"revision": first["revision"], "file_id": first_entry["file_id"]}

    _put(data, "b.txt", b"b")
    after = broker_module.OutputsBroker().reconcile(CHAT)
    assert after["revision"] == first["revision"] + 1


def test_production_lifecycle_flock_serializes_concurrent_first_observation(world):
    broker_module, _docker_manager, data = world
    children, _outputs = _run_concurrent_first_observation(data, disabled_lock=False)

    assert len({child["file_id"] for child in children}) == 1
    assert {child["revision"] for child in children} == {1}
    final = broker_module.OutputsBroker().reconcile(CHAT)
    assert final["revision"] == 1
    assert final["entries"][0]["file_id"] == children[0]["file_id"]


def test_disabled_lifecycle_flock_negative_control_allows_conflicting_first_ids(world):
    broker_module, _docker_manager, data = world
    children, _outputs = _run_concurrent_first_observation(data, disabled_lock=True)

    assert len({child["file_id"] for child in children}) == 2
    final = broker_module.OutputsBroker().reconcile(CHAT)
    assert final["revision"] == 1
    assert final["entries"][0]["file_id"] in {child["file_id"] for child in children}


_FIFO_WATCHDOG = r'''
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ["OCU_SERVER_DIR"])
os.environ["BASE_DATA_DIR"] = os.environ["OCU_BASE"]
os.environ["DOCKER_HOST"] = "unix:///tmp/ocu-acceptance-no-docker.sock"
os.environ["DOCKER_SOCKET"] = "unix:///tmp/ocu-acceptance-no-docker.sock"

import outputs_broker

chat = os.environ["OCU_CHAT"]
target = Path(os.environ["OCU_BASE"]) / chat / "outputs" / "watched.txt"
target.parent.mkdir(parents=True)
target.write_bytes(b"regular-before-fifo")
original_scan = outputs_broker.OutputsBroker._scan_metadata

def scan_then_replace(self, chat_id, *args, **kwargs):
    observations = original_scan(self, chat_id, *args, **kwargs)
    target.unlink()
    os.mkfifo(target)
    return observations

outputs_broker.OutputsBroker._scan_metadata = scan_then_replace
try:
    outputs_broker.OutputsBroker().reconcile(chat)
except outputs_broker.UnstableReadError as exc:
    print("unstable:" + type(exc).__name__)
    raise SystemExit(0)
print("unexpected-success")
raise SystemExit(2)
'''


def test_fifo_replacement_after_scan_fails_promptly_and_releases_lock(world):
    broker_module, docker_manager, data = world
    environment = os.environ.copy()
    environment.update(
        {
            "OCU_SERVER_DIR": str(SERVER_DIR),
            "OCU_BASE": str(data),
            "OCU_CHAT": CHAT,
            "DOCKER_HOST": NO_DOCKER_SOCKET,
            "DOCKER_SOCKET": NO_DOCKER_SOCKET,
        }
    )
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, "-c", _FIFO_WATCHDOG],
        cwd=str(SERVER_DIR),
        env=environment,
        text=True,
        capture_output=True,
        timeout=3,
        check=False,
        start_new_session=True,
    )
    elapsed = time.monotonic() - started
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert "unstable:UnstableReadError" in completed.stdout
    assert elapsed < 2.5
    with docker_manager._combined_lock(CHAT):
        assert docker_manager._FLOCK_DEPTH.get(CHAT, 0) >= 1
    listing = broker_module.OutputsBroker().reconcile(CHAT)
    assert listing["entries"] == []


def test_backslash_name_is_rejected_and_removing_it_restores_reconciliation(world):
    broker_module, _docker_manager, data = world
    broker = broker_module.OutputsBroker()
    _put(data, "ok.txt", b"ok")
    broker.reconcile(CHAT)
    before = _index(data).read_bytes()
    bad = _outputs(data) / "report\\final.txt"
    bad.write_bytes(b"bad-name")

    with pytest.raises(broker_module.OutputsBrokerError) as raised:
        broker.reconcile(CHAT)
    assert type(raised.value).__name__ == "UnsupportedNameError"
    assert "unsupported output name" in str(raised.value)
    assert "final.txt" in str(raised.value)
    assert not isinstance(raised.value, broker_module.CorruptIndexError)
    assert _index(data).read_bytes() == before

    bad.unlink()
    restored = broker.reconcile(CHAT)
    assert restored["unchanged"] is True
    assert [entry["path"] for entry in restored["entries"]] == ["ok.txt"]


def test_surrogate_names_are_rejected_at_the_scan_name_policy(world, monkeypatch):
    broker_module, _docker_manager, data = world
    broker = broker_module.OutputsBroker()
    _put(data, "ok.txt", b"ok")
    broker.reconcile(CHAT)
    before = _index(data).read_bytes()

    class SurrogateDirEntry:
        name = "bad\udcff.txt"

        def stat(self, follow_symlinks=False):
            return os.stat(_outputs(data) / "ok.txt", follow_symlinks=follow_symlinks)

    class SurrogateListing:
        def __iter__(self):
            yield SurrogateDirEntry()

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(broker_module.os, "scandir", lambda _fd: SurrogateListing())
    monkeypatch.setattr(broker_module.os, "listdir", lambda _fd: ["bad\udcff.txt"])
    with pytest.raises(broker_module.OutputsBrokerError) as raised:
        broker.reconcile(CHAT)
    assert type(raised.value).__name__ == "UnsupportedNameError"
    assert "unsupported output name" in str(raised.value)
    assert "bad" in str(raised.value)
    assert not isinstance(raised.value, broker_module.CorruptIndexError)
    monkeypatch.undo()

    restored = broker.reconcile(CHAT)
    assert restored["unchanged"] is True
    assert [entry["path"] for entry in restored["entries"]] == ["ok.txt"]


def test_mtime_only_change_keeps_identity_without_content_reads(world, monkeypatch):
    broker_module, _docker_manager, data = world
    path = _put(data, "same.txt", b"payload")
    broker = broker_module.OutputsBroker()
    before = broker.reconcile(CHAT)
    entry = _entry_by_path(before, "same.txt")
    before_bytes = _index(data).read_bytes()
    identity = (path.stat().st_dev, path.stat().st_ino)
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000))
    original_read = broker_module.os.read
    content_reads = {"count": 0}

    def count_output_reads(fd, count):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == identity:
            content_reads["count"] += 1
        return original_read(fd, count)

    monkeypatch.setattr(broker_module.os, "read", count_output_reads)
    after = broker.reconcile(CHAT)
    after_entry = _entry_by_path(after, "same.txt")

    assert after["unchanged"] is True
    assert after_entry["file_id"] == entry["file_id"]
    assert after_entry["revision"] == entry["revision"]
    assert after_entry["hash"] == entry["hash"]
    assert after_entry["mtime_ns"] == path.stat().st_mtime_ns
    assert _index(data).read_bytes() == before_bytes
    assert content_reads["count"] == 0


def test_missing_outputs_root_with_active_entries_is_retryable_and_restores_ids(world):
    broker_module, _docker_manager, data = world
    broker = broker_module.OutputsBroker()
    empty = broker.reconcile(CHAT)
    assert empty["revision"] == 0
    assert empty["entries"] == []

    _put(data, "keep.txt", b"keep")
    indexed = broker.reconcile(CHAT)
    keep_id = _entry_by_path(indexed, "keep.txt")["file_id"]
    before = _index(data).read_bytes()
    outputs = _outputs(data)
    parked = outputs.with_name("outputs-parked")
    outputs.rename(parked)
    with pytest.raises(broker_module.UnstableReadError, match="outputs root"):
        broker.reconcile(CHAT)
    assert _index(data).read_bytes() == before

    parked.rename(outputs)
    restored = broker.reconcile(CHAT)
    assert restored["unchanged"] is True
    assert _entry_by_path(restored, "keep.txt")["file_id"] == keep_id


def test_deep_directory_tree_is_traversed_without_recursion_error(world):
    broker_module, _docker_manager, data = world
    nested = _outputs(data)
    nested.mkdir(parents=True)
    for index in range(80):
        nested = nested / f"d{index}"
        os.mkdir(nested)
    (nested / "leaf.txt").write_bytes(b"leaf")
    original_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(50)
    try:
        listing = broker_module.OutputsBroker().reconcile(CHAT)
    finally:
        sys.setrecursionlimit(original_limit)

    assert listing["total"] == 1
    assert listing["entries"][0]["name"] == "leaf.txt"
    assert listing["entries"][0]["path"].endswith("/leaf.txt")


# Child instruments stdlib fcntl.flock only. LOCK_EX must observe a real
# LOCK_NB denial before blocking; the parent waits for that contention marker.
# OCU_CHILD_OP selects the locked payload; OCU_LOCK_DENIAL is the in-child
# negative control when the exclusive lock is granted without contention.
_PROCESS_WHILE_LOCKED = r'''
import fcntl
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ["OCU_SERVER_DIR"])
os.environ["BASE_DATA_DIR"] = os.environ["OCU_BASE"]
os.environ["DOCKER_HOST"] = "unix:///tmp/ocu-acceptance-no-docker.sock"
os.environ["DOCKER_SOCKET"] = "unix:///tmp/ocu-acceptance-no-docker.sock"

import outputs_broker

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
broker = outputs_broker.OutputsBroker()
if os.environ["OCU_CHILD_OP"] == "resolve":
    payload = {"path": broker.resolve_file_id(os.environ["OCU_CHAT"], os.environ["OCU_FILE_ID"])}
else:
    listing = broker.reconcile(os.environ["OCU_CHAT"])
    entry = next(item for item in listing["entries"] if item["path"] == os.environ["OCU_PATH"])
    payload = {"file_id": entry["file_id"], "revision": entry["revision"], "hash": entry["hash"]}
Path(os.environ["OCU_RESULT"]).write_text(json.dumps(payload), encoding="utf-8")
print(json.dumps(payload))
'''


def test_active_nested_file_id_resolves_without_mutating_index(world):
    broker_module, _docker_manager, data = world
    broker = broker_module.OutputsBroker()
    _put(data, "docs/report.docx", b"nested-report")
    listing = broker.reconcile(CHAT)
    file_id = _entry_by_path(listing, "docs/report.docx")["file_id"]
    before_bytes = _index(data).read_bytes()
    before_counter = _read_index(data)["counter"]

    assert broker.resolve_file_id(CHAT, file_id) == "docs/report.docx"
    assert _index(data).read_bytes() == before_bytes
    assert _read_index(data)["counter"] == before_counter
    assert before_counter == listing["revision"]


def test_recorded_rename_is_followed_and_unrecorded_rename_keeps_indexed_path(world):
    broker_module, _docker_manager, data = world
    broker = broker_module.OutputsBroker()
    old = _put(data, "report.docx", b"rename-body")
    listing = broker.reconcile(CHAT)
    file_id = _entry_by_path(listing, "report.docx")["file_id"]

    old.rename(_outputs(data) / "final.docx")
    assert broker.resolve_file_id(CHAT, file_id) == "report.docx"

    recorded = broker.reconcile(CHAT)
    assert _entry_by_path(recorded, "final.docx")["file_id"] == file_id
    before_bytes = _index(data).read_bytes()
    assert broker.resolve_file_id(CHAT, file_id) == "final.docx"
    assert _index(data).read_bytes() == before_bytes
    assert _read_index(data)["counter"] == recorded["revision"]


def test_tombstoned_id_fails_after_path_reuse(world):
    broker_module, _docker_manager, data = world
    broker = broker_module.OutputsBroker()
    path = _put(data, "report.docx", b"original")
    listing = broker.reconcile(CHAT)
    old_id = _entry_by_path(listing, "report.docx")["file_id"]

    path.unlink()
    broker.reconcile(CHAT)
    _put(data, "report.docx", b"replacement")
    reused = broker.reconcile(CHAT)
    new_id = _entry_by_path(reused, "report.docx")["file_id"]
    before_bytes = _index(data).read_bytes()

    assert new_id != old_id
    assert old_id in _read_index(data)["tombstones"]
    with pytest.raises(broker_module.FileIdNotFoundError):
        broker.resolve_file_id(CHAT, old_id)
    assert broker.resolve_file_id(CHAT, new_id) == "report.docx"
    assert _index(data).read_bytes() == before_bytes


def test_unknown_malformed_and_missing_index_fail_without_creating_index(world):
    broker_module, _docker_manager, data = world
    broker = broker_module.OutputsBroker()
    unknown = "11111111-1111-4111-8111-111111111111"
    malformed = "not-a-uuid"

    with pytest.raises(broker_module.FileIdNotFoundError):
        broker.resolve_file_id(CHAT, unknown)
    with pytest.raises(broker_module.FileIdNotFoundError):
        broker.resolve_file_id(CHAT, malformed)
    assert not _index(data).exists()

    _put(data, "report.docx", b"indexed")
    listing = broker.reconcile(CHAT)
    live_id = _entry_by_path(listing, "report.docx")["file_id"]
    before_bytes = _index(data).read_bytes()
    with pytest.raises(broker_module.FileIdNotFoundError):
        broker.resolve_file_id(CHAT, unknown)
    with pytest.raises(broker_module.FileIdNotFoundError):
        broker.resolve_file_id(CHAT, malformed)
    with pytest.raises(broker_module.FileIdNotFoundError):
        broker.resolve_file_id(CHAT, live_id.upper())
    assert _index(data).read_bytes() == before_bytes


def test_corrupt_and_unreadable_indexes_retain_corruption_errors_without_mutation(world, monkeypatch):
    broker_module, _docker_manager, data = world
    broker = broker_module.OutputsBroker()
    _put(data, "report.docx", b"indexed")
    listing = broker.reconcile(CHAT)
    file_id = _entry_by_path(listing, "report.docx")["file_id"]
    index_path = _index(data)
    encoded_valid = index_path.read_bytes()

    broken = b"{broken-json"
    index_path.write_bytes(broken)
    with pytest.raises(broker_module.CorruptIndexError):
        broker.resolve_file_id(CHAT, file_id)
    assert index_path.read_bytes() == broken

    malformed = {
        "schema_version": 1,
        "counter": 1,
        "active": {},
        "fingerprints": {},
        "tombstones": {"00000000-0000-4000-8000-000000000000": []},
    }
    encoded_malformed = json.dumps(malformed)
    index_path.write_text(encoded_malformed, encoding="utf-8")
    with pytest.raises(broker_module.CorruptIndexError):
        broker.resolve_file_id(CHAT, file_id)
    assert index_path.read_text(encoding="utf-8") == encoded_malformed

    index_path.write_bytes(encoded_valid)
    index_identity = (index_path.stat().st_dev, index_path.stat().st_ino)
    original_open = broker_module.os.open

    def deny_index_open(path, flags, *args, **kwargs):
        dir_fd = kwargs.get("dir_fd")
        try:
            info = (
                os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
                if dir_fd is not None
                else os.lstat(path)
            )
        except (OSError, TypeError, ValueError):
            info = None
        if info is not None and (info.st_dev, info.st_ino) == index_identity:
            raise OSError(errno.EACCES, "Permission denied")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(broker_module.os, "open", deny_index_open)
    with pytest.raises(broker_module.UnstableReadError, match="cannot safely open broker index") as raised:
        broker.resolve_file_id(CHAT, file_id)
    assert not isinstance(raised.value, broker_module.FileIdNotFoundError)
    assert not isinstance(raised.value, broker_module.CorruptIndexError)
    assert index_path.read_bytes() == encoded_valid


def test_resolution_opens_no_workspace_content_and_never_contacts_docker(world, monkeypatch):
    """Resolution has one Docker-free path; sandbox stopped/paused/absent are not distinct branches."""
    broker_module, docker_manager, data = world
    broker = broker_module.OutputsBroker()
    path = _put(data, "docs/report.docx", b"do-not-read")
    listing = broker.reconcile(CHAT)
    file_id = _entry_by_path(listing, "docs/report.docx")["file_id"]
    identity = (path.stat().st_dev, path.stat().st_ino)
    original_open = broker_module.os.open
    original_read = broker_module.os.read
    original_scandir = broker_module.os.scandir
    content_reads = {"count": 0}
    workspace_scans = {"count": 0}
    output_dir = _outputs(data)

    def forbid_workspace_open(path, flags, *args, **kwargs):
        dir_fd = kwargs.get("dir_fd")
        try:
            info = (
                os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
                if dir_fd is not None
                else os.lstat(path)
            )
        except (OSError, TypeError, ValueError):
            info = None
        if info is not None and (info.st_dev, info.st_ino) == identity:
            raise OSError(errno.EACCES, "workspace content must not be opened")
        return original_open(path, flags, *args, **kwargs)

    def count_output_reads(fd, count):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == identity:
            content_reads["count"] += 1
        return original_read(fd, count)

    def count_workspace_scans(target, *args, **kwargs):
        if isinstance(target, int):
            info = os.fstat(target)
            directory = os.stat(output_dir)
            if (info.st_dev, info.st_ino) == (directory.st_dev, directory.st_ino):
                workspace_scans["count"] += 1
        return original_scandir(target, *args, **kwargs)

    def forbidden_docker(*_args, **_kwargs):
        raise AssertionError("file-id resolution must not contact Docker")

    monkeypatch.setattr(broker_module.os, "open", forbid_workspace_open)
    monkeypatch.setattr(broker_module.os, "read", count_output_reads)
    monkeypatch.setattr(broker_module.os, "scandir", count_workspace_scans)
    monkeypatch.setattr(docker_manager, "get_docker_client", forbidden_docker)
    monkeypatch.setattr(docker_manager.docker, "DockerClient", forbidden_docker)
    assert broker.resolve_file_id(CHAT, file_id) == "docs/report.docx"

    assert content_reads["count"] == 0
    assert workspace_scans["count"] == 0


def test_resolution_returns_while_same_thread_already_holds_lock(world):
    broker_module, docker_manager, data = world
    broker = broker_module.OutputsBroker()
    _put(data, "docs/report.docx", b"locked")
    listing = broker.reconcile(CHAT)
    file_id = _entry_by_path(listing, "docs/report.docx")["file_id"]

    with docker_manager._combined_lock(CHAT):
        assert docker_manager._FLOCK_DEPTH[CHAT] == 1
        assert broker.resolve_file_id(CHAT, file_id) == "docs/report.docx"
        assert docker_manager._FLOCK_DEPTH[CHAT] == 1
    assert CHAT not in docker_manager._FLOCK_DEPTH


def test_cross_process_resolution_waits_for_writer_commit_then_observes_new_path(world):
    broker_module, docker_manager, data = world
    broker = broker_module.OutputsBroker()
    old = _put(data, "report.docx", b"locked-rename")
    listing = broker.reconcile(CHAT)
    file_id = _entry_by_path(listing, "report.docx")["file_id"]
    contended = data.parent / "resolve-contended"
    result = data.parent / "resolve-result.json"
    environment = _broker_child_env(
        data,
        OCU_FILE_ID=file_id,
        OCU_CONTENDED=str(contended),
        OCU_RESULT=str(result),
        OCU_CHILD_OP="resolve",
        OCU_LOCK_DENIAL="resolver lock was granted without contention",
    )

    child = None
    try:
        with docker_manager._combined_lock(CHAT):
            child = subprocess.Popen(
                [sys.executable, "-c", _PROCESS_WHILE_LOCKED],
                cwd=str(SERVER_DIR),
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 5
            while not contended.exists():
                if time.monotonic() >= deadline or child.poll() is not None:
                    raise AssertionError(
                        ("resolver did not contend for the lifecycle flock", child.poll(), result.exists())
                    )
                time.sleep(0.005)
            assert child.poll() is None
            assert not result.exists()
            old.rename(_outputs(data) / "final.docx")
            committed = broker.reconcile(CHAT)
            assert _entry_by_path(committed, "final.docx")["file_id"] == file_id
            assert child.poll() is None
            assert not result.exists()

        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, (stdout, stderr)
        assert json.loads(result.read_text(encoding="utf-8")) == {"path": "final.docx"}
        assert json.loads(stdout.strip().splitlines()[-1]) == {"path": "final.docx"}
    finally:
        _stop_child(child)


_PROCESS_READ_REGISTERED = r'''
import json
import os
import sys

sys.path.insert(0, os.environ["OCU_SERVER_DIR"])
os.environ["BASE_DATA_DIR"] = os.environ["OCU_BASE"]
os.environ["DOCKER_HOST"] = "unix:///tmp/ocu-acceptance-no-docker.sock"
os.environ["DOCKER_SOCKET"] = "unix:///tmp/ocu-acceptance-no-docker.sock"

import outputs_broker

broker = outputs_broker.OutputsBroker()
listing = broker.reconcile(os.environ["OCU_CHAT"])
entry = next(item for item in listing["entries"] if item["path"] == os.environ["OCU_PATH"])
print(json.dumps({
    "file_id": entry["file_id"],
    "revision": entry["revision"],
    "hash": entry["hash"],
    "counter": broker.current_revision(os.environ["OCU_CHAT"]),
    "unchanged": listing["unchanged"],
}))
'''


def _seed_report_revision_seven(broker, data, body: bytes) -> dict:
    for index in range(6):
        _put(data, f"pad{index}.txt", b"p")
    _put(data, "report.docx", body)
    broker.reconcile(CHAT)
    _put(data, "pad6.txt", b"p")
    _put(data, "pad7.txt", b"p")
    listing = broker.reconcile(CHAT)
    entry = _entry_by_path(listing, "report.docx")
    assert listing["revision"] == 9
    assert entry["revision"] == 7
    return entry


def _forbid_inode_open(broker_module, monkeypatch, *paths: Path) -> None:
    identities = {(path.lstat().st_dev, path.lstat().st_ino) for path in paths}
    original_open = broker_module.os.open
    nofollow = getattr(os, "O_NOFOLLOW", 0)

    def guarded(target, flags, *args, **kwargs):
        dir_fd = kwargs.get("dir_fd")
        follow_symlinks = not (flags & nofollow)
        try:
            info = os.stat(target, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        except (OSError, TypeError, ValueError):
            info = None
        if info is not None and (info.st_dev, info.st_ino) in identities:
            raise AssertionError("external content must not be opened")
        return original_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(broker_module.os, "open", guarded)


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


def _broker_child_env(data: Path, **extra: str) -> dict[str, str]:
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


def test_same_size_host_write_advances_revision_and_hash(world):
    broker_module, docker_manager, data = world
    broker = broker_module.OutputsBroker()
    old_body = b"AAAAAAAA"
    new_body = b"BBBBBBBB"
    assert len(old_body) == len(new_body)
    seeded = _seed_report_revision_seven(broker, data, old_body)
    path = _outputs(data) / "report.docx"
    stored_mtime = seeded["mtime_ns"]

    with docker_manager._combined_lock(CHAT):
        path.write_bytes(new_body)
        os.utime(path, ns=(path.stat().st_atime_ns, stored_mtime + 1_000_000))
        fresh_mtime = path.stat().st_mtime_ns
        registered = broker.register_host_write(CHAT, "report.docx")

    expected_hash = hashlib.sha256(new_body).hexdigest()
    persisted = _read_index(data)
    entry = persisted["active"]["report.docx"]
    assert registered["file_id"] == seeded["file_id"]
    assert registered["revision"] == 10
    assert registered["hash"] == expected_hash
    assert persisted["counter"] == 10
    assert entry["file_id"] == seeded["file_id"]
    assert entry["revision"] == 10
    assert entry["hash"] == expected_hash
    assert entry["size"] == len(new_body)
    assert registered["mtime_ns"] == fresh_mtime
    assert entry["mtime_ns"] == fresh_mtime
    assert fresh_mtime != stored_mtime
    assert persisted["fingerprints"][f"{entry['size']}:{expected_hash}"] == [seeded["file_id"]]


def test_registered_size_change_is_not_counted_again_by_reconcile(world):
    broker_module, docker_manager, data = world
    broker = broker_module.OutputsBroker()
    seeded = _seed_report_revision_seven(broker, data, b"AAAAAAAA")
    path = _outputs(data) / "report.docx"

    with docker_manager._combined_lock(CHAT):
        path.write_bytes(b"CCCCCCCCC")
        registered = broker.register_host_write(CHAT, "report.docx")

    listing = broker.reconcile(CHAT)
    entry = _entry_by_path(listing, "report.docx")
    assert listing["unchanged"] is True
    assert listing["revision"] == 10
    assert _read_index(data)["counter"] == 10
    assert entry["file_id"] == seeded["file_id"] == registered["file_id"]
    assert entry["revision"] == 10 == registered["revision"]
    assert entry["hash"] == hashlib.sha256(b"CCCCCCCCC").hexdigest()


def test_registering_one_path_leaves_sibling_entry_bytes_unchanged(world, monkeypatch):
    broker_module, docker_manager, data = world
    broker = broker_module.OutputsBroker()
    _put(data, "a.docx", b"aaa")
    _put(data, "b.docx", b"bbb")
    broker.reconcile(CHAT)
    before = _read_index(data)
    sibling = dict(before["active"]["b.docx"])
    scans = {"count": 0}

    def forbid_register_scandir(target, *args, **kwargs):
        scans["count"] += 1
        raise AssertionError("register_host_write must not scan the outputs tree")

    (_outputs(data) / "b.docx").write_bytes(b"BBB-DIRTY-SIBLING")
    with docker_manager._combined_lock(CHAT):
        (_outputs(data) / "a.docx").write_bytes(b"AAA")
        with monkeypatch.context() as patches:
            patches.setattr(broker_module.os, "scandir", forbid_register_scandir)
            registered = broker.register_host_write(CHAT, "a.docx")

    persisted = _read_index(data)
    assert persisted["active"]["b.docx"] == sibling
    assert persisted["tombstones"] == before["tombstones"]
    assert registered["file_id"] == before["active"]["a.docx"]["file_id"]
    assert registered["revision"] == before["counter"] + 1
    assert persisted["counter"] == before["counter"] + 1
    assert persisted["active"]["a.docx"]["revision"] != sibling["revision"]
    assert scans["count"] == 0


def test_unindexed_path_gets_fresh_id_not_borrowed_from_tombstone(world):
    broker_module, docker_manager, data = world
    broker = broker_module.OutputsBroker()
    gone = _put(data, "gone.docx", b"old")
    listing = broker.reconcile(CHAT)
    tombstone_id = _entry_by_path(listing, "gone.docx")["file_id"]
    gone.unlink()
    broker.reconcile(CHAT)
    before = _read_index(data)
    assert tombstone_id in before["tombstones"]
    _put(data, "report (2).docx", b"copy")

    with docker_manager._combined_lock(CHAT):
        registered = broker.register_host_write(CHAT, "report (2).docx")

    _assert_uuid(registered["file_id"])
    assert registered["file_id"] != tombstone_id
    assert registered["file_id"] not in _read_index(data)["tombstones"]
    assert registered["revision"] == before["counter"] + 1
    assert _read_index(data)["counter"] == before["counter"] + 1
    assert _read_index(data)["tombstones"] == before["tombstones"]
    assert broker.resolve_file_id(CHAT, registered["file_id"]) == "report (2).docx"


def test_valid_registration_creates_missing_index_once(world):
    broker_module, docker_manager, data = world
    broker = broker_module.OutputsBroker()
    _put(data, "fresh.docx", b"new")
    assert not _index(data).exists()

    with docker_manager._combined_lock(CHAT):
        registered = broker.register_host_write(CHAT, "fresh.docx")

    _assert_uuid(registered["file_id"])
    assert registered["revision"] == 1
    assert _index(data).exists()
    assert _read_index(data)["counter"] == 1
    assert broker.resolve_file_id(CHAT, registered["file_id"]) == "fresh.docx"


def test_fresh_process_reads_registered_id_revision_and_hash(world):
    broker_module, docker_manager, data = world
    broker = broker_module.OutputsBroker()
    _put(data, "report.docx", b"AAAAAAAA")
    listing = broker.reconcile(CHAT)
    file_id = _entry_by_path(listing, "report.docx")["file_id"]
    new_body = b"BBBBBBBB"
    with docker_manager._combined_lock(CHAT):
        (_outputs(data) / "report.docx").write_bytes(new_body)
        registered = broker.register_host_write(CHAT, "report.docx")

    environment = _broker_child_env(data, OCU_PATH="report.docx")
    completed = subprocess.run(
        [sys.executable, "-c", _PROCESS_READ_REGISTERED],
        cwd=str(SERVER_DIR),
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    observed = json.loads(completed.stdout.strip().splitlines()[-1])
    expected_hash = hashlib.sha256(new_body).hexdigest()
    assert observed == {
        "file_id": file_id,
        "revision": registered["revision"],
        "hash": expected_hash,
        "counter": registered["revision"],
        "unchanged": True,
    }


def test_register_precommit_replace_failure_keeps_predecessor(world, monkeypatch):
    broker_module, _docker_manager, data = world
    broker = broker_module.OutputsBroker()
    _put(data, "a.docx", b"a")
    broker.reconcile(CHAT)
    before = _index(data).read_bytes()
    _put(data, "b.docx", b"b")

    def rejected_replace(*_args, **_kwargs):
        raise OSError("controlled atomic replacement failure")

    monkeypatch.setattr(broker_module.os, "replace", rejected_replace)
    with pytest.raises(OSError, match="controlled atomic replacement failure"):
        broker.register_host_write(CHAT, "b.docx")
    assert _index(data).read_bytes() == before
    assert not list(_index(data).parent.glob("*.tmp"))


def test_register_postcommit_durability_failure_is_not_rollback(world, monkeypatch):
    broker_module, _docker_manager, data = world
    broker = broker_module.OutputsBroker()
    _put(data, "a.docx", b"a")
    broker.reconcile(CHAT)
    before = _index(data).read_bytes()
    _put(data, "b.docx", b"b")
    original_fsync = broker_module.os.fsync

    def fail_directory_sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("controlled directory fsync failure")
        return original_fsync(fd)

    with monkeypatch.context() as patches:
        patches.setattr(broker_module.os, "fsync", fail_directory_sync)
        with pytest.raises(broker_module.CommitDurabilityError, match="committed"):
            broker.register_host_write(CHAT, "b.docx")

    assert _index(data).read_bytes() != before
    persisted = _read_index(data)
    assert "b.docx" in persisted["active"]
    assert persisted["counter"] == 2


def test_writer_lock_excludes_reconcile_until_registration_commits(world):
    broker_module, docker_manager, data = world
    broker = broker_module.OutputsBroker()
    path = _put(data, "report.docx", b"AAAAAAAA")
    listing = broker.reconcile(CHAT)
    file_id = _entry_by_path(listing, "report.docx")["file_id"]
    contended = data.parent / "register-contended"
    result = data.parent / "register-result.json"
    environment = _broker_child_env(
        data,
        OCU_PATH="report.docx",
        OCU_CONTENDED=str(contended),
        OCU_RESULT=str(result),
        OCU_CHILD_OP="reconcile",
        OCU_LOCK_DENIAL="reconcile lock was granted without contention",
    )
    child = None
    try:
        with docker_manager._combined_lock(CHAT):
            path.write_bytes(b"BBBBBBBB")
            child = subprocess.Popen(
                [sys.executable, "-c", _PROCESS_WHILE_LOCKED],
                cwd=str(SERVER_DIR),
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 5
            while not contended.exists():
                if time.monotonic() >= deadline or child.poll() is not None:
                    raise AssertionError(
                        ("reconcile did not contend for the lifecycle flock", child.poll(), result.exists())
                    )
                time.sleep(0.005)
            assert child.poll() is None
            assert not result.exists()
            registered = broker.register_host_write(CHAT, "report.docx")
            assert child.poll() is None
            assert not result.exists()

        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, (stdout, stderr)
        expected = {
            "file_id": file_id,
            "revision": registered["revision"],
            "hash": hashlib.sha256(b"BBBBBBBB").hexdigest(),
        }
        assert json.loads(result.read_text(encoding="utf-8")) == expected
        assert json.loads(stdout.strip().splitlines()[-1]) == expected
        assert registered["file_id"] == file_id
        assert registered["revision"] == listing["revision"] + 1
    finally:
        _stop_child(child)


def test_nested_host_write_retains_identity_and_is_unchanged_on_reconcile(world):
    broker_module, docker_manager, data = world
    broker = broker_module.OutputsBroker()
    old_body = b"nested-old"
    new_body = b"nested-new"
    path = _put(data, "a/b/c.docx", old_body)
    listing = broker.reconcile(CHAT)
    seeded = _entry_by_path(listing, "a/b/c.docx")
    prior_counter = listing["revision"]

    with docker_manager._combined_lock(CHAT):
        path.write_bytes(new_body)
        registered = broker.register_host_write(CHAT, "a/b/c.docx")

    expected_hash = hashlib.sha256(new_body).hexdigest()
    persisted = _read_index(data)
    entry = persisted["active"]["a/b/c.docx"]
    fresh = path.stat()
    assert registered["file_id"] == seeded["file_id"]
    assert registered["path"] == "a/b/c.docx"
    assert registered["name"] == "c.docx"
    assert registered["size"] == len(new_body)
    assert registered["hash"] == expected_hash
    assert registered["revision"] == prior_counter + 1
    assert registered["mtime_ns"] == fresh.st_mtime_ns
    assert entry == registered
    assert persisted["counter"] == prior_counter + 1
    listing = broker.reconcile(CHAT)
    assert listing["unchanged"] is True
    assert listing["revision"] == prior_counter + 1
    assert _entry_by_path(listing, "a/b/c.docx")["file_id"] == seeded["file_id"]
    assert _read_index(data)["counter"] == prior_counter + 1


def test_register_corrupt_index_fails_closed_without_reset(world):
    broker_module, docker_manager, data = world
    broker = broker_module.OutputsBroker()
    _put(data, "keep.docx", b"k")
    broker.reconcile(CHAT)
    index_path = _index(data)
    broken = b"{broken-json"
    index_path.write_bytes(broken)
    _put(data, "keep.docx", b"K")

    with docker_manager._combined_lock(CHAT):
        with pytest.raises(broker_module.CorruptIndexError):
            broker.register_host_write(CHAT, "keep.docx")

    assert index_path.read_bytes() == broken


def test_existing_file_registers_at_active_file_limit(world):
    broker_module, docker_manager, data = world
    broker = broker_module.OutputsBroker(max_active_files=1)
    _put(data, "keep.docx", b"k")
    listing = broker.reconcile(CHAT)
    seeded = _entry_by_path(listing, "keep.docx")
    before_counter = _read_index(data)["counter"]

    with docker_manager._combined_lock(CHAT):
        (_outputs(data) / "keep.docx").write_bytes(b"K")
        registered = broker.register_host_write(CHAT, "keep.docx")

    persisted = _read_index(data)
    assert registered["file_id"] == seeded["file_id"]
    assert registered["revision"] == before_counter + 1
    assert persisted["counter"] == before_counter + 1
    assert list(persisted["active"]) == ["keep.docx"]

    _put(data, "extra.docx", b"x")
    after_existing = _index(data).read_bytes()
    with pytest.raises(broker_module.LimitExceededError):
        broker.register_host_write(CHAT, "extra.docx")
    assert _index(data).read_bytes() == after_existing


def _prepare_register_rejection(world, name: str):
    broker_module, docker_manager, data = world
    broker = broker_module.OutputsBroker()
    outside = data.parent / "external.bin"
    outside.write_bytes(b"EXTERNAL-SECRET-BYTES")
    guarded = [outside]
    if name == "no-index":
        return broker_module, broker, data, outside, guarded, "../other/a.docx", None, None
    _put(data, "keep.docx", b"k")
    broker.reconcile(CHAT)
    before_bytes = _index(data).read_bytes()
    before_counter = _read_index(data)["counter"]
    target = "keep.docx"
    if name == "absolute":
        target = str(outside)
    elif name == "traversal":
        target = "../other/a.docx"
    elif name == "foreign":
        foreign = _put(data, "foreign.docx", b"FOREIGN-BYTES", OTHER_CHAT)
        guarded.append(foreign)
        target = f"../{OTHER_CHAT}/outputs/foreign.docx"
    elif name == "hidden-file":
        _put(data, ".staged.docx", b"hidden-file")
        target = ".staged.docx"
    elif name == "hidden-dir":
        _put(data, ".cache/a.docx", b"hidden-dir")
        target = ".cache/a.docx"
    elif name == "symlink-self":
        os.symlink(outside, _outputs(data) / "link.docx")
        target = "link.docx"
    elif name == "symlink-parent":
        linked = _outputs(data) / "linked"
        os.symlink(outside.parent, linked)
        target = "linked/external.bin"
    elif name == "missing":
        target = "missing.docx"
    elif name == "directory":
        (_outputs(data) / "folder").mkdir()
        target = "folder"
    elif name == "fifo":
        os.mkfifo(_outputs(data) / "pipe.fifo")
        target = "pipe.fifo"
    elif name == "file-size":
        broker = broker_module.OutputsBroker(max_file_size=3)
        _put(data, "large.docx", b"four")
        target = "large.docx"
    elif name == "active-count":
        broker = broker_module.OutputsBroker(max_active_files=1)
        _put(data, "extra.docx", b"x")
        target = "extra.docx"
    elif name == "index-size":
        broker = broker_module.OutputsBroker(max_index_size=len(before_bytes) + 1)
        _put(data, "extra.docx", b"x")
        target = "extra.docx"
    return broker_module, broker, data, outside, guarded, target, before_bytes, before_counter

@pytest.mark.parametrize(
    "name",
    [
        "absolute",
        "traversal",
        "foreign",
        "hidden-file",
        "hidden-dir",
        "symlink-self",
        "symlink-parent",
        "missing",
        "directory",
        "fifo",
        "file-size",
        "active-count",
        "index-size",
        "no-index",
    ],
)
def test_register_rejection_preserves_predecessor_and_skips_external_bytes(world, monkeypatch, name):
    broker_module, broker, data, outside, guarded, target, before_bytes, before_counter = _prepare_register_rejection(world, name)
    _forbid_inode_open(broker_module, monkeypatch, *guarded)
    with pytest.raises(broker_module.OutputsBrokerError):
        broker.register_host_write(CHAT, target)
    assert outside.read_bytes() == b"EXTERNAL-SECRET-BYTES"
    if name == "no-index":
        assert not _index(data).exists()
        return
    assert _index(data).read_bytes() == before_bytes
    assert _read_index(data)["counter"] == before_counter
    if name in {"hidden-file", "hidden-dir"}:
        assert target not in _read_index(data)["active"]
    if name == "foreign":
        assert (_outputs(data, OTHER_CHAT) / "foreign.docx").read_bytes() == b"FOREIGN-BYTES"



