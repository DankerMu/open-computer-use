# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Operator post-deploy overlay smoke for publications, isolation, and terminal default."""

from __future__ import annotations

import errno
import ipaddress
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from firewall.policy import METADATA, parse_allowlist
from netinspect import (
    NetworkInspectError,
    container_identity,
    inspect_container,
    inspect_network,
    parse_ipv4_address,
    parse_ipv4_network,
    protected_membership,
)
from smoke_tty import TtydProtocolError, TtydSession, connect_ttyd

PREFIX = "overlay-smoke"
PROXY = "proxy"
WEBUI = "open-webui"
OCU = "computer-use-server"
POSTGRES = "postgres"
RETENTION = "retention-guard"
CLEANUP = "cleanup"
REQUIRED_RUNNING = (OCU, WEBUI, PROXY, POSTGRES, RETENTION)
PROXY_TARGET = "8082"
OCU_TARGET = 8081
WEBUI_TARGET = 8080
MANAGED_LABEL = "mcp-computer-use-orchestrator"
NO_AUTOSTART = "NO_AUTOSTART=1"
CURL_WRITE_OUT = (
    "exitcode=%{exitcode} num_connects=%{num_connects} "
    "remote_ip=%{remote_ip} time_connect=%{time_connect} http_code=%{http_code}"
)
BASH_NAMES = {"bash", "-bash"}
CONNECT_TIMEOUT = 3.0
HTTP_TIMEOUT = 5.0
SANDBOX_CONNECT = "5"
SANDBOX_MAX = "15"
TERMINAL_DEADLINE = 10.0
STABLE_WINDOW = 2.0
STABLE_INTERVAL = 0.25
SIGNAL_EXITS = {signal.SIGINT: 130, signal.SIGTERM: 143, signal.SIGHUP: 129}

_OWNED_SESSION: TtydSession | None = None
_OWNED_CLEANUP: dict | None = None
_CLEANUP_DONE = False
_OWNED_PGIDS: set[int] = set()
_PENDING_START: dict | None = None


class SmokeError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def fail(message: str, code: int = 1) -> None:
    print(f"{PREFIX}: {message}", file=sys.stderr)
    raise SmokeError(message, code)


def require_present(name: str) -> str:
    if name not in os.environ:
        fail(f"{name} is unset", 2)
    return os.environ[name]


