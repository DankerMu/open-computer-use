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
    docker_manager._docker_client = None
    docker_manager._chat_locks.clear()
    client = _docker()
    monkeypatch.setattr(docker_manager, "get_docker_client", lambda: client)
    monkeypatch.setattr(docker_manager, "render_system_prompt_sync", lambda *args, **kwargs: "readme")
    monkeypatch.setattr(docker_manager, "_get_compose_network_name", lambda force_refresh=False: None)
    monkeypatch.setattr(docker_manager.skill_manager, "get_user_skills_sync", lambda email: [])
    monkeypatch.setattr(docker_manager.skill_manager, "get_skill_mounts", lambda skills: {})
    clock = Clock()
    monkeypatch.setattr(docker_manager.time, "time", clock.time)
    monkeypatch.setattr(docker_manager.time, "sleep", lambda _seconds: None)
    return docker_manager, client, clock, tmp_path


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

    loaded.LifecycleError = docker_manager.LifecycleError
    loaded.startup_idle_sweep = lambda now=None: None
    loaded.reap_known_sandboxes = lambda now=None: None
    loaded.validate_idle_configuration = lambda *args, **kwargs: (600, 30)
    with TestClient(loaded.app) as client:
        loaded._lifecycle_client = client
        try:
            yield loaded
        finally:
            for key in ("OCU_INTERNAL_TOKEN", "PUBLIC_BASE_URL", "MCP_API_KEY"):
                os.environ.pop(key, None)




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
    docker_manager.note_running_activity(CHAT, container, now=clock.time())
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
    docker_manager.ANTHROPIC_AUTH_TOKEN = "server-fallback-token"
    docker_manager.current_gitlab_token.set("request-token-must-not-enter")
    docker_manager.current_anthropic_auth_token.set("request-anthropic-must-not-enter")
    body = docker_manager.launch_sandbox(CHAT, credential_source="server")
    assert body == {"state": "running"}
    env = _env_of(client._created[0])
    assert env["ANTHROPIC_AUTH_TOKEN"] == "server-fallback-token"
    assert "request-token-must-not-enter" not in env.values()
    assert "request-anthropic-must-not-enter" not in env.values()
    assert env["GIT_AUTHOR_EMAIL"] == "saved@example"


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
import json, os, sys, threading
from pathlib import Path
from unittest.mock import MagicMock
sys.path.insert(0, os.environ["OCU_SERVER_DIR"])
os.environ["BASE_DATA_DIR"] = os.environ["OCU_BASE"]
os.environ["USER_DATA_BASE_PATH"] = os.environ["OCU_USER"]
os.environ["DOCKER_IMAGE"] = "python:3.12-slim"
import docker
import docker_manager

docker.errors.NotFound = type("NotFound", (Exception,), {})
class _Response:
    status_code = 409
    reason = "Conflict"
    url = "http://docker.test"
def raise_conflict():
    raise docker.errors.APIError("conflict", response=_Response(), explanation=b"conflict")
root = Path(os.environ["OCU_SHARED"])
store = root / "containers.json"
lock_path = root / "engine.lock"

def mutate(fn):
    import fcntl
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        data = json.loads(store.read_text()) if store.exists() else {}
        result = fn(data)
        store.write_text(json.dumps(data))
        return result

client = MagicMock()
created = []
def get(name):
    data = json.loads(store.read_text()) if store.exists() else {}
    if name not in data:
        raise docker.errors.NotFound(name)
    container = MagicMock()
    container.name = name
    container.id = data[name]
    container.status = "running"
    container.attrs = {"Id": container.id, "State": {"Status": "running"}}
    container.reload.return_value = None
    return container
def create(**config):
    def op(data):
        if config["name"] in data:
            raise_conflict()
        data[config["name"]] = "shared-cid"
        return "shared-cid"
    mutate(op)
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
docker_manager._get_or_create_container(os.environ["OCU_CHAT"])
print(json.dumps({"created": created, "store": json.loads(store.read_text())}))
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


def test_mcp_stopped_error_is_workspace_stopped(monkeypatch):
    import mcp_tools

    monkeypatch.setattr(mcp_tools, "_validate_chat_id", lambda: (CHAT, None))
    monkeypatch.setattr(mcp_tools, "_ensure_gitlab_token", lambda: _async_none())
    monkeypatch.setattr(
        mcp_tools,
        "_get_or_create_container",
        lambda chat_id: (_ for _ in ()).throw(mcp_tools.SandboxStopped(STOPPED_MESSAGE)),
    )
    result = _run(mcp_tools.view("inspect", "/tmp/file", None, None))
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