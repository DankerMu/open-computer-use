# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Public-seam tests for Office version blobs, receipts and the free-space floor."""
from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path

import pytest

from tests.orchestrator._office_store import (
    CHAT,
    _inode,
    _outputs,
    _run_child,
    _start_child,
    _state,
    _stop_child,
    _wait_marker,
)
from tests.orchestrator.test_office_workspace import _forbid_inode_open

FILE_A = "11111111-1111-4111-8111-111111111111"
FILE_B = "22222222-2222-4222-8222-222222222222"
SESSION = "sess-one"
WORKSPACE = b"workspace-v1"
AUTOSAVE = b"autosave-v2"
SAVE = b"save-v3"
SHARED = b"shared-bytes"
V4 = b"restore-v4"
V5 = b"save-v5"


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _office(data: Path, chat: str = CHAT) -> Path:
    return _state(data, chat).parent


def _versions_dir(data: Path, chat: str = CHAT) -> Path:
    return _office(data, chat) / "versions"


def _blob(data: Path, body: bytes, chat: str = CHAT) -> Path:
    return _versions_dir(data, chat) / _sha(body)


def _staging(data: Path, chat: str = CHAT) -> Path:
    return _office(data, chat) / "staging"


def _receipt(session: str, seq: int, status: int, digest: str | None, version: int | None, answer: dict) -> dict:
    return {
        "session_id": session,
        "save_seq": seq,
        "status": status,
        "sha256": digest,
        "version": version,
        "answer": answer,
    }


def _store(store_mod, body: bytes, *, file_id: str = FILE_A, source: str, parent, published: bool, receipt=None, floor: int = 0):
    return store_mod.OfficeStore().store_version(
        CHAT,
        file_id,
        body,
        source=source,
        parent=parent,
        published=published,
        min_free_bytes=floor,
        receipt=receipt,
    )


def _doc_versions(store_mod, file_id: str = FILE_A) -> list:
    return store_mod.OfficeStore().read(CHAT)["documents"][file_id]["versions"]


def _pin_floor_fd(versions_mod, monkeypatch, available):
    seen = []

    class Info:
        def __init__(self, size):
            self.f_bavail = size
            self.f_frsize = 1

    def spy(fd):
        info = os.fstat(fd)
        seen.append((info.st_dev, info.st_ino))
        return Info(available)

    monkeypatch.setattr(versions_mod.os, "fstatvfs", spy)
    return seen


def _overlap_store(data, *, holder_file, waiter_file, text, holder_receipt, waiter_receipt, token, holder_parent=None, waiter_parent=None, waiter_text=None, waiter_source="conflict"):
    hold = data.parent / f"office-ident-hold-{token}"
    entered = data.parent / f"office-ident-entered-{token}"
    contended = data.parent / f"office-ident-contended-{token}"
    os.mkfifo(hold)
    holder = waiter = None
    holder_env = {} if holder_parent is None else {"OCU_PARENT": json.dumps(holder_parent)}
    waiter_env = {} if waiter_parent is None else {"OCU_PARENT": json.dumps(waiter_parent)}
    try:
        holder = _start_child(
            data,
            OCU_CHILD_OP="hold-then-store",
            OCU_ENTERED=str(entered),
            OCU_HOLD=str(hold),
            OCU_FILE_ID=holder_file,
            OCU_CONTENT=text,
            OCU_SOURCE="workspace" if holder_parent is None else "save",
            OCU_PUBLISHED="1",
            OCU_FLOOR="0",
            OCU_RECEIPT=json.dumps(holder_receipt),
            **holder_env,
        )
        _wait_marker(entered, holder, "holder did not enter the locked mutator")
        waiter = _start_child(
            data,
            OCU_CHILD_OP="wait-then-store",
            OCU_CONTENDED=str(contended),
            OCU_LOCK_DENIAL="version store lock was granted without contention",
            OCU_FILE_ID=waiter_file,
            OCU_CONTENT=text if waiter_text is None else waiter_text,
            OCU_SOURCE=waiter_source,
            OCU_FLOOR="0",
            OCU_RECEIPT=json.dumps(waiter_receipt),
            **waiter_env,
        )
        _wait_marker(contended, waiter, "waiter did not observe LOCK_NB denial")
        assert holder.poll() is None
        assert waiter.poll() is None
        os.close(os.open(hold, os.O_WRONLY))
        holder_out, holder_err = holder.communicate(timeout=10)
        waiter_out, waiter_err = waiter.communicate(timeout=10)
        assert holder.returncode == 0, (holder_out, holder_err)
        assert waiter.returncode == 0, (waiter_out, waiter_err)
        return (
            json.loads(holder_out.strip().splitlines()[-1]),
            json.loads(waiter_out.strip().splitlines()[-1]),
        )
    finally:
        _stop_child(holder)
        _stop_child(waiter)


