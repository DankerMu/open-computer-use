#!/bin/bash
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Generates a root-owned, non-provider runtime configuration for the test
# deployment. The DMXAPI token remains in the separately supplied 0600 file.
#
# Required operator inputs (no historical source, image, or publication defaults):
#   SOURCE_SHA — full 40-character commit matching the selected source checkout
#   OPENWEBUI_IMAGE DOCKER_IMAGE COMPUTER_USE_SERVER_IMAGE
#   RETENTION_GUARD_IMAGE OCU_PROXY_IMAGE
#   OCU_WEBUI_ORIGIN — absolute HTTP(S) origin, no path/credentials/query/fragment/slash
#   OCU_SANDBOX_EGRESS_ALLOW — must be present; empty is deny-all
# Optional:
#   OCU_ADMIN_CREDENTIALS_FILE — isolated-test output path; default unchanged
#   POSTGRES_IMAGE OPENWEBUI_VERSION OCU_PROXY_PORT OCU_PRIVATE_* OCU_SANDBOX_*

set -euo pipefail
umask 077

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'run this script as root' >&2
    exit 1
fi

deploy_root=${DEPLOY_ROOT:-/opt/ai-workbench-test/open-computer-use}
dmx_env_file=${DMX_ENV_FILE:-/home/ubuntu/.config/ocu-test/dmxapi.env}
runtime_dir="$deploy_root/config"
runtime_file="$runtime_dir/runtime.env"
credentials_file=${OCU_ADMIN_CREDENTIALS_FILE:-/root/ocu-test-openwebui-admin-credentials.txt}
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
source_root=$(CDPATH= cd -- "$script_dir/../../.." && pwd)
credentials_parent=$(dirname -- "$credentials_file")
runtime_tmp=""
credentials_tmp=""

cleanup_temps() {
    rm -f ${runtime_tmp:+"$runtime_tmp"} ${credentials_tmp:+"$credentials_tmp"}
}
trap cleanup_temps EXIT

fail() {
    printf '%s\n' "$1" >&2
    exit 1
}

require_present() {
    local name=$1
    if ! python3 -c 'import os,sys; sys.exit(0 if sys.argv[1] in os.environ else 1)' "$name"; then
        fail "${name} is required"
    fi
}

require_nonempty() {
    local name=$1
    local value=${!name-}
    if [ -z "$value" ]; then
        fail "${name} is required"
    fi
}

# Values written into runtime.env must be a single dotenv assignment that both
# `sed -n 's/^NAME=//p'` and Compose interpolation can consume without quoting.
# Reject newlines, NULs, and characters that would inject extra assignments,
# shell metacharacters, or executable content across those consumers.
safe_dotenv_value() {
    local name=$1
    local value=$2
    case "$value" in
        *$'\n'*|*$'\r'*)
            fail "${name} contains an unsupported line break"
            ;;
    esac
    case "$value" in
        *'$'*|*'`'*|*'\\'*|*'"'*|*"'"*|'#'*'#'*|*'='*|*' '*|*$'\t'*)
            fail "${name} cannot be safely represented for dotenv consumers"
            ;;
    esac
    if [ -n "$value" ]; then
        printf '%s' "$value" | python3 -c '
import sys
value = sys.stdin.read()
if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
    raise SystemExit(1)
' || fail "${name} cannot be safely represented for dotenv consumers"
    fi
}

require_image() {
    local name=$1
    require_nonempty "$name"
    safe_dotenv_value "$name" "${!name}"
}

if [ ! -r "$dmx_env_file" ] || ! grep -q '^DMXAPI_API_KEY=.' "$dmx_env_file"; then
    fail "DMXAPI credential file is unreadable or missing DMXAPI_API_KEY: $dmx_env_file"
fi

if [ "$(stat -c '%a' "$dmx_env_file")" != "600" ]; then
    fail "DMXAPI credential file must be mode 0600: $dmx_env_file"
fi

if [ ! -d "$deploy_root/source/.git" ]; then
    fail "source checkout is missing: $deploy_root/source"
fi

require_nonempty SOURCE_SHA
safe_dotenv_value SOURCE_SHA "$SOURCE_SHA"
case "$SOURCE_SHA" in
    *[!0-9a-fA-F]*)
        fail "SOURCE_SHA must be the full commit matching the selected checkout"
        ;;
esac
if [ "${#SOURCE_SHA}" -ne 40 ]; then
    fail "SOURCE_SHA must be the full commit matching the selected checkout"
fi

actual_sha=$(git -C "$deploy_root/source" rev-parse HEAD)
if [ "$actual_sha" != "$SOURCE_SHA" ]; then
    fail "source checkout must be $SOURCE_SHA, found $actual_sha"
fi

require_image OPENWEBUI_IMAGE
require_image DOCKER_IMAGE
require_image COMPUTER_USE_SERVER_IMAGE
require_image RETENTION_GUARD_IMAGE
require_image OCU_PROXY_IMAGE

