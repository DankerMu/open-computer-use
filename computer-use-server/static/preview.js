// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
// =============================================================================
// Preview SPA — Preact + HTM
// =============================================================================

import { html, render, useState, useEffect, useRef, useCallback } from './preact-htm.min.js';
import { icon, fileIcon, fileIconLarge } from './icons.js';
import { BrowserViewer } from './browser-viewer.js';
import { t, LANG } from './locale.js';
import {
  ocuFetch,
  moduleAssetUrl,
  terminalWsUrl,
  startWorkspaceHeartbeat,
  loadCliBadge,
  recoverStoppedContainer,
  loadOutputsWindow,
  renderKey,
  pickAutoSelect,
  applyListingSelection,
  formulaHasCachedValue,
  formulaCellDisplay,
  disconnectPreviewObserver,
  attachPreviewObserver,
  workspaceHttpHeaders,
} from './ocu-request.js';

const { apiUrl: API_URL, filesBase: FILES_BASE, chatId: CHAT_ID, describeUrl: DESCRIBE_URL } = window.__CONFIG__;
const embedParams = new URLSearchParams(location.search).getAll('embed');
const EMBED_MODE = embedParams.length === 0 ? 'standalone'
  : embedParams.length === 1 && ['files', 'browser', 'terminal'].includes(embedParams[0])
    && window.parent !== window ? embedParams[0] : 'invalid';
const EMBED_PAGES = 100;
const EMBED_DEADLINE_MS = 10000;

// =============================================================================
// Utilities
// =============================================================================

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function formatSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

const _loadedScripts = new Map();
function loadScript(url) {
  const pending = _loadedScripts.get(url);
  if (pending) return pending;
  const work = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = url;
    s.onload = () => resolve();
    s.onerror = () => {
      _loadedScripts.delete(url);
      reject(new Error(`Failed to load ${url}`));
    };
    document.head.appendChild(s);
  });
  _loadedScripts.set(url, work);
  return work;
}

function fetchOutput(url) {
  return ocuFetch(url, { serverUrl: true, cache: 'no-store' });
}

function officeBanner() {
  return `<div class="office-preview-banner"><strong>${t('content_preview')}</strong> ${t('content_preview_disclaimer')}</div>`;
}
// Office converters produce HTML from document-controlled bytes. Only this
// detached, sanitized fragment may cross into the trusted preview DOM.
export function safeOfficeHtml(markup) {
  const sanitizer = window.__OCU_OFFICE_PURIFY || window.DOMPurify;
  if (!sanitizer?.isSupported) throw new Error('Office sanitizer unavailable');
  const inlineImage = /^data:image\/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/]+={0,2}$/;
  const blockRemoteImage = (node, data) => {
    if (data.attrName === 'src' && node.nodeName.toLowerCase() === 'img'
        && !inlineImage.test(data.attrValue)) data.keepAttr = false;
  };
  sanitizer.addHook('uponSanitizeAttribute', blockRemoteImage);
  let fragment;
  try {
    fragment = sanitizer.sanitize(markup, {
      RETURN_DOM_FRAGMENT: true,
      ALLOWED_TAGS: ['p', 'div', 'span', 'br', 'strong', 'em', 'b', 'i', 'u', 's',
        'sub', 'sup', 'blockquote', 'pre', 'code', 'ul', 'ol', 'li', 'h1', 'h2',
        'h3', 'h4', 'h5', 'h6', 'table', 'thead', 'tbody', 'tfoot', 'tr',
        'th', 'td', 'caption', 'a', 'img', 'hr'],
      ALLOWED_ATTR: ['href', 'src', 'alt', 'title', 'colspan', 'rowspan', 'id', 'scope'],
      FORBID_TAGS: ['script', 'style', 'svg', 'math', 'iframe', 'object', 'embed',
        'form', 'link', 'meta', 'base', 'audio', 'video'],
      FORBID_ATTR: ['style', 'srcset'],
      ALLOW_DATA_ATTR: false,
      ALLOW_ARIA_ATTR: false,
      SANITIZE_NAMED_PROPS: true,
    });
  } finally {
    sanitizer.removeHook('uponSanitizeAttribute', blockRemoteImage);
  }
  for (const image of fragment.querySelectorAll('img')) {
    const src = image.getAttribute('src') || '';
    if (!inlineImage.test(src)) {
      image.remove();
    }
  }
  let ids = null;
  for (const link of fragment.querySelectorAll('a[href]')) {
    const href = link.getAttribute('href');
    if (href.startsWith('#')) {
      try {
        ids ??= new Set(Array.from(fragment.querySelectorAll('[id]'), node => node.id));
        const target = 'user-content-' + decodeURIComponent(href.slice(1));
        if (ids.has(target)) link.setAttribute('href', '#' + encodeURIComponent(target));
        else link.removeAttribute('href');
      } catch {
        link.removeAttribute('href');
      }
      continue;
    }
    let safe = false;
    try {
      const url = new URL(href, location.href);
      safe = /^(https?:)$/.test(url.protocol) && !url.username && !url.password
        && /^(https?:)?\/\//i.test(href);
    } catch {
      safe = false;
    }
    if (!safe) link.removeAttribute('href');
    else {
      link.setAttribute('target', '_blank');
      link.setAttribute('rel', 'noopener noreferrer');
    }
  }
  return fragment;
}

function normalizePath(path) {
  const parts = path.split('/');
  const result = [];
  for (let i = 0; i < parts.length; i++) {
    if (parts[i] === '.' || parts[i] === '') continue;
    if (parts[i] === '..') { result.pop(); }
    else { result.push(parts[i]); }
  }
  return result.join('/');
}

function parseCSV(text, delimiter) {
  const rows = [];
  let row = [];
  let cell = '';
  let inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inQuotes) {
      if (ch === '"') {
        if (i + 1 < text.length && text[i + 1] === '"') { cell += '"'; i++; }
        else { inQuotes = false; }
      } else { cell += ch; }
    } else {
      if (ch === '"') { inQuotes = true; }
      else if (ch === delimiter) { row.push(cell); cell = ''; }
      else if (ch === '\n') {
        row.push(cell); cell = '';
        if (row.length > 0) rows.push(row);
        row = [];
      } else if (ch !== '\r') { cell += ch; }
    }
  }
  if (cell || row.length > 0) { row.push(cell); rows.push(row); }
  return rows;
}

function showToast(text) {
  const msg = document.createElement('div');
  msg.className = 'toast';
  msg.textContent = text;
  document.body.appendChild(msg);
  setTimeout(() => msg.remove(), 1500);
}

function copyText(text) {
  // Try modern clipboard API first, fallback to execCommand for iframe sandbox
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text).then(
      () => true,
      () => copyTextFallback(text)
    );
  }
  return Promise.resolve(copyTextFallback(text));
}

function copyTextFallback(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;left:-9999px;top:-9999px';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); return true; }
  catch { return false; }
  finally { ta.remove(); }
}

// =============================================================================
// Link interception for HTML previews and Markdown
// =============================================================================

function handleLinkClick(href, resolvedUrl, files, selectedFile, onSelectFile) {
  if (!href) return;
  if (href.startsWith('#')) {
    const targetId = decodeURIComponent(href.substring(1));
    let el = document.getElementById(targetId);
    if (!el) { try { el = document.querySelector(href); } catch(e) {} }
    if (el) el.scrollIntoView({ behavior: 'smooth' });
    return;
  }
  if (href.startsWith('http://') || href.startsWith('https://') || href.startsWith('//')) {
    try {
      const linkUrl = new URL(href, window.location.origin);
      if (linkUrl.origin === window.location.origin) {
        window.open(href, '_blank', 'noopener');
      } else {
        _showExternalLinkDialog(href);
      }
    } catch (e) { /* invalid URL, ignore */ }
    return;
  }
  let filePath = null;
  const filesBaseWithSlash = FILES_BASE + '/';
  if (resolvedUrl) {
    try {
      const url = new URL(resolvedUrl, window.location.origin);
      if (url.pathname.startsWith(filesBaseWithSlash)) {
        filePath = decodeURIComponent(url.pathname.substring(filesBaseWithSlash.length));
      }
    } catch(e) {}
  }
  if (!filePath) {
    const currentDir = selectedFile && selectedFile.path.includes('/')
      ? selectedFile.path.substring(0, selectedFile.path.lastIndexOf('/'))
      : '';
    filePath = currentDir ? currentDir + '/' + href : href;
    filePath = normalizePath(filePath);
  }
  if (filePath) {
    filePath = filePath.split('?')[0].split('#')[0];
    let targetFile = files.find(f => f.path === filePath);
    if (!targetFile) {
      const lp = filePath.toLowerCase();
      targetFile = files.find(f => f.path.toLowerCase() === lp);
    }
    if (targetFile) { onSelectFile(targetFile); return; }
  }
  if (resolvedUrl) {
    try {
      const rUrl = new URL(resolvedUrl, window.location.origin);
      if (rUrl.origin !== window.location.origin) {
        _showExternalLinkDialog(resolvedUrl);
        return;
      }
    } catch (e) { /* invalid URL, ignore */ }
    window.open(resolvedUrl, '_blank', 'noopener');
  }
}

function _showExternalLinkDialog(href) {
  const existing = document.getElementById('__ext_link_dialog');
  if (existing) existing.remove();
  const overlay = document.createElement('div');
  overlay.id = '__ext_link_dialog';
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;z-index:99999';
  const box = document.createElement('div');
  box.style.cssText = 'background:var(--bg-primary,#fff);color:var(--text-primary,#000);border-radius:8px;padding:20px;max-width:420px;word-break:break-all;font-family:system-ui,sans-serif';
  box.innerHTML = '<p style="margin:0 0 8px;font-weight:600">Open external link?</p>'
    + '<p style="margin:0 0 16px;font-size:13px;opacity:.8">' + href.replace(/</g, '&lt;') + '</p>';
  const btns = document.createElement('div');
  btns.style.cssText = 'display:flex;gap:8px;justify-content:flex-end';
  const cancel = document.createElement('button');
  cancel.textContent = 'Cancel';
  cancel.style.cssText = 'padding:6px 16px;border:1px solid #ccc;border-radius:4px;background:transparent;cursor:pointer';
  cancel.onclick = () => overlay.remove();
  const open = document.createElement('button');
  open.textContent = 'Open';
  open.style.cssText = 'padding:6px 16px;border:none;border-radius:4px;background:#2563eb;color:#fff;cursor:pointer';
  open.onclick = () => { overlay.remove(); window.open(href, '_blank', 'noopener,noreferrer'); };
  btns.append(cancel, open);
  box.appendChild(btns);
  overlay.appendChild(box);
  overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
  document.body.appendChild(overlay);
}

// =============================================================================
// Preview Renderers (imperative DOM — these render into a container ref)
// =============================================================================

