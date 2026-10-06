# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Running publication uses an observed engine fence around real workspace IO."""
from __future__ import annotations

import copy
import json
import os
import stat
import time
from types import SimpleNamespace

import pytest
from docker.errors import NotFound

from tests.orchestrator._office_store import CHAT
from tests.orchestrator.test_office_publish import (
    BASELINE,
    OBLIGATION,
    SAVED,
    SESSION,
    _prepared,
    _reload_publish,
    _sha,
    _on_staging_sync,
    _publish,
    _without_obligation,
)


def _running(world):
    fixture = _prepared(world)
    container = fixture.container
    fixture.engine_state = {"status": "running"}
    fixture.events = []
    fixture.marker = fixture.data / CHAT / ".ocu" / "office" / "fence.json"
    fixture.original_id = container.id

    def observe():
        container.status = fixture.engine_state["status"]
        container.attrs["State"].update(
            Status=container.status, Paused=container.status == "paused",
        )
        fixture.events.append(f"observed-{container.status}")

    def pause():
        marker = json.loads(fixture.marker.read_text())
        assert marker["container_id"] == fixture.original_id
        assert isinstance(marker["pause_started_at"], (float, int))
        assert stat.S_IMODE(fixture.marker.stat().st_mode) == 0o600
        entry = fixture.store.read(CHAT)["journal"][OBLIGATION]
        fixture.pause_observation = {
            "marker": marker, "mode": stat.S_IMODE(fixture.marker.stat().st_mode),
            "marker_inode": fixture.marker.stat().st_ino, "entry": entry,
        }
        assert entry["target_path"] == "report.docx"
        assert entry["temporary_name"].startswith(".office-publish.")
        fixture.events.append("pause")
        fixture.engine_state["status"] = "paused"

    def unpause():
        assert container.id == fixture.original_id
        assert fixture.marker.is_file()
        fixture.release_observation = {
            "container_id": container.id, "marker_inode": fixture.marker.stat().st_ino,
            "workspace_bytes": fixture.target.read_bytes(),
        }
        fixture.events.append("unpause")
        fixture.engine_state["status"] = "running"

    container.reload.side_effect = observe
    container.pause.side_effect = pause
    container.unpause.side_effect = unpause
    observe()
    return fixture


def test_running_publish_observes_pause_before_workspace_work_and_releases_owned_fence(
    world, monkeypatch, _reload_publish,
):
    fixture = _running(world)
    container = fixture.container
    events, marker, original_id = fixture.events, fixture.marker, fixture.original_id
    release = container.unpause.side_effect

    def unpause():
        assert fixture.target.read_bytes() == SAVED
        release()

    container.unpause.side_effect = unpause
    real_open, real_replace = os.open, os.replace

    def require_observed_pause():
        assert fixture.engine_state["status"] == "paused", "workspace work began without the writer barrier"
        assert "observed-paused" in events, "pause request was not freshly observed"
        assert events.index("pause") < events.index("observed-paused")
        assert marker.is_file()

    def opened(path, flags, *args, **kwargs):
        name = os.fspath(path) if not isinstance(path, int) else path
        if name in (fixture.target.name, str(fixture.target)):
            require_observed_pause()
            events.append("workspace-read")
        elif isinstance(name, str) and name.startswith(".office-publish."):
            require_observed_pause()
            events.append("workspace-stage")
        return real_open(path, flags, *args, **kwargs)

    def replaced(source, target, *args, **kwargs):
        if os.fspath(target) in (fixture.target.name, str(fixture.target)):
            require_observed_pause()
            events.append("workspace-replace")
        return real_replace(source, target, *args, **kwargs)

    from office.publish import publish

    with monkeypatch.context() as boundary:
        boundary.setattr(os, "open", opened)
        boundary.setattr(os, "replace", replaced)
        result = publish(CHAT, OBLIGATION)

    assert (result.outcome, result.reason) == ("published", None)
    assert fixture.target.read_bytes() == SAVED
    expected = copy.deepcopy(fixture.before)
    expected["documents"][fixture.file_id]["versions"][1]["published"] = True
    expected["documents"][fixture.file_id]["published_version"] = 2
    expected["documents"][fixture.file_id]["published_sha256"] = _sha(SAVED)
    expected["sessions"][SESSION]["baseline_sha256"] = _sha(SAVED)
    del expected["journal"][OBLIGATION]
    assert fixture.store.read(CHAT) == expected
    index = json.loads((fixture.data / CHAT / ".ocu" / "index.json").read_text())
    assert index["counter"] == fixture.indexed["revision"] + 1
    assert index["active"]["report.docx"]["file_id"] == fixture.file_id
    assert index["active"]["report.docx"]["hash"] == _sha(SAVED)
    assert fixture.broker.reconcile(CHAT)["revision"] == index["counter"]
    assert fixture.pause_observation["marker"]["container_id"] == original_id
    assert isinstance(fixture.pause_observation["marker"]["pause_started_at"], (float, int))
    assert fixture.pause_observation["mode"] == 0o600
    assert fixture.pause_observation["entry"]["target_path"] == "report.docx"
    assert fixture.release_observation == {
        "container_id": original_id,
        "marker_inode": fixture.pause_observation["marker_inode"],
        "workspace_bytes": SAVED,
    }
    assert events.index("observed-paused") < events.index("workspace-read")
    assert events.index("workspace-read") < events.index("workspace-stage")
    assert events.index("workspace-stage") < events.index("workspace-replace")
    assert events.index("workspace-replace") < events.index("unpause")
    assert "observed-running" in events[events.index("unpause") + 1:]
    container.pause.assert_called_once_with()
    container.unpause.assert_called_once_with()
    container.start.assert_not_called()
    fixture.engine.containers.create.assert_not_called()
    assert container.id == original_id
    assert container.status == "running"
    assert container.attrs["State"]["Paused"] is False
    assert not marker.exists()


