# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""The ocu-eval adapter against recorded dmxapi responses: speech (#817), images (#853).

The adapter and OCU's speech client are loaded by file location under unique
names and never registered in sys.modules: a module whose file lies under
computer-use-server/ would otherwise count as an unowned server module in the
module-ownership tests. The adapter runs under uvicorn on an ephemeral
127.0.0.1 port; a threaded fake of dmxapi replays the recordings.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
import os
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from unittest import mock
import uuid

import aiohttp
import uvicorn
import uvicorn.logging

ROOT = Path(__file__).resolve().parents[2]
ADAPTER_FILE = ROOT / "deploy" / "eval" / "adapter" / "eval_adapter.py"
SPEECH_FILE = ROOT / "computer-use-server" / "speech.py"
RECORDINGS = json.loads((Path(__file__).resolve().parent / "fixtures" / "eval_adapter"
                         / "dmxapi-tts-recorded.json").read_text(encoding="utf-8"))
MP3 = RECORDINGS["mp3_with_instructions_and_voice"]
WAV = RECORDINGS["wav_default_voice"]
UNKNOWN_VOICE = RECORDINGS["unknown_voice_400"]
UNKNOWN_MODEL = RECORDINGS["unknown_model"]
SEEDREAM = json.loads((Path(__file__).resolve().parent / "fixtures" / "eval_adapter"
                       / "dmxapi-seedream-recorded.json").read_text(encoding="utf-8"))["cases"]
GEN_B64 = SEEDREAM["gen_b64"]
GEN_URL = SEEDREAM["gen_default_format"]
EDIT_B64 = SEEDREAM["edit_b64"]
BAD_SIZE = SEEDREAM["bad_size"]
MODEL = MP3["request"]["model"]
INSTRUCTIONS = MP3["request"]["messages"][0]["content"]
TEXT = MP3["request"]["messages"][1]["content"]
VOICE = MP3["request"]["audio"]["voice"]
PROVIDER_KEY = "fake-provider-key"
ADAPTER_TOKEN = "fake-adapter-token"
SPEECH_PATH = "/v1/audio/speech"
# speech.py keeps the first 200 characters of an error body.
OCU_EXCERPT_CHARS = 200
ERROR_CHARS = 160
REDACTED = "[redacted]"

_LOADED: dict[str, object] = {}


def load_by_location(path: Path, name: str):
    """Execute a module from its file without registering it or writing bytecode."""
    if name not in _LOADED:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        previous = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = previous
        _LOADED[name] = module
    return _LOADED[name]


def adapter_module():
    return load_by_location(ADAPTER_FILE, "ocu_eval_adapter_under_test")


def speech_module():
    return load_by_location(SPEECH_FILE, "ocu_speech_client_under_test")


def audio_of(recording: dict) -> bytes:
    return base64.b64decode(recording["body"]["choices"][0]["message"]["audio"]["data"])


def send(handler, status: int, body: bytes, content_type: str = "application/json",
         headers: tuple = ()):
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    for name, value in headers:
        handler.send_header(name, value)
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        pass  # the adapter abandoned the read on purpose


def replay(recording: dict):
    body = json.dumps(recording["body"], ensure_ascii=False).encode("utf-8")
    return lambda handler: send(handler, recording["status"], body, recording["content_type"])


def success_with(data) -> callable:
    envelope = json.loads(json.dumps(MP3["body"]))
    envelope["choices"][0]["message"]["audio"]["data"] = data
    body = json.dumps(envelope).encode("utf-8")
    return lambda handler: send(handler, 200, body)


class FakeServer:
    """A recording HTTP server on an ephemeral 127.0.0.1 port."""

    def __init__(self):
        self.requests: list[dict] = []
        self.reply = lambda handler: send(handler, 500, b"{}")
        self.release = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                owner.requests.append({"method": self.command, "path": self.path,
                                       "headers": dict(self.headers),
                                       "body": self.rfile.read(length)})
                owner.reply(self)

            do_GET = do_POST

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(self.format(record))


class AdapterCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapter = adapter_module()
        self.bodies: list[bytes] = []
        self.capture = Capture()
        watched = [logging.getLogger(), logging.getLogger(self.adapter.LOGGER_NAME)]
        levels = [logger.level for logger in watched]
        for logger in watched:
            logger.addHandler(self.capture)
        logging.getLogger().setLevel(logging.INFO)

        def restore_logging():
            for logger, level in zip(watched, levels):
                logger.removeHandler(self.capture)
                logger.setLevel(level)

        # Cleanups run last-in first-out: the leak check runs after every server stopped.
        self.addCleanup(restore_logging)
        self.addCleanup(self.assert_no_secret_anywhere)
        self.upstream = FakeServer()
        self.addCleanup(self.upstream.close)
        self.adapter_url = await self.serve_adapter(self.upstream.origin + "/v1")

    async def serve_adapter(self, upstream_base_url: str) -> str:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        config = uvicorn.Config(self.adapter.app, log_config=None, log_level="info", lifespan="on")
        server = uvicorn.Server(config)
        environment = {"PROVIDER_KEY": PROVIDER_KEY, "ADAPTER_TOKEN": ADAPTER_TOKEN,
                       "UPSTREAM_BASE_URL": upstream_base_url}
        with mock.patch.dict(os.environ, environment):
            task = asyncio.create_task(server.serve(sockets=[listener]))
            for _ in range(500):
                if server.started or task.done():
                    break
                await asyncio.sleep(0.01)
        self.assertTrue(server.started, "the adapter did not start")

        async def stop():
            server.should_exit = True
            await asyncio.wait_for(task, 10)
            listener.close()

        self.addAsyncCleanup(stop)
        return f"http://127.0.0.1:{listener.getsockname()[1]}"

    async def call(self, path: str = SPEECH_PATH, body=None, *, token: str | None = ADAPTER_TOKEN,
                   raw: bytes | None = None, method: str = "POST", base: str | None = None):
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        if raw is not None:
            headers["Content-Type"] = "application/json"
        async with aiohttp.ClientSession() as session:
            async with session.request(method, (base or self.adapter_url) + path,
                                       json=None if raw is not None else body,
                                       data=raw, headers=headers) as response:
                payload = await response.read()
        self.bodies.append(payload)
        return response.status, response.content_type, payload

    async def speak(self, fmt: str, **fields):
        body = {"model": MODEL, "input": TEXT, "response_format": fmt, **fields}
        return await self.call(body=body)

    def assert_no_secret_anywhere(self):
        logs = "\n".join(self.capture.lines)
        for secret in (PROVIDER_KEY, ADAPTER_TOKEN):
            for payload in self.bodies:
                self.assertFalse(secret.encode() in payload, "a secret reached a response body")
            self.assertFalse(secret in logs, "a secret reached a log line")

    def upstream_json(self, index: int = -1) -> dict:
        return json.loads(self.upstream.requests[index]["body"])

    def assert_502(self, result, *, contains: str | None = None, equals: str | None = None) -> str:
        status, content_type, payload = result
        self.assertEqual(status, 502, payload)
        self.assertEqual(content_type, "application/json")
        text = payload.decode("utf-8")
        self.assertNotIn("\\u", text)
        self.assertLessEqual(len(text), OCU_EXCERPT_CHARS, "the body overflows speech.py's excerpt")
        document = json.loads(text)
        self.assertEqual(list(document), ["error"])
        message = document["error"]
        self.assertIsInstance(message, str)
        self.assertLessEqual(len(message), ERROR_CHARS)
        if contains is not None:
            self.assertIn(contains, message)
        if equals is not None:
            self.assertEqual(message, equals)
        return message


