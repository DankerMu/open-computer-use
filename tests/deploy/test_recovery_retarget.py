# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Pointing an existing runtime configuration at a newly imported release."""

from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import stat
import subprocess
import sys
import unittest
from unittest.mock import patch

from support import (
    DEFAULT_RELEASE_IMAGES,
    RUNTIME_IMAGE_VARS,
    ROOT,
    committed_up_fixture,
    git_init_commit,
    tmp_dir,
    write_release_for_sha,
)

sys.path.insert(0, str(ROOT / "deploy"))
import recovery
import recovery_resources
import release

RECOVERY = ROOT / "deploy" / "recovery.py"
OLD_SHA = "1" * 40
OLD_WEBUI_SHA = "2" * 40
NEW_WEBUI_SHA = "3" * 40
UNCHANGED_ROLE = "postgres"
SECRETS = {
    "POSTGRES_PASSWORD": "pg-secret-sentinel",
    "OCU_INTERNAL_TOKEN": "internal-token-sentinel",
    "WEBUI_SECRET_KEY": "webui-secret-sentinel",
    "ADMIN_PASSWORD": "admin-password-sentinel",
    "MCP_API_KEY": "mcp-api-key-sentinel",
    "OCU_OFFICE_JWT_SECRET": "office-jwt-sentinel",
}


def new_reference(role: str) -> str:
    if role == UNCHANGED_ROLE:
        return DEFAULT_RELEASE_IMAGES[role]
    return DEFAULT_RELEASE_IMAGES[role].replace(":synthetic", ":upgraded")


