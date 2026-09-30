# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Independent literals and paths for deployment preflight tests."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tarfile
import tempfile





ROOT = Path(__file__).resolve().parents[2]
CHECK_PORTS = ROOT / "deploy" / "check-ports.sh"
PROVISION = ROOT / "deploy" / "provision-networks.sh"
UP = ROOT / "deploy" / "up.sh"
FIREWALL_INSTALL = ROOT / "deploy" / "firewall" / "docker-user-rules.sh"
FIREWALL_CHECK = ROOT / "deploy" / "firewall" / "check.sh"
CHECK_SANDBOX_DNS = ROOT / "deploy" / "check-sandbox-dns.sh"
SMOKE = ROOT / "deploy" / "smoke.sh"
SMOKE_PY = ROOT / "deploy" / "smoke_deployment.py"
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
ROLE_ORDER = (
    "workspace",
    "computer-use-server",
    "retention-guard",
    "proxy",
    "open-webui",
    "postgres",
)
RUNTIME_IMAGE_VARS = {
    "workspace": "DOCKER_IMAGE",
    "computer-use-server": "COMPUTER_USE_SERVER_IMAGE",
    "retention-guard": "RETENTION_GUARD_IMAGE",
    "proxy": "OCU_PROXY_IMAGE",
    "open-webui": "OPENWEBUI_IMAGE",
    "postgres": "POSTGRES_IMAGE",
}
DEFAULT_RELEASE_IMAGES = {
    "workspace": "open-computer-use:synthetic",
    "computer-use-server": "ocu-computer-use-server:synthetic",
    "retention-guard": "ocu-retention-guard:synthetic",
    "proxy": "ocu-test-proxy:synthetic",
    "open-webui": "ocu-open-webui:synthetic",
    "postgres": "postgres:17-alpine",
}
DEFAULT_RELEASE_DIGESTS = {
    role: "sha256:" + hashlib.sha256(f"synthetic-{role}".encode("utf-8")).hexdigest()
    for role in ROLE_ORDER
}
FORMAT_VERSION = 1
PLATFORM = "linux/amd64"
WEBUI_SYNTHETIC_SHA = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


UP_FIXTURE_PATHS = (
    "deploy/up.sh",
    "deploy/release.py",
    "deploy/__init__.py",
    "deploy/check-ports.sh",

    "deploy/provision-networks.sh",
    "deploy/check-sandbox-dns.sh",
    "deploy/check_sandbox_dns.py",
    "deploy/netinspect.py",
    "deploy/firewall/docker-user-rules.sh",
    "deploy/firewall/check.sh",
    "deploy/firewall/policy.py",
    "deploy/firewall/__init__.py",

    "deploy/production-like-test/compose.core.override.yml",
    "deploy/production-like-test/compose.webui.override.yml",
    "deploy/production-like-test/compose.proxy.yml",
    "deploy/production-like-test/init/run-init.sh",
    "openwebui/init.sh",
    "openwebui/tools/computer_use_tools.py",
    "openwebui/functions/computer_link_filter.py",
    "computer-use-server/sandbox_dns.py",
    "docker-compose.yml",
    "docker-compose.webui.yml",
)



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


def service(*, ports=None, networks=None, network_mode=None, expose=None, environment=None, image=None):
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
    if image is not None:
        body["image"] = image
    return body



def stack(services, networks=None):
    return {"services": services, "networks": networks if networks is not None else control_networks()}


