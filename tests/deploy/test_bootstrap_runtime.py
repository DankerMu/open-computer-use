# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Bootstrap-to-consumer runtime provisioning against isolated fake tools."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

from support import (
    FAKE_DOCKER,
    OCU_SERVICE,
    PROXY_SERVICE,
    ROOT,
    SANDBOX_NETWORK,
    UP,
    WEBUI_SERVICE,
    intended_docs,
    seed_healthy_host,
    write_fake_configs,
    write_network,
)


BOOTSTRAP = ROOT / "deploy" / "production-like-test" / "scripts" / "bootstrap-test.sh"
WRITE_VERSION = ROOT / "deploy" / "production-like-test" / "scripts" / "write-deployed-version.sh"
PROVIDER_SENTINEL = "provider-secret-not-for-logs"
ORIGIN = "https://workbench.example.test"
PUBLIC_BASE = ORIGIN + "/ocu"
INTERNAL_AUTH = "http://open-webui:8080/api/v1/ocu/auth"
IMAGES = {
    "OPENWEBUI_IMAGE": "ocu-test-openwebui:local",
    "DOCKER_IMAGE": "open-computer-use-test:local",
    "COMPUTER_USE_SERVER_IMAGE": "ocu-test-server:local",
    "RETENTION_GUARD_IMAGE": "ocu-test-retention:local",
    "OCU_PROXY_IMAGE": "ocu-test-proxy:local",
}
GENERATED_SECRETS = (
    "OCU_INTERNAL_TOKEN",
    "ADMIN_PASSWORD",
    "WEBUI_SECRET_KEY",
    "MCP_API_KEY",
    "POSTGRES_PASSWORD",
)
REQUIRED_RUNTIME = (
    "COMPOSE_PROJECT_NAME",
    "SOURCE_SHA",
    "OPENWEBUI_IMAGE",
    "POSTGRES_IMAGE",
    "DOCKER_IMAGE",
    "COMPUTER_USE_SERVER_IMAGE",
    "RETENTION_GUARD_IMAGE",
    "OCU_PROXY_IMAGE",
    "OCU_PRIVATE_NETWORK",
    "OCU_PRIVATE_SUBNET",
    "OCU_PRIVATE_GATEWAY",
    "OCU_SANDBOX_NETWORK",
    "OCU_SANDBOX_SUBNET",
    "OCU_SANDBOX_GATEWAY",
    "OCU_SANDBOX_EGRESS_ALLOW",
    "OCU_PROXY_PORT",
    "OCU_WEBUI_ORIGIN",
    "PUBLIC_BASE_URL",
    "OCU_WEBUI_AUTH_URL",
    "OCU_PUBLIC_PREFIX",
    "OCU_SANDBOX_NO_AUTOSTART",
    "OCU_INTERNAL_TOKEN",
)


def parse_env_file(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name] = value
    return values