case "$DOCKER_IMAGE" in
    *open-computer-use*)
        ;;
    *)
        fail 'DOCKER_IMAGE must retain the open-computer-use name used for /home/assistant mounts'
        ;;
esac

require_present OCU_SANDBOX_EGRESS_ALLOW
safe_dotenv_value OCU_SANDBOX_EGRESS_ALLOW "$OCU_SANDBOX_EGRESS_ALLOW"
export OCU_SANDBOX_EGRESS_ALLOW
export PYTHONPATH="$source_root/deploy${PYTHONPATH:+:$PYTHONPATH}"
python3 - "$source_root" <<'PY' || fail 'OCU_SANDBOX_EGRESS_ALLOW is invalid'
import os
import sys

sys.path.insert(0, os.path.join(sys.argv[1], "deploy"))
from firewall.policy import parse_allowlist

parse_allowlist(os.environ["OCU_SANDBOX_EGRESS_ALLOW"])
PY

require_nonempty OCU_WEBUI_ORIGIN
safe_dotenv_value OCU_WEBUI_ORIGIN "$OCU_WEBUI_ORIGIN"
python3 - "$OCU_WEBUI_ORIGIN" <<'PY' || fail 'OCU_WEBUI_ORIGIN must be an absolute HTTP(S) origin without path, credentials, query, fragment, or trailing slash'
import sys
from urllib.parse import urlsplit

value = sys.argv[1]
if any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in value):
    raise SystemExit(1)
if any(marker in value for marker in ("@", "?", "#")):
    raise SystemExit(1)
try:
    parsed = urlsplit(value)
    port = parsed.port
except ValueError:
    raise SystemExit(1)
if (
    parsed.scheme not in {"http", "https"}
    or not parsed.hostname
    or parsed.username
    or parsed.password
    or parsed.path
    or parsed.query
    or parsed.fragment
    or value.endswith("/")
    or parsed.netloc != parsed.netloc.lower()
):
    raise SystemExit(1)
if port is not None and not (1 <= port <= 65535):
    raise SystemExit(1)
PY

postgres_image=${POSTGRES_IMAGE:-postgres:17-alpine}
openwebui_version=${OPENWEBUI_VERSION:-0.11.3}
proxy_port=${OCU_PROXY_PORT:-8082}
private_network=${OCU_PRIVATE_NETWORK:-ocu-test-private}
private_subnet=${OCU_PRIVATE_SUBNET:-172.30.0.0/24}
private_gateway=${OCU_PRIVATE_GATEWAY:-172.30.0.1}
sandbox_network=${OCU_SANDBOX_NETWORK:-ocu-sandbox}
sandbox_subnet=${OCU_SANDBOX_SUBNET:-172.31.0.0/24}
sandbox_gateway=${OCU_SANDBOX_GATEWAY:-172.31.0.1}
public_base_url="${OCU_WEBUI_ORIGIN}/ocu"
internal_auth_url='http://open-webui:8080/api/v1/ocu/auth'
chat_data_dir="$deploy_root/data/chat"
skills_cache_dir="$deploy_root/data/skills-cache"

for pair in \
    POSTGRES_IMAGE:"$postgres_image" \
    OPENWEBUI_VERSION:"$openwebui_version" \
    OCU_PROXY_PORT:"$proxy_port" \
    OCU_PRIVATE_NETWORK:"$private_network" \
    OCU_PRIVATE_SUBNET:"$private_subnet" \
    OCU_PRIVATE_GATEWAY:"$private_gateway" \
    OCU_SANDBOX_NETWORK:"$sandbox_network" \
    OCU_SANDBOX_SUBNET:"$sandbox_subnet" \
    OCU_SANDBOX_GATEWAY:"$sandbox_gateway" \
    PUBLIC_BASE_URL:"$public_base_url" \
    OCU_WEBUI_AUTH_URL:"$internal_auth_url" \
    OCU_CHAT_DATA_DIR:"$chat_data_dir" \
    OCU_SKILLS_CACHE_DIR:"$skills_cache_dir"
