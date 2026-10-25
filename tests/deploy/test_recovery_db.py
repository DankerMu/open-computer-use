# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Restored database prune and compatibility checks against the fake engine."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

from support import DEFAULT_RELEASE_IMAGES, FAKE_DOCKER, ROOT, fake_env, seed_images, tmp_dir

sys.path.insert(0, str(ROOT / "deploy"))
import recovery
import recovery_db


class RecoveryDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.context = tmp_dir()
        self.addCleanup(self.context.cleanup)
        self.root = Path(self.context.name)
        self.state = self.root / "fake-state"
        self.state.mkdir()
        self.lock_dir = self.root / "image-store-lock"
        self.lock_dir.mkdir()
        self.env = fake_env(self.state)
        self.env["DOCKER_HOST"] = "unix:///var/run/docker.sock"
        self.enterContext(patch.dict(os.environ, self.env))
        seed_images(self.state)
        recovery.pin_runtime_docker_host("unix:///var/run/docker.sock")
        self.container = "ocu-test-postgres-1"
        (self.state / "containers.json").write_text(
            json.dumps(
                [
                    {
                        "Id": "pg1",
                        "Name": self.container,
                        "State": {"Status": "running", "Running": True, "Paused": False},
                    }
                ]
            ),
            encoding="utf-8",
        )
        (self.state / "postgres.json").write_text(
            json.dumps(
                {
                    self.container: {
                        "alembic_revision": "e6f7a8b9c0d1",
                        "server_version": "17.5",
                        "extensions": ["plpgsql=1.0"],
                        "chats": ["chat-live"],
                        "chat_owners": {"chat-live": "owner-live"},
                        "chat_state": [
                            {
                                "chat_id": "chat-live",
                                "last_seen_revision": 2,
                                "prefs": {"theme": "keep|live", "note": "line\nbreak"},
                                "updated_at": 1700000000,
                            },
                            {
                                "chat_id": "chat-orphan",
                                "last_seen_revision": 9,
                                "prefs": {"drop": "me"},
                                "updated_at": 1700000001,
                            },
                        ],
                        "config": [
                            {
                                "key": "openai.api_key",
                                "value": "credential-A",
                                "updated_at": 1700000000,
                            }
                        ],

                    }
                }
            ),
            encoding="utf-8",
        )

    def test_prune_removes_only_orphan_chat_state(self):
        removed = recovery_db.prune_orphans(self.container)
        self.assertEqual(removed, ["chat-orphan"])
        restored = recovery_db.inspect_restored_state(self.container)
        self.assertEqual(restored["live_chats"], ["chat-live"])
        self.assertEqual(len(restored["chat_state"]), 1)
        live = restored["chat_state"][0]
        self.assertEqual(live["prefs"], {"theme": "keep|live", "note": "line\nbreak"})
        self.assertEqual(live["last_seen_revision"], 2)
        self.assertEqual(live["updated_at"], 1700000000)
        self.assertNotIn("owner_id", live)
        self.assertNotIn("file_ids", live)
        self.assertEqual(restored["owners"]["chat-live"], "owner-live")


    def test_invented_columns_are_rejected(self):
        with self.assertRaises(recovery.RecoveryError):
            recovery_db._exec_postgres(
                self.container,
                "SELECT preferences, owner_id, file_ids FROM ocu_chat_state",
            )
        with self.assertRaises(recovery.RecoveryError):
            recovery_db._exec_postgres(
                self.container,
                "SELECT COALESCE(data::text, '') FROM config ORDER BY id LIMIT 5;",
            )
        rows = recovery_db.inspect_provider_config(self.container)
        self.assertEqual(rows[0]["key"], "openai.api_key")
        self.assertEqual(rows[0]["value"], "credential-A")


    def test_selected_heads_admit_ancestors_and_merge_parents(self):
        path = self.state / "migration-graph.json"
        graph = {"heads": ["H"], "revisions": {
            "H": ["L", "R"], "L": ["P"], "R": ["P"], "P": [], "unrelated": [],
        }}
        path.write_text(json.dumps(graph))
        for revision in ("H", "L", "R", "P"):
            recovery_db.require_compatible_revision(
                {"alembic_revision": revision}, DEFAULT_RELEASE_IMAGES["open-webui"]
            )
        for revision in ("unrelated", "unknown"):
            with self.assertRaises(recovery.RecoveryError):
                recovery_db.require_compatible_revision(
                    {"alembic_revision": revision}, DEFAULT_RELEASE_IMAGES["open-webui"]
                )
        for invalid in (
            {"H": ["R"], "R": ["H"]},
            {"H": ["missing"]},
        ):
            path.write_text(json.dumps({"heads": ["H"], "revisions": invalid}))
            with self.assertRaises(recovery.RecoveryError):
                recovery_db.require_compatible_revision(
                    {"alembic_revision": "H"}, DEFAULT_RELEASE_IMAGES["open-webui"]
                )

    def test_incompatible_revision_and_tools_reject(self):
        schema = {"alembic_revision": "deadbeefc0de", "server_version": "17.5"}
        with self.assertRaises(recovery.RecoveryError) as raised:
            recovery_db.require_compatible_revision(
                schema, DEFAULT_RELEASE_IMAGES["open-webui"]
            )
        self.assertIn("does not recognize restored revision", str(raised.exception))
        old = {"alembic_revision": "e6f7a8b9c0d1", "server_version": "18.0"}
        with self.assertRaises(recovery.RecoveryError) as tools:
            recovery_db.require_compatible_tools(old, DEFAULT_RELEASE_IMAGES["postgres"])
        self.assertIn("cannot restore dump", str(tools.exception))


    def _probe_runs(self) -> list[dict]:
        runs = json.loads((self.state / "runs.json").read_text(encoding="utf-8"))
        return [
            row
            for row in runs
            if "-c" in row["command"]
            and "ScriptDirectory" in row["command"][row["command"].index("-c") + 1]
        ]

    def _set_relations(self, relations: list[dict]) -> None:
        path = self.state / "postgres.json"
        db = json.loads(path.read_text(encoding="utf-8"))
        db[self.container]["relations"] = relations
        path.write_text(json.dumps(db), encoding="utf-8")

    def _dump(self) -> Path:
        dump = self.root / "openwebui.dump"
        dump.write_text(
            json.dumps(
                {
                    "chats": ["chat-restored"],
                    "alembic_revision": "e6f7a8b9c0d1",
                    "server_version": "17.5",
                }
            ),
            encoding="utf-8",
        )
        return dump

    def test_migration_probe_receives_only_the_placeholder_secret(self):
        graph = recovery_db.inspect_migration_graph(DEFAULT_RELEASE_IMAGES["open-webui"])
        self.assertEqual(graph["heads"], ["e6f7a8b9c0d1"])
        self.assertEqual(
            graph["revisions"], {"e6f7a8b9c0d1": "d4c1a8e37b62", "d4c1a8e37b62": ""}
        )
        probes = self._probe_runs()
        self.assertEqual(len(probes), 1)
        probe = probes[0]
        self.assertEqual(
            probe["env"], {"WEBUI_SECRET_KEY": "recovery-migration-probe-placeholder"}
        )
        self.assertEqual(probe["network"], "none")
        self.assertIsNone(probe["env_file"])
        self.assertEqual(probe["mounts"], [])

    def test_probe_without_a_graph_still_refuses(self):
        message = "selected WebUI image did not report a migration graph"
        schema = {"alembic_revision": "e6f7a8b9c0d1"}
        with self.assertRaises(recovery.RecoveryError) as failed:
            recovery_db.require_compatible_revision(schema, "ocu-open-webui:not-loaded")
        self.assertEqual(str(failed.exception), message)
        (self.state / "migration-graph.json").write_text(
            json.dumps({"heads": [], "revisions": {}}), encoding="utf-8"
        )
        with self.assertRaises(recovery.RecoveryError) as empty:
            recovery_db.require_compatible_revision(schema, DEFAULT_RELEASE_IMAGES["open-webui"])
        self.assertEqual(str(empty.exception), message)

    def test_restore_accepts_target_holding_only_system_relations(self):
        self._set_relations(
            [
                {"schema": "pg_toast", "name": "pg_toast_1255"},
                {"schema": "pg_toast", "name": "pg_toast_1255_index"},
                {"schema": "pg_temp_3", "name": "scratch"},
                {"schema": "pg_toast_temp_3", "name": "pg_toast_16390"},
                {"schema": "pg_catalog", "name": "pg_class"},
                {"schema": "information_schema", "name": "sql_features"},
            ]
        )
        recovery_db.restore_database(self.container, self._dump())
        restored = recovery_db.inspect_restored_state(self.container)
        self.assertEqual(restored["live_chats"], ["chat-restored"])

    def test_restore_refuses_target_holding_user_relations(self):
        for relation in (
            {"schema": "public", "name": "t"},
            {"schema": "public", "name": "s"},
            {"schema": "app", "name": "v"},
            {"schema": "pgx", "name": "t"},
        ):
            with self.subTest(relation=relation):
                self._set_relations([{"schema": "pg_toast", "name": "pg_toast_1255"}, relation])
                with self.assertRaises(recovery.RecoveryError) as raised:
                    recovery_db.restore_database(self.container, self._dump())
                self.assertEqual(str(raised.exception), "target database is not empty")
                restored = recovery_db.inspect_restored_state(self.container)
                self.assertEqual(restored["live_chats"], ["chat-live"])


