# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Single production definition for the sandbox egress host guard."""

from __future__ import annotations

import fcntl
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys


OWNED_IPV4 = "OCU-SANDBOX-EGRESS"
OWNED_IPV6 = "OCU-SANDBOX-EGRESS6"
OWNED_COMMENT = "ocu-sandbox-egress"
OWNED_IPV6_COMMENT = "ocu-sandbox-egress6"
METADATA = ipaddress.ip_network("169.254.169.254/32")
XTABLES_WAIT = "5"
LOCK_NAME = "ocu-sandbox-egress.lock"
IFNAMSIZ = 15
REQUIRED_SYSCTLS = (
    "net.bridge.bridge-nf-call-iptables",
    "net.bridge.bridge-nf-call-ip6tables",
)



def fail(message: str, code: int = 1) -> None:
    print(f"sandbox-egress: {message}", file=sys.stderr)
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


def parse_ipv4_network(name: str) -> ipaddress.IPv4Network:
    raw = require_nonempty(name)
    try:
        network = ipaddress.ip_network(raw, strict=True)
    except ValueError:
        fail(f"{name} is not an IPv4 network")
    if network.version != 4:
        fail(f"{name} is not an IPv4 network")
    return network


def parse_allowlist(raw: str) -> list[ipaddress.IPv4Network]:
    text = raw.strip()
    if text == "":
        return []
    seen: set[ipaddress.IPv4Network] = set()
    ordered: list[ipaddress.IPv4Network] = []
    for item in text.split(","):
        entry = item.strip()
        if not entry:
            fail("OCU_SANDBOX_EGRESS_ALLOW contains an empty entry")
        try:
            if "/" in entry:
                network = ipaddress.ip_network(entry, strict=True)
            else:
                address = ipaddress.ip_address(entry)
                network = ipaddress.ip_network(f"{address}/32", strict=True)
        except ValueError:
            fail(f"OCU_SANDBOX_EGRESS_ALLOW has an invalid entry: {entry}")
        if network.version != 4:
            fail(f"OCU_SANDBOX_EGRESS_ALLOW has a non-IPv4 entry: {entry}")
        if network in seen:
            continue
        seen.add(network)
        ordered.append(network)
    return ordered


