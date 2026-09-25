# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Parsed overlay and packaging assertions without emulating Compose merge."""

from __future__ import annotations

from pathlib import Path
import unittest

try:
    import yaml
    from yaml.nodes import MappingNode, SequenceNode
except ImportError as exc:  # pragma: no cover - exercised by missing-dependency CI
    raise ImportError("PyYAML is required for parsed overlay checks") from exc

from support import (
    CORE_OVERRIDE,
    PROXY_COMPOSE,
    PROXY_DOCKERFILE,
    PROXY_DOCKERIGNORE,
    ROOT,
    WEBUI_OVERRIDE,
)


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


class OverlayStructureTests(unittest.TestCase):
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
        self.assertTrue(interpolated(webui_env["OCU_INTERNAL_TOKEN"], "OCU_INTERNAL_TOKEN"))
        self.assertEqual(webui_env["ENABLE_OCU_WORKSPACE"], "true")
        self.assertEqual(webui_env["OCU_INTERNAL_URL"], "http://computer-use-server:8081")
        self.assertEqual(webui_env["ORCHESTRATOR_URL"], "http://computer-use-server:8081")
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
        self.assertEqual(
            core["services"]["retention-guard"]["environment"]["CONTAINER_MAX_AGE_HOURS"],
            "${CONTAINER_MAX_AGE_HOURS-168}",
        )
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


if __name__ == "__main__":
    unittest.main()
