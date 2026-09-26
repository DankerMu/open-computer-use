# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Pure sandbox DNS policy: syntax, effective Docker values, ordered compatibility.

This module never reads the process environment and never imports the Docker SDK.
Runtime callers pass a raw env value; deploy callers reuse firewall.policy.parse_allowlist
and METADATA plus the configured control subnet for destination membership.
"""

from __future__ import annotations

import ipaddress

POLICY_NAME = "OCU_SANDBOX_DNS"
# Explicit-empty policy must be a nonempty HostConfig.Dns override. Moby applies
# OverrideNameServers only when len(dnsList) > 0; an empty list inherits host
# nameservers and dials them from the host netns. 127.0.0.11 is the embedded
# resolver listen address, which filterExtServers drops from external upstreams.
EMPTY_POLICY_OVERRIDE = "127.0.0.11"
MAX_RESOLVERS = 3


class SandboxDnsError(ValueError):
    """Configured DNS syntax or destination membership is invalid."""


def parse_sandbox_dns(raw: str | None) -> list[str] | None:
    """Return None when policy is absent, else the effective ordered Docker DNS list.

    Present-and-empty becomes ``[EMPTY_POLICY_OVERRIDE]``. Nonempty input is a
    comma-separated ordered list of one to three unique canonical IPv4 addresses.
    """
    if raw is None:
        return None
    text = raw.strip()
    if text == "":
        return [EMPTY_POLICY_OVERRIDE]
    seen: set[str] = set()
    ordered: list[str] = []
    for item in text.split(","):
        entry = item.strip()
        if not entry:
            raise SandboxDnsError(f"{POLICY_NAME} contains an empty entry")
        try:
            address = ipaddress.ip_address(entry)
        except ValueError as exc:
            raise SandboxDnsError(f"{POLICY_NAME} has an invalid entry: {entry}") from exc
        if address.version != 4:
            raise SandboxDnsError(f"{POLICY_NAME} has a non-IPv4 entry: {entry}")
        canonical = str(address)
        if canonical in seen:
            raise SandboxDnsError(f"{POLICY_NAME} has a duplicate entry: {canonical}")
        seen.add(canonical)
        ordered.append(canonical)
    if len(ordered) > MAX_RESOLVERS:
        raise SandboxDnsError(
            f"{POLICY_NAME} lists {len(ordered)} resolvers; at most {MAX_RESOLVERS} are allowed"
        )
    return ordered


def inspected_dns(host_config) -> list[str] | None:
    """Return the inspected HostConfig.Dns list, or None when missing/unusable."""
    if not isinstance(host_config, dict):
        return None
    if "Dns" not in host_config:
        return None
    value = host_config.get("Dns")
    if value is None:
        return None
    if not isinstance(value, list):
        return None
    return list(value)


def dns_compatible(host_dns, expected: list[str] | None) -> bool:
    """True when inspected DNS equals the effective ordered policy list.

    Absent policy is always compatible (development mode). Under policy, missing,
    null, empty, wrong-type, or reordered inspected DNS is incompatible.
    """
    if expected is None:
        return True
    if not isinstance(host_dns, list):
        return False
    if len(host_dns) != len(expected):
        return False
    for actual, wanted in zip(host_dns, expected):
        if str(actual) != wanted:
            return False
    return True


def resolver_violations(
    resolvers: list[str],
    allow: list[ipaddress.IPv4Network],
    control: ipaddress.IPv4Network,
    metadata: ipaddress.IPv4Network,
) -> list[str]:
    """Return resolvers that are unlisted or sit on a hard-denied destination.

    Hard denies win even under a broad allowlist. The empty-policy sentinel is
    container-local and is not a destination membership check.
    """
    if resolvers == [EMPTY_POLICY_OVERRIDE]:
        return []
    offenders: list[str] = []
    for item in resolvers:
        address = ipaddress.IPv4Address(item)
        if address in control or address in metadata:
            offenders.append(item)
            continue
        if not any(address in network for network in allow):
            offenders.append(item)
    return offenders
