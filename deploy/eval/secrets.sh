#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
# Generate the ocu-eval stack secrets into ${EVAL_ROOT}/stack.env (mode 0600).
# Values use [A-Za-z0-9] only: POSTGRES_PASSWORD is embedded in DATABASE_URL and
# ADMIN_PASSWORD in shell-built JSON in openwebui/init.sh. An existing value is
# never changed, so MCP_API_KEY stays stable across restarts: on an existing
# stack.env, init only appends the names it lacks (such as ADAPTER_TOKEN, added
# after the first six), and with none missing it changes nothing. No value is
# printed.
set -euo pipefail
umask 077

EVAL_SOURCE="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=deploy/eval/common.sh
. "$EVAL_SOURCE/deploy/eval/common.sh"

if [[ $# -ne 1 || "$1" != "init" ]]; then
    eval_fail "usage: secrets.sh init"
fi
eval_require_root
stack="$EVAL_ROOT/stack.env"
if [[ -e "$stack" || -L "$stack" ]]; then
    eval_require_private "$stack"
fi

# shellcheck disable=SC2086 # the name list splits on purpose
python3 -I - "$stack" $EVAL_STACK_NAMES <<'PY'
import os
import re
import secrets
import string
import sys

path = sys.argv[1]
names = sys.argv[2:]
alphabet = string.ascii_letters + string.digits
# The lines and values eval_export_stack in common.sh accepts.
LINE = re.compile(r"([A-Z][A-Z0-9_]*)=(.*)")
VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.@-]*")


def generated(missing):
    lines = []
    for name in missing:
        if name == "ADMIN_EMAIL":
            value = "eval-admin@ocu-eval.local"
        else:
            value = "".join(secrets.choice(alphabet) for _ in range(40))
        lines.append(f"{name}={value}\n")
    return "".join(lines).encode("ascii")


def write_partial(body):
    """A private, fully written temporary file next to stack.env."""
    partial = f"{path}.partial.{os.getpid()}"
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(body)
        while view:
            view = view[os.write(descriptor, view):]
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        os.unlink(partial)
        raise
    os.close(descriptor)
    return partial


def existing_names():
    """The names in stack.env, refusing anything start.sh would refuse; values stay unprinted."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        sys.exit(f"eval: cannot read {path}: {error.strerror}")
    with os.fdopen(descriptor, "rb") as stream:
        original = stream.read()
    try:
        text = original.decode("ascii")
    except UnicodeDecodeError:
        sys.exit(f"eval: {path} must hold ASCII NAME=value lines")
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    seen = []
    for number, line in enumerate(lines, start=1):
        match = LINE.fullmatch(line)
        if match is None:
            sys.exit(f"eval: {path}:{number}: expected NAME=value")
        name, value = match.groups()
        if name not in names or name in seen:
            sys.exit(f"eval: {path}:{number}: unexpected or repeated name {name}")
        if VALUE.fullmatch(value) is None:
            sys.exit(f"eval: {path}:{number}: {name} has a malformed value")
        seen.append(name)
    return original, seen


if not os.path.lexists(path):
    partial = write_partial(generated(names))
    # Hard-link the finished file into place: the link fails on any existing
    # entry, and an interrupted run never leaves a partial stack.env.
    try:
        os.link(partial, path, follow_symlinks=False)
    except FileExistsError:
        sys.exit(f"eval: {path} appeared while init ran; run init again")
    finally:
        os.unlink(partial)
    print(f"eval: wrote {path}")
    sys.exit(0)

original, seen = existing_names()
missing = [name for name in names if name not in seen]
if not missing:
    print(f"eval: {path} holds every name; nothing to add")
    sys.exit(0)
# Existing lines stay byte-identical; the new lines follow them.
separator = b"\n" if original and not original.endswith(b"\n") else b""
partial = write_partial(original + separator + generated(missing))
try:
    os.replace(partial, path)
except BaseException:
    os.unlink(partial)
    raise
print(f"eval: added {' '.join(missing)} to {path}")
PY
