# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Actual-app join/reopen/status contracts against durable state and signed HTTP commands."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
from contextlib import contextmanager
from urllib.parse import quote

import pytest

from tests.orchestrator.test_office_commands import _command_origin
from tests.orchestrator.test_office_ooxml import (
    WORD_NS, WORD_TYPE, _content_types, _main, _package, _rels, intact_docx,
)
from tests.orchestrator.test_office_sessions import (
    FINAL_STATES, OPEN_STATES, JWT_SECRET, SELF_URL, SERVER_DIR,
    _assert_no_secrets, _assert_refusal, _create, _index_file, _office,
    _oracle_verify, _put, _sha, _snapshot, _state, _ticket_key, _versions,
    office_world,
)
from tests.orchestrator.test_office_workspace import _forbid_inode_open
from tests.orchestrator.test_outputs_endpoint import CHAT, CHAT_B, INTERNAL, MCP_KEY, _auth

INITIAL = {
    "reason": None, "save_seq": 0, "last_committed_seq": 0,
    "last_published_seq": 0, "workspace_changed": False, "saved_as": None,
}

def _changed_docx():
    return _package({
        "[Content_Types].xml": _content_types("word/document.xml", WORD_TYPE),
        "_rels/.rels": _rels("word/document.xml"),
        "word/document.xml": _main("document", WORD_NS) + b"\n",
    })



def _status(http, session_id, chat=CHAT):
    return http.get(f"/api/office/{chat}/sessions/{quote(session_id, safe='')}", headers=_auth())


def _created(world, state="opening", *, chat=CHAT, name="brief.docx"):
    http, data, _origin, _docker, broker = world
    _put(data, name, intact_docx(), chat=chat)
    file_id = _index_file(broker, data, name, chat=chat)
    response = _create(http, file_id, chat=chat)
    assert response.status_code == 201
    payload = response.json()
    _change(payload["session_id"], chat=chat, state=state)
    return file_id, payload


def _change(identity, *, chat=CHAT, **fields):
    from office.store import OfficeStore
    def mutate(state):
        state["sessions"][identity].update(fields)
    OfficeStore().update(chat, mutate)


def _ticket(payload):
    return payload["editor_config"]["document"]["url"].rsplit("/", 1)[-1]


def _verify_config(payload, file_id, number):
    from office import tokens
    config = payload["editor_config"]
    signed = _oracle_verify(config["token"], JWT_SECRET)
    assert signed == {key: value for key, value in config.items() if key != "token"}
    assert config["document"]["key"] == payload["document_key"]
    assert config["document"]["url"].startswith(SELF_URL + "/office/source/")
    assert config["editorConfig"]["callbackUrl"] == f"{SELF_URL}/office/callback/{CHAT}/{payload['session_id']}"
    expected = {"chat_id": CHAT, "file_id": file_id, "version": number, "session_id": payload["session_id"]}
    assert tokens.verify_source_ticket(_ticket(payload)) == expected
    claims = _oracle_verify(_ticket(payload), _ticket_key(INTERNAL))
    assert {key: claims[key] for key in expected} == expected


@contextmanager
def _info(monkeypatch, key, *, code=0, status=200, entered=None, release=None):
    def responder(handler, body, _parent):
        assert handler.path == "/command"
        envelope = json.loads(body)
        assert set(envelope) == {"token"}
        assert _oracle_verify(envelope["token"], JWT_SECRET) == {"c": "info", "key": key}
        if entered is not None:
            entered.set()
        if release is not None:
            assert release.wait(5), "lookup test did not release its HTTP responder"
        return status, {"Content-Type": "application/json"}, json.dumps({"error": code}).encode()
    with _command_origin(responder) as origin:
        with monkeypatch.context() as env:
            env.setenv("OCU_OFFICE_DOCSERVER_URL", origin.url)
            yield origin


def _unpublished(file_id, source="autosave"):
    from office.store import OfficeStore
    return OfficeStore().store_version(
        CHAT, file_id, _changed_docx(), source=source, parent=1,
        published=False, min_free_bytes=0,
    )


