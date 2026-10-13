# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Ownership contracts of the actual single-file route response."""
from __future__ import annotations

import asyncio
import builtins
import os
import sys
import threading
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

import anyio
import pytest

SERVER_DIR = Path(__file__).resolve().parents[1] / "computer-use-server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

CHAT = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
MAX_READ = 1024 * 1024
WAIT_SECONDS = 5


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", "ocu-response-lifecycle-test-token")
    monkeypatch.setenv("MCP_API_KEY", "ocu-response-lifecycle-mcp-key")
    monkeypatch.setenv("OCU_WEBUI_ORIGIN", "https://webui.example")
    monkeypatch.setenv("OCU_SANDBOX_SUBNET", "10.90.0.0/24")
    monkeypatch.setenv("SINGLE_USER_MODE", "true")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://ocu.example")
    monkeypatch.setenv("BASE_DATA_DIR", str(tmp_path))
    for name in list(sys.modules):
        if name in {
            "app", "auth_guard", "mcp_tools", "docker_manager", "outputs_broker",
            "context_vars", "security", "system_prompt", "skill_manager", "file_response",
            "descriptor_io",
        } or name.startswith("mcp_resources"):
            sys.modules.pop(name, None)
    import app
    import docker_manager
    import uploads

    for module in (app, docker_manager, uploads):
        monkeypatch.setattr(module, "BASE_DATA_DIR", tmp_path)
    output_dir = tmp_path / CHAT / "outputs"
    output_dir.mkdir(parents=True)
    victim = output_dir / "victim.txt"
    victim.write_bytes(b"original lifecycle response bytes")
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_bytes(b"sentinel descriptor remains owned by the test")
    return app, victim, sentinel


class BoundaryFailure(OSError):
    pass


