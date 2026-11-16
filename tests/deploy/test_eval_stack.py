# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""The isolated ocu-eval stack: native Compose shape, secrets, image load and start/stop guards."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

import yaml

from support import (
    DEFAULT_RELEASE_DIGESTS,
    DEFAULT_RELEASE_IMAGES,
    ROOT,
    RUNTIME_IMAGE_VARS,
    config_payload,
    synthetic_inventory,
    write_image_archive,
    write_inventory,
)

EVAL_DIR = ROOT / "deploy" / "eval"
COMPOSE_EVAL = EVAL_DIR / "compose.eval.yml"
SECRETS = EVAL_DIR / "secrets.sh"
LOAD = EVAL_DIR / "load.sh"
START = EVAL_DIR / "start.sh"
STOP = EVAL_DIR / "stop.sh"
PROJECT = "ocu-eval"
EVAL_ROLES = ("workspace", "computer-use-server", "open-webui", "postgres")
STARTED = {"computer-use-server", "open-webui", "postgres", "open-webui-init"}
OMITTED = {"workspace", "cleanup", "proxy", "messages-ingress", "documentserver", "retention-guard"}
STACK_NAMES = ("OCU_INTERNAL_TOKEN", "MCP_API_KEY", "WEBUI_SECRET_KEY",
               "POSTGRES_PASSWORD", "ADMIN_EMAIL", "ADMIN_PASSWORD")
ADMIN_EMAIL = "eval-admin@ocu-eval.local"
PROVIDER_KEY_VAR = "EVAL_PROVIDER_KEY"
SERVICE_IMAGE_VARS = {
    "computer-use-server": "COMPUTER_USE_SERVER_IMAGE",
    "open-webui": "OPENWEBUI_IMAGE",
    "open-webui-init": "OPENWEBUI_IMAGE",
    "postgres": "POSTGRES_IMAGE",
}
WEBUI_PORT = "3390"
# 15 GiB on Docker's root filesystem, stated in bytes by the scripts.
FLOOR_BYTES = 15 * 1024 ** 3
PLENTY_KB = 40 * 1024 ** 2
LOW_KB = 10 * 1024 ** 2
FORBIDDEN_TOKENS = {"rmi", "--rmi", "prune", "system", "rm", "-v", "--volumes", "kill"}
PLANTED = {
    "OCU_INTERNAL_TOKEN": "fake-internal-token",
    "MCP_API_KEY": "fake-mcp-key",
    "WEBUI_SECRET_KEY": "fake-webui-secret",
    "POSTGRES_PASSWORD": "fake-postgres-password",
    "ADMIN_EMAIL": ADMIN_EMAIL,
    "ADMIN_PASSWORD": "fake-admin-password",
}
PLANTED_KEY = "fake-provider-key"
SECRET_VALUES = tuple(value for name, value in PLANTED.items() if name != "ADMIN_EMAIL") + (PLANTED_KEY,)


def compose_command() -> list[str] | None:
    standalone = shutil.which("docker-compose")
    if standalone:
        return [standalone]
    if shutil.which("docker") is None:
        return None
    probe = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True,
                           timeout=30, check=False)
    return ["docker", "compose"] if probe.returncode == 0 else None


COMPOSE = compose_command()


def config_environment(directory: Path, eval_root: Path) -> dict[str, str]:
    """Fake values for every name the eval stack interpolates; no daemon is reachable."""
    environment = {
        "PATH": os.environ["PATH"], "HOME": str(directory),
        "DOCKER_CONFIG": str(directory), "COMPOSE_DISABLE_ENV_FILE": "1",
        "DOCKER_HOST": "unix://" + str(directory / "no-daemon.sock"),
        "EVAL_ROOT": str(eval_root), "EVAL_WEBUI_PORT": WEBUI_PORT,
        PROVIDER_KEY_VAR: "fixture-provider-key",
    }
    for name in STACK_NAMES:
        environment[name] = "fixture" + name.lower().replace("_", "")
    environment["ADMIN_EMAIL"] = ADMIN_EMAIL
    for role in EVAL_ROLES:
        environment[RUNTIME_IMAGE_VARS[role]] = DEFAULT_RELEASE_IMAGES[role]
    return environment


def resolve_eval(directory: Path, environment: dict[str, str]) -> subprocess.CompletedProcess:
    directory.mkdir(parents=True, exist_ok=True)
    empty = directory / "empty.env"
    empty.write_text("", encoding="utf-8")
    argv = [*COMPOSE, "-p", PROJECT, "--project-directory", str(ROOT), "--env-file", str(empty),
            "-f", str(ROOT / "docker-compose.yml"), "-f", str(ROOT / "docker-compose.webui.yml"),
            "-f", str(COMPOSE_EVAL), "config", "--format", "json", "--no-env-resolution"]
    return subprocess.run(argv, cwd=ROOT, env=environment, capture_output=True, text=True,
                          timeout=60, check=False)


