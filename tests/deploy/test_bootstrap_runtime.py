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
    ROOT,
    SANDBOX_NETWORK,
    UP,
    fake_env,
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
        subprocess.run(
            ["git", "init", "-q"],
            cwd=str(source),
            check=True,
            capture_output=True,
            text=True,
        )
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
        subprocess.run(
            ["git", "add", "README"],
            cwd=str(source),
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "synthetic"],
            cwd=str(source),
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "commit", "--amend", "-q", "--no-edit", f"--date=1970-01-01T00:00:00"],
            cwd=str(source),
            check=False,
            capture_output=True,
            text=True,
        )
        self.sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(source),
            text=True,
        ).strip()
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
        self.assertEqual(runtime["OCU_WEBUI_ORIGIN"], ORIGIN)
        self.assertEqual(runtime["PUBLIC_BASE_URL"], PUBLIC_BASE)
        self.assertEqual(runtime["OCU_WEBUI_AUTH_URL"], INTERNAL_AUTH)
        self.assertEqual(runtime["OCU_PUBLIC_PREFIX"], "/ocu")
        self.assertEqual(runtime["OCU_SANDBOX_NO_AUTOSTART"], "1")
        self.assertEqual(runtime["OCU_PRIVATE_NETWORK"], "ocu-test-private")
        self.assertEqual(runtime["OCU_SANDBOX_NETWORK"], "ocu-sandbox")
        self.assertEqual(runtime["OCU_SANDBOX_GATEWAY"], "172.31.0.1")
        self.assertEqual(runtime["OCU_PROXY_PORT"], "8082")
        self.assertEqual(runtime["OCU_SANDBOX_EGRESS_ALLOW"], "8.8.8.8/32")
        self.assertEqual(runtime["DOCKER_IMAGE"], IMAGES["DOCKER_IMAGE"])
        token = runtime["OCU_INTERNAL_TOKEN"]
        self.assertRegex(token, r"^[0-9a-f]{64}$")
        self.assertNotIn(token, self.combined(result))
        self.assertNotIn(PROVIDER_SENTINEL, self.combined(result))
        self.assertNotIn(runtime["ADMIN_PASSWORD"], self.combined(result))
        self.assertNotIn("MCP_PORT=8081", self.runtime_path().read_text(encoding="utf-8"))
        self.assertNotIn("OPENWEBUI_PORT=3000", self.runtime_path().read_text(encoding="utf-8"))
        self.assertNotIn("SANDBOX_HOST_BIND_IP=", self.runtime_path().read_text(encoding="utf-8"))
        self.assertNotIn("ghcr.io/open-webui/open-webui", self.runtime_path().read_text(encoding="utf-8"))
        credential_text = self.credentials.read_text(encoding="utf-8")
        self.assertIn("admin@ai-test.local", credential_text)
        self.assertIn(runtime["ADMIN_PASSWORD"], credential_text)
        self.assertNotIn(token, credential_text)
        self.assertNotIn(PROVIDER_SENTINEL, credential_text)

        write_fake_configs(self.state, intended_docs())
        seed_healthy_host(self.state)
        write_network(
            self.state,
            "ocu-test-private",
            subnet="172.30.0.0/24",
            gateway="172.30.0.1",
        )
        write_network(
            self.state,
            SANDBOX_NETWORK,
            subnet="172.31.0.0/24",
            gateway="172.31.0.1",
        )
        up_env = fake_env(self.state, runtime)
        up_env["PATH"] = str(FAKE_DOCKER.parent) + os.pathsep + up_env.get("PATH", "")
        up = subprocess.run(
            ["bash", str(UP)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=up_env,
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

    def test_explicit_empty_egress_is_preserved(self):
        result = self.run_bootstrap({"OCU_SANDBOX_EGRESS_ALLOW": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        runtime = parse_env_file(self.runtime_path())
        self.assertEqual(runtime["OCU_SANDBOX_EGRESS_ALLOW"], "")
        self.assertIn("OCU_SANDBOX_EGRESS_ALLOW=", self.runtime_path().read_text(encoding="utf-8"))

    def test_missing_or_conflicting_input_fails_before_publish(self):
        cases = (
            ({}, ["SOURCE_SHA"]),
            ({"SOURCE_SHA": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}, None),
            ({}, ["OPENWEBUI_IMAGE"]),
            ({}, ["DOCKER_IMAGE"]),
            ({"DOCKER_IMAGE": "custom-workspace:local"}, None),
            ({}, ["OCU_PROXY_IMAGE"]),
            ({}, ["OCU_WEBUI_ORIGIN"]),
            ({"OCU_WEBUI_ORIGIN": "https://workbench.example.test/"}, None),
            ({"OCU_WEBUI_ORIGIN": "https://user:pass@workbench.example.test"}, None),
            ({"OCU_WEBUI_ORIGIN": "https://workbench.example.test/ocu"}, None),
            ({"OCU_WEBUI_ORIGIN": "https://workbench.example.test?x=1"}, None),
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
                self.assertFalse(self.runtime_path().exists())
                self.assertFalse(self.credentials.exists())
                self.assertNotIn(PROVIDER_SENTINEL, self.combined(result))
                self.assertEqual(
                    leftover_hidden(self.deploy_root / "config"),
                    [],
                )

    def test_insecure_provider_file_fails_before_publish(self):
        self.provider.chmod(0o644)
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.runtime_path().exists())
        self.assertFalse(self.credentials.exists())
        self.assertNotIn(PROVIDER_SENTINEL, self.combined(result))

    def test_existing_outputs_are_preserved(self):
        self.runtime_path().parent.mkdir(parents=True)
        self.runtime_path().write_text("EXISTING_RUNTIME=keep-me\n", encoding="utf-8")
        self.credentials.write_text("EXISTING_ADMIN=keep-me\n", encoding="utf-8")
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.runtime_path().read_text(encoding="utf-8"), "EXISTING_RUNTIME=keep-me\n")
        self.assertEqual(self.credentials.read_text(encoding="utf-8"), "EXISTING_ADMIN=keep-me\n")
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
        self.assertNotIn(runtime["OCU_INTERNAL_TOKEN"], record)
        self.assertNotIn(runtime["ADMIN_PASSWORD"], record)
        self.assertNotIn(runtime["MCP_API_KEY"], record)
        self.assertNotIn(PROVIDER_SENTINEL, record)
        self.assertNotIn(runtime["WEBUI_SECRET_KEY"], record)
        self.assertIn("sha256:workspace-fake", record)
        self.assertEqual(stat.S_IMODE((self.deploy_root / "DEPLOYED_VERSION.md").stat().st_mode), 0o644)


if __name__ == "__main__":
    unittest.main()
