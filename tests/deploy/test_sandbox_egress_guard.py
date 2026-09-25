# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Sandbox egress guard CLI against an independent fake host/engine."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest

from packet_oracle import evaluate_packet
from support import (
    CONTROL_NETWORK,
    DEFAULT_ALLOW,
    FIREWALL_CHECK,
    FIREWALL_INSTALL,
    METADATA_ADDR,
    OWNED_IPV4,
    OWNED_IPV6,
    ROOT,
    SANDBOX_NETWORK,
    UP,
    fake_env,
    load_firewall,
    ops,
    run_script,
    seed_healthy_host,
    tmp_dir,
    write_fake_configs,
    write_firewall,
    write_network,
)


BRIDGE = "br-id-ocu-sandb"
FOREIGN_RULE = ["-s", "10.9.9.9/32", "-j", "ACCEPT", "-m", "comment", "--comment", "foreign-keep"]
FOREIGN_INPUT = ["-i", "eth0", "-j", "ACCEPT", "-m", "comment", "--comment", "foreign-input"]
FOREIGN_V6 = ["-i", "eth0", "-j", "ACCEPT", "-m", "comment", "--comment", "foreign-v6"]


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


def _owned_hooks(rules):
    return [rule for rule in rules if OWNED_IPV4 in rule or "ocu-sandbox-egress" in rule]


def _flag(rule: list[str], flag: str):
    if flag not in rule:
        return None
    index = rule.index(flag)
    if index + 1 >= len(rule):
        return ""
    return rule[index + 1]


