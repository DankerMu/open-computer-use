# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Request lifecycle Docker failures retain their typed error contract."""

import pytest
from docker.errors import DockerException

from tests.orchestrator.test_lifecycle import CHAT, _put, world

__all__ = ["world"]


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
