#!/bin/bash
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
# Open WebUI initialization script
#
# Waits for Open WebUI to be ready, then installs tools, functions, and
# configures Valves from env. Guarded by a marker file — runs once, skips on
# subsequent container starts so user edits in the Open WebUI admin UI are
# never clobbered on restart.
#
# Valves are env-seeded on FIRST boot only. ORCHESTRATOR_URL is persisted into
# the Tool and Filter Valves; MCP_API_KEY is persisted only into the Tool Valve.
# OCU_INTERNAL_TOKEN stays in the Open WebUI server environment and the Tool reads
# it directly on every call; it is not a Valve and this script never writes it to
# the persistent Valve payload. PUBLIC_BASE_URL lives on the computer-use-server
# container. To force an ORCHESTRATOR_URL Valve re-seed (e.g. after changing it
# in .env), delete the marker file and
# restart this container:
#
#   docker compose -f docker-compose.webui.yml exec open-webui \
#       rm /app/backend/data/.computer-use-initialized
#   docker compose -f docker-compose.webui.yml restart open-webui

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEBUI_URL="${WEBUI_URL:-http://localhost:8080}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@open-computer-use.dev}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin}"
ADMIN_NAME="${ADMIN_NAME:-Admin}"
# ORCHESTRATOR_URL: internal URL of computer-use-server, reachable from inside
# the open-webui container. Seeded into both Tool and Filter Valves. The public
# URL (browser-facing) is NOT set here — it lives only on the server as the
# PUBLIC_BASE_URL env var and is delivered to the filter via response header.
# OCU_INTERNAL_TOKEN remains an environment-only tool credential, read on every call.
ORCHESTRATOR_URL="${ORCHESTRATOR_URL:-http://computer-use-server:8081}"
MCP_API_KEY="${MCP_API_KEY:-}"
MARKER_FILE="${MARKER_FILE:-/app/backend/data/.computer-use-initialized}"
export TOOL_SOURCE="${SCRIPT_DIR}/tools/computer_use_tools.py"
export FILTER_SOURCE="${SCRIPT_DIR}/functions/computer_link_filter.py"
# The video switch. Its id is VIDEO_FILTER_ID in computer_link_filter.py.
VIDEO_FILTER_ID="video_production_filter"
VIDEO_FILTER_SOURCE="${SCRIPT_DIR}/functions/video_production_filter.py"

# Sanity checks — run EVERY start (before marker-gate), so stale-default
# warnings resurface on each restart until the user fixes them.
if [[ "$ADMIN_PASSWORD" == "admin" || "$ADMIN_PASSWORD" == "change-me" ]]; then
    echo "[init] WARNING: ADMIN_PASSWORD is still the default — change it for anything beyond local dev."
fi
if [[ -z "$MCP_API_KEY" ]]; then
    echo "[init] WARNING: MCP_API_KEY is empty — /mcp still requires OCU_INTERNAL_TOKEN, but has no second Bearer credential. Set it for defense in depth."
fi

# Skip if already initialized
if [ -f "$MARKER_FILE" ]; then
    echo "[init] Already initialized, skipping."
    echo "[init] To re-seed ORCHESTRATOR_URL and MCP_API_KEY Valves from env, delete $MARKER_FILE and restart the container."
    exit 0
fi

echo "[init] Waiting for Open WebUI to be ready..."
ready=0
READY_ATTEMPTS="${OCU_INIT_READY_ATTEMPTS:-60}"
READY_SLEEP="${OCU_INIT_READY_SLEEP:-2}"
for i in $(seq 1 "$READY_ATTEMPTS"); do
    if curl -sf "$WEBUI_URL/api/version" >/dev/null 2>&1; then
        echo "[init] Open WebUI is ready."
        ready=1
        break
    fi
    sleep "$READY_SLEEP"
done
if [ "$ready" != "1" ]; then
    echo "[init] ERROR: Open WebUI did not become ready." >&2
    exit 1
fi

# Check if any users exist
USERS=$(curl -sf "$WEBUI_URL/api/v1/auths/signin" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}" 2>/dev/null || echo "")

