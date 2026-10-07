# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Final copy outcomes through authenticated HTTP, durable Office state and broker."""
from __future__ import annotations

import errno
import json
import os
import shutil
import socket
import subprocess
import threading
import sys
import time

import pytest
import httpx

from tests.orchestrator._office_recorded_callbacks import recorded_status_1_payload, recorded_status_4_payload
from tests.orchestrator._office_store import SERVER_DIR, _child_env, _stop_child, _wait_marker
from tests.orchestrator.test_office_callback_publish import (
    CHANGED, _allocate, _autosaved, _bind_internal, _content_origin, _opened,
    _payload, _post, _read, _running, _sha,
)
from tests.orchestrator.test_office_control_plane import _open_session
from tests.orchestrator.test_office_session_lifecycle import _status
from tests.orchestrator.test_office_sessions import (
    _assert_refusal, _create, _index_file, _office, _outputs, _put, _snapshot, _versions, office_world,
)
from tests.orchestrator.test_outputs_endpoint import CHAT, INTERNAL, _auth
from tests.orchestrator.test_office_router import _raw_office


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


EDITED = CHANGED + b"newer-workspace-edit"


def _assert_private_reclaimed(data):
    private = _office(data) / "staging"
    assert not [path for path in private.iterdir() if path.is_file() and path.read_bytes() == CHANGED]
    assert (_versions(data) / _sha(CHANGED)).read_bytes() == CHANGED


def _interrupt_claim(monkeypatch, save_as):
    claim = save_as.claim_file_no_replace

    def claimed_then_interrupted(*args, **kwargs):
        _claimed = claim(*args, **kwargs)
        raise OSError("interrupted after actual no-replace claim")

    monkeypatch.setattr(save_as, "claim_file_no_replace", claimed_then_interrupted)


def _interrupt_helper_fallback(monkeypatch, save_as, occupied):
    claim = save_as.claim_file_no_replace

    def claimed_then_interrupted(temporary, *, src_dir_fd, dst_dir_fd, requested_name):
        occupied.write_bytes(b"foreign")
        _claimed = claim(temporary, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd, requested_name=requested_name)
        raise OSError("interrupted after helper fallback claim")

    monkeypatch.setattr(save_as, "claim_file_no_replace", claimed_then_interrupted)


def _interrupt_prepare_allocate(monkeypatch, publish_mod):
    allocate = publish_mod._Stage.allocate

    def allocated(self, *args, **kwargs):
        _identity = allocate(self, *args, **kwargs)
        raise OSError("interrupted after real prepared allocation")

    monkeypatch.setattr(publish_mod._Stage, "allocate", allocated)
    monkeypatch.setattr(publish_mod._Stage, "cleanup", lambda *a, **k: None)


def _final_fault(http, session, monkeypatch, *, content=CHANGED):
    with _content_origin({"/save.docx": content}) as origin, _bind_internal(monkeypatch, origin.url):
        try:
            response = _post(http, session, _payload(session, origin.url + "/save.docx", True))
        except OSError:
            return None
        return response


def _recover(recovery, http, session, monkeypatch):
    if recovery == "direct":
        from office.publish import recover_publications
        recover_publications(CHAT)
        return None
    if recovery == "files":
        return http.get(f"/api/outputs/{CHAT}", headers=_auth())
    if recovery == "files-first":
        status, body = _raw_office(
            http.app, f"/api/outputs/{CHAT}", [(b"authorization", f"Bearer {INTERNAL}".encode())],
        )
        return status, body
    if recovery == "callback":
        with _content_origin({"/save.docx": CHANGED}) as origin, _bind_internal(monkeypatch, origin.url):
            return _post(http, session, _payload(session, origin.url + "/save.docx", True))
    from office.sweep import sweep_office_publications
    sweep_office_publications()
    return None


def _refused_listing(listed, data, broker, revision, index_before, session, path, body):
    if isinstance(listed, tuple):
        status, payload = listed
        assert status == 503
        assert "outputs listing failed" in str(payload.get("detail", payload))
    else:
        assert listed.status_code == 503
        assert listed.headers["Retry-After"] == "1"
        assert "outputs listing failed" in str(listed.json()["detail"])
    state = _read(data)
    assert state["journal"]
    assert state["sessions"][session["session_id"]]["file_id"] == session["file_id"]
    assert path.read_bytes() == body
    assert (data / CHAT / ".ocu" / "index.json").read_bytes() == index_before
    assert broker.OutputsBroker().current_revision(CHAT) == revision




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
        _assert_private_reclaimed(data)
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


def test_files_lists_new_identity_after_interrupted_equal_content_claim(office_world, monkeypatch):
    from office import save_as
    from office.store import OfficeStore

    http, data, _origin, _manager, _broker, original, session = _opened(office_world)
    (_outputs(data) / "report.docx").unlink()
    claim = save_as.claim_file_no_replace

    def claimed_then_interrupted(*args, **kwargs):
        claim(*args, **kwargs)
        raise OSError("interrupted after actual no-replace claim")

    with monkeypatch.context() as cut:
        cut.setattr(save_as, "claim_file_no_replace", claimed_then_interrupted)
        with _content_origin({"/save.docx": original}) as server, _bind_internal(cut, server.url):
            try:
                response = _post(http, session, _payload(session, server.url + "/save.docx", True))
            except OSError as error:
                assert str(error) == "interrupted after actual no-replace claim"
            else:
                assert response.status_code >= 500

    copied = _outputs(data) / "report (2).docx"
    assert copied.read_bytes() == original
    before = OfficeStore().read(CHAT)
    assert len(before["journal"]) == 1
    staging = next(iter(before["journal"].values()))["staging"]
    private = _office(data) / "staging"
    witness = private / staging["witness_name"]
    assert (witness.stat().st_dev, witness.stat().st_ino) == (copied.stat().st_dev, copied.stat().st_ino)
    assert (private / staging["anchor_name"]).read_bytes() == original
    listing = http.get(f"/api/outputs/{CHAT}", headers=_auth())
    assert listing.status_code == 200, listing.text
    listed = next(item for item in listing.json()["files"] if item["path"] == "report (2).docx")
    after = OfficeStore().read(CHAT)
    record = after["sessions"][session["session_id"]]
    assert listed["file_id"] != session["file_id"]
    assert record["state"] == "closed"
    assert record["file_id"] == listed["file_id"]
    assert record["saved_as"] == {"file_id": listed["file_id"], "path": listed["path"]}
    assert after["journal"] == {}
    assert after["documents"][session["file_id"]] == before["documents"][session["file_id"]]
    new = after["documents"][listed["file_id"]]
    assert new["path"] == "report (2).docx"
    assert len(new["versions"]) == 1
    assert new["versions"][0]["source"] == "conflict"
    assert new["versions"][0]["published"] is True
    assert sorted(path.name for path in _outputs(data).glob("report (*).docx")) == ["report (2).docx"]
    assert copied.read_bytes() == original


