# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Public HTTP seam for the open-session workspace_changed status notice."""
from __future__ import annotations

import errno
import fcntl
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tests.orchestrator._office_store import _stop_child, _wait_marker
from tests.orchestrator.test_office_ooxml import intact_docx
from tests.orchestrator.test_office_save_close import (
    _child_env,
    _close,
    _command_box,
    _forcesave,
    _record,
    _save,
)
from tests.orchestrator.test_office_session_lifecycle import (
    _change,
    _created,
    _status,
)
from tests.orchestrator.test_office_sessions import (
    FINAL_STATES,
    SERVER_DIR,
    SPARSE_SIZE,
    _assert_refusal,
    _outputs,
    _put,
    _sha,
    _snapshot,
    _state,
    _versions,
    office_world,
)
from tests.orchestrator.test_office_workspace import _forbid_inode_open
from tests.orchestrator.test_outputs_endpoint import CHAT, _auth

BOOKKEEPING = ("workspace_changed", "last_checked_size", "last_checked_mtime_ns")


def _same_size_bytes(original: bytes) -> bytes:
    flipped = bytes((original[0] ^ 1,)) + original[1:]
    assert len(flipped) == len(original)
    assert flipped != original
    return flipped


def _workspace(data: Path, name="brief.docx") -> Path:
    return _outputs(data) / name


def _sample(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_size, info.st_mtime_ns


def _restore_sample(path: Path, size: int, mtime_ns: int, body: bytes) -> None:
    path.write_bytes(body)
    if path.stat().st_size != size:
        with path.open("r+b") as stream:
            stream.truncate(size)
    os.utime(path, ns=(mtime_ns, mtime_ns))
    restored = _sample(path)
    assert restored[0] == size
    assert restored[1] == mtime_ns


def _count_workspace_reads(module, monkeypatch, path: Path):
    identity = (path.stat().st_dev, path.stat().st_ino)
    original = module.os.read
    seen = {"count": 0}

    def counted(fd, amount):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == identity:
            seen["count"] += 1
        return original(fd, amount)

    monkeypatch.setattr(module.os, "read", counted)
    return seen


def _forbid_inode_stat(module, monkeypatch, *paths: Path):
    identities = {(path.lstat().st_dev, path.lstat().st_ino) for path in paths}
    original_stat = module.os.stat
    original_lstat = module.os.lstat

    def wrapped_stat(name, *args, **kwargs):
        info = original_stat(name, *args, **kwargs)
        if (info.st_dev, info.st_ino) in identities:
            raise AssertionError(("external target stated", name))
        return info

    def wrapped_lstat(name, *args, **kwargs):
        info = original_lstat(name, *args, **kwargs)
        if (info.st_dev, info.st_ino) in identities:
            raise AssertionError(("external target stated", name))
        return info

    monkeypatch.setattr(module.os, "stat", wrapped_stat)
    monkeypatch.setattr(module.os, "lstat", wrapped_lstat)


def _status_fields(http, session_id):
    response = _status(http, session_id)
    assert response.status_code == 200
    return response.json()


def _unrelated(record, before):
    return {key: value for key, value in record.items() if key not in BOOKKEEPING} == {
        key: value for key, value in before.items() if key not in BOOKKEEPING
    }


def test_first_status_hashes_then_equal_pair_repeats_false_without_reading(office_world, monkeypatch):
    http, data, origin, _docker, _broker = office_world
    import office.notice as notice
    _file_id, first = _created(office_world, "opening")
    path = _workspace(data)
    original = path.read_bytes()
    first_status = _status_fields(http, first["session_id"])
    assert first_status["workspace_changed"] is False
    assert first_status["state"] == "opening"
    record = _record(first["session_id"])
    assert (record["last_checked_size"], record["last_checked_mtime_ns"]) == _sample(path)
    reads = _count_workspace_reads(notice, monkeypatch, path)
    second = _status_fields(http, first["session_id"])
    assert second["workspace_changed"] is False
    assert second["state"] == "opening"
    assert reads["count"] == 0
    assert path.read_bytes() == original
    assert origin.hits == 0


def test_equal_pair_repeats_true_without_reading(office_world, monkeypatch):
    http, data, origin, _docker, _broker = office_world
    import office.notice as notice
    _file_id, first = _created(office_world, "editing")
    path = _workspace(data)
    assert _status_fields(http, first["session_id"])["workspace_changed"] is False
    path.write_bytes(path.read_bytes() + b"external edit")
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000))
    changed = _status_fields(http, first["session_id"])
    assert changed["workspace_changed"] is True
    assert changed["state"] == "editing"
    reads = _count_workspace_reads(notice, monkeypatch, path)
    repeated = _status_fields(http, first["session_id"])
    assert repeated["workspace_changed"] is True
    assert repeated["state"] == "editing"
    assert reads["count"] == 0
    assert origin.hits == 0


