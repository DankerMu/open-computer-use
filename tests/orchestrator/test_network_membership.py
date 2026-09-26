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
        self.noop_disconnect = False

    def connect(self, container):
        self.owner.ops.append((self.id, "connect", container.id))
        if self.fail_connect:
            raise _api_error("failed to connect network")
        membership = self.owner.engine_membership.setdefault(container.id, {})
        membership[self.id] = {"name": self.name, "NetworkID": self.id, "IPAddress": "172.31.0.10"}

    def disconnect(self, container, force=False):
        self.owner.ops.append((self.id, "disconnect", container.id))
        if self.fail_disconnect:
            raise _api_error("failed to disconnect network")
        if self.noop_disconnect:
            return
        membership = self.owner.engine_membership.setdefault(container.id, {})
        membership.pop(self.id, None)


class FakeNetworks:
    def __init__(self, owner):
        self.owner = owner

    def get(self, key):
        self.owner.network_gets.append(key)
        unique = {id(net): net for net in self.owner._network_store.values()}
        found = None
        for net in unique.values():
            if net.name == key or net.id == key:
                found = net
                break
        if found is None:
            raise NotFound(key)
        if found.name == NETWORK_NAME:
            self.owner._sandbox_lookups += 1
            hook = self.owner.on_sandbox_lookup
            if hook is not None:
                replacement = hook(self.owner._sandbox_lookups)
                if replacement is not None:
                    return replacement
        return found


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
        if self.owner.replace_named_network_on_create:
            self.owner.replace_named_network_on_create()
            self.owner.replace_named_network_on_create = None
        if self.owner.refuse_hostname and "hostname" in config:
            self.owner.refuse_hostname = False
            raise _api_error("cannot set hostname in host UTS namespace", status_code=500)
        container = self.owner.make_container(
            name, status="created", container_id=f"cid-{len(self.owner.created) + 1}"
        )
        container._create_config = config
        self.owner.apply_create_config(container, config)
        if self.owner.inspect_dns_mismatch:
            container.attrs.setdefault("HostConfig", {})["Dns"] = list(self.owner.inspect_dns_mismatch)
        self.owner.store[name] = container
        self.owner.created.append(container)
        return container


class FakeClient:
    def __init__(self):
        self.store = {}
        self.created = []
        self._network_store = {}
        self.engine_membership = {}
        self.ops = []
        self.network_gets = []
        self._next_host_port = 49152
        self.containers = FakeContainers(self)
        self.networks = FakeNetworks(self)
        self.replace_named_network_on_create = None
        self.on_sandbox_lookup = None
        self._sandbox_lookups = 0
        self.refuse_hostname = False
        self.inspect_dns_mismatch = None

    def add_network(self, name=NETWORK_NAME, network_id=NETWORK_ID, driver="bridge", internal=False, gateway=GATEWAY):
        net = FakeNetwork(self, name, network_id, driver=driver, internal=internal, gateway=gateway)
        if name not in self._network_store or self._network_store[name].id == network_id:
            self._network_store[name] = net
        self._network_store[network_id] = net
        return net

    def replace_named_network(self, name, network_id, driver="bridge", internal=False, gateway=GATEWAY):
        previous = self._network_store.get(name)
        if previous is not None:
            self._network_store.pop(previous.id, None)
            self._network_store.pop(name, None)
        return self.add_network(name=name, network_id=network_id, driver=driver, internal=internal, gateway=gateway)

    def inspect_membership(self, container):
        snapshot = {}
        for net_id, data in dict(self.engine_membership.get(container.id, {})).items():
            snapshot[data["name"]] = {
                "NetworkID": data["NetworkID"],
                "IPAddress": data.get("IPAddress", "172.31.0.10"),
            }
        return snapshot

    def seed_membership(self, container, networks):
        membership = {}
        for name, data in (networks or {}).items():
            net_id = (data or {}).get("NetworkID") or (data or {}).get("NetworkId")
            membership[net_id] = {
                "name": name,
                "NetworkID": net_id,
                "IPAddress": (data or {}).get("IPAddress", "172.31.0.10"),
            }
        self.engine_membership[container.id] = membership
        container.attrs.setdefault("NetworkSettings", {})["Networks"] = self.inspect_membership(container)

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
            container.attrs.setdefault("NetworkSettings", {})["Networks"] = self.inspect_membership(container)

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
            self.engine_membership[container.id] = {}
            if "dns" in config:
                host["Dns"] = list(config["dns"]) if config["dns"] is not None else None
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
        net_key = config.get("network")
        if net_key:
            net = self.networks.get(net_key)
            host["NetworkMode"] = net_key
            self.engine_membership[container.id] = {
                net.id: {"name": net.name, "NetworkID": net.id, "IPAddress": "172.31.0.10"},
            }
            settings["Networks"] = self.inspect_membership(container)
        if "dns" in config:
            host["Dns"] = list(config["dns"]) if config["dns"] is not None else None


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
        dns=None,
    ):
        container = self.make_container(name, status=status, container_id=container_id, disabled=disabled)
        if disabled:
            container.attrs["Config"]["NetworkDisabled"] = True
            container.attrs["HostConfig"]["NetworkMode"] = "none"
            container.attrs["HostConfig"]["PortBindings"] = {}
            container.attrs["NetworkSettings"]["Networks"] = {}
            container.attrs["NetworkSettings"]["Ports"] = {}
            self.engine_membership[container.id] = {}
        else:
            if bindings is None:
                bindings = {
                    CDP: [{"HostIp": GATEWAY, "HostPort": "49153"}],
                    TTYD: [{"HostIp": GATEWAY, "HostPort": "49154"}],
                }
            container.attrs["HostConfig"]["PortBindings"] = bindings
            if networks is None:
                networks = {NETWORK_NAME: {"NetworkID": NETWORK_ID, "IPAddress": "172.31.0.10"}}
            self.seed_membership(container, networks)
            if published is None:
                published = {
                    key: [dict(entry) for entry in entries]
                    for key, entries in bindings.items()
                    if any((entry or {}).get("HostPort") for entry in entries)
                }
            container.attrs["NetworkSettings"]["Ports"] = published
            if networks:
                container.attrs["HostConfig"]["NetworkMode"] = next(iter(networks))
        if dns is not None:
            container.attrs["HostConfig"]["Dns"] = list(dns)
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
    monkeypatch.delenv("OCU_SANDBOX_DNS", raising=False)
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