class _ObservedFile:
    """Observe baseline buffered IO while delegating every real read and close."""

    def __init__(self, file, observer):
        self.file = file
        self.observer = observer
        self.fd = file.fileno()

    def __getattr__(self, name):
        return getattr(self.file, name)

    def read(self, size=-1):
        return self.observer.read(self.fd, size, lambda: self.file.read(size))

    def close(self):
        if not self.file.closed:
            self.observer.close(self.fd, self.file.close)

    def __enter__(self):
        self.file.__enter__()
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _FileIO:
    """Track the target inode through both genuine buffered and descriptor IO."""

    def __init__(self, monkeypatch, victim, sentinel):
        self.victim = victim
        item = victim.stat()
        self.identity = (item.st_dev, item.st_ino)
        self.sentinel = sentinel
        self.real_open = builtins.open
        self.real_os_open = os.open
        self.real_read = os.read
        self.real_close = os.close
        self.real_fstat = os.fstat
        self.real_stat = os.stat
        self.real_dup = os.dup
        self.directory_identity = set()
        for directory in victim.parents[:3]:
            item = self.real_stat(directory)
            self.directory_identity.add((item.st_dev, item.st_ino))
        self.directory_acquired = []
        self.directory_closed = []
        self.active_directories = {}
        self.emergency_releases = []
        self.acquired = []
        self.live_files = {}
        self.closed = []
        self.close_attempts = []
        self.read_sizes = []
        self.active_reads = set()
        self.premature_closes = []
        self.read_entered = threading.Event()
        self.release_read = threading.Event()
        self.read_finished = threading.Event()
        self.release_read.set()
        self.block_read = False
        self.fail_read = False
        self.fail_setup = False
        self.fail_setup_ordinal = None
        self.setup_faults = []
        self.read_faults = []
        self.worker_errors = []
        self.setup_seam = None
        self.setup_entered = threading.Event()
        self.release_setup = threading.Event()
        self.setup_boundary_finished = threading.Event()
        self.opener_metadata_finished = threading.Event()
        self.active_setup = set()
        self.setup_entries = []
        self.target_fstats = []
        self.target_stat_results = []
        self.recycle = False
        self.sentinel_fds = []
        self.reused = []
        self.lock = threading.Lock()
        monkeypatch.setattr(builtins, "open", self.open)
        monkeypatch.setattr(os, "open", self.os_open)
        monkeypatch.setattr(os, "read", self.os_read)
        monkeypatch.setattr(os, "close", self.os_close)
        monkeypatch.setattr(os, "fstat", self.fstat)
        monkeypatch.setattr(os, "stat", self.stat)
        monkeypatch.setattr(os, "dup", self.dup)

    def is_target(self, fd):
        item = self.real_fstat(fd)
        return (item.st_dev, item.st_ino) == self.identity

    def acquire(self, fd):
        item = self.real_fstat(fd)
        identity = (item.st_dev, item.st_ino)
        if identity == self.identity:
            self.acquired.append(fd)
            self.live_files[fd] = len(self.acquired)
            return True
        if identity in self.directory_identity:
            self.directory_acquired.append(fd)
            self.active_directories[fd] = identity
        return False

    def open(self, *args, **kwargs):
        file = self.real_open(*args, **kwargs)
        return _ObservedFile(file, self) if self.acquire(file.fileno()) else file

    def os_open(self, *args, **kwargs):
        fd = self.real_os_open(*args, **kwargs)
        if self.acquire(fd) and self.setup_seam == "acquisition":
            with self.setup_worker(fd):
                return fd
        return fd

    def dup(self, fd):
        duplicate = self.real_dup(fd)
        self.acquire(duplicate)
        return duplicate

    @contextmanager
    def setup_worker(self, fd):
        with self.lock:
            self.active_setup.add(fd)
            self.setup_entries.append((self.setup_seam, fd, len(self.target_fstats)))
        self.setup_entered.set()
        try:
            if not self.release_setup.wait(WAIT_SECONDS):
                self.emergency_releases.append(("setup", fd))
                self.release_setup.set()
            yield
        except BaseException as exc:
            self.worker_errors.append(exc)
            raise
        finally:
            with self.lock:
                self.active_setup.remove(fd)
            self.setup_boundary_finished.set()

    def fstat(self, fd):
        target = fd in self.live_files
        if target:
            self.target_fstats.append(fd)
        ordinal = len(self.target_fstats)
        if target and self.setup_seam == "response-fstat" and ordinal == 2:
            with self.setup_worker(fd):
                item = self.real_fstat(fd)
                self.target_stat_results.append((item.st_dev, item.st_ino))
        else:
            try:
                item = self.real_fstat(fd)
                if target:
                    self.target_stat_results.append((item.st_dev, item.st_ino))
            finally:
                if target and ordinal == 1:
                    self.opener_metadata_finished.set()
        if (self.fail_setup and (item.st_dev, item.st_ino) == self.identity
                and (self.fail_setup_ordinal is None or ordinal == self.fail_setup_ordinal)):
            self.setup_faults.append("fstat")
            raise BoundaryFailure("injected descriptor metadata failure")
        return item

    def stat(self, path, *args, **kwargs):
        if self.fail_setup and not isinstance(path, int) and os.fspath(path) == str(self.victim):
            self.setup_faults.append("stat")
            raise BoundaryFailure("injected pathname metadata failure")
        return self.real_stat(path, *args, **kwargs)

    def read(self, fd, size, operation):
        with self.lock:
            self.read_sizes.append(size)
            self.active_reads.add(fd)
        self.read_entered.set()
        try:
            if self.block_read and not self.release_read.wait(WAIT_SECONDS):
                self.emergency_releases.append(fd)
                self.release_read.set()
            if self.fail_read:
                self.read_faults.append(fd)
                raise BoundaryFailure("injected file read failure")
            try:
                return operation()
            except BaseException as exc:
                self.worker_errors.append(exc)
                raise
        finally:
            with self.lock:
                self.active_reads.remove(fd)
            self.read_finished.set()

    def os_read(self, fd, size):
        if fd in self.live_files:
            return self.read(fd, size, lambda: self.real_read(fd, size))
        return self.real_read(fd, size)

    def close(self, fd, operation):
        with self.lock:
            self.close_attempts.append(fd)
            if fd in self.active_reads or fd in self.active_setup:
                self.premature_closes.append(fd)
        operation()
        self.closed.append(fd)
        self.live_files.pop(fd, None)
        if self.recycle:
            # Fill vacant slots with owned descriptors, without overwriting live FDs.
            for _ in range(64):
                sentinel_fd = self.real_os_open(self.sentinel, os.O_RDONLY)
                self.sentinel_fds.append(sentinel_fd)
                if sentinel_fd == fd:
                    self.reused.append(fd)
                    break

    def os_close(self, fd):
        if fd in self.active_directories:
            self.real_close(fd)
            del self.active_directories[fd]
            self.directory_closed.append(fd)
            return
        if fd in self.live_files:
            return self.close(fd, lambda: self.real_close(fd))
        if fd in self.reused:
            # This is the sentinel generation, not an unrelated recycled runtime FD.
            self.close_attempts.append(fd)
        return self.real_close(fd)

    def assert_closed(self):
        assert self.close_attempts == self.acquired, "Owned file descriptors must close exactly once"
        assert self.closed == self.acquired
        assert not self.premature_closes, "File descriptor closed while a worker still owned its read"
        assert not self.worker_errors, "Real worker IO failed after premature descriptor release"
        assert not self.emergency_releases, "Real read required the thread-side emergency release"
        assert Counter(self.directory_closed) == Counter(self.directory_acquired)
        assert not self.active_directories, "Workspace directory descriptor leaked"
        assert not self.live_files, "Owned file descriptor generation leaked"
        for fd in self.acquired:
            if fd not in self.sentinel_fds:
                try:
                    item = self.real_fstat(fd)
                except OSError:
                    continue
                assert (item.st_dev, item.st_ino) != self.identity

    def assert_reused_descriptor_survives(self):
        assert self.reused == self.acquired, "Harness did not exercise actual descriptor reuse"
        for fd in self.reused:
            assert self.real_read(fd, 128) == b"sentinel descriptor remains owned by the test"

    def cleanup(self):
        self.release_read.set()
        self.release_setup.set()
        sentinel_stat = self.real_stat(self.sentinel)
        sentinel_identity = (sentinel_stat.st_dev, sentinel_stat.st_ino)
        for fd in self.sentinel_fds:
            try:
                item = self.real_fstat(fd)
            except OSError:
                continue
            if (item.st_dev, item.st_ino) == sentinel_identity:
                self.real_close(fd)
        for fd in self.live_files:
            try:
                if self.is_target(fd):
                    self.real_close(fd)
            except OSError:
                pass
        for fd, identity in list(self.active_directories.items()):
            try:
                item = self.real_fstat(fd)
                if (item.st_dev, item.st_ino) == identity:
                    self.real_close(fd)
            except OSError:
                pass