class SpeechSuccessTests(AdapterCase):
    async def test_mp3_with_instructions_and_voice_sends_the_recorded_request(self):
        self.upstream.reply = replay(MP3)
        status, content_type, payload = await self.speak("mp3", voice=VOICE, instructions=INSTRUCTIONS)
        self.assertEqual(status, 200, payload)
        self.assertEqual(content_type, "audio/mpeg")
        self.assertEqual(payload, audio_of(MP3))
        self.assertEqual(len(self.upstream.requests), 1)
        sent = self.upstream.requests[0]
        self.assertEqual(sent["path"], "/v1/chat/completions")
        self.assertEqual(sent["headers"]["Authorization"], f"Bearer {PROVIDER_KEY}")
        upstream = self.upstream_json()
        self.assertEqual(upstream, MP3["request"])
        self.assertEqual([message["role"] for message in upstream["messages"]], ["user", "assistant"])

    async def test_wav_without_voice_or_instructions_sends_one_assistant_message(self):
        self.upstream.reply = replay(WAV)
        for case, fields in (("absent", {}), ("empty", {"voice": "", "instructions": ""})):
            with self.subTest(case=case):
                status, content_type, payload = await self.speak("wav", **fields)
                self.assertEqual(status, 200, payload)
                self.assertEqual(content_type, "audio/wav")
                self.assertEqual(payload, audio_of(WAV))
                upstream = self.upstream_json()
                self.assertEqual(upstream, WAV["request"])
                self.assertNotIn("voice", upstream["audio"])

    async def test_contract_matches_ocu_speech_client(self):
        speech = speech_module()
        self.assertEqual(self.adapter.MAX_AUDIO_BYTES, speech.MAX_AUDIO_BYTES)
        self.assertEqual(self.adapter.UPSTREAM_TIMEOUT_SECONDS, 90)
        self.assertLess(self.adapter.UPSTREAM_TIMEOUT_SECONDS, speech.DEFAULT_TIMEOUT_SECONDS)
        for fmt, recording in (("mp3", MP3), ("wav", WAV)):
            with self.subTest(fmt=fmt):
                self.upstream.reply = replay(recording)
                status, content_type, _ = await self.speak(fmt)
                self.assertEqual(status, 200)
                self.assertIn(content_type, speech.CONTENT_TYPES[fmt])

    async def test_healthz_is_unauthenticated(self):
        status, content_type, payload = await self.call("/healthz", method="GET", token=None)
        self.assertEqual((status, content_type), (200, "application/json"))
        self.assertEqual(json.loads(payload), {"ok": True})
        self.assertEqual(self.upstream.requests, [])

    async def test_log_line_names_route_status_elapsed_and_upstream_status_only(self):
        self.upstream.reply = replay(MP3)
        await self.speak("mp3", voice=VOICE, instructions=INSTRUCTIONS)
        self.upstream.reply = replay(UNKNOWN_VOICE)
        await self.speak("wav", voice="alloy")
        adapter_lines = [line for line in self.capture.lines
                         if line.startswith(self.adapter.LOGGER_NAME + " ")]
        self.assertEqual(len(adapter_lines), 2, adapter_lines)
        for line, status, upstream in zip(adapter_lines, ("200", "502"), ("200", "400")):
            self.assertIn(SPEECH_PATH, line)
            self.assertIn(f"status={status}", line)
            self.assertIn(f"upstream={upstream}", line)
            self.assertRegex(line, r"elapsed_ms=\d+")
        logs = "\n".join(self.capture.lines)
        for private in (TEXT, INSTRUCTIONS, "Unknown voice", "Bearer"):
            self.assertNotIn(private, logs)


