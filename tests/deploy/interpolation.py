# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Narrow Compose interpolation for fake config resolution only."""

from __future__ import annotations

import re


INTERPOLATION = re.compile(
    r"\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)(:?[-?])([^}]*)\}|\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)|\$\{([^}]*)\}"
)


class UnsupportedInterpolation(ValueError):
    """Compose placeholder uses syntax this fake does not implement."""


def interpolate_value(value, env):
    if isinstance(value, str):
        for token in re.findall(r"\$\$|\$\{[^}]*\}", value):
            if token == "$$":
                continue
            inner = token[2:-1]
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", inner):
                continue
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(:?[-?]).*", inner):
                continue
            raise UnsupportedInterpolation(token)

        def replace(match):
            token = match.group(0)
            if token == "$$":
                return "$"
            name = match.group(1) or match.group(4) or match.group(5)
            if name is None:
                raise UnsupportedInterpolation(token)
            operator = match.group(2)
            default = match.group(3)
            present = name in env
            current = env.get(name, "")
            if operator is None:
                return current if present else ""
            if operator == "-":
                return current if present else (default or "")
            if operator == ":-":
                return current if present and current != "" else (default or "")
            if operator == "?":
                if present:
                    return current
                raise UnsupportedInterpolation(token)
            if operator == ":?":
                if present and current != "":
                    return current
                raise UnsupportedInterpolation(token)
            raise UnsupportedInterpolation(token)

        return INTERPOLATION.sub(replace, value)
    if isinstance(value, list):
        return [interpolate_value(item, env) for item in value]
    if isinstance(value, dict):
        return {key: interpolate_value(item, env) for key, item in value.items()}
    return value
