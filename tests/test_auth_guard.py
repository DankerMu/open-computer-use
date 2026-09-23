# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Service-boundary behavior for the OCU auth guard.

Seams are the real FastAPI app, its mounted /mcp transport, WebSocket
handshakes, and the packaged multi-worker startup command. Expected statuses
and payloads come from the ocu-auth-guard fixture, not from reimplementing
the guard.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from urllib.parse import quote

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1] / "computer-use-server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

INTERNAL = "ocu-test-internal-token"
MCP_KEY = "ocu-test-mcp-api-key"
OTHER = "not-the-configured-secret"
ORIGIN = "https://webui.example"


def _subprocess_env():
    env = os.environ.copy()
    env["PUBLIC_BASE_URL"] = "/ocu"
    return env


SUBNET = "10.90.0.0/24"
SANDBOX_PEER = "10.90.0.8"
OUTSIDE_PEER = "10.90.1.8"
CHAT = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
CHAT_B = "b2c3d4e5-f6a7-8901-bcde-f12345678901"

# Representative chat-bound surfaces. One mutating upload plus reads that
# would touch the filesystem or resolve a container when unguarded.
CHAT_ROUTES = (
    ("GET", f"/api/outputs/{CHAT}"),
    ("GET", f"/api/uploads/{CHAT}/manifest"),
    ("GET", f"/api/uploads/{CHAT}/list"),
    ("GET", f"/files/{CHAT}/archive"),
    ("GET", f"/files/{CHAT}/test.txt"),
    ("GET", f"/browser/{CHAT}/status"),
    ("GET", f"/browser/{CHAT}/json"),
    ("GET", f"/browser/{CHAT}/json/version"),
    ("GET", f"/terminal/{CHAT}/status"),
    ("POST", f"/terminal/{CHAT}/start-ttyd"),
    ("POST", f"/terminal/{CHAT}/stop-ttyd"),
    ("POST", f"/terminal/{CHAT}/restart-container"),
    ("POST", f"/terminal/{CHAT}/resurrect-container"),
    ("GET", f"/terminal/{CHAT}/sessions"),
    ("GET", f"/terminal/{CHAT}/processes"),
    ("POST", f"/terminal/{CHAT}/processes/1/kill"),
    ("GET", f"/terminal/{CHAT}/heartbeat"),
    ("GET", f"/preview/{CHAT}"),
)

IDENTITY_ROUTES = ("/system-prompt", "/skill-list", "/skill-mounts")

INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "auth-guard-test", "version": "0"},
    },
}


def _apply_env(monkeypatch):
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL)
    monkeypatch.setenv("MCP_API_KEY", MCP_KEY)
    monkeypatch.setenv("OCU_WEBUI_ORIGIN", ORIGIN)
    monkeypatch.setenv("OCU_SANDBOX_SUBNET", SUBNET)
    monkeypatch.setenv("SINGLE_USER_MODE", "true")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://ocu.example")
    monkeypatch.setenv("BASE_DATA_DIR", "/tmp/ocu-auth-guard-unused")


@pytest.fixture
def app_module(monkeypatch):
    """Import the server after the guard env is set, once per test."""
    _apply_env(monkeypatch)
    for name in list(sys.modules):
        if name in {
            "app",
            "auth_guard",
            "mcp_tools",
            "docker_manager",
            "outputs_broker",
            "context_vars",
            "security",
            "system_prompt",
            "skill_manager",
        } or name.startswith("mcp_resources"):
            sys.modules.pop(name, None)
    import app as loaded

    return loaded


