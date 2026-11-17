# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""The evaluation driver (#818): request body, ledger, preflight, waiting, outcomes and redaction.

The driver runs against a fake WebUI on 127.0.0.1 and recording stubs for docker
and df on PATH. Most tests import driver.py in-process so the poll and retry
intervals can be shortened; the run.sh tests run the real entry point.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest import mock
import urllib.error
import uuid

from test_eval_cleanup import FLOCK_SHIM
from support import (
    DEFAULT_RELEASE_IMAGES,
    ROOT,
    RUNTIME_IMAGE_VARS,
    synthetic_inventory,
    write_inventory,
)

EVAL_DIR = ROOT / "deploy" / "eval"
DRIVER_PATH = EVAL_DIR / "driver.py"
RUN = EVAL_DIR / "run.sh"
REQUESTS = json.loads((EVAL_DIR / "requests.json").read_text(encoding="utf-8"))["requests"]
FIRST = REQUESTS[0]
MODEL = "qwen3.8-27b"
ROLES = ("workspace", "computer-use-server", "open-webui", "postgres")
STACK_NAMES = ("OCU_INTERNAL_TOKEN", "MCP_API_KEY", "WEBUI_SECRET_KEY", "POSTGRES_PASSWORD",
               "ADMIN_EMAIL", "ADMIN_PASSWORD", "ADAPTER_TOKEN")
ADMIN_EMAIL = "eval-admin@ocu-eval.local"
STACK = {
    "OCU_INTERNAL_TOKEN": "fake-drv-internal-token",
    "MCP_API_KEY": "fake-drv-mcp-key",
    "WEBUI_SECRET_KEY": "fake-drv-webui-secret",
    "POSTGRES_PASSWORD": "fake-drv-postgres-password",
    "ADMIN_EMAIL": ADMIN_EMAIL,
    "ADMIN_PASSWORD": "fake-drv-admin-password",
    "ADAPTER_TOKEN": "fake-drv-adapter-token",
}
# Distinct from every stack value, so dropping it from the redaction set is visible.
PLANTED_KEY = "fake-drv-provider-key"
SESSION_PREFIX = "fake-drv-session-token-"
IMAGE_IDS = {role: "sha256:" + hashlib.sha256(role.encode()).hexdigest() for role in ROLES}
FLOOR_BYTES = 15 * 1024 ** 3
PLENTY_KB = 40 * 1024 ** 2
LOW_KB = 10 * 1024 ** 2
VALID_PROBE = {"streams": [{"codec_type": "video", "codec_name": "h264"},
                           {"codec_type": "audio", "codec_name": "aac"}],
               "format": {"duration": "12.500000"}}
MANIFEST_KEYS = {
    "version", "phase", "request_id", "kind", "repetition", "attempt", "chat_id", "release",
    "webui_image_id", "models", "started_at", "elapsed_seconds", "outcome", "failure_class",
    "failure_reason", "check", "counts", "mp4", "skipped_outputs"}
MP4_BYTES = b"\x00\x00\x00\x18ftypmp42fake-mp4-body"


