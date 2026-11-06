# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Orchestrator fixtures: Office store world, fresh server imports, module identity."""

import sys
import types

import pytest

from tests.file_route_module_ownership import (
    _FRESH_IMPORT_MODULES,
    _SERVER_MODULES,
    _SERVER_PREFIXES,
    isolated_modules,
)
from tests.orchestrator._office_store import world

__all__ = ["fresh_server_modules", "world"]


@pytest.fixture
def fresh_server_modules():
    """Server modules imported during the test are fresh and retired afterwards."""
    with isolated_modules(_FRESH_IMPORT_MODULES, _SERVER_PREFIXES):
        yield


def _own_definitions(name, module):
    return {
        key: value for key, value in vars(module).items()
        if isinstance(value, (type, types.FunctionType)) and value.__module__ == name
    }


def _server_modules():
    return {
        name: module for name, module in sys.modules.copy().items()
        if module is not None and (name in _SERVER_MODULES or name.startswith(_SERVER_PREFIXES))
    }


@pytest.fixture(scope="module", autouse=True)
def _server_module_identity(request):
    """#730: a test module leaves every server module it found, and its classes, in place.

    Modules first imported by the test module may stay (lazy imports are legitimate);
    per-site absence checks live in test_fresh_import_sites.py.
    """
    entry = {name: (module, _own_definitions(name, module)) for name, module in _server_modules().items()}
    yield
    where = request.module.__name__
    for name, (module, definitions) in entry.items():
        assert sys.modules.get(name) is module, f"{where} replaced or removed server module {name}"
        current = _own_definitions(name, module)
        changed = sorted(key for key, value in definitions.items() if current.get(key) is not value)
        assert not changed, (
            f"{where} replaced {len(changed)} classes or functions of server module {name}, "
            f"e.g. {changed[:5]}")
    manager = sys.modules.get("docker_manager")
    if manager is None:
        return
    for holder in ("mcp_tools", "app"):
        module = sys.modules.get(holder)
        if module is not None:
            assert module.LifecycleError is manager.LifecycleError, (
                f"{where} left {holder}.LifecycleError from another docker_manager")
    broker = sys.modules.get("outputs_broker")
    if broker is not None:
        assert broker.docker_manager is manager, (
            f"{where} left outputs_broker bound to another docker_manager")
