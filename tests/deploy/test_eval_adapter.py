# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""The ocu-eval adapter's speech route against recorded dmxapi responses (#817).

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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
import os
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest import mock

import aiohttp
import uvicorn

ROOT = Path(__file__).resolve().parents[2]
ADAPTER_FILE = ROOT / "deploy" / "eval" / "adapter" / "eval_adapter.py"
SPEECH_FILE = ROOT / "computer-use-server" / "speech.py"
RECORDINGS = json.loads((Path(__file__).resolve().parent / "fixtures" / "eval_adapter"
                         / "dmxapi-tts-recorded.json").read_text(encoding="utf-8"))
MP3 = RECORDINGS["mp3_with_instructions_and_voice"]
WAV = RECORDINGS["wav_default_voice"]
UNKNOWN_VOICE = RECORDINGS["unknown_voice_400"]
UNKNOWN_MODEL = RECORDINGS["unknown_model"]
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


if __name__ == "__main__":
    unittest.main()
