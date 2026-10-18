# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Real resource-retirement regressions for non-Office HTTP test origins."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import errno
from http.server import BaseHTTPRequestHandler
import socket
from pathlib import Path
import sys
import threading
import unittest
import tempfile
from unittest.mock import patch
from urllib.parse import urlsplit

from support import ROOT, serve_font_archive

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import test_deployment_smoke as deployment_smoke
import test_init_wrapper as init_wrapper
import test_smoke_native as smoke_native
from tests.orchestrator._http_origin import HttpOriginLifetime
from tests.test_tools import _local_origin


def _shutdown_socket(connection):
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError as error:
        if error.errno not in (errno.EBADF, errno.ENOTCONN, errno.EINVAL):
            raise


def _join_worker(worker):
    worker.join(timeout=5)
    assert not worker.is_alive(), "emergency cleanup left a request worker alive"


@contextmanager
def _observe_requests(*, handlers=None):
    entered = threading.Event()
    observed = []
    original_handle = BaseHTTPRequestHandler.handle

    def observe_handle(handler):
        if handlers is not None:
            handlers.append(handler)
        observed.append((
            threading.current_thread(), handler.connection, handler.server.socket,
        ))
        entered.set()
        return original_handle(handler)

    with patch.object(BaseHTTPRequestHandler, "handle", observe_handle):
        try:
            yield observed, entered
        finally:
            with ExitStack() as cleanup:
                for worker, accepted, listener in observed:
                    cleanup.callback(_join_worker, worker)
                    cleanup.callback(listener.close)
                    cleanup.callback(accepted.close)
                    cleanup.callback(_shutdown_socket, accepted)


def _assert_retired(test, observed):
    state_at_exit = [
        (worker.is_alive(), accepted.fileno(), listener.fileno())
        for worker, accepted, listener in observed
    ]
    test.assertEqual(state_at_exit, [(False, -1, -1)] * len(observed))



class LocalOriginLifetimeTests(unittest.TestCase):
    def test_partial_http_input_retires_request_resources_on_context_exit(self):
        entered = threading.Event()
        observed = []
        client = None
        original_handle = BaseHTTPRequestHandler.handle

        def observe_handle(handler):
            observed.append((
                threading.current_thread(),
                handler.connection,
                handler.server.socket,
            ))
            entered.set()
            return original_handle(handler)

        with patch.object(BaseHTTPRequestHandler, "handle", observe_handle):
            try:
                with _local_origin(lambda _request: (200, {}, b"")) as origin:
                    address = urlsplit(origin.url)
                    client = socket.create_connection(
                        (address.hostname, address.port), timeout=2,
                    )
                    client.sendall(
                        b"GET /partial HTTP/1.1\r\n"
                        b"Host: 127.0.0.1\r\n"
                        b"X-Pending: incomplete"
                    )
                    self.assertTrue(entered.wait(2), "partial request was not admitted")
                    worker, accepted, listener = observed[0]
                    self.assertTrue(worker.is_alive(), "partial request already retired")
                    self.assertGreaterEqual(accepted.fileno(), 0)

                # Freeze every observation before closing the client or rescuing the worker.
                state_at_exit = {
                    "worker_alive": worker.is_alive(),
                    "accepted_fd": accepted.fileno(),
                    "listener_fd": listener.fileno(),
                }
                self.assertEqual(
                    state_at_exit,
                    {"worker_alive": False, "accepted_fd": -1, "listener_fd": -1},
                    "partial-input resources survived _local_origin context exit",
                )
            finally:
                try:
                    for worker, accepted, listener in observed:
                        try:
                            try:
                                accepted.shutdown(socket.SHUT_RDWR)
                            except OSError as error:
                                if error.errno not in (
                                    errno.EBADF, errno.ENOTCONN, errno.EINVAL,
                                ):
                                    raise
                            finally:
                                accepted.close()
                        finally:
                            try:
                                listener.close()
                            finally:
                                worker.join(timeout=5)
                                self.assertFalse(
                                    worker.is_alive(),
                                    "emergency cleanup left a request worker alive",
                                )
                finally:
                    if client is not None:
                        client.close()


