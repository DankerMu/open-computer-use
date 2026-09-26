#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Independent fake id/stat/date CLIs driven by FAKE_DOCKER_STATE."""

from __future__ import annotations

import calendar
import os
from pathlib import Path
import stat
import sys
import time


def state_dir() -> Path:
    raw = os.environ.get("FAKE_DOCKER_STATE", "")
    if not raw:
        sys.stderr.write("FAKE_DOCKER_STATE is required\n")
        sys.exit(2)
    path = Path(raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def log(argv: list[str], state: Path) -> None:
    with (state / "ops.log").open("a", encoding="utf-8") as stream:
        stream.write(" ".join(argv) + "\n")


def fake_id(_state: Path, argv: list[str]) -> int:
    if argv not in ([], ["-u"]):
        sys.stderr.write("unsupported id command\n")
        return 1
    override = os.environ.get("FAKE_ID_UID", "0")
    sys.stdout.write(override + "\n")
    return 0


def fake_stat(_state: Path, argv: list[str]) -> int:
    if len(argv) != 3 or argv[0] != "-c" or argv[1] != "%a":
        sys.stderr.write("unsupported stat command\n")
        return 1
    path = Path(argv[2])
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        sys.stderr.write(f"stat: {path}: {exc.strerror}\n")
        return 1
    sys.stdout.write(f"{mode:o}\n")
    return 0


def fake_date(state: Path, argv: list[str]) -> int:
    if argv[:1] != ["-u"]:
        sys.stderr.write("unsupported date command\n")
        return 1
    rest = argv[1:]
    now_path = state / "now-epoch"
    if now_path.exists():
        now = int(now_path.read_text(encoding="utf-8").strip())
    else:
        now = int(time.time())
    if rest == ["+%s"]:
        sys.stdout.write(f"{now}\n")
        return 0
    if rest == ["+%Y-%m-%dT%H:%M:%SZ"]:
        sys.stdout.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)) + "\n")
        return 0
    if len(rest) == 3 and rest[0] == "-d" and rest[2] == "+%s":
        try:
            parsed = time.strptime(rest[1], "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            sys.stderr.write("date: invalid date\n")
            return 1
        sys.stdout.write(f"{calendar.timegm(parsed)}\n")
        return 0
    sys.stderr.write("unsupported date command\n")
    return 1


def command_name(argv0: str) -> str:
    return Path(argv0).name


def main(argv: list[str]) -> int:
    state = state_dir()
    tool = command_name(argv[0] if argv else sys.argv[0])
    args = argv[1:] if argv and command_name(argv[0]) == tool else argv
    if tool == "fakeposix.py":
        if not args:
            sys.stderr.write("missing posix command\n")
            return 1
        tool = args[0]
        args = args[1:]
    log([tool, *args], state)
    if tool == "id":
        return fake_id(state, args)
    if tool == "stat":
        return fake_stat(state, args)
    if tool == "date":
        return fake_date(state, args)
    sys.stderr.write(f"unsupported posix command: {tool}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
