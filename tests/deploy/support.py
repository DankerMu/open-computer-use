# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Independent literals and paths for deployment preflight tests."""

from __future__ import annotations
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
import threading
import zipfile





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
DOCUMENTSERVER_SERVICE = "documentserver"
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
    "documentserver",
)
RUNTIME_IMAGE_VARS = {
    "workspace": "DOCKER_IMAGE",
    "computer-use-server": "COMPUTER_USE_SERVER_IMAGE",
    "retention-guard": "RETENTION_GUARD_IMAGE",
    "proxy": "OCU_PROXY_IMAGE",
    "open-webui": "OPENWEBUI_IMAGE",
    "postgres": "POSTGRES_IMAGE",
    "documentserver": "DOCUMENTSERVER_IMAGE",
}
DEFAULT_RELEASE_IMAGES = {
    "workspace": "open-computer-use:synthetic",
    "computer-use-server": "ocu-computer-use-server:synthetic",
    "retention-guard": "ocu-retention-guard:synthetic",
    "proxy": "ocu-test-proxy:synthetic",
    "open-webui": "ocu-open-webui:synthetic",
    "postgres": "postgres:17-alpine",
    "documentserver": "ocu-documentserver:synthetic",
}
DEFAULT_RELEASE_DIGESTS = {
    role: "sha256:" + hashlib.sha256(f"synthetic-{role}".encode("utf-8")).hexdigest()
    for role in ROLE_ORDER
}
FORMAT_VERSION = 2
PLATFORM = "linux/amd64"
WEBUI_SYNTHETIC_SHA = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
SOURCE_CONSUMER_CONTRACT = 1
HISTORICAL_INCOMPATIBLE_SOURCE = "f851621f7d425487c99b0f548dcb4dab06a9648e"
OCI_LAYOUT_VERSION = "1.0.0"
ANNOTATION_IMAGE_NAME = "io.containerd.image.name"
ANNOTATION_REF_NAME = "org.opencontainers.image.ref.name"
DOCUMENTSERVER_UPSTREAM = (
    "onlyoffice/documentserver@sha256:"
    "e3da62a847b9a5d51a11f73cfea1d9c13c3be3809614490d4edddcf01dcf919b"
)


