# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Stop-only retention against independent fake container state."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import time
import unittest

from support import FAKE_DOCKER, ROOT, ops, tmp_dir


STOP_OVERAGE = ROOT / "deploy" / "production-like-test" / "retention" / "stop-overage.sh"
NOW = 1_800_000_000
HOUR = 3600
VOLUME_SENTINEL = b"volume-bytes-must-survive-stop\n"
DIRECTORY_SENTINEL = b"chat-directory-bytes-must-survive-stop\n"


def utc_started(age_hours: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - int(age_hours * HOUR)))


def write_containers(state: Path, containers) -> None:
    (state / "containers.json").write_text(json.dumps(containers), encoding="utf-8")


def load_containers(state: Path) -> list[dict]:
    return json.loads((state / "containers.json").read_text(encoding="utf-8"))


def container(*, cid, name, age_hours, state="running", managed=True, extra_labels=None):
    labels = dict(extra_labels or {})
    if managed:
        labels["managed-by"] = "mcp-computer-use-orchestrator"
    return {
        "Id": cid,
        "Name": name,
        "State": state,
        "StartedAt": utc_started(age_hours),
        "Labels": labels,
    }


class RetentionGuardTests(unittest.TestCase):
    def setUp(self):
        self.context = tmp_dir()
        self.state = Path(self.context.name)
        (self.state / "now-epoch").write_text(str(NOW), encoding="utf-8")
        self.volume = self.state / "volumes" / "chat-vol"
        self.directory = self.state / "directories" / "chat-dir"
        self.volume.parent.mkdir(parents=True)
        self.directory.mkdir(parents=True)
        self.volume.write_bytes(VOLUME_SENTINEL)
        (self.directory / "workspace.txt").write_bytes(DIRECTORY_SENTINEL)
        self.env = os.environ.copy()
        self.env["PATH"] = str(FAKE_DOCKER.parent) + os.pathsep + self.env.get("PATH", "")
        self.env["FAKE_DOCKER_STATE"] = str(self.state)
        self.env["TMPDIR"] = str(self.state)

    def tearDown(self):
        self.context.cleanup()

    def run_guard(self, extra=None):
        env = dict(self.env)
        if extra:
            env.update(extra)
        return subprocess.run(
            ["sh", str(STOP_OVERAGE)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=20,
        )

    def assert_sentinels(self):
        self.assertEqual(self.volume.read_bytes(), VOLUME_SENTINEL)
        self.assertEqual((self.directory / "workspace.txt").read_bytes(), DIRECTORY_SENTINEL)

    def assert_no_destructive(self):
        recorded = "\n".join(ops(self.state))
        for verb in (" rm ", " rmi ", " volume rm", " container rm", " prune"):
            self.assertNotIn(verb, recorded)

    def test_default_168h_boundary_stops_overage_and_preserves_underage(self):
        write_containers(
            self.state,
            [
                container(cid="overage", name="owui-chat-old", age_hours=168),
                container(cid="boundary-plus", name="owui-chat-plus", age_hours=168 + (1 / 3600)),
                container(cid="underage", name="owui-chat-young", age_hours=167),
                container(cid="unmanaged", name="foreign", age_hours=500, managed=False),
                container(cid="already-stopped", name="owui-chat-stopped", age_hours=200, state="exited"),
            ],
        )
        result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        states = {item["Id"]: item["State"] for item in load_containers(self.state)}
        self.assertEqual(states["overage"], "exited")
        self.assertEqual(states["boundary-plus"], "exited")
        self.assertEqual(states["underage"], "running")
        self.assertEqual(states["unmanaged"], "running")
        self.assertEqual(states["already-stopped"], "exited")
        self.assert_sentinels()
        self.assert_no_destructive()
        self.assertIn("docker stop --time 30 overage", "\n".join(ops(self.state)))
        self.assertNotIn("docker rm", "\n".join(ops(self.state)))

    def test_invalid_max_age_rejects_without_stopping(self):
        write_containers(
            self.state,
            [container(cid="keep-running", name="owui-chat-keep", age_hours=500)],
        )
        for value in ("", "abc", "-1", "1.5"):
            with self.subTest(value=repr(value)):
                before = (self.state / "containers.json").read_text(encoding="utf-8")
                result = self.run_guard({"CONTAINER_MAX_AGE_HOURS": value})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((self.state / "containers.json").read_text(encoding="utf-8"), before)
                self.assertNotIn("docker stop", "\n".join(ops(self.state)))
                self.assert_sentinels()

    def test_unknown_and_destructive_commands_fail(self):
        result = subprocess.run(
            ["docker", "rm", "-f", "overage"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=self.env,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported", result.stderr)
        unknown = subprocess.run(
            ["docker", "totally-unknown"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=self.env,
            check=False,
        )
        self.assertNotEqual(unknown.returncode, 0)
        self.assertIn("unsupported", unknown.stderr)

    def test_label_selection_ignores_other_labels(self):
        write_containers(
            self.state,
            [
                container(cid="managed", name="owui-chat-managed", age_hours=200),
                container(
                    cid="other-label",
                    name="other",
                    age_hours=200,
                    managed=False,
                    extra_labels={"managed-by": "someone-else"},
                ),
            ],
        )
        result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        states = {item["Id"]: item["State"] for item in load_containers(self.state)}
        self.assertEqual(states["managed"], "exited")
        self.assertEqual(states["other-label"], "running")
        self.assertEqual(stat.S_IMODE(self.volume.stat().st_mode) & 0o111, 0)
        self.assert_sentinels()


if __name__ == "__main__":
    unittest.main()
