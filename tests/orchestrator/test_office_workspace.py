# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Public-seam tests for the Office no-follow workspace read."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from tests.orchestrator._office_store import CHAT, _outputs, _state

WORKSPACE = b"workspace-v1"
SAVE = b"save-v3"


def _sha(body: bytes) -> str:
    import hashlib
    return hashlib.sha256(body).hexdigest()


def _office(data: Path, chat: str = CHAT) -> Path:
    return _state(data, chat).parent


def _versions_dir(data: Path, chat: str = CHAT) -> Path:
    return _office(data, chat) / "versions"


def _staging(data: Path, chat: str = CHAT) -> Path:
    return _office(data, chat) / "staging"


def _office_names(data: Path) -> set[str]:
    office = _office(data)
    if not office.exists():
        return set()
    return {path.name for path in office.rglob("*") if path.is_file()}


def _forbid_inode_open(module, monkeypatch, *paths: Path) -> None:
    identities = {(path.lstat().st_dev, path.lstat().st_ino) for path in paths}
    original = module.os.open

    def guarded(name, flags, *args, **kwargs):
        dir_fd = kwargs.get("dir_fd")
        try:
            info = (
                os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                if dir_fd is not None
                else os.lstat(name)
            )
        except (OSError, TypeError, ValueError):
            info = None
        if info is not None and (info.st_dev, info.st_ino) in identities:
            raise AssertionError(("external target opened", name))
        return original(name, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", guarded)


def test_safe_workspace_read_hashes_root_and_nested_regular_files(world):
    store_mod, _docker_manager, data = world
    root = _outputs(data) / "brief.docx"
    nested = _outputs(data) / "reports" / "q1.docx"
    nested.parent.mkdir(parents=True)
    root.write_bytes(WORKSPACE)
    nested.write_bytes(SAVE)
    body, digest = store_mod.OfficeStore().read_workspace_file(CHAT, "brief.docx")
    assert body == WORKSPACE
    assert digest == _sha(WORKSPACE)
    nested_body, nested_digest = store_mod.OfficeStore().read_workspace_file(CHAT, "reports/q1.docx")
    assert nested_body == SAVE
    assert nested_digest == _sha(SAVE)
    assert not _versions_dir(data).exists()
    assert not _staging(data).exists()


def test_leaf_parent_symlink_traversal_and_nonregular_reads_refuse_before_external_bytes(world, monkeypatch):
    store_mod, _docker_manager, data = world
    outside = data.parent / "external-secret.bin"
    outside.write_bytes(b"EXTERNAL-TARGET-BYTES")
    workspace = _outputs(data)
    workspace.mkdir(parents=True)
    os.symlink(outside, workspace / "link.docx")
    linked = workspace / "linked"
    os.symlink(outside.parent, linked)
    os.mkfifo(workspace / "pipe.docx")
    (workspace / "folder").mkdir()
    store = store_mod.OfficeStore()
    import office.workspace as workspace_mod
    _forbid_inode_open(workspace_mod, monkeypatch, outside)
    for relative in (
        "link.docx",
        "linked/external-secret.bin",
        "../external-secret.bin",
        "pipe.docx",
        "folder",
        "/tmp/x.docx",
        "reports/../brief.docx",
        ".hidden.docx",
    ):
        with pytest.raises(workspace_mod.UnsafePathError) as error:
            store.read_workspace_file(CHAT, relative)
        assert error.value.reason == "unsafe_path"
    assert outside.read_bytes() == b"EXTERNAL-TARGET-BYTES"
    assert not _versions_dir(data).exists()
    assert not _staging(data).exists()
    names = _office_names(data)
    assert all("external-secret" not in name for name in names)
