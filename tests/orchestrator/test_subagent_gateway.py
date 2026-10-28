# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""sub_agent Claude Code gateway (#662): the caller's WebUI token and the chat model.

Seams: the real `mcp_tools.sub_agent` and `cli_runtime.dispatch` with fake container,
`_execute_bash` and `_execute_bash_capture` collaborators; the real `MCPContextMiddleware`;
the real `claude_gateway` builder and `docker_manager._execute_bash_capture` against a fake
container. Expected values are the ocu-subagent-claude-gateway fixture literals (design
evidence a-e, j) and the terminal gateway's existing detail strings, not recomputed from
the implementation.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import logging
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

SERVER_DIR = Path(__file__).resolve().parents[2] / "computer-use-server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from context_vars import (  # noqa: E402
    current_anthropic_auth_token,
    current_anthropic_base_url,
    current_chat_id,
    current_chat_model,
    current_gateway_token,
    current_user_email,
)

ADDRESS = "172.31.0.254"
BASE_URL = "http://172.31.0.254:8080/api"
TOKEN = "tok-1"
OTHER_TOKEN = "tok-2"
CHAT = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
NO_TOKEN = (
    "Claude Code gateway: the request carries no Open WebUI login token; "
    "sign in again and reopen the terminal"
)
NO_MODEL = (
    "Claude Code gateway: the chat has no usable model id (1-256 printable ASCII "
    "characters without spaces); select a model and reopen the terminal"
)
PLAN = "/home/assistant/task_plan.md"
NEW_PROMPT = f"Read and execute your task plan from {PLAN}"
RESUME_PROMPT = f"Continue working on the task. If needed, re-read {PLAN} for full context."
MAX_TURNS = 7
COMMON = [
    "--max-turns", str(MAX_TURNS),
    "--permission-mode", "bypassPermissions",
    "--disallowedTools", "AskUserQuestion,ExitPlanMode",
    "--output-format", "json",
]
CD = "cd /home/assistant && "
RESULT_LINE = json.dumps({
    "type": "result", "result": "sub-agent done", "total_cost_usd": 0.01,
    "num_turns": 2, "is_error": False, "session_id": "s-9",
})
_SCRUB = (
    "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_SUB_AGENT_DEFAULT_MODEL", "CODEX_SUB_AGENT_DEFAULT_MODEL", "OPENCODE_SUB_AGENT_DEFAULT_MODEL",
    "CODEX_MODEL", "OPENCODE_MODEL", "OPENCODE_MODEL_ALIASES", "SUB_AGENT_MAX_TURNS",
)


def expected_environment(model: str, token: str) -> dict:
    return {
        "ANTHROPIC_BASE_URL": BASE_URL,
        "ANTHROPIC_AUTH_TOKEN": token,
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        "CLAUDE_CODE_SUBAGENT_MODEL": model,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
        "OCU_TERMINAL_FINGERPRINT": hashlib.sha256(f"{BASE_URL}\0{model}\0{token}".encode()).hexdigest(),
    }


def argv_of(command: str) -> list[str]:
    assert command.startswith(CD), command
    return shlex.split(command[len(CD):])


class FakeContainer:
    id = "cid-subagent"
    name = "owui-test-subagent"
    labels: dict = {}
    attrs: dict = {}

    def __init__(self, exec_error: bool = False):
        self.exec_error = exec_error
        self.exec_kwargs: list[dict] = []

    def exec_run(self, **kwargs):
        self.exec_kwargs.append(kwargs)
        if self.exec_error:
            raise RuntimeError(f"exec failed with environment {kwargs.get('environment')}")
        return SimpleNamespace(exit_code=0, output=(RESULT_LINE.encode(), b""))


