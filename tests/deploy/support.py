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
    attached = service(networks={"default": {}})
    return stack(
        {
            "workspace": service(),
            OCU_SERVICE: attached,
            "cleanup": service(),
            "retention-guard": attached,
        }
    )


def intended_webui():
    attached = service(networks={"default": {}})
    return stack(
        {
            WEBUI_SERVICE: service(networks={"default": {}}, expose=["8080"]),
            "postgres": attached,
            "open-webui-init": attached,
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
        for name in (
            "OCU_CHECK_PROXY_SERVICE", "OCU_CHECK_WEBUI_SERVICE", "OCU_CHECK_OCU_SERVICE",
            "OCU_CHECK_PROXY_TARGET", "OCU_CHECK_PROXY_PUBLISHED",
            "OCU_CHECK_SANDBOX_NETWORK", "OCU_CHECK_CONTROL_NETWORK",
        ):
            merged.pop(name, None)
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
    if extra:
        env.update(extra)
    return env


def write_network(state_dir: Path, name, *, subnet, gateway, driver="bridge", internal=False):
    networks = state_dir / "networks"
    networks.mkdir(parents=True, exist_ok=True)
    payload = {
        "Name": name,
        "Id": f"id-{name}",
        "Driver": driver,
        "Internal": internal,
        "IPAM": {"Config": [{"Subnet": subnet, "Gateway": gateway}]},
    }
    (networks / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")


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


