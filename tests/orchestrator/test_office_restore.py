# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Public HTTP seam for restoring immutable Office history to the workspace."""
from __future__ import annotations

import hashlib
import errno
import os
import shutil

import pytest

from tests.orchestrator.test_office_callback_publish import _autosaved, _read, _running, CHANGED
from tests.orchestrator.test_office_session_lifecycle import _change, _created, _info
from tests.orchestrator.test_office_sessions import _assert_refusal, _create, _office
from tests.orchestrator.test_outputs_endpoint import CHAT_B
from urllib.parse import quote

from tests.orchestrator.test_office_callback_publish import (
    _bind_internal, _content_origin, _opened, _payload, _post,
)
from tests.orchestrator.test_office_sessions import (
    _outputs, _put, _snapshot, _versions, office_world,
)
from tests.orchestrator.test_outputs_endpoint import CHAT, _auth


def test_restore_version_two_of_five_publishes_new_version_without_changing_history(
    office_world, monkeypatch,
):
    from office.store import OfficeStore

    http, data, _origin, _manager, broker, original, session = _opened(office_world)
    file_id = session["file_id"]
    contents = (
        original,
        original + b"\nselected revision two",
        original + b"\nrevision three",
        original + b"\nrevision four",
        original + b"\nlatest revision five",
    )
    store = OfficeStore()
    for number, content in enumerate(contents[1:4], start=2):
        store.store_version(
            CHAT, file_id, content, source="autosave", parent=number - 1,
            published=False, min_free_bytes=0,
        )
    with _content_origin({"/final.docx": contents[4]}) as server, _bind_internal(monkeypatch, server.url):
        callback = _post(http, session, _payload(session, server.url + "/final.docx", final=True))
    assert callback.status_code == 200
    assert callback.json() == {"error": 0}

    workspace = _outputs(data) / "report.docx"
    document_url = f"/api/office/{CHAT}/documents/{quote(file_id, safe='')}"
    before = store.read(CHAT)
    old_versions = before["documents"][file_id]["versions"]
    assert [record["number"] for record in old_versions] == [1, 2, 3, 4, 5]
    assert [record["sha256"] for record in old_versions] == [
        hashlib.sha256(content).hexdigest() for content in contents
    ]
    assert before["documents"][file_id]["published_version"] == 5
    assert before["sessions"][session["session_id"]]["state"] == "closed"
    assert before["receipts"][session["session_id"]]["1"]["status"] == 2
    assert workspace.read_bytes() == contents[4]
    history_before = http.get(document_url + "/versions", headers=_auth())
    assert history_before.status_code == 200
    assert history_before.json()["open_session"] is None
    assert history_before.json()["published_version"] == 5
    outputs_before = http.get(f"/api/outputs/{CHAT}", headers=_auth())
    assert outputs_before.status_code == 200
    revision_before = outputs_before.json()["revision"]
    blobs_before = _snapshot(_versions(data))
    selected = contents[1]
    selected_hash = hashlib.sha256(selected).hexdigest()

    response = http.post(document_url + "/restore", headers=_auth(), json={"number": 2})

    assert response.status_code == 200
    assert response.json() == {"file_id": file_id, "number": 6, "published": True}
    after = store.read(CHAT)
    document = after["documents"][file_id]
    assert document["versions"][:5] == old_versions
    assert [record["number"] for record in document["versions"]] == [1, 2, 3, 4, 5, 6]
    expected_restore = {
        "number": 6,
        "parent": 2,
        "source": "restore",
        "sha256": selected_hash,
        "size": len(selected),
        "published": True,
    }
    assert {
        key: value for key, value in document["versions"][5].items() if key != "created_at"
    } == expected_restore
    assert document["published_version"] == 6
    assert document["published_sha256"] == selected_hash
    assert (_versions(data) / selected_hash).read_bytes() == selected
    assert _snapshot(_versions(data)) == blobs_before
    assert workspace.read_bytes() == selected
    assert after["sessions"] == before["sessions"]
    assert after["receipts"] == before["receipts"]
    assert broker.OutputsBroker().current_revision(CHAT) > revision_before

    history_after = http.get(document_url + "/versions", headers=_auth())
    assert history_after.status_code == 200
    history = history_after.json()
    assert history["file_id"] == file_id
    assert history["published_version"] == 6
    assert history["open_session"] is None
    assert history["versions"][:5] == history_before.json()["versions"]
    assert [record["number"] for record in history["versions"]] == [1, 2, 3, 4, 5, 6]
    assert {
        key: value for key, value in history["versions"][5].items() if key != "created_at"
    } == expected_restore
    outputs_after = http.get(f"/api/outputs/{CHAT}", headers=_auth())
    assert outputs_after.status_code == 200
    listing = outputs_after.json()
    assert listing["revision"] > revision_before
    entry = next(item for item in listing["files"] if item["file_id"] == file_id)
    assert entry["path"] == "report.docx"
    assert entry["hash"] == selected_hash
    assert entry["size"] == len(selected)
    final = store.read(CHAT)
    assert final["sessions"] == before["sessions"]
    assert final["receipts"] == before["receipts"]


