# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Final copy outcomes through authenticated HTTP, durable Office state and broker."""
from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
import sys
import time

import pytest

from tests.orchestrator._office_recorded_callbacks import recorded_status_1_payload, recorded_status_4_payload
from tests.orchestrator._office_store import _child_env
from tests.orchestrator.test_office_callback_publish import (
    CHANGED, _allocate, _autosaved, _bind_internal, _content_origin, _opened,
    _payload, _post, _read, _running, _sha,
)
from tests.orchestrator.test_office_control_plane import _open_session
from tests.orchestrator.test_office_session_lifecycle import _status
from tests.orchestrator.test_office_sessions import (
    _assert_refusal, _create, _outputs, _snapshot, _versions, office_world,
)
from tests.orchestrator.test_outputs_endpoint import CHAT


def _nested(world, name):
    opened = _open_session(world, name=name)
    http, _data, _origin, _manager, _broker, _body, session = opened
    assert _post(http, session, recorded_status_1_payload(document_key=session["document_key"])).json() == {"error": 0}
    return opened


def _final(http, session, monkeypatch):
    with _content_origin({"/save.docx": CHANGED}) as origin, _bind_internal(monkeypatch, origin.url):
        payload = _payload(session, origin.url + "/save.docx", final=True)
        response = _post(http, session, payload)
    return response, payload


def _copy_outcome(http, data, broker, session, path):
    response = _status(http, session["session_id"])
    assert response.status_code == 200
    status = response.json()
    assert status["state"] == "closed"
    assert status["reason"] is None
    assert status["document_key"] == session["document_key"]
    assert status["file_id"] != session["file_id"]
    assert status["saved_as"] == {"file_id": status["file_id"], "path": path}
    assert status["last_published_seq"] == status["last_committed_seq"] == status["save_seq"]
    copied = _outputs(data) / path
    assert copied.read_bytes() == CHANGED
    blob = _versions(data) / _sha(CHANGED)
    assert blob.read_bytes() == CHANGED
    assert (copied.stat().st_dev, copied.stat().st_ino) != (blob.stat().st_dev, blob.stat().st_ino)
    state = _read(data)
    new = state["documents"][status["file_id"]]
    assert new["path"] == path
    assert new["published_version"] == 1
    assert len(new["versions"]) == 1
    version = new["versions"][0]
    assert {key: version[key] for key in ("number", "parent", "source", "published", "sha256", "size")} == {
        "number": 1, "parent": None, "source": "conflict", "published": True,
        "sha256": _sha(CHANGED), "size": len(CHANGED),
    }
    source = state["documents"][session["file_id"]]
    assert source["versions"][-1]["published"] is False
    assert source["published_version"] == 1
    assert state["journal"] == {}
    record = state["sessions"][session["session_id"]]
    assert "last_checked_size" not in record
    assert "last_checked_mtime_ns" not in record
    assert record["workspace_changed"] is False
    listing = broker.OutputsBroker().reconcile(CHAT)
    entries = {entry["path"]: entry for entry in listing["entries"]}
    assert entries[path]["file_id"] == status["file_id"]
    revision = listing["revision"]
    assert broker.OutputsBroker().reconcile(CHAT)["revision"] == revision
    return state, status


