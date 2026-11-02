# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Documented sandbox resource defaults match the code: 4 GB and 2 CPUs.

The code defaults are tested at container creation in
tests/orchestrator/test_lifecycle.py and the deployment bootstrap in
tests/deploy/test_bootstrap_runtime.py.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _text(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


def test_server_readme_lists_the_defaults():
    readme = _text("computer-use-server/README.md")
    assert re.search(r"^\| `CONTAINER_MEM_LIMIT` \| `4g` \|", readme, re.MULTILINE)
    assert re.search(r"^\| `CONTAINER_CPU_LIMIT` \| `2\.0` \|", readme, re.MULTILINE)


def test_env_example_lists_the_defaults():
    lines = _text(".env.example").splitlines()
    assert "# CONTAINER_MEM_LIMIT=4g" in lines
    assert "# CONTAINER_CPU_LIMIT=2.0" in lines


def test_helm_values_use_the_defaults():
    values = _text("helm/computer-use-server/values.yaml").splitlines()
    assert '    CONTAINER_MEM_LIMIT: "4g"' in values
    assert '    CONTAINER_CPU_LIMIT: "2.0"' in values
