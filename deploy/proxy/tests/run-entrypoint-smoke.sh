#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
#
# Native proxy-entrypoint smoke. Parent launches nginx via PATH (known local
# binary: /opt/homebrew/bin/nginx). Shipped sources never mention that path.
# Does not invoke Docker.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
PROXY="$ROOT/deploy/proxy"
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/ocu-proxy-entry-smoke.XXXXXX")"
AUTH_PORT="${OCU_TEST_AUTH_PORT:-18780}"
OCU_PORT="${OCU_TEST_OCU_PORT:-18790}"
LISTEN_PORT="${OCU_TEST_PROXY_PORT:-18782}"
TOKEN='synthetic-entry-token'
ORIGIN="http://127.0.0.1:${AUTH_PORT}"
RECORD="$WORKDIR/record/requests.jsonl"
PASS=0
FAIL=0

cleanup() {
    if [[ -n "${ENTRY_PID:-}" ]] && kill -0 "$ENTRY_PID" 2>/dev/null; then
        kill -TERM "$ENTRY_PID" 2>/dev/null || true
        wait "$ENTRY_PID" 2>/dev/null || true
    fi
    if [[ -n "${FIXTURE_PID:-}" ]] && kill -0 "$FIXTURE_PID" 2>/dev/null; then
        kill -TERM "$FIXTURE_PID" 2>/dev/null || true
        wait "$FIXTURE_PID" 2>/dev/null || true
    fi
    rm -rf "$WORKDIR"
}
trap cleanup EXIT

pass() { printf 'PASS %s\n' "$1"; PASS=$((PASS + 1)); }
fail() { printf 'FAIL %s\n' "$1"; FAIL=$((FAIL + 1)); }

if ! command -v nginx >/dev/null; then
    printf 'nginx is not on PATH; parent should prepend /opt/homebrew/bin\n' >&2
    exit 2
fi

umask 077
mkdir -p "$WORKDIR/opt" "$WORKDIR/record"
cp "$PROXY/render.py" "$WORKDIR/opt/render.py"
cp "$PROXY/nginx.conf.in" "$WORKDIR/opt/nginx.conf.in"
cp "$PROXY/routes.json" "$WORKDIR/opt/routes.json"
cp "$PROXY/entrypoint.sh" "$WORKDIR/opt/entrypoint.sh"
chmod 0555 "$WORKDIR/opt/entrypoint.sh"

python3 "$PROXY/tests/fixture.py" \
    --auth-port "$AUTH_PORT" \
    --ocu-port "$OCU_PORT" \
    --record "$RECORD" &
FIXTURE_PID=$!

for _ in $(seq 1 50); do
    if python3 -c "import socket; s=socket.create_connection(('127.0.0.1', $AUTH_PORT), 0.2); s.close()"; then
        break
    fi
    sleep 0.1
done

export OCU_INTERNAL_TOKEN="$TOKEN"
export OCU_WEBUI_ORIGIN="$ORIGIN"
export OCU_WEBUI_UPSTREAM="http://127.0.0.1:${AUTH_PORT}"
export OCU_PROXY_UPSTREAM="http://127.0.0.1:${OCU_PORT}"
export OCU_PROXY_LISTEN="127.0.0.1:${LISTEN_PORT}"

"$WORKDIR/opt/entrypoint.sh" &
ENTRY_PID=$!

ready=0
for _ in $(seq 1 50); do
    if python3 -c "import socket; s=socket.create_connection(('127.0.0.1', $LISTEN_PORT), 0.2); s.close()"; then
        ready=1
        break
    fi
    sleep 0.1
done
if [[ "$ready" -eq 1 ]]; then
    pass "entrypoint is listening"
else
    fail "entrypoint did not listen"
fi

body="$(python3 - "$LISTEN_PORT" <<'PY'
import http.client, sys
port = int(sys.argv[1])
conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
conn.request("GET", "/", headers={"Host": "127.0.0.1"})
resp = conn.getresponse()
print(resp.status)
print(resp.read().decode("utf-8", "replace"))
PY
)"
status="$(printf '%s\n' "$body" | sed -n '1p')"
payload="$(printf '%s\n' "$body" | sed -n '2p')"
if [[ "$status" == "200" && "$payload" == "webui" ]]; then
    pass "webui origin is forwarded"
else
    fail "webui origin was not forwarded"
fi
ocu_forward="$(python3 - "$LISTEN_PORT" <<'PY'
import http.client, sys
conn = http.client.HTTPConnection("127.0.0.1", int(sys.argv[1]), timeout=5)
conn.request("GET", "/ocu/api/outputs/chat-ABC-123", headers={"Cookie": "session=owner"})
resp = conn.getresponse()
print(resp.status)
print(resp.read().decode("utf-8", "replace"))
PY
)"
if [[ "$ocu_forward" == $'200\nocu' ]]; then
    pass "owner-authenticated OCU request is forwarded"
else
    fail "owner-authenticated OCU request was not forwarded"
fi


conf="$WORKDIR/opt/nginx.conf"
runtime="$WORKDIR/opt/runtime"
mode_conf="$(python3 -c "import os,stat; print(oct(os.stat('$conf').st_mode & 0o777))")"
mode_runtime="$(python3 -c "import os,stat; print(oct(os.stat('$runtime').st_mode & 0o777))")"
if [[ "$mode_conf" == "0o600" ]]; then
    pass "generated nginx.conf is private"
else
    fail "generated nginx.conf mode is $mode_conf"
fi
if [[ "$mode_runtime" == "0o700" ]]; then
    pass "runtime directory is private"
else
    fail "runtime directory mode is $mode_runtime"
fi
if grep -q "server 127.0.0.1:${AUTH_PORT};" "$conf" && grep -q "server 127.0.0.1:${OCU_PORT};" "$conf"; then
    pass "rendered upstreams match local fixtures"
else
    fail "rendered upstreams are missing"
fi

kill -TERM "$ENTRY_PID"
wait "$ENTRY_PID" || true
ENTRY_PID=""
if python3 -c "import socket; s=socket.create_connection(('127.0.0.1', $LISTEN_PORT), 0.2); s.close()" 2>/dev/null; then
    fail "listen port remained open after TERM"
else
    pass "TERM stopped the listener"
fi

unset OCU_INTERNAL_TOKEN
set +e
fail_out="$WORKDIR/missing-token.err"
( "$WORKDIR/opt/entrypoint.sh" >"$WORKDIR/missing-token.out" 2>"$fail_out" ) &
fail_pid=$!
wait "$fail_pid"
fail_status=$?
set -e
if [[ "$fail_status" -ne 0 ]]; then
    pass "missing token fails closed"
else
    fail "missing token started the proxy"
fi
if python3 -c "import socket; s=socket.create_connection(('127.0.0.1', $LISTEN_PORT), 0.2); s.close()" 2>/dev/null; then
    fail "missing-token path still listened"
else
    pass "missing-token path did not serve"
fi
if grep -F "$TOKEN" "$fail_out" "$WORKDIR/missing-token.out" >/dev/null 2>&1; then
    fail "token leaked into diagnostics"
else
    pass "token is absent from diagnostics"
fi

printf 'PASSED=%s FAILED=%s\n' "$PASS" "$FAIL"
if [[ "$FAIL" -ne 0 ]]; then
    exit 1
fi
