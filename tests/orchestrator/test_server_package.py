# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Inventory: every top-level Python module and package is an explicit COPY source."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = ROOT / "computer-use-server"
DOCKERFILE = SERVER_DIR / "Dockerfile"
_COPY = re.compile(r"^COPY\s+(\S+)\s+\S+\s*$")


def _copy_sources(text: str) -> set[str]:
    sources: set[str] = set()
    for raw in text.splitlines():
        match = _COPY.match(raw.strip())
        if match is None:
            continue
        source = match.group(1)
        if source.startswith("./"):
            source = source[2:]
        sources.add(source.rstrip("/"))
    return sources


def _shipped_python(server_dir: Path) -> set[str]:
    required: set[str] = set()
    for child in server_dir.iterdir():
        if child.name.startswith("."):
            continue
        if child.is_file() and child.suffix == ".py":
            required.add(child.name)
        elif child.is_dir() and (child / "__init__.py").is_file():
            required.add(child.name)
    return required


def _assert_copy_inventory(dockerfile_text: str, server_dir: Path) -> None:
    sources = _copy_sources(dockerfile_text)
    missing = sorted(_shipped_python(server_dir) - sources)
    assert missing == [], f"Dockerfile COPY omits {missing}; sources={sorted(sources)}"


def test_dockerfile_copies_every_top_level_python_module_and_package():
    _assert_copy_inventory(DOCKERFILE.read_text(encoding="utf-8"), SERVER_DIR)


def test_omitting_office_copy_fails_when_office_is_a_package():
    required = _shipped_python(SERVER_DIR)
    assert "office" in required
    stripped = "\n".join(
        line
        for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()
        if _COPY.match(line.strip()) is None or _copy_sources(line) != {"office"}
    )
    with pytest.raises(AssertionError, match="office"):
        _assert_copy_inventory(stripped, SERVER_DIR)