class RecoveryDatabaseEnvironmentTests(unittest.TestCase):
    def test_successful_recovery_restores_complete_environment(self):
        observed_docker = []

        class SuccessfulCase(RecoveryDatabaseTests):
            def runTest(self):
                observed_docker.append(shutil.which("docker"))
                self.test_prune_removes_only_orphan_chat_state()

        with patch.dict(os.environ, {
            "DOCKER_HOST": "unix:///ambient-recovery-test.sock",
            "COMPOSE_PROJECT_NAME": "ambient-recovery-project",
            "DOCKER_CONTEXT": "ambient-recovery-context",
        }):
            os.environ.pop("FAKE_DOCKER_STATE", None)
            before = dict(os.environ)
            result = unittest.TestResult()
            SuccessfulCase().run(result)

            self.assertTrue(
                result.wasSuccessful(),
                f"Recovery lifecycle recorded {len(result.errors)} errors "
                f"and {len(result.failures)} failures",
            )
            self.assertEqual(observed_docker, [str(FAKE_DOCKER)])
            self.assert_environment_restored(before)

    def test_recovery_body_error_restores_complete_environment(self):
        observed_docker = []

        class FailingCase(RecoveryDatabaseTests):
            def runTest(self):
                observed_docker.append(shutil.which("docker"))
                raise RuntimeError("recovery-body-error-canary")

        with patch.dict(os.environ, {
            "DOCKER_HOST": "unix:///ambient-recovery-test.sock",
            "COMPOSE_PROJECT_NAME": "ambient-recovery-project",
            "DOCKER_CONTEXT": "ambient-recovery-context",
        }):
            os.environ.pop("FAKE_DOCKER_STATE", None)
            before = dict(os.environ)
            case = FailingCase()
            result = unittest.TestResult()
            case.run(result)

            self.assertEqual(len(result.errors), 1)
            self.assertEqual(len(result.failures), 0)
            self.assertTrue(
                "RuntimeError: recovery-body-error-canary" in result.errors[0][1],
                "Expected recovery body error was not reported",
            )
            self.assertEqual(observed_docker, [str(FAKE_DOCKER)])
            self.assert_environment_restored(before)
            self.assertFalse(case.root.exists(), "Failed body retained its temporary root")

    def test_recovery_setup_error_restores_complete_environment(self):
        observed_docker = []
        body_ran = []

        class FailingCase(RecoveryDatabaseTests):
            def runTest(self):
                body_ran.append(True)

        def failing_seed_images(_state):
            observed_docker.append(shutil.which("docker"))
            raise RuntimeError("recovery-setup-error-canary")

        with patch.dict(os.environ, {
            "DOCKER_HOST": "unix:///ambient-recovery-test.sock",
            "COMPOSE_PROJECT_NAME": "ambient-recovery-project",
            "DOCKER_CONTEXT": "ambient-recovery-context",
        }):
            os.environ.pop("FAKE_DOCKER_STATE", None)
            before = dict(os.environ)
            case = FailingCase()
            result = unittest.TestResult()
            with patch.object(sys.modules[__name__], "seed_images", failing_seed_images):
                case.run(result)

            self.assertEqual(len(result.errors), 1)
            self.assertEqual(len(result.failures), 0)
            self.assertTrue(
                "RuntimeError: recovery-setup-error-canary" in result.errors[0][1],
                "Expected recovery setup error was not reported",
            )
            self.assertEqual(observed_docker, [str(FAKE_DOCKER)])
            self.assertEqual(body_ran, [])
            self.assert_environment_restored(before)
            self.assertFalse(case.root.exists(), "Failed setup retained its temporary root")

    def assert_environment_restored(self, before):
        after = dict(os.environ)
        added = sorted(after.keys() - before.keys())
        removed = sorted(before.keys() - after.keys())
        changed = sorted(
            key for key in before.keys() & after.keys()
            if before[key] != after[key]
        )
        self.assertTrue(
            after == before,
            f"Environment was not restored: added keys={added}; "
            f"removed keys={removed}; changed keys={changed}",
        )


if __name__ == "__main__":
    unittest.main()