class World:
    def __init__(self, monkeypatch, cli: str, gateway: bool):
        for name in _SCRUB:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("SUBAGENT_CLI", cli)
        if gateway:
            monkeypatch.setenv("OCU_SANDBOX_MESSAGES_ADDRESS", ADDRESS)
        else:
            monkeypatch.delenv("OCU_SANDBOX_MESSAGES_ADDRESS", raising=False)
        for name in ("docker_manager", "cli_runtime", "mcp_tools"):
            if name in sys.modules:
                importlib.reload(sys.modules[name])
            else:
                importlib.import_module(name)
        import cli_runtime
        import docker_manager
        import mcp_tools

        self.dm = docker_manager
        self.runtime = cli_runtime
        self.tools = mcp_tools
        self.container = FakeContainer()
        self.lookups: list[dict] = []
        self.bash: list[str] = []
        self.captures: list[dict] = []
        self.gitlab = AsyncMock(return_value=None)
        self.resolver = MagicMock(wraps=cli_runtime.resolve_subagent_model)

        def get_or_create(chat_id):
            self.lookups.append({
                "chat_id": chat_id,
                "anthropic_token": current_anthropic_auth_token.get(),
                "anthropic_base": current_anthropic_base_url.get(),
                "gateway_token": current_gateway_token.get(),
            })
            return self.container

        def bash(container, command, timeout=None, **kwargs):
            assert kwargs == {}, kwargs
            self.bash.append(command)
            return {"output": "", "exit_code": 0, "success": True}

        def capture(container, command, timeout=None, **kwargs):
            self.captures.append({"command": command, "timeout": timeout, "kwargs": kwargs})
            return SimpleNamespace(stdout=RESULT_LINE, stderr="", returncode=0)

        monkeypatch.setattr(mcp_tools, "_validate_chat_id", lambda: (CHAT, None))
        monkeypatch.setattr(mcp_tools, "_ensure_gitlab_token", self.gitlab)
        monkeypatch.setattr(mcp_tools, "_get_or_create_container", get_or_create)
        monkeypatch.setattr(mcp_tools, "_execute_bash", bash)
        monkeypatch.setattr(mcp_tools, "resolve_subagent_model", self.resolver)
        monkeypatch.setattr(cli_runtime, "resolve_subagent_model", self.resolver)
        monkeypatch.setattr(cli_runtime, "_execute_bash_capture", capture)
        monkeypatch.setattr(mcp_tools.skill_manager, "get_user_skills_sync", lambda *a, **k: [])
        monkeypatch.setattr(mcp_tools.skill_manager, "build_sub_agent_skills_text", lambda *a, **k: "")

    def call(self, *, token=None, chat_model=None, email=None, anthropic_token=None, **kwargs) -> str:
        fn = getattr(self.tools.sub_agent, "fn", None) or self.tools.sub_agent
        ctx = MagicMock()
        ctx.report_progress = AsyncMock()
        arguments = {"task": "write and run hello.py", "description": "delegate",
                     "max_turns": MAX_TURNS, **kwargs}

        async def run():
            current_chat_id.set(CHAT)
            current_user_email.set(email)
            current_gateway_token.set(token)
            current_chat_model.set(chat_model)
            current_anthropic_auth_token.set(anthropic_token)
            return await fn(ctx=ctx, **arguments)

        return asyncio.run(run())

    def texts(self) -> list[str]:
        return self.bash + [capture["command"] for capture in self.captures]


@pytest.fixture
def gateway(monkeypatch):
    return World(monkeypatch, "claude", gateway=True)


@pytest.fixture
def gateway_off(monkeypatch):
    return World(monkeypatch, "claude", gateway=False)


def _assert_secret_absent(world, result, capsys, caplog, *tokens):
    captured = capsys.readouterr()
    for text in [result, captured.out, captured.err, caplog.text, *world.texts()]:
        for token in tokens or (TOKEN,):
            assert token not in text


# --- a: token and chat model bind the call -------------------------------