def _engine_nets(client, container):
    return client.inspect_membership(container)


def test_create_selects_sandbox_bridge_and_gateway_publications(world):
    docker_manager, client, _tmp = world
    docker_manager._get_or_create_container(CHAT)
    created = client.created[0]
    config = created._create_config
    assert config["network"] == NETWORK_ID
    assert config["ports"] == {CDP: (GATEWAY, None), TTYD: (GATEWAY, None)}
    assert "network_disabled" not in config
    assert list(_engine_nets(client, created)) == [NETWORK_NAME]
    assert _engine_nets(client, created)[NETWORK_NAME]["NetworkID"] == NETWORK_ID
    assert client.ops == []


def test_absent_container_reconstruction_uses_create_path(world):
    docker_manager, client, _tmp = world
    _meta(docker_manager)
    assert docker_manager.launch_sandbox(CHAT) == {"state": "running"}
    created = client.created[0]
    assert created.status == "running"
    assert created.id == docker_manager._lookup_container(CHAT).id
    assert created._create_config["network"] == NETWORK_ID
    assert created._create_config["ports"] == {CDP: (GATEWAY, None), TTYD: (GATEWAY, None)}
    assert list(_engine_nets(client, created)) == [NETWORK_NAME]
    assert docker_manager.load_container_meta(CHAT)["user_email"] == "owner@example"


def test_stop_launch_and_restart_alias_keep_one_membership(world):
    docker_manager, client, _tmp = world
    container = client.put(_name(docker_manager), status="running", container_id="keep-me")
    container.stop()
    assert container.status == "exited"
    before = dict(_engine_nets(client, container))
    assert docker_manager.launch_sandbox(CHAT) == {"state": "running"}
    assert container.status == "running"
    assert container.id == "keep-me"
    assert _engine_nets(client, container) == before
    assert container._removed["value"] is False
    container.stop()
    assert docker_manager.launch_sandbox(CHAT) == {"state": "running"}
    assert list(_engine_nets(client, container)) == [NETWORK_NAME]
    assert _engine_nets(client, container)[NETWORK_NAME]["NetworkID"] == NETWORK_ID
    assert container._removed["value"] is False


def test_missing_sandbox_network_fails_before_create(world):
    docker_manager, client, _tmp = world
    client.clear_networks()
    with pytest.raises(docker_manager.LaunchFailed) as missing:
        docker_manager._create_container(CHAT, _name(docker_manager))
    assert missing.value.status_code == 500
    assert client.created == []
    assert _name(docker_manager) not in client.store


