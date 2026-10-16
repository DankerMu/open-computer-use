# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Apply the reviewed bootstrap inventory only after every output is verified."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys


class PatchError(Exception):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def target_path(root, relative):
    if not isinstance(relative, str):
        raise PatchError('target path must be a relative POSIX path')
    parts = PurePosixPath(relative).parts
    if (
        not parts
        or relative != PurePosixPath(relative).as_posix()
        or PurePosixPath(relative).is_absolute()
        or '..' in parts
        or '\\' in relative
    ):
        raise PatchError(f'unsafe target path: {relative}')
    target = root
    for part in parts:
        target = target / part
        if target.is_symlink():
            raise PatchError(f'symlink target path: {relative}')
    if not target.is_file():
        raise PatchError(f'missing regular target: {relative}')
    if target.stat().st_nlink != 1:
        raise PatchError(f'hard-linked target: {relative}')
    return target


def apply(root, inventory):
    files = inventory.get('files')
    if not isinstance(files, list) or not files:
        raise PatchError('inventory must contain files')
    prepared = []
    seen = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise PatchError('file entry must be an object')
        target = target_path(root, entry.get('path'))
        if target in seen:
            raise PatchError(f'repeated target: {target}')
        seen.add(target)
        metadata = target.stat()
        for name in ('before_sha256', 'after_sha256'):
            if not isinstance(entry.get(name), str) or not re.fullmatch(r'[0-9a-f]{64}', entry[name]):
                raise PatchError(f'invalid {name}: {target}')
        descriptor = os.open(target, os.O_RDONLY | getattr(os, 'O_NOATIME', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            source = stream.read()
        if digest(source) != entry['before_sha256']:
            raise PatchError(f'wrong original hash (or already patched): {target}')
        edits = entry.get('edits')
        if not isinstance(edits, list) or not edits:
            raise PatchError(f'missing edits: {target}')
        spans = []
        for edit in edits:
            if (
                not isinstance(edit, dict)
                or not isinstance(edit.get('before'), str)
                or not isinstance(edit.get('after'), str)
            ):
                raise PatchError(f'invalid edit: {target}')
            before, after = edit['before'].encode(), edit['after'].encode()
            start = source.find(before)
            if not before or start < 0 or source.find(before, start + 1) >= 0:
                raise PatchError(f'edit must match exactly once: {target}')
            spans.append((start, start + len(before), after))
        spans.sort()
        chunks = []
        cursor = 0
        for start, end, after in spans:
            if start < cursor:
                raise PatchError(f'overlapping edits: {target}')
            chunks.extend((source[cursor:start], after))
            cursor = end
        chunks.append(source[cursor:])
        result = b''.join(chunks)
        if digest(result) != entry['after_sha256']:
            raise PatchError(f'wrong resulting hash: {target}')
        prepared.append((target, metadata, result))
    for target, metadata, result in prepared:
        # Write the existing inode: retain ownership, permissions and legal notices.
        with target.open('wb') as stream:
            stream.write(result)
        os.chmod(target, stat.S_IMODE(metadata.st_mode))
        os.utime(target, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--inventory', type=Path, required=True)
    args = parser.parse_args()
    try:
        inventory = json.loads(args.inventory.read_text())
        if not isinstance(inventory, dict):
            raise PatchError('inventory must be an object')
        apply(args.root.resolve(strict=True), inventory)
    except (PatchError, OSError, ValueError) as exc:
        print(f'documentserver patch: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
