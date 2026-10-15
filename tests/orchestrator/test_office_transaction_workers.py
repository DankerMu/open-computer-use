# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Office transaction admission, cancellation and lifespan ownership with real workers."""
from __future__ import annotations

import asyncio
import contextvars
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import anyio
import anyio.to_thread
import httpx
import pytest
from starlette.datastructures import State

from tests.orchestrator._office_recorded_callbacks import recorded_status_1_payload
from tests.orchestrator.test_office_callback_processing import _post
from tests.orchestrator.test_office_save_close import _command_box, _lookup, _save
from tests.orchestrator.test_office_session_lifecycle import _created
from tests.orchestrator.test_office_sessions import (
    _snapshot, _state, office_world as office_world,
)
from tests.orchestrator.test_outputs_endpoint import CHAT, _auth

READY_TIMEOUT = 2
RESCUE_TIMEOUT = 5


async def _event(event):
    async def wait():
        while not event.is_set():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(wait(), READY_TIMEOUT)


def _held(event):
    assert event.wait(RESCUE_TIMEOUT), "test worker emergency escape invalidates ownership proof"


@contextmanager
def _resources(monkeypatch, workers, *, fail_start=False, shutdown_failure=None):
    """Control executor capacity while retaining real startup, work and shutdown."""
    resources = SimpleNamespace(
        executors=[], started=[], shutdown=[], futures=[], joining_workers=threading.Event(),
    )
    start = threading.Thread.start
    join = threading.Thread.join

    class Executor(ThreadPoolExecutor):
        def __init__(self, *args, **kwargs):
            super().__init__(max_workers=1, thread_name_prefix="test-office-transaction-owned")
            resources.executors.append(self)

        def submit(self, *args, **kwargs):
            future = super().submit(*args, **kwargs)
            resources.futures.append(future)
            return future

        def shutdown(self, *args, **kwargs):
            super().shutdown(*args, **kwargs)
            resources.shutdown.append(self)
            if shutdown_failure is not None:
                raise shutdown_failure

    def launch(thread):
        if fail_start:
            raise RuntimeError("controlled supervisor startup failure")
        start(thread)
        resources.started.append(thread)

    def collect(thread, *args, **kwargs):
        if thread.name.startswith("test-office-transaction-owned"):
            resources.joining_workers.set()
        return join(thread, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(workers, "ThreadPoolExecutor", Executor)
            patch.setattr(threading.Thread, "start", launch)
            patch.setattr(threading.Thread, "join", collect)
            yield resources
    finally:
        assert len(resources.executors) == 1
        assert all(executor in resources.shutdown for executor in resources.executors)
        assert all(not thread.is_alive() for thread in resources.started)


def _other_bytes(before, data):
    state_path = str(_state(data).relative_to(data))
    assert {key: value for key, value in _snapshot(data).items() if key != state_path} == {
        key: value for key, value in before.items() if key != state_path
    }


def test_forcesave_induced_callback_finishes_before_accepted_command_response(
    office_world, monkeypatch,
):
    # Exercise the public save route and authenticated callback admission.
    http, data, recording, _manager, _broker = office_world
    _file_id, session = _created(office_world, "editing")
    before = json.loads(_state(data).read_bytes())
    before_bytes = _snapshot(data)
    callback_done, save_done, release = (threading.Event() for _ in range(3))
    callback, saved, failures = {}, {}, []

    def limit_callbacks():
        limiter = anyio.to_thread.current_default_thread_limiter()
        prior = limiter.total_tokens
        limiter.total_tokens = 1
        return limiter, prior

    limiter, prior = http.portal.call(limit_callbacks)

    def induce(_userdata):
        try:
            response = _post(http, session, recorded_status_1_payload(document_key=session["document_key"]))
            callback.update(response=response, released=release.is_set(), save_done=save_done.is_set())
        except BaseException as exc:
            failures.append(exc)
        finally:
            callback_done.set()

    def save():
        try:
            saved["response"] = _save(http, session["session_id"], "persist")
        except BaseException as exc:
            failures.append(exc)
        finally:
            save_done.set()

    with _command_box(monkeypatch, session["document_key"], observe=induce, release=release) as (origin, saves, infos):
        worker = threading.Thread(target=save, name="office-induced-callback-save")
        try:
            worker.start()
            assert callback_done.wait(READY_TIMEOUT), "authenticated induced callback is blocked behind save admission"
            if failures:
                raise failures[0]
            assert callback["response"].status_code == 200
            assert callback["response"].json() == {"error": 0}
            assert not callback["released"] and not callback["save_done"]
            assert not release.is_set() and not save_done.is_set()
            during = json.loads(_state(data).read_bytes())
            record = during["sessions"][session["session_id"]]
            assert record["state"] == "saving"
            assert record["save_seq"] == record["pending_save_seq"] == 1
            assert record["save_intents"] == {"1": "persist"}
            release.set()
            assert save_done.wait(READY_TIMEOUT)
            if failures:
                raise failures[0]
            assert saved["response"].status_code == 202
            assert saved["response"].json() == {
                "session_id": session["session_id"], "save_seq": 1, "intent": "persist",
            }
            assert json.loads(_state(data).read_bytes()) == during
            assert during["documents"] == before["documents"]
            assert during["receipts"] == before["receipts"]
            assert during["journal"] == before["journal"]
            assert saves == [{"save_seq": 1, "intent": "persist"}] and infos == []
            assert len(origin.requests) == 1
            _other_bytes(before_bytes, data)
        finally:
            # Rescue must free callback admission before collecting the blocked RED request.
            http.portal.call(setattr, limiter, "total_tokens", prior)
            release.set()
            if worker.ident is not None:
                worker.join(RESCUE_TIMEOUT)
            assert not worker.is_alive(), "save request survived callback/response release"
            assert callback_done.wait(RESCUE_TIMEOUT)
            if failures:
                raise failures[0]
    assert recording.hits == 0


@pytest.mark.parametrize("body_failure", (False, True))
def test_transaction_owner_drains_real_workers_on_normal_and_failed_body(monkeypatch, tmp_path, body_failure):
    import office.transaction_workers as workers

    app = SimpleNamespace(state=State())
    failure = ValueError("controlled lifespan body failure")
    release, entered = threading.Event(), threading.Event()
    output = tmp_path / "worker-result"
    queued_output = tmp_path / "queued-result"

    def work():
        entered.set()
        _held(release)
        output.write_bytes(b"owned transaction finished")
        return threading.current_thread()

    async def scenario():
        with pytest.raises(Exception):
            await workers.run_transaction_worker(app, output.write_bytes, b"unowned execution")
        assert not output.exists()
        requester = queued = coordinator = None

        async def release_after_admission_closes():
            async def withdrawn():
                while getattr(app.state, "office_transaction_executor", None) is not None:
                    await asyncio.sleep(0.001)
            try:
                await asyncio.wait_for(withdrawn(), READY_TIMEOUT)
                # Joining begins after real executor shutdown has handled its queue.
                await _event(resources.joining_workers)
                assert not release.is_set()
                assert resources.futures[0].running()
                assert not resources.futures[1].running() and not resources.futures[1].cancelled()
            finally:
                release.set()
        try:
            async with workers.transaction_worker_lifespan(app):
                assert len(resources.started) == 1 and resources.started[0].is_alive()
                try:
                    requester = asyncio.create_task(workers.run_transaction_worker(app, work))
                    await _event(entered)
                    queued = asyncio.create_task(workers.run_transaction_worker(
                        app, queued_output.write_bytes, b"queued transaction finished",
                    ))
                    await asyncio.sleep(0)
                    assert len(resources.futures) == 2 and not resources.futures[1].running()
                    requester.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(asyncio.shield(requester), READY_TIMEOUT)
                    coordinator = asyncio.create_task(release_after_admission_closes())
                    if body_failure:
                        raise failure
                finally:
                    if coordinator is None:
                        release.set()
        finally:
            release.set()
            if requester is not None and not requester.done():
                requester.cancel()
                await asyncio.gather(requester, return_exceptions=True)
            if queued is not None:
                await asyncio.gather(queued, return_exceptions=True)
            if coordinator is not None:
                await coordinator
        assert output.read_bytes() == b"owned transaction finished"
        assert queued_output.read_bytes() == b"queued transaction finished"
        with pytest.raises(Exception):
            await workers.run_transaction_worker(app, output.write_bytes, b"post-close execution")
        assert output.read_bytes() == b"owned transaction finished"

    with _resources(monkeypatch, workers) as resources:
        if body_failure:
            with pytest.raises(ValueError) as caught:
                asyncio.run(scenario())
            assert caught.value is failure
            assert output.read_bytes() == b"owned transaction finished"
            assert queued_output.read_bytes() == b"queued transaction finished"
        else:
            asyncio.run(scenario())


def test_transaction_owner_start_failure_cleans_unused_executor_without_admission(monkeypatch):
    import office.transaction_workers as workers

    app = SimpleNamespace(state=State())
    admitted = []

    async def scenario():
        with pytest.raises(RuntimeError, match="controlled supervisor startup failure"):
            async with workers.transaction_worker_lifespan(app):
                admitted.append("body entered")
        with pytest.raises(Exception):
            await workers.run_transaction_worker(app, admitted.append, "fallback worker")

    with _resources(monkeypatch, workers, fail_start=True) as resources:
        asyncio.run(scenario())
    assert admitted == []
    assert resources.started == []


def test_transaction_owner_publication_failure_joins_started_supervisor(monkeypatch):
    import office.transaction_workers as workers

    failure = RuntimeError("controlled owner publication failure")

    class PublishingState(State):
        def __setattr__(self, name, value):
            if name == "office_transaction_executor" and value is not None:
                raise failure
            super().__setattr__(name, value)

    app = SimpleNamespace(state=PublishingState())
    admitted = []

    async def scenario():
        with pytest.raises(RuntimeError) as caught:
            async with workers.transaction_worker_lifespan(app):
                admitted.append("body entered")
        assert caught.value is failure
        with pytest.raises(Exception):
            await workers.run_transaction_worker(app, admitted.append, "fallback worker")

    with _resources(monkeypatch, workers) as resources:
        asyncio.run(scenario())
    assert admitted == []
    assert len(resources.started) == 1


def test_transaction_owner_propagates_shutdown_failure_after_real_workers_join(monkeypatch, tmp_path):
    import office.transaction_workers as workers

    app = SimpleNamespace(state=State())
    failure = OSError("controlled shutdown failure")
    output = tmp_path / "completed-save"

    async def scenario():
        with pytest.raises(OSError) as caught:
            async with workers.transaction_worker_lifespan(app):
                await workers.run_transaction_worker(app, output.write_bytes, b"save completed")
        assert caught.value is failure
        with pytest.raises(Exception):
            await workers.run_transaction_worker(app, output.write_bytes, b"fallback after failed shutdown")
        assert output.read_bytes() == b"save completed"

    with _resources(monkeypatch, workers, shutdown_failure=failure):
        asyncio.run(scenario())


def test_transaction_owner_isolates_fresh_request_context_through_loop_submission(monkeypatch):
    import office.transaction_workers as workers

    request_value = contextvars.ContextVar("office-transaction-request", default="outside")
    app = SimpleNamespace(state=State())

    async def scenario():
        loop = asyncio.get_running_loop()

        async def read_context():
            return request_value.get()

        def work():
            initial = request_value.get()
            submitted = asyncio.run_coroutine_threadsafe(read_context(), loop).result(READY_TIMEOUT)
            request_value.set("worker-only")
            return initial, submitted, threading.current_thread()

        async def request(value):
            token = request_value.set(value)
            try:
                initial, submitted, thread = await workers.run_transaction_worker(app, work)
                assert (initial, submitted, request_value.get()) == (value, value, value)
                assert thread is not threading.current_thread()
            finally:
                request_value.reset(token)

        async with workers.transaction_worker_lifespan(app):
            await asyncio.gather(request("first-request"), request("second-request"))
            initial, submitted, _thread = await workers.run_transaction_worker(app, work)
            assert (initial, submitted, request_value.get()) == ("outside", "outside", "outside")
            with pytest.raises(ValueError, match="invalid save operation"):
                def invalid():
                    raise ValueError("invalid save operation")
                await workers.run_transaction_worker(app, invalid)

    with _resources(monkeypatch, workers):
        asyncio.run(scenario())


async def _transaction_request(http, session, operation="save"):
    if operation in ("save", "close"):
        path = f"/api/office/{CHAT}/sessions/{session['session_id']}/{operation}"
        body = {"intent": "persist"} if operation == "save" else {}
    else:
        path = f"/api/office/{CHAT}/documents/{session['file_id']}"
        path += "/sessions" if operation == "join" else "/restore"
        body = {} if operation == "join" else {"number": 1}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=http.app), base_url="http://testserver") as client:
        return await client.post(path, headers=_auth(), json=body)


