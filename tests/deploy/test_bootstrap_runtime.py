# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Bootstrap-to-consumer runtime provisioning against isolated fake tools."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest

from support import (
    DEFAULT_RELEASE_IMAGES,
    FAKE_DOCKER,
    OCU_SERVICE,
    PROXY_SERVICE,
    ROOT,
    SANDBOX_NETWORK,
    WEBUI_SERVICE,
    WEBUI_SYNTHETIC_SHA,
    committed_up_fixture,
    default_config_id,
    intended_docs,
    seed_healthy_host,
    seed_images,
    write_fake_configs,
    write_network,
    write_release_for_sha,
)







BOOTSTRAP = ROOT / "deploy" / "production-like-test" / "scripts" / "bootstrap-test.sh"
WRITE_VERSION = ROOT / "deploy" / "production-like-test" / "scripts" / "write-deployed-version.sh"
PROVIDER_SENTINEL = "provider-secret-not-for-logs"
ORIGIN = "https://workbench.example.test"
PUBLIC_BASE = ORIGIN + "/ocu"
INTERNAL_AUTH = "http://open-webui:8080/api/v1/ocu/auth"
IMAGES = {
    "OPENWEBUI_IMAGE": DEFAULT_RELEASE_IMAGES["open-webui"],
    "DOCKER_IMAGE": DEFAULT_RELEASE_IMAGES["workspace"],
    "COMPUTER_USE_SERVER_IMAGE": DEFAULT_RELEASE_IMAGES["computer-use-server"],
    "RETENTION_GUARD_IMAGE": DEFAULT_RELEASE_IMAGES["retention-guard"],
    "OCU_PROXY_IMAGE": DEFAULT_RELEASE_IMAGES["proxy"],
    "DOCUMENTSERVER_IMAGE": DEFAULT_RELEASE_IMAGES["documentserver"],
}

