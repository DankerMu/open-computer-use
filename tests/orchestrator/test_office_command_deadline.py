# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Command deadlines and caller admission through the actual application loop."""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import aiohttp.connector
import aiohttp.resolver
import pytest

from tests.orchestrator.test_office_restore import _restore
from tests.orchestrator.test_office_save_close import _close, _command_box, _save
from tests.orchestrator.test_office_session_lifecycle import _change, _created
from tests.orchestrator.test_office_sessions import (
    _assert_refusal, _create, _snapshot, _state, office_world as office_world,
)
from tests.orchestrator.test_office_sweep import NOW, _poll
from tests.orchestrator.test_outputs_endpoint import CHAT, CHAT_B

ALLOWANCE = 0.15
CLEANUP_TIMEOUT = 5


def _prepare(world, monkeypatch, caller):
    from office.store import OfficeStore

    monkeypatch.setattr(time, "time", lambda: NOW)
    file_id, session = _created(world, "saving" if caller == "poll" else "editing")
    if caller == "poll":
        _change(session["session_id"], save_seq=3, pending_save_seq=3,
                save_intents={"3": "persist"}, saving_started_at=NOW - 31)
    unrelated_file, unrelated = _created(world, "closed", name="unrelated.docx")
    _created(world, "closed", chat=CHAT_B, name="other-chat.docx")
    store = OfficeStore()
    selected = store.read(CHAT)["documents"][unrelated_file]["versions"][0]
    _change(unrelated["session_id"], save_seq=1, last_committed_seq=1,
            save_intents={"1": "persist"})

    def receipt(working):
        working["receipts"][unrelated["session_id"]] = {"1": {
            "status": 6, "version": 1, "sha256": selected["sha256"], "answer": {"error": 0},
        }}

    store.update(CHAT, receipt)
    return file_id, session


def _invoke(http, monkeypatch, caller, file_id, session):
    if caller == "join":
        return _create(http, file_id)
    if caller == "close":
        return _close(http, session["session_id"])
    if caller == "save":
        return _save(http, session["session_id"], "persist")
    if caller == "restore":
        return _restore(http, file_id)
    assert caller == "poll"
    return _poll(monkeypatch, loop_runner=http.portal.call)


def _assert_result(caller, response, session, before, data, *, reachable):
    session_id = session["session_id"]
    original = before["sessions"][session_id]
    expected = dict(original)
    if caller == "join":
        if reachable:
            assert response.status_code == 200
            assert response.json()["joined"] is True
            assert {key: response.json()[key] for key in ("session_id", "document_key", "state")} == {
                "session_id": session_id, "document_key": session["document_key"], "state": "editing",
            }
        else:
            _assert_refusal(response, 502, "documentserver_unavailable")
    elif caller == "close":
        assert response.status_code == 202
        assert response.json() == {"session_id": session_id, "save_seq": 1, "state": "closing"}
        expected.update(state="closing", save_seq=1, pending_close_seq=1, last_activity_at=NOW)
    elif caller == "save":
        if reachable:
            assert response.status_code == 202
            assert response.json() == {"session_id": session_id, "save_seq": 1, "intent": "persist"}
        else:
            _assert_refusal(response, 502, "documentserver_unavailable", save_seq=1)
        expected.update(state="saving" if reachable else "editing", save_seq=1,
                        pending_save_seq=1 if reachable else None, save_intents={"1": "persist"},
                        last_activity_at=NOW, saving_started_at=NOW)
    elif caller == "restore":
        _assert_refusal(response, 409, "session_open")
    else:
        assert caller == "poll" and response is None
        if reachable:
            expected.update(state="editing", reason="save_timeout")
    after = json.loads(_state(data).read_bytes())
    assert after == {**before, "sessions": {**before["sessions"], session_id: expected}}


def _assert_other_bytes(before, data, *, unchanged=False):
    after = _snapshot(data)
    if unchanged:
        assert after == before
    else:
        state_path = str(_state(data).relative_to(data))
        assert {key: value for key, value in after.items() if key != state_path} == {
            key: value for key, value in before.items() if key != state_path
        }