async function renderHtmlPreview(container, file) {
  const sandbox = 'allow-scripts allow-forms';
  try {
    const resp = await fetchOutput(file.url);
    let text = await resp.text();
    const fileDir = file.path.includes('/') ? file.path.substring(0, file.path.lastIndexOf('/')) : '';
    const baseUrl = fileDir ? FILES_BASE + '/' + fileDir + '/' : FILES_BASE + '/';
    const baseTag = `<base href="${baseUrl}">`;
    const linkInterceptScript = '<scr' + 'ipt>'
      + '(function(){'
      + 'document.addEventListener("click",function(e){'
      + 'var a=e.target.closest("a");'
      + 'if(!a)return;'
      + 'var href=a.getAttribute("href");'
      + 'if(!href)return;'
      + 'e.preventDefault();'
      + 'e.stopPropagation();'
      + 'window.parent.postMessage({'
      + 'type:"iframe-link-click",'
      + 'href:href,'
      + 'resolvedUrl:a.href'
      + '},"*");'
      + '},true);'
      + '})();'
      + '</scr' + 'ipt>';
    const injection = baseTag + linkInterceptScript;
    if (text.includes('<head>')) {
      text = text.replace('<head>', '<head>' + injection);
    } else if (text.includes('<html>')) {
      text = text.replace('<html>', '<html><head>' + injection + '</head>');
    } else {
      text = injection + text;
    }
    const iframe = document.createElement('iframe');
    iframe.setAttribute('sandbox', sandbox);
    iframe.srcdoc = text;
    container.replaceChildren(iframe);
  } catch {
    const iframe = document.createElement('iframe');
    iframe.setAttribute('sandbox', sandbox);
    iframe.src = file.url;
    container.replaceChildren(iframe);
  }
}

async function renderPdfPreview(container, file) {
  container.innerHTML = `<div class="pdf-container" id="pdfContainer"><div class="empty-state"><div class="spinner"></div><p class="loading-text">${t('loading_pdf')}</p></div></div>`;
  try {
    await loadScript(moduleAssetUrl('pdf.min.js'));
    const pdfjsLib = window.pdfjsLib;
    if (!pdfjsLib) throw new Error('pdf.js not loaded');
    pdfjsLib.GlobalWorkerOptions.workerSrc = moduleAssetUrl('pdf.worker.min.js');
    const pdf = await pdfjsLib.getDocument({
      url: file.url,
      httpHeaders: workspaceHttpHeaders(),
    }).promise;
    const pdfContainer = container.querySelector('#pdfContainer');
    pdfContainer.innerHTML = '';
    const maxPages = Math.min(pdf.numPages, 30);
    for (let i = 1; i <= maxPages; i++) {
      const page = await pdf.getPage(i);
      const viewport = page.getViewport({ scale: 1.5 });
      const canvas = document.createElement('canvas');
      canvas.width = viewport.width;
      canvas.height = viewport.height;
      await page.render({ canvasContext: canvas.getContext('2d'), viewport }).promise;
      pdfContainer.appendChild(canvas);
    }
    if (pdf.numPages > maxPages) {
      const p = document.createElement('p');
      p.className = 'truncation-notice';
      p.textContent = t('showing_pages', { max: maxPages, total: pdf.numPages });
      pdfContainer.appendChild(p);
    }
  } catch (err) {
    console.error('PDF render error:', err);
    container.innerHTML = `<iframe src="${escapeHtml(file.url)}"></iframe>`;
  }
}

async function renderMarkdownPreview(container, file, files, onSelectFile) {
  try {
    const resp = await fetchOutput(file.url);
    let text = await resp.text();
    if (text.length > 500000) text = text.substring(0, 500000) + '\n\n... (truncated)';
    const renderer = new marked.Renderer();
    const origImage = renderer.image.bind(renderer);
    const origLink = renderer.link.bind(renderer);
    renderer.heading = function(token) {
      let text = token.text;
      let prev;
      do { prev = text; text = text.replace(/<[^>]*>/g, ''); } while (text !== prev);
      const slug = text.toLowerCase()
        .replace(/[^\w\u0400-\u04ff\s-]/g, '')
        .replace(/\s+/g, '-');
      return `<h${token.depth} id="${slug}">${token.text}</h${token.depth}>\n`;
    };
    function resolveUrl(href) {
      if (href && !href.startsWith('http') && !href.startsWith('//') && !href.startsWith('data:') && !href.startsWith('#')) {
        const fileDir = file.path.includes('/') ? file.path.substring(0, file.path.lastIndexOf('/')) : '';
        const base = fileDir ? FILES_BASE + '/' + fileDir : FILES_BASE;
        return base + '/' + href;
      }
      return href;
    }
    renderer.image = function(token) { token.href = resolveUrl(token.href); return origImage(token); };
    renderer.link = function(token) { token.href = resolveUrl(token.href); return origLink(token); };
    const htmlContent = marked.parse(text, { renderer });
    container.innerHTML = `<div class="markdown-body">${htmlContent}</div>`;
    const mdBody = container.querySelector('.markdown-body');

    // Link interception
    mdBody.addEventListener('click', function(e) {
      const a = e.target.closest('a');
      if (!a) return;
      e.preventDefault();
      handleLinkClick(a.getAttribute('href'), a.href, files, file, onSelectFile);
    });

    // Syntax highlighting
    mdBody.querySelectorAll('pre code').forEach(el => {
      if (!el.classList.contains('language-mermaid')) hljs.highlightElement(el);
    });

    // Mermaid
    const mermaidBlocks = mdBody.querySelectorAll('pre code.language-mermaid');
    if (mermaidBlocks.length > 0) {
      try {
        await loadScript(moduleAssetUrl('mermaid.min.js'));
        mermaid.initialize({ startOnLoad: false, theme: window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'default' });
        mermaidBlocks.forEach(codeEl => {
          const pre = codeEl.parentElement;
          const div = document.createElement('div');
          div.className = 'mermaid';
          div.textContent = codeEl.textContent;
          pre.replaceWith(div);
        });
        await mermaid.run({ querySelector: '.mermaid' });
      } catch (e) { console.warn('Mermaid:', e); }
    }

    // KaTeX
    try {
      await loadScript(moduleAssetUrl('katex/katex.min.js'));
      await loadScript(moduleAssetUrl('katex/auto-render.min.js'));
      renderMathInElement(mdBody, {
        delimiters: [
          { left: '$$', right: '$$', display: true },
          { left: '$', right: '$', display: false }
        ],
        throwOnError: false
      });
    } catch (e) { console.warn('KaTeX:', e); }
  } catch (err) {
    console.error('Markdown render error:', err);
    container.innerHTML = `<div class="empty-state"><p>${t('load_fail')}</p></div>`;
  }
}

async function renderCodePreview(container, file) {
  try {
    const resp = await fetchOutput(file.url);
    let text = await resp.text();
    if (text.length > 200000) text = text.substring(0, 200000) + '\n... (truncated)';
    const ext = file.name.split('.').pop() || '';
    container.innerHTML = `<pre><code class="language-${ext}">${escapeHtml(text)}</code></pre>`;
    container.querySelectorAll('pre code').forEach(el => {
      hljs.highlightElement(el);
      if (typeof hljs.lineNumbersBlock === 'function') hljs.lineNumbersBlock(el);
    });
  } catch {
    container.innerHTML = `<div class="empty-state"><p>${t('load_fail')}</p></div>`;
  }
}

async function renderSpreadsheetPreview(container, file) {
  try {
    const resp = await fetchOutput(file.url);
    let text = await resp.text();
    if (text.length > 500000) text = text.substring(0, 500000);
    const ext = file.name.split('.').pop().toLowerCase();
    const delimiter = ext === 'tsv' ? '\t' : ',';
    const rows = parseCSV(text, delimiter);
    let tableHtml = '<table><thead><tr>';
    if (rows.length > 0) {
      rows[0].forEach(cell => { tableHtml += `<th>${escapeHtml(cell)}</th>`; });
      tableHtml += '</tr></thead><tbody>';
      for (let i = 1; i < Math.min(rows.length, 1000); i++) {
        tableHtml += '<tr>';
        rows[i].forEach(cell => { tableHtml += `<td>${escapeHtml(cell)}</td>`; });
        tableHtml += '</tr>';
      }
      tableHtml += '</tbody></table>';
      if (rows.length > 1000) {
        tableHtml += `<p class="truncation-notice">${t('showing_rows', { n: rows.length })}</p>`;
      }
    }
    container.innerHTML = `<div class="data-table-wrap">${tableHtml}</div>`;
  } catch {
    container.innerHTML = `<div class="empty-state"><p>${t('load_fail')}</p></div>`;
  }
}

let officeSanitizerOwned = null;
let sanitizerGate = Promise.resolve();

function withSanitizerGate(work) {
  const next = sanitizerGate.then(work, work);
  sanitizerGate = next.then(() => undefined, () => undefined);
  return next;
}

async function ensureOfficeSanitizer() {
  return withSanitizerGate(async () => {
    if (officeSanitizerOwned?.isSupported) {
      window.__OCU_OFFICE_PURIFY = officeSanitizerOwned;
      return officeSanitizerOwned;
    }
    const previous = window.DOMPurify;
    await loadScript(moduleAssetUrl('purify.min.js'));
    const loaded = window.DOMPurify;
    if (!loaded?.isSupported) throw new Error('Office sanitizer unavailable');
    officeSanitizerOwned = loaded;
    window.__OCU_OFFICE_PURIFY = loaded;
    if (previous && previous !== loaded) window.DOMPurify = previous;
    return loaded;
  });
}

async function renderDocxPreview(container, file, embedded = false) {
  container.innerHTML = officeBanner() + '<div class="markdown-body" id="docxContainer"><div class="spinner" style="margin:20px auto"></div></div>';
  try {
    await loadScript(moduleAssetUrl('mammoth.browser.min.js'));
    await ensureOfficeSanitizer();
    const resp = await fetchOutput(file.url);
    if (embedded && !resp.ok) throw new Error('Office response failed');
    const arrayBuffer = await resp.arrayBuffer();
    const result = await mammoth.convertToHtml({ arrayBuffer }, { includeEmbeddedStyleMap: false });
    const safe = safeOfficeHtml(result.value);
    const body = container.querySelector('#docxContainer');
    if (body) body.replaceChildren(safe);
  } catch (err) {
    if (embedded) {
      container.innerHTML = `<div class="empty-state"><p>${t('load_fail')}</p></div>`;
      return false;
    }
    console.error('DOCX render error:', err);
    renderDownloadFallback(container, file, 'fileText');
  }
  return true;
}

function markUncomputedFormulaCells(root, sheet) {
  root.querySelectorAll('td[id], th[id]').forEach((el) => {
    const id = el.getAttribute('id') || '';
    const addr = id.includes('-') ? id.slice(id.lastIndexOf('-') + 1) : '';
    const cell = sheet[addr];
    if (cell && cell.f != null && cell.f !== '' && !formulaHasCachedValue(cell)) {
      el.classList.add('xlsx-uncomputed');
      el.textContent = formulaCellDisplay(cell);
    }
  });
}

