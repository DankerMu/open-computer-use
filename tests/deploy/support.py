# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Independent literals and paths for deployment preflight tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
CHECK_PORTS = ROOT / "deploy" / "check-ports.sh"
PROVISION = ROOT / "deploy" / "provision-networks.sh"
UP = ROOT / "deploy" / "up.sh"
FIREWALL_INSTALL = ROOT / "deploy" / "firewall" / "docker-user-rules.sh"
FIREWALL_CHECK = ROOT / "deploy" / "firewall" / "check.sh"
CHECK_SANDBOX_DNS = ROOT / "deploy" / "check-sandbox-dns.sh"
FAKE_DOCKER = ROOT / "tests" / "deploy" / "fakebin" / "docker"
CORE_OVERRIDE = ROOT / "deploy" / "production-like-test" / "compose.core.override.yml"
WEBUI_OVERRIDE = ROOT / "deploy" / "production-like-test" / "compose.webui.override.yml"
PROXY_COMPOSE = ROOT / "deploy" / "production-like-test" / "compose.proxy.yml"
PROXY_DOCKERFILE = ROOT / "deploy" / "proxy" / "Dockerfile"
PROXY_ENTRYPOINT = ROOT / "deploy" / "proxy" / "entrypoint.sh"
PROXY_DOCKERIGNORE = ROOT / "deploy" / "proxy" / ".dockerignore"

CONTROL_NETWORK = "ocu-test-private"
SANDBOX_NETWORK = "ocu-sandbox"
PROXY_SERVICE = "proxy"
WEBUI_SERVICE = "open-webui"
OCU_SERVICE = "computer-use-server"
PROXY_TARGET = 8082
PROXY_PUBLISHED = "8082"
DEFAULT_ALLOW = "8.8.8.8/32,1.1.1.1/32"
DEFAULT_DNS = "8.8.8.8"
METADATA_ADDR = "169.254.169.254"
OWNED_IPV4 = "OCU-SANDBOX-EGRESS"
OWNED_IPV6 = "OCU-SANDBOX-EGRESS6"


def control_networks():
    return {
        "default": {
            "name": CONTROL_NETWORK,
            "driver": "bridge",
            "ipam": {"config": [{"subnet": "172.30.0.0/24", "gateway": "172.30.0.1"}]},
        }
    }


def proxy_mapping(*, published=PROXY_PUBLISHED, target=PROXY_TARGET, protocol="tcp", host_ip=""):
    mapping = {"target": target, "published": published, "protocol": protocol}
    if host_ip != "":
        mapping["host_ip"] = host_ip
    return mapping


def service(*, ports=None, networks=None, network_mode=None, expose=None, environment=None):
    body = {}
    if ports is not None:
        body["ports"] = ports
    if networks is not None:
        body["networks"] = networks
    if network_mode is not None:
        body["network_mode"] = network_mode
    if expose is not None:
        body["expose"] = expose
    if environment is not None:
        body["environment"] = environment
    return body


def stack(services, networks=None):
    return {"services": services, "networks": networks if networks is not None else control_networks()}


def intended_core():
    return stack(
        {
            "workspace": service(networks={"default": {}}),
            OCU_SERVICE: service(
                networks={"default": {}},
                environment={"OCU_SANDBOX_DNS": DEFAULT_DNS},
            ),
            "cleanup": service(networks={"default": {}}),
            "retention-guard": service(networks={"default": {}}),
        }
    )

def with_core_dns(docs, value):
    payload = dict(docs)
    core = dict(payload["core.json"])
    services = dict(core["services"])
    service_body = dict(services[OCU_SERVICE])
    environment = dict(service_body.get("environment") or {})
    environment["OCU_SANDBOX_DNS"] = value
    service_body["environment"] = environment
    services[OCU_SERVICE] = service_body
    core["services"] = services
    payload["core.json"] = core
    return payload


def intended_webui():
    return stack(
        {
            WEBUI_SERVICE: service(networks={"default": {}}, expose=["8080"]),
            "postgres": service(networks={"default": {}}),
            "open-webui-init": service(networks={"default": {}}),
        }
    )


def intended_proxy():
    return stack(
        {
            PROXY_SERVICE: service(
                networks={"default": {}},
                ports=[proxy_mapping()],
                environment={
                    "OCU_PROXY_LISTEN": "0.0.0.0:8082",
                    "OCU_WEBUI_UPSTREAM": "http://open-webui:8080",
                    "OCU_PROXY_UPSTREAM": "http://computer-use-server:8081",
                },
            )
        }
    )


def intended_docs():
    return {
        "core.json": intended_core(),
        "webui.json": intended_webui(),
        "proxy.json": intended_proxy(),
    }


