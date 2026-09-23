# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Public prefix for the preview shell, static mount, and browser-viewer addresses.

Seams are the real FastAPI app (TestClient HTTP), process import of `app`,
and the actual BrowserViewer class executed in Node's vm.SourceTextModule.
Expected URLs are the ocu-public-prefix spec literals.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = ROOT / "computer-use-server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

CHAT = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
INTERNAL = "ocu-preview-prefix-test-token"
MCP_KEY = "ocu-preview-prefix-mcp-key"
NODE = os.environ.get("OCU_TEST_NODE") or shutil.which("node")
if not NODE and os.path.isfile("/Users/danker/.nvm/versions/node/v22.23.2/bin/node"):
    NODE = "/Users/danker/.nvm/versions/node/v22.23.2/bin/node"
VIEWER = SERVER_DIR / "static" / "browser-viewer.js"

_APP_MODULES = (
    "app",
    "auth_guard",
    "mcp_tools",
    "docker_manager",
    "context_vars",
    "security",
    "system_prompt",
    "skill_manager",
    "cli_runtime",
    "uploads",
    "docs_html",
)

_BASELINE_ASSETS = (
    'href="/static/preview.css"',
    'href="/static/github.min.css"',
    'href="/static/github-dark.min.css"',
    'href="/static/katex/katex.min.css"',
    'href="/static/xterm.css"',
    'src="/static/highlight.min.js"',
    'src="/static/highlightjs-line-numbers.min.js"',
    'src="/static/marked.min.js"',
    'src="/static/xterm.min.js"',
    'src="/static/xterm-addon-fit.min.js"',
    'src="/static/xterm-addon-web-links.min.js"',
    'src="/static/preview.js"',
)

_HARNESS = r"""
import fs from 'node:fs';
import vm from 'node:vm';

const [
  sourcePath,
  moduleUrl,
  protocol,
  host,
  chatId,
  firstPageId,
  nextPageId,
] = process.argv.slice(2);

const source = fs.readFileSync(sourcePath, 'utf8');
const fetches = [];
const sockets = [];

class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  constructor(url) {
    this.url = url;
    this.readyState = FakeWebSocket.CONNECTING;
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
    sockets.push(url);
    Promise.resolve().then(() => {
      this.readyState = FakeWebSocket.OPEN;
      if (this.onopen) this.onopen();
    });
  }
  send() {}
  close() { this.readyState = FakeWebSocket.CLOSED; }
}

const context = vm.createContext({
  fetch: async (url) => {
    fetches.push(String(url));
    const pages = fetches.length === 1
      ? [{ type: 'page', id: firstPageId, url: 'https://example.test' }]
      : [
          { type: 'page', id: firstPageId, url: 'chrome://newtab' },
          { type: 'page', id: nextPageId, url: 'https://other.test' },
        ];
    return { json: async () => pages };
  },
  WebSocket: FakeWebSocket,
  location: { protocol, host },
  window: { devicePixelRatio: 1, focus() {}, addEventListener() {} },
  Image: class Image {},
  document: { createElement() { return {}; } },
  ResizeObserver: class {
    observe() {}
    disconnect() {}
  },
  URL,
  Date,
  JSON,
  Promise,
  console,
  setTimeout(fn, ms) { if (!ms) fn(); return 0; },
  setInterval() { return 1; },
  clearTimeout() {},
  clearInterval() {},
  queueMicrotask,
});

const linker = async (specifier) => {
  const stub = new vm.SyntheticModule(['t'], function () {
    this.setExport('t', (key) => key);
  }, { context, identifier: specifier });
  await stub.link(async () => {
    throw new Error(`unexpected stub import ${specifier}`);
  });
  await stub.evaluate();
  return stub;
};

const module = new vm.SourceTextModule(source, {
  context,
  identifier: moduleUrl,
  initializeImportMeta(meta) {
    meta.url = moduleUrl;
  },
});
await module.link(linker);
await module.evaluate();

const canvas = {
  getContext() { return { drawImage() {} }; },
  addEventListener() {},
  focus() {},
  getBoundingClientRect() { return { width: 800, height: 600, top: 0, left: 0 }; },
  parentElement: null,
};
const viewer = new module.namespace.BrowserViewer(canvas, chatId);
await viewer.connect();
await viewer._checkTabSwitch();
process.stdout.write(JSON.stringify({ fetches, sockets }));
"""


