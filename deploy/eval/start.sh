#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
# Start the isolated ocu-eval stack from the images bound by load.sh.
# Requires EVAL_ROOT and EVAL_WEBUI_PORT. Reads ${EVAL_ROOT}/stack.env,
# ${EVAL_ROOT}/images.env and the provider key file ${EVAL_KEY_FILE} (default
# ${EVAL_ROOT}/dmxapi.key); each must be a 0600 regular file owned by the caller.
# Values are exported into the compose environment only and never printed.
set -euo pipefail
umask 077

EVAL_SOURCE="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=deploy/eval/common.sh
. "$EVAL_SOURCE/deploy/eval/common.sh"
trap eval_cleanup EXIT

eval_require_root
eval_require_port
key_file="${EVAL_KEY_FILE:-$EVAL_ROOT/dmxapi.key}"
eval_require_private "$key_file"
eval_export_stack
eval_export_images
eval_require_space

EVAL_PROVIDER_KEY=$(<"$key_file")
if [[ -z "$EVAL_PROVIDER_KEY" || "$EVAL_PROVIDER_KEY" == *[[:space:]]* ]]; then
    eval_fail "$key_file must hold one key on one line"
fi
export EVAL_PROVIDER_KEY

# The orchestrator's data directory; the compose bind refuses to create it.
mkdir -p -- "$EVAL_ROOT/data"
eval_compose up -d --no-build --pull never
