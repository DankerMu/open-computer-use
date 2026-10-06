# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Surviving publication obligations through the existing Office sweep."""
from __future__ import annotations

import copy
import errno
import hashlib
import importlib
import json
import os
import subprocess
import sys
import time

import pytest
from docker.errors import NotFound

from tests.orchestrator._office_store import CHAT, _outputs
from tests.orchestrator.test_lifecycle import _bind_outputs_broker, _container, _docker
from tests.orchestrator.test_office_publish import (
    BASELINE, OBLIGATION, SAVED, SESSION, _prepared, _reload_publish, _sha,
)
from tests.orchestrator._office_store import _child_env, _stop_child, _wait_marker
from tests.orchestrator.test_office_sessions import _snapshot


def test_sweep_publishes_unstarted_obligation_in_office_only_chat(world, monkeypatch):
    store_mod, manager, data = world
    monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", "http://documentserver.invalid")
    _bind_outputs_broker(manager)
    for module in ("office.sessions", "office.publish", "office.sweep"):
        if module in sys.modules:
            importlib.reload(sys.modules[module])

    from office.sweep import sweep_office_sessions
    from office.versions import read_version_bytes
    from outputs_broker import OutputsBroker

    now = 1_700_000_000.25
    baseline = b"workspace-v1"
    saved = b"published-v2"
    baseline_hash = hashlib.sha256(baseline).hexdigest()
    saved_hash = hashlib.sha256(saved).hexdigest()
    outputs = _outputs(data)
    outputs.mkdir(parents=True)
    target = outputs / "report.docx"
    target.write_bytes(baseline)
    broker = OutputsBroker()
    indexed = broker.reconcile(CHAT)
    file_id = indexed["entries"][0]["file_id"]
    store = store_mod.OfficeStore()
    store.store_version(CHAT, file_id, baseline, source="workspace", parent=None,
                        published=True, min_free_bytes=0)
    store.store_version(CHAT, file_id, saved, source="save", parent=1,
                        published=False, min_free_bytes=0)
    session_id = "recovery-session"
    journal_id = "recovery-save-1"

    def seed(state):
        state["documents"][file_id].update({
            "file_id": file_id, "type": "docx", "path": "report.docx",
            "published_version": 1, "published_sha256": baseline_hash,
        })
        state["sessions"][session_id] = {
            "session_id": session_id, "file_id": file_id,
            "document_key": "recovery-key", "restore_epoch": "epoch-one",
            "baseline_sha256": baseline_hash, "state": "editing", "reason": None,
            "save_seq": 1, "last_committed_seq": 1, "last_published_seq": 0,
            "save_intents": {"1": "publish"}, "workspace_changed": False,
            "saved_as": None, "last_activity_at": now,
        }
        state["receipts"][session_id] = {"1": {
            "status": 6, "version": 2, "sha256": saved_hash, "answer": {"error": 0},
        }}
        state["journal"][journal_id] = {
            "file_id": file_id, "version": 2, "session_id": session_id,
            "save_seq": 1, "requester": "save",
        }

    before = store.update(CHAT, seed)
    engine = _docker([])
    manager._docker_client = engine
    assert not (data / CHAT / ".meta.json").exists()
    assert not (data / CHAT / ".idle.json").exists()

    sweep_office_sessions(now=now)

    assert target.read_bytes() == b"published-v2"
    expected = copy.deepcopy(before)
    expected["documents"][file_id]["versions"][1]["published"] = True
    expected["documents"][file_id]["published_version"] = 2
    expected["documents"][file_id]["published_sha256"] = saved_hash
    expected["sessions"][session_id]["baseline_sha256"] = saved_hash
    expected["sessions"][session_id]["last_published_seq"] = 1
    del expected["journal"][journal_id]
    assert store.read(CHAT) == expected
    assert read_version_bytes(store, CHAT, saved_hash) == b"published-v2"
    assert broker.current_revision(CHAT) == indexed["revision"] + 1
    listing = broker.reconcile(CHAT)
    assert listing["revision"] == indexed["revision"] + 1
    assert listing["entries"][0]["file_id"] == file_id
    assert listing["entries"][0]["size"] == len(b"published-v2")
    assert listing["entries"][0]["revision"] == indexed["revision"] + 1
    engine.containers.create.assert_not_called()
    assert not (data / CHAT / ".meta.json").exists()
    assert not (data / CHAT / ".idle.json").exists()


def _recover():
    from office.publish import recover_publications
    recover_publications(CHAT)


def _published(fixture):
    state = fixture.store.read(CHAT)
    assert fixture.target.read_bytes() == SAVED
    assert state["journal"] == {}
    assert state["documents"][fixture.file_id]["published_version"] == 2
    assert state["documents"][fixture.file_id]["published_sha256"] == _sha(SAVED)
    assert state["documents"][fixture.file_id]["versions"][1]["published"] is True
    assert state["sessions"][SESSION]["baseline_sha256"] == _sha(SAVED)
    assert state["sessions"][SESSION]["last_published_seq"] == 3
    assert state["receipts"] == fixture.before["receipts"]
    listing = fixture.broker.reconcile(CHAT)
    assert listing["entries"][0]["file_id"] == fixture.file_id
    assert listing["entries"][0]["size"] == len(SAVED)
    blob = fixture.data / CHAT / ".ocu" / "office" / "versions" / _sha(SAVED)
    assert blob.read_bytes() == SAVED
    assert blob.stat().st_ino != fixture.target.stat().st_ino


def test_completed_recovery_is_a_filesystem_and_engine_noop(world):
    fixture = _prepared(world, "absent")
    _recover()
    _published(fixture)
    before = _snapshot(fixture.data)
    inode = fixture.target.stat().st_ino
    calls = list(fixture.engine.mock_calls)
    _recover()
    assert _snapshot(fixture.data) == before
    assert fixture.target.stat().st_ino == inode
    assert fixture.engine.mock_calls == calls


def test_recovery_finishes_matching_successor_without_rewriting_workspace(world):
    fixture = _prepared(world, "absent")
    fixture.target.write_bytes(SAVED)
    inode = fixture.target.stat().st_ino
    _recover()
    _published(fixture)
    assert fixture.target.stat().st_ino == inode
    assert fixture.broker.current_revision(CHAT) == fixture.indexed["revision"] + 1


def test_recovery_re_registers_visible_index_commit_without_rewriting(world):
    fixture = _prepared(world, "absent")
    fixture.target.write_bytes(SAVED)
    fixture.broker.register_host_write(CHAT, "report.docx")
    inode = fixture.target.stat().st_ino
    _recover()
    _published(fixture)
    assert fixture.target.stat().st_ino == inode
    assert fixture.broker.current_revision(CHAT) == fixture.indexed["revision"] + 2


def test_recovery_preserves_baseline_conflict_and_completes_the_obligation(world):
    fixture = _prepared(world, "absent")
    fixture.target.write_bytes(b"agent-change")
    _recover()
    state = fixture.store.read(CHAT)
    assert fixture.target.read_bytes() == b"agent-change"
    assert state["journal"] == {}
    assert state["documents"] == fixture.before["documents"]
    expected_sessions = copy.deepcopy(fixture.before["sessions"])
    expected_sessions[SESSION].update(state="conflict", reason="baseline_mismatch")
    assert state["sessions"] == expected_sessions
    assert fixture.broker.current_revision(CHAT) == fixture.indexed["revision"]


