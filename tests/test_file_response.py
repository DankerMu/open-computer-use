# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Ownership contracts of the actual single-file route response."""
from __future__ import annotations

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
        self.setup_faults = []
        self.read_faults = []
        self.worker_errors = []
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
        self.acquire(fd)
        return fd

    def dup(self, fd):
        duplicate = self.real_dup(fd)
        self.acquire(duplicate)
        return duplicate

    def fstat(self, fd):
        item = self.real_fstat(fd)
        if self.fail_setup and (item.st_dev, item.st_ino) == self.identity:
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
            if fd in self.active_reads:
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


def _scope():
    return {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "method": "GET", "scheme": "http",
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


@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_file_response_metadata_failure_closes_any_acquired_descriptor(workspace, file_io, download):
    messages = []

    async def scenario():
        response = await _response(workspace, download)
        file_io.fail_setup = True
        async def send(message):
            messages.append(message)
        with _expect_boundary_failure("metadata failure"), anyio.fail_after(WAIT_SECONDS):
            await response(_scope(), _receive_until_cancelled, send)

    anyio.run(scenario)
    assert file_io.setup_faults, "No genuine metadata boundary was faulted"
    assert messages == []
    file_io.assert_closed()


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


@pytest.mark.parametrize("failure_at", ("start", "body"))
@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_file_response_send_failure_releases_ownership(workspace, file_io, failure_at, download):
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
            await response(_scope(), _receive_until_cancelled, send)

    anyio.run(scenario)
    assert failures == [f"http.response.{failure_at}"]
    assert sum(message["type"] == "http.response.start" for message in messages) == 1
    if failure_at == "body":
        assert len(file_io.acquired) == 1
    file_io.assert_closed()
    if file_io.acquired:
        file_io.assert_reused_descriptor_survives()


@pytest.mark.parametrize("termination", ("disconnect", "cancel"))
@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_file_response_termination_before_first_body_releases_ownership(
    workspace, file_io, termination, download
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
                await response(_scope(), receive, send)

    anyio.run(scenario)
    assert not any(message.get("body") for message in messages), "Body published after termination"
    assert sum(message["type"] == "http.response.start" for message in messages) <= 1
    file_io.assert_closed()
    if file_io.acquired:
        file_io.assert_reused_descriptor_survives()


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
