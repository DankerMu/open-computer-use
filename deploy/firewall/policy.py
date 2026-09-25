# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Single production definition for the sandbox egress host guard."""

from __future__ import annotations

import errno
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys

from netinspect import NetworkInspectError, compatible, inspect_network, parse_ipv4_address, parse_ipv4_network


OWNED_IPV4 = "OCU-SANDBOX-EGRESS"
OWNED_IPV6 = "OCU-SANDBOX-EGRESS6"
OWNED_COMMENT = "ocu-sandbox-egress"
OWNED_IPV6_COMMENT = "ocu-sandbox-egress6"
OWNED_CHAINS = {OWNED_IPV4, OWNED_IPV6}
METADATA = ipaddress.ip_network("169.254.169.254/32")
XTABLES_WAIT = "5"
LOCK_NAME = "ocu-sandbox-egress.lock"
DEFAULT_LOCK_DIR = Path("/run/ocu-sandbox-egress")
IFNAMSIZ = 15
REQUIRED_SYSCTLS = (
    "net.bridge.bridge-nf-call-iptables",
    "net.bridge.bridge-nf-call-ip6tables",
)
CTSTATE_ORDER = ("INVALID", "NEW", "RELATED", "ESTABLISHED", "UNTRACKED", "SNAT", "DNAT")


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


def serialize_rule(tokens: list[str]) -> str:
    rendered: list[str] = []
    for token in tokens:
        if token.startswith("-") and token not in {"-", "--"}:
            rendered.append(token)
        else:
            rendered.append(shlex.quote(token))
    return " ".join(rendered)


def parse_rule_tokens(tokens: list[str]) -> dict:
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


def rules_equivalent(actual: list[str], expected: list[str]) -> bool:
    left = parse_rule_tokens(actual)
    right = parse_rule_tokens(expected)
    if left["unknown"] or right["unknown"]:
        return False
    return left == right


def sequences_equivalent(actual: list[list[str]], expected: list[list[str]]) -> bool:
    if len(actual) != len(expected):
        return False
    return all(rules_equivalent(left, right) for left, right in zip(actual, expected))


def jump_target(rule: list[str]) -> str | None:
    parsed = parse_rule_tokens(rule)
    return parsed["target"]


def flag_value(rule: list[str], flag: str) -> str | None:
    if flag == "-i":
        return parse_rule_tokens(rule)["interface"]
    if flag == "-s":
        return parse_rule_tokens(rule)["source"]
    if flag == "-d":
        return parse_rule_tokens(rule)["dest"]
    if flag == "--comment":
        return parse_rule_tokens(rule)["comment"]
    if flag not in rule:
        return None
    index = rule.index(flag)
    if index + 1 >= len(rule):
        return ""
    return rule[index + 1]


def owned_hook(bridge: str, chain: str, comment: str) -> list[str]:
    return ["-i", bridge, "-m", "comment", "--comment", comment, "-j", chain]


def is_owned_hook(rule: list[str], comment: str) -> bool:
    parsed = parse_rule_tokens(rule)
    return parsed["comment"] == comment and parsed["target"] in OWNED_CHAINS


