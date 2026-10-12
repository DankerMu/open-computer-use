# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Test-local ownership of Office HTTP sockets, workers, waits and failures."""
from __future__ import annotations

import errno
import selectors
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _raise_errors(errors, body_error=None):
    if not errors:
        return
    failures = ([body_error] if body_error is not None else []) + errors
    if len(failures) == 1:
        raise failures[0]
    raise BaseExceptionGroup("Office HTTP origin failures", failures)


class _ExpectedDisconnect(Exception):
    """A peer or teardown closed actual request/response transport I/O."""


class _TransportStream:
    def __init__(self, stream, owner):
        self._stream = stream
        self._owner = owner

    def __getattr__(self, name):
        return getattr(self._stream, name)

    def _call(self, method, *args, **kwargs):
        try:
            result = getattr(self._stream, method)(*args, **kwargs)
        except OSError as error:
            if isinstance(error, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)) or (
                self._owner.stopping and error.errno == errno.EBADF
            ):
                raise _ExpectedDisconnect() from error
            raise
        if method in ("read", "readline") and self._owner.stopping:
            # shutdown() can unblock a partial request with EOF instead of an OSError.
            raise _ExpectedDisconnect()
        return result

    def read(self, *args, **kwargs):
        return self._call("read", *args, **kwargs)

    def readline(self, *args, **kwargs):
        return self._call("readline", *args, **kwargs)

    def write(self, *args, **kwargs):
        return self._call("write", *args, **kwargs)

    def flush(self):
        return self._call("flush")

    def close(self):
        return self._call("close")


class OriginRequestHandler(BaseHTTPRequestHandler):
    def setup(self):
        # Setup and teardown must not race makefile() on an already closed socket.
        with self.server.owner._lock:
            if self.server.owner._stopping:
                raise _ExpectedDisconnect()
            super().setup()
            self.rfile = _TransportStream(self.rfile, self.server.owner)
            self.wfile = _TransportStream(self.wfile, self.server.owner)

    def finish(self):
        body_error = sys.exception()
        errors = []
        operations = [] if self.wfile.closed else [self.wfile.flush]
        operations.extend((self.wfile.close, self.rfile.close))
        for operation in operations:
            try:
                operation()
            except _ExpectedDisconnect:
                # Only tagged stream I/O can be an expected peer/teardown closure.
                pass
            except BaseException as error:
                errors.append(error)
        if isinstance(body_error, _ExpectedDisconnect):
            body_error = None
        _raise_errors(errors, body_error)


class _OwnedHTTPServer(ThreadingHTTPServer):
    # The owner joins every worker against one deadline, not server_close's unbounded join.
    block_on_close = False

    def __init__(self, owner, handler):
        self.owner = owner
        super().__init__(("127.0.0.1", 0), handler, bind_and_activate=False)

    def get_request(self):
        with self.owner._lock:
            if self.owner._stopping:
                raise BlockingIOError(errno.EAGAIN, "Office HTTP origin is stopping")
            request, address = super().get_request()
            self.owner._sockets.add(request)
            try:
                # Darwin can inherit listener O_NONBLOCK even when gettimeout() is None.
                request.setblocking(True)
            except BaseException as error:
                self.owner._record_error(error)
                self.shutdown_request(request)
                raise
            return request, address

    def process_request(self, request, address):
        with self.owner._lock:
            if self.owner._stopping:
                self.shutdown_request(request)
                return
            # A deadline failure must reach pytest rather than hang interpreter shutdown.
            worker = threading.Thread(
                target=self.owner._handle, args=(request, address), daemon=True,
                name="office-http-handler",
            )
            self.owner._workers.append(worker)
            try:
                worker.start()
            except BaseException as error:
                self.owner._record_error(error)
                self.shutdown_request(request)

    def shutdown_request(self, request):
        self.owner._close_socket(request)
        with self.owner._lock:
            self.owner._sockets.discard(request)

    def handle_error(self, request, address):
        error = sys.exception()
        if error is not None:
            self.owner._record_error(error)


class HttpOriginLifetime:
    # Released holds and closed I/O should drain promptly; stuck responders must fail finitely.
    TEARDOWN_TIMEOUT_SECONDS = 2.0

    def __init__(self, handler):
        self._lock = threading.RLock()
        self._stopping = False
        self._cancel = threading.Event()
        self._releases = []
        self._sockets = set()
        self._workers = []
        self._errors = []
        self.server = None
        self._thread = None
        try:
            self.server = _OwnedHTTPServer(self, handler)
            self.server.server_bind()
            self.server.server_activate()
            self.server.socket.setblocking(False)
            self._thread = threading.Thread(
                target=self._serve, daemon=True, name="office-http-origin",
            )
            self._thread.start()
        except BaseException as error:
            self.stop(body_error=error)
            raise

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    @property
    def stopping(self):
        return self._cancel.is_set()

    def release_on_close(self, event):
        if event is not None:
            with self._lock:
                if self._stopping:
                    event.set()
                else:
                    self._releases.append(event)

    def pause(self, seconds):
        self._cancel.wait(seconds)

    def _record_error(self, error):
        with self._lock:
            self._errors.append(error)

    def _serve(self):
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self.server.socket, selectors.EVENT_READ)
                while not self._cancel.is_set():
                    # A positive wait avoids handle_request's zero-timeout spin and bounds stop latency.
                    ready = selector.select(timeout=0.05)
                    if ready and not self._cancel.is_set():
                        self.server._handle_request_noblock()
        except BaseException as error:
            self._record_error(error)

    def _handle(self, request, address):
        try:
            self.server.finish_request(request, address)
        except _ExpectedDisconnect:
            # This tag is produced only by transport I/O or teardown-before-setup.
            pass
        except BaseException as error:
            self._record_error(error)
        finally:
            try:
                self.server.shutdown_request(request)
            except BaseException as error:
                self._record_error(error)

    def _close_socket(self, request):
        with self._lock:
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError as error:
                # Completed peers and repeated teardown may already have retired this socket.
                if error.errno not in (errno.EBADF, errno.ENOTCONN, errno.EINVAL):
                    self._record_error(error)
            finally:
                try:
                    request.close()
                except OSError as error:
                    self._record_error(error)

    def stop(self, *, body_error=None):
        deadline = time.monotonic() + self.TEARDOWN_TIMEOUT_SECONDS
        with self._lock:
            self._stopping = True
            self._cancel.set()
            releases = tuple(self._releases)
            sockets = tuple(self._sockets)
            workers = tuple(self._workers)
        try:
            for event in releases:
                try:
                    event.set()
                except BaseException as error:
                    self._record_error(error)
            for request in sockets:
                self._close_socket(request)
            threads = ([self._thread] if self._thread is not None else []) + list(workers)
            for worker in threads:
                if worker.ident is not None:
                    try:
                        worker.join(timeout=max(0.0, deadline - time.monotonic()))
                    except BaseException as error:
                        self._record_error(error)
            if any(worker.is_alive() for worker in threads):
                self._record_error(TimeoutError("Office HTTP origin workers exceeded teardown deadline"))
        finally:
            if self.server is not None:
                try:
                    self.server.server_close()
                except BaseException as error:
                    self._record_error(error)
        with self._lock:
            errors = list(self._errors)
        _raise_errors(errors, body_error)

    def __enter__(self):
        return self

    def __exit__(self, _type, body_error, _traceback):
        self.stop(body_error=body_error)
        return False
