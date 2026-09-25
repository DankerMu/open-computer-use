# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Public renderer contract: fail-closed inputs and private atomic replacement."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

RENDERER = Path(__file__).resolve().parents[1] / "render.py"
spec = importlib.util.spec_from_file_location("ocu_proxy_renderer", RENDERER)
render_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render_module)
NGINX = "/opt/homebrew/bin/nginx"
ORIGIN = "http://127.0.0.1:18780"


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory(prefix="ocu-proxy-render-")
        self.addCleanup(self.sandbox.cleanup)
        self.output = Path(self.sandbox.name) / "nginx.conf"
        self.env = {"OCU_INTERNAL_TOKEN": "synthetic-render-token", "OCU_WEBUI_ORIGIN": ORIGIN,
                    "OCU_PROXY_LISTEN": "127.0.0.1:18782",
                    "OCU_WEBUI_UPSTREAM": ORIGIN,
                    "OCU_PROXY_UPSTREAM": "http://127.0.0.1:18790"}

    def render(self, **kwargs):
        return render_module.render(output=self.output, env=self.env, nginx=NGINX, **kwargs)

    def test_private_atomic_render_preserves_previous_config_on_invalid_inputs(self):
        self.render()
        original = self.output.read_bytes()
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.output.parent / "runtime").stat().st_mode), 0o700)
        self.assertEqual(subprocess.run([NGINX, "-t", "-c", str(self.output)], capture_output=True).returncode, 0)
        for invalid in ("", "space token", "\tsecret", "\nsecret", "é", "a\x00b"):
            with self.subTest(unsafe_token=repr(invalid)):
                self.env["OCU_INTERNAL_TOKEN"] = invalid
                with self.assertRaisesRegex(render_module.RenderError, "OCU_INTERNAL_TOKEN") as raised:
                    self.render()
                self.assertNotIn(invalid, str(raised.exception)) if invalid else None
                self.assertEqual(self.output.read_bytes(), original)
        self.env["OCU_INTERNAL_TOKEN"] = "synthetic-render-token"
        for name, value in (("OCU_WEBUI_ORIGIN", "https://safe.test/evil\nhttp {"),
                            ("OCU_PROXY_UPSTREAM", "http://127.0.0.1:18790/a;return 200"),
                            ("OCU_PROXY_LISTEN", "127.0.0.1:18782;#")):
            with self.subTest(name=name):
                previous = self.env[name]
                self.env[name] = value
                try:
                    with self.assertRaises(render_module.RenderError) as raised:
                        self.render()
                    self.assertIn(name, str(raised.exception))
                    self.assertNotIn(value, str(raised.exception))
                    self.assertEqual(self.output.read_bytes(), original)
                finally:
                    self.env[name] = previous

    def test_every_visible_ascii_token_byte_renders_as_valid_nginx(self):
        tokens = ("".join(chr(n) for n in range(0x21, 0x7F)),
                  "a@@b", "@@LOCATIONS@@", "$http_host${x}",
                  "\"\\;{}#@:+")
        for token in tokens:
            with self.subTest(token_length=len(token)):
                self.env["OCU_INTERNAL_TOKEN"] = token
                self.render()
                self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
                self.assertEqual(subprocess.run([NGINX, "-t", "-c", str(self.output)],
                                                capture_output=True).returncode, 0)

    def test_public_runtime_or_symlink_target_cannot_replace_private_config(self):
        self.render()
        original = self.output.read_bytes()
        temp_dir = self.output.parent / "runtime" / "proxy"
        temp_dir.chmod(0o755)
        try:
            with self.assertRaises(render_module.RenderError):
                self.render()
            self.assertEqual(self.output.read_bytes(), original)
        finally:
            temp_dir.chmod(0o700)
        link = self.output.parent / "link.conf"
        link.symlink_to(self.output)
        with self.assertRaises(render_module.RenderError):
            render_module.render(output=link, env=self.env, nginx=NGINX)
        self.assertEqual(self.output.read_bytes(), original)
        error_log = self.output.parent / "runtime" / "error.log"
        error_log.unlink(missing_ok=True)
        error_log.symlink_to(self.output)
        try:
            with self.assertRaises(render_module.RenderError):
                self.render()
            self.assertEqual(self.output.read_bytes(), original)
        finally:
            error_log.unlink()

    def test_failed_native_validation_leaves_prior_config_usable(self):
        self.render()
        original = self.output.read_bytes()
        with self.assertRaisesRegex(render_module.RenderError, "nginx configuration validation failed"):
            render_module.render(output=self.output, env=self.env, nginx="/usr/bin/false")
        self.assertEqual(self.output.read_bytes(), original)
        self.assertEqual(subprocess.run([NGINX, "-t", "-c", str(self.output)], capture_output=True).returncode, 0)

    def test_bad_route_table_does_not_replace_valid_configuration(self):
        self.render()
        original = self.output.read_bytes()
        fixture = json.loads((RENDERER.parent / "routes.json").read_text())
        for label, change, reason in (
            ("unknown method", lambda rows: rows[0].update(methods=["DELETE"]), "methods"),
            ("no auth", lambda rows: rows[0].update(auth="none"), "auth"),
            ("unknown row", lambda rows: rows[0].update(path="internal/launch/{chat}"), "path"),
            ("ambiguous parameter", lambda rows: rows[0].update(path="api/outputs/{unknown}"), "path"),
            ("nonmutating POST", lambda rows: rows[13].update(mutating=False), "mutation"),
            ("wrong static prefix", lambda rows: rows[-1].update(prefix="strip"), "prefix"),
        ):
            with self.subTest(label=label):
                candidate = json.loads(json.dumps(fixture))
                change(candidate["rows"])
                table = Path(self.sandbox.name) / "routes.json"
                table.write_text(json.dumps(candidate))
                with self.assertRaisesRegex(render_module.RenderError, reason):
                    self.render(table=table)
                self.assertEqual(self.output.read_bytes(), original)
        changed = json.loads(json.dumps(fixture))
        changed["rows"][0]["path"] = "api/outputs/{chat}/extra"
        table.write_text(json.dumps(changed))
        with self.assertRaisesRegex(render_module.RenderError, "reviewed inventory"):
            self.render(table=table)
        self.assertEqual(self.output.read_bytes(), original)
        table.write_text('{"version":1,"version":1,"rows":[]}')
        with self.assertRaises(render_module.RenderError):
            self.render(table=table)
        self.assertEqual(self.output.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