@pytest.mark.parametrize("caller", ("join", "close", "save", "poll"))
def test_command_callers_return_and_release_lookup_lock_before_held_dns(
    office_world, monkeypatch, caller,
):
    # office_world reloads these modules; collection-time imports would observe stale objects.
    import docker_manager
    import office.commands as commands

    http, data, recording, _manager, _broker = office_world
    file_id, session = _prepare(office_world, monkeypatch, caller)
    before = json.loads(_state(data).read_bytes())
    before_bytes = _snapshot(data)
    configured_timeout = commands.HTTP_TIMEOUT_SECONDS
    assert configured_timeout == 10
    entered, release, returned, escaped = (threading.Event() for _ in range(4))
    completed, ready, observed, acquired = (threading.Event() for _ in range(4))
    timestamps, outcome, lock_observation = {}, {}, {}
    failures, calls = [], []
    original = socket.getaddrinfo
    host = "held-command-resolution.example"

    def delayed(name, port, *args, **kwargs):
        if str(name) != host:
            return original(name, port, *args, **kwargs)
        calls.append(str(name))
        timestamps["entered"] = time.monotonic()
        entered.set()
        ready.set()
        try:
            if not release.wait(configured_timeout + 2 * CLEANUP_TIMEOUT):
                escaped.set()
                raise TimeoutError("resolver emergency escape invalidates command-deadline evidence")
            return original("127.0.0.1", port, *args, **kwargs)
        except BaseException as exc:
            failures.append(exc)
            raise
        finally:
            timestamps["returned"] = time.monotonic()
            returned.set()

    def request():
        try:
            response = _invoke(http, monkeypatch, caller, file_id, session)
            timestamps["responded"] = time.monotonic()
            outcome.update(response=response, released=release.is_set(), returned=returned.is_set(),
                           escaped=escaped.is_set())
        except BaseException as exc:
            failures.append(exc)
        finally:
            completed.set()
            ready.set()

    def contend():
        try:
            lock = docker_manager.get_chat_lock(CHAT)
            free = lock.acquire(blocking=False)
            lock_observation["free"] = free
            if free:
                lock.release()
            observed.set()
            with docker_manager._combined_lock(CHAT):
                timestamps["acquired"] = time.monotonic()
                lock_observation.update(
                    record=json.loads(_state(data).read_bytes())["sessions"][session["session_id"]],
                    released=release.is_set(), returned=returned.is_set(),
                )
                acquired.set()
        except BaseException as exc:
            failures.append(exc)
            observed.set()

    monkeypatch.setattr(aiohttp.connector, "DefaultResolver", aiohttp.resolver.ThreadedResolver)
    monkeypatch.setattr(socket, "getaddrinfo", delayed)
    with _command_box(monkeypatch, session["document_key"]) as (origin, saves, infos):
        monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", f"http://{host}:{urlsplit(origin.url).port}")
        worker = threading.Thread(target=request, name=f"office-{caller}-held-dns-request")
        contender = threading.Thread(target=contend, name=f"office-{caller}-lock-contender")
        try:
            timestamps["started"] = time.monotonic()
            deadline = timestamps["started"] + configured_timeout + ALLOWANCE
            worker.start()
            assert ready.wait(max(0, deadline - time.monotonic())), "resolver did not enter by caller deadline"
            if failures:
                raise failures[0]
            assert entered.is_set(), "caller completed without entering real threaded DNS"
            contender.start()
            assert observed.wait(max(0, deadline - time.monotonic())), "canonical lock was not observed"
            if failures:
                raise failures[0]
            assert lock_observation["free"] is (caller == "save")
            if caller == "save":
                assert acquired.wait(max(0, deadline - time.monotonic()))
                pending = lock_observation["record"]
                assert pending["state"] == "saving"
                assert pending["pending_save_seq"] == pending["save_seq"] == 1
                assert pending["save_intents"] == {"1": "persist"}
                assert not completed.is_set(), "force-save was not pending at lock acquisition"
            assert completed.wait(max(0, deadline - time.monotonic())), (
                f"{caller} did not complete within the unchanged 10-second deadline plus 150 ms while DNS held"
            )
            assert acquired.wait(max(0, deadline - time.monotonic())), (
                "canonical lookup lock did not become available by caller deadline while DNS held"
            )
            if failures:
                raise failures[0]
            assert timestamps["entered"] <= timestamps["responded"] <= deadline
            assert timestamps["acquired"] <= deadline
            assert not outcome["released"] and not outcome["returned"] and not outcome["escaped"]
            assert not lock_observation["released"] and not lock_observation["returned"]
            assert not release.is_set() and not returned.is_set() and not escaped.is_set()
            assert calls == [host]
            assert origin.requests == [] and saves == [] and infos == []
            _assert_result(caller, outcome["response"], session, before, data, reachable=False)
            _assert_other_bytes(before_bytes, data, unchanged=caller in ("join", "poll"))
        finally:
            release.set()
            if worker.ident is not None:
                worker.join(timeout=CLEANUP_TIMEOUT)
            if contender.ident is not None:
                contender.join(timeout=CLEANUP_TIMEOUT)
            resolver_finished = not entered.is_set() or returned.wait(CLEANUP_TIMEOUT)
            assert not worker.is_alive(), "request did not finish after explicit DNS release"
            assert not contender.is_alive(), "lock contender did not finish after explicit DNS release"
            assert resolver_finished, "resolver did not return after explicit release"
            assert not escaped.is_set(), "resolver emergency escape invalidates command-deadline evidence"
            if failures:
                raise failures[0]
    assert recording.hits == 0


