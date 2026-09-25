#!/bin/bash
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Produce a credential-free deployment bill of materials for this exact host.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'run this script as root' >&2
    exit 1
fi

deploy_root=${DEPLOY_ROOT:-/opt/ai-workbench-test/open-computer-use}
source_dir="$deploy_root/source"
runtime_file="$deploy_root/config/runtime.env"
output_file="$deploy_root/DEPLOYED_VERSION.md"

if [ ! -d "$source_dir/.git" ] || [ ! -r "$runtime_file" ]; then
    printf '%s\n' 'source checkout or protected runtime configuration is missing' >&2
    exit 1
fi

env_value() {
    sed -n "s/^$1=//p" "$runtime_file"
}

source_sha=$(git -C "$source_dir" rev-parse HEAD)
openwebui_image=$(env_value OPENWEBUI_IMAGE)
postgres_image=$(env_value POSTGRES_IMAGE)
workspace_image=$(env_value DOCKER_IMAGE)
server_image=$(env_value COMPUTER_USE_SERVER_IMAGE)
retention_image=$(env_value RETENTION_GUARD_IMAGE)
generated_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
tmp_file=$(mktemp "$deploy_root/.DEPLOYED_VERSION.md.XXXXXX")
trap 'rm -f "$tmp_file"' EXIT

image_id() {
    docker image inspect --format '{{.Id}}' "$1"
}

{
    printf '%s\n' '# Open WebUI + Open Computer Use deployment version record'
    printf '\nGenerated at (UTC): `%s`\n' "$generated_at"
    printf '\n## Source and runtime images\n\n'
    printf -- '- Open Computer Use source commit: `%s`\n' "$source_sha"
    printf -- '- Open WebUI image: `%s`\n' "$openwebui_image"
    printf -- '- PostgreSQL image: `%s`\n' "$postgres_image"
    printf -- '- Workspace image: `%s` (`%s`)\n' "$workspace_image" "$(image_id "$workspace_image")"
    printf -- '- Computer Use server image: `%s` (`%s`)\n' "$server_image" "$(image_id "$server_image")"
    printf -- '- Retention guard image: `%s` (`%s`)\n' "$retention_image" "$(image_id "$retention_image")"
    printf -- '- Docker engine: `%s`\n' "$(docker version --format '{{.Server.Version}}')"
    printf -- '- Docker Compose: `%s`\n' "$(docker compose version --short)"
    printf '\n## Runtime policy\n\n'
    printf -- '- Multi-user mode: `SINGLE_USER_MODE=%s`\n' "$(env_value SINGLE_USER_MODE)"
    printf -- '- Per-sandbox limit: `%s` memory, `%s` CPU\n' "$(env_value CONTAINER_MEM_LIMIT)" "$(env_value CONTAINER_CPU_LIMIT)"
    printf -- '- Sandbox idle/maximum continuous runtime: `%s` seconds / `%s` hours\n' "$(env_value CONTAINER_IDLE_TIMEOUT)" "$(env_value CONTAINER_MAX_AGE_HOURS)"
    printf -- '- Host publication: proxy only (`OCU_PROXY_PORT`)\n'
    printf -- '- Sandbox CDP/ttyd published ports: dedicated sandbox bridge gateway (`%s`)\n' "$(env_value OCU_SANDBOX_GATEWAY)"
    printf -- '- Chat/workspace data retention: no automatic deletion\n'
    printf '\n## Deployment-local modifications\n\n'
    printf -- '- `%s`\n' 'disable-cli-autostart.patch'
    printf -- '- `%s`\n' 'Open WebUI bootstrap wrapper: explicit Qwen model selection, public internal-model access, direct tools only, no credential log'
    printf -- '- `%s`\n' 'Ollama disabled; only the configured OpenAI-compatible provider is enabled'
    printf '\n## Upgrade and offline-production notes\n\n'
    printf -- '- Do not replace digest-pinned Open WebUI/PostgreSQL images with tags during routine startup.\n'
    printf -- '- Re-run the compatibility, UI, RAG, sandbox-isolation and backup tests before changing the source commit, Open WebUI image, model endpoint or Docker engine.\n'
    printf -- '- The upstream workspace Dockerfile downloads apt, PyPI, npm, Playwright and external skill dependencies while building. Offline production must import the tested built images (or use an internal, locked mirror) rather than rebuild from the internet.\n'
    printf -- '- The runtime configuration and provider credential files are intentionally excluded from this document.\n'
} > "$tmp_file"

install -m 0644 "$tmp_file" "$output_file"
printf '%s\n' "wrote credential-free deployment record: $output_file"
