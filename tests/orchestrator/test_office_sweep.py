# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Idle-poll Office recovery against authenticated HTTP and durable chat state."""
from __future__ import annotations

import asyncio
import errno
import json
import os
import socket
import subprocess
import sys
import threading
from contextlib import contextmanager

import pytest

from tests.orchestrator._office_store import _stop_child, _wait_marker
from tests.orchestrator.test_office_commands import _command_origin
from tests.orchestrator.test_office_save_close import _child_env, _close, _command_box, _save
from tests.orchestrator.test_office_session_lifecycle import _asgi, _change, _created, _status
from tests.orchestrator.test_office_sessions import (
    JWT_SECRET, SERVER_DIR, _assert_refusal, _create, _oracle_verify,
    _snapshot, _state, _versions, office_world,
)
from tests.orchestrator.test_outputs_endpoint import CHAT, CHAT_B, INTERNAL, _auth

NOW = 1_700_000_000.25
ELIGIBLE = ("opening", "editing", "saving", "closing")
EXCLUDED = ("conflict", "closed", "error", "orphaned")


def _poll(monkeypatch, *, sandbox_error=False):
    import app

    async def tick():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        calls = []

        def reap():
            calls.append("sandbox")
            loop.call_soon_threadsafe(stop.set)
            if sandbox_error:
                raise RuntimeError("sandbox tick failure")

        with monkeypatch.context() as patch:
            patch.setattr(app, "startup_idle_sweep", lambda: None)
            patch.setattr(app, "validate_idle_configuration", lambda: (600, 0.001))
            patch.setattr(app, "reap_known_sandboxes", reap)
            await asyncio.wait_for(app._idle_reaper(stop), 15)
        assert calls == ["sandbox"]

    asyncio.run(tick())


@contextmanager
def _info_origin(monkeypatch, expected_keys, *, code=0, status=200, entered=None, release=None):
    errors, seen = [], []

    def respond(handler, body, _parent):
        try:
            assert handler.path == "/command"
            envelope = json.loads(body)
            assert set(envelope) == {"token"}
            command = _oracle_verify(envelope["token"], JWT_SECRET)
            assert command["c"] == "info"
            assert set(command) == {"c", "key"}
            assert command["key"] in expected_keys
            seen.append(command)
            if entered is not None:
                entered.set()
            if release is not None:
                assert release.wait(10), "info response barrier was not released"
            return status, {"Content-Type": "application/json"}, json.dumps({"error": code}).encode()
        except BaseException as exc:
            errors.append(exc)
            return 500, {}, b""

    with _command_origin(respond) as origin:
        with monkeypatch.context() as patch:
            patch.setenv("OCU_OFFICE_DOCSERVER_URL", origin.url)
            try:
                yield origin, seen
            finally:
                if release is not None:
                    release.set()
    if errors:
        raise errors[0]


def _aged(world, state, *, chat=CHAT, name="brief.docx", **fields):
    file_id, created = _created(world, state, chat=chat, name=name)
    _change(created["session_id"], chat=chat, **{
        "last_activity_at": NOW - 601, "saving_started_at": NOW - 31, **fields,
    })
    return file_id, created


def _assert_transition(before, after, session_id, state, reason):
    assert after == {**before, "sessions": {
        **before["sessions"], session_id: {
            **before["sessions"][session_id], "state": state, "reason": reason,
        },
    }}


@pytest.mark.parametrize("state", ELIGIBLE)
@pytest.mark.parametrize("code,status", ((0, 200), (1, 200), (0, 503), (True, 200)))
def test_poll_sweeps_office_only_sessions_and_preserves_every_other_record(
    office_world, monkeypatch, state, code, status,
):
    from office.store import OfficeStore
    http, data, recording, _docker, _broker = office_world
    monkeypatch.setattr("time.time", lambda: NOW)
    file_id, created = _aged(office_world, state)
    session_id = created["session_id"]
    store = OfficeStore()
    store.update(CHAT, lambda working: working["journal"].__setitem__("unrelated", {"keep": True}))
    _change(session_id, save_seq=4, pending_save_seq=3, pending_close_seq=4,
            save_intents={"1": "persist", "3": "publish"},
            last_committed_seq=2, last_published_seq=1)
    before = store.read(CHAT)
    bytes_before = _snapshot(data)
    identity = _state(data).stat().st_ino
    assert not (data / CHAT / ".meta.json").exists()
    assert not (data / CHAT / ".idle.json").exists()
    with _info_origin(monkeypatch, {created["document_key"]}, code=code, status=status) as (_origin, seen):
        _poll(monkeypatch)
    assert seen == [{"c": "info", "key": created["document_key"]}]
    after = store.read(CHAT)
    if status == 200 and type(code) is int and code == 1:
        _assert_transition(before, after, session_id, "orphaned", "editor_state_lost")
    elif status == 200 and type(code) is int and code == 0 and state == "saving":
        _assert_transition(before, after, session_id, "editing", "save_timeout")
    else:
        assert after == before
        assert _state(data).stat().st_ino == identity
        assert _snapshot(data) == bytes_before
    state_path = str(_state(data).relative_to(data))
    assert {key: value for key, value in _snapshot(data).items() if key != state_path} == {
        key: value for key, value in bytes_before.items() if key != state_path
    }
    assert after["documents"][file_id]["versions"] == before["documents"][file_id]["versions"]
    assert recording.hits == 0