def test_identical_content_shares_one_blob_across_documents_and_nonconsecutive_repeats(world):
    store_mod, _docker_manager, data = world
    first = _store(store_mod, SHARED, source="workspace", parent=None, published=True)
    other = _store(store_mod, SHARED, file_id=FILE_B, source="conflict", parent=None, published=True)
    later = _store(store_mod, AUTOSAVE, source="autosave", parent=1, published=False)
    repeated = _store(store_mod, SHARED, source="restore", parent=2, published=False)
    assert first["number"] == 1
    assert other["number"] == 1
    assert later["number"] == 2
    assert repeated["number"] == 3
    assert first["sha256"] == other["sha256"] == repeated["sha256"] == _sha(SHARED)
    blobs = list(_versions_dir(data).iterdir())
    assert {path.name for path in blobs} == {_sha(SHARED), _sha(AUTOSAVE)}
    assert _blob(data, SHARED).read_bytes() == SHARED
    assert _blob(data, AUTOSAVE).read_bytes() == AUTOSAVE
    assert [record["number"] for record in _doc_versions(store_mod)] == [1, 2, 3]
    assert [record["number"] for record in _doc_versions(store_mod, FILE_B)] == [1]


def test_workspace_autosave_save_numbers_are_one_two_three_and_later_stores_reuse_none(world):
    store_mod, _docker_manager, data = world
    one = _store(store_mod, WORKSPACE, source="workspace", parent=None, published=True)
    two = _store(store_mod, AUTOSAVE, source="autosave", parent=1, published=False)
    three = _store(store_mod, SAVE, source="save", parent=2, published=True)
    later = _store(store_mod, V4, source="close", parent=3, published=False)
    assert [one["number"], two["number"], three["number"], later["number"]] == [1, 2, 3, 4]
    records = _doc_versions(store_mod)
    assert [record["source"] for record in records] == ["workspace", "autosave", "save", "close"]
    assert [record["number"] for record in records] == [1, 2, 3, 4]
    assert records[0]["parent"] is None
    assert [record["parent"] for record in records[1:]] == [1, 2, 3]
    assert records[0]["created_at"] == one["created_at"]
    assert records[1]["published"] is False
    assert records[2]["published"] is True


def test_equal_latest_hash_adds_no_record_and_receipt_names_existing_version(world):
    store_mod, _docker_manager, data = world
    _store(store_mod, WORKSPACE, source="workspace", parent=None, published=True)
    _store(store_mod, AUTOSAVE, source="autosave", parent=1, published=False)
    third = _store(store_mod, SAVE, source="save", parent=2, published=True)
    before = _state(data).read_bytes()
    blob_before = _blob(data, SAVE).read_bytes()
    receipt = _receipt(SESSION, 9, 6, _sha(SAVE), None, {"error": 0})
    reused = _store(store_mod, SAVE, source="autosave", parent=3, published=False, receipt=receipt)
    assert reused["number"] == 3
    assert reused["source"] == "save"
    assert reused["published"] is True
    assert reused["created_at"] == third["created_at"]
    assert reused["parent"] == 2
    persisted = store_mod.OfficeStore().read(CHAT)
    assert [record["number"] for record in persisted["documents"][FILE_A]["versions"]] == [1, 2, 3]
    stored = persisted["receipts"][SESSION]["9"]
    assert stored["status"] == 6
    assert stored["sha256"] == _sha(SAVE)
    assert stored["version"] == 3
    assert stored["answer"] == {"error": 0}
    assert _blob(data, SAVE).read_bytes() == blob_before
    unpublished = _store(store_mod, AUTOSAVE, source="close", parent=3, published=False)
    assert unpublished["number"] == 4
    assert unpublished["published"] is False
    flipped = _store(store_mod, AUTOSAVE, source="save", parent=4, published=True)
    assert flipped["number"] == 4
    assert flipped["source"] == "close"
    assert flipped["published"] is True
    assert [record["number"] for record in _doc_versions(store_mod)] == [1, 2, 3, 4]
    assert _doc_versions(store_mod)[3]["published"] is True
    assert _state(data).read_bytes() != before


def test_existing_version_bytes_stay_and_published_flag_is_one_way(world):
    store_mod, _docker_manager, data = world
    first = _store(store_mod, WORKSPACE, source="workspace", parent=None, published=True)
    second = _store(store_mod, AUTOSAVE, source="autosave", parent=1, published=False)
    first_blob = _blob(data, WORKSPACE).read_bytes()
    second_blob = _blob(data, AUTOSAVE).read_bytes()
    first_inode = _inode(_blob(data, WORKSPACE))
    marked = store_mod.OfficeStore().mark_published(CHAT, FILE_A, 2)
    assert marked["published"] is True
    assert marked["number"] == 2
    records = _doc_versions(store_mod)
    assert records[0] == first
    assert records[1]["published"] is True
    assert records[1]["sha256"] == second["sha256"]
    assert records[1]["source"] == "autosave"
    assert _blob(data, WORKSPACE).read_bytes() == first_blob == WORKSPACE
    assert _blob(data, AUTOSAVE).read_bytes() == second_blob == AUTOSAVE
    assert hashlib.sha256(_blob(data, WORKSPACE).read_bytes()).hexdigest() == _blob(data, WORKSPACE).name
    assert _inode(_blob(data, WORKSPACE)) == first_inode
    with pytest.raises(TypeError):
        store_mod.OfficeStore().mark_published(CHAT, FILE_A, 2, False)  # type: ignore[misc]
    assert _doc_versions(store_mod)[1]["published"] is True


