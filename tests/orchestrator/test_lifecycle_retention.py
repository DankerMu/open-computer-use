# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Stopped-sandbox retention, SANDBOX_RETENTION_DAYS and internal deletion (#698).

Docker is the injected fake from test_lifecycle plus a volume store. Expected
values come from the ocu-lifecycle spec: retention ages a stopped sandbox from
State.FinishedAt (Created when FinishedAt is the zero time) and keeps the chat
files; deletion leaves only .lifecycle.lock and .deleted; a tombstoned chat is
refused with 410 chat_deleted.
"""

from __future__ import annotations

import datetime
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

from tests.orchestrator._office_store import ROOT, _child_env, _stop_child
from tests.orchestrator.test_lifecycle import (  # noqa: F401  (fixtures)
    APIError,
    CHAT,
    CHAT_B,
    INTERNAL,
    MCP_KEY,
    NotFound,
    SERVER_DIR,
    _INTERPRETER_ENV,
    _docker,
    _meta,
    _put,
    app_module,
    world,
)

DAY = 86400
RETENTION = 30 * DAY
HEADERS = {"Authorization": f"Bearer {INTERNAL}"}
ZERO_TIME = "0001-01-01T00:00:00Z"
CHAT_DELETED = "chat_deleted"
DELETE_FAILED = "delete_failed"


class ProcessStopped(BaseException):
    """Stands in for the server process dying at an engine call."""


def _iso(epoch: float) -> str:
    """Docker's own RFC 3339 form, nanosecond fraction included."""
    stamp = datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S") + ".123456789Z"


def _volumes(client):
    # A MagicMock invents any missing attribute; look in the instance dict.
    store = vars(client).get("_volumes")
    if store is not None:
        return store
    store = {}

    def get(name):
        if name not in store:
            raise NotFound(name)
        return store[name]

    client.volumes.get.side_effect = get
    client._volumes = store
    return store


def _volume(client, chat=CHAT):
    store = _volumes(client)
    name = f"chat-{chat}-workspace"
    volume = MagicMock(name=name)
    volume.name = name

    def remove(force=False):
        store.pop(name, None)

    volume.remove.side_effect = remove
    store[name] = volume
    return volume


def _sandbox(client, status, *, finished=None, created=None, chat=CHAT, cid="cid-old"):
    container = _put(client, f"owui-chat-{chat}", status, container_id=cid)
    if finished is not None:
        container.attrs["State"]["FinishedAt"] = finished
    if created is not None:
        container.attrs["Created"] = created

    def remove(force=False):
        container._removed["value"] = True
        client._store.pop(container.name, None)

    container.remove.side_effect = remove
    return container


def _outputs_file(docker_manager, chat=CHAT, name="report.txt", body=b"kept bytes"):
    path = docker_manager.BASE_DATA_DIR / chat / "outputs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _entries(docker_manager, chat=CHAT):
    return sorted(os.listdir(docker_manager.BASE_DATA_DIR / chat))


def _inode(path: Path):
    info = path.lstat()
    return info.st_dev, info.st_ino


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def test_retention_removes_container_and_volume_after_period_and_keeps_files(world):
    docker_manager, client, clock, _tmp = world
    meta = _meta(docker_manager, CHAT)
    output = _outputs_file(docker_manager)
    container = _sandbox(client, "exited", finished=_iso(clock.now - RETENTION - 1))
    _volume(client)

    docker_manager.reap_idle(CHAT, now=clock.now)

    container.remove.assert_called_once_with()
    assert f"owui-chat-{CHAT}" not in client._store
    assert client._volumes == {}
    assert output.read_bytes() == b"kept bytes"
    assert meta.is_file()
    assert "retained_at" not in (docker_manager.read_idle_state(CHAT) or {})


def test_retention_keeps_sandbox_inside_period(world):
    docker_manager, client, clock, _tmp = world
    _meta(docker_manager, CHAT)
    container = _sandbox(client, "exited", finished=_iso(clock.now - RETENTION + 1))
    volume = _volume(client)

    docker_manager.reap_idle(CHAT, now=clock.now)

    container.remove.assert_not_called()
    volume.remove.assert_not_called()
    assert client._store[f"owui-chat-{CHAT}"] is container