def test_invalid_network_configuration_does_not_create_or_mutate(world):
    docker_manager, client, _tmp = world
    existing = client.put(_name(docker_manager), status="exited", container_id="stay")

    client.replace_named_network(NETWORK_NAME, NETWORK_ID, driver="overlay")
    with pytest.raises(docker_manager.LaunchFailed):
        docker_manager.launch_sandbox(CHAT)
    assert existing.status == "exited"
    assert existing._started["n"] == 0
    assert existing._removed["value"] is False

    client.replace_named_network(NETWORK_NAME, NETWORK_ID, internal=True)
    with pytest.raises(docker_manager.LaunchFailed):
        docker_manager.launch_sandbox(CHAT)
    assert existing._started["n"] == 0

    client.replace_named_network(NETWORK_NAME, NETWORK_ID, gateway=None)
    with pytest.raises(docker_manager.LaunchFailed):
        docker_manager.launch_sandbox(CHAT)
    assert existing._started["n"] == 0

    client.replace_named_network(NETWORK_NAME, NETWORK_ID, gateway=["172.31.0.1", "172.31.0.2"])
    with pytest.raises(docker_manager.LaunchFailed):
        docker_manager.launch_sandbox(CHAT)
    assert existing._started["n"] == 0

    client.replace_named_network(NETWORK_NAME, NETWORK_ID)
    docker_manager.SANDBOX_HOST_BIND_IP = "172.30.0.1"
    with pytest.raises(docker_manager.LaunchFailed):
        docker_manager._create_container("aaaaaaaa-bbbb-cccc-dddd-ffffffffffff", "owui-chat-fresh")
    assert client.created == []
    assert existing.status == "exited"
    assert existing._started["n"] == 0


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

    client.seed_membership(winner, {COMPOSE_NAME: {"NetworkID": COMPOSE_ID, "IPAddress": "172.18.0.5"}})
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
    client.add_network(NETWORK_NAME, STALE_ID, gateway=GATEWAY)
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
    assert _engine_nets(client, container) == {
        NETWORK_NAME: {"NetworkID": NETWORK_ID, "IPAddress": "172.31.0.10"}
    }
    assert (STALE_ID, "disconnect", container.id) in client.ops
    assert (NETWORK_ID, "connect", container.id) in client.ops
    assert container._removed["value"] is False


def test_same_name_network_replacement_is_repaired_by_id(world):
    docker_manager, client, _tmp = world
    client.add_network(NETWORK_NAME, STALE_ID, gateway=GATEWAY)
    container = client.put(
        _name(docker_manager),
        status="exited",
        container_id="replaced-id",
        networks={NETWORK_NAME: {"NetworkID": STALE_ID, "IPAddress": "172.31.0.10"}},
    )
    assert docker_manager.launch_sandbox(CHAT) == {"state": "running"}
    assert list(_engine_nets(client, container)) == [NETWORK_NAME]
    assert _engine_nets(client, container)[NETWORK_NAME]["NetworkID"] == NETWORK_ID
    assert (STALE_ID, "disconnect", container.id) in client.ops
    assert (NETWORK_ID, "connect", container.id) in client.ops
    assert container._removed["value"] is False


def test_foreign_membership_repaired_on_stopped_launch(world):
    docker_manager, client, _tmp = world
    container = client.put(
        _name(docker_manager),
        status="exited",
        container_id="foreign",
        networks={COMPOSE_NAME: {"NetworkID": COMPOSE_ID, "IPAddress": "172.18.0.9"}},
    )
    assert docker_manager.launch_sandbox(CHAT) == {"state": "running"}
    assert container.id == "foreign"
    assert list(_engine_nets(client, container)) == [NETWORK_NAME]
    assert _engine_nets(client, container)[NETWORK_NAME]["NetworkID"] == NETWORK_ID
    assert (COMPOSE_ID, "disconnect", container.id) in client.ops
    assert (NETWORK_ID, "connect", container.id) in client.ops
    assert container._removed["value"] is False


