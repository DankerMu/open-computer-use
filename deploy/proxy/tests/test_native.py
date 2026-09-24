# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Native nginx public-boundary checks against private recording HTTP/WS fixtures."""

from __future__ import annotations

import base64
import http.client
import json
import os
from pathlib import Path
import unittest

CHAT = "chat-ABC-123"
ORIGIN = "http://127.0.0.1:18780"
TOKEN = ("".join(chr(n) for n in range(0x21, 0x7F)) + "$http_host${x}@@LOCATIONS@@"
         if os.environ.get("OCU_TEST_TOKEN_DOMAIN") == "visible-ascii" else "synthetic-native-token")
MUTATION = {"X-Requested-With": "ocu-workspace", "Origin": ORIGIN}
RECORD = Path(os.environ.get("OCU_TEST_RECORD", "/tmp/ocu-proxy-native-record/requests.jsonl"))
PROXY_PORT = int(os.environ.get("OCU_TEST_PROXY_PORT", "18782"))


class NativeProxyTests(unittest.TestCase):
    def setUp(self):
        self.before = len(self.observations())

    def observations(self):
        if not RECORD.exists():
            return []
        return [json.loads(row) for row in RECORD.read_text().splitlines() if row]

    def since(self, kind):
        return [row for row in self.observations()[self.before:] if row["kind"] == kind]

    def request(self, path, method="GET", headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", PROXY_PORT, timeout=15)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = (response.status, response.getheaders(), response.read())
        connection.close()
        return result

    def owner(self, extra=None):
        return {"Cookie": "session=owner", **(extra or {})}

    def check_forward(self, path, upstream, method="GET", extra=None, body=None):
        status, _, _ = self.request(path, method, self.owner(extra), body)
        self.assertEqual(status, 200, path)
        seen = self.since("ocu")[-1]
        self.assertEqual((seen["method"], seen["target"]), (method, upstream), path)
        self.assertEqual(self.since("auth")[-1]["headers"].get("x-chat-id"), CHAT)
        self.assertEqual(seen["headers"].get("x-chat-id"), CHAT)
        return seen

    def test_canonical_rows_forward_only_allowed_methods_and_preserve_prefix(self):
        rows = (
            ("/ocu/api/outputs/{c}", "/api/outputs/{c}", "GET"),
            ("/ocu/api/uploads/{c}/manifest", "/api/uploads/{c}/manifest", "GET"),
            ("/ocu/api/uploads/{c}/list", "/api/uploads/{c}/list", "GET"),
            ("/ocu/files/{c}/archive", "/files/{c}/archive", "GET"),
            ("/ocu/files/{c}/sub/report.bin", "/files/{c}/sub/report.bin", "GET"),
            ("/ocu/preview/{c}", "/preview/{c}", "GET"),
            ("/ocu/browser/{c}/status", "/browser/{c}/status", "GET"),
            ("/ocu/browser/{c}/json/version", "/browser/{c}/json/version", "GET"),
            ("/ocu/browser/{c}/json", "/browser/{c}/json", "GET"),
            ("/ocu/terminal/{c}/status", "/terminal/{c}/status", "GET"),
            ("/ocu/terminal/{c}/heartbeat", "/terminal/{c}/heartbeat", "GET"),
            ("/ocu/terminal/{c}/sessions", "/terminal/{c}/sessions", "GET"),
            ("/ocu/terminal/{c}/processes", "/terminal/{c}/processes", "GET"),
            ("/ocu/api/uploads/{c}/folder/data.bin", "/api/uploads/{c}/folder/data.bin", "POST"),
            ("/ocu/terminal/{c}/start-ttyd", "/terminal/{c}/start-ttyd", "POST"),
            ("/ocu/terminal/{c}/stop-ttyd", "/terminal/{c}/stop-ttyd", "POST"),
            ("/ocu/terminal/{c}/restart-container", "/terminal/{c}/restart-container", "POST"),
            ("/ocu/terminal/{c}/resurrect-container", "/terminal/{c}/resurrect-container", "POST"),
            ("/ocu/terminal/{c}/processes/123/kill", "/terminal/{c}/processes/123/kill", "POST"),
        )
        for path, upstream, method in rows:
            with self.subTest(path=path):
                extra = MUTATION if path.split("/")[2] == "terminal" and path.endswith(("heartbeat", "sessions", "processes")) or method == "POST" else None
                self.check_forward(path.format(c=CHAT), upstream.format(c=CHAT), method, extra)
        status, _, _ = self.request("/ocu/static/deep/preview.js?rev=5", headers=self.owner())
        self.assertEqual(status, 200)
        self.assertEqual(self.since("ocu")[-1]["target"], "/ocu/static/deep/preview.js?rev=5")
        self.assertEqual(self.since("auth")[-1]["target"], "/api/v1/auths/")
        status, _, body = self.request("/ocu/static/deep/preview.js", "HEAD", self.owner())
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(self.since("ocu")[-1]["method"], "HEAD")
        status, _, body = self.request("/outside/ocu", headers=self.owner())
        self.assertEqual((status, body), (200, b"webui"))

    def test_literal_upload_filenames_dispatch_by_method(self):
        for filename in ("manifest", "list", "%6danifest", "%6cist"):
            with self.subTest(filename=filename):
                path = "/ocu/api/uploads/" + CHAT + "/" + filename
                self.check_forward(path, "/api/uploads/" + CHAT + "/" + filename,
                                   method="POST", extra=MUTATION, body=b"payload")
        self.check_forward("/ocu/api/uploads/" + CHAT + "/man%69fest",
                           "/api/uploads/" + CHAT + "/man%69fest")
        previous = len(self.since("ocu"))
        path = "/ocu/api/uploads/" + CHAT + "/manifest"
        status, _, _ = self.request(
            path, "POST", self.owner({**MUTATION, "Origin": "null"}), b"blocked")
        self.assertEqual(status, 403)
        self.assertEqual(len(self.since("ocu")), previous)
        status, _, _ = self.request(
            path, "POST", {"Cookie": "session=foreign", **MUTATION}, b"blocked")
        self.assertEqual(status, 404)
        self.assertEqual(len(self.since("ocu")), previous)

    def test_unknown_paths_methods_and_ambiguous_targets_never_contact_ocu(self):
        targets = (
            "/ocu", "/ocu/", "/ocu/health", "/ocu/docs", "/ocu/mcp",
            "/ocu/mcp-info", "/ocu/system-prompt", "/ocu/skill-list",
            "/ocu/skill-mounts", "/ocu/api/runtime/cli", "/ocu/internal/launch/" + CHAT,
            "/ocu/files/" + CHAT + "/a%2fb", "/ocu/files/" + CHAT + "/a%5cb",
            "/ocu/../api/v1/ocu/auth", "/ocu/./api/outputs/" + CHAT,
            "/ocu/files/" + CHAT + "/%2e%2e/secret", "/ocu/files/" + CHAT + "/a%252fb",
            "/ocu/files/" + CHAT + "/.%2e/secret", "/ocu/files/" + CHAT + "/%2e./secret",
            "/ocu/files/" + CHAT + "/a%252eb", "/ocu/files/" + CHAT + "/sub/../secret",
            "/ocu/files/" + CHAT + "//secret", "/ocu/files/" + CHAT + "/sub\\secret",
            "/ocu/files/%63hat-ABC-123/report",
            "/ocu/files/" + CHAT + "/../foreign/report.bin",
            "/ocu/files/" + CHAT + "/a%GG", "/ocu/terminal/" + CHAT + "/wrong",
            "/ocu/terminal/" + CHAT + "/ws",
            "/ocu/browser/" + CHAT + "/devtools/page/PAGE-1",
        )
        for path in targets:
            with self.subTest(path=path):
                previous = len(self.since("ocu"))
                status, _, _ = self.request(path, headers=self.owner())
                self.assertEqual(status, 400 if path.endswith("/a%GG") else 404, path)
                self.assertEqual(len(self.since("ocu")), previous, path)
        for method, target in (("DELETE", "/ocu/api/outputs/" + CHAT),
                               ("OPTIONS", "/ocu/static/preview.js"),
                               ("POST", "/ocu/files/" + CHAT + "/report.html")):
            with self.subTest(method=method):
                previous = len(self.since("ocu"))
                status, _, _ = self.request(target, method, self.owner(MUTATION))
                self.assertEqual(status, 404)
                self.assertEqual(len(self.since("ocu")), previous)

    def test_session_owner_identity_headers_and_status_provenance(self):
        target = "/ocu/api/outputs/" + CHAT
        previous = len(self.since("ocu"))
        status, _, _ = self.request(target)
        self.assertEqual(status, 401)
        self.assertEqual(len(self.since("ocu")), previous)
        status, _, _ = self.request(target, headers={"Cookie": "session=foreign"})
        self.assertEqual(status, 404)
        self.assertEqual(len(self.since("ocu")), previous)
        status, _, _ = self.request(target, headers={"Cookie": "session=error"})
        self.assertEqual(status, 500)
        self.assertEqual(len(self.since("ocu")), previous)
        static = "/ocu/static/deep/preview.js"
        status, _, _ = self.request(static)
        self.assertEqual(status, 401)
        self.assertEqual(len(self.since("ocu")), previous)
        status, _, _ = self.request(static, headers={"Cookie": "session=foreign"})
        self.assertEqual(status, 404)
        self.assertEqual(len(self.since("ocu")), previous)
        supplied = {"Authorization": "Bearer user-controlled", "X-User-Id": "forged-id",
                    "X-User-Email": "forged@example.test", "X-Chat-Id": "other-chat",
                    "X-OCU-Internal-Token": "forged-token", "X-Ocu-Grant": "forged-grant"}
        status, headers, body = self.request(target, headers=self.owner(supplied))
        self.assertEqual(status, 200)
        auth = self.since("auth")[-1]
        ocu = self.since("ocu")[-1]
        self.assertNotIn("authorization", auth["headers"])
        self.assertEqual(auth["headers"].get("x-chat-id"), CHAT)
        self.assertEqual(auth["headers"].get("cookie"), "session=owner")
        self.assertEqual(ocu["headers"].get("authorization"), "Bearer " + TOKEN)
        self.assertEqual(ocu["headers"].get("x-user-id"), "trusted-user")
        self.assertEqual(ocu["headers"].get("x-user-email"), "owner%2Bqa%40example.test")
        self.assertEqual(ocu["headers"].get("x-chat-id"), CHAT)
        self.assertEqual(ocu["headers"].get("cookie"), "session=owner")
        self.assertNotIn("x-ocu-internal-token", ocu["headers"])
        self.assertNotIn("x-ocu-grant", ocu["headers"])
        self.assertNotIn(TOKEN.encode(), body)
        self.assertNotIn(TOKEN, str(headers))
        status, _, _ = self.request("/ocu/static/deep/preview.js", headers=self.owner(supplied))
        self.assertEqual(status, 200)
        static = self.since("ocu")[-1]["headers"]
        self.assertNotIn("x-user-id", static)
        self.assertNotIn("x-user-email", static)
        self.assertNotIn("x-chat-id", static)
        self.assertEqual(static.get("authorization"), "Bearer " + TOKEN)
        for filename, expected in (("backend403", 403), ("backend409", 409)):
            status, _, _ = self.request("/ocu/files/" + CHAT + "/" + filename, headers=self.owner())
            self.assertEqual(status, expected)
            self.assertEqual(self.since("ocu")[-1]["target"], "/files/" + CHAT + "/" + filename)

    def test_mutating_get_and_post_require_exact_header_and_origin_or_fetch_site(self):
        for action in ("heartbeat", "sessions", "processes"):
            path = "/ocu/terminal/" + CHAT + "/" + action
            previous = len(self.since("ocu"))
            for extra in ({"Origin": "null", "X-Requested-With": "ocu-workspace", "Sec-Fetch-Site": "same-origin"},
                          {"Origin": ORIGIN},
                          {"Origin": "https://other.example", "X-Requested-With": "ocu-workspace"},
                          {"Origin": ORIGIN.upper(), "X-Requested-With": "ocu-workspace"}):
                status, _, _ = self.request(path, headers=self.owner(extra))
                self.assertEqual(status, 403, (action, extra))
                self.assertEqual(len(self.since("ocu")), previous)
            self.check_forward(path, "/terminal/" + CHAT + "/" + action, extra=MUTATION)
            self.check_forward(path, "/terminal/" + CHAT + "/" + action,
                               extra={"Sec-Fetch-Site": "same-origin", "X-Requested-With": "ocu-workspace"})
        upload = "/ocu/api/uploads/" + CHAT + "/large.bin"
        before = len(self.since("ocu"))
        status, _, _ = self.request(upload, "POST", self.owner({**MUTATION, "Origin": "null"}), b"bad")
        self.assertEqual(status, 403)
        self.assertEqual(len(self.since("ocu")), before)
        body = b"x" * (1024 * 1024 + 1)
        self.check_forward(upload, "/api/uploads/" + CHAT + "/large.bin", "POST", MUTATION, body)
        self.assertEqual(self.since("ocu")[-1]["headers"].get("content-length"), str(len(body)))

    def test_encoded_file_names_query_and_generated_file_headers(self):
        target = "/ocu/files/" + CHAT + "/sub/space%20hash%23plus%2Bpercent%25.html?revision=7"
        status, headers, body = self.request(target, headers=self.owner())
        self.assertEqual(status, 200)
        self.assertEqual(self.since("ocu")[-1]["target"], target[4:])
        self.assertEqual(body, b"file")
        self.assertEqual([value for name, value in headers if name.lower() == "content-security-policy"],
                         ["sandbox allow-scripts allow-forms"])
        self.assertEqual([value for name, value in headers if name.lower() == "x-content-type-options"], ["nosniff"])
        encoded_dot = "/ocu/files/" + CHAT + "/sub/report%2Ehtml?revision=8"
        status, headers, _ = self.request(encoded_dot, headers=self.owner())
        self.assertEqual(status, 200)
        self.assertEqual(self.since("ocu")[-1]["target"], encoded_dot[4:])
        self.assertEqual([value for key, value in headers if key.lower() == "content-security-policy"],
                         ["sandbox allow-scripts allow-forms"])
        for extension in ("svg", "xhtml", "xml"):
            with self.subTest(extension=extension):
                status, headers, _ = self.request("/ocu/files/" + CHAT + "/report." + extension, headers=self.owner())
                self.assertEqual(status, 200)
                self.assertEqual([value for name, value in headers if name.lower() == "content-security-policy"],
                                 ["sandbox allow-scripts allow-forms"])
                self.assertEqual([value for name, value in headers if name.lower() == "x-content-type-options"], ["nosniff"])
        for name, disposition in (("report.bin", "inline; filename=report.html"),
                                  ("report.html?download=1", "attachment; filename=report.html"),
                                  ("report.html?revision=7&download=1", "attachment; filename=report.html")):
            status, headers, _ = self.request("/ocu/files/" + CHAT + "/" + name, headers=self.owner())
            self.assertEqual(status, 200)
            self.assertEqual([value for key, value in headers if key.lower() == "content-security-policy"],
                             ["default-src 'self'"])
            self.assertEqual([value for key, value in headers if key.lower() == "x-content-type-options"], ["other"])
            self.assertEqual([value for key, value in headers if key.lower() == "content-disposition"],
                             [disposition])
        for query, disposition in (("download=1&download=0", "inline; filename=report.html"),
                                   ("download=0&download=1", "attachment; filename=report.html"),
                                   ("DOWNLOAD=1", "inline; filename=report.html"),
                                   ("download=1&%64ownload=0", "inline; filename=report.html"),
                                   ("download=1&download", "inline; filename=report.html")):
            status, headers, _ = self.request(
                "/ocu/files/" + CHAT + "/report.html?" + query, headers=self.owner())
            self.assertEqual(status, 200)
            self.assertEqual(self.since("ocu")[-1]["target"],
                             "/files/" + CHAT + "/report.html?" + query)
            self.assertEqual([value for key, value in headers if key.lower() == "content-disposition"],
                             [disposition])
            self.assertEqual([value for key, value in headers if key.lower() == "content-security-policy"],
                             ["sandbox allow-scripts allow-forms"])

        status, headers, body = self.request(
            "/ocu/files/" + CHAT + "/backend-html403.html", headers=self.owner())
        self.assertEqual((status, body), (403, b"upstream error"))
        self.assertEqual([value for key, value in headers if key.lower() == "content-security-policy"],
                         ["sandbox allow-scripts allow-forms"])
        self.assertEqual([value for key, value in headers if key.lower() == "x-content-type-options"],
                         ["nosniff"])

    def test_websocket_auth_and_cookie_forwarding_without_custom_header(self):
        for route in ("/ocu/terminal/" + CHAT + "/ws",
                      "/ocu/browser/" + CHAT + "/devtools/page/PAGE-1"):
            with self.subTest(route=route):
                headers = self.owner({"Connection": "Upgrade", "Upgrade": "websocket",
                                      "Origin": ORIGIN,
                                      "Sec-WebSocket-Key": base64.b64encode(b"websocket-test-key").decode(),
                                      "Sec-WebSocket-Version": "13"})
                connection = http.client.HTTPConnection("127.0.0.1", PROXY_PORT, timeout=15)
                connection.request("GET", route, headers=headers)
                response = connection.getresponse()
                self.assertEqual(response.status, 101)
                self.assertEqual(response.getheader("Upgrade"), "websocket")
                self.assertEqual(response.fp.read(4), b"\x81\x02ok")
                connection.close()
                seen = self.since("ocu")[-1]
                self.assertEqual(seen["headers"].get("cookie"), "session=owner")
                self.assertEqual(seen["headers"].get("authorization"), "Bearer " + TOKEN)
                self.assertEqual(seen["headers"].get("upgrade", "").lower(), "websocket")
                self.assertEqual(self.since("auth")[-1]["headers"].get("x-chat-id"), CHAT)
                previous = len(self.since("ocu"))
                for denied, expected in (({"Origin": "null"}, 401),
                                         ({"Origin": "https://foreign.test"}, 401),
                                         ({"Cookie": "session=foreign"}, 404)):
                    status, _, _ = self.request(
                        route, headers={**{key: value for key, value in headers.items()
                                           if key != "Cookie"}, **denied})
                    self.assertEqual(status, expected)
                    self.assertEqual(len(self.since("ocu")), previous)


if __name__ == "__main__":
    unittest.main()
