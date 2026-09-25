#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Independent fake iptables/ip/sysctl/docker-info host CLI."""

from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import shlex
import sys
import time


OWNED_IPV4 = "OCU-SANDBOX-EGRESS"
SAVE_OPTSTRING = "bcdt:M:f:V"
CTSTATE_ORDER = ("INVALID", "NEW", "RELATED", "ESTABLISHED", "UNTRACKED", "SNAT", "DNAT")


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


def reject_save_wait(argv: list[str]) -> None:
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in {"-w", "--wait"} or token.startswith("-w") and token[2:].isdigit() or token.startswith("--wait"):
            sys.stderr.write(f"unrecognized option '{token}'\n")
            sys.stderr.write(f"Look at manual page `iptables-save.8' for more information.\n")
            raise SystemExit(1)
        i += 1


def parse_save_argv(argv: list[str]) -> None:
    reject_save_wait(argv)
    i = 0
    while i < len(argv):
        token = argv[i]
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
        if token in {"-c", "-d", "-b", "-V"}:
            i += 1
            continue
        if token in {"-M", "-f"} and i + 1 < len(argv):
            i += 2
            continue
        if token.startswith("-") and len(token) == 2 and token[1] in SAVE_OPTSTRING.replace(":", ""):
            sys.stderr.write(f"unrecognized option '{token}'\n")
            raise SystemExit(1)
        if token.startswith("-"):
            sys.stderr.write(f"unrecognized option '{token}'\n")
            raise SystemExit(1)
        sys.stderr.write("Unknown arguments found on commandline\n")
        raise SystemExit(1)


