#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Confined filesystem capture and extraction for recovery archives."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import tarfile

from recovery import RecoveryError, sha256_file


ARCHIVE_FORMAT = "ocu-recovery-fs/v1"
MAX_ARCHIVE_MEMBERS = 100_000
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024 * 1024
PAX_ACL_ACCESS = "SCHILY.acl.access"
PAX_ACL_DEFAULT = "SCHILY.acl.default"
PAX_XATTR_PREFIX = "SCHILY.xattr."
GNU_TAR = ("tar",)
CAPTURE_FLAGS = (
    "--format=pax",
    "--numeric-owner",
    "--acls",
    "--xattrs",
    "--xattrs-include=user.*",
    "--xattrs-include=security.selinux",
    "--no-recursion",
    "-czf",
)
EXTRACT_FLAGS = (
    "--numeric-owner",
    "--acls",
    "--xattrs",
    "--xattrs-include=user.*",
    "--xattrs-include=security.selinux",
    "--no-overwrite-dir",
    "-xzf",
)


def fail(message: str) -> None:
    raise RecoveryError(message)


def confined_relative(root: Path, path: Path) -> str:
    root_text = str(root)
    path_text = str(path)
    if path_text == root_text:
        return "."
    prefix = root_text.rstrip("/") + "/"
    if not path_text.startswith(prefix):
        fail(f"{path}: escapes {root}")
    text = path_text[len(prefix) :]
    if text.startswith("/") or ".." in Path(text).parts or "\x00" in text:
        fail(f"{path}: unsafe relative path")
    return text


def canonical_member_name(name: str) -> str:
    text = name.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    if text in ("", ".", "./"):
        return "."
    if text.startswith("/") or ".." in Path(text).parts or "\x00" in text:
        fail(f"unsafe archive member {name}")
    return text.rstrip("/") or "."


def _lstat(path: Path):
    try:
        return path.lstat()
    except OSError as exc:
        raise RecoveryError(f"cannot inspect {path}: {exc}") from exc


def _is_unsupported_type(mode: int) -> bool:
    return (
        stat.S_ISCHR(mode)
        or stat.S_ISBLK(mode)
        or stat.S_ISFIFO(mode)
        or stat.S_ISSOCK(mode)
    )


