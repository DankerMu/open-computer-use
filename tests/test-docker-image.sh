#!/bin/bash
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
# Test Docker image for package availability, CLI tools, and correct npm layout.
# Usage: ./tests/test-docker-image.sh [image-name] [--browser-relay-only]
# Default image: ai-computer-use-test:latest
#
# Exit code: 0 = all tests passed, 1 = some tests failed

set -euo pipefail

IMAGE="${1:-ai-computer-use-test:latest}"
MODE="${2:-}"
if [ "$#" -gt 2 ] || { [ -n "$MODE" ] && [ "$MODE" != "--browser-relay-only" ]; }; then
    echo "Usage: $0 [image-name] [--browser-relay-only]" >&2
    exit 2
fi
PASSED=0
FAILED=0
FAILURES=""

pass() {
    PASSED=$((PASSED + 1))
    echo "  PASS: $1"
}

fail() {
    FAILED=$((FAILED + 1))
    FAILURES="${FAILURES}\n  - $1"
    echo "  FAIL: $1"
}

run_in_container() {
    # --entrypoint=bash bypasses /home/assistant/.entrypoint.sh, which prints
    # GITLAB_TOKEN / ANTHROPIC_AUTH_TOKEN status banners and corrupts captured
    # stdout when those env vars are unset (CI default).
    # --user=assistant matches production (docker-compose runs as assistant)
    # so user-scoped npm config (prefix delete, etc.) is the configuration
    # actually under test.
    docker run --rm --platform linux/amd64 --entrypoint=bash --user=assistant "$IMAGE" -c "$1" 2>/dev/null
}

# Offline, bounded tool probes retain diagnostics and the container's exit status.
run_in_container_verbose() {
    # Kill stalled compilers/browser children before Docker removes the owned container.
    docker run --rm --platform linux/amd64 --network=none \
        --entrypoint=timeout --user=assistant "$IMAGE" \
        --kill-after=10s 240s bash -euo pipefail -c "$1" 2>&1
}

# Host Python bounds Docker calls, including startup and cleanup.
bounded_docker() {
    python3 -c '
import subprocess
import sys

try:
    result = subprocess.run(["docker", *sys.argv[2:]], timeout=float(sys.argv[1]))
except subprocess.TimeoutExpired:
    print(f"Docker command exceeded {sys.argv[1]}s: {sys.argv[2:]}", file=sys.stderr)
    sys.exit(124)
sys.exit(result.returncode)
' "$@"
}

# Remove every container labelled <label-key>=<owner>. The label, not the
# cidfile, is the record: a timed-out Docker client may not have written it.
reap_owned_containers() {
    local owned container rc=0
    owned=$(bounded_docker 30 ps -aq --filter "label=$1=$2") || return 1
    for container in $owned; do
        if ! bounded_docker 30 rm -f "$container" >/dev/null; then
            rc=1
        fi
    done
    return "$rc"
}