def ipv4_policy_rules(bridge: str, control: ipaddress.IPv4Network, allow: list[ipaddress.IPv4Network]) -> list[list[str]]:
    del bridge
    rules = [
        ["-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "--ctdir", "REPLY", "-j", "RETURN"],
        ["-d", str(control), "-j", "DROP"],
        ["-d", str(METADATA), "-j", "DROP"],
    ]
    for network in allow:
        rules.append(["-d", str(network), "-j", "RETURN"])
    rules.append(["-j", "DROP"])
    return rules


def ipv6_policy_rules() -> list[list[str]]:
    return [["-j", "DROP"]]


def save_rules(family: str) -> dict[str, list[list[str]]]:
    tool = "ip6tables-save" if family == "ipv6" else "iptables-save"
    result = run_cmd([tool, "-t", "filter"])
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
            try:
                tokens = shlex.split(line)
            except ValueError:
                fail(f"{tool} returned an unparsable rule: {line}")
            if len(tokens) < 2:
                continue
            name = tokens[1]
            chains.setdefault(name, []).append(tokens[2:])
    return chains


def require_docker_user_hook(ipv4: dict[str, list[list[str]]]) -> None:
    if "DOCKER-USER" not in ipv4:
        fail("DOCKER-USER chain is missing; refusing to create a placeholder")
    forward = ipv4.get("FORWARD") or []
    if not forward:
        fail("FORWARD is missing the Docker DOCKER-USER hook")
    first = parse_rule_tokens(forward[0])
    if first["target"] == "DOCKER-USER" and not any(
        (
            first["interface"],
            first["out"],
            first["source"],
            first["dest"],
            first["comment"],
            first["matches"],
            first["unknown"],
        )
    ):
        return
    fail("DOCKER-USER is bypassed by an earlier FORWARD rule")


def reject_unrecognized_owned_references(family: str, chains: dict[str, list[list[str]]], comment: str) -> None:
    for name, rules in chains.items():
        if name in OWNED_CHAINS:
            continue
        for rule in rules:
            parsed = parse_rule_tokens(rule)
            jump = parsed["target"]
            goto = parsed["goto"]
            if jump in OWNED_CHAINS and parsed["comment"] != comment:
                fail(f"{name} references reserved chain {jump} without owned comment")
            if goto in OWNED_CHAINS:
                fail(f"{name} references reserved chain {goto} without owned comment")
            if parsed["comment"] == comment and jump not in OWNED_CHAINS:
                fail(f"{name} has an unrecognized owned comment on {family}")


def refuse_foreign_bridge(parent: str, rules: list[list[str]], hook: list[str], comment: str) -> None:
    wanted = parse_rule_tokens(hook)["interface"]
    other = [
        rule
        for rule in rules
        if is_owned_hook(rule, comment) and parse_rule_tokens(rule)["interface"] not in {None, wanted}
    ]
    if other:
        fail(f"existing managed hooks belong to a different bridge on {parent}")


def precheck_hooks(ctx, ipv4: dict[str, list[list[str]]], ipv6: dict[str, list[list[str]]]) -> None:
    require_docker_user_hook(ipv4)
    for name in ("INPUT", "FORWARD"):
        if name not in ipv4:
            fail(f"{name} chain is missing")
        if name not in ipv6:
            fail(f"IPv6 {name} chain is missing")
    hook4 = owned_hook(ctx["bridge"], OWNED_IPV4, OWNED_COMMENT)
    hook6 = owned_hook(ctx["bridge"], OWNED_IPV6, OWNED_IPV6_COMMENT)
    reject_unrecognized_owned_references("ipv4", ipv4, OWNED_COMMENT)
    reject_unrecognized_owned_references("ipv6", ipv6, OWNED_IPV6_COMMENT)
    refuse_foreign_bridge("DOCKER-USER", ipv4.get("DOCKER-USER") or [], hook4, OWNED_COMMENT)
    refuse_foreign_bridge("INPUT", ipv4.get("INPUT") or [], hook4, OWNED_COMMENT)
    refuse_foreign_bridge("INPUT", ipv6.get("INPUT") or [], hook6, OWNED_IPV6_COMMENT)
    refuse_foreign_bridge("FORWARD", ipv6.get("FORWARD") or [], hook6, OWNED_IPV6_COMMENT)


def restore_payload(chain: str, rules: list[list[str]]) -> str:
    lines = ["*filter", f":{chain} - [0:0]", f"-F {chain}"]
    for rule in rules:
        lines.append(f"-A {chain} {serialize_rule(rule)}")
    lines.append("COMMIT")
    return "\n".join(lines) + "\n"


def restore_owned(family: str, chain: str, rules: list[list[str]]) -> None:
    tool = "ip6tables-restore" if family == "ipv6" else "iptables-restore"
    result = run_cmd([tool, "-w", XTABLES_WAIT, "--noflush"], stdin=restore_payload(chain, rules))
    if result.returncode != 0:
        combined = ((result.stderr or "") + (result.stdout or "")).strip()
        fail(f"{tool} failed: {combined or 'nonzero status'}")


def reconcile_payload(parent: str, hook: list[str], comment: str, rules: list[list[str]]) -> str | None:
    extras = [rule for rule in rules if is_owned_hook(rule, comment)]
    if extras and rules and rules_equivalent(rules[0], hook) and len(extras) == 1:
        return None
    lines = ["*filter"]
    for rule in extras:
        lines.append(f"-D {parent} {serialize_rule(rule)}")
    lines.append(f"-I {parent} 1 {serialize_rule(hook)}")
    lines.append("COMMIT")
    return "\n".join(lines) + "\n"


def reconcile_hooks(family: str, parent: str, hook: list[str], comment: str, chains: dict[str, list[list[str]]]) -> None:
    if parent not in chains:
        fail(f"{parent} chain is missing")
    payload = reconcile_payload(parent, hook, comment, list(chains.get(parent) or []))
    if payload is None:
        return
    tool = "ip6tables-restore" if family == "ipv6" else "iptables-restore"
    result = run_cmd([tool, "-w", XTABLES_WAIT, "--noflush"], stdin=payload)
    if result.returncode != 0:
        combined = ((result.stderr or "") + (result.stdout or "")).strip()
        fail(f"failed to reconcile {parent} hook: {combined or 'nonzero status'}")


def _lstat(path: Path):
    try:
        return os.lstat(path)
    except OSError as exc:
        fail(f"cannot inspect {path}: {exc}")


def _require_private_dir(path: Path, info) -> None:
    if stat.S_ISLNK(info.st_mode):
        fail(f"lock directory {path} is a symlink")
    if not stat.S_ISDIR(info.st_mode):
        fail(f"lock directory {path} is not a directory")
    if info.st_uid != os.geteuid():
        fail(f"lock directory {path} is not owned by the current user")
    if info.st_mode & 0o022:
        fail(f"lock directory {path} is world/group writable")


def _ensure_lock_dir(path: Path) -> None:
    if path.exists() or path.is_symlink():
        _require_private_dir(path, _lstat(path))
        return
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        fail(f"cannot create lock directory {path}: {exc}")
    _require_private_dir(path, _lstat(path))


def lock_path() -> Path:
    override = os.environ.get("OCU_SANDBOX_EGRESS_LOCK", "").strip()
    if override:
        path = Path(override)
    else:
        path = DEFAULT_LOCK_DIR / LOCK_NAME
    _ensure_lock_dir(path.parent)
    return path


class ExclusiveLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd = None

    def __enter__(self):
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
        try:
            self.fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                fail(f"lock path {self.path} is a symlink")
            fail(f"cannot open lock {self.path}: {exc}")
        try:
            info = os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode):
                fail(f"lock path {self.path} is not a regular file")
            if info.st_uid != os.geteuid():
                fail(f"lock path {self.path} is not owned by the current user")
            os.fchmod(self.fd, 0o600)
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        except SystemExit:
            os.close(self.fd)
            self.fd = None
            raise
        except OSError as exc:
            os.close(self.fd)
            self.fd = None
            fail(f"cannot lock {self.path}: {exc}")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None



