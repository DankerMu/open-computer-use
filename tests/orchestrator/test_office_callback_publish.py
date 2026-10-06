# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Callback publication and recovery through real HTTP, store and broker seams."""
from __future__ import annotations

import errno
import json
import os
import time
from contextlib import contextmanager

import pytest

from tests.orchestrator._office_recorded_callbacks import (
    recorded_status_1_payload,
    recorded_status_2_payload,
    recorded_status_6_payload,
    recorded_status_4_payload,
    synthetic_status_7_payload,
)
from tests.orchestrator.test_lifecycle import _container, _docker
from tests.orchestrator.test_office_callback_processing import (
    CHANGED, _bind_internal, _content_origin, _post, _sha,
)
from tests.orchestrator.test_office_control_plane import _open_session
from tests.orchestrator.test_office_publish import _on_staging_sync
from tests.orchestrator.test_office_save_close import _close, _forcesave, _hold_save, _lookup, _save
from tests.orchestrator.test_office_session_lifecycle import _change, _status
from tests.orchestrator.test_office_sessions import (
    _assert_refusal, _create, _outputs, _snapshot, _state, _versions, office_world,
)
from tests.orchestrator.test_outputs_endpoint import CHAT


def _read(data):
    return json.loads(_state(data).read_bytes())


def _opened(office_world):
    world = _open_session(office_world)
    http, _data, _origin, _manager, _broker, _content, session = world
    response = _post(http, session, recorded_status_1_payload(document_key=session["document_key"]))
    assert response.status_code == 200
    assert response.json() == {"error": 0}
    return world


def _autosaved(office_world, monkeypatch):
    world = _opened(office_world)
    http, data, _origin, _manager, _broker, content, session = world
    with _forcesave(monkeypatch, session["document_key"]) as (_origin, issued):
        accepted = _save(http, session["session_id"], "persist")
    assert accepted.status_code == 202
    assert accepted.json()["save_seq"] == 1
    assert issued == [{"save_seq": 1, "intent": "persist"}]
    with _content_origin({"/autosave.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        response = _post(http, session, recorded_status_6_payload(
            document_key=session["document_key"], url=server.url + "/autosave.docx",
            save_seq=issued[0]["save_seq"], intent=issued[0]["intent"],
        ))
    assert response.status_code == 200
    assert response.json() == {"error": 0}
    assert (_outputs(data) / "report.docx").read_bytes() == content
    assert _read(data)["documents"][session["file_id"]]["versions"][-1]["published"] is False
    return world


def _nothing_new(http, session, monkeypatch, trigger):
    if trigger == "final":
        return _post(http, session, recorded_status_4_payload(
            document_key=session["document_key"],
        ))
    with _forcesave(monkeypatch, session["document_key"], code=4):
        return _save(http, session["session_id"], "publish")


def _allocate(http, session, monkeypatch):
    with _forcesave(monkeypatch, session["document_key"]) as (_origin, issued):
        accepted = _save(http, session["session_id"], "publish")
    assert accepted.status_code == 202
    assert accepted.json()["save_seq"] == 1
    assert issued == [{"save_seq": 1, "intent": "publish"}]


def _payload(session, url, final=False, save_seq=1):
    if final:
        return recorded_status_2_payload(document_key=session["document_key"], url=url)
    return recorded_status_6_payload(
        document_key=session["document_key"], url=url, save_seq=save_seq, intent="publish",
    )


@contextmanager
def _persist_cut(monkeypatch, data, session):
    sync = os.fsync

    def synced(fd):
        sync(fd)
        persisted = _read(data)
        if any(entry.get("session_id") == session["session_id"]
               and "target_path" not in entry for entry in persisted["journal"].values()):
            raise OSError("interrupted after durable callback commit")

    with monkeypatch.context() as boundary:
        boundary.setattr(os, "fsync", synced)
        yield


def _surviving(http, data, session, monkeypatch, server, final=False):
    payload = _payload(session, server.url + "/save.docx", final)
    with _persist_cut(monkeypatch, data, session):
        response = _post(http, session, payload)
    _assert_refusal(response, 500, "state_durability")
    persisted = _read(data)
    assert len(persisted["documents"][session["file_id"]]["versions"]) == 2
    assert (_versions(data) / _sha(CHANGED)).read_bytes() == CHANGED
    assert persisted["receipts"][session["session_id"]]["1"] == {
        "status": 2 if final else 6, "version": 2,
        "sha256": _sha(CHANGED), "answer": {"error": 0},
    }
    assert list(persisted["journal"].values()) == [{
        "file_id": session["file_id"], "version": 2,
        "session_id": session["session_id"], "save_seq": 1,
        "requester": "final" if final else "save",
    }]
    assert persisted["sessions"][session["session_id"]]["last_published_seq"] == 0
    return payload


def _running(manager):
    container = _container(manager._container_name(CHAT), status="running")

    def transition(status):
        container.status = status
        container.attrs["State"].update(Status=status, Paused=status == "paused")

    container.pause.side_effect = lambda: transition("paused")
    container.unpause.side_effect = lambda: transition("running")
    transition("running")
    manager._docker_client = _docker([container])
    return container


def _unsafe_replacement(monkeypatch, workspace, content):
    def change(_fd):
        replacement = workspace.with_name("replacement.docx")
        replacement.write_bytes(content)
        replacement.replace(workspace)
    _on_staging_sync(monkeypatch, change)


def test_nothing_new_persist_keeps_autosave_unpublished_without_deferred_work(office_world, monkeypatch):
    from office.publish import recover_publications

    http, data, _origin, _manager, broker, content, session = _autosaved(office_world, monkeypatch)
    before = _read(data)
    revision = broker.OutputsBroker().current_revision(CHAT)
    blobs = _snapshot(_versions(data))
    with _forcesave(monkeypatch, session["document_key"], code=4):
        response = _save(http, session["session_id"], "persist")
    assert response.status_code == 202
    assert response.json()["save_seq"] == 2
    persisted = _read(data)
    record = persisted["sessions"][session["session_id"]]
    assert (record["state"], record["pending_save_seq"]) == ("editing", None)
    assert (record["last_committed_seq"], record["last_published_seq"]) == (2, 0)
    assert record["baseline_sha256"] == _sha(content)
    assert persisted["documents"] == before["documents"]
    assert persisted["receipts"] == before["receipts"]
    assert persisted["journal"] == {}
    assert _snapshot(_versions(data)) == blobs
    before_recovery = _snapshot(data)
    recover_publications(CHAT)
    assert _snapshot(data) == before_recovery
    assert (_outputs(data) / "report.docx").read_bytes() == content
    assert broker.OutputsBroker().current_revision(CHAT) == revision


@pytest.mark.parametrize("conflict", (False, True), ids=("published", "conflict"))
def test_status4_after_autosave_reaches_final_outcome_and_complete_replay_is_inert(office_world, monkeypatch, conflict):
    http, data, _origin, _manager, broker, content, session = _autosaved(office_world, monkeypatch)
    with _lookup(monkeypatch, session["document_key"]):
        closed = _close(http, session["session_id"])
    assert closed.status_code == 202
    assert closed.json()["save_seq"] == 2
    before = _read(data)
    revision = broker.OutputsBroker().current_revision(CHAT)
    workspace = _outputs(data) / "report.docx"
    if conflict:
        workspace.write_bytes(content + b"agent")
    response = _nothing_new(http, session, monkeypatch, "final")
    assert response.status_code == 200
    assert response.json() == {"error": 0}
    persisted = _read(data)
    record = persisted["sessions"][session["session_id"]]
    assert record["state"] == ("conflict" if conflict else "closed")
    assert record["reason"] == ("baseline_mismatch" if conflict else None)
    assert record["pending_close_seq"] is None
    assert (record["last_committed_seq"], record["last_published_seq"]) == (2, 0 if conflict else 2)
    assert record["baseline_sha256"] == _sha(content if conflict else CHANGED)
    document = persisted["documents"][session["file_id"]]
    assert document["versions"] == [
        before["documents"][session["file_id"]]["versions"][0],
        {**before["documents"][session["file_id"]]["versions"][1], "published": not conflict},
    ]
    assert document["published_version"] == (1 if conflict else 2)
    assert document["published_sha256"] == _sha(content if conflict else CHANGED)
    assert workspace.read_bytes() == (content + b"agent" if conflict else CHANGED)
    assert persisted["receipts"][session["session_id"]]["2"] == {
        "status": 4, "sha256": None, "version": None, "answer": {"error": 0},
    }
    assert persisted["journal"] == {}
    assert broker.OutputsBroker().current_revision(CHAT) == revision + (0 if conflict else 1)
    before_replay = _snapshot(data)
    assert _nothing_new(http, session, monkeypatch, "final").json() == {"error": 0}
    assert _snapshot(data) == before_replay
    assert broker.OutputsBroker().current_revision(CHAT) == revision + (0 if conflict else 1)


