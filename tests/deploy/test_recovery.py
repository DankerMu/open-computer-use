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


def stack_container(name: str, *, running=True, paused=False, extra=None, image=None, service=None):
    role = next((role for role, reference in DEFAULT_RELEASE_IMAGES.items() if reference == image), None)
    image_id = "sha256:" + hashlib.sha256(config_payload(DEFAULT_RELEASE_DIGESTS[role])).hexdigest() if role else ""
    body = {
        "Id": hashlib.sha256(name.encode("utf-8")).hexdigest()[:12],
        "Name": name,
        "Image": image_id,
        "State": {
            "Status": "paused" if paused else ("running" if running else "exited"),
            "Running": running and not paused,
            "Paused": paused,
        },
        "Labels": {},
        "Config": {"Image": image or "", "Labels": {}},
        "HostConfig": {"NetworkMode": "ocu-test-private"},
        "NetworkSettings": {
            "Networks": {
                "ocu-test-private": {"NetworkID": "id-ocu-test-private", "IPAddress": "172.30.0.10"}
            }
        },
    }
    if service:
        labels = {
            "com.docker.compose.project": "ocu-test",
            "com.docker.compose.service": service,
        }
        body["Labels"] = labels
        body["Config"]["Labels"] = labels
    if extra:
        body.update(extra)
        if "Labels" in extra:
            config = body.get("Config") or {}
            config["Labels"] = extra["Labels"]
            body["Config"] = config
        if image:
            config = body.get("Config") or {}
            config["Image"] = image
            body["Config"] = config
            body["Image"] = image_id
    return body


