# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Public HTTP seam for authenticated Office version history listing."""
from __future__ import annotations

import errno
import hashlib
import json
import os
from urllib.parse import quote

import pytest

from tests.orchestrator.test_office_control_plane import _open_session
from tests.orchestrator.test_office_sessions import office_world
from tests.orchestrator.test_office_sessions import (
    FINAL_STATES, OPEN_STATES, JWT_SECRET, _assert_no_secrets, _assert_refusal,
    _index_file, _office, _outputs, _put, _snapshot, _state, _versions,
)
from tests.orchestrator.test_office_session_lifecycle import _change, _created, _info, _unpublished
from tests.orchestrator.test_outputs_endpoint import CHAT_B, INTERNAL, MCP_KEY
from tests.orchestrator.test_outputs_endpoint import CHAT, _auth


def test_versions_lists_history_published_pointer_and_open_session(office_world):
    from office.store import OfficeStore

    http, _data, _origin, _docker, _broker, workspace, session = _open_session(office_world)
    file_id = session["file_id"]
    autosave = workspace + b"\nautosaved draft"
    store = OfficeStore()
    store.store_version(
        CHAT,
        file_id,
        autosave,
        source="autosave",
        parent=1,
        published=False,
        min_free_bytes=0,
    )

    def pin_history_times(state):
        records = state["documents"][file_id]["versions"]
        records[0]["created_at"] = "2026-10-07T10:00:00Z"
        records[1]["created_at"] = "2026-10-07T10:01:00Z"

    store.update(CHAT, pin_history_times)

    response = http.get(
        f"/api/office/{CHAT}/documents/{quote(file_id, safe='')}/versions",
        headers=_auth(),
    )

    assert response.status_code == 200
    assert response.json() == {
        "file_id": file_id,
        "published_version": 1,
        "open_session": {
            "session_id": session["session_id"],
            "state": "opening",
            "reason": None,
            "editor_ended": False,
        },
        "versions": [
            {
                "number": 1,
                "parent": None,
                "source": "workspace",
                "sha256": hashlib.sha256(workspace).hexdigest(),
                "size": len(workspace),
                "created_at": "2026-10-07T10:00:00Z",
                "published": True,
            },
            {
                "number": 2,
                "parent": 1,
                "source": "autosave",
                "sha256": hashlib.sha256(autosave).hexdigest(),
                "size": len(autosave),
                "created_at": "2026-10-07T10:01:00Z",
                "published": False,
            },
        ],
    }


def _listing(http, file_id, chat=CHAT, headers=None):
    return http.get(
        f"/api/office/{quote(chat, safe='')}/documents/{quote(file_id, safe='')}/versions",
        headers=_auth() if headers is None else headers,
    )


def test_versions_shows_workspace_autosave_and_published_save(office_world, monkeypatch):
    from tests.orchestrator.test_office_callback_publish import (
        CHANGED, _autosaved, _bind_internal, _content_origin, _payload, _post,
    )
    from tests.orchestrator.test_office_save_close import _forcesave, _save

    http, data, origin, _manager, _broker, _content, session = _autosaved(office_world, monkeypatch)
    saved_content = CHANGED + b"published save"
    with _forcesave(monkeypatch, session["document_key"]):
        saved = _save(http, session["session_id"], "publish")
    assert saved.status_code == 202 and saved.json()["save_seq"] == 2
    with _content_origin({"/saved.docx": saved_content}) as server, _bind_internal(monkeypatch, server.url):
        callback = _post(http, session, _payload(session, server.url + "/saved.docx", save_seq=2))
    assert callback.status_code == 200
    before = _snapshot(data)
    response = _listing(http, session["file_id"])
    assert response.status_code == 200
    result = response.json()
    assert result["published_version"] == 3
    assert [(v["number"], v["parent"], v["source"], v["published"]) for v in result["versions"]] == [
        (1, None, "workspace", True), (2, 1, "autosave", False), (3, 2, "save", True),
    ]
    assert result["versions"][2]["sha256"] == hashlib.sha256(saved_content).hexdigest()
    assert result["open_session"] == {
        "session_id": session["session_id"], "state": "editing", "reason": None, "editor_ended": False,
    }
    assert _snapshot(data) == before
    assert origin.hits == 0