def intended_core():
    return stack(
        {
            "workspace": service(networks={"default": {}}, image=DEFAULT_RELEASE_IMAGES["workspace"]),
            OCU_SERVICE: service(
                networks={"default": {}},
                environment={
                    "OCU_SANDBOX_DNS": DEFAULT_DNS,
                    "DOCKER_IMAGE": DEFAULT_RELEASE_IMAGES["workspace"],
                },
                image=DEFAULT_RELEASE_IMAGES["computer-use-server"],
            ),
            "cleanup": service(networks={"default": {}}),
            "retention-guard": service(
                networks={"default": {}},
                image=DEFAULT_RELEASE_IMAGES["retention-guard"],
            ),
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
            WEBUI_SERVICE: service(
                networks={"default": {}},
                expose=["8080"],
                image=DEFAULT_RELEASE_IMAGES["open-webui"],
            ),
            "postgres": service(
                networks={"default": {}},
                image=DEFAULT_RELEASE_IMAGES["postgres"],
            ),
            "open-webui-init": service(
                networks={"default": {}},
                image=DEFAULT_RELEASE_IMAGES["open-webui"],
            ),
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
                image=DEFAULT_RELEASE_IMAGES["proxy"],
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


def smoke_env(state_dir: Path, extra=None):
    env = fake_env(state_dir)
    env["OCU_SMOKE_CHAT_ID"] = "smoke-chat"
    env["OCU_SMOKE_SANDBOX_ID"] = "sandbox-smoke"
    env["OCU_SMOKE_EXCLUSIVE"] = "1"
    env["OCU_SMOKE_OWNER_TOKEN"] = "synthetic-owner-token"
    env["OCU_SMOKE_FORMER_URL"] = "127.0.0.1:18081"
    env["OCU_SMOKE_EGRESS_URL"] = "http://8.8.8.8:80/"
    env["OCU_SMOKE_HOST_LAN_IPV4"] = "127.0.0.1"
    if extra:
        env.update(extra)
    return env


def write_ps(state_dir: Path, stack: str, rows) -> Path:
    directory = state_dir / "ps"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stack}.jsonl"
    if isinstance(rows, str):
        path.write_text(rows if rows.endswith("\n") else rows + "\n", encoding="utf-8")
        return path
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def write_exec(state_dir: Path, rows) -> Path:
    path = state_dir / "exec.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def write_curl(state_dir: Path, rows) -> Path:
    path = state_dir / "curl.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def write_sandbox_terminal(state_dir: Path, payload) -> Path:
    path = state_dir / "sandbox-terminal.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


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
    config_env=None,
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
    if config_env is not None:
        body["Config"] = {"Env": list(config_env), "Labels": dict(labels or {})}
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
        cwd=str(env.get("OCU_TEST_ROOT", ROOT)),
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


def empty_build(role: str) -> dict:
    return {
        "dockerfile": f"{role}.Dockerfile",
        "dockerfile_sha256": hashlib.sha256(role.encode("utf-8")).hexdigest(),
        "context": ".",
        "arguments": {},
        "argument_defaults": {},
        "argument_overrides": {},
        "input_manifest_sha256": hashlib.sha256(f"input-{role}".encode("utf-8")).hexdigest(),
        "materials": [{"name": role, "requested": "synthetic", "kind": "test"}],
    }


def synthetic_inventory(*, ocu_sha: str, webui_sha: str, images=None, bundle_sha=None) -> dict:
    records = {}
    for role in ROLE_ORDER:
        source = (images or {}).get(role, {})
        reference = source.get("reference", DEFAULT_RELEASE_IMAGES[role])
        digest = source.get("configuration_digest", default_config_id(role))
        archive = source.get(
            "archive",
            {"path": f"images/{role}.tar", "sha256": hashlib.sha256(role.encode("utf-8")).hexdigest()},
        )
        records[role] = {
            "reference": reference,
            "configuration_digest": digest,
            "archive": archive,
            "build": source.get("build", empty_build(role)),
        }
        if "registry_digests" in source:
            records[role]["registry_digests"] = source["registry_digests"]
    return {
        "format_version": FORMAT_VERSION,
        "platform": PLATFORM,
        "ocu_source_sha": ocu_sha,
        "webui_source_sha": webui_sha,
        "source_bundle": {
            "path": "source.bundle",
            "sha256": bundle_sha or hashlib.sha256(b"bundle").hexdigest(),
        },
        "images": records,
    }



def write_inventory(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path


def config_payload(digest: str, os_name="linux", architecture="amd64") -> bytes:
    return json.dumps(
        {
            "os": os_name,
            "architecture": architecture,
            "rootfs": {"diff_ids": [digest]},
            "config": {"Env": [f"OCU_FAKE={digest}"]},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def image_id_for_config(config: bytes) -> str:
    return "sha256:" + hashlib.sha256(config).hexdigest()


def default_config_id(role: str) -> str:
    return image_id_for_config(config_payload(DEFAULT_RELEASE_DIGESTS[role]))


def write_image_archive(path: Path, tags: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = []
    repositories = {}
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        for tag, config in tags.items():
            digest = hashlib.sha256(config).hexdigest()
            name = digest + ".json"
            info = tarfile.TarInfo(name=name)
            info.size = len(config)
            archive.addfile(info, io.BytesIO(config))
            entries.append({"Config": name, "RepoTags": [tag], "Layers": []})
            repo, _, label = tag.partition(":")
            repositories.setdefault(repo, {})[label or "latest"] = digest
        manifest = json.dumps(entries).encode("utf-8")
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest)
        archive.addfile(info, io.BytesIO(manifest))
        repos = json.dumps(repositories).encode("utf-8")
        info = tarfile.TarInfo(name="repositories")
        info.size = len(repos)
        archive.addfile(info, io.BytesIO(repos))
    path.write_bytes(payload.getvalue())
    return path



def seed_images(state_dir: Path, mapping: dict[str, dict] | None = None) -> dict:
    payload = {}
    if mapping is None:
        mapping = {}
        for role in ROLE_ORDER:

            config = config_payload(DEFAULT_RELEASE_DIGESTS[role])
            mapping[DEFAULT_RELEASE_IMAGES[role]] = {
                "Id": image_id_for_config(config),
                "Os": "linux",
                "Architecture": "amd64",
                "ConfigBytes": config.decode("utf-8"),
            }
    for name, record in mapping.items():
        payload[name] = record
    (state_dir / "images.json").write_text(json.dumps(payload), encoding="utf-8")
    return payload



def git_init_commit(root: Path, message="synthetic") -> str:
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "release@example.test"], cwd=str(root), check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Release Test"], cwd=str(root), check=True, capture_output=True, text=True)
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", message],
        cwd=str(root),
        check=True,
        capture_output=True,
        text=True,
    )
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), text=True).strip()


