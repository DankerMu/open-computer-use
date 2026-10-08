// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
import { ocuFetch } from './ocu-request.js';

const OPEN_KEYS = 'chat_id,file_id,generation,type';
const COMMAND_KEYS = 'chat_id,command,generation,type';
const SESSION_STATES = new Set(['opening', 'editing', 'saving', 'closing', 'closed', 'conflict', 'error', 'orphaned']);
const FINAL_STATES = new Set(['closed', 'error', 'orphaned']);
const REFUSALS = new Map([
  ['unknown_file', 404], ['unsupported_type', 415], ['file_too_large', 413],
  ['unsafe_path', 422], ['corrupt_document', 422], ['storage_low', 503],
  ['unpublished_version', 409],
]);
// A stalled API script must become a visible failure instead of leaving opening forever.
const API_LOAD_TIMEOUT_MS = 10000;
const POLL_DELAY_MS = 1000;
const AUTO_SAVE_DELAY_MS = 5 * 60 * 1000;

function loadEditorApi(origin, hostWindow, hostDocument) {
  return new Promise((resolve, reject) => {
    const script = hostDocument.createElement('script');
    script.src = new URL('/web-apps/apps/api/documents/api.js', origin).href;
    script.async = true;
    let settled = false;
    const finish = (reason) => {
      if (settled) return;
      settled = true;
      hostWindow.clearTimeout(deadline);
      script.onload = null;
      script.onerror = null;
      if (reason) {
        script.remove();
        reject(new Error(reason));
      } else {
        resolve(hostWindow.DocsAPI.DocEditor);
      }
    };
    const deadline = hostWindow.setTimeout(() => finish('editor_api_timeout'), API_LOAD_TIMEOUT_MS);
    script.onload = () => finish(typeof hostWindow.DocsAPI?.DocEditor === 'function' ? null : 'editor_api_unavailable');
    script.onerror = () => finish('editor_api_load_failed');
    hostDocument.head.appendChild(script);
  });
}

function reasonOf(body, fallback) {
  return typeof body?.reason === 'string' && body.reason.length > 0 ? body.reason : fallback;
}

function validStatus(status, sessionId) {
  if (!status || typeof status !== 'object' || Array.isArray(status) || status.session_id !== sessionId ||
      !SESSION_STATES.has(status.state) || typeof status.workspace_changed !== 'boolean') return false;
  if (status.reason !== null && (typeof status.reason !== 'string' || !status.reason)) return false;
  if ((status.state === 'conflict' || status.state === 'error') && !status.reason) return false;
  const sequences = [status.last_published_seq, status.last_committed_seq, status.save_seq];
  return sequences.every(value => Number.isSafeInteger(value) && value >= 0) &&
    sequences[0] <= sequences[1] && sequences[1] <= sequences[2];
}

