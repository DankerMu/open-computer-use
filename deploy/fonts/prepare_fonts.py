#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Prepare the release's pinned fonts without committing font binaries."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tarfile
import tempfile
import urllib.request
from urllib.parse import urlsplit
import zipfile


class FontError(ValueError):
    pass


def _size(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _digest(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _relative(name) -> bool:
    return (
        isinstance(name, str) and bool(name) and "\\" not in name and "\x00" not in name
        and not name.startswith("/") and all(part not in ("", ".", "..") for part in name.split("/"))
    )


def load_pin(path: Path) -> list[dict]:
    if path.is_symlink() or not path.is_file():
        raise FontError(f"{path}: font pin is not a regular file")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"archives"}:
        raise FontError(f"{path}: font pin must contain archives")
    archives = data["archives"]
    if not isinstance(archives, list) or not archives:
        raise FontError(f"{path}: font archives must be a nonempty list")
    names = set()
    for archive in archives:
        if not isinstance(archive, dict) or set(archive) != {"url", "sha256", "size", "files"}:
            raise FontError(f"{path}: invalid font archive fields")
        url = archive["url"]
        if not isinstance(url, str) or urlsplit(url).scheme not in ("http", "https"):
            raise FontError(f"{path}: font archive requires an HTTP(S) URL")
        if not _digest(archive["sha256"]) or not _size(archive["size"]):
            raise FontError(f"{path}: invalid font archive SHA-256 or size")
        files = archive["files"]
        if not isinstance(files, list) or not files:
            raise FontError(f"{path}: font files must be a nonempty list")
        members = set()
        for item in files:
            if not isinstance(item, dict) or set(item) != {"member", "name", "sha256", "size"}:
                raise FontError(f"{path}: invalid pinned font fields")
            name, member = item["name"], item["member"]
            if not _relative(name) or PurePosixPath(name).name != name or not _relative(member):
                raise FontError(f"{path}: unsafe font name or archive member")
            if name in names or member in members:
                raise FontError(f"{path}: duplicate font name or archive member")
            if not _digest(item["sha256"]) or not _size(item["size"]):
                raise FontError(f"{path}: invalid font SHA-256 or size")
            names.add(name)
            members.add(member)
    return archives


def _copy_verified(source, target, item: dict, label: str) -> None:
    digest = hashlib.sha256()
    count = 0
    while True:
        chunk = source.read(min(65536, item["size"] - count + 1))
        if not chunk:
            break
        count += len(chunk)
        if count > item["size"]:
            raise FontError(f"{label}: size exceeds pin")
        digest.update(chunk)
        if target is not None:
            target.write(chunk)
    if count != item["size"]:
        raise FontError(f"{label}: size differs from pin")
    if digest.hexdigest() != item["sha256"]:
        raise FontError(f"{label}: SHA-256 differs from pin")


def pinned_files(archives: list[dict]) -> dict[str, dict]:
    return {item["name"]: item for archive in archives for item in archive["files"]}


def prepare_fonts(pin: Path, output: Path) -> None:
    archives = load_pin(pin)
    if output.exists() or output.is_symlink():
        raise FontError(f"{output}: destination already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ocu-fonts-", dir=output.parent) as temporary:
        root = Path(temporary)
        files = root / "files"
        files.mkdir()
        for index, record in enumerate(archives):
            downloaded = root / f"archive-{index}.zip"
            with urllib.request.urlopen(record["url"], timeout=30) as response, downloaded.open("xb") as target:
                _copy_verified(response, target, record, "font archive")
            with zipfile.ZipFile(downloaded) as archive:
                members = archive.infolist()
                for item in record["files"]:
                    matches = [entry for entry in members if entry.filename == item["member"]]
                    if len(matches) != 1:
                        raise FontError(f"{item['member']}: missing or duplicate font member")
                    member = matches[0]
                    kind = stat.S_IFMT(member.external_attr >> 16)
                    if member.is_dir() or kind not in (0, stat.S_IFREG):
                        raise FontError(f"{item['member']}: font member is not regular")
                    if member.file_size != item["size"]:
                        raise FontError(f"{item['member']}: font member size differs from pin")
                    with archive.open(member) as source, (files / item["name"]).open("xb") as target:
                        _copy_verified(source, target, item, item["name"])
        staged = root / "fonts.tar"
        with tarfile.open(staged, "w", format=tarfile.USTAR_FORMAT) as bundle:
            for name, item in sorted(pinned_files(archives).items()):
                member = tarfile.TarInfo(name)
                member.size = item["size"]
                member.mode = 0o644
                with (files / name).open("rb") as source:
                    bundle.addfile(member, source)
        os.link(staged, output, follow_symlinks=False)


def verify_bundle(path: Path, archives: list[dict], *, destination: Path | None = None) -> None:
    expected = pinned_files(archives)
    seen = set()
    if destination is not None:
        destination.mkdir(mode=0o755)
        # A restrictive importer umask must not block the renderer's font mount.
        destination.chmod(0o755)
    with tarfile.open(path, "r:") as bundle:
        for member in bundle:
            if not member.isfile() or member.name not in expected or member.name in seen:
                raise FontError(f"{path}: unexpected, duplicate or non-regular font member {member.name!r}")
            item = expected[member.name]
            if member.size != item["size"]:
                raise FontError(f"{path}: font member size differs from pin")
            with bundle.extractfile(member) as source:
                if destination is None:
                    _copy_verified(source, None, item, f"{path}: {member.name}")
                else:
                    target = destination / member.name
                    with target.open("xb") as stream:
                        _copy_verified(source, stream, item, f"{path}: {member.name}")
                    # The read-only mount must remain readable by the non-root renderer.
                    target.chmod(0o644)
            seen.add(member.name)
    if seen != set(expected):
        raise FontError(f"{path}: missing pinned font files")


def verify_directory(directory: Path, archives: list[dict]) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise FontError(f"{directory}: fonts must be a real directory")
    expected = pinned_files(archives)
    if {path.name for path in directory.iterdir()} != set(expected):
        raise FontError(f"{directory}: font directory membership differs from pin")
    for name, item in expected.items():
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise FontError(f"{directory}: font {name} is not regular")
        with path.open("rb") as source:
            _copy_verified(source, None, item, str(path))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pin", type=Path, default=Path(__file__).with_name("fonts.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        prepare_fonts(args.pin, args.output)
    except (OSError, ValueError, zipfile.BadZipFile, tarfile.TarError) as exc:
        print(f"fonts: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
