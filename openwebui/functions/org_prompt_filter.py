# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""
title: Company System Prompt
author: Open Computer Use Contributors
version: 1.0.0
required_open_webui_version: 0.5.17
description: Puts the deployment's company system prompt at the front of every chat's system message.

The deployment mounts its prompts/ directory read-only at /etc/ocu/prompts. The
filter reads system-prompt.md there on every chat request, so an edit applies to
the next message. A directory mount, not a file mount: editors save by renaming
a new file over the old one, and a file bind mount keeps the old inode.

The text goes in front of the first system message, or becomes one, so the
order does not depend on filter priority: the Computer Use filter appends its
prompt after it. Open WebUI applies the model's own system prompt after the
filters and puts it first. Open WebUI prompt variables such as {{CURRENT_DATE}}
are not substituted here: Open WebUI substitutes them before filters run.

An empty or whitespace-only file injects nothing. A missing, oversized or
non-UTF-8 file raises OrgPromptError, which Open WebUI shows as the chat's
error. deploy/up.sh runs read_org_prompt on the host file before it starts the
stack, so the two agree on what is a usable file.
"""

import os
import stat
from pathlib import Path
from typing import Optional

PROMPT_FILE = Path("/etc/ocu/prompts/system-prompt.md")
MAX_BYTES = 64 * 1024


class OrgPromptError(Exception):
    """The company prompt file cannot be used; the message names the file."""


def read_org_prompt(path: Path) -> str:
    """Return the file's text as written, or "" when it holds only whitespace."""
    try:
        mode = os.stat(path).st_mode
    except FileNotFoundError:
        raise OrgPromptError(f"company system prompt {path} is missing") from None
    except OSError as exc:
        raise OrgPromptError(f"company system prompt {path} is unreadable: {exc.strerror}") from None
    # Checked before opening: opening a FIFO would block the request.
    if not stat.S_ISREG(mode):
        raise OrgPromptError(f"company system prompt {path} is not a regular file")
    try:
        with open(path, "rb") as stream:
            data = stream.read(MAX_BYTES + 1)
    except OSError as exc:
        raise OrgPromptError(f"company system prompt {path} is unreadable: {exc.strerror}") from None
    if len(data) > MAX_BYTES:
        raise OrgPromptError(f"company system prompt {path} is larger than {MAX_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise OrgPromptError(f"company system prompt {path} is not valid UTF-8") from None
    return text if text.strip() else ""


class Filter:
    def inlet(self, body: dict, __metadata__: Optional[dict] = None) -> dict:
        """Prepend the company prompt to the system message of every chat request."""
        prompt = read_org_prompt(PROMPT_FILE)
        messages = body.get("messages")
        if not prompt or not messages:
            return body
        for message in messages:
            if message.get("role") == "system":
                content = message.get("content")
                if isinstance(content, list):
                    message["content"] = [{"type": "text", "text": prompt}, *content]
                elif content:
                    message["content"] = f"{prompt}\n\n{content}"
                else:
                    message["content"] = prompt
                return body
        messages.insert(0, {"role": "system", "content": prompt})
        return body