@pytest.mark.parametrize("state", OPEN_STATES)
@pytest.mark.parametrize("published", (True, False))
def test_known_open_session_joins_without_changing_history_or_baseline(office_world, monkeypatch, state, published):
    http, data, recording, _docker, _broker = office_world
    file_id, first = _created(office_world, state)
    if not published:
        _unpublished(file_id)
    # Admission observes current valid workspace bytes; tickets must not capture them.
    _put(data, "brief.docx", _changed_docx())
    before = _snapshot(data)
    with _info(monkeypatch, first["document_key"]) as origin:
        response = _create(http, file_id)
        assert len(origin.requests) == (0 if state == "opening" else 1)
    assert response.status_code == 200
    joined = response.json()
    assert {key: joined[key] for key in ("session_id", "file_id", "document_key", "state")} == {
        "session_id": first["session_id"], "file_id": file_id,
        "document_key": first["document_key"], "state": state,
    }
    assert joined["joined"] is True
    assert _ticket(joined) != _ticket(first)
    _verify_config(joined, file_id, 1 if published else 2)
    _assert_no_secrets(response, INTERNAL, MCP_KEY, JWT_SECRET)
    assert _snapshot(data) == before
    assert recording.hits == 0


def test_expired_opening_ticket_is_replaced_and_same_clock_join_is_fresh(office_world, monkeypatch):
    http, data, origin, _docker, _broker = office_world
    from office import tokens, config
    with monkeypatch.context() as clock:
        clock.setattr("time.time", lambda: 1_700_000_000)
        file_id, first = _created(office_world)
        joined = _create(http, file_id)
        assert joined.status_code == 200
        assert _ticket(first) != _ticket(joined.json())
        _verify_config(joined.json(), file_id, 1)
    before = _snapshot(data)
    with monkeypatch.context() as clock:
        clock.setattr("time.time", lambda: 1_700_000_000 + config.SOURCE_TICKET_TTL_SECONDS)
        with pytest.raises(tokens.InvalidTokenError):
            tokens.verify_source_ticket(_ticket(first))
        refreshed = _create(http, file_id)
        assert refreshed.status_code == 200
        _verify_config(refreshed.json(), file_id, 1)
        assert _ticket(refreshed.json()) != _ticket(first)
    assert _snapshot(data) == before
    assert origin.hits == 0


@pytest.mark.parametrize("state", ("editing", "saving", "closing", "conflict"))
@pytest.mark.parametrize("published", (True, False))
def test_forgotten_editor_is_durably_orphaned_before_replacement_or_offer(office_world, monkeypatch, state, published):
    http, data, recording, _docker, _broker = office_world
    from office.store import OfficeStore
    file_id, first = _created(office_world, state)
    if not published:
        _unpublished(file_id, source="conflict" if state == "conflict" else "autosave")
    before = OfficeStore().read(CHAT)
    blobs = _snapshot(_versions(data))
    workspace = _snapshot(data / CHAT / "outputs")
    with _info(monkeypatch, first["document_key"], code=1) as origin:
        response = _create(http, file_id)
        assert len(origin.requests) == 1
        after = OfficeStore().read(CHAT)
        old = after["sessions"][first["session_id"]]
        assert old == {**before["sessions"][first["session_id"]], "state": "orphaned", "reason": "editor_state_lost"}
        if published:
            assert response.status_code == 201
            assert response.json()["session_id"] != first["session_id"]
            assert response.json()["document_key"] != first["document_key"]
            assert after["documents"] == before["documents"]
        else:
            _assert_refusal(response, 409, "unpublished_version")
            assert after["documents"] == before["documents"]
            assert set(after["sessions"]) == {first["session_id"]}
            second = _create(http, file_id)
            assert second.status_code == 201
            assert len(origin.requests) == 1
            newest = OfficeStore().read(CHAT)["documents"][file_id]["versions"]
            assert newest[:2] == before["documents"][file_id]["versions"]
            assert [(item["number"], item["source"], item["published"]) for item in newest] == [
                (1, "workspace", True), (2, "conflict" if state == "conflict" else "autosave", False), (3, "workspace", True)
            ]
            assert newest[-1]["sha256"] == _sha(intact_docx())
            assert second.json()["session_id"] != first["session_id"]
    assert _snapshot(_versions(data)) == blobs
    assert _snapshot(data / CHAT / "outputs") == workspace
    assert recording.hits == 0


