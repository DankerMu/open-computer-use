# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Startup failures must survive the ordinary-holder owner's cleanup."""
from __future__ import annotations

import fcntl
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from tests.orchestrator import _office_store
from tests.orchestrator import test_office_control_plane_concurrency as contention
from tests.orchestrator.test_office_sessions import office_world as office_world


@pytest.mark.parametrize("failure_stage", ["popen", "first-marker"])
def test_ordinary_process_holder_startup_failure_preserves_error_and_cleans_up(
    office_world, monkeypatch, failure_stage,
):
    _http, data, _origin, docker, _broker = office_world
    original_lock = docker._combined_lock
    original_flock = fcntl.flock
    original_wait_marker = _office_store._wait_marker
    injected = OSError(f"injected ordinary holder {failure_stage} failure")
    children = []
    watchdogs = []
    join_started = []
    popen_calls = []
    marker_calls = []
    caught = None

    class RecordingThread(threading.Thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            watchdogs.append(self)

        def join(self, *args, **kwargs):
            join_started.append(self.ident is not None)
            return super().join(*args, **kwargs)

    def start_holder(*args, **kwargs):
        popen_calls.append(True)
        if failure_stage == "popen":
            raise injected
        child = subprocess.Popen(*args, **kwargs)
        children.append(child)
        return child

    def wait_then_fail(path, child, label):
        marker_calls.append(path)
        original_wait_marker(path, child, label)
        assert path == data.parent / "callback-process-entered"
        assert child is children[0]
        assert path.read_text(encoding="utf-8") == "held"
        assert child.poll() is None
        raise injected

    # Replace only the owner's bindings: fixtures still use real threads and Popen.
    with monkeypatch.context() as patch:
        patch.setattr(
            contention, "threading",
            SimpleNamespace(Event=threading.Event, Thread=RecordingThread),
        )
        patch.setattr(
            contention, "subprocess",
            SimpleNamespace(Popen=start_holder, PIPE=subprocess.PIPE),
        )
        if failure_stage == "first-marker":
            # The owner imports this helper inside its test function.
            patch.setattr(_office_store, "_wait_marker", wait_then_fail)

        try:
            try:
                contention.test_callback_waits_for_ordinary_process_holder_while_health_progresses(office_world)
            except Exception as error:
                caught = error

            # Observe owner cleanup before either rescue or outer fixture teardown.
            lock_restored = docker._combined_lock is original_lock
            flock_restored = fcntl.flock is original_flock
            child_states = [
                (child.poll(), child.stdout.closed, child.stderr.closed)
                for child in children
            ]
            owner_joins = list(join_started)

            assert caught is injected, f"owner raised {caught!r} instead of the injected exception"
            assert popen_calls == [True]
            assert len(watchdogs) == 1
            assert all(owner_joins), "owner joined a watchdog that never started"
            assert lock_restored, "owner did not restore the Docker lock"
            assert flock_restored, "owner did not restore fcntl.flock"
            if failure_stage == "popen":
                assert children == []
                assert marker_calls == []
            else:
                assert marker_calls == [data.parent / "callback-process-entered"]
                assert len(child_states) == 1
                returncode, stdout_closed, stderr_closed = child_states[0]
                assert returncode is not None, "owner left the real holder running"
                assert stdout_closed, "owner left holder stdout open"
                assert stderr_closed, "owner left holder stderr open"
        finally:
            # Rescue never contributes to the assertions above, including on RED.
            docker._combined_lock = original_lock
            fcntl.flock = original_flock
            try:
                if children:
                    (data.parent / "callback-process-release").write_text("release", encoding="utf-8")
            finally:
                try:
                    for child in children:
                        try:
                            if child.poll() is None:
                                child.kill()
                                child.wait(timeout=2)
                        finally:
                            child.stdout.close()
                            child.stderr.close()
                finally:
                    for watchdog in watchdogs:
                        if watchdog.ident is not None:
                            threading.Thread.join(watchdog, timeout=2)


def test_thread_holder_readiness_failure_preserves_error_and_cleans_up(office_world, monkeypatch):
    _http, _data, _origin, docker, _broker = office_world
    original_lock = docker._combined_lock
    owner_thread = threading.get_ident()
    injected = OSError("injected thread holder readiness failure")
    events = []
    futures = []
    original_results = []
    readiness_observed = []
    owner_releases = []
    owner_results = []
    emergency_releases = []
    caught = None

    class RecordingEvent(threading.Event):
        def __init__(self):
            super().__init__()
            events.append(self)

        def wait(self, timeout=None):
            signaled = super().wait(timeout)
            if self is events[0] and threading.get_ident() == owner_thread:
                assert signaled, "real thread holder did not acquire the chat lock"
                readiness_observed.append(True)
                raise injected
            return signaled

        def set(self):
            if self is events[1] and threading.get_ident() == owner_thread:
                owner_releases.append(True)
            return super().set()

    class RecordingExecutor(ThreadPoolExecutor):
        def submit(self, *args, **kwargs):
            future = super().submit(*args, **kwargs)
            futures.append(future)
            original_result = future.result
            original_results.append(original_result)

            def record_result(*args, **kwargs):
                if threading.get_ident() == owner_thread:
                    owner_results.append(True)
                return original_result(*args, **kwargs)

            future.result = record_result
            return future

        def __exit__(self, *args):
            # Rescue before real executor shutdown, not after its 10-second wait.
            # Bypass the recorder so this can never count as the owner's release.
            if not events[1].is_set():
                emergency_releases.append(True)
                threading.Event.set(events[1])
            return super().__exit__(*args)

    with monkeypatch.context() as patch:
        patch.setattr(contention, "threading", SimpleNamespace(Event=RecordingEvent))
        patch.setattr(contention, "ThreadPoolExecutor", RecordingExecutor)
        try:
            try:
                contention.test_callback_lock_keeps_same_loop_health_responsive(office_world)
            except Exception as error:
                caught = error

            # These observations precede fallback release/restoration below.
            lock_restored = docker._combined_lock is original_lock
            worker_completed = [future.done() for future in futures]

            assert caught is injected, f"owner raised {caught!r} instead of the readiness exception"
            assert readiness_observed == [True]
            assert owner_releases, f"owner did not release its holder; emergency releases: {emergency_releases}"
            assert owner_results, "owner did not retrieve the real holder future's result"
            assert emergency_releases == [], "emergency release was needed to finish the holder"
            assert lock_restored, "owner did not restore the Docker lock"
            assert worker_completed == [True], "real holder worker did not complete"
            assert original_results[0](timeout=0) is None
            assert not events[2].is_set(), "real holder expired instead of being explicitly released"
        finally:
            try:
                if len(events) > 1:
                    threading.Event.set(events[1])
                for original_result in original_results:
                    original_result(timeout=2)
            finally:
                docker._combined_lock = original_lock