@pytest.mark.parametrize("state", EXCLUDED)
def test_poll_never_sweeps_conflict_or_final_records(office_world, monkeypatch, state):
    from office.store import OfficeStore
    _http, data, recording, _docker, _broker = office_world
    monkeypatch.setattr("time.time", lambda: NOW)
    _file, created = _aged(office_world, state)
    before = OfficeStore().read(CHAT), _snapshot(data), _state(data).stat().st_ino
    with _info_origin(monkeypatch, {created["document_key"]}, code=1) as (_origin, seen):
        _poll(monkeypatch)
    assert seen == []
    assert (OfficeStore().read(CHAT), _snapshot(data), _state(data).stat().st_ino) == before
    assert recording.hits == 0


@pytest.mark.parametrize("state,activity,start,expected", (
    ("editing", NOW - 600, NOW - 100, None),
    ("editing", NOW - 600.001, NOW, "orphaned"),
    ("editing", NOW + 1, NOW, None),
    ("editing", 0, NOW, "orphaned"),
    ("saving", NOW, NOW - 30, None),
    ("saving", NOW, NOW - 30.001, "orphaned"),
    ("saving", NOW - 600, NOW - 30, None),
    ("saving", NOW + 1, NOW + 1, None),
))
def test_poll_uses_strict_fractional_thresholds_and_never_expires_future_clocks(
    office_world, monkeypatch, state, activity, start, expected,
):
    from office.store import OfficeStore
    _http, data, _recording, _docker, _broker = office_world
    monkeypatch.setattr("time.time", lambda: NOW)
    _file, created = _aged(office_world, state, last_activity_at=activity, saving_started_at=start)
    before = OfficeStore().read(CHAT)
    identity = _state(data).stat().st_ino
    with _info_origin(monkeypatch, {created["document_key"]}, code=1) as (_origin, seen):
        _poll(monkeypatch)
    after = OfficeStore().read(CHAT)
    if expected:
        assert seen == [{"c": "info", "key": created["document_key"]}]
        _assert_transition(before, after, created["session_id"], expected, "editor_state_lost")
    else:
        assert seen == []
        assert after == before
        assert _state(data).stat().st_ino == identity


@pytest.mark.parametrize("missing", (
    ("last_activity_at",), ("saving_started_at",), ("last_activity_at", "saving_started_at"),
))
def test_absent_clocks_have_unknown_zero_age_without_rewrite(office_world, monkeypatch, missing):
    from office.store import OfficeStore
    _http, data, _recording, _docker, _broker = office_world
    monkeypatch.setattr("time.time", lambda: NOW)
    _file, created = _created(office_world, "saving")
    _change(created["session_id"], last_activity_at=NOW, saving_started_at=NOW)
    def strip(working):
        for field in missing:
            working["sessions"][created["session_id"]].pop(field)
    OfficeStore().update(CHAT, strip)
    before = _snapshot(data), _state(data).stat().st_ino
    with _info_origin(monkeypatch, {created["document_key"]}, code=1) as (_origin, seen):
        _poll(monkeypatch)
    assert seen == []
    assert (_snapshot(data), _state(data).stat().st_ino) == before


@pytest.mark.parametrize("field", ("last_activity_at", "saving_started_at"))
@pytest.mark.parametrize("bad", (None, True, False, -1, -0.1, "0", [], {}, 1e309))
def test_malformed_present_clocks_fail_requests_without_mutation(
    office_world, monkeypatch, field, bad,
):
    http, data, _recording, _docker, _broker = office_world
    monkeypatch.setattr("time.time", lambda: NOW)
    file_id, created = _created(office_world, "editing")
    persisted = json.loads(_state(data).read_bytes())
    persisted["sessions"][created["session_id"]][field] = bad
    _state(data).write_text(json.dumps(persisted))
    before = _snapshot(data)
    for request in (
        lambda: _status(http, created["session_id"]),
        lambda: _create(http, file_id),
        lambda: _save(http, created["session_id"]),
        lambda: _close(http, created["session_id"]),
    ):
        _assert_refusal(request(), 500, "state_corrupt")
        assert _snapshot(data) == before


