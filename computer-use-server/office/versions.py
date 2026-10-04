# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Immutable versions, content-addressed blobs and save receipts."""
from __future__ import annotations

import datetime
import errno
import hashlib
import os
import re
import stat
import uuid
from typing import TYPE_CHECKING, Any, Callable

import docker_manager

from .store import (
    StateCorruptError,
    StateDurabilityError,
    _DATA_ROOT_FLAGS,
    _FILE_FLAGS,
    _WRITE_FLAGS,
)

if TYPE_CHECKING:
    from .store import OfficeStore

VERSION_SOURCES = ("workspace", "save", "autosave", "close", "restore", "conflict")
_VERSION_FIELDS = ("number", "parent", "sha256", "size", "source", "created_at", "published")
_RECEIPT_VALUE_FIELDS = ("status", "sha256", "version", "answer")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class StorageLowError(RuntimeError):
    """Free space is below the caller-supplied floor, or a write failed for lack of space."""

    reason = "storage_low"

    def __init__(self, message: str = "office storage is below the free-space floor") -> None:
        super().__init__(message)
        self.reason = "storage_low"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _require_file_id(file_id: str) -> None:
    if not isinstance(file_id, str) or not file_id or "\x00" in file_id:
        raise ValueError("file_id must be a non-empty string")


def _require_session_id(session_id: Any) -> str:
    if not isinstance(session_id, str) or not session_id or "\x00" in session_id:
        raise ValueError("session_id must be a non-empty string")
    return session_id


def _require_save_seq(save_seq: Any) -> int:
    if type(save_seq) is not int or save_seq < 1:
        raise ValueError("save_seq must be a positive integer")
    return save_seq


def _require_floor(min_free_bytes: Any) -> None:
    if type(min_free_bytes) is not int or min_free_bytes < 0:
        raise ValueError("min_free_bytes must be a nonnegative integer")


def _storage_error(error: OSError) -> Exception:
    if error.errno == errno.ENOSPC:
        return StorageLowError("office storage write failed for lack of space")
    return error


def _available_bytes(fd: int) -> int:
    info = os.fstatvfs(fd)
    return info.f_bavail * info.f_frsize


