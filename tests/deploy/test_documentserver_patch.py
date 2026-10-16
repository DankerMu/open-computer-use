# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Exercise the build entrypoint on independent, deterministic source trees."""

import hashlib
import json
import os
import subprocess
import sys

import pytest

from support import ROOT

PATCH = ROOT / 'deploy/documentserver/patch.py'
BEFORE = b'/* retained legal notice */\nlocale(ready);\ncontrollers();\n'
AFTER = b'/* retained legal notice */\nlocale(ready);\nwindowReady(controllers);\n'


def tree(tmp_path):
    root = tmp_path / 'apps'
    entries = []
    for editor in ('documenteditor', 'spreadsheeteditor', 'presentationeditor'):
        relative = f'{editor}/main/app.js'
        path = root / relative
        path.parent.mkdir(parents=True)
        path.write_bytes(BEFORE)
        path.chmod(0o640)
        os.utime(path, ns=(1_600_000_000_000_000_000, 1_600_000_001_000_000_000))
        entries.append(
            {
                'path': relative,
                'before_sha256': hashlib.sha256(BEFORE).hexdigest(),
                'after_sha256': hashlib.sha256(AFTER).hexdigest(),
                'edits': [{'before': 'controllers();', 'after': 'windowReady(controllers);'}],
            }
        )
    unrelated = root / 'unrelated.js'
    unrelated.write_bytes(b'unchanged vendor component\x00\xff')
    unrelated.chmod(0o444)
    os.utime(unrelated, ns=(1_600_000_002_000_000_000, 1_600_000_003_000_000_000))
    return root, {'files': entries}


def snapshot(root):
    result = {}
    for path in root.rglob('*'):
        if path.is_file():
            metadata = path.stat()
            result[path.relative_to(root).as_posix()] = (
                path.read_bytes(),
                metadata.st_mode,
                metadata.st_uid,
                metadata.st_gid,
                metadata.st_mtime_ns,
                metadata.st_ino,
            )
    return result


def invoke(tmp_path, root, inventory):
    declaration = tmp_path / 'inventory.json'
    declaration.write_text(json.dumps(inventory))
    return subprocess.run(
        [sys.executable, str(PATCH), '--root', str(root), '--inventory', str(declaration)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_patch_preserves_unrelated_bytes_and_target_metadata(tmp_path):
    root, inventory = tree(tmp_path)
    before = snapshot(root)
    result = invoke(tmp_path, root, inventory)
    assert result.returncode == 0, result.stderr
    after = snapshot(root)
    assert after['unrelated.js'] == before['unrelated.js']
    for entry in inventory['files']:
        path = entry['path']
        assert after[path][0] == AFTER
        assert after[path][1:] == before[path][1:]


@pytest.mark.parametrize(
    'defect',
    [
        'wrong-last-input',
        'missing',
        'repeated-application',
        'ambiguous-edit',
        'wrong-output',
        'unsafe-relative',
        'absolute',
        'symlink',
        'overlap',
    ],
)
def test_patch_refuses_entire_tree_before_any_target_write(tmp_path, defect):
    root, inventory = tree(tmp_path)
    last = inventory['files'][-1]
    target = root / last['path']
    expected_error = ''
    if defect == 'wrong-last-input':
        target.write_bytes(b'unexpected upstream bytes')
        expected_error = 'wrong original hash'
    elif defect == 'missing':
        target.unlink()
        expected_error = 'missing regular target'
    elif defect == 'repeated-application':
        assert invoke(tmp_path, root, inventory).returncode == 0
        expected_error = 'already patched'
    elif defect == 'ambiguous-edit':
        content = BEFORE + b'controllers();\n'
        target.write_bytes(content)
        last['before_sha256'] = hashlib.sha256(content).hexdigest()
        expected_error = 'exactly once'
    elif defect == 'wrong-output':
        last['after_sha256'] = '0' * 64
        expected_error = 'wrong resulting hash'
    elif defect in ('unsafe-relative', 'absolute'):
        last['path'] = '../outside.js' if defect == 'unsafe-relative' else str(target)
        expected_error = 'unsafe target path'
    elif defect == 'symlink':
        outside = tmp_path / 'outside.js'
        outside.write_bytes(BEFORE)
        target.unlink()
        target.symlink_to(outside)
        expected_error = 'symlink target path'
    elif defect == 'overlap':
        last['edits'].append({'before': 'controllers()', 'after': 'other()'})
        expected_error = 'overlapping edits'
    before = snapshot(root)
    result = invoke(tmp_path, root, inventory)
    assert result.returncode == 1
    assert expected_error in result.stderr
    assert snapshot(root) == before