@pytest.fixture
def file_io(workspace, monkeypatch):
    _, victim, sentinel = workspace
    observer = _FileIO(monkeypatch, victim, sentinel)
    try:
        yield observer
    finally:
        observer.cleanup()


def _scope(method="GET"):
    return {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "method": method, "scheme": "http",
        "path": f"/files/{CHAT}/victim.txt", "raw_path": b"/files/victim.txt",
        "query_string": b"", "headers": [], "root_path": "",
        "server": ("testserver", 80), "client": ("127.0.0.1", 12345),
    }


async def _receive_until_cancelled():
    await anyio.sleep_forever()


async def _response(workspace, download):
    app, _, _ = workspace
    return await app.download_file(CHAT, "victim.txt", download=1 if download else None)


@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_unexecuted_file_response_does_not_acquire_a_descriptor(workspace, file_io, download):
    response = anyio.run(_response, workspace, download)
    assert file_io.acquired == []
    assert file_io.directory_acquired == []
    assert file_io.read_sizes == []
    assert response.status_code == 200


@contextmanager
def _expect_boundary_failure(fragment):
    try:
        yield
    except Exception as exc:
        pending = [exc]
        leaves = []
        while pending:
            error = pending.pop()
            if isinstance(error, BaseExceptionGroup):
                pending.extend(error.exceptions)
            else:
                leaves.append(error)
        assert leaves and all(
            isinstance(error, BoundaryFailure) and fragment in str(error)
            for error in leaves
        ), f"Unexpected response failure: {exc!r}"
    else:
        pytest.fail("The injected system-boundary failure was suppressed")


