# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Documented sandbox resource defaults: 4 GB and 2 CPUs, except helm.

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


def _helm_env(name):
    values = _text("helm/computer-use-server/values.yaml")
    match = re.search(rf'^    {name}: "([^"]+)"', values, re.MULTILINE)
    assert match, name
    return match.group(1)


def _podman_limits():
    values = _text("helm/computer-use-server/values.yaml")
    block = re.search(r"^podman:\n(.*?)^\S", values, re.MULTILINE | re.DOTALL).group(1)
    limits = re.search(
        r'^    limits:\n      cpu: "([^"]+)"\n      memory: (\S+)', block, re.MULTILINE
    )
    assert limits, "podman resource limits not found"
    return limits.group(1), limits.group(2)


def _gibibytes(quantity):
    """Docker (`2g`) or Kubernetes (`4Gi`) memory quantity in GiB."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(g|Gi|m|Mi)", str(quantity))
    assert match, quantity
    number, unit = float(match.group(1)), match.group(2)
    return number / 1024 if unit in ("m", "Mi") else number


def test_helm_keeps_one_cpu_and_two_gigabytes():
    # Every helm sandbox runs inside the one podman sidecar, so helm keeps
    # the former limits instead of the 2 CPU / 4 GB code default.
    assert _helm_env("CONTAINER_MEM_LIMIT") == "2g"
    assert _helm_env("CONTAINER_CPU_LIMIT") == "1.0"


def test_helm_sandbox_limit_fits_inside_the_podman_sidecar():
    sidecar_cpu, sidecar_memory = _podman_limits()
    assert _gibibytes(_helm_env("CONTAINER_MEM_LIMIT")) <= _gibibytes(sidecar_memory)
    assert float(_helm_env("CONTAINER_CPU_LIMIT")) <= float(sidecar_cpu)