class SpeechFailureTests(AdapterCase):
    async def test_recorded_unknown_voice_is_502_with_the_voice_list(self):
        self.upstream.reply = replay(UNKNOWN_VOICE)
        result = await self.speak("wav", voice="alloy")
        expected = f"upstream HTTP 400: {UNKNOWN_VOICE['body']['error']['param']}"[:ERROR_CHARS]
        message = self.assert_502(result, equals=expected)
        for fragment in ("Unknown voice", "冰糖", "Dean"):
            self.assertIn(fragment.encode("utf-8"), result[2])
        self.assertIn("Dean", message)
        self.assertEqual(self.upstream_json()["audio"], {"format": "wav", "voice": "alloy"})

    async def test_recorded_unknown_model_is_502_with_the_message(self):
        self.upstream.reply = replay(UNKNOWN_MODEL)
        result = await self.call(body={"model": UNKNOWN_MODEL["request"]["model"], "input": TEXT,
                                       "response_format": "wav"})
        expected = f"upstream HTTP 503: {UNKNOWN_MODEL['body']['error']['message']}"[:ERROR_CHARS]
        self.assert_502(result, equals=expected)
        self.assertEqual(self.upstream_json(), UNKNOWN_MODEL["request"])

    async def test_redirect_is_not_followed(self):
        elsewhere = FakeServer()
        self.addCleanup(elsewhere.close)
        elsewhere.reply = replay(MP3)
        location = elsewhere.origin + "/v1/chat/completions"
        self.upstream.reply = lambda handler: send(handler, 302, b"", "text/plain",
                                                   (("Location", location),))
        self.assert_502(await self.speak("mp3"), contains="upstream HTTP 302")
        self.assertEqual(len(self.upstream.requests), 1)
        self.assertEqual(elsewhere.requests, [])

    async def test_timeout_is_502(self):
        def stall(handler):
            self.upstream.release.wait(10)
            send(handler, 200, json.dumps(MP3["body"]).encode())

        self.upstream.reply = stall
        with mock.patch.object(self.adapter, "UPSTREAM_TIMEOUT_SECONDS", 0.3):
            result = await self.speak("mp3")
        self.upstream.release.set()
        self.assert_502(result, contains="timed out")

    async def test_transport_error_names_the_class_only(self):
        unused = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
        unused.close()
        base = await self.serve_adapter(f"http://127.0.0.1:{port}/v1")
        result = await self.call(body={"model": MODEL, "input": TEXT, "response_format": "mp3"},
                                 base=base)
        message = self.assert_502(result, contains="ClientConnectorError")
        self.assertNotIn("127.0.0.1", message)
        self.assertNotIn(str(port), message)

    async def test_unusable_success_bodies_are_502(self):
        cases = (
            ("non-JSON", lambda handler: send(handler, 200, b"<html>busy</html>", "text/html"),
             "mp3", "not JSON"),
            ("not an object", lambda handler: send(handler, 200, b"[]"), "mp3", "no audio"),
            ("no audio", lambda handler: send(handler, 200, json.dumps(
                {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}).encode()),
             "mp3", "no audio"),
            ("non-string audio", success_with(123), "mp3", "no audio"),
            ("invalid base64", success_with("not base64!"), "mp3", "base64"),
            ("empty audio", success_with(""), "mp3", "empty"),
            ("wav bytes for mp3", replay(WAV), "mp3", "mp3"),
            ("mp3 bytes for wav", replay(MP3), "wav", "wav"),
        )
        for case, reply, fmt, fragment in cases:
            with self.subTest(case=case):
                self.upstream.reply = reply
                self.assert_502(await self.speak(fmt), contains=fragment)

    async def test_id3_tagged_mp3_is_accepted(self):
        audio = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 32
        self.upstream.reply = success_with(base64.b64encode(audio).decode())
        status, content_type, payload = await self.speak("mp3")
        self.assertEqual((status, content_type, payload), (200, "audio/mpeg", audio))

    async def test_upstream_body_over_the_read_cap_is_abandoned(self):
        limit = 3000
        read_cap = math.ceil(limit * 4 / 3) + 64 * 1024
        oversized = json.dumps({"choices": [{"message": {"audio": {
            "data": "A" * (read_cap + 1)}}}]}).encode()

        def chunked(handler):
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Transfer-Encoding", "chunked")
            handler.end_headers()
            try:
                for start in range(0, len(oversized), 8192):
                    piece = oversized[start:start + 8192]
                    handler.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                handler.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass  # the adapter abandoned the read

        cases = (("chunked", chunked),
                 ("declared length", lambda handler: send(handler, 200, oversized)))
        with mock.patch.object(self.adapter, "MAX_AUDIO_BYTES", limit):
            for case, reply in cases:
                with self.subTest(case=case):
                    self.upstream.reply = reply
                    self.assert_502(await self.speak("mp3"),
                                    equals=f"upstream body exceeds {read_cap} bytes")

    async def test_decoded_audio_over_the_cap_is_refused(self):
        limit = 3000
        with mock.patch.object(self.adapter, "MAX_AUDIO_BYTES", limit):
            at_limit = b"ID3" + b"\x00" * (limit - 3)
            self.upstream.reply = success_with(base64.b64encode(at_limit).decode())
            status, _, payload = await self.speak("mp3")
            self.assertEqual((status, payload), (200, at_limit))
            self.upstream.reply = success_with(base64.b64encode(at_limit + b"\x00").decode())
            self.assert_502(await self.speak("mp3"), equals=f"decoded audio exceeds {limit} bytes")

    async def test_echoed_secrets_are_redacted_before_the_cut(self):
        prefix = "upstream HTTP 400: "
        # The key straddles the 160-character cut: cutting first would keep part of it.
        filler = "x" * (ERROR_CHARS - len(prefix) - 5)
        cases = (
            ("param", {"error": {"message": "m", "param": f"{filler}{PROVIDER_KEY}"}},
             f"{filler}{PROVIDER_KEY}"),
            ("message", {"error": {"message": f"bad key {PROVIDER_KEY} and {ADAPTER_TOKEN}"}},
             f"bad key {PROVIDER_KEY} and {ADAPTER_TOKEN}"),
        )
        for case, body, detail in cases:
            with self.subTest(case=case):
                encoded = json.dumps(body).encode()
                self.upstream.reply = lambda handler, encoded=encoded: send(handler, 400, encoded)
                expected = (prefix + detail.replace(PROVIDER_KEY, REDACTED)
                            .replace(ADAPTER_TOKEN, REDACTED))[:ERROR_CHARS]
                self.assert_502(await self.speak("mp3"), equals=expected)
        with self.subTest(case="plain-text body"):
            self.upstream.reply = lambda handler: send(
                handler, 500, f"gateway saw {PROVIDER_KEY}".encode(), "text/plain")
            self.assert_502(await self.speak("mp3"),
                            equals=f"upstream HTTP 500: gateway saw {REDACTED}")


class SpeechRequestTests(AdapterCase):
    async def test_missing_or_wrong_token_is_401_without_upstream_call(self):
        self.upstream.reply = replay(MP3)
        body = {"model": MODEL, "input": TEXT, "response_format": "mp3"}
        for case, token in (("missing", None), ("wrong", "fake-wrong-token"), ("empty", "")):
            with self.subTest(case=case):
                status, content_type, payload = await self.call(body=body, token=token)
                self.assertEqual((status, content_type), (401, "application/json"))
                self.assertIn("error", json.loads(payload))
        self.assertEqual(self.upstream.requests, [])

    async def test_invalid_requests_are_400_without_upstream_call(self):
        self.upstream.reply = replay(MP3)
        valid = {"model": MODEL, "input": TEXT, "response_format": "mp3"}
        cases = (
            ("ogg", {**valid, "response_format": "ogg"}),
            ("empty input", {**valid, "input": ""}),
            ("missing format", {key: value for key, value in valid.items()
                                if key != "response_format"}),
            ("empty model", {**valid, "model": ""}),
            ("non-string voice", {**valid, "voice": 3}),
            ("non-string instructions", {**valid, "instructions": None}),
            ("unknown field", {**valid, "speed": 1.0}),
            ("not an object", [valid]),
        )
        for case, body in cases:
            with self.subTest(case=case):
                status, content_type, payload = await self.call(body=body)
                self.assertEqual((status, content_type), (400, "application/json"), payload)
                self.assertIn("error", json.loads(payload))
        with self.subTest(case="not JSON"):
            status, _, _ = await self.call(raw=b"{not json")
            self.assertEqual(status, 400)
        self.assertEqual(self.upstream.requests, [])

    async def test_empty_secrets_refuse_startup(self):
        for name in ("PROVIDER_KEY", "ADAPTER_TOKEN"):
            with self.subTest(name=name):
                environment = {"PROVIDER_KEY": PROVIDER_KEY, "ADAPTER_TOKEN": ADAPTER_TOKEN,
                               "UPSTREAM_BASE_URL": self.upstream.origin + "/v1", name: ""}
                with mock.patch.dict(os.environ, environment):
                    with self.assertRaises(RuntimeError) as caught:
                        async with self.adapter.app.router.lifespan_context(self.adapter.app):
                            pass
                self.assertIn(name, str(caught.exception))