def test_creation_and_successful_requests_persist_independent_fractional_clocks(
    office_world, monkeypatch,
):
    from office.store import OfficeStore
    http, _data, _recording, _docker, _broker = office_world
    clock = {"now": NOW}
    monkeypatch.setattr("time.time", lambda: clock["now"])
    file_id, created = _created(office_world, "editing")
    session_id = created["session_id"]
    store = OfficeStore()
    assert store.read(CHAT)["sessions"][session_id]["last_activity_at"] == NOW
    assert "saving_started_at" not in store.read(CHAT)["sessions"][session_id]
    with _command_box(monkeypatch, created["document_key"]) as (_origin, seen, infos):
        clock["now"] = NOW + 1.5
        assert _save(http, session_id, "persist").status_code == 202
        allocated = store.read(CHAT)["sessions"][session_id]
        assert allocated["last_activity_at"] == allocated["saving_started_at"] == NOW + 1.5
        clock["now"] = NOW + 2.5
        assert _status(http, session_id).status_code == 200
        assert store.read(CHAT)["sessions"][session_id]["last_activity_at"] == NOW + 2.5
        clock["now"] = NOW + 3.5
        assert _create(http, file_id).status_code == 200
        joined = store.read(CHAT)["sessions"][session_id]
        assert joined["last_activity_at"] == NOW + 3.5
        assert joined["saving_started_at"] == NOW + 1.5
        clock["now"] = NOW + 4.5
        assert _close(http, session_id).status_code == 202
        closed = store.read(CHAT)["sessions"][session_id]
        assert closed["last_activity_at"] == NOW + 4.5
        assert closed["saving_started_at"] == NOW + 1.5
        clock["now"] = NOW + 5.5
        assert _close(http, session_id).status_code == 202
        repeated = store.read(CHAT)["sessions"][session_id]
        assert repeated == {**closed, "last_activity_at": NOW + 5.5}
    assert seen == [{"save_seq": 1, "intent": "persist"}]
    assert infos == [{"c": "info", "key": created["document_key"]}] * 2


@pytest.mark.parametrize("request_kind", ("join", "status", "close"))
def test_recent_successful_requests_prevent_idle_expiry(office_world, monkeypatch, request_kind):
    from office.store import OfficeStore
    http, data, _recording, _docker, _broker = office_world
    monkeypatch.setattr("time.time", lambda: NOW - 601)
    file_id, created = _created(office_world, "editing")
    monkeypatch.setattr("time.time", lambda: NOW)
    with _info_origin(monkeypatch, {created["document_key"]}, code=0) as (_origin, seen):
        response = {
            "join": lambda: _create(http, file_id),
            "status": lambda: _status(http, created["session_id"]),
            "close": lambda: _close(http, created["session_id"]),
        }[request_kind]()
        assert response.status_code == (202 if request_kind == "close" else 200)
        before = OfficeStore().read(CHAT)
        seen.clear()
        identity = _state(data).stat().st_ino
        _poll(monkeypatch)
    assert seen == []
    assert OfficeStore().read(CHAT) == before
    assert _state(data).stat().st_ino == identity