if echo "$USERS" | python3 -c "import sys,json; json.load(sys.stdin)['token']" 2>/dev/null; then
    TOKEN=$(echo "$USERS" | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")
    echo "[init] Logged in as existing admin."
else
    # Try to create first user (becomes admin automatically)
    SIGNUP=$(curl -sf "$WEBUI_URL/api/v1/auths/signup" \
        -H "Content-Type: application/json" \
        -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\",\"name\":\"$ADMIN_NAME\"}" 2>/dev/null || echo "")

    if echo "$SIGNUP" | python3 -c "import sys,json; json.load(sys.stdin)['token']" 2>/dev/null; then
        TOKEN=$(echo "$SIGNUP" | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")
        echo "[init] Created admin user."
    else
        echo "[init] ERROR: Could not create or login as admin." >&2
        exit 1
    fi
fi

AUTH="Authorization: Bearer $TOKEN"

# install_function ID NAME SOURCE DESCRIPTION: create the Open WebUI function,
# or update it when it exists, from a shipped source file. A failed write stops
# init before the marker is written.
install_function() {
    local id=$1 name=$2 source=$3 description=$4 payload existing action url
    payload=$(FUNCTION_ID="$id" FUNCTION_NAME="$name" FUNCTION_SOURCE="$source" \
        FUNCTION_DESCRIPTION="$description" python3 -c "
import json, os
with open(os.environ['FUNCTION_SOURCE'], encoding='utf-8') as source:
    code = source.read()
print(json.dumps({
    'id': os.environ['FUNCTION_ID'],
    'name': os.environ['FUNCTION_NAME'],
    'content': code,
    'meta': {'description': os.environ['FUNCTION_DESCRIPTION']},
}))
")
    existing=$(curl -sf "$WEBUI_URL/api/v1/functions/id/$id" -H "$AUTH" 2>/dev/null || echo "")
    if [ -n "$existing" ] && echo "$existing" | python3 -c "import sys,json; json.load(sys.stdin)['id']" 2>/dev/null; then
        action=updated
        url="$WEBUI_URL/api/v1/functions/id/$id/update"
    else
        action=created
        url="$WEBUI_URL/api/v1/functions/create"
    fi
    if ! curl -sf -X POST "$url" -H "$AUTH" -H "Content-Type: application/json" \
        -d "$payload" >/dev/null; then
        echo "[init] ERROR: Could not install function $id. Init will retry on next restart." >&2
        exit 1
    fi
    echo "[init] Function $action: $id"
}

# enable_global_function ID: make the function active (is_active) and global
# (is_global). Open WebUI has TWO flip endpoints, /toggle and /toggle/global;
# active-but-not-global is silently inert — the filter loads but is never
# applied to chats. Read the state first and flip only what is off, so re-runs
# are idempotent. A failure sets INIT_FAILED, so the marker is not written.
enable_global_function() {
    local id=$1 state flags
    if ! state=$(curl -sf "$WEBUI_URL/api/v1/functions/id/$id" -H "$AUTH" 2>/dev/null) ||
        ! flags=$(printf '%s' "$state" | python3 -c "
import json, sys
function = json.load(sys.stdin)
print(function.get('is_active') is True, function.get('is_global') is True)
" 2>/dev/null); then
        echo "[init] ERROR: Could not read the state of function $id. Init will retry on next restart." >&2
        INIT_FAILED=1
        return 0
    fi
    if [ "${flags% *}" != "True" ]; then
        if curl -sf -X POST "$WEBUI_URL/api/v1/functions/id/$id/toggle" -H "$AUTH" >/dev/null 2>&1; then
            echo "[init] Function activated: $id"
        else
            echo "[init] ERROR: Could not activate function $id — it stays disabled. Init will retry on next restart." >&2
            INIT_FAILED=1
        fi
    else
        echo "[init] Function already active: $id"
    fi
    if [ "${flags#* }" != "True" ]; then
        if curl -sf -X POST "$WEBUI_URL/api/v1/functions/id/$id/toggle/global" -H "$AUTH" >/dev/null 2>&1; then
            echo "[init] Function marked global (applies to all chats): $id"
        else
            echo "[init] ERROR: Could not mark function $id global — it won't apply to chats. Init will retry on next restart." >&2
            INIT_FAILED=1
        fi
    else
        echo "[init] Function already global: $id"
    fi
}

# Install tool: computer_use_tools.py
echo "[init] Installing Computer Use tool..."
TOOL_PAYLOAD=$(python3 -c "
import json, os
code = open(os.environ['TOOL_SOURCE']).read()
print(json.dumps({
    'id': 'ai_computer_use',
    'name': 'Computer Use Tools',
    'content': code,
    'meta': {'description': 'Execute commands, create files, and delegate tasks in isolated Docker containers.'}
}))
")

# Check if tool already exists
EXISTING=$(curl -sf "$WEBUI_URL/api/v1/tools/id/ai_computer_use" -H "$AUTH" 2>/dev/null || echo "")
if [ -n "$EXISTING" ] && echo "$EXISTING" | python3 -c "import sys,json; json.load(sys.stdin)['id']" 2>/dev/null; then
    # Update existing tool
    curl -sf -X POST "$WEBUI_URL/api/v1/tools/id/ai_computer_use/update" \
        -H "$AUTH" -H "Content-Type: application/json" \
        -d "$TOOL_PAYLOAD" >/dev/null
    echo "[init] Tool updated: ai_computer_use"
else
    # Create new tool
    curl -sf -X POST "$WEBUI_URL/api/v1/tools/create" \
        -H "$AUTH" -H "Content-Type: application/json" \
        -d "$TOOL_PAYLOAD" >/dev/null
    echo "[init] Tool created: ai_computer_use"
fi

# Set tool valves
echo "[init] Configuring tool valves..."
curl -sf -X POST "$WEBUI_URL/api/v1/tools/id/ai_computer_use/valves/update" \
    -H "$AUTH" -H "Content-Type: application/json" \
    -d "{\"ORCHESTRATOR_URL\": \"$ORCHESTRATOR_URL\", \"MCP_API_KEY\": \"$MCP_API_KEY\", \"DEBUG_LOGGING\": false}" >/dev/null
echo "[init] Tool valves set: ORCHESTRATOR_URL=$ORCHESTRATOR_URL"

# Make tool public-read so non-admin users can see & call it.
# Open WebUI's UI "Public" toggle writes BOTH group:* and user:* wildcards — we mirror
# that exactly. Without these grants, only the admin who created the tool sees it, so
# a failure here must block the marker file — otherwise the next restart skips init
# and the tool stays admin-only forever.
INIT_FAILED=0
if curl -sf -X POST "$WEBUI_URL/api/v1/tools/id/ai_computer_use/access/update" \
    -H "$AUTH" -H "Content-Type: application/json" \
    -d '{"access_grants":[
           {"principal_type":"group","principal_id":"*","permission":"read"},
           {"principal_type":"user","principal_id":"*","permission":"read"}
         ]}' >/dev/null 2>&1; then
    echo "[init] Tool marked public (all users + all groups, read)."
else
    echo "[init] ERROR: Could not set tool public access — tool will remain admin-only. Init will retry on next restart."
    INIT_FAILED=1
fi

# Install function: computer_link_filter.py
echo "[init] Installing Computer Use filter..."
install_function computer_use_filter "Computer Use Filter" "$FILTER_SOURCE" \
    "Injects system prompt with file URLs and adds archive download button."

# Configure filter valves. ORCHESTRATOR_URL is the internal URL used for
# server→server fetch of /system-prompt. The public URL (for browser-facing
# iframe/archive links) lives only on the server as PUBLIC_BASE_URL env and is
# returned to the filter via the X-Public-Base-URL response header — no Valve
# for it here.
echo "[init] Configuring filter valves..."
if curl -sf -X POST "$WEBUI_URL/api/v1/functions/id/computer_use_filter/valves/update" \
    -H "$AUTH" -H "Content-Type: application/json" \
    -d "{\"ORCHESTRATOR_URL\": \"$ORCHESTRATOR_URL\", \"ARCHIVE_BUTTON\": \"on\", \"INJECT_SYSTEM_PROMPT\": true}" >/dev/null 2>&1; then
    echo "[init] Filter valves set: ORCHESTRATOR_URL=$ORCHESTRATOR_URL"
else
    echo "[init] ERROR: Could not seed filter valves — ORCHESTRATOR_URL will fall back to the code default until the next successful init. Init will retry on next restart."
    INIT_FAILED=1
fi

enable_global_function computer_use_filter

# Install the video switch: an active, global toggle filter appears in every
# model's integrations menu, off by default. The Computer Use filter reads the
# switch from the request and asks for the remotion skill.
echo "[init] Installing video switch filter..."
install_function "$VIDEO_FILTER_ID" "视频制作" "$VIDEO_FILTER_SOURCE" \
    "Offers the remotion video skill for the next message."
enable_global_function "$VIDEO_FILTER_ID"

# Set global DEFAULT_MODEL_PARAMS so every model uses Native Function Calling + streaming
# without per-model Advanced Params clicks. The old /api/v1/configs/models/default/update
# endpoint does not exist in Open WebUI v0.8.12 (returns 405) — use POST /api/v1/configs/models
# which takes the full ModelsConfigForm and merges DEFAULT_MODEL_PARAMS. We preserve
# existing fields so we don't clobber DEFAULT_MODELS / DEFAULT_MODEL_METADATA.
# A failed read, parse, or post must not write the marker or replace an unread
# configuration with an empty object.
echo "[init] Ensuring DEFAULT_MODEL_PARAMS has native function calling + streaming..."
# Fetch current config; pipe its body into Python via stdin so arbitrary content
# (quotes, newlines, triple-quote sequences) cannot break the interpolation.
# Use direct assignment, not setdefault — any prior value for these two fields
# must be overwritten so our "params enforced" log is truthful.
if MODELS_CFG=$(curl -sf "$WEBUI_URL/api/v1/configs/models" -H "$AUTH"); then
    if MERGED_CFG=$(printf '%s' "$MODELS_CFG" | python3 -c "
import json, sys
raw = sys.stdin.read()
if not raw.strip():
    raise SystemExit('empty models configuration')
cfg = json.loads(raw)
if not isinstance(cfg, dict):
    raise SystemExit('models configuration is not an object')
params = cfg.get('DEFAULT_MODEL_PARAMS')
if not isinstance(params, dict):
    params = {}
else:
    params = dict(params)
params['function_calling'] = 'native'
params['stream_response'] = True
cfg['DEFAULT_MODEL_PARAMS'] = params
cfg.setdefault('DEFAULT_MODELS', cfg.get('DEFAULT_MODELS') or '')
cfg.setdefault('DEFAULT_PINNED_MODELS', cfg.get('DEFAULT_PINNED_MODELS') or '')
cfg.setdefault('MODEL_ORDER_LIST', cfg.get('MODEL_ORDER_LIST') or [])
cfg.setdefault('DEFAULT_MODEL_METADATA', cfg.get('DEFAULT_MODEL_METADATA') or {})
print(json.dumps(cfg))
"); then
        if curl -sf -X POST "$WEBUI_URL/api/v1/configs/models" \
            -H "$AUTH" -H "Content-Type: application/json" \
            -d "$MERGED_CFG" >/dev/null; then
            echo "[init] DEFAULT_MODEL_PARAMS set (function_calling=native, stream_response=true)."
        else
            echo "[init] ERROR: Could not POST DEFAULT_MODEL_PARAMS. Init will retry on next restart." >&2
            INIT_FAILED=1
        fi
    else
        echo "[init] ERROR: Could not parse models configuration. Init will retry on next restart." >&2
        INIT_FAILED=1
    fi
else
    echo "[init] ERROR: Could not read models configuration. Init will retry on next restart." >&2
    INIT_FAILED=1
fi

# Mark as initialized — only if every required step succeeded. If INIT_FAILED=1,
# leave the marker off so the next container start retries the failed steps
# (public-access grant, filter valves, filter states, models configuration).
# Without this guard a transient failure would be baked in forever.
if [ "$INIT_FAILED" = "0" ]; then
    touch "$MARKER_FILE"
    echo "[init] Done! Open WebUI is ready with Computer Use."
else
    echo "[init] ERROR: required setup failed — marker not written." >&2
    exit 1
fi
