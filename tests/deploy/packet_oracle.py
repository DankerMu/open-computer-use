# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Independent packet judgments over fake iptables state.

This module does not import production policy helpers. Expected allowlist
and protected destinations are literals supplied by the test.
"""

from __future__ import annotations

from ipaddress import ip_address, ip_network


BUILTIN_CHAINS = {"INPUT", "FORWARD", "OUTPUT", "PREROUTING", "POSTROUTING"}
TERMINAL = {"ACCEPT", "DROP", "REJECT"}


def _flag_value(rule: list[str], flag: str):
    if flag not in rule:
        return None
    index = rule.index(flag)
    if index + 1 >= len(rule):
        return ""
    return rule[index + 1]


def _matches_iface(rule: list[str], iface: str | None) -> bool:
    value = _flag_value(rule, "-i")
    if value is None:
        return True
    return value == iface


def _matches_source(rule: list[str], src: str | None) -> bool:
    value = _flag_value(rule, "-s")
    if value is None:
        return True
    if src is None:
        return False
    try:
        return ip_address(src) in ip_network(value, strict=False)
    except ValueError:
        return src == value


def _matches_dest(rule: list[str], dst: str | None) -> bool:
    value = _flag_value(rule, "-d")
    if value is None:
        return True
    if dst is None:
        return False
    try:
        return ip_address(dst) in ip_network(value, strict=False)
    except ValueError:
        return dst == value


def _matches_ctstate(rule: list[str], ctstate: str | None) -> bool:
    value = _flag_value(rule, "--ctstate")
    if value is None:
        return True
    if ctstate is None:
        return False
    wanted = {item.strip() for item in value.split(",") if item.strip()}
    return ctstate in wanted


def _matches_ctdir(rule: list[str], ctdir: str | None) -> bool:
    value = _flag_value(rule, "--ctdir")
    if value is None:
        return True
    return value == ctdir


def _target(rule: list[str]) -> str | None:
    return _flag_value(rule, "-j")


def _policy(firewall: dict, family: str, chain: str) -> str:
    policies = (firewall.get("policies") or {}).get(family) or {}
    value = policies.get(chain)
    if value in TERMINAL:
        return value
    if chain in BUILTIN_CHAINS:
        return "DROP"
    return "RETURN"


def evaluate_chain(rules: list[list[str]], packet: dict, chains: dict, seen=None, *, family="ipv4", firewall=None, chain_name=""):
    seen = set() if seen is None else seen
    for rule in rules:
        if not _matches_iface(rule, packet.get("in_iface")):
            continue
        if not _matches_source(rule, packet.get("src")):
            continue
        if not _matches_dest(rule, packet.get("dst")):
            continue
        if not _matches_ctstate(rule, packet.get("ctstate")):
            continue
        if not _matches_ctdir(rule, packet.get("ctdir")):
            continue
        target = _target(rule)
        if target is None:
            continue
        if target in TERMINAL:
            return target
        if target == "RETURN":
            return "RETURN"
        if target in chains:
            if target in seen:
                return "LOOP"
            nested = evaluate_chain(
                chains[target],
                packet,
                chains,
                seen | {target},
                family=family,
                firewall=firewall,
                chain_name=target,
            )
            if nested != "RETURN":
                return nested
            continue
        return "UNKNOWN-TARGET"
    if chain_name in BUILTIN_CHAINS:
        return _policy(firewall or {}, family, chain_name)
    return "RETURN"


def evaluate_packet(firewall: dict, *, family: str, chain: str, **packet) -> str:
    chains = firewall.get(family) or {}
    if chain not in chains:
        return "MISSING-CHAIN"
    return evaluate_chain(
        chains[chain],
        packet,
        chains,
        family=family,
        firewall=firewall,
        chain_name=chain,
    )