def require_nonempty(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        fail(f"{name} is required", 2)
    return value


def script_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def overlay_dir(root: str) -> str:
    return os.path.join(root, "deploy", "production-like-test")


def compose_contexts(root: str, project: str) -> list[tuple[str, list[str]]]:
    overlay = overlay_dir(root)
    return [
        (
            "core",
            [
                "-p",
                project,
                "--project-directory",
                root,
                "-f",
                os.path.join(root, "docker-compose.yml"),
                "-f",
                os.path.join(overlay, "compose.core.override.yml"),
            ],
        ),
        (
            "webui",
            [
                "-p",
                project,
                "--project-directory",
                root,
                "-f",
                os.path.join(root, "docker-compose.webui.yml"),
                "-f",
                os.path.join(overlay, "compose.webui.override.yml"),
            ],
        ),
        (
            "proxy",
            [
                "-p",
                project,
                "--project-directory",
                overlay,
                "-f",
                os.path.join(overlay, "compose.proxy.yml"),
            ],
        ),
    ]


def _stop_pgid(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except OSError:
            return
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        return


def stop_owned_children() -> None:
    pgids = list(_OWNED_PGIDS)
    _OWNED_PGIDS.clear()
    for pgid in pgids:
        _stop_pgid(pgid)


def run_cmd(argv: list[str], *, timeout: float, env=None) -> subprocess.CompletedProcess:
    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    pgid = process.pid
    _OWNED_PGIDS.add(pgid)
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            fail(f"{argv[0]} timed out")
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    finally:
        if process.poll() is None:
            _stop_pgid(pgid)
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            _stop_pgid(pgid)
            try:
                process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                fail(f"{argv[0]} could not be reaped")
        _OWNED_PGIDS.discard(pgid)


def docker(*args: str, timeout: float = 20.0) -> subprocess.CompletedProcess:
    return run_cmd(["docker", *args], timeout=timeout)


def suppress_unsafe(text: str) -> str:
    secrets = []
    for name in ("OCU_SMOKE_OWNER_TOKEN", "OCU_INTERNAL_TOKEN", "MCP_API_KEY"):
        value = os.environ.get(name, "")
        if value:
            secrets.append(value)
    for secret in secrets:
        text = text.replace(secret, "[redacted]")
    return text


def parse_compose_ps(text: str, stack: str) -> list[dict]:
    stripped = text.strip()
    if not stripped:
        fail(f"{stack}: compose ps inventory is empty", 2)
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        payload = None
    rows: list[dict] = []
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = [payload]
    else:
        for line in stripped.splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                fail(f"{stack}: compose ps returned malformed JSON", 2)
            rows.append(item)
    if not rows:
        fail(f"{stack}: compose ps inventory is empty", 2)
    for row in rows:
        if not isinstance(row, dict):
            fail(f"{stack}: compose ps returned a non-object row", 2)
    return rows


def _identity_fields(row: dict) -> dict:
    publishers = row.get("Publishers")
    if publishers is None:
        publishers = []
    if not isinstance(publishers, list):
        fail("compose ps publishers are malformed", 2)
    return {
        "ID": str(row.get("ID") or row.get("Id") or "").strip(),
        "Name": str(row.get("Name") or "").strip(),
        "Service": str(row.get("Service") or "").strip(),
        "State": str(row.get("State") or "").strip().lower(),
        "Publishers": publishers,
        "Project": str(row.get("Project") or "").strip(),
    }


def merge_inventory(stacks: list[tuple[str, list[dict]]]) -> dict[str, dict]:
    combined: dict[str, dict] = {}
    unnamed = 0
    for stack, rows in stacks:
        for row in rows:
            fields = _identity_fields(row)
            container_id = fields["ID"]
            if not container_id:
                unnamed += 1
                container_id = f"{stack}-unnamed-{unnamed}"
            existing = combined.get(container_id)
            if existing is None:
                combined[container_id] = fields
                continue
            if existing != fields:
                fail(f"{fields['Service'] or container_id}: conflicting compose identity", 2)
    return combined


def service_running(state: str) -> bool:
    return state in {"running", "up"} or state.startswith("running")


def publisher_ok(mapping: dict, published: str, bound: str) -> bool:
    if not isinstance(mapping, dict):
        return False
    target = str(mapping.get("TargetPort") or mapping.get("Target") or "")
    published_port = str(mapping.get("PublishedPort") or mapping.get("Published") or "")
    protocol = str(mapping.get("Protocol") or "tcp").lower()
    url = str(mapping.get("URL") or mapping.get("url") or "")
    host_ip = str(mapping.get("HostIP") or mapping.get("HostIp") or "")
    if target != PROXY_TARGET or published_port != published or protocol != "tcp":
        return False
    return (host_ip or url) == bound


def judge_publications(inventory: dict[str, dict], published: str) -> None:
    by_service: dict[str, list[dict]] = {}
    for row in inventory.values():
        service = row["Service"]
        if not service:
            fail("compose ps row is missing Service", 2)
        by_service.setdefault(service, []).append(row)
    if CLEANUP in by_service:
        states = {row["State"] for row in by_service[CLEANUP]}
        if states & {"running", "restarting"}:
            fail("cleanup: running destructive maintenance service")
    for name in REQUIRED_RUNNING:
        rows = by_service.get(name) or []
        if not rows:
            fail(f"{name}: required service is missing", 2)
        if not any(service_running(row["State"]) for row in rows):
            fail(f"{name}: required service is not running")
    proxy_rows = by_service.get(PROXY) or []
    publications = []
    for row in proxy_rows:
        publications.extend(row["Publishers"])
    if not 1 <= len(publications) <= 2 or not any(
        publisher_ok(mapping, published, "0.0.0.0") or publisher_ok(mapping, published, "")
        for mapping in publications
    ):
        fail(f"{PROXY}: incorrect TCP listen/publication mapping")
    if len(publications) == 2 and not any(
        publisher_ok(mapping, published, "::") for mapping in publications
    ):
        fail(f"{PROXY}: incorrect IPv6 TCP publication")
    if len(publications) == 2 and sum(
        publisher_ok(mapping, published, "::") for mapping in publications
    ) != 1:
        fail(f"{PROXY}: unexpected proxy publication")
    for service, rows in by_service.items():
        if service == PROXY:
            continue
        for row in rows:
            if row["Publishers"]:
                fail(f"{service}: host publication is forbidden")


def inspect_required(container_id: str) -> dict:
    try:
        return inspect_container(container_id)
    except NetworkInspectError as exc:
        fail(str(exc), 2)


def ipv4_on_network(payload: dict, network_name: str, network_id: str) -> str:
    settings = payload.get("NetworkSettings")
    membership = settings.get("Networks") if isinstance(settings, dict) else None
    if not isinstance(membership, dict):
        fail(f"{container_identity(payload, '')}: control-plane address is missing", 2)
    wanted = {item for item in (network_name, network_id) if item}
    for name, data in membership.items():
        current_id = ""
        if isinstance(data, dict):
            current_id = str(data.get("NetworkID") or data.get("NetworkId") or "").strip()
        if str(name) in wanted or current_id in wanted:
            address = str(data.get("IPAddress") or "").strip() if isinstance(data, dict) else ""
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError:
                fail(f"{container_identity(payload, '')}: control-plane address is not IPv4", 2)
            if parsed.version != 4:
                fail(f"{container_identity(payload, '')}: control-plane address is not IPv4", 2)
            return str(parsed)
    fail(f"{container_identity(payload, '')}: not attached to the control-plane network", 2)


def service_container(inventory: dict[str, dict], service: str) -> tuple[str, dict]:
    for container_id, row in inventory.items():
        if row["Service"] == service and service_running(row["State"]):
            return container_id, inspect_required(container_id)
    fail(f"{service}: required service is missing", 2)


def parse_literal_url(name: str, raw: str) -> tuple[str, int, str]:
    try:
        parts = urlsplit(raw)
    except ValueError:
        fail(f"{name} is not an IPv4 HTTP URL", 2)
    if parts.scheme != "http":
        fail(f"{name} must be an http URL", 2)
    host = parts.hostname
    if host is None:
        fail(f"{name} is not an IPv4 HTTP URL", 2)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        fail(f"{name} host is not an IPv4 literal", 2)
    if address.version != 4:
        fail(f"{name} host is not an IPv4 literal", 2)
    if parts.port is None:
        fail(f"{name} must include an explicit port", 2)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return str(address), parts.port, path


def former_target(raw: str) -> tuple[str, int]:
    if "://" in raw:
        host, port, _path = parse_literal_url("OCU_SMOKE_FORMER_URL", raw)
        return host, port
    if ":" in raw:
        host, _, port_text = raw.rpartition(":")
        try:
            address = ipaddress.ip_address(host)
            port = int(port_text)
        except ValueError:
            fail("OCU_SMOKE_FORMER_URL is not an IPv4 host:port", 2)
        if address.version != 4:
            fail("OCU_SMOKE_FORMER_URL is not an IPv4 host:port", 2)
        return str(address), port
    fail("OCU_SMOKE_FORMER_URL is not an IPv4 host:port", 2)


def probe_tcp_refused(host: str, port: int, *, timeout: float = CONNECT_TIMEOUT) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
    except ConnectionRefusedError:
        return
    except TimeoutError:
        fail("former OCU publication timed out rather than refusing")
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.ECONNREFUSED:
            return
        fail(f"former OCU publication failed without ECONNREFUSED ({exc.errno or type(exc).__name__})")
    else:
        try:
            sock.close()
        except OSError:
            pass
        fail("former OCU publication accepted a connection")
    finally:
        try:
            sock.close()
        except OSError:
            pass


class _NoRedirect(urllib.request.HTTPErrorProcessor):
    def http_response(self, request, response):  # noqa: ANN001
        return response

    https_response = http_response


def _http_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))