class LogRedactionTests(AdapterCase):
    def capture_on(self, name: str, formatter: logging.Formatter | None = None) -> Capture:
        """A capture attached to one logger itself, where its filters run."""
        capture = Capture()
        if formatter is not None:
            capture.setFormatter(formatter)
        logger = logging.getLogger(name)
        logger.addHandler(capture)
        self.addCleanup(logger.removeHandler, capture)
        return capture

    async def test_access_lines_redact_secrets_in_the_query(self):
        # uvicorn's own access format, with the colors that would split an exact match off.
        access = self.capture_on("uvicorn.access", uvicorn.logging.AccessFormatter(
            '%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False))
        self.upstream.reply = replay(MP3)
        query = f"?k={PROVIDER_KEY}&t={ADAPTER_TOKEN}"
        status, _, _ = await self.call(SPEECH_PATH + query, body={
            "model": MODEL, "input": TEXT, "response_format": "mp3"})
        self.assertEqual(status, 200)
        status, _, _ = await self.call(f"/healthz?k={PROVIDER_KEY}", method="GET", token=None)
        self.assertEqual(status, 200)
        expected = (f'"POST {SPEECH_PATH}?k={REDACTED}&t={REDACTED} HTTP/1.1" 200',
                    f'"GET /healthz?k={REDACTED} HTTP/1.1" 200')
        self.assertEqual(len(access.lines), 2, access.lines)
        for line, request_line in zip(access.lines, expected):
            self.assertIn(request_line, line)
            for secret in (PROVIDER_KEY, ADAPTER_TOKEN):
                self.assertNotIn(secret, line)

    async def test_adapter_and_uvicorn_error_records_are_redacted(self):
        records = (
            ("message", (f"saw {PROVIDER_KEY} and {ADAPTER_TOKEN}",)),
            ("tuple args", ("saw %s and %s", PROVIDER_KEY, ADAPTER_TOKEN)),
            ("dict args", ("saw %(key)s and %(token)s",
                           {"key": PROVIDER_KEY, "token": ADAPTER_TOKEN})),
        )
        for name in (self.adapter.LOGGER_NAME, "uvicorn.error"):
            capture = self.capture_on(name, logging.Formatter("%(message)s"))
            for shape, arguments in records:
                with self.subTest(logger=name, shape=shape):
                    capture.lines.clear()
                    logging.getLogger(name).warning(*arguments)
                    self.assertEqual(capture.lines, [f"saw {REDACTED} and {REDACTED}"])


class OcuSpeechClientTests(AdapterCase):
    def client_config(self):
        speech = speech_module()
        return speech, speech.SpeechConfig(base_url=self.adapter_url + "/v1", model=MODEL, voice="",
                                           api_key=ADAPTER_TOKEN, timeout=30)

    async def test_synthesize_returns_the_mp3_bytes(self):
        speech, config = self.client_config()
        self.upstream.reply = replay(MP3)
        audio = await speech.synthesize(config, TEXT, "mp3", instructions=INSTRUCTIONS, voice=VOICE)
        self.assertEqual(audio, audio_of(MP3))
        self.assertEqual(self.upstream_json(), MP3["request"])

    async def test_synthesize_reports_the_unknown_voice(self):
        speech, config = self.client_config()
        self.upstream.reply = replay(UNKNOWN_VOICE)
        with self.assertRaises(speech.SpeechError) as caught:
            await speech.synthesize(config, TEXT, "wav", voice="alloy")
        text = str(caught.exception)
        for fragment in ("Unknown voice", "冰糖", "Dean"):
            self.assertIn(fragment, text)
        for secret in (PROVIDER_KEY, ADAPTER_TOKEN):
            self.assertNotIn(secret, text)


# --- Image routes (#853) -------------------------------------------------------------------

GENERATIONS_PATH = "/v1/images/generations"
EDITS_PATH = "/v1/images/edits"
IMAGE_MODEL = GEN_B64["request"]["model"]
GEN_PROMPT = GEN_B64["request"]["input"]
EDIT_PROMPT = EDIT_B64["request"]["input"]
WEBUI_SIZE = "2048x2048"
# The recorded generation (truncated JPEG): the image the recorded edit sent back as image/png.
JPEG = base64.b64decode(GEN_B64["body"]["data"][0]["b64_json"])
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
WEBP = b"RIFF" + (36).to_bytes(4, "little") + b"WEBPVP8 " + bytes(28)
# WebUI's IMAGE_FILE_EXTENSIONS for get_image_file_item's filename.
EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "text/plain": ".txt"}
EXTRA = {"seed": 42}


def webui_generation_payload(**extra) -> dict:
    """The JSON body WebUI's ``openai`` engine posts to ``/images/generations``.

    open-webui ``backend/open_webui/routers/images.py`` ~643-661: model, prompt, n
    (``CreateImageForm.n`` defaults to 1), size, ``response_format: "b64_json"``
    (the model does not match ``^gpt-image``), then ``openai_request_params`` of the
    configured params (``utils/images/aspect_ratio.py`` ~31-35), which drops
    ``aspect_ratio_sizes`` and passes every other key on.
    """
    return {"model": IMAGE_MODEL, "prompt": GEN_PROMPT, "n": 1, "size": WEBUI_SIZE,
            "response_format": "b64_json", **extra}


def multipart_form(fields: list[tuple[str, str]], files: list[tuple[str, bytes, str]]) -> aiohttp.FormData:
    """Text fields as ``str``, then files with a filename and a content type.

    open-webui ``routers/images.py`` ~1035-1043 (``form.add_field(key, str(value))``,
    then ``add_field(param_name, BytesIO, filename=…, content_type=…)``) and
    ``get_image_file_item`` ~165-177 (``<uuid><extension>``, the data URL's MIME type).
    """
    form = aiohttp.FormData(default_to_multipart=True)
    for name, value in fields:
        form.add_field(name, value)
    for name, data, content_type in files:
        form.add_field(name, io.BytesIO(data), filename=f"{uuid.uuid4()}{EXTENSIONS[content_type]}",
                       content_type=content_type)
    return form


def webui_edit_form(prompt: str, images: list[tuple[str, bytes, str]], *, n: int | None = None,
                    background: str | None = None) -> aiohttp.FormData:
    """The multipart form WebUI's ``openai`` edit engine posts to ``/images/edits``.

    open-webui ``routers/images.py`` ~1001-1012: model, prompt, ``n`` only when set,
    size, ``background`` only when set, ``response_format: "b64_json"``; no
    configured extras. One image goes under ``image``, a list under ``image[]``
    (~1015-1025).
    """
    data = {"model": IMAGE_MODEL, "prompt": prompt, **({"n": n} if n else {}), "size": WEBUI_SIZE,
            **({"background": background} if background else {}), "response_format": "b64_json"}
    return multipart_form([(key, str(value)) for key, value in data.items()], images)


async def form_bytes(form: aiohttp.FormData) -> tuple[bytes, str]:
    payload = form()

    class Sink:
        def __init__(self):
            self.data = bytearray()

        async def write(self, chunk):
            self.data.extend(chunk)

    sink = Sink()
    await payload.write(sink)
    return bytes(sink.data), payload.content_type


def in_chunks(body: bytes):
    """An async body aiohttp sends with ``Transfer-Encoding: chunked`` and no length."""
    async def generate():
        for start in range(0, len(body), 1024):
            yield body[start:start + 1024]
    return generate()


def data_url(content_type: str, data: bytes) -> str:
    return f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}"


