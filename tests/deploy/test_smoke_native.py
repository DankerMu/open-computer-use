# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Native TCP/HTTP/WebSocket and process judges for overlay smoke."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import pty
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
if str(DEPLOY) not in sys.path:
    sys.path.insert(0, str(DEPLOY))

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


def native_exec(_container, command):
    """Only adapt Darwin SID representation; every other ps field stays native."""
    needs_sid = sys.platform == "darwin" and "exec ps -eo pid=,ppid=,pgid=,tpgid=,sess=,tty=,comm=" in command
    native_command = command.replace("tpgid=,sess=,tty=", "tpgid=,tty=") if needs_sid else command
    result = subprocess.run(["sh", "-c", native_command], capture_output=True, text=True, timeout=3)
    if not needs_sid or result.returncode:
        return result
    lines = result.stdout.splitlines()
    if not lines or not lines[0].startswith("OCU_SMOKE_OBSERVER="):
        return subprocess.CompletedProcess(result.args, 1, "", "native observer header missing")
    output = [lines[0]]
    for line in lines[1:]:
        fields = line.split(maxsplit=5)
        if len(fields) != 6 or not fields[0].isdigit():
            return subprocess.CompletedProcess(result.args, 1, "", "native process row malformed")
        pid = int(fields[0])
        if pid == 0:
            continue
        try:
            sid = os.getsid(pid)
        except (ProcessLookupError, PermissionError):
            # A vanished/inaccessible host process is not evidence about the pane.
            continue
        output.append(" ".join([*fields[:4], str(sid), *fields[4:]]))
    return subprocess.CompletedProcess(result.args, 0, "\n".join(output) + "\n", result.stderr)


def pane_exec(pane_pid):
    def execute(container, command):
        if command.startswith("tmux display-message"):
            row = collect_processes(container, native_exec).get(pane_pid)
            tty = row[4] if row else "?"
            return SimpleNamespace(
                returncode=0, stdout=f"main|%1|{pane_pid}|/dev/{tty}|bash\n", stderr="",
            )
        return native_exec(container, command)
    return execute


def start_real_foreground_cat(pane_pid, master, exec_fn):
    os.write(master, b"cat\n")
    deadline = time.monotonic() + 5
    while True:
        snapshot = pane_snapshot("native", exec_fn)
        pane = snapshot["processes"][pane_pid]
        for pid, row in snapshot["processes"].items():
            if (
                pane[2] != pane[1]
                and row[5].split("/")[-1] == "cat"
                and row[1] == pane[2]
                and row[3] == pane[3]
                and row[4] == pane[4]
            ):
                return snapshot, pid, row[1]
        if time.monotonic() >= deadline:
            raise AssertionError("foreground cat was not observed")
        time.sleep(0.05)


def live_group(group):
    result = subprocess.run(
        ["ps", "-eo", "pgid=,stat="], capture_output=True, text=True, timeout=1,
    )
    if result.returncode:
        raise AssertionError("cannot inspect owned PTY process groups")
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == str(group) and not fields[1].startswith("Z"):
            return True
    return False

def owned_pid_exists(pid):
    result = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=1,
    )
    if result.returncode not in {0, 1}:
        raise AssertionError("cannot inspect owned PTY child")
    return bool(result.stdout.strip())