@pytest.mark.parametrize("status4", (False, True), ids=("status2", "status4"))
@pytest.mark.parametrize("tombstoned", (False, True), ids=("active-index", "tombstoned"))
def test_final_missing_copy_conserves_source_receipt_key_and_replay(office_world, monkeypatch, status4, tombstoned):
    http, data, _origin, _manager, broker, _body, session = (
        _autosaved(office_world, monkeypatch) if status4 else _opened(office_world)
    )
    before = _read(data)
    _status(http, session["session_id"])
    (_outputs(data) / "report.docx").unlink()
    if tombstoned:
        broker.OutputsBroker().reconcile(CHAT)
    revision = broker.OutputsBroker().current_revision(CHAT)
    if status4:
        payload = recorded_status_4_payload(document_key=session["document_key"])
        response = _post(http, session, payload)
    else:
        response, payload = _final(http, session, monkeypatch)
    assert response.status_code == 200
    assert response.json() == {"error": 0}
    assert broker.OutputsBroker().current_revision(CHAT) == revision + 1
    state, status = _copy_outcome(http, data, broker, session, "report (2).docx")
    old = state["documents"][session["file_id"]]
    if status4:
        assert old == before["documents"][session["file_id"]]
        assert state["receipts"][session["session_id"]][str(status["save_seq"])] == {
            "status": 4, "sha256": None, "version": None, "answer": {"error": 0},
        }
    else:
        assert old["versions"][0] == before["documents"][session["file_id"]]["versions"][0]
        assert old["versions"][-1]["source"] == "close"
        assert state["receipts"][session["session_id"]]["1"] == {
            "status": 2, "sha256": _sha(CHANGED), "version": 2, "answer": {"error": 0},
        }
    assert not (_outputs(data) / "report.docx").exists()
    frozen = _snapshot(data)
    assert _post(http, session, payload).json() == {"error": 0}
    assert _snapshot(data) == frozen


@pytest.mark.parametrize("collision", ("file", "symlink", "stale-index", "unlocked-writer"))
def test_final_copy_skips_occupied_or_indexed_numbered_name(office_world, monkeypatch, collision):
    http, data, _origin, _manager, broker, _body, session = _opened(office_world)
    root = _outputs(data)
    first = root / "report (2).docx"
    outside = data.parent / "outside-copy"
    outside.write_bytes(b"foreign")
    if collision == "symlink":
        first.symlink_to(outside)
    elif collision in ("file", "stale-index"):
        first.write_bytes(b"foreign")
        if collision == "stale-index":
            broker.OutputsBroker().reconcile(CHAT)
            first.unlink()
    else:
        original = os.link
        won = []
        def competing(source, destination, *args, **kwargs):
            if destination == "report (2).docx" and not won:
                won.append(True)
                first.write_bytes(b"foreign")
            return original(source, destination, *args, **kwargs)
        monkeypatch.setattr(os, "link", competing)
    (root / "report.docx").unlink()
    response, _payload_value = _final(http, session, monkeypatch)
    assert response.status_code == 200
    _copy_outcome(http, data, broker, session, "report (3).docx")
    assert outside.read_bytes() == b"foreign"
    if collision == "symlink":
        assert first.is_symlink()
    elif collision == "stale-index":
        assert not first.exists()
    else:
        assert first.read_bytes() == b"foreign"
    assert not (root / "report (2) (2).docx").exists()


@pytest.mark.parametrize("name,removed", (("nested/report.docx", False), ("nested/report.docx", True), ("one/two/report.docx", True)))
def test_final_nested_missing_uses_existing_parent_or_workspace_root(office_world, monkeypatch, name, removed):
    http, data, _origin, _manager, broker, _body, session = _nested(office_world, name)
    target = _outputs(data) / name
    target.unlink()
    if removed:
        shutil.rmtree(_outputs(data) / name.split("/")[0])
    response, _payload_value = _final(http, session, monkeypatch)
    assert response.status_code == 200
    path = "report (2).docx" if removed else "nested/report (2).docx"
    _copy_outcome(http, data, broker, session, path)
    assert not target.exists()
    if removed:
        assert not (_outputs(data) / name.split("/")[0]).exists()


@pytest.mark.parametrize("name,depth", (("nested/report.docx", 0), ("one/two/report.docx", 0), ("one/two/report.docx", 1)))
@pytest.mark.parametrize("final", (False, True), ids=("ordinary-save", "final"))
def test_only_final_parent_symlink_copies_at_root_without_refresh(office_world, monkeypatch, name, depth, final):
    http, data, _origin, _manager, broker, body, session = _nested(office_world, name)
    if not final:
        _allocate(http, session, monkeypatch)
    root = _outputs(data)
    parts = name.split("/")
    parent = root.joinpath(*parts[:depth + 1])
    detached = data.parent / "detached-parent"
    parent.rename(detached)
    outside = data.parent / "foreign-parent"
    outside.mkdir()
    (outside / "report.docx").write_bytes(b"do-not-touch")
    parent.symlink_to(outside, target_is_directory=True)
    with _content_origin({"/save.docx": CHANGED}) as origin, _bind_internal(monkeypatch, origin.url):
        response = _post(http, session, _payload(session, origin.url + "/save.docx", final))
    assert response.status_code == 200
    assert (outside / "report.docx").read_bytes() == b"do-not-touch"
    assert parent.is_symlink()
    if final:
        _copy_outcome(http, data, broker, session, "report (2).docx")
    else:
        status = _status(http, session["session_id"]).json()
        assert (status["state"], status["reason"], status["saved_as"]) == ("conflict", "baseline_mismatch", None)
        assert not (root / "report (2).docx").exists()
    assert detached.joinpath(*parts[depth + 1:]).read_bytes() == body


