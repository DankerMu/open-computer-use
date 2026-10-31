#!/bin/bash
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Produce a credential-free deployment bill of materials for this exact host.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'run this script as root' >&2
    exit 1
fi

deploy_root=${DEPLOY_ROOT:-/opt/ai-workbench-test/open-computer-use}
runtime_file="$deploy_root/config/runtime.env"
output_file="$deploy_root/DEPLOYED_VERSION.md"

if [ ! -r "$runtime_file" ]; then
    printf '%s\n' 'protected runtime configuration is missing' >&2
    exit 1
fi

env_value() {
    sed -n "s/^$1=//p" "$runtime_file"
}

manifest=${OCU_RELEASE_MANIFEST:-$(env_value OCU_RELEASE_MANIFEST)}
if [ -z "$manifest" ]; then
    printf '%s\n' 'OCU_RELEASE_MANIFEST is required' >&2
    exit 1
fi
# The selected release's own checkout sits next to its manifest; after an
# upgrade it is a separate install root, not $deploy_root/source.
if ! release_root=$(CDPATH='' cd -- "$(dirname -- "$manifest")" 2>/dev/null && pwd -P); then
    printf '%s\n' "release root of $manifest is missing" >&2
    exit 1
fi
manifest="$release_root/$(basename -- "$manifest")"
source_dir="$release_root/source"
if [ ! -d "$source_dir/.git" ]; then
    printf '%s\n' "source checkout is missing: expected $source_dir next to $manifest" >&2
    exit 1
fi
generated_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
tmp_file=$(mktemp "$deploy_root/.DEPLOYED_VERSION.md.XXXXXX")
trap 'rm -f "$tmp_file"' EXIT

{
    printf '%s\n' '# Open WebUI + Open Computer Use deployment version record'
    printf '\nGenerated at (UTC): `%s`\n' "$generated_at"
    printf '\n## Source and runtime images\n\n'
    PYTHONPATH="$source_dir/deploy${PYTHONPATH:+:$PYTHONPATH}" python3 - "$manifest" "$source_dir" "$runtime_file" <<'PY'
import sys
from pathlib import Path
import release


payload = release.load_inventory(Path(sys.argv[1]))
source = Path(sys.argv[2])
runtime = {}
for line in Path(sys.argv[3]).read_text(encoding="utf-8").splitlines():
    if not line or line.startswith("#") or "=" not in line:
        continue
    name, value = line.split("=", 1)
    runtime[name] = value
release.verify_runtime_binding(payload, runtime)
release.verify_tracked_source(source, payload["ocu_source_sha"])
release.require_supported_source_contract(source, payload["source_consumer_contract"])
selected_ids = release.verify_local_images(payload)
sys.stdout.write("\n".join(release.provenance_lines(payload)) + "\n")
for role, record in payload["images"].items():
    print(f"- {role} image selected engine ID: `{record['reference']}` (`{selected_ids[role]}`)")
PY

    printf -- '- Docker engine: `%s`\n' "$(docker version --format '{{.Server.Version}}')"
    printf -- '- Docker Compose: `%s`\n' "$(docker compose version --short)"
    printf '\n## Runtime policy\n\n'
    printf -- '- Multi-user mode: `SINGLE_USER_MODE=%s`\n' "$(env_value SINGLE_USER_MODE)"
    printf -- '- Per-sandbox limit: `%s` memory, `%s` CPU\n' "$(env_value CONTAINER_MEM_LIMIT)" "$(env_value CONTAINER_CPU_LIMIT)"
    printf -- '- Sandbox idle/maximum continuous runtime: `%s` seconds / `%s` hours\n' "$(env_value CONTAINER_IDLE_TIMEOUT)" "$(env_value CONTAINER_MAX_AGE_HOURS)"
    printf -- '- Host publications: proxy only (`OCU_PROXY_PORT:8082`, `OCU_OFFICE_PROXY_PORT:8083`)\n'
    printf -- '- Sandbox CDP/ttyd published ports: dedicated sandbox bridge gateway (`%s`)\n' "$(env_value OCU_SANDBOX_GATEWAY)"
    printf -- '- Chat/workspace data retention: no automatic deletion\n'
    printf -- '- Offline mode: `OFFLINE_MODE=true`; version update checks disabled; embedding/rerank auto-update disabled\n'
    printf -- '- LAN OpenAI/RAG endpoints remain the configured provider URLs; local Draw.io and Pyodide materials stay enabled\n'
    printf '\n## Deployment-local modifications\n\n'
    printf -- '- `%s`\n' 'Sandbox terminals use the OCU_SANDBOX_NO_AUTOSTART=1 environment policy; no historical source patch is applied'
    printf -- '- `%s`\n' 'Open WebUI bootstrap wrapper: default model taken from the runtime configuration (left to the admin UI when empty), public internal-model access, direct tools only, no credential log'
    printf -- '- `%s`\n' 'Ollama disabled; only the configured OpenAI-compatible provider is enabled'
    printf '\n## Upgrade and offline-production notes\n\n'
    printf -- '- Import a verified `release.json` before bootstrap or startup. Do not rebuild or pull images during `deploy/up.sh`.\n'
    printf -- '- Configuration digests hash archive configuration bytes; engine IDs may instead identify verified image manifests or ancestor indexes.\n'
    printf -- '- Dockerfile hashes and material input versions record requested build inputs, not installed package versions.\n'
    printf -- '- Re-run the compatibility, UI, RAG, sandbox-isolation and backup tests before changing the source commit, Open WebUI image, model endpoint or Docker engine.\n'
    printf -- '- The runtime configuration and provider credential files are intentionally excluded from this document.\n'
} > "$tmp_file"

install -m 0644 "$tmp_file" "$output_file"
printf '%s\n' "wrote credential-free deployment record: $output_file"