def test_status_and_join_do_not_postpone_save_timeout_and_next_save_retains_prior_intent(
    office_world, monkeypatch,
):
    from office.store import OfficeStore
    http, data, _recording, _docker, _broker = office_world
    monkeypatch.setattr("time.time", lambda: NOW - 31)
    file_id, created = _created(office_world, "editing")
    store = OfficeStore()
    with _command_box(monkeypatch, created["document_key"]) as (_origin, seen, infos):
        saved = _save(http, created["session_id"], "persist")
        assert saved.status_code == 202
        assert saved.json()["save_seq"] == 1
        start = store.read(CHAT)["sessions"][created["session_id"]]["saving_started_at"]
        monkeypatch.setattr("time.time", lambda: NOW)
        assert _status(http, created["session_id"]).status_code == 200
        assert _create(http, file_id).status_code == 200
        before = store.read(CHAT)
        bytes_before = _snapshot(_versions(data))
        assert before["sessions"][created["session_id"]]["last_activity_at"] == NOW
        assert start == NOW - 31
        _poll(monkeypatch)
        after = store.read(CHAT)
        _assert_transition(before, after, created["session_id"], "editing", "save_timeout")
        assert after["sessions"][created["session_id"]]["pending_save_seq"] == 1
        next_save = _save(http, created["session_id"], "publish")
        assert next_save.status_code == 202
        assert next_save.json() == {
            "session_id": created["session_id"], "save_seq": 2, "intent": "publish",
        }
        final = store.read(CHAT)
    record = final["sessions"][created["session_id"]]
    assert record["save_intents"] == {"1": "persist", "2": "publish"}
    assert record["pending_save_seq"] == 2
    assert record["save_seq"] == 2
    assert record["last_committed_seq"] == record["last_published_seq"] == 0
    assert record["saving_started_at"] == record["last_activity_at"] == NOW
    assert final["documents"] == before["documents"]
    assert final["receipts"] == before["receipts"]
    assert final["journal"] == before["journal"]
    assert _snapshot(_versions(data)) == bytes_before
    assert seen == [{"save_seq": 1, "intent": "persist"}, {"save_seq": 2, "intent": "publish"}]
    assert infos == [{"c": "info", "key": created["document_key"]}] * 2


def test_denied_unknown_foreign_and_refused_requests_cannot_refresh_activity(office_world, monkeypatch):
    from office.store import OfficeStore
    http, data, origin, _docker, _broker = office_world
    monkeypatch.setattr("time.time", lambda: NOW)
    _file, created = _aged(office_world, "opening")
    _other_file, foreign = _created(office_world, chat=CHAT_B)
    before = OfficeStore().read(CHAT), OfficeStore().read(CHAT_B), _snapshot(data)
    assert http.get(f"/api/office/{CHAT}/sessions/{created['session_id']}",
                    headers=_auth("wrong-token")).status_code == 401
    _assert_refusal(_save(http, created["session_id"]), 409, "session_not_editing")
    _assert_refusal(_status(http, "unknown"), 404, "unknown_session")
    _assert_refusal(_status(http, foreign["session_id"]), 404, "unknown_session")
    _assert_refusal(_close(http, foreign["session_id"]), 404, "unknown_session")
    assert (OfficeStore().read(CHAT), OfficeStore().read(CHAT_B), _snapshot(data)) == before
    assert origin.hits == 0


def test_disabled_poll_performs_no_office_discovery_or_network(office_world, monkeypatch):
    from office.store import OfficeStore
    import office.sweep as sweep
    _http, data, recording, _docker, _broker = office_world
    _file, created = _aged(office_world, "opening")
    before = OfficeStore().read(CHAT), _snapshot(data)
    with _info_origin(monkeypatch, {created["document_key"]}, code=1) as (_origin, seen):
        with monkeypatch.context() as trap:
            trap.setenv("OCU_OFFICE_DOCSERVER_URL", " \t ")
            trap.setattr(sweep.os, "scandir", lambda *_a, **_k: pytest.fail("disabled discovery"))
            trap.setattr(sweep.os, "open", lambda *_a, **_k: pytest.fail("disabled Office IO"))
            _poll(trap)
    assert seen == []
    assert (OfficeStore().read(CHAT), _snapshot(data)) == before
    assert recording.hits == 0


@pytest.mark.parametrize("linked", ("chat", "ocu", "office", "state", "lock"))
def test_sweep_skips_linked_control_paths_without_opening_external_targets(
    office_world, monkeypatch, linked,
):
    from office.store import OfficeStore
    import office.sweep as sweep
    from tests.orchestrator.test_office_workspace import _forbid_inode_open
    _http, data, _recording, _docker, _broker = office_world
    monkeypatch.setattr("time.time", lambda: NOW)
    _file, good = _aged(office_world, "opening")
    unsafe = data / "unsafe-chat"
    office = unsafe / ".ocu" / "office"
    office.mkdir(parents=True)
    outside = data.parent / "external-control"
    outside.mkdir()
    external_state = outside / "state.json"
    external_state.write_bytes(_state(data).read_bytes())
    if linked == "chat":
        (office).rmdir()
        (unsafe / ".ocu").rmdir()
        unsafe.rmdir()
        unsafe.symlink_to(outside, target_is_directory=True)
    elif linked == "ocu":
        office.rmdir()
        (unsafe / ".ocu").rmdir()
        (unsafe / ".ocu").symlink_to(outside, target_is_directory=True)
    elif linked == "office":
        office.rmdir()
        office.symlink_to(outside, target_is_directory=True)
    elif linked == "state":
        (office / "state.json").symlink_to(external_state)
    else:
        (office / "state.json").write_bytes(_state(data).read_bytes())
        (unsafe / ".lifecycle.lock").symlink_to(external_state)
    outside_before = _snapshot(outside)
    with _info_origin(monkeypatch, {good["document_key"]}, code=1) as (_origin, seen):
        with monkeypatch.context() as trap:
            _forbid_inode_open(sweep, trap, outside, external_state)
            _poll(trap)
    assert seen == [{"c": "info", "key": good["document_key"]}]
    assert OfficeStore().read(CHAT)["sessions"][good["session_id"]]["state"] == "orphaned"
    assert _snapshot(outside) == outside_before
    if linked == "lock":
        assert (unsafe / ".lifecycle.lock").is_symlink()
    else:
        assert not (unsafe / ".lifecycle.lock").exists()


