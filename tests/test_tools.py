# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Tests for computer_use_tools (Open WebUI Tool).

Run: python -m pytest tests/test_tools.py -v
"""

import sys
import unittest
import asyncio
import urllib.parse
import json
import socket
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import asynccontextmanager, contextmanager

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


def _workspace_hints(events):
    return [event for event in events if event.get("type") == "ocu:workspace_changed"]


def _status_events(events):
    return [event for event in events if event.get("type") == "status"]


def _hint(chat_id):
    return {
        "type": "ocu:workspace_changed",
        "data": {"chat_id": chat_id, "reason": "tool_completed"},
    }


class _RecordingEmitter:
    def __init__(self, fail_hint=False):
        self.events = []
        self.fail_hint = fail_hint
        self.hint_attempts = 0

    async def __call__(self, event):
        if event.get("type") == "ocu:workspace_changed":
            self.hint_attempts += 1
            if self.fail_hint:
                raise RuntimeError("hint emitter failed")
        self.events.append(event)


def _stub_call_tool(monkeypatch, behavior, emitter=None):
    async def call_tool(self, *args, **kwargs):
        if emitter is not None:
            emitter.events.append({"type": "client_completed"})
        if isinstance(behavior, BaseException):
            raise behavior
        return behavior

    monkeypatch.setattr(computer_use_tools._MCPClient, "call_tool", call_tool)


def _tools_with_token(monkeypatch):
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
    tools = computer_use_tools.Tools()
    tools.valves.MCP_API_KEY = MCP_API_KEY
    return tools


def _call_with_empty_chat(tools, method, metadata, emitter):
    kwargs = {
        "__event_emitter__": emitter,
        "__metadata__": metadata,
        "__files__": [{"name": "input.txt", "path": "/unused/input.txt"}],
    }
    if method == "bash_tool":
        return tools.bash_tool("/mnt/user-data/files/input.txt", "read upload", **kwargs)
    if method == "str_replace":
        return tools.str_replace("edit", "before", "/home/assistant/a.txt", "after", **kwargs)
    if method == "create_file":
        return tools.create_file("create", "contents", "/home/assistant/a.txt", **kwargs)
    if method == "view":
        return tools.view("read", "/mnt/user-data/files/input.txt", **kwargs)
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
    monkeypatch.setattr(
        computer_use_tools.urllib.request.OpenerDirector, "open", blocked
    )
    tools = computer_use_tools.Tools()
    result = asyncio.run(
        _call_with_empty_chat(tools, method, metadata, lambda e: _collect(events, e))
    )

    assert result.startswith("[TOOL ERROR]")
    assert "chat" in result.lower()
    assert calls == []
    assert events[-1]["data"]["status"] == "error"
    assert events[-1]["data"]["done"] is True
    assert _workspace_hints(events) == []


@pytest.mark.parametrize(
    ("internal", "mcp_key", "label", "hidden"),
    (
        ("", "", "OCU_INTERNAL_TOKEN", None),
        ("invalid internal token", "", "OCU_INTERNAL_TOKEN", "invalid internal token"),
        (INTERNAL_TOKEN, "invalid\r\nmcp key", "MCP_API_KEY", "invalid\r\nmcp key"),
        (INTERNAL_TOKEN, "invalid-\u2603", "MCP_API_KEY", "invalid-\u2603"),
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
    monkeypatch.setattr(
        computer_use_tools.urllib.request.OpenerDirector, "open", blocked
    )
    tools = computer_use_tools.Tools()
    tools.valves.MCP_API_KEY = mcp_key
    result = asyncio.run(
        tools.bash_tool(
            "/mnt/user-data/files/input.txt",
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
    assert _workspace_hints(events) == []


APP_MODULES = {
    "app", "auth_guard", "ws_recheck", "mcp_tools", "docker_manager",
    "outputs_broker", "context_vars", "security", "system_prompt",
    "skill_manager", "cli_runtime", "uploads", "docs_html",
}


def _guard_environment(data):
    return {
        "OCU_WEBUI_ORIGIN": "https://webui.example",
        "SINGLE_USER_MODE": "true",
        "OCU_SANDBOX_SUBNET": "10.90.0.0/24",
        "PUBLIC_BASE_URL": "http://ocu.example",
        "BASE_DATA_DIR": str(data),
        "DOCKER_HOST": "unix:///tmp/ocu-acceptance-no-docker.sock",
        "DOCKER_SOCKET": "unix:///tmp/ocu-acceptance-no-docker.sock",
        "OCU_WEBUI_AUTH_URL": "http://127.0.0.1:9/api/v1/ocu/auth",
    }


class _GuardedOCU:
    def __init__(
        self,
        monkeypatch,
        tmp_path,
        *,
        imports_mode="live",
    ):
        import auth_guard

        self.requests = []
        self.uploads = []
        self.commands = []
        self.calls = []
        self.imports_mode = imports_mode
        self.server = None
        self.thread = None
        self.data_dir = tmp_path / "ocu-data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._snapshot = {
            name: value
            for name, value in sys.modules.items()
            if name in APP_MODULES or name.startswith("mcp_resources")
        }
        for key, value in _guard_environment(self.data_dir).items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("OCU_PUBLIC_PREFIX", raising=False)
        for name in self._snapshot:
            sys.modules.pop(name, None)
        import app as ocu_app

        self.ocu_app = ocu_app
        monkeypatch.setattr(ocu_app, "BASE_DATA_DIR", self.data_dir)
        import docker_manager
        import uploads

        monkeypatch.setattr(docker_manager, "BASE_DATA_DIR", self.data_dir)
        monkeypatch.setattr(uploads, "BASE_DATA_DIR", self.data_dir)
        self.mcp = FastMCP(
            "tool-auth-test", streamable_http_path="/", stateless_http=True
        )

        @self.mcp.tool()
        async def bash_tool(command: str, description: str) -> str:
            self.commands.append({"command": command, "uploads": list(self.uploads)})
            self.calls.append(("bash_tool", command))
            return f"ran:{command}"

        @self.mcp.tool()
        async def str_replace(
            description: str, old_str: str, path: str, new_str: str = ""
        ) -> str:
            self.calls.append(("str_replace", path))
            return f"edited:{path}"

        @self.mcp.tool()
        async def create_file(description: str, file_text: str, path: str) -> str:
            self.calls.append(("create_file", path))
            return f"created:{path}"

        @self.mcp.tool()
        async def view(
            description: str, path: str, view_range: list | None = None
        ) -> str:
            self.calls.append(("view", path))
            return f"viewed:{path}"

        @self.mcp.tool()
        async def sub_agent(
            task: str,
            description: str,
            model: str = "sonnet",
            max_turns: int = 25,
            working_directory: str = "/home/assistant",
            resume_session_id: str = "",
        ) -> str:
            self.calls.append(("sub_agent", task))
            return f"delegated:{task}"



        mcp_app = self.mcp.streamable_http_app()

        @asynccontextmanager
        async def lifespan(app):
            async with self.mcp.session_manager.run():
                yield

        async def health(request):
            return PlainTextResponse("healthy")

        async def imports_endpoint(request):
            mode = self.imports_mode
            if mode == "fail":
                return JSONResponse({"detail": "imports unavailable"}, status_code=500)
            if mode == "malformed":
                return JSONResponse({"ids": "F1"})
            if mode == "redirect":
                return JSONResponse({}, status_code=302, headers={"Location": "/elsewhere"})
            payload = await ocu_app.get_upload_imports(request.path_params["chat_id"])
            return JSONResponse(payload)

        async def upload(request):
            from fastapi import HTTPException, UploadFile
            from starlette.datastructures import UploadFile as StarletteUpload

            form = await request.form()
            uploaded = form["file"]
            if not isinstance(uploaded, (UploadFile, StarletteUpload)):
                raise RuntimeError("upload form missing file")
            filename = request.path_params["filename"]
            attachment_id = request.headers.get("x-ocu-attachment-id")
            self.uploads.append(
                {"filename": filename, "attachment_id": attachment_id}
            )
            try:
                result = await ocu_app.upload_file(
                    request.path_params["chat_id"],
                    filename,
                    uploaded,
                    attachment_id,
                )
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            return JSONResponse(result)

        app = Starlette(
            routes=[
                Route("/health", health),
                Route("/api/uploads/{chat_id}/imports", imports_endpoint),
                Route(
                    "/api/uploads/{chat_id}/{filename:path}",
                    upload,
                    methods=["POST"],
                ),
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
                        "raw_path": scope.get("raw_path"),
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
        for name in list(sys.modules):
            if name in APP_MODULES or name.startswith("mcp_resources"):
                sys.modules.pop(name, None)
        sys.modules.update(self._snapshot)

    def chat_uploads(self, chat_id):
        return self.data_dir / chat_id / "outputs"

    def stored_files(self, chat_id):
        uploads = self.chat_uploads(chat_id)
        if not uploads.is_dir():
            return {}
        return {
            path.name: path.read_bytes()
            for path in uploads.iterdir()
            if path.is_file() and not path.name.startswith(".")
        }

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"


def _with_guard(monkeypatch, tmp_path, imports_mode="live"):
    class GuardContext:
        def __enter__(self):
            self.server = _GuardedOCU(
                monkeypatch, tmp_path, imports_mode=imports_mode
            )
            self.server.start()
            return self.server

        def __exit__(self, *args):
            self.server.stop()

    return GuardContext()


def _attachment(file_id, name, source):
    return {"id": file_id, "name": name, "file": {"path": source}}


def _invoke_public_tool(tools, method, chat_id, files, command="echo ready"):
    metadata = {"chat_id": chat_id}
    if method == "bash_tool":
        return tools.bash_tool(
            command, "start work", __metadata__=metadata, __files__=files
        )
    if method == "str_replace":
        return tools.str_replace(
            "edit", "before", "/home/assistant/a.txt", "after",
            __metadata__=metadata, __files__=files,
        )
    if method == "create_file":
        return tools.create_file(
            "create", "contents", "/home/assistant/a.txt",
            __metadata__=metadata, __files__=files,
        )
    if method == "view":
        return tools.view(
            "read", "/home/assistant/notes.txt",
            __metadata__=metadata, __files__=files,
        )
    return tools.sub_agent(
        "inspect attachment", "delegate",
        __metadata__=metadata, __files__=files,
    )


def _install_storage(monkeypatch, paths):
    snapshots = {key: Path(path).read_bytes() for key, path in paths.items()}
    destinations = {key: Path(path) for key, path in paths.items()}

    def get_file(source):
        destination = destinations[source]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(snapshots[source])
        return str(destination)

    provider = types.ModuleType("open_webui.storage.provider")
    provider.Storage = type("Storage", (), {"get_file": staticmethod(get_file)})
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


class _LocalOrigin:
    """Small real HTTP origin that records requests for redirect-boundary tests."""

    def __init__(self, responder):
        self.requests = []
        self._responder = responder
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self._respond()

            def do_POST(self):
                self._respond()

            def do_DELETE(self):
                self._respond()

            def _respond(self):
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length:
                    self.rfile.read(content_length)
                request = {
                    "method": self.command,
                    "path": self.path,
                    "headers": {
                        name.lower(): value for name, value in self.headers.items()
                    },
                }
                owner.requests.append(request)
                status, headers, body = owner._responder(request)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def log_message(self, format, *args):
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )

    def start(self):
        self._thread.start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def url(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}"


@contextmanager
def _local_origin(responder):
    origin = _LocalOrigin(responder)
    origin.start()
    try:
        yield origin
    finally:
        origin.stop()


def _redirect_to(target):
    def responder(request):
        return 302, {"Location": f"{target.url}{request['path']}"}, b""

    return responder


def _service_unavailable(request):
    return 503, {}, b"redirect target"



def test_bash_imports_attachment_before_command_without_uploads_path(monkeypatch, tmp_path):
    source = tmp_path / "attachment.tmp"
    source.write_bytes(b"attachment original")
    _install_storage(monkeypatch, {"attachment-source": source})
    _disable_proxy_env(monkeypatch)
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("MCP_API_KEY", MCP_API_KEY)

    with _with_guard(monkeypatch, tmp_path) as ocu:
        tools = _tools_with_token(monkeypatch)
        tools.valves.ORCHESTRATOR_URL = ocu.url
        result = asyncio.run(
            tools.bash_tool(
                "echo ready",
                "start work",
                __metadata__={"chat_id": "chat-attachment"},
                __files__=[
                    {"id": "F1", "name": "brief.docx", "file": {"path": "attachment-source"}}
                ],
            )
        )

        assert result == "ran:echo ready"
        upload = _records(ocu, "/api/uploads/chat-attachment/brief.docx")
        assert len(upload) == 1, "attachment must be uploaded even without an uploads path"
        assert ocu.uploads == [{"filename": "brief.docx", "attachment_id": "F1"}]
        assert ocu.commands == [
            {
                "command": "echo ready",
                "uploads": [{"filename": "brief.docx", "attachment_id": "F1"}],
            }
        ]
        imports = _records(ocu, "/api/uploads/chat-attachment/imports")
        assert len(imports) == 1
        assert imports[0]["method"] == "GET"
        assert upload[0]["method"] == "POST"
        assert upload[0]["headers"].get("x-ocu-attachment-id") == "F1"
        assert all(
            record["headers"].get("authorization") == f"Bearer {INTERNAL_TOKEN}"
            for record in imports + upload + _records(ocu, "/health")
        )
        mcp = _records(ocu, "/mcp")
        assert mcp
        assert all(
            record["headers"].get("x-ocu-internal-token") == INTERNAL_TOKEN
            and record["headers"].get("authorization") == f"Bearer {MCP_API_KEY}"
            for record in mcp
        )
        calls = _records(ocu, "/mcp", **{"x-chat-id": "chat-attachment"})
        assert calls
        assert ocu.requests.index(imports[0]) < ocu.requests.index(upload[0])
        assert ocu.requests.index(upload[0]) < ocu.requests.index(calls[0])


@pytest.mark.parametrize(
    "method", ("bash_tool", "str_replace", "create_file", "view", "sub_agent")
)
def test_public_tools_sync_new_attachment_before_command(monkeypatch, tmp_path, method):
    source = tmp_path / "brief.tmp"
    source.write_bytes(b"attachment original")
    _install_storage(monkeypatch, {"attachment-source": source})
    _disable_proxy_env(monkeypatch)
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("MCP_API_KEY", MCP_API_KEY)
    chat_id = f"chat-{method.replace('_', '-')}"
    files = [_attachment("F1", "brief.docx", "attachment-source")]

    with _with_guard(monkeypatch, tmp_path) as ocu:
        tools = _tools_with_token(monkeypatch)
        tools.valves.ORCHESTRATOR_URL = ocu.url
        result = asyncio.run(_invoke_public_tool(tools, method, chat_id, files))

        encoded = urllib.parse.quote("brief.docx", safe="")
        upload = _records(ocu, f"/api/uploads/{chat_id}/{encoded}")
        imports = _records(ocu, f"/api/uploads/{chat_id}/imports")
        calls = _records(ocu, "/mcp", **{"x-chat-id": chat_id})
        assert result
        assert len(upload) == 1
        assert upload[0]["method"] == "POST"
        assert upload[0]["headers"].get("x-ocu-attachment-id") == "F1"
        assert len(imports) == 1
        assert imports[0]["method"] == "GET"
        assert ocu.uploads == [{"filename": "brief.docx", "attachment_id": "F1"}]
        assert ocu.stored_files(chat_id) == {"brief.docx": b"attachment original"}
        assert calls
        assert ocu.requests.index(imports[0]) < ocu.requests.index(upload[0])
        assert ocu.requests.index(upload[0]) < ocu.requests.index(calls[0])
        assert not _records(ocu, f"/api/uploads/{chat_id}/manifest")


def test_imported_edited_attachment_is_not_posted_again(monkeypatch, tmp_path):
    source = tmp_path / "brief.tmp"
    source.write_bytes(b"attachment original")
    _install_storage(monkeypatch, {"attachment-source": source})
    _disable_proxy_env(monkeypatch)
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("MCP_API_KEY", MCP_API_KEY)
    files = [_attachment("F1", "brief.docx", "attachment-source")]

    with _with_guard(monkeypatch, tmp_path) as ocu:
        tools = _tools_with_token(monkeypatch)
        tools.valves.ORCHESTRATOR_URL = ocu.url
        first = asyncio.run(
            tools.bash_tool(
                "echo first",
                "import",
                __metadata__={"chat_id": "chat-skip"},
                __files__=files,
            )
        )
        stored = ocu.chat_uploads("chat-skip") / "brief.docx"
        stored.write_bytes(b"user edited bytes")
        fetches = []

        def counting_get_file(key):
            fetches.append(key)
            return str(source)

        monkeypatch.setattr(
            sys.modules["open_webui.storage.provider"].Storage,
            "get_file",
            staticmethod(counting_get_file),
        )
        ocu.uploads.clear()
        second = asyncio.run(
            tools.bash_tool(
                "echo second",
                "skip imported",
                __metadata__={"chat_id": "chat-skip"},
                __files__=files,
            )
        )

        assert "ran:echo first" in first
        assert "ran:echo second" in second
        uploads = _records(ocu, "/api/uploads/chat-skip/brief.docx")
        assert len(uploads) == 1
        assert ocu.uploads == []
        assert fetches == []
        assert stored.read_bytes() == b"user edited bytes"
        assert ocu.stored_files("chat-skip") == {"brief.docx": b"user edited bytes"}


def test_two_consecutive_calls_store_one_file_for_the_same_id(monkeypatch, tmp_path):
    source = tmp_path / "brief.tmp"
    source.write_bytes(b"attachment original")
    _install_storage(monkeypatch, {"attachment-source": source})
    _disable_proxy_env(monkeypatch)
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("MCP_API_KEY", MCP_API_KEY)
    files = [_attachment("F1", "brief.docx", "attachment-source")]

    with _with_guard(monkeypatch, tmp_path) as ocu:
        tools = _tools_with_token(monkeypatch)
        tools.valves.ORCHESTRATOR_URL = ocu.url
        asyncio.run(
            tools.bash_tool(
                "echo first",
                "import",
                __metadata__={"chat_id": "chat-once"},
                __files__=files,
            )
        )
        asyncio.run(
            tools.bash_tool(
                "echo second",
                "repeat",
                __metadata__={"chat_id": "chat-once"},
                __files__=files,
            )
        )
        assert ocu.stored_files("chat-once") == {"brief.docx": b"attachment original"}
        assert len(_records(ocu, "/api/uploads/chat-once/brief.docx")) == 1


@pytest.mark.parametrize("imports_mode", ("fail", "malformed", "redirect"))
def test_failed_imports_read_posts_all_ids_and_server_writes_nothing(
    monkeypatch, tmp_path, imports_mode
):
    source = tmp_path / "brief.tmp"
    source.write_bytes(b"attachment original")
    _install_storage(monkeypatch, {"attachment-source": source})
    _disable_proxy_env(monkeypatch)
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("MCP_API_KEY", MCP_API_KEY)
    files = [_attachment("F1", "brief.docx", "attachment-source")]

    with _with_guard(monkeypatch, tmp_path) as ocu:
        tools = _tools_with_token(monkeypatch)
        tools.valves.ORCHESTRATOR_URL = ocu.url
        asyncio.run(
            tools.bash_tool(
                "echo seed",
                "import once",
                __metadata__={"chat_id": "chat-failed-read"},
                __files__=files,
            )
        )
        stored = ocu.chat_uploads("chat-failed-read") / "brief.docx"
        assert stored.read_bytes() == b"attachment original"
        stored.write_bytes(b"user edited bytes")
        ocu.imports_mode = imports_mode
        ocu.uploads.clear()
        result = asyncio.run(
            tools.bash_tool(
                "echo retry",
                "failed read",
                __metadata__={"chat_id": "chat-failed-read"},
                __files__=files,
            )
        )

        uploads_records = _records(ocu, "/api/uploads/chat-failed-read/brief.docx")
        assert "ran:echo retry" in result
        assert len(uploads_records) == 2
        assert all(
            record["headers"].get("x-ocu-attachment-id") == "F1"
            for record in uploads_records
        )
        assert ocu.stored_files("chat-failed-read") == {
            "brief.docx": b"user edited bytes"
        }
        assert not (ocu.chat_uploads("chat-failed-read") / "brief (2).docx").exists()


def test_reserved_filename_is_url_encoded_and_storage_temp_is_cleaned(
    monkeypatch, tmp_path
):
    downloaded = tmp_path / "downloaded.tmp"
    downloaded.write_bytes(b"reserved name")
    _install_storage(monkeypatch, {"attachment-source": downloaded})
    _disable_proxy_env(monkeypatch)
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("MCP_API_KEY", MCP_API_KEY)
    filename = "hash#query?percent%文件.txt"
    files = [_attachment("F-reserved", filename, "attachment-source")]

    with _with_guard(monkeypatch, tmp_path) as ocu:
        tools = _tools_with_token(monkeypatch)
        tools.valves.ORCHESTRATOR_URL = ocu.url
        result = asyncio.run(
            tools.bash_tool(
                "echo reserved",
                "encode name",
                __metadata__={"chat_id": "chat-reserved"},
                __files__=files,
            )
        )
        upload = _records(ocu, f"/api/uploads/chat-reserved/{filename}")
        encoded = urllib.parse.quote(filename, safe="")
        expected_raw = f"/api/uploads/chat-reserved/{encoded}".encode("ascii")
        assert "ran:echo reserved" in result
        assert len(upload) == 1
        assert upload[0]["path"] == f"/api/uploads/chat-reserved/{filename}"
        assert upload[0]["raw_path"] == expected_raw
        assert b"%23" in upload[0]["raw_path"]
        assert b"%3F" in upload[0]["raw_path"]
        assert b"%25" in upload[0]["raw_path"]
        assert b"%E6%96%87%E4%BB%B6" in upload[0]["raw_path"]
        assert b"#" not in upload[0]["raw_path"]
        assert b"?" not in upload[0]["raw_path"]
        assert ocu.stored_files("chat-reserved") == {filename: b"reserved name"}
        assert not downloaded.exists()


def test_missing_attachment_id_does_not_create_an_unstable_import(monkeypatch, tmp_path):
    source = tmp_path / "brief.tmp"
    source.write_bytes(b"no identity")
    _install_storage(monkeypatch, {"attachment-source": source})
    _disable_proxy_env(monkeypatch)
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("MCP_API_KEY", MCP_API_KEY)

    with _with_guard(monkeypatch, tmp_path) as ocu:
        result = computer_use_tools._sync_uploaded_files(
            ocu.url,
            "chat-missing-id",
            [{"name": "brief.docx", "file": {"path": "attachment-source"}}],
            INTERNAL_TOKEN,
        )
        assert result == {"synced": 0, "skipped": 0, "errors": 1}
        assert ocu.uploads == []
        assert ocu.stored_files("chat-missing-id") == {}
        assert not _records(ocu, "/api/uploads/chat-missing-id/brief.docx")


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
    with _with_guard(monkeypatch, tmp_path) as ocu:
        tools = computer_use_tools.Tools()
        tools.valves.ORCHESTRATOR_URL = ocu.url
        tools.valves.MCP_API_KEY = MCP_API_KEY
        first = asyncio.run(
            tools.bash_tool(
                "cat /mnt/user-data/files/uploaded.txt",
                "read upload",
                __metadata__={"chat_id": "chat-one"},
                __user__={"email": "first@example.test", "name": "First User"},
                __request__=types.SimpleNamespace(
                    headers={"X-User-Email": "forged@example.test"}
                ),
                __files__=[
                    _attachment("existing-id", "existing.txt", "existing-source"),
                    {"id": "uploaded-id", "name": "uploaded.txt", "path": "uploaded-source"},
                ],
            )
        )
        first_records = list(ocu.requests)
        assert "ran:cat /mnt/user-data/files/uploaded.txt" in first
        assert not existing.exists()
        assert not uploaded.exists()

        health = _records(ocu, "/health")
        imports = _records(ocu, "/api/uploads/chat-one/imports")
        upload = _records(ocu, "/api/uploads/chat-one/uploaded.txt")
        existing_upload = _records(ocu, "/api/uploads/chat-one/existing.txt")
        probe = _records(ocu, "/mcp", **{"x-chat-id": "preflight"})
        first_call = _records(ocu, "/mcp", **{"x-user-email": "first@example.test"})
        assert health and all(r["headers"].get("authorization") == f"Bearer {first_token}" for r in health)
        assert imports and upload
        assert all(r["headers"].get("authorization") == f"Bearer {first_token}" for r in imports + upload)
        assert probe and first_call
        assert all(r["headers"].get("x-ocu-internal-token") == first_token for r in probe + first_call)
        assert all(r["headers"].get("authorization") == f"Bearer {MCP_API_KEY}" for r in probe + first_call)
        assert all(r["headers"].get("x-user-email") != "forged@example.test" for r in first_call)
        assert all(r["headers"].get("x-chat-id") == "chat-one" for r in first_call)
        assert existing_upload
        assert upload[0]["method"] == "POST"
        assert upload[0]["headers"].get("x-ocu-attachment-id") == "uploaded-id"
        assert existing_upload[0]["method"] == "POST"
        assert existing_upload[0]["headers"].get("x-ocu-attachment-id") == "existing-id"
        assert ocu.uploads == [
            {"filename": "existing.txt", "attachment_id": "existing-id"},
            {"filename": "uploaded.txt", "attachment_id": "uploaded-id"},
        ]
        assert ocu.stored_files("chat-one") == {
            "existing.txt": b"already uploaded",
            "uploaded.txt": b"new upload",
        }
        assert not _records(ocu, "/api/uploads/chat-one/manifest")

        mcp_headers = {
            name: value
            for name, value in first_call[0]["headers"].items()
            if name != "x-ocu-internal-token"
        }
        imports_headers = {
            name: value for name, value in imports[0]["headers"].items() if name != "authorization"
        }
        assert requests.post(f"{ocu.url}/mcp", headers=mcp_headers, json={}).status_code == 401
        assert requests.get(f"{ocu.url}{imports[0]['path']}", headers=imports_headers).status_code == 401

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
        assert all(r["headers"].get("x-chat-id") == "chat-two" for r in second_call)
        assert len(ocu.requests) > len(first_records)


def test_tool_omits_optional_mcp_bearer_when_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
    monkeypatch.delenv("MCP_API_KEY", raising=False)
    _disable_proxy_env(monkeypatch)
    with _with_guard(monkeypatch, tmp_path) as ocu:
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


def test_mcp_api_key_with_spaces_reaches_the_real_guard(monkeypatch, tmp_path):
    spaced_key = "mcp key with spaces"
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("MCP_API_KEY", spaced_key)
    _disable_proxy_env(monkeypatch)
    with _with_guard(monkeypatch, tmp_path) as ocu:
        tools = computer_use_tools.Tools()
        tools.valves.ORCHESTRATOR_URL = ocu.url
        tools.valves.MCP_API_KEY = spaced_key
        result = asyncio.run(
            tools.bash_tool(
                "echo spaced key",
                "check configured key",
                __metadata__={"chat_id": "chat-spaced-key"},
                __user__={"email": "spaced-key@example.test"},
            )
        )

        calls = _records(
            ocu, "/mcp", **{"x-user-email": "spaced-key@example.test"}
        )
    assert "ran:echo spaced key" in result
    assert calls
    assert all(
        request["headers"].get("authorization") == f"Bearer {spaced_key}"
        for request in calls
    )


def test_health_redirect_does_not_leave_the_configured_origin(monkeypatch):
    _disable_proxy_env(monkeypatch)
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
    with _local_origin(_service_unavailable) as target:
        with _local_origin(_redirect_to(target)) as origin:
            tools = computer_use_tools.Tools()
            tools.valves.ORCHESTRATOR_URL = origin.url
            result = asyncio.run(
                tools.bash_tool(
                    "echo health redirect",
                    "check redirect",
                    __metadata__={"chat_id": "chat-health-redirect"},
                )
            )

    assert target.requests == []
    assert result.startswith("[CONFIG ERROR]")
    assert "redirect" in result.lower()


def test_mcp_initialize_redirect_does_not_leave_the_configured_origin(monkeypatch):
    _disable_proxy_env(monkeypatch)
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
    with _local_origin(_service_unavailable) as target:
        def origin_response(request):
            if request["path"] == "/health":
                return 200, {}, b"healthy"
            return _redirect_to(target)(request)

        with _local_origin(origin_response) as origin:
            tools = computer_use_tools.Tools()
            tools.valves.ORCHESTRATOR_URL = origin.url
            tools.valves.MCP_API_KEY = MCP_API_KEY
            result = asyncio.run(
                tools.bash_tool(
                    "echo initialize redirect",
                    "check redirect",
                    __metadata__={"chat_id": "chat-initialize-redirect"},
                )
            )

    assert target.requests == []
    assert result.startswith("[CONFIG ERROR]")
    assert "redirect" in result.lower()


def test_mcp_sdk_redirect_does_not_leave_the_configured_origin(monkeypatch, tmp_path):
    _disable_proxy_env(monkeypatch)
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
    monkeypatch.delenv("MCP_API_KEY", raising=False)
    with _with_guard(monkeypatch, tmp_path) as target:
        def origin_response(request):
            if request["path"] == "/health":
                return 200, {}, b"healthy"
            if request["headers"].get("x-chat-id") == "preflight":
                return 200, {}, b"ready"
            return 307, {"Location": f"{target.url}{request['path']}"}, b""

        with _local_origin(origin_response) as origin:
            client = computer_use_tools._MCPClient(
                origin.url, MCP_API_KEY, INTERNAL_TOKEN
            )
            result = asyncio.run(
                client.call_tool(
                    "bash_tool",
                    {"command": "echo sdk redirect", "description": "check redirect"},
                    headers=client.build_headers("chat-sdk-redirect"),
                    timeout=1,
                )
            )

    assert target.requests == []
    assert result.startswith("[CONFIG ERROR]")
    assert "redirect" in result.lower()
    assert INTERNAL_TOKEN not in result


def test_upload_redirect_does_not_leave_the_configured_origin(monkeypatch, tmp_path):
    source = tmp_path / "upload.txt"
    source.write_text("redirect upload")
    _install_storage(monkeypatch, {"upload-source": source})
    _disable_proxy_env(monkeypatch)
    with _local_origin(_service_unavailable) as target:
        with _local_origin(_redirect_to(target)) as origin:
            result = computer_use_tools._sync_uploaded_files(
                origin.url,
                "chat-upload-redirect",
                [_attachment("upload-id", "upload.txt", "upload-source")],
                INTERNAL_TOKEN,
            )

    assert target.requests == []
    assert result == {"synced": 0, "skipped": 0, "errors": 1}


@pytest.mark.parametrize("status", (401, 403))
def test_mcp_preflight_auth_rejection_is_a_configuration_error(monkeypatch, status):
    client_token = "well-formed-but-wrong-internal-token"
    client_mcp_key = "well-formed-but-wrong-mcp-key"
    events = []
    _disable_proxy_env(monkeypatch)
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", client_token)

    def origin_response(request):
        if request["path"] == "/health":
            return 200, {}, b"healthy"
        return status, {}, b"rejected"

    with _local_origin(origin_response) as origin:
        tools = computer_use_tools.Tools()
        tools.valves.ORCHESTRATOR_URL = origin.url
        tools.valves.MCP_API_KEY = client_mcp_key
        result = asyncio.run(
            tools.bash_tool(
                "echo rejected",
                "check credentials",
                __event_emitter__=lambda event: _collect(events, event),
                __metadata__={"chat_id": "chat-credential-rejection"},
                __user__={"email": "rejected@example.test"},
            )
        )

    mcp_requests = [
        request for request in origin.requests if request["path"] == "/mcp"
    ]
    assert [request["headers"].get("x-chat-id") for request in mcp_requests] == [
        "preflight"
    ]
    assert result.startswith("[CONFIG ERROR]")
    assert f"HTTP {status}" in result
    assert "traceback" not in result.lower()
    assert client_token not in result
    assert client_mcp_key not in result
    status_events = _status_events(events)
    assert status_events[-1]["data"]["status"] == "error"
    assert status_events[-1]["data"]["done"] is True
    assert _workspace_hints(events) == [_hint("chat-credential-rejection")]


@pytest.mark.parametrize("status", (401, 403))
def test_health_preflight_rejection_is_a_configuration_error_with_hint(
    monkeypatch, status
):
    client_token = "well-formed-but-wrong-internal-token"
    events = []
    _disable_proxy_env(monkeypatch)
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", client_token)

    def origin_response(request):
        if request["path"] == "/health":
            return status, {}, b"rejected"
        raise AssertionError("MCP must not run after health rejection")

    with _local_origin(origin_response) as origin:
        tools = computer_use_tools.Tools()
        tools.valves.ORCHESTRATOR_URL = origin.url
        result = asyncio.run(
            tools.bash_tool(
                "echo rejected",
                "check health",
                __event_emitter__=lambda event: _collect(events, event),
                __metadata__={"chat_id": "chat-health-rejection"},
            )
        )

    assert [request["path"] for request in origin.requests] == ["/health"]
    assert result.startswith("[CONFIG ERROR]")
    assert f"HTTP {status}" in result
    assert "traceback" not in result.lower()
    assert client_token not in result
    status_events = _status_events(events)
    assert status_events[-1]["data"]["status"] == "error"
    assert status_events[-1]["data"]["done"] is True
    assert _workspace_hints(events) == [_hint("chat-health-rejection")]


@pytest.mark.parametrize(
    ("behavior", "expected", "status"),
    (
        ("sandbox output", "sandbox output", "complete"),
        ("[CONFIG ERROR] remote failed", "[CONFIG ERROR] remote failed", "error"),
        (RuntimeError("wrapper boom"), "[Error]", "error"),
    ),
    ids=("success", "error-valued", "caught-exception"),
)
def test_public_tool_emits_one_workspace_hint_after_client_completion(
    monkeypatch, behavior, expected, status
):
    chat_id = "chat-hint"
    emitter = _RecordingEmitter()
    _stub_call_tool(monkeypatch, behavior, emitter)
    tools = _tools_with_token(monkeypatch)
    result = asyncio.run(
        tools.bash_tool(
            "echo hint",
            "emit hint",
            __event_emitter__=emitter,
            __metadata__={"chat_id": chat_id},
        )
    )

    if expected == "[Error]":
        assert result.startswith("[Error]")
        assert "RuntimeError" in result
        assert "wrapper boom" in result
    else:
        assert result == expected
    status_events = _status_events(emitter.events)
    assert status_events[-1]["data"]["status"] == status
    assert status_events[-1]["data"]["done"] is True
    hints = _workspace_hints(emitter.events)
    assert hints == [_hint(chat_id)]
    assert "revision" not in hints[0]["data"]
    client_completed = next(
        event for event in emitter.events if event.get("type") == "client_completed"
    )
    assert emitter.events.index(client_completed) < emitter.events.index(status_events[-1])
    assert emitter.events.index(status_events[-1]) < emitter.events.index(hints[0])


def test_missing_emitter_is_a_noop_after_completion(monkeypatch):
    _stub_call_tool(monkeypatch, "sandbox output")
    tools = _tools_with_token(monkeypatch)
    result = asyncio.run(
        tools.bash_tool(
            "echo hint",
            "emit hint",
            __event_emitter__=None,
            __metadata__={"chat_id": "chat-no-emitter"},
        )
    )
    assert result == "sandbox output"


@pytest.mark.parametrize(
    ("behavior", "expected", "status"),
    (
        ("sandbox output", "sandbox output", "complete"),
        ("[CONFIG ERROR] remote failed", "[CONFIG ERROR] remote failed", "error"),
        (RuntimeError("wrapper boom"), "[Error]", "error"),
    ),
    ids=("success", "error-valued", "caught-exception"),
)
def test_broken_hint_emitter_preserves_result_and_attempts_once(
    monkeypatch, behavior, expected, status
):
    emitter = _RecordingEmitter(fail_hint=True)
    _stub_call_tool(monkeypatch, behavior, emitter)
    tools = _tools_with_token(monkeypatch)
    result = asyncio.run(
        tools.bash_tool(
            "echo hint",
            "emit hint",
            __event_emitter__=emitter,
            __metadata__={"chat_id": "chat-broken-emitter"},
        )
    )
    if expected == "[Error]":
        assert result.startswith("[Error]")
        assert "RuntimeError" in result
        assert "wrapper boom" in result
    else:
        assert result == expected
    status_events = _status_events(emitter.events)
    assert status_events[-1]["data"]["status"] == status
    assert status_events[-1]["data"]["done"] is True
    assert _workspace_hints(emitter.events) == []
    assert emitter.hint_attempts == 1


def test_header_construction_failure_does_not_emit_workspace_hint(monkeypatch):
    calls = []

    async def call_tool(self, *args, **kwargs):
        calls.append((args, kwargs))
        return "should not run"

    def fail_headers(self, *args, **kwargs):
        raise ValueError("cannot encode header")

    monkeypatch.setattr(computer_use_tools._MCPClient, "call_tool", call_tool)
    monkeypatch.setattr(computer_use_tools.Tools, "_build_mcp_headers", fail_headers)
    emitter = _RecordingEmitter()
    tools = _tools_with_token(monkeypatch)
    result = asyncio.run(
        tools.bash_tool(
            "echo hint",
            "emit hint",
            __event_emitter__=emitter,
            __metadata__={"chat_id": "chat-header-failure"},
        )
    )
    assert result.startswith("[Error]")
    assert "cannot encode header" in result
    assert calls == []
    assert _workspace_hints(emitter.events) == []
    assert emitter.hint_attempts == 0


def test_cancellation_propagates_without_workspace_hint(monkeypatch):
    _stub_call_tool(monkeypatch, asyncio.CancelledError())
    emitter = _RecordingEmitter()
    tools = _tools_with_token(monkeypatch)

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await tools.bash_tool(
                "echo hint",
                "emit hint",
                __event_emitter__=emitter,
                __metadata__={"chat_id": "chat-cancelled"},
            )

    asyncio.run(run())
    assert _workspace_hints(emitter.events) == []
    assert emitter.hint_attempts == 0


if __name__ == "__main__":
    unittest.main()
