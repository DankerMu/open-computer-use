# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Capture authenticated production preview responses for the browser recording harness."""

import base64
import hashlib
import hmac
import os
import json
import re
import sys
from pathlib import Path

from test_preview_prefix import INTERNAL, MCP_KEY, _client, _isolated_app


QUERIES = (
    "",
    "?embed=files",
    "?embed=files&embed=files",
    "?embed=browser",
    "?embed=browser&embed=browser",
    "?embed=terminal",
    "?embed=unknown",
    "?embed=office",
    "?embed=office&embed=office",
    "?embed=office&embed=files",
    "?embed=office&office_fixture=absent",
    "?embed=office&office_fixture=blank",
)
PREFIXES = ("", "/ocu", "/tools/ocu")
HEADERS = ("content-type", "cache-control", "content-security-policy", "x-content-type-options")


def main():
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("preview capture requires Python 3.12")
    if len(sys.argv) != 4:
        raise SystemExit("usage: _preview_capture.py CHAT_ID OUTPUT_JSON DOCSERVER_ORIGIN")
    chat_id, output, docserver_origin = sys.argv[1:]
    jwt_secret = "ocu-preview-office-jwt-secret-canary"
    model_key = "ocu-preview-model-key-canary"
    ticket_key = hmac.new(INTERNAL.encode(), b"ocu-office-source-ticket", hashlib.sha256).digest()
    secret_canaries = [
        INTERNAL, MCP_KEY, jwt_secret, model_key, ticket_key.hex(),
        base64.urlsafe_b64encode(ticket_key).decode().rstrip("="),
    ]
    os.environ["ANTHROPIC_API_KEY"] = model_key
    captures = []
    for prefix in PREFIXES:
        with _isolated_app(prefix) as loaded:
            from office import config

            os.environ.update({
                config.OCU_OFFICE_DOCSERVER_ORIGIN: docserver_origin,
                config.OCU_OFFICE_JWT_SECRET: jwt_secret,
                config.OCU_OFFICE_SELF_URL: "http://ocu-internal.office.test:8081",
            })
            client = _client(loaded)
            authorization = {"Authorization": f"Bearer {INTERNAL}"}
            for query in QUERIES:
                os.environ[config.OCU_OFFICE_DOCSERVER_URL] = "http://docserver-internal.office.test:8080"
                if query.endswith("office_fixture=absent"):
                    os.environ.pop(config.OCU_OFFICE_DOCSERVER_URL)
                elif query.endswith("office_fixture=blank"):
                    os.environ[config.OCU_OFFICE_DOCSERVER_URL] = " \t\n"
                response = client.get(f"/preview/{chat_id}{query}", headers=authorization)
                if response.status_code != 200:
                    raise RuntimeError(f"preview capture failed for {prefix}{query}: {response.status_code}")
                if any(secret in response.text or secret in response.headers.get("content-security-policy", "")
                       for secret in secret_canaries):
                    raise RuntimeError("preview response exposed synthetic signing material")
                if query == "?embed=office":
                    if f'officeDocserverOrigin: {json.dumps(docserver_origin)}' not in response.text:
                        raise RuntimeError("Office capture omitted the configured stand-in origin")
                    if f"frame-src {docserver_origin};" not in response.headers.get("content-security-policy", ""):
                        raise RuntimeError("Office capture omitted its production frame policy")
                elif docserver_origin in response.text or docserver_origin in response.headers.get("content-security-policy", ""):
                    raise RuntimeError("non-Office capture exposed the Office origin")
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
                if query in ("?embed=browser", "?embed=terminal", "?embed=office"):
                    another = client.get(f"/preview/{chat_id}{query}", headers=authorization)
                    if another.status_code != 200:
                        raise RuntimeError("repeated runtime preview capture failed")
                    first_nonce = re.search(r"'nonce-([^']+)'", response.headers.get("content-security-policy", ""))
                    next_nonce = re.search(r"'nonce-([^']+)'", another.headers.get("content-security-policy", ""))
                    if not first_nonce or not next_nonce:
                        raise RuntimeError("framed preview capture omitted a response nonce")
                    if first_nonce and next_nonce and first_nonce.group(1) == next_nonce.group(1):
                        raise RuntimeError("runtime preview reused a response nonce")
    Path(output).write_text(json.dumps({
        "responses": captures, "secretCanaries": secret_canaries,
    }), encoding="utf-8")


if __name__ == "__main__":
    main()
