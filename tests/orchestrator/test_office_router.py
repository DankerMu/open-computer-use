# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Office route availability: disabled, unknown chat, and unknown-route fallback."""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import pytest

SERVER_DIR = Path(__file__).resolve().parents[2] / "computer-use-server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from tests.orchestrator.test_outputs_endpoint import (
    CHAT,
    INTERNAL,
    _auth,
    _client,
    _isolated_app,
)


MISSING = "missing-office-chat"
FILE_ID = "file"
SESSION_ID = "session"
OFFICE_SETTINGS = {
    "OCU_OFFICE_DOCSERVER_ORIGIN": "http://docs.example:8082",
    "OCU_OFFICE_SELF_URL": "http://ocu:8081",
    "OCU_OFFICE_JWT_SECRET": "office-router-test-canary",
}
PLANNED_ROUTES = (
    ("POST", f"/api/office/{CHAT}/documents/{FILE_ID}/sessions"),
    ("GET", f"/api/office/{CHAT}/sessions/{SESSION_ID}"),
    ("POST", f"/api/office/{CHAT}/sessions/{SESSION_ID}/save"),
    ("POST", f"/api/office/{CHAT}/sessions/{SESSION_ID}/close"),
    ("POST", f"/api/office/{CHAT}/sessions/{SESSION_ID}/resolve"),
    ("GET", f"/api/office/{CHAT}/documents/{FILE_ID}/versions"),
    ("POST", f"/api/office/{CHAT}/documents/{FILE_ID}/restore"),
)
DISABLED_ROUTES = PLANNED_ROUTES + (
    ("GET", f"/api/office/{CHAT}/unknown"),
    ("OPTIONS", f"/api/office/{CHAT}/sessions/{SESSION_ID}"),
    ("CUSTOM", f"/api/office/{CHAT}/unknown"),
)


class _RecordingOrigin:
    def __init__(self):
        self.hits = 0
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def _record(self):
                parent.hits += 1
                self.send_response(204)
                self.end_headers()

            def do_GET(self):
                self._record()

            def do_POST(self):
                self._record()

            def do_OPTIONS(self):
                self._record()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


@contextmanager
def _recording_origin():
    origin = _RecordingOrigin()
    try:
        yield origin
    finally:
        origin.stop()


def _inventory(root: Path):
    if not root.exists():
        return set()
    return {path.relative_to(root) for path in root.rglob("*")}


def _assert_reason(response, reason):
    assert response.status_code == 404
    assert json.loads(response.content)["reason"] == reason
    assert INTERNAL not in response.text


def _forbidden(*args, **kwargs):
    raise AssertionError("office availability took a chat lock or state path")


@contextmanager
def _trap_lock_and_state(docker_manager):
    from office.store import OfficeStore

    original_lock = docker_manager._combined_lock
    original_read = OfficeStore.read
    original_update = OfficeStore.update
    docker_manager._combined_lock = _forbidden
    OfficeStore.read = _forbidden
    OfficeStore.update = _forbidden
    try:
        yield
    finally:
        docker_manager._combined_lock = original_lock
        OfficeStore.read = original_read
        OfficeStore.update = original_update


@contextmanager
def _trap_chat_fs(data: Path, chat_id: str):
    root = Path(data) / chat_id
    original_is_dir = Path.is_dir
    original_exists = Path.exists
    original_stat = Path.stat
    original_mkdir = Path.mkdir

    def _hit(path):
        candidate = Path(path)
        return candidate == root or root in candidate.parents

    def is_dir(self):
        if _hit(self):
            raise AssertionError(("chat filesystem inspection", str(self)))
        return original_is_dir(self)

    def exists(self):
        if _hit(self):
            raise AssertionError(("chat filesystem inspection", str(self)))
        return original_exists(self)

    def stat(self, *args, **kwargs):
        if _hit(self):
            raise AssertionError(("chat filesystem inspection", str(self)))
        return original_stat(self, *args, **kwargs)

    def mkdir(self, *args, **kwargs):
        if _hit(self):
            raise AssertionError(("chat directory created", str(self)))
        return original_mkdir(self, *args, **kwargs)

    Path.is_dir = is_dir
    Path.exists = exists
    Path.stat = stat
    Path.mkdir = mkdir
    try:
        yield
    finally:
        Path.is_dir = original_is_dir
        Path.exists = original_exists
        Path.stat = original_stat
        Path.mkdir = original_mkdir


@contextmanager
def _office_app(tmp_path, monkeypatch, *, enabled: bool):
    root = tmp_path / ("enabled" if enabled else "disabled")
    root.mkdir(parents=True, exist_ok=True)
    with _recording_origin() as origin:
        with monkeypatch.context() as env:
            env.delenv("OCU_OFFICE_DOCSERVER_URL", raising=False)
            for name in OFFICE_SETTINGS:
                env.delenv(name, raising=False)
            if enabled:
                for name, value in OFFICE_SETTINGS.items():
                    env.setenv(name, value)
                env.setenv("OCU_OFFICE_DOCSERVER_URL", origin.url)
            with _isolated_app(root) as (loaded, docker_manager, _broker, data):
                with _client(loaded) as http:
                    yield http, data, origin, docker_manager