def _subprocess_env():
    env = os.environ.copy()
    env["DOCKER_HOST"] = "unix:///tmp/ocu-acceptance-no-docker.sock"
    env["OCU_INTERNAL_TOKEN"] = INTERNAL
    env["MCP_API_KEY"] = MCP_KEY
    env["PUBLIC_BASE_URL"] = "http://ocu.example"
    env["OCU_WEBUI_ORIGIN"] = "https://webui.example"
    env["OCU_SANDBOX_SUBNET"] = "10.90.0.0/24"
    env["SINGLE_USER_MODE"] = "true"
    env["BASE_DATA_DIR"] = "/tmp/ocu-preview-prefix-unused"
    return env


@contextmanager
def _isolated_app(prefix=None):
    snapshot = sys.modules.copy()
    saved_env = os.environ.copy()
    try:
        os.environ.update(_subprocess_env())
        if prefix is None:
            os.environ.pop("OCU_PUBLIC_PREFIX", None)
        else:
            os.environ["OCU_PUBLIC_PREFIX"] = prefix
        for name in list(sys.modules):
            if name in _APP_MODULES or name.startswith("mcp_resources"):
                sys.modules.pop(name, None)
        import app as loaded

        yield loaded
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        for name in list(sys.modules):
            if name not in snapshot:
                sys.modules.pop(name, None)
        sys.modules.update(snapshot)


def _client(loaded):
    from fastapi.testclient import TestClient

    return TestClient(loaded.app, raise_server_exceptions=True)


def _preview(client):
    return client.get(f"/preview/{CHAT}", headers={"Authorization": f"Bearer {INTERNAL}"})


def _field(html, name):
    match = re.search(rf'{name}:\s*"((?:\\.|[^"\\])*)"', html)
    assert match, f"{name} missing from {html[:400]}"
    return json.loads('"' + match.group(1) + '"')


def test_empty_prefix_keeps_baseline_shell_urls_and_adds_unprefixed_describe_url():
    with _isolated_app(None) as loaded:
        response = _preview(_client(loaded))
        html = response.text
        static = _client(loaded).get("/static/preview.css")

    assert response.status_code == 200
    for asset in _BASELINE_ASSETS:
        assert asset in html
    assert _field(html, "apiUrl") == f"/api/outputs/{CHAT}"
    assert _field(html, "filesBase") == f"/files/{CHAT}"
    assert _field(html, "chatId") == CHAT
    assert _field(html, "describeUrl") == f"/api/v1/ocu/workspaces/{CHAT}"
    assert "fetch('/terminal/' + " + json.dumps(CHAT) + " + '/heartbeat')" in html
    assert static.status_code == 200
    assert "text/css" in static.headers["content-type"]
    assert "Preview SPA" in static.text


def test_ocu_prefix_shell_urls_static_mount_auth_and_no_secret():
    with _isolated_app("/ocu") as loaded:
        client = _client(loaded)
        denied = client.get(f"/preview/{CHAT}")
        response = _preview(client)
        html = response.text
        prefixed = client.get("/ocu/static/preview.css")
        unprefixed = client.get("/static/preview.css")

    assert denied.status_code == 401
    assert CHAT not in denied.text
    assert response.status_code == 200
    assert INTERNAL not in html
    assert MCP_KEY not in html
    for asset in _BASELINE_ASSETS:
        assert asset.replace('="/static/', '="/ocu/static/') in html
        assert asset not in html
    assert _field(html, "apiUrl") == f"/ocu/api/outputs/{CHAT}"
    assert _field(html, "filesBase") == f"/ocu/files/{CHAT}"
    assert _field(html, "describeUrl") == f"/api/v1/ocu/workspaces/{CHAT}"
    assert "fetch('/ocu/terminal/' + " + json.dumps(CHAT) + " + '/heartbeat')" in html
    assert prefixed.status_code == 200
    assert "text/css" in prefixed.headers["content-type"]
    assert "Preview SPA" in prefixed.text
    assert unprefixed.status_code == 404


