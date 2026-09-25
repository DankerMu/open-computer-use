# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Isolated policy faults the parent can restore against the independent judges."""

from __future__ import annotations

from pathlib import Path
import unittest

from packet_oracle import evaluate_packet
from support import (
    CONTROL_NETWORK,
    FIREWALL_CHECK,
    FIREWALL_INSTALL,
    METADATA_ADDR,
    OWNED_IPV4,
    OWNED_IPV6,
    SANDBOX_NETWORK,
    UP,
    fake_env,
    load_firewall,
    run_script,
    seed_healthy_host,
    tmp_dir,
    write_fake_configs,
    write_firewall,
    write_network,
)


BRIDGE = "br-id-ocu-sandb"


def starts(state_dir: Path) -> list[str]:
    log = state_dir / "starts.log"
    if not log.exists():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line]


class EgressFaultQualificationTests(unittest.TestCase):
    def setUp(self):
        self.context = tmp_dir()
        self.state = Path(self.context.name)
        write_fake_configs(self.state)
        seed_healthy_host(self.state)
        write_network(self.state, CONTROL_NETWORK, subnet="172.30.0.0/24", gateway="172.30.0.1")
        write_network(self.state, SANDBOX_NETWORK, subnet="172.31.0.0/24", gateway="172.31.0.1")
        self.env = fake_env(self.state)

    def tearDown(self):
        self.context.cleanup()

    def install(self):
        result = run_script(FIREWALL_INSTALL, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return load_firewall(self.state)

    def test_missing_final_drop_is_rejected_by_check_and_packet_oracle(self):
        firewall = self.install()
        firewall["ipv4"][OWNED_IPV4] = [rule for rule in firewall["ipv4"][OWNED_IPV4] if rule != ["-j", "DROP"]]
        write_firewall(self.state, firewall)
        result = run_script(FIREWALL_CHECK, self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DROP", result.stderr)
        self.assertIn(OWNED_IPV4, result.stderr)
        self.assertNotEqual(
            evaluate_packet(firewall, family="ipv4", chain="DOCKER-USER", in_iface=BRIDGE, src="172.31.0.20", dst="9.9.9.9"),
            "DROP",
        )

    def test_reversed_deny_allow_order_is_rejected(self):
        firewall = self.install()
        owned = firewall["ipv4"][OWNED_IPV4]
        drops = [rule for rule in owned if rule[-1] == "DROP" and rule != ["-j", "DROP"]]
        reply = [rule for rule in owned if "--ctdir" in rule]
        allows = [rule for rule in owned if "-j" in rule and rule[rule.index("-j") + 1] == "RETURN" and "--ctdir" not in rule]
        final = [rule for rule in owned if rule == ["-j", "DROP"]]
        firewall["ipv4"][OWNED_IPV4] = reply + allows + drops + final
        write_firewall(self.state, firewall)
        result = run_script(FIREWALL_CHECK, self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(OWNED_IPV4, result.stderr)
        self.assertEqual(
            evaluate_packet(firewall, family="ipv4", chain="DOCKER-USER", in_iface=BRIDGE, src="172.31.0.20", dst=METADATA_ADDR),
            "RETURN",
        )

    def test_missing_reply_exception_is_rejected(self):
        firewall = self.install()
        firewall["ipv4"][OWNED_IPV4] = [rule for rule in firewall["ipv4"][OWNED_IPV4] if "--ctdir" not in rule]
        write_firewall(self.state, firewall)
        result = run_script(FIREWALL_CHECK, self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REPLY", result.stderr)
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
            "DROP",
        )

    def test_missing_input_hook_is_rejected(self):
        firewall = self.install()
        firewall["ipv4"]["INPUT"] = [rule for rule in firewall["ipv4"]["INPUT"] if OWNED_IPV4 not in rule]
        write_firewall(self.state, firewall)
        result = run_script(FIREWALL_CHECK, self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("INPUT", result.stderr)
        self.assertEqual(
            evaluate_packet(firewall, family="ipv4", chain="INPUT", in_iface=BRIDGE, src="172.31.0.20", dst="172.31.0.1"),
            "POLICY",
        )

    def test_missing_ipv6_hook_is_rejected(self):
        firewall = self.install()
        firewall["ipv6"]["FORWARD"] = [rule for rule in firewall["ipv6"]["FORWARD"] if OWNED_IPV6 not in rule]
        write_firewall(self.state, firewall)
        result = run_script(FIREWALL_CHECK, self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FORWARD", result.stderr)
        self.assertIn(OWNED_IPV6, result.stderr)
        self.assertEqual(
            evaluate_packet(firewall, family="ipv6", chain="FORWARD", in_iface=BRIDGE, src="fe80::1", dst="fe80::2"),
            "POLICY",
        )

    def test_source_subnet_only_hook_is_rejected(self):
        firewall = self.install()
        firewall["ipv4"]["DOCKER-USER"] = [
            ["-s", "172.31.0.0/24", "-j", OWNED_IPV4, "-m", "comment", "--comment", "ocu-sandbox-egress"]
            if OWNED_IPV4 in rule
            else rule
            for rule in firewall["ipv4"]["DOCKER-USER"]
        ]
        write_firewall(self.state, firewall)
        result = run_script(FIREWALL_CHECK, self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("-i", result.stderr)
        self.assertEqual(
            evaluate_packet(firewall, family="ipv4", chain="DOCKER-USER", in_iface=BRIDGE, src="8.8.8.8", dst="9.9.9.9"),
            "POLICY",
        )

    def test_duplicate_or_bypassed_jump_is_rejected(self):
        firewall = self.install()
        hook = ["-i", BRIDGE, "-j", OWNED_IPV4, "-m", "comment", "--comment", "ocu-sandbox-egress"]
        firewall["ipv4"]["DOCKER-USER"].insert(0, ["-i", BRIDGE, "-j", "ACCEPT"])
        firewall["ipv4"]["DOCKER-USER"].append(hook)
        write_firewall(self.state, firewall)
        result = run_script(FIREWALL_CHECK, self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DOCKER-USER", result.stderr)
        self.assertEqual(
            evaluate_packet(firewall, family="ipv4", chain="DOCKER-USER", in_iface=BRIDGE, src="172.31.0.20", dst="9.9.9.9"),
            "ACCEPT",
        )

    def test_ignored_preflight_failure_must_not_start_services(self):
        del self.env["OCU_SANDBOX_EGRESS_ALLOW"]
        result = run_script(UP, self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(starts(self.state), [])
        self.assertIn("OCU_SANDBOX_EGRESS_ALLOW", result.stderr)


if __name__ == "__main__":
    unittest.main()
