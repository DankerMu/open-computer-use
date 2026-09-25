#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
#
# Provision or validate the dedicated sandbox bridge. Never delete, replace,
# disconnect, or silently accept an incompatible network.

set -euo pipefail

python3 - <<'PY'
from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import sys


def fail(message: str) -> None:
    print(f"provision-networks: {message}", file=sys.stderr)
    raise SystemExit(1)


def require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        fail(f"{name} is required")
    return value


def parse_ipv4_network(name: str) -> ipaddress.IPv4Network:
    raw = require(name)
    try:
        network = ipaddress.ip_network(raw, strict=True)
    except ValueError:
        fail(f"{name} is not an IPv4 network")
    if network.version != 4:
        fail(f"{name} is not an IPv4 network")
    return network


def parse_ipv4_address(name: str) -> ipaddress.IPv4Address:
    raw = require(name)
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        fail(f"{name} is not an IPv4 address")
    if address.version != 4:
        fail(f"{name} is not an IPv4 address")
    return address


def docker(*args: str):
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def inspect(name: str):
    result = docker("network", "inspect", name, "--format", "{{json .}}")
    if result.returncode == 0:
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            fail(f"{name}: inspect returned malformed JSON")
        if isinstance(payload, list):
            if len(payload) != 1 or not isinstance(payload[0], dict):
                fail(f"{name}: inspect returned an unexpected document")
            return payload[0]
        if not isinstance(payload, dict):
            fail(f"{name}: inspect returned an unexpected document")
        return payload
    combined = (result.stderr or "") + (result.stdout or "")
    lowered = combined.lower()
    if f"no such network: {name.lower()}" in lowered or f"network {name.lower()} not found" in lowered:
        return None
    fail(f"{name}: inspect failed")


def ipam_config(payload: dict) -> dict:
    ipam = payload.get("IPAM") or {}
    configs = ipam.get("Config") or []
    if not isinstance(configs, list) or len(configs) != 1 or not isinstance(configs[0], dict):
        fail("existing network IPAM is incompatible")
    return configs[0]


def compatible(payload: dict, *, name: str, subnet: ipaddress.IPv4Network, gateway: ipaddress.IPv4Address) -> None:
    inspected_name = str(payload.get("Name") or "")
    driver = str(payload.get("Driver") or "").lower()
    internal = payload.get("Internal")
    if inspected_name != name:
        fail(f"{name}: existing network name is incompatible")
    if driver != "bridge":
        fail(f"{name}: existing network driver is incompatible")
    if internal is not False:
        fail(f"{name}: existing network is not explicitly non-internal")
    config = ipam_config(payload)
    existing_subnet = str(config.get("Subnet") or "")
    existing_gateway = str(config.get("Gateway") or "")
    try:
        same_subnet = ipaddress.ip_network(existing_subnet, strict=False) == subnet
    except ValueError:
        same_subnet = False
    if not same_subnet:
        fail(f"{name}: existing network subnet is incompatible")
    if existing_gateway != str(gateway):
        fail(f"{name}: existing network gateway is incompatible")


control_name = require("OCU_PRIVATE_NETWORK")
sandbox_name = require("OCU_SANDBOX_NETWORK")
control_subnet = parse_ipv4_network("OCU_PRIVATE_SUBNET")
sandbox_subnet = parse_ipv4_network("OCU_SANDBOX_SUBNET")
control_gateway = parse_ipv4_address("OCU_PRIVATE_GATEWAY")
sandbox_gateway = parse_ipv4_address("OCU_SANDBOX_GATEWAY")

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
    compatible(control, name=control_name, subnet=control_subnet, gateway=control_gateway)

sandbox = inspect(sandbox_name)
if sandbox is not None:
    compatible(sandbox, name=sandbox_name, subnet=sandbox_subnet, gateway=sandbox_gateway)
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
compatible(sandbox, name=sandbox_name, subnet=sandbox_subnet, gateway=sandbox_gateway)
raise SystemExit(0)
PY
