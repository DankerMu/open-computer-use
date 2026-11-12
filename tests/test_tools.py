# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Tests for computer_use_tools (Open WebUI Tool).

Run: python -m pytest tests/test_tools.py -v
"""

import sys
import unittest
import asyncio
import errno
import inspect
import os
import re
import urllib.parse
import json
import socket
import threading
import time
import types
from contextlib import asynccontextmanager, contextmanager

import pytest
import requests
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "openwebui" / "tools"))
SERVER_DIR = ROOT / "computer-use-server"
sys.path.insert(0, str(SERVER_DIR))

from tests.orchestrator._http_origin import HttpOriginLifetime, OriginRequestHandler  # noqa: E402


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
VIEW_IMAGE_B64 = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAs="


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


PUBLIC_TOOLS = ("bash_tool", "str_replace", "create_file", "view", "sub_agent", "generate_speech")


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
    if method == "generate_speech":
        return tools.generate_speech("narration", "line1.mp3", **kwargs)
    return tools.sub_agent("inspect upload", "delegate", **kwargs)


@pytest.mark.parametrize(
    "metadata",
    (None, {"chat_id": None}, {"chat_id": ""}, {"chat_id": " \t"}),
    ids=("absent", "none", "empty", "whitespace"),
)
@pytest.mark.parametrize(
    "method", PUBLIC_TOOLS
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
    "file_response",
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
        self.upload_responses = []
        self.bash_gate = None
        self.commands = []
        self.calls = []
        self.speech_calls = []
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
            if self.bash_gate is not None:
                await self.bash_gate(command)
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

        @self.mcp.tool(structured_output=False)
        async def view(
            description: str, path: str, view_range: list | None = None
        ) -> str | list:
            self.calls.append(("view", path))
            if path.endswith(".png"):
                # The shape of the real MCP view for an image (mcp_tools.view, #706).
                return [
                    f"Image: {path}",
                    ImageContent(type="image", data=VIEW_IMAGE_B64, mimeType="image/jpeg"),
                ]
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

        @self.mcp.tool()
        async def generate_speech(
            text: str, filename: str, instructions: str = "", voice: str = ""
        ) -> str:
            self.calls.append(("generate_speech", filename))
            self.speech_calls.append(
                {"text": text, "filename": filename, "instructions": instructions, "voice": voice}
            )
            return f"spoken:{filename}"



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
            reimport_missing = request.headers.get("x-ocu-reimport-missing")
            record = {"filename": filename, "attachment_id": attachment_id}
            if reimport_missing is not None:
                record["reimport_missing"] = reimport_missing
            self.uploads.append(record)
            try:
                result = await ocu_app.upload_file(
                    request.path_params["chat_id"],
                    filename,
                    uploaded,
                    attachment_id,
                    x_ocu_reimport_missing=reimport_missing,
                )
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            self.upload_responses.append(result)
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


OWNER = {"id": "user-owner", "email": "owner@example.test", "name": "Owner", "role": "user"}
OTHER = {"id": "user-other", "email": "other@example.test", "name": "Other", "role": "user"}
ADMIN = {"id": "user-admin", "email": "admin@example.test", "name": "Admin", "role": "admin"}


def _attachment(file_id, name):
    """An attachment as the request body carries it; the path names the DB row's storage key."""
    return {"id": file_id, "name": name, "file": {"path": f"webui/{file_id}"}}


def _invoke_public_tool(tools, method, chat_id, files, command="echo ready", user=OWNER):
    metadata = {"chat_id": chat_id}
    common = {"__metadata__": metadata, "__files__": files, "__user__": user}
    if method == "bash_tool":
        return tools.bash_tool(command, "start work", **common)
    if method == "str_replace":
        return tools.str_replace("edit", "before", "/home/assistant/a.txt", "after", **common)
    if method == "create_file":
        return tools.create_file("create", "contents", "/home/assistant/a.txt", **common)
    if method == "view":
        return tools.view("read", "/home/assistant/notes.txt", **common)
    if method == "generate_speech":
        return tools.generate_speech("narration", "line1.mp3", **common)
    return tools.sub_agent("inspect attachment", "delegate", **common)


_STORED = object()


def _row(filename, content, user_id=OWNER["id"], content_type="application/octet-stream",
         path=_STORED, collection_name=None):
    spec = {"filename": filename, "content": content, "user_id": user_id, "content_type": content_type}
    if path is not _STORED:
        spec["path"] = path
    if collection_name is not None:
        spec["collection_name"] = collection_name
    return spec


class _FakeWebUI:
    """The Open WebUI modules the tool imports: storage, files, users, chats, file access,
    knowledge bases, access grants and groups.

    `files` maps a file id to a _row(); each row's storage key is webui/<id>, unless the
    row sets `path`, which stores no bytes under it. `chats` maps a chat id to (owner id,
    history messages). `grants` holds (file_id, user_id) pairs that has_access_to_file
    grants for read. `knowledge` maps a knowledge-base id to (owner id, member file ids);
    `kb_grants` holds (resource type, resource id, principal type, principal id,
    permission) rows; `groups` maps a user id to group ids; `faults` maps a fake method
    name to the exception it raises.
    """

    def __init__(self, monkeypatch, tmp_path, files=None, chats=None, grants=(), chat_error=None,
                 knowledge=None, kb_grants=(), groups=None, faults=None):
        self.fetched_dir = tmp_path / "webui-fetched"
        self.rows = {}
        self.contents = {}
        for file_id, spec in (files or {}).items():
            meta = {"name": spec["filename"], "content_type": spec["content_type"]}
            if "collection_name" in spec:
                meta["collection_name"] = spec["collection_name"]
            row = types.SimpleNamespace(
                id=file_id, user_id=spec["user_id"], filename=spec["filename"],
                path=spec.get("path", f"webui/{file_id}"), meta=meta,
            )
            self.rows[file_id] = row
            if "path" not in spec:
                self.contents[row.path] = spec["content"]
        self.chats = chats or {}
        self.grants = set(grants)
        self.chat_error = chat_error
        self.knowledge = knowledge or {}
        self.kb_grants = set(kb_grants)
        self.groups = groups or {}
        self.faults = faults or {}
        self.get_file_calls, self.fetched, self.lookups, self.access_checks = [], [], [], []
        self.chat_lookups, self.knowledge_lookups, self.group_lookups, self.grant_checks = [], [], [], []
        self._install(monkeypatch)

    def _fault(self, name):
        if name in self.faults:
            raise self.faults[name]

    def get_file(self, key):
        self.get_file_calls.append(key)
        if key not in self.contents:
            raise FileNotFoundError(key)
        self.fetched_dir.mkdir(parents=True, exist_ok=True)
        fetched = self.fetched_dir / f"fetch-{len(self.fetched)}"
        fetched.write_bytes(self.contents[key])
        self.fetched.append(fetched)
        return str(fetched)

    def _install(self, monkeypatch):
        webui = self

        class Storage:
            get_file = staticmethod(webui.get_file)

        class Files:
            @staticmethod
            async def get_file_by_id(file_id, db=None):
                webui.lookups.append(file_id)
                webui._fault("get_file_by_id")
                return webui.rows.get(file_id)

        class Users:
            @staticmethod
            async def get_user_by_id(user_id, db=None):
                for user in (OWNER, OTHER, ADMIN):
                    if user["id"] == user_id:
                        return types.SimpleNamespace(id=user["id"], role=user["role"])
                return None

        class Chats:
            @staticmethod
            async def get_chat_by_id(id, db=None):
                webui.chat_lookups.append(id)
                if webui.chat_error is not None:
                    raise webui.chat_error
                if id not in webui.chats:
                    return None
                owner_id, messages = webui.chats[id]
                return types.SimpleNamespace(
                    id=id, user_id=owner_id, chat={"history": {"messages": messages}}
                )

        async def has_access_to_file(file_id, access_type, user, db=None, user_group_ids=None):
            webui.access_checks.append((file_id, access_type, user.id))
            return access_type == "read" and (file_id, user.id) in webui.grants

        class Knowledges:
            @staticmethod
            async def get_knowledges_by_file_id(file_id, db=None):
                webui.knowledge_lookups.append(file_id)
                webui._fault("get_knowledges_by_file_id")
                return [
                    types.SimpleNamespace(id=kb_id, user_id=owner_id)
                    for kb_id, (owner_id, members) in webui.knowledge.items()
                    if file_id in members
                ]

        class Groups:
            @staticmethod
            async def get_groups_by_member_id(user_id, db=None):
                webui.group_lookups.append(user_id)
                webui._fault("get_groups_by_member_id")
                return [types.SimpleNamespace(id=group_id) for group_id in webui.groups.get(user_id, ())]

        class AccessGrants:
            @staticmethod
            async def has_access(user_id, resource_type, resource_id, permission="read",
                                 user_group_ids=None, db=None):
                webui.grant_checks.append((user_id, resource_type, resource_id, permission, user_group_ids))
                assert (resource_type, permission) == ("knowledge", "read")
                webui._fault("has_access")
                if user_group_ids is None:
                    user_group_ids = {g.id for g in await Groups.get_groups_by_member_id(user_id)}
                principals = {("user", "*"), ("user", user_id)} | {("group", g) for g in user_group_ids}
                return any(
                    (resource_type, resource_id, kind, principal, permission) in webui.kb_grants
                    for kind, principal in principals
                )

        modules = {}
        for name in (
            "open_webui", "open_webui.storage", "open_webui.models", "open_webui.utils",
            "open_webui.utils.access_control",
        ):
            modules[name] = types.ModuleType(name)
            modules[name].__path__ = []
        for name, attribute, value in (
            ("open_webui.storage.provider", "Storage", Storage),
            ("open_webui.models.files", "Files", Files),
            ("open_webui.models.users", "Users", Users),
            ("open_webui.models.chats", "Chats", Chats),
            ("open_webui.utils.access_control.files", "has_access_to_file", has_access_to_file),
            ("open_webui.models.knowledge", "Knowledges", Knowledges),
            ("open_webui.models.groups", "Groups", Groups),
            ("open_webui.models.access_grants", "AccessGrants", AccessGrants),
        ):
            modules[name] = types.ModuleType(name)
            setattr(modules[name], attribute, value)
        for name, module in modules.items():
            monkeypatch.setitem(sys.modules, name, module)


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

        class Handler(OriginRequestHandler):
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

        self._lifetime = HttpOriginLifetime(Handler)

    @property
    def url(self):
        return self._lifetime.url


