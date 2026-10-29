# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Terminal Claude Code gateway (#656): start-ttyd with the opener's token and the chat model.

Seams: the real FastAPI app over TestClient, the real per-chat lifecycle lock and the
real `_execute_bash`, against a fake container whose `exec_run` runs the probe, stop and
readiness scripts in a local bash with stub `pgrep`, `pkill` and `tmux`, a fake `/proc`,
and a fake listening file in place of the `/dev/tcp` listener test, so nothing on the host
is signalled or connected to. Startup refusal runs the real lifespan in a subprocess.
Expected values are the ocu-terminal-claude-gateway, ocu-terminal-start-race and
ocu-review-followups fixture literals (design evidence a-h, a-g and a-d), not recomputed
from the implementation.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = ROOT / "computer-use-server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.file_route_module_ownership import isolated_file_route_modules  # noqa: E402

CHAT = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
INTERNAL = "ocu-terminal-gateway-test-token"
MCP_KEY = "ocu-terminal-gateway-mcp-key"
ADDRESS = "172.31.0.254"
BASE_URL = "http://172.31.0.254:8080/api"
CANARY = "CANARY-webui-jwt-must-never-leak-7f3a"
OTHER_TOKEN = "CANARY-second-jwt-must-never-leak-91c2"  # gitleaks:allow fake test canary, not a credential
MODEL = "m-1"
START_TTYD = f"/terminal/{CHAT}/start-ttyd"
LISTENER = "(exec 3<>/dev/tcp/127.0.0.1/7681)"
LEGACY_CHECK = "pgrep -x ttyd > /dev/null 2>&1 && echo RUNNING || echo STOPPED"
LEGACY_START = (
    "cd /home/assistant && echo 'set -g mouse on' > ~/.tmux.conf && LANG=C.UTF-8 "
    "{prefix}nohup ttyd -W -p 7681 tmux -u new-session -A -s main bash > /dev/null 2>&1 &"
)
TTYD_PID = "4242"
NO_TOKEN_DETAIL = (
    "Claude Code gateway: the request carries no Open WebUI login token; "
    "sign in again and reopen the terminal"
)
NO_MODEL_DETAIL = (
    "Claude Code gateway: the chat has no usable model id (1-256 printable ASCII "
    "characters without spaces); select a model and reopen the terminal"
)
OLD_TTYD_DETAIL = "terminal did not start: the previous ttyd did not exit"
NOT_LISTENING_DETAIL = "terminal did not start: no ttyd with this request's settings is listening on 7681"
NEVER_EXITS = 1_000_000


def fingerprint(base_url: str, model: str, token: str) -> str:
    return hashlib.sha256(f"{base_url}\0{model}\0{token}".encode()).hexdigest()


def gateway_environment(model: str, token: str) -> dict:
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
        "OCU_TERMINAL_FINGERPRINT": fingerprint(BASE_URL, model, token),
    }


