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
from unittest.mock import patch



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

    def test_destination_alias_and_file_prefix_reject_before_mutation(self):
        dest = self.root / "target"
        dest.mkdir()
        sentinel = dest / "keep.txt"
        sentinel.write_text("untouched\n", encoding="utf-8")
        alias = self.root / "alias.tar.gz"
        with tarfile.open(alias, "w:gz") as bundle:
            first = tarfile.TarInfo(name="a/b")
            first.size = 1
            bundle.addfile(first, io.BytesIO(b"a"))
            second = tarfile.TarInfo(name="a/./b")
            second.size = 1
            bundle.addfile(second, io.BytesIO(b"b"))
        with self.assertRaises(recovery.RecoveryError):
            recovery_fs.validate_archive(alias)
        prefix = self.root / "prefix.tar.gz"
        with tarfile.open(prefix, "w:gz") as bundle:
            file_member = tarfile.TarInfo(name="a")
            file_member.size = 1
            bundle.addfile(file_member, io.BytesIO(b"a"))
            nested = tarfile.TarInfo(name="a/b")
            nested.size = 1
            bundle.addfile(nested, io.BytesIO(b"b"))
        with self.assertRaises(recovery.RecoveryError):
            recovery_fs.extract_tree(prefix, dest)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "untouched\n")

    def test_python_ownership_failure_is_explicit(self):
        source = self.root / "owned"
        source.mkdir()
        (source / "secret.txt").write_bytes(b"secret\n")
        archive = self.root / "owned.tar.gz"
        recovery_fs.capture_tree(source, archive)
        rewritten = self.root / "owned-mismatch.tar.gz"
        with tarfile.open(archive, "r:*") as bundle:
            members = bundle.getmembers()
            contents = {}
            for member in members:
                if member.isfile():
                    extracted = bundle.extractfile(member)
                    contents[member.name] = extracted.read() if extracted is not None else b""
        with tarfile.open(rewritten, "w:gz", format=tarfile.PAX_FORMAT) as bundle:
            for member in members:
                member.uid = 65534
                member.gid = 65534
                if member.isfile():
                    data = contents[member.name]
                    member.size = len(data)
                    bundle.addfile(member, io.BytesIO(data))
                else:
                    bundle.addfile(member)
        dest = self.root / "owned-out"
        original = os.lchown

        def refuse(path, uid, gid):
            raise OSError(1, "operation not permitted")

        os.lchown = refuse
        try:
            with self.assertRaises(recovery.RecoveryError) as raised:
                recovery_fs.extract_tree(rewritten, dest)
        finally:
            os.lchown = original
        self.assertIn("cannot restore ownership", str(raised.exception))

    def test_literal_backslash_and_special_names_round_trip(self):
        source = self.root / "names"
        source.mkdir()
        (source / "a\\b").write_bytes(b"backslash\n")
        nested = source / "a"
        nested.mkdir()
        (nested / "b").write_bytes(b"slash\n")
        (source / "-leading").write_bytes(b"dash\n")
        (source / "new\nline").write_bytes(b"newline\n")
        archive = self.root / "names.tar.gz"
        recovery_fs.capture_tree(source, archive)
        dest = self.root / "names-out"
        recovery_fs.extract_tree(archive, dest)
        self.assertEqual((dest / "a\\b").read_bytes(), b"backslash\n")
        self.assertEqual((dest / "a" / "b").read_bytes(), b"slash\n")
        self.assertEqual((dest / "-leading").read_bytes(), b"dash\n")
        self.assertEqual((dest / "new\nline").read_bytes(), b"newline\n")
        members = {record["name"] for record in recovery_fs.validate_archive(archive)}
        self.assertIn("a\\b", members)
        self.assertIn("a/b", members)

    def test_destination_conflicts_use_ancestors_not_all_pairs(self):
        names = {f"file-{index:05d}": tarfile.TarInfo(name=f"file-{index:05d}") for index in range(10_000)}
        names["file-00000"].type = tarfile.REGTYPE
        recovery_fs._reject_destination_conflicts(names)
        nested = tarfile.TarInfo(name="file-00000/child")
        nested.type = tarfile.REGTYPE
        names["file-00000/child"] = nested

        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery_fs._reject_destination_conflicts(names)
        self.assertIn("non-directory", str(raised.exception))

    def _require_gnu_tar(self):
        if not recovery_fs._gnu_tar():
            self.skipTest("GNU tar unavailable (bsdtar on macOS)")

    def _round_trip_links(self, links: dict[str, str], files: dict[str, bytes]) -> Path:
        source = self.root / "source"
        for name, data in files.items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        for name, target in links.items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, path)
        archive = self.root / "links.tar.gz"
        recovery_fs.capture_tree(source, archive)
        dest = self.root / "links-out"
        recovery_fs.extract_tree(archive, dest)
        for name, target in links.items():
            self.assertTrue((dest / name).is_symlink(), name)
            self.assertEqual(os.readlink(dest / name), target, name)
        return dest

    def _assert_hf_cache_round_trip(self):
        dest = self._round_trip_links(
            {
                "models--x/snapshots/r1/model.safetensors": "../../blobs/abc",
                "models--x/snapshots/r1/1_Pooling/config.json": "../../../blobs/def",
            },
            {"models--x/blobs/abc": b"weights\n", "models--x/blobs/def": b"pooling\n"},
        )
        snapshot = dest / "models--x" / "snapshots" / "r1"
        self.assertEqual((snapshot / "model.safetensors").read_bytes(), b"weights\n")
        self.assertEqual((snapshot / "1_Pooling" / "config.json").read_bytes(), b"pooling\n")

    def _assert_confined_links_round_trip(self):
        dest = self._round_trip_links(
            {
                "a/dot-slash": "./y",
                "a/double-slash": "b//y",
                "a/dot": ".",
                "a/dot-dir": "./",
                "a/parent": "..",
                "chatA/outputs/y": "../../chatB/x",
            },
            {"a/y": b"local\n", "a/b/y": b"nested\n", "chatB/x": b"other chat\n"},
        )
        self.assertEqual((dest / "a" / "dot-slash").read_bytes(), b"local\n")
        self.assertEqual((dest / "a" / "double-slash").read_bytes(), b"nested\n")
        self.assertEqual((dest / "chatA" / "outputs" / "y").read_bytes(), b"other chat\n")

    def _assert_escaping_capture_refused(self):
        source = self.root / "escape"
        (source / "a").mkdir(parents=True)
        os.symlink("../../outside", source / "a" / "x")
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery_fs.capture_tree(source, self.root / "escape.tar.gz")
        self.assertIn("unsafe symlink", str(raised.exception))

    def test_hf_cache_links_round_trip_with_gnu_tar(self):
        self._require_gnu_tar()
        self._assert_hf_cache_round_trip()

    def test_hf_cache_links_round_trip_with_python_fallback(self):
        with patch.object(recovery_fs, "_gnu_tar", return_value=False):
            self._assert_hf_cache_round_trip()

    def test_confined_links_round_trip_with_gnu_tar(self):
        self._require_gnu_tar()
        self._assert_confined_links_round_trip()

    def test_confined_links_round_trip_with_python_fallback(self):
        with patch.object(recovery_fs, "_gnu_tar", return_value=False):
            self._assert_confined_links_round_trip()

    def test_escaping_link_capture_refused_with_gnu_tar(self):
        self._require_gnu_tar()
        self._assert_escaping_capture_refused()

    def test_escaping_link_capture_refused_with_python_fallback(self):
        with patch.object(recovery_fs, "_gnu_tar", return_value=False):
            self._assert_escaping_capture_refused()

    def test_escaping_link_archives_refuse_before_mutation(self):
        cases = [
            ("x", "../y"),
            ("a/x", "../../y"),
            ("a/b/x", "a/../../../y"),
            ("a/x", "b/../y"),
            ("a/x", "/etc/passwd"),
            ("a/b/x", "../../.."),
            ("a/x", ""),
        ]
        for index, (name, target) in enumerate(cases):
            with self.subTest(name=name, target=target):
                archive = self.root / f"unsafe-{index}.tar.gz"
                with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as bundle:
                    root = tarfile.TarInfo(name=".")
                    root.type = tarfile.DIRTYPE
                    bundle.addfile(root)
                    parts = name.split("/")
                    for depth in range(1, len(parts)):
                        directory = tarfile.TarInfo(name="/".join(parts[:depth]))
                        directory.type = tarfile.DIRTYPE
                        bundle.addfile(directory)
                    link = tarfile.TarInfo(name=name)
                    link.type = tarfile.SYMTYPE
                    link.linkname = target
                    bundle.addfile(link)
                with self.assertRaises(recovery.RecoveryError) as raised:
                    recovery_fs.validate_archive(archive)
                self.assertIn("unsafe symlink", str(raised.exception))
                dest = self.root / f"unsafe-out-{index}"
                dest.mkdir()
                with self.assertRaises(recovery.RecoveryError) as raised:
                    recovery_fs.extract_tree(archive, dest)
                self.assertIn("unsafe symlink", str(raised.exception))
                self.assertEqual(os.listdir(dest), [])

    def _require_case_insensitive(self):
        probe = self.root / "CaseProbe"
        probe.write_bytes(b"")
        insensitive = (self.root / "caseprobe").exists()
        probe.unlink()
        if not insensitive:
            self.skipTest("temporary filesystem is case-sensitive")

    def _owned_archive(self, archive: Path, members: list[tuple[str, str, str | bytes]]) -> list:
        with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as bundle:
            for name, kind, value in members:
                member = tarfile.TarInfo(name=name)
                member.uid = os.getuid()
                member.gid = os.getgid()
                member.mode = 0o755
                if kind == "dir":
                    member.type = tarfile.DIRTYPE
                    bundle.addfile(member)
                elif kind == "link":
                    member.type = tarfile.SYMTYPE
                    member.linkname = value
                    bundle.addfile(member)
                else:
                    member.size = len(value)
                    member.mode = 0o644
                    bundle.addfile(member, io.BytesIO(value))
        return recovery_fs.inspect_archive(archive)

    def _assert_case_alias_refused(self, members, expected: str):
        self._require_case_insensitive()
        archive = self.root / "alias.tar.gz"
        self._owned_archive(archive, members)
        outer = self.root / "x"
        dest = outer / "y" / "dest"
        (outer / "y").mkdir(parents=True)
        with patch.object(recovery_fs, "_gnu_tar", return_value=False):
            with self.assertRaises(recovery.RecoveryError) as raised:
                recovery_fs.extract_tree(archive, dest)
        self.assertFalse((outer / "evil").exists(), "file written outside the destination")
        self.assertEqual(os.listdir(outer), ["y"])
        self.assertEqual(os.listdir(outer / "y"), ["dest"])
        self.assertIn(expected, str(raised.exception))

    def test_case_alias_parent_link_is_refused_before_writing(self):
        self._assert_case_alias_refused(
            [
                (".", "dir", ""),
                ("d", "dir", ""),
                ("d/L", "link", ".."),
                ("d/l/M", "link", "../.."),
                ("d/l/m/evil", "file", b"pwned\n"),
            ],
            "unsafe parent d/l/M",
        )

    def test_case_alias_link_over_existing_directory_is_refused(self):
        self._assert_case_alias_refused(
            [
                (".", "dir", ""),
                ("d", "dir", ""),
                ("d/l/m/evil", "file", b"pwned\n"),
                ("d/L", "link", ".."),
                ("d/l/M", "link", "../.."),
            ],
            "existing target d/L",
        )

    def test_python_extract_refuses_on_disk_symlink_parent(self):
        outside = self.root / "outside"
        outside.mkdir()
        dest = self.root / "dest"
        (dest / "d").mkdir(parents=True)
        os.symlink(outside, dest / "d" / "l")
        archive = self.root / "parent.tar.gz"
        members = self._owned_archive(
            archive,
            [(".", "dir", ""), ("d", "dir", ""), ("d/l/m/evil", "file", b"pwned\n")],
        )
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery_fs._python_extract(archive, dest, members)
        self.assertEqual(os.listdir(outside), [])
        self.assertIn("unsafe parent d/l/m/evil", str(raised.exception))

    def test_python_extract_refuses_existing_target(self):
        dest = self.root / "dest"
        (dest / "d").mkdir(parents=True)
        (dest / "d" / "x").write_bytes(b"kept\n")
        archive = self.root / "existing.tar.gz"
        members = self._owned_archive(archive, [(".", "dir", ""), ("d/x", "link", "y")])
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery_fs._python_extract(archive, dest, members)
        self.assertEqual((dest / "d" / "x").read_bytes(), b"kept\n")
        self.assertIn("existing target d/x", str(raised.exception))









if __name__ == "__main__":
    unittest.main()