@pytest.mark.parametrize("empty", (True, False), ids=("empty", "multi-chunk"))
@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_completed_file_response_bounds_reads_and_closes_its_descriptor(
    workspace, file_io, empty, download
):
    _, victim, _ = workspace
    contents = b"" if empty else b"0123456789abcdef" * (128 * 1024) + b"tail"
    victim.write_bytes(contents)
    messages = []

    async def scenario():
        response = await _response(workspace, download)
        async def send(message):
            messages.append(message)
        with anyio.fail_after(WAIT_SECONDS):
            await response(_scope(), _receive_until_cancelled, send)

    anyio.run(scenario)
    assert [message["status"] for message in messages if message["type"] == "http.response.start"] == [200]
    bodies = [message for message in messages if message["type"] == "http.response.body"]
    assert b"".join(message.get("body", b"") for message in bodies) == contents
    assert bodies[-1].get("more_body", False) is False
    assert len(file_io.acquired) == 1
    assert file_io.read_sizes and all(0 < size <= MAX_READ for size in file_io.read_sizes)
    file_io.assert_closed()


@pytest.mark.parametrize("empty", (True, False), ids=("empty", "multi-chunk"))
@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_completed_head_fstats_without_reads_and_closes_after_terminal_send(
    workspace, file_io, empty, download
):
    _, victim, _ = workspace
    victim.write_bytes(b"" if empty else b"0123456789abcdef" * (128 * 1024) + b"tail")
    os.utime(victim, (1700000000, 1700000000))
    file_io.fail_read = True
    file_io.recycle = True
    messages = []
    send_states = []

    async def scenario():
        response = await _response(workspace, download)
        async def send(message):
            messages.append(message)
            send_states.append((message["type"], list(file_io.closed), list(file_io.live_files)))
        with anyio.fail_after(WAIT_SECONDS):
            await response(_scope("HEAD"), _receive_until_cancelled, send)

    anyio.run(scenario)
    assert len(file_io.acquired) == 1
    fd = file_io.acquired[0]
    assert file_io.target_fstats == [fd, fd]
    assert all(identity == file_io.identity for identity in file_io.target_stat_results)
    assert file_io.read_sizes == []
    assert file_io.read_faults == []
    assert [message["status"] for message in messages if message["type"] == "http.response.start"] == [200]
    bodies = [message for message in messages if message["type"] == "http.response.body"]
    assert bodies == [{"type": "http.response.body", "body": b"", "more_body": False}]
    assert send_states == [
        ("http.response.start", [], [fd]), ("http.response.body", [], [fd]),
    ]
    start = next(message for message in messages if message["type"] == "http.response.start")
    headers = dict(start["headers"])
    assert headers[b"content-length"] == (b"0" if empty else b"2097156")
    assert headers[b"etag"] == (
        b'"e7d53fbff452d442c477af2510b0e0b0"' if empty
        else b'"69c1476782e5783776d96540a5a7d719"'
    )
    assert headers[b"last-modified"] == b"Tue, 14 Nov 2023 22:13:20 GMT"
    file_io.assert_closed()
    file_io.assert_reused_descriptor_survives()