def images_reply(**changes):
    envelope = json.loads(json.dumps(GEN_B64["body"]))
    envelope.update(changes)
    for key, value in list(envelope.items()):
        if value is None:
            del envelope[key]
    body = json.dumps(envelope).encode("utf-8")
    return lambda handler: send(handler, 200, body)


def image_item_reply(item):
    return images_reply(data=[item])


def send_chunked(handler, body: bytes):
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()
    try:
        for start in range(0, len(body), 8192):
            piece = body[start:start + 8192]
            handler.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
        handler.wfile.write(b"0\r\n\r\n")
    except (BrokenPipeError, ConnectionResetError):
        pass  # the adapter abandoned the read


class ImageCase(AdapterCase):
    async def post(self, path: str, *, token: str | None = ADAPTER_TOKEN, json_body=None,
                   form: aiohttp.FormData | None = None, data=None, content_type: str | None = None):
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        if content_type is not None:
            headers["Content-Type"] = content_type
        arguments = {"json": json_body} if json_body is not None else {
            "data": form if form is not None else data}
        async with aiohttp.ClientSession() as session:
            async with session.post(self.adapter_url + path, headers=headers, **arguments) as response:
                payload = await response.read()
        self.bodies.append(payload)
        return response.status, response.content_type, payload

    async def generate(self, **extra):
        return await self.post(GENERATIONS_PATH, json_body=webui_generation_payload(**extra))

    def assert_images(self, result, b64_json: str, created: int):
        status, content_type, payload = result
        self.assertEqual(status, 200, payload)
        self.assertEqual(content_type, "application/json")
        self.assertEqual(json.loads(payload), {"created": created, "data": [{"b64_json": b64_json}]})

    def assert_upstream_responses_call(self, expected: dict):
        self.assertEqual(len(self.upstream.requests), 1)
        sent = self.upstream.requests[0]
        self.assertEqual((sent["method"], sent["path"]), ("POST", "/v1/responses"))
        self.assertEqual(sent["headers"]["Authorization"], f"Bearer {PROVIDER_KEY}")
        upstream = self.upstream_json()
        self.assertEqual(upstream, expected)
        self.assertNotIn("n", upstream)
        self.assertNotIn("prompt", upstream)


class ImageAuthTests(ImageCase):
    async def test_missing_or_wrong_token_is_401_on_both_image_routes(self):
        self.upstream.reply = replay(GEN_B64)
        for case, token in (("missing", None), ("wrong", "fake-wrong-token"), ("empty", "")):
            for route in ("generations", "edits"):
                with self.subTest(case=case, route=route):
                    if route == "generations":
                        result = await self.post(GENERATIONS_PATH, token=token,
                                                 json_body=webui_generation_payload())
                    else:
                        result = await self.post(EDITS_PATH, token=token, form=webui_edit_form(
                            EDIT_PROMPT, [("image[]", JPEG, "image/png")]))
                    status, content_type, payload = result
                    self.assertEqual((status, content_type), (401, "application/json"), payload)
                    self.assertIn("error", json.loads(payload))
        self.assertEqual(self.upstream.requests, [])

    async def test_edit_token_is_checked_before_the_body_is_read(self):
        self.upstream.reply = replay(EDIT_B64)
        body, content_type = await form_bytes(webui_edit_form(
            EDIT_PROMPT, [("image[]", JPEG + bytes(8000), "image/png")]))
        with mock.patch.object(self.adapter, "EDIT_BODY_CAP_BYTES", 4096):
            for case, token in (("missing", None), ("wrong", "fake-wrong-token")):
                for shape, data in (("declared", lambda: body), ("chunked", lambda: in_chunks(body))):
                    with self.subTest(case=case, shape=shape):
                        status, _, payload = await self.post(EDITS_PATH, token=token, data=data(),
                                                             content_type=content_type)
                        self.assertEqual(status, 401, payload)
            # Control: the same body with the token crosses the cap.
            status, _, _ = await self.post(EDITS_PATH, data=body, content_type=content_type)
            self.assertEqual(status, 413)
        self.assertEqual(self.upstream.requests, [])


class ImageGenerationTests(ImageCase):
    async def test_webui_payload_sends_the_recorded_request(self):
        self.upstream.reply = replay(GEN_B64)
        result = await self.generate(**EXTRA)
        self.assert_images(result, GEN_B64["body"]["data"][0]["b64_json"], GEN_B64["body"]["created"])
        self.assert_upstream_responses_call({**GEN_B64["request"], **EXTRA})

    async def test_url_response_format_still_returns_b64_json(self):
        self.upstream.reply = replay(GEN_B64)
        payload = {**webui_generation_payload(), "response_format": "url"}
        result = await self.post(GENERATIONS_PATH, json_body=payload)
        self.assert_images(result, GEN_B64["body"]["data"][0]["b64_json"], GEN_B64["body"]["created"])
        self.assert_upstream_responses_call(GEN_B64["request"])

    async def test_without_n_size_or_response_format(self):
        self.upstream.reply = replay(GEN_B64)
        status, _, payload = await self.post(GENERATIONS_PATH, json_body={
            "model": IMAGE_MODEL, "prompt": GEN_PROMPT})
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.upstream_json(), {"model": IMAGE_MODEL, "input": GEN_PROMPT,
                                                "watermark": False, "response_format": "b64_json"})

    async def test_created_falls_back_to_now(self):
        for case, created in (("missing", None), ("not an integer", "1794890108"), ("boolean", True)):
            with self.subTest(case=case):
                self.upstream.reply = images_reply(created=created)
                before = int(time.time())
                status, _, payload = await self.generate()
                after = int(time.time())
                self.assertEqual(status, 200, payload)
                document = json.loads(payload)
                self.assertIs(type(document["created"]), int)
                self.assertTrue(before <= document["created"] <= after, document)

    async def test_png_and_webp_images_are_accepted(self):
        for case, image in (("png", PNG), ("webp", WEBP), ("jpeg", JPEG)):
            with self.subTest(case=case):
                encoded = base64.b64encode(image).decode()
                self.upstream.reply = image_item_reply({"b64_json": encoded, "size": WEBUI_SIZE})
                self.assert_images(await self.generate(), encoded, GEN_B64["body"]["created"])

    async def test_image_routes_contract(self):
        self.assertEqual(self.adapter.MAX_IMAGE_BYTES, 50 * 1024 * 1024)
        self.assertEqual(self.adapter.IMAGE_TIMEOUT_SECONDS, 180)
        self.assertEqual(self.adapter.JSON_BODY_CAP_BYTES, 1024 * 1024)
        self.assertEqual(self.adapter.EDIT_BODY_CAP_BYTES, 64 * 1024 * 1024)