def test_status_six_version_five_and_seq_three_share_one_state_update(world):
    store_mod, _docker_manager, data = world
    _store(store_mod, WORKSPACE, source="workspace", parent=None, published=True)
    _store(store_mod, AUTOSAVE, source="autosave", parent=1, published=False)
    _store(store_mod, SAVE, source="save", parent=2, published=True)
    _store(store_mod, V4, source="restore", parent=3, published=False)
    answer = {"error": 0, "seq": 3}
    receipt = _receipt(SESSION, 3, 6, None, None, answer)
    fifth = _store(store_mod, V5, source="save", parent=4, published=True, receipt=receipt)
    assert fifth["number"] == 5
    persisted = json.loads(_state(data).read_text(encoding="utf-8"))
    stored = persisted["receipts"][SESSION]["3"]
    assert stored == {
        "status": 6,
        "sha256": _sha(V5),
        "version": 5,
        "answer": answer,
    }
    assert persisted["documents"][FILE_A]["versions"][4]["number"] == 5
    assert _blob(data, V5).read_bytes() == V5
    found = _run_child(
        data,
        OCU_CHILD_OP="get-receipt",
        OCU_SESSION=SESSION,
        OCU_SEQ="3",
        OCU_EXPECTED_HASH=_sha(V5),
    )
    assert found == stored
    mismatched = _run_child(
        data,
        OCU_CHILD_OP="get-receipt",
        OCU_SESSION=SESSION,
        OCU_SEQ="3",
        OCU_EXPECTED_HASH=_sha(SAVE),
    )
    assert mismatched is None


def test_status_seven_receipt_has_no_version_and_fresh_process_lookup_returns_answer(world):
    store_mod, _docker_manager, data = world
    answer = {"error": 0, "reason": "forcesave_failed"}
    stored = store_mod.OfficeStore().record_receipt(
        CHAT, SESSION, 2, {"status": 7, "sha256": None, "version": None, "answer": answer}
    )
    assert stored == {"status": 7, "sha256": None, "version": None, "answer": answer}
    assert store_mod.OfficeStore().read(CHAT)["documents"] == {}
    found = _run_child(
        data,
        OCU_CHILD_OP="get-receipt",
        OCU_SESSION=SESSION,
        OCU_SEQ="2",
    )
    assert found == stored
    hashed = _run_child(
        data,
        OCU_CHILD_OP="get-receipt",
        OCU_SESSION=SESSION,
        OCU_SEQ="2",
        OCU_EXPECTED_HASH=_sha(SAVE),
    )
    assert hashed is None
    matched = store_mod.OfficeStore().get_receipt(CHAT, SESSION, 2)
    assert matched["answer"] == answer
    assert matched["version"] is None


def test_standalone_floor_rejects_below_and_admits_equal_without_storing(world, monkeypatch):
    store_mod, _docker_manager, data = world
    data.mkdir(exist_ok=True)
    import office.versions as versions_mod
    seen = _pin_floor_fd(versions_mod, monkeypatch, 40)
    store = store_mod.OfficeStore()
    with pytest.raises(versions_mod.StorageLowError) as below:
        store.check_free_space(CHAT, 41)
    assert below.value.reason == "storage_low"
    chat_fd = seen[-1]
    assert chat_fd == _inode(data / CHAT)
    store.check_free_space(CHAT, 40)
    store.check_free_space(CHAT, 0)
    assert seen[-1] == chat_fd
    assert not _state(data).exists()
    assert not _versions_dir(data).exists()
    with pytest.raises(versions_mod.StorageLowError) as refused:
        _store(store_mod, SAVE, source="workspace", parent=None, published=True, floor=41)
    assert refused.value.reason == "storage_low"
    assert seen[-1] == chat_fd
    assert not _state(data).exists()
    assert not _blob(data, SAVE).exists()
    assert not any(path.is_file() for path in _office(data).rglob("*")) if _office(data).exists() else True


def test_store_below_floor_adds_no_blob_record_or_receipt(world, monkeypatch):
    store_mod, _docker_manager, data = world
    _store(store_mod, WORKSPACE, source="workspace", parent=None, published=True)
    before_state = _state(data).read_bytes()
    before_blobs = {path.name: path.read_bytes() for path in _versions_dir(data).iterdir()}
    import office.versions as versions_mod
    seen = _pin_floor_fd(versions_mod, monkeypatch, 1)
    office_fd = _inode(_office(data))
    receipt = _receipt(SESSION, 8, 6, None, None, {"error": 0})
    with pytest.raises(versions_mod.StorageLowError) as standalone:
        store_mod.OfficeStore().check_free_space(CHAT, 2)
    assert standalone.value.reason == "storage_low"
    assert seen[-1] == office_fd
    with pytest.raises(versions_mod.StorageLowError) as error:
        _store(store_mod, SAVE, source="save", parent=1, published=True, receipt=receipt, floor=2)
    assert error.value.reason == "storage_low"
    assert seen[-1] == office_fd
    assert _state(data).read_bytes() == before_state
    assert {path.name: path.read_bytes() for path in _versions_dir(data).iterdir()} == before_blobs
    assert not _blob(data, SAVE).exists()
    assert SESSION not in store_mod.OfficeStore().read(CHAT)["receipts"]
    assert not list(_staging(data).glob("*")) if _staging(data).exists() else True


