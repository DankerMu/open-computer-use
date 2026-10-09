# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Native Compose contracts plus parsed overlay and packaging guards."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

try:
    import yaml
    from yaml.nodes import MappingNode, SequenceNode
except ImportError as exc:  # pragma: no cover - exercised by missing-dependency CI
    raise ImportError("PyYAML is required for parsed overlay checks") from exc

from interpolation import UnsupportedInterpolation, interpolate_value
from support import (
    CORE_OVERRIDE,
    DEFAULT_RELEASE_IMAGES,
    PROXY_COMPOSE,
    PROXY_DOCKERFILE,
    PROXY_DOCKERIGNORE,
    ROOT,
    RUNTIME_IMAGE_VARS,
    WEBUI_OVERRIDE,
)

SERVER_DIR = ROOT / "computer-use-server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))
from office import config as office_config

OFFICE_INPUTS = {
    "OCU_OFFICE_DOCSERVER_URL": "http://documentserver",
    "OCU_OFFICE_DOCSERVER_ORIGIN": "http://workbench.test:8083",
    "OCU_OFFICE_SELF_URL": "http://computer-use-server:8081",
    "OCU_OFFICE_JWT_SECRET": "native-office-jwt-canary",
}


REQUIRED_CORE_NAMES = (
    "OCU_INTERNAL_TOKEN",
    "OCU_WEBUI_ORIGIN",
    "OCU_WEBUI_AUTH_URL",
    "PUBLIC_BASE_URL",
    "OCU_SANDBOX_NETWORK",
    "OCU_SANDBOX_SUBNET",
)
REQUIRED_PROXY_INTERPOLATED = (
    "OCU_INTERNAL_TOKEN",
    "OCU_WEBUI_ORIGIN",
)
ADOPTED = (
    ROOT / "deploy" / "production-like-test" / "retention" / "Dockerfile",
    ROOT / "deploy" / "production-like-test" / "retention" / "stop-overage.sh",
    ROOT / "deploy" / "production-like-test" / "init" / "run-init.sh",
    ROOT / "openwebui" / "init.sh",
    ROOT / "openwebui" / "tools" / "computer_use_tools.py",
    ROOT / "openwebui" / "functions" / "computer_link_filter.py",
)
PUBLIC_COPY_SOURCES = (
    "render.py",
    "nginx.conf.in",
    "routes.json",
    "entrypoint.sh",
)


class ComposeLoader(yaml.SafeLoader):
    pass


def override(loader, node):
    if isinstance(node, SequenceNode):
        value = loader.construct_sequence(node)
    elif isinstance(node, MappingNode):
        value = loader.construct_mapping(node)
    else:
        raise ValueError("unexpected !override value")
    return {"__override__": True, "value": value}


ComposeLoader.add_constructor("!override", override)


def parsed(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=ComposeLoader)


def override_value(node):
    if not isinstance(node, dict) or node.get("__override__") is not True:
        raise AssertionError("expected Compose !override node")
    return node["value"]


def interpolated(value: str, name: str) -> bool:
    return isinstance(value, str) and value.startswith(f"${{{name}:?")


def interpolated_present(value: str, name: str) -> bool:
    return isinstance(value, str) and value.startswith(f"${{{name}?")