@pytest.fixture
def client(app_module, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    data = tmp_path / "data"
    chat_outputs = data / CHAT / "outputs"
    chat_uploads = data / CHAT / "uploads"
    chat_outputs.mkdir(parents=True)
    chat_uploads.mkdir(parents=True)
    (chat_outputs / "test.txt").write_text("hello-output")
    (chat_uploads / "uploaded.txt").write_text("hello-upload")
    monkeypatch.setattr(app_module, "BASE_DATA_DIR", data)
    import docker_manager

    monkeypatch.setattr(docker_manager, "BASE_DATA_DIR", data)
    with TestClient(app_module.app) as http:
        yield http


def _bearer(token=INTERNAL):
    return {"Authorization": f"Bearer {token}"}


def _mcp_headers(internal=INTERNAL, mcp=MCP_KEY, chat=CHAT, extra=None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "X-OCU-Internal-Token": internal,
        "Authorization": f"Bearer {mcp}",
        "X-Chat-Id": chat,
    }
    if extra:
        headers.update(extra)
    return headers


def _instructions(response):
    body = response.text
    if "text/event-stream" in response.headers.get("content-type", ""):
        for line in body.splitlines():
            if line.startswith("data:"):
                body = line[len("data:") :].strip()
                break
    payload = json.loads(body)
    return payload["result"]["instructions"]


def _peer_client(app, host, port):
    """Pinned Starlette hardcodes client=('testclient', 50000); wrap the peer."""
    from fastapi.testclient import TestClient

    async def _app(scope, receive, send):
        scope = dict(scope)
        scope["client"] = (host, port)
        await app(scope, receive, send)

    return TestClient(_app)


def _raw_http_response(app, path, headers, method="GET"):
    """Exercise the ASGI boundary with raw header bytes TestClient cannot send."""
    messages = []
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))
    start = next(message for message in messages if message["type"] == "http.response.start")
    response_headers = {
        name.lower(): value for name, value in start.get("headers") or []
    }
    return start["status"], response_headers



