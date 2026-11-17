# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""cleanup.sh (#818): exact-name removal of the ocu-eval objects, the lock, idempotence.

A recording docker stub holds containers, volumes and images in a state file and
fails any argv listed in fail.json. When flock(1) is absent (macOS), a shim that
takes the same flock(2) lock on the inherited descriptor stands in for it; the
evaluation host has util-linux flock.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

from support import DEFAULT_RELEASE_IMAGES, ROOT, RUNTIME_IMAGE_VARS

EVAL_DIR = ROOT / "deploy" / "eval"
CLEANUP = EVAL_DIR / "cleanup.sh"
DRIVER_PATH = EVAL_DIR / "driver.py"
ROLES = ("workspace", "computer-use-server", "open-webui", "postgres")
WORKSPACE = DEFAULT_RELEASE_IMAGES["workspace"]
STACK = {
    "OCU_INTERNAL_TOKEN": "fake-cln-internal-token", "MCP_API_KEY": "fake-cln-mcp-key",
    "WEBUI_SECRET_KEY": "fake-cln-webui-secret", "POSTGRES_PASSWORD": "fake-cln-postgres-password",
    "ADMIN_EMAIL": "eval-admin@ocu-eval.local", "ADMIN_PASSWORD": "fake-cln-admin-password",
    "ADAPTER_TOKEN": "fake-cln-adapter-token",
}
PROJECT_LABEL = {"com.docker.compose.project": "ocu-eval"}
REAL_FLOCK = shutil.which("flock")

DOCKER_STUB = r'''#!__PYTHON__
import json, os, shutil, sys
state = os.environ["EVAL_STUB_STATE"]
args = sys.argv[1:]
path = os.path.join(state, "docker.json")
with open(path, encoding="utf-8") as stream:
    world = json.load(stream)
with open(os.path.join(state, "calls.jsonl"), "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\n")
def save():
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(world, stream)
def fail(message, code=1):
    print(message, file=sys.stderr)
    sys.exit(code)
if args in world["fail"]:
    fail("injected failure")
def option(name):
    return args[args.index(name) + 1]
def image_matches(image, name):
    return name in (image["ref"], image["id"])
if args[:1] == ["compose"]:
    if world.get("compose_fails"):
        fail("compose down failed")
elif args[:2] == ["network", "ls"]:
    for name in world.get("compose_networks", []):
        print(name)
elif args[:2] == ["container", "ls"] and option("--filter").startswith("label="):
    for name in world.get("compose_containers", []):
        print(name)
elif args[:2] == ["container", "ls"]:
    wanted = option("--filter").removeprefix("name=")
    for name in world["containers"]:
        if wanted in name:
            print(name)
elif args[:2] == ["rm", "-f"]:
    if args[2] not in world["containers"]:
        fail("No such container")
    world["containers"].remove(args[2]); save()
elif args[:2] == ["volume", "ls"]:
    key, _, value = option("--filter").partition("=")
    for name, labels in world["volumes"].items():
        if key == "name" and value in name:
            print(name)
        if key == "label" and value.partition("=")[0] in labels and labels[value.partition("=")[0]] == value.partition("=")[2]:
            print(name)
elif args[:2] == ["volume", "rm"]:
    if args[2] not in world["volumes"]:
        fail("No such volume")
    del world["volumes"][args[2]]; save()
elif args[:1] == ["run"]:
    image = args[args.index("--entrypoint") + 4]
    if not any(image_matches(entry, image) for entry in world["images"]):
        fail("Unable to find image " + image, 125)
    source = option("-v").rsplit(":", 1)[0]
    shutil.rmtree(os.path.join(source, args[-1].removeprefix("/d/")), ignore_errors=True)
elif args[:2] == ["image", "ls"] and "--filter" in args:
    label = option("--filter").removeprefix("label=")
    for image in world["images"]:
        if label in image["labels"]:
            print((image["ref"] or "<none>:<none>") + " " + image["id"])
elif args[:2] == ["image", "ls"]:
    for image in world["images"]:
        if image_matches(image, args[-1]):
            print(image["id"])
elif args[:2] == ["image", "rm"]:
    found = [image for image in world["images"] if image_matches(image, args[-1])]
    if not found:
        fail("No such image")
    world["images"].remove(found[0]); save()
else:
    fail("docker stub: unexpected call", 97)
'''

