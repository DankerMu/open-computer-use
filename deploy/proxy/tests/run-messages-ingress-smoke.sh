#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
#
# Native smoke for the shipped sandbox Messages ingress configuration. It
# copies messages-ingress.conf, points its one upstream URL and its one listen
# directive at loopback, starts a recording stub upstream and nginx from PATH,
# drives requests and removes everything it started. Does not invoke Docker.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
CONF_SOURCE="$ROOT/deploy/proxy/messages-ingress.conf"
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/ocu-messages-ingress.XXXXXX")"
UPSTREAM_TOKEN='http://open-webui:8080'
LISTEN_TOKEN='listen 8080;'

cleanup() {
    if [[ -n "${NGINX_PID:-}" ]] && kill -0 "$NGINX_PID" 2>/dev/null; then
        kill -TERM "$NGINX_PID" 2>/dev/null || true
        wait "$NGINX_PID" 2>/dev/null || true
    fi
    if [[ -n "${STUB_PID:-}" ]] && kill -0 "$STUB_PID" 2>/dev/null; then
        kill -TERM "$STUB_PID" 2>/dev/null || true
        wait "$STUB_PID" 2>/dev/null || true
    fi
    rm -rf "$WORKDIR"
}
trap cleanup EXIT

if ! command -v nginx >/dev/null; then
    printf 'nginx is not on PATH\n' >&2
    exit 2
fi
if [[ ! -f "$CONF_SOURCE" ]]; then
    printf 'FAIL shipped config is missing: %s\n' "$CONF_SOURCE"
    exit 1
fi

umask 077
mkdir -p "$WORKDIR/prefix" "$WORKDIR/record"
read -r STUB_PORT LISTEN_PORT < <(python3 - <<'PY'
import socket
sockets = [socket.socket() for _ in range(2)]
for sock in sockets:
    sock.bind(("127.0.0.1", 0))
print(*(sock.getsockname()[1] for sock in sockets))
for sock in sockets:
    sock.close()
PY
)

python3 - "$CONF_SOURCE" "$WORKDIR/messages-ingress.conf" "$UPSTREAM_TOKEN" \
    "http://127.0.0.1:${STUB_PORT}" "$LISTEN_TOKEN" "listen 127.0.0.1:${LISTEN_PORT};" <<'PY'
import sys
source, destination, upstream, upstream_new, listen, listen_new = sys.argv[1:]
text = open(source, encoding="utf-8").read()
for token in (upstream, listen):
    count = text.count(token)
    if count != 1:
        print(f"FAIL {token!r} occurs {count} times in the shipped config, expected 1")
        raise SystemExit(1)
text = text.replace(upstream, upstream_new).replace(listen, listen_new)
open(destination, "w", encoding="utf-8").write(text)
print("PASS upstream URL and listen directive each occur exactly once")
PY

cat >"$WORKDIR/stub.py" <<'PY'
import hashlib
import http.server
import json
import os
import sys
import time