@pytest.mark.parametrize("change", ("leaf-symlink", "changed"))
def test_final_existing_conflict_is_offered_on_next_create_without_new_editor(office_world, monkeypatch, change):
    http, data, _origin, _manager, broker, body, session = _opened(office_world)
    target = _outputs(data) / "report.docx"
    outside = data.parent / "foreign-leaf.docx"
    outside.write_bytes(body)
    if change == "leaf-symlink":
        target.unlink()
        target.symlink_to(outside)
    else:
        target.write_bytes(CHANGED)
    response, _payload_value = _final(http, session, monkeypatch)
    assert response.status_code == 200
    status = _status(http, session["session_id"]).json()
    assert (status["state"], status["reason"], status["saved_as"]) == ("conflict", "baseline_mismatch", None)
    assert not (_outputs(data) / "report (2).docx").exists()
    assert outside.read_bytes() == body
    if change == "changed":
        before = _read(data)
        joined = _create(http, session["file_id"])
        assert joined.status_code == 200
        assert joined.json()["session_id"] == session["session_id"]
        assert joined.json()["editor_config"] is None
        assert joined.json()["state"] == "conflict"
        assert _read(data) == before
        source = r'''
import json, os
from types import SimpleNamespace
from docker.errors import NotFound
os.environ["BASE_DATA_DIR"] = os.environ["OCU_BASE"]
import docker_manager
def missing(identity):
    raise NotFound(identity)
docker_manager._docker_client = SimpleNamespace(containers=SimpleNamespace(get=missing))
from fastapi.testclient import TestClient
from app import app
with TestClient(app, raise_server_exceptions=False) as client:
    response = client.post(
        f"/api/office/{os.environ['OCU_CHAT']}/documents/{os.environ['COPY_FILE']}/sessions",
        headers={"Authorization": "Bearer " + os.environ["OCU_INTERNAL_TOKEN"]},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["session_id"] == os.environ["COPY_SESSION"]
    assert result["state"] == "conflict" and result["editor_config"] is None
'''
        fresh = subprocess.run([sys.executable, "-c", source],
                               env=_child_env(data, COPY_FILE=session["file_id"], COPY_SESSION=session["session_id"]),
                               capture_output=True, text=True, timeout=15)
        assert fresh.returncode == 0, (fresh.stdout, fresh.stderr)
        assert _read(data) == before


@pytest.mark.parametrize("status4", (False, True), ids=("status2", "status4"))
@pytest.mark.parametrize("unsafe", (False, True), ids=("missing-root", "unsafe-root"))
def test_final_unusable_workspace_creates_nothing_and_retains_content(office_world, monkeypatch, unsafe, status4):
    http, data, _origin, _manager, broker, body, session = (
        _autosaved(office_world, monkeypatch) if status4 else _opened(office_world)
    )
    root = _outputs(data)
    detached = data.parent / "detached-root"
    root.rename(detached)
    outside = data.parent / "outside-root"
    outside.mkdir()
    (outside / "report.docx").write_bytes(b"foreign")
    if unsafe:
        root.symlink_to(outside, target_is_directory=True)
    if status4:
        response = _post(http, session, recorded_status_4_payload(document_key=session["document_key"]))
    else:
        response, _payload_value = _final(http, session, monkeypatch)
    assert response.status_code == 200
    status = _status(http, session["session_id"]).json()
    assert (status["state"], status["reason"]) == ("error", "unsafe_path" if unsafe else "workspace_missing")
    assert status["saved_as"] is None
    assert status["file_id"] == session["file_id"]
    assert _snapshot(outside) == {"report.docx": b"foreign"}
    assert detached.joinpath("report.docx").read_bytes() == body
    assert root.is_symlink() if unsafe else not root.exists()
    state = _read(data)
    assert state["documents"][session["file_id"]]["versions"][-1]["published"] is False
    assert state["journal"] == {}


