# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Session-bound CDP/ttyd authorization re-check and revocation.

Public seams are the two existing WebSocket routes plus process-level
startup_preflight. Docker, WebUI, and wall-clock sleep are faked; the
supervisor under test is not.
"""

from __future__ import annotations

import asyncio
import importlib
import io
import os
import sys
import threading
import time

_WALL_MONOTONIC = time.monotonic
from pathlib import Path
from urllib.parse import urlparse

import pytest

SERVER_DIR = Path(__file__).resolve().parents[2] / "computer-use-server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

INTERNAL = "ocu-test-internal-token"
MCP_KEY = "ocu-test-mcp-api-key"
ORIGIN = "https://webui.example"
SUBNET = "10.90.0.0/24"
CHAT = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
CHAT_B = "b2c3d4e5-f6a7-8901-bcde-f12345678901"
TOKEN_A = "session-token-chat-a"
TOKEN_B = "session-token-chat-b"
COOKIE_A = f"token={TOKEN_A}"
COOKIE_B = f"token={TOKEN_B}"
AUTH_PATH = "/api/v1/ocu/auth"
RECHECK_INTERVAL = 30.0
AUTH_DEADLINE = 5.0
REVOKE_CODE = 4401
BACKEND_FAIL_CODE = 1011
PREACCEPT_CODE = 1008
FUTURE_MARKER = "post-revoke-marker"


def _apply_env(monkeypatch, tmp_path, auth_url=None):
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL)
    monkeypatch.setenv("MCP_API_KEY", MCP_KEY)
    monkeypatch.setenv("OCU_WEBUI_ORIGIN", ORIGIN)
    monkeypatch.setenv("OCU_SANDBOX_SUBNET", SUBNET)
    monkeypatch.setenv("SINGLE_USER_MODE", "true")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://ocu.example")
    monkeypatch.setenv("BASE_DATA_DIR", str(tmp_path / "data"))
    if auth_url is None:
        monkeypatch.delenv("OCU_WEBUI_AUTH_URL", raising=False)
    else:
        monkeypatch.setenv("OCU_WEBUI_AUTH_URL", auth_url)


def _reload_server():
    for name in list(sys.modules):
        if name in {
            "app",
            "auth_guard",
            "ws_recheck",
            "mcp_tools",
            "docker_manager",
            "outputs_broker",
            "context_vars",
            "security",
            "system_prompt",
            "skill_manager",
        } or name.startswith(("mcp_resources", "cli_adapters")):
            sys.modules.pop(name, None)
    return importlib.import_module("app")


@pytest.fixture
def app_module(monkeypatch, tmp_path):
    _apply_env(monkeypatch, tmp_path, auth_url="http://127.0.0.1:9" + AUTH_PATH)
    loaded = _reload_server()
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(loaded, "BASE_DATA_DIR", data)
    import docker_manager

    monkeypatch.setattr(docker_manager, "BASE_DATA_DIR", data)
    monkeypatch.setattr(loaded, "startup_idle_sweep", lambda now=None: None)
    monkeypatch.setattr(loaded, "reap_known_sandboxes", lambda now=None: None)
    monkeypatch.setattr(loaded, "validate_idle_configuration", lambda *a, **k: (600, 30))
    return loaded


def _client(app_module):
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)


def _bearer(**extra):
    headers = {"Authorization": f"Bearer {INTERNAL}"}
    headers.update(extra)
    return headers


def _cookie_headers(cookie=COOKIE_A, **extra):
    return _bearer(Cookie=cookie, **extra)


def _ws_connect(client, kind, chat_id=CHAT, headers=None):
    path = (
        f"/browser/{chat_id}/devtools/page/page-1"
        if kind == "cdp"
        else f"/terminal/{chat_id}/ws"
    )
    kwargs = {"headers": headers or _cookie_headers()}
    if kind == "ttyd":
        kwargs["subprotocols"] = ["tty"]
    return client.websocket_connect(path, **kwargs)


def _ws_scope(kind, chat_id=CHAT, headers=None):
    path = (
        f"/browser/{chat_id}/devtools/page/page-1"
        if kind == "cdp"
        else f"/terminal/{chat_id}/ws"
    )
    header_map = headers or _cookie_headers()
    raw = [
        (name.lower().encode("latin-1"), str(value).encode("latin-1"))
        for name, value in header_map.items()
    ]
    return {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": raw,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "subprotocols": ["tty"] if kind == "ttyd" else [],
        "extensions": {},
        "root_path": "",
    }


def _run_ws_route(app, kind, chat_id=CHAT, headers=None, timeout=8.0):
    """Drive the real ASGI websocket route without TestClient lifespan join.

    Denial is observed from the close message independently of lookup threads.
    """
    messages = []
    opened = threading.Event()

    async def receive():
        if not opened.is_set():
            opened.set()
            return {"type": "websocket.connect"}
        await asyncio.Event().wait()
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message):
        messages.append(message)

    async def drive():
        await app(_ws_scope(kind, chat_id, headers), receive, send)

    box = {}

    def runner():
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(asyncio.wait_for(drive(), timeout=timeout))
        except Exception as exc:
            box["error"] = exc
        finally:
            loop.close()

    thread = threading.Thread(target=runner)
    thread.start()
    thread.join(timeout + 1)
    if thread.is_alive():
        raise AssertionError("ASGI websocket route exceeded bounded harness timeout")
    close = next((item for item in messages if item.get("type") == "websocket.close"), None)
    return close, box.get("error"), messages


class RecordingClock:
    def __init__(self, start=1_000.0):
        self.now = start
        self.sleeps = []
        self._lock = threading.Lock()
        self._loop = None
        self._waiters = []

    def monotonic(self):
        return self.now

    def bind(self, loop):
        self._loop = loop

    def advance(self, seconds):
        with self._lock:
            self.now += seconds
            due = self.now
            waiters = list(self._waiters)
            self._waiters = []
        loop = self._loop
        for wake_at, event in waiters:
            if due >= wake_at:
                if loop is not None:
                    loop.call_soon_threadsafe(event.set)
                else:
                    event.set()
            else:
                with self._lock:
                    self._waiters.append((wake_at, event))
        return self.now

    async def sleep(self, seconds):
        loop = asyncio.get_running_loop()
        self.bind(loop)
        event = asyncio.Event()
        with self._lock:
            self.sleeps.append(seconds)
            wake_at = self.now + seconds
            if self.now >= wake_at:
                return
            self._waiters.append((wake_at, event))
        await event.wait()


class ScriptedAuth:
    def __init__(self, statuses, *, delay=0.0, error=None, hang=False, by_chat=None):
        self.statuses = list(statuses)
        self.by_chat = {key: list(value) for key, value in (by_chat or {}).items()}
        self.delay = delay
        self.error = error
        self.hang = hang
        self.calls = []
        self.sessions = []
        self._lock = threading.Lock()
        self.hold_decision = None
        self.loop = None
        self.periodic_entered = threading.Event()
        self.decision_made = threading.Event()

    def bind(self, loop):
        self.loop = loop
        if self.hold_decision is None:
            self.hold_decision = asyncio.Event()
            self.hold_decision.set()

    def pause_decisions(self):
        if self.loop is None or self.hold_decision is None:
            raise RuntimeError("auth loop not bound")
        self.loop.call_soon_threadsafe(self.hold_decision.clear)

    def resume_decisions(self):
        if self.loop is None or self.hold_decision is None:
            raise RuntimeError("auth loop not bound")
        self.loop.call_soon_threadsafe(self.hold_decision.set)

    def next_status(self, headers=None):
        headers = {str(name).lower(): value for name, value in (headers or {}).items()}
        chat = headers.get("x-chat-id")
        with self._lock:
            if chat in self.by_chat:
                sequence = self.by_chat[chat]
                if not sequence:
                    return 200
                if len(sequence) == 1:
                    return sequence[0]
                return sequence.pop(0)
            if not self.statuses:
                return 200
            if len(self.statuses) == 1:
                return self.statuses[0]
            return self.statuses.pop(0)

    def record(self, request):
        with self._lock:
            self.calls.append(request)
            if len(self.calls) > 1:
                self.periodic_entered.set()

    def pair_counts(self):
        counts = {}
        for call in self.calls:
            headers = {str(name).lower(): value for name, value in call["headers"].items()}
            pair = (headers.get("cookie"), headers.get("x-chat-id"))
            counts[pair] = counts.get(pair, 0) + 1
        return counts


class FakeAuthResponse:
    def __init__(self, status):
        self.status = status

    async def read(self):
        raise AssertionError("auth response body must not be consumed")

    async def text(self):
        raise AssertionError("auth response body must not be consumed")

    async def json(self):
        raise AssertionError("auth response body must not be consumed")

    async def release(self):
        return None


class FakeAuthGet:
    def __init__(self, owner, request, status, error, hang, delay):
        self._owner = owner
        self._request = request
        self._status = status
        self._error = error
        self._hang = hang
        self._delay = delay
        self._released = False

    async def __aenter__(self):
        loop = asyncio.get_running_loop()
        self._owner.bind(loop)
        self._owner.record(self._request)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._owner.hold_decision is not None:
            await self._owner.hold_decision.wait()
        if self._hang:
            timeout = self._request.get("session_kwargs", {}).get("timeout")
            total = getattr(timeout, "total", None)
            if total is None:
                await asyncio.Event().wait()
            else:
                try:
                    await asyncio.wait_for(asyncio.Event().wait(), timeout=total)
                except TimeoutError as timeout_exc:
                    raise TimeoutError("auth deadline") from timeout_exc
        if self._error is not None:
            raise self._error
        self._owner.decision_made.set()
        return FakeAuthResponse(self._status)

    async def __aexit__(self, *_exc):
        self._released = True
        return False


class FakeAuthSession:
    def __init__(self, script, **kwargs):
        self.script = script
        self.kwargs = kwargs
        self.requests = []
        self.closed = False
        self.close_entered = threading.Event()
        script.sessions.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        await self.close()
        return False

    async def close(self):
        self.close_entered.set()
        if getattr(self.script, "session_close_hang", False):
            await asyncio.Event().wait()
        self.closed = True

    def get(self, url, *, headers=None, allow_redirects=None, **_kwargs):
        request = {
            "url": url,
            "headers": dict(headers or {}),
            "allow_redirects": allow_redirects,
            "session_kwargs": dict(self.kwargs),
        }
        self.requests.append(request)
        return FakeAuthGet(
            self.script,
            request,
            self.script.next_status(headers),
            self.script.error,
            self.script.hang,
            self.script.delay,
        )


class FakeBackendMessage:
    def __init__(self, kind, data=None):
        self.type = kind
        self.data = data


class FakeBackendWs:
    def __init__(self, owner, url, kwargs):
        self.owner = owner
        self.url = url
        self.kwargs = kwargs
        self.incoming = asyncio.Queue()
        self.sent = []
        self.closed = None
        self.close_started = threading.Event()
        self.close_delay = 0.0
        self.fail_enter = owner.fail_enter
        self._closed = asyncio.Event()
        self.loop = None
        self.connect_entered = threading.Event()

    def __await__(self):
        return self._connect().__await__()

    async def _connect(self):
        self.loop = asyncio.get_running_loop()
        self.connect_entered.set()
        owner = self.owner
        if owner.connect_entered is not None:
            owner.connect_entered.set()
        if owner.connect_gate is not None:
            await owner.connect_gate.wait()
        if owner.connect_delay:
            await asyncio.sleep(owner.connect_delay)
        if self.fail_enter:
            raise RuntimeError("backend unavailable")
        if owner.hang_connect:
            await asyncio.Event().wait()
        owner.connections.append(self)
        owner.connected.set()
        return self

    async def __aenter__(self):
        return await self._connect()

    async def __aexit__(self, *_exc):
        if self.closed is None:
            await self.close()
        return False

    def push(self, message):
        loop = self.loop
        if loop is None:
            self.incoming.put_nowait(message)
            return
        loop.call_soon_threadsafe(self.incoming.put_nowait, message)

    def eof(self):
        self.push(None)

    async def send_str(self, data):
        block = self.owner.send_block
        if self.owner.send_entered is not None:
            self.owner.send_entered.set()
        if block is not None:
            await block.wait()
        self.sent.append(("text", data))
        self.owner.sent.append(("text", data))
        self.owner.sent_event.set()

    async def send_bytes(self, data):
        block = self.owner.send_block
        if self.owner.send_entered is not None:
            self.owner.send_entered.set()
        if block is not None:
            await block.wait()
        self.sent.append(("bytes", data))
        self.owner.sent.append(("bytes", data))
        self.owner.sent_event.set()

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.incoming.get()
        if item is None or self._closed.is_set():
            raise StopAsyncIteration
        return item

    async def close(self, code=1000, message=b""):
        self.close_started.set()
        if self.close_delay:
            await asyncio.Event().wait()
        self.closed = (code, message)
        self._closed.set()
        await self.incoming.put(None)


class FakeBackendSession:
    def __init__(self, owner, **kwargs):
        self.owner = owner
        self.kwargs = kwargs
        self.closed = False
        self.close_entered = threading.Event()
        owner.backend_sessions.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        await self.close()
        return False

    async def close(self):
        self.close_entered.set()
        if self.owner.session_close_hang:
            await asyncio.Event().wait()
        self.closed = True

    def ws_connect(self, url, **kwargs):
        self.owner.ws_connect_calls.append((url, dict(kwargs)))
        return FakeBackendWs(self.owner, url, kwargs)


class FakeBackend:
    def __init__(self, *, fail_enter=False, hang_connect=False, connect_delay=0.0):
        self.fail_enter = fail_enter
        self.hang_connect = hang_connect
        self.connect_delay = connect_delay
        self.address_calls = []
        self.connections = []
        self.ws_connect_calls = []
        self.backend_sessions = []
        self.sent = []
        self.sent_event = threading.Event()
        self.connected = threading.Event()
        self.send_block = None
        self.send_entered = None
        self.connect_entered = threading.Event()
        self.connect_gate = None
        self.lookup_entered = threading.Event()
        self.lookup_release = threading.Event()
        self.lookup_release.set()
        self.lookup_block = False
        self.session_close_hang = False

    def address(self, chat_id, port):
        self.address_calls.append((chat_id, port))
        self.lookup_entered.set()
        if self.lookup_block:
            self.lookup_release.wait()
        return f"sandbox.{chat_id}:{port}"

    def session(self, **kwargs):
        return FakeBackendSession(self, **kwargs)


def _install_fakes(app_module, monkeypatch, auth, backend, clock=None):
    def session_factory(**kwargs):
        if kwargs.get("cookie_jar") is not None or kwargs.get("trust_env") is False:
            return FakeAuthSession(auth, **kwargs)
        return backend.session(**kwargs)

    monkeypatch.setattr(app_module, "get_container_service_address", backend.address)
    monkeypatch.setattr(app_module.aiohttp, "ClientSession", session_factory)
    import ws_recheck

    monkeypatch.setattr(ws_recheck.aiohttp, "ClientSession", session_factory)
    if clock is not None:
        monkeypatch.setattr(ws_recheck, "_now", clock.monotonic)
        monkeypatch.setattr(ws_recheck, "_sleep", clock.sleep)
    return ws_recheck


def _expect_close_code(socket, code, timeout=2.0):
    box = {}

    def _recv():
        try:
            box["message"] = socket.receive()
        except Exception as exc:
            box["error"] = exc

    worker = threading.Thread(target=_recv, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise AssertionError(f"timed out waiting for websocket close {code}")
    if "error" in box:
        observed = getattr(box["error"], "code", None)
        if observed == code:
            return {"type": "websocket.close", "code": code}
        raise box["error"]
    message = box.get("message") or {}
    if message.get("type") == "websocket.close":
        assert message.get("code") == code, message
        return message
    raise AssertionError(f"expected close {code}, got {message!r}")

# ---------------------------------------------------------------------------
# Admission: missing config/cookie must not touch Docker/backend.
# ---------------------------------------------------------------------------


class TestAdmissionDeniesBeforeBackend:
    @pytest.mark.parametrize("kind", ("cdp", "ttyd"))
    def test_missing_auth_url_closes_1008_without_backend_lookup(
        self, monkeypatch, tmp_path, kind
    ):
        _apply_env(monkeypatch, tmp_path, auth_url=None)
        app_module = _reload_server()
        backend = FakeBackend()
        monkeypatch.setattr(app_module, "get_container_service_address", backend.address)
        with _client(app_module) as client:
            with pytest.raises(Exception) as exc:
                with _ws_connect(client, kind):
                    pass
            assert getattr(exc.value, "code", None) == PREACCEPT_CODE
        assert backend.address_calls == []
        assert backend.ws_connect_calls == []

    @pytest.mark.parametrize("kind", ("cdp", "ttyd"))
    def test_missing_session_cookie_closes_1008_without_backend_lookup(
        self, app_module, monkeypatch, kind
    ):
        backend = FakeBackend()
        auth = ScriptedAuth([200])
        _install_fakes(app_module, monkeypatch, auth, backend)
        with _client(app_module) as client:
            with pytest.raises(Exception) as exc:
                with _ws_connect(client, kind, headers=_bearer()):
                    pass
            assert getattr(exc.value, "code", None) == PREACCEPT_CODE
        assert backend.address_calls == []
        assert auth.calls == []

    @pytest.mark.parametrize("kind", ("cdp", "ttyd"))
    def test_empty_token_cookie_closes_1008_without_backend_lookup(
        self, app_module, monkeypatch, kind
    ):
        backend = FakeBackend()
        auth = ScriptedAuth([200])
        _install_fakes(app_module, monkeypatch, auth, backend)
        with _client(app_module) as client:
            with pytest.raises(Exception) as trans:
                with _ws_connect(client, kind, headers=_bearer(Cookie="token=")):
                    pass
            assert getattr(trans.value, "code", None) == PREACCEPT_CODE
        assert backend.address_calls == []
        assert auth.calls == []


class TestStartupAuthUrl:
    def test_invalid_nonempty_url_fails_startup_without_leaking_value(self, monkeypatch, tmp_path):
        secret = "http://user:leaked-secret@webui.example/wrong"
        _apply_env(monkeypatch, tmp_path, auth_url=secret)
        import auth_guard

        stderr = io.StringIO()
        monkeypatch.setattr(sys, "stderr", stderr)
        assert auth_guard.startup_preflight() != 0
        text = stderr.getvalue()
        assert "OCU_WEBUI_AUTH_URL" in text
        assert "leaked-secret" not in text
        assert secret not in text
        assert "user:leaked-secret" not in text

    def test_whitespace_only_url_fails_startup(self, monkeypatch, tmp_path):
        _apply_env(monkeypatch, tmp_path, auth_url="   ")
        import auth_guard

        stderr = io.StringIO()
        monkeypatch.setattr(sys, "stderr", stderr)
        assert auth_guard.startup_preflight() != 0
        assert "OCU_WEBUI_AUTH_URL" in stderr.getvalue()

    def test_malformed_ipv6_url_fails_startup_without_leaking_value(self, monkeypatch, tmp_path):
        secret = "http://[not-a-host/api/v1/ocu/auth"
        _apply_env(monkeypatch, tmp_path, auth_url=secret)
        import auth_guard

        stderr = io.StringIO()
        monkeypatch.setattr(sys, "stderr", stderr)
        assert auth_guard.startup_preflight() != 0
        text = stderr.getvalue()
        assert "OCU_WEBUI_AUTH_URL" in text
        assert secret not in text


class TestInitialOwnerCheck:
    @pytest.mark.parametrize("kind", ("cdp", "ttyd"))
    def test_initial_non_200_closes_1008_without_backend_lookup(
        self, app_module, monkeypatch, kind
    ):
        backend = FakeBackend()
        auth = ScriptedAuth([403])
        _install_fakes(app_module, monkeypatch, auth, backend)
        with _client(app_module) as client:
            with pytest.raises(Exception):
                with _ws_connect(client, kind):
                    pass
        assert backend.address_calls == []
        assert len(auth.calls) == 1
        request = auth.calls[0]
        headers = {name.lower(): value for name, value in request["headers"].items()}
        assert request["url"].endswith(AUTH_PATH)
        assert headers.get("cookie") == COOKIE_A
        assert headers.get("x-chat-id") == CHAT
        assert headers.get("x-ocu-internal-token") == INTERNAL
        assert "authorization" not in headers
        assert request["allow_redirects"] is False

    @pytest.mark.parametrize("kind", ("cdp", "ttyd"))
    def test_absent_sandbox_after_authorization_still_closes_1008(
        self, app_module, monkeypatch, kind
    ):
        backend = FakeBackend()
        auth = ScriptedAuth([200])
        _install_fakes(app_module, monkeypatch, auth, backend)
        monkeypatch.setattr(app_module, "get_container_service_address", lambda *_a, **_k: None)
        with _client(app_module) as client:
            with pytest.raises(Exception):
                with _ws_connect(client, kind):
                    pass
        assert auth.calls


class TestHealthyRelayAndIsolation:
    @pytest.mark.parametrize("kind", ("cdp", "ttyd"))
    def test_healthy_recheck_keeps_relaying_across_multiple_ticks(
        self, app_module, monkeypatch, kind
    ):
        backend = FakeBackend()
        auth = ScriptedAuth([200, 200, 200, 200])
        clock = RecordingClock()
        ws_recheck = _install_fakes(app_module, monkeypatch, auth, backend, clock)
        if ws_recheck is not None:
            assert ws_recheck.RECHECK_INTERVAL_SECONDS == RECHECK_INTERVAL
            assert ws_recheck.AUTH_TIMEOUT_SECONDS == AUTH_DEADLINE
        with _client(app_module) as client:
            with _ws_connect(client, kind) as socket:
                deadline = time.monotonic() + 2
                while not backend.connections and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert backend.connections
                conn = backend.connections[0]
                if kind == "cdp":
                    socket.send_text("hello-cdp")
                else:
                    socket.send_bytes(b"hello-bin")
                    socket.send_text("hello-txt")
                deadline = time.monotonic() + 2
                while not conn.sent and time.monotonic() < deadline:
                    time.sleep(0.01)
                if kind == "cdp":
                    assert ("text", "hello-cdp") in conn.sent
                    conn.push(
                        FakeBackendMessage(app_module.aiohttp.WSMsgType.TEXT, "from-cdp")
                    )
                    assert socket.receive_text() == "from-cdp"
                else:
                    assert ("bytes", b"hello-bin") in conn.sent
                    assert ("text", "hello-txt") in conn.sent
                    conn.push(
                        FakeBackendMessage(app_module.aiohttp.WSMsgType.BINARY, b"from-bin")
                    )
                    conn.push(
                        FakeBackendMessage(app_module.aiohttp.WSMsgType.TEXT, "from-txt")
                    )
                    assert socket.receive_bytes() == b"from-bin"
                    assert socket.receive_text() == "from-txt"
                assert len(auth.calls) == 1
                deadline = time.monotonic() + 2
                while not clock.sleeps and time.monotonic() < deadline:
                    time.sleep(0.01)
                clock.advance(RECHECK_INTERVAL)
                deadline = time.monotonic() + 2
                while len(auth.calls) < 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
                deadline = time.monotonic() + 2
                while len(clock.sleeps) < 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
                clock.advance(RECHECK_INTERVAL)
                deadline = time.monotonic() + 2
                while len(auth.calls) < 3 and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert len(auth.calls) == 3
                socket.send_text("still-open") if kind == "cdp" else socket.send_bytes(b"still-open")
                deadline = time.monotonic() + 2
                while not any("still-open" in str(item[1]) for item in conn.sent) and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert any("still-open" in str(item[1]) for item in conn.sent)
                assert all(call["headers"].get("cookie") == COOKIE_A or call["headers"].get("Cookie") == COOKIE_A for call in auth.calls)
        assert clock.sleeps[:2] == [RECHECK_INTERVAL, RECHECK_INTERVAL]
        assert not any(kwargs.get("headers") for _url, kwargs in backend.ws_connect_calls)

    def test_concurrent_sockets_keep_isolated_cookie_and_chat(self, app_module, monkeypatch):
        backend = FakeBackend()
        auth = ScriptedAuth([200, 200, 200, 200])
        clock = RecordingClock()
        _install_fakes(app_module, monkeypatch, auth, backend, clock)
        with _client(app_module) as client:
            with _ws_connect(client, "cdp", chat_id=CHAT, headers=_cookie_headers(COOKIE_A)) as sock_a:
                with _ws_connect(
                    client, "ttyd", chat_id=CHAT_B, headers=_cookie_headers(COOKIE_B)
                ) as sock_b:
                    deadline = time.monotonic() + 2
                    while len(backend.connections) < 2 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    sock_a.send_text("a")
                    sock_b.send_bytes(b"b")
                    deadline = time.monotonic() + 2
                    while len(backend.sent) < 2 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    deadline = time.monotonic() + 2
                    while len(clock.sleeps) < 2 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    clock.advance(RECHECK_INTERVAL)
                    deadline = time.monotonic() + 2
                    while len(auth.calls) < 4 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    pairs = {
                        (
                            {k.lower(): v for k, v in call["headers"].items()}.get("cookie"),
                            {k.lower(): v for k, v in call["headers"].items()}.get("x-chat-id"),
                        )
                        for call in auth.calls
                    }
                    assert (COOKIE_A, CHAT) in pairs
                    assert (COOKIE_B, CHAT_B) in pairs
                    assert (COOKIE_A, CHAT_B) not in pairs
                    assert (COOKIE_B, CHAT) not in pairs
                    _ = sock_a, sock_b


class TestRevocationClosesAndStopsForwarding:
    @pytest.mark.parametrize("kind", ("cdp", "ttyd"))
    @pytest.mark.parametrize("status", (401, 403, 500, 302))
    def test_non_200_closes_4401_and_drops_queued_input(
        self, app_module, monkeypatch, kind, status
    ):
        backend = FakeBackend()
        auth = ScriptedAuth([200, status])
        clock = RecordingClock()
        _install_fakes(app_module, monkeypatch, auth, backend, clock)
        with _client(app_module) as client:
            with _ws_connect(client, kind) as socket:
                deadline = time.monotonic() + 2
                while not backend.connections and time.monotonic() < deadline:
                    time.sleep(0.01)
                conn = backend.connections[0]
                if kind == "cdp":
                    socket.send_text("pre")
                else:
                    socket.send_bytes(b"pre")
                deadline = time.monotonic() + 2
                while not conn.sent and time.monotonic() < deadline:
                    time.sleep(0.01)
                before = list(conn.sent)
                deadline = time.monotonic() + 2
                while not clock.sleeps and time.monotonic() < deadline:
                    time.sleep(0.01)
                clock.advance(RECHECK_INTERVAL)
                from starlette.websockets import WebSocketDisconnect

                deadline = time.monotonic() + 2
                while conn.closed is None and time.monotonic() < deadline:
                    if kind == "cdp":
                        socket.send_text(FUTURE_MARKER)
                    else:
                        socket.send_bytes(FUTURE_MARKER.encode())
                    time.sleep(0.01)
                _expect_close_code(socket, REVOKE_CODE)
                assert all(FUTURE_MARKER not in str(item[1]) for item in conn.sent)
                assert conn.sent[: len(before)] == before
                assert conn.closed is not None

    @pytest.mark.parametrize("kind", ("cdp", "ttyd"))
    def test_auth_timeout_or_transport_error_closes_4401(
        self, app_module, monkeypatch, kind
    ):
        backend = FakeBackend()
        auth = ScriptedAuth([200])
        clock = RecordingClock()
        _install_fakes(app_module, monkeypatch, auth, backend, clock)
        original_enter = FakeAuthGet.__aenter__

        async def fail_after_first(self):
            if self._owner.calls:
                self._error = OSError("auth disconnected")
            return await original_enter(self)

        monkeypatch.setattr(FakeAuthGet, "__aenter__", fail_after_first)
        with _client(app_module) as client:
            with _ws_connect(client, kind) as socket:
                deadline = time.monotonic() + 2
                while not backend.connections and time.monotonic() < deadline:
                    time.sleep(0.01)
                deadline = time.monotonic() + 2
                while not clock.sleeps and time.monotonic() < deadline:
                    time.sleep(0.01)
                clock.advance(RECHECK_INTERVAL)
                _expect_close_code(socket, REVOKE_CODE)

    def test_redirect_is_denial_and_does_not_follow(self, app_module, monkeypatch):
        backend = FakeBackend()
        auth = ScriptedAuth([200, 302])
        clock = RecordingClock()
        _install_fakes(app_module, monkeypatch, auth, backend, clock)
        with _client(app_module) as client:
            with _ws_connect(client, "cdp") as socket:
                deadline = time.monotonic() + 2
                while not backend.connections and time.monotonic() < deadline:
                    time.sleep(0.01)
                deadline = time.monotonic() + 2
                while not clock.sleeps and time.monotonic() < deadline:
                    time.sleep(0.01)
                clock.advance(RECHECK_INTERVAL)
                _expect_close_code(socket, REVOKE_CODE)
        assert all(call["allow_redirects"] is False for call in auth.calls)
        assert all(
            session.kwargs.get("trust_env") is False for session in auth.sessions
        )
        jars = [session.kwargs.get("cookie_jar") for session in auth.sessions]
        assert jars and all(jar is not None for jar in jars)


class TestFlagRaceAndBlockedSends:
    @pytest.mark.parametrize("kind", ("cdp", "ttyd"))
    def test_blocked_pending_send_is_cancelled_on_revoke(
        self, app_module, monkeypatch, kind
    ):
        backend = FakeBackend()
        backend.send_block = asyncio.Event()
        backend.send_entered = threading.Event()
        auth = ScriptedAuth([200, 403])
        clock = RecordingClock()
        _install_fakes(app_module, monkeypatch, auth, backend, clock)
        with _client(app_module) as client:
            with _ws_connect(client, kind) as socket:
                assert backend.connected.wait(2)
                if kind == "cdp":
                    socket.send_text("blocked")
                else:
                    socket.send_bytes(b"blocked")
                assert backend.send_entered.wait(2)
                deadline = time.monotonic() + 2
                while not clock.sleeps and time.monotonic() < deadline:
                    time.sleep(0.01)
                clock.advance(RECHECK_INTERVAL)
                _expect_close_code(socket, REVOKE_CODE)
                backend.send_block.set()
                assert all("blocked" not in str(item[1]) for item in backend.sent)
                try:
                    socket.send_text(FUTURE_MARKER)
                except Exception:
                    pass
                assert all(FUTURE_MARKER not in str(item[1]) for item in backend.sent)
        assert all(session.close_entered.is_set() for session in auth.sessions)


class TestNormalDisconnectCancelAndBackendFailure:
    @pytest.mark.parametrize("kind", ("cdp", "ttyd"))
    def test_backend_connect_failure_still_closes_1011(
        self, app_module, monkeypatch, kind
    ):
        backend = FakeBackend(fail_enter=True)
        auth = ScriptedAuth([200])
        _install_fakes(app_module, monkeypatch, auth, backend)
        with _client(app_module) as client:
            with _ws_connect(client, kind) as socket:
                _expect_close_code(socket, BACKEND_FAIL_CODE)
        assert backend.address_calls
        assert auth.calls

    @pytest.mark.parametrize("kind", ("cdp", "ttyd"))
    def test_client_disconnect_cancels_owned_tasks(self, app_module, monkeypatch, kind):
        backend = FakeBackend()
        auth = ScriptedAuth([200, 200, 200])
        clock = RecordingClock()
        _install_fakes(app_module, monkeypatch, auth, backend, clock)
        with _client(app_module) as client:
            with _ws_connect(client, kind) as socket:
                deadline = time.monotonic() + 2
                while not backend.connections and time.monotonic() < deadline:
                    time.sleep(0.01)
                socket.close()
        deadline = time.monotonic() + 2
        while backend.connections and backend.connections[0].closed is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert backend.connections[0].closed is not None

    @pytest.mark.parametrize("kind", ("cdp", "ttyd"))
    def test_slow_backend_connect_does_not_delay_first_periodic_check(
        self, app_module, monkeypatch, kind
    ):
        backend = FakeBackend(connect_delay=0.2)
        auth = ScriptedAuth([200, 403])
        clock = RecordingClock()
        _install_fakes(app_module, monkeypatch, auth, backend, clock)
        from starlette.websockets import WebSocketDisconnect

        with _client(app_module) as client:
            with _ws_connect(client, kind) as socket:
                # Rechecker must already be waiting the full 30s from initial
                # success, not from backend connect completion.
                deadline = time.monotonic() + 2
                while not clock.sleeps and time.monotonic() < deadline:
                    time.sleep(0.01)
                clock.advance(RECHECK_INTERVAL)
                _expect_close_code(socket, REVOKE_CODE)
        assert clock.sleeps
        assert clock.sleeps[0] == RECHECK_INTERVAL

    @pytest.mark.parametrize("kind", ("cdp", "ttyd"))
    def test_bounded_close_does_not_wait_forever_on_stuck_backend(
        self, app_module, monkeypatch, kind
    ):
        backend = FakeBackend()
        auth = ScriptedAuth([200, 403])
        clock = RecordingClock()
        _install_fakes(app_module, monkeypatch, auth, backend, clock)
        import ws_recheck

        monkeypatch.setattr(ws_recheck, "BACKEND_CLOSE_TIMEOUT_SECONDS", 0.05)
        with _client(app_module) as client:
            with _ws_connect(client, kind) as socket:
                deadline = time.monotonic() + 2
                while not backend.connections and time.monotonic() < deadline:
                    time.sleep(0.01)
                backend.connections[0].close_delay = 30.0
                deadline = time.monotonic() + 2
                while not clock.sleeps and time.monotonic() < deadline:
                    time.sleep(0.01)
                wall_started = _WALL_MONOTONIC()
                clock.advance(RECHECK_INTERVAL)
                _expect_close_code(socket, REVOKE_CODE)
                elapsed = _WALL_MONOTONIC() - wall_started
                assert elapsed < 2


class TestAuthRequestShape:
    def test_auth_client_disables_redirects_proxy_and_cookie_jar(self, app_module, monkeypatch):
        backend = FakeBackend()
        auth = ScriptedAuth([200])
        _install_fakes(app_module, monkeypatch, auth, backend)
        with _client(app_module) as client:
            with _ws_connect(client, "cdp") as socket:
                deadline = time.monotonic() + 2
                while not backend.connections and time.monotonic() < deadline:
                    time.sleep(0.01)
                socket.close()
        assert auth.sessions
        kwargs = auth.sessions[0].kwargs
        assert kwargs.get("trust_env") is False
        assert kwargs.get("cookie_jar") is not None
        timeout = kwargs.get("timeout")
        assert timeout is not None
        assert getattr(timeout, "total", None) == AUTH_DEADLINE
        headers = {k.lower(): v for k, v in auth.calls[0]["headers"].items()}
        assert headers["cookie"] == COOKIE_A
        assert headers["x-chat-id"] == CHAT
        assert headers["x-ocu-internal-token"] == INTERNAL
        assert "authorization" not in headers
        parsed = urlparse(auth.calls[0]["url"])
        assert parsed.path == AUTH_PATH
        assert parsed.query == ""
        assert parsed.fragment == ""


class TestBackendEofClosesFrontend:
    @pytest.mark.parametrize("kind", ("cdp", "ttyd"))
    def test_backend_eof_closes_frontend_1000(self, app_module, monkeypatch, kind):
        backend = FakeBackend()
        auth = ScriptedAuth([200])
        clock = RecordingClock()
        _install_fakes(app_module, monkeypatch, auth, backend, clock)
        with _client(app_module) as client:
            with _ws_connect(client, kind) as socket:
                assert backend.connected.wait(2)
                conn = backend.connections[0]
                if kind == "cdp":
                    socket.send_text("pre")
                else:
                    socket.send_bytes(b"pre")
                deadline = time.monotonic() + 2
                while not conn.sent and time.monotonic() < deadline:
                    time.sleep(0.01)
                conn.eof()
                _expect_close_code(socket, 1000)


class TestLookupAndHungConnect:
    def test_blocking_lookup_does_not_stall_sibling_recheck(
        self, app_module, monkeypatch
    ):
        backend = FakeBackend()
        auth = ScriptedAuth(
            [200],
            by_chat={CHAT: [200, 200, 200], CHAT_B: [200, 403]},
        )
        clock = RecordingClock()
        _install_fakes(app_module, monkeypatch, auth, backend, clock)
        slow = FakeBackend()
        slow.lookup_block = True
        slow.lookup_release.clear()

        def lookup(chat_id, port):
            if chat_id == CHAT_B:
                return slow.address(chat_id, port)
            return backend.address(chat_id, port)

        monkeypatch.setattr(app_module, "get_container_service_address", lookup)
        b_result = {}

        def open_b():
            try:
                with _ws_connect(
                    client, "ttyd", chat_id=CHAT_B, headers=_cookie_headers(COOKIE_B)
                ) as sock:
                    b_result["code"] = None
                    sock.close()
            except Exception as exc:
                b_result["code"] = getattr(exc, "code", None)

        with _client(app_module) as client:
            with _ws_connect(client, "cdp", chat_id=CHAT, headers=_cookie_headers(COOKIE_A)) as sock_a:
                assert backend.connected.wait(2)
                starter = threading.Thread(target=open_b, daemon=True)
                starter.start()
                try:
                    assert slow.lookup_entered.wait(2)
                    deadline = time.monotonic() + 2
                    while not clock.sleeps and time.monotonic() < deadline:
                        time.sleep(0.01)
                    clock.advance(RECHECK_INTERVAL)
                    deadline = time.monotonic() + 2
                    while auth.pair_counts().get((COOKIE_A, CHAT), 0) < 2 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    assert auth.pair_counts().get((COOKIE_A, CHAT), 0) >= 2
                    sock_a.send_text("alive")
                    deadline = time.monotonic() + 2
                    while not backend.connections[0].sent and time.monotonic() < deadline:
                        time.sleep(0.01)
                    starter.join(2)
                    assert b_result.get("code") == REVOKE_CODE
                finally:
                    slow.lookup_release.set()
                    starter.join(2)

    @pytest.mark.parametrize("kind", ("cdp", "ttyd"))
    def test_hung_connect_still_revokes_from_initial_success(
        self, app_module, monkeypatch, kind
    ):
        backend = FakeBackend(hang_connect=True)
        backend.connect_gate = asyncio.Event()
        auth = ScriptedAuth([200, 403])
        clock = RecordingClock()
        _install_fakes(app_module, monkeypatch, auth, backend, clock)
        with _client(app_module) as client:
            with _ws_connect(client, kind) as socket:
                assert backend.connect_entered.wait(2)
                assert not backend.connections
                deadline = time.monotonic() + 2
                while not clock.sleeps and time.monotonic() < deadline:
                    time.sleep(0.01)
                clock.advance(RECHECK_INTERVAL)
                _expect_close_code(socket, REVOKE_CODE)
                assert not backend.connections


class TestSessionCloseDoesNotBlockRevoke:
    @pytest.mark.parametrize("kind", ("cdp", "ttyd"))
    def test_stuck_session_close_still_delivers_4401(
        self, app_module, monkeypatch, kind
    ):
        backend = FakeBackend()
        backend.session_close_hang = True
        auth = ScriptedAuth([200, 403])
        clock = RecordingClock()
        _install_fakes(app_module, monkeypatch, auth, backend, clock)
        with _client(app_module) as client:
            with _ws_connect(client, kind) as socket:
                assert backend.connected.wait(2)
                conn = backend.connections[0]
                conn.close_delay = 30.0
                deadline = time.monotonic() + 2
                while not clock.sleeps and time.monotonic() < deadline:
                    time.sleep(0.01)
                clock.advance(RECHECK_INTERVAL)
                _expect_close_code(socket, REVOKE_CODE)
                assert conn.close_started.wait(2)
                assert backend.backend_sessions[0].close_entered.wait(2)


class TestHungAuthAndCancel:
    @pytest.mark.parametrize("kind", ("cdp", "ttyd"))
    def test_initial_hung_auth_closes_1008(self, app_module, monkeypatch, kind):
        backend = FakeBackend()
        auth = ScriptedAuth([200], hang=True)
        _install_fakes(app_module, monkeypatch, auth, backend)
        with _client(app_module) as client:
            with pytest.raises(Exception) as exc:
                with _ws_connect(client, kind):
                    pass
            assert getattr(exc.value, "code", None) == PREACCEPT_CODE
        assert backend.address_calls == []

    @pytest.mark.parametrize("kind", ("cdp", "ttyd"))
    def test_periodic_hung_auth_closes_4401(self, app_module, monkeypatch, kind):
        backend = FakeBackend()
        auth = ScriptedAuth([200])
        clock = RecordingClock()
        _install_fakes(app_module, monkeypatch, auth, backend, clock)
        with _client(app_module) as client:
            with _ws_connect(client, kind) as socket:
                assert backend.connected.wait(2)
                deadline = time.monotonic() + 2
                while not clock.sleeps and time.monotonic() < deadline:
                    time.sleep(0.01)
                auth.hang = True
                clock.advance(RECHECK_INTERVAL)
                _expect_close_code(socket, REVOKE_CODE, timeout=6.0)

    @pytest.mark.parametrize("kind", ("cdp", "ttyd"))
    def test_route_cancel_during_hung_auth_cleans_up(self, app_module, monkeypatch, kind):
        backend = FakeBackend()
        auth = ScriptedAuth([200], hang=True)
        _install_fakes(app_module, monkeypatch, auth, backend)
        import ws_recheck

        pending = threading.Event()
        original = ws_recheck.RelaySession.authorize_once

        async def hanging_authorize(self):
            pending.set()
            await asyncio.Event().wait()
            return True

        monkeypatch.setattr(ws_recheck.RelaySession, "authorize_once", hanging_authorize)
        box = {}

        async def drive():
            closer = []

            async def receive():
                if not closer:
                    closer.append(True)
                    return {"type": "websocket.connect"}
                await asyncio.Event().wait()
                return {"type": "websocket.disconnect", "code": 1000}

            async def send(message):
                return None

            task = asyncio.create_task(
                app_module.app(_ws_scope(kind), receive, send)
            )
            deadline = time.monotonic() + 2
            while not pending.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert pending.is_set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                box["cancelled"] = True
            else:
                box["cancelled"] = False
            box["done"] = task.done()

        asyncio.run(drive())
        assert box.get("cancelled") is True
        assert box.get("done") is True
        assert backend.address_calls == []
        assert all(session.close_entered.is_set() or session.closed for session in auth.sessions)



class TestDecisionBarriersAndConcurrentPairs:
    @pytest.mark.parametrize("kind", ("cdp", "ttyd"))
    def test_predecision_frame_forwards_postdecision_does_not(
        self, app_module, monkeypatch, kind
    ):
        backend = FakeBackend()
        auth = ScriptedAuth([200, 403])
        clock = RecordingClock()
        _install_fakes(app_module, monkeypatch, auth, backend, clock)
        with _client(app_module) as client:
            with _ws_connect(client, kind) as socket:
                assert backend.connected.wait(2)
                conn = backend.connections[0]
                pre = "pre-decision"
                if kind == "cdp":
                    socket.send_text(pre)
                else:
                    socket.send_bytes(pre.encode())
                deadline = time.monotonic() + 2
                while not conn.sent and time.monotonic() < deadline:
                    time.sleep(0.01)
                deadline = time.monotonic() + 2
                while not clock.sleeps and time.monotonic() < deadline:
                    time.sleep(0.01)
                auth.pause_decisions()
                clock.advance(RECHECK_INTERVAL)
                assert auth.periodic_entered.wait(2)
                if kind == "cdp":
                    socket.send_text("allowed-before-decision")
                else:
                    socket.send_bytes(b"allowed-before-decision")
                deadline = time.monotonic() + 2
                while not any("allowed-before-decision" in str(item[1]) for item in conn.sent) and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert any("allowed-before-decision" in str(item[1]) for item in conn.sent)
                auth.resume_decisions()
                assert auth.decision_made.wait(2)
                if kind == "cdp":
                    socket.send_text(FUTURE_MARKER)
                else:
                    socket.send_bytes(FUTURE_MARKER.encode())
                _expect_close_code(socket, REVOKE_CODE)
                assert all(FUTURE_MARKER not in str(item[1]) for item in conn.sent)

    def test_concurrent_pairs_each_recheck_and_b_revokes_alone(
        self, app_module, monkeypatch
    ):
        backend = FakeBackend()
        auth = ScriptedAuth(
            [200],
            by_chat={CHAT: [200, 200, 200], CHAT_B: [200, 403]},
        )
        clock = RecordingClock()
        _install_fakes(app_module, monkeypatch, auth, backend, clock)
        with _client(app_module) as client:
            with _ws_connect(client, "cdp", chat_id=CHAT, headers=_cookie_headers(COOKIE_A)) as sock_a:
                with _ws_connect(
                    client, "ttyd", chat_id=CHAT_B, headers=_cookie_headers(COOKIE_B)
                ) as sock_b:
                    deadline = time.monotonic() + 2
                    while len(backend.connections) < 2 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    deadline = time.monotonic() + 2
                    while len(clock.sleeps) < 2 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    clock.advance(RECHECK_INTERVAL)
                    deadline = time.monotonic() + 2
                    while (
                        auth.pair_counts().get((COOKIE_A, CHAT), 0) < 2
                        or auth.pair_counts().get((COOKIE_B, CHAT_B), 0) < 2
                    ) and time.monotonic() < deadline:
                        time.sleep(0.01)
                    counts = auth.pair_counts()
                    assert counts.get((COOKIE_A, CHAT), 0) >= 2
                    assert counts.get((COOKIE_B, CHAT_B), 0) >= 2
                    _expect_close_code(sock_b, REVOKE_CODE)
                    sock_a.send_text("a-still-open")
                    deadline = time.monotonic() + 2
                    while not any("a-still-open" in str(item[1]) for item in backend.connections[0].sent) and time.monotonic() < deadline:
                        time.sleep(0.01)
                    assert any("a-still-open" in str(item[1]) for item in backend.connections[0].sent)

class TestConnectRevokeAndRouteCancel:
    @pytest.mark.parametrize("kind", ("cdp", "ttyd"))
    def test_connect_phase_revoke_enters_session_close(self, app_module, monkeypatch, kind):
        backend = FakeBackend(hang_connect=True)
        backend.session_close_hang = True
        backend.connect_gate = asyncio.Event()
        auth = ScriptedAuth([200, 403])
        clock = RecordingClock()
        _install_fakes(app_module, monkeypatch, auth, backend, clock)
        with _client(app_module) as client:
            with _ws_connect(client, kind) as socket:
                assert backend.connect_entered.wait(2)
                deadline = time.monotonic() + 2
                while not clock.sleeps and time.monotonic() < deadline:
                    time.sleep(0.01)
                clock.advance(RECHECK_INTERVAL)
                if backend.backend_sessions:
                    assert backend.backend_sessions[0].close_entered.wait(2)
                _expect_close_code(socket, REVOKE_CODE)
                assert not backend.connections


class TestLookupTimeoutAndException:
    @pytest.mark.parametrize("kind", ("cdp", "ttyd"))
    def test_healthy_lookup_stall_closes_1011(self, app_module, monkeypatch, kind):
        backend = FakeBackend()
        backend.lookup_block = True
        backend.lookup_release.clear()
        auth = ScriptedAuth([200])
        _install_fakes(app_module, monkeypatch, auth, backend)
        import ws_recheck
        monkeypatch.setattr(ws_recheck, "LOOKUP_TIMEOUT_SECONDS", 5.0)
        try:
            close, error, _messages = _run_ws_route(
                app_module.app, kind, timeout=7.0
            )
            code = (close or {}).get("code")
            if code is None and error is not None:
                code = getattr(error, "code", None)
            assert code == BACKEND_FAIL_CODE, (close, error)
        finally:
            backend.lookup_release.set()

    @pytest.mark.parametrize("kind", ("cdp", "ttyd"))
    def test_lookup_exception_closes_1011(self, app_module, monkeypatch, kind):
        backend = FakeBackend()
        auth = ScriptedAuth([200])
        _install_fakes(app_module, monkeypatch, auth, backend)

        def boom(*_a, **_k):
            raise RuntimeError("docker ping failed")

        monkeypatch.setattr(app_module, "get_container_service_address", boom)
        with _client(app_module) as client:
            with pytest.raises(Exception) as exc:
                with _ws_connect(client, kind):
                    pass
            assert getattr(exc.value, "code", None) == BACKEND_FAIL_CODE
        assert auth.calls


class TestEofVersusRevokeRace:
    @pytest.mark.parametrize("kind", ("cdp", "ttyd"))
    def test_simultaneous_eof_and_healthy_tick_closes_1000(self, app_module, monkeypatch, kind):
        backend = FakeBackend()
        auth = ScriptedAuth([200, 200, 200])
        clock = RecordingClock()
        _install_fakes(app_module, monkeypatch, auth, backend, clock)
        with _client(app_module) as client:
            with _ws_connect(client, kind) as socket:
                assert backend.connected.wait(2)
                conn = backend.connections[0]
                deadline = time.monotonic() + 2
                while not clock.sleeps and time.monotonic() < deadline:
                    time.sleep(0.01)
                conn.eof()
                clock.advance(RECHECK_INTERVAL)
                _expect_close_code(socket, 1000)

    @pytest.mark.parametrize("kind", ("cdp", "ttyd"))
    def test_genuine_403_still_closes_4401(self, app_module, monkeypatch, kind):
        backend = FakeBackend()
        auth = ScriptedAuth([200, 403])
        clock = RecordingClock()
        _install_fakes(app_module, monkeypatch, auth, backend, clock)
        with _client(app_module) as client:
            with _ws_connect(client, kind) as socket:
                assert backend.connected.wait(2)
                deadline = time.monotonic() + 2
                while not clock.sleeps and time.monotonic() < deadline:
                    time.sleep(0.01)
                clock.advance(RECHECK_INTERVAL)
                _expect_close_code(socket, REVOKE_CODE)