def test_files_lists_new_identity_after_interrupted_claim_matching_removed_history(
    office_world, monkeypatch,
):
    from office import save_as
    from office.store import OfficeStore

    http, data, _origin, _manager, broker, _original, session = _opened(office_world)
    notes_path = _put(data, "notes.docx", CHANGED)
    notes_id = _index_file(broker, data, "notes.docx")
    created = _create(http, notes_id)
    assert created.status_code == 201
    prior = OfficeStore().read(CHAT)
    notes_history = prior["documents"][notes_id]
    (_outputs(data) / "report.docx").unlink()
    notes_path.unlink()
    claim = save_as.claim_file_no_replace

    def claimed_then_interrupted(*args, **kwargs):
        claim(*args, **kwargs)
        raise OSError("interrupted after actual no-replace claim")

    with monkeypatch.context() as cut:
        cut.setattr(save_as, "claim_file_no_replace", claimed_then_interrupted)
        with _content_origin({"/save.docx": CHANGED}) as server, _bind_internal(cut, server.url):
            try:
                response = _post(http, session, _payload(session, server.url + "/save.docx", True))
            except OSError as error:
                assert str(error) == "interrupted after actual no-replace claim"
            else:
                assert response.status_code >= 500

    copied = _outputs(data) / "report (2).docx"
    assert copied.read_bytes() == CHANGED
    before = OfficeStore().read(CHAT)
    assert len(before["journal"]) == 1
    listing = http.get(f"/api/outputs/{CHAT}", headers=_auth())
    assert listing.status_code == 200, listing.text
    files = listing.json()["files"]
    listed = next(item for item in files if item["path"] == "report (2).docx")
    assert listed["file_id"] != session["file_id"]
    assert listed["file_id"] != notes_id
    assert all(item["file_id"] != notes_id for item in files)
    after = OfficeStore().read(CHAT)
    record = after["sessions"][session["session_id"]]
    assert record["state"] == "closed"
    assert record["file_id"] == listed["file_id"]
    assert record["saved_as"] == {"file_id": listed["file_id"], "path": listed["path"]}
    assert after["journal"] == {}
    assert after["documents"][session["file_id"]] == before["documents"][session["file_id"]]
    assert after["documents"][notes_id] == notes_history
    new = after["documents"][listed["file_id"]]
    assert new["path"] == "report (2).docx"
    assert len(new["versions"]) == 1
    assert new["versions"][0]["source"] == "conflict"
    assert new["versions"][0]["published"] is True
    persisted = json.loads((data / CHAT / ".ocu" / "index.json").read_text(encoding="utf-8"))
    assert notes_id in persisted["tombstones"]
    assert persisted["tombstones"][notes_id]["path"] == "notes.docx"
    assert persisted["tombstones"][notes_id]["hash"] == _sha(CHANGED)
    assert persisted["active"]["report (2).docx"]["file_id"] == listed["file_id"]
    assert sorted(path.name for path in _outputs(data).glob("*.docx")) == ["report (2).docx"]
    assert copied.read_bytes() == CHANGED


def test_files_first_lifespan_off_recovers_interrupted_equal_content_claim(office_world, monkeypatch):
    from office import save_as
    from office.store import OfficeStore

    http, data, _origin, _manager, _broker, original, session = _opened(office_world)
    (_outputs(data) / "report.docx").unlink()
    claim = save_as.claim_file_no_replace

    def claimed_then_interrupted(*args, **kwargs):
        claim(*args, **kwargs)
        raise OSError("interrupted after actual no-replace claim")

    with monkeypatch.context() as cut:
        cut.setattr(save_as, "claim_file_no_replace", claimed_then_interrupted)
        with _content_origin({"/save.docx": original}) as server, _bind_internal(cut, server.url):
            try:
                response = _post(http, session, _payload(session, server.url + "/save.docx", True))
            except OSError as error:
                assert str(error) == "interrupted after actual no-replace claim"
            else:
                assert response.status_code >= 500

    before = OfficeStore().read(CHAT)
    assert len(before["journal"]) == 1
    status, body = _raw_office(
        http.app,
        f"/api/outputs/{CHAT}",
        [(b"authorization", f"Bearer {INTERNAL}".encode())],
    )
    assert status == 200
    listed = next(item for item in body["files"] if item["path"] == "report (2).docx")
    after = OfficeStore().read(CHAT)
    record = after["sessions"][session["session_id"]]
    assert listed["file_id"] != session["file_id"]
    assert record["state"] == "closed"
    assert record["file_id"] == listed["file_id"]
    assert record["saved_as"] == {"file_id": listed["file_id"], "path": listed["path"]}
    assert after["journal"] == {}
    assert after["documents"][session["file_id"]] == before["documents"][session["file_id"]]