@contextmanager
def _local_origin(responder):
    origin = _LocalOrigin(responder)
    with origin._lifetime:
        yield origin


def _redirect_to(target):
    def responder(request):
        return 302, {"Location": f"{target.url}{request['path']}"}, b""

    return responder


def _service_unavailable(request):
    return 503, {}, b"redirect target"



def test_bash_imports_attachment_before_command_without_uploads_path(monkeypatch, tmp_path):
    _FakeWebUI(monkeypatch, tmp_path, files={"F1": _row("brief.docx", b"attachment original")})
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
                __user__=OWNER,
                __files__=[_attachment("F1", "brief.docx")],
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
    "method", PUBLIC_TOOLS
)
def test_public_tools_sync_new_attachment_before_command(monkeypatch, tmp_path, method):
    webui = _FakeWebUI(monkeypatch, tmp_path, files={"F1": _row("brief.docx", b"attachment original")})
    _disable_proxy_env(monkeypatch)
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("MCP_API_KEY", MCP_API_KEY)
    chat_id = f"chat-{method.replace('_', '-')}"
    files = [_attachment("F1", "brief.docx")]

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
        assert "x-ocu-reimport-missing" not in upload[0]["headers"]
        assert len(imports) == 1
        assert imports[0]["method"] == "GET"
        assert ocu.uploads == [{"filename": "brief.docx", "attachment_id": "F1"}]
        assert ocu.stored_files(chat_id) == {"brief.docx": b"attachment original"}
        assert calls
        assert ocu.requests.index(imports[0]) < ocu.requests.index(upload[0])
        assert ocu.requests.index(upload[0]) < ocu.requests.index(calls[0])
        assert not _records(ocu, f"/api/uploads/{chat_id}/manifest")


