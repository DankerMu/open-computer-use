# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Dedicated sandbox-bridge membership, launch migration, and gateway addressing.

Exercises actual create, stop-launch, restart-alias (launch_sandbox), and
absent-container reconstruction against a stateful fake engine. No real Docker.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SERVER_DIR = Path(__file__).resolve().parents[2] / "computer-use-server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import docker as docker_sdk

APIError = docker_sdk.errors.APIError
NotFound = docker_sdk.errors.NotFound

CHAT = "c1d2e3f4-a5b6-7890-abcd-ef1234567890"
GATEWAY = "172.31.0.1"
NETWORK_NAME = "ocu-sandbox"
NETWORK_ID = "netid-ocu-sandbox-current"
STALE_ID = "netid-ocu-sandbox-stale"
COMPOSE_NAME = "compose_default"
COMPOSE_ID = "netid-compose"
CDP = "9222/tcp"
TTYD = "7681/tcp"


def _api_error(message, status_code=500):
    response = MagicMock(status_code=status_code, url="http://docker.test", reason="error")
    return APIError(message, response=response, explanation=message.encode())


class FakeNetwork:
    def __init__(self, owner, name, network_id, driver="bridge", internal=False, gateway=GATEWAY):
        self.owner = owner
        self.name = name
        self.id = network_id
        if gateway is None:
            config = []
        elif isinstance(gateway, list):
            config = [{"Gateway": item} for item in gateway]
        else:
            config = [{"Gateway": gateway, "Subnet": "172.31.0.0/24"}]
        self.attrs = {
            "Id": network_id,
            "Name": name,
            "Driver": driver,
            "Internal": internal,
            "IPAM": {"Config": config},
        }
        self.fail_connect = False
        self.fail_disconnect = False

    def connect(self, container):
        self.owner.ops.append((self.name, "connect", container.name))
        if self.fail_connect:
            raise _api_error("failed to connect network")
        networks = container.attrs.setdefault("NetworkSettings", {}).setdefault("Networks", {})
        networks[self.name] = {"NetworkID": self.id, "IPAddress": "172.31.0.10"}
        container.attrs.setdefault("HostConfig", {})["NetworkMode"] = self.name

    def disconnect(self, container, force=False):
        self.owner.ops.append((self.name, "disconnect", container.name))
        if self.fail_disconnect:
            raise _api_error("failed to disconnect network")
        networks = container.attrs.setdefault("NetworkSettings", {}).setdefault("Networks", {})
        networks.pop(self.name, None)
        for key, data in list(networks.items()):
            if (data or {}).get("NetworkID") == self.id:
                networks.pop(key, None)


class FakeNetworks:
    def __init__(self, owner):
        self.owner = owner

    def get(self, key):
        self.owner.network_gets.append(key)
        for net in self.owner._network_store.values():
            if net.name == key or net.id == key:
                return net
        raise NotFound(key)


class FakeContainers:
    def __init__(self, owner):
        self.owner = owner

    def get(self, name):
        if name not in self.owner.store:
            raise NotFound(name)
        return self.owner.store[name]

    def create(self, **config):
        name = config["name"]
        if name in self.owner.store:
            response = MagicMock(status_code=409, url="http://docker.test", reason="Conflict")
            raise APIError("Conflict", response=response, explanation=b"conflict")
        container = self.owner.make_container(
            name, status="created", container_id=f"cid-{len(self.owner.created) + 1}"
        )
        container._create_config = config
        self.owner.apply_create_config(container, config)
        self.owner.store[name] = container
        self.owner.created.append(container)
        return container


