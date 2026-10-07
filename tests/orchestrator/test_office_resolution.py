# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Explicit conflict resolution through authenticated HTTP and durable state."""
from __future__ import annotations

import errno
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

import httpx
import pytest

from tests.orchestrator._office_recorded_callbacks import recorded_status_4_payload
from tests.orchestrator._office_store import SERVER_DIR, _child_env, _stop_child, _wait_marker
from tests.orchestrator.test_office_callback_publish import _read, _running
from tests.orchestrator.test_office_save_as import _nested, _fresh_files
from tests.orchestrator.test_office_save_close import _forcesave, _save
from tests.orchestrator.test_office_session_lifecycle import _info, _created
from tests.orchestrator.test_office_sessions import _assert_refusal, _create, _snapshot, _put, _index_file
from tests.orchestrator.test_outputs_endpoint import CHAT_B

from tests.orchestrator.test_office_callback_publish import (
    CHANGED, _allocate, _bind_internal, _content_origin, _opened, _payload, _post, _sha,
)
from tests.orchestrator.test_office_session_lifecycle import _status
from tests.orchestrator.test_office_sessions import _outputs, _versions, office_world
from tests.orchestrator.test_outputs_endpoint import CHAT, _auth


def test_resolve_default_saves_latest_user_content_as_new_document(office_world, monkeypatch):
    http, data, _origin, _manager, broker, content, session = _opened(office_world)
    from office.store import OfficeStore

    store = OfficeStore()
    session_id = session["session_id"]
    original_id = session["file_id"]
    assert store.read(CHAT)["sessions"][session_id]["state"] == "editing"
    _allocate(http, session, monkeypatch)
    original = _outputs(data) / "report.docx"
    agent_content = content + b"agent-workspace-change"
    original.write_bytes(agent_content)

    with _content_origin({"/save.docx": CHANGED}) as origin, _bind_internal(monkeypatch, origin.url):
        callback = _post(http, session, _payload(session, origin.url + "/save.docx"))
    assert callback.status_code == 200, callback.text
    assert callback.json() == {"error": 0}
    before = store.read(CHAT)
    conflicted = before["sessions"][session_id]
    assert (conflicted["state"], conflicted["reason"]) == ("conflict", "baseline_mismatch")
    assert (conflicted["save_seq"], conflicted["last_committed_seq"], conflicted["last_published_seq"]) == (1, 1, 0)
    assert before["documents"][original_id]["versions"][-1]["sha256"] == _sha(CHANGED)
    assert before["documents"][original_id]["versions"][-1]["published"] is False
    assert before["receipts"][session_id]["1"] == {
        "status": 6, "version": 2, "sha256": _sha(CHANGED), "answer": {"error": 0},
    }
    assert before["journal"] == {}
    assert original.read_bytes() == agent_content
    assert (_versions(data) / _sha(CHANGED)).read_bytes() == CHANGED

    resolved = http.post(
        f"/api/office/{CHAT}/sessions/{session_id}/resolve", headers=_auth(), json={},
    )
    assert resolved.status_code == 200, resolved.text
    result = resolved.json()
    new_id = result["file_id"]
    assert new_id != original_id
    assert result == {
        "session_id": session_id, "state": "editing", "file_id": new_id,
        "path": "report (2).docx",
    }
    copied = _outputs(data) / "report (2).docx"
    assert original.read_bytes() == agent_content
    assert copied.read_bytes() == CHANGED

    status_response = _status(http, session_id)
    assert status_response.status_code == 200, status_response.text
    status = status_response.json()
    assert status["session_id"] == session_id
    assert (status["state"], status["reason"]) == ("editing", None)
    assert status["file_id"] == new_id
    assert status["document_key"] == session["document_key"]
    assert status["saved_as"] == {"file_id": new_id, "path": "report (2).docx"}
    assert (status["save_seq"], status["last_committed_seq"], status["last_published_seq"]) == (1, 1, 1)

    after = store.read(CHAT)
    record = after["sessions"][session_id]
    assert (record["state"], record["file_id"], record["document_key"]) == (
        "editing", new_id, session["document_key"],
    )
    assert record["saved_as"] == {"file_id": new_id, "path": "report (2).docx"}
    assert (record["save_seq"], record["last_committed_seq"], record["last_published_seq"]) == (1, 1, 1)
    assert record["baseline_sha256"] == _sha(CHANGED)
    assert after["documents"][original_id] == before["documents"][original_id]
    assert after["receipts"] == before["receipts"]
    assert after["journal"] == {}
    assert set(after["documents"]) == {original_id, new_id}
    document = after["documents"][new_id]
    assert document["path"] == "report (2).docx"
    assert document["published_version"] == 1
    assert document["published_sha256"] == _sha(CHANGED)
    version, = document["versions"]
    assert {key: version[key] for key in ("number", "parent", "source", "published", "sha256", "size")} == {
        "number": 1, "parent": None, "source": "conflict", "published": True,
        "sha256": _sha(CHANGED), "size": len(CHANGED),
    }
    assert (_versions(data) / _sha(content)).read_bytes() == content
    blob = _versions(data) / _sha(CHANGED)
    assert blob.read_bytes() == CHANGED
    assert (copied.stat().st_dev, copied.stat().st_ino) != (blob.stat().st_dev, blob.stat().st_ino)

    listing = http.get(f"/api/outputs/{CHAT}", headers=_auth())
    assert listing.status_code == 200, listing.text
    entries = {entry["path"]: entry for entry in listing.json()["files"]}
    assert entries["report.docx"]["file_id"] == original_id
    assert entries["report (2).docx"]["file_id"] == new_id
    indexed = broker.OutputsBroker().reconcile(CHAT)
    identities = {entry["path"]: entry["file_id"] for entry in indexed["entries"]}
    assert identities["report.docx"] == original_id
    assert identities["report (2).docx"] == new_id
    assert original.read_bytes() == agent_content
    assert copied.read_bytes() == CHANGED


def _resolve(http, session, action="save_as", *, body=None, chat=CHAT, headers=None):
    options = {"headers": _auth() if headers is None else headers}
    if body is not None:
        options["content"] = body
    else:
        options["json"] = {"action": action}
    return http.post(f"/api/office/{chat}/sessions/{session['session_id']}/resolve", **options)


def _conflict(world, monkeypatch, *, name="report.docx", final=False):
    opened = _opened(world) if name == "report.docx" else _nested(world, name)
    http, data, _origin, _manager, _broker, original, session = opened
    if not final:
        _allocate(http, session, monkeypatch)
    workspace = _outputs(data) / name
    workspace.write_bytes(original + b"agent-workspace")
    with _content_origin({"/user.docx": CHANGED}) as origin, _bind_internal(monkeypatch, origin.url):
        response = _post(http, session, _payload(session, origin.url + "/user.docx", final=final))
    assert response.status_code == 200 and response.json() == {"error": 0}
    assert _read(data)["sessions"][session["session_id"]]["state"] == "conflict"
    return opened


def _assert_resolved(data, session, *, ended=False, content=CHANGED):
    state = _read(data)
    record = state["sessions"][session["session_id"]]
    assert (record["state"], record["reason"]) == ("closed" if ended else "editing", None)
    assert record["document_key"] == session["document_key"]
    assert record["last_published_seq"] == record["last_committed_seq"]
    assert record["baseline_sha256"] == _sha(content)
    assert state["journal"] == {}
    latest = state["documents"][record["file_id"]]["versions"][-1]
    assert latest["sha256"] == _sha(content) and latest["published"] is True
    return state


@pytest.mark.parametrize("body", (b"", b'{"unrelated":true}'))
def test_empty_or_actionless_body_defaults_to_copy(office_world, monkeypatch, body):
    http, data, _origin, _manager, _broker, original, session = _conflict(office_world, monkeypatch)
    before = _read(data)
    response = _resolve(http, session, body=body)
    assert response.status_code == 200, response.text
    assert response.json()["path"] == "report (2).docx"
    assert (_outputs(data) / "report.docx").read_bytes() == original + b"agent-workspace"
    assert (_outputs(data) / "report (2).docx").read_bytes() == CHANGED
    after = _assert_resolved(data, session)
    assert after["documents"][session["file_id"]] == before["documents"][session["file_id"]]
    assert after["receipts"] == before["receipts"]


@pytest.mark.parametrize("body", (
    b"not-json", b"[", b"[]", b"null", b"false", b'"overwrite"', b"1",
    b'{"action":null}', b'{"action":true}', b'{"action":1}',
    b'{"action":[]}', b'{"action":{}}', b'{"action":"Overwrite"}', b'{"action":"discard"}',
))
def test_invalid_resolve_body_preserves_all_content(office_world, monkeypatch, body):
    http, data, _origin, _manager, _broker, _original, session = _conflict(office_world, monkeypatch)
    before = _snapshot(data)
    _assert_refusal(_resolve(http, session, body=body), 422, "invalid_request")
    assert _snapshot(data) == before


@pytest.mark.parametrize("ended", (False, True))
@pytest.mark.parametrize("action", ("save_as", "overwrite"))
def test_both_actions_complete_final_receipt_lifecycle_without_new_sequence(office_world, monkeypatch, ended, action):
    http, data, _origin, _manager, broker, original, session = _conflict(
        office_world, monkeypatch, final=ended,
    )
    before = _read(data)
    revision = broker.OutputsBroker().current_revision(CHAT)
    response = _resolve(http, session, action)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["state"] == ("closed" if ended else "editing")
    after = _assert_resolved(data, session, ended=ended)
    assert after["receipts"] == before["receipts"]
    assert after["sessions"][session["session_id"]]["save_seq"] == 1
    assert broker.OutputsBroker().current_revision(CHAT) == revision + 1
    source = after["documents"][session["file_id"]]
    if action == "overwrite":
        assert result == {"session_id": session["session_id"], "state": result["state"],
                          "file_id": session["file_id"], "path": "report.docx"}
        assert (_outputs(data) / "report.docx").read_bytes() == CHANGED
        assert [(v["number"], v["parent"], v["source"], v["published"]) for v in source["versions"]] == [
            (1, None, "workspace", True), (2, 1, "save" if not ended else "close", True),
            (3, 2, "workspace", True), (4, 2, "restore", True),
        ]
        assert source["versions"][2]["sha256"] == _sha(original + b"agent-workspace")
        assert source["versions"][3]["sha256"] == _sha(CHANGED)
        assert source["published_version"] == 4
        assert (_versions(data) / _sha(original + b"agent-workspace")).read_bytes() == original + b"agent-workspace"
    else:
        assert result["file_id"] != session["file_id"]
        assert source == before["documents"][session["file_id"]]
        assert (_outputs(data) / "report.docx").read_bytes() == original + b"agent-workspace"
    frozen = _snapshot(data)
    _assert_refusal(_resolve(http, session, action), 409, "not_in_conflict")
    assert _snapshot(data) == frozen