UP_FIXTURE_PATHS = (
    "deploy/up.sh",
    "deploy/settings.py",
    "deploy/release.py",
    "deploy/fonts/prepare_fonts.py",
    "deploy/fonts/fonts.json",
    "deploy/recovery.py",
    "deploy/recovery_fs.py",
    "deploy/recovery_resources.py",
    "deploy/recovery_db.py",
    "deploy/recovery_helper.py",
    "deploy/BACKUP-RESTORE.md",
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
    "deploy/production-like-test/scripts/bootstrap-test.sh",
    "deploy/production-like-test/scripts/write-deployed-version.sh",
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
                    "OCU_OFFICE_JWT_SECRET": "synthetic-office-jwt-secret",
                    "DOCKER_IMAGE": DEFAULT_RELEASE_IMAGES["workspace"],
                },
                image=DEFAULT_RELEASE_IMAGES["computer-use-server"],
            ),
            "cleanup": service(networks={"default": {}}),
            "retention-guard": service(
                networks={"default": {}},
                image=DEFAULT_RELEASE_IMAGES["retention-guard"],
            ),
            DOCUMENTSERVER_SERVICE: service(
                networks={"default": {}},
                environment={"JWT_ENABLED": "true", "JWT_SECRET": "synthetic-office-jwt-secret"},
                image=DEFAULT_RELEASE_IMAGES["documentserver"],
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

def intended_docs_for_images(images: dict) -> dict:
    docs = intended_docs()
    mapping = {
        "workspace": images["workspace"]["reference"],
        "computer-use-server": images["computer-use-server"]["reference"],
        "retention-guard": images["retention-guard"]["reference"],
        "open-webui": images["open-webui"]["reference"],
        "postgres": images["postgres"]["reference"],
        "proxy": images["proxy"]["reference"],
        "documentserver": images["documentserver"]["reference"],
    }
    core = docs["core.json"]
    core["services"]["workspace"]["image"] = mapping["workspace"]
    core["services"][OCU_SERVICE]["image"] = mapping["computer-use-server"]
    core["services"][OCU_SERVICE]["environment"]["DOCKER_IMAGE"] = mapping["workspace"]
    core["services"]["retention-guard"]["image"] = mapping["retention-guard"]
    core["services"][DOCUMENTSERVER_SERVICE]["image"] = mapping["documentserver"]
    webui = docs["webui.json"]
    webui["services"][WEBUI_SERVICE]["image"] = mapping["open-webui"]
    webui["services"]["postgres"]["image"] = mapping["postgres"]
    webui["services"]["open-webui-init"]["image"] = mapping["open-webui"]
    docs["proxy.json"]["services"][PROXY_SERVICE]["image"] = mapping["proxy"]
    return docs


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
    env.update({
        "OCU_OFFICE_JWT_SECRET": "synthetic-office-jwt-secret",
        "OCU_OFFICE_DOCSERVER_URL": "http://documentserver",
        "OCU_OFFICE_DOCSERVER_ORIGIN": "http://localhost:8083",
        "OCU_OFFICE_SELF_URL": "http://computer-use-server:8081",
        "OCU_OFFICE_PROXY_PORT": "8083",
        "OCU_OFFICE_FONTS_DIR": str(state_dir / "office-fonts"),
        "ENABLE_OCU_OFFICE_EDIT": "false",
    })
    env["OCU_SANDBOX_EGRESS_LOCK"] = str(state_dir / "ocu-sandbox-egress.lock")
    env["DOCKER_HOST"] = "unix://" + str(state_dir / "docker.sock")
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


FONT_FILES = {
    "fixture-cjk.otf": b"OTTO synthetic CJK font bytes\n",
    "fixture-LICENSE.txt": b"SIL Open Font License 1.1 fixture\n",
}


def font_archive_bytes(*, year=2020, linked=False) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in sorted(FONT_FILES.items()):
            member = zipfile.ZipInfo("upstream/" + name, (year, 1, 1, 0, 0, 0))
            kind = stat.S_IFLNK if linked and name.endswith(".otf") else stat.S_IFREG
            member.external_attr = (kind | 0o644) << 16
            archive.writestr(member, content)
    return buffer.getvalue()


def font_pin(url="http://127.0.0.1:1/fonts.zip", *, archive_bytes=None) -> dict:
    data = font_archive_bytes() if archive_bytes is None else archive_bytes
    return {"archives": [{
        "url": url,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "files": [
            {"member": "upstream/" + name, "name": name,
             "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
            for name, content in sorted(FONT_FILES.items())
        ],
    }]}


def write_font_pin(root: Path, url="http://127.0.0.1:1/fonts.zip") -> Path:
    path = root / "deploy/fonts/fonts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(font_pin(url)) + "\n", encoding="utf-8")
    return path


def font_bundle_bytes() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, content in sorted(FONT_FILES.items()):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(content))
    return buffer.getvalue()


def write_font_bundle(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    data = font_bundle_bytes()
    (root / "fonts.tar").write_bytes(data)
    return {"path": "fonts.tar", "sha256": hashlib.sha256(data).hexdigest()}


def write_installed_fonts(root: Path) -> None:
    directory = root / "fonts"
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in FONT_FILES.items():
        (directory / name).write_bytes(content)


@contextmanager
def serve_font_archive(data=None):
    content = font_archive_bytes() if data is None else data

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/fonts.zip":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/fonts.zip"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError("font fixture server did not stop")


def empty_build(role: str) -> dict:
    upstream = DOCUMENTSERVER_UPSTREAM if role == "documentserver" else None
    return {
        "dockerfile": "deploy/release.py" if upstream else f"{role}.Dockerfile",
        "dockerfile_sha256": hashlib.sha256(role.encode("utf-8")).hexdigest(),
        "context": ".",
        "arguments": {"DOCUMENTSERVER_IMAGE": upstream} if upstream else {},
        "argument_defaults": {"DOCUMENTSERVER_IMAGE": upstream} if upstream else {},
        "argument_overrides": {},
        "input_manifest_sha256": hashlib.sha256(f"input-{role}".encode("utf-8")).hexdigest(),
        "materials": [{
            "name": role,
            "requested": upstream or "synthetic",
            "kind": "upstream-image" if upstream else "test",
        }],
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
        "source_consumer_contract": SOURCE_CONSUMER_CONTRACT,
        "source_bundle": {
            "path": "source.bundle",
            "sha256": bundle_sha or hashlib.sha256(b"bundle").hexdigest(),
        },
        "font_bundle": {
            "path": "fonts.tar", "sha256": hashlib.sha256(font_bundle_bytes()).hexdigest(),
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


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_docker_ref(reference: str) -> str:
    text = str(reference or "")
    if not text or any(ch.isspace() for ch in text) or "@" in text:
        raise ValueError(f"invalid docker reference {reference!r}")
    name, sep, tag = text.rpartition(":")
    if not sep or "/" in tag:
        name, tag = text, "latest"
    if "/" not in name:
        name = "docker.io/library/" + name
    else:
        registry = name.split("/", 1)[0]
        if "." not in registry and ":" not in registry and registry != "localhost":
            name = "docker.io/" + name
    return f"{name}:{tag}"


def familiar_docker_ref(reference: str) -> str:
    normalized = normalize_docker_ref(reference)
    if normalized.startswith("docker.io/library/"):
        return normalized[len("docker.io/library/") :]
    if normalized.startswith("docker.io/"):
        return normalized[len("docker.io/") :]
    return normalized


def oci_ref_name(reference: str) -> str:
    normalized = normalize_docker_ref(reference)
    return normalized.rsplit(":", 1)[1]


def write_hybrid_image_archive(
    path: Path,
    *,
    reference: str,
    config: bytes,
    extra_oci_names=(),
    extra_index_descriptors=(),
    nested_index=False,
    attestation=None,
    layer: bytes | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    layer_bytes = b"ocu-hybrid-layer\n" if layer is None else layer
    config_digest = _sha256_hex(config)
    layer_digest = _sha256_hex(layer_bytes)
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": f"sha256:{config_digest}",
            "size": len(config),
        },
        "layers": [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar",
                "digest": f"sha256:{layer_digest}",
                "size": len(layer_bytes),
            }
        ],
    }
    manifest_bytes = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode("utf-8")
    manifest_digest = _sha256_hex(manifest_bytes)
    named = [
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": f"sha256:{manifest_digest}",
            "size": len(manifest_bytes),
            "platform": {"os": "linux", "architecture": "amd64"},
            "annotations": {
                ANNOTATION_IMAGE_NAME: normalize_docker_ref(reference),
                ANNOTATION_REF_NAME: oci_ref_name(reference),
            },
        }
    ]
    for extra in extra_oci_names:
        named.append(
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": f"sha256:{manifest_digest}",
                "size": len(manifest_bytes),
                "platform": {"os": "linux", "architecture": "amd64"},
                "annotations": {
                    ANNOTATION_IMAGE_NAME: normalize_docker_ref(extra),
                    ANNOTATION_REF_NAME: oci_ref_name(extra),
                },
            }
        )
    blobs: dict[str, bytes] = {
        config_digest: config,
        layer_digest: layer_bytes,
        manifest_digest: manifest_bytes,
    }
    index_manifests = list(named)
    if nested_index:
        child = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": named,
        }
        child_bytes = json.dumps(child, separators=(",", ":"), sort_keys=True).encode("utf-8")
        child_digest = _sha256_hex(child_bytes)
        blobs[child_digest] = child_bytes
        index_manifests = [
            {
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "digest": f"sha256:{child_digest}",
                "size": len(child_bytes),
            }
        ]
    if attestation is not None:
        subject = f"sha256:{manifest_digest}"
        attestation_manifest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": f"sha256:{config_digest}",
                "size": len(config),
            },
            "layers": [],
            "subject": {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": subject,
                "size": len(manifest_bytes),
            },
        }
        attestation_bytes = json.dumps(
            attestation_manifest, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        attestation_digest = _sha256_hex(attestation_bytes)
        blobs[attestation_digest] = attestation_bytes
        index_manifests.append(
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": f"sha256:{attestation_digest}",
                "size": len(attestation_bytes),
                "annotations": {
                    "vnd.docker.reference.type": "attestation-manifest",
                    "containerd.io/manifest.subject": subject,
                },
            }
        )
    index_manifests.extend(list(extra_index_descriptors))
    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": index_manifests,
    }
    index_bytes = json.dumps(index, separators=(",", ":"), sort_keys=True).encode("utf-8")
    layout_bytes = json.dumps(
        {"imageLayoutVersion": OCI_LAYOUT_VERSION}, separators=(",", ":")
    ).encode("utf-8")
    docker_manifest = json.dumps(
        [
            {
                "Config": f"blobs/sha256/{config_digest}",
                "RepoTags": [familiar_docker_ref(reference)],
                "Layers": [f"blobs/sha256/{layer_digest}"],
            }
        ]
    ).encode("utf-8")
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        def add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))

        add("oci-layout", layout_bytes)
        add("index.json", index_bytes)
        add("manifest.json", docker_manifest)
        for digest, data in blobs.items():
            add(f"blobs/sha256/{digest}", data)
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


