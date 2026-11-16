# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Load and verify CC0 music candidates, their evidence and the listening decisions."""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import urllib.request
from urllib.parse import unquote, urlsplit
import zipfile

MOODS = ("calm-curious", "upbeat-corporate", "neutral-serious", "loop")
AUDIO_SUFFIXES = (".mp3", ".ogg", ".wav", ".flac")
DECISIONS = ("approve", "drop")
EVIDENCE_DIR = "deploy/music/evidence/"
CANDIDATE_FIELDS = {"id", "mood", "loop", "title", "author", "licence_url", "source", "evidence", "rerelease_note"}
SOURCE_FIELDS = {"url", "sha256", "size"}
MEMBER_FIELDS = {"name", "sha256", "size"}
LIVE_FIELDS = {"kind", "snapshot", "captured", "capture_url"}
ARCHIVED_FIELDS = LIVE_FIELDS | {"wayback_capture", "wayback_sha1"}
WAYBACK_CAPTURE = re.compile(r"http://web\.archive\.org/web/[0-9]{14}id_/https?://\S+")


class MusicError(ValueError):
    pass


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _size(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _digest(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _url(value) -> bool:
    return (
        isinstance(value, str) and re.fullmatch(r"\S+", value) is not None
        and urlsplit(value).scheme in ("http", "https") and bool(urlsplit(value).netloc)
    )


def _date(value) -> bool:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _relative(name) -> bool:
    return (
        isinstance(name, str) and bool(name) and "\\" not in name and "\x00" not in name
        and not name.startswith("/") and all(part not in ("", ".", "..") for part in name.split("/"))
    )


def _json_object(path: Path, label: str) -> dict:
    if path.is_symlink() or not path.is_file():
        raise MusicError(f"{path}: {label} is not a regular file")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise MusicError(f"{path}: {label} must be a JSON object")
    return data


def audio_suffix(candidate: dict) -> str:
    """The extension of the audio file the page serves: the member's, or the URL path's."""
    source = candidate["source"]
    name = source["member"]["name"] if "member" in source else unquote(urlsplit(source["url"]).path)
    return PurePosixPath(name).suffix.lower()


def _check_source(source, label: str) -> None:
    if not isinstance(source, dict) or set(source) not in (SOURCE_FIELDS, SOURCE_FIELDS | {"member"}):
        raise MusicError(f"{label}: invalid source fields")
    if not _url(source["url"]) or not _digest(source["sha256"]) or not _size(source["size"]):
        raise MusicError(f"{label}: invalid source URL, SHA-256 or size")
    if "member" in source:
        member = source["member"]
        if not isinstance(member, dict) or set(member) != MEMBER_FIELDS:
            raise MusicError(f"{label}: invalid source member fields")
        if not _relative(member["name"]) or not _digest(member["sha256"]) or not _size(member["size"]):
            raise MusicError(f"{label}: invalid source member name, SHA-256 or size")


def _check_evidence(evidence, root: Path, label: str) -> None:
    if not isinstance(evidence, dict) or evidence.get("kind") not in ("live", "archived"):
        raise MusicError(f"{label}: evidence kind must be live or archived")
    expected = LIVE_FIELDS if evidence["kind"] == "live" else ARCHIVED_FIELDS
    if set(evidence) != expected:
        raise MusicError(f"{label}: invalid {evidence['kind']} evidence fields")
    snapshot = evidence["snapshot"]
    name = snapshot[len(EVIDENCE_DIR):] if isinstance(snapshot, str) and snapshot.startswith(EVIDENCE_DIR) else None
    if name is None or not _relative(name) or "/" in name:
        raise MusicError(f"{label}: evidence snapshot must be a file directly under {EVIDENCE_DIR}")
    path = root / snapshot
    if path.is_symlink() or not path.is_file():
        raise MusicError(f"{label}: evidence snapshot {snapshot} is not a regular file")
    if not _date(evidence["captured"]) or not _url(evidence["capture_url"]):
        raise MusicError(f"{label}: invalid evidence capture date or URL")
    if evidence["kind"] == "archived":
        if not isinstance(evidence["wayback_capture"], str) or not WAYBACK_CAPTURE.fullmatch(evidence["wayback_capture"]):
            raise MusicError(f"{label}: wayback_capture must be http://web.archive.org/web/<ts>id_/<original URL>")
        if not isinstance(evidence["wayback_sha1"], str) or not re.fullmatch(r"[A-Z2-7]{32}", evidence["wayback_sha1"]):
            raise MusicError(f"{label}: wayback_sha1 must be 32 base32 characters")


def load_candidates(path: Path, root: Path) -> list[dict]:
    """Validate candidates.json; evidence snapshots resolve against the repository root."""
    data = _json_object(path, "music candidates")
    if set(data) != {"version", "candidates"} or data["version"] != 1:
        raise MusicError(f"{path}: candidates must be version 1 with candidates")
    candidates = data["candidates"]
    if not isinstance(candidates, list) or not candidates:
        raise MusicError(f"{path}: candidates must be a nonempty list")
    ids = set()
    for index, item in enumerate(candidates):
        label = f"{path}: candidate {index}"
        if not isinstance(item, dict) or set(item) != CANDIDATE_FIELDS:
            raise MusicError(f"{label}: invalid candidate fields")
        if not isinstance(item["id"], str) or not re.fullmatch(r"[a-z0-9-]+", item["id"]):
            raise MusicError(f"{label}: id must match ^[a-z0-9-]+$")
        label = f"{path}: {item['id']}"
        if item["id"] in ids:
            raise MusicError(f"{label}: duplicate candidate id")
        ids.add(item["id"])
        if item["mood"] not in MOODS:
            raise MusicError(f"{label}: mood must be one of {', '.join(MOODS)}")
        if not isinstance(item["loop"], bool) or item["loop"] != (item["mood"] == "loop"):
            raise MusicError(f"{label}: loop must be true exactly for the loop mood")
        if not all(_text(item[field]) for field in ("title", "author", "rerelease_note")):
            raise MusicError(f"{label}: title, author and rerelease_note must be nonempty text")
        if not _url(item["licence_url"]):
            raise MusicError(f"{label}: invalid licence URL")
        _check_source(item["source"], label)
        if audio_suffix(item) not in AUDIO_SUFFIXES:
            raise MusicError(f"{label}: audio must be one of {', '.join(AUDIO_SUFFIXES)}")
        _check_evidence(item["evidence"], root, label)
    return candidates


def load_decisions(path: Path, candidates: list[dict]) -> dict[str, str]:
    """Validate a listening-page export: exactly one approve or drop per candidate."""
    data = _json_object(path, "music decisions")
    if set(data) != {"version", "reviewer", "date", "decisions"} or data["version"] != 1:
        raise MusicError(f"{path}: decisions must be version 1 with reviewer, date and decisions")
    if not _text(data["reviewer"]) or not _date(data["date"]):
        raise MusicError(f"{path}: invalid reviewer or date")
    if not isinstance(data["decisions"], list):
        raise MusicError(f"{path}: decisions must be a list")
    known = [item["id"] for item in candidates]
    result = {}
    for entry in data["decisions"]:
        if not isinstance(entry, dict) or set(entry) != {"id", "decision"} or not isinstance(entry["id"], str):
            raise MusicError(f"{path}: invalid decision fields")
        identifier = entry["id"]
        if entry["decision"] not in DECISIONS:
            raise MusicError(f"{path}: {identifier}: decision must be approve or drop")
        if identifier not in known:
            raise MusicError(f"{path}: unknown candidate id {identifier!r}")
        if identifier in result:
            raise MusicError(f"{path}: duplicate decision for {identifier}")
        result[identifier] = entry["decision"]
    missing = [identifier for identifier in known if identifier not in result]
    if missing:
        raise MusicError(f"{path}: missing decisions for {', '.join(missing)}")
    return result


def _copy_verified(source, target, size: int, sha256: str, label: str) -> str:
    """Stream at most size bytes; return the base32 SHA-1 of what was read."""
    digest, sha1, count = hashlib.sha256(), hashlib.sha1(), 0
    while True:
        chunk = source.read(min(65536, size - count + 1))
        if not chunk:
            break
        count += len(chunk)
        if count > size:
            raise MusicError(f"{label}: size exceeds pin")
        digest.update(chunk)
        sha1.update(chunk)
        target.write(chunk)
    if count != size:
        raise MusicError(f"{label}: size differs from pin")
    if digest.hexdigest() != sha256:
        raise MusicError(f"{label}: SHA-256 differs from pin")
    return base64.b32encode(sha1.digest()).decode("ascii")


def fetch_candidate(candidate: dict, directory: Path) -> Path:
    """Download one verified source into directory as <id>.<ext>; zips yield only their member."""
    label, source = candidate["id"], candidate["source"]
    target = directory / f"{label}{audio_suffix(candidate)}"
    downloaded = directory / f"{label}.download" if "member" in source else target
    with urllib.request.urlopen(source["url"], timeout=60) as response, downloaded.open("xb") as stream:
        wayback = _copy_verified(response, stream, source["size"], source["sha256"], label)
    evidence = candidate["evidence"]
    if evidence["kind"] == "archived" and wayback != evidence["wayback_sha1"]:
        raise MusicError(f"{label}: Wayback SHA-1 differs from the source bytes ({wayback})")
    if "member" in source:
        pinned = source["member"]
        with zipfile.ZipFile(downloaded) as archive:
            matches = [entry for entry in archive.infolist() if entry.filename == pinned["name"]]
            if len(matches) != 1:
                raise MusicError(f"{label}: missing or duplicate member {pinned['name']}")
            member = matches[0]
            if member.is_dir() or stat.S_IFMT(member.external_attr >> 16) not in (0, stat.S_IFREG):
                raise MusicError(f"{label}: member {pinned['name']} is not a regular file")
            with archive.open(member) as stream, target.open("xb") as output:
                _copy_verified(stream, output, pinned["size"], pinned["sha256"], f"{label}: member")
        downloaded.unlink()
    return target
