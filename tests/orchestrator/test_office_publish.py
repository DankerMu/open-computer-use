# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Publication consumes persisted obligations through the standalone Office seam."""
from __future__ import annotations

import copy
import hashlib
import json
import importlib
import errno
import os
import stat
import threading
import sys
from types import SimpleNamespace

import pytest

from tests.orchestrator._office_store import CHAT, _outputs
from tests.orchestrator.test_lifecycle import _container, _docker

SESSION = "publish-session"
OBLIGATION = "publish-save-3"
BASELINE = b"workspace-v1"
SAVED = b"published-v2"


def _sha(body):
    return hashlib.sha256(body).hexdigest()


@pytest.fixture(autouse=True)
def _reload_publish(world):
    if "office.publish" in sys.modules:
        importlib.reload(sys.modules["office.publish"])


def _prepared(world, sandbox_state="exited", path="report.docx"):
    store_mod, docker_manager, data = world
    import outputs_broker

    outputs = _outputs(data)
    outputs.mkdir(parents=True)
    target = outputs / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(BASELINE)
    broker = outputs_broker.OutputsBroker()
    indexed = broker.reconcile(CHAT)
    file_id = indexed["entries"][0]["file_id"]
    store = store_mod.OfficeStore()
    store.store_version(CHAT, file_id, BASELINE, source="workspace", parent=None,
                        published=True, min_free_bytes=0)
    store.store_version(CHAT, file_id, SAVED, source="save", parent=1,
                        published=False, min_free_bytes=0)

    def seed(state):
        state["documents"][file_id].update({
            "file_id": file_id, "type": "docx", "path": path,
            "published_version": 1, "published_sha256": _sha(BASELINE),
        })
        state["sessions"][SESSION] = {
            "session_id": SESSION, "file_id": file_id, "document_key": "publish-key",
            "baseline_sha256": _sha(BASELINE), "restore_epoch": "epoch-one",
            "state": "saving", "reason": None, "save_seq": 4,
            "last_committed_seq": 3, "last_published_seq": 1,
            "pending_save_seq": 4, "save_intents": {"3": "publish", "4": "persist"},
            "workspace_changed": False, "saved_as": None, "last_activity_at": 123,
        }
        # Deliberately inert sibling records probe collection conservation only.
        state["sessions"]["sibling-session"] = {"state": "closing", "save_seq": 8}
        state["receipts"][SESSION] = {"3": {"status": 6, "version": 2,
            "sha256": _sha(SAVED), "answer": {"error": 0}}}
        state["journal"][OBLIGATION] = {
            "file_id": file_id, "version": 2, "session_id": SESSION,
            "save_seq": 3, "requester": "save",
        }
        state["journal"]["sibling-obligation"] = {"untouched": True}

    before = store.update(CHAT, seed)
    container = _container(docker_manager._container_name(CHAT), status="exited")
    engine = _docker([] if sandbox_state == "absent" else [container])
    docker_manager._docker_client = engine

    return SimpleNamespace(store=store, broker=broker, target=target, file_id=file_id,
                           before=before, indexed=indexed, engine=engine,
                           container=container, data=data, manager=docker_manager)


@pytest.mark.parametrize("sandbox_state", ["absent", "exited"])
def test_stopped_publish_commits_workspace_index_and_office_together(world, sandbox_state):
    fixture = _prepared(world, sandbox_state)
    store, broker, target = fixture.store, fixture.broker, fixture.target
    before, indexed, file_id = fixture.before, fixture.indexed, fixture.file_id
    container, engine, data = fixture.container, fixture.engine, fixture.data
    from office.publish import publish_stopped

    result = publish_stopped(CHAT, OBLIGATION)

    assert result.outcome == "published"
    assert result.reason is None
    assert target.read_bytes() == SAVED
    after = store.read(CHAT)
    expected = copy.deepcopy(before)
    expected["documents"][file_id]["versions"][1]["published"] = True
    expected["documents"][file_id]["published_version"] = 2
    expected["documents"][file_id]["published_sha256"] = _sha(SAVED)
    expected["sessions"][SESSION]["baseline_sha256"] = _sha(SAVED)
    del expected["journal"][OBLIGATION]
    assert after == expected
    persisted_index = json.loads((data / CHAT / ".ocu" / "index.json").read_text())
    assert persisted_index["counter"] == indexed["revision"] + 1
    assert persisted_index["active"]["report.docx"]["hash"] == _sha(SAVED)
    listing = broker.reconcile(CHAT)
    assert listing["revision"] == indexed["revision"] + 1
    assert listing["entries"][0]["file_id"] == file_id
    assert listing["entries"][0]["size"] == len(SAVED)
    assert listing["entries"][0]["revision"] == indexed["revision"] + 1
    assert broker.reconcile(CHAT)["revision"] == listing["revision"]
    container.pause.assert_not_called()
    container.unpause.assert_not_called()
    container.start.assert_not_called()
    engine.containers.create.assert_not_called()
    assert container.status == "exited"