GENERATED_SECRETS = (
    "OCU_INTERNAL_TOKEN",
    "ADMIN_PASSWORD",
    "WEBUI_SECRET_KEY",
    "MCP_API_KEY",
    "POSTGRES_PASSWORD",
    "OCU_OFFICE_JWT_SECRET",
)
REQUIRED_RUNTIME = (
    "COMPOSE_PROJECT_NAME",
    "SOURCE_SHA",
    "WEBUI_SOURCE_SHA",
    "OCU_RELEASE_MANIFEST",
    "OPENWEBUI_IMAGE",
    "POSTGRES_IMAGE",
    "DOCUMENTSERVER_IMAGE",
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
    "OCU_SANDBOX_DNS",
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
        "OCU_SANDBOX_DNS": "${OCU_SANDBOX_DNS}",
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
        self.sha = committed_up_fixture(source, lock_dir=self.state / "image-store-lock")
        self.webui_sha = WEBUI_SYNTHETIC_SHA
        self.env = os.environ.copy()
        self.env["PATH"] = str(FAKE_DOCKER.parent) + os.pathsep + self.env.get("PATH", "")
        self.env["FAKE_DOCKER_STATE"] = str(self.state)
        self.env["DOCKER_HOST"] = "unix://" + str(self.state / "docker.sock")
        self.env["TMPDIR"] = str(self.private)
        self.env["HOME"] = str(self.private)
        self.env["DEPLOY_ROOT"] = str(self.deploy_root)
        self.env["DMX_ENV_FILE"] = str(self.provider)
        self.env["OCU_ADMIN_CREDENTIALS_FILE"] = str(self.credentials)
        self.env["SOURCE_SHA"] = self.sha
        self.env["OCU_WEBUI_ORIGIN"] = ORIGIN
        self.env["OCU_OFFICE_DOCSERVER_ORIGIN"] = "https://workbench.example.test:8083"
        self.env["ENABLE_OCU_OFFICE_EDIT"] = "false"
        self.env["OCU_SANDBOX_EGRESS_ALLOW"] = "8.8.8.8/32"
        self.env["OCU_SANDBOX_DNS"] = ""
        self.env.update(IMAGES)
        self.inventory = write_release_for_sha(
            self.root / "release.json",
            self.sha,
            self.webui_sha,
        )
        self.env["OCU_RELEASE_MANIFEST"] = str(self.inventory)
        self.env["WEBUI_SOURCE_SHA"] = self.webui_sha
        seed_images(self.state)




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

    def openssl_env(self, failure="none"):
        commands = self.root / "commands"
        commands.mkdir(exist_ok=True)
        command = commands / "openssl"
        command.write_text("""#!/usr/bin/env python3
import os
from pathlib import Path
import subprocess
import sys
counter = Path(os.environ["FAKE_DOCKER_STATE"]) / "openssl-count"
count = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(count))
if count == 6:
    failure = os.environ["OCU_TEST_JWT_FAILURE"]
    if failure == "empty":
        raise SystemExit(0)
    if failure == "failed":
        print("jwt-generator-secret-canary", file=sys.stderr)
        raise SystemExit(1)
raise SystemExit(subprocess.run([os.environ["OCU_TEST_REAL_OPENSSL"], *sys.argv[1:]]).returncode)
""", encoding="utf-8")
        command.chmod(0o755)
        return {"PATH": str(commands) + os.pathsep + self.env["PATH"],
                "OCU_TEST_REAL_OPENSSL": shutil.which("openssl"),
                "OCU_TEST_JWT_FAILURE": failure}

    def consumer_up_env(self, runtime):
        env = {
            "PATH": str(FAKE_DOCKER.parent) + os.pathsep + os.environ.get("PATH", ""),
            "HOME": str(self.private),
            "TMPDIR": str(self.state),
            "FAKE_DOCKER_STATE": str(self.state),
            "OCU_SANDBOX_EGRESS_LOCK": str(self.state / "ocu-sandbox-egress.lock"),
            "OCU_TEST_ROOT": str(self.deploy_root / "source"),
        }
        env.update(runtime)
        return env, self.deploy_root / "source" / "deploy" / "up.sh", self.deploy_root / "source"



    def test_non_root_fails_before_publishing(self):
        result = self.run_bootstrap({"FAKE_ID_UID": "1000"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("root", result.stderr)
        self.assert_unpublished()
        self.assertNotIn(PROVIDER_SENTINEL, self.combined(result))

    def test_office_flag_changes_only_visibility(self):
        snapshots = []
        for flag in ("true", "false"):
            result = self.run_bootstrap({"ENABLE_OCU_OFFICE_EDIT": flag})
            self.assertEqual(result.returncode, 0, result.stderr)
            runtime = parse_env_file(self.runtime_path())
            office = {name: value for name, value in runtime.items()
                      if name.startswith("OCU_OFFICE_") or name == "ENABLE_OCU_OFFICE_EDIT"}
            self.assertEqual(set(office), {
                "OCU_OFFICE_JWT_SECRET", "OCU_OFFICE_PROXY_PORT",
                "OCU_OFFICE_DOCSERVER_URL", "OCU_OFFICE_DOCSERVER_ORIGIN",
                "OCU_OFFICE_SELF_URL", "OCU_OFFICE_FONTS_DIR", "ENABLE_OCU_OFFICE_EDIT",
            })
            self.assertEqual(office["ENABLE_OCU_OFFICE_EDIT"], flag)
            self.assertEqual(office["OCU_OFFICE_PROXY_PORT"], "8083")
            self.assertEqual(office["OCU_OFFICE_DOCSERVER_URL"], "http://documentserver")
            self.assertEqual(office["OCU_OFFICE_SELF_URL"], "http://computer-use-server:8081")
            self.assertEqual(office["OCU_OFFICE_DOCSERVER_ORIGIN"],
                             self.env["OCU_OFFICE_DOCSERVER_ORIGIN"])
            fonts = self.deploy_root / "data/office-fonts"
            self.assertEqual(office["OCU_OFFICE_FONTS_DIR"], str(fonts))
            self.assertTrue(fonts.is_dir())
            self.assertEqual(list(fonts.iterdir()), [])
            self.assertNotIn("OCU_RELEASE_FONTS_DIR", runtime)
            self.assertEqual(stat.S_IMODE(self.runtime_path().stat().st_mode), 0o600)
            self.assert_generated_secrets_hidden(runtime, result)
            self.assertNotIn(office["OCU_OFFICE_JWT_SECRET"], self.credentials.read_text())
            snapshots.append(runtime)
            self.runtime_path().replace(self.private / f"{flag}-runtime.txt")
            self.credentials.replace(self.private / f"{flag}-admin.txt")
        self.assertEqual(set(snapshots[0]), set(snapshots[1]))
        for name in snapshots[0]:
            if name in GENERATED_SECRETS:
                self.assertNotEqual(snapshots[0][name], snapshots[1][name], name)
            elif name != "ENABLE_OCU_OFFICE_EDIT":
                self.assertEqual(snapshots[0][name], snapshots[1][name], name)

    def test_invalid_office_choices_refuse_both_outputs(self):
        flag_name = "ENABLE_OCU_OFFICE_EDIT"
        origin_name = "OCU_OFFICE_DOCSERVER_ORIGIN"
        cases = [(flag_name, value, {}) for value in (None, "", "TRUE", "0", "no")]
        cases += [(origin_name, value, {}) for value in (
            None, "", ORIGIN, ORIGIN + ":443", ORIGIN + "/",
            ORIGIN + "/editor", "https://user:credential-canary@docs.example.test",
            "https://docs.example.test?", "https://docs.example.test#",
            "https://docs.example.test:abc", "https://docs.example.test:65536",
        )]
        cases += [(origin_name, ORIGIN, {"OCU_WEBUI_ORIGIN": ORIGIN + ":443"})]
        for name, value, extra in cases:
            with self.subTest(name=name, value=value):
                for path in (self.runtime_path(), self.credentials):
                    if path.exists():
                        path.unlink()
                unset = [name] if value is None else None
                if value is not None:
                    extra = {**extra, name: value}
                result = self.run_bootstrap(extra, unset=unset)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(name, result.stderr)
                self.assertNotIn("credential-canary", self.combined(result))
                self.assert_unpublished()

    def test_invalid_optional_office_inputs_are_not_defaulted(self):
        cases = [("OCU_OFFICE_PROXY_PORT", value)
                 for value in ("", "abc", "0", "65536", "-1", "1.5")]
        cases += [("OCU_OFFICE_FONTS_DIR", value)
                  for value in ("", str(self.root / "font path"),
                                str(self.root / "fonts\nINJECTED=1"))]
        for name, value in cases:
            with self.subTest(name=name, value=value):
                for path in (self.runtime_path(), self.credentials):
                    if path.exists():
                        path.unlink()
                result = self.run_bootstrap({name: value})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(name, result.stderr)
                self.assert_unpublished()

    def test_operator_fonts_keep_existing_contents_and_metadata(self):
        fonts = self.root / "operator-fonts"
        fonts.mkdir(mode=0o750)
        font = fonts / "organisation.otf"
        font.write_bytes(b"OTTO organisation-owned fixture")
        font.chmod(0o640)
        os.utime(fonts, ns=(123456789, 123456789))
        before = {path: (path.stat().st_mode, path.stat().st_mtime_ns, path.stat().st_ino)
                  for path in (fonts, font)}
        result = self.run_bootstrap({"OCU_OFFICE_FONTS_DIR": str(fonts),
                                     "OCU_OFFICE_PROXY_PORT": "8444"})
        self.assertEqual(result.returncode, 0, result.stderr)
        runtime = parse_env_file(self.runtime_path())
        self.assertEqual(runtime["OCU_OFFICE_FONTS_DIR"], str(fonts))
        self.assertEqual(runtime["OCU_OFFICE_PROXY_PORT"], "8444")
        self.assertEqual(list(fonts.iterdir()), [font])
        self.assertEqual(font.read_bytes(), b"OTTO organisation-owned fixture")
        self.assertEqual({path: (path.stat().st_mode, path.stat().st_mtime_ns, path.stat().st_ino)
                          for path in (fonts, font)}, before)

    def test_failed_or_empty_jwt_generation_publishes_nothing(self):
        for failure in ("empty", "failed"):
            with self.subTest(failure=failure):
                for path in (self.runtime_path(), self.credentials, self.state / "openssl-count"):
                    if path.exists():
                        path.unlink()
                result = self.run_bootstrap(self.openssl_env(failure))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("OCU_OFFICE_JWT_SECRET", result.stderr)
                self.assertEqual((self.state / "openssl-count").read_text(), "6")
                self.assertNotIn("jwt-generator-secret-canary", self.combined(result))
                self.assert_unpublished()

    def test_missing_documentserver_reference_publishes_nothing(self):
        original = json.loads(self.inventory.read_text())
        for missing in (True, False):
            with self.subTest(missing=missing):
                payload = json.loads(json.dumps(original))
                if missing:
                    payload["images"]["documentserver"].pop("reference")
                else:
                    payload["images"]["documentserver"]["reference"] = ""
                self.inventory.write_text(json.dumps(payload))
                result = self.run_bootstrap()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("documentserver", result.stderr.lower())
                self.assert_unpublished()

    def test_success_emits_consumer_visible_topology_and_shared_token(self):
        result = self.run_bootstrap(
            extra={"OCU_RELEASE_FONTS_DIR": "/unselected/fonts"},
            unset=["DOCUMENTSERVER_IMAGE"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        runtime = parse_env_file(self.runtime_path())
        self.assertNotIn("OCU_RELEASE_FONTS_DIR", runtime)
        self.assertEqual(stat.S_IMODE(self.runtime_path().stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.credentials.stat().st_mode), 0o600)
        self.assert_no_temp_residue()
        for name in REQUIRED_RUNTIME:
            self.assertIn(name, runtime, name)
        self.assertEqual(runtime["SOURCE_SHA"], self.sha)
        self.assertEqual(runtime["WEBUI_SOURCE_SHA"], self.webui_sha)
        self.assertEqual(runtime["OCU_RELEASE_MANIFEST"], str(self.inventory))
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
        env, script, source = self.consumer_up_env(runtime)
        up = subprocess.run(
            ["bash", str(script)],
            cwd=str(source),
            capture_output=True,
            text=True,
            env=env,
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

    def test_explicit_empty_dns_is_preserved(self):
        result = self.run_bootstrap({"OCU_SANDBOX_DNS": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        runtime = parse_env_file(self.runtime_path())
        self.assertEqual(runtime["OCU_SANDBOX_DNS"], "")
        self.assertIn("OCU_SANDBOX_DNS=", self.runtime_path().read_text(encoding="utf-8"))
        self.assert_no_temp_residue()

    def test_listed_nonempty_dns_is_published(self):
        result = self.run_bootstrap({"OCU_SANDBOX_DNS": "8.8.8.8"})
        self.assertEqual(result.returncode, 0, result.stderr)
        runtime = parse_env_file(self.runtime_path())
        self.assertEqual(runtime["OCU_SANDBOX_DNS"], "8.8.8.8")
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
            ({}, ["OCU_RELEASE_MANIFEST"]),
            ({"DOCKER_IMAGE": "custom-workspace:local"}, None),
            ({"DOCUMENTSERVER_IMAGE": "unselected-documentserver:local"}, None),
            ({"COMPUTER_USE_SERVER_IMAGE": "ocu-test-server:local\nINJECTED=1"}, None),
            ({"RETENTION_GUARD_IMAGE": "ocu-test-retention:local\nINJECTED=1"}, None),
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
            ({}, ["OCU_SANDBOX_DNS"]),
            ({"OCU_SANDBOX_DNS": "not-an-ip"}, None),
            ({"OCU_SANDBOX_DNS": "169.254.169.254"}, None),
            ({"OCU_SANDBOX_DNS": "9.9.9.9"}, None),
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
        generator = self.openssl_env()
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
                result = self.run_bootstrap(generator)
                self.assertFalse((self.state / "openssl-count").exists())
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
        seed_images(
            self.state,
            {
                runtime["DOCKER_IMAGE"]: {"Id": default_config_id("workspace"), "Os": "linux", "Architecture": "amd64"},
                runtime["COMPUTER_USE_SERVER_IMAGE"]: {"Id": default_config_id("computer-use-server"), "Os": "linux", "Architecture": "amd64"},
                runtime["RETENTION_GUARD_IMAGE"]: {"Id": default_config_id("retention-guard"), "Os": "linux", "Architecture": "amd64"},
                runtime["OCU_PROXY_IMAGE"]: {"Id": default_config_id("proxy"), "Os": "linux", "Architecture": "amd64"},
                runtime["OPENWEBUI_IMAGE"]: {"Id": default_config_id("open-webui"), "Os": "linux", "Architecture": "amd64"},
                runtime["POSTGRES_IMAGE"]: {"Id": default_config_id("postgres"), "Os": "linux", "Architecture": "amd64"},
                runtime["DOCUMENTSERVER_IMAGE"]: {"Id": default_config_id("documentserver"), "Os": "linux", "Architecture": "amd64"},
            },
        )
        (self.state / "now-epoch").write_text("1800000000", encoding="utf-8")
        env = dict(self.env)
        env["OCU_RELEASE_MANIFEST"] = runtime["OCU_RELEASE_MANIFEST"]
        result = subprocess.run(
            ["bash", str(WRITE_VERSION)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        record = (self.deploy_root / "DEPLOYED_VERSION.md").read_text(encoding="utf-8")
        self.assertIn("OCU_SANDBOX_NO_AUTOSTART=1", record)
        for name in GENERATED_SECRETS:
            self.assertNotIn(runtime[name], record, name)
        self.assertNotIn(PROVIDER_SENTINEL, record)
        self.assertIn(default_config_id("workspace"), record)
        self.assertIn(runtime["COMPUTER_USE_SERVER_IMAGE"], record)
        self.assertIn(runtime["RETENTION_GUARD_IMAGE"], record)
        self.assertIn(runtime["OCU_PROXY_IMAGE"], record)
        self.assertIn(runtime["OPENWEBUI_IMAGE"], record)
        self.assertIn(runtime["POSTGRES_IMAGE"], record)
        self.assertTrue(any(
            runtime["DOCUMENTSERVER_IMAGE"] in line
            and default_config_id("documentserver") in line
            for line in record.splitlines() if "image runtime ID:" in line
        ))
        self.assertIn(self.sha, record)
        self.assertIn(self.webui_sha, record)
        self.assertNotIn(runtime["WEBUI_SECRET_KEY"], record)
        self.assertEqual(stat.S_IMODE((self.deploy_root / "DEPLOYED_VERSION.md").stat().st_mode), 0o644)

    def test_replaced_image_rejects_bootstrap_before_publication(self):
        images = json.loads((self.state / "images.json").read_text(encoding="utf-8"))
        tag = DEFAULT_RELEASE_IMAGES["proxy"]
        images[tag]["Id"] = "sha256:" + ("c" * 64)
        (self.state / "images.json").write_text(json.dumps(images), encoding="utf-8")
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("configuration digest", result.stderr)
        self.assert_unpublished()

    def test_replaced_image_preserves_existing_version_record(self):
        created = self.run_bootstrap()
        self.assertEqual(created.returncode, 0, created.stderr)
        runtime = parse_env_file(self.runtime_path())
        existing = self.deploy_root / "DEPLOYED_VERSION.md"
        existing.write_text("keep-me\n", encoding="utf-8")
        images = json.loads((self.state / "images.json").read_text(encoding="utf-8"))
        images[runtime["OCU_PROXY_IMAGE"]]["Id"] = "sha256:" + ("d" * 64)
        (self.state / "images.json").write_text(json.dumps(images), encoding="utf-8")
        env = dict(self.env)
        env["OCU_RELEASE_MANIFEST"] = runtime["OCU_RELEASE_MANIFEST"]
        result = subprocess.run(
            ["bash", str(WRITE_VERSION)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=20,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(existing.read_text(encoding="utf-8"), "keep-me\n")




if __name__ == "__main__":
    unittest.main()