@pytest.mark.parametrize(("created_age", "removed"), ((31 * DAY, True), (29 * DAY, False)))
def test_zero_finished_at_is_aged_from_created(world, created_age, removed):
    docker_manager, client, clock, _tmp = world
    _meta(docker_manager, CHAT)
    container = _sandbox(
        client, "created", finished=ZERO_TIME, created=_iso(clock.now - created_age))
    _volume(client)

    docker_manager.reap_idle(CHAT, now=clock.now)

    assert container._removed["value"] is removed
    assert (client._volumes == {}) is removed


@pytest.mark.parametrize("status", ("running", "paused", "restarting", "dead"))
def test_retention_never_removes_live_or_dead_sandbox(world, status):
    docker_manager, client, clock, _tmp = world
    _meta(docker_manager, CHAT)
    ancient = _iso(clock.now - 365 * DAY)
    container = _sandbox(client, status, finished=ancient, created=ancient)
    volume = _volume(client)

    docker_manager.reap_idle(CHAT, now=clock.now)

    container.remove.assert_not_called()
    volume.remove.assert_not_called()
    assert client._store[f"owui-chat-{CHAT}"] is container


@pytest.mark.parametrize(
    ("finished", "created"),
    (
        ("not-a-time", None),
        (None, None),
        ("2023-13-45T99:00:00Z", None),
        (ZERO_TIME, "garbage"),
        (ZERO_TIME, None),
    ),
)
def test_unparsable_timestamp_keeps_sandbox(world, finished, created):
    docker_manager, client, clock, _tmp = world
    _meta(docker_manager, CHAT)
    container = _sandbox(client, "exited", finished=finished, created=created)
    volume = _volume(client)

    docker_manager.reap_idle(CHAT, now=clock.now)

    container.remove.assert_not_called()
    volume.remove.assert_not_called()


@pytest.mark.parametrize("change", ("restarted", "replaced"))
def test_revalidation_under_lock_keeps_a_changed_sandbox(world, change):
    docker_manager, client, clock, _tmp = world
    _meta(docker_manager, CHAT)
    old = _iso(clock.now - RETENTION - DAY)
    container = _sandbox(client, "exited", finished=old)
    replacement = _sandbox(client, "exited", finished=old, cid="cid-replacement")
    client._store[container.name] = container
    volume = _volume(client)
    lookups = {"n": 0}
    real_get = client.containers.get.side_effect

    def get(name):
        lookups["n"] += 1
        if lookups["n"] == 2:
            if change == "restarted":
                container.status = "running"
            else:
                client._store[name] = replacement
        return real_get(name)

    client.containers.get.side_effect = get

    docker_manager.reap_idle(CHAT, now=clock.now)

    assert lookups["n"] >= 2
    container.remove.assert_not_called()
    replacement.remove.assert_not_called()
    volume.remove.assert_not_called()
    assert "retained_at" not in (docker_manager.read_idle_state(CHAT) or {})


def test_retained_at_is_persisted_before_container_remove(world):
    docker_manager, client, clock, _tmp = world
    _meta(docker_manager, CHAT)
    container = _sandbox(client, "exited", finished=_iso(clock.now - RETENTION - 1))
    _volume(client)
    seen = []

    def remove(force=False):
        seen.append((docker_manager.read_idle_state(CHAT) or {}).get("retained_at"))
        client._store.pop(container.name, None)

    container.remove.side_effect = remove

    docker_manager.reap_idle(CHAT, now=clock.now)

    assert seen == [clock.now]


@pytest.mark.parametrize("failure", ("engine_error", "process_stop"))
def test_volume_removal_is_completed_on_a_later_tick(world, failure):
    docker_manager, client, clock, _tmp = world
    _meta(docker_manager, CHAT)
    container = _sandbox(client, "exited", finished=_iso(clock.now - RETENTION - 1))
    volume = _volume(client)
    attempts = {"n": 0}
    real_get = client.volumes.get.side_effect

    def failing_get(name):
        attempts["n"] += 1
        if attempts["n"] == 1:
            if failure == "process_stop":
                raise ProcessStopped()
            response = MagicMock(status_code=500, url="http://docker.test", reason="error")
            raise APIError("volume busy", response=response, explanation=b"busy")
        return real_get(name)

    client.volumes.get.side_effect = failing_get
    expected = ProcessStopped if failure == "process_stop" else APIError
    with pytest.raises(expected):
        docker_manager.reap_idle(CHAT, now=clock.now)
    assert container._removed["value"] is True
    assert client._volumes == {volume.name: volume}
    assert docker_manager.read_idle_state(CHAT)["retained_at"] == clock.now

    docker_manager.reap_idle(CHAT, now=clock.advance(30))

    assert client._volumes == {}
    assert "retained_at" not in docker_manager.read_idle_state(CHAT)


