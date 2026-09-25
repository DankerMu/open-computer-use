# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Checker CLI: complete matrix is accepted; publication and topology mutants fail."""

from __future__ import annotations

from pathlib import Path
import unittest

from support import (
    OCU_SERVICE,
    PROXY_SERVICE,
    SANDBOX_NETWORK,
    WEBUI_SERVICE,
    intended_docs,
    proxy_mapping,
    run_checker,
    service,
    tmp_dir,
    write_docs,
)


class CheckPortsTests(unittest.TestCase):
    def check(self, docs, extra_env=None):
        with tmp_dir() as raw:
            directory = Path(raw)
            paths = write_docs(directory, docs)
            return run_checker(paths, extra_env=extra_env)

    def test_complete_intended_matrix_exits_zero_and_names_no_service(self):
        result = self.check(intended_docs())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")
        self.assertEqual(result.stderr.strip(), "")

    def test_webui_loopback_publication_is_rejected(self):
        docs = intended_docs()
        docs["webui.json"]["services"][WEBUI_SERVICE]["ports"] = [
            proxy_mapping(published="3000", target=8080, host_ip="127.0.0.1")
        ]
        result = self.check(docs)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(WEBUI_SERVICE, result.stderr)
        self.assertNotIn("DATABASE_URL", result.stderr)
        self.assertNotIn("MCP_API_KEY", result.stderr)

    def test_ocu_publication_is_rejected(self):
        docs = intended_docs()
        docs["core.json"]["services"][OCU_SERVICE]["ports"] = [
            proxy_mapping(published="8081", target=8081, host_ip="127.0.0.1")
        ]
        result = self.check(docs)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(OCU_SERVICE, result.stderr)

    def test_unrelated_service_loopback_publication_is_rejected(self):
        docs = intended_docs()
        docs["core.json"]["services"]["retention-guard"]["ports"] = [
            {"target": 9, "published": "9", "protocol": "tcp", "host_ip": "127.0.0.1"}
        ]
        result = self.check(docs)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("retention-guard", result.stderr)

    def test_expose_is_not_a_host_publication(self):
        docs = intended_docs()
        docs["webui.json"]["services"][WEBUI_SERVICE]["expose"] = ["8080", "8081"]
        result = self.check(docs)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_proxy_loopback_mapping_is_rejected(self):
        docs = intended_docs()
        docs["proxy.json"]["services"][PROXY_SERVICE]["ports"] = [
            proxy_mapping(host_ip="127.0.0.1")
        ]
        result = self.check(docs)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(PROXY_SERVICE, result.stderr)

    def test_proxy_wrong_target_is_rejected(self):
        docs = intended_docs()
        docs["proxy.json"]["services"][PROXY_SERVICE]["ports"] = [proxy_mapping(target=80)]
        result = self.check(docs)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(PROXY_SERVICE, result.stderr)

    def test_proxy_loopback_listen_is_rejected_despite_correct_publication(self):
        docs = intended_docs()
        docs["proxy.json"]["services"][PROXY_SERVICE]["environment"]["OCU_PROXY_LISTEN"] = "127.0.0.1:8082"
        result = self.check(docs)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(PROXY_SERVICE, result.stderr)

    def test_proxy_unresolvable_upstream_alias_is_rejected(self):
        docs = intended_docs()
        docs["proxy.json"]["services"][PROXY_SERVICE]["environment"]["OCU_PROXY_UPSTREAM"] = "http://localhost:8081"
        result = self.check(docs)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(PROXY_SERVICE, result.stderr)

    def test_proxy_udp_mapping_is_rejected(self):
        docs = intended_docs()
        docs["proxy.json"]["services"][PROXY_SERVICE]["ports"] = [proxy_mapping(protocol="udp")]
        result = self.check(docs)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(PROXY_SERVICE, result.stderr)

    def test_proxy_missing_publication_is_rejected(self):
        docs = intended_docs()
        docs["proxy.json"]["services"][PROXY_SERVICE].pop("ports", None)
        result = self.check(docs)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(PROXY_SERVICE, result.stderr)

    def test_proxy_second_mapping_is_rejected(self):
        docs = intended_docs()
        docs["proxy.json"]["services"][PROXY_SERVICE]["ports"] = [
            proxy_mapping(),
            proxy_mapping(published="8443", target=8443),
        ]
        result = self.check(docs)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(PROXY_SERVICE, result.stderr)

    def test_second_proxy_service_is_rejected(self):
        docs = intended_docs()
        docs["core.json"]["services"][PROXY_SERVICE] = service(
            networks={"default": {}},
            ports=[proxy_mapping()],
        )
        result = self.check(docs)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(PROXY_SERVICE, result.stderr)

    def test_host_networking_is_rejected(self):
        docs = intended_docs()
        docs["core.json"]["services"][OCU_SERVICE]["network_mode"] = "host"
        result = self.check(docs)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(OCU_SERVICE, result.stderr)
        self.assertIn("host", result.stderr)

    def test_shared_service_namespace_is_rejected(self):
        docs = intended_docs()
        docs["webui.json"]["services"][WEBUI_SERVICE]["network_mode"] = f"service:{OCU_SERVICE}"
        result = self.check(docs)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(WEBUI_SERVICE, result.stderr)

    def test_shared_container_namespace_is_rejected(self):
        docs = intended_docs()
        docs["core.json"]["services"]["retention-guard"]["network_mode"] = "container:foreign"
        result = self.check(docs)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("retention-guard", result.stderr)

    def test_unrelated_service_cannot_leave_control_plane_bridge(self):
        docs = intended_docs()
        docs["core.json"]["networks"]["other"] = {"name": "other-bridge"}
        docs["core.json"]["services"]["retention-guard"]["networks"] = {"other": {}}
        result = self.check(docs)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("retention-guard", result.stderr)

    def test_bridge_network_mode_without_service_networks_is_rejected(self):
        docs = intended_docs()
        docs["webui.json"]["services"][WEBUI_SERVICE].pop("networks", None)
        docs["webui.json"]["services"][WEBUI_SERVICE]["network_mode"] = "bridge"
        result = self.check(docs)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(WEBUI_SERVICE, result.stderr)
        self.assertIn("network mode", result.stderr)


    def test_sandbox_bridge_membership_is_rejected(self):
        docs = intended_docs()
        docs["core.json"]["networks"]["sandbox"] = {"name": SANDBOX_NETWORK}
        docs["core.json"]["services"][OCU_SERVICE]["networks"] = {
            "default": {},
            "sandbox": {},
        }
        result = self.check(docs)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(OCU_SERVICE, result.stderr)
        self.assertIn(SANDBOX_NETWORK, result.stderr)

    def test_proxy_must_share_control_plane_with_applications(self):
        docs = intended_docs()
        docs["proxy.json"]["networks"] = {
            "edge": {"name": "ocu-test-edge", "driver": "bridge"}
        }
        docs["proxy.json"]["services"][PROXY_SERVICE]["networks"] = {"edge": {}}
        result = self.check(docs)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(PROXY_SERVICE, result.stderr)
        self.assertIn("control-plane network", result.stderr)

    def test_duplicate_critical_service_across_docs_is_rejected(self):
        docs = intended_docs()
        docs["webui.json"]["services"][OCU_SERVICE] = service(networks={"default": {}})
        result = self.check(docs)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(OCU_SERVICE, result.stderr)

    def test_missing_proxy_service_is_rejected(self):
        docs = intended_docs()
        docs["proxy.json"]["services"].pop(PROXY_SERVICE)
        result = self.check(docs)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(PROXY_SERVICE, result.stderr)

    def test_missing_application_service_is_rejected(self):
        docs = intended_docs()
        docs["webui.json"]["services"].pop(WEBUI_SERVICE)
        result = self.check(docs)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(WEBUI_SERVICE, result.stderr)

    def test_missing_services_key_is_rejected(self):
        docs = intended_docs()
        docs["core.json"] = {"networks": docs["core.json"]["networks"]}
        result = self.check(docs)
        self.assertNotEqual(result.returncode, 0)

    def test_malformed_network_definition_is_rejected_without_environment_dump(self):
        docs = intended_docs()
        docs["core.json"]["networks"]["default"] = ["not-a-mapping"]
        docs["webui.json"]["services"][WEBUI_SERVICE]["environment"] = {
            "WEBUI_SECRET_KEY": "another-secret",
        }
        result = self.check(docs)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("malformed network definition", result.stderr)
        self.assertNotIn("another-secret", result.stderr)
        self.assertNotIn("WEBUI_SECRET_KEY", result.stderr)

    def test_malformed_json_is_rejected(self):
        with tmp_dir() as raw:
            directory = Path(raw)
            paths = write_docs(directory, intended_docs())
            paths[0].write_text("{not-json", encoding="utf-8")
            result = run_checker(paths)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotEqual(result.stderr.strip(), "")

    def test_missing_services_key_is_rejected(self):
        docs = intended_docs()
        docs["core.json"] = {"networks": docs["core.json"]["networks"]}
        result = self.check(docs)
        self.assertNotEqual(result.returncode, 0)


    def test_empty_argv_is_rejected(self):
        result = run_checker([])
        self.assertNotEqual(result.returncode, 0)

    def test_integer_published_port_is_accepted(self):
        docs = intended_docs()
        docs["proxy.json"]["services"][PROXY_SERVICE]["ports"] = [proxy_mapping(published=8082)]
        result = self.check(docs)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_diagnostics_do_not_print_environment_blocks(self):
        docs = intended_docs()
        docs["webui.json"]["services"][WEBUI_SERVICE]["environment"] = {
            "MCP_API_KEY": "super-secret-value",
            "WEBUI_SECRET_KEY": "another-secret",
        }
        docs["webui.json"]["services"][WEBUI_SERVICE]["ports"] = [
            proxy_mapping(published="3000", target=8080, host_ip="127.0.0.1")
        ]
        result = self.check(docs)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("super-secret-value", result.stderr)
        self.assertNotIn("another-secret", result.stderr)
        self.assertNotIn("WEBUI_SECRET_KEY", result.stderr)


if __name__ == "__main__":
    unittest.main()