class TagLoader(yaml.SafeLoader):
    pass


def _tagged(tag):
    def construct(loader, node):
        if isinstance(node, yaml.MappingNode):
            value = loader.construct_mapping(node)
        elif isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node)
        else:
            value = loader.construct_scalar(node)
        return {tag: value}
    return construct


TagLoader.add_constructor("!reset", _tagged("!reset"))
TagLoader.add_constructor("!override", _tagged("!override"))


@unittest.skipIf(COMPOSE is None, "docker compose v2 is not installed; native config resolution cannot run")
class EvalComposeConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ocu-eval-config-")
        self.tmp = Path(self._tmp.name)
        self.eval_root = self.tmp / "eval-root"
        self.addCleanup(self._tmp.cleanup)

    def resolved(self) -> dict:
        result = resolve_eval(self.tmp / "compose", config_environment(self.tmp, self.eval_root))
        self.assertEqual(result.returncode, 0, f"native Compose resolution failed: {result.stderr}")
        self.assertNotIn("is not set", result.stderr)
        return json.loads(result.stdout)

    def test_project_runs_exactly_the_four_services(self):
        document = self.resolved()
        self.assertEqual(document["name"], PROJECT)
        self.assertEqual(set(document["services"]), STARTED)
        self.assertFalse(OMITTED & set(document["services"]))
        for name, service in document["services"].items():
            self.assertEqual(service.get("container_name"), f"{PROJECT}-{name}")
            self.assertNotIn("build", service, name)
            self.assertFalse(service.get("env_file"), name)
            expected = config_environment(self.tmp, self.eval_root)[SERVICE_IMAGE_VARS[name]]
            self.assertEqual(service["image"], expected, name)
        self.assertEqual(set(document.get("volumes", {})), {"open-webui-data", "postgres-data"})

    def test_only_open_webui_publishes_and_only_on_loopback(self):
        document = self.resolved()
        for name, service in document["services"].items():
            ports = service.get("ports") or []
            if name != "open-webui":
                self.assertEqual(ports, [], name)
                continue
            self.assertEqual(len(ports), 1)
            self.assertEqual(ports[0]["host_ip"], "127.0.0.1")
            self.assertEqual(str(ports[0]["published"]), WEBUI_PORT)
            self.assertEqual(ports[0]["target"], 8080)

    def test_bind_sources_stay_inside_the_allowed_set(self):
        document = self.resolved()
        allowed = {"/var/run/docker.sock", str(ROOT / "openwebui"),
                   str(ROOT / "deploy/production-like-test/init/run-init.sh")}
        root_prefix = str(self.eval_root) + "/"
        for name, service in document["services"].items():
            for mount in service.get("volumes") or []:
                if mount["type"] == "volume":
                    continue
                self.assertEqual(mount["type"], "bind", name)
                source = mount["source"]
                self.assertTrue(source in allowed or source.startswith(root_prefix),
                                f"{name}: bind source {source} is outside the allowed set")
        server = document["services"]["computer-use-server"]
        binds = {mount["source"]: mount for mount in server["volumes"]}
        data = str(self.eval_root / "data")
        self.assertEqual(set(binds), {"/var/run/docker.sock", data})
        self.assertEqual(binds[data]["target"], data)
        # Compose omits a false create_host_path from JSON; the short syntax reports true.
        self.assertFalse(binds[data].get("bind", {}).get("create_host_path", False))

    def test_server_environment_is_the_listed_override(self):
        document = self.resolved()
        server = document["services"]["computer-use-server"]
        environment = server["environment"]
        self.assertEqual(set(environment), {
            "ENABLE_NETWORK", "BASE_DATA_DIR", "DOCKER_IMAGE", "MCP_API_KEY", "OCU_INTERNAL_TOKEN",
            "TTS_BASE_URL", "TTS_MODEL", "TTS_VOICE", "TTS_API_KEY", "CONTAINER_IDLE_TIMEOUT",
        })
        self.assertEqual(environment["ENABLE_NETWORK"], "false")
        # Production's three days, not the upstream 600 s that reaps a long render.
        self.assertEqual(environment["CONTAINER_IDLE_TIMEOUT"], "259200")
        self.assertEqual(environment["BASE_DATA_DIR"], str(self.eval_root / "data"))
        self.assertEqual(environment["DOCKER_IMAGE"], DEFAULT_RELEASE_IMAGES["workspace"])
        for name in ("TTS_BASE_URL", "TTS_MODEL", "TTS_VOICE", "TTS_API_KEY"):
            self.assertEqual(environment[name], "", name)
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertFalse([name for name in environment
                          if name.startswith(("VISION_", "ANTHROPIC_"))])
        self.assertFalse(server.get("depends_on"))

    def test_webui_postgres_and_init_wiring(self):
        document = self.resolved()
        fake = config_environment(self.tmp, self.eval_root)
        webui = document["services"]["open-webui"]["environment"]
        self.assertEqual(webui, {
            "DATABASE_URL": f"postgresql://openwebui:{fake['POSTGRES_PASSWORD']}@postgres:5432/openwebui",
            "WEBUI_ADMIN_EMAIL": ADMIN_EMAIL,
            "WEBUI_ADMIN_PASSWORD": fake["ADMIN_PASSWORD"],
            "WEBUI_SECRET_KEY": fake["WEBUI_SECRET_KEY"],
            "OPENAI_API_BASE_URL": "https://www.dmxapi.cn/v1",
            "OPENAI_API_KEY": fake[PROVIDER_KEY_VAR],
            "ENABLE_OLLAMA_API": "false",
            "OFFLINE_MODE": "true",
            "ENABLE_OCU_WORKSPACE": "true",
            "OCU_INTERNAL_URL": "http://computer-use-server:8081",
            "OCU_INTERNAL_TOKEN": fake["OCU_INTERNAL_TOKEN"],
            "ENABLE_OCU_OFFICE_EDIT": "false",
        })
        # The admin password reaches only WebUI and its initializer.
        for name in ("computer-use-server", "postgres"):
            self.assertFalse(fake["ADMIN_PASSWORD"] in json.dumps(document["services"][name]),
                             f"the admin password reached {name}")
        postgres = document["services"]["postgres"]["environment"]
        self.assertEqual(postgres["POSTGRES_PASSWORD"], fake["POSTGRES_PASSWORD"])
        init = document["services"]["open-webui-init"]
        self.assertEqual(init["user"], "0:0")
        self.assertEqual(init["entrypoint"], ["/bin/bash", "/bootstrap/run-init.sh"])
        self.assertEqual(init["depends_on"]["open-webui"]["condition"], "service_started")
        self.assertEqual(init["restart"], "no")
        self.assertEqual(init["environment"], {
            "WEBUI_URL": "http://open-webui:8080",
            "ADMIN_EMAIL": ADMIN_EMAIL,
            "ADMIN_PASSWORD": fake["ADMIN_PASSWORD"],
            "ORCHESTRATOR_URL": "http://computer-use-server:8081",
            "MCP_API_KEY": fake["MCP_API_KEY"],
            "PRIMARY_CHAT_MODEL": "qwen3.8-27b",
        })
        mounts = {(mount["type"], mount["source"], mount["target"]) for mount in init["volumes"]}
        self.assertEqual(mounts, {
            ("bind", str(ROOT / "openwebui"), "/app/source"),
            ("bind", str(ROOT / "deploy/production-like-test/init/run-init.sh"), "/bootstrap/run-init.sh"),
            ("volume", "open-webui-data", "/app/backend/data"),
        })

    def test_unset_eval_root_fails_loud(self):
        environment = config_environment(self.tmp, self.eval_root)
        del environment["EVAL_ROOT"]
        result = resolve_eval(self.tmp / "compose", environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("EVAL_ROOT", result.stderr)



class EvalComposeSourceTests(unittest.TestCase):
    """Source-level shape that native config output cannot show; needs only PyYAML."""

    def test_source_keeps_disabled_services_and_fail_loud_interpolation(self):
        text = COMPOSE_EVAL.read_text(encoding="utf-8")
        source = yaml.load(text, Loader=TagLoader)
        for name in ("workspace", "cleanup"):
            self.assertEqual(source["services"][name]["profiles"], ["never"], name)
        self.assertEqual(source["services"]["computer-use-server"]["depends_on"], {"!reset": {}})
        webui = source["services"]["open-webui"]["environment"]["!override"]
        self.assertTrue(webui["WEBUI_ADMIN_EMAIL"].startswith("${ADMIN_EMAIL:?"))
        self.assertTrue(webui["WEBUI_ADMIN_PASSWORD"].startswith("${ADMIN_PASSWORD:?"))
        required = {"EVAL_ROOT", "EVAL_WEBUI_PORT", *SERVICE_IMAGE_VARS.values(), "DOCKER_IMAGE",
                    *STACK_NAMES}
        seen = set()
        body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        for name, modifier in re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)([^}]*)\}", body):
            if name in required:
                seen.add(name)
                self.assertTrue(modifier.startswith(":?"), f"{name} must use the :? form")
            if name == PROVIDER_KEY_VAR:
                seen.add(name)
                # Set-but-empty is allowed: stop.sh exports it empty for compose down.
                self.assertTrue(modifier.startswith("?"), f"{name} must use the ? form")
        self.assertEqual(seen, required | {PROVIDER_KEY_VAR})
        self.assertNotIn("env_file", text)


