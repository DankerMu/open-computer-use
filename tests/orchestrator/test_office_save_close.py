# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Public HTTP seam for Office save and close request admission."""
from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from urllib.parse import quote

import pytest

from tests.orchestrator._office_store import _stop_child, _wait_marker
from tests.orchestrator.test_office_commands import (
    _command_origin,
    _independent_verify,
)
from tests.orchestrator.test_office_ooxml import intact_docx
from tests.orchestrator.test_office_session_lifecycle import (
    _change,
    _created,
    _status,
    _verify_config,
)
from tests.orchestrator.test_office_sessions import (
    FINAL_STATES,
    JWT_SECRET,
    OPEN_STATES,
    SERVER_DIR,
    _assert_no_secrets,
    _assert_refusal,
    _create,
    _office,
    _snapshot,
    _state,
    _versions,
    office_world,
)
from tests.orchestrator.test_office_workspace import _forbid_inode_open
from tests.orchestrator.test_outputs_endpoint import CHAT, CHAT_B, INTERNAL, MCP_KEY, _auth

REFUSED_SAVE_STATES = tuple(state for state in OPEN_STATES + FINAL_STATES if state != "editing")
INVALID_BODIES = (
    None,
    b"",
    b"not-json",
    b"[]",
    b'"publish"',
    b"1",
    b"true",
    b"null",
    b"{}",
    b'{"intent":"Publish"}',
    b'{"intent":"force"}',
    b'{"intent":1}',
    b'{"intent":true}',
    b'{"intent":null}',
    b'{"intent":["publish"]}',
)


def _save(http, session_id, intent="publish", *, chat=CHAT, content=None, headers=None):
    kwargs = {"headers": headers or _auth()}
    if content is not None:
        kwargs["content"] = content
    elif intent is not None:
        kwargs["json"] = {"intent": intent}
    return http.post(
        f"/api/office/{chat}/sessions/{quote(session_id, safe='')}/save", **kwargs
    )


def _close(http, session_id, *, chat=CHAT, headers=None):
    return http.post(
        f"/api/office/{chat}/sessions/{quote(session_id, safe='')}/close",
        headers=headers or _auth(),
    )


def _record(identity):
    from office.store import OfficeStore
    return OfficeStore().read(CHAT)["sessions"][identity]


def _history(data, file_id):
    persisted = json.loads(_state(data).read_text(encoding="utf-8"))
    return persisted["documents"][file_id], persisted["receipts"], persisted["journal"]


def _assert_history_untouched(data, file_id, before_document, before_receipts, before_journal):
    document, receipts, journal = _history(data, file_id)
    assert document == before_document
    assert receipts == before_receipts
    assert journal == before_journal
    blob = _versions(data) / document["versions"][0]["sha256"]
    assert blob.read_bytes() == intact_docx()


def _copy_record(record):
    return json.loads(json.dumps(record))


def _hold_save(http, monkeypatch, first, *, forcesave_code=0, info_code=0):
    entered, release = threading.Event(), threading.Event()
    box = _command_box(
        monkeypatch,
        first["document_key"],
        forcesave_code=forcesave_code,
        info_code=info_code,
        entered=entered,
        release=release,
    )
    origin, seen, infos = box.__enter__()
    saver = threading.Thread(
        target=lambda: setattr(saver, "response", _save(http, first["session_id"], "publish"))
    )
    saver.start()
    assert entered.wait(5), "forcesave command did not arrive"
    return origin, seen, infos, saver, release, box


def _verify_command(body, document_key, save_seq, intent):
    envelope = json.loads(body.decode("utf-8"))
    assert set(envelope) == {"token"}
    payload = _independent_verify(envelope["token"], JWT_SECRET)
    assert payload["c"] == "forcesave"
    assert payload["key"] == document_key
    assert json.loads(payload["userdata"]) == {"save_seq": save_seq, "intent": intent}
    return payload


@contextmanager
def _command_box(
    monkeypatch,
    key,
    *,
    forcesave_code=0,
    forcesave_status=200,
    info_code=0,
    info_status=200,
    entered=None,
    release=None,
    observe=None,
):
    seen = []
    infos = []

    def responder(handler, body, _parent):
        assert handler.path == "/command"
        payload = json.loads(body)
        token = _independent_verify(payload["token"], JWT_SECRET)
        assert token["key"] == key
        if token["c"] == "info":
            assert set(token) == {"c", "key"}
            infos.append(token)
            return (
                info_status,
                {"Content-Type": "application/json"},
                json.dumps({"error": info_code}).encode(),
            )
        if token["c"] != "forcesave":
            raise AssertionError(("unexpected command", token))
        userdata = json.loads(token["userdata"])
        seen.append(userdata)
        if observe is not None:
            observe(userdata)
        if entered is not None:
            entered.set()
        if release is not None:
            assert release.wait(5), "save test did not release its HTTP responder"
        return (
            forcesave_status,
            {"Content-Type": "application/json"},
            json.dumps({"error": forcesave_code}).encode(),
        )

    with _command_origin(responder) as origin:
        with monkeypatch.context() as env:
            env.setenv("OCU_OFFICE_DOCSERVER_URL", origin.url)
            yield origin, seen, infos


@contextmanager
def _forcesave(monkeypatch, key, *, code=0, status=200, entered=None, release=None, observe=None):
    with _command_box(
        monkeypatch,
        key,
        forcesave_code=code,
        forcesave_status=status,
        entered=entered,
        release=release,
        observe=observe,
    ) as (origin, seen, infos):
        yield origin, seen
        assert infos == []


