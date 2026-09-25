# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Shared Docker-network inspect and IPv4 IPAM validation for deploy scripts."""

from __future__ import annotations

import ipaddress
import json
import subprocess


class NetworkInspectError(Exception):
    """Neutral inspect/IPAM failure; callers wrap with their own prefix."""


def parse_ipv4_network(name: str, raw: str) -> ipaddress.IPv4Network:
    try:
        network = ipaddress.ip_network(raw, strict=True)
    except ValueError as exc:
        raise NetworkInspectError(f"{name} is not an IPv4 network") from exc
    if network.version != 4:
        raise NetworkInspectError(f"{name} is not an IPv4 network")
    return network


def parse_ipv4_address(name: str, raw: str) -> ipaddress.IPv4Address:
    try:
        address = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise NetworkInspectError(f"{name} is not an IPv4 address") from exc
    if address.version != 4:
        raise NetworkInspectError(f"{name} is not an IPv4 address")
    return address


def docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def inspect_network(name: str) -> dict | None:
    result = docker("network", "inspect", name, "--format", "{{json .}}")
    if result.returncode == 0:
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise NetworkInspectError(f"{name}: inspect returned malformed JSON") from exc
        if isinstance(payload, list):
            if len(payload) != 1 or not isinstance(payload[0], dict):
                raise NetworkInspectError(f"{name}: inspect returned an unexpected document")
            return payload[0]
        if not isinstance(payload, dict):
            raise NetworkInspectError(f"{name}: inspect returned an unexpected document")
        return payload
    combined = (result.stderr or "") + (result.stdout or "")
    lowered = combined.lower()
    if f"no such network: {name.lower()}" in lowered or f"network {name.lower()} not found" in lowered:
        return None
    detail = combined.strip() or "nonzero status"
    raise NetworkInspectError(f"{name}: inspect failed: {detail}")


def ipam_config(payload: dict) -> dict:
    ipam = payload.get("IPAM") or {}
    configs = ipam.get("Config") or []
    if not isinstance(configs, list) or len(configs) != 1 or not isinstance(configs[0], dict):
        raise NetworkInspectError("existing network IPAM is incompatible")
    return configs[0]


def compatible(
    payload: dict,
    *,
    name: str,
    subnet: ipaddress.IPv4Network,
    gateway: ipaddress.IPv4Address,
) -> None:
    inspected_name = str(payload.get("Name") or "")
    driver = str(payload.get("Driver") or "").lower()
    internal = payload.get("Internal")
    if inspected_name != name:
        raise NetworkInspectError(f"{name}: existing network name is incompatible")
    if driver != "bridge":
        raise NetworkInspectError(f"{name}: existing network driver is incompatible")
    if internal is not False:
        raise NetworkInspectError(f"{name}: existing network is not explicitly non-internal")
    config = ipam_config(payload)
    existing_subnet = str(config.get("Subnet") or "")
    existing_gateway = str(config.get("Gateway") or "")
    try:
        same_subnet = ipaddress.ip_network(existing_subnet, strict=False) == subnet
    except ValueError:
        same_subnet = False
    if not same_subnet:
        raise NetworkInspectError(f"{name}: existing network subnet is incompatible")
    if existing_gateway != str(gateway):
        raise NetworkInspectError(f"{name}: existing network gateway is incompatible")
