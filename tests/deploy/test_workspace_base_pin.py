# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""The workspace recipe names its base image by an immutable digest."""

from __future__ import annotations

from pathlib import Path
import re
import sys
import tempfile
import unittest

from support import ROOT

sys.path.insert(0, str(ROOT / "deploy"))
import release


class WorkspaceBasePinTests(unittest.TestCase):
    def test_root_recipe_names_its_base_by_digest(self):
        self.assertRegex(release.workspace_base(ROOT), r"\Aubuntu@sha256:[0-9a-f]{64}\Z")

    def test_undigested_root_recipe_is_refused_naming_the_line(self):
        text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        undigested, count = re.subn(
            r"^FROM ubuntu:24\.04@sha256:[0-9a-f]+$", "FROM ubuntu:24.04", text,
            flags=re.MULTILINE,
        )
        self.assertEqual(count, 1)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "Dockerfile").write_text(undigested, encoding="utf-8")
            with self.assertRaises(release.ReleaseError) as caught:
                release.workspace_base(Path(tmp))
        message = str(caught.exception)
        self.assertTrue(message.startswith("workspace recipe"), message)
        self.assertIn("FROM ubuntu:24.04", message)


if __name__ == "__main__":
    unittest.main()
