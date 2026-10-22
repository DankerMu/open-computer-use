# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""One-off wrapper-model removal (#590): refusal, exact match, idempotence, no secrets."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
import unittest

from support import ROOT

from tests.orchestrator._http_origin import HttpOriginLifetime, OriginRequestHandler


SCRIPT = ROOT / "deploy" / "production-like-test" / "init" / "remove-wrapper-model.py"
SOURCE_ROOT = ROOT / "openwebui"
SHIPPED_FILTER = SOURCE_ROOT / "functions" / "computer_link_filter.py"
SENTINEL = "sentinel-admin-password-5b1e"
TOKEN = "sentinel-session-token-77ad"
ADMIN_EMAIL = "admin@example.test"

# The old init.sh created the wrapper with id == base_model_id; Open WebUI
# stores such a row as a base-model override (base_model_id None), which
# /api/v1/models/export omits and /api/v1/models/base lists.
WRAPPER = {
    "id": "qwen3.8-27b",
    "name": "qwen3.8-27b (Computer Use)",
    "base_model_id": None,
    "meta": {"toolIds": ["ai_computer_use"], "filterIds": ["computer_use_filter"]},
    "params": {"function_calling": "native"},
}
DERIVED_WRAPPER = {
    "id": "custom-computer-use",
    "name": "custom-computer-use (Computer Use)",
    "base_model_id": "deepseek-flash-guan",
    "meta": {"toolIds": ["ai_computer_use"]},
    "params": {},
}
SUFFIX_ONLY = {
    "id": "custom-suffix",
    "name": "custom-suffix (Computer Use)",
    "base_model_id": "qwen3.8-27b",
    "meta": {"toolIds": ["web_search"]},
    "params": {},
}
TOOL_ONLY = {
    "id": "research-helper",
    "name": "Research helper",
    "base_model_id": "deepseek-flash-guan",
    "meta": {"toolIds": ["web_search", "ai_computer_use"]},
    "params": {},
}
UNRELATED = {
    "id": "deepseek-flash-guan",
    "name": "deepseek-flash-guan",
    "base_model_id": None,
    "meta": {},
    "params": {},
}


class FakeWebUI:
    def __init__(self):
        self.stored_filter: str | None = None
        self.models: list[dict] = []
        self.calls: list[str] = []
        self.deleted: list[object] = []
        self.lock = threading.Lock()
        self._lifetime = None

    def _handler(self):
        owner = self

        class Handler(OriginRequestHandler):
            def log_message(self, format, *args):
                return

            def _json(self, status, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _payload(self):
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length) or b"null")

            def _authorized(self):
                return self.headers.get("Authorization") == f"Bearer {TOKEN}"

            def do_GET(self):
                with owner.lock:
                    owner.calls.append(f"GET {self.path}")
                if not self._authorized():
                    return self._json(401, {"detail": "unauthorized"})
                if self.path == "/api/v1/functions/id/computer_use_filter":
                    if owner.stored_filter is None:
                        return self._json(401, {"detail": "not found"})
                    return self._json(
                        200, {"id": "computer_use_filter", "content": owner.stored_filter}
                    )
                # Mirrors the fork: export lists derived rows only
                # (base_model_id set), base lists base-model rows only.
                if self.path == "/api/v1/models/export":
                    return self._json(
                        200, [m for m in owner.models if m["base_model_id"] is not None]
                    )
                if self.path == "/api/v1/models/base":
                    return self._json(
                        200, [m for m in owner.models if m["base_model_id"] is None]
                    )
                return self._json(404, {"detail": "unexpected"})

            def do_POST(self):
                payload = self._payload()
                with owner.lock:
                    owner.calls.append(f"POST {self.path}")
                if self.path == "/api/v1/auths/signin":
                    if payload == {"email": ADMIN_EMAIL, "password": SENTINEL}:
                        return self._json(200, {"token": TOKEN})
                    return self._json(400, {"detail": "wrong credentials"})
                if not self._authorized():
                    return self._json(401, {"detail": "unauthorized"})
                if self.path == "/api/v1/models/model/delete":
                    with owner.lock:
                        owner.deleted.append(payload)
                        before = len(owner.models)
                        owner.models = [m for m in owner.models if m["id"] != payload.get("id")]
                        found = len(owner.models) != before
                    if not found:
                        return self._json(401, {"detail": "not found"})
                    return self._json(200, True)
                return self._json(404, {"detail": "unexpected"})

        return Handler

    @property
    def url(self):
        host, port = self._lifetime.server.server_address
        return f"http://{host}:{port}"

    def start(self):
        self._lifetime = HttpOriginLifetime(self._handler())

    def stop(self):
        if self._lifetime is not None:
            self._lifetime.stop()


