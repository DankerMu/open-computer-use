// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
// Shared SPA request wrapper: prefix once, workspace header, explicit server URLs.

export const WORKSPACE_HEADER = 'ocu-workspace';
export const PAGE_LIMIT = 100;
export const HEARTBEAT_MS = 120000;
export const FORMULA_UNCOMPUTED = 'uncomputed';

function moduleParentPath() {
  const directory = new URL('.', import.meta.url);
  const parent = new URL('..', directory);
  let pathname = parent.pathname;
  if (pathname.length > 1 && pathname.endsWith('/')) pathname = pathname.slice(0, -1);
  return pathname === '/' ? '' : pathname;
}

function splitPath(url) {
  const query = url.indexOf('?');
  const hash = url.indexOf('#');
  let end = url.length;
  if (query >= 0) end = Math.min(end, query);
  if (hash >= 0) end = Math.min(end, hash);
  return { pathname: url.slice(0, end), rest: url.slice(end) };
}

function hasPrefixSegment(pathname, prefix) {
  if (!prefix) return false;
  return pathname === prefix || pathname.startsWith(prefix + '/');
}

export function publicPrefix() {
  return moduleParentPath();
}

export function moduleAssetUrl(name) {
  return new URL(name, import.meta.url).href;
}

export function resolveClientUrl(url, options = {}) {
  if (options.serverUrl) return url;
  if (typeof url !== 'string') return url;
  if (url.startsWith('//') || !url.startsWith('/')) return url;
  const prefix = publicPrefix();
  if (!prefix) return url;
  const { pathname, rest } = splitPath(url);
  if (hasPrefixSegment(pathname, prefix)) return url;
  return prefix + pathname + rest;
}

export function mergeRequestHeaders(init) {
  const headers = new Headers();
  if (!init) {
    headers.set('X-Requested-With', WORKSPACE_HEADER);
    return headers;
  }
  if (init.headers) {
    new Headers(init.headers).forEach((value, key) => {
      headers.set(key, value);
    });
  }
  if (init.header) {
    new Headers(init.header).forEach((value, key) => {
      headers.set(key, value);
    });
  }
  headers.set('X-Requested-With', WORKSPACE_HEADER);
  return headers;
}

export function workspaceHttpHeaders() {
  return { 'X-Requested-With': WORKSPACE_HEADER };
}

export function ocuFetch(url, init = {}) {
  const serverUrl = Boolean(init && init.serverUrl);
  const resolved = resolveClientUrl(url, { serverUrl });
  const headers = mergeRequestHeaders(init);
  const next = { ...init, headers };
  delete next.serverUrl;
  delete next.header;
  return fetch(resolved, next);
}

export function terminalWsUrl(chatId) {
  const protocol = (typeof location !== 'undefined' && location.protocol === 'https:') ? 'wss:' : 'ws:';
  const host = typeof location !== 'undefined' ? location.host : '';
  return `${protocol}//${host}${resolveClientUrl(`/terminal/${chatId}/ws`)}`;
}

export function startWorkspaceHeartbeat(chatId, timers = globalThis) {
  const beat = () => {
    ocuFetch(`/terminal/${chatId}/heartbeat`, { cache: 'no-store' }).catch(() => {});
  };
  const id = timers.setInterval(beat, HEARTBEAT_MS);
  return () => timers.clearInterval(id);
}

export async function loadCliBadge(describeUrl) {
  if (!describeUrl) return null;
  try {
    const resp = await ocuFetch(describeUrl, { serverUrl: true, cache: 'no-store' });
    if (!resp.ok) return null;
    const data = await resp.json();
    return data.cli_badge || null;
  } catch {
    return null;
  }
}

export async function recoverStoppedContainer(chatId, dangerousMode) {
  const launch = await ocuFetch(`/terminal/${chatId}/restart-container`, { method: 'POST' });
  if (!launch.ok) return { ok: false, launched: false };
  const ttyd = await ocuFetch(`/terminal/${chatId}/start-ttyd`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ dangerous_mode: Boolean(dangerousMode) }),
  });
  let alreadyRunning = false;
  if (ttyd.ok) {
    try {
      const body = await ttyd.json();
      alreadyRunning = Boolean(body && body.already_running);
    } catch {
      alreadyRunning = false;
    }
  }
  return { ok: ttyd.ok, launched: true, already_running: alreadyRunning };
}

