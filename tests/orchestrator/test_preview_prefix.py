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
    "outputs_broker",
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
  Headers,
  fetch: async (url, init = {}) => {
    const headers = {};
    new Headers(init.headers || {}).forEach((value, key) => {
      headers[key.toLowerCase()] = value;
    });
    fetches.push({ url: String(url), headers });
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

const linker = async (specifier, referencingModule) => {
  const resolved = new URL(specifier, referencingModule.identifier);
  const filename = resolved.pathname.split('/').pop();
  const directory = sourcePath.slice(0, sourcePath.lastIndexOf('/') + 1);
  const fileSource = fs.readFileSync(directory + filename, 'utf8');
  const child = new vm.SourceTextModule(fileSource, {
    context,
    identifier: resolved.href,
    initializeImportMeta(meta) {
      meta.url = resolved.href;
    },
  });
  await child.link(linker);
  await child.evaluate();
  return child;
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


def _startup_import(prefix):
    env = _subprocess_env()
    env["OCU_PUBLIC_PREFIX"] = prefix
    return subprocess.run(
        [sys.executable, "-c", "import app"],
        cwd=str(SERVER_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


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
    assert "setInterval(function() { fetch(" not in html
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
    assert "setInterval(function() { fetch(" not in html
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
    assert "setInterval(function() { fetch(" not in html
    assert prefixed.status_code == 200
    assert "text/css" in prefixed.headers["content-type"]
    assert unprefixed.status_code == 404


@pytest.mark.parametrize(
    "value",
    (
        "ocu",
        "/ocu/",
        "//ocu",
        "/../ocu",
        "/ocu?x=1",
        " /ocu",
        "/ocu ",
        "/ocu%2f",
        "/ocu#x",
        "https://example.com/ocu",
        "/./ocu",
        "/ocu/..",
    ),
)
def test_invalid_prefix_fails_import_naming_the_variable(value):
    completed = _startup_import(value)
    assert completed.returncode != 0
    assert "OCU_PUBLIC_PREFIX" in completed.stderr


@pytest.mark.parametrize(
    "prefix",
    (
        "/files",
        "/preview",
        "/browser",
        "/terminal",
        "/internal",
        "/api/outputs",
        "/api/uploads",
        "/files/x",
        "/terminal/a/b",
        "/api/uploads/x",
    ),
)
def test_guarded_namespace_prefix_fails_startup(prefix):
    completed = _startup_import(prefix)
    assert completed.returncode != 0
    assert "OCU_PUBLIC_PREFIX" in completed.stderr


def test_explicit_empty_string_prefix_is_accepted_at_startup():
    env = _subprocess_env()
    env["OCU_PUBLIC_PREFIX"] = ""
    completed = subprocess.run(
        [sys.executable, "-c", "import app; assert app.OCU_PUBLIC_PREFIX == ''"],
        cwd=str(SERVER_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("prefix", ("/api", "/files-ui"))
def test_nonconflicting_prefix_serves_static_without_changing_auth(prefix):
    with _isolated_app(prefix) as loaded:
        client = _client(loaded)
        static = client.get(f"{prefix}/static/preview.css")
        denied = client.get(f"/preview/{CHAT}")
        authorized = _preview(client)

    assert static.status_code == 200
    assert "text/css" in static.headers["content-type"]
    assert "Preview SPA" in static.text
    assert denied.status_code == 401
    assert authorized.status_code == 200
    assert CHAT not in denied.text


def test_invalid_token_on_preview_returns_401():
    with _isolated_app(None) as loaded:
        response = _client(loaded).get(
            f"/preview/{CHAT}",
            headers={"Authorization": f"Bearer not-{INTERNAL}"},
        )

    assert response.status_code == 401
    assert CHAT not in response.text
    assert INTERNAL not in response.text



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
        fetch_paths = [urlsplit(row["url"]).path for row in recorded["fetches"]]
        assert fetch_paths == [json_path, json_path], recorded
        assert recorded["sockets"] == [connect_ws, reconnect_ws], recorded
        for row in recorded["fetches"]:
            assert row["headers"].get("x-requested-with") == "ocu-workspace", recorded


_JS_CASES = r"""
import fs from 'node:fs';
import vm from 'node:vm';

const [sourceDir, moduleUrl, scenario] = process.argv.slice(2);
const context = vm.createContext({
  Headers,
  URL,
  Date,
  JSON,
  Promise,
  console,
  setTimeout(fn, ms) { if (!ms) fn(); return 0; },
  setInterval(fn) { context.__interval = fn; return 7; },
  clearInterval(id) { context.__cleared = id; },
  location: { protocol: 'https:', host: 'webui.example' },
  fetch: async (url, init = {}) => {
    const headers = {};
    new Headers(init.headers || {}).forEach((value, key) => { headers[key.toLowerCase()] = value; });
    context.__calls.push({ url: String(url), method: init.method || 'GET', headers, cache: init.cache || null, body: init.body || null });
    const handler = context.__respond;
    return handler(url, init, context.__calls.length);
  },
  __calls: [],
  __interval: null,
  __cleared: null,
  __respond: async () => ({ ok: true, status: 200, json: async () => ({}) }),
});

const linker = async (specifier, referencingModule) => {
  const resolved = new URL(specifier, referencingModule.identifier);
  const filename = resolved.pathname.split('/').pop();
  const fileSource = fs.readFileSync(sourceDir + filename, 'utf8');
  const child = new vm.SourceTextModule(fileSource, {
    context,
    identifier: resolved.href,
    initializeImportMeta(meta) { meta.url = resolved.href; },
  });
  await child.link(linker);
  await child.evaluate();
  return child;
};

const module = new vm.SourceTextModule(
  "export * from './ocu-request.js';",
  { context, identifier: moduleUrl, initializeImportMeta(meta) { meta.url = moduleUrl; } },
);
await module.link(linker);
await module.evaluate();
const api = module.namespace;

async function run() {
  if (scenario === 'wrapper') {
    await api.ocuFetch('/terminal/x/heartbeat', { headers: { Accept: 'application/json' }, header: { 'X-Extra': '1' } });
    await api.ocuFetch('/ocu/files/c/a.html', { serverUrl: true, cache: 'no-store' });
    await api.ocuFetch('/oculus/keep', {});
    await api.ocuFetch('relative.json', {});
    await api.ocuFetch('https://example.test/abs', {});
    await api.ocuFetch('//cdn.example/x', {});
    return { calls: context.__calls, ws: api.terminalWsUrl('chat-1') };
  }
  if (scenario === 'heartbeat') {
    const stop = api.startWorkspaceHeartbeat('chat-1', context);
    await context.__interval();
    stop();
    return { calls: context.__calls, cleared: context.__cleared };
  }
  if (scenario === 'badge') {
    context.__respond = async (url) => {
      if (String(url).includes('runtime/cli')) throw new Error('runtime-cli');
      return { ok: true, status: 200, json: async () => ({ cli_badge: { cli: 'claude', supports_cost: true } }) };
    };
    const ok = await api.loadCliBadge('/api/v1/ocu/workspaces/chat-1');
    context.__respond = async () => ({ ok: false, status: 500, json: async () => ({}) });
    const hidden = await api.loadCliBadge('/api/v1/ocu/workspaces/chat-1');
    return { ok, hidden, calls: context.__calls };
  }
  if (scenario === 'recover') {
    context.__respond = async (url) => {
      if (String(url).includes('resurrect')) throw new Error('resurrect');
      if (String(url).includes('restart-container')) return { ok: true, status: 200, json: async () => ({ state: 'running' }) };
      if (String(url).includes('start-ttyd')) return { ok: true, status: 200, json: async () => ({ already_running: false }) };
      return { ok: true, status: 200, json: async () => ({}) };
    };
    const ok = await api.recoverStoppedContainer('chat-1', false);
    context.__respond = async (url) => {
      if (String(url).includes('restart-container')) return { ok: false, status: 409, json: async () => ({}) };
      throw new Error('start after failed launch');
    };
    const failed = await api.recoverStoppedContainer('chat-1', false);
    return { ok, failed, calls: context.__calls };
  }
  if (scenario === 'listing') {
    const pages = {
      1: { files: [{ file_id: 'a', path: 'a.txt', revision: 7, type: 'text' }], revision: 7, next_cursor: '7:100', total: 101 },
      2: { files: [{ file_id: 'z', path: 'z.txt', revision: 7, type: 'text' }], revision: 7, next_cursor: null, total: 101 },
    };
    context.__respond = async (url) => {
      const parsed = new URL(url, 'https://webui.example');
      const cursor = parsed.searchParams.get('cursor');
      const body = cursor ? pages[2] : pages[1];
      return { ok: true, status: 200, json: async () => body };
    };
    const loaded = await api.loadOutputsWindow({
      apiUrl: '/ocu/api/outputs/chat',
      pageCount: 2,
      generation: 2,
      currentGeneration: () => 2,
    });
    const late = await api.loadOutputsWindow({
      apiUrl: '/ocu/api/outputs/chat',
      pageCount: 1,
      generation: 1,
      currentGeneration: () => 2,
    });
    const previous = { file_id: 'keep', path: 'old.docx', revision: 3, type: 'docx' };
    const renamed = [{ file_id: 'keep', path: 'new.docx', revision: 4, type: 'docx' }];
    const selected = api.applyListingSelection(renamed, previous, { path: 'noise.txt', revision: 9 }, true);
    const deleted = api.applyListingSelection([{ file_id: 'other', path: 'other.txt', revision: 1 }], previous, null, false);
    const same = api.applyListingSelection(
      [{ file_id: 'keep', path: 'same.txt', revision: 3, modified: 99 }],
      { file_id: 'keep', path: 'same.txt', revision: 3, modified: 1 },
      null,
      false,
    );
    return { loaded, late, selected, deleted, same, key: api.renderKey({ path: 'a.txt', revision: 7 }) };
  }
  if (scenario === 'stale-cursor') {
    let hits = 0;
    context.__respond = async (url) => {
      hits += 1;
      const parsed = new URL(url, 'https://webui.example');
      if (parsed.searchParams.get('cursor')) return { ok: false, status: 409, json: async () => ({}) };
      if (hits === 1) return { ok: true, status: 200, json: async () => ({ files: [{ path: 'a.txt', revision: 1 }], revision: 1, next_cursor: '1:100', total: 101 }) };
      return { ok: true, status: 200, json: async () => ({ files: [{ path: 'b.txt', revision: 2 }], revision: 2, next_cursor: null, total: 1 }) };
    };
    const loaded = await api.loadOutputsWindow({
      apiUrl: '/ocu/api/outputs/chat',
      pageCount: 2,
      generation: 1,
      currentGeneration: () => 1,
    });
    return { loaded, hits };
  }
  throw new Error('unknown scenario');
}

process.stdout.write(JSON.stringify(await run()));
"""


def _run_js_scenario(tmp_path, scenario, module_url="https://webui.example/ocu/static/ocu-request.js"):
    if not NODE:
        pytest.fail("node is required for SPA request tests; install Node 22 or set OCU_TEST_NODE")
    harness = tmp_path / "ocu_request_harness.mjs"
    harness.write_text(_JS_CASES)
    completed = subprocess.run(
        [
            NODE,
            "--experimental-vm-modules",
            str(harness),
            str(SERVER_DIR / "static") + "/",
            module_url,
            scenario,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_static_js_has_no_root_absolute_static_literal():
    for path in (SERVER_DIR / "static").rglob("*.js"):
        text = path.read_text(encoding="utf-8")
        assert "/static/" not in text, path


def test_wrapper_prefixes_client_paths_once_and_keeps_server_urls(tmp_path):
    recorded = _run_js_scenario(tmp_path, "wrapper")
    calls = recorded["calls"]
    assert calls[0]["url"] == "/ocu/terminal/x/heartbeat"
    assert calls[0]["headers"]["x-requested-with"] == "ocu-workspace"
    assert calls[0]["headers"]["accept"] == "application/json"
    assert calls[0]["headers"]["x-extra"] == "1"
    assert calls[1]["url"] == "/ocu/files/c/a.html"
    assert "/ocu/ocu/" not in calls[1]["url"]
    assert calls[2]["url"] == "/ocu/oculus/keep"
    assert calls[3]["url"] == "relative.json"
    assert calls[4]["url"] == "https://example.test/abs"
    assert calls[5]["url"] == "//cdn.example/x"
    assert recorded["ws"] == "wss://webui.example/ocu/terminal/chat-1/ws"


def test_heartbeat_uses_wrapper_and_cleans_up(tmp_path):
    recorded = _run_js_scenario(tmp_path, "heartbeat")
    assert recorded["calls"][0]["url"] == "/ocu/terminal/chat-1/heartbeat"
    assert recorded["calls"][0]["headers"]["x-requested-with"] == "ocu-workspace"
    assert recorded["cleared"] == 7


def test_badge_uses_describe_url_and_hides_on_failure(tmp_path):
    recorded = _run_js_scenario(tmp_path, "badge")
    assert recorded["ok"]["cli"] == "claude"
    assert recorded["hidden"] is None
    assert recorded["calls"][0]["url"] == "/api/v1/ocu/workspaces/chat-1"
    assert not any("runtime/cli" in row["url"] for row in recorded["calls"])


def test_both_stopped_branches_use_restart_alias_and_skip_ttyd_on_failure(tmp_path):
    recorded = _run_js_scenario(tmp_path, "recover")
    urls = [row["url"] for row in recorded["calls"]]
    assert urls.count("/ocu/terminal/chat-1/restart-container") == 2
    assert "/ocu/terminal/chat-1/resurrect-container" not in urls
    assert recorded["ok"]["ok"] is True
    assert recorded["failed"]["ok"] is False
    assert urls.count("/ocu/terminal/chat-1/start-ttyd") == 1


def test_listing_keeps_later_pages_and_rejects_stale_generation(tmp_path):
    recorded = _run_js_scenario(tmp_path, "listing")
    assert [row["path"] for row in recorded["loaded"]["files"]] == ["a.txt", "z.txt"]
    assert recorded["late"]["stale"] is True
    assert recorded["selected"]["path"] == "new.docx"
    assert recorded["deleted"]["path"] == "other.txt"
    assert recorded["same"]["modified"] == 1
    assert recorded["key"] == "a.txt\x007"


def test_stale_cursor_restarts_once(tmp_path):
    recorded = _run_js_scenario(tmp_path, "stale-cursor")
    assert recorded["loaded"]["files"][0]["path"] == "b.txt"
    assert recorded["hits"] == 3


_XLSX_HARNESS = r"""
import fs from 'node:fs';
import vm from 'node:vm';

const [xlsxPath, requestPath, workbookPath, moduleUrl] = process.argv.slice(2);
const bytes = fs.readFileSync(workbookPath);
const context = vm.createContext({
  console,
  Uint8Array,
  ArrayBuffer,
  DataView,
  Buffer,
  process,
  setTimeout,
  clearTimeout,
});
vm.runInContext(fs.readFileSync(xlsxPath, 'utf8'), context, { filename: xlsxPath });
const linker = async (specifier, referencingModule) => {
  const resolved = new URL(specifier, referencingModule.identifier);
  const child = new vm.SourceTextModule(fs.readFileSync(requestPath, 'utf8'), {
    context,
    identifier: resolved.href,
    initializeImportMeta(meta) { meta.url = resolved.href; },
  });
  await child.link(async () => { throw new Error('unexpected import'); });
  await child.evaluate();
  return child;
};
const module = new vm.SourceTextModule(
  "export * from './ocu-request.js';",
  { context, identifier: moduleUrl, initializeImportMeta(meta) { meta.url = moduleUrl; } },
);
await module.link(linker);
await module.evaluate();
const workbook = context.XLSX.read(bytes, { type: 'buffer', cellFormula: true, sheetStubs: true, raw: false });
const sheet = workbook.Sheets[workbook.SheetNames[0]];
const api = module.namespace;
process.stdout.write(JSON.stringify({
  names: workbook.SheetNames,
  keys: Object.keys(sheet),
  a1: sheet.A1 || null,
  a2: sheet.A2 || null,
  a3: sheet.A3 || null,
  a4: sheet.A4 || null,
  a1d: { f: sheet.A1 && sheet.A1.f, v: sheet.A1 && sheet.A1.v, cached: api.formulaHasCachedValue(sheet.A1), display: api.formulaCellDisplay(sheet.A1) },
  a2d: { f: sheet.A2 && sheet.A2.f, v: sheet.A2 && sheet.A2.v, cached: api.formulaHasCachedValue(sheet.A2), display: api.formulaCellDisplay(sheet.A2) },
  a3d: { f: sheet.A3 && sheet.A3.f, v: sheet.A3 && sheet.A3.v, cached: api.formulaHasCachedValue(sheet.A3), display: api.formulaCellDisplay(sheet.A3) },
  a4d: { f: sheet.A4 && sheet.A4.f, v: sheet.A4 && sheet.A4.v, cached: api.formulaHasCachedValue(sheet.A4), display: api.formulaCellDisplay(sheet.A4) },
}));
"""


def _xlsx_bytes():
    import zipfile
    from io import BytesIO

    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    sheet = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="{ns}">
  <sheetData>
    <row r="1"><c r="A1" t="n"><f>1+1</f><v>0</v></c></row>
    <row r="2"><c r="A2" t="b"><f>FALSE()</f><v>0</v></c></row>
    <row r="3"><c r="A3"><f>A1+A2</f></c></row>
    <row r="4"><c r="A4" t="n"><v>9</v></c></row>
  </sheetData>
</worksheet>'''
    workbook = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Cached" sheetId="1" r:id="rId1"/><sheet name="Formulas" sheetId="2" r:id="rId2"/></sheets>
</workbook>'''
    rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
</Relationships>'''
    content = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>'''
    root_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>'''
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", content)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", rels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
        zf.writestr("xl/worksheets/sheet2.xml", sheet.replace("Cached", "Formulas"))
    return buf.getvalue()


def test_xlsx_formula_cache_distinguishes_zero_false_and_absent(tmp_path):
    if not NODE:
        pytest.fail("node is required for SheetJS formula tests; install Node 22 or set OCU_TEST_NODE")
    workbook = tmp_path / "formulas.xlsx"
    workbook.write_bytes(_xlsx_bytes())
    harness = tmp_path / "xlsx_harness.mjs"
    harness.write_text(_XLSX_HARNESS)
    completed = subprocess.run(
        [
            NODE,
            "--experimental-vm-modules",
            str(harness),
            str(SERVER_DIR / "static" / "xlsx.full.min.js"),
            str(SERVER_DIR / "static" / "ocu-request.js"),
            str(workbook),
            "https://webui.example/ocu/static/ocu-request.js",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    recorded = json.loads(completed.stdout)
    assert "A3" in recorded["keys"]
    assert recorded["a1d"]["cached"] is True
    assert recorded["a1d"]["display"] == "0"
    assert recorded["a2d"]["cached"] is True
    assert recorded["a2d"]["display"] == "FALSE"
    assert recorded["a3d"]["cached"] is False
    assert recorded["a3d"]["display"] == "uncomputed"
    assert recorded["a4d"]["cached"] is True
    assert recorded["a4d"]["display"] == "9"


def _mutant_copy(tmp_path, name, replacements):
    source = (SERVER_DIR / "static" / "ocu-request.js").read_text(encoding="utf-8")
    for old, new in replacements:
        assert old in source, old
        source = source.replace(old, new, 1)
    dest = tmp_path / name
    dest.mkdir()
    dest.joinpath("ocu-request.js").write_text(source, encoding="utf-8")
    return dest


def _run_js_from_dir(tmp_path, source_dir, scenario, module_url="https://webui.example/ocu/static/ocu-request.js"):
    if not NODE:
        pytest.fail("node is required for SPA request tests; install Node 22 or set OCU_TEST_NODE")
    harness = tmp_path / f"{scenario}_harness.mjs"
    harness.write_text(_JS_CASES)
    completed = subprocess.run(
        [
            NODE,
            "--experimental-vm-modules",
            str(harness),
            str(source_dir) + "/",
            module_url,
            scenario,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    return completed


def test_listing_generation_rejection_fails_on_mtime_mutant(tmp_path):
    mutant = _mutant_copy(
        tmp_path,
        "mtime-key",
        [("String(file.path) + '\\0' + String(file.revision);", "String(file.path) + '\\0' + String(file.modified);")],
    )
    completed = _run_js_from_dir(tmp_path, mutant, "listing")
    assert completed.returncode == 0, completed.stderr
    recorded = json.loads(completed.stdout)
    assert recorded["key"] != "a.txt\x007"


def test_stale_cursor_no_retry_mutant_keeps_error(tmp_path):
    mutant = _mutant_copy(
        tmp_path,
        "no-stale-retry",
        [("if (resp.status === 409 && cursor && staleRetries === 0) {", "if (false && resp.status === 409 && cursor && staleRetries === 0) {")],
    )
    completed = _run_js_from_dir(tmp_path, mutant, "stale-cursor")
    assert completed.returncode == 0, completed.stderr
    recorded = json.loads(completed.stdout)
    assert recorded["loaded"].get("error") == "stale-cursor"
    assert recorded["hits"] == 2


def test_formula_zero_cache_mutant_treats_zero_as_uncomputed(tmp_path):
    mutant_src = (SERVER_DIR / "static" / "ocu-request.js").read_text(encoding="utf-8")
    old = "if (cell.t === 'z') return false;\n  return Object.prototype.hasOwnProperty.call(cell, 'v') && cell.v !== undefined;"
    new = "if (!cell.v) return false;\n  return true;"
    assert old in mutant_src
    dest = tmp_path / "formula-mutant"
    dest.mkdir()
    dest.joinpath("ocu-request.js").write_text(mutant_src.replace(old, new, 1), encoding="utf-8")
    workbook = tmp_path / "formulas.xlsx"
    workbook.write_bytes(_xlsx_bytes())
    harness = tmp_path / "xlsx_mutant.mjs"
    harness.write_text(_XLSX_HARNESS)
    completed = subprocess.run(
        [
            NODE,
            "--experimental-vm-modules",
            str(harness),
            str(SERVER_DIR / "static" / "xlsx.full.min.js"),
            str(dest / "ocu-request.js"),
            str(workbook),
            "https://webui.example/ocu/static/ocu-request.js",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    recorded = json.loads(completed.stdout)
    assert recorded["a1d"]["cached"] is False
    assert recorded["a1d"]["display"] == "uncomputed"