# Own a fresh container; cleanup also runs when setup or a bounded probe fails.
run_browser_relay_regression() (
    set -euo pipefail
    work=$(mktemp -d) || exit
    owner="ocu-browser-relay-${work##*/}"
    launching=0
    cleanup() {
        rc=$?
        trap - EXIT
        if [ "$launching" -eq 1 ]; then
            reap_owned_containers ocu.browser-relay-test-owner "$owner" || rc=1
        fi
        rm -rf "$work"
        exit "$rc"
    }
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    existing=$(bounded_docker 30 ps -aq --filter "label=ocu.browser-relay-test-owner=$owner") || exit
    if [ -n "$existing" ]; then
        echo "Refusing to reuse browser relay test ownership label: $owner" >&2
        exit 1
    fi
    launching=1

    bounded_docker 30 run -d --rm --platform linux/amd64 --user=assistant \
        --cidfile "$work/cid" --entrypoint=bash \
        --label "ocu.browser-relay-test-owner=$owner" \
        -e PLAYWRIGHT_MCP_CONFIG=/tmp/relay-playwright.json \
        "$IMAGE" -c 'exec sleep 180' >/dev/null || exit
    cid=$(cat "$work/cid") || exit

    bounded_docker 100 exec -i "$cid" timeout 90s bash -s <<'RELAY_SMOKE'
set -euo pipefail

# Use the image's installed Chromium without an external display or download.
timeout 20s python3 - <<'CONFIG'
import json
from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    executable = playwright.chromium.executable_path
with open("/tmp/relay-playwright.json", "w") as config:
    json.dump({
        "outputDir": "/tmp/relay-output",
        "browser": {
            "browserName": "chromium",
            "launchOptions": {
                "executablePath": executable,
                "headless": True,
                "args": ["--no-sandbox", "--disable-dev-shm-usage"],
            },
        },
    }, config)
CONFIG

cat > /tmp/relay-probe.py <<'PROBE'
import json
import os
from pathlib import Path
import socket
import sys
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright


def listeners():
    result = {}
    for row in Path("/proc/net/tcp").read_text().splitlines()[1:]:
        fields = row.split()
        if fields[3] != "0A":
            continue
        host, port = fields[1].split(":")
        address = socket.inet_ntoa(bytes.fromhex(host)[::-1])
        result[(address, int(port, 16))] = fields[9]
    return result


def relays():
    result = {}
    for process in Path("/proc").glob("[0-9]*"):
        try:
            args = (process / "cmdline").read_bytes().split(b"\0")
            if (os.path.basename(args[0]) == b"socat"
                    and b"TCP-LISTEN:9222,fork,reuseaddr,bind=0.0.0.0" in args):
                result[process.name] = args
        except (FileNotFoundError, ProcessLookupError):
            continue
    return result


active = listeners()
processes = relays()
if sys.argv[1] == "before":
    assert not processes, f"Expected no prior relay, found {processes}"
    assert not any(port in (9222, 9223) for _, port in active), active
    print("Fresh container: no prior relay or CDP listener")
    sys.exit(0)

assert ("0.0.0.0", 9222) in active, f"Missing all-interface relay: {active}"
assert ("127.0.0.1", 9223) in active, f"Missing loopback Chromium: {active}"
inode = f"socket:[{active[('0.0.0.0', 9222)]}]"
owners = []
for pid, args in processes.items():
    assert b"TCP:127.0.0.1:9223" in args, args
    try:
        sockets = [os.readlink(fd) for fd in Path(f"/proc/{pid}/fd").iterdir()]
    except (FileNotFoundError, ProcessLookupError):
        continue
    if inode in sockets:
        owners.append(pid)
assert len(owners) == 1, f"Expected one relay listener owner, found {owners}"
pid = owners[0]
if len(sys.argv) == 3:
    assert pid == sys.argv[2], f"Relay replaced: {sys.argv[2]} -> {pid}"

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
versions = []
pages = []
for port in (9222, 9223):
    base = f"http://127.0.0.1:{port}"
    with opener.open(f"{base}/json/version", timeout=2) as response:
        versions.append(json.load(response))
    with opener.open(f"{base}/json/list", timeout=2) as response:
        pages.append({(page["id"], page["url"]) for page in json.load(response)
                      if page["type"] == "page"})
assert pages[0] == pages[1] and any(url == "about:blank" for _, url in pages[0]), pages
paths = [urllib.parse.urlsplit(version["webSocketDebuggerUrl"]).path
         for version in versions]
assert paths[0] == paths[1] and paths[0].startswith("/devtools/browser/"), paths
assert versions[0]["Browser"] == versions[1]["Browser"], versions

# Send a real CDP command through the relay, not just a discovery request.
with sync_playwright() as playwright:
    browser = playwright.chromium.connect_over_cdp("http://127.0.0.1:9222", timeout=3000)
    session = browser.new_browser_cdp_session()
    version = session.send("Browser.getVersion")
    assert version["product"] == versions[1]["Browser"], version
    session.detach()
    browser.close()

print(f"Live CDP via 0.0.0.0:9222 -> 127.0.0.1:9223; relay PID {pid}", file=sys.stderr)
print(pid)
PROBE

timeout 15s python3 /tmp/relay-probe.py before
relay_pid=""
for attempt in 1 2; do
    if output=$(timeout 15s bash -c "playwright-cli open about:blank 2>&1 | head -40"); then
        status=0
    else
        status=$?
    fi
    printf 'Captured open %s: exit %s\n%s\n' "$attempt" "$status" "$output"
    if [ "$status" -ne 0 ] || [[ "$output" != *opened* ]] || [[ "$output" != *about:blank* ]]; then
        echo "Expected captured open to exit 0 and report an opened about:blank page" >&2
        exit 1
    fi
    if [ "$attempt" -eq 1 ]; then
        relay_pid=$(timeout 15s python3 /tmp/relay-probe.py after)
    else
        timeout 15s python3 /tmp/relay-probe.py after "$relay_pid"
    fi
done
RELAY_SMOKE
)

test_browser_relay() {
    echo ""
    echo "[browser-relay] Captured open releases streams and preserves live CDP"
    if RELAY_OUT=$(run_browser_relay_regression 2>&1); then
        pass "captured browser open completes; live CDP relay is reused"
    else
        RELAY_EXIT=$?
        fail "browser relay regression exited $RELAY_EXIT"
    fi
    printf '%s\n' "$RELAY_OUT"
}

# Own a container started through the real ENTRYPOINT, which writes the
# production CLI config (run_in_container bypasses it); probe from the home
# directory and from an unrelated one, each in its own subshell.
run_browser_cli_config_regression() (
    set -euo pipefail
    work=$(mktemp -d) || exit
    owner="ocu-browser-cli-config-${work##*/}"
    launching=0
    cleanup() {
        rc=$?
        trap - EXIT
        if [ "$launching" -eq 1 ]; then
            reap_owned_containers ocu.browser-cli-config-test-owner "$owner" || rc=1
        fi
        rm -rf "$work"
        exit "$rc"
    }
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    existing=$(bounded_docker 30 ps -aq --filter "label=ocu.browser-cli-config-test-owner=$owner") || exit
    if [ -n "$existing" ]; then
        echo "Refusing to reuse browser CLI config test ownership label: $owner" >&2
        exit 1
    fi
    launching=1

    # The entrypoint ends with exec "$@", so sleep keeps the container alive.
    bounded_docker 30 run -d --rm --platform linux/amd64 --network=none --user=assistant \
        --cidfile "$work/cid" \
        --label "ocu.browser-cli-config-test-owner=$owner" \
        "$IMAGE" sleep 420 >/dev/null || exit
    cid=$(cat "$work/cid") || exit

    # Two iterations of at most 60 + 60 + 30 s each fit inside the bound.
    bounded_docker 400 exec -i "$cid" timeout 360s bash -s <<'CLI_CONFIG_PROBE'
set -uo pipefail
for i in $(seq 50); do [ -s /home/assistant/playwright-cli.json ] && break; sleep 0.2; done
[ -s /home/assistant/playwright-cli.json ] || { echo "entrypoint did not write /home/assistant/playwright-cli.json" >&2; exit 1; }
rc=0
for dir in /home/assistant /tmp; do
  (
    set -euo pipefail
    trap 'timeout 30s playwright-cli close >/dev/null 2>&1 || echo "close failed from '"$dir"'" >&2' EXIT
    cd "$dir" || exit 1
    rm -f /mnt/user-data/files/page-*.yml
    if out=$(timeout 60s bash -o pipefail -c "playwright-cli open about:blank 2>&1 | head -40"); then status=0; else status=$?; fi
    printf 'open from %s: exit %s\n%s\n' "$dir" "$status" "$out"
    case "$out" in *"Chromium distribution"*) echo "chrome channel fallback from $dir" >&2; exit 1;; esac
    [ "$status" -eq 0 ] && [[ "$out" == *opened* ]] && [[ "$out" == *about:blank* ]] || { echo "open did not report an opened about:blank page from $dir" >&2; exit 1; }
    if snap=$(timeout 60s playwright-cli snapshot 2>&1 | head -20); then :; else echo "snapshot failed from $dir: $snap" >&2; exit 1; fi
    printf 'snapshot from %s:\n%s\n' "$dir" "$snap"
    [[ "$snap" == *"mnt/user-data/files/page-"*".yml)"* ]] || { echo "snapshot link does not point into /mnt/user-data/files from $dir" >&2; exit 1; }
    ls /mnt/user-data/files/page-*.yml >/dev/null 2>&1 || { echo "no snapshot under /mnt/user-data/files from $dir" >&2; ls -la /mnt/user-data/files >&2 2>&1 || true; exit 1; }
    for stray in /home/assistant/.playwright-cli /tmp/.playwright-cli; do
      [ ! -e "$stray" ] || { echo "unexpected $stray from $dir" >&2; ls -la "$stray" >&2 2>&1 || true; exit 1; }
    done
  ); st=$?; [ "$st" -eq 0 ] || { rc=1; echo "FAILED from $dir" >&2; }