class ImageEditTests(ImageCase):
    async def test_one_image_file_with_n_and_background(self):
        self.upstream.reply = replay(EDIT_B64)
        result = await self.post(EDITS_PATH, form=webui_edit_form(
            EDIT_PROMPT, [("image", PNG, "image/png")], n=1, background="transparent"))
        self.assert_images(result, EDIT_B64["body"]["data"][0]["b64_json"], EDIT_B64["body"]["created"])
        self.assert_upstream_responses_call({**EDIT_B64["request"], "image": [data_url("image/png", PNG)]})
        self.assertNotIn("background", self.upstream_json())

    async def test_two_image_list_files_of_different_types_keep_their_order(self):
        self.upstream.reply = replay(EDIT_B64)
        result = await self.post(EDITS_PATH, form=webui_edit_form(
            EDIT_PROMPT, [("image[]", WEBP, "image/webp"), ("image[]", JPEG, "image/jpeg")]))
        self.assert_images(result, EDIT_B64["body"]["data"][0]["b64_json"], EDIT_B64["body"]["created"])
        self.assert_upstream_responses_call({**EDIT_B64["request"], "image": [
            data_url("image/webp", WEBP), data_url("image/jpeg", JPEG)]})

    async def test_model_tool_form_sends_the_recorded_edit(self):
        # The edit_image tool (open-webui tools/builtin.py ~477, utils/middleware.py ~1825)
        # passes a list without n or background; WebUI stored the generated image as image/png
        # (routers/images.py ~520-522), so JPEG bytes go out declared image/png, as recorded.
        self.upstream.reply = replay(EDIT_B64)
        result = await self.post(EDITS_PATH, form=webui_edit_form(
            EDIT_PROMPT, [("image[]", JPEG, "image/png")]))
        self.assert_images(result, EDIT_B64["body"]["data"][0]["b64_json"], EDIT_B64["body"]["created"])
        sent = {**EDIT_B64["request"], "image": [data_url("image/png", JPEG)]}
        self.assert_upstream_responses_call(sent)
        self.assertTrue(self.upstream_json()["image"][0].startswith("data:image/png;base64,/9j/"))


class ImageRequestTests(ImageCase):
    async def test_invalid_generation_requests_are_400_without_upstream_call(self):
        self.upstream.reply = replay(GEN_B64)
        valid = webui_generation_payload()
        cases = (
            ("n 2", {**valid, "n": 2}),
            ("n true", {**valid, "n": True}),
            ("n string", {**valid, "n": "1"}),
            ("n float", {**valid, "n": 1.0}),
            ("missing prompt", {key: value for key, value in valid.items() if key != "prompt"}),
            ("empty prompt", {**valid, "prompt": ""}),
            ("empty model", {**valid, "model": ""}),
            ("non-string size", {**valid, "size": 2048}),
            ("response_format png", {**valid, "response_format": "png"}),
            ("response_format list", {**valid, "response_format": ["b64_json"]}),
            ("response_format object", {**valid, "response_format": {}}),
            ("extra watermark", {**valid, "watermark": False}),
            ("extra input", {**valid, "input": GEN_PROMPT}),
            ("extra image", {**valid, "image": [data_url("image/png", PNG)]}),
            ("not an object", [valid]),
        )
        for case, body in cases:
            with self.subTest(case=case):
                status, content_type, payload = await self.post(GENERATIONS_PATH, json_body=body)
                self.assertEqual((status, content_type), (400, "application/json"), payload)
                self.assertIn("error", json.loads(payload))
        with self.subTest(case="not JSON"):
            status, _, _ = await self.post(GENERATIONS_PATH, data=b"{not json",
                                           content_type="application/json")
            self.assertEqual(status, 400)
        self.assertEqual(self.upstream.requests, [])

    async def test_invalid_edit_requests_are_400_without_upstream_call(self):
        self.upstream.reply = replay(EDIT_B64)
        text = [("model", IMAGE_MODEL), ("prompt", EDIT_PROMPT), ("size", WEBUI_SIZE),
                ("response_format", "b64_json")]
        image = [("image[]", PNG, "image/png")]

        def without(name):
            return [field for field in text if field[0] != name]

        cases = (
            ("n 2", multipart_form(text + [("n", "2")], image)),
            ("n true", multipart_form(text + [("n", "true")], image)),
            ("missing prompt", multipart_form(without("prompt"), image)),
            ("empty prompt", multipart_form(without("prompt") + [("prompt", "")], image)),
            ("missing model", multipart_form(without("model"), image)),
            ("response_format png", multipart_form(without("response_format")
                                                   + [("response_format", "png")], image)),
            ("unknown field", multipart_form(text + [("seed", "42")], image)),
            ("watermark field", multipart_form(text + [("watermark", "false")], image)),
            ("duplicate prompt", multipart_form(text + [("prompt", "again")], image)),
            ("no files", multipart_form(text, [])),
            ("urlencoded, no files", aiohttp.FormData(text)),
            ("text/plain file", multipart_form(text, [("image[]", b"hello", "text/plain")])),
            ("empty file", multipart_form(text, [("image[]", b"", "image/png")])),
            ("file under another name", multipart_form(text, [("mask", PNG, "image/png")])),
            ("image as a text field", multipart_form(text + [("image", data_url("image/png", PNG))], [])),
        )
        for case, form in cases:
            with self.subTest(case=case):
                status, content_type, payload = await self.post(EDITS_PATH, form=form)
                self.assertEqual((status, content_type), (400, "application/json"), payload)
                self.assertIn("error", json.loads(payload))
        for case, data, content_type in (
                ("JSON body", json.dumps({"model": IMAGE_MODEL, "prompt": EDIT_PROMPT}).encode(),
                 "application/json"),
                ("no boundary", b"--x\r\n", "multipart/form-data"),
                ("broken multipart", b"garbage", "multipart/form-data; boundary=fake-boundary")):
            with self.subTest(case=case):
                status, _, payload = await self.post(EDITS_PATH, data=data, content_type=content_type)
                self.assertEqual(status, 400, payload)
        self.assertEqual(self.upstream.requests, [])