def inspect_required_network(name: str) -> dict:
    try:
        payload = inspect_network(name)
    except NetworkInspectError as exc:
        fail(str(exc))
    if payload is None:
        fail(f"{name}: inspect failed: network not found")
    return payload


def inspect_optional_network(name: str):
    try:
        return inspect_network(name)
    except NetworkInspectError as exc:
        fail(str(exc))


def load_context():
    require_present("OCU_SANDBOX_EGRESS_ALLOW")
    allow = parse_allowlist(os.environ["OCU_SANDBOX_EGRESS_ALLOW"])
    sandbox_name = require_nonempty("OCU_SANDBOX_NETWORK")
    control_name = require_nonempty("OCU_PRIVATE_NETWORK")
    try:
        sandbox_subnet = parse_ipv4_network("OCU_SANDBOX_SUBNET", require_nonempty("OCU_SANDBOX_SUBNET"))
        control_subnet = parse_ipv4_network("OCU_PRIVATE_SUBNET", require_nonempty("OCU_PRIVATE_SUBNET"))
        sandbox_gateway = parse_ipv4_address("OCU_SANDBOX_GATEWAY", require_nonempty("OCU_SANDBOX_GATEWAY"))
        control_gateway = parse_ipv4_address("OCU_PRIVATE_GATEWAY", require_nonempty("OCU_PRIVATE_GATEWAY"))
    except NetworkInspectError as exc:
        fail(str(exc))
    if sandbox_gateway not in sandbox_subnet:
        fail("OCU_SANDBOX_GATEWAY is outside OCU_SANDBOX_SUBNET")
    if control_gateway not in control_subnet:
        fail("OCU_PRIVATE_GATEWAY is outside OCU_PRIVATE_SUBNET")
    sandbox = inspect_required_network(sandbox_name)
    try:
        compatible(sandbox, name=sandbox_name, subnet=sandbox_subnet, gateway=sandbox_gateway)
    except NetworkInspectError as exc:
        fail(str(exc))
    control = inspect_optional_network(control_name)
    if control is not None:
        try:
            compatible(control, name=control_name, subnet=control_subnet, gateway=control_gateway)
        except NetworkInspectError as exc:
            fail(str(exc))
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


