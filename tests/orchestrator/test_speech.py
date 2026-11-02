# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""generate_speech against a local stub of the OpenAI-compatible speech route (#703).

Expected values come from the ocu-video-production design, section Speech: the
request body and headers, the accepted content types per format, the 50 MB cap,
redirect refusal, key redaction, the tombstone refusal, the write through the
upload store and the TTS_* startup table.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = ROOT / "computer-use-server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from context_vars import current_chat_id  # noqa: E402

CHAT = "chat-speech"
KEY = "fake-tts-key-0123456789"
AUDIO = b"ID3\x04fake-mp3-frames" * 8
TTS_NAMES = ("TTS_BASE_URL", "TTS_MODEL", "TTS_VOICE", "TTS_API_KEY", "TTS_TIMEOUT_SECONDS")
_INTERPRETER_ENV = ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "PATH", "SYSTEMROOT")


class Stub:
    """A real local HTTP server whose /v1/audio/speech answer each test chooses."""

    def __init__(self, respond):
        self.respond = respond
        self.requests = []

    async def speech(self, request):
        self.requests.append({
            "path": request.path,
            "headers": dict(request.headers),
            "json": await request.json(),
        })
        return await self.respond(request)

    @staticmethod
    async def ok(_request):
        return web.Response(body=b"redirected-audio", content_type="audio/mpeg")


def _audio(content_type, body=AUDIO):
    async def respond(_request):
        return web.Response(body=body, headers={"Content-Type": content_type})
    return respond


async def _call(world, respond, **arguments):
    stub = Stub(respond)
    app = web.Application()
    app.router.add_post("/v1/audio/speech", stub.speech)
    app.router.add_post("/elsewhere", stub.ok)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    try:
        base = str(server.make_url("/v1"))
        world.base = base
        world.origin = str(server.make_url(""))
        if world.configure:
            os.environ.setdefault("TTS_BASE_URL", base)
        current_chat_id.set(CHAT)
        result = await world.tools.generate_speech(**arguments)
    finally:
        await server.close()
    return result, stub


@pytest.fixture
def world(tmp_path, monkeypatch):
    import mcp_tools

    docker_manager = sys.modules["docker_manager"]
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(mcp_tools.uploads, "BASE_DATA_DIR", data)
    monkeypatch.setattr(docker_manager, "BASE_DATA_DIR", data)
    for name in TTS_NAMES:
        # setenv first so teardown restores the original state even after a
        # scenario writes os.environ directly.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    monkeypatch.setenv("TTS_MODEL", "breeze-tts-2")
    return SimpleNamespace(tools=mcp_tools, data=data, configure=True, base=None, origin=None)


def _outputs(world):
    root = world.data / CHAT / "outputs"
    if not root.exists():
        return {}
    return {path.name: path.read_bytes() for path in root.iterdir()}


def run(world, respond, **arguments):
    arguments.setdefault("text", "你好，欢迎收看。")
    arguments.setdefault("filename", "line1.mp3")
    return asyncio.run(_call(world, respond, **arguments))


# ---------------------------------------------------------------------------
# Successful synthesis
# ---------------------------------------------------------------------------


def test_voiceover_line_sends_the_contract_and_writes_the_file(world, monkeypatch):
    import mcp_resources

    monkeypatch.setenv("TTS_API_KEY", KEY)
    monkeypatch.setenv("TTS_VOICE", "server-default")
    result, stub = run(
        world, _audio("audio/mpeg"),
        text="第一句旁白", filename="line1.mp3",
        instructions="a calm female narrator", voice="narrator-a",
    )

    assert result == f"Saved speech to /mnt/user-data/files/line1.mp3 ({len(AUDIO)} bytes)"
    [request] = stub.requests
    assert request["path"] == "/v1/audio/speech"
    assert request["json"] == {
        "model": "breeze-tts-2",
        "input": "第一句旁白",
        "response_format": "mp3",
        "voice": "narrator-a",
        "instructions": "a calm female narrator",
    }
    assert request["headers"]["Authorization"] == f"Bearer {KEY}"
    assert _outputs(world) == {"line1.mp3": AUDIO}
    # The same resource sync uploads run publishes the new file.
    assert any("line1.mp3" in uri for uri in mcp_resources._chat_uris.get(CHAT, ()))


