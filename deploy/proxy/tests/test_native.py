# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Native nginx public-boundary checks against private recording HTTP/WS fixtures."""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import socket
from pathlib import Path
import unittest

CHAT = "chat-ABC-123"
ORIGIN = "http://127.0.0.1:18780"
TOKEN = ("".join(chr(n) for n in range(0x21, 0x7F)) + "$http_host${x}@@LOCATIONS@@"
         if os.environ.get("OCU_TEST_TOKEN_DOMAIN") == "visible-ascii" else "synthetic-native-token")
MUTATION = {"X-Requested-With": "ocu-workspace", "Origin": ORIGIN}
RECORD = Path(os.environ.get("OCU_TEST_RECORD", "/tmp/ocu-proxy-native-record/requests.jsonl"))
PROXY_PORT = int(os.environ.get("OCU_TEST_PROXY_PORT", "18782"))
OFFICE_PROXY_PORT = int(os.environ.get("OCU_TEST_OFFICE_PROXY_PORT", "18783"))
GRANT = "synthetic-payload.synthetic-signature"
ERROR_LOG = RECORD.parent / "runtime" / "error.log"
OFFICE_ROWS = (
    ("documents/file-123/sessions", "POST"),
    ("sessions/session-123", "GET"),
    ("sessions/session-123/save", "POST"),
    ("sessions/session-123/close", "POST"),
    ("sessions/session-123/resolve", "POST"),
    ("documents/file-123/versions", "GET"),
    ("documents/file-123/restore", "POST"),
)