@contextmanager
def _lookup(monkeypatch, key, *, code=0, status=200, entered=None, release=None):
    def responder(handler, body, _parent):
        assert handler.path == "/command"
        payload = json.loads(body)
        token = _independent_verify(payload["token"], JWT_SECRET)
        assert token == {"c": "info", "key": key}
        if entered is not None:
            entered.set()
        if release is not None:
            assert release.wait(5), "close test did not release its HTTP responder"
        return status, {"Content-Type": "application/json"}, json.dumps({"error": code}).encode()

    with _command_origin(responder) as origin:
        with monkeypatch.context() as env:
            env.setenv("OCU_OFFICE_DOCSERVER_URL", origin.url)
            yield origin


def _child_env(data, origin_url, **extra):
    env = os.environ.copy()
    pythonpath = env.get("PYTHONPATH", "")
    env.update(
        {
            "PYTHONPATH": str(SERVER_DIR) + (os.pathsep + pythonpath if pythonpath else ""),
            "BASE_DATA_DIR": str(data),
            "OCU_CHAT": CHAT,
            "OCU_INTERNAL_TOKEN": INTERNAL,
            "OCU_OFFICE_JWT_SECRET": JWT_SECRET,
            "OCU_OFFICE_SELF_URL": "http://ocu:8081",
            "OCU_OFFICE_DOCSERVER_URL": origin_url,
            "OCU_OFFICE_DOCSERVER_ORIGIN": "http://docs.example:8082",
            "DOCKER_HOST": "unix:///tmp/ocu-acceptance-no-docker.sock",
            "DOCKER_SOCKET": "unix:///tmp/ocu-acceptance-no-docker.sock",
        }
    )
    env.update(extra)
    return env


def test_create_and_join_signed_config_leave_user_forcesave_off(office_world, monkeypatch):
    http, data, recording, _docker, _broker = office_world
    file_id, first = _created(office_world, "editing")
    created_config = first["editor_config"]
    created_signed = _independent_verify(created_config["token"], JWT_SECRET)
    assert created_config["editorConfig"]["customization"]["forcesave"] is False
    assert created_signed["editorConfig"]["customization"]["forcesave"] is False
    with _lookup(monkeypatch, first["document_key"]) as origin:
        joined = _create(http, file_id)
    assert joined.status_code == 200
    _verify_config(joined.json(), file_id, 1)
    assert len(origin.requests) == 1
    assert recording.hits == 0


@pytest.mark.parametrize("intent", ("publish", "persist"))
def test_accepted_save_allocates_and_sends_one_signed_forcesave(office_world, monkeypatch, intent):
    http, data, recording, _docker, _broker = office_world
    file_id, first = _created(office_world, "editing")
    before = _snapshot(data)
    document, receipts, journal = _history(data, file_id)
    observed = []

    def observe(userdata):
        persisted = json.loads(_state(data).read_text(encoding="utf-8"))
        record = persisted["sessions"][first["session_id"]]
        observed.append(dict(record))
        assert record["state"] == "saving"
        assert record["pending_save_seq"] == userdata["save_seq"]
        assert record["save_intents"][str(userdata["save_seq"])] == intent
        assert record["last_committed_seq"] == 0
        assert record["last_published_seq"] == 0

    with _forcesave(monkeypatch, first["document_key"], observe=observe) as (origin, seen):
        response = _save(http, first["session_id"], intent)
    assert response.status_code == 202
    assert response.json() == {
        "session_id": first["session_id"], "save_seq": 1, "intent": intent,
    }
    assert seen == [{"save_seq": 1, "intent": intent}]
    assert len(origin.requests) == 1
    _verify_command(origin.requests[0]["body"], first["document_key"], 1, intent)
    record = _record(first["session_id"])
    assert record["state"] == "saving"
    assert record["save_seq"] == 1
    assert record["pending_save_seq"] == 1
    assert record["save_intents"] == {"1": intent}
    assert record["last_committed_seq"] == 0
    assert record["last_published_seq"] == 0
    assert "pending_close_seq" not in record
    status = _status(http, first["session_id"]).json()
    assert status["state"] == "saving"
    assert status["save_seq"] == 1
    assert status["last_committed_seq"] == 0
    assert status["last_published_seq"] == 0
    after = _snapshot(data)
    state_path = str(_state(data).relative_to(data))
    assert {path: value for path, value in after.items() if path != state_path} == {
        path: value for path, value in before.items() if path != state_path
    }
    _assert_history_untouched(data, file_id, document, receipts, journal)
    assert observed and observed[0]["pending_save_seq"] == 1
    _assert_no_secrets(response, INTERNAL, MCP_KEY, JWT_SECRET)
    assert recording.hits == 0


def test_nothing_new_returns_editing_and_advances_only_committed_seq(office_world, monkeypatch):
    http, data, recording, _docker, _broker = office_world
    file_id, first = _created(office_world, "editing")
    _change(first["session_id"], last_committed_seq=0, last_published_seq=0)
    document, receipts, journal = _history(data, file_id)
    with _forcesave(monkeypatch, first["document_key"], code=4) as (origin, seen):
        response = _save(http, first["session_id"], "persist")
    assert response.status_code == 202
    assert response.json() == {
        "session_id": first["session_id"], "save_seq": 1, "intent": "persist",
    }
    record = _record(first["session_id"])
    assert record["state"] == "editing"
    assert record["save_seq"] == 1
    assert record["pending_save_seq"] is None
    assert record["save_intents"] == {"1": "persist"}
    assert record["last_committed_seq"] == 1
    assert record["last_published_seq"] == 0
    assert seen == [{"save_seq": 1, "intent": "persist"}]
    assert len(origin.requests) == 1
    _assert_history_untouched(data, file_id, document, receipts, journal)
    assert recording.hits == 0


