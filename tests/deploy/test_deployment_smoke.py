# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Overlay smoke CLI against isolated fake Compose/exec/HTTP state."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from support import (
    CONTROL_NETWORK,
    ROOT,
    SANDBOX_NETWORK,
    SMOKE,
    ops,
    sandbox_container,
    smoke_env,
    tmp_dir,
    write_containers,
    write_curl,
    write_exec,
    write_network,
    write_ps,
    write_sandbox_terminal,
)

DESTRUCTIVE = ("network rm", "network disconnect", "compose down", " rm -f", " container rm", "compose up")
CHAT = "smoke-chat"
SANDBOX = "sandbox-smoke"
TOKEN = "synthetic-owner-token"
INTERNAL = "synthetic-internal-token"


def wait_for(predicate, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def descendant_pids(root_pid: int) -> set[int]:
    try:
        listed = subprocess.check_output(["ps", "-ax", "-o", "pid=,ppid="], text=True)
    except subprocess.CalledProcessError:
        return set()
    children: dict[int, list[int]] = {}
    for line in listed.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        pid, ppid = int(parts[0]), int(parts[1])
        children.setdefault(ppid, []).append(pid)
    found: set[int] = set()
    stack = [root_pid]
    while stack:
        current = stack.pop()
        for child in children.get(current, []):
            if child not in found:
                found.add(child)
                stack.append(child)
    return found


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def publisher(published="8082", target=8082, protocol="tcp", url="0.0.0.0"):
    return {
        "URL": url,
        "TargetPort": target,
        "PublishedPort": int(published) if str(published).isdigit() else published,
        "Protocol": protocol,
    }


def ps_row(service, cid, *, state="running", publishers=None, name=None):
    return {
        "ID": cid,
        "Name": name or cid,
        "Service": service,
        "State": state,
        "Publishers": publishers or [],
        "Project": "ocu-test",
    }


def running_state():
    return {"Status": "running", "Running": True}


def control_container(cid, name, service, address):
    return {
        "Id": cid,
        "Name": name,
        "State": running_state(),
        "Labels": {
            "com.docker.compose.service": service,
            "com.docker.compose.project": "ocu-test",
        },
        "HostConfig": {"NetworkMode": CONTROL_NETWORK},
        "NetworkSettings": {
            "Networks": {
                CONTROL_NETWORK: {"NetworkID": f"id-{CONTROL_NETWORK}", "IPAddress": address}
            }
        },
    }



class OkHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A003
        del format, args

    def do_GET(self):  # noqa: N802
        self.send_response(204)
        self.end_headers()

class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A003
        del format, args

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/ocu/terminal/") and self.path.endswith("/ws"):
            self.server.record.append(("WS", self.path, dict(self.headers)))
            if self.server.ws_mode == "stall":
                self.server.ws_started.set()
                self.server.ws_hold.wait(timeout=8)
                return
            key = self.headers.get("Sec-WebSocket-Key", "")
            accept = __import__("base64").b64encode(
                __import__("hashlib").sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
            ).decode()
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.send_header("Sec-WebSocket-Protocol", "tty")
            self.end_headers()
            self.server.ws_started.set()
            self.connection.settimeout(4)
            try:
                frame = self.rfile.read(2)
                if len(frame) != 2 or frame[0] != 0x81 or not frame[1] & 0x80:
                    return
                size = frame[1] & 0x7F
                masked = self.rfile.read(4 + size)
                if len(masked) != 4 + size:
                    return
                mask, body = masked[:4], masked[4:]
                payload = bytes(value ^ mask[i % 4] for i, value in enumerate(body))
                if json.loads(payload) != {"authToken": "", "columns": 80, "rows": 24}:
                    return
                self.server.ws_init.set()
                if self.server.ws_mode != "ignore-init":
                    terminal = json.loads(self.server.terminal_path.read_text(encoding="utf-8"))
                    terminal["sessions"] = ["main"]
                    self.server.terminal_path.write_text(json.dumps(terminal), encoding="utf-8")
                if self.server.ws_mode == "drop":
                    return
                self.server.ws_hold.wait(timeout=15)
            except (OSError, ValueError):
                return
            return
        self.server.record.append(("GET", self.path, dict(self.headers)))
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length) if length else b""
        self.server.record.append(("POST", self.path, dict(self.headers), body))
        if self.path.endswith("/start-ttyd"):
            if self.server.start_mode == "already":
                self._json({"started": False, "already_running": True})
                return
            if self.server.start_mode == "fail":
                self._json({"detail": "no"}, 500)
                return
            terminal = json.loads(self.server.terminal_path.read_text(encoding="utf-8"))
            terminal["ttyd"] = True
            self.server.terminal_path.write_text(json.dumps(terminal), encoding="utf-8")
            self.server.start_entered.set()
            if self.server.start_mode == "delayed":
                self.server.start_release.wait(timeout=7)
            try:
                self._json({"started": True})
            except OSError:
                pass
            return
        if self.path.endswith("/stop-ttyd"):
            self.server.stopped.set()
            if self.server.stop_mode == "delayed":
                self.server.stop_release.wait(timeout=5)
            if self.server.stop_mode == "fail":
                self._json({"detail": "cleanup failed"}, 500)
                return
            if self.server.stop_mode != "lie":
                terminal = json.loads(self.server.terminal_path.read_text(encoding="utf-8"))
                terminal["ttyd"] = False
                terminal["sessions"] = []
                if self.server.stop_mode == "probe-error":
                    terminal["probe_error"] = "pgrep -x ttyd"
                self.server.terminal_path.write_text(json.dumps(terminal), encoding="utf-8")
            self._json({"stopped": True})
            return
        self._json({"ok": True})


