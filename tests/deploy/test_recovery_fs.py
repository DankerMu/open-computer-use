# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Filesystem recovery archives preserve metadata and reject unsafe members."""

from __future__ import annotations
import io
import os
from pathlib import Path
import stat
import tarfile
import unittest

from support import ROOT, tmp_dir

import sys

sys.path.insert(0, str(ROOT / "deploy"))
import recovery
import recovery_fs


class RecoveryFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.context = tmp_dir()
        self.root = Path(self.context.name)

    def tearDown(self):
        self.context.cleanup()

    def test_supported_links_and_modes_round_trip(self):
        source = self.root / "source"
        source.mkdir()
        regular = source / "notes.txt"
        regular.write_bytes(b"workspace-bytes\n")
        os.chmod(regular, 0o640)
        os.symlink("notes.txt", source / "alias")
        hard = source / "notes-hard.txt"
        os.link(regular, hard)
        nested = source / "keep"
        nested.mkdir()
        os.chmod(nested, 0o750)
        archive = self.root / "tree.tar.gz"
        recovery_fs.capture_tree(source, archive)
        dest = self.root / "restored"
        recovery_fs.extract_tree(archive, dest)
        restored = dest / "notes.txt"
        self.assertEqual(restored.read_bytes(), b"workspace-bytes\n")
        self.assertEqual(stat.S_IMODE(restored.stat().st_mode), 0o640)
        self.assertTrue((dest / "alias").is_symlink())
        self.assertEqual(os.readlink(dest / "alias"), "notes.txt")
        self.assertEqual((dest / "notes-hard.txt").read_bytes(), b"workspace-bytes\n")
        self.assertEqual(int(restored.stat().st_mtime), int(regular.stat().st_mtime))
        self.assertEqual(stat.S_IMODE((dest / "keep").stat().st_mode), 0o750)

    def test_traversal_and_duplicate_members_reject_before_mutation(self):
        dest = self.root / "target"
        dest.mkdir()
        sentinel = dest / "keep.txt"
        sentinel.write_text("untouched\n", encoding="utf-8")
        archive = self.root / "bad.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            member = tarfile.TarInfo(name="../escape.txt")
            payload = b"nope"
            member.size = len(payload)
            bundle.addfile(member, io.BytesIO(payload))
        with self.assertRaises(recovery.RecoveryError):
            recovery_fs.extract_tree(archive, dest)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "untouched\n")
        self.assertFalse((dest.parent / "escape.txt").exists())

        duplicate = self.root / "dup.tar.gz"
        with tarfile.open(duplicate, "w:gz") as bundle:
            first = tarfile.TarInfo(name="file.txt")
            first.size = 1
            bundle.addfile(first, io.BytesIO(b"a"))
            second = tarfile.TarInfo(name="file.txt")
            second.size = 1
            bundle.addfile(second, io.BytesIO(b"b"))
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery_fs.validate_archive(duplicate)
        self.assertIn("duplicate", str(raised.exception))

    def test_symlink_ancestor_and_device_members_fail_closed(self):
        archive = self.root / "link-escape.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            link = tarfile.TarInfo(name="out")
            link.type = tarfile.SYMTYPE
            link.linkname = ".."
            bundle.addfile(link)
            nested = tarfile.TarInfo(name="out/secret.txt")
            payload = b"nope"
            nested.size = len(payload)
            bundle.addfile(nested, io.BytesIO(payload))
        with self.assertRaises(recovery.RecoveryError):
            recovery_fs.validate_archive(archive)

        device = self.root / "device.tar.gz"
        with tarfile.open(device, "w:gz") as bundle:
            member = tarfile.TarInfo(name="null")
            member.type = tarfile.CHRTYPE
            bundle.addfile(member)
        with self.assertRaises(recovery.RecoveryError):
            recovery_fs.validate_archive(device)


if __name__ == "__main__":
    unittest.main()