def test_overwrite_join_no_change_save_final_and_later_conflict_select_user_lineage(office_world, monkeypatch):
    http, data, _origin, _manager, broker, original, session = _conflict(office_world, monkeypatch)
    assert _resolve(http, session, "overwrite").status_code == 200
    restored = _read(data)
    with _info(monkeypatch, session["document_key"]):
        joined = _create(http, session["file_id"])
    assert joined.status_code == 200
    source_path = urlsplit(joined.json()["editor_config"]["document"]["url"]).path
    source = http.get(source_path)
    assert source.status_code == 200 and source.content == CHANGED
    revision = broker.OutputsBroker().current_revision(CHAT)
    with _forcesave(monkeypatch, session["document_key"], code=4):
        saved = _save(http, session["session_id"], "publish")
    assert saved.status_code == 202
    assert _read(data)["documents"] == restored["documents"]
    assert (_outputs(data) / "report.docx").read_bytes() == CHANGED
    assert broker.OutputsBroker().current_revision(CHAT) == revision
    (_outputs(data) / "report.docx").write_bytes(original + b"later-agent")
    # A later save uses the same editor key but a newer user-content callback.
    with _forcesave(monkeypatch, session["document_key"]):
        allocated = _save(http, session["session_id"])
    assert allocated.json()["save_seq"] == 3
    newest = CHANGED + b"later-user"
    with _content_origin({"/new.docx": newest}) as origin, _bind_internal(monkeypatch, origin.url):
        response = _post(http, session, _payload(session, origin.url + "/new.docx", save_seq=3))
    assert response.status_code == 200
    assert _read(data)["sessions"][session["session_id"]]["state"] == "conflict"
    assert _resolve(http, session, "overwrite").status_code == 200
    assert (_outputs(data) / "report.docx").read_bytes() == newest
    document = _read(data)["documents"][session["file_id"]]
    assert document["versions"][-1]["sha256"] == _sha(newest)
    assert document["versions"][-1]["parent"] == 5
    before_final = _read(data)
    response = _post(http, session, recorded_status_4_payload(document_key=session["document_key"]))
    assert response.status_code == 200 and response.json() == {"error": 0}
    after_final = _read(data)
    assert after_final["sessions"][session["session_id"]]["state"] == "closed"
    assert after_final["journal"] == {}
    assert after_final["documents"] == before_final["documents"]
    assert (_outputs(data) / "report.docx").read_bytes() == newest


@pytest.mark.parametrize("action", ("save_as", "overwrite"))
def test_resolve_uses_final_version_four_not_first_conflict_three(office_world, monkeypatch, action):
    from office.store import OfficeStore
    http, data, _origin, _manager, _broker, _original, session = _conflict(office_world, monkeypatch)
    store = OfficeStore()
    # The first stored save is version 2; two later actual status-6/2 arrivals
    # retain versions 3 and 4 while the workspace remains in conflict.
    def allocation(state):
        record = state["sessions"][session["session_id"]]
        record["save_seq"] = 2
        record["save_intents"]["2"] = "publish"
    store.update(CHAT, allocation)
    with _content_origin({"/three.docx": CHANGED + b"three", "/four.docx": CHANGED + b"four"}) as origin, _bind_internal(monkeypatch, origin.url):
        assert _post(http, session, _payload(session, origin.url + "/three.docx", save_seq=2)).json() == {"error": 0}
        assert _post(http, session, _payload(session, origin.url + "/four.docx", final=True)).json() == {"error": 0}
    before = store.read(CHAT)
    assert before["documents"][session["file_id"]]["versions"][-1]["number"] == 4
    assert before["sessions"][session["session_id"]]["state"] == "conflict"
    response = _resolve(http, session, action)
    assert response.status_code == 200
    assert response.json()["state"] == "closed"
    assert (_outputs(data) / response.json()["path"]).read_bytes() == CHANGED + b"four"
    after = _assert_resolved(data, session, ended=True, content=CHANGED + b"four")
    assert after["receipts"] == before["receipts"]
    assert after["sessions"][session["session_id"]]["save_seq"] == 3


@pytest.mark.parametrize("historical", (True, False), ids=("older-workspace-hash", "equal-user-bytes"))
def test_overwrite_reuses_known_workspace_history_without_duplicate_restore(office_world, monkeypatch, historical):
    http, data, _origin, _manager, _broker, original, session = _conflict(office_world, monkeypatch)
    (_outputs(data) / "report.docx").write_bytes(original if historical else CHANGED)
    before = _read(data)
    response = _resolve(http, session, "overwrite")
    assert response.status_code == 200
    after = _assert_resolved(data, session)
    document = after["documents"][session["file_id"]]
    assert len(document["versions"]) == 2
    assert document["published_version"] == 2
    assert document["versions"][0] == before["documents"][session["file_id"]]["versions"][0]
    assert document["versions"][1] == {**before["documents"][session["file_id"]]["versions"][1], "published": True}
    assert (_outputs(data) / "report.docx").read_bytes() == CHANGED


@pytest.mark.parametrize("tombstone", (False, True))
def test_missing_path_refuses_overwrite_then_copies_without_recreating_old_name(office_world, monkeypatch, tombstone):
    http, data, _origin, _manager, broker, _original, session = _conflict(office_world, monkeypatch)
    (_outputs(data) / "report.docx").unlink()
    if tombstone:
        broker.OutputsBroker().reconcile(CHAT)
    before = _read(data)
    _assert_refusal(_resolve(http, session, "overwrite"), 409, "path_missing")
    after = _read(data)
    assert after["documents"] == before["documents"] and after["receipts"] == before["receipts"]
    assert after["sessions"][session["session_id"]]["state"] == "conflict"
    assert not (_outputs(data) / "report.docx").exists()
    assert _resolve(http, session, "save_as").status_code == 200
    assert (_outputs(data) / "report (2).docx").read_bytes() == CHANGED
    assert not (_outputs(data) / "report.docx").exists()


@pytest.mark.parametrize("mutation", ("leaf-link", "parent-link", "root-link", "directory", "oversize"))
def test_unsafe_overwrite_refuses_before_capture_and_preserves_targets(office_world, monkeypatch, mutation):
    name = "nested/report.docx" if mutation == "parent-link" else "report.docx"
    http, data, _origin, _manager, _broker, _original, session = _conflict(office_world, monkeypatch, name=name)
    root = _outputs(data)
    target = root / name
    outside = data.parent / "outside"
    outside.mkdir()
    secret = outside / "report.docx"
    secret.write_bytes(b"outside-private")
    if mutation == "leaf-link":
        target.unlink()
        target.symlink_to(secret)
    elif mutation == "parent-link":
        target.unlink()
        target.parent.rmdir()
        target.parent.symlink_to(outside, target_is_directory=True)
    elif mutation == "root-link":
        root.rename(root.with_name("detached"))
        root.symlink_to(outside, target_is_directory=True)
    elif mutation == "directory":
        target.unlink()
        target.mkdir()
    else:
        with target.open("wb") as stream:
            stream.truncate(100 * 1024 * 1024 + 1)
    before = _read(data)
    real_open = os.open
    forbidden = {(p.stat().st_dev, p.stat().st_ino) for p in (outside, secret)}
    def no_external_open(name, flags, *args, **kwargs):
        fd = real_open(name, flags, *args, **kwargs)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) in forbidden:
            os.close(fd)
            pytest.fail("resolve opened a symlink target")
        return fd
    with monkeypatch.context() as boundary:
        boundary.setattr(os, "open", no_external_open)
        _assert_refusal(_resolve(http, session, "overwrite"), 503, "unsafe_path")
    after = _read(data)
    assert after["documents"] == before["documents"] and after["receipts"] == before["receipts"]
    assert after["sessions"][session["session_id"]]["state"] == "conflict"
    assert secret.read_bytes() == b"outside-private"
    assert after["journal"] == {}


@pytest.mark.parametrize("missing", (False, True), ids=("linked-parent", "missing-parent"))
def test_explicit_copy_falls_back_to_root_without_following_or_creating_parent(office_world, monkeypatch, missing):
    http, data, _origin, _manager, _broker, original, session = _conflict(office_world, monkeypatch, name="nested/report.docx")
    parent = _outputs(data) / "nested"
    detached = data.parent / "detached"
    parent.rename(detached)
    if not missing:
        parent.symlink_to(detached, target_is_directory=True)
    response = _resolve(http, session)
    assert response.status_code == 200 and response.json()["path"] == "report (2).docx"
    assert (_outputs(data) / "report (2).docx").read_bytes() == CHANGED
    assert (detached / "report.docx").read_bytes() == original + b"agent-workspace"
    assert parent.is_symlink() if not missing else not parent.exists()


@pytest.mark.parametrize("action", ("save_as", "overwrite"))
def test_missing_workspace_ends_error_and_allows_another_document_session(office_world, monkeypatch, action):
    http, data, _origin, _manager, broker, _original, session = _conflict(office_world, monkeypatch)
    shutil.rmtree(_outputs(data))
    before = _read(data)
    _assert_refusal(_resolve(http, session, action), 409, "workspace_missing")
    assert not _outputs(data).exists()
    after = _read(data)
    assert after["documents"] == before["documents"] and after["receipts"] == before["receipts"]
    assert after["sessions"][session["session_id"]]["state"] == "error"
    assert after["sessions"][session["session_id"]]["reason"] == "workspace_missing"
    _put(data, "another.docx", CHANGED)
    file_id = _index_file(broker, data, "another.docx")
    assert _create(http, file_id).status_code == 201


def test_editing_unknown_foreign_and_unauthenticated_resolve_have_no_authority(office_world):
    http, data, _origin, _manager, _broker, _original, session = _opened(office_world)
    _file_id, foreign = _created(office_world, chat=CHAT_B)
    before = _snapshot(data)
    _assert_refusal(_resolve(http, session), 409, "not_in_conflict")
    _assert_refusal(_resolve(http, {"session_id": "absent"}), 404, "unknown_session")
    _assert_refusal(_resolve(http, foreign), 404, "unknown_session")
    assert _resolve(http, session, headers={}).status_code == 401
    assert _snapshot(data) == before


@pytest.mark.parametrize("action", ("save_as", "overwrite"))
def test_changed_restore_epoch_orphans_without_creating_resolve_authority(office_world, monkeypatch, action):
    http, data, _origin, _manager, _broker, _original, session = _conflict(office_world, monkeypatch)
    (data / ".office-restore-epoch").write_text("restored-epoch\\n")
    before = _read(data)
    workspace = _snapshot(_outputs(data))
    _assert_refusal(_resolve(http, session, action), 409, "not_in_conflict")
    after = _read(data)
    assert (after["sessions"][session["session_id"]]["state"], after["sessions"][session["session_id"]]["reason"]) == ("orphaned", "restore_epoch_changed")
    assert after["documents"] == before["documents"] and after["receipts"] == before["receipts"]
    assert after["journal"] == {} and _snapshot(_outputs(data)) == workspace


@pytest.mark.parametrize("action", ("save_as", "overwrite"))
@pytest.mark.parametrize("reason", ("pause_failed", "publish_timeout", "index_unavailable", "storage_low"))
def test_resolve_refusals_keep_conflict_reason_and_stored_content(office_world, monkeypatch, action, reason):
    http, data, _origin, manager, _broker, _original, session = _conflict(office_world, monkeypatch)
    before = _read(data)
    workspace = _snapshot(_outputs(data))
    if reason in ("pause_failed", "publish_timeout"):
        container = _running(manager)
        if reason == "pause_failed":
            container.pause.side_effect = RuntimeError("engine refuses pause")
        else:
            pause = container.pause.side_effect
            real_clock = time.monotonic
            elapsed = [0.0]
            monkeypatch.setattr(time, "monotonic", lambda: real_clock() + elapsed[0])
            def slow_pause():
                pause()
                elapsed[0] = 5.1
            container.pause.side_effect = slow_pause
    elif reason == "index_unavailable":
        (data / CHAT / ".ocu" / "index.json").write_bytes(b"{broken")
    else:
        monkeypatch.setattr(os, "fstatvfs", lambda _fd: type("Low", (), {"f_bavail": 0, "f_frsize": 4096})())
    _assert_refusal(_resolve(http, session, action), 503, reason)
    after = _read(data)
    assert (after["sessions"][session["session_id"]]["state"], after["sessions"][session["session_id"]]["reason"]) == ("conflict", "baseline_mismatch")
    assert after["documents"] == before["documents"] and after["receipts"] == before["receipts"]
    assert _snapshot(_outputs(data)) == workspace