async function renderXlsxPreview(container, file, embedded = false) {
  container.innerHTML = officeBanner() + '<div class="data-table-wrap" id="xlsxContainer"><div class="spinner" style="margin:20px auto"></div></div>';
  try {
    await loadScript(moduleAssetUrl('xlsx.full.min.js'));
    await ensureOfficeSanitizer();
    const resp = await fetchOutput(file.url);
    if (embedded && !resp.ok) throw new Error('Office response failed');
    const arrayBuffer = await resp.arrayBuffer();
    if (embedded) {
      const bytes = new Uint8Array(arrayBuffer);
      const zip = bytes.length >= 4 && bytes[0] === 0x50 && bytes[1] === 0x4b
        && bytes[2] === 0x03 && bytes[3] === 0x04;
      const cfb = bytes.length >= 8
        && [0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1].every((byte, i) => bytes[i] === byte);
      if (zip) {
        await loadScript(moduleAssetUrl('jszip.min.js'));
        const contents = await JSZip.loadAsync(arrayBuffer.slice(0));
        if (!contents.file('xl/workbook.xml')) throw new Error('Invalid Office workbook');
      } else if (!cfb) {
        // SheetJS writes and reads raw BIFF2/3/4 with these versioned BOF
        // records. Validate framing through EOF before its permissive parser.
        const view = new DataView(arrayBuffer);
        const bof = bytes.length >= 8 && view.getUint16(0, true);
        const version = bof === 9 ? 2 : bof === 521 ? 3 : bof === 1033 ? 4 : null;
        const minimum = version === 2 ? 4 : 6;
        if (!version || bytes.length < 4 + minimum
            || view.getUint16(2, true) < minimum
            || 4 + view.getUint16(2, true) > bytes.length
            || !(view.getUint16(4, true) === version || view.getUint16(4, true) === (version << 8)
              || (version === 2 && view.getUint16(4, true) === 7))) {
          throw new Error('Invalid raw BIFF workbook');
        }
        let offset = 0;
        let depth = 0;
        let closed = false;
        while (offset + 4 <= bytes.length) {
          const id = view.getUint16(offset, true);
          const length = view.getUint16(offset + 2, true);
          const end = offset + 4 + length;
          if (end > bytes.length) break;
          if (id === 9 || id === 521 || id === 1033) {
            const nestedVersion = id === 9 ? 2 : id === 521 ? 3 : 4;
            if (length < (nestedVersion === 2 ? 4 : 6)) break;
            const word = view.getUint16(offset + 4, true);
            if (word !== nestedVersion && word !== (nestedVersion << 8)
                && !(nestedVersion === 2 && word === 7)) break;
            depth++;
          } else if (id === 10) {
            if (length !== 0 || --depth < 0) break;
            if (depth === 0 && end === bytes.length) {
              closed = true;
              break;
            }
          } else if (depth === 0) break;
          offset = end;
        }
        if (!closed) throw new Error('Truncated raw BIFF workbook');
      }
    }
    const workbook = XLSX.read(arrayBuffer, { type: 'array', cellFormula: true, cellNF: true, sheetStubs: true, raw: false });
    if (embedded && !workbook.SheetNames.length) throw new Error('Empty Office workbook');
    let html = '';
    if (workbook.SheetNames.length > 1) {
      html += '<div class="sheet-tabs">';
      workbook.SheetNames.forEach((name, i) => {
        html += `<button class="sheet-tab${i === 0 ? ' active' : ''}" data-sheet="${i}">${escapeHtml(name)}</button>`;
      });
      html += '</div>';
    }
    html += '<div id="sheetContent"></div>';
    const xlsxContainer = container.querySelector('#xlsxContainer');
    xlsxContainer.innerHTML = html;
    function renderSheet(index) {
      const sheet = workbook.Sheets[workbook.SheetNames[index]];
      const content = xlsxContainer.querySelector('#sheetContent');
      content.replaceChildren(safeOfficeHtml(XLSX.utils.sheet_to_html(sheet, { editable: false, id: 'xlsx' })));
      markUncomputedFormulaCells(content, sheet);
    }
    renderSheet(0);
    xlsxContainer.querySelectorAll('.sheet-tab').forEach(btn => {
      btn.addEventListener('click', function() {
        xlsxContainer.querySelectorAll('.sheet-tab').forEach(b => b.classList.remove('active'));
        this.classList.add('active');
        renderSheet(parseInt(this.getAttribute('data-sheet')));
      });
    });
  } catch (err) {
    if (embedded) {
      container.innerHTML = `<div class="empty-state"><p>${t('load_fail')}</p></div>`;
      return false;
    }
    console.error('XLSX render error:', err);
    renderDownloadFallback(container, file, 'fileSpreadsheet');
  }
  return true;
}

async function renderPptxPreview(container, file, embedded = false) {
  container.innerHTML = officeBanner() + `<div class="empty-state" id="pptxLoading"><div class="spinner"></div><p class="loading-text">${t('loading')}</p></div><div class="pptx-container" id="pptxContainer" style="display:none"></div>`;
  try {
    await loadScript(moduleAssetUrl('jszip.min.js'));
    await loadScript(moduleAssetUrl('chart.umd.js'));
    await loadScript(moduleAssetUrl('pptxviewjs.min.js'));
    const pptxResp = await fetchOutput(file.url);
    if (embedded && !pptxResp.ok) throw new Error('Office response failed');
    const pptxBuf = await pptxResp.arrayBuffer();
    const pptxContainer = container.querySelector('#pptxContainer');
    let ratio = 9 / 16;
    let deckCx = 0;
    let deckCy = 0;
    try {
      const zip = await JSZip.loadAsync(pptxBuf.slice(0));
      const presXml = await zip.file('ppt/presentation.xml').async('string');
      const tag = (presXml.match(/<p:sldSz\b[^>]*>/) || [])[0] || '';
      deckCx = Number((/cx="(\d+)"/.exec(tag) || [])[1]);
      deckCy = Number((/cy="(\d+)"/.exec(tag) || [])[1]);
      if (deckCx > 0 && deckCy > 0) ratio = deckCy / deckCx;
    } catch (err) {
      if (embedded) throw err;
      console.warn('PPTX slide size:', err);
    }
    const contentWidth = Math.max(container.clientWidth || container.parentElement?.clientWidth || 0, 1);
    const slideWidth = Math.min(contentWidth, 960);
    const slideHeight = Math.max(1, Math.round(slideWidth * ratio));
    pptxContainer.style.setProperty('--pptx-slide-width', slideWidth + 'px');
    pptxContainer.style.setProperty('--pptx-slide-aspect', `${slideWidth} / ${slideHeight}`);
    const applyDisplayedSize = () => {
      const box = Math.max((pptxContainer.clientWidth || contentWidth) - 20, 1);
      const cssWidth = Math.min(box, 960);
      const cssHeight = Math.max(1, Math.round(cssWidth * ratio));
      pptxContainer.querySelectorAll('canvas.pptx-slide').forEach((canvas) => {
        canvas.style.removeProperty('width');
        canvas.style.removeProperty('height');
        canvas.style.width = cssWidth + 'px';
        canvas.style.height = cssHeight + 'px';
        canvas.style.maxWidth = '100%';
        canvas.style.aspectRatio = `${canvas.width} / ${canvas.height}`;
      });
    };
    const viewer = new PptxViewJS.PPTXViewer();
    await viewer.loadFile(pptxBuf);
    const slideCount = viewer.getSlideCount();
    if (embedded && !slideCount) throw new Error('Empty Office presentation');
    for (let i = 0; i < slideCount; i++) {
      const canvas = document.createElement('canvas');
      canvas.width = slideWidth;
      canvas.height = slideHeight;
      canvas.dataset.ratio = String(ratio);
      canvas.dataset.deckCx = String(deckCx);
      canvas.dataset.deckCy = String(deckCy);
      canvas.className = 'pptx-slide';
      pptxContainer.appendChild(canvas);
      viewer.setCanvas(canvas);
      await viewer.goToSlide(i);
      await viewer.render();
      applyDisplayedSize();
    }
    container.querySelector('#pptxLoading')?.remove();
    pptxContainer.style.display = '';
    applyDisplayedSize();
    if (typeof ResizeObserver === 'function' && container.isConnected !== false) {
      const observer = new ResizeObserver(() => applyDisplayedSize());
      observer.observe(pptxContainer);
      if (!attachPreviewObserver(container, observer)) return false;
    }
  } catch (err) {
    if (embedded) {
      container.innerHTML = `<div class="empty-state"><p>${t('load_fail')}</p></div>`;
      return false;
    }
    console.error('PPTX render error:', err);
    renderDownloadFallback(container, file, 'filePresentation', t('pptx_fail'));
  }
  return true;
}


function configureDrawioBases() {
  const base = moduleAssetUrl('drawio/');
  const root = base.replace(/\/$/, '');
  window.DRAWIO_BASE_URL = root;
  window.DRAWIO_SERVER_URL = base;
  window.DRAWIO_LIGHTBOX_URL = root;
  window.DRAWIO_VIEWER_URL = moduleAssetUrl('drawio/js/viewer-static.min.js');
  window.EXPORT_URL = '';
  window.PROXY_URL = '';
  window.SAVE_URL = '';
  window.OPEN_FORM = '';
  window.VSS_CONVERT_URL = '';
  window.REALTIME_URL = '';
  window.NOTIFICATIONS_URL = '';
  window.STYLE_PATH = root + '/styles';
  window.CSS_PATH = root + '/styles';
  window.SHAPES_PATH = root + '/shapes';
  window.STENCIL_PATH = root + '/stencils';
  window.IMAGE_PATH = root + '/images';
  window.GRAPH_IMAGE_PATH = root + '/img';
  window.DRAW_MATH_URL = root + '/math4/es5';
  window.mxBasePath = root + '/mxgraph';
  window.mxImageBasePath = root + '/mxgraph/images';
  window.mxLoadStylesheets = false;
}

function parseDrawioDocument(xml) {
  const doc = new DOMParser().parseFromString(xml, 'text/xml');
  if (doc.querySelector('parsererror')) throw new Error('Invalid Draw.io document');
  const root = doc.documentElement;
  if (!root || (root.nodeName !== 'mxfile' && root.nodeName !== 'mxGraphModel')) {
    throw new Error('Invalid Draw.io document');
  }
  return root;
}

function mxgraphShapeFromStyle(style) {
  if (typeof style !== 'string') return null;
  const match = /(?:^|;)shape=(mxgraph\.[^;]+)/i.exec(style);
  return match ? match[1].trim().toLowerCase() : null;
}

function collectMxgraphShapes(root, names) {
  if (!root || root.nodeType !== 1) return;
  const name = mxgraphShapeFromStyle(root.getAttribute('style'));
  if (name) names.add(name);
  for (const child of root.children) collectMxgraphShapes(child, names);
}

function collectViewerMxgraphShapes(viewer, names) {
  try {
    const cells = viewer?.graph?.model?.cells;
    if (!cells) return;
    for (const cell of Object.values(cells)) {
      const name = mxgraphShapeFromStyle(cell?.style);
      if (name) names.add(name);
    }
  } catch {
  }
}

