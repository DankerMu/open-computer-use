# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""CLI-runtime fixture isolation preserves pre-existing callers' imports."""

import importlib
import sys

import pytest

from tests.orchestrator import test_cli_runtime as cli_tests


def test_cli_fixture_restores_caller_module_identities_after_normal_exit(monkeypatch):
    owned_modules = ("cli_runtime", "docker_manager", "outputs_broker")
    snapshot = {name: sys.modules[name] for name in owned_modules
                if name in sys.modules}
    lifecycle = None
    try:
        monkeypatch.delenv("SUBAGENT_CLI", raising=False)
        cli_tests._reload_docker_manager()
        caller = {name: importlib.import_module(name) for name in owned_modules}
        caller_cli = caller["cli_runtime"].Cli
        caller_manager = caller["outputs_broker"].docker_manager
        assert caller_manager is caller["docker_manager"]

        lifecycle = cli_tests._clean_subagent_env.__wrapped__(monkeypatch)
        next(lifecycle)
        cli_tests._reload_docker_manager()
        fresh = {name: importlib.import_module(name) for name in owned_modules}
        for name in owned_modules:
            assert fresh[name] is not caller[name], name
        assert fresh["outputs_broker"].docker_manager is fresh["docker_manager"]
        assert fresh["cli_runtime"].Cli is not caller_cli
        with pytest.raises(StopIteration):
            next(lifecycle)

        for name in owned_modules:
            assert sys.modules.get(name) is caller[name], (
                f"CLI fixture did not restore caller module {name}"
            )
        assert importlib.import_module("cli_runtime").Cli is caller_cli
        assert importlib.import_module("outputs_broker").docker_manager is caller_manager
    finally:
        try:
            if lifecycle is not None:
                lifecycle.close()
        finally:
            for name in owned_modules:
                sys.modules.pop(name, None)
            sys.modules.update(snapshot)


def test_cli_fixture_leaves_owned_modules_absent_when_absent_at_entry(monkeypatch):
    owned_modules = ("cli_runtime", "docker_manager", "outputs_broker")
    snapshot = {name: sys.modules[name] for name in owned_modules
                if name in sys.modules}
    lifecycle = None
    try:
        for name in owned_modules:
            sys.modules.pop(name, None)
        assert all(name not in sys.modules for name in owned_modules)

        lifecycle = cli_tests._clean_subagent_env.__wrapped__(monkeypatch)
        next(lifecycle)
        for name in owned_modules:
            importlib.import_module(name)
        assert all(name in sys.modules for name in owned_modules)
        with pytest.raises(StopIteration):
            next(lifecycle)

        for name in owned_modules:
            assert name not in sys.modules, (
                f"CLI fixture left newly imported module {name} behind"
            )
    finally:
        try:
            if lifecycle is not None:
                lifecycle.close()
        finally:
            for name in owned_modules:
                sys.modules.pop(name, None)
            sys.modules.update(snapshot)