@pytest.mark.parametrize("name,content", (("fresh.docx", b"not inspected"), ("notes.txt", b"plain")))
def test_active_file_without_office_history_returns_empty_without_creating_state(office_world, name, content):
    http, data, origin, _manager, broker = office_world
    path = _put(data, name, content)
    file_id = _index_file(broker, data, name)
    # The persisted identity, not a new workspace observation, admits the read.
    path.unlink()
    before = _snapshot(data)
    response = _listing(http, file_id)
    assert response.status_code == 200
    assert response.json() == {
        "file_id": file_id, "published_version": None, "open_session": None, "versions": [],
    }
    assert _snapshot(data) == before
    assert not _office(data).exists()
    assert origin.hits == 0


def test_versions_refuses_symlinked_chat_root_without_external_mutation(office_world):
    http, data, origin, _manager, _broker = office_world
    file_id, _session = _created(office_world)
    root = data / CHAT
    detached = data.parent / "detached-chat"
    outside = data.parent / "external-control-root"
    outside.mkdir()
    (outside / "private.txt").write_bytes(b"external content must be conserved")
    root.rename(detached)
    root.symlink_to(outside, target_is_directory=True)
    outside_before = _snapshot(outside)
    detached_before = _snapshot(detached)
    before = _snapshot(data)

    _assert_refusal(_listing(http, file_id), 500, "state_corrupt")

    assert _snapshot(outside) == outside_before
    assert not (outside / ".lifecycle.lock").exists()
    assert _snapshot(detached) == detached_before
    assert _snapshot(data) == before
    assert root.is_symlink()
    assert origin.hits == 0


@pytest.mark.parametrize("removed_at", ("availability", "lock_open"))
def test_versions_does_not_recreate_chat_removed_during_admission(office_world, monkeypatch, removed_at):
    from pathlib import Path

    http, data, origin, _manager, _broker = office_world
    file_id, _session = _created(office_world)
    root = data / CHAT
    detached = data.parent / "removed-chat"
    before = _snapshot(root)
    real_is_dir, real_open, real_lstat = Path.is_dir, Path.open, os.lstat
    removed = False
    lock_unavailable = False

    def checked_directory(path):
        nonlocal removed
        result = real_is_dir(path)
        if path == root and result and not removed:
            root.rename(detached)
            removed = True
        return result

    def opened(path, *args, **kwargs):
        nonlocal removed, lock_unavailable
        if path == root / ".lifecycle.lock" and not removed:
            root.rename(detached)
            removed = True
            try:
                return real_open(path, *args, **kwargs)
            except FileNotFoundError:
                lock_unavailable = True
                raise
        return real_open(path, *args, **kwargs)

    def inspected(path, *args, **kwargs):
        if lock_unavailable and os.fspath(path) == os.fspath(root):
            pytest.fail("versions inspected chat state after lock acquisition failed")
        return real_lstat(path, *args, **kwargs)

    with monkeypatch.context() as boundary:
        if removed_at == "availability":
            boundary.setattr(Path, "is_dir", checked_directory)
        else:
            boundary.setattr(Path, "open", opened)
            boundary.setattr(os, "lstat", inspected)
        response = _listing(http, file_id)

    _assert_refusal(response, 404, "unknown_file")
    assert removed
    if removed_at == "lock_open":
        assert lock_unavailable
    assert not root.exists()
    assert not (root / ".lifecycle.lock").exists()
    assert _snapshot(detached) == before
    assert not (root / ".ocu" / "office").exists()
    assert origin.hits == 0



def test_identity_refusals_precede_history_access_and_preserve_both_chats(office_world):
    http, data, origin, _manager, broker = office_world
    gone = _put(data, "gone.docx", b"gone")
    tombstone = _index_file(broker, data, "gone.docx")
    gone.unlink()
    broker.OutputsBroker().reconcile(CHAT)
    _put(data, "foreign.docx", b"foreign", chat=CHAT_B)
    foreign = _index_file(broker, data, "foreign.docx", chat=CHAT_B)
    _office(data).mkdir(parents=True)
    _state(data).write_bytes(b"unreadable history must not precede identity admission")
    before = _snapshot(data)
    for file_id in ("unknown", "not a uuid\x00", tombstone, foreign):
        _assert_refusal(_listing(http, file_id), 404, "unknown_file")
    assert _snapshot(data) == before
    assert origin.hits == 0


