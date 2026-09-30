#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Prepare pinned Draw.io viewer materials from a verified source archive."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

PINNED_COMMIT = "0f419a92c769adb5fb20f2b18053a5ae8c7e4993"
PINNED_ARCHIVE_URL = f"https://codeload.github.com/jgraph/drawio/tar.gz/{PINNED_COMMIT}"
PINNED_ARCHIVE_SHA256 = "42a3f9b9cbf2ae1a95f1c4a642996e2d96ee689e54a0e77430bae69975d09487"
PINNED_VIEWER_SHA256 = "41f8360963bb485db74517ae7ca8ca01e563b587607a82238f901750b14e26d0"
ARCHIVE_ROOT = f"drawio-{PINNED_COMMIT}/"
WEBAPP_PREFIX = "src/main/webapp/"
VIEWER_PATH = "js/viewer-static.min.js"
ALLOWED_FILES = ("LICENSE", VIEWER_PATH)
ALLOWED_DIRS = (
    "stencils/",
    "shapes/",
    "img/",
    "images/",
    "mxgraph/",
    "math4/",
    "styles/",
)
HEX_LEN = 64


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_allowed(rel: str) -> bool:
    if rel in ALLOWED_FILES:
        return True
    return any(rel.startswith(prefix) for prefix in ALLOWED_DIRS)


def validate_relative_path(rel: str) -> None:
    if not isinstance(rel, str) or not rel or rel.startswith(("/", "\\")):
        raise ValueError(f"Unsafe inventory path {rel}")
    if Path(rel).is_absolute():
        raise ValueError(f"Unsafe inventory path {rel}")
    parts = [part for part in rel.replace("\\", "/").split("/")]
    if not parts or any(part == "" for part in parts):
        raise ValueError(f"Unsafe inventory path {rel}")
    for part in parts:
        if part in (".", "..") or "/" in part or "\\" in part or "\0" in part:
            raise ValueError(f"Unsafe inventory path {rel}")


def archive_member_dest(member_name: str) -> str | None:
    name = member_name[2:] if member_name.startswith("./") else member_name
    if name.startswith(("/", "\\")) or Path(name).is_absolute():
        raise ValueError(f"Unsafe inventory path {member_name}")
    if not name.startswith(ARCHIVE_ROOT):
        return None
    inner = name[len(ARCHIVE_ROOT) :]
    if inner == "LICENSE":
        return "LICENSE"
    if inner.startswith(WEBAPP_PREFIX):
        rel = inner[len(WEBAPP_PREFIX) :]
        return rel or None
    return None


def load_inventory(raw: str | dict) -> dict[str, dict]:
    data = json.loads(raw) if isinstance(raw, str) else raw
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list) or not files:
        raise ValueError("Drawio inventory is empty")
    inventory: dict[str, dict] = {}
    for entry in files:
        path = entry.get("path") if isinstance(entry, dict) else None
        digest = entry.get("sha256") if isinstance(entry, dict) else None
        size = entry.get("size") if isinstance(entry, dict) else None
        if not path or not isinstance(digest, str) or not isinstance(size, int) or size < 0:
            raise ValueError("Drawio inventory entry is incomplete")
        try:
            digest_bytes = bytes.fromhex(digest)
        except ValueError as exc:
            raise ValueError("Drawio inventory entry is incomplete") from exc
        if len(digest) != HEX_LEN or len(digest_bytes) != 32:
            raise ValueError("Drawio inventory entry is incomplete")
        validate_relative_path(path)
        if not is_allowed(path):
            raise ValueError(f"Inventory path outside closure: {path}")
        if path in inventory:
            raise ValueError(f"Duplicate inventory path {path}")
        inventory[path] = {"path": path, "sha256": digest.lower(), "size": size}
    viewer = inventory.get(VIEWER_PATH)
    if not viewer or viewer["sha256"] != PINNED_VIEWER_SHA256:
        raise ValueError("Inventory viewer hash does not match the pinned release")
    if "LICENSE" not in inventory:
        raise ValueError("Missing upstream LICENSE")
    return inventory


def verify_archive(data: bytes) -> bytes:
    if sha256_bytes(data) != PINNED_ARCHIVE_SHA256:
        raise ValueError("Drawio archive hash mismatch")
    return data


def extract_allowed_members(archive_bytes: bytes, inventory: dict[str, dict]) -> dict[str, bytes]:
    extracted: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        for member in archive.getmembers():
            rel = archive_member_dest(member.name)
            if not rel or member.isdir():
                continue
            if not is_allowed(rel):
                continue
            validate_relative_path(rel)
            if member.issym() or member.islnk() or not member.isfile():
                raise ValueError(f"Rejected archive member type {member.type}: {rel}")
            expected = inventory.get(rel)
            if expected is None:
                raise ValueError(f"Unexpected archive member {rel}")
            if rel in extracted:
                raise ValueError(f"Duplicate archive member {rel}")
            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError(f"Unable to read archive member {rel}")
            data = handle.read()
            if len(data) != expected["size"] or sha256_bytes(data) != expected["sha256"]:
                raise ValueError(f"Archive member hash mismatch {rel}")
            extracted[rel] = data
    if len(extracted) != len(inventory):
        raise ValueError(f"Incomplete extraction: {len(extracted)} of {len(inventory)}")
    return extracted


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.partial-{os.getpid()}-{os.urandom(4).hex()}")
    tmp.write_bytes(data)
    tmp.replace(path)


