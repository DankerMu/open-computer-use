// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
// BrowserViewer — CDP screencast + interactive input proxy
// Extracted from inline app.py preview SPA

import { t } from './locale.js';
import { ocuFetch } from './ocu-request.js';

const _browserBasePath = new URL('..', import.meta.url).pathname;

export class BrowserViewer {
  constructor(canvas, chatId) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.chatId = chatId;
    this.ws = null;
    this.cmdId = 1;
    this.pageId = null;
    this.connected = false;
    this._img = new Image();
    this._deviceWidth = 0;
    this._deviceHeight = 0;
    this._targetWidth = 0;
    this._targetHeight = 0;
    this._navigating = false;
    this._loadingShownAt = 0;
    this._loadingHideTimer = null;
    this._viewportFixTimer = null;
    this._viewportFixPending = false;
    this._lastMouseMove = 0;
    this._tabPollTimer = null;
    this._tabController = null;
    this._resizeObserver = null;
    this._disposed = false;
    this._connectController = null;
    this._connectTimer = null;
    this._connectResolve = null;
    this._inputTimers = new Set();

    this._bindInputHandlers();
  }

  _bindInputHandlers() {
    const c = this.canvas;
    c.addEventListener('mousedown', (e) => {
      if (!this.connected) return;
      e.preventDefault();
      const coords = this._getCoords(e);
      const mods = this._modifiers(e);
      // Move cursor first — triggers mouseenter/mouseover in remote browser
      this._send('Input.dispatchMouseEvent', {
        type: 'mouseMoved', x: coords.x, y: coords.y,
        button: 'none', clickCount: 0, modifiers: mods
      });
      // Press after microtask delay — lets Chromium process hover before click
      // (fixes JS widgets like SearchBooster that require hover state)
      const timer = setTimeout(() => {
        this._inputTimers.delete(timer);
        if (!this._disposed) this._send('Input.dispatchMouseEvent', {
          type: 'mousePressed', x: coords.x, y: coords.y,
          button: 'left', clickCount: 1, modifiers: mods
        });
      }, 0);
      this._inputTimers.add(timer);
      window.focus();
      c.focus();
    });
    c.addEventListener('mouseup', (e) => this._onMouse('mouseReleased', e));
    c.addEventListener('click', (e) => { e.preventDefault(); window.focus(); c.focus(); });
    c.addEventListener('contextmenu', (e) => e.preventDefault());
    c.addEventListener('keydown', (e) => this._onKey('keyDown', e));
    c.addEventListener('keyup', (e) => this._onKey('keyUp', e));

    // Throttled mousemove
    c.addEventListener('mousemove', (e) => {
      const now = Date.now();
      if (now - this._lastMouseMove < 100) return;
      this._lastMouseMove = now;
      this._onMouse('mouseMoved', e);
    });

    // Wheel scroll
    c.addEventListener('wheel', (e) => {
      if (!this.connected) return;
      e.preventDefault();
      const coords = this._getCoords(e);
      this._send('Input.dispatchMouseEvent', {
        type: 'mouseWheel', x: coords.x, y: coords.y,
        deltaX: Math.round(e.deltaX), deltaY: Math.round(e.deltaY),
        modifiers: this._modifiers(e)
      });
    }, { passive: false });
  }

  async connect() {
    if (this._disposed) return false;
    const controller = new AbortController();
    this._connectController = controller;
    try {
      const resp = await ocuFetch(`${_browserBasePath}browser/${this.chatId}/json?_t=${Date.now()}`,
        { cache: 'no-store', signal: controller.signal });
      if (!resp.ok) return false;
      const pages = await resp.json();
      if (this._disposed || !Array.isArray(pages)) return false;
      const page = pages.find(p => p.type === 'page' && typeof p.id === 'string' && p.id);
      if (!page) return false;
      this.pageId = page.id;

      const wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      const wsUrl = `${wsProto}//${location.host}${_browserBasePath}browser/${this.chatId}/devtools/page/${this.pageId}`;
      const ws = new WebSocket(wsUrl);
      this.ws = ws;
      return await new Promise(resolve => {
        let settled = false;
        const finish = ok => {
          if (settled) return;
          settled = true;
          clearTimeout(this._connectTimer);
          this._connectTimer = null;
          this._connectResolve = null;
          if (!ok) {
            this.connected = false;
            if (this._resizeObserver) { this._resizeObserver.disconnect(); this._resizeObserver = null; }
            if (this._tabPollTimer) { clearInterval(this._tabPollTimer); this._tabPollTimer = null; }
            if (this.ws === ws) {
              this.ws = null;
              ws.close();
            }
          }
          resolve(ok);
        };
        this._connectResolve = finish;
        this._connectTimer = setTimeout(() => finish(false), 10000);
        ws.onopen = () => {
          if (this._disposed || this.ws !== ws) { finish(false); return; }
          this.connected = true;
          try {
            this._send('Page.enable', {});
            this._send('Fetch.enable', { handleAuthRequests: true });
            this._send('Browser.setDownloadBehavior', {
              behavior: 'allow', downloadPath: '/mnt/user-data/files'
            });
            this._startScreencast();
            this._resizeObserver = new ResizeObserver(() => {
              if (this.connected && !this._disposed) this._startScreencast();
            });
            this._resizeObserver.observe(this.canvas);
            this._tabPollTimer = setInterval(() => this._checkTabSwitch(), 2000);
            finish(true);
          } catch {
            finish(false);
          }
        };
        ws.onmessage = event => {
          if (!this._disposed && this.ws === ws) this._onWsMessage(event);
        };
        ws.onclose = () => {
          if (this.ws === ws) this.connected = false;
          finish(false);
        };
        ws.onerror = () => {
          if (this.ws === ws) this.connected = false;
          finish(false);
        };
      });
    } catch {
      return false;
    } finally {
      if (this._connectController === controller) this._connectController = null;
    }
  }

  disconnect() {
    this._disposed = true;
    this._connectController?.abort();
    this._connectResolve?.(false);
    if (this._connectTimer) { clearTimeout(this._connectTimer); this._connectTimer = null; }
    if (this._tabPollTimer) { clearInterval(this._tabPollTimer); this._tabPollTimer = null; }
    this._tabController?.abort();
    this._tabController = null;
    if (this._resizeObserver) { this._resizeObserver.disconnect(); this._resizeObserver = null; }
    if (this._viewportFixTimer) { clearTimeout(this._viewportFixTimer); this._viewportFixTimer = null; }
    this._viewportFixPending = false;
    if (this._loadingHideTimer) { clearTimeout(this._loadingHideTimer); this._loadingHideTimer = null; }
    for (const timer of this._inputTimers) clearTimeout(timer);
    this._inputTimers.clear();
    this._img.onload = null;
    if (this.ws) {
      if (this.connected) {
        try { this._send('Page.stopScreencast', {}); } catch(e) {}
      }
      const ws = this.ws;
      this.ws = null;
      ws.close();
    }
    this.connected = false;
  }

  _onWsMessage(event) {
    const msg = JSON.parse(event.data);
    if (msg.method === 'Page.screencastFrame') {
      this._renderFrame(msg.params);
      this._send('Page.screencastFrameAck', { sessionId: msg.params.sessionId });
    } else if (msg.method === 'Page.frameNavigated') {
      if (!msg.params.frame.parentId) {
        this._navigating = true;
        this._showLoading(true);
        this._applyViewport();
      }
    } else if (msg.method === 'Page.loadEventFired') {
      this._navigating = false;
      if (this.connected) {
        this._applyViewport();
        this._send('Page.startScreencast', {
          format: 'jpeg', quality: 80,
          maxWidth: this._targetWidth, maxHeight: this._targetHeight,
          everyNthFrame: 1
        });
      }
    } else if (msg.method === 'Fetch.authRequired') {
      this._showAuthDialog(msg.params);
    } else if (msg.method === 'Fetch.requestPaused') {
      this._send('Fetch.continueRequest', { requestId: msg.params.requestId });
    }
  }

  async _checkTabSwitch() {
    if (this._disposed || !this.connected || this._tabController) return;
    const controller = new AbortController();
    this._tabController = controller;
    try {
      const resp = await ocuFetch(`${_browserBasePath}browser/${this.chatId}/json?_t=${Date.now()}`,
        { cache: 'no-store', signal: controller.signal });
      if (!resp.ok) return;
      const pages = await resp.json();
      if (this._disposed || !this.connected || !Array.isArray(pages)) return;
      const realPages = pages.filter(p => p.type === 'page' && !p.url.startsWith('chrome://'));
      if (realPages.length === 0) return;
      const target = realPages[realPages.length - 1];
      if (target.id !== this.pageId) this._reconnectToPage(target.id);
    } catch(e) {
    } finally {
      if (this._tabController === controller) this._tabController = null;
    }
  }

  _reconnectToPage(newPageId) {
    if (this._disposed) return;
    if (this.ws) {
      if (this.ws.readyState === WebSocket.OPEN) {
        try { this._send('Page.stopScreencast', {}); } catch(e) {}
      }
      this.ws.close();
    }
    this.connected = false;
    this.pageId = newPageId;
    const wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${wsProto}//${location.host}${_browserBasePath}browser/${this.chatId}/devtools/page/${this.pageId}`;
    const ws = new WebSocket(wsUrl);
    this.ws = ws;
    ws.onopen = () => {
      if (this._disposed || this.ws !== ws) { ws.close(); return; }
      this.connected = true;
      this._send('Page.enable', {});
      this._send('Fetch.enable', { handleAuthRequests: true });
      this._startScreencast();
    };
    ws.onmessage = event => {
      if (!this._disposed && this.ws === ws) this._onWsMessage(event);
    };
    ws.onclose = () => { if (this.ws === ws) this.connected = false; };
  }

  _applyViewport() {
    if (!this._targetWidth) return;
    this._send('Emulation.setDeviceMetricsOverride', {
      width: this._targetWidth, height: this._targetHeight,
      deviceScaleFactor: 1, mobile: false
    });
  }

  _startScreencast() {
    const rect = this.canvas.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    let w = Math.round(rect.width * dpr);
    let h = Math.round(rect.height * dpr);
    // Scale down proportionally to fit within limits (preserve aspect ratio
    // so object-fit:contain doesn't cause letterboxing → coordinate errors)
    const maxW = 1920, maxH = 1080;
    if (w > maxW || h > maxH) {
      const scale = Math.min(maxW / w, maxH / h);
      w = Math.round(w * scale);
      h = Math.round(h * scale);
    }
    if (w < 100 || h < 100) return;
    this._targetWidth = w;
    this._targetHeight = h;
    this._send('Emulation.setDeviceMetricsOverride', {
      width: w, height: h, deviceScaleFactor: 1, mobile: false
    });
    this._send('Page.startScreencast', {
      format: 'jpeg', quality: 80,
      maxWidth: w, maxHeight: h, everyNthFrame: 1
    });
  }

  _send(method, params) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify({ id: this.cmdId++, method, params }));
  }

  _renderFrame(params) {
    if (params.metadata) {
      const devW = params.metadata.deviceWidth;
      const devH = params.metadata.deviceHeight;
      this._deviceWidth = devW;
      this._deviceHeight = devH;
      if (this._targetWidth && !this._viewportFixPending &&
          (devW !== this._targetWidth || devH !== this._targetHeight)) {
        this._viewportFixPending = true;
        this._viewportFixTimer = setTimeout(() => {
          this._viewportFixTimer = null;
          this._viewportFixPending = false;
          if (this.connected && !this._disposed) this._startScreencast();
        }, 200);
      }
      if (!this._navigating && this._targetWidth &&
          devW === this._targetWidth && devH === this._targetHeight) {
        this._showLoading(false);
      }
    }
    const img = this._img;
    img.onload = () => {
      if (this._disposed) return;
      this.canvas.width = img.width;
      this.canvas.height = img.height;
      this.ctx.drawImage(img, 0, 0);
    };
    img.src = 'data:image/jpeg;base64,' + params.data;
  }

  _showLoading(show) {
    if (!this.canvas.parentElement) return;
    let overlay = this.canvas.parentElement.querySelector('.browser-loading');
    if (show && !overlay) {
      overlay = document.createElement('div');
      overlay.className = 'browser-loading';
      this.canvas.parentElement.appendChild(overlay);
      this._loadingShownAt = Date.now();
      if (this._loadingHideTimer) { clearTimeout(this._loadingHideTimer); this._loadingHideTimer = null; }
    } else if (!show && overlay) {
      const minTime = 500;
      const elapsed = Date.now() - (this._loadingShownAt || 0);
      if (elapsed >= minTime) {
        overlay.remove();
      } else {
        if (!this._loadingHideTimer) {
          this._loadingHideTimer = setTimeout(() => {
            this._loadingHideTimer = null;
            this.canvas.parentElement?.querySelector('.browser-loading')?.remove();
          }, minTime - elapsed);
        }
      }
    }
  }

  _showAuthDialog(params) {
    const container = this.canvas.parentElement;
    if (!container) return;
    // Remove existing auth dialog if any
    container.querySelector('.browser-auth-overlay')?.remove();

    const origin = params.authChallenge?.origin || params.request?.url || '';
    const overlay = document.createElement('div');
    overlay.className = 'browser-auth-overlay';
    Object.assign(overlay.style, {
      position: 'absolute', inset: '0', zIndex: '1000',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      background: 'rgba(0,0,0,0.5)', backdropFilter: 'blur(2px)'
    });

    const dialog = document.createElement('div');
    Object.assign(dialog.style, {
      background: '#fff', borderRadius: '12px', padding: '24px 28px',
      boxShadow: '0 8px 32px rgba(0,0,0,0.3)', minWidth: '320px',
      fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
      color: '#1a1a1a'
    });

    dialog.innerHTML = `
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:16px">
        <svg width="20" height="20" viewBox="0 0 20 20" fill="none" style="flex-shrink:0">
          <path d="M10 2a5 5 0 00-5 5v2H4a1 1 0 00-1 1v7a1 1 0 001 1h12a1 1 0 001-1v-7a1 1 0 00-1-1h-1V7a5 5 0 00-5-5zm-3 5a3 3 0 116 0v2H7V7z" fill="#555"/>
        </svg>
        <span style="font-size:15px;font-weight:600">${t('auth_required')}</span>
      </div>
      <div style="font-size:12px;color:#666;margin-bottom:16px;word-break:break-all">${origin}</div>
      <input type="text" placeholder="${t('username')}" class="auth-user"
        style="display:block;width:100%;box-sizing:border-box;padding:8px 12px;border:1px solid #ccc;border-radius:6px;font-size:14px;margin-bottom:8px;outline:none" />
      <input type="password" placeholder="${t('password')}" class="auth-pass"
        style="display:block;width:100%;box-sizing:border-box;padding:8px 12px;border:1px solid #ccc;border-radius:6px;font-size:14px;margin-bottom:16px;outline:none" />
      <div style="display:flex;gap:8px;justify-content:flex-end">
        <button class="auth-cancel"
          style="padding:8px 16px;border:1px solid #ccc;border-radius:6px;background:#fff;cursor:pointer;font-size:13px">${t('cancel')}</button>
        <button class="auth-submit"
          style="padding:8px 16px;border:none;border-radius:6px;background:#e52e2e;color:#fff;cursor:pointer;font-size:13px;font-weight:500">${t('login')}</button>
      </div>
    `;
    overlay.appendChild(dialog);
    container.appendChild(overlay);

    const userInput = dialog.querySelector('.auth-user');
    const passInput = dialog.querySelector('.auth-pass');
    userInput.focus();

    const submit = () => {
      this._send('Fetch.continueWithAuth', {
        requestId: params.requestId,
        authChallengeResponse: {
          response: 'ProvideCredentials',
          username: userInput.value,
          password: passInput.value
        }
      });
      overlay.remove();
    };

    const cancel = () => {
      this._send('Fetch.continueWithAuth', {
        requestId: params.requestId,
        authChallengeResponse: { response: 'CancelAuth' }
      });
      overlay.remove();
    };

    dialog.querySelector('.auth-submit').addEventListener('click', submit);
    dialog.querySelector('.auth-cancel').addEventListener('click', cancel);
    passInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
    userInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') passInput.focus(); });
    overlay.addEventListener('click', (e) => { if (e.target === overlay) cancel(); });
  }

  _getCoords(event) {
    const rect = this.canvas.getBoundingClientRect();
    const devW = this._deviceWidth || this.canvas.width;
    const devH = this._deviceHeight || this.canvas.height;
    // Account for object-fit:contain letterboxing/pillarboxing —
    // the bitmap may not fill the entire canvas element when aspect ratios differ
    // (e.g. during viewport resize transitions or DPR cap rounding)
    const displayAspect = rect.width / rect.height;
    const contentAspect = devW / devH;
    let contentW, contentH, offX, offY;
    if (contentAspect > displayAspect) {
      // Width-limited: black bars top/bottom
      contentW = rect.width;
      contentH = rect.width / contentAspect;
      offX = 0;
      offY = (rect.height - contentH) / 2;
    } else {
      // Height-limited: black bars left/right
      contentH = rect.height;
      contentW = rect.height * contentAspect;
      offX = (rect.width - contentW) / 2;
      offY = 0;
    }
    return {
      x: Math.round(((event.clientX - rect.left) - offX) * (devW / contentW)),
      y: Math.round(((event.clientY - rect.top) - offY) * (devH / contentH))
    };
  }

  _onMouse(type, event) {
    if (!this.connected) return;
    event.preventDefault();
    const coords = this._getCoords(event);
    const isClick = (type === 'mousePressed' || type === 'mouseReleased');
    this._send('Input.dispatchMouseEvent', {
      type, x: coords.x, y: coords.y,
      button: isClick ? 'left' : 'none',
      clickCount: isClick ? 1 : 0,
      modifiers: this._modifiers(event)
    });
  }

  _onKey(type, event) {
    if (!this.connected) return;
    event.preventDefault();
    const mods = this._modifiers(event);
    const vk = event.keyCode;
    if (type === 'keyDown' && event.key.length === 1) {
      this._send('Input.dispatchKeyEvent', {
        type: 'keyDown', key: event.key, code: event.code,
        text: '', windowsVirtualKeyCode: vk,
        nativeVirtualKeyCode: vk, modifiers: mods
      });
      this._send('Input.dispatchKeyEvent', {
        type: 'char', text: event.key,
        windowsVirtualKeyCode: vk,
        nativeVirtualKeyCode: vk, modifiers: mods
      });
    } else {
      this._send('Input.dispatchKeyEvent', {
        type, key: event.key, code: event.code,
        windowsVirtualKeyCode: vk,
        nativeVirtualKeyCode: vk, modifiers: mods
      });
    }
  }

  _modifiers(event) {
    return (event.altKey ? 1 : 0) | (event.ctrlKey ? 2 : 0) |
           (event.metaKey ? 4 : 0) | (event.shiftKey ? 8 : 0);
  }
}