@pytest.mark.parametrize("operation", ("save", "join", "close", "restore"))
def test_cancelled_queued_transaction_issues_no_sequence_or_command(office_world, monkeypatch, operation):
    import office.transaction_workers as workers

    http, data, recording, _manager, _broker = office_world
    _file_id, session = _created(office_world, "editing")
    before = _snapshot(data)
    app = SimpleNamespace(state=State())
    entered, release = threading.Event(), threading.Event()

    def occupied():
        entered.set()
        _held(release)

    async def scenario(resources):
        blocker = queued = None
        async with workers.transaction_worker_lifespan(app):
            previous = getattr(http.app.state, "office_transaction_executor", None)
            http.app.state.office_transaction_executor = app.state.office_transaction_executor
            try:
                blocker = asyncio.create_task(workers.run_transaction_worker(app, occupied))
                await _event(entered)
                queued = asyncio.create_task(_transaction_request(http, session, operation))

                async def submitted():
                    while len(resources.futures) < 2:
                        await asyncio.sleep(0.001)
                await asyncio.wait_for(submitted(), READY_TIMEOUT)
                assert not resources.futures[1].running()
                queued.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(queued), READY_TIMEOUT)
                release.set()
                await blocker
            finally:
                release.set()
                http.app.state.office_transaction_executor = previous
                for task in (blocker, queued):
                    if task is not None:
                        if not task.done():
                            task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    with _command_box(monkeypatch, session["document_key"]) as (origin, saves, infos):
        with _resources(monkeypatch, workers) as resources:
            http.portal.call(scenario, resources)
        assert origin.requests == [] and saves == [] and infos == []
    assert _snapshot(data) == before
    assert recording.hits == 0


