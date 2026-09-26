# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Native TCP/HTTP/WebSocket and process judges for overlay smoke."""

from __future__ import annotations

import json
import os
from pathlib import Path
import pty
import signal
import socket
import subprocess
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
if str(DEPLOY) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(DEPLOY))

from smoke_deployment import (
    SmokeError,
    collect_processes,
    connected,
    foreground_is_bash,
    pane_snapshot,
    parse_compose_ps,
    parse_curl_writeout,
    probe_tcp_refused,
    require_allowed,
    require_blocked,
    require_http_status,
)
from smoke_tty import TtydProtocolError, connect_ttyd, expected_accept


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
        self.server.init_release.wait(timeout=3)
        self.connection.settimeout(3)
        try:
            payload = self.rfile.read(2)
            if payload:
                size = payload[1] & 0x7F
                rest = self.rfile.read(4 + size)
                mask = rest[:4]
                data = bytes(byte ^ mask[i % 4] for i, byte in enumerate(rest[4:]))
                self.server.init = data
                self.server.init_seen.set()
            if self.server.drop_after_init:
                self.server.closed.set()
                return
            self.server.hold.wait(timeout=5)
        except OSError:
            return


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
            "exitcode=28 num_connects=0 remote_ip= time_connect=0.000 http_code=000"
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
        with self.assertRaises(SmokeError) as late:
            require_blocked(
                parse_curl_writeout(
                    "exitcode=28 num_connects=0 remote_ip= time_connect=0.01 http_code=000"
                ),
                "ocu",
            )
        self.assertIn("after TCP connection", str(late.exception))
        require_allowed(
            parse_curl_writeout("exitcode=0 num_connects=1 remote_ip=8.8.8.8 time_connect=0.02 http_code=302")
        )
        with self.assertRaises(SmokeError):
            require_allowed(
                parse_curl_writeout("exitcode=28 num_connects=0 remote_ip= time_connect=5 http_code=000")
            )

    def test_tty_handshake_requires_subprotocol_and_init(self):
        for selected in (True, False):
            server = ThreadingHTTPServer(("127.0.0.1", 0), TtyHandler)
            server.select_tty = selected
            server.upgraded = threading.Event()
            server.init_release = threading.Event()
            server.init_seen = threading.Event()
            server.drop_after_init = False
            server.closed = threading.Event()
            server.hold = threading.Event()
            server.init = b""
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            session = None
            try:
                origin = f"http://127.0.0.1:{server.server_address[1]}"
                if not selected:
                    server.init_release.set()
                    with self.assertRaises(TtydProtocolError):
                        connect_ttyd(origin, "/ocu/terminal/chat/ws", {"Origin": origin})
                    continue
                session = connect_ttyd(origin, "/ocu/terminal/chat/ws", {"Origin": origin, "Cookie": "token=x"})
                self.assertTrue(server.upgraded.wait(timeout=2))
                self.assertEqual(server.headers_seen.get("Sec-WebSocket-Protocol"), "tty")
                # Delayed receipt proves handshake completion is not confused with ttyd init.
                self.assertFalse(server.init_seen.is_set())
                server.init_release.set()
                self.assertTrue(server.init_seen.wait(timeout=2))
                self.assertEqual(json.loads(server.init.decode()), {"authToken": "", "columns": 80, "rows": 24})
                session.check_alive()
            finally:
                if session is not None:
                    session.close()
                server.init_release.set()
                server.hold.set()
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_ttyd_eof_after_init_is_not_a_held_connection(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), TtyHandler)
        server.select_tty = True
        server.upgraded = threading.Event()
        server.init_release = threading.Event()
        server.init_release.set()
        server.init_seen = threading.Event()
        server.closed = threading.Event()
        server.drop_after_init = True
        server.hold = threading.Event()
        server.init = b""
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        session = None
        try:
            origin = f"http://127.0.0.1:{server.server_address[1]}"
            session = connect_ttyd(origin, "/ocu/terminal/chat/ws", {"Origin": origin})
            self.assertTrue(server.init_seen.wait(timeout=2))
            self.assertTrue(server.closed.wait(timeout=2))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    session.check_alive()
                except TtydProtocolError:
                    break
                time.sleep(0.02)
            else:
                self.fail("client accepted an early ttyd EOF")
        finally:
            if session is not None:
                session.close()
            server.hold.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_native_pty_foreground_judge_rejects_harmless_cat(self):
        pane_pid, master = pty.fork()
        if pane_pid == 0:
            os.execvp("bash", ["bash", "--noprofile", "--norc", "-i"])
        try:
            def local_exec(_container, command):
                if command.startswith("tmux display-message"):
                    row = collect_processes("native", local_exec).get(pane_pid)
                    tty = row[4] if row else "?"
                    return SimpleNamespace(
                        returncode=0,
                        stdout=f"main|%1|{pane_pid}|/dev/{tty}|bash\n",
                        stderr="",
                    )
                return subprocess.run(["sh", "-c", command], capture_output=True, text=True, timeout=3)

            deadline = time.monotonic() + 5
            while True:
                records = collect_processes("native", local_exec)
                row = records.get(pane_pid)
                if row and row[2] == row[1] and row[4] not in {"?", "??"}:
                    break
                self.assertLess(time.monotonic(), deadline, "plain Bash never acquired the PTY")
                time.sleep(0.05)
            self.assertTrue(foreground_is_bash(pane_snapshot("native", local_exec)))
            os.write(master, b"cat\n")
            deadline = time.monotonic() + 5
            while True:
                snapshot = pane_snapshot("native", local_exec)
                pane = snapshot["processes"][pane_pid]
                if pane[2] != pane[1] and any(
                    row[5].split("/")[-1] == "cat" and row[1] == pane[2]
                    for row in snapshot["processes"].values()
                ):
                    break
                self.assertLess(time.monotonic(), deadline, "foreground cat was not observed")
                time.sleep(0.05)
            self.assertFalse(foreground_is_bash(snapshot))
            os.write(master, b"\x03")
            deadline = time.monotonic() + 5
            while True:
                snapshot = pane_snapshot("native", local_exec)
                if foreground_is_bash(snapshot):
                    break
                self.assertLess(time.monotonic(), deadline, "Bash did not regain foreground after Ctrl-C")
                time.sleep(0.05)
            self.assertTrue(foreground_is_bash(snapshot))
        finally:
            try:
                os.write(master, b"\x03exit\n")
            except OSError:
                pass
            deadline = time.monotonic() + 2
            while True:
                finished, _ = os.waitpid(pane_pid, os.WNOHANG)
                if finished:
                    break
                if time.monotonic() >= deadline:
                    os.killpg(pane_pid, signal.SIGKILL)
                    os.waitpid(pane_pid, 0)
                    break
                time.sleep(0.05)
            os.close(master)


if __name__ == "__main__":
    unittest.main()