@pytest.mark.parametrize("cut", ("claim-before-return", "registration-before-return", "office-before-replace", "office-after-replace"))
def test_copy_fault_recovers_same_inode_identity_and_single_registration(office_world, monkeypatch, cut):
    http, data, _origin, _manager, broker, _body, session = _opened(office_world)
    (_outputs(data) / "report.docx").unlink()
    revision = broker.OutputsBroker().current_revision(CHAT)
    real_link, real_replace = os.link, os.replace
    stopped = []
    def linked(source, destination, *args, **kwargs):
        answer = real_link(source, destination, *args, **kwargs)
        if cut == "claim-before-return" and destination == "report (2).docx" and not stopped:
            stopped.append(True)
            raise OSError(errno.EIO, "claim return interrupted")
        return answer
    def replaced(source, destination, *args, **kwargs):
        successor = None
        if destination == "state.json":
            fd = os.open(source, os.O_RDONLY, dir_fd=kwargs.get("src_dir_fd"))
            try:
                successor = json.loads(os.read(fd, 1024 * 1024))
            finally:
                os.close(fd)
        completion = successor is not None and successor["sessions"][session["session_id"]].get("saved_as") is not None
        trigger = (cut == "registration-before-return" and destination == "index.json") or (
            cut.startswith("office-") and completion
        )
        if trigger and not stopped and cut == "office-before-replace":
            stopped.append(True)
            raise OSError(errno.EIO, "Office successor interrupted")
        answer = real_replace(source, destination, *args, **kwargs)
        if trigger and not stopped:
            stopped.append(True)
            raise OSError(errno.EIO, "visible successor interrupted")
        return answer
    with monkeypatch.context() as boundary:
        boundary.setattr(os, "link", linked)
        boundary.setattr(os, "replace", replaced)
        response, payload = _final(http, session, boundary)
    assert response.status_code == 500
    assert stopped == [True]
    copied = _outputs(data) / "report (2).docx"
    inode = copied.stat().st_ino
    state_before = _read(data)
    source_before = state_before["documents"][session["file_id"]]
    receipts_before = state_before["receipts"]
    if cut != "office-after-replace":
        assert state_before["sessions"][session["session_id"]]["file_id"] == session["file_id"]
        assert state_before["journal"]
    from office.publish import recover_publications
    recover_publications(CHAT)
    assert broker.OutputsBroker().current_revision(CHAT) == revision + 1
    assert copied.stat().st_ino == inode
    state, _status_value = _copy_outcome(http, data, broker, session, "report (2).docx")
    assert state["documents"][session["file_id"]] == source_before
    assert state["receipts"] == receipts_before
    frozen = _snapshot(data)
    recover_publications(CHAT)
    assert _post(http, session, payload).json() == {"error": 0}
    assert _snapshot(data) == frozen


def test_copy_foreign_same_hash_substitution_is_never_adopted(office_world, monkeypatch):
    http, data, _origin, _manager, _broker, _body, session = _opened(office_world)
    (_outputs(data) / "report.docx").unlink()
    original = os.replace
    def stop(source, destination, *args, **kwargs):
        if destination == "index.json":
            raise OSError(errno.EIO, "registration refused")
        return original(source, destination, *args, **kwargs)
    with monkeypatch.context() as boundary:
        boundary.setattr(os, "replace", stop)
        response, _payload_value = _final(http, session, boundary)
    assert response.status_code == 500
    copy = _outputs(data) / "report (2).docx"
    foreign = data.parent / "foreign-samehash"
    foreign.write_bytes(CHANGED)
    foreign.replace(copy)
    inode = copy.stat().st_ino
    before = _read(data)
    from office.publish import RecoveryRequiredError, recover_publications
    with pytest.raises(RecoveryRequiredError):
        recover_publications(CHAT)
    assert copy.read_bytes() == CHANGED
    assert copy.stat().st_ino == inode
    assert _read(data) == before
    assert not (_outputs(data) / "report (3).docx").exists()