@pytest.mark.parametrize("metadata_at", (1, 2), ids=("validation", "response"))
@pytest.mark.parametrize("method", ("GET", "HEAD"))
@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_file_response_metadata_failure_closes_any_acquired_descriptor(
    workspace, file_io, download, method, metadata_at
):
    messages = []
    file_io.fail_setup_ordinal = metadata_at
    file_io.recycle = True

    async def scenario():
        response = await _response(workspace, download)
        file_io.fail_setup = True
        async def send(message):
            messages.append(message)
        with _expect_boundary_failure("metadata failure"), anyio.fail_after(WAIT_SECONDS):
            await response(_scope(method), _receive_until_cancelled, send)

    anyio.run(scenario)
    assert file_io.setup_faults, "No genuine metadata boundary was faulted"
    assert messages == []
    file_io.assert_closed()
    assert len(file_io.acquired) == 1
    file_io.assert_reused_descriptor_survives()
    if method == "HEAD":
        assert file_io.read_sizes == []


@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_file_response_read_failure_closes_without_a_second_response(workspace, file_io, download):
    file_io.fail_read = True
    file_io.recycle = True
    messages = []

    async def scenario():
        response = await _response(workspace, download)
        async def send(message):
            messages.append(message)
        with _expect_boundary_failure("file read failure"), anyio.fail_after(WAIT_SECONDS):
            await response(_scope(), _receive_until_cancelled, send)

    anyio.run(scenario)
    assert len(file_io.acquired) == 1
    assert file_io.read_faults == file_io.acquired
    assert [message["status"] for message in messages if message["type"] == "http.response.start"] == [200]
    assert not any(message.get("body") for message in messages)
    file_io.assert_closed()
    file_io.assert_reused_descriptor_survives()


@pytest.mark.parametrize("method", ("GET", "HEAD"))
@pytest.mark.parametrize("failure_at", ("start", "body"))
@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_file_response_send_failure_releases_ownership(
    workspace, file_io, failure_at, download, method
):
    file_io.recycle = True
    messages = []
    failures = []

    async def scenario():
        response = await _response(workspace, download)
        async def send(message):
            messages.append(message)
            if message["type"] == f"http.response.{failure_at}":
                failures.append(message["type"])
                raise BoundaryFailure("injected ASGI send failure")
        with _expect_boundary_failure("ASGI send failure"), anyio.fail_after(WAIT_SECONDS):
            await response(_scope(method), _receive_until_cancelled, send)

    anyio.run(scenario)
    assert failures == [f"http.response.{failure_at}"]
    assert sum(message["type"] == "http.response.start" for message in messages) == 1
    if failure_at == "body":
        assert len(file_io.acquired) == 1
    file_io.assert_closed()
    if file_io.acquired:
        file_io.assert_reused_descriptor_survives()
    if method == "HEAD":
        assert file_io.read_sizes == []
        assert not any(message.get("body") for message in messages)


@pytest.mark.parametrize("method", ("GET", "HEAD"))
@pytest.mark.parametrize("termination", ("disconnect", "cancel"))
@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_file_response_termination_before_first_body_releases_ownership(
    workspace, file_io, termination, download, method
):
    messages = []
    file_io.recycle = True

    async def scenario():
        response = await _response(workspace, download)
        async def receive():
            return {"type": "http.disconnect"}
        with anyio.CancelScope() as scope:
            async def send(message):
                messages.append(message)
                if termination == "cancel" and message["type"] == "http.response.start":
                    scope.cancel()
                    await anyio.lowlevel.checkpoint()
            with anyio.fail_after(WAIT_SECONDS):
                await response(_scope(method), receive, send)

    anyio.run(scenario)
    assert not any(message.get("body") for message in messages), "Body published after termination"
    assert sum(message["type"] == "http.response.start" for message in messages) <= 1
    file_io.assert_closed()
    if file_io.acquired:
        file_io.assert_reused_descriptor_survives()
    if method == "HEAD":
        assert file_io.read_sizes == []