def copy_tracked(relative: str, dest_root: Path) -> None:
    source = ROOT / relative
    target = dest_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True)
    else:
        shutil.copy2(source, target)


def committed_up_fixture(dest_root: Path) -> str:
    for relative in UP_FIXTURE_PATHS:
        copy_tracked(relative, dest_root)
    (dest_root / "README").write_text("synthetic committed fixture\n", encoding="utf-8")
    return git_init_commit(dest_root, "committed deploy fixture")


def write_release_for_sha(dest: Path, ocu_sha: str, webui_sha: str, *, images=None) -> Path:
    payload = synthetic_inventory(ocu_sha=ocu_sha, webui_sha=webui_sha, images=images)
    return write_inventory(dest, payload)


def seed_release_env(env: dict, state_dir: Path, source_root: Path, inventory: Path) -> dict:
    merged = dict(env)
    merged["OCU_RELEASE_MANIFEST"] = str(inventory)
    merged["OCU_TEST_ROOT"] = str(source_root)
    if "SOURCE_SHA" not in merged:
        merged["SOURCE_SHA"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(source_root), text=True).strip()
    merged.setdefault("WEBUI_SOURCE_SHA", WEBUI_SYNTHETIC_SHA)
    for role, name in RUNTIME_IMAGE_VARS.items():
        merged.setdefault(name, DEFAULT_RELEASE_IMAGES[role])
    if not (state_dir / "images.json").exists():
        seed_images(state_dir)
    return merged


def prepare_up_context(state_dir: Path, extra=None):
    source = state_dir / "committed-source"
    if not (source / ".git").exists():
        source.mkdir(parents=True, exist_ok=True)
        sha = committed_up_fixture(source)
        inventory = write_release_for_sha(state_dir / "release.json", sha, WEBUI_SYNTHETIC_SHA)
        if not (state_dir / "images.json").exists():
            seed_images(state_dir)
    else:
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(source), text=True).strip()
        inventory = state_dir / "release.json"
        if not inventory.exists():
            inventory = write_release_for_sha(inventory, sha, WEBUI_SYNTHETIC_SHA)
    env = dict(extra) if extra is not None else fake_env(state_dir)
    env = seed_release_env(env, state_dir, source, inventory)
    return env, source / "deploy" / "up.sh", source