class FakeClient:
    def __init__(self):
        self.store = {}
        self.created = []
        self._network_store = {}
        self.ops = []
        self.network_gets = []
        self._next_host_port = 49152
        self.containers = FakeContainers(self)
        self.networks = FakeNetworks(self)

    def add_network(self, name=NETWORK_NAME, network_id=NETWORK_ID, driver="bridge", internal=False, gateway=GATEWAY):
        net = FakeNetwork(self, name, network_id, driver=driver, internal=internal, gateway=gateway)
        self._network_store[name] = net
        self._network_store[network_id] = net
        return net

    def clear_networks(self):
        self._network_store.clear()

    def make_container(self, name, status="running", container_id="cid-1", disabled=False):
        container = MagicMock(name=name)
        container.name = name
        container.id = container_id
        container.short_id = container_id[:12]
        container.status = status
        container.attrs = {
            "Id": container_id,
            "Name": f"/{name}",
            "State": {"Status": status, "Paused": status == "paused"},
            "Config": {"NetworkDisabled": disabled},
            "HostConfig": {
                "NetworkMode": "none" if disabled else "bridge",
                "PortBindings": {},
            },
            "NetworkSettings": {"Networks": {}, "Ports": {}},
        }
        removed = {"value": False}
        started = {"n": 0}
        unpaused = {"n": 0}

        def reload():
            container.attrs["State"]["Status"] = container.status
            container.attrs["State"]["Paused"] = container.status == "paused"
            container.attrs["Id"] = container.id

        def start():
            started["n"] += 1
            _assign_published_ports(self, container)
            container.status = "running"

        def unpause():
            unpaused["n"] += 1
            container.status = "running"

        def stop(timeout=10):
            container.status = "exited"

        def remove(force=False):
            removed["value"] = True

        def exec_run(cmd, detach=False, user=None, **kwargs):
            return MagicMock(exit_code=0, output=(b"ok\n", b""))

        container.reload.side_effect = reload
        container.start.side_effect = start
        container.unpause.side_effect = unpause
        container.stop.side_effect = stop
        container.remove.side_effect = remove
        container.exec_run.side_effect = exec_run
        container._removed = removed
        container._started = started
        container._unpaused = unpaused
        container._create_config = None
        return container

    def apply_create_config(self, container, config):
        host = container.attrs.setdefault("HostConfig", {})
        settings = container.attrs.setdefault("NetworkSettings", {})
        cfg = container.attrs.setdefault("Config", {})
        if config.get("network_disabled"):
            cfg["NetworkDisabled"] = True
            host["NetworkMode"] = "none"
            host["PortBindings"] = {}
            settings["Networks"] = {}
            settings["Ports"] = {}
            return
        cfg["NetworkDisabled"] = False
        bindings = {}
        for key, value in (config.get("ports") or {}).items():
            if value is None:
                bindings[key] = [{"HostIp": "", "HostPort": ""}]
            elif isinstance(value, tuple):
                host_ip, host_port = value
                bindings[key] = [{
                    "HostIp": host_ip or "",
                    "HostPort": "" if host_port is None else str(host_port),
                }]
        host["PortBindings"] = bindings
        net_name = config.get("network")
        if net_name:
            net = self.networks.get(net_name)
            host["NetworkMode"] = net_name
            settings["Networks"] = {
                net_name: {"NetworkID": net.id, "IPAddress": "172.31.0.10"},
            }

    def put(
        self,
        name,
        status="running",
        container_id="cid-existing",
        *,
        disabled=False,
        networks=None,
        bindings=None,
        published=None,
    ):
        container = self.make_container(name, status=status, container_id=container_id, disabled=disabled)
        if disabled:
            container.attrs["Config"]["NetworkDisabled"] = True
            container.attrs["HostConfig"]["NetworkMode"] = "none"
            container.attrs["HostConfig"]["PortBindings"] = {}
            container.attrs["NetworkSettings"]["Networks"] = {}
            container.attrs["NetworkSettings"]["Ports"] = {}
        else:
            if bindings is None:
                bindings = {
                    CDP: [{"HostIp": GATEWAY, "HostPort": "49153"}],
                    TTYD: [{"HostIp": GATEWAY, "HostPort": "49154"}],
                }
            container.attrs["HostConfig"]["PortBindings"] = bindings
            if networks is None:
                networks = {NETWORK_NAME: {"NetworkID": NETWORK_ID, "IPAddress": "172.31.0.10"}}
            container.attrs["NetworkSettings"]["Networks"] = dict(networks)
            if published is None:
                published = {
                    key: [dict(entry) for entry in entries]
                    for key, entries in bindings.items()
                    if any((entry or {}).get("HostPort") for entry in entries)
                }
            container.attrs["NetworkSettings"]["Ports"] = published
            if networks:
                container.attrs["HostConfig"]["NetworkMode"] = next(iter(networks))
        self.store[name] = container
        return container