def committed_up_fixture(dest_root: Path, *, lock_dir: Path | None = None) -> str:
    for relative in UP_FIXTURE_PATHS:
        copy_tracked(relative, dest_root)
    if lock_dir is not None:
        release_path = dest_root / "deploy" / "release.py"
        text = release_path.read_text(encoding="utf-8")
        lock_literal = 'IMAGE_STORE_LOCK_DIR = Path("/run/ocu-image-store")'
        if lock_literal not in text:
            raise RuntimeError("release fixture is missing IMAGE_STORE_LOCK_DIR")
        release_path.write_text(
            text.replace(
                lock_literal,
                f"IMAGE_STORE_LOCK_DIR = Path({str(lock_dir)!r})",
                1,
            ),
            encoding="utf-8",
        )
    write_font_pin(dest_root)
    (dest_root / "README").write_text("synthetic committed fixture\n", encoding="utf-8")
    return git_init_commit(dest_root, "committed deploy fixture")


def write_release_for_sha(dest: Path, ocu_sha: str, webui_sha: str, *, images=None) -> Path:
    payload = synthetic_inventory(ocu_sha=ocu_sha, webui_sha=webui_sha, images=images)
    write_installed_fonts(dest.parent)
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
    lock_dir = state_dir / "image-store-lock"
    lock_dir.mkdir(parents=True, exist_ok=True)
    if not (source / ".git").exists():
        (state_dir / "office-fonts").mkdir(exist_ok=True)
        source.mkdir(parents=True, exist_ok=True)
        sha = committed_up_fixture(source, lock_dir=lock_dir)
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