def test_new_publish_drives_same_document_dependencies_not_sorted_journal_names(world):
    from office.publish import publish
    fixture = _prepared(world, "absent")
    third = b"third-save-v3"
    fixture.store.store_version(CHAT, fixture.file_id, third, source="save", parent=2,
                               published=False, min_free_bytes=0)
    def seed(state):
        state["journal"]["z-older"] = state["journal"].pop(OBLIGATION)
        state["journal"]["a-newer"] = {
            "file_id": fixture.file_id, "version": 3, "session_id": SESSION,
            "save_seq": 4, "requester": "save",
        }
        state["sessions"][SESSION]["save_intents"]["4"] = "publish"
    fixture.store.update(CHAT, seed)
    result = publish(CHAT, "a-newer")
    assert result.outcome == "published"
    assert fixture.target.read_bytes() == b"third-save-v3"
    state = fixture.store.read(CHAT)
    assert state["journal"] == {}
    assert state["documents"][fixture.file_id]["published_version"] == 3
    assert [item["published"] for item in state["documents"][fixture.file_id]["versions"]] == [True, True, True]
    assert fixture.broker.current_revision(CHAT) == fixture.indexed["revision"] + 2


@pytest.mark.parametrize("obligation", (
    {"keep": True},
    {"file_id": "missing", "version": 2, "requester": "save"},
))
def test_malformed_obligation_is_retained_and_blocks_other_publication(world, obligation):
    from office.publish import publish
    fixture = _prepared(world, "absent")
    fixture.store.update(CHAT, lambda state: state["journal"].__setitem__("malformed", obligation))
    before = _snapshot(fixture.data)
    with pytest.raises(world[0].StateCorruptError):
        _recover()
    with pytest.raises(world[0].StateCorruptError):
        publish(CHAT, OBLIGATION)
    assert _snapshot(fixture.data) == before
    assert fixture.engine.mock_calls == []


def _marker(fixture, value):
    path = fixture.data / CHAT / ".ocu" / "office" / "fence.json"
    path.write_text(json.dumps(value))
    return path


def _engine_by_identity(fixture, *containers):
    def get(identity):
        for container in containers:
            if identity == container.id:
                return container
        for container in reversed(containers):
            if identity == container.name:
                return container
        raise NotFound(identity)
    fixture.engine.containers.get.side_effect = get


@pytest.mark.parametrize("age", (4.999, 5.0, 5.001))
def test_stale_fence_age_is_strict_and_owned_release_precedes_publication(world, age):
    from office.publish import RecoveryRequiredError, recover_publications
    fixture = _prepared(world)
    fixture.container.status = "paused"
    fixture.container.reload()
    fixture.container.pause.side_effect = lambda: setattr(fixture.container, "status", "paused")
    _engine_by_identity(fixture, fixture.container)
    marker = _marker(fixture, {
        "schema_version": 1, "container_id": fixture.container.id,
        "pause_started_at": 100,
    })
    if age <= 5:
        before = _snapshot(fixture.data)
        with pytest.raises(RecoveryRequiredError):
            recover_publications(CHAT, now=100 + age)
        assert _snapshot(fixture.data) == before
        fixture.container.unpause.assert_not_called()
    else:
        recover_publications(CHAT, now=100 + age)
        _published(fixture)
        assert not marker.exists()
        assert fixture.container.status == "running"
        assert fixture.container.unpause.call_count == 2


@pytest.mark.parametrize("marker", (
    {"schema_version": 1, "container_id": "cid-1", "pause_started_at": True},
    {"schema_version": 1, "container_id": "cid-1", "pause_started_at": float("inf")},
    {"schema_version": 2, "container_id": "cid-1", "pause_started_at": 100},
    {"schema_version": 1, "container_id": "", "pause_started_at": 100},
))
def test_corrupt_fence_never_authorizes_engine_action(world, marker):
    from office.publish import RecoveryRequiredError, recover_publications
    fixture = _prepared(world)
    _marker(fixture, marker)
    before = _snapshot(fixture.data)
    with pytest.raises(RecoveryRequiredError):
        recover_publications(CHAT, now=106)
    assert _snapshot(fixture.data) == before
    assert fixture.engine.mock_calls == []


def test_stale_fence_addresses_original_id_not_replacement_name(world):
    from office.publish import recover_publications
    fixture = _prepared(world)
    original = fixture.container
    original.status = "exited"
    original.reload()
    replacement = _container(original.name, status="paused", container_id="replacement-id")
    _engine_by_identity(fixture, original, replacement)
    _marker(fixture, {"schema_version": 1, "container_id": original.id, "pause_started_at": 100})
    recover_publications(CHAT, now=106)
    _published(fixture)
    original.unpause.assert_not_called()
    replacement.unpause.assert_not_called()


def test_failed_once_unpause_retains_marker_and_next_poll_recovers(world):
    from office.publish import RecoveryRequiredError, recover_publications
    fixture = _prepared(world)
    fixture.container.status = "paused"
    fixture.container.reload()
    fixture.container.pause.side_effect = lambda: setattr(fixture.container, "status", "paused")
    unpause = fixture.container.unpause.side_effect
    calls = 0
    def release():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(errno.EIO, "engine release failed")
        unpause()
    fixture.container.unpause.side_effect = release
    _engine_by_identity(fixture, fixture.container)
    marker = _marker(fixture, {"schema_version": 1, "container_id": fixture.container.id, "pause_started_at": 100})
    before = fixture.store.read(CHAT)
    with pytest.raises(RecoveryRequiredError):
        recover_publications(CHAT, now=106)
    assert marker.exists()
    assert fixture.store.read(CHAT) == before
    assert fixture.target.read_bytes() == BASELINE
    recover_publications(CHAT, now=107)
    _published(fixture)
    assert not marker.exists()


def test_uncertain_engine_observation_retains_owned_marker(world):
    from office.publish import RecoveryRequiredError, recover_publications
    fixture = _prepared(world)
    fixture.container.reload.side_effect = OSError(errno.EIO, "engine observation failed")
    _engine_by_identity(fixture, fixture.container)
    marker = _marker(fixture, {"schema_version": 1, "container_id": fixture.container.id, "pause_started_at": 100})
    before = fixture.store.read(CHAT)
    with pytest.raises(OSError):
        recover_publications(CHAT, now=106)
    assert marker.exists()
    assert fixture.store.read(CHAT) == before
    fixture.container.unpause.assert_not_called()


def test_absent_original_marker_does_not_release_external_pause(world):
    from office.publish import recover_publications
    fixture = _prepared(world)
    fixture.container.status = "paused"
    fixture.container.reload()
    _engine_by_identity(fixture, fixture.container)
    marker = _marker(fixture, {"schema_version": 1, "container_id": "removed-id", "pause_started_at": 100})
    recover_publications(CHAT, now=106)
    _published(fixture)
    assert not marker.exists()
    fixture.container.unpause.assert_not_called()
    fixture.container.pause.assert_not_called()


