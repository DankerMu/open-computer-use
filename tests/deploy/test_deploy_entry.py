# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Deployment entry and network provisioning against an independent fake engine."""

from __future__ import annotations

import json
from pathlib import Path
import signal
import stat
import subprocess
import time
import unittest

from support import (
    CONTROL_NETWORK,
    PROVISION,
    ROOT,
    SANDBOX_NETWORK,
    UP,
    fake_env,
    intended_docs,
    ops,
    tmp_dir,
    write_fake_configs,
    write_network,
)


DESTRUCTIVE = ("network rm", "network disconnect", "compose down", " rm -f", " container rm")


def run_script(script: Path, env, *, timeout=20):
    return subprocess.run(
        ["bash", str(script)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=timeout,
    )


def starts(state_dir: Path) -> list[str]:
    log = state_dir / "starts.log"
    if not log.exists():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line]


def leftover_tmp(state_dir: Path) -> list[Path]:
    return [path for path in state_dir.glob("ocu-deploy-config-*") if path.exists()]


class DeployEntryTests(unittest.TestCase):
    def setUp(self):
        self.context = tmp_dir()
        self.state = Path(self.context.name)
        write_fake_configs(self.state)
        write_network(
            self.state,
            CONTROL_NETWORK,
            subnet="172.30.0.0/24",
            gateway="172.30.0.1",
        )
        self.env = fake_env(self.state)

    def tearDown(self):
        self.context.cleanup()

    def assert_no_destructive(self):
        recorded = "\n".join(ops(self.state))
        for verb in DESTRUCTIVE:
            self.assertNotIn(verb, recorded)

    def test_missing_sandbox_bridge_is_created_then_reinspected(self):
        result = run_script(PROVISION, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        recorded = ops(self.state)
        self.assertTrue(any("network inspect" in line and SANDBOX_NETWORK in line for line in recorded))
        self.assertTrue(any("network create" in line and SANDBOX_NETWORK in line for line in recorded))
        inspect_after = [line for line in recorded if "network inspect" in line and SANDBOX_NETWORK in line]
        self.assertGreaterEqual(len(inspect_after), 2)
        created = json.loads((self.state / "networks" / f"{SANDBOX_NETWORK}.json").read_text(encoding="utf-8"))
        self.assertEqual(created["Driver"], "bridge")
        self.assertFalse(created["Internal"])
        self.assertEqual(created["IPAM"]["Config"][0]["Subnet"], "172.31.0.0/24")
        self.assertEqual(created["IPAM"]["Config"][0]["Gateway"], "172.31.0.1")
        self.assert_no_destructive()

    def test_compatible_existing_bridge_is_reused(self):
        write_network(
            self.state,
            SANDBOX_NETWORK,
            subnet="172.31.0.0/24",
            gateway="172.31.0.1",
        )
        result = run_script(PROVISION, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        recorded = "\n".join(ops(self.state))
        self.assertNotIn("network create", recorded)
        self.assert_no_destructive()

    def test_incompatible_existing_bridge_fails_without_mutation(self):
        write_network(
            self.state,
            SANDBOX_NETWORK,
            subnet="10.0.0.0/24",
            gateway="10.0.0.1",
        )
        original = (self.state / "networks" / f"{SANDBOX_NETWORK}.json").read_text(encoding="utf-8")
        result = run_script(PROVISION, self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            (self.state / "networks" / f"{SANDBOX_NETWORK}.json").read_text(encoding="utf-8"),
            original,
        )
        self.assert_no_destructive()

    def test_existing_bridge_driver_internal_or_gateway_disagreement_is_preserved(self):
        cases = (
            {"driver": "overlay", "internal": False, "gateway": "172.31.0.1"},
            {"driver": "bridge", "internal": True, "gateway": "172.31.0.1"},
            {"driver": "bridge", "internal": False, "gateway": "172.31.0.2"},
        )
        for case in cases:
            with self.subTest(case=case):
                write_network(
                    self.state,
                    SANDBOX_NETWORK,
                    subnet="172.31.0.0/24",
                    gateway=case["gateway"],
                    driver=case["driver"],
                    internal=case["internal"],
                )
                result = run_script(PROVISION, self.env)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(starts(self.state), [])
                self.assert_no_destructive()

    def test_existing_control_plane_mismatch_fails_before_sandbox_create(self):
        write_network(
            self.state,
            CONTROL_NETWORK,
            subnet="172.30.0.0/24",
            gateway="172.30.0.2",
        )
        result = run_script(PROVISION, self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("network create" in line for line in ops(self.state)))
        self.assert_no_destructive()

    def test_non_conflict_create_error_does_not_claim_a_bridge(self):
        (self.state / "create-script.json").write_text(
            json.dumps({"code": 1, "message": "permission denied"}),
            encoding="utf-8",
        )
        result = run_script(PROVISION, self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(starts(self.state), [])
        self.assert_no_destructive()


    def test_inspection_error_other_than_absence_fails_closed(self):
        (self.state / "inspect-error.json").write_text(
            json.dumps({"code": 1, "message": "permission denied"}),
            encoding="utf-8",
        )
        result = run_script(PROVISION, self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("inspect", result.stderr.lower())
        recorded = "\n".join(ops(self.state))
        self.assertNotIn("network create", recorded)
        self.assert_no_destructive()

    def test_overlapping_subnets_fail_before_engine_create(self):
        env = dict(self.env)
        env["OCU_SANDBOX_SUBNET"] = "172.30.0.0/24"
        env["OCU_SANDBOX_GATEWAY"] = "172.30.0.1"
        result = run_script(PROVISION, env)
        self.assertNotEqual(result.returncode, 0)
        recorded = "\n".join(ops(self.state))
        self.assertNotIn("network create", recorded)

    def test_identical_network_names_fail_before_engine_create(self):
        env = dict(self.env)
        env["OCU_SANDBOX_NETWORK"] = CONTROL_NETWORK
        result = run_script(PROVISION, env)
        self.assertNotEqual(result.returncode, 0)
        recorded = "\n".join(ops(self.state))
        self.assertNotIn("network create", recorded)

    def test_concurrent_create_reinspects_compatible_winner(self):
        (self.state / "create-script.json").write_text(
            json.dumps(
                {
                    "code": 1,
                    "message": "network with name ocu-sandbox already exists",
                    "once": True,
                    "write": {
                        "Name": SANDBOX_NETWORK,
                        "Id": "id-winner",
                        "Driver": "bridge",
                        "Internal": False,
                        "IPAM": {"Config": [{"Subnet": "172.31.0.0/24", "Gateway": "172.31.0.1"}]},
                    },
                }
            ),
            encoding="utf-8",
        )
        result = run_script(PROVISION, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        inspect_lines = [line for line in ops(self.state) if "network inspect" in line and SANDBOX_NETWORK in line]
        self.assertGreaterEqual(len(inspect_lines), 2)
        self.assert_no_destructive()

    def test_concurrent_create_rejects_incompatible_winner(self):
        (self.state / "create-script.json").write_text(
            json.dumps(
                {
                    "code": 1,
                    "message": "network with name ocu-sandbox already exists",
                    "once": True,
                    "write": {
                        "Name": SANDBOX_NETWORK,
                        "Id": "id-winner",
                        "Driver": "bridge",
                        "Internal": True,
                        "IPAM": {"Config": [{"Subnet": "172.31.0.0/24", "Gateway": "172.31.0.1"}]},
                    },
                }
            ),
            encoding="utf-8",
        )
        result = run_script(PROVISION, self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_destructive()

    def test_config_resolution_failure_starts_no_service(self):
        (self.state / "config-fail").write_text("1", encoding="utf-8")
        result = run_script(UP, self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(starts(self.state), [])
        self.assertFalse(any("compose up" in line for line in ops(self.state)))
        self.assert_no_destructive()

    def test_checker_failure_starts_no_service_and_cleans_temp_files(self):
        docs = intended_docs()
        docs["webui.json"]["services"]["open-webui"]["ports"] = [
            {"target": 8080, "published": "3000", "protocol": "tcp", "host_ip": "127.0.0.1"}
        ]
        write_fake_configs(self.state, docs)
        before = leftover_tmp(self.state)
        result = run_script(UP, self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(starts(self.state), [])
        self.assertFalse(any("compose up" in line for line in ops(self.state)))
        after = leftover_tmp(self.state)
        self.assertEqual(after, before)
        self.assert_no_destructive()

    def test_incompatible_bridge_prevents_starts(self):
        write_network(
            self.state,
            SANDBOX_NETWORK,
            subnet="10.8.0.0/24",
            gateway="10.8.0.1",
        )
        result = run_script(UP, self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(starts(self.state), [])
        self.assert_no_destructive()

    def test_successful_entry_starts_applications_before_proxy(self):
        write_network(
            self.state,
            SANDBOX_NETWORK,
            subnet="172.31.0.0/24",
            gateway="172.31.0.1",
        )
        result = run_script(UP, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(leftover_tmp(self.state), [])
        self.assertEqual(starts(self.state), ["core", "webui", "proxy"])
        recorded = "\n".join(ops(self.state))
        self.assertNotIn("--remove-orphans", recorded)
        self.assert_no_destructive()

    def test_signal_during_entry_removes_temporary_configs(self):
        write_network(
            self.state,
            SANDBOX_NETWORK,
            subnet="172.31.0.0/24",
            gateway="172.31.0.1",
        )
        marker = self.state / "up-hold"
        marker.write_text("1", encoding="utf-8")
        env = dict(self.env)
        env["FAKE_DOCKER_HOLD_UP"] = str(marker)
        before = leftover_tmp(self.state)
        process = subprocess.Popen(
            ["bash", str(UP)],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        deadline = time.time() + 5
        while time.time() < deadline:
            if any("compose up" in line for line in ops(self.state)):
                break
            time.sleep(0.05)
        try:
            private = leftover_tmp(self.state)
            self.assertEqual(len(private), 1)
            self.assertEqual(stat.S_IMODE(private[0].stat().st_mode), 0o700)
            for name in ("core.json", "webui.json", "proxy.json"):
                self.assertEqual(stat.S_IMODE((private[0] / name).stat().st_mode), 0o600)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
            marker.unlink(missing_ok=True)
            process.communicate(timeout=10)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(leftover_tmp(self.state), before)
        self.assert_no_destructive()


if __name__ == "__main__":
    unittest.main()
