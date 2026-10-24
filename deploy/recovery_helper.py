#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""In-image scan, capture, extract, marker probe, and read-only broker revision probe."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


def _revision(chat_dir: Path, chat_id: str) -> dict:
    os.environ["BASE_DATA_DIR"] = str(chat_dir)
    os.environ.pop("DOCKER_HOST", None)
    os.environ.pop("DOCKER_CONTEXT", None)
    for candidate in (Path("/app"), Path.cwd()):
        if (candidate / "outputs_broker.py").is_file():
            sys.path.insert(0, str(candidate))
            break
    import docker_manager
    import outputs_broker

    docker_manager.BASE_DATA_DIR = chat_dir
    return {"revision": outputs_broker.OutputsBroker().current_revision(chat_id)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan")
    scan.add_argument("--links", required=True, choices=("confined", "verbatim"))
    scan.add_argument("--live", action="store_true")
    scan.add_argument("source")
    capture = sub.add_parser("capture")
    capture.add_argument("--links", required=True, choices=("confined", "verbatim"))
    capture.add_argument("source")
    capture.add_argument("archive")
    extract = sub.add_parser("extract")
    extract.add_argument("--links", required=True, choices=("confined", "verbatim"))
    extract.add_argument("archive")
    extract.add_argument("target")
    probe = sub.add_parser("probe")
    probe.add_argument("path")
    revision = sub.add_parser("revision")
    revision.add_argument("chat_dir")
    revision.add_argument("chat_id")
    args = parser.parse_args(argv)
    if args.command == "revision":
        payload = _revision(Path(args.chat_dir), args.chat_id)
        sys.stdout.write(json.dumps(payload, separators=(",", ":")))
        return 0
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import recovery_fs

    if args.command == "scan":
        recovery_fs.scan_tree(Path(args.source), links=args.links, live=args.live)
        return 0
    if args.command == "capture":
        recovery_fs.capture_tree(Path(args.source), Path(args.archive), links=args.links)
        return 0
    if args.command == "extract":
        recovery_fs.extract_tree(Path(args.archive), Path(args.target), links=args.links)
        return 0
    marker = Path(args.path)
    if marker.is_file() and not marker.is_symlink():
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