@pytest.mark.parametrize("external", (False, True), ids=("running", "external-pause"))
def test_final_copy_uses_owned_fence_and_preserves_external_pause(office_world, monkeypatch, external):
    http, data, _origin, manager, broker, _body, session = _opened(office_world)
    container = _running(manager)
    if external:
        container.status = "paused"
        container.attrs["State"].update(Status="paused", Paused=True)
    (_outputs(data) / "report.docx").unlink()
    response, _payload_value = _final(http, session, monkeypatch)
    assert response.status_code == 200
    _copy_outcome(http, data, broker, session, "report (2).docx")
    if external:
        container.pause.assert_not_called()
        container.unpause.assert_not_called()
        assert container.status == "paused"
    else:
        container.pause.assert_called_once()
        container.unpause.assert_called_once()
        assert container.status == "running"
    assert not (data / CHAT / ".ocu" / "office" / "fence.json").exists()

@pytest.mark.parametrize("mutation", (
    "file-deleted",
    "parent-removed",
    "parent-symlink",
    "outputs-removed",
    "nested-destination-removed",
))
def test_final_copy_follows_pause_settled_workspace(office_world, monkeypatch, mutation):
    nested = mutation in ("parent-removed", "parent-symlink", "nested-destination-removed")
    http, data, _origin, manager, broker, body, session = (
        _nested(office_world, "nested/report.docx") if nested else _opened(office_world)
    )
    container = _running(manager)
    root = _outputs(data)
    target = root / ("nested/report.docx" if nested else "report.docx")
    pause = container.pause.side_effect
    outside = data.parent / "pause-settled-parent"
    detached = data.parent / "pause-settled-detached"
    def settle():
        if mutation == "file-deleted":
            target.unlink()
        elif mutation == "parent-removed":
            shutil.rmtree(root / "nested")
        elif mutation == "parent-symlink":
            (root / "nested").rename(detached)
            outside.mkdir()
            (outside / "report.docx").write_bytes(b"do-not-touch")
            (root / "nested").symlink_to(outside, target_is_directory=True)
        elif mutation == "outputs-removed":
            shutil.rmtree(root)
        else:
            from office.store import OfficeStore
            def preplan(state):
                live = next(iter(state["journal"].values()))
                live.update(
                    copy={"schema_version": 1, "destination": "nested", "basename": "report.docx",
                          "initial_revision": 0},
                    temporary_name=".office-publish." + ("0" * 32) + ".tmp",
                )
                live.pop("target_path", None)
                live.pop("staging", None)
            OfficeStore().update(CHAT, preplan)
            shutil.rmtree(root / "nested")
        pause()

    container.pause.side_effect = settle
    response, _payload_value = _final(http, session, monkeypatch)
    assert response.status_code == 200
    assert response.json() == {"error": 0}
    container.pause.assert_called_once()
    container.unpause.assert_called_once()
    assert container.status == "running"
    if mutation == "outputs-removed":
        status = _status(http, session["session_id"]).json()
        assert (status["state"], status["reason"], status["saved_as"]) == (
            "error", "workspace_missing", None,
        )
        assert status["file_id"] == session["file_id"]
        assert not root.exists()
        assert _read(data)["journal"] == {}
        assert _read(data)["documents"][session["file_id"]]["versions"][-1]["published"] is False
        return
    _copy_outcome(http, data, broker, session, "report (2).docx")
    if mutation == "file-deleted":
        assert not target.exists()
    elif mutation == "parent-removed":
        assert not (root / "nested").exists()
    elif mutation == "parent-symlink":
        assert (root / "nested").is_symlink()
        assert (outside / "report.docx").read_bytes() == b"do-not-touch"
        assert detached.joinpath("report.docx").read_bytes() == body
    else:
        assert not (root / "nested").exists()



