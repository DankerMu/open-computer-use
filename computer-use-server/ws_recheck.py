# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Session-bound WebSocket authorization re-check for CDP and ttyd relays.

Captures the handshake cookie and canonical chat id, performs a bounded
owner check before backend access, then rechecks the same pairing every
30s. Any non-200, timeout, or transport failure revokes the connection
with close 4401 and stops further forwarding.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

import aiohttp
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from auth_guard import canonical_chat_id

AUTH_PATH = "/api/v1/ocu/auth"
RECHECK_INTERVAL_SECONDS = 30.0
AUTH_TIMEOUT_SECONDS = 5.0
BACKEND_CLOSE_TIMEOUT_SECONDS = 5.0
FRONTEND_CLOSE_TIMEOUT_SECONDS = 5.0
LOOKUP_TIMEOUT_SECONDS = 5.0  # handshake address lookup; to_thread worker is not killable
REVOKE_CLOSE_CODE = 4401
PREACCEPT_CLOSE_CODE = 1008
BACKEND_FAIL_CLOSE_CODE = 1011
INTERNAL_HEADER = "X-OCU-Internal-Token"
CHAT_HEADER = "X-Chat-Id"


def _now() -> float:
    return time.monotonic()


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)



def configured_auth_url() -> str:
    return os.environ.get("OCU_WEBUI_AUTH_URL", "")


def validate_webui_auth_url(raw: str | None = None) -> int:
    """Return 0 when the configured auth URL is usable or intentionally absent."""
    value = os.environ.get("OCU_WEBUI_AUTH_URL", "") if raw is None else raw
    if value == "":
        return 0
    if not _is_exact_auth_url(value):
        print(
            "OCU_WEBUI_AUTH_URL is not a usable WebUI auth URL. Refusing to start.",
            file=sys.stderr,
        )
        return 1
    return 0


def _is_exact_auth_url(value: str) -> bool:
    if value.strip() != value:
        return False
    if any(ord(character) < 0x21 or ord(character) > 0x7E for character in value):
        return False
    if any(marker in value for marker in ("@", "?", "#", " ")):
        return False
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return False
    if parts.scheme not in {"http", "https"}:
        return False
    if not parts.netloc or "@" in parts.netloc:
        return False
    if parts.path != AUTH_PATH:
        return False
    if parts.query or parts.fragment or parts.username or parts.password:
        return False
    if not host:
        return False
    if port is not None and not (1 <= port <= 65535):
        return False
    return True


def handshake_cookie(websocket: WebSocket) -> str | None:
    headers = websocket.headers
    cookie_header = headers.get("cookie")
    if not cookie_header or not cookie_header.strip():
        return None
    parsed = SimpleCookie()
    try:
        parsed.load(cookie_header)
    except Exception:
        return None
    morsel = parsed.get("token")
    if morsel is None:
        return None
    if not morsel.value:
        return None
    return cookie_header


def _internal_token() -> str:
    return os.environ.get("OCU_INTERNAL_TOKEN", "")


