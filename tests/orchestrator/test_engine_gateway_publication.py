# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Opt-in engine qualification for gateway-bound sandbox publication (#442).

Creates a temporary bridge and a container named like a chat sandbox that
publishes both SANDBOX_PUBLISHED_PORTS on the bridge gateway in the
_create_container ``ports=`` shape, then resolves each port through the real
get_container_service_address against the real engine. Per port, the verdict
must match the engine's own NetworkSettings.Ports: an assigned host port
resolves to ``gateway:host_port``; an unassigned one raises
GatewayPublicationUnassigned. The test passes on both profiles and writes a
JSON record of which one it saw.

Gating: skipped unless OCU_ENGINE_PROBE=1 is exported; skipped when the
Docker engine does not answer ping.

Env knobs:
  OCU_ENGINE_PROBE_SUBNET  IPv4 subnet of the temporary bridge
                           (default 10.242.0.0/24; the gateway is host .1)
  OCU_ENGINE_PROBE_IMAGE   Container image, pulled when absent (default alpine:3.20)
  OCU_ENGINE_PROBE_OUT     JSON record path
                           (default <tmp_path>/engine-gateway-publication.json)

Run:
  OCU_ENGINE_PROBE=1 python -m pytest tests/orchestrator/test_engine_gateway_publication.py -v -s
"""

import ipaddress
import json
import os
import platform
import sys
import time
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[2] / "computer-use-server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

pytestmark = pytest.mark.skipif(
    os.environ.get("OCU_ENGINE_PROBE", "").strip().lower() not in ("1", "true", "yes"),
    reason="Engine gateway-publication probe is opt-in; export OCU_ENGINE_PROBE=1 to enable.",
)

LABEL = {"managed-by": "ocu-engine-probe"}
RUNNING_WAIT_SECONDS = 15


def _wait_running(container):
    deadline = time.monotonic() + RUNNING_WAIT_SECONDS
    while True:
        container.reload()
        if container.status == "running":
            return
        if time.monotonic() >= deadline:
            pytest.fail(f"probe container did not reach running within {RUNNING_WAIT_SECONDS}s: {container.status}")
        time.sleep(0.2)


def _image(client, reference):
    import docker

    try:
        return client.images.get(reference)
    except docker.errors.ImageNotFound:
        return client.images.pull(reference)


def _assigned_host_port(entries):
    for entry in entries or []:
        host_port = (entry or {}).get("HostPort")
        if host_port not in ("", "0", None):
            return host_port
    return None


def test_gateway_publication_verdict_matches_the_engine(monkeypatch, tmp_path):
    import docker
    import docker_manager

    try:
        client = docker.from_env()
        client.ping()
    except Exception as exc:
        pytest.skip(f"Docker engine not reachable: {exc}")

    subnet = os.environ.get("OCU_ENGINE_PROBE_SUBNET", "10.242.0.0/24")
    gateway = str(next(ipaddress.ip_network(subnet).hosts()))
    reference = os.environ.get("OCU_ENGINE_PROBE_IMAGE", "alpine:3.20")
    out = Path(os.environ.get("OCU_ENGINE_PROBE_OUT") or tmp_path / "engine-gateway-publication.json")
    pid = os.getpid()
    network_name = f"ocu-engine-probe-{pid}"
    chat_id = f"engine-probe-{pid}"
    container_name = f"owui-chat-{chat_id}"
    image = _image(client, reference)

    network = None
    try:
        try:
            network = client.networks.create(
                network_name,
                driver="bridge",
                ipam=docker.types.IPAMConfig(
                    pool_configs=[docker.types.IPAMPool(subnet=subnet, gateway=gateway)]
                ),
            )
        except docker.errors.APIError as exc:
            pytest.fail(f"cannot create probe network on OCU_ENGINE_PROBE_SUBNET={subnet}: {exc}")
        container = client.containers.create(
            reference,
            command=["sleep", "120"],
            name=container_name,
            labels=LABEL,
            network=network.id,
            ports={f"{port}/tcp": (gateway, None) for port in docker_manager.SANDBOX_PUBLISHED_PORTS},
            detach=True,
        )
        container.start()
        _wait_running(container)

        published = container.attrs["NetworkSettings"]["Ports"] or {}
        bindings = container.attrs["HostConfig"]["PortBindings"] or {}
        monkeypatch.setattr(docker_manager, "get_docker_client", lambda: client)
        verdict = {}
        for port in docker_manager.SANDBOX_PUBLISHED_PORTS:
            key = f"{port}/tcp"
            assert bindings[key][0]["HostIp"] == gateway
            assert bindings[key][0]["HostPort"] in ("", "0")
            host_port = _assigned_host_port(published.get(key))
            if host_port is not None:
                address = docker_manager.get_container_service_address(chat_id, port)
                assert address == f"{gateway}:{host_port}"
                verdict[str(port)] = f"assigned {address}"
            else:
                with pytest.raises(docker_manager.GatewayPublicationUnassigned) as raised:
                    docker_manager.get_container_service_address(chat_id, port)
                assert key in str(raised.value)
                assert gateway in str(raised.value)
                verdict[str(port)] = "unassigned"

        version = client.version()
        info = client.info()
        record = {
            "engine": {
                "version": version.get("Version"),
                "os": version.get("Os"),
                "arch": version.get("Arch"),
                "api_version": version.get("ApiVersion"),
            },
            "platform": {
                "kernel": info.get("KernelVersion"),
                "storage_driver": info.get("Driver"),
                "driver_status": info.get("DriverStatus"),
                "architecture": info.get("Architecture"),
                "os_type": info.get("OSType"),
                "operating_system": info.get("OperatingSystem"),
            },
            "host": {"platform": platform.platform(), "machine": platform.machine()},
            "image": {
                "reference": reference,
                "platform": f"{image.attrs['Os']}/{image.attrs['Architecture']}",
            },
            "gateway": gateway,
            "ports": published,
            "bindings": bindings,
            "verdict": verdict,
        }
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        print(f"engine gateway-publication record: {out}")
    finally:
        try:
            client.containers.get(container_name).remove(force=True)
        except docker.errors.NotFound:
            pass
        if network is not None:
            network.remove()