function installStencilLoadTracker() {
  const utils = window.mxUtils;
  const failures = [];
  if (!utils) return { failures, restore() {} };
  const previousLoad = utils.load;
  const previousGet = utils.get;
  const note = (url, status) => {
    if (!String(url || '').includes('/stencils/')) return;
    if (!(status >= 200 && status < 300)) failures.push(String(url));
  };
  if (typeof previousLoad === 'function') {
    utils.load = function(url) {
      const result = previousLoad.apply(this, arguments);
      try { note(url, result && result.getStatus()); } catch {
        note(url, 0);
      }
      return result;
    };
  }
  if (typeof previousGet === 'function') {
    utils.get = function(url, onload, onerror) {
      return previousGet.call(this, url, function(request) {
        try { note(url, request && request.getStatus()); } catch {
          note(url, 0);
        }
        if (onload) onload(request);
      }, function(request) {
        note(url, 0);
        if (onerror) onerror(request);
      });
    };
  }
  return {
    failures,
    restore() {
      utils.load = previousLoad;
      utils.get = previousGet;
      const registry = window.mxStencilRegistry;
      if (!registry) return;
      for (const url of failures) {
        try {
          if (registry.packages) delete registry.packages[url];
          if (registry.filesLoaded) delete registry.filesLoaded[url];
          const libraries = registry.libraries || {};
          for (const [name, members] of Object.entries(libraries)) {
            if (!Array.isArray(members)) continue;
            if (members.some((member) => String(member) === url || String(member).includes(url) ||
                url.endsWith(String(member)) || url.includes(String(member)))) {
              if (registry.packages) delete registry.packages[name];
            }
          }
        } catch {
        }
      }
    }
  };
}

function assertRequiredStencilsLoaded(names, failures) {
  if (failures.length) throw new Error('Draw.io stencil fetch failed');
  const registry = window.mxStencilRegistry && window.mxStencilRegistry.stencils;
  for (const name of names) {
    if (!registry || !Object.prototype.hasOwnProperty.call(registry, name) || !registry[name]) {
      throw new Error('Draw.io required stencil unavailable');
    }
  }
}

function waitForDrawioRender(viewer, host) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (error) => {
      if (settled) return;
      if (error) {
        settled = true;
        clearTimeout(timer);
        reject(error);
        return;
      }
      if (!host.querySelector('svg')) return;
      settled = true;
      clearTimeout(timer);
      resolve();
    };
    const timer = setTimeout(() => {
      if (host.querySelector('svg')) finish();
      else finish(new Error('Draw.io render timed out'));
    }, 10000);
    if (typeof viewer.addListener !== 'function') {
      finish(new Error('Draw.io viewer API unavailable'));
      return;
    }
    viewer.addListener('render', () => finish());
    finish();
  });
}

async function loadDrawioViewer() {
  return withSanitizerGate(async () => {
    const office = officeSanitizerOwned || window.__OCU_OFFICE_PURIFY;
    const previous = window.DOMPurify;
    let replaced = false;
    if (office && window.DOMPurify === office) {
      delete window.DOMPurify;
      replaced = true;
    }
    try {
      await loadScript(moduleAssetUrl('drawio/js/viewer-static.min.js'));
      if (typeof GraphViewer !== 'function') throw new Error('Draw.io viewer unavailable');
      if (office && window.DOMPurify === office) {
        throw new Error('Draw.io viewer reused the Office sanitizer');
      }
      window.__OCU_OFFICE_PURIFY = office || window.__OCU_OFFICE_PURIFY;
      return GraphViewer;
    } finally {
      if (replaced && office && !window.DOMPurify) window.DOMPurify = previous;
      if (office) window.__OCU_OFFICE_PURIFY = office;
    }
  });
}

async function renderDrawioPreview(container, file) {
  container.innerHTML = `<div class="empty-state"><div class="spinner"></div><p class="loading-text">${t('loading')}</p></div>`;
  const tracker = { restore() {} };
  try {
    configureDrawioBases();
    const drawioResp = await fetchOutput(file.url);
    if (!drawioResp.ok) throw new Error('Draw.io document fetch failed');
    const drawioXml = await drawioResp.text();
    const xmlRoot = parseDrawioDocument(drawioXml);
    const required = new Set();
    collectMxgraphShapes(xmlRoot, required);
    const Viewer = await loadDrawioViewer();
    const host = document.createElement('div');
    host.className = 'drawio-host';
    host.style.cssText = 'max-width:100%;margin:0 auto;background:#fff;padding:20px;border-radius:8px;min-height:120px;';
    container.innerHTML = '';
    container.style.display = 'flex';
    container.style.justifyContent = 'center';
    container.style.alignItems = 'flex-start';
    container.appendChild(host);
    Object.assign(tracker, installStencilLoadTracker());
    const viewer = new Viewer(host, xmlRoot, { 'auto-fit': true, lightbox: false, nav: true });
    await waitForDrawioRender(viewer, host);
    if (!host.isConnected) return;
    collectViewerMxgraphShapes(viewer, required);
    assertRequiredStencilsLoaded(required, tracker.failures);
    const svg = host.querySelector('svg');
    if (!svg) throw new Error('Draw.io render produced no diagram');
  } catch (err) {
    if (!container.isConnected) return;
    console.error('Draw.io render error:', err);
    renderDownloadFallback(container, file, 'diagram', t('drawio_fail'));
  } finally {
    tracker.restore();
  }
}

function renderDownloadFallback(container, file, iconType, errorMsg) {
  container.innerHTML = `<div class="download-prompt">
    <div class="dl-icon">${fileIconLarge(file.type)}</div>
    <div class="dl-name">${escapeHtml(file.name)}</div>
    <div class="dl-size">${formatSize(file.size)}</div>
    ${errorMsg ? `<div class="dl-error">${escapeHtml(errorMsg)}</div>` : ''}
    <a class="btn" href="${escapeHtml(file.url)}" download>${icon('download')} ${t('download')}</a>
  </div>`;
}

function renderPreviewContent(container, file, files, onSelectFile, embedded = false) {
  switch (file.type) {
    case 'html': return renderHtmlPreview(container, file);
    case 'image':
      container.innerHTML = `<img src="${escapeHtml(file.url)}" alt="${escapeHtml(file.name)}">`;
      return;
    case 'pdf': return renderPdfPreview(container, file);
    case 'markdown': return renderMarkdownPreview(container, file, files, onSelectFile);
    case 'code':
    case 'text': return renderCodePreview(container, file);
    case 'spreadsheet': return renderSpreadsheetPreview(container, file);
    case 'docx': return renderDocxPreview(container, file, embedded);
    case 'xlsx': return renderXlsxPreview(container, file, embedded);
    case 'pptx': return renderPptxPreview(container, file, embedded);
    case 'drawio': return renderDrawioPreview(container, file);
    case 'audio':
      container.innerHTML = `<div class="media-container">
        <div class="media-icon">${icon('music', 48)}</div>
        <div class="media-name">${escapeHtml(file.name)}</div>
        <div class="media-size">${formatSize(file.size)}</div>
        <audio controls preload="metadata" src="${escapeHtml(file.url)}">${t('audio_unsupported')}</audio>
        <a class="btn" href="${escapeHtml(file.url)}" download>${icon('download')} ${t('download')}</a>
      </div>`;
      return;
    case 'video':
      container.innerHTML = `<div class="media-container">
        <video controls preload="metadata" src="${escapeHtml(file.url)}">${t('video_unsupported')}</video>
        <a class="btn" href="${escapeHtml(file.url)}" download>${icon('download')} ${t('download')}</a>
      </div>`;
      return;
    default:
      renderDownloadFallback(container, file);
  }
}

// =============================================================================
// Components
// =============================================================================

function ViewTabs({ currentView, onSwitch, browserActive, terminalActive }) {
  return html`
    <div class="view-tabs">
      <button class="view-tab ${currentView === 'files' ? 'active' : ''}"
              onClick=${() => onSwitch('files')}>
        <span class="icon-inline" dangerouslySetInnerHTML=${{ __html: icon('folder') }}></span> ${t('files')}
      </button>
      <button class="view-tab ${currentView === 'browser' ? 'active' : ''}"
              onClick=${() => onSwitch('browser')}>
        <span class="icon-inline" dangerouslySetInnerHTML=${{ __html: icon('globe') }}></span> ${t('browser')}
        ${browserActive && html`<span class="tab-dot"></span>`}
      </button>
      <button class="view-tab ${currentView === 'terminal' ? 'active' : ''}"
              onClick=${() => onSwitch('terminal')}>
        <span class="icon-inline" dangerouslySetInnerHTML=${{ __html: icon('terminal') }}></span> ${t('subagent')}
        <span class="beta-badge">${t('beta')}</span>
        ${terminalActive && html`<span class="tab-dot"></span>`}
      </button>
    </div>
  `;
}

function FileSelector({ files, selectedFile, seenFiles, onSelect }) {
  const [open, setOpen] = useState(false);
  const [dropdownPath, setDropdownPath] = useState('');
  const ref = useRef(null);

  // Close on click outside
  useEffect(() => {
    const handler = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener('click', handler);
    return () => document.removeEventListener('click', handler);
  }, []);

  const prefix = dropdownPath ? dropdownPath + '/' : '';
  const folders = new Set();
  const currentFiles = [];
  for (const f of files) {
    if (prefix && !f.path.startsWith(prefix)) continue;
    const rest = prefix ? f.path.substring(prefix.length) : f.path;
    if (rest.includes('/')) { folders.add(rest.substring(0, rest.indexOf('/'))); }
    else { currentFiles.push(f); }
  }

  const selectedIcon = selectedFile ? fileIcon(selectedFile.type) : icon('folderOpen');
  const hasNew = files.some(f => !seenFiles.has(f.path));

  return html`
    <div class="file-selector" ref=${ref}>
      <button class="file-selector-btn ${open ? 'open' : ''}" type="button"
              onClick=${(e) => { e.stopPropagation(); setOpen(!open); setDropdownPath(''); }}>
        <span class="icon-inline" dangerouslySetInnerHTML=${{ __html: selectedIcon }}></span>
        <span class="selector-name">${selectedFile ? selectedFile.name : t('no_file_selected')}</span>
        <span class="selector-count">${files.length}</span>
        ${hasNew && html`<span class="selector-new-dot"></span>`}
        <span class="selector-arrow icon-inline" dangerouslySetInnerHTML=${{ __html: icon('chevronDown') }}></span>
      </button>
      <ul class="dropdown-menu ${open ? 'open' : ''}">
        ${dropdownPath && html`
          <li class="dropdown-item back" onClick=${(e) => {
            e.stopPropagation();
            setDropdownPath(dropdownPath.includes('/') ? dropdownPath.substring(0, dropdownPath.lastIndexOf('/')) : '');
          }}>
            <span class="item-icon icon-inline" dangerouslySetInnerHTML=${{ __html: icon('arrowLeft') }}></span>
            <span class="item-name">..</span>
          </li>
        `}
        ${[...folders].sort().map(folder => html`
          <li class="dropdown-item folder" onClick=${(e) => { e.stopPropagation(); setDropdownPath(prefix + folder); }}>
            <span class="item-icon icon-inline" dangerouslySetInnerHTML=${{ __html: icon('folder') }}></span>
            <span class="item-name">${folder}</span>
            <span class="item-chevron icon-inline" dangerouslySetInnerHTML=${{ __html: icon('chevronRight') }}></span>
          </li>
        `)}
        ${currentFiles.map(f => html`
          <li class="dropdown-item ${selectedFile && f.path === selectedFile.path ? 'active' : ''}"
              onClick=${() => { onSelect(f); setOpen(false); }}>
            <span class="item-icon icon-inline" dangerouslySetInnerHTML=${{ __html: fileIcon(f.type) }}></span>
            <span class="item-name">${f.name}</span>
            <span class="item-size">${formatSize(f.size)}</span>
            ${!seenFiles.has(f.path) && html`<span class="badge-new"></span>`}
          </li>
        `)}
      </ul>
    </div>
  `;
}