@pytest.mark.parametrize("content_type", ("audio/wav", "audio/x-wav", "audio/x-wav; charset=binary"))
def test_wav_omits_empty_instructions_uses_the_default_voice_and_no_key(world, monkeypatch, content_type):
    monkeypatch.setenv("TTS_VOICE", "server-default")
    result, stub = run(world, _audio(content_type), filename="intro.wav")

    assert result == f"Saved speech to /mnt/user-data/files/intro.wav ({len(AUDIO)} bytes)"
    [request] = stub.requests
    assert request["json"] == {
        "model": "breeze-tts-2",
        "input": "你好，欢迎收看。",
        "response_format": "wav",
        "voice": "server-default",
    }
    assert "Authorization" not in request["headers"]
    assert _outputs(world) == {"intro.wav": AUDIO}


def test_no_voice_anywhere_sends_no_voice(world):
    _result, stub = run(world, _audio("audio/mpeg"), instructions="warm")
    [request] = stub.requests
    assert "voice" not in request["json"]
    assert request["json"]["instructions"] == "warm"


def test_trailing_slash_on_the_base_url_is_not_doubled(world, monkeypatch):
    world.configure = False

    async def scenario():
        stub = Stub(_audio("audio/mpeg"))
        app = web.Application()
        app.router.add_post("/v1/audio/speech", stub.speech)
        server = TestServer(app, host="127.0.0.1")
        await server.start_server()
        try:
            os.environ["TTS_BASE_URL"] = str(server.make_url("/v1")) + "/"
            current_chat_id.set(CHAT)
            result = await world.tools.generate_speech(text="hi", filename="a.mp3")
        finally:
            await server.close()
        return result, stub

    result, stub = asyncio.run(scenario())
    assert result.startswith("Saved speech to /mnt/user-data/files/a.mp3")
    assert [r["path"] for r in stub.requests] == ["/v1/audio/speech"]


def test_an_existing_name_gets_the_collision_suffix(world):
    root = world.data / CHAT / "outputs"
    root.mkdir(parents=True)
    (root / "line1.mp3").write_bytes(b"older line")

    result, _stub = run(world, _audio("audio/mpeg"))

    assert result == f"Saved speech to /mnt/user-data/files/line1 (2).mp3 ({len(AUDIO)} bytes)"
    assert _outputs(world) == {"line1.mp3": b"older line", "line1 (2).mp3": AUDIO}


# ---------------------------------------------------------------------------
# Refusals before the network
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", (None, ""), ids=("unset", "empty"))
def test_not_configured_makes_no_network_call(world, monkeypatch, value):
    world.configure = False
    if value is not None:
        monkeypatch.setenv("TTS_BASE_URL", value)
    result, stub = run(world, _audio("audio/mpeg"))

    assert result == "Error: speech synthesis is not configured"
    assert stub.requests == []
    assert _outputs(world) == {}


@pytest.mark.parametrize(
    "filename",
    ("../line.mp3", "sub/line.mp3", "/abs/line.mp3", "line\x00.mp3", "line.ogg", "line", "line.mp3.txt", ""),
)
def test_invalid_filename_is_an_error_result_without_network_or_write(world, filename):
    result, stub = run(world, _audio("audio/mpeg"), filename=filename)

    assert result.startswith("Error:"), result
    assert stub.requests == []
    assert not (world.data / CHAT).exists()


def test_upload_name_rejection_becomes_an_error_result(world):
    """The upload-name check's HTTPException(403) maps to the wrapper's Error: prefix."""
    result, stub = run(world, _audio("audio/mpeg"), filename="..\x00.mp3")

    assert result == "Error: Invalid upload path"
    assert stub.requests == []


