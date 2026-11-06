# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""The prompt describes what `view` does for a text model (DankerMu/open-webui#727).

The prompt said `view` returns "an AI description for text models". It does
not: the Open WebUI tool refuses an image for a chat model whose capabilities
mark vision as off (error naming `bash_tool`), and the server's `view` only
returns the image. The sentence now states that condition and the fallbacks
without naming a skill, since #720 hides `describe-image` when Vision is not
configured and this line is in every rendered prompt.

Seams: the prompt constant and builder; nothing is stubbed.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "computer-use-server"))

import system_prompt  # noqa: E402

FIRST = (
    "IMPORTANT: When you create or save image files (screenshots, charts, plots — any "
    ".png, .jpg, .gif, .webp), ALWAYS call the `view` tool on them to actually see the content."
)
VIEW = (
    "The `view` tool returns the image unless the chat model's capabilities mark vision as "
    "off; then it returns an error instead, and an image-description skill (if one is listed "
    "in <available_skills>) or `bash_tool` (`file`, `ffprobe`) is the way to inspect the image."
)
LAST = "Without `view`, you are blind. Never describe an image you haven't viewed."
USER_SKILLS_XML = "<available_skills>\n<skill>\n<name>\nexample\n</name>\n</skill>\n</available_skills>"


@pytest.mark.parametrize(
    "prompt",
    (
        system_prompt.SYSTEM_PROMPT_TEMPLATE,
        system_prompt.build_system_prompt(skills_xml=USER_SKILLS_XML),
    ),
    ids=("template", "built"),
)
def test_view_line_states_the_error_and_the_fallbacks(prompt):
    lines = [line for line in prompt.splitlines() if "ALWAYS call the `view` tool" in line]

    assert lines == [f"{FIRST} {VIEW} {LAST}"]
    assert prompt.count(VIEW) == 1
    assert "AI description" not in prompt
    assert "describe-image" not in lines[0]
