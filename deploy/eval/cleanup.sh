#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
# Remove the ocu-eval objects, by exact name, and nothing else.
# Usage: cleanup.sh [--runs-only]
# Requires EVAL_ROOT; the full mode also EVAL_WEBUI_PORT (for stop.sh). Takes
# ${EVAL_ROOT}/eval.lock with flock -n and refuses while a driver holds it.
# For each chat id in ${EVAL_ROOT}/chats.txt: the sandbox owui-chat-<id>, the
# volume chat-<id>-workspace and the root-owned data directory, removed by a
# helper container of the workspace image. Without --runs-only, first the
# compose down of stop.sh, then the per-chat steps, then the project's labelled
# volumes and the images this project loaded or labelled, without -f. Images are
# not touched after a failed per-chat step, so the helper image stays for the
# next run. chats.txt keeps the ids not fully removed; images.env and
# loaded-images.txt go only with the last image. Exits 0 only when every target
# is gone; a second run finishes what the first left. runs/, stack.env, src/ and
# packages/ stay. Never prunes.
set -euo pipefail
umask 077

EVAL_SOURCE="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=deploy/eval/common.sh
. "$EVAL_SOURCE/deploy/eval/common.sh"
trap eval_cleanup EXIT

case "$*" in
    "") runs_only=false ;;
    --runs-only) runs_only=true ;;
    *) eval_fail "usage: cleanup.sh [--runs-only]" ;;
esac
eval_require_root
if ! $runs_only; then
    eval_require_port
fi
exec 9>>"$EVAL_ROOT/eval.lock"
if ! flock -n 9; then
    eval_fail "$EVAL_ROOT/eval.lock is held: a driver is running; cleanup refuses"
fi

ledger="$EVAL_ROOT/chats.txt"
loaded="$EVAL_ROOT/loaded-images.txt"
ids=()
if [[ -e "$ledger" || -L "$ledger" ]]; then
    # Ids only, no secret: a hand-made ledger may have the default mode.
    if [[ -L "$ledger" || ! -f "$ledger" ]]; then
        eval_fail "$ledger must be a regular file, not a symlink"
    fi
    number=0
    while IFS= read -r line || [[ -n "$line" ]]; do
        number=$((number + 1))
        if [[ ! "$line" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
            eval_fail "$ledger:$number: expected a lowercase chat UUID"
        fi
        if [[ " ${ids[*]-} " != *" $line "* ]]; then
            ids+=("$line")
        fi
    done <"$ledger"
fi
references=()
if [[ -e "$loaded" ]]; then
    number=0
    while IFS= read -r line || [[ -n "$line" ]]; do
        number=$((number + 1))
        if [[ ! "$line" =~ ^[A-Za-z0-9][A-Za-z0-9._/:@-]*$ ]]; then
            eval_fail "$loaded:$number: expected an image reference"
        fi
        references+=("$line")
    done <"$loaded"
fi

failed=false
have_images=false
if [[ -e "$EVAL_ROOT/images.env" || -L "$EVAL_ROOT/images.env" ]]; then
    eval_export_images
    have_images=true
fi
if ! $runs_only; then
    if $have_images; then
        "$EVAL_SOURCE/deploy/eval/stop.sh" || failed=true
    elif ! remaining=$(docker container ls --all --quiet --filter "label=com.docker.compose.project=$EVAL_PROJECT"); then
        failed=true
    elif [[ -n "$remaining" ]]; then
        printf '%s\n' "eval: $EVAL_ROOT/images.env is absent, but ocu-eval containers remain; compose down needs it (run load.sh)" >&2
        failed=true
    else
        printf '%s\n' "eval: $EVAL_ROOT/images.env is absent and no ocu-eval container remains; nothing to stop"
    fi
fi

# Whether the docker listing in the remaining arguments holds exactly $1:
# status 0 yes, 1 no, 2 when docker fails.
eval_listed() {
    local name=$1 listing
    shift
    listing=$(docker "$@") || return 2
    grep -Fxq -- "$name" <<<"$listing"
}

# One chat's sandbox, volume and data directory; fails when any of them stays.
eval_remove_chat() {
    local id=$1 container="owui-chat-$1" volume="chat-$1-workspace" status=0 found
    found=0
    eval_listed "$container" container ls --all --filter "name=$container" --format '{{.Names}}' || found=$?
    case $found in
        0) docker rm -f "$container" >/dev/null || status=1 ;;
        1) ;;
        *) status=1 ;;
    esac
    found=0
    eval_listed "$volume" volume ls --quiet --filter "name=$volume" || found=$?
    case $found in
        0) docker volume rm "$volume" >/dev/null || status=1 ;;
        1) ;;
        *) status=1 ;;
    esac
    if [[ -e "$EVAL_ROOT/data/$id" || -L "$EVAL_ROOT/data/$id" ]]; then
        if ! $have_images; then
            printf '%s\n' "eval: $EVAL_ROOT/images.env is absent; cannot remove $EVAL_ROOT/data/$id" >&2
            return 1
        fi
        # The directory is root-owned (mode 0777, made by the orchestrator).
        docker run --rm --network none --user 0:0 --entrypoint rm \
            -v "$EVAL_ROOT/data:/d" "$DOCKER_IMAGE" -rf -- "/d/$id" || status=1
        if [[ -e "$EVAL_ROOT/data/$id" || -L "$EVAL_ROOT/data/$id" ]]; then
            status=1
        fi
    fi
    return "$status"
}

