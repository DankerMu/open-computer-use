#!/bin/sh
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
# Render the canonical policy privately before accepting requests.

set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT"
python3 "$ROOT/render.py"
exec nginx -c "$ROOT/nginx.conf" -g 'daemon off;'