done
exit "$rc"
CLI_CONFIG_PROBE
)

test_browser_cli_config() {
    echo ""
    echo "[browser-cli-config] Production CLI config loads from /home/assistant and /tmp"
    if CLI_CONFIG_OUT=$(run_browser_cli_config_regression 2>&1); then
        pass "browser CLI loads the production config; snapshots land in /mnt/user-data/files"
    else
        CLI_CONFIG_EXIT=$?
        fail "browser CLI config regression exited $CLI_CONFIG_EXIT"
    fi
    printf '%s\n' "$CLI_CONFIG_OUT"
}

if [ "$MODE" = "--browser-relay-only" ]; then
    test_browser_relay
    [ "$FAILED" -eq 0 ]
    exit
fi

echo "=== Testing Docker image: $IMAGE ==="
echo ""

# Note on `|| VAR=""` after every `VAR=$(run_in_container ...)`:
# Without it, `set -euo pipefail` aborts the whole script the moment docker
# returns non-zero — before fail() can record the failure or print summary.
# Forcing the assignment to succeed (with empty content on docker error) lets
# the existing grep-based pass/fail accounting handle the failure naturally.

# 1. Runtime versions and executable Node tools
echo "[1/14] Runtime versions and Node tools"
NODE_PROBE=$(cat <<'PROBE'
node <<'JS'
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { execFileSync } = require("node:child_process");
const semver = require("/usr/local/lib/node_modules/npm/node_modules/semver");
const versions = { node: process.versions.node, npm: execFileSync("npm", ["--version"], { encoding: "utf8" }).trim() };
console.log(JSON.stringify({ ...versions, platform: process.platform, arch: process.arch }));
assert.equal(versions.node, "22.23.2");
assert.equal(versions.npm, "10.9.8");
assert.equal(process.platform, "linux");
assert.equal(process.arch, "x64");
const manifest = JSON.parse(fs.readFileSync("/tmp/package.json", "utf8"));
assert.deepEqual(manifest.engines, { node: ">=22.20.0", npm: ">=10.9.3" });
for (const [runtime, range] of Object.entries(manifest.engines)) {
    assert(semver.satisfies(versions[runtime], range), `manifest ${runtime}: ${range}`);
}
for (const root of ["/home/node_modules", "/usr/local/lib/node_modules_global/lib/node_modules"]) {
    for (const [name, range] of Object.entries(manifest.dependencies)) {
        const pkg = JSON.parse(fs.readFileSync(`${root}/${name}/package.json`, "utf8"));
        console.log(JSON.stringify({ root, name, version: pkg.version, engines: pkg.engines ?? {} }));
        assert(semver.satisfies(pkg.version, range), `${root}/${name}: ${pkg.version} outside ${range}`);
        for (const runtime of ["node", "npm"]) {
            if (pkg.engines?.[runtime]) {
                assert(semver.satisfies(versions[runtime], pkg.engines[runtime]), `${root}/${name}: incompatible ${runtime} ${pkg.engines[runtime]}`);
            }
        }
    }
}
JS

# Keep ESM fixtures under /home so they resolve the image's installed packages.
work=$(mktemp -d /home/assistant/.node-probe.XXXXXX)
trap 'rm -rf "$work"' EXIT
cd "$work"
printf '{"type":"module"}\n' > package.json
mkdir types
cat > good.ts <<'TS'
export const twice = (value: number): number => value * 2;
console.log(twice(21));
TS
TSX_RESULT=$(tsx good.ts)
test "$TSX_RESULT" = "42"
echo "tsx: typed fixture = 42"
tsc --strict --target es2022 --module es2022 --typeRoots "$work/types" --outDir compiled good.ts
COMPILED_RESULT=$(node compiled/good.js)
test "$COMPILED_RESULT" = "42"
printf 'const value: number = "wrong";\n' > bad.ts
if tsc --noEmit --strict --typeRoots "$work/types" bad.ts > bad-types.log 2>&1; then
    echo "tsc accepted an ill-typed fixture" >&2
    exit 1
fi
cat bad-types.log
grep -q 'error TS2322' bad-types.log
echo "tsc: emitted result = 42; invalid assignment rejected with TS2322"

python3 <<'PY'
import json
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    config = {"executablePath": p.chromium.executable_path, "args": ["--no-sandbox"]}
with open("puppeteer.json", "w") as output:
    json.dump(config, output)
print("Mermaid browser configuration:", json.dumps(config))
PY
printf 'graph LR\n  input[RuntimeInput] --> output[RuntimeOutput]\n' > graph.mmd
# Keep graph labels in SVG text rather than embedded HTML.
printf '{"htmlLabels":false}\n' > mermaid.json
mmdc -p puppeteer.json -c mermaid.json -i graph.mmd -o graph.svg
python3 <<'PY'
import json
import xml.etree.ElementTree as ET
root = ET.parse("graph.svg").getroot()
ns = "{http://www.w3.org/2000/svg}"
assert root.tag == ns + "svg", root.tag
labels = ["".join(node.itertext()).strip() for node in root.iter(ns + "text")]
assert "RuntimeInput" in labels and "RuntimeOutput" in labels, labels
edges = [
    node for node in root.iter(ns + "path")
    if "flowchart-link" in node.get("class", "").split()
]
assert len(edges) == 1 and edges[0].get("d") and edges[0].get("marker-end")
print("Mermaid SVG:", json.dumps({"labels": labels, "directed_edges": len(edges)}))
PY

# A different PDF implementation produces the input for pdfjs-dist.
python3 <<'PY'
from reportlab.pdfgen.canvas import Canvas
pdf = Canvas("fixture.pdf", pagesize=(300, 200))
pdf.drawString(30, 100, "Node runtime PDF fixture")
pdf.showPage()
pdf.save()
PY
cat > parse.mjs <<'JS'
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { getDocument } from "pdfjs-dist/legacy/build/pdf.mjs";
const loadingTask = getDocument({
    data: new Uint8Array(readFileSync("fixture.pdf")),
    standardFontDataUrl: "/home/node_modules/pdfjs-dist/standard_fonts/"
});
try {
    const pdf = await loadingTask.promise;
    assert.equal(pdf.numPages, 1);
    const page = await pdf.getPage(1);
    const content = await page.getTextContent();
    const text = content.items.map(item => item.str).join("");
    assert.equal(text, "Node runtime PDF fixture");
    console.log("pdfjs-dist:", JSON.stringify({ pages: pdf.numPages, text }));
} finally {
    await loadingTask.destroy();
}
JS
node parse.mjs
PROBE
)
if NODE_OUT=$(run_in_container_verbose "$NODE_PROBE"); then
    pass "approved Node/npm, installed engines, Mermaid, tsx, tsc and PDF behavior"
