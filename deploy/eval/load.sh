#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
# Load the four ocu-eval images from a release package and bind them for start.sh.
# Usage: load.sh <package dir>
# Every archive is checked against release.json before the first docker load.
# A reference that already exists with another image id is refused, so no tag on
# the host is ever moved. References absent before the load are appended to
# ${EVAL_ROOT}/loaded-images.txt for cleanup; ${EVAL_ROOT}/images.env binds each
# reference to its RUNTIME_IMAGE_VARS name. Docker's root keeps a 15 GiB floor,
# and before each load also room for that archive unpacked above the floor.
set -euo pipefail
umask 077

EVAL_SOURCE="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=deploy/eval/common.sh
. "$EVAL_SOURCE/deploy/eval/common.sh"
trap eval_cleanup EXIT

if [[ $# -ne 1 ]]; then
    eval_fail "usage: load.sh <package dir>"
fi
if [[ ! -d "$1" || ! -f "$1/release.json" ]]; then
    eval_fail "$1 is not a release package: release.json is missing"
fi
PACKAGE="$(cd "$1" && pwd -P)"
eval_require_root
eval_require_space 0
EVAL_TMP=$(mktemp -d "${TMPDIR:-/tmp}/ocu-eval.XXXXXX")

# plan: verify every archive and the host's existing references, then print one
# "<load|present> <variable> <reference> <archive> <unpack bytes>" line per role.
# The unpack estimate is release.py's IMPORT_UNPACK_FACTOR times the archive size.
# verify: every reference now resolves to the identity release.json declares.
eval_images() {
    # shellcheck disable=SC2086 # the role list splits on purpose
    python3 -I - "$EVAL_SOURCE/deploy" "$PACKAGE" "$1" $EVAL_ROLES <<'PY'
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
import release

package = Path(sys.argv[2])
mode = sys.argv[3]
rows = []
try:
    payload = release.load_inventory(package / "release.json")
    release.pin_local_docker_host()
    for role in sys.argv[4:]:
        record = payload["images"][role]
        reference = record["reference"]
        identities = release.accepted_image_identities(
            record["configuration_digest"], record.get("descriptor_digests", [])
        )
        if mode == "verify":
            if release.existing_reference(reference, identities=identities) is None:
                raise release.ReleaseError(f"{role}: {reference} is missing after the load")
            continue
        archive = release.relative_archive_path(package, record["archive"]["path"])
        release.require_regular_file(archive)
        if release.sha256_file(archive) != record["archive"]["sha256"]:
            raise release.ReleaseError(f"{role}: archive checksum mismatch for {archive}")
        declared = release.alias_set(reference)
        mappings = release.inspect_archive_bytes(archive)
        if not declared & set(mappings):
            raise release.ReleaseError(f"{role}: archive does not contain {reference}")
        for tag, info in mappings.items():
            if tag not in declared:
                raise release.ReleaseError(f"{role}: archive contains undeclared reference {tag}")
            if info["digest"] != record["configuration_digest"]:
                raise release.ReleaseError(f"{role}: archive digest for {tag} does not match release.json")
        present = release.existing_reference(reference, identities=identities) is not None
        rows.append(("present" if present else "load", release.RUNTIME_IMAGE_VARS[role],
                     reference, str(archive),
                     str(release.IMPORT_UNPACK_FACTOR * archive.stat().st_size)))
except release.ReleaseError as cop:
    sys.exit(f"eval: {cop}")
for row in rows:
    print("\t".join(row))
PY
}

eval_images plan >"$EVAL_TMP/plan" || exit 1
: >"$EVAL_TMP/images.env"
while IFS=$'\t' read -r action name reference archive needed; do
    if [[ "$action" == "load" ]]; then
        eval_require_space "$needed" "$archive"
        # Recorded before the load, so an interrupted load is still cleaned up.
        if ! grep -Fxq -- "$reference" "$EVAL_ROOT/loaded-images.txt" 2>/dev/null; then
            printf '%s\n' "$reference" >>"$EVAL_ROOT/loaded-images.txt"
        fi
        printf '%s\n' "eval: loading $reference"
        docker load -i "$archive" </dev/null
    else
        printf '%s\n' "eval: $reference is already present with the release identity"
    fi
    printf '%s=%s\n' "$name" "$reference" >>"$EVAL_TMP/images.env"
done <"$EVAL_TMP/plan"
eval_images verify >/dev/null || exit 1
cp "$EVAL_TMP/images.env" "$EVAL_ROOT/images.env.partial.$$"
mv -f "$EVAL_ROOT/images.env.partial.$$" "$EVAL_ROOT/images.env"
printf '%s\n' "eval: wrote $EVAL_ROOT/images.env"
