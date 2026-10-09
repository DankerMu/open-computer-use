# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Shared bootstrap/deployment input boundaries."""

import sys
import unittest

from support import ROOT

sys.path.insert(0, str(ROOT / "deploy"))
from settings import origin, port


class SettingsTests(unittest.TestCase):
    def test_origin_identity_preserves_effective_ports_and_ipv6(self):
        cases = {
            "http://docs.test": ("http", "docs.test", 80),
            "http://docs.test:80": ("http", "docs.test", 80),
            "HTTPS://docs.test": ("https", "docs.test", 443),
            "https://docs.test:8443": ("https", "docs.test", 8443),
            "http://[::1]:8083": ("http", "::1", 8083),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(origin("DOCSERVER_ORIGIN", value), expected)

    def test_origin_rejects_non_origins_without_echoing_credentials(self):
        for value in ("", "docs.test", "ftp://docs.test", "http://DOCS.test",
                      "http://docs.test/", "http://docs.test/path", "http://docs.test?",
                      "http://docs.test#", "http://user:secret-canary@docs.test",
                      "http://docs.test:0", "http://docs.test:65536", "http://docs.test:abc",
                      "http://[::1", "http://docs.test\n", "http://例.test"):
            with self.subTest(value=value):
                with self.assertRaises(SystemExit) as raised:
                    origin("DOCSERVER_ORIGIN", value)
                self.assertIn("DOCSERVER_ORIGIN", str(raised.exception))
                self.assertNotIn("secret-canary", str(raised.exception))

    def test_port_accepts_boundaries_and_decimal_spelling(self):
        for value, expected in (("1", 1), ("65535", 65535), ("00080", 80)):
            with self.subTest(value=value):
                self.assertEqual(port("OFFICE_PORT", value), expected)

    def test_port_rejects_nondecimal_and_out_of_range_values(self):
        for value in ("", "0", "65536", "-1", "+80", "1.5", " 80", "８０", "9" * 5000):
            with self.subTest(value=value):
                with self.assertRaises(SystemExit) as raised:
                    port("OFFICE_PORT", value)
                self.assertIn("OFFICE_PORT", str(raised.exception))