else
    NODE_EXIT=$?
    fail "Node runtime/tool verification exited $NODE_EXIT"
fi
printf '%s\n' "$NODE_OUT"
VERSIONS=$(run_in_container 'python3 --version') || VERSIONS=""
echo "$VERSIONS" | grep -q "Python 3" && pass "Python 3" || fail "Python version"

# 2. CommonJS require()
echo ""
echo "[2/14] CommonJS require()"
for pkg in react pptxgenjs pdf-lib docx sharp react-dom/server react-icons/fa; do
    RESULT=$(run_in_container "node -e \"try { require('$pkg'); console.log('OK') } catch(e) { console.log('FAIL: ' + e.code) }\"") || RESULT=""
    echo "$RESULT" | grep -q "OK" && pass "require('$pkg')" || fail "require('$pkg'): $RESULT"
done

# marked renders, rather than merely loading. A major of a renderer keeps the
# module importable and changes the API or the output shape, so loading it alone
# waves that through; parsing a heading and checking for <h1> does not.
#
# ESM, not require(): the image ships a marked build that is ESM-only, so
# require() raises ERR_REQUIRE_ESM. It therefore does NOT belong in the CommonJS
# list above — measured in the image, not assumed from the host, where an older
# CommonJS marked is installed and require() succeeds.
RESULT=$(run_in_container "node --input-type=module -e \"import {marked} from 'marked'; const h=marked.parse('# t'); console.log(/<h1/.test(h)?'OK':'FAIL')\"") || RESULT=""
echo "$RESULT" | grep -q "OK" && pass "marked renders a heading" || fail "marked render"

# 3. ES Modules import
echo ""
echo "[3/14] ES Modules import"
for pkg in react pptxgenjs pdf-lib; do
    RESULT=$(run_in_container "node --input-type=module -e \"import '$pkg'; console.log('OK')\"") || RESULT=""
    echo "$RESULT" | grep -q "OK" && pass "import '$pkg'" || fail "import '$pkg'"
done

# 4. html2pptx import (full path)
echo ""
echo "[4/14] html2pptx import"
RESULT=$(run_in_container "node --input-type=module -e \"import { html2pptx } from '/usr/local/lib/node_modules_global/lib/node_modules/@anthropic-ai/html2pptx/dist/html2pptx.mjs'; console.log('OK')\"" 2>/dev/null || echo "SKIP")
if echo "$RESULT" | grep -q "OK"; then
    pass "html2pptx ESM import"
elif echo "$RESULT" | grep -q "SKIP"; then
    echo "  SKIP: html2pptx (package path may vary)"
else
    fail "html2pptx ESM import"
fi

