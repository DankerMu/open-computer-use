# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Module-cache ownership shared by the response and header fixtures."""

import sys
from contextlib import contextmanager

_FILE_ROUTE_MODULES = {
    "app", "auth_guard", "mcp_tools", "docker_manager", "outputs_broker",
    "context_vars", "security", "system_prompt", "skill_manager", "file_response",
    "descriptor_io", "uploads",
}


@contextmanager
def isolated_file_route_modules():
    snapshot = {
        name: module for name, module in sys.modules.copy().items()
        if name in _FILE_ROUTE_MODULES or name.startswith("mcp_resources")
    }
    try:
        for name in snapshot:
            sys.modules.pop(name, None)
        yield
    finally:
        for name in list(sys.modules):
            if name in _FILE_ROUTE_MODULES or name.startswith("mcp_resources"):
                sys.modules.pop(name, None)
        sys.modules.update(snapshot)