@pytest.mark.parametrize("code,state,reason", ((1, "orphaned", "editor_state_lost"), (5, "editing", None)))
def test_cancelled_running_save_finishes_outcome_specific_reconciliation(
    office_world, monkeypatch, code, state, reason,
):
    import office.transaction_workers as workers

    http, data, recording, _manager, _broker = office_world
    _file_id, session = _created(office_world, "editing")
    before = json.loads(_state(data).read_bytes())
    before_bytes = _snapshot(data)
    app = SimpleNamespace(state=State())
    entered, release = threading.Event(), threading.Event()

    async def scenario(resources):
        requester = None
        async with workers.transaction_worker_lifespan(app):
            previous = getattr(http.app.state, "office_transaction_executor", None)
            http.app.state.office_transaction_executor = app.state.office_transaction_executor
            try:
                requester = asyncio.create_task(_transaction_request(http, session))
                await _event(entered)
                assert len(resources.futures) == 1 and resources.futures[0].running()
                requester.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(requester), READY_TIMEOUT)
                assert not resources.futures[0].done()
                record = json.loads(_state(data).read_bytes())["sessions"][session["session_id"]]
                assert record["state"] == "saving" and record["pending_save_seq"] == 1
            finally:
                release.set()
                http.app.state.office_transaction_executor = previous
                if requester is not None:
                    if not requester.done():
                        requester.cancel()
                    await asyncio.gather(requester, return_exceptions=True)
        assert resources.futures[0].done()

    with _command_box(monkeypatch, session["document_key"], forcesave_code=code,
                      entered=entered, release=release) as (origin, saves, infos):
        with _resources(monkeypatch, workers) as resources:
            http.portal.call(scenario, resources)
        assert len(origin.requests) == 1
        assert saves == [{"save_seq": 1, "intent": "persist"}] and infos == []
    after = json.loads(_state(data).read_bytes())
    target = after["sessions"][session["session_id"]]
    assert target["state"] == state and target["reason"] == reason
    assert target["save_seq"] == 1 and target["pending_save_seq"] is None
    assert target["save_intents"] == {"1": "persist"}
    assert target["last_committed_seq"] == target["last_published_seq"] == 0
    assert after["documents"] == before["documents"]
    assert after["receipts"] == before["receipts"] and after["journal"] == before["journal"]
    assert resources.futures[0].exception().reason == (
        "session_not_editing" if code == 1 else "documentserver_unavailable"
    )
    _other_bytes(before_bytes, data)
    assert recording.hits == 0


