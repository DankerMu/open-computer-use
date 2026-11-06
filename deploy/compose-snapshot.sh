# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
# shellcheck shell=bash
# Sourced by deploy/up.sh; defines functions only. The caller provides run_owned
# (runs a command in the background, sets OWNED_PID, waits through wait_owned),
# CONFIG_DIR and an empty "$CONFIG_DIR/empty.env".
#
# `docker compose config` writes a literal `$` as `$$`, so its output is the
# snapshot up.sh starts, byte for byte (#691). check_snapshot refuses a snapshot
# Compose would load into a different model than the source configuration.

# resolve DEST COMPOSE_ARGS...: the source configuration's resolved document.
resolve() {
    local dest=$1
    shift
    if ! run_owned docker compose "$@" config --format json >"$dest"; then
        printf '%s\n' 'deploy: compose config failed' >&2
        return 1
    fi
}

# check_snapshot STACK PROJECT PROJECT_DIR COMPOSE_ARGS...: copy
# "$CONFIG_DIR/STACK.json" to "$CONFIG_DIR/STACK.up.json" and require the per-service
# `config --hash` of the source (COMPOSE_ARGS) to equal that of the snapshot,
# loaded as up.sh starts it.
check_snapshot() {
    local stack=$1
    local project=$2
    local project_dir=$3
    shift 3
    local snapshot="$CONFIG_DIR/$stack.up.json"
    local source_hash="$CONFIG_DIR/$stack.src.hash"
    local snapshot_hash="$CONFIG_DIR/$stack.up.hash"
    if ! cp "$CONFIG_DIR/$stack.json" "$snapshot"; then
        printf '%s\n' "deploy: cannot write the ${stack} snapshot" >&2
        return 1
    fi
    if ! run_owned docker compose "$@" config --hash '*' >"$source_hash"; then
        printf '%s\n' "deploy: compose config --hash failed for the ${stack} source" >&2
        return 1
    fi
    if ! run_owned docker compose -p "$project" --project-directory "$project_dir" \
        --env-file "$CONFIG_DIR/empty.env" -f "$snapshot" config --hash '*' >"$snapshot_hash"; then
        printf '%s\n' "deploy: compose config --hash failed for the ${stack} snapshot" >&2
        return 1
    fi
    if [[ ! -s "$source_hash" || ! -s "$snapshot_hash" ]]; then
        printf '%s\n' "deploy: compose config --hash printed nothing for ${stack}" >&2
        return 1
    fi
    if ! LC_ALL=C sort -o "$source_hash" "$source_hash" \
        || ! LC_ALL=C sort -o "$snapshot_hash" "$snapshot_hash"; then
        printf '%s\n' "deploy: cannot sort the ${stack} configuration hashes" >&2
        return 1
    fi
    if ! cmp -s "$source_hash" "$snapshot_hash"; then
        printf '%s\n' "deploy: the ${stack} snapshot does not load as its source configuration; refusing to start" >&2
        return 1
    fi
}
