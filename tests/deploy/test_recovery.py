# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Cold backup, empty-target restore, and previous-release activation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import unittest

from support import (
    DEFAULT_RELEASE_DIGESTS,
    DEFAULT_RELEASE_IMAGES,
    FAKE_DOCKER,
    ROLE_ORDER,
    ROOT,
    UP_FIXTURE_PATHS,
    WEBUI_SYNTHETIC_SHA,
    config_payload,
    copy_tracked,
    git_init_commit,
    prepare_up_context,
    seed_healthy_host,
    seed_images,
    synthetic_inventory,
    tmp_dir,
    write_fake_configs,
    write_image_archive,
    write_inventory,
    write_network,
)

sys.path.insert(0, str(ROOT / "deploy"))

RECOVERY = ROOT / "deploy" / "recovery.py"
CHAT_ID = "chat-live"
ORPHAN_ID = "chat-orphan"
PROVIDER_A = "credential-A"
PROVIDER_B = "credential-B"


def stack_container(name: str, *, running=True, paused=False, extra=None):
    body = {
        "Id": hashlib.sha256(name.encode("utf-8")).hexdigest()[:12],
        "Name": name,
        "State": {
            "Status": "paused" if paused else ("running" if running else "exited"),
            "Running": running and not paused,
            "Paused": paused,
        },
        "Labels": {},
        "HostConfig": {"NetworkMode": "ocu-test-private"},
        "NetworkSettings": {
            "Networks": {
                "ocu-test-private": {"NetworkID": "id-ocu-test-private", "IPAddress": "172.30.0.10"}
            }
        },
    }
    if extra:
        body.update(extra)
    return body


def sandbox(name: str, chat_id: str, *, running=True, paused=False):
    return stack_container(
        name,
        running=running,
        paused=paused,
        extra={
            "Labels": {
                "managed-by": "mcp-computer-use-orchestrator",
                "chat-id": chat_id,
                "tool": "computer-use-mcp",
            },
            "HostConfig": {"NetworkMode": "ocu-sandbox"},
            "NetworkSettings": {
                "Networks": {
                    "ocu-sandbox": {"NetworkID": "id-ocu-sandbox", "IPAddress": "172.31.0.10"}
                }
            },
        },
    )


