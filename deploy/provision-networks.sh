#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
#
# Provision or validate the dedicated sandbox bridge. Never delete, replace,
# disconnect, or silently accept an incompatible network.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$ROOT/deploy${PYTHONPATH:+:$PYTHONPATH}"


python3 - <<'PY'
from __future__ import annotations

import os
import sys

from netinspect import NetworkInspectError, compatible, docker, inspect_network, parse_ipv4_address, parse_ipv4_network


def fail(message: str) -> None:
    print(f"provision-networks: {message}", file=sys.stderr)
    raise SystemExit(1)


def require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        fail(f"{name} is required")
    return value


def parse_required_network(name: str):
    try:
        return parse_ipv4_network(name, require(name))
    except NetworkInspectError as exc:
        fail(str(exc))


def parse_required_address(name: str):
    try:
        return parse_ipv4_address(name, require(name))
    except NetworkInspectError as exc:
        fail(str(exc))


def inspect(name: str):
    try:
        return inspect_network(name)
    except NetworkInspectError as exc:
        fail(str(exc))


def require_compatible(payload: dict, *, name: str, subnet, gateway) -> None:
    try:
        compatible(payload, name=name, subnet=subnet, gateway=gateway)
    except NetworkInspectError as exc:
        fail(str(exc))


control_name = require("OCU_PRIVATE_NETWORK")
sandbox_name = require("OCU_SANDBOX_NETWORK")
control_subnet = parse_required_network("OCU_PRIVATE_SUBNET")
sandbox_subnet = parse_required_network("OCU_SANDBOX_SUBNET")
control_gateway = parse_required_address("OCU_PRIVATE_GATEWAY")
sandbox_gateway = parse_required_address("OCU_SANDBOX_GATEWAY")

if control_name == sandbox_name:
    fail("control-plane and sandbox network names must differ")
if control_name.lower() in {"bridge", "host", "none"} or sandbox_name.lower() in {"bridge", "host", "none"}:
    fail("reserved Docker network names are forbidden")
if control_subnet.overlaps(sandbox_subnet):
    fail("control-plane and sandbox subnets overlap")
if control_gateway not in control_subnet:
    fail("OCU_PRIVATE_GATEWAY is outside OCU_PRIVATE_SUBNET")
if sandbox_gateway not in sandbox_subnet:
    fail("OCU_SANDBOX_GATEWAY is outside OCU_SANDBOX_SUBNET")

control = inspect(control_name)
if control is not None:
    require_compatible(control, name=control_name, subnet=control_subnet, gateway=control_gateway)

sandbox = inspect(sandbox_name)
if sandbox is not None:
    require_compatible(sandbox, name=sandbox_name, subnet=sandbox_subnet, gateway=sandbox_gateway)
    raise SystemExit(0)

created = docker(
    "network",
    "create",
    "--driver",
    "bridge",
    "--subnet",
    str(sandbox_subnet),
    "--gateway",
    str(sandbox_gateway),
    sandbox_name,
)
if created.returncode != 0:
    combined = ((created.stderr or "") + (created.stdout or "")).lower()
    if "already exists" not in combined:
        fail(f"{sandbox_name}: create failed")

sandbox = inspect(sandbox_name)
if sandbox is None:
    fail(f"{sandbox_name}: inspect failed after create")
require_compatible(sandbox, name=sandbox_name, subnet=sandbox_subnet, gateway=sandbox_gateway)
raise SystemExit(0)
PY