@pytest.mark.parametrize("termination", ("disconnect", "cancel"))
@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_file_response_termination_waits_for_real_read_before_close(
    workspace, file_io, termination, download
):
    file_io.block_read = True
    file_io.release_read.clear()
    file_io.recycle = True
    messages = []
    states_while_blocked = []

    async def scenario():
        response = await _response(workspace, download)
        disconnected = anyio.Event()
        finished = anyio.Event()
        scope_ready = anyio.Event()
        scopes = []
        async def receive():
            await disconnected.wait()
            return {"type": "http.disconnect"}
        async def send(message):
            messages.append(message)
        async def run_response():
            try:
                with anyio.CancelScope() as scope:
                    scopes.append(scope)
                    scope_ready.set()
                    await response(_scope(), receive, send)
            finally:
                finished.set()
        async with anyio.create_task_group() as group:
            group.start_soon(run_response)
            try:
                with anyio.fail_after(WAIT_SECONDS):
                    await scope_ready.wait()
                    entered = await anyio.to_thread.run_sync(file_io.read_entered.wait, WAIT_SECONDS)
                    assert entered, "The genuine buffered/descriptor read never started"
                    if termination == "cancel":
                        scopes[0].cancel()
                    else:
                        disconnected.set()
                    # Give incorrect abandoned-worker cleanup a bounded opportunity to run.
                    with anyio.move_on_after(0.25):
                        await finished.wait()
                    states_while_blocked.append((finished.is_set(), list(file_io.closed)))
            finally:
                file_io.release_read.set()
                with anyio.fail_after(WAIT_SECONDS):
                    await finished.wait()

    anyio.run(scenario)
    assert states_while_blocked == [(False, [])], "Response completed/closed while its real read was pending"
    assert file_io.read_finished.is_set()
    assert not any(message.get("body") for message in messages), "Body published after termination"
    assert len(file_io.acquired) == 1
    file_io.assert_closed()
    file_io.assert_reused_descriptor_survives()


async def _wait_for_observation(predicate):
    deadline = asyncio.get_running_loop().time() + WAIT_SECONDS
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.001)
    return True


async def _native_setup_cancellation(workspace, file_io, download, full_app, method="GET"):
    messages = []
    evidence = {"entrypoint": "full-app" if full_app else "response"}

    async def send(message):
        messages.append(message)

    if full_app:
        app, _, _ = workspace
        scope = _scope(method)
        scope["raw_path"] = scope["path"].encode("ascii")
        scope["query_string"] = b"download=1" if download else b""
        scope["headers"] = [
            (b"authorization", b"Bearer ocu-response-lifecycle-test-token"),
        ]
        request_delivered = False

        async def receive():
            nonlocal request_delivered
            if not request_delivered:
                request_delivered = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await _receive_until_cancelled()

        task = asyncio.create_task(app.app(scope, receive, send))
    else:
        response = await _response(workspace, download)
        task = asyncio.create_task(response(_scope(method), _receive_until_cancelled, send))

    try:
        evidence["entered"] = await _wait_for_observation(file_io.setup_entered.is_set)
        if evidence["entered"]:
            evidence["cancel_requested"] = task.cancel()
            # asyncio.wait observes completion without a second cancellation.
            await asyncio.wait({task}, timeout=0.25)
            evidence["while_blocked"] = {
                "done": task.done(),
                "closed": list(file_io.closed),
                "close_attempts": list(file_io.close_attempts),
                "active_setup": list(file_io.active_setup),
                "boundary_finished": file_io.setup_boundary_finished.is_set(),
                "released": file_io.release_setup.is_set(),
            }
    finally:
        # Never let the expected semantic RED strand a real worker or owned FD.
        file_io.release_setup.set()
        done, _ = await asyncio.wait({task}, timeout=WAIT_SECONDS)
        evidence["drained"] = bool(done)
        if not done:
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=WAIT_SECONDS)
            evidence["drained_after_emergency_cancel"] = bool(done)
        evidence["setup_io_drained"] = await _wait_for_observation(
            lambda: (
                file_io.setup_boundary_finished.is_set()
                and file_io.opener_metadata_finished.is_set()
                and not file_io.active_directories
                and len(file_io.directory_closed) == len(file_io.directory_acquired)
            )
        )
        if task.done():
            try:
                task.result()
            except asyncio.CancelledError:
                evidence["outcome"] = "cancelled"
            except BaseException as exc:
                evidence["outcome"] = (type(exc).__name__, str(exc))
            else:
                evidence["outcome"] = "returned"
        else:
            evidence["outcome"] = "pending"

    if full_app:
        middleware = app.app.middleware_stack
        evidence["middleware"] = []
        while middleware is not None:
            evidence["middleware"].append(type(middleware).__name__)
            middleware = getattr(middleware, "app", None)
    return messages, evidence


