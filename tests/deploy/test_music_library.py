# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Only approved, pin-verified tracks ship, each normalized to -16 LUFS with a true peak at or below -1.5 dBTP."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest
from unittest import mock
import zipfile

from support import ROOT, tmp_dir
from tests.orchestrator._http_origin import HttpOriginLifetime, OriginRequestHandler

MUSIC_DIR = ROOT / "deploy" / "music"
if str(MUSIC_DIR) not in sys.path:
    sys.path.insert(0, str(MUSIC_DIR))

import music_library  # noqa: E402
from music_candidates import MOODS, MusicError  # noqa: E402

MUSIC_LIBRARY = MUSIC_DIR / "music_library.py"
FFMPEG = shutil.which("ffmpeg")
NEEDS_FFMPEG = unittest.skipIf(FFMPEG is None, "ffmpeg is not on PATH; the loudness tests need ffmpeg and ffprobe")
RATE = 44100
APPROVALS_SHA256 = "207fcbd170d914cb96135e30fb03a6a2e9b7fccde5873d0e6b96ab1b45c99b6b"
# Approved ids of deploy/music/approvals.json less the user's 2026-11-17 exclusions, in candidates.json order.
SHIPPED_IDS = [
    "inventing-flight", "from-page-to-practice", "arpent", "wisdom-in-the-sun",
    "inspiration", "beat-one", "spacetime", "great-mission",
    "snowfall-loop", "heavenly-loop", "simple-menu-loop",
]
# The user's decision of 2026-11-17 on the Phase 2 measurements of the real sources.
LIBRARY_EXCLUDED = {"version": 1, "excluded": [
    {"id": "infinite-wonder",
     "reason": "linear gain to -16 LUFS would put the true peak at -0.3 dBTP; the peak cap leaves it at -17.2 LUFS",
     "measured": {"integrated_lufs": -21.8, "true_peak_dbtp": -6.1, "loudness_range_lu": 8.5},
     "decided_by": "user", "date": "2026-11-17"},
    {"id": "space-ranger-loop",
     "reason": "peak cap leaves it at -17.1 LUFS; a loop takes no limiter",
     "measured": {"integrated_lufs": -15.7, "true_peak_dbtp": -0.1, "loudness_range_lu": 1.6},
     "decided_by": "user", "date": "2026-11-17"},
    {"id": "observing-the-star",
     "reason": "peak cap leaves it at -23.3 LUFS",
     "measured": {"integrated_lufs": -22.3, "true_peak_dbtp": -0.5, "loudness_range_lu": 10.5},
     "decided_by": "user", "date": "2026-11-17"},
    {"id": "and-just-like-that",
     "reason": "after 320 kb/s MP3 encoding no gain meets both limits: -1.9 dB gives -16.6 LUFS at -1.4 dBTP,"
               " -2.0 dB gives -16.7 LUFS at -1.6 dBTP",
     "measured": {"integrated_lufs": -14.6, "true_peak_dbtp": 0.4, "loudness_range_lu": 4.7},
     "decided_by": "user", "date": "2026-11-17"},
]}
DROPPED_IDS = [
    "painting-room", "advertime", "funshine", "pickled-pink", "goodnightmare", "motions",
    "my-very-own-dead-ship", "twists", "piano-loop",
]
OBSERVING_NOTE = (
    'Searched 2026-11-17 under the OpenGameArt page title: the iTunes Search API (term "Another space background'
    ' track", with and without "yd") found no track of that title or by that author, and MusicBrainz recording'
    ' searches for "Another space background track" and "Observing The Star" (any artist) returned 0 recordings.'
)
FIXTURE_REVIEWER, FIXTURE_DATE = "Fixture Reviewer", "2026-11-15"
MEMBER_NAME = "pack/Steady Theme.flac"
MANIFEST_KEYS = {"version", "approvals", "tracks"}
APPROVAL_KEYS = {"file", "sha256", "reviewer", "date"}
TRACK_TYPES = {
    "id": str, "mood": str, "loop": bool, "title": str, "author": str,
    "file": str, "format": str, "sample_rate": int, "duration_seconds": float,
    "source_url": str, "licence_url": str, "evidence": str,
    "source_sha256": str, "shipped_sha256": str,
    "integrated_lufs": float, "true_peak_dbtp": float,
    "rerelease_note": str, "approver": str, "approval_date": str,
}