do
    name=${pair%%:*}
    value=${pair#*:}
    safe_dotenv_value "$name" "$value"
done

if [ -e "$runtime_file" ] || [ -e "$credentials_file" ]; then
    fail 'refusing to overwrite an existing runtime configuration or admin credential file'
fi

install -d -m 0700 "$runtime_dir" "$deploy_root/backups"
install -d -m 0755 "$deploy_root/data/chat" "$deploy_root/data/skills-cache"
if [ ! -d "$credentials_parent" ]; then
    install -d -m 0700 "$credentials_parent"
fi

webui_secret=$(openssl rand -hex 32)
mcp_api_key=$(openssl rand -hex 32)
postgres_password=$(openssl rand -hex 32)
admin_password=$(openssl rand -hex 32)
internal_token=$(openssl rand -hex 32)

runtime_tmp=$(mktemp "$runtime_dir/.runtime.env.XXXXXX")
credentials_tmp=$(mktemp "$credentials_parent/.ocu-test-admin-credentials.XXXXXX")

{
    printf '%s\n' 'COMPOSE_PROJECT_NAME=ocu-test'
    printf '%s\n' "SOURCE_SHA=$SOURCE_SHA"
    printf '%s\n' "OPENWEBUI_VERSION=$openwebui_version"
    printf '%s\n' "OPENWEBUI_IMAGE=$OPENWEBUI_IMAGE"
    printf '%s\n' "POSTGRES_IMAGE=$postgres_image"
    # docker_manager identifies the production workspace path from this image
    # name. Keep "open-computer-use" in the tag so each workspace volume is
    # mounted at /home/assistant rather than the development /root path.
    printf '%s\n' "DOCKER_IMAGE=$DOCKER_IMAGE"
    printf '%s\n' "COMPUTER_USE_SERVER_IMAGE=$COMPUTER_USE_SERVER_IMAGE"
    printf '%s\n' "RETENTION_GUARD_IMAGE=$RETENTION_GUARD_IMAGE"
    printf '%s\n' "OCU_PROXY_IMAGE=$OCU_PROXY_IMAGE"
    printf '%s\n' "OCU_PRIVATE_NETWORK=$private_network"
    printf '%s\n' "OCU_PRIVATE_SUBNET=$private_subnet"
    printf '%s\n' "OCU_PRIVATE_GATEWAY=$private_gateway"
    printf '%s\n' "OCU_SANDBOX_NETWORK=$sandbox_network"
    printf '%s\n' "OCU_SANDBOX_SUBNET=$sandbox_subnet"
    printf '%s\n' "OCU_SANDBOX_GATEWAY=$sandbox_gateway"
    printf '%s\n' "OCU_SANDBOX_EGRESS_ALLOW=$OCU_SANDBOX_EGRESS_ALLOW"
    printf '%s\n' "OCU_PROXY_PORT=$proxy_port"
    printf '%s\n' "OCU_WEBUI_ORIGIN=$OCU_WEBUI_ORIGIN"
    printf '%s\n' "PUBLIC_BASE_URL=$public_base_url"
    printf '%s\n' "OCU_WEBUI_AUTH_URL=$internal_auth_url"
    printf '%s\n' 'OCU_PUBLIC_PREFIX=/ocu'
    printf '%s\n' 'OCU_SANDBOX_NO_AUTOSTART=1'
    # One generated token is consumed by WebUI, OCU, and proxy.
    printf '%s\n' "OCU_INTERNAL_TOKEN=$internal_token"
    printf '%s\n' 'DMXAPI_BASE_URL=https://www.dmxapi.cn/v1'
    printf '%s\n' "OCU_CHAT_DATA_DIR=$chat_data_dir"
    printf '%s\n' "OCU_SKILLS_CACHE_DIR=$skills_cache_dir"
    printf '%s\n' 'ADMIN_EMAIL=admin@ai-test.local'
    printf '%s\n' "ADMIN_PASSWORD=$admin_password"
    printf '%s\n' "WEBUI_SECRET_KEY=$webui_secret"
    printf '%s\n' "MCP_API_KEY=$mcp_api_key"
    printf '%s\n' "POSTGRES_PASSWORD=$postgres_password"
    printf '%s\n' 'SINGLE_USER_MODE=false'
    printf '%s\n' 'CONTAINER_MEM_LIMIT=2g'
    printf '%s\n' 'CONTAINER_CPU_LIMIT=1.0'
    # Do not stop an otherwise healthy chat sandbox before the selected
    # 168-hour maximum continuous-runtime policy. The retention guard enforces
    # that hard ceiling without deleting chat data or workspace volumes.
    printf '%s\n' 'CONTAINER_IDLE_TIMEOUT=604800'
    printf '%s\n' 'COMMAND_TIMEOUT=120'
    printf '%s\n' 'SUB_AGENT_TIMEOUT=3600'
    printf '%s\n' 'CONTAINER_MAX_AGE_HOURS=168'
    printf '%s\n' 'RETENTION_CHECK_INTERVAL_SECONDS=3600'
} > "$runtime_tmp"

{
    printf '%s\n' 'Open WebUI test administrator credentials'
    printf '%s\n' 'Access: SSH tunnel only; see deployment report for the local URL.'
    printf '%s\n' 'Email: admin@ai-test.local'
    printf '%s\n' "Password: $admin_password"
} > "$credentials_tmp"

install -m 0600 "$runtime_tmp" "$runtime_file"
install -m 0600 "$credentials_tmp" "$credentials_file"

printf '%s\n' "created protected runtime configuration: $runtime_file"
printf '%s\n' "created protected administrator credential file: $credentials_file"