def test_enospc_mid_blob_cleans_owned_temp_and_leaves_no_receipt(world, monkeypatch):
    store_mod, _docker_manager, data = world
    _store(store_mod, WORKSPACE, source="workspace", parent=None, published=True)
    before_state = _state(data).read_bytes()
    shared = _blob(data, WORKSPACE)
    shared_bytes = shared.read_bytes()
    import office.versions as versions_mod
    original_write = versions_mod.os.write

    def fail_blob_write(fd, payload):
        if payload[:1] != b"{":
            raise OSError(errno.ENOSPC, "injected blob write fault")
        return original_write(fd, payload)

    monkeypatch.setattr(versions_mod.os, "write", fail_blob_write)
    receipt = _receipt(SESSION, 4, 6, None, None, {"error": 0})
    with pytest.raises(versions_mod.StorageLowError) as error:
        _store(store_mod, SAVE, source="save", parent=1, published=True, receipt=receipt)
    assert error.value.reason == "storage_low"
    assert _state(data).read_bytes() == before_state
    assert shared.read_bytes() == shared_bytes
    assert not _blob(data, SAVE).exists()
    leftovers = [path for path in _office(data).rglob("*") if path.is_file() and path.suffix == ".tmp"]
    assert leftovers == []
    assert store_mod.OfficeStore().read(CHAT)["receipts"] == {}


def test_enospc_before_state_commit_cleans_new_blob_and_preserves_shared_blob(world, monkeypatch):
    store_mod, _docker_manager, data = world
    import office.versions as versions_mod
    _store(store_mod, WORKSPACE, source="workspace", parent=None, published=True)
    shared = _blob(data, WORKSPACE)
    shared_bytes = shared.read_bytes()
    before_state = _state(data).read_bytes()
    original_write = store_mod.os.write

    def fail_state_write(fd, payload):
        if payload[:1] == b"{":
            raise OSError(errno.ENOSPC, "injected state write fault")
        return original_write(fd, payload)

    monkeypatch.setattr(store_mod.os, "write", fail_state_write)
    receipt = _receipt(SESSION, 5, 6, None, None, {"error": 0})
    with pytest.raises(versions_mod.StorageLowError):
        _store(store_mod, SAVE, source="save", parent=1, published=True, receipt=receipt)
    assert _state(data).read_bytes() == before_state
    assert shared.read_bytes() == shared_bytes
    assert not _blob(data, SAVE).exists()
    assert store_mod.OfficeStore().read(CHAT)["receipts"] == {}

    monkeypatch.setattr(store_mod.os, "write", fail_state_write)
    with pytest.raises(versions_mod.StorageLowError):
        _store(store_mod, WORKSPACE, file_id=FILE_B, source="conflict", parent=None, published=True, receipt=receipt)
    assert shared.read_bytes() == shared_bytes
    assert _inode(shared) == _inode(_blob(data, WORKSPACE))
    assert FILE_B not in store_mod.OfficeStore().read(CHAT)["documents"]
    assert store_mod.OfficeStore().read(CHAT)["receipts"] == {}


def test_corrupt_version_or_receipt_records_fail_closed_without_partial_blobs(world):
    store_mod, _docker_manager, data = world
    _store(store_mod, WORKSPACE, source="workspace", parent=None, published=True)
    payload = json.loads(_state(data).read_text(encoding="utf-8"))
    payload["documents"][FILE_A]["versions"][0]["source"] = "unknown"
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    _state(data).write_bytes(encoded)
    before_blobs = {path.name: path.read_bytes() for path in _versions_dir(data).iterdir()}
    with pytest.raises(store_mod.StateCorruptError):
        _store(store_mod, SAVE, source="save", parent=1, published=True)
    assert _state(data).read_bytes() == encoded
    assert {path.name: path.read_bytes() for path in _versions_dir(data).iterdir()} == before_blobs
    assert not _blob(data, SAVE).exists()

    payload["documents"][FILE_A]["versions"][0]["source"] = "workspace"
    payload["receipts"] = {SESSION: "broken"}
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    _state(data).write_bytes(encoded)
    with pytest.raises(store_mod.StateCorruptError):
        store_mod.OfficeStore().record_receipt(
            CHAT, SESSION, 1, {"status": 7, "sha256": None, "version": None, "answer": {"error": 0}}
        )
    assert _state(data).read_bytes() == encoded


def test_invalid_source_parent_and_receipt_keys_fail_before_content_publication(world, monkeypatch):
    store_mod, _docker_manager, data = world
    store = store_mod.OfficeStore()

    def forbid_publication(*args, **kwargs):
        raise AssertionError("invalid caller published content")

    with monkeypatch.context() as patches:
        patches.setattr(os, "link", forbid_publication)
        with pytest.raises(ValueError):
            store.store_version(CHAT, FILE_A, WORKSPACE, source="workspace", parent=1, published=True, min_free_bytes=0)
    assert not _blob(data, WORKSPACE).exists()
    _store(store_mod, WORKSPACE, source="workspace", parent=None, published=True)
    before_state = _state(data).read_bytes()
    before_blobs = {path.name: path.read_bytes() for path in _versions_dir(data).iterdir()}
    monkeypatch.setattr(os, "link", forbid_publication)
    with pytest.raises(ValueError):
        store.store_version(CHAT, FILE_A, SAVE, source="draft", parent=1, published=True, min_free_bytes=0)
    with pytest.raises(ValueError):
        store.store_version(CHAT, FILE_A, SAVE, source="save", parent=9, published=True, min_free_bytes=0)
    with pytest.raises(ValueError):
        store.store_version(CHAT, FILE_A, SAVE, source="save", parent=None, published=True, min_free_bytes=0)
    with pytest.raises(ValueError):
        store.store_version(
            CHAT,
            FILE_A,
            SAVE,
            source="save",
            parent=1,
            published=True,
            min_free_bytes=0,
            receipt={"session_id": SESSION, "save_seq": 1, "status": 6},
        )
    with pytest.raises(ValueError):
        store.record_receipt(CHAT, SESSION, 1, {"status": 7, "sha256": None, "version": None})
    assert _state(data).read_bytes() == before_state
    assert {path.name: path.read_bytes() for path in _versions_dir(data).iterdir()} == before_blobs
    assert not _blob(data, SAVE).exists()


