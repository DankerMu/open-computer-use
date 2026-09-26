# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Bounded stdlib ttyd WebSocket client for overlay smoke."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import time
from urllib.parse import urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
INIT_COLUMNS = 80
INIT_ROWS = 24
TTY_SUBPROTOCOL = "tty"


class TtydProtocolError(Exception):
    """Handshake or ttyd initialization failed."""


def _deadline_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TtydProtocolError("ttyd websocket deadline exceeded")
    return remaining


def _mask_frame(payload: bytes, opcode: int) -> bytes:
    if len(payload) > 125:
        raise TtydProtocolError("ttyd websocket frame too large")
    mask = os.urandom(4)
    header = bytes((0x80 | opcode, 0x80 | len(payload))) + mask
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return header + masked


def _recv_exact(sock: socket.socket, size: int, deadline: float) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        sock.settimeout(_deadline_timeout(deadline))
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise TtydProtocolError("ttyd websocket closed during handshake")
        chunks.extend(chunk)
    return bytes(chunks)


def _read_headers(sock: socket.socket, deadline: float) -> tuple[str, dict[str, str], bytes]:
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        sock.settimeout(_deadline_timeout(deadline))
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > 65536:
            raise TtydProtocolError("ttyd websocket handshake too large")
    if b"\r\n\r\n" not in buf:
        raise TtydProtocolError("ttyd websocket handshake truncated")
    head, rest = bytes(buf).split(b"\r\n\r\n", 1)
    lines = head.split(b"\r\n")
    status = lines[0].decode("latin1", "replace")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if b":" not in line:
            continue
        name, value = line.split(b":", 1)
        headers[name.decode("latin1").lower()] = value.strip().decode("latin1")
    return status, headers, rest


def expected_accept(key: str) -> str:
    digest = hashlib.sha1((key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def init_payload(columns: int = INIT_COLUMNS, rows: int = INIT_ROWS) -> bytes:
    return json.dumps(
        {"authToken": "", "columns": columns, "rows": rows},
        separators=(",", ":"),
    ).encode("utf-8")


class TtydSession:
    def __init__(self, sock: socket.socket):
        self.sock = sock

    def close(self) -> None:
        sock = self.sock
        self.sock = None
        if sock is None:
            return
        try:
            sock.settimeout(2)
            sock.sendall(_mask_frame(b"", 0x8))
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


def connect_ttyd(
    origin: str,
    path: str,
    headers: dict[str, str],
    *,
    timeout: float = 10.0,
) -> TtydSession:
    parts = urlsplit(origin)
    if parts.scheme not in {"http", "https"}:
        raise TtydProtocolError("ttyd origin must be http or https")
    host = parts.hostname
    if not host:
        raise TtydProtocolError("ttyd origin host is missing")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    header_lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host}:{port}",
        "Connection: Upgrade",
        "Upgrade: websocket",
        "Sec-WebSocket-Version: 13",
        f"Sec-WebSocket-Key: {key}",
        f"Sec-WebSocket-Protocol: {TTY_SUBPROTOCOL}",
    ]
    for name, value in headers.items():
        if name.lower() in {"host", "connection", "upgrade", "sec-websocket-key", "sec-websocket-version"}:
            continue
        header_lines.append(f"{name}: {value}")
    request = ("\r\n".join(header_lines) + "\r\n\r\n").encode("latin1")
    deadline = time.monotonic() + timeout
    raw = socket.create_connection((host, port), timeout=_deadline_timeout(deadline))
    sock: socket.socket = raw
    try:
        if parts.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        sock.settimeout(_deadline_timeout(deadline))
        sock.sendall(request)
        status, response_headers, _rest = _read_headers(sock, deadline)
        try:
            code = int(status.split()[1])
        except (IndexError, ValueError) as exc:
            raise TtydProtocolError("ttyd websocket status is malformed") from exc
        if code != 101:
            raise TtydProtocolError(f"ttyd websocket upgrade refused ({code})")
        if response_headers.get("upgrade", "").lower() != "websocket":
            raise TtydProtocolError("ttyd websocket upgrade header missing")
        if response_headers.get("sec-websocket-accept") != expected_accept(key):
            raise TtydProtocolError("ttyd websocket accept mismatch")
        selected = {
            item.strip()
            for item in response_headers.get("sec-websocket-protocol", "").split(",")
            if item.strip()
        }
        if TTY_SUBPROTOCOL not in selected:
            raise TtydProtocolError("ttyd websocket subprotocol tty was not selected")
        sock.sendall(_mask_frame(init_payload(), 0x1))
        return TtydSession(sock)
    except Exception:
        try:
            sock.close()
        except OSError:
            pass
        raise
