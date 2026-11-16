#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
# Stop and remove the ocu-eval containers and network. Named volumes, images and
# sandboxes stay; nothing outside the project is touched. The provider key file
# is not needed: compose down interpolates the key variable but never uses it.
set -euo pipefail
umask 077

EVAL_SOURCE="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=deploy/eval/common.sh
. "$EVAL_SOURCE/deploy/eval/common.sh"
trap eval_cleanup EXIT

eval_require_root
eval_require_port
eval_export_stack
eval_export_images
export EVAL_PROVIDER_KEY=""
eval_compose down
