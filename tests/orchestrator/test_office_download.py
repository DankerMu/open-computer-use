# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Public-seam tests for confined DocumentServer callback downloads."""
from __future__ import annotations

import asyncio
import gzip
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from anyio.from_thread import start_blocking_portal

SERVER_DIR = Path(__file__).resolve().parents[2] / "computer-use-server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from office import download
from tests.orchestrator.test_office_ooxml import intact_docx
from tests.orchestrator.test_office_router import OFFICE_SETTINGS


class _DownloadOrigin:
    def __init__(self, responder):
        self.requests = []
        parent = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):
                return

            def do_GET(self):
                parent.requests.append(
                    {
                        "path": self.path,
                        "headers": {key.lower(): value for key, value in self.headers.items()},
                    }
                )
                status, headers, body = responder(self)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def close(self):
        self._server.shutdown()
        self._thread.join(timeout=2)
        self._server.server_close()


@contextmanager
def _origin(responder):
    server = _DownloadOrigin(responder)
    try:
        yield server
    finally:
        server.close()


@contextmanager
def _running_loop():
    with start_blocking_portal() as portal:
        yield portal.call(asyncio.get_running_loop)


def _fetch(url: str) -> bytes:
    with _running_loop() as loop:
        return download.fetch_callback_content(url, loop)


@pytest.fixture
def office_env(monkeypatch):
    monkeypatch.setenv("OCU_OFFICE_DOCSERVER_ORIGIN", OFFICE_SETTINGS["OCU_OFFICE_DOCSERVER_ORIGIN"])
    return monkeypatch


def test_browser_origin_is_rewritten_to_internal_path_and_query(office_env):
    body = intact_docx()

    def responder(handler):
        assert handler.path == "/cache/output.docx?filename=out.docx"
        return 200, {"Content-Type": "application/octet-stream"}, body

    with _origin(responder) as server:
        office_env.setenv("OCU_OFFICE_DOCSERVER_URL", server.url)
        presented = OFFICE_SETTINGS["OCU_OFFICE_DOCSERVER_ORIGIN"] + "/cache/output.docx?filename=out.docx"
        assert _fetch(presented) == body
        assert [item["path"] for item in server.requests] == ["/cache/output.docx?filename=out.docx"]
        assert "authorization" not in server.requests[0]["headers"]
        assert "cookie" not in server.requests[0]["headers"]


def test_internal_origin_is_fetched_directly(office_env):
    body = intact_docx()

    def responder(_handler):
        return 200, {}, body

    with _origin(responder) as server:
        office_env.setenv("OCU_OFFICE_DOCSERVER_URL", server.url)
        assert _fetch(server.url + "/kept.docx") == body
        assert [item["path"] for item in server.requests] == ["/kept.docx"]


def test_foreign_origin_makes_no_request(office_env):
    with _origin(lambda _handler: (200, {}, b"secret")) as server:
        office_env.setenv("OCU_OFFICE_DOCSERVER_URL", server.url)
        with pytest.raises(download.DownloadRejected) as error:
            _fetch("http://127.0.0.1:9/secret")
        assert error.value.reason == "download_url_rejected"
        assert server.requests == []


@pytest.mark.parametrize(
    "url",
    (
        "not-a-url",
        "ftp://docs.example:8082/file",
        "http://user:pass@docs.example:8082/file",
        "http:///file",
        "http://[::1/bad",
        "http://docs.example:99999/file",
    ),
)
def test_malformed_addresses_are_rejected_without_fetch(office_env, url):
    with _origin(lambda _handler: (200, {}, b"no")) as server:
        office_env.setenv("OCU_OFFICE_DOCSERVER_URL", server.url)
        with pytest.raises(download.DownloadRejected) as error:
            _fetch(url)
        assert error.value.reason == "download_url_rejected"
        assert server.requests == []


def test_foreign_redirect_is_not_followed(office_env):
    def responder(handler):
        return 302, {"Location": "http://example.test/elsewhere"}, b""

    with _origin(responder) as server:
        office_env.setenv("OCU_OFFICE_DOCSERVER_URL", server.url)
        with pytest.raises(download.DownloadFailed) as error:
            _fetch(server.url + "/start")
        assert error.value.reason == "download_failed"
        assert [item["path"] for item in server.requests] == ["/start"]