def _assert_native_setup_ownership(file_io, messages, evidence, seam, record_property):
    sentinel_results = []
    for fd in file_io.reused:
        try:
            sentinel_results.append((fd, os.pread(fd, 128, 0)))
        except OSError as exc:
            sentinel_results.append((fd, repr(exc)))
    evidence.update({
        "setup_entries": list(file_io.setup_entries),
        "target_fstats": list(file_io.target_fstats),
        "stat_identities": list(file_io.target_stat_results),
        "acquired": list(file_io.acquired),
        "closed": list(file_io.closed),
        "close_attempts": list(file_io.close_attempts),
        "premature_closes": list(file_io.premature_closes),
        "live_files": list(file_io.live_files),
        "reused": list(file_io.reused),
        "emergency_releases": list(file_io.emergency_releases),
        "sentinel_results": sentinel_results,
        "opener_metadata_finished": file_io.opener_metadata_finished.is_set(),
        "directory_acquired": list(file_io.directory_acquired),
        "directory_closed": list(file_io.directory_closed),
        "active_directories": list(file_io.active_directories),
        "worker_errors": [repr(error) for error in file_io.worker_errors],
        "body_messages": [message for message in messages if message["type"] == "http.response.body"],
    })
    record_property("native_setup_cancellation", repr(evidence))
    diagnostic = f"Native setup cancellation evidence: {evidence!r}"
    assert evidence["entered"], diagnostic
    assert len(file_io.acquired) == 1, diagnostic
    fd = file_io.acquired[0]
    # Ordinal zero means the genuine leaf open has not delivered its integer;
    # ordinal two is the response metadata call, after the opener's real fstat.
    ordinal = 0 if seam == "acquisition" else 2
    assert file_io.setup_entries == [(seam, fd, ordinal)], diagnostic
    assert evidence["cancel_requested"], diagnostic
    blocked = evidence["while_blocked"]
    assert blocked["active_setup"] == [fd], diagnostic
    assert not blocked["boundary_finished"] and not blocked["released"], diagnostic
    assert not blocked["done"] and not blocked["close_attempts"] and not blocked["closed"], (
        "Response completed/closed while its genuine setup worker was pending. " + diagnostic
    )
    assert evidence["drained"] and evidence["setup_io_drained"], diagnostic
    assert evidence["outcome"] == "cancelled", diagnostic
    assert not evidence["body_messages"], diagnostic
    assert file_io.target_stat_results and all(
        identity == file_io.identity for identity in file_io.target_stat_results
    ), diagnostic
    file_io.assert_closed()
    file_io.assert_reused_descriptor_survives()


@pytest.mark.parametrize("method", ("GET", "HEAD"))
@pytest.mark.parametrize("seam", ("acquisition", "response-fstat"))
@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_native_file_response_cancellation_waits_for_setup_ownership(
    workspace, file_io, download, seam, record_property, method
):
    file_io.setup_seam = seam
    file_io.recycle = True
    messages, evidence = anyio.run(
        _native_setup_cancellation, workspace, file_io, download, False, method
    )
    _assert_native_setup_ownership(file_io, messages, evidence, seam, record_property)
    if method == "HEAD":
        assert file_io.read_sizes == []