@pytest.mark.parametrize("callback_status", (2, 3, 4))
def test_conflict_with_final_receipt_returns_null_configuration_without_lookup(office_world, monkeypatch, callback_status):
    http, data, origin, _docker, _broker = office_world
    from office.store import OfficeStore
    file_id, first = _created(office_world, "conflict")
    _unpublished(file_id, "conflict")
    _change(first["session_id"], save_seq=4, last_committed_seq=3, last_published_seq=1, reason="baseline_mismatch")
    OfficeStore().record_receipt(CHAT, first["session_id"], 4, {
        "status": callback_status, "sha256": None, "version": None, "answer": {"error": 0},
    })
    before = _snapshot(data)
    monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", "http://127.0.0.1:9")
    response = _create(http, file_id)
    assert response.status_code == 200
    assert response.json() == {
        "session_id": first["session_id"], "file_id": file_id,
        "document_key": first["document_key"], "state": "conflict", "joined": True, "editor_config": None,
    }
    assert _snapshot(data) == before
    assert origin.hits == 0


@pytest.mark.parametrize("receipt", (None, [], {"1": {"status": 2}}, {"1": {"status": True, "sha256": None, "version": None, "answer": {}}}, {"01": {"status": 2, "sha256": None, "version": None, "answer": {}}}))
def test_malformed_conflict_receipts_fail_closed_without_lookup_or_mutation(office_world, receipt):
    http, data, origin, _docker, _broker = office_world
    from office.store import OfficeStore
    file_id, first = _created(office_world, "conflict")
    OfficeStore().update(CHAT, lambda state: state["receipts"].__setitem__(first["session_id"], receipt))
    before = _snapshot(data)
    _assert_refusal(_create(http, file_id), 500, "state_corrupt")
    assert _snapshot(data) == before
    assert origin.hits == 0


@pytest.mark.parametrize("state", ("editing", "saving", "closing", "conflict"))
@pytest.mark.parametrize("failure", ("connection", "http", "invalid_code"))
def test_unavailable_lookup_preserves_every_persisted_byte(office_world, monkeypatch, state, failure):
    http, data, recording, _docker, _broker = office_world
    file_id, first = _created(office_world, state)
    _unpublished(file_id)
    before = _snapshot(data)
    if failure == "connection":
        with socket.socket() as holder:
            holder.bind(("127.0.0.1", 0))
            monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", f"http://127.0.0.1:{holder.getsockname()[1]}")
            response = _create(http, file_id)
    else:
        with _info(monkeypatch, first["document_key"], code=1 if failure == "http" else True, status=503 if failure == "http" else 200) as origin:
            response = _create(http, file_id)
            assert len(origin.requests) == 1
    _assert_refusal(response, 502, "documentserver_unavailable")
    assert _snapshot(data) == before
    assert recording.hits == 0


@pytest.mark.parametrize("state", OPEN_STATES + FINAL_STATES)
@pytest.mark.parametrize("changed", (False, True))
def test_status_epoch_matrix_projects_persisted_fields_without_workspace_or_lookup(office_world, monkeypatch, state, changed):
    http, data, origin, _docker, _broker = office_world
    from office.store import OfficeStore
    import office.sessions as sessions
    file_id, first = _created(office_world, state)
    _change(first["session_id"], reason="stored_reason", save_seq=7, last_committed_seq=5,
            last_published_seq=3, workspace_changed=True, saved_as={"file_id": "saved-id", "path": "saved.docx"})
    before = OfficeStore().read(CHAT)
    if changed:
        (data / ".office-restore-epoch").write_text("epoch-B\n")
    encoded = _state(data).read_bytes()
    with monkeypatch.context() as trap:
        _forbid_inode_open(sessions, trap, data / CHAT / "outputs", data / CHAT / "outputs" / "brief.docx")
        response = _status(http, first["session_id"])
    assert response.status_code == 200
    orphaned = changed and state in OPEN_STATES
    expected = {
        "session_id": first["session_id"], "file_id": file_id, "document_key": first["document_key"],
        "state": "orphaned" if orphaned else state, "reason": "restore_epoch_changed" if orphaned else "stored_reason",
        "save_seq": 7, "last_committed_seq": 5, "last_published_seq": 3,
        "workspace_changed": True, "saved_as": {"file_id": "saved-id", "path": "saved.docx"},
    }
    assert response.json() == expected
    after = OfficeStore().read(CHAT)
    assert after["documents"] == before["documents"]
    assert after["receipts"] == before["receipts"]
    if not orphaned:
        assert _state(data).read_bytes() == encoded
    else:
        assert after["sessions"][first["session_id"]] == {
            **before["sessions"][first["session_id"]], "state": "orphaned", "reason": "restore_epoch_changed",
        }
    assert origin.hits == 0