@pytest.mark.parametrize("prefix", ("/tools/ocu", "/.ocu"))
def test_nested_prefix_shell_urls_and_static_mount(prefix):
    with _isolated_app(prefix) as loaded:
        client = _client(loaded)
        response = _preview(client)
        html = response.text
        prefixed = client.get(f"{prefix}/static/preview.css")
        unprefixed = client.get("/static/preview.css")

    assert response.status_code == 200
    assert f'href="{prefix}/static/preview.css"' in html
    assert f'src="{prefix}/static/preview.js"' in html
    assert _field(html, "apiUrl") == f"{prefix}/api/outputs/{CHAT}"
    assert _field(html, "filesBase") == f"{prefix}/files/{CHAT}"
    assert _field(html, "describeUrl") == f"/api/v1/ocu/workspaces/{CHAT}"
    assert f"fetch('{prefix}/terminal/' + " + json.dumps(CHAT) + " + '/heartbeat')" in html
    assert prefixed.status_code == 200
    assert "text/css" in prefixed.headers["content-type"]
    assert unprefixed.status_code == 404


@pytest.mark.parametrize(
    "value",
    ("ocu", "/ocu/", "//ocu", "/../ocu", "/ocu?x=1"),
)
def test_invalid_prefix_fails_import_naming_the_variable(value):
    env = _subprocess_env()
    env["OCU_PUBLIC_PREFIX"] = value
    completed = subprocess.run(
        [sys.executable, "-c", "import app"],
        cwd=str(SERVER_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode != 0
    assert "OCU_PUBLIC_PREFIX" in completed.stderr


def test_browser_viewer_addresses_follow_module_url_and_protocol(tmp_path):
    if not NODE:
        pytest.fail(
            "node is required for browser-viewer address tests; "
            "install Node 22 or set OCU_TEST_NODE"
        )
    harness = tmp_path / "browser_viewer_harness.mjs"
    harness.write_text(_HARNESS)
    cases = (
        (
            "http://ocu.example/static/browser-viewer.js",
            "http:",
            "ocu.example",
            f"/browser/{CHAT}/json",
            f"ws://ocu.example/browser/{CHAT}/devtools/page/page-1",
            f"ws://ocu.example/browser/{CHAT}/devtools/page/page-2",
        ),
        (
            "https://webui.example/ocu/static/browser-viewer.js",
            "https:",
            "webui.example",
            f"/ocu/browser/{CHAT}/json",
            f"wss://webui.example/ocu/browser/{CHAT}/devtools/page/page-1",
            f"wss://webui.example/ocu/browser/{CHAT}/devtools/page/page-2",
        ),
        (
            "http://ocu.example/tools/ocu/static/browser-viewer.js",
            "http:",
            "ocu.example",
            f"/tools/ocu/browser/{CHAT}/json",
            f"ws://ocu.example/tools/ocu/browser/{CHAT}/devtools/page/page-1",
            f"ws://ocu.example/tools/ocu/browser/{CHAT}/devtools/page/page-2",
        ),
        (
            "https://webui.example/tools/ocu/static/browser-viewer.js",
            "https:",
            "webui.example",
            f"/tools/ocu/browser/{CHAT}/json",
            f"wss://webui.example/tools/ocu/browser/{CHAT}/devtools/page/page-1",
            f"wss://webui.example/tools/ocu/browser/{CHAT}/devtools/page/page-2",
        ),
    )
    for module_url, protocol, host, json_path, connect_ws, reconnect_ws in cases:
        completed = subprocess.run(
            [
                NODE,
                "--experimental-vm-modules",
                str(harness),
                str(VIEWER),
                module_url,
                protocol,
                host,
                CHAT,
                "page-1",
                "page-2",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert completed.returncode == 0, completed.stderr
        recorded = json.loads(completed.stdout)
        fetch_paths = [urlsplit(url).path for url in recorded["fetches"]]
        assert fetch_paths == [json_path, json_path], recorded
        assert recorded["sockets"] == [connect_ws, reconnect_ws], recorded