def _assign_published_ports(client, container):
    bindings = (container.attrs.get("HostConfig") or {}).get("PortBindings") or {}
    published = {}
    for key, entries in bindings.items():
        assigned = []
        for entry in entries or []:
            item = dict(entry or {})
            if not item.get("HostPort"):
                item["HostPort"] = str(client._next_host_port)
                client._next_host_port += 1
            assigned.append(item)
        published[key] = assigned
        bindings[key] = assigned
    container.attrs["NetworkSettings"]["Ports"] = published


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", "ocu-test-internal-token")
    monkeypatch.setenv("PUBLIC_BASE_URL", "/ocu")
    monkeypatch.setenv("BASE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("USER_DATA_BASE_PATH", str(tmp_path / "user-data"))
    monkeypatch.setenv("DOCKER_IMAGE", "python:3.12-slim")
    monkeypatch.delenv("OCU_SANDBOX_NO_AUTOSTART", raising=False)
    monkeypatch.delenv("SUBAGENT_CLI", raising=False)
    import docker_manager

    docker_manager.BASE_DATA_DIR = tmp_path / "data"
    docker_manager.USER_DATA_BASE_PATH = str(tmp_path / "user-data")
    monkeypatch.setattr(docker_manager, "DOCKER_IMAGE", "python:3.12-slim")
    monkeypatch.setattr(docker_manager, "SUBAGENT_CLI", "claude")
    monkeypatch.setattr(docker_manager, "ENABLE_NETWORK", True)
    monkeypatch.setattr(docker_manager, "OCU_SANDBOX_NETWORK", NETWORK_NAME, raising=False)
    monkeypatch.setattr(docker_manager, "SANDBOX_HOST_BIND_IP", "", raising=False)
    docker_manager._docker_client = None
    docker_manager._chat_locks.clear()
    docker_manager._FLOCK_DEPTH.clear()
    client = FakeClient()
    client.add_network()
    client.add_network(COMPOSE_NAME, COMPOSE_ID, gateway="172.18.0.1")
    orch = client.make_container("computer-use-server", status="running", container_id="orch")
    orch.attrs["NetworkSettings"]["Networks"] = {
        COMPOSE_NAME: {"NetworkID": COMPOSE_ID, "IPAddress": "172.18.0.2"},
    }
    client.store["computer-use-server"] = orch
    monkeypatch.setattr(docker_manager, "get_docker_client", lambda: client)
    monkeypatch.setattr(docker_manager, "render_system_prompt_sync", lambda *args, **kwargs: "readme")
    monkeypatch.setattr(docker_manager.skill_manager, "get_user_skills_sync", lambda email: [])
    monkeypatch.setattr(docker_manager.skill_manager, "get_skill_mounts", lambda skills: {})
    gitlab_tok = docker_manager.current_gitlab_token.set(None)
    anth_tok = docker_manager.current_anthropic_auth_token.set(None)
    email_tok = docker_manager.current_user_email.set(None)
    name_tok = docker_manager.current_user_name.set(None)
    source_tok = docker_manager.current_credential_source.set("request")
    try:
        yield docker_manager, client, tmp_path
    finally:
        docker_manager.current_gitlab_token.reset(gitlab_tok)
        docker_manager.current_anthropic_auth_token.reset(anth_tok)
        docker_manager.current_user_email.reset(email_tok)
        docker_manager.current_user_name.reset(name_tok)
        docker_manager.current_credential_source.reset(source_tok)
        docker_manager._FLOCK_DEPTH.clear()
        docker_manager._chat_locks.clear()
        docker_manager._docker_client = None


def _name(docker_manager):
    return docker_manager._container_name(CHAT)


def _meta(docker_manager, **extra):
    payload = {
        "user_email": "owner@example",
        "user_name": "Owner",
        "mcp_servers": "",
        "created_at": "2026-01-01T00:00:00Z",
    }
    payload.update(extra)
    path = docker_manager._get_meta_path(CHAT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return path


def _membership(container):
    return dict((container.attrs.get("NetworkSettings") or {}).get("Networks") or {})


def test_create_selects_sandbox_bridge_and_gateway_publications(world):
    docker_manager, client, _tmp = world
    docker_manager._get_or_create_container(CHAT)
    created = client.created[0]
    config = created._create_config
    assert config["network"] == NETWORK_NAME
    assert config["ports"] == {CDP: (GATEWAY, None), TTYD: (GATEWAY, None)}
    assert "network_disabled" not in config
    assert list(_membership(created)) == [NETWORK_NAME]
    assert _membership(created)[NETWORK_NAME]["NetworkID"] == NETWORK_ID
    assert client.ops == []


def test_absent_container_reconstruction_uses_create_path(world):
    docker_manager, client, _tmp = world
    _meta(docker_manager)
    assert docker_manager.launch_sandbox(CHAT) == {"state": "running"}
    created = client.created[0]
    assert created.status == "running"
    assert created.id == docker_manager._lookup_container(CHAT).id
    assert created._create_config["network"] == NETWORK_NAME
    assert created._create_config["ports"] == {CDP: (GATEWAY, None), TTYD: (GATEWAY, None)}
    assert list(_membership(created)) == [NETWORK_NAME]
    assert docker_manager.load_container_meta(CHAT)["user_email"] == "owner@example"


def test_stop_launch_and_restart_alias_keep_one_membership(world):
    docker_manager, client, _tmp = world
    container = client.put(_name(docker_manager), status="running", container_id="keep-me")
    container.stop()
    assert container.status == "exited"
    before = dict(_membership(container))
    assert docker_manager.launch_sandbox(CHAT) == {"state": "running"}
    assert container.status == "running"
    assert container.id == "keep-me"
    assert _membership(container) == before
    assert container._removed["value"] is False
    container.stop()
    assert docker_manager.launch_sandbox(CHAT) == {"state": "running"}
    assert list(_membership(container)) == [NETWORK_NAME]
    assert _membership(container)[NETWORK_NAME]["NetworkID"] == NETWORK_ID
    assert container._removed["value"] is False


def test_invalid_network_configuration_does_not_create_or_mutate(world):
    docker_manager, client, _tmp = world
    existing = client.put(_name(docker_manager), status="exited", container_id="stay")

    client.clear_networks()
    with pytest.raises(docker_manager.LaunchFailed) as missing:
        docker_manager._create_container(CHAT, _name(docker_manager))
    assert missing.value.status_code == 500
    assert client.created == []
    assert existing.status == "exited"
    assert existing._removed["value"] is False

    client.add_network(driver="overlay")
    with pytest.raises(docker_manager.LaunchFailed):
        docker_manager.launch_sandbox(CHAT)
    assert existing.status == "exited"
    assert existing._started["n"] == 0

    client.clear_networks()
    client.add_network(internal=True)
    with pytest.raises(docker_manager.LaunchFailed):
        docker_manager.launch_sandbox(CHAT)
    assert existing._started["n"] == 0

    client.clear_networks()
    client.add_network(gateway=None)
    with pytest.raises(docker_manager.LaunchFailed):
        docker_manager.launch_sandbox(CHAT)
    assert existing._started["n"] == 0

    client.clear_networks()
    client.add_network(gateway=["172.31.0.1", "172.31.0.2"])
    with pytest.raises(docker_manager.LaunchFailed):
        docker_manager.launch_sandbox(CHAT)
    assert existing._started["n"] == 0

    client.clear_networks()
    client.add_network()
    docker_manager.SANDBOX_HOST_BIND_IP = "172.30.0.1"
    with pytest.raises(docker_manager.LaunchFailed):
        docker_manager._create_container(CHAT, _name(docker_manager))
    assert client.created == []
    assert existing.status == "exited"


def test_disabled_create_stop_launch_skips_network_lookup(world):
    docker_manager, client, _tmp = world
    docker_manager.ENABLE_NETWORK = False
    docker_manager._get_or_create_container(CHAT)
    created = client.created[0]
    assert created._create_config.get("network_disabled") is True
    assert "network" not in created._create_config
    assert "ports" not in created._create_config
    assert created.attrs["Config"]["NetworkDisabled"] is True
    assert client.network_gets == []
    created.status = "exited"
    client.network_gets.clear()
    assert docker_manager.launch_sandbox(CHAT) == {"state": "running"}
    assert created.status == "running"
    assert client.network_gets == []
    assert created.attrs["Config"]["NetworkDisabled"] is True
    assert created.attrs["HostConfig"].get("PortBindings") in ({}, None)


def test_disabled_launch_rejects_bindings_or_membership_without_lookup(world):
    docker_manager, client, _tmp = world
    docker_manager.ENABLE_NETWORK = False
    dirty = client.put(_name(docker_manager), status="exited", container_id="dirty-disabled", disabled=True)
    dirty.attrs["HostConfig"]["PortBindings"] = {
        CDP: [{"HostIp": GATEWAY, "HostPort": "49153"}],
    }
    client.network_gets.clear()
    with pytest.raises(docker_manager.LaunchFailed) as caught:
        docker_manager.launch_sandbox(CHAT)
    assert caught.value.status_code == 409
    assert dirty.status == "exited"
    assert dirty._started["n"] == 0
    assert client.network_gets == []



def test_configured_and_inspected_mode_mismatch_fails_without_mutation(world):
    docker_manager, client, _tmp = world
    disabled = client.put(_name(docker_manager), status="exited", container_id="disabled", disabled=True)
    with pytest.raises(docker_manager.LaunchFailed):
        docker_manager.launch_sandbox(CHAT)
    assert disabled.status == "exited"
    assert disabled._started["n"] == 0
    assert disabled._removed["value"] is False

    docker_manager.ENABLE_NETWORK = False
    enabled = client.put(_name(docker_manager), status="exited", container_id="enabled")
    client.network_gets.clear()
    with pytest.raises(docker_manager.LaunchFailed):
        docker_manager.launch_sandbox(CHAT)
    assert enabled.status == "exited"
    assert enabled._started["n"] == 0
    assert enabled._unpaused["n"] == 0
    assert enabled._removed["value"] is False
    assert client.network_gets == []


def test_create_conflict_adopts_only_compatible_running_winner(world):
    docker_manager, client, _tmp = world
    winner = client.put(_name(docker_manager), status="running", container_id="winner")

    def conflict(**config):
        response = MagicMock(status_code=409, url="http://docker.test", reason="Conflict")
        raise APIError("Conflict", response=response, explanation=b"conflict")

    client.containers.create = conflict
    adopted = docker_manager._create_container(CHAT, _name(docker_manager))
    assert adopted is winner
    assert winner._removed["value"] is False
    assert client.ops == []

    winner.attrs["NetworkSettings"]["Networks"] = {
        COMPOSE_NAME: {"NetworkID": COMPOSE_ID, "IPAddress": "172.18.0.5"},
    }
    with pytest.raises(docker_manager.LaunchFailed):
        docker_manager._create_container(CHAT, _name(docker_manager))
    assert winner.status == "running"
    assert winner._removed["value"] is False
    assert client.ops == []

    docker_manager.ENABLE_NETWORK = False
    with pytest.raises(docker_manager.LaunchFailed):
        docker_manager._create_container(CHAT, _name(docker_manager))
    assert winner.status == "running"

    disabled_winner = client.put(
        _name(docker_manager), status="running", container_id="disabled-win", disabled=True
    )
    client.network_gets.clear()
    adopted_disabled = docker_manager._create_container(CHAT, _name(docker_manager))
    assert adopted_disabled is disabled_winner
    assert client.network_gets == []


def test_same_gateway_stale_network_id_is_repaired_before_start(world):
    docker_manager, client, _tmp = world
    container = client.put(
        _name(docker_manager),
        status="exited",
        container_id="stale",
        networks={NETWORK_NAME: {"NetworkID": STALE_ID, "IPAddress": "172.31.0.10"}},
        bindings={
            CDP: [{"HostIp": GATEWAY, "HostPort": "49153"}],
            TTYD: [{"HostIp": GATEWAY, "HostPort": "49154"}],
        },
    )
    assert docker_manager.launch_sandbox(CHAT) == {"state": "running"}
    assert container.status == "running"
    assert container.id == "stale"
    assert _membership(container) == {
        NETWORK_NAME: {"NetworkID": NETWORK_ID, "IPAddress": "172.31.0.10"}
    }
    assert container._removed["value"] is False


def test_same_name_network_replacement_is_repaired_by_id(world):
    docker_manager, client, _tmp = world
    container = client.put(
        _name(docker_manager),
        status="exited",
        container_id="replaced-id",
        networks={NETWORK_NAME: {"NetworkID": STALE_ID, "IPAddress": "172.31.0.10"}},
    )
    client._network_store[STALE_ID] = FakeNetwork(client, NETWORK_NAME, STALE_ID)
    assert docker_manager.launch_sandbox(CHAT) == {"state": "running"}
    assert list(_membership(container)) == [NETWORK_NAME]
    assert _membership(container)[NETWORK_NAME]["NetworkID"] == NETWORK_ID
    assert any(op[1] == "disconnect" for op in client.ops)
    assert (NETWORK_NAME, "connect", container.name) in client.ops
    assert container._removed["value"] is False


def test_foreign_membership_repaired_on_stopped_launch(world):
    docker_manager, client, _tmp = world
    client.add_network(COMPOSE_NAME, COMPOSE_ID, gateway="172.18.0.1")
    container = client.put(
        _name(docker_manager),
        status="exited",
        container_id="foreign",
        networks={COMPOSE_NAME: {"NetworkID": COMPOSE_ID, "IPAddress": "172.18.0.9"}},
    )
    assert docker_manager.launch_sandbox(CHAT) == {"state": "running"}
    assert container.id == "foreign"
    assert list(_membership(container)) == [NETWORK_NAME]
    assert _membership(container)[NETWORK_NAME]["NetworkID"] == NETWORK_ID
    assert container._removed["value"] is False


def test_unassigned_dynamic_host_port_does_not_block_launch(world):
    docker_manager, client, _tmp = world
    container = client.put(
        _name(docker_manager),
        status="created",
        container_id="unassigned",
        bindings={
            CDP: [{"HostIp": GATEWAY, "HostPort": ""}],
            TTYD: [{"HostIp": GATEWAY, "HostPort": ""}],
        },
        published={},
    )
    assert docker_manager.launch_sandbox(CHAT) == {"state": "running"}
    published = container.attrs["NetworkSettings"]["Ports"]
    assert published[CDP][0]["HostIp"] == GATEWAY
    assert published[CDP][0]["HostPort"]
    assert published[CDP][0]["HostPort"] != "9222"
    assert list(_membership(container)) == [NETWORK_NAME]
    assert _membership(container)[NETWORK_NAME]["NetworkID"] == NETWORK_ID
    assert COMPOSE_NAME not in _membership(container)
    address = docker_manager.get_container_service_address(CHAT, 9222)
    assert address == f"{GATEWAY}:{published[CDP][0]['HostPort']}"


def test_missing_mixed_and_wrong_bindings_fail_without_start(world):
    docker_manager, client, _tmp = world
    missing = client.put(
        _name(docker_manager),
        status="exited",
        container_id="missing-bind",
        bindings={CDP: [{"HostIp": GATEWAY, "HostPort": "49153"}]},
    )
    with pytest.raises(docker_manager.LaunchFailed):
        docker_manager.launch_sandbox(CHAT)
    assert missing.status == "exited"
    assert missing._started["n"] == 0
    assert missing._removed["value"] is False

    mixed = client.put(
        _name(docker_manager),
        status="exited",
        container_id="mixed-bind",
        bindings={
            CDP: [
                {"HostIp": GATEWAY, "HostPort": "49153"},
                {"HostIp": "0.0.0.0", "HostPort": "49199"},
            ],
            TTYD: [{"HostIp": GATEWAY, "HostPort": "49154"}],
        },
    )
    with pytest.raises(docker_manager.LaunchFailed):
        docker_manager.launch_sandbox(CHAT)
    assert mixed.status == "exited"
    assert mixed._started["n"] == 0
    assert mixed._removed["value"] is False

    wrong = client.put(
        _name(docker_manager),
        status="exited",
        container_id="wrong-bind",
        bindings={
            CDP: [{"HostIp": "127.0.0.1", "HostPort": "49153"}],
            TTYD: [{"HostIp": GATEWAY, "HostPort": "49154"}],
        },
    )
    with pytest.raises(docker_manager.LaunchFailed):
        docker_manager.launch_sandbox(CHAT)
    assert wrong.status == "exited"
    assert wrong._started["n"] == 0
    assert wrong._removed["value"] is False
    workspace = Path(docker_manager.USER_DATA_BASE_PATH) / CHAT
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "layer").write_text("keep")
    meta = _meta(docker_manager)
    meta_bytes = meta.read_text()
    mixed_ttyd = client.put(
        _name(docker_manager),
        status="exited",
        container_id="mixed-ttyd",
        bindings={
            CDP: [{"HostIp": GATEWAY, "HostPort": "49153"}],
            TTYD: [
                {"HostIp": GATEWAY, "HostPort": "49154"},
                {"HostIp": "0.0.0.0", "HostPort": "49200"},
            ],
        },
    )
    with pytest.raises(docker_manager.LaunchFailed) as mixed_exc:
        docker_manager.launch_sandbox(CHAT)
    assert mixed_exc.value.status_code == 409
    assert mixed_ttyd.status == "exited"
    assert mixed_ttyd._started["n"] == 0
    assert mixed_ttyd.id == "mixed-ttyd"
    assert mixed_ttyd._removed["value"] is False
    assert meta.read_text() == meta_bytes
    assert (workspace / "layer").read_text() == "keep"


def test_changed_gateway_or_wildcard_requires_operator_migration(world):
    docker_manager, client, _tmp = world
    wildcard = client.put(
        _name(docker_manager),
        status="exited",
        container_id="wildcard",
        bindings={
            CDP: [{"HostIp": "0.0.0.0", "HostPort": "49153"}],
            TTYD: [{"HostIp": "0.0.0.0", "HostPort": "49154"}],
        },
    )
    with pytest.raises(docker_manager.LaunchFailed) as caught:
        docker_manager.launch_sandbox(CHAT)
    assert caught.value.status_code == 409
    assert wildcard.status == "exited"
    assert wildcard._started["n"] == 0
    assert wildcard._removed["value"] is False

    changed = client.put(
        _name(docker_manager),
        status="exited",
        container_id="changed-gw",
        bindings={
            CDP: [{"HostIp": "172.30.0.1", "HostPort": "49153"}],
            TTYD: [{"HostIp": "172.30.0.1", "HostPort": "49154"}],
        },
    )
    with pytest.raises(docker_manager.LaunchFailed) as changed_exc:
        docker_manager.launch_sandbox(CHAT)
    assert changed_exc.value.status_code == 409
    assert changed.status == "exited"
    assert changed._started["n"] == 0
    assert changed._removed["value"] is False


@pytest.mark.parametrize("status", ["running", "paused", "restarting"])
def test_live_foreign_membership_fails_without_mutation(world, status):
    docker_manager, client, _tmp = world
    container = client.put(
        _name(docker_manager),
        status=status,
        container_id="live-foreign",
        networks={COMPOSE_NAME: {"NetworkID": COMPOSE_ID, "IPAddress": "172.18.0.9"}},
    )
    if status == "paused":
        docker_manager.mark_sleeper_retired(CHAT, container)
    ops_before = list(client.ops)
    with pytest.raises(docker_manager.LaunchFailed) as caught:
        docker_manager.launch_sandbox(CHAT)
    assert caught.value.status_code == 409
    assert container.status == status
    assert container._started["n"] == 0
    assert container._unpaused["n"] == 0
    assert container._removed["value"] is False
    assert client.ops == ops_before
    assert _membership(container) == {
        COMPOSE_NAME: {"NetworkID": COMPOSE_ID, "IPAddress": "172.18.0.9"}
    }


def test_membership_repair_failure_does_not_start_or_succeed(world):
    docker_manager, client, _tmp = world
    client.add_network(COMPOSE_NAME, COMPOSE_ID, gateway="172.18.0.1")
    container = client.put(
        _name(docker_manager),
        status="exited",
        container_id="repair-fail",
        networks={COMPOSE_NAME: {"NetworkID": COMPOSE_ID, "IPAddress": "172.18.0.9"}},
    )
    client.networks.get(NETWORK_NAME).fail_connect = True
    _meta(docker_manager)
    with pytest.raises(docker_manager.LaunchFailed) as caught:
        docker_manager.launch_sandbox(CHAT)
    assert caught.value.status_code == 500
    assert container.status == "exited"
    assert container._started["n"] == 0
    assert container._removed["value"] is False
    assert container.id == "repair-fail"
    assert docker_manager.load_container_meta(CHAT)["user_email"] == "owner@example"
    assert NETWORK_NAME not in _membership(container)


def test_disconnect_failure_and_partial_repair_do_not_start(world):
    docker_manager, client, _tmp = world
    client.add_network(COMPOSE_NAME, COMPOSE_ID, gateway="172.18.0.1")
    container = client.put(
        _name(docker_manager),
        status="exited",
        container_id="disconnect-fail",
        networks={COMPOSE_NAME: {"NetworkID": COMPOSE_ID, "IPAddress": "172.18.0.9"}},
    )
    client.networks.get(COMPOSE_ID).fail_disconnect = True
    with pytest.raises(docker_manager.LaunchFailed) as caught:
        docker_manager.launch_sandbox(CHAT)
    assert caught.value.status_code == 500
    assert container.status == "exited"
    assert container._started["n"] == 0
    assert COMPOSE_NAME in _membership(container)

    client.networks.get(COMPOSE_ID).fail_disconnect = False
    client.networks.get(NETWORK_NAME).fail_connect = True
    container.attrs["NetworkSettings"]["Networks"] = {
        COMPOSE_NAME: {"NetworkID": COMPOSE_ID, "IPAddress": "172.18.0.9"},
    }
    with pytest.raises(docker_manager.LaunchFailed):
        docker_manager.launch_sandbox(CHAT)
    assert container._started["n"] == 0
    assert NETWORK_NAME not in _membership(container) or _membership(container).get(COMPOSE_NAME)


def test_address_uses_assigned_gateway_publication_only(world):
    docker_manager, client, _tmp = world
    client.put(
        _name(docker_manager),
        status="running",
        container_id="addr",
        published={
            CDP: [{"HostIp": GATEWAY, "HostPort": "40001"}],
            TTYD: [{"HostIp": GATEWAY, "HostPort": "40002"}],
        },
    )
    ops_before = list(client.ops)
    assert docker_manager.get_container_service_address(CHAT, 9222) == f"{GATEWAY}:40001"
    assert docker_manager.get_container_service_address(CHAT, 7681) == f"{GATEWAY}:40002"
    container = docker_manager._lookup_container(CHAT)
    container.attrs["NetworkSettings"]["Networks"][COMPOSE_NAME] = {
        "NetworkID": COMPOSE_ID, "IPAddress": "172.18.0.5",
    }
    assert docker_manager.get_container_service_address(CHAT, 9222) == f"{GATEWAY}:40001"
    assert client.ops == ops_before


def test_missing_publication_is_unavailable_even_with_container_ip(world):
    docker_manager, client, _tmp = world
    container = client.put(
        _name(docker_manager),
        status="running",
        container_id="no-ports",
        published={},
    )
    container.attrs["NetworkSettings"]["Networks"] = {
        NETWORK_NAME: {"NetworkID": NETWORK_ID, "IPAddress": "172.31.0.10"},
        COMPOSE_NAME: {"NetworkID": COMPOSE_ID, "IPAddress": "172.18.0.5"},
    }
    container.attrs["NetworkSettings"]["IPAddress"] = "172.18.0.5"
    ops_before = list(client.ops)
    assert docker_manager.get_container_service_address(CHAT, 9222) is None
    assert docker_manager.get_container_service_address(CHAT, 7681) is None
    assert client.ops == ops_before




def test_rejected_default_network_names_fail_before_create(world):
    docker_manager, client, _tmp = world
    for name in ("bridge", "host", "none"):
        docker_manager.OCU_SANDBOX_NETWORK = name
        with pytest.raises(docker_manager.LaunchFailed):
            docker_manager._create_container(CHAT, _name(docker_manager))
        assert client.created == []
        assert client.network_gets == []
        client.network_gets.clear()
