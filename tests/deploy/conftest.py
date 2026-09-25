# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Expose sibling test support under pytest --import-mode=importlib."""

from pathlib import Path
import sys

TEST_DIR = str(Path(__file__).resolve().parent)
if TEST_DIR not in sys.path:
    sys.path.insert(0, TEST_DIR)