def _publish():
    from office.publish import publish_stopped
    return publish_stopped(CHAT, OBLIGATION)


def _without_obligation(fixture):
    expected = copy.deepcopy(fixture.before)
    del expected["journal"][OBLIGATION]
    return expected


def _index(fixture):
    return fixture.data / CHAT / ".ocu" / "index.json"


def _on_staging_sync(monkeypatch, action):
    original_open, original_sync = os.open, os.fsync
    staged = set()

    def opened(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if isinstance(path, str) and path.startswith(".office-publish."):
            staged.add(fd)
        return fd

    def synced(fd):
        if fd in staged:
            staged.remove(fd)
            action(fd)
        return original_sync(fd)

    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "fsync", synced)


def test_same_size_and_same_mtime_workspace_change_conflicts(world):
    fixture = _prepared(world)
    previous = fixture.target.stat()
    fixture.target.write_bytes(b"agent-change")
    os.utime(fixture.target, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    result = _publish()
    assert (result.outcome, result.reason) == ("conflict", "baseline_mismatch")
    assert fixture.target.read_bytes() == b"agent-change"
    assert fixture.store.read(CHAT) == _without_obligation(fixture)
    assert fixture.broker.current_revision(CHAT) == fixture.indexed["revision"]


@pytest.mark.parametrize("change", ["missing", "unrecorded-rename", "recorded-rename"])
def test_publish_follows_only_indexed_rename(world, change):
    fixture = _prepared(world)
    renamed = fixture.target.with_name("renamed.docx")
    if change == "missing":
        fixture.target.unlink()
    else:
        fixture.target.rename(renamed)
    if change == "recorded-rename":
        assert fixture.broker.reconcile(CHAT)["entries"][0]["file_id"] == fixture.file_id
    result = _publish()
    assert not fixture.target.exists()
    if change == "recorded-rename":
        assert result.outcome == "published"
        assert renamed.read_bytes() == SAVED
        assert fixture.store.read(CHAT)["documents"][fixture.file_id]["published_version"] == 2
    else:
        assert (result.outcome, result.reason) == ("conflict", "path_missing")
        assert fixture.store.read(CHAT) == _without_obligation(fixture)
        if change != "missing":
            assert renamed.read_bytes() == BASELINE


@pytest.mark.parametrize("component", ["file", "parent"])
def test_initial_symlink_does_not_read_outside_workspace(world, monkeypatch, component):
    from tests.orchestrator.test_office_workspace import _forbid_inode_open
    fixture = _prepared(world, path="nested/report.docx")
    outside = fixture.data.parent / "outside"
    outside.mkdir()
    outside_file = outside / "report.docx"
    outside_file.write_bytes(BASELINE)
    if component == "file":
        fixture.target.unlink()
        fixture.target.symlink_to(outside_file)
    else:
        fixture.target.parent.rename(fixture.target.parent.with_name("detached"))
        fixture.target.parent.symlink_to(outside, target_is_directory=True)
    _forbid_inode_open(world[0], monkeypatch, outside_file)
    result = _publish()
    assert (result.outcome, result.reason) == ("conflict", "baseline_mismatch")
    assert fixture.store.read(CHAT) == _without_obligation(fixture)
    assert outside_file.read_bytes() == BASELINE


@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_late_parent_change_rejects_replace_and_cleans_renamed_parent(world, monkeypatch, replacement):
    fixture = _prepared(world, path="nested/report.docx")
    original_parent = fixture.target.parent
    detached = original_parent.with_name("detached")
    outside = fixture.data.parent / "outside"
    outside.mkdir()
    (outside / "report.docx").write_bytes(b"outside-data")

    def change(_fd):
        original_parent.rename(detached)
        if replacement == "symlink":
            original_parent.symlink_to(outside, target_is_directory=True)
        else:
            original_parent.mkdir()
            (original_parent / "report.docx").write_bytes(BASELINE)
    _on_staging_sync(monkeypatch, change)
    result = _publish()
    assert (result.outcome, result.reason) == ("failed", "unsafe_path")
    assert (detached / "report.docx").read_bytes() == BASELINE
    assert not list(detached.glob(".office-publish.*"))
    assert (outside / "report.docx").read_bytes() == b"outside-data"
    assert fixture.store.read(CHAT) == _without_obligation(fixture)


@pytest.mark.parametrize("collision", ["file", "symlink"])
def test_temporary_collision_is_never_followed_or_removed(world, monkeypatch, collision):
    fixture = _prepared(world)
    original = os.open
    collided = []
    outside = fixture.data.parent / "outside"
    outside.write_bytes(b"not-owned")

    def opened(path, flags, *args, **kwargs):
        if isinstance(path, str) and path.startswith(".office-publish."):
            entry = fixture.store.read(CHAT)["journal"][OBLIGATION]
            assert entry["target_path"] == "report.docx"
            assert entry["temporary_name"] == path
            target = fixture.target.parent / path
            if collision == "file":
                target.write_bytes(b"collision")
            else:
                target.symlink_to(outside)
            collided.append(target)
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", opened)
    with pytest.raises(FileExistsError):
        _publish()
    assert fixture.target.read_bytes() == BASELINE
    assert outside.read_bytes() == b"not-owned"
    assert collided[0].is_symlink() if collision == "symlink" else collided[0].read_bytes() == b"collision"
    assert OBLIGATION in fixture.store.read(CHAT)["journal"]
    assert fixture.broker.current_revision(CHAT) == fixture.indexed["revision"]


def test_corrupt_index_fails_without_workspace_change(world):
    fixture = _prepared(world)
    _index(fixture).write_bytes(b"{broken")
    result = _publish()
    assert (result.outcome, result.reason) == ("failed", "index_unavailable")
    assert _index(fixture).read_bytes() == b"{broken"
    assert fixture.target.read_bytes() == BASELINE
    assert fixture.store.read(CHAT) == _without_obligation(fixture)


def test_hidden_staging_adds_no_listing_event(world, monkeypatch):
    fixture = _prepared(world)
    observations = []

    def observe(fd):
        assert os.fstat(fd).st_size == len(SAVED)
        observations.append(fixture.broker.reconcile(CHAT))
    _on_staging_sync(monkeypatch, observe)
    assert _publish().outcome == "published"
    assert [entry["path"] for entry in observations[0]["entries"]] == ["report.docx"]
    assert observations[0]["revision"] == fixture.indexed["revision"]
    assert fixture.broker.reconcile(CHAT)["revision"] == fixture.indexed["revision"] + 1
    assert not list(fixture.target.parent.glob(".office-publish.*"))


def test_unrelated_active_writer_is_not_read_or_reconciled(world, monkeypatch):
    from tests.orchestrator.test_office_workspace import _forbid_inode_open
    fixture = _prepared(world)
    unrelated = fixture.target.with_name("unrelated.txt")
    unrelated.write_bytes(b"active")
    _forbid_inode_open(world[0], monkeypatch, unrelated)
    ready, release = threading.Event(), threading.Event()
    failures = []

    def writer():
        try:
            with unrelated.open("ab", buffering=0) as stream:
                stream.write(b"-writing")
                ready.set()
                assert release.wait(5)
                stream.write(b"-complete")
        except BaseException as exc:
            failures.append(exc)
    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert ready.wait(5)
        assert _publish().outcome == "published"
        persisted = json.loads(_index(fixture).read_text())
        assert set(persisted["active"]) == {"report.docx"}
        assert persisted["counter"] == fixture.indexed["revision"] + 1
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert not failures
    assert unrelated.read_bytes() == b"active-writing-complete"


def test_second_publish_uses_updated_bound_baseline(world):
    fixture = _prepared(world)
    assert _publish().outcome == "published"
    third = b"third-save-v3"
    fixture.store.store_version(CHAT, fixture.file_id, third, source="save", parent=2,
                               published=False, min_free_bytes=0)

    def next_obligation(state):
        state["journal"][OBLIGATION] = {"file_id": fixture.file_id, "version": 3,
            "session_id": SESSION, "save_seq": 4, "requester": "save"}
    fixture.store.update(CHAT, next_obligation)
    assert _publish().outcome == "published"
    assert fixture.target.read_bytes() == third
    after = fixture.store.read(CHAT)
    assert after["sessions"][SESSION]["baseline_sha256"] == _sha(third)
    assert after["sessions"][SESSION]["last_published_seq"] == 1
    assert after["documents"][fixture.file_id]["published_version"] == 3
    assert fixture.broker.current_revision(CHAT) == fixture.indexed["revision"] + 2


@pytest.mark.parametrize("status", ["running", "paused", "created", "restarting", "removing", "dead", "unknown", ""])
def test_unsupported_actual_sandbox_state_preserves_obligation(world, status):
    from office.publish import SandboxStateError
    fixture = _prepared(world)
    fixture.container.status = status
    with pytest.raises(SandboxStateError):
        _publish()
    assert fixture.store.read(CHAT) == fixture.before
    assert fixture.target.read_bytes() == BASELINE
    fixture.container.pause.assert_not_called()
    fixture.container.unpause.assert_not_called()


@pytest.mark.parametrize("boundary", ["lookup", "reload"])
def test_engine_uncertainty_is_not_absence(world, boundary):
    fixture = _prepared(world)
    if boundary == "lookup":
        fixture.engine.containers.get.side_effect = RuntimeError("engine uncertain")
    else:
        fixture.container.reload.side_effect = RuntimeError("engine uncertain")
    with pytest.raises(RuntimeError, match="engine uncertain"):
        _publish()
    assert fixture.store.read(CHAT) == fixture.before
    assert fixture.target.read_bytes() == BASELINE


@pytest.mark.parametrize("field,value", [
    ("file_id", "foreign"), ("version", 99), ("version", True),
    ("session_id", "foreign"), ("save_seq", True), ("save_seq", 99),
    ("requester", "invented"),
])
def test_invalid_obligation_binding_preserves_everything(world, field, value):
    fixture = _prepared(world)
    def damage(state):
        state["journal"][OBLIGATION][field] = value
    before = fixture.store.update(CHAT, damage)
    with pytest.raises(world[0].StateCorruptError):
        _publish()
    assert fixture.store.read(CHAT) == before
    assert fixture.target.read_bytes() == BASELINE
    fixture.engine.containers.get.assert_not_called()


def test_sessionless_restore_uses_document_baseline_without_touching_sessions(world):
    fixture = _prepared(world)
    def unbind(state):
        state["journal"][OBLIGATION].update(session_id=None, save_seq=None, requester="restore")
        state["sessions"][SESSION]["baseline_sha256"] = _sha(b"different-session-baseline")
    before = fixture.store.update(CHAT, unbind)
    assert _publish().outcome == "published"
    assert fixture.target.read_bytes() == SAVED
    assert fixture.store.read(CHAT)["sessions"] == before["sessions"]


def test_missing_chat_is_not_recreated(world):
    from office.publish import publish_stopped
    store_mod, _manager, data = world
    with pytest.raises(store_mod.StateCorruptError):
        publish_stopped(CHAT, OBLIGATION)
    assert not (data / CHAT).exists()


def test_real_launch_waits_for_publish_and_reads_complete_file(world, monkeypatch):
    fixture = _prepared(world)
    monkeypatch.setattr(fixture.manager, "ENABLE_NETWORK", True)
    monkeypatch.setattr(fixture.manager, "OCU_SANDBOX_NETWORK", "ocu-sandbox")
    monkeypatch.setattr(fixture.manager, "SANDBOX_HOST_BIND_IP", "")
    monkeypatch.delenv("OCU_SANDBOX_DNS", raising=False)
    staging, release, contended = threading.Event(), threading.Event(), threading.Event()
    actual_lock = fixture.manager.get_chat_lock(CHAT)

    class ObservedLock:
        def acquire(self):
            if threading.current_thread().name == "publish-launch":
                if not actual_lock.acquire(blocking=False):
                    contended.set()
                    return actual_lock.acquire()
                return True
            return actual_lock.acquire()

        def release(self):
            actual_lock.release()

    fixture.manager._chat_locks[CHAT] = ObservedLock()
    observed, outcomes, failures = [], {}, []
    original_start = fixture.container.start.side_effect

    def start():
        observed.append(fixture.target.read_bytes())
        original_start()
    fixture.container.start.side_effect = start

    def hold(_fd):
        staging.set()
        assert release.wait(5)
    _on_staging_sync(monkeypatch, hold)

    def publish():
        try:
            outcomes["publish"] = _publish()
        except BaseException as exc:
            failures.append(exc)

    def launch():
        try:
            outcomes["launch"] = fixture.manager.launch_sandbox(CHAT)
        except BaseException as exc:
            failures.append(exc)

    publisher = threading.Thread(target=publish, name="publish-holder")
    launcher = threading.Thread(target=launch, name="publish-launch")
    publisher.start()
    try:
        assert staging.wait(5)
        launcher.start()
        assert contended.wait(5)
        fixture.container.start.assert_not_called()
        assert fixture.target.read_bytes() == BASELINE
    finally:
        release.set()
        publisher.join(5)
        if launcher.ident is not None:
            launcher.join(5)
    assert not publisher.is_alive() and not launcher.is_alive()
    assert not failures
    assert observed == [SAVED]
    assert outcomes["publish"].outcome == "published"
    assert outcomes["launch"] == {"state": "running"}
    assert fixture.store.read(CHAT)["journal"] == {"sibling-obligation": {"untouched": True}}


@pytest.mark.parametrize("phase", [
    "prepare-replace", "workspace-read", "temporary-write", "temporary-sync",
    "workspace-replace", "workspace-directory-sync", "index-replace",
    "index-directory-sync", "completion-replace", "completion-directory-sync",
])
def test_interrupted_io_retains_recovery_obligation_or_visible_successor(world, monkeypatch, phase):
    fixture = _prepared(world)
    original_open, original_write = os.open, os.write
    original_replace, original_sync, original_read = os.replace, os.fsync, os.read
    temporary_fds = set()
    workspace_fds = set()
    state_replaces = 0
    workspace_replaced = False
    index_replaced = False
    office_identity = (fixture.data / CHAT / ".ocu" / "office").stat()
    outputs_identity = fixture.target.parent.stat()
    control_identity = _index(fixture).parent.stat()
    target_identity = fixture.target.stat()

    def same(fd, info):
        current = os.fstat(fd)
        return (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino)

    def fault():
        raise OSError(errno.EIO, f"injected {phase}")

    def opened(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if isinstance(path, str) and path.startswith(".office-publish."):
            temporary_fds.add(fd)
        elif same(fd, target_identity):
            workspace_fds.add(fd)
        return fd

    def read(fd, count):
        if phase == "workspace-read" and fd in workspace_fds and same(fd, target_identity):
            fault()
        return original_read(fd, count)

    def write(fd, body):
        if phase == "temporary-write" and fd in temporary_fds:
            fault()
        return original_write(fd, body)

    def replace(source, destination, *args, **kwargs):
        nonlocal state_replaces, workspace_replaced, index_replaced
        if destination == "state.json":
            state_replaces += 1
            if phase == "prepare-replace" and state_replaces == 1:
                fault()
            if phase == "completion-replace" and state_replaces == 2:
                fault()
        if isinstance(source, str) and source.startswith(".office-publish."):
            if phase == "workspace-replace":
                fault()
            answer = original_replace(source, destination, *args, **kwargs)
            workspace_replaced = True
            return answer
        if destination == "index.json":
            if phase == "index-replace":
                fault()
            answer = original_replace(source, destination, *args, **kwargs)
            index_replaced = True
            return answer
        return original_replace(source, destination, *args, **kwargs)

    def sync(fd):
        if phase == "temporary-sync" and fd in temporary_fds and stat.S_ISREG(os.fstat(fd).st_mode):
            fault()
        if phase == "workspace-directory-sync" and workspace_replaced and same(fd, outputs_identity):
            fault()
        if phase == "index-directory-sync" and index_replaced and same(fd, control_identity):
            fault()
        if phase == "completion-directory-sync" and state_replaces == 2 and same(fd, office_identity):
            fault()
        return original_sync(fd)

    with monkeypatch.context() as boundary:
        boundary.setattr(os, "open", opened)
        boundary.setattr(os, "write", write)
        boundary.setattr(os, "read", read)
        boundary.setattr(os, "replace", replace)
        boundary.setattr(os, "fsync", sync)
        expected_error = world[0].StateDurabilityError if phase == "completion-directory-sync" else RuntimeError if phase == "workspace-read" else OSError
        if phase == "index-directory-sync":
            from outputs_broker import CommitDurabilityError
            expected_error = CommitDurabilityError
        with pytest.raises(expected_error):
            _publish()
    after = fixture.store.read(CHAT)
    postreplace = phase in {"workspace-directory-sync", "index-replace", "index-directory-sync",
                            "completion-replace", "completion-directory-sync"}
    assert fixture.target.read_bytes() == (SAVED if postreplace else BASELINE)
    assert not list(fixture.target.parent.glob(".office-publish.*"))
    assert after["receipts"] == fixture.before["receipts"]
    if phase == "completion-directory-sync":
        assert OBLIGATION not in after["journal"]
        assert after["documents"][fixture.file_id]["published_version"] == 2
        assert after["documents"][fixture.file_id]["versions"][1]["published"] is True
        assert after["sessions"][SESSION]["baseline_sha256"] == _sha(SAVED)
    else:
        assert OBLIGATION in after["journal"]
        assert after["documents"] == fixture.before["documents"]
        assert after["sessions"] == fixture.before["sessions"]
        if phase != "prepare-replace":
            assert after["journal"][OBLIGATION]["target_path"] == "report.docx"
            assert after["journal"][OBLIGATION]["temporary_name"].startswith(".office-publish.")
    registered = phase in {"index-directory-sync", "completion-replace", "completion-directory-sync"}
    assert fixture.broker.current_revision(CHAT) == fixture.indexed["revision"] + int(registered)


def test_prepared_obligation_cannot_be_republished_as_new_attempt(world):
    from office.publish import RecoveryRequiredError
    fixture = _prepared(world)
    def prepare(state):
        state["journal"][OBLIGATION].update(target_path="report.docx", temporary_name=".interrupted")
    before = fixture.store.update(CHAT, prepare)
    with pytest.raises(RecoveryRequiredError):
        _publish()
    assert fixture.store.read(CHAT) == before
    assert fixture.target.read_bytes() == BASELINE


def test_temporary_path_replacement_does_not_delete_foreign_inode(world, monkeypatch):
    fixture = _prepared(world)
    foreign = []

    def substitute(_fd):
        temporary = fixture.target.parent / fixture.store.read(CHAT)["journal"][OBLIGATION]["temporary_name"]
        temporary.unlink()
        temporary.write_bytes(b"foreign-temp")
        foreign.append(temporary)
    _on_staging_sync(monkeypatch, substitute)
    result = _publish()
    assert (result.outcome, result.reason) == ("failed", "unsafe_path")
    assert foreign[0].read_bytes() == b"foreign-temp"
    assert fixture.target.read_bytes() == BASELINE
    assert fixture.store.read(CHAT) == _without_obligation(fixture)


def test_target_and_temporary_metadata_are_durable_before_workspace_access(world, monkeypatch):
    fixture = _prepared(world)
    original = os.lstat
    observations = []

    def inspect(path, *args, **kwargs):
        if path == "report.docx" and kwargs.get("dir_fd") is not None:
            state_path = fixture.data / CHAT / ".ocu" / "office" / "state.json"
            entry = json.loads(state_path.read_text())["journal"][OBLIGATION]
            assert entry["target_path"] == "report.docx"
            assert entry["temporary_name"].startswith(".office-publish.")
            observations.append(entry)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, "lstat", inspect)
    assert _publish().outcome == "published"
    assert observations[0]["file_id"] == fixture.file_id
    assert observations[0]["version"] == 2


def test_published_replacement_keeps_shared_workspace_write_policy(world):
    fixture = _prepared(world)
    fixture.target.chmod(0o644)
    private_state = fixture.data / CHAT / ".ocu" / "office" / "state.json"
    private_blob = private_state.parent / "versions" / _sha(SAVED)
    original_umask = os.umask(0o077)
    try:
        assert _publish().outcome == "published"
    finally:
        os.umask(original_umask)
    assert fixture.target.read_bytes() == SAVED
    assert stat.S_IMODE(fixture.target.stat().st_mode) == 0o666
    assert stat.S_IMODE(private_state.stat().st_mode) == 0o600
    assert stat.S_IMODE(private_blob.stat().st_mode) == 0o600
    assert private_blob.read_bytes() == SAVED


def test_workspace_permission_failure_retains_obligation_before_replace(world, monkeypatch):
    fixture = _prepared(world)
    fixture.target.chmod(0o644)
    before_index = _index(fixture).read_bytes()

    def fail(_fd, _mode):
        raise OSError(errno.EIO, "injected workspace permission failure")
    monkeypatch.setattr(os, "fchmod", fail)
    with pytest.raises(OSError, match="workspace permission failure"):
        _publish()
    after = fixture.store.read(CHAT)
    obligation = after["journal"][OBLIGATION]
    assert obligation["target_path"] == "report.docx"
    assert obligation["temporary_name"].startswith(".office-publish.")
    del obligation["target_path"]
    del obligation["temporary_name"]
    assert after == fixture.before
    assert fixture.target.read_bytes() == BASELINE
    assert stat.S_IMODE(fixture.target.stat().st_mode) == 0o644
    assert _index(fixture).read_bytes() == before_index
    assert not list(fixture.target.parent.glob(".office-publish.*"))


def test_grown_workspace_conflict_bounds_actual_target_reads(world, monkeypatch):
    import office.publish as publisher
    import outputs_broker

    fixture = _prepared(world)
    limit = 32
    broker = outputs_broker.OutputsBroker(max_file_size=limit)
    assert broker.reconcile(CHAT)["entries"][0]["size"] == len(BASELINE)
    before_index = _index(fixture).read_bytes()
    grown = b"agent-expanded-content-" * 3
    fixture.target.write_bytes(grown)
    target_identity = fixture.target.stat()
    read_bytes = 0
    original_read = os.read

    def observed_read(fd, count):
        nonlocal read_bytes
        body = original_read(fd, count)
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) == (target_identity.st_dev, target_identity.st_ino):
            read_bytes += len(body)
        return body

    with monkeypatch.context() as boundary:
        boundary.setattr(publisher, "OutputsBroker", lambda: broker)
        boundary.setattr(os, "read", observed_read)
        result = _publish()
    assert (result.outcome, result.reason) == ("conflict", "baseline_mismatch")
    assert read_bytes <= limit + 1
    assert fixture.target.read_bytes() == grown
    assert fixture.target.stat().st_ino == target_identity.st_ino
    assert fixture.target.stat().st_size == len(grown)
    assert _index(fixture).read_bytes() == before_index
    assert fixture.store.read(CHAT) == _without_obligation(fixture)
    blob = fixture.data / CHAT / ".ocu" / "office" / "versions" / _sha(SAVED)
    assert blob.read_bytes() == SAVED


