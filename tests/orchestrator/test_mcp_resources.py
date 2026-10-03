# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""
Tier 6 — uploaded files as MCP resources.

Exercises:
  - sync_chat_resources + list_resources for flat and nested paths
  - read_resource returning text for text/*, bytes for everything else
  - tenancy: a fresh chat has no inherited resources
  - idempotent re-sync (no duplicate registrations)
  - concurrency safety: sync while list_resources iterates
"""
import asyncio
import io
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent.parent


class McpResourcesContract(unittest.TestCase):
    """Needs BASE_DATA_DIR set BEFORE uploads/mcp_resources are imported."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="ocu-resource-test-")
        os.environ["BASE_DATA_DIR"] = cls._tmp

        sys.path.insert(0, str(ROOT / "computer-use-server"))
        # Fresh imports so BASE_DATA_DIR is picked up
        import uploads as uploads_mod
        import importlib
        importlib.reload(uploads_mod)

        import mcp_tools  # noqa: F401 — the singleton
        import mcp_resources as mr
        importlib.reload(mr)

        cls.mcp_tools = mcp_tools
        cls.mcp_resources = mr
        cls.uploads = uploads_mod

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _make_upload(self, chat_id: str, rel_path: str, content: str | bytes):
        target = Path(self._tmp) / chat_id / "outputs" / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)

    def test_flat_and_nested_list_read(self):
        self._make_upload("demoa", "hello.txt", "hi")
        self._make_upload("demoa", "sub/nested.json", '{"k":1}')
        n = asyncio.run(self.mcp_resources.sync_chat_resources("demoa"))
        self.assertEqual(n, 2)

        resources = asyncio.run(self.mcp_tools.mcp.list_resources())
        uris = {str(r.uri) for r in resources}
        self.assertIn("file://uploads/demoa/hello.txt", uris)
        self.assertIn("file://uploads/demoa/sub%2Fnested.json", uris)

        from pydantic import AnyUrl
        flat = list(asyncio.run(self.mcp_tools.mcp.read_resource(
            AnyUrl("file://uploads/demoa/hello.txt"))))
        self.assertEqual(flat[0].content, "hi")
        nested = list(asyncio.run(self.mcp_tools.mcp.read_resource(
            AnyUrl("file://uploads/demoa/sub%2Fnested.json"))))
        self.assertEqual(nested[0].content, '{"k":1}')

    def test_docx_resource_reads_workspace_outputs_bytes(self):
        from pydantic import AnyUrl

        docx_bytes = b"PK\x03\x04docx-fixture"
        self._make_upload("demoe", "brief.docx", docx_bytes)
        n = asyncio.run(self.mcp_resources.sync_chat_resources("demoe"))
        self.assertEqual(n, 1)

        listed = {str(r.uri) for r in asyncio.run(self.mcp_tools.mcp.list_resources())}
        self.assertIn("file://uploads/demoe/brief.docx", listed)
        chunks = list(asyncio.run(self.mcp_tools.mcp.read_resource(
            AnyUrl("file://uploads/demoe/brief.docx"))))
        payload = chunks[0]
        self.assertEqual(payload.content, docx_bytes)

    def test_hidden_names_and_ocu_paths_are_not_listed(self):
        self._make_upload("demof", "visible.txt", "ok")
        self._make_upload("demof", ".secret.txt", "hidden-file")
        self._make_upload("demof", "nested/.hidden/notes.txt", "hidden-dir")
        self._make_upload("demof", ".ocu/imports.json", '{"ids":[]}')
        n = asyncio.run(self.mcp_resources.sync_chat_resources("demof"))
        self.assertEqual(n, 1)

        uris = {str(r.uri) for r in asyncio.run(self.mcp_tools.mcp.list_resources())
                if "demof" in str(r.uri)}
        self.assertEqual(uris, {"file://uploads/demof/visible.txt"})


    def test_tenancy_empty_for_unknown_chat(self):
        # Plant a real upload for tenant A so the global resource registry
        # has at least one entry. Then assert tenant B sees ZERO resources
        # AND the registry-level list contains tenant A's URI but not any
        # tenant-B URI — proves there is no leak across chats via the
        # shared FastMCP._resource_manager._resources dict.
        self._make_upload("tenant-a", "secret.txt", "A-only")
        asyncio.run(self.mcp_resources.sync_chat_resources("tenant-a"))

        n = asyncio.run(self.mcp_resources.sync_chat_resources("tenant-b"))
        self.assertEqual(n, 0)

        all_uris = {
            str(r.uri) for r in asyncio.run(self.mcp_tools.mcp.list_resources())
        }
        # Tenant A's resource must still be present (sync_chat_resources(B)
        # must not have wiped A).
        self.assertIn("file://uploads/tenant-a/secret.txt", all_uris)
        # And NO resource URI should mention tenant-b.
        leaked = [u for u in all_uris if "tenant-b" in u]
        self.assertEqual(leaked, [], f"tenant-b leaked URIs: {leaked}")

    def test_idempotent_resync(self):
        self._make_upload("demob", "once.txt", "only")
        asyncio.run(self.mcp_resources.sync_chat_resources("demob"))
        asyncio.run(self.mcp_resources.sync_chat_resources("demob"))
        resources = asyncio.run(self.mcp_tools.mcp.list_resources())
        demob_uris = [r for r in resources if "demob" in str(r.uri)]
        self.assertEqual(len(demob_uris), 1, "re-sync must not duplicate entries")

    def test_concurrent_sync_and_list(self):
        """Stress: sync + list in parallel. Without the lock around the
        clear-then-rebuild, `dict changed size during iteration` fires."""
        self._make_upload("democ", "a.txt", "A")
        self._make_upload("democ", "b.txt", "B")

        async def _stress():
            await self.mcp_resources.sync_chat_resources("democ")
            tasks = []
            for _ in range(20):
                tasks.append(self.mcp_resources.sync_chat_resources("democ"))
                tasks.append(self.mcp_tools.mcp.list_resources())
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    raise r
            return len(results)

        n = asyncio.run(_stress())
        self.assertEqual(n, 40)

    def test_list_changed_notification_skipped_outside_request_context(self):
        """sync_chat_resources is called from docker_manager._create_container
        on a worker thread — no `request_ctx` is set, and we must NOT blow up.
        Notification is silently skipped; fresh list still surfaces on the
        next resources/list call."""
        self._make_upload("demod", "skip.txt", "no ctx")
        # Confirm we actually have no request context right now
        from mcp.server.lowlevel.server import request_ctx
        self.assertRaises(LookupError, request_ctx.get)
        # Should not raise — graceful skip of the notification branch
        n = asyncio.run(self.mcp_resources.sync_chat_resources("demod"))
        self.assertEqual(n, 1)

    def test_read_rejects_static_symlink_leaf_and_does_not_return_outside_bytes(self):
        from fastapi import HTTPException

        chat_id = "readleaf"
        sentinel = b"OUTSIDE-SENTINEL-LEAF"
        outside = Path(self._tmp) / "outside-leaf.bin"
        outside.write_bytes(sentinel)
        self._make_upload(chat_id, "visible.txt", "inside-ok")
        planted = Path(self._tmp) / chat_id / "outputs" / "leak.bin"
        planted.symlink_to(outside)

        n = asyncio.run(self.mcp_resources.sync_chat_resources(chat_id))
        self.assertGreaterEqual(n, 1)

        data, _mime = self.uploads.read_chat_upload(chat_id, "visible.txt")
        self.assertEqual(data, b"inside-ok")

        with self.assertRaises(HTTPException) as raised:
            self.uploads.read_chat_upload(chat_id, "leak.bin")
        self.assertEqual(raised.exception.status_code, 403)

        from pydantic import AnyUrl
        visible = list(asyncio.run(self.mcp_tools.mcp.read_resource(
            AnyUrl(f"file://uploads/{chat_id}/visible.txt"))))
        self.assertEqual(visible[0].content, "inside-ok")
        with self.assertRaises(Exception):
            asyncio.run(self.mcp_tools.mcp.read_resource(
                AnyUrl(f"file://uploads/{chat_id}/leak.bin")))
        self.assertEqual(outside.read_bytes(), sentinel)

    def test_read_after_validated_ancestor_swap_does_not_return_outside_sentinel(self):
        from fastapi import HTTPException

        chat_id = "readswap"
        sentinel = b"OUTSIDE-SENTINEL-ANCESTOR"
        decoy = b"inside-decoy"
        outside_root = Path(self._tmp) / "outside-read-tree"
        outside_nested = outside_root / "nested"
        outside_nested.mkdir(parents=True)
        (outside_nested / "env").write_bytes(sentinel)

        self._make_upload(chat_id, "nested/env", decoy)
        outputs = Path(self._tmp) / chat_id / "outputs"
        nested = outputs / "nested"
        backup = outputs / "nested.aside"
        outside_nested_realpath = os.path.realpath(str(outside_nested))

        n = asyncio.run(self.mcp_resources.sync_chat_resources(chat_id))
        self.assertEqual(n, 1)

        opened = os.open
        io_open = io.open
        swapped = {"done": False}

        def _same_inode(left, right):
            return left.st_dev == right.st_dev and left.st_ino == right.st_ino

        def matches_nested(args, kwargs):
            try:
                expected = os.lstat(nested)
            except FileNotFoundError:
                return False
            dir_fd = kwargs.get("dir_fd")
            if dir_fd is not None:
                try:
                    observed = os.fstat(dir_fd)
                except OSError:
                    observed = None
                if observed is not None and _same_inode(observed, expected):
                    return True
            if not args:
                return False
            candidate = args[0]
            if not isinstance(candidate, (str, bytes, os.PathLike)):
                return False
            text = os.fspath(candidate)
            if os.path.isabs(text):
                try:
                    resolved = os.path.realpath(text)
                except OSError:
                    return False
                return resolved in {
                    os.path.realpath(str(nested / "env")),
                    os.path.realpath(str(nested)),
                }
            if dir_fd is not None and os.path.basename(text) == "env":
                try:
                    parent = os.fstat(dir_fd)
                    ancestor = os.lstat(nested)
                except OSError:
                    return False
                return _same_inode(parent, ancestor)
            return False

        def swap_nested():
            if swapped["done"] or nested.is_symlink() or not nested.is_dir():
                return
            swapped["done"] = True
            nested.rename(backup)
            nested.symlink_to(outside_nested_realpath, target_is_directory=True)

        def swapping_open(*args, **kwargs):
            fd = opened(*args, **kwargs)
            if matches_nested(args, kwargs):
                swap_nested()
            return fd

        def swapping_file_open(*args, **kwargs):
            if args and matches_nested(args[:1], kwargs):
                swap_nested()
            return io_open(*args, **kwargs)

        with (
            patch("os.open", swapping_open),
            patch("io.open", swapping_file_open),
            patch("builtins.open", swapping_file_open),
        ):
            try:
                data, _mime = self.uploads.read_chat_upload(chat_id, "nested/env")
            except HTTPException as orig:
                self.assertEqual(orig.status_code, 403)
                data = None
            else:
                self.assertEqual(data, decoy)

        self.assertTrue(swapped["done"])
        self.assertNotEqual(data, sentinel)
        self.assertEqual((outside_nested / "env").read_bytes(), sentinel)
        if data is not None:
            self.assertEqual(data, decoy)
        if backup.exists():
            self.assertEqual((backup / "env").read_bytes(), decoy)
        else:
            self.assertEqual((nested / "env").read_bytes(), decoy)
            self.assertFalse(nested.is_symlink())

    def test_list_omits_symlink_file_dir_and_cycle_and_keeps_real_nested_files(self):
        chat_id = "listsym"
        outside_file = Path(self._tmp) / "outside-listed.bin"
        outside_file.write_bytes(b"OUTSIDE-LIST-FILE")
        outside_dir = Path(self._tmp) / "outside-listed-dir"
        outside_dir.mkdir()
        (outside_dir / "secret.txt").write_bytes(b"OUTSIDE-LIST-DIR")

        self._make_upload(chat_id, "visible.txt", "keep-me")
        self._make_upload(chat_id, "nested/real.txt", "nested-keep")
        outputs = Path(self._tmp) / chat_id / "outputs"
        (outputs / "leak.bin").symlink_to(outside_file)
        (outputs / "x").symlink_to(outside_dir, target_is_directory=True)
        (outputs / "loop").symlink_to(".", target_is_directory=True)
        fifo = outputs / "block.fifo"
        os.mkfifo(fifo)

        listed = self.uploads.list_chat_uploads(chat_id)
        rels = {entry.rel_path for entry in listed}
        self.assertEqual(rels, {"visible.txt", "nested/real.txt"})
        sizes = {entry.rel_path: entry.size for entry in listed}
        self.assertEqual(sizes["visible.txt"], len("keep-me"))
        self.assertEqual(sizes["nested/real.txt"], len("nested-keep"))
        self.assertTrue(stat.S_ISFIFO(os.lstat(fifo).st_mode))

        n = asyncio.run(self.mcp_resources.sync_chat_resources(chat_id))
        self.assertEqual(n, 2)
        uris = {str(resource.uri) for resource in asyncio.run(self.mcp_tools.mcp.list_resources())
                if chat_id in str(resource.uri)}
        self.assertEqual(uris, {
            f"file://uploads/{chat_id}/visible.txt",
            f"file://uploads/{chat_id}/nested%2Freal.txt",
        })


if __name__ == "__main__":
    unittest.main()
