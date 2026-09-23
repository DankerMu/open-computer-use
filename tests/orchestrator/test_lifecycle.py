# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Per-chat lifecycle: explicit launch, shared locks, credentials, host idle.

Docker is a MagicMock. Nothing in this module opens a socket or imports a live
engine client. Expected statuses and error literals come from the ocu-lifecycle
spec, not from recomputing the implementation.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SERVER_DIR = Path(__file__).resolve().parents[2] / "computer-use-server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

INTERNAL = "ocu-test-internal-token"
MCP_KEY = "ocu-test-mcp-api-key"
CHAT = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
CHAT_B = "b2c3d4e5-f6a7-8901-bcde-f12345678901"
STOPPED_MESSAGE = "workspace is stopped and needs an explicit launch"
NEVER_CREATED = "never_created"
MIGRATION_REQUIRED = "migration_required"
UV = "/Users/danker/.local/bin/uv"
PYTEST_PREFIX = [
    UV,
    "run",
    "--no-project",
    "--python",
    "3.12",
    "--with",
    "pytest",
    "--with-requirements",
    str(SERVER_DIR / "requirements.txt"),
    "--",
    "python",
    "-m",
    "pytest",
]


import docker as docker_sdk

APIError = docker_sdk.errors.APIError
NotFound = docker_sdk.errors.NotFound


class Clock:
    def __init__(self):
        self.now = 1_700_000_000.0

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