def _fail_directory_enumeration(monkeypatch, root):
    identity = root.stat()
    scandir = os.scandir

    def failed_scan(path):
        info = os.fstat(path) if isinstance(path, int) else os.stat(path)
        if (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
            raise OSError(errno.EIO, "private-host-path-canary")
        return scandir(path)

    monkeypatch.setattr(os, "scandir", failed_scan)


@pytest.mark.parametrize(
    "method", PUBLIC_TOOLS
)
def test_public_tools_report_committed_upload_discovery_warning_without_retry(
    monkeypatch, tmp_path, method
):
    webui = _FakeWebUI(monkeypatch, tmp_path, files={"F1": _row("brief.docx", b"attachment original")})
    _disable_proxy_env(monkeypatch)
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("MCP_API_KEY", MCP_API_KEY)
    chat_id = f"discovery-{method.replace('_', '-')}"
    files = [_attachment("F1", "brief.docx")]

    with _with_guard(monkeypatch, tmp_path) as ocu:
        root = ocu.chat_uploads(chat_id)
        root.mkdir(parents=True)
        tools = _tools_with_token(monkeypatch)
        tools.valves.ORCHESTRATOR_URL = ocu.url
        with monkeypatch.context() as faults:
            _fail_directory_enumeration(faults, root)
            result = asyncio.run(_invoke_public_tool(tools, method, chat_id, files))
            repeated = asyncio.run(_invoke_public_tool(tools, method, chat_id, files))

        assert ocu.upload_responses[0]["discovery"] == {
            "status": "failed", "reason": "io_error"
        }
        assert ocu.stored_files(chat_id) == {"brief.docx": b"attachment original"}
        assert ocu.uploads == [{"filename": "brief.docx", "attachment_id": "F1"}]
        assert len(_records(ocu, f"/api/uploads/{chat_id}/brief.docx")) == 1
        expected_calls = {
            "bash_tool": ("echo ready", "ran:echo ready"),
            "str_replace": ("/home/assistant/a.txt", "edited:/home/assistant/a.txt"),
            "create_file": ("/home/assistant/a.txt", "created:/home/assistant/a.txt"),
            "view": ("/home/assistant/notes.txt", "viewed:/home/assistant/notes.txt"),
            "sub_agent": ("inspect attachment", "delegated:inspect attachment"),
            "generate_speech": ("line1.mp3", "spoken:line1.mp3"),
        }
        argument, output = expected_calls[method]
        assert ocu.calls == [(method, argument)] * 2
        assert output in result
        assert repeated == output
        assert "discovery" in result.lower()
        assert "io_error" in result
        assert "stored" in result.lower()
        assert "warning" in result.lower()
        assert "private-host-path-canary" not in result
        assert str(tmp_path) not in result
        assert "discovery" not in repeated.lower(), "warning must not leak into a receipted call"


def test_tool_discovery_warning_identifies_collision_stored_filename(
    monkeypatch, tmp_path
):
    webui = _FakeWebUI(monkeypatch, tmp_path, files={"F1": _row("brief.docx", b"attachment original")})
    _disable_proxy_env(monkeypatch)
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("MCP_API_KEY", MCP_API_KEY)
    files = [_attachment("F1", "brief.docx")]

    with _with_guard(monkeypatch, tmp_path) as ocu:
        root = ocu.chat_uploads("collision-warning")
        root.mkdir(parents=True)
        (root / "brief.docx").write_bytes(b"existing edited file")
        tools = _tools_with_token(monkeypatch)
        tools.valves.ORCHESTRATOR_URL = ocu.url
        with monkeypatch.context() as faults:
            _fail_directory_enumeration(faults, root)
            result = asyncio.run(_invoke_public_tool(
                tools, "bash_tool", "collision-warning", files
            ))
            repeated = asyncio.run(_invoke_public_tool(
                tools, "bash_tool", "collision-warning", files
            ))

        assert ocu.upload_responses[0]["filename"] == "brief (2).docx"
        assert ocu.upload_responses[0]["discovery"] == {
            "status": "failed", "reason": "io_error"
        }
        assert ocu.stored_files("collision-warning") == {
            "brief.docx": b"existing edited file",
            "brief (2).docx": b"attachment original",
        }
        assert len(_records(ocu, "/api/uploads/collision-warning/brief.docx")) == 1
        assert ocu.uploads == [{"filename": "brief.docx", "attachment_id": "F1"}]
        assert repeated == "ran:echo ready"
        assert "ran:echo ready" in result
        assert "io_error" in result
        assert "brief (2).docx" in result, "warning must identify the actual stored file"


def test_overlapping_tool_calls_keep_discovery_warning_on_its_own_result(
    monkeypatch, tmp_path
):
    webui = _FakeWebUI(monkeypatch, tmp_path, files={"F1": _row("brief.docx", b"attachment original")})
    _disable_proxy_env(monkeypatch)
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("MCP_API_KEY", MCP_API_KEY)

    with _with_guard(monkeypatch, tmp_path) as ocu:
        root = ocu.chat_uploads("warning-chat")
        root.mkdir(parents=True)
        reached = threading.Event()
        release = threading.Event()

        async def gate(command):
            if command == "echo held":
                reached.set()
                while not release.is_set():
                    await asyncio.sleep(0.01)

        ocu.bash_gate = gate
        tools = _tools_with_token(monkeypatch)
        tools.valves.ORCHESTRATOR_URL = ocu.url

        async def overlap():
            warning_call = asyncio.create_task(_invoke_public_tool(
                tools, "bash_tool", "warning-chat",
                [_attachment("F1", "brief.docx")], "echo held"
            ))
            try:
                assert await asyncio.to_thread(reached.wait, 10), "warning call never reached MCP"
                clean_result = await asyncio.wait_for(_invoke_public_tool(
                    tools, "bash_tool", "clean-chat", [], "echo clean"
                ), 10)
            finally:
                release.set()
            return await asyncio.wait_for(warning_call, 10), clean_result

        with monkeypatch.context() as faults:
            _fail_directory_enumeration(faults, root)
            warning_result, clean_result = asyncio.run(overlap())
        assert clean_result == "ran:echo clean"
        assert "ran:echo held" in warning_result
        assert "discovery" in warning_result.lower()
        assert "io_error" in warning_result
        assert "stored" in warning_result.lower()
        assert len(_records(ocu, "/api/uploads/warning-chat/brief.docx")) == 1
        assert ocu.stored_files("warning-chat") == {"brief.docx": b"attachment original"}


def test_imported_edited_attachment_is_not_posted_again(monkeypatch, tmp_path):
    webui = _FakeWebUI(monkeypatch, tmp_path, files={"F1": _row("brief.docx", b"attachment original")})
    _disable_proxy_env(monkeypatch)
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("MCP_API_KEY", MCP_API_KEY)
    files = [_attachment("F1", "brief.docx")]

    with _with_guard(monkeypatch, tmp_path) as ocu:
        tools = _tools_with_token(monkeypatch)
        tools.valves.ORCHESTRATOR_URL = ocu.url
        first = asyncio.run(
            tools.bash_tool(
                "echo first",
                "import",
                __metadata__={"chat_id": "chat-skip"},
                __files__=files,
                __user__=OWNER,
            )
        )
        stored = ocu.chat_uploads("chat-skip") / "brief.docx"
        stored.write_bytes(b"user edited bytes")
        webui.get_file_calls.clear()
        webui.lookups.clear()
        ocu.uploads.clear()
        second = asyncio.run(
            tools.bash_tool(
                "echo second",
                "skip imported",
                __metadata__={"chat_id": "chat-skip"},
                __files__=files,
                __user__=OWNER,
            )
        )

        assert "ran:echo first" in first
        assert "ran:echo second" in second
        uploads = _records(ocu, "/api/uploads/chat-skip/brief.docx")
        assert len(uploads) == 1
        assert ocu.uploads == []
        assert webui.get_file_calls == []
        assert webui.lookups == [], "a receipted id is skipped before any DB lookup"
        assert stored.read_bytes() == b"user edited bytes"
        assert ocu.stored_files("chat-skip") == {"brief.docx": b"user edited bytes"}


def test_two_consecutive_calls_store_one_file_for_the_same_id(monkeypatch, tmp_path):
    webui = _FakeWebUI(monkeypatch, tmp_path, files={"F1": _row("brief.docx", b"attachment original")})
    _disable_proxy_env(monkeypatch)
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("MCP_API_KEY", MCP_API_KEY)
    files = [_attachment("F1", "brief.docx")]

    with _with_guard(monkeypatch, tmp_path) as ocu:
        tools = _tools_with_token(monkeypatch)
        tools.valves.ORCHESTRATOR_URL = ocu.url
        asyncio.run(
            tools.bash_tool(
                "echo first",
                "import",
                __metadata__={"chat_id": "chat-once"},
                __files__=files,
                __user__=OWNER,
            )
        )
        asyncio.run(
            tools.bash_tool(
                "echo second",
                "repeat",
                __metadata__={"chat_id": "chat-once"},
                __files__=files,
                __user__=OWNER,
            )
        )
        assert ocu.stored_files("chat-once") == {"brief.docx": b"attachment original"}
        assert len(_records(ocu, "/api/uploads/chat-once/brief.docx")) == 1


@pytest.mark.parametrize("imports_mode", ("fail", "malformed", "redirect"))
def test_failed_imports_read_posts_all_ids_and_server_writes_nothing(
    monkeypatch, tmp_path, imports_mode
):
    webui = _FakeWebUI(monkeypatch, tmp_path, files={"F1": _row("brief.docx", b"attachment original")})
    _disable_proxy_env(monkeypatch)
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("MCP_API_KEY", MCP_API_KEY)
    files = [_attachment("F1", "brief.docx")]

    with _with_guard(monkeypatch, tmp_path) as ocu:
        tools = _tools_with_token(monkeypatch)
        tools.valves.ORCHESTRATOR_URL = ocu.url
        asyncio.run(
            tools.bash_tool(
                "echo seed",
                "import once",
                __metadata__={"chat_id": "chat-failed-read"},
                __files__=files,
                __user__=OWNER,
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
                __user__=OWNER,
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
    filename = "hash#query?percent%文件.txt"
    webui = _FakeWebUI(monkeypatch, tmp_path, files={"F-reserved": _row(filename, b"reserved name")})
    _disable_proxy_env(monkeypatch)
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("MCP_API_KEY", MCP_API_KEY)
    files = [_attachment("F-reserved", filename)]

    with _with_guard(monkeypatch, tmp_path) as ocu:
        tools = _tools_with_token(monkeypatch)
        tools.valves.ORCHESTRATOR_URL = ocu.url
        result = asyncio.run(
            tools.bash_tool(
                "echo reserved",
                "encode name",
                __metadata__={"chat_id": "chat-reserved"},
                __files__=files,
                __user__=OWNER,
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
        assert len(webui.fetched) == 1
        assert not webui.fetched[0].exists(), "the Storage temp copy is removed"


def test_missing_attachment_id_does_not_create_an_unstable_import(monkeypatch, tmp_path):
    webui = _FakeWebUI(monkeypatch, tmp_path, files={"F1": _row("brief.docx", b"no identity")})
    _disable_proxy_env(monkeypatch)
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("MCP_API_KEY", MCP_API_KEY)

    with _with_guard(monkeypatch, tmp_path) as ocu:
        result = asyncio.run(_invoke_public_tool(
            _guarded_tools(monkeypatch, ocu),
            "bash_tool", "chat-missing-id",
            [{"name": "brief.docx", "file": {"path": "webui/F1"}}, {"id": "", "name": "x"}],
        ))
        assert result == "ran:echo ready"
        assert webui.lookups == []
        assert webui.get_file_calls == []
        assert not _records(ocu, "/api/uploads/chat-missing-id/imports")
        assert ocu.uploads == []
        assert ocu.stored_files("chat-missing-id") == {}
        assert not _records(ocu, "/api/uploads/chat-missing-id/brief.docx")


def test_tool_authenticates_real_transports_and_rotates_identity(monkeypatch, tmp_path):
    first_token, second_token = "test-internal-one", "test-internal-two"
    webui = _FakeWebUI(monkeypatch, tmp_path, files={
        "existing-id": _row("existing.txt", b"already uploaded"),
        "uploaded-id": _row("uploaded.txt", b"new upload"),
    })
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
                __user__={"id": OWNER["id"], "email": "first@example.test", "name": "First User"},
                __request__=types.SimpleNamespace(
                    headers={"X-User-Email": "forged@example.test"}
                ),
                __files__=[
                    _attachment("existing-id", "existing.txt"),
                    {"id": "uploaded-id", "name": "uploaded.txt", "path": "webui/uploaded-id"},
                ],
            )
        )
        first_records = list(ocu.requests)
        assert "ran:cat /mnt/user-data/files/uploaded.txt" in first
        assert webui.fetched and not any(path.exists() for path in webui.fetched)

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
    _FakeWebUI(monkeypatch, tmp_path, files={"upload-id": _row("upload.txt", b"redirect upload")})
    _disable_proxy_env(monkeypatch)
    with _local_origin(_service_unavailable) as target:
        with _local_origin(_redirect_to(target)) as origin:
            result = computer_use_tools._sync_uploaded_files(
                origin.url,
                "chat-upload-redirect",
                [{"id": "upload-id", "name": "upload.txt", "path": "webui/upload-id"}],
                INTERNAL_TOKEN,
            )

    assert target.requests == []
    assert result == {"synced": 0, "errors": 1}


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


GATEWAY_HEADERS = ("X-OCU-Gateway-Token", "X-Chat-Model")


def _capture_call_headers(monkeypatch):
    sent = []

    async def call_tool(self, tool_name, args, headers=None, **kwargs):
        sent.append({"tool": tool_name, "args": args, "headers": dict(headers)})
        return "sandbox output"

    monkeypatch.setattr(computer_use_tools._MCPClient, "call_tool", call_tool)
    return sent


def _chat_request(token):
    state = types.SimpleNamespace()
    if token is not None:
        state.token = types.SimpleNamespace(scheme="Bearer", credentials=token)
    return types.SimpleNamespace(state=state)


GATEWAY_USER = {"id": "u1", "email": "u@example.test", "name": "U"}


def _chat_ownership(monkeypatch, outcome):
    """Install a fake open_webui.models.chats whose is_chat_owner returns or raises outcome."""
    calls = []

    class Chats:
        @staticmethod
        async def is_chat_owner(chat_id, user_id):
            calls.append((chat_id, user_id))
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    package = types.ModuleType("open_webui")
    models = types.ModuleType("open_webui.models")
    chats = types.ModuleType("open_webui.models.chats")
    chats.Chats = Chats
    package.models = models
    models.chats = chats
    monkeypatch.setitem(sys.modules, "open_webui", package)
    monkeypatch.setitem(sys.modules, "open_webui.models", models)
    monkeypatch.setitem(sys.modules, "open_webui.models.chats", chats)
    return calls


def _gateway_call(monkeypatch, request_token="tok-1", metadata_extra=None, user=GATEWAY_USER, task_model=None):
    """Call sub_agent the way Open WebUI injects reserved arguments: it offers every extra
    parameter, including the task model as __model__ (middleware.py:2916-2925), and passes
    only those the signature names (utils/tools.py:220)."""
    sent = _capture_call_headers(monkeypatch)
    tools = _tools_with_token(monkeypatch)
    offered = {
        "__metadata__": {"chat_id": "chat-gateway", **(metadata_extra or {})},
        "__user__": user,
        "__request__": _chat_request(request_token),
        "__model__": task_model,
    }
    accepted = inspect.signature(tools.sub_agent).parameters
    injected = {name: value for name, value in offered.items() if name in accepted}
    result = asyncio.run(tools.sub_agent("write and run hello.py", "delegate", **injected))
    assert result == "sandbox output"
    [call] = sent
    assert call["tool"] == "sub_agent"
    assert call["headers"]["X-Chat-Id"] == "chat-gateway"
    assert call["headers"]["X-OCU-Internal-Token"] == INTERNAL_TOKEN
    assert "tok-1" not in json.dumps(call["args"])
    return {name: call["headers"][name] for name in GATEWAY_HEADERS if name in call["headers"]}


@pytest.mark.parametrize(
    ("request_token", "metadata_extra", "expected"),
    (
        ("tok-1", {"model": {"id": "m-1"}}, {"X-OCU-Gateway-Token": "tok-1", "X-Chat-Model": "m-1"}),
        (None, {"model": {"id": "m-1"}}, {"X-Chat-Model": "m-1"}),
        ("", {"model": {"id": "m-1"}}, {"X-Chat-Model": "m-1"}),
        ("tok-1", {}, {"X-OCU-Gateway-Token": "tok-1"}),
        ("tok-1", {"model": None}, {"X-OCU-Gateway-Token": "tok-1"}),
        ("tok-1", {"model": {"id": ""}}, {"X-OCU-Gateway-Token": "tok-1"}),
        ("tok-1", {"model": {"name": "no id"}}, {"X-OCU-Gateway-Token": "tok-1"}),
        ("tok-1", {"model": "m-1"}, {"X-OCU-Gateway-Token": "tok-1", "X-Chat-Model": "m-1"}),
    ),
    ids=("both", "no-token", "empty-token", "no-model", "null-model", "empty-model-id",
         "model-without-id", "string-model-id"),
)
def test_sub_agent_forwards_the_owners_token_and_chat_model(monkeypatch, request_token, metadata_extra, expected):
    calls = _chat_ownership(monkeypatch, True)
    assert _gateway_call(monkeypatch, request_token, metadata_extra) == expected
    assert calls == ([("chat-gateway", "u1")] if request_token else [])


def test_sub_agent_takes_the_chat_model_from_metadata_not_the_task_model(monkeypatch):
    _chat_ownership(monkeypatch, True)
    headers = _gateway_call(
        monkeypatch, metadata_extra={"model": {"id": "m-1"}}, task_model={"id": "cheap-m"},
    )
    assert headers == {"X-OCU-Gateway-Token": "tok-1", "X-Chat-Model": "m-1"}


def test_sub_agent_without_chat_model_in_metadata_sends_no_model_even_with_a_task_model(monkeypatch):
    _chat_ownership(monkeypatch, True)
    headers = _gateway_call(monkeypatch, task_model={"id": "cheap-m"})
    assert headers == {"X-OCU-Gateway-Token": "tok-1"}


@pytest.mark.parametrize(
    "outcome",
    (False, RuntimeError("database unavailable")),
    ids=("caller-is-not-owner", "ownership-lookup-raises"),
)
def test_sub_agent_sends_no_token_unless_the_caller_owns_the_chat(monkeypatch, outcome):
    calls = _chat_ownership(monkeypatch, outcome)
    admin = {"id": "admin-1", "email": "admin@example.test", "name": "Admin", "role": "admin"}
    headers = _gateway_call(monkeypatch, metadata_extra={"model": {"id": "m-1"}}, user=admin)
    assert headers == {"X-Chat-Model": "m-1"}
    assert calls == [("chat-gateway", "admin-1")]


def test_sub_agent_sends_no_token_when_open_webui_is_not_importable(monkeypatch):
    monkeypatch.setitem(sys.modules, "open_webui.models.chats", None)
    headers = _gateway_call(monkeypatch, metadata_extra={"model": {"id": "m-1"}})
    assert headers == {"X-Chat-Model": "m-1"}


def test_sub_agent_without_user_sends_no_token(monkeypatch):
    calls = _chat_ownership(monkeypatch, True)
    headers = _gateway_call(monkeypatch, metadata_extra={"model": {"id": "m-1"}}, user=None)
    assert headers == {"X-Chat-Model": "m-1"}
    assert calls == []


def test_sub_agent_without_request_sends_no_gateway_token(monkeypatch):
    calls = _chat_ownership(monkeypatch, True)
    sent = _capture_call_headers(monkeypatch)
    tools = _tools_with_token(monkeypatch)
    asyncio.run(tools.sub_agent(
        "t", "d", __metadata__={"chat_id": "chat-gateway", "model": {"id": "m-1"}}, __user__=GATEWAY_USER,
    ))
    assert "X-OCU-Gateway-Token" not in sent[0]["headers"]
    assert sent[0]["headers"]["X-Chat-Model"] == "m-1"
    assert calls == []


@pytest.mark.parametrize("method", ("bash_tool", "str_replace", "create_file", "view", "generate_speech"))
def test_other_tools_never_send_gateway_headers(monkeypatch, method):
    sent = _capture_call_headers(monkeypatch)
    tools = _tools_with_token(monkeypatch)
    common = {"__metadata__": {"chat_id": "chat-gateway"}, "__request__": _chat_request("tok-1")}
    calls = {
        "bash_tool": lambda: tools.bash_tool("echo hi", "run", **common),
        "str_replace": lambda: tools.str_replace("edit", "a", "/home/assistant/a.txt", "b", **common),
        "create_file": lambda: tools.create_file("create", "x", "/home/assistant/a.txt", **common),
        "view": lambda: tools.view("read", "/home/assistant/a.txt", **common),
        "generate_speech": lambda: tools.generate_speech("hi", "a.mp3", **common),
    }
    asyncio.run(calls[method]())
    [call] = sent
    assert set(call["headers"]) == {"X-Chat-Id", "X-OCU-Internal-Token", "Authorization"}


@pytest.mark.parametrize(
    ("arguments", "forwarded"),
    (
        (("第一句", "line1.mp3"), {"text": "第一句", "filename": "line1.mp3", "instructions": "", "voice": ""}),
        (("hi", "intro.wav", "calm narrator", "v-2"),
         {"text": "hi", "filename": "intro.wav", "instructions": "calm narrator", "voice": "v-2"}),
    ),
    ids=("defaults", "all-arguments"),
)
def test_generate_speech_forwards_to_the_mcp_tool(monkeypatch, arguments, forwarded):
    sent = _capture_call_headers(monkeypatch)
    tools = _tools_with_token(monkeypatch)
    events = []
    result = asyncio.run(tools.generate_speech(
        *arguments, __metadata__={"chat_id": "chat-speech"},
        __event_emitter__=lambda e: _collect(events, e),
    ))
    assert result == "sandbox output"
    [call] = sent
    assert call["tool"] == "generate_speech"
    assert call["args"] == forwarded
    assert call["headers"]["X-Chat-Id"] == "chat-speech"
    statuses = [e["data"]["status"] for e in events if e.get("type") == "status"]
    assert statuses == ["in_progress", "complete"]


def test_generate_speech_error_result_marks_the_status_as_error(monkeypatch):
    _stub_call_tool(monkeypatch, "Error: speech synthesis is not configured")
    tools = _tools_with_token(monkeypatch)
    events = []
    result = asyncio.run(tools.generate_speech(
        "hi", "a.mp3", __metadata__={"chat_id": "chat-speech"},
        __event_emitter__=lambda e: _collect(events, e),
    ))
    assert result == "Error: speech synthesis is not configured"
    statuses = [e["data"]["status"] for e in events if e.get("type") == "status"]
    assert statuses[-1] == "error"


def test_sub_agent_docstring_says_the_chat_model_is_used_in_gateway_deployments():
    doc = computer_use_tools.Tools.sub_agent.__doc__
    assert "gateway" in doc
    assert "chat's model" in doc
    assert '"sonnet"' in doc and '"opus"' in doc


# ============================================================================
# Workspace images: view returns images, chat-generated images sync (#706, #707)
# ============================================================================

GEN_SUNSET = "0a1b2c3d-0000-4000-8000-000000000001"
GEN_LOGO = "0a1b2c3d-0000-4000-8000-000000000002"
GEN_MISSING = "0a1b2c3d-0000-4000-8000-0000000000ff"
DOC_BRIEF = "0a1b2c3d-0000-4000-8000-0000000000d0"
VISION_OFF = {"id": "text-only", "info": {"meta": {"capabilities": {"vision": False}}}}
VISION_ON = {"id": "seeing", "info": {"meta": {"capabilities": {"vision": True}}}}


def _image_ref(file_id):
    """A message file entry as upstream generate_image records it (tools/builtin.py)."""
    return {"type": "image", "url": f"/api/v1/files/{file_id}/content"}


def _message(timestamp, *files):
    return {"role": "assistant", "content": "", "timestamp": timestamp, "files": list(files)}


def _image_row(filename, content, user_id=OWNER["id"]):
    return _row(filename, content, user_id, "image/png")


def _chat_webui(monkeypatch, tmp_path, chat_id, messages, files, chat_owner=OWNER["id"], **kwargs):
    return _FakeWebUI(
        monkeypatch, tmp_path, files=files, chats={chat_id: (chat_owner, messages)}, **kwargs
    )


def _guarded_tools(monkeypatch, ocu):
    tools = _tools_with_token(monkeypatch)
    tools.valves.ORCHESTRATOR_URL = ocu.url
    return tools


def _image_env(monkeypatch):
    _disable_proxy_env(monkeypatch)
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("MCP_API_KEY", MCP_API_KEY)


@pytest.mark.parametrize(
    "metadata_model",
    (None, {"id": "base-model"}, {"id": "m", "info": {"meta": {}}}, VISION_ON),
    ids=("no-model", "no-info", "no-capabilities", "vision-true"),
)
def test_view_of_an_image_returns_a_data_uri_for_the_model(monkeypatch, tmp_path, metadata_model):
    _image_env(monkeypatch)
    metadata = {"chat_id": "chat-view-image"}
    if metadata_model is not None:
        metadata["model"] = metadata_model
    with _with_guard(monkeypatch, tmp_path) as ocu:
        events = []
        result = asyncio.run(_guarded_tools(monkeypatch, ocu).view(
            "check frame", "/home/assistant/videos/intro/out/frames/element-045.png",
            __metadata__=metadata, __event_emitter__=lambda e: _collect(events, e),
        ))

    # Upstream routes a result starting with data:image/ to the model as input_image
    # only when the whole string is the data URI (middleware.py 1124-1129).
    assert result == f"data:image/jpeg;base64,{VIEW_IMAGE_B64}"
    assert ocu.calls == [("view", "/home/assistant/videos/intro/out/frames/element-045.png")]
    assert _status_events(events)[-1]["data"]["status"] == "complete"


@pytest.mark.parametrize(
    "path",
    ("/a/f.png", "/a/f.jpg", "/a/f.jpeg", "/a/f.gif", "/a/f.webp", "/a/F.PNG"),
)
def test_view_of_an_image_by_a_non_vision_model_errors_before_any_call(monkeypatch, path):
    calls, events = [], []

    def blocked(*args, **kwargs):
        calls.append((args, kwargs))
        raise OSError("network must not run")

    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setattr(requests, "get", blocked)
    monkeypatch.setattr(requests, "post", blocked)
    monkeypatch.setattr(computer_use_tools.urllib.request.OpenerDirector, "open", blocked)
    monkeypatch.setattr(computer_use_tools._MCPClient, "call_tool", blocked)
    result = asyncio.run(computer_use_tools.Tools().view(
        "look", path,
        __metadata__={"chat_id": "chat-text-only", "model": VISION_OFF},
        __files__=[_attachment("F1", "brief.docx")],
        __event_emitter__=lambda e: _collect(events, e),
    ))

    assert result.startswith("Error: this model cannot view images")
    assert "bash_tool" in result
    assert calls == []
    assert _status_events(events)[-1]["data"]["status"] == "error"
    assert _workspace_hints(events) == []


def test_vision_guard_reads_the_chat_model_not_the_injected_task_model(monkeypatch):
    """Open WebUI offers the task model as __model__ (middleware.py:2916-2925); the image
    goes to the chat model in __metadata__["model"] (main.py:1261)."""
    sent = _capture_call_headers(monkeypatch)
    tools = _tools_with_token(monkeypatch)

    def view_with(chat_model, task_model):
        offered = {
            "__metadata__": {"chat_id": "chat-models", "model": chat_model},
            "__model__": task_model,
        }
        accepted = inspect.signature(tools.view).parameters
        injected = {name: value for name, value in offered.items() if name in accepted}
        return asyncio.run(tools.view("look", "/a/frame.png", **injected))

    refused = view_with(VISION_OFF, VISION_ON)
    assert refused.startswith("Error: this model cannot view images")
    assert "bash_tool" in refused
    assert sent == []
    assert view_with(VISION_ON, VISION_OFF) == "sandbox output"
    assert [call["tool"] for call in sent] == ["view"]


@pytest.mark.parametrize("path", ("/home/assistant/notes.txt", "/home/assistant"))
@pytest.mark.parametrize("model", (VISION_OFF, VISION_ON), ids=("vision-off", "vision-on"))
def test_view_of_text_and_directories_is_unchanged(monkeypatch, tmp_path, path, model):
    _image_env(monkeypatch)
    with _with_guard(monkeypatch, tmp_path) as ocu:
        result = asyncio.run(_guarded_tools(monkeypatch, ocu).view(
            "read", path, __metadata__={"chat_id": "chat-view-text", "model": model},
        ))

    assert result == f"viewed:{path}"
    assert ocu.calls == [("view", path)]


def test_text_extraction_for_other_tools_ignores_image_blocks():
    result = types.SimpleNamespace(isError=False, content=[
        TextContent(type="text", text="Image: /a/f.png"),
        ImageContent(type="image", data=VIEW_IMAGE_B64, mimeType="image/jpeg"),
    ])
    assert computer_use_tools._MCPClient._extract_text(result) == "Image: /a/f.png"


@pytest.mark.parametrize("method", PUBLIC_TOOLS)
def test_chat_images_sync_once_and_are_listed(monkeypatch, tmp_path, method):
    _image_env(monkeypatch)
    chat_id = f"gen-{method.replace('_', '-')}"
    messages = {
        "m1": _message(100, _image_ref(GEN_SUNSET)),
        "m2": _message(
            200,
            _image_ref(GEN_LOGO),
            {"type": "file", "id": DOC_BRIEF, "url": f"/api/v1/files/{DOC_BRIEF}/content"},
        ),
        "m3": _message(300),
    }
    webui = _chat_webui(monkeypatch, tmp_path, chat_id, messages, {
        GEN_SUNSET: _image_row("sunset.png", b"sunset bytes"),
        GEN_LOGO: _image_row("logo.png", b"logo bytes"),
        DOC_BRIEF: _row("brief.pdf", b"document bytes"),
    })

    with _with_guard(monkeypatch, tmp_path) as ocu:
        root = ocu.chat_uploads(chat_id)
        root.mkdir(parents=True)
        (root / "sunset.png").write_bytes(b"the user's own sunset")
        tools = _guarded_tools(monkeypatch, ocu)
        first = asyncio.run(_invoke_public_tool(tools, method, chat_id, []))
        webui.get_file_calls.clear()
        webui.lookups.clear()
        second = asyncio.run(_invoke_public_tool(tools, "bash_tool", chat_id, [], "echo second"))

        assert ocu.uploads == [
            {"filename": "logo.png", "attachment_id": GEN_LOGO},
            {"filename": "sunset.png", "attachment_id": GEN_SUNSET},
        ]
        assert ocu.stored_files(chat_id) == {
            "sunset.png": b"the user's own sunset",
            "logo.png": b"logo bytes",
            "sunset (2).png": b"sunset bytes",
        }
        assert "Synced image: /mnt/user-data/files/logo.png" in first
        assert "Synced image: /mnt/user-data/files/sunset (2).png" in first
        assert first.count("Synced image:") == 2
        # The second call skips both receipted ids before any DB lookup, Storage fetch or upload.
        assert second == "ran:echo second"
        assert webui.lookups == []
        assert webui.get_file_calls == []
        assert len(_records(ocu, f"/api/uploads/{chat_id}/logo.png")) == 1
        assert len(_records(ocu, f"/api/uploads/{chat_id}/sunset.png")) == 1
        assert ocu.calls[-1] == ("bash_tool", "echo second")


def test_chat_images_merge_with_attachments_and_only_chat_images_are_listed(
    monkeypatch, tmp_path
):
    _image_env(monkeypatch)
    chat_id = "gen-merge"
    _chat_webui(
        monkeypatch, tmp_path, chat_id,
        {"m1": _message(1, _image_ref(GEN_LOGO))},
        {GEN_LOGO: _image_row("logo.png", b"logo bytes"), "F1": _row("brief.docx", b"attachment")},
    )
    with _with_guard(monkeypatch, tmp_path) as ocu:
        result = asyncio.run(_invoke_public_tool(
            _guarded_tools(monkeypatch, ocu), "bash_tool", chat_id, [_attachment("F1", "brief.docx")]
        ))

    assert ocu.stored_files(chat_id) == {"brief.docx": b"attachment", "logo.png": b"logo bytes"}
    assert result == "ran:echo ready\n\nSynced image: /mnt/user-data/files/logo.png"


def test_missing_chat_image_file_is_skipped(monkeypatch, tmp_path):
    _image_env(monkeypatch)
    chat_id = "gen-missing"
    webui = _chat_webui(
        monkeypatch, tmp_path, chat_id,
        {"m1": _message(1, _image_ref(GEN_LOGO)), "m2": _message(2, _image_ref(GEN_MISSING))},
        {GEN_LOGO: _image_row("logo.png", b"logo bytes")},
    )
    with _with_guard(monkeypatch, tmp_path) as ocu:
        result = asyncio.run(_invoke_public_tool(
            _guarded_tools(monkeypatch, ocu), "bash_tool", chat_id, []
        ))

    assert GEN_MISSING in webui.lookups
    assert ocu.uploads == [{"filename": "logo.png", "attachment_id": GEN_LOGO}]
    assert result == "ran:echo ready\n\nSynced image: /mnt/user-data/files/logo.png"


@pytest.mark.parametrize(
    "fault",
    (RuntimeError("database unavailable"), None),
    ids=("chat-read-raises", "open-webui-not-importable"),
)
def test_chat_read_failure_does_not_fail_the_call(monkeypatch, tmp_path, fault):
    _image_env(monkeypatch)
    chat_id = "gen-broken"
    _chat_webui(
        monkeypatch, tmp_path, chat_id, {"m1": _message(1, _image_ref(GEN_LOGO))},
        {GEN_LOGO: _image_row("logo.png", b"logo bytes")}, chat_error=fault,
    )
    if fault is None:
        monkeypatch.setitem(sys.modules, "open_webui.models.chats", None)
    with _with_guard(monkeypatch, tmp_path) as ocu:
        result = asyncio.run(_invoke_public_tool(
            _guarded_tools(monkeypatch, ocu), "bash_tool", chat_id, []
        ))

    assert result == "ran:echo ready"
    assert ocu.uploads == []
    assert not _records(ocu, f"/api/uploads/{chat_id}/imports")


def test_chat_image_cap_applies_after_receipts_so_older_images_follow(monkeypatch, tmp_path):
    _image_env(monkeypatch)
    chat_id = "gen-cap"
    ids = [f"0a1b2c3d-0000-4000-8000-{n:012x}" for n in range(1, 26)]
    messages = {f"m{n}": _message(n, _image_ref(ids[n - 1])) for n in range(1, 26)}
    # The newest image also appears in an older message; it still counts once.
    messages["m0"] = _message(0, _image_ref(ids[-1]))
    webui = _chat_webui(
        monkeypatch, tmp_path, chat_id, messages,
        {file_id: _image_row(f"img{n}.png", b"x%d" % n) for n, file_id in enumerate(ids, 1)},
    )
    with _with_guard(monkeypatch, tmp_path) as ocu:
        tools = _guarded_tools(monkeypatch, ocu)
        first = asyncio.run(_invoke_public_tool(tools, "bash_tool", chat_id, []))
        first_lookups = list(webui.lookups)
        webui.lookups.clear()
        second = asyncio.run(_invoke_public_tool(tools, "bash_tool", chat_id, [], "echo second"))

        assert first_lookups == list(reversed(ids[5:]))
        assert first.count("Synced image:") == 20
        assert webui.lookups == list(reversed(ids[:5]))
        assert second.count("Synced image:") == 5
        assert sorted(ocu.stored_files(chat_id)) == sorted(f"img{n}.png" for n in range(1, 26))
        assert len(ocu.uploads) == 25
        assert len({upload["attachment_id"] for upload in ocu.uploads}) == 25


def test_chat_image_cap_without_readable_receipts_takes_the_20_newest(monkeypatch, tmp_path):
    _image_env(monkeypatch)
    chat_id = "gen-cap-blind"
    ids = [f"0a1b2c3d-0000-4000-8000-{n:012x}" for n in range(1, 26)]
    webui = _chat_webui(
        monkeypatch, tmp_path, chat_id,
        {f"m{n}": _message(n, _image_ref(ids[n - 1])) for n in range(1, 26)},
        {file_id: _image_row(f"img{n}.png", b"x%d" % n) for n, file_id in enumerate(ids, 1)},
    )
    with _with_guard(monkeypatch, tmp_path, imports_mode="fail") as ocu:
        asyncio.run(_invoke_public_tool(_guarded_tools(monkeypatch, ocu), "bash_tool", chat_id, []))

    assert webui.lookups == list(reversed(ids[5:]))
    assert sorted(ocu.stored_files(chat_id)) == sorted(f"img{n}.png" for n in range(6, 26))


def test_view_of_an_image_keeps_the_data_uri_bare_when_images_sync(monkeypatch, tmp_path):
    _image_env(monkeypatch)
    chat_id = "gen-view"
    _chat_webui(
        monkeypatch, tmp_path, chat_id, {"m1": _message(1, _image_ref(GEN_LOGO))},
        {GEN_LOGO: _image_row("logo.png", b"logo bytes")},
    )
    with _with_guard(monkeypatch, tmp_path) as ocu:
        result = asyncio.run(_guarded_tools(monkeypatch, ocu).view(
            "check", "/home/assistant/out/frame.png",
            __metadata__={"chat_id": chat_id}, __user__=OWNER,
        ))

    assert result == f"data:image/jpeg;base64,{VIEW_IMAGE_B64}"
    assert ocu.stored_files(chat_id) == {"logo.png": b"logo bytes"}


# --- Attachment and chat-image sync resolve files by id and check read access ---

SECRET = b"bytes outside the row"


def _sync_from(source, file_id, filename="pic.png"):
    """(chat messages, __files__) referencing file_id through one sync source."""
    if source == "attachment":
        return {}, [_attachment(file_id, filename)]
    return {"m1": _message(1, _image_ref(file_id))}, []


@pytest.mark.parametrize("source", ("attachment", "chat-image"))
@pytest.mark.parametrize("requester", (OWNER, ADMIN), ids=("user", "admin"))
def test_a_file_the_requester_cannot_read_is_not_synced(monkeypatch, tmp_path, source, requester):
    _image_env(monkeypatch)
    chat_id = f"refuse-{source}-{requester['role']}"
    messages, files = _sync_from(source, GEN_LOGO)
    webui = _chat_webui(
        monkeypatch, tmp_path, chat_id, messages,
        {GEN_LOGO: _image_row("pic.png", SECRET, user_id=OTHER["id"])},
        chat_owner=requester["id"],
    )
    with _with_guard(monkeypatch, tmp_path) as ocu:
        result = asyncio.run(_invoke_public_tool(
            _guarded_tools(monkeypatch, ocu), "bash_tool", chat_id, files, user=requester
        ))

    assert result == "ran:echo ready"
    assert webui.access_checks == [(GEN_LOGO, "read", requester["id"])]
    assert webui.get_file_calls == []
    assert ocu.uploads == []
    assert ocu.stored_files(chat_id) == {}


@pytest.mark.parametrize("source", ("attachment", "chat-image"))
def test_a_file_shared_with_read_access_is_synced(monkeypatch, tmp_path, source):
    _image_env(monkeypatch)
    chat_id = f"shared-{source}"
    messages, files = _sync_from(source, GEN_LOGO)
    webui = _chat_webui(
        monkeypatch, tmp_path, chat_id, messages,
        {GEN_LOGO: _image_row("pic.png", b"shared picture", user_id=OTHER["id"])},
        grants={(GEN_LOGO, OWNER["id"])},
    )
    with _with_guard(monkeypatch, tmp_path) as ocu:
        asyncio.run(_invoke_public_tool(_guarded_tools(monkeypatch, ocu), "bash_tool", chat_id, files))

    assert webui.access_checks == [(GEN_LOGO, "read", OWNER["id"])]
    assert webui.get_file_calls == [f"webui/{GEN_LOGO}"]
    assert ocu.stored_files(chat_id) == {"pic.png": b"shared picture"}


@pytest.mark.parametrize("source", ("attachment", "chat-image"))
def test_the_requesters_own_file_syncs_without_an_access_lookup(monkeypatch, tmp_path, source):
    _image_env(monkeypatch)
    chat_id = f"own-{source}"
    messages, files = _sync_from(source, GEN_LOGO)
    webui = _chat_webui(
        monkeypatch, tmp_path, chat_id, messages, {GEN_LOGO: _image_row("pic.png", b"mine")},
    )
    with _with_guard(monkeypatch, tmp_path) as ocu:
        asyncio.run(_invoke_public_tool(_guarded_tools(monkeypatch, ocu), "bash_tool", chat_id, files))

    assert webui.access_checks == []
    assert webui.get_file_calls == [f"webui/{GEN_LOGO}"]
    assert ocu.stored_files(chat_id) == {"pic.png": b"mine"}


def test_attachment_sync_reads_only_the_db_path(monkeypatch, tmp_path):
    _image_env(monkeypatch)
    canary = tmp_path / "elsewhere.bin"
    canary.write_bytes(SECRET)
    webui = _FakeWebUI(monkeypatch, tmp_path, files={"F1": _row("brief.docx", b"attachment")})
    files = [
        {"id": "F1", "name": "brief.docx", "path": str(canary), "file": {"path": str(canary)}},
        {"id": "F-unknown", "name": "other.bin", "path": str(canary), "file": {"path": str(canary)}},
    ]
    with _with_guard(monkeypatch, tmp_path) as ocu:
        result = asyncio.run(_invoke_public_tool(
            _guarded_tools(monkeypatch, ocu), "bash_tool", "forged-path", files
        ))

    assert result == "ran:echo ready"
    assert webui.get_file_calls == ["webui/F1"]
    assert ocu.stored_files("forged-path") == {"brief.docx": b"attachment"}
    assert ocu.uploads == [{"filename": "brief.docx", "attachment_id": "F1"}]


def test_without_a_requester_nothing_syncs(monkeypatch, tmp_path):
    _image_env(monkeypatch)
    webui = _chat_webui(
        monkeypatch, tmp_path, "no-user", {"m1": _message(1, _image_ref(GEN_LOGO))},
        {GEN_LOGO: _image_row("logo.png", b"logo bytes"), "F1": _row("brief.docx", b"a")},
    )
    with _with_guard(monkeypatch, tmp_path) as ocu:
        result = asyncio.run(_invoke_public_tool(
            _guarded_tools(monkeypatch, ocu), "bash_tool", "no-user",
            [_attachment("F1", "brief.docx")], user=None,
        ))

    assert result == "ran:echo ready"
    assert webui.get_file_calls == []
    assert ocu.uploads == []


def test_chat_images_of_a_chat_the_requester_does_not_own_are_not_read(monkeypatch, tmp_path):
    _image_env(monkeypatch)
    webui = _chat_webui(
        monkeypatch, tmp_path, "owners-chat", {"m1": _message(1, _image_ref(GEN_LOGO))},
        {GEN_LOGO: _image_row("logo.png", b"logo bytes")}, chat_owner=OWNER["id"],
    )
    with _with_guard(monkeypatch, tmp_path) as ocu:
        result = asyncio.run(_invoke_public_tool(
            _guarded_tools(monkeypatch, ocu), "bash_tool", "owners-chat", [], user=ADMIN
        ))

    assert result == "ran:echo ready"
    assert webui.lookups == []
    assert ocu.uploads == []


def test_a_chat_image_entry_naming_a_non_image_file_is_not_synced(monkeypatch, tmp_path):
    _image_env(monkeypatch)
    webui = _chat_webui(
        monkeypatch, tmp_path, "not-an-image", {"m1": _message(1, _image_ref(DOC_BRIEF))},
        {DOC_BRIEF: _row("brief.pdf", b"document", content_type="application/pdf")},
    )
    with _with_guard(monkeypatch, tmp_path) as ocu:
        result = asyncio.run(_invoke_public_tool(
            _guarded_tools(monkeypatch, ocu), "bash_tool", "not-an-image", []
        ))

    assert result == "ran:echo ready"
    assert webui.lookups == [DOC_BRIEF]
    assert webui.get_file_calls == []
    assert ocu.uploads == []

# --- copy_knowledge_file: a knowledge-base file the chat owner can read (#786) ---

KB_FILE = "F-kb"
KB_NAME = "报告.docx"
KB_BYTES = b"knowledge original"
KB_CHAT = "chat-kb"
COPIED = f"Copied {KB_NAME} to /mnt/user-data/files/{KB_NAME}"
UNREADABLE = f"Error: file {KB_FILE} is not in a knowledge base the chat owner can read"
COPY_PROGRESS = {"description": "read the report", "status": "in_progress", "done": False}
COPY_DONE = {"description": "Knowledge file copied", "status": "complete", "done": True}
COPY_FAILED = {"description": "Knowledge file copy failed", "status": "error", "done": True}
KB_OF_OTHER = {"K": (OTHER["id"], {KB_FILE})}


def _kb_webui(monkeypatch, tmp_path, chat_owner=OWNER, files=None, knowledge=None, **kwargs):
    if files is None:
        files = {KB_FILE: _row(KB_NAME, KB_BYTES, user_id=OTHER["id"])}
    if knowledge is None:
        knowledge = {"K": (chat_owner["id"], {KB_FILE})}
    return _FakeWebUI(
        monkeypatch, tmp_path, files=files, knowledge=knowledge,
        chats={KB_CHAT: (chat_owner["id"], {})}, **kwargs,
    )


def _copy_file(tools, chat_id=KB_CHAT, user=OWNER, description="read the report"):
    """Call the tool with the injections Open WebUI offers, filtered by its signature."""
    events = []
    offered = {
        "__event_emitter__": lambda e: _collect(events, e),
        "__metadata__": {"chat_id": chat_id},
        "__user__": user,
        "__files__": [_attachment("F-attached", "attached.docx")],
        "__request__": None,
    }
    accepted = inspect.signature(tools.copy_knowledge_file).parameters
    injected = {name: value for name, value in offered.items() if name in accepted}
    return asyncio.run(tools.copy_knowledge_file(KB_FILE, description, **injected)), events


def _assert_uploaded(ocu, webui, chat_id, events, posts=1, progress=COPY_PROGRESS):
    """Only upload POSTs with both headers reached OCU: no /imports, no /mcp."""
    assert [(r["method"], r["path"]) for r in ocu.requests] == [
        ("POST", f"/api/uploads/{chat_id}/{KB_NAME}")
    ] * posts
    assert all(
        r["headers"].get("x-ocu-attachment-id") == KB_FILE
        and r["headers"].get("x-ocu-reimport-missing") == "1"
        for r in ocu.requests
    )
    assert ocu.uploads == [
        {"filename": KB_NAME, "attachment_id": KB_FILE, "reimport_missing": "1"}
    ] * posts
    assert webui.access_checks == []
    assert _workspace_hints(events) == [_hint(chat_id)] * posts
    assert [e["data"] for e in _status_events(events)] == [progress, COPY_DONE] * posts


def _assert_refused(ocu, webui, result, events, expected):
    assert result == expected
    assert ocu.requests == []
    assert webui.access_checks == []
    assert webui.get_file_calls == []
    assert _workspace_hints(events) == []
    assert [e["data"] for e in _status_events(events)] == [COPY_PROGRESS, COPY_FAILED]


@pytest.mark.parametrize(
    ("caller", "chat_owner", "setup", "expected"),
    (
        pytest.param(OWNER, OWNER, {}, COPIED, id="owner-owned-kb"),
        pytest.param(OWNER, OWNER, {
            "knowledge": KB_OF_OTHER, "kb_grants": [("knowledge", "K", "user", OWNER["id"], "read")],
        }, COPIED, id="direct-grant"),
        pytest.param(OWNER, OWNER, {
            "knowledge": KB_OF_OTHER, "kb_grants": [("knowledge", "K", "group", "G", "read")],
            "groups": {OWNER["id"]: ["G"], OTHER["id"]: ["G-other"]},
        }, COPIED, id="group-grant"),
        pytest.param(OWNER, OWNER, {
            "knowledge": KB_OF_OTHER, "kb_grants": [("knowledge", "K", "user", "*", "read")],
        }, COPIED, id="open-to-everyone"),
        pytest.param(OWNER, OWNER, {
            "knowledge": {"K1": (OTHER["id"], {KB_FILE}), "K2": (OWNER["id"], {KB_FILE})},
        }, COPIED, id="several-kbs"),
        pytest.param(OWNER, OWNER, {"knowledge": KB_OF_OTHER}, UNREADABLE, id="ungranted"),
        pytest.param(OWNER, OWNER, {
            "knowledge": KB_OF_OTHER, "kb_grants": [("knowledge", "K", "user", OWNER["id"], "write")],
        }, UNREADABLE, id="write-only-grant"),
        pytest.param(ADMIN, OWNER, {"knowledge": {"K": (ADMIN["id"], {KB_FILE})}},
                     UNREADABLE, id="admin-in-another-users-chat"),
        pytest.param(OTHER, OWNER, {}, COPIED, id="other-caller-owner-may-read"),
        pytest.param(ADMIN, ADMIN, {"knowledge": KB_OF_OTHER}, UNREADABLE, id="admin-owner-without-grant"),
        pytest.param(OWNER, OWNER, {
            "files": {KB_FILE: _row(KB_NAME, KB_BYTES, user_id=OTHER["id"], collection_name="K")},
            "knowledge": {"K": (OWNER["id"], set())},
        }, UNREADABLE, id="legacy-name-only"),
        pytest.param(OWNER, OWNER, {
            "files": {KB_FILE: _row(KB_NAME, KB_BYTES)}, "knowledge": {},
        }, UNREADABLE, id="own-upload-outside-any-kb"),
        pytest.param(OWNER, OWNER, {"files": {}}, f"Error: file {KB_FILE} not found", id="unknown-id"),
        pytest.param(OWNER, OWNER, {
            "files": {KB_FILE: _row(KB_NAME, KB_BYTES, user_id=OTHER["id"], path=None)},
        }, f"Error: file {KB_FILE} has no stored bytes", id="no-stored-bytes"),
        pytest.param(OWNER, OWNER, {
            "files": {KB_FILE: _row(KB_NAME, KB_BYTES, user_id=OTHER["id"], path=None)},
            "knowledge": KB_OF_OTHER,
        }, UNREADABLE, id="unreadable-hides-missing-bytes"),
    ),
)
def test_copy_knowledge_file_judges_the_chat_owners_access(
    monkeypatch, tmp_path, caller, chat_owner, setup, expected
):
    _image_env(monkeypatch)
    webui = _kb_webui(monkeypatch, tmp_path, chat_owner, **setup)
    with _with_guard(monkeypatch, tmp_path) as ocu:
        result, events = _copy_file(_guarded_tools(monkeypatch, ocu), user=caller)

    if expected == COPIED:
        assert result == COPIED
        _assert_uploaded(ocu, webui, KB_CHAT, events)
        assert ocu.stored_files(KB_CHAT) == {KB_NAME: KB_BYTES}
        assert webui.get_file_calls == [f"webui/{KB_FILE}"]
    else:
        _assert_refused(ocu, webui, result, events, expected)
        assert ocu.stored_files(KB_CHAT) == {}
    assert webui.chat_lookups == [KB_CHAT]
    assert webui.knowledge_lookups == ([] if "not found" in expected else [KB_FILE])
    owner_groups = set(setup.get("groups", {}).get(chat_owner["id"], ()))
    assert all(check == (chat_owner["id"], "knowledge", check[2], "read", owner_groups)
               for check in webui.grant_checks)
    assert set(webui.group_lookups) <= {chat_owner["id"]}


def _seeded_copies(monkeypatch, tmp_path, between):
    """Copy twice in one chat, running between(outputs dir) after the first copy."""
    _image_env(monkeypatch)
    webui = _kb_webui(monkeypatch, tmp_path)
    with _with_guard(monkeypatch, tmp_path) as ocu:
        tools = _guarded_tools(monkeypatch, ocu)
        first, first_events = _copy_file(tools)
        between(ocu.chat_uploads(KB_CHAT))
        second, second_events = _copy_file(tools)
    assert first == second == COPIED
    _assert_uploaded(ocu, webui, KB_CHAT, first_events + second_events, posts=2)
    return ocu


def test_copying_a_file_twice_in_one_chat_stores_one_file(monkeypatch, tmp_path):
    ocu = _seeded_copies(monkeypatch, tmp_path, lambda root: None)
    assert ocu.stored_files(KB_CHAT) == {KB_NAME: KB_BYTES}


def test_a_deleted_workspace_file_is_copied_again(monkeypatch, tmp_path):
    ocu = _seeded_copies(monkeypatch, tmp_path, lambda root: (root / KB_NAME).unlink())
    assert ocu.stored_files(KB_CHAT) == {KB_NAME: KB_BYTES}


def test_an_edited_workspace_file_is_kept(monkeypatch, tmp_path):
    ocu = _seeded_copies(monkeypatch, tmp_path, lambda root: (root / KB_NAME).write_bytes(b"edited"))
    assert ocu.stored_files(KB_CHAT) == {KB_NAME: b"edited"}


def test_copy_into_an_occupied_name_returns_the_suffixed_path(monkeypatch, tmp_path):
    _image_env(monkeypatch)
    webui = _kb_webui(monkeypatch, tmp_path)
    with _with_guard(monkeypatch, tmp_path) as ocu:
        root = ocu.chat_uploads(KB_CHAT)
        root.mkdir(parents=True)
        (root / KB_NAME).write_bytes(b"unrelated")
        result, events = _copy_file(_guarded_tools(monkeypatch, ocu))

    assert result == f"Copied {KB_NAME} to /mnt/user-data/files/报告 (2).docx"
    assert ocu.stored_files(KB_CHAT) == {KB_NAME: b"unrelated", "报告 (2).docx": KB_BYTES}
    _assert_uploaded(ocu, webui, KB_CHAT, events)


def test_copy_reports_a_discovery_failure_after_storing(monkeypatch, tmp_path):
    _image_env(monkeypatch)
    webui = _kb_webui(monkeypatch, tmp_path)
    with _with_guard(monkeypatch, tmp_path) as ocu:
        root = ocu.chat_uploads(KB_CHAT)
        root.mkdir(parents=True)
        with monkeypatch.context() as faults:
            _fail_directory_enumeration(faults, root)
            result, events = _copy_file(_guarded_tools(monkeypatch, ocu))

    assert result == (
        f"{COPIED}\n\n[DISCOVERY WARNING] File '{KB_NAME}' was stored, but workspace "
        "file discovery failed (io_error). Do not upload it again."
    )
    assert ocu.stored_files(KB_CHAT) == {KB_NAME: KB_BYTES}
    _assert_uploaded(ocu, webui, KB_CHAT, events)


def test_an_empty_description_shows_the_fallback_progress_text(monkeypatch, tmp_path):
    _image_env(monkeypatch)
    webui = _kb_webui(monkeypatch, tmp_path)
    with _with_guard(monkeypatch, tmp_path) as ocu:
        result, events = _copy_file(_guarded_tools(monkeypatch, ocu), description="")

    assert result == COPIED
    _assert_uploaded(ocu, webui, KB_CHAT, events, progress={
        "description": "Copying knowledge file...", "status": "in_progress", "done": False,
    })


def test_copy_storage_error_is_an_upload_error_and_writes_nothing(monkeypatch, tmp_path):
    _image_env(monkeypatch)
    webui = _kb_webui(monkeypatch, tmp_path, files={
        KB_FILE: _row(KB_NAME, KB_BYTES, user_id=OTHER["id"], path="webui/gone"),
    })
    with _with_guard(monkeypatch, tmp_path) as ocu:
        result, events = _copy_file(_guarded_tools(monkeypatch, ocu))
        assert ocu.requests == []
        imports = requests.get(
            f"{ocu.url}/api/uploads/{KB_CHAT}/imports",
            headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"}, timeout=5,
        )

    assert result == f"Error: upload of {KB_NAME} failed"
    assert webui.get_file_calls == ["webui/gone"]
    assert webui.access_checks == []
    assert ocu.stored_files(KB_CHAT) == {}
    assert imports.json()["ids"] == []
    assert _workspace_hints(events) == []
    assert [e["data"] for e in _status_events(events)] == [COPY_PROGRESS, COPY_FAILED]


def test_copy_upload_http_error_is_an_upload_error_without_retry(monkeypatch, tmp_path):
    _image_env(monkeypatch)
    webui = _kb_webui(monkeypatch, tmp_path)
    with _local_origin(lambda request: (500, {}, b"storage failed")) as origin:
        tools = _tools_with_token(monkeypatch)
        tools.valves.ORCHESTRATOR_URL = origin.url
        result, events = _copy_file(tools)

    assert result == f"Error: upload of {KB_NAME} failed"
    assert [(r["method"], r["path"]) for r in origin.requests] == [
        ("POST", f"/api/uploads/{KB_CHAT}/{urllib.parse.quote(KB_NAME, safe='')}")
    ]
    assert origin.requests[0]["headers"].get("x-ocu-attachment-id") == KB_FILE
    assert origin.requests[0]["headers"].get("x-ocu-reimport-missing") == "1"
    assert webui.access_checks == []
    assert _workspace_hints(events) == []


def test_copy_to_an_unreachable_server_is_an_upload_error(monkeypatch, tmp_path):
    _image_env(monkeypatch)
    _kb_webui(monkeypatch, tmp_path)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    tools = _tools_with_token(monkeypatch)
    tools.valves.ORCHESTRATOR_URL = f"http://127.0.0.1:{port}"

    result, _ = _copy_file(tools)
    assert result == f"Error: upload of {KB_NAME} failed"


@pytest.mark.parametrize(
    "fault", ("get_file_by_id", "get_knowledges_by_file_id", "get_groups_by_member_id", "has_access")
)
def test_a_failing_lookup_is_an_error_result(monkeypatch, tmp_path, fault):
    _image_env(monkeypatch)
    webui = _kb_webui(monkeypatch, tmp_path, knowledge=KB_OF_OTHER, faults={fault: RuntimeError("db down")})
    with _with_guard(monkeypatch, tmp_path) as ocu:
        result, events = _copy_file(_guarded_tools(monkeypatch, ocu))

    _assert_refused(ocu, webui, result, events, f"Error: lookup of file {KB_FILE} failed")


@pytest.mark.parametrize("chat", ("missing", "raises", "no-owner"))
def test_copy_in_a_chat_that_cannot_be_found_looks_up_no_file(monkeypatch, tmp_path, chat):
    _image_env(monkeypatch)
    webui = _kb_webui(monkeypatch, tmp_path, chat_error=RuntimeError("db down") if chat == "raises" else None)
    if chat == "missing":
        webui.chats.clear()
    if chat == "no-owner":
        webui.chats[KB_CHAT] = ("", {})
    with _with_guard(monkeypatch, tmp_path) as ocu:
        result, events = _copy_file(_guarded_tools(monkeypatch, ocu))

    _assert_refused(ocu, webui, result, events, "Error: chat not found")
    assert webui.chat_lookups == [KB_CHAT]
    assert webui.lookups == webui.knowledge_lookups == []


def _block_network(monkeypatch):
    calls = []

    def blocked(*args, **kwargs):
        calls.append((args, kwargs))
        raise OSError("network must not run")

    monkeypatch.setattr(requests, "get", blocked)
    monkeypatch.setattr(requests, "post", blocked)
    monkeypatch.setattr(computer_use_tools.urllib.request.OpenerDirector, "open", blocked)
    return calls


def _assert_no_webui_lookups(webui):
    assert webui.lookups == webui.chat_lookups == webui.knowledge_lookups == []
    assert webui.group_lookups == webui.grant_checks == webui.get_file_calls == webui.access_checks == []


@pytest.mark.parametrize(
    ("internal", "mcp_key", "expected"),
    (
        ("", "", "[CONFIG ERROR] OCU_INTERNAL_TOKEN must be a non-empty HTTP-safe server credential."),
        (INTERNAL_TOKEN, "invalid\r\nmcp key",
         "[CONFIG ERROR] MCP_API_KEY must be empty or safely transmittable as an HTTP header."),
    ),
)
def test_copy_with_unusable_credentials_rejects_before_any_lookup(
    monkeypatch, tmp_path, internal, mcp_key, expected
):
    webui = _kb_webui(monkeypatch, tmp_path)
    calls = _block_network(monkeypatch)
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", internal)
    tools = computer_use_tools.Tools()
    tools.valves.MCP_API_KEY = mcp_key
    result, events = _copy_file(tools)

    assert result == expected
    assert calls == []
    _assert_no_webui_lookups(webui)
    assert _workspace_hints(events) == []
    assert [e["data"] for e in _status_events(events)] == [
        {"description": "Computer Use configuration error", "status": "error", "done": True}
    ]


@pytest.mark.parametrize(
    "metadata",
    (None, {"chat_id": None}, {"chat_id": ""}, {"chat_id": " \t"}),
    ids=("absent", "none", "empty", "whitespace"),
)
def test_empty_chat_rejects_copy_knowledge_file_before_work(monkeypatch, tmp_path, metadata):
    webui = _kb_webui(monkeypatch, tmp_path)
    calls = _block_network(monkeypatch)

    def fall_through(*args, **kwargs):
        raise AssertionError("sub_agent must not run")

    monkeypatch.setattr(computer_use_tools.Tools, "sub_agent", fall_through)
    tools = _tools_with_token(monkeypatch)
    events = []
    result = asyncio.run(tools.copy_knowledge_file(
        KB_FILE, "read the report",
        __event_emitter__=lambda e: _collect(events, e), __metadata__=metadata,
    ))

    assert result == "[TOOL ERROR] chat_id is required."
    assert calls == []
    _assert_no_webui_lookups(webui)
    assert _workspace_hints(events) == []
    assert [e["data"] for e in _status_events(events)] == [
        {"description": "Chat ID is required", "status": "error", "done": True}
    ]


def test_copy_knowledge_file_description_carries_the_flow():
    doc = computer_use_tools.Tools.copy_knowledge_file.__doc__
    # Open WebUI's parse_description: the lines before the first :param or :return line.
    description = []
    for line in (line.strip() for line in doc.strip().split("\n")):
        if re.match(r":param", line) or re.match(r":return", line):
            break
        description.append(line)
    description = "\n".join(description)

    assert "search_knowledge_files" in description or "query_knowledge_files" in description
    for token in ("docx", "pdf", "/mnt/user-data/files/", "!["):
        assert token in description
    params = {m.group(1) for m in (re.match(r":param (\w+):", line.strip()) for line in doc.splitlines()) if m}
    assert params == {"file_id", "description"}
    assert "__files__" not in inspect.signature(computer_use_tools.Tools.copy_knowledge_file).parameters


if __name__ == "__main__":
    unittest.main()