@pytest.mark.parametrize("lifecycle", OPEN_STATES + FINAL_STATES)
@pytest.mark.parametrize("changed_epoch", (False, True))
def test_epoch_matrix_preserves_history_and_only_orphans_open_sessions(office_world, lifecycle, changed_epoch):
    from office.store import OfficeStore
    http, data, origin, _manager, _broker = office_world
    file_id, session = _created(office_world, lifecycle)
    _unpublished(file_id)
    if changed_epoch:
        (data / ".office-restore-epoch").write_text("epoch-after-restore\n")
    before = OfficeStore().read(CHAT)
    files = _snapshot(_outputs(data))
    blobs = _snapshot(_versions(data))
    response = _listing(http, file_id)
    assert response.status_code == 200
    result = response.json()
    orphaned = changed_epoch and lifecycle in OPEN_STATES
    expected_session = None if orphaned or lifecycle in FINAL_STATES else {
        "session_id": session["session_id"], "state": lifecycle, "reason": None, "editor_ended": False,
    }
    assert result["open_session"] == expected_session
    assert result["published_version"] == 1
    assert [(v["number"], v["source"], v["published"]) for v in result["versions"]] == [
        (1, "workspace", True), (2, "autosave", False),
    ]
    expected = json.loads(json.dumps(before))
    if orphaned:
        expected["sessions"][session["session_id"]].update(state="orphaned", reason="restore_epoch_changed")
    assert OfficeStore().read(CHAT) == expected
    assert _snapshot(_outputs(data)) == files
    assert _snapshot(_versions(data)) == blobs
    assert origin.hits == 0


@pytest.mark.parametrize("status", (2, 3, 4))
def test_final_receipt_not_terminal_state_controls_editor_ended(office_world, status):
    from office.store import OfficeStore
    http, data, origin, _manager, _broker = office_world
    file_id, session = _created(office_world, "conflict")
    _change(session["session_id"], reason="baseline_mismatch", save_seq=1)
    OfficeStore().record_receipt(CHAT, session["session_id"], 1, {
        "status": status, "sha256": None, "version": None, "answer": {"error": 0},
    })
    before = _snapshot(data)
    response = _listing(http, file_id)
    assert response.status_code == 200
    assert response.json()["open_session"] == {
        "session_id": session["session_id"], "state": "conflict",
        "reason": "baseline_mismatch", "editor_ended": True,
    }
    assert _snapshot(data) == before
    assert origin.hits == 0


def test_unattended_final_conflict_is_listed_without_contact_or_notice_refresh(office_world, monkeypatch):
    from tests.orchestrator.test_office_resolution import _conflict
    http, data, recording, _manager, _broker, _original, session = _conflict(office_world, monkeypatch, final=True)
    before = _snapshot(data)
    with _info(monkeypatch, session["document_key"], code=1) as origin:
        response = _listing(http, session["file_id"])
        assert origin.requests == []
    assert response.status_code == 200
    assert response.json()["open_session"] == {
        "session_id": session["session_id"], "state": "conflict",
        "reason": "baseline_mismatch", "editor_ended": True,
    }
    assert response.json()["versions"][-1]["published"] is False
    assert _snapshot(data) == before
    assert recording.hits == 0


def test_forgotten_editing_session_does_not_contact_documentserver_or_inspect_content(office_world, monkeypatch):
    from office import workspace
    from office.store import OfficeStore
    from tests.orchestrator.test_office_workspace import _forbid_inode_open

    http, data, recording, _manager, _broker = office_world
    file_id, session = _created(office_world, "editing")
    _unpublished(file_id)
    _put(data, "brief.docx", b"external workspace edit")
    # Extra persisted metadata, including private values, must not escape the whitelist.
    def hidden(state):
        state["documents"][file_id]["versions"][0]["internal"] = "private-version-canary"
    OfficeStore().update(CHAT, hidden)
    before = _snapshot(data)
    with monkeypatch.context() as boundary:
        _forbid_inode_open(workspace, boundary, _outputs(data) / "brief.docx", *(_versions(data).iterdir()))
        with _info(monkeypatch, session["document_key"], code=1) as origin:
            response = _listing(http, file_id)
            assert origin.requests == []
    assert response.status_code == 200
    result = response.json()
    assert set(result) == {"file_id", "published_version", "open_session", "versions"}
    assert all(set(v) == {"number", "parent", "source", "sha256", "size", "created_at", "published"} for v in result["versions"])
    assert result["open_session"] == {
        "session_id": session["session_id"], "state": "editing", "reason": None, "editor_ended": False,
    }
    _assert_no_secrets(response, session["document_key"], INTERNAL, MCP_KEY, JWT_SECRET, "private-version-canary")
    assert _snapshot(data) == before
    assert recording.hits == 0


