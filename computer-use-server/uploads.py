# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""
Shared helpers for publishing, listing and reading chat uploads.

Used by:
  - GET /api/uploads/{chat_id}/list (existing HTTP endpoint).
  - sync_chat_resources / the @mcp.resource handler in mcp_resources.py
    (Tier 6 native MCP surface).

Read traversal protection reuses security.safe_path / security.sanitize_chat_id.
"""

import datetime
import json
import mimetypes
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from security import safe_path, sanitize_chat_id


# Module-level so tests can patch / so app.py re-uses the same value.
BASE_DATA_DIR = Path(os.getenv("BASE_DATA_DIR", "/data"))


def claim_file_no_replace(temporary: Path, requested: Path) -> Path:
    """Hard-link complete bytes to the first free name; never follow a leaf.

    The caller owns parent-path validation, locking and temporary-file cleanup.
    Both paths must be on the same filesystem. Unlocked writers can win a name;
    EEXIST advances to the next candidate without modifying the occupied entry.
    """
    candidate = requested
    number = 2
    while True:
        try:
            os.link(temporary, candidate)
            return candidate
        except FileExistsError:
            candidate = requested.with_name(
                f"{requested.stem} ({number}){requested.suffix}"
            )
            number += 1


@dataclass(frozen=True)
class UploadEntry:
    name: str          # basename — display label
    rel_path: str      # relative to the uploads dir; may contain "/"
    size: int
    modified: float    # st_mtime
    mime_type: str


def _guess_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    return mime or "application/octet-stream"


def list_chat_uploads(chat_id: str) -> list[UploadEntry]:
    """List files under BASE_DATA_DIR/{chat_id}/uploads/ recursively.

    Returns [] if the directory doesn't exist (newly-created chat).
    Sorted by modification time, newest first — matches the HTTP endpoint's
    existing behavior (app.py:404).
    """
    chat_id = sanitize_chat_id(chat_id)
    uploads_dir = safe_path(BASE_DATA_DIR, chat_id, "uploads")
    if not uploads_dir.exists():
        return []
    entries: list[UploadEntry] = []
    for fp in uploads_dir.rglob("*"):
        if not fp.is_file():
            continue
        rel = fp.relative_to(uploads_dir)
        st = fp.stat()
        entries.append(UploadEntry(
            name=fp.name,
            rel_path=str(rel),
            size=st.st_size,
            modified=st.st_mtime,
            mime_type=_guess_mime(fp),
        ))
    entries.sort(key=lambda e: e.modified, reverse=True)
    return entries


def read_chat_upload(chat_id: str, rel_path: str) -> tuple[bytes, str]:
    """Read a single uploaded file. Returns (bytes, mime_type).

    rel_path is whatever list_chat_uploads reported (may contain "/").
    safe_path enforces traversal protection — no `..`, no absolute paths.
    """
    chat_id = sanitize_chat_id(chat_id)
    uploads_dir = safe_path(BASE_DATA_DIR, chat_id, "uploads")
    # safe_path handles multi-segment join with traversal protection.
    file_path = safe_path(uploads_dir, rel_path)
    if not file_path.is_file():
        raise FileNotFoundError(f"No such upload: {chat_id}/{rel_path}")
    return file_path.read_bytes(), _guess_mime(file_path)


class CorruptReceiptsError(RuntimeError):
    """Persisted import receipts are malformed and must not be treated as empty."""


def import_receipt(stored_name: str, size: int, md5: str) -> dict[str, Any]:
    return {
        "imported_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "md5": md5,
        "size": size,
        "stored_name": stored_name,
    }


def _lstat_or_none(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _require_control_directory(control: Path, *, create: bool) -> os.stat_result | None:
    info = _lstat_or_none(control)
    if info is None:
        if not create:
            return None
        try:
            os.mkdir(control, 0o700)
        except FileExistsError:
            pass
        info = _lstat_or_none(control)
        if info is None:
            raise CorruptReceiptsError("import receipts are corrupt")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise CorruptReceiptsError("import receipts are corrupt")
    return info


def read_import_receipts(chat_dir: Path) -> dict[str, dict[str, Any]]:
    control = chat_dir / ".ocu"
    if _require_control_directory(control, create=False) is None:
        return {}
    path = control / "imports.json"
    info = _lstat_or_none(path)
    if info is None:
        return {}
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise CorruptReceiptsError("import receipts are corrupt")
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorruptReceiptsError("import receipts are corrupt") from exc
    return _validate_receipts(loaded)


def write_import_receipts(chat_dir: Path, receipts: dict[str, dict[str, Any]]) -> None:
    encoded = json.dumps(
        _validate_receipts(receipts), ensure_ascii=False,
        separators=(",", ":"), sort_keys=True).encode("utf-8")
    control = chat_dir / ".ocu"
    _require_control_directory(control, create=True)
    destination = control / "imports.json"
    existing = _lstat_or_none(destination)
    if existing is not None and (
            stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)):
        raise CorruptReceiptsError("import receipts are corrupt")
    temporary = control / f".imports.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    committed = False
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        fd = os.open(temporary, flags, 0o600)
        try:
            view = memoryview(encoded)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, destination)
        committed = True
        dir_fd = os.open(control, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                         | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        if not committed:
            temporary.unlink(missing_ok=True)
        raise


def _validate_receipts(loaded: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(loaded, dict):
        raise CorruptReceiptsError("import receipts are corrupt")
    validated: dict[str, dict[str, Any]] = {}
    for attachment_id, record in loaded.items():
        if not isinstance(attachment_id, str) or not attachment_id or not isinstance(record, dict):
            raise CorruptReceiptsError("import receipts are corrupt")
        stored_name, imported_at = record.get("stored_name"), record.get("imported_at")
        size, md5 = record.get("size"), record.get("md5")
        if (not isinstance(stored_name, str) or not stored_name
                or not isinstance(imported_at, str) or not imported_at
                or not isinstance(size, int) or isinstance(size, bool) or size < 0
                or not isinstance(md5, str) or not md5):
            raise CorruptReceiptsError("import receipts are corrupt")
        validated[attachment_id] = {
            "imported_at": imported_at, "md5": md5, "size": size,
            "stored_name": stored_name,
        }
    return validated
