# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""TEST-02-01: D-05 / D-06 — empty _OPENCODE_ALIAS_MAP and raising _resolve_opencode.

Covers:
  1. _OPENCODE_ALIAS_MAP literal is {} (no Anthropic baseline)
  2. _merge_opencode_alias_map() with OPENCODE_MODEL_ALIASES unset returns {}
  3. _resolve_opencode("sonnet", "sonnet") with env unset raises ValueError
  4. ValueError message contains required substrings per D-06
  5. With OPENCODE_MODEL_ALIASES='{"sonnet":"anthropic/claude-sonnet-4-6"}',
     _resolve_opencode("sonnet", "sonnet") returns ("anthropic/claude-sonnet-4-6", "sonnet")
  6. Provider/model pass-through: "openrouter/qwen/qwen-3-coder" (contains '/')
     bypasses alias map and returns verbatim

Env-scrub pattern mirrors tests/orchestrator/test_sub_agent_dispatch.py:48-55
so local-dev shells with model-override env vars do not break assertions.
"""

import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_SERVER_DIR = os.path.join(
    os.path.dirname(__file__), "..", "computer-use-server"
)
sys.path.insert(0, _SERVER_DIR)

from tests.file_route_module_ownership import isolated_modules  # noqa: E402

_ALIAS_RUNTIME_MODULES = ("cli_runtime", "docker_manager", "outputs_broker")

# Env vars that could affect opencode alias resolution — scrub before each test.
_OPENCODE_ENV_VARS = (
    "OPENCODE_MODEL_ALIASES",
    "OPENCODE_SUB_AGENT_DEFAULT_MODEL",
    "OPENCODE_MODEL",
)


def _drop_modules():
    """Drop cli_runtime + docker_manager so the next import re-reads env."""
    for mod in _ALIAS_RUNTIME_MODULES:
        sys.modules.pop(mod, None)


def _import_cli_runtime():
    _drop_modules()
    return importlib.import_module("cli_runtime")


@pytest.fixture
def alias_runtime_modules():
    """Own the runtime module cache entries that alias tests re-import."""
    with isolated_modules(_ALIAS_RUNTIME_MODULES):
        yield


@pytest.fixture(autouse=True)
def _scrub_opencode_env(alias_runtime_modules, monkeypatch):
    """Scrub opencode-related env vars before each test."""
    for var in _OPENCODE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield


def test_scrub_fixture_owns_runtime_modules(request):
    """The autouse env scrub keeps runtime module ownership in every alias test."""
    assert "alias_runtime_modules" in request.fixturenames


# ---------------------------------------------------------------------------
# Test 1: _OPENCODE_ALIAS_MAP literal is empty dict
# ---------------------------------------------------------------------------

def test_opencode_alias_map_is_empty_dict():
    """D-05: _OPENCODE_ALIAS_MAP must be an empty dict — no Anthropic baseline."""
    cli_runtime = _import_cli_runtime()
    assert cli_runtime._OPENCODE_ALIAS_MAP == {}, (
        f"Expected _OPENCODE_ALIAS_MAP == {{}} but got {cli_runtime._OPENCODE_ALIAS_MAP!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: _merge_opencode_alias_map() with no env returns {}
# ---------------------------------------------------------------------------

def test_merge_opencode_alias_map_no_env_returns_empty():
    """D-05: with OPENCODE_MODEL_ALIASES unset, merged map is empty."""
    cli_runtime = _import_cli_runtime()
    result = cli_runtime._merge_opencode_alias_map()
    assert result == {}, (
        f"Expected empty merged map but got {result!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: _resolve_opencode raises ValueError for unresolvable alias
# ---------------------------------------------------------------------------

def test_resolve_opencode_missing_alias_raises_value_error():
    """D-06: alias 'sonnet' not in map, no env default -> ValueError."""
    cli_runtime = _import_cli_runtime()
    with pytest.raises(ValueError):
        cli_runtime._resolve_opencode("sonnet", "sonnet")


# ---------------------------------------------------------------------------
# Test 4: ValueError message contains required D-06 substrings
# ---------------------------------------------------------------------------

def test_resolve_opencode_missing_alias_error_message():
    """D-06: error message must name the alias, OPENCODE_MODEL_ALIASES, list-subagent-models."""
    cli_runtime = _import_cli_runtime()
    with pytest.raises(ValueError) as exc_info:
        cli_runtime._resolve_opencode("sonnet", "sonnet")
    msg = str(exc_info.value)
    assert "alias 'sonnet' is not defined" in msg, (
        f"Error message missing \"alias 'sonnet' is not defined\": {msg!r}"
    )
    assert "OPENCODE_MODEL_ALIASES" in msg, (
        f"Error message missing 'OPENCODE_MODEL_ALIASES': {msg!r}"
    )
    assert "list-subagent-models" in msg, (
        f"Error message missing 'list-subagent-models': {msg!r}"
    )


# ---------------------------------------------------------------------------
# Test 5: With OPENCODE_MODEL_ALIASES env set, alias resolves correctly
# ---------------------------------------------------------------------------

def test_resolve_opencode_alias_via_env(monkeypatch):
    """D-05 / D-06: operator-supplied alias in OPENCODE_MODEL_ALIASES resolves."""
    monkeypatch.setenv(
        "OPENCODE_MODEL_ALIASES",
        '{"sonnet": "anthropic/claude-sonnet-4-6"}',
    )
    cli_runtime = _import_cli_runtime()
    model_id, display = cli_runtime._resolve_opencode("sonnet", "sonnet")
    assert model_id == "anthropic/claude-sonnet-4-6"
    assert display == "sonnet"


# ---------------------------------------------------------------------------
# Test 6: Provider/model pass-through — string with '/' bypasses alias map
# ---------------------------------------------------------------------------

def test_resolve_opencode_provider_model_passthrough():
    """Existing pass-through: 'openrouter/qwen/qwen-3-coder' (contains '/')
    is returned verbatim — alias map lookup skipped."""
    cli_runtime = _import_cli_runtime()
    model_id, display = cli_runtime._resolve_opencode(
        "openrouter/qwen/qwen-3-coder",
        "openrouter/qwen/qwen-3-coder",
    )
    assert model_id == "openrouter/qwen/qwen-3-coder"
    assert display == "openrouter/qwen/qwen-3-coder"