def test_failed_container_remove_clears_the_retention_marker(world):
    docker_manager, client, clock, _tmp = world
    _meta(docker_manager, CHAT)
    container = _sandbox(client, "exited", finished=_iso(clock.now - RETENTION - 1))
    volume = _volume(client)
    response = MagicMock(status_code=409, url="http://docker.test", reason="conflict")
    container.remove.side_effect = APIError(
        "cannot remove a running container", response=response, explanation=b"running")

    with pytest.raises(APIError):
        docker_manager.reap_idle(CHAT, now=clock.now)

    assert "retained_at" not in (docker_manager.read_idle_state(CHAT) or {})
    assert client._store[container.name] is container
    # An operator's later `docker rm` must not let the reaper take the volume.
    client._store.pop(container.name)
    docker_manager.reap_idle(CHAT, now=clock.advance(30))
    volume.remove.assert_not_called()
    assert client._volumes == {volume.name: volume}


@pytest.mark.parametrize("form", ("offset", "whole_seconds"))
@pytest.mark.parametrize(("age", "removed"), ((RETENTION + 1, True), (RETENTION - 1, False)))
def test_offset_and_whole_second_timestamps_are_aged_exactly(world, form, age, removed):
    docker_manager, client, clock, _tmp = world
    _meta(docker_manager, CHAT)
    finished_utc = datetime.datetime.fromtimestamp(int(clock.now - age), datetime.timezone.utc)
    if form == "offset":
        zone = datetime.timezone(datetime.timedelta(hours=8))
        finished = finished_utc.astimezone(zone).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    else:
        finished = finished_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    container = _sandbox(client, "exited", finished=finished)
    _volume(client)

    docker_manager.reap_idle(CHAT, now=clock.now)

    assert container._removed["value"] is removed


@pytest.mark.parametrize("failure", ("not_found", "timeout_after_engine_removed"))
def test_remove_failure_with_container_gone_still_removes_the_volume(world, failure):
    import requests

    docker_manager, client, clock, _tmp = world
    _meta(docker_manager, CHAT)
    container = _sandbox(client, "exited", finished=_iso(clock.now - RETENTION - 1))
    _volume(client)

    def remove(force=False):
        client._store.pop(container.name, None)
        if failure == "not_found":
            raise NotFound("removed elsewhere")
        raise requests.exceptions.ReadTimeout("engine answered after the client timeout")

    container.remove.side_effect = remove

    docker_manager.reap_idle(CHAT, now=clock.now)
    docker_manager.reap_idle(CHAT, now=clock.advance(30))

    assert client._volumes == {}
    assert "retained_at" not in docker_manager.read_idle_state(CHAT)


def test_absent_container_without_retention_marker_keeps_its_volume(world):
    docker_manager, client, clock, _tmp = world
    _meta(docker_manager, CHAT)
    volume = _volume(client)

    docker_manager.reap_idle(CHAT, now=clock.now)

    volume.remove.assert_not_called()
    assert client._volumes == {volume.name: volume}


def test_tool_call_after_retention_recreates_sandbox_over_the_old_files(world):
    docker_manager, client, clock, _tmp = world
    _meta(docker_manager, CHAT, user_email="saved@example")
    output = _outputs_file(docker_manager)
    _sandbox(client, "exited", finished=_iso(clock.now - RETENTION - 1))
    _volume(client)
    docker_manager.reap_idle(CHAT, now=clock.now)
    assert f"owui-chat-{CHAT}" not in client._store

    recreated = docker_manager._get_or_create_container(CHAT)

    assert recreated.status == "running"
    assert client._created == [recreated]
    volumes = recreated._create_config["volumes"]
    source = next(src for src, spec in volumes.items() if spec["bind"] == "/mnt/user-data/files")
    assert Path(source) == output.parent
    assert (Path(source) / output.name).read_bytes() == b"kept bytes"