kept=()
for id in ${ids[@]+"${ids[@]}"}; do
    if ! eval_remove_chat "$id"; then
        printf '%s\n' "eval: chat $id is not fully removed; it stays in $ledger" >&2
        kept+=("$id")
        failed=true
    fi
done
if [[ -e "$ledger" ]]; then
    : >"$ledger.partial.$$"
    for id in ${kept[@]+"${kept[@]}"}; do
        printf '%s\n' "$id" >>"$ledger.partial.$$"
    done
    mv -f -- "$ledger.partial.$$" "$ledger"
fi

if ! $runs_only; then
    if volumes=$(docker volume ls --quiet --filter "label=com.docker.compose.project=$EVAL_PROJECT"); then
        for volume in $volumes; do
            if [[ "$volume" == "${EVAL_PROJECT}_"* ]]; then
                docker volume rm "$volume" >/dev/null || failed=true
            fi
        done
    else
        failed=true
    fi
    if [[ -n "${kept[*]-}" ]]; then
        printf '%s\n' "eval: a chat removal failed; images stay for the next run" >&2
    else
        images_gone=true
        targets=()
        for reference in ${references[@]+"${references[@]}"}; do
            if ! present=$(docker image ls --quiet "$reference"); then
                images_gone=false
            elif [[ -n "$present" ]]; then
                targets+=("$reference")
            fi
        done
        if labelled=$(docker image ls --filter "label=$EVAL_PROJECT" --format '{{.Repository}}:{{.Tag}} {{.ID}}'); then
            while read -r name image_id; do
                [[ -n "${name:-}" ]] || continue
                if [[ "$name" == *"<none>"* ]]; then
                    name=$image_id
                fi
                if [[ " ${targets[*]-} " != *" $name "* ]]; then
                    targets+=("$name")
                fi
            done <<<"$labelled"
        else
            images_gone=false
        fi
        for target in ${targets[@]+"${targets[@]}"}; do
            docker image rm "$target" >/dev/null || images_gone=false
        done
        if $images_gone; then
            rm -f -- "$EVAL_ROOT/images.env" "$loaded"
        else
            printf '%s\n' "eval: an image is not removed; images.env and loaded-images.txt stay" >&2
            failed=true
        fi
    fi
fi

if $failed; then
    eval_fail "cleanup is incomplete; run it again after fixing the reported failures"
fi
printf '%s\n' "eval: cleanup finished"