def test_discovery_does_not_create_missing_office_or_noncanonical_chat_state(
    office_world, monkeypatch,
):
    from office.store import OfficeStore
    _http, data, _recording, _docker, _broker = office_world
    monkeypatch.setattr("time.time", lambda: NOW)
    _file, good = _aged(office_world, "opening")
    empty = data / "empty-chat"
    empty.mkdir()
    incomplete = data / "incomplete-chat" / ".ocu" / "office"
    incomplete.mkdir(parents=True)
    noncanonical = data / "UPPER-CHAT" / ".ocu" / "office"
    noncanonical.mkdir(parents=True)
    (noncanonical / "state.json").write_bytes(_state(data).read_bytes())
    (data / "not-a-chat").write_text("ordinary file")
    entry_names = {child.name for child in data.iterdir()}
    noncanonical_before = _snapshot(noncanonical.parents[1])
    with _info_origin(monkeypatch, {good["document_key"]}, code=1) as (_origin, seen):
        _poll(monkeypatch)
    assert seen == [{"c": "info", "key": good["document_key"]}]
    assert OfficeStore().read(CHAT)["sessions"][good["session_id"]]["state"] == "orphaned"
    assert list(empty.iterdir()) == []
    assert list(incomplete.iterdir()) == []
    assert not (incomplete.parents[1] / ".lifecycle.lock").exists()
    assert not (noncanonical.parents[1] / ".lifecycle.lock").exists()
    assert {child.name for child in data.iterdir()} == entry_names
    assert _snapshot(noncanonical.parents[1]) == noncanonical_before
    assert not (data / "missing-chat").exists()


@pytest.mark.parametrize("damage", ("json", "timestamp", "unreadable"))
def test_corrupt_chat_and_sandbox_failure_do_not_suppress_later_office_chat(
    office_world, monkeypatch, capsys, damage,
):
    from office.store import OfficeStore
    import office.store as store_module
    _http, data, _recording, _docker, _broker = office_world
    monkeypatch.setattr("time.time", lambda: NOW)
    _file, bad = _aged(office_world, "opening")
    _other, good = _aged(office_world, "opening", chat=CHAT_B)
    if damage == "json":
        _state(data).write_bytes(b"{malformed")
    elif damage == "timestamp":
        _change(bad["session_id"], last_activity_at="secret-invalid-clock")
    identity = _state(data).stat().st_ino
    encoded = _state(data).read_bytes()
    original_open = store_module.os.open
    def deny_state(name, flags, *args, **kwargs):
        if name == "state.json":
            directory = os.fstat(kwargs["dir_fd"])
            own = _state(data).parent.stat()
            if (directory.st_dev, directory.st_ino) == (own.st_dev, own.st_ino):
                raise PermissionError("document-key-and-secret-canary")
        return original_open(name, flags, *args, **kwargs)
    with _info_origin(monkeypatch, {good["document_key"]}, code=1) as (_origin, seen):
        with monkeypatch.context() as patch:
            if damage == "unreadable":
                patch.setattr(store_module.os, "open", deny_state)
            _poll(patch, sandbox_error=True)
    assert seen == [{"c": "info", "key": good["document_key"]}]
    assert OfficeStore().read(CHAT_B)["sessions"][good["session_id"]]["state"] == "orphaned"
    assert _state(data).read_bytes() == encoded
    assert _state(data).stat().st_ino == identity
    output = capsys.readouterr().out
    assert "[OFFICE] session sweep chat failed" in output
    for hidden in (bad["document_key"], good["document_key"], JWT_SECRET, INTERNAL,
                   "document-key-and-secret-canary", "secret-invalid-clock"):
        assert hidden not in output