_COPY_WORKER = r'''
import json, os, time
from pathlib import Path
from types import SimpleNamespace
from docker.errors import NotFound
os.environ["BASE_DATA_DIR"] = os.environ["OCU_BASE"]
import docker_manager
from office.publish import recover_publications
def missing(identity):
    raise NotFound(identity)
docker_manager._docker_client = SimpleNamespace(containers=SimpleNamespace(get=missing))
cut = os.environ.get("COPY_CUT", "")
marker = Path(os.environ["COPY_MARKER"])
def stop(point):
    if point == cut:
        marker.write_text(point)
        while True:
            time.sleep(0.02)
real_open, real_link, real_replace, real_unlink, real_sync = os.open, os.link, os.replace, os.unlink, os.fsync
staged = {}
def opened(name, flags, *args, **kwargs):
    anchor = isinstance(name, str) and name.startswith(".office-publish.") and flags & os.O_CREAT
    if anchor:
        stop("anchor-before")
    fd = real_open(name, flags, *args, **kwargs)
    if anchor:
        info = os.fstat(fd)
        staged[fd] = (info.st_dev, info.st_ino)
        stop("anchor-after")
    return fd
def linked(source, destination, *args, **kwargs):
    point = "claim" if destination == "report (2).docx" else (
        "witness" if isinstance(destination, str) and destination.startswith(".publish-owner.") else ""
    )
    stop(point + "-before")
    answer = real_link(source, destination, *args, **kwargs)
    stop(point + "-after")
    return answer
def replaced(source, destination, *args, **kwargs):
    point = ""
    if destination == "index.json":
        point = "registration"
    elif destination == "state.json":
        fd = real_open(source, os.O_RDONLY, dir_fd=kwargs.get("src_dir_fd"))
        try:
            successor = json.loads(os.read(fd, 1024 * 1024))
        finally:
            os.close(fd)
        session = successor["sessions"][os.environ["COPY_SESSION"]]
        if session.get("saved_as"):
            point = "completion"
        else:
            entries = list(successor["journal"].values())
            if entries and "copy" in entries[0]:
                binding = entries[0]["copy"]
                if "registered_file_id" in binding:
                    point = "registration-journal"
                elif "claimed_name" in binding:
                    point = "claim-journal"
                elif "attempt" in binding:
                    point = "attempt"
                elif "staging" in entries[0]:
                    point = "binding"
                else:
                    point = "intent"
    stop(point + "-before")
    answer = real_replace(source, destination, *args, **kwargs)
    stop(point + "-after")
    return answer
def unlinked(name, *args, **kwargs):
    point = "anchor-cleanup" if isinstance(name, str) and name.startswith(".office-publish.") else (
        "witness-cleanup" if isinstance(name, str) and name.startswith(".publish-owner.") else ""
    )
    stop(point + "-before")
    answer = real_unlink(name, *args, **kwargs)
    stop(point + "-after")
    return answer
def synced(fd):
    if fd in staged:
        try:
            info = os.fstat(fd)
            complete = info.st_size > 0 and (info.st_dev, info.st_ino) == staged[fd]
        except OSError:
            complete = False
        if complete:
            stop("content-durability-before")
            answer = real_sync(fd)
            stop("content-durability-after")
            staged.pop(fd, None)
            return answer
    return real_sync(fd)
os.open, os.link, os.replace, os.unlink, os.fsync = opened, linked, replaced, unlinked, synced
recover_publications(os.environ["OCU_CHAT"])
print("completed", flush=True)
'''