@pytest.mark.parametrize("method", ("GET", "HEAD"))
@pytest.mark.parametrize("seam", ("acquisition", "response-fstat"))
@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_native_full_app_cancellation_waits_for_setup_ownership(
    workspace, file_io, download, seam, record_property, method
):
    file_io.setup_seam = seam
    file_io.recycle = True
    messages, evidence = anyio.run(
        _native_setup_cancellation, workspace, file_io, download, True, method
    )
    _assert_native_setup_ownership(file_io, messages, evidence, seam, record_property)
    if method == "HEAD":
        assert file_io.read_sizes == []


@pytest.mark.parametrize("method", ("GET", "HEAD"))
@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
@pytest.mark.parametrize(
    "target_kind, expected_status",
    (
        ("missing", 404), ("directory", 400), ("fifo", 400),
        ("outside", 403), ("sibling-leaf", 403), ("private-alias", 403),
        ("private-traversal", 403), ("chat-root", 404), ("output-root", 404),
        ("unauthorized", 401),
    ),
)
def test_refused_file_requests_disclose_no_target_metadata_and_release_descriptors(
    workspace, file_io, tmp_path, method, download, target_kind, expected_status
):
    from fastapi.testclient import TestClient

    app, victim, _ = workspace
    outputs = victim.parent
    filename = "refused.txt"
    canary = b"benign private refusal canary with distinct size"
    selected = None
    if target_kind == "directory":
        selected = outputs / filename
        selected.mkdir()
    elif target_kind == "fifo":
        selected = outputs / filename
        os.mkfifo(selected)
    elif target_kind in ("outside", "sibling-leaf", "private-alias", "private-traversal"):
        directory = {
            "outside": tmp_path / "outside",
            "sibling-leaf": tmp_path / "sibling-chat" / "outputs",
            "private-alias": outputs.parent / ".ocu",
            "private-traversal": outputs.parent / ".ocu",
        }[target_kind]
        directory.mkdir(parents=True)
        selected = directory / filename
        selected.write_bytes(canary)
        os.utime(selected, (1800000000, 1800000000))
        if target_kind == "private-traversal":
            filename = "%2E%2E%2F.ocu%2Frefused.txt"
        else:
            (outputs / filename).symlink_to(selected)
    elif target_kind in ("chat-root", "output-root"):
        sibling_chat = tmp_path / "sibling-chat"
        sibling_outputs = sibling_chat / "outputs"
        sibling_outputs.mkdir(parents=True)
        selected = sibling_outputs / filename
        selected.write_bytes(canary)
        root = outputs.parent if target_kind == "chat-root" else outputs
        redirect = sibling_chat if target_kind == "chat-root" else sibling_outputs
        root.rename(tmp_path / "parked-root")
        root.symlink_to(redirect, target_is_directory=True)
    elif target_kind == "unauthorized":
        filename = victim.name
        selected = victim
    if selected is not None:
        item = file_io.real_stat(selected)
        file_io.identity = (item.st_dev, item.st_ino)
    file_io.fail_read = True
    headers = (
        {} if target_kind == "unauthorized"
        else {"Authorization": "Bearer ocu-response-lifecycle-test-token"}
    )

    with TestClient(app.app) as http:
        response = http.request(
            method, f"/files/{CHAT}/{filename}", headers=headers,
            params={"download": 1} if download else None,
        )

    assert response.status_code == expected_status
    assert canary not in response.content
    if method == "HEAD":
        assert response.content == b""
    for header in ("etag", "last-modified", "content-disposition",
                   "content-security-policy", "x-content-type-options", "cache-control"):
        assert header not in response.headers
    if target_kind == "unauthorized":
        assert response.headers.get_list("www-authenticate") == ["Bearer"]
    assert file_io.read_sizes == []
    file_io.assert_closed()
