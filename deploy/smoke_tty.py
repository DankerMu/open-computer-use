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
import threading
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
    def __init__(self, sock: socket.socket, buffered: bytes = b""):
        self.sock = sock
        self._closed = threading.Event()
        self._dead = threading.Event()
        self._send_lock = threading.Lock()
        self._reader = threading.Thread(target=self._watch, args=(buffered,), daemon=True)
        self._reader.start()

    def check_alive(self) -> None:
        if self._closed.is_set() or self._dead.is_set():
            raise TtydProtocolError("ttyd websocket closed before terminal observation finished")

    def _watch(self, buffered: bytes) -> None:
        data = bytearray(buffered)
        try:
            while not self._closed.is_set():
                while len(data) >= 2:
                    opcode = data[0] & 0x0F
                    if data[0] & 0x70 or data[1] & 0x80:
                        raise TtydProtocolError("ttyd websocket server frame is malformed")
                    length = data[1] & 0x7F
                    header = 2
                    if length == 126:
                        if len(data) < 4:
                            break
                        length = int.from_bytes(data[2:4], "big")
                        header = 4
                    elif length == 127:
                        if len(data) < 10:
                            break
                        length = int.from_bytes(data[2:10], "big")
                        header = 10
                    if length > 1_048_576:
                        raise TtydProtocolError("ttyd websocket frame exceeds smoke limit")
                    if len(data) < header + length:
                        break
                    payload = bytes(data[header:header + length])
                    del data[:header + length]
                    if opcode == 0x8:
                        raise TtydProtocolError("ttyd websocket sent a close frame")
                    if opcode == 0x9:
                        with self._send_lock:
                            self.sock.sendall(_mask_frame(payload, 0xA))
                    elif opcode not in {0, 1, 2, 0xA}:
                        raise TtydProtocolError("ttyd websocket frame opcode is invalid")
                try:
                    chunk = self.sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    raise TtydProtocolError("ttyd websocket ended")
                data.extend(chunk)
        except (OSError, TtydProtocolError):
            if not self._closed.is_set():
                self._dead.set()

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        with self._send_lock:
            try:
                self.sock.settimeout(0.5)
                self.sock.sendall(_mask_frame(b"", 0x8))
            except OSError:
                pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        self._reader.join(timeout=0.5)


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
        if name.lower() in {"host", "connection", "upgrade", "sec-websocket-key", "sec-websocket-version", "sec-websocket-protocol"}:
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
        status, response_headers, rest = _read_headers(sock, deadline)
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
        sock.settimeout(0.25)
        sock.sendall(_mask_frame(init_payload(), 0x1))
        return TtydSession(sock, rest)
    except (OSError, TtydProtocolError, ValueError) as exc:
        try:
            sock.close()
        except OSError:
            pass
        if isinstance(exc, TtydProtocolError):
            raise
        raise TtydProtocolError(f"ttyd websocket transport failed ({type(exc).__name__})") from exc