@pytest.mark.parametrize("cut", [
    f"{point}-{side}"
    for point in ("intent", "anchor", "witness", "binding", "content-durability",
                  "claim", "attempt", "claim-journal", "registration", "registration-journal",
                  "completion", "anchor-cleanup", "witness-cleanup")
    for side in ("before", "after")
])
def test_killed_final_copy_converges_in_fresh_worker_without_duplicate_inode_or_revision(office_world, monkeypatch, cut):
    from tests.orchestrator.test_office_callback_publish import _surviving

    http, data, _origin, _manager, broker, _body, session = _opened(office_world)
    (_outputs(data) / "report.docx").unlink()
    revision = broker.OutputsBroker().current_revision(CHAT)
    with _content_origin({"/save.docx": CHANGED}) as origin, _bind_internal(monkeypatch, origin.url):
        payload = _surviving(http, data, session, monkeypatch, origin, final=True)
    marker = data.parent / "copy-crash-marker"
    env = _child_env(data, COPY_CUT=cut, COPY_MARKER=str(marker), COPY_SESSION=session["session_id"])
    child = subprocess.Popen([sys.executable, "-c", _COPY_WORKER], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 15
        while not marker.exists():
            if child.poll() is not None or time.monotonic() >= deadline:
                if child.poll() is None:
                    child.kill()
                out, err = child.communicate(timeout=5)
                pytest.fail(f"copy cut {cut} not reached: {out} {err}")
            time.sleep(0.01)
        assert marker.read_text() == cut
        child.kill()
        child.communicate(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)
    copy = _outputs(data) / "report (2).docx"
    inode = copy.stat().st_ino if copy.exists() else None
    state_before = _read(data)
    source_before = state_before["documents"][session["file_id"]]
    receipts_before = state_before["receipts"]
    fresh = subprocess.run([sys.executable, "-c", _COPY_WORKER],
                           env={**env, "COPY_CUT": ""}, capture_output=True, text=True, timeout=15)
    assert fresh.returncode == 0, (fresh.stdout, fresh.stderr)
    assert fresh.stdout.strip() == "completed"
    assert broker.OutputsBroker().current_revision(CHAT) == revision + 1
    if inode is not None:
        assert copy.stat().st_ino == inode
    state, _status_value = _copy_outcome(http, data, broker, session, "report (2).docx")
    assert state["documents"][session["file_id"]] == source_before
    assert state["receipts"] == receipts_before
    assert sorted(path.name for path in _outputs(data).iterdir()) == ["report (2).docx"]
    frozen = _snapshot(data)
    assert _post(http, session, payload).json() == {"error": 0}
    assert _snapshot(data) == frozen


@pytest.mark.parametrize("after_claim", (False, True), ids=("before-claim", "after-claim"))
def test_copy_budget_preserves_preclaim_failure_and_postclaim_recovery(office_world, monkeypatch, after_claim):
    http, data, _origin, manager, broker, _body, session = _opened(office_world)
    container = _running(manager)
    (_outputs(data) / "report.docx").unlink()
    revision = broker.OutputsBroker().current_revision(CHAT)
    real_link, real_replace, monotonic = os.link, os.replace, time.monotonic
    elapsed = [0.0]
    def linked(source, destination, *args, **kwargs):
        answer = real_link(source, destination, *args, **kwargs)
        if after_claim and destination == "report (2).docx":
            elapsed[0] = 6.0
        return answer
    def replaced(source, destination, *args, **kwargs):
        answer = real_replace(source, destination, *args, **kwargs)
        if not after_claim and destination == "state.json":
            state = _read(data)
            if any("attempt" in entry.get("copy", {}) for entry in state["journal"].values()):
                elapsed[0] = 6.0
        return answer
    with monkeypatch.context() as boundary:
        boundary.setattr(os, "link", linked)
        boundary.setattr(os, "replace", replaced)
        boundary.setattr(time, "monotonic", lambda: monotonic() + elapsed[0])
        response, payload = _final(http, session, boundary)
    assert response.status_code == 200
    state = _read(data)
    assert bool(state["journal"]) is after_claim
    record = state["sessions"][session["session_id"]]
    assert record["file_id"] == session["file_id"]
    assert record["last_published_seq"] == 0
    assert broker.OutputsBroker().current_revision(CHAT) == revision
    copy = _outputs(data) / "report (2).docx"
    assert copy.exists() is after_claim
    inode = copy.stat().st_ino if after_claim else None
    assert container.status == "running"
    assert container.unpause.call_count == 1
    from office.publish import recover_publications
    recover_publications(CHAT)
    if after_claim:
        assert copy.stat().st_ino == inode
        assert broker.OutputsBroker().current_revision(CHAT) == revision + 1
        _copy_outcome(http, data, broker, session, "report (2).docx")
    else:
        assert (record["state"], record["reason"]) == ("error", "publish_timeout")
        assert state["documents"][session["file_id"]]["versions"][-1]["published"] is False
        assert not copy.exists()
        assert broker.OutputsBroker().current_revision(CHAT) == revision
    frozen = _snapshot(data)
    assert _post(http, session, payload).json() == {"error": 0}
    assert _snapshot(data) == frozen


def test_copy_office_successor_durability_error_preserves_visible_completed_identity(office_world, monkeypatch):
    http, data, _origin, _manager, broker, _body, session = _opened(office_world)
    (_outputs(data) / "report.docx").unlink()
    revision = broker.OutputsBroker().current_revision(CHAT)
    directory = data / CHAT / ".ocu" / "office"
    directory_id = (directory.stat().st_dev, directory.stat().st_ino)
    real_sync = os.fsync
    stopped = []
    def synced(fd):
        real_sync(fd)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == directory_id and not stopped:
            state = _read(data)
            if state["sessions"][session["session_id"]].get("saved_as") is not None:
                stopped.append(True)
                raise OSError(errno.EIO, "Office completion directory sync refused")
    with monkeypatch.context() as boundary:
        boundary.setattr(os, "fsync", synced)
        response, payload = _final(http, session, boundary)
    _assert_refusal(response, 500, "state_durability")
    assert stopped == [True]
    assert broker.OutputsBroker().current_revision(CHAT) == revision + 1
    state, _status_value = _copy_outcome(http, data, broker, session, "report (2).docx")
    assert state["journal"] == {}
    frozen = _snapshot(data)
    from office.publish import recover_publications
    recover_publications(CHAT)
    assert _post(http, session, payload).json() == {"error": 0}
    assert _snapshot(data) == frozen


def test_copy_revalidates_destination_when_parent_changes_after_private_write(office_world, monkeypatch):
    from tests.orchestrator.test_office_publish import _on_staging_sync
    http, data, _origin, _manager, _broker, _body, session = _nested(office_world, "nested/report.docx")
    parent = _outputs(data) / "nested"
    (parent / "report.docx").unlink()
    outside = data.parent / "late-parent-target"
    outside.mkdir()
    (outside / "report.docx").write_bytes(b"foreign")
    detached = data.parent / "late-parent-detached"
    def substitute(_fd):
        parent.rename(detached)
        parent.symlink_to(outside, target_is_directory=True)
    with monkeypatch.context() as boundary:
        _on_staging_sync(boundary, substitute)
        response, _payload_value = _final(http, session, boundary)
    _assert_refusal(response, 500, "state_corrupt")
    assert _snapshot(outside) == {"report.docx": b"foreign"}
    assert not (detached / "report (2).docx").exists()
    assert _read(data)["journal"]


def test_final_copy_in_stopped_sandbox_never_starts_or_pauses_it(office_world, monkeypatch):
    from tests.orchestrator.test_lifecycle import _container, _docker
    http, data, _origin, manager, broker, _body, session = _opened(office_world)
    container = _container(manager._container_name(CHAT), status="exited")
    engine = _docker([container])
    manager._docker_client = engine
    (_outputs(data) / "report.docx").unlink()
    response, _payload_value = _final(http, session, monkeypatch)
    assert response.status_code == 200
    _copy_outcome(http, data, broker, session, "report (2).docx")
    container.pause.assert_not_called()
    container.unpause.assert_not_called()
    container.start.assert_not_called()
    engine.containers.create.assert_not_called()
    assert container.status == "exited"


def test_final_copy_release_failure_keeps_completed_successor_and_owned_marker(office_world, monkeypatch):
    http, data, _origin, manager, broker, _body, session = _opened(office_world)
    container = _running(manager)
    container.unpause.side_effect = OSError(errno.EIO, "release refused")
    (_outputs(data) / "report.docx").unlink()
    response, _payload_value = _final(http, session, monkeypatch)
    assert response.status_code == 200
    _copy_outcome(http, data, broker, session, "report (2).docx")
    assert container.status == "paused"
    container.unpause.assert_called_once()
    assert (data / CHAT / ".ocu" / "office" / "fence.json").exists()