def test_identical_receipt_replay_is_ok_and_conflicting_overwrite_fails(world):
    store_mod, _docker_manager, data = world
    answer = {"error": 0}
    first = store_mod.OfficeStore().record_receipt(
        CHAT, SESSION, 4, {"status": 7, "sha256": None, "version": None, "answer": answer}
    )
    again = store_mod.OfficeStore().record_receipt(
        CHAT, SESSION, 4, {"status": 7, "sha256": None, "version": None, "answer": answer}
    )
    assert again == first
    with pytest.raises(ValueError, match="conflicting"):
        store_mod.OfficeStore().record_receipt(
            CHAT, SESSION, 4, {"status": 6, "sha256": None, "version": None, "answer": answer}
        )
    stored = store_mod.OfficeStore().get_receipt(CHAT, SESSION, 4)
    assert stored["status"] == 7
    assert stored["answer"] == answer


def test_two_workers_store_contiguous_numbers_and_dedup_identical_content_under_lock(world):
    store_mod, _docker_manager, data = world
    first_state, second_state = _overlap_store(
        data, holder_file=FILE_A, waiter_file=FILE_A, text="alpha-bytes",
        waiter_text="beta-bytes", waiter_source="autosave", waiter_parent=1, token="distinct",
        holder_receipt=_receipt("sess-a", 1, 6, None, None, {"error": 0, "who": "a"}),
        waiter_receipt=_receipt("sess-b", 1, 6, None, None, {"error": 0, "who": "b"}),
    )
    assert first_state["record"]["number"] == 1
    assert "sess-b" not in first_state["state"]["receipts"]
    assert second_state["record"]["number"] == 2
    assert second_state["state"]["receipts"]["sess-a"]["1"]["answer"]["who"] == "a"
    assert second_state["state"]["receipts"]["sess-b"]["1"]["answer"]["who"] == "b"
    persisted = store_mod.OfficeStore().read(CHAT)
    numbers = [record["number"] for record in persisted["documents"][FILE_A]["versions"]]
    assert numbers == [1, 2]
    assert persisted["receipts"]["sess-a"]["1"]["version"] == 1
    assert persisted["receipts"]["sess-b"]["1"]["version"] == 2
    assert _blob(data, b"alpha-bytes").read_bytes() == b"alpha-bytes"
    assert _blob(data, b"beta-bytes").read_bytes() == b"beta-bytes"

    same = _run_child(
        data,
        OCU_CHILD_OP="store-version",
        OCU_FILE_ID=FILE_A,
        OCU_CONTENT="beta-bytes",
        OCU_SOURCE="save",
        OCU_PARENT="2",
        OCU_FLOOR="0",
        OCU_RECEIPT=json.dumps(_receipt("sess-c", 1, 6, None, None, {"error": 0, "who": "c"})),
    )
    assert same["record"]["number"] == 2
    assert same["record"]["source"] == "autosave"
    assert same["state"]["receipts"]["sess-c"]["1"]["version"] == 2
    assert len(list(_versions_dir(data).iterdir())) == 2


def test_post_replace_state_durability_error_retains_blob_and_visible_successor(world, monkeypatch):
    store_mod, _docker_manager, data = world
    _store(store_mod, WORKSPACE, source="workspace", parent=None, published=True)
    original_replace = store_mod.os.replace
    original_fsync = store_mod.os.fsync
    replaced = {"done": False}

    def watch_state_replace(src, dst, *args, **kwargs):
        result = original_replace(src, dst, *args, **kwargs)
        if dst == "state.json" or (isinstance(dst, str) and dst.endswith("state.json")):
            replaced["done"] = True
        return result

    def fail_after_state_replace(fd):
        if replaced["done"]:
            replaced["done"] = False
            raise OSError("controlled directory fsync failure")
        return original_fsync(fd)

    receipt = _receipt(SESSION, 6, 6, None, None, {"error": 0})
    with monkeypatch.context() as patches:
        patches.setattr(store_mod.os, "replace", watch_state_replace)
        patches.setattr(store_mod.os, "fsync", fail_after_state_replace)
        with pytest.raises(store_mod.StateDurabilityError, match="committed"):
            _store(store_mod, SAVE, source="save", parent=1, published=True, receipt=receipt)
    persisted = store_mod.OfficeStore().read(CHAT)
    assert persisted["documents"][FILE_A]["versions"][-1]["sha256"] == _sha(SAVE)
    assert persisted["receipts"][SESSION]["6"]["version"] == 2
    assert _blob(data, SAVE).read_bytes() == SAVE
    assert _blob(data, WORKSPACE).read_bytes() == WORKSPACE