def _office_headers():
    return {
        **_auth(),
        "Origin": "https://webui.example",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "Authorization",
    }


SANDBOX_PEER = "10.90.0.8"
NEWLINE_ENCODED = f"/api/office/{CHAT}/unknown%0Asegment"
NEWLINE_DECODED = f"/api/office/{CHAT}/unknown\nsegment"


def _raw_office(app, path, headers, method="GET", client=("testclient", 50000), body=b""):
    messages = []
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": headers,
        "client": client,
        "server": ("testserver", 80),
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))
    start = next(message for message in messages if message["type"] == "http.response.start")
    payload = b"".join(
        message.get("body") or b""
        for message in messages
        if message["type"] == "http.response.body"
    )
    return start["status"], json.loads(payload) if payload else {}





@pytest.mark.parametrize("method,path", DISABLED_ROUTES)
def test_disabled_office_routes_are_404_without_work(
    tmp_path, monkeypatch, method, path
):
    with _office_app(tmp_path, monkeypatch, enabled=False) as (
        http,
        data,
        origin,
        docker_manager,
    ):
        before = _inventory(data)
        headers = _office_headers() if method == "OPTIONS" else _auth()
        with _trap_lock_and_state(docker_manager), _trap_chat_fs(data, CHAT):
            response = http.request(method, path, headers=headers)
        _assert_reason(response, "office_disabled")
        assert _inventory(data) == before
        assert origin.hits == 0
        assert not (data / CHAT / ".ocu" / "office").exists()


@pytest.mark.parametrize("method,suffix", (
    ("POST", "sessions"), ("GET", "versions"),
))
def test_enabled_missing_chat_is_404_without_creating_directory(tmp_path, monkeypatch, method, suffix):
    with _office_app(tmp_path, monkeypatch, enabled=True) as (
        http,
        data,
        origin,
        docker_manager,
    ):
        before = _inventory(data)
        with _trap_lock_and_state(docker_manager):
            response = http.request(
                method, f"/api/office/{MISSING}/documents/{FILE_ID}/{suffix}",
                headers=_auth(),
            )
        _assert_reason(response, "unknown_chat")
        assert _inventory(data) == before
        assert not (data / MISSING).exists()
        assert origin.hits == 0


def test_enabled_existing_chat_unknown_file_is_not_unknown_route(tmp_path, monkeypatch):
    with _office_app(tmp_path, monkeypatch, enabled=True) as (
        http,
        data,
        origin,
        docker_manager,
    ):
        (data / CHAT).mkdir()
        with docker_manager._combined_lock(CHAT):
            pass
        before = _inventory(data)
        response = http.post(
            f"/api/office/{CHAT}/documents/{FILE_ID}/sessions",
            headers=_auth(),
        )
        assert response.status_code == 404
        assert json.loads(response.content) == {"reason": "unknown_file"}
        assert _inventory(data) == before
        assert origin.hits == 0


def test_enabled_existing_unknown_path_is_unknown_route(tmp_path, monkeypatch):
    with _office_app(tmp_path, monkeypatch, enabled=True) as (
        http,
        data,
        origin,
        docker_manager,
    ):
        (data / CHAT).mkdir()
        with _trap_lock_and_state(docker_manager):
            response = http.get(f"/api/office/{CHAT}/unknown", headers=_auth())
            custom = http.request(
                "CUSTOM", f"/api/office/{CHAT}/unknown", headers=_auth()
            )
            options = http.options(
                f"/api/office/{CHAT}/sessions/{SESSION_ID}",
                headers=_office_headers(),
            )
        _assert_reason(response, "unknown_route")
        _assert_reason(custom, "unknown_route")
        _assert_reason(options, "unknown_route")
        assert origin.hits == 0


def test_enabled_existing_chat_newline_suffix_is_unknown_route(tmp_path, monkeypatch):
    with _office_app(tmp_path, monkeypatch, enabled=True) as (
        http,
        data,
        origin,
        docker_manager,
    ):
        (data / CHAT).mkdir()
        before = _inventory(data)
        with _trap_lock_and_state(docker_manager):
            encoded = http.get(NEWLINE_ENCODED, headers=_auth())
            status, parsed = _raw_office(
                http.app,
                NEWLINE_DECODED,
                [(b"authorization", f"Bearer {INTERNAL}".encode("ascii"))],
            )
        _assert_reason(encoded, "unknown_route")
        assert status == 404
        assert parsed["reason"] == "unknown_route"
        assert _inventory(data) == before
        assert origin.hits == 0


