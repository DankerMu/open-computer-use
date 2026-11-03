# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Input validation shared by bootstrap and deployment admission."""

import os
from pathlib import Path
import re
from urllib.parse import urlsplit

# The host whose zone every container and sandbox follows (#708). Only tests
# point this elsewhere; no runtime setting can.
HOST_ROOT = Path("/")
ZONE_NAME = re.compile(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*")


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


def host_time_zone(root=None) -> str:
    """The host's IANA zone name, from the /etc/localtime link or else /etc/timezone."""
    root = HOST_ROOT if root is None else root
    # Compared after resolving both, so a zoneinfo directory that is itself a
    # link still matches; on Linux both are the literal paths.
    zoneinfo = os.path.realpath(root / "usr/share/zoneinfo")
    target = os.path.realpath(root / "etc/localtime")
    if target.startswith(zoneinfo + os.sep):
        source, name = "/etc/localtime", target[len(zoneinfo) + 1:]
    else:
        source = "/etc/timezone"
        try:
            lines = (root / "etc/timezone").read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            raise ValueError(
                "/etc/localtime is not a link into /usr/share/zoneinfo and /etc/timezone is missing"
            ) from None
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"/etc/timezone is unreadable: {exc}") from None
        name = lines[0].strip() if lines else ""
    if not ZONE_NAME.fullmatch(name):
        raise ValueError(f"{source} gives {name!r}, which is not a zone name")
    if not os.path.isfile(os.path.join(zoneinfo, name)):
        raise ValueError(f"{source} gives {name}, which has no zone file in /usr/share/zoneinfo")
    return name
