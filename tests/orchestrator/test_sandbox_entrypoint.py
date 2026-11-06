# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""The sandbox entrypoint is a repository file the root Dockerfile copies (#681).

It used to be one `RUN printf '...\\` with continuation lines; the Dockerfile
parser drops every line that starts with `#` inside a continuation, so the
CLAUDE.md the entrypoint writes lost all its Markdown headings.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile"
SCRIPT_RELATIVE = "computer-use-server/bin/sandbox-entrypoint.sh"
SCRIPT = ROOT / SCRIPT_RELATIVE
TARGET = "/home/assistant/.entrypoint.sh"
HEADINGS = [
    "# Environment",
    "## File Locations",
    "## Output Rules",
    "## Plan Mode — MANDATORY",
    "## Verification",
    "## Self-Improvement",
    "## Useful Commands",
]


def _instructions(text: str) -> list[str]:
    """Logical Dockerfile instructions, continuation lines joined as Docker does."""
    instructions: list[str] = []
    pending: list[str] = []
    for raw in text.splitlines():
        if re.match(r"^\s*#", raw) or (not raw.strip() and not pending):
            continue
        if raw.endswith("\\"):
            pending.append(raw[:-1])
            continue
        pending.append(raw)
        instructions.append(" ".join(part.strip() for part in pending if part.strip()))
        pending = []
    assert not pending, f"Dockerfile ends inside a continuation: {pending[:1]}"
    return instructions


def test_dockerfile_copies_the_entrypoint_file() -> None:
    instructions = _instructions(DOCKERFILE.read_text(encoding="utf-8"))
    copy = f"COPY --chown=assistant:assistant {SCRIPT_RELATIVE} {TARGET}"
    chmod = f"RUN chmod +x {TARGET}"
    assert copy in instructions, "root Dockerfile does not COPY the entrypoint file"
    assert chmod in instructions, "root Dockerfile does not chmod +x the entrypoint"
    assert instructions.index(copy) < instructions.index(chmod)
    assert instructions.index(chmod) < instructions.index(f'ENTRYPOINT ["{TARGET}"]')
    writers = [
        line for line in instructions
        if line.startswith("RUN ") and "printf" in line and ".entrypoint.sh" in line
    ]
    assert writers == [], f"a RUN printf still writes the entrypoint: {writers[0][:80]}"


def test_entrypoint_writes_claude_md_headings() -> None:
    lines = SCRIPT.read_text(encoding="utf-8").split("\n")
    assert lines[0] == "#!/bin/bash"
    start = lines.index("cat > /home/assistant/.claude/CLAUDE.md << CLAUDEMDEOF")
    end = lines.index("CLAUDEMDEOF", start)
    body = lines[start + 1 : end]
    assert body[0] == HEADINGS[0]
    missing = [heading for heading in HEADINGS if heading not in body]
    assert missing == [], f"CLAUDE.md heredoc lacks headings {missing}"


def test_entrypoint_parses_under_bash() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_entrypoint_is_executable_in_git() -> None:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-s", "--", SCRIPT_RELATIVE],
        capture_output=True, text=True, check=True,
    )
    fields = result.stdout.split()
    assert fields, f"{SCRIPT_RELATIVE} is not tracked by git"
    assert fields[0] == "100755", f"{SCRIPT_RELATIVE} git mode is {fields[0]}"