def load_driver():
    spec = importlib.util.spec_from_file_location("ocu_eval_driver_under_test", DRIVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DRIVER = load_driver()


DOCKER_STUB = r'''#!__PYTHON__
import json, os, sys
state = os.environ["EVAL_STUB_STATE"]
args = sys.argv[1:]
def load(name, default=None):
    path = os.path.join(state, name)
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)
secrets = load("secrets.json", [])
names = load("watched.json", [])
with open(os.path.join(state, "calls.jsonl"), "a", encoding="utf-8") as log:
    log.write(json.dumps({"tool": "docker", "argv": args,
                          "env_names": sorted(name for name in names if name in os.environ),
                          "env_secret": any(value in os.environ.get(name, "") for value in secrets
                                            for name in os.environ)}) + "\n")
if args[:1] == ["info"]:
    print(json.dumps({"DockerRootDir": os.environ["EVAL_STUB_DOCKER_ROOT"], "ID": "stub-daemon"}))
elif args[:2] == ["image", "inspect"]:
    image = load("images.json", {}).get(args[-1])
    if image is None:
        print("Error: No such image: " + args[-1], file=sys.stderr)
        sys.exit(1)
    print(image)
elif args[:2] == ["container", "inspect"]:
    container = load("containers.json", {}).get(args[-1])
    if container is None:
        print("Error: No such container: " + args[-1], file=sys.stderr)
        sys.exit(1)
    print(("true" if container["running"] else "false") + " " + container["image"])
elif args == ["exec", "ocu-eval-computer-use-server", "printenv", "TTS_MODEL"]:
    print("mimo-v2.5-tts")
elif args[:1] == ["run"] and "ffprobe" in args:
    source = args[args.index("-v") + 1].split(":")[0]
    name = args[-1].rsplit("/", 1)[1]
    if not os.path.isfile(os.path.join(source, name)):
        print("No such file", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(load("probes.json", {}).get(name, __VALID__)))
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


def call(name, call_id, arguments="{}", status="completed"):
    return {"type": "function_call", "name": name, "call_id": call_id, "arguments": arguments,
            "status": status}


def result(call_id, text):
    return {"type": "function_call_output", "call_id": call_id,
            "output": [{"type": "input_text", "text": text}], "status": "completed"}


def scenario(**overrides):
    """One fake chat: the assistant message the WebUI ends with, and how its task behaves."""
    base = {
        "message": {"done": True, "content": "done",
                    "output": [call("bash_tool", "c1"), result("c1", "rendered")]},
        "busy_polls": 0,          # polls that still list a task before the list empties
        "until_stop": False,      # the task list empties only after the stop endpoint
        "tasks_status": None,     # an HTTP status every task-list GET answers with
        "chat_status": None,      # an HTTP status the chat GET answers with
        "drop_answer": False,     # create the chat, then close the POST without answering
        "post_status": None,      # create the chat, then answer the POST with this HTTP status
        "forget": False,          # drop the chat right after creating it: later GETs do not find it
        "outputs": {"video.mp4": MP4_BYTES},
    }
    base.update(overrides)
    return base


class FakeWebUI(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, eval_root: Path):
        super().__init__(("127.0.0.1", 0), FakeHandler)
        self.eval_root = eval_root
        self.lock = threading.Lock()
        self.models = [{"id": MODEL, "name": MODEL,
                        "filters": [{"id": "video_production_filter", "name": "Video"}]}]
        self.functions = [
            {"id": "computer_use_filter", "type": "filter", "is_active": True, "is_global": True},
            {"id": "video_production_filter", "type": "filter", "is_active": True, "is_global": True},
        ]
        # The real answer holds the adapter token as both image keys.
        self.image_config = {
            "ENABLE_IMAGE_GENERATION": True, "ENABLE_IMAGE_EDIT": True,
            "IMAGE_GENERATION_ENGINE": "openai", "IMAGE_EDIT_ENGINE": "openai",
            "IMAGES_OPENAI_API_BASE_URL": "http://adapter:8090/v1",
            "IMAGES_EDIT_OPENAI_API_BASE_URL": "http://adapter:8090/v1",
            "IMAGES_OPENAI_API_KEY": STACK["ADAPTER_TOKEN"],
            "IMAGES_EDIT_OPENAI_API_KEY": STACK["ADAPTER_TOKEN"],
            "IMAGES_GEMINI_API_KEY": PLANTED_KEY,
            "IMAGE_GENERATION_MODEL": "doubao-seedream-5-0-flash-260915",
            "IMAGE_EDIT_MODEL": "doubao-seedream-5-0-flash-260915",
        }
        self.default_params = {"function_calling": "native", "stream_response": True}
        self.scenarios = [scenario()]
        self.posts: list[dict] = []
        self.stops: list[str] = []
        self.paths: list[tuple[str, str]] = []
        self.chats: dict[str, dict] = {}
        self.state: dict[str, dict] = {}
        self.tokens: set[str] = set()
        self.first_poll: dict | None = None
        self.list_status: int | None = None  # an HTTP status the chat-list GET answers with

    def start(self):
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    @property
    def port(self) -> int:
        return self.server_address[1]

    def snapshot(self):
        ledger = self.eval_root / "chats.txt"
        pending = [json.loads(path.read_text(encoding="utf-8"))
                   for path in sorted(self.eval_root.glob("runs/*/*/*/pending.json"))]
        return {"ledger": ledger.read_text(encoding="utf-8") if ledger.exists() else "",
                "pending": pending}

    def create_chat(self, body: dict) -> tuple[str, dict]:
        current = self.scenarios.pop(0) if len(self.scenarios) > 1 else self.scenarios[0]
        chat_id = str(uuid.uuid4())
        user = dict(body["user_message"])
        assistant_id = body["message_ids"][0]["message_id"]
        assistant = {"id": assistant_id, "parentId": user["id"], "role": "assistant",
                     "model": MODEL, "childrenIds": []}
        assistant.update(json.loads(json.dumps(current["message"])))
        now = int(time.time())
        self.chats[chat_id] = {
            "id": chat_id, "user_id": "admin-1", "title": "New Chat", "archived": False,
            "chat": {"title": "New Chat", "models": [MODEL],
                     "history": {"messages": {user["id"]: user, assistant_id: assistant},
                                 "currentId": assistant_id}},
            "updated_at": now, "created_at": now,
        }
        self.state[chat_id] = {"busy": current["busy_polls"], "stopped": False, **current}
        outputs = self.eval_root / "data" / chat_id / "outputs"
        outputs.mkdir(parents=True)
        for name, content in current["outputs"].items():
            if isinstance(content, tuple) and content[0] == "sparse":
                with open(outputs / name, "wb") as stream:
                    stream.truncate(content[1])
            elif isinstance(content, tuple):
                (outputs / name).symlink_to(content[1])
            else:
                (outputs / name).write_bytes(content)
        return chat_id, current


class FakeHandler(BaseHTTPRequestHandler):
    server: FakeWebUI

    def log_message(self, *args):
        pass

    def reply(self, status: int, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if header.removeprefix("Bearer ") in self.server.tokens:
            return True
        self.reply(401, {"detail": "Not authenticated"})
        return False

    def do_POST(self):
        server = self.server
        server.paths.append(("POST", self.path))
        if self.path == "/api/v1/auths/signin":
            form = self.body()
            if form != {"email": ADMIN_EMAIL, "password": STACK["ADMIN_PASSWORD"]}:
                return self.reply(400, {"detail": "bad credentials"})
            token = f"{SESSION_PREFIX}{len(server.tokens)}"
            server.tokens.add(token)
            return self.reply(200, {"token": token, "token_type": "Bearer", "id": "admin-1",
                                    "email": ADMIN_EMAIL, "name": "Eval Admin", "role": "admin"})
        if not self.authorized():
            return None
        if self.path == "/api/chat/completions":
            body = self.body()
            with server.lock:
                server.posts.append(body)
                chat_id, current = server.create_chat(body)
                if current["forget"]:
                    del server.chats[chat_id]
            if current["drop_answer"]:
                self.close_connection = True
                return None
            if current["post_status"]:
                return self.reply(current["post_status"], {"detail": "failure"})
            return self.reply(200, {"status": True, "task_ids": ["task-1"], "chat_id": chat_id})
        if self.path.startswith("/api/tasks/chat/") and self.path.endswith("/stop"):
            chat_id = self.path.split("/")[4]
            server.stops.append(chat_id)
            server.state[chat_id]["stopped"] = True
            return self.reply(200, {"status": True})
        return self.reply(404, {"detail": "Not Found"})

    def do_GET(self):
        server = self.server
        server.paths.append(("GET", self.path))
        if not self.authorized():
            return None
        if self.path == "/api/models":
            return self.reply(200, {"data": server.models})
        if self.path == "/api/v1/functions/":
            return self.reply(200, server.functions)
        if self.path == "/api/v1/images/config":
            return self.reply(200, server.image_config)
        if self.path == "/api/v1/configs/models":
            return self.reply(200, {"DEFAULT_MODELS": MODEL,
                                    "DEFAULT_MODEL_PARAMS": server.default_params})
        if self.path.startswith("/api/tasks/chat/"):
            chat_id = self.path.split("/")[4]
            if server.first_poll is None:
                server.first_poll = server.snapshot()
            state = server.state[chat_id]
            if state["tasks_status"]:
                return self.reply(state["tasks_status"], {"detail": "failure"})
            if state["stopped"]:
                return self.reply(200, {"task_ids": []})
            if state["until_stop"] or state["busy"] > 0:
                state["busy"] -= 1
                return self.reply(200, {"task_ids": ["task-1"]})
            return self.reply(200, {"task_ids": []})
        if self.path == "/api/v1/chats/?page=1":
            if server.list_status:
                return self.reply(server.list_status, {"detail": "failure"})
            listing = sorted(server.chats.values(), key=lambda chat: -chat["updated_at"])
            return self.reply(200, [{"id": chat["id"], "title": chat["title"],
                                     "updated_at": chat["updated_at"],
                                     "created_at": chat["created_at"]} for chat in listing])
        if self.path.startswith("/api/v1/chats/"):
            chat_id = self.path.rsplit("/", 1)[1]
            state = server.state.get(chat_id, {})
            if chat_id not in server.chats:
                # WebUI 6b95a4995 routers/chats.py: an unknown chat is a 401, not a 404.
                return self.reply(401, {"detail": "We could not find what you're looking for :/"})
            if state.get("chat_status"):
                return self.reply(state["chat_status"], {"detail": "Not Found"})
            return self.reply(200, server.chats[chat_id])
        return self.reply(404, {"detail": "Not Found"})


class DriverCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ocu-eval-driver-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.eval_root = self.tmp / "eval-root"
        self.eval_root.mkdir(mode=0o700)
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.docker_root = self.tmp / "docker-root"
        self.docker_root.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, body in (("docker", DOCKER_STUB), ("df", DF_STUB)):
            path = self.bin / name
            path.write_text(body.replace("__PYTHON__", sys.executable)
                            .replace("__VALID__", repr(VALID_PROBE)), encoding="utf-8")
            path.chmod(0o755)
        self.write_state("secrets.json", self.secret_values())
        self.write_state("watched.json", [*STACK_NAMES, "EVAL_PROVIDER_KEY"])
        self.write_state("images.json", {DEFAULT_RELEASE_IMAGES[role]: IMAGE_IDS[role]
                                         for role in ROLES})
        self.write_state("containers.json", {
            "ocu-eval-computer-use-server": {"running": True, "image": IMAGE_IDS["computer-use-server"]},
            "ocu-eval-adapter": {"running": True, "image": IMAGE_IDS["computer-use-server"]},
            "ocu-eval-open-webui": {"running": True, "image": IMAGE_IDS["open-webui"]},
            "ocu-eval-postgres": {"running": True, "image": IMAGE_IDS["postgres"]},
        })
        self.package = self.tmp / "package"
        write_inventory(self.package / "release.json",
                        synthetic_inventory(ocu_sha="a" * 40, webui_sha="b" * 40))
        self.webui = FakeWebUI(self.eval_root).start()
        self.addCleanup(self.webui.server_close)
        self.addCleanup(self.webui.shutdown)
        for name, value in (("POLL_SECONDS", 0.02), ("RETRY_SECONDS", 0.01)):
            patcher = mock.patch.object(DRIVER, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def secret_values(self) -> list[str]:
        return [value for name, value in STACK.items() if name != "ADMIN_EMAIL"] + [PLANTED_KEY]

    def write_state(self, name: str, payload):
        (self.state / name).write_text(json.dumps(payload), encoding="utf-8")

    def environment(self, **extra) -> dict[str, str]:
        environment = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.tmp),
            "DOCKER_HOST": "unix://" + str(self.tmp / "stub.sock"),
            "EVAL_STUB_STATE": str(self.state),
            "EVAL_STUB_DOCKER_ROOT": str(self.docker_root),
            "EVAL_STUB_DF_KB": str(PLENTY_KB),
            "EVAL_ROOT": str(self.eval_root),
            "EVAL_WEBUI_PORT": str(self.webui.port),
        }
        environment.update(extra)
        return environment

    def driver_environment(self, **extra) -> dict[str, str]:
        """What run.sh exports into the driver: the stack and image variables and both name lists."""
        environment = self.environment(**extra)
        environment.update(STACK)
        environment["EVAL_STACK_NAMES"] = " ".join(STACK_NAMES)
        environment["EVAL_ROLES"] = " ".join(ROLES)
        for role in ROLES:
            environment[RUNTIME_IMAGE_VARS[role]] = DEFAULT_RELEASE_IMAGES[role]
        return environment

    def drive(self, *args: str, request: str | None = FIRST["id"], **extra) -> tuple[int, str, str]:
        argv = ["--package", str(self.package), "--phase", "smoke", *args]
        if request is not None:
            argv += ["--request", request]
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.driver_environment(**extra), clear=True), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = DRIVER.main(argv, io.StringIO(PLANTED_KEY + "\n"))
        self.assert_no_leak(stdout.getvalue() + stderr.getvalue())
        return code, stdout.getvalue(), stderr.getvalue()

    def calls(self, tool: str | None = None) -> list[dict]:
        path = self.state / "calls.jsonl"
        if not path.exists():
            return []
        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return [entry for entry in entries if tool in (None, entry["tool"])]

    def run_dirs(self, request: str = FIRST["id"], phase: str = "smoke") -> list[Path]:
        base = self.eval_root / "runs" / phase / request
        return sorted(base.iterdir(), key=lambda path: path.name) if base.exists() else []

    def manifest(self, run_dir: Path) -> dict:
        return json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

    def only_manifest(self) -> dict:
        dirs = self.run_dirs()
        self.assertEqual([path.name for path in dirs], ["r1-a1"])
        return self.manifest(dirs[0])

    def ledger(self) -> list[str]:
        path = self.eval_root / "chats.txt"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def secrets(self) -> dict[str, str]:
        values = {name: value for name, value in STACK.items() if name != "ADMIN_EMAIL"}
        values["provider key"] = PLANTED_KEY
        values.update({f"session token {index}": token
                       for index, token in enumerate(sorted(self.webui.tokens))})
        return values

    def assert_no_leak(self, text: str):
        for label, value in self.secrets().items():
            self.assertNotIn(value, text, f"{label} reached driver output")

    def assert_no_leak_anywhere(self):
        for path in self.eval_root.rglob("*"):
            if path.is_file() and not path.is_symlink() and path.name != "stack.env":
                data = path.read_bytes()
                for label, value in self.secrets().items():
                    self.assertNotIn(value.encode(), data, f"{label} reached {path.relative_to(self.eval_root)}")
        for entry in self.calls():
            argv = json.dumps(entry["argv"])
            for label, value in self.secrets().items():
                self.assertNotIn(value, argv, f"{label} reached a {entry['tool']} command line")
        for entry in self.calls("docker"):
            self.assertEqual(entry["env_names"], [], "a stack variable reached a docker environment")
            self.assertFalse(entry["env_secret"], "a secret value reached a docker environment")


class DriverEntryPointTests(DriverCase):
    """run.sh: the key on stdin, the exported files, the done line."""

    def write_private(self, path: Path, text: str) -> Path:
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
        return path

    def write_files(self):
        self.write_private(self.eval_root / "stack.env",
                           "".join(f"{name}={value}\n" for name, value in STACK.items()))
        self.write_private(self.eval_root / "images.env",
                           "".join(f"{RUNTIME_IMAGE_VARS[role]}={DEFAULT_RELEASE_IMAGES[role]}\n"
                                   for role in ROLES))
        self.write_private(self.tmp / ".ocu-eval-dmxapi.key", PLANTED_KEY + "\n")

    def run_script(self, *args: str, **extra) -> subprocess.CompletedProcess:
        argv = ["bash", str(RUN), "--package", str(self.package), "--phase", "smoke", *args]
        result = subprocess.run(argv, cwd=self.tmp, env=self.environment(**extra),
                                capture_output=True, text=True, timeout=120, check=False)
        self.assert_no_leak(result.stdout + result.stderr)
        return result

    def test_completion_body_matches_the_contract_key_for_key(self):
        self.write_files()
        before = int(time.time())
        result = self.run_script("--request", FIRST["id"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1], "eval: done 0")
        self.assertEqual(len(self.webui.posts), 1)
        body = self.webui.posts[0]
        self.assertEqual(set(body), {
            "stream", "model", "params", "files", "filter_ids", "tool_ids", "tool_servers",
            "features", "variables", "chat_variables", "session_id", "id", "message_ids",
            "parent_id", "user_message", "background_tasks"})
        self.assertIs(body["stream"], True)
        self.assertEqual(body["model"], MODEL)
        self.assertEqual(body["params"], {})
        self.assertEqual(body["files"], [])
        self.assertEqual(body["filter_ids"], ["video_production_filter"])
        self.assertEqual(body["tool_ids"], [])
        self.assertEqual(body["tool_servers"], [])
        self.assertEqual(body["features"], {"voice": False, "image_generation": True,
                                            "code_interpreter": False, "web_search": False})
        variables = body["variables"]
        self.assertEqual(set(variables), {
            "{{USER_NAME}}", "{{USER_EMAIL}}", "{{USER_LOCATION}}", "{{CURRENT_DATETIME}}",
            "{{CURRENT_DATE}}", "{{CURRENT_TIME}}", "{{CURRENT_WEEKDAY}}", "{{CURRENT_TIMEZONE}}",
            "{{USER_LANGUAGE}}"})
        self.assertEqual(variables["{{USER_NAME}}"], "Eval Admin")
        self.assertEqual(variables["{{USER_EMAIL}}"], ADMIN_EMAIL)
        self.assertEqual(variables["{{USER_LOCATION}}"], "Unknown")
        self.assertEqual(variables["{{CURRENT_TIMEZONE}}"], "Asia/Shanghai")
        self.assertEqual(variables["{{USER_LANGUAGE}}"], "zh-CN")
        self.assertEqual(variables["{{CURRENT_DATETIME}}"],
                         f"{variables['{{CURRENT_DATE}}']} {variables['{{CURRENT_TIME}}']}")
        self.assertRegex(variables["{{CURRENT_DATE}}"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertRegex(variables["{{CURRENT_TIME}}"], r"^\d{2}:\d{2}:\d{2}$")
        self.assertIn(variables["{{CURRENT_WEEKDAY}}"], ("Monday", "Tuesday", "Wednesday", "Thursday",
                                                         "Friday", "Saturday", "Sunday"))
        self.assertEqual(body["chat_variables"], {})
        self.assertTrue(body["session_id"].startswith("eval-"))
        self.assertEqual(uuid.UUID(body["session_id"][5:]).version, 4)
        assistant_id = body["id"]
        self.assertEqual(uuid.UUID(assistant_id).version, 4)
        self.assertEqual(body["message_ids"], [{"model_id": MODEL, "message_id": assistant_id,
                                                "modelIdx": 0}])
        self.assertIsNone(body["parent_id"])
        user = body["user_message"]
        self.assertEqual(set(user), {"id", "parentId", "childrenIds", "role", "content",
                                     "timestamp", "models"})
        self.assertEqual(uuid.UUID(user["id"]).version, 4)
        self.assertNotEqual(user["id"], assistant_id)
        self.assertIsNone(user["parentId"])
        self.assertEqual(user["childrenIds"], [assistant_id])
        self.assertEqual(user["role"], "user")
        self.assertEqual(user["content"], FIRST["prompt"])
        self.assertIs(type(user["timestamp"]), int)
        self.assertTrue(before <= user["timestamp"] <= int(time.time()))
        self.assertEqual(user["models"], [MODEL])
        self.assertEqual(body["background_tasks"], {"title_generation": True, "tags_generation": True,
                                                    "follow_up_generation": True})

    def test_secrets_never_reach_output_run_files_or_command_lines(self):
        self.write_files()
        # The WebUI echoes every secret in the chat: the content, a tool call and its result.
        planted = " ".join(self.secret_values())
        self.webui.scenarios = [scenario(message={
            "done": True, "content": f"planted {planted}",
            "output": [call("generate_speech", "c1", json.dumps({"text": planted})),
                       result("c1", f"Saved line1.mp3 {planted}")]})]
        completed = self.run_script("--request", FIRST["id"])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        manifest = self.only_manifest()
        self.assertEqual(manifest["outcome"], "succeeded")
        transcript = (self.run_dirs()[0] / "transcript.json").read_text(encoding="utf-8")
        self.assertIn("[REDACTED]", transcript)
        self.assertGreaterEqual(len(self.webui.tokens), 1)
        self.assert_no_leak_anywhere()

    def test_low_free_space_refuses_with_both_values_before_any_post(self):
        self.write_files()
        result = self.run_script(EVAL_STUB_DF_KB=str(LOW_KB))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(LOW_KB * 1024), result.stderr)
        self.assertIn(str(FLOOR_BYTES), result.stderr)
        self.assertEqual(self.webui.posts, [])
        self.assertEqual(self.run_dirs(), [])
        self.assertRegex(result.stdout.splitlines()[-1], r"^eval: done [1-9][0-9]*$")

    def test_done_line_is_written_when_run_sh_refuses_early(self):
        self.write_files()
        (self.tmp / ".ocu-eval-dmxapi.key").chmod(0o644)
        result = self.run_script("--request", FIRST["id"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("0600", result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1], f"eval: done {result.returncode}")
        self.assertEqual(self.webui.posts, [])

    def test_a_running_driver_holds_the_lock_against_cleanup_and_a_second_driver(self):
        self.write_files()
        if shutil.which("flock") is None:
            # macOS has no flock(1): the same flock(2) lock on fd 9; the host has util-linux flock.
            shim = self.bin / "flock"
            shim.write_text(FLOCK_SHIM.replace("__PYTHON__", sys.executable), encoding="utf-8")
            shim.chmod(0o755)
        self.webui.scenarios = [scenario(until_stop=True)]
        argv = ["bash", str(RUN), "--package", str(self.package), "--phase", "smoke",
                "--request", FIRST["id"]]
        driver = subprocess.Popen(argv, cwd=self.tmp, env=self.environment(), start_new_session=True,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 60
            while self.webui.first_poll is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertIsNotNone(self.webui.first_poll, "the driver never polled")
            docker_calls = len(self.calls("docker"))
            cleanup = subprocess.run(["bash", str(EVAL_DIR / "cleanup.sh"), "--runs-only"], cwd=self.tmp,
                                     env=self.environment(), capture_output=True, text=True,
                                     timeout=60, check=False)
            self.assertNotEqual(cleanup.returncode, 0)
            self.assertIn("eval.lock", cleanup.stderr)
            self.assertEqual(len(self.calls("docker")), docker_calls, "cleanup ran a docker command")
            code, _, stderr = self.drive()
            self.assertNotEqual(code, 0)
            self.assertIn("eval.lock", stderr)
            self.assertEqual(len(self.webui.posts), 1)
        finally:
            os.killpg(driver.pid, signal.SIGTERM)
            driver.wait(timeout=30)

    def test_whole_request_set_runs_in_file_order_with_its_repetitions(self):
        self.write_files()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = [(request["id"], repetition) for request in REQUESTS
                    for repetition in range(1, request["repetitions"] + 1)]
        prompts = [body["user_message"]["content"] for body in self.webui.posts]
        self.assertEqual(prompts, [next(r["prompt"] for r in REQUESTS if r["id"] == rid)
                                   for rid, _ in expected])
        for request_id, repetition in expected:
            self.assertTrue((self.eval_root / "runs" / "smoke" / request_id / f"r{repetition}-a1"
                             / "manifest.json").is_file(), (request_id, repetition))
        self.assertEqual(len(result.stdout.splitlines()), len(expected) + 1)


class DriverFlowTests(DriverCase):
    def test_chat_is_in_the_ledger_and_pending_before_the_first_poll(self):
        self.webui.scenarios = [scenario(tasks_status=500)]
        code, stdout, _ = self.drive()
        self.assertEqual(code, 0)
        chat_ids = list(self.webui.chats)
        self.assertEqual(len(chat_ids), 3, "an infra outcome reruns twice")
        snapshot = self.webui.first_poll
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["ledger"].splitlines(), chat_ids[:1])
        self.assertEqual([entry.get("chat_id") for entry in snapshot["pending"]], chat_ids[:1])
        self.assertEqual(self.ledger(), chat_ids)
        for run_dir, chat_id in zip(self.run_dirs(), chat_ids, strict=True):
            pending = json.loads((run_dir / "pending.json").read_text(encoding="utf-8"))
            self.assertEqual(pending["chat_id"], chat_id)
            manifest = self.manifest(run_dir)
            self.assertEqual((manifest["failure_class"], manifest["chat_id"]), ("infra", chat_id))
            self.assertTrue(manifest["failure_reason"].startswith(f"GET /api/tasks/chat/{chat_id}"))

    def test_dropped_post_answer_is_found_through_the_chat_list_and_not_repeated(self):
        self.webui.scenarios = [scenario(drop_answer=True)]
        code, _, _ = self.drive()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.webui.posts), 1)
        chat_id = next(iter(self.webui.chats))
        self.assertIn(("GET", "/api/v1/chats/?page=1"), self.webui.paths)
        self.assertEqual(self.ledger(), [chat_id])
        manifest = self.only_manifest()
        self.assertEqual((manifest["chat_id"], manifest["outcome"]), (chat_id, "succeeded"))

    def test_post_lost_without_a_chat_is_infra(self):
        self.webui.scenarios = [scenario(drop_answer=True, forget=True)]
        code, _, _ = self.drive()
        self.assertEqual(code, 0)
        manifests = [self.manifest(path) for path in self.run_dirs()]
        self.assertEqual(len(manifests), 3)
        for manifest in manifests:
            self.assertEqual((manifest["failure_class"], manifest["failure_reason"], manifest["chat_id"]),
                             ("infra", "post lost", None))
        self.assertEqual(self.ledger(), [])

    def test_refused_connection_is_retried(self):
        real_open = DRIVER.OPENER.open
        refused = []

        def open_(request, *args, **kwargs):
            if request.full_url.endswith("/api/chat/completions") and len(refused) < 2:
                refused.append(request.full_url)
                raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
            return real_open(request, *args, **kwargs)

        with mock.patch.object(DRIVER.OPENER, "open", open_):
            code, _, _ = self.drive()
        self.assertEqual(code, 0)
        self.assertEqual(len(refused), 2)
        self.assertEqual(len(self.webui.posts), 1)
        self.assertEqual(self.only_manifest()["outcome"], "succeeded")

    def test_post_http_error_with_the_chat_created_is_found_and_ledgered(self):
        self.webui.scenarios = [scenario(post_status=500)]
        code, _, _ = self.drive()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.webui.posts), 1)
        chat_id = next(iter(self.webui.chats))
        self.assertEqual(self.ledger(), [chat_id])
        manifest = self.only_manifest()
        self.assertEqual((manifest["chat_id"], manifest["outcome"]), (chat_id, "succeeded"))

    def test_post_http_error_without_a_chat_names_the_status(self):
        self.webui.scenarios = [scenario(post_status=500, forget=True)]
        code, _, _ = self.drive()
        self.assertEqual(code, 0)
        manifest = self.manifest(self.run_dirs()[0])
        self.assertEqual((manifest["failure_class"], manifest["failure_reason"], manifest["chat_id"]),
                         ("infra", "POST /api/chat/completions: HTTP 500", None))
        self.assertEqual(self.ledger(), [])

    def test_failed_lookup_after_a_lost_answer_stops_without_a_manifest(self):
        self.webui.scenarios = [scenario(drop_answer=True), scenario()]
        self.webui.list_status = 500
        code, _, stderr = self.drive()
        self.assertNotEqual(code, 0)
        self.assertIn("refused", stderr)
        self.assertIn("/api/v1/chats/?page=1", stderr)
        self.assertEqual(len(self.webui.posts), 1, "the request is never sent twice")
        run_dir = self.run_dirs()[0]
        self.assertEqual([path.name for path in self.run_dirs()], ["r1-a1"])
        self.assertFalse((run_dir / "manifest.json").exists())
        pending = json.loads((run_dir / "pending.json").read_text(encoding="utf-8"))
        self.assertNotIn("chat_id", pending)
        self.assertEqual(self.ledger(), [])
        # The next driver run repeats the lookup, records the chat and goes on.
        self.webui.list_status = None
        chat_id = next(iter(self.webui.chats))
        code, _, _ = self.drive()
        self.assertEqual(code, 0)
        recovered = self.manifest(run_dir)
        self.assertEqual((recovered["failure_reason"], recovered["chat_id"]),
                         ("driver interrupted", chat_id))
        self.assertEqual(self.ledger()[0], chat_id)

    def test_an_abandoned_chat_is_stopped_before_the_next_post(self):
        self.webui.scenarios = [scenario(tasks_status=500)]
        code, _, _ = self.drive()
        self.assertEqual(code, 0)
        chat_ids = list(self.webui.chats)
        self.assertEqual(self.webui.stops, chat_ids)
        posts = [path for method, path in self.webui.paths if method == "POST"
                 and (path == "/api/chat/completions" or path.endswith("/stop"))]
        self.assertEqual(posts, [path for chat_id in chat_ids
                                 for path in ("/api/chat/completions", f"/api/tasks/chat/{chat_id}/stop")])
        for path in self.run_dirs():
            self.assertEqual(self.manifest(path)["failure_class"], "infra")

    def test_free_space_is_checked_before_every_attempt(self):
        self.webui.scenarios = [scenario(message={"done": False, "output": []})]
        code, _, _ = self.drive()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.run_dirs()), 3)
        self.assertEqual(len(self.calls("df")), 3)

    def test_initial_sign_in_retries_a_reset_connection(self):
        real_open = DRIVER.OPENER.open
        resets = []

        def open_(request, *args, **kwargs):
            if request.full_url.endswith("/api/v1/auths/signin") and len(resets) < 2:
                resets.append(request.full_url)
                raise ConnectionResetError(54, "Connection reset by peer")
            return real_open(request, *args, **kwargs)

        with mock.patch.object(DRIVER, "SIGNIN_RETRY_SECONDS", 0.01), \
                mock.patch.object(DRIVER.OPENER, "open", open_):
            code, _, _ = self.drive()
        self.assertEqual(code, 0)
        self.assertEqual(len(resets), 2)
        self.assertEqual(self.only_manifest()["outcome"], "succeeded")

    def test_initial_sign_in_gives_up_after_its_tries(self):
        def refuse(request, *args, **kwargs):
            raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

        with mock.patch.object(DRIVER, "SIGNIN_RETRY_SECONDS", 0.01), \
                mock.patch.object(DRIVER.OPENER, "open", refuse):
            code, _, stderr = self.drive()
        self.assertNotEqual(code, 0)
        self.assertIn("/api/v1/auths/signin", stderr)
        self.assertEqual(self.run_dirs(), [])

    def test_failure_reason_stays_on_one_stdout_line(self):
        self.webui.scenarios = [scenario(message={"done": True, "output": [],
                                                  "error": {"content": "first line\nsecond\r\nthird"}})]
        code, stdout, _ = self.drive()
        self.assertEqual(code, 0)
        lines = stdout.splitlines()
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertTrue(line.startswith("eval: runs/smoke/"), line)
            self.assertIn("first line second third", line)

    def test_each_preflight_failure_refuses_before_any_post(self):
        def drop(items, key):
            return [item for item in items if item["id"] != key]

        cases = {
            "qwen3.8-27b": lambda w: setattr(w, "models", []),
            "video_production_filter": lambda w: w.models[0].update(filters=[]),
            "computer_use_filter": lambda w: setattr(w, "functions", drop(w.functions, "computer_use_filter")),
            "is not active": lambda w: w.functions[1].update(is_active=False),
            "is not global": lambda w: w.functions[0].update(is_global=False),
            "ENABLE_IMAGE_GENERATION": lambda w: w.image_config.update(ENABLE_IMAGE_GENERATION=False),
            "ENABLE_IMAGE_EDIT": lambda w: w.image_config.update(ENABLE_IMAGE_EDIT=False),
            "IMAGE_GENERATION_ENGINE": lambda w: w.image_config.update(IMAGE_GENERATION_ENGINE="comfyui"),
            "IMAGE_EDIT_ENGINE": lambda w: w.image_config.update(IMAGE_EDIT_ENGINE="gemini"),
            "IMAGES_OPENAI_API_BASE_URL": lambda w: w.image_config.update(
                IMAGES_OPENAI_API_BASE_URL="https://www.dmxapi.cn/v1"),
            "IMAGES_EDIT_OPENAI_API_BASE_URL": lambda w: w.image_config.update(
                IMAGES_EDIT_OPENAI_API_BASE_URL="https://www.dmxapi.cn/v1"),
            "function_calling": lambda w: w.default_params.update(function_calling="default"),
            "stream_response": lambda w: w.default_params.update(stream_response=False),
        }
        for reason, breaks in cases.items():
            with self.subTest(reason):
                self.webui.posts.clear()
                saved = json.loads(json.dumps([self.webui.models, self.webui.functions,
                                               self.webui.image_config, self.webui.default_params]))
                breaks(self.webui)
                code, _, stderr = self.drive()
                self.assertNotEqual(code, 0)
                self.assertIn("refused", stderr)
                self.assertIn(reason, stderr)
                self.assertEqual(self.webui.posts, [])
                self.assertEqual(self.run_dirs(), [])
                (self.webui.models, self.webui.functions, self.webui.image_config,
                 self.webui.default_params) = saved

    def test_release_mismatch_refuses_in_preflight(self):
        containers = json.loads((self.state / "containers.json").read_text(encoding="utf-8"))
        cases = {
            "ocu-eval-adapter": ("containers.json",
                                 {**containers, "ocu-eval-adapter": {"running": True,
                                                                     "image": IMAGE_IDS["postgres"]}}),
            "ocu-eval-postgres": ("containers.json",
                                  {**containers, "ocu-eval-postgres": {"running": False,
                                                                       "image": IMAGE_IDS["postgres"]}}),
        }
        for reason, (name, payload) in cases.items():
            with self.subTest(reason):
                self.write_state(name, payload)
                code, _, stderr = self.drive()
                self.assertNotEqual(code, 0)
                self.assertIn(reason, stderr)
                self.assertEqual(self.webui.posts, [])
        self.write_state("containers.json", containers)
        moved = synthetic_inventory(ocu_sha="a" * 40, webui_sha="b" * 40,
                                    images={"open-webui": {"reference": "ocu-open-webui:other"}})
        write_inventory(self.package / "release.json", moved)
        code, _, stderr = self.drive()
        self.assertNotEqual(code, 0)
        self.assertIn("ocu-open-webui:other", stderr)
        self.assertIn("OPENWEBUI_IMAGE", stderr)
        self.assertEqual(self.webui.posts, [])

    def test_time_limit_calls_stop_and_gives_model(self):
        self.webui.scenarios = [scenario(until_stop=True, message={"done": False, "output": []})]
        with mock.patch.object(DRIVER, "TIME_LIMIT_SECONDS", 0.2), \
                mock.patch.object(DRIVER, "STOP_WAIT_SECONDS", 2):
            code, _, _ = self.drive()
        self.assertEqual(code, 0)
        chat_id = next(iter(self.webui.chats))
        self.assertEqual(self.webui.stops, [chat_id])
        manifest = self.only_manifest()
        self.assertEqual((manifest["outcome"], manifest["failure_class"], manifest["failure_reason"]),
                         ("failed", "model", "time limit"))
        self.assertGreaterEqual(manifest["elapsed_seconds"], 0.2)

    def test_infra_outcome_reruns_twice_and_stops(self):
        self.webui.scenarios = [scenario(message={"done": False, "output": []})]
        code, stdout, _ = self.drive()
        self.assertEqual(code, 0)
        dirs = self.run_dirs()
        self.assertEqual([path.name for path in dirs], ["r1-a1", "r1-a2", "r1-a3"])
        for number, path in enumerate(dirs, start=1):
            manifest = self.manifest(path)
            self.assertEqual((manifest["attempt"], manifest["failure_class"], manifest["failure_reason"]),
                             (number, "infra", "task lost"))
        self.assertEqual(len(self.webui.posts), 3)
        self.assertEqual(len(stdout.splitlines()), 3)

    def test_success_on_the_second_attempt_stops_there(self):
        self.webui.scenarios = [scenario(message={"done": False, "output": []}), scenario()]
        code, _, _ = self.drive()
        self.assertEqual(code, 0)
        dirs = self.run_dirs()
        self.assertEqual([path.name for path in dirs], ["r1-a1", "r1-a2"])
        self.assertEqual(self.manifest(dirs[0])["failure_class"], "infra")
        self.assertEqual(self.manifest(dirs[1])["outcome"], "succeeded")
        self.assertEqual(len(self.webui.posts), 2)

    def interrupted_dir(self, pending: dict) -> Path:
        run_dir = self.eval_root / "runs" / "smoke" / FIRST["id"] / "r1-a1"
        run_dir.mkdir(parents=True)
        (run_dir / "pending.json").write_text(json.dumps(pending), encoding="utf-8")
        return run_dir

    def test_interrupted_run_gets_a_manifest_and_the_next_attempt_number_is_free(self):
        chat_id = str(uuid.uuid4())
        (self.eval_root / "chats.txt").write_text(chat_id + "\n", encoding="utf-8")
        run_dir = self.interrupted_dir({"user_message_id": str(uuid.uuid4()),
                                        "started_at": "2026-11-17T08:00:00Z", "chat_id": chat_id})
        code, stdout, _ = self.drive()
        self.assertEqual(code, 0)
        recovered = self.manifest(run_dir)
        self.assertEqual((recovered["outcome"], recovered["failure_class"], recovered["failure_reason"],
                          recovered["chat_id"], recovered["attempt"], recovered["repetition"]),
                         ("failed", "infra", "driver interrupted", chat_id, 1, 1))
        self.assertEqual(recovered["started_at"], "2026-11-17T08:00:00Z")
        self.assertEqual(set(recovered), MANIFEST_KEYS)
        for name in ("release", "webui_image_id", "models", "counts", "elapsed_seconds", "check"):
            self.assertIsNone(recovered[name], name)
        self.assertEqual((recovered["mp4"], recovered["skipped_outputs"], recovered["version"],
                          recovered["phase"], recovered["request_id"], recovered["kind"]),
                         ([], [], 1, "smoke", FIRST["id"], FIRST["kind"]))
        self.assertEqual([path.name for path in self.run_dirs()], ["r1-a1", "r1-a2"])
        self.assertEqual(self.manifest(self.run_dirs()[1])["attempt"], 2)
        self.assertEqual(self.ledger()[0], chat_id)
        self.assertEqual(self.ledger().count(chat_id), 1)
        self.assertIn("driver interrupted", stdout)

    def test_an_interrupted_attempt_uses_up_one_of_the_three(self):
        chat_id = str(uuid.uuid4())
        self.interrupted_dir({"user_message_id": str(uuid.uuid4()),
                              "started_at": "2026-11-17T08:00:00Z", "chat_id": chat_id})
        self.webui.scenarios = [scenario(message={"done": False, "output": []})]
        code, _, _ = self.drive()
        self.assertEqual(code, 0)
        self.assertEqual([path.name for path in self.run_dirs()], ["r1-a1", "r1-a2", "r1-a3"])
        self.assertEqual(len(self.webui.posts), 2)

    def test_the_rerun_budget_counts_infra_attempts_of_earlier_batches(self):
        other = REQUESTS[1]["id"]
        run_dir = self.eval_root / "runs" / "smoke" / other / "r1-a1"
        run_dir.mkdir(parents=True)
        (run_dir / "pending.json").write_text(json.dumps({
            "user_message_id": str(uuid.uuid4()), "started_at": "2026-11-17T08:00:00Z",
            "chat_id": str(uuid.uuid4())}), encoding="utf-8")
        # A batch for another request recovers it.
        code, _, _ = self.drive()
        self.assertEqual(code, 0)
        self.assertEqual(self.manifest(run_dir)["failure_reason"], "driver interrupted")
        posts = len(self.webui.posts)
        self.webui.scenarios = [scenario(message={"done": False, "output": []})]
        code, _, _ = self.drive(request=other)
        self.assertEqual(code, 0)
        self.assertEqual([path.name for path in self.run_dirs(other)], ["r1-a1", "r1-a2", "r1-a3"])
        self.assertEqual(len(self.webui.posts) - posts, 2)

    def test_the_rerun_budget_survives_a_preflight_refusal_after_recovery(self):
        run_dir = self.interrupted_dir({"user_message_id": str(uuid.uuid4()),
                                        "started_at": "2026-11-17T08:00:00Z", "chat_id": str(uuid.uuid4())})
        self.webui.default_params["stream_response"] = False
        code, _, _ = self.drive()
        self.assertNotEqual(code, 0)
        self.assertEqual(self.manifest(run_dir)["failure_reason"], "driver interrupted")
        self.webui.default_params["stream_response"] = True
        self.webui.scenarios = [scenario(message={"done": False, "output": []})]
        code, _, _ = self.drive()
        self.assertEqual(code, 0)
        self.assertEqual([path.name for path in self.run_dirs()], ["r1-a1", "r1-a2", "r1-a3"])
        self.assertEqual(len(self.webui.posts), 2)

    def test_a_slot_whose_last_attempt_was_not_infra_starts_fresh(self):
        self.webui.scenarios = [scenario()]
        self.assertEqual(self.drive()[0], 0)
        self.webui.scenarios = [scenario(message={"done": False, "output": []})]
        self.assertEqual(self.drive()[0], 0)
        self.assertEqual([path.name for path in self.run_dirs()], ["r1-a1", "r1-a2", "r1-a3", "r1-a4"])
        self.assertEqual(self.drive()[0], 0)
        self.assertEqual(len(self.run_dirs()), 4, "three infra attempts in a row leave no budget")

    def test_three_interrupted_attempts_leave_no_new_attempt(self):
        base = self.eval_root / "runs" / "smoke" / FIRST["id"]
        for number in (1, 2, 3):
            (base / f"r1-a{number}").mkdir(parents=True)
            (base / f"r1-a{number}" / "pending.json").write_text(json.dumps({
                "user_message_id": str(uuid.uuid4()), "started_at": "2026-11-17T08:00:00Z",
                "chat_id": str(uuid.uuid4())}), encoding="utf-8")
        code, stdout, _ = self.drive()
        self.assertEqual(code, 0)
        self.assertEqual(self.webui.posts, [])
        self.assertEqual([path.name for path in self.run_dirs()], ["r1-a1", "r1-a2", "r1-a3"])
        self.assertIn("no attempt left", stdout)

    def test_interrupted_run_without_a_chat_id_is_found_by_its_user_message(self):
        # An earlier driver sent the POST and died before it read the answer.
        self.webui.scenarios = [scenario(drop_answer=True), scenario()]
        lost_body = None
        real_open = DRIVER.OPENER.open

        def die_after_send(request, *args, **kwargs):
            nonlocal lost_body
            if request.full_url.endswith("/api/chat/completions") and lost_body is None:
                lost_body = json.loads(request.data)
                raise KeyboardInterrupt
            return real_open(request, *args, **kwargs)

        with mock.patch.object(DRIVER.OPENER, "open", die_after_send):
            with self.assertRaises(KeyboardInterrupt):
                self.drive()
        # The interrupted request reaches the WebUI, which creates the chat.
        with self.webui.lock:
            chat_id, _ = self.webui.create_chat(lost_body)
        self.assertEqual(self.ledger(), [])
        code, _, _ = self.drive()
        self.assertEqual(code, 0)
        recovered = self.manifest(self.run_dirs()[0])
        self.assertEqual((recovered["failure_reason"], recovered["chat_id"]),
                         ("driver interrupted", chat_id))
        self.assertEqual(self.ledger()[0], chat_id)
        self.assertEqual(len(self.run_dirs()), 2)

    def test_manifest_holds_every_field_with_its_type(self):
        code, _, _ = self.drive()
        self.assertEqual(code, 0)
        manifest = self.only_manifest()
        chat_id = next(iter(self.webui.chats))
        self.assertEqual(set(manifest), MANIFEST_KEYS)
        self.assertEqual(manifest["version"], 1)
        self.assertEqual((manifest["phase"], manifest["request_id"], manifest["kind"]),
                         ("smoke", FIRST["id"], FIRST["kind"]))
        self.assertEqual((manifest["repetition"], manifest["attempt"], manifest["chat_id"]),
                         (1, 1, chat_id))
        release_bytes = (self.package / "release.json").read_bytes()
        self.assertEqual(manifest["release"], {
            "ocu_source_sha": "a" * 40, "webui_source_sha": "b" * 40,
            "release_json_sha256": hashlib.sha256(release_bytes).hexdigest(),
            "images": {role: {"reference": DEFAULT_RELEASE_IMAGES[role], "image_id": IMAGE_IDS[role]}
                       for role in ROLES}})
        self.assertEqual(manifest["webui_image_id"], IMAGE_IDS["open-webui"])
        self.assertEqual(manifest["models"], {"chat": MODEL,
                                              "image": "doubao-seedream-5-0-flash-260915",
                                              "image_edit": "doubao-seedream-5-0-flash-260915",
                                              "speech": "mimo-v2.5-tts"})
        self.assertRegex(manifest["started_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertIsInstance(manifest["elapsed_seconds"], (int, float))
        self.assertEqual((manifest["outcome"], manifest["failure_class"], manifest["failure_reason"],
                          manifest["check"]), ("succeeded", None, None, None))
        self.assertEqual(manifest["counts"], {"tool_calls": 1, "image_calls": 0, "speech_calls": 0})
        self.assertEqual(manifest["mp4"], [{
            "name": "video.mp4", "size": len(MP4_BYTES),
            "sha256": hashlib.sha256(MP4_BYTES).hexdigest(),
            "duration": 12.5, "video_codec": "h264", "valid": True}])
        self.assertEqual(manifest["skipped_outputs"], [])
        run_dir = self.run_dirs()[0]
        self.assertEqual((run_dir / "video.mp4").read_bytes(), MP4_BYTES)
        self.assertEqual(sorted(path.name for path in run_dir.iterdir()),
                         ["manifest.json", "pending.json", "transcript.json", "video.mp4"])
        probes = [entry["argv"] for entry in self.calls("docker") if entry["argv"][:1] == ["run"]]
        self.assertEqual(probes, [[
            "run", "--rm", "--network", "none", "--entrypoint", "ffprobe",
            "-v", f"{run_dir}:/run:ro", DEFAULT_RELEASE_IMAGES["workspace"],
            "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name",
            "-of", "json", "/run/video.mp4"]])
        self.assert_no_leak_anywhere()

    def test_long_data_uri_is_replaced_in_the_transcript(self):
        long_uri = "data:image/png;base64," + "A" * 5000
        short_uri = "data:image/png;base64," + "B" * 100
        self.webui.scenarios = [scenario(message={"done": True, "content": long_uri,
                                                  "files": [{"url": short_uri}], "output": []})]
        self.drive()
        transcript = json.loads((self.run_dirs()[0] / "transcript.json").read_text(encoding="utf-8"))
        chat_id = next(iter(self.webui.chats))
        assistant_id = self.webui.posts[0]["id"]
        message = transcript["chat"]["history"]["messages"][assistant_id]
        digest = hashlib.sha256(long_uri.encode()).hexdigest()
        self.assertEqual(message["content"], f"<data-uri sha256={digest} bytes={len(long_uri)}>")
        self.assertEqual(message["files"], [{"url": short_uri}])
        self.assertEqual(transcript["id"], chat_id)

    def test_mp4_symlink_is_not_copied_and_is_listed(self):
        planted = self.tmp / "planted-secret"
        planted.write_text(PLANTED_KEY, encoding="utf-8")
        self.webui.scenarios = [scenario(outputs={"video.mp4": MP4_BYTES,
                                                  "leak.mp4": ("symlink", str(planted)),
                                                  "notes.txt": b"x"})]
        code, _, _ = self.drive()
        self.assertEqual(code, 0)
        run_dir = self.run_dirs()[0]
        manifest = self.manifest(run_dir)
        self.assertFalse((run_dir / "leak.mp4").exists())
        self.assertFalse((run_dir / "notes.txt").exists())
        self.assertEqual(manifest["skipped_outputs"], [{"name": "leak.mp4", "reason": "symlink"}])
        self.assertEqual([entry["name"] for entry in manifest["mp4"]], ["video.mp4"])
        self.assert_no_leak_anywhere()

    def listdir_adding(self, *extra: str):
        real = os.listdir

        def listdir(path="."):
            names = real(path)
            return names + list(extra) if isinstance(path, int) else names

        return mock.patch.object(DRIVER.os, "listdir", listdir)

    def test_a_vanished_output_is_skipped(self):
        with self.listdir_adding("ghost.mp4"):
            code, _, _ = self.drive()
        self.assertEqual(code, 0)
        manifest = self.only_manifest()
        self.assertEqual(manifest["skipped_outputs"],
                         [{"name": "ghost.mp4", "reason": "unreadable: No such file or directory"}])
        self.assertEqual([entry["name"] for entry in manifest["mp4"]], ["video.mp4"])

    def test_a_name_that_is_not_utf8_is_skipped(self):
        with self.listdir_adding("bad\udcff.mp4"):
            code, _, _ = self.drive()
        self.assertEqual(code, 0)
        manifest = self.only_manifest()
        self.assertEqual(manifest["skipped_outputs"],
                         [{"name": "bad\\xff.mp4", "reason": "name is not UTF-8"}])
        self.assertEqual(manifest["outcome"], "succeeded")

    def test_an_mp4_over_the_size_cap_is_not_copied(self):
        self.webui.scenarios = [scenario(outputs={"video.mp4": MP4_BYTES,
                                                  "huge.mp4": ("sparse", 4 * 1024 ** 3 + 1)})]
        code, _, _ = self.drive()
        self.assertEqual(code, 0)
        run_dir = self.run_dirs()[0]
        manifest = self.manifest(run_dir)
        self.assertEqual(manifest["skipped_outputs"], [{"name": "huge.mp4", "reason": "too large"}])
        self.assertFalse((run_dir / "huge.mp4").exists())
        self.assertEqual([entry["name"] for entry in manifest["mp4"]], ["video.mp4"])

    def assert_first_attempt(self, outcome: tuple, attempts: int = 3) -> dict:
        dirs = self.run_dirs()
        self.assertEqual(len(dirs), attempts)
        manifest = self.manifest(dirs[0])
        self.assertEqual((manifest["outcome"], manifest["failure_class"], manifest["failure_reason"]),
                         outcome)
        return manifest

    def test_an_mp4_larger_than_the_free_space_is_an_infra_failure(self):
        full = types.SimpleNamespace(f_bavail=0, f_frsize=4096)
        with mock.patch.object(DRIVER.os, "statvfs", return_value=full):
            code, _, _ = self.drive()
        self.assertEqual(code, 0)
        manifest = self.assert_first_attempt(("failed", "infra", "disk: no space for video.mp4"))
        self.assertEqual(manifest["skipped_outputs"], [{"name": "video.mp4", "reason": "no space"}])
        self.assertFalse((self.run_dirs()[0] / "video.mp4").exists())

    def test_a_full_disk_while_copying_is_an_infra_failure_without_a_partial_file(self):
        real_write = os.write

        def write(descriptor, data):
            if bytes(data) == MP4_BYTES:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(descriptor, data)

        with mock.patch.object(DRIVER.os, "write", write):
            code, _, _ = self.drive()
        self.assertEqual(code, 0)
        manifest = self.assert_first_attempt(("failed", "infra", "disk: no space for video.mp4"))
        self.assertEqual(manifest["skipped_outputs"], [{"name": "video.mp4", "reason": "no space"}])
        for run_dir in self.run_dirs():
            self.assertFalse((run_dir / "video.mp4").exists())

    def test_a_file_that_grows_or_fails_mid_copy_leaves_no_partial_file(self):
        real_read, real_fstat = os.read, os.fstat

        def grows(descriptor, count):
            data = real_read(descriptor, count)
            return data + b"!" if data else data

        def fails_at_the_end(descriptor, count):
            data = real_read(descriptor, count)
            if not data:
                raise OSError(errno.EIO, "Input/output error")
            return data

        cases = {"unreadable: the file grew while it was copied": grows,
                 "unreadable: Input/output error": fails_at_the_end}
        for reason, faulty in cases.items():
            with self.subTest(reason):
                def read(descriptor, count, faulty=faulty):
                    if stat.S_ISREG(real_fstat(descriptor).st_mode):
                        return faulty(descriptor, count)
                    return real_read(descriptor, count)

                with mock.patch.object(DRIVER.os, "read", read):
                    code, _, _ = self.drive()
                self.assertEqual(code, 0)
                run_dir = self.run_dirs()[-1]
                manifest = self.manifest(run_dir)
                self.assertEqual(manifest["skipped_outputs"], [{"name": "video.mp4", "reason": reason}])
                self.assertFalse((run_dir / "video.mp4").exists())
                self.assertEqual(manifest["failure_class"], "model")

    def test_entries_past_the_cap_get_one_summary(self):
        self.webui.scenarios = [scenario(outputs={"a.mp4": MP4_BYTES, "b.mp4": MP4_BYTES,
                                                  "c.mp4": MP4_BYTES, "d.mp4": MP4_BYTES})]
        with mock.patch.object(DRIVER, "MAX_MP4_FILES", 2):
            code, _, _ = self.drive()
        self.assertEqual(code, 0)
        manifest = self.only_manifest()
        self.assertEqual([entry["name"] for entry in manifest["mp4"]], ["a.mp4", "b.mp4"])
        self.assertEqual(manifest["skipped_outputs"], [{"name": "outputs", "reason": "2 more .mp4 entries"}])

    def test_a_failure_while_collecting_is_an_infra_failure(self):
        with mock.patch.object(DRIVER.Driver, "copy", side_effect=RuntimeError("boom")):
            code, _, _ = self.drive()
        self.assertEqual(code, 0)
        manifest = self.assert_first_attempt(("failed", "infra", "driver: collect failed: RuntimeError"))
        self.assertEqual(manifest["mp4"], [])
        self.assertEqual(manifest["skipped_outputs"], [{"name": "outputs", "reason": "collect failed: RuntimeError"}])

    def test_a_failure_while_collecting_keeps_the_entries_already_probed(self):
        self.webui.scenarios = [scenario(outputs={"a.mp4": MP4_BYTES, "b.mp4": MP4_BYTES})]
        real_copy = DRIVER.Driver.copy
        copies = []

        def copy(directory, name, target):
            copies.append(name)
            if len(copies) == 2:
                raise RuntimeError("boom")
            return real_copy(directory, name, target)

        with mock.patch.object(DRIVER.Driver, "copy", staticmethod(copy)):
            code, _, _ = self.drive()
        self.assertEqual(code, 0)
        manifest = self.only_manifest()
        self.assertEqual([(entry["name"], entry["valid"]) for entry in manifest["mp4"]], [("a.mp4", True)])
        self.assertEqual(manifest["skipped_outputs"], [{"name": "outputs", "reason": "collect failed: RuntimeError"}])
        self.assertEqual(manifest["outcome"], "succeeded")

    def test_counts_equal_the_hand_count(self):
        output = [
            call("bash_tool", "a"), result("a", "ok"),
            call("generate_image", "b"), result("b", '{"status": "ok"}'),
            call("generate_speech", "c"), result("c", "Saved line1.mp3"),
            call("edit_image", "d"), result("d", "{}"),
            call("generate_speech", "e"), result("e", "Saved line2.mp3"),
            call("view", "f"), result("f", "ok"),
            call("generate_image", "g"), result("g", "{}"),
            {"type": "message", "content": [{"type": "output_text", "text": "narration"}]},
        ]
        self.webui.scenarios = [scenario(message={"done": True, "output": output})]
        self.drive()
        self.assertEqual(self.only_manifest()["counts"],
                         {"tool_calls": 7, "image_calls": 3, "speech_calls": 2})


class OutcomeTableTests(DriverCase):
    """Each row of the outcome table, first match wins, from a fake chat."""

    def outcome(self, **overrides) -> dict:
        self.webui.scenarios = [scenario(**overrides)]
        code, _, _ = self.drive()
        self.assertEqual(code, 0)
        manifest = self.manifest(self.run_dirs()[0])
        return {"outcome": manifest["outcome"], "class": manifest["failure_class"],
                "reason": manifest["failure_reason"]}

    def assert_outcome(self, expected: tuple, **overrides):
        got = self.outcome(**overrides)
        self.assertEqual((got["outcome"], got["class"], got["reason"]), expected)

    def test_row_1_done_without_error_and_a_valid_mp4_succeeds(self):
        self.assert_outcome(("succeeded", None, None))

    def test_row_2_time_limit(self):
        with mock.patch.object(DRIVER, "TIME_LIMIT_SECONDS", 0.1):
            # Even a finished message with a valid MP4 is a model failure once stopped.
            self.assert_outcome(("failed", "model", "time limit"), until_stop=True)

    def test_row_3_pending_ask_user(self):
        # Row 3 precedes row 6: the loop waits on the user, so the message is not done.
        output = [call("ask_user", "q", status="pending")]
        self.assert_outcome(("failed", "model", "asked the user"),
                            message={"done": False, "output": output}, outputs={})

    def test_row_4_tool_call_limit_with_a_valid_mp4_is_model(self):
        self.assert_outcome(("failed", "model", "tool-call limit"),
                            message={"done": True, "output": [],
                                     "error": {"content": "Tool-call limit reached (30 iterations)."}})

    def test_row_5_other_message_error_is_infra(self):
        error = f"502 upstream unavailable {PLANTED_KEY}"
        got = self.outcome(message={"done": True, "output": [], "error": {"content": error}})
        self.assertEqual((got["outcome"], got["class"]), ("failed", "infra"))
        self.assertEqual(got["reason"], "model API: 502 upstream unavailable [REDACTED]")

    def test_row_5_reason_is_cut_to_200_characters(self):
        got = self.outcome(message={"done": True, "output": [], "error": {"content": "x" * 500}})
        self.assertEqual(len(got["reason"]), 200)
        self.assertTrue(got["reason"].startswith("model API: xxx"))

    def test_row_6_empty_task_list_with_done_false_is_task_lost(self):
        self.assert_outcome(("failed", "infra", "task lost"), message={"done": False, "output": []})

    def test_row_7_failed_webui_call(self):
        got = self.outcome(tasks_status=503)
        self.assertEqual((got["outcome"], got["class"]), ("failed", "infra"))
        self.assertRegex(got["reason"], r"^GET /api/tasks/chat/[0-9a-f-]{36}: HTTP 503$")

    def test_row_7_missing_chat_answered_404(self):
        # The fixture's contract: a 404 for the chat is "chat missing".
        self.assert_outcome(("failed", "infra", "chat missing"), chat_status=404)

    def test_row_7_unknown_chat_is_refused_after_a_fresh_sign_in(self):
        # WebUI answers a chat it cannot find with 401: one new sign-in, then infra.
        got = self.outcome(forget=True)
        self.assertEqual((got["outcome"], got["class"]), ("failed", "infra"))
        self.assertRegex(got["reason"], r"^GET /api/v1/chats/[0-9a-f-]{36}: HTTP 401$")
        self.assertEqual(len(self.webui.tokens), 1 + 3, "one sign-in per batch and one per attempt")
        self.assert_no_leak_anywhere()

    def test_row_8_speech_synthesis_error_without_mp4_is_adapter(self):
        output = [call("generate_speech", "s"),
                  result("s", "Error: speech synthesis failed: HTTP 502: upstream HTTP 503")]
        self.assert_outcome(("failed", "infra", "adapter: generate_speech"),
                            message={"done": True, "output": output}, outputs={})

    def test_row_8_image_error_without_mp4_is_adapter(self):
        output = [call("generate_image", "i"),
                  result("i", json.dumps({"error": "400: [ERROR: upstream HTTP 502]"}))]
        self.assert_outcome(("failed", "infra", "adapter: generate_image"),
                            message={"done": True, "output": output}, outputs={})

    def test_row_8_image_without_status_without_mp4_is_adapter(self):
        output = [call("edit_image", "i"),
                  result("i", json.dumps({"error": "400: Something went wrong :/"}))]
        self.assert_outcome(("failed", "infra", "adapter: edit_image"),
                            message={"done": True, "output": output}, outputs={})

    def test_row_8_wrapper_transport_prefix_without_mp4_is_orchestrator(self):
        output = [call("bash_tool", "b"),
                  result("b", "[MCP TRANSPORT ERROR] session terminated")]
        self.assert_outcome(("failed", "infra", "orchestrator: bash_tool"),
                            message={"done": True, "output": output}, outputs={})

    def test_row_8_wrapper_crash_without_mp4_is_orchestrator(self):
        output = [call("view", "v"),
                  result("v", "[Error] view wrapper crashed: RuntimeError: boom")]
        self.assert_outcome(("failed", "infra", "orchestrator: view"),
                            message={"done": True, "output": output}, outputs={})

    def test_row_8_does_not_apply_with_a_valid_mp4(self):
        output = [call("generate_speech", "s"), result("s", "Error: speech synthesis timed out after 120s")]
        self.assert_outcome(("succeeded", None, None), message={"done": True, "output": output})

    def test_row_9_speech_argument_error_without_mp4_is_model(self):
        output = [call("generate_speech", "s"), result("s", "Error: text must be 1 to 2000 characters"),
                  call("bash_tool", "b"), result("b", "Error: command exited 1")]
        self.assert_outcome(("failed", "model", "no valid MP4"),
                            message={"done": True, "output": output}, outputs={})

    def test_row_9_invalid_mp4_is_model_and_keeps_the_file(self):
        self.write_state("probes.json", {"video.mp4": {
            "streams": [{"codec_type": "audio", "codec_name": "aac"}],
            "format": {"duration": "3.0"}}})
        self.assert_outcome(("failed", "model", "no valid MP4"))
        manifest = self.manifest(self.run_dirs()[0])
        self.assertEqual(manifest["mp4"][0]["valid"], False)
        self.assertIsNone(manifest["mp4"][0]["video_codec"])
        self.assertTrue((self.run_dirs()[0] / "video.mp4").is_file())


if __name__ == "__main__":
    unittest.main()