def test_store_traversal_refusal_becomes_an_error_result(world, tmp_path):
    """A symlinked outputs directory makes the upload store raise HTTPException(403)."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    chat_dir = world.data / CHAT
    chat_dir.mkdir()
    (chat_dir / "outputs").symlink_to(elsewhere)

    result, stub = run(world, _audio("audio/mpeg"))

    assert result == "Error: Access denied: path traversal detected"
    assert len(stub.requests) == 1
    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize("text", ("", "x" * 2001), ids=("empty", "2001-chars"))
def test_text_outside_one_to_2000_characters_is_refused(world, text):
    result, stub = run(world, _audio("audio/mpeg"), text=text)

    assert result.startswith("Error:") and "2000" in result
    assert stub.requests == []
    assert _outputs(world) == {}


def test_text_of_exactly_2000_characters_is_accepted(world):
    result, stub = run(world, _audio("audio/mpeg"), text="字" * 2000)

    assert result.startswith("Saved speech to")
    assert stub.requests[0]["json"]["input"] == "字" * 2000


# ---------------------------------------------------------------------------
# Upstream failures write nothing and never show the key or the URL
# ---------------------------------------------------------------------------


async def _redirect(_request):
    raise web.HTTPFound("/elsewhere")


async def _server_error(_request):
    return web.Response(status=500, text=f"upstream exploded; got Authorization: Bearer {KEY}")


async def _oversize_streamed(request):
    response = web.StreamResponse(headers={"Content-Type": "audio/mpeg"})
    response.enable_chunked_encoding()
    await response.prepare(request)
    for _ in range(8):
        await response.write(b"\0" * 1024)
    await response.write_eof()
    return response


async def _oversize_declared(_request):
    return web.Response(body=b"\0" * 8192, content_type="audio/mpeg")


async def _slow(_request):
    await asyncio.sleep(2.5)
    return web.Response(body=AUDIO, content_type="audio/mpeg")


@pytest.mark.parametrize(
    ("respond", "expected"),
    (
        (_redirect, "HTTP 302"),
        (_server_error, "HTTP 500"),
        (_audio("text/html; charset=utf-8", b"<html>login</html>"), "text/html"),
        (_audio("audio/wav"), "audio/wav"),
        (_audio("audio/mpeg", b""), "no audio"),
        (_oversize_streamed, "exceeds"),
        (_oversize_declared, "exceeds"),
        (_slow, "timed out"),
    ),
    ids=("redirect", "500", "html", "wav-for-mp3", "empty", "oversize-streamed",
         "oversize-declared", "timeout"),
)
def test_upstream_failure_writes_nothing_and_hides_the_key(world, monkeypatch, caplog, respond, expected):
    import speech

    monkeypatch.setenv("TTS_API_KEY", KEY)
    monkeypatch.setenv("TTS_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr(speech, "MAX_AUDIO_BYTES", 4096)
    with caplog.at_level(logging.WARNING, logger="ocu.speech"):
        result, stub = run(world, respond)

    assert result.startswith("Error:"), result
    assert expected in result, result
    assert KEY not in result
    assert world.base not in result and world.origin not in result
    assert len(stub.requests) == 1
    assert not (world.data / CHAT / "outputs").exists() or _outputs(world) == {}
    assert KEY not in caplog.text


def test_echoed_key_is_redacted_from_the_result_and_the_log(world, monkeypatch, caplog):
    monkeypatch.setenv("TTS_API_KEY", KEY)
    with caplog.at_level(logging.WARNING, logger="ocu.speech"):
        result, _stub = run(world, _server_error)

    assert result.startswith("Error: speech synthesis failed: HTTP 500")
    assert "upstream exploded" in result
    assert "[redacted]" in result
    assert KEY not in result
    assert "upstream exploded" in caplog.text
    assert KEY not in caplog.text


def test_error_body_is_cut_to_200_characters_after_redaction(world, monkeypatch):
    monkeypatch.setenv("TTS_API_KEY", KEY)
    body = KEY + "y" * 1000

    async def long_error(_request):
        return web.Response(status=503, text=body)

    result, _stub = run(world, long_error)

    prefix = "Error: speech synthesis failed: HTTP 503: "
    assert result.startswith(prefix)
    snippet = result[len(prefix):]
    assert snippet == ("[redacted]" + "y" * 1000)[:200]


def test_redirect_target_is_never_requested(world):
    calls = []

    async def counted_ok(_request):
        calls.append(True)
        return web.Response(body=AUDIO, content_type="audio/mpeg")

    async def scenario():
        stub = Stub(_redirect)
        app = web.Application()
        app.router.add_post("/v1/audio/speech", stub.speech)
        app.router.add_post("/elsewhere", counted_ok)
        app.router.add_get("/elsewhere", counted_ok)
        server = TestServer(app, host="127.0.0.1")
        await server.start_server()
        try:
            os.environ["TTS_BASE_URL"] = str(server.make_url("/v1"))
            current_chat_id.set(CHAT)
            return await world.tools.generate_speech(text="hi", filename="a.mp3")
        finally:
            await server.close()

    result = asyncio.run(scenario())
    assert result.startswith("Error: speech synthesis failed: HTTP 302")
    assert calls == []
    assert _outputs(world) == {}


# ---------------------------------------------------------------------------
# Deleted chats
# ---------------------------------------------------------------------------


def test_tombstoned_chat_is_refused_and_nothing_is_written(world):
    chat_dir = world.data / CHAT
    chat_dir.mkdir()
    (chat_dir / ".deleted").write_bytes(b"")

    result, _stub = run(world, _audio("audio/mpeg"))

    assert result == "Error: chat was deleted"
    assert set(os.listdir(chat_dir)) <= {".deleted", ".lifecycle.lock"}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_configuration_defaults_and_values(monkeypatch):
    import speech

    for name in TTS_NAMES:
        monkeypatch.delenv(name, raising=False)
    assert speech.configuration() is None
    monkeypatch.setenv("TTS_BASE_URL", "https://tts.lan/v1")
    monkeypatch.setenv("TTS_MODEL", "breeze-tts-2")
    config = speech.configuration()
    assert (config.base_url, config.model, config.voice, config.api_key, config.timeout) == (
        "https://tts.lan/v1", "breeze-tts-2", "", "", 120,
    )
    monkeypatch.setenv("TTS_TIMEOUT_SECONDS", "300")
    monkeypatch.setenv("TTS_VOICE", "v1")
    monkeypatch.setenv("TTS_API_KEY", KEY)
    config = speech.configuration()
    assert (config.voice, config.api_key, config.timeout) == ("v1", KEY, 300)
    assert KEY not in repr(config)


@pytest.mark.parametrize(
    "environment",
    (
        {"TTS_BASE_URL": "ftp://tts.lan/v1", "TTS_MODEL": "m"},
        {"TTS_BASE_URL": "tts.lan/v1", "TTS_MODEL": "m"},
        {"TTS_BASE_URL": "http://", "TTS_MODEL": "m"},
        {"TTS_BASE_URL": "http://tts.lan/v1"},
        {"TTS_BASE_URL": "http://tts.lan/v1", "TTS_MODEL": ""},
        {"TTS_TIMEOUT_SECONDS": ""},
        {"TTS_TIMEOUT_SECONDS": "0"},
        {"TTS_TIMEOUT_SECONDS": "-1"},
        {"TTS_TIMEOUT_SECONDS": "1.5"},
        {"TTS_TIMEOUT_SECONDS": "abc"},
    ),
    ids=("ftp", "no-scheme", "no-host", "model-unset", "model-empty", "timeout-empty",
         "timeout-zero", "timeout-negative", "timeout-fraction", "timeout-text"),
)
def test_invalid_configuration_is_refused(monkeypatch, environment):
    import speech

    for name in TTS_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(speech.SpeechConfigError) as refused:
        speech.configuration()
    assert next(iter(environment)) in str(refused.value)


_LIFESPAN_SCRIPT = textwrap.dedent(
    """
    import json
    import os
    import sys
    import docker as docker_mod
    from fastapi.testclient import TestClient

    def fail(*_args, **_kwargs):
        raise AssertionError("real docker client constructed")

    docker_mod.DockerClient = fail
    sys.path.insert(0, os.environ["OCU_SERVER_DIR"])
    import app
    import speech

    app.startup_preflight = lambda: 0
    app.startup_idle_sweep = lambda now=None: None
    app.reap_known_sandboxes = lambda now=None: None
    app.sweep_office_publications = lambda *a, **k: None
    outcomes = {}
    # The MCP session manager runs once per process, so only the last case may start.
    for label, values in json.loads(sys.argv[1]):
        for name in ("TTS_BASE_URL", "TTS_MODEL", "TTS_TIMEOUT_SECONDS"):
            os.environ.pop(name, None)
        os.environ.update(values)
        try:
            with TestClient(app.app):
                outcomes[label] = "started"
        except speech.SpeechConfigError:
            outcomes[label] = "refused"
    print(json.dumps(outcomes))
    """
)


def _lifespan_outcomes(tmp_path, cases):
    completed = subprocess.run(
        [sys.executable, "-c", _LIFESPAN_SCRIPT, json.dumps(cases)],
        cwd=str(SERVER_DIR),
        env={
            **{name: os.environ[name] for name in _INTERPRETER_ENV if name in os.environ},
            "OCU_SERVER_DIR": str(SERVER_DIR),
            "OCU_INTERNAL_TOKEN": "speech-startup-internal-token",
            "MCP_API_KEY": "speech-startup-mcp-key",
            "OCU_WEBUI_ORIGIN": "https://webui.example",
            "PUBLIC_BASE_URL": "/ocu",
            "BASE_DATA_DIR": str(tmp_path / "data"),
            "CONTAINER_IDLE_TIMEOUT": "600",
            "OCU_IDLE_POLL_SECONDS": "30",
            "DOCKER_IMAGE": "python:3.12-slim",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr[-1200:]
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_lifespan_refuses_invalid_tts_configuration_and_starts_configured(tmp_path):
    assert _lifespan_outcomes(tmp_path, [
        ["bad-url", {"TTS_BASE_URL": "ftp://tts.lan/v1", "TTS_MODEL": "m"}],
        ["no-model", {"TTS_BASE_URL": "http://tts.lan/v1"}],
        ["empty-timeout", {"TTS_TIMEOUT_SECONDS": ""}],
        ["zero-timeout", {"TTS_TIMEOUT_SECONDS": "0"}],
        ["configured", {"TTS_BASE_URL": "http://tts.lan/v1", "TTS_MODEL": "m"}],
    ]) == {
        "bad-url": "refused", "no-model": "refused", "empty-timeout": "refused",
        "zero-timeout": "refused", "configured": "started",
    }


def test_lifespan_starts_with_tts_unset(tmp_path):
    assert _lifespan_outcomes(tmp_path, [["unset", {}]]) == {"unset": "started"}


# ---------------------------------------------------------------------------
# Tool surface and sandbox environment
# ---------------------------------------------------------------------------


def test_generate_speech_is_listed_with_its_parameters():
    import mcp_tools

    tools = {tool.name: tool for tool in asyncio.run(mcp_tools.mcp.list_tools())}
    schema = tools["generate_speech"].inputSchema
    assert set(schema["properties"]) == {"text", "filename", "instructions", "voice"}
    assert set(schema["required"]) == {"text", "filename"}
    assert "api_key" not in json.dumps(schema).lower()


def test_tts_key_never_reaches_the_sandbox_environment(monkeypatch):
    import docker_manager

    for name, value in {
        "TTS_BASE_URL": "http://tts.lan/v1", "TTS_MODEL": "m",
        "TTS_VOICE": "v", "TTS_API_KEY": KEY, "TTS_TIMEOUT_SECONDS": "120",
    }.items():
        monkeypatch.setenv(name, value)
    environment = docker_manager._build_container_env({"EXTRA": "1"})
    assert not any(name.startswith("TTS_") for name in environment)
    assert KEY not in json.dumps(environment)