@pytest.mark.parametrize("failure", ((5, 200), (0, 500), (None, None)))
def test_rejected_or_unreachable_save_retains_seq_and_returns_editing(office_world, monkeypatch, failure):
    http, data, recording, _docker, _broker = office_world
    file_id, first = _created(office_world, "editing")
    document, receipts, journal = _history(data, file_id)
    code, status = failure
    if status is None:
        monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", "http://127.0.0.1:9")
        response = _save(http, first["session_id"], "publish")
        hits = 0
    else:
        with _forcesave(monkeypatch, first["document_key"], code=code, status=status) as (origin, _seen):
            response = _save(http, first["session_id"], "publish")
        hits = len(origin.requests)
    _assert_refusal(response, 502, "documentserver_unavailable")
    record = _record(first["session_id"])
    assert record["state"] == "editing"
    assert record["save_seq"] == 1
    assert record["pending_save_seq"] is None
    assert record["save_intents"] == {"1": "publish"}
    assert record["last_committed_seq"] == 0
    assert record["last_published_seq"] == 0
    if status is not None:
        assert hits == 1
    _assert_history_untouched(data, file_id, document, receipts, journal)
    assert recording.hits == 0


def test_unknown_key_save_orphans_with_editor_state_lost(office_world, monkeypatch):
    http, data, recording, _docker, _broker = office_world
    file_id, first = _created(office_world, "editing")
    document, receipts, journal = _history(data, file_id)
    with _forcesave(monkeypatch, first["document_key"], code=1) as (origin, seen):
        response = _save(http, first["session_id"], "publish")
    _assert_refusal(response, 409, "session_not_editing")
    record = _record(first["session_id"])
    assert record["state"] == "orphaned"
    assert record["reason"] == "editor_state_lost"
    assert record["save_seq"] == 1
    assert record["pending_save_seq"] is None
    assert record["save_intents"] == {"1": "publish"}
    assert seen == [{"save_seq": 1, "intent": "publish"}]
    assert len(origin.requests) == 1
    _assert_history_untouched(data, file_id, document, receipts, journal)
    assert recording.hits == 0


@pytest.mark.parametrize("state", REFUSED_SAVE_STATES)
def test_save_outside_editing_allocates_nothing(office_world, monkeypatch, state):
    http, data, recording, _docker, _broker = office_world
    file_id, first = _created(office_world, state)
    if state == "saving":
        _change(
            first["session_id"], save_seq=2, pending_save_seq=2,
            save_intents={"2": "persist"}, last_committed_seq=1, last_published_seq=1,
        )
    before = _snapshot(data)
    monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", "http://127.0.0.1:9")
    _assert_refusal(_save(http, first["session_id"], "publish"), 409, "session_not_editing")
    assert _snapshot(data) == before
    assert recording.hits == 0


@pytest.mark.parametrize("body", INVALID_BODIES)
def test_invalid_save_body_is_invalid_request_without_mutation(office_world, body):
    http, data, origin, _docker, _broker = office_world
    _file_id, first = _created(office_world, "editing")
    before = _snapshot(data)
    kwargs = {"content": body} if body is not None else {"content": None, "intent": None}
    if body is None:
        response = http.post(
            f"/api/office/{CHAT}/sessions/{first['session_id']}/save",
            headers=_auth(),
        )
    else:
        response = _save(http, first["session_id"], content=body)
    _assert_refusal(response, 422, "invalid_request")
    assert _snapshot(data) == before
    assert origin.hits == 0


def test_unknown_foreign_and_malformed_ids_do_not_open_foreign_content(office_world, monkeypatch):
    http, data, origin, _docker, _broker = office_world
    import office.sessions as sessions
    _file_id, foreign = _created(office_world, "editing", chat=CHAT_B)
    before = _snapshot(data / CHAT_B)
    paths = [data / CHAT_B, *(data / CHAT_B).rglob("*")]
    with monkeypatch.context() as trap:
        _forbid_inode_open(sessions, trap, *paths)
        _assert_refusal(_save(http, foreign["session_id"], "publish"), 404, "unknown_session")
        _assert_refusal(_close(http, foreign["session_id"]), 404, "unknown_session")
        _assert_refusal(_save(http, "unknown", "publish"), 404, "unknown_session")
        _assert_refusal(_close(http, "unknown"), 404, "unknown_session")
        _assert_refusal(_save(http, "bad\x00session", "publish"), 404, "unknown_session")
        _assert_refusal(_close(http, "bad\x00session"), 404, "unknown_session")
        _assert_refusal(_save(http, foreign["session_id"], "publish", chat="missing-chat"), 404, "unknown_chat")
        _assert_refusal(_close(http, foreign["session_id"], chat="missing-chat"), 404, "unknown_chat")
    assert not _office(data).exists()
    assert not (data / "missing-chat").exists()
    assert _snapshot(data / CHAT_B) == before
    assert origin.hits == 0


def test_denied_internal_token_leaves_state_and_sends_no_command(office_world):
    http, data, origin, _docker, _broker = office_world
    _file_id, first = _created(office_world, "editing")
    before = _snapshot(data)
    for method in (_save, _close):
        kwargs = {"headers": {"Authorization": "Bearer not-the-configured-secret"}}
        if method is _save:
            response = method(http, first["session_id"], "publish", **kwargs)
        else:
            response = method(http, first["session_id"], **kwargs)
        assert response.status_code == 401
        assert json.loads(response.content)["reason"] == "unauthorized"
    assert _snapshot(data) == before
    assert origin.hits == 0


