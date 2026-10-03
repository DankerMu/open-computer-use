# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""
Shared helpers for publishing, listing and reading chat workspace files.

Used by:
  - POST /api/uploads/{chat_id}/{path} (publication via claim_file_no_replace).
  - sync_chat_resources / the @mcp.resource handler in mcp_resources.py
    (native MCP surface over workspace files).

List and read walk BASE_DATA_DIR/{chat_id}/outputs through O_NOFOLLOW
descriptors from the chat root. Hidden names and any path under .ocu are
skipped from MCP discovery. Upload staging lives in the server-private
.ocu control directory, outside the sandbox bind.
"""

import errno
import datetime
import json
import mimetypes
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from security import sanitize_chat_id


# Module-level so tests can patch / so app.py re-uses the same value.
BASE_DATA_DIR = Path(os.getenv("BASE_DATA_DIR", "/data"))


_NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CLOSE_ON_EXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | _DIRECTORY | _NO_FOLLOW | _CLOSE_ON_EXEC
_FILE_FLAGS = os.O_RDONLY | _NO_FOLLOW | _NONBLOCK | _CLOSE_ON_EXEC
_STAGING_FLAGS = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NO_FOLLOW | _CLOSE_ON_EXEC
)
_WORKSPACE_DIR_MODE = 0o777
_CONTROL_DIR_MODE = 0o700
_TRAVERSAL_DENIED = "Access denied: path traversal detected"


def _is_nofollow_error(error: OSError) -> bool:
    return error.errno in (errno.ELOOP, errno.EMLINK)


def _is_missing(error: OSError) -> bool:
    return error.errno in (errno.ENOENT, errno.ENOTDIR)


def _deny_traversal(error: OSError | None = None) -> HTTPException:
    exception = HTTPException(status_code=403, detail=_TRAVERSAL_DENIED)
    if error is not None:
        exception.__cause__ = error
    return exception


def close_fd(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def open_directory_path(path: Path) -> int:
    try:
        directory_fd = os.open(path, _DIRECTORY_FLAGS)
    except OSError as exc:
        if _is_nofollow_error(exc) or exc.errno == errno.ENOTDIR:
            raise _deny_traversal(exc)
        raise
    try:
        if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
            raise _deny_traversal()
        return directory_fd
    except BaseException:
        close_fd(directory_fd)
        raise



def _open_child_directory(parent_fd: int, name: str) -> int:
    try:
        child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        if _is_nofollow_error(exc) or exc.errno == errno.ENOTDIR:
            raise _deny_traversal(exc)
        raise
    try:
        if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
            raise _deny_traversal()
        return child_fd
    except BaseException:
        close_fd(child_fd)
        raise


def _lexical_parts(relative: Path) -> tuple[str, ...]:
    if relative == Path("."):
        return ()
    parts = relative.parts
    if any(part in {"", ".", ".."} for part in parts):
        raise _deny_traversal()
    return parts


def ensure_workspace_directories(root_fd: int, parts: tuple[str, ...]) -> int:
    """Create missing directories under an opened root; return an owned fd.

    ``root_fd`` is borrowed and left open. The returned descriptor is owned
    by the caller and must be closed. Empty ``parts`` duplicates ``root_fd``.
    Newly created components receive mode 0777 through the opened directory
    fd; existing directory modes are left unchanged. Each mkdir and open is
    relative to the current ancestor fd with O_NOFOLLOW, so a swapped
    intermediate symlink is never followed.
    """
    if not parts:
        return os.dup(root_fd)
    current_fd = os.dup(root_fd)
    try:
        for part in parts:
            created = False
            try:
                os.mkdir(part, _WORKSPACE_DIR_MODE, dir_fd=current_fd)
                created = True
            except FileExistsError:
                pass
            except OSError as exc:
                if _is_nofollow_error(exc) or exc.errno == errno.ENOTDIR:
                    raise _deny_traversal(exc)
                raise
            next_fd = _open_child_directory(current_fd, part)
            try:
                if created:
                    os.fchmod(next_fd, _WORKSPACE_DIR_MODE)
            except BaseException:
                close_fd(next_fd)
                raise
            close_fd(current_fd)
            current_fd = next_fd
        owned = current_fd
        current_fd = None
        return owned
    finally:
        close_fd(current_fd)


def claim_file_no_replace(
    temporary: str,
    *,
    src_dir_fd: int,
    dst_dir_fd: int,
    requested_name: str,
) -> str:
    """Hard-link complete bytes to the first free name; never follow a leaf.

    Both directory fds are borrowed. ``temporary`` is a name relative to
    ``src_dir_fd``; ``requested_name`` is a basename relative to
    ``dst_dir_fd``. Occupied names, including symlinks, advance to
    ``stem (N).suffix`` without modifying the occupied entry. Both
    directories must be on the same filesystem; EXDEV is not retried.
    """
    requested = Path(requested_name)
    candidate = requested.name
    number = 2
    while True:
        try:
            os.link(
                temporary,
                candidate,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
                follow_symlinks=False,
            )
            return candidate
        except FileExistsError:
            candidate = f"{requested.stem} ({number}){requested.suffix}"
            number += 1
        except OSError as exc:
            if _is_nofollow_error(exc) or exc.errno == errno.ENOTDIR:
                raise _deny_traversal(exc)
            raise


def open_control_directory(chat_fd: int) -> int:
    """Open ``.ocu`` relative to the chat root, creating it at mode 0700.

    ``chat_fd`` is borrowed. The returned descriptor is owned by the caller.
    A swapped ``.ocu`` symlink is rejected rather than followed.
    """
    created = False
    try:
        os.mkdir(".ocu", _CONTROL_DIR_MODE, dir_fd=chat_fd)
        created = True
    except FileExistsError:
        pass
    except OSError as exc:
        if _is_nofollow_error(exc) or exc.errno == errno.ENOTDIR:
            raise _deny_traversal(exc)
        raise
    control_fd = _open_child_directory(chat_fd, ".ocu")
    try:
        if created:
            os.fchmod(control_fd, _CONTROL_DIR_MODE)
        return control_fd
    except BaseException:
        close_fd(control_fd)
        raise


def stage_upload_bytes(control_fd: int, content: bytes) -> tuple[int, str]:
    """Write complete bytes into a private staging file under ``control_fd``.

    Returns ``(staging_fd, staging_name)``. The caller owns the fd and must
    unlink the name from ``control_fd`` after publication or on error.
    Mode is 0600 while writing and 0666 on the flushed fd before return,
    so the later hard link publishes a sandbox-writable inode.
    """
    staging_name = f".upload-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        staging_fd = os.open(
            staging_name, _STAGING_FLAGS, 0o600, dir_fd=control_fd,
        )
    except OSError as exc:
        if _is_nofollow_error(exc) or exc.errno == errno.ENOTDIR:
            raise _deny_traversal(exc)
        raise
    try:
        view = memoryview(content)
        while view:
            view = view[os.write(staging_fd, view):]
        os.fsync(staging_fd)
        os.fchmod(staging_fd, 0o666)
        return staging_fd, staging_name
    except BaseException:
        close_fd(staging_fd)
        try:
            os.unlink(staging_name, dir_fd=control_fd)
        except OSError:
            pass
        raise


def unlink_relative(name: str, dir_fd: int) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass


@dataclass(frozen=True)
class UploadEntry:
    name: str          # basename — display label
    rel_path: str      # relative to the workspace files dir; may contain "/"
    size: int
    modified: float    # st_mtime
    mime_type: str


def _guess_mime(name: str) -> str:
    mime, _ = mimetypes.guess_type(name)
    return mime or "application/octet-stream"


def _open_outputs_root(chat_id: str) -> int | None:
    chat_dir = BASE_DATA_DIR / chat_id
    try:
        chat_stat = os.lstat(chat_dir)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(chat_stat.st_mode) or not stat.S_ISDIR(chat_stat.st_mode):
        return None
    chat_fd = None
    try:
        chat_fd = os.open(chat_dir, _DIRECTORY_FLAGS)
        if not stat.S_ISDIR(os.fstat(chat_fd).st_mode):
            return None
        try:
            outputs_fd = os.open("outputs", _DIRECTORY_FLAGS, dir_fd=chat_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if _is_nofollow_error(exc) or _is_missing(exc):
                return None
            raise
        try:
            if not stat.S_ISDIR(os.fstat(outputs_fd).st_mode):
                close_fd(outputs_fd)
                return None
            return outputs_fd
        except BaseException:
            close_fd(outputs_fd)
            raise
    except OSError as exc:
        if _is_nofollow_error(exc) or _is_missing(exc):
            return None
        raise
    finally:
        close_fd(chat_fd)


def _scan_visible_files(root_fd: int) -> list[UploadEntry]:
    entries: list[UploadEntry] = []
    stack: list[tuple[int, tuple[str, ...], bool]] = [(root_fd, (), False)]
    try:
        while stack:
            directory_fd, prefix, owned = stack.pop()
            try:
                with os.scandir(directory_fd) as listing:
                    children = []
                    for entry in listing:
                        name = entry.name
                        if name.startswith("."):
                            continue
                        relative_path = "/".join((*prefix, name))
                        try:
                            item_stat = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        mode = item_stat.st_mode
                        if stat.S_ISLNK(mode):
                            continue
                        if stat.S_ISDIR(mode):
                            children.append(name)
                            continue
                        if not stat.S_ISREG(mode):
                            continue
                        entries.append(UploadEntry(
                            name=name,
                            rel_path=relative_path,
                            size=item_stat.st_size,
                            modified=item_stat.st_mtime,
                            mime_type=_guess_mime(name),
                        ))
                    for name in reversed(children):
                        try:
                            child_fd = os.open(
                                name, _DIRECTORY_FLAGS, dir_fd=directory_fd,
                            )
                        except OSError:
                            continue
                        if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
                            close_fd(child_fd)
                            continue
                        stack.append((child_fd, (*prefix, name), True))
            except OSError:
                pass
            finally:
                if owned:
                    close_fd(directory_fd)
    except BaseException:
        while stack:
            directory_fd, _prefix, owned = stack.pop()
            if owned:
                close_fd(directory_fd)
        raise
    entries.sort(key=lambda e: e.modified, reverse=True)
    return entries


def list_chat_uploads(chat_id: str) -> list[UploadEntry]:
    """List visible files under BASE_DATA_DIR/{chat_id}/outputs/ recursively.

    Returns [] if the directory doesn't exist (newly-created chat).
    Hidden names, hidden directory segments, and anything under .ocu are
    excluded. Symlinks and non-regular entries are skipped. Sorted by
    modification time, newest first.
    """
    chat_id = sanitize_chat_id(chat_id)
    outputs_fd = _open_outputs_root(chat_id)
    if outputs_fd is None:
        return []
    try:
        return _scan_visible_files(outputs_fd)
    finally:
        close_fd(outputs_fd)


def _open_regular_relative(root_fd: int, parts: tuple[str, ...]) -> int:
    if not parts:
        raise FileNotFoundError("No such upload")
    parent_fd = os.dup(root_fd)
    try:
        for component in parts[:-1]:
            next_fd = _open_child_directory(parent_fd, component)
            close_fd(parent_fd)
            parent_fd = next_fd
        try:
            file_fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            if _is_nofollow_error(exc) or exc.errno == errno.ENOTDIR:
                raise _deny_traversal(exc)
            if exc.errno in (errno.ENXIO, errno.EAGAIN, errno.EWOULDBLOCK):
                raise _deny_traversal(exc)
            if _is_missing(exc):
                raise FileNotFoundError("No such upload") from exc
            raise
        try:
            opened = os.fstat(file_fd)
            if not stat.S_ISREG(opened.st_mode):
                raise _deny_traversal()
            return file_fd
        except BaseException:
            close_fd(file_fd)
            raise
    finally:
        close_fd(parent_fd)


def _read_fd_bytes(file_fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(file_fd, 1024 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def read_chat_upload(chat_id: str, rel_path: str) -> tuple[bytes, str]:
    """Read a single workspace file. Returns (bytes, mime_type).

    ``rel_path`` is whatever list_chat_uploads reported (may contain "/").
    Ancestors and the leaf are opened with O_NOFOLLOW from the chat root;
    the bytes are read from the verified regular-file descriptor.
    """
    chat_id = sanitize_chat_id(chat_id)
    relative = Path(rel_path)
    if (not rel_path or relative.is_absolute() or ".." in relative.parts
            or relative.name in {"", ".", ".."} or "\x00" in rel_path):
        raise _deny_traversal()
    parts = _lexical_parts(relative)
    outputs_fd = _open_outputs_root(chat_id)
    if outputs_fd is None:
        raise FileNotFoundError(f"No such upload: {chat_id}/{rel_path}")
    try:
        file_fd = _open_regular_relative(outputs_fd, parts)
        try:
            return _read_fd_bytes(file_fd), _guess_mime(parts[-1])
        finally:
            close_fd(file_fd)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"No such upload: {chat_id}/{rel_path}") from exc
    finally:
        close_fd(outputs_fd)


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
