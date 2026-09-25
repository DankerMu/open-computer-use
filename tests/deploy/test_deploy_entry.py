# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Deployment entry and network provisioning against an independent fake engine."""

from __future__ import annotations

import json
import os
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
    return [
        path
        for path in state_dir.glob("ocu-deploy-config.*")
        if path.is_dir() and path.name.startswith("ocu-deploy-config.")
    ]


def compose_up_ops(state_dir: Path) -> list[str]:
    return [line for line in ops(state_dir) if " compose " in f" {line} " and line.split()[-1:] != ["config"] and " up " in f" {line} "]


def executed_rows(state_dir: Path) -> list[dict]:
    path = state_dir / "executed.json"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def running_services(state_dir: Path) -> dict:
    path = state_dir / "running.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def wait_for(predicate, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def descendant_pids(root_pid: int) -> set[int]:
    try:
        listed = subprocess.check_output(["ps", "-ax", "-o", "pid=,ppid="], text=True)
    except subprocess.CalledProcessError:
        return set()
    children: dict[int, list[int]] = {}
    for line in listed.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        pid, ppid = int(parts[0]), int(parts[1])
        children.setdefault(ppid, []).append(pid)
    found: set[int] = set()
    stack = [root_pid]
    while stack:
        current = stack.pop()
        for child in children.get(current, []):
            if child not in found:
                found.add(child)
                stack.append(child)
    return found


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def recorded_child(state_dir: Path, name: str) -> int:
    return int((state_dir / name).read_text(encoding="utf-8").strip())


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
        self.assertEqual(compose_up_ops(self.state), [])
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
        self.assertEqual(compose_up_ops(self.state), [])
        after = leftover_tmp(self.state)
        self.assertEqual(after, before)
        self.assert_no_destructive()

    def test_bridge_network_mode_prevents_starts(self):
        docs = intended_docs()
        docs["webui.json"]["services"]["open-webui"].pop("networks", None)
        docs["webui.json"]["services"]["open-webui"]["network_mode"] = "bridge"
        write_fake_configs(self.state, docs)
        result = run_script(UP, self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(starts(self.state), [])
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
        env = dict(self.env)
        env["COMPOSE_REMOVE_ORPHANS"] = "1"
        env["COMPOSE_PROFILES"] = "manual-maintenance"
        result = run_script(UP, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(leftover_tmp(self.state), [])
        self.assertEqual(starts(self.state), ["core", "webui", "proxy"])
        recorded = "\n".join(ops(self.state))
        self.assertNotIn("--remove-orphans", recorded)
        rows = executed_rows(self.state)
        self.assertEqual([row["stack"] for row in rows], ["core", "webui", "proxy"])
        self.assertTrue(all(row["snapshot"] for row in rows))
        self.assertTrue(all(row["env_remove_orphans"] == "false" for row in rows))
        self.assertTrue(all(row["env_profiles"] == "" for row in rows))
        self.assertNotIn("cleanup", running_services(self.state))
        self.assertEqual(
            set(running_services(self.state)),
            {
                "workspace",
                "computer-use-server",
                "retention-guard",
                "open-webui",
                "postgres",
                "open-webui-init",
                "proxy",
            },
        )
        self.assert_no_destructive()

    def test_application_start_failure_preserves_earlier_stack(self):
        write_network(
            self.state,
            SANDBOX_NETWORK,
            subnet="172.31.0.0/24",
            gateway="172.31.0.1",
        )
        (self.state / "up-fail-webui").write_text("1", encoding="utf-8")
        result = run_script(UP, self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(starts(self.state), ["core", "webui:failed"])
        self.assertEqual(
            {name for name, owner in running_services(self.state).items() if owner == "core"},
            {"workspace", "computer-use-server", "retention-guard"},
        )
        self.assertNotIn("proxy", starts(self.state))
        self.assertEqual(leftover_tmp(self.state), [])
        self.assert_no_destructive()

    def test_checked_snapshot_is_started_after_source_mutation(self):
        write_network(
            self.state,
            SANDBOX_NETWORK,
            subnet="172.31.0.0/24",
            gateway="172.31.0.1",
        )
        docs = intended_docs()
        docs["webui.json"]["services"]["open-webui"]["environment"] = {
            "OCU_INTERNAL_TOKEN": "synthetic$TOKEN",
        }
        write_fake_configs(self.state, docs)
        marker = self.state / "up-hold"
        marker.write_text("1", encoding="utf-8")
        env = dict(self.env)
        env["FAKE_DOCKER_HOLD_UP"] = str(marker)
        env["TOKEN"] = "hostile-token-value"
        process = subprocess.Popen(
            ["bash", str(UP)],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
        try:
            self.assertTrue(
                wait_for(lambda: (self.state / "entered-up").exists()),
                "entry never reached compose up",
            )
            mutated = intended_docs()
            mutated["webui.json"]["services"]["open-webui"]["ports"] = [
                {"target": 8080, "published": "3000", "protocol": "tcp", "host_ip": "127.0.0.1"}
            ]
            write_fake_configs(self.state, mutated)
            marker.unlink(missing_ok=True)
            process.communicate(timeout=10)
        finally:
            marker.unlink(missing_ok=True)
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        rows = {row["stack"]: row for row in executed_rows(self.state)}
        webui = rows["webui"]["document"]["services"]["open-webui"]
        self.assertEqual(webui.get("ports", []), [])
        self.assertEqual(webui["environment"]["OCU_INTERNAL_TOKEN"], "synthetic$$TOKEN")
        consumer = rows["webui"]["consumer_document"]["services"]["open-webui"]
        self.assertEqual(consumer["environment"]["OCU_INTERNAL_TOKEN"], "synthetic$TOKEN")
        self.assertTrue(rows["webui"]["snapshot"])

    def _signal_during_hold(self, sig, *, hold_env, entered_name, require_resolved=False, require_snapshots=False):
        write_network(
            self.state,
            SANDBOX_NETWORK,
            subnet="172.31.0.0/24",
            gateway="172.31.0.1",
        )
        marker = self.state / f"{entered_name}-hold"
        marker.write_text("1", encoding="utf-8")
        env = dict(self.env)
        env[hold_env] = str(marker)
        before = leftover_tmp(self.state)
        process = subprocess.Popen(
            ["bash", str(UP)],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
        children: set[int] = set()
        try:
            self.assertTrue(
                wait_for(lambda: (self.state / entered_name).exists()),
                f"entry never reached {entered_name}",
            )
            private = leftover_tmp(self.state)
            self.assertEqual(len(private), 1)
            self.assertEqual(stat.S_IMODE(private[0].stat().st_mode), 0o700)
            names = []
            if require_resolved:
                names.extend(["core.json", "webui.json", "proxy.json"])
            if require_snapshots:
                names.extend(["core.up.json", "webui.up.json", "proxy.up.json"])
            for name in names:
                self.assertTrue((private[0] / name).is_file(), name)
                self.assertEqual(stat.S_IMODE((private[0] / name).stat().st_mode), 0o600)
            held_pid = recorded_child(self.state, entered_name)
            children = descendant_pids(process.pid)
            children.add(held_pid)
            self.assertTrue(pid_alive(held_pid), "held child died before the signal")
            process.send_signal(sig)
            self.assertTrue(
                wait_for(lambda: process.poll() is not None, timeout=8),
                "entry did not exit after signal",
            )
            self.assertTrue(
                wait_for(lambda: all(not pid_alive(pid) for pid in children), timeout=8),
                "owned child survived parent exit",
            )
            self.assertEqual(leftover_tmp(self.state), before)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGKILL)
            for pid in children:
                if pid_alive(pid):
                    os.kill(pid, signal.SIGKILL)
            marker.unlink(missing_ok=True)
            process.communicate(timeout=10)
        self.assertNotEqual(process.returncode, 0)
        self.assert_no_destructive()

    def _signal_during_held_up(self, sig):
        self._signal_during_hold(
            sig,
            hold_env="FAKE_DOCKER_HOLD_UP",
            entered_name="entered-up",
            require_resolved=True,
            require_snapshots=True,
        )

    def test_signal_during_entry_removes_temporary_configs(self):
        self._signal_during_held_up(signal.SIGTERM)

    def test_hangup_during_entry_removes_temporary_configs(self):
        self._signal_during_held_up(signal.SIGHUP)

    def test_int_during_entry_removes_temporary_configs(self):
        self._signal_during_held_up(signal.SIGINT)

    def test_int_during_config_removes_temporary_configs_and_children(self):
        self._signal_during_hold(
            signal.SIGINT,
            hold_env="FAKE_DOCKER_HOLD_CONFIG",
            entered_name="entered-config",
        )

    def test_term_during_config_removes_temporary_configs_and_children(self):
        self._signal_during_hold(
            signal.SIGTERM,
            hold_env="FAKE_DOCKER_HOLD_CONFIG",
            entered_name="entered-config",
        )

    def test_hup_during_inspect_removes_temporary_configs_and_children(self):
        self._signal_during_hold(
            signal.SIGHUP,
            hold_env="FAKE_DOCKER_HOLD_INSPECT",
            entered_name="entered-inspect",
            require_resolved=True,
            require_snapshots=True,
        )



if __name__ == "__main__":
    unittest.main()
