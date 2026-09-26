# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Read-only protected-bridge DNS preflight. Does not mutate containers."""

from __future__ import annotations

import json
import os
import sys

from firewall.policy import METADATA, parse_allowlist
from netinspect import (
    NetworkInspectError,
    compatible,
    container_identity,
    inspect_container,
    inspect_network,
    list_container_ids,
    parse_ipv4_address,
    parse_ipv4_network,
    protected_membership,
)
import sandbox_dns


def fail(message: str, code: int = 1) -> None:
    print(f"sandbox-dns: {message}", file=sys.stderr)
    raise SystemExit(code)


def require_present(name: str) -> str:
    if name not in os.environ:
        fail(f"{name} is unset")
    return os.environ[name]


def require_nonempty(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        fail(f"{name} is required")
    return value


def load_core_environment(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, json.JSONDecodeError, UnicodeError):
        fail("malformed or unreadable resolved Compose JSON")
    if not isinstance(payload, dict) or not isinstance(payload.get("services"), dict):
        fail("resolved Compose document has no services object")
    service = payload["services"].get("computer-use-server")
    if not isinstance(service, dict):
        fail("computer-use-server: required service is missing")
    environment = service.get("environment")
    if not isinstance(environment, dict):
        fail("computer-use-server: missing environment")
    return environment


def resolved_dns_value(environment: dict) -> str:
    if sandbox_dns.POLICY_NAME not in environment:
        fail(
            f"computer-use-server environment omits {sandbox_dns.POLICY_NAME}; "
            "production overlay must not activate development DNS"
        )
    value = environment[sandbox_dns.POLICY_NAME]
    if value is None:
        fail(f"computer-use-server {sandbox_dns.POLICY_NAME} is missing")
    if not isinstance(value, str):
        fail(f"computer-use-server {sandbox_dns.POLICY_NAME} is not a string")
    return value


def parse_host_policy() -> list[str]:
    require_present(sandbox_dns.POLICY_NAME)
    try:
        expected = sandbox_dns.parse_sandbox_dns(os.environ[sandbox_dns.POLICY_NAME])
    except sandbox_dns.SandboxDnsError as exc:
        fail(str(exc))
    if expected is None:
        fail(f"{sandbox_dns.POLICY_NAME} is unset")
    return expected


def require_matching_resolved(environment: dict, host_raw: str) -> None:
    resolved = resolved_dns_value(environment)
    if resolved != host_raw:
        fail(
            f"resolved computer-use-server {sandbox_dns.POLICY_NAME} does not match the host policy"
        )


def destination_context():
    require_present("OCU_SANDBOX_EGRESS_ALLOW")
    try:
        allow = parse_allowlist(os.environ["OCU_SANDBOX_EGRESS_ALLOW"])
        sandbox_name = require_nonempty("OCU_SANDBOX_NETWORK")
        sandbox_subnet = parse_ipv4_network("OCU_SANDBOX_SUBNET", require_nonempty("OCU_SANDBOX_SUBNET"))
        sandbox_gateway = parse_ipv4_address("OCU_SANDBOX_GATEWAY", require_nonempty("OCU_SANDBOX_GATEWAY"))
        control_subnet = parse_ipv4_network("OCU_PRIVATE_SUBNET", require_nonempty("OCU_PRIVATE_SUBNET"))
    except NetworkInspectError as exc:
        fail(str(exc))
    payload = inspect_network(sandbox_name)
    if payload is None:
        fail(f"{sandbox_name}: sandbox network is missing")
    try:
        compatible(payload, name=sandbox_name, subnet=sandbox_subnet, gateway=sandbox_gateway)
    except NetworkInspectError as exc:
        fail(str(exc))
    network_id = str(payload.get("Id") or "").strip()
    if not network_id:
        fail(f"{sandbox_name}: inspected network id is missing")
    return {
        "allow": allow,
        "control": control_subnet,
        "network_name": sandbox_name,
        "network_id": network_id,
    }


def inspect_protected_containers(expected: list[str], ctx) -> None:
    try:
        ids = list_container_ids()
    except NetworkInspectError as exc:
        fail(str(exc))
    offenders: list[str] = []
    for container_id in ids:
        try:
            payload = inspect_container(container_id)
        except NetworkInspectError as exc:
            fail(str(exc))
        identity = container_identity(payload, container_id)
        membership = protected_membership(
            payload,
            network_name=ctx["network_name"],
            network_id=ctx["network_id"],
        )
        if membership is None:
            fail(f"{identity}: protected-bridge membership cannot be classified")
        if membership is False:
            continue
        host = payload.get("HostConfig") if isinstance(payload, dict) else None
        if not sandbox_dns.dns_compatible(sandbox_dns.inspected_dns(host), expected):
            offenders.append(identity)
    if offenders:
        named = ", ".join(offenders)
        fail(f"incompatible DNS on protected-bridge containers: {named}")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        fail("resolved core Compose JSON is required")
    environment = load_core_environment(argv[1])
    host_raw = os.environ.get(sandbox_dns.POLICY_NAME) if sandbox_dns.POLICY_NAME in os.environ else None
    if host_raw is None:
        fail(f"{sandbox_dns.POLICY_NAME} is unset")
    expected = parse_host_policy()
    require_matching_resolved(environment, host_raw)
    ctx = destination_context()
    offenders = sandbox_dns.resolver_violations(expected, ctx["allow"], ctx["control"], METADATA)
    if offenders:
        fail(
            f"{sandbox_dns.POLICY_NAME} resolvers are unlisted or protected: {', '.join(offenders)}"
        )
    inspect_protected_containers(expected, ctx)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
