# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Pinning tests for /system-prompt endpoint.

The endpoint at computer-use-server/app.py:1154-1205 is already a correct port
of the internal fork v3.7/v3.8. These tests pin the current contract so future
refactors cannot regress it. All tests should PASS on current HEAD.

Run: python -m pytest tests/orchestrator/test_system_prompt_endpoint.py -v
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "computer-use-server"))

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import skill_manager as skill_manager_module  # noqa: E402
import system_prompt  # noqa: E402

COPY_KNOWLEDGE_OPENING = "If a `copy_knowledge_file` tool is available"
COPY_KNOWLEDGE_SENTENCE = (
    "If a `copy_knowledge_file` tool is available, it brings a knowledge-base document's "
    "original file into `/mnt/user-data/files/`, where you can process it like an uploaded file."
)
UPLOADED_NOTES_OPEN = "<notes_on_user_uploaded_files>"
UPLOADED_NOTES_CLOSE = "</notes_on_user_uploaded_files>"
USER_SKILLS_XML = "<available_skills>\n<skill>\n<name>\nexample\n</name>\n</skill>\n</available_skills>"


class SystemPromptEndpointContract(unittest.TestCase):
    """Pin /system-prompt endpoint behaviour — DO NOT modify the endpoint to make these pass."""

    @classmethod
    def setUpClass(cls):
        os_token = "ocu-test-internal-token"
        import os
        os.environ["OCU_INTERNAL_TOKEN"] = os_token
        cls.client = TestClient(app_module.app)
        cls.client.headers.update({"Authorization": f"Bearer {os_token}"})
        cls.public_base_url = app_module.PUBLIC_BASE_URL

    def setUp(self):
        # Ensure tests are hermetic: reset skill_manager memory cache so that
        # a prior test run (or another test in this file) cannot leak a cached
        # skill list for the same email. Tests that need an external provider
        # stub it explicitly below.
        skill_manager_module._memory_cache.clear()

    def test_chat_id_substitutes_urls_and_literal(self):
        resp = self.client.get("/system-prompt", params={"chat_id": "abc123"})
        self.assertEqual(resp.status_code, 200)
        body = resp.text
        self.assertIn(f"{self.public_base_url}/files/abc123", body)
        self.assertIn(f"{self.public_base_url}/files/abc123/archive", body)
        self.assertIn("abc123", body)
        self.assertNotIn("{file_base_url}", body)
        self.assertNotIn("{archive_url}", body)
        self.assertNotIn("{chat_id}", body)

    def test_user_email_returns_default_skills_when_provider_unconfigured(self):
        # Hermetic: force _fetch_user_config -> None and _load_user_config_cache -> None
        # so get_user_skills() takes the DEFAULT_PUBLIC_SKILLS fallback path
        # regardless of MCP_TOKENS_URL / MCP_TOKENS_API_KEY env state on the host.
        async def _no_provider(_email):
            return None

        with patch.object(skill_manager_module, "_fetch_user_config", side_effect=_no_provider), \
             patch.object(skill_manager_module, "_load_user_config_cache", return_value=None):
            resp = self.client.get(
                "/system-prompt",
                params={"chat_id": "abc", "user_email": "test@example.com"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("<available_skills>", resp.text)
        # DEFAULT_PUBLIC_SKILLS must appear — pin a representative one
        self.assertIn("<name>\ndocx\n</name>", resp.text)

    def test_legacy_file_base_url_extracts_chat_id(self):
        """
        Pre-v4.0.0 integrations could pass ?file_base_url=... to embed their
        own browser-facing URLs in the prompt. Since v4.0.0 the server owns
        PUBLIC_BASE_URL. We only still accept file_base_url to extract its
        trailing chat_id for seamless migration. archive_url is ignored —
        server derives it from PUBLIC_BASE_URL + chat_id.
        """
        resp = self.client.get(
            "/system-prompt",
            params={
                "file_base_url": "https://legacy.example.com/files/xyz",
                "archive_url": "https://legacy.example.com/files/xyz/arch",
            },
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.text
        # chat_id extracted from the trailing path segment, URL owned by server.
        self.assertIn(f"{self.public_base_url}/files/xyz", body)
        self.assertIn(f"{self.public_base_url}/files/xyz/archive", body)
        # Neither the legacy host nor the legacy archive URL leaks into the prompt.
        self.assertNotIn("legacy.example.com", body)
        # No raw template placeholders.
        self.assertNotIn("{file_base_url}", body)
        self.assertNotIn("{archive_url}", body)
        self.assertNotIn("{chat_id}", body)

    def test_legacy_file_base_url_ignored_when_chat_id_present(self):
        """Explicit chat_id always wins over legacy file_base_url extraction."""
        resp = self.client.get(
            "/system-prompt",
            params={
                "chat_id": "winner",
                "file_base_url": "https://legacy.example.com/files/loser",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(f"{self.public_base_url}/files/winner", resp.text)
        self.assertNotIn("loser", resp.text)

    def test_no_params_returns_unsubstituted_template(self):
        """Legacy diagnostic path — pre-v4.0.0 external integrators (n8n, custom
        HTTP callers) hit /system-prompt with no params and substitute the
        placeholders themselves downstream. Substituting them server-side with a
        fake "default" chat_id silently feeds those callers URLs they never
        agreed to. Keep the placeholders intact when no chat_id is provided
        from any source (header, query, or legacy file_base_url)."""
        resp = self.client.get("/system-prompt")
        self.assertEqual(resp.status_code, 200)
        body = resp.text
        # At least one placeholder still present — the template wasn't
        # substituted with a synthetic chat_id.
        self.assertTrue(
            any(ph in body for ph in ("{file_base_url}", "{archive_url}", "{chat_id}")),
            "Expected un-substituted placeholders in no-params response",
        )

    # ------------------------------------------------------------------
    # Tier 7 header priority — added in the "maximum native surface" refactor
    # ------------------------------------------------------------------

    def test_header_overrides_query_chat_id(self):
        resp = self.client.get(
            "/system-prompt",
            params={"chat_id": "qry"},
            headers={"X-Chat-Id": "hdr"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(f"{self.public_base_url}/files/hdr", resp.text)
        self.assertNotIn("/files/qry", resp.text)

    def test_openwebui_alias_works(self):
        resp = self.client.get(
            "/system-prompt",
            headers={"X-OpenWebUI-Chat-Id": "alias-demo"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(f"{self.public_base_url}/files/alias-demo", resp.text)

    def test_header_user_email_overrides_query(self):
        # Patch the renderer itself so we can assert exactly which user_email
        # the endpoint forwarded — without this, the assertion only proves
        # that *some* prompt came back (DEFAULT_PUBLIC_SKILLS appears for
        # every email), which would silently pass even if header priority
        # broke and the query value won.
        captured: dict = {}

        async def _capture(chat_id, user_email, features=frozenset()):
            captured["chat_id"] = chat_id
            captured["user_email"] = user_email
            return "stub-prompt-body"

        with patch.object(app_module, "render_system_prompt", side_effect=_capture):
            resp = self.client.get(
                "/system-prompt",
                params={"chat_id": "abc", "user_email": "ignored@example.com"},
                headers={"X-User-Email": "winner@example.com"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured.get("user_email"), "winner@example.com")

    def test_content_type_is_text_plain(self):
        resp = self.client.get("/system-prompt", params={"chat_id": "abc"})
        self.assertTrue(
            resp.headers.get("content-type", "").startswith("text/plain"),
            f"Expected text/plain, got {resp.headers.get('content-type')}",
        )

    def test_public_base_url_header_is_returned(self):
        """The filter needs the public URL to build browser-facing preview/archive
        links but its ORCHESTRATOR_URL Valve holds the internal URL. Server must
        expose the public URL (from PUBLIC_BASE_URL env) on every /system-prompt
        response so the filter can cache and use it in outlet()."""
        resp = self.client.get("/system-prompt", params={"chat_id": "abc"})
        self.assertEqual(resp.status_code, 200)
        header = resp.headers.get("X-Public-Base-URL")
        self.assertIsNotNone(header, "X-Public-Base-URL header missing")
        self.assertEqual(header, self.public_base_url)

    def test_rendered_prompt_names_only_workspace_files_path(self):
        resp = self.client.get("/system-prompt", params={"chat_id": "abc123"})
        self.assertEqual(resp.status_code, 200)
        body = resp.text
        self.assertNotIn("/mnt/user-data/uploads", body)
        self.assertNotIn("/mnt/user-data/outputs", body)
        self.assertIn("/mnt/user-data/files", body)
        self.assertIn("/home/assistant", body)
        self.assertRegex(
            body,
            r"(?is)/mnt/user-data/files.{0,400}(uploaded|generated).{0,400}(uploaded|generated)",
        )

    def test_uploaded_files_notes_carry_copy_knowledge_file_sentence(self):
        """DankerMu/open-webui#785: a saved chat's prompt tells the model, inside
        the uploaded-files notes, what an available copy tool does."""
        resp = self.client.get(
            "/system-prompt", params={"chat_id": "3f6c1e2a-8b4d-4c7e-9a15-2d0f6b8e4c31"}
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.text
        self.assertEqual(body.count(COPY_KNOWLEDGE_OPENING), 1)
        self.assertEqual(body.count(COPY_KNOWLEDGE_SENTENCE), 1)
        self.assertEqual(body.count(UPLOADED_NOTES_OPEN), 1)
        self.assertEqual(body.count(UPLOADED_NOTES_CLOSE), 1)
        self.assertLess(body.index(UPLOADED_NOTES_OPEN), body.index(COPY_KNOWLEDGE_SENTENCE))
        self.assertLess(body.index(COPY_KNOWLEDGE_SENTENCE), body.index(UPLOADED_NOTES_CLOSE))


@pytest.mark.parametrize(
    "prompt",
    (
        system_prompt.SYSTEM_PROMPT_TEMPLATE,
        system_prompt.build_system_prompt(skills_xml=USER_SKILLS_XML),
    ),
    ids=("template", "built"),
)
def test_every_prompt_variant_carries_copy_knowledge_file_sentence_once(prompt):
    assert prompt.count(COPY_KNOWLEDGE_SENTENCE) == 1






if __name__ == "__main__":
    unittest.main()
