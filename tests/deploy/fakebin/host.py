#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Independent fake iptables/ip/sysctl/docker-info host CLI."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import sys
import time


OWNED_IPV4 = "OCU-SANDBOX-EGRESS"
OWNED_IPV6 = "OCU-SANDBOX-EGRESS6"
OWNED_COMMENT = "ocu-sandbox-egress"
OWNED_IPV6_COMMENT = "ocu-sandbox-egress6"


def state_dir() -> Path:
    raw = os.environ.get("FAKE_DOCKER_STATE", "")
    if not raw:
        sys.stderr.write("FAKE_DOCKER_STATE is required\n")
        sys.exit(2)
    path = Path(raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def log(argv: list[str], state: Path) -> None:
    with (state / "ops.log").open("a", encoding="utf-8") as stream:
        stream.write(" ".join(argv) + "\n")


def firewall_path(state: Path) -> Path:
    return state / "firewall.json"


def default_firewall() -> dict:
    return {
        "ipv4": {
            "DOCKER-USER": [["-j", "RETURN"]],
            "INPUT": [["-j", "ACCEPT"]],
            "FORWARD": [["-j", "DOCKER-USER"], ["-j", "ACCEPT"]],
            "OUTPUT": [["-j", "ACCEPT"]],
        },
        "ipv6": {
            "INPUT": [["-j", "ACCEPT"]],
            "FORWARD": [["-j", "ACCEPT"]],
            "OUTPUT": [["-j", "ACCEPT"]],
        },
        "policies": {
            "ipv4": {"INPUT": "ACCEPT", "FORWARD": "ACCEPT", "OUTPUT": "ACCEPT", "DOCKER-USER": "-"},
            "ipv6": {"INPUT": "ACCEPT", "FORWARD": "ACCEPT", "OUTPUT": "ACCEPT"},
        },
    }


def load_firewall(state: Path) -> dict:
    path = firewall_path(state)
    if not path.exists():
        payload = default_firewall()
        save_firewall(state, payload)
        return payload
    return json.loads(path.read_text(encoding="utf-8"))


def save_firewall(state: Path, payload: dict) -> None:
    firewall_path(state).write_text(json.dumps(payload), encoding="utf-8")


def hold(state: Path, env_name: str, entered_name: str) -> None:
    hold_path = os.environ.get(env_name, "")
    if not hold_path:
        return
    marker = Path(hold_path)
    (state / entered_name).write_text(str(os.getpid()), encoding="utf-8")
    while marker.exists():
        time.sleep(0.05)


def deny_if_permission(state: Path, family: str) -> bool:
    marker = state / f"{family}-permission-denied"
    if marker.exists():
        sys.stderr.write("Permission denied (you must be root)\n")
        return True
    return False


def fail_if_missing_backend(state: Path, tool: str) -> bool:
    marker = state / f"missing-{tool}"
    if marker.exists():
        sys.stderr.write(f"{tool}: command not found\n")
        return True
    return False


def strip_wait(argv: list[str]) -> tuple[list[str], str | None]:
    wait = None
    out: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "-w" and i + 1 < len(argv):
            wait = argv[i + 1]
            i += 2
            continue
        if token.startswith("-w") and token[2:].isdigit():
            wait = token[2:]
            i += 1
            continue
        if token == "-t" and i + 1 < len(argv):
            table = argv[i + 1]
            if table != "filter":
                sys.stderr.write(f"unsupported table {table}\n")
                raise SystemExit(1)
            i += 2
            continue
        if token.startswith("-t") and len(token) > 2:
            table = token[2:]
            if table != "filter":
                sys.stderr.write(f"unsupported table {table}\n")
                raise SystemExit(1)
            i += 1
            continue
        out.append(token)
        i += 1
    return out, wait


def chain_map(payload: dict, family: str) -> dict:
    return payload.setdefault(family, {})


def policies(payload: dict, family: str) -> dict:
    return payload.setdefault("policies", {}).setdefault(family, {})


def render_save(payload: dict, family: str) -> str:
    chains = chain_map(payload, family)
    pols = policies(payload, family)
    lines = ["*filter"]
    for name, rules in chains.items():
        policy = pols.get(name, "-")
        lines.append(f":{name} {policy} [0:0]")
    for name, rules in chains.items():
        for rule in rules:
            lines.append("-A " + name + " " + " ".join(rule))
    lines.append("COMMIT")
    lines.append("")
    return "\n".join(lines)


def parse_restore(text: str) -> list[tuple[str, list]]:
    ops: list[tuple[str, list]] = []
    table = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("*"):
            table = line[1:]
            if table != "filter":
                raise ValueError(f"unsupported table {table}")
            continue
        if line == "COMMIT":
            table = None
            continue
        if table != "filter":
            raise ValueError("restore line outside filter table")
        if line.startswith(":"):
            parts = line[1:].split()
            name = parts[0]
            policy = parts[1] if len(parts) > 1 else "-"
            ops.append(("define", [name, policy]))
            continue
        tokens = shlex.split(line)
        if not tokens:
            continue
        ops.append((tokens[0], tokens[1:]))
    return ops


def apply_restore(payload: dict, family: str, text: str) -> None:
    chains = chain_map(payload, family)
    pols = policies(payload, family)
    for op, args in parse_restore(text):
        if op == "define":
            name, policy = args
            chains.setdefault(name, [])
            pols[name] = policy
        elif op == "-N":
            name = args[0]
            chains.setdefault(name, [])
            pols.setdefault(name, "-")
        elif op == "-F":
            name = args[0]
            chains.setdefault(name, [])
            chains[name] = []
        elif op == "-A":
            name = args[0]
            chains.setdefault(name, [])
            chains[name].append(args[1:])
        elif op == "-X":
            name = args[0]
            chains.pop(name, None)
            pols.pop(name, None)
        elif op == "-P":
            name, policy = args[0], args[1]
            pols[name] = policy
        else:
            raise ValueError(f"unsupported restore operation {op}")


def insert_rule(payload: dict, family: str, chain: str, index: int, rule: list[str]) -> None:
    chains = chain_map(payload, family)
    if chain not in chains:
        raise KeyError(chain)
    pos = index - 1
    if pos < 0 or pos > len(chains[chain]):
        raise IndexError(index)
    chains[chain].insert(pos, rule)


def append_rule(payload: dict, family: str, chain: str, rule: list[str]) -> None:
    chains = chain_map(payload, family)
    if chain not in chains:
        raise KeyError(chain)
    chains[chain].append(rule)


def delete_rule(payload: dict, family: str, chain: str, spec) -> None:
    chains = chain_map(payload, family)
    if chain not in chains:
        raise KeyError(chain)
    rules = chains[chain]
    if isinstance(spec, int):
        if spec < 1 or spec > len(rules):
            raise IndexError(spec)
        del rules[spec - 1]
        return
    for i, rule in enumerate(rules):
        if rule == spec:
            del rules[i]
            return
    raise KeyError("rule")


def new_chain(payload: dict, family: str, chain: str) -> None:
    chains = chain_map(payload, family)
    if chain in chains:
        raise FileExistsError(chain)
    chains[chain] = []
    policies(payload, family)[chain] = "-"


def iptables_family(tool: str) -> str:
    return "ipv6" if tool in {"ip6tables", "ip6tables-restore", "ip6tables-save"} else "ipv4"


def handle_iptables(state: Path, tool: str, argv: list[str]) -> int:
    if fail_if_missing_backend(state, tool):
        return 127
    if deny_if_permission(state, iptables_family(tool)):
        return 1
    family = iptables_family(tool)
    payload = load_firewall(state)
    if tool.endswith("-restore"):
        hold(state, "FAKE_FIREWALL_HOLD_RESTORE", "entered-restore")
        fail = state / f"{family}-restore-fail"
        if fail.exists():
            sys.stderr.write("iptables-restore failed: Invalid argument\n")
            return 1
        text = sys.stdin.read()
        noflush = "--noflush" in argv
        if not noflush:
            sys.stderr.write("iptables-restore requires --noflush\n")
            return 1
        try:
            apply_restore(payload, family, text)
        except Exception as exc:
            sys.stderr.write(f"iptables-restore failed: {exc}\n")
            return 1
        save_firewall(state, payload)
        return 0
    args, _wait = strip_wait(argv)
    if tool.endswith("-save") or args[:1] == ["-S"]:
        hold(state, "FAKE_FIREWALL_HOLD_SAVE", "entered-save")
        sys.stdout.write(render_save(payload, family))
        return 0
    if not args:
        sys.stderr.write("missing iptables command\n")
        return 1
    if args[0] == "-V":
        sys.stdout.write("iptables v1.8.10 (nf_tables)\n")
        return 0
    if args[0] == "-L":
        chain = args[1] if len(args) > 1 else ""
        chains = chain_map(payload, family)
        if chain and chain not in chains:
            sys.stderr.write(f"iptables: No chain/target/match by that name.\n")
            return 1
        return 0
    if args[0] == "-N":
        try:
            new_chain(payload, family, args[1])
        except FileExistsError:
            sys.stderr.write("Chain already exists.\n")
            return 1
        save_firewall(state, payload)
        return 0
    if args[0] == "-I":
        chain = args[1]
        if len(args) > 2 and args[2].isdigit():
            index = int(args[2])
            rule = args[3:]
        else:
            index = 1
            rule = args[2:]
        try:
            insert_rule(payload, family, chain, index, rule)
        except KeyError:
            sys.stderr.write("iptables: No chain/target/match by that name.\n")
            return 1
        save_firewall(state, payload)
        return 0
    if args[0] == "-A":
        try:
            append_rule(payload, family, args[1], args[2:])
        except KeyError:
            sys.stderr.write("iptables: No chain/target/match by that name.\n")
            return 1
        save_firewall(state, payload)
        return 0
    if args[0] == "-D":
        chain = args[1]
        spec: object
        if len(args) == 3 and args[2].isdigit():
            spec = int(args[2])
        else:
            spec = args[2:]
        try:
            delete_rule(payload, family, chain, spec)
        except (KeyError, IndexError):
            sys.stderr.write("iptables: No chain/target/match by that name.\n")
            return 1
        save_firewall(state, payload)
        return 0
    sys.stderr.write(f"unsupported {tool} command: {' '.join(args)}\n")
    return 1


def docker_info(state: Path) -> int:
    payload = {
        "Driver": "overlay2",
        "SecurityOptions": [],
        "CgroupDriver": "systemd",
        "FirewallBackend": "iptables",
        "Rootless": False,
    }
    override = state / "docker-info.json"
    if override.exists():
        payload.update(json.loads(override.read_text(encoding="utf-8")))
    sys.stdout.write(json.dumps(payload))
    return 0


def sysctl_cmd(state: Path, argv: list[str]) -> int:
    if fail_if_missing_backend(state, "sysctl"):
        return 127
    values = {
        "net.bridge.bridge-nf-call-iptables": "1",
        "net.bridge.bridge-nf-call-ip6tables": "1",
        "net.ipv4.ip_forward": "1",
    }
    override = state / "sysctl.json"
    if override.exists():
        values.update(json.loads(override.read_text(encoding="utf-8")))
    if not argv or argv[0] not in {"-n", "-e"}:
        sys.stderr.write("unsupported sysctl command\n")
        return 1
    names = argv[1:]
    if argv[0] == "-e":
        names = argv[1:]
    for name in names:
        if name not in values:
            sys.stderr.write(f"sysctl: cannot stat {name}: No such file or directory\n")
            return 1
        sys.stdout.write(f"{values[name]}\n")
    return 0


def ip_cmd(state: Path, argv: list[str]) -> int:
    if fail_if_missing_backend(state, "ip"):
        return 127
    if argv[:2] != ["link", "show"]:
        sys.stderr.write("unsupported ip command\n")
        return 1
    name = None
    if len(argv) >= 4 and argv[2] == "dev":
        name = argv[3]
    elif len(argv) >= 3:
        name = argv[2]
    if not name:
        sys.stderr.write("ip: missing device\n")
        return 1
    aliases = {}
    alias_path = state / "interfaces.json"
    if alias_path.exists():
        aliases = json.loads(alias_path.read_text(encoding="utf-8"))
    names = set(aliases.values()) if aliases else set()
    names.update(aliases.keys())
    if name in aliases:
        name = aliases[name]
    if name not in names:
        # Accept any interface the tests created as a known device, including
        # the default Docker br-<id[:12]> convention derived from network files.
        networks = state / "networks"
        if networks.is_dir():
            for path in networks.glob("*.json"):
                payload = json.loads(path.read_text(encoding="utf-8"))
                net_id = str(payload.get("Id") or "")
                options = payload.get("Options") or {}
                bridge = options.get("com.docker.network.bridge.name")
                if bridge:
                    names.add(bridge)
                if net_id:
                    names.add("br-" + net_id[:12])
    if name not in names:
        sys.stderr.write(f'Device "{name}" does not exist.\n')
        return 1
    sys.stdout.write(f"2: {name}: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n")
    return 0


def command_name(argv0: str) -> str:
    return Path(argv0).name


def main(argv: list[str]) -> int:
    state = state_dir()
    tool = command_name(argv[0] if argv else sys.argv[0])
    args = argv[1:] if argv and command_name(argv[0]) == tool and Path(argv[0]).name == tool else argv
    # When invoked as `python3 host.py iptables ...` the first arg is the tool.
    if tool == "host.py":
        if not args:
            sys.stderr.write("missing host command\n")
            return 1
        tool = args[0]
        args = args[1:]
    log([tool, *args], state)
    if tool in {"iptables", "ip6tables", "iptables-restore", "ip6tables-restore", "iptables-save", "ip6tables-save"}:
        return handle_iptables(state, tool, args)
    if tool == "sysctl":
        return sysctl_cmd(state, args)
    if tool == "ip":
        return ip_cmd(state, args)
    if tool == "docker":
        sys.stderr.write("host.py does not implement docker; use fakebin/docker\n")
        return 1
    sys.stderr.write(f"unsupported host command: {tool}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
