// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
// Run from repository root: node tests/orchestrator/preview_embedding_browser.cjs
// Requires the declared playwright 1.62.1 package and its Chromium browser.
const assert = require('node:assert/strict');
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
const heldListings = [];
const within = (promise, name) => Promise.race([
  promise,
  new Promise((_, reject) => setTimeout(() => reject(new Error(`${name} timed out`)), 10000).unref()),
]);
let listMode = 'normal';
const bytes = {};
const contentTypes = { '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2' };
const respond = (res, status, body, type = 'application/json') => {
  if (res.destroyed) return;
  res.writeHead(status, { 'Content-Type': type, 'Cache-Control': 'no-store' });
  res.end(body);
};
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  requests.push({ path: url.pathname, cursor: url.searchParams.get('cursor'), header: req.headers['x-requested-with'] });
  try {
    if (url.pathname.startsWith('/ocu/static/')) {
      const name = decodeURIComponent(url.pathname.slice('/ocu/static/'.length));
      const filename = path.resolve(STATIC, name);
      if (!filename.startsWith(STATIC + path.sep)) return respond(res, 404, 'Not found', 'text/plain');
      return respond(res, 200, await fs.readFile(filename), contentTypes[path.extname(filename)] || 'application/octet-stream');
    }
    if (url.pathname === '/ocu/preview/' + CHAT) {
      const origin = `http://${req.headers.host}`;
      const html = `<!DOCTYPE html><html><head><meta charset="utf-8">
        <link rel="stylesheet" href="/ocu/static/preview.css">
        <link rel="stylesheet" href="/ocu/static/github.min.css">
        <link rel="stylesheet" href="/ocu/static/katex/katex.min.css">
        <link rel="stylesheet" href="/ocu/static/xterm.css">
        <script src="/ocu/static/highlight.min.js"></script>
        <script src="/ocu/static/highlightjs-line-numbers.min.js"></script>
        <script src="/ocu/static/marked.min.js"></script>
        <script src="/ocu/static/xterm.min.js"></script>
        <script src="/ocu/static/xterm-addon-fit.min.js"></script>
        <script src="/ocu/static/xterm-addon-web-links.min.js"></script>
        </head><body><div id="app"></div><script>window.__CONFIG__ = {
          apiUrl:'/ocu/api/outputs/${CHAT}', filesBase:'/ocu/files/${CHAT}', chatId:'${CHAT}',
          describeUrl:'/api/v1/ocu/workspaces/${CHAT}' };</script>
          <script type="module" src="/ocu/static/preview.js"></script></body></html>`;
      assert.equal(new URL(origin).hostname, '127.0.0.1');
      return respond(res, 200, html, 'text/html');
    }
    if (url.pathname === '/ocu/api/outputs/' + CHAT) {
      if (listMode === 'failure') return respond(res, 503, '{}');
      if (listMode === 'timeout') { heldListings.push(res); return; }
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
    if (url.pathname.startsWith(`/ocu/files/${CHAT}/`)) {
      if (url.pathname.endsWith('/slow.docx')) { held.push(res); slowRequested(); return; }
      const name = decodeURIComponent(url.pathname.slice(`/ocu/files/${CHAT}/`.length));
      if (!(name in bytes)) return respond(res, 404, 'Not found', 'text/plain');
      return respond(res, 200, bytes[name], 'application/octet-stream');
    }
    if (url.pathname === '/favicon.ico') return respond(res, 204, '', 'text/plain');
    if (url.pathname === '/parent') return respond(res, 200, `<!doctype html><html><body>
      <script src="/ocu/static/jszip.min.js"></script><script src="/ocu/static/xlsx.full.min.js"></script>
      <script>
        window.states=[];
        window.addEventListener('message', e => { if(e.origin===location.origin) window.states.push(e.data); if(e.data?.type==='opaque-done') window.opaqueDone=true; });
        window.select=(file_id,generation,extra={})=>document.querySelector('#preview').contentWindow.postMessage(
          {type:'ocu:preview-select',chat_id:'${CHAT}',file_id,generation,...extra},location.origin);
        window.mount=(query='?embed=files')=>{const frame=document.createElement('iframe');frame.id='preview';
          frame.setAttribute('sandbox','allow-scripts allow-same-origin allow-forms');
          frame.src='/ocu/preview/${CHAT}'+query;document.body.appendChild(frame);};
      </script></body></html>`, 'text/html');
    return respond(res, 404, 'Not found', 'text/plain');
  } catch (error) { respond(res, 500, String(error), 'text/plain'); }
});

