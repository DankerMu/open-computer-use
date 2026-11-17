#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Fetch the approved music sources, normalize them to -16 LUFS and write the library manifest."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

# Isolated mode (python -I) leaves the script directory off sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import music_candidates  # noqa: E402
from music_candidates import MOODS, MusicError  # noqa: E402

TARGET_PER_MOOD = 4
TARGET_LUFS = -16.0
TOLERANCE_LU = 0.5
PEAK_CEILING_DBTP = -1.5
FFMPEG_TIMEOUT_SECONDS = 600
EXCLUSION_FIELDS = {"id", "reason", "measured", "decided_by", "date"}
MEASURED_FIELDS = {"integrated_lufs", "true_peak_dbtp", "loudness_range_lu"}


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def load_exclusions(path: Path, approved: list[dict]) -> list[dict]:
    """Validate library-excluded.json: approved tracks the user excluded after measurement, once each."""
    data = music_candidates._json_object(path, "library exclusions")
    if set(data) != {"version", "excluded"} or data["version"] != 1 or not isinstance(data["excluded"], list):
        raise MusicError(f"{path}: exclusions must be version 1 with an excluded list")
    approved_ids, seen = {item["id"] for item in approved}, set()
    for entry in data["excluded"]:
        if not isinstance(entry, dict) or set(entry) != EXCLUSION_FIELDS or not isinstance(entry["id"], str):
            raise MusicError(f"{path}: invalid exclusion fields")
        label = f"{path}: {entry['id']}"
        if entry["id"] not in approved_ids:
            raise MusicError(f"{label}: excluded id is not an approved candidate")
        if entry["id"] in seen:
            raise MusicError(f"{label}: duplicate exclusion")
        seen.add(entry["id"])
        measured = entry["measured"]
        if not isinstance(measured, dict) or set(measured) != MEASURED_FIELDS \
                or not all(_number(value) for value in measured.values()):
            raise MusicError(f"{label}: invalid measured fields")
        if entry["decided_by"] != "user":
            raise MusicError(f"{label}: decided_by must be user")
        if not music_candidates._text(entry["reason"]) or not music_candidates._date(entry["date"]):
            raise MusicError(f"{label}: invalid reason or date")
    return data["excluded"]


def approved_library(
    candidates_path: Path, approvals_path: Path, excluded_path: Path, root: Path,
) -> tuple[list[dict], dict, list[dict]]:
    """The approved, not excluded candidates in candidates.json order, the manifest's approvals block and the exclusions."""
    candidates = music_candidates.load_candidates(candidates_path, root)
    decisions = music_candidates.load_decisions(approvals_path, candidates)
    data = approvals_path.read_bytes()
    export = json.loads(data)
    approvals = {"sha256": hashlib.sha256(data).hexdigest(), "reviewer": export["reviewer"], "date": export["date"]}
    approved = [item for item in candidates if decisions[item["id"]] == "approve"]
    excluded = load_exclusions(excluded_path, approved)
    excluded_ids = {entry["id"] for entry in excluded}
    return [item for item in approved if item["id"] not in excluded_ids], approvals, excluded