DOCKER_STUB = r'''#!__PYTHON__
import hashlib, json, os, sys, tarfile
state = os.environ["EVAL_STUB_STATE"]
args = sys.argv[1:]
with open(os.path.join(state, "calls.jsonl"), "a", encoding="utf-8") as log:
    log.write(json.dumps({"tool": "docker", "argv": args}) + "\n")
images_path = os.path.join(state, "images.json")
with open(images_path, encoding="utf-8") as stream:
    images = json.load(stream)

def norm(reference):
    name, _, tag = reference.rpartition(":")
    if "/" not in name:
        name = "docker.io/library/" + name
    return name + ":" + tag

if args[:1] == ["info"]:
    print(json.dumps({"DockerRootDir": os.environ["EVAL_STUB_DOCKER_ROOT"], "DriverStatus": [],
                      "ID": "stub-daemon"}))
elif args[:2] == ["image", "inspect"]:
    image_id = images.get(norm(args[-1]))
    if image_id is None:
        print("Error response from daemon: No such image: " + args[-1], file=sys.stderr)
        sys.exit(1)
    print(json.dumps([{"Id": image_id, "Os": "linux", "Architecture": "amd64"}]))
elif args[:1] == ["load"]:
    archive = args[args.index("-i") + 1]
    with tarfile.open(archive) as bundle:
        manifest = json.load(bundle.extractfile("manifest.json"))
        for entry in manifest:
            config = bundle.extractfile(entry["Config"]).read()
            for tag in entry["RepoTags"]:
                images[norm(tag)] = "sha256:" + hashlib.sha256(config).hexdigest()
                print("Loaded image: " + tag)
    with open(images_path, "w", encoding="utf-8") as stream:
        json.dump(images, stream)
elif args[:1] == ["compose"]:
    watched = __WATCHED__
    record = {name: (None if name not in os.environ
                     else hashlib.sha256(os.environ[name].encode()).hexdigest())
              for name in watched}
    record["COMPOSE_PROFILES"] = os.environ.get("COMPOSE_PROFILES")
    record["COMPOSE_REMOVE_ORPHANS"] = os.environ.get("COMPOSE_REMOVE_ORPHANS")
    env_file = args[args.index("--env-file") + 1]
    record["env_file_empty"] = os.path.getsize(env_file) == 0
    with open(os.path.join(state, "compose-env.jsonl"), "a", encoding="utf-8") as log:
        log.write(json.dumps(record) + "\n")
    if "wait" in args:
        status = int(os.environ.get("EVAL_STUB_WAIT_EXIT", "0"))
        if status:
            print(f'container "stub" exited with status code {status}')
        sys.exit(status)
else:
    print("docker stub: unexpected call", file=sys.stderr)
    sys.exit(97)
'''

