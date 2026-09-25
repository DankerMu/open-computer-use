# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Static overlay and packaging assertions without emulating Compose merge."""

from __future__ import annotations

from pathlib import Path
import re
import unittest
try:
    import yaml
    from yaml.nodes import MappingNode, SequenceNode
except ImportError:
    yaml = None

from support import (
    CORE_OVERRIDE,
    PROXY_COMPOSE,
    PROXY_DOCKERFILE,
    PROXY_DOCKERIGNORE,
    PROXY_ENTRYPOINT,
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
REQUIRED_WEBUI_NAMES = (
    "OCU_INTERNAL_TOKEN",
)
REQUIRED_PROXY_INTERPOLATED = (
    "OCU_INTERNAL_TOKEN",
    "OCU_WEBUI_ORIGIN",
)
REQUIRED_PRIVATE_NET = (
    "OCU_PRIVATE_NETWORK",
    "OCU_PRIVATE_SUBNET",
    "OCU_PRIVATE_GATEWAY",
)
ADOPTED = (
    ROOT / "deploy" / "production-like-test" / "retention" / "Dockerfile",
    ROOT / "deploy" / "production-like-test" / "retention" / "stop-overage.sh",
    ROOT / "deploy" / "production-like-test" / "init" / "run-init.sh",
)
PUBLIC_COPIES = (
    "COPY render.py /opt/ocu-proxy/render.py",
    "COPY nginx.conf.in /opt/ocu-proxy/nginx.conf.in",
    "COPY routes.json /opt/ocu-proxy/routes.json",
    "COPY entrypoint.sh /opt/ocu-proxy/entrypoint.sh",
)


if yaml is not None:
    class ComposeLoader(yaml.SafeLoader):
        pass

    def override(loader, node):
        if isinstance(node, SequenceNode):
            return loader.construct_sequence(node)
        if isinstance(node, MappingNode):
            return loader.construct_mapping(node)
        raise ValueError("unexpected !override value")

    ComposeLoader.add_constructor("!override", override)


def parsed(path: Path) -> dict:
    if yaml is None:
        raise unittest.SkipTest("PyYAML is required for parsed overlay checks")
    return yaml.load(path.read_text(encoding="utf-8"), Loader=ComposeLoader)


class OverlayStructureTests(unittest.TestCase):
    def read(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def test_core_and_webui_remove_host_publications_with_override(self):
        core = self.read(CORE_OVERRIDE)
        webui = self.read(WEBUI_OVERRIDE)
        self.assertIn("ports: !override []", core)
        self.assertIn("ports: !override []", webui)
        self.assertNotIn("127.0.0.1:", core)
        self.assertNotIn("127.0.0.1:", webui)
        self.assertNotRegex(core, r"ports:\s*\n\s*-\s*\"")
        self.assertNotRegex(webui, r"ports:\s*\n\s*-\s*\"")

    def test_required_auth_and_topology_names_cannot_be_dropped(self):
        core = self.read(CORE_OVERRIDE)
        webui = self.read(WEBUI_OVERRIDE)
        proxy = self.read(PROXY_COMPOSE)
        for name in REQUIRED_CORE_NAMES:
            self.assertIn(f"{name}: ${{{name}:?", core)
        self.assertIn("SANDBOX_HOST_BIND_IP: ${OCU_SANDBOX_GATEWAY:?", core)
        for name in REQUIRED_WEBUI_NAMES:
            self.assertIn(f"{name}: ${{{name}:?", webui)
        for name in REQUIRED_PRIVATE_NET:
            self.assertIn(f"${{{name}:?", core)
            self.assertIn(f"${{{name}:?", webui)
        self.assertIn("${OCU_PRIVATE_NETWORK:?", proxy)
        for name in REQUIRED_PROXY_INTERPOLATED:
            self.assertIn(f"{name}: ${{{name}:?", proxy)
        self.assertIn("image: ${OCU_PROXY_IMAGE:?", proxy)
        self.assertIn("${OCU_PROXY_PORT:?", proxy)
        self.assertIn("OCU_WEBUI_UPSTREAM: http://open-webui:8080", proxy)
        self.assertIn("OCU_PROXY_UPSTREAM: http://computer-use-server:8081", proxy)
        self.assertIn("0.0.0.0:8082", proxy)

    def test_adopted_local_dependencies_exist(self):
        for path in ADOPTED:
            self.assertTrue(path.is_file(), path)
        self.assertIn("./deploy/production-like-test/retention", self.read(CORE_OVERRIDE))
        self.assertIn("./deploy/production-like-test/init/run-init.sh", self.read(WEBUI_OVERRIDE))

    @unittest.skipUnless(yaml is not None, "PyYAML is required for parsed overlay checks")
    def test_parsed_overrides_and_local_build_mount_dependencies(self):
        core = parsed(CORE_OVERRIDE)
        webui = parsed(WEBUI_OVERRIDE)
        proxy = parsed(PROXY_COMPOSE)
        self.assertEqual(core["services"]["computer-use-server"]["ports"], [])
        self.assertEqual(webui["services"]["open-webui"]["ports"], [])
        core_context = ROOT / core["services"]["retention-guard"]["build"]["context"]
        self.assertTrue((core_context / "Dockerfile").is_file())
        self.assertTrue((core_context / "stop-overage.sh").is_file())
        mounts = webui["services"]["open-webui-init"]["volumes"]
        self.assertTrue(any("init/run-init.sh:/bootstrap/run-init.sh:ro" in value for value in mounts))
        proxy_context = (PROXY_COMPOSE.parent / proxy["services"]["proxy"]["build"]["context"]).resolve()
        self.assertEqual(proxy_context, ROOT / "deploy" / "proxy")
        self.assertTrue((proxy_context / proxy["services"]["proxy"]["build"]["dockerfile"]).is_file())
        self.assertEqual(list(proxy["services"]), ["proxy"])


    def test_proxy_packaging_copies_only_public_sources(self):
        dockerfile = self.read(PROXY_DOCKERFILE)
        dockerignore = self.read(PROXY_DOCKERIGNORE)
        copies = re.findall(r"^COPY .+$", dockerfile, re.MULTILINE)
        self.assertEqual(copies, list(PUBLIC_COPIES))
        self.assertTrue(any(line.strip() == "*" for line in dockerignore.splitlines()), dockerignore)
        for source in ("render.py", "nginx.conf.in", "routes.json", "entrypoint.sh"):
            self.assertIn(f"!{source}", dockerignore)
        self.assertIn("nginx.conf", dockerignore)
        self.assertIn("runtime/", dockerignore)
        copied = [line.split()[1] for line in copies]
        self.assertNotIn("nginx.conf", copied)
        self.assertNotIn("/opt/homebrew", dockerfile)
        self.assertNotIn("/opt/homebrew", self.read(PROXY_ENTRYPOINT))

    def test_obsolete_private_binding_patch_is_absent(self):
        patch = ROOT / "deploy" / "production-like-test" / "patches" / "private-sandbox-port-bindings.patch"
        self.assertFalse(patch.exists())
        claim = self.read(ROOT / "deploy" / "production-like-test" / "scripts" / "write-deployed-version.sh")
        self.assertNotIn("private-sandbox-port-bindings.patch", claim)
        self.assertIn("disable-cli-autostart.patch", claim)

    def test_cli_autostart_patch_remains(self):
        patch = ROOT / "deploy" / "production-like-test" / "patches" / "disable-cli-autostart.patch"
        self.assertTrue(patch.is_file())

    def test_base_development_compose_is_untouched_by_this_slice(self):
        base = self.read(ROOT / "docker-compose.yml")
        webui = self.read(ROOT / "docker-compose.webui.yml")
        self.assertIn("${MCP_PORT:-8081}:8081", base)
        self.assertIn("${OPENWEBUI_PORT:-3000}:8080", webui)


if __name__ == "__main__":
    unittest.main()