def test_null_document_and_receipt_slots_preserve_state_and_blobs(world):
    store_mod, _docker_manager, data = world
    store = store_mod.OfficeStore()
    _store(store_mod, WORKSPACE, source="workspace", parent=None, published=True)
    valid = store.read(CHAT)
    before_blobs = {path.name: path.read_bytes() for path in _versions_dir(data).iterdir()}
    for collection, key in (("documents", FILE_A), ("receipts", SESSION)):
        payload = json.loads(json.dumps(valid))
        payload[collection][key] = None
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        _state(data).write_bytes(encoded)
        with pytest.raises(store_mod.StateCorruptError):
            _store(store_mod, SAVE, source="save", parent=1, published=True,
                   receipt=_receipt(SESSION, 1, 6, None, None, {"error": 0}))
        if collection == "receipts":
            with pytest.raises(store_mod.StateCorruptError):
                store.record_receipt(CHAT, SESSION, 1,
                                     {"status": 7, "sha256": None, "version": None, "answer": {"error": 0}})
            with pytest.raises(store_mod.StateCorruptError):
                store.get_receipt(CHAT, SESSION, 1)
        assert _state(data).read_bytes() == encoded
        assert {path.name: path.read_bytes() for path in _versions_dir(data).iterdir()} == before_blobs
    valid["documents"][FILE_B] = {"id": FILE_B}
    _state(data).write_text(json.dumps(valid), encoding="utf-8")
    created = _store(store_mod, SAVE, file_id=FILE_B, source="workspace", parent=None, published=True)
    assert created["number"] == 1
    assert store.read(CHAT)["documents"][FILE_B]["id"] == FILE_B


@pytest.mark.parametrize("waiter_file", [FILE_A, FILE_B])
def test_identical_content_concurrent_same_and_different_documents(world, waiter_file):
    store_mod, _docker_manager, data = world
    first, second = _overlap_store(
        data, holder_file=FILE_A, waiter_file=waiter_file, text="shared-bytes", token="identical",
        holder_receipt=_receipt("sess-holder", 1, 6, None, None, {"error": 0, "who": "holder"}),
        waiter_receipt=_receipt("sess-waiter", 1, 6, None, None, {"error": 0, "who": "waiter"}),
    )
    assert first["record"]["number"] == second["record"]["number"] == 1
    if waiter_file == FILE_A:
        assert second["record"] == first["record"]
    assert "sess-waiter" not in first["state"]["receipts"]
    persisted = store_mod.OfficeStore().read(CHAT)
    assert set(persisted["documents"]) == {FILE_A, waiter_file}
    for document in persisted["documents"].values():
        assert [record["number"] for record in document["versions"]] == [1]
        assert document["versions"][0]["sha256"] == _sha(SHARED)
    for session, who in (("sess-holder", "holder"), ("sess-waiter", "waiter")):
        assert persisted["receipts"][session]["1"] == {
            "status": 6, "sha256": _sha(SHARED), "version": 1, "answer": {"error": 0, "who": who},
        }
    assert {path.name for path in _versions_dir(data).iterdir()} == {_sha(SHARED)}
    assert _blob(data, SHARED).read_bytes() == SHARED


@pytest.mark.parametrize("damage", ["corrupt", "symlink", "nonregular", "unreadable"])
def test_claim_path_rejects_damaged_existing_blob_without_rewrite(world, monkeypatch, damage):
    store_mod, _docker_manager, data = world
    import office.versions as versions_mod
    _store(store_mod, SHARED, source="workspace", parent=None, published=True)
    _store(store_mod, AUTOSAVE, source="autosave", parent=1, published=False)
    blob = _blob(data, SHARED)
    outside = data.parent / "blob-secret.bin"
    outside.write_bytes(b"EXTERNAL-BLOB")
    if damage == "corrupt":
        blob.write_bytes(b"not-the-hash")
    elif damage == "symlink":
        blob.unlink()
        os.symlink(outside, blob)
        _forbid_inode_open(versions_mod, monkeypatch, outside)
    elif damage == "nonregular":
        blob.unlink()
        os.mkfifo(blob)
    else:
        original_open = versions_mod.os.open
        identity = _inode(blob)

        def deny(name, flags, *args, **kwargs):
            info = os.stat(name, dir_fd=kwargs.get("dir_fd"), follow_symlinks=False)
            if (info.st_dev, info.st_ino) == identity:
                raise OSError(errno.EACCES, "injected unreadable blob")
            return original_open(name, flags, *args, **kwargs)

        monkeypatch.setattr(versions_mod.os, "open", deny)
    before = _state(data).read_bytes()
    blob_info = blob.lstat()
    for file_id, parent in ((FILE_B, None), (FILE_A, 2)):
        with pytest.raises(store_mod.StateCorruptError):
            _store(store_mod, SHARED, file_id=file_id, source="restore", parent=parent, published=False,
                   receipt=_receipt(SESSION, 1, 6, None, None, {"error": 0}))
        assert _state(data).read_bytes() == before
        after = blob.lstat()
        assert (after.st_dev, after.st_ino, after.st_mode) == (blob_info.st_dev, blob_info.st_ino, blob_info.st_mode)
        assert not list(_staging(data).iterdir())
    if damage == "corrupt":
        assert blob.read_bytes() == b"not-the-hash"
    elif damage == "symlink":
        assert os.readlink(blob) == str(outside)
    elif damage == "unreadable":
        assert blob.read_bytes() == SHARED
    assert outside.read_bytes() == b"EXTERNAL-BLOB"