@pytest.mark.parametrize("cancellation", ("asyncio", "anyio"))
def test_teardown_drains_cancelled_transaction_with_both_shared_worker_pools_occupied(monkeypatch, cancellation):
    import office.transaction_workers as workers

    app = SimpleNamespace(state=State())
    default_entered, callback_entered, work_entered = (threading.Event() for _ in range(3))
    shared_release, work_release, work_finished = (threading.Event() for _ in range(3))
    body_exit, closing, cancellation_seen = (threading.Event() for _ in range(3))
    scopes, failures, ordering = [], [], []

    async def scenario(resources):
        loop = asyncio.get_running_loop()
        default = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-dns-occupied")
        loop.set_default_executor(default)
        limiter = anyio.to_thread.current_default_thread_limiter()
        prior_tokens = limiter.total_tokens
        limiter.total_tokens = 1

        def block(entered):
            entered.set()
            _held(shared_release)

        async def commit():
            ordering.append("loop reconciliation")
            return "committed"

        def work():
            work_entered.set()
            try:
                _held(work_release)
                assert asyncio.run_coroutine_threadsafe(commit(), loop).result(READY_TIMEOUT) == "committed"
                work_finished.set()
            except BaseException as exc:
                failures.append(exc)
                raise

        async def own():
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                try:
                    async with workers.transaction_worker_lifespan(app):
                        requester = asyncio.create_task(workers.run_transaction_worker(app, work))
                        await _event(work_entered)
                        requester.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await asyncio.wait_for(asyncio.shield(requester), READY_TIMEOUT)
                        await _event(body_exit)
                        closing.set()
                except asyncio.CancelledError:
                    assert work_finished.is_set()
                    assert all(not thread.is_alive() for thread in resources.started)
                    ordering.append("cancellation propagated")
                    cancellation_seen.set()
                    raise

        default_job = callback_job = owner = None
        try:
            default_job = loop.run_in_executor(default, block, default_entered)
            callback_job = asyncio.create_task(anyio.to_thread.run_sync(block, callback_entered))
            await _event(default_entered)
            await _event(callback_entered)
            # Record only transaction-owner threads, not the occupied shared pools.
            resources.started.clear()
            owner = asyncio.create_task(own())
            await _event(work_entered)
            body_exit.set()
            await _event(closing)
            await asyncio.sleep(0)
            assert getattr(app.state, "office_transaction_executor", None) is None
            with pytest.raises(Exception):
                await workers.run_transaction_worker(app, ordering.append, "late admission")
            if cancellation == "asyncio":
                owner.cancel()
                await asyncio.sleep(0)
                owner.cancel()
            else:
                scopes[0].cancel()
                await asyncio.sleep(0)
                scopes[0].cancel()
            await asyncio.sleep(0)
            assert not owner.done() and not work_finished.is_set() and not cancellation_seen.is_set()
            assert not shared_release.is_set()
            assert not default_job.done() and not callback_job.done()
            # Drain must complete while both shared domains remain unavailable.
            work_release.set()
            if cancellation == "asyncio":
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(owner), READY_TIMEOUT)
            else:
                await asyncio.wait_for(asyncio.shield(owner), READY_TIMEOUT)
                assert scopes[0].cancelled_caught
            assert work_finished.is_set() and cancellation_seen.is_set()
            assert ordering == ["loop reconciliation", "cancellation propagated"]
            assert not shared_release.is_set()
            assert not default_job.done() and not callback_job.done()
            if failures:
                raise failures[0]
        finally:
            work_release.set()
            shared_release.set()
            limiter.total_tokens = prior_tokens
            if owner is not None:
                await asyncio.gather(owner, return_exceptions=True)
            if callback_job is not None:
                await asyncio.gather(callback_job, return_exceptions=True)
            if default_job is not None:
                await asyncio.gather(default_job, return_exceptions=True)

    with _resources(monkeypatch, workers) as resources:
        asyncio.run(scenario(resources))


