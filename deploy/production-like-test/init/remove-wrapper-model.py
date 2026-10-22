#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Remove the per-model Computer Use wrapper from an upgraded deployment (#590).

The Computer Use tool now reaches every model through the global filter, so the
"<model> (Computer Use)" row that older initializers created is obsolete. Run
this once, after open-webui-init has stored the shipped filter. It refuses to
delete anything until the stored computer_use_filter equals the shipped file
byte for byte, then deletes only rows whose name ends with " (Computer Use)"
and whose meta.toolIds contains ai_computer_use. A second run removes nothing.

Inputs come from the environment, as for run-init.sh: WEBUI_URL, ADMIN_EMAIL,
ADMIN_PASSWORD and OCU_INIT_SOURCE_ROOT (default /app/source). The password and
the session token are never printed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

FILTER_ID = "computer_use_filter"
TOOL_ID = "ai_computer_use"
WRAPPER_SUFFIX = " (Computer Use)"


class Refusal(Exception):
    """A precondition or request failed; the script stops before any further change."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect would carry the admin session token to another origin.
    def redirect_request(self, *_args, **_kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def _call(base: str, method: str, path: str, token: str = "", payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(base + path, data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with _OPENER.open(request, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        raise Refusal(f"{method} {path} failed with HTTP {error.code}", error.code) from None
    except (urllib.error.URLError, TimeoutError) as error:
        raise Refusal(f"{method} {path} failed: {type(error).__name__}") from None
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise Refusal(f"{method} {path} did not return JSON") from None


def _is_wrapper(row: object) -> bool:
    if not isinstance(row, dict) or not isinstance(row.get("id"), str):
        return False
    name = row.get("name")
    meta = row.get("meta")
    tool_ids = meta.get("toolIds") if isinstance(meta, dict) else None
    return (
        isinstance(name, str)
        and name.endswith(WRAPPER_SUFFIX)
        and isinstance(tool_ids, list)
        and TOOL_ID in tool_ids
    )


def remove_wrappers(base: str, email: str, password: str, shipped: bytes) -> None:
    try:
        session = _call(
            base, "POST", "/api/v1/auths/signin", payload={"email": email, "password": password}
        )
    except Refusal as refusal:
        hint = ""
        if refusal.status is not None and 400 <= refusal.status < 500:
            hint = (
                "; if the admin password was changed in the UI, set it back to "
                "ADMIN_PASSWORD from the runtime configuration first"
            )
        raise Refusal(f"admin sign-in failed ({refusal}){hint}") from None
    token = session.get("token") if isinstance(session, dict) else None
    if not isinstance(token, str) or not token:
        raise Refusal("admin sign-in failed (no session token returned)")

    try:
        stored = _call(base, "GET", f"/api/v1/functions/id/{FILTER_ID}", token)
    except Refusal as refusal:
        stored = None
        reason = str(refusal)
    else:
        reason = "its content differs from the shipped filter"
    content = stored.get("content") if isinstance(stored, dict) else None
    if not isinstance(content, str) or content.encode("utf-8") != shipped:
        raise Refusal(
            f"the stored {FILTER_ID} is not the shipped filter ({reason}); "
            "delete the init marker and re-run open-webui-init first"
        )

    rows = _call(base, "GET", "/api/v1/models/export", token)
    if not isinstance(rows, list):
        raise Refusal("GET /api/v1/models/export did not return a list")
    wrappers = [row for row in rows if _is_wrapper(row)]
    if not wrappers:
        print("nothing to remove")
        return
    for row in wrappers:
        deleted = _call(base, "POST", "/api/v1/models/model/delete", token, {"id": row["id"]})
        if deleted is not True:
            raise Refusal(f"could not remove {row['id']}")
        print(f"removed {row['id']} ({row['name']})")


def main() -> int:
    base = os.environ.get("WEBUI_URL", "http://open-webui:8080").rstrip("/")
    email = os.environ.get("ADMIN_EMAIL", "")
    password = os.environ.get("ADMIN_PASSWORD", "")
    filter_path = (
        Path(os.environ.get("OCU_INIT_SOURCE_ROOT", "/app/source"))
        / "functions"
        / "computer_link_filter.py"
    )
    try:
        if not email or not password:
            raise Refusal("ADMIN_EMAIL and ADMIN_PASSWORD are required")
        try:
            shipped = filter_path.read_bytes()
        except OSError:
            raise Refusal(f"cannot read the shipped filter at {filter_path}") from None
        remove_wrappers(base, email, password, shipped)
    except Refusal as refusal:
        print(f"remove-wrapper-model: ERROR: {refusal}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