async function main() {
  const artifacts = process.env.OCU_PREVIEW_ARTIFACTS || await fs.mkdtemp(path.join(os.tmpdir(), 'ocu-preview-browser-'));
  await fs.mkdir(artifacts, { recursive: true });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await playwright.chromium.launch({ headless: true });
  const page = await browser.newPage();
  page.setDefaultTimeout(10000);
  const consoleErrors = [];
  page.on('pageerror', error => consoleErrors.push(String(error)));
  page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()); });
  const frame = () => page.frameLocator('#preview');
  const waitState = async (generation, state, timeout = 10000) => {
    await page.waitForFunction(({ generation, state }) => window.states.some(row =>
      row.type === 'ocu:preview-state' && row.generation === generation && row.state === state),
    { generation, state }, { timeout });
    const result = (await page.evaluate(g => window.states.filter(row => row.generation === g), generation)).at(-1);
    assert.deepEqual(Object.keys(result).sort(), ['chat_id', 'file_id', 'generation', 'state', 'type']);
    assert.equal(result.chat_id, CHAT);
    return result;
  };
  try {
    await page.goto(origin + '/parent');
    const fixture = await page.evaluate(async () => {
      const zip = new JSZip();
      zip.file('[Content_Types].xml', `<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>`);
      zip.file('_rels/.rels', `<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>`);
      zip.file('word/document.xml', `<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Hello Office</w:t></w:r></w:p></w:body></w:document>`);
      zip.file('word/_rels/document.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>');
      const doc = Array.from(await zip.generateAsync({ type: 'uint8array' }));
      const wb = XLSX.utils.book_new();
      XLSX.utils.book_append_sheet(wb, XLSX.utils.aoa_to_sheet([['Report'], ['Visible cell']]), 'Evidence');
      const sheet = Array.from(new Uint8Array(XLSX.write(wb, { bookType: 'xlsx', type: 'array' })));
      return { doc, sheet };
    });
    bytes['valid.docx'] = Buffer.from(fixture.doc);
    bytes['slow.docx'] = bytes['valid.docx'];
    bytes['valid.xlsx'] = Buffer.from(fixture.sheet);
    bytes['broken.xlsx'] = Buffer.from('not an Office ZIP');
    bytes['broken.pptx'] = Buffer.from('not an Office ZIP');

    await page.evaluate(() => window.mount());
    await page.waitForFunction(() => window.states.some(row => row.type === 'ocu:preview-ready'), null, { timeout: 10000 });
    assert.deepEqual(await page.evaluate(() => window.states[0]), { type: 'ocu:preview-ready', chat_id: CHAT });
    assert.equal(await page.locator('#preview').getAttribute('sandbox'), 'allow-scripts allow-same-origin allow-forms');
    const before = requests.filter(row => row.path.includes('/api/outputs/')).length;
    assert.equal(before, 0, 'embedded page polls before selection');
    const bad = { type:'ocu:preview-select', chat_id:CHAT, file_id:'broken', generation:0 };
    await frame().locator('#app').waitFor();
    await page.evaluate(payload => {
      const iframe = document.createElement('iframe');
      iframe.srcdoc = `<script>parent.frames[0].postMessage(${JSON.stringify(payload)}, location.origin);parent.postMessage({type:'sibling-done'}, location.origin)<\/script>`;
      document.body.appendChild(iframe);
    }, bad);
    await page.evaluate(payload => {
      const iframe = document.createElement('iframe');
      iframe.setAttribute('sandbox', 'allow-scripts');
      iframe.srcdoc = `<script>parent.frames[0].postMessage(${JSON.stringify(payload)}, '*');parent.postMessage({type:'opaque-done'}, '*')<\/script>`;
      document.body.appendChild(iframe);
    }, bad);
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
    await page.waitForFunction(() => window.states.some(row => row.type === 'sibling-done'));
    await page.waitForFunction(() => window.opaqueDone === true);
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
    const secondSlow = new Promise(resolve => { slowRequested = resolve; });
    await page.evaluate(() => window.select('slow', 19));
    await waitState(19, 'loading');
    await within(secondSlow, 'unmounted Office fetch');
    const beforeUnmount = await page.evaluate(() => window.states.length);
    await page.locator('#preview').evaluate(element => element.remove());
    for (const response of held.splice(0)) respond(response, 200, bytes['slow.docx'], 'application/octet-stream');
    await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 0)));
    assert.equal(await page.evaluate(() => window.states.length), beforeUnmount);
    await page.evaluate(() => window.mount('?embed=files&embed=files'));
    await frame().locator('[role="alert"]').getByText('Invalid preview embedding').waitFor();
    assert.equal((await page.evaluate(() => window.states.filter(row => row.type === 'ocu:preview-ready'))).length, 1);
    await page.locator('#preview').evaluate(element => element.remove());
    await page.evaluate(() => window.mount('?embed=browser'));
    await frame().locator('[role="alert"]').getByText('Invalid preview embedding').waitFor();
    assert.equal((await page.evaluate(() => window.states.filter(row => row.type === 'ocu:preview-ready'))).length, 1);
    await page.locator('#preview').evaluate(element => element.remove());
    await page.evaluate(() => window.mount());
    await page.waitForFunction(() => window.states.filter(row => row.type === 'ocu:preview-ready').length === 2);
    assert.equal(requests.filter(row => /\/(browser|terminal)\//.test(row.path)
      || /\/api\/runtime\//.test(row.path) || /\/api\/v1\//.test(row.path)).length, 0);
    assert(requests.filter(row => row.path.includes('/api/outputs/')).every(row => row.header === 'ocu-workspace'));
    assert(requests.filter(row => row.path.endsWith('.docx') || row.path.endsWith('.xlsx') || row.path.endsWith('.pptx'))
      .every(row => row.header === 'ocu-workspace'));
    // Navigation teardown and the timed-out listing intentionally abort pending fetches.
    assert.deepEqual(consoleErrors.filter(error => !error.includes('net::ERR_ABORTED')),
      [], 'unexpected browser console errors');
    console.log(JSON.stringify({ result: 'ok', screenshots: artifacts, listingRequests: requests.filter(r => r.path.includes('/api/outputs/')).length }));
  } finally {
    await browser.close();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  for (const response of held.splice(0)) response.destroy();
  for (const response of heldListings.splice(0)) response.destroy();
  await new Promise(resolve => server.close(resolve));
});
