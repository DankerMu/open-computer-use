# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Service-key comparisons for /mcp-info and /api/skill-stats.

Seam is the real FastAPI app through TestClient, loaded by the auth-guard
fixtures. The comparison is observed by wrapping the `_credential_matches`
name `app` calls; the status contract comes from the
ocu-service-key-constant-time fixture.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SERVER_DIR = ROOT / "computer-use-server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from tests.test_auth_guard import (  # noqa: E402,F401
    app_module,
    client,
    isolated_office_env,
)

STATS_KEY = "k1"
MCP_KEY = "k2"
# A key outside latin-1, written as escapes because the repo forbids Cyrillic
# source text (tests/test-no-cyrillic.sh).
NON_ASCII = "\u043a\u043b\u044e\u0447"
NON_ASCII_BYTES = NON_ASCII.encode()


@pytest.fixture
def calls(app_module, monkeypatch):
    recorded = []

    def wrapper(presented, secret):
        recorded.append((presented, secret))
        return sys.modules["auth_guard"]._credential_matches(presented, secret)

    monkeypatch.setattr(app_module, "_credential_matches", wrapper, raising=False)
    return recorded


@pytest.fixture
def stub_stats(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "_harvest_and_get_stats", lambda: {"stub": True})


def _assert_mcp_401(response, detail):
    assert response.status_code == 401
    assert response.json()["detail"] == detail
    assert response.headers["www-authenticate"] == "Bearer"


def test_skill_stats_compares_through_credential_matches(client, calls, stub_stats, monkeypatch):
    monkeypatch.setenv("MCP_TOKENS_API_KEY", STATS_KEY)

    response = client.get("/api/skill-stats", headers={"X-Internal-Api-Key": STATS_KEY})

    assert response.status_code == 200
    assert response.json() == {"stub": True}
    assert calls == [(STATS_KEY, STATS_KEY)]


def test_mcp_info_compares_through_credential_matches(client, app_module, calls, monkeypatch):
    monkeypatch.setattr(app_module, "MCP_API_KEY", MCP_KEY)

    response = client.get("/mcp-info", headers={"Authorization": f"Bearer {MCP_KEY}"})

    assert response.status_code == 200
    assert calls == [(MCP_KEY, MCP_KEY)]


def test_skill_stats_status_contract(client, stub_stats, monkeypatch):
    monkeypatch.setenv("MCP_TOKENS_API_KEY", STATS_KEY)

    missing = client.get("/api/skill-stats")
    assert missing.status_code == 403
    assert missing.json()["detail"] == "Forbidden"
    wrong = client.get("/api/skill-stats", headers={"X-Internal-Api-Key": "wrong"})
    assert wrong.status_code == 403
    assert wrong.json()["detail"] == "Forbidden"
    right = client.get("/api/skill-stats", headers={"X-Internal-Api-Key": STATS_KEY})
    assert right.status_code == 200
    assert right.json() == {"stub": True}

    monkeypatch.setenv("MCP_TOKENS_API_KEY", "")
    unset = client.get("/api/skill-stats")
    assert unset.status_code == 200
    assert unset.json() == {"stub": True}


def test_mcp_info_status_contract(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "MCP_API_KEY", MCP_KEY)

    _assert_mcp_401(client.get("/mcp-info"), "Missing Authorization header")
    _assert_mcp_401(
        client.get("/mcp-info", headers={"Authorization": "Bearer wrong"}),
        "Invalid API key",
    )
    assert client.get("/mcp-info", headers={"Authorization": f"Bearer {MCP_KEY}"}).status_code == 200

    monkeypatch.setattr(app_module, "MCP_API_KEY", None)
    assert client.get("/mcp-info").status_code == 200


def test_non_ascii_header_values_are_rejected(client, app_module, stub_stats, monkeypatch):
    monkeypatch.setenv("MCP_TOKENS_API_KEY", STATS_KEY)
    monkeypatch.setattr(app_module, "MCP_API_KEY", MCP_KEY)

    stats = client.get("/api/skill-stats", headers={"X-Internal-Api-Key": NON_ASCII_BYTES})
    assert stats.status_code == 403
    assert stats.json()["detail"] == "Forbidden"
    _assert_mcp_401(
        client.get("/mcp-info", headers={"Authorization": b"Bearer " + NON_ASCII_BYTES}),
        "Invalid API key",
    )


def test_non_latin1_configured_key_is_rejected(client, stub_stats, monkeypatch):
    monkeypatch.setenv("MCP_TOKENS_API_KEY", NON_ASCII)

    response = client.get("/api/skill-stats", headers={"X-Internal-Api-Key": NON_ASCII_BYTES})

    assert response.status_code == 403
    assert response.json()["detail"] == "Forbidden"