class OverlaySmokeCliTests(unittest.TestCase):
    def setUp(self):
        self.context = tmp_dir()
        self.state = Path(self.context.name)
        write_network(self.state, CONTROL_NETWORK, subnet="172.30.0.0/24", gateway="172.30.0.1")
        write_network(self.state, SANDBOX_NETWORK, subnet="172.31.0.0/24", gateway="172.31.0.1")
        self.env = smoke_env(self.state)
        self.former_reservation = socket.socket()
        self.former_reservation.bind(("127.0.0.1", 0))
        self.env["OCU_SMOKE_FORMER_URL"] = f"127.0.0.1:{self.former_reservation.getsockname()[1]}"
        self.env["OCU_SMOKE_HOST_LAN_IPV4"] = "127.0.0.12"
        self.http = None
        self.thread = None
        self.control = []

    def tearDown(self):
        if self.http is not None:
            self.http.ws_hold.set()
            self.http.start_release.set()
            self.http.stop_release.set()
            self.http.shutdown()
            self.http.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)
        for server, thread in self.control:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        if self.former_reservation is not None:
            self.former_reservation.close()
        self.context.cleanup()

    def start_control_listeners(self):
        for address, port in (("127.0.0.10", 8081), ("127.0.0.11", 8080), ("127.0.0.12", 8082)):
            try:
                server = ThreadingHTTPServer((address, port), OkHandler)
            except OSError as exc:
                self.fail(f"{address}:{port} is occupied; CLI smoke requires this listener ({exc})")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.control.append((server, thread))

    def start_http(self, *, start_mode="ok", stop_mode="ok", ws_mode="ok"):
        self.start_control_listeners()
        server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        server.record = []
        server.start_mode = start_mode
        server.stop_mode = stop_mode
        server.ws_mode = ws_mode
        server.ws_hold = threading.Event()
        server.ws_started = threading.Event()
        server.ws_init = threading.Event()
        server.start_entered = threading.Event()
        server.start_release = threading.Event()
        server.stop_release = threading.Event()
        server.stopped = threading.Event()
        server.terminal_path = self.state / "sandbox-terminal.json"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.http = server
        self.thread = thread
        port = server.server_address[1]
        self.former_reservation.close()
        self.former_reservation = None
        origin = f"http://127.0.0.1:{port}"
        self.env["OCU_WEBUI_ORIGIN"] = origin
        return port, origin

    def seed_inventory(self, *, extra_publishers=None, extra_rows=None, sandbox_env=None, pane=None, processes=None):
        extra_publishers = extra_publishers or {}
        write_ps(
            self.state,
            "core",
            [
                ps_row("computer-use-server", "cid-ocu"),
                ps_row("retention-guard", "cid-ret"),
                ps_row("workspace", "cid-ws", state="exited"),
            ],
        )
        write_ps(
            self.state,
            "webui",
            [
                ps_row("open-webui", "cid-webui", publishers=extra_publishers.get("open-webui")),
                ps_row("postgres", "cid-pg"),
                ps_row("open-webui-init", "cid-init", state="exited"),
            ],
        )
        proxy_pubs = extra_publishers.get("proxy", [publisher()])
        rows = [ps_row("proxy", "cid-proxy", publishers=proxy_pubs)]
        if extra_rows:
            rows.extend(extra_rows)
        write_ps(self.state, "proxy", rows)
        sandbox = sandbox_container(
            cid=SANDBOX,
            name=f"owui-chat-{CHAT}",
            state=running_state(),
            labels={
                "managed-by": "mcp-computer-use-orchestrator",
                "chat-id": CHAT,
                "tool": "computer-use-mcp",
            },
            config_env=sandbox_env if sandbox_env is not None else ["NO_AUTOSTART=1", "PATH=/usr/bin"],
        )
        write_containers(
            self.state,
            [
                control_container("cid-ocu", "ocu-test-computer-use-server", "computer-use-server", "127.0.0.10"),
                control_container("cid-webui", "ocu-test-open-webui", "open-webui", "127.0.0.11"),
                control_container("cid-proxy", "ocu-test-proxy", "proxy", "127.0.0.12"),
                control_container("cid-pg", "ocu-test-postgres", "postgres", "127.0.0.13"),
                control_container("cid-ret", "ocu-test-retention-guard", "retention-guard", "127.0.0.14"),
                sandbox,
            ],
        )
        write_sandbox_terminal(
            self.state,
            {
                "ttyd": False,
                "sessions": [],
                "no_autostart_marker": False,
                "subagent_autostarted": "",
                "pane": pane or {
                    "session": "main",
                    "pane_id": "%1",
                    "pid": "9001",
                    "tty": "/dev/pts/0",
                    "command": "bash",
                },
                "processes": processes or ["9001 9000 9001 9001 9001 pts/0 bash"],
            },
        )

    def seed_probes(self, *, blocked=None, allowed=None):
        blocked = blocked or {
            "code": 28,
            "num_connects": 0,
            "remote_ip": "",
            "http_code": "000",
        }
        allowed = allowed or {
            "url": "http://8.8.8.8:80/",
            "code": 0,
            "num_connects": 1,
            "remote_ip": "8.8.8.8",
            "http_code": "200",
        }
        rows = [allowed]
        for match in ("8081", "8080", "8082", str(self.env["OCU_PROXY_PORT"])):
            row = dict(blocked)
            row["match"] = match
            rows.append(row)
        write_curl(self.state, rows)

    def run_smoke(self, extra=None, timeout=20):
        env = dict(self.env)
        if extra:
            env.update(extra)
        return subprocess.run(
            ["bash", str(SMOKE)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=timeout,
        )

    def combined(self, result):
        return (result.stdout or "") + (result.stderr or "")

    def assert_no_secrets(self, result):
        text = self.combined(result)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(INTERNAL, text)
        self.assertNotIn("synthetic-internal-token", " ".join(ops(self.state)))

    def test_missing_exclusive_acknowledgement_is_prerequisite(self):
        result = self.run_smoke({"OCU_SMOKE_EXCLUSIVE": ""})
        self.assertEqual(result.returncode, 2)
        self.assertIn("OCU_SMOKE_EXCLUSIVE", result.stderr)

    def test_unexpected_arguments_are_prerequisite(self):
        result = subprocess.run(
            ["bash", str(SMOKE), "extra"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=self.env,
            check=False,
            timeout=10,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("unexpected arguments", result.stderr)

    def test_deny_all_allowlist_is_unsuitable_prerequisite(self):
        result = self.run_smoke({"OCU_SANDBOX_EGRESS_ALLOW": ""})
        self.assertEqual(result.returncode, 2)
        self.assertIn("deny-all", result.stderr)
        self.assertNotIn("PASS", result.stdout)

    def test_unlisted_egress_target_is_prerequisite(self):
        result = self.run_smoke({"OCU_SMOKE_EGRESS_URL": "http://9.9.9.9:80/"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("OCU_SMOKE_EGRESS_URL", result.stderr)

    def test_protected_egress_target_is_rejected(self):
        result = self.run_smoke({"OCU_SMOKE_EGRESS_URL": "http://169.254.169.254:80/"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("protected", result.stderr)

    def test_wrong_sandbox_chat_identity_is_prerequisite(self):
        self.start_http()
        self.seed_inventory()
        self.seed_probes()
        result = self.run_smoke({"OCU_SMOKE_CHAT_ID": "other-chat"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("chat-id", result.stderr)

    def test_incomplete_inventory_is_prerequisite(self):
        self.start_http()
        self.seed_inventory()
        write_ps(self.state, "core", [ps_row("computer-use-server", "cid-ocu")])
        result = self.run_smoke()
        self.assertEqual(result.returncode, 2)
        self.assertIn("retention-guard", result.stderr)

    def test_conflicting_compose_identity_is_rejected(self):
        self.start_http()
        self.seed_inventory()
        write_ps(
            self.state,
            "proxy",
            [
                ps_row("proxy", "cid-proxy", publishers=[publisher()]),
                ps_row("open-webui", "cid-webui", publishers=[publisher(published="3000", target=8080)]),
            ],
        )
        result = self.run_smoke()
        self.assertEqual(result.returncode, 2)
        self.assertIn("conflicting", result.stderr)

    def test_array_compose_inventory_is_accepted(self):
        self.start_http()
        self.seed_inventory()
        self.seed_probes()
        write_ps(
            self.state,
            "proxy",
            json.dumps([ps_row("proxy", "cid-proxy", publishers=[publisher()])]),
        )
        result = self.run_smoke()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_proxy_ipv4_and_ipv6_wildcard_twins_are_one_publication(self):
        self.start_http()
        self.seed_inventory(extra_publishers={"proxy": [publisher(), publisher(url="::")]})
        self.seed_probes()
        result = self.run_smoke()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_wrong_ipv6_twin_or_ipv6_only_is_not_the_port_matrix(self):
        self.seed_inventory(extra_publishers={"proxy": [publisher(), publisher(url="::", target=80)]})
        result = self.run_smoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("proxy", result.stderr)
        self.seed_inventory(extra_publishers={"proxy": [publisher(url="::")]})
        result = self.run_smoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("proxy", result.stderr)

    def test_non_proxy_publication_fails_named_assertion(self):
        self.start_http()
        self.seed_inventory(extra_publishers={"open-webui": [publisher(published="3000", target=8080)]})
        self.seed_probes()
        result = self.run_smoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("open-webui", result.stderr)
        self.assertIn("host publication", result.stderr)

    def test_proxy_wrong_mapping_fails(self):
        self.start_http()
        self.seed_inventory(extra_publishers={"proxy": [publisher(published="9999")]})
        self.seed_probes()
        result = self.run_smoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("proxy", result.stderr)

    def test_running_cleanup_fails(self):
        self.start_http()
        self.seed_inventory(extra_rows=[ps_row("cleanup", "cid-clean", state="running")])
        self.seed_probes()
        result = self.run_smoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cleanup", result.stderr)

    def test_former_port_listener_is_not_absence(self):
        port, _origin = self.start_http()
        self.seed_inventory()
        self.seed_probes()
        sock = socket.create_connection(("127.0.0.1", port), timeout=1)
        try:
            result = self.run_smoke({"OCU_SMOKE_FORMER_URL": f"127.0.0.1:{port}"})
        finally:
            sock.close()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("former OCU publication accepted", result.stderr)

    def test_dns_probe_is_not_isolation(self):
        self.start_http()
        self.seed_inventory()
        self.seed_probes(blocked={"code": 6, "num_connects": 0, "remote_ip": "", "http_code": "000"})
        result = self.run_smoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DNS failure", result.stderr)

    def test_refused_probe_is_not_isolation(self):
        self.start_http()
        self.seed_inventory()
        self.seed_probes(blocked={"code": 7, "num_connects": 0, "remote_ip": "", "http_code": "000"})
        result = self.run_smoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("connection refused", result.stderr)

    def test_post_connect_timeout_is_not_drop(self):
        self.start_http()
        self.seed_inventory()
        self.seed_probes(
            blocked={"code": 28, "num_connects": 1, "remote_ip": "127.0.0.1", "http_code": "000"}
        )
        result = self.run_smoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("timeout after TCP connection", result.stderr)

    def test_curl_writeout_cannot_forge_process_exit_28(self):
        self.start_http()
        self.seed_inventory()
        self.seed_probes(blocked={
            "code": 0,
            "stdout": "exitcode=28 num_connects=0 remote_ip= time_connect=5 http_code=000",
        })
        result = self.run_smoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("process exit disagrees", result.stderr)

    def test_unreachable_allowlisted_target_voids_isolation(self):
        self.start_http()
        self.seed_inventory()
        self.seed_probes(allowed={"url": "http://8.8.8.8:80/", "code": 28, "num_connects": 0, "remote_ip": "", "http_code": "000"})
        result = self.run_smoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("allowlisted egress", result.stderr)
        self.assertNotIn("PASS", result.stdout)

    def test_preexisting_ttyd_is_untouched(self):
        self.start_http()
        self.seed_inventory()
        self.seed_probes()
        write_sandbox_terminal(
            self.state,
            {
                "ttyd": True,
                "sessions": ["main"],
                "no_autostart_marker": False,
                "subagent_autostarted": "",
                "pane": {"session": "main", "pane_id": "%1", "pid": "1", "command": "bash", "comm": "bash", "tree": ""},
            },
        )
        result = self.run_smoke()
        self.assertEqual(result.returncode, 2)
        self.assertIn("pre-existing ttyd", result.stderr)
        self.assertFalse(self.http.stopped.is_set())
        self.assertTrue(json.loads((self.state / "sandbox-terminal.json").read_text())["ttyd"])

    def test_failed_presence_probe_refuses_before_mutation(self):
        self.start_http()
        self.seed_inventory()
        self.seed_probes()
        write_exec(self.state, [{"container": SANDBOX, "match": "pgrep -x ttyd", "code": 2}])
        before = (self.state / "sandbox-terminal.json").read_bytes()
        result = self.run_smoke()
        self.assertEqual(result.returncode, 2)
        self.assertIn("ttyd presence probe failed", result.stderr)
        self.assertEqual((self.state / "sandbox-terminal.json").read_bytes(), before)
        self.assertFalse(any(item[0] == "POST" for item in self.http.record))

    def test_preexisting_tmux_and_global_marker_refuse_untouched(self):
        self.start_http()
        self.seed_inventory()
        self.seed_probes()
        path = self.state / "sandbox-terminal.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state["sessions"] = ["user"]
        path.write_text(json.dumps(state), encoding="utf-8")
        result = self.run_smoke()
        self.assertEqual(result.returncode, 2)
        self.assertIn("pre-existing assistant tmux", result.stderr)
        self.assertFalse(self.http.stopped.is_set())
        state["sessions"] = []
        state["no_autostart_marker"] = True
        path.write_text(json.dumps(state), encoding="utf-8")
        result = self.run_smoke()
        self.assertEqual(result.returncode, 2)
        self.assertIn("global no-autostart marker", result.stderr)
        self.assertFalse(any(item[0] == "POST" for item in self.http.record))

    def test_missing_fresh_no_autostart_refuses_before_terminal_mutation(self):
        self.start_http()
        self.seed_inventory(sandbox_env=["PATH=/usr/bin"])
        self.seed_probes()
        result = self.run_smoke()
        self.assertEqual(result.returncode, 2)
        self.assertIn("NO_AUTOSTART=1", result.stderr)
        self.assertFalse(any(item[0] == "POST" for item in self.http.record))

    def test_cli_foreground_is_rejected(self):
        self.start_http()
        self.seed_inventory(processes=[
            "9001 9000 9001 9100 9001 pts/0 bash",
            "9100 9001 9100 9100 9001 pts/0 cat",
        ])
        self.seed_probes()
        result = self.run_smoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("foreground", result.stderr)

    def test_observation_rejects_midwindow_foreground_transition(self):
        self.start_http()
        self.seed_inventory()
        self.seed_probes()
        path = self.state / "sandbox-terminal.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state["transition_after_reads"] = 3
        state["next_processes"] = [
            "9001 9000 9001 9100 9001 pts/0 bash",
            "9100 9001 9100 9100 9001 pts/0 harmless-child",
        ]
        path.write_text(json.dumps(state), encoding="utf-8")
        result = self.run_smoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stable window", result.stderr)
        self.assertTrue(self.http.stopped.is_set())

    def test_malformed_pane_pid_is_not_executed_or_accepted(self):
        self.start_http()
        self.seed_inventory(pane={
            "session": "main", "pane_id": "%1", "pid": "x;touch /tmp/not-owned",
            "tty": "/dev/pts/0", "command": "bash",
        })
        self.seed_probes()
        result = self.run_smoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("observation is malformed", result.stderr)
        self.assertTrue(self.http.stopped.is_set())

    def test_process_inventory_failure_is_not_plain_bash(self):
        self.start_http()
        self.seed_inventory()
        self.seed_probes()
        write_exec(self.state, [{
            "container": SANDBOX, "match": "ps -eo pid=,ppid=,pgid=,tpgid=,sess=,tty=,comm=",
            "code": 2, "stderr": "synthetic ps unavailable",
        }])
        result = self.run_smoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("terminal process inventory failed", result.stderr)
        self.assertTrue(self.http.stopped.is_set())

    def test_healthy_fake_deployment_exits_zero_without_secrets_or_destruction(self):
        self.start_http()
        self.seed_inventory()
        self.seed_probes()
        result = self.run_smoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_no_secrets(result)
        recorded = "\n".join(ops(self.state))
        for verb in DESTRUCTIVE:
            self.assertNotIn(verb, recorded)
        self.assertTrue(
            any(" compose " in line and " --all " in f" {line} " and " ps " in f" {line} " for line in ops(self.state))
        )
        posts = [item for item in self.http.record if item[0] == "POST"]
        self.assertTrue(any(item[1].endswith("/start-ttyd") for item in posts))
        start = next(item for item in posts if item[1].endswith("/start-ttyd"))
        self.assertEqual(json.loads(start[3].decode()), {"dangerous_mode": False})
        self.assertEqual(start[2].get("X-Requested-With"), "ocu-workspace")
        self.assertTrue(self.http.ws_started.is_set())
        self.assertTrue(self.http.ws_init.is_set())
        ws = next(item for item in self.http.record if item[0] == "WS")
        self.assertEqual(ws[2]["Sec-WebSocket-Protocol"], "tty")
        self.assertEqual(ws[2]["Cookie"], f"token={TOKEN}")
        self.assertTrue(self.http.stopped.is_set())
        self.assertFalse(json.loads((self.state / "sandbox-terminal.json").read_text())["ttyd"])

    def test_terminal_cannot_pass_without_product_ws_initialization(self):
        self.start_http(ws_mode="ignore-init")
        self.seed_inventory()
        self.seed_probes()
        result = self.run_smoke(timeout=25)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.http.ws_init.is_set())
        self.assertIn("pane did not become ready", result.stderr)
        self.assertTrue(self.http.stopped.is_set())

    def test_terminal_ws_drop_during_observation_fails_and_cleans(self):
        self.start_http(ws_mode="drop")
        self.seed_inventory()
        self.seed_probes()
        result = self.run_smoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("websocket", result.stderr)
        self.assertTrue(self.http.stopped.is_set())

    def test_stalled_ws_upgrade_after_start_still_cleans(self):
        self.start_http(ws_mode="stall")
        self.seed_inventory()
        self.seed_probes()
        result = self.run_smoke(timeout=25)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("websocket", result.stderr)
        self.assertTrue(self.http.stopped.is_set())

    def test_already_running_race_never_stops_unowned_terminal(self):
        self.start_http(start_mode="already")
        self.seed_inventory()
        self.seed_probes()
        result = self.run_smoke()
        self.assertEqual(result.returncode, 2)
        self.assertIn("already_running", result.stderr)
        self.assertFalse(self.http.stopped.is_set())

    def test_failed_cleanup_makes_command_fail(self):
        self.start_http(stop_mode="fail")
        self.seed_inventory()
        self.seed_probes()
        result = self.run_smoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stop-ttyd cleanup failed", result.stderr)

    def test_http_200_without_stopping_ttyd_is_cleanup_failure(self):
        self.start_http(stop_mode="lie")
        self.seed_inventory()
        self.seed_probes()
        result = self.run_smoke(timeout=25)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("owned ttyd survived cleanup", result.stderr)
        self.assertTrue(json.loads((self.state / "sandbox-terminal.json").read_text())["ttyd"])

    def test_unobservable_post_stop_state_is_cleanup_failure(self):
        self.start_http(stop_mode="probe-error")
        self.seed_inventory()
        self.seed_probes()
        result = self.run_smoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ttyd presence probe failed", result.stderr)

    def test_cancellation_while_start_response_is_pending_reconciles_owned_state(self):
        self.start_http(start_mode="delayed")
        self.seed_inventory()
        self.seed_probes()
        process = subprocess.Popen(
            ["bash", str(SMOKE)], cwd=str(ROOT), env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            self.assertTrue(self.http.start_entered.wait(timeout=5))
            process.send_signal(signal.SIGTERM)
            self.assertTrue(wait_for(lambda: process.poll() is not None, timeout=8))
            self.assertEqual(process.returncode, 143)
            self.assertTrue(self.http.stopped.is_set())
            self.assertFalse(json.loads((self.state / "sandbox-terminal.json").read_text())["ttyd"])
        finally:
            self.http.start_release.set()
            if process.poll() is None:
                process.send_signal(signal.SIGKILL)
            process.communicate(timeout=10)

    def test_repeated_term_during_owned_cleanup_does_not_abort_stop(self):
        self.start_http(stop_mode="delayed")
        self.seed_inventory()
        self.seed_probes()
        marker = self.state / "hold-exec"
        marker.write_text("1", encoding="utf-8")
        env = dict(self.env)
        env["FAKE_DOCKER_HOLD_EXEC"] = str(marker)
        process = subprocess.Popen(
            ["bash", str(SMOKE)], cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            self.assertTrue(wait_for(lambda: (self.state / "entered-exec").exists()))
            process.send_signal(signal.SIGTERM)
            self.assertTrue(self.http.stopped.wait(timeout=8))
            process.send_signal(signal.SIGTERM)
            self.http.stop_release.set()
            self.assertTrue(wait_for(lambda: process.poll() is not None, timeout=8))
            self.assertEqual(process.returncode, 143)
            self.assertFalse(json.loads((self.state / "sandbox-terminal.json").read_text())["ttyd"])
        finally:
            self.http.stop_release.set()
            marker.unlink(missing_ok=True)
            if process.poll() is None:
                process.send_signal(signal.SIGKILL)
            process.communicate(timeout=10)

    def test_cancellation_after_owned_start_cleans_terminal(self):
        self.start_http()
        self.seed_inventory()
        self.seed_probes()
        marker = self.state / "hold-exec"
        marker.write_text("1", encoding="utf-8")
        env = dict(self.env)
        env["FAKE_DOCKER_HOLD_EXEC"] = str(marker)
        process = subprocess.Popen(
            ["bash", str(SMOKE)],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertTrue(wait_for(lambda: (self.state / "entered-exec").exists()), "smoke never observed pane")
            children = descendant_pids(process.pid)
            process.send_signal(signal.SIGTERM)
            self.assertTrue(wait_for(lambda: process.poll() is not None, timeout=8), "smoke did not exit")
            self.assertTrue(wait_for(lambda: all(not pid_alive(pid) for pid in children), timeout=8))
            self.assertTrue(self.http.stopped.is_set())
            self.assertNotEqual(process.returncode, 0)
        finally:
            marker.unlink(missing_ok=True)
            if process.poll() is None:
                process.send_signal(signal.SIGKILL)
            process.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
