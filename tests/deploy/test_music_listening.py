# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Music candidates download only verified bytes and keep their CC0 evidence on the page."""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import zipfile

from support import ROOT, tmp_dir
from tests.orchestrator._http_origin import HttpOriginLifetime, OriginRequestHandler

MUSIC_DIR = ROOT / "deploy" / "music"
if str(MUSIC_DIR) not in sys.path:
    sys.path.insert(0, str(MUSIC_DIR))

import listening_page  # noqa: E402
import music_candidates  # noqa: E402

LISTENING_PAGE = MUSIC_DIR / "listening_page.py"
DIRECT_AUDIO = b"ID3 synthetic direct candidate bytes\n"
MEMBER_NAME = "pack/Loop One.wav"
MEMBER_AUDIO = b"RIFF synthetic loop member bytes\n"
AUDIO_SUFFIXES = {".mp3", ".wav", ".ogg", ".flac", ".zip"}
EVIDENCE_LIMIT = 500 * 1024
MOODS = ("calm-curious", "upbeat-corporate", "neutral-serious", "loop")
EXCLUDED_FIELDS = {"title", "author", "source", "conflict", "reason"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def wayback_sha1(data: bytes) -> str:
    return base64.b32encode(hashlib.sha1(data).digest()).decode("ascii")


def pack_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(zipfile.ZipInfo("pack/README.txt", (2020, 1, 1, 0, 0, 0)), b"not audio\n")
        archive.writestr(zipfile.ZipInfo(MEMBER_NAME, (2020, 1, 1, 0, 0, 0)), MEMBER_AUDIO)
    return buffer.getvalue()


PACK = pack_bytes()


@contextmanager
def serve_audio():
    files = {"/direct.mp3": DIRECT_AUDIO, "/pack.zip": PACK}

    class Handler(OriginRequestHandler):
        def do_GET(self):
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
        yield lifetime.url


def manifest(base="http://127.0.0.1:1") -> dict:
    return {"version": 1, "candidates": [
        {
            "id": "calm-direct",
            "mood": "calm-curious",
            "loop": False,
            "title": "Direct <Track>",
            "author": "Fixture Composer",
            "licence_url": "https://creativecommons.org/publicdomain/zero/1.0/",
            "source": {"url": f"{base}/direct.mp3", "sha256": sha256(DIRECT_AUDIO), "size": len(DIRECT_AUDIO)},
            "evidence": {
                "kind": "live",
                "snapshot": "deploy/music/evidence/direct.txt",
                "captured": "2026-11-16",
                "capture_url": "https://example.invalid/direct",
            },
            "rerelease_note": "Searched the composer site for Direct Track: no other licence.",
        },
        {
            "id": "loop-member",
            "mood": "loop",
            "loop": True,
            "title": "Loop One",
            "author": "Fixture Looper",
            "licence_url": "https://creativecommons.org/publicdomain/zero/1.0/",
            "source": {
                "url": f"{base}/pack.zip", "sha256": sha256(PACK), "size": len(PACK),
                "member": {"name": MEMBER_NAME, "sha256": sha256(MEMBER_AUDIO), "size": len(MEMBER_AUDIO)},
            },
            "evidence": {
                "kind": "archived",
                "snapshot": "deploy/music/evidence/pack.txt",
                "captured": "2026-11-16",
                "capture_url": "http://web.archive.org/web/20200101000000/https://example.invalid/",
                "wayback_capture": "http://web.archive.org/web/20200101000000id_/https://example.invalid/pack.zip",
                "wayback_sha1": wayback_sha1(PACK),
            },
            "rerelease_note": "Searched MusicBrainz for Loop One: no ISRC.",
        },
    ]}


def write_tree(root: Path, data: dict) -> Path:
    evidence = root / "deploy/music/evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "direct.txt").write_text("Live CC0 statement fixture\n", encoding="utf-8")
    (evidence / "pack.txt").write_text("Archived CC0 statement fixture\n", encoding="utf-8")
    path = root / "deploy/music/candidates.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.audio, self.links, self.notes, self.choices = [], [], [], []
        self._note = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "audio":
            self.audio.append(attributes.get("src"))
        elif tag == "a":
            self.links.append(attributes.get("href"))
        elif tag == "input" and attributes.get("type") == "radio":
            self.choices.append((attributes.get("name"), attributes.get("value")))
        self._note = "rerelease-note" in (attributes.get("class") or "").split()

    def handle_data(self, data):
        if self._note:
            self.notes.append(data.strip())
            self._note = False


class ListeningPageTests(unittest.TestCase):
    def build(self, root: Path, data: dict):
        candidates = write_tree(root, data)
        pages = root / "pages"
        pages.mkdir(exist_ok=True)
        output = pages / "page"
        # A mistaken real-manifest fetch must not turn a unit test into WAN access.
        env = {"PATH": os.environ["PATH"], "HOME": str(root),
               "http_proxy": "http://127.0.0.1:1", "https_proxy": "http://127.0.0.1:1",
               "no_proxy": "127.0.0.1,localhost"}
        result = subprocess.run(
            [sys.executable, "-I", str(LISTENING_PAGE), "--candidates", str(candidates),
             "--root", str(root), "--output", str(output)],
            env=env, text=True, capture_output=True, timeout=30,
        )
        return result, output

    def assert_failed_cleanly(self, result, output, message):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(message, result.stderr)
        self.assertEqual(list(output.parent.iterdir()), [])

    def test_page_holds_audio_evidence_and_rerelease_note_per_candidate(self):
        with tmp_dir() as directory, serve_audio() as base:
            result, output = self.build(Path(directory), manifest(base))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((output / "calm-direct.mp3").read_bytes(), DIRECT_AUDIO)
            self.assertEqual((output / "loop-member.wav").read_bytes(), MEMBER_AUDIO)
            self.assertFalse(any(path.suffix == ".zip" for path in output.rglob("*")))
            self.assertEqual((output / "evidence/direct.txt").read_text(), "Live CC0 statement fixture\n")
            self.assertEqual((output / "evidence/pack.txt").read_text(), "Archived CC0 statement fixture\n")
            page = PageParser()
            text = (output / "index.html").read_text(encoding="utf-8")
            page.feed(text)
            self.assertEqual(page.audio, ["calm-direct.mp3", "loop-member.wav"])
            self.assertIn("evidence/direct.txt", page.links)
            self.assertIn("evidence/pack.txt", page.links)
            self.assertIn(manifest()["candidates"][1]["evidence"]["wayback_capture"], page.links)
            self.assertEqual(page.notes, [item["rerelease_note"] for item in manifest()["candidates"]])
            self.assertEqual(page.choices, [
                ("decision-calm-direct", "approve"), ("decision-calm-direct", "drop"),
                ("decision-loop-member", "approve"), ("decision-loop-member", "drop"),
            ])
            self.assertIn("Direct &lt;Track&gt;", text)
            self.assertNotIn("Direct <Track>", text)
            self.assertIn('id="export"', text)

    def test_wayback_digest_over_the_member_fails(self):
        with tmp_dir() as directory, serve_audio() as base:
            data = manifest(base)
            data["candidates"][1]["evidence"]["wayback_sha1"] = wayback_sha1(MEMBER_AUDIO)
            result, output = self.build(Path(directory), data)
            self.assert_failed_cleanly(result, output, "loop-member: Wayback SHA-1 differs")

    def test_source_mismatches_fail_without_writing(self):
        cases = {
            "sha256": (lambda data: data["candidates"][0]["source"].update(sha256="0" * 64),
                       "calm-direct: SHA-256 differs"),
            "size": (lambda data: data["candidates"][0]["source"].update(size=len(DIRECT_AUDIO) + 1),
                     "calm-direct: size differs"),
            "member": (lambda data: data["candidates"][1]["source"]["member"].update(sha256="0" * 64),
                       "loop-member: member: SHA-256 differs"),
            "wayback": (lambda data: data["candidates"][1]["evidence"].update(wayback_sha1="A" * 32),
                        "loop-member: Wayback SHA-1 differs"),
        }
        for name, (mutate, message) in cases.items():
            with self.subTest(name), tmp_dir() as directory, serve_audio() as base:
                data = manifest(base)
                mutate(data)
                result, output = self.build(Path(directory), data)
                self.assert_failed_cleanly(result, output, message)

    def test_existing_output_directory_is_refused_and_preserved(self):
        with tmp_dir() as directory, serve_audio() as base:
            root = Path(directory)
            occupied = root / "pages/page"
            occupied.mkdir(parents=True)
            (occupied / "previous.txt").write_text("previous page\n", encoding="utf-8")
            result, output = self.build(root, manifest(base))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("already exists", result.stderr)
            self.assertEqual(sorted(path.name for path in output.iterdir()), ["previous.txt"])
            self.assertEqual(sorted(path.name for path in output.parent.iterdir()), ["page"])


# Runs the page script in a vm context with a strict DOM stub; prints what the export button produced.
EXPORT_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const input = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const elements = {};
for (const id of ["candidate-ids", "reviewer", "date", "status", "export"]) {
  elements[id] = {value: "", textContent: "", listeners: {},
                  addEventListener(type, listener) { this.listeners[type] = listener; }};
}
elements["candidate-ids"].textContent = input.ids_text;
const blobs = new Map();
const revoked = [];
let clicked = null;
const document = {
  getElementById(id) {
    if (!(id in elements)) throw new Error(`unexpected element id ${id}`);
    return elements[id];
  },
  querySelector(selector) {
    const match = /^input\[name="decision-([a-z0-9-]+)"\]:checked$/.exec(selector);
    if (!match) throw new Error(`unexpected selector ${selector}`);
    return match[1] in input.checked ? {value: input.checked[match[1]]} : null;
  },
  createElement(tag) {
    if (tag !== "a") throw new Error(`unexpected element ${tag}`);
    return {href: "", download: "", click() { clicked = {href: this.href, download: this.download}; }};
  },
};
const URL = {
  createObjectURL(blob) { const href = `blob:export-${blobs.size}`; blobs.set(href, blob); return href; },
  revokeObjectURL(href) { revoked.push(href); },
};
const setTimeout = (callback) => callback();
vm.runInNewContext(input.script, {document, URL, Blob, setTimeout});
const prefilled = elements.date.value;
elements.reviewer.value = input.reviewer;
elements.date.value = input.date;
if (typeof elements.export.listeners.click !== "function") throw new Error("no click listener on export");
elements.export.listeners.click();
(async () => {
  const body = clicked ? await blobs.get(clicked.href).text() : null;
  process.stdout.write(JSON.stringify({prefilled, status: elements.status.textContent, revoked,
                                       download: clicked && clicked.download, href: clicked && clicked.href, body}));
})();
"""


class ScriptParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids_text, self.script = None, None
        self._target = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script":
            self._target = "ids_text" if attributes.get("id") == "candidate-ids" else "script"

    def handle_endtag(self, tag):
        if tag == "script":
            self._target = None

    def handle_data(self, data):
        if self._target:
            setattr(self, self._target, data)


class ExportScriptTests(unittest.TestCase):
    candidates = manifest()["candidates"]

    def export(self, directory: Path, checked: dict) -> dict:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not on PATH; the page export script needs a JavaScript runtime")
        page = ScriptParser()
        page.feed(listening_page.render(self.candidates))
        self.assertIsNotNone(page.ids_text)
        self.assertIsNotNone(page.script)
        harness, data = directory / "harness.js", directory / "input.json"
        harness.write_text(EXPORT_HARNESS, encoding="utf-8")
        data.write_text(json.dumps({"script": page.script, "ids_text": page.ids_text, "checked": checked,
                                    "reviewer": "  Fixture Reviewer ", "date": "2026-11-16"}), encoding="utf-8")
        result = subprocess.run([node, str(harness), str(data)], text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertRegex(output["prefilled"], r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
        return output

    def test_complete_export_is_accepted_by_load_decisions(self):
        with tmp_dir() as directory:
            output = self.export(Path(directory), {"calm-direct": "approve", "loop-member": "drop"})
            self.assertEqual(output["status"], "Exported 2 decisions.")
            self.assertEqual(output["download"], "music-decisions.json")
            self.assertEqual(output["revoked"], [output["href"]])
            path = Path(directory) / "music-decisions.json"
            path.write_text(output["body"], encoding="utf-8")
            self.assertEqual(music_candidates.load_decisions(path, self.candidates),
                             {"calm-direct": "approve", "loop-member": "drop"})
            export = json.loads(output["body"])
            self.assertEqual((export["reviewer"], export["date"]), ("Fixture Reviewer", "2026-11-16"))

    def test_partial_selection_exports_nothing(self):
        with tmp_dir() as directory:
            output = self.export(Path(directory), {"calm-direct": "approve"})
            self.assertEqual(output["status"], "No decision yet for: loop-member")
            self.assertIsNone(output["download"])
            self.assertIsNone(output["body"])
            self.assertEqual(output["revoked"], [])


class ManifestLoaderTests(unittest.TestCase):
    def load(self, data: dict):
        with tmp_dir() as directory:
            root = Path(directory)
            return music_candidates.load_candidates(write_tree(root, data), root)

    def assert_rejected(self, mutate, message):
        data = manifest()
        mutate(data)
        with self.assertRaisesRegex(music_candidates.MusicError, message):
            self.load(data)

    def test_valid_manifest_loads(self):
        self.assertEqual([item["id"] for item in self.load(manifest())], ["calm-direct", "loop-member"])

    def test_missing_field_is_rejected(self):
        self.assert_rejected(lambda data: data["candidates"][0].pop("rerelease_note"), "fields")

    def test_unknown_mood_is_rejected(self):
        self.assert_rejected(lambda data: data["candidates"][0].update(mood="happy"), "mood")

    def test_evidence_outside_the_evidence_directory_is_rejected(self):
        for snapshot in ("deploy/music/direct.txt", "deploy/music/evidence/../candidates.json",
                         "/etc/passwd", "deploy/fonts/fonts.json"):
            with self.subTest(snapshot):
                self.assert_rejected(
                    lambda data: data["candidates"][0]["evidence"].update(snapshot=snapshot), "evidence")

    def test_duplicate_id_is_rejected(self):
        self.assert_rejected(lambda data: data["candidates"][1].update(id="calm-direct"), "duplicate")

    def test_non_slug_id_is_rejected(self):
        for value in ("Calm-Direct", "calm_direct", "calm direct", "../calm", ""):
            with self.subTest(value):
                self.assert_rejected(lambda data: data["candidates"][0].update(id=value), "id must match")

    def test_wayback_sha1_must_be_32_base32_characters(self):
        hex_digest = hashlib.sha1(PACK).hexdigest()
        for value in (hex_digest, "A" * 31, "A" * 33, "a" * 32, "1" * 32):
            with self.subTest(value):
                self.assert_rejected(
                    lambda data: data["candidates"][1]["evidence"].update(wayback_sha1=value), "wayback_sha1")


class DecisionLoaderTests(unittest.TestCase):
    candidates = manifest()["candidates"]

    def load(self, decisions: list[dict]):
        export = {"version": 1, "reviewer": "Fixture Reviewer", "date": "2026-11-16", "decisions": decisions}
        with tmp_dir() as directory:
            path = Path(directory) / "decisions.json"
            path.write_text(json.dumps(export), encoding="utf-8")
            return music_candidates.load_decisions(path, self.candidates)

    def test_one_decision_per_candidate_loads(self):
        decisions = [{"id": "calm-direct", "decision": "approve"}, {"id": "loop-member", "decision": "drop"}]
        self.assertEqual(self.load(decisions), {"calm-direct": "approve", "loop-member": "drop"})

    def test_invalid_decisions_are_rejected(self):
        cases = {
            "yes": ([{"id": "calm-direct", "decision": "yes"}, {"id": "loop-member", "decision": "drop"}],
                    "decision must be approve or drop"),
            "unknown": ([{"id": "calm-direct", "decision": "approve"}, {"id": "loop-member", "decision": "drop"},
                         {"id": "stranger", "decision": "drop"}], "unknown"),
            "missing": ([{"id": "calm-direct", "decision": "approve"}], "missing"),
            "duplicate": ([{"id": "calm-direct", "decision": "approve"}, {"id": "calm-direct", "decision": "drop"},
                           {"id": "loop-member", "decision": "drop"}], "duplicate"),
        }
        for name, (decisions, message) in cases.items():
            with self.subTest(name), self.assertRaisesRegex(music_candidates.MusicError, message):
                self.load(decisions)


class RealMusicFilesTests(unittest.TestCase):
    def test_tracked_candidates_evidence_and_exclusions(self):
        candidates = music_candidates.load_candidates(MUSIC_DIR / "candidates.json", ROOT)
        for item in candidates:
            self.assertTrue(item["source"]["url"].startswith("https://"), item["id"])
            snapshot = ROOT / item["evidence"]["snapshot"]
            self.assertTrue(snapshot.is_file(), item["id"])
            self.assertLess(snapshot.stat().st_size, EVIDENCE_LIMIT, item["id"])
            self.assertTrue(item["rerelease_note"].strip(), item["id"])
        excluded = json.loads((MUSIC_DIR / "excluded.json").read_text(encoding="utf-8"))
        self.assertEqual(set(excluded), {"version", "excluded"})
        self.assertEqual(excluded["version"], 1)
        self.assertTrue(excluded["excluded"])
        works = set()
        for entry in excluded["excluded"]:
            self.assertEqual(set(entry), EXCLUDED_FIELDS, entry)
            for field in ("title", "author", "source", "reason"):
                self.assertIsInstance(entry[field], str, entry)
                self.assertTrue(entry[field].strip(), entry)
            self.assertTrue(entry["conflict"] is None or (isinstance(entry["conflict"], str) and entry["conflict"]))
            work = (entry["title"], entry["author"])
            self.assertNotIn(work, works)
            works.add(work)
        candidate_works = {(item["title"], item["author"]) for item in candidates}
        self.assertEqual(candidate_works & works, set())
        counts = {mood: sum(item["mood"] == mood for item in candidates) for mood in MOODS}
        print(f"\nmusic candidates per mood: {json.dumps(counts)}; excluded works: {len(works)}")

    def test_no_audio_binary_is_tracked(self):
        tracked = subprocess.check_output(["git", "ls-files", "-z", "deploy/music"], cwd=ROOT).decode().split("\0")
        self.assertEqual([path for path in tracked if Path(path).suffix.lower() in AUDIO_SUFFIXES], [])


if __name__ == "__main__":
    unittest.main()
