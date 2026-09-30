#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""In-image capture, extract, marker probe, and first recovered broker listing."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


def _listing(chat_dir: Path, chat_id: str) -> dict:
    os.environ["BASE_DATA_DIR"] = str(chat_dir)
    os.environ["USER_DATA_BASE_PATH"] = str(chat_dir)
    os.environ.pop("DOCKER_HOST", None)
    os.environ.pop("DOCKER_CONTEXT", None)
    for candidate in (Path("/app"), Path.cwd()):
        if (candidate / "outputs_broker.py").is_file():
            sys.path.insert(0, str(candidate))
            break
    import docker_manager
    import outputs_broker

    docker_manager.BASE_DATA_DIR = chat_dir
    return outputs_broker.OutputsBroker().reconcile(chat_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture")
    capture.add_argument("source")
    capture.add_argument("archive")
    extract = sub.add_parser("extract")
    extract.add_argument("archive")
    extract.add_argument("target")
    probe = sub.add_parser("probe")
    probe.add_argument("path")
    listing = sub.add_parser("listing")
    listing.add_argument("chat_dir")
    listing.add_argument("chat_id")
    args = parser.parse_args(argv)
    if args.command == "listing":
        payload = _listing(Path(args.chat_dir), args.chat_id)
        sys.stdout.write(json.dumps(payload, separators=(",", ":")))
        return 0
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import recovery_fs

    if args.command == "capture":
        recovery_fs.capture_tree(Path(args.source), Path(args.archive))
        return 0
    if args.command == "extract":
        recovery_fs.extract_tree(Path(args.archive), Path(args.target))
        return 0
    if Path(args.path).exists():
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