record, release = sys.argv[2], sys.argv[3]


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A003
        del format, args

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except ConnectionError:
            self.close_connection = True

    def _record(self, body):
        row = {
            "method": self.command,
            "uri": self.path,
            "headers": [[name.lower(), value] for name, value in self.headers.items()],
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "body_length": len(body),
        }
        with open(record, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")

    def _serve(self):
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length) if length else b""
        self._record(body)
        if self.path.startswith("/api/v1/messages?stream=sse"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(b"event: first\ndata: first-chunk\n\n")
            self.wfile.flush()
            deadline = time.monotonic() + 10
            while not os.path.exists(release) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.wfile.write(b"event: last\ndata: last-chunk\n\n")
            self.wfile.flush()
            return
        payload = b'{"stub":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _serve


http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
PY

cat >"$WORKDIR/client.py" <<'PY'
import hashlib
import json
import os
import socket
import sys
import time

port, record, release, access_log = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
LIMIT = 32 * 1024 * 1024
failures = 0

ALLOWED = {
    "authorization": "Bearer canary-auth-token",
    "x-api-key": "canary-api-key",
    "content-type": "application/json",
    "accept": "application/json",
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "canary-beta-2026",
    "user-agent": "claude-cli/2.1.112 (canary)",
}
FORBIDDEN = {
    "cookie": "session=canary-cookie",
    "x-forwarded-for": "10.9.9.9",
    "x-user-id": "canary-user",
    "x-chat-id": "canary-chat",
    "x-stainless-lang": "js",
}
TRANSPORT = {"host", "connection", "content-length"}


def check(ok, name):
    global failures
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        failures += 1


def rows():
    if not os.path.exists(record):
        return []
    with open(record, encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def raw(method, target, headers=None, body=b"", chunked=False):
    sock = socket.create_connection(("127.0.0.1", port), timeout=15)
    lines = [f"{method} {target} HTTP/1.1", f"Host: 127.0.0.1:{port}", "Connection: close"]
    for name, value in (headers or {}).items():
        lines.append(f"{name}: {value}")
    if chunked:
        lines.append("Transfer-Encoding: chunked")
    elif body or method == "POST":
        lines.append(f"Content-Length: {len(body)}")
    try:
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        step = 1024 * 1024
        for offset in range(0, len(body), step):
            piece = body[offset:offset + step]
            if chunked:
                sock.sendall(b"%x\r\n" % len(piece) + piece + b"\r\n")
            else:
                sock.sendall(piece)
        if chunked:
            sock.sendall(b"0\r\n\r\n")
    except (BrokenPipeError, ConnectionResetError):
        pass
    data = b""
    try:
        while True:
            piece = sock.recv(65536)
            if not piece:
                break
            data += piece
    except (ConnectionResetError, socket.timeout):
        pass
    sock.close()
    head = data.split(b"\r\n", 1)[0].decode("latin-1")
    parts = head.split(" ")
    return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0, data


headers = {**{name.title(): value for name, value in ALLOWED.items()},
           **{name.title(): value for name, value in FORBIDDEN.items()}}
for target in ("/api/v1/messages", "/api/v1/messages/count_tokens?beta=true"):
    before = len(rows())
    body = json.dumps({"model": "canary-model", "target": target}).encode()
    status, data = raw("POST", target, headers, body)
    after = rows()
    check(status == 200 and b'{"stub":"ok"}' in data, f"POST {target} is forwarded")
    if len(after) != before + 1:
        check(False, f"POST {target} reached the stub exactly once")
        continue
    seen = after[-1]
    check(seen["method"] == "POST" and seen["uri"] == target, f"POST {target} keeps method and URI")
    check(seen["body_sha256"] == hashlib.sha256(body).hexdigest(), f"POST {target} keeps the body")
    received = {}
    for name, value in seen["headers"]:
        received.setdefault(name, []).append(value)
    for name, value in ALLOWED.items():
        check(received.get(name) == [value], f"{target}: {name} arrives unchanged")
    for name in FORBIDDEN:
        check(name not in received, f"{target}: {name} is not forwarded")
    extra = set(received) - set(ALLOWED) - TRANSPORT
    check(not extra, f"{target}: no other header is forwarded ({sorted(extra)})")

before = len(rows())
sock = socket.create_connection(("127.0.0.1", port), timeout=15)
body = b'{"stream":true}'
sock.sendall((
    "POST /api/v1/messages?stream=sse HTTP/1.1\r\n"
    f"Host: 127.0.0.1:{port}\r\nConnection: close\r\n"
    "Content-Type: application/json\r\nAccept: text/event-stream\r\n"
    f"Content-Length: {len(body)}\r\n\r\n"
).encode() + body)
received = b""
while b"first-chunk" not in received:
    piece = sock.recv(65536)
    if not piece:
        break
    received += piece
first_before_last = b"first-chunk" in received and b"last-chunk" not in received
open(release, "w").close()
while True:
    piece = sock.recv(65536)
    if not piece:
        break
    received += piece
sock.close()
check(first_before_last, "SSE first chunk reaches the client before the last chunk is written")
check(b"last-chunk" in received, "SSE stream completes")
check(len(rows()) == before + 1, "SSE request reached the stub once")

before = len(rows())
refused = (
    ("GET", "/api/v1/messages"),
    ("POST", "/api/v1/messages/batches"),
    ("POST", "/api/v1/chats/"),
    ("GET", "/"),
    ("GET", "/ocu/x"),
    ("POST", "/api/v1/auths/signin"),
    ("POST", "/api/v1/messages/../chats/"),
    ("POST", "//api/v1/messages"),
    ("POST", "/api/v1/%6Dessages/../chats"),
)
for method, target in refused:
    status, _ = raw(method, target, {"Content-Type": "application/json"},
                    b"{}" if method == "POST" else b"")
    check(status == 404, f"{method} {target} answers 404 locally (got {status})")
check(len(rows()) == before, "refused requests never reach the stub")

before = len(rows())
oversize = b"x" * (LIMIT + 1024 * 1024)
status, _ = raw("POST", "/api/v1/messages", {"Content-Type": "application/json"}, oversize)
check(status == 413, f"33 MiB declared body answers 413 (got {status})")
status, _ = raw("POST", "/api/v1/messages", {"Content-Type": "application/json"}, oversize, chunked=True)
check(status == 413, f"33 MiB chunked body answers 413 (got {status})")
check(len(rows()) == before, "oversize bodies never reach the stub")

before = len(rows())
status, data = raw("GET", "/healthz")
check(status == 200, f"GET /healthz answers 200 (got {status})")
check(len(rows()) == before, "healthz never reaches the stub")

time.sleep(0.2)
log = open(access_log, encoding="utf-8", errors="replace").read() if os.path.exists(access_log) else ""
check("/healthz" in log, "access log records requests")
canaries = list(ALLOWED.values()) + list(FORBIDDEN.values())
check(not any(value in log for value in canaries), "access log holds no request header values")
raise SystemExit(1 if failures else 0)
PY

python3 "$WORKDIR/stub.py" "$STUB_PORT" "$WORKDIR/record/requests.jsonl" "$WORKDIR/release" &
STUB_PID=$!

if ! nginx -t -q -e stderr -p "$WORKDIR/prefix/" -c "$WORKDIR/messages-ingress.conf"; then
    printf 'FAIL shipped config does not pass nginx -t\n'
    exit 1
fi
printf 'PASS shipped config passes nginx -t\n'
nginx -e stderr -p "$WORKDIR/prefix/" -c "$WORKDIR/messages-ingress.conf" \
    >"$WORKDIR/access.log" 2>"$WORKDIR/error.log" &
NGINX_PID=$!

ready=0
for _ in $(seq 1 50); do
    if python3 -c "import socket; s=socket.create_connection(('127.0.0.1', $LISTEN_PORT), 0.2); s.close()" 2>/dev/null \
        && python3 -c "import socket; s=socket.create_connection(('127.0.0.1', $STUB_PORT), 0.2); s.close()" 2>/dev/null; then
        ready=1
        break
    fi
    sleep 0.1
done
if [[ "$ready" -ne 1 ]]; then
    printf 'FAIL nginx or stub did not listen\n'
    cat "$WORKDIR/error.log" >&2 || true
    exit 1
fi

set +e
python3 "$WORKDIR/client.py" "$LISTEN_PORT" "$WORKDIR/record/requests.jsonl" "$WORKDIR/release" "$WORKDIR/access.log"
status=$?
set -e
if [[ "$status" -ne 0 ]]; then
    printf 'FAILED (nginx error log follows)\n'
    cat "$WORKDIR/error.log" >&2 || true
    exit 1
fi
printf 'PASSED\n'