function FilesView({ files, selectedFile, onSelectFile, selectionKey, onRenderState, embedded = false }) {
  const containerRef = useRef(null);
  const prevKeyRef = useRef(null);
  const generationRef = useRef(0);
  const ownedStageRef = useRef(null);
  const mountedRef = useRef(true);

  const dropOwnedStage = (stage) => {
    disconnectPreviewObserver(stage);
    if (stage && stage !== containerRef.current) stage.remove();
    if (ownedStageRef.current === stage) ownedStageRef.current = null;
  };

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      generationRef.current += 1;
      dropOwnedStage(ownedStageRef.current);
      disconnectPreviewObserver(containerRef.current);
    };
  }, []);

  useEffect(() => {
    if (!selectedFile) {
      generationRef.current += 1;
      prevKeyRef.current = null;
      dropOwnedStage(ownedStageRef.current);
      disconnectPreviewObserver(containerRef.current);
      const host = containerRef.current;
      if (host) host.querySelectorAll('.preview-stage').forEach((node) => disconnectPreviewObserver(node));
      return;
    }
    if (!containerRef.current) return;
    const key = embedded ? renderKey(selectedFile) + '\0' + selectionKey : renderKey(selectedFile);
    if (key === prevKeyRef.current) return;
    prevKeyRef.current = key;
    const generation = ++generationRef.current;
    const host = containerRef.current;
    host.querySelectorAll('.preview-stage').forEach((node) => {
      disconnectPreviewObserver(node);
      if (node !== host) node.remove();
    });
    disconnectPreviewObserver(host);
    const stage = document.createElement('div');
    stage.className = 'preview-stage';
    stage.dataset.renderGeneration = String(generation);
    stage.style.cssText = 'display:flex;flex:1;flex-direction:column;min-height:0;width:100%;height:100%;overflow:auto';
    ownedStageRef.current = stage;
    host.replaceChildren(stage);
    Promise.resolve(renderPreviewContent(stage, selectedFile, files, onSelectFile, embedded)).then((success) => {
      if (!mountedRef.current || generation !== generationRef.current) {
        dropOwnedStage(stage);
        return;
      }
      if (stage.parentNode !== host) host.replaceChildren(stage);
      if (onRenderState) onRenderState(success === false ? 'error' : 'ready');
    }).catch(() => {
      if (!mountedRef.current || generation !== generationRef.current) {
        dropOwnedStage(stage);
        return;
      }
      if (embedded) stage.innerHTML = `<div class="empty-state"><p>${t('load_fail')}</p></div>`;
      if (onRenderState) onRenderState('error');
    });
  }, [selectedFile, files, onSelectFile, selectionKey, onRenderState, embedded]);

  if (!selectedFile) {
    return html`
      <div class="preview" ref=${containerRef}>
        <div class="empty-state">
          <div class="empty-icon" dangerouslySetInnerHTML=${{ __html: icon('folder', 48) }}></div>
          <div class="empty-title">${t('no_files_yet')}</div>
          <div class="empty-desc" dangerouslySetInnerHTML=${{ __html: t('no_files_desc') }}></div>
          <div class="empty-example">${t('no_files_example')}</div>
        </div>
      </div>
    `;
  }

  return html`<div class="preview" ref=${containerRef}></div>`;
}

function BrowserView({ chatId, browserActive, onBrowserViewerRef }) {
  const canvasRef = useRef(null);
  const urlBarRef = useRef(null);
  const viewerRef = useRef(null);
  const [connecting, setConnecting] = useState(false);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState(false);

  useEffect(() => {
    if (!browserActive) {
      if (viewerRef.current) { viewerRef.current.disconnect(); viewerRef.current = null; }
      setConnected(false);
      setConnecting(false);
      setError(false);
      return;
    }
    if (viewerRef.current) return;

    setConnecting(true);
    setError(false);
    const canvas = canvasRef.current;
    if (!canvas) return;
    const viewer = new BrowserViewer(canvas, chatId);
    viewerRef.current = viewer;
    if (onBrowserViewerRef) onBrowserViewerRef(viewer);
    let retired = false;
    viewer.connect().then(ok => {
      if (retired) return;
      setConnecting(false);
      if (ok) {
        setConnected(true);
        canvas.focus();
      } else {
        setError(true);
        viewerRef.current = null;
      }
    }).catch(() => {
      if (!retired) { setConnecting(false); setError(true); viewerRef.current = null; }
    });

    return () => {
      retired = true;
      viewer.disconnect();
      if (viewerRef.current === viewer) viewerRef.current = null;
    };
  }, [browserActive]);

  if (!browserActive) {
    return html`
      <div class="browser-panel">
        <div class="empty-state">
          <div class="empty-icon" dangerouslySetInnerHTML=${{ __html: icon('globe', 48) }}></div>
          <div class="empty-title">${t('browser_title')}</div>
          <div class="empty-desc" dangerouslySetInnerHTML=${{ __html: t('browser_desc') }}></div>
          <div class="empty-example" dangerouslySetInnerHTML=${{ __html: t('browser_example') }}></div>
        </div>
      </div>
    `;
  }

  return html`
    <div class="browser-panel">
      ${connecting && html`<div class="browser-connecting"><div class="spinner"></div> ${t('loading_browser')}</div>`}
      ${error && html`<div class="browser-connecting">${t('browser_connect_fail')}</div>`}
      <canvas ref=${canvasRef} tabindex="0" style="display:${connected ? '' : 'none'};flex:1;width:100%;object-fit:contain;cursor:pointer;background:var(--bg-primary)"></canvas>
      <div ref=${urlBarRef} class="browser-url-bar" id="browserUrlBar" style="display:${connected ? '' : 'none'}"></div>
    </div>
  `;
}

function TerminalDashboard({ chatId, dangerousMode, onToggleDangerous, onStartSession, onResumeSession }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const mountedRef = useRef(false);
  const requestRef = useRef(null);

  const fetchData = useCallback(async () => {
    if (!mountedRef.current) return;
    requestRef.current?.abort();
    const controller = new AbortController();
    requestRef.current = controller;
    setLoading(true);
    try {
      const [sResp, sessResp, procResp, uplResp] = await Promise.all([
        ocuFetch(`/terminal/${chatId}/status?_t=${Date.now()}`, { signal: controller.signal }),
        ocuFetch(`/terminal/${chatId}/sessions?_t=${Date.now()}`, { signal: controller.signal }),
        ocuFetch(`/terminal/${chatId}/processes?_t=${Date.now()}`, { signal: controller.signal }),
        ocuFetch(`/api/uploads/${chatId}/list?_t=${Date.now()}`, { signal: controller.signal }),
      ]);
      const [status, sessions, processes, uploads] = await Promise.all([
        sResp.json(), sessResp.json(), procResp.json(), uplResp.json()
      ]);
      if (mountedRef.current && !controller.signal.aborted)
        setData({ status, sessions, processes, uploads });
    } catch(e) {
      if (mountedRef.current && !controller.signal.aborted)
        setData({ status: { active: false }, sessions: { sessions: [] },
          processes: { processes: [] }, uploads: { files: [], total: 0 } });
    } finally {
      if (requestRef.current === controller) {
        requestRef.current = null;
        if (mountedRef.current) setLoading(false);
      }
    }
  }, [chatId]);

  useEffect(() => {
    mountedRef.current = true;
    fetchData();
    return () => { mountedRef.current = false; requestRef.current?.abort(); };
  }, [fetchData]);

  const uploadFile = useCallback((evt) => {
    if (evt) evt.stopPropagation();
    const input = document.createElement('input');
    input.type = 'file';
    input.multiple = true;
    input.style.display = 'none';
    document.body.appendChild(input);
    input.addEventListener('change', async () => {
      for (const file of input.files) {
        const formData = new FormData();
        formData.append('file', file);
        await ocuFetch(`/api/uploads/${chatId}/${encodeURIComponent(file.name)}`, { method: 'POST', body: formData });
      }
      input.remove();
      fetchData();
    });
    input.click();
  }, [chatId]);

  const killProcess = useCallback(async (pid) => {
    try { await ocuFetch(`/terminal/${chatId}/processes/${pid}/kill`, { method: 'POST' }); } catch(e) {}
    fetchData();
  }, [chatId]);

  const copyPath = useCallback((path) => {
    copyText(path).then(() => showToast(t('copied')));
  }, []);

  if (loading || !data) {
    return html`<div class="dash-scroll" style="display:flex;align-items:center;justify-content:center;height:100%"><div class="spinner"></div></div>`;
  }

  const { status, sessions, processes, uploads } = data;
  const hasProcesses = Boolean(processes && processes.processes && processes.processes.length);
  const hasSessions = Boolean(sessions && sessions.sessions && sessions.sessions.length);
  const hasUploads = Boolean(uploads && uploads.files && uploads.files.length);

  return html`
    <div class="dash-scroll">
      <div class="dash-hero">
        <h3>Claude Code <span class="badge">${t('beta')}</span></h3>
        <p>${t('hero_desc')}</p>
        <p class="warn">${t('pd_warning')}</p>
        <div class="dangerous-toggle-row ${dangerousMode ? 'active' : ''}" onClick=${() => onToggleDangerous(!dangerousMode)}>
          <div class="dangerous-toggle-track ${dangerousMode ? 'on' : ''}">
            <div class="dangerous-toggle-knob"></div>
          </div>
          <div class="dangerous-toggle-label">
            <span class="dangerous-toggle-title">${t('skip_perms')}</span>
            ${dangerousMode && html`<span class="dangerous-toggle-warn">${t('dangerous_warn')}</span>`}
          </div>
        </div>
        <div class="dash-actions">
          <button class="dash-btn-primary" onClick=${() => onStartSession()}>
            <span class="icon-inline" dangerouslySetInnerHTML=${{ __html: icon('play') }}></span> ${t('open_terminal')}
          </button>
          <button class="dash-btn-secondary" onClick=${uploadFile}>
            <span class="icon-inline" dangerouslySetInnerHTML=${{ __html: icon('upload') }}></span> ${t('upload_file')}
          </button>
          <a class="dash-link" href="https://github.com/Wide-Moat/open-computer-use/blob/main/docs/TERMINAL-TAB.md" target="_blank">
            <span class="icon-inline" dangerouslySetInnerHTML=${{ __html: icon('book') }}></span> ${t('how_to_use')}
          </a>
        </div>
      </div>

      ${hasProcesses && html`
        <div class="dash-card">
          <h4><span class="icon-inline" dangerouslySetInnerHTML=${{ __html: icon('settings') }}></span> ${t('running_now')}</h4>
          ${processes.processes.map(p => {
            const mins = p.elapsed_minutes || 0;
            const timeStr = mins < 1 ? t('less_than_min') : mins + t('min_suffix');
            return html`
              <div class="dash-process">
                <span class="dot"></span>
                <span>${t('claude_running')} · ${timeStr}</span>
                <button class="action-btn kill-btn" onClick=${() => killProcess(p.pid)}>${t('stop')}</button>
              </div>
            `;
          })}
        </div>
      `}

      ${hasUploads && html`
        <div class="dash-card">
          <h4><span class="icon-inline" dangerouslySetInnerHTML=${{ __html: icon('paperclip') }}></span> ${t('uploaded_files')}</h4>
          <table class="dash-table">
            <thead><tr><th>${t('th_file')}</th><th>${t('th_size')}</th><th></th></tr></thead>
            <tbody>
              ${uploads.files.map(f => html`
                  <tr>
                    <td>${f.name}</td>
                    <td style="white-space:nowrap">${formatSize(f.size)}</td>
                    <td><button class="action-btn" onClick=${() => copyPath(f.container_path)} title=${f.container_path}>
                      <span class="icon-inline" dangerouslySetInnerHTML=${{ __html: icon('copy') }}></span> ${t('copy_path')}
                    </button></td>
                  </tr>
              `)}
            </tbody>
          </table>
        </div>
      `}

      ${hasSessions && html`
        <div class="dash-card">
          <h4><span class="icon-inline" dangerouslySetInnerHTML=${{ __html: icon('fileText') }}></span> ${t('prev_sessions')}</h4>
          <table class="dash-table">
            <thead><tr><th>${t('th_task')}</th><th>${t('th_date')}</th><th></th></tr></thead>
            <tbody>
              ${sessions.sessions.map(s => {
                const dt = s.timestamp ? new Date(s.timestamp * 1000).toLocaleDateString(LANG === 'ru' ? 'ru-RU' : 'en-US', { day: 'numeric', month: 'short' }) : '';
                const name = (s.label || s.session_id.substring(0, 16) + '...').substring(0, 50);
                return html`
                  <tr>
                    <td title=${s.session_id}>${name}</td>
                    <td>${dt}</td>
                    <td><button class="action-btn" onClick=${() => onResumeSession(s.session_id)}>${t('resume')}</button></td>
                  </tr>
                `;
              })}
            </tbody>
          </table>
        </div>
      `}

      ${!status.active && !hasSessions && !hasProcesses && html`
        <div class="dash-hint">${t('dash_hint')}</div>
      `}
    </div>
  `;
}