@pytest.mark.parametrize("state", OPEN_STATES)
@pytest.mark.parametrize("published", (False, True))
def test_post_epoch_change_skips_lookup_and_limits_unpublished_refusal_to_this_request(office_world, monkeypatch, state, published):
    http, data, origin, _docker, _broker = office_world
    from office.store import OfficeStore
    file_id, first = _created(office_world, state)
    if not published:
        _unpublished(file_id)
    before = OfficeStore().read(CHAT)
    (data / ".office-restore-epoch").write_text("epoch-B\n")
    monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", "http://127.0.0.1:9")
    response = _create(http, file_id)
    assert OfficeStore().read(CHAT)["sessions"][first["session_id"]] == {
        **before["sessions"][first["session_id"]], "state": "orphaned", "reason": "restore_epoch_changed",
    }
    if published:
        assert response.status_code == 201
    else:
        _assert_refusal(response, 409, "unpublished_version")
        assert OfficeStore().read(CHAT)["documents"] == before["documents"]
        response = _create(http, file_id)
        assert response.status_code == 201
    assert response.json()["document_key"] != first["document_key"]
    assert OfficeStore().read(CHAT)["sessions"][response.json()["session_id"]]["restore_epoch"] == "epoch-B"
    assert origin.hits == 0


@pytest.mark.parametrize("state", FINAL_STATES)
def test_final_state_with_unpublished_history_allows_creation_despite_changed_epoch(office_world, state):
    http, data, origin, _docker, _broker = office_world
    from office.store import OfficeStore
    file_id, first = _created(office_world, state)
    _unpublished(file_id)
    old = OfficeStore().read(CHAT)["sessions"][first["session_id"]]
    (data / ".office-restore-epoch").write_text("new-epoch")
    response = _create(http, file_id)
    assert response.status_code == 201
    after = OfficeStore().read(CHAT)
    assert after["sessions"][first["session_id"]] == old
    assert [version["number"] for version in after["documents"][file_id]["versions"]] == [1, 2, 3]
    assert response.json()["session_id"] != first["session_id"]
    assert response.json()["document_key"] != first["document_key"]
    assert origin.hits == 0


def test_absent_empty_and_equal_opaque_epochs_are_distinct(office_world):
    http, data, origin, _docker, _broker = office_world
    file_id, first = _created(office_world)
    marker = data / ".office-restore-epoch"
    marker.write_text(" \n")
    response = _status(http, first["session_id"])
    assert response.json()["state"] == "orphaned"
    assert response.json()["reason"] == "restore_epoch_changed"
    second = _create(http, file_id)
    assert second.status_code == 201
    before = _state(data).read_bytes()
    marker.write_text("\t\n")
    assert _status(http, second.json()["session_id"]).json()["state"] == "opening"
    assert _state(data).read_bytes() == before
    assert origin.hits == 0


@pytest.mark.parametrize("method", ("GET", "POST"))
@pytest.mark.parametrize("marker_kind", ("invalid_utf8", "directory", "symlink"))
def test_unreadable_epoch_fails_without_mutation(office_world, method, marker_kind):
    http, data, origin, _docker, _broker = office_world
    file_id, first = _created(office_world, "editing")
    marker = data / ".office-restore-epoch"
    if marker_kind == "directory":
        marker.mkdir()
    elif marker_kind == "symlink":
        target = data.parent / "epoch-target"
        target.write_text("B")
        marker.symlink_to(target)
    else:
        marker.write_bytes(b"\xff")
    before = _snapshot(data)
    response = _status(http, first["session_id"]) if method == "GET" else _create(http, file_id)
    _assert_refusal(response, 500, "state_corrupt")
    assert _snapshot(data) == before
    assert origin.hits == 0