def test_transaction_owner_start_failure_after_acquisition_drains_and_joins_before_propagating(monkeypatch):
    import office.transaction_workers as workers

    app = SimpleNamespace(state=State())
    failure = RuntimeError("controlled post-acquisition startup failure")
    original_thread, original_event = threading.Thread, threading.Event
    acquired, events, admitted = [], [], []
    shutdown_completed = original_event()

    class AcquiredStartFailure(original_thread):
        def start(self):
            super().start()
            acquired.append(self)
            raise failure

    class Executor(ThreadPoolExecutor):
        def shutdown(self, *args, **kwargs):
            super().shutdown(*args, **kwargs)
            shutdown_completed.set()

    def event():
        created = original_event()
        events.append(created)
        return created

    async def scenario():
        try:
            with pytest.raises(RuntimeError) as caught:
                async with workers.transaction_worker_lifespan(app):
                    admitted.append("body entered")
            assert caught.value is failure
            assert admitted == []
            assert len(acquired) == 1 and acquired[0].ident is not None
            assert shutdown_completed.is_set()
            assert not acquired[0].is_alive(), "acquired supervisor survived startup failure before rescue"
            with pytest.raises(Exception):
                await workers.run_transaction_worker(app, admitted.append, "fallback after failed start")
            assert admitted == []
        finally:
            # Release captured real Events only for rescue, after all proof observations.
            for pending in events:
                pending.set()
            for supervisor in acquired:
                supervisor.join(RESCUE_TIMEOUT)
                assert not supervisor.is_alive(), "acquired-start supervisor rescue failed"
            await asyncio.sleep(0)

    with monkeypatch.context() as patch:
        patch.setattr(workers.threading, "Thread", AcquiredStartFailure)
        patch.setattr(workers.threading, "Event", event)
        patch.setattr(workers, "ThreadPoolExecutor", Executor)
        asyncio.run(scenario())