class TestStartupFailClosed:
    def test_packaged_command_without_token_parent_exits_nonzero(self):
        """The production multi-worker command itself must fail, not a child."""
        env = _subprocess_env()
        env.pop("OCU_INTERNAL_TOKEN", None)
        env["OCU_SANDBOX_SUBNET"] = SUBNET
        env["OCU_WEBUI_ORIGIN"] = ORIGIN
        env["MCP_API_KEY"] = MCP_KEY
        script = textwrap.dedent(
            """
            import os, socket, subprocess, sys, time
            os.chdir(sys.argv[1])
            sock = socket.socket(); sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()
            command = (
                "python -c 'import auth_guard,sys; sys.exit(auth_guard.startup_preflight())' "
                "&& exec python -m uvicorn app:app --host 127.0.0.1 --port %s "
                "--workers 2 --no-proxy-headers"
            ) % port
            proc = subprocess.Popen(["sh", "-c", command])
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                code = proc.poll()
                if code is not None:
                    sys.exit(0 if code != 0 else 2)
                time.sleep(0.1)
            alive = proc.poll() is None
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            sys.exit(3 if alive else 4)
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script, str(SERVER_DIR)],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert completed.returncode == 0, (
            f"parent stayed up or exited zero: rc={completed.returncode}\n"
            f"stdout={completed.stdout[-500:]}\nstderr={completed.stderr[-800:]}"
        )

    def test_trailing_public_base_stops_the_packaged_parent_before_listening(self):
        env = _subprocess_env()
        env["OCU_INTERNAL_TOKEN"] = INTERNAL
        env["MCP_API_KEY"] = MCP_KEY
        env["OCU_SANDBOX_SUBNET"] = SUBNET
        env["OCU_WEBUI_ORIGIN"] = ORIGIN
        env["PUBLIC_BASE_URL"] = "https://webui.example/ocu/"
        script = textwrap.dedent(
            """
            import os, socket, subprocess, sys, time
            os.chdir(sys.argv[1])
            sock = socket.socket(); sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()
            command = (
                "python -c 'import auth_guard,sys; sys.exit(auth_guard.startup_preflight())' "
                "&& exec python -m uvicorn app:app --host 0.0.0.0 --port %s "
                "--workers 2 --no-proxy-headers"
            ) % port
            proc = subprocess.Popen(
                ["sh", "-c", command],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                code = proc.poll()
                if code is not None:
                    stdout, stderr = proc.communicate()
                    sys.exit(
                        0
                        if code != 0 and "PUBLIC_BASE_URL" in stdout + stderr
                        else 2
                    )
                time.sleep(0.1)
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            sys.exit(3)
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script, str(SERVER_DIR)],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert completed.returncode == 0, (
            "packaged command did not reject trailing PUBLIC_BASE_URL before "
            f"listening: rc={completed.returncode}"
        )

    def test_malformed_subnet_prevents_packaged_startup(self):
        env = _subprocess_env()
        env["OCU_INTERNAL_TOKEN"] = INTERNAL
        env["OCU_SANDBOX_SUBNET"] = "not-a-cidr"
        env["OCU_WEBUI_ORIGIN"] = ORIGIN
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import auth_guard; raise SystemExit(auth_guard.startup_preflight())",
            ],
            cwd=SERVER_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert completed.returncode != 0

    def test_malformed_origin_prevents_packaged_startup(self):
        env = _subprocess_env()
        env["OCU_INTERNAL_TOKEN"] = INTERNAL
        env["OCU_WEBUI_ORIGIN"] = "webui.example"
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import auth_guard; raise SystemExit(auth_guard.startup_preflight())",
            ],
            cwd=SERVER_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert completed.returncode != 0

    @pytest.mark.parametrize(
        "token",
        (
            f" {INTERNAL}",
            f"{INTERNAL} ",
            f"{INTERNAL}\n",
            f"{INTERNAL}\x7f",
            f"{INTERNAL}\u00e9",
        ),
    )
    def test_malformed_internal_token_prevents_startup(self, token):
        env = _subprocess_env()
        env["OCU_INTERNAL_TOKEN"] = token
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import auth_guard; raise SystemExit(auth_guard.startup_preflight())",
            ],
            cwd=SERVER_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert completed.returncode != 0
        assert "OCU_INTERNAL_TOKEN" in completed.stderr


    def test_root_relative_public_base_preflight_succeeds(self):
        env = _subprocess_env()
        env["OCU_INTERNAL_TOKEN"] = INTERNAL
        env["OCU_SANDBOX_SUBNET"] = SUBNET
        env["OCU_WEBUI_ORIGIN"] = ORIGIN
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import auth_guard; raise SystemExit(auth_guard.startup_preflight())",
            ],
            cwd=SERVER_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert completed.returncode == 0, completed.stderr[-500:]

    def test_packaged_command_with_valid_public_base_serves_guarded_prompt_and_mcp(self):
        env = _subprocess_env()
        env["OCU_INTERNAL_TOKEN"] = INTERNAL
        env["MCP_API_KEY"] = MCP_KEY
        env["OCU_SANDBOX_SUBNET"] = SUBNET
        env["OCU_WEBUI_ORIGIN"] = ORIGIN
        env["PUBLIC_BASE_URL"] = "https://webui.example/ocu"
        script = textwrap.dedent(
            """
            import json, os, socket, subprocess, sys, time, urllib.request
            os.chdir(sys.argv[1])
            sock = socket.socket(); sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()
            command = (
                "python -c 'import auth_guard,sys; sys.exit(auth_guard.startup_preflight())' "
                "&& exec python -m uvicorn app:app --host 0.0.0.0 --port %s "
                "--workers 2 --no-proxy-headers"
            ) % port
            proc = subprocess.Popen(["sh", "-c", command])
            try:
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        sys.exit(2)
                    try:
                        with urllib.request.urlopen(
                            "http://127.0.0.1:%s/health" % port, timeout=0.5
                        ) as response:
                            if response.status == 200:
                                break
                    except OSError:
                        time.sleep(0.1)
                else:
                    sys.exit(3)
                prompt_request = urllib.request.Request(
                    "http://127.0.0.1:%s/system-prompt?chat_id=startup-health-chat" % port,
                    headers={"Authorization": "Bearer " + sys.argv[2]},
                )
                with urllib.request.urlopen(prompt_request, timeout=5) as response:
                    prompt = response.read()
                    public_base = response.headers.get("X-Public-Base-URL")
                if (
                    public_base != sys.argv[4]
                    or (sys.argv[4] + "/files/startup-health-chat").encode() not in prompt
                ):
                    sys.exit(5)
                body = json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                    "clientInfo": {"name": "startup-test", "version": "0"}},
                }).encode()
                request = urllib.request.Request(
                    "http://127.0.0.1:%s/mcp" % port,
                    data=body,
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                        "X-OCU-Internal-Token": sys.argv[2],
                        "Authorization": "Bearer " + sys.argv[3],
                        "X-Chat-Id": "startup-health-chat",
                    },
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    payload = response.read()
                sys.exit(0 if b"instructions" in payload else 4)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            """
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(SERVER_DIR),
                INTERNAL,
                MCP_KEY,
                "https://webui.example/ocu",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=25,
        )
        assert completed.returncode == 0, (
            f"packaged command did not serve guarded prompt and MCP: rc={completed.returncode}\n"
            f"stdout={completed.stdout[-500:]}\nstderr={completed.stderr[-800:]}"
        )