def test_repair_revalidates_refreshed_gateway_before_start(world):
    docker_manager, client, _tmp = world
    container = client.put(
        _name(docker_manager),
        status="exited",
        container_id="gw-race",
        networks={COMPOSE_NAME: {"NetworkID": COMPOSE_ID, "IPAddress": "172.18.0.9"}},
    )
    _meta(docker_manager)
    workspace = Path(docker_manager.USER_DATA_BASE_PATH) / CHAT
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "layer").write_text("keep")
    replacement = FakeNetwork(client, NETWORK_NAME, "netid-ocu-sandbox-g2", gateway="172.31.9.1")

    def after_first(count):
        if count >= 2:
            client.replace_named_network(NETWORK_NAME, "netid-ocu-sandbox-g2", gateway="172.31.9.1")
            return replacement
        return None

    client.on_sandbox_lookup = after_first
    with pytest.raises(docker_manager.LaunchFailed) as caught:
        docker_manager.launch_sandbox(CHAT)
    assert caught.value.status_code == 409
    assert container.status == "exited"
    assert container._started["n"] == 0
    assert container.id == "gw-race"
    assert container._removed["value"] is False
    assert docker_manager.load_container_meta(CHAT)["user_email"] == "owner@example"
    assert (workspace / "layer").read_text() == "keep"


def test_same_gateway_id_replacement_still_repairs(world):
    docker_manager, client, _tmp = world
    container = client.put(
        _name(docker_manager),
        status="exited",
        container_id="gw-same",
        networks={COMPOSE_NAME: {"NetworkID": COMPOSE_ID, "IPAddress": "172.18.0.9"}},
    )
    replacement = FakeNetwork(client, NETWORK_NAME, "netid-ocu-sandbox-g1b", gateway=GATEWAY)

    def after_first(count):
        if count >= 2:
            client.replace_named_network(NETWORK_NAME, "netid-ocu-sandbox-g1b", gateway=GATEWAY)
            return replacement
        return None

    client.on_sandbox_lookup = after_first
    assert docker_manager.launch_sandbox(CHAT) == {"state": "running"}
    assert container.status == "running"
    assert _engine_nets(client, container)[NETWORK_NAME]["NetworkID"] == "netid-ocu-sandbox-g1b"
    assert container._removed["value"] is False


def test_create_pins_inspected_network_id_against_same_name_swap(world):
    docker_manager, client, _tmp = world
    client.replace_named_network_on_create = lambda: client.replace_named_network(
        NETWORK_NAME, "netid-ocu-sandbox-swapped", internal=True, gateway=GATEWAY
    )
    with pytest.raises(docker_manager.LaunchFailed):
        docker_manager._create_container(CHAT, _name(docker_manager))
    if client.created:
        created = client.created[0]
        assert created._started["n"] == 0
        assert created._removed["value"] is False
        nets = _engine_nets(client, created)
        assert "netid-ocu-sandbox-swapped" not in {item["NetworkID"] for item in nets.values()}
    else:
        assert client.created == []


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
    assert _engine_nets(client, container) == {
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
    assert NETWORK_NAME not in _engine_nets(client, container)


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
    assert COMPOSE_NAME in _engine_nets(client, container)

    client.networks.get(COMPOSE_ID).fail_disconnect = False
    client.networks.get(NETWORK_NAME).fail_connect = True
    client.seed_membership(container, {COMPOSE_NAME: {"NetworkID": COMPOSE_ID, "IPAddress": "172.18.0.9"}})
    with pytest.raises(docker_manager.LaunchFailed):
        docker_manager.launch_sandbox(CHAT)
    assert container._started["n"] == 0
    assert NETWORK_NAME not in _engine_nets(client, container) or _engine_nets(client, container).get(COMPOSE_NAME)


def test_noop_or_wrong_id_detach_fails_without_start(world):
    docker_manager, client, _tmp = world
    compose = client.networks.get(COMPOSE_ID)
    compose.noop_disconnect = True
    container = client.put(
        _name(docker_manager),
        status="exited",
        container_id="noop-detach",
        networks={COMPOSE_NAME: {"NetworkID": COMPOSE_ID, "IPAddress": "172.18.0.9"}},
    )
    with pytest.raises(docker_manager.LaunchFailed) as caught:
        docker_manager.launch_sandbox(CHAT)
    assert caught.value.status_code == 500
    assert container.status == "exited"
    assert container._started["n"] == 0
    assert COMPOSE_NAME in _engine_nets(client, container)
    assert container._removed["value"] is False


def test_missing_stale_network_object_fails_without_start_or_forged_inspect(world):
    docker_manager, client, _tmp = world
    container = client.put(
        _name(docker_manager),
        status="exited",
        container_id="missing-stale",
        networks={NETWORK_NAME: {"NetworkID": STALE_ID, "IPAddress": "172.31.0.10"}},
    )
    _meta(docker_manager)
    workspace = Path(docker_manager.USER_DATA_BASE_PATH) / CHAT
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "layer").write_text("keep")
    meta = docker_manager._get_meta_path(CHAT)
    meta_bytes = meta.read_text()
    client._network_store.pop(STALE_ID, None)
    assert STALE_ID not in {net.id for net in {id(n): n for n in client._network_store.values()}.values()}
    container.attrs["NetworkSettings"]["Networks"] = {
        NETWORK_NAME: {"NetworkID": NETWORK_ID, "IPAddress": "172.31.0.10"},
    }
    with pytest.raises(docker_manager.LaunchFailed) as caught:
        docker_manager.launch_sandbox(CHAT)
    assert caught.value.status_code == 500
    assert container.status == "exited"
    assert container._started["n"] == 0
    assert container._unpaused["n"] == 0
    assert container._removed["value"] is False
    assert container.id == "missing-stale"
    assert _engine_nets(client, container)[NETWORK_NAME]["NetworkID"] == STALE_ID
    assert docker_manager.load_container_meta(CHAT)["user_email"] == "owner@example"
    assert meta.read_text() == meta_bytes
    assert (workspace / "layer").read_text() == "keep"
    assert (NETWORK_ID, "connect", container.id) not in client.ops

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