DF_STUB = r'''#!__PYTHON__
import json, os, sys
state = os.environ["EVAL_STUB_STATE"]
with open(os.path.join(state, "calls.jsonl"), "a", encoding="utf-8") as log:
    log.write(json.dumps({"tool": "df", "argv": sys.argv[1:]}) + "\n")
available = int(os.environ["EVAL_STUB_DF_KB"])
print("Filesystem     1024-blocks      Used Available Capacity Mounted on")
print(f"/dev/stub        999999999 100000000 {available}      10% {sys.argv[-1]}")
'''


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class EvalScriptCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ocu-eval-scripts-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"
        self.state.mkdir()
        (self.state / "images.json").write_text("{}", encoding="utf-8")
        self.docker_root = self.tmp / "docker-root"
        self.docker_root.mkdir()
        self.eval_root = self.tmp / "eval-root"
        self.eval_root.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        watched = [*STACK_NAMES, PROVIDER_KEY_VAR, "EVAL_ROOT", "EVAL_WEBUI_PORT",
                   *(RUNTIME_IMAGE_VARS[role] for role in EVAL_ROLES)]
        for name, body in (("docker", DOCKER_STUB), ("df", DF_STUB)):
            path = self.bin / name
            path.write_text(body.replace("__PYTHON__", sys.executable)
                            .replace("__WATCHED__", repr(watched)), encoding="utf-8")
            path.chmod(0o755)

    def environment(self, **extra) -> dict[str, str]:
        environment = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.tmp),
            "DOCKER_HOST": "unix://" + str(self.tmp / "stub.sock"),
            "EVAL_STUB_STATE": str(self.state),
            "EVAL_STUB_DOCKER_ROOT": str(self.docker_root),
            "EVAL_STUB_DF_KB": str(PLENTY_KB),
            "EVAL_ROOT": str(self.eval_root),
            "EVAL_WEBUI_PORT": WEBUI_PORT,
        }
        environment.update(extra)
        return {name: value for name, value in environment.items() if value is not None}

    def run_script(self, script: Path, *args: str, cwd: Path | None = None,
                   **extra) -> subprocess.CompletedProcess:
        result = subprocess.run(["bash", str(script), *args], cwd=cwd or self.tmp,
                                env=self.environment(**extra),
                                capture_output=True, text=True, timeout=120, check=False)
        # Before any assertion that could print stdout or stderr.
        self.assert_no_leak(result, self.planted_values())
        return result

    def planted_values(self) -> list[str]:
        values = list(SECRET_VALUES)
        stack = self.eval_root / "stack.env"
        if stack.is_file() and not stack.is_symlink():
            for line in stack.read_text(encoding="utf-8").splitlines():
                name, _, value = line.partition("=")
                if name != "ADMIN_EMAIL" and value:
                    values.append(value)
        return values

    def calls(self, tool: str | None = None) -> list[list[str]]:
        path = self.state / "calls.jsonl"
        if not path.exists():
            return []
        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return [entry["argv"] if tool else [entry["tool"], *entry["argv"]]
                for entry in entries if tool in (None, entry["tool"])]

    def compose_records(self) -> list[dict]:
        path = self.state / "compose-env.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def assert_no_leak(self, result: subprocess.CompletedProcess, values=SECRET_VALUES):
        recorded = json.dumps(self.calls())
        for value in values:
            self.assertFalse(value in result.stdout or value in result.stderr,
                             "a planted secret reached script output")
            self.assertFalse(value in recorded, "a planted secret reached a command line")

    def assert_no_forbidden_operation(self):
        for argv in self.calls("docker"):
            self.assertFalse(FORBIDDEN_TOKENS & set(argv), f"forbidden docker operation {argv}")

    def write_private(self, path: Path, text: str, mode: int = 0o600) -> Path:
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)
        return path

    def write_stack(self, values=None, mode: int = 0o600) -> Path:
        values = PLANTED if values is None else values
        body = "".join(f"{name}={value}\n" for name, value in values.items())
        return self.write_private(self.eval_root / "stack.env", body, mode)

    @property
    def key_path(self) -> Path:
        # HOME is self.tmp, so this is start.sh's default $HOME/.ocu-eval-dmxapi.key.
        return self.tmp / ".ocu-eval-dmxapi.key"

    def write_key(self, path: Path | None = None, mode: int = 0o600, text: str | None = None) -> Path:
        return self.write_private(path or self.key_path, PLANTED_KEY + "\n" if text is None else text,
                                  mode)

    def write_images(self, mode: int = 0o600) -> Path:
        body = "".join(f"{RUNTIME_IMAGE_VARS[role]}={DEFAULT_RELEASE_IMAGES[role]}\n"
                       for role in EVAL_ROLES)
        return self.write_private(self.eval_root / "images.env", body, mode)

    def seed_image(self, reference: str, image_id: str):
        path = self.state / "images.json"
        images = json.loads(path.read_text(encoding="utf-8"))
        name, _, tag = reference.rpartition(":")
        images[("docker.io/library/" + name if "/" not in name else name) + ":" + tag] = image_id
        path.write_text(json.dumps(images), encoding="utf-8")

    def make_package(self) -> Path:
        package = self.tmp / "package"
        images = {}
        for role in EVAL_ROLES:
            archive = write_image_archive(
                package / "images" / f"{role}.tar",
                {DEFAULT_RELEASE_IMAGES[role]: config_payload(DEFAULT_RELEASE_DIGESTS[role])},
            )
            images[role] = {"archive": {"path": f"images/{role}.tar",
                                        "sha256": sha256_hex(archive.read_bytes())}}
        write_inventory(package / "release.json",
                        synthetic_inventory(ocu_sha="a" * 40, webui_sha="b" * 40, images=images))
        return package