def store_version(
    store: OfficeStore,
    chat_id: str,
    file_id: str,
    content: bytes,
    *,
    source: str,
    parent: int | None,
    published: bool,
    min_free_bytes: int,
    receipt: dict[str, Any] | None = None,
    mutate_state: Callable[[dict[str, Any], dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    chat = docker_manager.canonical_lock_chat_id(chat_id)
    _require_file_id(file_id)
    if not isinstance(content, (bytes, bytearray)):
        raise ValueError("version content must be bytes")
    content = bytes(content)
    if source not in VERSION_SOURCES:
        raise ValueError(f"version source must be one of {VERSION_SOURCES}")
    if parent is not None and (type(parent) is not int or parent < 1):
        raise ValueError("version parent must be None or an existing version number")
    if type(published) is not bool:
        raise ValueError("published must be a boolean")
    _require_floor(min_free_bytes)
    receipt_spec = _caller_receipt(receipt) if receipt is not None else None
    digest = hashlib.sha256(content).hexdigest()
    selected: dict[str, Any] | None = None
    owned_blob = False
    store._assert_chat_root_safe(chat, allow_missing=True)
    with docker_manager._combined_lock(chat):
        store._assert_chat_root_safe(chat, allow_missing=False)
        state = store._snapshot(store._load(chat, create=False))
        versions = _document_versions(state, file_id)
        latest = versions[-1] if versions else None
        reuse_latest = latest is not None and latest["sha256"] == digest
        if reuse_latest:
            if receipt_spec is not None:
                receipt_spec = _bind_receipt(receipt_spec, latest["number"], digest)
        else:
            next_number = versions[-1]["number"] + 1 if versions else 1
            _require_parent(versions, parent, next_number)
            if receipt_spec is not None:
                receipt_spec = _bind_receipt(receipt_spec, next_number, digest)
            _require_free_space(store, chat, min_free_bytes)
            try:
                owned_blob = _publish_blob(store, chat, digest, content)
            except OSError as extra:
                raise _storage_error(extra) from extra
        bound = receipt_spec

        def mutate(working: dict[str, Any]) -> None:
            nonlocal selected
            listed = _document_versions(working, file_id)
            current = listed[-1] if listed else None
            if current is not None and current["sha256"] == digest:
                if published and not current["published"]:
                    current["published"] = True
                selected = dict(current)
            else:
                number = listed[-1]["number"] + 1 if listed else 1
                _require_parent(listed, parent, number)
                selected = {
                    "number": number,
                    "parent": parent,
                    "sha256": digest,
                    "size": len(content),
                    "source": source,
                    "created_at": _now(),
                    "published": published,
                }
                listed.append(selected)
            if bound is not None:
                _put_receipt(working, bound["session_id"], bound["save_seq"], bound["value"])
            if mutate_state is not None:
                extra = mutate_state(working, selected)
                if extra is not None:
                    raise ValueError("office state mutator must return None")

        try:
            store.update(chat, mutate)
        except StateDurabilityError:
            raise
        except OSError as extra:
            if owned_blob:
                _unlink_blob(store, chat, digest)
            raise _storage_error(extra) from extra
        except BaseException:
            if owned_blob:
                _unlink_blob(store, chat, digest)
            raise
    assert selected is not None
    return dict(selected)


def mark_published(store: OfficeStore, chat_id: str, file_id: str, number: int) -> dict[str, Any]:
    _require_file_id(file_id)
    if type(number) is not int or number < 1:
        raise ValueError("version number must be a positive integer")
    marked: dict[str, Any] | None = None

    def mutate(state: dict[str, Any]) -> None:
        nonlocal marked
        for record in _document_versions(state, file_id):
            if record["number"] == number:
                record["published"] = True
                marked = dict(record)
                return
        raise ValueError(f"version {number} does not exist for document {file_id}")

    store.update(chat_id, mutate)
    assert marked is not None
    return marked


def record_receipt(
    store: OfficeStore,
    chat_id: str,
    session_id: str,
    save_seq: int,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    session = _require_session_id(session_id)
    seq = _require_save_seq(save_seq)
    value = _receipt_value(receipt)

    def mutate(state: dict[str, Any]) -> None:
        _put_receipt(state, session, seq, value)

    store.update(chat_id, mutate)
    return dict(value)


def get_receipt(
    store: OfficeStore,
    chat_id: str,
    session_id: str,
    save_seq: int,
    expected_hash: str | None = None,
) -> dict[str, Any] | None:
    session = _require_session_id(session_id)
    seq = _require_save_seq(save_seq)
    if expected_hash is not None and not _HASH.fullmatch(expected_hash):
        raise ValueError("expected content hash must be a SHA-256 hex digest")
    state = store.read(chat_id)
    slot = _session_receipts(state, session, create=False)
    if slot is None:
        return None
    key = str(seq)
    if key not in slot:
        return None
    value = _persisted_receipt(slot[key])
    if expected_hash is not None and value["sha256"] != expected_hash:
        return None
    return dict(value)


def check_free_space(store: OfficeStore, chat_id: str, min_free_bytes: int) -> None:
    chat = docker_manager.canonical_lock_chat_id(chat_id)
    _require_floor(min_free_bytes)
    store._assert_chat_root_safe(chat, allow_missing=True)
    with docker_manager._combined_lock(chat):
        store._assert_chat_root_safe(chat, allow_missing=False)
        _require_free_space(store, chat, min_free_bytes)


def _require_free_space(store: OfficeStore, chat: str, min_free_bytes: int) -> None:
    fd = _probe_free_space_fd(store, chat)
    try:
        available = _available_bytes(fd)
    finally:
        os.close(fd)
    if available < min_free_bytes:
        raise StorageLowError(
            f"free space {available} is below the floor {min_free_bytes}"
        )


def _probe_free_space_fd(store: OfficeStore, chat: str) -> int:
    base = str(docker_manager.BASE_DATA_DIR)
    try:
        base_fd = os.open(base, _DATA_ROOT_FLAGS)
    except OSError as extra:
        raise StateCorruptError(f"cannot safely open data root: {base}") from extra
    held: list[int] = [base_fd]
    try:
        for name, label in (
            (chat, "chat control root"),
            (".ocu", "office control directory"),
            ("office", "office state directory"),
        ):
            child = store._open_dir(name, dir_fd=held[-1], create=False, label=label)
            if child is None:
                break
            held.append(child)
        probe = held[-1]
        held[-1] = -1
        return probe
    finally:
        for fd in reversed(held):
            if fd >= 0:
                os.close(fd)


def _publish_blob(store: OfficeStore, chat: str, digest: str, content: bytes) -> bool:
    opened = store._open_tree(chat, create=True)
    assert opened is not None
    base_fd, root_fd, ocu_fd, office_fd = opened
    staging_fd = versions_fd = None
    temporary = f".blob.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    claimed = False
    try:
        staging_fd = store._open_dir(
            "staging", dir_fd=office_fd, create=True, label="office staging directory"
        )
        versions_fd = store._open_dir(
            "versions", dir_fd=office_fd, create=True, label="office versions directory"
        )
        assert staging_fd is not None and versions_fd is not None
        os.fsync(office_fd)
        existing = _blob_stat(versions_fd, digest)
        if existing is not None:
            _verify_blob(store, versions_fd, digest, existing)
            return False
        temp_fd = os.open(temporary, _WRITE_FLAGS, 0o600, dir_fd=staging_fd)
        try:
            store._write_all(temp_fd, content)
            os.fsync(temp_fd)
        except BaseException:
            os.close(temp_fd)
            try:
                os.unlink(temporary, dir_fd=staging_fd)
            except FileNotFoundError:
                pass
            raise
        else:
            os.close(temp_fd)
        try:
            os.link(
                temporary,
                digest,
                src_dir_fd=staging_fd,
                dst_dir_fd=versions_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            _verify_blob(store, versions_fd, digest, _blob_stat(versions_fd, digest))
            return False
        claimed = True
        os.fsync(versions_fd)
        os.fsync(office_fd)
        os.fsync(ocu_fd)
        os.fsync(root_fd)
        os.fsync(base_fd)
        return True
    except BaseException:
        if claimed and versions_fd is not None:
            try:
                os.unlink(digest, dir_fd=versions_fd)
            except FileNotFoundError:
                pass
        raise
    finally:
        if staging_fd is not None:
            try:
                os.unlink(temporary, dir_fd=staging_fd)
            except FileNotFoundError:
                pass
            os.close(staging_fd)
        if versions_fd is not None:
            os.close(versions_fd)
        os.close(office_fd)
        os.close(ocu_fd)
        os.close(root_fd)
        os.close(base_fd)


def _unlink_blob(store: OfficeStore, chat: str, digest: str) -> None:
    opened = store._open_tree(chat, create=False)
    if opened is None:
        return
    base_fd, root_fd, ocu_fd, office_fd = opened
    try:
        versions_fd = store._open_dir(
            "versions", dir_fd=office_fd, create=False, label="office versions directory"
        )
        if versions_fd is None:
            return
        try:
            os.unlink(digest, dir_fd=versions_fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(versions_fd)
    finally:
        os.close(office_fd)
        os.close(ocu_fd)
        os.close(root_fd)
        os.close(base_fd)


def _blob_stat(versions_fd: int, digest: str) -> os.stat_result | None:
    try:
        return os.lstat(digest, dir_fd=versions_fd)
    except FileNotFoundError:
        return None
    except OSError as extra:
        raise StateCorruptError("version blob is unreadable") from extra


def _verify_blob(store: OfficeStore, versions_fd: int, digest: str, info: os.stat_result | None) -> None:
    if info is None:
        raise StateCorruptError("version blob is missing after an exclusive claim")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise StateCorruptError("version blob is not a regular file")
    try:
        blob_fd = os.open(digest, _FILE_FLAGS, dir_fd=versions_fd)
    except OSError as extra:
        raise StateCorruptError("version blob is unreadable") from extra
    try:
        opened = os.fstat(blob_fd)
        if not stat.S_ISREG(opened.st_mode):
            raise StateCorruptError("version blob is not a regular file")
        body = store._read_all(blob_fd)
    except OSError as extra:
        raise StateCorruptError("version blob is unreadable") from extra
    finally:
        os.close(blob_fd)
    if hashlib.sha256(body).hexdigest() != digest:
        raise StateCorruptError("version blob does not hash to its name")


def _document_versions(state: dict[str, Any], file_id: str) -> list[dict[str, Any]]:
    documents = state["documents"]
    if not isinstance(documents, dict):
        raise StateCorruptError("office documents collection is invalid")
    if file_id not in documents:
        document = {"versions": []}
        documents[file_id] = document
        return document["versions"]
    document = documents[file_id]
    if not isinstance(document, dict):
        raise StateCorruptError(f"office document {file_id} is invalid")
    if "versions" not in document:
        document["versions"] = []
        return document["versions"]
    listed = document["versions"]
    if not isinstance(listed, list):
        raise StateCorruptError(f"office document {file_id} versions are invalid")
    validated: list[dict[str, Any]] = []
    expected = 1
    for record in listed:
        item = _persisted_version(record)
        if item["number"] != expected:
            raise StateCorruptError(f"office document {file_id} version numbers are not consecutive")
        expected += 1
        validated.append(item)
    document["versions"] = validated
    return validated


def _persisted_version(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict) or any(name not in record for name in _VERSION_FIELDS):
        raise StateCorruptError("office version record is invalid")
    number = record["number"]
    parent = record["parent"]
    digest = record["sha256"]
    size = record["size"]
    source = record["source"]
    created_at = record["created_at"]
    published = record["published"]
    if type(number) is not int or number < 1:
        raise StateCorruptError("office version number is invalid")
    if parent is not None and (type(parent) is not int or parent < 1 or parent >= number):
        raise StateCorruptError("office version parent is invalid")
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise StateCorruptError("office version hash is invalid")
    if type(size) is not int or size < 0:
        raise StateCorruptError("office version size is invalid")
    if source not in VERSION_SOURCES:
        raise StateCorruptError("office version source is invalid")
    if not isinstance(created_at, str) or not created_at:
        raise StateCorruptError("office version created_at is invalid")
    if type(published) is not bool:
        raise StateCorruptError("office version published flag is invalid")
    return record


def _session_receipts(state: dict[str, Any], session_id: str, *, create: bool) -> dict[str, Any] | None:
    receipts = state["receipts"]
    if not isinstance(receipts, dict):
        raise StateCorruptError("office receipts collection is invalid")
    if session_id not in receipts:
        if not create:
            return None
        slot = {}
        receipts[session_id] = slot
        return slot
    slot = receipts[session_id]
    if not isinstance(slot, dict):
        raise StateCorruptError(f"office receipts for session {session_id} are invalid")
    return slot


def _put_receipt(state: dict[str, Any], session_id: str, save_seq: int, value: dict[str, Any]) -> None:
    slot = _session_receipts(state, session_id, create=True)
    assert slot is not None
    key = str(save_seq)
    if key in slot:
        existing = _persisted_receipt(slot[key])
        if existing != value:
            raise ValueError(
                f"conflicting save receipt for session {session_id} sequence {save_seq}"
            )
        return
    slot[key] = dict(value)


def _persisted_receipt(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict) or any(name not in record for name in _RECEIPT_VALUE_FIELDS):
        raise StateCorruptError("office save receipt is invalid")
    status = record["status"]
    digest = record["sha256"]
    number = record["version"]
    if type(status) is not int:
        raise StateCorruptError("office save receipt status is invalid")
    if digest is not None and (not isinstance(digest, str) or not _HASH.fullmatch(digest)):
        raise StateCorruptError("office save receipt hash is invalid")
    if number is not None and (type(number) is not int or number < 1):
        raise StateCorruptError("office save receipt version is invalid")
    return {
        "status": status,
        "sha256": digest,
        "version": number,
        "answer": record["answer"],
    }


def _caller_receipt(receipt: Any) -> dict[str, Any]:
    if not isinstance(receipt, dict):
        raise ValueError("save receipt must be a mapping")
    missing = [name for name in ("session_id", "save_seq", *_RECEIPT_VALUE_FIELDS) if name not in receipt]
    if missing:
        raise ValueError(f"save receipt is missing {missing[0]}")
    session_id = _require_session_id(receipt["session_id"])
    save_seq = _require_save_seq(receipt["save_seq"])
    value = _receipt_value({name: receipt[name] for name in _RECEIPT_VALUE_FIELDS})
    return {"session_id": session_id, "save_seq": save_seq, "value": value}


def _bind_receipt(spec: dict[str, Any], number: int, digest: str) -> dict[str, Any]:
    value = dict(spec["value"])
    if value["version"] is None:
        value["version"] = number
    elif value["version"] != number:
        raise ValueError("save receipt version does not match the stored version")
    if value["sha256"] is None:
        value["sha256"] = digest
    elif value["sha256"] != digest:
        raise ValueError("save receipt hash does not match the stored content")
    return {
        "session_id": spec["session_id"],
        "save_seq": spec["save_seq"],
        "value": value,
    }


def _receipt_value(receipt: Any) -> dict[str, Any]:
    if not isinstance(receipt, dict) or any(name not in receipt for name in _RECEIPT_VALUE_FIELDS):
        raise ValueError("save receipt must include status, sha256, version and answer")
    status = receipt["status"]
    digest = receipt["sha256"]
    number = receipt["version"]
    if type(status) is not int:
        raise ValueError("save receipt status must be an integer")
    if digest is not None and (not isinstance(digest, str) or not _HASH.fullmatch(digest)):
        raise ValueError("save receipt hash must be a SHA-256 hex digest or None")
    if number is not None and (type(number) is not int or number < 1):
        raise ValueError("save receipt version must be a positive integer or None")
    return {
        "status": status,
        "sha256": digest,
        "version": number,
        "answer": receipt["answer"],
    }


def _require_parent(versions: list[dict[str, Any]], parent: int | None, next_number: int) -> None:
    if next_number == 1:
        if parent is not None:
            raise ValueError("the first version parent must be None")
        return
    existing = {record["number"] for record in versions}
    if parent not in existing:
        raise ValueError("version parent must name an existing prior version")