# flock(1) -n <fd>: the same flock(2) lock on the descriptor the shell opened.
FLOCK_SHIM = r'''#!__PYTHON__
import fcntl, sys
if sys.argv[1:] != ["-n", "9"]:
    sys.exit("flock shim: unexpected arguments")
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(1)
'''


class CleanupCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ocu-eval-cleanup-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.eval_root = self.tmp / "eval-root"
        self.eval_root.mkdir(mode=0o700)
        self.data = self.eval_root / "data"
        self.data.mkdir()
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        stubs = {"docker": DOCKER_STUB}
        if REAL_FLOCK is None:
            stubs["flock"] = FLOCK_SHIM
        for name, body in stubs.items():
            path = self.bin / name
            path.write_text(body.replace("__PYTHON__", sys.executable), encoding="utf-8")
            path.chmod(0o755)
        self.ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        self.loaded = [DEFAULT_RELEASE_IMAGES["computer-use-server"], DEFAULT_RELEASE_IMAGES["open-webui"]]
        self.world = {
            "containers": [f"owui-chat-{self.ids[0]}", "other-project-web", "owui-chat-foreign"],
            "volumes": {
                f"chat-{self.ids[0]}-workspace": {},
                "chat-foreign-workspace": {},
                "ocu-eval_open-webui-data": PROJECT_LABEL,
                "ocu-eval_postgres-data": PROJECT_LABEL,
                "renamed-by-hand": PROJECT_LABEL,
                "other_data": {"com.docker.compose.project": "other"},
            },
            "images": [
                {"ref": WORKSPACE, "id": "sha256:ws", "labels": {}},
                {"ref": self.loaded[0], "id": "sha256:cus", "labels": {}},
                {"ref": self.loaded[1], "id": "sha256:owui", "labels": {"ocu-eval": "1"}},
                {"ref": None, "id": "sha256:dangling", "labels": {"ocu-eval": "1"}},
                {"ref": "postgres:17-alpine", "id": "sha256:pg", "labels": {}},
                {"ref": "other:latest", "id": "sha256:other", "labels": {}},
            ],
            "fail": [],
        }
        self.save_world()
        for chat_id in self.ids:
            (self.data / chat_id / "outputs").mkdir(parents=True)
            (self.data / chat_id / "outputs" / "video.mp4").write_bytes(b"mp4")
        (self.data / "foreign").mkdir()
        self.write_private(self.eval_root / "chats.txt", "".join(f"{i}\n" for i in self.ids))
        self.write_private(self.eval_root / "stack.env",
                           "".join(f"{name}={value}\n" for name, value in STACK.items()))
        # images.env binds the release references; loaded-images.txt lists the two this project loaded.
        self.write_private(self.eval_root / "images.env",
                           "".join(f"{RUNTIME_IMAGE_VARS[role]}={DEFAULT_RELEASE_IMAGES[role]}\n"
                                   for role in ROLES))
        self.write_private(self.eval_root / "loaded-images.txt", "".join(f"{r}\n" for r in self.loaded))
        for name in ("runs", "src", "packages"):
            (self.eval_root / name).mkdir()
            (self.eval_root / name / "keep").write_text("keep", encoding="utf-8")

    def write_private(self, path: Path, text: str):
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)

    def save_world(self):
        (self.state / "docker.json").write_text(json.dumps(self.world), encoding="utf-8")

    def load_world(self) -> dict:
        return json.loads((self.state / "docker.json").read_text(encoding="utf-8"))

    def fail_on(self, *argvs: list[str]):
        self.world = self.load_world()
        self.world["fail"] = [list(argv) for argv in argvs]
        self.save_world()

    def environment(self) -> dict[str, str]:
        return {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.tmp),
            "EVAL_STUB_STATE": str(self.state),
            "EVAL_ROOT": str(self.eval_root),
            "EVAL_WEBUI_PORT": "38080",
        }

    def cleanup(self, *args: str) -> subprocess.CompletedProcess:
        calls = self.state / "calls.jsonl"
        if calls.exists():
            calls.unlink()
        result = subprocess.run(["bash", str(CLEANUP), *args], cwd=self.tmp, env=self.environment(),
                                capture_output=True, text=True, timeout=120, check=False)
        for value in STACK.values():
            if value != STACK["ADMIN_EMAIL"]:
                self.assertNotIn(value, result.stdout + result.stderr)
        self.assert_safe()
        return result

    def calls(self) -> list[list[str]]:
        path = self.state / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def mutations(self) -> list[list[str]]:
        """Every call that changes the world: compose, rm, run, volume rm, image rm."""
        return [argv for argv in self.calls()
                if argv[:1] in (["compose"], ["rm"], ["run"]) or argv[:2] in (["volume", "rm"],
                                                                                ["image", "rm"])]

    def per_id(self, chat_id: str, container=True, volume=True) -> list[list[str]]:
        steps = []
        if container:
            steps.append(["rm", "-f", f"owui-chat-{chat_id}"])
        if volume:
            steps.append(["volume", "rm", f"chat-{chat_id}-workspace"])
        steps.append(["run", "--rm", "--network", "none", "--user", "0:0", "--entrypoint", "rm",
                      "-v", f"{self.eval_root}/data:/d", WORKSPACE, "-rf", "--", f"/d/{chat_id}"])
        return steps

    def assert_safe(self):
        computed = ({f"owui-chat-{i}" for i in self.ids} | {f"chat-{i}-workspace" for i in self.ids}
                    | {"ocu-eval_open-webui-data", "ocu-eval_postgres-data"}
                    | set(self.loaded) | {"sha256:dangling"})
        for argv in self.calls():
            self.assertFalse({"prune", "system"} & set(argv), argv)
            if argv[:2] == ["image", "rm"]:
                self.assertFalse({"-f", "--force"} & set(argv), argv)
            if argv[:2] in (["image", "rm"], ["volume", "rm"]) or argv[:1] == ["rm"]:
                self.assertIn(argv[-1], computed, f"removal of a name cleanup did not compute: {argv}")
            if argv[:1] == ["run"]:
                self.assertIn(argv[-1], {f"/d/{i}" for i in self.ids}, argv)

    def ledger(self) -> list[str]:
        return (self.eval_root / "chats.txt").read_text(encoding="utf-8").splitlines()

    def assert_kept(self):
        for name in ("runs", "src", "packages"):
            self.assertTrue((self.eval_root / name / "keep").is_file(), name)
        self.assertTrue((self.eval_root / "stack.env").is_file())
        self.assertTrue((self.data / "foreign").is_dir())
        world = self.load_world()
        self.assertIn("other-project-web", world["containers"])
        self.assertIn("owui-chat-foreign", world["containers"])
        self.assertIn("chat-foreign-workspace", world["volumes"])
        self.assertIn("other_data", world["volumes"])
        self.assertIn("renamed-by-hand", world["volumes"])
        refs = {image["ref"] for image in world["images"]}
        self.assertTrue({"other:latest", "postgres:17-alpine", WORKSPACE} <= refs)