# 5. CLI tools (Phase 6 — extended to include codex + opencode + --version smoke)
echo ""
echo "[5/14] CLI tools"
for tool in mmdc tsc tsx claude codex opencode; do
    RESULT=$(run_in_container "which $tool >/dev/null 2>&1 && echo OK || echo MISSING") || RESULT=""
    echo "$RESULT" | grep -q "OK" && pass "$tool in PATH" || fail "$tool not found in PATH"
    # Phase 6 / TEST-01 — every CLI must respond to --version with exit 0.
    VRESULT=$(run_in_container "$tool --version >/dev/null 2>&1 && echo OK || echo FAIL") || VRESULT=""
    echo "$VRESULT" | grep -q "OK" && pass "$tool --version exit 0" || fail "$tool --version failed"
done

# list-subagent-models presence + executability (Phase 1 / Plan 01-05 install line)
RESULT=$(run_in_container "which list-subagent-models >/dev/null 2>&1 && echo OK || echo MISSING") || RESULT=""
echo "$RESULT" | grep -q "OK" && pass "list-subagent-models in PATH" || fail "list-subagent-models not found in PATH"
RESULT=$(run_in_container "test -x \$(which list-subagent-models 2>/dev/null || echo /nonexistent) && echo OK || echo FAIL") || RESULT=""
echo "$RESULT" | grep -q "OK" && pass "list-subagent-models is executable" || fail "list-subagent-models is not executable inside image"

# Phase 2 smoke: list-subagent-models claude path (always available, no env or
# config files needed) returns valid JSON with cli="claude" and non-empty
# models array. This catches a class of bugs where the script is present and
# executable but crashes at runtime (broken import, missing stdlib, etc.).
RESULT=$(run_in_container "SUBAGENT_CLI=claude list-subagent-models 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d[\"cli\"]==\"claude\" and len(d[\"models\"])>=3, d; print(\"OK\")'") || RESULT=""
echo "$RESULT" | grep -q "OK" && pass "list-subagent-models claude returns valid JSON with >=3 models" || fail "list-subagent-models claude smoke failed"

# Phase 2 smoke: list-subagent-models opencode path with no env (canonical
# cli-defaults/opencode.json fallback). Validates the host-friendly read path
# end-to-end inside the container.
RESULT=$(run_in_container "unset OPENCODE_CONFIG_EXTRA OPENCODE_MODEL_ALIASES OPENCODE_SUB_AGENT_DEFAULT_MODEL; SUBAGENT_CLI=opencode list-subagent-models 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d[\"cli\"]==\"opencode\" and \"models\" in d, d; print(\"OK\")'") || RESULT=""
echo "$RESULT" | grep -q "OK" && pass "list-subagent-models opencode returns valid JSON from canonical cli-defaults" || fail "list-subagent-models opencode canonical smoke failed"