# ---------------------------------------------------------------------------
# SANDBOX_RETENTION_DAYS
# ---------------------------------------------------------------------------


def test_retention_days_default_and_valid_value(world, monkeypatch):
    docker_manager = world[0]
    monkeypatch.delenv("SANDBOX_RETENTION_DAYS", raising=False)
    assert docker_manager.configured_retention_days() == 30
    monkeypatch.setenv("SANDBOX_RETENTION_DAYS", "45")
    assert docker_manager.configured_retention_days() == 45


@pytest.mark.parametrize("raw", ("abc", "0", "-1", "", " 30", "+3", "1.5", "30d"))
def test_retention_days_invalid_value_is_refused(world, monkeypatch, raw):
    docker_manager = world[0]
    monkeypatch.setenv("SANDBOX_RETENTION_DAYS", raw)
    with pytest.raises(docker_manager.LifecycleConfigError) as refused:
        docker_manager.configured_retention_days()
    assert "SANDBOX_RETENTION_DAYS" in str(refused.value)


def test_configured_retention_days_drive_the_reaper(world, monkeypatch):
    docker_manager, client, clock, _tmp = world
    monkeypatch.setenv("SANDBOX_RETENTION_DAYS", "2")
    _meta(docker_manager, CHAT)
    container = _sandbox(client, "exited", finished=_iso(clock.now - 2 * DAY - 1))
    _volume(client)

    docker_manager.reap_idle(CHAT, now=clock.now)

    assert container._removed["value"] is True