def test_size_changing_rewrite_reports_true_without_lifecycle_change(office_world):
    http, data, origin, _docker, _broker = office_world
    from office.store import OfficeStore
    file_id, first = _created(office_world, "editing")
    path = _workspace(data)
    assert _status_fields(http, first["session_id"])["workspace_changed"] is False
    before = OfficeStore().read(CHAT)
    path.write_bytes(path.read_bytes() + b"external edit")
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000))
    changed = _status_fields(http, first["session_id"])
    assert changed["workspace_changed"] is True
    assert changed["state"] == "editing"
    after = OfficeStore().read(CHAT)
    assert after["documents"] == before["documents"]
    assert after["receipts"] == before["receipts"]
    assert after["journal"] == before["journal"]
    assert _unrelated(after["sessions"][first["session_id"]], before["sessions"][first["session_id"]])
    assert origin.hits == 0
    assert file_id == first["file_id"]


def test_same_size_new_mtime_is_true_and_forged_restore_repeats_prior_false(office_world):
    http, data, origin, _docker, _broker = office_world
    _file_id, first = _created(office_world, "editing")
    path = _workspace(data)
    original = path.read_bytes()
    assert _status_fields(http, first["session_id"])["workspace_changed"] is False
    first_sample = _sample(path)
    replacement = _same_size_bytes(original)
    path.write_bytes(replacement)
    os.utime(path, ns=(first_sample[1] + 2_000_000, first_sample[1] + 2_000_000))
    assert path.stat().st_size == first_sample[0]
    assert path.stat().st_mtime_ns != first_sample[1]
    changed = _status_fields(http, first["session_id"])
    assert changed["workspace_changed"] is True
    true_sample = _sample(path)
    forged = _same_size_bytes(replacement)
    path.write_bytes(forged)
    os.utime(path, ns=(true_sample[1], true_sample[1]))
    assert _sample(path) == true_sample
    restored = _status_fields(http, first["session_id"])
    assert restored["workspace_changed"] is True
    _restore_sample(path, first_sample[0], first_sample[1], original)
    original_restored = _status_fields(http, first["session_id"])
    assert original_restored["workspace_changed"] is False
    assert original_restored["state"] == "editing"
    assert origin.hits == 0


def test_deleted_or_moved_file_reports_true_and_clears_cache(office_world):
    http, data, origin, _docker, _broker = office_world
    _file_id, first = _created(office_world, "editing")
    path = _workspace(data)
    assert _status_fields(http, first["session_id"])["workspace_changed"] is False
    path.rename(path.with_name("moved.docx"))
    moved = _status_fields(http, first["session_id"])
    assert moved["workspace_changed"] is True
    record = _record(first["session_id"])
    assert record["last_checked_size"] is None
    assert record["last_checked_mtime_ns"] is None
    path.with_name("moved.docx").unlink()
    missing = _status_fields(http, first["session_id"])
    assert missing["workspace_changed"] is True
    assert origin.hits == 0


