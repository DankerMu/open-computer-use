// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
// Run from repository root: node tests/orchestrator/preview_embedding_browser.cjs
// Requires the declared playwright 1.62.1 package and its Chromium browser.
const assert = require('node:assert/strict');
const { createHash, randomBytes } = require('node:crypto');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
let playwright;
try { playwright = require('playwright'); }
catch { playwright = require('@playwright/test'); }

const ROOT = path.resolve(__dirname, '../..');
const STATIC = path.join(ROOT, 'computer-use-server/static');
const CHAT = 'a1b2c3d4-e5f6-7890-abcd-ef1234567890';
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
  { file_id: 'html-doc', path: 'spoof.docx', type: 'docx', mime: 'text/html; charset=utf-8' },
  { file_id: 'xml-doc', path: 'text-xml.docx', type: 'docx', mime: 'text/xml' },
  { file_id: 'plusxml', path: 'suffix.docx', type: 'docx', mime: 'application/vnd.example+xml' },
  { file_id: 'foreign', path: 'foreign.docx', type: 'docx', mime: OFFICE_MIME.docx, url: 'https://example.invalid/foreign.docx' },
  { file_id: 'cross-chat', path: 'cross-chat.docx', type: 'docx', mime: OFFICE_MIME.docx, url: '/ocu/files/another-chat/cross-chat.docx' },
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
let injectedListingFailure = false;
const bytes = {};
const contentTypes = { '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2' };
const respond = (res, status, body, type = 'application/json', headers = {}) => {
  if (res.destroyed) return;
  res.writeHead(status, { 'Content-Type': type, 'Cache-Control': 'no-store', ...headers });
  res.end(body);
};
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  const prefix = url.pathname.startsWith('/tools/ocu/') ? '/tools/ocu' : '/ocu';
  requests.push({ path: url.pathname, cursor: url.searchParams.get('cursor'), header: req.headers['x-requested-with'] });
  try {
    if (url.pathname.startsWith(`${prefix}/static/`)) {
      const name = decodeURIComponent(url.pathname.slice(`${prefix}/static/`.length));
      const filename = path.resolve(STATIC, name);
      if (!filename.startsWith(STATIC + path.sep)) return respond(res, 404, 'Not found', 'text/plain');
      return respond(res, 200, await fs.readFile(filename), contentTypes[path.extname(filename)] || 'application/octet-stream');
    }
    if (url.pathname === `${prefix}/preview/${CHAT}`) {
      const origin = `http://${req.headers.host}`;
      const embed = url.searchParams.getAll('embed');
      const runtime = embed.length === 1 && ['browser', 'terminal'].includes(embed[0]);
      const nonce = runtime ? randomBytes(18).toString('base64url') : '';
      const policy = runtime
        ? `default-src 'none'; script-src 'self' 'nonce-${nonce}'; ` +
          "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; " +
          "connect-src 'self'; frame-src 'none'; base-uri 'none'; object-src 'none'; " +
          "form-action 'none'; frame-ancestors 'self'"
        : null;
      const html = `<!DOCTYPE html><html><head><meta charset="utf-8">
        <link rel="stylesheet" href="${prefix}/static/preview.css">
        <link rel="stylesheet" href="${prefix}/static/github.min.css">
        <link rel="stylesheet" href="${prefix}/static/katex/katex.min.css">
        <link rel="stylesheet" href="${prefix}/static/xterm.css">
        <script src="${prefix}/static/highlight.min.js"></script>
        <script src="${prefix}/static/highlightjs-line-numbers.min.js"></script>
        <script src="${prefix}/static/marked.min.js"></script>
        <script src="${prefix}/static/xterm.min.js"></script>
        <script src="${prefix}/static/xterm-addon-fit.min.js"></script>
        <script src="${prefix}/static/xterm-addon-web-links.min.js"></script>
        </head><body><div id="app"></div><script${runtime ? ` nonce="${nonce}"` : ''}>window.__CONFIG__ = {
          apiUrl:'${prefix}/api/outputs/${CHAT}', filesBase:'${prefix}/files/${CHAT}', chatId:'${CHAT}',
          describeUrl:'/api/v1/ocu/workspaces/${CHAT}' };</script>
          <script type="module" src="${prefix}/static/preview.js"></script></body></html>`;
      assert.equal(new URL(origin).hostname, '127.0.0.1');
      return respond(res, 200, html, 'text/html', policy ? { 'Content-Security-Policy': policy } : {});
    }
    if (url.pathname === `${prefix}/api/outputs/${CHAT}`) {
      if (listMode === 'failure') {
        injectedListingFailure = true;
        return respond(res, 503, '{}');
      }
      if (listMode === 'timeout') { heldListings.push(res); return; }
      if (listMode === 'standalone') {
        const file = { file_id: 'hostile', path: 'hostile.docx', name: 'hostile.docx',
          type: 'docx', mime: OFFICE_MIME.docx, size: 128, revision: 1,
          url: `/ocu/files/${CHAT}/hostile.docx` };
        return respond(res, 200, JSON.stringify({
          chat_id: CHAT, files: [file], total: 1, revision: 1, next_cursor: null,
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
    if (url.pathname.startsWith(`${prefix}/files/${CHAT}/`)) {
      if (url.pathname.endsWith('/slow.docx')) { held.push(res); slowRequested(); return; }
      if (url.pathname.endsWith('/slow.xlsx')) { heldSheets.push(res); sheetRequested(); return; }
      const name = decodeURIComponent(url.pathname.slice(`${prefix}/files/${CHAT}/`.length));
      if (!(name in bytes)) return respond(res, 404, 'Not found', 'text/plain');
      return respond(res, 200, bytes[name], 'application/octet-stream');
    }
    if (url.pathname === `${prefix}/browser/${CHAT}/status`) {
      if (browserFixture === 'held') { heldBrowserStatuses.push(res); return; }
      if (browserFixture === 'failure') return respond(res, 503, '{}');
      if (browserFixture === 'malformed') return respond(res, 200, '{"active":"yes"}');
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
    if (url.pathname === `${prefix}/terminal/${CHAT}/start-ttyd` && req.method === 'POST')
      return respond(res, 200, '{"already_running":true}');
    if (url.pathname === `${prefix}/terminal/${CHAT}/processes`) return respond(res, 200, JSON.stringify({ processes: [] }));
    if (url.pathname === `${prefix}/terminal/${CHAT}/status`) return respond(res, 200, JSON.stringify({ active: false }));
    if (url.pathname === `${prefix}/terminal/${CHAT}/sessions`) return respond(res, 200, JSON.stringify({ sessions: [] }));
    if (url.pathname === `${prefix}/api/uploads/${CHAT}/list`) return respond(res, 200, JSON.stringify({ files: [], total: 0 }));
    if (url.pathname === `/api/v1/ocu/workspaces/${CHAT}`) return respond(res, 200, JSON.stringify({ cli_badge: null }));
    if (url.pathname === '/favicon.ico') return respond(res, 204, '', 'text/plain');
    if (url.pathname === '/parent') return respond(res, 200, `<!doctype html><html><body>
      <script src="/ocu/static/jszip.min.js"></script><script src="/ocu/static/xlsx.full.min.js"></script>
      <script src="/ocu/static/mammoth.browser.min.js"></script>
      <script>
        window.states=[];
        window.expectedSelections={};
        window.beacons={};
        window.retiredFrames=new Set();
        window.postRetirement=[];
        window.addEventListener('message', e => {
          if(e.data?.type==='sibling-done' || e.data?.type==='opaque-done') {
            window.beacons[e.data.type]={origin:e.origin, source:e.source};
          }
          const current=document.querySelector('#preview')?.contentWindow;
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

async function main() {
  const artifacts = process.env.OCU_PREVIEW_ARTIFACTS || await fs.mkdtemp(path.join(os.tmpdir(), 'ocu-preview-browser-'));
  await fs.mkdir(artifacts, { recursive: true });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await playwright.chromium.launch({ headless: true });
  try {
    const context = await browser.newContext();
    context.setDefaultTimeout(10000);
    await context.addInitScript(() => {
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
      window.setInterval = (callback, delay, ...args) => {
        const timer = start(callback, delay, ...args);
        if (delay === 120000) window.__heartbeatTimers.add(timer);
        return timer;
      };
      window.clearInterval = timer => {
        window.__heartbeatTimers.delete(timer);
        return stop(timer);
      };
    });
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    const consoleErrors = [];
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
            address.pathname.endsWith(`/browser/${CHAT}/json`))) return;
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
          <w:hyperlink w:anchor="section"><w:r><w:t>Go to section</w:t></w:r></w:hyperlink>` : '';
        const blocks = hostile ? `<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Table cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
          <w:p><w:r><w:drawing><wp:inline><wp:extent cx="9525" cy="9525"/><wp:docPr id="1" name="pixel"/>
            <a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
              <pic:pic><pic:nvPicPr><pic:cNvPr id="0" name="pixel.png"/><pic:cNvPicPr/></pic:nvPicPr>
                <pic:blipFill><a:blip r:embed="image"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>
                <pic:spPr><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>
              </pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>` : '';
        zip.file('word/document.xml', `<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="${officeNS}"
          xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
          xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
          xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">
          <w:body><w:p>${hostile ? '<w:bookmarkStart w:id="7" w:name="section"/>' : ''}<w:r><w:rPr><w:b/></w:rPr><w:t>${text}</w:t></w:r>${links}${hostile ? '<w:bookmarkEnd w:id="7"/>' : ''}</w:p>${blocks}</w:body></w:document>`);
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
      row.path.endsWith('foreign.docx') || row.path.endsWith('cross-chat.docx')).length, 0);
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
    // Synthetic converted-markup boundary proof of the real sanitizer seam,
    // not an end-to-end DOCX fixture or a substituted Office renderer.
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
    const secondSlow = new Promise(resolve => { slowRequested = resolve; });
    await page.evaluate(() => window.select('slow', 37));
    await waitState(37, 'loading');
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
    await page.evaluate(() => window.mount('?embed=terminal'));
    await frame().locator('.dash-btn-primary').waitFor();
    assert.equal(await frame().locator('.view-tabs').count(), 0, 'terminal embed mounted nested view tabs');
    assert.equal(requests.filter(row => row.path.startsWith(`/ocu/browser/${CHAT}/`)).length,
      browserBeforeTerminal, 'terminal embed mounted the browser client');
    assert.equal(requests.filter(row => row.path === `/ocu/api/outputs/${CHAT}`).length,
      listingBeforeRuntime, 'terminal embed listed Files');
    assert.equal(requests.filter(row => /\/(restart-container|start-ttyd)$/.test(row.path)).length,
      launchBeforeRuntime, 'runtime embedding implicitly launched a sandbox');
    await page.screenshot({ path: path.join(artifacts, 'embedded-terminal-dashboard.png') });
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
            `${prefix}/terminal/${CHAT}/processes`, `${prefix}/terminal/${CHAT}/start-ttyd`,
            `${prefix}/api/uploads/${CHAT}/list`];
        for (const path of required)
          assert(cycleRequests.some(row => row.path === path && row.header === 'ocu-workspace'),
            `${mode} cycle ${cycle} missed the prefixed workspace request ${path}`);
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
    const standalone = await context.newPage();
    standalone.on('pageerror', error => consoleErrors.push({ text: String(error), url: '' }));
    standalone.on('console', message => {
      if (message.type() === 'error') consoleErrors.push({ text: message.text(), url: message.location().url });
    });
    try {
      await standalone.goto(`${origin}/ocu/preview/${CHAT}`);
      const standaloneUnsafe = standalone.locator('.preview-stage a').filter({ hasText: 'Unsafe link' });
      await standaloneUnsafe.waitFor();
      assert.equal(await standaloneUnsafe.getAttribute('href'), null);
      await standaloneUnsafe.click();
      assert.equal(await standalone.evaluate(() => window.__docxExecuted), undefined);
      await standalone.locator('.preview-stage').getByText('Table cell').waitFor();
      await standalone.screenshot({ path: path.join(artifacts, 'standalone-sanitized-office.png') });
    } finally {
      await standalone.close();
      listMode = 'normal';
    }
    assert.deepEqual(externalRequests, [], 'Office content requested an external resource');
    const abortLogs = consoleErrors.filter(error => error.text.includes('net::ERR_ABORTED'));
    assert(abortLogs.length <= expectedAbortUrls.length, 'unaccounted aborted-resource console errors');
    assert.deepEqual(abortLogs.filter(error => !expectedAbortUrls.includes(error.url)),
      [], 'unexpected aborted-resource URL');
    assert.deepEqual(unexpectedNetworkFailures, [], 'unexpected network failure');
    assert.deepEqual(consoleErrors.filter(error => !error.text.includes('net::ERR_ABORTED')),
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
  for (const response of heldBrowserStatuses.splice(0)) response.destroy();
  for (const response of heldBrowserPages.splice(0)) response.destroy();
  for (const upgrade of heldUpgrades.splice(0)) upgrade.socket.destroy();
  for (const socket of activeSockets) socket.destroy();
  await new Promise(resolve => server.close(resolve));
});
