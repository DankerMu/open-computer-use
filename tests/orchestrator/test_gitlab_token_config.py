# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""#776: the GitLab token fetch reads only its own URL and key.

`MCP_TOKENS_URL`/`MCP_TOKENS_API_KEY` belong to the skill registry. The GitLab
fetch uses the header pair `X-Mcp-Tokens-Url`/`X-Mcp-Tokens-Api-Key` when the URL
header is set, else `GITLAB_TOKENS_URL`/`GITLAB_TOKENS_API_KEY`, and sends nothing
when the chosen URL or key is empty. Every case runs against a loopback
`aiohttp.web` stub that records each request.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from tests.file_route_module_ownership import (
    _FRESH_IMPORT_MODULES,
    _SERVER_PREFIXES,
    isolated_modules,
)

SERVER_DIR = Path(__file__).resolve().parents[2] / "computer-use-server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

EMAIL = "alice@example.test"
REGISTRY_KEY = "synthetic-registry-key"
GITLAB_KEY = "synthetic-gitlab-key"
HEADER_KEY = "synthetic-header-key"
TOKEN_PATH = f"/api/internal/tokens/{EMAIL}/gitlab"
ELSEWHERE = "http://127.0.0.1:9/elsewhere"  # never contacted: the header URL wins on the tool path
NAMES = ("GITLAB_TOKENS_URL", "GITLAB_TOKENS_API_KEY", "MCP_TOKENS_URL", "MCP_TOKENS_API_KEY")


class Stub:
    """Records (method, path, X-Internal-Api-Key) and answers with `mode`."""

    def __init__(self, mode):
        self.mode = mode
        self.requests = []
        self.release = asyncio.Event()

    async def handle(self, request):
        self.requests.append((request.method, request.path, request.headers.get("X-Internal-Api-Key")))
        if self.mode == "hang":
            try:
                await asyncio.wait_for(self.release.wait(), 7)
            except TimeoutError:
                pass
        if self.mode == "404":
            return web.json_response({"detail": "not found"}, status=404)
        return web.json_response({"token": "t"})


@pytest.fixture
def dm(fresh_server_modules, monkeypatch):
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)
    return importlib.import_module("docker_manager")


def _configure(monkeypatch, dm, **values):
    for name in NAMES:
        monkeypatch.setattr(dm, name, values.get(name, ""), raising=False)


def _run(dm, mode, configure, header_url=None, header_key="", server_path=True):
    """Run the tool path (and the server path) against one stub; `configure(base)` sets the module pair."""

    async def body():
        stub = Stub(mode)
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", stub.handle)
        server = TestServer(app, host="127.0.0.1", port=0)
        await server.start_server()
        try:
            base = str(server.make_url("")).rstrip("/")
            configure(base)
            dm.current_gitlab_token.set(None)
            dm.current_user_email.set(EMAIL)
            dm.current_mcp_tokens_url.set(base if header_url == "stub" else (header_url or ""))
            dm.current_mcp_tokens_api_key.set(header_key)

            started = time.monotonic()
            await dm._ensure_gitlab_token()
            ensure_seconds = time.monotonic() - started
            tool_token = dm.current_gitlab_token.get()

            server_token = server_seconds = None
            if server_path:
                started = time.monotonic()
                server_token = await asyncio.to_thread(dm._server_gitlab_token, EMAIL)
                server_seconds = time.monotonic() - started
        finally:
            stub.release.set()
            await server.close()
        return SimpleNamespace(
            requests=stub.requests, tool_token=tool_token, server_token=server_token,
            ensure_seconds=ensure_seconds, server_seconds=server_seconds,
        )

    # asyncio.run runs body in a copy of this context, so the ContextVar sets stay inside it.
    return asyncio.run(body())


def _registry_only(monkeypatch, dm):
    return lambda base: _configure(
        monkeypatch, dm, MCP_TOKENS_URL=base, MCP_TOKENS_API_KEY=REGISTRY_KEY,
    )


def _gitlab_pair(monkeypatch, dm):
    return lambda base: _configure(
        monkeypatch, dm, GITLAB_TOKENS_URL=base, GITLAB_TOKENS_API_KEY=GITLAB_KEY,
    )


def test_a_registry_config_alone_sends_no_gitlab_request(dm, monkeypatch):
    result = _run(dm, "200", _registry_only(monkeypatch, dm))
    assert result.requests == []
    assert result.tool_token is None
    assert result.server_token is None


def test_b_hanging_registry_does_not_delay_either_path(dm, monkeypatch):
    result = _run(dm, "hang", _registry_only(monkeypatch, dm))
    assert (result.ensure_seconds < 1, result.server_seconds < 1) == (True, True), result
    assert result.requests == []


def test_c_configured_gitlab_pair_fetches_the_token(dm, monkeypatch):
    result = _run(dm, "200", _gitlab_pair(monkeypatch, dm))
    assert result.tool_token == "t"
    assert result.server_token == "t"
    assert result.requests == [("GET", TOKEN_PATH, GITLAB_KEY)] * 2


def test_d_configured_gitlab_pair_404_gives_no_token(dm, monkeypatch):
    result = _run(dm, "404", _gitlab_pair(monkeypatch, dm))
    assert result.tool_token is None
    assert result.server_token is None
    assert result.requests == [("GET", TOKEN_PATH, GITLAB_KEY)] * 2


def test_e_header_url_without_header_key_sends_no_env_key(dm, monkeypatch):
    def configure(_base):
        _configure(
            monkeypatch, dm,
            GITLAB_TOKENS_URL=ELSEWHERE, GITLAB_TOKENS_API_KEY=GITLAB_KEY,
            MCP_TOKENS_URL=ELSEWHERE, MCP_TOKENS_API_KEY=GITLAB_KEY,
        )

    # Headers only reach the tool path; the server path reads the env pair alone.
    result = _run(dm, "200", configure, header_url="stub", header_key="", server_path=False)
    assert result.requests == []
    assert result.tool_token is None


def test_f_header_pair_overrides_env_pair(dm, monkeypatch):
    def configure(_base):
        _configure(
            monkeypatch, dm,
            GITLAB_TOKENS_URL=ELSEWHERE, GITLAB_TOKENS_API_KEY=GITLAB_KEY,
            MCP_TOKENS_URL=ELSEWHERE, MCP_TOKENS_API_KEY=REGISTRY_KEY,
        )

    result = _run(dm, "200", configure, header_url="stub", header_key=HEADER_KEY, server_path=False)
    assert result.requests == [("GET", TOKEN_PATH, HEADER_KEY)]
    assert result.tool_token == "t"


def test_g_import_reads_gitlab_names_and_drops_registry_names(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKENS_URL", "http://gitlab-tokens.example.test")
    monkeypatch.setenv("GITLAB_TOKENS_API_KEY", GITLAB_KEY)
    with isolated_modules(_FRESH_IMPORT_MODULES, _SERVER_PREFIXES):
        fresh = importlib.import_module("docker_manager")
        assert getattr(fresh, "GITLAB_TOKENS_URL", None) == "http://gitlab-tokens.example.test"
        assert getattr(fresh, "GITLAB_TOKENS_API_KEY", None) == GITLAB_KEY
        assert not hasattr(fresh, "MCP_TOKENS_URL")
        assert not hasattr(fresh, "MCP_TOKENS_API_KEY")
