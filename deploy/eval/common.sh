# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
# Shared checks for the ocu-eval scripts; sourced, never executed. Secret values
# travel only through exported variables: no function prints a value it reads.
# shellcheck shell=bash

EVAL_PROJECT="ocu-eval"
# Docker's root filesystem must keep this much free before a load and a start.
EVAL_FLOOR_BYTES=$((15 * 1024 * 1024 * 1024))
EVAL_STACK_NAMES="OCU_INTERNAL_TOKEN MCP_API_KEY WEBUI_SECRET_KEY POSTGRES_PASSWORD ADMIN_EMAIL ADMIN_PASSWORD"
EVAL_ROLES="workspace computer-use-server open-webui postgres"
EVAL_TMP=""

eval_fail() {
    printf '%s\n' "eval: $*" >&2
    exit 1
}

eval_cleanup() {
    local status=$?
    if [[ -n "${EVAL_TMP:-}" && -d "$EVAL_TMP" ]]; then
        rm -rf -- "$EVAL_TMP"
    fi
    return "$status"
}

eval_require_root() {
    if [[ -z "${EVAL_ROOT:-}" ]]; then
        eval_fail "EVAL_ROOT is required"
    fi
    if [[ "$EVAL_ROOT" != /* || ! -d "$EVAL_ROOT" || -L "$EVAL_ROOT" ]]; then
        eval_fail "EVAL_ROOT must be an existing absolute directory, not a symlink: $EVAL_ROOT"
    fi
    export EVAL_ROOT
}

eval_require_port() {
    if [[ -z "${EVAL_WEBUI_PORT:-}" ]]; then
        eval_fail "EVAL_WEBUI_PORT is required"
    fi
    if [[ ! "$EVAL_WEBUI_PORT" =~ ^[1-9][0-9]{0,4}$ ]] || (( EVAL_WEBUI_PORT > 65535 )); then
        eval_fail "EVAL_WEBUI_PORT must be a TCP port number"
    fi
    export EVAL_WEBUI_PORT
}

# A regular file, not a symlink, owned by the caller, mode exactly 0600.
eval_require_private() {
    local path=$1
    python3 - "$path" <<'PY' || exit 1
import os
import stat
import sys

path = sys.argv[1]
try:
    info = os.lstat(path)
except FileNotFoundError:
    sys.exit(f"eval: {path} is missing")
if not stat.S_ISREG(info.st_mode):
    sys.exit(f"eval: {path} must be a regular file, not a symlink or other type")
if info.st_uid != os.getuid():
    sys.exit(f"eval: {path} must be owned by the caller")
if stat.S_IMODE(info.st_mode) != 0o600:
    sys.exit(f"eval: {path} must have mode 0600, found {stat.S_IMODE(info.st_mode):04o}")
PY
}

# Export NAME=value lines for exactly the given names. A malformed line is
# reported by line number only, so its value never reaches the terminal.
eval_export_file() {
    local path=$1 pattern=$2
    shift 2
    local expected=" $* " seen=" " line name value number=0
    eval_require_private "$path"
    while IFS= read -r line || [[ -n "$line" ]]; do
        number=$((number + 1))
        if [[ ! "$line" =~ ^([A-Z][A-Z0-9_]*)=(.*)$ ]]; then
            eval_fail "$path:$number: expected NAME=value"
        fi
        name=${BASH_REMATCH[1]}
        value=${BASH_REMATCH[2]}
        if [[ "$expected" != *" $name "* || "$seen" == *" $name "* ]]; then
            eval_fail "$path:$number: unexpected or repeated name $name"
        fi
        if [[ ! "$value" =~ $pattern ]]; then
            eval_fail "$path:$number: $name has a malformed value"
        fi
        printf -v "$name" '%s' "$value"
        # shellcheck disable=SC2163 # exports the variable named by $name
        export "$name"
        seen="$seen$name "
    done <"$path"
    for name in "$@"; do
        if [[ "$seen" != *" $name "* ]]; then
            eval_fail "$path: $name is missing"
        fi
    done
}

eval_export_stack() {
    # shellcheck disable=SC2086 # the name list splits on purpose
    eval_export_file "$EVAL_ROOT/stack.env" '^[A-Za-z0-9][A-Za-z0-9.@-]*$' $EVAL_STACK_NAMES
}

# The image variable names for the four roles, from release.py.
eval_image_names() {
    # shellcheck disable=SC2086 # the role list splits on purpose
    python3 - "$EVAL_SOURCE/deploy" $EVAL_ROLES <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
import release

print(" ".join(release.RUNTIME_IMAGE_VARS[role] for role in sys.argv[2:]))
PY
}

eval_export_images() {
    local names
    names=$(eval_image_names) || eval_fail "cannot read the image variable names from release.py"
    # shellcheck disable=SC2086 # the name list splits on purpose
    eval_export_file "$EVAL_ROOT/images.env" '^[A-Za-z0-9][A-Za-z0-9._/:-]*$' $names
}

# Free space on Docker's root filesystem. The root comes from release.py's
# local-daemon DockerRootDir lookup; df measures it.
eval_require_space() {
    local docker_root line available
    docker_root=$(python3 - "$EVAL_SOURCE/deploy" <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
import release

try:
    root = release.local_daemon_info().get("DockerRootDir")
except release.ReleaseError as cop:
    sys.exit(f"eval: {cop}")
if not isinstance(root, str) or not root:
    sys.exit("eval: docker info did not report DockerRootDir")
sys.stdout.write(root)
PY
) || exit 1
    line=$(df -Pk -- "$docker_root" | tail -n 1) || eval_fail "df cannot measure $docker_root"
    available=$(printf '%s\n' "$line" | awk '{print $4}')
    if [[ ! "$available" =~ ^[0-9]+$ ]]; then
        eval_fail "df reported no free-space figure for $docker_root"
    fi
    available=$((available * 1024))
    if (( available < EVAL_FLOOR_BYTES )); then
        eval_fail "Docker root $docker_root has $available bytes free; the floor is $EVAL_FLOOR_BYTES bytes (15 GiB)"
    fi
}

# Compose with the fixed project, project directory, empty env file and files.
eval_compose() {
    if [[ -z "${EVAL_TMP:-}" ]]; then
        EVAL_TMP=$(mktemp -d "${TMPDIR:-/tmp}/ocu-eval.XXXXXX")
    fi
    : >"$EVAL_TMP/empty.env"
    # An ambient profile would re-enable the disabled workspace and cleanup jobs.
    export COMPOSE_PROFILES=""
    export COMPOSE_REMOVE_ORPHANS=false
    docker compose -p "$EVAL_PROJECT" --project-directory "$EVAL_SOURCE" \
        --env-file "$EVAL_TMP/empty.env" \
        -f "$EVAL_SOURCE/docker-compose.yml" \
        -f "$EVAL_SOURCE/docker-compose.webui.yml" \
        -f "$EVAL_SOURCE/deploy/eval/compose.eval.yml" "$@"
}