def test_office_tick_failure_does_not_suppress_next_sandbox_tick(office_world, monkeypatch, capsys):
    import app
    import office.sweep as sweep
    _http, data, _recording, _docker, _broker = office_world
    _file, _created_session = _aged(office_world, "opening")
    calls = []
    async def scenario():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        def reap():
            calls.append("sandbox")
            if len(calls) == 2:
                loop.call_soon_threadsafe(stop.set)
        with monkeypatch.context() as patch:
            patch.setattr(app, "startup_idle_sweep", lambda: None)
            patch.setattr(app, "reap_known_sandboxes", reap)
            patch.setattr(app, "validate_idle_configuration", lambda: (600, 0.001))
            patch.setattr(sweep.os, "scandir", lambda *_a, **_k: (_ for _ in ()).throw(
                PermissionError("secret-discovery-error")))
            await asyncio.wait_for(app._idle_reaper(stop), 10)
    asyncio.run(scenario())
    assert calls == ["sandbox", "sandbox"]
    output = capsys.readouterr().out
    assert output.count("[OFFICE] session sweep tick failed") == 2
    assert "secret-discovery-error" not in output
    assert _state(data).exists()


@pytest.mark.parametrize("failure", ("write", "replace", "durability"))
def test_sweep_write_failures_keep_complete_state_and_allow_later_chats(
    office_world, monkeypatch, capsys, failure,
):
    from office.store import OfficeStore
    import office.store as store_module
    _http, data, _recording, _docker, _broker = office_world
    monkeypatch.setattr("time.time", lambda: NOW)
    _file, first = _aged(office_world, "saving")
    _other, second = _aged(office_world, "opening", chat=CHAT_B)
    store = OfficeStore()
    before = store.read(CHAT)
    encoded = _state(data).read_bytes()
    directory_id = _state(data).parent.stat().st_ino
    write, replace, fsync = store_module.os.write, store_module.os.replace, store_module.os.fsync
    replaced = {"fd": None}
    def fail_write(fd, body):
        if failure == "write" and first["session_id"].encode() in body:
            raise OSError(errno.ENOSPC, "secret-write-canary")
        return write(fd, body)
    def fail_replace(src, dst, *args, **kwargs):
        own = dst == "state.json" and os.fstat(kwargs["dst_dir_fd"]).st_ino == directory_id
        if own and failure == "replace":
            raise OSError(errno.EIO, "secret-replace-canary")
        result = replace(src, dst, *args, **kwargs)
        if own:
            replaced["fd"] = kwargs["dst_dir_fd"]
        return result
    def fail_fsync(fd):
        if failure == "durability" and fd == replaced["fd"]:
            replaced["fd"] = None
            raise OSError(errno.EIO, "secret-durability-canary")
        return fsync(fd)
    with _info_origin(monkeypatch, {first["document_key"], second["document_key"]}, code=1) as (_origin, seen):
        with monkeypatch.context() as patch:
            patch.setattr(store_module.os, "write", fail_write)
            patch.setattr(store_module.os, "replace", fail_replace)
            patch.setattr(store_module.os, "fsync", fail_fsync)
            _poll(patch)
    assert {item["key"] for item in seen} == {first["document_key"], second["document_key"]}
    after = store.read(CHAT)
    if failure == "durability":
        _assert_transition(before, after, first["session_id"], "orphaned", "editor_state_lost")
    else:
        assert after == before
        assert _state(data).read_bytes() == encoded
    assert store.read(CHAT_B)["sessions"][second["session_id"]]["state"] == "orphaned"
    assert not list(_state(data).parent.glob(".state.*.tmp"))
    output = capsys.readouterr().out
    assert ("state durability failed" if failure == "durability" else "sweep chat failed") in output
    for hidden in (JWT_SECRET, first["document_key"], second["document_key"],
                   "secret-write-canary", "secret-replace-canary", "secret-durability-canary"):
        assert hidden not in output


