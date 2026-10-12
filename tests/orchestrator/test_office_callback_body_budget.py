# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Raw ASGI admission limits for compatible DocumentServer body tokens."""
from __future__ import annotations

import json

import pytest

from tests.orchestrator.test_office_control_plane import _body_jwt, _callback_path, _open_session
from tests.orchestrator.test_office_router import SANDBOX_PEER, _raw_office
from tests.orchestrator.test_office_sessions import _snapshot, office_world as office_world
from tests.orchestrator.test_outputs_endpoint import CHAT


BODY_LIMIT = 1_048_576


def _sized_body(session, size):
    envelope = json.dumps(
        {"token": _body_jwt(session, status=1), "padding": "é" * 1024},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    body = envelope[:-2] + b"x" * (size - len(envelope)) + envelope[-2:]
    assert len(body) == size
    assert len(body.decode("utf-8")) == size - 1024
    return body


class _BodyJSONObserver:
    def __init__(self, original, *, forbid):
        self.original = original
        self.forbid = forbid
        self.calls = 0

    def __getattr__(self, name):
        return getattr(self.original, name)

    def loads(self, *args, **kwargs):
        self.calls += 1
        if self.forbid:
            raise AssertionError("rejected callback body must not be parsed")
        return self.original.loads(*args, **kwargs)


def _watch_body_json(monkeypatch, *, forbid):
    import office.control_plane as control_plane

    observer = _BodyJSONObserver(control_plane.json, forbid=forbid)
    monkeypatch.setattr(control_plane, "json", observer)
    return observer


def _assert_unchanged(data, before_state, before_files, origin):
    from office.store import OfficeStore

    assert OfficeStore().read(CHAT) == before_state
    assert _snapshot(data) == before_files
    assert origin.hits == 0


def test_body_token_limit_is_inclusive_for_multibyte_json(office_world, monkeypatch):
    http, data, origin, _docker, _broker, _content, session = _open_session(office_world)
    from office.store import OfficeStore

    before = OfficeStore().read(CHAT)
    assert before["sessions"][session["session_id"]]["state"] == "opening"
    body = _sized_body(session, BODY_LIMIT)
    _watch_body_json(monkeypatch, forbid=False)
    status, response = _raw_office(
        http.app,
        _callback_path(session),
        [(b"content-type", b"application/json"), (b"content-length", b"1048576")],
        method="POST",
        body=body,
    )
    assert status == 200
    assert response == {"error": 0}
    after = OfficeStore().read(CHAT)
    assert after["sessions"][session["session_id"]]["state"] == "editing"
    assert after["documents"] == before["documents"]
    assert after["receipts"] == before["receipts"]
    assert origin.hits == 0


def test_body_token_over_limit_is_rejected_without_parsing(office_world, monkeypatch):
    http, data, origin, _docker, _broker, _content, session = _open_session(office_world)
    from office.store import OfficeStore

    before_state = OfficeStore().read(CHAT)
    before_files = _snapshot(data)
    body = _sized_body(session, BODY_LIMIT + 1)
    observer = _watch_body_json(monkeypatch, forbid=True)
    receive_calls = 0

    async def receive():
        nonlocal receive_calls
        receive_calls += 1
        if receive_calls > 1:
            raise AssertionError("callback received after crossing the body budget")
        return {"type": "http.request", "body": body, "more_body": False}

    status, response = _raw_office(
        http.app,
        _callback_path(session),
        [(b"content-type", b"application/json"), (b"content-length", b"1048577")],
        method="POST",
        body=body,
        receive=receive,
    )
    assert status == 413
    assert response == {"error": 1, "reason": "callback_body_too_large"}
    assert observer.calls == 0
    assert receive_calls <= 1
    _assert_unchanged(data, before_state, before_files, origin)


@pytest.mark.parametrize("content_length", ("missing", "forged_small"))
def test_streamed_body_token_stops_at_overflow(office_world, monkeypatch, content_length):
    http, data, origin, _docker, _broker, _content, session = _open_session(office_world)
    from office.store import OfficeStore

    before_state = OfficeStore().read(CHAT)
    before_files = _snapshot(data)
    body = _sized_body(session, BODY_LIMIT + 1)
    chunks = [body[offset:offset + 65_536] for offset in range(0, len(body), 65_536)]
    assert sum(map(len, chunks[:-1])) == BODY_LIMIT
    assert len(chunks[-1]) == 1
    observer = _watch_body_json(monkeypatch, forbid=True)
    receive_calls = 0

    async def receive():
        nonlocal receive_calls
        receive_calls += 1
        if receive_calls > len(chunks):
            raise AssertionError("callback received after crossing the body budget")
        return {"type": "http.request", "body": chunks[receive_calls - 1], "more_body": True}

    headers = [(b"content-type", b"application/json")]
    if content_length == "forged_small":
        headers.append((b"content-length", b"1"))
    status, response = _raw_office(
        http.app,
        _callback_path(session),
        headers,
        method="POST",
        receive=receive,
    )
    assert status == 413
    assert response == {"error": 1, "reason": "callback_body_too_large"}
    assert observer.calls == 0
    assert receive_calls == len(chunks)
    _assert_unchanged(data, before_state, before_files, origin)


def test_sandbox_callback_is_rejected_before_body_reception_or_parsing(office_world, monkeypatch):
    http, data, origin, _docker, _broker, _content, session = _open_session(office_world)
    from office.store import OfficeStore

    before_state = OfficeStore().read(CHAT)
    before_files = _snapshot(data)
    body = _sized_body(session, BODY_LIMIT + 1)
    observer = _watch_body_json(monkeypatch, forbid=True)
    receive_calls = 0

    async def receive():
        nonlocal receive_calls
        receive_calls += 1
        raise AssertionError("sandbox callback must not receive its body")

    status, response = _raw_office(
        http.app,
        _callback_path(session),
        [
            (b"content-type", b"application/json"),
            (b"content-length", b"1048577"),
            (b"x-forwarded-for", b"198.51.100.9"),
        ],
        method="POST",
        client=(SANDBOX_PEER, 40021),
        body=body,
        receive=receive,
    )
    assert status == 403
    assert response["reason"] == "forbidden"
    assert observer.calls == 0
    assert receive_calls == 0
    _assert_unchanged(data, before_state, before_files, origin)
