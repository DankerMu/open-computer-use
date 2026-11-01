# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Narrow Compose interpolation for fake config resolution only."""

from __future__ import annotations

import re


NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
EXPRESSION = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?])(.*))?", re.DOTALL)


class UnsupportedInterpolation(ValueError):
    """Compose placeholder uses syntax this fake does not implement."""


def closing_brace(text, start):
    """Index of the brace closing the `${` at start; defaults may nest more `${`."""
    depth = 0
    index = start
    while index < len(text):
        if text.startswith("${", index):
            depth += 1
            index += 2
            continue
        if text[index] == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise UnsupportedInterpolation(text[start:])


def resolve_expression(token, env):
    match = EXPRESSION.fullmatch(token[2:-1])
    if match is None:
        raise UnsupportedInterpolation(token)
    name, operator, operand = match.groups()
    present = name in env
    current = env.get(name, "")
    if operator is None:
        return current
    if operator == "?":
        if present:
            return current
        raise UnsupportedInterpolation(token)
    if operator == ":?":
        if present and current != "":
            return current
        raise UnsupportedInterpolation(token)
    # Compose substitutes a default before it checks the name, so a required
    # name inside a default refuses even when the outer name is set.
    default = substitute(operand, env)
    if operator == "-":
        return current if present else default
    return current if present and current != "" else default


def substitute(text, env):
    parts = []
    index = 0
    while (dollar := text.find("$", index)) != -1:
        parts.append(text[index:dollar])
        if text.startswith("$$", dollar):
            parts.append("$")
            index = dollar + 2
        elif text.startswith("${", dollar):
            end = closing_brace(text, dollar)
            parts.append(resolve_expression(text[dollar : end + 1], env))
            index = end + 1
        elif bare := NAME.match(text, dollar + 1):
            parts.append(env.get(bare.group(0), ""))
            index = bare.end()
        else:
            parts.append("$")
            index = dollar + 1
    parts.append(text[index:])
    return "".join(parts)


def interpolate_value(value, env):
    if isinstance(value, str):
        return substitute(value, env)
    if isinstance(value, list):
        return [interpolate_value(item, env) for item in value]
    if isinstance(value, dict):
        return {key: interpolate_value(item, env) for key, item in value.items()}
    return value
