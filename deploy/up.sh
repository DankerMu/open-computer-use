#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
# Resolve privately, preflight, provision, start upstream applications then proxy.
set -euo pipefail
set -m
umask 077

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OVERLAY="$ROOT/deploy/production-like-test"
CONFIG_DIR=""
OWNED_PID=""

group_alive() {
    local pid=$1
    kill -0 -- "-$pid" 2>/dev/null || kill -0 "$pid" 2>/dev/null
}

stop_owned() {
    local pid="${OWNED_PID:-}"
    OWNED_PID=""
    if [[ -z "$pid" ]]; then
        return 0
    fi
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    local waited=0
    while group_alive "$pid" && (( waited < 20 )); do
        sleep 0.1
        waited=$((waited + 1))
    done
    if group_alive "$pid"; then
        kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    fi
    wait "$pid" 2>/dev/null || true
}

cleanup() {
    local status=$?
    trap '' INT TERM HUP
    stop_owned || true
    if [[ -n "${CONFIG_DIR:-}" && -d "$CONFIG_DIR" ]]; then
        rm -rf "$CONFIG_DIR"
    fi
    return "$status"
}

interrupt() {
    local status=$1
    trap '' INT TERM HUP
    trap - EXIT
    cleanup || true
    exit "$status"
}

trap cleanup EXIT
trap 'interrupt 130' INT
trap 'interrupt 143' TERM
trap 'interrupt 129' HUP

require() {
    local name=$1
    if [[ -z "${!name:-}" ]]; then
        printf '%s\n' "deploy: ${name} is required" >&2
        exit 1
    fi
    export "$name"
}

wait_owned() {
    local pid=$1
    local status=0
    wait "$pid" || status=$?
    if [[ "${OWNED_PID:-}" == "$pid" ]]; then
        if group_alive "$pid"; then
            stop_owned || true
        else
            OWNED_PID=""
        fi
    fi
    return "$status"
}

run_owned() {
    "$@" &
    OWNED_PID=$!
    wait_owned "$OWNED_PID"
}

for name in COMPOSE_PROJECT_NAME OCU_PRIVATE_NETWORK OCU_PRIVATE_SUBNET OCU_PRIVATE_GATEWAY \
    OCU_SANDBOX_NETWORK OCU_SANDBOX_SUBNET OCU_SANDBOX_GATEWAY OCU_PROXY_PORT \
    OCU_INTERNAL_TOKEN OCU_WEBUI_ORIGIN OCU_WEBUI_AUTH_URL PUBLIC_BASE_URL OCU_PROXY_IMAGE; do
    require "$name"
done
if ! declare -p OCU_SANDBOX_EGRESS_ALLOW >/dev/null 2>&1; then
    printf '%s\n' 'deploy: OCU_SANDBOX_EGRESS_ALLOW is unset' >&2
    exit 1
fi
export OCU_SANDBOX_EGRESS_ALLOW
if ! declare -p OCU_SANDBOX_DNS >/dev/null 2>&1; then
    printf '%s\n' 'deploy: OCU_SANDBOX_DNS is unset' >&2
    exit 1
fi
export OCU_SANDBOX_DNS
PROJECT="$COMPOSE_PROJECT_NAME"
# Shared-project siblings must survive later ups; the cleanup profile must stay off.
export COMPOSE_REMOVE_ORPHANS=false
export COMPOSE_PROFILES=""

if [[ -z "${OCU_RELEASE_MANIFEST:-}" ]]; then
    printf '%s\n' 'deploy: OCU_RELEASE_MANIFEST is required' >&2
    exit 1
fi
for name in DOCUMENTSERVER_IMAGE OCU_OFFICE_JWT_SECRET OCU_OFFICE_DOCSERVER_URL \
    OCU_OFFICE_DOCSERVER_ORIGIN OCU_OFFICE_SELF_URL OCU_OFFICE_PROXY_PORT \
    OCU_OFFICE_FONTS_DIR ENABLE_OCU_OFFICE_EDIT; do
    require "$name"