def test_unreconciled_rename_reports_true_without_following_new_path(office_world, monkeypatch):
    http, data, origin, _docker, _broker = office_world
    import office.notice as notice
    _file_id, first = _created(office_world, "editing")
    path = _workspace(data)
    assert _status_fields(http, first["session_id"])["workspace_changed"] is False
    renamed = path.with_name("renamed.docx")
    path.rename(renamed)
    with monkeypatch.context() as trap:
        _forbid_inode_open(notice, trap, renamed)
        changed = _status_fields(http, first["session_id"])
    assert changed["workspace_changed"] is True
    assert origin.hits == 0


def test_symlink_leaf_parent_nonregular_and_traversal_do_not_open_targets(office_world, monkeypatch):
    http, data, origin, _docker, broker = office_world
    import office.notice as notice
    _put(data, "nested/brief.docx", intact_docx())
    listing = broker.OutputsBroker().reconcile(CHAT)
    nested_id = next(item["file_id"] for item in listing["entries"] if item["path"] == "nested/brief.docx")
    created = http.post(
        f"/api/office/{CHAT}/documents/{nested_id}/sessions", headers=_auth()
    )
    assert created.status_code == 201
    session_id = created.json()["session_id"]
    _change(session_id, state="editing")
    assert _status_fields(http, session_id)["workspace_changed"] is False
    secret = data.parent / "external-secret.bin"
    secret.write_bytes(b"EXTERNAL-TARGET-BYTES")
    leaf = _outputs(data) / "nested" / "brief.docx"
    parent = _outputs(data) / "nested"
    leaf.unlink()
    os.symlink(secret, leaf)
    with monkeypatch.context() as trap:
        _forbid_inode_open(notice, trap, secret)
        leaf_status = _status_fields(http, session_id)
    assert leaf_status["workspace_changed"] is True
    assert leaf_status["state"] == "editing"
    assert secret.read_bytes() == b"EXTERNAL-TARGET-BYTES"
    leaf.unlink()
    parent.rmdir()
    os.symlink(secret.parent, parent)
    with monkeypatch.context() as trap:
        _forbid_inode_open(notice, trap, secret, secret.parent)
        parent_status = _status_fields(http, session_id)
    assert parent_status["workspace_changed"] is True
    parent.unlink()
    parent.mkdir()
    os.mkfifo(leaf)
    fifo_status = _status_fields(http, session_id)
    assert fifo_status["workspace_changed"] is True
    leaf.unlink()
    leaf.mkdir()
    folder_status = _status_fields(http, session_id)
    assert folder_status["workspace_changed"] is True
    record = _record(session_id)
    assert record["last_checked_size"] is None
    assert record["last_checked_mtime_ns"] is None
    assert origin.hits == 0


def test_recovery_after_invalid_sample_hashes_even_when_old_stat_matches(office_world, monkeypatch):
    http, data, origin, _docker, _broker = office_world
    import office.notice as notice
    _file_id, first = _created(office_world, "editing")
    path = _workspace(data)
    original = path.read_bytes()
    first_status = _status_fields(http, first["session_id"])
    assert first_status["workspace_changed"] is False
    cached = _sample(path)
    path.unlink()
    missing = _status_fields(http, first["session_id"])
    assert missing["workspace_changed"] is True
    assert _record(first["session_id"])["last_checked_size"] is None
    path.write_bytes(original)
    os.utime(path, ns=(cached[1], cached[1]))
    assert _sample(path) == cached
    reads = _count_workspace_reads(notice, monkeypatch, path)
    recovered = _status_fields(http, first["session_id"])
    assert recovered["workspace_changed"] is False
    assert reads["count"] > 0
    assert origin.hits == 0


