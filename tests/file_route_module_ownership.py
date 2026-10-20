# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Module-cache ownership shared by file-route, auth-guard and WebSocket fixtures."""

import sys
from contextlib import contextmanager

_FILE_ROUTE_MODULES = {
    "app", "auth_guard", "mcp_tools", "docker_manager", "outputs_broker",
    "context_vars", "security", "system_prompt", "skill_manager", "file_response",
    "descriptor_io", "uploads",
}


@contextmanager
def isolated_modules(names, prefixes=()):
    prefixes = tuple(prefixes)
    snapshot = {
        name: module for name, module in sys.modules.copy().items()
        if name in names or name.startswith(prefixes)
    }
    try:
        for name in snapshot:
            sys.modules.pop(name, None)
        yield
    finally:
        for name in list(sys.modules):
            if name in names or name.startswith(prefixes):
                sys.modules.pop(name, None)
        sys.modules.update(snapshot)


def isolated_file_route_modules():
    return isolated_modules(_FILE_ROUTE_MODULES, ("mcp_resources",))