class InboundCapTests(ImageCase):
    JSON_CAP = 2048
    EDIT_CAP = 8192

    def setUp(self):
        super().setUp()
        for name, value in (("JSON_BODY_CAP_BYTES", self.JSON_CAP), ("EDIT_BODY_CAP_BYTES", self.EDIT_CAP)):
            patcher = mock.patch.object(adapter_module(), name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def bodies_over(self) -> dict:
        speech = json.dumps({"model": MODEL, "input": "x" * self.JSON_CAP, "response_format": "mp3"})
        generation = json.dumps(webui_generation_payload(pad="x" * self.JSON_CAP))
        edit, edit_type = await form_bytes(webui_edit_form(
            EDIT_PROMPT, [("image[]", PNG + bytes(self.EDIT_CAP), "image/png")]))
        return {SPEECH_PATH: (speech.encode(), "application/json"),
                GENERATIONS_PATH: (generation.encode(), "application/json"),
                EDITS_PATH: (edit, edit_type)}

    async def test_declared_and_chunked_bodies_over_each_cap_are_413(self):
        self.upstream.reply = replay(GEN_B64)
        for path, (body, content_type) in (await self.bodies_over()).items():
            for shape, data in (("declared", lambda: body), ("chunked", lambda: in_chunks(body))):
                with self.subTest(path=path, shape=shape):
                    status, response_type, payload = await self.post(
                        path, data=data(), content_type=content_type)
                    self.assertEqual((status, response_type), (413, "application/json"), payload)
                    self.assertIn("error", json.loads(payload))
        self.assertEqual(self.upstream.requests, [])

    async def test_bodies_within_each_cap_pass(self):
        # Padded JSON to exactly the JSON cap; an edit above the JSON cap but within its own.
        speech = json.dumps({"model": MODEL, "input": TEXT, "response_format": "mp3"}).encode()
        speech += b" " * (self.JSON_CAP - len(speech))
        self.upstream.reply = replay(MP3)
        for shape, data in (("declared", lambda: speech), ("chunked", lambda: in_chunks(speech))):
            with self.subTest(path=SPEECH_PATH, shape=shape):
                status, _, payload = await self.post(SPEECH_PATH, data=data(),
                                                     content_type="application/json")
                self.assertEqual((status, payload), (200, audio_of(MP3)))
        edit, edit_type = await form_bytes(webui_edit_form(
            EDIT_PROMPT, [("image[]", PNG + bytes(self.JSON_CAP * 2), "image/png")]))
        self.assertGreater(len(edit), self.JSON_CAP)
        self.assertLess(len(edit), self.EDIT_CAP)
        self.upstream.reply = replay(EDIT_B64)
        for shape, data in (("declared", lambda: edit), ("chunked", lambda: in_chunks(edit))):
            with self.subTest(path=EDITS_PATH, shape=shape):
                status, _, payload = await self.post(EDITS_PATH, data=data(), content_type=edit_type)
                self.assertEqual(status, 200, payload)


class ImageFailureTests(ImageCase):
    async def test_recorded_bad_size_is_502_with_the_upstream_message(self):
        for route in ("generations", "edits"):
            with self.subTest(route=route):
                self.upstream.reply = replay(BAD_SIZE)
                if route == "generations":
                    result = await self.post(GENERATIONS_PATH, json_body={
                        **webui_generation_payload(), "prompt": BAD_SIZE["request"]["input"],
                        "size": BAD_SIZE["request"]["size"]})
                else:
                    result = await self.post(EDITS_PATH, form=webui_edit_form(
                        EDIT_PROMPT, [("image[]", JPEG, "image/png")]))
                # error.param is empty, so the reason is error.message.
                expected = f"upstream HTTP 502: {BAD_SIZE['body']['error']['message']}"[:ERROR_CHARS]
                self.assert_502(result, equals=expected)
                self.assertIn(b"InvalidParameter", result[2])
        self.assertEqual(self.upstream_json(0), BAD_SIZE["request"])

    async def test_recorded_url_item_is_502(self):
        for route in ("generations", "edits"):
            with self.subTest(route=route):
                self.upstream.reply = replay(GEN_URL)
                if route == "generations":
                    result = await self.generate()
                else:
                    result = await self.post(EDITS_PATH, form=webui_edit_form(
                        EDIT_PROMPT, [("image[]", JPEG, "image/png")]))
                self.assert_502(result, contains="b64_json")
                self.assertNotIn(b"url", result[2])
                self.assertNotIn(b"volces", result[2])

    async def test_redirect_is_not_followed(self):
        elsewhere = FakeServer()
        self.addCleanup(elsewhere.close)
        elsewhere.reply = replay(GEN_B64)
        location = elsewhere.origin + "/v1/responses"
        self.upstream.reply = lambda handler: send(handler, 307, b"", "text/plain",
                                                   (("Location", location),))
        self.assert_502(await self.generate(), contains="upstream HTTP 307")
        self.assertEqual(len(self.upstream.requests), 1)
        self.assertEqual(elsewhere.requests, [])

    async def test_timeout_is_502(self):
        def stall(handler):
            self.upstream.release.wait(10)
            send(handler, 200, json.dumps(GEN_B64["body"]).encode())

        self.upstream.reply = stall
        with mock.patch.object(self.adapter, "IMAGE_TIMEOUT_SECONDS", 0.3):
            result = await self.generate()
        self.upstream.release.set()
        self.assert_502(result, equals="upstream timed out after 0.3s")

    async def test_transport_error_names_the_class_only(self):
        unused = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
        unused.close()
        self.adapter_url = await self.serve_adapter(f"http://127.0.0.1:{port}/v1")
        message = self.assert_502(await self.generate(), contains="ClientConnectorError")
        self.assertNotIn("127.0.0.1", message)
        self.assertNotIn(str(port), message)

    async def test_unusable_success_bodies_are_502(self):
        cases = (
            ("non-JSON", lambda handler: send(handler, 200, b"<html>busy</html>", "text/html"),
             "not JSON"),
            ("not an object", lambda handler: send(handler, 200, b"[]"), "no images"),
            ("data missing", images_reply(data=None), "no images"),
            ("data empty", images_reply(data=[]), "no images"),
            ("data not a list", images_reply(data={"b64_json": GEN_B64["body"]["data"][0]["b64_json"]}),
             "no images"),
            ("item not an object", images_reply(data=["abc"]), "b64_json"),
            ("b64_json not a string", image_item_reply({"b64_json": 123}), "b64_json"),
            ("b64_json empty", image_item_reply({"b64_json": ""}), "b64_json"),
            ("url beside an empty b64_json", image_item_reply({"b64_json": "", "url": GEN_URL["body"][
                "data"][0]["url"]}), "b64_json"),
            ("invalid base64", image_item_reply({"b64_json": "not base64!"}), "base64"),
            ("not an image", image_item_reply({"b64_json": base64.b64encode(b"hello world").decode()}),
             "JPEG, PNG or WebP"),
            ("RIFF but not WebP", image_item_reply({"b64_json": base64.b64encode(
                b"RIFF\x00\x00\x00\x00WAVEfmt ").decode()}), "JPEG, PNG or WebP"),
            ("second item a url", images_reply(data=[GEN_B64["body"]["data"][0],
                                                      GEN_URL["body"]["data"][0]]), "b64_json"),
        )
        for case, reply, fragment in cases:
            with self.subTest(case=case):
                self.upstream.reply = reply
                self.assert_502(await self.generate(), contains=fragment)

    async def test_upstream_body_over_the_read_cap_is_abandoned(self):
        limit = 3000
        read_cap = math.ceil(limit * 4 / 3) + 64 * 1024
        oversized = json.dumps({"data": [{"b64_json": "A" * (read_cap + 1)}]}).encode()
        cases = (("chunked", lambda handler: send_chunked(handler, oversized)),
                 ("declared length", lambda handler: send(handler, 200, oversized)))
        with mock.patch.object(self.adapter, "MAX_IMAGE_BYTES", limit):
            for case, reply in cases:
                with self.subTest(case=case):
                    self.upstream.reply = reply
                    self.assert_502(await self.generate(), equals=f"upstream body exceeds {read_cap} bytes")

    async def test_decoded_image_over_the_cap_is_refused(self):
        limit = 3000
        with mock.patch.object(self.adapter, "MAX_IMAGE_BYTES", limit):
            at_limit = b"\xff\xd8\xff" + bytes(limit - 3)
            encoded = base64.b64encode(at_limit).decode()
            self.upstream.reply = image_item_reply({"b64_json": encoded})
            self.assert_images(await self.generate(), encoded, GEN_B64["body"]["created"])
            self.upstream.reply = image_item_reply({"b64_json": base64.b64encode(at_limit + b"\x00").decode()})
            self.assert_502(await self.generate(), equals=f"decoded image exceeds {limit} bytes")

    async def test_echoed_secrets_are_redacted(self):
        self.upstream.reply = lambda handler: send(handler, 401, json.dumps(
            {"error": {"message": f"bad key {PROVIDER_KEY} and {ADAPTER_TOKEN}", "param": ""}}).encode())
        self.assert_502(await self.generate(),
                        equals=f"upstream HTTP 401: bad key {REDACTED} and {REDACTED}")


class CatchAllTests(ImageCase):
    async def test_an_unexpected_exception_is_a_redacted_502_naming_the_class(self):
        self.upstream.reply = replay(GEN_B64)
        calls = (
            (SPEECH_PATH, lambda: self.call(body={"model": MODEL, "input": TEXT, "response_format": "mp3"})),
            (GENERATIONS_PATH, lambda: self.generate()),
            (EDITS_PATH, lambda: self.post(EDITS_PATH, form=webui_edit_form(
                EDIT_PROMPT, [("image[]", JPEG, "image/png")]))),
        )
        failure = mock.AsyncMock(side_effect=LookupError(f"boom {PROVIDER_KEY} {ADAPTER_TOKEN}"))
        with mock.patch.object(self.adapter, "post_upstream", failure):
            for path, call in calls:
                with self.subTest(path=path):
                    self.capture.lines.clear()
                    status, content_type, payload = await call()
                    self.assertEqual((status, content_type), (502, "application/json"), payload)
                    self.assertEqual(json.loads(payload), {"error": "adapter error (LookupError)"})
                    logs = "\n".join(self.capture.lines)
                    self.assertNotIn("Traceback", logs)
                    self.assertNotIn("boom", logs)
                    adapter_lines = [line for line in self.capture.lines
                                     if line.startswith(self.adapter.LOGGER_NAME + " ")]
                    self.assertEqual(len(adapter_lines), 1, adapter_lines)
                    self.assertIn(f"route={path} status=502", adapter_lines[0])
        self.assertEqual(failure.await_count, 3)
        self.assertEqual(self.upstream.requests, [])

    async def test_exc_info_records_are_redacted(self):
        for name in (self.adapter.LOGGER_NAME, "uvicorn.error"):
            capture = Capture()
            capture.setFormatter(logging.Formatter("%(message)s"))
            logger = logging.getLogger(name)
            logger.addHandler(capture)
            self.addCleanup(logger.removeHandler, capture)
            for shape in ("exception", "exc_info tuple"):
                with self.subTest(logger=name, shape=shape):
                    capture.lines.clear()
                    try:
                        raise ValueError(f"saw {PROVIDER_KEY} and {ADAPTER_TOKEN}")
                    except ValueError as exc:
                        if shape == "exception":
                            logger.exception("failed")
                        else:
                            logger.error("failed", exc_info=(type(exc), exc, exc.__traceback__))
                    self.assertEqual(len(capture.lines), 1)
                    line = capture.lines[0]
                    self.assertTrue(line.startswith("failed\nTraceback"), line)
                    self.assertIn(f"ValueError: saw {REDACTED} and {REDACTED}", line)
                    for secret in (PROVIDER_KEY, ADAPTER_TOKEN):
                        self.assertNotIn(secret, line)


class ImageLogTests(ImageCase):
    async def test_image_route_access_and_adapter_lines_hold_no_secret_or_prompt(self):
        access = Capture()
        access.setFormatter(uvicorn.logging.AccessFormatter(
            '%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False))
        logging.getLogger("uvicorn.access").addHandler(access)
        self.addCleanup(logging.getLogger("uvicorn.access").removeHandler, access)
        self.upstream.reply = replay(GEN_B64)
        query = f"?k={PROVIDER_KEY}&t={ADAPTER_TOKEN}"
        status, _, _ = await self.post(GENERATIONS_PATH + query, json_body=webui_generation_payload())
        self.assertEqual(status, 200)
        self.upstream.reply = replay(BAD_SIZE)
        status, _, _ = await self.post(EDITS_PATH + query, form=webui_edit_form(
            EDIT_PROMPT, [("image[]", JPEG, "image/png")]))
        self.assertEqual(status, 502)
        expected = (f'"POST {GENERATIONS_PATH}?k={REDACTED}&t={REDACTED} HTTP/1.1" 200',
                    f'"POST {EDITS_PATH}?k={REDACTED}&t={REDACTED} HTTP/1.1" 502')
        self.assertEqual(len(access.lines), 2, access.lines)
        for line, request_line in zip(access.lines, expected):
            self.assertIn(request_line, line)
        adapter_lines = [line for line in self.capture.lines
                         if line.startswith(self.adapter.LOGGER_NAME + " ")]
        self.assertEqual(len(adapter_lines), 2, adapter_lines)
        for line, path, status, upstream in zip(adapter_lines, (GENERATIONS_PATH, EDITS_PATH),
                                                ("200", "502"), ("200", "502")):
            self.assertIn(f"route={path} status={status}", line)
            self.assertIn(f"upstream={upstream}", line)
            self.assertRegex(line, r"elapsed_ms=\d+")
        logs = "\n".join(self.capture.lines + access.lines)
        for private in (GEN_PROMPT, EDIT_PROMPT, "InvalidParameter", "Bearer", "/9j/"):
            self.assertNotIn(private, logs)


if __name__ == "__main__":
    unittest.main()