done
export PYTHONPATH="$ROOT/deploy${PYTHONPATH:+:$PYTHONPATH}"
run_owned python3 - <<'PY'
import os
from settings import origin, port

if os.environ["ENABLE_OCU_OFFICE_EDIT"] not in {"true", "false"}:
    raise SystemExit("deploy: ENABLE_OCU_OFFICE_EDIT must be true or false")
port("OCU_OFFICE_PROXY_PORT", os.environ["OCU_OFFICE_PROXY_PORT"])
origin("OCU_OFFICE_DOCSERVER_ORIGIN", os.environ["OCU_OFFICE_DOCSERVER_ORIGIN"])
PY
# Compose's project directory must not change the source checked from this cwd.
if [[ "$OCU_OFFICE_FONTS_DIR" != /* ]]; then
    export OCU_OFFICE_FONTS_DIR="$PWD/$OCU_OFFICE_FONTS_DIR"
fi
if [[ ! -d "$OCU_OFFICE_FONTS_DIR" ]]; then
    printf '%s\n' 'deploy: OCU_OFFICE_FONTS_DIR must be an existing directory' >&2
    exit 1
fi
CONFIG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ocu-deploy-config.XXXXXX")"
: >"$CONFIG_DIR/empty.env"
if ! run_owned python3 - "$ROOT" "$OCU_RELEASE_MANIFEST" >"$CONFIG_DIR/release-fonts" <<'PY'
from __future__ import annotations

import os
import sys
from pathlib import Path

import release


root = Path(sys.argv[1])
try:
    payload = release.load_inventory(Path(sys.argv[2]))
    runtime = dict(os.environ)
    release.verify_runtime_binding(payload, runtime)
    release.verify_tracked_source(root, payload["ocu_source_sha"])
    fonts = release.verify_release_fonts(Path(sys.argv[2]), root)
    release.verify_local_images(payload)
    print(fonts, end="\0")
except release.ReleaseError as exc:
    print(f"deploy: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
then
    printf '%s\n' 'deploy: release inventory verification failed' >&2
    exit 1
fi
IFS= read -r -d '' OCU_RELEASE_FONTS_DIR <"$CONFIG_DIR/release-fonts"
export OCU_RELEASE_FONTS_DIR



core_files=(-p "$PROJECT" --project-directory "$ROOT" -f "$ROOT/docker-compose.yml" -f "$OVERLAY/compose.core.override.yml")
webui_files=(-p "$PROJECT" --project-directory "$ROOT" -f "$ROOT/docker-compose.webui.yml" -f "$OVERLAY/compose.webui.override.yml")
proxy_files=(-p "$PROJECT" --project-directory "$OVERLAY" -f "$OVERLAY/compose.proxy.yml")

resolve() {
    local dest=$1
    shift
    if ! run_owned docker compose "$@" config --format json >"$dest"; then
        printf '%s\n' 'deploy: compose config failed' >&2
        return 1
    fi
}

freeze() {
    python3 - "$1" "$2" <<'PY' &
from __future__ import annotations

import json
import sys
from pathlib import Path


def escape_dollars(value):
    if isinstance(value, str):
        return value.replace("$", "$$")
    if isinstance(value, list):
        return [escape_dollars(item) for item in value]
    if isinstance(value, dict):
        return {key: escape_dollars(item) for key, item in value.items()}
    return value


source = Path(sys.argv[1])
destination = Path(sys.argv[2])
payload = json.loads(source.read_text(encoding="utf-8"))
destination.write_text(json.dumps(escape_dollars(payload)), encoding="utf-8")
PY
    OWNED_PID=$!
    if ! wait_owned "$OWNED_PID"; then
        printf '%s\n' 'deploy: snapshot freeze failed' >&2
        return 1
    fi
}

resolve "$CONFIG_DIR/core.json" "${core_files[@]}"
resolve "$CONFIG_DIR/webui.json" "${webui_files[@]}"
resolve "$CONFIG_DIR/proxy.json" "${proxy_files[@]}"
freeze "$CONFIG_DIR/core.json" "$CONFIG_DIR/core.up.json"
freeze "$CONFIG_DIR/webui.json" "$CONFIG_DIR/webui.up.json"
freeze "$CONFIG_DIR/proxy.json" "$CONFIG_DIR/proxy.up.json"
if ! run_owned python3 - "$OCU_RELEASE_MANIFEST" "$CONFIG_DIR/core.json" "$CONFIG_DIR/webui.json" "$CONFIG_DIR/proxy.json" <<'PY'
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import release


try:
    payload = release.load_inventory(Path(sys.argv[1]))
    docs = {
        "core.json": json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")),
        "webui.json": json.loads(Path(sys.argv[3]).read_text(encoding="utf-8")),
        "proxy.json": json.loads(Path(sys.argv[4]).read_text(encoding="utf-8")),
    }
    release.verify_service_images(payload, docs)
except release.ReleaseError as cop:
    print(f"deploy: {cop}", file=sys.stderr)
    raise SystemExit(1)
except (OSError, json.JSONDecodeError) as cop:
    print(f"deploy: resolved compose documents are unreadable: {cop}", file=sys.stderr)
    raise SystemExit(1)

services = docs["core.json"]["services"]
documentserver = services.get("documentserver", {}).get("environment") or {}
broker = services.get("computer-use-server", {}).get("environment") or {}
if not isinstance(documentserver, dict) or documentserver.get("JWT_ENABLED") != "true":
    raise SystemExit("deploy: documentserver JWT_ENABLED must be true")
secret = documentserver.get("JWT_SECRET")
if not isinstance(secret, str) or not secret.strip() or secret != os.environ["OCU_OFFICE_JWT_SECRET"]:
    raise SystemExit("deploy: documentserver JWT_SECRET must match OCU_OFFICE_JWT_SECRET")
if not isinstance(broker, dict) or broker.get("OCU_OFFICE_JWT_SECRET") != secret:
    raise SystemExit("deploy: computer-use-server OCU_OFFICE_JWT_SECRET must match JWT_SECRET")
PY
then
    printf '%s\n' 'deploy: resolved service configuration verification failed' >&2
    exit 1
fi


if ! run_owned bash "$ROOT/deploy/check-ports.sh" \
    "$CONFIG_DIR/core.json" "$CONFIG_DIR/webui.json" "$CONFIG_DIR/proxy.json"; then
    printf '%s\n' 'deploy: publication check failed' >&2
    exit 1
fi
if ! run_owned bash "$ROOT/deploy/provision-networks.sh"; then
    printf '%s\n' 'deploy: network provisioning failed' >&2
    exit 1
fi
if ! run_owned bash "$ROOT/deploy/check-sandbox-dns.sh" "$CONFIG_DIR/core.json"; then
    printf '%s\n' 'deploy: sandbox DNS policy check failed' >&2
    exit 1
fi
if ! run_owned bash "$ROOT/deploy/firewall/docker-user-rules.sh"; then
    printf '%s\n' 'deploy: sandbox egress policy installation failed' >&2
    exit 1
fi
if ! run_owned bash "$ROOT/deploy/firewall/check.sh"; then
    printf '%s\n' 'deploy: sandbox egress policy check failed' >&2
    exit 1
fi

start_stack() {
    local project_dir=$1
    local snapshot=$2
    if ! run_owned docker compose -p "$PROJECT" --project-directory "$project_dir" \
        --env-file "$CONFIG_DIR/empty.env" -f "$snapshot" up -d --no-build --pull never; then
        printf '%s\n' "deploy: compose up failed for ${snapshot}" >&2
        return 1
    fi

}

# Execute the already-checked documents, not the mutable source YAML.
start_stack "$ROOT" "$CONFIG_DIR/core.up.json"
start_stack "$ROOT" "$CONFIG_DIR/webui.up.json"
start_stack "$OVERLAY" "$CONFIG_DIR/proxy.up.json"