def test_other_workspace_files_are_not_stated_or_read(office_world, monkeypatch):
    http, data, origin, _docker, broker = office_world
    import office.notice as notice
    _put(data, "other.docx", intact_docx())
    broker.OutputsBroker().reconcile(CHAT)
    _file_id, first = _created(office_world, "editing")
    other = _workspace(data, "other.docx")
    assert _status_fields(http, first["session_id"])["workspace_changed"] is False
    other.write_bytes(other.read_bytes() + b"sibling")
    os.utime(other, ns=(other.stat().st_atime_ns, other.stat().st_mtime_ns + 3_000_000))
    with monkeypatch.context() as trap:
        reads = _count_workspace_reads(notice, trap, other)
        _forbid_inode_open(notice, trap, other)
        _forbid_inode_stat(notice, trap, other)
        status = _status_fields(http, first["session_id"])
    assert status["workspace_changed"] is False
    assert status["state"] == "editing"
    assert reads["count"] == 0
    assert origin.hits == 0


def test_save_is_accepted_while_workspace_changed_is_true(office_world, monkeypatch):
    http, data, recording, _docker, _broker = office_world
    _file_id, first = _created(office_world, "editing")
    path = _workspace(data)
    assert _status_fields(http, first["session_id"])["workspace_changed"] is False
    path.write_bytes(path.read_bytes() + b"external edit")
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000))
    changed = _status_fields(http, first["session_id"])
    assert changed["workspace_changed"] is True
    with _forcesave(monkeypatch, first["document_key"]) as (origin, seen):
        response = _save(http, first["session_id"], "publish")
        assert response.status_code == 202
        assert response.json() == {
            "session_id": first["session_id"], "save_seq": 1, "intent": "publish",
        }
        assert seen == [{"save_seq": 1, "intent": "publish"}]
        assert _status_fields(http, first["session_id"])["workspace_changed"] is True
        assert len(origin.requests) == 1
    assert recording.hits == 0


def test_open_status_cache_miss_preserves_pending_allocations(office_world, monkeypatch):
    http, data, recording, _docker, _broker = office_world
    from office.store import OfficeStore
    file_id, first = _created(office_world, "editing")
    path = _workspace(data)
    assert _status_fields(http, first["session_id"])["workspace_changed"] is False
    with _command_box(monkeypatch, first["document_key"]) as (origin, seen, infos):
        saved = _save(http, first["session_id"], "publish")
        assert saved.status_code == 202
        closed = _close(http, first["session_id"])
        assert closed.status_code == 202
        assert closed.json()["state"] == "closing"
        before = OfficeStore().read(CHAT)
        record_before = before["sessions"][first["session_id"]]
        assert record_before["pending_save_seq"] == 1
        assert record_before["pending_close_seq"] == 2
        path.write_bytes(path.read_bytes() + b"external edit")
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 5_000_000))
        status = _status_fields(http, first["session_id"])
        assert status["workspace_changed"] is True
        assert status["state"] == "closing"
        after = OfficeStore().read(CHAT)
        record = after["sessions"][first["session_id"]]
        assert _unrelated(record, record_before)
        assert record["pending_save_seq"] == 1
        assert record["pending_close_seq"] == 2
        assert record["save_seq"] == 2
        assert record["save_intents"] == {"1": "publish"}
        assert record["reason"] == record_before.get("reason")
        assert record["baseline_sha256"] == record_before["baseline_sha256"]
        assert after["documents"] == before["documents"]
        assert after["receipts"] == before["receipts"]
        assert after["journal"] == before["journal"]
        assert seen == [{"save_seq": 1, "intent": "publish"}]
        assert infos == [{"c": "info", "key": first["document_key"]}]
    assert recording.hits == 0
    assert file_id == first["file_id"]