class RemoveWrapperModelTests(unittest.TestCase):
    def setUp(self):
        self.webui = FakeWebUI()
        self.addCleanup(self.webui.stop)
        self.webui.start()
        self.shipped = SHIPPED_FILTER.read_text(encoding="utf-8")
        self.rows = [WRAPPER, DERIVED_WRAPPER, SUFFIX_ONLY, TOOL_ONLY, UNRELATED]
        self.webui.models = copy.deepcopy(self.rows)

    def run_script(self, password=SENTINEL):
        env = {
            name: value
            for name, value in os.environ.items()
            if name.lower() not in {"http_proxy", "https_proxy", "all_proxy"}
        }
        env.update({
            "WEBUI_URL": self.webui.url,
            "ADMIN_EMAIL": ADMIN_EMAIL,
            "ADMIN_PASSWORD": password,
            "OCU_INIT_SOURCE_ROOT": str(SOURCE_ROOT),
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        })
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )
        output = result.stdout + result.stderr
        self.assertNotIn(SENTINEL, output)
        self.assertNotIn(TOKEN, output)
        return result

    def test_refuses_until_the_stored_filter_is_the_shipped_file(self):
        for stored in (
            None,
            "old filter that still requires a per-model tool binding\n",
            self.shipped + "\n",
            self.shipped.replace("from ", "from open_webui.", 1),
        ):
            with self.subTest(stored=None if stored is None else stored[:40]):
                self.webui.calls.clear()
                self.webui.stored_filter = stored
                result = self.run_script()
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("re-run open-webui-init", result.stderr)
                self.assertNotIn("GET /api/v1/models/export", self.webui.calls)
                self.assertNotIn("GET /api/v1/models/base", self.webui.calls)
                self.assertEqual(self.webui.deleted, [])
                self.assertEqual(self.webui.models, self.rows)

    def test_removes_base_and_derived_wrappers_then_reports_nothing_to_remove(self):
        self.webui.stored_filter = self.shipped
        first = self.run_script()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        removed = [{"id": WRAPPER["id"]}, {"id": DERIVED_WRAPPER["id"]}]
        self.assertCountEqual(self.webui.deleted, removed)
        self.assertEqual(len(self.webui.deleted), 2)
        self.assertEqual(self.webui.models, [SUFFIX_ONLY, TOOL_ONLY, UNRELATED])
        self.assertIn("GET /api/v1/models/base", self.webui.calls)
        self.assertIn("GET /api/v1/models/export", self.webui.calls)
        self.assertIn(f"removed {WRAPPER['id']}", first.stdout)
        self.assertIn(f"removed {DERIVED_WRAPPER['id']}", first.stdout)
        self.assertNotIn("nothing to remove", first.stdout)

        second = self.run_script()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("nothing to remove", second.stdout)
        self.assertEqual(len(self.webui.deleted), 2)
        self.assertEqual(self.webui.models, [SUFFIX_ONLY, TOOL_ONLY, UNRELATED])

    def test_failed_admin_sign_in_refuses_before_reading_anything(self):
        self.webui.stored_filter = self.shipped
        result = self.run_script(password="not-the-admin-password")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("admin sign-in failed", result.stderr)
        self.assertEqual(self.webui.calls, ["POST /api/v1/auths/signin"])
        self.assertEqual(self.webui.models, self.rows)

    def test_shipped_filter_survives_the_webui_import_rewrite(self):
        # Open WebUI rewrites these substrings in every stored function, which
        # would make the byte-for-byte refresh check impossible to satisfy.
        for rewritten in ("from utils", "from apps", "from main", "from config"):
            self.assertNotIn(rewritten, self.shipped)


if __name__ == "__main__":
    unittest.main()