def test_nothing_new_save_retries_retained_version_after_pause_failed(office_world, monkeypatch):
    http, data, _origin, manager, broker, content, session = _opened(office_world)
    _allocate(http, session, monkeypatch)
    container = _running(manager)
    container.pause.side_effect = RuntimeError("external engine refused pause")
    revision = broker.OutputsBroker().current_revision(CHAT)
    with _content_origin({"/save.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        response = _post(http, session, _payload(session, server.url + "/save.docx"))
    assert response.status_code == 200
    assert response.json() == {"error": 0}
    failed = _read(data)
    record = failed["sessions"][session["session_id"]]
    assert (record["state"], record["reason"]) == ("editing", "pause_failed")
    assert (record["last_committed_seq"], record["last_published_seq"]) == (1, 0)
    assert failed["journal"] == {}
    assert failed["documents"][session["file_id"]]["versions"][1]["published"] is False
    assert (_outputs(data) / "report.docx").read_bytes() == content
    assert broker.OutputsBroker().current_revision(CHAT) == revision
    _running(manager)
    retried = _nothing_new(http, session, monkeypatch, "save")
    assert retried.status_code == 202
    assert retried.json()["save_seq"] == 2
    completed = _read(data)
    assert completed["documents"][session["file_id"]]["versions"] == [
        failed["documents"][session["file_id"]]["versions"][0],
        {**failed["documents"][session["file_id"]]["versions"][1], "published": True},
    ]
    assert completed["receipts"] == failed["receipts"]
    assert completed["journal"] == {}
    record = completed["sessions"][session["session_id"]]
    assert (record["state"], record["reason"], record["pending_save_seq"]) == ("editing", None, None)
    assert (record["last_committed_seq"], record["last_published_seq"]) == (2, 2)
    assert record["baseline_sha256"] == _sha(CHANGED)
    assert (_outputs(data) / "report.docx").read_bytes() == CHANGED
    assert broker.OutputsBroker().current_revision(CHAT) == revision + 1


@pytest.mark.parametrize("trigger", ("save", "final"))
def test_no_change_commit_retains_bound_obligation_for_startup_recovery(office_world, monkeypatch, trigger):
    from office.sweep import sweep_office_publications

    http, data, _origin, _manager, broker, content, session = _autosaved(office_world, monkeypatch)
    before = _read(data)
    revision = broker.OutputsBroker().current_revision(CHAT)
    with _persist_cut(monkeypatch, data, session):
        response = _nothing_new(http, session, monkeypatch, trigger)
    _assert_refusal(response, 500, "state_durability")
    committed = _read(data)
    assert committed["documents"] == before["documents"]
    assert list(committed["journal"].values()) == [{
        "file_id": session["file_id"], "version": 2, "session_id": session["session_id"],
        "save_seq": 2, "requester": trigger,
    }]
    record = committed["sessions"][session["session_id"]]
    assert (record["last_committed_seq"], record["last_published_seq"]) == (2, 0)
    assert record["pending_save_seq"] == (2 if trigger == "save" else None)
    assert record["state"] == ("saving" if trigger == "save" else "editing")
    if trigger == "final":
        assert committed["receipts"][session["session_id"]]["2"] == {
            "status": 4, "sha256": None, "version": None, "answer": {"error": 0},
        }
    else:
        assert committed["receipts"] == before["receipts"]
    assert (_outputs(data) / "report.docx").read_bytes() == content
    assert broker.OutputsBroker().current_revision(CHAT) == revision
    sweep_office_publications()
    completed = _read(data)
    record = completed["sessions"][session["session_id"]]
    assert record["state"] == ("editing" if trigger == "save" else "closed")
    assert record["pending_save_seq"] is None
    assert (record["last_committed_seq"], record["last_published_seq"]) == (2, 2)
    assert record["baseline_sha256"] == _sha(CHANGED)
    assert completed["documents"][session["file_id"]]["versions"] == [
        before["documents"][session["file_id"]]["versions"][0],
        {**before["documents"][session["file_id"]]["versions"][1], "published": True},
    ]
    assert completed["receipts"] == committed["receipts"]
    assert completed["journal"] == {}
    assert (_outputs(data) / "report.docx").read_bytes() == CHANGED
    assert broker.OutputsBroker().current_revision(CHAT) == revision + 1
    before_recovery = _snapshot(data)
    sweep_office_publications()
    assert _snapshot(data) == before_recovery
    assert broker.OutputsBroker().current_revision(CHAT) == revision + 1


def test_status4_duplicate_recovers_committed_version_not_newer_latest(office_world, monkeypatch):
    from office.store import OfficeStore

    http, data, _origin, _manager, broker, content, session = _autosaved(office_world, monkeypatch)
    revision = broker.OutputsBroker().current_revision(CHAT)
    with _persist_cut(monkeypatch, data, session):
        response = _nothing_new(http, session, monkeypatch, "final")
    _assert_refusal(response, 500, "state_durability")
    committed = _read(data)
    newer = OfficeStore().store_version(
        CHAT, session["file_id"], CHANGED + b"newer", source="autosave", parent=2,
        published=False, min_free_bytes=0,
    )
    assert newer["number"] == 3
    response = _nothing_new(http, session, monkeypatch, "final")
    assert response.status_code == 200
    assert response.json() == {"error": 0}
    completed = _read(data)
    document = completed["documents"][session["file_id"]]
    assert [version["published"] for version in document["versions"]] == [True, True, False]
    assert document["published_version"] == 2
    assert document["published_sha256"] == _sha(CHANGED)
    assert completed["receipts"] == committed["receipts"]
    assert completed["sessions"][session["session_id"]]["save_seq"] == 2
    assert completed["sessions"][session["session_id"]]["state"] == "closed"
    assert completed["sessions"][session["session_id"]]["last_published_seq"] == 2
    assert completed["journal"] == {}
    assert (_outputs(data) / "report.docx").read_bytes() == CHANGED
    assert broker.OutputsBroker().current_revision(CHAT) == revision + 1
    before_replay = _snapshot(data)
    assert _nothing_new(http, session, monkeypatch, "final").json() == {"error": 0}
    assert _snapshot(data) == before_replay
    assert broker.OutputsBroker().current_revision(CHAT) == revision + 1


@pytest.mark.parametrize("trigger", ("save", "final"))
def test_no_change_unresolved_publication_retains_ownership_until_recovery(office_world, monkeypatch, trigger):
    from office.publish import recover_publications

    http, data, _origin, manager, broker, content, session = _autosaved(office_world, monkeypatch)
    revision = broker.OutputsBroker().current_revision(CHAT)
    manager._docker_client = _docker([_container(manager._container_name(CHAT), status="restarting")])
    response = _nothing_new(http, session, monkeypatch, trigger)
    assert response.status_code == (202 if trigger == "save" else 200)
    if trigger == "final":
        assert response.json() == {"error": 0}
    else:
        assert response.json() == {
            "session_id": session["session_id"], "save_seq": 2, "intent": "publish",
        }
    pending = _read(data)
    assert list(pending["journal"].values()) == [{
        "file_id": session["file_id"], "version": 2, "session_id": session["session_id"],
        "save_seq": 2, "requester": trigger,
    }]
    record = pending["sessions"][session["session_id"]]
    assert (record["last_committed_seq"], record["last_published_seq"]) == (2, 0)
    assert record["pending_save_seq"] == (2 if trigger == "save" else None)
    assert record["state"] == ("saving" if trigger == "save" else "editing")
    assert (_outputs(data) / "report.docx").read_bytes() == content
    assert broker.OutputsBroker().current_revision(CHAT) == revision
    manager._docker_client = _docker()
    recover_publications(CHAT)
    completed = _read(data)
    record = completed["sessions"][session["session_id"]]
    assert record["state"] == ("editing" if trigger == "save" else "closed")
    assert record["pending_save_seq"] is None
    assert (record["last_committed_seq"], record["last_published_seq"]) == (2, 2)
    assert record["baseline_sha256"] == _sha(CHANGED)
    assert completed["documents"][session["file_id"]]["versions"][1]["published"] is True
    assert completed["journal"] == {}
    assert (_outputs(data) / "report.docx").read_bytes() == CHANGED
    assert broker.OutputsBroker().current_revision(CHAT) == revision + 1


@pytest.mark.parametrize("trigger", ("save", "final"))
def test_no_change_unexpected_publication_fault_is_visible_and_recoverable(office_world, monkeypatch, trigger):
    from office.publish import recover_publications

    http, data, _origin, _manager, broker, content, session = _autosaved(office_world, monkeypatch)
    revision = broker.OutputsBroker().current_revision(CHAT)
    replaced = os.replace

    def fail_workspace_replace(source, target, *args, **kwargs):
        if target == "report.docx":
            raise RuntimeError("unexpected host replacement failure")
        return replaced(source, target, *args, **kwargs)

    with monkeypatch.context() as boundary:
        boundary.setattr(os, "replace", fail_workspace_replace)
        with pytest.raises(RuntimeError, match="unexpected host replacement failure"):
            _nothing_new(http, session, monkeypatch, trigger)
    pending = _read(data)
    assert len(pending["journal"]) == 1
    obligation = next(iter(pending["journal"].values()))
    assert (obligation["version"], obligation["save_seq"], obligation["requester"]) == (2, 2, trigger)
    assert pending["documents"][session["file_id"]]["versions"][1]["published"] is False
    assert pending["sessions"][session["session_id"]]["last_published_seq"] == 0
    assert (_outputs(data) / "report.docx").read_bytes() == content
    assert broker.OutputsBroker().current_revision(CHAT) == revision
    recover_publications(CHAT)
    completed = _read(data)
    assert completed["journal"] == {}
    assert completed["sessions"][session["session_id"]]["state"] == (
        "editing" if trigger == "save" else "closed"
    )
    assert completed["sessions"][session["session_id"]]["last_published_seq"] == 2
    assert (_outputs(data) / "report.docx").read_bytes() == CHANGED
    assert broker.OutputsBroker().current_revision(CHAT) == revision + 1


@pytest.mark.parametrize("interruption", ("closing", "closed", "ended-conflict", "callback", "newer", "key"))
def test_delayed_nothing_new_save_preserves_live_ownership_and_final_outcomes(office_world, monkeypatch, interruption):
    http, data, _origin, _manager, broker, content, session = _autosaved(office_world, monkeypatch)
    revision = broker.OutputsBroker().current_revision(CHAT)
    _origin, seen, _infos, saver, release, box = _hold_save(
        http, monkeypatch, session, forcesave_code=4,
    )
    try:
        if interruption == "closing":
            response = _close(http, session["session_id"])
            assert response.status_code == 202
            assert response.json()["save_seq"] == 3
        elif interruption in ("closed", "ended-conflict"):
            if interruption == "ended-conflict":
                (_outputs(data) / "report.docx").write_bytes(content + b"agent")
            response = _nothing_new(http, session, monkeypatch, "final")
            assert response.status_code == 200
        elif interruption in ("callback", "newer"):
            with _content_origin({"/save.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
                response = _post(http, session, _payload(session, server.url + "/save.docx", save_seq=2))
            assert response.status_code == 200
            if interruption == "newer":
                with _forcesave(monkeypatch, session["document_key"]):
                    response = _save(http, session["session_id"], "persist")
                assert response.status_code == 202
                assert response.json()["save_seq"] == 3
        else:
            _change(session["session_id"], document_key="replacement-editor-key")
        before = _snapshot(data)
        before_revision = broker.OutputsBroker().current_revision(CHAT)
        release.set()
        saver.join(timeout=10)
        assert not saver.is_alive()
        assert saver.response.status_code == 202
        assert saver.response.json()["save_seq"] == 2
        assert seen == [{"save_seq": 2, "intent": "publish"}]
        if interruption == "closing":
            completed = _read(data)
            record = completed["sessions"][session["session_id"]]
            assert (record["state"], record["pending_save_seq"], record["pending_close_seq"]) == (
                "closing", None, 3,
            )
            assert (record["last_committed_seq"], record["last_published_seq"]) == (2, 2)
            assert record["baseline_sha256"] == _sha(CHANGED)
            assert completed["journal"] == {}
            assert completed["documents"][session["file_id"]]["versions"][1]["published"] is True
            assert (_outputs(data) / "report.docx").read_bytes() == CHANGED
            assert broker.OutputsBroker().current_revision(CHAT) == revision + 1
        else:
            assert _snapshot(data) == before
            assert broker.OutputsBroker().current_revision(CHAT) == before_revision
    finally:
        release.set()
        saver.join(timeout=10)
        box.__exit__(None, None, None)


def test_nothing_new_publish_save_publishes_retained_autosave(office_world, monkeypatch):
    http, data, recording, _manager, broker, content, session = _opened(office_world)
    session_id = session["session_id"]
    file_id = session["file_id"]
    workspace = _outputs(data) / "report.docx"
    before_revision = broker.OutputsBroker().current_revision(CHAT)
    assert workspace.read_bytes() == content
    assert CHANGED != content

    with _forcesave(monkeypatch, session["document_key"]) as (_command, issued):
        accepted = _save(http, session_id, "persist")
    assert accepted.status_code == 202
    assert accepted.json() == {
        "session_id": session_id, "save_seq": 1, "intent": "persist",
    }
    assert issued == [{"save_seq": 1, "intent": "persist"}]

    with (
        _content_origin({"/autosave.docx": CHANGED}) as server,
        _bind_internal(monkeypatch, server.url),
    ):
        payload = recorded_status_6_payload(
            document_key=session["document_key"],
            url=server.url + "/autosave.docx",
            save_seq=issued[0]["save_seq"],
            intent=issued[0]["intent"],
        )
        response = _post(http, session, payload)
        assert response.status_code == 200
        assert response.json() == {"error": 0}
        assert [request["path"] for request in server.requests] == ["/autosave.docx"]

    persisted = _read(data)
    document = persisted["documents"][file_id]
    assert [version["number"] for version in document["versions"]] == [1, 2]
    autosave = document["versions"][-1]
    assert autosave["source"] == "autosave"
    assert autosave["published"] is False
    assert autosave["sha256"] == _sha(CHANGED)
    assert (_versions(data) / autosave["sha256"]).read_bytes() == CHANGED
    assert document["published_version"] == 1
    assert document["published_sha256"] == _sha(content)
    record = persisted["sessions"][session_id]
    assert record["state"] == "editing"
    assert record["pending_save_seq"] is None
    assert record["last_committed_seq"] == 1
    assert record["last_published_seq"] == 0
    assert record["baseline_sha256"] == _sha(content)
    assert persisted["receipts"][session_id] == {
        "1": {
            "status": 6, "version": 2, "sha256": _sha(CHANGED),
            "answer": {"error": 0},
        },
    }
    assert persisted["journal"] == {}
    assert workspace.read_bytes() == content
    assert broker.OutputsBroker().current_revision(CHAT) == before_revision
    stored_blobs = _snapshot(_versions(data))

    with _forcesave(monkeypatch, session["document_key"], code=4) as (command, issued):
        published = _save(http, session_id, "publish")
    assert published.status_code == 202
    assert published.json() == {
        "session_id": session_id, "save_seq": 2, "intent": "publish",
    }
    assert issued == [{"save_seq": 2, "intent": "publish"}]
    assert len(command.requests) == 1

    completed = _read(data)
    latest = completed["documents"][file_id]
    assert [version["number"] for version in latest["versions"]] == [1, 2]
    assert _snapshot(_versions(data)) == stored_blobs
    assert completed["receipts"] == persisted["receipts"]
    record = completed["sessions"][session_id]
    assert record["state"] == "editing"
    assert record["pending_save_seq"] is None
    assert record["save_seq"] == published.json()["save_seq"]
    assert record["last_committed_seq"] == published.json()["save_seq"]

    assert workspace.read_bytes() == CHANGED
    assert latest["versions"] == [
        document["versions"][0], {**autosave, "published": True},
    ]
    assert latest["published_version"] == autosave["number"]
    assert latest["published_sha256"] == _sha(CHANGED)
    assert record["last_published_seq"] == published.json()["save_seq"]
    assert record["baseline_sha256"] == _sha(CHANGED)
    assert record["reason"] is None
    assert completed["journal"] == {}
    assert broker.OutputsBroker().current_revision(CHAT) == before_revision + 1
    status = _status(http, session_id)
    assert status.status_code == 200
    assert status.json()["state"] == "editing"
    assert status.json()["last_committed_seq"] == published.json()["save_seq"]
    assert status.json()["last_published_seq"] == published.json()["save_seq"]
    assert status.json()["workspace_changed"] is False

    with _forcesave(monkeypatch, session["document_key"], code=4):
        repeated = _save(http, session_id, "publish")
    assert repeated.status_code == 202
    assert repeated.json()["save_seq"] == 3
    after_repeat = _read(data)
    assert after_repeat["documents"] == completed["documents"]
    assert after_repeat["receipts"] == completed["receipts"]
    assert after_repeat["journal"] == {}
    assert _snapshot(_versions(data)) == stored_blobs
    assert workspace.read_bytes() == CHANGED
    assert broker.OutputsBroker().current_revision(CHAT) == before_revision + 1
    assert after_repeat["sessions"][session_id]["state"] == "editing"
    assert after_repeat["sessions"][session_id]["last_committed_seq"] == 3
    assert after_repeat["sessions"][session_id]["last_published_seq"] == 3
    assert recording.hits == 0


@pytest.mark.parametrize("final", (False, True), ids=("save", "final"))
@pytest.mark.parametrize("reason", ("pause_failed", "publish_timeout", "unsafe_path", "index_unavailable"))
def test_terminal_publish_failure_acknowledges_durable_content(office_world, monkeypatch, final, reason):
    http, data, _origin, manager, _broker, content, session = _opened(office_world)
    if not final:
        _allocate(http, session, monkeypatch)
    workspace = _outputs(data) / "report.docx"
    clock = None
    if reason in ("pause_failed", "publish_timeout"):
        container = _running(manager)
        if reason == "pause_failed":
            container.pause.side_effect = RuntimeError("external engine refused pause")
        else:
            pause = container.pause.side_effect
            monotonic = time.monotonic
            elapsed = [0.0]
            clock = lambda: monotonic() + elapsed[0]

            def slow_pause():
                pause()
                elapsed[0] = 5.1
            container.pause.side_effect = slow_pause
    elif reason == "unsafe_path":
        _unsafe_replacement(monkeypatch, workspace, content)
    else:
        (data / CHAT / ".ocu" / "index.json").write_bytes(b"{broken")
    with (
        _content_origin({"/save.docx": CHANGED}) as server,
        _bind_internal(monkeypatch, server.url),
        monkeypatch.context() as boundary,
    ):
        if clock is not None:
            boundary.setattr(time, "monotonic", clock)
        response = _post(http, session, _payload(session, server.url + "/save.docx", final))
    assert response.status_code == 200
    assert response.json() == {"error": 0}
    persisted = _read(data)
    record = persisted["sessions"][session["session_id"]]
    assert record["state"] == ("error" if final else "editing")
    assert record["reason"] == reason
    assert record["last_committed_seq"] == 1
    assert record["last_published_seq"] == 0
    assert record["baseline_sha256"] == _sha(content)
    assert persisted["documents"][session["file_id"]]["versions"][-1]["published"] is False
    assert persisted["receipts"][session["session_id"]]["1"]["answer"] == {"error": 0}
    assert (_versions(data) / _sha(CHANGED)).read_bytes() == CHANGED
    assert workspace.read_bytes() == content
    assert persisted["journal"] == {}


@pytest.mark.parametrize("final,missing", ((False, False), (False, True), (True, False)))
def test_callback_conflict_keeps_workspace_and_unpublished_version(office_world, monkeypatch, final, missing):
    http, data, _origin, _manager, _broker, content, session = _opened(office_world)
    if not final:
        _allocate(http, session, monkeypatch)
    workspace = _outputs(data) / "report.docx"
    if missing:
        workspace.unlink()
    else:
        workspace.write_bytes(content + b"agent")
    with _content_origin({"/save.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        response = _post(http, session, _payload(session, server.url + "/save.docx", final))
    assert response.status_code == 200
    assert response.json() == {"error": 0}
    persisted = _read(data)
    record = persisted["sessions"][session["session_id"]]
    assert (record["state"], record["reason"]) == (
        "conflict", "path_missing" if missing else "baseline_mismatch",
    )
    assert persisted["documents"][session["file_id"]]["versions"][-1]["published"] is False
    assert record["baseline_sha256"] == _sha(content)
    assert record["last_published_seq"] == 0
    assert (_versions(data) / _sha(CHANGED)).read_bytes() == CHANGED
    assert persisted["journal"] == {}
    if missing:
        assert not workspace.exists()
    else:
        assert workspace.read_bytes() == content + b"agent"


@pytest.mark.parametrize("final", (False, True), ids=("save", "final"))
@pytest.mark.parametrize("conflict", (False, True), ids=("published", "conflict"))
def test_duplicate_drives_only_its_validated_surviving_publication(office_world, monkeypatch, final, conflict):
    http, data, _origin, _manager, broker, content, session = _opened(office_world)
    if not final:
        _allocate(http, session, monkeypatch)
    before_revision = broker.OutputsBroker().current_revision(CHAT)
    with _content_origin({"/save.docx": CHANGED, "/different.docx": CHANGED + b"different"}) as server, _bind_internal(monkeypatch, server.url):
        payload = _surviving(http, data, session, monkeypatch, server, final)
        before = _snapshot(data)
        if not final:
            wrong_status = synthetic_status_7_payload(
                document_key=session["document_key"], save_seq=1, intent="publish",
            )
            _assert_refusal(_post(http, session, wrong_status), 409, "stale_save_seq")
            wrong_hash = _payload(session, server.url + "/different.docx")
            _assert_refusal(_post(http, session, wrong_hash), 409, "stale_save_seq")
            assert _snapshot(data) == before
        if conflict:
            (_outputs(data) / "report.docx").write_bytes(content + b"agent")
        hits = server.hits
        response = _post(http, session, payload)
        assert response.status_code == 200
        assert response.json() == {"error": 0}
        assert server.hits == hits + (0 if final else 1)
        persisted = _read(data)
        saved = persisted["documents"][session["file_id"]]["versions"]
        assert len(saved) == 2
        assert saved[-1]["published"] is (not conflict)
        assert persisted["journal"] == {}
        record = persisted["sessions"][session["session_id"]]
        assert record["state"] == ("conflict" if conflict else "closed" if final else "editing")
        assert record["last_published_seq"] == (0 if conflict else 1)
        assert (_outputs(data) / "report.docx").read_bytes() == (content + b"agent" if conflict else CHANGED)
        assert broker.OutputsBroker().current_revision(CHAT) == before_revision + (0 if conflict else 1)
        completed = _snapshot(data)
        assert _post(http, session, payload).json() == {"error": 0}
        assert _snapshot(data) == completed


def test_late_published_save_preserves_newer_outstanding_save(office_world, monkeypatch):
    http, data, _origin, _manager, _broker, _content, session = _opened(office_world)
    _change(session["session_id"], state="saving", save_seq=4, pending_save_seq=4,
            save_intents={"3": "publish", "4": "publish"}, last_committed_seq=2)
    with _content_origin({"/save.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        assert _post(http, session, _payload(session, server.url + "/save.docx", save_seq=3)).json() == {"error": 0}
    persisted = _read(data)
    record = persisted["sessions"][session["session_id"]]
    assert (record["state"], record["pending_save_seq"]) == ("saving", 4)
    assert (record["last_committed_seq"], record["last_published_seq"]) == (3, 3)
    assert record["baseline_sha256"] == _sha(CHANGED)
    assert persisted["documents"][session["file_id"]]["versions"][-1]["published"] is True
    assert (_outputs(data) / "report.docx").read_bytes() == CHANGED
    assert persisted["journal"] == {}


@pytest.mark.parametrize("conflict", (False, True))
def test_closing_save_does_not_end_session_before_final_publish(office_world, monkeypatch, conflict):
    http, data, _origin, _manager, _broker, content, session = _opened(office_world)
    _allocate(http, session, monkeypatch)
    with _lookup(monkeypatch, session["document_key"]):
        assert _close(http, session["session_id"]).status_code == 202
    workspace = _outputs(data) / "report.docx"
    if conflict:
        workspace.write_bytes(content + b"agent")
    with _content_origin({"/save.docx": CHANGED, "/final.docx": CHANGED + b"final"}) as server, _bind_internal(monkeypatch, server.url):
        assert _post(http, session, _payload(session, server.url + "/save.docx")).json() == {"error": 0}
        between = _read(data)
        assert between["sessions"][session["session_id"]]["state"] == "closing"
        assert between["documents"][session["file_id"]]["versions"][-1]["published"] is (not conflict)
        assert between["journal"] == {}
        assert _post(http, session, _payload(session, server.url + "/final.docx", True)).json() == {"error": 0}
    persisted = _read(data)
    record = persisted["sessions"][session["session_id"]]
    assert record["state"] == ("conflict" if conflict else "closed")
    assert record["reason"] == ("baseline_mismatch" if conflict else None)
    assert workspace.read_bytes() == (content + b"agent" if conflict else CHANGED + b"final")
    assert record["last_published_seq"] == (0 if conflict else 2)
    assert persisted["journal"] == {}


@pytest.mark.parametrize("trigger", ("create", "sweep"))
@pytest.mark.parametrize("outcome", ("published", "conflict", "failed"))
def test_forgotten_surviving_save_is_completed_before_orphaning(office_world, monkeypatch, trigger, outcome):
    http, data, _origin, _manager, _broker, content, session = _opened(office_world)
    _allocate(http, session, monkeypatch)
    with _content_origin({"/save.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        _surviving(http, data, session, monkeypatch, server)
    workspace = _outputs(data) / "report.docx"
    if outcome == "conflict":
        workspace.write_bytes(content + b"agent")
    elif outcome == "failed":
        _unsafe_replacement(monkeypatch, workspace, content)
    _change(session["session_id"], last_activity_at=0, saving_started_at=0)
    with _lookup(monkeypatch, session["document_key"], code=1):
        if trigger == "create":
            response = _create(http, session["file_id"])
            if outcome == "published":
                assert response.status_code == 201
            else:
                _assert_refusal(response, 409, "unpublished_version")
        else:
            from office.sweep import sweep_office_sessions
            sweep_office_sessions(now=1_700_000_000)
    persisted = _read(data)
    record = persisted["sessions"][session["session_id"]]
    assert (record["state"], record["reason"]) == ("orphaned", "editor_state_lost")
    assert persisted["documents"][session["file_id"]]["versions"][1]["published"] is (outcome == "published")
    assert persisted["journal"] == {}
    assert workspace.read_bytes() == (CHANGED if outcome == "published" else content + b"agent" if outcome == "conflict" else content)


@pytest.mark.parametrize("outcome", ("published", "conflict", "failed"))
def test_final_recovery_outcomes_are_not_orphaned_by_sweep(office_world, monkeypatch, outcome):
    http, data, _origin, _manager, _broker, content, session = _opened(office_world)
    with _content_origin({"/save.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        _surviving(http, data, session, monkeypatch, server, final=True)
    workspace = _outputs(data) / "report.docx"
    if outcome == "conflict":
        workspace.write_bytes(content + b"agent")
    elif outcome == "failed":
        _unsafe_replacement(monkeypatch, workspace, content)
    _change(session["session_id"], last_activity_at=0)
    with _lookup(monkeypatch, session["document_key"], code=1) as origin:
        from office.sweep import sweep_office_sessions
        sweep_office_sessions(now=1_700_000_000)
        assert origin.requests == []
    persisted = _read(data)
    record = persisted["sessions"][session["session_id"]]
    assert record["state"] == {"published": "closed", "conflict": "conflict", "failed": "error"}[outcome]
    assert record["reason"] == {"published": None, "conflict": "baseline_mismatch", "failed": "unsafe_path"}[outcome]
    assert persisted["journal"] == {}


@pytest.mark.parametrize("trigger", ("save", "close", "status_epoch", "callback_epoch"))
def test_request_recovers_owned_publication_before_epoch_orphan(office_world, monkeypatch, trigger):
    http, data, _origin, _manager, _broker, _content, session = _opened(office_world)
    _allocate(http, session, monkeypatch)
    with _content_origin({"/save.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        payload = _surviving(http, data, session, monkeypatch, server)
        (data / ".office-restore-epoch").write_text("new-epoch", encoding="utf-8")
        if trigger == "save":
            _assert_refusal(_save(http, session["session_id"]), 409, "session_not_editing")
        elif trigger == "close":
            _assert_refusal(_close(http, session["session_id"]), 409, "session_not_open")
        elif trigger == "status_epoch":
            status = _status(http, session["session_id"])
            assert status.status_code == 200
            assert status.json()["state"] == "orphaned"
        else:
            _assert_refusal(_post(http, session, payload), 409, "session_not_open")
    persisted = _read(data)
    record = persisted["sessions"][session["session_id"]]
    assert (record["state"], record["reason"]) == ("orphaned", "restore_epoch_changed")
    assert record["last_published_seq"] == 1
    assert persisted["documents"][session["file_id"]]["versions"][-1]["published"] is True
    assert (_outputs(data) / "report.docx").read_bytes() == CHANGED
    assert persisted["journal"] == {}


def test_callback_commit_and_terminal_outcome_are_atomic_successors(office_world, monkeypatch):
    http, data, _origin, _manager, _broker, _content, session = _opened(office_world)
    _allocate(http, session, monkeypatch)
    replaced = os.replace
    successors = []

    def observe(source, target, *args, **kwargs):
        result = replaced(source, target, *args, **kwargs)
        if target == "state.json":
            successors.append(_read(data))
        return result

    with (
        _content_origin({"/save.docx": CHANGED}) as server,
        _bind_internal(monkeypatch, server.url),
        monkeypatch.context() as boundary,
    ):
        boundary.setattr(os, "replace", observe)
        response = _post(http, session, _payload(session, server.url + "/save.docx"))
    assert response.status_code == 200
    assert response.json() == {"error": 0}
    committed = successors[0]
    assert committed["documents"][session["file_id"]]["versions"][-1]["published"] is False
    assert committed["receipts"][session["session_id"]]["1"]["version"] == 2
    assert list(committed["journal"].values()) == [{
        "file_id": session["file_id"], "version": 2, "session_id": session["session_id"],
        "save_seq": 1, "requester": "save",
    }]
    assert committed["sessions"][session["session_id"]]["state"] == "saving"
    assert committed["sessions"][session["session_id"]]["pending_save_seq"] == 1
    assert committed["sessions"][session["session_id"]]["last_published_seq"] == 0
    terminal = [state for state in successors if not state["journal"]]
    assert len(terminal) == 1
    completed = terminal[0]
    assert completed["documents"][session["file_id"]]["versions"][-1]["published"] is True
    record = completed["sessions"][session["session_id"]]
    assert (record["state"], record["last_published_seq"], record["baseline_sha256"]) == (
        "editing", 1, _sha(CHANGED),
    )
    assert record["pending_save_seq"] is None
    assert (_outputs(data) / "report.docx").read_bytes() == CHANGED


@pytest.mark.parametrize("final", (False, True))
def test_receipt_without_owned_obligation_does_not_recover_unrelated_session(office_world, monkeypatch, final):
    http, data, _origin, _manager, _broker, _content, session = _opened(office_world)
    if not final:
        _allocate(http, session, monkeypatch)
    with _content_origin({"/save.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        payload = _payload(session, server.url + "/save.docx", final)
        assert _post(http, session, payload).json() == {"error": 0}
    other_world = _open_session(office_world, name="other.docx")
    other = other_world[-1]
    assert _post(http, other, recorded_status_1_payload(document_key=other["document_key"])).status_code == 200
    _allocate(http, other, monkeypatch)
    with _content_origin({"/save.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        _surviving(http, data, other, monkeypatch, server)
        payload["url"] = server.url + "/save.docx"
        before = _snapshot(data)
        response = _post(http, session, payload)
        assert response.status_code == 200
        assert response.json() == {"error": 0}
        assert _snapshot(data) == before
    assert (_outputs(data) / "other.docx").read_bytes() == other_world[-2]
    assert len(_read(data)["journal"]) == 1


def test_startup_recovery_completes_save_as_editing_without_callback(office_world, monkeypatch):
    http, data, _origin, _manager, _broker, _content, session = _opened(office_world)
    _allocate(http, session, monkeypatch)
    with _content_origin({"/save.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        _surviving(http, data, session, monkeypatch, server)
    from office.sweep import sweep_office_publications
    sweep_office_publications()
    persisted = _read(data)
    record = persisted["sessions"][session["session_id"]]
    assert (record["state"], record["last_published_seq"]) == ("editing", 1)
    assert record["pending_save_seq"] is None
    assert record["baseline_sha256"] == _sha(CHANGED)
    assert persisted["journal"] == {}
    assert persisted["documents"][session["file_id"]]["versions"][-1]["published"] is True
    assert (_outputs(data) / "report.docx").read_bytes() == CHANGED


@pytest.mark.parametrize("trigger", ("save", "close"))
def test_unknown_key_request_publishes_survivor_before_orphaning(office_world, monkeypatch, trigger):
    http, data, _origin, _manager, _broker, _content, session = _opened(office_world)
    _allocate(http, session, monkeypatch)
    with _content_origin({"/save.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        _surviving(http, data, session, monkeypatch, server)
    if trigger == "save":
        with _forcesave(monkeypatch, session["document_key"], code=1):
            _assert_refusal(_save(http, session["session_id"]), 409, "session_not_editing")
    else:
        with _lookup(monkeypatch, session["document_key"], code=1):
            _assert_refusal(_close(http, session["session_id"]), 409, "session_not_open")
    persisted = _read(data)
    record = persisted["sessions"][session["session_id"]]
    assert (record["state"], record["reason"]) == ("orphaned", "editor_state_lost")
    assert record["last_published_seq"] == 1
    assert persisted["documents"][session["file_id"]]["versions"][-1]["published"] is True
    assert persisted["journal"] == {}
    assert (_outputs(data) / "report.docx").read_bytes() == CHANGED


@pytest.mark.parametrize("trigger", ("create", "save", "close", "status_epoch", "callback_epoch", "sweep"))
def test_unresolved_publication_blocks_orphan_transition(office_world, monkeypatch, trigger):
    from office.publish import SandboxStateError

    http, data, _origin, manager, _broker, content, session = _opened(office_world)
    _allocate(http, session, monkeypatch)
    with _content_origin({"/save.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        payload = _surviving(http, data, session, monkeypatch, server)
        container = _container(manager._container_name(CHAT), status="restarting")
        manager._docker_client = _docker([container])
        _change(session["session_id"], last_activity_at=0, saving_started_at=0)
        if trigger.endswith("epoch"):
            (data / ".office-restore-epoch").write_text("new-epoch", encoding="utf-8")
        before = _snapshot(data)
        with _lookup(monkeypatch, session["document_key"], code=1) as origin:
            if trigger == "sweep":
                from office.sweep import sweep_office_sessions
                sweep_office_sessions(now=1_700_000_000)
                assert origin.requests == []
            else:
                with pytest.raises(SandboxStateError):
                    if trigger == "create":
                        _create(http, session["file_id"])
                    elif trigger == "save":
                        _save(http, session["session_id"])
                    elif trigger == "close":
                        _close(http, session["session_id"])
                    elif trigger == "status_epoch":
                        _status(http, session["session_id"])
                    else:
                        _post(http, session, payload)
        assert _snapshot(data) == before
    persisted = _read(data)
    assert persisted["sessions"][session["session_id"]]["state"] == "saving"
    assert persisted["sessions"][session["session_id"]]["last_published_seq"] == 0
    assert persisted["documents"][session["file_id"]]["versions"][-1]["published"] is False
    assert len(persisted["journal"]) == 1
    assert (_outputs(data) / "report.docx").read_bytes() == content


@pytest.mark.parametrize("envelope", ("wrong-status", "wrong-hash", "invalid-userdata"))
def test_epoch_refusal_recovers_prior_save_without_reading_rejected_envelope(office_world, monkeypatch, envelope):
    http, data, _origin, _manager, _broker, _content, session = _opened(office_world)
    _allocate(http, session, monkeypatch)
    with _content_origin({"/save.docx": CHANGED, "/different.docx": CHANGED + b"different"}) as server, _bind_internal(monkeypatch, server.url):
        payload = _surviving(http, data, session, monkeypatch, server)
        if envelope == "wrong-status":
            payload["status"] = 7
        elif envelope == "wrong-hash":
            payload["url"] = server.url + "/different.docx"
        else:
            payload["userdata"] = "invalid"
        (data / ".office-restore-epoch").write_text("new-epoch", encoding="utf-8")
        hits = server.hits
        _assert_refusal(_post(http, session, payload), 409, "session_not_open")
        assert server.hits == hits
    persisted = _read(data)
    record = persisted["sessions"][session["session_id"]]
    assert (record["state"], record["reason"]) == ("orphaned", "restore_epoch_changed")
    assert record["last_published_seq"] == 1
    assert len(persisted["documents"][session["file_id"]]["versions"]) == 2
    assert persisted["documents"][session["file_id"]]["versions"][-1]["published"] is True
    assert (_outputs(data) / "report.docx").read_bytes() == CHANGED
    assert persisted["journal"] == {}


@pytest.mark.parametrize("trigger", ("callback_epoch", "status_epoch", "save_epoch", "close_epoch"))
@pytest.mark.parametrize("outcome", ("published", "conflict", "failed"))
def test_epoch_recovery_preserves_final_outcome(office_world, monkeypatch, trigger, outcome):
    http, data, _origin, _manager, _broker, content, session = _opened(office_world)
    with _content_origin({"/save.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        payload = _surviving(http, data, session, monkeypatch, server, final=True)
        receipts = _read(data)["receipts"]
        workspace = _outputs(data) / "report.docx"
        if outcome == "conflict":
            workspace.write_bytes(content + b"agent")
        elif outcome == "failed":
            _unsafe_replacement(monkeypatch, workspace, content)
        (data / ".office-restore-epoch").write_text("new-epoch", encoding="utf-8")
        hits = server.hits
        if trigger == "callback_epoch":
            _assert_refusal(_post(http, session, payload), 409, "session_not_open")
        elif trigger == "save_epoch":
            _assert_refusal(_save(http, session["session_id"]), 409, "session_not_editing")
        elif trigger == "close_epoch":
            _assert_refusal(_close(http, session["session_id"]), 409, "session_not_open")
        else:
            status = _status(http, session["session_id"])
            assert status.status_code == 200
            assert status.json()["state"] == {"published": "closed", "conflict": "conflict", "failed": "error"}[outcome]
        assert server.hits == hits
    persisted = _read(data)
    record = persisted["sessions"][session["session_id"]]
    assert record["state"] == {"published": "closed", "conflict": "conflict", "failed": "error"}[outcome]
    assert record["reason"] == {"published": None, "conflict": "baseline_mismatch", "failed": "unsafe_path"}[outcome]
    assert persisted["documents"][session["file_id"]]["versions"][-1]["published"] is (outcome == "published")
    assert len(persisted["documents"][session["file_id"]]["versions"]) == 2
    assert persisted["journal"] == {}
    assert persisted["receipts"] == receipts


@pytest.mark.parametrize("outcome", ("published", "conflict", "failed"))
def test_unknown_key_command_finishes_owned_obligation_before_orphaning(office_world, monkeypatch, outcome):
    http, data, _origin, _manager, _broker, content, session = _opened(office_world)
    workspace = _outputs(data) / "report.docx"
    if outcome == "conflict":
        workspace.write_bytes(content + b"agent")
    elif outcome == "failed":
        _unsafe_replacement(monkeypatch, workspace, content)
    with _content_origin({"/save.docx": CHANGED}) as server:
        def callback(_userdata):
            with _bind_internal(monkeypatch, server.url):
                _surviving(http, data, session, monkeypatch, server)
        with _forcesave(monkeypatch, session["document_key"], code=1, observe=callback):
            response = _save(http, session["session_id"])
    _assert_refusal(response, 409, "session_not_editing")
    persisted = _read(data)
    record = persisted["sessions"][session["session_id"]]
    assert (record["state"], record["reason"]) == ("orphaned", "editor_state_lost")
    assert record["last_published_seq"] == (1 if outcome == "published" else 0)
    assert persisted["documents"][session["file_id"]]["versions"][-1]["published"] is (outcome == "published")
    assert persisted["journal"] == {}
    assert workspace.read_bytes() == (CHANGED if outcome == "published" else content + b"agent" if outcome == "conflict" else content)


@pytest.mark.parametrize("recovery", ("direct", "duplicate", "startup"))
def test_interrupted_visible_publication_retains_session_ownership_until_recovery(office_world, monkeypatch, recovery):
    from tests.orchestrator.test_office_notice import _count_workspace_reads

    http, data, _origin, manager, broker, content, session = _opened(office_world)
    _allocate(http, session, monkeypatch)
    _running(manager)
    workspace = _outputs(data) / "report.docx"
    revision = broker.OutputsBroker().current_revision(CHAT)
    monotonic, replace = time.monotonic, os.replace
    elapsed = [0.0]

    def replaced(source, target, *args, **kwargs):
        result = replace(source, target, *args, **kwargs)
        if target == "report.docx":
            elapsed[0] = 5.1
        return result

    with (
        _content_origin({"/save.docx": CHANGED}) as server,
        _bind_internal(monkeypatch, server.url),
    ):
        payload = _payload(session, server.url + "/save.docx")
        with monkeypatch.context() as boundary:
            boundary.setattr(time, "monotonic", lambda: monotonic() + elapsed[0])
            boundary.setattr(os, "replace", replaced)
            response = _post(http, session, payload)
        assert response.status_code == 200
        assert response.json() == {"error": 0}
        interrupted = _read(data)
        record = interrupted["sessions"][session["session_id"]]
        assert (record["state"], record["pending_save_seq"], record["reason"]) == ("saving", 1, None)
        assert record["last_published_seq"] == 0
        assert record["baseline_sha256"] == _sha(content)
        assert interrupted["documents"][session["file_id"]]["versions"][-1]["published"] is False
        assert len(interrupted["journal"]) == 1
        assert workspace.read_bytes() == CHANGED
        assert (_versions(data) / _sha(CHANGED)).read_bytes() == CHANGED
        assert broker.OutputsBroker().current_revision(CHAT) == revision
        before_recovery_status = _status(http, session["session_id"])
        assert before_recovery_status.status_code == 200
        assert before_recovery_status.json()["workspace_changed"] is True
        assert before_recovery_status.json()["state"] == "saving"
        info = workspace.stat()
        sample = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        hits = server.hits
        if recovery == "direct":
            from office.publish import recover_publications
            recover_publications(CHAT)
        elif recovery == "duplicate":
            response = _post(http, session, payload)
            assert response.status_code == 200
            assert response.json() == {"error": 0}
        else:
            from office.sweep import sweep_office_publications
            sweep_office_publications()
        assert server.hits == hits + (1 if recovery == "duplicate" else 0)
        completed = _read(data)
        assert completed["journal"] == {}
        record = completed["sessions"][session["session_id"]]
        assert record["state"] == "editing"
        assert record["reason"] is None
        assert record["pending_save_seq"] is None
        assert record["save_seq"] == 1
        assert record["last_committed_seq"] == 1
        assert record["last_published_seq"] == 1
        assert record["baseline_sha256"] == _sha(CHANGED)
        document = completed["documents"][session["file_id"]]
        saved = dict(interrupted["documents"][session["file_id"]]["versions"][-1])
        saved["published"] = True
        assert document["versions"][-1] == saved
        assert document["versions"][:-1] == interrupted["documents"][session["file_id"]]["versions"][:-1]
        assert document["published_version"] == 2
        assert document["published_sha256"] == _sha(CHANGED)
        assert completed["receipts"] == interrupted["receipts"]
        assert workspace.read_bytes() == CHANGED
        assert (_versions(data) / _sha(CHANGED)).read_bytes() == CHANGED
        after = workspace.stat()
        assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == sample
        assert broker.OutputsBroker().current_revision(CHAT) == revision + 1

        import office.notice as notice
        with monkeypatch.context() as boundary:
            reads = _count_workspace_reads(notice, boundary, workspace)
            reconciled = _status(http, session["session_id"])
            assert reconciled.status_code == 200
            assert reconciled.json()["workspace_changed"] is False
            assert reconciled.json()["state"] == "editing"
            assert reconciled.json()["last_published_seq"] == 1
            assert reads["count"] > 0
            reads["count"] = 0
            cached = _status(http, session["session_id"])
            assert cached.status_code == 200
            assert cached.json()["workspace_changed"] is False
            assert reads["count"] == 0
            workspace.write_bytes(CHANGED + b"agent")
            changed = _status(http, session["session_id"])
            assert changed.status_code == 200
            assert changed.json()["workspace_changed"] is True
            assert changed.json()["state"] == "editing"
            assert changed.json()["last_published_seq"] == 1
            assert reads["count"] > 0
        after_notice = _read(data)
        assert after_notice["documents"] == completed["documents"]
        assert after_notice["receipts"] == completed["receipts"]
        assert after_notice["journal"] == {}
        assert after_notice["sessions"][session["session_id"]]["baseline_sha256"] == _sha(CHANGED)
        assert broker.OutputsBroker().current_revision(CHAT) == revision + 1


def test_late_failed_save_preserves_newer_outstanding_allocation(office_world, monkeypatch):
    http, data, _origin, _manager, _broker, content, session = _opened(office_world)
    _change(session["session_id"], state="saving", save_seq=4, pending_save_seq=4,
            save_intents={"3": "publish", "4": "publish"}, last_committed_seq=2)
    _unsafe_replacement(monkeypatch, _outputs(data) / "report.docx", content)
    with _content_origin({"/save.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        response = _post(http, session, _payload(session, server.url + "/save.docx", save_seq=3))
    assert response.status_code == 200
    assert response.json() == {"error": 0}
    persisted = _read(data)
    record = persisted["sessions"][session["session_id"]]
    assert (record["state"], record["pending_save_seq"], record["reason"]) == ("saving", 4, "unsafe_path")
    assert (record["last_committed_seq"], record["last_published_seq"]) == (3, 0)
    assert persisted["documents"][session["file_id"]]["versions"][-1]["published"] is False
    assert persisted["journal"] == {}
    assert (_outputs(data) / "report.docx").read_bytes() == content


@pytest.mark.parametrize("trigger", ("create", "status", "save", "close", "callback"))
def test_epoch_change_orphans_completed_final_conflict_without_obligation(office_world, monkeypatch, trigger):
    http, data, _origin, _manager, _broker, content, session = _opened(office_world)
    workspace = _outputs(data) / "report.docx"
    workspace.write_bytes(content + b"agent")
    with _content_origin({"/save.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        payload = _payload(session, server.url + "/save.docx", final=True)
        assert _post(http, session, payload).json() == {"error": 0}
        before = _read(data)
        assert before["sessions"][session["session_id"]]["state"] == "conflict"
        assert before["journal"] == {}
        (data / ".office-restore-epoch").write_text("new-epoch", encoding="utf-8")
        hits = server.hits
        if trigger == "create":
            _assert_refusal(_create(http, session["file_id"]), 409, "unpublished_version")
        elif trigger == "status":
            response = _status(http, session["session_id"])
            assert response.status_code == 200
            assert response.json()["state"] == "orphaned"
        elif trigger == "save":
            _assert_refusal(_save(http, session["session_id"]), 409, "session_not_editing")
        elif trigger == "close":
            _assert_refusal(_close(http, session["session_id"]), 409, "session_not_open")
        else:
            _assert_refusal(_post(http, session, payload), 409, "session_not_open")
        assert server.hits == hits
    expected = before
    expected["sessions"][session["session_id"]].update(
        state="orphaned", reason="restore_epoch_changed",
    )
    assert _read(data) == expected
    assert workspace.read_bytes() == content + b"agent"
    assert (_versions(data) / _sha(CHANGED)).read_bytes() == CHANGED


@pytest.mark.parametrize("trigger", ("create", "status"))
def test_recovered_final_conflict_epoch_protection_is_request_local(office_world, monkeypatch, trigger):
    http, data, _origin, _manager, _broker, content, session = _opened(office_world)
    workspace = _outputs(data) / "report.docx"
    with _content_origin({"/save.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        _surviving(http, data, session, monkeypatch, server, final=True)
        receipts = _read(data)["receipts"]
        workspace.write_bytes(content + b"agent")
        (data / ".office-restore-epoch").write_text("new-epoch", encoding="utf-8")
        if trigger == "create":
            first = _create(http, session["file_id"])
            assert first.status_code == 200
            assert first.json()["state"] == "conflict"
            assert first.json()["editor_config"] is None
        else:
            first = _status(http, session["session_id"])
            assert first.status_code == 200
            assert first.json()["state"] == "conflict"
        after_first = _read(data)
        assert after_first["journal"] == {}
        assert after_first["receipts"] == receipts
        assert after_first["sessions"][session["session_id"]]["state"] == "conflict"
        assert after_first["documents"][session["file_id"]]["versions"][-1]["published"] is False
        if trigger == "create":
            _assert_refusal(_create(http, session["file_id"]), 409, "unpublished_version")
        else:
            second = _status(http, session["session_id"])
            assert second.status_code == 200
            assert second.json()["state"] == "orphaned"
    expected = after_first
    expected["sessions"][session["session_id"]].update(
        state="orphaned", reason="restore_epoch_changed",
    )
    assert _read(data) == expected
    assert workspace.read_bytes() == content + b"agent"
    assert (_versions(data) / _sha(CHANGED)).read_bytes() == CHANGED


@pytest.mark.parametrize("final", (False, True), ids=("save", "final"))
@pytest.mark.parametrize("refusal", ("sandbox_state", "recovery_required"))
def test_durable_callback_acks_unresolved_publication_until_valid_replay(office_world, monkeypatch, final, refusal):
    http, data, _origin, manager, broker, content, session = _opened(office_world)
    if not final:
        _allocate(http, session, monkeypatch)
    revision = broker.OutputsBroker().current_revision(CHAT)
    marker = data / CHAT / ".ocu" / "office" / "fence.json"
    now = 1_700_000_000.0
    with (
        _content_origin({"/save.docx": CHANGED, "/different.docx": CHANGED + b"different"}) as server,
        _bind_internal(monkeypatch, server.url),
        monkeypatch.context() as boundary,
    ):
        if refusal == "sandbox_state":
            container = _container(manager._container_name(CHAT), status="restarting")
            manager._docker_client = _docker([container])
        else:
            boundary.setattr(time, "time", lambda: now)
            marker.write_text(json.dumps({
                "schema_version": 1, "container_id": "departed-original-container",
                "pause_started_at": now,
            }), encoding="utf-8")
            marker.chmod(0o600)
        payload = _payload(session, server.url + "/save.docx", final)
        response = _post(http, session, payload)
        assert response.status_code == 200
        assert response.json() == {"error": 0}
        pending = _read(data)
        document = pending["documents"][session["file_id"]]
        assert len(document["versions"]) == 2
        assert document["versions"][-1]["published"] is False
        assert document["versions"][-1]["source"] == ("close" if final else "save")
        assert document["published_version"] == 1
        assert document["published_sha256"] == _sha(content)
        record = pending["sessions"][session["session_id"]]
        assert record["state"] == ("editing" if final else "saving")
        assert record["reason"] is None
        assert (record["save_seq"], record["last_committed_seq"], record["last_published_seq"]) == (1, 1, 0)
        assert record["baseline_sha256"] == _sha(content)
        if not final:
            assert record["pending_save_seq"] == 1
        assert pending["receipts"][session["session_id"]]["1"] == {
            "status": 2 if final else 6, "version": 2,
            "sha256": _sha(CHANGED), "answer": {"error": 0},
        }
        assert list(pending["journal"].values()) == [{
            "file_id": session["file_id"], "version": 2,
            "session_id": session["session_id"], "save_seq": 1,
            "requester": "final" if final else "save",
        }]
        assert (_versions(data) / _sha(CHANGED)).read_bytes() == CHANGED
        assert (_outputs(data) / "report.docx").read_bytes() == content
        assert broker.OutputsBroker().current_revision(CHAT) == revision
        before_retry = _snapshot(data)
        if not final:
            wrong_status = synthetic_status_7_payload(
                document_key=session["document_key"], save_seq=1, intent="publish",
            )
            _assert_refusal(_post(http, session, wrong_status), 409, "stale_save_seq")
            wrong_hash = _payload(session, server.url + "/different.docx")
            _assert_refusal(_post(http, session, wrong_hash), 409, "stale_save_seq")
            assert _snapshot(data) == before_retry
        hits = server.hits
        duplicate = _post(http, session, payload)
        assert duplicate.status_code == 200
        assert duplicate.json() == {"error": 0}
        assert server.hits == hits + (0 if final else 1)
        assert _snapshot(data) == before_retry

        if refusal == "sandbox_state":
            manager._docker_client = _docker()
        else:
            marker.write_text(json.dumps({
                "schema_version": 1, "container_id": "departed-original-container",
                "pause_started_at": now - 6,
            }), encoding="utf-8")
        hits = server.hits
        recovered = _post(http, session, payload)
        assert recovered.status_code == 200
        assert recovered.json() == {"error": 0}
        assert server.hits == hits + (0 if final else 1)
        completed = _read(data)
        assert completed["receipts"] == pending["receipts"]
        assert len(completed["documents"][session["file_id"]]["versions"]) == 2
        assert completed["documents"][session["file_id"]]["versions"][-1]["published"] is True
        assert completed["documents"][session["file_id"]]["published_version"] == 2
        assert completed["documents"][session["file_id"]]["published_sha256"] == _sha(CHANGED)
        record = completed["sessions"][session["session_id"]]
        assert record["state"] == ("closed" if final else "editing")
        assert record["last_published_seq"] == 1
        assert record["baseline_sha256"] == _sha(CHANGED)
        assert completed["journal"] == {}
        assert not marker.exists()
        assert (_outputs(data) / "report.docx").read_bytes() == CHANGED
        assert (_versions(data) / _sha(CHANGED)).read_bytes() == CHANGED
        assert broker.OutputsBroker().current_revision(CHAT) == revision + 1
        snapshot = _snapshot(data)
        assert _post(http, session, payload).json() == {"error": 0}
        assert _snapshot(data) == snapshot
        assert broker.OutputsBroker().current_revision(CHAT) == revision + 1


@pytest.mark.parametrize("final", (False, True), ids=("save", "final"))
@pytest.mark.parametrize("delivery", ("fresh", "replay"))
@pytest.mark.parametrize("failure", ("programming", "io", "corruption", "durability"))
def test_unexpected_publication_errors_are_not_successful_callback_acks(office_world, monkeypatch, final, delivery, failure):
    http, data, _origin, manager, broker, content, session = _opened(office_world)
    if not final:
        _allocate(http, session, monkeypatch)
    revision = broker.OutputsBroker().current_revision(CHAT)
    with (
        _content_origin({"/save.docx": CHANGED}) as server,
        _bind_internal(monkeypatch, server.url),
        monkeypatch.context() as boundary,
    ):
        payload = _payload(session, server.url + "/save.docx", final)
        if delivery == "replay":
            _surviving(http, data, session, monkeypatch, server, final)
        if failure in ("programming", "io"):
            engine = _docker()
            engine.containers.get.side_effect = (
                RuntimeError("external engine adapter invariant failed")
                if failure == "programming" else OSError(errno.EIO, "engine transport IO error")
            )
            manager._docker_client = engine
        elif failure == "corruption":
            container = _container(manager._container_name(CHAT), status="exited")
            container.reload.side_effect = lambda: (
                _versions(data) / _sha(CHANGED)
            ).write_bytes(b"corrupt immutable blob")
            manager._docker_client = _docker([container])
        else:
            sync = os.fsync

            def fail_prepared_sync(fd):
                state = _read(data)
                if any(
                    entry.get("session_id") == session["session_id"] and "target_path" in entry
                    for entry in state["journal"].values()
                ):
                    raise OSError(errno.EIO, "publication directory sync failed")
                return sync(fd)
            boundary.setattr(os, "fsync", fail_prepared_sync)
        hits = server.hits
        if failure == "programming":
            with pytest.raises(RuntimeError, match="external engine adapter invariant failed"):
                _post(http, session, payload)
        else:
            response = _post(http, session, payload)
            _assert_refusal(
                response, 500, "state_durability" if failure == "durability" else "state_corrupt",
            )
        assert server.hits == hits + (0 if final and delivery == "replay" else 1)
    pending = _read(data)
    record = pending["sessions"][session["session_id"]]
    assert record["state"] == ("editing" if final else "saving")
    assert record["reason"] is None
    assert record["last_published_seq"] == 0
    assert record["baseline_sha256"] == _sha(content)
    if not final:
        assert record["pending_save_seq"] == 1
    assert len(pending["documents"][session["file_id"]]["versions"]) == 2
    assert pending["documents"][session["file_id"]]["versions"][-1]["published"] is False
    assert pending["receipts"][session["session_id"]]["1"] == {
        "status": 2 if final else 6, "version": 2,
        "sha256": _sha(CHANGED), "answer": {"error": 0},
    }
    assert len(pending["journal"]) == 1
    assert (_outputs(data) / "report.docx").read_bytes() == content
    assert broker.OutputsBroker().current_revision(CHAT) == revision
    assert (_versions(data) / _sha(CHANGED)).read_bytes() == (
        b"corrupt immutable blob" if failure == "corruption" else CHANGED
    )
