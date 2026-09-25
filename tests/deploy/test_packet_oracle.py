# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Call-stack semantics for the independent packet oracle."""

from __future__ import annotations

import unittest

from packet_oracle import evaluate_packet


class PacketOracleTests(unittest.TestCase):
    def test_user_chain_fallthrough_resumes_caller(self):
        firewall = {
            "ipv4": {
                "FORWARD": [["-j", "DOCKER-USER"], ["-j", "ACCEPT"]],
                "DOCKER-USER": [["-i", "eth0", "-j", "DROP"]],
            },
            "policies": {"ipv4": {"FORWARD": "DROP", "DOCKER-USER": "-"}},
        }
        self.assertEqual(
            evaluate_packet(firewall, family="ipv4", chain="FORWARD", in_iface="br0", src="1.1.1.1", dst="8.8.8.8"),
            "ACCEPT",
        )

    def test_builtin_fallthrough_uses_seeded_policy(self):
        firewall = {
            "ipv4": {"INPUT": [["-i", "lo", "-j", "ACCEPT"]]},
            "policies": {"ipv4": {"INPUT": "DROP"}},
        }
        self.assertEqual(
            evaluate_packet(firewall, family="ipv4", chain="INPUT", in_iface="eth0", src="1.1.1.1", dst="8.8.8.8"),
            "DROP",
        )
        firewall["policies"]["ipv4"]["INPUT"] = "ACCEPT"
        self.assertEqual(
            evaluate_packet(firewall, family="ipv4", chain="INPUT", in_iface="eth0", src="1.1.1.1", dst="8.8.8.8"),
            "ACCEPT",
        )

    def test_nested_return_resumes_caller(self):
        firewall = {
            "ipv4": {
                "FORWARD": [["-j", "INNER"], ["-j", "DROP"]],
                "INNER": [["-j", "RETURN"]],
            },
            "policies": {"ipv4": {"FORWARD": "ACCEPT", "INNER": "-"}},
        }
        self.assertEqual(
            evaluate_packet(firewall, family="ipv4", chain="FORWARD", in_iface="br0", src="1.1.1.1", dst="8.8.8.8"),
            "DROP",
        )

    def test_chain_scoped_user_fallthrough_is_return(self):
        firewall = {
            "ipv4": {"DOCKER-USER": [["-i", "eth0", "-j", "ACCEPT"]]},
            "policies": {"ipv4": {"DOCKER-USER": "-"}},
        }
        self.assertEqual(
            evaluate_packet(firewall, family="ipv4", chain="DOCKER-USER", in_iface="br0", src="1.1.1.1", dst="8.8.8.8"),
            "RETURN",
        )


if __name__ == "__main__":
    unittest.main()
