# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Tests for resolving a sandbox service address from published ports.

CDP (9222) and ttyd (7681) are reached through the engine-assigned published
host address and port. Missing publication is unavailable. Host-network and
wildcard HostIp still parse to loopback for the shared-namespace address
contract; that parser is not a managed-launch isolation exception.

Run: cd computer-use-server && python -m pytest ../tests/orchestrator/test_sandbox_addressing.py -v
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'computer-use-server'))

import docker_manager
from docker_manager import (
    _published_address,
    get_container_service_address,
    CDP_PORT,
    TTYD_PORT,
)


def _container(attrs, status="running"):
    c = MagicMock()
    c.attrs = attrs
    c.status = status
    c.reload = MagicMock()
    return c


class PublishedAddressTests(unittest.TestCase):
    """_published_address reads the engine's own port mapping."""

    def test_returns_host_port_when_published(self):
        c = _container({"NetworkSettings": {"Ports": {
            "9222/tcp": [{"HostIp": "127.0.0.1", "HostPort": "49153"}],
        }}})
        self.assertEqual(_published_address(c, 9222), "127.0.0.1:49153")

    def test_host_port_differs_from_container_port(self):
        """The engine assigns the host side; callers must not assume they match."""
        c = _container({"NetworkSettings": {"Ports": {
            "9222/tcp": [{"HostIp": "127.0.0.1", "HostPort": "32768"}],
        }}})
        self.assertNotIn(":9222", _published_address(c, 9222))

    def test_wildcard_host_ip_is_narrowed_to_loopback(self):
        """0.0.0.0 means "all interfaces"; the shared-namespace parser uses loopback."""
        for wildcard in ("0.0.0.0", "::", ""):
            c = _container({"NetworkSettings": {"Ports": {
                "7681/tcp": [{"HostIp": wildcard, "HostPort": "40000"}],
            }}})
            self.assertEqual(_published_address(c, 7681), "127.0.0.1:40000")

    def test_concrete_gateway_binding_is_returned_verbatim(self):
        c = _container({"NetworkSettings": {"Ports": {
            "9222/tcp": [{"HostIp": "172.31.0.1", "HostPort": "49153"}],
        }}})
        self.assertEqual(_published_address(c, 9222), "172.31.0.1:49153")

    def test_ports_are_resolved_independently(self):
        """CDP and the terminal get different host ports and must not be confused."""
        c = _container({"NetworkSettings": {"Ports": {
            "9222/tcp": [{"HostIp": "127.0.0.1", "HostPort": "49153"}],
            "7681/tcp": [{"HostIp": "127.0.0.1", "HostPort": "49154"}],
        }}})
        self.assertEqual(_published_address(c, CDP_PORT), "127.0.0.1:49153")
        self.assertEqual(_published_address(c, TTYD_PORT), "127.0.0.1:49154")

    def test_host_networking_uses_the_container_port_on_loopback(self):
        """Host netns: the port IS loopback, and Ports is empty — that is not "unpublished"."""
        c = _container({
            "HostConfig": {"NetworkMode": "host"},
            "NetworkSettings": {"Ports": {}},
        })
        self.assertEqual(_published_address(c, 9222), "127.0.0.1:9222")
        self.assertEqual(_published_address(c, 7681), "127.0.0.1:7681")

    def test_returns_none_when_not_published(self):
        for ports in ({}, {"9222/tcp": None}, {"9222/tcp": []}):
            c = _container({"NetworkSettings": {"Ports": ports}})
            self.assertIsNone(_published_address(c, 9222))

    def test_survives_a_missing_networksettings(self):
        self.assertIsNone(_published_address(_container({}), 9222))


class ServiceAddressTests(unittest.TestCase):
    """get_container_service_address uses assigned published ports only."""

    def setUp(self):
        self.client = MagicMock()
        patcher = patch.object(docker_manager, "get_docker_client", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_published_gateway_port_is_used_without_network_mutation(self):
        self.client.containers.get.return_value = _container({"NetworkSettings": {
            "Ports": {"9222/tcp": [{"HostIp": "172.31.0.1", "HostPort": "49153"}]},
            "Networks": {"ocu-sandbox": {"IPAddress": "172.31.0.10"}},
            "IPAddress": "172.31.0.10",
        }})
        self.assertEqual(get_container_service_address("chat-1", CDP_PORT), "172.31.0.1:49153")
        self.client.networks.get.assert_not_called()

    def test_missing_publication_is_unavailable_even_with_container_ip(self):
        self.client.containers.get.return_value = _container({"NetworkSettings": {
            "Ports": {},
            "Networks": {
                "ocu-sandbox": {"IPAddress": "172.31.0.10"},
                "compose_default": {"IPAddress": "172.18.0.5"},
            },
            "IPAddress": "172.18.0.5",
        }})
        self.assertIsNone(get_container_service_address("chat-1", CDP_PORT))
        self.assertIsNone(get_container_service_address("chat-1", TTYD_PORT))
        self.client.networks.get.assert_not_called()

    def test_returns_none_when_the_container_is_not_running(self):
        self.client.containers.get.return_value = _container(
            {"NetworkSettings": {"Ports": {}, "Networks": {}}}, status="exited"
        )
        self.assertIsNone(get_container_service_address("chat-1", CDP_PORT))

    def test_returns_none_when_the_container_is_absent(self):
        self.client.containers.get.side_effect = Exception("no such container")
        self.assertIsNone(get_container_service_address("chat-1", CDP_PORT))

    def test_published_port_works_without_any_network(self):
        """Rootless Podman: no compose network at all, only the published port."""
        self.client.containers.get.return_value = _container({"NetworkSettings": {
            "Ports": {"7681/tcp": [{"HostIp": "0.0.0.0", "HostPort": "45000"}]},
            "Networks": {},
            "IPAddress": "",
        }})
        self.assertEqual(get_container_service_address("chat-1", TTYD_PORT), "127.0.0.1:45000")


class UserDataDirectoryTests(unittest.TestCase):
    """Per-chat directories are created in-process, not by a root container.

    The old implementation ran a throwaway container as root to mkdir and chmod. Rootless Podman
    has no root to give, so that call failed and took container creation with it — the bind mount
    then pointed at a path that did not exist. The orchestrator mounts the same volume, so it can
    create them directly.
    """

    def test_directories_are_created_without_spawning_a_container(self):
        import tempfile
        import docker_manager as dm

        import docker as docker_sdk
        client = MagicMock()
        # A brand-new chat: no container exists yet, which is the path that prepares directories.
        client.containers.get.side_effect = docker_sdk.errors.NotFound("absent")
        with tempfile.TemporaryDirectory() as base:
            with patch.object(dm, "USER_DATA_BASE_PATH", base), \
                 patch.object(dm, "get_docker_client", return_value=client), \
                 patch.object(dm, "skill_manager", MagicMock(get_skill_mounts=lambda *a, **k: {})):
                try:
                    dm._get_or_create_container("chat-xyz")
                except Exception:
                    # Container creation itself is mocked out and may fail further down; the
                    # directories must exist regardless, and no root container may have been run.
                    pass

            chat_dir = os.path.join(base, "chat-xyz")
            self.assertTrue(os.path.isdir(os.path.join(chat_dir, "uploads")))
            self.assertTrue(os.path.isdir(os.path.join(chat_dir, "outputs")))

        # The root-container helper is the thing that cannot work rootless.
        for call in client.containers.run.call_args_list:
            self.assertNotEqual(call.kwargs.get("user"), "root",
                                "must not spawn a root container to prepare directories")


if __name__ == "__main__":
    unittest.main()
