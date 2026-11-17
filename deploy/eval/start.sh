#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
# Start the isolated ocu-eval stack from the images bound by load.sh.
# Requires EVAL_ROOT and EVAL_WEBUI_PORT. Reads ${EVAL_ROOT}/stack.env,
# ${EVAL_ROOT}/images.env and the provider key file ${EVAL_KEY_FILE} (default
# $HOME/.ocu-eval-dmxapi.key, outside EVAL_ROOT so the evaluation's key-leak
# search over EVAL_ROOT never finds it); each must be a 0600 regular file owned
# by the caller. Values are exported into the compose environment only and never
# printed. Exits non-zero unless open-webui-init finishes with status 0.
set -euo pipefail
umask 077

EVAL_SOURCE="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=deploy/eval/common.sh
. "$EVAL_SOURCE/deploy/eval/common.sh"
trap eval_cleanup EXIT

eval_require_root
eval_require_port
key_file="${EVAL_KEY_FILE:-$HOME/.ocu-eval-dmxapi.key}"
eval_require_private "$key_file"
eval_export_stack
eval_export_images
eval_require_space 0

EVAL_PROVIDER_KEY=$(<"$key_file")
if [[ -z "$EVAL_PROVIDER_KEY" || "$EVAL_PROVIDER_KEY" == *[[:space:]]* ]]; then
    eval_fail "$key_file must hold one key on one line"
fi
export EVAL_PROVIDER_KEY

# The orchestrator's data directory; the compose bind refuses to create it.
mkdir -p -- "$EVAL_ROOT/data"
eval_compose up -d --no-build --pull never
# compose.eval.yml names every container <project>-<service>.
EVAL_INIT_CONTAINER="$EVAL_PROJECT-open-webui-init"
# docker wait returns the initializer's exit code whether it is still running or
# has already exited. After the first start it exits within a second (the marker
# is in the volume), and compose wait, which lists only running containers in
# Compose 2.20-2.36, would then fail instead of reporting status 0.
if ! status=$(docker wait "$EVAL_INIT_CONTAINER"); then
    eval_fail "cannot read the exit status of $EVAL_INIT_CONTAINER: docker wait failed"
fi
if [[ ! "$status" =~ ^[0-9]+$ ]]; then
    eval_fail "docker wait $EVAL_INIT_CONTAINER returned no exit status"
fi
if (( status != 0 )); then
    eval_fail "open-webui-init exited with status $status; the stack is still running (docker logs $EVAL_INIT_CONTAINER)"
fi
printf '%s\n' "eval: ocu-eval is up and open-webui-init finished"
