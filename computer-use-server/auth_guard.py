# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Fail-closed service authorization for HTTP, WebSocket and MCP.

REST and WebSocket handshakes accept only ``Authorization: Bearer`` with
``OCU_INTERNAL_TOKEN``. MCP accepts that secret only from
``X-OCU-Internal-Token`` and keeps ``Authorization: Bearer`` for
``MCP_API_KEY``. The two secrets do not substitute for each other.

Policy is read at request time. ``startup_preflight`` is the process-level
check the packaged command runs before uvicorn's multi-worker supervisor.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
import sys
from urllib.parse import parse_qs, unquote, urlsplit

from security import sanitize_chat_id

INTERNAL_HEADER = "x-ocu-internal-token"
_TRANSIENT_PREFIXES = ("temporary:", "local:", "channel:")

# Chat-bound route prefixes and exact identity routes are separate policies.
# Health, runtime-cli, docs and static stay outside both lists; the sandbox-peer
# deny rule still applies to every HTTP and WebSocket scope.
_GUARDED_PREFIXES = (
    "/api/uploads/",
    "/files/",
    "/api/outputs/",
    "/browser/",
    "/terminal/",
    "/preview/",
    "/internal/",
)
_IDENTITY_PATHS = frozenset(("/system-prompt", "/skill-list", "/skill-mounts"))


class AuthGuardError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _is_http_safe_credential(value: str) -> bool:
    """Accept visible ASCII only, valid in both Bearer and internal-header forms."""
    return bool(value) and all(0x21 <= ord(character) <= 0x7E for character in value)


def startup_preflight() -> int:
    """Return 0 when startup config is usable, else 1. Never serves traffic."""
    from docker_manager import validate_public_base_url

    if validate_public_base_url():
        return 1
    token = os.environ.get("OCU_INTERNAL_TOKEN", "")
    if not _is_http_safe_credential(token):
        print(
            "OCU_INTERNAL_TOKEN must be a non-empty HTTP-safe credential. "
            "Refusing to start.",
            file=sys.stderr,
        )
        return 1
    subnet = os.environ.get("OCU_SANDBOX_SUBNET", "").strip()
    if subnet:
        try:
            ipaddress.ip_network(subnet, strict=False)
        except ValueError:
            print(
                f"OCU_SANDBOX_SUBNET is not a network: {subnet!r}. Refusing to start.",
                file=sys.stderr,
            )
            return 1
    origin = os.environ.get("OCU_WEBUI_ORIGIN", "").strip()
    if origin and not _valid_origin(origin):
        print(
            f"OCU_WEBUI_ORIGIN is not an origin: {origin!r}. Refusing to start.",
            file=sys.stderr,
        )
        return 1
    from ws_recheck import validate_webui_auth_url

    if validate_webui_auth_url():
        return 1
    return 0


def _valid_origin(value: str) -> bool:
    parts = urlsplit(value)
    return parts.scheme in {"http", "https"} and bool(parts.netloc) and parts.path in {"", "/"} and not parts.query and not parts.fragment


def _token() -> str:
    return os.environ.get("OCU_INTERNAL_TOKEN", "")


def _network():
    raw = os.environ.get("OCU_SANDBOX_SUBNET", "").strip()
    if not raw:
        return None
    return ipaddress.ip_network(raw, strict=False)


def _origin() -> str:
    raw = os.environ.get("OCU_WEBUI_ORIGIN", "").strip()
    return raw[:-1] if raw.endswith("/") else raw


def _headers(scope) -> dict[str, str]:
    found: dict[str, str] = {}
    for key, value in scope.get("headers") or []:
        name = key.decode("latin-1").lower()
        if name not in found:
            found[name] = value.decode("latin-1")
    return found


def _peer_host(scope) -> str | None:
    client = scope.get("client")
    if not client:
        return None
    host = client[0]
    if host == "testclient":
        return None
    return host


def peer_denied(scope) -> bool:
    """True when the transport peer, not a forwarded header, is in the subnet."""
    network = _network()
    host = _peer_host(scope)
    if network is None or not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address in network


def _credential_matches(presented: str, secret: str) -> bool:
    """Compare raw HTTP field bytes without changing configured secret policy."""
    try:
        return hmac.compare_digest(
            presented.encode("latin-1"),
            secret.encode("latin-1"),
        )
    except UnicodeEncodeError:
        return False


def bearer_matches(headers: dict[str, str], secret: str) -> bool:
    presented = headers.get("authorization", "")
    if not presented.startswith("Bearer ") or not secret:
        return False
    return _credential_matches(presented[7:], secret)


def internal_header_matches(headers: dict[str, str]) -> bool:
    presented = headers.get(INTERNAL_HEADER, "")
    secret = _token()
    if not presented or not secret:
        return False
    return _credential_matches(presented, secret)


def canonical_chat_id(value: str) -> str:
    """Normalize, then reject empty, default and transient chat identifiers."""
    decoded = value or ""
    for _ in range(4):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    try:
        normalized = sanitize_chat_id(decoded.strip())
    except Exception as exc:
        status = getattr(exc, "status_code", 400)
        raise AuthGuardError(status, "Invalid chat_id") from exc
    if normalized == "default" or normalized.startswith(_TRANSIENT_PREFIXES):
        raise AuthGuardError(400, "Invalid chat_id")
    return normalized


def _chat_id_from_path(path: str) -> str | None:
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        return None
    if parts[0] in {"files", "browser", "terminal", "preview"}:
        return parts[1]
    if parts[0] == "internal" and len(parts) >= 3 and parts[1] in {"launch", "describe"}:
        return parts[2]
    if parts[0] == "api" and len(parts) >= 3 and parts[1] in {"uploads", "outputs"}:
        return parts[2]
    return None