def strip_mutate_flags(argv: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in {"-w", "--wait", "-W", "--wait-interval"} and i + 1 < len(argv) and not argv[i + 1].startswith("-"):
            i += 2
            continue
        if token.startswith("-w") and token[2:].isdigit():
            i += 1
            continue
        if token.startswith("--wait=") or token.startswith("--wait-interval="):
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
    return out


def chain_map(payload: dict, family: str) -> dict:
    return payload.setdefault(family, {})


def policies(payload: dict, family: str) -> dict:
    return payload.setdefault("policies", {}).setdefault(family, {})


def canonical_ctstate(value: str) -> str:
    wanted = {item.strip().upper() for item in value.split(",") if item.strip()}
    ordered = [name for name in CTSTATE_ORDER if name in wanted]
    leftover = wanted.difference(CTSTATE_ORDER)
    if leftover:
        ordered.extend(sorted(leftover))
    return ",".join(ordered)


def canonical_cidr(value: str) -> str:
    try:
        return str(ipaddress.ip_network(value, strict=False))
    except ValueError:
        return value


def parse_rule(tokens: list[str]) -> dict:
    interface = None
    out_iface = None
    source = None
    dest = None
    comment = None
    target = None
    goto = None
    matches: dict[str, str | None] = {}
    unknown: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "-i" and i + 1 < len(tokens):
            interface = tokens[i + 1]
            i += 2
            continue
        if token == "-o" and i + 1 < len(tokens):
            out_iface = tokens[i + 1]
            i += 2
            continue
        if token == "-s" and i + 1 < len(tokens):
            source = canonical_cidr(tokens[i + 1])
            i += 2
            continue
        if token == "-d" and i + 1 < len(tokens):
            dest = canonical_cidr(tokens[i + 1])
            i += 2
            continue
        if token == "-j" and i + 1 < len(tokens):
            target = tokens[i + 1]
            i += 2
            continue
        if token == "-g" and i + 1 < len(tokens):
            goto = tokens[i + 1]
            unknown.extend(["-g", tokens[i + 1]])
            i += 2
            continue
        if token == "-m" and i + 1 < len(tokens):
            module = tokens[i + 1]
            i += 2
            if module not in {"comment", "conntrack"}:
                unknown.extend(["-m", module])
            continue
        if token == "--comment" and i + 1 < len(tokens):
            comment = tokens[i + 1]
            i += 2
            continue
        if token == "--ctstate" and i + 1 < len(tokens):
            matches["--ctstate"] = canonical_ctstate(tokens[i + 1])
            i += 2
            continue
        if token == "--ctdir" and i + 1 < len(tokens):
            matches["--ctdir"] = tokens[i + 1]
            i += 2
            continue
        if token.startswith("-"):
            unknown.append(token)
            i += 1
            continue
        unknown.append(token)
        i += 1
    return {
        "interface": interface,
        "out": out_iface,
        "source": source,
        "dest": dest,
        "comment": comment,
        "target": target,
        "goto": goto,
        "matches": matches,
        "unknown": tuple(unknown),
    }


def rules_match(left: list[str], right: list[str]) -> bool:
    return parse_rule(left) == parse_rule(right)


def serialize_rule(tokens: list[str]) -> str:
    parsed = parse_rule(tokens)
    parts: list[str] = []
    if parsed["interface"] is not None:
        parts.extend(["-i", parsed["interface"]])
    if parsed["out"] is not None:
        parts.extend(["-o", parsed["out"]])
    if parsed["source"] is not None:
        parts.extend(["-s", parsed["source"]])
    if parsed["dest"] is not None:
        parts.extend(["-d", parsed["dest"]])
    if parsed["matches"].get("--ctstate") is not None or parsed["matches"].get("--ctdir") is not None:
        parts.extend(["-m", "conntrack"])
        if parsed["matches"].get("--ctstate") is not None:
            parts.extend(["--ctstate", parsed["matches"]["--ctstate"]])
        if parsed["matches"].get("--ctdir") is not None:
            parts.extend(["--ctdir", parsed["matches"]["--ctdir"]])
    if parsed["comment"] is not None:
        parts.extend(["-m", "comment", "--comment", shlex.quote(parsed["comment"])])
    for token in parsed["unknown"]:
        parts.append(token)
    if parsed["target"] is not None:
        parts.extend(["-j", parsed["target"]])
    return " ".join(parts)


def render_save(payload: dict, family: str) -> str:
    chains = chain_map(payload, family)
    pols = policies(payload, family)
    lines = ["*filter"]
    for name, rules in chains.items():
        policy = pols.get(name, "-")
        lines.append(f":{name} {policy} [0:0]")
    for name, rules in chains.items():
        for rule in rules:
            lines.append("-A " + name + " " + serialize_rule(rule))
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
        elif op == "-I":
            name = args[0]
            if len(args) > 1 and args[1].isdigit():
                index = int(args[1])
                rule = args[2:]
            else:
                index = 1
                rule = args[1:]
            insert_rule(payload, family, name, index, rule)
        elif op == "-D":
            name = args[0]
            spec: object
            if len(args) == 2 and args[1].isdigit():
                spec = int(args[1])
            else:
                spec = args[1:]
            delete_rule(payload, family, name, spec)
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
        if rules_match(rule, spec):
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


def maybe_corrupt_owned(state: Path, payload: dict, family: str, text: str) -> None:
    marker = state / "check-fail-once"
    if not marker.exists() or family != "ipv4":
        return
    if f"-F {OWNED_IPV4}" not in text:
        return
    owned = chain_map(payload, "ipv4").get(OWNED_IPV4) or []
    chain_map(payload, "ipv4")[OWNED_IPV4] = [
        rule for rule in owned if parse_rule(rule) != parse_rule(["-j", "DROP"])
    ]
    marker.unlink()



def maybe_inject_foreign(state: Path, payload: dict, family: str) -> None:
    marker = state / "inject-foreign-once"
    if not marker.exists() or family != "ipv4":
        return
    if OWNED_IPV4 not in chain_map(payload, "ipv4"):
        return
    extra = ["-s", "10.8.8.8/32", "-j", "ACCEPT", "-m", "comment", "--comment", "foreign-concurrent"]
    docker_user = chain_map(payload, "ipv4").setdefault("DOCKER-USER", [])
    if extra not in docker_user:
        docker_user.insert(0, extra)
    marker.unlink()


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
        stripped = strip_mutate_flags(argv)
        if any(token.startswith("-") and token not in {"--noflush"} for token in stripped):
            sys.stderr.write(f"unsupported {tool} command: {' '.join(stripped)}\n")
            return 1
        try:
            apply_restore(payload, family, text)
        except Exception as exc:
            sys.stderr.write(f"iptables-restore failed: {exc}\n")
            return 1
        maybe_corrupt_owned(state, payload, family, text)
        save_firewall(state, payload)
        return 0
    if tool.endswith("-save"):
        parse_save_argv(argv)
        hold(state, "FAKE_FIREWALL_HOLD_SAVE", "entered-save")
        maybe_inject_foreign(state, payload, family)
        save_firewall(state, payload)
        sys.stdout.write(render_save(payload, family))
        return 0
    args = strip_mutate_flags(argv)
    if args[:1] == ["-S"]:
        hold(state, "FAKE_FIREWALL_HOLD_SAVE", "entered-save")
        sys.stdout.write(render_save(payload, family))
        return 0
    if not args:
        sys.stderr.write("missing iptables command\n")
        return 1
    if args[0] == "-V":
        sys.stdout.write("iptables v1.8.11 (legacy)\n")
        return 0
    if args[0] == "-L":
        chain = args[1] if len(args) > 1 else ""
        chains = chain_map(payload, family)
        if chain and chain not in chains:
            sys.stderr.write("iptables: No chain/target/match by that name.\n")
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
