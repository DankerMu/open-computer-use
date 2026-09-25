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
COMPOSE_PID=""

cleanup() {
    local status=$?
    if [[ -n "${COMPOSE_PID:-}" ]] && kill -0 "$COMPOSE_PID" 2>/dev/null; then
        kill -TERM -- "-$COMPOSE_PID" 2>/dev/null || kill -TERM "$COMPOSE_PID" 2>/dev/null || true
        local waited=0
        while kill -0 "$COMPOSE_PID" 2>/dev/null && (( waited < 20 )); do
            sleep 0.1
            waited=$((waited + 1))
        done
        if kill -0 "$COMPOSE_PID" 2>/dev/null; then
            kill -KILL -- "-$COMPOSE_PID" 2>/dev/null || kill -KILL "$COMPOSE_PID" 2>/dev/null || true
        fi
        wait "$COMPOSE_PID" 2>/dev/null || true
        COMPOSE_PID=""
    fi
    if [[ -n "${CONFIG_DIR:-}" && -d "$CONFIG_DIR" ]]; then
        rm -rf "$CONFIG_DIR"
    fi
    return "$status"
}

interrupt() {
    local status=$1
    trap - EXIT INT TERM HUP
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

for name in COMPOSE_PROJECT_NAME OCU_PRIVATE_NETWORK OCU_PRIVATE_SUBNET OCU_PRIVATE_GATEWAY \
    OCU_SANDBOX_NETWORK OCU_SANDBOX_SUBNET OCU_SANDBOX_GATEWAY OCU_PROXY_PORT \
    OCU_INTERNAL_TOKEN OCU_WEBUI_ORIGIN OCU_WEBUI_AUTH_URL PUBLIC_BASE_URL OCU_PROXY_IMAGE; do
    require "$name"
done
PROJECT="$COMPOSE_PROJECT_NAME"

# Shared-project siblings must survive later ups; the cleanup profile must stay off.
export COMPOSE_REMOVE_ORPHANS=false
export COMPOSE_PROFILES=""

CONFIG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ocu-deploy-config.XXXXXX")"
: >"$CONFIG_DIR/empty.env"
core_files=(-p "$PROJECT" --project-directory "$ROOT" -f "$ROOT/docker-compose.yml" -f "$OVERLAY/compose.core.override.yml")
webui_files=(-p "$PROJECT" --project-directory "$ROOT" -f "$ROOT/docker-compose.webui.yml" -f "$OVERLAY/compose.webui.override.yml")
proxy_files=(-p "$PROJECT" --project-directory "$OVERLAY" -f "$OVERLAY/compose.proxy.yml")

resolve() {
    local dest=$1
    shift
    if ! docker compose "$@" config --format json >"$dest"; then
        printf '%s\n' 'deploy: compose config failed' >&2
        exit 1
    fi
}

freeze() {
    python3 - "$1" "$2" <<'PY'
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
}

resolve "$CONFIG_DIR/core.json" "${core_files[@]}"
resolve "$CONFIG_DIR/webui.json" "${webui_files[@]}"
resolve "$CONFIG_DIR/proxy.json" "${proxy_files[@]}"
freeze "$CONFIG_DIR/core.json" "$CONFIG_DIR/core.up.json"
freeze "$CONFIG_DIR/webui.json" "$CONFIG_DIR/webui.up.json"
freeze "$CONFIG_DIR/proxy.json" "$CONFIG_DIR/proxy.up.json"

if ! bash "$ROOT/deploy/check-ports.sh" \
    "$CONFIG_DIR/core.json" "$CONFIG_DIR/webui.json" "$CONFIG_DIR/proxy.json"; then
    printf '%s\n' 'deploy: publication check failed' >&2
    exit 1
fi
if ! bash "$ROOT/deploy/provision-networks.sh"; then
    printf '%s\n' 'deploy: network provisioning failed' >&2
    exit 1
fi

start_stack() {
    local project_dir=$1
    local snapshot=$2
    docker compose -p "$PROJECT" --project-directory "$project_dir" \
        --env-file "$CONFIG_DIR/empty.env" -f "$snapshot" up -d --build &
    COMPOSE_PID=$!
    local status=0
    wait "$COMPOSE_PID" || status=$?
    COMPOSE_PID=""
    if [[ "$status" -ne 0 ]]; then
        printf '%s\n' "deploy: compose up failed for ${snapshot}" >&2
        exit "$status"
    fi
}

# Execute the already-checked documents, not the mutable source YAML.
start_stack "$ROOT" "$CONFIG_DIR/core.up.json"
start_stack "$ROOT" "$CONFIG_DIR/webui.up.json"
start_stack "$OVERLAY" "$CONFIG_DIR/proxy.up.json"