@pytest.mark.parametrize("code,expected_state,reason,sequence", (
    (0, "closing", None, 1),
    (1, "orphaned", "editor_state_lost", 0),
    (5, "closing", None, 1),
))
def test_cancelled_running_close_drains_lookup_and_reconciles_current_authority(
    office_world, monkeypatch, code, expected_state, reason, sequence,
):
    import office.transaction_workers as workers

    http, data, recording, _manager, _broker = office_world
    monkeypatch.setattr("time.time", lambda: 1_700_000_000.25)
    _file_id, session = _created(office_world, "editing")
    before = json.loads(_state(data).read_bytes())
    before_bytes = _snapshot(data)
    app = SimpleNamespace(state=State())
    entered, release = threading.Event(), threading.Event()

    async def scenario(resources):
        requester = None
        async with workers.transaction_worker_lifespan(app):
            previous = http.app.state.office_transaction_executor
            http.app.state.office_transaction_executor = app.state.office_transaction_executor
            try:
                requester = asyncio.create_task(_transaction_request(http, session, "close"))
                await _event(entered)
                assert len(resources.futures) == 1 and resources.futures[0].running()
                requester.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(requester), READY_TIMEOUT)
                assert not resources.futures[0].done()
                assert json.loads(_state(data).read_bytes()) == before
            finally:
                release.set()
                http.app.state.office_transaction_executor = previous
                if requester is not None:
                    if not requester.done():
                        requester.cancel()
                    await asyncio.gather(requester, return_exceptions=True)
        assert resources.futures[0].done()

    with _lookup(
        monkeypatch, session["document_key"], code=code, entered=entered, release=release,
    ) as origin:
        with _resources(monkeypatch, workers) as resources:
            http.portal.call(scenario, resources)
        assert len(origin.requests) == 1
    expected = {
        **before["sessions"][session["session_id"]],
        "state": expected_state, "reason": reason, "save_seq": sequence,
    }
    if sequence:
        expected.update(pending_close_seq=1)
        expected["last_activity_at"] = 1_700_000_000.25
        assert resources.futures[0].result() == {
            "session_id": session["session_id"], "save_seq": 1, "state": "closing",
        }
    else:
        assert resources.futures[0].exception().reason == "session_not_open"
    assert json.loads(_state(data).read_bytes()) == {
        **before, "sessions": {**before["sessions"], session["session_id"]: expected},
    }
    _other_bytes(before_bytes, data)
    assert recording.hits == 0