def test_legacy_record_projects_initial_bookkeeping_without_rewrite(office_world):
    http, data, origin, _docker, _broker = office_world
    from office.store import OfficeStore
    file_id, first = _created(office_world)
    def strip(state):
        for field in INITIAL:
            if field != "save_seq":
                state["sessions"][first["session_id"]].pop(field)
    OfficeStore().update(CHAT, strip)
    before = _snapshot(data)
    response = _status(http, first["session_id"])
    assert response.status_code == 200
    assert response.json() == {"session_id": first["session_id"], "file_id": file_id,
        "document_key": first["document_key"], "state": "opening", **INITIAL}
    assert _snapshot(data) == before
    assert origin.hits == 0


@pytest.mark.parametrize("field,bad", (
    ("reason", False), ("reason", ""), ("save_seq", True), ("save_seq", -1),
    ("last_committed_seq", "0"), ("last_committed_seq", -1), ("last_published_seq", True),
    ("last_published_seq", 1), ("workspace_changed", 0), ("saved_as", []),
    ("saved_as", {}), ("saved_as", {"file_id": "id", "path": 0}), ("restore_epoch", []),
    ("state", "edting"), ("session_id", "other"), ("file_id", ""), ("document_key", None),
))
@pytest.mark.parametrize("method", ("GET", "POST"))
def test_corrupted_consumed_session_fields_fail_closed(office_world, field, bad, method):
    http, data, origin, _docker, _broker = office_world
    file_id, first = _created(office_world)
    _change(first["session_id"], **{field: bad})
    before = _snapshot(data)
    response = _status(http, first["session_id"]) if method == "GET" else _create(http, file_id)
    _assert_refusal(response, 500, "state_corrupt")
    assert _snapshot(data) == before
    assert origin.hits == 0


@pytest.mark.parametrize("damage", ("document", "versions", "empty_versions", "multiple_open"))
def test_active_session_without_unique_history_is_corrupt_not_recaptured(office_world, damage):
    http, data, origin, _docker, _broker = office_world
    from office.store import OfficeStore
    file_id, first = _created(office_world)
    def damage_state(state):
        if damage == "document":
            state["documents"].pop(file_id)
        elif damage == "versions":
            state["documents"][file_id].pop("versions")
        elif damage == "empty_versions":
            state["documents"][file_id]["versions"] = []
        else:
            state["sessions"]["duplicate"] = {
                **state["sessions"][first["session_id"]], "session_id": "duplicate", "document_key": "duplicate-key",
            }
    OfficeStore().update(CHAT, damage_state)
    before = _snapshot(data)
    _assert_refusal(_create(http, file_id), 500, "state_corrupt")
    assert _snapshot(data) == before
    assert origin.hits == 0


@pytest.mark.parametrize("failure", ("floor", "signing", "validation"))
def test_failed_replacement_keeps_orphan_and_has_no_partial_capture(office_world, monkeypatch, failure):
    http, data, origin, _docker, _broker = office_world
    from office.store import OfficeStore
    from office import config
    file_id, first = _created(office_world, "editing")
    # Make replacement need a new blob, but use independently validated OOXML bytes.
    newer = _changed_docx()
    _put(data, "brief.docx", newer if failure != "validation" else b"not-a-document")
    before = OfficeStore().read(CHAT)
    blobs = _snapshot(_versions(data))
    (data / ".office-restore-epoch").write_text("B")
    if failure == "floor":
        monkeypatch.setattr(config, "MIN_FREE_BYTES", 10**30)
    elif failure == "signing":
        monkeypatch.delenv("OCU_OFFICE_SELF_URL")
    response = _create(http, file_id)
    _assert_refusal(response, {"floor": 503, "signing": 500, "validation": 422}[failure],
                    {"floor": "storage_low", "signing": "creation_failed", "validation": "corrupt_document"}[failure])
    after = OfficeStore().read(CHAT)
    assert after == {**before, "sessions": {
        first["session_id"]: {**before["sessions"][first["session_id"]], "state": "orphaned", "reason": "restore_epoch_changed"}
    }}
    assert _snapshot(_versions(data)) == blobs
    assert origin.hits == 0