@pytest.mark.parametrize("action", ("save_as", "overwrite"))
@pytest.mark.parametrize("state", ("running", "exited", "paused"))
def test_resolve_observes_writer_exclusion_without_starting_or_claiming_external_pause(office_world, monkeypatch, action, state):
    from tests.orchestrator.test_lifecycle import _container, _docker
    http, data, _origin, manager, _broker, _original, session = _conflict(office_world, monkeypatch)
    if state == "running":
        container = _running(manager)
    else:
        container = _container(manager._container_name(CHAT), status=state)
        manager._docker_client = _docker([container])
    response = _resolve(http, session, action)
    assert response.status_code == 200
    assert (_outputs(data) / response.json()["path"]).read_bytes() == CHANGED
    assert container.pause.call_count == (1 if state == "running" else 0)
    assert container.unpause.call_count == (1 if state == "running" else 0)
    assert container.status == state


@pytest.mark.parametrize("occupied", ("file", "symlink", "directory"))
def test_explicit_copy_claim_skips_every_existing_directory_entry(office_world, monkeypatch, occupied):
    http, data, _origin, _manager, _broker, _original, session = _conflict(office_world, monkeypatch)
    candidate = _outputs(data) / "report (2).docx"
    if occupied == "file":
        candidate.write_bytes(b"unrelated")
    elif occupied == "directory":
        candidate.mkdir()
    else:
        outside = data.parent / "foreign"
        outside.write_bytes(b"unrelated")
        candidate.symlink_to(outside)
    response = _resolve(http, session)
    assert response.status_code == 200 and response.json()["path"] == "report (3).docx"
    assert (_outputs(data) / "report (3).docx").read_bytes() == CHANGED
    if occupied != "directory":
        assert candidate.read_bytes() == b"unrelated"


@pytest.mark.parametrize("action", ("save_as", "overwrite"))
def test_enospc_workspace_storage_refuses_without_losing_user_or_agent_bytes(office_world, monkeypatch, action):
    http, data, _origin, _manager, _broker, original, session = _conflict(office_world, monkeypatch)
    before = _read(data)
    real_write = os.write
    def no_space(fd, body):
        if bytes(body) in (CHANGED, original + b"agent-workspace"):
            raise OSError(errno.ENOSPC, "no space for workspace bytes")
        return real_write(fd, body)
    with monkeypatch.context() as fault:
        fault.setattr(os, "write", no_space)
        _assert_refusal(_resolve(http, session, action), 503, "storage_low")
    after = _read(data)
    assert after["documents"] == before["documents"] and after["receipts"] == before["receipts"]
    assert after["sessions"][session["session_id"]]["state"] == "conflict"
    assert after["journal"] == {}
    assert (_outputs(data) / "report.docx").read_bytes() == original + b"agent-workspace"
    response = _resolve(http, session, action)
    assert response.status_code == 200
    assert (_outputs(data) / response.json()["path"]).read_bytes() == CHANGED


def test_failed_replacement_keeps_user_restore_latest_and_retry_does_not_duplicate_history(office_world, monkeypatch):
    http, data, _origin, _manager, _broker, original, session = _conflict(office_world, monkeypatch)
    before = _read(data)
    real_link = os.link
    def refuse_exposure(source, destination, *args, **kwargs):
        if str(destination).startswith(".office-publish."):
            raise OSError(errno.ELOOP, "exposure path became unsafe")
        return real_link(source, destination, *args, **kwargs)
    with monkeypatch.context() as fault:
        fault.setattr(os, "link", refuse_exposure)
        _assert_refusal(_resolve(http, session, "overwrite"), 503, "unsafe_path")
    failed = _read(data)
    document = failed["documents"][session["file_id"]]
    assert [(v["source"], v["sha256"], v["published"]) for v in document["versions"][2:]] == [
        ("workspace", _sha(original + b"agent-workspace"), True),
        ("restore", _sha(CHANGED), False),
    ]
    assert document["versions"][-1]["parent"] == 2
    assert failed["receipts"] == before["receipts"]
    assert (failed["sessions"][session["session_id"]]["state"], failed["sessions"][session["session_id"]]["reason"]) == ("conflict", "baseline_mismatch")
    assert (_outputs(data) / "report.docx").read_bytes() == original + b"agent-workspace"
    with _info(monkeypatch, session["document_key"]):
        joined = _create(http, session["file_id"])
    assert joined.status_code == 200
    source = http.get(urlsplit(joined.json()["editor_config"]["document"]["url"]).path)
    assert source.status_code == 200 and source.content == CHANGED
    assert _resolve(http, session, "overwrite").status_code == 200
    after = _read(data)
    assert len(after["documents"][session["file_id"]]["versions"]) == 4
    assert after["documents"][session["file_id"]]["versions"][-1]["sha256"] == _sha(CHANGED)
    assert after["documents"][session["file_id"]]["versions"][1]["published"] is True
    assert (_outputs(data) / "report.docx").read_bytes() == CHANGED


_RESOLVE_WORKER = r'''
import json, os, stat, time
from pathlib import Path
from types import SimpleNamespace
from docker.errors import NotFound
os.environ["BASE_DATA_DIR"] = os.environ["OCU_BASE"]
import docker_manager
def missing(identity):
    raise NotFound(identity)
docker_manager._docker_client = SimpleNamespace(containers=SimpleNamespace(get=missing))
import app
from fastapi.testclient import TestClient
from office.publish import recover_publications
cut = os.environ.get("RESOLVE_CUT", "")
marker = Path(os.environ["RESOLVE_MARKER"])
pending = {}
real_replace, real_link, real_sync = os.replace, os.link, os.fsync
import fcntl
real_flock = fcntl.flock
wait_marker = os.environ.get("RESOLVE_WAIT_MARKER")
announced = False
def flock(fd, operation):
    global announced
    if wait_marker and not announced and operation & fcntl.LOCK_EX:
        announced = True
        with open(wait_marker, "w") as stream:
            stream.write("waiting")
            stream.flush()
            real_sync(stream.fileno())
    return real_flock(fd, operation)
fcntl.flock = flock
def stop(point):
    if not cut or not point or cut != point:
        return
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, point.encode())
        real_sync(fd)
    finally:
        os.close(fd)
    while True:
        time.sleep(0.02)
def replaced(source, destination, *args, **kwargs):
    phase = ""
    parent = kwargs.get("dst_dir_fd")
    if destination == "state.json":
        fd = os.open(source, os.O_RDONLY, dir_fd=kwargs.get("src_dir_fd"))
        try:
            body = b""
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                body += chunk
            successor = json.loads(body)
        finally:
            os.close(fd)
        live = [entry for entry in successor["journal"].values() if entry["requester"] == "resolve"]
        if live:
            if "restore_version" in live[0]:
                phase = "lineage"
            elif "target_path" not in live[0] and "copy" not in live[0]:
                phase = "accepted"
        else:
            record = successor["sessions"].get(os.environ["RESOLVE_SESSION"])
            if record is not None and record["state"] in ("editing", "closed"):
                phase = "completion"
    elif destination == "report.docx":
        phase = "replacement"
    elif destination == "index.json":
        phase = "registration"
    if phase == "lineage":
        stop("lineage-before")
    result = real_replace(source, destination, *args, **kwargs)
    if phase and parent is not None:
        info = os.fstat(parent)
        pending[(info.st_dev, info.st_ino)] = phase
    return result
def linked(source, destination, *args, **kwargs):
    result = real_link(source, destination, *args, **kwargs)
    if destination == "report (2).docx":
        parent = kwargs["dst_dir_fd"]
        info = os.fstat(parent)
        pending[(info.st_dev, info.st_ino)] = "claim"
    return result
def synced(fd):
    result = real_sync(fd)
    info = os.fstat(fd)
    if stat.S_ISDIR(info.st_mode):
        phase = pending.pop((info.st_dev, info.st_ino), "")
        stop(phase)
    return result
os.replace, os.link, os.fsync = replaced, linked, synced
mode = os.environ.get("RESOLVE_MODE", "request")
if mode == "http":
    import uvicorn
    uvicorn.run(app.app, host="127.0.0.1", port=int(os.environ["OCU_PORT"]),
                lifespan="off", log_level="warning")
elif mode == "recover":
    recover_publications(os.environ["OCU_CHAT"])
elif mode == "startup":
    app.sweep_office_publications()
elif mode == "callback":
    http = TestClient(app.app)
    response = http.post(
        "/office/callback/" + os.environ["OCU_CHAT"] + "/" + os.environ["RESOLVE_SESSION"],
        headers={"Authorization": "Bearer " + os.environ["CALLBACK_TOKEN"]},
        json={},
    )
    print("callback_result=" + json.dumps({"status": response.status_code, "body": response.json()}), flush=True)
else:
    http = TestClient(app.app)
    response = http.post(
        "/api/office/" + os.environ["OCU_CHAT"] + "/sessions/" + os.environ["RESOLVE_SESSION"] + "/resolve",
        headers={"Authorization": "Bearer " + os.environ["OCU_INTERNAL_TOKEN"]},
        json={"action": os.environ["RESOLVE_ACTION"]},
    )
    print(json.dumps({"status": response.status_code, "body": response.json()}), flush=True)
print("completed", flush=True)
'''