def test_published_pointer_is_not_inferred_from_highest_published_flag(office_world):
    from office.store import OfficeStore
    http, data, _origin, _manager, _broker = office_world
    file_id, _session = _created(office_world, "closed")
    _unpublished(file_id)
    OfficeStore().mark_published(CHAT, file_id, 2)
    before = _snapshot(data)
    response = _listing(http, file_id)
    assert response.status_code == 200
    assert response.json()["published_version"] == 1
    assert [v["published"] for v in response.json()["versions"]] == [True, True]
    assert response.json()["open_session"] is None
    assert _snapshot(data) == before


@pytest.mark.parametrize("bad_marker", ("directory", "invalid_utf8", "symlink"))
def test_unreadable_epoch_returns_explicit_corruption_without_orphaning(office_world, bad_marker):
    http, data, origin, _manager, _broker = office_world
    file_id, _session = _created(office_world, "editing")
    marker = data / ".office-restore-epoch"
    if bad_marker == "directory":
        marker.mkdir()
    elif bad_marker == "invalid_utf8":
        marker.write_bytes(b"\xff")
    else:
        marker.symlink_to(_state(data))
    before = _snapshot(data)
    _assert_refusal(_listing(http, file_id), 500, "state_corrupt")
    assert _snapshot(data) == before
    assert origin.hits == 0


@pytest.mark.parametrize("corruption", ("versions", "pointer", "sessions", "receipt", "index"))
def test_corrupt_history_and_index_are_not_empty_or_pending_success(office_world, corruption):
    from office.store import OfficeStore
    http, data, origin, _manager, _broker = office_world
    file_id, session = _created(office_world, "editing")
    def corrupt(state):
        if corruption == "versions":
            state["documents"][file_id]["versions"][0]["number"] = 2
        elif corruption == "pointer":
            state["documents"][file_id]["published_version"] = True
        elif corruption == "sessions":
            state["sessions"][session["session_id"]]["state"] = "invalid"
        elif corruption == "receipt":
            state["receipts"][session["session_id"]] = {"01": {
                "status": 4, "sha256": None, "version": None, "answer": {"error": 0},
            }}
    OfficeStore().update(CHAT, corrupt)
    if corruption == "index":
        (data / CHAT / ".ocu" / "index.json").write_bytes(b"invalid index")
    before = _snapshot(data)
    _assert_refusal(_listing(http, file_id), 500, "state_corrupt")
    assert _snapshot(data) == before
    assert origin.hits == 0


def test_listing_inherits_chat_auth_and_uses_canonical_chat_scope(office_world):
    http, data, origin, _manager, _broker = office_world
    file_id, _session = _created(office_world)
    before = _snapshot(data)
    for headers in ({}, _auth("wrong-secret")):
        denied = _listing(http, file_id, headers=headers)
        assert denied.status_code == 401
        assert denied.json() == {"reason": "unauthorized", "detail": "Unauthorized"}
    invalid = _listing(http, file_id, chat="default")
    assert invalid.status_code == 400
    assert invalid.json() == {"reason": "invalid_chat_id", "detail": "Invalid chat_id"}
    canonical = _listing(http, file_id, chat=" " + CHAT.upper() + " ")
    assert canonical.status_code == 200 and canonical.json()["file_id"] == file_id
    assert _snapshot(data) == before
    assert origin.hits == 0


