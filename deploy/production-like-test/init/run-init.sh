#!/bin/bash
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# One-shot Open WebUI bootstrap wrapper for the production-like test deployment.
# It installs the shipped Computer Use Tool whole; its sub_agent runs Claude Code
# through the sandbox Messages ingress with the calling user's own token (#662).
set -euo pipefail
set -m

source_root="${OCU_INIT_SOURCE_ROOT:-/app/source}"
work_root=""
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
    if [[ -n "${work_root:-}" && -d "$work_root" ]]; then
        rm -rf "$work_root"
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

work_root=$(mktemp -d "${TMPDIR:-/tmp}/ocu-init.XXXXXX")
mkdir -p "$work_root/tools" "$work_root/functions"

python3 - "$source_root" "$work_root" <<'PY' &
from pathlib import Path
import os
import sys

source_root = Path(sys.argv[1])
work_root = Path(sys.argv[2])
primary_model = os.environ.get("PRIMARY_CHAT_MODEL", "").strip()

(work_root / "tools" / "computer_use_tools.py").write_bytes(
    (source_root / "tools" / "computer_use_tools.py").read_bytes()
)

init_source = (source_root / "init.sh").read_text(encoding="utf-8")

# WebUI's default model is the deployment model the operator configured. The
# Computer Use tool reaches every model through the global filter, so no model
# is created here. Both anchors are checked whether or not a model is set, so
# upstream drift fails loud in either mode.
default_models_line = "cfg.setdefault('DEFAULT_MODELS', cfg.get('DEFAULT_MODELS') or '')"
if init_source.count(default_models_line) != 1:
    raise SystemExit("refusing to bootstrap: expected DEFAULT_MODELS config line changed upstream")
marker_section = "\n\n# Mark as initialized — only if every required step succeeded."
if init_source.count(marker_section) != 1:
    raise SystemExit("refusing to bootstrap: could not locate initialization marker section")

# Validate the configured model against /api/models after DEFAULT_MODELS is
# posted and before the marker is written, so a bad value fails loud and init
# stays re-runnable.
primary_model_check = '''
# Refuse to finish unless the approved deployment model is offered by the provider.
curl -sf "$WEBUI_URL/api/models" -H "$AUTH" 2>/dev/null | \\
    PRIMARY_CHAT_MODEL="$PRIMARY_CHAT_MODEL" python3 -c "
import json, os, sys
target = os.environ['PRIMARY_CHAT_MODEL']
data = json.load(sys.stdin).get('data', [])
if not any(model.get('id') == target for model in data):
    raise SystemExit(f'requested PRIMARY_CHAT_MODEL is unavailable: {target}')
" 2>/dev/null || {
    echo "[init] ERROR: PRIMARY_CHAT_MODEL is unavailable: $PRIMARY_CHAT_MODEL" >&2
    exit 1
}
'''
if primary_model:
    safe_init = init_source.replace(
        default_models_line,
        f"cfg['DEFAULT_MODELS'] = {primary_model!r}",
    ).replace(marker_section, primary_model_check + marker_section)
else:
    # Upstream keeps the stored DEFAULT_MODELS, so an admin's choice survives.
    print("[init] PRIMARY_CHAT_MODEL is not set; leaving the default model to the admin UI")
    safe_init = init_source
(work_root / "init.sh").write_text(safe_init, encoding="utf-8")
(work_root / "functions" / "computer_link_filter.py").write_bytes(
    (source_root / "functions" / "computer_link_filter.py").read_bytes()
)
PY
OWNED_PID=$!
wait_owned "$OWNED_PID"

run_owned /bin/bash "$work_root/init.sh"
