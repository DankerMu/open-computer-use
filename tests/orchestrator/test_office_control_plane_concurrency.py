# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Callback lock admission must not block the event loop."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tests.orchestrator.test_office_control_plane import _callback_path, _header_jwt, _open_session
from tests.orchestrator.test_office_sessions import SERVER_DIR, _snapshot, office_world
from tests.orchestrator.test_outputs_endpoint import CHAT


def _hold_chat_lock(docker, chat, held, release, expired, timeout=10):
    with docker._combined_lock(chat):
        held.set()
        if not release.wait(timeout):
            expired.set()


def test_callback_lock_keeps_same_loop_health_responsive(office_world):
    import httpx

    http, data, origin, docker, _broker, _content, session = _open_session(office_world)
    token = _header_jwt(session)
    before = _snapshot(data)
    held = threading.Event()
    release = threading.Event()
    expired = threading.Event()
    entering = threading.Event()
    original_lock = docker._combined_lock

    class AdmissionLock:
        def __init__(self, lock):
            self.lock = lock

        def __enter__(self):
            entering.set()
            return self.lock.__enter__()

        def __exit__(self, *args):
            return self.lock.__exit__(*args)

    def instrumented(chat, **kwargs):
        lock = original_lock(chat, **kwargs)
        return AdmissionLock(lock) if kwargs.get("create") is False else lock

    docker._combined_lock = instrumented

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=http.app),
            base_url="http://testserver",
            trust_env=False,
        ) as client:
            pending = asyncio.create_task(
                client.post(
                    _callback_path(session),
                    headers={"Authorization": "Bearer " + token},
                    json={},
                )
            )
            await asyncio.to_thread(entering.wait, 5)
            assert entering.is_set()
            assert not pending.done()
            health = await client.get("/health")
            assert health.status_code == 200
            assert health.json() == {"status": "healthy"}
            assert not pending.done()
            assert not expired.is_set()
            release.set()
            callback = await pending
            assert callback.status_code == 503
            assert callback.json() == {"reason": "callback_processing_unavailable"}

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(_hold_chat_lock, docker, CHAT, held, release, expired)
        assert held.wait(5)
        try:
            asyncio.run(exercise())
        finally:
            release.set()
            holder.result(timeout=5)
            docker._combined_lock = original_lock
    assert not expired.is_set()
    assert _snapshot(data) == before
    assert origin.hits == 0


def test_callback_waits_for_ordinary_process_holder_while_health_progresses(office_world):
    import httpx
    from tests.orchestrator._office_store import _stop_child, _wait_marker

    http, data, origin, docker, _broker, _content, session = _open_session(office_world)
    token = _header_jwt(session)
    before = _snapshot(data)
    entered = data.parent / "callback-process-entered"
    contended = data.parent / "callback-process-contended"
    release_path = data.parent / "callback-process-release"
    env = os.environ.copy()
    pythonpath = env.get("PYTHONPATH", "")
    env.update(
        {
            "PYTHONPATH": str(SERVER_DIR) + (os.pathsep + pythonpath if pythonpath else ""),
            "BASE_DATA_DIR": str(data),
            "OCU_CHAT": CHAT,
            "ENTERED": str(entered),
            "CONTENDED": str(contended),
            "RELEASE": str(release_path),
        }
    )
    holder_source = r'''
import os
import time
from pathlib import Path
import docker_manager

chat = os.environ["OCU_CHAT"]
release = Path(os.environ["RELEASE"])
deadline = time.monotonic() + 8
with docker_manager._combined_lock(chat):
    Path(os.environ["ENTERED"]).write_text("held", encoding="utf-8")
    while not release.exists():
        if time.monotonic() >= deadline:
            break
        time.sleep(0.01)
print("released")
'''
    original_lock = docker._combined_lock
    original_flock = None
    health_progress = threading.Event()
    finished = threading.Event()
    watchdog_released = threading.Event()

    def restore_patches():
        docker._combined_lock = original_lock
        if original_flock is not None:
            import fcntl

            fcntl.flock = original_flock

    def release_holder():
        release_path.write_text("release", encoding="utf-8")

    class AdmissionLock:
        def __init__(self, lock):
            self.lock = lock

        def __enter__(self):
            return self.lock.__enter__()

        def __exit__(self, *args):
            return self.lock.__exit__(*args)

    def instrumented(chat, **kwargs):
        lock = original_lock(chat, **kwargs)
        if kwargs.get("create") is False:
            import fcntl

            nonlocal original_flock
            original_flock = fcntl.flock

            def contend(fd, operation):
                if operation == fcntl.LOCK_EX:
                    try:
                        original_flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        Path(contended).write_text("denied", encoding="utf-8")
                        return original_flock(fd, fcntl.LOCK_EX)
                    original_flock(fd, fcntl.LOCK_UN)
                    Path(contended).write_text("acquired", encoding="utf-8")
                    raise AssertionError("callback lock was granted without process contention")
                return original_flock(fd, operation)

            fcntl.flock = contend
            return AdmissionLock(lock)
        return lock

    docker._combined_lock = instrumented
    holder = None

    def watchdog():
        if health_progress.wait(8) or finished.wait(0):
            return
        watchdog_released.set()
        release_holder()

    watching = threading.Thread(target=watchdog, daemon=True)
    try:
        holder = subprocess.Popen(
            [sys.executable, "-c", holder_source],
            cwd=str(SERVER_DIR),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _wait_marker(entered, holder, "ordinary process holder did not acquire the chat lock")
        watching.start()

        async def exercise():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=http.app),
                base_url="http://testserver",
                trust_env=False,
            ) as client:
                pending = asyncio.create_task(
                    client.post(
                        _callback_path(session),
                        headers={"Authorization": "Bearer " + token},
                        json={},
                    )
                )
                await asyncio.to_thread(_wait_marker, contended, holder, "callback did not observe LOCK_NB denial")
                assert contended.read_text(encoding="utf-8") == "denied"
                assert not pending.done()
                health = await client.get("/health")
                assert health.status_code == 200
                assert health.json() == {"status": "healthy"}
                health_progress.set()
                assert not pending.done()
                release_holder()
                callback = await pending
                assert callback.status_code == 503
                assert callback.json() == {"reason": "callback_processing_unavailable"}

        asyncio.run(exercise())
        stdout, stderr = holder.communicate(timeout=10)
        assert holder.returncode == 0, (stdout, stderr)
        assert not watchdog_released.is_set()
    finally:
        finished.set()
        restore_patches()
        release_holder()
        _stop_child(holder)
        watching.join(timeout=2)
    assert not watchdog_released.is_set()
    assert _snapshot(data) == before
    assert origin.hits == 0