def _accepted_callback(world, monkeypatch, final=False):
    from tests.orchestrator.test_office_callback_publish import (
        CHANGED, _allocate, _bind_internal, _content_origin, _opened, _surviving,
    )
    opened = _opened(world)
    http, data, _origin, _manager, _broker, _content, session = opened
    if not final:
        _allocate(http, session, monkeypatch)
    with _content_origin({"/save.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        _surviving(http, data, session, monkeypatch, server, final=final)
    return opened


@pytest.mark.parametrize("final", (False, True))
def test_same_epoch_does_not_recover_an_accepted_publication(office_world, monkeypatch, final):
    http, data, origin, manager, _broker, content, session = _accepted_callback(office_world, monkeypatch, final)
    before = _snapshot(data)
    calls = list(manager._docker_client.mock_calls)
    response = _listing(http, session["file_id"])
    assert response.status_code == 200
    assert response.json()["published_version"] == 1
    assert response.json()["versions"][-1]["published"] is False
    assert response.json()["open_session"]["editor_ended"] is final
    assert _snapshot(data) == before
    assert (_outputs(data) / "report.docx").read_bytes() == content
    assert manager._docker_client.mock_calls == calls
    assert origin.hits == 0


@pytest.mark.parametrize("final", (False, True))
@pytest.mark.parametrize("outcome", ("published", "conflict"))
def test_epoch_listing_finishes_save_or_final_obligation_before_orphaning(office_world, monkeypatch, final, outcome):
    from office.store import OfficeStore
    from tests.orchestrator.test_office_callback_publish import CHANGED
    http, data, origin, _manager, broker, content, session = _accepted_callback(office_world, monkeypatch, final)
    workspace = _outputs(data) / "report.docx"
    if outcome == "conflict":
        workspace.write_bytes(content + b"agent")
    before = OfficeStore().read(CHAT)
    revision = broker.OutputsBroker().current_revision(CHAT)
    (data / ".office-restore-epoch").write_text("restored-after-acceptance")
    response = _listing(http, session["file_id"])
    assert response.status_code == 200
    result = response.json()
    persisted = OfficeStore().read(CHAT)
    record = persisted["sessions"][session["session_id"]]
    assert persisted["journal"] == {}
    assert persisted["receipts"] == before["receipts"]
    protected_conflict = final and outcome == "conflict"
    assert (record["state"], record["reason"]) == (
        ("conflict", "baseline_mismatch") if protected_conflict else
        ("closed", None) if final else ("orphaned", "restore_epoch_changed")
    )
    assert result["open_session"] == ({
        "session_id": session["session_id"], "state": "conflict",
        "reason": "baseline_mismatch", "editor_ended": True,
    } if protected_conflict else None)
    assert result["published_version"] == (2 if outcome == "published" else 1)
    assert result["versions"][-1]["published"] is (outcome == "published")
    assert workspace.read_bytes() == (CHANGED if outcome == "published" else content + b"agent")
    assert broker.OutputsBroker().current_revision(CHAT) == revision + (outcome == "published")
    assert origin.hits == 0


def test_second_epoch_listing_orphans_recovered_final_conflict(office_world, monkeypatch):
    from office.store import OfficeStore

    http, data, origin, manager, broker, content, session = _accepted_callback(
        office_world, monkeypatch, final=True,
    )
    workspace = _outputs(data) / "report.docx"
    workspace.write_bytes(content + b"agent")
    (data / ".office-restore-epoch").write_text("restored-after-final-acceptance")

    first = _listing(http, session["file_id"])
    assert first.status_code == 200
    assert first.json()["open_session"] == {
        "session_id": session["session_id"], "state": "conflict",
        "reason": "baseline_mismatch", "editor_ended": True,
    }
    assert first.json()["published_version"] == 1
    assert [(item["number"], item["source"], item["published"]) for item in first.json()["versions"]] == [
        (1, "workspace", True), (2, "close", False),
    ]
    after_recovery = OfficeStore().read(CHAT)
    assert after_recovery["journal"] == {}
    assert (after_recovery["sessions"][session["session_id"]]["state"],
            after_recovery["sessions"][session["session_id"]]["reason"]) == (
        "conflict", "baseline_mismatch",
    )
    outputs_before = _snapshot(_outputs(data))
    versions_before = _snapshot(_versions(data))
    revision = broker.OutputsBroker().current_revision(CHAT)
    calls = list(manager._docker_client.mock_calls)

    second = _listing(http, session["file_id"])
    assert second.status_code == 200
    assert second.json() == {
        **first.json(), "open_session": None,
    }
    expected = json.loads(json.dumps(after_recovery))
    expected["sessions"][session["session_id"]].update(
        state="orphaned", reason="restore_epoch_changed",
    )
    assert OfficeStore().read(CHAT) == expected
    assert _snapshot(_outputs(data)) == outputs_before
    assert _snapshot(_versions(data)) == versions_before
    assert workspace.read_bytes() == content + b"agent"
    assert broker.OutputsBroker().current_revision(CHAT) == revision
    assert manager._docker_client.mock_calls == calls
    assert origin.hits == 0



@pytest.mark.parametrize("ended", (False, True))
@pytest.mark.parametrize("action", ("save_as", "overwrite"))
def test_epoch_listing_recovers_accepted_resolve_and_rereads_requested_identity(office_world, monkeypatch, ended, action):
    from office.store import OfficeStore
    from tests.orchestrator.test_office_resolution import CHANGED, _conflict, _kill_resolve
    http, data, origin, _manager, _broker, original, session = _conflict(office_world, monkeypatch, final=ended)
    before = OfficeStore().read(CHAT)
    _kill_resolve(data, session, action, "accepted")
    (data / ".office-restore-epoch").write_text("epoch-after-accepted-resolve")
    response = _listing(http, session["file_id"])
    assert response.status_code == 200
    result = response.json()
    after = OfficeStore().read(CHAT)
    record = after["sessions"][session["session_id"]]
    assert after["journal"] == {}
    assert after["receipts"] == before["receipts"]
    assert (record["state"], record["reason"]) == (
        ("closed", None) if ended else ("orphaned", "restore_epoch_changed")
    )
    assert result["open_session"] is None
    if action == "save_as":
        assert record["file_id"] != session["file_id"]
        assert after["documents"][session["file_id"]] == before["documents"][session["file_id"]]
        assert result["published_version"] == 1
        assert result["versions"][-1]["published"] is False
        copied = _listing(http, record["file_id"])
        assert copied.status_code == 200
        assert copied.json()["open_session"] is None
        assert copied.json()["published_version"] == 1
        assert [(v["number"], v["source"], v["published"]) for v in copied.json()["versions"]] == [
            (1, "conflict", True),
        ]
        assert (_outputs(data) / "report (2).docx").read_bytes() == CHANGED
        assert (_outputs(data) / "report.docx").read_bytes() == original + b"agent-workspace"
    else:
        assert result["published_version"] == 4
        assert [(v["number"], v["parent"], v["source"], v["published"]) for v in result["versions"]] == [
            (1, None, "workspace", True), (2, 1, "close" if ended else "save", True),
            (3, 2, "workspace", True), (4, 2, "restore", True),
        ]
        assert (_outputs(data) / "report.docx").read_bytes() == CHANGED
    frozen = _snapshot(data)
    assert _listing(http, session["file_id"]).json() == result
    assert _snapshot(data) == frozen
    assert origin.hits == 0


def test_pending_epoch_recovery_retains_obligation_without_fabricating_orphan(office_world, monkeypatch):
    from office.store import OfficeStore
    http, data, origin, _manager, _broker, _content, session = _accepted_callback(office_world, monkeypatch)
    # A fresh valid fence is unresolved ownership, not corrupt state.
    (_office(data) / "fence.json").write_text(json.dumps({
        "schema_version": 1, "container_id": "pending-container", "pause_started_at": 1e20,
    }))
    (data / ".office-restore-epoch").write_text("epoch-pending")
    before = _snapshot(data)
    before_state = OfficeStore().read(CHAT)
    _assert_refusal(_listing(http, session["file_id"]), 503, "publish_pending")
    assert OfficeStore().read(CHAT) == before_state
    assert _snapshot(data) == before
    assert origin.hits == 0


@pytest.mark.parametrize("failure", ("durability", "io", "storage"))
def test_epoch_orphan_write_failures_remain_explicit(office_world, monkeypatch, failure):
    from office.store import OfficeStore
    http, data, origin, _manager, _broker = office_world
    file_id, session = _created(office_world, "editing")
    (data / ".office-restore-epoch").write_text("epoch-write-failure")
    before = OfficeStore().read(CHAT)
    real_sync, real_replace = os.fsync, os.replace
    replaced_directory = None
    def replaced(source, destination, *args, **kwargs):
        nonlocal replaced_directory
        if destination == "state.json" and failure != "durability":
            raise OSError(errno.ENOSPC if failure == "storage" else errno.EIO, "boundary failure")
        result = real_replace(source, destination, *args, **kwargs)
        if destination == "state.json" and failure == "durability":
            info = os.fstat(kwargs["dst_dir_fd"])
            replaced_directory = (info.st_dev, info.st_ino)
        return result
    def synced(fd):
        nonlocal replaced_directory
        import stat
        if failure == "durability" and replaced_directory is not None:
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == replaced_directory:
                replaced_directory = None
                raise OSError(errno.EIO, "directory durability failure")
        return real_sync(fd)
    with monkeypatch.context() as boundary:
        boundary.setattr(os, "replace", replaced)
        boundary.setattr(os, "fsync", synced)
        response = _listing(http, file_id)
    _assert_refusal(response, 503 if failure == "storage" else 500, {
        "durability": "state_durability", "io": "state_corrupt", "storage": "storage_low",
    }[failure])
    after = OfficeStore().read(CHAT)
    expected = json.loads(json.dumps(before))
    if failure == "durability":
        expected["sessions"][session["session_id"]].update(state="orphaned", reason="restore_epoch_changed")
    assert after == expected
    assert origin.hits == 0


@pytest.mark.parametrize("missing", ("leaf", "workspace"))
def test_epoch_final_recovery_protects_automatic_copy_and_error_outcomes(office_world, monkeypatch, missing):
    from office.store import OfficeStore
    from tests.orchestrator.test_office_callback_publish import CHANGED
    http, data, origin, _manager, _broker, _content, session = _accepted_callback(office_world, monkeypatch, final=True)
    source = _outputs(data) / "report.docx"
    source.unlink()
    if missing == "workspace":
        _outputs(data).rmdir()
    before = OfficeStore().read(CHAT)
    (data / ".office-restore-epoch").write_text("restored-final")
    response = _listing(http, session["file_id"])
    assert response.status_code == 200
    result = response.json()
    after = OfficeStore().read(CHAT)
    record = after["sessions"][session["session_id"]]
    assert result["open_session"] is None
    assert result["published_version"] == 1
    assert result["versions"][-1]["published"] is False
    assert after["documents"][session["file_id"]] == before["documents"][session["file_id"]]
    assert after["receipts"] == before["receipts"]
    assert after["journal"] == {}
    if missing == "leaf":
        assert (record["state"], record["reason"]) == ("closed", None)
        assert record["file_id"] != session["file_id"]
        assert (_outputs(data) / "report (2).docx").read_bytes() == CHANGED
        copied = _listing(http, record["file_id"])
        assert copied.status_code == 200
        assert copied.json()["published_version"] == 1
        assert copied.json()["open_session"] is None
    else:
        assert (record["state"], record["reason"]) == ("error", "workspace_missing")
        assert record["file_id"] == session["file_id"]
        assert not _outputs(data).exists()
    assert origin.hits == 0


def test_same_epoch_does_not_drive_unrelated_publication(office_world, monkeypatch):
    from office.store import OfficeStore
    http, data, origin, manager, _broker, _content, session = _accepted_callback(office_world, monkeypatch)
    file_id, unrelated = _created(office_world, "editing", name="other.docx")
    before = _snapshot(data)
    calls = list(manager._docker_client.mock_calls)
    response = _listing(http, file_id)
    assert response.status_code == 200
    assert response.json()["open_session"] == {
        "session_id": unrelated["session_id"], "state": "editing", "reason": None, "editor_ended": False,
    }
    assert response.json()["published_version"] == 1
    assert any(entry["session_id"] == session["session_id"] for entry in OfficeStore().read(CHAT)["journal"].values())
    assert _snapshot(data) == before
    assert manager._docker_client.mock_calls == calls
    assert origin.hits == 0


def test_listing_snapshot_excludes_concurrent_history_and_receipt_commit(office_world):
    import subprocess
    import sys
    from tests.orchestrator._office_store import SERVER_DIR, _child_env, _stop_child, _wait_marker

    http, data, origin, _manager, _broker = office_world
    file_id, session = _created(office_world, "editing")
    entered = data.parent / "history-reading"
    contended = data.parent / "history-writer-blocked"
    release = data.parent / "history-release"
    os.mkfifo(release)
    environment = _child_env(
        data, BASE_DATA_DIR=str(data), HISTORY_FILE=file_id,
        HISTORY_SESSION=session["session_id"], HISTORY_ENTERED=str(entered),
        HISTORY_CONTENDED=str(contended), HISTORY_RELEASE=str(release),
    )
    source = r'''
import fcntl, json, os
from pathlib import Path
from fastapi.testclient import TestClient
from office.store import OfficeStore
if os.environ["HISTORY_ROLE"] == "reader":
    import app
    real_open = os.open
    def opened(name, flags, *args, **kwargs):
        fd = real_open(name, flags, *args, **kwargs)
        if name == "state.json":
            os.open = real_open
            Path(os.environ["HISTORY_ENTERED"]).write_text("reading")
            gate = real_open(os.environ["HISTORY_RELEASE"], os.O_RDONLY)
            os.close(gate)
        return fd
    os.open = opened
    response = TestClient(app.app).get(
        "/api/office/" + os.environ["OCU_CHAT"] + "/documents/" + os.environ["HISTORY_FILE"] + "/versions",
        headers={"Authorization": "Bearer " + os.environ["OCU_INTERNAL_TOKEN"]},
    )
    print("history_result=" + json.dumps({"status": response.status_code, "body": response.json()}))
else:
    real_flock = fcntl.flock
    def contend(fd, operation):
        if operation == fcntl.LOCK_EX:
            try:
                real_flock(fd, operation | fcntl.LOCK_NB)
            except BlockingIOError:
                Path(os.environ["HISTORY_CONTENDED"]).write_text("blocked")
                fcntl.flock = real_flock
                return real_flock(fd, operation)
            real_flock(fd, fcntl.LOCK_UN)
            raise AssertionError("history snapshot released its canonical lock")
        return real_flock(fd, operation)
    fcntl.flock = contend
    def commit(state, selected):
        record = state["sessions"][os.environ["HISTORY_SESSION"]]
        record.update(state="conflict", reason="baseline_mismatch", save_seq=1, last_committed_seq=1)
        state["receipts"][record["session_id"]] = {"1": {
            "status": 2, "sha256": selected["sha256"], "version": 2, "answer": {"error": 0},
        }}
    OfficeStore().store_version(
        os.environ["OCU_CHAT"], os.environ["HISTORY_FILE"], b"concurrent user version",
        source="close", parent=1, published=False, min_free_bytes=0, mutate_state=commit,
    )
'''
    reader = writer = None
    try:
        reader = subprocess.Popen(
            [sys.executable, "-c", source], cwd=SERVER_DIR,
            env={**environment, "HISTORY_ROLE": "reader"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        _wait_marker(entered, reader, "listing did not reach state read")
        writer = subprocess.Popen(
            [sys.executable, "-c", source], cwd=SERVER_DIR,
            env={**environment, "HISTORY_ROLE": "writer"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        _wait_marker(contended, writer, "history writer did not contend on canonical flock")
        gate = os.open(release, os.O_WRONLY)
        os.close(gate)
        output, errors = reader.communicate(timeout=15)
        assert reader.returncode == 0, (output, errors)
        result = json.loads(next(line.removeprefix("history_result=") for line in output.splitlines() if line.startswith("history_result=")))
        assert result["status"] == 200
        assert [(v["number"], v["source"]) for v in result["body"]["versions"]] == [(1, "workspace")]
        assert result["body"]["open_session"] == {
            "session_id": session["session_id"], "state": "editing", "reason": None, "editor_ended": False,
        }
        writer_output, writer_errors = writer.communicate(timeout=15)
        assert writer.returncode == 0, (writer_output, writer_errors)
        after = _listing(http, file_id)
        assert after.status_code == 200
        assert [(v["number"], v["source"]) for v in after.json()["versions"]] == [(1, "workspace"), (2, "close")]
        assert after.json()["open_session"] == {
            "session_id": session["session_id"], "state": "conflict",
            "reason": "baseline_mismatch", "editor_ended": True,
        }
        assert origin.hits == 0
    finally:
        _stop_child(writer)
        _stop_child(reader)
