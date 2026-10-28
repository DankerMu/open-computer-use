# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Shared module-cache ownership of every fixture that imports the server.

The file-route, auth-guard and WebSocket owners all own `_SERVER_MODULES` plus
`_SERVER_PREFIXES`: the whole server directory except the standalone `drawio/`
script, so each fixture generation binds only modules of its own generation.
"""

import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = ROOT / "computer-use-server"
DRAWIO_DIR = SERVER_DIR / "drawio"
_SERVER_MODULES = {
    "app", "auth_guard", "ws_recheck", "mcp_tools", "docker_manager", "outputs_broker",
    "context_vars", "security", "system_prompt", "skill_manager", "uploads", "file_response",
    "descriptor_io", "docs_html", "sandbox_dns", "cli_runtime", "cli_adapters", "office",
    "claude_gateway",
}
_SERVER_PREFIXES = ("mcp_resources", "office.", "cli_adapters.")


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
    return isolated_modules(_SERVER_MODULES, _SERVER_PREFIXES)


def _server_local_modules():
    local = {}
    for name, module in sys.modules.copy().items():
        file = getattr(module, "__file__", None)
        if not isinstance(file, str):
            continue  # builtins and namespace packages such as `tests` have no file
        path = Path(file).resolve()
        if path.is_relative_to(SERVER_DIR) and not path.is_relative_to(DRAWIO_DIR):
            local[name] = module
    return local


def _server_local_files():
    names = set()
    for path in SERVER_DIR.rglob("*.py"):
        if path.is_relative_to(DRAWIO_DIR) or "__pycache__" in path.parts:
            continue
        parts = path.relative_to(SERVER_DIR).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        names.add(".".join(parts))
    return names