def test_final_and_epoch_orphan_status_do_no_workspace_io(office_world, monkeypatch):
    http, data, origin, _docker, _broker = office_world
    import office.notice as notice
    import office.sessions as sessions
    for state in FINAL_STATES:
        _file_id, first = _created(office_world, state, name=f"{state}.docx")
        _change(
            first["session_id"],
            pending_save_seq=1,
            pending_close_seq=2,
            save_intents={"1": "publish"},
            save_seq=2,
            last_committed_seq=1,
        )
        encoded = _state(data).read_bytes()
        path = _workspace(data, f"{state}.docx")
        with monkeypatch.context() as trap:
            _forbid_inode_open(sessions, trap, path.parent, path)
            _forbid_inode_open(notice, trap, path.parent, path)
            response = _status(http, first["session_id"])
        assert response.status_code == 200
        assert response.json()["state"] == state
        assert _state(data).read_bytes() == encoded
        record = _record(first["session_id"])
        assert record["pending_save_seq"] == 1
        assert record["pending_close_seq"] == 2
    file_id, live = _created(office_world, "editing", name="epoch.docx")
    _change(
        live["session_id"],
        pending_save_seq=3,
        save_intents={"3": "persist"},
        save_seq=3,
        last_committed_seq=2,
        last_published_seq=2,
    )
    before = _record(live["session_id"])
    (data / ".office-restore-epoch").write_text("epoch-B\n")
    path = _workspace(data, "epoch.docx")
    with monkeypatch.context() as trap:
        _forbid_inode_open(sessions, trap, path.parent, path)
        _forbid_inode_open(notice, trap, path.parent, path)
        response = _status(http, live["session_id"])
    assert response.status_code == 200
    assert response.json()["state"] == "orphaned"
    after = _record(live["session_id"])
    assert after["pending_save_seq"] == 3
    assert after["save_intents"] == {"3": "persist"}
    assert after["reason"] == "restore_epoch_changed"
    assert after.get("last_checked_size") == before.get("last_checked_size")
    assert origin.hits == 0
    assert file_id == live["file_id"]


@pytest.mark.parametrize("updates", (
    {"baseline_sha256": None},
    {"baseline_sha256": "not-a-hash"},
    {"baseline_sha256": "f" * 63},
    {"last_checked_size": True, "last_checked_mtime_ns": 1},
    {"last_checked_size": 12, "last_checked_mtime_ns": True},
    {"last_checked_size": -1, "last_checked_mtime_ns": 1},
    {"last_checked_size": 12, "last_checked_mtime_ns": None},
    {"last_checked_size": None, "last_checked_mtime_ns": 1},
    {"last_checked_size": 12},
    {"last_checked_mtime_ns": 1},
))
def test_corrupt_baseline_or_partial_cache_is_state_corrupt(office_world, updates):
    http, data, origin, _docker, _broker = office_world
    _file_id, first = _created(office_world, "editing")
    _change(first["session_id"], **updates)
    before = _snapshot(data)
    _assert_refusal(_status(http, first["session_id"]), 500, "state_corrupt")
    assert _snapshot(data) == before
    assert origin.hits == 0


def test_missing_cache_pair_bootstraps_and_boolean_mtime_is_corrupt(office_world):
    http, data, origin, _docker, _broker = office_world
    _file_id, first = _created(office_world, "editing")
    _change(first["session_id"], last_checked_size=None, last_checked_mtime_ns=None)
    first_status = _status_fields(http, first["session_id"])
    assert first_status["workspace_changed"] is False
    record = _record(first["session_id"])
    assert type(record["last_checked_size"]) is int
    assert type(record["last_checked_mtime_ns"]) is int
    _change(first["session_id"], last_checked_size=record["last_checked_size"], last_checked_mtime_ns=True)
    before = _snapshot(data)
    _assert_refusal(_status(http, first["session_id"]), 500, "state_corrupt")
    assert _snapshot(data) == before
    assert origin.hits == 0