def sandbox(name: str, chat_id: str, *, running=True, paused=False, chat_dir: Path | None = None):
    chat_root = str(chat_dir / chat_id) if chat_dir is not None else chat_id
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
            "Mounts": [
                {
                    "Type": "volume",
                    "RW": True,
                    "Name": f"chat-{chat_id}-workspace",
                    "Source": f"chat-{chat_id}-workspace",
                    "Destination": "/home/assistant",
                },
                {
                    "Type": "bind",
                    "RW": True,
                    "Source": f"{chat_root}/outputs",
                    "Destination": "/mnt/user-data/files",
                },
            ],

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
        images = DEFAULT_RELEASE_IMAGES
        containers = [
            stack_container(
                "ocu-test-computer-use-server",
                image=images["computer-use-server"],
                service="computer-use-server",
            ),
            stack_container(
                "ocu-test-retention-guard",
                image=images["retention-guard"],
                service="retention-guard",
            ),
            stack_container(
                "ocu-test-open-webui-init",
                running=False,
                image=images["open-webui"],
                service="open-webui-init",
            ),
            stack_container("ocu-test-proxy", image=images["proxy"], service="proxy"),
            stack_container(
                "ocu-test-open-webui-1",
                image=images["open-webui"],
                service="open-webui",
            ),
            stack_container(
                "ocu-test-postgres-1",
                image=images["postgres"],
                service="postgres",
            ),
            sandbox(
                f"owui-chat-{CHAT_ID}",
                CHAT_ID,
                running=running_sandbox,
                paused=paused,
                chat_dir=self.chat_dir,
            ),

        ]
        for item in containers:
            service = item["Config"]["Labels"].get("com.docker.compose.service")
            if service == "computer-use-server":
                item["Mounts"] = [
                    {"Type": "bind", "Source": str(path), "Destination": str(path), "RW": True}
                    for path in (self.chat_dir, self.skills_dir)
                ]
                item["Config"]["Env"] = [
                    f"BASE_DATA_DIR={self.chat_dir}",
                    f"SKILLS_CACHE_DIR={self.skills_dir}", f"SKILLS_CACHE_HOST_PATH={self.skills_dir}",
                ]
            elif service in ("open-webui", "open-webui-init", "postgres"):
                is_pg = service == "postgres"
                item["Mounts"] = [{
                    "Type": "volume", "RW": True,
                    "Name": "ocu-test_postgres-data" if is_pg else "ocu-test_open-webui-data",
                    "Destination": "/var/lib/postgresql/data" if is_pg else "/app/backend/data",
                }]
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
                        "chat_owners": {CHAT_ID: "owner-live"},
                        "chat_state": [
                            {
                                "chat_id": CHAT_ID,
                                "last_seen_revision": 2,
                                "prefs": {"theme": "keep|live"},
                                "updated_at": 1700000000,
                            },
                            {
                                "chat_id": ORPHAN_ID,
                                "last_seen_revision": 9,
                                "prefs": {"drop": "me"},
                                "updated_at": 1700000001,
                            },
                        ],
                        "config": [
                            {
                                "key": "openai.api_key",
                                "value": PROVIDER_A,
                                "updated_at": 1700000000,
                            }
                        ],

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

    def _write_retained_delivery(self, name="retained-delivery", images=None):
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
        selected = images or DEFAULT_RELEASE_IMAGES
        for role in ROLE_ORDER:
            tag = selected[role]
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


    def test_actual_image_id_mismatch_refuses_matching_launch_reference(self):
        path = self.state / "containers.json"
        containers = json.loads(path.read_text())
        server = next(row for row in containers if row["Name"] == "ocu-test-computer-use-server")
        self.assertEqual(server["Config"]["Image"], DEFAULT_RELEASE_IMAGES["computer-use-server"])
        server["Image"] = "sha256:" + "0" * 64
        path.write_text(json.dumps(containers))
        before = path.read_bytes()
        result = self.run_cli(["backup", "--deploy-root", str(self.deploy_root),
                               "--destination", str(self.root / "wrong-id"),
                               "--runtime-file", str(self.runtime)])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.state / "stopped.log").exists())

    def test_foreign_producer_identity_refuses_before_any_stop(self):
        original = (self.state / "containers.json").read_bytes()
        for defect in ("name", "tool", "type"):
            with self.subTest(defect=defect):
                containers = json.loads(original)
                row = next(item for item in containers if item["Name"] == f"owui-chat-{CHAT_ID}")
                if defect == "name":
                    row["Name"] += "-foreign"
                elif defect == "tool":
                    row["Labels"]["tool"] = "other-producer"
                    row["Config"]["Labels"]["tool"] = "other-producer"
                else:
                    row["Mounts"][0]["Type"] = "bind"
                path = self.state / "containers.json"
                path.write_text(json.dumps(containers))
                before = path.read_bytes()
                result = self.run_cli(["backup", "--deploy-root", str(self.deploy_root),
                                       "--destination", str(self.root / f"foreign-{defect}"),
                                       "--runtime-file", str(self.runtime)])
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse((self.state / "stopped.log").exists())

    def test_stack_data_provenance_refuses_before_stopping(self):
        path = self.state / "containers.json"
        containers = [row for row in json.loads(path.read_text())
                      if not row["Name"].startswith("owui-chat-")]
        volumes_path = self.state / "volumes.json"
        volumes = json.loads(volumes_path.read_text())
        volumes.pop(f"chat-{CHAT_ID}-workspace")
        volumes_path.write_text(json.dumps(volumes))
        original_runtime = self.runtime.read_text()
        wrong_chat = self.deploy_root / "data" / "inactive-chat"
        wrong_skills = self.deploy_root / "data" / "inactive-skills"
        wrong_chat.mkdir()
        wrong_skills.mkdir()
        for defect in ("stale-roots", "environment", "missing-chat", "duplicate-skills",
                       "wrong-type", "read-only", "missing-webui", "missing-postgres"):
            with self.subTest(defect=defect):
                records = json.loads(json.dumps(containers))
                server = next(row for row in records if row["Name"] == "ocu-test-computer-use-server")
                self.runtime.write_text(original_runtime)
                if defect == "stale-roots":
                    self.runtime.write_text(original_runtime.replace(str(self.chat_dir), str(wrong_chat))
                                            .replace(str(self.skills_dir), str(wrong_skills)))
                    server["Config"]["Env"] = [
                        value.replace(str(self.chat_dir), str(wrong_chat))
                             .replace(str(self.skills_dir), str(wrong_skills))
                        for value in server["Config"]["Env"]
                    ]
                elif defect == "environment":
                    server["Config"]["Env"][0] = f"BASE_DATA_DIR={wrong_chat}"
                elif defect == "missing-chat":
                    server["Mounts"].pop(0)
                elif defect == "duplicate-skills":
                    server["Mounts"].append(dict(server["Mounts"][1]))
                elif defect == "wrong-type":
                    server["Mounts"][0]["Type"] = "volume"
                elif defect == "read-only":
                    server["Mounts"][0]["RW"] = False
                else:
                    name = "ocu-test-open-webui-1" if defect == "missing-webui" else "ocu-test-postgres-1"
                    next(row for row in records if row["Name"] == name)["Mounts"] = []
                path.write_text(json.dumps(records))
                before = path.read_bytes()
                dest = self.root / f"bad-provenance-{defect}"
                result = self.run_cli(["backup", "--deploy-root", str(self.deploy_root),
                                       "--destination", str(dest), "--runtime-file", str(self.runtime)])
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(dest.exists())
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse((self.state / "stopped.log").exists())
        self.runtime.write_text(original_runtime)
        path.write_text(json.dumps(containers))
        result = self.run_cli(["backup", "--deploy-root", str(self.deploy_root),
                               "--destination", str(self.root / "matching-data"),
                               "--runtime-file", str(self.runtime)])
        self.assertEqual(result.returncode, 0, result.stderr)

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
        dest = self.root / "backup-running"
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

    def test_detached_volume_is_captured_without_sandbox_container(self):
        self._seed_volumes_and_db(running_sandbox=False)
        containers = json.loads((self.state / "containers.json").read_text(encoding="utf-8"))
        containers = [item for item in containers if not str(item.get("Name", "")).startswith("owui-chat-")]
        (self.state / "containers.json").write_text(json.dumps(containers), encoding="utf-8")
        dest = self.root / "backup-detached"
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
        self.assertTrue(archive.exists())
        extracted = self.root / "detached-out"
        import recovery_fs

        recovery_fs.extract_tree(archive, extracted)
        self.assertEqual((extracted / "README.md").read_bytes(), b"sandbox-home\n")
        ops = (self.state / "ops.log").read_text(encoding="utf-8")
        self.assertNotIn("docker start", ops)
        self.assertNotIn("owui-chat-" + CHAT_ID, json.dumps(json.loads((self.state / "containers.json").read_text(encoding="utf-8"))))

    def test_empty_and_unlaunched_chats_are_valid_complete_backups(self):
        shutil.rmtree(self.chat_dir)
        self.chat_dir.mkdir()
        volumes = json.loads((self.state / "volumes.json").read_text(encoding="utf-8"))
        volumes.pop(f"chat-{CHAT_ID}-workspace", None)
        (self.state / "volumes.json").write_text(json.dumps(volumes), encoding="utf-8")
        containers = [
            item
            for item in json.loads((self.state / "containers.json").read_text(encoding="utf-8"))
            if not str(item.get("Name", "")).startswith("owui-chat-")
        ]
        (self.state / "containers.json").write_text(json.dumps(containers), encoding="utf-8")
        dest = self.root / "backup-empty"
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
        self.assertEqual(manifest["components"]["workspaces"], {})
        self.assertTrue((dest / "recovery.json").exists())

        chat_only = self.chat_dir / "chat-unlaunched"
        chat_only.mkdir()
        dest2 = self.root / "backup-unlaunched"
        result = self.run_cli(
            [
                "backup",
                "--deploy-root",
                str(self.deploy_root),
                "--destination",
                str(dest2),
                "--runtime-file",
                str(self.runtime),
            ]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((dest2 / "recovery.json").read_text(encoding="utf-8"))
        self.assertIn("chat-unlaunched", manifest["chats"])
        self.assertEqual(manifest["components"]["workspaces"], {})


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
        restored_db = next(iter(db.values()))
        state_rows = restored_db["chat_state"]
        self.assertEqual([row["chat_id"] for row in state_rows], [CHAT_ID])
        self.assertEqual(state_rows[0]["prefs"], {"theme": "keep|live"})

        self.assertEqual(state_rows[0]["updated_at"], 1700000000)
        self.assertEqual(restored_db["chat_owners"][CHAT_ID], "owner-live")
        self.assertNotIn("owner_id", state_rows[0])
        self.assertNotIn("file_ids", state_rows[0])
        self.assertEqual(
            restored_db["config"],
            [
                {
                    "key": "openai.api_key",
                    "value": PROVIDER_A,
                    "updated_at": 1700000000,
                }
            ],
        )

        self.assertNotIn(PROVIDER_B, restored.stdout + restored.stderr)
        self.assertTrue(
            any(name.startswith("ocu-test-postgres-restore-") for name in db),
        )
        leftover = [
            item.get("Name")
            for item in json.loads((target_state / "containers.json").read_text(encoding="utf-8"))
            if str(item.get("Name", "")).startswith("ocu-test-postgres-restore-")
        ]
        self.assertEqual(leftover, [])
        runtime = (dest / "config" / "runtime.env").read_text(encoding="utf-8")
        self.assertIn(f"DMX_ENV_FILE={provider}", runtime)
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


    def test_mixed_component_membership_rejects_before_allocation(self):
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
        manifest = json.loads((backup / "recovery.json").read_text(encoding="utf-8"))
        chat = manifest["components"]["chat-data"]
        manifest["components"]["skills-cache"] = {
            "path": chat["path"],
            "sha256": chat["sha256"],
        }
        (backup / "skills-cache.tar.gz").unlink()
        (backup / "recovery.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        provider = self.root / "provider.env"
        provider.write_text(f"DMXAPI_API_KEY={PROVIDER_B}\n", encoding="utf-8")
        os.chmod(provider, 0o600)
        dest = self.root / "mixed-root"
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

    def test_nested_marker_rejects_before_allocation(self):
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
        nested = self.root / "nested-webui"
        nested.mkdir()
        (nested / "lookalike").mkdir()
        (nested / "lookalike" / ".computer-use-initialized").write_bytes(b"1\n")
        import recovery_fs

        recovery_fs.capture_tree(nested, backup / "webui-data.tar.gz")
        manifest = json.loads((backup / "recovery.json").read_text(encoding="utf-8"))
        manifest["components"]["webui-data"]["sha256"] = hashlib.sha256(
            (backup / "webui-data.tar.gz").read_bytes()
        ).hexdigest()
        (backup / "recovery.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        provider = self.root / "provider.env"
        provider.write_text(f"DMXAPI_API_KEY={PROVIDER_B}\n", encoding="utf-8")
        os.chmod(provider, 0o600)
        dest = self.root / "nested-marker-root"
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

    def test_unrelated_sandbox_is_not_stopped(self):
        extra = sandbox("owui-chat-foreign", "chat-foreign")
        extra["Mounts"] = [
            {
                "Name": "chat-foreign-workspace",
                "Source": "chat-foreign-workspace",
                "Destination": "/home/assistant",
            }
        ]
        self._seed_volumes_and_db(extra_containers=[extra])
        dest = self.root / "backup-foreign"
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
        containers = json.loads((self.state / "containers.json").read_text(encoding="utf-8"))
        foreign = next(item for item in containers if item["Name"] == "owui-chat-foreign")
        self.assertTrue(foreign["State"]["Running"])
        stopped = (self.state / "stopped.log").read_text(encoding="utf-8") if (self.state / "stopped.log").exists() else ""
        self.assertNotIn(foreign["Id"], stopped)

    def test_same_chat_without_mounts_is_not_attributed(self):
        self._seed_volumes_and_db()
        path = self.state / "containers.json"
        containers = json.loads(path.read_text(encoding="utf-8"))
        canonical = next(item for item in containers if item["Name"] == f"owui-chat-{CHAT_ID}")
        canonical["Mounts"] = []
        path.write_text(json.dumps(containers), encoding="utf-8")
        dest = self.root / "backup-no-mounts"
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
        containers = json.loads((self.state / "containers.json").read_text(encoding="utf-8"))
        sandbox_row = next(item for item in containers if item["Name"] == f"owui-chat-{CHAT_ID}")
        self.assertTrue(sandbox_row["State"]["Running"])
        stopped = (self.state / "stopped.log").read_text(encoding="utf-8") if (self.state / "stopped.log").exists() else ""
        self.assertNotIn(sandbox_row["Id"], stopped)

    def test_legacy_uploads_outputs_binds_are_not_attributed(self):
        self._seed_volumes_and_db()
        path = self.state / "containers.json"
        containers = json.loads(path.read_text(encoding="utf-8"))
        canonical = next(item for item in containers if item["Name"] == f"owui-chat-{CHAT_ID}")
        chat_root = str(self.chat_dir / CHAT_ID)
        canonical["Mounts"] = [
            {
                "Type": "volume",
                "RW": True,
                "Name": f"chat-{CHAT_ID}-workspace",
                "Source": f"chat-{CHAT_ID}-workspace",
                "Destination": "/home/assistant",
            },
            {
                "Type": "bind",
                "RW": False,
                "Source": f"{chat_root}/uploads",
                "Destination": "/mnt/user-data/uploads",
            },
            {
                "Type": "bind",
                "RW": True,
                "Source": f"{chat_root}/outputs",
                "Destination": "/mnt/user-data/outputs",
            },
        ]
        path.write_text(json.dumps(containers), encoding="utf-8")
        dest = self.root / "backup-legacy-binds"
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
        containers = json.loads((self.state / "containers.json").read_text(encoding="utf-8"))
        sandbox_row = next(item for item in containers if item["Name"] == f"owui-chat-{CHAT_ID}")
        self.assertTrue(sandbox_row["State"]["Running"])
        stopped = (self.state / "stopped.log").read_text(encoding="utf-8") if (self.state / "stopped.log").exists() else ""
        self.assertNotIn(sandbox_row["Id"], stopped)



    def test_swapped_workspace_archives_reject_before_allocation(self):
        other = "chat-other"
        self._volume(f"chat-{other}-workspace", {"README.md": b"other-home\n"})
        extra = sandbox(f"owui-chat-{other}", other, running=False, chat_dir=self.chat_dir)
        (self.chat_dir / other / "uploads").mkdir(parents=True)
        (self.chat_dir / other / "outputs").mkdir(parents=True)
        self._seed_volumes_and_db(running_sandbox=False, extra_containers=[extra])
        backup = self.root / "two-chat-backup"
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
        manifest = json.loads((backup / "recovery.json").read_text(encoding="utf-8"))
        workspaces = manifest["components"]["workspaces"]

        first, second = sorted(workspaces)
        workspaces[first]["path"], workspaces[second]["path"] = (
            workspaces[second]["path"],
            workspaces[first]["path"],
        )
        workspaces[first]["sha256"], workspaces[second]["sha256"] = (
            workspaces[second]["sha256"],
            workspaces[first]["sha256"],
        )
        (backup / "recovery.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        provider = self.root / "provider.env"
        provider.write_text(f"DMXAPI_API_KEY={PROVIDER_B}\n", encoding="utf-8")
        os.chmod(provider, 0o600)
        target_state = self.root / "swapped-state"
        target_state.mkdir()
        seed_images(target_state)
        seed_healthy_host(target_state)
        write_network(target_state, "ocu-test-private", subnet="172.30.0.0/24", gateway="172.30.0.1")
        write_network(target_state, "ocu-sandbox", subnet="172.31.0.0/24", gateway="172.31.0.1")
        write_fake_configs(target_state)
        (target_state / "docker-info.json").write_text(json.dumps({"ID": "swapped-daemon"}), encoding="utf-8")
        dest = self.root / "swapped-root"
        result = self.run_cli(
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
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("declared recovery component", result.stderr)
        self.assertFalse(dest.exists())
        self.assertFalse((target_state / "volumes.json").exists())
        self.assertFalse((target_state / "postgres.json").exists())
        self.assertFalse(list(target_state.glob("postgres-phase-*")))



    def test_two_chat_restore_keeps_each_workspace_home(self):
        other = "chat-other"
        self._volume(f"chat-{other}-workspace", {"README.md": b"other-home\n"})
        extra = sandbox(f"owui-chat-{other}", other, running=False, chat_dir=self.chat_dir)
        (self.chat_dir / other / "uploads").mkdir(parents=True)
        (self.chat_dir / other / "outputs").mkdir(parents=True)
        self._seed_volumes_and_db(running_sandbox=False, extra_containers=[extra])
        backup = self.root / "keep-two-backup"
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
        target_state = self.root / "keep-two-state"
        target_state.mkdir()
        seed_images(target_state)
        seed_healthy_host(target_state)
        write_network(target_state, "ocu-test-private", subnet="172.30.0.0/24", gateway="172.30.0.1")
        write_network(target_state, "ocu-sandbox", subnet="172.31.0.0/24", gateway="172.31.0.1")
        write_fake_configs(target_state)
        (target_state / "docker-info.json").write_text(json.dumps({"ID": "keep-two-daemon"}), encoding="utf-8")
        dest = self.root / "keep-two-root"
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
        import recovery_fs

        first = self.root / "first-home"
        second = self.root / "second-home"
        recovery_fs.extract_tree(backup / "workspaces" / f"{CHAT_ID}.tar.gz", first)
        recovery_fs.extract_tree(backup / "workspaces" / f"{other}.tar.gz", second)
        self.assertEqual((first / "README.md").read_bytes(), b"sandbox-home\n")
        self.assertEqual((second / "README.md").read_bytes(), b"other-home\n")


    def test_stale_runtime_images_fail_before_capture(self):
        containers = json.loads((self.state / "containers.json").read_text(encoding="utf-8"))
        for item in containers:
            if item["Name"] == "ocu-test-computer-use-server":
                item["Image"] = "open-computer-use:stale-a"
                item["Config"]["Image"] = "open-computer-use:stale-a"
        (self.state / "containers.json").write_text(json.dumps(containers), encoding="utf-8")
        dest = self.root / "backup-stale"
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

    def test_external_data_roots_reject_before_allocation(self):
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
        runtime = (backup / "runtime.env").read_text(encoding="utf-8")
        runtime = runtime.replace(
            f"OCU_CHAT_DATA_DIR={self.chat_dir}",
            "OCU_CHAT_DATA_DIR=/srv/ocu-chats",
        )
        (backup / "runtime.env").write_text(runtime, encoding="utf-8")
        manifest = json.loads((backup / "recovery.json").read_text(encoding="utf-8"))
        manifest["components"]["runtime-config"]["sha256"] = hashlib.sha256(
            (backup / "runtime.env").read_bytes()
        ).hexdigest()
        (backup / "recovery.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        provider = self.root / "provider.env"
        provider.write_text(f"DMXAPI_API_KEY={PROVIDER_B}\n", encoding="utf-8")
        os.chmod(provider, 0o600)
        dest = self.root / "external-root"
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

    def test_preferences_only_chat_without_index_restores(self):
        live = self.chat_dir / CHAT_ID
        shutil.rmtree(live / ".ocu", ignore_errors=True)
        shutil.rmtree(live / "outputs", ignore_errors=True)
        (live / "outputs").mkdir()
        db = json.loads((self.state / "postgres.json").read_text(encoding="utf-8"))
        db["ocu-test-postgres-1"]["chat_state"][0]["last_seen_revision"] = 0
        (self.state / "postgres.json").write_text(json.dumps(db), encoding="utf-8")
        backup = self.root / "backup-prefs"
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
        target_state = self.root / "prefs-state"
        target_state.mkdir()
        seed_images(target_state)
        (target_state / "docker-info.json").write_text(json.dumps({"ID": "prefs-daemon"}), encoding="utf-8")
        dest = self.root / "prefs-root"
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
        self.assertFalse((dest / "data" / "chat" / CHAT_ID / ".ocu" / "index.json").exists())
        rows = next(iter(json.loads((target_state / "postgres.json").read_text(encoding="utf-8")).values()))["chat_state"]
        self.assertEqual(rows[0]["last_seen_revision"], 0)
        self.assertEqual(rows[0]["prefs"], {"theme": "keep|live"})


    def test_postgres_env_file_is_private_at_creation(self):
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
        target_state = self.root / "private-state"
        target_state.mkdir()
        seed_images(target_state)
        (target_state / "docker-info.json").write_text(json.dumps({"ID": "private-daemon"}), encoding="utf-8")
        dest = self.root / "private-root"
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
            extra={"FAKE_DOCKER_STATE": str(target_state), "FAKE_POSTGRES_READY_AFTER": "0"},
        )
        self.assertEqual(restored.returncode, 0, restored.stderr)
        db = json.loads((target_state / "postgres.json").read_text(encoding="utf-8"))
        modes = [record.get("env_file_mode") for record in db.values()]
        self.assertTrue(any(mode == "0o600" for mode in modes), modes)

    def test_failed_isolated_postgres_stop_does_not_publish_restored(self):
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
        target_state = self.root / "stop-fail-state"
        target_state.mkdir()
        seed_images(target_state)
        (target_state / "docker-info.json").write_text(json.dumps({"ID": "stop-fail-daemon"}), encoding="utf-8")
        dest = self.root / "stop-fail-root"
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
            extra={"FAKE_DOCKER_STATE": str(target_state), "FAKE_DOCKER_STOP_FAIL_PREFIX": "ocu-test-postgres-restore-"},
        )
        self.assertNotEqual(restored.returncode, 0)
        self.assertFalse((dest / ".restored").exists())


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
        foreign_root = dest.parent / f"{dest.name}.selected-source"
        independently_built = self.root / "foreign-selection"
        independently_built.mkdir()
        shutil.copytree(_source, independently_built / "source")
        altered = json.loads(json.dumps(payload))
        altered["images"]["workspace"]["configuration_digest"] = "sha256:" + "0" * 64
        before_manifest = (dest / "release.json").read_bytes()
        before_runtime = (dest / "config" / "runtime.env").read_bytes()
        for linked, foreign_inventory in ((False, payload), (False, altered), (True, altered)):
            write_inventory(independently_built / "release.json", foreign_inventory)
            if linked:
                foreign_root.symlink_to(independently_built, target_is_directory=True)
            else:
                shutil.copytree(independently_built, foreign_root)
            refused = self.run_cli(["activate", "--destination-root", str(dest),
                                    "--retained-delivery", str(delivery)], extra=extra)
            self.assertNotEqual(refused.returncode, 0)
            self.assertFalse((dest / "source").exists())
            self.assertEqual((dest / "release.json").read_bytes(), before_manifest)
            self.assertEqual((dest / "config" / "runtime.env").read_bytes(), before_runtime)
            self.assertFalse((target_state / "starts.log").exists())
            self.assertEqual(json.loads((foreign_root / "release.json").read_text()), foreign_inventory)
            self.assertFalse((dest / "config" / "selected-source-owner.json").exists())
            if linked:
                foreign_root.unlink()
            else:
                shutil.rmtree(foreign_root)
        (dest / "source").symlink_to(independently_built / "source", target_is_directory=True)
        refused = self.run_cli(["activate", "--destination-root", str(dest),
                                "--retained-delivery", str(delivery)], extra=extra)
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual((dest / "release.json").read_bytes(), before_manifest)
        self.assertEqual((dest / "config" / "runtime.env").read_bytes(), before_runtime)
        self.assertFalse((dest / "config" / "selected-source-owner.json").exists())
        self.assertEqual((dest / "source").resolve(), (independently_built / "source").resolve())
        (dest / "source").unlink()
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
        persisted = {}
        for line in (dest / "config" / "runtime.env").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                name, value = line.split("=", 1)
                persisted[name] = value
        self.assertEqual(persisted["SOURCE_SHA"], payload["ocu_source_sha"])
        self.assertEqual(persisted["OCU_RELEASE_MANIFEST"], str(dest / "release.json"))
        self.assertEqual(persisted["DOCKER_IMAGE"], payload["images"]["workspace"]["reference"])
        self.assertEqual(persisted["OPENWEBUI_IMAGE"], payload["images"]["open-webui"]["reference"])
        self.assertEqual(persisted.get("DMX_ENV_FILE"), str(provider))
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

    def test_incompatible_activation_leaves_compatible_retry(self):
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
        target_state = self.root / "compat-state"
        target_state.mkdir()
        seed_images(target_state)
        seed_healthy_host(target_state)
        write_network(target_state, "ocu-test-private", subnet="172.30.0.0/24", gateway="172.30.0.1")
        write_network(target_state, "ocu-sandbox", subnet="172.31.0.0/24", gateway="172.31.0.1")
        write_fake_configs(target_state)
        (target_state / "docker-info.json").write_text(json.dumps({"ID": "compat-daemon"}), encoding="utf-8")
        dest = self.root / "compat-root"
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
        matching, matching_payload, _matching_source = self._write_retained_delivery("matching-b")
        other, other_payload, _other_source = self._write_retained_delivery("incompatible-c")
        self.assertNotEqual(matching_payload["ocu_source_sha"], other_payload["ocu_source_sha"])
        before_manifest = (dest / "release.json").read_bytes()
        before_runtime = (dest / "config" / "runtime.env").read_bytes()
        refused = self.run_cli(
            [
                "activate",
                "--destination-root",
                str(dest),
                "--retained-delivery",
                str(other),
            ],
            extra={**extra, "FAKE_DOCKER_MIGRATION_HEADS": "deadbeefc0de"},
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertFalse((target_state / "starts.log").exists())
        self.assertFalse((dest / "source").exists())
        self.assertEqual((dest / "release.json").read_bytes(), before_manifest)
        self.assertEqual((dest / "config" / "runtime.env").read_bytes(), before_runtime)
        original_bundle = (other / "source.bundle").read_bytes()
        original_inventory = (other / "release.json").read_bytes()
        for invalid_kind in ("corrupt", "wrong-commit"):
            if invalid_kind == "corrupt":
                (other / "source.bundle").write_bytes(b"not a git bundle")
            else:
                (other / "source.bundle").write_bytes(original_bundle)
                wrong = json.loads(original_inventory)
                wrong["ocu_source_sha"] = matching_payload["ocu_source_sha"]
                write_inventory(other / "release.json", wrong)
            rejected = self.run_cli(
                ["activate", "--destination-root", str(dest), "--retained-delivery", str(other)],
                extra=extra,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertFalse((dest / "config" / "selected-source-owner.json").exists())
            self.assertFalse((dest / "source").exists())
            self.assertFalse((target_state / "starts.log").exists())
            self.assertEqual((dest / "release.json").read_bytes(), before_manifest)
            self.assertEqual((dest / "config" / "runtime.env").read_bytes(), before_runtime)
        (target_state / "migration-graph.json").write_text(json.dumps({
            "heads": ["newhead"], "revisions": {
                "newhead": ["e6f7a8b9c0d1"], "e6f7a8b9c0d1": ["d4c1a8e37b62"],
                "d4c1a8e37b62": [],
            },
        }))
        activated = self.run_cli(
            [
                "activate",
                "--destination-root",
                str(dest),
                "--retained-delivery",
                str(matching),
            ],
            extra=extra,
        )
        self.assertEqual(activated.returncode, 0, activated.stderr)
        installed = json.loads((dest / "release.json").read_text(encoding="utf-8"))
        self.assertEqual(installed["ocu_source_sha"], matching_payload["ocu_source_sha"])
        persisted = {}
        for line in (dest / "config" / "runtime.env").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                name, value = line.split("=", 1)
                persisted[name] = value
        self.assertEqual(persisted["SOURCE_SHA"], matching_payload["ocu_source_sha"])
        self.assertTrue((dest / "DEPLOYED_VERSION.md").is_file())
        self.assertIn(matching_payload["ocu_source_sha"], (dest / "DEPLOYED_VERSION.md").read_text(encoding="utf-8"))

    def test_interrupted_selected_source_publication_retries_same_delivery(self):
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
        target_state = self.root / "interrupt-state"
        target_state.mkdir()
        seed_images(target_state)
        seed_healthy_host(target_state)
        write_network(target_state, "ocu-test-private", subnet="172.30.0.0/24", gateway="172.30.0.1")
        write_network(target_state, "ocu-sandbox", subnet="172.31.0.0/24", gateway="172.31.0.1")
        write_fake_configs(target_state)
        (target_state / "docker-info.json").write_text(json.dumps({"ID": "interrupt-daemon"}), encoding="utf-8")
        dest = self.root / "interrupt-root"
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
        delivery, inventory, _source = self._write_retained_delivery("interrupt-delivery")
        package = self.recovery_pkg / "recovery_resources.py"
        original = package.read_text(encoding="utf-8")
        after_import = '        release.import_release(delivery=delivery, install_root=source_root, recovery_owner=owner)'
        after_symlink = '        os.symlink(source_root / "source", source)'
        after_inventory = "    _publish_selected_inventory(manifest, source_root)"
        after_runtime = "        env = activation_env(published, inventory, destination_root, provider)"
        after_version = '        record = destination_root / "DEPLOYED_VERSION.md"'
        foreign = dest.parent / "foreign-keep"
        foreign.mkdir()
        (foreign / "keep.txt").write_text("untouched\n", encoding="utf-8")
        before_manifest = (dest / "release.json").read_bytes()
        before_runtime = (dest / "config" / "runtime.env").read_bytes()
        selected_root = dest.parent / f"{dest.name}.selected-source"
        for needle, injection in (
            (after_import, after_import + '\n        raise RecoveryError("injected interruption after source-root")'),
            (after_symlink, after_symlink + '\n        raise RecoveryError("injected interruption after source publication")'),
            (after_inventory, after_inventory + '\n    raise RecoveryError("injected interruption after inventory publication")'),
            (after_runtime, '        raise RecoveryError("injected interruption after runtime publication")\n' + after_runtime),
            (after_version, '        raise RecoveryError("injected interruption after version publication")\n' + after_version),
        ):
            self.assertIn(needle, original)
            if (dest / "source").exists() or (dest / "source").is_symlink():
                (dest / "source").unlink()
            if selected_root.exists():
                shutil.rmtree(selected_root)
            (dest / "release.json").write_bytes(before_manifest)
            (dest / "config" / "runtime.env").write_bytes(before_runtime)
            (dest / "DEPLOYED_VERSION.md").unlink(missing_ok=True)
            (target_state / "starts.log").unlink(missing_ok=True)
            package.write_text(original.replace(needle, injection, 1), encoding="utf-8")
            try:
                interrupted = self.run_cli(
                    ["activate", "--destination-root", str(dest),
                     "--retained-delivery", str(delivery)],
                    extra=extra,
                )
            finally:
                package.write_text(original, encoding="utf-8")
            self.assertNotEqual(interrupted.returncode, 0, injection)
            if needle != after_version:
                self.assertFalse((target_state / "starts.log").exists())
            self.assertEqual((foreign / "keep.txt").read_text(encoding="utf-8"), "untouched\n")
            original_inventory = (selected_root / "release.json").read_bytes()
            tampered = json.loads(original_inventory)
            tampered["images"]["workspace"]["configuration_digest"] = "sha256:" + "0" * 64
            (selected_root / "release.json").write_text(json.dumps(tampered))
            target_inventory = (dest / "release.json").read_bytes()
            target_runtime = (dest / "config" / "runtime.env").read_bytes()
            refused = self.run_cli(["activate", "--destination-root", str(dest),
                                    "--retained-delivery", str(delivery)], extra=extra)
            self.assertNotEqual(refused.returncode, 0)
            self.assertEqual((dest / "release.json").read_bytes(), target_inventory)
            self.assertEqual((dest / "config" / "runtime.env").read_bytes(), target_runtime)
            (selected_root / "release.json").write_bytes(original_inventory)
            retry = self.run_cli(
                [
                    "activate",
                    "--destination-root",
                    str(dest),
                    "--retained-delivery",
                    str(delivery),
                ],
                extra=extra,
            )
            self.assertEqual(retry.returncode, 0, retry.stderr)
            installed = json.loads((dest / "release.json").read_text(encoding="utf-8"))
            self.assertEqual(installed, inventory)
            persisted = {}
            for line in (dest / "config" / "runtime.env").read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.startswith("#"):
                    name, value = line.split("=", 1)
                    persisted[name] = value
            self.assertEqual(persisted["SOURCE_SHA"], inventory["ocu_source_sha"])
            self.assertTrue((dest / "DEPLOYED_VERSION.md").is_file())
            self.assertIn(inventory["ocu_source_sha"], (dest / "DEPLOYED_VERSION.md").read_text())
            actual_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=dest / "source", text=True
            ).strip()
            self.assertEqual(actual_sha, inventory["ocu_source_sha"])
        self.assertEqual((foreign / "keep.txt").read_text(encoding="utf-8"), "untouched\n")


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

    def test_restore_binds_selected_release_images_before_allocation(self):
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
        previous = {
            role: f"{DEFAULT_RELEASE_IMAGES[role]}-previous"
            for role in ROLE_ORDER
        }
        delivery, payload, _source = self._write_retained_delivery("previous-b", images=previous)
        target_state = self.root / "selected-state"
        target_state.mkdir()
        mapping = {}
        for role in ROLE_ORDER:
            config = config_payload(DEFAULT_RELEASE_DIGESTS[role])
            mapping[previous[role]] = {
                "Id": "sha256:" + hashlib.sha256(config).hexdigest(),
                "Os": "linux",
                "Architecture": "amd64",
                "ConfigBytes": config.decode("utf-8"),
            }
        seed_images(target_state, mapping)
        (target_state / "docker-info.json").write_text(
            json.dumps({"ID": "selected-daemon"}), encoding="utf-8"
        )
        dest = self.root / "selected-root"
        restored = self.run_cli(
            [
                "restore",
                "--recovery-set",
                str(backup),
                "--destination-root",
                str(dest),
                "--provider-file",
                str(provider),
                "--retained-delivery",
                str(delivery),
            ],
            extra={"FAKE_DOCKER_STATE": str(target_state)},
        )
        self.assertEqual(restored.returncode, 0, restored.stderr)
        runtime = {}
        for line in (dest / "config" / "runtime.env").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                name, value = line.split("=", 1)
                runtime[name] = value
        self.assertEqual(runtime["SOURCE_SHA"], payload["ocu_source_sha"])
        self.assertEqual(runtime["DOCKER_IMAGE"], previous["workspace"])
        self.assertEqual(runtime["OPENWEBUI_IMAGE"], previous["open-webui"])
        self.assertNotEqual(runtime["DOCKER_IMAGE"], DEFAULT_RELEASE_IMAGES["workspace"])
        self.assertTrue((dest / ".restored").exists())

    def test_restore_waits_through_temporary_postgres_socket(self):
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
        target_state = self.root / "pg-phase-state"
        target_state.mkdir()
        seed_images(target_state)
        seed_healthy_host(target_state)
        write_network(target_state, "ocu-test-private", subnet="172.30.0.0/24", gateway="172.30.0.1")
        write_network(target_state, "ocu-sandbox", subnet="172.31.0.0/24", gateway="172.31.0.1")
        write_fake_configs(target_state)
        (target_state / "docker-info.json").write_text(json.dumps({"ID": "pg-phase-daemon"}), encoding="utf-8")
        dest = self.root / "pg-phase-root"
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
            extra={
                "FAKE_DOCKER_STATE": str(target_state),
                "FAKE_POSTGRES_READY_AFTER": "0.3",
            },
        )
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertTrue((dest / ".restored").exists())





if __name__ == "__main__":
    unittest.main()