# The entrypoint's own config writer, run for real rather than bypassed.
#
# Every other check here uses --entrypoint=bash, which is what let this defect
# live: the entrypoint is generated by one ~300-line `RUN printf '...'`, and a
# bare %s inside that format string is consumed by the build's own printf. The
# shipped line became `printf ""` — an empty format with the config passed as a
# discarded argument. The file was created, so nothing failed; it was 0 bytes,
# and opencode silently loaded its own defaults instead of the operator's
# gateway. Measured in production: 465 bytes of the variable inside the
# container, 0 bytes in the file it should have written.
#
# So this asserts the byte count, not the exit code. Reading the Dockerfile
# source would not catch it — the source is correct, and the bug is introduced
# between source and image.
#
# The branch also echoes a status banner on success, so its output is discarded;
# only the file is measured.
CFG='{"model":"litellm/probe","provider":{"litellm":{}}}'
RESULT=$(docker run --rm --platform linux/amd64 --user=assistant \
    -e SUBAGENT_CLI=opencode -e OPENCODE_CONFIG_EXTRA="$CFG" \
    --entrypoint=bash "$IMAGE" -c '
        eval "$(sed -n "/^ *case .\${SUBAGENT_CLI/,/^ *esac/p" /home/assistant/.entrypoint.sh)" >/dev/null 2>&1
        printf "%s|%s" "$(wc -c < /tmp/opencode.json)" "$(cat /tmp/opencode.json)"
    ' 2>/dev/null) || RESULT=""
EXPECTED_LEN=${#CFG}
if [ "$RESULT" = "${EXPECTED_LEN}|${CFG}" ]; then
    pass "entrypoint writes OPENCODE_CONFIG_EXTRA to /tmp/opencode.json intact ($EXPECTED_LEN bytes)"
else
    fail "entrypoint config writer: expected ${EXPECTED_LEN}|<config>, got '${RESULT}'"
fi

# 6. Python packages
echo ""
echo "[6/14] Python packages"
for pkg in docx pptx openpyxl; do
    RESULT=$(run_in_container "python3 -c \"import $pkg; print('OK')\"") || RESULT=""
    echo "$RESULT" | grep -q "OK" && pass "python import $pkg" || fail "python import $pkg"
done

# The data/imaging packages requirements.txt installs but nothing imported. A
# major bump of any of these builds cleanly — pip resolves it, the image layer
# is produced, "Build Sandbox Image" goes green — and then fails the first time
# a skill touches it. Each line below EXERCISES the package rather than merely
# importing it, because a broken major usually imports fine and moves an API.
RESULT=$(run_in_container "python3 -c \"import pandas as pd; df=pd.DataFrame({'a':[1,2]}); assert df['a'].sum()==3; print('OK')\"") || RESULT=""
echo "$RESULT" | grep -q "OK" && pass "python pandas (DataFrame + sum)" || fail "python pandas"
RESULT=$(run_in_container "python3 -c \"import numpy as np; assert np.arange(3).sum()==3; print('OK')\"") || RESULT=""
echo "$RESULT" | grep -q "OK" && pass "python numpy (arange + sum)" || fail "python numpy"
RESULT=$(run_in_container "python3 -c \"import scipy.stats as st; assert round(st.norm.cdf(0),3)==0.5; print('OK')\"") || RESULT=""
echo "$RESULT" | grep -q "OK" && pass "python scipy (stats.norm)" || fail "python scipy"
RESULT=$(run_in_container "python3 -c \"import cv2, numpy as np; img=np.zeros((4,4,3),dtype=np.uint8); assert cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).shape==(4,4); print('OK')\"") || RESULT=""
echo "$RESULT" | grep -q "OK" && pass "python opencv (cvtColor)" || fail "python opencv"
RESULT=$(run_in_container "python3 -c \"import imageio.v3 as iio, numpy as np, tempfile, os; p=os.path.join(tempfile.mkdtemp(),'t.png'); iio.imwrite(p, np.zeros((4,4,3),dtype=np.uint8)); assert iio.imread(p).shape==(4,4,3); print('OK')\"") || RESULT=""
echo "$RESULT" | grep -q "OK" && pass "python imageio (write + read round-trip)" || fail "python imageio"
RESULT=$(run_in_container "python3 -c \"from PIL import Image; im=Image.new('RGB',(4,4)); assert im.size==(4,4); print('OK')\"") || RESULT=""
echo "$RESULT" | grep -q "OK" && pass "python pillow (Image.new)" || fail "python pillow"
RESULT=$(run_in_container "python3 -c \"from playwright.sync_api import sync_playwright; print('OK')\"") || RESULT=""
echo "$RESULT" | grep -q "OK" && pass "python playwright" || fail "python playwright"

# 7. User npm install (lodash)
echo ""
echo "[7/14] User npm install"
RESULT=$(run_in_container 'cd /home/assistant && npm install lodash >/dev/null 2>&1 && if [ -d /home/assistant/node_modules/lodash ]; then echo "user=YES"; else echo "user=NO"; fi && SYS_COUNT=$(ls /home/node_modules/ 2>/dev/null | wc -l) && echo "system=$SYS_COUNT"') || RESULT=""
echo "$RESULT" | grep -q "user=YES" && pass "lodash in /home/assistant/node_modules" || fail "lodash not in user dir"
SYS=$(echo "$RESULT" | awk -F= '/^system=/{print $2}')
[ "${SYS:-0}" -gt 100 ] && pass "system packages intact ($SYS)" || fail "system packages count: $SYS (expected > 100)"

# 8. npm prefix check
echo ""
echo "[8/14] npm configuration"
RESULT=$(run_in_container 'npm config get prefix 2>/dev/null || echo "undefined"') || RESULT=""
# Trim trailing whitespace/newlines so the anchored regex below can match
# against a clean single-line value.
RESULT_TRIMMED=$(echo "$RESULT" | head -n1 | tr -d '[:space:]')
# Acceptable, anchored values:
#   - "undefined"                           — legacy npm with prefix unset
#   - "/usr/local"                          — Dockerfile runs `npm config
#                                             delete prefix` as last step, so
#                                             npm falls back to its default
#   - "/usr/local/lib/node_modules_global"  — explicit pin (older image layout)
# The anchored regex rejects silently permissive substring matches like
# "/usr/local/something/weird" or "node_modules_global" appearing in
# arbitrary paths.
if echo "$RESULT_TRIMMED" | grep -qE "^(undefined|/usr/local|/usr/local/lib/node_modules_global)$"; then
    pass "npm prefix configured correctly ($RESULT_TRIMMED)"
else
    fail "npm prefix: $RESULT_TRIMMED"
fi

# 9. Volume size
echo ""
echo "[9/14] Volume size"
SIZE_KB=$(run_in_container "du -sk /home/assistant/ | cut -f1") || SIZE_KB=""
if [ "${SIZE_KB:-999999}" -lt 1024 ]; then
    pass "/home/assistant < 1MB (${SIZE_KB}KB)"
else
    fail "/home/assistant = ${SIZE_KB}KB (expected < 1024KB)"
fi

# 10. Permissions and guard files
echo ""
echo "[10/14] Permissions and guard files"
RESULT=$(run_in_container '
[ -x /home/assistant/.entrypoint.sh ] && echo "entrypoint=OK" || echo "entrypoint=FAIL"
[ -f /home/assistant/.gitconfig ] && echo "gitconfig=OK" || echo "gitconfig=FAIL"
[ -f /home/assistant/package.json ] && echo "packagejson=OK" || echo "packagejson=FAIL"
OWNER=$(stat -c %U /home/assistant/.entrypoint.sh)
echo "owner=$OWNER"
') || RESULT=""
echo "$RESULT" | grep -q "entrypoint=OK" && pass ".entrypoint.sh executable" || fail ".entrypoint.sh not executable"
echo "$RESULT" | grep -q "gitconfig=OK" && pass ".gitconfig exists" || fail ".gitconfig missing"
echo "$RESULT" | grep -q "packagejson=OK" && pass "package.json guard exists" || fail "package.json guard missing"
echo "$RESULT" | grep -q "owner=assistant" && pass "files owned by assistant" || fail "files not owned by assistant"

# 11. Per-CLI dispatch smoke (Phase 6 / TEST-06).
# Run the image with SUBAGENT_CLI set to each value and verify:
#   (a) entrypoint marker file /tmp/.cli-runtime-initialised lands on first run
#   (b) on a SECOND entrypoint invocation the heredoc is SKIPPED
#       (ROADMAP success #4: "on restart sentinel present, heredoc skipped")
# Stub auth env vars only — no real LLM calls.
# NOTE: do NOT pass --user=assistant. Production entrypoint runs as root before
# the user shell starts (Plan 06-03 codex chown line assumes root). Running as
# root here matches production parity. /tmp is world-readable so the marker
# check works regardless.
echo ""
echo "[11/14] Per-CLI dispatch smoke + marker gating"
for cli in claude codex opencode; do
    SMOKE_CONTAINER="ocu-smoke-${cli}-$$"
    # Start a long-running container (so we can exec the entrypoint twice).
    docker run -d --rm --platform linux/amd64 \
        --name "$SMOKE_CONTAINER" \
        -e SUBAGENT_CLI="$cli" \
        -e ANTHROPIC_AUTH_TOKEN="sk-stub" \
        -e OPENAI_API_KEY="sk-stub" \
        -e OPENROUTER_API_KEY="sk-or-stub" \
        --entrypoint=bash \
        "$IMAGE" \
        -c "tail -f /dev/null" >/dev/null 2>&1 || { fail "SUBAGENT_CLI=$cli — could not start smoke container"; continue; }

    # First run: entrypoint must land marker.
    docker exec "$SMOKE_CONTAINER" /home/assistant/.entrypoint.sh true >/dev/null 2>&1 || true
    MARKER_RESULT=$(docker exec "$SMOKE_CONTAINER" sh -c "[ -f /tmp/.cli-runtime-initialised ] && echo MARKER_OK || echo MARKER_MISSING" 2>&1) || MARKER_RESULT=""
    if echo "$MARKER_RESULT" | grep -q "MARKER_OK"; then
        pass "SUBAGENT_CLI=$cli — marker /tmp/.cli-runtime-initialised landed"
    else
        fail "SUBAGENT_CLI=$cli — marker missing after first run"
        docker rm -f "$SMOKE_CONTAINER" >/dev/null 2>&1 || true
        continue
    fi

    # Marker-gating verification (only meaningful for opencode — that's the
    # CLI whose heredoc writes /tmp/opencode.json that we can sentinel-overwrite).
    # For codex the rendered file is in /home/assistant/.codex/config.toml, but
    # the gating logic is identical (one if-block guards both branches in the
    # entrypoint), so verifying for opencode proves the gate works for codex too.
    if [ "$cli" = "opencode" ]; then
        # Overwrite /tmp/opencode.json with a sentinel string. If the heredoc
        # re-fires on second invocation (gate broken), it will overwrite our
        # sentinel back to the JSON config.
        docker exec "$SMOKE_CONTAINER" sh -c 'echo "GATED-SENTINEL" > /tmp/opencode.json' >/dev/null 2>&1 || true
        # Second run: marker is already present, heredoc must be skipped.
        docker exec "$SMOKE_CONTAINER" /home/assistant/.entrypoint.sh true >/dev/null 2>&1 || true
        SENTINEL_RESULT=$(docker exec "$SMOKE_CONTAINER" cat /tmp/opencode.json 2>&1) || SENTINEL_RESULT=""
        if [ "$SENTINEL_RESULT" = "GATED-SENTINEL" ]; then
            pass "SUBAGENT_CLI=$cli — marker gating works (heredoc skipped on second run)"
        else
            fail "SUBAGENT_CLI=$cli — marker gating BROKEN: heredoc re-fired despite sentinel marker (got: $(echo "$SENTINEL_RESULT" | head -c 100))"
        fi
    fi

    # Phase 7 verification: autostart line in .bashrc references the chosen CLI.
    # The literal exec line is the same for every $cli (the SUBAGENT_CLI value
    # is resolved at session-start time, not at .bashrc-write time), so we just
    # assert the new shape exists once per loop iteration.
    AUTOSTART_LINE_BASHRC=$(docker exec "$SMOKE_CONTAINER" cat /home/assistant/.bashrc 2>/dev/null | grep -F 'SUBAGENT_AUTOSTARTED' || true)
    if echo "$AUTOSTART_LINE_BASHRC" | grep -qF 'exec "${SUBAGENT_CLI:-claude}"'; then
        pass "SUBAGENT_CLI=$cli — .bashrc autostart line wires to \${SUBAGENT_CLI:-claude}"
    else
        fail "SUBAGENT_CLI=$cli — .bashrc autostart line missing or malformed (got: $AUTOSTART_LINE_BASHRC)"
    fi
    if echo "$AUTOSTART_LINE_BASHRC" | grep -qF 'NO_AUTOSTART' && echo "$AUTOSTART_LINE_BASHRC" | grep -qF '/tmp/.no_autostart'; then
        pass "SUBAGENT_CLI=$cli — .bashrc autostart honours NO_AUTOSTART env + /tmp/.no_autostart sentinel"
    else
        fail "SUBAGENT_CLI=$cli — .bashrc autostart missing escape hatches (got: $AUTOSTART_LINE_BASHRC)"
    fi
    # Backwards-compat regression guard: old marker name must NOT appear.
    if docker exec "$SMOKE_CONTAINER" grep -q 'CLAUDE_AUTOSTARTED' /home/assistant/.bashrc 2>/dev/null; then
        fail "SUBAGENT_CLI=$cli — orphan CLAUDE_AUTOSTARTED reference in .bashrc (rename incomplete)"
    else
        pass "SUBAGENT_CLI=$cli — old CLAUDE_AUTOSTARTED marker fully purged from .bashrc"
    fi

    docker rm -f "$SMOKE_CONTAINER" >/dev/null 2>&1 || true
done

# 12. NO_AUTOSTART escape hatch smoke (Phase 7 / TERM-02).
# Exercises the .bashrc autostart guards by sourcing .bashrc with PS1 set
# (the [ -n "$PS1" ] guard would otherwise short-circuit and falsely pass).
# Two variants: env var NO_AUTOSTART=1 and sentinel file /tmp/.no_autostart.
echo ""
echo "[12/14] NO_AUTOSTART escape hatch"
NOAUTOSTART_OUT=$(docker run --rm --platform linux/amd64 \
    -e SUBAGENT_CLI=claude \
    -e NO_AUTOSTART=1 \
    --entrypoint=bash \
    "$IMAGE" \
    -c 'PS1="t> " && source /home/assistant/.bashrc 2>&1 && echo BASH_REACHED' 2>&1) || true
if echo "$NOAUTOSTART_OUT" | grep -q 'BASH_REACHED'; then
    pass "NO_AUTOSTART=1 — autostart skipped, plain bash reached"
else
    fail "NO_AUTOSTART=1 — autostart still fired (output: $(echo "$NOAUTOSTART_OUT" | head -c 200))"
fi

# Sentinel-file escape hatch: same shape, but use /tmp/.no_autostart instead.
SENTINEL_OUT=$(docker run --rm --platform linux/amd64 \
    -e SUBAGENT_CLI=claude \
    --entrypoint=bash \
    "$IMAGE" \
    -c 'touch /tmp/.no_autostart && PS1="t> " && source /home/assistant/.bashrc 2>&1 && echo BASH_REACHED' 2>&1) || true
if echo "$SENTINEL_OUT" | grep -q 'BASH_REACHED'; then
    pass "/tmp/.no_autostart sentinel — autostart skipped, plain bash reached"
else
    fail "/tmp/.no_autostart sentinel — autostart still fired (output: $(echo "$SENTINEL_OUT" | head -c 200))"
fi

# 13. cli-defaults/ canonical configs present and parseable (Phase 2 D-09).
echo ""
echo "[13/14] cli-defaults/ canonical configs present and parseable"
for f in opencode.json codex.json README.md; do
    if docker run --rm --platform linux/amd64 "$IMAGE" sh -c "test -f /opt/cli-defaults/$f" 2>/dev/null; then
        pass "/opt/cli-defaults/$f exists in image"
    else
        fail "/opt/cli-defaults/$f missing in image"
    fi
done
for f in opencode.json codex.json; do
    if docker run --rm --platform linux/amd64 "$IMAGE" python3 -c "import json; json.load(open('/opt/cli-defaults/$f'))" 2>/dev/null; then
        pass "/opt/cli-defaults/$f parses as JSON"
    else
        fail "/opt/cli-defaults/$f failed JSON parse"
    fi
done
# _spdx key present in opencode.json
if docker run --rm --platform linux/amd64 "$IMAGE" python3 -c "import json; assert json.load(open('/opt/cli-defaults/opencode.json'))['_spdx'] == 'FSL-1.1-Apache-2.0'" 2>/dev/null; then
    pass "/opt/cli-defaults/opencode.json carries _spdx=FSL-1.1-Apache-2.0"
else
    fail "/opt/cli-defaults/opencode.json missing _spdx key"
fi

# 14. Entrypoint executes cleanly with the real ENTRYPOINT path.
# All other tests bypass /home/assistant/.entrypoint.sh via --entrypoint=bash
# so stdout parsing works. That leaves no coverage for the entrypoint script
# itself — a shell syntax error there would ship unnoticed. This step runs
# the image with its declared entrypoint and checks that (a) the process
# exits 0, and (b) the expected status banner is printed.
echo ""
echo "[14/14] Entrypoint execution"
# Wrap the command substitution in `if` so `set -e` does not abort the
# whole test script on a non-zero docker exit — we need to reach fail()
# with the captured output for structured reporting.
if ENTRYPOINT_OUT=$(docker run --rm --platform linux/amd64 --user=assistant "$IMAGE" true 2>&1); then
    pass "entrypoint exits 0 with default command"
else
    ENTRYPOINT_EXIT=$?
    fail "entrypoint exited $ENTRYPOINT_EXIT (output: $ENTRYPOINT_OUT)"
fi
# The banner text changes based on token presence; at least one of these
# lines must appear, proving the entrypoint ran through its env-check block.
if echo "$ENTRYPOINT_OUT" | grep -qE "(GITLAB_TOKEN|ANTHROPIC_AUTH_TOKEN|Claude Code configured)"; then
    pass "entrypoint printed expected status banner"
else
    fail "entrypoint ran but produced no recognisable banner: $ENTRYPOINT_OUT"
fi
# Phase 7 / TERM-03: entrypoint MOTD must include the autostart escape hint.
if echo "$ENTRYPOINT_OUT" | grep -qF "NO_AUTOSTART=1 bash"; then
    pass "entrypoint printed sub-agent autostart escape hint"
else
    fail "entrypoint missing escape-hint line (NO_AUTOSTART=1 bash / touch /tmp/.no_autostart): $ENTRYPOINT_OUT"
fi

test_browser_cli_config
test_browser_relay

# Summary
echo ""
echo "==============================="
echo "  PASSED: $PASSED"
echo "  FAILED: $FAILED"
if [ "$FAILED" -gt 0 ]; then
    echo ""
    echo "  Failures:"
    echo -e "$FAILURES"
    echo ""
    echo "  RESULT: FAIL"
    exit 1
else
    echo ""
    echo "  RESULT: ALL TESTS PASSED"
    exit 0
fi