@pytest.mark.parametrize("failure", ["refused", "effect-despite-error", "not-paused", "observation-error"])
def test_failed_pause_keeps_workspace_and_releases_any_owned_pause(world, failure):
    fixture = _running(world)
    pause, observe = fixture.container.pause.side_effect, fixture.container.reload.side_effect
    failed_observation = False

    def request():
        if failure != "refused":
            pause()
        if failure in {"refused", "effect-despite-error"}:
            raise RuntimeError("pause API failed")
        if failure == "not-paused":
            fixture.engine_state["status"] = "running"

    def inspect():
        nonlocal failed_observation
        if failure == "observation-error" and "pause" in fixture.events and not failed_observation:
            failed_observation = True
            raise RuntimeError("inspection unavailable")
        observe()

    fixture.container.pause.side_effect = request
    fixture.container.reload.side_effect = inspect
    result = _publish()
    assert (result.outcome, result.reason) == ("failed", "pause_failed")
    assert fixture.target.read_bytes() == BASELINE
    assert fixture.store.read(CHAT) == _without_obligation(fixture)
    assert fixture.broker.current_revision(CHAT) == fixture.indexed["revision"]
    assert fixture.engine_state["status"] == "running"
    assert not fixture.marker.exists()
    if failure in {"effect-despite-error", "observation-error"}:
        fixture.container.unpause.assert_called_once_with()
    else:
        fixture.container.unpause.assert_not_called()


@pytest.mark.parametrize("failure", ["refused", "effect-despite-error", "unknown-after-release"])
@pytest.mark.parametrize("conflict", [False, True])
def test_release_uncertainty_does_not_change_established_publish_outcome(world, failure, conflict, caplog):
    fixture = _running(world)
    if conflict:
        fixture.target.write_bytes(b"agent-content")
    release, observe = fixture.container.unpause.side_effect, fixture.container.reload.side_effect

    def unpause():
        fixture.events.append("release-attempt")
        if failure != "refused":
            release()
        if failure != "unknown-after-release":
            raise RuntimeError("release API failed")

    def inspect():
        if failure == "unknown-after-release" and "release-attempt" in fixture.events:
            raise RuntimeError("release observation uncertain")
        observe()

    fixture.container.unpause.side_effect = unpause
    fixture.container.reload.side_effect = inspect
    with caplog.at_level("INFO", logger="office.publish"):
        result = _publish()
    assert (result.outcome, result.reason) == (("conflict", "baseline_mismatch") if conflict else ("published", None))
    assert fixture.target.read_bytes() == (b"agent-content" if conflict else SAVED)
    after = fixture.store.read(CHAT)
    assert OBLIGATION not in after["journal"]
    assert after["documents"][fixture.file_id]["versions"][1]["published"] is (not conflict)
    retained = failure != "effect-despite-error"
    assert fixture.marker.exists() is retained
    records = [record for record in caplog.records if hasattr(record, "paused_duration_seconds")]
    assert len(records) == 1
    assert records[0].release_observed is (not retained)
    assert records[0].marker_retained is retained
    assert records[0].publish_outcome == result.outcome