def test_corrupt_index_is_explicit_and_missing_identity_is_changed(office_world):
    http, data, origin, _docker, _broker = office_world
    _file_id, first = _created(office_world, "editing")
    assert _status_fields(http, first["session_id"])["workspace_changed"] is False
    index = data / CHAT / ".ocu" / "index.json"
    broken = b"{broken-json"
    encoded = index.read_bytes()
    index.write_bytes(broken)
    before = _snapshot(data)
    _assert_refusal(_status(http, first["session_id"]), 500, "state_corrupt")
    assert _snapshot(data) == before
    parsed = json.loads(encoded.decode("utf-8"))
    parsed["active"] = {}
    parsed["fingerprints"] = {}
    index.write_text(json.dumps(parsed))
    missing = _status_fields(http, first["session_id"])
    assert missing["workspace_changed"] is True
    assert origin.hits == 0


def test_unreadable_unsafe_and_oversized_index_are_state_corrupt(office_world, monkeypatch):
    http, data, origin, _docker, _broker = office_world
    import outputs_broker as broker_mod
    import office.notice as notice
    _file_id, first = _created(office_world, "editing")
    assert _status_fields(http, first["session_id"])["workspace_changed"] is False
    index = data / CHAT / ".ocu" / "index.json"
    original_open = broker_mod.os.open

    def deny_index(path, flags, *args, **kwargs):
        if path == "index.json":
            raise PermissionError(errno.EACCES, "injected unreadable broker index")
        return original_open(path, flags, *args, **kwargs)

    before = _snapshot(data)
    with monkeypatch.context() as trap:
        trap.setattr(broker_mod.os, "open", deny_index)
        response = _status(http, first["session_id"])
    _assert_refusal(response, 500, "state_corrupt")
    assert response.content == b'{"reason":"state_corrupt"}'
    assert _snapshot(data) == before

    encoded = index.read_bytes()
    outside = data.parent / "index-target.json"
    outside.write_bytes(encoded)
    index.unlink()
    index.symlink_to(outside)
    before_link = _snapshot(data)
    _assert_refusal(_status(http, first["session_id"]), 500, "state_corrupt")
    assert _snapshot(data) == before_link
    index.unlink()
    index.write_bytes(encoded)

    oversized = broker_mod.OutputsBroker(max_index_size=len(encoded) - 1)
    monkeypatch.setattr(notice, "OutputsBroker", lambda *args, **kwargs: oversized)
    before_limit = _snapshot(data)
    _assert_refusal(_status(http, first["session_id"]), 500, "state_corrupt")
    assert _snapshot(data) == before_limit
    assert origin.hits == 0


def test_store_write_failures_stay_explicit(office_world, monkeypatch):
    http, data, origin, _docker, _broker = office_world
    import office.store as store_mod
    _file_id, first = _created(office_world, "editing")
    path = _workspace(data)
    path.write_bytes(path.read_bytes() + b"external")
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000))
    original_write = store_mod.os.write

    def fail_state_write(fd, payload):
        if payload[:1] == b"{":
            raise OSError(errno.EIO, "injected notice write fault")
        return original_write(fd, payload)

    monkeypatch.setattr(store_mod.os, "write", fail_state_write)
    _assert_refusal(_status(http, first["session_id"]), 500, "state_corrupt")
    original_replace = store_mod.os.replace
    original_fsync = store_mod.os.fsync
    replaced = {"done": False}

    def watch_state_replace(src, dst, *args, **kwargs):
        result = original_replace(src, dst, *args, **kwargs)
        if isinstance(dst, str) and dst.endswith("state.json"):
            replaced["done"] = True
        return result

    def fail_after_state_replace(fd):
        if replaced["done"]:
            replaced["done"] = False
            raise OSError("controlled directory fsync failure")
        return original_fsync(fd)

    monkeypatch.setattr(store_mod.os, "write", original_write)
    monkeypatch.setattr(store_mod.os, "replace", watch_state_replace)
    monkeypatch.setattr(store_mod.os, "fsync", fail_after_state_replace)
    _assert_refusal(_status(http, first["session_id"]), 500, "state_durability")
    assert origin.hits == 0