def test_missing_claim_blob_republishes_matching_content_and_missing_publish_preserves_state(world):
    store_mod, _docker_manager, data = world
    _store(store_mod, SHARED, source="workspace", parent=None, published=True)
    _blob(data, SHARED).unlink()
    restored = _store(store_mod, SHARED, file_id=FILE_B, source="conflict", parent=None, published=True,
                      receipt=_receipt(SESSION, 1, 6, None, None, {"error": 0}))
    assert restored["number"] == 1
    assert restored["sha256"] == _sha(SHARED)
    assert _blob(data, SHARED).read_bytes() == SHARED
    assert store_mod.OfficeStore().get_receipt(CHAT, SESSION, 1)["version"] == 1
    before = _state(data).read_bytes()
    for file_id, number in ((FILE_A, 9), ("missing-doc", 1)):
        with pytest.raises(ValueError):
            store_mod.OfficeStore().mark_published(CHAT, file_id, number)
        assert _state(data).read_bytes() == before


def test_conflicting_attached_receipt_cleans_owned_blob_and_keeps_shared(world, monkeypatch):
    store_mod, _docker_manager, data = world
    _store(store_mod, WORKSPACE, source="workspace", parent=None, published=True)
    prior = store_mod.OfficeStore().record_receipt(
        CHAT, SESSION, 4, {"status": 7, "sha256": None, "version": None, "answer": {"error": 0}}
    )
    before = _state(data).read_bytes()
    shared = _blob(data, WORKSPACE)
    shared_inode = _inode(shared)
    conflict = _receipt(SESSION, 4, 6, None, None, {"error": 1})
    original_link = os.link
    published = []

    def watch_link(src, dst, *args, **kwargs):
        result = original_link(src, dst, *args, **kwargs)
        published.append(dst)
        return result

    monkeypatch.setattr(os, "link", watch_link)
    with pytest.raises(ValueError, match="conflicting"):
        _store(store_mod, SAVE, source="save", parent=1, published=True, receipt=conflict)
    assert published == [_sha(SAVE)]
    assert _state(data).read_bytes() == before
    assert not _blob(data, SAVE).exists()
    assert store_mod.OfficeStore().get_receipt(CHAT, SESSION, 4) == prior
    with pytest.raises(ValueError, match="conflicting"):
        _store(store_mod, WORKSPACE, file_id=FILE_B, source="conflict", parent=None, published=True, receipt=conflict)
    assert _state(data).read_bytes() == before
    assert shared.read_bytes() == WORKSPACE
    assert _inode(shared) == shared_inode


@pytest.mark.parametrize("ancestor", [".ocu", ".ocu/office"])
def test_floor_uses_deepest_existing_ancestor_and_equal_store_admits(world, monkeypatch, ancestor):
    store_mod, _docker_manager, data = world
    import office.versions as versions_mod
    deepest = data / CHAT / ancestor
    deepest.mkdir(parents=True)
    before = {path.relative_to(data) for path in data.rglob("*")}
    identity = _inode(deepest)
    seen = _pin_floor_fd(versions_mod, monkeypatch, 40)
    store = store_mod.OfficeStore()
    with pytest.raises(versions_mod.StorageLowError):
        store.check_free_space(CHAT, 41)
    with pytest.raises(versions_mod.StorageLowError):
        _store(store_mod, SAVE, source="workspace", parent=None, published=True, floor=41)
    store.check_free_space(CHAT, 40)
    assert seen == [identity, identity, identity]
    assert {path.relative_to(data) for path in data.rglob("*")} == before | {Path(CHAT) / ".lifecycle.lock"}
    selected = _store(store_mod, SAVE, source="workspace", parent=None, published=True, floor=40)
    assert seen == [identity, identity, identity, identity]
    assert selected["number"] == 1
    assert _blob(data, SAVE).read_bytes() == SAVE


@pytest.mark.parametrize("tier", ["chat", ".ocu", "office"])
def test_floor_rejects_symlinked_control_ancestry_without_external_probe(world, monkeypatch, tier):
    store_mod, _docker_manager, data = world
    import office.versions as versions_mod
    outside = data.parent / "outside-floor"
    outside.mkdir()
    sentinel = outside / "keep.bin"
    sentinel.write_bytes(b"FLOOR-SECRET")
    target = {"chat": data / CHAT, ".ocu": data / CHAT / ".ocu",
              "office": _office(data)}[tier]
    target.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(outside, target)
    seen = _pin_floor_fd(versions_mod, monkeypatch, 40)
    _forbid_inode_open(versions_mod, monkeypatch, outside, sentinel)
    with pytest.raises(store_mod.StateCorruptError):
        store_mod.OfficeStore().check_free_space(CHAT, 0)
    with pytest.raises(store_mod.StateCorruptError):
        _store(store_mod, SAVE, source="workspace", parent=None, published=True)
    assert seen == []
    assert sentinel.read_bytes() == b"FLOOR-SECRET"
    assert {path.name for path in outside.iterdir()} == {"keep.bin"}