class RelaySession:
    def __init__(self, websocket: WebSocket, chat_id: str, cookie: str):
        self.websocket = websocket
        self.chat_id = chat_id
        self.cookie = cookie
        self.revoked = False
        self._stop = asyncio.Event()
        self._outcome: str | None = None
        self._recheck_task: asyncio.Task | None = None
        self._connect_task: asyncio.Task | None = None
        self._pump_tasks: list[asyncio.Task] = []
        self._auth_session: aiohttp.ClientSession | None = None
        self._backend_ws = None
        self._backend_session: aiohttp.ClientSession | None = None

    def request_stop(self, outcome: str) -> None:
        if self._outcome is None or (outcome == "revoked" and self._outcome != "revoked"):
            self._outcome = outcome
        if outcome == "revoked":
            self.revoked = True
        self._stop.set()

    async def authorize_once(self) -> bool:
        url = configured_auth_url()
        if not url or not _is_exact_auth_url(url):
            return False
        session = await self._auth_client()
        headers = {
            "Cookie": self.cookie,
            CHAT_HEADER: self.chat_id,
            INTERNAL_HEADER: _internal_token(),
        }
        try:
            async with session.get(
                url,
                headers=headers,
                allow_redirects=False,
            ) as response:
                status = response.status
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        return status == 200

    async def _auth_client(self) -> aiohttp.ClientSession:
        if self._auth_session is None:
            self._auth_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=AUTH_TIMEOUT_SECONDS),
                trust_env=False,
                cookie_jar=aiohttp.DummyCookieJar(),
            )
        return self._auth_session

    async def _recheck_loop(self, started: float) -> None:
        try:
            while not self._stop.is_set():
                remaining = RECHECK_INTERVAL_SECONDS - (_now() - started)
                if remaining > 0:
                    try:
                        await _sleep(remaining)
                    except asyncio.CancelledError:
                        raise
                    if self._stop.is_set():
                        return
                if self._stop.is_set():
                    return
                started = _now()
                allowed = await self.authorize_once()
                if not allowed:
                    self.revoked = True
                    self.request_stop("revoked")
                    return
        except asyncio.CancelledError:
            raise

    async def _close_frontend(self, code: int, reason: str = "") -> None:
        websocket = self.websocket
        if websocket.client_state == WebSocketState.DISCONNECTED:
            return
        try:
            await asyncio.wait_for(
                websocket.close(code=code, reason=reason),
                timeout=FRONTEND_CLOSE_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _close_backend(self) -> None:
        backend = self._backend_ws
        if backend is None:
            return
        try:
            await asyncio.wait_for(
                backend.close(),
                timeout=BACKEND_CLOSE_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _cancel_tasks(self, tasks: list[asyncio.Task]) -> None:
        pending = [task for task in tasks if task is not None and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _aclose_session(self, session) -> None:
        if session is None:
            return
        try:
            await asyncio.wait_for(
                session.close(),
                timeout=BACKEND_CLOSE_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _aclose_auth(self) -> None:
        session = self._auth_session
        self._auth_session = None
        await self._aclose_session(session)

    async def _aclose_backend_session(self) -> None:
        session = self._backend_session
        self._backend_session = None
        await self._aclose_session(session)

    async def _cleanup_backend(self) -> None:
        await self._close_backend()
        await self._aclose_backend_session()


    async def run(
        self,
        *,
        backend_url: str,
        accept_kwargs: dict | None = None,
        connect_kwargs: dict | None = None,
        client_to_backend,
        backend_to_client,
        lookup,
    ) -> None:
        cancelled = False
        outcome = "normal"
        lookup_task = None
        try:
            allowed = await self.authorize_once()
            if not allowed:
                await self._close_frontend(PREACCEPT_CLOSE_CODE)
                return
            started = _now()
            self._recheck_task = asyncio.create_task(self._recheck_loop(started))
            lookup_task = asyncio.create_task(asyncio.to_thread(lookup))
            done, _pending = await asyncio.wait(
                [lookup_task, self._recheck_task],
                return_when=asyncio.FIRST_COMPLETED,
                timeout=LOOKUP_TIMEOUT_SECONDS,
            )
            if self.revoked:
                await self._close_frontend(REVOKE_CLOSE_CODE)
                await self._cancel_tasks([self._recheck_task])
                return
            if lookup_task not in done:
                await self._close_frontend(
                    BACKEND_FAIL_CLOSE_CODE, "Backend connection failed"
                )
                await self._cancel_tasks([self._recheck_task])
                return
            try:
                container_addr = lookup_task.result()
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._cancel_tasks([self._recheck_task])
                await self._close_frontend(
                    BACKEND_FAIL_CLOSE_CODE, "Backend connection failed"
                )
                return
            if not container_addr:
                self.request_stop("denied")
                await self._cancel_tasks([self._recheck_task])
                await self._close_frontend(PREACCEPT_CLOSE_CODE)
                return
            if self.revoked:
                await self._cancel_tasks([self._recheck_task])
                await self._close_frontend(REVOKE_CLOSE_CODE)
                return
            await self.websocket.accept(**(accept_kwargs or {}))

            async def connect():
                self._backend_session = aiohttp.ClientSession()
                self._backend_ws = await self._backend_session.ws_connect(
                    backend_url.format(addr=container_addr),
                    **(connect_kwargs or {}),
                )
                return self._backend_ws

            self._connect_task = asyncio.create_task(connect())
            waiters = [self._connect_task, self._recheck_task]
            done, _pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if self.revoked:
                await self._cancel_tasks([self._connect_task, self._recheck_task])
                await self._close_frontend(REVOKE_CLOSE_CODE)
                return
            try:
                backend_ws = self._connect_task.result()
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._cancel_tasks([self._recheck_task])
                await self._close_frontend(BACKEND_FAIL_CLOSE_CODE, "Backend connection failed")
                await self._aclose_backend_session()
                return

            client_task = asyncio.create_task(client_to_backend(self, backend_ws))
            backend_task = asyncio.create_task(backend_to_client(self, backend_ws))
            self._pump_tasks = [client_task, backend_task]
            waiters = [client_task, backend_task, self._recheck_task]
            done, _pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if self.revoked:
                outcome = "revoked"
            elif backend_task in done:
                outcome = "normal"
            elif client_task in done:
                outcome = "client"
            self.request_stop("revoked" if outcome == "revoked" else "normal")
            await self._cancel_tasks([client_task, backend_task, self._recheck_task])
            if outcome == "revoked":
                await self._close_frontend(REVOKE_CLOSE_CODE)
            elif outcome == "normal":
                await self._close_frontend(1000)
        except asyncio.CancelledError:
            cancelled = True
            self.request_stop("cancelled")
            raise
        except Exception:
            if self.revoked:
                await self._close_frontend(REVOKE_CLOSE_CODE)
            else:
                await self._close_frontend(BACKEND_FAIL_CLOSE_CODE, "Backend connection failed")
        finally:
            task = asyncio.current_task()
            if cancelled and task is not None:
                while task.cancelling():
                    task.uncancel()
            await self._cancel_tasks(
                [lookup_task, self._connect_task, self._recheck_task, *self._pump_tasks]
            )
            await self._cleanup_backend()
            await self._aclose_auth()
            if cancelled:
                raise asyncio.CancelledError


async def admit(websocket: WebSocket, chat_id: str) -> RelaySession | None:
    """Capture credentials and deny before backend lookup when admission fails."""
    try:
        canonical = canonical_chat_id(chat_id)
    except Exception:
        await _preaccept_close(websocket)
        return None
    cookie = handshake_cookie(websocket)
    if cookie is None or not configured_auth_url():
        await _preaccept_close(websocket)
        return None
    return RelaySession(websocket, canonical, cookie)


async def _preaccept_close(websocket: WebSocket) -> None:
    try:
        await websocket.close(code=PREACCEPT_CLOSE_CODE)
    except Exception:
        pass


def _still_open(session: RelaySession) -> bool:
    return not session.revoked and not session._stop.is_set()


async def forward_cdp_client(session: RelaySession, backend_ws) -> None:
    websocket = session.websocket
    try:
        while _still_open(session):
            data = await websocket.receive_text()
            if not _still_open(session):
                return
            await backend_ws.send_str(data)
    except WebSocketDisconnect:
        session.request_stop("normal")
    except asyncio.CancelledError:
        raise
    except Exception:
        session.request_stop("normal")


async def forward_cdp_backend(session: RelaySession, backend_ws) -> None:
    websocket = session.websocket
    try:
        async for msg in backend_ws:
            if not _still_open(session):
                return
            if msg.type == aiohttp.WSMsgType.TEXT:
                await websocket.send_text(msg.data)
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break
        session.request_stop("normal")
    except asyncio.CancelledError:
        raise
    except Exception:
        session.request_stop("normal")


async def forward_ttyd_client(session: RelaySession, backend_ws) -> None:
    websocket = session.websocket
    try:
        while _still_open(session):
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                session.request_stop("normal")
                return
            if not _still_open(session):
                return
            if msg.get("bytes"):
                await backend_ws.send_bytes(msg["bytes"])
            elif msg.get("text"):
                await backend_ws.send_str(msg["text"])
    except WebSocketDisconnect:
        session.request_stop("normal")
    except asyncio.CancelledError:
        raise
    except Exception:
        session.request_stop("normal")


async def forward_ttyd_backend(session: RelaySession, backend_ws) -> None:
    websocket = session.websocket
    try:
        async for msg in backend_ws:
            if not _still_open(session):
                return
            if msg.type == aiohttp.WSMsgType.BINARY:
                await websocket.send_bytes(msg.data)
            elif msg.type == aiohttp.WSMsgType.TEXT:
                await websocket.send_text(msg.data)
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break
        session.request_stop("normal")
    except asyncio.CancelledError:
        raise
    except Exception:
        session.request_stop("normal")