def _wait(predicate, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _restore_lines(state_dir: Path) -> list[str]:
    return [line for line in ops(state_dir) if "iptables-restore" in line or "ip6tables-restore" in line]


class EgressGuardTests(unittest.TestCase):
    def setUp(self):
        self.context = tmp_dir()
        self.state = Path(self.context.name)
        write_fake_configs(self.state)
        seed_healthy_host(self.state)
        write_network(
            self.state,
            CONTROL_NETWORK,
            subnet="172.30.0.0/24",
            gateway="172.30.0.1",
        )
        write_network(
            self.state,
            SANDBOX_NETWORK,
            subnet="172.31.0.0/24",
            gateway="172.31.0.1",
        )
        payload = load_firewall(self.state)
        payload["ipv4"]["DOCKER-USER"].insert(0, FOREIGN_RULE)
        payload["ipv4"]["INPUT"].insert(0, FOREIGN_INPUT)
        payload["ipv6"]["FORWARD"].insert(0, FOREIGN_V6)
        write_firewall(self.state, payload)
        self.env = fake_env(self.state)

    def tearDown(self):
        self.context.cleanup()

    def install(self, extra=None):
        env = dict(self.env)
        if extra:
            env.update(extra)
        return run_script(FIREWALL_INSTALL, env)

    def check(self, extra=None):
        env = dict(self.env)
        if extra:
            env.update(extra)
        return run_script(FIREWALL_CHECK, env)

    def test_unset_allowlist_fails_without_firewall_mutation(self):
        env = dict(self.env)
        del env["OCU_SANDBOX_EGRESS_ALLOW"]
        before = (self.state / "firewall.json").read_text(encoding="utf-8")
        result = run_script(FIREWALL_INSTALL, env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("OCU_SANDBOX_EGRESS_ALLOW", result.stderr)
        self.assertIn("unset", result.stderr.lower())
        self.assertEqual((self.state / "firewall.json").read_text(encoding="utf-8"), before)

    def test_malformed_allowlist_fails_without_firewall_mutation(self):
        before = (self.state / "firewall.json").read_text(encoding="utf-8")
        for value in ("8.8.8.8/32,", "not-an-ip", "2001:db8::1", "8.8.8.8/33", "8.8.8.8/32, ,1.1.1.1/32"):
            with self.subTest(value=value):
                result = self.install({"OCU_SANDBOX_EGRESS_ALLOW": value})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("OCU_SANDBOX_EGRESS_ALLOW", result.stderr)
                self.assertEqual((self.state / "firewall.json").read_text(encoding="utf-8"), before)

    def test_empty_allowlist_installs_deny_all_and_check_passes(self):
        result = self.install({"OCU_SANDBOX_EGRESS_ALLOW": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        check = self.check({"OCU_SANDBOX_EGRESS_ALLOW": ""})
        self.assertEqual(check.returncode, 0, check.stderr)
        firewall = load_firewall(self.state)
        owned = firewall["ipv4"][OWNED_IPV4]
        self.assertEqual(owned[-1], ["-j", "DROP"])
        self.assertFalse(any(_flag(rule, "-d") == "8.8.8.8/32" for rule in owned))
        packet = evaluate_packet(
            firewall,
            family="ipv4",
            chain="DOCKER-USER",
            in_iface=BRIDGE,
            src="172.31.0.20",
            dst="8.8.8.8",
        )
        self.assertEqual(packet, "DROP")

    def test_repeated_install_leaves_one_ordered_copy_and_keeps_foreign_rules(self):
        first = self.install()
        self.assertEqual(first.returncode, 0, first.stderr)
        before_foreign = evaluate_packet(
            load_firewall(self.state),
            family="ipv4",
            chain="FORWARD",
            in_iface="eth0",
            src="10.9.9.9",
            dst="8.8.8.8",
        )
        second = self.install()
        self.assertEqual(second.returncode, 0, second.stderr)
        firewall = load_firewall(self.state)
        owned = _owned_hooks(firewall["ipv4"]["DOCKER-USER"])
        self.assertEqual(len(owned), 1)
        self.assertEqual(firewall["ipv4"]["DOCKER-USER"][0], owned[0])
        self.assertEqual(_flag(owned[0], "-i"), BRIDGE)
        self.assertEqual(_flag(owned[0], "-j"), OWNED_IPV4)
        self.assertIn(FOREIGN_RULE, firewall["ipv4"]["DOCKER-USER"])
        self.assertIn(FOREIGN_INPUT, firewall["ipv4"]["INPUT"])
        self.assertEqual(
            evaluate_packet(firewall, family="ipv4", chain="FORWARD", in_iface="eth0", src="10.9.9.9", dst="8.8.8.8"),
            "ACCEPT",
        )
        self.assertEqual(before_foreign, "ACCEPT")
        self.assertEqual(
            evaluate_packet(firewall, family="ipv4", chain="INPUT", in_iface="eth0", src="10.0.0.8", dst="172.31.0.1"),
            "ACCEPT",
        )
        check = self.check()
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_stale_allowlist_and_duplicate_hooks_reconcile(self):
        first = self.install({"OCU_SANDBOX_EGRESS_ALLOW": "9.9.9.9/32"})
        self.assertEqual(first.returncode, 0, first.stderr)
        firewall = load_firewall(self.state)
        hook = list(firewall["ipv4"]["DOCKER-USER"][0])
        firewall["ipv4"]["DOCKER-USER"].append(list(hook))
        firewall["ipv4"]["DOCKER-USER"].append(list(hook))
        firewall["ipv4"]["INPUT"].append(["-s", "172.31.0.0/24", "-j", OWNED_IPV4, "-m", "comment", "--comment", "ocu-sandbox-egress"])
        write_firewall(self.state, firewall)
        result = self.install({"OCU_SANDBOX_EGRESS_ALLOW": DEFAULT_ALLOW})
        self.assertEqual(result.returncode, 0, result.stderr)
        firewall = load_firewall(self.state)
        owned = _owned_hooks(firewall["ipv4"]["DOCKER-USER"])
        self.assertEqual(len(owned), 1)
        self.assertEqual(firewall["ipv4"]["DOCKER-USER"][0], owned[0])
        self.assertEqual(_flag(firewall["ipv4"]["INPUT"][0], "-i"), BRIDGE)
        self.assertEqual(_flag(firewall["ipv4"]["INPUT"][0], "-j"), OWNED_IPV4)
        destinations = [_flag(rule, "-d") for rule in firewall["ipv4"][OWNED_IPV4] if _flag(rule, "-j") == "RETURN" and _flag(rule, "-d")]
        self.assertEqual(destinations, ["8.8.8.8/32", "1.1.1.1/32"])
        self.assertNotIn("9.9.9.9/32", destinations)
        self.assertIn(FOREIGN_RULE, firewall["ipv4"]["DOCKER-USER"])
        self.assertEqual(
            evaluate_packet(firewall, family="ipv4", chain="FORWARD", in_iface="eth0", src="10.9.9.9", dst="1.1.1.1"),
            "ACCEPT",
        )

    def test_missing_docker_user_is_an_error_not_a_placeholder(self):
        firewall = load_firewall(self.state)
        del firewall["ipv4"]["DOCKER-USER"]
        write_firewall(self.state, firewall)
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DOCKER-USER", result.stderr)
        self.assertNotIn(OWNED_IPV4, load_firewall(self.state).get("ipv4", {}))

    def test_bypassed_docker_user_hook_fails_preflight(self):
        firewall = load_firewall(self.state)
        firewall["ipv4"]["FORWARD"] = [["-j", "ACCEPT"], ["-j", "DOCKER-USER"]]
        write_firewall(self.state, firewall)
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DOCKER-USER", result.stderr)
        self.assertIn("FORWARD", result.stderr)

    def test_interface_conditional_forward_accept_fails_preflight(self):
        firewall = load_firewall(self.state)
        firewall["ipv4"]["FORWARD"] = [["-i", BRIDGE, "-j", "ACCEPT"], ["-j", "DOCKER-USER"]]
        write_firewall(self.state, firewall)
        before = (self.state / "firewall.json").read_text(encoding="utf-8")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DOCKER-USER", result.stderr)
        self.assertIn("FORWARD", result.stderr)
        self.assertEqual((self.state / "firewall.json").read_text(encoding="utf-8"), before)
        self.assertEqual(
            evaluate_packet(firewall, family="ipv4", chain="FORWARD", in_iface=BRIDGE, src="172.31.0.20", dst="8.8.8.8"),
            "ACCEPT",
        )

    def test_source_conditional_forward_accept_fails_preflight(self):
        firewall = load_firewall(self.state)
        firewall["ipv4"]["FORWARD"] = [["-s", "172.31.0.0/24", "-j", "ACCEPT"], ["-j", "DOCKER-USER"]]
        write_firewall(self.state, firewall)
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FORWARD", result.stderr)

    def test_foreign_chain_jump_before_docker_user_fails_preflight(self):
        firewall = load_firewall(self.state)
        firewall["ipv4"]["ACCEPT-SANDBOX"] = [["-j", "ACCEPT"]]
        firewall["ipv4"]["FORWARD"] = [["-j", "ACCEPT-SANDBOX"], ["-j", "DOCKER-USER"]]
        write_firewall(self.state, firewall)
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FORWARD", result.stderr)
        self.assertEqual(
            evaluate_packet(firewall, family="ipv4", chain="FORWARD", in_iface=BRIDGE, src="172.31.0.20", dst="8.8.8.8"),
            "ACCEPT",
        )

    def test_nftables_backend_and_rootless_fail_named(self):
        (self.state / "docker-info.json").write_text(json.dumps({"FirewallBackend": "nftables"}), encoding="utf-8")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nftables", result.stderr.lower())
        (self.state / "docker-info.json").write_text(json.dumps({"FirewallBackend": "iptables", "Rootless": True}), encoding="utf-8")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rootless", result.stderr.lower())

    def test_missing_sysctl_fails_named(self):
        (self.state / "sysctl.json").write_text(
            json.dumps({"net.bridge.bridge-nf-call-iptables": "0", "net.bridge.bridge-nf-call-ip6tables": "1"}),
            encoding="utf-8",
        )
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bridge-nf-call-iptables", result.stderr)

    def test_permission_denied_fails_loud_without_euid_bypass(self):
        (self.state / "ipv4-permission-denied").write_text("1", encoding="utf-8")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Permission denied", result.stderr)

    def test_restore_failure_leaves_no_service_start_from_up(self):
        (self.state / "ipv4-restore-fail").write_text("1", encoding="utf-8")
        result = run_script(UP, self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(starts(self.state), [])
        self.assertEqual(leftover_tmp(self.state), [])

    def test_check_names_missing_drop_and_does_not_mutate(self):
        self.assertEqual(self.install().returncode, 0)
        firewall = load_firewall(self.state)
        firewall["ipv4"][OWNED_IPV4] = [rule for rule in firewall["ipv4"][OWNED_IPV4] if rule != ["-j", "DROP"]]
        write_firewall(self.state, firewall)
        before = (self.state / "firewall.json").read_text(encoding="utf-8")
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(OWNED_IPV4, result.stderr)
        self.assertIn("DROP", result.stderr)
        self.assertEqual((self.state / "firewall.json").read_text(encoding="utf-8"), before)

    def test_check_names_duplicate_and_shadowed_hooks(self):
        self.assertEqual(self.install().returncode, 0)
        firewall = load_firewall(self.state)
        hook = list(_owned_hooks(firewall["ipv4"]["DOCKER-USER"])[0])
        firewall["ipv4"]["DOCKER-USER"].insert(0, ["-i", BRIDGE, "-j", "ACCEPT"])
        firewall["ipv4"]["DOCKER-USER"].append(hook)
        write_firewall(self.state, firewall)
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DOCKER-USER", result.stderr)

    def test_check_is_read_only_on_healthy_policy(self):
        self.assertEqual(self.install().returncode, 0)
        before = (self.state / "firewall.json").read_text(encoding="utf-8")
        before_restore = len(_restore_lines(self.state))
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.state / "firewall.json").read_text(encoding="utf-8"), before)
        self.assertEqual(len(_restore_lines(self.state)), before_restore)

    def test_unknown_fake_command_fails(self):
        result = subprocess.run(
            ["iptables", "--totally-unknown"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=self.env,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported", result.stderr)

    def test_save_rejects_wait_flag(self):
        result = subprocess.run(
            ["iptables-save", "-w", "5", "-t", "filter"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=self.env,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unrecognized option", result.stderr)

    def test_canonical_save_round_trip_passes_check(self):
        self.assertEqual(self.install().returncode, 0)
        firewall = load_firewall(self.state)
        owned = firewall["ipv4"][OWNED_IPV4]
        owned[0] = ["-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "--ctdir", "REPLY", "-j", "RETURN"]
        write_firewall(self.state, firewall)
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_frag_match_is_not_equivalent_to_unconditional_drop(self):
        env = dict(self.env)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(ROOT / "deploy" / "firewall"), str(ROOT / "deploy"), env.get("PYTHONPATH", "")]
        )
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import policy; print(policy.rules_equivalent(['-m','frag','-j','DROP'],['-j','DROP']))",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")

    def test_seeded_ipv6_frag_only_drop_fails_check_naming_owned_chain(self):
        self.assertEqual(self.install().returncode, 0)
        firewall = load_firewall(self.state)
        firewall["ipv6"][OWNED_IPV6] = [["-m", "frag", "-j", "DROP"]]
        write_firewall(self.state, firewall)
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(OWNED_IPV6, result.stderr)

    def test_fake_save_retains_frag_module(self):
        restore = (
            "*filter\n"
            f":{OWNED_IPV6} - [0:0]\n"
            f"-A {OWNED_IPV6} -m frag -j DROP\n"
            "COMMIT\n"
        )
        restored = subprocess.run(
            ["ip6tables-restore", "-w", "5", "--noflush"],
            cwd=str(ROOT),
            input=restore,
            capture_output=True,
            text=True,
            env=self.env,
            check=False,
        )
        self.assertEqual(restored.returncode, 0, restored.stderr)
        result = subprocess.run(
            ["ip6tables-save", "-t", "filter"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=self.env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-m frag", result.stdout)
        self.assertRegex(result.stdout, rf"-A {OWNED_IPV6} .*?-m frag")

    def test_check_rejects_uncommented_jump_to_reserved_chain_without_mutation(self):
        self.assertEqual(self.install().returncode, 0)
        firewall = load_firewall(self.state)
        firewall["ipv4"]["INPUT"].append(["-i", "otherbridge", "-j", OWNED_IPV4])
        write_firewall(self.state, firewall)
        before = (self.state / "firewall.json").read_bytes()
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("INPUT", result.stderr)
        self.assertIn(OWNED_IPV4, result.stderr)
        self.assertEqual((self.state / "firewall.json").read_bytes(), before)

    def test_check_rejects_uncommented_goto_to_reserved_chain_without_mutation(self):
        self.assertEqual(self.install().returncode, 0)
        firewall = load_firewall(self.state)
        firewall["ipv4"]["INPUT"].append(["-i", "otherbridge", "-g", OWNED_IPV4])
        write_firewall(self.state, firewall)
        before = (self.state / "firewall.json").read_bytes()
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("INPUT", result.stderr)
        self.assertIn(OWNED_IPV4, result.stderr)
        self.assertEqual((self.state / "firewall.json").read_bytes(), before)

    def test_refuses_managed_hooks_for_a_different_bridge(self):
        self.assertEqual(self.install().returncode, 0)
        before = (self.state / "firewall.json").read_text(encoding="utf-8")
        other = dict(self.env)
        other["OCU_SANDBOX_NETWORK"] = "other-sandbox"
        other["OCU_SANDBOX_SUBNET"] = "172.32.0.0/24"
        other["OCU_SANDBOX_GATEWAY"] = "172.32.0.1"
        other["OCU_SANDBOX_EGRESS_ALLOW"] = "9.9.9.9/32"
        write_network(
            self.state,
            "other-sandbox",
            subnet="172.32.0.0/24",
            gateway="172.32.0.1",
            net_id="id-other-sandbox",
        )
        result = run_script(FIREWALL_INSTALL, other)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("different bridge", result.stderr.lower())
        self.assertEqual((self.state / "firewall.json").read_text(encoding="utf-8"), before)

    def test_packet_policy_allows_listed_and_drops_unlisted(self):
        self.assertEqual(self.install().returncode, 0)
        firewall = load_firewall(self.state)
        self.assertEqual(
            evaluate_packet(firewall, family="ipv4", chain="DOCKER-USER", in_iface=BRIDGE, src="172.31.0.20", dst="8.8.8.8"),
            "RETURN",
        )
        self.assertEqual(
            evaluate_packet(firewall, family="ipv4", chain="DOCKER-USER", in_iface=BRIDGE, src="172.31.0.20", dst="1.1.1.1"),
            "RETURN",
        )
        self.assertEqual(
            evaluate_packet(firewall, family="ipv4", chain="DOCKER-USER", in_iface=BRIDGE, src="172.31.0.20", dst="9.9.9.9"),
            "DROP",
        )
        self.assertEqual(
            evaluate_packet(firewall, family="ipv4", chain="DOCKER-USER", in_iface=BRIDGE, src="172.31.0.20", dst="10.0.0.5"),
            "DROP",
        )
        self.assertEqual(
            evaluate_packet(firewall, family="ipv4", chain="INPUT", in_iface=BRIDGE, src="172.31.0.20", dst="172.31.0.1"),
            "DROP",
        )

    def test_protected_destinations_override_broad_allow(self):
        result = self.install({"OCU_SANDBOX_EGRESS_ALLOW": "0.0.0.0/0"})
        self.assertEqual(result.returncode, 0, result.stderr)
        firewall = load_firewall(self.state)
        self.assertEqual(
            evaluate_packet(firewall, family="ipv4", chain="DOCKER-USER", in_iface=BRIDGE, src="172.31.0.20", dst="172.30.0.10"),
            "DROP",
        )
        self.assertEqual(
            evaluate_packet(firewall, family="ipv4", chain="DOCKER-USER", in_iface=BRIDGE, src="172.31.0.20", dst=METADATA_ADDR),
            "DROP",
        )
        self.assertEqual(
            evaluate_packet(firewall, family="ipv4", chain="DOCKER-USER", in_iface=BRIDGE, src="172.31.0.20", dst="8.8.8.8"),
            "RETURN",
        )

    def test_reply_exception_and_original_established_still_denied(self):
        self.assertEqual(self.install().returncode, 0)
        firewall = load_firewall(self.state)
        self.assertEqual(
            evaluate_packet(
                firewall,
                family="ipv4",
                chain="DOCKER-USER",
                in_iface=BRIDGE,
                src="172.31.0.20",
                dst="172.30.0.10",
                ctstate="ESTABLISHED",
                ctdir="REPLY",
            ),
            "RETURN",
        )
        self.assertEqual(
            evaluate_packet(
                firewall,
                family="ipv4",
                chain="DOCKER-USER",
                in_iface=BRIDGE,
                src="172.31.0.20",
                dst="9.9.9.9",
                ctstate="ESTABLISHED",
                ctdir="ORIGINAL",
            ),
            "DROP",
        )

    def test_forged_source_and_foreign_traffic(self):
        self.assertEqual(self.install().returncode, 0)
        firewall = load_firewall(self.state)
        self.assertEqual(
            evaluate_packet(firewall, family="ipv4", chain="FORWARD", in_iface=BRIDGE, src="8.8.8.8", dst="1.1.1.1"),
            "ACCEPT",
        )
        self.assertEqual(
            evaluate_packet(firewall, family="ipv4", chain="FORWARD", in_iface=BRIDGE, src="8.8.8.8", dst="9.9.9.9"),
            "DROP",
        )
        self.assertEqual(
            evaluate_packet(firewall, family="ipv4", chain="FORWARD", in_iface="eth0", src="10.9.9.9", dst="9.9.9.9"),
            "ACCEPT",
        )
        self.assertEqual(
            evaluate_packet(firewall, family="ipv4", chain="DOCKER-USER", in_iface="eth0", src="172.31.0.20", dst="9.9.9.9"),
            "RETURN",
        )

    def test_ipv6_sandbox_ingress_is_dropped(self):
        self.assertEqual(self.install().returncode, 0)
        firewall = load_firewall(self.state)
        self.assertEqual(
            evaluate_packet(firewall, family="ipv6", chain="INPUT", in_iface=BRIDGE, src="fe80::1", dst="fe80::2"),
            "DROP",
        )
        self.assertEqual(
            evaluate_packet(firewall, family="ipv6", chain="FORWARD", in_iface=BRIDGE, src="2001:db8::1", dst="2001:db8::2"),
            "DROP",
        )

    def test_up_runs_installer_and_checker_before_starts_on_reused_bridge(self):
        result = run_script(UP, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(starts(self.state), ["core", "webui", "proxy"])
        recorded = ops(self.state)
        firewall_ops = [line for line in recorded if "iptables" in line or "ip6tables" in line]
        compose_up = [line for line in recorded if " compose " in f" {line} " and " up " in f" {line} "]
        self.assertTrue(firewall_ops)
        self.assertTrue(compose_up)
        first_up = recorded.index(compose_up[0])
        last_firewall_before_up = max(i for i, line in enumerate(recorded) if i < first_up and ("iptables" in line or "ip6tables" in line or "sysctl" in line))
        self.assertLess(last_firewall_before_up, first_up)
        check = self.check()
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_fresh_host_up_uses_configured_control_cidr(self):
        (self.state / "networks" / f"{CONTROL_NETWORK}.json").unlink()
        result = run_script(UP, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(starts(self.state), ["core", "webui", "proxy"])
        firewall = load_firewall(self.state)
        destinations = [_flag(rule, "-d") for rule in firewall["ipv4"][OWNED_IPV4]]
        self.assertIn("172.30.0.0/24", destinations)
        self.assertEqual(
            evaluate_packet(firewall, family="ipv4", chain="DOCKER-USER", in_iface=BRIDGE, src="172.31.0.20", dst="172.30.0.10"),
            "DROP",
        )

    def test_present_control_mismatch_fails_without_mutation(self):
        write_network(self.state, CONTROL_NETWORK, subnet="172.33.0.0/24", gateway="172.33.0.1")
        before = (self.state / "firewall.json").read_text(encoding="utf-8")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("incompatible", result.stderr)
        self.assertEqual((self.state / "firewall.json").read_text(encoding="utf-8"), before)
        self.assertEqual(starts(self.state), [])

    def test_control_inspect_daemon_error_fails_closed(self):
        (self.state / "networks" / f"{CONTROL_NETWORK}.json").unlink()
        (self.state / "inspect-error.json").write_text(
            json.dumps({"code": 1, "message": "permission denied"}),
            encoding="utf-8",
        )
        before = (self.state / "firewall.json").read_text(encoding="utf-8")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("inspect failed", result.stderr)
        self.assertEqual((self.state / "firewall.json").read_text(encoding="utf-8"), before)

    def test_malformed_gateway_is_named_config_error(self):
        before = (self.state / "firewall.json").read_text(encoding="utf-8")
        result = self.install({"OCU_SANDBOX_GATEWAY": "abc"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("OCU_SANDBOX_GATEWAY", result.stderr)
        self.assertIn("IPv4 address", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual((self.state / "firewall.json").read_text(encoding="utf-8"), before)

    def test_unset_allowlist_in_up_starts_no_service(self):
        env = dict(self.env)
        del env["OCU_SANDBOX_EGRESS_ALLOW"]
        result = run_script(UP, env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("OCU_SANDBOX_EGRESS_ALLOW", result.stderr)
        self.assertEqual(starts(self.state), [])
        self.assertEqual(leftover_tmp(self.state), [])

    def test_blank_allowlist_in_up_is_deny_all_not_unset(self):
        env = dict(self.env)
        env["OCU_SANDBOX_EGRESS_ALLOW"] = ""
        result = run_script(UP, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(starts(self.state), ["core", "webui", "proxy"])
        firewall = load_firewall(self.state)
        self.assertEqual(
            evaluate_packet(firewall, family="ipv4", chain="DOCKER-USER", in_iface=BRIDGE, src="172.31.0.20", dst="8.8.8.8"),
            "DROP",
        )

    def test_installer_restore_failure_in_up_starts_no_service(self):
        (self.state / "ipv4-restore-fail").write_text("1", encoding="utf-8")
        result = run_script(UP, self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(starts(self.state), [])
        self.assertEqual(leftover_tmp(self.state), [])

    def test_check_failure_after_install_blocks_start(self):
        (self.state / "check-fail-once").write_text("1", encoding="utf-8")
        result = run_script(UP, self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sandbox egress policy check failed", result.stderr)
        self.assertEqual(starts(self.state), [])
        self.assertEqual(leftover_tmp(self.state), [])

    def test_concurrent_foreign_insert_preserves_foreign_rule(self):
        concurrent = ["-s", "10.8.8.8/32", "-j", "ACCEPT", "-m", "comment", "--comment", "foreign-concurrent"]
        (self.state / "inject-foreign-once").write_text("1", encoding="utf-8")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        firewall = load_firewall(self.state)
        self.assertIn(FOREIGN_RULE, firewall["ipv4"]["DOCKER-USER"])
        self.assertIn(concurrent, firewall["ipv4"]["DOCKER-USER"])
        owned = _owned_hooks(firewall["ipv4"]["DOCKER-USER"])
        self.assertEqual(len(owned), 1)
        self.assertEqual(firewall["ipv4"]["DOCKER-USER"][0], owned[0])
        self.assertEqual(
            evaluate_packet(firewall, family="ipv4", chain="FORWARD", in_iface="eth0", src="10.8.8.8", dst="8.8.8.8"),
            "ACCEPT",
        )
        recorded = "\n".join(ops(self.state))
        self.assertNotRegex(recorded, r"iptables .* -D DOCKER-USER [0-9]+")

    def test_native_lock_serializes_cooperating_installers(self):
        hold = self.state / "restore-hold"
        hold.write_text("1", encoding="utf-8")
        env = dict(self.env)
        env["FAKE_FIREWALL_HOLD_RESTORE"] = str(hold)
        lock = Path(self.env["OCU_SANDBOX_EGRESS_LOCK"])
        first = subprocess.Popen(
            ["bash", str(FIREWALL_INSTALL)],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        second = None
        first_err = ""
        second_err = ""
        probe_fd = None
        barrier_error = None
        try:
            entered = _wait(lambda: (self.state / "entered-restore").exists())
            if not (entered and hold.exists() and first.poll() is None):
                barrier_error = "first installer never entered held restore"
            else:
                try:
                    probe_fd = os.open(lock, os.O_RDWR)
                    fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                except OSError as exc:
                    barrier_error = f"could not open lock file {lock}: {exc}"
                else:
                    barrier_error = "native lock was not held exclusively by the first installer"
            if barrier_error is None:
                restores_while_held = len(_restore_lines(self.state))
                firewall_while_held = (self.state / "firewall.json").read_bytes()
                second_env = dict(self.env)
                second_env["XDG_RUNTIME_DIR"] = str(self.state / "xdg-other")
                second = subprocess.Popen(
                    ["bash", str(FIREWALL_INSTALL)],
                    cwd=str(ROOT),
                    env=second_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                deadline = time.time() + 1.0
                while time.time() < deadline:
                    if not hold.exists():
                        barrier_error = "hold file vanished during second-installer window"
                        break
                    if second.poll() is not None:
                        barrier_error = "second installer exited while first still held restore"
                        break
                    if len(_restore_lines(self.state)) != restores_while_held or (
                        self.state / "firewall.json"
                    ).read_bytes() != firewall_while_held:
                        barrier_error = "second installer mutated firewall while the first held the lock"
                        break
                    time.sleep(0.05)
                else:
                    hold.unlink()
        finally:
            hold.unlink(missing_ok=True)
            if probe_fd is not None:
                os.close(probe_fd)
            collected = {}
            for slot, proc in (("first", first), ("second", second)):
                if proc is None:
                    continue
                running = proc.poll() is None
                if barrier_error is not None and running:
                    proc.terminate()
                    try:
                        proc.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    running = proc.poll() is None
                try:
                    _out, err = proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    if running:
                        proc.kill()
                    _out, err = proc.communicate(timeout=5)
                except ValueError:
                    err = ""
                collected[slot] = err or ""
                for pipe in (proc.stdout, proc.stderr):
                    if pipe is not None:
                        pipe.close()
            first_err = collected.get("first", first_err)
            second_err = collected.get("second", second_err)
        if barrier_error:
            self.fail(
                f"{barrier_error}; first_stderr={first_err!r}; second_stderr={second_err!r}"
            )
        self.assertEqual(first.returncode, 0, first_err)
        self.assertEqual(second.returncode, 0, second_err)
        firewall = load_firewall(self.state)
        owned = _owned_hooks(firewall["ipv4"]["DOCKER-USER"])
        self.assertEqual(len(owned), 1)
        self.assertEqual(firewall["ipv4"]["DOCKER-USER"][0], owned[0])

    def test_term_during_held_restore_releases_lock(self):
        hold = self.state / "restore-hold"
        hold.write_text("1", encoding="utf-8")
        env = dict(self.env)
        env["FAKE_FIREWALL_HOLD_RESTORE"] = str(hold)
        process = subprocess.Popen(
            ["bash", str(UP)],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            self.assertTrue(_wait(lambda: (self.state / "entered-restore").exists()), "up never entered restore")
            process.send_signal(signal.SIGTERM)
            self.assertTrue(_wait(lambda: process.poll() is not None, timeout=8), "up did not exit after TERM")
            self.assertNotEqual(process.returncode, 0)
            self.assertEqual(starts(self.state), [])
            self.assertEqual(leftover_tmp(self.state), [])
        finally:
            hold.unlink(missing_ok=True)
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        follow = self.install()
        self.assertEqual(follow.returncode, 0, follow.stderr)

    def test_hup_during_held_check_releases_lock(self):
        hold = self.state / "save-hold"
        hold.write_text("1", encoding="utf-8")
        env = dict(self.env)
        env["FAKE_FIREWALL_HOLD_SAVE"] = str(hold)
        process = subprocess.Popen(
            ["bash", str(FIREWALL_CHECK)],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            self.assertTrue(_wait(lambda: (self.state / "entered-save").exists()), "check never entered save")
            process.send_signal(signal.SIGHUP)
            self.assertTrue(_wait(lambda: process.poll() is not None, timeout=8), "check did not exit after HUP")
            self.assertNotEqual(process.returncode, 0)
        finally:
            hold.unlink(missing_ok=True)
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        follow = self.install()
        self.assertEqual(follow.returncode, 0, follow.stderr)

    def test_lock_path_is_private_and_not_world_writable(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        lock = Path(self.env["OCU_SANDBOX_EGRESS_LOCK"])
        self.assertTrue(lock.exists())
        mode = lock.stat().st_mode & 0o777
        self.assertEqual(mode & 0o022, 0)

    def test_group_writable_lock_directory_is_refused(self):
        parent = self.state / "lock-dir"
        parent.mkdir()
        os.chmod(parent, 0o775)
        env = dict(self.env)
        env["OCU_SANDBOX_EGRESS_LOCK"] = str(parent / "ocu-sandbox-egress.lock")
        before = (self.state / "firewall.json").read_text(encoding="utf-8")
        result = run_script(FIREWALL_INSTALL, env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("world/group writable", result.stderr)
        self.assertEqual((self.state / "firewall.json").read_text(encoding="utf-8"), before)

    def test_symlinked_lock_path_is_refused_without_touching_target(self):
        target = self.state / "lock-target"
        target.write_text("keep", encoding="utf-8")
        os.chmod(target, 0o644)
        link = self.state / "ocu-sandbox-egress.lock"
        link.unlink(missing_ok=True)
        os.symlink(target, link)
        env = dict(self.env)
        env["OCU_SANDBOX_EGRESS_LOCK"] = str(link)
        before = (self.state / "firewall.json").read_text(encoding="utf-8")
        result = run_script(FIREWALL_INSTALL, env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink", result.stderr)
        self.assertEqual(target.read_text(encoding="utf-8"), "keep")
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)
        self.assertEqual((self.state / "firewall.json").read_text(encoding="utf-8"), before)

    def test_default_lock_path_is_host_stable(self):
        env = dict(self.env)
        del env["OCU_SANDBOX_EGRESS_LOCK"]
        env["XDG_RUNTIME_DIR"] = str(self.state / "xdg-ignored")
        result = run_script(FIREWALL_INSTALL, env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/run/ocu-sandbox-egress", result.stderr)
        self.assertNotIn(str(self.state / "xdg-ignored"), result.stderr)


if __name__ == "__main__":
    unittest.main()