function TerminalSession({ chatId, resumeId, dangerousMode, onBack }) {
  const containerRef = useRef(null);
  const xtermRef = useRef(null);
  const fitAddonRef = useRef(null);
  const wsRef = useRef(null);
  const reconnectAttemptsRef = useRef(0);
  const dangerousModeRef = useRef(dangerousMode);
  useEffect(() => { dangerousModeRef.current = dangerousMode; }, [dangerousMode]);
  const [startError, setStartError] = useState(null);
  const [selectMode, setSelectMode] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const mountedRef = useRef(true);
  const timersRef = useRef(new Map());
  const reconnectTimerRef = useRef(null);
  const backTimerRef = useRef(null);
  const schedule = useCallback((callback, delay) => {
    if (!mountedRef.current) return null;
    const timer = setTimeout(() => {
      timersRef.current.delete(timer);
      if (mountedRef.current) callback();
    }, delay);
    timersRef.current.set(timer, null);
    return timer;
  }, []);
  const pause = useCallback(delay => new Promise(resolve => {
    if (!mountedRef.current) { resolve(false); return; }
    const timer = schedule(() => resolve(true), delay);
    timersRef.current.set(timer, () => resolve(false));
  }), [schedule]);
  const cancelTimer = useCallback(timer => {
    if (timer === null) return;
    const resolve = timersRef.current.get(timer);
    clearTimeout(timer);
    timersRef.current.delete(timer);
    if (resolve) resolve();
  }, []);
  useEffect(() => () => {
    mountedRef.current = false;
    for (const timer of timersRef.current.keys()) cancelTimer(timer);
  }, [cancelTimer]);

  const connectWs = useCallback((rId) => {
    if (!mountedRef.current || !xtermRef.current) return;
    const ws = new WebSocket(terminalWsUrl(chatId), ['tty']);
    ws.binaryType = 'arraybuffer';
    wsRef.current = ws;
    const current = () => mountedRef.current && wsRef.current === ws && ws.readyState === WebSocket.OPEN;

    let receivedData = false;
    ws.onopen = () => {
      if (!current()) { ws.close(); return; }
      const dims = fitAddonRef.current ? fitAddonRef.current.proposeDimensions() : { cols: 80, rows: 24 };
      ws.send(JSON.stringify({ authToken: '', columns: dims.cols || 80, rows: dims.rows || 24 }));
      if (rId) {
        (async () => {
          const flagSuffix = dangerousModeRef.current ? ' --dangerously-skip-permissions' : '';
          try {
            const resp = await ocuFetch(`/terminal/${chatId}/processes?_t=${Date.now()}`);
            const data = await resp.json();
            if (!current()) return;
            if (data.processes && data.processes.length > 0) {
              ws.send(new TextEncoder().encode('\x30\x03'));
              if (!(await pause(500)) || !current()) return;
              ws.send(new TextEncoder().encode('\x30\x03'));
              if (!(await pause(1500)) || !current()) return;
            } else if (!(await pause(1000)) || !current()) return;
            ws.send(new TextEncoder().encode('\x30claude --resume ' + rId + flagSuffix + '\r'));
          } catch {
            if (!(await pause(2000)) || !current()) return;
            ws.send(new TextEncoder().encode('\x30claude --resume ' + rId +
              (dangerousModeRef.current ? ' --dangerously-skip-permissions' : '') + '\r'));
          }
        })();
      } else if (dangerousModeRef.current) {
        (async () => {
          if (!(await pause(1500)) || !current()) return;
          try {
            const resp = await ocuFetch(`/terminal/${chatId}/processes?_t=${Date.now()}`);
            const data = await resp.json();
            if (current() && !(data.processes && data.processes.length > 0))
              ws.send(new TextEncoder().encode('\x30claude --dangerously-skip-permissions\r'));
          } catch {
            if (current()) ws.send(new TextEncoder().encode('\x30claude --dangerously-skip-permissions\r'));
          }
        })();
      }
    };

    ws.onmessage = (ev) => {
      if (!current()) return;
      if (!receivedData) { receivedData = true; reconnectAttemptsRef.current = 0; }
      if (ev.data instanceof ArrayBuffer && xtermRef.current) {
        const data = new Uint8Array(ev.data);
        if (data.length > 1) {
          const type = data[0];
          if (type === 0x30) { xtermRef.current.write(data.slice(1)); }
          else if (type === 0x31 || type === 0x32) { /* ignore title/prefs */ }
          else { xtermRef.current.write(data); }
        }
      }
    };

    ws.onclose = () => {
      if (wsRef.current !== ws || !mountedRef.current || !xtermRef.current) return;
      wsRef.current = null;
      if (reconnectAttemptsRef.current < 3) {
        const delay = 1000 * Math.pow(2, reconnectAttemptsRef.current);
        reconnectAttemptsRef.current++;
        reconnectTimerRef.current = schedule(() => {
          reconnectTimerRef.current = null;
          if (!wsRef.current && xtermRef.current) connectWs();
        }, delay);
      } else {
        xtermRef.current.write('\r\n\x1b[90m' + t('session_ended') + '\x1b[0m\r\n');
        backTimerRef.current = schedule(() => {
          backTimerRef.current = null;
          onBack();
        }, 2000);
      }
    };
  }, [chatId, onBack, pause, schedule]);

  const [ready, setReady] = useState(false);

  // Phase 1: start ttyd (if not already running)
  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    (async () => {
      try {
        const resp = await ocuFetch(`/terminal/${chatId}/start-ttyd`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          signal: controller.signal,
          body: JSON.stringify({ dangerous_mode: dangerousModeRef.current }),
        });
        if (cancelled || !mountedRef.current) return;
        if (!resp.ok) {
          if (!cancelled) {
            // Check if container is stopped (can be restarted) or removed with meta (can be resurrected)
            try {
              const statusResp = await ocuFetch(`/terminal/${chatId}/status?_t=${Date.now()}`,
                { signal: controller.signal });
              const statusData = await statusResp.json();
              if (cancelled || !mountedRef.current) return;
              if (statusData.container_stopped) {
                setStartError('__stopped__');
              } else if (statusData.meta_exists) {
                setStartError('__meta_exists__');
              } else {
                setStartError(t('terminal_fail'));
              }
            } catch { if (!cancelled && mountedRef.current) setStartError(t('terminal_fail')); }
          }
          return;
        }
        const data = await resp.json();
        // If already running, shorter wait (just need WS connection)
        const wait = data.already_running ? 500 : 1500;
        if (!(await pause(wait))) return;
      } catch(e) { if (!cancelled && mountedRef.current) setStartError(t('server_fail')); return; }
      if (!cancelled && mountedRef.current) setReady(true);
    })();
    return () => { cancelled = true; controller.abort(); };
  }, [chatId, pause]);

  // Phase 2: init xterm + connect WS (only after ttyd is ready)
  useEffect(() => {
    if (!ready || !containerRef.current) return;

    const term = new Terminal({
      cursorBlink: true,
      fontSize: 13,
      fontFamily: "'JetBrains Mono', 'Fira Code', Monaco, monospace",
      scrollback: 10000,
      fastScrollModifier: 'shift',
      rightClickSelectsWord: true,
      macOptionClickForcesSelection: true,
      theme: {
        background: '#1a1b26',
        foreground: '#c0caf5',
        cursor: '#c0caf5',
        selectionBackground: '#33467c',
      },
    });
    // Copy: Cmd+C (Mac), Ctrl+Shift+C (Linux) — handle directly in key handler, don't send to terminal
    // Paste: Cmd+V (Mac), Ctrl+V / Ctrl+Shift+V (Windows/Linux) — read clipboard and paste
    term.attachCustomKeyEventHandler((ev) => {
      if (ev.type !== 'keydown') return true;
      const isCopy = (ev.metaKey && ev.key === 'c') ||
                     (ev.ctrlKey && ev.shiftKey && ev.key === 'C');
      const isPaste = (ev.metaKey && ev.key === 'v') ||
                      (ev.ctrlKey && ev.key === 'v');
      if (ev.metaKey && ev.key === 'a') {
        term.selectAll();
        return false;
      }
      if (isCopy) {
        if (term.hasSelection()) navigator.clipboard.writeText(term.getSelection()).catch(() => {});
        return false;
      }
      if (isPaste) {
        navigator.clipboard.readText().then(text => {
          if (xtermRef.current) xtermRef.current.paste(text);
        }).catch(() => {});
        return false;
      }
      return true;
    });
    const fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    try { term.loadAddon(new WebLinksAddon.WebLinksAddon()); } catch(e) {}
    term.open(containerRef.current);
    fitAddon.fit();
    xtermRef.current = term;
    // Mouse select auto-copy: onSelectionChange fires synchronously from mouseup (user gesture)
    term.onSelectionChange(() => {
      if (term.hasSelection()) navigator.clipboard.writeText(term.getSelection()).catch(() => {});
    });
    fitAddonRef.current = fitAddon;

    term.onData((data) => {
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        wsRef.current.send(new TextEncoder().encode('\x30' + data));
      }
    });

    const ro = new ResizeObserver(() => {
      if (fitAddonRef.current && xtermRef.current) {
        fitAddonRef.current.fit();
        if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
          const dims = fitAddonRef.current.proposeDimensions();
          if (dims) {
            wsRef.current.send(new TextEncoder().encode('\x31' + JSON.stringify({ columns: dims.cols, rows: dims.rows })));
          }
        }
      }
    });
    ro.observe(containerRef.current);

    connectWs(resumeId);

    return () => {
      ro.disconnect();
      cancelTimer(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
      cancelTimer(backTimerRef.current);
      backTimerRef.current = null;
      if (wsRef.current) {
        const ws = wsRef.current;
        wsRef.current = null;
        ws.close();
      }
      if (xtermRef.current) { xtermRef.current.dispose(); xtermRef.current = null; }
    };
  }, [ready, connectWs, cancelTimer]);

  const handleToggleSelectMode = useCallback(() => {
    if (!xtermRef.current) return;
    setSelectMode(prev => {
      const enabling = !prev;
      if (enabling) {
        // Disable mouse event reporting — xterm stops forwarding mouse to terminal, drag selects text
        xtermRef.current.write('\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l');
      } else {
        // Re-enable mouse event reporting
        xtermRef.current.write('\x1b[?1000h\x1b[?1002h\x1b[?1006h');
      }
      return enabling;
    });
  }, []);

  const handleClear = useCallback(() => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      // Send Ctrl+L
      wsRef.current.send(new TextEncoder().encode('\x30\x0c'));
    }
  }, []);

  const handleKill = useCallback(async () => {
    // Send Ctrl+C
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(new TextEncoder().encode('\x30\x03'));
    }
    // Kill claude processes
    try {
      const resp = await ocuFetch(`/terminal/${chatId}/processes?_t=${Date.now()}`);
      const data = await resp.json();
      for (const p of (data.processes || [])) {
        await ocuFetch(`/terminal/${chatId}/processes/${p.pid}/kill`, { method: 'POST' });
      }
    } catch(e) {}
    // Kill ttyd + tmux so next "Open terminal" starts fresh (with .bashrc → Claude Code autostart)
    try {
      await ocuFetch(`/terminal/${chatId}/stop-ttyd`, { method: 'POST' });
    } catch(e) {}
    if (wsRef.current) { wsRef.current.close(); wsRef.current = null; }
    if (xtermRef.current) { xtermRef.current.dispose(); xtermRef.current = null; }
    if (mountedRef.current) onBack();
  }, [chatId, onBack]);

  if (startError) {
    const isStopped = startError === '__stopped__';
    const isMetaExists = startError === '__meta_exists__';
    const canRecover = isStopped || isMetaExists;

    const handleRestart = async () => {
      if (!mountedRef.current) return;
      setRestarting(true);
      try {
        const recovered = await recoverStoppedContainer(chatId, dangerousModeRef.current);
        if (!mountedRef.current) return;
        if (recovered.ok) {
          setStartError(null);
          setReady(false);
          if (!(await pause(isMetaExists ? 2500 : (recovered.already_running ? 500 : 1500)))) return;
          setReady(true);
          return;
        }
        setStartError(t('restore_fail'));
      } catch { if (mountedRef.current) setStartError(t('restore_fail')); }
      if (mountedRef.current) setRestarting(false);
    };

    return html`
      <div class="empty-state">
        <div class="empty-icon" dangerouslySetInnerHTML=${{ __html: icon('terminal', 48) }}></div>
        <div class="empty-title">${
          isStopped ? t('container_stopped')
          : isMetaExists ? t('container_removed')
          : startError
        }</div>
        <div class="empty-desc">${
          isStopped ? t('container_stopped_desc')
          : isMetaExists ? t('container_removed_desc')
          : t('container_generic_desc')
        }</div>
        ${canRecover && html`
          <button class="dash-btn-primary" onClick=${handleRestart} disabled=${restarting}>
            <span class="icon-inline" dangerouslySetInnerHTML=${{ __html: icon('play') }}></span>
            ${restarting
              ? (isMetaExists ? t('restoring') : t('starting'))
              : (isMetaExists ? t('restore_container') : t('restart_container'))
            }
          </button>
        `}
        <button class="btn" onClick=${onBack}>
          <span class="icon-inline" dangerouslySetInnerHTML=${{ __html: icon('arrowLeft') }}></span> ${t('back')}
        </button>
      </div>
    `;
  }

  if (!ready) {
    return html`
      <div class="empty-state">
        <div class="spinner"></div>
        <div class="empty-title">${t('starting_terminal')}</div>
      </div>
    `;
  }

  return html`
    <div class="terminal-view">
      <div class="terminal-toolbar">
        <button class="terminal-btn" onClick=${onBack}>
          <span class="icon-inline" dangerouslySetInnerHTML=${{ __html: icon('arrowLeft') }}></span> ${t('back')}
        </button>
        <div style="flex:1"></div>
        <button class="terminal-btn ${selectMode ? 'terminal-btn-active' : ''}" onClick=${handleToggleSelectMode} title="${t('select_mode_title')}">
          ${selectMode ? t('select_on') : t('select_off')}
        </button>
        <button class="terminal-btn terminal-btn-danger" onClick=${handleKill}>
          <span class="icon-inline" dangerouslySetInnerHTML=${{ __html: icon('stop') }}></span> ${t('terminate')}
        </button>
      </div>
      <div class="terminal-container" ref=${containerRef}></div>
    </div>
  `;
}