def test_external_pause_is_published_without_claiming_or_releasing_it(world, caplog):
    fixture = _running(world)
    fixture.engine_state["status"] = "paused"
    with caplog.at_level("INFO", logger="office.publish"):
        assert _publish().outcome == "published"
    assert fixture.target.read_bytes() == SAVED
    assert fixture.container.status == "paused"
    assert fixture.container.attrs["State"]["Paused"] is True
    fixture.container.pause.assert_not_called()
    fixture.container.unpause.assert_not_called()
    assert not fixture.marker.exists()
    assert not [record for record in caplog.records if hasattr(record, "paused_duration_seconds")]


@pytest.mark.parametrize("missing", ["index", "file-id", "path"])
def test_running_missing_target_never_requests_pause(world, missing):
    fixture = _running(world)
    index = fixture.data / CHAT / ".ocu" / "index.json"
    if missing == "index":
        index.write_bytes(b"{invalid")
    elif missing == "file-id":
        index.unlink()
    else:
        fixture.target.unlink()
    result = _publish()
    assert (result.outcome, result.reason) == (
        ("failed", "index_unavailable") if missing == "index" else ("conflict", "path_missing")
    )
    fixture.container.pause.assert_not_called()
    fixture.container.unpause.assert_not_called()
    assert OBLIGATION not in fixture.store.read(CHAT)["journal"]
    assert not fixture.marker.exists()
    if missing != "path":
        assert fixture.target.read_bytes() == BASELINE


@pytest.mark.parametrize("status", ["running", "paused", "exited"])
@pytest.mark.parametrize("marker_kind", ["file", "symlink", "directory"])
def test_existing_fence_is_recovery_owned_and_never_overwritten(world, status, marker_kind):
    from office.publish import RecoveryRequiredError
    fixture = _running(world)
    fixture.engine_state["status"] = status
    outside = fixture.data.parent / "foreign-marker"
    outside.write_bytes(b"foreign-fence")
    if marker_kind == "file":
        fixture.marker.write_bytes(b"old-fence")
    elif marker_kind == "symlink":
        fixture.marker.symlink_to(outside)
    else:
        fixture.marker.mkdir()
    original = fixture.marker.lstat()
    with pytest.raises(RecoveryRequiredError):
        _publish()
    assert fixture.marker.lstat().st_ino == original.st_ino
    assert outside.read_bytes() == b"foreign-fence"
    if marker_kind == "file":
        assert fixture.marker.read_bytes() == b"old-fence"
    assert fixture.store.read(CHAT) == fixture.before
    assert fixture.target.read_bytes() == BASELINE
    fixture.container.pause.assert_not_called()
    fixture.container.unpause.assert_not_called()


@pytest.mark.parametrize("change", ["stopped", "absent", "replacement", "unknown"])
def test_retention_and_container_identity_changes_never_resume_another_sandbox(world, monkeypatch, change):
    from tests.orchestrator.test_lifecycle import _container
    fixture = _running(world)
    observe = fixture.container.reload.side_effect
    replacement = _container(fixture.container.name, status="paused", container_id="replacement-id")
    changed = False

    def during_staging(_fd):
        nonlocal changed
        changed = True
        if change == "stopped":
            fixture.engine_state["status"] = "exited"
        elif change == "replacement":
            fixture.engine.containers.get.side_effect = lambda _name: replacement
        elif change == "unknown":
            fixture.engine_state["status"] = "restarting"

    def inspect():
        if changed and change in {"absent", "replacement"}:
            raise NotFound("original container removed")
        observe()

    fixture.container.reload.side_effect = inspect
    _on_staging_sync(monkeypatch, during_staging)
    assert _publish().outcome == "published"
    assert fixture.target.read_bytes() == SAVED
    assert fixture.store.read(CHAT)["documents"][fixture.file_id]["versions"][1]["published"] is True
    assert fixture.marker.exists() is (change == "unknown")
    fixture.container.unpause.assert_not_called()
    fixture.container.start.assert_not_called()
    replacement.unpause.assert_not_called()
    replacement.start.assert_not_called()
    if change == "stopped":
        assert fixture.container.status == "exited"