def _header_chat_id(headers: dict[str, str]) -> str | None:
    if "x-chat-id" in headers:
        return headers["x-chat-id"]
    if "x-openwebui-chat-id" in headers:
        return headers["x-openwebui-chat-id"]
    return None


def _query_chat_id(scope) -> str | None:
    values = parse_qs(
        (scope.get("query_string") or b"").decode("latin-1"),
        keep_blank_values=True,
    )
    chat_ids = values.get("chat_id")
    return chat_ids[0] if chat_ids else None


def _is_identity_path(path: str) -> bool:
    return path in _IDENTITY_PATHS




def _guarded(path: str) -> bool:
    return _is_identity_path(path) or any(
        path.startswith(prefix) for prefix in _GUARDED_PREFIXES
    )


def authorize_http(scope) -> None:
    """Raise AuthGuardError before a protected handler or identity read runs."""
    if peer_denied(scope):
        raise AuthGuardError(403, "Forbidden")
    path = scope.get("path") or ""
    if not _guarded(path):
        return
    headers = _headers(scope)
    if not bearer_matches(headers, _token()):
        raise AuthGuardError(401, "Unauthorized")
    raw = _chat_id_from_path(path)
    if raw is None and _is_identity_path(path):
        raw = _header_chat_id(headers)
        if raw is None:
            raw = _query_chat_id(scope)
    if raw is not None:
        canonical_chat_id(raw)


def authorize_websocket(scope) -> None:
    authorize_http(scope)


def authorize_mcp(scope) -> str:
    """Require both independent service credentials before identity handling."""
    if peer_denied(scope):
        raise AuthGuardError(403, "Forbidden")
    headers = _headers(scope)
    if not internal_header_matches(headers):
        raise AuthGuardError(401, "Unauthorized")
    mcp_key = os.environ.get("MCP_API_KEY", "")
    if mcp_key and not bearer_matches(headers, mcp_key):
        raise AuthGuardError(401, "Unauthorized")
    raw = _header_chat_id(headers)
    if raw is None:
        raise AuthGuardError(400, "Invalid chat_id")
    return canonical_chat_id(raw)


def cors_allow_origin(request_origin: str | None) -> str | None:
    """Return the configured ASCII origin only. Missing config grants nothing."""
    allowed = _origin()
    if not allowed or not request_origin or not request_origin.isascii():
        return None
    return allowed if request_origin == allowed else None


_CORS_ALLOW_HEADERS = (
    b"Authorization, Content-Type, X-OCU-Internal-Token, X-Chat-Id, "
    b"X-OpenWebUI-Chat-Id, X-User-Email, X-OpenWebUI-User-Email"
)


class AuthGuardMiddleware:
    """Pure ASGI guard outside HTTP, WebSocket and mounted MCP handlers."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http":
            headers = _headers(scope)
            if _is_cors_preflight(scope, headers):
                if peer_denied(scope):
                    await _reject_http(send, 403, "Forbidden", scope)
                else:
                    await _preflight(send, headers)
                return
        try:
            if scope["type"] == "websocket":
                authorize_websocket(scope)
            elif scope.get("path") == "/mcp":
                scope["ocu_chat_id"] = authorize_mcp(scope)
            else:
                authorize_http(scope)
        except AuthGuardError as exc:
            if scope["type"] == "websocket":
                await _reject_websocket(send)
                return
            await _reject_http(send, exc.status, exc.detail, scope)
            return
        if scope["type"] == "http":
            await self.app(scope, receive, _cors_sender(scope, send))
            return
        await self.app(scope, receive, send)


def _is_cors_preflight(scope, headers: dict[str, str]) -> bool:
    return (
        scope.get("method") == "OPTIONS"
        and "origin" in headers
        and "access-control-request-method" in headers
    )


async def _preflight(send, headers: dict[str, str]) -> None:
    origin = cors_allow_origin(headers.get("origin"))
    if not origin:
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-length", b"0")],
            }
        )
        await send({"type": "http.response.body", "body": b""})
        return
    await send(
        {
            "type": "http.response.start",
            "status": 204,
            "headers": [
                (b"access-control-allow-origin", origin.encode("latin-1")),
                (b"access-control-allow-methods", b"GET, POST, OPTIONS"),
                (b"access-control-allow-headers", _CORS_ALLOW_HEADERS),
                (b"vary", b"Origin"),
                (b"content-length", b"0"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": b""})


def _cors_sender(scope, send):
    async def send_with_cors(message):
        if message["type"] == "http.response.start":
            origin = cors_allow_origin(_headers(scope).get("origin"))
            if origin:
                headers = list(message.get("headers") or [])
                headers.append((b"access-control-allow-origin", origin.encode("latin-1")))
                headers.append((b"vary", b"Origin"))
                message = {**message, "headers": headers}
        await send(message)

    return send_with_cors


async def _reject_http(send, status: int, detail: str, scope) -> None:
    body = f'{{"detail":"{detail}"}}'.encode("ascii")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if status == 401:
        headers.append((b"www-authenticate", b"Bearer"))
    origin = cors_allow_origin(_headers(scope).get("origin"))
    if origin:
        headers.append((b"access-control-allow-origin", origin.encode("latin-1")))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _reject_websocket(send) -> None:
    """Deny before accept. Starlette TestClient surfaces this as a failed handshake."""
    await send({"type": "websocket.close", "code": 1008})