# Fixture expressions: every track is modulated, so its loudness changes over its length.
QUIET = "0.03*sin(2*PI*330*t)*(0.6+0.4*sin(2*PI*0.25*t))|0.03*sin(2*PI*440*t)*(0.6+0.4*sin(2*PI*0.3*t))"
LOUD = "0.6*sin(2*PI*220*t)*(0.7+0.3*sin(2*PI*0.5*t))|0.6*sin(2*PI*277*t)*(0.7+0.3*sin(2*PI*0.4*t))"
LOOP = "0.1*sin(2*PI*262*t)*(0.6+0.4*sin(2*PI*0.2*t))"
# A quiet tone with a 1 ms near-full-scale click each second: low loudness, high true peak.
PEAKY = "0.005*sin(2*PI*440*t)*(0.6+0.4*sin(2*PI*0.25*t))+if(lt(mod(t,1),0.001),0.95,0)"
# Stereo, so the upmix does not lower the peak: linear gain to -16 LUFS overshoots the -1.5 dBTP cap by about 0.3 dB.
CAPPED_CHANNEL = "0.1*sin(2*PI*440*t)*(0.6+0.4*sin(2*PI*0.25*t))+if(lt(mod(t,1),0.001),0.27,0)"
CAPPED = f"{CAPPED_CHANNEL}|{CAPPED_CHANNEL}"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def render(directory: Path, name: str, expression: str, seconds: float, codec: list[str]) -> bytes:
    path = directory / name
    subprocess.run(
        [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", f"aevalsrc='{expression}':s={RATE}:d={seconds}", *codec, str(path)],
        check=True, timeout=60,
    )
    return path.read_bytes()


def measure(path: Path) -> tuple[float, float]:
    """The test's own EBU R128 reading: integrated loudness and true peak from ffmpeg's summary."""
    result = subprocess.run(
        [FFMPEG, "-nostdin", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128=peak=true",
         "-f", "null", "-"], capture_output=True, text=True, timeout=60, check=True,
    )
    summary = result.stderr.rsplit("Summary:", 1)[1]
    loudness = float(re.search(r"Integrated loudness:\s+I:\s+(\S+) LUFS", summary).group(1))
    peak = float(re.search(r"True peak:\s+Peak:\s+(\S+) dBFS", summary).group(1))
    return loudness, peak


def probe(path: Path) -> dict:
    output = subprocess.run(
        [shutil.which("ffprobe"), "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=codec_name,sample_rate,channels,bit_rate", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout
    return json.loads(output)["streams"][0]


def frames(path: Path) -> int:
    pcm = subprocess.run(
        [FFMPEG, "-nostdin", "-loglevel", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-"],
        capture_output=True, timeout=60, check=True,
    ).stdout
    return len(pcm) // 2


def pack(member: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(zipfile.ZipInfo("pack/README.txt", (2020, 1, 1, 0, 0, 0)), b"not audio\n")
        archive.writestr(zipfile.ZipInfo(MEMBER_NAME, (2020, 1, 1, 0, 0, 0)), member)
    return buffer.getvalue()


def track(identifier: str, mood: str, audio: bytes, suffix: str, *, zipped=False, decision="approve",
          excluded=False) -> dict:
    """One fixture track: the served bytes, the audio fetch yields and the build reads, its decision and exclusion."""
    served = pack(audio) if zipped else audio
    return {"id": identifier, "mood": mood, "audio": audio, "served": served, "suffix": suffix,
            "zipped": zipped, "decision": decision, "excluded": excluded}


def exclusion(identifier: str) -> dict:
    return {"id": identifier, "reason": f"fixture exclusion of {identifier}",
            "measured": {"integrated_lufs": -20.0, "true_peak_dbtp": -0.5, "loudness_range_lu": 3.0},
            "decided_by": "user", "date": "2026-11-17"}


def candidate(item: dict, base: str) -> dict:
    name = "pack.zip" if item["zipped"] else f"{item['id']}{item['suffix']}"
    source = {"url": f"{base}/{name}", "sha256": sha256(item["served"]), "size": len(item["served"])}
    if item["zipped"]:
        source["member"] = {"name": MEMBER_NAME, "sha256": sha256(item["audio"]), "size": len(item["audio"])}
    return {
        "id": item["id"], "mood": item["mood"], "loop": item["mood"] == "loop",
        "title": f"Title of {item['id']}", "author": "Fixture Composer",
        "licence_url": "https://creativecommons.org/publicdomain/zero/1.0/",
        "source": source,
        "evidence": {"kind": "live", "snapshot": f"deploy/music/evidence/{item['id']}.txt",
                     "captured": "2026-11-15", "capture_url": f"https://example.invalid/{item['id']}"},
        "rerelease_note": f"Searched the catalogues for {item['id']}: no other licence.",
    }


def write_library(root: Path, tracks: list[dict], base="http://127.0.0.1:1") -> dict:
    """candidates.json, approvals.json, evidence and the sources build reads, in a temporary repository root."""
    evidence = root / "deploy/music/evidence"
    evidence.mkdir(parents=True)
    sources = root / "sources"
    sources.mkdir()
    candidates = [candidate(item, base) for item in tracks]
    for item in tracks:
        (evidence / f"{item['id']}.txt").write_text(f"CC0 statement for {item['id']}\n", encoding="utf-8")
        if item["decision"] == "approve" and not item["excluded"]:
            (sources / f"{item['id']}{item['suffix']}").write_bytes(item["audio"])
    candidates_path = root / "deploy/music/candidates.json"
    candidates_path.write_text(json.dumps({"version": 1, "candidates": candidates}), encoding="utf-8")
    approvals = json.dumps({
        "version": 1, "reviewer": FIXTURE_REVIEWER, "date": FIXTURE_DATE,
        "decisions": [{"id": item["id"], "decision": item["decision"]} for item in tracks],
    }, indent=2).encode("utf-8") + b"\n"
    approvals_path = root / "deploy/music/approvals.json"
    approvals_path.write_bytes(approvals)
    excluded_path = root / "deploy/music/library-excluded.json"
    excluded_path.write_text(json.dumps({"version": 1, "excluded": [
        exclusion(item["id"]) for item in tracks if item["excluded"]]}), encoding="utf-8")
    (root / "out").mkdir()
    return {"root": root, "candidates": candidates_path, "approvals": approvals_path, "approvals_bytes": approvals,
            "excluded": excluded_path, "sources": sources, "out": root / "out" / "library", "entries": candidates}


def build(library: dict):
    return music_library.build(library["candidates"], library["approvals"], library["excluded"], library["root"],
                               library["sources"], library["out"])


# A mistaken real-source fetch must not turn a unit test into WAN access.
def isolated_env(home: Path) -> dict:
    return {"PATH": os.environ["PATH"], "HOME": str(home),
            "http_proxy": "http://127.0.0.1:1", "https_proxy": "http://127.0.0.1:1",
            "no_proxy": "127.0.0.1,localhost"}


def run_cli(*args, home: Path):
    return subprocess.run([sys.executable, "-I", str(MUSIC_LIBRARY), *map(str, args)],
                          env=isolated_env(home), text=True, capture_output=True, timeout=120)


def library_args(library: dict) -> list:
    return ["--candidates", library["candidates"], "--approvals", library["approvals"],
            "--excluded", library["excluded"], "--root", library["root"]]


@contextmanager
def serve(tracks: list[dict], changed: dict | None = None):
    """Serve each track's bytes under its candidate URL path; record every requested path."""
    files = {}
    for item in tracks:
        name = "/pack.zip" if item["zipped"] else f"/{item['id']}{item['suffix']}"
        files[name] = (changed or {}).get(item["id"], item["served"])
    requested = []

    class Handler(OriginRequestHandler):
        def do_GET(self):
            requested.append(self.path)
            content = files.get(self.path)
            if content is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format, *args):  # noqa: A003
            pass

    with HttpOriginLifetime(Handler) as lifetime:
        yield lifetime.url, requested


class RefusalAssertions(unittest.TestCase):
    def assert_refused(self, library: dict, identifier: str, *messages: str) -> str:
        with self.assertRaises(MusicError) as caught:
            build(library)
        message = str(caught.exception)
        self.assertIn(identifier, message)
        for expected in messages:
            self.assertIn(expected, message)
        self.assertFalse(library["out"].exists())
        self.assertEqual(list(library["out"].parent.iterdir()), [])
        return message


class RealLibraryTests(unittest.TestCase):
    def test_approved_library_is_the_approved_subset_less_the_exclusions(self):
        tracks, approvals, excluded = music_library.approved_library(
            MUSIC_DIR / "candidates.json", MUSIC_DIR / "approvals.json", MUSIC_DIR / "library-excluded.json", ROOT)
        self.assertEqual([item["id"] for item in tracks], SHIPPED_IDS)
        self.assertEqual(approvals, {"sha256": APPROVALS_SHA256, "reviewer": "Danker", "date": "2026-11-16"})
        self.assertEqual(excluded, LIBRARY_EXCLUDED["excluded"])
        ids = {item["id"] for item in tracks}
        self.assertEqual(ids & set(DROPPED_IDS), set())
        self.assertEqual(ids & {entry["id"] for entry in LIBRARY_EXCLUDED["excluded"]}, set())
        counts = {mood: sum(item["mood"] == mood for item in tracks) for mood in MOODS}
        self.assertEqual(counts, {"calm-curious": 4, "upbeat-corporate": 1, "neutral-serious": 3, "loop": 3})

    def test_tracked_exclusions_are_the_user_decision(self):
        data = json.loads((MUSIC_DIR / "library-excluded.json").read_text(encoding="utf-8"))
        self.assertEqual(data, LIBRARY_EXCLUDED)

    def test_report_prints_each_mood_against_four_and_the_exclusions(self):
        with tmp_dir() as directory:
            result = run_cli("report", home=Path(directory))
        self.assertEqual(result.returncode, 0, result.stderr)
        for line in ("calm-curious 4/4", "upbeat-corporate 1/4", "neutral-serious 3/4", "loop 3/4"):
            self.assertRegex(result.stdout, rf"(?m)^{re.escape(line)}\b")
        for entry in LIBRARY_EXCLUDED["excluded"]:
            self.assertRegex(result.stdout, rf"(?m)^excluded {re.escape(entry['id'])} .*{re.escape(entry['reason'])}")
        print(f"\n{result.stdout}", end="")

    def test_observing_the_star_note_records_the_page_title_search(self):
        candidates = json.loads((MUSIC_DIR / "candidates.json").read_text(encoding="utf-8"))["candidates"]
        note = next(item for item in candidates if item["id"] == "observing-the-star")["rerelease_note"]
        self.assertIn("Another space background track", note)
        self.assertTrue(note.endswith(" " + OBSERVING_NOTE), note)


class FetchTests(unittest.TestCase):
    def fixture_tracks(self) -> list[dict]:
        return [
            track("direct-tone", "calm-curious", b"ID3 synthetic direct bytes\n", ".mp3"),
            track("dropped-tone", "upbeat-corporate", b"ID3 synthetic dropped bytes\n", ".mp3", decision="drop"),
            track("member-tone", "neutral-serious", b"fLaC synthetic member bytes\n", ".flac", zipped=True),
            track("excluded-tone", "loop", b"RIFF synthetic excluded bytes\n", ".wav", excluded=True),
        ]

    def test_only_approved_sources_are_downloaded(self):
        tracks = self.fixture_tracks()
        with tmp_dir() as directory, serve(tracks) as (base, requested):
            library = write_library(Path(directory), tracks, base)
            result = run_cli("fetch", *library_args(library), "--out", library["out"], home=Path(directory))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(sorted(requested), ["/direct-tone.mp3", "/pack.zip"])
            self.assertEqual(sorted(path.name for path in library["out"].iterdir()),
                             ["direct-tone.mp3", "member-tone.flac"])
            self.assertEqual((library["out"] / "member-tone.flac").read_bytes(), tracks[2]["audio"])
            self.assertEqual((library["out"] / "direct-tone.mp3").read_bytes(), tracks[0]["audio"])
            self.assertEqual(list(library["out"].parent.iterdir()), [library["out"]])

    def test_served_byte_change_fails_naming_the_track(self):
        tracks = self.fixture_tracks()
        changed = {"member-tone": tracks[2]["served"][:-1] + bytes([tracks[2]["served"][-1] ^ 1])}
        with tmp_dir() as directory, serve(tracks, changed) as (base, _requested):
            library = write_library(Path(directory), tracks, base)
            result = run_cli("fetch", *library_args(library), "--out", library["out"], home=Path(directory))
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("member-tone: SHA-256 differs from pin", result.stderr)
            self.assertEqual(list(library["out"].parent.iterdir()), [])

    def test_existing_output_directory_is_refused_and_preserved(self):
        tracks = self.fixture_tracks()
        with tmp_dir() as directory, serve(tracks) as (base, requested):
            library = write_library(Path(directory), tracks, base)
            library["out"].mkdir()
            (library["out"] / "previous.txt").write_text("previous\n", encoding="utf-8")
            result = run_cli("fetch", *library_args(library), "--out", library["out"], home=Path(directory))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(f"{library['out']}: destination already exists", result.stderr)
            self.assertEqual([path.name for path in library["out"].iterdir()], ["previous.txt"])
            self.assertEqual(list(library["out"].parent.iterdir()), [library["out"]])
            self.assertEqual(requested, [])


class BuildPinRefusalTests(RefusalAssertions):
    """Pins are verified before any ffmpeg call, so these refusals need no ffmpeg."""

    def fixture_tracks(self) -> list[dict]:
        return [
            track("direct-tone", "calm-curious", b"ID3 synthetic direct bytes\n", ".mp3"),
            track("member-tone", "neutral-serious", b"fLaC synthetic member bytes\n", ".flac", zipped=True),
        ]

    def test_source_digest_differing_from_its_pin_fails(self):
        with tmp_dir() as directory:
            library = write_library(Path(directory), self.fixture_tracks())
            (library["sources"] / "direct-tone.mp3").write_bytes(b"ID3 synthetic DIRECT bytes\n")
            self.assert_refused(library, "direct-tone", "source SHA-256 differs from pin")

    def test_member_differing_from_its_pin_fails(self):
        with tmp_dir() as directory:
            library = write_library(Path(directory), self.fixture_tracks())
            (library["sources"] / "member-tone.flac").write_bytes(b"fLaC synthetic MEMBER bytes\n")
            self.assert_refused(library, "member-tone", "member SHA-256 differs from pin")

    def test_missing_source_fails(self):
        with tmp_dir() as directory:
            library = write_library(Path(directory), self.fixture_tracks())
            (library["sources"] / "member-tone.flac").unlink()
            self.assert_refused(library, "member-tone", "missing")

    def test_existing_output_directory_is_refused_and_preserved(self):
        with tmp_dir() as directory:
            library = write_library(Path(directory), self.fixture_tracks())
            library["out"].mkdir()
            (library["out"] / "previous.txt").write_text("previous\n", encoding="utf-8")
            result = run_cli("build", *library_args(library), "--sources", library["sources"],
                             "--out", library["out"], home=Path(directory))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(f"{library['out']}: destination already exists", result.stderr)
            self.assertEqual([path.name for path in library["out"].iterdir()], ["previous.txt"])
            self.assertEqual(list(library["out"].parent.iterdir()), [library["out"]])


class ExclusionFileTests(unittest.TestCase):
    """library-excluded.json names only approved candidates, once each, with exactly its fields."""

    def fixture_tracks(self) -> list[dict]:
        return [
            track("direct-tone", "calm-curious", b"ID3 synthetic direct bytes\n", ".mp3", excluded=True),
            track("dropped-tone", "upbeat-corporate", b"ID3 synthetic dropped bytes\n", ".mp3", decision="drop"),
            track("loop-tone", "loop", b"RIFF synthetic loop bytes\n", ".wav"),
        ]

    def load(self, mutate=None):
        with tmp_dir() as directory:
            library = write_library(Path(directory), self.fixture_tracks())
            data = json.loads(library["excluded"].read_text(encoding="utf-8"))
            if mutate is not None:
                mutate(data)
            library["excluded"].write_text(json.dumps(data), encoding="utf-8")
            return music_library.approved_library(
                library["candidates"], library["approvals"], library["excluded"], library["root"])

    def test_excluded_approved_track_is_subtracted(self):
        tracks, _approvals, excluded = self.load()
        self.assertEqual([item["id"] for item in tracks], ["loop-tone"])
        self.assertEqual(excluded, [exclusion("direct-tone")])

    def test_invalid_exclusions_are_rejected(self):
        cases = {
            "unknown": (lambda data: data["excluded"].append(exclusion("stranger-tone")), "stranger-tone"),
            "dropped": (lambda data: data["excluded"].append(exclusion("dropped-tone")), "dropped-tone"),
            "duplicate": (lambda data: data["excluded"].append(exclusion("direct-tone")), "duplicate"),
            "extra key": (lambda data: data["excluded"][0].update(note="extra"), "fields"),
            "extra measured key": (lambda data: data["excluded"][0]["measured"].update(peak=0.0), "fields"),
            "top-level key": (lambda data: data.update(reviewer="someone"), "version 1"),
            "decided by": (lambda data: data["excluded"][0].update(decided_by="orchestrator"), "decided_by"),
        }
        for name, (mutate, message) in cases.items():
            with self.subTest(name), self.assertRaisesRegex(MusicError, re.escape(message)):
                self.load(mutate)


@NEEDS_FFMPEG
class LoudnessTests(RefusalAssertions):
    def render_tracks(self, directory: Path) -> list[dict]:
        audio = directory / "audio"
        audio.mkdir()
        return [
            track("quiet-tone", "calm-curious",
                  render(audio, "quiet.mp3", QUIET, 12, ["-c:a", "libmp3lame", "-b:a", "192k"]), ".mp3"),
            track("dropped-tone", "upbeat-corporate",
                  render(audio, "dropped.mp3", QUIET, 10, ["-c:a", "libmp3lame", "-b:a", "128k"]), ".mp3",
                  decision="drop"),
            track("member-tone", "neutral-serious",
                  render(audio, "member.flac", LOUD, 11, ["-c:a", "flac"]), ".flac", zipped=True),
            track("loop-tone", "loop", render(audio, "loop.wav", LOOP, 10, ["-c:a", "pcm_s16le"]), ".wav"),
            track("excluded-tone", "loop", b"RIFF never decoded\n", ".wav", excluded=True),
        ]

    def test_build_ships_normalized_files_and_their_manifest(self):
        with tmp_dir() as directory:
            root = Path(directory)
            tracks = self.render_tracks(root)
            library = write_library(root, tracks)
            returned = build(library)
            out = library["out"]
            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(returned, manifest)
            self.assertEqual(set(manifest), MANIFEST_KEYS)
            self.assertEqual(manifest["version"], 1)
            self.assertEqual(manifest["approvals"], {
                "file": "approvals.json", "sha256": sha256(library["approvals_bytes"]),
                "reviewer": FIXTURE_REVIEWER, "date": FIXTURE_DATE,
            })
            self.assertEqual((out / "approvals.json").read_bytes(), library["approvals_bytes"])
            self.assertEqual([entry["id"] for entry in manifest["tracks"]], ["quiet-tone", "member-tone", "loop-tone"])
            by_id = {item["id"]: item for item in tracks}
            entries = {item["id"]: item for item in library["entries"]}
            for entry in manifest["tracks"]:
                with self.subTest(entry["id"]):
                    item, pinned = by_id[entry["id"]], entries[entry["id"]]
                    expected_keys = set(TRACK_TYPES) | ({"member"} if item["zipped"] else set())
                    self.assertEqual(set(entry), expected_keys)
                    for field, kind in TRACK_TYPES.items():
                        self.assertIs(type(entry[field]), kind, field)
                    for field in ("mood", "loop", "title", "author", "licence_url", "rerelease_note"):
                        self.assertEqual(entry[field], pinned[field], field)
                    self.assertEqual(entry["source_url"], pinned["source"]["url"])
                    self.assertEqual((entry["approver"], entry["approval_date"]), (FIXTURE_REVIEWER, FIXTURE_DATE))
                    self.assertEqual(entry["source_sha256"], sha256(item["served"]))
                    if item["zipped"]:
                        self.assertEqual(entry["member"], {"name": MEMBER_NAME, "sha256": sha256(item["audio"])})
                    shipped = out / entry["file"]
                    self.assertEqual(entry["file"], f"{entry['id']}.{entry['format']}")
                    self.assertEqual(entry["shipped_sha256"], sha256(shipped.read_bytes()))
                    loudness, peak = measure(shipped)
                    self.assertLessEqual(abs(loudness + 16), 0.5, loudness)
                    self.assertLessEqual(peak, -1.5)
                    self.assertAlmostEqual(entry["integrated_lufs"], loudness, delta=0.1)
                    self.assertAlmostEqual(entry["true_peak_dbtp"], peak, delta=0.1)
                    stream = probe(shipped)
                    self.assertEqual((int(stream["sample_rate"]), stream["channels"]), (RATE, 2))
                    self.assertEqual(entry["sample_rate"], RATE)
                    if entry["loop"]:
                        self.assertEqual((entry["format"], stream["codec_name"]), ("wav", "pcm_s16le"))
                        source = library["sources"] / "loop-tone.wav"
                        self.assertEqual(frames(shipped), frames(source))
                        self.assertEqual(frames(source), 10 * RATE)
                    else:
                        self.assertEqual((entry["format"], stream["codec_name"]), ("mp3", "mp3"))
                        self.assertEqual(stream["bit_rate"], "320000")
                    self.assertAlmostEqual(entry["duration_seconds"], {"quiet-tone": 12, "member-tone": 11,
                                                                       "loop-tone": 10}[entry["id"]], delta=0.1)
                    self.assertEqual(entry["evidence"], f"evidence/{entry['id']}.txt")
                    self.assertEqual((out / entry["evidence"]).read_bytes(),
                                     (library["root"] / pinned["evidence"]["snapshot"]).read_bytes())
            shipped_names = sorted(str(path.relative_to(out)) for path in out.rglob("*") if path.is_file())
            self.assertEqual(shipped_names, sorted([
                "approvals.json", "manifest.json", "quiet-tone.mp3", "member-tone.mp3", "loop-tone.wav",
                "evidence/quiet-tone.txt", "evidence/member-tone.txt", "evidence/loop-tone.txt",
            ]))
            self.assertEqual(list(out.parent.iterdir()), [out])
            print(f"\nfixture manifest:\n{json.dumps(manifest, indent=2)}")

    def test_infeasible_gain_fails_naming_the_track_and_its_measurements(self):
        for identifier, mood, suffix in (("peaky-tone", "calm-curious", ".flac"), ("peaky-loop", "loop", ".wav")):
            with self.subTest(identifier), tmp_dir() as directory:
                root = Path(directory)
                audio_dir = root / "audio"
                audio_dir.mkdir()
                codec = ["-c:a", "flac"] if suffix == ".flac" else ["-c:a", "pcm_s16le"]
                audio = render(audio_dir, f"peaky{suffix}", PEAKY, 10, codec)
                loudness, peak = measure(audio_dir / f"peaky{suffix}")
                gain = -16 - loudness
                self.assertGreater(peak + gain, -1.5, "the fixture must need an infeasible gain")
                library = write_library(root, [track(identifier, mood, audio, suffix)])
                capped = loudness + (-1.5 - peak)
                self.assertLess(round(capped, 1), -16.5, "the peak cap must leave the fixture out of tolerance")
                message = self.assert_refused(library, identifier, "infeasible")
                self.assertIn(f"I {loudness:.1f} LUFS", message)
                self.assertIn(f"TP {peak:.1f} dBTP", message)
                self.assertIn(f"gain {gain:+.1f} dB", message)
                self.assertIn(f"{capped:.1f} LUFS", message)

    def test_peak_capped_gain_within_tolerance_builds(self):
        with tmp_dir() as directory:
            root = Path(directory)
            audio_dir = root / "audio"
            audio_dir.mkdir()
            audio = render(audio_dir, "capped.wav", CAPPED, 10, ["-c:a", "pcm_s16le"])
            loudness, peak = measure(audio_dir / "capped.wav")
            excess = peak + (-16 - loudness) + 1.5
            self.assertTrue(0 < excess <= 0.5, f"the fixture must overshoot the cap by 0..0.5 dB, not {excess:.2f}")
            library = write_library(root, [track("capped-loop", "loop", audio, ".wav")])
            manifest = build(library)
            shipped = library["out"] / manifest["tracks"][0]["file"]
            output_loudness, output_peak = measure(shipped)
            self.assertLessEqual(abs(output_loudness + 16), 0.5, output_loudness)
            self.assertLess(output_loudness, -16.0)
            self.assertAlmostEqual(output_loudness, loudness + (-1.5 - peak), delta=0.1)
            self.assertTrue(-1.6 <= output_peak <= -1.5, output_peak)
            self.assertEqual(frames(shipped), frames(audio_dir / "capped.wav"))

    def test_output_out_of_tolerance_after_encoding_fails(self):
        encode = music_library._encode
        calls = []

        def quieter(source, target, gain, *rest):
            calls.append(target.name)
            return encode(source, target, gain - 6, *rest)

        with tmp_dir() as directory:
            root = Path(directory)
            audio = root / "audio"
            audio.mkdir()
            tracks = [track("quiet-tone", "calm-curious",
                            render(audio, "quiet.mp3", QUIET, 10, ["-c:a", "libmp3lame", "-b:a", "192k"]), ".mp3")]
            library = write_library(root, tracks)
            with mock.patch.object(music_library, "_encode", quieter):
                message = self.assert_refused(library, "quiet-tone", "out of tolerance")
            self.assertEqual(calls, ["quiet-tone.mp3"])
            loudness = float(re.search(r"I (-?[0-9.]+) LUFS", message).group(1))
            self.assertAlmostEqual(loudness, -22, delta=0.5)

    def test_fetched_sources_build(self):
        with tmp_dir() as directory:
            root = Path(directory)
            tracks = self.render_tracks(root)
            with serve(tracks) as (base, requested):
                library = write_library(root, tracks, base)
                shutil.rmtree(library["sources"])
                fetched = root / "fetched"
                result = run_cli("fetch", *library_args(library), "--out", fetched, home=root)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("/dropped-tone.mp3", requested)
            library["sources"] = fetched
            manifest = build(library)
            self.assertEqual([entry["id"] for entry in manifest["tracks"]], ["quiet-tone", "member-tone", "loop-tone"])


if __name__ == "__main__":
    unittest.main()
