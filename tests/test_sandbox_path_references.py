# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Tracked-file guard against retired sandbox path strings.

Membership is the Git index at the repository root resolved from this
file (`git ls-files -z`). Content is working-tree bytes of every
indexed path, with no extension filter. A tracked symlink is scanned
as `os.readlink` bytes, not the target. Git enumeration and unreadable
or missing tracked paths fail this test; untracked files are not
scanned.

ALLOWED is the exact five-file exception map: this module's needle
constants, recovery's legacy-bind non-attribution fixture, the
workspace-lifecycle absent-path and failed-write assertions, and the
two prompt tests that assert those strings are absent.

Run:
    uv run --no-project --with pytest --with-requirements computer-use-server/requirements.txt -- python -m pytest tests/test_sandbox_path_references.py -q --import-mode=importlib
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NEEDLES = (b"/mnt/user-data/uploads", b"/mnt/user-data/outputs")
ALLOWED = {
    "tests/test_sandbox_path_references.py": "own module constants",
    "tests/deploy/test_recovery.py": "legacy bind non-attribution",
    "tests/integration/test_workspace_lifecycle.py": "absent paths and failed writes",
    "tests/orchestrator/test_sub_agent_dispatch.py": "absent prompt strings",
    "tests/orchestrator/test_system_prompt_endpoint.py": "absent prompt strings",
}


def _tracked_paths(root: Path) -> list[str]:
    listing = subprocess.check_output(["git", "ls-files", "-z"], cwd=root)
    return [os.fsdecode(raw) for raw in listing.split(b"\0") if raw]


def _bytes_of_tracked_path(root: Path, relpath: str) -> bytes:
    full = os.path.join(os.fspath(root), relpath)
    try:
        mode = os.lstat(full).st_mode
    except OSError as exc:
        pytest.fail(f"tracked path missing or unreadable: {relpath}: {exc}")
    if stat.S_ISLNK(mode):
        try:
            return os.readlink(os.fsencode(full))
        except OSError as exc:
            pytest.fail(f"tracked symlink unreadable: {relpath}: {exc}")
    if not stat.S_ISREG(mode):
        pytest.fail(f"tracked path is not a regular file or symlink: {relpath}")
    try:
        with open(full, "rb") as handle:
            return handle.read()
    except OSError as exc:
        pytest.fail(f"tracked file unreadable: {relpath}: {exc}")


def test_tracked_files_do_not_name_legacy_sandbox_paths() -> None:
    offenders: list[str] = []
    for relpath in _tracked_paths(ROOT):
        payload = _bytes_of_tracked_path(ROOT, relpath)
        if any(needle in payload for needle in NEEDLES) and relpath not in ALLOWED:
            offenders.append(relpath)
    if offenders:
        pytest.fail("\n".join(sorted(offenders)))