def publish_bundle(stage_dir: Path, dest_dir: Path) -> None:
    stage_dir = Path(stage_dir)
    dest_dir = Path(dest_dir)
    if not stage_dir.exists():
        raise FileNotFoundError(stage_dir)
    lock_path = dest_dir.with_name(dest_dir.name + ".publish.lock")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError as exc:
        raise RuntimeError(f"publication already in progress for {dest_dir}") from exc
    stamp = f"{os.getpid()}-{os.urandom(4).hex()}"
    staged = dest_dir.with_name(f"{dest_dir.name}.next-{stamp}")
    backup = dest_dir.with_name(f"{dest_dir.name}.prev-{stamp}")
    committed = False
    try:
        os.close(fd)
        stage_dir.replace(staged)
        if dest_dir.exists():
            dest_dir.replace(backup)
        staged.replace(dest_dir)
        committed = True
        if backup.exists():
            try:
                shutil.rmtree(backup)
            except Exception as cleanup_error:
                raise RuntimeError(
                    f"published {dest_dir}; residual backup remains at {backup}"
                ) from cleanup_error
    except BaseException as error:
        if not committed:
            failures = [error]
            if backup.exists():
                try:
                    if dest_dir.exists():
                        shutil.rmtree(dest_dir)
                    backup.replace(dest_dir)
                except Exception as restore_error:
                    wrapped = RuntimeError(
                        f"publication rollback failed; prior bundle remains at {backup}"
                    )
                    wrapped.__cause__ = restore_error
                    failures.append(wrapped)
            if staged.exists():
                shutil.rmtree(staged, ignore_errors=True)
            if len(failures) > 1:
                raise BaseExceptionGroup("publication failed", failures) from error
        raise
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def _workspace_dirs(root: Path) -> tuple[Path, Path]:
    if root.name == "computer-use-server":
        run_dir = root.parent / ".run"
        return run_dir, run_dir / "drawio-cache"
    tmp = Path("/tmp")
    return tmp, tmp / "ocu-drawio-cache"


def prepare_drawio(
    *,
    root: Path | None = None,
    dest_dir: Path | None = None,
    cache_dir: Path | None = None,
    inventory_path: Path | None = None,
    opener=None,
) -> dict:
    root = Path(root or Path(__file__).resolve().parent.parent)
    dest_dir = Path(dest_dir or root / "static" / "drawio")
    run_dir, default_cache = _workspace_dirs(root)
    cache_dir = Path(cache_dir or os.environ.get("OCU_DRAWIO_CACHE", default_cache))
    inventory_path = Path(inventory_path or Path(__file__).with_name("inventory.json"))
    inventory = load_inventory(inventory_path.read_text(encoding="utf-8"))
    run_dir.mkdir(parents=True, exist_ok=True)
    stage_dir = Path(tempfile.mkdtemp(prefix="drawio-stage-", dir=run_dir))
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached = cache_dir / f"{PINNED_COMMIT}.tar.gz"
        try:
            archive = verify_archive(cached.read_bytes())
        except Exception:
            fetch = opener or urllib.request.urlopen
            with fetch(PINNED_ARCHIVE_URL) as response:
                archive = verify_archive(response.read())
            _write_atomic(cached, archive)
        members = extract_allowed_members(archive, inventory)
        for rel, data in members.items():
            target = stage_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        notice = [
            "Draw.io viewer materials",
            f"Upstream commit {PINNED_COMMIT}",
            f"Archive SHA256 {PINNED_ARCHIVE_SHA256}",
            f"Viewer SHA256 {PINNED_VIEWER_SHA256}",
            "Licenses: LICENSE, img/LICENSE, shapes/LICENSE, stencils/LICENSE",
            "",
        ]
        (stage_dir / "NOTICE").write_text("\n".join(notice), encoding="utf-8")
        for entry in inventory.values():
            digest = sha256_bytes((stage_dir / entry["path"]).read_bytes())
            if digest != entry["sha256"]:
                raise ValueError(f"Staged hash mismatch {entry['path']}")
        publish_bundle(stage_dir, dest_dir)
        return {"destDir": str(dest_dir), "files": len(inventory)}
    except BaseException:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        result = prepare_drawio()
    except Exception as error:
        print(error, file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "destination": result["destDir"], "files": result["files"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
