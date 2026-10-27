// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
// Run from repository root with OCU_PREVIEW_PYTHON pointing to Python 3.12
// with the server requirements and pytest; requires Playwright 1.62.1 + Chromium.
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const { spawnSync } = require('node:child_process');
let playwright;
try { playwright = require('playwright'); }
catch { playwright = require('@playwright/test'); }

const ROOT = path.resolve(__dirname, '../..');
const STATIC = path.join(ROOT, 'computer-use-server/static');
const CHAT = 'a1b2c3d4-e5f6-7890-abcd-ef1234567890';
let previewCaptures;
let secretCanaries = [];
const officeArrivals = { allowed: [], denied: [] };
const officeStandins = Object.keys(officeArrivals).map(name => http.createServer((req, res) => {
  officeArrivals[name].push({ path: req.url, authorization: req.headers.authorization });
  if (req.url === '/office-script.js')
    return respond(res, 200, 'window.__officeScriptCanary = "executed";', 'text/javascript');
  if (req.url === '/office-frame')
    return respond(res, 200, '<!doctype html><p id="office-frame-canary">Office frame rendered</p>', 'text/html');
  return respond(res, 200, '{}');
}));

async function captureProductionPreviews(docserverOrigin) {
  const python = process.env.OCU_PREVIEW_PYTHON ||
    (process.env.VIRTUAL_ENV ? path.join(process.env.VIRTUAL_ENV, 'bin/python') : null);
  if (!python || !path.isAbsolute(python))
    throw new Error('OCU_PREVIEW_PYTHON must name Python 3.12 with server requirements and pytest');
  try { await fs.access(python); }
  catch { throw new Error(`OCU_PREVIEW_PYTHON is not an available executable: ${python}`); }
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'ocu-preview-capture-'));
  const output = path.join(directory, 'responses.json');
  try {
    const result = spawnSync(python,
      [path.join(__dirname, '_preview_capture.py'), CHAT, output, docserverOrigin],
      {
        cwd: ROOT,
        env: {
          PATH: process.env.PATH || '',
          HOME: process.env.HOME || '',
          TMPDIR: process.env.TMPDIR || os.tmpdir(),
          PYTHONUNBUFFERED: '1',
        },
        timeout: 180000,
        maxBuffer: 1024 * 1024,
      });
    if (result.error || result.status !== 0)
      throw new Error(`production preview capture failed (${result.error?.code || result.status}); ` +
        'OCU_PREVIEW_PYTHON must provide Python 3.12, server requirements and pytest');
    const captured = JSON.parse(await fs.readFile(output, 'utf8'));
    const captures = captured.responses;
    secretCanaries = captured.secretCanaries;
    assert(Array.isArray(secretCanaries) && secretCanaries.length === 6);
    if (!Array.isArray(captures) || captures.length !== 42)
      throw new Error('production preview capture omitted required prefix/mode responses');
    const byRoute = new Map();
    for (const response of captures) {
      const key = response.prefix + response.query;
      if (byRoute.has(key) || response.status !== 200 || typeof response.body !== 'string' ||
          !response.headers?.['content-type'])
        throw new Error(`invalid production preview capture: ${key}`);
      byRoute.set(key, response);
    }
    for (const prefix of ['', '/ocu', '/tools/ocu']) {
      for (const query of ['', '?embed=files', '?embed=files&embed=files', '?embed=browser',
        '?embed=browser&embed=browser', '?embed=terminal', '?embed=unknown', '?embed=office',
        '?embed=office&embed=office', '?embed=office&embed=files',
        '?embed=office&office_fixture=absent', '?embed=office&office_fixture=blank',
        '?embed=terminal&model=m-1', '?model=m-1'])
        assert(byRoute.has(prefix + query), `missing production preview capture: ${prefix}${query}`);
    }
    return byRoute;
  } finally {
    await fs.rm(directory, { recursive: true, force: true });
  }
}
const OFFICE_MIME = {
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
};
const files = [
  { file_id: 'doc', path: 'valid.docx', type: 'docx', mime: OFFICE_MIME.docx },
  { file_id: 'slow', path: 'slow.docx', type: 'docx', mime: OFFICE_MIME.docx },
  { file_id: 'sheet', path: 'valid.xlsx', type: 'xlsx', mime: OFFICE_MIME.xlsx },
  { file_id: 'broken', path: 'broken.xlsx', type: 'xlsx', mime: OFFICE_MIME.xlsx },
  { file_id: 'deck', path: 'broken.pptx', type: 'pptx', mime: OFFICE_MIME.pptx },
  { file_id: 'deck-valid', path: 'valid.pptx', type: 'pptx', mime: OFFICE_MIME.pptx },
  { file_id: 'biff', path: 'valid.xls', type: 'xlsx', mime: OFFICE_MIME.xlsx },
  { file_id: 'raw2', path: 'raw2.xls', type: 'xlsx', mime: OFFICE_MIME.xlsx },
  { file_id: 'raw3', path: 'raw3.xls', type: 'xlsx', mime: OFFICE_MIME.xlsx },
  { file_id: 'raw4', path: 'raw4.xls', type: 'xlsx', mime: OFFICE_MIME.xlsx },
  { file_id: 'raw-truncated', path: 'truncated.xls', type: 'xlsx', mime: OFFICE_MIME.xlsx },
  { file_id: 'raw-no-eof', path: 'missing-eof.xls', type: 'xlsx', mime: OFFICE_MIME.xlsx },
  { file_id: 'raw-payload', path: 'truncated-payload.xls', type: 'xlsx', mime: OFFICE_MIME.xlsx },
  { file_id: 'raw-historical', path: 'historical.xls', type: 'xlsx', mime: OFFICE_MIME.xlsx },
  { file_id: 'raw-short-header', path: 'short-header.xls', type: 'xlsx', mime: OFFICE_MIME.xlsx },
  { file_id: 'raw-text', path: 'not-a-workbook.xls', type: 'xlsx', mime: OFFICE_MIME.xlsx },
  { file_id: 'punct', path: "report (1)!'.docx", type: 'docx', mime: OFFICE_MIME.docx,
    url: `/ocu/files/${CHAT}/report%20%281%29%21%27.docx` },
  { file_id: 'hostile', path: 'hostile.docx', type: 'docx', mime: OFFICE_MIME.docx },
  { file_id: 'corrupt-doc', path: 'corrupt.docx', type: 'docx', mime: OFFICE_MIME.docx },
  { file_id: 'slow-sheet', path: 'slow.xlsx', type: 'xlsx', mime: OFFICE_MIME.xlsx },
  { file_id: 'hostile-sheet', path: 'hostile.xlsx', type: 'xlsx', mime: OFFICE_MIME.xlsx },
  { file_id: 'svg', path: 'vector.svg', type: 'image', mime: 'image/svg+xml' },
  { file_id: 'xhtml', path: 'page.xhtml', type: 'other', mime: 'application/xhtml+xml' },
  { file_id: 'xml', path: 'data.xml', type: 'code', mime: 'application/xml' },
  { file_id: 'drawio', path: 'diagram.drawio', type: 'drawio', mime: 'application/xml' },
  { file_id: 'drawio-empty', path: 'empty.drawio', type: 'drawio', mime: 'application/xml' },
  { file_id: 'drawio-lazy', path: 'lazy.drawio', type: 'drawio', mime: 'application/xml' },
  { file_id: 'drawio-broken', path: 'broken.drawio', type: 'drawio', mime: 'application/xml' },
  { file_id: 'drawio-compressed', path: 'compressed.drawio', type: 'drawio', mime: 'application/xml' },
  { file_id: 'drawio-image', path: 'image.drawio', type: 'drawio', mime: 'application/xml' },
  { file_id: 'drawio-math', path: 'math.drawio', type: 'drawio', mime: 'application/xml' },
  { file_id: 'drawio-missing', path: 'missing.drawio', type: 'drawio', mime: 'application/xml' },
  { file_id: 'drawio-bpmn', path: 'bpmn.drawio', type: 'drawio', mime: 'application/xml' },
  { file_id: 'drawio-er', path: 'er.drawio', type: 'drawio', mime: 'application/xml' },
  { file_id: 'drawio-pages', path: 'pages.drawio', type: 'drawio', mime: 'application/xml' },
  { file_id: 'drawio-pages-missing', path: 'pages-missing.drawio', type: 'drawio', mime: 'application/xml' },
  { file_id: 'drawio-corrupt-lazy', path: 'corrupt-lazy.drawio', type: 'drawio', mime: 'application/xml' },
  { file_id: 'drawio-pages-image', path: 'pages-image.drawio', type: 'drawio', mime: 'application/xml' },
  { file_id: 'html-doc', path: 'spoof.docx', type: 'docx', mime: 'text/html; charset=utf-8' },
  { file_id: 'xml-doc', path: 'text-xml.docx', type: 'docx', mime: 'text/xml' },
  { file_id: 'plusxml', path: 'suffix.docx', type: 'docx', mime: 'application/vnd.example+xml' },
  { file_id: 'foreign', path: 'foreign.docx', type: 'docx', mime: OFFICE_MIME.docx, url: 'https://example.invalid/foreign.docx' },
  { file_id: 'cross-chat', path: 'cross-chat.docx', type: 'docx', mime: OFFICE_MIME.docx, url: '/ocu/files/another-chat/cross-chat.docx' },
  { file_id: 'hostile-markdown', path: 'hostile.md', type: 'markdown', mime: 'text/markdown' },
];
const requests = [];
let slowRequested;
const slowArrival = new Promise(resolve => { slowRequested = resolve; });
const held = [];
let sheetRequested;
const sheetArrival = new Promise(resolve => { sheetRequested = resolve; });
const heldSheets = [];
const heldListings = [];
const heldBrowserStatuses = [];
const heldBrowserPages = [];
const heldUpgrades = [];
const activeSockets = new Set();
const socketMessages = [];
const upgradeAttempts = [];
let browserFixture = 'inactive';
let terminalFixture = 'inactive';
const GATEWAY_DETAIL = 'Claude Code gateway: the chat has no usable model id (1-256 printable ASCII ' +
  'characters without spaces); select a model and reopen the terminal';
const startBody = row => JSON.parse(row.body);
const FAULT_DETAIL = '9222/tcp publication on 172.31.0.1 was accepted by the engine but never assigned a host port; gateway publication is unsupported on this engine profile (computer-use-server/README.md, Sandbox network host support)';
let browserPagesHeld = false;
let upgradesHeld = false;
let ttydSendsData = true;
const within = (promise, name) => Promise.race([
  promise,
  new Promise((_, reject) => setTimeout(() => reject(new Error(`${name} timed out`)), 10000).unref()),
]);
const waitUntil = async (condition, label) => {
  const deadline = Date.now() + 10000;
  while (!condition()) {
    if (Date.now() >= deadline) throw new Error(`${label} timed out`);
    await new Promise(resolve => setTimeout(resolve, 25));
  }
};
let listMode = 'normal';
let standaloneUploadedNote = false;
let standaloneFirstFile = null;
let standaloneListing = null;
let injectedListingFailure = false;
const uploadFixtures = new Map();
const retainedUploads = new Map();
const uploadDiscoveryFixtures = new Map();
const discoverySurface = process.env.OCU_PREVIEW_DISCOVERY_SURFACE || 'both';
assert(['both', 'embedded', 'standalone'].includes(discoverySurface),
  'OCU_PREVIEW_DISCOVERY_SURFACE must be both, embedded or standalone');
const uploadBodyCanary = '<img id="upload-body-canary" src=x onerror="window.__uploadCanary=1">';
const uploadFailureOrder = process.env.OCU_PREVIEW_UPLOAD_FAILURE_ORDER || 'http-first';
assert(['http-first', 'network-first'].includes(uploadFailureOrder),
  'OCU_PREVIEW_UPLOAD_FAILURE_ORDER must be http-first or network-first');

let drawioMissingViewer = false;
let drawioCorruptViewer = false;
let drawioMissingLazy = false;
let drawioCorruptLazy = false;
let drawioMissingImage = false;
let drawioCorruptImage = false;
let drawioHeldImage = false;
let drawioHeldDocument = false;
let drawioHeldViewer = false;
const heldDrawio = [];
const heldViewer = [];
const heldImages = [];
const htmlFixtures = new Map();
const heldHtml = [];
const bytes = {};
const contentTypes = {
  '.js': 'text/javascript',
  '.mjs': 'text/javascript',
  '.css': 'text/css',
  '.woff2': 'font/woff2',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.gif': 'image/gif',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.xml': 'application/xml',
  '.json': 'application/json',
  '.txt': 'text/plain'
};
const respond = (res, status, body, type = 'application/json', headers = {}) => {
  if (res.destroyed) return;
  res.writeHead(status, { 'Content-Type': type, 'Cache-Control': 'no-store', ...headers });
  res.end(body);
};
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  const prefix = url.pathname.startsWith('/tools/ocu/') ? '/tools/ocu'
    : url.pathname.startsWith('/ocu/') || url.pathname === '/ocu' ? '/ocu' : '';
  const record = { path: url.pathname, method: req.method, cursor: url.searchParams.get('cursor'), header: req.headers['x-requested-with'], status: 0 };
  requests.push(record);
  try {
    if ((prefix && url.pathname.startsWith(`${prefix}/static/`)) || url.pathname.startsWith('/static/')) {
      const staticRoot = prefix ? `${prefix}/static/` : '/static/';
      const name = decodeURIComponent(url.pathname.slice(staticRoot.length));
      const filename = path.resolve(STATIC, name);
      if (!filename.startsWith(STATIC + path.sep)) return respond(res, 404, 'Not found', 'text/plain');
      const rel = name.replaceAll('\\', '/');
      if (drawioMissingViewer && rel === 'drawio/js/viewer-static.min.js') {
        record.status = 404;
        return respond(res, 404, 'missing viewer', 'text/plain');
      }
      if (drawioCorruptViewer && rel === 'drawio/js/viewer-static.min.js') {
        record.status = 200;
        return respond(res, 200, 'window.GraphViewer = 1;', 'text/javascript');
      }
      if (drawioMissingLazy && rel.startsWith('drawio/stencils/')) {
        record.status = 404;
        return respond(res, 404, 'missing stencil', 'text/plain');
      }
      if (drawioCorruptLazy && rel.includes('/stencils/electrical/logic_gates.xml')) {
        record.status = 200;
        return respond(res, 200, '<not-a-stencil/>', 'application/xml');
      }
      if (drawioHeldViewer && rel === 'drawio/js/viewer-static.min.js') {
        heldViewer.push({ res, record });
        return;
      }
      if (rel === 'drawio/img/telecommunication/Cellphone_128x128.png') {
        if (drawioHeldImage) {
          heldImages.push({ res, record });
          return;
        }
        if (drawioMissingImage) {
          record.status = 404;
          return respond(res, 404, 'missing image', 'text/plain');
        }
        if (drawioCorruptImage) {
          record.status = 200;
          return respond(res, 200, 'invalid PNG', 'image/png');
        }
      }
      record.status = 200;
      const body = await fs.readFile(filename);
      if (contentTypes[path.extname(filename)] === 'text/javascript') {
        for (const secret of secretCanaries)
          assert(!body.includes(secret), `served script exposed synthetic signing material: ${rel}`);
      }
      return respond(res, 200, body, contentTypes[path.extname(filename)] || 'application/octet-stream');
    }
    if (url.pathname === `${prefix}/preview/${CHAT}`) {
      const captured = previewCaptures?.get(prefix + url.search);
      if (!captured) throw new Error(`uncaptured production preview response: ${prefix}${url.search}`);
      res.writeHead(captured.status, captured.headers);
      res.end(captured.body);
      return;
    }
    if (url.pathname === `${prefix}/api/outputs/${CHAT}`) {
      if (listMode === 'failure') {
        injectedListingFailure = true;
        return respond(res, 503, '{}');
      }
      if (listMode === 'timeout') { heldListings.push(res); return; }
      if (listMode === 'standalone') {
        let listed = files.filter(f => ['hostile', 'drawio', 'drawio-empty', 'drawio-lazy', 'drawio-broken',
          'drawio-compressed', 'drawio-image', 'drawio-math', 'drawio-missing',
          'drawio-bpmn', 'drawio-er', 'drawio-pages', 'drawio-pages-missing', 'drawio-corrupt-lazy',
          'drawio-pages-image', 'hostile-markdown'].includes(f.file_id));
        listed.push(
          { file_id: 'safe-markdown', path: 'guides/safe.md', type: 'markdown', mime: 'text/markdown' },
          { file_id: 'compatibility-markdown', path: 'guides/compatibility.md', type: 'markdown', mime: 'text/markdown' },
          { file_id: 'matrix-markdown', path: 'matrix.md', type: 'markdown', mime: 'text/markdown' });
        if (standaloneFirstFile) {
          const index = listed.findIndex(file => file.path === standaloneFirstFile);
          assert(index >= 0, 'unknown first standalone fixture');
          listed.unshift(...listed.splice(index, 1));
        }
        if (standaloneUploadedNote) {
          listed.push({ file_id: 'note', path: 'note.txt', type: 'text', mime: 'text/plain' });
        }
        if (standaloneListing) listed = standaloneListing;
        return respond(res, 200, JSON.stringify({
          chat_id: CHAT, files: listed.map(f => ({
            name: f.path, size: 128, revision: 1,
            url: `${prefix || ''}/files/${CHAT}/${encodeURIComponent(f.path)}`, ...f
          })), total: listed.length, revision: 1, next_cursor: null,
        }));
      }
      const entries = files.filter(f => listMode !== 'deleted' || f.file_id !== 'doc');
      const all = [...Array.from({ length: 100 }, (_, i) => ({
        file_id: `filler-${i}`, path: `filler-${i}.txt`, type: 'text', mime: 'text/plain',
      })), ...entries];
      const next = url.searchParams.has('cursor');
      const page = next ? all.slice(100) : all.slice(0, 100);
      return respond(res, 200, JSON.stringify({ chat_id: CHAT, revision: 1, total: all.length,
        next_cursor: next ? (listMode === 'incomplete' ? '1:999' : null) : '1:100',
        files: page.map(f => ({ name: f.path, size: 128, revision: 1,
          url: `/ocu/files/${CHAT}/${encodeURIComponent(f.path)}`, ...f })) }));
    }

    if (url.pathname === '/raw-root-chart.png') {
      record.status = 200;
      return respond(res, 200, bytes['guides/chart.png'], 'image/png');
    }
    const htmlFixture = htmlFixtures.get(url.pathname);
    if (htmlFixture) {
      if (htmlFixture.redirect) {
        record.status = 302;
        return respond(res, 302, '', 'text/plain', { Location: htmlFixture.redirect });
      }
      if (htmlFixture.hold) {
        heldHtml.push({ res, record, fixture: htmlFixture });
        return;
      }
      record.status = 200;
      return respond(res, 200, bytes[htmlFixture.key], htmlFixture.type,
        { 'Access-Control-Allow-Origin': 'null' });
    }
    if ((prefix && url.pathname.startsWith(`${prefix}/files/${CHAT}/`)) ||
        (!prefix && url.pathname.startsWith(`/files/${CHAT}/`))) {
      const filesRoot = `${prefix || ''}/files/${CHAT}/`;
      if (url.pathname.endsWith('/slow.docx')) { held.push(res); slowRequested(); return; }
      if (url.pathname.endsWith('/slow.xlsx')) { heldSheets.push(res); sheetRequested(); return; }
      if (drawioHeldDocument && url.pathname.endsWith('/diagram.drawio')) { heldDrawio.push(res); return; }
      const name = decodeURIComponent(url.pathname.slice(filesRoot.length)).replace(/\/{2,}/g, '/');
      if (!(name in bytes)) {
        requests[requests.length - 1].status = 404;
        return respond(res, 404, 'Not found', 'text/plain');
      }
      requests[requests.length - 1].status = 200;
      return respond(res, 200, bytes[name], 'application/octet-stream');
    }
    if (url.pathname === `${prefix}/browser/${CHAT}/status`) {
      if (browserFixture === 'held') { heldBrowserStatuses.push(res); return; }
      if (browserFixture === 'failure') return respond(res, 503, '{}');
      if (browserFixture === 'malformed') return respond(res, 200, '{"active":"yes"}');
      if (browserFixture === 'fault') return respond(res, 200, JSON.stringify({
        active: false, pages: [], reason: 'gateway_publication_unassigned', detail: FAULT_DETAIL
      }));
      return respond(res, 200, JSON.stringify({
        active: browserFixture === 'active', pages: browserFixture === 'active'
          ? [{ id: 'fixture-page', type: 'page', url: 'about:blank' }] : []
      }));
    }
    if (url.pathname === `${prefix}/browser/${CHAT}/json`) {
      if (browserPagesHeld) { heldBrowserPages.push(res); return; }
      return respond(res, 200, JSON.stringify([{ id: 'fixture-page', type: 'page', url: 'about:blank' }]));
    }
    if (url.pathname === `${prefix}/terminal/${CHAT}/heartbeat`)
      return respond(res, 200, '{"ok":true}');
    if (url.pathname === `${prefix}/terminal/${CHAT}/start-ttyd` && req.method === 'POST') {
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      record.body = Buffer.concat(chunks).toString('utf8');
      if (terminalFixture === 'refused' || terminalFixture === 'refused-nonstring') {
        record.status = 422;
        return respond(res, 422, JSON.stringify({ detail: terminalFixture === 'refused'
          ? GATEWAY_DETAIL : [{ loc: ['body', 'model'], msg: GATEWAY_DETAIL }] }));
      }
      if (terminalFixture === 'stopped') {
        record.status = 503;
        return respond(res, 503, '{}');
      }
      return respond(res, 200, '{"already_running":true}');
    }
    if (url.pathname === `${prefix}/terminal/${CHAT}/restart-container` && req.method === 'POST') {
      record.status = 200;
      return respond(res, 200, '{}');
    }
    if (url.pathname === `${prefix}/terminal/${CHAT}/processes`) return respond(res, 200, JSON.stringify({ processes: [] }));
    if (url.pathname === `${prefix}/terminal/${CHAT}/status`) {
      if (terminalFixture === 'status-error') {
        record.status = 503;
        return respond(res, 503, '{}');
      }
      if (terminalFixture === 'fault') return respond(res, 200, JSON.stringify({
        active: false, reason: 'gateway_publication_unassigned', detail: FAULT_DETAIL
      }));
      if (terminalFixture === 'stopped')
        return respond(res, 200, JSON.stringify({ active: false, container_stopped: true }));
      return respond(res, 200, JSON.stringify({ active: false }));
    }
    if (url.pathname === `${prefix}/terminal/${CHAT}/sessions`) return respond(res, 200, JSON.stringify({ sessions: [] }));
    if (url.pathname.startsWith(`${prefix}/api/uploads/${CHAT}/`) && req.method === 'POST' &&
        uploadFixtures.has(url.pathname)) {
      const status = uploadFixtures.get(url.pathname);
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      record.status = status;
      if (status === 200) retainedUploads.set(url.pathname, Buffer.concat(chunks));
      const discoveryFixture = uploadDiscoveryFixtures.get(url.pathname);
      const filename = discoveryFixture?.filename || decodeURIComponent(url.pathname.split('/').pop());
      const body = status === 200 ? JSON.stringify({
        status: 'success', filename, size: 5, md5: '0',
        ...(discoveryFixture
          ? { discovery: { status: 'failed', reason: discoveryFixture.reason } }
          : {}),
      }) : uploadBodyCanary;
      return respond(res, status, body, status === 200 ? 'application/json' : 'text/plain');
    }
    if (url.pathname === `${prefix}/api/uploads/${CHAT}/note.txt` && req.method === 'POST') {
      record.status = 200;
      standaloneUploadedNote = true;
      return respond(res, 200, JSON.stringify({ status: 'success', filename: 'note.txt', size: 5, md5: '0' }));
    }

    if (url.pathname === `/api/v1/ocu/workspaces/${CHAT}`) return respond(res, 200, JSON.stringify({ cli_badge: null }));
    if (url.pathname === '/favicon.ico') return respond(res, 204, '', 'text/plain');
    if (url.pathname === '/parent') return respond(res, 200, `<!doctype html><html><body>
      <script src="/ocu/static/jszip.min.js"></script><script src="/ocu/static/xlsx.full.min.js"></script>
      <script src="/ocu/static/mammoth.browser.min.js"></script>
      <script>
        window.states=[];
        window.frameMessages=[];
        window.expectedSelections={};
        window.beacons={};
        window.retiredFrames=new Set();
        window.postRetirement=[];
        window.addEventListener('message', e => {
          if(e.data?.type==='sibling-done' || e.data?.type==='opaque-done') {
            window.beacons[e.data.type]={origin:e.origin, source:e.source};
          }
          const current=document.querySelector('#preview')?.contentWindow;
          if(e.source===current) window.frameMessages.push(e.data);
          if(e.source===current && e.origin===location.origin && e.data
              && typeof e.data==='object' && typeof e.data.type==='string'
              && e.data.type.startsWith('ocu:preview-')) window.states.push(e.data);
          if(e.origin===location.origin && window.retiredFrames.has(e.source) && e.data
              && typeof e.data==='object' && typeof e.data.type==='string'
              && e.data.type.startsWith('ocu:preview-')) window.postRetirement.push(e.data);
        });
        window.select=(file_id,generation,extra={})=>{
          const request={type:'ocu:preview-select',chat_id:'${CHAT}',file_id,generation,...extra};
          if(typeof generation==='number' && Number.isSafeInteger(generation)
              && generation>=0 && request.chat_id==='${CHAT}' && !('url' in request)
              && file_id && !(generation in window.expectedSelections))
            window.expectedSelections[generation]=file_id;
          document.querySelector('#preview').contentWindow.postMessage(request,location.origin);
        };
        window.mount=(query='?embed=files',prefix='/ocu')=>{const frame=document.createElement('iframe');frame.id='preview';
          frame.setAttribute('sandbox','allow-scripts allow-same-origin allow-forms');
          frame.src=prefix+'/preview/${CHAT}'+query;document.body.appendChild(frame);};
        window.retire=()=>{const frame=document.querySelector('#preview');
          window.retiredFrames.add(frame.contentWindow);frame.remove();};
      </script></body></html>`, 'text/html');
    return respond(res, 404, 'Not found', 'text/plain');
  } catch (error) { respond(res, 500, String(error), 'text/plain'); }
});

