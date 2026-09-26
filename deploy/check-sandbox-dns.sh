#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
# Read-only protected-bridge DNS check. Does not mutate containers.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ "$#" -ne 1 ]]; then
    printf '%s\n' 'sandbox-dns: resolved core Compose JSON is required' >&2
    exit 1
fi
export PYTHONPATH="$ROOT/computer-use-server:$ROOT/deploy${PYTHONPATH:+:$PYTHONPATH}"
exec python3 "$ROOT/deploy/check_sandbox_dns.py" "$1"