def test_disabled_and_missing_chat_newline_suffix_keep_availability_reasons(
    tmp_path, monkeypatch
):
    with _office_app(tmp_path, monkeypatch, enabled=False) as (
        http,
        data,
        origin,
        docker_manager,
    ):
        before = _inventory(data)
        with _trap_lock_and_state(docker_manager), _trap_chat_fs(data, CHAT):
            disabled = http.get(NEWLINE_ENCODED, headers=_auth())
        _assert_reason(disabled, "office_disabled")
        assert _inventory(data) == before
        assert origin.hits == 0
    with _office_app(tmp_path / "missing", monkeypatch, enabled=True) as (
        http,
        data,
        origin,
        docker_manager,
    ):
        before = _inventory(data)
        with _trap_lock_and_state(docker_manager):
            missing = http.get(
                f"/api/office/{MISSING}/unknown%0Asegment",
                headers=_auth(),
            )
            unauth = http.get(NEWLINE_ENCODED)
        _assert_reason(missing, "unknown_chat")
        assert unauth.status_code == 401
        assert json.loads(unauth.content)["reason"] == "unauthorized"
        assert _inventory(data) == before
        assert not (data / MISSING).exists()
        assert origin.hits == 0


def test_enabled_existing_chat_denials_do_no_office_work(tmp_path, monkeypatch):
    with _office_app(tmp_path, monkeypatch, enabled=True) as (
        http,
        data,
        origin,
        docker_manager,
    ):
        (data / CHAT).mkdir()
        before = _inventory(data)
        with _trap_lock_and_state(docker_manager), _trap_chat_fs(data, CHAT):
            missing = http.get(f"/api/office/{CHAT}/unknown")
            wrong = http.get(
                f"/api/office/{CHAT}/unknown",
                headers=_auth("not-the-configured-secret"),
            )
            preflight = http.options(
                f"/api/office/{CHAT}/sessions/{SESSION_ID}",
                headers={
                    "Origin": "https://webui.example",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "Authorization",
                },
            )
            invalid = http.get(
                "/api/office/default/sessions/session",
                headers={**_auth(), "X-Chat-Id": CHAT},
                params={"chat_id": CHAT},
            )
        assert missing.status_code == 401
        assert json.loads(missing.content)["reason"] == "unauthorized"
        assert wrong.status_code == 401
        assert json.loads(wrong.content)["reason"] == "unauthorized"
        assert preflight.status_code == 401
        assert json.loads(preflight.content)["reason"] == "unauthorized"
        assert invalid.status_code == 400
        assert json.loads(invalid.content)["reason"] == "invalid_chat_id"
        with _trap_lock_and_state(docker_manager), _trap_chat_fs(data, CHAT):
            sandbox_status, sandbox_body = _raw_office(
                http.app,
                f"/api/office/{CHAT}/sessions/{SESSION_ID}",
                [(b"authorization", f"Bearer {INTERNAL}".encode("ascii"))],
                client=(SANDBOX_PEER, 40020),
            )
        assert sandbox_status == 403
        assert sandbox_body["reason"] == "forbidden"
        assert _inventory(data) == before
        assert origin.hits == 0
        assert not (data / CHAT / ".ocu" / "office").exists()



def test_availability_uses_canonical_chat_identity(tmp_path, monkeypatch):
    presented = quote(" Known-Chat ", safe="")
    canonical = "known-chat"
    with _office_app(tmp_path, monkeypatch, enabled=True) as (
        http,
        data,
        origin,
        docker_manager,
    ):
        (data / canonical).mkdir()
        assert not (data / " Known-Chat ").exists()
        with _trap_lock_and_state(docker_manager):
            response = http.get(
                f"/api/office/{presented}/unknown",
                headers=_auth(),
            )
        _assert_reason(response, "unknown_route")
        assert origin.hits == 0


def test_fresh_enabled_and_disabled_imports_in_one_run(tmp_path, monkeypatch):
    with _office_app(tmp_path / "one", monkeypatch, enabled=False) as (
        http,
        data,
        origin,
        docker_manager,
    ):
        with _trap_lock_and_state(docker_manager):
            response = http.get(f"/api/office/{CHAT}/unknown", headers=_auth())
        _assert_reason(response, "office_disabled")
        assert origin.hits == 0
        assert not (data / CHAT).exists()
    with _office_app(tmp_path / "two", monkeypatch, enabled=True) as (
        http,
        data,
        origin,
        docker_manager,
    ):
        (data / CHAT).mkdir()
        with _trap_lock_and_state(docker_manager):
            response = http.get(f"/api/office/{CHAT}/unknown", headers=_auth())
            missing = http.get(f"/api/office/{MISSING}/unknown", headers=_auth())
        _assert_reason(response, "unknown_route")
        _assert_reason(missing, "unknown_chat")
        assert origin.hits == 0
        assert not (data / MISSING).exists()



@pytest.mark.parametrize("method", ("GET", "OPTIONS"))
def test_non_post_creation_path_keeps_unknown_route_fallback(tmp_path, monkeypatch, method):
    with _office_app(tmp_path, monkeypatch, enabled=True) as (http, data, origin, docker_manager):
        (data / CHAT).mkdir()
        before = _inventory(data)
        with _trap_lock_and_state(docker_manager):
            response = http.request(
                method, f"/api/office/{CHAT}/documents/{FILE_ID}/sessions",
                headers=_office_headers() if method == "OPTIONS" else _auth(),
            )
        _assert_reason(response, "unknown_route")
        assert _inventory(data) == before
        assert origin.hits == 0