def test_unknown_foreign_and_missing_chat_status_have_no_foreign_opens(office_world, monkeypatch):
    http, data, origin, _docker, _broker = office_world
    import office.sessions as sessions
    _file_id, foreign = _created(office_world, chat=CHAT_B)
    before = _snapshot(data / CHAT_B)
    paths = [data / CHAT_B, *(data / CHAT_B).rglob("*")]
    with monkeypatch.context() as trap:
        _forbid_inode_open(sessions, trap, *paths)
        _assert_refusal(_status(http, foreign["session_id"]), 404, "unknown_session")
        _assert_refusal(_status(http, "unknown"), 404, "unknown_session")
        _assert_refusal(_status(http, "bad\x00session"), 404, "unknown_session")
        _assert_refusal(_status(http, foreign["session_id"], chat="missing-chat"), 404, "unknown_chat")
    assert not _office(data).exists()
    assert not (data / "missing-chat").exists()
    assert _snapshot(data / CHAT_B) == before
    assert origin.hits == 0


def test_status_is_read_from_another_process_with_exact_persisted_fields(office_world):
    http, data, origin, _docker, _broker = office_world
    file_id, first = _created(office_world, "editing")
    _change(first["session_id"], save_seq=8, last_committed_seq=6, last_published_seq=4,
            reason="persisted", workspace_changed=True, saved_as={"file_id": "new-id", "path": "copy.docx"})
    expected = {"session_id": first["session_id"], "file_id": file_id, "document_key": first["document_key"],
        "state": "editing", "save_seq": 8, "last_committed_seq": 6, "last_published_seq": 4,
        "reason": "persisted", "workspace_changed": True, "saved_as": {"file_id": "new-id", "path": "copy.docx"}}
    source = '''
import json, os
from fastapi.testclient import TestClient
import app
with TestClient(app.app) as client:
    response = client.get(os.environ["STATUS_URL"], headers={"Authorization": "Bearer " + os.environ["OCU_INTERNAL_TOKEN"]})
print(json.dumps({"status": response.status_code, "body": response.json()}))
'''
    env = {**os.environ, "PYTHONPATH": str(SERVER_DIR), "BASE_DATA_DIR": str(data),
        "STATUS_URL": f"/api/office/{CHAT}/sessions/{first['session_id']}"}
    before = _snapshot(data)
    child = subprocess.run([sys.executable, "-c", source], env=env, cwd=SERVER_DIR,
                           capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout.strip().splitlines()[-1]) == {"status": 200, "body": expected}
    assert _status(http, first["session_id"]).json() == expected
    assert _snapshot(data) == before
    assert origin.hits == 0


async def _asgi(app, method, path):
    messages = []
    received = False
    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": b"", "more_body": False}
    async def send(message):
        messages.append(message)
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": method, "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": b"", "headers": [(b"authorization", f"Bearer {INTERNAL}".encode())],
        "client": ("testclient", 50000), "server": ("testserver", 80), "root_path": ""}
    await app(scope, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return start["status"], json.loads(body)


def test_delayed_lookup_serializes_same_chat_without_blocking_health(office_world, monkeypatch):
    http, data, recording, _docker, _broker = office_world
    file_id, first = _created(office_world, "editing")
    entered, release = threading.Event(), threading.Event()
    before = _snapshot(data)
    with _info(monkeypatch, first["document_key"], entered=entered, release=release) as origin:
        async def scenario():
            path = f"/api/office/{CHAT}/documents/{file_id}/sessions"
            one = asyncio.create_task(_asgi(http.app, "POST", path))
            try:
                assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 3), 4)
                two = asyncio.create_task(_asgi(http.app, "POST", path))
                health = await asyncio.wait_for(_asgi(http.app, "GET", "/health"), 1)
                assert health == (200, {"status": "healthy"})
                assert not one.done() and not two.done()
                assert len(origin.requests) == 1
                release.set()
                results = await asyncio.wait_for(asyncio.gather(one, two), 5)
                for status, body in results:
                    assert status == 200
                    assert body["session_id"] == first["session_id"]
                    assert body["document_key"] == first["document_key"]
                    assert body["joined"] is True
                assert _ticket(results[0][1]) != _ticket(results[1][1])
                assert len(origin.requests) == 2
            finally:
                release.set()
                await one
        asyncio.run(scenario())
    assert _snapshot(data) == before
    assert recording.hits == 0


