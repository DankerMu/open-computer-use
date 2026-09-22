# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Tests for computer_use_tools (Open WebUI Tool).

Run: python -m pytest tests/test_tools.py -v
"""

import sys
import unittest
import asyncio
import hashlib
import json
import socket
import threading
import time
import types
from contextlib import asynccontextmanager

import pytest
import requests
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "openwebui" / "tools"))
SERVER_DIR = ROOT / "computer-use-server"
sys.path.insert(0, str(SERVER_DIR))


import computer_use_tools  # noqa: E402


class ValveSchema(unittest.TestCase):
    """v4.0.0: Tool Valve renamed FILE_SERVER_URL → ORCHESTRATOR_URL for
    consistency with the filter. Semantics unchanged — still the internal URL
    of the Computer Use server for MCP forwarding.
    """

    def test_orchestrator_url_valve_exists(self):
        valve_fields = set(computer_use_tools.Tools.Valves.model_fields.keys())
        self.assertIn("ORCHESTRATOR_URL", valve_fields)

    def test_file_server_url_valve_removed(self):
        valve_fields = set(computer_use_tools.Tools.Valves.model_fields.keys())
        self.assertNotIn("FILE_SERVER_URL", valve_fields)

def test_browser_valve_payload_excludes_internal_token(monkeypatch):
    configured_token = "test-server-only-internal-token"
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", configured_token)

    valves = computer_use_tools.Tools.Valves()
    browser_payload = json.dumps(
        {"schema": valves.model_json_schema(), "values": valves.model_dump()}
    )

    assert "OCU_INTERNAL_TOKEN" not in browser_payload
    assert configured_token not in browser_payload

INTERNAL_TOKEN = "test-internal-token"
MCP_API_KEY = "test-mcp-key"


async def _collect(events, event):
    events.append(event)


def _call_with_empty_chat(tools, method, metadata, emitter):
    kwargs = {
        "__event_emitter__": emitter,
        "__metadata__": metadata,
        "__files__": [{"name": "input.txt", "path": "/unused/input.txt"}],
    }
    if method == "bash_tool":
        return tools.bash_tool("/mnt/user-data/uploads/input.txt", "read upload", **kwargs)
    if method == "str_replace":
        return tools.str_replace("edit", "before", "/home/assistant/a.txt", "after", **kwargs)
    if method == "create_file":
        return tools.create_file("create", "contents", "/home/assistant/a.txt", **kwargs)
    if method == "view":
        return tools.view("read", "/mnt/user-data/uploads/input.txt", **kwargs)
    return tools.sub_agent("inspect upload", "delegate", **kwargs)


@pytest.mark.parametrize(
    "metadata",
    (None, {"chat_id": None}, {"chat_id": ""}, {"chat_id": " \t"}),
    ids=("absent", "none", "empty", "whitespace"),
)
@pytest.mark.parametrize(
    "method", ("bash_tool", "str_replace", "create_file", "view", "sub_agent")
)
def test_empty_chat_rejects_every_public_tool_before_work(monkeypatch, method, metadata):
    calls, events = [], []

    def blocked(*args, **kwargs):
        calls.append((args, kwargs))
        raise OSError("network must not run")

    monkeypatch.setattr(requests, "get", blocked)
    monkeypatch.setattr(requests, "post", blocked)
    monkeypatch.setattr(computer_use_tools.urllib.request, "urlopen", blocked)
    tools = computer_use_tools.Tools()
    result = asyncio.run(
        _call_with_empty_chat(tools, method, metadata, lambda e: _collect(events, e))
    )

    assert result.startswith("[TOOL ERROR]")
    assert "chat" in result.lower()
    assert calls == []
    assert events[-1]["data"]["status"] == "error"
    assert events[-1]["data"]["done"] is True


@pytest.mark.parametrize(
    ("internal", "mcp_key", "label", "hidden"),
    (
        ("", "", "OCU_INTERNAL_TOKEN", None),
        ("invalid internal token", "", "OCU_INTERNAL_TOKEN", "invalid internal token"),
        (INTERNAL_TOKEN, "invalid mcp key", "MCP_API_KEY", "invalid mcp key"),
    ),
)
def test_unusable_credentials_reject_before_upload_or_probe(
    monkeypatch, internal, mcp_key, label, hidden
):
    calls, events = [], []

    def blocked(*args, **kwargs):
        calls.append((args, kwargs))
        raise OSError("network must not run")

    monkeypatch.setenv("OCU_INTERNAL_TOKEN", internal)
    monkeypatch.setattr(requests, "get", blocked)
    monkeypatch.setattr(requests, "post", blocked)
    monkeypatch.setattr(computer_use_tools.urllib.request, "urlopen", blocked)
    tools = computer_use_tools.Tools()
    tools.valves.MCP_API_KEY = mcp_key
    result = asyncio.run(
        tools.bash_tool(
            "/mnt/user-data/uploads/input.txt",
            "read upload",
            __event_emitter__=lambda e: _collect(events, e),
            __metadata__={"chat_id": "chat-credentials"},
            __files__=[{"name": "input.txt", "path": "/unused/input.txt"}],
        )
    )

    assert result.startswith("[CONFIG ERROR]")
    assert label in result
    if hidden:
        assert hidden not in result
    assert calls == []
    assert events[-1]["data"]["status"] == "error"
    assert events[-1]["data"]["done"] is True


class _GuardedOCU:
    def __init__(self, remote_manifest):
        import auth_guard

        self.requests = []
        self.server = None
        self.thread = None
        self.mcp = FastMCP(
            "tool-auth-test", streamable_http_path="/", stateless_http=True
        )

        @self.mcp.tool()
        async def bash_tool(command: str, description: str) -> str:
            return f"ran:{command}"

        mcp_app = self.mcp.streamable_http_app()

        @asynccontextmanager
        async def lifespan(app):
            async with self.mcp.session_manager.run():
                yield

        async def health(request):
            return PlainTextResponse("healthy")

        async def manifest_endpoint(request):
            return JSONResponse(remote_manifest)

        async def upload(request):
            return JSONResponse({"status": "ok"})

        app = Starlette(
            routes=[
                Route("/health", health),
                Route("/api/uploads/{chat_id}/manifest", manifest_endpoint),
                Route("/api/uploads/{chat_id}/{filename}", upload, methods=["POST"]),
            ],
            lifespan=lifespan,
        )

        async def dispatch(scope, receive, send):
            if scope["type"] == "http" and scope["path"] == "/mcp":
                mcp_scope = dict(scope)
                mcp_scope["path"] = "/"
                mcp_scope["raw_path"] = b"/"
                await mcp_app(mcp_scope, receive, send)
                return
            await app(scope, receive, send)

        async def record(scope, receive, send):
            if scope["type"] == "http":
                self.requests.append(
                    {
                        "method": scope["method"],
                        "path": scope["path"],
                        "headers": {
                            name.decode("latin-1").lower(): value.decode("latin-1")
                            for name, value in scope.get("headers", [])
                        },
                    }
                )
            await dispatch(scope, receive, send)

        self.app = auth_guard.AuthGuardMiddleware(record)

    def start(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        self.port = listener.getsockname()[1]
        listener.close()
        self.server = uvicorn.Server(
            uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        if not self.server.started:
            raise RuntimeError("test OCU server did not start")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"




def _with_guard(manifest):
    class GuardContext:
        def __enter__(self):
            self.server = _GuardedOCU(manifest)
            self.server.start()
            return self.server

        def __exit__(self, *args):
            self.server.stop()

    return GuardContext()


def _install_storage(monkeypatch, paths):
    provider = types.ModuleType("open_webui.storage.provider")
    provider.Storage = type(
        "Storage", (), {"get_file": staticmethod(lambda source: str(paths[source]))}
    )
    storage = types.ModuleType("open_webui.storage")
    storage.__path__ = []
    package = types.ModuleType("open_webui")
    package.__path__ = []
    monkeypatch.setitem(sys.modules, "open_webui", package)
    monkeypatch.setitem(sys.modules, "open_webui.storage", storage)
    monkeypatch.setitem(sys.modules, "open_webui.storage.provider", provider)


def _records(server, path, **headers):
    return [
        record
        for record in server.requests
        if record["path"] == path
        and all(record["headers"].get(name) == value for name, value in headers.items())
    ]

def _disable_proxy_env(monkeypatch):
    for name in ("ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")



def test_tool_authenticates_real_transports_and_rotates_identity(monkeypatch, tmp_path):
    first_token, second_token = "test-internal-one", "test-internal-two"
    existing = tmp_path / "existing.tmp"
    uploaded = tmp_path / "uploaded.tmp"
    existing.write_bytes(b"already uploaded")
    uploaded.write_bytes(b"new upload")
    _install_storage(
        monkeypatch, {"existing-source": existing, "uploaded-source": uploaded}
    )
    _disable_proxy_env(monkeypatch)
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", first_token)
    monkeypatch.setenv("MCP_API_KEY", MCP_API_KEY)
    with _with_guard({"existing.txt": hashlib.md5(existing.read_bytes()).hexdigest()}) as ocu:
        tools = computer_use_tools.Tools()
        tools.valves.ORCHESTRATOR_URL = ocu.url
        tools.valves.MCP_API_KEY = MCP_API_KEY
        first = asyncio.run(
            tools.bash_tool(
                "cat /mnt/user-data/uploads/uploaded.txt",
                "read upload",
                __metadata__={"chat_id": "chat-one"},
                __user__={"email": "first@example.test", "name": "First User"},
                __request__=types.SimpleNamespace(
                    headers={"X-User-Email": "forged@example.test"}
                ),
                __files__=[
                    {"name": "existing.txt", "path": "existing-source"},
                    {"name": "uploaded.txt", "path": "uploaded-source"},
                ],
            )
        )
        first_records = list(ocu.requests)
        assert "ran:cat /mnt/user-data/uploads/uploaded.txt" in first
        assert not existing.exists()
        assert not uploaded.exists()

        health = _records(ocu, "/health")
        manifest = _records(ocu, "/api/uploads/chat-one/manifest")
        upload = _records(ocu, "/api/uploads/chat-one/uploaded.txt")
        probe = _records(ocu, "/mcp", **{"x-chat-id": "preflight"})
        first_call = _records(ocu, "/mcp", **{"x-user-email": "first@example.test"})
        assert health and all(r["headers"].get("authorization") == f"Bearer {first_token}" for r in health)
        assert manifest and upload
        assert all(r["headers"].get("authorization") == f"Bearer {first_token}" for r in manifest + upload)
        assert probe and first_call
        assert all(r["headers"].get("x-ocu-internal-token") == first_token for r in probe + first_call)
        assert all(r["headers"].get("authorization") == f"Bearer {MCP_API_KEY}" for r in probe + first_call)
        assert all(r["headers"].get("x-user-email") != "forged@example.test" for r in first_call)
        assert not _records(ocu, "/api/uploads/chat-one/existing.txt")

        mcp_headers = {
            name: value
            for name, value in first_call[0]["headers"].items()
            if name != "x-ocu-internal-token"
        }
        manifest_headers = {
            name: value for name, value in manifest[0]["headers"].items() if name != "authorization"
        }
        assert requests.post(f"{ocu.url}/mcp", headers=mcp_headers, json={}).status_code == 401
        assert requests.get(f"{ocu.url}{manifest[0]['path']}", headers=manifest_headers).status_code == 401

        monkeypatch.setenv("OCU_INTERNAL_TOKEN", second_token)
        second = asyncio.run(
            tools.bash_tool(
                "echo second",
                "second call",
                __metadata__={"chat_id": "chat-two"},
                __user__={"email": "second@example.test"},
            )
        )
        assert "ran:echo second" in second
        second_call = _records(ocu, "/mcp", **{"x-user-email": "second@example.test"})
        assert second_call
        assert all(r["headers"].get("x-ocu-internal-token") == second_token for r in second_call)
        assert len(ocu.requests) > len(first_records)


def test_tool_omits_optional_mcp_bearer_when_unconfigured(monkeypatch):
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
    monkeypatch.delenv("MCP_API_KEY", raising=False)
    _disable_proxy_env(monkeypatch)
    with _with_guard({}) as ocu:
        tools = computer_use_tools.Tools()
        tools.valves.ORCHESTRATOR_URL = ocu.url
        result = asyncio.run(
            tools.bash_tool(
                "echo optional",
                "optional auth",
                __metadata__={"chat_id": "chat-optional"},
                __user__={"email": "optional@example.test"},
            )
        )
        calls = _records(ocu, "/mcp", **{"x-user-email": "optional@example.test"})
        assert "ran:echo optional" in result
        assert calls
        assert all(r["headers"].get("x-ocu-internal-token") == INTERNAL_TOKEN for r in calls)
        assert all("authorization" not in r["headers"] for r in calls)


if __name__ == "__main__":
    unittest.main()