const closeSent = new WeakSet();
const sendWsFrame = (socket, opcode, payload) => {
  if (opcode === 8) {
    if (closeSent.has(socket)) throw new Error('fixture sent duplicate WebSocket close');
    closeSent.add(socket);
  }
  const body = Buffer.isBuffer(payload) ? payload : Buffer.from(payload);
  const header = body.length < 126
    ? Buffer.from([0x80 | opcode, body.length])
    : Buffer.from([0x80 | opcode, 126, body.length >> 8, body.length & 255]);
  socket.write(Buffer.concat([header, body]));
};
const acceptUpgrade = ({ req, socket, head }) => {
  if (socket.destroyed || socket.readableEnded || socket.writableEnded) {
    if (!socket.destroyed) socket.end();
    return;
  }
  const path = new URL(req.url, 'http://127.0.0.1').pathname;
  const terminal = path.endsWith(`/terminal/${CHAT}/ws`);
  const accept = createHash('sha1')
    .update(req.headers['sec-websocket-key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11')
    .digest('base64');
  socket.write('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n' +
    `Sec-WebSocket-Accept: ${accept}\r\n` +
    (terminal ? 'Sec-WebSocket-Protocol: tty\r\n' : '') + '\r\n');
  activeSockets.add(socket);
  socket.on('close', () => activeSockets.delete(socket));
  let buffered = head;
  const receive = chunk => {
    buffered = Buffer.concat([buffered, chunk]);
    while (buffered.length >= 2) {
      const opcode = buffered[0] & 15;
      const masked = Boolean(buffered[1] & 0x80);
      const wireLength = buffered[1] & 127;
      let length = wireLength;
      let offset = 2;
      if (wireLength === 126) {
        if (buffered.length < 4) return;
        length = buffered.readUInt16BE(2);
        offset = 4;
      }
      if (wireLength === 127 || !masked || (opcode >= 8 && length > 125)) {
        socket.destroy();
        return;
      }
      if (buffered.length < offset + 4 + length) return;
      const mask = buffered.subarray(offset, offset + 4);
      const body = Buffer.from(buffered.subarray(offset + 4, offset + 4 + length));
      for (let i = 0; i < body.length; i++) body[i] ^= mask[i % 4];
      buffered = buffered.subarray(offset + 4 + length);
      if (opcode === 8) {
        if (!closeSent.has(socket)) sendWsFrame(socket, 8, body);
        socket.end();
        return;
      }
      if (opcode !== 1) continue;
      try {
        const message = JSON.parse(body.toString('utf8'));
        socketMessages.push({ path, method: message.method || (terminal ? 'ttyd-init' : 'unknown') });
        if (terminal && ttydSendsData)
          sendWsFrame(socket, 2, Buffer.from([0x30, 0x6f, 0x6b, 0x0d, 0x0a]));
        else if (Number.isInteger(message.id)) sendWsFrame(socket, 1,
          JSON.stringify({ id: message.id, result: {} }));
      } catch { socket.destroy(); return; }
    }
  };
  socket.on('data', receive);
  if (head.length) receive(Buffer.alloc(0));
};
server.on('upgrade', (req, socket, head) => {
  socket.on('error', () => socket.destroy());
  const path = new URL(req.url, 'http://127.0.0.1').pathname;
  if (!path.endsWith(`/terminal/${CHAT}/ws`) &&
      !path.endsWith(`/browser/${CHAT}/devtools/page/fixture-page`)) {
    socket.destroy();
    return;
  }
  socket.on('end', () => socket.end());
  upgradeAttempts.push(path);
  if (upgradesHeld) heldUpgrades.push({ req, socket, head });
  else acceptUpgrade({ req, socket, head });
});

async function verifyOfficeShell(browser, origin, artifacts, allowedOrigin, deniedOrigin) {
  const context = await browser.newContext();
  context.setDefaultTimeout(10000);
  await context.addInitScript(() => {
    window.__officeViolations = [];
    document.addEventListener('securitypolicyviolation', event => window.__officeViolations.push({
      directive: event.effectiveDirective, uri: event.blockedURI,
    }));
    window.__officeMessages = [];
    window.addEventListener('message', event => {
      if (event.source !== window) window.__officeMessages.push(event.data);
    });
    window.__officeMessageListeners = 0;
    const add = window.addEventListener.bind(window);
    window.addEventListener = (type, ...args) => {
      if (type === 'message') window.__officeMessageListeners++;
      return add(type, ...args);
    };
    window.__officeIntervals = 0;
    const interval = window.setInterval.bind(window);
    window.setInterval = (...args) => {
      window.__officeIntervals++;
      return interval(...args);
    };
  });
  const page = await context.newPage();
  const errors = [];
  const failures = [];
  const outbound = [];
  const probes = [];
  page.on('pageerror', error => errors.push({ text: String(error), pageerror: true }));
  page.on('console', message => {
    if (message.type() === 'error') errors.push({ text: message.text(), pageerror: false });
  });
  page.on('requestfailed', request => failures.push({
    url: request.url(), failure: request.failure()?.errorText || '',
  }));
  page.on('request', request => outbound.push(request.url()));
  page.on('websocket', socket => outbound.push(socket.url()));
  const shell = () => page.frameLocator('#preview');
  const settle = async () => {
    await shell().locator('#app').evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await new Promise(resolve => setTimeout(resolve, 150));
  };
  const silent = async (start, officeReady = false, priorMessages = []) => {
    const nonAssets = outbound.slice(start).filter(url => {
      const address = new URL(url);
      return address.origin !== origin ||
        !(/\/static\//.test(address.pathname) || address.pathname.includes(`/preview/${CHAT}`));
    });
    assert.deepEqual(nonAssets, [], 'Office or invalid embedding issued a request');
    if (!officeReady) {
      assert.deepEqual(await page.evaluate(() => window.__officeMessages), priorMessages, 'invalid embedding posted a message');
    }
    assert.equal(await shell().locator('#app').evaluate(() => window.__officeIntervals), 0,
      'idle embedding started a timer');
    if (!officeReady) {
      assert.equal(await shell().locator('#app').evaluate(() => window.__officeMessageListeners), 0,
        'invalid embedding installed a message listener');
    }
    assert.deepEqual(officeArrivals, { allowed: [], denied: [] }, 'idle embedding loaded DocumentServer');
  };
  try {
    for (const prefix of ['', '/ocu', '/tools/ocu']) {
      await page.goto(`${origin}/parent`);
      const start = outbound.length;
      await page.evaluate(prefix => window.mount('?embed=office', prefix), prefix);
      await shell().locator('#office-editor [role="status"]').getByText('Office editor idle').waitFor();
      assert.equal(await shell().locator('#office-editor').isVisible(), true);
      const area = await shell().locator('#office-editor').boundingBox();
      assert(area && area.width > 0 && area.height > 0, 'Office editor has no visible area');
      assert.equal(await shell().locator('.view-tabs, .files-panel, .file-selector-btn, .upload-btn, .browser-panel, .terminal-panel, .cli-badge, input[type="file"]').count(), 0);
      assert.equal(await shell().locator('#app').evaluate(() => window.__CONFIG__.officeDocserverOrigin), allowedOrigin);
      await page.evaluate(() => window.select('doc', 0));
      await page.waitForFunction(() => document.querySelector('#preview').contentWindow.__officeMessages
        .some(payload => payload?.type === 'ocu:preview-select' && payload.generation === 0));
      await settle();
      assert.deepEqual(await page.evaluate(() => window.__officeMessages),
        [{ type: 'ocu:office-ready', chat_id: CHAT }], 'Office host did not announce readiness');
      await silent(start, true);
      await page.screenshot({ path: path.join(artifacts, `office-shell-${prefix ? prefix.replaceAll('/', '-') : 'root'}.png`) });
      await page.evaluate(() => window.retire());
      for (const query of ['?embed=office&embed=office', '?embed=office&embed=files', '?embed=unknown',
        '?embed=office&office_fixture=absent', '?embed=office&office_fixture=blank']) {
        const invalidStart = outbound.length;
        const priorMessages = await page.evaluate(() => window.__officeMessages);
        await page.evaluate(({ query, prefix }) => window.mount(query, prefix), { query, prefix });
        await shell().getByRole('alert').getByText('Invalid preview embedding').waitFor();
        assert.equal(await shell().locator('#office-editor').count(), 0);
        await settle();
        await silent(invalidStart, false, priorMessages);
        await page.screenshot({ path: path.join(artifacts, `office-invalid-${prefix ? prefix.replaceAll('/', '-') : 'root'}-${query.replaceAll(/[^a-z]/g, '-')}.png`) });
        await page.evaluate(() => window.retire());
      }
      const top = await context.newPage();
      const topRequests = [];
      top.on('request', request => topRequests.push(request.url()));
      top.on('websocket', socket => topRequests.push(socket.url()));
      top.on('pageerror', error => errors.push({ text: String(error), pageerror: true }));
      top.on('console', message => {
        if (message.type() === 'error') errors.push({ text: message.text(), pageerror: false });
      });
      top.on('requestfailed', request => failures.push({
        url: request.url(), failure: request.failure()?.errorText || '',
      }));
      try {
        await top.goto(`${origin}${prefix}/preview/${CHAT}?embed=office`);
        await top.getByRole('alert').getByText('Invalid preview embedding').waitFor();
        await new Promise(resolve => setTimeout(resolve, 150));
        assert.equal(await top.locator('#office-editor').count(), 0);
        assert.deepEqual(topRequests.filter(url => {
          const address = new URL(url);
          return address.origin !== origin || !(/\/static\//.test(address.pathname) ||
            address.pathname === `${prefix}/preview/${CHAT}` || address.pathname === '/favicon.ico');
        }), [], 'top-level Office embedding issued a request');
        assert.deepEqual(await top.evaluate(() => window.__officeMessages), []);
        assert.equal(await top.evaluate(() => window.__officeMessageListeners + window.__officeIntervals), 0);
        assert.deepEqual(officeArrivals, { allowed: [], denied: [] });
        await top.screenshot({ path: path.join(artifacts, `office-top-level-${prefix ? prefix.replaceAll('/', '-') : 'root'}.png`) });
      } finally { await top.close(); }
    }
    assert.deepEqual(errors, [], 'idle or invalid Office embedding produced console errors');
    assert.deepEqual(failures, [], 'idle or invalid Office embedding produced network failures');
    await page.goto(`${origin}/parent`);
    await page.evaluate(() => window.mount('?embed=office'));
    await shell().locator('#office-editor').waitFor();
    await page.waitForFunction(() => window.__officeMessages.some(message => message?.type === 'ocu:office-ready'));
    const policyMessages = await page.evaluate(() => window.__officeMessages);
    assert.deepEqual(policyMessages, [{ type: 'ocu:office-ready', chat_id: CHAT }]);
    const positive = await shell().locator('#app').evaluate(async (_node, allowed) => {
      const script = document.createElement('script');
      script.src = `${allowed}/office-script.js`;
      const loaded = new Promise((resolve, reject) => {
        script.onload = () => resolve(window.__officeScriptCanary);
        script.onerror = () => reject(new Error('configured script did not load'));
      });
      document.head.appendChild(script);
      const frame = document.createElement('iframe');
      frame.id = 'office-policy-frame';
      frame.src = `${allowed}/office-frame`;
      document.body.appendChild(frame);
      return loaded;
    }, allowedOrigin);
    assert.equal(positive, 'executed', 'configured-origin script did not execute');
    const positiveFrame = shell().frameLocator('#office-policy-frame').locator('#office-frame-canary');
    await positiveFrame.getByText('Office frame rendered').waitFor();
    assert.equal(await positiveFrame.isVisible(), true, 'configured-origin frame did not render');
    assert.deepEqual(officeArrivals.allowed.map(row => row.path).sort(), ['/office-frame', '/office-script.js']);
    assert(officeArrivals.allowed.every(row => row.authorization === undefined), 'external origin received authorization');
    assert.deepEqual(errors, [], 'configured-origin canaries produced console errors');
    assert.deepEqual(failures, [], 'configured-origin canaries produced network failures');
    probes.push(
      { url: `${deniedOrigin}/office-script.js`, directive: 'script-src-elem' },
      { url: `${deniedOrigin}/office-frame`, directive: 'frame-src' },
      { url: `${deniedOrigin}/office-connect`, directive: 'connect-src' },
      { url: `${allowedOrigin}/office-connect`, directive: 'connect-src' },
    );
    const blocked = await shell().locator('#app').evaluate(async (_node, probes) => {
      const script = document.createElement('script');
      script.src = probes[0].url;
      const deniedScript = new Promise(resolve => {
        script.onload = () => resolve(false);
        script.onerror = () => resolve(true);
      });
      document.head.appendChild(script);
      const frame = document.createElement('iframe');
      frame.src = probes[1].url;
      document.body.appendChild(frame);
      return Promise.all([deniedScript, ...probes.slice(2).map(probe => fetch(probe.url).then(() => false, () => true))]);
    }, probes);
    assert.deepEqual(blocked, [true, true, true], 'Office CSP admitted forbidden script or connections');
    await page.waitForFunction(probes => {
      const violations = document.querySelector('#preview').contentWindow.__officeViolations;
      return probes.every(probe => violations.some(event => event.directive === probe.directive &&
        (event.uri === probe.url || event.uri === new URL(probe.url).origin)));
    }, probes);
    const violations = await shell().locator('#app').evaluate(() => window.__officeViolations);
    await waitUntil(() => probes.every(probe => errors.some(error =>
      !error.pageerror && /content security policy/i.test(error.text) &&
      (error.text.includes(probe.url) || probe.directive === 'frame-src' &&
        error.text.includes(new URL(probe.url).origin) && error.text.includes('frame-src')))),
    'correlated Office CSP console errors');
    await settle();
    const correlated = probe => violations.some(event => event.directive === probe.directive &&
      (event.uri === probe.url || event.uri === new URL(probe.url).origin));
    assert(probes.every(correlated), 'Office denial lacked a correlated CSP event');
    assert.deepEqual(errors.filter(error => error.pageerror ||
      !/content security policy/i.test(error.text) ||
      !probes.some(probe => correlated(probe) &&
        (error.text.includes(probe.url) || probe.directive === 'frame-src' &&
          error.text.includes(new URL(probe.url).origin) && error.text.includes('frame-src')))),
    [], 'unexpected Office browser console errors');
    assert.deepEqual(failures.filter(failure =>
      !probes.some(probe => probe.url === failure.url && correlated(probe)) ||
      !(failure.failure === 'csp' || failure.failure.includes('ERR_BLOCKED_BY_CSP'))),
    [], 'unexpected Office network failure');
    assert.deepEqual(officeArrivals.denied, [], 'forbidden origin received a request');
    assert.deepEqual(officeArrivals.allowed.map(row => row.path).sort(), ['/office-frame', '/office-script.js'],
      'configured origin received a forbidden connection');
    assert.equal(await shell().locator('#app').evaluate(() => window.__officeScriptCanary), 'executed');
    assert.deepEqual(await page.evaluate(() => window.__officeMessages), policyMessages, 'Office shell reported policy probes');
  } finally { await context.close(); }
}


async function main() {
  const artifacts = process.env.OCU_PREVIEW_ARTIFACTS || await fs.mkdtemp(path.join(os.tmpdir(), 'ocu-preview-browser-'));
  await fs.mkdir(artifacts, { recursive: true });
  await Promise.all(officeStandins.map(standin => new Promise(resolve => standin.listen(0, '127.0.0.1', resolve))));
  const [allowedOrigin, deniedOrigin] = officeStandins.map(standin => `http://127.0.0.1:${standin.address().port}`);
  previewCaptures = await captureProductionPreviews(allowedOrigin);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await playwright.chromium.launch({ headless: true });
  try {
    await verifyOfficeShell(browser, origin, artifacts, allowedOrigin, deniedOrigin);
    const context = await browser.newContext();
    context.setDefaultTimeout(10000);
    const freezePollingClock = () => {
      const start = window.setInterval.bind(window);
      const stop = window.clearInterval.bind(window);
      const startTimeout = window.setTimeout.bind(window);
      const stopTimeout = window.clearTimeout.bind(window);
      window.__runtimeTimeouts = new Map();
      window.setTimeout = (callback, delay, ...args) => {
        if (typeof callback !== 'function' || ![1000, 2000, 4000].includes(delay))
          return startTimeout(callback, delay, ...args);
        const timer = startTimeout((...values) => {
          window.__runtimeTimeouts.delete(timer);
          callback.apply(window, values);
        }, delay, ...args);
        window.__runtimeTimeouts.set(timer, delay);
        return timer;
      };
      window.clearTimeout = timer => {
        window.__runtimeTimeouts.delete(timer);
        return stopTimeout(timer);
      };
      window.__heartbeatTimers = new Set();
      window.__pollIntervals = new Set();
      window.setInterval = (callback, delay, ...args) => {
        const timer = start(callback, delay, ...args);
        if (delay === 120000) window.__heartbeatTimers.add(timer);
        // App visible/hidden poll delays in preview.js (setInterval(poll, 3000/15000)).
        if (delay === 3000 || delay === 15000) window.__pollIntervals.add(timer);
        if (delay === 3000 || delay === 15000) window.__runAppPoll = callback;
        return timer;
      };
      window.clearInterval = timer => {
        window.__heartbeatTimers.delete(timer);
        window.__pollIntervals.delete(timer);
        return stop(timer);
      };
      window.__freezePollingClock = () => {
        for (const timer of [...window.__pollIntervals]) {
          stop(timer);
          window.__pollIntervals.delete(timer);
        }
      };
    };
    await context.addInitScript(freezePollingClock);
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    const consoleErrors = [];
    const expectedOwnedErrors = [];
    const uploadFaults = [];
    const observedBrowserSockets = [];
    page.on('websocket', socket => {
      const path = new URL(socket.url()).pathname;
      if (!path.endsWith(`/browser/${CHAT}/devtools/page/fixture-page`)) return;
      const observation = { closed: false, socket };
      observedBrowserSockets.push(observation);
      socket.on('close', () => { observation.closed = true; });
    });
    page.on('pageerror', error => consoleErrors.push({ text: String(error), url: '' }));
    const expectedAbortUrls = [];
    const expectedCspErrors = [];
    const deniedScriptUrl = 'https://example.invalid/runtime-denied.js';
    const deniedConnectionUrl = 'https://example.invalid/runtime-denied-api';
    let cspProbeArmed = false;
    let frameCspArmed = false;
    const cspProbeTargets = new Set();
    const cspProbeFailures = new Set();
    const pendingRuntimeRequests = new Set();
    const expectedRetiredRuntimeRequests = new Set();
    let retiringFrame = null;
    const expectedRuntimeFailures = [];
    const unexpectedNetworkFailures = [];
    page.on('request', request => {
      const address = new URL(request.url());
      if (address.origin !== origin ||
          !(address.pathname.endsWith(`/browser/${CHAT}/status`) ||
            address.pathname.endsWith(`/browser/${CHAT}/json`) ||
            // The 503 start and the recovery restart bodies stay unread; frame retirement aborts them.
            address.pathname.endsWith(`/terminal/${CHAT}/start-ttyd`) ||
            address.pathname.endsWith(`/terminal/${CHAT}/restart-container`) ||
            // The failed status read before a start leaves its 503 body unread.
            (terminalFixture === 'status-error' && address.pathname.endsWith(`/terminal/${CHAT}/status`)))) return;
      pendingRuntimeRequests.add(request);
      try {
        if (retiringFrame && request.frame() === retiringFrame)
          expectedRetiredRuntimeRequests.add(request);
      } catch {}
    });
    page.on('requestfinished', request => {
      pendingRuntimeRequests.delete(request);
      expectedRetiredRuntimeRequests.delete(request);
    });
    page.on('requestfailed', request => {
      const failure = request.failure()?.errorText || '';
      const url = request.url();
      const uploadFault = uploadFaults.find(fault => fault.request === request && fault.kind === 'network');
      if (uploadFault && failure === 'net::ERR_CONNECTION_RESET') {
        uploadFault.failure = failure;
        return;
      }
      if (cspProbeTargets.has(url) &&
          (failure === 'csp' || failure.includes('ERR_BLOCKED_BY_CSP'))) {
        cspProbeTargets.delete(url);
        cspProbeFailures.add(url);
        return;
      }
      const expectedRetirement = expectedRetiredRuntimeRequests.delete(request);
      pendingRuntimeRequests.delete(request);
      if (failure.includes('ERR_ABORTED') && (
          url.includes(`/ocu/api/outputs/${CHAT}`) ||
          url.endsWith('/slow.docx') ||
          expectedRetirement)) {
        expectedAbortUrls.push(url);
      } else unexpectedNetworkFailures.push(`${url}: ${failure}`);
    });
    const externalRequests = [];
    await context.route('**/*', route => {
      const url = route.request().url();
      if (/^https?:/.test(url) && !url.startsWith(origin + '/')) {
        externalRequests.push(url);
        return route.abort();
      }
      return route.continue();
    });
    let accepted503 = 0;
    page.on('console', message => {
      if (message.type() !== 'error') return;
      const text = message.text();
      const url = message.location().url;
      const expectedBrokerUrl = `${origin}/ocu/api/outputs/${CHAT}`;
      if (injectedListingFailure && accepted503 === 0 && text.includes('503')
          && (url.startsWith(expectedBrokerUrl) || text.includes(expectedBrokerUrl))) {
        accepted503++;
        return;
      }
      if (text.includes('Content Security Policy') &&
          ((cspProbeArmed &&
            (text.includes(deniedScriptUrl) || text.includes(deniedConnectionUrl))) ||
           (frameCspArmed && text.includes("frame-src 'none'")))) {
        expectedCspErrors.push(text);
        return;
      }
      if (browserFixture === 'failure' && text.includes('503') &&
          (url.includes(`/browser/${CHAT}/status`) || text.includes(`/browser/${CHAT}/status`))) {
        expectedRuntimeFailures.push(text);
        return;
      }
      if ((text.includes('503') || (text.includes('422') && terminalFixture.startsWith('refused'))) &&
          (url.includes(`/terminal/${CHAT}/start-ttyd`) || text.includes(`/terminal/${CHAT}/start-ttyd`))) {
        expectedRuntimeFailures.push(text);
        return;
      }
      if (text.includes('503') &&
          (url.includes(`/terminal/${CHAT}/status`) || text.includes(`/terminal/${CHAT}/status`))) {
        expectedRuntimeFailures.push(text);
        return;
      }
      consoleErrors.push({ text, url });
    });
    const frame = () => page.frameLocator('#preview');
    const retireFrame = async () => {
      const iframe = await page.locator('#preview').elementHandle();
      assert(iframe, 'missing preview iframe before retirement');
      const owner = await iframe.contentFrame();
      await iframe.dispose();
      assert(owner, 'preview iframe has no attached frame');
      retiringFrame = owner;
      for (const request of pendingRuntimeRequests) {
        try {
          for (let requestFrame = request.frame(); requestFrame; requestFrame = requestFrame.parentFrame()) {
            if (requestFrame === owner) {
              expectedRetiredRuntimeRequests.add(request);
              break;
            }
          }
        } catch {}
      }
      try {
        await page.evaluate(() => window.retire());
      } finally {
        retiringFrame = null;
      }
    };
    const waitState = async (generation, state, timeout = 10000) => {
      await page.waitForFunction(({ generation, state }) => window.states.some(row =>
        row.type === 'ocu:preview-state' && row.generation === generation && row.state === state),
      { generation, state }, { timeout });
      const result = await page.evaluate(({ generation, state }) => window.states.find(row =>
        row.type === 'ocu:preview-state' && row.generation === generation && row.state === state),
      { generation, state });
      assert.deepEqual(Object.keys(result).sort(), ['chat_id', 'file_id', 'generation', 'state', 'type']);
      assert.equal(result.chat_id, CHAT);
      assert.equal(result.file_id, await page.evaluate(g => window.expectedSelections[g], generation));
      return result;
    };
    await page.goto(origin + '/parent');
    const extendedFrame = await within(page.evaluate(async socketUrl => {
      const socket = new WebSocket(socketUrl);
      const closed = new Promise(resolve => socket.addEventListener('close', resolve, { once: true }));
      try {
        await new Promise((resolve, reject) => {
          socket.addEventListener('open', resolve, { once: true });
          socket.addEventListener('error', () => reject(new Error('fixture WebSocket upgrade failed')),
            { once: true });
        });
        const message = { id: 77, method: 'Fixture.length127', padding: '' };
        const baseLength = new TextEncoder().encode(JSON.stringify(message)).length;
        if (baseLength > 127) throw new Error('127-byte fixture method exceeds frame boundary');
        message.padding = 'x'.repeat(127 - baseLength);
        const payload = JSON.stringify(message);
        const length = new TextEncoder().encode(payload).length;
        if (length !== 127) throw new Error('fixture did not build a 127-byte payload');
        const reply = new Promise((resolve, reject) => {
          socket.addEventListener('message', event => {
            const result = JSON.parse(event.data);
            if (result.id === message.id) resolve(result);
          });
          socket.addEventListener('close', () => reject(new Error('127-byte frame was rejected')),
            { once: true });
        });
        socket.send(payload);
        const result = await reply;
        socket.close();
        await closed;
        return { length, replyId: result.id };
      } finally {
        if (socket.readyState < WebSocket.CLOSING) socket.close();
      }
    }, `${origin.replace(/^http/, 'ws')}/ocu/browser/${CHAT}/devtools/page/fixture-page`),
    '127-byte masked WebSocket frame');
    assert.deepEqual(extendedFrame, { length: 127, replyId: 77 });
    assert(socketMessages.some(row => row.method === 'Fixture.length127'),
      'fixture decoder did not consume the extended 127-byte frame');
    await waitUntil(() => activeSockets.size === 0, '127-byte fixture WebSocket close');
    const fixture = await page.evaluate(async () => {
      const relNS = 'http://schemas.openxmlformats.org/package/2006/relationships';
      const officeNS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships';
      const typesNS = 'http://schemas.openxmlformats.org/package/2006/content-types';
      const makeDoc = async (text, hostile = false) => {
        const zip = new JSZip();
        zip.file('[Content_Types].xml', `<Types xmlns="${typesNS}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="png" ContentType="image/png"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>`);
        zip.file('_rels/.rels', `<Relationships xmlns="${relNS}"><Relationship Id="rId1" Type="${officeNS}/officeDocument" Target="word/document.xml"/></Relationships>`);
        const links = hostile ? `<w:hyperlink r:id="evil"><w:r><w:t>Unsafe link</w:t></w:r></w:hyperlink>
          <w:hyperlink r:id="safe"><w:r><w:t>Safe link</w:t></w:r></w:hyperlink>
          <w:hyperlink w:anchor="section"><w:r><w:t>Go to section</w:t></w:r></w:hyperlink>
          <w:hyperlink w:anchor="user-content-policy"><w:r><w:t>Go to prefixed policy</w:t></w:r></w:hyperlink>
          <w:hyperlink w:anchor="chapter"><w:r><w:t>Go to ordinary chapter</w:t></w:r></w:hyperlink>` : '';
        const blocks = hostile ? `<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Table cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
          <w:p><w:r><w:drawing><wp:inline><wp:extent cx="9525" cy="9525"/><wp:docPr id="1" name="pixel"/>
            <a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
              <pic:pic><pic:nvPicPr><pic:cNvPr id="0" name="pixel.png"/><pic:cNvPicPr/></pic:nvPicPr>
                <pic:blipFill><a:blip r:embed="image"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>
                <pic:spPr><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>
              </pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>` : '';
        const bookmarkTargets = hostile
          ? Array.from({ length: 48 }, (_, index) =>
            `<w:p><w:r><w:t>Bookmark separation paragraph ${index + 1}.</w:t></w:r></w:p>`).join('') +
            '<w:p><w:bookmarkStart w:id="8" w:name="user-content-policy"/>' +
            '<w:r><w:t>Prefixed policy bookmark target</w:t></w:r><w:bookmarkEnd w:id="8"/></w:p>' +
            '<w:p><w:bookmarkStart w:id="9" w:name="chapter"/>' +
            '<w:r><w:t>Ordinary chapter bookmark target</w:t></w:r><w:bookmarkEnd w:id="9"/></w:p>'
          : '';
        zip.file('word/document.xml', `<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="${officeNS}"
          xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
          xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
          xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">
          <w:body><w:p>${hostile ? '<w:bookmarkStart w:id="7" w:name="section"/>' : ''}<w:r><w:rPr><w:b/></w:rPr><w:t>${text}</w:t></w:r>${links}${hostile ? '<w:bookmarkEnd w:id="7"/>' : ''}</w:p>${blocks}${bookmarkTargets}</w:body></w:document>`);
        zip.file('word/_rels/document.xml.rels', hostile
          ? `<Relationships xmlns="${relNS}"><Relationship Id="evil" Type="${officeNS}/hyperlink" Target="javascript:parent.__docxExecuted=1;void(0)" TargetMode="External"/>
            <Relationship Id="safe" Type="${officeNS}/hyperlink" Target="https://example.invalid/safe" TargetMode="External"/>
            <Relationship Id="image" Type="${officeNS}/image" Target="media/pixel.png"/></Relationships>`
          : `<Relationships xmlns="${relNS}"/>`);
        if (hostile) {
          zip.file('mammoth/style-map', 'p => iframe');
          zip.file('word/media/pixel.png', Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+XwN8AAAAASUVORK5CYII='), c => c.charCodeAt(0)));
        }
        return Array.from(await zip.generateAsync({ type: 'uint8array' }));
      };
      const wb = XLSX.utils.book_new();
      const formulaSheet = XLSX.utils.aoa_to_sheet([['Report'], ['Visible cell'], ['']]);
      formulaSheet.A3 = { t: 'n', f: '1+1' };
      XLSX.utils.book_append_sheet(wb, formulaSheet, 'Evidence');
      const biffBook = XLSX.utils.book_new();
      XLSX.utils.book_append_sheet(biffBook, XLSX.utils.aoa_to_sheet([['Report'], ['Visible cell']]), 'Evidence');
      const linkedBook = XLSX.utils.book_new();
      const linkedSheet = XLSX.utils.aoa_to_sheet([['Report'], ['Unsafe cell link']]);
      linkedSheet.A2.l = { Target: 'javascript:parent.__sheetExecuted=1;void(0)' };
      XLSX.utils.book_append_sheet(linkedBook, linkedSheet, 'Evidence');
      const oldBook = XLSX.utils.book_new();
      XLSX.utils.book_append_sheet(oldBook, XLSX.utils.aoa_to_sheet([['Report'], ['Older sheet']]), 'Evidence');
      const ppt = new JSZip();
      ppt.file('[Content_Types].xml', `<Types xmlns="${typesNS}">
        <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
        <Default Extension="xml" ContentType="application/xml"/>
        <Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
        <Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>
        <Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>
        <Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>
        <Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/></Types>`);
      ppt.file('_rels/.rels', `<Relationships xmlns="${relNS}"><Relationship Id="rId1" Type="${officeNS}/officeDocument" Target="ppt/presentation.xml"/></Relationships>`);
      ppt.file('ppt/presentation.xml', `<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="${officeNS}"><p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId2"/></p:sldMasterIdLst><p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst><p:sldSz cx="9144000" cy="5143500"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>`);
      ppt.file('ppt/_rels/presentation.xml.rels', `<Relationships xmlns="${relNS}"><Relationship Id="rId1" Type="${officeNS}/slide" Target="slides/slide1.xml"/><Relationship Id="rId2" Type="${officeNS}/slideMaster" Target="slideMasters/slideMaster1.xml"/></Relationships>`);
      ppt.file('ppt/slides/slide1.xml', `<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree>
        <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
        <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
        <p:sp><p:nvSpPr><p:cNvPr id="2" name="Office text"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
          <p:spPr><a:xfrm><a:off x="600000" y="600000"/><a:ext cx="8000000" cy="1000000"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:solidFill><a:srgbClr val="FF0000"/></a:solidFill></p:spPr>
          <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="en-US" sz="2400"/><a:t>Hello Deck</a:t></a:r></a:p></p:txBody>
        </p:sp></p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>`);
      ppt.file('ppt/slides/_rels/slide1.xml.rels', `<Relationships xmlns="${relNS}"><Relationship Id="rId1" Type="${officeNS}/slideLayout" Target="../slideLayouts/slideLayout1.xml"/></Relationships>`);
      const group = `<p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr></p:spTree>`;
      ppt.file('ppt/slideLayouts/slideLayout1.xml', `<p:sldLayout xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" type="blank" preserve="1"><p:cSld>${group}</p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>`);
      ppt.file('ppt/slideLayouts/_rels/slideLayout1.xml.rels', `<Relationships xmlns="${relNS}"><Relationship Id="rId1" Type="${officeNS}/slideMaster" Target="../slideMasters/slideMaster1.xml"/></Relationships>`);
      ppt.file('ppt/slideMasters/slideMaster1.xml', `<p:sldMaster xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="${officeNS}"><p:cSld>${group}</p:cSld><p:clrMap accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" bg1="lt1" bg2="lt2" folHlink="folHlink" hlink="hlink" tx1="dk1" tx2="dk2"/><p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst></p:sldMaster>`);
      ppt.file('ppt/slideMasters/_rels/slideMaster1.xml.rels', `<Relationships xmlns="${relNS}"><Relationship Id="rId1" Type="${officeNS}/slideLayout" Target="../slideLayouts/slideLayout1.xml"/><Relationship Id="rId2" Type="${officeNS}/theme" Target="../theme/theme1.xml"/></Relationships>`);
      ppt.file('ppt/theme/theme1.xml', `<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Office"><a:themeElements>
        <a:clrScheme name="Office"><a:dk1><a:srgbClr val="000000"/></a:dk1><a:lt1><a:srgbClr val="FFFFFF"/></a:lt1><a:dk2><a:srgbClr val="222222"/></a:dk2><a:lt2><a:srgbClr val="EEEEEE"/></a:lt2><a:accent1><a:srgbClr val="FF0000"/></a:accent1><a:accent2><a:srgbClr val="008000"/></a:accent2><a:accent3><a:srgbClr val="0000FF"/></a:accent3><a:accent4><a:srgbClr val="FFFF00"/></a:accent4><a:accent5><a:srgbClr val="00FFFF"/></a:accent5><a:accent6><a:srgbClr val="FF00FF"/></a:accent6><a:hlink><a:srgbClr val="0000FF"/></a:hlink><a:folHlink><a:srgbClr val="800080"/></a:folHlink></a:clrScheme>
        <a:fontScheme name="Office"><a:majorFont><a:latin typeface="Arial"/></a:majorFont><a:minorFont><a:latin typeface="Arial"/></a:minorFont></a:fontScheme>
        <a:fmtScheme name="Office"><a:fillStyleLst><a:solidFill><a:schemeClr val="accent1"/></a:solidFill></a:fillStyleLst><a:lnStyleLst><a:ln w="9525"><a:solidFill><a:schemeClr val="dk1"/></a:solidFill></a:ln></a:lnStyleLst><a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst><a:bgFillStyleLst><a:solidFill><a:schemeClr val="lt1"/></a:solidFill></a:bgFillStyleLst></a:fmtScheme>
      </a:themeElements></a:theme>`);
      return {
        doc: await makeDoc('Hello Office'), oldDoc: await makeDoc('Older Office'),
        hostile: await makeDoc('Safe content', true),
        sheet: Array.from(new Uint8Array(XLSX.write(wb, { bookType: 'xlsx', type: 'array' }))),
        biff: Array.from(new Uint8Array(XLSX.write(biffBook, { bookType: 'biff8', type: 'array' }))),
        raw2: Array.from(new Uint8Array(XLSX.write(biffBook, { bookType: 'biff2', type: 'array' }))),
        raw3: Array.from(new Uint8Array(XLSX.write(biffBook, { bookType: 'biff3', type: 'array' }))),
        raw4: Array.from(new Uint8Array(XLSX.write(biffBook, { bookType: 'biff4', type: 'array' }))),
        hostileSheet: Array.from(new Uint8Array(XLSX.write(linkedBook, { bookType: 'xlsx', type: 'array' }))),
        remoteMarkup: `<p>Converted Office image</p>
          <img src="https://example.invalid/document-image.png" alt="remote">
          <img src="/remote-document-image.png" alt="same-origin remote">
          <img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+XwN8AAAAASUVORK5CYII=" alt="inline">`,
        oldSheet: Array.from(new Uint8Array(XLSX.write(oldBook, { bookType: 'xlsx', type: 'array' }))),
        deck: Array.from(await ppt.generateAsync({ type: 'uint8array' })),
      };
    });
    const styleMapEvidence = await page.evaluate(async (raw) => {
      const arrayBuffer = Uint8Array.from(raw).buffer;
      const enabled = await mammoth.convertToHtml({ arrayBuffer: arrayBuffer.slice(0) }, { includeEmbeddedStyleMap: true });
      const disabled = await mammoth.convertToHtml({ arrayBuffer: arrayBuffer.slice(0) }, { includeEmbeddedStyleMap: false });
      return { enabled: enabled.value, disabled: disabled.value };
    }, fixture.hostile);
    assert(styleMapEvidence.enabled.includes('<iframe'), 'embedded hostile style-map fixture was not consumed');
    assert(!styleMapEvidence.disabled.includes('<iframe'), 'embedded style-map remained active');
    assert(styleMapEvidence.disabled.includes('javascript:parent.__docxExecuted'),
      'DOCX fixture did not expose the unsafe link when style maps were disabled');
    bytes['valid.docx'] = Buffer.from(fixture.doc);
    bytes['slow.docx'] = Buffer.from(fixture.oldDoc);
    bytes['diagram.drawio'] = Buffer.from('<mxfile><diagram id="page-1" name="Page-1"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/><mxCell id="2" value="Local shape" style="rounded=1;" vertex="1" parent="1"><mxGeometry x="40" y="40" width="120" height="60" as="geometry"/></mxCell></root></mxGraphModel></diagram></mxfile>');
    bytes['empty.drawio'] = Buffer.from('<mxfile host="app.diagrams.net"><diagram id="empty" name="Empty"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/></root></mxGraphModel></diagram></mxfile>');
    bytes['lazy.drawio'] = Buffer.from('<mxfile><diagram id="lazy" name="Lazy"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/><mxCell id="2" value="AND" style="shape=mxgraph.electrical.logic_gates.and;whiteSpace=wrap;" vertex="1" parent="1"><mxGeometry x="20" y="20" width="100" height="60" as="geometry"/></mxCell></root></mxGraphModel></diagram></mxfile>');
    bytes['broken.drawio'] = Buffer.from('<not-drawio/>');
    bytes['compressed.drawio'] = Buffer.from('<mxfile><diagram id="compressed" name="Compressed">' + require('node:zlib').deflateRawSync(Buffer.from(encodeURIComponent('<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/><mxCell id="2" value="Compressed shape" style="rounded=1;" vertex="1" parent="1"><mxGeometry x="40" y="40" width="120" height="60" as="geometry"/></mxCell></root></mxGraphModel>'))).toString('base64') + '</diagram></mxfile>');
    bytes['image.drawio'] = Buffer.from('<mxfile><diagram id="image" name="Image"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/><mxCell id="2" value="" style="shape=image;image=img/telecommunication/Cellphone_128x128.png;aspect=fixed;" vertex="1" parent="1"><mxGeometry x="20" y="20" width="80" height="80" as="geometry"/></mxCell></root></mxGraphModel></diagram></mxfile>');
    bytes['math.drawio'] = Buffer.from('<mxfile><diagram id="math" name="Math"><mxGraphModel math="1"><root><mxCell id="0"/><mxCell id="1" parent="0"/><mxCell id="2" value="$$E=mc^2$$" style="html=1;" vertex="1" parent="1"><mxGeometry x="20" y="20" width="120" height="40" as="geometry"/></mxCell></root></mxGraphModel></diagram></mxfile>');
    bytes['bpmn.drawio'] = Buffer.from('<mxfile><diagram id="bpmn" name="BPMN"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/><mxCell id="2" value="Start" style="shape=mxgraph.bpmn.shape;html=1;outline=standard;symbol=general;verticalLabelPosition=bottom;verticalAlign=top;" vertex="1" parent="1"><mxGeometry x="40" y="40" width="50" height="50" as="geometry"/></mxCell></root></mxGraphModel></diagram></mxfile>');
    bytes['er.drawio'] = Buffer.from('<mxfile><diagram id="er" name="ER"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/><mxCell id="2" value="Entity" style="shape=mxgraph.er.entity;whiteSpace=wrap;html=1;buttonText=Customer;" vertex="1" parent="1"><mxGeometry x="20" y="20" width="140" height="60" as="geometry"/></mxCell></root></mxGraphModel></diagram></mxfile>');
    bytes['pages.drawio'] = Buffer.from('<mxfile><diagram id="page-ordinary" name="Ordinary"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/><mxCell id="2" value="Page one" style="rounded=1;" vertex="1" parent="1"><mxGeometry x="40" y="40" width="120" height="60" as="geometry"/></mxCell></root></mxGraphModel></diagram><diagram id="page-and" name="AND"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/><mxCell id="2" value="AND" style="shape=mxgraph.electrical.logic_gates.and;whiteSpace=wrap;" vertex="1" parent="1"><mxGeometry x="20" y="20" width="100" height="60" as="geometry"/></mxCell></root></mxGraphModel></diagram></mxfile>');
    bytes['pages-missing.drawio'] = Buffer.from(bytes['pages.drawio']);
    bytes['corrupt-lazy.drawio'] = Buffer.from('<mxfile><diagram id="lazy" name="Lazy"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/><mxCell id="2" value="AND" style="shape=mxgraph.electrical.logic_gates.and;whiteSpace=wrap;" vertex="1" parent="1"><mxGeometry x="20" y="20" width="100" height="60" as="geometry"/></mxCell></root></mxGraphModel></diagram></mxfile>');
    bytes['pages-image.drawio'] = Buffer.from('<mxfile><diagram id="page-ordinary" name="Ordinary"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/><mxCell id="2" value="Page one" style="rounded=1;" vertex="1" parent="1"><mxGeometry x="40" y="40" width="120" height="60" as="geometry"/></mxCell></root></mxGraphModel></diagram><diagram id="page-image" name="Image"><mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/><mxCell id="2" value="" style="shape=image;image=img/telecommunication/Cellphone_128x128.png;aspect=fixed;" vertex="1" parent="1"><mxGeometry x="20" y="20" width="80" height="80" as="geometry"/></mxCell></root></mxGraphModel></diagram></mxfile>');
    bytes["report (1)!'.docx"] = Buffer.from(fixture.doc);
    bytes['hostile.docx'] = Buffer.from(fixture.hostile);
    bytes['corrupt.docx'] = Buffer.from('not an Office ZIP');
    bytes['valid.xlsx'] = Buffer.from(fixture.sheet);
    bytes['hostile.xlsx'] = Buffer.from(fixture.hostileSheet);
    bytes['valid.xls'] = Buffer.from(fixture.biff);
    bytes['slow.xlsx'] = Buffer.from(fixture.oldSheet);
    bytes['broken.xlsx'] = Buffer.from('not an Office ZIP');
    bytes['valid.pptx'] = Buffer.from(fixture.deck);
    for (const [version, name] of [[2, 'raw2'], [3, 'raw3'], [4, 'raw4']]) {
      const raw = Buffer.from(fixture[name]);
      assert.equal(raw.readUInt16LE(0), { 2: 9, 3: 521, 4: 1033 }[version]);
      assert.equal(raw.readUInt16LE(4), version);
      bytes[`${name}.xls`] = raw;
    }
    const raw3 = Buffer.from(fixture.raw3);
    const firstRecordEnd = 4 + raw3.readUInt16LE(2);
    const nextLength = raw3.readUInt16LE(firstRecordEnd + 2);
    assert(nextLength > 0, 'raw BIFF fixture has no nonempty record after BOF');
    bytes['truncated.xls'] = raw3.subarray(0, raw3.length - 1);
    bytes['truncated-payload.xls'] = raw3.subarray(0, firstRecordEnd + 4 + nextLength - 1);
    bytes['missing-eof.xls'] = Buffer.from(fixture.raw4.slice(0, -4));
    const historical = Buffer.from(fixture.raw3);
    historical.writeUInt16LE(0x0300, 4);
    bytes['historical.xls'] = historical;
    bytes['short-header.xls'] = raw3.subarray(0, firstRecordEnd + 2);
    bytes['not-a-workbook.xls'] = Buffer.from('name,value\\nfalse,positive');
    bytes['broken.pptx'] = Buffer.from('not an Office ZIP');
    const rasters = await page.evaluate(() => {
      const canvas = document.createElement('canvas');
      canvas.width = canvas.height = 2;
      canvas.getContext('2d').fillRect(0, 0, 2, 2);
      return {
        png: canvas.toDataURL('image/png'),
        jpeg: canvas.toDataURL('image/jpeg'),
        webp: canvas.toDataURL('image/webp'),
        gif: 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7',
      };
    });
    for (const [format, data] of Object.entries(rasters))
      assert(data.startsWith(`data:image/${format};base64,`), `browser did not encode ${format}`);
    bytes['guides/pixel.png'] = Buffer.from(rasters.png.split(',')[1], 'base64');
    bytes['guides/chart.png'] = bytes['guides/pixel.png'];
    const compatibilityFragments = [
      { label: 'Normal heading', fragment: 'ordinary-policy', target: 'Ordinary Policy' },
      { label: 'Encoded normal heading', fragment: '%6Frdinary-policy', target: 'Ordinary Policy' },
      { label: 'Prefixed heading', fragment: 'user-content-policy', target: 'User Content Policy' },
      { label: 'Encoded prefixed heading', fragment: '%75ser-content-policy', target: 'User Content Policy' },
      { label: 'Normal HTML ID', fragment: 'raw-section', target: 'Raw Policy' },
      { label: 'Encoded normal HTML ID', fragment: '%72aw-section', target: 'Raw Policy' },
      { label: 'Prefixed HTML ID', fragment: 'user-content-raw-policy', target: 'Prefixed Raw Policy' },
      { label: 'Encoded prefixed HTML ID', fragment: '%75ser-content-raw-policy', target: 'Prefixed Raw Policy' },
    ];
    bytes['guides/compatibility.md'] = Buffer.from([
      ...compatibilityFragments.map(({ label, fragment }) => `[${label}](#${fragment})`),
      '![Nested token chart](/chart.png)',
      '<img alt="Origin-root HTML chart" src="/raw-root-chart.png">',
      ...Array.from({ length: 40 }, (_, index) => `Navigation spacing paragraph ${index + 1}.`),
      '# Ordinary Policy', '# User Content Policy',
      '<h2 id="raw-section">Raw Policy</h2>',
      '<h2 id="user-content-raw-policy">Prefixed Raw Policy</h2>',
    ].join('\n\n'));
    bytes['guides/safe.md'] = Buffer.from([
      '[Jump to heading](#safe-heading)',
      '<p><strong>Bold text</strong> <em>Italic text</em> <s>Struck text</s></p>',
      '~~Deleted text~~',
      '<blockquote>Quoted text</blockquote>', '- First item\n- Second item',
      '| Column | Value |\n| --- | --- |\n| Answer | 42 |',
      '<details><summary>Folded content</summary><p>Visible when opened</p></details>',
      '[Relative Office](../hostile.docx)', '[HTTPS destination](https://links.example.test/approved)',
      '[HTTP destination](http://links.example.test/approved)', '[Send mail](mailto:reader@example.test)',
      '![Relative raster](pixel.png)', '<img alt="Raw relative raster" src="pixel.png">',
      '![HTTPS raster](https://images.example.test/pixel.png)',
      '![HTTP raster](http://images.example.test/pixel.png)',
      ...Object.entries(rasters).map(([format, data]) => `![Inline ${format}](${data})`),
      '```javascript\nconst answer = 42;\n```',
      '```mermaid\ngraph TD\n  A[Alpha] --> B[Beta]\n```',
      '# Safe heading', 'Inline math $x^2$ and display math $$E=mc^2$$',
    ].join('\n\n'));
    bytes['matrix.md'] = Buffer.from([
      '<p><strong>Allowed matrix neighbor</strong><span style="position:fixed" onclick="document.documentElement.dataset.markdownMatrixCanary=\'executed\'">Clean span</span></p>',
      '<script>document.documentElement.dataset.markdownMatrixCanary="executed"</script>',
      '<style>body { display:none }</style><link rel="stylesheet" href="data:text/css,body{}">',
      '<meta name="matrix-probe" content="blocked"><base href="https://base.example.test/">',
      '<form><input value="blocked"><button>Blocked control</button><textarea>Blocked textarea</textarea><select><option>Blocked option</option></select></form>',
      '<iframe srcdoc="<p>blocked frame</p>"></iframe><object data="data:text/plain,blocked"></object><embed src="data:text/plain,blocked">',
      '<svg><text>Raw SVG forbidden</text></svg><math><mi>Raw math forbidden</mi></math>',
      '<audio></audio><video></video>',
      '<p id="DOMPurify">Namespaced content</p><a name="__OCU_OFFICE_PURIFY">Named anchor</a>',
      '<a href="javascript:document.documentElement.dataset.markdownMatrixCanary=\'executed\'">Script scheme</a>',
      '<a href="jav&#x09;ascript:alert(1)">Obfuscated scheme</a><a href="vbscript:msgbox(1)">VB scheme</a>',
      '<a href="file:///etc/passwd">File scheme</a><a href="blob:https://example.test/id">Blob scheme</a>',
      '<a href="data:text/html,blocked">Data link</a><a href="unknown:blocked">Unknown scheme</a>',
      '[Token script](javascript:alert%281%29)', '[Token unknown](unknown:blocked)',
      '<a href="#%E0%A4%A">Malformed fragment</a>',
      '<img alt="Forbidden SVG image" src="data:image/svg+xml;base64,PHN2Zy8+">',
      '<img alt="Forbidden mail image" src="mailto:reader@example.test">',
      '<img alt="Forbidden data image" src="data:text/plain;base64,YQ==">',
      '<img alt="Allowed matrix raster" src="' + rasters.png + '" srcset="https://images.example.test/other.png 2x">',
    ].join('\n\n'));
    bytes['hostile.md'] = Buffer.from(
      '<p><strong>Safe Markdown neighbor</strong></p>\n\n' +
      '<img alt="Markdown event canary" ' +
      'src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII=" ' +
      'onload="document.documentElement.dataset.markdownEventCanary = \'executed\'">\n');

    await page.evaluate(() => window.mount());
    await page.waitForFunction(() => window.states.some(row => row.type === 'ocu:preview-ready'), null, { timeout: 10000 });
    assert.deepEqual(await page.evaluate(() => window.states[0]), { type: 'ocu:preview-ready', chat_id: CHAT });
    assert.equal(await page.locator('#preview').getAttribute('sandbox'), 'allow-scripts allow-same-origin allow-forms');
    const before = requests.filter(row => row.path.includes('/api/outputs/')).length;
    assert.equal(before, 0, 'embedded page polls before selection');
    const bad = { type:'ocu:preview-select', chat_id:CHAT, file_id:'broken', generation:0 };
    await frame().locator('#app').waitFor();
    assert.equal(await frame().locator('#app').evaluate(() => window.__heartbeatTimers.size), 0,
      'Files embedding started a runtime heartbeat');
    const officeOpenRequests = requests.length;
    const officeOpenStates = await page.evaluate(() => window.states.length);
    const officeOpenMessages = await page.evaluate(() => window.frameMessages.length);
    const officeOpenUi = await frame().locator('#app').innerHTML();
    await frame().locator('#app').evaluate(() => {
      window.__filesOfficeDelivered = false;
      window.addEventListener('message', event => {
        window.__filesOfficeDelivered = event.data?.type === 'ocu:office-open';
      }, { once: true });
    });
    await page.evaluate(chat => document.querySelector('#preview').contentWindow.postMessage({
      type: 'ocu:office-open', chat_id: chat, file_id: 'doc', version: 1, generation: 0,
    }, location.origin), CHAT);
    await page.waitForFunction(() => document.querySelector('#preview').contentWindow.__filesOfficeDelivered);
    await frame().locator('#app').evaluate(() => new Promise(resolve => requestAnimationFrame(resolve)));
    assert.equal(requests.length, officeOpenRequests, 'Files frame acted on Office open');
    assert.equal(await page.evaluate(() => window.states.length), officeOpenStates, 'Files frame reported Office open');
    assert.equal(await page.evaluate(() => window.frameMessages.length), officeOpenMessages, 'Files frame posted an Office message');
    assert.equal(await frame().locator('#app').innerHTML(), officeOpenUi, 'Files frame changed on Office open');
    await page.evaluate(({ payload, target }) => {
      const iframe = document.createElement('iframe');
      iframe.srcdoc = `<script>parent.frames[0].postMessage(${JSON.stringify(payload)}, ${JSON.stringify(target)});parent.postMessage({type:'sibling-done'}, ${JSON.stringify(target)})<\/script>`;
      document.body.appendChild(iframe);
    }, { payload: bad, target: origin });
    await page.evaluate(({ payload, target }) => {
      const iframe = document.createElement('iframe');
      iframe.setAttribute('sandbox', 'allow-scripts');
      iframe.srcdoc = `<script>parent.frames[0].postMessage(${JSON.stringify(payload)}, ${JSON.stringify(target)});parent.postMessage({type:'opaque-done'}, ${JSON.stringify(target)})<\/script>`;
      document.body.appendChild(iframe);
    }, { payload: bad, target: origin });
    await frame().locator('#app').evaluate((element, payload) => {
      window.dispatchEvent(new MessageEvent('message', { data: payload, source: window.parent, origin: 'https://wrong.example' }));
      window.dispatchEvent(new MessageEvent('message', { data: payload, source: window, origin: location.origin }));
    }, bad);
    await page.evaluate(() => {
      window.select('doc', 0, { url: '/ocu/files/not-allowed' });
      window.select('doc', -1);
      window.select('doc', Number.MAX_SAFE_INTEGER + 1);
      window.select('', 0);
      window.select('doc', 0, { chat_id: 'wrong-chat' });
    });
    await page.waitForFunction(() => window.beacons['sibling-done']);
    await page.waitForFunction(() => window.beacons['opaque-done']);
    assert.equal((await page.evaluate(() => window.beacons['opaque-done'].origin)), 'null');
    assert.equal(await page.evaluate(() => window.beacons['sibling-done'].source === document.querySelector('#preview').contentWindow), false);
    await frame().locator('#app').evaluate(() => new Promise(resolve => setTimeout(resolve, 0)));
    assert.equal(requests.filter(row => row.path.includes('/api/outputs/')).length, before);
    assert.equal((await page.evaluate(() => window.states.filter(row => row.type === 'ocu:preview-state'))).length, 0);
    await page.evaluate(() => window.select('doc', 0));
    await waitState(0, 'loading');
    await waitState(0, 'ready');
    assert.equal(requests.filter(row => row.path.includes('/api/outputs/')).length, 2);
    await frame().locator('.preview-stage').getByText('Hello Office').waitFor();
    await page.screenshot({ path: path.join(artifacts, 'valid-office.png') });
    await page.evaluate(() => window.select('sheet', 1));
    await waitState(1, 'ready');
    await frame().locator('#app').evaluate(() => window.dispatchEvent(new MessageEvent('message', {
      data: { type: 'iframe-link-click', href: 'broken.xlsx', resolvedUrl: '/ocu/files/' + location.host + '/broken.xlsx' },
      source: window, origin: location.origin,
    })));
    await frame().locator('.preview-stage').getByText('Visible cell').waitFor();
    assert.equal(await frame().locator('.preview-stage .xlsx-uncomputed').textContent(), 'uncomputed');
    await page.evaluate(() => window.select('broken', 2));
    await waitState(2, 'error');
    await frame().locator('.preview-stage .empty-state').waitFor();
    assert.equal(await frame().locator('a[download]').count(), 0);
    await page.screenshot({ path: path.join(artifacts, 'corrupt-office.png') });
    const beforeReplay = requests.filter(row => row.path.includes('/api/outputs/')).length;
    await page.evaluate(() => {
      window.select('broken', 2);
      window.select('doc', 1);
      window.select('deck', 3);
    });
    await waitState(3, 'error');
    assert.equal(requests.filter(row => row.path.includes('/api/outputs/')).length, beforeReplay + 2);
    assert.equal((await page.evaluate(() => window.states.filter(row => row.generation === 2))).length, 2);
    assert.equal((await page.evaluate(() => window.states.filter(row => row.generation === 1))).length, 2);
    for (const [index, id] of ['svg', 'xhtml', 'xml', 'drawio', 'html-doc', 'xml-doc', 'plusxml'].entries()) {
      await page.evaluate(([file, generation]) => window.select(file, generation), [id, index + 4]);
      await waitState(index + 4, 'unsupported');
    }
    for (const [index, id] of ['foreign', 'cross-chat'].entries()) {
      await page.evaluate(([file, generation]) => window.select(file, generation), [id, index + 11]);
      await waitState(index + 11, 'error');
    }
    assert.equal(requests.filter(row => row.path.endsWith('.svg') || row.path.endsWith('.xhtml') ||
      row.path.endsWith('.xml') || row.path.endsWith('.drawio') || row.path.endsWith('spoof.docx') ||
      row.path.endsWith('text-xml.docx') || row.path.endsWith('suffix.docx') ||
      row.path.endsWith('foreign.docx') || row.path.endsWith('cross-chat.docx') ||
      row.path.includes('/static/drawio/')).length, 0);
    listMode = 'deleted';
    await page.evaluate(() => window.select('doc', 13));
    await waitState(13, 'missing');
    listMode = 'incomplete';
    await page.evaluate(() => window.select('absent', 14));
    await waitState(14, 'error');
    listMode = 'failure';
    await page.evaluate(() => window.select('doc', 15));
    await waitState(15, 'error');
    listMode = 'timeout';
    const started = Date.now();
    await page.evaluate(() => window.select('doc', 16));
    await waitState(16, 'error', 15000);
    const elapsed = Date.now() - started;
    assert(elapsed >= 9000 && elapsed <= 14000, `listing deadline was ${elapsed}ms, expected 10 seconds`);
    for (const response of heldListings.splice(0)) response.destroy();
    await frame().locator('#app').evaluate(() => {
      window.__officeCompleted = 0;
      const convert = window.mammoth.convertToHtml.bind(window.mammoth);
      window.mammoth.convertToHtml = async (...args) => {
        const result = await convert(...args);
        window.__officeCompleted++;
        return result;
      };
    });
    listMode = 'normal';
    await page.evaluate(() => window.select('slow', 17));
    await waitState(17, 'loading');
    await within(slowArrival, 'first delayed Office fetch');
    await page.evaluate(() => window.select('doc', 18));
    await waitState(18, 'ready');
    for (const response of held.splice(0)) respond(response, 200, bytes['slow.docx'], 'application/octet-stream');
    await page.frames().find(child => child.url().includes('/ocu/preview/')).waitForFunction(
      () => window.__officeCompleted === 2, null, { timeout: 10000 });
    assert.equal((await page.evaluate(() => window.states.filter(row => row.generation === 17))).length, 1);
    assert.equal(await frame().locator('.preview-stage').getByText('Hello Office').count(), 1);
    assert.equal(await frame().locator('.preview-stage').getByText('Older Office').count(), 0);
    await page.evaluate(() => window.select('deck-valid', 19));
    await waitState(19, 'ready');
    const visibleSlide = await frame().locator('canvas.pptx-slide').evaluate(canvas => {
      const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
      let red = 0;
      for (let i = 0; i < pixels.length; i += 4) {
        if (pixels[i + 3] && pixels[i] > 200 && pixels[i + 1] < 80 && pixels[i + 2] < 80) red++;
      }
      return red;
    });
    assert(visibleSlide > 100, 'real PPTX slide did not paint its red text panel');
    await page.screenshot({ path: path.join(artifacts, 'valid-presentation.png') });
    await page.evaluate(() => window.select('biff', 20));
    await waitState(20, 'ready');
    await frame().locator('.preview-stage').getByText('Visible cell').waitFor();
    await page.evaluate(() => window.select('punct', 21));
    await waitState(21, 'ready');
    assert(requests.some(row => row.path === `/ocu/files/${CHAT}/report%20%281%29%21%27.docx`));
    await page.evaluate(() => window.select('hostile', 22));
    await waitState(22, 'ready');
    await frame().locator('.preview-stage').getByText('Safe content').waitFor();
    assert.equal(await frame().locator('.preview-stage strong').filter({ hasText: 'Safe content' }).count(), 1);
    await frame().locator('.preview-stage').getByText('Table cell').waitFor();
    const inlineImages = await frame().locator('.preview-stage img[src^="data:image/png;base64,"]').count();
    assert.equal(inlineImages, 1, 'supported DOCX inline raster image lost');
    const decodedImageWidth = await frame().locator('.preview-stage img').evaluate(async image => {
      await image.decode();
      return image.naturalWidth;
    });
    assert.equal(decodedImageWidth, 1);
    const unsafe = frame().locator('.preview-stage a').filter({ hasText: 'Unsafe link' });
    assert.equal(await unsafe.getAttribute('href'), null);
    await unsafe.click();
    assert.equal(await page.evaluate(() => window.__docxExecuted), undefined);
    assert.equal(await frame().locator('#app').evaluate(() => window.__docxExecuted), undefined);
    const safeLink = frame().getByRole('link', { name: 'Safe link', exact: true });
    assert.equal(await safeLink.getAttribute('href'), 'https://example.invalid/safe');
    assert.equal(await safeLink.getAttribute('target'), '_blank');
    assert.equal(await safeLink.getAttribute('rel'), 'noopener noreferrer');
    const bookmark = frame().locator('.preview-stage a').filter({ hasText: 'Go to section' });
    assert.equal(await bookmark.getAttribute('href'), '#user-content-section');
    assert.equal(await frame().locator('.preview-stage [id="user-content-section"]').count(), 1);
    assert.equal(await frame().locator('.preview-stage .markdown-body').locator('script, style, svg, iframe, [onclick], [onerror], [style]').count(), 0);
    assert.deepEqual(externalRequests, [], 'document content caused an external request');
    const exerciseOfficeBookmarks = async (targetPage, surface, mode, iframe = null) => {
      for (const spec of [
        { kind: 'prefixed', linkText: 'Go to prefixed policy', targetId: 'user-content-policy',
          targetText: 'Prefixed policy bookmark target', href: '#user-content-policy' },
        { kind: 'ordinary', linkText: 'Go to ordinary chapter', targetId: 'user-content-chapter',
          targetText: 'Ordinary chapter bookmark target', href: '#user-content-chapter' },
      ]) {
        const label = `office-bookmark-${spec.kind}-${mode}`;
        const evidence = { case: label, clicked: false, before: null, after: null, errors: [] };
        const errorsBefore = consoleErrors.length;
        const frameStyle = iframe ? await iframe.getAttribute('style') : null;
        const measureBookmark = async () => surface.locator('#docxContainer').evaluate((body, spec) => {
          const link = Array.from(body.querySelectorAll('a')).find(node => node.textContent === spec.linkText);
          const target = body.querySelector(`[id="${spec.targetId}"]`);
          const paragraph = target?.closest('p');
          const measure = node => {
            if (!node) return null;
            const range = document.createRange();
            range.selectNodeContents(node);
            const bounds = range.getBoundingClientRect();
            const rect = { top: bounds.top, bottom: bounds.bottom, left: bounds.left,
              right: bounds.right, width: bounds.width, height: bounds.height };
            const clip = { top: 0, bottom: innerHeight, left: 0, right: innerWidth };
            const scroll = [];
            for (let ancestor = node.parentElement; ancestor; ancestor = ancestor.parentElement) {
              const style = getComputedStyle(ancestor);
              const box = ancestor.getBoundingClientRect();
              if (/(auto|scroll|hidden|clip)/.test(style.overflowY)) {
                clip.top = Math.max(clip.top, box.top + ancestor.clientTop);
                clip.bottom = Math.min(clip.bottom, box.top + ancestor.clientTop + ancestor.clientHeight);
              }
              if (/(auto|scroll|hidden|clip)/.test(style.overflowX)) {
                clip.left = Math.max(clip.left, box.left + ancestor.clientLeft);
                clip.right = Math.min(clip.right, box.left + ancestor.clientLeft + ancestor.clientWidth);
              }
              if (ancestor.scrollHeight > ancestor.clientHeight)
                scroll.push({ id: ancestor.id, className: ancestor.className,
                  top: ancestor.scrollTop, height: ancestor.clientHeight, contentHeight: ancestor.scrollHeight });
            }
            return { rect, clip, scroll,
              visible: rect.width > 0 && rect.height > 0 && rect.top >= clip.top &&
                rect.bottom <= clip.bottom && rect.left >= clip.left && rect.right <= clip.right,
              intersects: rect.width > 0 && rect.height > 0 && rect.bottom > clip.top &&
                rect.top < clip.bottom && rect.right > clip.left && rect.left < clip.right };
          };
          return { href: link?.getAttribute('href') ?? null, targetId: target?.id ?? null,
            targetText: paragraph?.textContent ?? null, link: measure(link), target: measure(paragraph) };
        }, spec);
        let failure;
        try {
          if (iframe) await iframe.evaluate(node => {
            node.style.width = '1000px';
            node.style.height = '700px';
          });
          const link = surface.locator('#docxContainer a').filter({ hasText: spec.linkText });
          await link.scrollIntoViewIfNeeded();
          evidence.before = await measureBookmark();
          assert.equal(evidence.before.targetId, spec.targetId, `${label}: DOCX bookmark target lost`);
          assert.equal(evidence.before.targetText, spec.targetText);
          assert.equal(evidence.before.link?.visible, true, `${label}: link is not in the visible scroll area`);
          assert.equal(evidence.before.target?.intersects, false, `${label}: target must start outside the visible scroll area`);
          await targetPage.screenshot({ path: path.join(artifacts, `${label}-before.png`) });
          await link.click();
          evidence.clicked = true;
          evidence.after = await measureBookmark();
          assert.equal(evidence.before.href, spec.href, `${label}: DOCX bookmark lost its usable href after Office sanitization`);
          const scrollDeadline = Date.now() + 2000;
          while (!evidence.after.target?.visible && Date.now() < scrollDeadline) {
            await new Promise(resolve => setTimeout(resolve, 25));
            evidence.after = await measureBookmark();
          }
          assert.equal(evidence.after.target?.visible, true,
            `${label}: clicking DOCX bookmark did not reveal its target text: ${JSON.stringify(evidence)}`);
          assert.deepEqual(consoleErrors.slice(errorsBefore), [], `${label}: DOCX bookmark produced browser errors`);
        } catch (error) {
          failure = error;
          evidence.failure = String(error);
          throw error;
        } finally {
          evidence.errors = consoleErrors.slice(errorsBefore);
          console.log(JSON.stringify(evidence));
          const captured = await Promise.allSettled([
            targetPage.screenshot({ path: path.join(artifacts, `${label}.png`) }),
            fs.writeFile(path.join(artifacts, `${label}.json`), JSON.stringify(evidence, null, 2) + '\n'),
          ]);
          const evidenceFailures = captured.filter(result => result.status === 'rejected').map(result => result.reason);
          try {
            if (iframe) await iframe.evaluate((node, style) => {
              if (style === null) node.removeAttribute('style');
              else node.setAttribute('style', style);
            }, frameStyle);
          } catch (error) {
            evidenceFailures.push(error);
          }
          for (const error of evidenceFailures)
            console.error(JSON.stringify({ case: label, artifactFailure: String(error) }));
          if (!failure && evidenceFailures.length) throw evidenceFailures[0];
        }
      }
    };
    await exerciseOfficeBookmarks(page, frame(), 'embedded', page.locator('#preview'));
    // Synthetic converted-markup boundary proof of the real sanitizer seam,
    // not an end-to-end DOCX fixture or a substituted Office renderer.
    const fragmentPolicy = await frame().locator('#app').evaluate(async root => {
      const { safeOfficeHtml } = await import(new URL('../static/preview.js', document.baseURI).href);
      const outside = document.createElement('p');
      outside.id = 'user-content-boundary-outside';
      outside.textContent = 'Document-global target outside the sanitized fragment';
      document.body.appendChild(outside);
      const probe = document.createElement('div');
      try {
        probe.appendChild(safeOfficeHtml(`
          <p id="boundary-chapter">Ordinary boundary target</p>
          <p id="user-content-boundary-policy">Prefixed boundary target</p>
          <a href="#boundary%2Dchapter">Encoded ordinary</a>
          <a href="#user%2Dcontent%2Dboundary%2Dpolicy">Encoded prefixed</a>
          <a href="#%E0%A4%A">Malformed encoding</a>
          <a href="#boundary-missing">Missing target</a>
          <a href="#boundary-outside">Document-global-only target</a>`));
        root.appendChild(probe);
        return {
          ids: Array.from(probe.querySelectorAll('[id]'), node => node.id),
          links: Array.from(probe.querySelectorAll('a'), node =>
            ({ text: node.textContent, href: node.getAttribute('href') })),
          documentGlobalTargetPresent: document.getElementById('user-content-boundary-outside') === outside,
          documentGlobalTargetInFragment: !!probe.querySelector('[id="user-content-boundary-outside"]'),
        };
      } finally {
        probe.remove();
        outside.remove();
      }
    });
    console.log(JSON.stringify({ case: 'office-bookmark-sanitizer-boundary', ...fragmentPolicy }));
    assert.deepEqual(fragmentPolicy, {
      ids: ['user-content-boundary-chapter', 'user-content-boundary-policy'],
      links: [
        { text: 'Encoded ordinary', href: '#user-content-boundary-chapter' },
        { text: 'Encoded prefixed', href: '#user-content-boundary-policy' },
        { text: 'Malformed encoding', href: null },
        { text: 'Missing target', href: null },
        { text: 'Document-global-only target', href: null },
      ],
      documentGlobalTargetPresent: true,
      documentGlobalTargetInFragment: false,
    }, 'Office fragments must use idempotent sanitized IDs and reject non-local or malformed targets');
    const remotePolicy = await frame().locator('#app').evaluate(async (root, markup) => {
      const { safeOfficeHtml } = await import(new URL('../static/preview.js', document.baseURI).href);
      const fragment = safeOfficeHtml(markup);
      const detached = document.createElement('div');
      detached.appendChild(fragment);
      root.appendChild(detached);
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      const images = Array.from(detached.querySelectorAll('img'), image => image.getAttribute('src'));
      const image = detached.querySelector('img');
      if (image) await image.decode();
      const width = image?.naturalWidth ?? 0;
      detached.remove();
      return { images, width };
    }, fixture.remoteMarkup);
    assert.equal(remotePolicy.images.length, 1, 'inline raster lost at sanitizer boundary');
    assert(remotePolicy.images[0].startsWith('data:image/png;base64,'));
    assert.equal(remotePolicy.width, 1);
    assert.equal(requests.filter(row => row.path === '/remote-document-image.png').length, 0);
    assert.deepEqual(externalRequests, [], 'remote converted image initiated a request');
    await page.screenshot({ path: path.join(artifacts, 'sanitized-office.png') });
    await page.evaluate(() => window.select('corrupt-doc', 23));
    await waitState(23, 'error');
    await frame().locator('.preview-stage .empty-state').waitFor();
    const rawSheetMarkup = await page.evaluate(data => {
      const workbook = XLSX.read(Uint8Array.from(data), { type: 'array' });
      return XLSX.utils.sheet_to_html(workbook.Sheets[workbook.SheetNames[0]]);
    }, fixture.hostileSheet);
    assert(rawSheetMarkup.includes('javascript:'), 'SheetJS fixture did not emit its hostile link');
    await page.evaluate(() => window.select('hostile-sheet', 24));
    await waitState(24, 'ready');
    const unsafeCell = frame().locator('.preview-stage a').filter({ hasText: 'Unsafe cell link' });
    assert.equal(await unsafeCell.getAttribute('href'), null);
    await unsafeCell.click();
    assert.equal(await page.evaluate(() => window.__sheetExecuted), undefined);
    await frame().locator('#app').evaluate(() => {
      window.__sheetCompleted = 0;
      const renderSheet = window.XLSX.utils.sheet_to_html;
      window.XLSX.utils.sheet_to_html = (...args) => {
        const output = renderSheet(...args);
        window.__sheetCompleted++;
        return output;
      };
    });
    await page.evaluate(() => window.select('slow-sheet', 25));
    await waitState(25, 'loading');
    await within(sheetArrival, 'delayed spreadsheet fetch');
    await page.evaluate(() => window.select('sheet', 26));
    await waitState(26, 'ready');
    for (const response of heldSheets.splice(0)) respond(response, 200, bytes['slow.xlsx'], 'application/octet-stream');
    await page.frames().find(child => child.url().includes('/ocu/preview/')).waitForFunction(
      () => window.__sheetCompleted === 2, null, { timeout: 10000 });
    assert.equal((await page.evaluate(() => window.states.filter(row => row.generation === 25))).length, 1);
    await frame().locator('.preview-stage').getByText('Visible cell').waitFor();
    assert.equal(await frame().locator('.preview-stage').getByText('Older sheet').count(), 0);
    for (const [index, file] of ['raw2', 'raw3', 'raw4', 'raw-historical'].entries()) {
      const generation = 28 + index;
      await page.evaluate(([id, value]) => window.select(id, value), [file, generation]);
      await waitState(generation, 'ready');
      await frame().locator('.preview-stage').getByText('Visible cell').waitFor();
    }
    for (const [index, file] of ['raw-truncated', 'raw-no-eof', 'raw-text', 'raw-payload', 'raw-short-header'].entries()) {
      const generation = 32 + index;
      await page.evaluate(([id, value]) => window.select(id, value), [file, generation]);
      await waitState(generation, 'error');
      await frame().locator('.preview-stage .empty-state').waitFor();
    }
    await page.evaluate(() => window.select('hostile-markdown', 37));
    await waitState(37, 'unsupported');
    assert.equal(requests.filter(row => row.path.endsWith('/hostile.md')).length, 0,
      'Office-only embedding fetched Markdown bytes');
    assert.equal(await frame().locator('.preview-stage .markdown-body').count(), 0,
      'Office-only embedding rendered Markdown');
    assert.equal(await page.locator('#preview').getAttribute('sandbox'),
      'allow-scripts allow-same-origin allow-forms');
    const secondSlow = new Promise(resolve => { slowRequested = resolve; });
    await page.evaluate(() => window.select('slow', 38));
    await waitState(38, 'loading');
    await within(secondSlow, 'unmounted Office fetch');
    const beforeUnmount = await page.evaluate(() => window.states.length);
    const abortedFetch = page.waitForEvent('requestfailed', {
      predicate: request => request.url().endsWith('/slow.docx'), timeout: 10000,
    });
    await retireFrame();
    await abortedFetch;
    for (const response of held.splice(0)) respond(response, 200, bytes['slow.docx'], 'application/octet-stream');
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    assert.equal(await page.evaluate(() => window.states.length), beforeUnmount);
    assert.deepEqual(await page.evaluate(() => window.postRetirement), [],
      'retired iframe emitted a product protocol message');
    await page.evaluate(() => window.mount('?embed=files&embed=files'));
    await frame().locator('[role="alert"]').getByText('Invalid preview embedding').waitFor();
    assert.equal((await page.evaluate(() => window.states.filter(row => row.type === 'ocu:preview-ready'))).length, 1);
    await page.locator('#preview').evaluate(element => element.remove());
    await page.evaluate(() => window.mount('?embed=unknown'));
    await frame().locator('[role="alert"]').getByText('Invalid preview embedding').waitFor();
    assert.equal((await page.evaluate(() => window.states.filter(row => row.type === 'ocu:preview-ready'))).length, 1);
    await page.locator('#preview').evaluate(element => element.remove());
    await page.evaluate(() => window.mount());
    await page.waitForFunction(() => window.states.filter(row => row.type === 'ocu:preview-ready').length === 2);
    const transcript = await page.evaluate(() => ({
      states: window.states, expected: window.expectedSelections,
    }));
    for (const message of transcript.states) {
      if (message.type === 'ocu:preview-ready') {
        assert.deepEqual(message, { type: 'ocu:preview-ready', chat_id: CHAT });
        continue;
      }
      assert.equal(message.type, 'ocu:preview-state', 'unexpected preview protocol message');
      assert.deepEqual(Object.keys(message).sort(), ['chat_id', 'file_id', 'generation', 'state', 'type']);
      assert.equal(message.chat_id, CHAT);
      assert(Number.isSafeInteger(message.generation) && message.generation >= 0);
      assert(Object.hasOwn(transcript.expected, message.generation),
        `unexpected generation ${message.generation}`);
      assert.equal(message.file_id, transcript.expected[message.generation],
        `wrong file_id in generation ${message.generation}`);
      assert(['loading', 'ready', 'error', 'missing', 'unsupported'].includes(message.state));
    }
    assert.equal(requests.filter(row => /\/(browser|terminal)\//.test(row.path)
      || /\/api\/runtime\//.test(row.path) || /\/api\/v1\//.test(row.path)).length, 0);
    assert(requests.filter(row => row.path.includes('/api/outputs/')).every(row => row.header === 'ocu-workspace'));
    assert(requests.filter(row => row.path.endsWith('.docx') || row.path.endsWith('.xlsx') || row.path.endsWith('.pptx'))
      .every(row => row.header === 'ocu-workspace'));
    await retireFrame();
    const listingBeforeRuntime = requests.filter(row => row.path === `/ocu/api/outputs/${CHAT}`).length;
    const terminalBeforeBrowser = requests.filter(row =>
      row.path.startsWith(`/ocu/terminal/${CHAT}/`) && !row.path.endsWith('/heartbeat')).length;
    const launchBeforeRuntime = requests.filter(row => /\/(restart-container|start-ttyd)$/.test(row.path)).length;
    const browserStatus = page.waitForResponse(response =>
      new URL(response.url()).pathname === `/ocu/browser/${CHAT}/status`);
    browserStatus.catch(() => {});
    const runtimeShell = page.waitForResponse(response =>
      new URL(response.url()).pathname === `/ocu/preview/${CHAT}` &&
      new URL(response.url()).searchParams.get('embed') === 'browser');
    runtimeShell.catch(() => {});
    await page.evaluate(() => window.mount('?embed=browser'));
    await frame().locator('.browser-panel .empty-title').waitFor();
    const policy = (await runtimeShell).headers()['content-security-policy'];
    assert(policy.includes("default-src 'none'") && policy.includes("frame-ancestors 'self'"));
    const nonce = policy.match(/'nonce-([^']+)'/)?.[1];
    assert(nonce, 'runtime response omitted its script nonce');
    assert.equal(await frame().locator('script[nonce]').evaluate(element => element.nonce), nonce);
    assert.equal(await frame().locator('#app').evaluate(async () => {
      const font = new FontFace('OCUFixture', 'url(/ocu/static/katex/fonts/KaTeX_Main-Regular.woff2)');
      await font.load();
      return font.status;
    }), 'loaded');
    cspProbeArmed = true;
    cspProbeTargets.add(deniedScriptUrl);
    cspProbeTargets.add(deniedConnectionUrl);
    const blocked = await frame().locator('#app').evaluate(async (_element, { scriptUrl, connectionUrl }) => {
      if (new URL(scriptUrl).origin === location.origin ||
          new URL(connectionUrl).origin === location.origin)
        throw new Error('runtime CSP negative controls must be external');
      const script = new Promise(resolve => {
        const element = document.createElement('script');
        element.src = scriptUrl;
        element.onload = () => resolve(false);
        element.onerror = () => resolve(true);
        document.head.appendChild(element);
      });
      const connection = fetch(connectionUrl).then(() => false, () => true);
      return Promise.all([script, connection]);
    }, { scriptUrl: deniedScriptUrl, connectionUrl: deniedConnectionUrl });
    assert.deepEqual(blocked, [true, true], 'runtime CSP allowed external content');
    assert.equal(externalRequests.length, 0, 'external runtime request reached the network');
    await waitUntil(() => expectedCspErrors.length >= 2, 'runtime CSP violations');
    await waitUntil(() => cspProbeFailures.has(deniedScriptUrl),
      'runtime script CSP request denial');
    assert(expectedCspErrors.some(text => text.includes(deniedScriptUrl)),
      'runtime script request lacked a correlated CSP violation');
    cspProbeTargets.clear();
    cspProbeArmed = false;
    const beforeNested = requests.filter(row => row.path === `/ocu/preview/${CHAT}`).length;
    frameCspArmed = true;
    await frame().locator('#app').evaluate((_element, chat) => {
      const nested = document.createElement('iframe');
      nested.src = `/ocu/preview/${chat}`;
      document.body.appendChild(nested);
    }, CHAT);
    await waitUntil(() => expectedCspErrors.some(text => text.includes("frame-src 'none'")),
      'runtime frame-src denial');
    assert.equal(requests.filter(row => row.path === `/ocu/preview/${CHAT}`).length,
      beforeNested, 'runtime CSP allowed a nested frame request');
    frameCspArmed = false;
    assert.equal((await browserStatus).status(), 200);
    assert.equal(await frame().locator('.view-tabs').count(), 0, 'browser embed mounted nested view tabs');
    assert(requests.some(row => row.path === `/ocu/browser/${CHAT}/status`),
      'browser embed did not discover existing browser status');
    assert.equal(requests.filter(row =>
      row.path.startsWith(`/ocu/terminal/${CHAT}/`) && !row.path.endsWith('/heartbeat')).length,
      terminalBeforeBrowser, 'browser embed mounted the terminal client');
    assert.equal(requests.filter(row => row.path === `/ocu/api/outputs/${CHAT}`).length,
      listingBeforeRuntime, 'browser embed listed Files');
    await page.screenshot({ path: path.join(artifacts, 'embedded-browser-inactive.png') });
    await retireFrame();
    const browserBeforeTerminal = requests.filter(row => row.path.startsWith(`/ocu/browser/${CHAT}/`)).length;
    const beforeTerminalDashboard = requests.length;
    await page.evaluate(() => window.mount('?embed=terminal'));
    await frame().locator('.dash-btn-primary').waitFor();
    assert.equal(await frame().locator('.dash-btn-secondary').count(), 1,
      'terminal embed omitted the upload action');
    assert.equal(await frame().locator('h4').filter({ hasText: 'Uploaded files' }).count(), 0,
      'terminal embed rendered an uploaded-files section');
    assert(requests.slice(beforeTerminalDashboard).some(row =>
      row.path === `/ocu/terminal/${CHAT}/status` && row.header === 'ocu-workspace'),
      'terminal embed missed the status request');
    assert(requests.slice(beforeTerminalDashboard).some(row =>
      row.path === `/ocu/terminal/${CHAT}/sessions` && row.header === 'ocu-workspace'),
      'terminal embed missed the sessions request');
    assert(requests.slice(beforeTerminalDashboard).some(row =>
      row.path === `/ocu/terminal/${CHAT}/processes` && row.header === 'ocu-workspace'),
      'terminal embed missed the processes request');
    assert.equal(requests.slice(beforeTerminalDashboard).filter(row =>
      row.path === `/ocu/api/uploads/${CHAT}/list` ||
      row.path === `/ocu/api/uploads/${CHAT}/manifest`).length, 0,
      'terminal embed issued an upload list or manifest request');
    assert.equal(await frame().locator('.view-tabs').count(), 0, 'terminal embed mounted nested view tabs');
    assert.equal(requests.filter(row => row.path.startsWith(`/ocu/browser/${CHAT}/`)).length,
      browserBeforeTerminal, 'terminal embed mounted the browser client');
    assert.equal(requests.filter(row => row.path === `/ocu/api/outputs/${CHAT}`).length,
      listingBeforeRuntime, 'terminal embed listed Files');
    assert.equal(requests.filter(row => /\/(restart-container|start-ttyd)$/.test(row.path)).length,
      launchBeforeRuntime, 'runtime embedding implicitly launched a sandbox');
    const captureUploadFeedback = async (target, surface, standalone, label, requireAlert = false) => {
      const iframe = standalone ? null : target.locator('#preview');
      const originalStyle = iframe ? await iframe.getAttribute('style') : null;
      try {
        if (iframe) await iframe.evaluate(node => {
          node.style.width = '1000px';
          node.style.height = '700px';
        });
        const alert = surface.locator('.dash-scroll').getByRole('alert');
        if (await alert.isVisible()) await alert.scrollIntoViewIfNeeded();
        if (requireAlert) {
          assert(await alert.isVisible(), `${label} feedback was not visible for capture`);
          assert(await alert.evaluate(node => {
            const bounds = node.getBoundingClientRect();
            const scrollport = node.closest('.dash-scroll').getBoundingClientRect();
            return bounds.top >= Math.max(0, scrollport.top) &&
              bounds.bottom <= Math.min(window.innerHeight, scrollport.bottom) &&
              bounds.left >= Math.max(0, scrollport.left) &&
              bounds.right <= Math.min(window.innerWidth, scrollport.right);
          }), `${label} feedback was clipped inside the captured surface`);
        }
        await target.screenshot({
          path: path.join(artifacts, `${label}.png`), ...(requireAlert ? { fullPage: true } : {}),
        });
      } finally {
        if (iframe) await iframe.evaluate((node, style) => {
          if (style === null) node.removeAttribute('style');
          else node.setAttribute('style', style);
        }, originalStyle);
      }
    };
    const exerciseUploadFailures = async (target, surface, standalone) => {
      const dashboard = surface.locator('.dash-scroll');
      const captureUploadFailure = label => captureUploadFeedback(target, surface, standalone, label);
      const uploadPath = name => `/ocu/api/uploads/${CHAT}/${encodeURIComponent(name)}`;
      const submitted = [];
      const recordSubmission = request => {
        if (request.method() === 'POST' && request.url().startsWith(`${origin}/ocu/api/uploads/${CHAT}/`))
          submitted.push(request);
      };
      const recordStandaloneFailure = request => {
        const fault = uploadFaults.find(candidate => candidate.request === request && candidate.kind === 'network');
        const failure = request.failure()?.errorText || '';
        if (fault && failure === 'net::ERR_CONNECTION_RESET') fault.failure = failure;
        else unexpectedNetworkFailures.push(`${request.url()}: ${failure}`);
      };
      const settle = () => dashboard.evaluate(() => new Promise(resolve =>
        requestAnimationFrame(() => requestAnimationFrame(resolve))));
      const choose = async names => {
        const chooserPromise = target.waitForEvent('filechooser');
        await surface.locator('.dash-btn-secondary').click();
        const chooser = await chooserPromise;
        const input = chooser.element();
        await chooser.setFiles(names.map(name => ({
          name, mimeType: 'text/plain', buffer: Buffer.from(`retained content: ${name}`),
        })));
        return input;
      };
      const waitInputRemoved = input => within(input.evaluate(node => new Promise(resolve => {
        if (!node.isConnected) return resolve();
        const observer = new MutationObserver(() => {
          if (!node.isConnected) {
            observer.disconnect();
            resolve();
          }
        });
        observer.observe(node.ownerDocument, { childList: true, subtree: true });
      })), 'selected upload input removal');
      const dashboardGets = after => requests.slice(after).filter(row =>
        row.method === 'GET' && ['/status', '/sessions', '/processes'].some(suffix =>
          row.path === `/ocu/terminal/${CHAT}${suffix}`));
      const listingGets = after => requests.slice(after).filter(row =>
        row.method === 'GET' && row.path === `/ocu/api/outputs/${CHAT}`);
      const cases = uploadFailureOrder === 'network-first'
        ? ['network', 'http4', 'http5'] : ['http4', 'http5', 'network'];
      target.on('request', recordSubmission);
      if (standalone) target.on('requestfailed', recordStandaloneFailure);
      try {
        for (const kind of cases) {
          const label = `${standalone ? 'standalone' : 'embedded'}-terminal-upload-${kind}`;
          const name = `${label}-<img src=x onerror=window.__uploadCanary=1>.txt`;
          const priorName = `${label}-prior.txt`;
          const laterName = `${label}-later.txt`;
          const partial = kind === 'http5';
          const pathname = uploadPath(name);
          const url = origin + pathname;
          const fault = { kind, url, status: kind === 'http4' ? 413 : 503, request: null, failure: null };
          const names = partial ? [priorName, name, laterName] : [name];
          for (const selected of names) uploadFixtures.set(uploadPath(selected), 200);
          if (kind !== 'network') uploadFixtures.set(pathname, fault.status);
          uploadFaults.push(fault);
          const abortUpload = route => {
            const request = route.request();
            if (request.method() !== 'POST' || fault.request) return route.continue();
            fault.request = request;
            return route.abort('connectionreset');
          };
          if (kind === 'network') await target.route(url, abortUpload);
          const before = requests.length;
          const beforeSubmissions = submitted.length;
          const beforeErrors = consoleErrors.length;
          let input;
          let retryInput;
          try {
            const outcome = kind === 'network'
              ? target.waitForEvent('requestfailed', request =>
                request.url() === url && request.method() === 'POST')
              : target.waitForResponse(response =>
                response.url() === url && response.request().method() === 'POST');
            input = await choose(names);
            const observed = await outcome;
            if (kind === 'network') {
              assert.equal(observed, fault.request, `${label} aborted a different request`);
              assert.equal(fault.failure, 'net::ERR_CONNECTION_RESET', `${label} lacked the injected network failure`);
              assert.equal(requests.slice(before).filter(row => row.path === pathname).length, 0,
                `${label} aborted upload unexpectedly reached the recording server`);
            } else {
              fault.request = observed.request();
              assert.equal(observed.status(), fault.status, `${label} lacked the real HTTP refusal`);
              assert.equal(requests.slice(before).find(row => row.path === pathname)?.status, fault.status);
            }
            const alert = dashboard.getByRole('alert');
            await alert.waitFor({ state: 'visible' });
            const text = await alert.innerText();
            assert(text.includes(name), `${label} feedback did not identify the failed filename`);
            assert(/\bretry\b|\btry again\b|\u043f\u043e\u0432\u0442\u043e\u0440|\u0441\u043d\u043e\u0432\u0430/i.test(text), `${label} feedback omitted retry guidance`);
            await waitInputRemoved(input);
            await settle();
            assert.deepEqual(dashboardGets(before), [], `${label} falsely refreshed the successful dashboard`);
            assert.deepEqual(listingGets(before), [], `${label} falsely called successful Files refresh`);
            assert.deepEqual(submitted.slice(beforeSubmissions).map(request => new URL(request.url()).pathname),
              partial ? [uploadPath(priorName), pathname] : [pathname],
              `${label} retried or attempted a file after the first failure`);
            assert.equal(retainedUploads.has(pathname), false, `${label} accepted the refused upload`);
            const retainedPrior = retainedUploads.get(uploadPath(priorName));
            if (partial) {
              assert(retainedPrior?.includes(Buffer.from(`retained content: ${priorName}`)),
                `${label} lost the earlier successful upload`);
              assert.equal(retainedUploads.has(uploadPath(laterName)), false, `${label} uploaded the later file`);
            }
            assert.equal(await dashboard.locator('img, #upload-body-canary').count(), 0,
              `${label} interpreted the filename or server body as HTML`);
            assert.equal(await dashboard.evaluate(() => window.__uploadCanary), undefined,
              `${label} executed upload error HTML`);
            assert(!text.includes(uploadBodyCanary), `${label} exposed the raw server body`);
            await captureUploadFailure(label);
            const owned = consoleErrors.slice(beforeErrors);
            const nativeDiagnostic = error => error.url === url &&
              (kind === 'network'
                ? error.text === 'Failed to load resource: net::ERR_CONNECTION_RESET' &&
                  fault.failure === 'net::ERR_CONNECTION_RESET'
                : error.text.startsWith('Failed to load resource: the server responded with a status of ') &&
                  error.text.includes(` ${fault.status} `));
            assert.deepEqual(owned.filter(error => !nativeDiagnostic(error)), [],
              `${label} produced unhandled page errors or unrelated console diagnostics`);
            assert(owned.length <= 1, `${label} produced duplicate native diagnostics`);
            expectedOwnedErrors.push(...owned);
            if (kind === 'network') await target.unroute(url, abortUpload);
            uploadFixtures.set(pathname, 200);
            const beforeRetry = requests.length;
            const beforeRetrySubmissions = submitted.length;
            const beforeRetryErrors = consoleErrors.length;
            const retryResponse = target.waitForResponse(response =>
              response.url() === url && response.request().method() === 'POST');
            retryInput = await choose([name]);
            assert.equal((await retryResponse).status(), 200, `${label} retry was not accepted`);
            await waitInputRemoved(retryInput);
            await surface.locator('.dash-btn-secondary').waitFor({ state: 'visible' });
            await alert.waitFor({ state: 'hidden' });
            await waitUntil(() => dashboardGets(beforeRetry).length === 3, `${label} retry dashboard refresh`);
            if (standalone)
              await waitUntil(() => listingGets(beforeRetry).length === 1, `${label} retry Files callback`);
            await settle();
            assert.deepEqual(dashboardGets(beforeRetry).map(row => row.path).sort(),
              ['processes', 'sessions', 'status'].map(endpoint => `/ocu/terminal/${CHAT}/${endpoint}`),
              `${label} retry did not refresh the dashboard once`);
            assert.equal(listingGets(beforeRetry).length, standalone ? 1 : 0,
              `${label} retry violated standalone-only Files refresh`);
            assert.deepEqual(submitted.slice(beforeRetrySubmissions).map(request => new URL(request.url()).pathname),
              [pathname], `${label} successful retry unexpectedly repeated another upload`);
            assert(retainedUploads.get(pathname)?.includes(Buffer.from(`retained content: ${name}`)),
              `${label} successful retry lost its uploaded content`);
            if (partial)
              assert.equal(retainedUploads.get(uploadPath(priorName)), retainedPrior,
                `${label} retry replaced or rolled back the earlier successful upload`);
            assert.deepEqual(consoleErrors.slice(beforeRetryErrors), [], `${label} retry produced browser errors`);
            const uploadCalls = requests.slice(before).filter(row => row.path.startsWith(`/ocu/api/uploads/${CHAT}/`));
            assert(uploadCalls.every(row => row.method === 'POST' && names.some(selected =>
              row.path === uploadPath(selected))), `${label} requested rollback, list or manifest`);
            if (!standalone) {
              assert.equal(await surface.locator('.file-selector-btn, .view-tabs').count(), 0,
                `${label} mounted Files inside terminal embedding`);
              assert.equal(listingGets(before).length, 0, `${label} terminal embedding requested Files`);
            }
            console.log(JSON.stringify({ uploadFeedback: label, status: kind === 'network' ? fault.failure : fault.status,
              failedSelection: partial ? ['prior-success', 'failed', 'later-unattempted'] : ['failed'],
              retry: 'ok' }));
          } catch (error) {
            await captureUploadFailure(`fail-${label}`);
            console.error(JSON.stringify({ uploadFeedback: label, requests: requests.slice(before),
              errors: consoleErrors.slice(beforeErrors), failure: fault.failure,
              observedStatus: kind === 'network' ? null :
                requests.slice(before).find(row => row.path === pathname)?.status }));
            throw error;
          } finally {
            if (kind === 'network') await target.unroute(url, abortUpload);
            if (input) await input.dispose();
            if (retryInput) await retryInput.dispose();
            for (const selected of names) {
              uploadFixtures.delete(uploadPath(selected));
              retainedUploads.delete(uploadPath(selected));
            }
          }
        }
      } finally {
        target.off('request', recordSubmission);
        if (standalone) target.off('requestfailed', recordStandaloneFailure);
      }
    };
    const exerciseDiscoveryWarning = async (target, surface, standalone) => {
      if (discoverySurface !== 'both' && discoverySurface !== (standalone ? 'standalone' : 'embedded'))
        return;
      const dashboard = surface.locator('.dash-scroll');
      const label = `${standalone ? 'standalone' : 'embedded'}-stored-discovery-failed`;
      const name = `${label}-{reason}-$&-<img src=x onerror=window.__uploadCanary=1>.txt`;
      const storedName = `${label}-{reason}-$&-<img src=x onerror=window.__uploadCanary=1>-collision-1.txt`;
      const later = `${label}-later.txt`;
      const pathname = `/ocu/api/uploads/${CHAT}/${encodeURIComponent(name)}`;
      const laterPath = `/ocu/api/uploads/${CHAT}/${encodeURIComponent(later)}`;
      const before = requests.length;
      let input;
      try {
        uploadFixtures.set(pathname, 200);
        uploadFixtures.set(laterPath, 200);
        uploadDiscoveryFixtures.set(pathname, { reason: 'file_limit', filename: storedName });
        const chooserPromise = target.waitForEvent('filechooser');
        await surface.locator('.dash-btn-secondary').click();
        const chooser = await chooserPromise;
        input = chooser.element();
        const responsePromise = target.waitForResponse(response =>
          response.url() === origin + pathname && response.request().method() === 'POST');
        await chooser.setFiles([name, later].map(filename => ({
          name: filename, mimeType: 'text/plain', buffer: Buffer.from(`stored: ${filename}`),
        })));
        const response = await responsePromise;
        assert.equal(response.status(), 200);
        const payload = await response.json();
        assert.equal(payload.filename, storedName, `${label} response omitted the actual stored filename`);
        assert.notEqual(payload.filename, name, `${label} response did not distinguish the collision filename`);
        assert.deepEqual(payload.discovery, { status: 'failed', reason: 'file_limit' });
        await waitUntil(() => requests.slice(before).some(row =>
          row.method === 'GET' && row.path === `/ocu/terminal/${CHAT}/status`),
          `${label} completed dashboard refresh`);
        await surface.locator('.dash-btn-secondary').waitFor({ state: 'visible' });
        await dashboard.evaluate(() => new Promise(resolve =>
          requestAnimationFrame(() => requestAnimationFrame(resolve))));
        const alert = dashboard.getByRole('alert');
        assert.equal(await alert.count(), 1, `${label} omitted the stored-but-discovery-failed warning`);
        const text = await alert.innerText();
        assert(text.includes(storedName), `${label} warning omitted the actual stored filename`);
        assert(!text.includes(name), `${label} warning reported the requested filename instead of the stored filename`);
        assert(/stored|saved|\u0441\u043e\u0445\u0440\u0430\u043d/i.test(text), `${label} falsely reported a file-save failure`);
        assert(/discovery|\u043e\u0431\u043d\u0430\u0440\u0443\u0436|\u0440\u0435\u0441\u0443\u0440\u0441/i.test(text), `${label} omitted the distinct discovery failure`);
        assert(text.includes('file_limit'), `${label} omitted the stable discovery reason`);
        assert(!/retry|try again|\u043f\u043e\u0432\u0442\u043e\u0440|\u0441\u043d\u043e\u0432\u0430/i.test(text), `${label} invited duplicate publication`);
        assert.equal(await alert.locator('button, a, img').count(), 0,
          `${label} offered re-upload or interpreted the filename as HTML`);
        assert.equal(await dashboard.evaluate(() => window.__uploadCanary), undefined,
          `${label} executed filename HTML`);
        assert.equal(await input.evaluate(node => node.isConnected), false,
          `${label} retained the selected file input`);
        assert.deepEqual(requests.slice(before).filter(row =>
          row.method === 'POST' && row.path.startsWith(`/ocu/api/uploads/${CHAT}/`))
          .map(row => row.path), [pathname, laterPath],
          `${label} retried a committed upload or abandoned the remaining selection`);
        assert(retainedUploads.get(pathname)?.includes(Buffer.from(`stored: ${name}`)));
        assert(retainedUploads.get(laterPath)?.includes(Buffer.from(`stored: ${later}`)));
        assert.equal(requests.slice(before).filter(row =>
          row.method === 'GET' && row.path === `/ocu/api/outputs/${CHAT}`).length,
          standalone ? 1 : 0, `${label} violated the standalone-only Files refresh`);
        await captureUploadFeedback(target, surface, standalone, label, true);
        console.log(JSON.stringify({ uploadFeedback: label, stored: true,
          requestedFilename: name, storedFilename: payload.filename,
          discovery: 'file_limit', publicationRequests: 2, retry: false }));
      } finally {
        if (input) await input.dispose();
        for (const uploadPath of [pathname, laterPath]) {
          uploadFixtures.delete(uploadPath);
          uploadDiscoveryFixtures.delete(uploadPath);
          retainedUploads.delete(uploadPath);
        }
      }
    };
    await page.screenshot({ path: path.join(artifacts, 'embedded-terminal-dashboard.png') });
    const beforeEmbedUpload = requests.length;
    const beforeEmbedUploadErrors = consoleErrors.length;
    const embedChooserPromise = page.waitForEvent('filechooser');
    await frame().locator('.dash-btn-secondary').click();
    const embedChooser = await embedChooserPromise;
    await embedChooser.setFiles({ name: 'note.txt', mimeType: 'text/plain', buffer: Buffer.from('hello') });
    await waitUntil(() => requests.slice(beforeEmbedUpload).some(row =>
      row.method === 'POST' && row.path === `/ocu/api/uploads/${CHAT}/note.txt`),
      'terminal embed upload POST');
    await waitUntil(() => requests.slice(beforeEmbedUpload).some(row =>
      row.method === 'GET' && row.path === `/ocu/terminal/${CHAT}/status`),
      'terminal embed upload dashboard refresh');
    await frame().locator('.dash-btn-secondary').waitFor();
    const afterEmbedUpload = requests.slice(beforeEmbedUpload);
    assert.equal(await frame().locator('.dash-btn-secondary').count(), 1,
      'terminal embed upload dropped the upload action');
    assert.equal(await frame().locator('h4').filter({ hasText: 'Uploaded files' }).count(), 0,
      'terminal embed upload rendered an uploaded-files section');
    assert.equal(afterEmbedUpload.filter(row =>
      row.path === `/ocu/api/uploads/${CHAT}/list` ||
      row.path === `/ocu/api/uploads/${CHAT}/manifest`).length, 0,
      'terminal embed upload issued an upload list or manifest request');
    assert.equal(requests.filter(row => row.path === `/ocu/api/outputs/${CHAT}`).length,
      listingBeforeRuntime, 'terminal embed upload listed Files');
    assert.deepEqual(consoleErrors.slice(beforeEmbedUploadErrors), [],
      'terminal embed upload produced unexpected console errors');
    await exerciseUploadFailures(page, frame(), false);
    await exerciseDiscoveryWarning(page, frame(), false);
    await retireFrame();
    const runtimeCalls = () => requests.filter(row =>
      /\/(?:browser|terminal)\/|\/api\/uploads\//.test(row.path)).length;
    const beforeInvalid = runtimeCalls();
    await page.evaluate(() => window.mount('?embed=browser&embed=browser'));
    await frame().locator('[role="alert"]').getByText('Invalid preview embedding').waitFor();
    await retireFrame();
    const unframed = await context.newPage();
    try {
      await unframed.goto(`${origin}/ocu/preview/${CHAT}?embed=browser`);
      await unframed.locator('[role="alert"]').getByText('Invalid preview embedding').waitFor();
    } finally {
      await unframed.close();
    }
    assert.equal(runtimeCalls(), beforeInvalid, 'invalid runtime embedding issued a runtime request');

    for (const state of ['failure', 'malformed']) {
      browserFixture = state;
      const beforeConnections = activeSockets.size;
      await page.evaluate(() => window.mount('?embed=browser'));
      await frame().getByRole('alert').getByText('Browser unavailable').waitFor();
      assert.equal(activeSockets.size, beforeConnections, 'unavailable Browser started a connection');
      await retireFrame();
    }
    browserFixture = 'fault';
    const beforeBrowserFault = requests.length;
    const socketsBeforeBrowserFault = activeSockets.size;
    await page.evaluate(() => window.mount('?embed=browser'));
    const browserFaultAlert = frame().getByRole('alert');
    await browserFaultAlert.getByText('gateway_publication_unassigned').waitFor();
    const browserFaultText = await browserFaultAlert.innerText();
    assert(browserFaultText.includes('9222/tcp'), 'embedded Browser fault omitted the detail');
    assert(browserFaultText.includes('Browser port not published by the host engine'),
      'embedded Browser fault omitted the title');
    assert.equal(await frame().locator('.runtime-fault-detail').count(), 1,
      'embedded Browser fault did not render one detail line');
    assert.equal(await frame().getByText('Browser unavailable').count(), 0,
      'embedded Browser fault rendered the unavailable alert');
    assert.equal(await frame().getByText('Shared AI Browser').count(), 0,
      'embedded Browser fault rendered the empty state');
    assert.equal(await frame().locator('.browser-connecting, canvas').count(), 0,
      'embedded Browser fault mounted the viewer');
    assert.equal(requests.slice(beforeBrowserFault).filter(row =>
      row.path === `/ocu/browser/${CHAT}/json`).length, 0, 'embedded Browser fault requested pages');
    assert.equal(activeSockets.size, socketsBeforeBrowserFault, 'embedded Browser fault started a connection');
    await page.screenshot({ path: path.join(artifacts, 'embedded-browser-fault.png'), animations: 'disabled' });
    await retireFrame();
    browserFixture = 'inactive';
    await page.evaluate(() => window.mount('?embed=browser'));
    await frame().getByText('Shared AI Browser').waitFor();
    assert.equal(await frame().locator('.runtime-fault').count(), 0,
      'inactive Browser without a reason rendered the fault panel');
    assert.equal(await frame().getByRole('alert').count(), 0,
      'inactive Browser without a reason rendered an alert');
    await retireFrame();

    browserFixture = 'held';
    await page.evaluate(() => window.mount('?embed=browser'));
    await waitUntil(() => heldBrowserStatuses.length === 1, 'held browser status');
    await new Promise(resolve => setTimeout(resolve, 3100));
    assert.equal(heldBrowserStatuses.length, 1, 'overlapping browser status polls');
    const beforeLateStatus = requests.length;
    await retireFrame();
    browserFixture = 'active';
    for (const response of heldBrowserStatuses.splice(0))
      respond(response, 200, '{"active":true,"pages":[{"id":"fixture-page","type":"page"}]}');
    await new Promise(resolve => setTimeout(resolve, 50));
    assert.equal(requests.slice(beforeLateStatus).filter(row => row.path.endsWith('/json')).length, 0,
      'retired Browser created a viewer after late status');

    browserPagesHeld = true;
    await page.evaluate(() => window.mount('?embed=browser'));
    await waitUntil(() => heldBrowserPages.length === 1, 'held browser pages');
    await retireFrame();
    browserPagesHeld = false;
    for (const response of heldBrowserPages.splice(0))
      respond(response, 200, '[{"id":"fixture-page","type":"page"}]');
    await new Promise(resolve => setTimeout(resolve, 50));
    assert.equal(activeSockets.size, 0, 'retired Browser connected after late page discovery');

    upgradesHeld = true;
    const browserSocketsBeforeHold = observedBrowserSockets.length;
    await page.evaluate(() => window.mount('?embed=browser'));
    await waitUntil(() => heldUpgrades.length === 1, 'held browser WebSocket');
    const heldBrowserSocket = heldUpgrades[0].socket;
    await retireFrame();
    await waitUntil(() => heldBrowserSocket.readableEnded || heldBrowserSocket.destroyed,
      'retired Browser WebSocket peer FIN');
    if (observedBrowserSockets.length > browserSocketsBeforeHold) {
      await waitUntil(() => observedBrowserSockets.slice(browserSocketsBeforeHold).some(item => {
        if (item.closed) return true;
        try { return typeof item.socket.isClosed === 'function' && item.socket.isClosed(); }
        catch { return false; }
      }), 'browser WebSocket client close');
    }
    upgradesHeld = false;
    for (const upgrade of heldUpgrades.splice(0)) acceptUpgrade(upgrade);
    await waitUntil(() => heldBrowserSocket.destroyed, 'held browser transport close');
    assert.equal(activeSockets.size, 0, 'retired Browser retained an accepted WebSocket');

    terminalFixture = 'fault';
    const beforeTerminalFault = requests.length;
    const upgradesBeforeTerminalFault = upgradeAttempts.length;
    await page.evaluate(() => window.mount('?embed=terminal'));
    await frame().locator('.dash-btn-primary').click();
    await frame().getByRole('alert').getByText('gateway_publication_unassigned').waitFor();
    const terminalFaultText = await frame().getByRole('alert').innerText();
    assert(terminalFaultText.includes('9222/tcp'), 'terminal fault omitted the detail');
    assert(terminalFaultText.includes('Terminal port not published by the host engine'),
      'terminal fault omitted the title');
    assert.equal(requests.slice(beforeTerminalFault).filter(row => row.method === 'POST' &&
      row.path === `/ocu/terminal/${CHAT}/start-ttyd`).length, 0, 'terminal fault requested a ttyd start');
    assert.equal(upgradeAttempts.length, upgradesBeforeTerminalFault, 'terminal fault opened a WebSocket');
    assert.equal(activeSockets.size, 0, 'terminal fault retained a WebSocket');
    assert.equal(await frame().getByRole('alert').locator('.dash-btn-primary').count(), 0,
      'terminal fault offered a recover action');
    assert.equal(await frame().getByRole('alert').locator('button.btn').count(), 1,
      'terminal fault dropped the Back button');
    assert.equal(await frame().getByText('[Session ended]').count(), 0, 'terminal fault ended a session');
    await page.screenshot({ path: path.join(artifacts, 'embedded-terminal-fault.png'), animations: 'disabled' });
    await retireFrame();
    terminalFixture = 'inactive';

    terminalFixture = 'stopped';
    const beforeTerminalStopped = requests.length;
    await page.evaluate(() => window.mount('?embed=terminal'));
    await frame().locator('.dash-btn-primary').click();
    await frame().getByText('Container stopped').waitFor();
    assert.equal(await frame().getByRole('alert').count(), 0, 'stopped container rendered a fault alert');
    assert.equal(await frame().locator('.dash-btn-primary').count(), 1,
      'stopped container dropped the Restart button');
    assert(requests.slice(beforeTerminalStopped).some(row => row.method === 'POST' &&
      row.path === `/ocu/terminal/${CHAT}/start-ttyd` && row.status === 503),
    'stopped container did not fail its ttyd start');
    terminalFixture = 'fault';
    const beforeTerminalRecovery = requests.length;
    const upgradesBeforeTerminalRecovery = upgradeAttempts.length;
    await frame().locator('.dash-btn-primary').click();
    await frame().getByRole('alert').getByText('gateway_publication_unassigned').waitFor();
    assert.equal(upgradeAttempts.length, upgradesBeforeTerminalRecovery,
      'recovered terminal fault opened a WebSocket');
    assert.equal(activeSockets.size, 0, 'recovered terminal fault retained a WebSocket');
    assert.equal(requests.slice(beforeTerminalRecovery).filter(row => row.method === 'POST' &&
      row.path === `/ocu/terminal/${CHAT}/restart-container`).length, 1,
    'terminal recovery did not restart the container once');
    await retireFrame();
    terminalFixture = 'inactive';

    terminalFixture = 'status-error';
    const beforeTerminalStatusError = requests.length;
    const messagesBeforeTerminalStatusError = socketMessages.length;
    await page.evaluate(() => window.mount('?embed=terminal'));
    await frame().locator('.dash-btn-primary').click();
    await waitUntil(() => socketMessages.slice(messagesBeforeTerminalStatusError).some(row =>
      row.path === `/ocu/terminal/${CHAT}/ws` && row.method === 'ttyd-init') &&
      activeSockets.size === 1, 'terminal start after a failed status read');
    assert(requests.slice(beforeTerminalStatusError).some(row => row.method === 'GET' &&
      row.path === `/ocu/terminal/${CHAT}/status` && row.status === 503),
    'terminal status read did not fail');
    assert(requests.slice(beforeTerminalStatusError).some(row => row.method === 'POST' &&
      row.path === `/ocu/terminal/${CHAT}/start-ttyd`),
    'failed terminal status read blocked the ttyd start');
    assert.equal(await frame().getByRole('alert').count(), 0, 'failed terminal status read rendered an alert');
    assert.equal(await frame().locator('.runtime-fault, .runtime-fault-detail').count(), 0,
      'failed terminal status read rendered the fault panel');
    await retireFrame();
    await waitUntil(() => activeSockets.size === 0, 'terminal status-error socket cleanup');
    terminalFixture = 'inactive';
    const statusErrorStart = requests.slice(beforeTerminalStatusError).find(row => row.method === 'POST' &&
      row.path === `/ocu/terminal/${CHAT}/start-ttyd`);
    assert.deepEqual(startBody(statusErrorStart), { dangerous_mode: false },
      'terminal embed without a model parameter sent a model');

    const beforeModelStart = requests.length;
    const messagesBeforeModelStart = socketMessages.length;
    await page.evaluate(() => window.mount('?embed=terminal&model=m-1'));
    await frame().locator('.dash-btn-primary').click();
    await waitUntil(() => socketMessages.slice(messagesBeforeModelStart).some(row =>
      row.path === `/ocu/terminal/${CHAT}/ws` && row.method === 'ttyd-init') &&
      activeSockets.size === 1, 'terminal start with a model');
    const modelStarts = requests.slice(beforeModelStart).filter(row => row.method === 'POST' &&
      row.path === `/ocu/terminal/${CHAT}/start-ttyd`);
    assert.deepEqual(modelStarts.map(startBody), [{ dangerous_mode: false, model: 'm-1' }],
      'terminal start omitted the model parameter');
    await retireFrame();
    await waitUntil(() => activeSockets.size === 0, 'terminal model socket cleanup');

    terminalFixture = 'stopped';
    const beforeModelRecovery = requests.length;
    const messagesBeforeModelRecovery = socketMessages.length;
    await page.evaluate(() => window.mount('?embed=terminal&model=m-1'));
    await frame().locator('.dash-btn-primary').click();
    await frame().getByText('Container stopped').waitFor();
    terminalFixture = 'inactive';
    await frame().locator('.dash-btn-primary').click();
    await waitUntil(() => socketMessages.slice(messagesBeforeModelRecovery).some(row =>
      row.path === `/ocu/terminal/${CHAT}/ws` && row.method === 'ttyd-init') &&
      activeSockets.size === 1, 'recovered terminal start with a model');
    assert.deepEqual(requests.slice(beforeModelRecovery).filter(row => row.method === 'POST' &&
      row.path === `/ocu/terminal/${CHAT}/start-ttyd`).map(startBody),
    [{ dangerous_mode: false, model: 'm-1' }, { dangerous_mode: false, model: 'm-1' }],
    'terminal recovery omitted the model parameter');
    await retireFrame();
    await waitUntil(() => activeSockets.size === 0, 'recovered model terminal socket cleanup');

    terminalFixture = 'refused';
    const beforeRefusal = requests.length;
    const upgradesBeforeRefusal = upgradeAttempts.length;
    await page.evaluate(() => window.mount('?embed=terminal'));
    await frame().locator('.dash-btn-primary').click();
    await frame().getByRole('alert').getByText(`Terminal not started: ${GATEWAY_DETAIL}`).waitFor();
    await new Promise(resolve => setTimeout(resolve, 300));
    const refusalRequests = requests.slice(beforeRefusal);
    const refusalIndex = refusalRequests.findIndex(row => row.method === 'POST' &&
      row.path === `/ocu/terminal/${CHAT}/start-ttyd`);
    assert(refusalIndex >= 0 && refusalRequests[refusalIndex].status === 422, 'gateway refusal was not a 422');
    assert.deepEqual(startBody(refusalRequests[refusalIndex]), { dangerous_mode: false });
    assert.equal(refusalRequests.slice(refusalIndex + 1).filter(row =>
      row.path === `/ocu/terminal/${CHAT}/status` ||
      row.path === `/ocu/terminal/${CHAT}/restart-container`).length, 0,
    'gateway refusal probed the container status or restarted it');
    assert.equal(upgradeAttempts.length, upgradesBeforeRefusal, 'gateway refusal opened a WebSocket');
    assert.equal(await frame().locator('.dash-btn-primary').count(), 0, 'gateway refusal offered a recover action');
    assert.equal(await frame().getByText('Container stopped').count(), 0);
    await page.screenshot({ path: path.join(artifacts, 'embedded-terminal-gateway-refusal.png'), animations: 'disabled' });
    await retireFrame();

    terminalFixture = 'refused-nonstring';
    const beforeOpaqueRefusal = requests.length;
    await page.evaluate(() => window.mount('?embed=terminal&model=m-1'));
    await frame().locator('.dash-btn-primary').click();
    await frame().getByText('Failed to start terminal (container not found)').waitFor();
    await new Promise(resolve => setTimeout(resolve, 300));
    assert.equal(await frame().getByText(GATEWAY_DETAIL, { exact: false }).count(), 0,
      'non-string refusal detail reached the page');
    const opaqueRequests = requests.slice(beforeOpaqueRefusal);
    const opaqueIndex = opaqueRequests.findIndex(row => row.method === 'POST' &&
      row.path === `/ocu/terminal/${CHAT}/start-ttyd`);
    assert(opaqueIndex >= 0 && opaqueRequests[opaqueIndex].status === 422);
    assert.equal(opaqueRequests.slice(opaqueIndex + 1).filter(row =>
      row.path === `/ocu/terminal/${CHAT}/status` ||
      row.path === `/ocu/terminal/${CHAT}/restart-container`).length, 0,
    'non-string refusal probed the container status or restarted it');
    await retireFrame();

    terminalFixture = 'stopped';
    await page.evaluate(() => window.mount('?embed=terminal&model=m-1'));
    await frame().locator('.dash-btn-primary').click();
    await frame().getByText('Container stopped').waitFor();
    terminalFixture = 'refused';
    const beforeRecoveryRefusal = requests.length;
    const upgradesBeforeRecoveryRefusal = upgradeAttempts.length;
    await frame().locator('.dash-btn-primary').click();
    await frame().getByRole('alert').getByText(`Terminal not started: ${GATEWAY_DETAIL}`).waitFor();
    await new Promise(resolve => setTimeout(resolve, 300));
    assert.equal(await frame().getByText('Failed to restore container').count(), 0,
      'recovery refusal showed the generic restore failure');
    const recoveryRequests = requests.slice(beforeRecoveryRefusal);
    assert.equal(recoveryRequests.filter(row => row.method === 'POST' &&
      row.path === `/ocu/terminal/${CHAT}/restart-container`).length, 1);
    const recoveryStart = recoveryRequests.findIndex(row => row.method === 'POST' &&
      row.path === `/ocu/terminal/${CHAT}/start-ttyd`);
    assert(recoveryStart >= 0 && recoveryRequests[recoveryStart].status === 422);
    assert.deepEqual(startBody(recoveryRequests[recoveryStart]), { dangerous_mode: false, model: 'm-1' });
    assert.equal(recoveryRequests.slice(recoveryStart + 1).filter(row =>
      row.path === `/ocu/terminal/${CHAT}/status`).length, 0, 'recovery refusal probed the container status');
    assert.equal(upgradeAttempts.length, upgradesBeforeRecoveryRefusal, 'recovery refusal opened a WebSocket');
    await retireFrame();
    terminalFixture = 'inactive';
    console.log(JSON.stringify({ terminalGateway: 'ok', model: true, recoveryModel: true, refusal: true,
      opaqueRefusal: true, recoveryRefusal: true }));

    upgradesHeld = true;
    await page.evaluate(() => window.mount('?embed=terminal'));
    await frame().locator('.dash-btn-primary').click();
    await waitUntil(() => heldUpgrades.length === 1, 'held ttyd WebSocket');
    const heldTtydSocket = heldUpgrades[0].socket;
    await retireFrame();
    await waitUntil(() => heldTtydSocket.readableEnded || heldTtydSocket.destroyed,
      'retired ttyd WebSocket peer FIN');
    upgradesHeld = false;
    for (const upgrade of heldUpgrades.splice(0)) acceptUpgrade(upgrade);
    await waitUntil(() => heldTtydSocket.destroyed, 'held ttyd transport close');
    assert.equal(activeSockets.size, 0, 'retired Terminal retained an accepted WebSocket');

    ttydSendsData = false;
    const beforeTtydMessages = socketMessages.length;
    await page.evaluate(() => window.mount('?embed=terminal'));
    await frame().locator('.dash-btn-primary').click();
    await waitUntil(() => socketMessages.slice(beforeTtydMessages).some(row =>
      row.path === `/ocu/terminal/${CHAT}/ws` && row.method === 'ttyd-init') &&
      activeSockets.size === 1, 'ttyd reconnect setup');
    sendWsFrame([...activeSockets][0], 8, Buffer.alloc(0));
    await waitUntil(() => activeSockets.size === 0, 'ttyd remote close');
    const beforeBackoff = upgradeAttempts.length;
    await page.waitForFunction(() =>
      [...document.querySelector('#preview').contentWindow.__runtimeTimeouts.values()].includes(1000),
    null, { timeout: 10000 });
    await frame().locator('.terminal-toolbar .terminal-btn').first().click();
    await frame().locator('.dash-btn-primary').waitFor();
    assert.equal(await frame().locator('#app').evaluate(() =>
      [...window.__runtimeTimeouts.values()].includes(1000)), false,
    'TerminalSession retained reconnect timer after component unmount');
    await new Promise(resolve => setTimeout(resolve, 1200));
    assert.equal(upgradeAttempts.length, beforeBackoff,
      'unmounted ttyd reconnect backoff created a new WebSocket');
    await retireFrame();
    const beforeExhaustion = socketMessages.length;
    await page.evaluate(() => window.mount('?embed=terminal'));
    await frame().locator('.dash-btn-primary').click();
    await waitUntil(() => socketMessages.slice(beforeExhaustion).some(row =>
      row.path === `/ocu/terminal/${CHAT}/ws` && row.method === 'ttyd-init'),
    'terminal exhausted reconnect setup');
    for (let attempt = 0; attempt < 4; attempt++) {
      const seen = socketMessages.length;
      assert.equal(activeSockets.size, 1, 'terminal reconnect opened duplicate sockets');
      sendWsFrame([...activeSockets][0], 8, Buffer.alloc(0));
      await waitUntil(() => activeSockets.size === 0, `terminal remote close ${attempt}`);
      if (attempt < 3) await waitUntil(() => socketMessages.slice(seen).some(row =>
        row.path === `/ocu/terminal/${CHAT}/ws` && row.method === 'ttyd-init') &&
        activeSockets.size === 1, `terminal reconnect ${attempt}`);
    }
    await page.waitForFunction(() =>
      [...document.querySelector('#preview').contentWindow.__runtimeTimeouts.values()].includes(2000),
    null, { timeout: 10000 });
    const beforeBack = upgradeAttempts.length;
    await frame().locator('.terminal-toolbar .terminal-btn').first().click();
    await frame().locator('.dash-btn-primary').waitFor();
    assert.equal(await frame().locator('#app').evaluate(() =>
      [...window.__runtimeTimeouts.values()].includes(2000)), false,
    'TerminalSession retained onBack timer after component unmount');
    await new Promise(resolve => setTimeout(resolve, 2200));
    assert.equal(upgradeAttempts.length, beforeBack, 'terminal created a socket after onBack cleanup');
    await retireFrame();
    ttydSendsData = true;

    const allListingBeforeCycles = requests.filter(row => row.path.includes(`/api/outputs/${CHAT}`)).length;
    for (let cycle = 0; cycle < 20; cycle++) {
      const prefix = cycle % 2 ? '/tools/ocu' : '/ocu';
      for (const mode of ['browser', 'terminal']) {
        const beforeMessages = socketMessages.length;
        const beforeRequests = requests.length;
        await page.evaluate(({ mode, prefix }) => window.mount(`?embed=${mode}`, prefix), { mode, prefix });
        if (mode === 'browser') {
          await waitUntil(() => socketMessages.slice(beforeMessages).some(row =>
            row.path === `${prefix}/browser/${CHAT}/devtools/page/fixture-page` &&
            row.method === 'Page.enable'), `Browser CDP cycle ${cycle}`);
          assert.equal(await frame().locator('.browser-panel canvas').count(), 1);
        } else {
          await frame().locator('.dash-btn-primary').waitFor();
          await frame().locator('.dash-btn-primary').click();
          await waitUntil(() => socketMessages.slice(beforeMessages).some(row =>
            row.path === `${prefix}/terminal/${CHAT}/ws` && row.method === 'ttyd-init'),
          `Terminal ttyd cycle ${cycle}`);
          assert.equal(await frame().locator('.terminal-view').count(), 1);
        }
        const cycleRequests = requests.slice(beforeRequests);
        const required = mode === 'browser'
          ? [`${prefix}/browser/${CHAT}/status`, `${prefix}/browser/${CHAT}/json`]
          : [`${prefix}/terminal/${CHAT}/status`, `${prefix}/terminal/${CHAT}/sessions`,
            `${prefix}/terminal/${CHAT}/processes`, `${prefix}/terminal/${CHAT}/start-ttyd`];
        for (const path of required)
          assert(cycleRequests.some(row => row.path === path && row.header === 'ocu-workspace'),
            `${mode} cycle ${cycle} missed the prefixed workspace request ${path}`);
        if (mode === 'terminal') {
          assert.equal(cycleRequests.filter(row =>
            row.path === `${prefix}/api/uploads/${CHAT}/list` ||
            row.path === `${prefix}/api/uploads/${CHAT}/manifest`).length, 0,
            `terminal cycle ${cycle} issued an upload list or manifest request`);
        }
        const other = mode === 'browser' ? '/terminal/' : '/browser/';
        assert.equal(cycleRequests.filter(row =>
          row.path.includes(`${prefix}${other}${CHAT}/`) && !row.path.endsWith('/heartbeat')).length, 0,
        `${mode} cycle ${cycle} mounted the unselected runtime`);
        assert.equal(await frame().locator('#app').evaluate(() => window.__heartbeatTimers.size), 1,
          `${mode} embed has multiple or missing heartbeat timers`);
        assert.equal(activeSockets.size, 1, `${mode} cycle ${cycle} has duplicate connections`);
        assert.equal(requests.filter(row => row.path.includes(`/api/outputs/${CHAT}`)).length,
          allListingBeforeCycles, `${mode} embed listed Files`);
        await retireFrame();
        await waitUntil(() => activeSockets.size === 0, `${mode} cycle ${cycle} socket cleanup`);
      }
    }
    const requestsAtRetirement = requests.length;
    await new Promise(resolve => setTimeout(resolve, 3200));
    assert.equal(requests.length, requestsAtRetirement,
      'retired runtime scheduled a late status, heartbeat or tab-poll request');
    assert.equal(activeSockets.size, 0);
    assert.equal(heldBrowserStatuses.length + heldBrowserPages.length + heldUpgrades.length, 0,
      'retired fixture retained a pending completion');
    browserFixture = 'inactive';
    listMode = 'standalone';

    const attachDrawioConsole = (target) => {
      target.on('pageerror', error => consoleErrors.push({ text: String(error), url: '' }));
      target.on('console', message => {
        if (message.type() !== 'error') return;
        consoleErrors.push({ text: message.text(), url: message.location().url });
      });
    };
    const selectStandaloneFile = async (target, name) => {
      await target.locator('.file-selector-btn').click();
      for (const folder of name.split('/').slice(0, -1))
        await target.locator('.dropdown-menu.open .item-name').getByText(folder, { exact: true }).click();
      await target.locator('.dropdown-menu.open .item-name').getByText(name, { exact: true }).click();
    };
    const openStandalone = async (prefix, options = {}) => {
      const isolated = await browser.newContext();
      isolated.setDefaultTimeout(10000);
      await isolated.addInitScript(freezePollingClock);
      await isolated.route('**/*', route => {
        const url = route.request().url();
        if (options.images && /^https?:\/\/images\.example\.test\/pixel\.png$/.test(url))
          return route.fulfill({ status: 200, contentType: 'image/png', body: bytes['guides/pixel.png'] });
        if (options.sanitizerRoute && url.endsWith('/static/purify.min.js'))
          return options.sanitizerRoute(route);
        if (/^https?:/.test(url) && !url.startsWith(origin + '/')) {
          externalRequests.push(url);
          return route.abort();
        }
        return route.continue();
      });
      const page = await isolated.newPage();
      attachDrawioConsole(page);
      const before = requests.length;
      const beforeErrors = consoleErrors.length;
      standaloneFirstFile = options.firstFile || null;
      try {
        await page.goto(`${origin}${prefix}/preview/${CHAT}${options.query || ''}`, {
          waitUntil: options.sanitizerRoute ? 'domcontentloaded' : 'load',
        });
        await page.locator('.file-selector-btn').waitFor();
      } finally { standaloneFirstFile = null; }
      await page.evaluate(() => window.__freezePollingClock());
      await page.locator('.file-selector-btn').waitFor();
      return { isolated, page, before, beforeErrors, prefix };
    };
    const captureStandaloneFailure = async (session, label) => {
      try {
        const html = await session.page.locator('.preview-stage, body').first().evaluate((node) => node.innerHTML);
        await session.page.screenshot({ path: path.join(artifacts, `fail-${label}.png`) });
        console.error(JSON.stringify({
          fail: label,
          requests: caseRequests(session),
          errors: caseErrors(session),
          html,
        }));
      } catch (error) {
        console.error(JSON.stringify({ fail: label, captureError: String(error) }));
      }
    };
    const closeStandalone = async (session) => {
      await session.page.close();
      await session.isolated.close();
    };
    let localShapeGeometryCapture = 0;
    const assertLocalShapeGeometry = async (session, {
      scaled = false, surface = '.drawio-host', phase = 'native', transition,
    } = {}) => {
      const label = `drawio-geometry-${session.prefix.replaceAll('/', '-') || 'root'}-${++localShapeGeometryCapture}-${phase}`;
      // A quarter CSS pixel tolerates subpixel rounding only, not a clipped stroke.
      const tolerance = 0.25;
      const evidence = { case: label, tolerance, url: session.page.url(), scaled, surface, transition };
      let failure;
      try {
        let transitionFailure;
        if (transition) {
          try {
            await session.page.waitForFunction(({ surface, transition, tolerance }) => {
              const shapes = [...document.querySelectorAll(`${surface} svg rect`)]
                .filter(node => node.rx.baseVal.value > 0 && node.ry.baseVal.value > 0);
              if (shapes.length !== 1) return false;
              const box = shapes[0].getBoundingClientRect();
              if (transition.target)
                return ['width', 'height'].every(axis =>
                  Math.abs(box[axis] - transition.target[axis]) <= tolerance);
              return ['width', 'height'].every(axis => transition.direction === 'larger' ?
                box[axis] > transition.before[axis] + tolerance :
                box[axis] < transition.before[axis] - tolerance);
            }, { surface, transition, tolerance }, { timeout: 10000 });
          } catch (error) {
            // Still measure and capture the unchanged surface when a control fails.
            transitionFailure = error;
          }
        }
        evidence.geometry = await session.page.locator(`${surface} svg`).evaluate((svg, scaled) => {
          const nodes = [...svg.querySelectorAll('path,rect')];
          const boxes = nodes.map(node => {
            const box = node.getBBox();
            return { width: Math.round(box.width), height: Math.round(box.height) };
          });
          const describe = box => ({ left: box.left, top: box.top, right: box.right,
            bottom: box.bottom, width: box.width, height: box.height });
          const viewport = { left: 0, top: 0, right: innerWidth, bottom: innerHeight };
          const shapes = nodes.flatMap((node, index) => {
            if (node.localName !== 'rect') return [];
            const box = node.getBBox();
            if (!scaled) {
              if (box.x !== 40 || box.y !== 40 || box.width !== 120 || box.height !== 60) return [];
            } else if (!(node.rx.baseVal.value > 0 && node.ry.baseVal.value > 0)) return [];
            const style = getComputedStyle(node);
            const matrix = node.getScreenCTM();
            const strokeWidth = parseFloat(style.strokeWidth);
            const halfStroke = strokeWidth / 2;
            const points = matrix ? [
              [box.x - halfStroke, box.y - halfStroke],
              [box.x + box.width + halfStroke, box.y - halfStroke],
              [box.x - halfStroke, box.y + box.height + halfStroke],
              [box.x + box.width + halfStroke, box.y + box.height + halfStroke],
            ].map(([x, y]) => new DOMPoint(x, y).matrixTransform(matrix)) : [];
            const painted = {
              left: Math.min(...points.map(point => point.x)),
              right: Math.max(...points.map(point => point.x)),
              top: Math.min(...points.map(point => point.y)),
              bottom: Math.max(...points.map(point => point.y)),
            };
            const clip = { ...viewport };
            const boundaries = [];
            let effectiveOpacity = 1;
            let displayed = true;
            for (let ancestor = node; ancestor; ancestor = ancestor.parentElement) {
              const computed = getComputedStyle(ancestor);
              effectiveOpacity *= Number(computed.opacity);
              displayed &&= computed.display !== 'none' && computed.contentVisibility !== 'hidden';
              if (ancestor === node) continue;
              const clipsX = /^(hidden|clip|auto|scroll)$/.test(computed.overflowX);
              const clipsY = /^(hidden|clip|auto|scroll)$/.test(computed.overflowY);
              if (!clipsX && !clipsY) continue;
              const bounds = ancestor.getBoundingClientRect();
              // SVG's bounding client rect is its viewport, not its graphics getBBox().
              const isSvgViewport = ancestor instanceof SVGSVGElement;
              const scaleX = ancestor.offsetWidth ? bounds.width / ancestor.offsetWidth : 1;
              const scaleY = ancestor.offsetHeight ? bounds.height / ancestor.offsetHeight : 1;
              const left = isSvgViewport ? bounds.left : bounds.left + ancestor.clientLeft * scaleX;
              const top = isSvgViewport ? bounds.top : bounds.top + ancestor.clientTop * scaleY;
              const right = isSvgViewport ? bounds.right : left + ancestor.clientWidth * scaleX;
              const bottom = isSvgViewport ? bounds.bottom : top + ancestor.clientHeight * scaleY;
              if (clipsX) {
                clip.left = Math.max(clip.left, left);
                clip.right = Math.min(clip.right, right);
              }
              if (clipsY) {
                clip.top = Math.max(clip.top, top);
                clip.bottom = Math.min(clip.bottom, bottom);
              }
              boundaries.push({ tag: ancestor.tagName, className: ancestor.getAttribute('class'),
                overflowX: computed.overflowX, overflowY: computed.overflowY,
                clipsX, clipsY, left, top, right, bottom });
            }
            const alpha = style.stroke.match(/^rgba\([^)]*,\s*([\d.]+)\s*\)$/) ||
              style.stroke.match(/\/\s*([\d.]+)\s*\)$/);
            return [{
              index, element: node.outerHTML,
              intrinsic: { x: box.x, y: box.y, width: box.width, height: box.height,
                rx: node.rx.baseVal.value, ry: node.ry.baseVal.value, strokeWidth },
              paint: { stroke: style.stroke, strokeAlpha: alpha ? Number(alpha[1]) : 1,
                strokeOpacity: Number(style.strokeOpacity), effectiveOpacity, displayed,
                visibility: style.visibility, vectorEffect: style.vectorEffect },
              matrix: matrix && { a: matrix.a, b: matrix.b, c: matrix.c, d: matrix.d, e: matrix.e, f: matrix.f },
              clientBox: describe(node.getBoundingClientRect()), painted, clip, boundaries,
              overruns: { left: clip.left - painted.left, right: painted.right - clip.right,
                top: clip.top - painted.top, bottom: painted.bottom - clip.bottom },
            }];
          });
          return { boxes, shapes, viewport };
        }, scaled);
        const { boxes, shapes } = evidence.geometry;
        if (!scaled) {
          assert(boxes.some(box => box.width === 120 && box.height === 60),
            'Drawio lost the fixture’s 120×60 shape geometry after renderer switching');
          assert.equal(shapes.length, 1, 'Drawio must render exactly one actual 120×60 fixture rectangle');
        } else {
          assert.equal(shapes.length, 1, 'Scaled Drawio must render exactly one rounded fixture rectangle');
        }
        const shape = shapes[0];
        if (!scaled) {
          assert.deepEqual(boxes[shape.index], { width: 120, height: 60 });
        }
        const { x, y, width, height, rx, ry, strokeWidth } = shape.intrinsic;
        if (!scaled) {
          assert.deepEqual({ x, y, width, height }, { x: 40, y: 40, width: 120, height: 60 },
            'Drawio changed the fixture rectangle geometry');
        } else {
          assert([x, y, width, height].every(Number.isFinite) && width > 0 && height > 0,
            'Scaled Drawio fixture rectangle must have finite positive dimensions');
          // The pinned viewer may round scaled SVG attributes rather than use a CTM scale.
          assert(Math.abs(width / height - 2) <= 0.002 &&
            Math.abs(shape.clientBox.width / shape.clientBox.height - 2) <= 0.002,
          'Scaled Drawio fixture rectangle must preserve its 2:1 aspect ratio');
        }
        assert(rx > 0 && rx <= width / 2 && ry > 0 && ry <= height / 2,
          'Drawio fixture rectangle must retain rounded corners within its dimensions');
        assert(Number.isFinite(strokeWidth) && strokeWidth > 0, 'Drawio fixture stroke must have positive width');
        const { paint, matrix, painted, clip } = shape;
        assert(paint.displayed && paint.visibility === 'visible' && paint.effectiveOpacity > 0 &&
          paint.stroke && paint.stroke !== 'none' && paint.stroke !== 'transparent' &&
          paint.strokeAlpha > 0 && paint.strokeOpacity > 0, 'Drawio fixture stroke must visibly paint');
        // Local half-stroke expansion is exact for this fixture's axis-aligned scale/translation.
        assert(matrix && Object.values(matrix).every(Number.isFinite) &&
          Math.abs(matrix.b) < 1e-9 && Math.abs(matrix.c) < 1e-9 &&
          matrix.a > 0 && matrix.d > 0 && paint.vectorEffect === 'none',
          'Drawio fixture requires a nonsingular axis-aligned transform and scaling stroke');
        assert([...Object.values(painted), ...Object.values(clip)].every(Number.isFinite) &&
          painted.right > painted.left && painted.bottom > painted.top &&
          clip.right > clip.left && clip.bottom > clip.top, 'Drawio paint and clip bounds must be finite and nonempty');
        for (const edge of ['left', 'right', 'top', 'bottom'])
          assert(shape.overruns[edge] <= tolerance,
            `Drawio fixture ${edge} painted stroke exceeds its visible clipping boundary: ${JSON.stringify(shape)}`);
        if (transitionFailure) throw transitionFailure;
        if (transition) {
          evidence.transition.after = shape.clientBox;
          for (const axis of ['width', 'height']) {
            if (transition.target) {
              assert(Math.abs(transition.before[axis] - transition.target[axis]) > tolerance,
                `${transition.control} must start away from the initial screen ${axis}`);
              assert(Math.abs(shape.clientBox[axis] - transition.target[axis]) <= tolerance,
                `${transition.control} did not restore the initial screen ${axis}`);
            } else {
              assert(transition.direction === 'larger' ?
                shape.clientBox[axis] > transition.before[axis] + tolerance :
                shape.clientBox[axis] < transition.before[axis] - tolerance,
              `${transition.control} did not make the fixture screen ${axis} ${transition.direction}`);
            }
          }
        }
        return evidence.geometry;
      } catch (error) {
        failure = error;
        evidence.failure = String(error);
        throw error;
      } finally {
        console.log(JSON.stringify(evidence));
        const captured = await Promise.allSettled([
          session.page.screenshot({ path: path.join(artifacts, `${label}.png`) }),
          fs.writeFile(path.join(artifacts, `${label}.json`), JSON.stringify(evidence, null, 2) + '\n'),
        ]);
        const evidenceFailures = captured.filter(result => result.status === 'rejected').map(result => result.reason);
        for (const error of evidenceFailures)
          console.error(JSON.stringify({ case: label, artifactFailure: String(error) }));
        if (!failure && evidenceFailures.length) throw evidenceFailures[0];
      }
    };
    const andGeometryOf = async (session) => session.page.locator('.drawio-host svg').evaluate((svg) => {
      const paths = [...svg.querySelectorAll('path')].map((path) => path.getAttribute('d') || '');
      return {
        curved: paths.some((d) => /[AaCcQqSs]/.test(d)),
        wired: paths.some((d) => d.includes('M') && d.includes('L') && !/[AaCcQqSs]/.test(d)),
        pathCount: paths.length,
      };
    });
    const assertAndGeometry = async (session, label) => {
      const geometry = await andGeometryOf(session);
      assert.equal(geometry.curved, true, `${label} did not render the curved AND body`);
      assert.equal(geometry.wired && geometry.pathCount >= 2, true, `${label} did not render AND wire geometry`);
    };
    const decodeImageHref = async (session, expectedPath) => {
      const imageNode = session.page.locator('.drawio-host svg image');
      await imageNode.waitFor();
      const href = await imageNode.evaluate((node) =>
        node.href?.baseVal || node.getAttribute('href') ||
        node.getAttributeNS('http://www.w3.org/1999/xlink', 'href') || '');
      assert(href.includes(expectedPath), `image Drawio used ${href || 'no href'}`);
      const decoded = await session.page.evaluate(async (src) => {
        const img = new Image();
        img.src = src;
        await img.decode();
        return { width: img.naturalWidth, height: img.naturalHeight };
      }, href);
      assert(decoded.width > 0 && decoded.height > 0, 'image Drawio asset did not decode');
      return href;
    };
    const assertMathGlyphs = async (session) => {
      await session.page.locator('.drawio-host mjx-container, .drawio-host mjx-math, .drawio-host .MathJax').waitFor();
      const mathOutput = await session.page.locator('.drawio-host').evaluate((host) => {
        const mjx = host.querySelector('mjx-container, mjx-math, .MathJax');
        return {
          hasMjx: Boolean(mjx),
          text: (mjx && mjx.textContent) || '',
          hasGlyph: Boolean(host.querySelector('mjx-mi, mjx-mo, mjx-mn, mjx-mrow, use[data-c], [data-mjx-texclass]')),
        };
      });
      assert.equal(mathOutput.hasMjx, true, 'math Drawio did not render MathJax output');
      assert(mathOutput.hasGlyph || /E/.test(mathOutput.text), 'math Drawio did not render formula glyphs');
    };
    const revealViewerToolbar = async (session) => {
      await session.page.locator('.drawio-host').hover();
    };
    const clickViewerToolbar = async (session, title) => {
      await revealViewerToolbar(session);
      await session.page.locator('body').getByTitle(title, { exact: true }).click();
    };
    const clickNextPage = async (session) => {
      await clickViewerToolbar(session, 'Next Page');
    };
    const clickFullscreenNextAnd = async (session) => {
      const lightbox = session.page.locator('body > .geDiagramContainer');
      await lightbox.hover();
      await session.page.locator('body').getByTitle('Next Page: AND', { exact: true }).click();
      return lightbox;
    };
    const clickFullscreenNextImage = async (session) => {
      const lightbox = session.page.locator('body > .geDiagramContainer');
      await lightbox.hover();
      await session.page.locator('body').getByTitle('Next Page: Image', { exact: true }).click();
      return lightbox;
    };
    const activateStandaloneFile = async (session, name) => {
      // Fullscreen covers the pointer region; these clicks use the mounted selector's actual handlers.
      await session.page.locator('.file-selector-btn').evaluate(node => node.click());
      await session.page.locator('.dropdown-menu.open .item-name')
        .getByText(name, { exact: true }).evaluate(node => node.click());
      await session.page.waitForFunction(expected =>
        document.querySelector('.file-selector-btn .selector-name')?.textContent === expected, name);
    };
    const captureFullscreenNodes = async (session, content) => {
      const diagram = session.page.locator('body > .geDiagramContainer');
      await diagram.getByText(content, { exact: true }).waitFor();
      const nodes = await diagram.evaluateHandle((diagram) => {
        const children = [...document.body.children];
        return [
          { role: 'diagram', node: diagram },
          { role: 'close', node: children.find(node => node.matches('img.geAdaptiveAsset') && node.style.position === 'fixed') },
          { role: 'toolbar', node: children.find(node => node.querySelector('[title="Close (Escape)"]')) },
          { role: 'backdrop', node: children.findLast(node => node !== diagram && node.tagName === 'DIV' &&
            node.style.position === 'fixed' && node.childElementCount === 0 &&
            ['top', 'left', 'bottom', 'right'].every(edge => node.style[edge] === '0px')) },
        ];
      });
      assert.deepEqual(await nodes.evaluate(nodes =>
        nodes.filter(({ node }) => node?.parentNode === document.body).map(({ role }) => role).sort()),
      ['backdrop', 'close', 'diagram', 'toolbar'], `${content} did not mount its complete fullscreen UI`);
      return nodes;
    };
    const assertFullscreenRetired = async (session, nodes, overflow, label) => {
      await session.page.waitForFunction(nodes =>
        nodes.every(({ node }) => !node.isConnected), nodes);
      assert.deepEqual(await nodes.evaluate(nodes =>
        nodes.filter(({ node }) => node.isConnected).map(({ role }) => role).sort()), [],
      `${label} retained old fullscreen nodes`);
      assert.equal(await session.page.locator('body > .geDiagramContainer').count(), 0,
        `${label} retained a fullscreen diagram`);
      assert.equal(await session.page.evaluate(() => document.body.style.overflow), overflow,
        `${label} did not restore body scrolling`);
    };
    const assertStandaloneChrome = async (session, label) => {
      assert.equal(await session.page.locator('#app').count(), 1, `${label} lost #app`);
      assert.equal(await session.page.locator('.file-selector').count(), 1, `${label} lost .file-selector`);
      assert.equal(await session.page.locator('body > .download-prompt').count(), 0,
        `${label} replaced document.body with the download fallback`);
    };
    const caseRequests = (session) => requests.slice(session.before);
    const caseErrors = (session, after = session.beforeErrors) => consoleErrors.slice(after);
    const requestUrl = (path) => `${origin}${path}`;
    const isHttp404Text = (error) =>
      /Failed to load resource: the server responded with a status of 404 \(Not Found\)/.test(error.text);
    const isExpected404 = (error, path) =>
      isHttp404Text(error) && (error.url === requestUrl(path) ||
        (error.url && new URL(error.url).pathname === path));
    const isDrawioRenderError = (error) => error.text.includes('Draw.io render error:');
    const isLoadStencilSetLog = (error) => error.text.includes('error in loadStencilSet');
    const acceptMissingStencilErrors = (session, after) => {
      const ownedRequests = caseRequests(session);
      const missingStencilPath = ownedRequests.find(row =>
        row.path.includes('/static/drawio/stencils/electrical/logic_gates.xml') && row.status === 404)?.path;
      assert(missingStencilPath, 'missing stencil case did not request the electrical AND stencil');
      const viewerScriptPath = ownedRequests.find(row =>
        row.path.endsWith('/drawio/js/viewer-static.min.js') && row.status === 200)?.path;
      const isMissingStencilError = (error) => {
        if (isDrawioRenderError(error) || isLoadStencilSetLog(error)) return true;
        if (isExpected404(error, missingStencilPath)) return true;
        return Boolean(viewerScriptPath) && isHttp404Text(error) &&
          error.url === requestUrl(viewerScriptPath);
      };
      const ownedStencilErrors = caseErrors(session, after).filter(isMissingStencilError);
      assert(ownedStencilErrors.some(isDrawioRenderError) ||
        ownedStencilErrors.some(error => isExpected404(error, missingStencilPath) ||
          (viewerScriptPath && error.url === requestUrl(viewerScriptPath))),
        'missing stencil did not fail visibly');
      acceptOwnedErrors(session, isMissingStencilError, after);
    };
    const acceptOwnedErrors = (session, isExpected, after) => {
      const owned = caseErrors(session, after);
      const unexpected = owned.filter(error => !isExpected(error));
      assert.deepEqual(unexpected, [], 'unrecognized console errors');
      expectedOwnedErrors.push(...owned.filter(isExpected));
    };
    const cellphoneImagePath = (session) =>
      `${session.prefix || '/ocu'}/static/drawio/img/telecommunication/Cellphone_128x128.png`;
    const waitCellphoneImage = async (session, after = session.before) => {
      await waitUntil(() => requests.slice(after).some(row =>
        row.path.endsWith('/drawio/img/telecommunication/Cellphone_128x128.png')),
        'cellphone image request');
      return requests.slice(after).find(row =>
        row.path.endsWith('/drawio/img/telecommunication/Cellphone_128x128.png'));
    };
    const acceptMissingImageErrors = (session, after) => {
      const ownedRequests = caseRequests(session);
      const missingPath = ownedRequests.find(row =>
        row.path.endsWith('/drawio/img/telecommunication/Cellphone_128x128.png') && row.status === 404)?.path;
      assert(missingPath, 'missing image case did not request the cellphone PNG');
      const isMissingImageError = (error) =>
        isDrawioRenderError(error) || isExpected404(error, missingPath);
      assert(caseErrors(session, after).some(isMissingImageError),
        'missing bundled image did not fail visibly');
      acceptOwnedErrors(session, isMissingImageError, after);
    };

    const htmlResults = [];
    const configureHtmlFixture = (prefix, redirected) => {
      const filePath = `reports/space "'&<img id="html-injected-element" onerror="window.__htmlUnexpectedEffect=true" src=x>.html`;
      const canonicalPath = `${prefix}/files/${CHAT}/reports/space%20%22%27%26%3Cimg%20id%3D%22html-injected-element%22%20onerror%3D%22window.__htmlUnexpectedEffect%3Dtrue%22%20src%3Dx%3E.html`;
      const canonicalDirectory = `${prefix}/files/${CHAT}/reports/`;
      const resourceDirectory = redirected
        ? `${prefix}/public-preview-resource-canary/$&/` : canonicalDirectory;
      const documentPath = redirected ? resourceDirectory + 'report.html' : canonicalPath;
      const neighborPath = `${prefix}/files/${CHAT}/neighbor.txt`;
      bytes['html-matrix/document'] = Buffer.from(`<!doctype html><html><head>
        <link rel="stylesheet" href="style.css"></head><body>
        <h1 id="redirect-resource">HTML resource directory</h1>
        <img id="resource-image" src="pixel.png" alt="Known local image">
        <p id="script-effect">Script pending</p><p id="data-effect">Data pending</p>
        <p id="section">Canonical section</p>
        <p><a href="#section">Same document</a></p>
        <p><a href="https://example.invalid/document">External document</a></p>
        <p><a id="malformed-link" href="http://[">Malformed document</a></p>
        <p><a href="../neighbor.txt?view=one#part">Canonical neighbor</a></p>
        <script>
          const denials = {};
          try { parent.document.getElementById('html-parent-canary').textContent; denials.parentDom = 'allowed'; }
          catch (error) { denials.parentDom = error.name; }
          try { parent.localStorage.getItem('html-parent-canary'); denials.parentStorage = 'allowed'; }
          catch (error) { denials.parentStorage = error.name; }
          try { localStorage.getItem('html-parent-canary'); denials.ownStorage = 'allowed'; }
          catch (error) { denials.ownStorage = error.name; }
          parent.postMessage({ type: 'html-isolation', denials }, '*');
          document.addEventListener('click', event => {
            if (event.target.closest('a')?.id === 'malformed-link')
              parent.postMessage({ type: 'html-malformed-click' }, '*');
          }, true);
        </script><script src="effect.js"></script></body></html>`);
      bytes['html-matrix/style.css'] = Buffer.from('#redirect-resource { color: rgb(17, 85, 153); }');
      bytes['html-matrix/wrong.css'] = Buffer.from('#redirect-resource { color: rgb(80, 80, 80); }');
      bytes['html-matrix/pixel.png'] = bytes['guides/pixel.png'];
      bytes['html-matrix/wrong.png'] = Buffer.from(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+XwN8AAAAASUVORK5CYII=', 'base64');
      bytes['html-matrix/effect.js'] = Buffer.from(`
        document.getElementById('script-effect').textContent = 'Classic script from resource directory';
        fetch('data.json', { credentials: 'omit' }).then(response => response.json()).then(data => {
          document.getElementById('data-effect').textContent = data.value;
        });`);
      bytes['html-matrix/wrong.js'] = Buffer.from(`
        document.getElementById('script-effect').textContent = 'Wrong canonical script';
        fetch('data.json', { credentials: 'omit' }).then(response => response.json()).then(data => {
          document.getElementById('data-effect').textContent = data.value;
        });`);
      bytes['html-matrix/data.json'] = Buffer.from('{"value":"Relative JSON from resource directory"}');
      bytes['html-matrix/wrong.json'] = Buffer.from('{"value":"Wrong canonical JSON"}');
      bytes['html-matrix/neighbor.txt'] = Buffer.from('Canonical neighbor content from the workspace.');
      htmlFixtures.set(documentPath, { key: 'html-matrix/document', type: 'text/html' });
      htmlFixtures.set(neighborPath, { key: 'html-matrix/neighbor.txt', type: 'text/plain' });
      if (redirected) htmlFixtures.set(canonicalPath, { redirect: documentPath });
      for (const [name, key, wrongKey, type] of [
        ['style.css', 'style.css', 'wrong.css', 'text/css'],
        ['pixel.png', 'pixel.png', 'wrong.png', 'image/png'],
        ['effect.js', 'effect.js', 'wrong.js', 'text/javascript'],
        ['data.json', 'data.json', 'wrong.json', 'application/json'],
      ]) {
        htmlFixtures.set(resourceDirectory + name, { key: `html-matrix/${key}`, type });
        if (redirected)
          htmlFixtures.set(canonicalDirectory + name, { key: `html-matrix/${wrongKey}`, type });
      }
      standaloneListing = [
        files.find(file => file.file_id === 'hostile-markdown'),
        { file_id: 'redirect-neighbor', path: 'neighbor.txt', type: 'text', mime: 'text/plain',
          url: neighborPath },
        { file_id: 'redirect-html', path: filePath, type: 'html', mime: 'text/html',
          url: canonicalPath },
      ];
      return { filePath, canonicalPath, canonicalDirectory, resourceDirectory, documentPath, neighborPath };
    };
    const clearHtmlFixture = () => {
      standaloneListing = null;
      for (const pending of heldHtml.splice(0)) pending.res.destroy();
      htmlFixtures.clear();
      for (const key of Object.keys(bytes))
        if (key.startsWith('html-matrix/')) delete bytes[key];
    };
    const prepareHtmlSession = async (session) => {
      session.failures = [];
      session.page.on('requestfailed', request => session.failures.push({
        request, url: request.url(), failure: request.failure()?.errorText,
      }));
      await session.page.evaluate(() => {
        const marker = document.createElement('div');
        marker.id = 'html-parent-canary';
        marker.textContent = 'Benign parent DOM marker';
        marker.hidden = true;
        document.body.appendChild(marker);
        localStorage.setItem('html-parent-canary', 'Benign parent storage marker');
        window.__htmlLinkMessages = [];
        window.__htmlIsolation = [];
        window.__htmlMalformedClicks = 0;
        window.__htmlGeneratedWindow = null;
        window.__htmlUnexpectedEffect = false;
        window.addEventListener('message', event => {
          const generatedWindow = window.__htmlGeneratedWindow ||
            document.querySelector('.preview-stage iframe')?.contentWindow;
          if (event.source !== generatedWindow) return;
          if (event.data?.type === 'html-isolation') {
            window.__htmlGeneratedWindow = event.source;
            window.__htmlIsolation.push({ origin: event.origin, denials: event.data.denials });
          }
          if (event.data?.type === 'iframe-link-click')
            window.__htmlLinkMessages.push({
              href: event.data.href, resolvedUrl: event.data.resolvedUrl, origin: event.origin,
            });
          if (event.data?.type === 'html-malformed-click') window.__htmlMalformedClicks++;
        });
      });
    };
    const closeHtmlDropdown = async (session) => {
      if (await session.page.locator('.dropdown-menu.open').count())
        await session.page.locator('.file-selector-btn').click();
    };
    const assertHtmlSelection = async (session, name, canonicalPath) => {
      assert.equal(await session.page.locator('.file-selector-btn .selector-name').innerText(), name);
      assert.equal(await session.page.locator('.toolbar-right a[download]').first().evaluate(node => node.href),
        `${origin}${canonicalPath}?download=1`, 'HTML navigation changed the canonical download URL');
    };
    for (const prefix of ['', '/ocu', '/tools/ocu']) {
      for (const redirected of [true, false]) {
        const fixture = configureHtmlFixture(prefix, redirected);
        const label = `html-${redirected ? 'redirect' : 'direct'}-${prefix.replaceAll('/', '-') || 'root'}`;
        let session;
        try {
          session = await openStandalone(prefix);
          await prepareHtmlSession(session);
          const finalResponsePromise = session.page.waitForResponse(response =>
            response.url() === `${origin}${fixture.documentPath}`);
          await selectStandaloneFile(session.page, fixture.filePath);
          const finalResponse = await finalResponsePromise;
          assert.equal(finalResponse.status(), 200);
          const canonicalRequest = finalResponse.request().redirectedFrom();
          if (redirected) {
            assert(canonicalRequest, 'generated HTML fetch did not follow a real redirect');
            assert.equal(canonicalRequest.url(), `${origin}${fixture.canonicalPath}`);
            const canonicalResponse = await canonicalRequest.response();
            assert.equal(canonicalResponse.status(), 302);
            assert.equal(canonicalResponse.headers().location, fixture.documentPath);
          } else {
            assert.equal(canonicalRequest, null, 'direct HTML fixture unexpectedly redirected');
          }
          const iframe = session.page.locator('.preview-stage iframe');
          const generated = session.page.frameLocator('.preview-stage iframe');
          const marker = generated.locator('#redirect-resource');
          await marker.waitFor();
          const color = await marker.evaluate(async node => {
            if (document.readyState !== 'complete')
              await new Promise(resolve => window.addEventListener('load', resolve, { once: true }));
            return getComputedStyle(node).color;
          });
          await generated.getByText('Classic script from resource directory', { exact: true }).waitFor();
          await generated.getByText('Relative JSON from resource directory', { exact: true }).waitFor();
          const dimensions = await generated.locator('#resource-image').evaluate(async node => {
            await node.decode();
            return { width: node.naturalWidth, height: node.naturalHeight };
          });
          await session.page.waitForFunction(() => window.__htmlIsolation.length === 1);
          const isolation = await session.page.evaluate(() => window.__htmlIsolation);
          assert.deepEqual(isolation, [{ origin: 'null', denials: {
            parentDom: 'SecurityError', parentStorage: 'SecurityError', ownStorage: 'SecurityError',
          } }], 'generated HTML acquired parent DOM or storage authority');
          assert.deepEqual(await session.page.evaluate(() => ({
            dom: document.getElementById('html-parent-canary').textContent,
            storage: localStorage.getItem('html-parent-canary'),
            effect: window.__htmlUnexpectedEffect,
            injected: document.querySelectorAll('#html-injected-element').length,
          })), { dom: 'Benign parent DOM marker', storage: 'Benign parent storage marker',
            effect: false, injected: 0 });
          assert.deepEqual(await marker.evaluate(() => ({
            injected: document.querySelectorAll('#html-injected-element').length,
            effect: Boolean(window.__htmlUnexpectedEffect),
          })), { injected: 0, effect: false }, 'encoded filename injected markup or script into generated HTML');
          await assertHtmlSelection(session, fixture.filePath, fixture.canonicalPath);
          assert.equal(await iframe.getAttribute('sandbox'), 'allow-scripts allow-forms');
          await closeHtmlDropdown(session);
          await session.page.screenshot({ path: path.join(artifacts, `${label}-resources.png`) });
          assert.equal(color, 'rgb(17, 85, 153)',
            'relative HTML stylesheet did not use the final response directory');
          assert.deepEqual(dimensions, { width: 2, height: 2 });
          for (const name of ['style.css', 'pixel.png', 'effect.js', 'data.json'])
            assert(caseRequests(session).some(row =>
              row.path === fixture.resourceDirectory + name && row.status === 200),
            `HTML resource was not fetched from the final directory: ${name}`);
          if (redirected) {
            assert(caseRequests(session).some(row =>
              row.path === fixture.canonicalPath && row.status === 302));
            assert(caseRequests(session).some(row =>
              row.path === fixture.documentPath && row.status === 200));
            assert.deepEqual(caseRequests(session).filter(row =>
              row.path.startsWith(fixture.canonicalDirectory) && row.path !== fixture.canonicalPath), [],
            'redirected HTML loaded resources from its canonical directory');
          }
          const topUrl = session.page.url();
          await generated.getByRole('link', { name: 'Same document', exact: true }).click();
          await session.page.waitForFunction(() => window.__htmlLinkMessages.length === 1);
          assert.deepEqual(await session.page.evaluate(() => window.__htmlLinkMessages), [{
            href: '#section', resolvedUrl: `${origin}${fixture.canonicalPath}#section`, origin: 'null',
          }]);
          await assertHtmlSelection(session, fixture.filePath, fixture.canonicalPath);
          assert.equal(session.page.url(), topUrl, 'fragment changed the trusted top-level location');
          const openedPages = [];
          session.isolated.on('page', openedPage => openedPages.push(openedPage.url()));
          const beforeExternal = externalRequests.length;
          await generated.getByRole('link', { name: 'External document', exact: true }).click();
          const dialog = session.page.locator('#__ext_link_dialog');
          await dialog.getByText('https://example.invalid/document', { exact: true }).waitFor();
          assert.deepEqual(await session.page.evaluate(() => window.__htmlLinkMessages[1]), {
            href: 'https://example.invalid/document',
            resolvedUrl: 'https://example.invalid/document', origin: 'null',
          });
          assert.equal(externalRequests.length, beforeExternal, 'external link fetched before confirmation');
          assert.deepEqual(openedPages, [], 'external link opened a window before confirmation');
          await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
          await dialog.waitFor({ state: 'detached' });
          await assertHtmlSelection(session, fixture.filePath, fixture.canonicalPath);
          await generated.getByRole('link', { name: 'Malformed document', exact: true }).click();
          await session.page.waitForFunction(() => window.__htmlMalformedClicks === 1);
          assert.equal(await session.page.evaluate(() => window.__htmlLinkMessages.length), 2,
            'malformed absolute link emitted a navigation message');
          assert.equal(session.page.url(), topUrl, 'malformed link navigated the trusted top-level page');
          await assertHtmlSelection(session, fixture.filePath, fixture.canonicalPath);
          await generated.getByRole('link', { name: 'Canonical neighbor', exact: true }).click();
          await session.page.waitForFunction(() => window.__htmlLinkMessages.length === 3);
          assert.deepEqual(await session.page.evaluate(() => window.__htmlLinkMessages[2]), {
            href: '../neighbor.txt?view=one#part',
            resolvedUrl: `${origin}${fixture.neighborPath}?view=one#part`, origin: 'null',
          }, 'generated HTML link message leaked the temporary resource URL');
          await session.page.locator('.preview-stage')
            .getByText('Canonical neighbor content from the workspace.', { exact: true }).waitFor();
          await assertHtmlSelection(session, 'neighbor.txt', fixture.neighborPath);
          assert.equal(session.page.url(), topUrl);
          assert.equal(externalRequests.length, beforeExternal);
          assert.deepEqual(openedPages, []);
          assert.deepEqual(caseErrors(session), [], `${label} produced unexpected browser errors`);
          assert.deepEqual(session.failures, [], `${label} produced unexpected network failures`);
          await closeHtmlDropdown(session);
          await session.page.screenshot({ path: path.join(artifacts, `${label}-canonical-neighbor.png`) });
          htmlResults.push({ prefix, mode: redirected ? 'redirect' : 'direct', color, dimensions,
            script: 'Classic script from resource directory', data: 'Relative JSON from resource directory',
            canonicalMessages: ['fragment', 'external', 'neighbor'], malformed: 'no navigation',
            selected: 'neighbor.txt', download: 'canonical', isolation: isolation[0],
            unexpectedErrors: 0 });
        } catch (error) {
          if (session) await captureStandaloneFailure(session, label);
          throw error;
        } finally {
          if (session) await closeStandalone(session);
          clearHtmlFixture();
        }
      }
    }
    for (const transition of ['superseded', 'fallback']) {
      const fixture = configureHtmlFixture('/ocu', true);
      let session;
      let oldStage;
      let abortFirstFetch;
      let abortedRequest;
      try {
        session = await openStandalone('/ocu');
        await prepareHtmlSession(session);
        if (transition === 'superseded') {
          const heldFixture = htmlFixtures.get(fixture.documentPath);
          heldFixture.hold = true;
          await selectStandaloneFile(session.page, fixture.filePath);
          await waitUntil(() => heldHtml.length === 1, 'held redirected HTML response');
          oldStage = await session.page.locator('.preview-stage').elementHandle();
          await selectStandaloneFile(session.page, 'neighbor.txt');
          await session.page.locator('.preview-stage')
            .getByText('Canonical neighbor content from the workspace.', { exact: true }).waitFor();
          await assertHtmlSelection(session, 'neighbor.txt', fixture.neighborPath);
          const pending = heldHtml.shift();
          heldFixture.hold = false;
          pending.record.status = 200;
          respond(pending.res, 200, bytes[pending.fixture.key], pending.fixture.type,
            { 'Access-Control-Allow-Origin': 'null' });
          await session.page.waitForFunction(stage => Boolean(stage.querySelector('iframe')), oldStage);
          assert.equal(await oldStage.evaluate(stage => stage.isConnected), false);
          await session.page.locator('.preview-stage')
            .getByText('Canonical neighbor content from the workspace.', { exact: true }).waitFor();
          await assertHtmlSelection(session, 'neighbor.txt', fixture.neighborPath);
          assert.equal(await session.page.locator('.preview-stage iframe').count(), 0,
            'late redirected HTML replaced the current neighbor preview');
          assert.deepEqual(session.failures, []);
          assert.deepEqual(caseErrors(session), []);
        } else {
          abortFirstFetch = async route => {
            if (!abortedRequest && route.request().resourceType() === 'fetch') {
              abortedRequest = route.request();
              return route.abort('connectionreset');
            }
            return route.continue();
          };
          await session.page.route(`${origin}${fixture.canonicalPath}`, abortFirstFetch);
          const fallbackResponsePromise = session.page.waitForResponse(response =>
            response.url() === `${origin}${fixture.documentPath}` &&
            response.request().resourceType() === 'document');
          await selectStandaloneFile(session.page, fixture.filePath);
          const fallbackResponse = await fallbackResponsePromise;
          assert.equal(fallbackResponse.status(), 200);
          assert(abortedRequest, 'fallback case did not abort the first canonical fetch');
          assert.equal(abortedRequest.url(), `${origin}${fixture.canonicalPath}`);
          const iframe = session.page.locator('.preview-stage iframe');
          const generated = session.page.frameLocator('.preview-stage iframe');
          const marker = generated.locator('#redirect-resource');
          await marker.waitFor();
          await marker.evaluate(async () => {
            if (document.readyState !== 'complete')
              await new Promise(resolve => window.addEventListener('load', resolve, { once: true }));
          });
          await generated.getByText('Relative JSON from resource directory', { exact: true }).waitFor();
          assert.equal(await iframe.getAttribute('src'), fixture.canonicalPath);
          assert.equal(await iframe.getAttribute('srcdoc'), null);
          assert.equal(await iframe.getAttribute('sandbox'), 'allow-scripts allow-forms');
          assert.equal(fallbackResponse.request().redirectedFrom().url(), `${origin}${fixture.canonicalPath}`);
          await assertHtmlSelection(session, fixture.filePath, fixture.canonicalPath);
          await waitUntil(() => session.failures.some(row => row.request === abortedRequest) &&
            caseErrors(session).some(error => error.url === `${origin}${fixture.canonicalPath}` &&
              error.text === 'Failed to load resource: net::ERR_CONNECTION_RESET'),
          'first-fetch fallback diagnostic');
          assert.deepEqual(session.failures.map(({ request, ...row }) => row), [{
            url: `${origin}${fixture.canonicalPath}`, failure: 'net::ERR_CONNECTION_RESET',
          }], 'fallback produced an unrelated network failure');
          assert.equal(session.failures[0].request, abortedRequest);
          const errors = caseErrors(session);
          assert.equal(errors.length, 1, 'fallback produced an unrelated browser error');
          assert.equal(errors[0].url, `${origin}${fixture.canonicalPath}`);
          assert.equal(errors[0].text, 'Failed to load resource: net::ERR_CONNECTION_RESET');
          expectedOwnedErrors.push(errors[0]);
        }
        await closeHtmlDropdown(session);
        await session.page.screenshot({ path: path.join(artifacts, `html-${transition}.png`) });
        htmlResults.push({ prefix: '/ocu', mode: transition,
          selected: transition === 'superseded' ? 'neighbor.txt' : fixture.filePath,
          download: 'canonical', unexpectedErrors: 0 });
      } catch (error) {
        if (session) await captureStandaloneFailure(session, `html-${transition}`);
        throw error;
      } finally {
        if (session && abortFirstFetch)
          await session.page.unroute(`${origin}${fixture.canonicalPath}`, abortFirstFetch);
        await oldStage?.dispose();
        for (const pending of heldHtml.splice(0)) pending.res.destroy();
        if (session) await closeStandalone(session);
        clearHtmlFixture();
      }
    }
    console.log(JSON.stringify({ htmlResourceBase: 'ok', cases: htmlResults }));

    const panelSession = await openStandalone('/ocu');
    try {
      await panelSession.page.evaluate(() => window.__freezePollingClock());
      await panelSession.page.locator('.view-tab').filter({ hasText: 'Sub-agent' }).click();
      await panelSession.page.locator('.dash-btn-secondary').waitFor();
      assert.equal(await panelSession.page.locator('h4').filter({ hasText: 'Uploaded files' }).count(), 0,
        'standalone panel rendered an uploaded-files section');
      const beforePanel = requests.length;
      const chooserPromise = panelSession.page.waitForEvent('filechooser');
      await panelSession.page.locator('.dash-btn-secondary').click();
      const chooser = await chooserPromise;
      await chooser.setFiles({ name: 'note.txt', mimeType: 'text/plain', buffer: Buffer.from('hello') });
      await waitUntil(() => requests.slice(beforePanel).some(row =>
        row.method === 'POST' && row.path === `/ocu/api/uploads/${CHAT}/note.txt`),
        'standalone upload POST');
      await waitUntil(() => requests.slice(beforePanel).some(row =>
        row.method === 'GET' && row.path === `/ocu/api/outputs/${CHAT}`),
        'standalone upload Files refresh');
      const afterUpload = requests.slice(beforePanel);
      assert.equal(afterUpload.filter(row =>
        row.path === `/ocu/api/uploads/${CHAT}/list` ||
        row.path === `/ocu/api/uploads/${CHAT}/manifest`).length, 0,
        'standalone upload issued an upload list or manifest request');
      // Frozen 3000ms App poll cannot satisfy this waitUntil (10s deadline) or the
      // exactly-one GET count; only onFilesRefresh after the upload POST can.
      assert.equal(afterUpload.filter(row =>
        row.method === 'GET' && row.path === `/ocu/api/outputs/${CHAT}`).length, 1,
        'standalone upload Files refresh was not a single callback GET');
      await panelSession.page.locator('.dash-btn-secondary').waitFor();
      await panelSession.page.screenshot({ path: path.join(artifacts, 'standalone-terminal-panel.png') });
      await panelSession.page.locator('.view-tab').filter({ hasText: 'Files' }).click();
      await panelSession.page.locator('.file-selector-btn').waitFor();
      await panelSession.page.locator('.file-selector-btn').click();
      await panelSession.page.locator('.dropdown-menu.open').waitFor();
      assert.equal(await panelSession.page.locator('.dropdown-menu.open .item-name')
        .getByText('hostile.docx', { exact: true }).count(), 1,
        'standalone Files listing omitted the retained outputs entry');
      assert.equal(await panelSession.page.locator('.dropdown-menu.open .item-name')
        .filter({ hasText: 'note.txt' }).count(), 1,
        'standalone upload did not appear in the Files dropdown');

      await panelSession.page.locator('.view-tab').filter({ hasText: 'Sub-agent' }).click();
      await panelSession.page.locator('.dash-btn-secondary').waitFor();
      await exerciseUploadFailures(panelSession.page, panelSession.page, true);
      await exerciseDiscoveryWarning(panelSession.page, panelSession.page, true);
      assert.deepEqual(caseErrors(panelSession).filter(error => !expectedOwnedErrors.includes(error)), [],
        'standalone panel produced unexpected console errors');
    } catch (error) {
      await captureStandaloneFailure(panelSession, 'standalone-panel');
      throw error;
    } finally {
      standaloneUploadedNote = false;
      await closeStandalone(panelSession);
    }

    browserFixture = 'fault';
    const beforeShellFault = requests.length;
    const socketsBeforeShellFault = activeSockets.size;
    const faultSession = await openStandalone('/ocu');
    try {
      await faultSession.page.locator('.view-tab').filter({ hasText: 'Browser' }).click();
      const shellFaultAlert = faultSession.page.getByRole('alert');
      await shellFaultAlert.getByText('gateway_publication_unassigned').waitFor();
      const shellFaultText = await shellFaultAlert.innerText();
      assert(shellFaultText.includes('9222/tcp'), 'standalone Browser fault omitted the detail');
      assert(shellFaultText.includes('Browser port not published by the host engine'),
        'standalone Browser fault omitted the title');
      assert.equal(await faultSession.page.locator('.runtime-fault-detail').count(), 1,
        'standalone Browser fault did not render one detail line');
      assert.equal(await faultSession.page.getByText('Browser unavailable').count(), 0,
        'standalone Browser fault rendered the unavailable alert');
      assert.equal(await faultSession.page.getByText('Shared AI Browser').count(), 0,
        'standalone Browser fault rendered the empty state');
      assert.equal(await faultSession.page.locator('.browser-connecting, canvas').count(), 0,
        'standalone Browser fault mounted the viewer');
      assert.equal(requests.slice(beforeShellFault).filter(row =>
        row.path === `/ocu/browser/${CHAT}/json`).length, 0, 'standalone Browser fault requested pages');
      assert.equal(activeSockets.size, socketsBeforeShellFault, 'standalone Browser fault started a connection');
      await faultSession.page.screenshot({ path: path.join(artifacts, 'standalone-browser-fault.png'), animations: 'disabled' });
    } finally {
      await closeStandalone(faultSession);
      browserFixture = 'inactive';
    }

    const beforeShellTerminal = requests.length;
    const messagesBeforeShellTerminal = socketMessages.length;
    const shellTerminalSession = await openStandalone('/ocu', { query: '?model=m-1' });
    try {
      await shellTerminalSession.page.locator('.view-tab').filter({ hasText: 'Sub-agent' }).click();
      await shellTerminalSession.page.locator('.dash-btn-primary').click();
      await waitUntil(() => socketMessages.slice(messagesBeforeShellTerminal).some(row =>
        row.path === `/ocu/terminal/${CHAT}/ws` && row.method === 'ttyd-init'), 'standalone terminal start');
      assert.deepEqual(requests.slice(beforeShellTerminal).filter(row => row.method === 'POST' &&
        row.path === `/ocu/terminal/${CHAT}/start-ttyd`).map(startBody), [{ dangerous_mode: false }],
      'standalone preview sent a model');
    } finally {
      await closeStandalone(shellTerminalSession);
    }
    await waitUntil(() => activeSockets.size === 0, 'standalone terminal socket cleanup');

    assert.equal(activeSockets.size, 0, 'sticky viewer case started with a connection');
    const messagesBeforeSticky = socketMessages.length;
    const stickySession = await openStandalone('/ocu');
    try {
      // An active mount poll switches the shell away from Files, which openStandalone waits on.
      browserFixture = 'active';
      await stickySession.page.evaluate(() => window.__runAppPoll());
      await stickySession.page.locator('.view-tab').filter({ hasText: 'Browser' }).click();
      await waitUntil(() => socketMessages.slice(messagesBeforeSticky).some(row =>
        row.path === `/ocu/browser/${CHAT}/devtools/page/fixture-page` && row.method === 'Page.enable') &&
        activeSockets.size === 1, 'standalone viewer connected');
      assert.equal(await stickySession.page.locator('canvas').count(), 1, 'standalone viewer has no canvas');
      browserFixture = 'fault';
      const beforeStickyPoll = requests.length;
      await stickySession.page.evaluate(() => window.__runAppPoll());
      await waitUntil(() => requests.slice(beforeStickyPoll).some(row =>
        row.path === `/ocu/browser/${CHAT}/status`), 'sticky viewer fault poll');
      await new Promise(resolve => setTimeout(resolve, 200));
      assert.equal(await stickySession.page.locator('canvas').count(), 1,
        'fault payload unmounted the connected viewer');
      assert.equal(await stickySession.page.getByRole('alert').count(), 0,
        'fault payload rendered an alert over the connected viewer');
      assert.equal(await stickySession.page.locator('.runtime-fault').count(), 0,
        'fault payload rendered the fault panel over the connected viewer');
      assert.equal(activeSockets.size, 1, 'fault payload dropped the connected viewer');
    } finally {
      await closeStandalone(stickySession);
      browserFixture = 'inactive';
    }
    await waitUntil(() => activeSockets.size === 0, 'sticky viewer socket cleanup');

    browserFixture = 'failure';
    const failureSession = await openStandalone('/ocu');
    try {
      await failureSession.page.locator('.view-tab').filter({ hasText: 'Browser' }).click();
      await failureSession.page.getByText('Shared AI Browser').waitFor();
      assert.equal(await failureSession.page.getByRole('alert').count(), 0,
        'failed shell Browser status rendered an alert');
      assert.equal(await failureSession.page.locator('.runtime-fault').count(), 0,
        'failed shell Browser status rendered the fault panel');
      const failureErrors = caseErrors(failureSession);
      const statusFailures = failureErrors.filter(error => error.text.includes('503') &&
        (error.url.includes(`/browser/${CHAT}/status`) || error.text.includes(`/browser/${CHAT}/status`)));
      expectedOwnedErrors.push(...statusFailures);
      assert.deepEqual(failureErrors.filter(error => !statusFailures.includes(error)), [],
        'failed shell Browser status produced unexpected console errors');
    } finally {
      await closeStandalone(failureSession);
      browserFixture = 'inactive';
    }
    console.log(JSON.stringify({ runtimeFault: 'ok', embeddedBrowser: true, shellBrowser: true,
      terminal: true, terminalRecovery: true, shellTerminalWithoutModel: true }));

    const compatibilityResults = [];
    const compatibilityExpected = [];
    const compatibilityErrors = [];
    for (const prefix of ['', '/ocu', '/tools/ocu']) {
      const session = await openStandalone(prefix);
      try {
        await selectStandaloneFile(session.page, 'guides/compatibility.md');
        const body = session.page.locator('.preview-stage .markdown-body');
        await body.locator('h1').getByText('User Content Policy', { exact: true }).waitFor();
        const images = await body.locator('img').evaluateAll(async nodes => Promise.all(nodes.map(async node => {
          let decoded = true;
          try { await node.decode(); } catch { decoded = false; }
          return { alt: node.alt, decoded, width: node.naturalWidth, height: node.naturalHeight, src: node.src };
        })));
        const fragments = [];
        for (const { label, target } of compatibilityFragments) {
          const link = body.locator('a').getByText(label, { exact: true });
          await link.scrollIntoViewIfNeeded();
          const start = await link.evaluate((node, text) => {
            const preview = node.closest('.preview').getBoundingClientRect();
            const top = Math.max(0, preview.top);
            const bottom = Math.min(innerHeight, preview.bottom);
            const heading = [...node.closest('.markdown-body').querySelectorAll('h1,h2')]
              .find(element => element.textContent === text);
            const targetBox = heading.getBoundingClientRect();
            const linkBox = node.getBoundingClientRect();
            return {
              href: node.getAttribute('href'),
              ready: linkBox.top >= top - 1 && linkBox.bottom <= bottom + 1 &&
                (targetBox.bottom < top - 1 || targetBox.top > bottom + 1),
            };
          }, target);
          let navigated = false;
          if (start.href && start.ready) {
            await link.click();
            try {
              await session.page.waitForFunction(text => {
                const preview = document.querySelector('.preview').getBoundingClientRect();
                const heading = [...document.querySelectorAll('.markdown-body h1,.markdown-body h2')]
                  .find(element => element.textContent === text).getBoundingClientRect();
                return heading.top >= Math.max(0, preview.top) - 1 &&
                  heading.bottom <= Math.min(innerHeight, preview.bottom) + 1;
              }, target, { timeout: 3000 });
              navigated = true;
            } catch (error) {
              if (error.name !== 'TimeoutError') throw error;
            }
          }
          fragments.push({ label, hrefPresent: start.href !== null, startReady: start.ready, navigated });
        }
        const tokenImage = images.find(image => image.alt === 'Nested token chart');
        const rawImage = images.find(image => image.alt === 'Origin-root HTML chart');
        compatibilityResults.push({
          prefix, fragments,
          tokenImage: {
            decoded: tokenImage.decoded, width: tokenImage.width, height: tokenImage.height,
            workspacePath: new URL(tokenImage.src).pathname.replace(/\/{2,}/g, '/') === `${prefix}/files/${CHAT}/guides/chart.png`,
          },
          rawImage: {
            decoded: rawImage.decoded, width: rawImage.width, height: rawImage.height,
            originRoot: rawImage.src === `${origin}/raw-root-chart.png`,
          },
        });
        compatibilityExpected.push({
          prefix,
          fragments: compatibilityFragments.map(({ label }) => ({
            label, hrefPresent: true, startReady: true, navigated: true,
          })),
          tokenImage: { decoded: true, width: 2, height: 2, workspacePath: true },
          rawImage: { decoded: true, width: 2, height: 2, originRoot: true },
        });
        compatibilityErrors.push(...caseErrors(session));
      } catch (error) {
        await captureStandaloneFailure(session, 'markdown-compatibility');
        throw error;
      } finally { await closeStandalone(session); }
    }
    assert.deepEqual(compatibilityResults, compatibilityExpected,
      'Markdown fragments and nested token resources must preserve consumer behavior');
    assert.deepEqual(compatibilityErrors, [], 'Markdown compatibility produced unexpected console errors');
    console.log(JSON.stringify({ markdownCompatibility: 'ok', prefixes: ['', '/ocu', '/tools/ocu'] }));

    const markdownSession = await openStandalone('/ocu');
    try {
      await markdownSession.page.locator('.preview').evaluate((host) => {
        window.__markdownInsertions = [];
        const isCanary = (node) => node.nodeType === Node.ELEMENT_NODE &&
          node.matches('img[alt="Markdown event canary"]');
        const recordCanary = (node) => {
          if (isCanary(node) && node.hasAttribute('onload'))
            window.__markdownInsertions.push({ kind: 'inserted', value: node.getAttribute('onload') });
        };
        // Observe the live preview boundary, including attributes stripped after insertion.
        const observer = new MutationObserver((records) => {
          for (const record of records) {
            if (record.type === 'attributes' && record.attributeName === 'onload' &&
                isCanary(record.target) && record.oldValue !== null)
              window.__markdownInsertions.push({ kind: 'changed', value: record.oldValue });
            for (const node of record.addedNodes) {
              recordCanary(node);
              if (node.nodeType === Node.ELEMENT_NODE)
                node.querySelectorAll('img[alt="Markdown event canary"]').forEach(recordCanary);
            }
          }
        });
        observer.observe(host, { childList: true, subtree: true, attributes: true, attributeOldValue: true });
      });
      await selectStandaloneFile(markdownSession.page, 'hostile.md');
      const body = markdownSession.page.locator('.preview-stage .markdown-body');
      await body.locator('strong').getByText('Safe Markdown neighbor', { exact: true }).waitFor();
      const image = body.locator('img[alt="Markdown event canary"]');
      const decoded = await image.evaluate(async (node) => {
        await node.decode();
        return { width: node.naturalWidth, height: node.naturalHeight };
      });
      assert.deepEqual(decoded, { width: 1, height: 1 }, 'harmless Markdown raster did not decode');
      const boundary = await markdownSession.page.evaluate(async () => {
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        return {
          executed: document.documentElement.getAttribute('data-markdown-event-canary'),
          insertions: window.__markdownInsertions,
        };
      });
      assert.deepEqual(boundary, { executed: null, insertions: [] },
        'hostile Markdown event content crossed the trusted preview boundary');
      assert.deepEqual(caseErrors(markdownSession), [], 'hostile Markdown produced unexpected console errors');
    } catch (error) {
      await captureStandaloneFailure(markdownSession, 'hostile-markdown');
      throw error;
    } finally {
      await closeStandalone(markdownSession);
    }

    const matrixSession = await openStandalone('/ocu');
    try {
      await matrixSession.page.locator('.preview').evaluate((host) => {
        window.__matrixInsertions = [];
        const prohibited = 'script,style,link,meta,base,form,input,button,textarea,select,option,iframe,object,embed,svg,math,audio,video,[style],[srcset],[name]';
        const inspect = (node, inMarkdown) => {
          if (node.nodeType !== Node.ELEMENT_NODE) return;
          const bodies = inMarkdown || node.matches('.markdown-body') || node.closest('.markdown-body')
            ? [node] : [...node.querySelectorAll('.markdown-body')];
          for (const body of bodies) {
            for (const element of [body, ...body.querySelectorAll('*')]) {
              if (element.matches(prohibited) || [...element.attributes].some(attribute => /^on/i.test(attribute.name)))
                window.__matrixInsertions.push(element.outerHTML);
              for (const attribute of ['href', 'src']) {
                const value = (element.getAttribute(attribute) || '').replace(/[\u0000-\u0020\u007f-\u009f]/g, '');
                if (/^(?:javascript|vbscript|file|blob|unknown):/i.test(value) ||
                    attribute === 'src' && /^data:(?!image\/(?:png|jpeg|gif|webp);base64,)/i.test(value))
                  window.__matrixInsertions.push(value);
              }
            }
          }
        };
        const observer = new MutationObserver(records => {
          for (const record of records) {
            if (record.type === 'attributes' && record.oldValue !== null &&
                /^(?:on.*|style|srcset|name)$/i.test(record.attributeName))
              window.__matrixInsertions.push(record.attributeName + '=' + record.oldValue);
            record.addedNodes.forEach(node => inspect(node, Boolean(record.target.closest('.markdown-body'))));
          }
        });
        observer.observe(host, { childList: true, subtree: true, attributes: true, attributeOldValue: true });
      });
      await selectStandaloneFile(matrixSession.page, 'matrix.md');
      const matrixBody = matrixSession.page.locator('.preview-stage .markdown-body');
      await matrixBody.locator('strong').getByText('Allowed matrix neighbor', { exact: true }).waitFor();
      await matrixBody.getByText('Clean span', { exact: true }).click();
      for (const label of ['Script scheme', 'Obfuscated scheme', 'VB scheme', 'File scheme', 'Blob scheme',
        'Data link', 'Unknown scheme', 'Token script', 'Token unknown', 'Malformed fragment']) {
        const link = matrixBody.locator('a').getByText(label, { exact: true });
        assert.equal(await link.getAttribute('href'), null, `${label} remained navigable`);
        await link.click();
      }
      assert.equal(await matrixBody.locator('img[alt^="Forbidden"]').count(), 0);
      assert.equal(await matrixBody.locator('[id="user-content-DOMPurify"]').count(), 1);
      assert.deepEqual(await matrixSession.page.evaluate(() => ({
        executed: document.documentElement.getAttribute('data-markdown-matrix-canary'),
        insertions: window.__matrixInsertions,
        clobbered: window.DOMPurify instanceof Element || window.__OCU_OFFICE_PURIFY instanceof Element,
      })), { executed: null, insertions: [], clobbered: false },
      'prohibited Markdown content reached the live preview');
      assert.deepEqual(caseErrors(matrixSession), [], 'hostile matrix produced unexpected console errors');
    } catch (error) {
      await captureStandaloneFailure(matrixSession, 'markdown-matrix');
      throw error;
    } finally { await closeStandalone(matrixSession); }

    for (const prefix of ['', '/ocu', '/tools/ocu']) {
      const safeSession = await openStandalone(prefix, { firstFile: 'hostile.md', images: true });
      try {
        await selectStandaloneFile(safeSession.page, 'guides/safe.md');
        const body = safeSession.page.locator('.preview-stage .markdown-body');
        await body.locator('.katex-display .katex-html').waitFor();
        assert.equal(await body.locator('strong').getByText('Bold text', { exact: true }).count(), 1);
        assert.equal(await body.locator('em').getByText('Italic text', { exact: true }).count(), 1);
        assert.equal(await body.locator('s').getByText('Struck text', { exact: true }).count(), 1);
        assert.equal(await body.locator('del').getByText('Deleted text', { exact: true }).count(), 1);
        assert.equal(await body.locator('blockquote').innerText(), 'Quoted text');
        assert.deepEqual(await body.locator('li').allTextContents(), ['First item', 'Second item']);
        assert.deepEqual(await body.locator('tbody td').allTextContents(), ['Answer', '42']);
        await body.locator('summary').click();
        assert.equal(await body.locator('details').evaluate(node => node.open), true);
        await body.getByText('Visible when opened', { exact: true }).waitFor();
        for (const [label, size] of [
          ['Relative raster', 2], ['Raw relative raster', 2], ['HTTPS raster', 2], ['HTTP raster', 2],
          ['Inline png', 2], ['Inline jpeg', 2], ['Inline webp', 2], ['Inline gif', 1],
        ]) {
          const decoded = await body.locator(`img[alt="${label}"]`).evaluate(async node => {
            await node.decode();
            return { width: node.naturalWidth, height: node.naturalHeight };
          });
          assert.deepEqual(decoded, { width: size, height: size }, `${label} did not decode`);
        }
        assert(caseRequests(safeSession).some(row => row.path === `${prefix}/files/${CHAT}/guides/pixel.png`),
          'relative Markdown image did not resolve within the document directory');
        assert.equal(await body.locator('pre code.language-javascript .hljs-keyword').innerText(), 'const');
        const diagram = body.locator('.mermaid svg');
        await diagram.waitFor();
        assert.equal(await diagram.getByText('Alpha', { exact: true }).count(), 1);
        assert.equal(await diagram.getByText('Beta', { exact: true }).count(), 1);
        assert.equal(await diagram.locator('.edgePaths path').evaluateAll(nodes =>
          nodes.some(node => /[ML]/.test(node.getAttribute('d') || ''))), true,
        'Markdown Mermaid did not render connecting geometry');
        assert((await body.locator('.katex-html').allTextContents()).some(text => text.includes('E') && text.includes('mc')),
          'Markdown math did not render formula glyphs');
        const heading = body.locator('h1').getByText('Safe heading', { exact: true });
        assert.equal(await heading.getAttribute('id'), 'user-content-safe-heading');
        const headingLink = body.getByText('Jump to heading', { exact: true });
        await headingLink.scrollIntoViewIfNeeded();
        const beforeNavigation = await headingLink.evaluate(link => {
          const preview = link.closest('.preview').getBoundingClientRect();
          const top = Math.max(0, preview.top);
          const bottom = Math.min(window.innerHeight, preview.bottom);
          const linkBox = link.getBoundingClientRect();
          const headingBox = document.getElementById('user-content-safe-heading').getBoundingClientRect();
          return {
            linkVisible: linkBox.top >= top - 1 && linkBox.bottom <= bottom + 1,
            headingOutside: headingBox.bottom < top - 1 || headingBox.top > bottom + 1,
          };
        });
        assert.deepEqual(beforeNavigation, { linkVisible: true, headingOutside: true },
          'heading navigation did not start from a visible link and an offscreen target');
        await headingLink.click();
        await safeSession.page.waitForFunction(() => {
          const preview = document.querySelector('.preview').getBoundingClientRect();
          const heading = document.getElementById('user-content-safe-heading').getBoundingClientRect();
          const top = Math.max(0, preview.top);
          const bottom = Math.min(window.innerHeight, preview.bottom);
          return heading.top >= top - 1 && heading.bottom <= bottom + 1;
        });
        for (const [label, destination] of [
          ['HTTPS destination', 'https://links.example.test/approved'],
          ['HTTP destination', 'http://links.example.test/approved'],
        ]) {
          await body.getByText(label, { exact: true }).click();
          await safeSession.page.locator('#__ext_link_dialog').getByText(destination, { exact: true }).waitFor();
          await safeSession.page.locator('#__ext_link_dialog').getByText('Cancel', { exact: true }).click();
        }
        // Observe native mailto dispatch, then stop at the operating-system boundary.
        await safeSession.page.evaluate(() => document.addEventListener('click', event => {
          const link = event.target.closest('a');
          if (link?.getAttribute('href')?.startsWith('mailto:')) {
            window.__mailDispatch = { href: link.href, prevented: event.defaultPrevented };
            event.preventDefault();
          }
        }));
        await body.getByText('Send mail', { exact: true }).click();
        assert.deepEqual(await safeSession.page.evaluate(() => window.__mailDispatch),
          { href: 'mailto:reader@example.test', prevented: false }, 'mailto became a workspace file link');
        await safeSession.page.screenshot({ path: path.join(artifacts, `markdown-safe-${prefix.replaceAll('/', '-') || 'root'}.png`) });
        await body.getByText('Relative Office', { exact: true }).click();
        await safeSession.page.locator('.preview-stage').getByText('Table cell', { exact: true }).waitFor();
        const unsafe = safeSession.page.locator('.preview-stage a').getByText('Unsafe link', { exact: true });
        assert.equal(await unsafe.getAttribute('href'), null, 'Markdown policy leaked into Office');
        // Converted-markup seam proof: Office must still reject Markdown-approved remote images.
        const officeImages = await safeSession.page.locator('.preview-stage').evaluate(async (stage, inline) => {
          const { safeOfficeHtml } = await import(new URL('../static/preview.js', document.baseURI).href);
          const fragment = safeOfficeHtml('<img src="https://images.example.test/pixel.png" alt="remote">' +
            '<img src="' + inline + '" alt="inline">');
          const probe = document.createElement('div');
          probe.appendChild(fragment);
          stage.appendChild(probe);
          const images = [...probe.querySelectorAll('img')];
          await Promise.all(images.map(image => image.decode()));
          const output = images.map(image => ({
            src: image.getAttribute('src'), width: image.naturalWidth, height: image.naturalHeight,
          }));
          probe.remove();
          return output;
        }, rasters.png);
        assert.deepEqual(officeImages, [{ src: rasters.png, width: 2, height: 2 }],
          'Markdown remote-image allowance leaked into Office');
        await selectStandaloneFile(safeSession.page, 'diagram.drawio');
        await safeSession.page.locator('.drawio-host svg').waitFor();
        await safeSession.page.getByText('Local shape', { exact: true }).waitFor();
        await assertLocalShapeGeometry(safeSession);
        await selectStandaloneFile(safeSession.page, 'guides/safe.md');
        await body.locator('img[alt="HTTPS raster"]').evaluate(node => node.decode());
        await body.locator('.katex-display .katex-html').waitFor();
        assert.deepEqual(caseErrors(safeSession), [], 'safe Markdown consumers produced unexpected console errors');
      } catch (error) {
        await captureStandaloneFailure(safeSession, `markdown-safe-${prefix.replaceAll('/', '-') || 'root'}`);
        throw error;
      } finally { await closeStandalone(safeSession); }
    }

    const reverseSession = await openStandalone('/ocu', { firstFile: 'diagram.drawio', images: true });
    try {
      await reverseSession.page.locator('.drawio-host svg').waitFor();
      await reverseSession.page.getByText('Local shape', { exact: true }).waitFor();
      await assertLocalShapeGeometry(reverseSession);
      await selectStandaloneFile(reverseSession.page, 'guides/safe.md');
      await reverseSession.page.locator('.markdown-body img[alt="HTTPS raster"]').evaluate(node => node.decode());
      await reverseSession.page.locator('.markdown-body .katex-display .katex-html').waitFor();
      await selectStandaloneFile(reverseSession.page, 'hostile.docx');
      const link = reverseSession.page.locator('.preview-stage a').getByText('Unsafe link', { exact: true });
      await link.waitFor();
      assert.equal(await link.getAttribute('href'), null);
      await reverseSession.page.locator('.preview-stage').getByText('Table cell', { exact: true }).waitFor();
      await selectStandaloneFile(reverseSession.page, 'diagram.drawio');
      await reverseSession.page.getByText('Local shape', { exact: true }).waitFor();
      await assertLocalShapeGeometry(reverseSession);
      assert.deepEqual(caseErrors(reverseSession), [], 'Drawio-first Markdown policy isolation failed');
    } catch (error) {
      await captureStandaloneFailure(reverseSession, 'markdown-drawio-first');
      throw error;
    } finally { await closeStandalone(reverseSession); }

    for (const mode of ['missing', 'invalid']) {
      let requested = false;
      const failureSession = await openStandalone('/ocu', {
        firstFile: 'hostile.md',
        sanitizerRoute: route => {
          requested = true;
          return route.fulfill(mode === 'missing'
            ? { status: 404, contentType: 'text/plain', body: 'missing sanitizer' }
            : { status: 200, contentType: 'text/javascript', body: 'window.DOMPurify = {isSupported:false};' });
        },
      });
      try {
        await failureSession.page.locator('.preview-stage .empty-state').getByText('Failed to load file', { exact: true }).waitFor();
        assert.equal(requested, true, 'failure fixture did not request the sanitizer');
        assert.equal(await failureSession.page.locator('.preview-stage .markdown-body').count(), 0);
        assert.equal(await failureSession.page.getByText('Safe Markdown neighbor', { exact: true }).count(), 0);
        assert.equal(await failureSession.page.evaluate(() =>
          document.documentElement.getAttribute('data-markdown-event-canary')), null);
        const sanitizerPath = '/ocu/static/purify.min.js';
        assert(caseErrors(failureSession).some(error => error.text.includes('Markdown render error:')),
          'sanitizer failure did not report a Markdown load error');
        acceptOwnedErrors(failureSession, error => error.text.includes('Markdown render error:') ||
          mode === 'missing' && isExpected404(error, sanitizerPath));
      } catch (error) {
        await captureStandaloneFailure(failureSession, `markdown-sanitizer-${mode}`);
        throw error;
      } finally { await closeStandalone(failureSession); }
    }

    let heldSanitizer;
    let sanitizerRequested;
    const sanitizerArrival = new Promise(resolve => { sanitizerRequested = resolve; });
    const supersededSession = await openStandalone('/ocu', {
      firstFile: 'hostile.md', images: true,
      sanitizerRoute: route => { heldSanitizer = route; sanitizerRequested(); },
    });
    try {
      await within(sanitizerArrival, 'held Markdown sanitizer');
      await selectStandaloneFile(supersededSession.page, 'guides/safe.md');
      assert.equal(await supersededSession.page.locator('.preview-stage .markdown-body').count(), 0,
        'pending sanitizer inserted unsanitized Markdown');
      await heldSanitizer.fulfill({
        status: 200, contentType: 'text/javascript', body: await fs.readFile(path.join(STATIC, 'purify.min.js')),
      });
      heldSanitizer = null;
      await supersededSession.page.locator('.markdown-body .katex-display .katex-html').waitFor();
      await supersededSession.page.locator('.markdown-body img[alt="HTTPS raster"]').evaluate(node => node.decode());
      assert.equal(await supersededSession.page.getByText('Safe Markdown neighbor', { exact: true }).count(), 0,
        'superseded Markdown overwrote the current selection');
      assert.equal(await supersededSession.page.locator('.preview-stage').count(), 1);
      assert.equal(await supersededSession.page.evaluate(() =>
        document.documentElement.getAttribute('data-markdown-event-canary')), null);
      assert.deepEqual(caseErrors(supersededSession), [], 'retired Markdown produced unexpected console errors');
    } catch (error) {
      await captureStandaloneFailure(supersededSession, 'markdown-superseded');
      throw error;
    } finally {
      if (heldSanitizer) await heldSanitizer.abort();
      await closeStandalone(supersededSession);
    }


    const officeSession = await openStandalone('/ocu');
    try {
      const unsafe = officeSession.page.locator('.preview-stage a').filter({ hasText: 'Unsafe link' });
      await unsafe.waitFor();
      assert.equal(await unsafe.getAttribute('href'), null);
      await unsafe.click();
      assert.equal(await officeSession.page.evaluate(() => window.__docxExecuted), undefined);
      await officeSession.page.locator('.preview-stage').getByText('Table cell').waitFor();
      await officeSession.page.screenshot({ path: path.join(artifacts, 'standalone-sanitized-office.png') });
      await exerciseOfficeBookmarks(officeSession.page, officeSession.page, 'standalone');
      await selectStandaloneFile(officeSession.page, 'diagram.drawio');
      await officeSession.page.locator('.drawio-host svg').waitFor();
      await officeSession.page.getByText('Local shape').waitFor();
      await assertLocalShapeGeometry(officeSession, { phase: 'plain' });
      await officeSession.page.screenshot({ path: path.join(artifacts, 'standalone-drawio.png') });
      const afterGeometry = caseRequests(officeSession);
      assert(afterGeometry.some(row => row.path.includes('/static/drawio/js/viewer-static.min.js')),
        'standalone Drawio did not load local viewer materials');
      assert.equal(afterGeometry.filter(row => row.path.includes('viewer.diagrams.net')).length, 0);
      await selectStandaloneFile(officeSession.page, 'hostile.docx');
      const unsafeAfter = officeSession.page.locator('.preview-stage a').filter({ hasText: 'Unsafe link' });
      await unsafeAfter.waitFor();
      assert.equal(await unsafeAfter.getAttribute('href'), null);
      await unsafeAfter.click();
      assert.equal(await officeSession.page.evaluate(() => window.__docxExecuted), undefined);
      await officeSession.page.locator('.preview-stage').getByText('Table cell').waitFor();
      await officeSession.page.locator('.preview-stage').getByText('Safe content').waitFor();
      assert.deepEqual(caseErrors(officeSession), [], 'unexpected Drawio geometry console errors');
    } finally {
      await closeStandalone(officeSession);
    }

    const emptySession = await openStandalone('/ocu');
    try {
      await selectStandaloneFile(emptySession.page, 'empty.drawio');
      await emptySession.page.locator('.drawio-host svg').waitFor();
      assert.equal(await emptySession.page.locator('.dl-error').count(), 0);
      assert.equal(await emptySession.page.locator('.loading-text').count(), 0);
      assert.deepEqual(caseErrors(emptySession), [], 'valid empty Drawio produced a render failure');
    } finally {
      await closeStandalone(emptySession);
    }

    const compressedSession = await openStandalone('/ocu');
    try {
      await selectStandaloneFile(compressedSession.page, 'compressed.drawio');
      await compressedSession.page.locator('.drawio-host svg').waitFor();
      await compressedSession.page.getByText('Compressed shape').waitFor();
      await assertLocalShapeGeometry(compressedSession, { phase: 'compressed' });
      assert.deepEqual(caseErrors(compressedSession), [], 'compressed Drawio produced unexpected console errors');
    } finally {
      await closeStandalone(compressedSession);
    }

    const lazySession = await openStandalone('/ocu');
    try {
      await selectStandaloneFile(lazySession.page, 'lazy.drawio');
      await lazySession.page.locator('.drawio-host svg').waitFor();
      const andGeometry = await lazySession.page.locator('.drawio-host svg').evaluate((svg) => {
        const paths = [...svg.querySelectorAll('path')].map((path) => path.getAttribute('d') || '');
        return {
          curved: paths.some((d) => /[AaCcQqSs]/.test(d)),
          wired: paths.some((d) => d.includes('M') && d.includes('L') && !/[AaCcQqSs]/.test(d)),
          pathCount: paths.length,
        };
      });
      assert.equal(andGeometry.curved, true, 'AND stencil did not render the curved body');
      assert.equal(andGeometry.wired && andGeometry.pathCount >= 2, true, 'AND stencil did not render wire geometry');
      const lazyRequests = caseRequests(lazySession);
      assert(lazyRequests.some(row => row.path.includes('/static/drawio/stencils/electrical/logic_gates.xml')),
        'lazy Drawio did not request the electrical AND stencil');
      assert.equal(await lazySession.page.locator('.dl-error').count(), 0);
      assert.deepEqual(caseErrors(lazySession), [], 'lazy stencil Drawio produced unexpected console errors');
    } finally {
      await closeStandalone(lazySession);
    }

    const imageSession = await openStandalone('/ocu');
    try {
      await selectStandaloneFile(imageSession.page, 'image.drawio');
      const imageNode = imageSession.page.locator('.drawio-host svg image');
      await imageNode.waitFor();
      const imageHref = await imageNode.evaluate((node) =>
        node.href?.baseVal || node.getAttribute('href') ||
        node.getAttributeNS('http://www.w3.org/1999/xlink', 'href') || '');
      assert(imageHref.includes('/static/drawio/img/telecommunication/Cellphone_128x128.png'),
        `image Drawio used ${imageHref || 'no href'}`);
      const imageRequests = caseRequests(imageSession);
      assert(imageRequests.some(row =>
        row.path === '/ocu/static/drawio/img/telecommunication/Cellphone_128x128.png'),
        'image Drawio did not request the pinned cellphone asset');
      const decoded = await imageSession.page.evaluate(async (src) => {
        const img = new Image();
        img.src = src;
        await img.decode();
        return { width: img.naturalWidth, height: img.naturalHeight };
      }, imageHref);
      assert(decoded.width > 0 && decoded.height > 0, 'image Drawio asset did not decode');
      await imageSession.page.screenshot({ path: path.join(artifacts, 'standalone-drawio-image.png') });
      assert.deepEqual(caseErrors(imageSession), [], 'image Drawio produced unexpected console errors');
    } finally {
      await closeStandalone(imageSession);
    }

    drawioMissingImage = true;
    const missingImageSession = await openStandalone('/ocu');
    try {
      await selectStandaloneFile(missingImageSession.page, 'image.drawio');
      const missingImageRequest = await waitCellphoneImage(missingImageSession);
      assert.equal(missingImageRequest.status, 404);
      await missingImageSession.page.locator('.dl-error').waitFor();
      assert.equal(await missingImageSession.page.locator('.drawio-host svg').count(), 0);
      await assertStandaloneChrome(missingImageSession, 'missing-image');
      acceptMissingImageErrors(missingImageSession);
      const afterMissingImageErrors = consoleErrors.length;
      const afterMissingImageRequests = requests.length;
      drawioMissingImage = false;
      await selectStandaloneFile(missingImageSession.page, 'hostile.docx');
      await missingImageSession.page.locator('.preview-stage a').filter({ hasText: 'Unsafe link' }).waitFor();
      await selectStandaloneFile(missingImageSession.page, 'image.drawio');
      await decodeImageHref(missingImageSession, '/static/drawio/img/telecommunication/Cellphone_128x128.png');
      const restored = requests.slice(afterMissingImageRequests).find(row =>
        row.path.endsWith('/drawio/img/telecommunication/Cellphone_128x128.png'));
      assert(restored && restored.status === 200, 'restored cellphone PNG did not receive HTTP 200');
      assert.deepEqual(caseErrors(missingImageSession, afterMissingImageErrors), [],
        'restored cellphone PNG produced unexpected console errors');
    } catch (error) {
      await captureStandaloneFailure(missingImageSession, 'missing-image');
      throw error;
    } finally {
      drawioMissingImage = false;
      await closeStandalone(missingImageSession);
    }

    drawioCorruptImage = true;
    const corruptImageSession = await openStandalone('/ocu');
    try {
      await selectStandaloneFile(corruptImageSession.page, 'image.drawio');
      const corruptImageRequest = await waitCellphoneImage(corruptImageSession);
      assert.equal(corruptImageRequest.status, 200);
      await corruptImageSession.page.locator('.dl-error').waitFor();
      assert.equal(await corruptImageSession.page.locator('.drawio-host svg').count(), 0);
      await assertStandaloneChrome(corruptImageSession, 'corrupt-image');
      acceptOwnedErrors(corruptImageSession, isDrawioRenderError);
      const afterCorruptImageErrors = consoleErrors.length;
      const afterCorruptImageRequests = requests.length;
      drawioCorruptImage = false;
      await selectStandaloneFile(corruptImageSession.page, 'hostile.docx');
      await corruptImageSession.page.locator('.preview-stage a').filter({ hasText: 'Unsafe link' }).waitFor();
      await selectStandaloneFile(corruptImageSession.page, 'image.drawio');
      await decodeImageHref(corruptImageSession, '/static/drawio/img/telecommunication/Cellphone_128x128.png');
      const restoredCorrupt = requests.slice(afterCorruptImageRequests).find(row =>
        row.path.endsWith('/drawio/img/telecommunication/Cellphone_128x128.png'));
      assert(restoredCorrupt && restoredCorrupt.status === 200, 'restored invalid PNG did not receive HTTP 200');
      assert.deepEqual(caseErrors(corruptImageSession, afterCorruptImageErrors), [],
        'restored invalid PNG produced unexpected console errors');
    } catch (error) {
      await captureStandaloneFailure(corruptImageSession, 'corrupt-image');
      throw error;
    } finally {
      drawioCorruptImage = false;
      await closeStandalone(corruptImageSession);
    }

    drawioMissingImage = true;
    const fullscreenMissingImageSession = await openStandalone('/ocu');
    try {
      await selectStandaloneFile(fullscreenMissingImageSession.page, 'pages-image.drawio');
      await fullscreenMissingImageSession.page.locator('.drawio-host').getByText('Page one', { exact: true }).waitFor();
      assert.equal(await fullscreenMissingImageSession.page.locator('.dl-error').count(), 0);
      await clickViewerToolbar(fullscreenMissingImageSession, 'Fullscreen');
      const missingImageLightbox = fullscreenMissingImageSession.page.locator('body > .geDiagramContainer');
      await missingImageLightbox.getByText('Page one', { exact: true }).waitFor();
      const beforeFullscreenImage = requests.length;
      await clickFullscreenNextImage(fullscreenMissingImageSession);
      const fullscreenMissingRequest = await waitCellphoneImage(fullscreenMissingImageSession, beforeFullscreenImage);
      assert.equal(fullscreenMissingRequest.status, 404);
      await fullscreenMissingImageSession.page.locator('.dl-error').waitFor();
      await fullscreenMissingImageSession.page.locator('body > .geDiagramContainer').waitFor({ state: 'detached' });
      await assertStandaloneChrome(fullscreenMissingImageSession, 'fullscreen-missing-image');
      acceptMissingImageErrors(fullscreenMissingImageSession);
    } catch (error) {
      await captureStandaloneFailure(fullscreenMissingImageSession, 'fullscreen-missing-image');
      throw error;
    } finally {
      drawioMissingImage = false;
      await closeStandalone(fullscreenMissingImageSession);
    }

    drawioCorruptImage = true;
    const fullscreenCorruptImageSession = await openStandalone('/ocu');
    try {
      await selectStandaloneFile(fullscreenCorruptImageSession.page, 'pages-image.drawio');
      await fullscreenCorruptImageSession.page.locator('.drawio-host').getByText('Page one', { exact: true }).waitFor();
      await clickViewerToolbar(fullscreenCorruptImageSession, 'Fullscreen');
      const corruptImageLightbox = fullscreenCorruptImageSession.page.locator('body > .geDiagramContainer');
      await corruptImageLightbox.getByText('Page one', { exact: true }).waitFor();
      const beforeFullscreenCorruptImage = requests.length;
      await clickFullscreenNextImage(fullscreenCorruptImageSession);
      const fullscreenCorruptRequest = await waitCellphoneImage(fullscreenCorruptImageSession, beforeFullscreenCorruptImage);
      assert.equal(fullscreenCorruptRequest.status, 200);
      await fullscreenCorruptImageSession.page.locator('.dl-error').waitFor();
      await fullscreenCorruptImageSession.page.locator('body > .geDiagramContainer').waitFor({ state: 'detached' });
      await assertStandaloneChrome(fullscreenCorruptImageSession, 'fullscreen-corrupt-image');
      acceptOwnedErrors(fullscreenCorruptImageSession, isDrawioRenderError);
      const afterFullscreenCorruptImageErrors = consoleErrors.length;
      const afterFullscreenCorruptImageRequests = requests.length;
      drawioCorruptImage = false;
      await selectStandaloneFile(fullscreenCorruptImageSession.page, 'hostile.docx');
      await fullscreenCorruptImageSession.page.locator('.preview-stage a').filter({ hasText: 'Unsafe link' }).waitFor();
      await selectStandaloneFile(fullscreenCorruptImageSession.page, 'pages-image.drawio');
      await fullscreenCorruptImageSession.page.locator('.drawio-host').getByText('Page one', { exact: true }).waitFor();
      await clickViewerToolbar(fullscreenCorruptImageSession, 'Fullscreen');
      const recoveredImageLightbox = fullscreenCorruptImageSession.page.locator('body > .geDiagramContainer');
      await recoveredImageLightbox.getByText('Page one', { exact: true }).waitFor();
      await clickFullscreenNextImage(fullscreenCorruptImageSession);
      await recoveredImageLightbox.locator('svg image').waitFor();
      const recoveredFullscreenImage = requests.slice(afterFullscreenCorruptImageRequests).find(row =>
        row.path.endsWith('/drawio/img/telecommunication/Cellphone_128x128.png'));
      assert(recoveredFullscreenImage && recoveredFullscreenImage.status === 200,
        'fullscreen restored cellphone PNG did not receive HTTP 200');
      const recoveredDecoded = await recoveredImageLightbox.locator('svg image').evaluate(async (node) => {
        const src = node.href?.baseVal || node.getAttribute('href') ||
          node.getAttributeNS('http://www.w3.org/1999/xlink', 'href') || '';
        const img = new Image();
        img.src = src;
        await img.decode();
        return { width: img.naturalWidth, height: img.naturalHeight };
      });
      assert(recoveredDecoded.width > 0 && recoveredDecoded.height > 0,
        'fullscreen restored cellphone PNG did not decode');
      assert.deepEqual(caseErrors(fullscreenCorruptImageSession, afterFullscreenCorruptImageErrors), [],
        'fullscreen restored cellphone PNG produced unexpected console errors');
    } catch (error) {
      await captureStandaloneFailure(fullscreenCorruptImageSession, 'fullscreen-corrupt-image');
      throw error;
    } finally {
      drawioCorruptImage = false;
      await closeStandalone(fullscreenCorruptImageSession);
    }

    drawioMissingImage = true;
    const offscreenImageSession = await openStandalone('/ocu');
    try {
      await selectStandaloneFile(offscreenImageSession.page, 'pages-image.drawio');
      await offscreenImageSession.page.locator('.drawio-host').getByText('Page one', { exact: true }).waitFor();
      assert.equal(await offscreenImageSession.page.locator('.dl-error').count(), 0);
      assert.equal(caseRequests(offscreenImageSession).filter(row =>
        row.path.endsWith('/drawio/img/telecommunication/Cellphone_128x128.png')).length, 0);
      const beforeOffscreenNav = requests.length;
      await clickNextPage(offscreenImageSession);
      const offscreenRequest = await waitCellphoneImage(offscreenImageSession, beforeOffscreenNav);
      assert.equal(offscreenRequest.status, 404);
      await offscreenImageSession.page.locator('.dl-error').waitFor();
      await assertStandaloneChrome(offscreenImageSession, 'offscreen-image');
      acceptMissingImageErrors(offscreenImageSession);
    } catch (error) {
      await captureStandaloneFailure(offscreenImageSession, 'offscreen-image');
      throw error;
    } finally {
      drawioMissingImage = false;
      await closeStandalone(offscreenImageSession);
    }

    drawioHeldImage = true;
    const heldImageSession = await openStandalone('/ocu');
    try {
      await selectStandaloneFile(heldImageSession.page, 'image.drawio');
      await waitUntil(() => heldImages.length > 0, 'held cellphone PNG');
      const pngPath = cellphoneImagePath(heldImageSession);
      await selectStandaloneFile(heldImageSession.page, 'hostile.docx');
      const unsafeHeld = heldImageSession.page.locator('.preview-stage a').filter({ hasText: 'Unsafe link' });
      await unsafeHeld.waitFor();
      const beforeHeldErrors = consoleErrors.length;
      const pngResponse = heldImageSession.page.waitForResponse((response) => {
        try {
          return new URL(response.url()).pathname === pngPath && response.status() === 404;
        } catch {
          return false;
        }
      }, { timeout: 10000 });
      for (const pending of heldImages.splice(0)) {
        pending.record.status = 404;
        respond(pending.res, 404, 'missing image', 'text/plain');
      }
      assert.equal((await pngResponse).status(), 404);
      await waitUntil(() => caseErrors(heldImageSession, beforeHeldErrors).some((error) =>
        isExpected404(error, pngPath)), 'held cellphone PNG 404 console');
      await unsafeHeld.waitFor();
      assert.equal(await unsafeHeld.getAttribute('href'), null);
      assert.equal(await heldImageSession.page.locator('.dl-error').count(), 0);
      await assertStandaloneChrome(heldImageSession, 'held-image');
      assert.equal(caseErrors(heldImageSession, beforeHeldErrors).some(isDrawioRenderError), false,
        'retired image decode reported a Draw.io render error');
      acceptOwnedErrors(heldImageSession, (error) => isExpected404(error, pngPath), beforeHeldErrors);
    } catch (error) {
      await captureStandaloneFailure(heldImageSession, 'held-image');
      throw error;
    } finally {
      drawioHeldImage = false;
      for (const pending of heldImages.splice(0)) pending.res.destroy();
      await closeStandalone(heldImageSession);
    }

    const mathSession = await openStandalone('/ocu');
    try {
      await selectStandaloneFile(mathSession.page, 'math.drawio');
      await mathSession.page.locator('.drawio-host mjx-container, .drawio-host mjx-math, .drawio-host .MathJax').waitFor();
      const mathOutput = await mathSession.page.locator('.drawio-host').evaluate((host) => {
        const mjx = host.querySelector('mjx-container, mjx-math, .MathJax');
        return {
          hasMjx: Boolean(mjx),
          text: (mjx && mjx.textContent) || '',
          hasGlyph: Boolean(host.querySelector('mjx-mi, mjx-mo, mjx-mn, mjx-mrow, use[data-c], [data-mjx-texclass]')),
        };
      });
      assert.equal(mathOutput.hasMjx, true, 'math Drawio did not render MathJax output');
      assert(mathOutput.hasGlyph || /E/.test(mathOutput.text),
        'math Drawio did not render formula glyphs');
      const mathRequests = caseRequests(mathSession);
      assert(mathRequests.some(row => row.path.includes('/static/drawio/math4/')),
        'math Drawio did not request local MathJax materials');
      assert.deepEqual(caseErrors(mathSession), [], 'math Drawio produced unexpected console errors');
    } finally {
      await closeStandalone(mathSession);
    }

    const brokenSession = await openStandalone('/ocu');
    try {
      await selectStandaloneFile(brokenSession.page, 'broken.drawio');
      await brokenSession.page.locator('.dl-error').waitFor();
      assert(caseErrors(brokenSession).some(isDrawioRenderError),
        'broken Drawio did not report a render error');
      acceptOwnedErrors(brokenSession, isDrawioRenderError);
    } finally {
      await closeStandalone(brokenSession);
    }

    const missingDocSession = await openStandalone('/ocu');
    try {
      await selectStandaloneFile(missingDocSession.page, 'missing.drawio');
      await missingDocSession.page.locator('.dl-error').waitFor();
      const missingDocRequests = caseRequests(missingDocSession);
      const missingDocPath = missingDocRequests.find(row => row.path.endsWith('/missing.drawio'))?.path;
      assert(missingDocPath, 'missing Drawio document was not requested');
      assert.equal(missingDocRequests.find(row => row.path === missingDocPath).status, 404);
      const isMissingDocError = (error) =>
        isDrawioRenderError(error) || isExpected404(error, missingDocPath);
      assert(caseErrors(missingDocSession).some(isMissingDocError),
        'missing Drawio document did not fail visibly');
      acceptOwnedErrors(missingDocSession, isMissingDocError);
    } finally {
      await closeStandalone(missingDocSession);
    }

    drawioMissingViewer = true;
    const missingViewerSession = await openStandalone('/ocu');
    try {
      await selectStandaloneFile(missingViewerSession.page, 'diagram.drawio');
      await missingViewerSession.page.locator('.dl-error').waitFor();
      const missingViewerRequests = caseRequests(missingViewerSession);
      const missingViewerPath = missingViewerRequests.find(row =>
        row.path.endsWith('/drawio/js/viewer-static.min.js'))?.path;
      assert(missingViewerPath, 'missing viewer case did not request viewer-static.min.js');
      assert.equal(missingViewerRequests.find(row => row.path === missingViewerPath).status, 404);
      const isMissingViewerError = (error) =>
        isDrawioRenderError(error) || isExpected404(error, missingViewerPath);
      assert(caseErrors(missingViewerSession).some(isMissingViewerError),
        'missing viewer did not surface a load failure');
      acceptOwnedErrors(missingViewerSession, isMissingViewerError);
    } finally {
      drawioMissingViewer = false;
      await closeStandalone(missingViewerSession);
    }

    drawioCorruptViewer = true;
    const corruptViewerSession = await openStandalone('/ocu');
    try {
      await selectStandaloneFile(corruptViewerSession.page, 'diagram.drawio');
      await corruptViewerSession.page.locator('.dl-error').waitFor();
      const corruptRequests = caseRequests(corruptViewerSession);
      assert(corruptRequests.some(row => row.path.endsWith('/drawio/js/viewer-static.min.js')),
        'corrupt viewer case did not request viewer-static.min.js');
      assert(caseErrors(corruptViewerSession).some(isDrawioRenderError),
        'corrupt viewer did not fail visibly');
      acceptOwnedErrors(corruptViewerSession, isDrawioRenderError);
    } finally {
      drawioCorruptViewer = false;
      await closeStandalone(corruptViewerSession);
    }

    drawioMissingLazy = true;
    const missingLazySession = await openStandalone('/ocu');
    try {
      await selectStandaloneFile(missingLazySession.page, 'lazy.drawio');
      await missingLazySession.page.locator('.dl-error').waitFor();
      assert.equal(await missingLazySession.page.locator('.drawio-host svg').count(), 0);
      const fallbackRect = await missingLazySession.page.locator('.drawio-host').count().then(async (count) => {
        if (!count) return false;
        return missingLazySession.page.locator('.drawio-host').evaluate((host) => {
          const svg = host.querySelector('svg');
          if (!svg) return false;
          return [...svg.querySelectorAll('rect, path')].some((node) => {
            const d = node.getAttribute('d') || '';
            return node.tagName.toLowerCase() === 'rect' || (!/[AaCc]/.test(d) && d.includes('H') && d.includes('V'));
          });
        });
      });
      assert.equal(fallbackRect, false, 'missing stencil substituted a rectangle');
      acceptMissingStencilErrors(missingLazySession);
      const afterFailureErrors = consoleErrors.length;
      const afterFailureRequests = requests.length;
      drawioMissingLazy = false;
      await selectStandaloneFile(missingLazySession.page, 'hostile.docx');
      await missingLazySession.page.locator('.preview-stage a').filter({ hasText: 'Unsafe link' }).waitFor();
      await selectStandaloneFile(missingLazySession.page, 'lazy.drawio');
      await missingLazySession.page.locator('.drawio-host svg').waitFor();
      const retriedRequests = requests.slice(afterFailureRequests);
      const retriedStencil = retriedRequests.find(row =>
        row.path.includes('/static/drawio/stencils/electrical/logic_gates.xml'));
      assert(retriedStencil, 'AND stencil retry did not request the electrical stencil');
      assert.equal(retriedStencil.status, 200, 'AND stencil retry did not receive HTTP 200');
      const retriedGeometry = await missingLazySession.page.locator('.drawio-host svg').evaluate((svg) => {
        const paths = [...svg.querySelectorAll('path')].map((path) => path.getAttribute('d') || '');
        return {
          curved: paths.some((d) => /[AaCcQqSs]/.test(d)),
          wired: paths.some((d) => d.includes('M') && d.includes('L') && !/[AaCcQqSs]/.test(d)),
          pathCount: paths.length,
        };
      });
      assert.equal(retriedGeometry.curved, true, 'AND stencil retry did not render the curved body');
      assert.equal(retriedGeometry.wired && retriedGeometry.pathCount >= 2, true,
        'AND stencil retry did not render wire geometry');
      assert.deepEqual(caseErrors(missingLazySession, afterFailureErrors), [],
        'AND stencil retry produced unexpected console errors');
    } finally {
      drawioMissingLazy = false;
      await closeStandalone(missingLazySession);
    }

    drawioHeldDocument = true;
    const delayedSession = await openStandalone('/ocu');
    try {
      await selectStandaloneFile(delayedSession.page, 'diagram.drawio');
      await waitUntil(() => heldDrawio.length > 0, 'delayed Drawio document fetch');
      await selectStandaloneFile(delayedSession.page, 'hostile.docx');
      await delayedSession.page.locator('.preview-stage a').filter({ hasText: 'Unsafe link' }).waitFor();
      for (const pending of heldDrawio.splice(0))
        respond(pending, 200, bytes['diagram.drawio'], 'application/octet-stream');
      await new Promise(resolve => setTimeout(resolve, 500));
      await delayedSession.page.locator('.preview-stage a').filter({ hasText: 'Unsafe link' }).waitFor();
      await delayedSession.page.locator('.preview-stage').getByText('Table cell').waitFor();
      assert.equal(await delayedSession.page.locator('.drawio-host svg').count(), 0);
    } finally {
      drawioHeldDocument = false;
      for (const pending of heldDrawio.splice(0)) pending.destroy();
      await closeStandalone(delayedSession);
    }

    drawioHeldViewer = true;
    const overlapSession = await openStandalone('/ocu');
    try {
      await selectStandaloneFile(overlapSession.page, 'diagram.drawio');
      await waitUntil(() => heldViewer.length > 0, 'held Drawio viewer script');
      await selectStandaloneFile(overlapSession.page, 'hostile.docx');
      await overlapSession.page.locator('.file-selector-btn, .preview-stage, .markdown-body, .office-preview-banner').first().waitFor();
      assert.equal(heldViewer.length > 0, true, 'viewer script was released before Office selection began');
      const held = heldViewer.splice(0);
      const viewerBytes = await fs.readFile(path.join(STATIC, 'drawio/js/viewer-static.min.js'));
      for (const pending of held) {
        pending.record.status = 200;
        respond(pending.res, 200, viewerBytes, 'text/javascript');
      }
      await overlapSession.page.locator('.preview-stage a').filter({ hasText: 'Unsafe link' }).waitFor();
      assert.equal(await overlapSession.page.evaluate(() => window.__docxExecuted), undefined);
      await overlapSession.page.locator('.preview-stage').getByText('Table cell').waitFor();
      assert.equal(await overlapSession.page.locator('.drawio-host svg').count(), 0);
      await selectStandaloneFile(overlapSession.page, 'diagram.drawio');
      await overlapSession.page.locator('.drawio-host svg').waitFor();
      await overlapSession.page.getByText('Local shape').waitFor();
    } finally {
      drawioHeldViewer = false;
      for (const pending of heldViewer.splice(0)) pending.res.destroy();
      await closeStandalone(overlapSession);
    }

    const bpmnSession = await openStandalone('/ocu');
    try {
      await selectStandaloneFile(bpmnSession.page, 'bpmn.drawio');
      await bpmnSession.page.locator('.drawio-host svg').waitFor();
      await bpmnSession.page.locator('.drawio-host').getByText('Start', { exact: true }).waitFor();
      const bpmnGeometry = await bpmnSession.page.locator('.drawio-host svg').evaluate((svg) => {
        const ellipses = [...svg.querySelectorAll('ellipse, circle')];
        const paths = [...svg.querySelectorAll('path')].map((path) => path.getAttribute('d') || '');
        return { ellipses: ellipses.length, curved: paths.some((d) => /[AaCc]/.test(d)) || ellipses.length > 0 };
      });
      assert.equal(bpmnGeometry.curved, true, 'BPMN start event did not render elliptical geometry');
      assert.equal(await bpmnSession.page.locator('.dl-error').count(), 0);
      assert.deepEqual(caseErrors(bpmnSession), [], 'BPMN custom shape produced unexpected console errors');
    } catch (error) {
      await captureStandaloneFailure(bpmnSession, 'bpmn');
      throw error;
    } finally {
      await closeStandalone(bpmnSession);
    }

    const erSession = await openStandalone('/ocu');
    try {
      await selectStandaloneFile(erSession.page, 'er.drawio');
      await erSession.page.locator('.drawio-host svg').waitFor();
      await erSession.page.locator('.drawio-host').getByText('Customer', { exact: true }).waitFor();
      const erGeometry = await erSession.page.locator('.drawio-host svg').evaluate((svg) => {
        const paths = [...svg.querySelectorAll('path')].map((path) => {
          const d = path.getAttribute('d') || '';
          const box = path.getBBox();
          return { closed: /z\s*$/i.test(d.trim()), rounded: /[Cc]/.test(d), width: box.width, height: box.height };
        });
        const body = paths.find((path) => path.closed && path.rounded && Math.abs(path.width - 140) < 2 && Math.abs(path.height - 60) < 2);
        return { body: Boolean(body) };
      });
      assert.equal(erGeometry.body, true, 'ER entity did not render a closed rounded 140x60 body');
      assert.equal(await erSession.page.locator('.dl-error').count(), 0);
      assert.deepEqual(caseErrors(erSession), [], 'ER custom shape produced unexpected console errors');
    } catch (error) {
      await captureStandaloneFailure(erSession, 'er');
      throw error;
    } finally {
      await closeStandalone(erSession);
    }

    const pagesSession = await openStandalone('/ocu');
    try {
      await selectStandaloneFile(pagesSession.page, 'pages.drawio');
      await pagesSession.page.locator('.drawio-host svg').waitFor();
      await pagesSession.page.locator('.drawio-host').getByText('Page one', { exact: true }).waitFor();
      const pageOneGeometry = await assertLocalShapeGeometry(pagesSession, { phase: 'page-one' });
      assert.equal(await pagesSession.page.locator('.dl-error').count(), 0);
      const beforePageTwo = requests.length;
      assert.equal(caseRequests(pagesSession).filter(row =>
        row.path.includes('/stencils/electrical/logic_gates.xml')).length, 0,
        'page one requested the page-two AND stencil');
      const beforeZoom = await pagesSession.page.locator('.drawio-host svg').evaluate((svg) => svg.getBoundingClientRect().width);
      await clickViewerToolbar(pagesSession, 'Zoom In');
      const afterZoom = await pagesSession.page.locator('.drawio-host svg').evaluate((svg) => svg.getBoundingClientRect().width);
      assert(afterZoom > beforeZoom, 'Zoom In did not enlarge the diagram');
      const zoomedIn = await assertLocalShapeGeometry(pagesSession, {
        scaled: true, phase: 'zoom-in',
        transition: { control: 'Zoom In', before: pageOneGeometry.shapes[0].clientBox, direction: 'larger' },
      });
      await clickViewerToolbar(pagesSession, 'Zoom Out');
      const zoomedOut = await assertLocalShapeGeometry(pagesSession, {
        scaled: true, phase: 'zoom-out',
        transition: { control: 'Zoom Out', before: zoomedIn.shapes[0].clientBox, direction: 'smaller' },
      });
      // Move below the initial scale so Fit cannot pass as a no-op after reciprocal zoom.
      await clickViewerToolbar(pagesSession, 'Zoom Out');
      const reduced = await assertLocalShapeGeometry(pagesSession, {
        scaled: true, phase: 'zoom-out-below-initial',
        transition: { control: 'Zoom Out', before: zoomedOut.shapes[0].clientBox, direction: 'smaller' },
      });
      await clickViewerToolbar(pagesSession, 'Fit');
      const fitted = await assertLocalShapeGeometry(pagesSession, {
        scaled: true, phase: 'fit',
        transition: { control: 'Fit', before: reduced.shapes[0].clientBox,
          target: pageOneGeometry.shapes[0].clientBox },
      });
      await clickViewerToolbar(pagesSession, 'Fullscreen');
      const lightbox = pagesSession.page.locator('body > .geDiagramContainer');
      await lightbox.locator('svg').waitFor();
      await lightbox.getByText('Page one', { exact: true }).waitFor();
      await assertLocalShapeGeometry(pagesSession, {
        scaled: true, surface: 'body > .geDiagramContainer', phase: 'fullscreen-page-one',
      });
      const beforeFullscreenPageTwo = requests.length;
      await clickFullscreenNextAnd(pagesSession);
      await lightbox.getByText('AND', { exact: true }).waitFor();
      const fullscreenStencil = requests.slice(beforeFullscreenPageTwo).find(row =>
        row.path.includes('/static/drawio/stencils/electrical/logic_gates.xml'));
      assert(fullscreenStencil && fullscreenStencil.status === 200, 'fullscreen page two did not load the AND stencil');
      const fullscreenGeometry = await lightbox.locator('svg').evaluate((svg) => {
        const paths = [...svg.querySelectorAll('path')].map((path) => path.getAttribute('d') || '');
        return {
          curved: paths.some((d) => /[AaCcQqSs]/.test(d)),
          wired: paths.some((d) => d.includes('M') && d.includes('L') && !/[AaCcQqSs]/.test(d)),
          pathCount: paths.length,
        };
      });
      assert.equal(fullscreenGeometry.curved, true, 'fullscreen page two did not render the curved AND body');
      assert.equal(fullscreenGeometry.wired && fullscreenGeometry.pathCount >= 2, true,
        'fullscreen page two did not render AND wire geometry');
      await pagesSession.page.locator('body > img.geAdaptiveAsset[style*="position: fixed"]').click();
      await lightbox.waitFor({ state: 'detached' });
      await pagesSession.page.locator('.drawio-host').getByText('Page one', { exact: true }).waitFor();
      const returned = await assertLocalShapeGeometry(pagesSession, {
        scaled: true, phase: 'fullscreen-return',
      });
      for (const axis of ['width', 'height'])
        assert(Math.abs(returned.shapes[0].clientBox[axis] - fitted.shapes[0].clientBox[axis]) <= 0.25,
          `Fullscreen close changed the ordinary fixture screen ${axis}`);
      await clickViewerToolbar(pagesSession, 'Fullscreen');
      const reopened = pagesSession.page.locator('body > .geDiagramContainer');
      await reopened.locator('svg').waitFor();
      await reopened.getByText('Page one', { exact: true }).waitFor();
      await pagesSession.page.locator('body > img.geAdaptiveAsset[style*="position: fixed"]').click();
      await reopened.waitFor({ state: 'detached' });
      await clickNextPage(pagesSession);
      await pagesSession.page.locator('.drawio-host').getByText('AND', { exact: true }).waitFor();
      const pageTwoStencil = requests.slice(beforePageTwo).find(row =>
        row.path.includes('/static/drawio/stencils/electrical/logic_gates.xml'));
      assert(pageTwoStencil && pageTwoStencil.status === 200, 'page two did not load the AND stencil');
      await assertAndGeometry(pagesSession, 'page two');
      assert.deepEqual(caseErrors(pagesSession), [], 'multipage Drawio produced unexpected console errors');
    } catch (error) {
      await captureStandaloneFailure(pagesSession, 'pages');
      throw error;
    } finally {
      await closeStandalone(pagesSession);
    }

    const fullscreenThemeSession = await openStandalone('/ocu');
    const themeLabel = 'fullscreen-theme-close-icon';
    const themeEvidence = {
      case: themeLabel,
      browser: browser.version(),
      themeInput: 'actual parent instance darkMode=auto; real prefers-color-scheme media changes',
      closed: false,
    };
    let themeNodes;
    const themeCycleNodes = [];
    let themeFailure;
    try {
      await fullscreenThemeSession.page.emulateMedia({ colorScheme: 'light' });
      await selectStandaloneFile(fullscreenThemeSession.page, 'diagram.drawio');
      await fullscreenThemeSession.page.locator('.drawio-host').getByText('Local shape', { exact: true }).waitFor();
      await fullscreenThemeSession.page.evaluate(() => {
        const originalFactory = GraphViewer.prototype.showLocalLightbox;
        const ids = new Map();
        const listeners = parent => {
          const pairs = [];
          for (let index = 0; index < (parent.eventListeners || []).length; index += 2) {
            const callback = parent.eventListeners[index + 1];
            if (!ids.has(callback)) ids.set(callback, ids.size + 1);
            pairs.push({ event: parent.eventListeners[index], callback, id: ids.get(callback) });
          }
          return pairs;
        };
        const describe = pairs => pairs.map(({ event, id }) => ({ event, id }));
        const state = container => ({
          connected: container.isConnected,
          darkClass: container.classList.contains('geDarkMode'),
          colorScheme: container.style.colorScheme,
        });
        const trace = { phase: 'open', calls: [], sentinelEvents: [], children: [], sameParent: true };
        const observeFactory = function() {
          if (!trace.parent) {
            trace.parent = this;
            trace.parentContainer = this.graph.container;
            trace.originalMode = this.darkMode;
          }
          trace.sameParent = trace.sameParent && trace.parent === this;
          trace.phase = 'open';
          // Auto is test input on this instance; production defaults stay untouched.
          this.darkMode = 'auto';
          this.darkModeChanged();
          trace.preexisting = listeners(this);
          const ui = originalFactory.apply(this, arguments);
          trace.ui = ui;
          trace.childContainer = ui.editor.graph.container;
          trace.afterFactory = listeners(this);
          trace.owned = trace.afterFactory.filter(pair => pair.event === 'darkModeChanged' &&
            !trace.preexisting.some(before => before.callback === pair.callback));
          const child = {
            ui, container: trace.childContainer, owned: trace.owned,
            originalSetDarkMode: ui.setDarkMode, calls: [], phase: 'open', retired: false,
          };
          trace.children.push(child);
          trace.calls = child.calls;
          ui.setDarkMode = function() {
            child.calls.push({
              phase: child.phase, dark: arguments[0], receiverIsUi: this === ui,
            });
            return child.originalSetDarkMode.apply(this, arguments);
          };
          return ui;
        };
        trace.markClosed = () => {
          const child = trace.children.at(-1);
          child.phase = 'closed';
          child.retired = true;
          child.closedCalls = child.calls.length;
          trace.phase = 'closed';
        };
        trace.snapshot = () => {
          const current = listeners(trace.parent);
          return {
            phase: trace.phase,
            mediaDark: matchMedia('(prefers-color-scheme: dark)').matches,
            originalMode: trace.originalMode,
            parent: state(trace.parentContainer),
            child: state(trace.childContainer),
            calls: trace.calls.slice(),
            sentinelEvents: trace.sentinelEvents.slice(),
            preexisting: describe(trace.preexisting),
            afterFactory: describe(trace.afterFactory),
            owned: describe(trace.owned),
            retainedOwned: describe(current.filter(pair =>
              trace.owned.some(owned => owned.callback === pair.callback))),
            unrelated: describe(current.filter(pair =>
              !trace.owned.some(owned => owned.callback === pair.callback))),
            current: describe(current),
            sameParent: trace.sameParent,
            children: trace.children.map((child, index) => ({
              index, retired: child.retired, calls: child.calls.slice(),
              closedCalls: child.closedCalls, container: state(child.container),
              owned: describe(child.owned),
              retainedOwned: describe(current.filter(pair =>
                child.owned.some(owned => owned.callback === pair.callback))),
            })),
          };
        };
        trace.restore = () => {
          if (GraphViewer.prototype.showLocalLightbox === observeFactory)
            GraphViewer.prototype.showLocalLightbox = originalFactory;
          for (const child of trace.children) child.ui.setDarkMode = child.originalSetDarkMode;
          if (trace.sentinel) trace.parent.removeListener(trace.sentinel);
          if (trace.parent) trace.parent.darkMode = trace.originalMode;
        };
        trace.restorePrototype = () => {
          GraphViewer.prototype.showLocalLightbox = originalFactory;
        };
        GraphViewer.prototype.showLocalLightbox = observeFactory;
        window.__drawioThemeTrace = trace;
      });
      // The mounted guard captures the factory during construction, so construct a fresh viewer.
      await selectStandaloneFile(fullscreenThemeSession.page, 'pages.drawio');
      await fullscreenThemeSession.page.locator('.drawio-host').getByText('Page one', { exact: true }).waitFor();
      await selectStandaloneFile(fullscreenThemeSession.page, 'diagram.drawio');
      await fullscreenThemeSession.page.locator('.drawio-host').getByText('Local shape', { exact: true }).waitFor();
      const themeOverflow = await fullscreenThemeSession.page.evaluate(() => document.body.style.overflow);
      await clickViewerToolbar(fullscreenThemeSession, 'Fullscreen');
      themeNodes = await captureFullscreenNodes(fullscreenThemeSession, 'Local shape');
      themeEvidence.before = await fullscreenThemeSession.page.evaluate(() => {
        const trace = window.__drawioThemeTrace;
        trace.restorePrototype();
        // Outside the factory window, this listener also witnesses real event delivery.
        trace.sentinel = () => trace.sentinelEvents.push({
          phase: trace.phase, dark: trace.parent.isDarkMode(),
        });
        trace.parent.addListener('darkModeChanged', trace.sentinel);
        return trace.snapshot();
      });
      assert.equal(themeEvidence.before.owned.length, 1,
        `${themeLabel}: real factory did not register its single child theme callback`);
      await fullscreenThemeSession.page.emulateMedia({ colorScheme: 'dark' });
      await fullscreenThemeSession.page.waitForFunction(() => {
        const trace = window.__drawioThemeTrace;
        return trace.calls.some(call => call.dark === true) &&
          trace.sentinelEvents.some(event => event.dark === true) &&
          trace.parent.graph.container.classList.contains('geDarkMode') &&
          trace.childContainer.classList.contains('geDarkMode');
      });
      themeEvidence.open = await fullscreenThemeSession.page.evaluate(() => window.__drawioThemeTrace.snapshot());
      assert.equal(themeEvidence.open.parent.colorScheme, 'dark');
      assert.equal(themeEvidence.open.child.colorScheme, 'dark');
      assert(themeEvidence.open.calls.every(call => call.receiverIsUi),
        `${themeLabel}: actual UI theme calls lost their receiver`);
      assert.deepEqual(themeEvidence.open.current, themeEvidence.before.current,
        `${themeLabel}: live media change altered parent callback identities`);
      await fullscreenThemeSession.page.screenshot({ path: path.join(artifacts, `${themeLabel}-open.png`) });
      await fullscreenThemeSession.page.locator('body > img.geAdaptiveAsset[style*="position: fixed"]').click();
      await assertFullscreenRetired(fullscreenThemeSession, themeNodes, themeOverflow, themeLabel);
      themeEvidence.closed = true;
      themeEvidence.afterClose = await fullscreenThemeSession.page.evaluate(() => {
        const trace = window.__drawioThemeTrace;
        trace.markClosed();
        return trace.snapshot();
      });
      await fullscreenThemeSession.page.emulateMedia({ colorScheme: 'light' });
      await fullscreenThemeSession.page.waitForFunction(({ calls, events }) => {
        const trace = window.__drawioThemeTrace;
        return !matchMedia('(prefers-color-scheme: dark)').matches &&
          trace.parent.graph.container.style.colorScheme === 'light' &&
          (trace.sentinelEvents.length > events || trace.calls.length > calls);
      }, { calls: themeEvidence.afterClose.calls.length, events: themeEvidence.afterClose.sentinelEvents.length });
      themeEvidence.afterMedia = await fullscreenThemeSession.page.evaluate(() => window.__drawioThemeTrace.snapshot());
      await fullscreenThemeSession.page.locator('.drawio-host').getByText('Local shape', { exact: true }).waitFor();
      const themeWidthBefore = await fullscreenThemeSession.page.locator('.drawio-host svg').evaluate(svg =>
        svg.getBoundingClientRect().width);
      await clickViewerToolbar(fullscreenThemeSession, 'Zoom In');
      themeEvidence.parentZoom = {
        before: themeWidthBefore,
        after: await fullscreenThemeSession.page.locator('.drawio-host svg').evaluate(svg =>
          svg.getBoundingClientRect().width),
      };
      assert.equal(themeEvidence.afterMedia.calls.length, themeEvidence.afterClose.calls.length,
        `${themeLabel}: real media event called retired UI.setDarkMode; ` +
        `calls=${JSON.stringify(themeEvidence.afterMedia.calls)}, ` +
        `retainedCallbacks=${JSON.stringify(themeEvidence.afterMedia.retainedOwned)}`);
      assert.deepEqual(themeEvidence.afterMedia.retainedOwned, [],
        `${themeLabel}: closed UI theme callback remains registered on its parent`);
      assert.deepEqual(themeEvidence.afterClose.retainedOwned, [],
        `${themeLabel}: close icon did not release its exact child callback`);
      assert.deepEqual(themeEvidence.afterMedia.current, themeEvidence.before.unrelated,
        `${themeLabel}: closing child changed unrelated parent callback identities`);
      assert.deepEqual(themeEvidence.afterClose.unrelated, themeEvidence.before.unrelated,
        `${themeLabel}: close icon removed unrelated parent listeners`);
      assert.equal(themeEvidence.afterMedia.parent.connected, true);
      assert.equal(themeEvidence.afterMedia.parent.darkClass, false);
      assert.equal(themeEvidence.afterMedia.parent.colorScheme, 'light');
      assert.equal(themeEvidence.afterMedia.sentinelEvents.at(-1)?.dark, false,
        `${themeLabel}: surviving parent listener did not receive the later real media event`);
      assert(themeEvidence.parentZoom.after > themeEvidence.parentZoom.before,
        `${themeLabel}: surviving parent toolbar stopped responding`);
      assert.deepEqual(caseErrors(fullscreenThemeSession), [], `${themeLabel}: unexpected browser errors`);
      const assertRetiredThemeChildren = (snapshot, label) => {
        assert.equal(snapshot.sameParent, true, `${label}: fullscreen did not reuse the same parent`);
        for (const child of snapshot.children.filter(child => child.retired)) {
          assert.equal(child.calls.length, child.closedCalls,
            `${label}: later theme event called retired UI ${child.index}: ${JSON.stringify(child)}`);
          assert.deepEqual(child.retainedOwned, [],
            `${label}: parent retained retired UI ${child.index}'s exact theme callback`);
        }
      };
      assertRetiredThemeChildren(themeEvidence.afterMedia, themeLabel);
      themeEvidence.paths = [];
      for (const [cycle, closePath] of ['Escape', 'backdrop', 'icon', 'Escape', 'backdrop', 'retirement'].entries()) {
        const label = `fullscreen-theme-${closePath}-${cycle}`;
        const record = { case: label, closePath };
        themeEvidence.paths.push(record);
        await clickViewerToolbar(fullscreenThemeSession, 'Fullscreen');
        const nodes = await captureFullscreenNodes(fullscreenThemeSession, 'Local shape');
        themeCycleNodes.push(nodes);
        record.before = await fullscreenThemeSession.page.evaluate(() => window.__drawioThemeTrace.snapshot());
        assert.equal(record.before.children.length, cycle + 2, `${label}: unexpected real UI creation count`);
        assert.equal(record.before.owned.length, 1, `${label}: missing actual child theme subscription`);
        assertRetiredThemeChildren(record.before, label);
        assert.deepEqual(record.before.unrelated, themeEvidence.before.unrelated,
          `${label}: reopening changed unrelated callback identities`);
        await fullscreenThemeSession.page.emulateMedia({ colorScheme: 'dark' });
        await fullscreenThemeSession.page.waitForFunction(events => {
          const trace = window.__drawioThemeTrace;
          return trace.calls.some(call => call.dark === true) &&
            trace.sentinelEvents.length > events &&
            trace.parentContainer.classList.contains('geDarkMode') &&
            trace.childContainer.classList.contains('geDarkMode');
        }, record.before.sentinelEvents.length);
        record.open = await fullscreenThemeSession.page.evaluate(() => window.__drawioThemeTrace.snapshot());
        assertRetiredThemeChildren(record.open, label);
        assert.equal(record.open.child.connected, true);
        assert.equal(record.open.child.colorScheme, 'dark');
        assert.equal(record.open.parent.colorScheme, 'dark');
        assert(record.open.calls.every(call => call.receiverIsUi), `${label}: UI theme receiver changed`);
        assert.deepEqual(record.open.current, record.before.current, `${label}: theme dispatch changed callback identities`);
        await fullscreenThemeSession.page.screenshot({ path: path.join(artifacts, `${label}-open.png`) });
        if (closePath === 'Escape') {
          await fullscreenThemeSession.page.keyboard.press('Escape');
        } else if (closePath === 'backdrop') {
          record.hitTarget = await nodes.evaluate(nodes => {
            const hit = document.elementFromPoint(8, 8);
            return {
              x: 8, y: 8,
              isBackdrop: hit === nodes.find(({ role }) => role === 'backdrop').node,
              tag: hit?.tagName, className: hit?.className,
            };
          });
          assert.equal(record.hitTarget.isBackdrop, true,
            `${label}: real pointer target at the visible margin is not the captured backdrop`);
          await fullscreenThemeSession.page.mouse.click(8, 8);
        } else if (closePath === 'retirement') {
          await activateStandaloneFile(fullscreenThemeSession, 'pages.drawio');
          await fullscreenThemeSession.page.locator('.drawio-host').getByText('Page one', { exact: true }).waitFor();
        } else {
          await fullscreenThemeSession.page.locator('body > img.geAdaptiveAsset[style*="position: fixed"]').click();
        }
        await assertFullscreenRetired(fullscreenThemeSession, nodes, themeOverflow, label);
        record.afterClose = await fullscreenThemeSession.page.evaluate(() => {
          const trace = window.__drawioThemeTrace;
          trace.markClosed();
          trace.ui.destroy();
          trace.ui.destroy();
          return trace.snapshot();
        });
        assertRetiredThemeChildren(record.afterClose, label);
        const totalCalls = record.afterClose.children.reduce((sum, child) => sum + child.calls.length, 0);
        await fullscreenThemeSession.page.emulateMedia({ colorScheme: 'light' });
        await fullscreenThemeSession.page.waitForFunction(({ events, calls }) => {
          const trace = window.__drawioThemeTrace;
          return !matchMedia('(prefers-color-scheme: dark)').matches &&
            trace.parentContainer.style.colorScheme === 'light' &&
            (trace.sentinelEvents.length > events ||
              trace.children.reduce((sum, child) => sum + child.calls.length, 0) > calls);
        }, { events: record.afterClose.sentinelEvents.length, calls: totalCalls });
        record.afterMedia = await fullscreenThemeSession.page.evaluate(() => window.__drawioThemeTrace.snapshot());
        assertRetiredThemeChildren(record.afterMedia, label);
        assert.equal(record.afterMedia.parent.darkClass, false);
        assert.equal(record.afterMedia.parent.colorScheme, 'light');
        assert.equal(record.afterMedia.sentinelEvents.at(-1)?.dark, false,
          `${label}: unrelated sentinel did not receive the later media event`);
        if (closePath === 'retirement') {
          assert.equal(record.afterMedia.parent.connected, false, `${label}: old preview was not retired`);
          record.afterParentEvent = await fullscreenThemeSession.page.evaluate(() => {
            const trace = window.__drawioThemeTrace;
            trace.parent.fireEvent(new mxEventObject('darkModeChanged'));
            return trace.snapshot();
          });
          assertRetiredThemeChildren(record.afterParentEvent, label);
          assert.deepEqual(record.afterParentEvent.current, record.afterMedia.current,
            `${label}: real post-retirement dispatch changed the remaining parent listeners`);
          assert.deepEqual(record.afterParentEvent.current.filter(pair => pair.event === 'darkModeChanged'),
            themeEvidence.before.unrelated.filter(pair => pair.event === 'darkModeChanged'),
            `${label}: retirement removed unrelated theme listeners`);
          assert.equal(record.afterParentEvent.sentinelEvents.length, record.afterMedia.sentinelEvents.length + 1,
            `${label}: real old-parent dispatch did not reach the unrelated sentinel`);
          await clickNextPage(fullscreenThemeSession);
          await fullscreenThemeSession.page.locator('.drawio-host').getByText('AND', { exact: true }).waitFor();
          await assertAndGeometry(fullscreenThemeSession, label);
        } else {
          assert.equal(record.afterMedia.parent.connected, true);
          assert.deepEqual(record.afterMedia.current, themeEvidence.before.unrelated,
            `${label}: closing changed unrelated parent callback identities`);
          const before = await fullscreenThemeSession.page.locator('.drawio-host svg').evaluate(svg =>
            svg.getBoundingClientRect().width);
          await clickViewerToolbar(fullscreenThemeSession, 'Zoom In');
          record.parentZoom = { before, after: await fullscreenThemeSession.page.locator('.drawio-host svg')
            .evaluate(svg => svg.getBoundingClientRect().width) };
          assert(record.parentZoom.after > before, `${label}: parent toolbar stopped responding`);
        }
        assert.deepEqual(caseErrors(fullscreenThemeSession), [], `${label}: unexpected browser errors`);
        await fullscreenThemeSession.page.screenshot({ path: path.join(artifacts, `${label}.png`) });
      }
    } catch (error) {
      themeFailure = error;
      themeEvidence.failure = String(error);
      throw error;
    } finally {
      themeEvidence.errors = caseErrors(fullscreenThemeSession);
      const themeCaptureFailures = [];
      try {
        themeEvidence.final = await fullscreenThemeSession.page.evaluate(() => {
          const trace = window.__drawioThemeTrace;
          return trace?.ui ? trace.snapshot() : null;
        });
      } catch (error) { themeCaptureFailures.push(error); }
      console.log(JSON.stringify(themeEvidence));
      const captures = await Promise.allSettled([
        fullscreenThemeSession.page.screenshot({ path: path.join(artifacts, `${themeLabel}.png`) }),
        fs.writeFile(path.join(artifacts, `${themeLabel}.json`), JSON.stringify(themeEvidence, null, 2) + '\n'),
      ]);
      themeCaptureFailures.push(...captures.filter(result => result.status === 'rejected').map(result => result.reason));
      try {
        await fullscreenThemeSession.page.evaluate(() => {
          window.__drawioThemeTrace?.restore();
          delete window.__drawioThemeTrace;
        });
      } catch (error) { themeCaptureFailures.push(error); }
      try { await themeNodes?.dispose(); }
      catch (error) { themeCaptureFailures.push(error); }
      for (const nodes of themeCycleNodes) {
        try { await nodes.dispose(); }
        catch (error) { themeCaptureFailures.push(error); }
      }
      try { await closeStandalone(fullscreenThemeSession); }
      catch (error) { themeCaptureFailures.push(error); }
      for (const error of themeCaptureFailures)
        console.error(JSON.stringify({ case: themeLabel, artifactOrCleanupFailure: String(error) }));
      if (!themeFailure && themeCaptureFailures.length) throw themeCaptureFailures[0];
    }

    const factoryProbeSession = await openStandalone('/ocu');
    const factoryProbeLabel = 'fullscreen-theme-synthetic-factory-outcomes';
    const factoryProbeEvidence = { case: factoryProbeLabel, synthetic: true, outcomes: [] };
    let factoryProbeFailure;
    try {
      await selectStandaloneFile(factoryProbeSession.page, 'diagram.drawio');
      await factoryProbeSession.page.locator('.drawio-host').getByText('Local shape', { exact: true }).waitFor();
      await factoryProbeSession.page.evaluate(() => {
        const originalFactory = GraphViewer.prototype.showLocalLightbox;
        const probe = {
          mode: 'capture', callbackCalls: 0, sentinelCalls: 0,
          error: new Error('synthetic vendor factory failure'),
        };
        probe.callback = () => { probe.callbackCalls += 1; };
        const injectedFactory = function() {
          probe.parent = this;
          if (probe.mode === 'capture') return null;
          this.addListener('darkModeChanged', probe.callback);
          if (probe.mode === 'throw') throw probe.error;
          return null;
        };
        probe.restore = () => {
          GraphViewer.prototype.showLocalLightbox = originalFactory;
          if (probe.parent) {
            if (probe.originalDescriptor)
              Object.defineProperty(probe.parent, 'addListener', probe.originalDescriptor);
            else delete probe.parent.addListener;
            if (probe.sentinel) probe.parent.removeListener(probe.sentinel);
          }
        };
        probe.finishCapture = () => {
          GraphViewer.prototype.showLocalLightbox = originalFactory;
          probe.originalDescriptor = Object.getOwnPropertyDescriptor(probe.parent, 'addListener');
          probe.originalAddListener = probe.parent.addListener;
          probe.sentinel = () => { probe.sentinelCalls += 1; };
          probe.parent.addListener('darkModeChanged', probe.sentinel);
          return {
            canonicalWrapper: probe.parent.showLocalLightbox !== injectedFactory &&
              probe.parent.showLocalLightbox !== originalFactory,
            inheritedMethod: !probe.originalDescriptor,
          };
        };
        GraphViewer.prototype.showLocalLightbox = injectedFactory;
        window.__drawioFactoryProbe = probe;
      });
      await selectStandaloneFile(factoryProbeSession.page, 'pages.drawio');
      await factoryProbeSession.page.locator('.drawio-host').getByText('Page one', { exact: true }).waitFor();
      await selectStandaloneFile(factoryProbeSession.page, 'diagram.drawio');
      await factoryProbeSession.page.locator('.drawio-host').getByText('Local shape', { exact: true }).waitFor();
      await clickViewerToolbar(factoryProbeSession, 'Fullscreen');
      factoryProbeEvidence.capture = await factoryProbeSession.page.evaluate(() =>
        window.__drawioFactoryProbe.finishCapture());
      assert.equal(factoryProbeEvidence.capture.canonicalWrapper, true,
        `${factoryProbeLabel}: injected dependency bypassed the actual OCU wrapper`);
      assert.equal(factoryProbeEvidence.capture.inheritedMethod, true,
        `${factoryProbeLabel}: bootstrap left the parent's temporary method shadow installed`);
      for (const propertyState of ['inherited', 'own']) {
        for (const outcome of ['throw', 'null']) {
          const result = await factoryProbeSession.page.evaluate(({ propertyState, outcome }) => {
            const probe = window.__drawioFactoryProbe;
            const parent = probe.parent;
            if (propertyState === 'own') {
              Object.defineProperty(parent, 'addListener', {
                configurable: true, enumerable: true, writable: false, value: probe.originalAddListener,
              });
            } else {
              delete parent.addListener;
            }
            const descriptor = Object.getOwnPropertyDescriptor(parent, 'addListener');
            const before = (parent.eventListeners || []).slice();
            const sentinelCalls = probe.sentinelCalls;
            probe.mode = outcome;
            let returned;
            let caught;
            let threw = false;
            try { returned = parent.showLocalLightbox(); }
            catch (error) { threw = true; caught = error; }
            const restored = Object.getOwnPropertyDescriptor(parent, 'addListener');
            const methodRestored = parent.addListener === probe.originalAddListener &&
              (descriptor ? restored?.value === descriptor.value &&
                restored?.configurable === descriptor.configurable &&
                restored?.enumerable === descriptor.enumerable &&
                restored?.writable === descriptor.writable : restored === undefined);
            parent.fireEvent(new mxEventObject('darkModeChanged'));
            const after = parent.eventListeners || [];
            return {
              propertyState, outcome, threw,
              exactOriginalError: caught === probe.error,
              unexpectedError: threw && caught !== probe.error ? String(caught) : null,
              returned: returned === null ? 'null' : typeof returned,
              methodRestored,
              unrelatedListenersUnchanged: before.length === after.length &&
                before.every((value, index) => value === after[index]),
              capturedCallbackRetained: after.includes(probe.callback),
              capturedCallbackCalls: probe.callbackCalls,
              sentinelDeliveries: probe.sentinelCalls - sentinelCalls,
            };
          }, { propertyState, outcome });
          factoryProbeEvidence.outcomes.push(result);
          assert.equal(result.unexpectedError, null, `${factoryProbeLabel}: unexpected setup error`);
          assert.equal(result.threw, outcome === 'throw',
            `${factoryProbeLabel}: factory throw/return outcome was not preserved`);
          assert.equal(result.exactOriginalError, outcome === 'throw',
            `${factoryProbeLabel}: factory exception identity was not preserved`);
          if (outcome !== 'throw') assert.equal(result.returned, outcome,
            `${factoryProbeLabel}: no-UI factory return was not preserved`);
          assert.equal(result.methodRestored, true,
            `${factoryProbeLabel}: ${propertyState} addListener property state was not restored`);
          assert.equal(result.unrelatedListenersUnchanged, true,
            `${factoryProbeLabel}: ${outcome} changed unrelated parent callback identities`);
          assert.equal(result.capturedCallbackRetained, false,
            `${factoryProbeLabel}: ${outcome} retained its captured theme callback`);
          assert.equal(result.capturedCallbackCalls, 0,
            `${factoryProbeLabel}: post-${outcome} parent event reached the abandoned callback`);
          assert.equal(result.sentinelDeliveries, 1,
            `${factoryProbeLabel}: ${outcome} disabled unrelated theme delivery`);
          assert.equal(await factoryProbeSession.page.locator('body > .geDiagramContainer').count(), 0,
            `${factoryProbeLabel}: synthetic no-UI outcome mounted a popup`);
        }
      }
      await factoryProbeSession.page.locator('.drawio-host').getByText('Local shape', { exact: true }).waitFor();
      assert.deepEqual(caseErrors(factoryProbeSession), [], `${factoryProbeLabel}: unexpected browser errors`);
    } catch (error) {
      factoryProbeFailure = error;
      factoryProbeEvidence.failure = String(error);
      throw error;
    } finally {
      factoryProbeEvidence.errors = caseErrors(factoryProbeSession);
      console.log(JSON.stringify(factoryProbeEvidence));
      const captures = await Promise.allSettled([
        factoryProbeSession.page.screenshot({ path: path.join(artifacts, `${factoryProbeLabel}.png`) }),
        fs.writeFile(path.join(artifacts, `${factoryProbeLabel}.json`),
          JSON.stringify(factoryProbeEvidence, null, 2) + '\n'),
      ]);
      const failures = captures.filter(result => result.status === 'rejected').map(result => result.reason);
      try {
        await factoryProbeSession.page.evaluate(() => {
          window.__drawioFactoryProbe?.restore();
          delete window.__drawioFactoryProbe;
        });
      } catch (error) { failures.push(error); }
      try { await closeStandalone(factoryProbeSession); }
      catch (error) { failures.push(error); }
      for (const error of failures)
        console.error(JSON.stringify({ case: factoryProbeLabel, artifactOrCleanupFailure: String(error) }));
      if (!factoryProbeFailure && failures.length) throw failures[0];
    }

    const fullscreenSelectionSession = await openStandalone('/ocu');
    let oldFullscreenNodes;
    try {
      await selectStandaloneFile(fullscreenSelectionSession.page, 'diagram.drawio');
      await fullscreenSelectionSession.page.locator('.drawio-host').getByText('Local shape', { exact: true }).waitFor();
      await clickViewerToolbar(fullscreenSelectionSession, 'Fullscreen');
      oldFullscreenNodes = await captureFullscreenNodes(fullscreenSelectionSession, 'Local shape');
      await fullscreenSelectionSession.page.screenshot({ path: path.join(artifacts, 'fullscreen-selection-a.png') });

      await activateStandaloneFile(fullscreenSelectionSession, 'pages.drawio');
      await fullscreenSelectionSession.page.locator('.drawio-host').getByText('Page one', { exact: true }).waitFor();
      await assertLocalShapeGeometry(fullscreenSelectionSession, { phase: 'selected-b' });
      const transition = await fullscreenSelectionSession.page.evaluate((nodes) => ({
        selected: document.querySelector('.file-selector-btn .selector-name').textContent,
        replacementRendered: document.querySelector('.drawio-host').textContent.includes('Page one'),
        oldFullscreenContent: nodes.find(({ role }) => role === 'diagram').node.textContent,
        remaining: nodes.filter(({ node }) => node.isConnected).map(({ role }) => role).sort(),
      }), oldFullscreenNodes);
      await fullscreenSelectionSession.page.screenshot({ path: path.join(artifacts, 'fullscreen-selection-b.png') });
      assert.deepEqual(transition.remaining, [],
        `selecting diagram B left diagram A's body-owned fullscreen UI: ${JSON.stringify(transition)}`);
      await clickNextPage(fullscreenSelectionSession);
      await fullscreenSelectionSession.page.locator('.drawio-host').getByText('AND', { exact: true }).waitFor();
      await assertAndGeometry(fullscreenSelectionSession, 'diagram B after fullscreen retirement');
      assert.deepEqual(caseErrors(fullscreenSelectionSession), [],
        'fullscreen selection retirement produced unexpected console errors');
    } catch (error) {
      await captureStandaloneFailure(fullscreenSelectionSession, 'fullscreen-selection');
      throw error;
    } finally {
      await oldFullscreenNodes?.dispose();
      await closeStandalone(fullscreenSelectionSession);
    }

    const fullscreenA = files.find(file => file.file_id === 'drawio');
    const fullscreenB = files.find(file => file.file_id === 'drawio-pages');
    const fullscreenListingCases = [
      { label: 'auto-new', initial: [fullscreenA], next: [fullscreenA, fullscreenB],
        selected: 'pages.drawio', content: 'Page one' },
      { label: 'rename', initial: [fullscreenA, fullscreenB],
        next: [{ ...fullscreenA, path: 'renamed/renamed.drawio' }, fullscreenB],
        selected: 'renamed/renamed.drawio', content: 'Local shape' },
      { label: 'revision', initial: [fullscreenA, fullscreenB],
        next: [{ ...fullscreenA, revision: 2 }, fullscreenB],
        selected: 'diagram.drawio', content: 'Compressed shape' },
      { label: 'removal-fallback', initial: [fullscreenA, fullscreenB], next: [fullscreenB],
        selected: 'pages.drawio', content: 'Page one' },
      { label: 'empty', initial: [fullscreenA], next: [], selected: null },
    ];
    for (const scenario of fullscreenListingCases) {
      standaloneListing = scenario.initial;
      const session = await openStandalone('/ocu');
      let nodes;
      let oldStage;
      const originalDiagram = bytes['diagram.drawio'];
      try {
        await session.page.locator('.drawio-host').getByText('Local shape', { exact: true }).waitFor();
        await session.page.evaluate(() => { document.body.style.overflow = 'auto'; });
        await clickViewerToolbar(session, 'Fullscreen');
        nodes = await captureFullscreenNodes(session, 'Local shape');
        oldStage = await session.page.locator('.preview-stage').elementHandle();
        standaloneListing = scenario.next;
        if (scenario.label === 'rename') bytes['renamed/renamed.drawio'] = originalDiagram;
        if (scenario.label === 'revision') bytes['diagram.drawio'] = bytes['compressed.drawio'];
        const beforeRefresh = requests.length;
        await session.page.evaluate(() => window.__runAppPoll());
        assert.equal(requests.slice(beforeRefresh).filter(row =>
          row.method === 'GET' && row.path === '/ocu/api/outputs/' + CHAT).length, 1,
        `${scenario.label} did not use one actual listing refresh`);
        await session.page.waitForFunction(stage => !stage.isConnected, oldStage);
        if (scenario.selected) {
          await session.page.waitForFunction(expected =>
            document.querySelector('.file-selector-btn .selector-name')?.textContent === expected, scenario.selected);
          await session.page.locator('.drawio-host').getByText(scenario.content, { exact: true }).waitFor();
          if (scenario.label === 'revision')
            await assertLocalShapeGeometry(session, { phase: 'revision-replacement' });
        } else {
          await session.page.locator('.preview .empty-state').waitFor();
          assert.equal(await session.page.locator('.drawio-host, .file-selector').count(), 0,
            'empty listing retained a selected preview or selector');
        }
        await assertFullscreenRetired(session, nodes, 'auto', scenario.label);
        if (scenario.selected) {
          const beforeZoom = await session.page.locator('.drawio-host svg').evaluate(svg => svg.getBoundingClientRect().width);
          await clickViewerToolbar(session, 'Zoom In');
          const afterZoom = await session.page.locator('.drawio-host svg').evaluate(svg => svg.getBoundingClientRect().width);
          assert(afterZoom > beforeZoom, `${scenario.label} replacement viewer did not accept Zoom In`);
        } else {
          standaloneListing = [fullscreenB];
          await session.page.evaluate(() => window.__runAppPoll());
          await session.page.locator('.drawio-host').getByText('Page one', { exact: true }).waitFor();
          await clickNextPage(session);
          await session.page.locator('.drawio-host').getByText('AND', { exact: true }).waitFor();
          await assertAndGeometry(session, 'viewer recovered after empty listing');
        }
        assert.deepEqual(caseErrors(session), [], `${scenario.label} retirement produced unexpected console errors`);
        await session.page.screenshot({ path: path.join(artifacts, `fullscreen-listing-${scenario.label}.png`) });
      } catch (error) {
        await captureStandaloneFailure(session, `fullscreen-listing-${scenario.label}`);
        throw error;
      } finally {
        standaloneListing = null;
        bytes['diagram.drawio'] = originalDiagram;
        delete bytes['renamed/renamed.drawio'];
        await nodes?.dispose();
        await oldStage?.dispose();
        await closeStandalone(session);
      }
    }

    const fullscreenCyclesSession = await openStandalone('/ocu');
    const cycleNodes = [];
    try {
      await selectStandaloneFile(fullscreenCyclesSession.page, 'diagram.drawio');
      await fullscreenCyclesSession.page.locator('.drawio-host').getByText('Local shape', { exact: true }).waitFor();
      await fullscreenCyclesSession.page.evaluate(() => { document.body.style.overflow = 'scroll'; });
      for (let cycle = 0; cycle < 3; cycle++) {
        await clickViewerToolbar(fullscreenCyclesSession, 'Fullscreen');
        const retired = await captureFullscreenNodes(fullscreenCyclesSession, 'Local shape');
        cycleNodes.push(retired);
        await activateStandaloneFile(fullscreenCyclesSession, 'pages.drawio');
        await fullscreenCyclesSession.page.locator('.drawio-host').getByText('Page one', { exact: true }).waitFor();
        await assertFullscreenRetired(fullscreenCyclesSession, retired, 'scroll', `retirement cycle ${cycle}`);
        for (const closePath of ['button', 'Escape']) {
          await clickViewerToolbar(fullscreenCyclesSession, 'Fullscreen');
          const closed = await captureFullscreenNodes(fullscreenCyclesSession, 'Page one');
          cycleNodes.push(closed);
          if (closePath === 'button')
            await fullscreenCyclesSession.page.locator('body > img.geAdaptiveAsset[style*="position: fixed"]').click();
          else await fullscreenCyclesSession.page.keyboard.press('Escape');
          await fullscreenCyclesSession.page.locator('body > .geDiagramContainer').waitFor({ state: 'detached' });
          await assertFullscreenRetired(fullscreenCyclesSession, closed, 'scroll', `${closePath} cycle ${cycle}`);
        }
        await clickNextPage(fullscreenCyclesSession);
        await fullscreenCyclesSession.page.locator('.drawio-host').getByText('AND', { exact: true }).waitFor();
        await assertAndGeometry(fullscreenCyclesSession, `reopened viewer cycle ${cycle}`);
        await selectStandaloneFile(fullscreenCyclesSession.page, 'diagram.drawio');
        await fullscreenCyclesSession.page.locator('.drawio-host').getByText('Local shape', { exact: true }).waitFor();
      }
      assert.deepEqual(caseErrors(fullscreenCyclesSession), [], 'repeated fullscreen cycles produced unexpected console errors');
      await fullscreenCyclesSession.page.screenshot({ path: path.join(artifacts, 'fullscreen-cycles.png') });
    } catch (error) {
      await captureStandaloneFailure(fullscreenCyclesSession, 'fullscreen-cycles');
      throw error;
    } finally {
      for (const nodes of cycleNodes) await nodes.dispose();
      await closeStandalone(fullscreenCyclesSession);
    }

    for (const newerFullscreen of [false, true]) {
      const label = newerFullscreen ? 'deferred-newer' : 'deferred-retirement';
      const session = await openStandalone('/ocu');
      let pendingNodes;
      let newerNodes;
      try {
        await selectStandaloneFile(session.page, 'diagram.drawio');
        await session.page.locator('.drawio-host').getByText('Local shape', { exact: true }).waitFor();
        await session.page.evaluate(() => { document.body.style.overflow = 'auto'; });
        await revealViewerToolbar(session);
        pendingNodes = await session.page.getByTitle('Fullscreen', { exact: true }).evaluateHandle(button => {
          const before = new Set(document.body.children);
          const schedule = window.setTimeout;
          const pending = [];
          // Hold every zero-delay callback from this actual click, including ownership settlement.
          window.setTimeout = (callback, delay, ...args) => {
            if (delay !== 0) return schedule(callback, delay, ...args);
            const timer = schedule(() => {}, 60000);
            pending.push({ callback, args, timer });
            return timer;
          };
          try { button.click(); } finally { window.setTimeout = schedule; }
          window.__releaseFullscreenMount = () => {
            const added = [];
            const observer = new MutationObserver(() => {});
            observer.observe(document.body, { childList: true });
            for (const { callback, args, timer } of pending.splice(0)) {
              window.clearTimeout(timer);
              callback.apply(window, args);
            }
            for (const record of observer.takeRecords()) added.push(...record.addedNodes);
            observer.disconnect();
            return added.filter(node => node.nodeType === 1 && node.isConnected)
              .map(node => ({ tag: node.tagName, class: node.className }));
          };
          return [...document.body.children].filter(node => !before.has(node));
        });
        assert.equal(await session.page.locator('body > .geDiagramContainer').count(), 0,
          `${label} fullscreen mounted before the controlled deferred boundary`);
        assert.equal(await pendingNodes.evaluate(nodes => nodes.some(node =>
          node.style.position === 'fixed' && node.style.backgroundColor === 'rgb(0, 0, 0)')), true,
        `${label} did not open the real fullscreen backdrop`);
        await activateStandaloneFile(session, 'pages.drawio');
        await session.page.locator('.drawio-host').getByText('Page one', { exact: true }).waitFor();
        if (newerFullscreen) {
          // A's pending backdrop still covers B; dispatch the real mounted toolbar control.
          await session.page.locator('.drawio-host').dispatchEvent('mousemove');
          await session.page.locator('.drawio-host').dispatchEvent('mouseenter');
          await session.page.getByTitle('Fullscreen', { exact: true }).evaluate(node => node.click());
          newerNodes = await captureFullscreenNodes(session, 'Page one');
        }
        const resurrected = await session.page.evaluate(() => window.__releaseFullscreenMount());
        assert.deepEqual(resurrected, [], `${label} resurrected deferred A fullscreen nodes`);
        assert.equal(await pendingNodes.evaluate(nodes => nodes.some(node => node.isConnected)), false,
          `${label} retained A's pending body UI`);
        if (newerFullscreen) {
          assert.deepEqual(await newerNodes.evaluate(nodes =>
            nodes.filter(({ node }) => node.isConnected).map(({ role }) => role).sort()),
          ['backdrop', 'close', 'diagram', 'toolbar'], 'old deferred cleanup removed newer fullscreen UI');
          assert.equal(await session.page.evaluate(() => document.body.style.overflow), 'hidden',
            'old deferred cleanup unlocked scrolling underneath newer fullscreen');
          await clickFullscreenNextAnd(session);
          await session.page.locator('body > .geDiagramContainer').getByText('AND', { exact: true }).waitFor();
          await session.page.keyboard.press('Escape');
          await session.page.locator('body > .geDiagramContainer').waitFor({ state: 'detached' });
          await assertFullscreenRetired(session, newerNodes, 'auto', label);
        } else {
          assert.equal(await session.page.evaluate(() => document.body.style.overflow), 'auto',
            'deferred retirement did not restore scrolling');
          await clickNextPage(session);
          await session.page.locator('.drawio-host').getByText('AND', { exact: true }).waitFor();
          await assertAndGeometry(session, label);
        }
        assert.deepEqual(caseErrors(session), [], `${label} produced unexpected console errors`);
        await session.page.screenshot({ path: path.join(artifacts, `fullscreen-${label}.png`) });
      } catch (error) {
        await captureStandaloneFailure(session, label);
        throw error;
      } finally {
        try {
          await session.page.evaluate(() => {
            window.__releaseFullscreenMount?.();
            delete window.__releaseFullscreenMount;
          });
        } finally {
          await pendingNodes?.dispose();
          await newerNodes?.dispose();
          await closeStandalone(session);
        }
      }
    }

    drawioMissingLazy = true;
    const pagesMissingSession = await openStandalone('/ocu');
    try {
      await selectStandaloneFile(pagesMissingSession.page, 'pages-missing.drawio');
      await pagesMissingSession.page.locator('.drawio-host svg').waitFor();
      await pagesMissingSession.page.locator('.drawio-host').getByText('Page one', { exact: true }).waitFor();
      assert.equal(await pagesMissingSession.page.locator('.dl-error').count(), 0);
      await clickNextPage(pagesMissingSession);
      await pagesMissingSession.page.locator('.dl-error').waitFor();
      assert.equal(await pagesMissingSession.page.locator('.drawio-host svg').count(), 0);
      acceptMissingStencilErrors(pagesMissingSession);
    } catch (error) {
      await captureStandaloneFailure(pagesMissingSession, 'pages-missing');
      throw error;
    } finally {
      drawioMissingLazy = false;
      await closeStandalone(pagesMissingSession);
    }

    drawioMissingLazy = true;
    const fullscreenMissingSession = await openStandalone('/ocu');
    try {
      await selectStandaloneFile(fullscreenMissingSession.page, 'pages-missing.drawio');
      await fullscreenMissingSession.page.locator('.drawio-host svg').waitFor();
      await clickViewerToolbar(fullscreenMissingSession, 'Fullscreen');
      const missingLightbox = fullscreenMissingSession.page.locator('body > .geDiagramContainer');
      await missingLightbox.getByText('Page one', { exact: true }).waitFor();
      await clickFullscreenNextAnd(fullscreenMissingSession);
      await fullscreenMissingSession.page.locator('.dl-error').waitFor();
      assert.equal(await missingLightbox.locator('svg').count(), 0);
      await assertStandaloneChrome(fullscreenMissingSession, 'fullscreen-missing');
      acceptMissingStencilErrors(fullscreenMissingSession);
    } catch (error) {
      await captureStandaloneFailure(fullscreenMissingSession, 'fullscreen-missing');
      throw error;
    } finally {
      drawioMissingLazy = false;
      await closeStandalone(fullscreenMissingSession);
    }

    drawioCorruptLazy = true;
    const fullscreenCorruptSession = await openStandalone('/ocu');
    try {
      await selectStandaloneFile(fullscreenCorruptSession.page, 'pages-missing.drawio');
      await fullscreenCorruptSession.page.locator('.drawio-host svg').waitFor();
      await clickViewerToolbar(fullscreenCorruptSession, 'Fullscreen');
      const corruptLightbox = fullscreenCorruptSession.page.locator('body > .geDiagramContainer');
      await corruptLightbox.getByText('Page one', { exact: true }).waitFor();
      await clickFullscreenNextAnd(fullscreenCorruptSession);
      await fullscreenCorruptSession.page.locator('.dl-error').waitFor();
      assert.equal(await corruptLightbox.locator('svg').count(), 0);
      await fullscreenCorruptSession.page.locator('body > .geDiagramContainer').waitFor({ state: 'detached' });
      await assertStandaloneChrome(fullscreenCorruptSession, 'fullscreen-corrupt');
      acceptOwnedErrors(fullscreenCorruptSession, isDrawioRenderError);
      const afterCorruptFullscreenErrors = consoleErrors.length;
      const afterCorruptFullscreenRequests = requests.length;
      drawioCorruptLazy = false;
      await selectStandaloneFile(fullscreenCorruptSession.page, 'hostile.docx');
      await fullscreenCorruptSession.page.locator('.preview-stage a').filter({ hasText: 'Unsafe link' }).waitFor();
      await selectStandaloneFile(fullscreenCorruptSession.page, 'pages-missing.drawio');
      await fullscreenCorruptSession.page.locator('.drawio-host svg').waitFor();
      await clickViewerToolbar(fullscreenCorruptSession, 'Fullscreen');
      const recoveredLightbox = fullscreenCorruptSession.page.locator('body > .geDiagramContainer');
      await recoveredLightbox.getByText('Page one', { exact: true }).waitFor();
      await clickFullscreenNextAnd(fullscreenCorruptSession);
      await recoveredLightbox.getByText('AND', { exact: true }).waitFor();
      const recovered = requests.slice(afterCorruptFullscreenRequests).find(row =>
        row.path.includes('/static/drawio/stencils/electrical/logic_gates.xml'));
      assert(recovered && recovered.status === 200, 'fullscreen corrupt retry did not receive HTTP 200');
      const recoveredGeometry = await recoveredLightbox.locator('svg').evaluate((svg) => {
        const paths = [...svg.querySelectorAll('path')].map((path) => path.getAttribute('d') || '');
        return {
          curved: paths.some((d) => /[AaCcQqSs]/.test(d)),
          wired: paths.some((d) => d.includes('M') && d.includes('L') && !/[AaCcQqSs]/.test(d)),
          pathCount: paths.length,
        };
      });
      assert.equal(recoveredGeometry.curved, true, 'fullscreen corrupt retry did not render the curved AND body');
      assert.deepEqual(caseErrors(fullscreenCorruptSession, afterCorruptFullscreenErrors), [],
        'fullscreen corrupt retry produced unexpected console errors');
    } catch (error) {
      await captureStandaloneFailure(fullscreenCorruptSession, 'fullscreen-corrupt');
      throw error;
    } finally {
      drawioCorruptLazy = false;
      await closeStandalone(fullscreenCorruptSession);
    }

    drawioCorruptLazy = true;
    const corruptLazySession = await openStandalone('/ocu');
    try {
      await selectStandaloneFile(corruptLazySession.page, 'corrupt-lazy.drawio');
      await corruptLazySession.page.locator('.dl-error').waitFor();
      assert.equal(await corruptLazySession.page.locator('.drawio-host svg').count(), 0);
      acceptOwnedErrors(corruptLazySession, isDrawioRenderError);
      const afterCorruptErrors = consoleErrors.length;
      const afterCorruptRequests = requests.length;
      drawioCorruptLazy = false;
      await selectStandaloneFile(corruptLazySession.page, 'hostile.docx');
      await corruptLazySession.page.locator('.preview-stage a').filter({ hasText: 'Unsafe link' }).waitFor();
      await selectStandaloneFile(corruptLazySession.page, 'corrupt-lazy.drawio');
      await corruptLazySession.page.locator('.drawio-host svg').waitFor();
      const recovered = requests.slice(afterCorruptRequests).find(row =>
        row.path.includes('/static/drawio/stencils/electrical/logic_gates.xml'));
      assert(recovered && recovered.status === 200, 'corrupt stencil retry did not receive HTTP 200');
      await assertAndGeometry(corruptLazySession, 'corrupt stencil retry');
      assert.deepEqual(caseErrors(corruptLazySession, afterCorruptErrors), [],
        'corrupt stencil retry produced unexpected console errors');
    } catch (error) {
      await captureStandaloneFailure(corruptLazySession, 'corrupt-lazy');
      throw error;
    } finally {
      drawioCorruptLazy = false;
      await closeStandalone(corruptLazySession);
    }

    const unprefixedSession = await openStandalone('');
    try {
      await selectStandaloneFile(unprefixedSession.page, 'lazy.drawio');
      await unprefixedSession.page.locator('.drawio-host svg').waitFor();
      await assertAndGeometry(unprefixedSession, 'empty-prefix AND');
      const unprefixedRequests = caseRequests(unprefixedSession);
      assert(unprefixedRequests.some(row =>
        row.path === '/static/drawio/stencils/electrical/logic_gates.xml' && row.status === 200),
        'empty prefix did not load the electrical stencil');
      await selectStandaloneFile(unprefixedSession.page, 'image.drawio');
      await decodeImageHref(unprefixedSession, '/static/drawio/img/telecommunication/Cellphone_128x128.png');
      await selectStandaloneFile(unprefixedSession.page, 'math.drawio');
      await assertMathGlyphs(unprefixedSession);
      assert(caseRequests(unprefixedSession).some(row => row.path.startsWith('/static/drawio/math4/')),
        'empty prefix did not load unprefixed MathJax materials');
      assert.deepEqual(caseErrors(unprefixedSession), [], 'empty-prefix Drawio produced unexpected console errors');
    } catch (error) {
      await captureStandaloneFailure(unprefixedSession, 'empty-prefix');
      throw error;
    } finally {
      await closeStandalone(unprefixedSession);
    }

    const nestedSession = await openStandalone('/tools/ocu');
    try {
      await selectStandaloneFile(nestedSession.page, 'lazy.drawio');
      await nestedSession.page.locator('.drawio-host svg').waitFor();
      await assertAndGeometry(nestedSession, 'nested-prefix AND');
      const nestedRequests = caseRequests(nestedSession);
      assert(nestedRequests.some(row =>
        row.path === '/tools/ocu/static/drawio/stencils/electrical/logic_gates.xml' && row.status === 200),
        'nested prefix did not load the electrical stencil');
      await selectStandaloneFile(nestedSession.page, 'image.drawio');
      await decodeImageHref(nestedSession, '/tools/ocu/static/drawio/img/telecommunication/Cellphone_128x128.png');
      await selectStandaloneFile(nestedSession.page, 'math.drawio');
      await assertMathGlyphs(nestedSession);
      assert(caseRequests(nestedSession).some(row => row.path.startsWith('/tools/ocu/static/drawio/math4/')),
        'nested prefix did not load prefixed MathJax materials');
      assert.deepEqual(caseErrors(nestedSession), [], 'nested-prefix Drawio produced unexpected console errors');
    } catch (error) {
      await captureStandaloneFailure(nestedSession, 'nested-prefix');
      throw error;
    } finally {
      await closeStandalone(nestedSession);
    }

    listMode = 'normal';
    assert.deepEqual(externalRequests, [], 'Office content requested an external resource');
    const abortLogs = consoleErrors.filter(error => error.text.includes('net::ERR_ABORTED'));
    assert(abortLogs.length <= expectedAbortUrls.length, 'unaccounted aborted-resource console errors');
    assert.deepEqual(abortLogs.filter(error => !expectedAbortUrls.includes(error.url)),
      [], 'unexpected aborted-resource URL');
    assert.deepEqual(unexpectedNetworkFailures, [], 'unexpected network failure');
    const expectedOwned = new Set(expectedOwnedErrors);
    assert.deepEqual(consoleErrors.filter(error =>
      !error.text.includes('net::ERR_ABORTED') && !expectedOwned.has(error)),
      [], 'unexpected browser console errors');
    console.log(JSON.stringify({ result: 'ok', screenshots: artifacts, listingRequests: requests.filter(r => r.path.includes('/api/outputs/')).length }));
  } finally {
    await browser.close();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  for (const response of held.splice(0)) response.destroy();
  for (const response of heldListings.splice(0)) response.destroy();
  for (const response of heldSheets.splice(0)) response.destroy();
  for (const response of heldDrawio.splice(0)) response.destroy();
  for (const pending of heldViewer.splice(0)) pending.res.destroy();
  for (const pending of heldImages.splice(0)) pending.res.destroy();
  for (const response of heldBrowserStatuses.splice(0)) response.destroy();
  for (const response of heldBrowserPages.splice(0)) response.destroy();
  for (const upgrade of heldUpgrades.splice(0)) upgrade.socket.destroy();
  for (const socket of activeSockets) socket.destroy();
  await new Promise(resolve => server.close(resolve));
  for (const standin of officeStandins) standin.closeAllConnections();
  await Promise.all(officeStandins.map(standin => new Promise(resolve => standin.close(resolve))));
});