def write_json(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path


def write_docs(directory: Path, docs=None) -> list[Path]:
    docs = intended_docs() if docs is None else docs
    paths = []
    for name, payload in docs.items():
        paths.append(write_json(directory / name, payload))
    return paths


def run_checker(paths, *, env=None, extra_env=None):
    merged = os.environ.copy() if env is None else dict(env)
    if env is None:
        merged.update({
            "OCU_PRIVATE_NETWORK": CONTROL_NETWORK,
            "OCU_SANDBOX_NETWORK": SANDBOX_NETWORK,
            "OCU_PROXY_PORT": PROXY_PUBLISHED,
        })
    if extra_env:
        merged.update(extra_env)
    return subprocess.run(
        ["bash", str(CHECK_PORTS), *[str(path) for path in paths]],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=merged,
        check=False,
    )


def fake_env(state_dir: Path, extra=None):
    env = os.environ.copy()
    env["PATH"] = str(FAKE_DOCKER.parent) + os.pathsep + env.get("PATH", "")
    env["FAKE_DOCKER_STATE"] = str(state_dir)
    env["TMPDIR"] = str(state_dir)
    env["COMPOSE_PROJECT_NAME"] = "ocu-test"
    env["OCU_PRIVATE_NETWORK"] = CONTROL_NETWORK
    env["OCU_PRIVATE_SUBNET"] = "172.30.0.0/24"
    env["OCU_PRIVATE_GATEWAY"] = "172.30.0.1"
    env["OCU_SANDBOX_NETWORK"] = SANDBOX_NETWORK
    env["OCU_SANDBOX_SUBNET"] = "172.31.0.0/24"
    env["OCU_SANDBOX_GATEWAY"] = "172.31.0.1"
    env["OCU_PROXY_PORT"] = PROXY_PUBLISHED
    env["OCU_INTERNAL_TOKEN"] = "synthetic-internal-token"
    env["OCU_WEBUI_ORIGIN"] = "http://localhost:8082"
    env["OCU_WEBUI_AUTH_URL"] = "http://open-webui:8080/api/v1/ocu/auth"
    env["PUBLIC_BASE_URL"] = "http://localhost:8082/ocu"
    env["OCU_PROXY_IMAGE"] = "ocu-test-proxy:synthetic"
    env["OCU_SANDBOX_EGRESS_ALLOW"] = DEFAULT_ALLOW
    env["OCU_SANDBOX_DNS"] = DEFAULT_DNS
    env["OCU_SANDBOX_EGRESS_LOCK"] = str(state_dir / "ocu-sandbox-egress.lock")
    if extra:
        env.update(extra)
    return env


def write_network(state_dir: Path, name, *, subnet, gateway, driver="bridge", internal=False, net_id=None, options=None):
    networks = state_dir / "networks"
    networks.mkdir(parents=True, exist_ok=True)
    net_id = net_id or f"id-{name}"
    payload = {
        "Name": name,
        "Id": net_id,
        "Driver": driver,
        "Internal": internal,
        "Options": options if options is not None else {"com.docker.network.bridge.name": "br-" + net_id[:12]},
        "IPAM": {"Config": [{"Subnet": subnet, "Gateway": gateway}]},
    }
    (networks / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")



def write_containers(state_dir: Path, containers) -> Path:
    path = state_dir / "containers.json"
    path.write_text(json.dumps(containers), encoding="utf-8")
    return path


def sandbox_container(
    *,
    cid,
    name,
    state="running",
    network_mode=None,
    networks=None,
    dns=None,
    labels=None,
    host_config=None,
    network_settings=None,
    omit_host_config=False,
    omit_network_settings=False,
):
    mode = SANDBOX_NETWORK if network_mode is None else network_mode
    membership = {SANDBOX_NETWORK: {"NetworkID": f"id-{SANDBOX_NETWORK}", "IPAddress": "172.31.0.10"}}
    if networks is not None:
        membership = networks
    host = {"NetworkMode": mode}
    if dns is not None:
        host["Dns"] = dns
    if host_config:
        host.update(host_config)
    body = {
        "Id": cid,
        "Name": name,
        "State": state,
        "Labels": dict(labels or {}),
    }
    if not omit_host_config:
        body["HostConfig"] = host
    if not omit_network_settings:
        body["NetworkSettings"] = (
            network_settings if network_settings is not None else {"Networks": membership}
        )
    return body

def write_firewall(state_dir: Path, payload) -> Path:
    path = state_dir / "firewall.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def load_firewall(state_dir: Path) -> dict:
    path = state_dir / "firewall.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def seed_healthy_host(state_dir: Path) -> None:
    # Permissive blankets stay last so a missing owned hook still lets
    # traffic flow; foreign policy is seeded in front of those terminators.
    write_firewall(
        state_dir,
        {
            "ipv4": {
                "DOCKER-USER": [["-j", "RETURN"]],
                "INPUT": [["-j", "ACCEPT"]],
                "FORWARD": [["-j", "DOCKER-USER"], ["-j", "ACCEPT"]],
                "OUTPUT": [["-j", "ACCEPT"]],
            },
            "ipv6": {
                "INPUT": [["-j", "ACCEPT"]],
                "FORWARD": [["-j", "ACCEPT"]],
                "OUTPUT": [["-j", "ACCEPT"]],
            },
            "policies": {
                "ipv4": {"INPUT": "ACCEPT", "FORWARD": "ACCEPT", "OUTPUT": "ACCEPT", "DOCKER-USER": "-"},
                "ipv6": {"INPUT": "ACCEPT", "FORWARD": "ACCEPT", "OUTPUT": "ACCEPT"},
            },
        },
    )


def run_script(script: Path, env, *, timeout=20):
    return subprocess.run(
        ["bash", str(script)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=timeout,
    )

def write_fake_configs(state_dir: Path, docs=None):
    config_dir = state_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    write_docs(config_dir, docs)


def ops(state_dir: Path) -> list[str]:
    log = state_dir / "ops.log"
    if not log.exists():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line]


def tmp_dir():
    return tempfile.TemporaryDirectory(prefix="ocu-deploy-test-")