class EvalSecretsTests(EvalScriptCase):
    def parse(self, path: Path) -> dict[str, str]:
        values = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            values[name] = value
        return values

    def test_init_writes_private_alphanumeric_values_once(self):
        result = self.run_script(SECRETS, "init")
        self.assertEqual(result.returncode, 0, result.stderr)
        path = self.eval_root / "stack.env"
        info = path.lstat()
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertEqual(info.st_uid, os.getuid())
        values = self.parse(path)
        self.assertEqual(set(values), set(STACK_NAMES))
        self.assertEqual(values["ADMIN_EMAIL"], ADMIN_EMAIL)
        secrets = [values[name] for name in STACK_NAMES if name != "ADMIN_EMAIL"]
        for value in secrets:
            self.assertTrue(re.fullmatch(r"[A-Za-z0-9]{32,}", value), "secret is not a long alphanumeric")
        self.assertEqual(len(set(secrets)), len(secrets), "secrets must be independent")
        before = path.read_bytes()
        again = self.run_script(SECRETS, "init")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("exists", again.stderr)
        self.assertTrue(path.read_bytes() == before, "stack.env was overwritten")

    def test_init_output_starts_the_stack(self):
        self.assertEqual(self.run_script(SECRETS, "init").returncode, 0)
        self.write_key()
        self.write_images()
        result = self.run_script(START)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.compose_records()), 2)

    def test_init_refuses_a_symlink_or_missing_root(self):
        target = self.write_private(self.tmp / "elsewhere", "untouched\n")
        (self.eval_root / "stack.env").symlink_to(target)
        result = self.run_script(SECRETS, "init")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(encoding="utf-8"), "untouched\n")
        missing = self.run_script(SECRETS, "init", EVAL_ROOT=None)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("EVAL_ROOT", missing.stderr)
        usage = self.run_script(SECRETS)
        self.assertNotEqual(usage.returncode, 0)
        self.assertIn("usage: secrets.sh init", usage.stderr)


    def test_eval_root_writable_by_group_or_others_is_refused(self):
        for mode in (0o775, 0o757):
            with self.subTest(mode=oct(mode)):
                self.eval_root.chmod(mode)
                try:
                    result = self.run_script(SECRETS, "init")
                finally:
                    self.eval_root.chmod(0o700)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("EVAL_ROOT", result.stderr)
                self.assertIn("writable", result.stderr)
                self.assertFalse((self.eval_root / "stack.env").exists())

    def test_scripts_ignore_python_modules_in_the_working_directory(self):
        hostile = self.tmp / "hostile"
        hostile.mkdir()
        marker = self.tmp / "hostile-module-ran"
        for name in ("secrets", "json"):
            (hostile / f"{name}.py").write_text(
                f"open({str(marker)!r}, 'a').write({name!r} + '\\n')\n"
                f"raise RuntimeError('hostile {name} module imported')\n", encoding="utf-8")
        init = self.run_script(SECRETS, "init", cwd=hostile)
        self.assertEqual(init.returncode, 0, init.stderr)
        self.write_key()
        self.write_images()
        start = self.run_script(START, cwd=hostile)
        self.assertEqual(start.returncode, 0, start.stderr)
        self.assertFalse(marker.exists(), "a module from the working directory was imported")