def _apply_env(monkeypatch, tmp_path):
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL)
    monkeypatch.setenv("MCP_API_KEY", MCP_KEY)
    monkeypatch.setenv("OCU_WEBUI_ORIGIN", "https://webui.example")
    monkeypatch.setenv("PUBLIC_BASE_URL", "/ocu")
    monkeypatch.setenv("BASE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("USER_DATA_BASE_PATH", str(tmp_path / "user-data"))
    monkeypatch.setenv("CONTAINER_IDLE_TIMEOUT", "600")
    monkeypatch.setenv("OCU_IDLE_POLL_SECONDS", "30")
    monkeypatch.setenv("DOCKER_IMAGE", "python:3.12-slim")
    monkeypatch.delenv("OCU_SANDBOX_NO_AUTOSTART", raising=False)
    monkeypatch.delenv("OCU_SANDBOX_SUBNET", raising=False)


def _container(name, status="running", container_id="cid-1"):
    container = MagicMock(name=name)
    container.name = name
    container.id = container_id
    container.short_id = container_id[:12]
    container.status = status
    container.attrs = {
        "Id": container_id,
        "Name": f"/{name}",
        "State": {"Status": status, "Paused": status == "paused"},
        "HostConfig": {"NetworkMode": "bridge"},
        "NetworkSettings": {"Networks": {}, "Ports": {}},
    }
    removed = {"value": False}
    stopped = {"value": False}
    execs = []

    def reload():
        container.attrs["State"]["Status"] = container.status
        container.attrs["State"]["Paused"] = container.status == "paused"
        container.attrs["Id"] = container.id

    def start():
        if container.status == "dead":
            response = MagicMock(status_code=500, url="http://docker.test", reason="error")
            raise APIError("engine refused start", response=response, explanation=b"refused")
        container.status = "running"

    def stop(timeout=10):
        stopped["value"] = True
        container.status = "exited"

    def unpause():
        container.status = "running"

    def remove(force=False):
        removed["value"] = True

    def exec_run(cmd, detach=False, user=None, **kwargs):
        execs.append({"cmd": cmd, "detach": detach, "user": user})
        if isinstance(cmd, str) and "shutdown-timer" in cmd:
            return MagicMock(exit_code=0, output=(b"", b""))
        if isinstance(cmd, str) and ".shutdown-timer-pid" in cmd:
            return MagicMock(exit_code=0, output=(b"4242\n", b""))
        if isinstance(cmd, list) and cmd[:3] == ["bash", "-lc", "kill"]:
            return MagicMock(exit_code=0, output=(b"", b""))
        return MagicMock(exit_code=0, output=(b"ok\n", b""))

    container.reload.side_effect = reload
    container.start.side_effect = start
    container.stop.side_effect = stop
    container.unpause.side_effect = unpause
    container.remove.side_effect = remove
    container.exec_run.side_effect = exec_run
    container._removed = removed
    container._stopped = stopped
    container._execs = execs
    return container


def _docker(containers=None):
    client = MagicMock(name="docker-client")
    client.ping.return_value = True
    store = {item.name: item for item in containers or []}
    created = []

    def get(name):
        if name not in store:
            raise NotFound(name)
        return store[name]

    def create(**config):
        name = config["name"]
        if name in store:
            raise APIError("Conflict", response=type("R", (), {"status_code": 409})())
        container = _container(name, status="created", container_id=f"cid-{len(created) + 1}")
        container._create_config = config
        store[name] = container
        created.append(container)
        return container

    client.containers.get.side_effect = get
    client.containers.create.side_effect = create
    client._store = store
    client._created = created
    return client


@pytest.fixture
def world(monkeypatch, tmp_path):
    _apply_env(monkeypatch, tmp_path)
    import docker_manager

    docker_manager.BASE_DATA_DIR = tmp_path / "data"
    docker_manager.USER_DATA_BASE_PATH = str(tmp_path / "user-data")
    monkeypatch.setattr(docker_manager, "DOCKER_IMAGE", os.environ["DOCKER_IMAGE"])
    docker_manager._docker_client = None
    docker_manager._chat_locks.clear()
    docker_manager._FLOCK_DEPTH.clear()
    client = _docker()
    monkeypatch.setattr(docker_manager, "get_docker_client", lambda: client)
    monkeypatch.setattr(docker_manager, "render_system_prompt_sync", lambda *args, **kwargs: "readme")
    monkeypatch.setattr(docker_manager, "_get_compose_network_name", lambda force_refresh=False: None)
    monkeypatch.setattr(docker_manager.skill_manager, "get_user_skills_sync", lambda email: [])
    monkeypatch.setattr(docker_manager.skill_manager, "get_skill_mounts", lambda skills: {})
    clock = Clock()
    monkeypatch.setattr(docker_manager.time, "time", clock.time)
    monkeypatch.setattr(docker_manager.time, "sleep", lambda _seconds: None)
    gitlab_tok = docker_manager.current_gitlab_token.set(None)
    anth_tok = docker_manager.current_anthropic_auth_token.set(None)
    email_tok = docker_manager.current_user_email.set(None)
    name_tok = docker_manager.current_user_name.set(None)
    source_tok = docker_manager.current_credential_source.set("request")
    saved_anthropic = docker_manager.ANTHROPIC_AUTH_TOKEN
    try:
        yield docker_manager, client, clock, tmp_path
    finally:
        docker_manager.current_gitlab_token.reset(gitlab_tok)
        docker_manager.current_anthropic_auth_token.reset(anth_tok)
        docker_manager.current_user_email.reset(email_tok)
        docker_manager.current_user_name.reset(name_tok)
        docker_manager.current_credential_source.reset(source_tok)
        docker_manager.ANTHROPIC_AUTH_TOKEN = saved_anthropic
        docker_manager._FLOCK_DEPTH.clear()
        docker_manager._chat_locks.clear()
        docker_manager._docker_client = None


@pytest.fixture(scope="module")
def app_module():
    os.environ["OCU_INTERNAL_TOKEN"] = INTERNAL
    os.environ["PUBLIC_BASE_URL"] = "/ocu"
    os.environ["MCP_API_KEY"] = MCP_KEY
    import app as loaded
    from fastapi.testclient import TestClient

    # Neighbor suites reload docker_manager after this process has already
    # imported app. importlib.reload replaces LifecycleError on the same
    # module object, so the name app bound at import time is a different
    # class from the one tests (and launch_sandbox) raise. Rebind once,
    # while this client is the only user of the loaded app, so except
    # clauses see the class identity the rest of the suite is using.
    import docker_manager

    saved = {
        "LifecycleError": loaded.LifecycleError,
        "startup_idle_sweep": loaded.startup_idle_sweep,
        "reap_known_sandboxes": loaded.reap_known_sandboxes,
        "validate_idle_configuration": loaded.validate_idle_configuration,
    }
    saved_client = getattr(loaded, "_lifecycle_client", None)
    loaded.LifecycleError = docker_manager.LifecycleError
    loaded.startup_idle_sweep = lambda now=None: None
    loaded.reap_known_sandboxes = lambda now=None: None
    loaded.validate_idle_configuration = lambda *args, **kwargs: (600, 30)
    with TestClient(loaded.app) as client:
        loaded._lifecycle_client = client
        try:
            yield loaded
        finally:
            for key, value in saved.items():
                setattr(loaded, key, value)
            if saved_client is None:
                loaded._lifecycle_client = None
            else:
                loaded._lifecycle_client = saved_client
            for key in ("OCU_INTERNAL_TOKEN", "PUBLIC_BASE_URL", "MCP_API_KEY"):
                os.environ.pop(key, None)
            assert loaded.startup_idle_sweep is saved["startup_idle_sweep"]
            assert loaded.reap_known_sandboxes is saved["reap_known_sandboxes"]
            assert loaded.validate_idle_configuration is saved["validate_idle_configuration"]
            assert loaded.LifecycleError is saved["LifecycleError"]





def _meta(docker_manager, chat_id, **extra):
    payload = {
        "user_email": "owner@example",
        "user_name": "Owner",
        "mcp_servers": "",
        "created_at": "2026-01-01T00:00:00Z",
    }
    payload.update(extra)
    path = docker_manager._get_meta_path(chat_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return path


def _put(client, name, status, container_id="cid-existing"):
    container = _container(name, status=status, container_id=container_id)
    client._store[name] = container
    return container


def _env_of(container):
    return container._create_config["environment"]


def test_stopped_tool_call_does_not_start_or_recreate(world):
    docker_manager, client, _clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", "exited")
    with pytest.raises(docker_manager.SandboxStopped, match=STOPPED_MESSAGE):
        docker_manager._get_or_create_container(CHAT)
    assert container.status == "exited"
    container.start.assert_not_called()
    assert client._created == []


@pytest.mark.parametrize("status", ["paused", "created", "restarting", "dead"])
def test_non_running_tool_call_leaves_state_unchanged(world, status):
    docker_manager, client, _clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", status)
    with pytest.raises(docker_manager.SandboxStopped):
        docker_manager._get_or_create_container(CHAT)
    assert container.status == status
    container.start.assert_not_called()
    container.unpause.assert_not_called()


def test_absent_valid_metadata_does_not_create_on_tool_path(world):
    docker_manager, client, _clock, _tmp = world
    _meta(docker_manager, CHAT)
    with pytest.raises(docker_manager.SandboxStopped):
        docker_manager._get_or_create_container(CHAT)
    assert client._created == []
    assert docker_manager.load_container_meta(CHAT)["user_email"] == "owner@example"


def test_corrupt_metadata_fails_closed_on_tool_path(world):
    docker_manager, client, _clock, _tmp = world
    path = docker_manager._get_meta_path(CHAT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json")
    with pytest.raises(docker_manager.MetadataCorrupt):
        docker_manager._get_or_create_container(CHAT)
    assert client._created == []
    assert path.read_text() == "{not-json"


def test_running_tool_call_reuses_without_start(world):
    docker_manager, client, _clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", "running")
    assert docker_manager._get_or_create_container(CHAT) is container
    container.start.assert_not_called()


def test_create_conflict_adopts_running_winner_without_delete(world):
    docker_manager, client, _clock, _tmp = world
    winner = _put(client, f"owui-chat-{CHAT}", "running", container_id="winner")

    def conflict(**config):
        raise APIError("name already in use", response=type("R", (), {"status_code": 409})())

    client.containers.create.side_effect = conflict
    docker_manager._create_container(CHAT, f"owui-chat-{CHAT}")
    winner.remove.assert_not_called()
    assert winner._removed["value"] is False


def test_create_conflict_non_running_winner_is_not_deleted(world):
    docker_manager, client, _clock, _tmp = world
    winner = _put(client, f"owui-chat-{CHAT}", "exited", container_id="loser")

    def conflict(**config):
        raise APIError("name already in use", response=type("R", (), {"status_code": 409})())

    client.containers.create.side_effect = conflict
    with pytest.raises(docker_manager.SandboxStopped):
        docker_manager._get_or_create_container(CHAT)
    winner.remove.assert_not_called()
    assert winner.status == "exited"


def test_launch_never_created_is_409_and_creates_nothing(world):
    docker_manager, client, _clock, _tmp = world
    with pytest.raises(docker_manager.NeverCreated) as caught:
        docker_manager.launch_sandbox(CHAT)
    assert caught.value.status_code == 409
    assert caught.value.reason == NEVER_CREATED
    assert client._created == []


def test_launch_running_is_idempotent(world):
    docker_manager, client, _clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", "running", container_id="same")
    body = docker_manager.launch_sandbox(CHAT)
    assert body == {"state": "running"}
    container.start.assert_not_called()
    container.stop.assert_not_called()
    assert client._created == []


@pytest.mark.parametrize("status", ["exited", "created"])
def test_launch_starts_exited_or_created_and_observes_running(world, status):
    docker_manager, client, _clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", status)
    assert docker_manager.launch_sandbox(CHAT) == {"state": "running"}
    assert container.status == "running"
    container.remove.assert_not_called()


def test_launch_paused_unpauses_after_fresh_idle_window(world):
    docker_manager, client, clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", "paused", container_id="paused-1")
    docker_manager.mark_sleeper_retired(CHAT, container, now=clock.time())
    clock.advance(10_000)
    assert docker_manager.launch_sandbox(CHAT) == {"state": "running"}
    container.unpause.assert_called()
    state = docker_manager.read_idle_state(CHAT)
    assert state["container_id"] == "paused-1"
    assert state["idle_expiry"] == clock.time() + 600
    docker_manager.reap_idle(CHAT, now=clock.time() + 599)
    assert container.status == "running"


def test_launch_restarting_waits_until_running(world):
    docker_manager, client, _clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", "restarting")
    calls = {"n": 0}

    def reload():
        calls["n"] += 1
        if calls["n"] >= 2:
            container.status = "running"
        container.attrs["State"]["Status"] = container.status

    container.reload.side_effect = reload
    assert docker_manager.launch_sandbox(CHAT) == {"state": "running"}
    container.remove.assert_not_called()


def test_launch_restart_timeout_preserves_container(world):
    docker_manager, client, _clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", "restarting")
    with pytest.raises(docker_manager.LaunchFailed) as caught:
        docker_manager.launch_sandbox(CHAT)
    assert caught.value.status_code == 504
    assert container.status == "restarting"
    container.remove.assert_not_called()
    assert client._created == []


def test_launch_dead_and_engine_refusal_do_not_delete(world):
    docker_manager, client, _clock, _tmp = world
    dead = _put(client, f"owui-chat-{CHAT}", "dead")
    with pytest.raises(docker_manager.LaunchFailed) as caught:
        docker_manager.launch_sandbox(CHAT)
    assert caught.value.status_code == 500
    dead.remove.assert_not_called()
    refused = _put(client, f"owui-chat-{CHAT_B}", "exited")
    refused.start.side_effect = APIError("engine refused start", response=MagicMock(status_code=500, url="http://docker.test", reason="error"), explanation=b"refused")
    with pytest.raises(docker_manager.LaunchFailed):
        docker_manager.launch_sandbox(CHAT_B)
    refused.remove.assert_not_called()
    assert refused.status == "exited"


def test_launch_corrupt_metadata_preserves_file(world):
    docker_manager, client, _clock, _tmp = world
    path = docker_manager._get_meta_path(CHAT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken")
    with pytest.raises(docker_manager.MetadataCorrupt) as caught:
        docker_manager.launch_sandbox(CHAT)
    assert caught.value.status_code == 500
    assert path.read_text() == "{broken"
    assert client._created == []


def test_launch_recreates_absent_container_from_server_fallbacks_only(world, monkeypatch):
    docker_manager, client, _clock, _tmp = world
    _meta(docker_manager, CHAT, user_email="saved@example", user_name="Saved")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "server-fallback-token")
    monkeypatch.setattr(docker_manager, "ANTHROPIC_AUTH_TOKEN", "server-fallback-token")
    gitlab_tok = docker_manager.current_gitlab_token.set("request-token-must-not-enter")
    anth_tok = docker_manager.current_anthropic_auth_token.set("request-anthropic-must-not-enter")
    try:
        body = docker_manager.launch_sandbox(CHAT, credential_source="server")
        assert body == {"state": "running"}
        env = _env_of(client._created[0])
        assert env["ANTHROPIC_AUTH_TOKEN"] == "server-fallback-token"
        assert "request-token-must-not-enter" not in env.values()
        assert "request-anthropic-must-not-enter" not in env.values()
        assert env["GIT_AUTHOR_EMAIL"] == "saved@example"
    finally:
        docker_manager.current_gitlab_token.reset(gitlab_tok)
        docker_manager.current_anthropic_auth_token.reset(anth_tok)


def test_metadata_write_is_atomic_and_corrupt_read_is_distinct(world):
    docker_manager, _client, _clock, tmp_path = world
    docker_manager.save_container_meta(CHAT, "a@example", "A", "srv")
    path = docker_manager._get_meta_path(CHAT)
    leftovers = list(path.parent.glob(".meta.json.*.tmp"))
    assert leftovers == []
    assert docker_manager.load_container_meta(CHAT)["user_email"] == "a@example"
    path.write_text("not-json")
    with pytest.raises(docker_manager.MetadataCorrupt):
        docker_manager.load_container_meta(CHAT)
    path.unlink()
    assert docker_manager.load_container_meta(CHAT) is None
    assert tmp_path.exists()


def test_lock_identity_is_stable_for_case_variants(world):
    docker_manager, _client, _clock, _tmp = world
    first = docker_manager.get_chat_lock(CHAT.upper())
    second = docker_manager.get_chat_lock(CHAT)
    assert first is second


def test_concurrent_first_use_creates_one_container(world):
    docker_manager, client, _clock, _tmp = world
    barrier = threading.Barrier(2)
    found = []
    errors = []

    def create_one():
        try:
            barrier.wait(timeout=5)
            found.append(docker_manager._get_or_create_container(CHAT))
        except Exception as exc:  # pragma: no cover - assertion below
            errors.append(exc)

    threads = [threading.Thread(target=create_one) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert errors == []
    assert len(client._created) == 1
    assert found[0] is found[1] is client._created[0]


def test_separate_request_contexts_do_not_cross_credentials(world):
    docker_manager, client, _clock, _tmp = world
    barrier = threading.Barrier(2)

    def create(chat_id, token):
        barrier.wait(timeout=5)
        docker_manager.current_gitlab_token.set(token)
        docker_manager._get_or_create_container(chat_id)

    threads = [
        threading.Thread(target=create, args=(CHAT, "token-a")),
        threading.Thread(target=create, args=(CHAT_B, "token-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    envs = {item.name: _env_of(item) for item in client._created}
    assert envs[f"owui-chat-{CHAT}"]["GITLAB_TOKEN"] == "token-a"
    assert envs[f"owui-chat-{CHAT_B}"]["GITLAB_TOKEN"] == "token-b"
    assert "token-b" not in envs[f"owui-chat-{CHAT}"].values()
    assert "token-a" not in envs[f"owui-chat-{CHAT_B}"].values()


def test_internal_secrets_never_enter_sandbox_and_no_autostart_is_exact(world, monkeypatch):
    docker_manager, client, _clock, _tmp = world
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL)
    monkeypatch.setenv("MCP_API_KEY", MCP_KEY)
    docker_manager._get_or_create_container(CHAT)
    env = _env_of(client._created[0])
    assert "OCU_INTERNAL_TOKEN" not in env
    assert "MCP_API_KEY" not in env
    assert INTERNAL not in env.values()
    assert MCP_KEY not in env.values()
    assert "NO_AUTOSTART" not in env

    monkeypatch.setenv("OCU_SANDBOX_NO_AUTOSTART", "1")
    docker_manager._get_or_create_container(CHAT_B)
    second = _env_of(client._created[1])
    assert second["NO_AUTOSTART"] == "1"
    assert "OCU_INTERNAL_TOKEN" not in second
    assert "MCP_API_KEY" not in second


def test_command_timeout_extends_idle_without_container_sleeper(world):
    docker_manager, client, clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", "running", container_id="run-1")
    docker_manager._execute_bash(container, "true", timeout=1000)
    state = docker_manager.read_idle_state(CHAT)
    assert state["idle_expiry"] >= clock.time() + max(600, 1000 + 60)
    assert all("sleep" not in item["cmd"] for item in container._execs)


def test_external_pause_beyond_timeout_does_not_stop_after_unpause(world):
    docker_manager, client, clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", "running", container_id="pause-1")
    docker_manager.note_running_activity(CHAT, container, now=clock.time())
    clock.advance(100)
    container.status = "paused"
    docker_manager.reap_idle(CHAT, now=clock.time())
    clock.advance(5_000)
    docker_manager.reap_idle(CHAT, now=clock.time())
    assert container.status == "paused"
    container.status = "running"
    docker_manager.reap_idle(CHAT, now=clock.time())
    assert container.status == "running"
    state = docker_manager.read_idle_state(CHAT)
    assert state["idle_expiry"] == clock.time() + 600


def test_tracking_interruption_grants_fresh_window(world):
    docker_manager, client, clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", "running", container_id="gap-1")
    docker_manager.write_idle_state(
        CHAT,
        {
            "container_id": "gap-1",
            "status": "running",
            "observed_at": clock.time() - 10_000,
            "idle_expiry": clock.time() - 9_000,
            "sleeper_retired_for": "gap-1",
        },
    )
    docker_manager.reap_idle(CHAT, now=clock.time())
    assert container.status == "running"
    assert docker_manager.read_idle_state(CHAT)["idle_expiry"] == clock.time() + 600


def test_worker_startup_does_not_reap_from_stale_deadline(world):
    docker_manager, client, clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", "running", container_id="boot-1")
    docker_manager.write_idle_state(
        CHAT,
        {
            "container_id": "boot-1",
            "status": "running",
            "observed_at": clock.time() - 50,
            "idle_expiry": clock.time() - 1,
            "sleeper_retired_for": "boot-1",
        },
    )
    docker_manager.startup_idle_sweep(now=clock.time())
    assert container.status == "running"
    assert docker_manager.read_idle_state(CHAT)["idle_expiry"] == clock.time() + 600

def test_heartbeat_before_expiry_prevents_stop(world):
    """A refresh that lands before the locked expiry decision survives that decision.

    The reaper and the heartbeat cannot hold the same chat lock at once, so this
    proves the decision re-reads idle state after acquisition rather than acting
    on the expiry snapshot it had before the refresh.
    """
    docker_manager, client, clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", "running", container_id="race-1")
    docker_manager.note_running_activity(CHAT, container, now=clock.time())
    expiry = docker_manager.read_idle_state(CHAT)["idle_expiry"]
    with docker_manager._combined_lock(CHAT):
        docker_manager.extend_idle(CHAT, container, 600, now=expiry - 1)
        docker_manager.reap_idle(CHAT, now=expiry)
    assert container.status == "running"
    assert docker_manager.read_idle_state(CHAT)["idle_expiry"] == expiry - 1 + 600


def test_continuous_running_expiry_stops_once(world):
    docker_manager, client, clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", "running", container_id="expire-1")
    docker_manager.note_running_activity(CHAT, container, now=clock.time())
    docker_manager.reap_idle(CHAT, now=clock.time() + 30)
    assert container.status == "running"
    docker_manager.reap_idle(CHAT, now=clock.time() + 601)
    assert container.status == "exited"
    assert container._stopped["value"] is True


def test_paused_legacy_launch_is_migration_required_without_mutation(world):
    docker_manager, client, _clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", "paused", container_id="legacy-paused")
    with pytest.raises(docker_manager.MigrationRequired) as caught:
        docker_manager.launch_sandbox(CHAT)
    assert caught.value.status_code == 409
    assert caught.value.reason == MIGRATION_REQUIRED
    container.unpause.assert_not_called()
    container.exec_run.assert_not_called()
    container.remove.assert_not_called()
    assert client._created == []
    assert container.status == "paused"


def test_exited_legacy_launch_starts_without_retirement_exec(world):
    docker_manager, client, _clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", "exited", container_id="legacy-exited")
    assert docker_manager.launch_sandbox(CHAT) == {"state": "running"}
    assert container._execs == []
    assert container.status == "running"


def test_running_legacy_retirement_failure_is_not_silent_adoption(world):
    docker_manager, client, _clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", "running", container_id="legacy-run")

    def fail(cmd, detach=False, user=None, **kwargs):
        container._execs.append({"cmd": cmd})
        return MagicMock(exit_code=1, output=(b"", b"still-alive"))

    container.exec_run.side_effect = fail
    with pytest.raises(docker_manager.MigrationRequired):
        docker_manager.retire_legacy_sleeper(CHAT, container)
    assert docker_manager.read_idle_state(CHAT) is None
    docker_manager.reap_idle(CHAT, now=10_000_000)
    assert container.status == "running"


def test_running_legacy_retirement_binds_evidence_to_container_identity(world):
    docker_manager, client, clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", "running", container_id="legacy-ok")
    docker_manager.retire_legacy_sleeper(CHAT, container)
    state = docker_manager.read_idle_state(CHAT)
    assert state["sleeper_retired_for"] == "legacy-ok"
    container.id = "replaced-id"
    docker_manager.write_idle_state(CHAT, {**state, "idle_expiry": clock.time() - 1, "observed_at": clock.time()})
    docker_manager.reap_idle(CHAT, now=clock.time())
    assert container.status == "running"
    assert docker_manager.read_idle_state(CHAT)["container_id"] == "replaced-id"


def test_operator_stopped_legacy_paused_launch_preserves_container(world):
    docker_manager, client, _clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", "exited", container_id="legacy-stopped")
    assert docker_manager.launch_sandbox(CHAT) == {"state": "running"}
    assert container.id == "legacy-stopped"
    assert client._created == []
    assert container._removed["value"] is False


def test_invalid_idle_poll_configuration_fails_loud():
    import docker_manager

    with pytest.raises(docker_manager.LifecycleConfigError):
        docker_manager.validate_idle_configuration(idle_timeout=600, poll_seconds=600)
    with pytest.raises(docker_manager.LifecycleConfigError):
        docker_manager.validate_idle_configuration(idle_timeout=0, poll_seconds=30)


def test_describe_shapes_do_not_mutate(world, monkeypatch):
    docker_manager, client, _clock, _tmp = world
    monkeypatch.setattr(
        docker_manager,
        "cli_badge",
        lambda: {"cli": "claude", "default_model": "sonnet", "supports_cost": True},
    )
    running = _put(client, f"owui-chat-{CHAT}", "running")
    body = docker_manager.describe_sandbox(CHAT)
    assert body == {
        "state": "running",
        "revision": 0,
        "views": ["files", "browser", "terminal"],
        "cli_badge": {"cli": "claude", "default_model": "sonnet", "supports_cost": True},
    }
    running.start.assert_not_called()

    paused = _put(client, f"owui-chat-{CHAT_B}", "paused")
    assert docker_manager.describe_sandbox(CHAT_B)["state"] == "stopped"
    assert docker_manager.describe_sandbox(CHAT_B)["views"] == ["files"]
    paused.unpause.assert_not_called()

    _meta(docker_manager, "c3d4e5f6-a7b8-9012-cdef-123456789012")
    absent = docker_manager.describe_sandbox("c3d4e5f6-a7b8-9012-cdef-123456789012")
    assert absent["state"] == "stopped"
    assert absent["revision"] == 0
    assert client._created == []

    assert docker_manager.describe_sandbox("d4e5f6a7-b8c9-0123-defa-234567890123")["state"] == "never_created"


def test_two_processes_share_one_creation(tmp_path):
    script = r'''
import json, os, sys, time
from pathlib import Path
from unittest.mock import MagicMock
sys.path.insert(0, os.environ["OCU_SERVER_DIR"])
os.environ["BASE_DATA_DIR"] = os.environ["OCU_BASE"]
os.environ["USER_DATA_BASE_PATH"] = os.environ["OCU_USER"]
os.environ["DOCKER_IMAGE"] = "python:3.12-slim"
import docker
import docker_manager

if os.environ.get("OCU_BROKEN_FLOCK") == "1":
    _orig_control = docker_manager._control_dir
    docker_manager._control_dir = lambda chat_id: _orig_control(chat_id) / str(os.getpid())

docker.errors.NotFound = type("NotFound", (Exception,), {})
class _Response:
    status_code = 409
    reason = "Conflict"
    url = "http://docker.test"
def raise_conflict():
    raise docker.errors.APIError("conflict", response=_Response(), explanation=b"conflict")
root = Path(os.environ["OCU_SHARED"])
store = root / "containers.json"
attempts_path = root / "attempts.jsonl"
client = MagicMock()
created = []
def get(name):
    if os.environ.get("OCU_BROKEN_FLOCK") == "1":
        looked = root / f"looked-{os.getpid()}"
        looked.write_text("1")
        while len(list(root.glob("looked-*"))) < 2:
            time.sleep(0.01)
    data = json.loads(store.read_text()) if store.exists() else {}
    if name not in data:
        if os.environ.get("OCU_BROKEN_FLOCK") != "1":
            time.sleep(float(os.environ.get("OCU_RACE_SLEEP", "0.2")))
        raise docker.errors.NotFound(name)
    container = MagicMock()
    container.name = name
    container.id = data[name]
    container.status = "running"
    container.attrs = {"Id": container.id, "State": {"Status": "running"}}
    container.reload.return_value = None
    return container
def create(**config):
    if os.environ.get("OCU_BROKEN_FLOCK") == "1":
        entered = root / f"create-{os.getpid()}"
        entered.write_text("1")
        while len(list(root.glob("create-*"))) < 2:
            time.sleep(0.01)
    with attempts_path.open("a") as handle:
        handle.write(json.dumps({"pid": os.getpid(), "name": config["name"]}) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    data = json.loads(store.read_text()) if store.exists() else {}
    if config["name"] in data:
        raise_conflict()
    data[config["name"]] = "shared-cid"
    store.write_text(json.dumps(data))
    container = get(config["name"])
    created.append(config["name"])
    return container
client.containers.get.side_effect = get
client.containers.create.side_effect = create
docker_manager.get_docker_client = lambda: client
docker_manager.render_system_prompt_sync = lambda *a, **k: "readme"
docker_manager._get_compose_network_name = lambda force_refresh=False: None
docker_manager.skill_manager.get_user_skills_sync = lambda email: []
docker_manager.skill_manager.get_skill_mounts = lambda skills: {}
ready = root / f"ready-{os.getpid()}"
ready.write_text("1")
while len(list(root.glob("ready-*"))) < 2:
    time.sleep(0.01)
docker_manager._get_or_create_container(os.environ["OCU_CHAT"])
print(json.dumps({
    "created": created,
    "store": json.loads(store.read_text()) if store.exists() else {},
}))
'''
    shared = tmp_path / "shared"
    shared.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "OCU_SERVER_DIR": str(SERVER_DIR),
            "OCU_BASE": str(tmp_path / "data"),
            "OCU_USER": str(tmp_path / "user"),
            "OCU_SHARED": str(shared),
            "OCU_CHAT": CHAT,
            "PUBLIC_BASE_URL": "/ocu",
        }
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    outputs = [proc.communicate(timeout=30) for proc in procs]
    assert [proc.returncode for proc in procs] == [0, 0], [(item[0], item[1]) for item in outputs]
    payloads = [json.loads(item[0].strip().splitlines()[-1]) for item in outputs]
    created = [name for payload in payloads for name in payload["created"]]
    assert created == [f"owui-chat-{CHAT}"]
    attempts = (shared / "attempts.jsonl").read_text().strip().splitlines()
    assert len(attempts) == 1
    assert payloads[0]["store"] == {f"owui-chat-{CHAT}": "shared-cid"}


def test_internal_routes_refuse_missing_token_before_docker(app_module, monkeypatch, tmp_path):
    _apply_env(monkeypatch, tmp_path)
    import docker_manager
    calls = {"n": 0}

    def fail(*_args, **_kwargs):
        calls["n"] += 1
        raise AssertionError("docker was reached")

    monkeypatch.setattr(docker_manager, "get_docker_client", fail)
    client = app_module._lifecycle_client
    routes = (
        ("get", f"/internal/describe/{CHAT}"),
        ("post", f"/internal/launch/{CHAT}"),
        ("post", f"/terminal/{CHAT}/restart-container"),
        ("post", f"/terminal/{CHAT.upper()}/resurrect-container"),
    )
    for method, path in routes:
        response = getattr(client, method)(path)
        assert response.status_code == 401, path
    invalid = {"Authorization": "Bearer not-the-internal-token"}
    for method, path in routes:
        response = getattr(client, method)(path, headers=invalid)
        assert response.status_code == 401, path
    assert calls["n"] == 0



def test_internal_launch_and_aliases_share_boundary(app_module, monkeypatch, tmp_path):
    _apply_env(monkeypatch, tmp_path)
    seen = []

    def launch(chat_id, credential_source="server"):
        seen.append((chat_id, credential_source))
        return {"state": "running"}

    monkeypatch.setattr(app_module, "launch_sandbox", launch)
    client = app_module._lifecycle_client
    headers = {"Authorization": f"Bearer {INTERNAL}"}
    launch_response = client.post(f"/internal/launch/{CHAT}", headers=headers)
    restart = client.post(f"/terminal/{CHAT.upper()}/restart-container", headers=headers)
    resurrect = client.post(f"/terminal/{CHAT}/resurrect-container", headers=headers)
    missing = client.post(f"/terminal/{CHAT}/restart-container")
    assert launch_response.status_code == 200
    assert launch_response.json() == {"state": "running"}
    assert restart.status_code == 200
    assert resurrect.status_code == 200
    assert missing.status_code == 401
    assert seen == [(CHAT, "server"), (CHAT, "server"), (CHAT, "server")]


def test_describe_route_reports_real_sandbox_state(app_module, monkeypatch, tmp_path):
    _apply_env(monkeypatch, tmp_path)
    import docker_manager

    client = _docker()
    docker_manager.BASE_DATA_DIR = tmp_path / "data"
    docker_manager._docker_client = None
    monkeypatch.setattr(docker_manager, "get_docker_client", lambda: client)
    monkeypatch.setattr(
        docker_manager,
        "cli_badge",
        lambda: {"cli": "claude", "default_model": "sonnet", "supports_cost": True},
    )
    monkeypatch.setattr(app_module, "describe_sandbox", docker_manager.describe_sandbox)
    http = app_module._lifecycle_client
    headers = {"Authorization": f"Bearer {INTERNAL}"}

    absent = http.get(f"/internal/describe/{CHAT}", headers=headers)
    assert absent.status_code == 200
    assert absent.json()["state"] == "never_created"
    assert absent.json()["views"] == ["files"]
    assert client._created == []

    running = _put(client, f"owui-chat-{CHAT}", "running")
    described = http.get(f"/internal/describe/{CHAT}", headers=headers)
    assert described.status_code == 200
    assert described.json() == {
        "state": "running",
        "revision": 0,
        "views": ["files", "browser", "terminal"],
        "cli_badge": {"cli": "claude", "default_model": "sonnet", "supports_cost": True},
    }
    running.start.assert_not_called()
    running.unpause.assert_not_called()

    paused = _put(client, f"owui-chat-{CHAT_B}", "paused")
    stopped = http.get(f"/internal/describe/{CHAT_B}", headers=headers)
    assert stopped.status_code == 200
    assert stopped.json()["state"] == "stopped"
    assert stopped.json()["views"] == ["files"]
    paused.unpause.assert_not_called()
    assert client._created == []


def test_launch_route_never_created_and_failure_statuses(app_module, monkeypatch, tmp_path):
    _apply_env(monkeypatch, tmp_path)
    import docker_manager
    from fastapi.testclient import TestClient

    def absent(chat_id, credential_source="server"):
        raise docker_manager.NeverCreated()

    monkeypatch.setattr(app_module, "launch_sandbox", absent)
    client = app_module._lifecycle_client
    headers = {"Authorization": f"Bearer {INTERNAL}"}
    response = client.post(f"/internal/launch/{CHAT}", headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == NEVER_CREATED

    def failed(chat_id, credential_source="server"):
        raise docker_manager.LaunchFailed(500, "engine refused")

    monkeypatch.setattr(app_module, "launch_sandbox", failed)
    failed_response = client.post(f"/internal/launch/{CHAT}", headers=headers)
    assert failed_response.status_code == 500
    assert failed_response.json()["detail"]["detail"] == "engine refused"


@pytest.mark.parametrize(
    "tool_name",
    ["view", "bash_tool", "str_replace", "create_file", "sub_agent"],
)
def test_mcp_stopped_error_is_workspace_stopped(monkeypatch, tool_name):
    import mcp_tools

    monkeypatch.setattr(mcp_tools, "_validate_chat_id", lambda: (CHAT, None))
    monkeypatch.setattr(mcp_tools, "_ensure_gitlab_token", lambda: _async_none())
    monkeypatch.setattr(
        mcp_tools,
        "_get_or_create_container",
        lambda chat_id: (_ for _ in ()).throw(mcp_tools.SandboxStopped(STOPPED_MESSAGE)),
    )
    result = _run(_mcp_tool(mcp_tools, tool_name))
    assert result.startswith("Error:")
    assert "workspace is stopped" in result
    assert "explicit launch" in result


def _async_none():
    async def _inner():
        return None

    return _inner()


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_lifespan_does_not_open_docker(app_module, monkeypatch, tmp_path):
    _apply_env(monkeypatch, tmp_path)
    import docker as docker_mod

    def fail(*_args, **_kwargs):
        raise AssertionError("real docker client constructed")

    monkeypatch.setattr(docker_mod, "DockerClient", fail)
    response = app_module._lifecycle_client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def _mcp_tool(mcp_tools, tool_name):
    if tool_name == "view":
        return mcp_tools.view("inspect", "/tmp/file", None, None)
    if tool_name == "bash_tool":
        return mcp_tools.bash_tool("true", "run", None)
    if tool_name == "str_replace":
        return mcp_tools.str_replace("edit", "old", "/tmp/file", "new", None)
    if tool_name == "create_file":
        return mcp_tools.create_file("create", "body", "/tmp/file", None)
    return mcp_tools.sub_agent("task", "desc", None)


def _sdk_inspect(name, labels, container_id="sdk-cid-1", status="created"):
    return {
        "Id": container_id,
        "Name": f"/{name}",
        "State": {"Status": status, "Paused": status == "paused"},
        "Config": {"Labels": dict(labels)},
        "NetworkSettings": {"Networks": {}, "Ports": {}},
        "HostConfig": {"NetworkMode": "bridge"},
    }


def test_fresh_create_reaches_setup_on_real_sdk_container(world, monkeypatch):
    docker_manager, _client, _clock, tmp_path = world
    Container = docker_sdk.models.containers.Container
    labels_prop = Container.__dict__["labels"]
    assert isinstance(labels_prop, property)
    assert labels_prop.fset is None

    name = f"owui-chat-{CHAT}"
    labels = {"managed-by": "mcp-computer-use-orchestrator", "chat-id": CHAT, "tool": "computer-use-mcp"}
    inspect = _sdk_inspect(name, labels)
    fake_api = MagicMock()
    fake_api.start.return_value = None
    fake_api.put_archive.return_value = True
    fake_api.exec_create.return_value = {"Id": "exec-1"}
    fake_api.exec_start.return_value = (b"ok\n", b"")
    fake_api.exec_inspect.return_value = {"ExitCode": 0}

    def inspect_container(_cid):
        payload = dict(inspect)
        payload["State"] = {"Status": "running", "Paused": False}
        inspect["State"] = payload["State"]
        return payload

    fake_api.inspect_container.side_effect = inspect_container
    fake_client = MagicMock()
    fake_client.api = fake_api

    created = []

    def create(**config):
        created.append(config)
        collection = MagicMock()

        def collection_get(_cid):
            payload = _sdk_inspect(config["name"], config.get("labels") or {}, status="running")
            inspect.update(payload)
            return Container(attrs=payload, client=fake_client, collection=collection)

        collection.get.side_effect = collection_get
        container = Container(
            attrs=_sdk_inspect(config["name"], config.get("labels") or {}, status="created"),
            client=fake_client,
            collection=collection,
        )
        inspect.update(container.attrs)
        return container

    def get(_name):
        if not created:
            raise NotFound(_name)
        collection = MagicMock()
        payload = _sdk_inspect(name, labels, status=inspect["State"]["Status"])
        collection.get.return_value = Container(attrs=payload, client=fake_client)
        return Container(attrs=payload, client=fake_client, collection=collection)

    fake_client.containers.create.side_effect = create
    fake_client.containers.get.side_effect = get
    monkeypatch.setattr(docker_manager, "get_docker_client", lambda: fake_client)
    monkeypatch.setattr(docker_manager, "_get_compose_network_name", lambda force_refresh=False: None)

    container = docker_manager._get_or_create_container(CHAT)
    assert isinstance(container, Container)
    assert container.status == "running"
    assert docker_manager.load_container_meta(CHAT)["user_email"] == ""
    state = docker_manager.read_idle_state(CHAT)
    assert state["sleeper_retired_for"] == "sdk-cid-1"
    assert (tmp_path / "data" / CHAT / ".meta.json").exists()
    with pytest.raises(AttributeError):
        container.labels = {"chat-id": CHAT}


def test_heartbeat_startup_and_tool_do_not_fabricate_retirement(world):
    docker_manager, client, clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", "running", container_id="legacy-run")
    docker_manager.record_heartbeat(CHAT, now=clock.time())
    heartbeat_state = docker_manager.read_idle_state(CHAT)
    assert heartbeat_state is None or heartbeat_state.get("sleeper_retired_for") != "legacy-run"
    assert container._execs == []

    docker_manager.startup_idle_sweep(now=clock.time())
    sweep_state = docker_manager.read_idle_state(CHAT)
    assert sweep_state is None or sweep_state.get("sleeper_retired_for") != "legacy-run"
    assert container._execs == []

    docker_manager._get_or_create_container(CHAT)
    tool_state = docker_manager.read_idle_state(CHAT)
    assert tool_state is None or tool_state.get("sleeper_retired_for") != "legacy-run"
    assert container._execs == []

    docker_manager.launch_sandbox(CHAT)
    assert any(".shutdown-timer-pid" in str(item["cmd"]) for item in container._execs)
    assert docker_manager.read_idle_state(CHAT)["sleeper_retired_for"] == "legacy-run"


def test_failed_retirement_then_paused_launch_stays_migration_required(world):
    docker_manager, client, clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", "running", container_id="legacy-fail")

    def fail(cmd, detach=False, user=None, **kwargs):
        container._execs.append({"cmd": cmd, "user": user})
        return MagicMock(exit_code=1, output=(b"", b""))

    container.exec_run.side_effect = fail
    with pytest.raises(docker_manager.MigrationRequired):
        docker_manager.launch_sandbox(CHAT)
    assert docker_manager.read_idle_state(CHAT) is None or docker_manager.read_idle_state(CHAT).get("sleeper_retired_for") != "legacy-fail"
    container.status = "paused"
    with pytest.raises(docker_manager.MigrationRequired) as caught:
        docker_manager.launch_sandbox(CHAT)
    assert caught.value.reason == MIGRATION_REQUIRED
    container.unpause.assert_not_called()


def test_short_pause_inside_idle_window_does_not_stop(world):
    docker_manager, client, clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", "running", container_id="short-pause")
    docker_manager.mark_sleeper_retired(CHAT, container, now=clock.time())
    clock.advance(100)
    docker_manager.reap_idle(CHAT, now=clock.time())
    assert docker_manager.read_idle_state(CHAT)["status"] == "running"
    container.status = "paused"
    docker_manager.reap_idle(CHAT, now=clock.time())
    paused_state = docker_manager.read_idle_state(CHAT)
    assert paused_state["status"] == "paused"
    assert container._stopped["value"] is False
    clock.advance(90)
    docker_manager.reap_idle(CHAT, now=clock.time())
    container.status = "running"
    docker_manager.reap_idle(CHAT, now=clock.time())
    assert container.status == "running"
    fresh = docker_manager.read_idle_state(CHAT)
    assert fresh["idle_expiry"] == clock.time() + 600
    docker_manager.reap_idle(CHAT, now=clock.time() + 599)
    assert container.status == "running"
    docker_manager.reap_idle(CHAT, now=clock.time() + 600)
    assert container.status == "exited"


def test_lock_open_failure_releases_thread_lock_for_other_thread(world, monkeypatch):
    docker_manager, _client, _clock, tmp_path = world
    original_open = Path.open
    failures = {"n": 0}

    def open_maybe(self, *args, **kwargs):
        if self.name == ".lifecycle.lock" and failures["n"] == 0:
            failures["n"] += 1
            raise OSError("disk full")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_maybe)
    with pytest.raises(OSError):
        with docker_manager._combined_lock(CHAT):
            pass
    acquired = {"ok": False}

    def other():
        with docker_manager._combined_lock(CHAT):
            acquired["ok"] = True

    thread = threading.Thread(target=other)
    thread.start()
    thread.join(timeout=2)
    assert thread.is_alive() is False
    assert acquired["ok"] is True
    assert docker_manager._FLOCK_DEPTH.get(CHAT, 0) == 0


def test_bad_chat_does_not_starve_reaper_or_next_tick(world, monkeypatch):
    docker_manager, client, clock, _tmp = world
    first = _put(client, f"owui-chat-{CHAT}", "running", container_id="bad")
    second = _put(client, f"owui-chat-{CHAT_B}", "running", container_id="good")
    docker_manager.mark_sleeper_retired(CHAT, first, now=clock.time() - 601)
    docker_manager.mark_sleeper_retired(CHAT_B, second, now=clock.time() - 601)
    docker_manager.write_idle_state(CHAT, {**docker_manager.read_idle_state(CHAT), "idle_expiry": clock.time() - 1, "observed_at": clock.time() - 30})
    docker_manager.write_idle_state(CHAT_B, {**docker_manager.read_idle_state(CHAT_B), "idle_expiry": clock.time() - 1, "observed_at": clock.time() - 30})
    _meta(docker_manager, CHAT)
    _meta(docker_manager, CHAT_B)
    original_lookup = docker_manager._lookup_container

    def lookup(chat_id):
        if chat_id == CHAT:
            raise docker_sdk.errors.DockerException("engine blip")
        return original_lookup(chat_id)

    monkeypatch.setattr(docker_manager, "_lookup_container", lookup)
    docker_manager.reap_known_sandboxes(now=clock.time())
    assert second._stopped["value"] is True
    assert first._stopped["value"] is False

    monkeypatch.setattr(docker_manager, "_lookup_container", original_lookup)
    first.status = "running"
    docker_manager.write_idle_state(CHAT, {**docker_manager.read_idle_state(CHAT), "idle_expiry": clock.time() - 1, "observed_at": clock.time() - 30, "sleeper_retired_for": "bad", "container_id": "bad", "status": "running"})
    docker_manager.reap_known_sandboxes(now=clock.time())
    assert first._stopped["value"] is True


def test_idle_reaper_survives_startup_and_tick_errors(monkeypatch):
    import asyncio
    import app as loaded

    calls = {"startup": 0, "reap": 0}

    def boom_startup(now=None):
        calls["startup"] += 1
        raise RuntimeError("startup boom")

    def boom_reap(now=None):
        calls["reap"] += 1
        if calls["reap"] == 1:
            raise docker_sdk.errors.APIError(
                "tick boom",
                response=MagicMock(status_code=500, url="http://docker.test", reason="error"),
                explanation=b"boom",
            )

    monkeypatch.setattr(loaded, "startup_idle_sweep", boom_startup)
    monkeypatch.setattr(loaded, "reap_known_sandboxes", boom_reap)
    monkeypatch.setattr(loaded, "validate_idle_configuration", lambda *args, **kwargs: (600, 0.01))

    async def scenario():
        stop_idle = asyncio.Event()
        reaper = asyncio.create_task(loaded._idle_reaper(stop_idle))
        await asyncio.sleep(0.05)
        stop_idle.set()
        await asyncio.wait_for(asyncio.shield(reaper), timeout=1)

    asyncio.run(scenario())
    assert calls["startup"] == 1
    assert calls["reap"] >= 2


def test_exited_dead_network_is_repaired_without_deletion(world, monkeypatch):
    docker_manager, client, _clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", "exited", container_id="dead-net")
    container.attrs["NetworkSettings"]["Networks"] = {"old-compose": {}}
    dead = MagicMock()
    live = MagicMock()
    networks = {"old-compose": dead, "compose-net": live}

    def get_network(name):
        if name == "old-compose":
            raise NotFound(name)
        return networks[name]

    client.networks.get.side_effect = get_network
    monkeypatch.setattr(docker_manager, "_get_compose_network_name", lambda force_refresh=False: "compose-net")
    starts = {"n": 0}
    original_start = container.start.side_effect

    def start():
        starts["n"] += 1
        if starts["n"] == 1:
            raise APIError(
                "network not found",
                response=MagicMock(status_code=500, url="http://docker.test", reason="error"),
                explanation=b"network not found",
            )
        return original_start()

    container.start.side_effect = start
    repaired = {"called": False}
    original_fix = docker_manager._fix_dead_networks

    def fix(client_arg, container_arg):
        repaired["called"] = True
        original_fix(client_arg, container_arg)

    monkeypatch.setattr(docker_manager, "_fix_dead_networks", fix)
    assert docker_manager.launch_sandbox(CHAT) == {"state": "running"}
    assert repaired["called"] is True
    live.connect.assert_called()
    container.remove.assert_not_called()
    assert container._removed["value"] is False


def test_server_recreate_uses_metadata_skills_and_trusted_token_lookup(world, monkeypatch):
    docker_manager, client, _clock, _tmp = world
    _meta(docker_manager, CHAT, user_email="saved@example", user_name="Saved")
    emails = []

    def skills(email):
        emails.append(email)
        return []

    monkeypatch.setattr(docker_manager.skill_manager, "get_user_skills_sync", skills)
    monkeypatch.setattr(docker_manager, "MCP_TOKENS_URL", "http://tokens.example")
    monkeypatch.setattr(docker_manager, "MCP_TOKENS_API_KEY", "wrapper-key")

    async def fetch(email, url, key):
        assert email == "saved@example"
        assert url == "http://tokens.example"
        assert key == "wrapper-key"
        return "server-gitlab-token"

    monkeypatch.setattr(docker_manager, "_fetch_gitlab_token", fetch)
    docker_manager.current_user_email.set("attacker@example")
    docker_manager.current_gitlab_token.set("poison-request-token")
    body = docker_manager.launch_sandbox(CHAT, credential_source="server")
    assert body == {"state": "running"}
    assert emails == ["saved@example"]
    env = _env_of(client._created[0])
    assert env["GITLAB_TOKEN"] == "server-gitlab-token"
    assert "poison-request-token" not in env.values()
    assert env["GIT_AUTHOR_EMAIL"] == "saved@example"


@pytest.mark.parametrize("exit_code", [None, 1])
def test_retirement_none_or_nonzero_does_not_mint_evidence(world, exit_code):
    docker_manager, client, _clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", "running", container_id="retire-fail")

    def result(cmd, detach=False, user=None, **kwargs):
        container._execs.append({"cmd": cmd, "user": user})
        return MagicMock(exit_code=exit_code, output=(b"", b""))

    container.exec_run.side_effect = result
    with pytest.raises(docker_manager.MigrationRequired):
        docker_manager.retire_legacy_sleeper(CHAT, container)
    assert docker_manager.read_idle_state(CHAT) is None
    with pytest.raises(docker_manager.MigrationRequired):
        docker_manager.retire_legacy_sleeper(CHAT, container)
    assert docker_manager.read_idle_state(CHAT) is None
    assert container._execs[0]["user"] is None


def test_retirement_engine_refusal_does_not_mint_evidence(world):
    docker_manager, client, _clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", "running", container_id="retire-api")

    def boom(cmd, detach=False, user=None, **kwargs):
        raise APIError(
            "exec failed",
            response=MagicMock(status_code=409, url="http://docker.test", reason="paused"),
            explanation=b"paused",
        )

    container.exec_run.side_effect = boom
    with pytest.raises(docker_manager.MigrationRequired):
        docker_manager.retire_legacy_sleeper(CHAT, container)
    assert docker_manager.read_idle_state(CHAT) is None


def test_retirement_script_keeps_marker_until_child_dies(tmp_path):
    script = textwrap.dedent(
        r"""
        MARKER="$1"
        CHILD_PID="$2"
        OLD=$(cat "$MARKER" 2>/dev/null || true)
        if [ -z "$OLD" ]; then
            exit 0
        fi
        kill "$CHILD_PID" 2>/dev/null || true
        i=0
        while [ "$i" -lt 20 ]; do
            if ! kill -0 "$CHILD_PID" 2>/dev/null; then
                rm -f "$MARKER"
                exit 0
            fi
            i=$((i + 1))
            sleep 0.05
        done
        if kill -0 "$CHILD_PID" 2>/dev/null; then
            exit 1
        fi
        rm -f "$MARKER"
        exit 0
        """
    ).strip()
    marker = tmp_path / "shutdown-timer-pid"
    stubborn = tmp_path / "stubborn.py"
    stubborn.write_text(
        "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True:\n    time.sleep(0.05)\n"
    )
    alive = subprocess.Popen([sys.executable, str(stubborn)])
    marker.write_text(str(alive.pid))
    try:
        failed = subprocess.run(
            ["bash", "-c", script, "retire", str(marker), str(alive.pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert failed.returncode == 1
        assert marker.exists()
    finally:
        alive.send_signal(9)
        alive.wait(timeout=2)
    dying = subprocess.Popen([sys.executable, "-c", "pass"])
    dying.wait(timeout=2)
    marker.write_text(str(dying.pid))
    try:
        ok = subprocess.run(
            ["bash", "-c", script, "retire", str(marker), str(dying.pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        dying.wait(timeout=2)
        assert ok.returncode == 0
        assert marker.exists() is False
    finally:
        if dying.poll() is None:
            dying.kill()
            dying.wait(timeout=2)


def test_threads_share_one_chat_lock_and_serialize_create(world):
    docker_manager, client, _clock, _tmp = world
    barrier = threading.Barrier(2)
    order = []

    original_create = client.containers.create.side_effect

    def create(**config):
        order.append(threading.get_ident())
        time.sleep(0.05)
        return original_create(**config)

    client.containers.create.side_effect = create
    found = []

    def worker():
        barrier.wait(timeout=5)
        found.append(docker_manager._get_or_create_container(CHAT))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert len(client._created) == 1
    assert found[0] is found[1]
    assert docker_manager.get_chat_lock(CHAT) is docker_manager.get_chat_lock(CHAT.upper())


def test_broken_flock_negative_control_allows_two_create_attempts(tmp_path):
    source = Path(__file__).read_text()
    start = source.index("script = r'''") + len("script = r'''")
    end = source.index("'''", start)
    child_script = source[start:end]
    shared = tmp_path / "shared"
    shared.mkdir()
    env = os.environ.copy()
    pythonpath = env.get("PYTHONPATH", "")
    env.update(
        {
            "OCU_SERVER_DIR": str(SERVER_DIR),
            "OCU_BASE": str(tmp_path / "data"),
            "OCU_USER": str(tmp_path / "user"),
            "OCU_SHARED": str(shared),
            "OCU_CHAT": CHAT,
            "PUBLIC_BASE_URL": "/ocu",
            "OCU_BROKEN_FLOCK": "1",
            "PYTHONPATH": str(SERVER_DIR) + (os.pathsep + pythonpath if pythonpath else ""),
        }
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", child_script],
            env=env,
            cwd=str(SERVER_DIR),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    outputs = [proc.communicate(timeout=30) for proc in procs]
    assert any(proc.returncode == 0 for proc in procs), [(item[0], item[1]) for item in outputs]
    attempts = (shared / "attempts.jsonl").read_text().strip().splitlines()
    assert len(attempts) == 2


def test_record_heartbeat_extends_running_idle_only(world):
    docker_manager, client, clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", "running", container_id="hb-1")
    docker_manager.mark_sleeper_retired(CHAT, container, now=clock.time())
    clock.advance(30)
    docker_manager.record_heartbeat(CHAT, now=clock.time())
    assert docker_manager.read_idle_state(CHAT)["idle_expiry"] == clock.time() + 600
    container.status = "exited"
    clock.advance(10)
    docker_manager.record_heartbeat(CHAT, now=clock.time())
    assert docker_manager.read_idle_state(CHAT)["idle_expiry"] == clock.time() + 590


def test_heartbeat_route_requires_token_and_extends_idle(app_module, monkeypatch, tmp_path):
    _apply_env(monkeypatch, tmp_path)
    import docker_manager

    client = _docker()
    docker_manager.BASE_DATA_DIR = tmp_path / "data"
    docker_manager._docker_client = None
    monkeypatch.setattr(docker_manager, "get_docker_client", lambda: client)
    monkeypatch.setattr(app_module, "record_heartbeat", docker_manager.record_heartbeat)
    container = _put(client, f"owui-chat-{CHAT}", "running", container_id="route-hb")
    docker_manager.mark_sleeper_retired(CHAT, container, now=1_700_000_000.0)
    http = app_module._lifecycle_client
    missing = http.get(f"/terminal/{CHAT}/heartbeat")
    assert missing.status_code == 401
    headers = {"Authorization": f"Bearer {INTERNAL}"}
    ok = http.get(f"/terminal/{CHAT}/heartbeat", headers=headers)
    assert ok.status_code == 200
    assert ok.json() == {"ok": True}
    assert docker_manager.read_idle_state(CHAT)["idle_expiry"] >= 1_700_000_000.0 + 600


def test_heartbeat_and_expiry_share_lock_order(world):
    docker_manager, client, clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", "running", container_id="lock-order")
    docker_manager.mark_sleeper_retired(CHAT, container, now=clock.time())
    expiry = docker_manager.read_idle_state(CHAT)["idle_expiry"]
    events = []
    ready = threading.Event()
    go = threading.Event()

    def holder():
        with docker_manager._combined_lock(CHAT):
            events.append("reaper-acquired")
            ready.set()
            go.wait(timeout=2)
            events.append("reaper-done")

    def heartbeat():
        ready.wait(timeout=2)
        events.append("heartbeat-waiting")
        docker_manager.record_heartbeat(CHAT, now=expiry - 1)
        events.append("heartbeat-done")

    first = threading.Thread(target=holder)
    second = threading.Thread(target=heartbeat)
    first.start()
    assert ready.wait(timeout=2)
    second.start()
    time.sleep(0.05)
    assert "heartbeat-done" not in events
    go.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert events[:2] == ["reaper-acquired", "heartbeat-waiting"]
    assert events[-2:] == ["reaper-done", "heartbeat-done"] or events[-2:] == ["heartbeat-done", "reaper-done"]
    docker_manager.reap_idle(CHAT, now=expiry)
    assert container.status == "running"


def test_restart_wait_uses_monotonic_deadline(world, monkeypatch):
    docker_manager, client, _clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", "restarting")
    monotonic = {"now": 0.0}
    monkeypatch.setattr(docker_manager.time, "monotonic", lambda: monotonic["now"])

    def reload():
        monotonic["now"] += 2.0
        container.attrs["State"]["Status"] = container.status

    container.reload.side_effect = reload
    with pytest.raises(docker_manager.LaunchFailed) as caught:
        docker_manager.launch_sandbox(CHAT)
    assert caught.value.status_code == 504
    assert monotonic["now"] >= docker_manager.RESTART_WAIT_SECONDS
    assert monotonic["now"] <= docker_manager.RESTART_WAIT_SECONDS + 6
    container.remove.assert_not_called()


def test_unpause_and_lookup_failures_are_structured(world):
    docker_manager, client, clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", "paused", container_id="unpause-fail")
    docker_manager.mark_sleeper_retired(CHAT, container, now=clock.time())
    container.unpause.side_effect = APIError(
        "cannot unpause",
        response=MagicMock(status_code=500, url="http://docker.test", reason="error"),
        explanation=b"cannot unpause",
    )
    with pytest.raises(docker_manager.LaunchFailed) as caught:
        docker_manager.launch_sandbox(CHAT)
    assert caught.value.reason == "launch_failed"
    assert "token" not in str(caught.value).lower()
    assert container.status == "paused"


@pytest.mark.parametrize(
    "tool_name",
    ["view", "bash_tool", "str_replace", "create_file", "sub_agent"],
)
def test_mcp_tools_surface_stopped_and_corrupt_meta(monkeypatch, tool_name):
    import mcp_tools

    monkeypatch.setattr(mcp_tools, "_validate_chat_id", lambda: (CHAT, None))
    monkeypatch.setattr(mcp_tools, "_ensure_gitlab_token", lambda: _async_none())
    monkeypatch.setattr(
        mcp_tools,
        "_get_or_create_container",
        lambda chat_id: (_ for _ in ()).throw(mcp_tools.SandboxStopped(STOPPED_MESSAGE)),
    )
    stopped = _run(_mcp_tool(mcp_tools, tool_name))
    assert "workspace is stopped" in stopped

    def corrupt(chat_id):
        raise mcp_tools.LifecycleError("sandbox metadata is corrupt")

    monkeypatch.setattr(mcp_tools, "_get_or_create_container", corrupt)
    result = _run(_mcp_tool(mcp_tools, tool_name))
    assert result.startswith("Error:")
    assert "metadata" in result.lower()
    assert "corrupt" in result.lower()