def http_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = HTTP_TIMEOUT,
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    opener = _http_opener()
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = response.read()
            header_map = {key.lower(): value for key, value in response.headers.items()}
            return int(response.status), header_map, payload
    except urllib.error.HTTPError as exc:
        payload = exc.read() if exc.fp is not None else b""
        header_map = {key.lower(): value for key, value in exc.headers.items()} if exc.headers else {}
        return int(exc.code), header_map, payload
    except TimeoutError:
        fail(f"HTTP probe of {urlsplit(url).hostname} timed out")
    except urllib.error.URLError as orig:
        reason = orig.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            fail(f"HTTP probe of {urlsplit(url).hostname} timed out")
        fail(f"HTTP probe of {urlsplit(url).hostname} failed: {type(reason).__name__}")


def require_http_status(url: str, *, allow_any: bool = False) -> int:
    status, _headers, _body = http_request(url)
    if allow_any:
        if status < 100 or status > 599:
            fail(f"{urlsplit(url).hostname}: host listener did not return HTTP")
        return status
    if status < 200 or status > 399:
        fail(f"{urlsplit(url).hostname}: HTTP status {status} is not success")
    return status


def parse_curl_writeout(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for token in text.strip().split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        fields[key] = value
    required = ("exitcode", "num_connects", "remote_ip", "time_connect", "http_code")
    if any(name not in fields for name in required):
        fail("sandbox curl write-out is malformed")
    return fields


def sandbox_curl(container: str, url: str, *, connect: str = SANDBOX_CONNECT, maximum: str = SANDBOX_MAX) -> dict:
    argv = [
        "docker",
        "exec",
        "-u",
        "assistant",
        container,
        "curl",
        "-sS",
        "--noproxy",
        "*",
        "--path-as-is",
        "--max-redirs",
        "0",
        "--connect-timeout",
        connect,
        "--max-time",
        maximum,
        "-o",
        "/dev/null",
        "-w",
        CURL_WRITE_OUT,
        url,
    ]
    result = run_cmd(argv, timeout=float(maximum) + 5)
    combined = suppress_unsafe((result.stdout or "") + "\n" + (result.stderr or ""))
    if result.returncode == 127 or "command not found" in combined.lower():
        fail("curl is absent in the sandbox image", 2)
    try:
        fields = parse_curl_writeout(result.stdout or "")
    except SmokeError:
        if result.returncode not in {0, 28, 7, 6, 5, 52, 56}:
            fail(f"sandbox curl failed ({result.returncode})")
        raise
    if fields["exitcode"] != str(result.returncode):
        fail("sandbox curl process exit disagrees with write-out")
    fields["process_exit"] = str(result.returncode)
    return fields


def connected(fields: dict[str, str]) -> bool:
    http_code = fields.get("http_code", "0")
    if http_code not in {"", "0", "000"}:
        return True
    remote = (fields.get("remote_ip") or "").strip()
    if remote and remote not in {"0.0.0.0"}:
        return True
    try:
        return int(fields.get("num_connects") or "0") > 0 or float(fields.get("time_connect") or "0") > 0
    except ValueError:
        return True


def require_blocked(fields: dict[str, str], name: str) -> None:
    exitcode = fields.get("exitcode") or fields.get("process_exit")
    try:
        code = int(exitcode)
    except (TypeError, ValueError):
        fail(f"{name}: sandbox curl exit is malformed")
    http_code = fields.get("http_code", "0")
    if code == 6:
        fail(f"{name}: DNS failure, not policy")
    if code == 7:
        fail(f"{name}: connection refused, not L3 isolation")
    if code == 5:
        fail(f"{name}: proxy environment used")
    if code == 127:
        fail("curl is absent in the sandbox image", 2)
    if code == 0 or (http_code not in {"", "0", "000"}):
        fail(f"{name}: sandbox reached the control plane")
    if code == 28:
        if connected(fields) or http_code not in {"", "0", "000"}:
            fail(f"{name}: timeout after TCP connection, not DROP")
        return
    fail(f"{name}: sandbox probe failed ({code})")


def require_allowed(fields: dict[str, str]) -> None:
    try:
        code = int(fields.get("exitcode") or fields.get("process_exit"))
        status = int(fields.get("http_code") or "0")
    except (TypeError, ValueError):
        fail("allowlisted egress write-out is malformed")
    if code != 0:
        fail("allowlisted egress did not succeed")
    if status < 200 or status > 399:
        fail(f"allowlisted egress HTTP status {status} is not success")


def exec_text(container: str, command: str, *, timeout: float = 10.0) -> subprocess.CompletedProcess:
    return docker(
        "exec",
        "-u",
        "assistant",
        container,
        "bash",
        "-c",
        command,
        timeout=timeout,
    )


def config_env(payload: dict) -> list[str]:
    config = payload.get("Config") if isinstance(payload, dict) else None
    values = config.get("Env") if isinstance(config, dict) else None
    if not isinstance(values, list):
        return []
    return [str(item) for item in values]


def exclusive_ack_ok(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes"}


def load_inputs() -> dict:
    project = require_nonempty("COMPOSE_PROJECT_NAME")
    published = require_nonempty("OCU_PROXY_PORT")
    private_name = require_nonempty("OCU_PRIVATE_NETWORK")
    sandbox_name = require_nonempty("OCU_SANDBOX_NETWORK")
    origin = require_nonempty("OCU_WEBUI_ORIGIN")
    require_present("OCU_SANDBOX_EGRESS_ALLOW")
    require_nonempty("OCU_PRIVATE_SUBNET")
    chat_id = require_nonempty("OCU_SMOKE_CHAT_ID")
    sandbox_id = require_nonempty("OCU_SMOKE_SANDBOX_ID")
    if not exclusive_ack_ok(require_nonempty("OCU_SMOKE_EXCLUSIVE")):
        fail("OCU_SMOKE_EXCLUSIVE must acknowledge exclusive smoke ownership", 2)
    token = require_nonempty("OCU_SMOKE_OWNER_TOKEN")
    former_raw = os.environ.get("OCU_SMOKE_FORMER_URL", "").strip()
    if not former_raw:
        fail("OCU_SMOKE_FORMER_URL is required", 2)
    egress_raw = os.environ.get("OCU_SMOKE_EGRESS_URL", "").strip()
    if not egress_raw:
        fail("OCU_SMOKE_EGRESS_URL is required", 2)
    lan_raw = require_nonempty("OCU_SMOKE_HOST_LAN_IPV4")
    try:
        lan = parse_ipv4_address("OCU_SMOKE_HOST_LAN_IPV4", lan_raw)
        control = parse_ipv4_network("OCU_PRIVATE_SUBNET", os.environ["OCU_PRIVATE_SUBNET"])
        allow = parse_allowlist(os.environ["OCU_SANDBOX_EGRESS_ALLOW"])
    except NetworkInspectError as exc:
        fail(str(exc), 2)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        raise SmokeError(str(exc), 2 if code == 1 else code) from exc
    former_host, former_port = former_target(former_raw)
    egress_host, egress_port, egress_path = parse_literal_url("OCU_SMOKE_EGRESS_URL", egress_raw)
    egress_ip = ipaddress.ip_address(egress_host)
    if egress_ip in METADATA or egress_ip in control:
        fail("OCU_SMOKE_EGRESS_URL is a protected destination", 2)
    if not allow:
        fail("deny-all allowlist cannot satisfy required positive egress", 2)
    if not any(egress_ip in network for network in allow):
        fail("OCU_SMOKE_EGRESS_URL is not in OCU_SANDBOX_EGRESS_ALLOW", 2)
    parts = urlsplit(origin)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        fail("OCU_WEBUI_ORIGIN is not a usable public origin", 2)
    return {
        "project": project,
        "published": published,
        "private_name": private_name,
        "sandbox_name": sandbox_name,
        "origin": origin.rstrip("/"),
        "chat_id": chat_id,
        "sandbox_id": sandbox_id,
        "token": token,
        "former_host": former_host,
        "former_port": former_port,
        "egress_url": f"http://{egress_host}:{egress_port}{egress_path}",
        "lan": str(lan),
    }


def collect_inventory(root: str, project: str) -> dict[str, dict]:
    stacks = []
    for name, args in compose_contexts(root, project):
        result = docker("compose", *args, "ps", "--all", "--format", "json")
        if result.returncode != 0:
            fail(f"{name}: compose ps failed", 2)
        stacks.append((name, parse_compose_ps(result.stdout or "", name)))
    return merge_inventory(stacks)


def resolve_control_endpoints(inventory: dict[str, dict], private_name: str) -> dict[str, tuple[str, int]]:
    payload = inspect_network(private_name)
    if payload is None:
        fail(f"{private_name}: control-plane network is missing", 2)
    network_id = str(payload.get("Id") or "").strip()
    if not network_id:
        fail(f"{private_name}: inspected network id is missing", 2)
    endpoints = {}
    for service, port in ((OCU, OCU_TARGET), (WEBUI, WEBUI_TARGET), (PROXY, int(PROXY_TARGET))):
        container_id, inspected = service_container(inventory, service)
        address = ipv4_on_network(inspected, private_name, network_id)
        endpoints[service] = (address, port)
        del container_id
    return endpoints


def resolve_sandbox(chat_id: str, sandbox_id: str, sandbox_name: str) -> dict:
    payload = inspect_required(sandbox_id)
    identity = container_identity(payload, sandbox_id)
    state = payload.get("State")
    running = False
    if isinstance(state, dict):
        running = bool(state.get("Running")) or str(state.get("Status") or "").lower() == "running"
    elif isinstance(state, str):
        running = state.lower() == "running"
    if not running:
        fail(f"{identity}: smoke sandbox is not running", 2)
    labels = payload.get("Labels") or (payload.get("Config") or {}).get("Labels") or {}
    if not isinstance(labels, dict):
        fail(f"{identity}: sandbox labels are missing", 2)
    if labels.get("managed-by") != MANAGED_LABEL:
        fail(f"{identity}: sandbox is not managed by the orchestrator", 2)
    if str(labels.get("chat-id") or "") != chat_id:
        fail(f"{identity}: sandbox chat-id does not match OCU_SMOKE_CHAT_ID", 2)
    network_payload = inspect_network(sandbox_name)
    if network_payload is None:
        fail(f"{sandbox_name}: sandbox network is missing", 2)
    network_id = str(network_payload.get("Id") or "").strip()
    membership = protected_membership(payload, network_name=sandbox_name, network_id=network_id)
    if membership is not True:
        fail(f"{identity}: sandbox is not on {sandbox_name}", 2)
    settings = payload.get("NetworkSettings") if isinstance(payload, dict) else None
    networks = settings.get("Networks") if isinstance(settings, dict) else None
    if not isinstance(networks, dict) or len(networks) != 1:
        fail(f"{identity}: sandbox must attach only to {sandbox_name}", 2)
    env = config_env(payload)
    if NO_AUTOSTART not in env:
        fail(f"{identity}: NO_AUTOSTART=1 missing; use a fresh smoke sandbox", 2)
    name = str(payload.get("Name") or "").lstrip("/") or sandbox_id
    return {"id": sandbox_id, "name": name, "payload": payload}


def _terminal_state(container: str, *, code: int = 2) -> tuple[bool, bool]:
    ttyd = exec_text(container, "pgrep -x ttyd")
    if ttyd.returncode not in {0, 1} or ttyd.stderr or (ttyd.returncode == 1 and ttyd.stdout):
        fail("ttyd presence probe failed", code)
    if ttyd.returncode == 0 and not (ttyd.stdout or "").strip().isdigit():
        fail("ttyd presence probe is malformed", code)
    tmux = exec_text(container, "tmux list-sessions -F '#{session_name}'")
    sessions = (tmux.stdout or "").strip().splitlines()
    if tmux.returncode == 0:
        if tmux.stderr or not sessions or any(not item.strip() for item in sessions):
            fail("tmux session inventory is malformed", code)
    elif tmux.returncode == 1:
        detail = (tmux.stderr or "").strip()
        if sessions or not (
            detail.startswith("no server running on ")
            or (detail.startswith("error connecting to ") and "No such file or directory" in detail)
        ):
            fail("tmux session inventory failed", code)
    else:
        fail("tmux session inventory failed", code)
    return ttyd.returncode == 0, bool(sessions)


def assert_terminal_absent(container: str, *, code: int = 2) -> None:
    ttyd, sessions = _terminal_state(container, code=code)
    if ttyd:
        fail("pre-existing ttyd process" if code == 2 else "owned ttyd survived cleanup", code)
    if sessions:
        fail("pre-existing assistant tmux session" if code == 2 else "owned tmux survived cleanup", code)


def precheck_terminal(container: str) -> None:
    assert_terminal_absent(container)
    marker = exec_text(container, "test -f /tmp/.no_autostart")
    if marker.returncode == 0 and not marker.stdout and not marker.stderr:
        fail("global no-autostart marker present; use a fresh smoke sandbox", 2)
    if marker.returncode != 1 or marker.stdout or marker.stderr:
        fail("global no-autostart marker probe failed", 2)
    started = exec_text(container, "printenv SUBAGENT_AUTOSTARTED")
    if started.returncode == 0:
        fail("stale SUBAGENT_AUTOSTARTED would make the negative vacuous", 2)
    if started.returncode != 1 or started.stdout or started.stderr:
        fail("autostart environment probe failed", 2)


def proxy_headers(origin: str, token: str) -> dict[str, str]:
    return {
        "Cookie": f"token={token}",
        "Origin": origin,
        "X-Requested-With": "ocu-workspace",
        "Content-Type": "application/json",
    }


def start_product_terminal(origin: str, chat_id: str, token: str) -> None:
    global _PENDING_START
    url = f"{origin}/ocu/terminal/{chat_id}/start-ttyd"
    body = json.dumps({"dangerous_mode": False}).encode("utf-8")
    status, _headers, payload = http_request(
        url,
        method="POST",
        headers=proxy_headers(origin, token),
        body=body,
    )
    if status != 200:
        fail(f"start-ttyd refused HTTP {status}", 2)
    try:
        document = json.loads(payload.decode("utf-8") or "{}")
    except (UnicodeError, json.JSONDecodeError):
        fail("start-ttyd returned malformed JSON", 2)
    if document.get("already_running") is True:
        _PENDING_START = None
        fail("start-ttyd reported already_running; refusing shared terminal", 2)
    if document.get("started") is not True:
        fail("start-ttyd did not start a new terminal", 2)


def stop_product_terminal(origin: str, chat_id: str, token: str) -> None:
    url = f"{origin}/ocu/terminal/{chat_id}/stop-ttyd"
    status, _headers, _payload = http_request(
        url,
        method="POST",
        headers=proxy_headers(origin, token),
        body=b"",
    )
    if status != 200:
        fail("owned stop-ttyd cleanup failed")


def collect_processes(container: str, exec_fn=exec_text) -> dict[int, tuple[int, int, int, int, str, str]]:
    """Read the sandbox's actual process and terminal foreground groups."""
    result = exec_fn(container, "ps -eo pid=,ppid=,pgid=,tpgid=,sess=,tty=,comm=")
    if result.returncode != 0 or not result.stdout or len(result.stdout) > 1_048_576:
        fail("terminal process inventory failed")
    records = {}
    for line in result.stdout.splitlines():
        columns = line.split(maxsplit=6)
        if len(columns) != 7 or not all(item.lstrip("-").isdigit() for item in columns[:5]):
            fail("terminal process inventory is malformed")
        pid, ppid, pgid, tpgid, sid = map(int, columns[:5])
        if pid < 0 or pid in records:
            fail("terminal process inventory has invalid identities")
        records[pid] = (ppid, pgid, tpgid, sid, columns[5], columns[6])
    return records


def pane_snapshot(container: str, exec_fn=exec_text) -> dict | None:
    tmux = exec_fn(
        container,
        "tmux display-message -p -t main '#{session_name}|#{pane_id}|#{pane_pid}|#{pane_tty}|#{pane_current_command}'",
    )
    if tmux.returncode != 0:
        if tmux.returncode == 1 and any(
            marker in (tmux.stderr or "") for marker in ("no current window", "can't find session", "no server running on ")
        ):
            return None
        fail("product tmux pane inspection failed")
    parts = (tmux.stdout or "").strip().split("|")
    if (
        len(parts) != 5
        or parts[0] != "main"
        or not parts[1].startswith("%")
        or not parts[2].isdigit()
        or int(parts[2]) <= 0
        or not parts[3].startswith("/dev/")
    ):
        fail("product tmux pane observation is malformed")
    return {
        "session": parts[0],
        "pane": parts[1],
        "pid": int(parts[2]),
        "tty": parts[3],
        "pane_command": parts[4],
        "processes": collect_processes(container, exec_fn),
    }


def foreground_is_bash(snapshot: dict) -> bool:
    pane_pid = snapshot["pid"]
    processes = snapshot["processes"]
    pane = processes.get(pane_pid)
    if pane is None or snapshot["pane_command"] not in BASH_NAMES:
        return False
    _, pgid, tpgid, sid, tty, comm = pane
    expected_tty = snapshot["tty"].removeprefix("/dev/")
    if tty != expected_tty or comm.split("/")[-1] not in BASH_NAMES:
        return False
    if tpgid <= 0 or tpgid != pgid or sid <= 0:
        return False
    foreground = [
        row for row in processes.values()
        if row[2] == tpgid and row[3] == sid and row[4] == tty
    ]
    if not foreground or any(row[1] == tpgid and row[5].split("/")[-1] not in BASH_NAMES for row in foreground):
        return False
    children = {pane_pid}
    changed = True
    while changed:
        changed = False
        for pid, row in processes.items():
            if pid not in children and row[0] in children:
                children.add(pid)
                changed = True
    return all(processes[pid][5].split("/")[-1] in BASH_NAMES for pid in children)


def observe_foreground(container: str, session: TtydSession) -> None:
    readiness_deadline = time.monotonic() + TERMINAL_DEADLINE
    while True:
        session.check_alive()
        snapshot = pane_snapshot(container)
        if snapshot is not None:
            if not foreground_is_bash(snapshot):
                fail("product terminal foreground is not plain bash")
            break
        if time.monotonic() >= readiness_deadline:
            fail("product terminal pane did not become ready")
        time.sleep(STABLE_INTERVAL)
    # Two seconds of pane PID, tpgid and descendant evidence, not universal
    # attribution of detached processes reparented outside this window.
    stable_until = time.monotonic() + STABLE_WINDOW
    while time.monotonic() < stable_until:
        time.sleep(min(STABLE_INTERVAL, stable_until - time.monotonic()))
        session.check_alive()
        snapshot = pane_snapshot(container)
        if snapshot is None or not foreground_is_bash(snapshot):
            fail("product terminal foreground changed during stable window")
    session.check_alive()


def open_terminal_session(origin: str, chat_id: str, token: str) -> TtydSession:
    headers = proxy_headers(origin, token)
    headers.pop("Content-Type", None)
    try:
        return connect_ttyd(
            origin,
            f"/ocu/terminal/{chat_id}/ws",
            headers,
            timeout=TERMINAL_DEADLINE,
        )
    except (TtydProtocolError, OSError) as exc:
        fail(f"ttyd websocket failed ({type(exc).__name__})")


def cleanup_owned(status: int) -> int:
    global _OWNED_SESSION, _OWNED_CLEANUP, _CLEANUP_DONE, _PENDING_START
    if _CLEANUP_DONE:
        return status
    _CLEANUP_DONE = True
    for signum in SIGNAL_EXITS:
        signal.signal(signum, signal.SIG_IGN)
    session, owned, pending = _OWNED_SESSION, _OWNED_CLEANUP, _PENDING_START
    _OWNED_SESSION = None
    _OWNED_CLEANUP = None
    _PENDING_START = None
    failed = False
    if session is not None:
        try:
            session.close()
        except Exception as exc:
            print(f"{PREFIX}: websocket cleanup failed ({type(exc).__name__})", file=sys.stderr)
            failed = True
    try:
        stop_owned_children()
    except Exception as exc:
        print(f"{PREFIX}: owned child cleanup failed ({type(exc).__name__})", file=sys.stderr)
        failed = True
    try:
        if owned is None and pending is not None:
            # The POST may have started ttyd before its response was observed.
            deadline = time.monotonic() + 5.0
            while True:
                ttyd, sessions = _terminal_state(pending["container"], code=1)
                if ttyd or sessions:
                    owned = pending
                    break
                if time.monotonic() >= deadline:
                    print(f"{PREFIX}: start-ttyd response unknown; no terminal observed", file=sys.stderr)
                    failed = True
                    break
                time.sleep(0.2)
        if owned:
            stop_product_terminal(owned["origin"], owned["chat_id"], owned["token"])
            deadline = time.monotonic() + 5.0
            while True:
                ttyd, sessions = _terminal_state(owned["container"], code=1)
                if not ttyd and not sessions:
                    break
                if time.monotonic() >= deadline:
                    fail("owned ttyd survived cleanup" if ttyd else "owned tmux survived cleanup")
                time.sleep(0.2)
    except Exception as exc:
        print(f"{PREFIX}: owned terminal cleanup failed ({type(exc).__name__})", file=sys.stderr)
        failed = True
    return 1 if failed else status


def handle_signal(signum, _frame) -> None:  # noqa: ANN001
    exit_code = SIGNAL_EXITS.get(signum, 143)
    raise SystemExit(cleanup_owned(exit_code))


def run(argv: list[str]) -> int:
    global _OWNED_SESSION, _OWNED_CLEANUP, _CLEANUP_DONE, _PENDING_START
    if len(argv) != 1:
        fail("unexpected arguments; configure the deployment shell and run deploy/smoke.sh", 2)
    _OWNED_SESSION = None
    _OWNED_CLEANUP = None
    _CLEANUP_DONE = False
    _PENDING_START = None
    _OWNED_PGIDS.clear()
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, handle_signal)
    inputs = load_inputs()
    root = script_root()
    inventory = collect_inventory(root, inputs["project"])
    judge_publications(inventory, inputs["published"])
    sandbox = resolve_sandbox(inputs["chat_id"], inputs["sandbox_id"], inputs["sandbox_name"])
    endpoints = resolve_control_endpoints(inventory, inputs["private_name"])
    probe_tcp_refused(inputs["former_host"], inputs["former_port"])
    control_urls = {
        OCU: f"http://{endpoints[OCU][0]}:{endpoints[OCU][1]}/",
        WEBUI: f"http://{endpoints[WEBUI][0]}:{endpoints[WEBUI][1]}/",
        PROXY: f"http://{endpoints[PROXY][0]}:{endpoints[PROXY][1]}/",
        "lan-proxy": f"http://{inputs['lan']}:{inputs['published']}/",
    }
    for url in control_urls.values():
        require_http_status(url, allow_any=True)
    require_allowed(sandbox_curl(sandbox["id"], inputs["egress_url"]))
    for name, url in control_urls.items():
        require_blocked(sandbox_curl(sandbox["id"], url), name)
    precheck_terminal(sandbox["id"])
    _PENDING_START = {
        "origin": inputs["origin"],
        "chat_id": inputs["chat_id"],
        "token": inputs["token"],
        "container": sandbox["id"],
    }
    start_product_terminal(inputs["origin"], inputs["chat_id"], inputs["token"])
    _OWNED_CLEANUP = _PENDING_START
    _PENDING_START = None
    _OWNED_SESSION = open_terminal_session(inputs["origin"], inputs["chat_id"], inputs["token"])
    observe_foreground(sandbox["id"], _OWNED_SESSION)
    return cleanup_owned(0)


def main(argv: list[str]) -> int:
    try:
        return run(argv)
    except SmokeError as exc:
        return cleanup_owned(exc.code)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        return cleanup_owned(code)
    except KeyboardInterrupt:
        print(f"{PREFIX}: interrupted", file=sys.stderr)
        return cleanup_owned(130)
    except TtydProtocolError as exc:
        print(f"{PREFIX}: {exc}", file=sys.stderr)
        return cleanup_owned(1)
    except Exception as exc:
        print(f"{PREFIX}: runtime failure ({type(exc).__name__})", file=sys.stderr)
        return cleanup_owned(1)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