class EvalLoadTests(EvalScriptCase):
    def test_loads_absent_roles_binds_variables_and_records_new_references(self):
        package = self.make_package()
        record = synthetic_inventory(ocu_sha="a" * 40, webui_sha="b" * 40)["images"]["postgres"]
        self.seed_image(DEFAULT_RELEASE_IMAGES["postgres"], record["configuration_digest"])
        result = self.run_script(LOAD, str(package))
        self.assertEqual(result.returncode, 0, result.stderr)
        loads = [argv for argv in self.calls("docker") if argv[:1] == ["load"]]
        self.assertEqual(sorted(argv[-1] for argv in loads),
                         sorted(str(package.resolve() / "images" / f"{role}.tar")
                                for role in EVAL_ROLES if role != "postgres"))
        recorded = (self.eval_root / "loaded-images.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(sorted(recorded), sorted(DEFAULT_RELEASE_IMAGES[role]
                                                  for role in EVAL_ROLES if role != "postgres"))
        images = self.eval_root / "images.env"
        self.assertEqual(stat.S_IMODE(images.lstat().st_mode), 0o600)
        self.assertEqual(images.read_text(encoding="utf-8").splitlines(), [
            f"{RUNTIME_IMAGE_VARS[role]}={DEFAULT_RELEASE_IMAGES[role]}" for role in EVAL_ROLES])
        # The free-space check runs on Docker's root before every load.
        sequence = self.calls()
        for index, argv in enumerate(sequence):
            if argv[:2] == ["docker", "load"]:
                previous = [entry[0] for entry in sequence[:index]
                            if entry[0] == "df" or entry[:2] == ["docker", "load"]]
                self.assertEqual(previous[-1], "df", "a load ran without a preceding free-space check")
        for argv in self.calls("df"):
            self.assertEqual(argv[-1], str(self.docker_root))
        self.assert_no_forbidden_operation()

    def test_low_free_space_refuses_before_any_load(self):
        package = self.make_package()
        result = self.run_script(LOAD, str(package), EVAL_STUB_DF_KB=str(LOW_KB))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(LOW_KB * 1024), result.stderr)
        self.assertIn(str(FLOOR_BYTES), result.stderr)
        self.assertFalse([argv for argv in self.calls("docker") if argv[:1] == ["load"]])
        self.assertFalse((self.eval_root / "images.env").exists())

    def test_archive_that_does_not_fit_above_the_floor_is_not_loaded(self):
        package = self.make_package()
        # Above the plain floor, below floor + 3 x the first archive (workspace).
        free_kb = FLOOR_BYTES // 1024 + 1
        needed = 3 * (package / "images" / "workspace.tar").stat().st_size
        self.assertGreater(needed, 1024)
        result = self.run_script(LOAD, str(package), EVAL_STUB_DF_KB=str(free_kb))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(free_kb * 1024), result.stderr)
        self.assertIn(str(needed), result.stderr)
        self.assertIn(str(FLOOR_BYTES), result.stderr)
        self.assertFalse([argv for argv in self.calls("docker") if argv[:1] == ["load"]])
        self.assertFalse((self.eval_root / "images.env").exists())

    def test_checksum_mismatch_refuses_before_any_load(self):
        package = self.make_package()
        archive = package / "images" / "open-webui.tar"
        archive.write_bytes(archive.read_bytes() + b"\0" * 512)
        result = self.run_script(LOAD, str(package))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum", result.stderr)
        self.assertFalse([argv for argv in self.calls("docker") if argv[:1] == ["load"]])
        self.assertFalse((self.eval_root / "loaded-images.txt").exists())
        self.assertFalse((self.eval_root / "images.env").exists())

    def test_existing_reference_with_another_id_is_never_moved(self):
        package = self.make_package()
        self.seed_image(DEFAULT_RELEASE_IMAGES["open-webui"], "sha256:" + "c" * 64)
        result = self.run_script(LOAD, str(package))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"existing image {DEFAULT_RELEASE_IMAGES['open-webui']} conflicts with the release",
                      result.stderr)
        self.assertFalse([argv for argv in self.calls("docker") if argv[:1] == ["load"]])
        self.assertFalse((self.eval_root / "loaded-images.txt").exists())

    def test_requires_a_package_argument(self):
        result = self.run_script(LOAD)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("usage: load.sh", result.stderr)
        self.assertFalse(self.calls("docker"))


