# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Request lifecycle Docker failures retain their typed error contract."""

import pytest
from docker.errors import DockerException

from tests.orchestrator.test_lifecycle import CHAT, _put, world
from tests.orchestrator.test_network_membership import (
    CHAT as NETWORK_CHAT,
    COMPOSE_ID,
    COMPOSE_NAME,
    NETWORK_ID,
    NETWORK_NAME,
    _engine_nets,
    _name,
    world as network_world,
)

__all__ = ["world", "network_world"]


@pytest.mark.parametrize(
    ("operation", "failure_at"),
    [
        pytest.param("_get_or_create_container", "get", id="get-create-get"),
        pytest.param("_get_or_create_container", "reload", id="get-create-reload"),
        pytest.param("describe_sandbox", "get", id="describe-get"),
        pytest.param("describe_sandbox", "reload", id="describe-reload"),
    ],
)
def test_lookup_docker_failure_is_launch_failed_without_creation(
    world, monkeypatch, operation, failure_at,
):
    docker_manager, client, _clock, _tmp = world
    container = _put(client, f"owui-chat-{CHAT}", "running")
    failure = DockerException("engine unavailable")
    boundary = client.containers.get if failure_at == "get" else container.reload
    monkeypatch.setattr(boundary, "side_effect", failure)

    with pytest.raises(docker_manager.LaunchFailed) as caught:
        getattr(docker_manager, operation)(CHAT)

    assert isinstance(caught.value, docker_manager.LifecycleError)
    assert caught.value.status_code == 500
    assert caught.value.reason == "launch_failed"
    assert caught.value.__cause__ is failure
    client.containers.create.assert_not_called()


@pytest.mark.parametrize(
    ("status", "failure_lookup"),
    [
        pytest.param("running", 1, id="running"),
        pytest.param("paused", 1, id="paused-verified"),
        pytest.param("exited", 1, id="exited"),
        pytest.param("created", 1, id="created"),
        pytest.param("restarting", 1, id="restarting"),
        pytest.param("exited", 3, id="exited-after-repair"),
        pytest.param("created", 3, id="created-after-repair"),
    ],
)
def test_launch_preparation_docker_failure_stops_later_actions(
    network_world, monkeypatch, status, failure_lookup,
):
    docker_manager, client, _tmp = network_world
    networks = None
    if failure_lookup == 3:
        networks = {
            COMPOSE_NAME: {"NetworkID": COMPOSE_ID, "IPAddress": "172.18.0.9"},
        }
    container = client.put(_name(docker_manager), status=status, networks=networks)
    if status == "paused":
        docker_manager.mark_sleeper_retired(NETWORK_CHAT, container)
    failure = DockerException("network engine unavailable")

    def fail_network_lookup(ordinal):
        if ordinal == failure_lookup:
            raise failure

    monkeypatch.setattr(client, "on_sandbox_lookup", fail_network_lookup)

    with pytest.raises(docker_manager.LaunchFailed) as caught:
        docker_manager.launch_sandbox(NETWORK_CHAT)

    assert caught.value.status_code == 500
    assert caught.value.reason == "launch_failed"
    assert caught.value.__cause__ is failure
    assert client._sandbox_lookups == failure_lookup
    assert container.status == status
    container.start.assert_not_called()
    container.unpause.assert_not_called()
    container.stop.assert_not_called()
    container.remove.assert_not_called()
    container.exec_run.assert_not_called()
    assert client.created == []
    assert _engine_nets(client, container) == {
        NETWORK_NAME: {"NetworkID": NETWORK_ID, "IPAddress": "172.31.0.10"},
    }
    if failure_lookup == 3:
        assert client.ops == [
            (COMPOSE_ID, "disconnect", container.id),
            (NETWORK_ID, "connect", container.id),
        ]
    else:
        assert client.ops == []


@pytest.mark.parametrize("readiness_path", ["loop", "final"])
def test_restart_readiness_docker_failure_stops_retirement(
    network_world, monkeypatch, readiness_path,
):
    docker_manager, client, _tmp = network_world
    container = client.put(_name(docker_manager), status="restarting")
    failure = DockerException("readiness engine unavailable")
    original_reload = container.reload.side_effect

    def fail_readiness_reload():
        if container.reload.call_count == 2:
            raise failure
        original_reload()

    monkeypatch.setattr(container.reload, "side_effect", fail_readiness_reload)
    monkeypatch.setattr(docker_manager.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(
        docker_manager, "RESTART_WAIT_SECONDS", 0 if readiness_path == "final" else 1,
    )

    with pytest.raises(docker_manager.LaunchFailed) as caught:
        docker_manager.launch_sandbox(NETWORK_CHAT)

    assert caught.value.status_code == 500
    assert caught.value.reason == "launch_failed"
    assert caught.value.__cause__ is failure
    assert container.reload.call_count == 2
    assert container.status == "restarting"
    container.start.assert_not_called()
    container.unpause.assert_not_called()
    container.stop.assert_not_called()
    container.remove.assert_not_called()
    container.exec_run.assert_not_called()
    assert client.created == []
    assert client.ops == []
