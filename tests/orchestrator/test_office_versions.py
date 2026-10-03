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

    class Info:
        def __init__(self, available):
            self.f_bavail = available
            self.f_frsize = 1

    monkeypatch.setattr(versions_mod.os, "fstatvfs", lambda fd: Info(40))
    store = store_mod.OfficeStore()
    with pytest.raises(versions_mod.StorageLowError) as below:
        store.check_free_space(CHAT, 41)
    assert below.value.reason == "storage_low"
    store.check_free_space(CHAT, 40)
    store.check_free_space(CHAT, 0)
    assert not _state(data).exists()
    assert not _versions_dir(data).exists()
    with pytest.raises(versions_mod.StorageLowError) as refused:
        _store(store_mod, SAVE, source="workspace", parent=None, published=True, floor=41)
    assert refused.value.reason == "storage_low"
    assert not _state(data).exists()
    assert not _blob(data, SAVE).exists()
    assert not any(path.is_file() for path in _office(data).rglob("*")) if _office(data).exists() else True


def test_store_below_floor_adds_no_blob_record_or_receipt(world, monkeypatch):
    store_mod, _docker_manager, data = world
    _store(store_mod, WORKSPACE, source="workspace", parent=None, published=True)
    before_state = _state(data).read_bytes()
    before_blobs = {path.name: path.read_bytes() for path in _versions_dir(data).iterdir()}
    import office.versions as versions_mod

    class Info:
        f_bavail = 1
        f_frsize = 1

    monkeypatch.setattr(versions_mod.os, "fstatvfs", lambda fd: Info())
    receipt = _receipt(SESSION, 8, 6, None, None, {"error": 0})
    with pytest.raises(versions_mod.StorageLowError) as error:
        _store(store_mod, SAVE, source="save", parent=1, published=True, receipt=receipt, floor=2)
    assert error.value.reason == "storage_low"
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


def test_invalid_source_parent_and_receipt_keys_fail_before_content_publication(world):
    store_mod, _docker_manager, data = world
    _store(store_mod, WORKSPACE, source="workspace", parent=None, published=True)
    before_state = _state(data).read_bytes()
    before_blobs = {path.name: path.read_bytes() for path in _versions_dir(data).iterdir()}
    store = store_mod.OfficeStore()
    with pytest.raises(ValueError):
        store.store_version(CHAT, FILE_A, SAVE, source="draft", parent=1, published=True, min_free_bytes=0)
    with pytest.raises(ValueError):
        store.store_version(CHAT, FILE_A, SAVE, source="save", parent=9, published=True, min_free_bytes=0)
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
    hold = data.parent / "office-version-hold"
    entered = data.parent / "office-version-entered"
    contended = data.parent / "office-version-contended"
    os.mkfifo(hold)
    first_receipt = json.dumps(_receipt("sess-a", 1, 6, None, None, {"error": 0, "who": "a"}))
    second_receipt = json.dumps(_receipt("sess-b", 1, 6, None, None, {"error": 0, "who": "b"}))
    holder = waiter = None
    try:
        holder = _start_child(
            data,
            OCU_CHILD_OP="hold-then-store",
            OCU_ENTERED=str(entered),
            OCU_HOLD=str(hold),
            OCU_FILE_ID=FILE_A,
            OCU_CONTENT="alpha-bytes",
            OCU_SOURCE="workspace",
            OCU_PUBLISHED="1",
            OCU_FLOOR="0",
            OCU_RECEIPT=first_receipt,
        )
        _wait_marker(entered, holder, "holder did not enter the locked mutator")
        waiter = _start_child(
            data,
            OCU_CHILD_OP="wait-then-store",
            OCU_CONTENDED=str(contended),
            OCU_LOCK_DENIAL="version store lock was granted without contention",
            OCU_FILE_ID=FILE_A,
            OCU_CONTENT="beta-bytes",
            OCU_SOURCE="autosave",
            OCU_PARENT="1",
            OCU_FLOOR="0",
            OCU_RECEIPT=second_receipt,
        )
        _wait_marker(contended, waiter, "waiter did not observe LOCK_NB denial")
        assert holder.poll() is None
        assert waiter.poll() is None
        os.close(os.open(hold, os.O_WRONLY))
        holder_out, holder_err = holder.communicate(timeout=10)
        waiter_out, waiter_err = waiter.communicate(timeout=10)
        assert holder.returncode == 0, (holder_out, holder_err)
        assert waiter.returncode == 0, (waiter_out, waiter_err)
        first_state = json.loads(holder_out.strip().splitlines()[-1])
        second_state = json.loads(waiter_out.strip().splitlines()[-1])
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
    finally:
        _stop_child(holder)
        _stop_child(waiter)

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