@pytest.mark.parametrize("caller", ("join", "close", "save", "poll", "restore"))
def test_command_callers_keep_normal_admission_with_one_dns_executor_worker(
    office_world, monkeypatch, caller,
):
    import office.commands as commands

    http, data, recording, _manager, _broker = office_world
    file_id, session = _prepare(office_world, monkeypatch, caller)
    before = json.loads(_state(data).read_bytes())
    before_bytes = _snapshot(data)
    assert commands.HTTP_TIMEOUT_SECONDS == 10
    entered, completed = threading.Event(), threading.Event()
    original = socket.getaddrinfo
    calls, failures, outcome = [], [], {}

    def resolve(name, port, *args, **kwargs):
        if str(name) == "localhost":
            calls.append(str(name))
            entered.set()
        return original(name, port, *args, **kwargs)

    limited = ThreadPoolExecutor(max_workers=1, thread_name_prefix="office-command-dns")

    def limit_executor():
        asyncio.get_running_loop().set_default_executor(limited)

    http.portal.call(limit_executor)
    monkeypatch.setattr(aiohttp.connector, "DefaultResolver", aiohttp.resolver.ThreadedResolver)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    with _command_box(monkeypatch, session["document_key"]) as (origin, saves, infos):
        monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", f"http://localhost:{urlsplit(origin.url).port}")

        def request():
            try:
                outcome["response"] = _invoke(http, monkeypatch, caller, file_id, session)
            except BaseException as exc:
                failures.append(exc)
            finally:
                completed.set()

        worker = threading.Thread(target=request, name=f"office-{caller}-one-dns-worker-request")
        try:
            started = time.monotonic()
            worker.start()
            assert completed.wait(max(0, started + commands.HTTP_TIMEOUT_SECONDS + ALLOWANCE - time.monotonic())), (
                f"healthy {caller} starved with one service DNS executor worker"
            )
            if failures:
                raise failures[0]
            assert entered.is_set(), "healthy caller did not enter the real threaded resolver"
            assert calls == ["localhost"]
            assert len(origin.requests) == 1
            assert saves == ([{"save_seq": 1, "intent": "persist"}] if caller == "save" else [])
            assert infos == ([] if caller == "save" else [{"c": "info", "key": session["document_key"]}])
            _assert_result(caller, outcome["response"], session, before, data, reachable=True)
            _assert_other_bytes(before_bytes, data, unchanged=caller in ("join", "restore"))
        finally:
            if worker.ident is not None:
                worker.join(timeout=CLEANUP_TIMEOUT)
            assert not worker.is_alive(), "healthy command request worker did not finish"
    # The TestClient owns this executor and joins it when its persistent portal closes.
    assert recording.hits == 0
