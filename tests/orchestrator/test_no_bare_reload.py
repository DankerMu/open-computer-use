# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""#730: no orchestrator test calls importlib.reload.

importlib.reload re-runs a server module on the cached object, so every module
that imported a class from it before keeps the old class (the LifecycleError
split behind #730). Tests that need a fresh import do it inside
`isolated_modules` instead. Calls are resolved through the file's import
bindings, never by argument, and there is no allowlist.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCANNED = sorted((ROOT / "tests" / "orchestrator").rglob("*.py")) + [
    ROOT / "tests" / "file_route_module_ownership.py",
]


def bare_reloads(source: str) -> list[int]:
    """Line numbers of calls that resolve to importlib.reload through imports."""
    tree = ast.parse(source)
    modules, functions = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname is None and alias.name.split(".")[0] == "importlib":
                    modules.add("importlib")  # `import importlib.util` binds importlib
                elif alias.name == "importlib":
                    modules.add(alias.asname)
        elif isinstance(node, ast.ImportFrom) and node.module == "importlib" and node.level == 0:
            functions.update(alias.asname or alias.name for alias in node.names if alias.name == "reload")
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == "reload"
                and isinstance(func.value, ast.Name) and func.value.id in modules):
            lines.append(node.lineno)
        elif isinstance(func, ast.Name) and func.id in functions:
            lines.append(node.lineno)
    return sorted(lines)


def test_plain_and_aliased_reloads_are_flagged():
    assert bare_reloads("import importlib\nimportlib.reload(docker_manager)\n") == [2]
    assert bare_reloads("import importlib as il\nfor m in mods:\n    il.reload(m)\n") == [3]
    assert bare_reloads("import importlib.util\nimportlib.reload(sys.modules[name])\n") == [2]
    assert bare_reloads("from importlib import reload as r\nr(mcp_tools)\n") == [2]
    assert bare_reloads("def f():\n    from importlib import reload\n    reload(store_mod)\n") == [3]


def test_other_reload_names_are_not_flagged():
    assert bare_reloads("import importlib\ncontainer.reload()\nfixture.container.reload()\n") == []
    assert bare_reloads("import importlib\ndef reload():\n    pass\nreload()\n") == []
    assert bare_reloads("import importlib as il\nimportlib.reload(x)\n") == []


def test_no_orchestrator_test_calls_importlib_reload():
    found = [
        f"{path.relative_to(ROOT)}:{line}"
        for path in SCANNED
        for line in bare_reloads(path.read_text(encoding="utf-8"))
    ]
    assert found == [], "importlib.reload leaks module generations; use isolated_modules:\n" + "\n".join(found)
