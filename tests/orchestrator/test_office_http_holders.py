# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Exceptional cleanup through real held-save wrappers and manual consumers."""
from __future__ import annotations

import os
import socket
import sys
import threading

import pytest

from tests.orchestrator import test_office_save_close as save_close
from tests.orchestrator._http_origin import HttpOriginLifetime
from tests.orchestrator.test_http_origin import (
    _assert_retired,
    _contains_exception,
    _emergency_cleanup,
)
from tests.orchestrator.test_office_session_lifecycle import _created
from tests.orchestrator.test_office_sessions import office_world as office_world


class _HolderResources:
    """Observe real thread dependencies without replacing the held wrapper."""

    def __init__(self, monkeypatch, *, saver_start_error=None):
        self.origins = []
        self.observed = []
        self.savers = []
        self.boxes = []
        self.releases = []
        original_start = threading.Thread.start

        def observe_start(thread):
            caller = sys._getframe(1)
            if caller.f_code is HttpOriginLifetime.__init__.__code__:
                self.origins.append((caller.f_locals["self"], thread))
            elif caller.f_code is save_close._hold_save.__code__:
                self.savers.append(thread)
                # Retain the real acquired box so GC cannot repair an omitted-cleanup mutant.
                self.boxes.append(caller.f_locals["box"])
                self.releases.append(caller.f_locals["release"])
                if saver_start_error is not None:
                    raise saver_start_error
            else:
                for origin, acceptor in self.origins:
                    if threading.current_thread() is acceptor:
                        self.observed.append((
                            thread, caller.f_locals["request"], origin.server.socket, acceptor,
                        ))
                        break
            return original_start(thread)

        monkeypatch.setattr(threading.Thread, "start", observe_start)

    def assert_retired(self, previous_url):
        assert os.environ["OCU_OFFICE_DOCSERVER_URL"] == previous_url
        origin, acceptor = self.origins[0]
        assert origin.server.socket.fileno() == -1, "held wrapper leaked its listener"
        assert not acceptor.is_alive(), "held wrapper leaked its admission loop"
        for resources in self.observed:
            _assert_retired([resources])
        for saver in self.savers:
            assert not saver.is_alive(), "held wrapper left its saver client running"

    def cleanup(self, *expected_errors):
        cleanup_errors = []
        for release in self.releases:
            release.set()
        try:
            for box in self.boxes:
                try:
                    box.__exit__(None, None, None)
                except BaseException as error:
                    # Emergency closure of a mutant can re-report the deliberate handler fault.
                    if not any(error is expected for expected in expected_errors):
                        cleanup_errors.append(error)
            for origin, acceptor in self.origins:
                if acceptor.is_alive() or origin.server.socket.fileno() != -1:
                    try:
                        origin.stop()
                    except BaseException as error:
                        if not any(error is expected for expected in expected_errors):
                            cleanup_errors.append(error)
        finally:
            try:
                _emergency_cleanup(self.observed)
            finally:
                for saver in self.savers:
                    if saver.ident is not None:
                        saver.join(timeout=10)
                    assert not saver.is_alive(), "holder emergency cleanup left a saver running"
        if cleanup_errors:
            raise BaseExceptionGroup("held-wrapper emergency cleanup failures", cleanup_errors)


def test_hold_save_client_start_failure_retires_the_already_acquired_origin(office_world, monkeypatch):
    http, _data, _recording, _docker, _broker = office_world
    _file_id, first = _created(office_world, "editing")
    previous_url = os.environ["OCU_OFFICE_DOCSERVER_URL"]
    expected = RuntimeError("held-save-client-start-failure")
    resources = _HolderResources(monkeypatch, saver_start_error=expected)
    try:
        with pytest.raises(BaseException) as failure:
            save_close._hold_save(http, monkeypatch, first)
        assert _contains_exception(failure.value, expected)
        resources.assert_retired(previous_url)
    finally:
        resources.cleanup(expected)


def test_hold_save_readiness_failure_reports_prior_handler_io_failure_and_retires_clients(
    office_world, monkeypatch,
):
    http, _data, _recording, _docker, _broker = office_world
    _file_id, first = _created(office_world, "editing")
    previous_url = os.environ["OCU_OFFICE_DOCSERVER_URL"]
    handler_error = LookupError("held-save-handler-input-failure-before-readiness")
    readiness_error = TimeoutError("held-save-readiness-dependency-failure")
    failed_input = threading.Event()
    resources = _HolderResources(monkeypatch)
    original_readinto = socket.SocketIO.readinto
    original_wait = threading.Event.wait

    def fail_handler_input(stream, buffer):
        if any(stream._sock is observed[1] for observed in resources.observed):
            failed_input.set()
            raise handler_error from None
        return original_readinto(stream, buffer)

    def fail_holder_readiness(event, timeout=None):
        if sys._getframe(1).f_code is save_close._hold_save.__code__:
            assert failed_input.wait(2), "holder readiness failed before real handler admission"
            assert not event.is_set(), "handler-I/O fault did not precede holder readiness"
            raise readiness_error from None
        return original_wait(event, timeout)

    monkeypatch.setattr(socket.SocketIO, "readinto", fail_handler_input)
    monkeypatch.setattr(threading.Event, "wait", fail_holder_readiness)
    try:
        with pytest.raises(BaseException) as failure:
            save_close._hold_save(http, monkeypatch, first)
        assert _contains_exception(failure.value, readiness_error)
        assert _contains_exception(failure.value, handler_error)
        assert resources.savers[0].ident is not None, "readiness failed before the saver started"
        assert resources.observed[0][0].ident is not None
        resources.assert_retired(previous_url)
    finally:
        resources.cleanup(readiness_error, handler_error)


def test_manual_save_close_consumer_visibly_preserves_body_and_handler_failures(
    office_world, monkeypatch,
):
    http, _data, _recording, _docker, _broker = office_world
    previous_url = os.environ["OCU_OFFICE_DOCSERVER_URL"]
    body_error = RuntimeError("manual-save-close-client-transport-failure")
    handler_error = AssertionError("manual-save-close-handler-output-failure")
    resources = _HolderResources(monkeypatch)
    transport = http._transport
    original_handle_request = type(transport).handle_request
    original_sendall = socket.socket.sendall

    def fail_close_request(client_transport, request):
        if client_transport is transport and request.method == "POST" and request.url.path.endswith("/close"):
            raise body_error from None
        return original_handle_request(client_transport, request)

    def fail_held_response(request, *args, **kwargs):
        if any(request is observed[1] for observed in resources.observed):
            # Suppressed context makes reverting the actual consumer's body forwarding observable.
            raise handler_error from None
        return original_sendall(request, *args, **kwargs)

    monkeypatch.setattr(type(transport), "handle_request", fail_close_request)
    monkeypatch.setattr(socket.socket, "sendall", fail_held_response)
    try:
        with pytest.raises(BaseException) as failure:
            save_close.test_delayed_accepted_save_keeps_concurrent_close(office_world, monkeypatch)
        assert _contains_exception(failure.value, body_error)
        assert _contains_exception(failure.value, handler_error)
        assert resources.savers[0].ident is not None
        resources.assert_retired(previous_url)
    finally:
        resources.cleanup(body_error, handler_error)
