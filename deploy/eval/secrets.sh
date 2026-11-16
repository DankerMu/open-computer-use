#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
# Generate the ocu-eval stack secrets once into ${EVAL_ROOT}/stack.env (mode 0600).
# Values use [A-Za-z0-9] only: POSTGRES_PASSWORD is embedded in DATABASE_URL and
# ADMIN_PASSWORD in shell-built JSON in openwebui/init.sh. The file is never
# overwritten, so MCP_API_KEY stays stable across restarts. No value is printed.
set -euo pipefail
umask 077

EVAL_SOURCE="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=deploy/eval/common.sh
. "$EVAL_SOURCE/deploy/eval/common.sh"

if [[ $# -ne 1 || "$1" != "init" ]]; then
    eval_fail "usage: secrets.sh init"
fi
eval_require_root

# shellcheck disable=SC2086 # the name list splits on purpose
python3 -I - "$EVAL_ROOT/stack.env" $EVAL_STACK_NAMES <<'PY'
import os
import secrets
import string
import sys

path = sys.argv[1]
alphabet = string.ascii_letters + string.digits
values = {}
for name in sys.argv[2:]:
    if name == "ADMIN_EMAIL":
        values[name] = "eval-admin@ocu-eval.local"
    else:
        values[name] = "".join(secrets.choice(alphabet) for _ in range(40))
body = "".join(f"{name}={value}\n" for name, value in values.items()).encode("ascii")
if os.path.lexists(path):
    sys.exit(f"eval: {path} exists; it is written once and never overwritten")
# Write a private temporary file, then hard-link it into place: the link fails on
# any existing entry, and an interrupted run never leaves a partial stack.env.
partial = f"{path}.partial.{os.getpid()}"
descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
try:
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(body)
        while view:
            view = view[os.write(descriptor, view):]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(partial, path, follow_symlinks=False)
    except FileExistsError:
        sys.exit(f"eval: {path} exists; it is written once and never overwritten")
finally:
    os.unlink(partial)
print(f"eval: wrote {path}")
PY
