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
                os.stat(name, dir_fd=dir_fd, follow_symlinks=True)
                if dir_fd is not None
                else os.stat(name)
            )
        except (OSError, TypeError, ValueError):
            info = None
        if info is not None and (info.st_dev, info.st_ino) in identities:
            # The oracle must escape production's per-chat Exception isolation.
            pytest.fail(f"external target opened: {name}")
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


def test_workspace_mutating_during_read_refuses_unstable_bytes(world, monkeypatch):
    store_mod, _docker_manager, data = world
    import office.workspace as workspace_mod
    target = _outputs(data) / "live.docx"
    target.parent.mkdir(parents=True)
    target.write_bytes(WORKSPACE)
    identity = target.stat()
    original_read = os.read
    changed = []

    def mutate_read(fd, size):
        chunk = original_read(fd, size)
        info = os.fstat(fd)
        if not changed and (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
            target.write_bytes(WORKSPACE + b"-changed-during-read")
            changed.append(True)
        return chunk

    monkeypatch.setattr(workspace_mod.os, "read", mutate_read)
    with pytest.raises(workspace_mod.UnsafePathError, match="changed while reading") as error:
        store_mod.OfficeStore().read_workspace_file(CHAT, "live.docx")
    assert error.value.reason == "unsafe_path"
    assert changed == [True]
    assert not _versions_dir(data).exists()
    assert not _staging(data).exists()


def test_missing_workspace_file_preserves_file_not_found_cause(world):
    store_mod, _docker_manager, data = world
    import office.workspace as workspace_mod
    _outputs(data).mkdir(parents=True)
    with pytest.raises(workspace_mod.UnsafePathError) as error:
        store_mod.OfficeStore().read_workspace_file(CHAT, "missing.docx")
    assert error.value.reason == "unsafe_path"
    assert isinstance(error.value.__cause__, FileNotFoundError)
    assert not _versions_dir(data).exists()
    assert not _staging(data).exists()


@pytest.mark.parametrize("tier", ["outputs", "chat"])
def test_outputs_and_chat_root_symlinks_refuse_external_bytes_and_lock(world, monkeypatch, tier):
    store_mod, _docker_manager, data = world
    import office.workspace as workspace_mod
    outside = data.parent / "external-root"
    external_file = outside / "secret.docx"
    external_file.parent.mkdir()
    external_file.write_bytes(b"EXTERNAL-ROOT-BYTES")
    lock = outside / ".lifecycle.lock"
    lock.write_bytes(b"EXTERNAL-LOCK")
    before_lock = lock.stat()
    if tier == "outputs":
        (data / CHAT).mkdir(parents=True)
        os.symlink(outside, _outputs(data))
        expected = workspace_mod.UnsafePathError
    else:
        data.mkdir()
        os.symlink(outside, data / CHAT)
        expected = store_mod.StateCorruptError
    _forbid_inode_open(workspace_mod, monkeypatch, outside, external_file, lock)
    with pytest.raises(expected):
        store_mod.OfficeStore().read_workspace_file(CHAT, "secret.docx")
    assert external_file.read_bytes() == b"EXTERNAL-ROOT-BYTES"
    assert lock.read_bytes() == b"EXTERNAL-LOCK"
    after_lock = lock.stat()
    assert (after_lock.st_dev, after_lock.st_ino) == (before_lock.st_dev, before_lock.st_ino)
    assert {path.name for path in outside.iterdir()} == {"secret.docx", ".lifecycle.lock"}
    assert not _versions_dir(data).exists()
    assert not _staging(data).exists()


def test_exact_limit_read_returns_bytes_and_oversize_refuses_before_content(world, monkeypatch):
    store_mod, _docker_manager, data = world
    import office.workspace as workspace_mod
    target = _outputs(data) / "brief.docx"
    target.parent.mkdir(parents=True)
    target.write_bytes(WORKSPACE)
    body, digest = store_mod.OfficeStore().read_workspace_file(
        CHAT, "brief.docx", max_bytes=len(WORKSPACE)
    )
    assert body == WORKSPACE
    assert digest == _sha(WORKSPACE)
    reads = {"count": 0}
    original_read = workspace_mod.os.read

    def count_read(fd, size):
        reads["count"] += 1
        return original_read(fd, size)

    monkeypatch.setattr(workspace_mod.os, "read", count_read)
    with pytest.raises(workspace_mod.FileTooLargeError) as error:
        store_mod.OfficeStore().read_workspace_file(
            CHAT, "brief.docx", max_bytes=len(WORKSPACE) - 1
        )
    assert error.value.reason == "file_too_large"
    assert reads["count"] == 0
    unlimited, unlimited_digest = store_mod.OfficeStore().read_workspace_file(CHAT, "brief.docx")
    assert unlimited == WORKSPACE
    assert unlimited_digest == _sha(WORKSPACE)
    empty = _outputs(data) / "empty.docx"
    empty.write_bytes(b"")
    empty_body, empty_digest = store_mod.OfficeStore().read_workspace_file(
        CHAT, "empty.docx", max_bytes=0
    )
    assert empty_body == b""
    assert empty_digest == _sha(b"")


def test_growth_past_limit_during_read_refuses_after_one_excess_byte(world, monkeypatch):
    store_mod, _docker_manager, data = world
    import office.workspace as workspace_mod
    target = _outputs(data) / "live.docx"
    target.parent.mkdir(parents=True)
    target.write_bytes(WORKSPACE)
    identity = target.stat()
    original_fstat = workspace_mod.os.fstat
    original_read = workspace_mod.os.read
    grew = []
    returned = []

    def lie_then_grow(fd):
        info = original_fstat(fd)
        if (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino) and not grew:
            class Small:
                st_mode = info.st_mode
                st_size = len(WORKSPACE)
                st_mtime_ns = info.st_mtime_ns
                st_ino = info.st_ino
                st_dev = info.st_dev
            return Small()
        return info

    def grow_on_read(fd, size):
        info = original_fstat(fd)
        if (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino) and not grew:
            target.write_bytes(WORKSPACE + b"X" * 64)
            grew.append(True)
        chunk = original_read(fd, size)
        if (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
            returned.append(len(chunk))
        return chunk

    monkeypatch.setattr(workspace_mod.os, "fstat", lie_then_grow)
    monkeypatch.setattr(workspace_mod.os, "read", grow_on_read)
    with pytest.raises(workspace_mod.FileTooLargeError) as error:
        store_mod.OfficeStore().read_workspace_file(
            CHAT, "live.docx", max_bytes=len(WORKSPACE)
        )
    assert error.value.reason == "file_too_large"
    assert grew == [True]
    assert sum(returned) <= len(WORKSPACE) + 1
    assert not _versions_dir(data).exists()


def test_unsafe_paths_are_not_read_when_a_byte_limit_is_supplied(world, monkeypatch):
    store_mod, _docker_manager, data = world
    import office.workspace as workspace_mod
    outside = data.parent / "external-secret.bin"
    outside.write_bytes(b"EXTERNAL-TARGET-BYTES")
    workspace = _outputs(data)
    workspace.mkdir(parents=True)
    os.symlink(outside, workspace / "link.docx")
    _forbid_inode_open(workspace_mod, monkeypatch, outside)
    with pytest.raises(workspace_mod.UnsafePathError) as error:
        store_mod.OfficeStore().read_workspace_file(CHAT, "link.docx", max_bytes=8)
    assert error.value.reason == "unsafe_path"
    assert outside.read_bytes() == b"EXTERNAL-TARGET-BYTES"



def test_boolean_max_bytes_is_rejected_before_opening_the_file(world):
    store_mod, _docker_manager, data = world
    target = _outputs(data) / "brief.docx"
    target.parent.mkdir(parents=True)
    target.write_bytes(WORKSPACE)
    with pytest.raises(ValueError, match="max_bytes"):
        store_mod.OfficeStore().read_workspace_file(CHAT, "brief.docx", max_bytes=True)

