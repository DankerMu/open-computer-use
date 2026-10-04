# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Public-seam tests for the DocumentServer command-service client."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import socket
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[2] / "computer-use-server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from office import commands

JWT_SECRET = "ds-jwt-secret-canary"
INTERNAL_TOKEN = "internal-token-canary"
ORIGIN = "http://documentserver-browser.example"
DOCUMENT_KEY = "doc-key-alpha"
SAVE_SEQ = 7
INTENT = "publish"


@pytest.fixture(autouse=True)
def isolated_office_env(monkeypatch):
    for name in (
        "OCU_OFFICE_DOCSERVER_URL",
        "OCU_OFFICE_DOCSERVER_ORIGIN",
        "OCU_OFFICE_SELF_URL",
        "OCU_OFFICE_JWT_SECRET",
        "OCU_INTERNAL_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OCU_OFFICE_JWT_SECRET", JWT_SECRET)
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("OCU_OFFICE_DOCSERVER_ORIGIN", ORIGIN)


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * ((4 - len(segment) % 4) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _independent_verify(token: str, secret: str) -> dict:
    header_b64, payload_b64, signature_b64 = token.split(".")
    signing = f"{header_b64}.{payload_b64}".encode("ascii")
    expected = hmac.new(secret.encode("utf-8"), signing, hashlib.sha256).digest()
    presented = _b64url_decode(signature_b64)
    assert hmac.compare_digest(presented, expected)
    header = json.loads(_b64url_decode(header_b64))
    assert header["alg"] == "HS256"
    payload = json.loads(_b64url_decode(payload_b64))
    assert isinstance(payload, dict)
    return payload


def _assert_no_secrets(capsys, caplog, *hidden: str, extra: str = "") -> None:
    captured = capsys.readouterr()
    text = captured.out + captured.err + caplog.text + extra
    for value in hidden:
        assert value not in text


class _CommandOrigin:
    def __init__(self, responder, trap=None):
        self.requests = []
        self.trap_hits = 0
        self._lock = threading.Lock()
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def _record(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b""
                with parent._lock:
                    parent.requests.append(
                        {
                            "path": self.path,
                            "method": self.command,
                            "headers": {key.lower(): value for key, value in self.headers.items()},
                            "body": body,
                        }
                    )
                return body

            def do_POST(self):
                body = self._record()
                status, headers, payload = responder(self, body, parent)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                if isinstance(payload, tuple):
                    for index, chunk in enumerate(payload):
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        if index + 1 < len(payload):
                            time.sleep(0.05)
                else:
                    self.wfile.write(payload)

            def do_GET(self):
                with parent._lock:
                    parent.trap_hits += 1
                status, headers, payload = (trap or (404, {}, b""))
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


@contextmanager
def _command_origin(responder, trap=None):
    origin = _CommandOrigin(responder, trap=trap)
    try:
        yield origin
    finally:
        origin.stop()


def _json_error(code):
    def responder(_handler, _body, _parent):
        return 200, {"Content-Type": "application/json"}, json.dumps({"error": code}).encode("utf-8")

    return responder


def _verified_forcesave(body: bytes) -> dict:
    envelope = json.loads(body.decode("utf-8"))
    assert set(envelope) == {"token"}
    payload = _independent_verify(envelope["token"], JWT_SECRET)
    assert payload["c"] == "forcesave"
    assert payload["key"] == DOCUMENT_KEY
    userdata = json.loads(payload["userdata"])
    assert userdata == {"save_seq": SAVE_SEQ, "intent": INTENT}
    assert payload["userdata"] == json.dumps(userdata, separators=(",", ":"))
    return payload


def _run(coro):
    return asyncio.run(coro)


def test_forcesave_accepted_round_trip_verifies_token_and_userdata(monkeypatch, capsys, caplog):
    def responder(handler, body, parent):
        assert handler.path == "/command"
        _verified_forcesave(body)
        return 200, {"Content-Type": "application/json"}, b'{"error":0}'

    with _command_origin(responder) as origin:
        monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", origin.url + "/")
        outcome = _run(commands.forcesave(DOCUMENT_KEY, SAVE_SEQ, INTENT))
        assert outcome is commands.ForceSaveOutcome.ACCEPTED
        assert origin.trap_hits == 0
        assert origin.requests[0]["path"] == "/command"
    _assert_no_secrets(capsys, caplog, JWT_SECRET, INTERNAL_TOKEN)


def test_forcesave_key_unknown_and_nothing_to_save_are_distinct(monkeypatch):
    with _command_origin(_json_error(1)) as origin:
        monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", origin.url)
        assert _run(commands.forcesave(DOCUMENT_KEY, SAVE_SEQ, INTENT)) is commands.ForceSaveOutcome.KEY_UNKNOWN
    with _command_origin(_json_error(4)) as origin:
        monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", origin.url)
        assert _run(commands.forcesave(DOCUMENT_KEY, SAVE_SEQ, INTENT)) is commands.ForceSaveOutcome.NOTHING_TO_SAVE


@pytest.mark.parametrize("code", (2, 3, 5, 6, -1, 99))
def test_forcesave_other_integer_codes_are_rejected(monkeypatch, code):
    with _command_origin(_json_error(code)) as origin:
        monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", origin.url)
        assert _run(commands.forcesave(DOCUMENT_KEY, SAVE_SEQ, INTENT)) is commands.ForceSaveOutcome.REJECTED


@pytest.mark.parametrize("code", (True, False, "0", 1.0, None))
def test_forcesave_boolean_and_noninteger_codes_are_rejected(monkeypatch, code):
    with _command_origin(_json_error(code)) as origin:
        monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", origin.url)
        assert _run(commands.forcesave(DOCUMENT_KEY, SAVE_SEQ, INTENT)) is commands.ForceSaveOutcome.REJECTED


def test_forcesave_malformed_and_error_http_are_rejected_not_accepted(monkeypatch):
    def malformed(_handler, _body, _parent):
        return 200, {"Content-Type": "application/json"}, b"not-json"

    def http_error(_handler, _body, _parent):
        return 500, {"Content-Type": "text/plain"}, b"boom"

    with _command_origin(malformed) as origin:
        monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", origin.url)
        assert _run(commands.forcesave(DOCUMENT_KEY, SAVE_SEQ, INTENT)) is commands.ForceSaveOutcome.REJECTED
    with _command_origin(http_error) as origin:
        monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", origin.url)
        assert _run(commands.forcesave(DOCUMENT_KEY, SAVE_SEQ, INTENT)) is commands.ForceSaveOutcome.REJECTED


def test_lookup_known_unknown_and_never_orphans_on_unavailability(monkeypatch):
    def info_ok(handler, body, _parent):
        envelope = json.loads(body.decode("utf-8"))
        payload = _independent_verify(envelope["token"], JWT_SECRET)
        assert payload == {"c": "info", "key": DOCUMENT_KEY}
        assert handler.path == "/command"
        return 200, {"Content-Type": "application/json"}, b'{"error":0}'

    with _command_origin(info_ok) as origin:
        monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", origin.url)
        assert _run(commands.lookup_key(DOCUMENT_KEY)) is commands.KeyLookupOutcome.KNOWN
    with _command_origin(_json_error(1)) as origin:
        monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", origin.url)
        assert _run(commands.lookup_key(DOCUMENT_KEY)) is commands.KeyLookupOutcome.KEY_UNKNOWN
    for responder in (_json_error(4), _json_error(True), _json_error("1")):
        with _command_origin(responder) as origin:
            monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", origin.url)
            assert _run(commands.lookup_key(DOCUMENT_KEY)) is commands.KeyLookupOutcome.UNREACHABLE


def test_refused_connection_is_unreachable_for_forcesave_and_lookup(monkeypatch):
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    port = holder.getsockname()[1]
    holder.close()
    monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", f"http://127.0.0.1:{port}")
    assert _run(commands.forcesave(DOCUMENT_KEY, SAVE_SEQ, INTENT)) is commands.ForceSaveOutcome.UNREACHABLE
    assert _run(commands.lookup_key(DOCUMENT_KEY)) is commands.KeyLookupOutcome.UNREACHABLE


def test_timeout_is_unreachable_and_does_not_follow_redirects(monkeypatch):
    started = threading.Event()

    def sleeper(_handler, _body, _parent):
        started.set()
        time.sleep(1)
        return 200, {"Content-Type": "application/json"}, b'{"error":0}'

    def redirect(handler, _body, parent):
        location = f"{parent.url.replace('127.0.0.1', 'documentserver-browser.example')}/trap"
        return 302, {"Location": location}, b""

    monkeypatch.setattr(commands, "HTTP_TIMEOUT_SECONDS", 0.05)
    with _command_origin(sleeper) as origin:
        monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", origin.url)
        assert _run(commands.forcesave(DOCUMENT_KEY, SAVE_SEQ, INTENT)) is commands.ForceSaveOutcome.UNREACHABLE
        assert started.wait(1)
    with _command_origin(redirect) as origin:
        monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", origin.url)
        assert _run(commands.forcesave(DOCUMENT_KEY, SAVE_SEQ, INTENT)) is commands.ForceSaveOutcome.REJECTED
        assert origin.trap_hits == 0


def test_fragmented_bodies_wait_for_eof_and_reject_trailing_garbage(monkeypatch):
    first, rest = b'{"error":0}', b" invalid trailing body"

    def trailing(_handler, _body, _parent):
        return 200, {"Content-Type": "application/json"}, (first, rest)

    def valid_chunks(_handler, _body, _parent):
        return 200, {"Content-Type": "application/json"}, (b'{"error":', b"0}")

    def oversized_chunks(_handler, _body, _parent):
        return (
            200,
            {"Content-Type": "application/json"},
            (b'{"error":0}', b"x" * commands.MAX_RESPONSE_BYTES),
        )

    with _command_origin(trailing) as origin:
        monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", origin.url)
        assert _run(commands.forcesave(DOCUMENT_KEY, SAVE_SEQ, INTENT)) is commands.ForceSaveOutcome.REJECTED
    with _command_origin(valid_chunks) as origin:
        monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", origin.url)
        assert _run(commands.forcesave(DOCUMENT_KEY, SAVE_SEQ, INTENT)) is commands.ForceSaveOutcome.ACCEPTED
    with _command_origin(oversized_chunks) as origin:
        monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", origin.url)
        assert _run(commands.forcesave(DOCUMENT_KEY, SAVE_SEQ, INTENT)) is commands.ForceSaveOutcome.REJECTED


def test_oversized_body_is_rejected_and_local_validation_happens_before_io(monkeypatch):
    oversized = b'{"error":0}' + b"x" * (commands.MAX_RESPONSE_BYTES)

    def huge(_handler, _body, _parent):
        return 200, {"Content-Type": "application/json"}, oversized

    with _command_origin(huge) as origin:
        monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", origin.url)
        assert _run(commands.forcesave(DOCUMENT_KEY, SAVE_SEQ, INTENT)) is commands.ForceSaveOutcome.REJECTED

    with _command_origin(_json_error(0)) as origin:
        monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", origin.url)
        for seq, intent in ((True, INTENT), (0, INTENT), (SAVE_SEQ, "archive"), (1.0, INTENT)):
            with pytest.raises(ValueError):
                _run(commands.forcesave(DOCUMENT_KEY, seq, intent))
        assert origin.requests == []


def test_cancellation_propagates_and_secrets_stay_out_of_errors(monkeypatch, capsys, caplog):
    release = threading.Event()

    def blocker(_handler, _body, _parent):
        release.wait(2)
        return 200, {"Content-Type": "application/json"}, b'{"error":0}'

    async def cancelled():
        task = asyncio.create_task(commands.forcesave(DOCUMENT_KEY, SAVE_SEQ, INTENT))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as excinfo:
            await task
        return excinfo

    with _command_origin(blocker) as origin:
        monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", origin.url)
        excinfo = _run(cancelled())
        extra = f"{excinfo.value!s} {excinfo.value!r}"
        _assert_no_secrets(capsys, caplog, JWT_SECRET, INTERNAL_TOKEN, extra=extra)
        release.set()