def test_two_process_sweeps_hold_flock_through_lookup_and_retain_real_store_commit(
    office_world, monkeypatch,
):
    from office.store import OfficeStore
    from tests.orchestrator.test_office_sessions import _sha
    _http, data, _recording, _docker, _broker = office_world
    monkeypatch.setattr("time.time", lambda: NOW)
    _file, created = _aged(office_world, "saving", save_seq=3, pending_save_seq=3,
                           save_intents={"3": "persist"}, last_committed_seq=2,
                           last_activity_at=NOW)
    before = OfficeStore().read(CHAT)["sessions"][created["session_id"]]
    entered, release = threading.Event(), threading.Event()
    contended = data.parent / "sweep-worker-contended"
    source = r'''
import fcntl, json, os
from pathlib import Path
from office.store import OfficeStore
from office.sweep import sweep_office_sessions
chat = os.environ["OCU_CHAT"]
store = OfficeStore()
if os.environ["ROLE"] == "first":
    store.store_version(chat, "unrelated-file", b"unrelated persisted content",
        source="workspace", parent=None, published=True, min_free_bytes=0)
else:
    original = fcntl.flock
    def contend(fd, operation):
        if operation == fcntl.LOCK_EX:
            try:
                original(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                Path(os.environ["CONTENDED"]).write_text("blocked")
                fcntl.flock = original
                return original(fd, fcntl.LOCK_EX)
            original(fd, fcntl.LOCK_UN)
            raise AssertionError("sweep released canonical flock during info lookup")
        return original(fd, operation)
    fcntl.flock = contend
sweep_office_sessions(float(os.environ["NOW"]))
print(json.dumps(store.read(chat)))
'''
    first = second = None
    with _info_origin(monkeypatch, {created["document_key"]}, code=0,
                      entered=entered, release=release) as (origin, seen):
        try:
            env = _child_env(data, origin.url, NOW=str(NOW), CONTENDED=str(contended))
            first = subprocess.Popen([sys.executable, "-c", source], cwd=SERVER_DIR,
                                     env={**env, "ROLE": "first"}, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
            assert entered.wait(10), "first sweep did not enter authenticated info request"
            second = subprocess.Popen([sys.executable, "-c", source], cwd=SERVER_DIR,
                                      env={**env, "ROLE": "second"}, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True)
            _wait_marker(contended, second, "second sweep did not observe real LOCK_NB denial")
            assert first.poll() is None and second.poll() is None
            assert seen == [{"c": "info", "key": created["document_key"]}]
            release.set()
            one_out, one_err = first.communicate(timeout=15)
            two_out, two_err = second.communicate(timeout=15)
            assert first.returncode == 0, (one_out, one_err)
            assert second.returncode == 0, (two_out, two_err)
            one = json.loads(one_out.strip().splitlines()[-1])
            two = json.loads(two_out.strip().splitlines()[-1])
            assert one == two == OfficeStore().read(CHAT)
        finally:
            release.set()
            _stop_child(first)
            _stop_child(second)
    assert len(seen) == 1
    record = one["sessions"][created["session_id"]]
    assert record == {**before, "state": "editing", "reason": "save_timeout"}
    version = one["documents"]["unrelated-file"]["versions"][0]
    assert {field: value for field, value in version.items() if field != "created_at"} == {
        "number": 1, "parent": None, "sha256": _sha(b"unrelated persisted content"),
        "size": 27, "source": "workspace", "published": True,
    }
    assert (_versions(data) / _sha(b"unrelated persisted content")).read_bytes() == b"unrelated persisted content"
    assert json.loads(_state(data).read_bytes()) == one


def test_delayed_poll_lookup_leaves_health_responsive(office_world, monkeypatch):
    import app
    http, _data, _recording, _docker, _broker = office_world
    monkeypatch.setattr("time.time", lambda: NOW)
    _file, created = _aged(office_world, "opening")
    entered, release = threading.Event(), threading.Event()
    with _info_origin(monkeypatch, {created["document_key"]}, code=1,
                      entered=entered, release=release) as (_origin, _seen):
        async def scenario():
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            with monkeypatch.context() as patch:
                patch.setattr(app, "startup_idle_sweep", lambda: None)
                patch.setattr(app, "validate_idle_configuration", lambda: (600, 0.001))
                patch.setattr(app, "reap_known_sandboxes", lambda: loop.call_soon_threadsafe(stop.set))
                poll = asyncio.create_task(app._idle_reaper(stop))
                try:
                    assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 5), 6)
                    assert await asyncio.wait_for(_asgi(http.app, "GET", "/health"), 1) == (
                        200, {"status": "healthy"},
                    )
                    assert not poll.done()
                finally:
                    release.set()
                    await asyncio.wait_for(poll, 10)
        asyncio.run(scenario())