def test_gateway_call_runs_claude_with_token_and_chat_model_verbatim(gateway, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    result = gateway.call(token=TOKEN, chat_model="m-1", model="opus", email="u@example.test")

    [run] = gateway.captures
    assert run["kwargs"] == {"environment": expected_environment("m-1", TOKEN)}
    tokens = argv_of(run["command"])
    assert tokens[0] == "ANTHROPIC_CUSTOM_HEADERS=x-openwebui-user-email: u@example.test"
    argv = tokens[1:]
    assert argv[:6] == ["claude", "-p", NEW_PROMPT, "--model", "m-1", "--append-system-prompt"]
    assert argv[7:] == COMMON
    assert "--model m-1" in run["command"]
    assert gateway.resolver.call_count == 0
    assert "**Model:** m-1 |" in result
    assert result.startswith("**Sub-Agent Completed** (success)")
    _assert_secret_absent(gateway, result, capsys, caplog)


# --- b: refusals before any sandbox work ----------------------------------


@pytest.mark.parametrize(
    "token, chat_model, detail",
    [
        (None, "m-1", NO_TOKEN),
        ("", "m-1", NO_TOKEN),
        (TOKEN, None, NO_MODEL),
        (TOKEN, "", NO_MODEL),
        (TOKEN, "bad model", NO_MODEL),
        (TOKEN, "m" * 257, NO_MODEL),
    ],
    ids=["no-token", "empty-token", "no-model", "empty-model", "space-model", "long-model"],
)
def test_missing_token_or_model_refuses_before_any_container_work(gateway, capsys, caplog, token, chat_model, detail):
    caplog.set_level(logging.DEBUG)
    result = gateway.call(token=token, chat_model=chat_model, model="opus")
    assert result == f"Error: {detail}"
    assert result.startswith("Error: Claude Code gateway:")
    assert gateway.lookups == []
    assert gateway.bash == []
    assert gateway.captures == []
    assert gateway.gitlab.await_count == 0
    _assert_secret_absent(gateway, result, capsys, caplog)


def test_missing_gateway_token_is_not_replaced_by_other_anthropic_tokens(monkeypatch, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    with monkeypatch.context() as scoped:
        scoped.setenv("ANTHROPIC_AUTH_TOKEN", "srv-canary")
        world = World(scoped, "claude", gateway=True)
        assert world.dm.ANTHROPIC_AUTH_TOKEN == "srv-canary"
        result = world.call(token=None, chat_model="m-1", anthropic_token="req-canary")
    importlib.reload(sys.modules["docker_manager"])
    assert result == f"Error: {NO_TOKEN}"
    assert world.lookups == []
    assert world.bash == []
    assert world.captures == []
    _assert_secret_absent(world, result, capsys, caplog, "srv-canary", "req-canary")


def test_longest_valid_chat_model_is_used(gateway):
    model = "!" + "a" * 254 + "~"
    gateway.call(token=TOKEN, chat_model=model)
    [run] = gateway.captures
    assert run["kwargs"]["environment"]["ANTHROPIC_MODEL"] == model
    assert argv_of(run["command"])[3:5] == ["--model", model]


# --- c: resume follows the current call -----------------------------------


def test_gateway_resume_uses_this_calls_token_and_model(gateway, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    result = gateway.call(token=OTHER_TOKEN, chat_model="m-2", model="sonnet", resume_session_id="s-1")
    [run] = gateway.captures
    assert run["kwargs"] == {"environment": expected_environment("m-2", OTHER_TOKEN)}
    assert argv_of(run["command"]) == ["claude", "-p", RESUME_PROMPT, "--resume", "s-1", "--model", "m-2", *COMMON]
    assert not any("base64 -d" in command for command in gateway.bash)
    assert "**Model:** m-2 |" in result
    _assert_secret_absent(gateway, result, capsys, caplog, OTHER_TOKEN)


def test_resume_without_gateway_has_no_model_flag(gateway_off):
    result = gateway_off.call(token=TOKEN, chat_model="m-2", model="sonnet", resume_session_id="s-1")
    [run] = gateway_off.captures
    assert run["kwargs"] == {}
    assert argv_of(run["command"]) == ["claude", "-p", RESUME_PROMPT, "--resume", "s-1", *COMMON]
    assert "**Model:** sonnet |" in result


# --- d: gateway off keeps today's behaviour -------------------------------


def test_gateway_off_ignores_forwarded_values_and_expands_the_alias(gateway_off, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    result = gateway_off.call(token=TOKEN, chat_model="m-1", model="opus")
    [run] = gateway_off.captures
    assert run["kwargs"] == {}
    argv = argv_of(run["command"])
    assert argv[:6] == ["claude", "-p", NEW_PROMPT, "--model", "claude-opus-4-6", "--append-system-prompt"]
    assert gateway_off.resolver.call_count >= 1
    assert "**Model:** opus |" in result
    _assert_secret_absent(gateway_off, result, capsys, caplog)


def test_codex_with_address_set_keeps_its_own_resolution(monkeypatch, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    world = World(monkeypatch, "codex", gateway=True)
    refused = world.call(token=TOKEN, chat_model="m-1", model="sonnet")
    assert refused.startswith("Sub-agent error: Model alias 'sonnet' is Claude-only")
    assert world.captures == []
    result = world.call(token=TOKEN, chat_model="m-1", model="gpt-5-codex")
    assert world.captures and all(capture["kwargs"] == {} for capture in world.captures)
    main = world.captures[-1]["command"]
    assert "codex" in main and "gpt-5-codex" in main and "m-1" not in main
    _assert_secret_absent(world, refused + result, capsys, caplog)


# --- e: the token stays out of OCU-written text and the container env -----


def test_exec_failure_echoing_the_environment_does_not_leak_the_token(gateway, monkeypatch, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(gateway.runtime, "_execute_bash_capture", gateway.dm._execute_bash_capture)
    gateway.container.exec_error = True
    result = gateway.call(token=TOKEN, chat_model="m-1")
    [exec_call] = gateway.container.exec_kwargs
    assert exec_call["environment"]["ANTHROPIC_AUTH_TOKEN"] == TOKEN
    assert result.startswith("**Sub-Agent Terminated**")
    _assert_secret_absent(gateway, result, capsys, caplog)


def test_container_creation_never_sees_the_gateway_token(gateway):
    gateway.call(token=TOKEN, chat_model="m-1")
    assert gateway.lookups == [{
        "chat_id": CHAT, "anthropic_token": None, "anthropic_base": None, "gateway_token": TOKEN,
    }]


# --- dispatch and exec seams ----------------------------------------------


def test_dispatch_passes_environment_and_verbatim_model_only_when_asked(gateway):
    environment = expected_environment("sonnet", TOKEN)

    def run(**kwargs):
        return asyncio.run(gateway.runtime.dispatch(
            container=gateway.container, task="t", system_prompt="", model="sonnet", max_turns=MAX_TURNS,
            timeout_s=60, working_directory="/home/assistant", resume_session_id="s-1", **kwargs,
        ))

    _, model_id, display = run(environment=environment, model_verbatim=True)
    assert (model_id, display) == ("sonnet", "sonnet")
    assert gateway.captures[-1]["kwargs"] == {"environment": environment}
    assert argv_of(gateway.captures[-1]["command"]) == [
        "claude", "-p", "t", "--resume", "s-1", "--model", "sonnet", *COMMON]
    assert gateway.resolver.call_count == 0
    _, model_id, display = run()
    assert (model_id, display) == ("claude-sonnet-4-6", "sonnet")
    assert gateway.captures[-1]["kwargs"] == {}
    assert argv_of(gateway.captures[-1]["command"]) == ["claude", "-p", "t", "--resume", "s-1", *COMMON]


def test_execute_bash_capture_passes_environment_only_when_given(gateway):
    container = FakeContainer()
    gateway.dm._execute_bash_capture(container, "true", 5)
    gateway.dm._execute_bash_capture(container, "true", 5, environment={"A": "1"})
    assert "environment" not in container.exec_kwargs[0]
    assert container.exec_kwargs[1]["environment"] == {"A": "1"}


# --- builder ---------------------------------------------------------------


def test_builder_returns_the_ten_keys_and_refuses_bad_inputs(gateway, monkeypatch):
    import claude_gateway

    assert claude_gateway.gateway_base_url() == BASE_URL
    assert claude_gateway.gateway_environment(BASE_URL, TOKEN, "m-1") == expected_environment("m-1", TOKEN)
    for token, model, detail in [(None, "m-1", NO_TOKEN), (TOKEN, "m\x7f", NO_MODEL), (TOKEN, 5, NO_MODEL)]:
        with pytest.raises(claude_gateway.GatewayInputError) as refused:
            claude_gateway.gateway_environment(BASE_URL, token, model)
        assert refused.value.detail == detail
        assert TOKEN not in str(refused.value)
    monkeypatch.setattr(gateway.dm, "SUBAGENT_CLI", "codex")
    assert claude_gateway.gateway_base_url() is None
    monkeypatch.delenv("OCU_SANDBOX_MESSAGES_ADDRESS")
    monkeypatch.setattr(gateway.dm, "SUBAGENT_CLI", "claude")
    assert claude_gateway.gateway_base_url() is None
    monkeypatch.setenv("OCU_SANDBOX_MESSAGES_ADDRESS", "not-an-ip")
    with pytest.raises(claude_gateway.GatewayConfigError, match="OCU_SANDBOX_MESSAGES_ADDRESS"):
        claude_gateway.gateway_base_url()


# --- j: per-request context reset -----------------------------------------


def test_forwarded_values_do_not_outlive_their_request(gateway, monkeypatch):
    monkeypatch.setattr(gateway.tools, "render_system_prompt", AsyncMock(return_value=""))
    fn = getattr(gateway.tools.sub_agent, "fn", None) or gateway.tools.sub_agent
    seen, results = [], []

    async def inner(scope, receive, send):
        seen.append((current_gateway_token.get(), current_chat_model.get()))
        ctx = MagicMock()
        ctx.report_progress = AsyncMock()
        results.append(await fn(task="t", description="d", ctx=ctx, max_turns=MAX_TURNS))

    middleware = gateway.tools.MCPContextMiddleware(inner)
    with_headers = {"type": "http", "ocu_chat_id": CHAT,
                    "headers": [(b"x-ocu-gateway-token", TOKEN.encode()), (b"x-chat-model", b"m-1")]}
    without = {"type": "http", "ocu_chat_id": CHAT, "headers": []}

    async def two_requests():
        await middleware(with_headers, None, None)
        after_first = (current_gateway_token.get(), current_chat_model.get())
        await middleware(without, None, None)
        return after_first

    assert asyncio.run(two_requests()) == (None, None)
    assert seen == [(TOKEN, "m-1"), (None, None)]
    assert gateway.lookups[0]["anthropic_token"] is None
    assert gateway.lookups[0]["anthropic_base"] is None
    assert "**Model:** m-1 |" in results[0]
    assert results[1] == f"Error: {NO_TOKEN}"