class FakeSandbox:
    """A running chat container. Shell scripts run in real bash against a fake /proc.

    A plain `pkill` (SIGTERM) arms a countdown of `linger` `pgrep` polls after which ttyd's
    pid is gone; `pkill -KILL` removes it at once unless `survives_kill` is set. Every `pkill`
    argument list is logged. `tmux` does nothing. The listener test reads a file that exists
    while ttyd listens.
    """

    def __init__(self, root: Path, docker_manager, server):
        self.name = f"owui-chat-{CHAT}"
        self.id = "cid-terminal"
        self.short_id = self.id
        self.status = "running"
        self.labels = {"chat-id": CHAT}
        self.attrs = {"Id": self.id, "Config": {"Labels": {"chat-id": CHAT}}, "State": {"Status": "running"}}
        self._dm = docker_manager
        self._server = server
        self._root = root
        self._proc = root / "proc"
        self._bin = root / "bin"
        self._pidfile = root / "ttyd.pid"
        self._listening = root / "ttyd.listening"
        self._countdown = root / "ttyd.countdown"
        self._linger = root / "ttyd.linger"
        self._survives = root / "ttyd.survives-kill"
        self._signals = root / "pkill.log"
        self._bin.mkdir(parents=True, exist_ok=True)
        stubs = {
            "pgrep": textwrap.dedent(f"""\
                #!/bin/sh
                [ -f "{self._pidfile}" ] || exit 1
                if [ -f "{self._countdown}" ]; then
                    left=$(cat "{self._countdown}")
                    if [ "$left" -le 0 ]; then
                        rm -f "{self._pidfile}" "{self._listening}" "{self._countdown}"
                        exit 1
                    fi
                    echo $((left - 1)) > "{self._countdown}"
                fi
                cat "{self._pidfile}"
                """),
            "pkill": textwrap.dedent(f"""\
                #!/bin/sh
                echo "$*" >> "{self._signals}"
                [ -f "{self._pidfile}" ] || exit 0
                if [ "$1" = -KILL ]; then
                    [ -f "{self._survives}" ] || rm -f "{self._pidfile}" "{self._listening}" "{self._countdown}"
                else
                    cat "{self._linger}" > "{self._countdown}"
                fi
                exit 0
                """),
            "tmux": "#!/bin/sh\nexit 0\n",
        }
        for name, text in stubs.items():
            (self._bin / name).write_text(text)
            (self._bin / name).chmod(0o755)
        self.calls: list[dict] = []
        self.events: list[str] = []
        self.ttyd_env: dict | None = None
        self.tmux_env: dict | None = None
        self.lock_held: list[bool] = []
        self.pid_at_start: list[bool] = []
        self.probe_answers: list[str] = []
        self.probe_delay = 0.0
        self.linger = 0
        self.survives_kill = False
        self.listens = True
        self.surviving_tmux = False

    def _spawn(self, environment: dict) -> None:
        self.ttyd_env = dict(environment)
        (self._proc / TTYD_PID).mkdir(parents=True, exist_ok=True)
        payload = b"".join(f"{key}={value}".encode() + b"\0" for key, value in self.ttyd_env.items())
        (self._proc / TTYD_PID / "environ").write_bytes(b"PATH=/usr/bin\0" + payload)
        self._pidfile.write_text(TTYD_PID + "\n")
        if self.listens:
            self._listening.write_text("")

    def run_ttyd(self, environment: dict | None) -> None:
        self.tmux_env = dict(environment or {})
        self._spawn(environment or {})

    def stale_tmux(self, environment: dict) -> None:
        self.tmux_env = dict(environment)

    def reload(self):
        return None

    def signals(self) -> list[str]:
        return self._signals.read_text().splitlines() if self._signals.exists() else []

    def _bash(self, script: str):
        local = script.replace("/proc/", f"{self._proc}/").replace(LISTENER, f'[ -f "{self._listening}" ]')
        assert "/dev/tcp" not in local, local
        assert all((self._bin / name).is_file() for name in ("pgrep", "pkill", "tmux"))
        self._linger.write_text(f"{self.linger}\n")
        if self.survives_kill:
            self._survives.write_text("")
        else:
            self._survives.unlink(missing_ok=True)
        completed = subprocess.run(
            ["bash", "-c", local],
            env={"PATH": f"{self._bin}:{os.environ.get('PATH', '/usr/bin:/bin')}"},
            capture_output=True,
            timeout=15,
        )
        return MagicMock(exit_code=completed.returncode, output=(completed.stdout, completed.stderr))

    def exec_run(self, cmd, **kwargs):
        argv = shlex.split(cmd[2])
        assert argv[0] == "timeout" and argv[1].isdigit() and argv[2:4] == ["bash", "-c"], argv
        script = argv[4]
        lock = self._dm.get_chat_lock(CHAT)
        self.lock_held.append(lock._is_owned())
        self.calls.append({"cmd": cmd, "kwargs": kwargs, "script": script, "timeout": int(argv[1])})
        if script == self._server.TERMINAL_STOP_COMMAND:
            self.events.append("stop")
            result = self._bash(script)
            if not self._pidfile.exists():
                self.ttyd_env = None
            if not self.surviving_tmux:
                self.tmux_env = None
            return result
        if "nohup ttyd" in script:
            self.events.append("start")
            self.pid_at_start.append(self._pidfile.exists())
            if self.tmux_env is not None and self.ttyd_env is None:
                # tmux new-session -A reattaches the surviving server and its old environment.
                self._spawn(self.tmux_env)
            else:
                self.run_ttyd(kwargs.get("environment"))
            return MagicMock(exit_code=0, output=(b"", b""))
        if script.startswith("SECONDS=0; until"):
            self.events.append("ready")
            return self._bash(script)
        assert "pgrep" in script, script
        self.events.append("probe")
        if self.probe_delay:
            threading.Event().wait(self.probe_delay)
        result = self._bash(script)
        self.probe_answers.append(result.output[0].decode().strip())
        return result