function TerminalView({ chatId }) {
  const [mode, setMode] = useState('dashboard');
  const [resumeId, setResumeId] = useState(null);
  const [dangerousMode, setDangerousMode] = useState(() => {
    try { return localStorage.getItem('claudeDangerousMode') === '1'; } catch(e) { return false; }
  });
  const toggleDangerous = useCallback((val) => {
    setDangerousMode(val);
    try { localStorage.setItem('claudeDangerousMode', val ? '1' : '0'); } catch(e) {}
  }, []);
  const backToDashboard = useCallback(() => {
    setMode('dashboard');
    setResumeId(null);
  }, []);

  if (mode === 'terminal') {
    return html`<${TerminalSession}
      chatId=${chatId}
      resumeId=${resumeId}
      dangerousMode=${dangerousMode}
      onBack=${backToDashboard}
    />`;
  }

  return html`<${TerminalDashboard}
    chatId=${chatId}
    dangerousMode=${dangerousMode}
    onToggleDangerous=${toggleDangerous}
    onStartSession=${() => { setResumeId(null); setMode('terminal'); }}
    onResumeSession=${(id) => { setResumeId(id); setMode('terminal'); }}
  />`;
}

// =============================================================================
// App
// =============================================================================

// Phase 9.5 — render a USD cost as either "$X.XXXX" or the literal
// "unavailable" so codex/opencode runs (where the CLI does not surface
// cost) never display a misleading "$0.0000". Mirrors the server-side
// branch in mcp_tools.sub_agent (see PITFALLS.md Pitfall 4).
function renderCost(costUsd) {
  if (costUsd === null || costUsd === undefined) return 'unavailable';
  const n = Number(costUsd);
  if (!Number.isFinite(n)) return 'unavailable';
  return `$${n.toFixed(4)}`;
}

function ActiveCliBadge() {
  const [info, setInfo] = useState(null);
  useEffect(() => {
    let cancelled = false;
    loadCliBadge(DESCRIBE_URL).then((data) => {
      if (!cancelled && data) setInfo(data);
    });
    return () => { cancelled = true; };
  }, []);
  if (!info || !info.cli) return null;
  const title = `Active sub-agent CLI: ${info.cli}`
    + (info.default_model ? `  ·  default model: ${info.default_model}` : '')
    + (info.supports_cost ? '' : '  ·  cost reporting: unavailable');
  return html`
    <span class="active-cli-badge" title=${title}
          style="display:inline-flex;align-items:center;gap:4px;padding:2px 8px;
                 margin-right:6px;border:1px solid var(--border, #d4d4d4);
                 border-radius:10px;font-size:11px;line-height:16px;
                 color:var(--muted, #666);background:var(--badge-bg, #f5f5f5);">
      <span style="font-weight:600;text-transform:lowercase">${info.cli}</span>
      ${!info.supports_cost && html`<span style="opacity:0.7">·</span><span style="opacity:0.7">cost n/a</span>`}
    </span>
  `;
}

function validEmbedSelection(data) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) return false;
  const keys = Object.keys(data).sort();
  return keys.length === 4 && keys.join(',') === 'chat_id,file_id,generation,type'
    && data.type === 'ocu:preview-select' && data.chat_id === CHAT_ID
    && typeof data.file_id === 'string' && data.file_id.length > 0
    && data.file_id.length <= 128 && data.file_id.trim().length > 0
    && Number.isSafeInteger(data.generation) && data.generation >= 0;
}