function listingUrl(apiUrl, cursor, limit) {
  const params = [];
  if (cursor) params.push('cursor=' + encodeURIComponent(cursor));
  params.push('limit=' + encodeURIComponent(String(limit)));
  const sep = apiUrl.includes('?') ? '&' : '?';
  return apiUrl + sep + params.join('&');
}

export async function fetchOutputsPage(apiUrl, { cursor, limit = PAGE_LIMIT } = {}) {
  return ocuFetch(listingUrl(apiUrl, cursor, limit), { serverUrl: true, cache: 'no-store' });
}

export async function loadOutputsWindow({
  apiUrl,
  pageCount,
  generation,
  currentGeneration,
  fetchPage = fetchOutputsPage,
}) {
  const depth = Math.max(1, pageCount | 0);
  const pages = [];
  let cursor = null;
  let staleRetries = 0;
  let loaded = 0;
  const seenCursors = new Set();
  while (loaded < depth) {
    if (generation !== currentGeneration()) return { stale: true, files: null };
    let resp;
    try {
      resp = await fetchPage(apiUrl, { cursor, limit: PAGE_LIMIT });
    } catch (err) {
      return { error: 'network', files: null };
    }
    if (resp.status === 409 && cursor && staleRetries === 0) {
      staleRetries += 1;
      cursor = null;
      pages.length = 0;
      loaded = 0;
      seenCursors.clear();
      continue;
    }
    if (resp.status === 409) return { error: 'stale-cursor', files: null };
    if (!resp.ok) return { error: resp.status, files: null };
    let body;
    try {
      body = await resp.json();
    } catch {
      return { error: 'body', files: null };
    }
    if (pages.length && pages[0].revision !== body.revision) {
      return { error: 'revision-mismatch', files: null };
    }
    if (cursor) {
      if (seenCursors.has(cursor)) return { error: 'repeated-cursor', files: null };
      seenCursors.add(cursor);
    }
    pages.push(body);
    loaded += 1;
    if (!body.next_cursor) break;
    cursor = body.next_cursor;
  }
  if (generation !== currentGeneration()) return { stale: true, files: null };
  const last = pages[pages.length - 1];
  return {
    files: pages.flatMap((page) => page.files || []),
    revision: last.revision,
    next_cursor: last.next_cursor,
    total: last.total,
    pages: pages.length,
  };
}

export function renderKey(file) {
  if (!file) return '';
  return String(file.path) + '\0' + String(file.revision);
}

export function pickAutoSelect(files, previousRevisions) {
  for (const file of files) {
    if (file.path.includes('/')) continue;
    const prev = previousRevisions.get(file.path);
    if (prev === undefined || prev !== file.revision) return file;
  }
  return null;
}

function sameIdentity(previous, next) {
  return next && previous
    && next.path === previous.path
    && next.revision === previous.revision;
}

export function applyListingSelection(files, previous, autoTarget, explicitSelection) {
  if (!files.length) return null;
  if (autoTarget && !explicitSelection) return autoTarget;
  if (previous && previous.file_id) {
    const kept = files.find((file) => file.file_id === previous.file_id);
    if (kept) return sameIdentity(previous, kept) ? previous : kept;
  }
  if (autoTarget) return autoTarget;
  if (!previous) return files.find((file) => !file.path.includes('/')) || files[0];
  return files.find((file) => !file.path.includes('/')) || files[0] || null;
}

export function formulaHasCachedValue(cell) {
  if (!cell || cell.f == null || cell.f === '') return true;
  if (cell.t === 'z') return false;
  return Object.prototype.hasOwnProperty.call(cell, 'v') && cell.v !== undefined;
}

export function formulaCellDisplay(cell) {
  if (!cell) return '';
  if (!formulaHasCachedValue(cell)) return FORMULA_UNCOMPUTED;
  if (cell.v === false) return 'FALSE';
  if (cell.v === true) return 'TRUE';
  if (cell.v === 0) return '0';
  if (cell.w != null) return String(cell.w);
  if (cell.v == null) return '';
  return String(cell.v);
}