@pytest.mark.parametrize("failure", ("unsafe_path", "corrupt_document", "signing"))
def test_join_admission_and_signing_refusals_preserve_session_and_history(office_world, monkeypatch, failure):
    http, data, origin, _docker, broker = office_world
    file_id, first = _created(office_world)
    path = data / CHAT / "outputs" / "brief.docx"
    if failure == "unsafe_path":
        target = data.parent / "foreign-document"
        target.write_bytes(_changed_docx())
        path.unlink()
        path.symlink_to(target)
    elif failure == "corrupt_document":
        path.write_bytes(b"not-ooxml")
    else:
        monkeypatch.delenv("OCU_OFFICE_SELF_URL")
    before = _snapshot(data)
    response = _create(http, file_id)
    status = {"unsafe_path": 422, "corrupt_document": 422, "signing": 500}[failure]
    reason = "creation_failed" if failure == "signing" else failure
    _assert_refusal(response, status, reason)
    assert _snapshot(data) == before
    assert _status(http, first["session_id"]).json()["state"] == "opening"
    assert origin.hits == 0


def test_replacement_precommit_enospc_preserves_durable_orphan_and_cleans_owned_blob(office_world, monkeypatch):
    import errno
    from office.store import OfficeStore
    import office.store as store_module
    http, data, origin, _docker, _broker = office_world
    file_id, first = _created(office_world, "editing")
    _put(data, "brief.docx", _changed_docx())
    before = OfficeStore().read(CHAT)
    blobs = _snapshot(_versions(data))
    (data / ".office-restore-epoch").write_text("B")
    original = store_module.os.write
    writes = {"states": 0}
    def fail_second_state(fd, body):
        if body[:1] == b"{":
            writes["states"] += 1
            if writes["states"] == 2:
                raise OSError(errno.ENOSPC, "injected replacement write failure")
        return original(fd, body)
    monkeypatch.setattr(store_module.os, "write", fail_second_state)
    _assert_refusal(_create(http, file_id), 503, "storage_low")
    after = OfficeStore().read(CHAT)
    assert after == {**before, "sessions": {
        first["session_id"]: {**before["sessions"][first["session_id"]], "state": "orphaned", "reason": "restore_epoch_changed"}
    }}
    assert _snapshot(_versions(data)) == blobs
    assert (data / CHAT / "outputs" / "brief.docx").read_bytes() == _changed_docx()
    assert origin.hits == 0


def test_changed_epoch_overrides_pending_conflict_final_receipt(office_world):
    from office.store import OfficeStore
    http, data, origin, _docker, _broker = office_world
    file_id, first = _created(office_world, "conflict")
    _unpublished(file_id, "conflict")
    _change(first["session_id"], save_seq=1)
    OfficeStore().record_receipt(CHAT, first["session_id"], 1, {
        "status": 2, "sha256": None, "version": None, "answer": {"error": 0},
    })
    before = OfficeStore().read(CHAT)
    (data / ".office-restore-epoch").write_text("B")
    _assert_refusal(_create(http, file_id), 409, "unpublished_version")
    after = OfficeStore().read(CHAT)
    assert after["sessions"][first["session_id"]]["state"] == "orphaned"
    assert after["sessions"][first["session_id"]]["reason"] == "restore_epoch_changed"
    assert after["documents"] == before["documents"]
    assert after["receipts"] == before["receipts"]
    assert origin.hits == 0


def test_oversized_join_refuses_before_reading_workspace_content(office_world, monkeypatch):
    http, data, origin, _docker, _broker = office_world
    import office.workspace as workspace
    file_id, first = _created(office_world)
    path = data / CHAT / "outputs" / "brief.docx"
    with path.open("wb") as stream:
        stream.truncate(100 * 1024 * 1024 + 1)
    before = _snapshot(_office(data))
    def forbid_read(*args, **kwargs):
        raise AssertionError("oversized join read document content")
    # Safe admission rejects the known size; state reads remain permitted.
    original = os.read
    identity = (path.stat().st_dev, path.stat().st_ino)
    def guarded_read(fd, amount):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == identity:
            return forbid_read()
        return original(fd, amount)
    with monkeypatch.context() as trap:
        trap.setattr(workspace.os, "read", guarded_read)
        _assert_refusal(_create(http, file_id), 413, "file_too_large")
    assert _snapshot(_office(data)) == before
    assert path.stat().st_size == 100 * 1024 * 1024 + 1
    assert _status(http, first["session_id"]).json()["state"] == "opening"
    assert origin.hits == 0


