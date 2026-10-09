# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Input validation shared by bootstrap and deployment admission."""

from urllib.parse import urlsplit


def origin(name: str, value: str) -> tuple[str, str, int]:
    error = f"{name} must be an absolute HTTP(S) origin without path, credentials, query, fragment, or trailing slash"
    if any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in value):
        raise SystemExit(error)
    if any(marker in value for marker in ("@", "?", "#")):
        raise SystemExit(error)
    try:
        parsed = urlsplit(value)
        parsed_port = parsed.port
    except ValueError:
        raise SystemExit(error) from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or value.endswith("/")
        or parsed.netloc != parsed.netloc.lower()
    ):
        raise SystemExit(error)
    if parsed_port is not None and not (1 <= parsed_port <= 65535):
        raise SystemExit(error)
    return parsed.scheme, parsed.hostname, parsed_port or (443 if parsed.scheme == "https" else 80)


def port(name: str, value: str) -> int:
    try:
        number = int(value) if value.isascii() and value.isdecimal() else 0
    except ValueError:
        number = 0
    if not 1 <= number <= 65535:
        raise SystemExit(f"{name} must be a decimal port in 1–65535")
    return number