def _kill_resolve(data, session, action, cut):
    marker = data.parent / ("resolve-" + cut)
    environment = _child_env(
        data, RESOLVE_CUT=cut, RESOLVE_MARKER=str(marker),
        RESOLVE_SESSION=session["session_id"], RESOLVE_ACTION=action,
    )
    child = subprocess.Popen(
        [sys.executable, "-c", _RESOLVE_WORKER], cwd=str(SERVER_DIR), env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        _wait_marker(marker, child, "resolve durable cut was not reached: " + cut)
        assert marker.read_text() == cut
        child.kill()
        child.communicate(timeout=5)
        assert child.returncode == -9
    finally:
        _stop_child(child)
    return environment


def _fresh_resolve(environment, *, mode="recover"):
    child = subprocess.run(
        [sys.executable, "-c", _RESOLVE_WORKER], cwd=str(SERVER_DIR),
        env={**environment, "RESOLVE_CUT": "", "RESOLVE_MODE": mode},
        capture_output=True, text=True, timeout=15,
    )
    assert child.returncode == 0, (child.stdout, child.stderr)
    assert child.stdout.strip().splitlines()[-1] == "completed"
    return child


@pytest.mark.parametrize("ended", (False, True), ids=("live-editor", "ended-editor"))
@pytest.mark.parametrize("action,cut", (
    ("save_as", "accepted"), ("save_as", "claim"), ("save_as", "registration"),
    ("overwrite", "accepted"), ("overwrite", "lineage-before"), ("overwrite", "lineage"),
    ("overwrite", "replacement"), ("overwrite", "registration"),
))
@pytest.mark.parametrize("recovery", ("startup", "files-first"))
def test_sigkill_resolve_recovers_bound_action_identity_history_and_single_revision(
    office_world, monkeypatch, ended, action, cut, recovery,
):
    http, data, _origin, _manager, broker, original, session = _conflict(
        office_world, monkeypatch, final=ended,
    )
    before = _read(data)
    revision = broker.OutputsBroker().current_revision(CHAT)
    environment = _kill_resolve(data, session, action, cut)
    interrupted = _read(data)
    entry, = interrupted["journal"].values()
    assert (entry["requester"], entry["action"], entry["version"], entry["source_sha256"], entry["save_seq"]) == (
        "resolve", action, 2, _sha(CHANGED), 1,
    )
    if cut == "lineage":
        assert entry["restore_version"] == 4
        assert interrupted["documents"][session["file_id"]]["versions"][-1]["sha256"] == _sha(CHANGED)
    path = _outputs(data) / ("report (2).docx" if action == "save_as" else "report.docx")
    exposed_inode = path.stat().st_ino if cut in ("claim", "replacement", "registration") else None
    if recovery == "startup":
        _fresh_resolve(environment, mode="startup")
    else:
        listing = _fresh_files(data)
        assert listing.status_code == 200
    after = _assert_resolved(data, session, ended=ended)
    assert after["receipts"] == before["receipts"]
    assert after["sessions"][session["session_id"]]["save_seq"] == 1
    assert path.read_bytes() == CHANGED
    if exposed_inode is not None:
        assert path.stat().st_ino == exposed_inode
    index = json.loads((data / CHAT / ".ocu" / "index.json").read_bytes())
    target_entry = index["active"][path.name]
    assert (target_entry["file_id"], target_entry["hash"], target_entry["size"], target_entry["revision"]) == (
        after["sessions"][session["session_id"]]["file_id"], _sha(CHANGED), len(CHANGED), revision + 1,
    )
    if recovery == "files-first" and action == "save_as":
        original_entry = index["active"]["report.docx"]
        agent_content = original + b"agent-workspace"
        assert (original_entry["file_id"], original_entry["hash"], original_entry["size"]) == (
            session["file_id"], _sha(agent_content), len(agent_content),
        )
        assert original_entry["revision"] > target_entry["revision"]
        assert broker.OutputsBroker().current_revision(CHAT) == original_entry["revision"]
    else:
        assert broker.OutputsBroker().current_revision(CHAT) == target_entry["revision"]
    if action == "save_as":
        assert after["documents"][session["file_id"]] == before["documents"][session["file_id"]]
        assert (_outputs(data) / "report.docx").read_bytes() == original + b"agent-workspace"
        assert sorted(p.name for p in _outputs(data).iterdir() if not p.name.startswith(".")) == ["report (2).docx", "report.docx"]
    else:
        assert [v["source"] for v in after["documents"][session["file_id"]]["versions"]] == ["workspace", "save" if not ended else "close", "workspace", "restore"]
    frozen = _snapshot(data)
    _fresh_resolve(environment)
    assert _snapshot(data) == frozen
    _assert_refusal(_resolve(http, session, action), 409, "not_in_conflict")
    assert _snapshot(data) == frozen


@pytest.mark.parametrize("cut", ("accepted", "lineage", "replacement"))
def test_recovery_captures_intervening_agent_content_before_overwrite(office_world, monkeypatch, cut):
    http, data, _origin, _manager, _broker, original, session = _conflict(office_world, monkeypatch)
    environment = _kill_resolve(data, session, "overwrite", cut)
    intervening = original + b"agent-after-crash"
    (_outputs(data) / "report.docx").write_bytes(intervening)
    _fresh_resolve(environment)
    state = _assert_resolved(data, session)
    document = state["documents"][session["file_id"]]
    assert document["versions"][-2]["source"] == "workspace"
    assert document["versions"][-2]["sha256"] == _sha(intervening)
    assert (_versions(data) / _sha(intervening)).read_bytes() == intervening
    assert document["versions"][-1]["source"] == "restore"
    assert document["versions"][-1]["parent"] == 2
    assert (_outputs(data) / "report.docx").read_bytes() == CHANGED
    frozen = _snapshot(data)
    _fresh_resolve(environment)
    assert _snapshot(data) == frozen


def test_changed_action_retry_drives_accepted_copy_instead_of_overwriting_original(office_world, monkeypatch):
    http, data, _origin, _manager, _broker, original, session = _conflict(office_world, monkeypatch)
    _kill_resolve(data, session, "save_as", "accepted")
    response = _resolve(http, session, "overwrite")
    assert response.status_code == 200 and response.json()["path"] == "report (2).docx"
    assert (_outputs(data) / "report.docx").read_bytes() == original + b"agent-workspace"
    assert (_outputs(data) / "report (2).docx").read_bytes() == CHANGED
    assert len(_read(data)["documents"][session["file_id"]]["versions"]) == 2


@pytest.mark.parametrize("action", ("save_as", "overwrite"))
def test_separate_workers_resolve_one_conflict_once_under_canonical_lock(office_world, monkeypatch, action):
    http, data, _origin, manager, broker, _original, session = _conflict(office_world, monkeypatch)
    environment = _child_env(
        data, RESOLVE_MARKER=str(data.parent / "unused"), RESOLVE_SESSION=session["session_id"],
        RESOLVE_ACTION=action,
    )
    revision = broker.OutputsBroker().current_revision(CHAT)
    children = []
    try:
        with manager._combined_lock(CHAT, create=False):
            for number in range(2):
                marker = data.parent / f"resolve-waiter-{number}"
                children.append(subprocess.Popen(
                    [sys.executable, "-c", _RESOLVE_WORKER], cwd=str(SERVER_DIR),
                    env={**environment, "RESOLVE_WAIT_MARKER": str(marker)},
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                ))
                _wait_marker(marker, children[-1], "resolve worker did not contend on flock")
            assert all(child.poll() is None for child in children)
        results = []
        for child in children:
            out, err = child.communicate(timeout=15)
            assert child.returncode == 0, (out, err)
            results.append(json.loads(next(line for line in out.splitlines() if line.startswith("{"))))
        assert sorted(result["status"] for result in results) == [200, 409]
        assert next(result["body"] for result in results if result["status"] == 409) == {"reason": "not_in_conflict"}
    finally:
        for child in children:
            _stop_child(child)
    state = _assert_resolved(data, session)
    assert broker.OutputsBroker().current_revision(CHAT) == revision + 1
    assert len(state["documents"]) == (2 if action == "save_as" else 1)
    assert len(state["documents"][session["file_id"]]["versions"]) == (2 if action == "save_as" else 4)


def test_resolve_lock_excludes_launch_until_complete_user_content_is_visible(office_world, monkeypatch):
    http, data, _origin, manager, _broker, _original, session = _conflict(office_world, monkeypatch)
    from tests.orchestrator.test_lifecycle import _container, _docker
    container = _container(manager._container_name(CHAT), status="exited")
    manager._docker_client = _docker([container])
    monkeypatch.setattr(manager, "ENABLE_NETWORK", True)
    monkeypatch.setattr(manager, "OCU_SANDBOX_NETWORK", "ocu-sandbox")
    monkeypatch.setattr(manager, "SANDBOX_HOST_BIND_IP", "")
    monkeypatch.delenv("OCU_SANDBOX_DNS", raising=False)
    launcher_started, sandbox_started = threading.Event(), threading.Event()
    entered, release = threading.Event(), threading.Event()
    real_replace = os.replace
    def hold_replace(source, destination, *args, **kwargs):
        if destination == "report.docx":
            entered.set()
            assert release.wait(5)
        return real_replace(source, destination, *args, **kwargs)
    result, launched = {}, []
    original_start = container.start.side_effect
    def start():
        launched.append((_outputs(data) / "report.docx").read_bytes())
        original_start()
        sandbox_started.set()
    container.start.side_effect = start
    def resolver():
        result["response"] = _resolve(http, session, "overwrite")
    def lifecycle():
        launcher_started.set()
        result["launch"] = manager.launch_sandbox(CHAT)
    with monkeypatch.context() as fault:
        fault.setattr(os, "replace", hold_replace)
        worker = threading.Thread(target=resolver)
        launcher = threading.Thread(target=lifecycle)
        worker.start()
        try:
            assert entered.wait(5)
            launcher.start()
            assert launcher_started.wait(5)
            assert not sandbox_started.wait(0.05)
            container.start.assert_not_called()
        finally:
            release.set()
            worker.join(5)
            if launcher.ident is not None:
                launcher.join(5)
    assert result["response"].status_code == 200
    assert launched == [CHANGED]
    assert result["launch"] == {"state": "running"}
    assert not worker.is_alive() and not launcher.is_alive()


@pytest.mark.parametrize("action", ("save_as", "overwrite"))
def test_killed_visible_office_successor_never_recreates_obligation_or_registration(office_world, monkeypatch, action):
    http, data, _origin, _manager, broker, _original, session = _conflict(office_world, monkeypatch)
    environment = _kill_resolve(data, session, action, "completion")
    completed = _assert_resolved(data, session)
    revision = broker.OutputsBroker().current_revision(CHAT)
    frozen = _snapshot(data)
    _fresh_resolve(environment, mode="startup")
    assert _read(data) == completed
    assert broker.OutputsBroker().current_revision(CHAT) == revision
    assert _snapshot(data) == frozen
    _assert_refusal(_resolve(http, session, action), 409, "not_in_conflict")
    assert _snapshot(data) == frozen


@pytest.mark.parametrize("action", ("save_as", "overwrite"))
def test_resolve_follows_recorded_identity_rename_and_returns_actual_path(office_world, monkeypatch, action):
    http, data, _origin, _manager, broker, _original, session = _conflict(office_world, monkeypatch)
    broker.OutputsBroker().reconcile(CHAT)
    original = _outputs(data) / "report.docx"
    original.rename(original.with_name("renamed.docx"))
    listing = broker.OutputsBroker().reconcile(CHAT)
    entry, = listing["entries"]
    assert entry["file_id"] == session["file_id"]
    response = _resolve(http, session, action)
    assert response.status_code == 200
    expected = "renamed (2).docx" if action == "save_as" else "renamed.docx"
    assert response.json()["path"] == expected
    assert (_outputs(data) / expected).read_bytes() == CHANGED
    assert not original.exists()


@pytest.mark.parametrize("root_link", (False, True), ids=("leaf-link", "root-link"))
def test_explicit_copy_preserves_symlink_targets_and_refuses_unsafe_root(office_world, monkeypatch, root_link):
    http, data, _origin, _manager, _broker, original, session = _conflict(office_world, monkeypatch)
    root = _outputs(data)
    external = data.parent / "external"
    external.mkdir()
    secret = external / "report.docx"
    secret.write_bytes(b"private-external")
    if root_link:
        root.rename(root.with_name("detached"))
        root.symlink_to(external, target_is_directory=True)
    else:
        (root / "report.docx").unlink()
        (root / "report.docx").symlink_to(secret)
    before = _read(data)
    response = _resolve(http, session, "save_as")
    if root_link:
        _assert_refusal(response, 503, "unsafe_path")
        assert _read(data)["sessions"][session["session_id"]]["state"] == "conflict"
        assert not (external / "report (2).docx").exists()
    else:
        assert response.status_code == 200
        assert (root / "report.docx").is_symlink()
        assert (root / "report (2).docx").read_bytes() == CHANGED
    assert _read(data)["documents"][session["file_id"]] == before["documents"][session["file_id"]]
    assert secret.read_bytes() == b"private-external"


def test_fresh_files_refuses_foreign_equal_content_copy_substitution(office_world, monkeypatch):
    http, data, _origin, _manager, broker, original, session = _conflict(office_world, monkeypatch)
    _kill_resolve(data, session, "save_as", "claim")
    owned = _outputs(data) / "report (2).docx"
    retained = data.parent / "retained-owned.docx"
    owned.rename(retained)
    owned.write_bytes(CHANGED)
    before = _read(data)
    revision = broker.OutputsBroker().current_revision(CHAT)
    response = _fresh_files(data)
    assert response.status_code == 503
    assert _read(data)["journal"] == before["journal"]
    assert _read(data)["documents"] == before["documents"]
    assert broker.OutputsBroker().current_revision(CHAT) == revision
    assert owned.read_bytes() == CHANGED
    assert retained.read_bytes() == CHANGED
    assert (_outputs(data) / "report.docx").read_bytes() == original + b"agent-workspace"
    assert not (_outputs(data) / "report (3).docx").exists()


def test_running_overwrite_reads_agent_content_only_after_observed_pause(office_world, monkeypatch):
    http, data, _origin, manager, _broker, _original, session = _conflict(office_world, monkeypatch)
    container = _running(manager)
    target = (_outputs(data) / "report.docx").stat()
    identity = target.st_dev, target.st_ino
    reads = []
    real_read = os.read
    def read(fd, size):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == identity:
            reads.append(container.attrs["State"]["Paused"])
            assert container.attrs["State"]["Paused"] is True
        return real_read(fd, size)
    with monkeypatch.context() as boundary:
        boundary.setattr(os, "read", read)
        response = _resolve(http, session, "overwrite")
    assert response.status_code == 200
    assert reads and all(reads)
    assert (_outputs(data) / "report.docx").read_bytes() == CHANGED


@pytest.mark.parametrize("ended", (False, True))
def test_epoch_refusal_drives_preexisting_resolve_but_grants_no_new_action(office_world, monkeypatch, ended):
    http, data, _origin, _manager, _broker, original, session = _conflict(
        office_world, monkeypatch, final=ended,
    )
    before = _read(data)
    _kill_resolve(data, session, "save_as", "accepted")
    (data / ".office-restore-epoch").write_text("restored-after-acceptance\n")
    _assert_refusal(_resolve(http, session, "overwrite"), 409, "not_in_conflict")
    after = _read(data)
    record = after["sessions"][session["session_id"]]
    assert record["state"] == ("closed" if ended else "orphaned")
    assert record["reason"] == (None if ended else "restore_epoch_changed")
    assert record["last_published_seq"] == record["last_committed_seq"] == 1
    assert after["receipts"] == before["receipts"]
    assert after["documents"][session["file_id"]] == before["documents"][session["file_id"]]
    assert after["journal"] == {}
    assert (_outputs(data) / "report (2).docx").read_bytes() == CHANGED
    assert (_outputs(data) / "report.docx").read_bytes() == original + b"agent-workspace"


@pytest.mark.parametrize("ended", (False, True))
def test_receipt_replay_after_resolve_copy_never_republishes_original(office_world, monkeypatch, ended):
    http, data, _origin, _manager, broker, original, session = _conflict(
        office_world, monkeypatch, final=ended,
    )
    response = _resolve(http, session, "save_as")
    assert response.status_code == 200
    before = _snapshot(data)
    revision = broker.OutputsBroker().current_revision(CHAT)
    with _content_origin({"/repeat.docx": CHANGED}) as origin, _bind_internal(monkeypatch, origin.url):
        replay = _post(http, session, _payload(session, origin.url + "/repeat.docx", final=ended))
    assert replay.status_code == 200 and replay.json() == {"error": 0}
    assert _snapshot(data) == before
    assert broker.OutputsBroker().current_revision(CHAT) == revision
    assert (_outputs(data) / "report.docx").read_bytes() == original + b"agent-workspace"
    assert (_outputs(data) / "report (2).docx").read_bytes() == CHANGED


def test_later_equal_content_obligation_cannot_impersonate_resolve_registration(office_world, monkeypatch):
    http, data, _origin, _manager, broker, _original, session = _late_save_conflict(
        office_world, monkeypatch, "publish",
    )
    revision = broker.OutputsBroker().current_revision(CHAT)
    _kill_resolve(data, session, "overwrite", "replacement")
    real_sync = os.fsync
    def committed(fd):
        real_sync(fd)
        if any(entry.get("requester") == "save" and entry.get("save_seq") == 2
               for entry in _read(data)["journal"].values()):
            raise OSError(errno.EIO, "cut after later callback obligation durability")
    with _content_origin({"/late.docx": CHANGED}) as origin, _bind_internal(monkeypatch, origin.url):
        payload = _payload(session, origin.url + "/late.docx", save_seq=2)
        with monkeypatch.context() as boundary:
            boundary.setattr(os, "fsync", committed)
            late = _post(http, session, payload)
        _assert_refusal(late, 500, "state_durability")
        pending = _read(data)
        entry, = pending["journal"].values()
        assert (entry["requester"], entry["save_seq"]) == ("save", 2)
        assert broker.OutputsBroker().current_revision(CHAT) == revision + 1
        assert _post(http, session, payload).json() == {"error": 0}
    after = _assert_resolved(data, session)
    assert after["receipts"] == pending["receipts"]
    assert after["sessions"][session["session_id"]]["last_published_seq"] == 2
    assert broker.OutputsBroker().current_revision(CHAT) == revision + 2
    assert len(after["documents"][session["file_id"]]["versions"]) == 4
    assert (_outputs(data) / "report.docx").read_bytes() == CHANGED


def test_unexpected_replace_io_error_remains_visible_and_retains_bound_user_lineage(office_world, monkeypatch):
    http, data, _origin, _manager, _broker, original, session = _conflict(office_world, monkeypatch)
    real_replace = os.replace
    def fail_replace(source, destination, *args, **kwargs):
        if destination == "report.docx":
            raise OSError(errno.EIO, "workspace replacement failed")
        return real_replace(source, destination, *args, **kwargs)
    with monkeypatch.context() as boundary:
        boundary.setattr(http._transport, "raise_server_exceptions", False)
        boundary.setattr(os, "replace", fail_replace)
        response = _resolve(http, session, "overwrite")
    assert response.status_code == 500
    failed = _read(data)
    entry, = failed["journal"].values()
    assert entry["action"] == "overwrite" and entry["source_sha256"] == _sha(CHANGED)
    assert failed["documents"][session["file_id"]]["versions"][-1]["sha256"] == _sha(CHANGED)
    assert (_outputs(data) / "report.docx").read_bytes() == original + b"agent-workspace"
    assert _resolve(http, session, "overwrite").status_code == 200
    after = _assert_resolved(data, session)
    assert len(after["documents"][session["file_id"]]["versions"]) == 4
    assert (_outputs(data) / "report.docx").read_bytes() == CHANGED


def _late_save_conflict(world, monkeypatch, intent):
    from office import config
    from office.sweep import sweep_office_sessions

    opened = _opened(world)
    http, data, _origin, _manager, _broker, original, session = opened
    _allocate(http, session, monkeypatch)
    started = _read(data)["sessions"][session["session_id"]]["saving_started_at"]
    with _info(monkeypatch, session["document_key"]):
        sweep_office_sessions(now=started + config.SAVE_CALLBACK_TIMEOUT_SECONDS + 1)
    timed_out = _read(data)["sessions"][session["session_id"]]
    assert (timed_out["state"], timed_out["reason"]) == ("editing", "save_timeout")
    with _forcesave(monkeypatch, session["document_key"]):
        second = _save(http, session["session_id"], intent)
    assert second.status_code == 202 and second.json()["save_seq"] == 2
    (_outputs(data) / "report.docx").write_bytes(original + b"agent-workspace")
    with _content_origin({"/delayed.docx": CHANGED}) as origin, _bind_internal(monkeypatch, origin.url):
        assert _post(http, session, _payload(session, origin.url + "/delayed.docx")).json() == {"error": 0}
    current = _read(data)["sessions"][session["session_id"]]
    assert (current["state"], current["pending_save_seq"], current["last_committed_seq"]) == ("conflict", 2, 1)
    return opened


def _late_payload(session, url, kind):
    if kind == "final4":
        return recorded_status_4_payload(document_key=session["document_key"])
    if kind == "final2":
        return _payload(session, url, final=True)
    return {
        **_payload(session, url, save_seq=2),
        "userdata": json.dumps({"save_seq": 2, "intent": kind}),
    }


def _fresh_callback(environment, session, payload, origin):
    from tests.orchestrator.test_office_control_plane import _header_jwt

    child = _fresh_resolve({
        **environment,
        "CALLBACK_TOKEN": _header_jwt(session, extra=payload),
        "OCU_OFFICE_DOCSERVER_URL": origin or environment["OCU_OFFICE_DOCSERVER_URL"],
    }, mode="callback")
    result, = [line.removeprefix("callback_result=") for line in child.stdout.splitlines()
               if line.startswith("callback_result=")]
    return json.loads(result)


@pytest.mark.parametrize("kind", ("final2", "final4", "publish", "persist"))
@pytest.mark.parametrize("action,cut", (
    ("save_as", "accepted"), ("save_as", "claim"), ("save_as", "registration"),
    ("overwrite", "accepted"), ("overwrite", "lineage-before"), ("overwrite", "lineage"),
    ("overwrite", "replacement"), ("overwrite", "registration"),
))
def test_callback_first_after_sigkill_resolve_keeps_current_user_content_and_receipt_binding(
    office_world, monkeypatch, kind, action, cut,
):
    opened = (_late_save_conflict(office_world, monkeypatch, kind)
              if kind in ("publish", "persist") else _conflict(office_world, monkeypatch))
    http, data, _origin, _manager, broker, original, session = opened
    before = _read(data)
    revision = broker.OutputsBroker().current_revision(CHAT)
    environment = _kill_resolve(data, session, action, cut)
    newest = CHANGED if kind == "final4" else CHANGED + b"newer-user"
    with _content_origin({"/newer.docx": newest}) as origin:
        payload = _late_payload(session, origin.url + "/newer.docx", kind)
        # No startup, poll, Files or session request precedes this fresh worker's callback.
        answer = _fresh_callback(environment, session, payload, origin.url)
        assert answer == {"status": 200, "body": {"error": 0}}
        after = _read(data)
        record = after["sessions"][session["session_id"]]
        current_id = record["file_id"]
        document = after["documents"][current_id]
        latest = document["versions"][-1]
        assert record["document_key"] == session["document_key"]
        assert (record["state"], record["reason"], record["save_seq"], record["last_committed_seq"]) == (
            "closed" if kind.startswith("final") else "editing", None, 2, 2,
        )
        assert record["pending_save_seq"] is None
        assert record["last_published_seq"] == (1 if kind == "persist" else 2)
        assert record["baseline_sha256"] == _sha(CHANGED if kind == "persist" else newest)
        assert after["journal"] == {}
        assert (latest["sha256"], latest["published"]) == (_sha(newest), kind != "persist")
        assert (_versions(data) / latest["sha256"]).read_bytes() == newest
        assert after["receipts"][session["session_id"]]["1"] == before["receipts"][session["session_id"]]["1"]
        second = after["receipts"][session["session_id"]]["2"]
        assert second == {
            "status": 4 if kind == "final4" else 2 if kind == "final2" else 6,
            "sha256": None if kind == "final4" else _sha(newest),
            "version": None if kind == "final4" else latest["number"],
            "answer": {"error": 0},
        }
        path = "report (2).docx" if action == "save_as" else "report.docx"
        expected_workspace = CHANGED if kind == "persist" else newest
        assert (_outputs(data) / path).read_bytes() == expected_workspace
        if action == "save_as":
            assert current_id != session["file_id"]
            assert record["saved_as"] == {"file_id": current_id, "path": path}
            assert after["documents"][session["file_id"]] == before["documents"][session["file_id"]]
            assert (_outputs(data) / "report.docx").read_bytes() == original + b"agent-workspace"
            assert [(v["number"], v["source"], v["parent"], v["sha256"]) for v in document["versions"]] == (
                [(1, "conflict", None, _sha(CHANGED))]
                + ([] if kind == "final4" else [(2, "close" if kind == "final2" else "autosave" if kind == "persist" else "save", 1, _sha(newest))])
            )
        else:
            assert current_id == session["file_id"] and record["saved_as"] is None
            assert document["versions"][0] == before["documents"][current_id]["versions"][0]
            assert document["versions"][1] == {**before["documents"][current_id]["versions"][1], "published": True}
            assert [(v["source"], v["parent"], v["sha256"], v["published"]) for v in document["versions"][2:4]] == [
                ("workspace", 2, _sha(original + b"agent-workspace"), True),
                ("restore", 2, _sha(CHANGED), True),
            ]
            assert (_versions(data) / _sha(original + b"agent-workspace")).read_bytes() == original + b"agent-workspace"
            if kind != "final4":
                assert (latest["number"], latest["parent"]) == (5, 4)
        assert document["published_version"] == (document["versions"][-2]["number"] if kind == "persist" else latest["number"])
        index = json.loads((data / CHAT / ".ocu" / "index.json").read_bytes())
        target = index["active"][path]
        assert (target["file_id"], target["hash"], target["size"], target["revision"]) == (
            current_id, _sha(expected_workspace), len(expected_workspace),
            revision + (2 if kind in ("final2", "publish") else 1),
        )
        listing = http.get(f"/api/outputs/{CHAT}", headers=_auth())
        assert listing.status_code == 200
        entries = {entry["path"]: entry["file_id"] for entry in listing.json()["files"]}
        assert entries[path] == current_id
        assert entries["report.docx"] == session["file_id"]
        assert sorted(p.name for p in _outputs(data).iterdir() if not p.name.startswith(".")) == (
            ["report (2).docx", "report.docx"] if action == "save_as" else ["report.docx"]
        )
        status = _status(http, session["session_id"])
        assert status.status_code == 200
        projected = status.json()
        assert (projected["file_id"], projected["document_key"], projected["state"]) == (
            current_id, session["document_key"], record["state"],
        )
        assert (projected["last_committed_seq"], projected["last_published_seq"]) == (
            2, 1 if kind == "persist" else 2,
        )
        settled = _snapshot(data)
        settled_revision = broker.OutputsBroker().current_revision(CHAT)
        assert _fresh_callback(environment, session, payload, origin.url) == answer
        _fresh_resolve(environment, mode="startup")
        _fresh_resolve(environment)
        assert _snapshot(data) == settled
        assert broker.OutputsBroker().current_revision(CHAT) == settled_revision
    with _info(monkeypatch, session["document_key"]):
        joined = _create(http, current_id)
    assert joined.status_code == (201 if kind.startswith("final") else 200)
    source_path = urlsplit(joined.json()["editor_config"]["document"]["url"]).path
    assert http.get(source_path).content == newest
    if not kind.startswith("final"):
        history = _read(data)["documents"][current_id]["versions"]
        with _forcesave(monkeypatch, session["document_key"], code=4):
            nothing_new = _save(http, session["session_id"], "publish")
        assert nothing_new.status_code == 202 and nothing_new.json()["save_seq"] == 3
        final = _post(http, session, recorded_status_4_payload(document_key=session["document_key"]))
        assert final.status_code == 200 and final.json() == {"error": 0}
        closed = _assert_resolved(data, session, ended=True, content=newest)
        assert closed["documents"][current_id]["versions"] == [
            {**version, "published": True} for version in history
        ]
        assert closed["sessions"][session["session_id"]]["last_published_seq"] == 4
        assert (_outputs(data) / path).read_bytes() == newest


@pytest.mark.parametrize("action", ("save_as", "overwrite"))
def test_rejected_or_receipt_only_callbacks_cannot_drive_an_unrelated_accepted_resolve(office_world, monkeypatch, action):
    http, data, _origin, _manager, broker, _original, session = _late_save_conflict(
        office_world, monkeypatch, "publish",
    )
    _kill_resolve(data, session, action, "accepted")
    revision = broker.OutputsBroker().current_revision(CHAT)
    with _content_origin({"/same.docx": CHANGED, "/different.docx": CHANGED + b"different", "/bad.docx": b"not-ooxml"}) as origin, _bind_internal(monkeypatch, origin.url):
        same = _payload(session, origin.url + "/same.docx")
        incoming = _late_payload(session, origin.url + "/different.docx", "publish")
        invalid = (
            ({**incoming, "key": "foreign-document-key"}, 401, "invalid_token"),
            ({**incoming, "status": True}, 422, "unknown_status"),
            ({**incoming, "status": 99}, 422, "unknown_status"),
            ({**incoming, "userdata": "not-json"}, 422, "invalid_userdata"),
            ({**incoming, "userdata": json.dumps({"save_seq": 2, "intent": "persist"})}, 422, "invalid_userdata"),
            ({**incoming, "userdata": json.dumps({"save_seq": 3, "intent": "publish"})}, 422, "invalid_userdata"),
            ({**same, "status": 7}, 409, "stale_save_seq"),
            ({**same, "url": origin.url + "/different.docx"}, 409, "stale_save_seq"),
            (_payload(session, origin.url + "/bad.docx", final=True), 422, "invalid_content"),
            (_payload(session, "http://foreign.invalid/content.docx", final=True), 422, "download_url_rejected"),
        )
        frozen = _snapshot(data)
        for payload, status, reason in invalid:
            _assert_refusal(_post(http, session, payload), status, reason)
            assert _snapshot(data) == frozen
        unsigned = http.post(f"/office/callback/{CHAT}/{session['session_id']}", json=incoming)
        _assert_refusal(unsigned, 401, "invalid_token")
        assert _snapshot(data) == frozen
        assert _post(http, session, same).json() == {"error": 0}
        assert _snapshot(data) == frozen
    assert broker.OutputsBroker().current_revision(CHAT) == revision
    entry, = _read(data)["journal"].values()
    assert (entry["requester"], entry["action"], entry["save_seq"]) == ("resolve", action, 1)


@pytest.mark.parametrize("action", ("save_as", "overwrite"))
def test_stale_issued_callback_without_receipt_cannot_finish_resolve(office_world, monkeypatch, action):
    from office import config
    from office.sweep import sweep_office_sessions

    http, data, _origin, _manager, _broker, original, session = _opened(office_world)
    _allocate(http, session, monkeypatch)
    started = _read(data)["sessions"][session["session_id"]]["saving_started_at"]
    with _info(monkeypatch, session["document_key"]):
        sweep_office_sessions(now=started + config.SAVE_CALLBACK_TIMEOUT_SECONDS + 1)
    with _forcesave(monkeypatch, session["document_key"]):
        assert _save(http, session["session_id"]).json()["save_seq"] == 2
    (_outputs(data) / "report.docx").write_bytes(original + b"agent-workspace")
    with _content_origin({"/second.docx": CHANGED}) as origin, _bind_internal(monkeypatch, origin.url):
        assert _post(http, session, _payload(session, origin.url + "/second.docx", save_seq=2)).json() == {"error": 0}
        _kill_resolve(data, session, action, "accepted")
        frozen = _snapshot(data)
        _assert_refusal(_post(http, session, _payload(session, origin.url + "/second.docx")), 409, "stale_save_seq")
        assert _snapshot(data) == frozen


@pytest.mark.parametrize("action", ("save_as", "overwrite"))
def test_final_receipt_replay_preserves_accepted_resolve_and_original_binding(office_world, monkeypatch, action):
    http, data, _origin, _manager, _broker, _original, session = _conflict(
        office_world, monkeypatch, final=True,
    )
    _kill_resolve(data, session, action, "accepted")
    frozen = _snapshot(data)
    with _content_origin({"/wrong.docx": b"not-ooxml"}) as origin, _bind_internal(monkeypatch, origin.url):
        for status in (2, 3, 4):
            response = _post(http, session, {
                "key": session["document_key"], "status": status, "url": origin.url + "/wrong.docx",
            })
            assert response.status_code == 200 and response.json() == {"error": 0}
            assert _snapshot(data) == frozen
        assert origin.hits == 0


@pytest.mark.parametrize("kind", ("final2", "final4", "publish", "persist"))
@pytest.mark.parametrize("action,obstruction", (
    ("save_as", "fence"), ("overwrite", "fence"), ("save_as", "path"), ("save_as", "index"),
))
def test_unresolved_resolve_leaves_incoming_callback_retryable_without_receipt_or_content(
    office_world, monkeypatch, kind, action, obstruction,
):
    opened = (_late_save_conflict(office_world, monkeypatch, kind)
              if kind in ("publish", "persist") else _conflict(office_world, monkeypatch))
    http, data, _origin, manager, _broker, _original, session = opened
    _kill_resolve(data, session, action, "accepted" if obstruction == "fence" else "claim")
    before = _read(data)
    copied = _outputs(data) / "report (2).docx"
    index = data / CHAT / ".ocu" / "index.json"
    if obstruction == "fence":
        container = _running(manager)
        pause = container.pause.side_effect
        container.pause.side_effect = RuntimeError("engine refuses writer exclusion")
    elif obstruction == "path":
        retained = data.parent / "owned-copy.docx"
        copied.rename(retained)
        copied.write_bytes(CHANGED)
    else:
        saved_index = index.read_bytes()
        index.write_bytes(b"{broken")
    newest = CHANGED + b"retryable-new-user"
    with _content_origin({"/incoming.docx": newest}) as origin, _bind_internal(monkeypatch, origin.url):
        payload = _late_payload(session, origin.url + "/incoming.docx", kind)
        _assert_refusal(_post(http, session, payload), 503, "publish_pending")
        pending = _read(data)
        assert pending["documents"] == before["documents"] and pending["receipts"] == before["receipts"]
        assert pending["sessions"] == before["sessions"]
        entry, = pending["journal"].values()
        assert entry["requester"] == "resolve" and entry["save_seq"] == 1
        assert not (_versions(data) / _sha(newest)).exists()
        if obstruction == "fence":
            container.pause.side_effect = pause
        elif obstruction == "path":
            copied.unlink()
            retained.rename(copied)
        else:
            index.write_bytes(saved_index)
        retry = _post(http, session, payload)
        assert retry.status_code == 200 and retry.json() == {"error": 0}
        completed = _read(data)
        current = completed["sessions"][session["session_id"]]
        latest = completed["documents"][current["file_id"]]["versions"][-1]
        assert (latest["sha256"], latest["published"]) == (
            _sha(CHANGED if kind == "final4" else newest), kind != "persist",
        )
        assert current["last_committed_seq"] == 2
        assert current["last_published_seq"] == (1 if kind == "persist" else 2)
        assert completed["journal"] == {}
        frozen = _snapshot(data)
        assert _post(http, session, payload).json() == {"error": 0}
        assert _snapshot(data) == frozen


@pytest.mark.parametrize("status", (1, 3, 7))
@pytest.mark.parametrize("action", ("save_as", "overwrite"))
def test_lifecycle_callback_first_finishes_resolve_before_participants_or_failure_receipt(
    office_world, monkeypatch, status, action,
):
    http, data, _origin, _manager, _broker, original, session = _late_save_conflict(
        office_world, monkeypatch, "publish",
    )
    before = _read(data)
    environment = _kill_resolve(data, session, action, "accepted")
    payload = {
        "key": session["document_key"], "status": status, "users": ["remaining-editor"],
        "userdata": json.dumps({"save_seq": 2, "intent": "publish"}),
    }
    assert _fresh_callback(environment, session, payload, "") == {"status": 200, "body": {"error": 0}}
    after = _read(data)
    record = after["sessions"][session["session_id"]]
    assert after["journal"] == {}
    assert record["document_key"] == session["document_key"]
    assert (record["state"], record["reason"]) == (
        ("error", "final_save_failed") if status == 3 else ("editing", None)
    )
    assert record["last_committed_seq"] == record["last_published_seq"] == 1
    assert after["receipts"][session["session_id"]]["1"] == before["receipts"][session["session_id"]]["1"]
    if status == 1:
        assert record["participants"] == ["remaining-editor"]
        assert after["receipts"] == before["receipts"] and record["pending_save_seq"] == 2
    else:
        assert after["receipts"][session["session_id"]]["3" if status == 3 else "2"] == {
            "status": status, "sha256": None, "version": None, "answer": {"error": 0},
        }
        if status == 7:
            assert record["pending_save_seq"] is None
    assert after["documents"][record["file_id"]]["versions"][-1]["sha256"] == _sha(CHANGED)
    if action == "save_as":
        assert after["documents"][session["file_id"]] == before["documents"][session["file_id"]]
        assert (_outputs(data) / "report.docx").read_bytes() == original + b"agent-workspace"
    else:
        assert [v["source"] for v in after["documents"][record["file_id"]]["versions"]] == [
            "workspace", "save", "workspace", "restore",
        ]


@pytest.mark.parametrize("action", ("save_as", "overwrite"))
def test_old_epoch_callback_recovers_preexisting_resolve_before_orphaning_without_new_receipt(
    office_world, monkeypatch, action,
):
    http, data, _origin, _manager, _broker, _original, session = _conflict(office_world, monkeypatch)
    before = _read(data)
    _kill_resolve(data, session, action, "accepted")
    (data / ".office-restore-epoch").write_text("restored-after-acceptance\n")
    with _content_origin({"/new.docx": CHANGED + b"not-admitted"}) as origin, _bind_internal(monkeypatch, origin.url):
        _assert_refusal(_post(http, session, _payload(session, origin.url + "/new.docx", final=True)), 409, "session_not_open")
        assert origin.hits == 0
    after = _read(data)
    record = after["sessions"][session["session_id"]]
    assert (record["state"], record["reason"]) == ("orphaned", "restore_epoch_changed")
    assert after["receipts"] == before["receipts"] and after["journal"] == {}
    assert record["last_published_seq"] == record["last_committed_seq"] == 1
    assert after["documents"][record["file_id"]]["versions"][-1]["sha256"] == _sha(CHANGED)


@pytest.mark.parametrize("action", ("save_as", "overwrite"))
@pytest.mark.parametrize("kind", ("final2", "final4", "publish", "persist"))
@pytest.mark.parametrize("error,status,reason", (
    (errno.EIO, 500, "state_corrupt"), (errno.ENOSPC, 503, "publish_pending"),
))
def test_resolve_registration_io_cut_does_not_consume_incoming_callback(
    office_world, monkeypatch, action, kind, error, status, reason,
):
    opened = (_late_save_conflict(office_world, monkeypatch, kind)
              if kind in ("publish", "persist") else _conflict(office_world, monkeypatch))
    http, data, _origin, _manager, broker, _original, session = opened
    _kill_resolve(data, session, action, "accepted")
    before = _read(data)
    revision = broker.OutputsBroker().current_revision(CHAT)
    real_replace = os.replace
    def interrupted(source, destination, *args, **kwargs):
        result = real_replace(source, destination, *args, **kwargs)
        if destination == "index.json":
            raise OSError(error, "interrupted after real resolve registration replace")
        return result
    newest = CHANGED + b"registration-cut-user"
    with _content_origin({"/incoming.docx": newest}) as origin, _bind_internal(monkeypatch, origin.url):
        payload = _late_payload(session, origin.url + "/incoming.docx", kind)
        with monkeypatch.context() as boundary:
            boundary.setattr(os, "replace", interrupted)
            _assert_refusal(_post(http, session, payload), status, reason)
        pending = _read(data)
        assert pending["receipts"] == before["receipts"]
        assert pending["sessions"] == before["sessions"]
        entry, = pending["journal"].values()
        assert (entry["requester"], entry["action"], entry["save_seq"]) == ("resolve", action, 1)
        assert not (_versions(data) / _sha(newest)).exists()
        assert pending["documents"][session["file_id"]]["versions"][-1]["sha256"] == _sha(CHANGED)
        retry = _post(http, session, payload)
        assert retry.status_code == 200 and retry.json() == {"error": 0}
        after = _read(data)
        current = after["sessions"][session["session_id"]]
        latest = after["documents"][current["file_id"]]["versions"][-1]
        assert (latest["sha256"], latest["published"]) == (
            _sha(CHANGED if kind == "final4" else newest), kind != "persist",
        )
        assert current["last_committed_seq"] == 2
        assert current["last_published_seq"] == (1 if kind == "persist" else 2)
        assert after["journal"] == {}
        assert broker.OutputsBroker().current_revision(CHAT) == revision + (
            2 if kind in ("final2", "publish") else 1
        )
        frozen = _snapshot(data)
        assert _post(http, session, payload).json() == {"error": 0}
        assert _snapshot(data) == frozen


def _timeout_first_save(world, monkeypatch):
    from office import config
    from office.sweep import sweep_office_sessions

    opened = _opened(world)
    http, data, _origin, _manager, _broker, _original, session = opened
    _allocate(http, session, monkeypatch)
    started = _read(data)["sessions"][session["session_id"]]["saving_started_at"]
    with _info(monkeypatch, session["document_key"]):
        sweep_office_sessions(now=started + config.SAVE_CALLBACK_TIMEOUT_SECONDS + 1)
    record = _read(data)["sessions"][session["session_id"]]
    assert (record["state"], record["save_seq"], record["last_committed_seq"]) == ("editing", 1, 0)
    return opened


@contextmanager
def _resolve_http_worker(data, session, action, origin, *, cut=""):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    marker = data.parent / ("http-resolve-" + (cut or "live"))
    environment = _child_env(
        data, OCU_PORT=str(port), RESOLVE_MODE="http", RESOLVE_CUT=cut,
        RESOLVE_MARKER=str(marker), RESOLVE_SESSION=session["session_id"],
        RESOLVE_ACTION=action, OCU_OFFICE_DOCSERVER_URL=origin,
    )
    child = subprocess.Popen(
        [sys.executable, "-c", _RESOLVE_WORKER], cwd=str(SERVER_DIR), env=environment,
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
                    pytest.fail("resolve HTTP worker did not bind")
                time.sleep(0.01)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15) as http:
            yield http, child, marker, environment
    finally:
        _stop_child(child)


@pytest.mark.parametrize("intent", ("publish", "persist"))
@pytest.mark.parametrize("action", ("save_as", "overwrite"))
def test_two_http_workers_nothing_new_command_settles_sigkill_resolve_before_completion(
    office_world, monkeypatch, intent, action,
):
    from tests.orchestrator.test_office_commands import _command_origin, _independent_verify
    from tests.orchestrator.test_office_control_plane import _header_jwt
    from tests.orchestrator.test_office_sessions import JWT_SECRET

    _http, data, _origin, _manager, broker, original, session = _timeout_first_save(
        office_world, monkeypatch,
    )
    entered, release = threading.Event(), threading.Event()
    command_errors, seen = [], []
    held_at = []

    def responder(handler, body, _parent):
        try:
            assert handler.path == "/command"
            payload = _independent_verify(json.loads(body)["token"], JWT_SECRET)
            assert payload["key"] == session["document_key"] and payload["c"] == "forcesave"
            seen.append(json.loads(payload["userdata"]))
            held_at.append(time.monotonic())
            entered.set()
            assert release.wait(8), "command response was not released within its production timeout"
            return 200, {"Content-Type": "application/json"}, b'{"error":4}'
        except Exception as extra:
            command_errors.append(extra)
            raise

    with _command_origin(responder, trap=(200, {}, CHANGED)) as origin:
        with _resolve_http_worker(data, session, action, origin.url) as (live, _live_child, _marker, _environment), \
                _resolve_http_worker(data, session, action, origin.url, cut="accepted") as (resolver, child, marker, environment):
            results = {}
            saver = threading.Thread(target=lambda: results.setdefault(
                "save", _save(live, session["session_id"], intent),
            ))
            def resolve():
                try:
                    results["resolve"] = _resolve(resolver, session, action)
                except httpx.TransportError:
                    # The crash may abort transport; the marker and child exit prove it.
                    pass
            cutter = threading.Thread(target=resolve)
            saver.start()
            try:
                assert entered.wait(5), "live worker never issued the second command"
                assert seen == [{"save_seq": 2, "intent": intent}]
                (_outputs(data) / "report.docx").write_bytes(original + b"agent-workspace")
                payload = _payload(session, origin.url + "/delayed.docx")
                callback = live.post(
                    f"/office/callback/{CHAT}/{session['session_id']}",
                    headers={"Authorization": "Bearer " + _header_jwt(session, extra=payload)}, json={},
                )
                assert callback.status_code == 200 and callback.json() == {"error": 0}
                before = _read(data)
                record = before["sessions"][session["session_id"]]
                assert (record["state"], record["pending_save_seq"], record["last_committed_seq"]) == ("conflict", 2, 1)
                revision = broker.OutputsBroker().current_revision(CHAT)
                cutter.start()
                _wait_marker(marker, child, "separate HTTP worker never durably accepted resolve")
                assert marker.read_text() == "accepted"
                accepted = _read(data)
                entry, = accepted["journal"].values()
                assert (entry["requester"], entry["action"], entry["save_seq"], entry["file_id"]) == (
                    "resolve", action, 1, session["file_id"],
                )
                child.kill()
                child.communicate(timeout=5)
                assert child.returncode == -9
                cutter.join(timeout=5)
                assert not cutter.is_alive()
                # Command completion is the first mutation after the process cut.
                assert time.monotonic() - held_at[0] < 10
                release.set()
                saver.join(timeout=10)
                assert not saver.is_alive() and not command_errors
                response = results["save"]
                assert response.status_code == 202, response.text
                assert response.json() == {"session_id": session["session_id"], "save_seq": 2, "intent": intent}
                after = _assert_resolved(data, session)
                record = after["sessions"][session["session_id"]]
                current_id = record["file_id"]
                document = after["documents"][current_id]
                latest = document["versions"][-1]
                path = "report (2).docx" if action == "save_as" else "report.docx"
                assert (record["save_seq"], record["last_committed_seq"], record["last_published_seq"]) == (2, 2, 2)
                assert record["pending_save_seq"] is None
                assert after["receipts"] == before["receipts"]
                assert after["receipts"][session["session_id"]]["1"]["version"] == 2
                assert after["receipts"][session["session_id"]]["1"]["sha256"] == _sha(CHANGED)
                assert (latest["sha256"], latest["published"], document["published_version"]) == (
                    _sha(CHANGED), True, latest["number"],
                )
                assert (_outputs(data) / path).read_bytes() == CHANGED
                assert (_versions(data) / _sha(original)).read_bytes() == original
                assert (_versions(data) / _sha(CHANGED)).read_bytes() == CHANGED
                if action == "save_as":
                    assert current_id != session["file_id"]
                    assert record["saved_as"] == {"file_id": current_id, "path": path}
                    assert after["documents"][session["file_id"]] == before["documents"][session["file_id"]]
                    assert (_outputs(data) / "report.docx").read_bytes() == original + b"agent-workspace"
                    assert [(v["number"], v["parent"], v["source"]) for v in document["versions"]] == [(1, None, "conflict")]
                else:
                    assert current_id == session["file_id"]
                    assert [(v["number"], v["parent"], v["source"], v["sha256"]) for v in document["versions"]] == [
                        (1, None, "workspace", _sha(original)),
                        (2, 1, "save", _sha(CHANGED)),
                        (3, 2, "workspace", _sha(original + b"agent-workspace")),
                        (4, 2, "restore", _sha(CHANGED)),
                    ]
                    assert (_versions(data) / _sha(original + b"agent-workspace")).read_bytes() == original + b"agent-workspace"
                index = json.loads((data / CHAT / ".ocu" / "index.json").read_bytes())
                target = index["active"][path]
                assert (target["file_id"], target["hash"], target["size"], target["revision"]) == (
                    current_id, _sha(CHANGED), len(CHANGED), revision + 1,
                )
                listing = live.get(f"/api/outputs/{CHAT}", headers=_auth())
                assert listing.status_code == 200, listing.text
                entries = {item["path"]: item["file_id"] for item in listing.json()["files"]}
                assert entries[path] == current_id and entries["report.docx"] == session["file_id"]
                settled = _snapshot(data)
                settled_revision = broker.OutputsBroker().current_revision(CHAT)
                replay = live.post(
                    f"/office/callback/{CHAT}/{session['session_id']}",
                    headers={"Authorization": "Bearer " + _header_jwt(session, extra=payload)}, json={},
                )
                assert replay.status_code == 200 and replay.json() == {"error": 0}
                _fresh_resolve(environment, mode="startup")
                _fresh_resolve(environment)
                assert _fresh_files(data).status_code == 200
                assert _snapshot(data) == settled
                assert broker.OutputsBroker().current_revision(CHAT) == settled_revision
            finally:
                release.set()
                _stop_child(child)
                saver.join(timeout=15)
                assert not saver.is_alive()
                if cutter.ident is not None:
                    cutter.join(timeout=15)
                    assert not cutter.is_alive()


@contextmanager
def _held_conflict_command(world, monkeypatch, intent="publish", *, code=4, status=200):
    from tests.orchestrator.test_office_save_close import _command_box

    opened = _timeout_first_save(world, monkeypatch)
    http, data, _origin, _manager, _broker, original, session = opened
    entered, release = threading.Event(), threading.Event()
    with _command_box(
        monkeypatch, session["document_key"], forcesave_code=code, forcesave_status=status,
        entered=entered, release=release,
    ):
        saver = threading.Thread(target=lambda: setattr(saver, "response", _save(http, session["session_id"], intent)))
        saver.start()
        try:
            assert entered.wait(5)
            (_outputs(data) / "report.docx").write_bytes(original + b"agent-workspace")
            with _content_origin({"/late.docx": CHANGED}) as origin, _bind_internal(monkeypatch, origin.url):
                assert _post(http, session, _payload(session, origin.url + "/late.docx")).json() == {"error": 0}
            record = _read(data)["sessions"][session["session_id"]]
            assert (record["state"], record["pending_save_seq"], record["last_committed_seq"]) == ("conflict", 2, 1)
            yield opened, saver, release
        finally:
            release.set()
            saver.join(timeout=10)
            assert not saver.is_alive()


@pytest.mark.parametrize("code", (4, 1), ids=("nothing-new", "unknown-key"))
@pytest.mark.parametrize("intent", ("publish", "persist"))
@pytest.mark.parametrize("action,obstruction", (
    ("save_as", "fence"), ("overwrite", "fence"), ("save_as", "path"), ("save_as", "index"),
))
def test_uncertain_resolve_retains_command_allocation_without_old_identity_journal(
    office_world, monkeypatch, intent, action, obstruction, code,
):
    with _held_conflict_command(office_world, monkeypatch, intent, code=code) as (opened, saver, release):
        http, data, _origin, manager, _broker, _original, session = opened
        environment = _kill_resolve(data, session, action, "accepted" if obstruction == "fence" else "claim")
        before = _read(data)
        copied = _outputs(data) / "report (2).docx"
        index = data / CHAT / ".ocu" / "index.json"
        if obstruction == "fence":
            container = _running(manager)
            pause = container.pause.side_effect
            container.pause.side_effect = RuntimeError("writer exclusion refused")
        elif obstruction == "path":
            retained = data.parent / "command-owned-copy.docx"
            copied.rename(retained)
            copied.write_bytes(CHANGED)
        else:
            saved_index = index.read_bytes()
            index.write_bytes(b"{broken")
        release.set()
        saver.join(timeout=10)
        _assert_refusal(saver.response, 503, "publish_pending")
        pending = _read(data)
        assert pending["sessions"] == before["sessions"]
        assert pending["documents"] == before["documents"]
        assert pending["receipts"] == before["receipts"]
        entry, = pending["journal"].values()
        assert (entry["requester"], entry["action"], entry["save_seq"], entry["file_id"]) == (
            "resolve", action, 1, session["file_id"],
        )
        assert pending["sessions"][session["session_id"]]["pending_save_seq"] == 2
        if obstruction == "fence":
            container.pause.side_effect = pause
        elif obstruction == "path":
            copied.unlink()
            retained.rename(copied)
        else:
            index.write_bytes(saved_index)
        _fresh_resolve(environment)
        with _content_origin({"/same.docx": CHANGED}) as origin, _bind_internal(monkeypatch, origin.url):
            payload = _late_payload(session, origin.url + "/same.docx", intent)
            assert _post(http, session, payload).json() == {"error": 0}
            after = _assert_resolved(data, session)
            assert after["sessions"][session["session_id"]]["last_published_seq"] == 2
            assert after["receipts"][session["session_id"]]["1"] == before["receipts"][session["session_id"]]["1"]
            frozen = _snapshot(data)
            assert _post(http, session, payload).json() == {"error": 0}
            assert _snapshot(data) == frozen


@pytest.mark.parametrize("action", ("save_as", "overwrite"))
@pytest.mark.parametrize("code,status,http_status", ((0, 200, 202), (1, 200, 409), (5, 200, 502), (0, 500, 502)))
def test_command_outcomes_preserve_resolve_responsibility_and_unknown_key_recovery(
    office_world, monkeypatch, action, code, status, http_status,
):
    with _held_conflict_command(office_world, monkeypatch, code=code, status=status) as (opened, saver, release):
        _http, data, _origin, _manager, _broker, _original, session = opened
        environment = _kill_resolve(data, session, action, "accepted")
        before = _read(data)
        release.set()
        saver.join(timeout=10)
        assert saver.response.status_code == http_status, saver.response.text
        after = _read(data)
        record = after["sessions"][session["session_id"]]
        assert after["receipts"] == before["receipts"]
        assert record["last_committed_seq"] == 1
        assert record["document_key"] == session["document_key"]
        if code == 1:
            assert (record["state"], record["reason"], record["pending_save_seq"]) == ("orphaned", "editor_state_lost", None)
            assert record["last_published_seq"] == 1 and after["journal"] == {}
            assert after["documents"][record["file_id"]]["versions"][-1]["sha256"] == _sha(CHANGED)
        else:
            assert after["documents"] == before["documents"] and after["journal"] == before["journal"]
            assert record["state"] == "conflict"
            assert record["pending_save_seq"] == (2 if code == 0 and status == 200 else None)
            _fresh_resolve(environment)
            assert _read(data)["journal"] == {}


@pytest.mark.parametrize("action", ("save_as", "overwrite"))
@pytest.mark.parametrize("code,http_status", ((4, 202), (1, 409), (5, 502)))
@pytest.mark.parametrize("transition", ("newer-pending", "final-conflict", "closed", "new-session", "error", "epoch"))
def test_delayed_command_cannot_consume_newer_or_terminal_responsibility(
    office_world, monkeypatch, action, code, http_status, transition,
):
    with _held_conflict_command(office_world, monkeypatch, code=code) as (opened, saver, release):
        http, data, _origin, _manager, _broker, _original, session = opened
        if transition == "newer-pending":
            assert _resolve(http, session, action).status_code == 200
            assert _post(http, session, {"key": session["document_key"], "status": 1, "users": ["editor"]}).json() == {"error": 0}
            with _forcesave(monkeypatch, session["document_key"]):
                assert _save(http, session["session_id"]).json()["save_seq"] == 3
            # A delayed earlier callback creates a new conflict while save3 owns admission.
            with _content_origin({"/third.docx": CHANGED + b"second-conflict"}) as origin, _bind_internal(monkeypatch, origin.url):
                (_outputs(data) / ("report (2).docx" if action == "save_as" else "report.docx")).write_bytes(CHANGED + b"agent-again")
                assert _post(http, session, _payload(session, origin.url + "/third.docx", save_seq=2)).json() == {"error": 0}
            _kill_resolve(data, session, action, "accepted")
        else:
            if transition in ("final-conflict", "closed", "new-session"):
                assert _post(http, session, recorded_status_4_payload(document_key=session["document_key"])).json() == {"error": 0}
            elif transition == "error":
                assert _post(http, session, {"key": session["document_key"], "status": 3}).json() == {"error": 0}
            if transition in ("closed", "new-session"):
                assert _resolve(http, session, action).status_code == 200
                if transition == "new-session":
                    current_id = _read(data)["sessions"][session["session_id"]]["file_id"]
                    new = _create(http, current_id)
                    assert new.status_code == 201
                    assert new.json()["document_key"] != session["document_key"]
                    assert new.json()["session_id"] != session["session_id"]
            elif transition != "error":
                _kill_resolve(data, session, action, "accepted")
            if transition == "epoch":
                (data / ".office-restore-epoch").write_text("changed-with-command-inflight\n")
        before = _read(data)
        frozen = _snapshot(data)
        release.set()
        saver.join(timeout=10)
        assert saver.response.status_code == http_status, saver.response.text
        after = _read(data)
        if transition != "epoch":
            assert _snapshot(data) == frozen
            assert after == before
            if transition == "newer-pending":
                assert after["sessions"][session["session_id"]]["pending_save_seq"] == 3
        else:
            record = after["sessions"][session["session_id"]]
            assert (record["state"], record["reason"]) == ("orphaned", "restore_epoch_changed")
            assert record["last_committed_seq"] == record["last_published_seq"] == 1
            assert record["pending_save_seq"] == 2
            assert after["receipts"] == before["receipts"] and after["journal"] == {}


@pytest.mark.parametrize("intent", ("publish", "persist"))
@pytest.mark.parametrize("action", ("save_as", "overwrite"))
@pytest.mark.parametrize("error,status,reason", (
    (errno.EIO, 500, "state_corrupt"), (errno.ENOSPC, 503, "publish_pending"),
))
def test_command_completion_registration_error_preserves_both_responsibilities(
    office_world, monkeypatch, intent, action, error, status, reason,
):
    with _held_conflict_command(office_world, monkeypatch, intent) as (opened, saver, release):
        http, data, _origin, _manager, broker, _original, session = opened
        environment = _kill_resolve(data, session, action, "accepted")
        before = _read(data)
        revision = broker.OutputsBroker().current_revision(CHAT)
        real_replace = os.replace

        def interrupted(source, destination, *args, **kwargs):
            result = real_replace(source, destination, *args, **kwargs)
            if destination == "index.json":
                raise OSError(error, "interrupted after real command-recovery registration")
            return result

        with monkeypatch.context() as boundary:
            boundary.setattr(os, "replace", interrupted)
            release.set()
            saver.join(timeout=10)
            _assert_refusal(saver.response, status, reason)
        pending = _read(data)
        assert pending["sessions"] == before["sessions"]
        assert pending["receipts"] == before["receipts"]
        entry, = pending["journal"].values()
        assert (entry["requester"], entry["action"], entry["save_seq"]) == ("resolve", action, 1)
        assert pending["sessions"][session["session_id"]]["pending_save_seq"] == 2
        _fresh_resolve(environment)
        assert broker.OutputsBroker().current_revision(CHAT) == revision + 1
        with _content_origin({"/same.docx": CHANGED}) as origin, _bind_internal(monkeypatch, origin.url):
            payload = _late_payload(session, origin.url + "/same.docx", intent)
            assert _post(http, session, payload).json() == {"error": 0}
            after = _assert_resolved(data, session)
            assert after["sessions"][session["session_id"]]["last_published_seq"] == 2
            frozen = _snapshot(data)
            assert _post(http, session, payload).json() == {"error": 0}
            assert _snapshot(data) == frozen
