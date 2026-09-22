# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Real-container credential contract for mounted MCP.

MCP requires X-OCU-Internal-Token. When MCP_API_KEY is configured it also
requires its existing Authorization Bearer credential; the values are distinct
and cannot substitute for one another.
"""
from __future__ import annotations

import httpx
import pytest

from conftest import mcp_request


def _init_payload() -> dict:
    return mcp_request(
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "integration-test", "version": "0.0.0"},
        },
    )


def _headers(orchestrator, chat_id, *, internal=None, mcp=None) -> dict:
    return {
        "X-OCU-Internal-Token": orchestrator["internal_token"] if internal is None else internal,
        "Authorization": f"Bearer {orchestrator['api_key'] if mcp is None else mcp}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "X-Chat-Id": chat_id,
    }


@pytest.mark.integration
def test_distinct_valid_credentials_return_200(orchestrator, chat_id):
    """Both known-good secrets are required and initialize remains useful."""
    with httpx.Client(base_url=orchestrator["url"], timeout=10.0) as c:
        r = c.post("/mcp", json=_init_payload(), headers=_headers(orchestrator, chat_id))
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text[:300]}"


@pytest.mark.integration
@pytest.mark.parametrize("missing", ("internal", "mcp"))
def test_missing_independent_credential_returns_401(orchestrator, chat_id, missing):
    headers = _headers(orchestrator, chat_id)
    if missing == "internal":
        headers.pop("X-OCU-Internal-Token")
    else:
        headers.pop("Authorization")
    with httpx.Client(base_url=orchestrator["url"], timeout=10.0) as c:
        r = c.post("/mcp", json=_init_payload(), headers=headers)
    assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text[:300]}"
    assert "bearer" in r.headers.get("www-authenticate", "").lower()


@pytest.mark.integration
@pytest.mark.parametrize("wrong", ("internal", "mcp"))
def test_wrong_independent_credential_returns_401(orchestrator, chat_id, wrong):
    headers = _headers(orchestrator, chat_id)
    if wrong == "internal":
        headers["X-OCU-Internal-Token"] = "obviously-wrong-internal-token"
    else:
        headers["Authorization"] = "Bearer obviously-wrong-mcp-token"
    with httpx.Client(base_url=orchestrator["url"], timeout=10.0) as c:
        r = c.post("/mcp", json=_init_payload(), headers=headers)
    assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text[:300]}"


@pytest.mark.integration
def test_credentials_cannot_substitute(orchestrator, chat_id):
    """Putting either valid secret in the other's carrier remains unauthorized."""
    with httpx.Client(base_url=orchestrator["url"], timeout=10.0) as c:
        internal_as_bearer = _headers(orchestrator, chat_id, mcp=orchestrator["internal_token"])
        mcp_as_internal = _headers(orchestrator, chat_id, internal=orchestrator["api_key"])
        first = c.post("/mcp", json=_init_payload(), headers=internal_as_bearer)
        second = c.post("/mcp", json=_init_payload(), headers=mcp_as_internal)
    assert first.status_code == 401
    assert second.status_code == 401
