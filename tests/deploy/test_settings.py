# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Shared bootstrap/deployment input boundaries."""

from pathlib import Path
import sys
import tempfile
import unittest

from support import FIXTURE_HOST_ROOT, FIXTURE_HOST_ZONE, ROOT, write_host_root

sys.path.insert(0, str(ROOT / "deploy"))
from settings import HOST_ROOT, host_time_zone, origin, port


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


class HostTimeZoneTests(unittest.TestCase):
    def setUp(self):
        context = tempfile.TemporaryDirectory(prefix="ocu-host-zone-")
        self.addCleanup(context.cleanup)
        self.root = Path(context.name) / "host"

    def test_production_reads_the_real_root(self):
        self.assertEqual(HOST_ROOT, Path("/"))

    def test_link_into_zoneinfo_names_the_zone(self):
        write_host_root(self.root, localtime="Asia/Shanghai", zones=("Asia/Shanghai",))
        self.assertEqual(host_time_zone(self.root), "Asia/Shanghai")
        self.assertEqual(host_time_zone(FIXTURE_HOST_ROOT), FIXTURE_HOST_ZONE)
        # posix/ holds the same data as the plain name; container images ship only the plain one.
        write_host_root(self.root, localtime="posix/Asia/Shanghai",
                        zones=("Asia/Shanghai", "posix/Asia/Shanghai"))
        self.assertEqual(host_time_zone(self.root), "Asia/Shanghai")
        write_host_root(self.root, localtime=b"TZif-copy", timezone="posix/Europe/Berlin\n",
                        zones=("Europe/Berlin", "posix/Europe/Berlin"))
        self.assertEqual(host_time_zone(self.root), "Europe/Berlin")

    def test_relative_and_chained_links_resolve_to_the_final_zone(self):
        write_host_root(self.root, zones=("Asia/Shanghai",))
        link = self.root / "etc" / "localtime"
        link.symlink_to("../usr/share/zoneinfo/Asia/Shanghai")
        self.assertEqual(host_time_zone(self.root), "Asia/Shanghai")
        alias = self.root / "usr/share/zoneinfo/PRC"
        alias.symlink_to("Asia/Shanghai")
        link.unlink()
        link.symlink_to(alias)
        self.assertEqual(host_time_zone(self.root), "Asia/Shanghai")

    def test_utc_host_resolves_like_any_zone(self):
        write_host_root(self.root, localtime="Etc/UTC", zones=("Etc/UTC",))
        self.assertEqual(host_time_zone(self.root), "Etc/UTC")

    def test_etc_timezone_is_the_fallback(self):
        cases = {
            "regular localtime": b"TZif-copy",
            "no localtime": None,
        }
        for label, localtime in cases.items():
            with self.subTest(case=label):
                write_host_root(self.root, localtime=localtime, timezone="Europe/Berlin\nignored\n",
                                zones=("Europe/Berlin",))
                self.assertEqual(host_time_zone(self.root), "Europe/Berlin")
        outside = self.root.parent / "elsewhere" / "Asia" / "Tokyo"
        outside.parent.mkdir(parents=True)
        outside.write_bytes(b"zone outside zoneinfo")
        write_host_root(self.root, timezone="Asia/Tokyo\n", zones=("Asia/Tokyo",))
        (self.root / "etc" / "localtime").symlink_to(outside)
        self.assertEqual(host_time_zone(self.root), "Asia/Tokyo")

    def test_unresolvable_hosts_are_refused(self):
        cases = {
            "nothing": dict(localtime=None, timezone=None),
            "regular localtime, no timezone file": dict(localtime=b"TZif-copy", timezone=None),
            "empty timezone file": dict(localtime=None, timezone=""),
            "blank first line": dict(localtime=None, timezone="\nEurope/Berlin\n"),
            "dot-dot in the stored name": dict(localtime=None, timezone="Asia/../Asia/Shanghai\n"),
            "absolute stored name": dict(localtime=None, timezone="/usr/share/zoneinfo/Asia/Shanghai\n"),
            "stored name with no zone file": dict(localtime=None, timezone="Mars/Olympus\n"),
            "link to a missing zone file": dict(localtime="Mars/Olympus", timezone=None),
            "link to a zone directory": dict(localtime="Asia", timezone=None),
            "stored name of a zone directory": dict(localtime=None, timezone="Asia\n"),
            "dangling link, unusable fallback": dict(localtime="Mars/Olympus", timezone="Mars/Olympus\n"),
            "posix/ link without the plain zone": dict(localtime="posix/Europe/Paris", timezone=None),
        }
        right = {
            "right/ link": dict(localtime="right/Asia/Shanghai", timezone=None),
            "right/ stored name": dict(localtime=None, timezone="right/Asia/Shanghai\n"),
        }
        zones = ("Asia/Shanghai", "Europe/Berlin", "posix/Europe/Paris", "right/Asia/Shanghai")
        for label, layout in {**cases, **right}.items():
            with self.subTest(case=label):
                write_host_root(self.root, zones=zones, **layout)
                with self.assertRaises(ValueError) as raised:
                    host_time_zone(self.root)
                self.assertTrue(str(raised.exception))
                if label in right:
                    # Leap-second zones are absent from container images (#708).
                    self.assertIn("right/", str(raised.exception))

    def test_link_into_zoneinfo_is_not_overridden_by_etc_timezone(self):
        write_host_root(self.root, localtime="Asia/Shanghai", timezone="Europe/Berlin\n",
                        zones=("Asia/Shanghai", "Europe/Berlin"))
        self.assertEqual(host_time_zone(self.root), "Asia/Shanghai")
