# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Public DNS policy syntax, empty override, and ordered compatibility."""

from __future__ import annotations

import ipaddress
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[2] / "computer-use-server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import sandbox_dns


def test_absent_policy_is_development_mode():
    assert sandbox_dns.parse_sandbox_dns(None) is None


def test_empty_policy_is_nonempty_local_override():
    assert sandbox_dns.parse_sandbox_dns("") == ["127.0.0.11"]
    assert sandbox_dns.parse_sandbox_dns("   ") == ["127.0.0.11"]


def test_ordered_unique_ipv4_are_canonical():
    assert sandbox_dns.parse_sandbox_dns("8.8.8.8,1.1.1.1") == ["8.8.8.8", "1.1.1.1"]
    assert sandbox_dns.parse_sandbox_dns(" 8.8.8.8 , 1.1.1.1 ") == ["8.8.8.8", "1.1.1.1"]


@pytest.mark.parametrize(
    "raw",
    [
        "8.8.8.8,8.8.8.8",
        "8.8.8.8,",
        ",8.8.8.8",
        "not-an-ip",
        "2001:db8::1",
        "8.8.8.8/32",
        "8.8.8.8,1.1.1.1,9.9.9.9,8.8.4.4",
    ],
)
def test_invalid_syntax_is_rejected(raw):
    with pytest.raises(sandbox_dns.SandboxDnsError) as caught:
        sandbox_dns.parse_sandbox_dns(raw)
    assert "OCU_SANDBOX_DNS" in str(caught.value)


def test_compatibility_is_ordered_and_rejects_missing_empty_or_wrong_type():
    expected = ["8.8.8.8", "1.1.1.1"]
    assert sandbox_dns.dns_compatible(["8.8.8.8", "1.1.1.1"], expected)
    assert not sandbox_dns.dns_compatible(["1.1.1.1", "8.8.8.8"], expected)
    assert not sandbox_dns.dns_compatible(None, expected)
    assert not sandbox_dns.dns_compatible([], expected)
    assert not sandbox_dns.dns_compatible("8.8.8.8", expected)
    assert sandbox_dns.dns_compatible(None, None)


def test_protected_and_unlisted_resolvers_are_named():
    allow = [ipaddress.ip_network("0.0.0.0/0")]
    control = ipaddress.ip_network("172.30.0.0/24")
    metadata = ipaddress.ip_network("169.254.169.254/32")
    assert sandbox_dns.resolver_violations(["8.8.8.8"], allow, control, metadata) == []
    assert sandbox_dns.resolver_violations(["169.254.169.254"], allow, control, metadata) == [
        "169.254.169.254"
    ]
    assert sandbox_dns.resolver_violations(["172.30.0.9"], allow, control, metadata) == ["172.30.0.9"]
    assert sandbox_dns.resolver_violations(["9.9.9.9"], [ipaddress.ip_network("8.8.8.8/32")], control, metadata) == [
        "9.9.9.9"
    ]
    assert sandbox_dns.resolver_violations(["127.0.0.11"], allow, control, metadata) == []
