# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Real HTTP regressions for Office test-origin lifetime ownership."""
from __future__ import annotations

import asyncio
import errno
import socket
import struct
import threading
from http.client import HTTPConnection, HTTPResponse, RemoteDisconnected
from urllib.parse import urlsplit

import pytest

from tests.orchestrator._http_origin import HttpOriginLifetime, OriginRequestHandler
from tests.orchestrator.test_office_commands import _command_origin, commands


def _contains_exception(error: BaseException, expected: BaseException) -> bool:
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if current is expected:
            return True
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        elif not current.__suppress_context__ and current.__context__ is not None:
            pending.append(current.__context__)
    return False


def _observe_request(handler, observed):
    observed.append((
        threading.current_thread(), handler.connection, handler.server.socket,
        handler.server.owner._thread,
    ))


def _assert_retired(observed):
    worker, accepted, listener, acceptor = observed[0]
    assert not worker.is_alive(), "request worker survived origin exit"
    assert accepted.fileno() == -1, "accepted socket survived origin exit"
    assert listener.fileno() == -1, "listening socket survived origin exit"
    assert not acceptor.is_alive(), "admission loop survived origin exit"


def _emergency_cleanup(observed):
    for worker, accepted, listener, acceptor in observed:
        try:
            accepted.shutdown(socket.SHUT_RDWR)
        except OSError as error:
            # Normal teardown has already closed or disconnected these real sockets.
            if error.errno not in (errno.EBADF, errno.ENOTCONN, errno.EINVAL):
                raise
        finally:
            accepted.close()
        listener.close()
        for thread in (worker, acceptor):
            if thread.ident is not None:
                thread.join(timeout=5)
            assert not thread.is_alive(), "HTTP regression emergency cleanup left a live worker"


def test_command_origin_reports_responder_failure_to_owning_test():
    expected = AssertionError("office-origin-responder-attribution-canary")
    responder_thread = None

    def responder(_handler, _body, _origin):
        nonlocal responder_thread
        responder_thread = threading.current_thread()
        raise expected

    try:
        with pytest.raises(BaseException) as failure:
            with _command_origin(responder) as origin:
                address = urlsplit(origin.url)
                connection = HTTPConnection(address.hostname, address.port, timeout=2)
                try:
                    connection.request("POST", "/command", body=b"{}")
                    try:
                        with connection.getresponse() as response:
                            response.read()
                    except RemoteDisconnected:
                        # A client disconnect alone does not attribute the handler error.
                        pass
                finally:
                    connection.close()
        assert _contains_exception(failure.value, expected), (
            "The origin context did not report the original responder assertion"
        )
    finally:
        if responder_thread is not None:
            responder_thread.join(timeout=2)
            assert not responder_thread.is_alive(), "The failed responder did not retire"


def test_command_origin_waits_for_complete_fragmented_request():
    called = threading.Event()
    observed = []
    request_body = b'{"operation":"fragmented"}'
    response_body = b"fragmented-request-complete"

    def responder(_handler, body, _origin):
        observed.append(body)
        called.set()
        return 200, {"Content-Length": str(len(response_body))}, response_body

    with _command_origin(responder) as origin:
        address = urlsplit(origin.url)
        connection = HTTPConnection(address.hostname, address.port, timeout=2)
        try:
            connection.connect()
            connection.sock.sendall(
                b"POST /command HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                + f"Content-Length: {len(request_body)}\r\n".encode("ascii")
                + b"X-Fragment: pending"
            )
            # Leave framing incomplete long enough for a premature responder to signal.
            assert not called.wait(0.2), "responder ran before headers were complete"
            connection.sock.sendall(b"\r\n\r\n" + request_body[:5])
            assert not called.wait(0.2), "responder ran before the declared body was complete"
            connection.sock.sendall(request_body[5:])
            assert called.wait(2), "complete fragmented request never reached the responder"
            with HTTPResponse(connection.sock) as response:
                response.begin()
                assert response.status == 200
                assert response.read() == response_body
            assert observed == [request_body]
        finally:
            connection.close()


