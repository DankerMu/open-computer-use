#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Loopback-only non-echoing auth/OCU fixtures for focused native proxy proof."""

from __future__ import annotations

import argparse
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
from urllib.parse import parse_qs, unquote, urlsplit

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class RecordingServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, record: Path):
        super().__init__(address, handler)
        self.record = record
        self.lock = threading.Lock()

    def observe(self, kind, handler, extra=None):
        entry = {"kind": kind, "method": handler.command, "target": handler.path,
                 "headers": {key.lower(): value for key, value in handler.headers.items()}}
        if extra:
            entry.update(extra)
        with self.lock:
            fd = os.open(self.record, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry) + "\n")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self._serve()

    def do_HEAD(self):
        self._serve()

    def do_POST(self):
        self._serve()

    def do_DELETE(self):
        self._serve()

    def _reply(self, status, body=b"ok", headers=None):
        self.send_response(status)
        for key, value in headers or ():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _serve(self):
        kind = self.server.kind
        extra = {}
        if kind == "ocu" and self.command == "POST":
            length = int(self.headers.get("Content-Length", "0"))
            payload = self.rfile.read(length) if length else b""
            extra["body_sha256"] = hashlib.sha256(payload).hexdigest()
            extra["body_length"] = len(payload)
        if kind == "auth":
            extra["auth_content_length"] = self.headers.get("Content-Length")
            extra["auth_transfer_encoding"] = self.headers.get("Transfer-Encoding")
            length_header = self.headers.get("Content-Length")
            framed = bool(self.headers.get("Transfer-Encoding"))
            if length_header not in {None, "0"}:
                framed = True
                leftover = self.rfile.read(int(length_header))
                extra["auth_body_length"] = len(leftover)
            extra["auth_frame"] = "body" if framed else "bodyless"
        self.server.observe(kind, self, extra)
        if kind == "auth":
            cookie = self.headers.get("Cookie", "")
            chat = self.headers.get("X-Chat-Id", "")
            if extra.get("auth_frame") == "body":
                self._reply(400, b"auth frame must be bodyless")
                return
            if self.path == "/api/v1/ocu/auth":
                if cookie == "session=owner" and chat == "chat-ABC-123":
                    self._reply(200, b"authenticated",
                                [("X-User-Id", "trusted-user"),
                                 ("X-User-Email", "owner%2Bqa%40example.test")])
                elif cookie in {"session=owner", "session=foreign"}:
                    self._reply(403)
                elif cookie == "session=error":
                    self._reply(503)
                else:
                    self._reply(401)
            elif self.path == "/api/v1/auths/":
                if cookie in {"session=owner", "session=foreign"}:
                    self._reply(200, b"authenticated")
                elif cookie == "session=error":
                    self._reply(503)
                else:
                    self._reply(401)
            else:
                self._reply(200, b"webui")
            return
        if self.headers.get("Upgrade", "").lower() == "websocket":
            key = self.headers.get("Sec-WebSocket-Key", "")
            digest = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", digest)
            self.end_headers()
            self.wfile.write(b"\x81\x02ok")
            self.wfile.flush()
            while True:
                frame = self.rfile.read(2)
                if len(frame) != 2:
                    return
                opcode, size = frame
                if not size & 0x80 or size & 0x7F > 125:
                    return
                mask = self.rfile.read(4)
                payload = self.rfile.read(size & 0x7F)
                decoded = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
                if opcode & 0x0F == 9:
                    self.wfile.write(bytes((0x8A, len(decoded))) + decoded)
                    self.wfile.flush()
                else:
                    return
        path = unquote(urlsplit(self.path).path)
        if path.endswith("/backend-html403.html"):
            self._reply(403, b"upstream error",
                        [("Content-Type", "TEXT/HTML; charset=utf-8"),
                         ("Content-Security-Policy", "default-src 'self'"),
                         ("X-Content-Type-Options", "other")])
            return
        if path.endswith("/backend403"):
            self._reply(403, b"backend 403")
            return
        if path.endswith("/backend409"):
            self._reply(409, b"backend 409")
            return
        if path.startswith("/files/"):
            mime = "application/octet-stream"
            if path.endswith(".html"):
                mime = "TeXt/HTmL; charset=utf-8"
            elif path.endswith(".svg"):
                mime = "IMAGE/SVG+XML; charset=utf-8"
            elif path.endswith(".xhtml"):
                mime = "application/xhtml+xml; charset=UTF-8"
            elif path.endswith(".xml"):
                mime = "application/xml; charset=UTF-8"
            download = parse_qs(urlsplit(self.path).query, keep_blank_values=True).get("download", ["0"])[-1] == "1"
            disposition = "attachment; filename=report.html" if download else "inline; filename=report.html"
            headers = [("Content-Type", mime), ("Content-Disposition", disposition),
                       ("Content-Security-Policy", "default-src 'self'"),
                       ("X-Content-Type-Options", "other")]
            self._reply(200, b"file", headers)
            return
        self._reply(200, b"ocu")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--auth-port", type=int, required=True)
    parser.add_argument("--ocu-port", type=int, required=True)
    parser.add_argument("--record", type=Path, required=True)
    args = parser.parse_args()
    args.record.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.record.exists() and args.record.stat().st_mode & 0o077:
        raise SystemExit("record path must be private")
    servers = []
    for kind, port in (("auth", args.auth_port), ("ocu", args.ocu_port)):
        server = RecordingServer(("127.0.0.1", port), Handler, args.record)
        server.kind = kind
        servers.append(server)
    threading.Thread(target=servers[1].serve_forever, daemon=True).start()
    print("recording fixtures ready", flush=True)
    try:
        servers[0].serve_forever()
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
