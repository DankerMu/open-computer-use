# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Restored database prune and compatibility checks against the fake engine."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import unittest

from support import DEFAULT_RELEASE_IMAGES, ROOT, fake_env, seed_images, tmp_dir

sys.path.insert(0, str(ROOT / "deploy"))
import recovery
import recovery_db


class RecoveryDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.context = tmp_dir()
        self.root = Path(self.context.name)
        self.state = self.root / "fake-state"
        self.state.mkdir()
        self.lock_dir = self.root / "image-store-lock"
        self.lock_dir.mkdir()
        self.env = fake_env(self.state)
        self.env["DOCKER_HOST"] = "unix:///var/run/docker.sock"
        os.environ.update(self.env)
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

    def tearDown(self):
        self.context.cleanup()

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


if __name__ == "__main__":
    unittest.main()