def run_cmd(argv: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def docker(*args: str) -> subprocess.CompletedProcess:
    return run_cmd(["docker", *args])


def xtables(tool: str, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return run_cmd([tool, "-w", XTABLES_WAIT, *args], stdin=stdin)


def inspect_network(name: str) -> dict:
    result = docker("network", "inspect", name, "--format", "{{json .}}")
    if result.returncode != 0:
        combined = ((result.stderr or "") + (result.stdout or "")).strip()
        fail(f"{name}: inspect failed: {combined or 'nonzero status'}")
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


def bridge_name(payload: dict) -> str:
    options = payload.get("Options") or {}
    explicit = str(options.get("com.docker.network.bridge.name") or "").strip()
    net_id = str(payload.get("Id") or "").strip()
    if explicit:
        name = explicit
    else:
        if len(net_id) < 12:
            fail("sandbox network id is too short to derive a bridge name")
        name = "br-" + net_id[:12]
    if not name or len(name) > IFNAMSIZ or any(ch.isspace() or ch == "/" for ch in name):
        fail(f"sandbox bridge interface name is unusable: {name}")
    probe = run_cmd(["ip", "link", "show", "dev", name])
    if probe.returncode != 0:
        fail(f"sandbox bridge interface {name} is not present")
    return name


def docker_backend() -> None:
    result = docker("info", "--format", "{{json .}}")
    if result.returncode != 0:
        combined = ((result.stderr or "") + (result.stdout or "")).strip()
        fail(f"docker info failed: {combined or 'nonzero status'}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        fail("docker info returned malformed JSON")
    backend = str(payload.get("FirewallBackend") or payload.get("Driver") or "").lower()
    info_text = json.dumps(payload).lower()
    if "nftables" in backend and "iptables" not in backend:
        fail("Docker native nftables backend is unsupported; iptables interface is required")
    if payload.get("Rootless") is True or "name=rootless" in info_text:
        fail("rootless Docker networking is unsupported")


def require_sysctls() -> None:
    for name in REQUIRED_SYSCTLS:
        result = run_cmd(["sysctl", "-n", name])
        if result.returncode != 0:
            fail(f"{name} is unavailable")
        value = (result.stdout or "").strip()
        if value != "1":
            fail(f"{name} must be 1 (got {value or 'empty'})")


def save_rules(family: str) -> dict[str, list[list[str]]]:
    tool = "ip6tables-save" if family == "ipv6" else "iptables-save"
    result = xtables(tool, "-t", "filter")
    if result.returncode != 0:
        combined = ((result.stderr or "") + (result.stdout or "")).strip()
        fail(f"{tool} failed: {combined or 'nonzero status'}")
    chains: dict[str, list[list[str]]] = {}
    for raw in (result.stdout or "").splitlines():
        line = raw.strip()
        if line.startswith(":"):
            name = line[1:].split()[0]
            chains.setdefault(name, [])
            continue
        if line.startswith("-A "):
            tokens = line.split()
            name = tokens[1]
            chains.setdefault(name, []).append(tokens[2:])
    return chains


def require_docker_user_hook(ipv4: dict[str, list[list[str]]]) -> None:
    if "DOCKER-USER" not in ipv4:
        fail("DOCKER-USER chain is missing; refusing to create a placeholder")
    forward = ipv4.get("FORWARD") or []
    for rule in forward:
        if rule == ["-j", "DOCKER-USER"]:
            return
        if terminating(rule):
            fail("DOCKER-USER is bypassed by an earlier FORWARD rule")
    fail("FORWARD is missing the Docker DOCKER-USER hook")


def terminating(rule: list[str]) -> bool:
    target = jump_target(rule)
    return target in {"ACCEPT", "DROP", "REJECT", "RETURN"} and "-i" not in rule and "-s" not in rule and "-d" not in rule and "-o" not in rule


def jump_target(rule: list[str]) -> str | None:
    if "-j" not in rule:
        return None
    return rule[rule.index("-j") + 1]


def flag_value(rule: list[str], flag: str) -> str | None:
    if flag not in rule:
        return None
    index = rule.index(flag)
    if index + 1 >= len(rule):
        return ""
    return rule[index + 1]


def owned_hook(bridge: str, chain: str, comment: str) -> list[str]:
    return ["-i", bridge, "-j", chain, "-m", "comment", "--comment", comment]


def is_owned_hook(rule: list[str], comment: str) -> bool:
    return flag_value(rule, "--comment") == comment and jump_target(rule) in {OWNED_IPV4, OWNED_IPV6}


def ipv4_policy_rules(bridge: str, control: ipaddress.IPv4Network, allow: list[ipaddress.IPv4Network]) -> list[list[str]]:
    rules = [
        ["-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "--ctdir", "REPLY", "-j", "RETURN"],
        ["-d", str(control), "-j", "DROP"],
        ["-d", str(METADATA), "-j", "DROP"],
    ]
    for network in allow:
        rules.append(["-d", str(network), "-j", "RETURN"])
    rules.append(["-j", "DROP"])
    return rules


def ipv6_policy_rules() -> list[list[str]]:
    return [["-j", "DROP"]]


def restore_owned(family: str, chain: str, rules: list[list[str]]) -> None:
    tool = "ip6tables-restore" if family == "ipv6" else "iptables-restore"
    lines = ["*filter", f":{chain} - [0:0]", f"-F {chain}"]
    for rule in rules:
        lines.append("-A " + chain + " " + " ".join(rule))
    lines.append("COMMIT")
    result = run_cmd([tool, "-w", XTABLES_WAIT, "--noflush"], stdin="\n".join(lines) + "\n")
    if result.returncode != 0:
        combined = ((result.stderr or "") + (result.stdout or "")).strip()
        fail(f"{tool} failed: {combined or 'nonzero status'}")


def insert_hook(family: str, parent: str, hook: list[str]) -> None:
    tool = "ip6tables" if family == "ipv6" else "iptables"
    result = xtables(tool, "-t", "filter", "-I", parent, "1", *hook)
    if result.returncode != 0:
        combined = ((result.stderr or "") + (result.stdout or "")).strip()
        fail(f"failed to insert {parent} hook: {combined or 'nonzero status'}")


def delete_hook(family: str, parent: str, spec) -> None:
    tool = "ip6tables" if family == "ipv6" else "iptables"
    if isinstance(spec, int):
        argv = [tool, "-w", XTABLES_WAIT, "-t", "filter", "-D", parent, str(spec)]
    else:
        argv = [tool, "-w", XTABLES_WAIT, "-t", "filter", "-D", parent, *spec]
    result = run_cmd(argv)
    if result.returncode != 0:
        combined = ((result.stderr or "") + (result.stdout or "")).strip()
        fail(f"failed to delete {parent} hook: {combined or 'nonzero status'}")


def reconcile_hooks(family: str, parent: str, hook: list[str], comment: str, chains: dict[str, list[list[str]]]) -> None:
    if parent not in chains:
        fail(f"{parent} chain is missing")
    rules = list(chains.get(parent) or [])
    other_bridge = [
        rule
        for rule in rules
        if is_owned_hook(rule, comment) and flag_value(rule, "-i") not in {None, hook[1]}
    ]
    if other_bridge:
        fail(f"existing managed hooks belong to a different bridge on {parent}")
    if rules[:1] != [hook]:
        insert_hook(family, parent, hook)
        rules = [hook, *rules]
    extras = [index for index, rule in enumerate(rules) if index != 0 and is_owned_hook(rule, comment)]
    for index in reversed(extras):
        delete_hook(family, parent, index + 1)


def lock_path() -> Path:
    override = os.environ.get("OCU_SANDBOX_EGRESS_LOCK", "").strip()
    if override:
        path = Path(override)
    else:
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
        path = runtime / LOCK_NAME
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        fail(f"cannot create lock directory {parent}: {exc}")
    if parent.exists() and parent.is_dir():
        mode = parent.stat().st_mode & 0o077
        if mode & 0o022:
            fail(f"lock directory {parent} is world/group writable")
    return path


class ExclusiveLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def __enter__(self):
        self.handle = open(self.path, "a+b")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.handle is not None:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle = None


def load_context():
    require_present("OCU_SANDBOX_EGRESS_ALLOW")
    allow = parse_allowlist(os.environ["OCU_SANDBOX_EGRESS_ALLOW"])
    sandbox_name = require_nonempty("OCU_SANDBOX_NETWORK")
    control_name = require_nonempty("OCU_PRIVATE_NETWORK")
    sandbox_subnet = parse_ipv4_network("OCU_SANDBOX_SUBNET")
    control_subnet = parse_ipv4_network("OCU_PRIVATE_SUBNET")
    sandbox_gateway = ipaddress.ip_address(require_nonempty("OCU_SANDBOX_GATEWAY"))
    control_gateway = ipaddress.ip_address(require_nonempty("OCU_PRIVATE_GATEWAY"))
    if sandbox_gateway.version != 4 or sandbox_gateway not in sandbox_subnet:
        fail("OCU_SANDBOX_GATEWAY is outside OCU_SANDBOX_SUBNET")
    if control_gateway.version != 4 or control_gateway not in control_subnet:
        fail("OCU_PRIVATE_GATEWAY is outside OCU_PRIVATE_SUBNET")
    sandbox = inspect_network(sandbox_name)
    compatible(sandbox, name=sandbox_name, subnet=sandbox_subnet, gateway=sandbox_gateway)
    control = inspect_network(control_name)
    compatible(control, name=control_name, subnet=control_subnet, gateway=control_gateway)
    docker_backend()
    require_sysctls()
    iface = bridge_name(sandbox)
    return {
        "allow": allow,
        "bridge": iface,
        "control": control_subnet,
        "sandbox": sandbox_subnet,
        "sandbox_id": str(sandbox.get("Id") or ""),
    }


def install() -> None:
    with ExclusiveLock(lock_path()):
        ctx = load_context()
        ipv4 = save_rules("ipv4")
        ipv6 = save_rules("ipv6")
        require_docker_user_hook(ipv4)
        for name in ("INPUT", "FORWARD"):
            if name not in ipv4:
                fail(f"{name} chain is missing")
            if name not in ipv6:
                fail(f"IPv6 {name} chain is missing")
        restore_owned("ipv4", OWNED_IPV4, ipv4_policy_rules(ctx["bridge"], ctx["control"], ctx["allow"]))
        restore_owned("ipv6", OWNED_IPV6, ipv6_policy_rules())
        hook4 = owned_hook(ctx["bridge"], OWNED_IPV4, OWNED_COMMENT)
        hook6 = owned_hook(ctx["bridge"], OWNED_IPV6, OWNED_IPV6_COMMENT)
        ipv4 = save_rules("ipv4")
        ipv6 = save_rules("ipv6")
        reconcile_hooks("ipv4", "DOCKER-USER", hook4, OWNED_COMMENT, ipv4)
        reconcile_hooks("ipv4", "INPUT", hook4, OWNED_COMMENT, ipv4)
        reconcile_hooks("ipv6", "INPUT", hook6, OWNED_IPV6_COMMENT, ipv6)
        reconcile_hooks("ipv6", "FORWARD", hook6, OWNED_IPV6_COMMENT, ipv6)


def expected_ipv4(ctx) -> list[list[str]]:
    return ipv4_policy_rules(ctx["bridge"], ctx["control"], ctx["allow"])


def check() -> None:
    with ExclusiveLock(lock_path()):
        ctx = load_context()
        ipv4 = save_rules("ipv4")
        ipv6 = save_rules("ipv6")
        require_docker_user_hook(ipv4)
        expected = expected_ipv4(ctx)
        actual = ipv4.get(OWNED_IPV4)
        if actual is None:
            fail(f"{OWNED_IPV4} chain is missing")
        diagnose_ipv4_policy(actual, expected)
        if ipv6.get(OWNED_IPV6) != ipv6_policy_rules():
            fail(f"{OWNED_IPV6} contents are missing, duplicated, stale or misordered")
        hook4 = owned_hook(ctx["bridge"], OWNED_IPV4, OWNED_COMMENT)
        hook6 = owned_hook(ctx["bridge"], OWNED_IPV6, OWNED_IPV6_COMMENT)
        _check_first_unique("DOCKER-USER", ipv4.get("DOCKER-USER") or [], hook4, OWNED_COMMENT)
        _check_first_unique("INPUT", ipv4.get("INPUT") or [], hook4, OWNED_COMMENT)
        _check_first_unique("INPUT", ipv6.get("INPUT") or [], hook6, OWNED_IPV6_COMMENT)
        _check_first_unique("FORWARD", ipv6.get("FORWARD") or [], hook6, OWNED_IPV6_COMMENT)


def diagnose_ipv4_policy(actual: list[list[str]], expected: list[list[str]]) -> None:
    if actual == expected:
        return
    if not any("--ctdir" in rule and "REPLY" in rule for rule in actual):
        fail(f"{OWNED_IPV4} is missing the REPLY exception")
    if not actual or actual[-1] != ["-j", "DROP"]:
        fail(f"{OWNED_IPV4} is missing the final DROP")
    fail(f"{OWNED_IPV4} contents are missing, duplicated, stale or misordered")


def _check_first_unique(parent: str, rules: list[list[str]], hook: list[str], comment: str) -> None:
    owned = [rule for rule in rules if is_owned_hook(rule, comment)]
    target = jump_target(hook) or comment
    if not owned:
        fail(f"{parent} is missing the owned first hook to {target}")
    first_owned = owned[0]
    if flag_value(first_owned, "-s") is not None or flag_value(first_owned, "-i") is None:
        fail(f"{parent} owned hook to {target} is not interface-scoped; expected -i")
    if flag_value(first_owned, "-i") != hook[1]:
        fail(f"existing managed hooks belong to a different bridge on {parent}")
    if first_owned != hook:
        fail(f"{parent} owned hook to {target} is missing, duplicated or not first")
    if rules[:1] != [hook]:
        fail(f"{parent} owned hook to {target} is shadowed or not first")
    if len(owned) != 1:
        fail(f"{parent} has duplicate owned hooks to {target}")


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in {"install", "check"}:
        fail("usage: policy.py install|check")
    try:
        if argv[1] == "install":
            install()
        else:
            check()
    except SystemExit:
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
