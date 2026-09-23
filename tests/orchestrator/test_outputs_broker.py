# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Real-filesystem contract tests for the persisted OCU outputs broker.

The public seam is ``OutputsBroker``.  Tests deliberately use real paths,
file descriptors, atomic replacement, and separate Python processes; Docker is
forbidden by the fixture and by the tests.
"""
from __future__ import annotations

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


def test_unchanged_five_thousand_file_scan_reads_no_contents_and_rewrites_no_index(world):
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

    for cursor in ("", "not-a-cursor", "1:-1", f"{broker.current_revision(CHAT)}:99"):
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

    for bad in (b"{broken-json", b'{"schema_version": 999}'):
        index_path.write_bytes(bad)
        with pytest.raises(broker_module.CorruptIndexError):
            broker.reconcile(CHAT)
        assert index_path.read_bytes() == bad
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