@pytest.mark.parametrize("state", OPEN_STATES)
def test_changed_epoch_orphans_save_and_close_without_command(office_world, monkeypatch, state):
    http, data, origin, _docker, _broker = office_world
    file_id, first = _created(office_world, state)
    if state == "saving":
        _change(first["session_id"], save_seq=3, pending_save_seq=3, save_intents={"3": "publish"})
    (data / ".office-restore-epoch").write_text("epoch-B\n")
    monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", "http://127.0.0.1:9")
    before_seq = _record(first["session_id"])["save_seq"]
    _assert_refusal(_save(http, first["session_id"], "publish"), 409, "session_not_editing")
    record = _record(first["session_id"])
    assert record["state"] == "orphaned"
    assert record["reason"] == "restore_epoch_changed"
    assert record["save_seq"] == before_seq
    _assert_refusal(_close(http, first["session_id"]), 409, "session_not_open")
    assert _record(first["session_id"])["save_seq"] == before_seq
    assert origin.hits == 0


@pytest.mark.parametrize("state", FINAL_STATES)
def test_final_epoch_mismatch_leaves_record_and_refuses_save_close(office_world, state):
    http, data, origin, _docker, _broker = office_world
    _file_id, first = _created(office_world, state)
    (data / ".office-restore-epoch").write_text("epoch-B\n")
    before = _snapshot(data)
    _assert_refusal(_save(http, first["session_id"], "publish"), 409, "session_not_editing")
    _assert_refusal(_close(http, first["session_id"]), 409, "session_not_open")
    assert _snapshot(data) == before
    assert origin.hits == 0


def test_malformed_present_allocation_metadata_is_state_corrupt(office_world):
    http, data, origin, _docker, _broker = office_world
    _file_id, first = _created(office_world, "editing")
    from office.store import OfficeStore
    for field, value in (
        ("save_intents", None),
        ("save_intents", []),
        ("save_intents", {"01": "publish"}),
        ("save_intents", {"1": "force"}),
        ("pending_save_seq", True),
        ("pending_close_seq", "1"),
        ("pending_save_seq", 1),
    ):
        _change(first["session_id"], **{field: value})
        before = _snapshot(data)
        _assert_refusal(_save(http, first["session_id"], "publish"), 500, "state_corrupt")
        _assert_refusal(_close(http, first["session_id"]), 500, "state_corrupt")
        _assert_refusal(_status(http, first["session_id"]), 500, "state_corrupt")
        assert _snapshot(data) == before
        def clear(state):
            record = state["sessions"][first["session_id"]]
            record.pop("save_intents", None)
            record.pop("pending_save_seq", None)
            record.pop("pending_close_seq", None)
        OfficeStore().update(CHAT, clear)
    assert origin.hits == 0


def test_precommit_enospc_sends_no_command(office_world, monkeypatch):
    http, data, origin, _docker, _broker = office_world
    import office.store as store_mod
    _file_id, first = _created(office_world, "editing")
    before = _snapshot(data)
    original_write = store_mod.os.write

    def fail_state_write(fd, payload):
        if payload[:1] == b"{":
            raise OSError(errno.ENOSPC, "injected state write fault")
        return original_write(fd, payload)

    monkeypatch.setattr(store_mod.os, "write", fail_state_write)
    monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", origin.url)
    _assert_refusal(_save(http, first["session_id"], "publish"), 503, "storage_low")
    assert _snapshot(data) == before
    assert origin.hits == 0


def test_postreplace_durability_failure_keeps_visible_successor_and_sends_no_command(
    office_world, monkeypatch
):
    http, data, origin, _docker, _broker = office_world
    import office.store as store_mod
    _file_id, first = _created(office_world, "editing")
    original_replace = store_mod.os.replace
    original_fsync = store_mod.os.fsync
    replaced = {"done": False}

    def watch_state_replace(src, dst, *args, **kwargs):
        result = original_replace(src, dst, *args, **kwargs)
        if dst == "state.json" or (isinstance(dst, str) and dst.endswith("state.json")):
            replaced["done"] = True
        return result

    def fail_after_state_replace(fd):
        if replaced["done"]:
            replaced["done"] = False
            raise OSError("controlled directory fsync failure")
        return original_fsync(fd)

    monkeypatch.setattr(store_mod.os, "replace", watch_state_replace)
    monkeypatch.setattr(store_mod.os, "fsync", fail_after_state_replace)
    monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", origin.url)
    _assert_refusal(_save(http, first["session_id"], "publish"), 500, "state_durability")
    record = _record(first["session_id"])
    assert record["state"] == "saving"
    assert record["save_seq"] == 1
    assert record["pending_save_seq"] == 1
    assert record["save_intents"] == {"1": "publish"}
    assert origin.hits == 0


def test_failed_save_never_recycles_sequence(office_world, monkeypatch):
    http, data, recording, _docker, _broker = office_world
    _file_id, first = _created(office_world, "editing")
    monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", "http://127.0.0.1:9")
    _assert_refusal(_save(http, first["session_id"], "publish"), 502, "documentserver_unavailable")
    assert _record(first["session_id"])["save_seq"] == 1
    with _forcesave(monkeypatch, first["document_key"]) as (origin, seen):
        response = _save(http, first["session_id"], "persist")
    assert response.json() == {
        "session_id": first["session_id"], "save_seq": 2, "intent": "persist",
    }
    record = _record(first["session_id"])
    assert record["save_intents"] == {"1": "publish", "2": "persist"}
    assert record["pending_save_seq"] == 2
    assert seen == [{"save_seq": 2, "intent": "persist"}]
    assert len(origin.requests) == 1
    assert recording.hits == 0