def _restore(http, file_id, number=1, *, body=None, chat=CHAT, headers=None):
    options = {"headers": _auth() if headers is None else headers}
    if body is None:
        options["json"] = {"number": number}
    else:
        options["content"] = body
    return http.post(f"/api/office/{chat}/documents/{quote(file_id, safe='')}/restore", **options)


def _closed(world, monkeypatch, name="report.docx"):
    from tests.orchestrator.test_office_save_as import _nested
    opened = _opened(world) if name == "report.docx" else _nested(world, name)
    http, _data, _origin, _manager, _broker, original, session = opened
    with _content_origin({"/final.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        response = _post(http, session, _payload(session, server.url + "/final.docx", final=True))
    assert response.status_code == 200 and response.json() == {"error": 0}
    return opened


@pytest.mark.parametrize("published", (True, False))
def test_latest_equal_restore_always_appends_without_rewriting_source(office_world, monkeypatch, published):
    opened = _closed(office_world, monkeypatch) if published else _autosaved(office_world, monkeypatch)
    http, data, _origin, _manager, _broker, original, session = opened
    if not published:
        _change(session["session_id"], state="orphaned", reason="editor_state_lost")
    before = _read(data)
    blobs = _snapshot(_versions(data))
    response = _restore(http, session["file_id"], 2)
    assert response.status_code == 200
    assert response.json() == {"file_id": session["file_id"], "number": 3, "published": True}
    after = _read(data)
    listed = after["documents"][session["file_id"]]["versions"]
    assert listed[:2] == before["documents"][session["file_id"]]["versions"]
    assert (listed[2]["source"], listed[2]["parent"], listed[2]["sha256"], listed[2]["published"]) == (
        "restore", 2, hashlib.sha256(CHANGED).hexdigest(), True,
    )
    assert (_outputs(data) / "report.docx").read_bytes() == CHANGED
    assert _snapshot(_versions(data)) == blobs
    assert after["sessions"] == before["sessions"] and after["receipts"] == before["receipts"]
    frozen = _snapshot(data)
    from office.publish import recover_publications
    recover_publications(CHAT)
    assert _snapshot(data) == frozen


@pytest.mark.parametrize("known_history", (False, True))
def test_agent_content_is_preserved_before_new_restore(office_world, monkeypatch, known_history):
    http, data, _origin, _manager, _broker, original, session = _closed(office_world, monkeypatch)
    agent = original if known_history else b"Agent complete new document bytes"
    (_outputs(data) / "report.docx").write_bytes(agent)
    before = _read(data)
    response = _restore(http, session["file_id"], 2)
    assert response.status_code == 200
    listed = _read(data)["documents"][session["file_id"]]["versions"]
    assert listed[:2] == before["documents"][session["file_id"]]["versions"]
    assert [(item["source"], item["sha256"]) for item in listed[2:]] == (
        [("restore", hashlib.sha256(CHANGED).hexdigest())] if known_history else [
            ("workspace", hashlib.sha256(agent).hexdigest()), ("restore", hashlib.sha256(CHANGED).hexdigest()),
        ]
    )
    assert (_versions(data) / hashlib.sha256(agent).hexdigest()).read_bytes() == agent
    assert listed[-1]["parent"] == 2 and listed[-1]["published"] is True
    assert (_outputs(data) / "report.docx").read_bytes() == CHANGED


@pytest.mark.parametrize("state", ("opening", "editing", "saving", "closing", "conflict"))
@pytest.mark.parametrize("code", (0, 1, 5))
def test_reopen_key_admission_preserves_or_orphans_exact_session(office_world, monkeypatch, state, code):
    http, data, _origin, _manager, _broker = office_world
    file_id, session = _created(office_world, state=state)
    before = _snapshot(data)
    with _info(monkeypatch, session["document_key"], code=code) as origin:
        response = _restore(http, file_id)
    assert len(origin.requests) == (0 if state == "opening" else 1)
    if state == "opening" or code == 0:
        _assert_refusal(response, 409, "session_open")
        assert _snapshot(data) == before
    elif code == 5:
        _assert_refusal(response, 502, "documentserver_unavailable")
        assert _snapshot(data) == before
    else:
        assert response.status_code == 200 and response.json()["number"] == 2
        record = _read(data)["sessions"][session["session_id"]]
        assert (record["state"], record["reason"]) == ("orphaned", "editor_state_lost")
        assert (_outputs(data) / "brief.docx").read_bytes() == (
            _versions(data) / _read(data)["documents"][file_id]["versions"][0]["sha256"]
        ).read_bytes()


def test_forgotten_editing_session_restores_unpublished_autosave(office_world, monkeypatch):
    http, data, _origin, _manager, _broker, original, session = _autosaved(office_world, monkeypatch)
    before = _read(data)
    with _info(monkeypatch, session["document_key"], code=1):
        response = _restore(http, session["file_id"], 2)
    assert response.status_code == 200 and response.json()["number"] == 3
    after = _read(data)
    assert after["receipts"] == before["receipts"]
    assert after["documents"][session["file_id"]]["versions"][:2] == before["documents"][session["file_id"]]["versions"]
    assert after["sessions"][session["session_id"]]["state"] == "orphaned"
    assert (_outputs(data) / "report.docx").read_bytes() == CHANGED


@pytest.mark.parametrize("marker", (None, "", "changed"))
def test_epoch_precedes_key_and_preserves_opaque_empty_token(office_world, monkeypatch, marker):
    http, data, _origin, _manager, _broker = office_world
    file_id, session = _created(office_world, state="editing")
    if marker is not None:
        (data / ".office-restore-epoch").write_text(marker)
    before = _snapshot(data)
    with _info(monkeypatch, session["document_key"], code=5) as origin:
        response = _restore(http, file_id)
    if marker is None:
        _assert_refusal(response, 502, "documentserver_unavailable")
        assert len(origin.requests) == 1 and _snapshot(data) == before
    else:
        assert response.status_code == 200 and origin.requests == []
        record = _read(data)["sessions"][session["session_id"]]
        assert (record["state"], record["reason"]) == ("orphaned", "restore_epoch_changed")


@pytest.mark.parametrize("changed_epoch", (False, True))
def test_final_receipt_conflict_excludes_key_lookup_before_session_refusal(office_world, monkeypatch, changed_epoch):
    from tests.orchestrator.test_office_resolution import _conflict
    http, data, _origin, _manager, _broker, original, session = _conflict(office_world, monkeypatch, final=True)
    if changed_epoch:
        (data / ".office-restore-epoch").write_text("other")
    before = _read(data)
    with _info(monkeypatch, session["document_key"], code=5) as origin:
        response = _restore(http, session["file_id"], 2)
    assert origin.requests == []
    if changed_epoch:
        assert response.status_code == 200
        assert _read(data)["sessions"][session["session_id"]]["state"] == "orphaned"
    else:
        _assert_refusal(response, 409, "session_open")
        assert _read(data) == before


@pytest.mark.parametrize("body", (b"", b"null", b"[]", b"{", b'{"number":true}', b'{"number":1.0}', b'{"number":"1"}', b"{}"))
def test_invalid_body_has_no_session_or_workspace_effect(office_world, monkeypatch, body):
    http, data, _origin, _manager, _broker, original, session = _opened(office_world)
    before = _snapshot(data)
    _assert_refusal(_restore(http, session["file_id"], body=body), 422, "invalid_request")
    assert _snapshot(data) == before


@pytest.mark.parametrize("number", (-1, 0, 3, 10**30))
def test_unknown_number_precedes_epoch_orphaning(office_world, monkeypatch, number):
    http, data, _origin, _manager, _broker, original, session = _opened(office_world)
    (data / ".office-restore-epoch").write_text("other")
    before = _snapshot(data)
    _assert_refusal(_restore(http, session["file_id"], number), 404, "unknown_version")
    assert _snapshot(data) == before


@pytest.mark.parametrize("identity", ("unknown", "foreign", "tombstoned"))
def test_unknown_file_has_no_restore_authority(office_world, monkeypatch, identity):
    http, data, _origin, _manager, broker, original, session = _closed(office_world, monkeypatch)
    file_id, chat = session["file_id"], CHAT
    if identity == "unknown":
        file_id = "not-a-file-id"
    elif identity == "foreign":
        _put(data, "other.txt", b"foreign-chat", chat=CHAT_B)
        broker.OutputsBroker().reconcile(CHAT_B)
        chat = CHAT_B
    else:
        (_outputs(data) / "report.docx").unlink()
        broker.OutputsBroker().reconcile(CHAT)
    before = _snapshot(data)
    _assert_refusal(_restore(http, file_id, chat=chat), 404, "unknown_file")
    assert _snapshot(data) == before


@pytest.mark.parametrize("mutation", ("missing", "leaf-link", "parent-link", "root-link", "directory", "oversize"))
def test_unsafe_or_missing_path_adds_no_version_and_never_recreates(office_world, monkeypatch, mutation):
    name = "nested/report.docx" if mutation == "parent-link" else "report.docx"
    http, data, _origin, _manager, _broker, original, session = _closed(office_world, monkeypatch, name)
    path = _outputs(data) / name
    outside = data.parent / "private"
    outside.mkdir()
    secret = outside / "report.docx"
    secret.write_bytes(b"private external bytes")
    if mutation == "missing":
        path.unlink()
    elif mutation == "leaf-link":
        path.unlink()
        path.symlink_to(secret)
    elif mutation == "parent-link":
        shutil.rmtree(_outputs(data) / "nested")
        (_outputs(data) / "nested").symlink_to(outside, target_is_directory=True)
    elif mutation == "root-link":
        shutil.rmtree(_outputs(data))
        _outputs(data).symlink_to(outside, target_is_directory=True)
    elif mutation == "directory":
        path.unlink()
        path.mkdir()
    else:
        with path.open("wb") as stream:
            stream.truncate(100 * 1024 * 1024 + 1)
    before = _read(data)
    _assert_refusal(_restore(http, session["file_id"]), 409 if mutation == "missing" else 503,
                    "path_missing" if mutation == "missing" else "unsafe_path")
    after = _read(data)
    assert after["documents"] == before["documents"] and after["sessions"] == before["sessions"]
    assert after["receipts"] == before["receipts"] and after["journal"] == {}
    assert secret.read_bytes() == b"private external bytes"
    if mutation == "missing":
        assert not path.exists()


@pytest.mark.parametrize("state", ("running", "paused", "exited"))
def test_one_fence_captures_only_after_pause_and_preserves_external_pause(office_world, monkeypatch, state):
    from tests.orchestrator.test_lifecycle import _container, _docker
    http, data, _origin, manager, _broker, original, session = _closed(office_world, monkeypatch)
    path = _outputs(data) / "report.docx"
    path.write_bytes(b"Agent bytes")
    container = _running(manager) if state == "running" else _container(manager._container_name(CHAT), status=state)
    if state != "running":
        manager._docker_client = _docker([container])
    inode = (path.stat().st_dev, path.stat().st_ino)
    reads, real_read = [], os.read
    def observed(fd, size):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == inode:
            assert state == "exited" or container.attrs["State"]["Paused"] is True
            reads.append(True)
        return real_read(fd, size)
    with monkeypatch.context() as boundary:
        boundary.setattr(os, "read", observed)
        response = _restore(http, session["file_id"])
    assert response.status_code == 200 and reads
    assert container.pause.call_count == container.unpause.call_count == (1 if state == "running" else 0)
    assert container.status == state
    assert not (_office(data) / "fence.json").exists()
    assert path.read_bytes() == original


def test_pause_failure_only_retains_new_unpublished_restore_without_workspace_read(office_world, monkeypatch):
    http, data, _origin, manager, broker, original, session = _closed(office_world, monkeypatch)
    path = _outputs(data) / "report.docx"
    agent = b"Agent must not be captured on pause failure"
    path.write_bytes(agent)
    before = _read(data)
    blobs = _snapshot(_versions(data))
    revision = broker.OutputsBroker().current_revision(CHAT)
    container = _running(manager)
    container.pause.side_effect = RuntimeError("pause refused")
    inode = (path.stat().st_dev, path.stat().st_ino)
    real_read = os.read
    def forbidden(fd, size):
        info = os.fstat(fd)
        assert (info.st_dev, info.st_ino) != inode, "workspace read before writer exclusion"
        return real_read(fd, size)
    with monkeypatch.context() as boundary:
        boundary.setattr(os, "read", forbidden)
        _assert_refusal(_restore(http, session["file_id"], 2), 503, "pause_failed")
    after = _read(data)
    listed = after["documents"][session["file_id"]]["versions"]
    assert listed[:2] == before["documents"][session["file_id"]]["versions"]
    assert (listed[2]["source"], listed[2]["parent"], listed[2]["published"]) == ("restore", 2, False)
    assert len(listed) == 3 and path.read_bytes() == agent
    assert _snapshot(_versions(data)) == blobs and after["journal"] == {}
    assert after["sessions"] == before["sessions"] and after["receipts"] == before["receipts"]
    assert broker.OutputsBroker().current_revision(CHAT) == revision
    assert container.unpause.call_count == 0
    assert not (_office(data) / "fence.json").exists()


def test_free_space_floor_refuses_before_acceptance(office_world, monkeypatch):
    http, data, _origin, _manager, _broker, original, session = _closed(office_world, monkeypatch)
    before = _snapshot(data)
    monkeypatch.setattr(os, "fstatvfs", lambda fd: type("Space", (), {"f_bavail": 0, "f_frsize": 4096})())
    _assert_refusal(_restore(http, session["file_id"]), 503, "storage_low")
    assert _snapshot(data) == before


@pytest.mark.parametrize("cut", ("acceptance", "capture", "replacement", "registration"))
def test_enospc_preserves_accepted_responsibility_or_prepared_content(office_world, monkeypatch, cut):
    from office.publish import recover_publications
    http, data, _origin, _manager, _broker, original, session = _closed(office_world, monkeypatch)
    path = _outputs(data) / "report.docx"
    agent = b"Agent content before storage failure"
    path.write_bytes(agent)
    before = _read(data)
    real_write, real_replace = os.write, os.replace
    def write(fd, body):
        encoded = bytes(body)
        if cut == "acceptance" and b'"requester":"restore"' in encoded:
            raise OSError(errno.ENOSPC, "acceptance storage full")
        if cut == "capture" and encoded == agent:
            raise OSError(errno.ENOSPC, "capture storage full")
        return real_write(fd, body)
    def replace(source, destination, *args, **kwargs):
        if destination == ("report.docx" if cut == "replacement" else "index.json") and cut in ("replacement", "registration"):
            raise OSError(errno.ENOSPC, "publication storage full")
        return real_replace(source, destination, *args, **kwargs)
    with monkeypatch.context() as boundary:
        boundary.setattr(os, "write", write)
        boundary.setattr(os, "replace", replace)
        _assert_refusal(_restore(http, session["file_id"]), 503, "storage_low")
    after = _read(data)
    assert after["receipts"] == before["receipts"] and after["sessions"] == before["sessions"]
    assert after["documents"][session["file_id"]]["versions"][:2] == before["documents"][session["file_id"]]["versions"]
    if cut == "acceptance":
        assert after == before and path.read_bytes() == agent
    elif after["journal"]:
        recover_publications(CHAT)
        assert path.read_bytes() == original and _read(data)["journal"] == {}
    else:
        assert path.read_bytes() == agent
        assert after["documents"][session["file_id"]]["versions"][-1]["source"] == "restore"
        assert after["documents"][session["file_id"]]["versions"][-1]["published"] is False


def test_unreadable_epoch_refuses_without_lookup_or_mutation(office_world, monkeypatch):
    http, data, _origin, _manager, _broker, original, session = _opened(office_world)
    (data / ".office-restore-epoch").mkdir()
    before = _snapshot(data)
    with _info(monkeypatch, session["document_key"], code=1) as origin:
        _assert_refusal(_restore(http, session["file_id"]), 500, "state_corrupt")
    assert origin.requests == [] and _snapshot(data) == before


@pytest.mark.parametrize("headers", ({}, {"Authorization": "Bearer invalid"}))
def test_restore_guard_refuses_before_any_history_mutation(office_world, monkeypatch, headers):
    http, data, _origin, _manager, _broker, original, session = _closed(office_world, monkeypatch)
    before = _snapshot(data)
    response = _restore(http, session["file_id"], headers=headers)
    assert response.status_code in (401, 403)
    assert _snapshot(data) == before


@pytest.mark.parametrize("enabled", (False, True))
def test_disabled_or_unknown_chat_restore_creates_nothing(office_world, monkeypatch, enabled):
    http, data, _origin, _manager, _broker, original, session = _closed(office_world, monkeypatch)
    chat = CHAT if not enabled else "c3d4e5f6-a7b8-9012-cdef-123456789012"
    with monkeypatch.context() as env:
        if not enabled:
            env.delenv("OCU_OFFICE_DOCSERVER_URL")
        before = _snapshot(data)
        response = _restore(http, session["file_id"], chat=chat)
        assert response.status_code == 404
        assert _snapshot(data) == before


@pytest.mark.parametrize("cut", ("accepted", "capture", "completion"))
def test_visible_state_durability_failure_keeps_recoverable_or_completed_restore(office_world, monkeypatch, cut):
    from office.publish import recover_publications
    http, data, _origin, _manager, broker, original, session = _closed(office_world, monkeypatch)
    path = _outputs(data) / "report.docx"
    path.write_bytes(b"Agent bytes retained despite durability error")
    before = _read(data)
    real_sync, real_replace = os.fsync, os.replace
    armed = set()
    def replace(source, destination, *args, **kwargs):
        result = real_replace(source, destination, *args, **kwargs)
        if destination == "state.json":
            current = _read(data)
            live = [entry for entry in current["journal"].values() if entry["requester"] == "restore"]
            phase = ("capture" if live and "restore_version" in live[0] else
                     "accepted" if live and "target_path" not in live[0] else
                     "completion" if not live and current["documents"][session["file_id"]]["published_version"] > 2 else "")
            if phase == cut:
                info = os.fstat(kwargs["dst_dir_fd"])
                armed.add((info.st_dev, info.st_ino))
        return result
    def sync(fd):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) in armed:
            raise OSError(errno.EIO, "state directory durability failed")
        return real_sync(fd)
    with monkeypatch.context() as boundary:
        boundary.setattr(os, "replace", replace)
        boundary.setattr(os, "fsync", sync)
        _assert_refusal(_restore(http, session["file_id"]), 500, "state_durability")
    visible = _read(data)
    assert bool(visible["journal"]) == (cut != "completion")
    recover_publications(CHAT)
    after = _read(data)
    assert path.read_bytes() == original
    assert after["documents"][session["file_id"]]["published_version"] == 4
    assert after["documents"][session["file_id"]]["versions"][:2] == before["documents"][session["file_id"]]["versions"]
    assert after["sessions"] == before["sessions"] and after["receipts"] == before["receipts"]
    frozen = _snapshot(data)
    recover_publications(CHAT)
    assert _snapshot(data) == frozen


@pytest.mark.parametrize("requester", ("save", "final", "resolve-copy", "resolve-overwrite"))
def test_epoch_restore_drives_old_obligation_before_deciding_original_identity(office_world, monkeypatch, requester):
    from tests.orchestrator.test_office_resolution import _conflict, _kill_resolve
    from tests.orchestrator.test_office_callback_publish import _surviving
    if requester.startswith("resolve"):
        http, data, _origin, _manager, broker, original, session = _conflict(office_world, monkeypatch, final=True)
        _kill_resolve(data, session, "save_as" if requester == "resolve-copy" else "overwrite", "accepted")
    else:
        http, data, _origin, _manager, broker, original, session = _opened(office_world)
        if requester == "save":
            from tests.orchestrator.test_office_callback_publish import _allocate
            _allocate(http, session, monkeypatch)
        with _content_origin({"/save.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
            _surviving(http, data, session, monkeypatch, server, final=requester == "final")
    before = _read(data)
    (data / ".office-restore-epoch").write_text("changed")
    with _info(monkeypatch, session["document_key"], code=5) as origin:
        response = _restore(http, session["file_id"], 1)
    assert origin.requests == [] and response.status_code == 200
    after = _read(data)
    assert after["journal"] == {} and after["receipts"] == before["receipts"]
    assert (_outputs(data) / "report.docx").read_bytes() == original
    assert after["documents"][session["file_id"]]["versions"][-1]["source"] == "restore"
    record = after["sessions"][session["session_id"]]
    assert record["state"] == ("orphaned" if requester == "save" else "closed")
    if requester == "resolve-copy":
        assert record["file_id"] != session["file_id"]
        assert (_outputs(data) / record["saved_as"]["path"]).read_bytes() == CHANGED
        assert after["documents"][record["file_id"]]["published_version"] == 1


def test_pending_old_recovery_retains_session_and_refuses_new_restore_authority(office_world, monkeypatch):
    from tests.orchestrator.test_office_resolution import _conflict, _kill_resolve
    http, data, _origin, manager, _broker, original, session = _conflict(office_world, monkeypatch, final=True)
    _kill_resolve(data, session, "overwrite", "accepted")
    (data / ".office-restore-epoch").write_text("changed")
    container = _running(manager)
    container.pause.side_effect = RuntimeError("cannot recover writer exclusion")
    before = _read(data)
    workspace = _snapshot(_outputs(data))
    _assert_refusal(_restore(http, session["file_id"]), 503, "publish_pending")
    after = _read(data)
    assert after["documents"] == before["documents"] and after["sessions"] == before["sessions"]
    assert after["receipts"] == before["receipts"] and after["journal"] == before["journal"]
    assert _snapshot(_outputs(data)) == workspace


def test_safe_boundary_timeout_keeps_prepared_restore_unpublished(office_world, monkeypatch):
    import time
    http, data, _origin, manager, _broker, original, session = _closed(office_world, monkeypatch)
    agent = b"Agent content preserved before timeout"
    path = _outputs(data) / "report.docx"
    path.write_bytes(agent)
    container = _running(manager)
    real_sync, clock = os.fsync, time.monotonic
    offset = [0.0]
    def sync(fd):
        result = real_sync(fd)
        state = _read(data)
        if any(entry.get("requester") == "restore" and "restore_version" in entry
               for entry in state["journal"].values()):
            offset[0] = 5.0
        return result
    with monkeypatch.context() as boundary:
        boundary.setattr(os, "fsync", sync)
        boundary.setattr(time, "monotonic", lambda: clock() + offset[0])
        _assert_refusal(_restore(http, session["file_id"]), 503, "publish_timeout")
    after = _read(data)
    listed = after["documents"][session["file_id"]]["versions"]
    assert [(item["source"], item["published"]) for item in listed[2:]] == [("workspace", True), ("restore", False)]
    assert path.read_bytes() == agent and after["journal"] == {}
    assert container.pause.call_count == container.unpause.call_count == 1
    assert not (_office(data) / "fence.json").exists()


def test_index_failure_after_replace_retains_obligation_and_unpublished_restore(office_world, monkeypatch):
    from office.publish import recover_publications
    http, data, _origin, _manager, broker, original, session = _closed(office_world, monkeypatch)
    index = data / CHAT / ".ocu" / "index.json"
    index_before = index.read_bytes()
    real_replace = os.replace
    def replace(source, destination, *args, **kwargs):
        result = real_replace(source, destination, *args, **kwargs)
        if destination == "report.docx":
            index.write_bytes(b"{broken")
        return result
    with monkeypatch.context() as boundary:
        boundary.setattr(os, "replace", replace)
        _assert_refusal(_restore(http, session["file_id"]), 503, "index_unavailable")
    after = _read(data)
    assert (_outputs(data) / "report.docx").read_bytes() == original
    assert after["journal"]
    assert after["documents"][session["file_id"]]["versions"][-1]["published"] is False
    index.write_bytes(index_before)
    recover_publications(CHAT)
    completed = _read(data)
    assert completed["journal"] == {}
    assert completed["documents"][session["file_id"]]["published_version"] == 3


@pytest.mark.parametrize("mutation", ("missing", "link"))
def test_pause_failure_rechecks_path_without_capturing_unsafe_or_missing_file(office_world, monkeypatch, mutation):
    http, data, _origin, manager, _broker, original, session = _closed(office_world, monkeypatch)
    path = _outputs(data) / "report.docx"
    private = data.parent / "private.docx"
    private.write_bytes(b"external private content")
    before = _read(data)
    container = _running(manager)
    def refused():
        path.unlink()
        if mutation == "link":
            path.symlink_to(private)
        raise RuntimeError("pause failed while path changed")
    container.pause.side_effect = refused
    _assert_refusal(_restore(http, session["file_id"]), 409 if mutation == "missing" else 503,
                    "path_missing" if mutation == "missing" else "unsafe_path")
    after = _read(data)
    assert after["documents"] == before["documents"] and after["sessions"] == before["sessions"]
    assert after["receipts"] == before["receipts"] and after["journal"] == {}
    assert private.read_bytes() == b"external private content"
    if mutation == "missing":
        assert not path.exists()
    else:
        assert path.is_symlink()


@pytest.mark.parametrize("stored,current", (("A", "A"), ("", ""), ("A", ""), ("", "A"), ("A", None)))
def test_opaque_epoch_equality_determines_key_lookup(office_world, monkeypatch, stored, current):
    http, data, _origin, _manager, _broker = office_world
    marker = data / ".office-restore-epoch"
    marker.write_text(stored)
    file_id, session = _created(office_world, state="editing")
    if current is None:
        marker.unlink()
    else:
        marker.write_text(current)
    before = _snapshot(data)
    with _info(monkeypatch, session["document_key"], code=5) as origin:
        response = _restore(http, file_id)
    if stored == current:
        _assert_refusal(response, 502, "documentserver_unavailable")
        assert len(origin.requests) == 1 and _snapshot(data) == before
    else:
        assert response.status_code == 200 and origin.requests == []
        assert _read(data)["sessions"][session["session_id"]]["reason"] == "restore_epoch_changed"


def test_unreachable_key_does_not_drive_preexisting_accepted_publication(office_world, monkeypatch):
    from tests.orchestrator.test_office_callback_publish import _allocate, _surviving
    http, data, _origin, _manager, _broker, original, session = _opened(office_world)
    _allocate(http, session, monkeypatch)
    with _content_origin({"/save.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        _surviving(http, data, session, monkeypatch, server)
    before = _snapshot(data)
    with _info(monkeypatch, session["document_key"], code=5):
        _assert_refusal(_restore(http, session["file_id"]), 502, "documentserver_unavailable")
    assert _snapshot(data) == before
