# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Native wrapped initializer: readiness/auth failure stays unmarked and secret-free."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

from support import ROOT


WRAPPER = ROOT / "deploy" / "production-like-test" / "init" / "run-init.sh"
SOURCE = ROOT / "openwebui"
SENTINEL = "sentinel-admin-password-9f3c"
PRIMARY_MODEL = "qwen3.8-27b"
MARKER_NAME = ".computer-use-initialized"


class FakeWebUI:
    def __init__(self):
        self.mode = "unavailable"
        self.calls: list[str] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def _handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_GET(self):
                owner.calls.append(f"GET {self.path}")
                if self.path == "/api/version" and owner.mode != "unavailable":
                    return self._json(200, {"version": "test"})
                if self.path == "/api/models" and owner.mode == "success":
                    return self._json(200, {"data": [{"id": PRIMARY_MODEL}]})
                if self.path.startswith("/api/v1/tools/id/") and owner.mode == "success":
                    self.send_error(404)
                    return
                if self.path.startswith("/api/v1/functions/id/") and owner.mode == "success":
                    self.send_error(404)
                    return
                if self.path == "/api/v1/configs/models" and owner.mode == "success":
                    return self._json(200, {})
                self.send_error(503)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                owner.calls.append(f"POST {self.path}")
                if self.path in {"/api/v1/auths/signin", "/api/v1/auths/signup"}:
                    if owner.mode == "auth-failure":
                        return self._json(401, {"detail": "invalid"})
                    if owner.mode == "success" and self.path.endswith("signin"):
                        return self._json(200, {"token": "admin-token"})
                    self.send_error(401)
                    return
                if owner.mode == "success":
                    if self.path.endswith("/toggle") or self.path.endswith("/toggle/global"):
                        return self._json(200, {"is_active": True, "is_global": True})
                    return self._json(200, {"id": "ok", "is_active": True, "is_global": True})
                self.send_error(503)

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

    def test_unavailable_api_exits_nonzero_without_marker_or_secret(self):
        self.webui.mode = "unavailable"
        result = self.run_wrapper()
        self.assertNotEqual(result.returncode, 0, self.combined(result))
        self.assertFalse((self.data / MARKER_NAME).exists())
        self.assertNotIn(SENTINEL, self.combined(result))


    def test_auth_failure_exits_nonzero_without_marker_or_secret(self):
        self.webui.mode = "auth-failure"
        result = self.run_wrapper()
        self.assertNotEqual(result.returncode, 0, self.combined(result))
        self.assertFalse((self.data / MARKER_NAME).exists())
        self.assertNotIn(SENTINEL, self.combined(result))
        self.assertNotIn("password=", self.combined(result).lower())

    def test_success_after_failure_writes_marker_without_secret(self):
        self.webui.mode = "auth-failure"
        failed = self.run_wrapper()
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse((self.data / MARKER_NAME).exists())
        self.webui.mode = "success"
        succeeded = self.run_wrapper()
        self.assertEqual(succeeded.returncode, 0, self.combined(succeeded))
        self.assertTrue((self.data / MARKER_NAME).is_file())
        self.assertNotIn(SENTINEL, self.combined(succeeded))
        skipped = self.run_wrapper()
        self.assertEqual(skipped.returncode, 0, self.combined(skipped))
        self.assertIn("Already initialized", skipped.stdout)


if __name__ == "__main__":
    unittest.main()