function embeddedFileDisposition(file) {
  if (!file || !['docx', 'xlsx', 'pptx'].includes(file.type)
      || typeof file.mime !== 'string') return 'unsupported';
  const mime = file.mime.split(';', 1)[0].trim().toLowerCase();
  if (!mime || ['text/html', 'image/svg+xml', 'application/xhtml+xml',
    'application/xml', 'text/xml'].includes(mime) || mime.endsWith('+xml')) return 'unsupported';
  try {
    if (typeof file.path !== 'string' || typeof file.url !== 'string') return 'error';
    const parts = file.path.split('/');
    if (parts.some((part) => !part || part === '.' || part === '..' || part.includes('\\'))) return 'error';
    const base = new URL(FILES_BASE + '/', location.origin);
    if (base.origin !== location.origin || base.search || base.hash) return 'error';
    const expected = new URL(base.href + parts.map((part) => encodeURIComponent(part)
      .replace(/[!'()*]/g, (char) => '%' + char.charCodeAt(0).toString(16).toUpperCase())).join('/'));
    const actual = new URL(file.url, location.origin);
    if (actual.origin !== location.origin || actual.href !== expected.href) return 'error';
  } catch {
    return 'error';
  }
  return 'ready';
}

function EmbeddedFilesApp() {
  const [selection, setSelection] = useState(null);
  const [status, setStatus] = useState('waiting');
  const currentRef = useRef(null);
  const requestRef = useRef(null);
  const mountedRef = useRef(false);

  const announce = (request, state) => {
    if (!mountedRef.current || currentRef.current !== request) return;
    setStatus(state);
    window.parent.postMessage({
      type: 'ocu:preview-state', chat_id: CHAT_ID,
      file_id: request.file_id, generation: request.generation, state,
    }, location.origin);
  };

  useEffect(() => {
    mountedRef.current = true;
    const handleMessage = async (event) => {
      if (event.source !== window.parent || event.origin !== location.origin
          || !validEmbedSelection(event.data)
          || event.data.generation <= (currentRef.current?.generation ?? -1)) return;
      const request = event.data;
      requestRef.current?.abort();
      const controller = new AbortController();
      requestRef.current = controller;
      currentRef.current = request;
      setSelection(null);
      announce(request, 'loading');
      const result = await loadOutputsWindow({
        apiUrl: API_URL, pageCount: EMBED_PAGES, chatId: CHAT_ID,
        matchFileId: request.file_id, deadlineMs: EMBED_DEADLINE_MS,
        generation: request.generation,
        currentGeneration: () => currentRef.current?.generation,
        signal: controller.signal,
      });
      if (!mountedRef.current || currentRef.current !== request) return;
      if (result.error !== undefined) { announce(request, 'error'); return; }
      if (result.stale) return;
      if (!result.file) { announce(request, 'missing'); return; }
      const disposition = embeddedFileDisposition(result.file);
      if (disposition !== 'ready') { announce(request, disposition); return; }
      setSelection({ file: result.file, request });
    };
    window.addEventListener('message', handleMessage);
    window.parent.postMessage({ type: 'ocu:preview-ready', chat_id: CHAT_ID }, location.origin);
    return () => {
      mountedRef.current = false;
      currentRef.current = null;
      requestRef.current?.abort();
      window.removeEventListener('message', handleMessage);
    };
  }, []);

  return html`<div style="display:flex;flex:1;flex-direction:column;min-height:0">
    ${selection ? html`<${FilesView} files=${[selection.file]} selectedFile=${selection.file}
      onSelectFile=${() => {}} selectionKey=${selection.request.generation}
      onRenderState=${(state) => announce(selection.request, state)} embedded=${true} />`
      : html`<div class="empty-state" role="status">${status}</div>`}
  </div>`;
}

function EmbeddedRuntimeApp({ mode }) {
  const [browserStatus, setBrowserStatus] = useState('loading');

  useEffect(() => {
    const stopHeartbeat = startWorkspaceHeartbeat(CHAT_ID);
    if (mode === 'terminal') return stopHeartbeat;

    let retired = false;
    let timer;
    let controller;
    const checkStatus = async () => {
      controller = new AbortController();
      try {
        const response = await ocuFetch(`/browser/${CHAT_ID}/status?_t=${Date.now()}`,
          { cache: 'no-store', signal: controller.signal });
        if (!response.ok) throw new Error('browser status unavailable');
        const status = await response.json();
        if (!status || typeof status.active !== 'boolean' || !Array.isArray(status.pages))
          throw new Error('invalid browser status');
        if (!retired) setBrowserStatus(status.active ? 'active' : 'inactive');
      } catch {
        if (!retired) setBrowserStatus('unavailable');
      } finally {
        if (!retired) timer = setTimeout(checkStatus, 3000);
      }
    };
    checkStatus();
    return () => {
      retired = true;
      clearTimeout(timer);
      controller?.abort();
      stopHeartbeat();
    };
  }, [mode]);

  return html`<div style="display:flex;flex:1;flex-direction:column;min-height:0">
    ${mode === 'browser'
      ? html`
        ${browserStatus === 'loading' && html`<div role="status">Checking browser status</div>`}
        ${browserStatus === 'unavailable' && html`<div role="alert">Browser unavailable</div>`}
        <${BrowserView} chatId=${CHAT_ID} browserActive=${browserStatus === 'active'} />
      `
      : html`<div class="terminal-panel"><${TerminalView} chatId=${CHAT_ID} /></div>`}
  </div>`;
}

function App() {
  const [files, setFiles] = useState([]);
  const [selectedFile, setSelectedFile] = useState(null);
  const [currentView, setCurrentView] = useState('files');
  const [browserActive, setBrowserActive] = useState(false);
  const [terminalActive, setTerminalActive] = useState(false);
  const [seenFiles, setSeenFiles] = useState(new Set());
  const [listingError, setListingError] = useState(null);
  const [hasMore, setHasMore] = useState(false);
  const pageCountRef = useRef(1);
  const listingGenerationRef = useRef(0);
  const fileRevisionsRef = useRef(new Map());
  const explicitOfficeRef = useRef(false);
  const browserViewerRef = useRef(null);
  const lastSyncTimeRef = useRef(null);
  const syncDotRef = useRef(null);

  const applyLoadedFiles = useCallback((newFiles) => {
    const autoSelectTarget = pickAutoSelect(newFiles, fileRevisionsRef.current);
    const revisions = fileRevisionsRef.current;
    revisions.clear();
    for (const f of newFiles) revisions.set(f.path, f.revision);
    setFiles(newFiles);
    setSelectedFile((prev) => applyListingSelection(newFiles, prev, autoSelectTarget, explicitOfficeRef.current));
    setSeenFiles((prev) => {
      const next = new Set(prev);
      newFiles.forEach((f) => next.add(f.path));
      return next;
    });
  }, []);

  const fetchFiles = useCallback(async () => {
    const dot = syncDotRef.current;
    if (dot) { dot.classList.add('syncing'); dot.title = t('checking'); }
    const generation = ++listingGenerationRef.current;
    const loaded = await loadOutputsWindow({
      apiUrl: API_URL,
      pageCount: pageCountRef.current,
      generation,
      currentGeneration: () => listingGenerationRef.current,
    });
    if (loaded.stale || generation !== listingGenerationRef.current) return loaded;
    if (loaded.error) {
      setListingError(t('listing_error'));
      if (dot) { dot.classList.remove('syncing'); dot.title = t('error'); }
      return loaded;
    }
    setListingError(null);
    applyLoadedFiles(loaded.files);
    setHasMore(Boolean(loaded.next_cursor));
    if (dot) { dot.classList.remove('syncing'); dot.title = t('synced'); }
    lastSyncTimeRef.current = Date.now();
    return loaded;
  }, [applyLoadedFiles]);

  const loadMore = useCallback(async () => {
    pageCountRef.current += 1;
    await fetchFiles();
  }, [fetchFiles]);

  const checkBrowserStatus = useCallback(async () => {
    try {
      const resp = await ocuFetch(`/browser/${CHAT_ID}/status?_t=${Date.now()}`, { cache: 'no-store' });
      const data = await resp.json();
      setBrowserActive(prev => {
        if (data.active && !prev) {
          setCurrentView('browser');
        }
        if (!data.active && prev && browserViewerRef.current?.connected) {
          return true;
        }
        return data.active;
      });
      if (data.active && data.pages && data.pages.length > 0) {
        const urlBar = document.getElementById('browserUrlBar');
        if (urlBar) urlBar.textContent = data.pages[0].url || '';
      }
    } catch (e) {}
  }, []);

  const checkTerminalStatus = useCallback(async () => {
    try {
      const resp = await ocuFetch(`/terminal/${CHAT_ID}/processes?_t=${Date.now()}`, { cache: 'no-store' });
      const data = await resp.json();
      setTerminalActive(data.processes && data.processes.length > 0);
    } catch(e) {}
  }, []);

  // Polling loop
  useEffect(() => {
    const poll = async () => {
      await fetchFiles();
      await checkBrowserStatus();
      await checkTerminalStatus();
    };
    poll();
    let timer = setInterval(poll, 3000);

    const visHandler = () => {
      clearInterval(timer);
      if (document.hidden) {
        timer = setInterval(poll, 15000);
      } else {
        poll();
        timer = setInterval(poll, 3000);
      }
    };
    document.addEventListener('visibilitychange', visHandler);

    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', visHandler);
    };
  }, [fetchFiles, checkBrowserStatus, checkTerminalStatus]);

  useEffect(() => startWorkspaceHeartbeat(CHAT_ID), []);

  const onSelectFile = useCallback((file) => {
    explicitOfficeRef.current = Boolean(file);
    setSelectedFile(file);
  }, []);

  useEffect(() => {
    const handler = (event) => {
      const data = event && event.data;
      if (!data || data.type !== 'iframe-link-click') return;
      handleLinkClick(data.href, data.resolvedUrl, files, selectedFile, onSelectFile);
    };
    window.addEventListener('message', handler);
    return () => window.removeEventListener('message', handler);
  }, [files, selectedFile, onSelectFile]);

  return html`
    <div class="toolbar">
      <div class="toolbar-left">
        <${ViewTabs}
          currentView=${currentView}
          onSwitch=${setCurrentView}
          browserActive=${browserActive}
          terminalActive=${terminalActive}
        />
        ${currentView === 'files' && files.length > 0 && html`
          <${FileSelector}
            files=${files}
            selectedFile=${selectedFile}
            seenFiles=${seenFiles}
            onSelect=${onSelectFile}
          />
        `}
        ${currentView === 'files' && hasMore && html`
          <button class="btn" type="button" onClick=${loadMore}>${t('more_files')}</button>
        `}
        ${listingError && html`<span class="listing-error">${listingError}</span>`}
      </div>
      <div class="toolbar-right">
        <${ActiveCliBadge} />
        <a class="btn btn-icon" href=${location.href} target="_blank" rel="noopener" title="${t('open_new_tab')}">
          <span class="icon-inline" dangerouslySetInnerHTML=${{ __html: icon('externalLink') }}></span>
        </a>
        <div class="status">
          <span class="dot" ref=${syncDotRef} title="${t('waiting_files')}"></span>
        </div>
        ${currentView === 'files' && selectedFile && html`
          <a class="btn btn-icon" href="${selectedFile.url}?download=1" download title="${t('download_file')}">
            <span class="icon-inline" dangerouslySetInnerHTML=${{ __html: icon('download') }}></span>
          </a>
        `}
        ${currentView === 'files' && files.length > 0 && html`
          <a class="btn btn-icon" href="${FILES_BASE}/archive" download title="${t('download_all')}">
            <span class="icon-inline" dangerouslySetInnerHTML=${{ __html: icon('archive') }}></span>
          </a>
        `}
      </div>
    </div>

    <div style="display:${currentView === 'files' ? 'flex' : 'none'};flex:1;flex-direction:column;overflow:hidden">
      <${FilesView}
        files=${files}
        selectedFile=${selectedFile}
        onSelectFile=${onSelectFile}
      />
    </div>

    <div style="display:${currentView === 'browser' ? 'flex' : 'none'};flex:1;flex-direction:column;overflow:hidden">
      <${BrowserView}
        chatId=${CHAT_ID}
        browserActive=${browserActive}
        onBrowserViewerRef=${(v) => { browserViewerRef.current = v; }}
      />
    </div>

    <div class="terminal-panel" style="display:${currentView === 'terminal' ? 'flex' : 'none'}">
      <${TerminalView} chatId=${CHAT_ID} />
    </div>
  `;
}

// =============================================================================
// Mount
// =============================================================================

render(EMBED_MODE === 'files' ? html`<${EmbeddedFilesApp} />`
  : EMBED_MODE === 'browser' || EMBED_MODE === 'terminal'
    ? html`<${EmbeddedRuntimeApp} mode=${EMBED_MODE} />`
    : EMBED_MODE === 'invalid' ? html`<div class="empty-state" role="alert">Invalid preview embedding</div>`
      : html`<${App} />`, document.getElementById('app'));
