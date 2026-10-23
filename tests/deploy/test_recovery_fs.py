# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Filesystem recovery archives preserve metadata and reject unsafe members."""

from __future__ import annotations
from contextlib import ExitStack, redirect_stderr
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

MODEL_LINKS = {
    "bin/python3": "/usr/bin/python3",
    ".claude/agents/x.md": "/opt/skills-external/a/x.md",
    ".claude/skills/s": "/mnt/skills/public/s/",
    "a/x": "../../outside",
    "x": "..",
}
NON_UTF8_FILE = b"\xd6\xd0.txt"
NON_UTF8_DIR = b"\xb9\xfa"
NON_UTF8_TARGET = b"\xd6\xd0-target"


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
        recovery_fs.capture_tree(source, archive, links="confined")
        dest = self.root / "restored"
        recovery_fs.extract_tree(archive, dest, links="confined")
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
            recovery_fs.extract_tree(archive, dest, links="confined")
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
            recovery_fs.validate_archive(duplicate, links="confined")
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
            recovery_fs.validate_archive(archive, links="confined")

        device = self.root / "device.tar.gz"
        with tarfile.open(device, "w:gz") as bundle:
            member = tarfile.TarInfo(name="null")
            member.type = tarfile.CHRTYPE
            bundle.addfile(member)
        with self.assertRaises(recovery.RecoveryError):
            recovery_fs.validate_archive(device, links="confined")

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
            recovery_fs.validate_archive(alias, links="confined")
        prefix = self.root / "prefix.tar.gz"
        with tarfile.open(prefix, "w:gz") as bundle:
            file_member = tarfile.TarInfo(name="a")
            file_member.size = 1
            bundle.addfile(file_member, io.BytesIO(b"a"))
            nested = tarfile.TarInfo(name="a/b")
            nested.size = 1
            bundle.addfile(nested, io.BytesIO(b"b"))
        with self.assertRaises(recovery.RecoveryError):
            recovery_fs.extract_tree(prefix, dest, links="confined")
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "untouched\n")

    def test_python_ownership_failure_is_explicit(self):
        source = self.root / "owned"
        source.mkdir()
        (source / "secret.txt").write_bytes(b"secret\n")
        archive = self.root / "owned.tar.gz"
        recovery_fs.capture_tree(source, archive, links="confined")
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
                recovery_fs.extract_tree(rewritten, dest, links="confined")
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
        recovery_fs.capture_tree(source, archive, links="confined")
        dest = self.root / "names-out"
        recovery_fs.extract_tree(archive, dest, links="confined")
        self.assertEqual((dest / "a\\b").read_bytes(), b"backslash\n")
        self.assertEqual((dest / "a" / "b").read_bytes(), b"slash\n")
        self.assertEqual((dest / "-leading").read_bytes(), b"dash\n")
        self.assertEqual((dest / "new\nline").read_bytes(), b"newline\n")
        members = {record["name"] for record in recovery_fs.validate_archive(archive, links="confined")}
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

    def _round_trip_links(self, links: dict[str, str], files: dict[str, bytes], *, policy: str) -> Path:
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
        recovery_fs.capture_tree(source, archive, links=policy)
        dest = self.root / "links-out"
        recovery_fs.extract_tree(archive, dest, links=policy)
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
            policy="confined",
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
            policy="confined",
        )
        self.assertEqual((dest / "a" / "dot-slash").read_bytes(), b"local\n")
        self.assertEqual((dest / "a" / "double-slash").read_bytes(), b"nested\n")
        self.assertEqual((dest / "chatA" / "outputs" / "y").read_bytes(), b"other chat\n")

    def _assert_escaping_capture_refused(self):
        source = self.root / "escape"
        (source / "a").mkdir(parents=True)
        os.symlink("../../outside", source / "a" / "x")
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery_fs.capture_tree(source, self.root / "escape.tar.gz", links="confined")
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
                    recovery_fs.validate_archive(archive, links="confined")
                self.assertIn("unsafe symlink", str(raised.exception))
                dest = self.root / f"unsafe-out-{index}"
                dest.mkdir()
                with self.assertRaises(recovery.RecoveryError) as raised:
                    recovery_fs.extract_tree(archive, dest, links="confined")
                self.assertIn("unsafe symlink", str(raised.exception))
                self.assertEqual(os.listdir(dest), [])

    def _require_case_insensitive(self):
        probe = self.root / "CaseProbe"
        probe.write_bytes(b"")
        insensitive = (self.root / "caseprobe").exists()
        probe.unlink()
        if not insensitive:
            self.skipTest("temporary filesystem is case-sensitive")

    def _owned_archive(
        self, archive: Path, members: list[tuple[str, str, str | bytes]], *, links: str = "confined"
    ) -> list:
        with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as bundle:
            for name, kind, value in members:
                member = tarfile.TarInfo(name=name)
                member.uid = os.getuid()
                member.gid = os.getgid()
                member.mode = 0o755
                if kind == "dir":
                    member.type = tarfile.DIRTYPE
                    bundle.addfile(member)
                elif kind in ("link", "hardlink"):
                    member.type = tarfile.SYMTYPE if kind == "link" else tarfile.LNKTYPE
                    member.linkname = value
                    bundle.addfile(member)
                else:
                    member.size = len(value)
                    member.mode = 0o644
                    bundle.addfile(member, io.BytesIO(value))
        return recovery_fs.inspect_archive(archive, links=links)

    def _assert_case_alias_refused(self, members, expected: str):
        self._require_case_insensitive()
        archive = self.root / "alias.tar.gz"
        self._owned_archive(archive, members)
        outer = self.root / "x"
        dest = outer / "y" / "dest"
        (outer / "y").mkdir(parents=True)
        with patch.object(recovery_fs, "_gnu_tar", return_value=False):
            with self.assertRaises(recovery.RecoveryError) as raised:
                recovery_fs.extract_tree(archive, dest, links="confined")
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

    def _hardlink_sentinels(self) -> tuple[Path, Path, list[Path]]:
        outside = self.root / "outside"
        outside.mkdir()
        sentinel = outside / "x"
        sentinel.write_bytes(b"outside sentinel\n")
        os.chmod(sentinel, 0o640)
        os.utime(sentinel, (1_000_000_000, 1_000_000_000))
        os.utime(outside, (1_000_000_000, 1_000_000_000))
        return outside, sentinel, [outside, sentinel]

    def _assert_hardlink_source_refused(self, run, dest: Path, observed: list[Path], name: str):
        before = self._observe(observed)
        with patch.object(recovery_fs, "_gnu_tar", return_value=False):
            with self.assertRaises(recovery.RecoveryError) as raised:
                run()
        self.assertEqual(self._observe(observed), before)
        self.assertEqual(observed[-1].lstat().st_nlink, 1, "hardlink created to an outside inode")
        self.assertFalse(os.path.lexists(dest / "h"))
        self.assertIn(f"unsafe hardlink source {name}", str(raised.exception))

    def test_case_alias_hardlink_source_symlink_is_refused(self):
        self._require_case_insensitive()
        _outside, sentinel, observed = self._hardlink_sentinels()
        archive = self.root / "alias-hardlink.tar.gz"
        self._owned_archive(
            archive,
            [(".", "dir", ""), ("f2", "link", str(sentinel)), ("h", "hardlink", "F2"), ("F2", "file", b"in\n")],
            links="verbatim",
        )
        dest = self.root / "dest"
        self._assert_hardlink_source_refused(
            lambda: recovery_fs.extract_tree(archive, dest, links="verbatim"), dest, observed, "F2"
        )

    def test_case_alias_hardlink_source_parent_symlink_is_refused(self):
        self._require_case_insensitive()
        outside, _sentinel, observed = self._hardlink_sentinels()
        archive = self.root / "alias-hardlink-parent.tar.gz"
        self._owned_archive(
            archive,
            [(".", "dir", ""), ("d", "link", str(outside)), ("h", "hardlink", "D/x"), ("D/x", "file", b"in\n")],
            links="verbatim",
        )
        dest = self.root / "dest"
        self._assert_hardlink_source_refused(
            lambda: recovery_fs.extract_tree(archive, dest, links="verbatim"), dest, observed, "D/x"
        )

    def test_python_extract_refuses_on_disk_symlink_hardlink_source(self):
        outside, sentinel, observed = self._hardlink_sentinels()
        cases = {"f": sentinel, "d/x": outside}
        for index, (source, link_target) in enumerate(cases.items()):
            with self.subTest(source=source):
                dest = self.root / f"dest-{index}"
                dest.mkdir()
                os.symlink(link_target, dest / Path(source).parts[0])
                archive = self.root / f"on-disk-{index}.tar.gz"
                members = self._owned_archive(
                    archive,
                    [(".", "dir", ""), ("h", "hardlink", source), (source, "file", b"in\n")],
                    links="verbatim",
                )
                self._assert_hardlink_source_refused(
                    lambda: recovery_fs._python_extract(archive, dest, members), dest, observed, source
                )

    def test_python_extract_refuses_missing_or_non_regular_hardlink_source(self):
        cases = {
            "f": [(".", "dir", ""), ("h", "hardlink", "f"), ("f", "file", b"in\n")],
            ".": [(".", "dir", ""), ("h", "hardlink", ".")],
        }
        for index, (source, members) in enumerate(cases.items()):
            with self.subTest(source=source):
                archive = self.root / f"source-order-{index}.tar.gz"
                self._owned_archive(archive, members, links="verbatim")
                dest = self.root / f"source-order-out-{index}"
                with patch.object(recovery_fs, "_gnu_tar", return_value=False):
                    with self.assertRaises(recovery.RecoveryError) as raised:
                        recovery_fs.extract_tree(archive, dest, links="verbatim")
                self.assertIn(f"unsafe hardlink source {source}", str(raised.exception))
                self.assertEqual(os.listdir(dest), [])

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

    def _assert_model_links_round_trip(self):
        self._round_trip_links(MODEL_LINKS, {"a/keep.txt": b"kept\n"}, policy="verbatim")
        records = recovery_fs.validate_archive(self.root / "links.tar.gz", links="verbatim")
        recorded = {record["name"]: record["linkname"] for record in records if record["type"] == "symlink"}
        self.assertEqual(recorded, MODEL_LINKS)

    def test_model_links_round_trip_verbatim_with_gnu_tar(self):
        self._require_gnu_tar()
        self._assert_model_links_round_trip()

    def test_model_links_round_trip_verbatim_with_python_fallback(self):
        with patch.object(recovery_fs, "_gnu_tar", return_value=False):
            self._assert_model_links_round_trip()

    def _non_utf8_source(self) -> Path:
        source = self.root / "non-utf8"
        source.mkdir()
        base = os.fsencode(source)
        try:
            with open(os.path.join(base, NON_UTF8_FILE), "wb") as stream:
                stream.write(b"gbk\n")
            os.mkdir(os.path.join(base, NON_UTF8_DIR))
            with open(os.path.join(base, NON_UTF8_DIR, b"inner.txt"), "wb") as stream:
                stream.write(b"inner\n")
            os.symlink(NON_UTF8_TARGET, os.path.join(base, b"link"))
        except OSError as exc:
            self.skipTest(f"filesystem refuses non-UTF-8 names: {exc}")
        return source

    def _assert_non_utf8_round_trip(self, *, capture_gnu: bool, extract_gnu: bool):
        source = self._non_utf8_source()
        if capture_gnu or extract_gnu:
            self._require_gnu_tar()
        archive = self.root / "non-utf8.tar.gz"
        with ExitStack() as stack:
            if not capture_gnu:
                stack.enter_context(patch.object(recovery_fs, "_gnu_tar", return_value=False))
            recovery_fs.capture_tree(source, archive, links="verbatim")
        records = {record["name"]: record for record in recovery_fs.validate_archive(archive, links="verbatim")}
        self.assertEqual(
            set(records),
            {".", os.fsdecode(NON_UTF8_FILE), os.fsdecode(NON_UTF8_DIR),
             os.fsdecode(NON_UTF8_DIR + b"/inner.txt"), "link"},
        )
        self.assertEqual(records["link"]["linkname"], os.fsdecode(NON_UTF8_TARGET))
        dest = self.root / "non-utf8-out"
        with ExitStack() as stack:
            if not extract_gnu:
                stack.enter_context(patch.object(recovery_fs, "_gnu_tar", return_value=False))
            recovery_fs.extract_tree(archive, dest, links="verbatim")
        out = os.fsencode(dest)
        self.assertEqual(sorted(os.listdir(out)), sorted([NON_UTF8_FILE, NON_UTF8_DIR, b"link"]))
        self.assertEqual(os.listdir(os.path.join(out, NON_UTF8_DIR)), [b"inner.txt"])
        with open(os.path.join(out, NON_UTF8_FILE), "rb") as stream:
            self.assertEqual(stream.read(), b"gbk\n")
        with open(os.path.join(out, NON_UTF8_DIR, b"inner.txt"), "rb") as stream:
            self.assertEqual(stream.read(), b"inner\n")
        self.assertEqual(os.readlink(os.path.join(out, b"link")), NON_UTF8_TARGET)

    def test_non_utf8_names_round_trip_with_gnu_tar(self):
        self._assert_non_utf8_round_trip(capture_gnu=True, extract_gnu=True)

    def test_non_utf8_names_round_trip_with_python_fallback(self):
        self._assert_non_utf8_round_trip(capture_gnu=False, extract_gnu=False)

    def test_non_utf8_names_gnu_capture_python_extract(self):
        self._assert_non_utf8_round_trip(capture_gnu=True, extract_gnu=False)

    def test_non_utf8_names_python_capture_gnu_extract(self):
        self._assert_non_utf8_round_trip(capture_gnu=False, extract_gnu=True)

    def _link_target_sentinels(self) -> tuple[Path, dict[str, str], list[Path]]:
        outer = self.root / "outer"
        abs_dir = outer / "abs-dir"
        abs_dir.mkdir(parents=True)
        abs_file = outer / "abs-file"
        abs_file.write_bytes(b"absolute file sentinel\n")
        (abs_dir / "entry").write_bytes(b"directory sentinel\n")
        restore = self.root / "restore"
        dest = restore / "dest"
        dest.mkdir(parents=True)
        outside = restore / "outside"
        outside.write_bytes(b"relative sentinel\n")
        for path in (abs_file, abs_dir / "entry", outside):
            os.chmod(path, 0o640)
        os.chmod(abs_dir, 0o750)
        for path in (abs_file, abs_dir / "entry", outside, abs_dir, outer, restore):
            os.utime(path, (1_000_000_000, 1_000_000_000))
        links = {
            "bin/python3": str(abs_file),
            ".claude/agents/x.md": str(abs_file),
            ".claude/skills/s": str(abs_dir) + "/",
            "a/x": "../../outside",
            "x": "..",
        }
        return dest, links, [outer, abs_file, abs_dir, abs_dir / "entry", restore, outside]

    def _observe(self, paths: list[Path]) -> dict:
        observed = {}
        for path in paths:
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                content = sorted(os.listdir(path))
            else:
                content = path.read_bytes()
            observed[str(path)] = (
                stat.S_IFMT(info.st_mode),
                stat.S_IMODE(info.st_mode),
                info.st_uid,
                info.st_gid,
                info.st_mtime_ns,
                content,
            )
        return observed

    def _model_archive(self, links: dict[str, str]) -> Path:
        source = self.root / "model-source"
        (source / "bin").mkdir(parents=True)
        (source / "bin" / "tool").write_bytes(b"tool\n")
        for name, target in links.items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, path)
        entries = sorted(source.rglob("*"), key=lambda path: len(path.parts), reverse=True) + [source]
        for path in entries:
            if not path.is_symlink():
                os.chmod(path, 0o700 if path.is_dir() else 0o600)
            if os.geteuid() == 0:
                os.lchown(path, 4242, 4242)
            os.utime(path, (2_000_000_000, 2_000_000_000), follow_symlinks=False)
        archive = self.root / "model.tar.gz"
        recovery_fs.capture_tree(source, archive, links="verbatim")
        return archive

    def _prepare_model_extract(self) -> tuple:
        dest, links, sentinels = self._link_target_sentinels()
        before = self._observe(sentinels)
        return dest, links, sentinels, before, self._model_archive(links)

    def _assert_extract_touches_nothing(self, prepared: tuple) -> Path:
        dest, links, sentinels, before, archive = prepared
        recovery_fs.extract_tree(archive, dest, links="verbatim")
        self.assertEqual(self._observe(sentinels), before)
        for name, target in links.items():
            self.assertTrue((dest / name).is_symlink(), name)
            self.assertEqual(os.readlink(dest / name), target, name)
        return dest

    def test_gnu_extract_writes_through_a_premade_link_positive_control(self):
        self._require_root_gnu_tar()
        target = self.root / "control-target"
        target.mkdir()
        dest = self.root / "control-dest"
        dest.mkdir()
        os.symlink(target, dest / "l")
        archive = self.root / "control.tar.gz"
        with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as bundle:
            evil = tarfile.TarInfo(name="l/evil")
            payload = b"written through the link\n"
            evil.size = len(payload)
            bundle.addfile(evil, io.BytesIO(payload))
        recovery_fs._run_tar([*recovery_fs.EXTRACT_FLAGS, str(archive), "-C", str(dest)])
        self.assertEqual((target / "evil").read_bytes(), payload)

    def test_gnu_extract_touches_nothing_through_verbatim_links(self):
        self._require_root_gnu_tar()
        real_run_tar = recovery_fs._run_tar
        argvs = []

        def recording_run_tar(argv, **kwargs):
            argvs.append(list(argv))
            return real_run_tar(argv, **kwargs)

        prepared = self._prepare_model_extract()
        with patch.object(recovery_fs, "_run_tar", side_effect=recording_run_tar):
            self._assert_extract_touches_nothing(prepared)
        self.assertTrue(
            any(argv[: len(recovery_fs.EXTRACT_FLAGS)] == list(recovery_fs.EXTRACT_FLAGS) for argv in argvs),
            argvs,
        )

    def _require_root_gnu_tar(self):
        if os.environ.get("OCU_TEST_EXPECT_ROOT") == "1":
            self.assertTrue(recovery_fs._gnu_tar(), "OCU_TEST_EXPECT_ROOT=1 requires GNU tar")
            self.assertEqual(os.geteuid(), 0, "OCU_TEST_EXPECT_ROOT=1 requires running as root")
        self._require_gnu_tar()

    def test_python_extract_touches_nothing_through_verbatim_links(self):
        calls = []

        def spy(name, real):
            def record(path, *args, **kwargs):
                calls.append((name, os.fspath(path)))
                return real(path, *args, **kwargs)

            return record

        spies = [
            patch.object(os, name, spy(name, getattr(os, name)))
            for name in ("lchown", "chown", "utime", "chmod", "setxattr")
            if hasattr(os, name)
        ]
        spies.append(patch("recovery_fs.open", spy("open", open), create=True))
        with patch.object(recovery_fs, "_gnu_tar", return_value=False), ExitStack() as stack:
            prepared = self._prepare_model_extract()
            for active in spies:
                stack.enter_context(active)
            dest = self._assert_extract_touches_nothing(prepared)
        root = os.path.realpath(dest)
        link_owners = set()
        for name, path in calls:
            if name == "lchown" and os.path.islink(path):
                link_owners.add(os.path.relpath(path, dest))
                continue
            self.assertFalse(os.path.islink(path), (name, path))
            resolved = os.path.realpath(path)
            self.assertTrue(resolved == root or resolved.startswith(root + os.sep), (name, path))
        self.assertEqual(link_owners, set(MODEL_LINKS))
        self.assertIn(("open", str(dest / "bin" / "tool")), calls)

    def test_structural_rules_still_refuse_under_verbatim(self):
        cases = {
            "member-below-absolute-link": [("l", "link", "/tmp"), ("l/evil", "file", b"pwned\n")],
            "hardlink-to-symlink": [("s", "link", "/usr/bin/python3"), ("h", "hardlink", "s")],
            "hardlink-absolute": [("h", "hardlink", "/etc/passwd")],
            "hardlink-dotdot": [("f", "file", b"f\n"), ("h", "hardlink", "../f")],
            "device": [("null", "device", "")],
            "empty-link-target": [("e", "link", "")],
        }
        for index, (case, members) in enumerate(cases.items()):
            with self.subTest(case=case):
                archive = self.root / f"structural-{index}.tar.gz"
                with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as bundle:
                    root = tarfile.TarInfo(name=".")
                    root.type = tarfile.DIRTYPE
                    bundle.addfile(root)
                    for name, kind, value in members:
                        member = tarfile.TarInfo(name=name)
                        if kind == "file":
                            member.size = len(value)
                            bundle.addfile(member, io.BytesIO(value))
                            continue
                        member.type = {
                            "link": tarfile.SYMTYPE,
                            "hardlink": tarfile.LNKTYPE,
                            "device": tarfile.CHRTYPE,
                        }[kind]
                        member.linkname = value
                        bundle.addfile(member)
                with self.assertRaises(recovery.RecoveryError):
                    recovery_fs.validate_archive(archive, links="verbatim")
                dest = self.root / f"structural-out-{index}"
                dest.mkdir()
                with self.assertRaises(recovery.RecoveryError):
                    recovery_fs.extract_tree(archive, dest, links="verbatim")
                self.assertEqual(os.listdir(dest), [])

    def test_link_policy_is_required_and_known(self):
        source = self.root / "plain"
        source.mkdir()
        (source / "f").write_bytes(b"plain\n")
        archive = self.root / "plain.tar.gz"
        recovery_fs.capture_tree(source, archive, links="confined")
        unused = self.root / "unused.tar.gz"
        dest = self.root / "plain-out"
        calls = {
            "capture_tree": lambda **policy: recovery_fs.capture_tree(source, unused, **policy),
            "validate_archive": lambda **policy: recovery_fs.validate_archive(archive, **policy),
            "inspect_archive": lambda **policy: recovery_fs.inspect_archive(archive, **policy),
            "extract_tree": lambda **policy: recovery_fs.extract_tree(archive, dest, **policy),
            "archive_has_regular_root_member": lambda **policy: recovery_fs.archive_has_regular_root_member(
                archive, "f", **policy
            ),
        }
        for name, call in calls.items():
            with self.subTest(function=name):
                with self.assertRaises(TypeError):
                    call()
                with self.assertRaises(ValueError) as raised:
                    call(links="bogus")
                self.assertIn("unknown link policy", str(raised.exception))
        self.assertFalse(unused.exists())
        self.assertFalse(dest.exists())

    def test_helper_cli_requires_a_known_link_policy(self):
        import recovery_helper

        source = self.root / "helper-source"
        (source / "bin").mkdir(parents=True)
        os.symlink("/usr/bin/python3", source / "bin" / "python3")
        archive = self.root / "helper.tar.gz"
        for argv in (
            ["capture", str(source), str(archive)],
            ["capture", "--links=bogus", str(source), str(archive)],
            ["extract", str(archive), str(self.root / "never")],
        ):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    recovery_helper.main(argv)
                self.assertEqual(raised.exception.code, 2)
        self.assertFalse(archive.exists())
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery_helper.main(["capture", "--links=confined", str(source), str(archive)])
        self.assertIn("unsafe symlink", str(raised.exception))
        archive.unlink(missing_ok=True)
        self.assertEqual(recovery_helper.main(["capture", "--links=verbatim", str(source), str(archive)]), 0)
        dest = self.root / "helper-out"
        self.assertEqual(recovery_helper.main(["extract", "--links=verbatim", str(archive), str(dest)]), 0)
        self.assertEqual(os.readlink(dest / "bin" / "python3"), "/usr/bin/python3")









if __name__ == "__main__":
    unittest.main()
