# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Font preparation uses pinned bytes and publishes complete regular-file bundles."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import unittest

from support import FONT_FILES, ROOT, font_archive_bytes, font_pin, serve_font_archive, tmp_dir


class PrepareFontsTests(unittest.TestCase):
    def prepare(self, root: Path, pin: dict, name: str):
        source = root / f"{name}.json"
        source.write_text(json.dumps(pin), encoding="utf-8")
        output = root / f"{name}.tar"
        # A mistaken default-pin fetch must not turn a unit test into WAN access.
        env = {"PATH": os.environ["PATH"], "HOME": str(root),
               "http_proxy": "http://127.0.0.1:1", "https_proxy": "http://127.0.0.1:1",
               "no_proxy": "127.0.0.1,localhost"}
        result = subprocess.run(
            [sys.executable, str(ROOT / "deploy/fonts/prepare_fonts.py"),
             "--pin", str(source), "--output", str(output)],
            env=env, text=True, capture_output=True, timeout=30,
        )
        return result, output

    def test_archive_digest_mismatch_leaves_no_output(self):
        with tmp_dir() as directory, serve_font_archive() as url:
            root = Path(directory)
            pin = font_pin(url)
            pin["archives"][0]["sha256"] = "0" * 64
            result, output = self.prepare(root, pin, "bad-digest")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("SHA-256", result.stderr)
            self.assertFalse(output.exists())

    def test_selected_zip_link_is_not_a_font_file(self):
        data = font_archive_bytes(linked=True)
        with tmp_dir() as directory, serve_font_archive(data) as url:
            result, output = self.prepare(Path(directory), font_pin(url, archive_bytes=data), "linked")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("regular", result.stderr)
            self.assertFalse(output.exists())

    def test_bundle_bytes_ignore_upstream_zip_timestamps(self):
        with tmp_dir() as directory:
            root = Path(directory)
            bundles = []
            for year in (2001, 2024):
                data = font_archive_bytes(year=year)
                with serve_font_archive(data) as url:
                    result, output = self.prepare(root, font_pin(url, archive_bytes=data), str(year))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                with tarfile.open(output) as archive:
                    self.assertEqual(set(archive.getnames()), set(FONT_FILES))
                    for member in archive:
                        self.assertTrue(member.isfile())
                        self.assertEqual(archive.extractfile(member).read(), FONT_FILES[member.name])
                bundles.append(output.read_bytes())
            self.assertEqual(bundles[0], bundles[1])

    def test_existing_output_is_preserved(self):
        with tmp_dir() as directory, serve_font_archive() as url:
            root = Path(directory)
            existing = root / "occupied.tar"
            existing.write_bytes(b"previous complete bundle")
            result, output = self.prepare(root, font_pin(url), "occupied")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.read_bytes(), b"previous complete bundle")

    def test_release_font_binaries_are_not_tracked(self):
        pin = json.loads((ROOT / "deploy/fonts/fonts.json").read_text())
        font_suffixes = {".otf", ".ttf", ".ttc", ".woff", ".woff2"}
        names = {
            item["name"] for archive in pin["archives"] for item in archive["files"]
            if Path(item["name"]).suffix.lower() in font_suffixes
        }
        tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
        self.assertEqual({Path(path).name for path in tracked} & names, set())
        for path in tracked:
            if path.startswith("deploy/fonts/"):
                self.assertNotIn(Path(path).suffix.lower(), font_suffixes)
                with (ROOT / path).open("rb") as source:
                    self.assertNotIn(source.read(4), (b"OTTO", b"\x00\x01\x00\x00", b"ttcf", b"wOFF", b"wOF2"))

    def test_download_cannot_exceed_pinned_size(self):
        with tmp_dir() as directory, serve_font_archive() as url:
            pin = font_pin(url)
            pin["archives"][0]["size"] -= 1
            result, output = self.prepare(Path(directory), pin, "oversize")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("size exceeds pin", result.stderr)
            self.assertFalse(output.exists())