class RecoveryCliTests(unittest.TestCase):
    def setUp(self):
        self.context = tmp_dir()
        self.root = Path(self.context.name)
        self.state = self.root / "fake-state"
        self.state.mkdir()
        self.lock_dir = self.root / "image-store-lock"
        self.lock_dir.mkdir()
        self.recovery_lock = self.root / "recovery-lock"
        self.recovery_lock.mkdir()
        self.deploy_root = self.root / "source-root"
        self.chat_dir = self.deploy_root / "data" / "chat"
        self.skills_dir = self.deploy_root / "data" / "skills-cache"
        self.chat_dir.mkdir(parents=True)
        self.skills_dir.mkdir(parents=True)
        self.env, self.up, self.source = prepare_up_context(self.state)
        self.env["PATH"] = str(FAKE_DOCKER.parent) + os.pathsep + self.env.get("PATH", "")
        self.env["DOCKER_HOST"] = "unix:///var/run/docker.sock"
        self.env.pop("FAKE_ID_UID", None)
        self.env.pop("IMAGE_STORE_LOCK_DIR", None)
        self.env.pop("OCU_RECOVERY_LOCK_DIR", None)
        self.launcher = self._write_launcher()
        seed_images(self.state)
        seed_healthy_host(self.state)
        write_network(self.state, "ocu-test-private", subnet="172.30.0.0/24", gateway="172.30.0.1")
        write_network(self.state, "ocu-sandbox", subnet="172.31.0.0/24", gateway="172.31.0.1")
        write_fake_configs(self.state)
        self.runtime = self.deploy_root / "config" / "runtime.env"
        self.runtime.parent.mkdir(parents=True)
        payload = {
            "COMPOSE_PROJECT_NAME": "ocu-test",
            "OCU_CHAT_DATA_DIR": str(self.chat_dir),
            "OCU_SKILLS_CACHE_DIR": str(self.skills_dir),
            "OCU_RELEASE_MANIFEST": self.env["OCU_RELEASE_MANIFEST"],
            "SOURCE_SHA": self.env["SOURCE_SHA"],
            "WEBUI_SOURCE_SHA": WEBUI_SYNTHETIC_SHA,
            "POSTGRES_PASSWORD": "pg-secret",
            "OCU_INTERNAL_TOKEN": "internal-secret",
            "OPENAI_API_KEY": PROVIDER_A,
            "DMXAPI_API_KEY": PROVIDER_A,
            "OCU_WEBUI_ORIGIN": "https://workbench.example.test",
            "PUBLIC_BASE_URL": "https://workbench.example.test/ocu",
            "OCU_WEBUI_AUTH_URL": "http://open-webui:8080/api/v1/ocu/auth",
            "OCU_PRIVATE_NETWORK": "ocu-test-private",
            "OCU_PRIVATE_SUBNET": "172.30.0.0/24",
            "OCU_PRIVATE_GATEWAY": "172.30.0.1",
            "OCU_SANDBOX_NETWORK": "ocu-sandbox",
            "OCU_SANDBOX_SUBNET": "172.31.0.0/24",
            "OCU_SANDBOX_GATEWAY": "172.31.0.1",
            "OCU_SANDBOX_EGRESS_ALLOW": "8.8.8.8/32",
            "OCU_SANDBOX_DNS": "8.8.8.8",
            "OCU_PROXY_PORT": "8082",
        }
        for role, name in (
            ("workspace", "DOCKER_IMAGE"),
            ("computer-use-server", "COMPUTER_USE_SERVER_IMAGE"),
            ("retention-guard", "RETENTION_GUARD_IMAGE"),
            ("proxy", "OCU_PROXY_IMAGE"),
            ("open-webui", "OPENWEBUI_IMAGE"),
            ("postgres", "POSTGRES_IMAGE"),
        ):
            payload[name] = DEFAULT_RELEASE_IMAGES[role]
        self.runtime.write_text("\n".join(f"{k}={v}" for k, v in payload.items()) + "\n", encoding="utf-8")
        os.chmod(self.runtime, 0o600)
        self._seed_chat()
        self._seed_volumes_and_db()

    def tearDown(self):
        self.context.cleanup()

    def _seed_chat(self):
        live = self.chat_dir / CHAT_ID
        (live / "uploads").mkdir(parents=True)
        (live / "outputs").mkdir()
        environment = self.env.copy()
        environment.update(
            {
                "BASE_DATA_DIR": str(self.chat_dir),
                "PYTHONPATH": str(ROOT / "computer-use-server"),
                "DOCKER_HOST": "unix:///tmp/ocu-recovery-fixture-no-docker.sock",
                "DOCKER_SOCKET": "unix:///tmp/ocu-recovery-fixture-no-docker.sock",
            }
        )
        subprocess.run(
            [sys.executable, "-", str(live / "outputs" / "result.txt"), CHAT_ID],
            input=(
                "import sys\n"
                "from pathlib import Path\n"
                "import docker_manager\n"
                "import outputs_broker\n"
                "def forbidden_client():\n"
                "    raise AssertionError('broker fixture must not contact Docker')\n"
                "docker_manager.get_docker_client = forbidden_client\n"
                "broker = outputs_broker.OutputsBroker()\n"
                "for content in (b'first', b'second version', b'broker-bytes\\n'):\n"
                "    Path(sys.argv[1]).write_bytes(content)\n"
                "    broker.reconcile(sys.argv[2])\n"
            ),
            env=environment,
            text=True,
            capture_output=True,
            check=True,
        )
        index = json.loads((live / ".ocu" / "index.json").read_text(encoding="utf-8"))
        self.file_id = index["active"]["result.txt"]["file_id"]
        (self.skills_dir / "demo").mkdir()
        (self.skills_dir / "demo" / "SKILL.md").write_text("skill\n", encoding="utf-8")

    def _volume(self, name: str, files: dict[str, bytes]) -> Path:
        mount = self.state / "volume-data" / name
        mount.mkdir(parents=True, exist_ok=True)
        for relative, data in files.items():
            path = mount / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        volumes = {}
        existing = self.state / "volumes.json"
        if existing.exists():
            volumes = json.loads(existing.read_text(encoding="utf-8"))
        volumes[name] = {"Name": name, "Mountpoint": str(mount)}
        existing.write_text(json.dumps(volumes), encoding="utf-8")
        return mount

    def _seed_volumes_and_db(self, *, running_sandbox=True, paused=False, extra_containers=None):
        self._volume("ocu-test_open-webui-data", {".computer-use-initialized": b"1\n"})
        self._volume("ocu-test_postgres-data", {"PG_VERSION": b"17\n"})
        self._volume(f"chat-{CHAT_ID}-workspace", {"README.md": b"sandbox-home\n"})
        containers = [
            stack_container("ocu-test-computer-use-server"),
            stack_container("ocu-test-retention-guard"),
            stack_container("ocu-test-open-webui-init", running=False),
            stack_container("ocu-test-proxy"),
            stack_container("ocu-test-open-webui-1"),
            stack_container("ocu-test-postgres-1"),
            sandbox(f"owui-chat-{CHAT_ID}", CHAT_ID, running=running_sandbox, paused=paused),
        ]
        if extra_containers:
            containers.extend(extra_containers)
        (self.state / "containers.json").write_text(json.dumps(containers), encoding="utf-8")
        (self.state / "postgres.json").write_text(
            json.dumps(
                {
                    "ocu-test-postgres-1": {
                        "alembic_revision": "e6f7a8b9c0d1",
                        "server_version": "17.5",
                        "extensions": ["plpgsql=1.0"],
                        "chats": [CHAT_ID],
                        "chat_state": [
                            {
                                "chat_id": CHAT_ID,
                                "last_seen_revision": 2,
                                "preferences": "keep-live",
                                "owner_id": "owner-live",
                                "file_ids": self.file_id,
                            },
                            {
                                "chat_id": ORPHAN_ID,
                                "last_seen_revision": 9,
                                "preferences": "drop-me",
                                "owner_id": "owner-orphan",
                                "file_ids": "gone",
                            },
                        ],
                        "config": [json.dumps({"OPENAI_API_KEY": PROVIDER_A})],
                    }
                }
            ),
            encoding="utf-8",
        )

    def _write_launcher(self) -> Path:
        package = self.root / "recovery-pkg"
        shutil.copytree(ROOT / "deploy", package)
        recovery_path = package / "recovery.py"
        source = recovery_path.read_text(encoding="utf-8")
        replacements = {
            'RECOVERY_LOCK_DIR = Path("/run/ocu-recovery")': (
                f"RECOVERY_LOCK_DIR = Path({str(self.recovery_lock)!r})"
            ),
            "if os.geteuid() != 0:": (
                "if int(os.environ.get('OCU_TEST_EUID', os.geteuid())) != 0:"
            ),
        }
        for old, new in replacements.items():
            if old not in source:
                raise AssertionError(f"launcher cannot patch {old}")
            source = source.replace(old, new, 1)
        recovery_path.write_text(source, encoding="utf-8")
        lock_path = package / "release.py"
        lock_source = lock_path.read_text(encoding="utf-8")
        lock_literal = 'IMAGE_STORE_LOCK_DIR = Path("/run/ocu-image-store")'
        if lock_literal not in lock_source:
            raise AssertionError("launcher cannot patch IMAGE_STORE_LOCK_DIR")
        lock_path.write_text(
            lock_source.replace(
                lock_literal,
                f"IMAGE_STORE_LOCK_DIR = Path({str(self.lock_dir)!r})",
                1,
            ),
            encoding="utf-8",
        )
        path = self.root / "recovery-launcher.py"
        path.write_text(
            "\n".join(
                [
                    "import runpy",
                    "import sys",
                    f"sys.path.insert(0, {str(package)!r})",
                    "sys.modules.pop('recovery', None)",
                    "sys.modules.pop('recovery_resources', None)",
                    "sys.modules.pop('recovery_fs', None)",
                    "sys.modules.pop('recovery_db', None)",
                    "sys.modules.pop('recovery_helper', None)",
                    "sys.modules.pop('release', None)",
                    "runpy.run_module('recovery', run_name='__main__')",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        path.chmod(0o755)
        self.recovery_pkg = package
        return path

    def _write_retained_delivery(self, name="retained-delivery"):
        source = self.root / f"{name}-source"
        source.mkdir()
        for relative in UP_FIXTURE_PATHS:
            copy_tracked(relative, source)
        (source / "README").write_text(f"retained delivery source {name}\n", encoding="utf-8")
        (source / "IDENTITY").write_text(f"{name}\n", encoding="utf-8")
        ocu_sha = git_init_commit(source, f"retained delivery {name}")
        delivery = self.root / name
        images_dir = delivery / "images"
        images_dir.mkdir(parents=True)
        image_records = {}
        for role in ROLE_ORDER:
            tag = DEFAULT_RELEASE_IMAGES[role]
            digest = DEFAULT_RELEASE_DIGESTS[role]
            config = config_payload(digest)
            archive = write_image_archive(images_dir / f"{role}.tar", {tag: config})
            image_records[role] = {
                "reference": tag,
                "configuration_digest": "sha256:" + hashlib.sha256(config).hexdigest(),
                "archive": {
                    "path": f"images/{role}.tar",
                    "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                },
            }
        bundle = delivery / "source.bundle"
        subprocess.run(
            ["git", "bundle", "create", str(bundle), "HEAD"],
            cwd=str(source),
            check=True,
            capture_output=True,
            text=True,
        )
        payload = synthetic_inventory(
            ocu_sha=ocu_sha,
            webui_sha=WEBUI_SYNTHETIC_SHA,
            images=image_records,
            bundle_sha=hashlib.sha256(bundle.read_bytes()).hexdigest(),
        )
        write_inventory(delivery / "release.json", payload)
        return delivery, payload, source

    def run_cli(self, args, extra=None, uid="0"):
        env = dict(self.env)
        env["OCU_TEST_EUID"] = uid
        env["DEPLOY_ROOT"] = str(self.deploy_root)
        env["PYTHONPATH"] = str(self.recovery_pkg) + os.pathsep + env.get("PYTHONPATH", "")
        env["DOCKER_HOST"] = "unix:///var/run/docker.sock"
        if extra:
            env.update(extra)
        env["DOCKER_HOST"] = "unix:///var/run/docker.sock"
        return subprocess.run(
            [sys.executable, str(self.launcher), *args],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=30,
        )

    def test_full_cold_capture_includes_detached_workspace(self):
        dest = self.root / "backup"
        result = self.run_cli(
            [
                "backup",
                "--deploy-root",
                str(self.deploy_root),
                "--destination",
                str(dest),
                "--runtime-file",
                str(self.runtime),
            ]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((dest / "recovery.json").read_text(encoding="utf-8"))
        self.assertIn(CHAT_ID, manifest["components"]["workspaces"])
        self.assertTrue((dest / "workspaces" / f"{CHAT_ID}.tar.gz").exists())
        self.assertTrue((self.state / "stopped.log").exists())
        self.assertIn("owui-chat-" + CHAT_ID, json.dumps(json.loads((self.state / "containers.json").read_text(encoding="utf-8"))))
        self.assertNotIn("docker start", (self.state / "ops.log").read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o700)
        self.assertNotIn(PROVIDER_A, result.stdout + result.stderr)

    def test_paused_writer_and_unknown_filter_fail_without_publication(self):
        self._seed_volumes_and_db(paused=True)
        dest = self.root / "paused-backup"
        result = self.run_cli(
            [
                "backup",
                "--deploy-root",
                str(self.deploy_root),
                "--destination",
                str(dest),
                "--runtime-file",
                str(self.runtime),
            ]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(dest.exists())
        unknown = subprocess.run(
            [str(FAKE_DOCKER), "ps", "-q", "--filter", "ancestor=busybox"],
            capture_output=True,
            text=True,
            env=self.env,
            check=False,
        )
        self.assertNotEqual(unknown.returncode, 0)
        self.assertIn("unsupported docker command", unknown.stderr)

    def test_restore_rejects_same_daemon_and_preserves_live_identity(self):
        backup = self.root / "backup"
        created = self.run_cli(
            [
                "backup",
                "--deploy-root",
                str(self.deploy_root),
                "--destination",
                str(backup),
                "--runtime-file",
                str(self.runtime),
            ]
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        provider = self.root / "provider.env"
        provider.write_text(f"DMXAPI_API_KEY={PROVIDER_B}\nOPENAI_API_KEY={PROVIDER_B}\n", encoding="utf-8")
        os.chmod(provider, 0o600)
        same = self.run_cli(
            [
                "restore",
                "--recovery-set",
                str(backup),
                "--destination-root",
                str(self.root / "same"),
                "--provider-file",
                str(provider),
            ]
        )
        self.assertNotEqual(same.returncode, 0)
        self.assertIn("distinct empty target daemon", same.stderr)
        target_state = self.root / "target-state"
        target_state.mkdir()
        seed_images(target_state)
        seed_healthy_host(target_state)
        write_network(target_state, "ocu-test-private", subnet="172.30.0.0/24", gateway="172.30.0.1")
        write_network(target_state, "ocu-sandbox", subnet="172.31.0.0/24", gateway="172.31.0.1")
        write_fake_configs(target_state)
        (target_state / "docker-info.json").write_text(
            json.dumps({"ID": "target-daemon"}), encoding="utf-8"
        )
        dest = self.root / "restored-root"
        restored = self.run_cli(
            [
                "restore",
                "--recovery-set",
                str(backup),
                "--destination-root",
                str(dest),
                "--provider-file",
                str(provider),
            ],
            extra={"FAKE_DOCKER_STATE": str(target_state)},
        )
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertTrue((dest / ".restored").exists())
        live = dest / "data" / "chat" / CHAT_ID / "outputs" / "result.txt"
        self.assertEqual(live.read_bytes(), b"broker-bytes\n")
        listing = json.loads((dest / "data" / "chat" / CHAT_ID / ".ocu" / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(listing["counter"], 3)
        self.assertEqual(listing["active"]["result.txt"]["file_id"], self.file_id)
        db = json.loads((target_state / "postgres.json").read_text(encoding="utf-8"))
        state_rows = next(iter(db.values()))["chat_state"]
        self.assertEqual([row["chat_id"] for row in state_rows], [CHAT_ID])
        self.assertIn(PROVIDER_A, json.dumps(next(iter(db.values()))["config"]))
        self.assertNotIn(PROVIDER_B, restored.stdout + restored.stderr)
        retry = self.run_cli(
            [
                "restore",
                "--recovery-set",
                str(backup),
                "--destination-root",
                str(dest),
                "--provider-file",
                str(provider),
            ],
            extra={"FAKE_DOCKER_STATE": str(target_state)},
        )
        self.assertNotEqual(retry.returncode, 0)

    def test_corrupt_archive_and_socket_mismatch_refuse_before_allocation(self):
        backup = self.root / "backup"
        created = self.run_cli(
            [
                "backup",
                "--deploy-root",
                str(self.deploy_root),
                "--destination",
                str(backup),
                "--runtime-file",
                str(self.runtime),
            ]
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        (backup / "chat-data.tar.gz").write_bytes(b"not-an-archive")
        provider = self.root / "provider.env"
        provider.write_text(f"DMXAPI_API_KEY={PROVIDER_B}\n", encoding="utf-8")
        os.chmod(provider, 0o600)
        dest = self.root / "corrupt-root"
        result = self.run_cli(
            [
                "restore",
                "--recovery-set",
                str(backup),
                "--destination-root",
                str(dest),
                "--provider-file",
                str(provider),
            ]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(dest.exists())
        mismatch = self.run_cli(
            [
                "restore",
                "--recovery-set",
                str(backup),
                "--destination-root",
                str(self.root / "mismatch"),
                "--provider-file",
                str(provider),
                "--docker-host",
                "tcp://127.0.0.1:2375",
            ]
        )
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertFalse((self.root / "mismatch").exists())

    def test_cursor_lead_rejects_and_activation_keeps_workspaces_stopped(self):
        backup = self.root / "backup"
        created = self.run_cli(
            [
                "backup",
                "--deploy-root",
                str(self.deploy_root),
                "--destination",
                str(backup),
                "--runtime-file",
                str(self.runtime),
            ]
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        dump = json.loads((backup / "database" / "openwebui.dump").read_text(encoding="utf-8"))
        dump["chat_state"][0]["last_seen_revision"] = 99
        (backup / "database" / "openwebui.dump").write_text(json.dumps(dump), encoding="utf-8")
        manifest = json.loads((backup / "recovery.json").read_text(encoding="utf-8"))
        manifest["components"]["database"]["sha256"] = hashlib.sha256(
            (backup / "database" / "openwebui.dump").read_bytes()
        ).hexdigest()
        (backup / "recovery.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        provider = self.root / "provider.env"
        provider.write_text(f"DMXAPI_API_KEY={PROVIDER_B}\n", encoding="utf-8")
        os.chmod(provider, 0o600)
        target_state = self.root / "lead-state"
        target_state.mkdir()
        seed_images(target_state)
        (target_state / "docker-info.json").write_text(json.dumps({"ID": "lead-daemon"}), encoding="utf-8")
        lead = self.run_cli(
            [
                "restore",
                "--recovery-set",
                str(backup),
                "--destination-root",
                str(self.root / "lead-root"),
                "--provider-file",
                str(provider),
            ],
            extra={"FAKE_DOCKER_STATE": str(target_state)},
        )
        self.assertNotEqual(lead.returncode, 0)
        self.assertIn("leads recovered broker counter", lead.stderr)

    def test_activation_uses_selected_up_and_leaves_sandboxes_stopped(self):
        backup = self.root / "backup"
        created = self.run_cli(
            [
                "backup",
                "--deploy-root",
                str(self.deploy_root),
                "--destination",
                str(backup),
                "--runtime-file",
                str(self.runtime),
            ]
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        provider = self.root / "provider.env"
        provider.write_text(f"DMXAPI_API_KEY={PROVIDER_B}\n", encoding="utf-8")
        os.chmod(provider, 0o600)
        target_state = self.root / "activate-state"
        target_state.mkdir()
        seed_images(target_state)
        seed_healthy_host(target_state)
        write_network(target_state, "ocu-test-private", subnet="172.30.0.0/24", gateway="172.30.0.1")
        write_network(target_state, "ocu-sandbox", subnet="172.31.0.0/24", gateway="172.31.0.1")
        write_fake_configs(target_state)
        (target_state / "docker-info.json").write_text(json.dumps({"ID": "activate-daemon"}), encoding="utf-8")
        dest = self.root / "activate-root"
        extra = {"FAKE_DOCKER_STATE": str(target_state)}
        restored = self.run_cli(
            [
                "restore",
                "--recovery-set",
                str(backup),
                "--destination-root",
                str(dest),
                "--provider-file",
                str(provider),
            ],
            extra=extra,
        )
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertFalse((dest / "source").exists())
        delivery, payload, _source = self._write_retained_delivery()
        captured = json.loads((dest / "release.json").read_text(encoding="utf-8"))
        self.assertNotEqual(captured["ocu_source_sha"], payload["ocu_source_sha"])
        activated = self.run_cli(
            [
                "activate",
                "--destination-root",
                str(dest),
                "--retained-delivery",
                str(delivery),
            ],
            extra=extra,
        )
        self.assertEqual(activated.returncode, 0, activated.stderr)
        self.assertTrue((dest / "source" / "deploy" / "up.sh").is_file())
        self.assertTrue((dest / "source" / ".git").exists())
        installed = json.loads((dest / "release.json").read_text(encoding="utf-8"))
        self.assertEqual(installed["ocu_source_sha"], payload["ocu_source_sha"])
        starts = (target_state / "starts.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(starts, ["core", "webui", "proxy"])
        executed = (target_state / "executed.json").read_text(encoding="utf-8")
        self.assertIn("--no-build", executed)
        self.assertIn("never", executed)
        containers = json.loads((target_state / "containers.json").read_text(encoding="utf-8"))
        sandboxes = [item for item in containers if str(item.get("Name", "")).startswith("owui-chat-")]
        for item in sandboxes:
            state = item.get("State")
            running = state.get("Running") if isinstance(state, dict) else str(state) == "running"
            self.assertFalse(running)

    def test_activation_refuses_mixed_identity_and_tampered_source(self):
        backup = self.root / "backup"
        created = self.run_cli(
            [
                "backup",
                "--deploy-root",
                str(self.deploy_root),
                "--destination",
                str(backup),
                "--runtime-file",
                str(self.runtime),
            ]
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        provider = self.root / "provider.env"
        provider.write_text(f"DMXAPI_API_KEY={PROVIDER_B}\n", encoding="utf-8")
        os.chmod(provider, 0o600)
        target_state = self.root / "mixed-state"
        target_state.mkdir()
        seed_images(target_state)
        seed_healthy_host(target_state)
        write_network(target_state, "ocu-test-private", subnet="172.30.0.0/24", gateway="172.30.0.1")
        write_network(target_state, "ocu-sandbox", subnet="172.31.0.0/24", gateway="172.31.0.1")
        write_fake_configs(target_state)
        (target_state / "docker-info.json").write_text(json.dumps({"ID": "mixed-daemon"}), encoding="utf-8")
        dest = self.root / "mixed-root"
        extra = {"FAKE_DOCKER_STATE": str(target_state)}
        restored = self.run_cli(
            [
                "restore",
                "--recovery-set",
                str(backup),
                "--destination-root",
                str(dest),
                "--provider-file",
                str(provider),
            ],
            extra=extra,
        )
        self.assertEqual(restored.returncode, 0, restored.stderr)
        matching, matching_payload, matching_source = self._write_retained_delivery("matching-delivery")
        other, other_payload, _other_source = self._write_retained_delivery("other-delivery")
        self.assertNotEqual(matching_payload["ocu_source_sha"], other_payload["ocu_source_sha"])
        subprocess.run(
            ["git", "clone", "--quiet", str(matching_source), str(dest / "source")],
            check=True,
            capture_output=True,
            text=True,
        )
        before_source = (dest / "source" / "deploy" / "up.sh").read_bytes()
        before_manifest = (dest / "release.json").read_bytes()
        mixed = self.run_cli(
            [
                "activate",
                "--destination-root",
                str(dest),
                "--retained-delivery",
                str(other),
            ],
            extra=extra,
        )
        self.assertNotEqual(mixed.returncode, 0)
        self.assertFalse((target_state / "starts.log").exists())
        self.assertEqual((dest / "source" / "deploy" / "up.sh").read_bytes(), before_source)
        self.assertEqual((dest / "release.json").read_bytes(), before_manifest)
        (dest / "source" / "deploy" / "up.sh").write_bytes(before_source + b"#tampered\n")
        tampered = self.run_cli(
            [
                "activate",
                "--destination-root",
                str(dest),
                "--retained-delivery",
                str(matching),
            ],
            extra=extra,
        )
        self.assertNotEqual(tampered.returncode, 0)
        self.assertFalse((target_state / "starts.log").exists())
        self.assertEqual((dest / "release.json").read_bytes(), before_manifest)

    def test_cli_help_names_tracked_commands(self):
        result = self.run_cli(["--help"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("backup", result.stdout)
        self.assertIn("restore", result.stdout)
        self.assertIn("activate", result.stdout)

    def test_environment_spoof_cannot_bypass_root_or_socket(self):
        dest = self.root / "spoof-backup"
        spoofed = self.run_cli(
            [
                "backup",
                "--deploy-root",
                str(self.deploy_root),
                "--destination",
                str(dest),
                "--runtime-file",
                str(self.runtime),
            ],
            extra={"FAKE_ID_UID": "0", "OCU_TEST_EUID": "501"},
        )
        self.assertNotEqual(spoofed.returncode, 0)
        self.assertFalse(dest.exists())
        provider = self.root / "provider.env"
        provider.write_text(f"DMXAPI_API_KEY={PROVIDER_B}\n", encoding="utf-8")
        os.chmod(provider, 0o600)
        mismatch = self.run_cli(
            [
                "restore",
                "--recovery-set",
                str(self.root / "missing"),
                "--destination-root",
                str(self.root / "mismatch"),
                "--provider-file",
                str(provider),
                "--docker-host",
                "unix:///tmp/wrong-runtime.sock",
            ],
            extra={"FAKE_DOCKER_STATE": str(self.state)},
        )
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertFalse((self.root / "mismatch").exists())

    def test_helper_copies_volume_bytes_not_host_mount_assumption(self):
        dest = self.root / "backup"
        result = self.run_cli(
            [
                "backup",
                "--deploy-root",
                str(self.deploy_root),
                "--destination",
                str(dest),
                "--runtime-file",
                str(self.runtime),
            ]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        archive = dest / "workspaces" / f"{CHAT_ID}.tar.gz"
        extracted = self.root / "helper-out"
        import recovery_fs

        recovery_fs.extract_tree(archive, extracted)
        self.assertEqual((extracted / "README.md").read_bytes(), b"sandbox-home\n")
        runs = json.loads((self.state / "runs.json").read_text(encoding="utf-8"))
        helpers = [row for row in runs if row.get("command")[:1] == ["/recovery/recovery_helper.py"]]
        self.assertTrue(helpers)
        self.assertTrue(
            any(
                "capture" in row.get("command", [])
                and f"chat-{CHAT_ID}-workspace:/source:ro" in row.get("mounts", [])
                for row in helpers
            )
        )
        self.assertTrue(
            any(any(item.endswith(":/recovery:ro") for item in row.get("mounts", [])) for row in helpers)
        )


if __name__ == "__main__":
    unittest.main()