def leftover_hidden(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return [path for path in directory.iterdir() if path.name.startswith(".")]


def token_docs():
    docs = intended_docs()
    docs["core.json"]["__unresolved__"] = True
    docs["webui.json"]["__unresolved__"] = True
    docs["proxy.json"]["__unresolved__"] = True
    docs["core.json"]["services"][OCU_SERVICE]["environment"] = {
        "OCU_INTERNAL_TOKEN": "${OCU_INTERNAL_TOKEN}",
    }
    docs["webui.json"]["services"][WEBUI_SERVICE]["environment"] = {
        "OCU_INTERNAL_TOKEN": "${OCU_INTERNAL_TOKEN}",
    }
    docs["proxy.json"]["services"][PROXY_SERVICE]["environment"]["OCU_INTERNAL_TOKEN"] = (
        "${OCU_INTERNAL_TOKEN}"
    )
    return docs


class BootstrapRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.context = tempfile.TemporaryDirectory(prefix="ocu-bootstrap-test-")
        self.root = Path(self.context.name)
        self.deploy_root = self.root / "deploy-root"
        self.private = self.root / "private"
        self.state = self.root / "fake-state"
        self.private.mkdir()
        self.state.mkdir()
        self.provider = self.private / "dmxapi.env"
        self.provider.write_text(f"DMXAPI_API_KEY={PROVIDER_SENTINEL}\n", encoding="utf-8")
        self.provider.chmod(0o600)
        self.credentials = self.private / "admin-credentials.txt"
        source = self.deploy_root / "source"
        source.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=str(source), check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "config", "user.email", "bootstrap@example.test"],
            cwd=str(source),
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Bootstrap Test"],
            cwd=str(source),
            check=True,
            capture_output=True,
            text=True,
        )
        (source / "README").write_text("synthetic checkout\n", encoding="utf-8")
        subprocess.run(["git", "add", "README"], cwd=str(source), check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "synthetic"],
            cwd=str(source),
            check=True,
            capture_output=True,
            text=True,
        )
        self.sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(source), text=True).strip()
        self.env = os.environ.copy()
        self.env["PATH"] = str(FAKE_DOCKER.parent) + os.pathsep + self.env.get("PATH", "")
        self.env["FAKE_DOCKER_STATE"] = str(self.state)
        self.env["TMPDIR"] = str(self.private)
        self.env["HOME"] = str(self.private)
        self.env["DEPLOY_ROOT"] = str(self.deploy_root)
        self.env["DMX_ENV_FILE"] = str(self.provider)
        self.env["OCU_ADMIN_CREDENTIALS_FILE"] = str(self.credentials)
        self.env["SOURCE_SHA"] = self.sha
        self.env["OCU_WEBUI_ORIGIN"] = ORIGIN
        self.env["OCU_SANDBOX_EGRESS_ALLOW"] = "8.8.8.8/32"
        self.env.update(IMAGES)

    def tearDown(self):
        self.context.cleanup()

    def run_bootstrap(self, extra=None, unset=None):
        env = dict(self.env)
        if unset:
            for name in unset:
                env.pop(name, None)
        if extra:
            env.update(extra)
        return subprocess.run(
            ["bash", str(BOOTSTRAP)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=20,
        )

    def combined(self, result) -> str:
        return result.stdout + result.stderr

    def runtime_path(self) -> Path:
        return self.deploy_root / "config" / "runtime.env"

    def assert_unpublished(self):
        self.assertFalse(self.runtime_path().exists())
        self.assertFalse(self.credentials.exists())
        self.assertEqual(leftover_hidden(self.deploy_root / "config"), [])
        self.assertEqual(leftover_hidden(self.private), [])

    def assert_no_temp_residue(self):
        self.assertEqual(leftover_hidden(self.deploy_root / "config"), [])
        self.assertEqual(leftover_hidden(self.private), [])

    def assert_generated_secrets_hidden(self, runtime, result):
        report = self.combined(result)
        values = [runtime[name] for name in GENERATED_SECRETS]
        self.assertEqual(len(set(values)), len(values))
        for name in GENERATED_SECRETS:
            self.assertRegex(runtime[name], r"^[0-9a-f]{64}$", name)
            self.assertNotIn(runtime[name], report)
        self.assertNotIn(PROVIDER_SENTINEL, report)

    def consumer_up_env(self, runtime):
        env = {
            "PATH": str(FAKE_DOCKER.parent) + os.pathsep + os.environ.get("PATH", ""),
            "HOME": str(self.private),
            "TMPDIR": str(self.state),
            "FAKE_DOCKER_STATE": str(self.state),
            "OCU_SANDBOX_EGRESS_LOCK": str(self.state / "ocu-sandbox-egress.lock"),
        }
        env.update(runtime)
        return env

    def test_non_root_fails_before_publishing(self):
        result = self.run_bootstrap({"FAKE_ID_UID": "1000"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("root", result.stderr)
        self.assert_unpublished()
        self.assertNotIn(PROVIDER_SENTINEL, self.combined(result))

    def test_success_emits_consumer_visible_topology_and_shared_token(self):
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        runtime = parse_env_file(self.runtime_path())
        self.assertEqual(stat.S_IMODE(self.runtime_path().stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.credentials.stat().st_mode), 0o600)
        self.assert_no_temp_residue()
        for name in REQUIRED_RUNTIME:
            self.assertIn(name, runtime, name)
        self.assertEqual(runtime["SOURCE_SHA"], self.sha)
        self.assertEqual(runtime["OCU_WEBUI_ORIGIN"], ORIGIN)
        self.assertEqual(runtime["PUBLIC_BASE_URL"], PUBLIC_BASE)
        self.assertEqual(runtime["OCU_WEBUI_AUTH_URL"], INTERNAL_AUTH)
        self.assertEqual(runtime["OCU_PUBLIC_PREFIX"], "/ocu")
        self.assertEqual(runtime["OCU_SANDBOX_NO_AUTOSTART"], "1")
        self.assertEqual(runtime["COMPOSE_PROJECT_NAME"], "ocu-test")
        self.assertEqual(runtime["OCU_PRIVATE_NETWORK"], "ocu-test-private")
        self.assertEqual(runtime["OCU_PRIVATE_SUBNET"], "172.30.0.0/24")
        self.assertEqual(runtime["OCU_PRIVATE_GATEWAY"], "172.30.0.1")
        self.assertEqual(runtime["OCU_SANDBOX_NETWORK"], "ocu-sandbox")
        self.assertEqual(runtime["OCU_SANDBOX_SUBNET"], "172.31.0.0/24")
        self.assertEqual(runtime["OCU_SANDBOX_GATEWAY"], "172.31.0.1")
        self.assertEqual(runtime["OCU_PROXY_PORT"], "8082")
        self.assertEqual(runtime["OCU_SANDBOX_EGRESS_ALLOW"], "8.8.8.8/32")
        for name, value in IMAGES.items():
            self.assertEqual(runtime[name], value, name)
        self.assertEqual(runtime["POSTGRES_IMAGE"], "postgres:17-alpine")
        self.assert_generated_secrets_hidden(runtime, result)
        self.assertNotIn("MCP_PORT=8081", self.runtime_path().read_text(encoding="utf-8"))
        self.assertNotIn("OPENWEBUI_PORT=3000", self.runtime_path().read_text(encoding="utf-8"))
        self.assertNotIn("SANDBOX_HOST_BIND_IP=", self.runtime_path().read_text(encoding="utf-8"))
        self.assertNotIn("ghcr.io/open-webui/open-webui", self.runtime_path().read_text(encoding="utf-8"))
        credential_text = self.credentials.read_text(encoding="utf-8")
        self.assertIn("admin@ai-test.local", credential_text)
        self.assertIn(runtime["ADMIN_PASSWORD"], credential_text)
        for name in GENERATED_SECRETS:
            if name == "ADMIN_PASSWORD":
                continue
            self.assertNotIn(runtime[name], credential_text)
        self.assertNotIn(PROVIDER_SENTINEL, credential_text)

        write_fake_configs(self.state, token_docs())
        seed_healthy_host(self.state)
        write_network(
            self.state,
            runtime["OCU_PRIVATE_NETWORK"],
            subnet=runtime["OCU_PRIVATE_SUBNET"],
            gateway=runtime["OCU_PRIVATE_GATEWAY"],
        )
        write_network(
            self.state,
            SANDBOX_NETWORK,
            subnet=runtime["OCU_SANDBOX_SUBNET"],
            gateway=runtime["OCU_SANDBOX_GATEWAY"],
        )
        token = runtime["OCU_INTERNAL_TOKEN"]
        up = subprocess.run(
            ["bash", str(UP)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=self.consumer_up_env(runtime),
            check=False,
            timeout=20,
        )
        self.assertEqual(up.returncode, 0, up.stderr)
        self.assertNotIn(token, up.stdout + up.stderr)
        executed = [
            json.loads(line)
            for line in (self.state / "executed.json").read_text(encoding="utf-8").splitlines()
            if line
        ]
        self.assertEqual([row["stack"] for row in executed], ["core", "webui", "proxy"])
        by_stack = {row["stack"]: row for row in executed}
        for stack, service in (("core", OCU_SERVICE), ("webui", WEBUI_SERVICE), ("proxy", PROXY_SERVICE)):
            self.assertEqual(
                by_stack[stack]["document"]["services"][service]["environment"]["OCU_INTERNAL_TOKEN"],
                token,
            )
            self.assertEqual(
                by_stack[stack]["consumer_document"]["services"][service]["environment"]["OCU_INTERNAL_TOKEN"],
                token,
            )

    def test_generated_credentials_are_fresh_across_deployments(self):
        first = self.run_bootstrap()
        self.assertEqual(first.returncode, 0, first.stderr)
        first_runtime = parse_env_file(self.runtime_path())
        self.assert_generated_secrets_hidden(first_runtime, first)
        first_copy = self.private / "first-runtime.env"
        first_creds = self.private / "first-admin.txt"
        self.runtime_path().replace(first_copy)
        self.credentials.replace(first_creds)
        second = self.run_bootstrap()
        self.assertEqual(second.returncode, 0, second.stderr)
        second_runtime = parse_env_file(self.runtime_path())
        self.assert_generated_secrets_hidden(second_runtime, second)
        self.assert_no_temp_residue()
        for name in GENERATED_SECRETS:
            self.assertNotEqual(first_runtime[name], second_runtime[name], name)

    def test_explicit_empty_egress_is_preserved(self):
        result = self.run_bootstrap({"OCU_SANDBOX_EGRESS_ALLOW": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        runtime = parse_env_file(self.runtime_path())
        self.assertEqual(runtime["OCU_SANDBOX_EGRESS_ALLOW"], "")
        self.assertIn("OCU_SANDBOX_EGRESS_ALLOW=", self.runtime_path().read_text(encoding="utf-8"))
        self.assert_no_temp_residue()

    def test_origin_with_explicit_port_composes_public_base(self):
        origin = "https://workbench.example.test:8443"
        result = self.run_bootstrap({"OCU_WEBUI_ORIGIN": origin})
        self.assertEqual(result.returncode, 0, result.stderr)
        runtime = parse_env_file(self.runtime_path())
        self.assertEqual(runtime["OCU_WEBUI_ORIGIN"], origin)
        self.assertEqual(runtime["PUBLIC_BASE_URL"], origin + "/ocu")
        self.assert_no_temp_residue()

    def test_missing_or_conflicting_input_fails_before_publish(self):
        cases = (
            ({}, ["SOURCE_SHA"]),
            ({"SOURCE_SHA": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}, None),
            ({}, ["OPENWEBUI_IMAGE"]),
            ({}, ["DOCKER_IMAGE"]),
            ({"DOCKER_IMAGE": "custom-workspace:local"}, None),
            ({}, ["COMPUTER_USE_SERVER_IMAGE"]),
            ({"COMPUTER_USE_SERVER_IMAGE": "ocu-test-server:local\nINJECTED=1"}, None),
            ({}, ["RETENTION_GUARD_IMAGE"]),
            ({"RETENTION_GUARD_IMAGE": "ocu-test-retention:local\nINJECTED=1"}, None),
            ({}, ["OCU_PROXY_IMAGE"]),
            ({}, ["OCU_WEBUI_ORIGIN"]),
            ({"OCU_WEBUI_ORIGIN": "https://workbench.example.test/"}, None),
            ({"OCU_WEBUI_ORIGIN": "https://user:pass@workbench.example.test"}, None),
            ({"OCU_WEBUI_ORIGIN": "https://workbench.example.test/ocu"}, None),
            ({"OCU_WEBUI_ORIGIN": "https://workbench.example.test?"}, None),
            ({"OCU_WEBUI_ORIGIN": "https://workbench.example.test#"}, None),
            ({"OCU_WEBUI_ORIGIN": "https://workbench.example.test:abc"}, None),
            ({"OCU_WEBUI_ORIGIN": "https://workbench.example.test:99999"}, None),
            ({}, ["OCU_SANDBOX_EGRESS_ALLOW"]),
            ({"OCU_SANDBOX_EGRESS_ALLOW": "not-an-ip"}, None),
            ({"OCU_WEBUI_ORIGIN": "https://workbench.example.test\nOCU_INTERNAL_TOKEN=injected"}, None),
        )
        for extra, unset in cases:
            with self.subTest(extra=extra, unset=unset):
                if self.runtime_path().exists():
                    self.runtime_path().unlink()
                if self.credentials.exists():
                    self.credentials.unlink()
                result = self.run_bootstrap(extra=extra, unset=unset)
                self.assertNotEqual(result.returncode, 0)
                self.assert_unpublished()
                self.assertNotIn(PROVIDER_SENTINEL, self.combined(result))

    def test_post_temp_failure_removes_temporary_files(self):
        result = self.run_bootstrap(
            {
                "OCU_FAKE_INSTALL_FAIL_FIRST": "1",
                "OCU_REAL_INSTALL": "/usr/bin/install",
            }
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.state / "install-fail-reached").read_text(encoding="utf-8"), "1")
        self.assert_unpublished()
        self.assertNotIn(PROVIDER_SENTINEL, self.combined(result))

    def test_insecure_provider_file_fails_before_publish(self):
        self.provider.chmod(0o644)
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assert_unpublished()
        self.assertNotIn(PROVIDER_SENTINEL, self.combined(result))

    def test_existing_outputs_are_preserved(self):
        cases = (
            ("runtime", True, False),
            ("credentials", False, True),
            ("both", True, True),
        )
        for label, write_runtime, write_credentials in cases:
            with self.subTest(existing=label):
                if self.runtime_path().exists():
                    self.runtime_path().unlink()
                if self.credentials.exists():
                    self.credentials.unlink()
                self.runtime_path().parent.mkdir(parents=True, exist_ok=True)
                if write_runtime:
                    self.runtime_path().write_text("EXISTING_RUNTIME=keep-me\n", encoding="utf-8")
                if write_credentials:
                    self.credentials.write_text("EXISTING_ADMIN=keep-me\n", encoding="utf-8")
                result = self.run_bootstrap()
                self.assertNotEqual(result.returncode, 0)
                if write_runtime:
                    self.assertEqual(self.runtime_path().read_text(encoding="utf-8"), "EXISTING_RUNTIME=keep-me\n")
                else:
                    self.assertFalse(self.runtime_path().exists())
                if write_credentials:
                    self.assertEqual(self.credentials.read_text(encoding="utf-8"), "EXISTING_ADMIN=keep-me\n")
                else:
                    self.assertFalse(self.credentials.exists())
                self.assertEqual(leftover_hidden(self.deploy_root / "config"), [])
                self.assertEqual(leftover_hidden(self.private), [])
                self.assertNotIn(PROVIDER_SENTINEL, self.combined(result))

    def test_version_record_is_secret_free_and_records_environment_policy(self):
        created = self.run_bootstrap()
        self.assertEqual(created.returncode, 0, created.stderr)
        runtime = parse_env_file(self.runtime_path())
        (self.state / "images.json").write_text(
            json.dumps(
                {
                    runtime["DOCKER_IMAGE"]: {"Id": "sha256:workspace-fake"},
                    runtime["COMPUTER_USE_SERVER_IMAGE"]: {"Id": "sha256:server-fake"},
                    runtime["RETENTION_GUARD_IMAGE"]: {"Id": "sha256:retention-fake"},
                }
            ),
            encoding="utf-8",
        )
        (self.state / "now-epoch").write_text("1800000000", encoding="utf-8")
        result = subprocess.run(
            ["bash", str(WRITE_VERSION)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=self.env,
            check=False,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        record = (self.deploy_root / "DEPLOYED_VERSION.md").read_text(encoding="utf-8")
        self.assertIn("OCU_SANDBOX_NO_AUTOSTART=1", record)
        self.assertNotIn("disable-cli-autostart.patch", record)
        for name in GENERATED_SECRETS:
            self.assertNotIn(runtime[name], record, name)
        self.assertNotIn(PROVIDER_SENTINEL, record)
        self.assertIn("sha256:workspace-fake", record)
        self.assertIn(runtime["COMPUTER_USE_SERVER_IMAGE"], record)
        self.assertIn(runtime["RETENTION_GUARD_IMAGE"], record)
        self.assertEqual(stat.S_IMODE((self.deploy_root / "DEPLOYED_VERSION.md").stat().st_mode), 0o644)


if __name__ == "__main__":
    unittest.main()