export function createOfficeEditorHost({
  chatId, docserverOrigin, container, window: hostWindow = window, document: hostDocument = document,
}) {
  const origin = hostWindow.location.origin;
  const statusElement = hostDocument.createElement('div');
  statusElement.className = 'empty-state';
  statusElement.setAttribute('role', 'status');
  statusElement.textContent = 'Office editor idle';
  const documentElement = hostDocument.createElement('div');
  documentElement.id = 'office-document';
  documentElement.style.width = '100%';
  documentElement.style.height = '100vh';
  documentElement.hidden = true;
  container.replaceChildren(statusElement, documentElement);

  let opened = null;
  let sessionId = null;
  let snapshot = null;
  let editor = null;
  let modification = 0;
  let coveredModification = 0;
  const saveCoverage = new Map();
  let refusalReason = null;
  let localError = null;
  let commandReason = null;
  let lastReport = null;
  let pollTimer = null;
  let autoSaveTimer = null;
  let statusFlight = null;
  let refreshAgain = false;
  let mutationEpoch = 0;
  let autoSave = null;
  let queuedPublish = null;
  let publishRequests = 0;
  let latestCommand = 0;
  let closeRequested = false;
  let closeAccepted = false;

  const final = () => refusalReason !== null || localError !== null || FINAL_STATES.has(snapshot?.state);
  const report = () => {
    if (!opened) return;
    const state = refusalReason !== null ? 'refused' : localError !== null ? 'error' : snapshot?.state || 'opening';
    const reason = refusalReason ?? localError ?? commandReason ?? snapshot?.reason ?? null;
    const next = {
      type: 'ocu:office-state', chat_id: chatId, file_id: opened.fileId, generation: opened.generation,
      session_id: sessionId, state,
      dirty: state === 'closed' ? false : state === 'conflict' || modification > coveredModification ||
        Boolean(snapshot && snapshot.last_committed_seq > snapshot.last_published_seq),
      workspace_changed: snapshot?.workspace_changed ?? false, reason,
    };
    statusElement.setAttribute('role', state === 'error' || state === 'refused' ? 'alert' : 'status');
    statusElement.textContent = `Office editor ${state}${reason ? ': ' + reason : ''}`;
    statusElement.hidden = editor !== null && reason === null;
    // The shared empty-state display rule overrides the browser's hidden rule.
    statusElement.style.display = statusElement.hidden ? 'none' : '';
    documentElement.hidden = editor === null;
    if (lastReport && Object.keys(next).every(key => next[key] === lastReport[key])) return;
    lastReport = next;
    hostWindow.parent.postMessage(next, origin);
  };
  const fail = reason => {
    if (final()) return;
    localError = reason;
    stopTimers();
    report();
  };
  const coverModifications = () => {
    for (const [sequence, generation] of saveCoverage) {
      if (snapshot && sequence <= snapshot.last_committed_seq) {
        coveredModification = Math.max(coveredModification, generation);
        saveCoverage.delete(sequence);
      }
    }
  };
  const applyStatus = status => {
    if (!opened || !sessionId || final()) return false;
    if (!validStatus(status, sessionId)) {
      fail('invalid_session_status');
      return false;
    }
    snapshot = { ...status };
    coverModifications();
    if (final()) stopTimers();
    else syncAutoSave();
    report();
    drainQueuedPublish();
    if (!autoSave?.pending && snapshot.state === 'editing') autoSave = null;
    return true;
  };
  function stopTimers() {
    hostWindow.clearTimeout(pollTimer);
    hostWindow.clearTimeout(autoSaveTimer);
    pollTimer = autoSaveTimer = null;
    queuedPublish = null;
  }

  function syncAutoSave() {
    if (final() || closeRequested || snapshot?.state !== 'editing') {
      hostWindow.clearTimeout(autoSaveTimer);
      autoSaveTimer = null;
    } else if (autoSaveTimer === null) {
      autoSaveTimer = hostWindow.setTimeout(() => {
        autoSaveTimer = null;
        if (!final() && !closeRequested && snapshot?.state === 'editing' &&
            modification > coveredModification && !autoSave?.pending) void save('persist');
        syncAutoSave();
      }, AUTO_SAVE_DELAY_MS);
    }
  }

  function drainQueuedPublish() {
    if (!queuedPublish || queuedPublish.pending || publishRequests || closeRequested || final()) return;
    if (queuedPublish.sequence === null) {
      queuedPublish = null;
      commandReason = 'session_not_editing';
      report();
    } else if (snapshot?.state === 'editing') {
      queuedPublish = null;
      void save();
    }
  }

  function refreshStatus() {
    if (!sessionId || final()) return Promise.resolve(false);
    if (statusFlight) {
      refreshAgain = true;
      return statusFlight;
    }
    hostWindow.clearTimeout(pollTimer);
    pollTimer = null;
    statusFlight = (async () => {
      let applied = false;
      do {
        refreshAgain = false;
        const epoch = mutationEpoch;
        try {
          const response = await ocuFetch(`/api/office/${encodeURIComponent(chatId)}/sessions/${encodeURIComponent(sessionId)}`, { cache: 'no-store' });
          const status = await response.json();
          if (final()) return false;
          if (epoch !== mutationEpoch) {
            refreshAgain = true;
            continue;
          }
          if (!response.ok) {
            fail(reasonOf(status, 'session_status_failed'));
            return false;
          }
          applied = applyStatus(status);
        } catch {
          if (epoch !== mutationEpoch) refreshAgain = true;
          else fail('session_status_failed');
        }
      } while (refreshAgain && !final());
      return applied;
    })().finally(() => {
      statusFlight = null;
      if (!final()) pollTimer = hostWindow.setTimeout(() => {
        pollTimer = null;
        void refreshStatus();
      }, POLL_DELAY_MS);
    });
    return statusFlight;
  }

  async function save(intent = 'publish') {
    if (!sessionId || closeRequested || final()) return;
    const requestedModification = modification;
    const command = ++latestCommand;
    const autoOwner = intent === 'publish' && autoSave && (autoSave.pending ||
      snapshot?.state === 'saving' && snapshot.save_seq === autoSave.sequence) ? autoSave : null;
    const attempt = intent === 'persist' ? { pending: true, sequence: null } : null;
    mutationEpoch++;
    if (attempt) autoSave = attempt;
    else publishRequests++;
    try {
      const response = await ocuFetch(`/api/office/${encodeURIComponent(chatId)}/sessions/${encodeURIComponent(sessionId)}/save`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ intent }),
      });
      const body = await response.json();
      if (final()) return;
      if (!response.ok) {
        if (command === latestCommand) {
          if (autoOwner && response.status === 409 && body?.reason === 'session_not_editing') queuedPublish = autoOwner;
          else commandReason = reasonOf(body, 'save_failed');
        }
      } else if (response.status !== 202 || body?.session_id !== sessionId ||
          !Number.isSafeInteger(body.save_seq) || body.save_seq < 1 || body.intent !== intent) {
        if (command === latestCommand) commandReason = 'invalid_save_response';
      } else {
        if (command === latestCommand) commandReason = null;
        if (attempt) attempt.sequence = body.save_seq;
        saveCoverage.set(body.save_seq, requestedModification);
        coverModifications();
        report();
      }
    } catch {
      if (!final() && command === latestCommand) commandReason = 'save_failed';
    } finally {
      mutationEpoch++;
      if (attempt) attempt.pending = false;
      else publishRequests--;
    }
    await refreshStatus();
  }

  function releaseEditor() {
    const instance = editor;
    editor = null;
    instance?.destroyEditor();
  }

  async function close() {
    if (!sessionId || closeRequested || final()) return;
    closeRequested = true;
    queuedPublish = null;
    const command = ++latestCommand;
    mutationEpoch++;
    syncAutoSave();
    try {
      const response = await ocuFetch(`/api/office/${encodeURIComponent(chatId)}/sessions/${encodeURIComponent(sessionId)}/close`, { method: 'POST' });
      const body = await response.json();
      if (final()) return;
      if (!response.ok) {
        commandReason = reasonOf(body, 'close_failed');
      } else if (response.status !== 202 || body?.session_id !== sessionId ||
          !Number.isSafeInteger(body.save_seq) || body.save_seq < 1 ||
          !['closing', 'closed', 'conflict'].includes(body.state)) {
        commandReason = 'invalid_close_response';
      } else {
        commandReason = null;
        closeAccepted = true;
      }
    } catch {
      if (!final() && command === latestCommand) commandReason = 'close_failed';
    } finally {
      mutationEpoch++;
      if (!closeAccepted) closeRequested = false;
    }
    if (closeAccepted) {
      try {
        releaseEditor();
      } catch {
        fail('editor_release_failed');
      }
    }
    await refreshStatus();
  }


  async function openSession() {
    const base = `/api/office/${encodeURIComponent(chatId)}`;
    let created;
    try {
      const response = await ocuFetch(`${base}/documents/${encodeURIComponent(opened.fileId)}/sessions`, { method: 'POST' });
      const body = await response.json();
      if (!response.ok) {
        const reason = reasonOf(body, 'session_creation_failed');
        if (REFUSALS.get(reason) === response.status) {
          refusalReason = reason;
          report();
        } else {
          fail(reason);
        }
        return;
      }
      if (typeof body?.session_id !== 'string' || !body.session_id) {
        fail('invalid_session_response');
        return;
      }
      created = body;
      sessionId = body.session_id;
      report();
    } catch {
      fail('session_creation_failed');
      return;
    }

    if (!await refreshStatus() || final() || closeAccepted) return;
    if (created.editor_config === null && snapshot.state === 'conflict') return;
    if (!created.editor_config || typeof created.editor_config !== 'object' || Array.isArray(created.editor_config)) {
      fail('editor_configuration_missing');
      return;
    }

    let DocEditor;
    try {
      DocEditor = await loadEditorApi(docserverOrigin, hostWindow, hostDocument);
    } catch (error) {
      if (!closeAccepted) fail(error.message || 'editor_api_load_failed');
      return;
    }
    if (final() || closeAccepted) return;
    const events = {
      ...created.editor_config.events,
      onDocumentStateChange(event) {
        if (final() || closeAccepted || event?.data !== true) return;
        // false acknowledges delivery to DocumentServer, not a committed workspace save.
        modification++;
        report();
      },
      onError(event) {
        if (closeAccepted) return;
        const code = event?.data?.errorCode;
        fail(Number.isInteger(code) ? `editor_error_${code}` : 'editor_error');
      },
    };
    try {
      documentElement.hidden = false;
      editor = new DocEditor(documentElement.id, { ...created.editor_config, events });
      report();
    } catch {
      documentElement.replaceChildren();
      fail('editor_creation_failed');
    }
  }

  function receive(event) {
    const data = event.data;
    if (event.source !== hostWindow.parent || event.origin !== origin ||
        !data || typeof data !== 'object' || Array.isArray(data) || data.chat_id !== chatId) return;
    if (data.type === 'ocu:office-command') {
      if (opened && Object.keys(data).sort().join(',') === COMMAND_KEYS && data.generation === opened.generation) {
        if (data.command === 'save') void save();
        else if (data.command === 'close') void close();
      }
      return;
    }
    if (opened || Object.keys(data).sort().join(',') !== OPEN_KEYS || data.type !== 'ocu:office-open' ||
        typeof data.file_id !== 'string' || data.file_id.length === 0 ||
        !Number.isSafeInteger(data.generation) || data.generation < 0) return;
    opened = { fileId: data.file_id, generation: data.generation };
    report();
    void openSession();
  }

  hostWindow.addEventListener('message', receive);
  hostWindow.parent.postMessage({ type: 'ocu:office-ready', chat_id: chatId }, origin);
  return { applyStatus };
}