def test_oversized_file_reports_true_without_content_read(office_world, monkeypatch):
    http, data, origin, _docker, _broker = office_world
    import office.workspace as workspace
    _file_id, first = _created(office_world, "editing")
    path = _workspace(data)
    assert _status_fields(http, first["session_id"])["workspace_changed"] is False
    with path.open("wb") as stream:
        stream.truncate(SPARSE_SIZE)
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000))
    reads = _count_workspace_reads(workspace, monkeypatch, path)
    changed = _status_fields(http, first["session_id"])
    assert changed["workspace_changed"] is True
    assert reads["count"] == 0
    record = _record(first["session_id"])
    assert record["last_checked_size"] is None
    assert origin.hits == 0


def test_unreadable_edited_file_reports_changed_and_invalidates_pair(office_world, monkeypatch):
    http, data, origin, _docker, _broker = office_world
    import office.workspace as workspace
    from office.store import OfficeStore
    _file_id, first = _created(office_world, "editing")
    path = _workspace(data)
    assert _status_fields(http, first["session_id"])["workspace_changed"] is False
    identity = (path.stat().st_dev, path.stat().st_ino)
    before = OfficeStore().read(CHAT)
    original_open = workspace.os.open

    def deny_edited(name, flags, *args, **kwargs):
        dir_fd = kwargs.get("dir_fd")
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False) if dir_fd is not None else os.stat(name)
        except (OSError, TypeError, ValueError):
            info = None
        if info is not None and (info.st_dev, info.st_ino) == identity:
            raise PermissionError(errno.EACCES, "injected unreadable edited file")
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(workspace.os, "open", deny_edited)
    changed = _status_fields(http, first["session_id"])
    assert changed["workspace_changed"] is True
    assert changed["state"] == "editing"
    after = OfficeStore().read(CHAT)
    record = after["sessions"][first["session_id"]]
    assert record["last_checked_size"] is None
    assert record["last_checked_mtime_ns"] is None
    assert after["documents"] == before["documents"]
    assert after["receipts"] == before["receipts"]
    assert after["journal"] == before["journal"]
    assert origin.hits == 0


def test_hash_then_stat_instability_invalidates_then_stable_poll_resamples(office_world, monkeypatch):
    http, data, origin, _docker, _broker = office_world
    import office.notice as notice
    from office.store import OfficeStore
    _file_id, first = _created(office_world, "editing")
    path = _workspace(data)
    original = path.read_bytes()
    assert _status_fields(http, first["session_id"])["workspace_changed"] is False
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000))
    real_hash = notice._hash_regular
    mutated = {"done": False}

    def mutate_after_hash(file_fd, label, *, max_bytes=None):
        result = real_hash(file_fd, label, max_bytes=max_bytes)
        if not mutated["done"]:
            mutated["done"] = True
            replacement = original + b"post-hash"
            path.write_bytes(replacement)
            os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 2_000_000))
        return result

    monkeypatch.setattr(notice, "_hash_regular", mutate_after_hash)
    before = OfficeStore().read(CHAT)
    unstable = _status_fields(http, first["session_id"])
    assert unstable["workspace_changed"] is True
    after = OfficeStore().read(CHAT)
    record = after["sessions"][first["session_id"]]
    assert record["last_checked_size"] is None
    assert record["last_checked_mtime_ns"] is None
    assert after["documents"] == before["documents"]
    monkeypatch.setattr(notice, "_hash_regular", real_hash)
    resampled = _status_fields(http, first["session_id"])
    assert resampled["workspace_changed"] is True
    recovered = _record(first["session_id"])
    assert recovered["last_checked_size"] == path.stat().st_size
    assert recovered["last_checked_mtime_ns"] == path.stat().st_mtime_ns
    assert origin.hits == 0