def test_oversized_stream_is_file_too_large(office_env, monkeypatch):
    monkeypatch.setattr(download, "MAX_FILE_SIZE", 4)

    def responder(_handler):
        return 200, {}, b"12345"

    with _origin(responder) as server:
        office_env.setenv("OCU_OFFICE_DOCSERVER_URL", server.url)
        with pytest.raises(download.FileTooLarge) as error:
            _fetch(server.url + "/big")
        assert error.value.reason == "file_too_large"


def test_timeout_is_download_failed(office_env, monkeypatch):
    monkeypatch.setattr("office.commands.HTTP_TIMEOUT_SECONDS", 0.05)

    def responder(_handler):
        time.sleep(0.3)
        return 200, {}, intact_docx()

    with _origin(responder) as server:
        office_env.setenv("OCU_OFFICE_DOCSERVER_URL", server.url)
        with pytest.raises(download.DownloadFailed) as error:
            _fetch(server.url + "/slow")
        assert error.value.reason == "download_failed"


def test_cancelled_submitted_download_future_is_download_failed(office_env, monkeypatch):
    submitted = []
    original = asyncio.run_coroutine_threadsafe

    def track(coro, loop):
        future = original(coro, loop)
        submitted.append(future)
        return future

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", track)

    def responder(_handler):
        time.sleep(2)
        return 200, {}, intact_docx()

    with _origin(responder) as server:
        office_env.setenv("OCU_OFFICE_DOCSERVER_URL", server.url)
        with _running_loop() as loop:
            def cancel_submitted():
                deadline = time.monotonic() + 2
                while not submitted:
                    if time.monotonic() >= deadline:
                        return
                    time.sleep(0.005)
                submitted[0].cancel()

            canceller = threading.Thread(target=cancel_submitted)
            canceller.start()
            try:
                with pytest.raises(download.DownloadFailed) as error:
                    download.fetch_callback_content(server.url + "/slow", loop)
                assert error.value.reason == "download_failed"
                assert submitted
                assert submitted[0].cancelled()
            finally:
                canceller.join(timeout=2)


def test_confined_rebuild_rejects_protocol_relative_path(office_env):
    office_env.setenv("OCU_OFFICE_DOCSERVER_URL", "http://127.0.0.1:1")
    with pytest.raises(download.DownloadRejected):
        download.confined_internal_url(
            OFFICE_SETTINGS["OCU_OFFICE_DOCSERVER_ORIGIN"] + "//evil.test/steal"
        )


def test_malformed_redirect_location_is_download_failed(office_env):
    def responder(_handler):
        return 302, {"Location": "http://[::1/bad"}, b""

    with _origin(responder) as server:
        office_env.setenv("OCU_OFFICE_DOCSERVER_URL", server.url)
        with pytest.raises(download.DownloadFailed) as error:
            _fetch(server.url + "/start")
        assert error.value.reason == "download_failed"
        assert [item["path"] for item in server.requests] == ["/start"]


def test_redirect_chain_obeys_single_total_deadline(office_env, monkeypatch):
    monkeypatch.setattr("office.commands.HTTP_TIMEOUT_SECONDS", 0.12)
    hops = {"n": 0}

    def responder(handler):
        hops["n"] += 1
        time.sleep(0.08)
        if handler.path == "/one":
            return 302, {"Location": "/two"}, b""
        return 200, {}, b"too-late"

    with _origin(responder) as server:
        office_env.setenv("OCU_OFFICE_DOCSERVER_URL", server.url)
        started = time.monotonic()
        with pytest.raises(download.DownloadFailed) as error:
            _fetch(server.url + "/one")
        elapsed = time.monotonic() - started
        assert error.value.reason == "download_failed"
        assert elapsed < 0.3
        assert hops["n"] >= 1
        assert [item["path"] for item in server.requests][0] == "/one"
        for item in server.requests:
            assert "authorization" not in item["headers"]
            assert "cookie" not in item["headers"]


def test_identity_encoding_is_requested_and_gzip_offer_is_not_accepted(office_env):
    body = intact_docx()
    packed = gzip.compress(body)

    def responder(handler):
        accept = handler.headers.get("Accept-Encoding", "")
        if "gzip" in accept.lower() and "identity" not in accept.lower():
            return 200, {"Content-Encoding": "gzip"}, packed
        return 200, {"Content-Encoding": "identity"}, body

    with _origin(responder) as server:
        office_env.setenv("OCU_OFFICE_DOCSERVER_URL", server.url)
        fetched = _fetch(server.url + "/encoded.docx")
        assert fetched == body
        assert fetched != packed
        headers = server.requests[0]["headers"]
        assert headers.get("accept-encoding") == "identity"
        assert "authorization" not in headers
        assert "cookie" not in headers