def resolved_stacks(directory: Path, flag: str, source: Path | None = None) -> dict:
    source = source or ROOT
    directory.mkdir(parents=True, exist_ok=True)
    empty = directory / "empty.env"
    empty.write_text("", encoding="utf-8")
    environment = {
        "PATH": os.environ["PATH"], "HOME": str(directory),
        "DOCKER_CONFIG": str(directory), "COMPOSE_DISABLE_ENV_FILE": "1",
        "DOCKER_HOST": "unix://" + str(directory / "no-daemon.sock"),
        "OCU_PRIVATE_NETWORK": "ocu-test-private", "OCU_PRIVATE_SUBNET": "172.30.0.0/24",
        "OCU_PRIVATE_GATEWAY": "172.30.0.1", "OCU_SANDBOX_NETWORK": "ocu-sandbox",
        "OCU_SANDBOX_SUBNET": "172.31.0.0/24", "OCU_SANDBOX_GATEWAY": "172.31.0.1",
        "OCU_SANDBOX_DNS": "8.8.8.8", "OCU_PROXY_PORT": "8082",
        "OCU_OFFICE_PROXY_PORT": "8083",
        "OCU_WEBUI_ORIGIN": "http://workbench.test:8082",
        "OCU_WEBUI_AUTH_URL": "http://open-webui:8080/api/v1/ocu/auth",
        "PUBLIC_BASE_URL": "http://workbench.test:8082/ocu",
        "OCU_CHAT_DATA_DIR": str(directory / "chat"),
        "OCU_SKILLS_CACHE_DIR": str(directory / "skills"),
        "OCU_RELEASE_FONTS_DIR": str(directory.parent / "release-fonts"),
        "OCU_OFFICE_FONTS_DIR": str(directory.parent / "operator-fonts"),
        "CONTAINER_MEM_LIMIT": "2g", "CONTAINER_CPU_LIMIT": "1.0",
        "CONTAINER_IDLE_TIMEOUT": "604800", "COMMAND_TIMEOUT": "120",
        "SUB_AGENT_TIMEOUT": "3600", "RETENTION_CHECK_INTERVAL_SECONDS": "3600",
        "ADMIN_EMAIL": "admin@fixture.test", "DMXAPI_BASE_URL": "http://provider.fixture/v1",
        "ENABLE_OCU_OFFICE_EDIT": flag, **OFFICE_INPUTS,
    }
    for name in ("MCP_API_KEY", "OCU_INTERNAL_TOKEN", "WEBUI_SECRET_KEY",
                 "POSTGRES_PASSWORD", "ADMIN_PASSWORD", "DMXAPI_API_KEY"):
        environment[name] = "native-fixture-" + name.lower()
    environment.update({name: DEFAULT_RELEASE_IMAGES[role]
                        for role, name in RUNTIME_IMAGE_VARS.items()})
    standalone = shutil.which("docker-compose")
    command = [standalone] if standalone else ["docker", "compose"]
    overlay = source / "deploy/production-like-test"
    documents = {}
    for name, project, files in (
        ("core", source, (source / "docker-compose.yml", overlay / "compose.core.override.yml")),
        ("webui", source, (source / "docker-compose.webui.yml", overlay / "compose.webui.override.yml")),
        ("proxy", overlay, (overlay / "compose.proxy.yml",)),
    ):
        argv = [*command, "-p", "ocu-test", "--project-directory", str(project),
                "--env-file", str(empty)]
        for path in files:
            argv.extend(("-f", str(path)))
        result = subprocess.run(
            [*argv, "config", "--format", "json", "--no-env-resolution"],
            cwd=source, env=environment, capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            raise AssertionError(f"native Compose v2 resolution failed: {result.stderr}")
        document = json.loads(result.stdout)
        if any(service.get("env_file") for service in document["services"].values()):
            raise AssertionError("native resolver must not omit service env_file inputs")
        documents[name + ".json"] = document
    return documents


class OverlayStructureTests(unittest.TestCase):
    def test_native_documentserver_topology_and_consumer_contract(self):
        states = []
        with tempfile.TemporaryDirectory(prefix="ocu-native-compose-") as raw:
            for flag in ("false", "true"):
                documents = resolved_stacks(Path(raw) / flag, flag)
                core, webui, proxy = (documents[name + ".json"] for name in ("core", "webui", "proxy"))
                self.assertIn("documentserver", core["services"])
                service = core["services"]["documentserver"]
                self.assertEqual(sum("documentserver" in doc["services"]
                                     for doc in documents.values()), 1)
                self.assertEqual(service["image"], DEFAULT_RELEASE_IMAGES["documentserver"])
                self.assertNotIn("build", service)
                self.assertFalse(service.get("profiles"))
                self.assertFalse(service.get("ports"))
                self.assertFalse(service.get("network_mode"))
                self.assertEqual(set(service["networks"]), {"default"})
                self.assertEqual(core["networks"]["default"]["name"], "ocu-test-private")
                expected_mounts = {
                    "/var/www/onlyoffice/Data": "documentserver-data",
                    "/var/lib/onlyoffice": "documentserver-cache",
                    "/var/log/onlyoffice": "documentserver-logs",
                }
                mounts = [mount for mount in service["volumes"] if mount["type"] == "volume"]
                self.assertEqual({mount["target"]: mount["source"] for mount in mounts},
                                 expected_mounts)
                self.assertEqual(len(mounts), len(expected_mounts))
                for mount in mounts:
                    self.assertEqual(mount["type"], "volume")
                    self.assertIn(mount["source"], core["volumes"])
                binds = [mount for mount in service["volumes"] if mount["type"] == "bind"]
                self.assertEqual({mount["target"]: mount["source"] for mount in binds}, {
                    "/usr/share/fonts/truetype/ocu-release": str(Path(raw) / "release-fonts"),
                    "/usr/share/fonts/truetype/ocu-operator": str(Path(raw) / "operator-fonts"),
                })
                self.assertEqual(len(service["volumes"]), len(mounts) + 2)
                for mount in binds:
                    self.assertIs(mount.get("read_only", False), True)
                    # Compose omits false bind options from normalized JSON.
                    self.assertIs(mount["bind"].get("create_host_path", False), False)
                names = (
                    office_config.OCU_OFFICE_DOCSERVER_URL,
                    office_config.OCU_OFFICE_DOCSERVER_ORIGIN,
                    office_config.OCU_OFFICE_SELF_URL,
                    office_config.OCU_OFFICE_JWT_SECRET,
                )
                environment = core["services"]["computer-use-server"]["environment"]
                for name in names:
                    self.assertTrue(environment.get(name), name)
                    self.assertEqual(environment[name], OFFICE_INPUTS[name])
                jwt = service.get("environment", {})
                self.assertEqual(jwt.get("JWT_ENABLED"), "true")
                self.assertEqual(jwt.get("JWT_SECRET"),
                                 environment[office_config.OCU_OFFICE_JWT_SECRET])
                self.assertEqual(webui["services"]["open-webui"]["environment"]["ENABLE_OCU_OFFICE_EDIT"], flag)
                gateway = proxy["services"]["proxy"]
                self.assertEqual({(str(port["target"]), str(port["published"]))
                                  for port in gateway["ports"]},
                                 {("8082", "8082"), ("8083", "8083")})
                self.assertEqual(len(gateway["ports"]), 2)
                self.assertEqual(gateway["environment"]["OCU_OFFICE_PROXY_LISTEN"], "0.0.0.0:8083")
                self.assertEqual(gateway["environment"]["OCU_OFFICE_PROXY_UPSTREAM"],
                                 environment[office_config.OCU_OFFICE_DOCSERVER_URL])
                states.append((service, {name: core["volumes"][name] for name in expected_mounts.values()}))
        self.assertEqual(states[0], states[1])

    def test_parsed_overrides_and_local_build_mount_dependencies(self):
        core = parsed(CORE_OVERRIDE)
        webui = parsed(WEBUI_OVERRIDE)
        proxy = parsed(PROXY_COMPOSE)
        self.assertEqual(override_value(core["services"]["computer-use-server"]["ports"]), [])
        self.assertEqual(override_value(webui["services"]["open-webui"]["ports"]), [])
        core_env = override_value(core["services"]["computer-use-server"]["environment"])
        webui_env = override_value(webui["services"]["open-webui"]["environment"])
        for name in REQUIRED_CORE_NAMES:
            self.assertTrue(interpolated(core_env[name], name), name)
        self.assertTrue(interpolated(core_env["SANDBOX_HOST_BIND_IP"], "OCU_SANDBOX_GATEWAY"))
        self.assertEqual(core_env["OCU_PUBLIC_PREFIX"], "/ocu")
        self.assertEqual(core_env["OCU_SANDBOX_NO_AUTOSTART"], "1")
        self.assertTrue(interpolated_present(core_env["OCU_SANDBOX_DNS"], "OCU_SANDBOX_DNS"))
        self.assertEqual(interpolate_value(core_env["OCU_SANDBOX_DNS"], {"OCU_SANDBOX_DNS": ""}), "")
        with self.assertRaises(UnsupportedInterpolation):
            interpolate_value(core_env["OCU_SANDBOX_DNS"], {})
        self.assertTrue(interpolated(webui_env["OCU_INTERNAL_TOKEN"], "OCU_INTERNAL_TOKEN"))
        self.assertEqual(webui_env["ENABLE_OCU_WORKSPACE"], "true")
        self.assertEqual(webui_env["OCU_INTERNAL_URL"], "http://computer-use-server:8081")
        self.assertEqual(webui_env["ORCHESTRATOR_URL"], "http://computer-use-server:8081")
        self.assertEqual(webui_env["OFFLINE_MODE"], "true")
        self.assertEqual(webui_env["ENABLE_VERSION_UPDATE_CHECK"], "false")
        self.assertEqual(webui_env["WHISPER_MODEL_AUTO_UPDATE"], "false")
        self.assertEqual(webui_env["RAG_EMBEDDING_MODEL_AUTO_UPDATE"], "false")
        self.assertEqual(webui_env["RAG_RERANKING_MODEL_AUTO_UPDATE"], "false")
        internal_provider = {"DMXAPI_BASE_URL": "http://lan-model:8000/v1"}
        for setting in ("OPENAI_API_BASE_URL", "RAG_OPENAI_API_BASE_URL"):
            self.assertEqual(interpolate_value(webui_env[setting], internal_provider), "http://lan-model:8000/v1")

        self.assertTrue(interpolated(core["networks"]["default"]["name"], "OCU_PRIVATE_NETWORK"))
        self.assertTrue(interpolated(core["networks"]["default"]["ipam"]["config"][0]["subnet"], "OCU_PRIVATE_SUBNET"))
        self.assertTrue(interpolated(core["networks"]["default"]["ipam"]["config"][0]["gateway"], "OCU_PRIVATE_GATEWAY"))
        self.assertTrue(interpolated(webui["networks"]["default"]["name"], "OCU_PRIVATE_NETWORK"))
        self.assertTrue(interpolated(webui["networks"]["default"]["ipam"]["config"][0]["subnet"], "OCU_PRIVATE_SUBNET"))
        self.assertTrue(interpolated(webui["networks"]["default"]["ipam"]["config"][0]["gateway"], "OCU_PRIVATE_GATEWAY"))
        self.assertTrue(interpolated(proxy["networks"]["default"]["name"], "OCU_PRIVATE_NETWORK"))
        for name in REQUIRED_PROXY_INTERPOLATED:
            self.assertTrue(interpolated(proxy["services"]["proxy"]["environment"][name], name), name)
        self.assertTrue(interpolated(proxy["services"]["proxy"]["image"], "OCU_PROXY_IMAGE"))
        self.assertEqual(proxy["services"]["proxy"]["environment"]["OCU_WEBUI_UPSTREAM"], "http://open-webui:8080")
        self.assertEqual(proxy["services"]["proxy"]["environment"]["OCU_PROXY_UPSTREAM"], "http://computer-use-server:8081")
        self.assertEqual(proxy["services"]["proxy"]["environment"]["OCU_PROXY_LISTEN"], "0.0.0.0:8082")
        age = core["services"]["retention-guard"]["environment"]["CONTAINER_MAX_AGE_HOURS"]
        self.assertEqual(interpolate_value(age, {}), "168")
        self.assertEqual(interpolate_value(age, {"CONTAINER_MAX_AGE_HOURS": ""}), "")
        self.assertEqual(interpolate_value(age, {"CONTAINER_MAX_AGE_HOURS": "24"}), "24")
        colon_default = "${CONTAINER_MAX_AGE_HOURS:-168}"
        self.assertEqual(interpolate_value(colon_default, {"CONTAINER_MAX_AGE_HOURS": ""}), "168")
        self.assertNotEqual(interpolate_value(age, {"CONTAINER_MAX_AGE_HOURS": ""}), "168")
        with self.assertRaises(UnsupportedInterpolation):
            interpolate_value("${CONTAINER_MAX_AGE_HOURS/foo/bar}", {})
        core_context = ROOT / core["services"]["retention-guard"]["build"]["context"]
        self.assertTrue((core_context / "Dockerfile").is_file())
        self.assertTrue((core_context / "stop-overage.sh").is_file())
        mounts = webui["services"]["open-webui-init"]["volumes"]
        self.assertTrue(any("init/run-init.sh:/bootstrap/run-init.sh:ro" in value for value in mounts))
        self.assertTrue(any(value.startswith("./openwebui:") for value in mounts))
        proxy_context = (PROXY_COMPOSE.parent / proxy["services"]["proxy"]["build"]["context"]).resolve()
        self.assertEqual(proxy_context, ROOT / "deploy" / "proxy")
        self.assertTrue((proxy_context / proxy["services"]["proxy"]["build"]["dockerfile"]).is_file())
        self.assertEqual(list(proxy["services"]), ["proxy"])

    def test_adopted_local_dependencies_exist(self):
        for path in ADOPTED:
            self.assertTrue(path.is_file(), path)
        core = parsed(CORE_OVERRIDE)
        webui = parsed(WEBUI_OVERRIDE)
        self.assertEqual(
            core["services"]["retention-guard"]["build"]["context"],
            "./deploy/production-like-test/retention",
        )
        mounts = webui["services"]["open-webui-init"]["volumes"]
        self.assertTrue(any("./deploy/production-like-test/init/run-init.sh:/bootstrap/run-init.sh:ro" in value for value in mounts))

    def test_proxy_packaging_copies_only_public_sources(self):
        dockerfile = PROXY_DOCKERFILE.read_text(encoding="utf-8")
        dockerignore = PROXY_DOCKERIGNORE.read_text(encoding="utf-8")
        copies = [
            line.split()[1]
            for line in dockerfile.splitlines()
            if line.startswith("COPY ")
        ]
        self.assertEqual(copies, list(PUBLIC_COPY_SOURCES))
        self.assertTrue(any(line.strip() == "*" for line in dockerignore.splitlines()), dockerignore)
        for source in PUBLIC_COPY_SOURCES:
            self.assertIn(f"!{source}", dockerignore)
        self.assertIn("nginx.conf", dockerignore)
        self.assertIn("runtime/", dockerignore)
        self.assertNotIn("nginx.conf", copies)

    def test_obsolete_private_binding_patch_is_absent(self):
        patch = ROOT / "deploy" / "production-like-test" / "patches" / "private-sandbox-port-bindings.patch"
        self.assertFalse(patch.exists())
        autostart = ROOT / "deploy" / "production-like-test" / "patches" / "disable-cli-autostart.patch"
        self.assertFalse(autostart.exists())
        claim = (ROOT / "deploy" / "production-like-test" / "scripts" / "write-deployed-version.sh").read_text(encoding="utf-8")
        self.assertNotIn("private-sandbox-port-bindings.patch", claim)

    def test_single_pass_dollar_interpolation(self):
        env = {"TOKEN": "HOST_VALUE"}
        self.assertEqual(interpolate_value("$${TOKEN}", env), "${TOKEN}")
        self.assertEqual(interpolate_value("$$TOKEN", env), "$TOKEN")
        self.assertEqual(interpolate_value("${TOKEN}", env), "HOST_VALUE")
        self.assertEqual(interpolate_value("$TOKEN", env), "HOST_VALUE")


if __name__ == "__main__":
    unittest.main()