def test_files_lock_excludes_second_publisher_between_recovery_and_scan(office_world, monkeypatch):
    from office import save_as
    from office.store import OfficeStore
    import office.publish as publish_mod

    http, data, _origin, _manager, broker, original, session = _opened(office_world)
    (_outputs(data) / "report.docx").unlink()
    claim = save_as.claim_file_no_replace

    def claimed_then_interrupted(*args, **kwargs):
        claim(*args, **kwargs)
        raise OSError("interrupted after actual no-replace claim")

    with monkeypatch.context() as cut:
        cut.setattr(save_as, "claim_file_no_replace", claimed_then_interrupted)
        with _content_origin({"/save.docx": original}) as server, _bind_internal(cut, server.url):
            try:
                response = _post(http, session, _payload(session, server.url + "/save.docx", True))
            except OSError as error:
                assert str(error) == "interrupted after actual no-replace claim"
            else:
                assert response.status_code >= 500

    before = OfficeStore().read(CHAT)
    assert len(before["journal"]) == 1
    entered = data.parent / "files-recovery-entered"
    release = data.parent / "files-recovery-release"
    contended = data.parent / "files-publisher-contended"
    real_recover = publish_mod.recover_publications
    recovered_index = []

    def recover_then_hold(chat_id, now=None, *, allow_missing_office=False):
        real_recover(chat_id, now=now, allow_missing_office=allow_missing_office)
        recovered_index.append((data / CHAT / ".ocu" / "index.json").read_bytes())
        entered.write_text("1", encoding="utf-8")
        deadline = time.monotonic() + 10
        while not release.exists():
            if time.monotonic() >= deadline:
                raise AssertionError("Files recovery hold was not released")
            time.sleep(0.005)

    monkeypatch.setattr(publish_mod, "recover_publications", recover_then_hold)
    monkeypatch.setattr("app.recover_publications", recover_then_hold)
    outcome = {}

    def list_files():
        outcome["response"] = http.get(f"/api/outputs/{CHAT}", headers=_auth())

    holder = threading.Thread(target=list_files)
    child = None
    holder.start()
    try:
        deadline = time.monotonic() + 5
        while not entered.exists():
            if time.monotonic() >= deadline or not holder.is_alive():
                raise AssertionError(("Files did not hold after recovery", holder.is_alive(), outcome))
            time.sleep(0.005)
        assert holder.is_alive()
        assert recovered_index
        index_after_recovery = recovered_index[0]
        child = subprocess.Popen(
            [sys.executable, "-c", _FILES_PUBLISHER_CHILD],
            cwd=str(SERVER_DIR),
            env=_child_env(
                data,
                OCU_CONTENDED=str(contended),
                OCU_RELEASE=str(release),
                OCU_LOCK_DENIAL="publisher lock was granted between recovery and scan",
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _wait_marker(contended, child, "second publisher did not contend for the chat flock")
        assert child.poll() is None
        assert (data / CHAT / ".ocu" / "index.json").read_bytes() == index_after_recovery
        release.write_text("1", encoding="utf-8")
        holder.join(timeout=10)
        assert not holder.is_alive()
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, (stdout, stderr)
    finally:
        release.write_text("1", encoding="utf-8")
        _stop_child(child)
        holder.join(timeout=2)

    listed = outcome["response"]
    assert listed.status_code == 200, listed.text
    copy = next(item for item in listed.json()["files"] if item["path"] == "report (2).docx")
    after = OfficeStore().read(CHAT)
    assert after["journal"] == {}
    assert after["sessions"][session["session_id"]]["file_id"] == copy["file_id"]
    assert copy["file_id"] != session["file_id"]
    assert after["documents"][session["file_id"]] == before["documents"][session["file_id"]]


_FILES_PUBLISHER_CHILD = r'''
import fcntl
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ["OCU_SERVER_DIR"])
os.environ["BASE_DATA_DIR"] = os.environ["OCU_BASE"]
os.environ["DOCKER_HOST"] = "unix:///tmp/ocu-acceptance-no-docker.sock"
os.environ["DOCKER_SOCKET"] = "unix:///tmp/ocu-acceptance-no-docker.sock"

import docker_manager

original_flock = fcntl.flock

def contend_then_block(fd, operation):
    if operation == fcntl.LOCK_EX:
        try:
            original_flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            Path(os.environ["OCU_CONTENDED"]).write_text("1", encoding="utf-8")
            return original_flock(fd, operation)
        try:
            original_flock(fd, fcntl.LOCK_UN)
        finally:
            raise AssertionError(os.environ["OCU_LOCK_DENIAL"])
    return original_flock(fd, operation)

fcntl.flock = contend_then_block
with docker_manager._combined_lock(os.environ["OCU_CHAT"], create=False) as lock:
    assert lock is not None
print("acquired")
'''


def test_files_refuses_foreign_same_hash_substitution_without_index_write(office_world, monkeypatch):
    http, data, _origin, _manager, broker, _body, session = _opened(office_world)
    (_outputs(data) / "report.docx").unlink()
    original_replace = os.replace

    def stop(source, destination, *args, **kwargs):
        if destination == "index.json":
            raise OSError(errno.EIO, "registration refused")
        return original_replace(source, destination, *args, **kwargs)

    with monkeypatch.context() as boundary:
        boundary.setattr(os, "replace", stop)
        response, _payload_value = _final(http, session, boundary)
    assert response.status_code == 500
    copy = _outputs(data) / "report (2).docx"
    foreign = data.parent / "foreign-samehash"
    foreign.write_bytes(CHANGED)
    foreign.replace(copy)
    inode = copy.stat().st_ino
    index_before = (data / CHAT / ".ocu" / "index.json").read_bytes()
    revision = broker.OutputsBroker().current_revision(CHAT)
    listed = http.get(
        f"/api/outputs/{CHAT}",
        headers={**_auth(), "If-None-Match": "*"},
    )
    assert listed.status_code == 503
    assert listed.headers["Retry-After"] == "1"
    assert "outputs listing failed" in str(listed.json()["detail"])
    assert copy.read_bytes() == CHANGED
    assert copy.stat().st_ino == inode
    assert (data / CHAT / ".ocu" / "index.json").read_bytes() == index_before
    assert broker.OutputsBroker().current_revision(CHAT) == revision
    assert _read(data)["journal"]


@pytest.mark.parametrize("recovery", ("direct", "files", "files-first", "callback", "sweep"))
def test_moved_parent_after_unjournaled_claim_retains_owned_copy(office_world, monkeypatch, recovery):
    from office import save_as
    from office.publish import RecoveryRequiredError

    http, data, _origin, _manager, broker, _body, session = _nested(office_world, "nested/report.docx")
    target = _outputs(data) / "nested/report.docx"
    target.unlink()
    with monkeypatch.context() as cut:
        _interrupt_claim(cut, save_as)
        _final_fault(http, session, cut)
    root = _outputs(data)
    claimed = root / "nested/report (2).docx"
    inode = claimed.stat().st_ino
    (root / "nested").rename(root / "moved")
    moved = root / "moved/report (2).docx"
    index_before = (data / CHAT / ".ocu" / "index.json").read_bytes()
    revision = broker.OutputsBroker().current_revision(CHAT)
    if recovery == "direct":
        with pytest.raises(RecoveryRequiredError):
            _recover(recovery, http, session, monkeypatch)
        listed = None
    else:
        listed = _recover(recovery, http, session, monkeypatch)
        if recovery in ("files", "files-first"):
            _refused_listing(listed, data, broker, revision, index_before, session, moved, CHANGED)
        else:
            if listed is not None:
                assert listed.status_code == 200
            state = _read(data)
            assert state["journal"]
            assert state["sessions"][session["session_id"]]["file_id"] == session["file_id"]
    assert not (root / "report (2).docx").exists()
    assert moved.read_bytes() == CHANGED
    assert moved.stat().st_ino == inode
    assert _read(data)["journal"]
    assert (data / CHAT / ".ocu" / "index.json").read_bytes() == index_before


@pytest.mark.parametrize("recovery", ("direct", "files", "files-first", "callback", "sweep"))
def test_renamed_and_edited_exposed_copy_is_retained(office_world, monkeypatch, recovery):
    from office import save_as
    from office.publish import RecoveryRequiredError

    http, data, _origin, _manager, broker, _body, session = _opened(office_world)
    (_outputs(data) / "report.docx").unlink()
    with monkeypatch.context() as cut:
        _interrupt_claim(cut, save_as)
        _final_fault(http, session, cut)
    root = _outputs(data)
    claimed = root / "report (2).docx"
    claimed.rename(root / "revised.docx")
    revised = root / "revised.docx"
    revised.write_bytes(EDITED)
    inode = revised.stat().st_ino
    index_before = (data / CHAT / ".ocu" / "index.json").read_bytes()
    revision = broker.OutputsBroker().current_revision(CHAT)
    if recovery == "direct":
        with pytest.raises(RecoveryRequiredError):
            _recover(recovery, http, session, monkeypatch)
    else:
        listed = _recover(recovery, http, session, monkeypatch)
        if recovery in ("files", "files-first"):
            _refused_listing(listed, data, broker, revision, index_before, session, revised, EDITED)
        elif listed is not None:
            assert listed.status_code == 200
    assert revised.exists() and revised.read_bytes() == EDITED
    assert revised.stat().st_ino == inode
    assert not (root / "report (2).docx").exists()
    assert _read(data)["journal"]
    assert (data / CHAT / ".ocu" / "index.json").read_bytes() == index_before


@pytest.mark.parametrize("recovery", ("direct", "files", "files-first"))
def test_rename_only_exposed_copy_completes_in_place(office_world, monkeypatch, recovery):
    from office import save_as

    http, data, _origin, _manager, broker, _body, session = _opened(office_world)
    (_outputs(data) / "report.docx").unlink()
    with monkeypatch.context() as cut:
        _interrupt_claim(cut, save_as)
        _final_fault(http, session, cut)
    root = _outputs(data)
    claimed = root / "report (2).docx"
    inode = claimed.stat().st_ino
    claimed.rename(root / "revised.docx")
    revised = root / "revised.docx"
    listed = _recover(recovery, http, session, monkeypatch)
    if recovery == "files":
        assert listed.status_code == 200
        listed_path = next(item["path"] for item in listed.json()["files"] if item["path"] == "revised.docx")
        assert listed_path == "revised.docx"
    elif recovery == "files-first":
        status, body = listed
        assert status == 200
        assert any(item["path"] == "revised.docx" for item in body["files"])
    _copy_outcome(http, data, broker, session, "revised.docx")
    assert revised.stat().st_ino == inode
    assert revised.read_bytes() == CHANGED
    assert not (root / "report (2).docx").exists()


@pytest.mark.parametrize("recovery", ("direct", "files", "files-first"))
def test_helper_fallback_before_return_completes_actual_name(office_world, monkeypatch, recovery):
    from office import save_as

    http, data, _origin, _manager, broker, _body, session = _opened(office_world)
    root = _outputs(data)
    occupied = root / "report (2).docx"
    (root / "report.docx").unlink()
    with monkeypatch.context() as cut:
        _interrupt_helper_fallback(cut, save_as, occupied)
        _final_fault(http, session, cut)
    fallback = root / "report (2) (2).docx"
    inode = fallback.stat().st_ino
    listed = _recover(recovery, http, session, monkeypatch)
    if recovery == "files":
        assert listed.status_code == 200
    elif recovery == "files-first":
        status, _body = listed
        assert status == 200
    _copy_outcome(http, data, broker, session, "report (2) (2).docx")
    assert fallback.stat().st_ino == inode
    assert occupied.read_bytes() == b"foreign"
    assert fallback.read_bytes() == CHANGED


@pytest.mark.parametrize("recovery", ("direct", "files", "files-first", "callback", "sweep"))
def test_stale_active_index_limit_fails_before_exposure_and_keeps_files_available(
    office_world, monkeypatch, recovery,
):
    import office.publish as publish_mod

    http, data, _origin, _manager, broker, _body, session = _opened(office_world)
    source = _outputs(data) / "report.docx"
    cls = broker.OutputsBroker
    monkeypatch.setattr(publish_mod, "OutputsBroker", lambda: cls(max_active_files=1))
    source.unlink()
    response, _payload_value = _final(http, session, monkeypatch)
    assert response.status_code == 200
    listed = _recover(recovery, http, session, monkeypatch)
    if recovery == "files":
        assert listed.status_code == 200
        assert listed.json()["files"] == []
    elif recovery == "files-first":
        status, body = listed
        assert status == 200
        assert body["files"] == []
    elif listed is not None:
        assert listed.status_code == 200
    state = _read(data)
    record = state["sessions"][session["session_id"]]
    assert record["state"] == "error"
    assert record["reason"] == "index_unavailable"
    assert record["file_id"] == session["file_id"]
    assert not state["journal"]
    assert not (_outputs(data) / "report (2).docx").exists()
    assert state["documents"][session["file_id"]]["versions"][-1]["published"] is False
    listing = broker.OutputsBroker().reconcile(CHAT)
    assert listing["entries"] == []
    _assert_private_reclaimed(data)


def test_index_size_limit_after_claim_retires_unedited_copy(office_world, monkeypatch):
    import office.publish as publish_mod

    http, data, _origin, _manager, broker, _body, session = _opened(office_world)
    index_path = data / CHAT / ".ocu" / "index.json"
    before_bytes = index_path.read_bytes()
    cls = broker.OutputsBroker
    monkeypatch.setattr(publish_mod, "OutputsBroker", lambda: cls(max_index_size=len(before_bytes) + 1))
    (_outputs(data) / "report.docx").unlink()
    response, _payload_value = _final(http, session, monkeypatch)
    assert response.status_code == 200
    listed = http.get(f"/api/outputs/{CHAT}", headers=_auth())
    assert listed.status_code == 200
    state = _read(data)
    record = state["sessions"][session["session_id"]]
    assert record["state"] == "error"
    assert record["reason"] == "index_unavailable"
    assert record["file_id"] == session["file_id"]
    assert not state["journal"]
    assert not (_outputs(data) / "report (2).docx").exists()
    assert state["documents"][session["file_id"]]["versions"][-1]["published"] is False
    _assert_private_reclaimed(data)


def test_postclaim_capacity_retirement_interrupted_before_finish_converges(office_world, monkeypatch):
    import office.publish as publish_mod

    http, data, _origin, _manager, broker, _body, session = _opened(office_world)
    index_path = data / CHAT / ".ocu" / "index.json"
    before_bytes = index_path.read_bytes()
    cls = broker.OutputsBroker
    monkeypatch.setattr(publish_mod, "OutputsBroker", lambda: cls(max_index_size=len(before_bytes) + 1))
    (_outputs(data) / "report.docx").unlink()
    real_finish = publish_mod._finish
    stopped = []

    def finish_once(store, chat, journal_id, result, entry, selected):
        if result.outcome == "failed" and result.reason == "index_unavailable" and not stopped:
            stopped.append(True)
            raise OSError(errno.EIO, "retirement finish interrupted")
        return real_finish(store, chat, journal_id, result, entry, selected)

    monkeypatch.setattr(publish_mod, "_finish", finish_once)
    response, _payload_value = _final(http, session, monkeypatch)
    assert response.status_code == 500
    assert stopped == [True]
    assert _read(data)["journal"]
    listed = http.get(f"/api/outputs/{CHAT}", headers=_auth())
    assert listed.status_code == 200
    state = _read(data)
    record = state["sessions"][session["session_id"]]
    assert record["state"] == "error"
    assert record["reason"] == "index_unavailable"
    assert not state["journal"]
    assert not (_outputs(data) / "report (2).docx").exists()
    _assert_private_reclaimed(data)


@pytest.mark.parametrize("recovery", ("direct", "files", "files-first", "callback", "sweep"))
def test_prepared_tombstoned_source_cleans_up_and_copies(office_world, monkeypatch, recovery):
    import office.publish as publish_mod

    http, data, _origin, _manager, broker, _body, session = _opened(office_world)
    with monkeypatch.context() as cut:
        _interrupt_prepare_allocate(cut, publish_mod)
        _final_fault(http, session, cut)
    state = _read(data)
    entry = next(iter(state["journal"].values()))
    assert "target_path" in entry and entry["staging"]["retired"] is False
    (_outputs(data) / "report.docx").unlink()
    broker.OutputsBroker().reconcile(CHAT)
    listed = _recover(recovery, http, session, monkeypatch)
    if recovery == "files":
        assert listed.status_code == 200
    elif recovery == "files-first":
        status, _body = listed
        assert status == 200
    elif listed is not None:
        assert listed.status_code == 200
    _copy_outcome(http, data, broker, session, "report (2).docx")


def test_prepared_foreign_temporary_still_refuses(office_world, monkeypatch):
    import office.publish as publish_mod
    from office.publish import RecoveryRequiredError

    http, data, _origin, _manager, broker, _body, session = _opened(office_world)
    with monkeypatch.context() as cut:
        _interrupt_prepare_allocate(cut, publish_mod)
        _final_fault(http, session, cut)
    state = _read(data)
    entry = next(iter(state["journal"].values()))
    temporary = _outputs(data) / entry["temporary_name"]
    foreign = data.parent / "foreign-prepared"
    foreign.write_bytes(CHANGED)
    foreign.replace(temporary)
    inode = temporary.stat().st_ino
    index_before = (data / CHAT / ".ocu" / "index.json").read_bytes()
    with pytest.raises(RecoveryRequiredError):
        publish_mod.recover_publications(CHAT)
    listed = http.get(f"/api/outputs/{CHAT}", headers=_auth())
    assert listed.status_code == 503
    assert temporary.read_bytes() == CHANGED
    assert temporary.stat().st_ino == inode
    assert (data / CHAT / ".ocu" / "index.json").read_bytes() == index_before
    assert _read(data)["journal"]


def test_prepared_cleanup_before_copy_when_parent_is_already_absent(office_world, monkeypatch):
    import office.publish as publish_mod

    http, data, _origin, _manager, broker, _body, session = _nested(office_world, "nested/report.docx")
    with monkeypatch.context() as cut:
        _interrupt_prepare_allocate(cut, publish_mod)
        _final_fault(http, session, cut)
    state = _read(data)
    entry = next(iter(state["journal"].values()))
    assert entry["target_path"] == "nested/report.docx"
    assert entry["staging"]["retired"] is False
    shutil.rmtree(_outputs(data) / "nested")
    listed = http.get(f"/api/outputs/{CHAT}", headers=_auth())
    assert listed.status_code == 200
    _copy_outcome(http, data, broker, session, "report (2).docx")
    assert not (_outputs(data) / "nested").exists()


def test_prepared_exposed_temporary_is_retired_before_copy(office_world, monkeypatch):
    import office.publish as publish_mod

    http, data, _origin, _manager, broker, _body, session = _opened(office_world)
    expose = publish_mod._Stage.expose

    def exposed_then_interrupted(self, parent, fence):
        identity = expose(self, parent, fence)
        raise OSError("interrupted after real shared exposure")
        return identity

    with monkeypatch.context() as cut:
        cut.setattr(publish_mod._Stage, "expose", exposed_then_interrupted)
        cut.setattr(publish_mod._Stage, "cleanup", lambda *a, **k: None)
        _final_fault(http, session, cut)
    state = _read(data)
    entry = next(iter(state["journal"].values()))
    temporary = _outputs(data) / entry["temporary_name"]
    assert temporary.exists()
    (_outputs(data) / "report.docx").unlink()
    broker.OutputsBroker().reconcile(CHAT)
    listed = http.get(f"/api/outputs/{CHAT}", headers=_auth())
    assert listed.status_code == 200
    assert not temporary.exists()
    _copy_outcome(http, data, broker, session, "report (2).docx")


def test_extra_hardlink_outside_destination_is_retained(office_world, monkeypatch):
    from office import save_as
    from office.publish import RecoveryRequiredError

    http, data, _origin, _manager, broker, _body, session = _opened(office_world)
    (_outputs(data) / "report.docx").unlink()
    with monkeypatch.context() as cut:
        _interrupt_claim(cut, save_as)
        _final_fault(http, session, cut)
    copy = _outputs(data) / "report (2).docx"
    extra = data.parent / "extra-hardlink.docx"
    os.link(copy, extra)
    index_before = (data / CHAT / ".ocu" / "index.json").read_bytes()
    with pytest.raises(RecoveryRequiredError):
        from office.publish import recover_publications
        recover_publications(CHAT)
    listed = http.get(f"/api/outputs/{CHAT}", headers=_auth())
    assert listed.status_code == 503
    assert copy.read_bytes() == CHANGED
    assert extra.read_bytes() == CHANGED
    assert (data / CHAT / ".ocu" / "index.json").read_bytes() == index_before
    assert _read(data)["journal"]


def test_journaled_claim_edited_before_registration_is_retained(office_world, monkeypatch):
    from office.store import OfficeStore

    http, data, _origin, _manager, broker, _body, session = _opened(office_world)
    (_outputs(data) / "report.docx").unlink()
    original_replace = os.replace

    def stop(source, destination, *args, **kwargs):
        if destination == "index.json":
            raise OSError(errno.EIO, "registration refused")
        return original_replace(source, destination, *args, **kwargs)

    with monkeypatch.context() as boundary:
        boundary.setattr(os, "replace", stop)
        response, _payload_value = _final(http, session, boundary)
    assert response.status_code == 500
    copy = _outputs(data) / "report (2).docx"
    copy.write_bytes(EDITED)
    index_before = (data / CHAT / ".ocu" / "index.json").read_bytes()
    listed = http.get(f"/api/outputs/{CHAT}", headers=_auth())
    assert listed.status_code == 503
    after = OfficeStore().read(CHAT)
    assert after["journal"]
    assert after["sessions"][session["session_id"]]["file_id"] == session["file_id"]
    assert copy.read_bytes() == EDITED
    assert (data / CHAT / ".ocu" / "index.json").read_bytes() == index_before


def test_retirement_interrupted_after_unlink_finishes_without_second_copy(office_world, monkeypatch):
    import office.publish as publish_mod

    http, data, _origin, _manager, broker, _body, session = _opened(office_world)
    index_path = data / CHAT / ".ocu" / "index.json"
    before_bytes = index_path.read_bytes()
    cls = broker.OutputsBroker
    monkeypatch.setattr(publish_mod, "OutputsBroker", lambda: cls(max_index_size=len(before_bytes) + 1))
    (_outputs(data) / "report.docx").unlink()
    real_unlink = os.unlink
    stopped = []

    def unlinked(name, *args, **kwargs):
        answer = real_unlink(name, *args, **kwargs)
        if isinstance(name, str) and name.endswith(".docx") and not stopped:
            stopped.append(True)
            raise OSError(errno.EIO, "retirement unlink interrupted")
        return answer

    with monkeypatch.context() as cut:
        cut.setattr(os, "unlink", unlinked)
        response, _payload_value = _final(http, session, cut)
    assert response.status_code == 500
    listed = http.get(f"/api/outputs/{CHAT}", headers=_auth())
    assert listed.status_code == 200
    state = _read(data)
    record = state["sessions"][session["session_id"]]
    assert record["state"] == "error"
    assert record["reason"] == "index_unavailable"
    assert not state["journal"]
    assert not (_outputs(data) / "report (2).docx").exists()
    _assert_private_reclaimed(data)


_FILES_HTTP_WORKER = r'''
import os
from types import SimpleNamespace
from docker.errors import NotFound
os.environ["BASE_DATA_DIR"] = os.environ["OCU_BASE"]
import docker_manager
def missing(identity):
    raise NotFound(identity)
docker_manager._docker_client = SimpleNamespace(containers=SimpleNamespace(get=missing))
import app
import uvicorn
uvicorn.run(app.app, host="127.0.0.1", port=int(os.environ["OCU_PORT"]),
            lifespan="off", log_level="warning")
'''


def _fresh_files(data):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    child = subprocess.Popen(
        [sys.executable, "-c", _FILES_HTTP_WORKER], cwd=str(SERVER_DIR),
        env=_child_env(data, OCU_PORT=str(port)),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                if child.poll() is not None or time.monotonic() >= deadline:
                    pytest.fail("fresh Files worker did not bind its HTTP socket")
                time.sleep(0.01)
        # Socket readiness makes no HTTP request; Files is the first request,
        # and lifespan=off prevents sweep/startup recovery from doing its work.
        return httpx.get(f"http://127.0.0.1:{port}/api/outputs/{CHAT}", headers=_auth(), timeout=15)
    finally:
        _stop_child(child)


def _kill_worker_at(data, script, cut, session, **settings):
    marker = data.parent / "retirement-crash-marker"
    if marker.exists():
        marker.unlink()
    environment = _child_env(
        data, COPY_CUT=cut, COPY_MARKER=str(marker), COPY_SESSION=session["session_id"], **settings,
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script], cwd=str(SERVER_DIR), env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        _wait_marker(marker, child, f"durable boundary {cut} not reached")
        assert marker.read_text() == cut
        child.kill()
        child.communicate(timeout=5)
        assert child.returncode == -9
    finally:
        _stop_child(child)
    return environment


@pytest.mark.parametrize("recovery", ("direct", "fresh-files"))
def test_journaled_preregistration_rename_completes_same_inode_in_place(
    office_world, monkeypatch, recovery,
):
    from tests.orchestrator.test_office_callback_publish import _surviving
    from office.publish import recover_publications

    http, data, _origin, _manager, broker, _body, session = _opened(office_world)
    (_outputs(data) / "report.docx").unlink()
    revision = broker.OutputsBroker().current_revision(CHAT)
    with _content_origin({"/save.docx": CHANGED}) as origin, _bind_internal(monkeypatch, origin.url):
        payload = _surviving(http, data, session, monkeypatch, origin, final=True)
    _kill_worker_at(data, _COPY_WORKER, "registration-before", session)
    before = _read(data)
    entry = next(iter(before["journal"].values()))
    assert entry["copy"]["claimed_name"] == "report (2).docx"
    assert "registered_file_id" not in entry["copy"]
    index = json.loads((data / CHAT / ".ocu" / "index.json").read_bytes())
    assert "report (2).docx" not in index["active"]
    assert "revised.docx" not in index["active"]
    claimed = _outputs(data) / "report (2).docx"
    identity = claimed.stat().st_dev, claimed.stat().st_ino
    witness = _office(data) / "staging" / entry["staging"]["witness_name"]
    assert (witness.stat().st_dev, witness.stat().st_ino) == identity
    assert witness.stat().st_nlink == 3
    claimed.rename(_outputs(data) / "revised.docx")
    revised = _outputs(data) / "revised.docx"
    if recovery == "direct":
        recover_publications(CHAT)
        assert broker.OutputsBroker().current_revision(CHAT) == revision + 1
    else:
        listed = _fresh_files(data)
        assert listed.status_code == 200, listed.text
        assert [item["path"] for item in listed.json()["files"]] == ["revised.docx"]
        assert listed.json()["files"][0]["file_id"] != session["file_id"]
    state, status = _copy_outcome(http, data, broker, session, "revised.docx")
    assert set(state["documents"]) == {session["file_id"], status["file_id"]}
    assert state["documents"][session["file_id"]] == before["documents"][session["file_id"]]
    assert state["receipts"] == before["receipts"]
    assert state["sessions"][session["session_id"]]["document_key"] == session["document_key"]
    assert (revised.stat().st_dev, revised.stat().st_ino) == identity
    assert revised.read_bytes() == CHANGED
    assert sorted(path.name for path in _outputs(data).iterdir()) == ["revised.docx"]
    _assert_private_reclaimed(data)
    frozen = _snapshot(data)
    assert _post(http, session, payload).json() == {"error": 0}
    assert _snapshot(data) == frozen


@pytest.mark.parametrize("registered_path", ("report (2).docx", "revised.docx"))
def test_journaled_rename_refuses_registration_at_either_path(
    office_world, monkeypatch, registered_path,
):
    from office.publish import RecoveryRequiredError, recover_publications
    http, data, _origin, _manager, broker, _body, session = _opened(office_world)
    (_outputs(data) / "report.docx").unlink()
    real_replace = os.replace
    def stop(source, destination, *args, **kwargs):
        if destination == "index.json":
            raise OSError(errno.EIO, "registration refused")
        return real_replace(source, destination, *args, **kwargs)
    with monkeypatch.context() as boundary:
        boundary.setattr(os, "replace", stop)
        response, _payload_value = _final(http, session, boundary)
    assert response.status_code == 500
    claimed = _outputs(data) / "report (2).docx"
    if registered_path == "report (2).docx":
        broker.OutputsBroker().register_host_write(CHAT, registered_path)
    claimed.rename(_outputs(data) / "revised.docx")
    if registered_path == "revised.docx":
        broker.OutputsBroker().register_host_write(CHAT, registered_path)
    index_before = (data / CHAT / ".ocu" / "index.json").read_bytes()
    with pytest.raises(RecoveryRequiredError):
        recover_publications(CHAT)
    assert (data / CHAT / ".ocu" / "index.json").read_bytes() == index_before
    assert (_outputs(data) / "revised.docx").read_bytes() == CHANGED
    assert _read(data)["journal"]


@pytest.mark.parametrize("capacity", ("active-count", "index-size"))
def test_equal_content_capacity_failures_reclaim_each_private_allocation(
    office_world, monkeypatch, capacity,
):
    import office.publish as publish_mod
    histories = {}
    for number in range(3):
        name = f"report-{number}.docx"
        http, data, _origin, _manager, broker, _body, session = _nested(office_world, name)
        cls = broker.OutputsBroker
        index = data / CHAT / ".ocu" / "index.json"
        if capacity == "active-count":
            limit = len(json.loads(index.read_bytes())["active"])
            factory = lambda: cls(max_active_files=limit)
        else:
            limit = index.stat().st_size + 1
            factory = lambda: cls(max_index_size=limit)
        (_outputs(data) / name).unlink()
        with monkeypatch.context() as configured:
            configured.setattr(publish_mod, "OutputsBroker", factory)
            response, _payload_value = _final(http, session, configured)
        assert response.status_code == 200
        state = _read(data)
        record = state["sessions"][session["session_id"]]
        assert (record["state"], record["reason"]) == ("error", "index_unavailable")
        assert not state["journal"]
        assert list(_outputs(data).iterdir()) == []
        assert state["documents"][session["file_id"]]["versions"][-1]["published"] is False
        histories[session["file_id"]] = state["documents"][session["file_id"]]
        for file_id, history in histories.items():
            assert state["documents"][file_id] == history
        _assert_private_reclaimed(data)


_RETIREMENT_WORKER = r'''
import json, os, time
from pathlib import Path
from types import SimpleNamespace
from docker.errors import NotFound
os.environ["BASE_DATA_DIR"] = os.environ["OCU_BASE"]
import docker_manager
def missing(identity):
    raise NotFound(identity)
docker_manager._docker_client = SimpleNamespace(containers=SimpleNamespace(get=missing))
timeout = os.environ.get("RETIRE_PRECLAIM_TIMEOUT") == "1"
elapsed = [0.0]
if timeout:
    container = SimpleNamespace(id="retirement-budget-sandbox", status="running", attrs={"State": {"Paused": False}})
    def pause():
        container.status, container.attrs["State"]["Paused"] = "paused", True
    def unpause():
        container.status, container.attrs["State"]["Paused"] = "running", False
    container.pause, container.unpause, container.reload = pause, unpause, lambda: None
    docker_manager._docker_client = SimpleNamespace(containers=SimpleNamespace(get=lambda identity: container))
    monotonic = time.monotonic
    time.monotonic = lambda: monotonic() + elapsed[0]
import office.publish as publish
from outputs_broker import OutputsBroker
if os.environ.get("RETIRE_LIMIT"):
    limit = int(os.environ["RETIRE_LIMIT"])
    publish.OutputsBroker = lambda: OutputsBroker(max_index_size=limit)
cut = os.environ.get("COPY_CUT", "")
marker = Path(os.environ["COPY_MARKER"])
def stop(point):
    if point == cut:
        marker.write_text(point)
        while True:
            time.sleep(0.02)
real_replace, real_sync, real_unlink = os.replace, os.fsync, os.unlink
pending = {}
def replaced(source, destination, *args, **kwargs):
    point = None
    if destination == "state.json":
        fd = os.open(source, os.O_RDONLY, dir_fd=kwargs.get("src_dir_fd"))
        try:
            state = json.loads(os.read(fd, 1024 * 1024))
        finally:
            os.close(fd)
        entries = list(state["journal"].values())
        if entries and entries[0].get("copy", {}).get("retained"):
            point = "private-retirement" if entries[0].get("staging", {}).get("retired") else "retained-intent"
    answer = real_replace(source, destination, *args, **kwargs)
    if point:
        pending[kwargs["dst_dir_fd"]] = point
    if timeout and destination == "state.json" and entries and "attempt" in entries[0].get("copy", {}):
        elapsed[0] = 6.0
    return answer
def synced(fd):
    answer = real_sync(fd)
    point = pending.pop(fd, None)
    if point:
        stop(point)
    return answer
def unlinked(name, *args, **kwargs):
    point = "shared-unlink" if name == "report (2).docx" else (
        "anchor-unlink" if isinstance(name, str) and name.startswith(".office-publish.") else (
            "witness-unlink" if isinstance(name, str) and name.startswith(".publish-owner.") else ""
        )
    )
    stop(point + "-before")
    answer = real_unlink(name, *args, **kwargs)
    stop(point + "-after")
    return answer
os.replace, os.fsync, os.unlink = replaced, synced, unlinked
if timeout:
    from office.store import OfficeStore
    journal_id = next(iter(OfficeStore().read(os.environ["OCU_CHAT"])["journal"]))
    publish.publish(os.environ["OCU_CHAT"], journal_id)
else:
    recover_now = os.environ.get("OCU_RECOVER_NOW")
    publish.recover_publications(os.environ["OCU_CHAT"], now=None if recover_now is None else float(recover_now))
'''


def _pending_retirement(office_world, monkeypatch, cut):
    from tests.orchestrator.test_office_callback_publish import _surviving
    http, data, _origin, _manager, broker, _body, session = _opened(office_world)
    (_outputs(data) / "report.docx").unlink()
    index = data / CHAT / ".ocu" / "index.json"
    index_before = index.read_bytes()
    with _content_origin({"/save.docx": CHANGED}) as origin, _bind_internal(monkeypatch, origin.url):
        _surviving(http, data, session, monkeypatch, origin, final=True)
    environment = _kill_worker_at(
        data, _RETIREMENT_WORKER, cut, session, RETIRE_LIMIT=str(len(index_before) + 1),
    )
    return http, data, broker, session, index_before, environment


@pytest.mark.parametrize("cut", (
    "retained-intent", "shared-unlink-before", "shared-unlink-after",
    "private-retirement", "anchor-unlink-after", "witness-unlink-after",
))
def test_killed_retirement_finishes_failure_without_republication_or_private_leak(
    office_world, monkeypatch, cut,
):
    http, data, broker, session, index_before, environment = _pending_retirement(office_world, monkeypatch, cut)
    before = _read(data)
    entry = next(iter(before["journal"].values()))
    assert entry["copy"].get("retained") is True
    assert entry["copy"]["retained_name"] == "report (2).docx"
    assert "claimed_name" not in entry["copy"]
    assert "registered_file_id" not in entry["copy"]
    shared = _outputs(data) / "report (2).docx"
    private = _office(data) / "staging"
    names = [private / entry["staging"][field] for field in ("anchor_name", "witness_name")]
    identity = entry["staging"]["device"], entry["staging"]["inode"]
    if cut in ("retained-intent", "shared-unlink-before"):
        # A reordered unlink-before-intent implementation fails here; an
        # omitted intent fails above at the actual shared-unlink boundary.
        assert shared.read_bytes() == CHANGED
        assert (shared.stat().st_dev, shared.stat().st_ino) == identity
        assert all(path.read_bytes() == CHANGED for path in names)
    else:
        assert not shared.exists()
    assert (data / CHAT / ".ocu" / "index.json").read_bytes() == index_before
    # Remove the capacity restriction in the fresh process. Only durable
    # retirement intent, not a repeated refusal, may choose terminal failure.
    fresh = subprocess.run(
        [sys.executable, "-c", _RETIREMENT_WORKER],
        cwd=str(SERVER_DIR), env={**environment, "COPY_CUT": "", "RETIRE_LIMIT": ""},
        capture_output=True, text=True, timeout=15,
    )
    assert fresh.returncode == 0, (fresh.stdout, fresh.stderr)
    state = _read(data)
    record = state["sessions"][session["session_id"]]
    assert (record["state"], record["reason"], record["saved_as"]) == ("error", "index_unavailable", None)
    assert record["file_id"] == session["file_id"]
    assert state["journal"] == {}
    assert state["documents"] == before["documents"]
    assert state["receipts"] == before["receipts"]
    assert list(_outputs(data).iterdir()) == []
    assert all(not path.exists() for path in names)
    assert (data / CHAT / ".ocu" / "index.json").read_bytes() == index_before
    _assert_private_reclaimed(data)
    listing = _fresh_files(data)
    assert listing.status_code == 200 and listing.json()["files"] == []


@pytest.mark.parametrize("mutation", ("changed", "renamed", "foreign", "extra-link", "private-foreign"))
def test_pending_retirement_preserves_mutated_content_and_refuses_in_fresh_files(
    office_world, monkeypatch, mutation,
):
    http, data, broker, session, index_before, _environment = _pending_retirement(
        office_world, monkeypatch, "retained-intent",
    )
    before = _read(data)
    shared = _outputs(data) / "report (2).docx"
    preserved = shared
    body = CHANGED
    if mutation == "changed":
        shared.write_bytes(EDITED)
        body = EDITED
    elif mutation == "renamed":
        shared.rename(_outputs(data) / "revised.docx")
        preserved = _outputs(data) / "revised.docx"
    elif mutation == "foreign":
        replacement = data.parent / "foreign.docx"
        replacement.write_bytes(CHANGED)
        replacement.replace(shared)
    elif mutation == "extra-link":
        os.link(shared, data.parent / "extra.docx")
    else:
        entry = next(iter(before["journal"].values()))
        preserved = _office(data) / "staging" / entry["staging"]["witness_name"]
        replacement = data.parent / "foreign-private"
        replacement.write_bytes(CHANGED)
        replacement.replace(preserved)
    identity = preserved.stat().st_dev, preserved.stat().st_ino
    response = _fresh_files(data)
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert preserved.read_bytes() == body
    assert (preserved.stat().st_dev, preserved.stat().st_ino) == identity
    assert _read(data)["journal"] == before["journal"]
    assert _read(data)["documents"] == before["documents"]
    assert (data / CHAT / ".ocu" / "index.json").read_bytes() == index_before
    assert not (_outputs(data) / "report (3).docx").exists()


def test_shared_absence_sync_failure_keeps_retirement_responsibility(office_world, monkeypatch):
    from office.publish import recover_publications
    http, data, broker, session, index_before, _environment = _pending_retirement(
        office_world, monkeypatch, "shared-unlink-after",
    )
    parent = _outputs(data)
    identity = parent.stat().st_dev, parent.stat().st_ino
    real_sync = os.fsync
    def refuse(fd):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == identity:
            raise OSError(errno.EIO, "shared absence durability refused")
        return real_sync(fd)
    with monkeypatch.context() as boundary:
        boundary.setattr(os, "fsync", refuse)
        with pytest.raises(OSError):
            recover_publications(CHAT)
    pending = _read(data)
    assert next(iter(pending["journal"].values()))["copy"]["retained"] is True
    assert list(parent.iterdir()) == []
    assert (data / CHAT / ".ocu" / "index.json").read_bytes() == index_before
    recover_publications(CHAT)
    state = _read(data)
    assert state["journal"] == {}
    assert state["sessions"][session["session_id"]]["reason"] == "index_unavailable"
    _assert_private_reclaimed(data)


@pytest.mark.parametrize("mutation", ("changed-private", "foreign-private", "extra-link", "foreign-shared"))
def test_private_retirement_replay_preserves_foreign_or_changed_remaining_links(
    office_world, monkeypatch, mutation,
):
    _http, data, _broker, _session, index_before, _environment = _pending_retirement(
        office_world, monkeypatch, "private-retirement",
    )
    before = _read(data)
    entry = next(iter(before["journal"].values()))
    assert entry["staging"]["retired"] is True
    assert not (_outputs(data) / "report (2).docx").exists()
    anchor = _office(data) / "staging" / entry["staging"]["anchor_name"]
    preserved = anchor
    body = CHANGED
    if mutation == "changed-private":
        anchor.write_bytes(EDITED)
        body = EDITED
    elif mutation == "foreign-private":
        replacement = data.parent / "foreign-retired"
        replacement.write_bytes(CHANGED)
        replacement.replace(anchor)
    elif mutation == "extra-link":
        preserved = data.parent / "retired-extra.docx"
        os.link(anchor, preserved)
    else:
        preserved = _outputs(data) / "report (2).docx"
        preserved.write_bytes(CHANGED)
    identity = preserved.stat().st_dev, preserved.stat().st_ino
    response = _fresh_files(data)
    assert response.status_code == 503
    assert preserved.read_bytes() == body
    assert (preserved.stat().st_dev, preserved.stat().st_ino) == identity
    assert _read(data)["journal"] == before["journal"]
    assert _read(data)["documents"] == before["documents"]
    assert (data / CHAT / ".ocu" / "index.json").read_bytes() == index_before


def test_journaled_registered_identity_rename_does_not_duplicate_registration(office_world, monkeypatch):
    from tests.orchestrator.test_office_callback_publish import _surviving
    http, data, _origin, _manager, _broker, _body, session = _opened(office_world)
    (_outputs(data) / "report.docx").unlink()
    with _content_origin({"/save.docx": CHANGED}) as origin, _bind_internal(monkeypatch, origin.url):
        _surviving(http, data, session, monkeypatch, origin, final=True)
    _kill_worker_at(data, _COPY_WORKER, "registration-journal-after", session)
    before = _read(data)
    binding = next(iter(before["journal"].values()))["copy"]
    assert binding["registered_file_id"] != session["file_id"]
    claimed = _outputs(data) / "report (2).docx"
    claimed.rename(_outputs(data) / "revised.docx")
    index_before = (data / CHAT / ".ocu" / "index.json").read_bytes()
    response = _fresh_files(data)
    assert response.status_code == 503
    assert (_outputs(data) / "revised.docx").read_bytes() == CHANGED
    assert (data / CHAT / ".ocu" / "index.json").read_bytes() == index_before
    assert _read(data)["journal"] == before["journal"]


def test_full_active_index_refuses_before_unavailable_private_allocation(office_world, monkeypatch):
    import office.publish as publish_mod
    http, data, _origin, _manager, broker, _body, session = _opened(office_world)
    (_outputs(data) / "report.docx").unlink()
    cls = broker.OutputsBroker
    monkeypatch.setattr(publish_mod, "OutputsBroker", lambda: cls(max_active_files=1))
    real_open = os.open
    def storage_full(name, flags, *args, **kwargs):
        if isinstance(name, str) and name.startswith(".office-publish.") and flags & os.O_CREAT:
            raise OSError(errno.ENOSPC, "private staging allocation unavailable")
        return real_open(name, flags, *args, **kwargs)
    with monkeypatch.context() as filesystem:
        filesystem.setattr(os, "open", storage_full)
        response, _payload_value = _final(http, session, filesystem)
    assert response.status_code == 200
    state = _read(data)
    record = state["sessions"][session["session_id"]]
    assert (record["state"], record["reason"]) == ("error", "index_unavailable")
    assert state["journal"] == {}
    assert list(_outputs(data).iterdir()) == []
    _assert_private_reclaimed(data)


def test_capacity_retirement_does_not_collect_unbound_private_content(office_world, monkeypatch):
    import office.publish as publish_mod
    http, data, _origin, _manager, broker, _body, session = _opened(office_world)
    private = _office(data) / "staging"
    private.mkdir(mode=0o700, exist_ok=True)
    unbound = private / (".office-publish." + "a" * 32 + ".tmp")
    unbound.write_bytes(CHANGED)
    witness = private / (".publish-owner." + "b" * 32)
    os.link(unbound, witness)
    identity = unbound.stat().st_dev, unbound.stat().st_ino
    (_outputs(data) / "report.docx").unlink()
    cls = broker.OutputsBroker
    index = data / CHAT / ".ocu" / "index.json"
    monkeypatch.setattr(publish_mod, "OutputsBroker", lambda: cls(max_index_size=index.stat().st_size + 1))
    response, _payload_value = _final(http, session, monkeypatch)
    assert response.status_code == 200
    assert _read(data)["journal"] == {}
    assert _read(data)["sessions"][session["session_id"]]["reason"] == "index_unavailable"
    assert (unbound.stat().st_dev, unbound.stat().st_ino) == identity
    assert (witness.stat().st_dev, witness.stat().st_ino) == identity
    assert unbound.read_bytes() == witness.read_bytes() == CHANGED
    assert {path.name for path in private.iterdir()} == {unbound.name, witness.name}


@pytest.mark.parametrize("cut", ("retained-intent", "private-retirement", "anchor-unlink-after", "witness-unlink-after"))
def test_killed_preclaim_timeout_reclaims_private_content_without_publishing(
    office_world, monkeypatch, cut,
):
    from tests.orchestrator.test_office_callback_publish import _surviving
    http, data, _origin, _manager, _broker, _body, session = _opened(office_world)
    (_outputs(data) / "report.docx").unlink()
    with _content_origin({"/save.docx": CHANGED}) as origin, _bind_internal(monkeypatch, origin.url):
        _surviving(http, data, session, monkeypatch, origin, final=True)
    index = data / CHAT / ".ocu" / "index.json"
    index_before = index.read_bytes()
    environment = _kill_worker_at(
        data, _RETIREMENT_WORKER, cut, session, RETIRE_PRECLAIM_TIMEOUT="1",
    )
    before = _read(data)
    entry = next(iter(before["journal"].values()))
    assert entry["copy"]["retained"] is True
    assert entry["copy"]["retained_reason"] == "publish_timeout"
    assert "retained_name" not in entry["copy"]
    assert "claimed_name" not in entry["copy"]
    assert list(_outputs(data).iterdir()) == []
    started = json.loads((_office(data) / "fence.json").read_bytes())["pause_started_at"]
    fresh = subprocess.run(
        [sys.executable, "-c", _RETIREMENT_WORKER], cwd=str(SERVER_DIR),
        env={**environment, "COPY_CUT": "", "RETIRE_PRECLAIM_TIMEOUT": "",
             "OCU_RECOVER_NOW": str(started + 5.001)},
        capture_output=True, text=True, timeout=15,
    )
    assert fresh.returncode == 0, (fresh.stdout, fresh.stderr)
    state = _read(data)
    record = state["sessions"][session["session_id"]]
    assert (record["state"], record["reason"], record["saved_as"]) == ("error", "publish_timeout", None)
    assert state["journal"] == {}
    assert state["documents"] == before["documents"]
    assert state["receipts"] == before["receipts"]
    assert (data / CHAT / ".ocu" / "index.json").read_bytes() == index_before
    assert list(_outputs(data).iterdir()) == []
    _assert_private_reclaimed(data)
