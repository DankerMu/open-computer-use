# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Cold backup, empty-target restore, and previous-release activation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import signal
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import unittest
from test_deployment_smoke import wait_for

from support import (
    DEFAULT_RELEASE_DIGESTS,
    DEFAULT_RELEASE_IMAGES,
    FAKE_DOCKER,
    FONT_FILES,
    ROLE_ORDER,
    ROOT,
    UP_FIXTURE_PATHS,
    WEBUI_SYNTHETIC_SHA,
    config_payload,
    copy_tracked,
    font_mount_docs,
    git_init_commit,
    ops,
    prepare_up_context,
    seed_healthy_host,
    seed_images,
    synthetic_inventory,
    tmp_dir,
    write_fake_configs,
    write_font_bundle,
    write_font_pin,
    write_installed_fonts,
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
        self.env.pop("OCU_OFFICE_BACKUP_TIMEOUT_SECONDS", None)
        self.env.pop("FAKE_DOCKER_HOLD_OFFICE_SHUTDOWN", None)
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
        for name in ("OCU_OFFICE_JWT_SECRET", "OCU_OFFICE_DOCSERVER_URL",
                     "OCU_OFFICE_DOCSERVER_ORIGIN", "OCU_OFFICE_SELF_URL",
                     "OCU_OFFICE_PROXY_PORT", "OCU_OFFICE_FONTS_DIR", "ENABLE_OCU_OFFICE_EDIT"):
            payload[name] = self.env[name]
        for role, name in (
            ("workspace", "DOCKER_IMAGE"),
            ("computer-use-server", "COMPUTER_USE_SERVER_IMAGE"),
            ("retention-guard", "RETENTION_GUARD_IMAGE"),
            ("proxy", "OCU_PROXY_IMAGE"),
            ("open-webui", "OPENWEBUI_IMAGE"),
            ("postgres", "POSTGRES_IMAGE"),
            ("documentserver", "DOCUMENTSERVER_IMAGE"),
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
        (live / "outputs").mkdir(parents=True)
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
                "docker_manager.save_container_meta(sys.argv[2], 'owner@fixture.test', 'owner', '')\n"
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

    def _seed_office(self, session_state="closed", journal=None, content=b"office-version",
                     parent=None, chat_id=CHAT_ID):
        program = """
import json, sys
import docker_manager
from office.store import OfficeStore
def forbidden_client():
    raise AssertionError('Office fixture must not contact Docker')
docker_manager.get_docker_client = forbidden_client
store = OfficeStore()
store.store_version(sys.argv[1], sys.argv[2], bytes.fromhex(sys.argv[5]),
                    source='close', parent=json.loads(sys.argv[6]), published=True, min_free_bytes=0)
def seed(state):
    state['sessions']['session-save'] = {'session_id': 'session-save', 'state': sys.argv[3]}
    state['journal'] = json.loads(sys.argv[4])
store.update(sys.argv[1], seed)
"""
        subprocess.run(
            [sys.executable, "-c", program, chat_id, self.file_id, session_state,
             json.dumps(journal or {}), content.hex(), json.dumps(parent)],
            env={**self.env, "BASE_DATA_DIR": str(self.chat_dir),
                 "PYTHONPATH": str(ROOT / "computer-use-server")},
            capture_output=True, text=True, check=True,
        )
        office = self.chat_dir / chat_id / ".ocu/office"
        return office / "state.json", office / "versions" / hashlib.sha256(content).hexdigest()

    def _read_restored_epoch(self, chat_root):
        result = subprocess.run(
            [sys.executable, "-c",
             "import json; from office.epoch import current_epoch; print(json.dumps(current_epoch()))"],
            env={**self.env, "BASE_DATA_DIR": str(chat_root),
                 "PYTHONPATH": str(ROOT / "computer-use-server")},
            capture_output=True, text=True, check=True,
        )
        return json.loads(result.stdout)

    def _seed_restore_target(self, name):
        target = self.root / name
        target.mkdir()
        seed_images(target)
        seed_healthy_host(target)
        write_network(target, "ocu-test-private", subnet="172.30.0.0/24", gateway="172.30.0.1")
        write_network(target, "ocu-sandbox", subnet="172.31.0.0/24", gateway="172.31.0.1")
        write_fake_configs(target)
        (target / "docker-info.json").write_text(json.dumps({"ID": name}))
        return target

    def _observe_office_wait(self, *, expire=False, elapsed_step=900):
        observer = f"""
import json, time as real_time
from pathlib import Path
from types import SimpleNamespace
import recovery_resources
clock = [0.0]
def observed_sleep(seconds):
    assert 0 < seconds <= 1, seconds
    clock[0] += {elapsed_step!r} if {expire!r} else seconds
    Path({str(self.state / 'office-poll.json')!r}).write_text(json.dumps({{'elapsed': clock[0], 'sleep': seconds}}))
    if not {expire!r}:
        real_time.sleep(seconds)
recovery_resources.time = SimpleNamespace(
    monotonic=(lambda: clock[0]) if {expire!r} else real_time.monotonic,
    sleep=observed_sleep)
"""
        marker = "runpy.run_module('recovery', run_name='__main__')"
        self.launcher.write_text(self.launcher.read_text().replace(marker, observer + "\n" + marker))

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
        self._volume("ocu-test_documentserver-data", {"not-in-backup": b"documentserver-owned"})
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
            stack_container("ocu-test-documentserver-1", image=images["documentserver"],
                            service="documentserver"),
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
        write_font_pin(source)
        (source / "README").write_text(f"retained delivery source {name}\n", encoding="utf-8")
        (source / "IDENTITY").write_text(f"{name}\n", encoding="utf-8")
        ocu_sha = git_init_commit(source, f"retained delivery {name}")
        delivery = self.root / name
        images_dir = delivery / "images"
        images_dir.mkdir(parents=True)
        write_font_bundle(delivery)
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

    def _cli_env(self, extra=None, uid="0"):
        env = dict(self.env)
        env["OCU_TEST_EUID"] = uid
        env["DEPLOY_ROOT"] = str(self.deploy_root)
        env["PYTHONPATH"] = str(self.recovery_pkg) + os.pathsep + env.get("PYTHONPATH", "")
        env["DOCKER_HOST"] = "unix:///var/run/docker.sock"
        if extra:
            env.update(extra)
        env["DOCKER_HOST"] = "unix:///var/run/docker.sock"
        return env

    def run_cli(self, args, extra=None, uid="0"):
        env = self._cli_env(extra, uid)
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
        office_state, version = self._seed_office()
        before = {path.relative_to(self.chat_dir).as_posix(): path.read_bytes()
                  for path in self.chat_dir.rglob("*") if path.is_file()}
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
        calls = ops(self.state)
        command = "docker exec ocu-test-documentserver-1 /usr/bin/documentserver-prepare4shutdown.sh"
        self.assertIn(command, calls)
        self.assertLess(calls.index("docker stop --time 30 ocu-test-proxy"), calls.index(command))
        for name in ("ocu-test-documentserver-1", "ocu-test-computer-use-server",
                     "ocu-test-open-webui-1"):
            self.assertGreater(calls.index("docker stop --time 30 " + name), calls.index(command))
        observed = json.loads((self.state / "office-shutdown.json").read_text())
        self.assertTrue(observed["ocu_running"])
        self.assertFalse(observed["proxy_running"])
        self.assertEqual(set(manifest["components"]), {
            "database", "webui-data", "chat-data", "skills-cache", "workspaces",
            "runtime-config", "admin-config", "release-inventory", "version-record",
        })
        self.assertNotIn("documentserver-data", json.dumps(manifest))
        self.assertFalse((self.chat_dir / CHAT_ID / "uploads").exists())
        with tarfile.open(dest / "chat-data.tar.gz") as archive:
            captured = {member.name.removeprefix("./"): archive.extractfile(member).read()
                        for member in archive.getmembers() if member.isfile()}
        self.assertEqual(captured, before)
        self.assertEqual(office_state.read_bytes(), before[office_state.relative_to(self.chat_dir).as_posix()])
        self.assertEqual(version.read_bytes(), b"office-version")

    def test_office_blockers_or_failed_shutdown_leave_writers_stopped_without_capture(self):
        self._observe_office_wait(expire=True)
        for cause in ("opening", "editing", "saving", "closing", "journal", "command"):
            with self.subTest(cause=cause):
                self._seed_volumes_and_db()
                journal = {"pending-journal": {"session_id": "session-save"}} if cause == "journal" else {}
                path, _blob = self._seed_office(
                    "closed" if cause == "journal" else ("saving" if cause == "command" else cause),
                    journal)
                before = path.read_bytes()
                if cause == "command":
                    (self.state / "exec.jsonl").write_text(json.dumps({
                        "container": "ocu-test-documentserver-1",
                        "match": "/usr/bin/documentserver-prepare4shutdown.sh",
                        "code": 17, "stderr": "command-output-secret",
                    }) + "\n")
                dest = self.root / ("refused-" + cause)
                result = self.run_cli(["backup", "--deploy-root", str(self.deploy_root),
                                       "--destination", str(dest), "--runtime-file", str(self.runtime)])
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(CHAT_ID, result.stderr)
                self.assertIn("pending-journal" if cause == "journal" else "session-save", result.stderr)
                self.assertNotIn("command-output-secret", result.stderr)
                self.assertFalse(dest.exists())
                self.assertEqual(path.read_bytes(), before)
                for row in json.loads((self.state / "containers.json").read_text()):
                    if row["Name"] != "ocu-test-postgres-1":
                        self.assertFalse(row["State"]["Running"], row["Name"])
                        self.assertFalse(row["State"]["Paused"], row["Name"])
                self.assertFalse(any("pg_dump" in call or " capture " in call for call in ops(self.state)))
                if cause != "command":
                    observed = json.loads((self.state / "office-poll.json").read_text())
                    self.assertEqual(observed["elapsed"], 900)

    def test_office_drain_captures_version_committed_during_wait(self):
        self._seed_office("saving")
        self._observe_office_wait()
        content = b"final callback version"

        def commit_callback():
            self.assertTrue(wait_for(lambda: (self.state / "office-poll.json").exists()))
            return self._seed_office("closed", content=content, parent=1)

        dest = self.root / "drained-backup"
        with ThreadPoolExecutor(max_workers=1) as executor:
            committed = executor.submit(commit_callback)
            result = self.run_cli(["backup", "--deploy-root", str(self.deploy_root),
                                   "--destination", str(dest), "--runtime-file", str(self.runtime)])
            state_path, blob = committed.result(timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        with tarfile.open(dest / "chat-data.tar.gz") as archive:
            captured = {member.name.removeprefix("./"): archive.extractfile(member).read()
                        for member in archive.getmembers() if member.isfile()}
        self.assertEqual(captured[blob.relative_to(self.chat_dir).as_posix()], content)
        self.assertEqual(captured[state_path.relative_to(self.chat_dir).as_posix()], state_path.read_bytes())
        self.assertEqual(json.loads(state_path.read_text())["sessions"]["session-save"]["state"], "closed")

    def test_documentserver_identity_is_checked_before_any_stop(self):
        path = self.state / "containers.json"
        for defect in ("image", "service"):
            with self.subTest(defect=defect):
                self._seed_volumes_and_db()
                containers = json.loads(path.read_text())
                documentserver = next(row for row in containers if row["Name"] == "ocu-test-documentserver-1")
                if defect == "image":
                    documentserver["Image"] = "sha256:" + "0" * 64
                else:
                    documentserver["Config"]["Labels"]["com.docker.compose.service"] = "foreign"
                path.write_text(json.dumps(containers))
                before = path.read_bytes()
                dest = self.root / ("wrong-documentserver-" + defect)
                result = self.run_cli(["backup", "--deploy-root", str(self.deploy_root),
                                       "--destination", str(dest), "--runtime-file", str(self.runtime)])
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("ocu-test-documentserver-1", result.stderr)
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse((self.state / "stopped.log").exists())
                self.assertFalse(dest.exists())

    def test_cancellation_during_shutdown_reaps_exec_and_stops_writers(self):
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(signal=signum):
                self._seed_volumes_and_db()
                self._seed_office("saving")
                entered = self.state / "entered-office-shutdown"
                entered.unlink(missing_ok=True)
                hold = self.state / "hold-office"
                hold.touch()
                dest = self.root / ("cancelled-" + str(signum))
                process = subprocess.Popen(
                    [sys.executable, str(self.launcher), "backup",
                     "--deploy-root", str(self.deploy_root), "--destination", str(dest),
                     "--runtime-file", str(self.runtime)],
                    cwd=ROOT, env=self._cli_env({"FAKE_DOCKER_HOLD_OFFICE_SHUTDOWN": str(hold)}),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
                )
                try:
                    self.assertTrue(wait_for(entered.exists, timeout=10))
                    child = int(entered.read_text())
                    process.send_signal(signum)
                    try:
                        stdout, stderr = process.communicate(timeout=8)
                    except subprocess.TimeoutExpired:
                        self.fail("shutdown cancellation did not release the owned Docker CLI")
                    self.assertEqual(process.returncode, 128 + signum, stdout + stderr)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(child, 0)
                    for row in json.loads((self.state / "containers.json").read_text()):
                        if row["Name"] != "ocu-test-postgres-1":
                            self.assertFalse(row["State"]["Running"], row["Name"])
                    self.assertFalse(dest.exists())
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=5)
                    hold.unlink(missing_ok=True)

    def test_owned_docker_timeout_reaps_shutdown_process(self):
        hold = self.state / "hold-office"
        hold.touch()
        entered = self.state / "entered-office-shutdown"
        code = """
import recovery, subprocess
try:
    recovery.docker("exec", "ocu-test-documentserver-1",
                    "/usr/bin/documentserver-prepare4shutdown.sh", timeout=0.5)
except subprocess.TimeoutExpired:
    print("owned deadline")
else:
    raise AssertionError("shutdown command escaped its deadline")
"""
        process = subprocess.Popen(
            [sys.executable, "-c", code], cwd=ROOT,
            env=self._cli_env({"FAKE_DOCKER_HOLD_OFFICE_SHUTDOWN": str(hold)}),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(stdout.strip(), "owned deadline")
            with self.assertRaises(ProcessLookupError):
                os.kill(int(entered.read_text()), 0)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
            hold.unlink(missing_ok=True)

    def test_office_timeout_configuration_refuses_before_mutation(self):
        import runpy
        import recovery_resources
        liveness = runpy.run_path(str(ROOT / "computer-use-server/office/config.py"))[
            "SESSION_LIVENESS_INTERVAL_SECONDS"]
        self.assertEqual(recovery_resources.OFFICE_LIVENESS_SECONDS, liveness)
        self.assertGreater(recovery_resources.OFFICE_QUIESCE_TIMEOUT_SECONDS, liveness)
        before = (self.state / "containers.json").read_bytes()
        for value in ("", "invalid", "nan", "inf", "-inf", "-1", str(liveness)):
            with self.subTest(value=value):
                dest = self.root / "invalid-timeout"
                result = self.run_cli(
                    ["backup", "--deploy-root", str(self.deploy_root), "--destination", str(dest),
                     "--runtime-file", str(self.runtime)],
                    {"OCU_OFFICE_BACKUP_TIMEOUT_SECONDS": value})
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("OCU_OFFICE_BACKUP_TIMEOUT_SECONDS", result.stderr)
                self.assertEqual((self.state / "containers.json").read_bytes(), before)
                self.assertFalse((self.state / "stopped.log").exists())
                self.assertFalse(dest.exists())

    def test_custom_office_timeout_bounds_the_drain(self):
        self._observe_office_wait(expire=True, elapsed_step=601.5)
        state, _ = self._seed_office("saving")
        before = state.read_bytes()
        dest = self.root / "custom-office-deadline"
        result = self.run_cli(
            ["backup", "--deploy-root", str(self.deploy_root), "--destination", str(dest),
             "--runtime-file", str(self.runtime)],
            {"OCU_OFFICE_BACKUP_TIMEOUT_SECONDS": "601.5"})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Office quiescence timed out", result.stderr)
        self.assertIn("session-save", result.stderr)
        self.assertEqual(json.loads((self.state / "office-poll.json").read_text())["elapsed"], 601.5)
        self.assertEqual(state.read_bytes(), before)
        self.assertFalse(dest.exists())
        for row in json.loads((self.state / "containers.json").read_text()):
            if row["Name"] != "ocu-test-postgres-1":
                self.assertFalse(row["State"]["Running"], row["Name"])

    def test_office_shutdown_requires_a_running_callback_server(self):
        path = self.state / "containers.json"
        for status in ("exited", "paused"):
            with self.subTest(status=status):
                self._seed_volumes_and_db()
                self._seed_office()
                rows = json.loads(path.read_text())
                server = next(row for row in rows if row["Name"] == "ocu-test-computer-use-server")
                server["State"] = {"Status": status, "Running": False, "Paused": status == "paused"}
                path.write_text(json.dumps(rows))
                dest = self.root / ("no-callback-server-" + status)
                result = self.run_cli(
                    ["backup", "--deploy-root", str(self.deploy_root), "--destination", str(dest),
                     "--runtime-file", str(self.runtime)])
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("OCU must remain running for Office shutdown", result.stderr)
                self.assertFalse((self.state / "office-shutdown.json").exists())
                self.assertFalse(dest.exists())
                self.assertFalse(any("pg_dump" in op for op in ops(self.state)))
                for row in json.loads(path.read_text()):
                    if row["Name"] not in ("ocu-test-postgres-1", "ocu-test-computer-use-server"):
                        self.assertFalse(row["State"]["Running"], row["Name"])
                if status == "paused":
                    self.assertIn("is paused", result.stderr)

    def test_unsafe_office_state_cannot_certify_quiescence(self):
        state, _ = self._seed_office()
        good = state.read_bytes()
        outside = self.root / "outside-state"
        outside.write_bytes(good)
        for defect in ("malformed", "schema", "duplicate", "symlink", "fifo", "directory"):
            with self.subTest(defect=defect):
                self._seed_volumes_and_db()
                if state.is_dir():
                    state.rmdir()
                else:
                    state.unlink(missing_ok=True)
                if defect == "malformed":
                    state.write_text("{")
                elif defect == "schema":
                    payload = json.loads(good)
                    payload["schema_version"] = 2
                    state.write_text(json.dumps(payload))
                elif defect == "duplicate":
                    state.write_text('{"schema_version":1,"schema_version":1}')
                elif defect == "symlink":
                    state.symlink_to(outside)
                elif defect == "fifo":
                    os.mkfifo(state)
                else:
                    state.mkdir()
                dest = self.root / ("unsafe-office-" + defect)
                result = self.run_cli(
                    ["backup", "--deploy-root", str(self.deploy_root), "--destination", str(dest),
                     "--runtime-file", str(self.runtime)])
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("cannot safely read Office state", result.stderr)
                self.assertIn(CHAT_ID, result.stderr)
                self.assertFalse(dest.exists())
                self.assertEqual(outside.read_bytes(), good)
                for row in json.loads((self.state / "containers.json").read_text()):
                    if row["Name"] != "ocu-test-postgres-1":
                        self.assertFalse(row["State"]["Running"], row["Name"])

    def test_terminal_office_sessions_without_journal_allow_capture(self):
        for status in ("closed", "error", "orphaned", "conflict"):
            with self.subTest(status=status):
                self._seed_volumes_and_db()
                state, _ = self._seed_office(status)
                before = state.read_bytes()
                dest = self.root / ("terminal-office-" + status)
                result = self.run_cli(
                    ["backup", "--deploy-root", str(self.deploy_root), "--destination", str(dest),
                     "--runtime-file", str(self.runtime)])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(state.read_bytes(), before)
                self.assertTrue((dest / "recovery.json").is_file())

    def test_documentserver_stop_failure_does_not_skip_other_writer_stops(self):
        for status in ("running", "paused", "stop-error"):
            with self.subTest(status=status):
                self._seed_volumes_and_db()
                self._seed_office()
                path = self.state / "containers.json"
                rows = json.loads(path.read_text())
                ds = next(row for row in rows if row["Name"] == "ocu-test-documentserver-1")
                if status == "stop-error":
                    ds["unstoppable"] = True
                else:
                    ds["after_stop_state"] = {"Status": status, "Running": status == "running",
                                              "Paused": status == "paused"}
                path.write_text(json.dumps(rows))
                dest = self.root / ("surviving-documentserver-" + status)
                result = self.run_cli(
                    ["backup", "--deploy-root", str(self.deploy_root), "--destination", str(dest),
                     "--runtime-file", str(self.runtime)])
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("ocu-test-documentserver-1", result.stderr)
                self.assertFalse(dest.exists())
                for row in json.loads(path.read_text()):
                    if row["Name"] not in ("ocu-test-postgres-1", "ocu-test-documentserver-1"):
                        self.assertFalse(row["State"]["Running"], row["Name"])
                self.assertFalse(any("pg_dump" in op for op in ops(self.state)))

    def test_late_office_journal_after_drain_prevents_capture(self):
        state, _ = self._seed_office()
        hold = self.state / "hold-stop"
        hold.touch()
        entered = self.state / "entered-stop"
        dest = self.root / "late-office"
        def commit_late_obligation():
            self.assertTrue(wait_for(entered.exists, timeout=10))
            payload = json.loads(state.read_text())
            payload["journal"]["late-save"] = {"kind": "publish"}
            staged = state.with_suffix(".pending")
            staged.write_text(json.dumps(payload))
            staged.replace(state)
            hold.unlink()
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(commit_late_obligation)
            result = self.run_cli(
                ["backup", "--deploy-root", str(self.deploy_root), "--destination", str(dest),
                 "--runtime-file", str(self.runtime)],
                {"FAKE_DOCKER_HOLD_STOP": str(hold),
                 "FAKE_DOCKER_HOLD_STOP_TARGET": "ocu-test-documentserver-1"})
            pending.result()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("late-save", result.stderr)
        self.assertIn("Office state changed before capture", result.stderr)
        self.assertFalse(dest.exists())
        self.assertIn("late-save", json.loads(state.read_text())["journal"])

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
        # A second maintenance run follows an operator start, not the stopped backup state.
        (self.state / "containers.json").write_text(json.dumps(containers), encoding="utf-8")
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
        target_state = self._seed_restore_target("target-state")
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
        epoch = (dest / "data/chat/.office-restore-epoch").read_bytes()
        self.assertEqual(epoch.count(b"\n"), 1)
        self.assertTrue(epoch.endswith(b"\n"))
        self.assertTrue(epoch.strip())
        self.assertEqual(self._read_restored_epoch(dest / "data/chat"), epoch.decode().strip())
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
        (self.chat_dir / other / "outputs").mkdir(parents=True)
        self._seed_volumes_and_db(running_sandbox=False, extra_containers=[extra])
        (self.chat_dir / other / "outputs/other.txt").write_bytes(b"other-workspace\n")
        self._seed_office(content=b"broker-bytes\n")
        self._seed_office(content=b"other-workspace\n", chat_id=other)
        marker = self.chat_dir / ".office-restore-epoch"
        marker.write_bytes(b"captured-epoch\n")
        captured = {path.relative_to(self.chat_dir).as_posix(): path.read_bytes()
                    for path in self.chat_dir.rglob("*") if path.is_file() and path != marker}
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
        tokens = {"captured-epoch"}
        for attempt in range(2):
            target_state = self._seed_restore_target(f"keep-two-state-{attempt}")
            dest = self.root / f"keep-two-root-{attempt}"
            restored = self.run_cli(
                ["restore", "--recovery-set", str(backup), "--destination-root", str(dest),
                 "--provider-file", str(provider)],
                extra={"FAKE_DOCKER_STATE": str(target_state)},
            )
            self.assertEqual(restored.returncode, 0, restored.stderr)
            chat_root = dest / "data/chat"
            epoch = (chat_root / marker.name).read_bytes()
            self.assertEqual(epoch.count(b"\n"), 1)
            self.assertTrue(epoch.endswith(b"\n"))
            token = epoch.decode().strip()
            self.assertNotIn(token, tokens)
            self.assertTrue(token)
            tokens.add(token)
            self.assertEqual(self._read_restored_epoch(chat_root), token)
            restored_files = {path.relative_to(chat_root).as_posix(): path.read_bytes()
                              for path in chat_root.rglob("*")
                              if path.is_file() and path != chat_root / marker.name}
            self.assertEqual(restored_files, captured)
            for chat, expected in ((CHAT_ID, b"sandbox-home\n"), (other, b"other-home\n")):
                home = target_state / "volume-data" / f"chat-{chat}-workspace"
                self.assertEqual((home / "README.md").read_bytes(), expected)
            self.assertFalse((target_state / "starts.log").exists())
        self.assertEqual(marker.read_bytes(), b"captured-epoch\n")


    def test_restore_epoch_io_failures_never_publish_readiness(self):
        (self.chat_dir / ".office-restore-epoch").write_bytes(b"captured-epoch\n")
        self._seed_office()
        backup = self.root / "epoch-failure-backup"
        captured = self.run_cli(
            ["backup", "--deploy-root", str(self.deploy_root), "--destination", str(backup),
             "--runtime-file", str(self.runtime)])
        self.assertEqual(captured.returncode, 0, captured.stderr)
        provider = self.root / "provider.env"
        provider.write_text(f"DMXAPI_API_KEY={PROVIDER_B}\n")
        provider.chmod(0o600)
        launcher = self.launcher.read_text()
        for failure in ("write", "replace", "dirsync", "directory"):
            with self.subTest(failure=failure):
                target = self._seed_restore_target("epoch-failure-" + failure)
                dest = self.root / ("epoch-root-" + failure)
                marker = dest / "data/chat/.office-restore-epoch"
                observed = self.root / ("epoch-fault-" + failure)
                injection = f"""
import errno, io, os, stat
from pathlib import Path
original_open, original_replace, original_fsync = io.open, os.replace, os.fsync
armed = [False]
def fail_epoch_io():
    Path({str(observed)!r}).write_text({failure!r})
    raise OSError(errno.ENOSPC if {failure!r} == 'write' else errno.EIO, 'injected epoch IO')
def epoch_open(path, mode='r', *args, **kwargs):
    if ({failure!r} == 'write' and not isinstance(path, int) and 'w' in mode
            and Path(path).name.startswith('.office-restore-epoch-')):
        fail_epoch_io()
    return original_open(path, mode, *args, **kwargs)
def epoch_replace(source, target, *args, **kwargs):
    if str(target) == {str(marker)!r}:
        if {failure!r} == 'replace':
            fail_epoch_io()
        if {failure!r} == 'directory':
            Path(target).unlink()
            Path(target).mkdir()
            Path({str(observed)!r}).write_text({failure!r})
        result = original_replace(source, target, *args, **kwargs)
        armed[0] = True
        return result
    return original_replace(source, target, *args, **kwargs)
def epoch_fsync(fd):
    if {failure!r} == 'dirsync' and armed[0] and stat.S_ISDIR(os.fstat(fd).st_mode):
        fail_epoch_io()
    return original_fsync(fd)
io.open, os.replace, os.fsync = epoch_open, epoch_replace, epoch_fsync
"""
                entry = "runpy.run_module('recovery', run_name='__main__')"
                self.launcher.write_text(launcher.replace(entry, injection + "\n" + entry))
                result = self.run_cli(
                    ["restore", "--recovery-set", str(backup), "--destination-root", str(dest),
                     "--provider-file", str(provider)], {"FAKE_DOCKER_STATE": str(target)})
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(observed.read_text(), failure)
                self.assertIn("cannot establish Office restore epoch", result.stderr)
                self.assertIn("owned partial resources remain", result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertFalse((dest / ".restored").exists())
                self.assertFalse((target / "starts.log").exists())
                self.assertFalse((target / "volumes.json").exists())
                self.assertFalse((target / "postgres.json").exists())
                self.assertEqual(list(dest.glob(".office-restore-epoch-*")), [])
                if failure == "dirsync":
                    self.assertNotEqual(marker.read_bytes(), b"captured-epoch\n")
                    self.assertEqual(self._read_restored_epoch(marker.parent), marker.read_text().strip())
                elif failure == "directory":
                    self.assertTrue(marker.is_dir())
                else:
                    self.assertEqual(marker.read_bytes(), b"captured-epoch\n")

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
        self.assertNotIn("OCU_RELEASE_FONTS_DIR=", self.runtime.read_text())
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
        self.assertNotIn("OCU_RELEASE_FONTS_DIR=", (backup / "runtime.env").read_text())
        provider = self.root / "provider.env"
        provider.write_text(f"DMXAPI_API_KEY={PROVIDER_B}\n", encoding="utf-8")
        os.chmod(provider, 0o600)
        target_state = self.root / "activate-state"
        target_state.mkdir()
        seed_images(target_state)
        seed_healthy_host(target_state)
        write_network(target_state, "ocu-test-private", subnet="172.30.0.0/24", gateway="172.30.0.1")
        write_network(target_state, "ocu-sandbox", subnet="172.31.0.0/24", gateway="172.31.0.1")
        write_fake_configs(target_state, font_mount_docs())
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
        self.assertNotIn("OCU_RELEASE_FONTS_DIR=", (dest / "config/runtime.env").read_text())
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
        write_installed_fonts(independently_built)
        font_entry = dest / "fonts"
        for linked in (False, True):
            with self.subTest(foreign_font_link=linked):
                if linked:
                    font_entry.symlink_to(independently_built / "fonts", target_is_directory=True)
                else:
                    shutil.copytree(independently_built / "fonts", font_entry)
                refused = self.run_cli(
                    ["activate", "--destination-root", str(dest),
                     "--retained-delivery", str(delivery)], extra=extra
                )
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn("font", refused.stderr)
                self.assertEqual((dest / "release.json").read_bytes(), before_manifest)
                self.assertEqual((dest / "config/runtime.env").read_bytes(), before_runtime)
                self.assertFalse((target_state / "starts.log").exists())
                self.assertFalse((dest / "config/selected-source-owner.json").exists())
                self.assertEqual(
                    {path.name: path.read_bytes() for path in font_entry.iterdir()}, FONT_FILES
                )
                if linked:
                    self.assertEqual(font_entry.resolve(), (independently_built / "fonts").resolve())
                    font_entry.unlink()
                else:
                    shutil.rmtree(font_entry)
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
        self.assertTrue(font_entry.is_symlink())
        self.assertEqual(font_entry.resolve(), (foreign_root / "fonts").resolve())
        self.assertEqual(
            {path.name: path.read_bytes() for path in font_entry.iterdir()}, FONT_FILES
        )
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
        self.assertNotIn("OCU_RELEASE_FONTS_DIR", persisted)
        self.assertEqual(persisted["OCU_OFFICE_FONTS_DIR"], self.env["OCU_OFFICE_FONTS_DIR"])
        starts = (target_state / "starts.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(starts, ["core", "webui", "proxy"])
        executed = (target_state / "executed.json").read_text(encoding="utf-8")
        self.assertIn("--no-build", executed)
        self.assertIn("never", executed)
        rows = [json.loads(line) for line in executed.splitlines()]
        core = next(row["consumer_document"] for row in rows if row["stack"] == "core")
        mounts = {mount["target"]: mount for mount in core["services"]["documentserver"]["volumes"]}
        release_source = Path(mounts["/usr/share/fonts/truetype/ocu-release"]["source"])
        self.assertEqual(release_source, dest / "fonts")
        self.assertEqual(release_source.resolve(), (foreign_root / "fonts").resolve())
        self.assertNotEqual(release_source.resolve(), (self.state / "fonts").resolve())
        self.assertEqual(mounts["/usr/share/fonts/truetype/ocu-operator"]["source"],
                         self.env["OCU_OFFICE_FONTS_DIR"])
        containers = json.loads((target_state / "containers.json").read_text(encoding="utf-8"))
        sandboxes = [item for item in containers if str(item.get("Name", "")).startswith("owui-chat-")]
        for item in sandboxes:
            state = item.get("State")
            running = state.get("Running") if isinstance(state, dict) else str(state) == "running"
            self.assertFalse(running)
        published = [dest / "release.json", dest / "config/runtime.env"]
        identities = [(path.stat().st_ino, path.read_bytes()) for path in published]
        before_starts = (target_state / "starts.log").read_bytes()
        selected_font = font_entry / "fixture-cjk.otf"
        selected_font.write_bytes(b"altered selected font")
        refused = self.run_cli(
            ["activate", "--destination-root", str(dest),
             "--retained-delivery", str(delivery)], extra=extra
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("font", refused.stderr)
        self.assertEqual(
            [(path.stat().st_ino, path.read_bytes()) for path in published], identities
        )
        self.assertEqual((target_state / "starts.log").read_bytes(), before_starts)
        selected_font.write_bytes(FONT_FILES["fixture-cjk.otf"])
        repeated = self.run_cli(
            ["activate", "--destination-root", str(dest),
             "--retained-delivery", str(delivery)], extra=extra
        )
        self.assertEqual(repeated.returncode, 0, repeated.stderr)


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
        after_fonts = '        os.symlink(source_root / "fonts", fonts)'
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
            (after_fonts, after_fonts + '\n        raise RecoveryError("injected interruption after font publication")'),
            (after_inventory, after_inventory + '\n    raise RecoveryError("injected interruption after inventory publication")'),
            (after_runtime, '        raise RecoveryError("injected interruption after runtime publication")\n' + after_runtime),
            (after_version, '        raise RecoveryError("injected interruption after version publication")\n' + after_version),
        ):
            self.assertIn(needle, original)
            if (dest / "source").exists() or (dest / "source").is_symlink():
                (dest / "source").unlink()
            (dest / "fonts").unlink(missing_ok=True)
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

    def test_retained_version_one_refuses_restore_and_activation_without_mutation(self):
        backup = self.root / "backup"
        created = self.run_cli([
            "backup", "--deploy-root", str(self.deploy_root),
            "--destination", str(backup), "--runtime-file", str(self.runtime),
        ])
        self.assertEqual(created.returncode, 0, created.stderr)
        provider = self.root / "provider.env"
        provider.write_text(f"DMXAPI_API_KEY={PROVIDER_B}\n")
        os.chmod(provider, 0o600)
        delivery, inventory, _source = self._write_retained_delivery()
        inventory["format_version"] = 1
        inventory["images"].pop("documentserver")
        inventory.pop("font_bundle")
        write_inventory(delivery / "release.json", inventory)
        target_state = self.root / "historical-target"
        target_state.mkdir()
        seed_images(target_state)
        (target_state / "docker-info.json").write_text(json.dumps({"ID": "target-daemon"}))
        extra = {"FAKE_DOCKER_STATE": str(target_state)}
        destination = self.root / "historical-restore"
        restore = [
            "restore", "--recovery-set", str(backup),
            "--destination-root", str(destination), "--provider-file", str(provider),
        ]

        def snapshot(root):
            return {
                str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in root.rglob("*") if path.is_file() and path.name != "ops.log"
            }

        mutation_commands = {"load", "pull", "build", "create", "up", "run",
                             "start", "stop", "rm", "iptables-restore", "ip6tables-restore"}
        before_state = snapshot(target_state)
        before_ops = len(ops(target_state))
        refused = self.run_cli(restore + ["--retained-delivery", str(delivery)], extra=extra)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("format_version 1", refused.stderr)
        self.assertFalse(destination.exists())
        self.assertEqual(snapshot(target_state), before_state)
        self.assertFalse(any(
            set(row.split()) & mutation_commands for row in ops(target_state)[before_ops:]
        ))

        restored = self.run_cli(restore, extra=extra)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        before_state, before_destination = snapshot(target_state), snapshot(destination)
        before_ops = len(ops(target_state))
        refused = self.run_cli([
            "activate", "--destination-root", str(destination),
            "--retained-delivery", str(delivery),
        ], extra=extra)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("format_version 1", refused.stderr)
        self.assertEqual(snapshot(target_state), before_state)
        self.assertEqual(snapshot(destination), before_destination)
        self.assertFalse(any(
            set(row.split()) & mutation_commands for row in ops(target_state)[before_ops:]
        ))

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
        self.assertEqual(runtime["DOCUMENTSERVER_IMAGE"], previous["documentserver"])
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