def test_close_opening_ends_closed_without_lookup(office_world):
    http, data, origin, _docker, _broker = office_world
    file_id, first = _created(office_world)
    workspace = data / CHAT / "outputs" / "brief.docx"
    before_bytes = workspace.read_bytes()
    document, receipts, journal = _history(data, file_id)
    response = _close(http, first["session_id"])
    assert response.status_code == 202
    assert response.json() == {
        "session_id": first["session_id"], "save_seq": 1, "state": "closed",
    }
    record = _record(first["session_id"])
    assert record["state"] == "closed"
    assert record["save_seq"] == 1
    assert "pending_close_seq" not in record
    assert workspace.read_bytes() == before_bytes
    created = _create(http, file_id)
    assert created.status_code == 201
    assert created.json()["session_id"] != first["session_id"]
    _assert_history_untouched(data, file_id, document, receipts, journal)
    assert origin.hits == 0


@pytest.mark.parametrize("state", ("editing", "saving"))
def test_close_editing_or_saving_records_pending_close(office_world, monkeypatch, state):
    http, data, recording, _docker, _broker = office_world
    file_id, first = _created(office_world, state)
    if state == "saving":
        _change(
            first["session_id"], save_seq=4, pending_save_seq=4,
            save_intents={"4": "publish"}, last_committed_seq=3, last_published_seq=2,
        )
    document, receipts, journal = _history(data, file_id)
    with _lookup(monkeypatch, first["document_key"]) as origin:
        response = _close(http, first["session_id"])
    assert response.status_code == 202
    expected_seq = 5 if state == "saving" else 1
    assert response.json() == {
        "session_id": first["session_id"], "save_seq": expected_seq, "state": "closing",
    }
    record = _record(first["session_id"])
    assert record["state"] == "closing"
    assert record["pending_close_seq"] == expected_seq
    assert record["save_seq"] == expected_seq
    if state == "saving":
        assert record["pending_save_seq"] == 4
        assert record["save_intents"] == {"4": "publish"}
    assert len(origin.requests) == 1
    _assert_history_untouched(data, file_id, document, receipts, journal)
    assert recording.hits == 0


def test_repeated_closing_reuses_pending_seq_without_lookup(office_world, monkeypatch):
    http, data, origin, _docker, _broker = office_world
    _file_id, first = _created(office_world, "closing")
    _change(first["session_id"], save_seq=6, pending_close_seq=6)
    before = _snapshot(data)
    monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", "http://127.0.0.1:9")
    first_close = _close(http, first["session_id"])
    second_close = _close(http, first["session_id"])
    assert first_close.status_code == second_close.status_code == 202
    assert first_close.json() == second_close.json() == {
        "session_id": first["session_id"], "save_seq": 6, "state": "closing",
    }
    assert _snapshot(data) == before
    assert origin.hits == 0


def test_live_conflict_records_close_and_preserves_reason(office_world):
    http, data, origin, _docker, _broker = office_world
    _file_id, first = _created(office_world, "conflict")
    _change(first["session_id"], reason="baseline_mismatch", save_seq=2, last_committed_seq=2)
    response = _close(http, first["session_id"])
    assert response.status_code == 202
    assert response.json() == {
        "session_id": first["session_id"], "save_seq": 3, "state": "conflict",
    }
    record = _record(first["session_id"])
    assert record["state"] == "conflict"
    assert record["reason"] == "baseline_mismatch"
    assert record["pending_close_seq"] == 3
    reused = _close(http, first["session_id"])
    assert reused.json()["save_seq"] == 3
    assert origin.hits == 0


@pytest.mark.parametrize("callback_status", (2, 3, 4))
def test_ended_conflict_returns_existing_seq_without_allocation(office_world, callback_status):
    http, data, origin, _docker, _broker = office_world
    from office.store import OfficeStore
    _file_id, first = _created(office_world, "conflict")
    _change(first["session_id"], save_seq=4, last_committed_seq=3, reason="baseline_mismatch")
    OfficeStore().record_receipt(CHAT, first["session_id"], 4, {
        "status": callback_status, "sha256": None, "version": None, "answer": {"error": 0},
    })
    before = _snapshot(data)
    response = _close(http, first["session_id"])
    assert response.status_code == 202
    assert response.json() == {
        "session_id": first["session_id"], "save_seq": 4, "state": "conflict",
    }
    assert _record(first["session_id"])["reason"] == "baseline_mismatch"
    assert "pending_close_seq" not in _record(first["session_id"])
    assert _snapshot(data) == before
    assert origin.hits == 0


@pytest.mark.parametrize("state", FINAL_STATES)
def test_close_of_final_session_is_session_not_open(office_world, state):
    http, data, origin, _docker, _broker = office_world
    _file_id, first = _created(office_world, state)
    before = _snapshot(data)
    _assert_refusal(_close(http, first["session_id"]), 409, "session_not_open")
    assert _snapshot(data) == before
    assert origin.hits == 0


def test_close_unknown_key_orphans_editing_session(office_world, monkeypatch):
    http, data, recording, _docker, _broker = office_world
    file_id, first = _created(office_world, "editing")
    document, receipts, journal = _history(data, file_id)
    with _lookup(monkeypatch, first["document_key"], code=1) as origin:
        response = _close(http, first["session_id"])
    _assert_refusal(response, 409, "session_not_open")
    record = _record(first["session_id"])
    assert record["state"] == "orphaned"
    assert record["reason"] == "editor_state_lost"
    assert record["save_seq"] == 0
    assert len(origin.requests) == 1
    _assert_history_untouched(data, file_id, document, receipts, journal)
    assert recording.hits == 0


def test_unreachable_close_lookup_still_records_closing(office_world, monkeypatch):
    http, data, recording, _docker, _broker = office_world
    _file_id, first = _created(office_world, "editing")
    monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", "http://127.0.0.1:9")
    response = _close(http, first["session_id"])
    assert response.status_code == 202
    assert response.json()["state"] == "closing"
    record = _record(first["session_id"])
    assert record["pending_close_seq"] == 1
    assert recording.hits == 0


