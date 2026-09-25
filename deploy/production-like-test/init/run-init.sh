#!/bin/bash
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# One-shot Open WebUI bootstrap wrapper for the production-like test deployment.
# It does not expose the upstream sub_agent capability until a fully internal
# sub-agent runtime is available.
set -euo pipefail

source_root="${OCU_INIT_SOURCE_ROOT:-/app/source}"
work_root=$(mktemp -d "${TMPDIR:-/tmp}/ocu-init.XXXXXX")

cleanup() {
    rm -rf "$work_root"
}
trap cleanup EXIT


mkdir -p "$work_root/tools" "$work_root/functions"

python3 - "$source_root" "$work_root" <<'PY'
from pathlib import Path
import os
import sys

source_root = Path(sys.argv[1])
work_root = Path(sys.argv[2])
primary_model = os.environ.get("PRIMARY_CHAT_MODEL", "").strip()
if not primary_model:
    raise SystemExit("refusing to bootstrap: PRIMARY_CHAT_MODEL is required")

tool_source = (source_root / "tools" / "computer_use_tools.py").read_text(encoding="utf-8")
method_start = tool_source.find("    async def sub_agent(\n")
method_end = tool_source.find(
    "\n\n# ============================================================================\n# File sync helper",
    method_start,
)
if method_start < 0 or method_end < 0:
    raise SystemExit("refusing to bootstrap: could not locate the upstream sub_agent method")
(work_root / "tools" / "computer_use_tools.py").write_text(
    tool_source[:method_start] + tool_source[method_end:], encoding="utf-8"
)

init_source = (source_root / "init.sh").read_text(encoding="utf-8")

# Never let the upstream initializer select the first arbitrary model returned
# by a provider. It must validate the explicitly approved deployment model and
# use it both for the Computer Use workspace model and as WebUI's default.
default_models_line = "cfg.setdefault('DEFAULT_MODELS', cfg.get('DEFAULT_MODELS') or '')"
if init_source.count(default_models_line) != 1:
    raise SystemExit("refusing to bootstrap: expected DEFAULT_MODELS config line changed upstream")
safe_init = init_source.replace(
    default_models_line,
    f"cfg['DEFAULT_MODELS'] = {primary_model!r}",
)
selection_start = safe_init.find(
    "# Also try setting via workspace model (fallback for v0.8.11–0.8.12)\n"
)
selection_end = safe_init.find(
    '\n\nif [ -n "$FIRST_MODEL" ]; then',
    selection_start,
)
if selection_start < 0 or selection_end < 0:
    raise SystemExit("refusing to bootstrap: could not locate upstream workspace-model selection")
selected_model_block = '''# Create a workspace model only for the explicitly approved provider model.
FIRST_MODEL=$(curl -sf "$WEBUI_URL/api/models" -H "$AUTH" 2>/dev/null | \\
    PRIMARY_CHAT_MODEL="$PRIMARY_CHAT_MODEL" python3 -c "
import json, os, sys
target = os.environ['PRIMARY_CHAT_MODEL']
data = json.load(sys.stdin).get('data', [])
if not any(model.get('id') == target for model in data):
    raise SystemExit(f'requested PRIMARY_CHAT_MODEL is unavailable: {target}')
print(target)
" 2>/dev/null) || {
    echo "[init] ERROR: PRIMARY_CHAT_MODEL is unavailable: $PRIMARY_CHAT_MODEL" >&2
    exit 1
}
'''
safe_init = safe_init[:selection_start] + selected_model_block + safe_init[selection_end:]

# The workspace model carries the Computer Use tool/filter metadata, so it
# must be visible to the same internal users who can read the public tool.
# Without these grants, non-admin requests resolve the model as "not found".
marker_section = "\n\n# Mark as initialized — only if every required step succeeded."
if safe_init.count(marker_section) != 1:
    raise SystemExit("refusing to bootstrap: could not locate initialization marker section")
workspace_model_access_block = '''
# Make the approved Computer Use workspace model visible to all internal users.
if [ -n "$FIRST_MODEL" ]; then
    if curl -sf -X POST "$WEBUI_URL/api/v1/models/model/access/update" \\
        -H "$AUTH" -H "Content-Type: application/json" \\
        -d "{\\"id\\":\\"$FIRST_MODEL\\",\\"access_grants\\":[
               {\\"principal_type\\":\\"group\\",\\"principal_id\\":\\"*\\",\\"permission\\":\\"read\\"},
               {\\"principal_type\\":\\"user\\",\\"principal_id\\":\\"*\\",\\"permission\\":\\"read\\"}
             ]}" >/dev/null 2>&1; then
        echo "[init] Workspace model marked public (all users + all groups, read)."
    else
        echo "[init] ERROR: Could not set workspace model public access. Init will retry on next restart."
        INIT_FAILED=1
    fi
fi
'''
safe_init = safe_init.replace(marker_section, workspace_model_access_block + marker_section)
(work_root / "init.sh").write_text(safe_init, encoding="utf-8")
(work_root / "functions" / "computer_link_filter.py").write_bytes(
    (source_root / "functions" / "computer_link_filter.py").read_bytes()
)
PY


/bin/bash "$work_root/init.sh"