def test_two_workers_preserve_callback_like_commit_and_notice_bookkeeping(office_world, monkeypatch):
    http, data, origin, _docker, _broker = office_world
    import docker_manager
    from office.store import OfficeStore
    file_id, first = _created(office_world, "editing")
    path = _workspace(data)
    assert _status_fields(http, first["session_id"])["workspace_changed"] is False
    ready = data.parent / "notice-worker-ready"
    entered = data.parent / "notice-worker-entered"
    hold = data.parent / "notice-worker-hold"
    os.mkfifo(hold)
    env = _child_env(
        data,
        "http://127.0.0.1:9",
        OCU_SESSION=first["session_id"],
        OCU_FILE=file_id,
        READY=str(ready),
        ENTERED=str(entered),
        HOLD=str(hold),
        OCU_DIGEST=_sha(b"callback-like-version"),
    )
    child_src = r"""
import json, os, sys
from pathlib import Path
import docker_manager
from office.store import OfficeStore
Path(os.environ["READY"]).write_text("ready")
sys.stdin.readline()
chat = os.environ["OCU_CHAT"]
with docker_manager._combined_lock(chat):
    Path(os.environ["ENTERED"]).write_text("entered")
    fd = os.open(os.environ["HOLD"], os.O_RDONLY)
    os.close(fd)
    OfficeStore().store_version(
        chat,
        os.environ["OCU_FILE"],
        b"callback-like-version",
        source="save",
        parent=1,
        published=False,
        min_free_bytes=0,
        receipt={
            "session_id": os.environ["OCU_SESSION"],
            "save_seq": 4,
            "status": 6,
            "sha256": os.environ["OCU_DIGEST"],
            "version": None,
            "answer": {"error": 0},
        },
    )
print(json.dumps({"ok": True}))
"""
    child = subprocess.Popen(
        [sys.executable, "-c", child_src],
        cwd=str(SERVER_DIR),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    holder = None
    parent = None
    lock_attempt = threading.Event()
    original_flock = docker_manager.fcntl.flock

    def watch_flock(fd, operation):
        if operation == fcntl.LOCK_EX:
            lock_attempt.set()
        return original_flock(fd, operation)

    monkeypatch.setattr(docker_manager.fcntl, "flock", watch_flock)
    try:
        _wait_marker(ready, child, "second worker did not load the store")
        path.write_bytes(path.read_bytes() + b"external edit")
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 4_000_000))
        child.stdin.write("go\n")
        child.stdin.flush()
        _wait_marker(entered, child, "callback-like commit did not enter the mutator")
        parent = threading.Thread(
            target=lambda: setattr(parent, "response", _status(http, first["session_id"]))
        )
        parent.start()
        assert lock_attempt.wait(5), "status did not attempt the canonical flock while the child held it"
        assert parent.is_alive()
        assert child.poll() is None
        holder = os.open(hold, os.O_WRONLY)
        stdout, stderr = child.communicate(timeout=30)
        parent.join(timeout=10)
        assert parent.is_alive() is False
        assert child.returncode == 0, stderr
        assert json.loads(stdout.strip().splitlines()[-1]) == {"ok": True}
        assert parent.response.status_code == 200
        assert parent.response.json()["workspace_changed"] is True
        persisted = OfficeStore().read(CHAT)
        record = persisted["sessions"][first["session_id"]]
        assert record["workspace_changed"] is True
        assert type(record["last_checked_size"]) is int
        assert persisted["receipts"][first["session_id"]]["4"]["status"] == 6
        assert persisted["documents"][file_id]["versions"][-1]["source"] == "save"
        assert (_versions(data) / _sha(b"callback-like-version")).read_bytes() == b"callback-like-version"
    finally:
        if holder is not None:
            os.close(holder)
        if parent is not None and parent.is_alive():
            parent.join(timeout=10)
        _stop_child(child)
    assert origin.hits == 0