def test_successful_command_retires_request_and_admission_resources():
    observed = []

    def responder(handler, _body, _origin):
        _observe_request(handler, observed)
        return 200, {"Content-Length": "2"}, b"ok"

    connection = None
    try:
        with _command_origin(responder) as origin:
            address = urlsplit(origin.url)
            connection = HTTPConnection(address.hostname, address.port, timeout=2)
            connection.request("POST", "/command", body=b"{}")
            with connection.getresponse() as response:
                assert response.status == 200
                assert response.read() == b"ok"
        _assert_retired(observed)
    finally:
        if connection is not None:
            connection.close()
        _emergency_cleanup(observed)


def test_command_timeout_releases_held_responder_and_retires_resources(monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    observed = []

    def responder(handler, _body, _origin):
        _observe_request(handler, observed)
        entered.set()
        try:
            assert release.wait(5), "timeout regression emergency release expired"
            return 200, {"Content-Length": "11"}, b'{"error":0}'
        finally:
            finished.set()

    monkeypatch.setenv("OCU_OFFICE_JWT_SECRET", "http-origin-test-signing-canary")
    monkeypatch.setattr(commands, "HTTP_TIMEOUT_SECONDS", 0.05)
    try:
        with _command_origin(responder, release=release) as origin:
            monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", origin.url)
            outcome = asyncio.run(commands.forcesave("timeout-document", 1, "publish"))
            assert outcome is commands.ForceSaveOutcome.UNREACHABLE
            assert entered.wait(2), "timeout occurred without handler admission"
            assert not finished.is_set(), "timeout did not leave the responder held"
            assert not release.is_set()
        assert release.is_set() and finished.is_set()
        _assert_retired(observed)
    finally:
        release.set()
        _emergency_cleanup(observed)


def test_admitted_command_cancellation_propagates_and_retires_resources(monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    observed = []

    def responder(handler, _body, _origin):
        _observe_request(handler, observed)
        entered.set()
        try:
            assert release.wait(5), "cancellation regression emergency release expired"
            return 200, {"Content-Length": "11"}, b'{"error":0}'
        finally:
            finished.set()

    async def cancel_after_admission():
        pending = asyncio.create_task(commands.forcesave("cancel-document", 1, "publish"))
        try:
            assert await asyncio.to_thread(entered.wait, 2), "command never reached its responder"
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 2)
            assert not finished.is_set()
        finally:
            if not pending.done():
                pending.cancel()
                try:
                    await asyncio.wait_for(pending, 2)
                except asyncio.CancelledError:
                    # Only this test's deliberately cancelled client task is discarded.
                    pass

    monkeypatch.setenv("OCU_OFFICE_JWT_SECRET", "http-origin-test-signing-canary")
    # Bound the real client as well as its task if cancellation is ever swallowed.
    monkeypatch.setattr(commands, "HTTP_TIMEOUT_SECONDS", 2)
    try:
        with _command_origin(responder, release=release) as origin:
            monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", origin.url)
            asyncio.run(cancel_after_admission())
            assert not release.is_set()
        assert release.is_set() and finished.is_set()
        _assert_retired(observed)
    finally:
        release.set()
        _emergency_cleanup(observed)


def test_body_failure_releases_held_responder_without_losing_primary_error():
    entered, release = threading.Event(), threading.Event()
    observed = []
    expected = ValueError("http-origin-body-failure")

    def responder(handler, _body, _origin):
        _observe_request(handler, observed)
        entered.set()
        assert release.wait(5), "body-failure regression emergency release expired"
        return 200, {"Content-Length": "2"}, b"ok"

    connection = None
    try:
        with pytest.raises(BaseException) as failure:
            with _command_origin(responder, release=release) as origin:
                address = urlsplit(origin.url)
                connection = HTTPConnection(address.hostname, address.port, timeout=2)
                connection.request("POST", "/command", body=b"{}")
                assert entered.wait(2), "body failed without admitting a held responder"
                assert not release.is_set()
                raise expected
        assert _contains_exception(failure.value, expected)
        assert release.is_set()
        _assert_retired(observed)
    finally:
        release.set()
        if connection is not None:
            connection.close()
        _emergency_cleanup(observed)


def test_body_and_teardown_responder_failures_are_both_visibly_reported():
    entered, release = threading.Event(), threading.Event()
    observed = []
    body_error = ValueError("http-origin-primary-body-failure")
    handler_error = AssertionError("http-origin-teardown-responder-failure")

    def responder(handler, _body, _origin):
        _observe_request(handler, observed)
        entered.set()
        assert release.wait(5), "combined-failure regression emergency release expired"
        raise handler_error

    connection = None
    try:
        with pytest.raises(BaseException) as failure:
            with _command_origin(responder, release=release) as origin:
                address = urlsplit(origin.url)
                connection = HTTPConnection(address.hostname, address.port, timeout=2)
                connection.request("POST", "/command", body=b"{}")
                assert entered.wait(2), "body failed without admitting its responder"
                raise body_error
        assert _contains_exception(failure.value, body_error)
        assert _contains_exception(failure.value, handler_error)
        _assert_retired(observed)
    finally:
        release.set()
        if connection is not None:
            connection.close()
        _emergency_cleanup(observed)


@pytest.mark.parametrize("error_type", (ConnectionError, BrokenPipeError))
def test_responder_connection_failure_is_not_an_expected_transport_disconnect(error_type):
    expected = error_type("http-origin-direct-responder-failure")
    observed = []
    connection = None

    def responder(handler, _body, _origin):
        _observe_request(handler, observed)
        raise expected

    try:
        with pytest.raises(BaseException) as failure:
            with _command_origin(responder) as origin:
                address = urlsplit(origin.url)
                connection = HTTPConnection(address.hostname, address.port, timeout=2)
                connection.request("POST", "/command", body=b"{}")
                with pytest.raises(RemoteDisconnected):
                    connection.getresponse()
        assert _contains_exception(failure.value, expected)
        _assert_retired(observed)
    finally:
        if connection is not None:
            connection.close()
        _emergency_cleanup(observed)


def test_actual_peer_reset_drains_transport_and_resources_without_origin_failure():
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    observed, disconnects = [], []
    peer = None

    def responder(handler, _body, _origin):
        _observe_request(handler, observed)
        entered.set()
        try:
            assert release.wait(5), "peer-reset regression emergency release expired"
            try:
                handler.rfile.read(1)
            except BaseException as error:
                disconnects.append(error)
                raise
            raise AssertionError("reset peer unexpectedly supplied more request input")
        finally:
            finished.set()

    try:
        with _command_origin(responder, release=release) as origin:
            address = urlsplit(origin.url)
            peer = socket.create_connection((address.hostname, address.port), timeout=2)
            peer.sendall(b"POST /command HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\n\r\n{}")
            assert entered.wait(2), "peer closed without responder admission"
            # Abort rather than FIN so the real transport read must observe ECONNRESET.
            peer.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            peer.close()
            release.set()
            assert finished.wait(2), "reset did not unblock responder transport input"
        transport_failure = disconnects[0]
        assert isinstance(transport_failure, ConnectionResetError) or isinstance(
            transport_failure.__cause__, ConnectionResetError,
        )
        _assert_retired(observed)
    finally:
        release.set()
        if peer is not None:
            peer.close()
        _emergency_cleanup(observed)


def test_keep_alive_input_wait_is_retired_before_origin_exit():
    observed = []

    class Handler(OriginRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            return

        def do_GET(self):
            _observe_request(self, observed)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

    connection = None
    try:
        with HttpOriginLifetime(Handler) as origin:
            address = urlsplit(origin.url)
            connection = HTTPConnection(address.hostname, address.port, timeout=2)
            connection.request("GET", "/")
            with connection.getresponse() as response:
                assert response.status == 200 and response.read() == b"ok"
            assert connection.sock is not None, "control client did not retain keep-alive"
            assert observed[0][0].is_alive(), "control handler was not waiting on keep-alive input"
        _assert_retired(observed)
    finally:
        if connection is not None:
            connection.close()
        _emergency_cleanup(observed)


def test_partial_request_input_is_unblocked_and_retired_by_origin_exit():
    reading, returned = threading.Event(), threading.Event()
    observed = []

    class Handler(OriginRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):
            _observe_request(self, observed)
            reading.set()
            self.rfile.read(100)
            returned.set()

    peer = None
    try:
        with HttpOriginLifetime(Handler) as origin:
            peer = socket.create_connection(origin.server.server_address, timeout=2)
            peer.sendall(b"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100\r\n\r\nx")
            assert reading.wait(2), "partial request never entered transport input"
            assert not returned.wait(0.1), "partial input did not hold the control handler"
        _assert_retired(observed)
    finally:
        if peer is not None:
            peer.close()
        _emergency_cleanup(observed)


def test_origin_exit_waits_for_handler_finish_before_reporting_success():
    finishing = [threading.Event(), threading.Event()]
    allow_finish = [threading.Event(), threading.Event()]
    completed = [threading.Event(), threading.Event()]
    closing, returned = threading.Event(), threading.Event()
    observed, failures, connections = [], [], []
    origin = closer = None

    class Handler(OriginRequestHandler):
        def log_message(self, *_args):
            return

        def do_GET(self):
            _observe_request(self, observed)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def finish(self):
            index = int(self.path[1:])
            finishing[index].set()
            try:
                assert allow_finish[index].wait(5), "finish-barrier emergency release expired"
                super().finish()
            finally:
                completed[index].set()

    def close_origin():
        closing.set()
        try:
            origin.stop()
        except BaseException as error:
            failures.append(error)
        finally:
            returned.set()

    try:
        origin = HttpOriginLifetime(Handler)
        address = urlsplit(origin.url)
        for index in range(2):
            connection = HTTPConnection(address.hostname, address.port, timeout=2)
            connections.append(connection)
            connection.request("GET", f"/{index}")
            with connection.getresponse() as response:
                assert response.status == 200 and response.read() == b"ok"
            assert finishing[index].wait(2), "control handler did not reach its finish barrier"

        closer = threading.Thread(target=close_origin, name="http-origin-regression-closer")
        closer.start()
        assert closing.wait(2), "origin closer did not start"
        # Observe premature return while both independently admitted handlers remain held.
        assert not returned.wait(0.2), "origin returned while both handlers were held"
        allow_finish[0].set()
        assert completed[0].wait(2), "first handler did not finish after its explicit release"
        observed[0][0].join(timeout=2)
        assert not observed[0][0].is_alive()
        assert not allow_finish[1].is_set() and not completed[1].is_set()
        assert not returned.wait(0.2), "origin returned with its second handler still held"
        allow_finish[1].set()
        assert returned.wait(2), "origin did not return after both handlers were released"
        if failures:
            raise failures[0]
        for resources in observed:
            _assert_retired([resources])
    finally:
        for release in allow_finish:
            release.set()
        for connection in connections:
            connection.close()
        try:
            if closer is not None and closer.ident is not None:
                closer.join(timeout=5)
                assert not closer.is_alive(), "origin closer survived emergency cleanup"
            elif origin is not None:
                origin.stop()
        finally:
            _emergency_cleanup(observed)


def test_admission_loop_start_failure_closes_listener_and_reports_original_error(monkeypatch):
    expected = RuntimeError("http-origin-admission-thread-start-failure")
    listeners, attempted = [], []
    original_bind = socket.socket.bind
    original_start = threading.Thread.start

    def record_bind(listener, address):
        result = original_bind(listener, address)
        if address == ("127.0.0.1", 0):
            listeners.append(listener)
        return result

    def fail_admission_start(thread):
        if thread.name == "office-http-origin":
            attempted.append(thread)
            raise expected
        return original_start(thread)

    monkeypatch.setattr(socket.socket, "bind", record_bind)
    monkeypatch.setattr(threading.Thread, "start", fail_admission_start)
    try:
        with pytest.raises(BaseException) as failure:
            with _command_origin(lambda *_args: (200, {}, b"unused")):
                pytest.fail("origin yielded despite admission-thread startup failure")
        assert _contains_exception(failure.value, expected)
        assert listeners[0].fileno() == -1, "partial startup leaked the bound listener"
        assert not attempted[0].is_alive()
    finally:
        for listener in listeners:
            listener.close()
        for thread in attempted:
            if thread.ident is not None:
                thread.join(timeout=5)
            assert not thread.is_alive(), "failed admission thread survived emergency cleanup"


def test_request_worker_start_failure_closes_accepted_socket_and_reports_original_error(monkeypatch):
    expected = RuntimeError("http-origin-request-thread-start-failure")
    failed = threading.Event()
    accepted, attempted = [], []
    listener = acceptor = peer = None
    original_accept = socket.socket.accept
    original_start = threading.Thread.start

    def record_accept(server_socket):
        request, address = original_accept(server_socket)
        if server_socket is listener:
            accepted.append(request)
        return request, address

    def fail_worker_start(thread):
        if thread.name == "office-http-handler":
            attempted.append(thread)
            failed.set()
            raise expected
        return original_start(thread)

    monkeypatch.setattr(socket.socket, "accept", record_accept)
    monkeypatch.setattr(threading.Thread, "start", fail_worker_start)
    try:
        with pytest.raises(BaseException) as failure:
            with _command_origin(lambda *_args: (200, {}, b"unused")) as origin:
                listener = origin._lifetime.server.socket
                acceptor = origin._lifetime._thread
                address = urlsplit(origin.url)
                peer = socket.create_connection((address.hostname, address.port), timeout=2)
                assert failed.wait(2), "accepted request never attempted worker startup"
        assert _contains_exception(failure.value, expected)
        assert accepted[0].fileno() == -1, "worker startup failure leaked the accepted socket"
        assert listener.fileno() == -1
        assert not attempted[0].is_alive() and not acceptor.is_alive()
    finally:
        if peer is not None:
            peer.close()
        for request in accepted:
            request.close()
        if listener is not None:
            listener.close()
        for thread in attempted + ([acceptor] if acceptor is not None else []):
            if thread.ident is not None:
                thread.join(timeout=5)
            assert not thread.is_alive(), "startup fault left a live HTTP worker"


def test_accepted_socket_setup_failure_closes_socket_and_reports_original_oserror(monkeypatch):
    expected = OSError(errno.EIO, "http-origin-accepted-socket-setup-failure")
    failed = threading.Event()
    accepted = []
    listener = acceptor = peer = None
    original_accept = socket.socket.accept
    original_setblocking = socket.socket.setblocking

    def record_accept(server_socket):
        request, address = original_accept(server_socket)
        if server_socket is listener:
            accepted.append(request)
        return request, address

    def fail_accepted_setup(request, blocking):
        if blocking and any(request is candidate for candidate in accepted):
            failed.set()
            raise expected
        return original_setblocking(request, blocking)

    monkeypatch.setattr(socket.socket, "accept", record_accept)
    monkeypatch.setattr(socket.socket, "setblocking", fail_accepted_setup)
    try:
        with pytest.raises(BaseException) as failure:
            with _command_origin(lambda *_args: (200, {}, b"unused")) as origin:
                listener = origin._lifetime.server.socket
                acceptor = origin._lifetime._thread
                address = urlsplit(origin.url)
                peer = socket.create_connection((address.hostname, address.port), timeout=2)
                assert failed.wait(2), "accepted request never reached socket setup"
        assert _contains_exception(failure.value, expected)
        assert accepted[0].fileno() == -1, "socket setup failure leaked its accepted descriptor"
        assert listener.fileno() == -1 and not acceptor.is_alive()
    finally:
        if peer is not None:
            peer.close()
        for request in accepted:
            request.close()
        if listener is not None:
            listener.close()
        if acceptor is not None:
            acceptor.join(timeout=5)
            assert not acceptor.is_alive(), "socket setup fault left a live admission loop"