@pytest.fixture
def world(monkeypatch, tmp_path):
    with isolated_file_route_modules():
        monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL)
        monkeypatch.setenv("MCP_API_KEY", MCP_KEY)
        monkeypatch.setenv("OCU_WEBUI_ORIGIN", "https://webui.example")
        monkeypatch.setenv("OCU_SANDBOX_SUBNET", "10.90.0.0/24")
        monkeypatch.setenv("PUBLIC_BASE_URL", "http://ocu.example")
        monkeypatch.setenv("BASE_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("DOCKER_IMAGE", "open-computer-use:latest")
        monkeypatch.delenv("SUBAGENT_CLI", raising=False)
        monkeypatch.delenv("OCU_SANDBOX_MESSAGES_ADDRESS", raising=False)
        from fastapi.testclient import TestClient

        import app as loaded
        import docker_manager

        monkeypatch.setattr(docker_manager, "BASE_DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(docker_manager, "SUBAGENT_CLI", "claude")
        docker_manager._chat_locks.clear()
        docker_manager._FLOCK_DEPTH.clear()
        sandbox = FakeSandbox(tmp_path / "sandbox", docker_manager, loaded)
        lookups = []

        def get(name):
            lookups.append(name)
            if sandbox.status is None:
                import docker as docker_sdk

                raise docker_sdk.errors.NotFound(name)
            return sandbox

        engine = MagicMock(name="docker-client")
        engine.containers.get.side_effect = get
        monkeypatch.setattr(docker_manager, "get_docker_client", lambda: engine)
        client = TestClient(loaded.app, raise_server_exceptions=True)
        try:
            yield {"app": loaded, "dm": docker_manager, "client": client, "sandbox": sandbox,
                   "lookups": lookups, "tmp": tmp_path}
        finally:
            docker_manager._chat_locks.clear()
            docker_manager._FLOCK_DEPTH.clear()


def _gateway_on(monkeypatch):
    monkeypatch.setenv("OCU_SANDBOX_MESSAGES_ADDRESS", ADDRESS)


def _post(world, body=None, token=CANARY, cookie=None):
    headers = {"Authorization": f"Bearer {INTERNAL}"}
    if cookie is not None:
        headers["Cookie"] = cookie
    elif token is not None:
        headers["Cookie"] = f"theme=dark; token={token}; other=1"
    if body is None:
        return world["client"].post(START_TTYD, headers=headers)
    return world["client"].post(START_TTYD, headers=headers, json=body)


def _assert_lock_released(world):
    acquired = {}

    def take():
        lock = world["dm"].get_chat_lock(CHAT)
        acquired["ok"] = lock.acquire(timeout=2)
        if acquired["ok"]:
            lock.release()

    other = threading.Thread(target=take)
    other.start()
    other.join(timeout=5)
    assert acquired == {"ok": True}
    assert world["dm"]._FLOCK_DEPTH == {}


def _assert_no_secret(world, response, capsys, caplog, *tokens):
    captured = capsys.readouterr()
    texts = [response.text, captured.out, captured.err, caplog.text]
    texts += [call["cmd"][2] for call in world["sandbox"].calls]
    for token in tokens or (CANARY,):
        for text in texts:
            assert token not in text


# --- a: first open ---------------------------------------------------------


def test_first_open_passes_token_and_model_only_through_exec_environment(world, monkeypatch, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    _gateway_on(monkeypatch)
    response = _post(world, {"dangerous_mode": False, "model": MODEL})
    assert response.status_code == 200, response.text
    assert response.json() == {"started": True, "already_running": False, "restarted": False}
    sandbox = world["sandbox"]
    starts = [call for call in sandbox.calls if "nohup ttyd" in call["script"]]
    assert len(starts) == 1
    assert starts[0]["kwargs"]["environment"] == gateway_environment(MODEL, CANARY)
    assert starts[0]["script"] == LEGACY_START.format(prefix="")
    for call in sandbox.calls:
        assert CANARY not in call["cmd"][2]
        assert MODEL not in call["cmd"][2]
        assert "environment" not in call["kwargs"] or "nohup ttyd" in call["script"]
    assert sandbox.ttyd_env["ANTHROPIC_AUTH_TOKEN"] == CANARY
    _assert_no_secret(world, response, capsys, caplog)


def test_dangerous_mode_keeps_no_autostart_in_gateway_mode(world, monkeypatch):
    _gateway_on(monkeypatch)
    response = _post(world, {"dangerous_mode": True, "model": MODEL})
    assert response.status_code == 200
    start = next(call for call in world["sandbox"].calls if "nohup ttyd" in call["script"])
    assert start["script"] == LEGACY_START.format(prefix="NO_AUTOSTART=1 ")
    assert start["kwargs"]["environment"] == gateway_environment(MODEL, CANARY)


# --- b: unchanged reopen ---------------------------------------------------


def test_matching_fingerprint_reuses_running_terminal(world, monkeypatch):
    _gateway_on(monkeypatch)
    sandbox = world["sandbox"]
    sandbox.run_ttyd(gateway_environment(MODEL, CANARY))
    response = _post(world, {"dangerous_mode": False, "model": MODEL})
    assert response.status_code == 200
    assert response.json() == {"started": False, "already_running": True}
    assert sandbox.events == ["probe"]


# --- c: restart cases, lock, deadlock guard --------------------------------


@pytest.mark.parametrize(
    "running",
    [
        gateway_environment("m-0", CANARY),
        gateway_environment(MODEL, OTHER_TOKEN),
        {"HOME": "/home/assistant"},
    ],
    ids=["other-model", "other-token", "no-fingerprint"],
)
def test_changed_or_missing_fingerprint_stops_then_starts(world, monkeypatch, capsys, caplog, running):
    _gateway_on(monkeypatch)
    sandbox = world["sandbox"]
    sandbox.run_ttyd(running)
    response = _post(world, {"dangerous_mode": False, "model": MODEL})
    assert response.status_code == 200
    assert response.json() == {"started": True, "already_running": False, "restarted": True}
    assert sandbox.events == ["probe", "stop", "start", "ready"]
    assert sandbox.ttyd_env == gateway_environment(MODEL, CANARY)
    _assert_no_secret(world, response, capsys, caplog, CANARY, OTHER_TOKEN)


def test_no_ttyd_with_stale_tmux_still_stops_before_start(world, monkeypatch):
    _gateway_on(monkeypatch)
    sandbox = world["sandbox"]
    sandbox.stale_tmux(gateway_environment("m-0", OTHER_TOKEN))
    response = _post(world, {"dangerous_mode": False, "model": MODEL})
    assert response.status_code == 200
    assert response.json() == {"started": True, "already_running": False, "restarted": False}
    assert sandbox.events == ["probe", "stop", "start", "ready"]
    assert sandbox.ttyd_env == gateway_environment(MODEL, CANARY)


def test_gateway_sequence_runs_under_the_chat_lock_and_releases_it(world, monkeypatch):
    _gateway_on(monkeypatch)
    sandbox = world["sandbox"]
    sandbox.run_ttyd({"HOME": "/home/assistant"})
    outcome = {}

    def call():
        outcome["response"] = _post(world, {"dangerous_mode": False, "model": MODEL})

    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    worker.join(timeout=15)
    assert not worker.is_alive(), "gateway start deadlocked on the chat lock"
    assert outcome["response"].status_code == 200
    assert sandbox.events == ["probe", "stop", "start", "ready"]
    assert sandbox.lock_held == [True, True, True, True]
    assert (world["tmp"] / "data" / CHAT / ".idle.json").is_file(), "shutdown timer did not re-enter the lock"
    _assert_lock_released(world)


def test_concurrent_starts_serialize_and_second_reuses(world, monkeypatch):
    _gateway_on(monkeypatch)
    sandbox = world["sandbox"]
    sandbox.probe_delay = 0.2
    barrier = threading.Barrier(2)
    responses = []

    def call():
        barrier.wait()
        responses.append(_post(world, {"dangerous_mode": False, "model": MODEL}).json())

    threads = [threading.Thread(target=call, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not any(thread.is_alive() for thread in threads)
    assert sandbox.events == ["probe", "stop", "start", "ready", "probe"]
    assert sorted(responses, key=json.dumps) == sorted(
        [
            {"started": True, "already_running": False, "restarted": False},
            {"started": False, "already_running": True},
        ],
        key=json.dumps,
    )


# --- d: refusals before any container lookup -------------------------------


@pytest.mark.parametrize("cookie", [None, "token=", "theme=dark", 'token="unterminated'],
                         ids=["no-cookie", "empty-token", "no-token-morsel", "malformed"])
def test_missing_token_answers_422_before_lookup(world, monkeypatch, cookie):
    _gateway_on(monkeypatch)
    world["sandbox"].status = None
    headers = {"Authorization": f"Bearer {INTERNAL}"}
    if cookie is not None:
        headers["Cookie"] = cookie
    response = world["client"].post(START_TTYD, headers=headers, json={"model": MODEL})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert detail.startswith("Claude Code gateway: ")
    assert "token" in detail
    assert world["lookups"] == []
    assert world["sandbox"].calls == []


@pytest.mark.parametrize(
    "body",
    [None, {"dangerous_mode": False}, {"model": None}, {"model": ""}, {"model": "m" * 257},
     {"model": "m 1"}, {"model": "m\t1"}, {"model": "m\n1"}, {"model": "m\x01"}, {"model": "m\x7f"},
     {"model": "modèle"}],
    ids=["no-body", "absent", "null", "empty", "too-long", "space", "tab", "newline", "control", "del",
         "non-ascii"],
)
def test_missing_or_invalid_model_answers_422_before_lookup(world, monkeypatch, capsys, caplog, body):
    _gateway_on(monkeypatch)
    world["sandbox"].status = None
    response = _post(world, body)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert detail.startswith("Claude Code gateway: ")
    assert "model" in detail
    assert world["lookups"] == []
    assert world["sandbox"].calls == []
    _assert_no_secret(world, response, capsys, caplog)


def test_longest_valid_model_is_accepted(world, monkeypatch):
    _gateway_on(monkeypatch)
    model = "!" + "a" * 254 + "~"
    response = _post(world, {"model": model})
    assert response.status_code == 200
    assert world["sandbox"].ttyd_env["ANTHROPIC_MODEL"] == model


def test_stopped_container_after_valid_inputs_keeps_404(world, monkeypatch):
    _gateway_on(monkeypatch)
    world["sandbox"].status = "exited"
    response = _post(world, {"model": MODEL})
    assert response.status_code == 404
    assert world["sandbox"].calls == []


# --- e/g: gateway off is today's behaviour ---------------------------------


@pytest.mark.parametrize("mode", ["unset", "codex-cli"])
@pytest.mark.parametrize("dangerous", [False, True])
def test_gateway_off_runs_todays_commands_without_environment(world, monkeypatch, mode, dangerous):
    if mode == "codex-cli":
        _gateway_on(monkeypatch)
        monkeypatch.setattr(world["dm"], "SUBAGENT_CLI", "codex")
    sandbox = world["sandbox"]
    response = _post(world, {"dangerous_mode": dangerous, "model": MODEL})
    assert response.status_code == 200
    assert response.json() == {"started": True}
    prefix = "NO_AUTOSTART=1 " if dangerous else ""
    expected = [LEGACY_CHECK, LEGACY_START.format(prefix=prefix)]
    assert [call["script"] for call in sandbox.calls] == expected
    for call, script in zip(sandbox.calls, expected):
        assert call["cmd"] == ["bash", "-c", f"timeout 5 bash -c {shlex.quote(script)}"]
        assert call["kwargs"] == {"stdout": True, "stderr": True, "demux": True, "workdir": "/home/assistant"}
    reuse = _post(world, {"dangerous_mode": dangerous, "model": MODEL})
    assert reuse.json() == {"started": False, "already_running": True}


def test_gateway_off_ignores_missing_model_and_token(world):
    response = _post(world, None, token=None)
    assert response.status_code == 200
    assert response.json() == {"started": True}


def test_execute_bash_passes_environment_only_when_given(world):
    dm = world["dm"]
    sandbox = world["sandbox"]
    dm._execute_bash(sandbox, LEGACY_CHECK, 5)
    dm._execute_bash(sandbox, LEGACY_CHECK, 5, environment={"A": "1"})
    assert "environment" not in sandbox.calls[0]["kwargs"]
    assert sandbox.calls[1]["kwargs"]["environment"] == {"A": "1"}


# --- g: startup validation -------------------------------------------------


@pytest.mark.parametrize("value", ["not-an-ip", "", "   ", " 172.31.0.254", "172.31.0.254/32", "fd00::1",
                                   "172.31.0.300"])
def test_invalid_messages_address_refuses_startup_naming_the_variable(tmp_path, value):
    script = textwrap.dedent(
        """
        import os
        import sys
        import docker as docker_mod
        from fastapi.testclient import TestClient

        def fail(*_args, **_kwargs):
            raise AssertionError("real docker client constructed")

        docker_mod.DockerClient = fail
        sys.path.insert(0, os.environ["OCU_SERVER_DIR"])
        import app

        app.startup_preflight = lambda: 0
        app.validate_idle_configuration = lambda *a, **k: (600, 30)
        app.startup_idle_sweep = lambda now=None: None
        app.reap_known_sandboxes = lambda now=None: None
        try:
            with TestClient(app.app):
                raise SystemExit("lifespan accepted the messages address")
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(3 if "OCU_SANDBOX_MESSAGES_ADDRESS" in str(exc) else 4)
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(SERVER_DIR),
        env={
            **{name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH")
               if name in os.environ},
            "OCU_SERVER_DIR": str(SERVER_DIR),
            "OCU_INTERNAL_TOKEN": INTERNAL,
            "MCP_API_KEY": MCP_KEY,
            "OCU_WEBUI_ORIGIN": "https://webui.example",
            "PUBLIC_BASE_URL": "/ocu",
            "BASE_DATA_DIR": str(tmp_path / "data"),
            "DOCKER_IMAGE": "python:3.12-slim",
            "OCU_SANDBOX_MESSAGES_ADDRESS": value,
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 3, completed.stderr[-800:]
    assert "real docker client constructed" not in completed.stderr


def test_gateway_base_url_is_derived_from_a_valid_address(world, monkeypatch):
    import claude_gateway

    assert claude_gateway.gateway_base_url() is None
    _gateway_on(monkeypatch)
    assert claude_gateway.gateway_base_url() == BASE_URL
    monkeypatch.setattr(world["dm"], "SUBAGENT_CLI", "opencode")
    assert claude_gateway.gateway_base_url() is None


@pytest.mark.parametrize(
    "cookie, body, detail",
    [
        (None, {"model": MODEL}, NO_TOKEN_DETAIL),
        (f"token={CANARY}", {"model": "m 1"}, NO_MODEL_DETAIL),
        (f"token={CANARY}", None, NO_MODEL_DETAIL),
    ],
    ids=["no-token", "bad-model", "no-model"],
)
def test_refusals_keep_the_exact_detail_strings(world, monkeypatch, cookie, body, detail):
    _gateway_on(monkeypatch)
    headers = {"Authorization": f"Bearer {INTERNAL}"}
    if cookie is not None:
        headers["Cookie"] = cookie
    response = world["client"].post(START_TTYD, headers=headers, json=body)
    assert response.status_code == 422
    assert response.json() == {"detail": detail}
    assert world["lookups"] == []


# --- h: one cookie parser --------------------------------------------------


@pytest.mark.parametrize(
    "header, expected",
    [
        (f"theme=dark; token={CANARY}; other=1", CANARY),
        (f"token={CANARY}", CANARY),
        (None, None),
        ("", None),
        ("   ", None),
        ("token=", None),
        ("theme=dark", None),
        ('token="unterminated', None),
    ],
    ids=["among-others", "alone", "none", "empty", "blank", "empty-token", "absent", "malformed"],
)
def test_token_cookie_returns_the_token_morsel_or_none(world, header, expected):
    import ws_recheck

    assert ws_recheck.token_cookie(header) == expected


def test_handshake_cookie_still_returns_the_whole_header(world):
    import ws_recheck

    class Socket:
        def __init__(self, cookie):
            self.headers = {} if cookie is None else {"cookie": cookie}

    header = f"theme=dark; token={CANARY}"
    assert ws_recheck.handshake_cookie(Socket(header)) == header
    assert ws_recheck.handshake_cookie(Socket("token=")) is None
    assert ws_recheck.handshake_cookie(Socket(None)) is None


def test_unreadable_ttyd_environment_restarts_the_terminal(world, monkeypatch):
    _gateway_on(monkeypatch)
    sandbox = world["sandbox"]
    sandbox.run_ttyd(gateway_environment(MODEL, CANARY))
    (sandbox._proc / TTYD_PID / "environ").unlink()
    response = _post(world, {"dangerous_mode": False, "model": MODEL})
    assert response.json() == {"started": True, "already_running": False, "restarted": True}
    assert sandbox.events == ["probe", "stop", "start", "ready"]


# --- start race (ocu-terminal-start-race) ----------------------------------


def test_old_ttyd_exiting_late_is_awaited_before_the_new_start(world, monkeypatch):
    _gateway_on(monkeypatch)
    sandbox = world["sandbox"]
    sandbox.run_ttyd(gateway_environment("m-0", CANARY))
    sandbox.linger = 3
    response = _post(world, {"dangerous_mode": False, "model": MODEL})
    assert response.status_code == 200, response.text
    assert response.json() == {"started": True, "already_running": False, "restarted": True}
    assert sandbox.events == ["probe", "stop", "start", "ready"]
    assert sandbox.pid_at_start == [False]
    assert sandbox.ttyd_env == gateway_environment(MODEL, CANARY)


def test_old_ttyd_that_never_exits_answers_503_without_starting(world, monkeypatch, capsys, caplog):
    _gateway_on(monkeypatch)
    sandbox = world["sandbox"]
    sandbox.run_ttyd(gateway_environment("m-0", OTHER_TOKEN))
    sandbox.linger = NEVER_EXITS
    sandbox.survives_kill = True
    response = _post(world, {"dangerous_mode": False, "model": MODEL})
    assert response.status_code == 503
    assert response.json() == {"detail": OLD_TTYD_DETAIL}
    assert sandbox.events == ["probe", "stop"]
    assert sandbox.signals() == ["-x ttyd", "-KILL -x ttyd"]
    assert not any("nohup ttyd" in call["script"] for call in sandbox.calls)
    _assert_lock_released(world)
    _assert_no_secret(world, response, capsys, caplog, CANARY, OTHER_TOKEN)


def test_ttyd_not_listening_after_start_answers_503_and_is_stopped(world, monkeypatch, capsys, caplog):
    _gateway_on(monkeypatch)
    sandbox = world["sandbox"]
    sandbox.listens = False
    response = _post(world, {"dangerous_mode": False, "model": MODEL})
    assert response.status_code == 503
    assert response.json() == {"detail": NOT_LISTENING_DETAIL}
    assert sandbox.events == ["probe", "stop", "start", "ready", "stop"]
    assert not sandbox._pidfile.exists()
    _assert_lock_released(world)
    _assert_no_secret(world, response, capsys, caplog)
    sandbox.listens = True
    again = _post(world, {"dangerous_mode": False, "model": MODEL})
    assert again.json() == {"started": True, "already_running": False, "restarted": False}


def test_fingerprint_mismatch_after_start_answers_503_and_is_stopped(world, monkeypatch, capsys, caplog):
    _gateway_on(monkeypatch)
    sandbox = world["sandbox"]
    sandbox.run_ttyd(gateway_environment("m-0", OTHER_TOKEN))
    sandbox.surviving_tmux = True
    response = _post(world, {"dangerous_mode": False, "model": MODEL})
    assert response.status_code == 503
    assert response.json() == {"detail": NOT_LISTENING_DETAIL}
    assert sandbox.events == ["probe", "stop", "start", "ready", "stop"]
    assert not sandbox._pidfile.exists()
    _assert_no_secret(world, response, capsys, caplog, CANARY, OTHER_TOKEN)


def test_bounded_waits_are_minimums_inside_their_exec_timeouts(world, monkeypatch):
    # $SECONDS counts whole seconds: -gt N waits N to N+1 s. Worst cases: stop 4 + 2 s, readiness 6 s.
    _gateway_on(monkeypatch)
    sandbox = world["sandbox"]
    sandbox.run_ttyd({"HOME": "/home/assistant"})
    assert _post(world, {"dangerous_mode": False, "model": MODEL}).status_code == 200
    calls = dict(zip(sandbox.events, sandbox.calls))
    stop, ready = calls["stop"]["script"], calls["ready"]["script"]
    assert "-gt 3" in stop and "-gt 1" in stop and "-ge" not in stop
    assert "-gt 5" in ready and "-ge" not in ready
    assert calls["stop"]["timeout"] > 6
    assert calls["ready"]["timeout"] > 6
    assert world["app"].TERMINAL_STOP_TIMEOUT > 6
    assert world["app"].TERMINAL_READY_TIMEOUT > 6


def test_stop_ttyd_answers_stopped_even_when_ttyd_outlives_the_wait(world):
    sandbox = world["sandbox"]
    sandbox.run_ttyd({"HOME": "/home/assistant"})
    sandbox.linger = NEVER_EXITS
    sandbox.survives_kill = True
    response = world["client"].post(f"/terminal/{CHAT}/stop-ttyd", headers={"Authorization": f"Bearer {INTERNAL}"})
    assert response.status_code == 200
    assert response.json() == {"stopped": True}
    assert sandbox.events == ["stop"]
    assert sandbox.signals() == ["-x ttyd", "-KILL -x ttyd"]
    assert sandbox._pidfile.exists()


# --- review follow-ups (ocu-review-followups) -------------------------------


def test_fingerprinted_ttyd_that_does_not_listen_is_restarted_not_reused(world, monkeypatch):
    _gateway_on(monkeypatch)
    sandbox = world["sandbox"]
    sandbox.listens = False
    sandbox.run_ttyd(gateway_environment(MODEL, CANARY))
    sandbox.listens = True
    response = _post(world, {"dangerous_mode": False, "model": MODEL})
    assert response.status_code == 200, response.text
    assert response.json() == {"started": True, "already_running": False, "restarted": True}
    assert sandbox.events == ["probe", "stop", "start", "ready"]
    assert sandbox.ttyd_env == gateway_environment(MODEL, CANARY)


def test_ttyd_ignoring_sigterm_after_a_failed_start_is_killed_and_never_reused(world, monkeypatch, capsys, caplog):
    _gateway_on(monkeypatch)
    sandbox = world["sandbox"]
    sandbox.listens = False
    sandbox.linger = NEVER_EXITS
    first = _post(world, {"dangerous_mode": False, "model": MODEL})
    assert first.status_code == 503
    assert first.json() == {"detail": NOT_LISTENING_DETAIL}
    assert sandbox.events == ["probe", "stop", "start", "ready", "stop"]
    _assert_lock_released(world)
    second = _post(world, {"dangerous_mode": False, "model": MODEL})
    assert second.json() == {"detail": NOT_LISTENING_DETAIL}
    assert second.status_code == 503
    assert sandbox.events[5:] == ["probe", "stop", "start", "ready", "stop"]
    assert sandbox.probe_answers == ["NONE", "NONE"]
    # Per start: the first stop finds no ttyd; the cleanup stop escalates.
    assert sandbox.signals() == ["-x ttyd", "-x ttyd", "-KILL -x ttyd"] * 2
    assert not sandbox._pidfile.exists()
    _assert_lock_released(world)
    _assert_no_secret(world, second, capsys, caplog)


def test_stop_escalates_to_sigkill_for_a_ttyd_ignoring_sigterm(world):
    sandbox = world["sandbox"]
    sandbox.run_ttyd({"HOME": "/home/assistant"})
    sandbox.linger = NEVER_EXITS
    result = world["dm"]._execute_bash(sandbox, world["app"].TERMINAL_STOP_COMMAND, world["app"].TERMINAL_STOP_TIMEOUT)
    assert result["exit_code"] == 0
    assert sandbox.signals() == ["-x ttyd", "-KILL -x ttyd"]
    assert not sandbox._pidfile.exists()