class TestHttpAuthorization:
    @pytest.mark.parametrize("method,path", CHAT_ROUTES)
    def test_chat_route_without_token_is_401_before_work(self, client, tmp_path, method, path):
        before = {p.name for p in tmp_path.rglob("*")}
        response = client.request(method, path)
        after = {p.name for p in tmp_path.rglob("*")}
        assert response.status_code == 401
        assert INTERNAL not in response.text
        assert after == before

    def test_wrong_bearer_is_401(self, client):
        response = client.get(f"/api/outputs/{CHAT}", headers=_bearer(OTHER))
        assert response.status_code == 401

    def test_token_in_query_does_not_authorize(self, client):
        response = client.get(f"/api/outputs/{CHAT}?access_token={INTERNAL}")
        assert response.status_code == 401

    def test_authorized_outputs_keep_payload_and_traversal_rejection(self, client):
        ok = client.get(f"/api/outputs/{CHAT}", headers=_bearer())
        assert ok.status_code == 200
        body = ok.json()
        assert body["chat_id"] == CHAT
        assert body["total"] == 1
        assert body["files"][0]["name"] == "test.txt"
        assert body["files"][0]["path"] == "test.txt"
        denied = client.get("/api/outputs/..test..", headers=_bearer())
        assert denied.status_code == 400

    def test_authorized_download_returns_file_bytes(self, client):
        response = client.get(f"/files/{CHAT}/test.txt", headers=_bearer())
        assert response.status_code == 200
        assert response.text == "hello-output"

    def test_authorized_upload_writes_only_after_token(self, client, tmp_path):
        denied = client.post(
            f"/api/uploads/{CHAT}/new.txt",
            files={"file": ("new.txt", b"secret-bytes")},
        )
        assert denied.status_code == 401
        assert not (tmp_path / "data" / CHAT / "uploads" / "new.txt").exists()
        allowed = client.post(
            f"/api/uploads/{CHAT}/new.txt",
            headers=_bearer(),
            files={"file": ("new.txt", b"secret-bytes")},
        )
        assert allowed.status_code == 200
        assert allowed.json()["filename"] == "new.txt"
        assert (tmp_path / "data" / CHAT / "uploads" / "new.txt").read_bytes() == b"secret-bytes"

    @pytest.mark.parametrize("path", IDENTITY_ROUTES)
    def test_identity_without_token_is_401(self, client, path):
        response = client.get(path, params={"user_email": "user@example.com", "chat_id": CHAT})
        assert response.status_code == 401

    def test_identity_without_chat_keeps_diagnostic_prompt(self, client):
        response = client.get("/system-prompt", headers=_bearer())
        assert response.status_code == 200
        assert "{chat_id}" in response.text or "{file_base_url}" in response.text
        assert response.headers["x-public-base-url"] == "http://ocu.example"

    def test_identity_header_alias_is_trusted_only_with_token(self, client):
        response = client.get(
            "/system-prompt",
            headers={**_bearer(), "X-OpenWebUI-Chat-Id": CHAT},
        )
        assert response.status_code == 200
        assert f"/files/{CHAT}" in response.text

    def test_supplied_invalid_identity_chat_is_rejected(self, client):
        response = client.get(
            "/system-prompt",
            headers={**_bearer(), "X-Chat-Id": "temporary:abc"},
        )
        assert response.status_code == 400

    @pytest.mark.parametrize("path", IDENTITY_ROUTES)
    def test_each_identity_endpoint_rejects_supplied_invalid_alias(self, client, path):
        response = client.get(
            path,
            headers={**_bearer(), "X-OpenWebUI-Chat-Id": "channel:temporary"},
        )
        assert response.status_code == 400

    @pytest.mark.parametrize("path", IDENTITY_ROUTES)
    @pytest.mark.parametrize("chat_id", ("default", quote("temporary:abc", safe="")))
    def test_each_identity_endpoint_rejects_supplied_invalid_query_chat_id(
        self, client, path, chat_id
    ):
        response = client.get(
            path,
            headers=_bearer(),
            params={"chat_id": chat_id},
        )
        assert response.status_code == 400

    def test_system_prompt_rejects_invalid_legacy_file_base_url_chat_id(self, client):
        response = client.get(
            "/system-prompt",
            headers=_bearer(),
            params={"file_base_url": "https://legacy.example/files/temporary%3Aabc"},
        )
        assert response.status_code == 400


    @pytest.mark.parametrize("path", ("/health", "/api/runtime/cli", "/", "/static/preview.js"))
    def test_public_routes_stay_available_without_token(self, client, path):
        response = client.get(path)
        assert response.status_code == 200

    def test_health_from_sandbox_peer_is_403(self, app_module):
        with _peer_client(app_module.app, SANDBOX_PEER, 40000) as http:
            response = http.get("/health", headers={"X-Forwarded-For": "203.0.113.9"})
        assert response.status_code == 403

    def test_sandbox_peer_forwarded_header_does_not_bypass(self, app_module):
        with _peer_client(app_module.app, SANDBOX_PEER, 40001) as http:
            response = http.get(
                f"/api/outputs/{CHAT}",
                headers={**_bearer(), "X-Forwarded-For": "203.0.113.9"},
            )
        assert response.status_code == 403

    def test_outside_peer_with_token_is_not_denied_as_sandbox(self, app_module, tmp_path, monkeypatch):
        data = tmp_path / "outside"
        (data / CHAT / "outputs").mkdir(parents=True)
        monkeypatch.setattr(app_module, "BASE_DATA_DIR", data)
        import docker_manager

        monkeypatch.setattr(docker_manager, "BASE_DATA_DIR", data)
        with _peer_client(app_module.app, OUTSIDE_PEER, 40002) as http:
            response = http.get(f"/api/outputs/{CHAT}", headers=_bearer())
        assert response.status_code == 200

    def test_alternate_method_on_guarded_route_still_requires_token(self, client):
        response = client.put(f"/api/outputs/{CHAT}")
        assert response.status_code == 401

    def test_missing_origin_grants_no_cross_origin_header(self, client, monkeypatch):
        monkeypatch.delenv("OCU_WEBUI_ORIGIN", raising=False)
        response = client.get(
            "/health",
            headers={"Origin": "https://foreign.example"},
        )
        assert "access-control-allow-origin" not in {
            k.lower(): v for k, v in response.headers.items()
        }

    def test_configured_origin_is_explicit_and_foreign_origin_is_not(self, client):
        allowed = client.get(f"/api/outputs/{CHAT}", headers={**_bearer(), "Origin": ORIGIN})
        allowed_preflight = client.options(
            f"/api/outputs/{CHAT}",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization",
            },
        )
        foreign = client.get(
            f"/api/outputs/{CHAT}",
            headers={**_bearer(), "Origin": "https://foreign.example"},
        )
        preflight = client.options(
            f"/api/outputs/{CHAT}",
            headers={
                "Origin": "https://foreign.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert allowed.headers.get("access-control-allow-origin") == ORIGIN
        assert allowed_preflight.status_code == 204
        assert allowed_preflight.headers.get("access-control-allow-origin") == ORIGIN
        assert "authorization" in allowed_preflight.headers.get("access-control-allow-headers", "").lower()
        assert foreign.headers.get("access-control-allow-origin") != "https://foreign.example"
        assert foreign.headers.get("access-control-allow-origin") != "*"
        assert preflight.headers.get("access-control-allow-origin") != "https://foreign.example"
        assert preflight.headers.get("access-control-allow-origin") != "*"



class TestMalformedHeaderBytes:
    def test_non_ascii_bearer_bytes_are_401(self, app_module):
        status, _ = _raw_http_response(
            app_module.app,
            f"/api/outputs/{CHAT}",
            [(b"authorization", b"Bearer \xff")],
        )
        assert status == 401

    def test_non_ascii_internal_header_bytes_are_401(self, app_module):
        status, _ = _raw_http_response(
            app_module.app,
            "/mcp",
            [(b"x-ocu-internal-token", b"\xff")],
            method="POST",
        )
        assert status == 401

    @pytest.mark.parametrize(
        ("path", "headers", "method", "expected_status"),
        (
            ("/health", [(b"origin", b"https://foreign.\xff")], "GET", 200),
            (
                f"/api/outputs/{CHAT}",
                [(b"origin", b"https://foreign.\xff")],
                "GET",
                401,
            ),
            (
                f"/api/outputs/{CHAT}",
                [
                    (b"origin", b"https://foreign.\xff"),
                    (b"access-control-request-method", b"GET"),
                ],
                "OPTIONS",
                403,
            ),
        ),
    )
    def test_non_ascii_origin_never_grants_cors(
        self, app_module, path, headers, method, expected_status
    ):
        status, response_headers = _raw_http_response(
            app_module.app,
            path,
            headers,
            method=method,
        )
        assert status == expected_status
        assert b"access-control-allow-origin" not in response_headers


class TestUnsafeChatIds:
    @pytest.mark.parametrize(
        "chat_id",
        [
            "default",
            "DEFAULT",
            " temporary:abc ",
            "Temporary:ABC",
            "local:abc",
            "LOCAL:abc",
            "channel:abc",
            "channel:ABC",
            quote("temporary:abc", safe=""),
        ],
    )
    def test_http_rejects_normalized_invalid_ids(self, client, chat_id, tmp_path):
        before = list(tmp_path.rglob("*"))
        response = client.get(f"/api/outputs/{chat_id}", headers=_bearer())
        assert response.status_code == 400
        assert list(tmp_path.rglob("*")) == before

    def test_single_user_mode_does_not_remap_valid_id(self, client):
        response = client.get(f"/api/outputs/{CHAT}", headers=_bearer())
        assert response.status_code == 200
        assert response.json()["chat_id"] == CHAT

class TestWebSockets:
    def test_cdp_without_token_is_rejected_before_upgrade(self, client, app_module, monkeypatch):
        calls = []
        monkeypatch.setattr(
            app_module,
            "get_container_service_address",
            lambda *_a, **_k: calls.append("address"),
        )
        with pytest.raises(Exception):
            with client.websocket_connect(f"/browser/{CHAT}/devtools/page/page-1"):
                pass
        assert calls == []

    def test_ttyd_without_token_is_rejected_before_upgrade(self, client, app_module, monkeypatch):
        calls = []
        monkeypatch.setattr(
            app_module,
            "get_container_service_address",
            lambda *_a, **_k: calls.append("address"),
        )
        with pytest.raises(Exception):
            with client.websocket_connect(f"/terminal/{CHAT}/ws"):
                pass
        assert calls == []

    def test_ttyd_invalid_id_is_rejected_before_upgrade(self, client, app_module, monkeypatch):
        calls = []
        monkeypatch.setattr(
            app_module,
            "get_container_service_address",
            lambda *_a, **_k: calls.append("address"),
        )
        with pytest.raises(Exception):
            with client.websocket_connect(
                "/terminal/temporary:abc/ws",
                headers=_bearer(),
            ):
                pass
        assert calls == []

    def test_ttyd_with_token_reaches_backend_after_upgrade(self, client, app_module, monkeypatch):
        from starlette.websockets import WebSocketDisconnect

        calls = []

        class FailingBackend:
            async def __aenter__(self):
                raise RuntimeError("backend unavailable")

            async def __aexit__(self, *_exc):
                return False

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            def ws_connect(self, *_args, **_kwargs):
                calls.append("backend")
                return FailingBackend()

        monkeypatch.setattr(
            app_module,
            "get_container_service_address",
            lambda *_a, **_k: calls.append("address") or "sandbox.test:7681",
        )
        monkeypatch.setattr(app_module.aiohttp, "ClientSession", Session)
        with client.websocket_connect(f"/terminal/{CHAT}/ws", headers=_bearer()) as socket:
            with pytest.raises(WebSocketDisconnect) as exc:
                socket.receive_text()
        assert calls == ["address", "backend"]
        assert exc.value.code == 1011


class TestMountedMcp:
    def test_mcp_without_internal_token_is_401_even_with_api_key(self, client):
        empty = client.post(
            "/mcp",
            headers=_mcp_headers(internal=""),
            json=INIT_BODY,
        )
        assert empty.status_code == 401
        assert INTERNAL not in empty.text
        headers = _mcp_headers()
        headers.pop("X-OCU-Internal-Token")
        missing = client.post("/mcp", headers=headers, json=INIT_BODY)
        assert missing.status_code == 401
        assert INTERNAL not in missing.text

    def test_mcp_without_api_key_is_401_even_with_internal_token(self, client):
        headers = _mcp_headers()
        headers.pop("Authorization")
        response = client.post("/mcp", headers=headers, json=INIT_BODY)
        assert response.status_code == 401

    def test_mcp_without_api_key_keeps_internal_token_mandatory(self, client, monkeypatch):
        monkeypatch.delenv("MCP_API_KEY", raising=False)
        headers = _mcp_headers()
        headers.pop("Authorization")
        allowed = client.post("/mcp", headers=headers, json=INIT_BODY)
        assert allowed.status_code == 200
        assert f"/files/{CHAT}" in _instructions(allowed)

        missing = dict(headers)
        missing.pop("X-OCU-Internal-Token")
        assert client.post("/mcp", headers=missing, json=INIT_BODY).status_code == 401

        wrong = _mcp_headers(internal=OTHER)
        wrong.pop("Authorization")
        assert client.post("/mcp", headers=wrong, json=INIT_BODY).status_code == 401

    def test_credentials_do_not_substitute(self, client):
        internal_as_bearer = _mcp_headers(mcp=INTERNAL)
        api_key_as_internal = _mcp_headers(internal=MCP_KEY)
        assert client.post("/mcp", headers=internal_as_bearer, json=INIT_BODY).status_code == 401
        assert client.post("/mcp", headers=api_key_as_internal, json=INIT_BODY).status_code == 401

    def test_wrong_secret_is_401(self, client):
        assert (
            client.post("/mcp", headers=_mcp_headers(internal=OTHER), json=INIT_BODY).status_code
            == 401
        )
        assert (
            client.post("/mcp", headers=_mcp_headers(mcp=OTHER), json=INIT_BODY).status_code == 401
        )

    def test_alias_without_internal_token_is_401(self, client):
        headers = _mcp_headers()
        headers.pop("X-OCU-Internal-Token")
        headers.pop("X-Chat-Id")
        headers["X-OpenWebUI-Chat-Id"] = CHAT
        headers["X-OpenWebUI-User-Email"] = "user@example.com"
        response = client.post("/mcp", headers=headers, json=INIT_BODY)
        assert response.status_code == 401


    def test_mcp_requires_an_explicit_non_default_chat_id(self, client):
        headers = _mcp_headers()
        headers.pop("X-Chat-Id")
        response = client.post("/mcp", headers=headers, json=INIT_BODY)
        assert response.status_code == 400

    def test_authenticated_openwebui_alias_is_trusted_for_this_request(self, client):
        headers = _mcp_headers()
        headers.pop("X-Chat-Id")
        headers["X-OpenWebUI-Chat-Id"] = CHAT
        response = client.post("/mcp", headers=headers, json=INIT_BODY)
        assert response.status_code == 200
        assert f"/files/{CHAT}" in _instructions(response)
    def test_invalid_chat_never_renders_prompt(self, client, monkeypatch):
        calls = []

        async def _render(*_a, **_k):
            calls.append("render")
            return "should-not-run"

        monkeypatch.setattr("system_prompt.render_system_prompt", _render)
        headers = _mcp_headers(chat="temporary:abc")
        response = client.post("/mcp", headers=headers, json=INIT_BODY)
        assert response.status_code == 400
        assert calls == []

    def test_authenticated_initialize_trusts_only_this_request(self, client):
        first = client.post("/mcp", headers=_mcp_headers(chat=CHAT), json=INIT_BODY)
        second = client.post("/mcp", headers=_mcp_headers(chat=CHAT_B), json=INIT_BODY)
        assert first.status_code == 200, first.text[:300]
        assert second.status_code == 200, second.text[:300]
        first_text = _instructions(first)
        second_text = _instructions(second)
        assert f"/files/{CHAT}" in first_text
        assert f"/files/{CHAT_B}" in second_text
        assert CHAT_B not in first_text
        assert f"/files/{CHAT}" not in second_text or CHAT in CHAT_B

    def test_context_does_not_survive_a_failed_request(self, client):
        bad = client.post(
            "/mcp",
            headers=_mcp_headers(chat="local:leftover"),
            json=INIT_BODY,
        )
        assert bad.status_code == 400
        good = client.post("/mcp", headers=_mcp_headers(chat=CHAT), json=INIT_BODY)
        assert good.status_code == 200
        text = _instructions(good)
        assert "local:leftover" not in text
        assert "local%3Aleftover" not in text.lower()

    def test_single_user_mode_keeps_explicit_chat(self, client):
        response = client.post("/mcp", headers=_mcp_headers(chat=CHAT), json=INIT_BODY)
        assert response.status_code == 200
        text = _instructions(response)
        assert f"/files/{CHAT}" in text
        assert "/files/default" not in text

    def test_get_mcp_requires_internal_token(self, client):
        headers = _mcp_headers()
        headers.pop("X-OCU-Internal-Token")
        response = client.get("/mcp", headers=headers)
        assert response.status_code == 401


class TestNoDisclosure:
    def test_401_body_and_logs_omit_token(self, client, capsys):
        response = client.get(f"/preview/{CHAT}")
        captured = capsys.readouterr()
        assert response.status_code == 401
        assert INTERNAL not in response.text
        assert INTERNAL not in response.headers.get("www-authenticate", "")
        assert INTERNAL not in captured.out
        assert INTERNAL not in captured.err
