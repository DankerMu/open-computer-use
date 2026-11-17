#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
# Run evaluation requests through the running ocu-eval stack.
# Usage: run.sh --package <dir> --phase baseline|after|smoke [--request <id>] [--repetition <n>]
# Requires EVAL_ROOT and EVAL_WEBUI_PORT. Exports stack.env and images.env as
# start.sh does and starts driver.py with the host's python3 in isolated mode. The
# provider key file ${EVAL_KEY_FILE} (default $HOME/.ocu-eval-dmxapi.key) reaches
# the driver on stdin only, never on a command line or in the environment.
# Prints one line per attempt and always ends with "eval: done <exit status>";
# exits 0 when every attempt ended with a manifest, whatever its outcome.
set -euo pipefail
umask 077

eval_done() {
    local status=$?
    printf '%s\n' "eval: done $status"
    exit "$status"
}
trap eval_done EXIT

EVAL_SOURCE="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=deploy/eval/common.sh
. "$EVAL_SOURCE/deploy/eval/common.sh"

eval_require_root
eval_require_port
key_file="${EVAL_KEY_FILE:-$HOME/.ocu-eval-dmxapi.key}"
eval_require_private "$key_file"
eval_export_stack
eval_export_images
# Names only: the driver redacts these variables' values and keeps them from its children.
export EVAL_STACK_NAMES EVAL_ROLES
python3 -I "$EVAL_SOURCE/deploy/eval/driver.py" "$@" <"$key_file"
