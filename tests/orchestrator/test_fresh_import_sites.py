# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""#730: each former reload site leaves the caller's server modules as it found them.

Every case runs one real site (fixture, test function or unittest case) inside its
own window that holds `docker_manager` and `outputs_broker` with every other
server module absent, then checks that window: the entry modules and their
classes are the same objects, and modules absent on entry are still absent.
"""
from __future__ import annotations

import contextlib
import importlib
import inspect
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.file_route_module_ownership import (
    _FRESH_IMPORT_MODULES,
    _SERVER_PREFIXES,
    isolated_modules,
)
from tests.orchestrator import (
    _office_store,
    conftest,
    test_lifecycle,
    test_mcp_resources,
    test_outputs_broker,
    test_passthrough_isolation,
    test_runtime_cli_endpoint,
    test_single_user_mode,
    test_sub_agent_dispatch,
    test_subagent_claude_compat,
    test_subagent_gateway,
)


def _tracked():
    return {
        name: module for name, module in sys.modules.items()
        if name in _FRESH_IMPORT_MODULES or name.startswith(_SERVER_PREFIXES)
    }


@pytest.fixture
def entry():
    with isolated_modules(_FRESH_IMPORT_MODULES, _SERVER_PREFIXES):
        manager = importlib.import_module("docker_manager")
        broker = importlib.import_module("outputs_broker")
        modules = _tracked()
        assert "mcp_tools" not in modules and "office.store" not in modules
        yield SimpleNamespace(
            modules=modules,
            classes=(manager.LifecycleError, manager.ChatDeleted, broker.OutputsBroker),
        )


def _assert_entry(entry):
    assert sorted(_tracked()) == sorted(entry.modules)
    for name, module in entry.modules.items():
        assert sys.modules[name] is module, name
    manager, broker = sys.modules["docker_manager"], sys.modules["outputs_broker"]
    assert (manager.LifecycleError, manager.ChatDeleted, broker.OutputsBroker) == entry.classes


def _drive(fixture, *args):
    """Run a generator fixture's setup and normal teardown, as pytest does."""
    lifecycle = fixture.__wrapped__(*args)
    value = next(lifecycle)
    with pytest.raises(StopIteration):
        next(lifecycle)
    return value


def _call(test, **fixtures):
    """Call a pytest test function with the fixtures its signature names."""
    names = inspect.signature(test).parameters
    with contextlib.ExitStack() as stack:
        if "fresh_server_modules" in names:
            window = contextlib.contextmanager(conftest.fresh_server_modules.__wrapped__)
            fixtures["fresh_server_modules"] = stack.enter_context(window())
        test(**{name: fixtures[name] for name in names})


def _run(test):
    result = unittest.TestResult()
    test.run(result)
    assert result.wasSuccessful(), result.failures + result.errors


@pytest.mark.parametrize("fixture", [_office_store.world, test_outputs_broker.world],
                         ids=["office-store", "outputs-broker"])
def test_data_root_world_reads_its_root_and_restores(entry, fixture, tmp_path):
    with pytest.MonkeyPatch.context() as monkeypatch:
        _module, manager, data = _drive(fixture, monkeypatch, tmp_path)
    _assert_entry(entry)
    assert manager.BASE_DATA_DIR == data


def test_lifecycle_world_restores(entry, tmp_path):
    with pytest.MonkeyPatch.context() as monkeypatch:
        _drive(test_lifecycle.world, monkeypatch, tmp_path)
    _assert_entry(entry)


def test_lifecycle_app_module_restores_its_set_and_shares_docker_manager(entry):
    # The narrower set is the site's design: docker_manager stays shared with `world`,
    # so other server modules app first-imports remain, bound to the entry docker_manager.
    with patch.dict(os.environ):
        loaded = _drive(test_lifecycle.app_module)
    assert loaded.LifecycleError is entry.classes[0]
    owned = [name for name in sys.modules
             if name in {"app", "mcp_tools", "file_response"} or name.startswith("mcp_resources")]
    assert owned == []
    assert sys.modules["docker_manager"] is entry.modules["docker_manager"]
    assert sys.modules["docker_manager"].LifecycleError is entry.classes[0]


def test_passthrough_isolation_restores(entry, tmp_path):
    _call(test_passthrough_isolation.test_passthrough_isolation, cli="claude",
          expected_keys={"ANTHROPIC_AUTH_TOKEN"}, forbidden_keys={"OPENAI_API_KEY"},
          expected_opencode_config=None, tmp_path=tmp_path)
    _assert_entry(entry)


def test_runtime_cli_endpoint_restores(entry):
    _run(test_runtime_cli_endpoint.RuntimeCliEndpointContract("test_codex"))
    _assert_entry(entry)


def test_subagent_gateway_world_restores(entry, capsys, caplog):
    with pytest.MonkeyPatch.context() as monkeypatch:
        _call(test_subagent_gateway.test_missing_gateway_token_is_not_replaced_by_other_anthropic_tokens,
              monkeypatch=monkeypatch, capsys=capsys, caplog=caplog)
    _assert_entry(entry)


def test_sub_agent_dispatch_restores(entry):
    with pytest.MonkeyPatch.context() as monkeypatch:
        _call(test_sub_agent_dispatch.test_cost_rendering_dollar_for_float, monkeypatch=monkeypatch)
    _assert_entry(entry)


def test_subagent_claude_compat_restores(entry):
    argv = test_subagent_claude_compat.fixture.__wrapped__()
    with pytest.MonkeyPatch.context() as monkeypatch:
        _call(test_subagent_claude_compat.test_claude_dispatch_resume_byte_compat,
              fixture=argv, monkeypatch=monkeypatch)
    _assert_entry(entry)


@pytest.mark.parametrize("case", [
    test_single_user_mode.TestValidateChatId("test_multi_user_mode_no_chat_id_returns_error"),
    test_single_user_mode.TestDefaultChatWarning("test_multi_user_mode_no_warning"),
    test_single_user_mode.TestBashToolSingleUserMode("test_bash_tool_multi_user_no_chat_id_returns_error"),
], ids=["validate", "warning", "bash-tool"])
def test_single_user_mode_restores(entry, case):
    _run(case)
    _assert_entry(entry)


def test_mcp_resources_class_restores(entry):
    with patch.dict(os.environ):
        _run(unittest.TestSuite([test_mcp_resources.McpResourcesContract("test_flat_and_nested_list_read")]))
    _assert_entry(entry)
