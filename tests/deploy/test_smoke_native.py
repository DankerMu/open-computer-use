# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Native TCP/HTTP/WebSocket and process judges for overlay smoke."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
if str(DEPLOY) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(DEPLOY))

from smoke_deployment import (
    SmokeError,
    connected,
    foreground_is_bash,
    parse_compose_ps,
    parse_curl_writeout,
    probe_tcp_refused,
    require_allowed,
    require_blocked,
    require_http_status,
)
from smoke_tty import TtydProtocolError, connect_ttyd, expected_accept, init_payload


def wait_port(host: str, port: int, timeout=2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            sock = socket.create_connection((host, port), timeout=0.2)
        except OSError:
            time.sleep(0.02)
            continue
        sock.close()
        return
    raise AssertionError(f"{host}:{port} never became ready")


class HangHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A003
        del format, args

    def do_GET(self):  # noqa: N802
        time.sleep(self.server.hang)


class OkHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A003
        del format, args

    def do_GET(self):  # noqa: N802
        self.send_response(204)
        self.end_headers()


class RedirectHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A003
        del format, args

    def do_GET(self):  # noqa: N802
        self.send_response(302)
        self.send_header("Location", "/next")
        self.end_headers()


class TtyHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A003
        del format, args

    def do_GET(self):  # noqa: N802
        self.server.headers_seen = dict(self.headers)
        key = self.headers.get("Sec-WebSocket-Key", "")
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", expected_accept(key))
        if self.server.select_tty:
            self.send_header("Sec-WebSocket-Protocol", "tty")
        self.end_headers()
        self.server.upgraded.set()
        payload = self.rfile.read(2)
        if payload:
            size = payload[1] & 0x7F
            rest = self.rfile.read(4 + size)
            mask = rest[:4]
            data = bytes(byte ^ mask[i % 4] for i, byte in enumerate(rest[4:]))
            self.server.init = data
        self.server.hold.wait(timeout=2)


class NativeSmokeTests(unittest.TestCase):
    def test_compose_ps_accepts_array_and_json_lines(self):
        rows = parse_compose_ps('{"Service":"proxy","ID":"a","State":"running","Publishers":[]}\n', "proxy")
        self.assertEqual(rows[0]["Service"], "proxy")
        rows = parse_compose_ps('[{"Service":"proxy","ID":"a","State":"running","Publishers":[]}]', "proxy")
        self.assertEqual(len(rows), 1)
        with self.assertRaises(SmokeError):
            parse_compose_ps("not-json\n", "proxy")

    def test_former_port_requires_econnrefused(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        probe_tcp_refused("127.0.0.1", port)
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        live = listener.getsockname()[1]
        try:
            with self.assertRaises(SmokeError) as raised:
                probe_tcp_refused("127.0.0.1", live, timeout=0.5)
            self.assertIn("accepted", str(raised.exception))
        finally:
            listener.close()

    def test_host_http_success_and_dead_listener(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), OkHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            self.assertEqual(require_http_status(f"http://127.0.0.1:{port}/", allow_any=True), 204)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        dead = sock.getsockname()[1]
        sock.close()
        with self.assertRaises(SmokeError):
            require_http_status(f"http://127.0.0.1:{dead}/", allow_any=True)

    def test_redirect_is_not_followed(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            status = require_http_status(f"http://127.0.0.1:{port}/", allow_any=True)
            self.assertEqual(status, 302)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_curl_classifier_separates_false_isolation_signals(self):
        blocked = parse_curl_writeout(
            "exitcode=28 num_connects=0 remote_ip= time_connect=5.001 http_code=000"
        )
        require_blocked(blocked, "ocu")
        with self.assertRaises(SmokeError) as dns:
            require_blocked(
                parse_curl_writeout("exitcode=6 num_connects=0 remote_ip= time_connect=0 http_code=000"),
                "ocu",
            )
        self.assertIn("DNS", str(dns.exception))
        with self.assertRaises(SmokeError) as refused:
            require_blocked(
                parse_curl_writeout("exitcode=7 num_connects=0 remote_ip= time_connect=0 http_code=000"),
                "ocu",
            )
        self.assertIn("refused", str(refused.exception))
        post = parse_curl_writeout(
            "exitcode=28 num_connects=1 remote_ip=172.30.0.8 time_connect=0.01 http_code=000"
        )
        self.assertTrue(connected(post))
        with self.assertRaises(SmokeError) as hang:
            require_blocked(post, "ocu")
        self.assertIn("after TCP connection", str(hang.exception))
        require_allowed(
            parse_curl_writeout("exitcode=0 num_connects=1 remote_ip=8.8.8.8 time_connect=0.02 http_code=302")
        )
        with self.assertRaises(SmokeError):
            require_allowed(
                parse_curl_writeout("exitcode=28 num_connects=0 remote_ip= time_connect=5 http_code=000")
            )

    def test_tty_handshake_requires_subprotocol_and_init(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), TtyHandler)
        server.select_tty = True
        server.upgraded = threading.Event()
        server.hold = threading.Event()
        server.init = b""
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            origin = f"http://127.0.0.1:{server.server_address[1]}"
            session = connect_ttyd(origin, "/ocu/terminal/chat/ws", {"Origin": origin, "Cookie": "token=x"})
            self.assertTrue(server.upgraded.wait(timeout=2))
            self.assertEqual(server.headers_seen.get("Sec-WebSocket-Protocol"), "tty")
            self.assertEqual(json.loads(server.init.decode()), {"authToken": "", "columns": 80, "rows": 24})
            self.assertEqual(init_payload(), b'{"authToken":"","columns":80,"rows":24}')
            session.close()
        finally:
            server.hold.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        server = ThreadingHTTPServer(("127.0.0.1", 0), TtyHandler)
        server.select_tty = False
        server.upgraded = threading.Event()
        server.hold = threading.Event()
        server.init = b""
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            origin = f"http://127.0.0.1:{server.server_address[1]}"
            with self.assertRaises(TtydProtocolError):
                connect_ttyd(origin, "/ocu/terminal/chat/ws", {"Origin": origin})
        finally:
            server.hold.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_foreground_judge_rejects_autostarted_cat(self):
        with tempfile.TemporaryDirectory(prefix="ocu-smoke-fg-") as raw:
            work = Path(raw)
            script = work / "autostart.sh"
            script.write_text(
                "#!/usr/bin/env bash\n"
                "if [ -z \"${NO_AUTOSTART:-}\" ]; then\n"
                "  exec cat >/dev/null\n"
                "fi\n"
                "exec bash --noprofile --norc\n",
                encoding="utf-8",
            )
            script.chmod(0o700)
            started = subprocess.Popen(
                ["bash", "--noprofile", "--norc", str(script)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                time.sleep(0.2)
                comm = Path(f"/proc/{started.pid}/comm").read_text().strip() if Path("/proc").exists() else ""
                if not comm:
                    listed = subprocess.check_output(["ps", "-o", "comm=", "-p", str(started.pid)], text=True)
                    comm = listed.strip()
                snapshot = {
                    "pane_command": "bash",
                    "comm": "bash" if comm == "bash" else comm,
                    "pgrp": f"{started.pid} {started.pid} {comm} {comm}",
                    "tree": f"{started.pid} 1 {started.pid} {comm} {comm}",
                }
                if comm == "cat":
                    snapshot["pane_command"] = "cat"
                    snapshot["comm"] = "cat"
                    self.assertFalse(foreground_is_bash(snapshot))
                else:
                    children = subprocess.check_output(
                        ["ps", "-ax", "-o", "pid=,ppid=,comm="],
                        text=True,
                    )
                    tree = "\n".join(
                        line for line in children.splitlines() if str(started.pid) in line.split()[:2]
                    )
                    snapshot["tree"] = tree
                    snapshot["comm"] = comm
                    self.assertFalse(foreground_is_bash({**snapshot, "pane_command": "bash", "comm": "bash", "tree": tree + "\ncat"}))
            finally:
                started.kill()
                started.wait(timeout=2)
            bash = subprocess.Popen(
                ["bash", "--noprofile", "--norc", "-c", "sleep 3"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                listed = subprocess.check_output(["ps", "-o", "comm=", "-p", str(bash.pid)], text=True).strip()
                snapshot = {
                    "pane_command": "bash",
                    "comm": listed.split("/")[-1],
                    "pgrp": f"{bash.pid} {bash.pid} bash bash --noprofile --norc -c sleep 3",
                    "tree": f"{bash.pid} 1 {bash.pid} bash bash --noprofile --norc -c sleep 3",
                }
                if snapshot["comm"] in {"bash", "-bash"}:
                    self.assertTrue(foreground_is_bash(snapshot))
            finally:
                bash.kill()
                bash.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