def test_status_durability_failure_exposes_complete_notice_and_activity_successor(
    office_world, monkeypatch,
):
    from office.store import OfficeStore
    import office.store as store_module
    http, data, _recording, _docker, _broker = office_world
    monkeypatch.setattr("time.time", lambda: NOW - 100)
    _file, created = _created(office_world, "editing")
    before = OfficeStore().read(CHAT)
    monkeypatch.setattr("time.time", lambda: NOW)
    original_replace, original_fsync = store_module.os.replace, store_module.os.fsync
    replaced = {"fd": None}
    def observe(src, dst, *args, **kwargs):
        result = original_replace(src, dst, *args, **kwargs)
        if dst == "state.json":
            replaced["fd"] = kwargs["dst_dir_fd"]
        return result
    def fail_directory(fd):
        if fd == replaced["fd"]:
            replaced["fd"] = None
            raise OSError(errno.EIO, "status durability failure")
        return original_fsync(fd)
    with monkeypatch.context() as patch:
        patch.setattr(store_module.os, "replace", observe)
        patch.setattr(store_module.os, "fsync", fail_directory)
        _assert_refusal(_status(http, created["session_id"]), 500, "state_durability")
    after = OfficeStore().read(CHAT)
    path = data / CHAT / "outputs" / "brief.docx"
    assert after == {**before, "sessions": {
        **before["sessions"], created["session_id"]: {
            **before["sessions"][created["session_id"]],
            "last_activity_at": NOW, "workspace_changed": False,
            "last_checked_size": path.stat().st_size,
            "last_checked_mtime_ns": path.stat().st_mtime_ns,
        },
    }}


@pytest.mark.parametrize("missing", ("last_activity_at", "saving_started_at"))
def test_missing_one_clock_does_not_disable_the_other_expiry_rule(office_world, monkeypatch, missing):
    from office.store import OfficeStore
    _http, _data, _recording, _docker, _broker = office_world
    monkeypatch.setattr("time.time", lambda: NOW)
    _file, created = _aged(office_world, "saving")
    def strip(working):
        working["sessions"][created["session_id"]].pop(missing)
    OfficeStore().update(CHAT, strip)
    before = OfficeStore().read(CHAT)
    with _info_origin(monkeypatch, {created["document_key"]}, code=1) as (_origin, seen):
        _poll(monkeypatch)
    assert seen == [{"c": "info", "key": created["document_key"]}]
    _assert_transition(before, OfficeStore().read(CHAT), created["session_id"], "orphaned", "editor_state_lost")


@pytest.mark.parametrize("state", ("closed", "error", "orphaned"))
def test_final_status_does_not_refresh_activity(office_world, monkeypatch, state):
    from office.store import OfficeStore
    http, data, origin, _docker, _broker = office_world
    monkeypatch.setattr("time.time", lambda: NOW - 100)
    _file, created = _created(office_world, state)
    before = OfficeStore().read(CHAT), _snapshot(data)
    monkeypatch.setattr("time.time", lambda: NOW)
    assert _status(http, created["session_id"]).status_code == 200
    assert (OfficeStore().read(CHAT), _snapshot(data)) == before
    assert origin.hits == 0


def test_idle_known_saving_session_does_not_time_out_before_its_save_deadline(
    office_world, monkeypatch,
):
    from office.store import OfficeStore
    _http, data, _recording, _docker, _broker = office_world
    monkeypatch.setattr("time.time", lambda: NOW)
    _file, created = _aged(office_world, "saving", saving_started_at=NOW)
    before = OfficeStore().read(CHAT), _snapshot(data), _state(data).stat().st_ino
    with _info_origin(monkeypatch, {created["document_key"]}, code=0) as (_origin, seen):
        _poll(monkeypatch)
    assert seen == [{"c": "info", "key": created["document_key"]}]
    assert (OfficeStore().read(CHAT), _snapshot(data), _state(data).stat().st_ino) == before


def test_refused_connection_leaves_overdue_saving_session_and_pending_allocation_unchanged(
    office_world, monkeypatch,
):
    from office.store import OfficeStore
    _http, data, _recording, _docker, _broker = office_world
    monkeypatch.setattr("time.time", lambda: NOW)
    _file, _created_session = _aged(office_world, "saving", save_seq=1,
                                    pending_save_seq=1, save_intents={"1": "persist"})
    before = OfficeStore().read(CHAT), _snapshot(data), _state(data).stat().st_ino
    with socket.socket() as refusing:
        refusing.bind(("127.0.0.1", 0))
        with monkeypatch.context() as patch:
            patch.setenv("OCU_OFFICE_DOCSERVER_URL", f"http://127.0.0.1:{refusing.getsockname()[1]}")
            _poll(patch)
    assert (OfficeStore().read(CHAT), _snapshot(data), _state(data).stat().st_ino) == before


def test_missing_data_root_poll_creates_no_chat_or_office_state(office_world, monkeypatch):
    _http, data, recording, docker, _broker = office_world
    missing = data.parent / "absent-data-root"
    monkeypatch.setattr(docker, "BASE_DATA_DIR", missing)
    _poll(monkeypatch)
    assert not missing.exists()
    assert recording.hits == 0