def test_voided_close_allocates_a_higher_sequence(office_world, monkeypatch):
    http, data, recording, _docker, _broker = office_world
    from office.store import OfficeStore
    _file_id, first = _created(office_world, "editing")
    def void(state):
        record = state["sessions"][first["session_id"]]
        record["save_seq"] = 3
        record["pending_close_seq"] = None
        record["save_intents"] = {"1": "publish"}
        record["last_committed_seq"] = 1
    OfficeStore().update(CHAT, void)
    with _lookup(monkeypatch, first["document_key"]) as origin:
        response = _close(http, first["session_id"])
    assert response.json() == {
        "session_id": first["session_id"], "save_seq": 4, "state": "closing",
    }
    record = _record(first["session_id"])
    assert record["pending_close_seq"] == 4
    assert record["save_intents"] == {"1": "publish"}
    assert len(origin.requests) == 1
    assert recording.hits == 0


def test_two_processes_save_one_outstanding_forcesave(office_world, monkeypatch):
    http, data, recording, _docker, _broker = office_world
    _file_id, first = _created(office_world, "editing")
    entered, release = threading.Event(), threading.Event()
    ready = data.parent / "save-worker-ready"
    with _forcesave(monkeypatch, first["document_key"], entered=entered, release=release) as (origin, seen):
        env = _child_env(
            data, origin.url, OCU_SESSION=first["session_id"], READY=str(ready),
        )
        child_src = r"""
import json, os, sys
from pathlib import Path
from fastapi.testclient import TestClient
import app
with TestClient(app.app) as client:
    Path(os.environ["READY"]).write_text("ready")
    sys.stdin.readline()
    response = client.post(
        f'/api/office/{os.environ["OCU_CHAT"]}/sessions/{os.environ["OCU_SESSION"]}/save',
        headers={"Authorization": "Bearer " + os.environ["OCU_INTERNAL_TOKEN"]},
        json={"intent": "persist"},
    )
print(json.dumps({"status": response.status_code, "body": response.json()}))
"""
        child = subprocess.Popen(
            [sys.executable, "-c", child_src], cwd=str(SERVER_DIR), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            _wait_marker(ready, child, "second worker did not load the actual app")
            parent = threading.Thread(
                target=lambda: setattr(parent, "response", _save(http, first["session_id"], "publish"))
            )
            parent.start()
            assert entered.wait(5)
            child.stdin.write("go\n")
            child.stdin.flush()
            stdout, stderr = child.communicate(timeout=30)
            release.set()
            parent.join(timeout=10)
            assert parent.response.status_code == 202
            child_response = json.loads(stdout.strip().splitlines()[-1])
            assert child.returncode == 0, stderr
            assert child_response["status"] == 409
            assert child_response["body"] == {"reason": "session_not_editing"}
            assert seen == [{"save_seq": 1, "intent": "publish"}]
            assert len(origin.requests) == 1
            record = _record(first["session_id"])
            assert record["pending_save_seq"] == 1
            assert record["save_intents"] == {"1": "publish"}
        finally:
            release.set()
            _stop_child(child)
    assert recording.hits == 0


def test_two_processes_close_reuse_one_allocation(office_world, monkeypatch):
    http, data, recording, _docker, _broker = office_world
    _file_id, first = _created(office_world, "editing")
    entered, release = threading.Event(), threading.Event()
    ready = data.parent / "close-worker-ready"
    with _lookup(monkeypatch, first["document_key"], entered=entered, release=release) as origin:
        env = _child_env(
            data, origin.url, OCU_SESSION=first["session_id"], READY=str(ready),
        )
        child_src = r"""
import json, os, sys
from pathlib import Path
from fastapi.testclient import TestClient
import app
with TestClient(app.app) as client:
    Path(os.environ["READY"]).write_text("ready")
    sys.stdin.readline()
    response = client.post(
        f'/api/office/{os.environ["OCU_CHAT"]}/sessions/{os.environ["OCU_SESSION"]}/close',
        headers={"Authorization": "Bearer " + os.environ["OCU_INTERNAL_TOKEN"]},
    )
print(json.dumps({"status": response.status_code, "body": response.json()}))
"""
        child = subprocess.Popen(
            [sys.executable, "-c", child_src], cwd=str(SERVER_DIR), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            _wait_marker(ready, child, "second worker did not load the actual app")
            parent = threading.Thread(
                target=lambda: setattr(parent, "response", _close(http, first["session_id"]))
            )
            parent.start()
            assert entered.wait(5)
            child.stdin.write("go\n")
            child.stdin.flush()
            stdout, stderr = child.communicate(timeout=30)
            release.set()
            parent.join(timeout=10)
            child_response = json.loads(stdout.strip().splitlines()[-1])
            assert child.returncode == 0, stderr
            assert parent.response.status_code == child_response["status"] == 202
            assert parent.response.json() == child_response["body"] == {
                "session_id": first["session_id"], "save_seq": 1, "state": "closing",
            }
            assert _record(first["session_id"])["pending_close_seq"] == 1
            assert len(origin.requests) == 1
        finally:
            release.set()
            _stop_child(child)
    assert recording.hits == 0


def test_callback_side_acquires_lock_before_forcesave_response(office_world, monkeypatch):
    http, data, recording, _docker, _broker = office_world
    import docker_manager
    from office.store import OfficeStore
    _file_id, first = _created(office_world, "editing")
    entered, release = threading.Event(), threading.Event()
    proof = {}

    def observe(_userdata):
        with docker_manager._combined_lock(CHAT):
            record = OfficeStore().read(CHAT)["sessions"][first["session_id"]]
            proof["state"] = record["state"]
            proof["pending_save_seq"] = record["pending_save_seq"]
            record["reason"] = "callback-side"
            def mutate(state):
                state["sessions"][first["session_id"]]["reason"] = "callback-side"
            OfficeStore().update(CHAT, mutate)

    with _forcesave(
        monkeypatch, first["document_key"], entered=entered, release=release, observe=observe,
    ) as (origin, seen):
        parent = threading.Thread(
            target=lambda: setattr(parent, "response", _save(http, first["session_id"], "publish"))
        )
        parent.start()
        assert entered.wait(5)
        assert proof == {"state": "saving", "pending_save_seq": 1}
        assert _record(first["session_id"])["reason"] == "callback-side"
        release.set()
        parent.join(timeout=10)
        assert parent.response.status_code == 202
        assert seen == [{"save_seq": 1, "intent": "publish"}]
        assert len(origin.requests) == 1
        assert _record(first["session_id"])["reason"] == "callback-side"
    assert recording.hits == 0


def test_delayed_accepted_save_keeps_concurrent_close(office_world, monkeypatch):
    http, data, recording, _docker, _broker = office_world
    _file_id, first = _created(office_world, "editing")
    origin, seen, infos, saver, release, box = _hold_save(http, monkeypatch, first)
    try:
        closed = _close(http, first["session_id"])
        assert closed.status_code == 202
        assert closed.json()["state"] == "closing"
        expected = _copy_record(_record(first["session_id"]))
        assert expected["pending_save_seq"] == 1
        assert expected["pending_close_seq"] == 2
        release.set()
        saver.join(timeout=10)
        assert saver.response.status_code == 202
        assert _record(first["session_id"]) == expected
        assert seen == [{"save_seq": 1, "intent": "publish"}]
        assert infos == [{"c": "info", "key": first["document_key"]}]
        assert len(origin.requests) == 2
    finally:
        release.set()
        box.__exit__(None, None, None)
    assert recording.hits == 0


@pytest.mark.parametrize("code,status,http_status", (
    (4, 200, 202),
    (5, 200, 502),
    (0, 500, 502),
))
def test_delayed_ordinary_save_failure_preserves_concurrent_close(
    office_world, monkeypatch, code, status, http_status
):
    http, data, recording, _docker, _broker = office_world
    _file_id, first = _created(office_world, "editing")
    entered, release = threading.Event(), threading.Event()
    with _command_box(
        monkeypatch, first["document_key"], forcesave_code=code, forcesave_status=status,
        entered=entered, release=release,
    ) as (origin, seen, infos):
        saver = threading.Thread(
            target=lambda: setattr(saver, "response", _save(http, first["session_id"], "publish"))
        )
        saver.start()
        assert entered.wait(5)
        closed = _close(http, first["session_id"])
        assert closed.status_code == 202
        expected = _copy_record(_record(first["session_id"]))
        release.set()
        saver.join(timeout=10)
        assert saver.response.status_code == http_status
        after = _record(first["session_id"])
        if code == 4:
            assert after["state"] == "closing"
            assert after["pending_close_seq"] == 2
            assert after["pending_save_seq"] is None
            assert after["last_committed_seq"] == 1
            assert after["last_published_seq"] == 0
            assert after["save_intents"] == {"1": "publish"}
        else:
            expected["pending_save_seq"] = None
            assert after == expected
            assert after["state"] == "closing"
            assert after["pending_close_seq"] == 2
            assert after["save_intents"] == {"1": "publish"}
        assert seen == [{"save_seq": 1, "intent": "publish"}]
        assert infos == [{"c": "info", "key": first["document_key"]}]
        assert len(origin.requests) == 2
    assert recording.hits == 0


def test_delayed_unknown_key_orphans_live_close_and_conflict(office_world, monkeypatch):
    http, data, recording, _docker, _broker = office_world
    from office.store import OfficeStore
    _file_id, first = _created(office_world, "editing")
    origin, seen, infos, saver, release, box = _hold_save(
        http, monkeypatch, first, forcesave_code=1,
    )
    try:
        closed = _close(http, first["session_id"])
        assert closed.status_code == 202
        before = _copy_record(_record(first["session_id"]))
        assert before["state"] == "closing"
        release.set()
        saver.join(timeout=10)
        assert saver.response.status_code == 409
        after = _record(first["session_id"])
        assert after["state"] == "orphaned"
        assert after["reason"] == "editor_state_lost"
        assert after["pending_close_seq"] == before["pending_close_seq"]
        assert after["save_intents"] == before["save_intents"]
        assert after["save_seq"] == before["save_seq"]
        assert seen == [{"save_seq": 1, "intent": "publish"}]
        assert infos == [{"c": "info", "key": first["document_key"]}]
    finally:
        release.set()
        box.__exit__(None, None, None)

    _file_id, live = _created(office_world, "editing", name="live.docx")
    origin, seen, infos, saver, release, box = _hold_save(
        http, monkeypatch, live, forcesave_code=1,
    )
    try:
        def conflict(state):
            record = state["sessions"][live["session_id"]]
            record["state"] = "conflict"
            record["reason"] = "baseline_mismatch"
        OfficeStore().update(CHAT, conflict)
        expected_seq = _record(live["session_id"])["save_seq"]
        release.set()
        saver.join(timeout=10)
        assert saver.response.status_code == 409
        after = _record(live["session_id"])
        assert after["state"] == "orphaned"
        assert after["reason"] == "editor_state_lost"
        assert after["save_seq"] == expected_seq
        assert after["save_intents"] == {"1": "publish"}
    finally:
        release.set()
        box.__exit__(None, None, None)
    assert recording.hits == 0


@pytest.mark.parametrize("callback_status", (2, 3, 4))
def test_delayed_unknown_key_does_not_orphan_ended_conflict(office_world, monkeypatch, callback_status):
    http, data, recording, _docker, _broker = office_world
    from office.store import OfficeStore
    _file_id, first = _created(office_world, "editing")
    origin, seen, infos, saver, release, box = _hold_save(
        http, monkeypatch, first, forcesave_code=1,
    )
    try:
        def end_conflict(state):
            record = state["sessions"][first["session_id"]]
            record["state"] = "conflict"
            record["reason"] = "baseline_mismatch"
            record["last_committed_seq"] = 1
        OfficeStore().update(CHAT, end_conflict)
        OfficeStore().record_receipt(CHAT, first["session_id"], 1, {
            "status": callback_status, "sha256": None, "version": None, "answer": {"error": 0},
        })
        expected = _copy_record(_record(first["session_id"]))
        release.set()
        saver.join(timeout=10)
        assert saver.response.status_code == 409
        assert _record(first["session_id"]) == expected
        assert expected["state"] == "conflict"
        assert expected["reason"] == "baseline_mismatch"
        assert expected["pending_save_seq"] == 1
        assert seen == [{"save_seq": 1, "intent": "publish"}]
        assert infos == []
    finally:
        release.set()
        box.__exit__(None, None, None)
    assert recording.hits == 0


def test_delayed_nothing_new_does_not_touch_completed_callback(office_world, monkeypatch):
    http, data, recording, _docker, _broker = office_world
    from office.store import OfficeStore
    _file_id, first = _created(office_world, "editing")
    origin, seen, infos, saver, release, box = _hold_save(
        http, monkeypatch, first, forcesave_code=4,
    )
    try:
        def complete(state):
            record = state["sessions"][first["session_id"]]
            record["state"] = "editing"
            record["pending_save_seq"] = None
            record["last_committed_seq"] = 1
        OfficeStore().update(CHAT, complete)
        expected = _copy_record(_record(first["session_id"]))
        release.set()
        saver.join(timeout=10)
        assert saver.response.status_code == 202
        assert _record(first["session_id"]) == expected
        assert seen == [{"save_seq": 1, "intent": "publish"}]
        assert infos == []
    finally:
        release.set()
        box.__exit__(None, None, None)
    assert recording.hits == 0


def test_delayed_stale_save_does_not_touch_newer_real_save(office_world, monkeypatch):
    http, data, recording, _docker, _broker = office_world
    from office.store import OfficeStore
    _file_id, first = _created(office_world, "editing")
    origin, seen, infos, saver, release, box = _hold_save(
        http, monkeypatch, first, forcesave_code=4,
    )
    try:
        def complete(state):
            record = state["sessions"][first["session_id"]]
            record["state"] = "editing"
            record["pending_save_seq"] = None
            record["last_committed_seq"] = 1
        OfficeStore().update(CHAT, complete)
        second_entered, second_release = threading.Event(), threading.Event()
        with _command_box(
            monkeypatch, first["document_key"], entered=second_entered, release=second_release,
        ) as (_second_origin, second_seen, second_infos):
            newer = threading.Thread(
                target=lambda: setattr(newer, "response", _save(http, first["session_id"], "persist"))
            )
            newer.start()
            assert second_entered.wait(5)
            expected = _copy_record(_record(first["session_id"]))
            assert expected["pending_save_seq"] == 2
            assert expected["save_intents"] == {"1": "publish", "2": "persist"}
            release.set()
            saver.join(timeout=10)
            assert saver.response.status_code == 202
            assert _record(first["session_id"]) == expected
            second_release.set()
            newer.join(timeout=10)
            assert newer.response.status_code == 202
            assert _record(first["session_id"])["pending_save_seq"] == 2
            assert second_seen == [{"save_seq": 2, "intent": "persist"}]
            assert second_infos == []
            assert seen == [{"save_seq": 1, "intent": "publish"}]
            assert infos == []
    finally:
        release.set()
        box.__exit__(None, None, None)
    assert recording.hits == 0


@pytest.mark.parametrize("state,reason", (
    ("closed", None),
    ("error", "publish_timeout"),
    ("orphaned", "restore_epoch_changed"),
    ("orphaned", "editor_state_lost"),
))
def test_delayed_save_does_not_touch_terminal_sessions(office_world, monkeypatch, state, reason):
    http, data, recording, _docker, _broker = office_world
    from office.store import OfficeStore
    _file_id, first = _created(office_world, "editing")
    origin, seen, infos, saver, release, box = _hold_save(
        http, monkeypatch, first, forcesave_code=4,
    )
    try:
        def finish(working):
            record = working["sessions"][first["session_id"]]
            record["state"] = state
            record["reason"] = reason
            record["pending_save_seq"] = None
        OfficeStore().update(CHAT, finish)
        expected = _copy_record(_record(first["session_id"]))
        release.set()
        saver.join(timeout=10)
        assert saver.response.status_code == 202
        assert _record(first["session_id"]) == expected
        assert seen == [{"save_seq": 1, "intent": "publish"}]
        assert infos == []
    finally:
        release.set()
        box.__exit__(None, None, None)
    assert recording.hits == 0



def test_extra_save_keys_are_accepted_when_intent_is_valid(office_world, monkeypatch):
    http, data, recording, _docker, _broker = office_world
    _file_id, first = _created(office_world, "editing")
    with _forcesave(monkeypatch, first["document_key"]) as (origin, seen):
        response = _save(
            http, first["session_id"], content=b'{"intent":"publish","note":"ignored"}'
        )
    assert response.status_code == 202
    assert seen == [{"save_seq": 1, "intent": "publish"}]
    assert len(origin.requests) == 1
    assert recording.hits == 0