class ConsumerOriginLifetimeTests(unittest.TestCase):
    def test_font_archive_partial_input_retires_on_body_failure(self):
        expected = RuntimeError("font-owner-body-failure-canary")
        with _observe_requests() as (observed, entered), ExitStack() as clients:
            with self.assertRaises(RuntimeError) as raised:
                with serve_font_archive(b"archive-bytes") as url:
                    address = urlsplit(url)
                    client = socket.create_connection(
                        (address.hostname, address.port), timeout=2,
                    )
                    clients.callback(client.close)
                    client.sendall(b"GET /fonts.zip HTTP/1.1\r\nX-Pending: incomplete")
                    self.assertTrue(entered.wait(2))
                    raise expected
            self.assertIs(raised.exception, expected)
            self.assertEqual(len(observed), 1)
            _assert_retired(self, observed)

    def test_fake_webui_stop_cancels_captured_marker_without_deleting_it(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "caller-owned-marker"
            marker.write_text("keep", encoding="utf-8")
            webui = init_wrapper.FakeWebUI()
            webui.mode = "ok"
            webui.hold_path = str(marker)
            webui.hold_request = "GET /api/version"
            with _observe_requests() as (observed, entered), ExitStack() as clients:
                clients.callback(marker.unlink, missing_ok=True)
                webui.start()
                try:
                    address = urlsplit(webui.url)
                    client = socket.create_connection(
                        (address.hostname, address.port), timeout=2,
                    )
                    clients.callback(client.close)
                    client.sendall(
                        b"GET /api/version HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
                    )
                    self.assertTrue(entered.wait(2))
                    self.assertTrue(webui.entered.wait(2), "marker hold was not entered")
                    self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
                finally:
                    webui.stop()
                self.assertEqual(len(observed), 1)
                _assert_retired(self, observed)
                self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_second_reservation_bind_failure_closes_acquired_sockets_and_temp_root(self):
        class StartupCase(deployment_smoke.OverlaySmokeCliTests):
            def runTest(self):
                self.start_http()

        acquired = []
        original_bind = socket.socket.bind
        expected = "second-reservation-bind-canary"

        def bind(connection, address):
            acquired.append(connection)
            if len(acquired) == 2:
                raise OSError(errno.EADDRINUSE, expected)
            return original_bind(connection, address)

        case = StartupCase()
        try:
            with patch.object(socket.socket, "bind", bind):
                result = unittest.TestResult()
                case.run(result)
            self.assertEqual(len(result.errors), 1)
            self.assertEqual(result.failures, [])
            self.assertIn(expected, result.errors[0][1])
            self.assertEqual([connection.fileno() for connection in acquired], [-1, -1])
            self.assertFalse(case.state.exists(), "failed setup retained its temporary root")
        finally:
            for connection in acquired:
                connection.close()
            if hasattr(case, "context"):
                case.context.cleanup()

    def test_second_control_bind_failure_retires_first_admitted_partial_request(self):
        class StartupCase(deployment_smoke.OverlaySmokeCliTests):
            def runTest(self):
                self.start_http()

        original_bind = socket.socket.bind
        acquired = []
        case = StartupCase()
        with _observe_requests() as (observed, entered), ExitStack() as clients:
            def bind(connection, address):
                acquired.append(connection)
                if address == ("127.0.0.1", 8080):
                    client = socket.create_connection(("127.0.0.1", 8081), timeout=2)
                    clients.callback(client.close)
                    client.sendall(b"GET /partial HTTP/1.1\r\nX-Pending: incomplete")
                    self.assertTrue(entered.wait(2), "first control did not admit input")
                    raise OSError(errno.EADDRINUSE, "second-control-bind-canary")
                return original_bind(connection, address)

            try:
                with patch.object(socket.socket, "bind", bind):
                    result = unittest.TestResult()
                    case.run(result)
                self.assertEqual(result.errors, [])
                self.assertEqual(len(result.failures), 1)
                self.assertIn("second-control-bind-canary", result.failures[0][1])
                self.assertEqual(len(observed), 1)
                _assert_retired(self, observed)
                self.assertEqual([connection.fileno() for connection in acquired], [-1] * 4)
                self.assertFalse(case.state.exists())
            finally:
                for connection in acquired:
                    connection.close()
                if hasattr(case, "context"):
                    case.context.cleanup()

    def test_fixture_handler_failure_does_not_bypass_other_owners_or_body_failure(self):
        with _observe_requests() as (observed, entered), ExitStack() as clients:
            class FailingCase(deployment_smoke.OverlaySmokeCliTests):
                def runTest(case):
                    port, _origin = case.start_http()
                    for control_port in (8081, 8080, 8082):
                        entered.clear()
                        client = socket.create_connection(
                            ("127.0.0.1", control_port), timeout=2,
                        )
                        clients.callback(client.close)
                        client.sendall(b"GET /partial HTTP/1.1\r\nX-Pending: incomplete")
                        self.assertTrue(entered.wait(2))
                    case.http.terminal_path.write_text("not-json", encoding="utf-8")
                    client = socket.create_connection(("127.0.0.1", port), timeout=2)
                    clients.callback(client.close)
                    client.sendall(
                        b"POST /ocu/terminal/chat/start-ttyd HTTP/1.1\r\n"
                        b"Host: 127.0.0.1\r\nContent-Length: 0\r\n\r\n"
                    )
                    self.assertEqual(client.recv(1), b"", "failed handler kept its connection")
                    raise AssertionError("multiowner-body-failure-canary")

            case = FailingCase()
            try:
                result = unittest.TestResult()
                case.run(result)
                self.assertEqual(len(result.failures), 1)
                self.assertIn("multiowner-body-failure-canary", result.failures[0][1])
                self.assertEqual(len(result.errors), 1)
                self.assertIn("JSONDecodeError", result.errors[0][1])
                self.assertEqual(len(observed), 4)
                _assert_retired(self, observed)
                self.assertFalse(case.state.exists())
            finally:
                case.doCleanups()

    def test_native_tty_timeout_configuration_after_socket_close_is_expected_teardown(self):
        original_settimeout = socket.socket.settimeout
        original_close = socket.socket.close
        closed = threading.Event()
        timeout_errors = []
        handlers = []
        with _observe_requests(handlers=handlers) as (observed, entered), ExitStack() as clients:
            def close(connection):
                result = original_close(connection)
                if observed and connection is observed[0][1]:
                    closed.set()
                return result

            def settimeout(connection, seconds):
                if observed and connection is observed[0][1]:
                    if not closed.wait(2):
                        raise AssertionError("teardown did not close the accepted socket")
                    # makefile() keeps the descriptor alive until its real reader closes.
                    handlers[0].rfile.close()
                    handlers[0].wfile.close()
                    try:
                        return original_settimeout(connection, seconds)
                    except OSError as error:
                        timeout_errors.append(error.errno)
                        raise
                return original_settimeout(connection, seconds)

            with patch.object(socket.socket, "close", close), patch.object(
                socket.socket, "settimeout", settimeout,
            ):
                with HttpOriginLifetime(smoke_native.TtyHandler) as lifetime:
                    server = lifetime.server
                    server.select_tty = True
                    server.upgraded = threading.Event()
                    server.init_release = threading.Event()
                    lifetime.release_on_close(server.init_release)
                    server.init_seen = threading.Event()
                    server.drop_after_init = False
                    server.closed = threading.Event()
                    server.hold = threading.Event()
                    lifetime.release_on_close(server.hold)
                    server.init = b""
                    address = urlsplit(lifetime.url)
                    client = socket.create_connection(
                        (address.hostname, address.port), timeout=2,
                    )
                    clients.callback(client.close)
                    client.sendall(
                        b"GET /ocu/terminal/chat/ws HTTP/1.1\r\n"
                        b"Host: 127.0.0.1\r\nUpgrade: websocket\r\n"
                        b"Connection: Upgrade\r\n"
                        b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n"  # gitleaks:allow public RFC 6455 example nonce
                    )
                    self.assertTrue(entered.wait(2))
                    self.assertTrue(server.upgraded.wait(2))
                self.assertEqual(timeout_errors, [errno.EBADF])
                self.assertEqual(len(observed), 1)
                _assert_retired(self, observed)

    def test_native_tty_timeout_bad_descriptor_while_running_reaches_owner(self):
        original_settimeout = socket.socket.settimeout
        handlers = []
        errors = []
        with _observe_requests(handlers=handlers) as (observed, entered), ExitStack() as clients:
            def settimeout(connection, seconds):
                if observed and connection is observed[0][1]:
                    handlers[0].rfile.close()
                    handlers[0].wfile.close()
                    connection.close()
                    try:
                        return original_settimeout(connection, seconds)
                    except OSError as error:
                        errors.append(error)
                        raise
                return original_settimeout(connection, seconds)

            with patch.object(socket.socket, "settimeout", settimeout):
                with self.assertRaises(OSError) as raised:
                    with HttpOriginLifetime(smoke_native.TtyHandler) as lifetime:
                        server = lifetime.server
                        server.select_tty = True
                        server.upgraded = threading.Event()
                        server.init_release = threading.Event()
                        server.init_release.set()
                        server.init_seen = threading.Event()
                        server.drop_after_init = False
                        server.closed = threading.Event()
                        server.hold = threading.Event()
                        lifetime.release_on_close(server.hold)
                        server.init = b""
                        address = urlsplit(lifetime.url)
                        client = socket.create_connection(
                            (address.hostname, address.port), timeout=2,
                        )
                        clients.callback(client.close)
                        client.sendall(
                            b"GET /ocu/terminal/chat/ws HTTP/1.1\r\n"
                            b"Host: 127.0.0.1\r\nUpgrade: websocket\r\n"
                            b"Connection: Upgrade\r\n"
                            b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n"  # gitleaks:allow public RFC 6455 example nonce
                        )
                        self.assertTrue(entered.wait(2))
                        while client.recv(4096):
                            pass
                self.assertEqual(raised.exception.errno, errno.EBADF)
                self.assertEqual(len(errors), 1)
                self.assertIs(raised.exception, errors[0])
                self.assertEqual(len(observed), 1)
                _assert_retired(self, observed)


if __name__ == "__main__":
    unittest.main()