def _policy(monkeypatch, docker_manager, raw):
    monkeypatch.setenv("OCU_SANDBOX_DNS", raw)


def test_policy_create_pins_ordered_dns_and_empty_override(world, monkeypatch):
    docker_manager, client, _tmp = world
    _policy(monkeypatch, docker_manager, "8.8.8.8,1.1.1.1")
    docker_manager._get_or_create_container(CHAT)
    created = client.created[0]
    assert created._create_config["dns"] == ["8.8.8.8", "1.1.1.1"]
    assert created.attrs["HostConfig"]["Dns"] == ["8.8.8.8", "1.1.1.1"]
    assert created.status == "running"

    monkeypatch.setenv("OCU_SANDBOX_DNS", "")
    docker_manager._get_or_create_container("aaaaaaaa-bbbb-cccc-dddd-ffffffffffff")
    empty = client.created[1]
    assert empty._create_config["dns"] == ["127.0.0.11"]
    assert empty.attrs["HostConfig"]["Dns"] == ["127.0.0.11"]


def test_absent_policy_create_does_not_send_dns(world):
    docker_manager, client, _tmp = world
    docker_manager._get_or_create_container(CHAT)
    created = client.created[0]
    assert "dns" not in created._create_config
    assert "Dns" not in created.attrs["HostConfig"]


def test_disabled_create_skips_dns_args_with_valid_or_invalid_policy(world, monkeypatch):
    docker_manager, client, _tmp = world
    docker_manager.ENABLE_NETWORK = False
    _policy(monkeypatch, docker_manager, "8.8.8.8")
    docker_manager._get_or_create_container(CHAT)
    created = client.created[0]
    assert created._create_config.get("network_disabled") is True
    assert "dns" not in created._create_config
    assert "Dns" not in created.attrs["HostConfig"]
    assert client.network_gets == []

    monkeypatch.setenv("OCU_SANDBOX_DNS", "not-an-ip")
    with pytest.raises(docker_manager.SandboxDnsConfigError):
        docker_manager._create_container("aaaaaaaa-bbbb-cccc-dddd-ffffffffffff", "owui-chat-invalid-dns")
    assert len(client.created) == 1


def test_metadata_recreate_uses_current_dns_policy(world, monkeypatch):
    docker_manager, client, _tmp = world
    _meta(docker_manager)
    _policy(monkeypatch, docker_manager, "1.1.1.1")
    assert docker_manager.launch_sandbox(CHAT) == {"state": "running"}
    created = client.created[0]
    assert created._create_config["dns"] == ["1.1.1.1"]
    assert created.attrs["HostConfig"]["Dns"] == ["1.1.1.1"]
    assert docker_manager.load_container_meta(CHAT)["user_email"] == "owner@example"


def test_hostname_retry_keeps_current_dns(world, monkeypatch):
    docker_manager, client, _tmp = world
    _policy(monkeypatch, docker_manager, "8.8.8.8")
    client.refuse_hostname = True
    docker_manager._create_container(CHAT, _name(docker_manager))
    created = client.created[0]
    assert "hostname" not in created._create_config
    assert created._create_config["dns"] == ["8.8.8.8"]
    assert created.attrs["HostConfig"]["Dns"] == ["8.8.8.8"]