class EvalStartStopTests(EvalScriptCase):
    def assert_compose(self, actions: list[list[str]]) -> dict:
        composes = [argv for argv in self.calls("docker") if argv[:1] == ["compose"]]
        self.assertEqual([argv[13:] for argv in composes], actions)
        for argv in composes:
            self.assertEqual(argv[:5], ["compose", "-p", PROJECT, "--project-directory", str(ROOT)])
            self.assertEqual(argv[5], "--env-file")
            self.assertEqual(argv[7:13], ["-f", str(ROOT / "docker-compose.yml"),
                                          "-f", str(ROOT / "docker-compose.webui.yml"),
                                          "-f", str(COMPOSE_EVAL)])
        record = self.compose_records()[0]
        self.assertIs(record["env_file_empty"], True)
        self.assertEqual(record["COMPOSE_PROFILES"], "")
        self.assertEqual(record["COMPOSE_REMOVE_ORPHANS"], "false")
        for name, value in PLANTED.items():
            self.assertTrue(record[name] == sha256_hex(value.encode()), f"{name} not exported")
        for role in EVAL_ROLES:
            name = RUNTIME_IMAGE_VARS[role]
            self.assertTrue(record[name] == sha256_hex(DEFAULT_RELEASE_IMAGES[role].encode()), name)
        return record

    def test_start_exports_files_and_brings_the_project_up(self):
        self.write_stack()
        self.write_key()
        self.write_images()
        result = self.run_script(START)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.assert_compose([["up", "-d", "--no-build", "--pull", "never"],
                                      ["wait", "open-webui-init"]])
        self.assertTrue(record[PROVIDER_KEY_VAR] == sha256_hex(PLANTED_KEY.encode()),
                        "provider key not exported")
        # The spec's key-leak check searches every file under EVAL_ROOT.
        for path in self.eval_root.rglob("*"):
            if path.is_file():
                self.assertFalse(PLANTED_KEY.encode() in path.read_bytes(),
                                 f"the provider key is stored under EVAL_ROOT: {path.name}")
        self.assertTrue((self.eval_root / "data").is_dir())
        self.assertEqual(self.calls("df")[0][-1], str(self.docker_root))
        self.assert_no_forbidden_operation()

    def test_start_reads_the_key_file_named_by_eval_key_file(self):
        self.write_stack()
        self.write_images()
        key = self.write_key(self.tmp / "provider.key")
        result = self.run_script(START, EVAL_KEY_FILE=str(key))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.compose_records()[0][PROVIDER_KEY_VAR]
                        == sha256_hex(PLANTED_KEY.encode()), "provider key not exported")

    def test_start_fails_naming_the_initializer_when_it_fails(self):
        self.write_stack()
        self.write_key()
        self.write_images()
        result = self.run_script(START, EVAL_STUB_WAIT_EXIT="3")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("open-webui-init", result.stderr)
        self.assertIn("status 3", result.stderr)
        self.assert_compose([["up", "-d", "--no-build", "--pull", "never"],
                             ["wait", "open-webui-init"]])

    def test_start_refuses_an_empty_or_multi_line_key(self):
        self.write_stack()
        self.write_images()
        for case, text in (("empty", ""), ("two-line", PLANTED_KEY + "\nfake-second-line\n")):
            with self.subTest(case=case):
                self.write_key(text=text)
                (self.state / "calls.jsonl").unlink(missing_ok=True)
                result = self.run_script(START)
                self.assertFalse("fake-second-line" in result.stdout + result.stderr,
                                 "key file content reached script output")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(str(self.key_path), result.stderr)
                self.assertFalse([argv for argv in self.calls("docker") if argv[:1] == ["compose"]])

    def test_start_refuses_low_free_space(self):
        self.write_stack()
        self.write_key()
        self.write_images()
        result = self.run_script(START, EVAL_STUB_DF_KB=str(LOW_KB))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(LOW_KB * 1024), result.stderr)
        self.assertIn(str(FLOOR_BYTES), result.stderr)
        self.assertFalse([argv for argv in self.calls("docker") if argv[:1] == ["compose"]])

    def test_start_refuses_unsafe_or_missing_private_files(self):
        writers = {"stack.env": self.write_stack, ".ocu-eval-dmxapi.key": self.write_key,
                   "images.env": self.write_images}
        paths = {"stack.env": self.eval_root / "stack.env", ".ocu-eval-dmxapi.key": self.key_path,
                 "images.env": self.eval_root / "images.env"}
        for target in writers:
            for case in ("mode-0644", "symlink", "missing"):
                with self.subTest(target=target, case=case):
                    for path in paths.values():
                        path.unlink(missing_ok=True)
                    for name, writer in writers.items():
                        if name != target:
                            writer()
                    path = paths[target]
                    if case == "mode-0644":
                        writers[target](mode=0o644)
                    elif case == "symlink":
                        real = writers[target]()
                        moved = real.rename(self.tmp / ("real-" + target))
                        path.symlink_to(moved)
                    (self.state / "calls.jsonl").unlink(missing_ok=True)
                    result = self.run_script(START)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(target, result.stderr)
                    self.assertFalse([argv for argv in self.calls("docker") if argv[:1] == ["compose"]])
                    (self.tmp / ("real-" + target)).unlink(missing_ok=True)

    def test_start_refuses_a_malformed_stack_file_without_echoing_it(self):
        values = dict(PLANTED)
        values["POSTGRES_PASSWORD"] = PLANTED["POSTGRES_PASSWORD"] + "'$(x)"
        self.write_stack(values)
        self.write_key()
        self.write_images()
        result = self.run_script(START)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stack.env", result.stderr)
        self.assertFalse([argv for argv in self.calls("docker") if argv[:1] == ["compose"]])

    def test_start_requires_eval_root_and_port(self):
        for name in ("EVAL_ROOT", "EVAL_WEBUI_PORT"):
            with self.subTest(name=name):
                result = self.run_script(START, **{name: None})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(name, result.stderr)
                self.assertFalse(self.calls("docker"))

    def test_stop_runs_down_without_the_key_file(self):
        self.write_stack()
        self.write_images()
        result = self.run_script(STOP)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_compose([["down"]])
        self.assert_no_forbidden_operation()

    def test_stop_refuses_an_unsafe_stack_file(self):
        self.write_stack(mode=0o644)
        self.write_images()
        result = self.run_script(STOP)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stack.env", result.stderr)
        self.assertFalse(self.calls("docker"))


if __name__ == "__main__":
    unittest.main()