@pytest.mark.parametrize("phase", ["pause", "hash", "blob", "stage-open", "stage-write", "staging", "before-replace", "replace", "directory-sync", "registration"])
@pytest.mark.parametrize("elapsed", [4.999, 5.0])
def test_monotonic_budget_preserves_prewrite_or_interrupted_successor(world, monkeypatch, phase, elapsed):
    fixture = _running(world)
    clock = SimpleNamespace(value=0.0)
    monkeypatch.setattr(time, "monotonic", lambda: clock.value)
    # Wall time is deliberately unrelated to the publication budget.
    monkeypatch.setattr(time, "time", lambda: 1_700_000_000.0 - clock.value * 1000)
    target_identity = fixture.target.stat()
    blob_identity = (fixture.marker.parent / "versions" / _sha(SAVED)).stat()
    outputs_identity = fixture.target.parent.stat()
    original_pause = fixture.container.pause.side_effect
    real_read, real_sync, real_replace, real_lstat = os.read, os.fsync, os.replace, os.lstat
    staged_fds = set()
    real_open = os.open
    real_write = os.write
    real_close = os.close
    staged_writes = []
    replaced = False
    expiry_triggered = False

    def same(fd, info):
        actual = os.fstat(fd)
        return (actual.st_dev, actual.st_ino) == (info.st_dev, info.st_ino)

    def expire():
        nonlocal expiry_triggered
        expiry_triggered = True
        clock.value = elapsed

    def pause():
        original_pause()
        if phase == "pause":
            expire()

    def opened(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if isinstance(path, str) and path.startswith(".office-publish."):
            staged_fds.add(fd)
            if phase == "stage-open":
                expire()
        return fd

    def read(fd, count):
        body = real_read(fd, count)
        if not expiry_triggered and ((phase == "hash" and same(fd, target_identity))
                or (phase == "blob" and same(fd, blob_identity))):
            expire()
        return body

    def closed(fd):
        staged_fds.discard(fd)
        return real_close(fd)

    def write(fd, body):
        answer = real_write(fd, body)
        if fd in staged_fds:
            staged_writes.append(bytes(body))
            if phase == "stage-write":
                expire()
        return answer

    def sync(fd):
        answer = real_sync(fd)
        if phase == "staging" and fd in staged_fds:
            expire()
        elif phase == "directory-sync" and replaced and same(fd, outputs_identity):
            expire()
        return answer

    def inspect(path, *args, **kwargs):
        answer = real_lstat(path, *args, **kwargs)
        if phase == "before-replace" and isinstance(path, str) and path.startswith(".office-publish."):
            expire()
        return answer

    def replace(source, destination, *args, **kwargs):
        nonlocal replaced
        answer = real_replace(source, destination, *args, **kwargs)
        if destination == "report.docx":
            replaced = True
            if phase == "replace":
                expire()
        elif destination == "index.json" and phase == "registration":
            expire()
        return answer

    fixture.container.pause.side_effect = pause
    with monkeypatch.context() as boundary:
        boundary.setattr(os, "open", opened)
        boundary.setattr(os, "close", closed)
        boundary.setattr(os, "read", read)
        boundary.setattr(os, "write", write)
        boundary.setattr(os, "fsync", sync)
        boundary.setattr(os, "lstat", inspect)
        boundary.setattr(os, "replace", replace)
        result = _publish()
    assert expiry_triggered
    if phase == "stage-open" and elapsed == 5.0:
        assert staged_writes == []
    interrupted = elapsed == 5.0 and phase in {"replace", "directory-sync", "registration"}
    timed_out = elapsed == 5.0
    assert (result.outcome, result.reason) == (
        ("interrupted" if interrupted else "failed", "publish_timeout") if timed_out else ("published", None)
    )
    assert fixture.target.read_bytes() == (SAVED if interrupted or not timed_out else BASELINE)
    after = fixture.store.read(CHAT)
    assert (OBLIGATION in after["journal"]) is interrupted
    assert after["documents"][fixture.file_id]["versions"][1]["published"] is (not timed_out)
    assert fixture.broker.current_revision(CHAT) == fixture.indexed["revision"] + int(not timed_out or phase == "registration")
    assert not list(fixture.target.parent.glob(".office-publish.*"))
    assert fixture.engine_state["status"] == "running"
    assert not fixture.marker.exists()


@pytest.mark.parametrize("conflict", [False, True])
def test_slow_release_preserves_completed_outcome_and_logs_elapsed_at_return(world, monkeypatch, caplog, conflict):
    fixture = _running(world)
    if conflict:
        fixture.target.write_bytes(b"agent-content")
    clock = SimpleNamespace(value=0.0)
    monkeypatch.setattr(time, "monotonic", lambda: clock.value)
    release = fixture.container.unpause.side_effect

    def delayed_release():
        clock.value = 8.0
        release()

    fixture.container.unpause.side_effect = delayed_release
    with caplog.at_level("INFO", logger="office.publish"):
        result = _publish()
    assert result.outcome == ("conflict" if conflict else "published")
    assert OBLIGATION not in fixture.store.read(CHAT)["journal"]
    assert fixture.target.read_bytes() == (b"agent-content" if conflict else SAVED)
    records = [record for record in caplog.records if hasattr(record, "paused_duration_seconds")]
    assert len(records) == 1
    assert records[0].paused_duration_seconds == 8.0
    assert records[0].release_observed is True
    assert records[0].marker_retained is False


def test_blocking_pause_overruns_budget_without_starting_workspace_write(world, monkeypatch, caplog):
    fixture = _running(world)
    pause = fixture.container.pause.side_effect
    real_open = os.open
    workspace_opens = []

    def delayed_pause():
        pause()
        time.sleep(5.02)

    def opened(path, flags, *args, **kwargs):
        if path == "report.docx" or isinstance(path, str) and path.startswith(".office-publish."):
            workspace_opens.append(path)
        return real_open(path, flags, *args, **kwargs)

    fixture.container.pause.side_effect = delayed_pause
    with caplog.at_level("INFO", logger="office.publish"), monkeypatch.context() as boundary:
        boundary.setattr(os, "open", opened)
        result = _publish()
    assert (result.outcome, result.reason) == ("failed", "publish_timeout")
    assert workspace_opens == []
    assert fixture.target.read_bytes() == BASELINE
    assert fixture.store.read(CHAT) == _without_obligation(fixture)
    assert fixture.engine_state["status"] == "running"
    assert not fixture.marker.exists()
    records = [record for record in caplog.records if hasattr(record, "paused_duration_seconds")]
    assert len(records) == 1
    assert records[0].paused_duration_seconds >= 5
    assert records[0].release_observed is True


@pytest.mark.parametrize("phase", ["create", "write", "file-sync", "directory-sync"])
def test_marker_durability_failure_cannot_request_pause_and_closes_descriptors(world, monkeypatch, phase):
    fixture = _running(world)
    real_open, real_close, real_write, real_sync = os.open, os.close, os.write, os.fsync
    opened_fds, marker_fds = set(), set()
    office_identity = fixture.marker.parent.stat()

    def fail():
        raise OSError(5, f"marker {phase} failed")

    def opened(path, flags, *args, **kwargs):
        if isinstance(path, str) and path.startswith(".fence.") and flags & os.O_CREAT and phase == "create":
            fail()
        fd = real_open(path, flags, *args, **kwargs)
        opened_fds.add(fd)
        if isinstance(path, str) and path.startswith(".fence."):
            marker_fds.add(fd)
        return fd

    def closed(fd):
        opened_fds.discard(fd)
        marker_fds.discard(fd)
        return real_close(fd)

    def write(fd, body):
        if fd in marker_fds and phase == "write":
            fail()
        return real_write(fd, body)

    def sync(fd):
        if fd in marker_fds and phase == "file-sync":
            fail()
        info = os.fstat(fd)
        if (phase == "directory-sync" and fixture.marker.exists()
                and (info.st_dev, info.st_ino) == (office_identity.st_dev, office_identity.st_ino)):
            fail()
        return real_sync(fd)

    with monkeypatch.context() as boundary:
        boundary.setattr(os, "open", opened)
        boundary.setattr(os, "close", closed)
        boundary.setattr(os, "write", write)
        boundary.setattr(os, "fsync", sync)
        with pytest.raises(OSError, match=f"marker {phase} failed"):
            _publish()
    assert opened_fds == set()
    fixture.container.pause.assert_not_called()
    fixture.container.unpause.assert_not_called()
    assert fixture.target.read_bytes() == BASELINE
    assert fixture.store.read(CHAT)["journal"][OBLIGATION]["target_path"] == "report.docx"
    assert not fixture.marker.exists()
    assert not list(fixture.marker.parent.glob(".fence.*"))


@pytest.mark.parametrize("failure", ["unlink", "directory-sync"])
def test_marker_cleanup_durability_error_preserves_published_success(world, monkeypatch, caplog, failure):
    fixture = _running(world)
    real_unlink, real_sync = os.unlink, os.fsync
    office_identity = fixture.marker.parent.stat()

    def unlink(path, *args, **kwargs):
        if path == "fence.json" and failure == "unlink":
            raise OSError(5, "marker unlink failed")
        return real_unlink(path, *args, **kwargs)

    def sync(fd):
        info = os.fstat(fd)
        if (failure == "directory-sync" and "unpause" in fixture.events
                and (info.st_dev, info.st_ino) == (office_identity.st_dev, office_identity.st_ino)
                and OBLIGATION in fixture.store.read(CHAT)["journal"]):
            raise OSError(5, "marker directory sync failed")
        return real_sync(fd)

    with caplog.at_level("INFO", logger="office.publish"), monkeypatch.context() as boundary:
        boundary.setattr(os, "unlink", unlink)
        boundary.setattr(os, "fsync", sync)
        result = _publish()
    assert result.outcome == "published"
    assert fixture.target.read_bytes() == SAVED
    assert OBLIGATION not in fixture.store.read(CHAT)["journal"]
    assert fixture.marker.exists() is (failure == "unlink")
    assert fixture.engine_state["status"] == "running"
    records = [record for record in caplog.records if hasattr(record, "paused_duration_seconds")]
    assert len(records) == 1
    assert records[0].cleanup_failed is True
    assert records[0].release_observed is True
    assert records[0].marker_retained is (failure == "unlink")


def test_release_does_not_delete_a_substituted_marker_inode(world):
    fixture = _running(world)
    release = fixture.container.unpause.side_effect
    detached = fixture.marker.with_name("owned-marker")

    def substitute():
        release()
        fixture.marker.rename(detached)
        fixture.marker.write_bytes(b"foreign-fence")

    fixture.container.unpause.side_effect = substitute
    assert _publish().outcome == "published"
    assert fixture.marker.read_bytes() == b"foreign-fence"
    assert json.loads(detached.read_text())["container_id"] == fixture.original_id
    assert fixture.target.read_bytes() == SAVED
    assert OBLIGATION not in fixture.store.read(CHAT)["journal"]


@pytest.mark.parametrize("uncertainty", ["paused-bit", "identity"])
def test_unverified_pause_cannot_hash_workspace_or_release_unowned_identity(world, monkeypatch, uncertainty):
    fixture = _running(world)
    observe = fixture.container.reload.side_effect
    real_read = os.read
    target_identity = fixture.target.stat()
    target_reads = []

    def inspect():
        observe()
        if fixture.engine_state["status"] == "paused":
            if uncertainty == "paused-bit":
                fixture.container.attrs["State"]["Paused"] = "true"
            else:
                fixture.container.id = "replacement-id"

    def read(fd, count):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == (target_identity.st_dev, target_identity.st_ino):
            target_reads.append(count)
        return real_read(fd, count)

    fixture.container.reload.side_effect = inspect
    with monkeypatch.context() as boundary:
        boundary.setattr(os, "read", read)
        result = _publish()
    assert (result.outcome, result.reason) == ("failed", "pause_failed")
    assert target_reads == []
    assert fixture.target.read_bytes() == BASELINE
    assert fixture.store.read(CHAT) == _without_obligation(fixture)
    assert fixture.marker.exists()
    fixture.container.unpause.assert_not_called()


def test_workspace_error_survives_release_and_logging_failures(world, monkeypatch):
    import office.publish as publisher
    fixture = _running(world)
    real_write, real_open = os.write, os.open
    staged_fds = set()
    original_error = OSError(5, "primary workspace write failed")

    def opened(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if isinstance(path, str) and path.startswith(".office-publish."):
            staged_fds.add(fd)
        return fd

    def write(fd, body):
        if fd in staged_fds:
            raise original_error
        return real_write(fd, body)

    def release_failure():
        raise RuntimeError("secondary release failed")

    def log_failure(*args, **kwargs):
        raise RuntimeError("secondary logger failed")

    fixture.container.unpause.side_effect = release_failure
    with monkeypatch.context() as boundary:
        boundary.setattr(os, "open", opened)
        boundary.setattr(os, "write", write)
        boundary.setattr(publisher._LOG, "info", log_failure)
        with pytest.raises(OSError) as caught:
            _publish()
    assert caught.value is original_error
    assert fixture.target.read_bytes() == BASELINE
    assert OBLIGATION in fixture.store.read(CHAT)["journal"]
    assert fixture.marker.exists()
    assert fixture.engine_state["status"] == "paused"
    assert not list(fixture.target.parent.glob(".office-publish.*"))


@pytest.mark.parametrize("failure", ["completion-replace", "completion-directory-sync"])
def test_running_final_bookkeeping_keeps_visible_successor_durability_rules(world, monkeypatch, failure):
    fixture = _running(world)
    real_replace, real_sync = os.replace, os.fsync
    office_identity = fixture.marker.parent.stat()
    completion_replaced = False

    def replace(source, destination, *args, **kwargs):
        nonlocal completion_replaced
        completing = False
        if destination == "state.json":
            successor = json.loads((fixture.marker.parent / source).read_bytes())
            completing = OBLIGATION not in successor["journal"]
            if completing:
                assert "unpause" in fixture.events
                assert not fixture.marker.exists()
                if failure == "completion-replace":
                    raise OSError(5, "completion failed")
        answer = real_replace(source, destination, *args, **kwargs)
        if completing:
            completion_replaced = True
        return answer

    def sync(fd):
        info = os.fstat(fd)
        if (failure == "completion-directory-sync" and completion_replaced
                and (info.st_dev, info.st_ino) == (office_identity.st_dev, office_identity.st_ino)):
            raise OSError(5, "completion durability failed")
        return real_sync(fd)

    with monkeypatch.context() as boundary:
        boundary.setattr(os, "replace", replace)
        boundary.setattr(os, "fsync", sync)
        expected_error = world[0].StateDurabilityError if failure == "completion-directory-sync" else OSError
        with pytest.raises(expected_error):
            _publish()
    visible = failure == "completion-directory-sync"
    state = fixture.store.read(CHAT)
    assert (OBLIGATION not in state["journal"]) is visible
    assert state["documents"][fixture.file_id]["versions"][1]["published"] is visible
    assert fixture.target.read_bytes() == SAVED
    assert fixture.broker.current_revision(CHAT) == fixture.indexed["revision"] + 1
    assert fixture.engine_state["status"] == "running"
    assert not fixture.marker.exists()


@pytest.mark.parametrize("race", ["existing-marker", "staged-symlink", "directory-replacement"])
def test_marker_installation_never_clobbers_or_follows_foreign_control_paths(world, monkeypatch, race):
    from office.store import StateCorruptError
    fixture = _running(world)
    outside = fixture.data.parent / "foreign-fence"
    outside.write_bytes(b"foreign-fence")
    real_link, real_sync = os.link, os.fsync
    detached = fixture.marker.parent.with_name("detached-office")
    substituted = []

    def link(source, destination, *args, **kwargs):
        # Complete private contents exist before the public marker name appears.
        source_path = fixture.marker.parent / source
        assert json.loads(source_path.read_text())["container_id"] == fixture.original_id
        assert stat.S_IMODE(source_path.stat().st_mode) == 0o600
        assert not fixture.marker.exists()
        if race == "existing-marker":
            fixture.marker.symlink_to(outside)
        elif race == "staged-symlink":
            source_path.unlink()
            source_path.symlink_to(outside)
            substituted.append(source_path)
        return real_link(source, destination, *args, **kwargs)

    def sync(fd):
        answer = real_sync(fd)
        if race == "directory-replacement" and fixture.marker.exists() and not detached.exists():
            fixture.marker.parent.rename(detached)
            fixture.marker.parent.symlink_to(detached, target_is_directory=True)
        return answer

    with monkeypatch.context() as boundary:
        boundary.setattr(os, "link", link)
        boundary.setattr(os, "fsync", sync)
        with pytest.raises(FileExistsError if race == "existing-marker" else StateCorruptError):
            _publish()
    assert outside.read_bytes() == b"foreign-fence"
    assert fixture.target.read_bytes() == BASELINE
    fixture.container.pause.assert_not_called()
    fixture.container.unpause.assert_not_called()
    if race in {"existing-marker", "staged-symlink"}:
        assert fixture.marker.is_symlink()
        assert fixture.marker.read_bytes() == b"foreign-fence"
    if substituted:
        assert substituted[0].is_symlink()
    if race == "directory-replacement":
        assert fixture.marker.parent.is_symlink()
        assert not (detached / "fence.json").exists()


@pytest.mark.parametrize("paused", [True, False, None, "true"])
def test_engine_pause_flag_admission_is_explicit_and_external_pause_is_not_claimed(world, paused):
    from office.publish import SandboxStateError
    fixture = _running(world)
    observe = fixture.container.reload.side_effect

    def inspect():
        observe()
        fixture.container.attrs["State"]["Paused"] = paused

    fixture.container.reload.side_effect = inspect
    if type(paused) is not bool:
        with pytest.raises(SandboxStateError):
            _publish()
        assert fixture.store.read(CHAT) == fixture.before
        assert fixture.target.read_bytes() == BASELINE
    elif paused:
        assert _publish().outcome == "published"
        assert fixture.target.read_bytes() == SAVED
        fixture.container.pause.assert_not_called()
        fixture.container.unpause.assert_not_called()
    else:
        # Running false is admitted, but a later false observation cannot establish
        # a paused barrier even if the fake status changes to paused.
        assert _publish().reason == "pause_failed"
        assert fixture.target.read_bytes() == BASELINE


def test_marker_install_error_is_not_a_workspace_path_missing_outcome(world, monkeypatch):
    fixture = _running(world)

    def unavailable(*args, **kwargs):
        raise FileNotFoundError(2, "marker installation failed")

    monkeypatch.setattr(os, "link", unavailable)
    with pytest.raises(FileNotFoundError, match="marker installation failed"):
        _publish()
    assert fixture.target.read_bytes() == BASELINE
    assert fixture.store.read(CHAT)["journal"][OBLIGATION]["target_path"] == "report.docx"
    fixture.container.pause.assert_not_called()
    fixture.container.unpause.assert_not_called()
    assert not fixture.marker.exists()
    assert not list(fixture.marker.parent.glob(".fence.*"))


def test_pause_interruption_retains_obligation_and_releases_the_owned_container(world):
    fixture = _running(world)
    pause = fixture.container.pause.side_effect
    interruption = KeyboardInterrupt("pause interrupted after engine effect")

    def interrupted_pause():
        pause()
        raise interruption

    fixture.container.pause.side_effect = interrupted_pause
    with pytest.raises(KeyboardInterrupt) as caught:
        _publish()
    assert caught.value is interruption
    assert fixture.target.read_bytes() == BASELINE
    assert OBLIGATION in fixture.store.read(CHAT)["journal"]
    assert fixture.engine_state["status"] == "running"
    fixture.container.unpause.assert_called_once_with()
    assert not fixture.marker.exists()


@pytest.mark.parametrize("outcome", ["published", "conflict", "interrupted"])
def test_workspace_descriptor_cleanup_cannot_replace_established_result(world, monkeypatch, caplog, outcome):
    fixture = _running(world)
    if outcome == "conflict":
        fixture.target.write_bytes(b"agent-content")
    clock = SimpleNamespace(value=0.0)
    monkeypatch.setattr(time, "monotonic", lambda: clock.value)
    real_open, real_close, real_replace = os.open, os.close, os.replace
    held_outputs_fd = None
    injected = False

    def opened(path, flags, *args, **kwargs):
        nonlocal held_outputs_fd
        fd = real_open(path, flags, *args, **kwargs)
        if path == "outputs" and held_outputs_fd is None:
            held_outputs_fd = fd
        return fd

    def closed(fd):
        nonlocal injected
        answer = real_close(fd)
        if fd == held_outputs_fd and not injected:
            injected = True
            raise OSError(5, "descriptor cleanup failed")
        return answer

    def replace(source, destination, *args, **kwargs):
        answer = real_replace(source, destination, *args, **kwargs)
        if destination == "report.docx" and outcome == "interrupted":
            clock.value = 5.0
        return answer

    with caplog.at_level("INFO", logger="office.publish"), monkeypatch.context() as boundary:
        boundary.setattr(os, "open", opened)
        boundary.setattr(os, "close", closed)
        boundary.setattr(os, "replace", replace)
        result = _publish()
    assert injected
    assert result.outcome == outcome
    assert fixture.target.read_bytes() == (b"agent-content" if outcome == "conflict" else SAVED)
    assert (OBLIGATION in fixture.store.read(CHAT)["journal"]) is (outcome == "interrupted")
    assert fixture.engine_state["status"] == "running"
    assert not fixture.marker.exists()
    assert any(record.message == "Office publication workspace cleanup failed" for record in caplog.records)


def test_duration_distinguishes_marker_preparation_from_attempted_pause(world, monkeypatch, caplog):
    fixture = _running(world)
    clock = SimpleNamespace(value=0.0)
    monkeypatch.setattr(time, "monotonic", lambda: clock.value)
    real_link = os.link
    release = fixture.container.unpause.side_effect

    def delayed_marker(*args, **kwargs):
        clock.value = 2.0
        return real_link(*args, **kwargs)

    def unpause():
        clock.value = 3.0
        release()

    fixture.container.unpause.side_effect = unpause
    with caplog.at_level("INFO", logger="office.publish"), monkeypatch.context() as boundary:
        boundary.setattr(os, "link", delayed_marker)
        assert _publish().outcome == "published"
    records = [record for record in caplog.records if hasattr(record, "paused_duration_seconds")]
    assert len(records) == 1
    assert records[0].paused_duration_seconds == 1.0
    assert records[0].publication_elapsed_seconds == 3.0
    assert records[0].duration_basis == "pause-request-through-release-handling"
    assert records[0].release_observed is True