class RetargetTests(unittest.TestCase):
    def setUp(self):
        self.context = tmp_dir()
        self.root = Path(self.context.name)
        self.release_root = self.root / "release-new"
        self.source = self.release_root / "source"
        self.source.mkdir(parents=True)
        self.sha = committed_up_fixture(self.source)
        self.write_release()
        self.deploy_root = self.root / "deploy-root"
        self.config = self.deploy_root / "config"
        self.config.mkdir(parents=True)
        self.config.chmod(0o700)
        self.runtime = self.config / "runtime.env"
        self.lines = [
            ("COMPOSE_PROJECT_NAME", "ocu-test"),
            ("SOURCE_SHA", OLD_SHA),
            ("POSTGRES_PASSWORD", SECRETS["POSTGRES_PASSWORD"]),
            ("OCU_CHAT_DATA_DIR", str(self.deploy_root / "data" / "chat")),
            ("WEBUI_SOURCE_SHA", OLD_WEBUI_SHA),
            ("OCU_SKILLS_CACHE_DIR", str(self.deploy_root / "data" / "skills-cache")),
            ("OCU_RELEASE_MANIFEST", str(self.deploy_root / "release.json")),
            ("OCU_INTERNAL_TOKEN", SECRETS["OCU_INTERNAL_TOKEN"]),
            ("OCU_SANDBOX_EGRESS_ALLOW", "8.8.8.8/32,1.1.1.1/32"),
            ("WEBUI_SECRET_KEY", SECRETS["WEBUI_SECRET_KEY"]),
            ("ADMIN_PASSWORD", SECRETS["ADMIN_PASSWORD"]),
            ("OCU_SANDBOX_DNS", ""),
            ("MCP_API_KEY", SECRETS["MCP_API_KEY"]),
            ("OCU_OFFICE_JWT_SECRET", SECRETS["OCU_OFFICE_JWT_SECRET"]),
        ]
        for role, name in RUNTIME_IMAGE_VARS.items():
            self.lines.append((name, DEFAULT_RELEASE_IMAGES[role]))
        self.write_runtime(self.lines)

    def tearDown(self):
        self.context.cleanup()

    def write_release(self):
        write_release_for_sha(
            self.release_root / "release.json",
            self.sha,
            NEW_WEBUI_SHA,
            images={role: {"reference": new_reference(role)} for role in RUNTIME_IMAGE_VARS},
        )

    def write_runtime(self, lines, *, raw: str | None = None):
        text = raw if raw is not None else "".join(f"{k}={v}\n" for k, v in lines)
        self.runtime.write_text(text, encoding="utf-8")
        self.runtime.chmod(0o600)
        self.before = self.runtime.read_bytes()

    def backup_path(self) -> Path:
        return self.config / f"runtime.env.before-{self.sha[:12]}"

    def expected_identity(self) -> dict[str, str]:
        identity = {
            "SOURCE_SHA": self.sha,
            "WEBUI_SOURCE_SHA": NEW_WEBUI_SHA,
            "OCU_RELEASE_MANIFEST": str(self.release_root / "release.json"),
        }
        for role, name in RUNTIME_IMAGE_VARS.items():
            identity[name] = new_reference(role)
        return identity

    def run_main(self, *extra: str):
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ["retarget", "--runtime-file", str(self.runtime),
                "--release-root", str(self.release_root), *extra]
        code = 0
        with patch.object(recovery, "require_root"), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = recovery.main(argv)
            except SystemExit as stop:
                code = stop.code
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_no_secret(self, text: str):
        for value in SECRETS.values():
            self.assertNotIn(value, text)

    def assert_unchanged(self, before_names: set[str]):
        self.assertEqual(self.runtime.read_bytes(), self.before)
        self.assertEqual(stat.S_IMODE(self.runtime.lstat().st_mode), 0o600)
        self.assertEqual({p.name for p in self.config.iterdir()}, before_names)

    def assert_refused(self, reason: str):
        names = {p.name for p in self.config.iterdir()}
        code, stdout, stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(len(stderr.splitlines()), 1, stderr)
        self.assertTrue(stderr.startswith("recovery: "), stderr)
        self.assertIn(reason, stderr)
        self.assertNotIn("Traceback", stderr)
        self.assert_no_secret(stderr)
        self.assert_unchanged(names)

    def test_rewrites_exactly_the_identity_keys_and_keeps_a_backup(self):
        code, stdout, stderr = self.run_main()
        self.assertEqual(code, 0, stderr)
        identity = self.expected_identity()
        expected = "".join(f"{k}={identity.get(k, v)}\n" for k, v in self.lines)
        self.assertEqual(self.runtime.read_text(encoding="utf-8"), expected)
        self.assertEqual(stat.S_IMODE(self.runtime.lstat().st_mode), 0o600)
        self.assertEqual(self.runtime.lstat().st_uid, os.geteuid())
        backup = self.backup_path()
        self.assertEqual(backup.read_bytes(), self.before)
        self.assertEqual(stat.S_IMODE(backup.lstat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.config.iterdir()), sorted(["runtime.env", backup.name]))
        old = dict(self.lines)
        lines = [
            f"{key} {'same' if old[key] == identity[key] else 'changed'} {identity[key]}"
            for key in recovery.IDENTITY_KEYS
        ]
        lines.append(f"backup {backup}")
        self.assertEqual(stdout.splitlines(), lines)
        self.assertIn(f"POSTGRES_IMAGE same {DEFAULT_RELEASE_IMAGES['postgres']}", stdout)
        self.assert_no_secret(stdout + stderr)

    def test_unchanged_identity_is_rewritten_with_a_backup(self):
        identity = self.expected_identity()
        self.lines = [(k, identity.get(k, v)) for k, v in self.lines]
        self.write_runtime(self.lines)
        code, stdout, stderr = self.run_main()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.runtime.read_bytes(), self.before)
        self.assertEqual(self.backup_path().read_bytes(), self.before)
        self.assertEqual(
            stdout.splitlines()[:-1],
            [f"{key} same {identity[key]}" for key in recovery.IDENTITY_KEYS],
        )

    def test_rerun_to_the_same_release_refuses_on_the_existing_backup(self):
        code, _stdout, stderr = self.run_main()
        self.assertEqual(code, 0, stderr)
        self.before = self.runtime.read_bytes()
        backup = self.backup_path().read_bytes()
        self.assert_refused("already exists")
        self.assertEqual(self.backup_path().read_bytes(), backup)

    def test_relative_release_root_is_recorded_as_an_absolute_manifest(self):
        cwd = os.getcwd()
        os.chdir(self.root)
        try:
            expected = Path(os.getcwd()) / "release-new" / "release.json"
            recovery.retarget_runtime(self.runtime, Path("release-new"))
        finally:
            os.chdir(cwd)
        self.assertIn(
            f"OCU_RELEASE_MANIFEST={expected}\n",
            self.runtime.read_text(encoding="utf-8"),
        )

    def test_source_at_another_commit_is_refused(self):
        (self.source / "IDENTITY").write_text("later\n", encoding="utf-8")
        git_init_commit(self.source, "later commit")
        self.assert_refused("expected " + self.sha)

    def test_dirty_source_is_refused(self):
        with open(self.source / "deploy" / "up.sh", "a", encoding="utf-8") as stream:
            stream.write("# drift\n")
        self.assert_refused("differ from the bundled commit")

    def test_missing_release_inventory_is_refused(self):
        (self.release_root / "release.json").unlink()
        self.assert_refused("release inventory is missing")

    def test_missing_release_source_is_refused(self):
        os.rename(self.source, self.root / "elsewhere")
        self.assert_refused(str(self.source))

    def test_missing_identity_key_is_refused(self):
        self.write_runtime([(k, v) for k, v in self.lines if k != "WEBUI_SOURCE_SHA"])
        self.assert_refused("WEBUI_SOURCE_SHA")

    def test_duplicate_key_is_refused(self):
        self.write_runtime(self.lines + [("SOURCE_SHA", OLD_SHA)])
        self.assert_refused("duplicate assignment SOURCE_SHA")

    def test_crlf_line_endings_are_refused(self):
        self.write_runtime(None, raw="".join(f"{k}={v}\r\n" for k, v in self.lines))
        self.assert_refused("LF-terminated")

    def test_comment_line_is_refused(self):
        text = "# operator note\n" + "".join(f"{k}={v}\n" for k, v in self.lines)
        self.write_runtime(None, raw=text)
        self.assert_refused("comment")

    def test_blank_line_is_refused(self):
        body = [f"{k}={v}\n" for k, v in self.lines]
        body.insert(3, "\n")
        self.write_runtime(None, raw="".join(body))
        self.assert_refused("blank line")

    def test_existing_backup_name_is_refused(self):
        self.backup_path().write_text("operator copy\n", encoding="utf-8")
        self.assert_refused("already exists")
        self.assertEqual(self.backup_path().read_text(encoding="utf-8"), "operator copy\n")

    def test_symlinked_runtime_file_is_refused(self):
        target = self.config / "real-runtime.env"
        os.rename(self.runtime, target)
        self.runtime.symlink_to(target.name)
        names = {p.name for p in self.config.iterdir()}
        code, stdout, stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(len(stderr.splitlines()), 1, stderr)
        self.assertIn("symbolic link", stderr)
        self.assertTrue(self.runtime.is_symlink())
        self.assertEqual(target.read_bytes(), self.before)
        self.assertEqual({p.name for p in self.config.iterdir()}, names)

    def test_missing_final_newline_is_refused(self):
        self.write_runtime(None, raw="".join(f"{k}={v}\n" for k, v in self.lines).rstrip("\n"))
        self.assert_refused("LF-terminated")

    def test_unsupported_source_contract_is_refused(self):
        script = self.source / "deploy" / "release.py"
        text = script.read_text(encoding="utf-8")
        literal = f"SOURCE_CONSUMER_CONTRACT = {release.SOURCE_CONSUMER_CONTRACT}\n"
        self.assertIn(literal, text)
        script.write_text(text.replace(literal, "SOURCE_CONSUMER_CONTRACT = 99\n"), encoding="utf-8")
        self.sha = git_init_commit(self.source, "unsupported contract")
        self.write_release()
        self.assert_refused("source consumer contract 99 is unsupported")

    def test_directory_at_runtime_path_is_refused(self):
        self.runtime.unlink()
        self.runtime.mkdir(mode=0o700)
        names = {p.name for p in self.config.iterdir()}
        code, stdout, stderr = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(len(stderr.splitlines()), 1, stderr)
        self.assertIn("must be a regular file", stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertTrue(self.runtime.is_dir())
        self.assertEqual(list(self.runtime.iterdir()), [])
        self.assertEqual({p.name for p in self.config.iterdir()}, names)

    def test_failed_backup_copy_leaves_no_backup(self):
        names = {p.name for p in self.config.iterdir()}
        with patch.object(recovery.shutil, "copyfileobj", side_effect=OSError("injected copy failure")):
            with self.assertRaises(OSError):
                recovery.retarget_runtime(self.runtime, self.release_root)
        self.assert_unchanged(names)

    def test_cancellation_after_replacement_keeps_the_backup(self):
        real_replace = os.replace

        def replace_then_cancel(source, dest):
            real_replace(source, dest)
            raise SystemExit(143)

        with patch.object(recovery.os, "replace", side_effect=replace_then_cancel):
            with self.assertRaises(SystemExit) as raised:
                recovery.retarget_runtime(self.runtime, self.release_root)
        self.assertEqual(raised.exception.code, 143)
        self.assertEqual(self.backup_path().read_bytes(), self.before)
        self.assertIn(f"SOURCE_SHA={self.sha}\n", self.runtime.read_text(encoding="utf-8"))
        self.assertEqual(
            sorted(p.name for p in self.config.iterdir()),
            sorted(["runtime.env", self.backup_path().name]),
        )

    def test_short_write_is_refused_and_names_the_backup(self):
        real_write = recovery.write_private_bytes

        def short_write(path, data, mode=0o600):
            real_write(path, data[:-1], mode)

        with patch.object(recovery, "write_private_bytes", side_effect=short_write):
            with self.assertRaises(recovery.RecoveryError) as raised:
                recovery.retarget_runtime(self.runtime, self.release_root)
        self.assertIn(str(self.backup_path()), str(raised.exception))
        self.assertEqual(self.backup_path().read_bytes(), self.before)
        published = self.runtime.read_text(encoding="utf-8")
        self.assertIn(f"SOURCE_SHA={self.sha}\n", published)
        self.assertFalse(published.endswith("\n"))
        self.assertEqual(
            sorted(p.name for p in self.config.iterdir()),
            sorted(["runtime.env", self.backup_path().name]),
        )

    def test_failure_before_replacement_removes_backup_and_staging(self):
        names = {p.name for p in self.config.iterdir()}
        with patch.object(recovery.os, "replace", side_effect=OSError("injected replace failure")):
            with self.assertRaises(recovery.RecoveryError):
                recovery.retarget_runtime(self.runtime, self.release_root)
        self.assert_unchanged(names)

    def test_failure_after_replacement_keeps_and_names_the_backup(self):
        with patch.object(recovery.os, "chmod", side_effect=OSError("injected chmod failure")):
            with self.assertRaises(recovery.RecoveryError) as raised:
                recovery.retarget_runtime(self.runtime, self.release_root)
        self.assertIn(str(self.backup_path()), str(raised.exception))
        self.assertEqual(self.backup_path().read_bytes(), self.before)
        self.assertIn(f"SOURCE_SHA={self.sha}\n", self.runtime.read_text(encoding="utf-8"))
        self.assertEqual(
            sorted(p.name for p in self.config.iterdir()),
            sorted(["runtime.env", self.backup_path().name]),
        )

    def test_script_refusal_is_one_line_without_traceback(self):
        (self.source / "IDENTITY").write_text("later\n", encoding="utf-8")
        git_init_commit(self.source, "later commit")
        names = {p.name for p in self.config.iterdir()}
        result = subprocess.run(
            [sys.executable, str(RECOVERY), "retarget", "--runtime-file", str(self.runtime),
             "--release-root", str(self.release_root)],
            capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        # Non-root stops at the root check; root reaches the release's ReleaseError.
        reason = "expected " + self.sha if os.geteuid() == 0 else "recovery requires root"
        self.assertIn(reason, result.stderr)
        self.assert_unchanged(names)


class PersistSelectedIdentityTests(unittest.TestCase):
    """Recovery activation keeps adding a missing identity key and pinning its root."""

    def test_missing_identity_key_is_added_and_manifest_pinned(self):
        context = tmp_dir()
        self.addCleanup(context.cleanup)
        root = Path(context.name)
        inventory = release.load_inventory(
            write_release_for_sha(root / "inventory" / "release.json", OLD_SHA, OLD_WEBUI_SHA)
        )
        config = root / "config"
        config.mkdir(mode=0o700)
        config.chmod(0o700)
        runtime = config / "runtime.env"
        current = {"COMPOSE_PROJECT_NAME": "ocu-test", "SOURCE_SHA": OLD_SHA}
        runtime.write_text("COMPOSE_PROJECT_NAME=ocu-test\nSOURCE_SHA=" + OLD_SHA + "\n", encoding="utf-8")
        runtime.chmod(0o600)
        published = recovery_resources.persist_selected_runtime_identity(
            destination_root=root, inventory=inventory, current=current,
        )
        self.assertEqual(published["OCU_RELEASE_MANIFEST"], str(root / "release.json"))
        self.assertEqual(published["WEBUI_SOURCE_SHA"], OLD_WEBUI_SHA)
        self.assertEqual(list(published)[:2], ["COMPOSE_PROJECT_NAME", "SOURCE_SHA"])
        self.assertEqual(list(published)[2:], list(recovery.IDENTITY_KEYS[1:]))
        self.assertEqual(stat.S_IMODE(runtime.lstat().st_mode), 0o600)
        self.assertEqual([p.name for p in config.iterdir()], ["runtime.env"])


if __name__ == "__main__":
    unittest.main()