def test_exact_limit_baseline_is_hashed_and_published(world, monkeypatch):
    import office.publish as publisher
    import outputs_broker

    fixture = _prepared(world)
    limit = len(BASELINE)
    broker = outputs_broker.OutputsBroker(max_file_size=limit)
    before_index = _index(fixture).read_bytes()
    original_read = os.read
    target_identity = fixture.target.stat()
    read_bytes = 0

    def observed_read(fd, count):
        nonlocal read_bytes
        body = original_read(fd, count)
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) == (target_identity.st_dev, target_identity.st_ino):
            read_bytes += len(body)
        return body

    with monkeypatch.context() as boundary:
        boundary.setattr(publisher, "OutputsBroker", lambda: broker)
        boundary.setattr(os, "read", observed_read)
        result = _publish()
    assert read_bytes == limit
    assert (result.outcome, result.reason) == ("published", None)
    assert fixture.target.read_bytes() == SAVED
    after = fixture.store.read(CHAT)
    expected = copy.deepcopy(fixture.before)
    document = expected["documents"][fixture.file_id]
    document["versions"][1]["published"] = True
    document["published_version"] = 2
    document["published_sha256"] = _sha(SAVED)
    expected["sessions"][SESSION]["baseline_sha256"] = _sha(SAVED)
    del expected["journal"][OBLIGATION]
    assert after == expected
    before_counter = json.loads(before_index)["counter"]
    persisted = json.loads(_index(fixture).read_bytes())
    assert persisted["counter"] == before_counter + 1
    assert persisted["active"]["report.docx"]["hash"] == _sha(SAVED)
    assert broker.reconcile(CHAT)["revision"] == before_counter + 1
    assert broker.reconcile(CHAT)["revision"] == before_counter + 1