@pytest.mark.parametrize("error", (errno.EXDEV, errno.EOPNOTSUPP))
def test_unsupported_shared_hardlink_retains_obligation_without_copy_fallback(world, monkeypatch, error):
    from office.publish import publish
    fixture = _prepared(world, "absent")
    original = os.link
    def link(source, destination, *args, **kwargs):
        if isinstance(destination, str) and destination.startswith(".office-publish."):
            raise OSError(error, "unsupported exposure")
        return original(source, destination, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(os, "link", link)
        with pytest.raises(OSError) as caught:
            publish(CHAT, OBLIGATION)
    assert caught.value.errno == error
    assert fixture.target.read_bytes() == BASELINE
    assert OBLIGATION in fixture.store.read(CHAT)["journal"]
    assert not list(fixture.target.parent.glob(".office-publish.*"))
    _recover()
    _published(fixture)


_CRASH_WORKER = r'''
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from docker.errors import NotFound
import docker_manager
from office.publish import publish, recover_publications

data, chat = Path(os.environ["OCU_BASE"]), os.environ["OCU_CHAT"]
office = data / chat / ".ocu" / "office"
marker = Path(os.environ["CRASH_MARKER"])
cut = os.environ.get("CRASH_CUT", "")
mode = os.environ.get("CRASH_MODE", "publish")
def stop(point):
    if point == cut:
        marker.write_text(point)
        while True:
            time.sleep(0.05)
engine_path = Path(os.environ["ENGINE_STATE"]) if os.environ.get("ENGINE_STATE") else None
if engine_path:
    time.time = lambda: float(os.environ.get("WALL_TIME", 100.0 if mode == "publish" else 106.0))
class Container:
    id = "fresh-original-id"
    name = docker_manager._container_name(chat)
    def reload(self):
        self.status = json.loads(engine_path.read_bytes())["status"]
        self.attrs = {"State": {"Status": self.status, "Paused": self.status == "paused"}}
    def pause(self):
        stop("engine-pause-before")
        state = json.loads(engine_path.read_bytes())
        state["status"] = "paused"
        state["pauses"] += 1
        engine_path.write_text(json.dumps(state))
        stop("engine-pause-after")
    def unpause(self):
        stop("engine-unpause-before")
        state = json.loads(engine_path.read_bytes())
        state["status"] = "running"
        state["unpauses"] += 1
        engine_path.write_text(json.dumps(state))
        stop("engine-unpause-after")
container = Container()
def get(identity):
    if engine_path and identity in (container.id, container.name):
        container.reload()
        return container
    raise NotFound(identity)
docker_manager._docker_client = SimpleNamespace(containers=SimpleNamespace(get=get))
if os.environ.get("CONTENDED_MARKER"):
    import fcntl
    real_flock = fcntl.flock
    def flock(fd, flags):
        if flags == fcntl.LOCK_EX:
            try:
                return real_flock(fd, flags | fcntl.LOCK_NB)
            except BlockingIOError:
                Path(os.environ["CONTENDED_MARKER"]).write_text("contended")
        return real_flock(fd, flags)
    fcntl.flock = flock
real_open, real_link, real_replace, real_unlink, real_sync = os.open, os.link, os.replace, os.unlink, os.fsync
anchors = set()
pending_sync = None
def durability(point, fd):
    global pending_sync
    info = os.fstat(fd)
    pending_sync = (point, info.st_dev, info.st_ino)
def opened(name, flags, *args, **kwargs):
    private = isinstance(name, str) and name.startswith(".office-publish.") and flags & os.O_CREAT
    if private:
        stop("anchor-before")
    fd = real_open(name, flags, *args, **kwargs)
    if private:
        anchors.add(fd)
        stop("anchor-after")
    return fd
def linked(source, destination, *args, **kwargs):
    point = "marker-install" if destination == "fence.json" else "witness" if isinstance(destination, str) and destination.startswith(".publish-owner.") else "exposure" if isinstance(destination, str) and destination.startswith(".office-publish.") else None
    if point:
        stop(point + "-before")
    answer = real_link(source, destination, *args, **kwargs)
    if point:
        durability(point, kwargs["dst_dir_fd"])
        stop(point + "-after")
    return answer
def replaced(source, destination, *args, **kwargs):
    point = None
    if destination == "state.json":
        successor = json.loads((office / source).read_bytes())
        journal = successor["journal"]
        binding = journal.get("publish-save-3", {}).get("staging")
        point = "completion" if not journal else "retirement" if binding and binding.get("retired") else "binding" if binding else "preparation"
    elif destination == "report.docx":
        point = "replace"
    elif destination == "index.json":
        point = "registration"
    if point:
        stop(point + "-before")
    answer = real_replace(source, destination, *args, **kwargs)
    if point:
        durability(point, kwargs["dst_dir_fd"])
        stop(point + "-after")
    return answer
def unlinked(name, *args, **kwargs):
    point = None
    if name == "fence.json":
        point = "marker-cleanup"
    elif isinstance(name, str) and name.startswith(".publish-owner."):
        point = "witness-cleanup"
    elif isinstance(name, str) and name.startswith(".office-publish."):
        parent = os.fstat(kwargs["dir_fd"])
        private = (office / "staging").stat()
        point = "anchor-cleanup" if (parent.st_dev, parent.st_ino) == (private.st_dev, private.st_ino) else "shared-cleanup"
    if point:
        stop(point + "-before")
    answer = real_unlink(name, *args, **kwargs)
    if point:
        durability(point, kwargs["dir_fd"])
        stop(point + "-after")
    return answer
def synced(fd):
    global pending_sync
    info = os.fstat(fd)
    point = None
    if fd in anchors and info.st_size == 0:
        point = "anchor"
    elif pending_sync is not None and (info.st_dev, info.st_ino) == pending_sync[1:]:
        point = pending_sync[0]
    if point:
        stop(point + "-durability-before")
    answer = real_sync(fd)
    if point:
        stop(point + "-durability-after")
        pending_sync = None
    return answer
os.open, os.link, os.replace, os.unlink, os.fsync = opened, linked, replaced, unlinked, synced
if mode == "publish":
    publish(chat, "publish-save-3")
else:
    recover_publications(chat)
print("completed", flush=True)
'''


def _crash(fixture, cut, *, mode="publish", engine_state=None):
    marker = fixture.data.parent / "crash-marker"
    marker.unlink(missing_ok=True)
    child = subprocess.Popen(
        [sys.executable, "-c", _CRASH_WORKER],
        env=_child_env(fixture.data, CRASH_MARKER=str(marker), CRASH_CUT=cut, CRASH_MODE=mode,
                       ENGINE_STATE=str(engine_state) if engine_state else ""),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not marker.exists():
            if child.poll() is not None or time.monotonic() >= deadline:
                child.kill() if child.poll() is None else None
                out, err = child.communicate(timeout=5)
                pytest.fail(f"crash cut {cut} not reached: {out} {err}")
            time.sleep(0.01)
        assert marker.read_text() == cut
        child.kill()
        child.communicate(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)


def _fresh_recover(fixture, engine_state=None, *, wall_time=106.0):
    completed = subprocess.run(
        [sys.executable, "-c", _CRASH_WORKER],
        env=_child_env(fixture.data, CRASH_MARKER=str(fixture.data.parent / "unused-marker"),
                       CRASH_MODE="recover", ENGINE_STATE=str(engine_state) if engine_state else "",
                       WALL_TIME=str(wall_time)),
        capture_output=True, text=True, timeout=15,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert completed.stdout.strip() == "completed"


@pytest.mark.parametrize("cut", [
    f"{point}-{side}"
    for point in ("preparation", "anchor", "anchor-durability", "witness", "binding",
                  "exposure", "replace", "registration", "anchor-cleanup",
                  "witness-cleanup", "completion", "preparation-durability",
                  "witness-durability", "binding-durability", "exposure-durability",
                  "replace-durability", "registration-durability",
                  "anchor-cleanup-durability", "witness-cleanup-durability",
                  "retirement", "retirement-durability",
                  "completion-durability")
    for side in ("before", "after")
])
def test_killed_publisher_converges_in_fresh_process_at_every_owned_crash_cut(world, cut):
    fixture = _prepared(world, "absent")
    _crash(fixture, cut)
    assert fixture.target.read_bytes() in (BASELINE, SAVED)
    matching = fixture.target.read_bytes() == SAVED
    inode = fixture.target.stat().st_ino
    unbound = {}
    if cut in {"anchor-after", "anchor-durability-before", "anchor-durability-after",
               "witness-before", "witness-after", "binding-before"}:
        unbound = {key: value for key, value in _snapshot(fixture.data).items()
                   if "/office/staging/" in key}
    _fresh_recover(fixture)
    _published(fixture)
    after = _snapshot(fixture.data)
    assert all(after.get(key) == value for key, value in unbound.items())
    if matching:
        assert fixture.target.stat().st_ino == inode
    before = _snapshot(fixture.data)
    _fresh_recover(fixture)
    assert _snapshot(fixture.data) == before
    assert not list(fixture.target.parent.glob(".office-publish.*"))


@pytest.mark.parametrize("cut", (
    "shared-cleanup-before", "shared-cleanup-after",
    "anchor-cleanup-before", "anchor-cleanup-after",
    "witness-cleanup-before", "witness-cleanup-after",
    "binding-after", "completion-before",
))
def test_recovery_killed_again_retains_responsibility_and_converges(world, cut):
    fixture = _prepared(world, "absent")
    _crash(fixture, "exposure-after")
    _crash(fixture, cut, mode="recover")
    assert OBLIGATION in fixture.store.read(CHAT)["journal"]
    _fresh_recover(fixture)
    _published(fixture)


@pytest.mark.parametrize("entry", ("shared", "anchor", "witness"))
@pytest.mark.parametrize("kind", ("regular", "symlink"))
def test_fresh_recovery_preserves_substituted_private_or_shared_entries(world, entry, kind):
    fixture = _prepared(world, "absent")
    _crash(fixture, "exposure-after")
    obligation = fixture.store.read(CHAT)["journal"][OBLIGATION]
    directory = fixture.target.parent if entry == "shared" else fixture.data / CHAT / ".ocu" / "office" / "staging"
    name = obligation["temporary_name"] if entry == "shared" else obligation["staging"][f"{entry}_name"]
    foreign = directory / name
    foreign.unlink()
    outside = fixture.data.parent / "foreign"
    outside.write_bytes(b"foreign-content")
    if kind == "symlink":
        foreign.symlink_to(outside)
    else:
        foreign.write_bytes(b"foreign-content")
    from office.publish import RecoveryRequiredError
    before = _snapshot(fixture.data)
    with pytest.raises(RecoveryRequiredError):
        _recover()
    assert _snapshot(fixture.data) == before
    assert outside.read_bytes() == b"foreign-content"
    assert foreign.is_symlink() if kind == "symlink" else foreign.read_bytes() == b"foreign-content"
    assert fixture.target.read_bytes() == BASELINE


def test_missing_owned_shared_temporary_is_normal_and_recovery_converges(world):
    fixture = _prepared(world, "absent")
    _crash(fixture, "exposure-after")
    entry = fixture.store.read(CHAT)["journal"][OBLIGATION]
    (fixture.target.parent / entry["temporary_name"]).unlink()
    _fresh_recover(fixture)
    _published(fixture)


def test_startup_recovers_office_only_chat_before_first_poll(tmp_path, monkeypatch):
    import asyncio
    from tests.orchestrator.test_outputs_endpoint import _isolated_app
    monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", "http://documentserver.invalid")
    with _isolated_app(tmp_path) as (app, manager, _broker, data):
        import office.store as store_mod
        fixture = _prepared((store_mod, manager, data), "absent")
        async def startup():
            stopped = asyncio.Event()
            stopped.set()
            await app._idle_reaper(stopped)
        asyncio.run(startup())
        _published(fixture)
        fixture.engine.containers.create.assert_not_called()


def test_marker_substitution_during_release_preserves_foreign_marker(world):
    from office.publish import RecoveryRequiredError, recover_publications
    fixture = _prepared(world)
    fixture.container.status = "paused"
    fixture.container.reload()
    _engine_by_identity(fixture, fixture.container)
    marker = _marker(fixture, {
        "schema_version": 1, "container_id": fixture.container.id, "pause_started_at": 100,
    })
    unpause = fixture.container.unpause.side_effect
    def release():
        unpause()
        marker.unlink()
        marker.write_bytes(b"foreign-marker")
    fixture.container.unpause.side_effect = release
    with pytest.raises(RecoveryRequiredError):
        recover_publications(CHAT, now=106)
    assert marker.read_bytes() == b"foreign-marker"
    assert fixture.target.read_bytes() == BASELINE
    assert OBLIGATION in fixture.store.read(CHAT)["journal"]


def test_fresh_worker_cannot_recover_while_live_publisher_holds_canonical_lock(world):
    fixture = _prepared(world, "absent")
    entered = fixture.data.parent / "publisher-entered"
    contended = fixture.data.parent / "recovery-contended"
    first = second = None
    try:
        first = subprocess.Popen(
            [sys.executable, "-c", _CRASH_WORKER],
            env=_child_env(fixture.data, CRASH_MARKER=str(entered), CRASH_CUT="replace-before"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        _wait_marker(entered, first, "publisher")
        second = subprocess.Popen(
            [sys.executable, "-c", _CRASH_WORKER],
            env=_child_env(fixture.data, CRASH_MARKER=str(fixture.data.parent / "unused-marker"),
                           CRASH_MODE="recover", CONTENDED_MARKER=str(contended)),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        _wait_marker(contended, second, "recovery lock contention")
        assert second.poll() is None
        assert fixture.target.read_bytes() == BASELINE
        state_path = fixture.data / CHAT / ".ocu" / "office" / "state.json"
        assert OBLIGATION in json.loads(state_path.read_bytes())["journal"]
        first.kill()
        first.communicate(timeout=5)
        out, err = second.communicate(timeout=15)
        assert second.returncode == 0, (out, err)
        assert out.strip() == "completed"
        _published(fixture)
    finally:
        _stop_child(first)
        _stop_child(second)


@pytest.mark.parametrize("kind", ("matching-content", "symlink"))
def test_valid_legacy_journal_name_never_authorizes_foreign_temporary_cleanup(world, kind):
    from office.publish import RecoveryRequiredError
    fixture = _prepared(world, "absent")
    temporary = ".office-publish." + "a" * 32 + ".tmp"
    def prepare(state):
        state["journal"][OBLIGATION].update(target_path="report.docx", temporary_name=temporary)
    fixture.store.update(CHAT, prepare)
    foreign = fixture.target.parent / temporary
    outside = fixture.data.parent / "foreign-legacy"
    outside.write_bytes(SAVED)
    if kind == "symlink":
        foreign.symlink_to(outside)
    else:
        foreign.write_bytes(SAVED)
    before = _snapshot(fixture.data)
    with pytest.raises(RecoveryRequiredError):
        _recover()
    assert _snapshot(fixture.data) == before
    assert outside.read_bytes() == SAVED
    assert fixture.target.read_bytes() == BASELINE


def test_uncertain_binding_commit_is_reread_and_recovery_keeps_visible_successor(world, monkeypatch):
    from office.publish import publish
    fixture = _prepared(world, "absent")
    office = fixture.data / CHAT / ".ocu" / "office"
    identity = office.stat()
    real_replace, real_sync = os.replace, os.fsync
    bound, failed = False, False
    def replace(source, destination, *args, **kwargs):
        nonlocal bound
        if destination == "state.json":
            successor = json.loads((office / source).read_bytes())
            bound = "staging" in successor["journal"].get(OBLIGATION, {})
        return real_replace(source, destination, *args, **kwargs)
    def sync(fd):
        nonlocal failed
        info = os.fstat(fd)
        if bound and not failed and (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
            failed = True
            raise OSError(errno.EIO, "binding durability failed")
        return real_sync(fd)
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", replace)
        patch.setattr(os, "fsync", sync)
        with pytest.raises(world[0].StateDurabilityError):
            publish(CHAT, OBLIGATION)
    assert failed
    entry = fixture.store.read(CHAT)["journal"][OBLIGATION]
    assert entry["staging"]["schema_version"] == 1
    assert fixture.target.read_bytes() == BASELINE
    _fresh_recover(fixture)
    _published(fixture)


@pytest.mark.parametrize("point", (
    "marker-install", "marker-install-durability", "engine-pause",
    "engine-unpause", "marker-cleanup", "marker-cleanup-durability",
))
@pytest.mark.parametrize("side", ("before", "after"))
def test_killed_fenced_publisher_releases_original_engine_and_converges_fresh(world, point, side):
    fixture = _prepared(world, "absent")
    engine = fixture.data.parent / "engine-state.json"
    engine.write_text(json.dumps({"status": "running", "pauses": 0, "unpauses": 0}))
    _crash(fixture, f"{point}-{side}", engine_state=engine)
    matching = fixture.target.read_bytes() == SAVED
    inode = fixture.target.stat().st_ino
    _fresh_recover(fixture, engine_state=engine)
    _published(fixture)
    if matching:
        assert fixture.target.stat().st_ino == inode
    assert json.loads(engine.read_bytes())["status"] == "running"
    assert not (fixture.data / CHAT / ".ocu" / "office" / "fence.json").exists()
    before = _snapshot(fixture.data), engine.read_bytes()
    _fresh_recover(fixture, engine_state=engine)
    assert (_snapshot(fixture.data), engine.read_bytes()) == before


def test_retired_private_names_are_never_re_adopted_even_if_inode_numbers_match(world):
    fixture = _prepared(world, "absent")
    _crash(fixture, "retirement-after")
    entry = fixture.store.read(CHAT)["journal"][OBLIGATION]
    assert entry["staging"]["retired"] is True
    staging = fixture.data / CHAT / ".ocu" / "office" / "staging"
    anchor = staging / entry["staging"]["anchor_name"]
    witness = staging / entry["staging"]["witness_name"]
    anchor.unlink()
    witness.unlink()
    anchor.write_bytes(b"foreign-retired-entry")
    os.link(anchor, witness)
    foreign = anchor.stat()
    def reused(state):
        state["journal"][OBLIGATION]["staging"].update(device=foreign.st_dev, inode=foreign.st_ino)
    fixture.store.update(CHAT, reused)
    inode = fixture.target.stat().st_ino
    _fresh_recover(fixture)
    _published(fixture)
    assert anchor.read_bytes() == b"foreign-retired-entry"
    assert witness.read_bytes() == b"foreign-retired-entry"
    assert fixture.target.stat().st_ino == inode


@pytest.mark.parametrize("cut", ("marker-install-after", "engine-unpause-after", "marker-cleanup-after"))
def test_fenced_recovery_killed_again_converges_after_the_new_marker_ages(world, cut):
    fixture = _prepared(world, "absent")
    engine = fixture.data.parent / "engine-state.json"
    engine.write_text(json.dumps({"status": "running", "pauses": 0, "unpauses": 0}))
    _crash(fixture, "engine-pause-after", engine_state=engine)
    _crash(fixture, cut, mode="recover", engine_state=engine)
    assert OBLIGATION in fixture.store.read(CHAT)["journal"]
    _fresh_recover(fixture, engine_state=engine, wall_time=112.0)
    _published(fixture)
    assert json.loads(engine.read_bytes())["status"] == "running"
    assert not (fixture.data / CHAT / ".ocu" / "office" / "fence.json").exists()


def _third_obligation(fixture):
    third = b"third-save-v3"
    fixture.store.store_version(CHAT, fixture.file_id, third, source="save", parent=2,
                               published=False, min_free_bytes=0)
    def seed(state):
        state["journal"]["a-newer"] = {
            "file_id": fixture.file_id, "version": 3, "session_id": SESSION,
            "save_seq": 4, "requester": "save",
        }
        state["sessions"][SESSION]["save_intents"]["4"] = "publish"
        state["sessions"][SESSION]["last_committed_seq"] = 4
        state["receipts"][SESSION]["4"] = {
            "status": 6, "version": 3, "sha256": _sha(third), "answer": {"error": 0},
        }
    fixture.store.update(CHAT, seed)
    return third


def test_requested_older_obligation_returns_its_conflict_not_later_recovery_success(world):
    from office.publish import publish
    fixture = _prepared(world, "absent")
    third = _third_obligation(fixture)
    fixture.target.write_bytes(third)
    result = publish(CHAT, OBLIGATION)
    assert (result.outcome, result.reason) == ("conflict", "baseline_mismatch")
    state = fixture.store.read(CHAT)
    assert state["journal"] == {}
    assert state["documents"][fixture.file_id]["published_version"] == 3
    assert state["documents"][fixture.file_id]["versions"][1]["published"] is False
    assert fixture.target.read_bytes() == b"third-save-v3"


def test_interrupted_dependency_prevents_requested_newer_publication(world, monkeypatch):
    from office.publish import RecoveryRequiredError, publish
    from tests.orchestrator.test_office_publish_fence import _running
    fixture = _running(world)
    _third_obligation(fixture)
    clock = {"now": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    real_replace = os.replace
    def replace(source, destination, *args, **kwargs):
        answer = real_replace(source, destination, *args, **kwargs)
        if destination == "report.docx":
            clock["now"] = 5.0
        return answer
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", replace)
        with pytest.raises(RecoveryRequiredError):
            publish(CHAT, "a-newer")
    assert fixture.target.read_bytes() == SAVED
    state = fixture.store.read(CHAT)
    assert set(state["journal"]) == {OBLIGATION, "a-newer"}
    assert state["documents"][fixture.file_id]["published_version"] == 1
    assert state["documents"][fixture.file_id]["versions"][1]["published"] is False
    assert fixture.engine_state["status"] == "running"


def test_shared_substitution_during_cleanup_preserves_foreign_entry_and_obligation(world, monkeypatch):
    from office.publish import RecoveryRequiredError
    fixture = _prepared(world, "absent")
    _crash(fixture, "exposure-after")
    entry = fixture.store.read(CHAT)["journal"][OBLIGATION]
    foreign = fixture.target.parent / entry["temporary_name"]
    parent = fixture.target.parent.stat()
    real_lstat = os.lstat
    substituted = False
    def lstat(name, *args, **kwargs):
        nonlocal substituted
        info = real_lstat(name, *args, **kwargs)
        fd = kwargs.get("dir_fd")
        if not substituted and name == entry["temporary_name"] and fd is not None:
            directory = os.fstat(fd)
            if (directory.st_dev, directory.st_ino) == (parent.st_dev, parent.st_ino):
                substituted = True
                foreign.unlink()
                foreign.write_bytes(b"foreign-during-cleanup")
        return info
    before = fixture.store.read(CHAT)
    with monkeypatch.context() as patch:
        patch.setattr(os, "lstat", lstat)
        with pytest.raises(RecoveryRequiredError):
            _recover()
    assert substituted
    assert foreign.read_bytes() == b"foreign-during-cleanup"
    assert fixture.store.read(CHAT) == before
    assert fixture.target.read_bytes() == BASELINE


def test_uncertain_retirement_commit_preserves_private_names_without_readopting_them(world, monkeypatch):
    from office.publish import publish
    fixture = _prepared(world, "absent")
    office = fixture.data / CHAT / ".ocu" / "office"
    identity = office.stat()
    real_replace, real_sync = os.replace, os.fsync
    retiring, failed = False, False
    def replace(source, destination, *args, **kwargs):
        nonlocal retiring
        if destination == "state.json":
            successor = json.loads((office / source).read_bytes())
            retiring = successor["journal"].get(OBLIGATION, {}).get("staging", {}).get("retired") is True
        return real_replace(source, destination, *args, **kwargs)
    def sync(fd):
        nonlocal failed
        info = os.fstat(fd)
        if retiring and not failed and (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
            failed = True
            raise OSError(errno.EIO, "retirement durability failed")
        return real_sync(fd)
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", replace)
        patch.setattr(os, "fsync", sync)
        with pytest.raises(world[0].StateDurabilityError):
            publish(CHAT, OBLIGATION)
    assert failed
    entry = fixture.store.read(CHAT)["journal"][OBLIGATION]
    assert entry["staging"]["retired"] is True
    private = {name: (office / "staging" / name).read_bytes()
               for name in (entry["staging"]["anchor_name"], entry["staging"]["witness_name"])}
    inode = fixture.target.stat().st_ino
    _fresh_recover(fixture)
    _published(fixture)
    assert fixture.target.stat().st_ino == inode
    assert {name: (office / "staging" / name).read_bytes() for name in private} == private


def test_private_substitution_during_retirement_remains_refused_on_next_recovery(world, monkeypatch):
    from office.publish import RecoveryRequiredError, publish
    fixture = _prepared(world, "absent")
    office = fixture.data / CHAT / ".ocu" / "office"
    real_replace = os.replace
    foreign = []
    def replace(source, destination, *args, **kwargs):
        retiring = None
        if destination == "state.json":
            successor = json.loads((office / source).read_bytes())
            binding = successor["journal"].get(OBLIGATION, {}).get("staging")
            if binding and binding["retired"]:
                retiring = binding
        answer = real_replace(source, destination, *args, **kwargs)
        if retiring and not foreign:
            anchor = office / "staging" / retiring["anchor_name"]
            anchor.unlink()
            anchor.write_bytes(b"foreign-private-retirement")
            foreign.append(anchor)
        return answer
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", replace)
        with pytest.raises(RecoveryRequiredError):
            publish(CHAT, OBLIGATION)
    assert foreign[0].read_bytes() == b"foreign-private-retirement"
    assert OBLIGATION in fixture.store.read(CHAT)["journal"]
    before = _snapshot(fixture.data)
    with pytest.raises(RecoveryRequiredError):
        _recover()
    assert _snapshot(fixture.data) == before
    assert foreign[0].read_bytes() == b"foreign-private-retirement"


def _resume_running_writer(fixture):
    container = fixture.container
    container.status = "running"
    container.reload()
    reload = container.reload.side_effect
    unpause = container.unpause.side_effect
    events = []
    def observe():
        reload()
        events.append(f"observed-{container.status}")
    def pause():
        events.append("pause")
        container.status = "paused"
    def release():
        unpause()
        events.append("unpause")
    container.reload.side_effect = observe
    container.pause.side_effect = pause
    container.unpause.side_effect = release
    _engine_by_identity(fixture, container)
    return events


def test_prepared_recovery_quiesces_writer_before_shared_cleanup_and_reuses_one_fence(
    world, monkeypatch,
):
    fixture = _prepared(world, "absent")
    _crash(fixture, "exposure-after")
    events = _resume_running_writer(fixture)
    entry = fixture.store.read(CHAT)["journal"][OBLIGATION]
    shared = fixture.target.parent / entry["temporary_name"]
    displaced = fixture.target.parent / "displaced-owned-inode"
    parent = fixture.target.parent.stat()
    real_lstat, real_unlink = os.lstat, os.unlink
    visits, interleavings = [], []
    inspections = 0
    def shared_name(name, kwargs):
        fd = kwargs.get("dir_fd")
        if name != entry["temporary_name"] or fd is None:
            return False
        directory = os.fstat(fd)
        return (directory.st_dev, directory.st_ino) == (parent.st_dev, parent.st_ino)
    def lstat(name, *args, **kwargs):
        nonlocal inspections
        matched = shared_name(name, kwargs)
        if matched:
            inspections += 1
            visits.append(("inspect", fixture.container.status, "observed-paused" in events))
        info = real_lstat(name, *args, **kwargs)
        if matched and inspections == 2 and fixture.container.status == "running":
            shared.rename(displaced)
            shared.write_bytes(b"foreign-running-writer")
            interleavings.append("writer-replaced-owned-name")
        return info
    def unlink(name, *args, **kwargs):
        if shared_name(name, kwargs):
            visits.append(("unlink", fixture.container.status, "observed-paused" in events))
        return real_unlink(name, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(os, "lstat", lstat)
        patch.setattr(os, "unlink", unlink)
        _recover()
    assert not interleavings or shared.exists() and shared.read_bytes() == b"foreign-running-writer"
    assert visits
    assert all(status == "paused" and observed for _operation, status, observed in visits)
    assert any(operation == "unlink" for operation, _status, _observed in visits)
    assert events.index("pause") < events.index("observed-paused")
    assert fixture.container.pause.call_count == 1
    assert fixture.container.unpause.call_count == 1
    assert fixture.container.status == "running"
    _published(fixture)


def test_running_recovery_fences_inspection_and_preserves_preexisting_foreign_temporary(
    world, monkeypatch,
):
    from office.publish import RecoveryRequiredError
    fixture = _prepared(world, "absent")
    _crash(fixture, "exposure-after")
    events = _resume_running_writer(fixture)
    entry = fixture.store.read(CHAT)["journal"][OBLIGATION]
    shared = fixture.target.parent / entry["temporary_name"]
    shared.unlink()
    shared.write_bytes(b"foreign-before-recovery")
    parent = fixture.target.parent.stat()
    real_lstat = os.lstat
    visits = []
    def lstat(name, *args, **kwargs):
        fd = kwargs.get("dir_fd")
        if name == entry["temporary_name"] and fd is not None:
            directory = os.fstat(fd)
            if (directory.st_dev, directory.st_ino) == (parent.st_dev, parent.st_ino):
                visits.append((fixture.container.status, "observed-paused" in events))
        return real_lstat(name, *args, **kwargs)
    before = _snapshot(fixture.data)
    with monkeypatch.context() as patch:
        patch.setattr(os, "lstat", lstat)
        with pytest.raises(RecoveryRequiredError):
            _recover()
    assert visits
    assert all(status == "paused" and observed for status, observed in visits)
    assert _snapshot(fixture.data) == before
    assert shared.read_bytes() == b"foreign-before-recovery"
    assert fixture.target.read_bytes() == BASELINE
    assert OBLIGATION in fixture.store.read(CHAT)["journal"]
    assert fixture.container.status == "running"
    assert fixture.container.pause.call_count == 1
    assert fixture.container.unpause.call_count == 1


@pytest.mark.parametrize("failure", ("refused", "uncertain"))
def test_failed_recovery_pause_never_inspects_or_removes_surviving_shared_temporary(
    world, monkeypatch, failure,
):
    from office.publish import RecoveryRequiredError
    fixture = _prepared(world, "absent")
    _crash(fixture, "exposure-after")
    _resume_running_writer(fixture)
    entry = fixture.store.read(CHAT)["journal"][OBLIGATION]
    shared = fixture.target.parent / entry["temporary_name"]
    parent = fixture.target.parent.stat()
    staging = fixture.data / CHAT / ".ocu" / "office" / "staging"
    private = {name: (staging / name).read_bytes()
               for name in (entry["staging"]["anchor_name"], entry["staging"]["witness_name"])}
    pause_requested = False
    observe = fixture.container.reload.side_effect
    def pause():
        nonlocal pause_requested
        pause_requested = True
        if failure == "refused":
            raise OSError(errno.EIO, "engine refused pause")
        fixture.container.status = "paused"
    def reload():
        observe()
        if pause_requested and failure == "uncertain":
            fixture.container.attrs["State"]["Paused"] = None
    fixture.container.pause.side_effect = pause
    fixture.container.reload.side_effect = reload
    real_lstat, real_unlink = os.lstat, os.unlink
    visits = []
    def record(name, kwargs, operation):
        fd = kwargs.get("dir_fd")
        if name == entry["temporary_name"] and fd is not None:
            directory = os.fstat(fd)
            if (directory.st_dev, directory.st_ino) == (parent.st_dev, parent.st_ino):
                visits.append(operation)
    def lstat(name, *args, **kwargs):
        record(name, kwargs, "inspect")
        return real_lstat(name, *args, **kwargs)
    def unlink(name, *args, **kwargs):
        record(name, kwargs, "unlink")
        return real_unlink(name, *args, **kwargs)
    error = None
    with monkeypatch.context() as patch:
        patch.setattr(os, "lstat", lstat)
        patch.setattr(os, "unlink", unlink)
        try:
            _recover()
        except RecoveryRequiredError as exc:
            error = exc
    assert visits == []
    assert shared.exists()
    assert shared.read_bytes() == SAVED
    assert fixture.target.read_bytes() == BASELINE
    assert fixture.store.read(CHAT)["journal"][OBLIGATION] == entry
    assert {name: (staging / name).read_bytes() for name in private} == private
    assert fixture.container.pause.call_count == 1
    assert isinstance(error, RecoveryRequiredError)


@pytest.mark.parametrize("cut", ("preparation-after", "binding-after"))
def test_verified_missing_prepared_parent_completes_path_missing_without_recreation(world, cut):
    fixture = _prepared(world, "absent", path="nested/report.docx")
    _crash(fixture, cut)
    fixture.target.unlink()
    fixture.target.parent.rmdir()
    assert not fixture.target.parent.exists()
    _recover()
    after = fixture.store.read(CHAT)
    expected = copy.deepcopy(fixture.before)
    expected["sessions"][SESSION].update(state="conflict", reason="path_missing")
    del expected["journal"][OBLIGATION]
    assert after == expected
    assert not fixture.target.parent.exists()
    assert not fixture.target.exists()
    assert fixture.broker.current_revision(CHAT) == fixture.indexed["revision"]
    blob = fixture.data / CHAT / ".ocu" / "office" / "versions" / _sha(SAVED)
    assert blob.read_bytes() == SAVED
    assert after["documents"][fixture.file_id]["versions"][1]["published"] is False
    before = _snapshot(fixture.data)
    _recover()
    assert _snapshot(fixture.data) == before


@pytest.mark.parametrize("damage", ("symlink", "non-directory", "permission", "uncertain-enoent"))
def test_unsafe_prepared_parent_is_not_treated_as_verified_temporary_absence(
    world, monkeypatch, damage,
):
    from office.publish import RecoveryRequiredError
    from office.workspace import UnsafePathError
    fixture = _prepared(world, "absent", path="nested/report.docx")
    _crash(fixture, "preparation-after")
    outside = fixture.data.parent / "foreign-prepared-parent"
    outside.mkdir()
    outside_file = outside / "report.docx"
    outside_file.write_bytes(b"foreign-parent-target")
    if damage in {"symlink", "non-directory"}:
        fixture.target.unlink()
        fixture.target.parent.rmdir()
        if damage == "symlink":
            fixture.target.parent.symlink_to(outside, target_is_directory=True)
        else:
            fixture.target.parent.write_bytes(b"foreign-parent-entry")
    real_open = os.open
    outputs_identity = fixture.target.parent.parent.stat()
    def opened(path, flags, *args, **kwargs):
        if path == "nested" and kwargs.get("dir_fd") is not None:
            directory = os.fstat(kwargs["dir_fd"])
            if (directory.st_dev, directory.st_ino) != (outputs_identity.st_dev, outputs_identity.st_ino):
                return real_open(path, flags, *args, **kwargs)
            if damage == "permission":
                raise PermissionError(errno.EACCES, "prepared parent permission is uncertain")
            if damage == "uncertain-enoent":
                raise FileNotFoundError(errno.ENOENT, "parent open failed despite surviving directory")
        return real_open(path, flags, *args, **kwargs)
    before = _snapshot(fixture.data)
    with monkeypatch.context() as patch:
        patch.setattr(os, "open", opened)
        with pytest.raises((OSError, RecoveryRequiredError, world[0].StateCorruptError, UnsafePathError)):
            _recover()
    assert _snapshot(fixture.data) == before
    assert OBLIGATION in fixture.store.read(CHAT)["journal"]
    assert outside_file.read_bytes() == b"foreign-parent-target"
    assert fixture.broker.current_revision(CHAT) == fixture.indexed["revision"]


def test_missing_prepared_parent_does_not_authorize_foreign_private_cleanup(world):
    from office.publish import RecoveryRequiredError
    fixture = _prepared(world, "absent", path="nested/report.docx")
    _crash(fixture, "binding-after")
    fixture.target.unlink()
    fixture.target.parent.rmdir()
    entry = fixture.store.read(CHAT)["journal"][OBLIGATION]
    anchor = fixture.data / CHAT / ".ocu" / "office" / "staging" / entry["staging"]["anchor_name"]
    anchor.unlink()
    anchor.write_bytes(b"foreign-private-missing-parent")
    before = _snapshot(fixture.data)
    with pytest.raises((OSError, RecoveryRequiredError, world[0].StateCorruptError)):
        _recover()
    assert _snapshot(fixture.data) == before
    assert anchor.read_bytes() == b"foreign-private-missing-parent"
    assert OBLIGATION in fixture.store.read(CHAT)["journal"]
    assert not fixture.target.parent.exists()


def test_matching_prepared_recovery_keeps_one_fence_through_cleanup_and_registration(
    world, monkeypatch,
):
    fixture = _prepared(world, "absent")
    _crash(fixture, "exposure-after")
    fixture.target.write_bytes(SAVED)
    inode = fixture.target.stat().st_ino
    events = _resume_running_writer(fixture)
    real_replace = os.replace
    registrations = []
    def replace(source, destination, *args, **kwargs):
        if destination == "index.json":
            registrations.append((fixture.container.status, "observed-paused" in events))
        return real_replace(source, destination, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", replace)
        _recover()
    assert registrations == [("paused", True)]
    assert fixture.target.stat().st_ino == inode
    assert fixture.container.pause.call_count == 1
    assert fixture.container.unpause.call_count == 1
    assert fixture.container.status == "running"
    _published(fixture)


def test_requested_publication_result_survives_refused_release_before_newer_obligation(
    world, monkeypatch,
):
    from office.publish import publish, recover_publications
    from tests.orchestrator.test_office_publish_fence import _running
    fixture = _running(world)
    third = _third_obligation(fixture)
    monkeypatch.setattr(time, "time", lambda: 100.0)
    monkeypatch.setattr(time, "monotonic", lambda: 0.0)
    def pause():
        fixture.engine_state["status"] = "paused"
    fixture.container.pause.side_effect = pause
    release = fixture.container.unpause.side_effect
    def refused_release():
        raise OSError(errno.EIO, "requested publication release refused")
    fixture.container.unpause.side_effect = refused_release
    _engine_by_identity(fixture, fixture.container)
    before = fixture.store.read(CHAT)

    result = publish(CHAT, OBLIGATION)

    assert (result.outcome, result.reason) == ("published", None)
    state = fixture.store.read(CHAT)
    assert set(state["journal"]) == {"a-newer"}
    assert state["journal"]["a-newer"] == before["journal"]["a-newer"]
    assert fixture.target.read_bytes() == SAVED
    assert state["documents"][fixture.file_id]["published_version"] == 2
    assert state["documents"][fixture.file_id]["published_sha256"] == _sha(SAVED)
    assert [item["published"] for item in state["documents"][fixture.file_id]["versions"]] == [True, True, False]
    assert state["sessions"][SESSION]["baseline_sha256"] == _sha(SAVED)
    assert state["receipts"] == before["receipts"]
    assert fixture.broker.current_revision(CHAT) == fixture.indexed["revision"] + 1
    assert fixture.marker.exists()
    assert json.loads(fixture.marker.read_bytes())["container_id"] == fixture.original_id
    assert fixture.engine_state["status"] == "paused"

    fixture.container.unpause.side_effect = release
    recover_publications(CHAT, now=106.0)
    after = fixture.store.read(CHAT)
    assert after["journal"] == {}
    assert after["documents"][fixture.file_id]["published_version"] == 3
    assert after["documents"][fixture.file_id]["published_sha256"] == _sha(third)
    assert [item["published"] for item in after["documents"][fixture.file_id]["versions"]] == [True, True, True]
    assert fixture.target.read_bytes() == b"third-save-v3"
    assert after["receipts"] == before["receipts"]
    assert fixture.broker.current_revision(CHAT) == fixture.indexed["revision"] + 2
    assert fixture.engine_state["status"] == "running"
    assert not fixture.marker.exists()
    completed = _snapshot(fixture.data), list(fixture.engine.mock_calls)
    recover_publications(CHAT, now=112.0)
    assert (_snapshot(fixture.data), fixture.engine.mock_calls) == completed


def test_requested_publication_result_survives_newer_successor_timeout(
    world, monkeypatch,
):
    from office.publish import publish, recover_publications
    from tests.orchestrator.test_office_publish_fence import _running
    fixture = _running(world)
    third = _third_obligation(fixture)
    def pause():
        fixture.engine_state["status"] = "paused"
    fixture.container.pause.side_effect = pause
    clock = {"now": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    real_replace = os.replace
    replacements = []
    def replace(source, destination, *args, **kwargs):
        answer = real_replace(source, destination, *args, **kwargs)
        if destination == "report.docx":
            replacements.append(fixture.target.read_bytes())
            if len(replacements) == 2:
                clock["now"] = 5.0
        return answer
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", replace)
        result = publish(CHAT, OBLIGATION)

    assert (result.outcome, result.reason) == ("published", None)
    assert replacements == [SAVED, third]
    state = fixture.store.read(CHAT)
    assert set(state["journal"]) == {"a-newer"}
    assert state["journal"]["a-newer"]["target_path"] == "report.docx"
    assert fixture.target.read_bytes() == b"third-save-v3"
    assert state["documents"][fixture.file_id]["published_version"] == 2
    assert [item["published"] for item in state["documents"][fixture.file_id]["versions"]] == [True, True, False]
    assert state["sessions"][SESSION]["baseline_sha256"] == _sha(SAVED)
    assert fixture.broker.current_revision(CHAT) == fixture.indexed["revision"] + 1
    assert fixture.engine_state["status"] == "running"
    assert not fixture.marker.exists()
    inode = fixture.target.stat().st_ino

    clock["now"] = 0.0
    recover_publications(CHAT)
    after = fixture.store.read(CHAT)
    assert after["journal"] == {}
    assert after["documents"][fixture.file_id]["published_version"] == 3
    assert after["documents"][fixture.file_id]["published_sha256"] == _sha(third)
    assert [item["published"] for item in after["documents"][fixture.file_id]["versions"]] == [True, True, True]
    assert fixture.target.stat().st_ino == inode
    assert fixture.target.read_bytes() == b"third-save-v3"
    assert fixture.broker.current_revision(CHAT) == fixture.indexed["revision"] + 2
    completed = _snapshot(fixture.data), list(fixture.engine.mock_calls)
    recover_publications(CHAT)
    assert (_snapshot(fixture.data), fixture.engine.mock_calls) == completed


def test_requested_result_survives_post_successor_owned_fence_check(world, monkeypatch):
    from office.publish import publish, recover_publications
    from tests.orchestrator.test_office_publish_fence import _running
    fixture = _running(world)
    third = _third_obligation(fixture)
    monkeypatch.setattr(time, "time", lambda: 100.0)
    monkeypatch.setattr(time, "monotonic", lambda: 0.0)
    def pause():
        fixture.engine_state["status"] = "paused"
    fixture.container.pause.side_effect = pause
    original_release = fixture.container.unpause.side_effect
    releases = 0
    def release():
        nonlocal releases
        releases += 1
        if releases == 2:
            raise OSError(errno.EIO, "successor release refused")
        original_release()
    fixture.container.unpause.side_effect = release
    _engine_by_identity(fixture, fixture.container)

    result = publish(CHAT, OBLIGATION)

    assert (result.outcome, result.reason) == ("published", None)
    state = fixture.store.read(CHAT)
    assert state["journal"] == {}
    assert state["documents"][fixture.file_id]["published_version"] == 3
    assert state["documents"][fixture.file_id]["published_sha256"] == _sha(third)
    assert fixture.target.read_bytes() == b"third-save-v3"
    assert fixture.broker.current_revision(CHAT) == fixture.indexed["revision"] + 2
    assert fixture.marker.exists()
    assert json.loads(fixture.marker.read_bytes())["container_id"] == fixture.original_id
    assert fixture.engine_state["status"] == "paused"
    recover_publications(CHAT, now=106.0)
    assert not fixture.marker.exists()
    assert fixture.engine_state["status"] == "running"
    assert fixture.store.read(CHAT) == state
    assert fixture.broker.current_revision(CHAT) == fixture.indexed["revision"] + 2


@pytest.mark.parametrize("failure", ("programming", "io"))
def test_unexpected_successor_failure_still_propagates_after_requested_completion(
    world, monkeypatch, failure,
):
    from office.publish import publish
    fixture = _prepared(world, "absent")
    _third_obligation(fixture)
    original = os.replace
    replacements = 0
    error = RuntimeError("successor programming failure") if failure == "programming" else OSError(
        errno.EIO, "successor workspace IO failure",
    )
    def replace(source, destination, *args, **kwargs):
        nonlocal replacements
        if destination == "report.docx":
            replacements += 1
            if replacements == 2:
                raise error
        return original(source, destination, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", replace)
        with pytest.raises(type(error)) as caught:
            publish(CHAT, OBLIGATION)
    assert caught.value is error
    assert replacements == 2
    state = fixture.store.read(CHAT)
    assert set(state["journal"]) == {"a-newer"}
    assert state["documents"][fixture.file_id]["published_version"] == 2
    assert [item["published"] for item in state["documents"][fixture.file_id]["versions"]] == [True, True, False]
    assert fixture.target.read_bytes() == SAVED
    assert fixture.broker.current_revision(CHAT) == fixture.indexed["revision"] + 1