def test_created_dns_mismatch_refuses_before_start(world, monkeypatch):
    docker_manager, client, _tmp = world
    _policy(monkeypatch, docker_manager, "8.8.8.8")
    client.inspect_dns_mismatch = ["1.1.1.1"]
    with pytest.raises(docker_manager.LaunchFailed) as caught:
        docker_manager._create_container(CHAT, _name(docker_manager))
    assert caught.value.status_code == 500
    created = client.created[0]
    assert created._started["n"] == 0
    assert created._removed["value"] is False


def test_running_reuse_refuses_inherited_or_reordered_dns(world, monkeypatch):
    docker_manager, client, tmp = world
    _policy(monkeypatch, docker_manager, "8.8.8.8,1.1.1.1")
    inherited = client.put(_name(docker_manager), status="running", container_id="run-inherit")
    ops_before = list(client.ops)
    with pytest.raises(docker_manager.LaunchFailed) as caught:
        docker_manager._get_or_create_container(CHAT)
    assert caught.value.status_code == 409
    assert inherited.status == "running"
    assert inherited._started["n"] == 0
    assert inherited._unpaused["n"] == 0
    assert inherited._removed["value"] is False
    assert client.ops == ops_before

    reordered = client.put(
        _name(docker_manager),
        status="running",
        container_id="run-reorder",
        dns=["1.1.1.1", "8.8.8.8"],
    )
    with pytest.raises(docker_manager.LaunchFailed):
        docker_manager._get_or_create_container(CHAT)
    assert reordered.status == "running"
    assert reordered._removed["value"] is False

    compatible = client.put(
        _name(docker_manager),
        status="running",
        container_id="run-ok",
        dns=["8.8.8.8", "1.1.1.1"],
    )
    assert docker_manager._get_or_create_container(CHAT) is compatible
    assert compatible._started["n"] == 0


@pytest.mark.parametrize("status", ["running", "paused", "exited"])
def test_launch_refuses_incompatible_dns_before_mutation(world, monkeypatch, status):
    docker_manager, client, tmp = world
    _policy(monkeypatch, docker_manager, "8.8.8.8")
    _meta(docker_manager)
    workspace = Path(docker_manager.USER_DATA_BASE_PATH) / CHAT
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "layer").write_text("keep")
    meta_bytes = docker_manager._get_meta_path(CHAT).read_bytes()
    container = client.put(
        _name(docker_manager),
        status=status,
        container_id=f"dns-{status}",
        networks={COMPOSE_NAME: {"NetworkID": COMPOSE_ID, "IPAddress": "172.18.0.9"}},
        dns=["1.1.1.1"],
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
    assert docker_manager._get_meta_path(CHAT).read_bytes() == meta_bytes
    assert (workspace / "layer").read_text() == "keep"


def test_create_conflict_adopts_only_compatible_dns_winner(world, monkeypatch):
    docker_manager, client, _tmp = world
    _policy(monkeypatch, docker_manager, "8.8.8.8")
    winner = client.put(_name(docker_manager), status="running", container_id="dns-win", dns=["8.8.8.8"])

    def conflict(**config):
        response = MagicMock(status_code=409, url="http://docker.test", reason="Conflict")
        raise APIError("Conflict", response=response, explanation=b"conflict")

    client.containers.create = conflict
    adopted = docker_manager._create_container(CHAT, _name(docker_manager))
    assert adopted is winner

    client.seed_membership(winner, {NETWORK_NAME: {"NetworkID": NETWORK_ID, "IPAddress": "172.31.0.10"}})
    winner.attrs["HostConfig"]["Dns"] = ["1.1.1.1"]
    with pytest.raises(docker_manager.LaunchFailed):
        docker_manager._create_container(CHAT, _name(docker_manager))
    assert winner.status == "running"
    assert winner._removed["value"] is False


def test_invalid_dns_configuration_fails_without_docker(world, monkeypatch):
    docker_manager, client, _tmp = world
    monkeypatch.setenv("OCU_SANDBOX_DNS", "8.8.8.8,8.8.8.8")
    with pytest.raises(docker_manager.SandboxDnsConfigError):
        docker_manager.validate_sandbox_dns_configuration()
    assert client.created == []
    assert client.network_gets == []
