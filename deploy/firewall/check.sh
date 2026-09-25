#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
# Read-only ordered-policy check. Does not mutate firewall state.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export PYTHONPATH="$ROOT/deploy${PYTHONPATH:+:$PYTHONPATH}"
exec python3 "$ROOT/deploy/firewall/policy.py" check
