#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
# Resolve privately, preflight, provision, start upstream applications then proxy.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OVERLAY="$ROOT/deploy/production-like-test"
CONFIG_DIR=""

cleanup() {
    if [[ -n "$CONFIG_DIR" && -d "$CONFIG_DIR" ]]; then
        rm -rf "$CONFIG_DIR"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

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

CONFIG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ocu-deploy-config.XXXXXX")"
core_files=(-p "$PROJECT" -f "$ROOT/docker-compose.yml" -f "$OVERLAY/compose.core.override.yml")
webui_files=(-p "$PROJECT" -f "$ROOT/docker-compose.webui.yml" -f "$OVERLAY/compose.webui.override.yml")
proxy_files=(-p "$PROJECT" -f "$OVERLAY/compose.proxy.yml")

resolve() {
    local dest=$1
    shift
    if ! docker compose "$@" config --format json >"$dest"; then
        printf '%s\n' 'deploy: compose config failed' >&2
        exit 1
    fi
}
resolve "$CONFIG_DIR/core.json" "${core_files[@]}"
resolve "$CONFIG_DIR/webui.json" "${webui_files[@]}"
resolve "$CONFIG_DIR/proxy.json" "${proxy_files[@]}"

if ! bash "$ROOT/deploy/check-ports.sh" \
    "$CONFIG_DIR/core.json" "$CONFIG_DIR/webui.json" "$CONFIG_DIR/proxy.json"; then
    printf '%s\n' 'deploy: publication check failed' >&2
    exit 1
fi
if ! bash "$ROOT/deploy/provision-networks.sh"; then
    printf '%s\n' 'deploy: network provisioning failed' >&2
    exit 1
fi

# Do not remove orphans: the three invocations share one Compose project.
docker compose "${core_files[@]}" up -d --build
docker compose "${webui_files[@]}" up -d --build
docker compose "${proxy_files[@]}" up -d --build