@contextmanager
def owned_bash_pty():
    """Real controlling PTY; never fork Python's threaded unittest process."""
    master, slave = pty.openpty()
    process = None
    groups: dict[int, int] = {}
    try:
        try:
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).with_name("pty_bash.py"))],
                stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
            )
            groups[process.pid] = process.pid
        finally:
            os.close(slave)
        yield process, master, groups
    finally:
        prior = sys.exc_info()[1]
        errors = []
        try:
            os.write(master, b"\x03")
        except OSError:
            pass
        # Reap the foreground child while Bash still owns and can wait for it.
        try:
            if process is not None:
                for group, child_pid in groups.items():
                    if group == process.pid:
                        continue
                    if group <= 0 or group == os.getpgrp():
                        errors.append("unsafe owned PTY process group")
                        continue
                    try:
                        os.killpg(group, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    deadline = time.monotonic() + 1
                    while owned_pid_exists(child_pid) and time.monotonic() < deadline:
                        time.sleep(0.05)
                    if owned_pid_exists(child_pid):
                        try:
                            os.killpg(group, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        deadline = time.monotonic() + 1
                        while owned_pid_exists(child_pid) and time.monotonic() < deadline:
                            time.sleep(0.05)
                        if owned_pid_exists(child_pid):
                            errors.append(f"PTY child {child_pid} not reaped")
        except (OSError, AssertionError, subprocess.TimeoutExpired) as exc:
            errors.append(f"PTY child cleanup: {type(exc).__name__}")
        finally:
            try:
                os.write(master, b"exit\n")
            except OSError:
                pass
            try:
                os.close(master)
            except OSError as exc:
                errors.append(f"master descriptor close: {type(exc).__name__}")
        if process is not None:
            if process.pid > 0 and process.pid != os.getpgrp():
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except OSError as exc:
                    errors.append(f"PTY TERM: {type(exc).__name__}")
        if process is not None:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                for group in groups:
                    if group > 0 and group != os.getpgrp():
                        try:
                            os.killpg(group, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        except OSError as exc:
                            errors.append(f"PTY KILL: {type(exc).__name__}")
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    errors.append("PTY child could not be reaped within 3 seconds")
            for group in groups:
                if group <= 0 or group == os.getpgrp():
                    continue
                try:
                    deadline = time.monotonic() + 1
                    while live_group(group) and time.monotonic() < deadline:
                        time.sleep(0.05)
                    if live_group(group):
                        os.killpg(group, signal.SIGKILL)
                        deadline = time.monotonic() + 1
                        while live_group(group) and time.monotonic() < deadline:
                            time.sleep(0.05)
                        if live_group(group):
                            errors.append(f"PTY process group {group} still running")
                except (OSError, AssertionError, subprocess.TimeoutExpired) as exc:
                    errors.append(f"PTY group inspection: {type(exc).__name__}")
        if errors:
            message = "; ".join(errors)
            if prior is not None:
                prior.add_note(f"PTY cleanup failure: {message}")
            else:
                raise AssertionError(f"PTY cleanup failure: {message}")


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
        with owned_bash_pty() as (process, master, groups):
            pane_pid = process.pid

            local_exec = pane_exec(pane_pid)

            deadline = time.monotonic() + 5
            while True:
                records = collect_processes("native", local_exec)
                row = records.get(pane_pid)
                if (
                    row and row[2] == row[1] and row[4] not in {"?", "??"}
                    and row[5].split("/")[-1] == "bash"
                ):
                    break
                self.assertLess(time.monotonic(), deadline, "plain Bash never acquired the PTY")
                time.sleep(0.05)
            self.assertTrue(foreground_is_bash(pane_snapshot("native", local_exec)))
            snapshot, cat_pid, cat_group = start_real_foreground_cat(pane_pid, master, local_exec)
            groups[cat_group] = cat_pid
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

    def test_failed_pty_assertion_still_closes_and_reaps(self):
        observed = {}
        started_cleanup = None
        with self.assertRaisesRegex(AssertionError, "forced PTY assertion") as caught:
            with owned_bash_pty() as (process, master, groups):
                observed["process"] = process
                observed["master"] = master
                local_exec = pane_exec(process.pid)
                deadline = time.monotonic() + 5
                while True:
                    records = collect_processes("native", local_exec)
                    row = records.get(process.pid)
                    if (
                        row and row[2] == row[1] and row[4] not in {"?", "??"}
                        and row[5].split("/")[-1] == "bash"
                    ):
                        break
                    self.assertLess(time.monotonic(), deadline, "PTY Bash never acquired the terminal")
                    time.sleep(0.05)
                _snapshot, cat_pid, cat_group = start_real_foreground_cat(process.pid, master, local_exec)
                groups[cat_group] = cat_pid
                observed["cat_pid"] = cat_pid
                observed["cat_group"] = cat_group
                started_cleanup = time.monotonic()
                raise AssertionError("forced PTY assertion")
        self.assertLess(time.monotonic() - started_cleanup, 12)
        self.assertFalse(any(
            note.startswith("PTY cleanup failure")
            for note in getattr(caught.exception, "__notes__", ())
        ))
        self.assertIsNotNone(observed["process"].poll())
        with self.assertRaises(OSError):
            os.fstat(observed["master"])
        self.assertFalse(live_group(observed["cat_group"]))
        self.assertFalse(owned_pid_exists(observed["cat_pid"]))


if __name__ == "__main__":
    unittest.main()