def test_lifespan_refuses_invalid_retention_days_and_accepts_unset(tmp_path):
    script = textwrap.dedent(
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
        import docker_manager
        import app

        app.startup_preflight = lambda: 0
        app.startup_idle_sweep = lambda now=None: None
        app.reap_known_sandboxes = lambda now=None: None
        app.sweep_office_publications = lambda *a, **k: None
        outcomes = {}
        for raw in ("abc", "0", "-1", ""):
            os.environ["SANDBOX_RETENTION_DAYS"] = raw
            try:
                with TestClient(app.app):
                    outcomes[raw] = "started"
            except docker_manager.LifecycleConfigError:
                outcomes[raw] = "refused"
        os.environ.pop("SANDBOX_RETENTION_DAYS")
        with TestClient(app.app):
            outcomes["unset"] = "started"
        print(json.dumps(outcomes))
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(SERVER_DIR),
        env={
            **{name: os.environ[name] for name in _INTERPRETER_ENV if name in os.environ},
            "OCU_SERVER_DIR": str(SERVER_DIR),
            "OCU_INTERNAL_TOKEN": INTERNAL,
            "MCP_API_KEY": MCP_KEY,
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
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        "abc": "refused", "0": "refused", "-1": "refused", "": "refused", "unset": "started",
    }


# ---------------------------------------------------------------------------
# DELETE /internal/sandbox/{chat_id}
# ---------------------------------------------------------------------------


def _bind_upload_root(docker_manager, monkeypatch):
    # uploads reads BASE_DATA_DIR at import; point discovery at this test's root.
    import uploads

    monkeypatch.setattr(uploads, "BASE_DATA_DIR", docker_manager.BASE_DATA_DIR)


@pytest.fixture
def http(app_module, world, monkeypatch):
    docker_manager = world[0]
    monkeypatch.setattr(app_module, "delete_sandbox", docker_manager.delete_sandbox)
    monkeypatch.setattr(app_module, "launch_sandbox", docker_manager.launch_sandbox)
    monkeypatch.setattr(app_module, "describe_sandbox", docker_manager.describe_sandbox)
    monkeypatch.setattr(app_module, "BASE_DATA_DIR", docker_manager.BASE_DATA_DIR)
    _bind_upload_root(docker_manager, monkeypatch)
    monkeypatch.setattr(
        docker_manager,
        "cli_badge",
        lambda: {"cli": "claude", "default_model": "sonnet", "supports_cost": True},
    )
    return app_module._lifecycle_client


def _populated_running_chat(docker_manager, client, chat=CHAT):
    _meta(docker_manager, chat)
    output = _outputs_file(docker_manager, chat)
    container = _sandbox(client, "running", chat=chat, cid="cid-running")
    docker_manager.note_running_activity(chat, container)
    office = docker_manager.BASE_DATA_DIR / chat / ".office" / "state.json"
    office.parent.mkdir(parents=True)
    office.write_text("{}")
    _volume(client, chat)
    return container, output


@pytest.mark.parametrize(
    ("path", "headers", "status"),
    (
        (f"/internal/sandbox/{CHAT}", {}, 401),
        (f"/internal/sandbox/{CHAT}", {"Authorization": "Bearer not-the-internal-token"}, 401),
        ("/internal/sandbox/default", HEADERS, 400),
        ("/internal/sandbox/local:x", HEADERS, 400),
        ("/internal/sandbox/%2E%2E", HEADERS, 400),
    ),
)
def test_delete_refuses_before_engine_or_filesystem(http, world, monkeypatch, path, headers, status):
    docker_manager, client, _clock, tmp_path = world
    _populated_running_chat(docker_manager, client)
    before = sorted(str(item) for item in tmp_path.rglob("*"))
    engine = []

    def fail(*_args, **_kwargs):
        engine.append(True)
        raise AssertionError("docker was reached")

    monkeypatch.setattr(docker_manager, "get_docker_client", fail)

    response = http.delete(path, headers=headers)

    assert response.status_code == status
    assert engine == []
    assert sorted(str(item) for item in tmp_path.rglob("*")) == before


def test_delete_running_sandbox_leaves_only_lock_and_tombstone(http, world):
    import mcp_resources

    docker_manager, client, _clock, _tmp = world
    container, _output = _populated_running_chat(docker_manager, client)
    assert mcp_resources.sync_chat_resources_sync(CHAT) == 1
    assert CHAT in mcp_resources._chat_uris
    with docker_manager._combined_lock(CHAT):
        pass
    lock = docker_manager.BASE_DATA_DIR / CHAT / ".lifecycle.lock"
    lock_inode = _inode(lock)

    response = http.delete(f"/internal/sandbox/{CHAT}", headers=HEADERS)

    assert response.status_code == 200
    assert response.json() == {"container": True, "volume": True, "data": True}
    container.remove.assert_called_once_with(force=True)
    assert client._store == {}
    assert client._volumes == {}
    assert _entries(docker_manager) == [".deleted", ".lifecycle.lock"]
    assert _inode(lock) == lock_inode
    assert CHAT not in mcp_resources._chat_uris
    assert not any(CHAT in uri for uri in mcp_resources.mcp._resource_manager._resources)
    assert CHAT not in docker_manager.list_reap_candidates()

    again = http.delete(f"/internal/sandbox/{CHAT}", headers=HEADERS)

    assert again.status_code == 200
    assert again.json() == {"container": False, "volume": False, "data": False}
    assert _entries(docker_manager) == [".deleted", ".lifecycle.lock"]
    assert _inode(lock) == lock_inode


def test_delete_of_never_seen_chat_writes_tombstone_that_refuses_first_tool_call(http, world):
    docker_manager, client, _clock, _tmp = world
    _volumes(client)
    assert not (docker_manager.BASE_DATA_DIR / CHAT_B).exists()

    response = http.delete(f"/internal/sandbox/{CHAT_B}", headers=HEADERS)

    assert response.status_code == 200
    assert response.json() == {"container": False, "volume": False, "data": False}
    assert _entries(docker_manager, CHAT_B) == [".deleted", ".lifecycle.lock"]
    with pytest.raises(docker_manager.LifecycleError) as refused:
        docker_manager._get_or_create_container(CHAT_B)
    assert (refused.value.status_code, refused.value.reason) == (410, CHAT_DELETED)
    assert client._created == []
    assert _entries(docker_manager, CHAT_B) == [".deleted", ".lifecycle.lock"]


def test_tombstoned_chat_refuses_launch_describe_outputs_write_and_listing(http, world):
    docker_manager, client, _clock, _tmp = world
    _populated_running_chat(docker_manager, client)
    assert http.delete(f"/internal/sandbox/{CHAT}", headers=HEADERS).status_code == 200

    launch = http.post(f"/internal/launch/{CHAT}", headers=HEADERS)
    describe = http.get(f"/internal/describe/{CHAT}", headers=HEADERS)
    upload = http.post(
        f"/api/uploads/{CHAT}/note.txt", headers=HEADERS, files={"file": ("note.txt", b"x")})
    listing = http.get(f"/api/outputs/{CHAT}", headers=HEADERS)

    for response in (launch, describe, upload, listing):
        assert response.status_code == 410, response.text
        assert response.json()["detail"]["reason"] == CHAT_DELETED
    assert client._created == []
    assert _entries(docker_manager) == [".deleted", ".lifecycle.lock"]


def test_delete_engine_failure_is_delete_failed_and_retry_completes(http, world):
    docker_manager, client, _clock, _tmp = world
    _populated_running_chat(docker_manager, client)
    volume = client._volumes[f"chat-{CHAT}-workspace"]
    attempts = {"n": 0}

    def flaky_remove(force=False):
        attempts["n"] += 1
        if attempts["n"] == 1:
            response = MagicMock(status_code=500, url="http://docker.test", reason="error")
            raise APIError("volume in use", response=response, explanation=b"in use")
        client._volumes.pop(volume.name, None)

    volume.remove.side_effect = flaky_remove

    failed = http.delete(f"/internal/sandbox/{CHAT}", headers=HEADERS)

    assert failed.status_code == 500
    assert failed.json()["detail"]["reason"] == DELETE_FAILED
    assert "volume" in failed.json()["detail"]["detail"]

    retried = http.delete(f"/internal/sandbox/{CHAT}", headers=HEADERS)

    assert retried.status_code == 200
    assert retried.json() == {"container": False, "volume": True, "data": True}
    assert client._volumes == {}
    assert _entries(docker_manager) == [".deleted", ".lifecycle.lock"]


def test_failed_file_removal_leaves_tombstone_and_retry_completes(http, world, monkeypatch):
    docker_manager, client, _clock, _tmp = world
    _populated_running_chat(docker_manager, client)
    real_rmtree = docker_manager.shutil.rmtree
    attempts = {"n": 0}

    def flaky_rmtree(path, *args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError(5, "chat file removal failed")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(docker_manager.shutil, "rmtree", flaky_rmtree)

    failed = http.delete(f"/internal/sandbox/{CHAT}", headers=HEADERS)

    assert failed.status_code == 500
    assert failed.json()["detail"]["reason"] == DELETE_FAILED
    assert "chat file removal" in failed.json()["detail"]["detail"]
    assert ".deleted" in _entries(docker_manager)
    launch = http.post(f"/internal/launch/{CHAT}", headers=HEADERS)
    assert launch.status_code == 410
    assert launch.json()["detail"]["reason"] == CHAT_DELETED
    with pytest.raises(docker_manager.LifecycleError) as refused:
        docker_manager._get_or_create_container(CHAT)
    assert refused.value.reason == CHAT_DELETED
    assert client._created == []

    retried = http.delete(f"/internal/sandbox/{CHAT}", headers=HEADERS)

    assert retried.status_code == 200
    assert retried.json() == {"container": False, "volume": False, "data": True}
    assert _entries(docker_manager) == [".deleted", ".lifecycle.lock"]
    clean = http.delete(f"/internal/sandbox/{CHAT}", headers=HEADERS)
    assert clean.json() == {"container": False, "volume": False, "data": False}


def test_busy_resource_discovery_does_not_fail_a_completed_delete(http, world):
    import mcp_resources

    docker_manager, client, _clock, _tmp = world
    _populated_running_chat(docker_manager, client)
    assert mcp_resources._discovery_admission.acquire(blocking=False)
    try:
        response = http.delete(f"/internal/sandbox/{CHAT}", headers=HEADERS)
    finally:
        mcp_resources._discovery_admission.release()

    assert response.status_code == 200, response.text
    assert response.json() == {"container": True, "volume": True, "data": True}
    assert _entries(docker_manager) == [".deleted", ".lifecycle.lock"]


def test_non_busy_discovery_failure_is_delete_failed_and_retry_completes(http, world, monkeypatch):
    import mcp_resources
    from uploads import DiscoveryError

    docker_manager, client, _clock, _tmp = world
    _populated_running_chat(docker_manager, client)
    assert mcp_resources.sync_chat_resources_sync(CHAT) == 1
    real_list = mcp_resources.list_chat_uploads
    attempts = {"n": 0}

    def flaky_list(chat_id):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise DiscoveryError("io_error")
        return real_list(chat_id)

    monkeypatch.setattr(mcp_resources, "list_chat_uploads", flaky_list)

    failed = http.delete(f"/internal/sandbox/{CHAT}", headers=HEADERS)

    assert failed.status_code == 500
    assert failed.json()["detail"]["reason"] == DELETE_FAILED
    assert "workspace resource cleanup" in failed.json()["detail"]["detail"]
    assert CHAT in mcp_resources._chat_uris

    retried = http.delete(f"/internal/sandbox/{CHAT}", headers=HEADERS)

    assert retried.status_code == 200
    assert retried.json() == {"container": False, "volume": False, "data": False}
    assert CHAT not in mcp_resources._chat_uris
    assert _entries(docker_manager) == [".deleted", ".lifecycle.lock"]


def _queued_creator_child():
    """Open the lock file, block on flock, then attempt a tool-path creation."""
    import docker_manager as manager

    client = _docker()
    manager.get_docker_client = lambda: client
    manager.render_system_prompt_sync = lambda *args, **kwargs: "readme"
    manager.skill_manager.get_user_skills_sync = lambda email: []
    manager.skill_manager.get_skill_mounts = lambda skills: {}
    sentinel = Path(os.environ["OCU_QUEUED_SENTINEL"])
    real_flock = manager.fcntl.flock

    def flock(fd, operation):
        if operation == manager.fcntl.LOCK_EX:
            sentinel.write_text(str(os.getpid()))
        return real_flock(fd, operation)

    manager.fcntl.flock = flock
    try:
        manager._get_or_create_container(CHAT)
        outcome = {"status": None, "reason": None}
    except manager.LifecycleError as exc:
        outcome = {"status": exc.status_code, "reason": exc.reason}
    outcome["created"] = [container.name for container in client._created]
    outcome["entries"] = sorted(os.listdir(manager.BASE_DATA_DIR / CHAT))
    print(json.dumps(outcome))


def test_creator_blocked_on_flock_in_another_process_is_refused_after_delete(world, tmp_path, monkeypatch):
    docker_manager, client, _clock, _tmp = world
    _bind_upload_root(docker_manager, monkeypatch)
    data = docker_manager.BASE_DATA_DIR
    _meta(docker_manager, CHAT)
    _outputs_file(docker_manager)
    _volume(client)
    sentinel = tmp_path / "queued-on-flock"
    child = None
    with docker_manager._combined_lock(CHAT):
        lock = data / CHAT / ".lifecycle.lock"
        lock_inode = _inode(lock)
        child = subprocess.Popen(
            [sys.executable, "-c",
             "from tests.orchestrator.test_lifecycle_retention import _queued_creator_child; "
             "_queued_creator_child()"],
            env=_child_env(data, BASE_DATA_DIR=str(data), OCU_QUEUED_SENTINEL=str(sentinel)),
            cwd=str(ROOT), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 15
            waiter = threading.Event()
            while not sentinel.exists():
                assert child.poll() is None, child.communicate()
                assert time.monotonic() < deadline, "child never reached flock"
                waiter.wait(0.01)
            waiter.wait(0.3)
            assert child.poll() is None, "child was admitted while the lock was held"
            result = docker_manager.delete_sandbox(CHAT)
        except BaseException:
            _stop_child(child)
            raise
    try:
        stdout, stderr = child.communicate(timeout=20)
    finally:
        _stop_child(child)
    assert child.returncode == 0, stderr[-1200:]
    assert result == {"container": False, "volume": True, "data": True}
    assert json.loads(stdout.strip().splitlines()[-1]) == {
        "status": 410,
        "reason": CHAT_DELETED,
        "created": [],
        "entries": [".deleted", ".lifecycle.lock"],
    }
    assert _entries(docker_manager) == [".deleted", ".lifecycle.lock"]
    assert _inode(lock) == lock_inode