def test_key_lookup_timeout_is_bounded_and_preserves_existing_session(office_world, monkeypatch):
    from office import commands
    http, data, recording, _docker, _broker = office_world
    file_id, first = _created(office_world, "editing")
    entered, release = threading.Event(), threading.Event()
    before = _snapshot(data)
    monkeypatch.setattr(commands, "HTTP_TIMEOUT_SECONDS", 0.2)
    with _info(monkeypatch, first["document_key"], entered=entered, release=release) as origin:
        try:
            async def scenario():
                response = await asyncio.wait_for(
                    _asgi(http.app, "POST", f"/api/office/{CHAT}/documents/{file_id}/sessions"), 2
                )
                assert response == (502, {"reason": "documentserver_unavailable"})
            asyncio.run(scenario())
            assert entered.is_set()
            assert len(origin.requests) == 1
        finally:
            release.set()
    assert _snapshot(data) == before
    assert recording.hits == 0


def test_join_below_storage_floor_still_joins_while_creation_is_refused(office_world, monkeypatch):
    from office import config
    http, data, origin, _docker, broker = office_world
    file_id, first = _created(office_world)
    monkeypatch.setattr(config, "MIN_FREE_BYTES", 10**30)
    _put(data, "new-document.docx", intact_docx())
    new_id = _index_file(broker, data, "new-document.docx")
    before = _snapshot(data)

    response = _create(http, file_id)
    assert response.status_code == 200
    joined = response.json()
    assert {
        field: joined[field]
        for field in ("session_id", "file_id", "document_key", "state")
    } == {
        "session_id": first["session_id"],
        "file_id": file_id,
        "document_key": first["document_key"],
        "state": "opening",
    }
    assert joined["joined"] is True
    assert _ticket(joined) != _ticket(first)
    _verify_config(joined, file_id, 1)
    _assert_no_secrets(response, INTERNAL, MCP_KEY, JWT_SECRET)
    assert _snapshot(data) == before
    assert origin.hits == 0

    _assert_refusal(_create(http, new_id), 503, "storage_low")
    assert _snapshot(data) == before
    assert origin.hits == 0


@pytest.mark.parametrize("receipt_status", (6, 7))
@pytest.mark.parametrize("known", (True, False))
def test_non_final_receipt_does_not_suppress_reopen_key_check(
    office_world, monkeypatch, receipt_status, known
):
    from office.store import OfficeStore
    http, data, recording, _docker, _broker = office_world
    file_id, first = _created(office_world, "conflict")
    latest = _unpublished(file_id, "conflict")
    _change(
        first["session_id"], save_seq=1,
        last_committed_seq=1 if receipt_status == 6 else 0,
        reason="baseline_mismatch",
    )
    store = OfficeStore()
    store.record_receipt(CHAT, first["session_id"], 1, {
        "status": receipt_status,
        "sha256": latest["sha256"] if receipt_status == 6 else None,
        "version": latest["number"] if receipt_status == 6 else None,
        "answer": {"error": 0},
    })
    before = store.read(CHAT)
    before_files = _snapshot(data)
    with _info(monkeypatch, first["document_key"], code=0 if known else 1) as origin:
        response = _create(http, file_id)
        if known:
            assert response.status_code == 200
            joined = response.json()
            assert joined["editor_config"] is not None
            assert joined["joined"] is True
            assert {
                field: joined[field]
                for field in ("session_id", "file_id", "document_key", "state")
            } == {
                "session_id": first["session_id"],
                "file_id": file_id,
                "document_key": first["document_key"],
                "state": "conflict",
            }
            assert _ticket(joined) != _ticket(first)
            _verify_config(joined, file_id, 2)
            assert store.read(CHAT) == before
            assert _snapshot(data) == before_files
        else:
            _assert_refusal(response, 409, "unpublished_version")
            after = store.read(CHAT)
            assert after == {**before, "sessions": {
                first["session_id"]: {
                    **before["sessions"][first["session_id"]],
                    "state": "orphaned",
                    "reason": "editor_state_lost",
                },
            }}
            # The orphan state is the only changed file; unpublished content,
            # receipts, immutable blobs and the workspace retain their bytes.
            after_files = _snapshot(data)
            state_path = str(_state(data).relative_to(data))
            assert {
                path: value for path, value in after_files.items() if path != state_path
            } == {
                path: value for path, value in before_files.items() if path != state_path
            }
            assert after["documents"][file_id]["versions"][-1] == latest
            assert latest["published"] is False
        assert len(origin.requests) == 1
    assert recording.hits == 0