@contextmanager
def _staged(output: Path, prefix: str):
    """Yield a temporary sibling of output; rename it into place only when the body succeeds."""
    if output.exists() or output.is_symlink():
        raise MusicError(f"{output}: destination already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=prefix, dir=output.parent) as temporary:
        staged = Path(temporary) / "library"
        staged.mkdir()
        yield staged
        if output.exists() or output.is_symlink():
            raise MusicError(f"{output}: destination already exists")
        os.rename(staged, output)


def fetch(candidates_path: Path, approvals_path: Path, excluded_path: Path, root: Path, output: Path) -> list[dict]:
    tracks, _approvals, _excluded = approved_library(candidates_path, approvals_path, excluded_path, root)
    with _staged(output, ".music-sources-") as staged:
        for item in tracks:
            try:
                music_candidates.fetch_candidate(item, staged)
            except (OSError, http.client.HTTPException, zipfile.BadZipFile) as exc:
                raise MusicError(f"{item['id']}: download failed: {exc}") from exc
    return tracks


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_source(item: dict, sources: Path) -> Path:
    """The fetched file of one track, checked against its pin: the member's for zip tracks."""
    label = item["id"]
    path = sources / f"{label}{music_candidates.audio_suffix(item)}"
    if path.is_symlink() or not path.is_file():
        raise MusicError(f"{label}: source file {path} is missing")
    kind, pin = ("member", item["source"]["member"]) if "member" in item["source"] else ("source", item["source"])
    if path.stat().st_size != pin["size"]:
        raise MusicError(f"{label}: {kind} size differs from pin")
    if _file_sha256(path) != pin["sha256"]:
        raise MusicError(f"{label}: {kind} SHA-256 differs from pin")
    return path


def _run(label: str, command: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(command, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS)
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1:] or ["no output"]
        raise MusicError(f"{label}: {Path(command[0]).name} failed: {detail[0]}")
    return result


def _measure(label: str, path: Path) -> dict:
    """Integrated loudness, loudness range and true peak from ffmpeg's ebur128 summary."""
    result = _run(label, [
        "ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-i", str(path), "-map", "0:a:0",
        # Per-frame lines go to the verbose level; the summary stays at info.
        "-af", "ebur128=peak=true:framelog=verbose", "-f", "null", "-",
    ])
    summary = result.stderr.rsplit("Summary:", 1)[-1] if "Summary:" in result.stderr else ""
    values = {}
    for key, pattern in (("I", r"Integrated loudness:\s+I:\s+(-?[0-9.]+|-inf) LUFS"),
                         ("LRA", r"Loudness range:\s+LRA:\s+(-?[0-9.]+) LU"),
                         ("TP", r"True peak:\s+Peak:\s+(-?[0-9.]+|-inf) dBFS")):
        match = re.search(pattern, summary)
        if match is None:
            raise MusicError(f"{label}: no {key} in the ebur128 summary of {path.name}")
        values[key] = float(match.group(1))
    return values


def _probe(label: str, path: Path) -> dict:
    result = _run(label, [
        "ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
        "stream=sample_rate:format=duration", "-of", "json", str(path),
    ])
    data = json.loads(result.stdout)
    if not data.get("streams"):
        raise MusicError(f"{label}: {path.name} has no audio stream")
    return {"sample_rate": int(data["streams"][0]["sample_rate"]),
            "duration_seconds": round(float(data["format"]["duration"]), 3)}


def _encode(source: Path, target: Path, gain: float, loop: bool, sample_rate: int) -> None:
    """The one encoding call: a constant linear gain, stereo, at the source's sample rate."""
    codec = ["-c:a", "pcm_s16le"] if loop else ["-c:a", "libmp3lame", "-b:a", "320k"]
    _run(target.stem, [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(source),
        "-map", "0:a:0", "-vn", "-map_metadata", "-1", "-af", f"volume={gain:.4f}dB",
        "-ac", "2", "-ar", str(sample_rate), *codec, str(target),
    ])


def _normalize(item: dict, source: Path, staged: Path) -> dict:
    label = item["id"]
    measured = _measure(label, source)
    # One constant gain, capped so the true peak stays at the ceiling: no limiter, no compressor, no loudnorm.
    needed = TARGET_LUFS - measured["I"]
    gain = min(needed, PEAK_CEILING_DBTP - measured["TP"])
    predicted = measured["I"] + gain
    # ebur128 reports one decimal; rounding keeps float noise from deciding a track at the tolerance edge.
    if round(predicted, 1) < TARGET_LUFS - TOLERANCE_LU:
        raise MusicError(
            f"{label}: infeasible gain: I {measured['I']:.1f} LUFS, TP {measured['TP']:.1f} dBTP,"
            f" LRA {measured['LRA']:.1f} LU; gain {needed:+.1f} dB would put the true peak at"
            f" {measured['TP'] + needed:.1f} dBTP, and the {PEAK_CEILING_DBTP} dBTP cap leaves it at"
            f" {predicted:.1f} LUFS, outside {TARGET_LUFS} ± {TOLERANCE_LU} LUFS"
        )
    target = staged / f"{label}.{'wav' if item['loop'] else 'mp3'}"
    _encode(source, target, gain, item["loop"], _probe(label, source)["sample_rate"])
    shipped = _measure(label, target)
    if round(abs(shipped["I"] - TARGET_LUFS), 1) > TOLERANCE_LU or shipped["TP"] > PEAK_CEILING_DBTP:
        raise MusicError(
            f"{label}: output out of tolerance: I {shipped['I']:.1f} LUFS, TP {shipped['TP']:.1f} dBTP;"
            f" needs {TARGET_LUFS} ± {TOLERANCE_LU} LUFS and at most {PEAK_CEILING_DBTP} dBTP"
        )
    print(f"music: {label}: source I {measured['I']:.1f} LUFS, TP {measured['TP']:.1f} dBTP,"
          f" LRA {measured['LRA']:.1f} LU, needed gain {needed:+.2f} dB, applied gain {gain:+.2f} dB;"
          f" output I {shipped['I']:.1f} LUFS, TP {shipped['TP']:.1f} dBTP")
    return {"file": target.name, "format": target.suffix[1:], **_probe(label, target),
            "integrated_lufs": shipped["I"], "true_peak_dbtp": shipped["TP"]}


def build(
    candidates_path: Path, approvals_path: Path, excluded_path: Path, root: Path, sources: Path, output: Path,
) -> dict:
    tracks, approvals, _excluded = approved_library(candidates_path, approvals_path, excluded_path, root)
    if output.exists() or output.is_symlink():
        raise MusicError(f"{output}: destination already exists")
    # Every pin is checked before any track is decoded.
    verified = [(item, _verify_source(item, sources)) for item in tracks]
    with _staged(output, ".music-library-") as staged:
        (staged / "evidence").mkdir()
        entries = []
        for item, source in verified:
            shipped = _normalize(item, source, staged)
            snapshot = root / item["evidence"]["snapshot"]
            evidence = staged / "evidence" / snapshot.name
            if not evidence.exists():
                shutil.copyfile(snapshot, evidence)
            pin = item["source"]
            entry = {
                "id": item["id"], "mood": item["mood"], "loop": item["loop"],
                "title": item["title"], "author": item["author"],
                **{key: shipped[key] for key in ("file", "format", "sample_rate", "duration_seconds")},
                "source_url": pin["url"], "licence_url": item["licence_url"],
                "evidence": f"evidence/{snapshot.name}",
                # For a zip track, the zip's digest; fetch verified it and kept only the member.
                "source_sha256": pin["sha256"],
            }
            if "member" in pin:
                entry["member"] = {"name": pin["member"]["name"], "sha256": pin["member"]["sha256"]}
            entry["shipped_sha256"] = _file_sha256(staged / shipped["file"])
            entry.update({
                "integrated_lufs": shipped["integrated_lufs"], "true_peak_dbtp": shipped["true_peak_dbtp"],
                "rerelease_note": item["rerelease_note"],
                "approver": approvals["reviewer"], "approval_date": approvals["date"],
            })
            entries.append(entry)
        data = approvals_path.read_bytes()
        if hashlib.sha256(data).hexdigest() != approvals["sha256"]:
            raise MusicError(f"{approvals_path}: changed during the build")
        (staged / "approvals.json").write_bytes(data)
        manifest = {"version": 1, "approvals": {"file": "approvals.json", **approvals}, "tracks": entries}
        (staged / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                                              encoding="utf-8")
    return manifest


def report(candidates_path: Path, approvals_path: Path, excluded_path: Path, root: Path) -> list[str]:
    tracks, approvals, excluded = approved_library(candidates_path, approvals_path, excluded_path, root)
    lines = [f"music: shipped per mood against the target of {TARGET_PER_MOOD}"
             f" (approved by {approvals['reviewer']}, {approvals['date']}):"]
    for mood in MOODS:
        count = sum(item["mood"] == mood for item in tracks)
        shortfall = f" (shortfall {TARGET_PER_MOOD - count})" if count < TARGET_PER_MOOD else ""
        lines.append(f"{mood} {count}/{TARGET_PER_MOOD}{shortfall}")
    for entry in excluded:
        measured = entry["measured"]
        lines.append(
            f"excluded {entry['id']} (I {measured['integrated_lufs']} LUFS, TP {measured['true_peak_dbtp']} dBTP,"
            f" LRA {measured['loudness_range_lu']} LU; {entry['decided_by']}, {entry['date']}): {entry['reason']}"
        )
    return lines


def main() -> int:
    here = Path(__file__).resolve().parent
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--candidates", type=Path, default=here / "candidates.json")
    common.add_argument("--approvals", type=Path, default=here / "approvals.json")
    common.add_argument("--excluded", type=Path, default=here / "library-excluded.json")
    common.add_argument("--root", type=Path, default=here.parents[1], help="repository root for evidence paths")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch_parser = commands.add_parser("fetch", parents=[common], help="download the approved sources")
    fetch_parser.add_argument("--out", type=Path, required=True)
    build_parser = commands.add_parser("build", parents=[common], help="normalize the fetched sources")
    build_parser.add_argument("--sources", type=Path, required=True)
    build_parser.add_argument("--out", type=Path, required=True)
    commands.add_parser("report", parents=[common], help="print the approved count per mood")
    args = parser.parse_args()
    try:
        if args.command == "fetch":
            tracks = fetch(args.candidates, args.approvals, args.excluded, args.root, args.out)
            print(f"music: fetched {len(tracks)} approved sources into {args.out}")
        elif args.command == "build":
            manifest = build(args.candidates, args.approvals, args.excluded, args.root, args.sources, args.out)
            print(f"music: built {len(manifest['tracks'])} tracks into {args.out}")
        else:
            print("\n".join(report(args.candidates, args.approvals, args.excluded, args.root)))
    except (OSError, ValueError) as exc:
        print(f"music: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