def test_state_mutator_sees_selected_version_and_commits_with_it(world):
    store_mod, _docker_manager, data = world
    seen = {}

    def mutate_state(working, selected):
        seen["number"] = selected["number"]
        seen["sha256"] = selected["sha256"]
        working["sessions"]["sess-create"] = {
            "session_id": "sess-create",
            "file_id": FILE_A,
            "state": "opening",
        }

    record = store_mod.OfficeStore().store_version(
        CHAT,
        FILE_A,
        WORKSPACE,
        source="workspace",
        parent=None,
        published=True,
        min_free_bytes=0,
        mutate_state=mutate_state,
    )
    persisted = store_mod.OfficeStore().read(CHAT)
    assert seen["number"] == 1
    assert seen["sha256"] == record["sha256"] == _sha(WORKSPACE)
    assert persisted["sessions"]["sess-create"]["file_id"] == FILE_A
    assert persisted["documents"][FILE_A]["versions"][0]["number"] == 1
    assert _blob(data, WORKSPACE).read_bytes() == WORKSPACE


def test_mutator_failure_cleans_owned_blob_and_leaves_no_record(world):
    store_mod, _docker_manager, data = world
    _store(store_mod, WORKSPACE, source="workspace", parent=None, published=True)
    before_state = _state(data).read_bytes()
    shared = _blob(data, WORKSPACE)
    shared_bytes = shared.read_bytes()

    def boom(_working, _selected):
        raise RuntimeError("mutator exploded")

    with pytest.raises(RuntimeError, match="mutator exploded"):
        store_mod.OfficeStore().store_version(
            CHAT,
            FILE_A,
            SAVE,
            source="save",
            parent=1,
            published=True,
            min_free_bytes=0,
            mutate_state=boom,
        )
    assert _state(data).read_bytes() == before_state
    assert shared.read_bytes() == shared_bytes
    assert not _blob(data, SAVE).exists()
    leftovers = [path for path in _office(data).rglob("*") if path.is_file() and path.suffix == ".tmp"]
    assert leftovers == []


def test_precommit_enospc_after_mutator_cleans_owned_blob(world, monkeypatch):
    store_mod, _docker_manager, data = world
    import office.versions as versions_mod
    _store(store_mod, WORKSPACE, source="workspace", parent=None, published=True)
    shared = _blob(data, WORKSPACE)
    shared_bytes = shared.read_bytes()
    before_state = _state(data).read_bytes()
    original_write = store_mod.os.write

    def fail_state_write(fd, payload):
        if payload[:1] == b"{":
            raise OSError(errno.ENOSPC, "injected state write fault")
        return original_write(fd, payload)

    def stamp(working, selected):
        working["sessions"]["lost"] = {"session_id": "lost", "file_id": FILE_A}

    monkeypatch.setattr(store_mod.os, "write", fail_state_write)
    with pytest.raises(versions_mod.StorageLowError) as error:
        store_mod.OfficeStore().store_version(
            CHAT,
            FILE_A,
            SAVE,
            source="save",
            parent=1,
            published=True,
            min_free_bytes=0,
            mutate_state=stamp,
        )
    assert error.value.reason == "storage_low"
    assert _state(data).read_bytes() == before_state
    assert shared.read_bytes() == shared_bytes
    assert not _blob(data, SAVE).exists()
    assert "lost" not in store_mod.OfficeStore().read(CHAT)["sessions"]


def test_postreplace_durability_failure_keeps_mutator_successor_and_blob(world, monkeypatch):
    store_mod, _docker_manager, data = world
    _store(store_mod, WORKSPACE, source="workspace", parent=None, published=True)
    original_replace = store_mod.os.replace
    original_fsync = store_mod.os.fsync
    replaced = {"done": False}

    def watch_state_replace(src, dst, *args, **kwargs):
        result = original_replace(src, dst, *args, **kwargs)
        if dst == "state.json" or (isinstance(dst, str) and dst.endswith("state.json")):
            replaced["done"] = True
        return result

    def fail_after_state_replace(fd):
        if replaced["done"]:
            replaced["done"] = False
            raise OSError("controlled directory fsync failure")
        return original_fsync(fd)

    def stamp(working, selected):
        working["sessions"]["live"] = {
            "session_id": "live",
            "file_id": FILE_A,
            "state": "opening",
        }

    with monkeypatch.context() as patches:
        patches.setattr(store_mod.os, "replace", watch_state_replace)
        patches.setattr(store_mod.os, "fsync", fail_after_state_replace)
        with pytest.raises(store_mod.StateDurabilityError, match="committed"):
            store_mod.OfficeStore().store_version(
                CHAT,
                FILE_A,
                SAVE,
                source="save",
                parent=1,
                published=True,
                min_free_bytes=0,
                mutate_state=stamp,
            )
    persisted = store_mod.OfficeStore().read(CHAT)
    assert persisted["documents"][FILE_A]["versions"][-1]["sha256"] == _sha(SAVE)
    assert persisted["sessions"]["live"]["state"] == "opening"
    assert _blob(data, SAVE).read_bytes() == SAVE
    assert _blob(data, WORKSPACE).read_bytes() == WORKSPACE