def install() -> None:
    with ExclusiveLock(lock_path()):
        ctx = load_context()
        ipv4 = save_rules("ipv4")
        ipv6 = save_rules("ipv6")
        precheck_hooks(ctx, ipv4, ipv6)
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
        reject_unrecognized_owned_references("ipv4", ipv4, OWNED_COMMENT)
        reject_unrecognized_owned_references("ipv6", ipv6, OWNED_IPV6_COMMENT)
        expected = expected_ipv4(ctx)
        actual = ipv4.get(OWNED_IPV4)
        if actual is None:
            fail(f"{OWNED_IPV4} chain is missing")
        diagnose_ipv4_policy(actual, expected)
        if not sequences_equivalent(ipv6.get(OWNED_IPV6) or [], ipv6_policy_rules()):
            fail(f"{OWNED_IPV6} contents are missing, duplicated, stale or misordered")
        hook4 = owned_hook(ctx["bridge"], OWNED_IPV4, OWNED_COMMENT)
        hook6 = owned_hook(ctx["bridge"], OWNED_IPV6, OWNED_IPV6_COMMENT)
        _check_first_unique("DOCKER-USER", ipv4.get("DOCKER-USER") or [], hook4, OWNED_COMMENT)
        _check_first_unique("INPUT", ipv4.get("INPUT") or [], hook4, OWNED_COMMENT)
        _check_first_unique("INPUT", ipv6.get("INPUT") or [], hook6, OWNED_IPV6_COMMENT)
        _check_first_unique("FORWARD", ipv6.get("FORWARD") or [], hook6, OWNED_IPV6_COMMENT)


def diagnose_ipv4_policy(actual: list[list[str]], expected: list[list[str]]) -> None:
    if sequences_equivalent(actual, expected):
        return
    if not any(parse_rule_tokens(rule)["matches"].get("--ctdir") == "REPLY" for rule in actual):
        fail(f"{OWNED_IPV4} is missing the REPLY exception")
    if not actual or parse_rule_tokens(actual[-1]) != parse_rule_tokens(["-j", "DROP"]):
        fail(f"{OWNED_IPV4} is missing the final DROP")
    fail(f"{OWNED_IPV4} contents are missing, duplicated, stale or misordered")


def _check_first_unique(parent: str, rules: list[list[str]], hook: list[str], comment: str) -> None:
    owned = [rule for rule in rules if is_owned_hook(rule, comment)]
    target = jump_target(hook) or comment
    if not owned:
        fail(f"{parent} is missing the owned first hook to {target}")
    first_owned = owned[0]
    parsed = parse_rule_tokens(first_owned)
    if parsed["source"] is not None or parsed["interface"] is None:
        fail(f"{parent} owned hook to {target} is not interface-scoped; expected -i")
    if parsed["interface"] != parse_rule_tokens(hook)["interface"]:
        fail(f"existing managed hooks belong to a different bridge on {parent}")
    if not rules_equivalent(first_owned, hook):
        fail(f"{parent} owned hook to {target} is missing, duplicated or not first")
    if not rules or not rules_equivalent(rules[0], hook):
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
