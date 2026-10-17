# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Reusable integration MCP helpers; pytest fixtures belong in conftest.py."""
from __future__ import annotations

import httpx


def mcp_request(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    """Build a JSON-RPC 2.0 request envelope matching the MCP spec."""
    payload: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        payload["params"] = params
    return payload


def parse_mcp_response(resp: httpx.Response) -> dict:
    """Return the decoded JSON-RPC envelope from either an SSE or JSON body.

    FastMCP's streamable_http_app picks the response format based on the
    request's Accept header — we send both, and the server tends to reply
    with text/event-stream. The single data: line carries the actual envelope.
    """
    import json

    ctype = resp.headers.get("content-type", "")
    body = resp.text
    # Detect SSE only by Content-Type or by an `event:`/`data:` line at the
    # *start* of a line. The original substring-anywhere check was too loose:
    # a perfectly valid JSON-RPC payload like {"result":{"data":"x"}} contains
    # the literal `"data:` and would be misrouted to the SSE branch.
    is_sse = (
        "text/event-stream" in ctype
        or body.lstrip().startswith("event:")
        or any(line.startswith("data:") for line in body.splitlines())
    )
    if is_sse and "text/event-stream" in ctype:
        # Trust Content-Type when present — only then walk SSE frames.
        for line in body.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:"):].strip())
        raise AssertionError(f"SSE body had no data: line. Raw:\n{body[:500]}")
    return json.loads(body)


def call_mcp(client: httpx.Client, chat_id: str, method: str,
             params: dict | None = None, req_id: int = 1) -> dict:
    """POST /mcp with the X-Chat-Id header after client auth defaults apply."""
    resp = client.post(
        "/mcp",
        json=mcp_request(method, params, req_id),
        headers={"X-Chat-Id": chat_id},
    )
    return {"status": resp.status_code, "headers": dict(resp.headers),
            "envelope": parse_mcp_response(resp) if resp.status_code == 200 else None,
            "body": resp.text}
