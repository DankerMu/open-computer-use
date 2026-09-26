#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
# Operator post-deploy overlay smoke. Does not start or stop Compose stacks.
set -euo pipefail
umask 077
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$ROOT/deploy:$ROOT/computer-use-server${PYTHONPATH:+:$PYTHONPATH}"
exec python3 "$ROOT/deploy/smoke_deployment.py" "$@"