class NativeProxyTests(unittest.TestCase):
    def observations(self):
        if not RECORD.exists():
            return []
        return [json.loads(row) for row in RECORD.read_text().splitlines() if row]

    def snapshot(self):
        return len(self.observations())

    def since(self, kind, before):
        return [row for row in self.observations()[before:] if row["kind"] == kind]

    def request(self, path, method="GET", headers=None, body=None, *, port=PROXY_PORT):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.getheaders(), response.read()
        finally:
            connection.close()

    def owner(self, extra=None):
        return {"Cookie": "session=owner", **(extra or {})}

    def expect_auth(self, before, *, session=False, cookie="session=owner", chat=CHAT):
        auth = self.since("auth", before)
        self.assertEqual(len(auth), 1)
        seen = auth[0]
        if session:
            self.assertEqual(seen["target"], "/api/v1/auths/")
            self.assertTrue("x-chat-id" not in seen["headers"], "session auth must exclude chat identity")
        else:
            self.assertEqual(seen["target"], "/api/v1/ocu/auth")
            self.assertEqual(seen["headers"].get("x-chat-id"), chat)
        if cookie is None:
            self.assertTrue("cookie" not in seen["headers"], "anonymous auth must have no cookie")
        else:
            self.assertTrue(seen["headers"].get("cookie") == cookie, "auth must preserve caller cookie")
        for name in ("authorization", "x-api-key", "x-openwebui-key"):
            self.assertTrue(name not in seen["headers"], f"auth must exclude client {name}")
        self.assertEqual(seen.get("auth_frame"), "bodyless")
        self.assertIn(seen.get("auth_content_length"), {None, "0"})
        self.assertIsNone(seen.get("auth_transfer_encoding"))
        return seen

    def expect_resource_auth(self, before, endpoint, *, method="GET"):
        rows = self.since("auth", before)
        self.assertEqual(len(rows), 1)
        return self.assert_resource_auth(rows[0], endpoint, method=method)

    def expect_resource_attempts(self, before, endpoint, inputs):
        rows = self.since("auth", before)
        self.assertTrue(len(rows) > 0, "transport failure must contact resource authorization")
        for seen in rows:
            self.assert_resource_auth(seen, endpoint)
            for name, expected in inputs.items():
                self.assertTrue(seen["headers"].get(name, "") == expected,
                                f"resource authorization attempt input mismatch: {name}")

    def assert_resource_auth(self, seen, endpoint, *, method="GET"):
        self.assertTrue(seen["target"] == "/api/v1/ocu/resource-grants/" + endpoint,
                        "resource authorization endpoint mismatch")
        self.assertEqual(seen["method"], method)
        self.assertTrue(seen.get("auth_frame") == "bodyless", "resource auth must be bodyless")
        self.assertTrue(seen.get("auth_content_length") in {None, "0"},
                        "resource auth must not carry a body")
        self.assertTrue(seen.get("auth_transfer_encoding") is None,
                        "resource auth must not carry transfer framing")
        for name in ("authorization", "x-api-key", "x-openwebui-key",
                     "x-user-id", "x-user-email", "x-ocu-auth-mode",
                     "x-ocu-internal-token", "referer"):
            self.assertTrue(name not in seen["headers"], f"resource auth must exclude {name}")
        forbidden = (("cookie", "x-ocu-resource-query", "origin") if endpoint == "auth"
                     else ("x-ocu-grant", "x-ocu-resource-method"))
        for name in forbidden:
            self.assertTrue(name not in seen["headers"], f"resource auth must exclude {name}")
        return seen

    def expect_file_auth(self, before, *, method="GET"):
        rows = self.since("auth", before)
        self.assertEqual(len(rows), 2)
        self.assertTrue([row["target"] for row in rows] ==
                        ["/api/v1/ocu/resource-grants/issue",
                         "/api/v1/ocu/resource-grants/auth"],
                        "file must issue then verify")
        self.assertEqual(rows[0]["method"], method)
        self.assertEqual(rows[1]["method"], "GET")

    def policy(self, headers, *, cors=False):
        for name, expected in (("cache-control", ["no-store"]),
                               ("referrer-policy", ["no-referrer"]),
                               ("access-control-allow-origin", ["null"] if cors else []),
                               ("access-control-allow-credentials", []),
                               ("set-cookie", [])):
            values = [value for key, value in headers if key.lower() == name]
            self.assertTrue(values == expected, f"resource response policy mismatch: {name}")

    def follow_file(self, path, method="GET", headers=None):
        before = self.snapshot()
        status, response_headers, body = self.request(path, method, self.owner(headers))
        self.assertEqual(status, 302)
        self.assertTrue(body == b"", "canonical redirect must be empty")
        self.policy(response_headers)
        locations = [value for name, value in response_headers if name.lower() == "location"]
        expected = "/ocu/f/" + GRANT + "/" + path.removeprefix("/ocu/files/")
        self.assertTrue(locations == [expected], "canonical redirect target mismatch")
        self.assertEqual(len(self.since("ocu", before)), 0)
        self.expect_resource_auth(before, "issue", method=method)
        return self.request(locations[0], method, headers=headers)

    def wire_head(self, path, headers=None):
        lines = [f"HEAD {path} HTTP/1.1", f"Host: 127.0.0.1:{PROXY_PORT}",
                 "Connection: close"]
        lines += [f"{key}: {value}" for key, value in (headers or {}).items()]
        with socket.create_connection(("127.0.0.1", PROXY_PORT), timeout=15) as connection:
            connection.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
            with connection.makefile("rb") as stream:
                wire = stream.read()
        head, separator, body = wire.partition(b"\r\n\r\n")
        self.assertTrue(separator == b"\r\n\r\n", "HEAD response must have a header terminator")
        self.assertTrue(body == b"", "HEAD must transmit no body bytes")
        lines = head.split(b"\r\n")
        status = int(lines[0].split()[1])
        response_headers = []
        for line in lines[1:]:
            name, value = line.split(b":", 1)
            response_headers.append((name.decode("ascii"), value.strip().decode("latin-1")))
        return status, response_headers

    def check_forward(self, path, upstream, method="GET", extra=None, body=None, *, session=False):
        before = self.snapshot()
        extra = {**{name: "forged-grant-input" for name in (
            "X-Ocu-Grant", "X-Ocu-Auth-Mode", "X-Ocu-Resource-Path",
            "X-Ocu-Resource-Query", "X-Ocu-Resource-Method")}, **(extra or {})}
        file_entry = path.startswith("/ocu/files/") and path.split("?", 1)[0] != "/ocu/files/" + CHAT + "/archive"
        status, _, _ = (self.follow_file(path, method, extra) if file_entry
                        else self.request(path, method, self.owner(extra), body))
        self.assertEqual(status, 200, path)
        ocu = self.since("ocu", before)
        self.assertEqual(len(ocu), 1, path)
        seen = ocu[0]
        self.assertEqual((seen["method"], seen["target"]), (method, upstream), path)
        for name in ("x-ocu-grant", "x-ocu-auth-mode", "x-ocu-resource-path",
                     "x-ocu-resource-query", "x-ocu-resource-method"):
            self.assertTrue(name not in seen["headers"], f"OCU row must strip client {name}")
        if file_entry:
            self.expect_file_auth(before, method=method)
        else:
            self.expect_auth(before, session=session)
        if session:
            self.assertNotIn("x-chat-id", seen["headers"])
        else:
            self.assertEqual(seen["headers"].get("x-chat-id"), CHAT)
        if body is not None:
            self.assertEqual(seen.get("body_sha256"), hashlib.sha256(body).hexdigest())
            self.assertEqual(seen.get("body_length"), len(body))
        return seen

    def test_documentserver_request_preserves_target_and_withholds_credentials(self):
        path = "/web-apps/space%20name%2Fpart.js?x=1&x=%2B"
        forged = {name: "forged-private-value" for name in (
            "Authorization", "X-OCU-Internal-Token", "X-Chat-Id",
            "X-User-Id", "X-User-Email", "X-Forwarded-Host",
            "X-Forwarded-Proto", "X-Forwarded-For")}
        before = self.snapshot()
        body = b"documentserver-body"
        status, _, _ = self.request(
            path, "POST", self.owner({**forged, "Host": "editor.test:19443"}),
            body, port=OFFICE_PROXY_PORT)
        self.assertEqual(status, 200)
        self.expect_auth(before, session=True)
        self.assertEqual(self.since("ocu", before), [])
        rows = self.since("documentserver", before)
        self.assertEqual(len(rows), 1)
        seen = rows[0]
        self.assertEqual((seen["method"], seen["target"]), ("POST", path))
        self.assertEqual(seen["body_sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(seen["body_length"], len(body))
        headers = seen["headers"]
        for name in ("authorization", "cookie", "x-ocu-internal-token",
                     "x-chat-id", "x-user-id", "x-user-email"):
            self.assertNotIn(name, headers)
        self.assertEqual(headers.get("host"), "editor.test:19443")
        self.assertEqual(headers.get("x-forwarded-host"), "editor.test:19443")
        self.assertEqual(headers.get("x-forwarded-proto"), "http")
        self.assertEqual(headers.get("x-forwarded-for"), "127.0.0.1")

    def test_documentserver_denials_and_listener_separation(self):
        for upgrade in (False, True):
            for cookie, expected in ((None, 401), ("session=status-302", 500),
                                     ("session=status-403", 403), ("session=status-404", 500),
                                     ("session=status-500", 500), ("session=error", 500)):
                with self.subTest(upgrade=upgrade, cookie=cookie):
                    before = self.snapshot()
                    headers = {"Authorization": "Bearer forged", "X-Api-Key": "forged"}
                    if cookie:
                        headers["Cookie"] = cookie
                    if upgrade:
                        headers.update({"Upgrade": "websocket", "Connection": "Upgrade"})
                    status, _, _ = self.request("/doc/key/c", headers=headers, port=OFFICE_PROXY_PORT)
                    self.assertEqual(status, expected)
                    self.expect_auth(before, session=True, cookie=cookie)
                    self.assertEqual(self.since("documentserver", before), [])
                    self.assertEqual(self.since("ocu", before), [])
        before = self.snapshot()
        self.assertEqual(self.request("/_ocu_session_auth", headers=self.owner(),
                                      port=OFFICE_PROXY_PORT)[0], 404)
        self.assertEqual(self.observations()[before:], [])
        for path in (f"/ocu/api/outputs/{CHAT}",
                     f"/ocu/api/office/{CHAT}/sessions/session-123",
                     f"/office/callback/{CHAT}/session-123", "/api/v1/chats/", "/"):
            before = self.snapshot()
            self.assertEqual(self.request(path, headers=self.owner(),
                                          port=OFFICE_PROXY_PORT)[0], 200)
            self.expect_auth(before, session=True)
            self.assertEqual(self.since("ocu", before), [])
            self.assertEqual([row["target"] for row in self.since("documentserver", before)], [path])
        before = self.snapshot()
        self.assertEqual(self.request("/web-apps/apps/api/documents/api.js",
                                      headers=self.owner())[0], 200)
        self.assertEqual(self.since("documentserver", before), [])

    def test_documentserver_websocket_is_bidirectional_and_cookie_free(self):
        before = self.snapshot()
        key = base64.b64encode(b"office-native-12").decode()
        request = "\r\n".join((
            "GET /doc/key/c?transport=websocket HTTP/1.1", "Host: editor.test:19443",
            "Connection: Upgrade", "Upgrade: websocket", f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13", "Cookie: session=foreign",
            "Authorization: Bearer forged", "X-Chat-Id: forged", "", "",
        )).encode()
        with socket.create_connection(("127.0.0.1", OFFICE_PROXY_PORT), timeout=15) as connection:
            connection.sendall(request)
            with connection.makefile("rb") as stream:
                self.assertEqual(stream.readline().split()[1], b"101")
                headers = {}
                while (line := stream.readline()) != b"\r\n":
                    self.assertTrue(line)
                    name, value = line.split(b":", 1)
                    headers[name.lower()] = value.strip()
                accept = base64.b64encode(hashlib.sha1(
                    (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
                self.assertEqual(headers.get(b"sec-websocket-accept"), accept)
                connection.sendall(b"\x89\x82\x11\x22\x33\x44\x7e\x49")
                self.assertEqual(stream.read(4), b"\x8a\x02ok")
        self.expect_auth(before, session=True, cookie="session=foreign")
        self.assertEqual(self.since("ocu", before), [])
        rows = self.since("documentserver", before)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["target"], "/doc/key/c?transport=websocket")
        for name in ("cookie", "authorization", "x-chat-id"):
            self.assertNotIn(name, rows[0]["headers"])

    def test_office_rows_forward_with_path_chat_and_internal_auth(self):
        for suffix, method in OFFICE_ROWS:
            with self.subTest(suffix=suffix):
                upstream = f"/api/office/{CHAT}/{suffix}"
                seen = self.check_forward("/ocu" + upstream, upstream, method,
                                          MUTATION if method == "POST" else None)
                self.assertTrue(seen["headers"].get("authorization") == "Bearer " + TOKEN,
                                "Office must use the internal credential")

    def test_office_authorization_and_mutation_denials_never_reach_ocu(self):
        for suffix, method in OFFICE_ROWS:
            cases = [({}, 401), ({"Cookie": "session=foreign"}, 404)]
            for credentials, expected in cases:
                with self.subTest(suffix=suffix, status=expected):
                    before = self.snapshot()
                    headers = {**(MUTATION if method == "POST" else {}), **credentials}
                    status, _, _ = self.request(f"/ocu/api/office/{CHAT}/{suffix}", method, headers)
                    self.assertEqual(status, expected)
                    self.assertEqual(self.since("ocu", before), [])
            if method == "POST":
                for headers in ({"Origin": ORIGIN}, {**MUTATION, "Origin": "null"}):
                    with self.subTest(suffix=suffix, headers=headers):
                        before = self.snapshot()
                        status, _, _ = self.request(f"/ocu/api/office/{CHAT}/{suffix}",
                                                    method, self.owner(headers))
                        self.assertEqual(status, 403)
                        self.assertEqual(self.since("ocu", before), [])

    def test_office_unknown_methods_and_ambiguous_identifiers_never_reach_ocu(self):
        for suffix, allowed in OFFICE_ROWS:
            path = f"/ocu/api/office/{CHAT}/{suffix}"
            for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
                if method == allowed:
                    continue
                with self.subTest(path=path, method=method):
                    before = self.snapshot()
                    self.assertEqual(self.request(path, method, self.owner(MUTATION))[0], 404)
                    self.assertEqual(self.since("ocu", before), [])
            identifier = "file-123" if suffix.startswith("documents/") else "session-123"
            for invalid in ("", "a/b", "a%2fb", "a%5cb", ".", "..", "%2e",
                            "a%252fb", "a%255cb", "%252e%252e"):
                with self.subTest(suffix=suffix, identifier=invalid):
                    before = self.snapshot()
                    self.assertEqual(self.request(path.replace(identifier, invalid),
                                                  allowed, self.owner(MUTATION))[0], 404)
                    self.assertEqual(self.since("ocu", before), [])
        for suffix in ("", "documents/file-123", "sessions/session-123/unknown"):
            with self.subTest(unknown=suffix):
                before = self.snapshot()
                self.assertEqual(self.request(f"/ocu/api/office/{CHAT}/{suffix}",
                                              headers=self.owner())[0], 404)
                self.assertEqual(self.since("ocu", before), [])

    def test_office_control_plane_and_import_reads_are_not_public_routes(self):
        control = ("/office/source/ticket-123", f"/office/callback/{CHAT}/session-123")
        paths = [(path, method) for path in control
                 for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")]
        paths.append((f"/api/uploads/{CHAT}/imports", "GET"))
        for path, method in paths:
            for credentials in ({}, {"Cookie": "session=owner"}, {"Cookie": "session=foreign"}):
                for prefix in ("/ocu", ""):
                    with self.subTest(path=prefix + path, method=method, credentials=credentials):
                        before = self.snapshot()
                        status, _, _ = self.request(prefix + path, method, {**MUTATION, **credentials})
                        if prefix:
                            self.assertEqual(status, 404)
                        self.assertEqual(self.since("ocu", before), [])

    def test_canonical_rows_forward_only_allowed_methods_and_preserve_prefix(self):
        rows = (
            ("/ocu/api/outputs/{c}", "/api/outputs/{c}", "GET"),
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
            ("/ocu/api/uploads/{c}/folder/data.bin", "/api/uploads/{c}/folder/data.bin", "POST", b"folder-payload"),
            ("/ocu/terminal/{c}/start-ttyd", "/terminal/{c}/start-ttyd", "POST"),
            ("/ocu/terminal/{c}/stop-ttyd", "/terminal/{c}/stop-ttyd", "POST"),
            ("/ocu/terminal/{c}/restart-container", "/terminal/{c}/restart-container", "POST"),
            ("/ocu/terminal/{c}/resurrect-container", "/terminal/{c}/resurrect-container", "POST"),
            ("/ocu/terminal/{c}/processes/123/kill", "/terminal/{c}/processes/123/kill", "POST"),
        )
        for path, upstream, method, *payload in rows:
            with self.subTest(path=path):
                extra = MUTATION if path.split("/")[2] == "terminal" and path.endswith(("heartbeat", "sessions", "processes")) or method == "POST" else None
                self.check_forward(path.format(c=CHAT), upstream.format(c=CHAT), method, extra,
                                   payload[0] if payload else None)
        self.check_forward("/ocu/static/deep/preview.js?rev=5",
                           "/ocu/static/deep/preview.js?rev=5", session=True)
        self.check_forward("/ocu/static/deep/preview.js",
                           "/ocu/static/deep/preview.js", "HEAD", session=True)
        before = self.snapshot()
        status, _, body = self.request("/outside/ocu", headers=self.owner())
        self.assertEqual((status, body), (200, b"webui"))
        self.assertEqual(self.since("ocu", before), [])
        self.assertEqual(self.since("auth", before)[-1]["target"], "/outside/ocu")

    def test_literal_upload_filenames_dispatch_by_method(self):
        payloads = {"manifest": b"manifest-bytes", "list": b"list-bytes",
                    "%6danifest": b"enc-manifest", "%6cist": b"enc-list"}
        for filename, payload in payloads.items():
            with self.subTest(filename=filename):
                path = "/ocu/api/uploads/" + CHAT + "/" + filename
                self.check_forward(path, "/api/uploads/" + CHAT + "/" + filename,
                                   method="POST", extra=MUTATION, body=payload)
        path = "/ocu/api/uploads/" + CHAT + "/manifest"
        before = self.snapshot()
        status, _, _ = self.request(
            path, "POST", self.owner({**MUTATION, "Origin": "null"}), b"blocked")
        self.assertEqual(status, 403)
        self.assertEqual(self.since("ocu", before), [])
        before = self.snapshot()
        status, _, _ = self.request(
            path, "POST", {"Cookie": "session=foreign", **MUTATION}, b"blocked")
        self.assertEqual(status, 404)
        self.assertEqual(self.since("ocu", before), [])
        self.expect_auth(before, cookie="session=foreign")
        large = b"x" * (1024 * 1024 + 1)
        self.check_forward("/ocu/api/uploads/" + CHAT + "/large.bin",
                           "/api/uploads/" + CHAT + "/large.bin", "POST", MUTATION, large)
        before = self.snapshot()
        status, _, _ = self.request("/ocu/api/uploads/" + CHAT + "/large.bin", "POST",
                                    self.owner(MUTATION), bytes((byte ^ 1) for byte in large[:64]) + large[64:])
        self.assertEqual(status, 200)
        self.expect_auth(before)
        ocu = self.since("ocu", before)
        self.assertEqual(len(ocu), 1)
        seen = ocu[0]
        self.assertEqual((seen["method"], seen["target"]),
                         ("POST", "/api/uploads/" + CHAT + "/large.bin"))
        self.assertNotEqual(seen.get("body_sha256"), hashlib.sha256(large).hexdigest())
        self.assertEqual(seen.get("body_length"), len(large))
        before = self.snapshot()
        status, _, _ = self.request("/ocu/api/outputs/" + CHAT, headers=self.owner())
        self.assertEqual(status, 200)
        self.expect_auth(before)
        ocu = self.since("ocu", before)
        self.assertEqual(len(ocu), 1)
        self.assertEqual(ocu[0]["target"], "/api/outputs/" + CHAT)

    def test_retired_upload_reads_never_contact_ocu(self):
        spellings = ("manifest", "list", "%6danifest", "%6cist", "man%69fest")
        callers = (
            ("owner", self.owner()),
            ("foreign", {"Cookie": "session=foreign"}),
            ("anonymous", None),
        )
        for filename in spellings:
            path = "/ocu/api/uploads/" + CHAT + "/" + filename
            for caller, headers in callers:
                with self.subTest(path=path, caller=caller):
                    before = self.snapshot()
                    status, _, _ = self.request(path, headers=headers)
                    self.assertEqual(status, 404, path)
                    self.assertEqual(self.since("ocu", before), [], path)

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
                before = self.snapshot()
                status, _, _ = self.request(path, headers=self.owner())
                self.assertEqual(status, 400 if path.endswith("/a%GG") else 404, path)
                self.assertEqual(self.since("ocu", before), [], path)
        for method, target in (("DELETE", "/ocu/api/outputs/" + CHAT),
                               ("OPTIONS", "/ocu/static/preview.js"),
                               ("POST", "/ocu/files/" + CHAT + "/report.html")):
            with self.subTest(method=method):
                before = self.snapshot()
                status, _, _ = self.request(target, method, self.owner(MUTATION))
                self.assertEqual(status, 404)
                self.assertEqual(self.since("ocu", before), [])

    def test_session_owner_identity_headers_and_status_provenance(self):
        target = "/ocu/api/outputs/" + CHAT
        before = self.snapshot()
        status, _, _ = self.request(target)
        self.assertEqual(status, 401)
        self.assertEqual(self.since("ocu", before), [])
        before = self.snapshot()
        status, _, _ = self.request(target, headers={"Cookie": "session=foreign"})
        self.assertEqual(status, 404)
        self.assertEqual(self.since("ocu", before), [])
        self.expect_auth(before, cookie="session=foreign")
        before = self.snapshot()
        status, _, _ = self.request(target, headers={"Cookie": "session=error"})
        self.assertEqual(status, 500)
        self.assertEqual(self.since("ocu", before), [])
        static = "/ocu/static/deep/preview.js"
        before = self.snapshot()
        status, _, _ = self.request(static)
        self.assertEqual(status, 401)
        self.assertEqual(self.since("ocu", before), [])
        before = self.snapshot()
        status, _, _ = self.request(static, headers={"Cookie": "session=foreign"})
        self.assertEqual(status, 200)
        self.expect_auth(before, session=True, cookie="session=foreign")
        self.assertEqual(self.since("ocu", before)[-1]["target"], "/ocu/static/deep/preview.js")
        supplied = {"Authorization": "Bearer user-controlled", "X-User-Id": "forged-id",
                    "X-User-Email": "forged@example.test", "X-Chat-Id": "other-chat",
                    "X-OCU-Internal-Token": "forged-token", "X-Ocu-Grant": "forged-grant",
                    "X-Api-Key": "jwt-without-cookie", "X-OpenWebUI-Key": "custom-jwt"}
        before = self.snapshot()
        status, headers, body = self.request(target, headers=self.owner(supplied))
        self.assertEqual(status, 200)
        auth = self.expect_auth(before)
        ocu = self.since("ocu", before)[-1]
        self.assertTrue(ocu["headers"].get("authorization") == "Bearer " + TOKEN,
                        "chat route must use the internal credential")
        self.assertEqual(ocu["headers"].get("x-user-id"), "trusted-user")
        self.assertEqual(ocu["headers"].get("x-user-email"), "owner%2Bqa%40example.test")
        self.assertEqual(ocu["headers"].get("x-chat-id"), CHAT)
        self.assertEqual(ocu["headers"].get("cookie"), "session=owner")
        for name in ("x-ocu-internal-token", "x-ocu-grant"):
            self.assertTrue(name not in ocu["headers"], f"chat route must strip client {name}")
        self.assertTrue(TOKEN.encode() not in body and TOKEN not in str(headers),
                        "chat response must not expose the internal credential")
        before = self.snapshot()
        status, _, _ = self.request(static, headers=self.owner(supplied))
        self.assertEqual(status, 200)
        self.expect_auth(before, session=True)
        static_headers = self.since("ocu", before)[-1]["headers"]
        for name in ("x-user-id", "x-user-email", "x-chat-id"):
            self.assertTrue(name not in static_headers, f"static route must strip client {name}")
        self.assertTrue(static_headers.get("authorization") == "Bearer " + TOKEN,
                        "static route must use the internal credential")
        for filename, expected in (("backend403", 403), ("backend409", 409)):
            before = self.snapshot()
            status, _, _ = self.follow_file("/ocu/files/" + CHAT + "/" + filename)
            self.assertEqual(status, expected)
            self.expect_file_auth(before)
            self.assertEqual(self.since("ocu", before)[-1]["target"], "/files/" + CHAT + "/" + filename)
        for header in ("X-Api-Key", "X-OpenWebUI-Key"):
            with self.subTest(header=header):
                before = self.snapshot()
                status, _, _ = self.request(target, headers={header: "owner-jwt"})
                self.assertEqual(status, 401)
                self.assertEqual(self.since("ocu", before), [])
                self.expect_auth(before, cookie=None)
                before = self.snapshot()
                status, _, _ = self.request(static, headers={header: "owner-jwt"})
                self.assertEqual(status, 401)
                self.assertEqual(self.since("ocu", before), [])
                self.expect_auth(before, session=True, cookie=None)

    def test_mutating_get_and_post_require_exact_header_and_origin_or_fetch_site(self):
        for action in ("heartbeat", "sessions", "processes"):
            path = "/ocu/terminal/" + CHAT + "/" + action
            for extra in ({"Origin": "null", "X-Requested-With": "ocu-workspace", "Sec-Fetch-Site": "same-origin"},
                          {"Origin": ORIGIN},
                          {"Origin": "https://other.example", "X-Requested-With": "ocu-workspace"},
                          {"Origin": ORIGIN.upper(), "X-Requested-With": "ocu-workspace"}):
                before = self.snapshot()
                status, _, _ = self.request(path, headers=self.owner(extra))
                self.assertEqual(status, 403, (action, extra))
                self.assertEqual(self.since("ocu", before), [])
            self.check_forward(path, "/terminal/" + CHAT + "/" + action, extra=MUTATION)
            self.check_forward(path, "/terminal/" + CHAT + "/" + action,
                               extra={"Sec-Fetch-Site": "same-origin", "X-Requested-With": "ocu-workspace"})
        upload = "/ocu/api/uploads/" + CHAT + "/large.bin"
        before = self.snapshot()
        status, _, _ = self.request(upload, "POST", self.owner({**MUTATION, "Origin": "null"}), b"bad")
        self.assertEqual(status, 403)
        self.assertEqual(self.since("ocu", before), [])

    def test_canonical_file_entry_authorizes_before_issuer_redirect(self):
        path = ("/ocu/files/chat-ABC-123/reports/space%20%22%27%26%3C%3E.html"
                "?revision=7&tag=%2B&tag=&x=%252F")
        location = ("/ocu/f/synthetic-payload.synthetic-signature/chat-ABC-123/"
                    "reports/space%20%22%27%26%3C%3E.html"
                    "?revision=7&tag=%2B&tag=&x=%252F")
        forged = {
            "Authorization": "Bearer forged-private-value",
            "X-Api-Key": "forged-private-value",
            "X-OpenWebUI-Key": "forged-private-value",
            "X-User-Id": "forged-user",
            "X-User-Email": "forged@example.test",
            "X-OCU-Internal-Token": "forged-private-value",
            "X-Ocu-Auth-Mode": "grant",
            "X-Ocu-Grant": "forged-payload.forged-signature",
            "X-Chat-Id": "other-chat",
            "X-Ocu-Resource-Path": "other/file.html",
            "X-Ocu-Resource-Query": "other=1",
            "X-Ocu-Resource-Method": "POST",
        }
        for caller, cookie, expected in (("owner", "session=owner", 302),
                                         ("anonymous", None, 401),
                                         ("foreign", "session=foreign", 404)):
            with self.subTest(caller=caller):
                before = self.snapshot()
                headers = dict(forged)
                if cookie:
                    headers["Cookie"] = cookie
                status, response_headers, body = self.request(path, headers=headers)
                self.assertEqual(status, expected)
                self.assertEqual(len(self.since("ocu", before)), 0)
                locations = [value for name, value in response_headers
                             if name.lower() == "location"]
                if caller == "owner":
                    self.assertTrue(body == b"", "issuer redirect must have an empty body")
                    self.assertTrue(locations == [location],
                                    "issuer redirect must preserve the relative encoded target")
                    for name, value in (("cache-control", "no-store"),
                                        ("referrer-policy", "no-referrer")):
                        values = [item for key, item in response_headers
                                  if key.lower() == name]
                        self.assertTrue(values == [value], f"issuer redirect must set {name}")
                else:
                    self.assertTrue(locations == [], "denied entry must not redirect")
                auth = self.since("auth", before)
                self.assertEqual(len(auth), 1)
                seen = auth[0]
                self.assertTrue(seen["target"] == "/api/v1/ocu/resource-grants/issue",
                                "canonical entry must use the issuer content endpoint")
                self.assertEqual(seen["method"], "GET")
                inputs = seen["headers"]
                self.assertTrue(inputs.get("x-chat-id") == "chat-ABC-123",
                                "issuer chat must come from the route")
                self.assertTrue(inputs.get("x-ocu-resource-path") ==
                                "reports/space%20%22%27%26%3C%3E.html",
                                "issuer path must preserve the encoded route")
                self.assertTrue(inputs.get("x-ocu-resource-query") ==
                                "revision=7&tag=%2B&tag=&x=%252F",
                                "issuer query must preserve the original bytes")
                if cookie is None:
                    self.assertTrue("cookie" not in inputs, "anonymous issuer must have no cookie")
                else:
                    self.assertTrue(inputs.get("cookie") == cookie,
                                    "issuer must receive the caller session cookie")
                for name in ("authorization", "x-api-key", "x-openwebui-key",
                             "x-user-id", "x-user-email", "x-ocu-internal-token",
                             "x-ocu-auth-mode", "x-ocu-grant", "x-ocu-resource-method"):
                    self.assertTrue(name not in inputs, f"issuer must not receive client {name}")
                self.assertTrue(seen.get("auth_frame") == "bodyless",
                                "issuer content request must be bodyless")
                self.assertTrue(seen.get("auth_content_length") in {None, "0"},
                                "issuer must not receive a request body")
                self.assertTrue(seen.get("auth_transfer_encoding") is None,
                                "issuer must not receive transfer framing")

    def test_resource_head_preserves_method_metadata_and_empty_wire_body(self):
        canonical = "/ocu/files/" + CHAT + "/reports/space%20name.html?x=%2B&x="
        grant = "/ocu/f/" + GRANT + "/" + CHAT + "/reports/space%20name.html?x=%2B&x="
        before = self.snapshot()
        status, headers = self.wire_head(canonical, self.owner())
        self.assertEqual(status, 302)
        self.policy(headers)
        self.assertTrue([value for name, value in headers if name.lower() == "location"] == [grant],
                        "HEAD issuer must preserve its relative target")
        self.assertEqual(len(self.since("ocu", before)), 0)
        issuer = self.expect_resource_auth(before, "issue", method="HEAD")
        self.assertTrue(issuer["headers"].get("x-ocu-resource-path") ==
                        "reports/space%20name.html", "HEAD issuer path mismatch")
        before = self.snapshot()
        status, head_headers = self.wire_head(grant, {"Origin": "null"})
        self.assertEqual(status, 200)
        self.policy(head_headers, cors=True)
        verifier = self.expect_resource_auth(before, "auth")
        self.assertTrue(verifier["headers"].get("x-ocu-resource-method") == "HEAD",
                        "verifier must receive the original HEAD method")
        ocu = self.since("ocu", before)
        self.assertEqual(len(ocu), 1)
        self.assertEqual(ocu[0]["method"], "HEAD")
        self.assertTrue(ocu[0]["target"] ==
                        "/files/chat-ABC-123/reports/space%20name.html?x=%2B&x=",
                        "HEAD file target mismatch")
        status, get_headers, body = self.request(grant, headers={"Origin": "null"})
        self.assertEqual(status, 200)
        self.assertEqual(body, b"file")
        for name in ("content-type", "content-length", "content-disposition",
                     "content-security-policy", "x-content-type-options"):
            get_values = [value for key, value in get_headers if key.lower() == name]
            head_values = [value for key, value in head_headers if key.lower() == name]
            self.assertTrue(head_values == get_values, f"HEAD metadata differs for {name}")
        before = self.snapshot()
        status, _ = self.wire_head("/ocu/files/" + CHAT + "/report.html?download=1", self.owner())
        self.assertEqual(status, 200)
        self.expect_auth(before)
        self.assertEqual(self.since("ocu", before)[0]["method"], "HEAD")
        before = self.snapshot()
        status, _ = self.wire_head("/ocu/files/" + CHAT + "/archive", self.owner())
        self.assertEqual(status, 404)
        self.assertEqual(len(self.since("auth", before)), 0)
        self.assertEqual(len(self.since("ocu", before)), 0)

    def test_grant_uses_route_authority_and_withholds_browser_credentials(self):
        target = "/ocu/f/" + GRANT + "/" + CHAT + "/sub/space%20hash%23plus%2Bpercent%25.html?x=%2B&x="
        forged = {
            "Cookie": "session=foreign", "Authorization": "Bearer forged-private-value",
            "X-User-Id": "forged", "X-User-Email": "forged@example.test",
            "X-Chat-Id": "other-chat", "X-Ocu-Grant": "wrong-payload.wrong-signature",
            "X-Ocu-Auth-Mode": "session", "X-OCU-Internal-Token": "forged",
            "X-Ocu-Resource-Path": "other.bin", "X-Ocu-Resource-Query": "wrong=1",
            "X-Ocu-Resource-Method": "POST", "Referer": target,
            "Origin": "null", "Accept": "text/html", "Range": "bytes=0-3",
        }
        before = self.snapshot()
        status, headers, body = self.request(target, headers=forged)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"file")
        self.policy(headers, cors=True)
        seen = self.expect_resource_auth(before, "auth")
        inputs = seen["headers"]
        for name, expected in (("x-ocu-grant", GRANT), ("x-chat-id", CHAT),
                               ("x-ocu-resource-path", "sub/space%20hash%23plus%2Bpercent%25.html"),
                               ("x-ocu-resource-method", "GET")):
            self.assertTrue(inputs.get(name) == expected, f"verifier route input mismatch: {name}")
        for name in ("cookie", "x-ocu-resource-query", "origin"):
            self.assertTrue(name not in inputs, f"verifier must not receive {name}")
        ocu = self.since("ocu", before)
        self.assertEqual(len(ocu), 1)
        self.assertTrue(ocu[0]["target"] ==
                        "/files/chat-ABC-123/sub/space%20hash%23plus%2Bpercent%25.html?x=%2B&x=",
                        "grant must forward only the ordinary raw target")
        inputs = ocu[0]["headers"]
        for name, expected in (("authorization", "Bearer " + TOKEN), ("x-chat-id", CHAT),
                               ("x-user-id", "grant-user-current"),
                               ("x-user-email", "grant%2Bcurrent%40example.test"),
                               ("accept", "text/html"), ("range", "bytes=0-3")):
            self.assertTrue(inputs.get(name) == expected, f"file trusted input mismatch: {name}")
        for name in ("cookie", "referer", "x-ocu-grant", "x-ocu-auth-mode",
                     "x-ocu-internal-token", "x-ocu-resource-path",
                     "x-ocu-resource-query", "x-ocu-resource-method", "origin"):
            self.assertTrue(name not in inputs, f"grant file must exclude {name}")
        self.assertTrue(TOKEN.encode() not in body and TOKEN not in str(headers),
                        "file response must not expose the internal credential")
        for path in ("manifest", "list", "imports", "office/source/ticket.bin",
                     "control/session.bin", "sub/archive"):
            with self.subTest(alias=path):
                before = self.snapshot()
                status, _, _ = self.request("/ocu/f/" + GRANT + "/" + CHAT + "/" + path)
                self.assertEqual(status, 200)
                self.assertEqual(len(self.since("auth", before)), 1)
                self.assertEqual(len(self.since("ocu", before)), 1)

    def test_grant_route_guards_deny_before_verifier_or_file(self):
        prefix = "/ocu/f/" + GRANT + "/" + CHAT + "/"
        invalid_paths = ("", "sub/", "archive", "%61rchive", "arch%69ve", ".", "..",
                         "%2e", "%2e%2e", "sub/../secret", "sub/./secret", "sub//file",
                         "a%2fb", "a%5cb", "a%252fb", "a%255cb", "a%252eb",
                         "sub\\file", "a%00b", "a%0ab", "a%1fb", "a%7fb")
        targets = [prefix + path for path in invalid_paths]
        targets += [
            "/ocu/f/" + GRANT + "/%63hat-ABC-123/report.html",
            "/ocu/f/synthetic%2Dpayload.synthetic-signature/" + CHAT + "/report.html",
            "/ocu/f/one-component/" + CHAT + "/report.html",
            "/ocu/f/a.b.c/" + CHAT + "/report.html",
            "/ocu/f/a%2fb.c/" + CHAT + "/report.html",
            "/ocu/f/" + GRANT + "/" + CHAT,
            "/ocu/%66/" + GRANT + "/" + CHAT + "/a%2fb",
            "/ocu/%66/" + GRANT + "/" + CHAT + "/a%00b",
        ]
        for index, target in enumerate(targets):
            with self.subTest(case=index):
                before = self.snapshot()
                status, headers, _ = self.request(target, headers=self.owner({"Origin": "null"}))
                self.assertEqual(status, 400 if "%00" in target else 404)
                self.policy(headers)
                self.assertEqual(len(self.since("auth", before)), 0)
                self.assertEqual(len(self.since("ocu", before)), 0)
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                before = self.snapshot()
                status, headers, _ = self.request(prefix + "report.html", method,
                                                  self.owner({"Origin": "null"}))
                self.assertEqual(status, 404)
                self.policy(headers)
                self.assertEqual(len(self.since("auth", before)), 0)
                self.assertEqual(len(self.since("ocu", before)), 0)
        for method in ("GET", "HEAD"):
            for query in ("download=1", "revision=7&download=1", "download=1&x="):
                with self.subTest(method=method, query=query):
                    before = self.snapshot()
                    status, headers, _ = self.request(prefix + "report.html?" + query, method)
                    self.assertEqual(status, 404)
                    self.policy(headers)
                    self.assertEqual(len(self.since("auth", before)), 0)
                    self.assertEqual(len(self.since("ocu", before)), 0)
        for endpoint in ("/_ocu_grant_auth", "/_ocu_chat_auth", "/_ocu_session_auth"):
            before = self.snapshot()
            self.assertEqual(self.request(endpoint)[0], 404)
            self.assertEqual(len(self.since("auth", before)), 0)
            self.assertEqual(len(self.since("ocu", before)), 0)
        before = self.snapshot()
        self.assertEqual(self.request("/_ocu_session_auth", port=OFFICE_PROXY_PORT)[0], 404)
        self.assertEqual(len(self.since("auth", before)), 0)
        self.assertEqual(len(self.since("documentserver", before)), 0)

    def test_resource_denials_keep_status_provenance_and_never_fallback(self):
        canonical = "/ocu/files/" + CHAT + "/report.html"
        for index, (cookie, expected) in enumerate(
            ((None, 401), ("session=foreign", 404), ("session=error", 503),
             ("session=drop", 502), ("session=malformed", 502))):
            with self.subTest(entry_case=index):
                before = self.snapshot()
                status, headers, _ = self.request(canonical, headers={"Cookie": cookie} if cookie else {})
                self.assertEqual(status, expected)
                self.assertTrue(not any(name.lower() == "location" for name, _ in headers),
                                "failed issuance must not redirect")
                self.policy(headers)
                if cookie in {"session=drop", "session=malformed"}:
                    self.expect_resource_attempts(before, "issue", {
                        "cookie": cookie, "x-chat-id": CHAT,
                        "x-ocu-resource-path": "report.html", "x-ocu-resource-query": "",
                    })
                else:
                    self.expect_resource_auth(before, "issue")
                self.assertEqual(len(self.since("ocu", before)), 0)
        cases = (("missing-payload.synthetic-signature", CHAT, 401),
                 ("expired-payload.synthetic-signature", CHAT, 404),
                 ("revoked-payload.synthetic-signature", CHAT, 404),
                 ("ineligible-payload.synthetic-signature", CHAT, 404),
                 ("disabled-payload.synthetic-signature", CHAT, 404),
                 ("tampered-payload.synthetic-signature", CHAT, 404),
                 (GRANT, "other-chat", 404), ("error-payload.synthetic-signature", CHAT, 500),
                 ("drop-payload.synthetic-signature", CHAT, 500),
                 ("malformed-payload.synthetic-signature", CHAT, 500))
        for index, (grant, chat, expected) in enumerate(cases):
            with self.subTest(grant_case=index):
                before = self.snapshot()
                status, headers, _ = self.request("/ocu/f/" + grant + "/" + chat + "/report.html",
                                                  headers=self.owner({"Origin": "null"}))
                self.assertEqual(status, expected)
                self.policy(headers)
                if grant in {"drop-payload.synthetic-signature",
                             "malformed-payload.synthetic-signature"}:
                    self.expect_resource_attempts(before, "auth", {
                        "x-ocu-grant": grant, "x-chat-id": chat,
                        "x-ocu-resource-path": "report.html", "x-ocu-resource-method": "GET",
                    })
                else:
                    self.expect_resource_auth(before, "auth")
                self.assertEqual(len(self.since("ocu", before)), 0)
        for filename, expected in (("backend403", 403), ("backend409", 409),
                                   ("backend-html403.html", 403)):
            before = self.snapshot()
            status, headers, _ = self.request("/ocu/f/" + GRANT + "/" + CHAT + "/" + filename,
                                              headers={"Origin": "null"})
            self.assertEqual(status, expected)
            self.policy(headers, cors=True)
            self.expect_resource_auth(before, "auth")
            self.assertEqual(len(self.since("ocu", before)), 1)

    def test_grant_cors_is_exact_null_and_preserves_upstream_vary(self):
        for origin in (None, "null", "Null", "NULL", ORIGIN, "https://other.example"):
            for filename in ("report.bin", "vary-origin.bin"):
                with self.subTest(origin=origin, file=filename):
                    headers = {"Origin": origin} if origin else {}
                    status, response_headers, _ = self.request(
                        "/ocu/f/" + GRANT + "/" + CHAT + "/" + filename, headers=headers)
                    self.assertEqual(status, 200)
                    self.policy(response_headers, cors=origin == "null")
                    vary = [value for name, value in response_headers if name.lower() == "vary"]
                    expected = ("Accept-Encoding, Origin"
                                if filename == "vary-origin.bin" or origin == "null"
                                else "Accept-Encoding")
                    self.assertTrue(vary == [expected], "grant must preserve Vary without duplicate Origin")

    def test_grant_failure_logs_exclude_capability_and_credentials_with_positive_control(self):
        self.assertTrue(ERROR_LOG.is_file(), "native private error log must be available")
        before_log = ERROR_LOG.stat().st_size
        for grant, filename, expected in (
            ("drop-payload.synthetic-signature", "report.html", 500),
            ("malformed-payload.synthetic-signature", "report.html", 500),
            (GRANT, "drop.bin", 502), (GRANT, "malformed.bin", 502)):
            before = self.snapshot()
            target = "/ocu/f/" + grant + "/" + CHAT + "/" + filename
            status, headers, body = self.request(
                target, headers=self.owner({"Authorization": "Bearer private-log-canary",
                                             "Referer": target, "Origin": "null"}))
            self.assertEqual(status, expected)
            self.policy(headers, cors=grant == GRANT)
            if grant == GRANT:
                self.expect_resource_auth(before, "auth")
                attempts = self.since("ocu", before)
                self.assertTrue(len(attempts) > 0, "file transport failure must contact OCU")
                for seen in attempts:
                    self.assertEqual(seen["method"], "GET")
                    self.assertTrue(seen["target"] == "/files/" + CHAT + "/" + filename,
                                    "every failed file attempt must use the ordinary target")
                    for name, expected in (("authorization", "Bearer " + TOKEN),
                                           ("x-chat-id", CHAT),
                                           ("x-user-id", "grant-user-current"),
                                           ("x-user-email", "grant%2Bcurrent%40example.test")):
                        self.assertTrue(seen["headers"].get(name) == expected,
                                        f"failed file attempt trusted input mismatch: {name}")
                    for name in ("cookie", "referer", "x-ocu-grant", "x-ocu-auth-mode",
                                 "x-ocu-internal-token", "x-ocu-resource-path",
                                 "x-ocu-resource-query", "x-ocu-resource-method", "origin"):
                        self.assertTrue(name not in seen["headers"],
                                        f"failed file attempt must exclude {name}")
            else:
                self.expect_resource_attempts(before, "auth", {
                    "x-ocu-grant": grant, "x-chat-id": CHAT,
                    "x-ocu-resource-path": filename, "x-ocu-resource-method": "GET",
                })
                self.assertEqual(len(self.since("ocu", before)), 0)
            for secret in (grant, "session=owner", TOKEN, "private-log-canary"):
                self.assertTrue(secret.encode() not in body and secret not in str(headers),
                                "failed grant response must not expose credentials")
        with ERROR_LOG.open("rb") as stream:
            stream.seek(before_log)
            protected = stream.read()
        for secret in (GRANT, "drop-payload.synthetic-signature",
                       "malformed-payload.synthetic-signature", "session=owner",
                       TOKEN, "private-log-canary"):
            self.assertTrue(secret.encode() not in protected,
                            "grant failure diagnostics must not expose credentials")
        control_offset = ERROR_LOG.stat().st_size
        self.assertEqual(self.request("/native-log-control")[0], 502)
        with ERROR_LOG.open("rb") as stream:
            stream.seek(control_offset)
            control = stream.read()
        self.assertTrue(b"/native-log-control" in control,
                        "nonsecret upstream failure must prove the log observer works")

    def test_resource_grant_cannot_authorize_session_or_other_routes(self):
        headers = {"Cookie": "session=" + GRANT, "Authorization": "Bearer " + GRANT,
                   "X-Ocu-Grant": GRANT, "X-Ocu-Auth-Mode": "grant"}
        for path, extra in (
            ("/ocu/api/outputs/" + CHAT, {}),
            ("/ocu/static/deep/preview.js", {}),
            ("/ocu/files/" + CHAT + "/report.html", {}),
            ("/ocu/terminal/" + CHAT + "/ws",
             {"Upgrade": "websocket", "Connection": "Upgrade"}),
        ):
            with self.subTest(surface=path.split("/")[2]):
                before = self.snapshot()
                status, response_headers, _ = self.request(path, headers={**headers, **extra})
                self.assertEqual(status, 401)
                self.assertTrue(not any(name.lower() == "location" for name, _ in response_headers),
                                "a grant cannot establish a session")
                self.assertEqual(len(self.since("ocu", before)), 0)
                rows = self.since("auth", before)
                self.assertEqual(len(rows), 1)
                self.assertTrue(rows[0]["target"] != "/api/v1/ocu/resource-grants/auth",
                                "other routes must not select grant verification")
        before = self.snapshot()
        self.assertEqual(self.request("/web-apps/editor.js", headers=headers, port=OFFICE_PROXY_PORT)[0], 401)
        self.assertEqual(len(self.since("documentserver", before)), 0)

    def test_encoded_file_names_query_and_generated_file_headers(self):
        target = "/ocu/files/" + CHAT + "/sub/space%20hash%23plus%2Bpercent%25.html?revision=7"
        before = self.snapshot()
        status, headers, body = self.follow_file(target)
        self.assertEqual(status, 200)
        self.expect_file_auth(before)
        self.assertEqual(self.since("ocu", before)[-1]["target"], target[4:])
        self.assertEqual(body, b"file")
        self.assertEqual([value for name, value in headers if name.lower() == "content-security-policy"],
                         ["sandbox allow-scripts allow-forms"])
        self.assertEqual([value for name, value in headers if name.lower() == "x-content-type-options"], ["nosniff"])
        encoded_dot = "/ocu/files/" + CHAT + "/sub/report%2Ehtml?revision=8"
        before = self.snapshot()
        status, headers, _ = self.follow_file(encoded_dot)
        self.assertEqual(status, 200)
        self.expect_file_auth(before)
        self.assertEqual(self.since("ocu", before)[-1]["target"], encoded_dot[4:])
        self.assertEqual([value for key, value in headers if key.lower() == "content-security-policy"],
                         ["sandbox allow-scripts allow-forms"])
        for extension in ("svg", "xhtml", "xml"):
            with self.subTest(extension=extension):
                before = self.snapshot()
                status, headers, _ = self.follow_file("/ocu/files/" + CHAT + "/report." + extension)
                self.assertEqual(status, 200)
                self.expect_file_auth(before)
                self.assertEqual([value for name, value in headers if name.lower() == "content-security-policy"],
                                 ["sandbox allow-scripts allow-forms"])
                self.assertEqual([value for name, value in headers if name.lower() == "x-content-type-options"], ["nosniff"])
        for name, disposition in (("report.bin", "inline; filename=report.html"),
                                  ("report.html?download=1", "attachment; filename=report.html"),
                                  ("report.html?revision=7&download=1", "attachment; filename=report.html")):
            before = self.snapshot()
            target = "/ocu/files/" + CHAT + "/" + name
            download = name != "report.bin"
            status, headers, _ = (self.request(target, headers=self.owner()) if download
                                  else self.follow_file(target))
            self.assertEqual(status, 200)
            if download:
                self.expect_auth(before)
            else:
                self.expect_file_auth(before)
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
            before = self.snapshot()
            status, headers, _ = self.follow_file(
                "/ocu/files/" + CHAT + "/report.html?" + query)
            self.assertEqual(status, 200)
            self.expect_file_auth(before)
            self.assertEqual(self.since("ocu", before)[-1]["target"],
                             "/files/" + CHAT + "/report.html?" + query)
            self.assertEqual([value for key, value in headers if key.lower() == "content-disposition"],
                             [disposition])
            self.assertEqual([value for key, value in headers if key.lower() == "content-security-policy"],
                             ["sandbox allow-scripts allow-forms"])
        before = self.snapshot()
        status, headers, body = self.follow_file(
            "/ocu/files/" + CHAT + "/backend-html403.html")
        self.assertEqual((status, body), (403, b"upstream error"))
        self.expect_file_auth(before)
        self.assertEqual([value for key, value in headers if key.lower() == "content-security-policy"],
                         ["sandbox allow-scripts allow-forms"])
        self.assertEqual([value for key, value in headers if key.lower() == "x-content-type-options"],
                         ["nosniff"])

    def test_webui_default_location_preserves_http_and_bidirectional_websocket(self):
        before = self.snapshot()
        status, _, body = self.request("/", headers=self.owner())
        self.assertEqual((status, body), (200, b"webui"))
        self.assertEqual([row["target"] for row in self.since("auth", before)], ["/"])
        self.assertEqual(self.since("ocu", before), [])

        path = "/ws/socket.io/?EIO=4&transport=websocket"
        key = base64.b64encode(b"webui-test-key12").decode("ascii")
        before = self.snapshot()
        request = "\r\n".join((
            f"GET {path} HTTP/1.1",
            f"Host: 127.0.0.1:{PROXY_PORT}",
            "Connection: Upgrade",
            "Upgrade: websocket",
            f"Origin: {ORIGIN}",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            "Cookie: session=owner",
            "Authorization: Bearer browser-user-token",
            "",
            "",
        )).encode("ascii")
        with socket.create_connection(("127.0.0.1", PROXY_PORT), timeout=15) as connection:
            connection.sendall(request)
            stream = connection.makefile("rb")
            status_line = stream.readline()
            self.assertEqual(status_line.split()[1], b"101", status_line)
            headers = {}
            while True:
                line = stream.readline()
                if line == b"\r\n":
                    break
                name, value = line.split(b":", 1)
                headers[name.lower()] = value.strip()
            accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
            self.assertEqual(headers.get(b"upgrade", b"").lower(), b"websocket")
            self.assertEqual(headers.get(b"sec-websocket-accept"), accept)
            payload = b"upstream-ok"
            mask = b"\x11\x22\x33\x44"
            frame = b"\x81" + bytes((0x80 | len(payload),)) + mask
            frame += bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            connection.sendall(frame)
            self.assertEqual(stream.read(2 + len(payload)), b"\x81" + bytes((len(payload),)) + payload)
            stream.close()

        seen = self.since("auth", before)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["target"], path)
        self.assertEqual(seen[0]["headers"].get("cookie"), "session=owner")
        self.assertEqual(seen[0]["headers"].get("authorization"), "Bearer browser-user-token")
        self.assertEqual(seen[0]["headers"].get("upgrade", "").lower(), "websocket")
        self.assertEqual(seen[0]["headers"].get("connection", "").lower(), "upgrade")
        self.assertEqual(self.since("ocu", before), [])

    def test_websocket_auth_and_cookie_forwarding_without_custom_header(self):
        for route, upstream in (("/ocu/terminal/" + CHAT + "/ws", "/terminal/" + CHAT + "/ws"),
                                ("/ocu/browser/" + CHAT + "/devtools/page/PAGE-1",
                                 "/browser/" + CHAT + "/devtools/page/PAGE-1")):
            with self.subTest(route=route):
                headers = self.owner({"Connection": "Upgrade", "Upgrade": "websocket",
                                      "Origin": ORIGIN,
                                      "X-Ocu-Grant": "forged-grant",
                                      "X-Ocu-Auth-Mode": "grant",
                                      "X-Ocu-Resource-Path": "other.bin",
                                      "X-Ocu-Resource-Query": "wrong=1",
                                      "X-Ocu-Resource-Method": "POST",
                                      "Sec-WebSocket-Key": base64.b64encode(b"websocket-test-key").decode(),
                                      "Sec-WebSocket-Version": "13"})
                before = self.snapshot()
                connection = http.client.HTTPConnection("127.0.0.1", PROXY_PORT, timeout=15)
                connection.request("GET", route, headers=headers)
                response = connection.getresponse()
                self.assertEqual(response.status, 101)
                self.assertEqual(response.getheader("Upgrade"), "websocket")
                self.assertEqual(response.fp.read(4), b"\x81\x02ok")
                connection.close()
                self.expect_auth(before)
                seen = self.since("ocu", before)[-1]
                self.assertEqual(seen["target"], upstream)
                self.assertEqual(seen["headers"].get("cookie"), "session=owner")
                self.assertTrue(seen["headers"].get("authorization") == "Bearer " + TOKEN,
                                "WebSocket must use the internal credential")
                self.assertEqual(seen["headers"].get("upgrade", "").lower(), "websocket")
                for name in ("x-ocu-grant", "x-ocu-auth-mode", "x-ocu-resource-path",
                             "x-ocu-resource-query", "x-ocu-resource-method"):
                    self.assertTrue(name not in seen["headers"], f"WebSocket must strip client {name}")
                before = self.snapshot()
                status, _, _ = self.request(route, headers={**{key: value for key, value in headers.items()
                                                               if key != "Cookie"},
                                                            "Cookie": "session=foreign"})
                self.assertEqual(status, 404)
                self.assertEqual(self.since("ocu", before), [])
                self.expect_auth(before, cookie="session=foreign")


if __name__ == "__main__":
    unittest.main()