def _gnu_tar() -> bool:
    result = subprocess.run(
        [*GNU_TAR, "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and "GNU tar" in (result.stdout or "")


def _run_tar(argv: list[str], *, cwd: Path | None = None) -> None:
    result = subprocess.run(
        [*GNU_TAR, *argv],
        cwd=None if cwd is None else str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RecoveryError(f"gnu tar failed: {detail}")


def _read_xattrs(path: Path) -> dict[str, bytes]:
    listxattr = getattr(os, "listxattr", None)
    getxattr = getattr(os, "getxattr", None)
    if listxattr is None or getxattr is None:
        return {}
    try:
        names = listxattr(path, follow_symlinks=False)
    except OSError:
        return {}
    values = {}
    for name in names:
        if name.startswith("system.posix_acl"):
            fail(f"{path}: POSIX ACL present but GNU tar --acls is unavailable")
        if not (name.startswith("user.") or name == "security.selinux"):
            fail(f"{path}: unsupported extended attribute {name}")
        values[name] = getxattr(path, name, follow_symlinks=False)
    return values


def _pax_headers(path: Path) -> dict[str, str]:
    headers = {}
    for name, value in _read_xattrs(path).items():
        headers[PAX_XATTR_PREFIX + name] = value.decode("utf-8", "surrogateescape")
    return headers


def _python_capture(root: Path, archive: Path, rows: list[tuple[Path, str, os.stat_result]]) -> None:
    inode_names: dict[tuple[int, int], str] = {}
    with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT, dereference=False) as bundle:
        for path, relative, info in rows:
            member = tarfile.TarInfo(name=relative)
            member.uid = info.st_uid
            member.gid = info.st_gid
            member.mtime = int(info.st_mtime)
            member.mode = stat.S_IMODE(info.st_mode)
            member.pax_headers = _pax_headers(path)
            key = (info.st_dev, info.st_ino)
            if (
                info.st_nlink > 1
                and not stat.S_ISDIR(info.st_mode)
                and not stat.S_ISLNK(info.st_mode)
                and key in inode_names
            ):
                member.type = tarfile.LNKTYPE
                member.linkname = inode_names[key]
                member.size = 0
                bundle.addfile(member)
                continue
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(path)
                if target.startswith("/") or ".." in Path(target).parts:
                    fail(f"{path}: absolute or parent symlink target is unsupported")
                member.type = tarfile.SYMTYPE
                member.linkname = target
                member.size = 0
                bundle.addfile(member)
            elif stat.S_ISDIR(info.st_mode):
                member.type = tarfile.DIRTYPE
                member.size = 0
                bundle.addfile(member)
            elif stat.S_ISREG(info.st_mode):
                member.type = tarfile.REGTYPE
                member.size = info.st_size
                with open(path, "rb") as stream:
                    bundle.addfile(member, stream)
                if info.st_nlink > 1:
                    inode_names[key] = relative
            else:
                fail(f"{path}: unsupported file type")


def _apply_metadata(path: Path, member: tarfile.TarInfo) -> None:
    headers = member.pax_headers or {}
    setxattr = getattr(os, "setxattr", None)
    for key, value in headers.items():
        if not key.startswith(PAX_XATTR_PREFIX):
            continue
        name = key[len(PAX_XATTR_PREFIX) :]
        if setxattr is None:
            fail(f"{path}: extended attributes are unsupported on this platform")
        setxattr(path, name, value.encode("utf-8", "surrogateescape"), follow_symlinks=False)
    if (PAX_ACL_ACCESS in headers or PAX_ACL_DEFAULT in headers) and not _gnu_tar():
        fail(f"{path}: POSIX ACL restore requires GNU tar --acls")
    try:
        os.lchown(path, int(member.uid), int(member.gid))
    except OSError:
        pass
    if not member.issym():
        os.chmod(path, member.mode & 0o7777)
        os.utime(path, (member.mtime, member.mtime), follow_symlinks=False)


def _python_extract(archive: Path, dest: Path, members: list[tarfile.TarInfo]) -> None:
    with tarfile.open(archive, "r:*") as bundle:
        for member in members:
            name = canonical_member_name(member.name)
            target = dest if name == "." else dest / name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.issym():
                target.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(member.linkname, target)
            elif member.islnk():
                source = dest / canonical_member_name(member.linkname)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.link(source, target)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted = bundle.extractfile(member)
                if extracted is None:
                    fail(f"{archive}: missing file content for {name}")
                with open(target, "wb") as stream:
                    stream.write(extracted.read())
            else:
                fail(f"{archive}: unsupported member {name}")
            _apply_metadata(target, member)

def _walk_tree(root: Path) -> list[tuple[Path, str, os.stat_result]]:
    stack = [root]
    rows: list[tuple[Path, str, os.stat_result]] = []
    while stack:
        current = stack.pop()
        info = _lstat(current)
        relative = "." if current == root else confined_relative(root, current)
        if _is_unsupported_type(info.st_mode):
            fail(f"{current}: unsupported file type")
        rows.append((current, relative, info))
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            try:
                names = sorted(os.listdir(current), reverse=True)
            except OSError as cop:
                raise RecoveryError(f"cannot list {current}: {cop}") from cop
            for name in names:
                stack.append(current / name)
    return rows


def capture_tree(root: Path, archive: Path) -> dict:
    if not root.exists():
        fail(f"{root}: missing")
    root_info = _lstat(root)
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        fail(f"{root}: capture root must be a directory")
    archive.parent.mkdir(parents=True, exist_ok=True)
    rows = _walk_tree(root)
    if len(rows) > MAX_ARCHIVE_MEMBERS:
        fail(f"{root}: archive member limit exceeded")
    names = [relative for _path, relative, _info in rows]
    seen: set[str] = set()
    for name in names:
        if name in seen:
            fail(f"{root}: duplicate member {name}")
        seen.add(name)
    if _gnu_tar():
        listing = archive.parent / (archive.name + ".list")
        listing.write_text("\n".join(names) + "\n", encoding="utf-8")
        try:
            _run_tar(
                [
                    *CAPTURE_FLAGS,
                    str(archive),
                    f"--directory={root}",
                    f"--files-from={listing}",
                ]
            )
        finally:
            listing.unlink(missing_ok=True)
    else:
        _python_capture(root, archive, rows)
    members = validate_archive(archive)
    return {
        "format": ARCHIVE_FORMAT,
        "root": str(root),
        "sha256": sha256_file(archive),
        "members": members,
    }


def _member_kind(member: tarfile.TarInfo) -> str:
    if member.isdir():
        return "directory"
    if member.issym():
        return "symlink"
    if member.islnk():
        return "hardlink"
    if member.isfile():
        return "file"
    return "unsupported"


def inspect_archive(archive: Path) -> list[tarfile.TarInfo]:
    if not archive.exists():
        fail(f"{archive}: missing")
    try:
        with tarfile.open(archive, "r:*") as bundle:
            members = bundle.getmembers()
    except tarfile.TarError as cop:
        raise RecoveryError(f"{archive}: unreadable recovery archive: {cop}") from cop
    if len(members) > MAX_ARCHIVE_MEMBERS:
        fail(f"{archive}: archive member limit exceeded")
    names: dict[str, tarfile.TarInfo] = {}
    total = 0
    for member in members:
        name = canonical_member_name(member.name)
        if name in names:
            fail(f"{archive}: duplicate member {name}")
        names[name] = member
        kind = _member_kind(member)
        if kind == "unsupported" or member.isdev() or member.isfifo():
            fail(f"{archive}: unsupported member type {member.name}")
        if member.issym() and (
            member.linkname.startswith("/") or ".." in Path(member.linkname).parts
        ):
            fail(f"{archive}: unsafe symlink {member.name}")
        if member.islnk() and (
            member.linkname.startswith("/") or ".." in Path(member.linkname).parts
        ):
            fail(f"{archive}: unsafe hardlink {member.name}")
        headers = member.pax_headers or {}
        for key in headers:
            if key.startswith(PAX_XATTR_PREFIX):
                attr = key[len(PAX_XATTR_PREFIX) :]
                if not (
                    attr.startswith("user.")
                    or attr == "security.selinux"
                    or attr.startswith("SCHILY.acl.")
                ):
                    if attr.startswith("system.posix_acl"):
                        continue
                    fail(f"{archive}: unsupported extended attribute {attr}")
        total += max(member.size, 0)
        if total > MAX_ARCHIVE_BYTES:
            fail(f"{archive}: archive size limit exceeded")
    _reject_link_escapes(names)
    return members


def _reject_link_escapes(names: dict[str, tarfile.TarInfo]) -> None:
    def parent_chain(name: str) -> list[str]:
        parts = [] if name == "." else name.split("/")
        chain = ["."]
        current = []
        for part in parts[:-1]:
            current.append(part)
            chain.append("/".join(current))
        return chain

    for name, member in names.items():
        for ancestor in parent_chain(name):
            parent = names.get(ancestor)
            if parent is None:
                continue
            if parent.issym() or parent.islnk():
                fail(f"archive member {name} traverses link {ancestor}")
        if member.islnk():
            target = canonical_member_name(member.linkname)
            if target not in names:
                fail(f"hardlink {name} target is missing")
            if names[target].issym():
                fail(f"hardlink {name} may not target a symlink")


def validate_archive(archive: Path) -> list[dict]:
    members = inspect_archive(archive)
    records = []
    for member in members:
        records.append(
            {
                "name": canonical_member_name(member.name),
                "type": _member_kind(member),
                "mode": member.mode,
                "uid": member.uid,
                "gid": member.gid,
                "mtime": int(member.mtime),
                "linkname": member.linkname or "",
                "size": member.size,
                "acl": bool(
                    (member.pax_headers or {}).get(PAX_ACL_ACCESS)
                    or (member.pax_headers or {}).get(PAX_ACL_DEFAULT)
                ),
                "xattrs": sorted(
                    key[len(PAX_XATTR_PREFIX) :]
                    for key in (member.pax_headers or {})
                    if key.startswith(PAX_XATTR_PREFIX)
                ),
            }
        )
    return records


def _destination_empty(dest: Path) -> None:
    try:
        info = dest.lstat()
    except FileNotFoundError:
        dest.mkdir(parents=True, exist_ok=False)
        os.chmod(dest, 0o700)
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        fail(f"{dest}: restore destination must be an empty directory")
    try:
        entries = list(os.scandir(dest))
    except OSError as cop:
        raise RecoveryError(f"cannot inspect destination {dest}: {cop}") from cop
    if entries:
        fail(f"{dest}: restore destination is not empty")


def extract_tree(archive: Path, dest: Path) -> None:
    members = inspect_archive(archive)
    _destination_empty(dest)
    if _gnu_tar():
        _run_tar([*EXTRACT_FLAGS, str(archive), "-C", str(dest)])
    else:
        _python_extract(archive, dest, members)
    restored = {relative for _path, relative, _info in _walk_tree(dest)}
    expected = {record["name"] for record in validate_archive(archive)}
    if not expected.issubset(restored):
        fail(f"{archive}: extraction omitted members")
