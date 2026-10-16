# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Command deadlines and caller admission through the actual application loop."""
from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from urllib.parse import urlsplit

import aiohttp.connector
import aiohttp.resolver
import anyio.to_thread
import pytest

from tests.orchestrator._office_recorded_callbacks import (
    recorded_status_1_payload, recorded_status_4_payload, synthetic_status_7_payload,
)
from tests.orchestrator.test_office_callback_processing import _post
from tests.orchestrator.test_office_commands import _command_origin
from tests.orchestrator.test_office_restore import _restore
from tests.orchestrator.test_office_save_close import _close, _command_box, _save
from tests.orchestrator.test_office_session_lifecycle import _change, _created, _unpublished
from tests.orchestrator.test_office_sessions import (
    JWT_SECRET, _assert_refusal, _create, _oracle_verify, _outputs, _snapshot, _state,
    office_world as office_world,
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
    with _command_box(monkeypatch, session["document_key"]) as (origin, saves, infos), monkeypatch.context() as command_env:
        command_env.setenv("OCU_OFFICE_DOCSERVER_URL", f"http://{host}:{urlsplit(origin.url).port}")
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
            assert lock_observation["free"] is True
            assert acquired.wait(max(0, deadline - time.monotonic()))
            assert not completed.is_set(), "command was not pending at lock acquisition"
            if caller == "save":
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
    with _command_box(monkeypatch, session["document_key"]) as (origin, saves, infos), monkeypatch.context() as command_env:
        command_env.setenv("OCU_OFFICE_DOCSERVER_URL", f"http://localhost:{urlsplit(origin.url).port}")

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


@contextmanager
def _pending_info(world, monkeypatch, caller, file_id, session, *, code=0,
                  one_callback_worker=False, number=1, observe=None, subsequent_code=None,
                  info_calls=None, subsequent_key=None):
    """Own held dependency I/O and collect even a lock-blocked pre-repair request."""
    import office.commands as commands

    http, _data, _recording, _manager, _broker = world
    assert commands.HTTP_TIMEOUT_SECONDS == 10
    entered, release, completed = (threading.Event() for _ in range(3))
    outcome, failures, contenders = {}, [], []
    infos = []
    # This readiness bound detects progress before the unchanged production deadline.
    progress_timeout = 2
    # RED may need the real command deadline to free its lock after callback admission.
    cleanup_timeout = commands.HTTP_TIMEOUT_SECONDS + CLEANUP_TIMEOUT

    def respond(handler, body, _parent):
        assert handler.path == "/command"
        command = _oracle_verify(json.loads(body)["token"], JWT_SECRET)
        expected_key = session["document_key"] if not infos or subsequent_key is None else subsequent_key
        assert command == {"c": "info", "key": expected_key}
        infos.append(command)
        if info_calls is not None:
            info_calls.append(command)
        if len(infos) == 1:
            entered.set()
            if observe is not None:
                observe()
            assert release.wait(cleanup_timeout), "info responder emergency escape"
        result_code = code if len(infos) == 1 or subsequent_code is None else subsequent_code
        return 200, {"Content-Type": "application/json"}, json.dumps({"error": result_code}).encode()

    def request():
        try:
            outcome["response"] = (
                _restore(http, file_id, number) if caller == "restore"
                else _invoke(http, monkeypatch, caller, file_id, session)
            )
        except BaseException as exc:
            failures.append(exc)
        finally:
            completed.set()

    def overlap(action):
        done, result = threading.Event(), {}

        def run():
            try:
                result["value"] = action()
            except BaseException as exc:
                failures.append(exc)
            finally:
                done.set()

        worker = threading.Thread(target=run, name=f"office-{caller}-lookup-contender")
        contenders.append(worker)
        worker.start()
        assert done.wait(progress_timeout), f"{caller} lookup blocked same-chat authority progress"
        if failures:
            raise failures[0]
        assert not completed.is_set(), "lookup completed before the controlled overlap"
        assert not release.is_set()
        return result["value"]

    def finish():
        release.set()
        assert completed.wait(CLEANUP_TIMEOUT), f"{caller} did not reconcile released info"
        if failures:
            raise failures[0]
        return outcome["response"]

    def limit_callbacks():
        limiter = anyio.to_thread.current_default_thread_limiter()
        prior = limiter.total_tokens
        limiter.total_tokens = 1
        return limiter, prior

    with _command_origin(respond, release=release) as origin, monkeypatch.context() as patch:
        patch.setenv("OCU_OFFICE_DOCSERVER_URL", f"http://localhost:{urlsplit(origin.url).port}")
        limiter = http.portal.call(limit_callbacks) if one_callback_worker else None
        worker = threading.Thread(target=request, name=f"office-{caller}-held-info")
        try:
            worker.start()
            assert entered.wait(progress_timeout), f"{caller} did not issue info"
            yield finish, overlap
        finally:
            # Restore callback capacity first: old callers occupy its only worker.
            if limiter is not None:
                http.portal.call(setattr, limiter[0], "total_tokens", limiter[1])
            release.set()
            worker.join(cleanup_timeout)
            for contender in contenders:
                contender.join(cleanup_timeout)
            assert not worker.is_alive(), "lookup request survived explicit dependency release"
            assert all(not contender.is_alive() for contender in contenders)
            if failures and sys.exception() is None:
                raise failures[0]


@pytest.mark.parametrize("caller,close_outcome", (
    ("join", None), ("close", "terminal"), ("close", "remaining"),
    ("poll", None), ("restore", None),
))
def test_info_induced_callback_precedes_lookup_admission_with_one_callback_and_dns_worker(
    office_world, monkeypatch, caller, close_outcome,
):
    http, data, recording, _manager, _broker = office_world
    file_id, session = _prepare(office_world, monkeypatch, caller)
    before = json.loads(_state(data).read_bytes())
    before_bytes = _snapshot(data)
    callback_done = threading.Event()
    callbacks = []
    limited = ThreadPoolExecutor(max_workers=1, thread_name_prefix="office-info-dns")
    http.portal.call(lambda: asyncio.get_running_loop().set_default_executor(limited))
    monkeypatch.setattr(aiohttp.connector, "DefaultResolver", aiohttp.resolver.ThreadedResolver)

    def induce():
        try:
            callbacks.append(_post(http, session, recorded_status_1_payload(
                document_key=session["document_key"],
            )))
        finally:
            callback_done.set()

    with _pending_info(
        office_world, monkeypatch, caller, file_id, session,
        one_callback_worker=True, observe=induce,
    ) as (finish, _overlap):
        assert callback_done.wait(2), f"{caller} info-induced authenticated callback was blocked"
        assert callbacks[0].status_code == 200
        assert callbacks[0].json() == {"error": 0}
        during = json.loads(_state(data).read_bytes())
        expected = {**before["sessions"][session["session_id"]], "participants": ["user-1"]}
        assert during == {**before, "sessions": {**before["sessions"], session["session_id"]: expected}}
        response = finish()
        _assert_result(caller, response, session, during, data, reachable=True)
        _assert_other_bytes(before_bytes, data)
        if caller == "close":
            # Repeated close reuses the allocation, independent of the info callback.
            duplicate = _close(http, session["session_id"])
            assert duplicate.status_code == 202 and duplicate.json() == response.json()
            if close_outcome == "terminal":
                final = _post(http, session, recorded_status_4_payload(
                    document_key=session["document_key"],
                ))
                assert final.status_code == 200 and final.json() == {"error": 0}
                after = json.loads(_state(data).read_bytes())
                record = after["sessions"][session["session_id"]]
                assert (record["state"], record["save_seq"], record["pending_close_seq"]) == ("closed", 1, None)
                assert (record["last_committed_seq"], record["last_published_seq"]) == (1, 1)
                assert after["receipts"][session["session_id"]] == {"1": {
                    "status": 4, "sha256": None, "version": None, "answer": {"error": 0},
                }}
                assert after["documents"] == before["documents"]
                assert after["journal"] == before["journal"]
            else:
                later = _post(http, session, recorded_status_1_payload(
                    document_key=session["document_key"],
                ))
                assert later.status_code == 200 and later.json() == {"error": 0}
                after = json.loads(_state(data).read_bytes())
                record = after["sessions"][session["session_id"]]
                assert (record["state"], record["save_seq"], record["pending_close_seq"]) == ("editing", 1, None)
                assert record["document_key"] == session["document_key"]
                assert after["receipts"] == before["receipts"]
            assert after["documents"] == before["documents"]
            assert after["journal"] == before["journal"]
            _assert_other_bytes(before_bytes, data)
    # The persistent TestClient portal owns and joins its default DNS executor.
    assert recording.hits == 0


def test_concurrent_join_waits_for_lookup_admission_without_blocking_callbacks(office_world, monkeypatch):
    import fcntl
    import os

    http, data, _recording, _manager, _broker = office_world
    file_id, session = _prepare(office_world, monkeypatch, "join")
    denied, completed = threading.Event(), threading.Event()
    responses, failures = [], []
    real_flock = fcntl.flock
    worker = None

    def join():
        try:
            responses.append(_create(http, file_id))
        except BaseException as exc:
            failures.append(exc)
        finally:
            completed.set()

    with _pending_info(office_world, monkeypatch, "join", file_id, session) as (finish, overlap):
        lock_info = (data / CHAT / ".office-lookup.lock").stat()

        def contend(fd, operation):
            info = os.fstat(fd)
            if operation == fcntl.LOCK_EX and (info.st_dev, info.st_ino) == (
                lock_info.st_dev, lock_info.st_ino,
            ):
                try:
                    real_flock(fd, operation | fcntl.LOCK_NB)
                except BlockingIOError:
                    denied.set()
                    return real_flock(fd, operation)
                raise AssertionError("concurrent join bypassed the first observation")
            return real_flock(fd, operation)

        with monkeypatch.context() as boundary:
            boundary.setattr(fcntl, "flock", contend)
            worker = threading.Thread(target=join, name="office-queued-join")
            try:
                worker.start()
                assert denied.wait(2), failures
                assert not completed.is_set()
                callback = overlap(lambda: _post(http, session, recorded_status_1_payload(
                    document_key=session["document_key"],
                )))
                assert callback.status_code == 200 and callback.json() == {"error": 0}
                frozen = _snapshot(data)
                first = finish()
                assert completed.wait(2), failures
                worker.join(CLEANUP_TIMEOUT)
                assert failures == []
                for response in (first, *responses):
                    assert response.status_code == 200 and response.json()["joined"] is True
                    assert response.json()["session_id"] == session["session_id"]
                    assert response.json()["document_key"] == session["document_key"]
                    assert response.json()["state"] == "editing"
                assert len(responses) == 1
                assert _snapshot(data) == frozen
            finally:
                # Finish releases the owned dependency even when an assertion fails.
                finish()
                worker.join(CLEANUP_TIMEOUT)
                assert not worker.is_alive()


def _replace_canonical_session(session_id, state):
    """Inject replaced authority, not a create command that can bypass the gate."""
    import docker_manager
    from office.store import OfficeStore

    successor_id = "00000000-0000-4000-8000-000000000425"

    def replace(working):
        original = working["sessions"][session_id]
        original["state"] = "closed"
        working["sessions"][successor_id] = {
            **original, "session_id": successor_id, "document_key": "replacement-editor-key",
            "state": state, "reason": None, "save_seq": 0, "pending_save_seq": None,
            "pending_close_seq": None, "last_committed_seq": 0, "last_published_seq": 0,
            "save_intents": {}, "participants": [],
        }

    with docker_manager._combined_lock(CHAT):
        return OfficeStore().update(CHAT, replace)["sessions"][successor_id]


def test_join_discards_unknown_lookup_for_replaced_session_and_key(office_world, monkeypatch):
    http, data, _recording, _manager, _broker = office_world
    file_id, session = _prepare(office_world, monkeypatch, "join")

    infos = []
    with _pending_info(
        office_world, monkeypatch, "join", file_id, session, code=1,
        subsequent_code=0, subsequent_key="replacement-editor-key", info_calls=infos,
    ) as (finish, overlap):
        successor = overlap(lambda: _replace_canonical_session(session["session_id"], "editing"))
        frozen = _snapshot(data)
        response = finish()
        assert response.status_code == 200 and response.json()["joined"] is True
        assert response.json()["session_id"] == successor["session_id"] != session["session_id"]
        assert response.json()["document_key"] == successor["document_key"] != session["document_key"]
        assert response.json()["state"] == "editing"
        assert infos == [
            {"c": "info", "key": session["document_key"]},
            {"c": "info", "key": "replacement-editor-key"},
        ]
        assert _snapshot(data) == frozen


@pytest.mark.parametrize("change", ("epoch", "terminal"))
def test_close_revalidates_authority_after_lookup_overlap(office_world, monkeypatch, change):
    import docker_manager
    from office.store import OfficeStore

    http, data, _recording, _manager, _broker = office_world
    file_id, session = _prepare(office_world, monkeypatch, "close")

    def mutate():
        if change == "epoch":
            with docker_manager._combined_lock(CHAT):
                (data / ".office-restore-epoch").write_text("epoch-during-info")
        elif change == "terminal":
            response = _post(http, session, recorded_status_4_payload(
                document_key=session["document_key"],
            ))
            assert response.status_code == 200 and response.json() == {"error": 0}

    with _pending_info(office_world, monkeypatch, "close", file_id, session) as (finish, overlap):
        overlap(mutate)
        frozen = _snapshot(data)
        observed = json.loads(_state(data).read_bytes())
        response = finish()
        _assert_refusal(response, 409, "session_not_open")
        if change == "epoch":
            after = OfficeStore().read(CHAT)
            record = after["sessions"][session["session_id"]]
            assert (record["state"], record["reason"], record["save_seq"]) == (
                "orphaned", "restore_epoch_changed", 0,
            )
            assert record.get("pending_close_seq") is None
            assert after == {**observed, "sessions": {**observed["sessions"], session["session_id"]: {
                **observed["sessions"][session["session_id"]],
                "state": "orphaned", "reason": "restore_epoch_changed",
            }}}
            _assert_other_bytes(frozen, data)
        else:
            assert _snapshot(data) == frozen


def test_sweep_does_not_timeout_newer_save_after_lookup_overlap(office_world, monkeypatch):
    http, data, _recording, _manager, _broker = office_world
    file_id, session = _prepare(office_world, monkeypatch, "poll")

    def save_again():
        failed = _post(http, session, synthetic_status_7_payload(
            document_key=session["document_key"], save_seq=3, intent="persist",
        ))
        assert failed.status_code == 200 and failed.json() == {"error": 0}
        with _command_box(monkeypatch, session["document_key"]):
            saved = _save(http, session["session_id"], "persist")
        assert saved.status_code == 202
        assert saved.json() == {"session_id": session["session_id"], "save_seq": 4, "intent": "persist"}

    with _pending_info(office_world, monkeypatch, "poll", file_id, session) as (finish, overlap):
        overlap(save_again)
        frozen = _snapshot(data)
        assert finish() is None
        assert _snapshot(data) == frozen
        record = json.loads(_state(data).read_bytes())["sessions"][session["session_id"]]
        assert (record["state"], record["save_seq"], record["pending_save_seq"]) == ("saving", 4, 4)
        assert record["saving_started_at"] == NOW


@pytest.mark.parametrize("change", ("selection", "file_identity"))
def test_restore_rechecks_original_file_and_selected_version_after_lookup(
    office_world, monkeypatch, change,
):
    import docker_manager
    from office.store import OfficeStore

    _http, data, _recording, _manager, broker = office_world
    file_id, session = _prepare(office_world, monkeypatch, "restore")
    _unpublished(file_id)

    def mutate():
        with docker_manager._combined_lock(CHAT):
            if change == "selection":
                def remove_selected(state):
                    state["documents"][file_id]["versions"].pop()
                OfficeStore().update(CHAT, remove_selected)
            else:
                path = _outputs(data) / "brief.docx"
                content = path.read_bytes()
                path.unlink()
                broker.OutputsBroker().reconcile(CHAT)
                path.write_bytes(content)
                listing = broker.OutputsBroker().reconcile(CHAT)
                assert next(item["file_id"] for item in listing["entries"] if item["path"] == "brief.docx") != file_id

    with _pending_info(
        office_world, monkeypatch, "restore", file_id, session, code=1, number=2,
    ) as (finish, overlap):
        overlap(mutate)
        frozen = _snapshot(data)
        response = finish()
        _assert_refusal(response, 404, "unknown_version" if change == "selection" else "unknown_file")
        assert _snapshot(data) == frozen


@pytest.mark.parametrize("caller,code", (("join", 5), ("restore", 5), ("close", 1), ("poll", 1)))
def test_info_activity_callback_does_not_retry_unchanged_failure_into_success(
    office_world, monkeypatch, caller, code,
):
    http, data, _recording, _manager, _broker = office_world
    file_id, session = _prepare(office_world, monkeypatch, caller)
    calls = []
    with _pending_info(
        office_world, monkeypatch, caller, file_id, session,
        code=code, subsequent_code=0, info_calls=calls,
    ) as (finish, overlap):
        response = overlap(lambda: _post(http, session, recorded_status_1_payload(
            document_key=session["document_key"],
        )))
        assert response.status_code == 200 and response.json() == {"error": 0}
        during = json.loads(_state(data).read_bytes())
        frozen = _snapshot(data)
        response = finish()
        if caller in ("join", "restore"):
            _assert_refusal(response, 502, "documentserver_unavailable")
            assert _snapshot(data) == frozen
        else:
            if caller == "close":
                _assert_refusal(response, 409, "session_not_open")
            else:
                assert response is None
            expected = {
                **during["sessions"][session["session_id"]],
                "state": "orphaned", "reason": "editor_state_lost",
            }
            assert json.loads(_state(data).read_bytes()) == {
                **during, "sessions": {**during["sessions"], session["session_id"]: expected},
            }
            _assert_other_bytes(frozen, data)
        assert calls == [{"c": "info", "key": session["document_key"]}]


@pytest.mark.parametrize("change", ("epoch", "terminal", "replacement"))
def test_sweep_discards_unknown_observation_after_authority_changes(office_world, monkeypatch, change):
    import docker_manager

    http, data, _recording, _manager, _broker = office_world
    file_id, session = _prepare(office_world, monkeypatch, "poll")

    def mutate():
        if change == "epoch":
            with docker_manager._combined_lock(CHAT):
                (data / ".office-restore-epoch").write_text("new-sweep-epoch")
        else:
            response = _post(http, session, recorded_status_4_payload(
                document_key=session["document_key"],
            ))
            assert response.status_code == 200 and response.json() == {"error": 0}
            if change == "replacement":
                created = _replace_canonical_session(session["session_id"], "opening")
                assert created["document_key"] != session["document_key"]

    with _pending_info(office_world, monkeypatch, "poll", file_id, session, code=1) as (finish, overlap):
        overlap(mutate)
        frozen = _snapshot(data)
        assert finish() is None
        assert _snapshot(data) == frozen


@pytest.mark.parametrize("change", ("key", "file"))
def test_close_never_allocates_for_replacement_session_responsibility(office_world, monkeypatch, change):
    http, data, _recording, _manager, _broker = office_world
    file_id, session = _prepare(office_world, monkeypatch, "close")

    def mutate():
        if change == "key":
            _change(session["session_id"], document_key="replacement-editor-key")
        else:
            state = json.loads(_state(data).read_bytes())
            other = next(key for key in state["documents"] if key != file_id)
            _change(session["session_id"], file_id=other)

    with _pending_info(office_world, monkeypatch, "close", file_id, session, code=1) as (finish, overlap):
        overlap(mutate)
        frozen = _snapshot(data)
        _assert_refusal(finish(), 409, "session_not_open")
        assert _snapshot(data) == frozen


def test_restore_refuses_new_opening_session_instead_of_using_old_unknown_key(office_world, monkeypatch):
    http, data, _recording, _manager, _broker = office_world
    file_id, session = _prepare(office_world, monkeypatch, "restore")

    def replace():
        response = _post(http, session, recorded_status_4_payload(
            document_key=session["document_key"],
        ))
        assert response.status_code == 200 and response.json() == {"error": 0}
        return _replace_canonical_session(session["session_id"], "opening")

    with _pending_info(office_world, monkeypatch, "restore", file_id, session, code=1) as (finish, overlap):
        successor = overlap(replace)
        assert successor["state"] == "opening" and successor["session_id"] != session["session_id"]
        frozen = _snapshot(data)
        _assert_refusal(finish(), 409, "session_open")
        assert _snapshot(data) == frozen


def test_restore_settles_final_publication_accepted_during_lookup(office_world, monkeypatch):
    from tests.orchestrator.test_office_callback_processing import CHANGED, _bind_internal, _content_origin
    from tests.orchestrator.test_office_callback_publish import _opened, _surviving

    http, data, _origin, _manager, _broker, original, session = _opened(office_world)
    file_id = session["file_id"]
    with _content_origin({"/save.docx": CHANGED}) as server:
        with _pending_info(office_world, monkeypatch, "restore", file_id, session, code=1) as (finish, overlap):
            def accept_final():
                # Info is already held; bind the independent download origin only for this callback.
                with _bind_internal(monkeypatch, server.url):
                    _surviving(http, data, session, monkeypatch, server, final=True)
            overlap(accept_final)
            accepted = json.loads(_state(data).read_bytes())
            assert list(accepted["journal"].values()) == [{
                "file_id": file_id, "version": 2, "session_id": session["session_id"],
                "save_seq": 1, "requester": "final",
            }]
            response = finish()
            assert response.status_code == 200
            assert response.json() == {"file_id": file_id, "number": 3, "published": True}
    after = json.loads(_state(data).read_bytes())
    record = after["sessions"][session["session_id"]]
    assert (record["state"], record["reason"], record["save_seq"]) == ("closed", None, 1)
    assert (record["last_committed_seq"], record["last_published_seq"]) == (1, 1)
    assert record.get("pending_close_seq") is None
    assert after["receipts"] == accepted["receipts"] and after["journal"] == {}
    listed = after["documents"][file_id]["versions"]
    assert listed[0] == accepted["documents"][file_id]["versions"][0]
    assert listed[1] == {**accepted["documents"][file_id]["versions"][1], "published": True}
    assert (listed[-1]["source"], listed[-1]["parent"], listed[-1]["published"]) == ("restore", 1, True)
    assert (_outputs(data) / "report.docx").read_bytes() == original


def test_restore_never_follows_copy_recovery_to_successor_file_during_lookup(office_world, monkeypatch):
    from tests.orchestrator.test_office_callback_processing import CHANGED
    from tests.orchestrator.test_office_resolution import _conflict, _kill_resolve

    http, data, _origin, _manager, _broker, original, session = _conflict(office_world, monkeypatch)
    file_id = session["file_id"]
    with _pending_info(office_world, monkeypatch, "restore", file_id, session, code=1) as (finish, overlap):
        overlap(lambda: _kill_resolve(data, session, "save_as", "accepted"))
        accepted = json.loads(_state(data).read_bytes())
        assert any(
            entry.get("requester") == "resolve" and entry.get("action") == "save_as"
            and entry.get("file_id") == file_id
            for entry in accepted["journal"].values()
        )
        response = finish()
        assert response.status_code == 200
        assert response.json() == {"file_id": file_id, "number": 4, "published": True}
    after = json.loads(_state(data).read_bytes())
    record = after["sessions"][session["session_id"]]
    assert record["file_id"] != file_id
    assert (record["state"], record["reason"], record["document_key"]) == ("editing", None, session["document_key"])
    assert record["last_committed_seq"] == record["last_published_seq"] == 1
    assert after["receipts"] == accepted["receipts"] and after["journal"] == {}
    assert (_outputs(data) / "report.docx").read_bytes() == original
    assert (_outputs(data) / record["saved_as"]["path"]).read_bytes() == CHANGED
    successor = after["documents"][record["file_id"]]
    assert successor["published_version"] == 1
    assert [(item["source"], item["published"]) for item in successor["versions"]] == [("conflict", True)]