class CleanupLedgerTests(CleanupCase):
    def test_malformed_ledger_line_refuses_without_any_docker_command(self):
        for bad in (self.ids[1].upper(), "not-a-uuid", "", f"{self.ids[1]} "):
            with self.subTest(bad=bad):
                self.write_private(self.eval_root / "chats.txt", f"{self.ids[0]}\n{bad}\n")
                result = self.cleanup()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("chats.txt:2", result.stderr)
                self.assertEqual(self.calls(), [])

    def test_symlinked_ledger_refuses_without_any_docker_command(self):
        target = self.tmp / "elsewhere.txt"
        target.write_text(self.ids[0] + "\n", encoding="utf-8")
        (self.eval_root / "chats.txt").unlink()
        (self.eval_root / "chats.txt").symlink_to(target)
        result = self.cleanup("--runs-only")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_runs_only_removes_exactly_the_ledger_objects(self):
        # Appended by hand under umask 022: the ledger holds ids only, so 0644 is accepted.
        (self.eval_root / "chats.txt").chmod(0o644)
        result = self.cleanup("--runs-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.mutations(), [*self.per_id(self.ids[0]),
                                            *self.per_id(self.ids[1], container=False, volume=False)])
        for chat_id in self.ids:
            self.assertFalse((self.data / chat_id).exists())
        self.assertEqual(self.ledger(), [])
        self.assertTrue((self.eval_root / "images.env").is_file())
        self.assertTrue((self.eval_root / "loaded-images.txt").is_file())
        world = self.load_world()
        self.assertIn("ocu-eval_postgres-data", world["volumes"])
        self.assertEqual(len(world["images"]), 6)
        self.assert_kept()

    def test_absent_objects_are_skipped(self):
        self.world["containers"].remove(f"owui-chat-{self.ids[0]}")
        del self.world["volumes"][f"chat-{self.ids[0]}-workspace"]
        self.world["images"] = [image for image in self.world["images"] if image["ref"] != self.loaded[0]]
        self.save_world()
        shutil.rmtree(self.data / self.ids[1])
        result = self.cleanup()
        self.assertEqual(result.returncode, 0, result.stderr)
        removed = [argv for argv in self.mutations() if argv[:1] != ["compose"]]
        self.assertEqual(removed, [
            *self.per_id(self.ids[0], container=False, volume=False),
            ["volume", "rm", "ocu-eval_open-webui-data"], ["volume", "rm", "ocu-eval_postgres-data"],
            ["image", "rm", self.loaded[1]], ["image", "rm", "sha256:dangling"]])
        self.assertEqual(self.ledger(), [])


