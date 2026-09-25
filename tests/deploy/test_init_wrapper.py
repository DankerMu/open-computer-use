# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Native wrapped initializer: readiness/auth failure stays unmarked and secret-free."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.parse import urlparse

from support import ROOT


WRAPPER = ROOT / "deploy" / "production-like-test" / "init" / "run-init.sh"
SOURCE = ROOT / "openwebui"
SENTINEL = "sentinel-admin-password-9f3c"
PRIMARY_MODEL = "qwen3.8-27b"
MARKER_NAME = ".computer-use-initialized"
PUBLIC_GRANTS = [
    {"principal_type": "group", "principal_id": "*", "permission": "read"},
    {"principal_type": "user", "principal_id": "*", "permission": "read"},
]
ALLOWED_GET = {
    "/api/version",
    "/api/models",
    "/api/v1/tools/id/ai_computer_use",
    "/api/v1/functions/id/computer_use_filter",
    "/api/v1/configs/models",
    f"/api/v1/models/model?id={PRIMARY_MODEL}",
}
ALLOWED_POST = {
    "/api/v1/auths/signin",
    "/api/v1/auths/signup",
    "/api/v1/tools/create",
    "/api/v1/tools/id/ai_computer_use/update",
    "/api/v1/tools/id/ai_computer_use/valves/update",
    "/api/v1/tools/id/ai_computer_use/access/update",
    "/api/v1/functions/create",
    "/api/v1/functions/id/computer_use_filter/update",
    "/api/v1/functions/id/computer_use_filter/valves/update",
    "/api/v1/functions/id/computer_use_filter/toggle",
    "/api/v1/functions/id/computer_use_filter/toggle/global",
    "/api/v1/configs/models",
    "/api/v1/models/create",
    f"/api/v1/models/model/update?id={PRIMARY_MODEL}",
    "/api/v1/models/model/access/update",
}


