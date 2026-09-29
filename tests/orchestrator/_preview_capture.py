# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Capture authenticated production preview responses for the browser recording harness."""

import json
import re
import sys
from pathlib import Path

from test_preview_prefix import INTERNAL, _client, _isolated_app


QUERIES = (
    "",
    "?embed=files",
    "?embed=files&embed=files",
    "?embed=browser",
    "?embed=browser&embed=browser",
    "?embed=terminal",
    "?embed=unknown",
)
PREFIXES = ("", "/ocu", "/tools/ocu")
HEADERS = ("content-type", "cache-control", "content-security-policy", "x-content-type-options")


def main():
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("preview capture requires Python 3.12")
    if len(sys.argv) != 3:
        raise SystemExit("usage: _preview_capture.py CHAT_ID OUTPUT_JSON")
    chat_id, output = sys.argv[1:]
    captures = []
    for prefix in PREFIXES:
        with _isolated_app(prefix) as loaded:
            client = _client(loaded)
            authorization = {"Authorization": f"Bearer {INTERNAL}"}
            for query in QUERIES:
                response = client.get(f"/preview/{chat_id}{query}", headers=authorization)
                if response.status_code != 200:
                    raise RuntimeError(f"preview capture failed for {prefix}{query}: {response.status_code}")
                if INTERNAL in response.text:
                    raise RuntimeError("preview response exposed fixture authorization")
                captures.append(
                    {
                        "prefix": prefix,
                        "query": query,
                        "status": response.status_code,
                        "headers": {
                            name: response.headers[name]
                            for name in HEADERS
                            if name in response.headers
                        },
                        "body": response.text,
                    }
                )
                if query in ("?embed=browser", "?embed=terminal"):
                    another = client.get(f"/preview/{chat_id}{query}", headers=authorization)
                    if another.status_code != 200:
                        raise RuntimeError("repeated runtime preview capture failed")
                    first_nonce = re.search(r"'nonce-([^']+)'", response.headers.get("content-security-policy", ""))
                    next_nonce = re.search(r"'nonce-([^']+)'", another.headers.get("content-security-policy", ""))
                    if first_nonce and next_nonce and first_nonce.group(1) == next_nonce.group(1):
                        raise RuntimeError("runtime preview reused a response nonce")
    Path(output).write_text(json.dumps(captures), encoding="utf-8")


if __name__ == "__main__":
    main()