class CleanupFullTests(CleanupCase):
    def test_full_mode_issues_exactly_the_project_removals_in_order(self):
        result = self.cleanup()
        self.assertEqual(result.returncode, 0, result.stderr)
        mutations = self.mutations()
        compose = mutations[0]
        self.assertEqual(compose[:3], ["compose", "-p", "ocu-eval"])
        self.assertEqual(compose[-1], "down")
        self.assertEqual(mutations[1:], [
            *self.per_id(self.ids[0]),
            *self.per_id(self.ids[1], container=False, volume=False),
            ["volume", "rm", "ocu-eval_open-webui-data"],
            ["volume", "rm", "ocu-eval_postgres-data"],
            ["image", "rm", self.loaded[0]],
            ["image", "rm", self.loaded[1]],
            ["image", "rm", "sha256:dangling"],
        ])
        self.assertEqual(self.ledger(), [])
        self.assertFalse((self.eval_root / "images.env").exists())
        self.assertFalse((self.eval_root / "loaded-images.txt").exists())
        world = self.load_world()
        self.assertEqual({image["ref"] for image in world["images"]},
                         {WORKSPACE, "postgres:17-alpine", "other:latest"})
        self.assert_kept()

    def test_failed_volume_removal_keeps_its_id_skips_images_and_a_second_run_finishes(self):
        self.fail_on(["volume", "rm", f"chat-{self.ids[0]}-workspace"])
        result = self.cleanup()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.ledger(), [self.ids[0]])
        self.assertFalse((self.data / self.ids[1]).exists())
        self.assertEqual([argv for argv in self.calls() if argv[:2] == ["image", "rm"]], [])
        self.assertTrue((self.eval_root / "images.env").is_file())
        self.assertTrue((self.eval_root / "loaded-images.txt").is_file())
        self.fail_on()
        second = self.cleanup()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.ledger(), [])
        self.assertNotIn(f"chat-{self.ids[0]}-workspace", self.load_world()["volumes"])
        self.assertFalse((self.eval_root / "images.env").exists())

    def test_failed_helper_issues_no_image_removal(self):
        self.fail_on(self.per_id(self.ids[1], container=False, volume=False)[0])
        result = self.cleanup()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([argv for argv in self.calls() if argv[:2] == ["image", "rm"]], [])
        self.assertEqual(self.ledger(), [self.ids[1]])
        self.assertTrue((self.data / self.ids[1]).exists())
        self.assertIn(WORKSPACE, {image["ref"] for image in self.load_world()["images"]})
        self.assertTrue((self.eval_root / "images.env").is_file())
        self.fail_on()
        second = self.cleanup()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertFalse((self.data / self.ids[1]).exists())
        self.assertEqual(self.ledger(), [])

    def test_failed_image_removal_keeps_the_bind_files(self):
        self.fail_on(["image", "rm", self.loaded[0]])
        result = self.cleanup()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.ledger(), [])
        self.assertTrue((self.eval_root / "images.env").is_file())
        self.assertTrue((self.eval_root / "loaded-images.txt").is_file())
        self.assertNotIn("sha256:dangling", {image["id"] for image in self.load_world()["images"]})
        self.fail_on()
        second = self.cleanup()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertFalse((self.eval_root / "images.env").exists())
        self.assertFalse((self.eval_root / "loaded-images.txt").exists())

    def test_a_run_after_a_complete_cleanup_succeeds_without_compose(self):
        self.assertEqual(self.cleanup().returncode, 0)
        again = self.cleanup()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.mutations(), [])
        self.assertIn("images.env", again.stdout)

    def test_failed_compose_down_keeps_the_bind_files_and_fails(self):
        self.world["compose_fails"] = True
        self.save_world()
        result = self.cleanup()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.eval_root / "images.env").is_file())
        self.assertTrue((self.eval_root / "loaded-images.txt").is_file())
        self.assertEqual([argv for argv in self.calls() if argv[:2] == ["image", "rm"]], [])

    def test_without_images_env_a_remaining_project_network_fails(self):
        self.assertEqual(self.cleanup().returncode, 0)
        self.world = self.load_world()
        self.world["compose_networks"] = ["ocu-eval_default"]
        self.save_world()
        again = self.cleanup()
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("ocu-eval_default", again.stderr)
        self.assertEqual(self.mutations(), [])

    def test_without_images_env_a_remaining_project_container_fails(self):
        self.assertEqual(self.cleanup().returncode, 0)
        self.world = self.load_world()
        self.world["compose_containers"] = ["ocu-eval-postgres"]
        self.save_world()
        again = self.cleanup()
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("ocu-eval objects remain", again.stderr)
        self.assertIn("ocu-eval-postgres", again.stderr)
        self.assertEqual(self.mutations(), [])


class CleanupLockTests(CleanupCase):
    def test_cleanup_refuses_while_a_driver_holds_the_lock_and_proceeds_after(self):
        """The driver's fcntl lock against cleanup's flock -n; real flock(1) wherever it exists."""
        holder = subprocess.Popen(
            [sys.executable, "-I", "-c",
             "import importlib.util, sys\n"
             "spec = importlib.util.spec_from_file_location('ocu_eval_driver_lock', sys.argv[1])\n"
             "driver = importlib.util.module_from_spec(spec); spec.loader.exec_module(driver)\n"
             "from pathlib import Path\n"
             "driver.take_lock(Path(sys.argv[2]))\n"
             "print('locked', flush=True)\n"
             "sys.stdin.read()\n",
             str(DRIVER_PATH), str(self.eval_root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            refused = self.cleanup("--runs-only")
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("eval.lock", refused.stderr)
            self.assertEqual(self.calls(), [])
        finally:
            holder.stdin.close()
            holder.wait(timeout=30)
            holder.stdout.close()
        proceeded = self.cleanup("--runs-only")
        self.assertEqual(proceeded.returncode, 0, proceeded.stderr)
        self.assertEqual(self.ledger(), [])


if __name__ == "__main__":
    unittest.main()