def wait_for(predicate, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def descendant_pids(root_pid: int) -> set[int]:
    try:
        listed = subprocess.check_output(["ps", "-ax", "-o", "pid=,ppid="], text=True)
    except subprocess.CalledProcessError:
        return set()
    children: dict[int, list[int]] = {}
    for line in listed.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        pid, ppid = int(parts[0]), int(parts[1])
        children.setdefault(ppid, []).append(pid)
    found: set[int] = set()
    stack = [root_pid]
    while stack:
        current = stack.pop()
        for child in children.get(current, []):
            if child not in found:
                found.add(child)
                stack.append(child)
    return found


def leftover_init(root: Path) -> list[Path]:
    return [
        path
        for path in root.glob("ocu-init.*")
        if path.is_dir() and path.name.startswith("ocu-init.")
    ]


class FakeWebUI:
    def __init__(self):
        self.mode = "unavailable"
        self.calls: list[str] = []
        self.bodies: dict[str, object] = {}
        self.unexpected: list[str] = []
        self.hold_path = ""
        self.entered = threading.Event()
        self.lock = threading.Lock()
        self.existing_config = {
            "DEFAULT_MODELS": "stale-model",
            "DEFAULT_PINNED_MODELS": "keep-pinned",
            "MODEL_ORDER_LIST": ["keep-order"],
            "DEFAULT_MODEL_METADATA": {"keep": True},
            "DEFAULT_MODEL_PARAMS": {"temperature": 0.2, "keep_nested": True},
        }
        self.posted_config = None
        self.tools: dict[str, dict] = {}
        self.functions: dict[str, dict] = {}
        self.workspace_models: dict[str, dict] = {}
        self.tool_valves = None
        self.filter_valves = None
        self.tool_access = None
        self.workspace_access = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def _handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def _read_body(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if not raw:
                    return None
                try:
                    return json.loads(raw.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError):
                    return raw.decode("utf-8", "replace")

            def _record(self, method, payload=None):
                key = f"{method} {self.path}"
                with owner.lock:
                    owner.calls.append(key)
                    if payload is not None:
                        owner.bodies[key] = payload

            def _hold_if_needed(self):
                if not owner.hold_path:
                    return
                marker = Path(owner.hold_path)
                owner.entered.set()
                while marker.exists():
                    time.sleep(0.05)

            def do_GET(self):
                self._hold_if_needed()
                parsed = urlparse(self.path)
                path = parsed.path
                self._record("GET")
                if self.path not in ALLOWED_GET and path not in ALLOWED_GET:
                    with owner.lock:
                        owner.unexpected.append(f"GET {self.path}")
                    self.send_error(404)
                    return
                if owner.mode == "unavailable":
                    self.send_error(503)
                    return
                if path == "/api/version":
                    return self._json(200, {"version": "test"})
                if owner.mode == "auth-failure":
                    self.send_error(503)
                    return
                if path == "/api/models":
                    return self._json(200, {"data": [{"id": PRIMARY_MODEL}]})
                if path == "/api/v1/tools/id/ai_computer_use":
                    tool = owner.tools.get("ai_computer_use")
                    if tool is None:
                        self.send_error(404)
                        return
                    return self._json(200, tool)
                if path == "/api/v1/functions/id/computer_use_filter":
                    function = owner.functions.get("computer_use_filter")
                    if function is None:
                        self.send_error(404)
                        return
                    return self._json(200, function)
                if path == "/api/v1/configs/models":
                    if owner.mode in {"config-read-failure"}:
                        self.send_error(503)
                        return
                    if owner.mode == "config-parse-failure":
                        body = b"not-json"
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    return self._json(200, owner.existing_config)
                if path == "/api/v1/models/model":
                    if owner.mode == "workspace-lookup-failure":
                        self.send_error(503)
                        return
                    model = owner.workspace_models.get(PRIMARY_MODEL)
                    if model is None:
                        self.send_error(404)
                        return
                    return self._json(200, model)
                self.send_error(404)

            def do_POST(self):
                self._hold_if_needed()
                payload = self._read_body()
                parsed = urlparse(self.path)
                path = parsed.path
                self._record("POST", payload)
                if self.path not in ALLOWED_POST and path not in ALLOWED_POST:
                    with owner.lock:
                        owner.unexpected.append(f"POST {self.path}")
                    self.send_error(404)
                    return
                if path in {"/api/v1/auths/signin", "/api/v1/auths/signup"}:
                    if owner.mode == "auth-failure":
                        return self._json(401, {"detail": "invalid"})
                    if owner.mode != "unavailable" and path.endswith("signin"):
                        return self._json(200, {"token": "admin-token"})
                    self.send_error(401)
                    return
                if owner.mode == "unavailable":
                    self.send_error(503)
                    return
                if path == "/api/v1/tools/create":
                    owner.tools["ai_computer_use"] = {"id": "ai_computer_use", **(payload or {})}
                    return self._json(200, owner.tools["ai_computer_use"])
                if path == "/api/v1/tools/id/ai_computer_use/update":
                    owner.tools["ai_computer_use"] = {"id": "ai_computer_use", **(payload or {})}
                    return self._json(200, owner.tools["ai_computer_use"])
                if path == "/api/v1/tools/id/ai_computer_use/valves/update":
                    owner.tool_valves = payload
                    return self._json(200, payload)
                if path == "/api/v1/tools/id/ai_computer_use/access/update":
                    owner.tool_access = payload
                    return self._json(200, payload)
                if path == "/api/v1/functions/create":
                    owner.functions["computer_use_filter"] = {
                        "id": "computer_use_filter",
                        "is_active": False,
                        "is_global": False,
                        **(payload or {}),
                    }
                    return self._json(200, owner.functions["computer_use_filter"])
                if path == "/api/v1/functions/id/computer_use_filter/update":
                    current = owner.functions.get("computer_use_filter", {})
                    current.update(payload or {})
                    owner.functions["computer_use_filter"] = current
                    return self._json(200, current)
                if path == "/api/v1/functions/id/computer_use_filter/valves/update":
                    owner.filter_valves = payload
                    return self._json(200, payload)
                if path.endswith("/toggle/global"):
                    current = owner.functions.setdefault(
                        "computer_use_filter",
                        {"id": "computer_use_filter", "is_active": False, "is_global": False},
                    )
                    current["is_global"] = True
                    return self._json(200, current)
                if path.endswith("/toggle"):
                    current = owner.functions.setdefault(
                        "computer_use_filter",
                        {"id": "computer_use_filter", "is_active": False, "is_global": False},
                    )
                    current["is_active"] = True
                    return self._json(200, current)
                if path == "/api/v1/configs/models":
                    if owner.mode == "config-post-failure":
                        self.send_error(503)
                        return
                    owner.posted_config = payload
                    owner.existing_config = payload
                    return self._json(200, payload)
                if path == "/api/v1/models/create":
                    if owner.mode == "workspace-create-failure":
                        self.send_error(503)
                        return
                    model_id = (payload or {}).get("id", PRIMARY_MODEL)
                    owner.workspace_models[model_id] = payload or {}
                    return self._json(200, payload)
                if path == "/api/v1/models/model/update":
                    model_id = (payload or {}).get("id", PRIMARY_MODEL)
                    owner.workspace_models[model_id] = payload or {}
                    return self._json(200, payload)
                if path == "/api/v1/models/model/access/update":
                    if owner.mode == "workspace-access-failure":
                        self.send_error(503)
                        return
                    owner.workspace_access = payload
                    return self._json(200, payload)
                self.send_error(404)

            def _json(self, status, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    @property
    def url(self):
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def start(self):
        self.thread.start()

    def stop(self):
        self.hold_path = ""
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class InitWrapperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ocu-init-test-")
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        shutil.copytree(SOURCE, self.source)
        self.data = self.root / "data"
        self.data.mkdir()
        self.webui = FakeWebUI()
        self.webui.start()

    def tearDown(self):
        self.webui.stop()
        self.temp.cleanup()

    def run_wrapper(self, extra_env=None, timeout=20):
        env = os.environ.copy()
        env.update({
            "OCU_INIT_SOURCE_ROOT": str(self.source),
            "TMPDIR": str(self.root),
            "WEBUI_URL": self.webui.url,
            "ADMIN_EMAIL": "admin@example.test",
            "ADMIN_PASSWORD": SENTINEL,
            "ADMIN_NAME": "Test Admin",
            "PRIMARY_CHAT_MODEL": PRIMARY_MODEL,
            "ORCHESTRATOR_URL": "http://computer-use-server:8081",
            "MCP_API_KEY": "synthetic-mcp",
            "MARKER_FILE": str(self.data / MARKER_NAME),
            "OCU_INIT_READY_ATTEMPTS": "3",
            "OCU_INIT_READY_SLEEP": "0",
        })
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", str(WRAPPER)],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
            check=False,
        )

    def combined(self, result) -> str:
        return result.stdout + result.stderr

    def assert_no_secret(self, result):
        self.assertNotIn(SENTINEL, self.combined(result))
        self.assertNotIn("password=", self.combined(result).lower())

    def assert_required_setup(self):
        self.assertEqual(self.webui.unexpected, [])
        self.assertIn("GET /api/version", self.webui.calls)
        self.assertIn("POST /api/v1/auths/signin", self.webui.calls)
        self.assertIn("POST /api/v1/tools/create", self.webui.calls)
        self.assertIn("POST /api/v1/functions/create", self.webui.calls)
        self.assertEqual(
            self.webui.tool_valves,
            {
                "ORCHESTRATOR_URL": "http://computer-use-server:8081",
                "MCP_API_KEY": "synthetic-mcp",
                "DEBUG_LOGGING": False,
            },
        )
        self.assertEqual(
            self.webui.filter_valves,
            {
                "ORCHESTRATOR_URL": "http://computer-use-server:8081",
                "ARCHIVE_BUTTON": "on",
                "INJECT_SYSTEM_PROMPT": True,
            },
        )
        self.assertEqual(self.webui.tool_access["access_grants"], PUBLIC_GRANTS)
        function = self.webui.functions["computer_use_filter"]
        self.assertTrue(function["is_active"])
        self.assertTrue(function["is_global"])
        self.assertEqual(self.webui.posted_config["DEFAULT_MODELS"], PRIMARY_MODEL)
        self.assertEqual(
            self.webui.posted_config["DEFAULT_MODEL_PARAMS"],
            {"temperature": 0.2, "keep_nested": True, "function_calling": "native", "stream_response": True},
        )
        self.assertEqual(self.webui.posted_config["DEFAULT_PINNED_MODELS"], "keep-pinned")
        self.assertEqual(self.webui.posted_config["MODEL_ORDER_LIST"], ["keep-order"])
        self.assertEqual(self.webui.posted_config["DEFAULT_MODEL_METADATA"], {"keep": True})
        workspace = self.webui.workspace_models[PRIMARY_MODEL]
        self.assertEqual(workspace["id"], PRIMARY_MODEL)
        self.assertEqual(workspace["base_model_id"], PRIMARY_MODEL)
        self.assertEqual(workspace["meta"]["toolIds"], ["ai_computer_use"])
        self.assertEqual(workspace["meta"]["filterIds"], ["computer_use_filter"])
        self.assertEqual(self.webui.workspace_access["id"], PRIMARY_MODEL)
        self.assertEqual(self.webui.workspace_access["access_grants"], PUBLIC_GRANTS)

    def test_unavailable_api_exits_nonzero_without_marker_or_secret(self):
        self.webui.mode = "unavailable"
        result = self.run_wrapper()
        self.assertNotEqual(result.returncode, 0, self.combined(result))
        self.assertFalse((self.data / MARKER_NAME).exists())
        self.assert_no_secret(result)
        self.assertIn("GET /api/version", self.webui.calls)
        self.assertFalse(any(call.startswith("POST /api/v1/auths/") for call in self.webui.calls))

    def test_auth_failure_exits_nonzero_without_marker_or_secret(self):
        self.webui.mode = "auth-failure"
        result = self.run_wrapper()
        self.assertNotEqual(result.returncode, 0, self.combined(result))
        self.assertFalse((self.data / MARKER_NAME).exists())
        self.assert_no_secret(result)
        self.assertIn("GET /api/version", self.webui.calls)
        self.assertIn("POST /api/v1/auths/signin", self.webui.calls)
        self.assertIn("POST /api/v1/auths/signup", self.webui.calls)
        self.assertFalse(any("tools" in call or "functions" in call or "configs" in call for call in self.webui.calls))

    def test_required_config_post_failure_preserves_existing_and_retries(self):
        self.webui.mode = "config-post-failure"
        original = dict(self.webui.existing_config)
        failed = self.run_wrapper()
        self.assertNotEqual(failed.returncode, 0, self.combined(failed))
        self.assertFalse((self.data / MARKER_NAME).exists())
        self.assert_no_secret(failed)
        self.assertIsNone(self.webui.posted_config)
        self.assertEqual(self.webui.existing_config, original)
        self.assertIn("POST /api/v1/configs/models", self.webui.calls)
        self.assertTrue(self.webui.tools)
        self.assertTrue(self.webui.functions)
        self.assertIn(PRIMARY_MODEL, self.webui.workspace_models)
        self.webui.mode = "success"
        succeeded = self.run_wrapper()
        self.assertEqual(succeeded.returncode, 0, self.combined(succeeded))
        self.assertTrue((self.data / MARKER_NAME).is_file())
        self.assert_no_secret(succeeded)
        self.assert_required_setup()
        self.assertIn(f"POST /api/v1/models/model/update?id={PRIMARY_MODEL}", self.webui.calls)
        skipped = self.run_wrapper()
        self.assertEqual(skipped.returncode, 0, self.combined(skipped))
        self.assertIn("Already initialized", skipped.stdout)

    def test_required_config_read_failure_does_not_post_or_write_marker(self):
        self.webui.mode = "config-read-failure"
        original = dict(self.webui.existing_config)
        result = self.run_wrapper()
        self.assertNotEqual(result.returncode, 0, self.combined(result))
        self.assertFalse((self.data / MARKER_NAME).exists())
        self.assert_no_secret(result)
        self.assertIn("GET /api/v1/configs/models", self.webui.calls)
        self.assertNotIn("POST /api/v1/configs/models", self.webui.calls)
        self.assertIsNone(self.webui.posted_config)
        self.assertEqual(self.webui.existing_config, original)

    def test_required_config_parse_failure_does_not_post_or_write_marker(self):
        self.webui.mode = "config-parse-failure"
        original = dict(self.webui.existing_config)
        result = self.run_wrapper()
        self.assertNotEqual(result.returncode, 0, self.combined(result))
        self.assertFalse((self.data / MARKER_NAME).exists())
        self.assert_no_secret(result)
        self.assertIn("GET /api/v1/configs/models", self.webui.calls)
        self.assertNotIn("POST /api/v1/configs/models", self.webui.calls)
        self.assertIsNone(self.webui.posted_config)
        self.assertEqual(self.webui.existing_config, original)

    def test_workspace_create_failure_does_not_write_marker(self):
        self.webui.mode = "workspace-create-failure"
        result = self.run_wrapper()
        self.assertNotEqual(result.returncode, 0, self.combined(result))
        self.assertFalse((self.data / MARKER_NAME).exists())
        self.assert_no_secret(result)
        self.assertIn("POST /api/v1/models/create", self.webui.calls)
        self.assertEqual(self.webui.workspace_models, {})
        self.assertIsNone(self.webui.workspace_access)

    def test_workspace_lookup_failure_does_not_create_or_write_marker(self):
        self.webui.mode = "workspace-lookup-failure"
        result = self.run_wrapper()
        self.assertNotEqual(result.returncode, 0, self.combined(result))
        self.assertFalse((self.data / MARKER_NAME).exists())
        self.assert_no_secret(result)
        self.assertTrue(any(call.startswith("GET /api/v1/models/model") for call in self.webui.calls))
        self.assertNotIn("POST /api/v1/models/create", self.webui.calls)
        self.assertEqual(self.webui.workspace_models, {})

    def test_workspace_access_failure_keeps_created_model_unmarked(self):
        self.webui.mode = "workspace-access-failure"
        result = self.run_wrapper()
        self.assertNotEqual(result.returncode, 0, self.combined(result))
        self.assertFalse((self.data / MARKER_NAME).exists())
        self.assert_no_secret(result)
        self.assertIn(PRIMARY_MODEL, self.webui.workspace_models)
        self.assertIsNone(self.webui.workspace_access)

    def test_success_after_failure_writes_marker_without_secret(self):
        self.webui.mode = "auth-failure"
        failed = self.run_wrapper()
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse((self.data / MARKER_NAME).exists())
        self.webui.mode = "success"
        succeeded = self.run_wrapper()
        self.assertEqual(succeeded.returncode, 0, self.combined(succeeded))
        self.assertTrue((self.data / MARKER_NAME).is_file())
        self.assert_no_secret(succeeded)
        self.assert_required_setup()
        skipped = self.run_wrapper()
        self.assertEqual(skipped.returncode, 0, self.combined(skipped))
        self.assertIn("Already initialized", skipped.stdout)

    def _signal_during_held_http(self, sig):
        self.webui.mode = "success"
        hold = self.root / "http-hold"
        hold.write_text("1", encoding="utf-8")
        self.webui.hold_path = str(hold)
        env = os.environ.copy()
        env.update({
            "OCU_INIT_SOURCE_ROOT": str(self.source),
            "TMPDIR": str(self.root),
            "WEBUI_URL": self.webui.url,
            "ADMIN_EMAIL": "admin@example.test",
            "ADMIN_PASSWORD": SENTINEL,
            "ADMIN_NAME": "Test Admin",
            "PRIMARY_CHAT_MODEL": PRIMARY_MODEL,
            "ORCHESTRATOR_URL": "http://computer-use-server:8081",
            "MCP_API_KEY": "synthetic-mcp",
            "MARKER_FILE": str(self.data / MARKER_NAME),
            "OCU_INIT_READY_ATTEMPTS": "30",
            "OCU_INIT_READY_SLEEP": "1",
        })
        process = subprocess.Popen(
            ["bash", str(WRAPPER)],
            cwd=str(self.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
        children: set[int] = set()
        try:
            self.assertTrue(self.webui.entered.wait(5), "wrapper never reached held HTTP")
            owned = leftover_init(self.root)
            self.assertEqual(len(owned), 1)
            self.assertEqual(stat.S_IMODE(owned[0].stat().st_mode), 0o700)
            children = descendant_pids(process.pid)
            self.assertTrue(children, "held initializer child was not recorded")
            process.send_signal(sig)
            self.assertTrue(
                wait_for(lambda: process.poll() is not None, timeout=8),
                "wrapper did not exit after signal",
            )
            self.assertTrue(
                wait_for(lambda: all(not _pid_alive(pid) for pid in children), timeout=8),
                "owned initializer child survived parent exit",
            )
            self.assertFalse((self.data / MARKER_NAME).exists())
            self.assertEqual(leftover_init(self.root), [])
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGKILL)
            for pid in children:
                if _pid_alive(pid):
                    os.kill(pid, signal.SIGKILL)
            hold.unlink(missing_ok=True)
            self.webui.hold_path = ""
            process.communicate(timeout=10)
        self.assertNotEqual(process.returncode, 0)

    def test_term_during_held_http_removes_owned_tmp_and_children(self):
        self._signal_during_held_http(signal.SIGTERM)

    def test_hup_during_held_http_removes_owned_tmp_and_children(self):
        self._signal_during_held_http(signal.SIGHUP)


    def test_term_during_generate_removes_owned_tmp_and_children(self):
        fifo = self.source / "init.sh"
        fifo.unlink()
        os.mkfifo(fifo)
        env = os.environ.copy()
        env.update({
            "OCU_INIT_SOURCE_ROOT": str(self.source),
            "TMPDIR": str(self.root),
            "WEBUI_URL": self.webui.url,
            "ADMIN_EMAIL": "admin@example.test",
            "ADMIN_PASSWORD": SENTINEL,
            "ADMIN_NAME": "Test Admin",
            "PRIMARY_CHAT_MODEL": PRIMARY_MODEL,
            "ORCHESTRATOR_URL": "http://computer-use-server:8081",
            "MCP_API_KEY": "synthetic-mcp",
            "MARKER_FILE": str(self.data / MARKER_NAME),
        })
        writer = None
        process = subprocess.Popen(
            ["bash", str(WRAPPER)],
            cwd=str(self.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
        children: set[int] = set()
        try:
            self.assertTrue(
                wait_for(lambda: leftover_init(self.root)),
                "wrapper never created owned generate directory",
            )
            for _ in range(50):
                try:
                    writer = os.open(str(fifo), os.O_WRONLY | os.O_NONBLOCK)
                    break
                except OSError:
                    time.sleep(0.05)
            self.assertIsNotNone(writer, "generator never opened the source fifo")
            owned = leftover_init(self.root)
            self.assertEqual(len(owned), 1)
            self.assertEqual(stat.S_IMODE(owned[0].stat().st_mode), 0o700)
            children = descendant_pids(process.pid)
            self.assertTrue(children, "held generator child was not recorded")
            process.send_signal(signal.SIGTERM)
            self.assertTrue(
                wait_for(lambda: process.poll() is not None, timeout=8),
                "wrapper did not exit after signal",
            )
            self.assertTrue(
                wait_for(lambda: all(not _pid_alive(pid) for pid in children), timeout=8),
                "owned generator survived parent exit",
            )
            self.assertEqual(leftover_init(self.root), [])
            self.assertFalse((self.data / MARKER_NAME).exists())
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGKILL)
            for pid in children:
                if _pid_alive(pid):
                    os.kill(pid, signal.SIGKILL)
            if writer is not None:
                os.close(writer)
            process.communicate(timeout=10)
        self.assertNotEqual(process.returncode, 0)

    def test_int_during_held_http_removes_owned_tmp_and_children(self):
        self._signal_during_held_http(signal.SIGINT)

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


if __name__ == "__main__":
    unittest.main()
