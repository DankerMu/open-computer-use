# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Tests for path traversal protection in computer-use-server/app.py endpoints.

Note: FastAPI/Starlette normalizes `..` in URL paths at the HTTP level,
so path traversal via `../../` in URL segments is blocked before reaching handlers.
These tests verify that:
1. sanitize_chat_id() rejects malicious chat_id values
2. safe_path() provides defense-in-depth at the application level
3. Normal operations continue to work correctly
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "computer-use-server"))

from fastapi.testclient import TestClient


VALID_CHAT_ID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
UNKNOWN_CHAT_ID = "c3d4e5f6-a7b8-9012-cdef-123456789012"
INTERNAL_TOKEN = "ocu-test-internal-token"
UPLOAD_READ_PATHS = ("manifest", "list")
# MD5 of the seeded uploads/uploaded.txt bytes b"world".
SEEDED_UPLOAD_MD5 = "7d793037a0760186574b0282f2f435e7"
SEEDED_UPLOAD_NAME = "uploaded.txt"


@pytest.fixture
def tmp_data(tmp_path):
    """Create temporary data directory with test files."""
    chat_dir = tmp_path / VALID_CHAT_ID
    outputs = chat_dir / "outputs"
    uploads = chat_dir / "uploads"
    outputs.mkdir(parents=True)
    uploads.mkdir(parents=True)
    (outputs / "test.txt").write_text("hello")
    (uploads / "uploaded.txt").write_text("world")
    return tmp_path


@pytest.fixture
def client(tmp_data, monkeypatch):
    """TestClient with patched BASE_DATA_DIR and a valid service token."""
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
    for name in ("app", "docker_manager", "outputs_broker"):
        sys.modules.pop(name, None)
    import app as app_module
    import docker_manager

    monkeypatch.setattr(app_module, "BASE_DATA_DIR", tmp_data)
    monkeypatch.setattr(docker_manager, "BASE_DATA_DIR", tmp_data)
    http = TestClient(app_module.app)
    http.headers.update({"Authorization": f"Bearer {INTERNAL_TOKEN}"})
    try:
        yield http
    finally:
        http.close()


class TestChatIdValidation:
    """Test that endpoints reject malicious chat_id values."""

    def test_upload_rejects_dot_dot_chat_id(self, client):
        resp = client.post(
            "/api/uploads/..test../file.txt",
            files={"file": ("test.txt", b"content")},
        )
        assert resp.status_code == 400

    def test_download_rejects_dot_dot_chat_id(self, client):
        resp = client.get("/files/..test../somefile")
        assert resp.status_code == 400

    def test_archive_rejects_dot_dot_chat_id(self, client):
        resp = client.get("/files/..test../archive")
        assert resp.status_code == 400

    def test_outputs_rejects_dot_dot_chat_id(self, client):
        resp = client.get("/api/outputs/..test..")
        assert resp.status_code == 400

    def test_manifest_rejects_dot_dot_chat_id(self, client):
        resp = client.get("/api/uploads/..test../manifest")
        assert resp.status_code == 400

    def test_uploads_list_rejects_dot_dot_chat_id(self, client):
        resp = client.get("/api/uploads/..test../list")
        assert resp.status_code == 400


class TestNormalOperations:
    """Test that legitimate operations continue to work."""

    def test_upload_normal(self, client):
        resp = client.post(
            f"/api/uploads/{VALID_CHAT_ID}/newfile.txt",
            files={"file": ("newfile.txt", b"content")},
        )
        assert resp.status_code == 200

    def test_download_normal(self, client):
        resp = client.get(f"/files/{VALID_CHAT_ID}/test.txt")
        assert resp.status_code == 200
        assert resp.text == "hello"

    def test_archive_normal(self, client):
        resp = client.get(f"/files/{VALID_CHAT_ID}/archive")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"

    def test_outputs_normal(self, client):
        resp = client.get(f"/api/outputs/{VALID_CHAT_ID}")
        assert resp.status_code == 200

    def test_default_chat_id_is_rejected(self, client, tmp_data):
        """A shared default sandbox is forbidden even in single-user mode."""
        (tmp_data / "default" / "outputs").mkdir(parents=True)
        resp = client.get("/api/outputs/default")
        assert resp.status_code == 400


def _assert_no_upload_metadata(response):
    assert response.status_code in (404, 405)
    assert set(response.json()) == {"detail"}
    body = response.text
    assert SEEDED_UPLOAD_NAME not in body
    assert SEEDED_UPLOAD_MD5 not in body


class TestRemovedUploadReadEndpoints:
    """Retired upload manifest and list GETs disclose no stored-file metadata."""

    @pytest.mark.parametrize("suffix", UPLOAD_READ_PATHS)
    def test_authenticated_canonical_get_returns_client_error_without_metadata(
            self, client, suffix):
        response = client.get(f"/api/uploads/{VALID_CHAT_ID}/{suffix}")
        _assert_no_upload_metadata(response)

    @pytest.mark.parametrize("suffix", UPLOAD_READ_PATHS)
    def test_missing_token_is_401_before_routing(self, client, suffix):
        client.headers.pop("Authorization", None)
        response = client.get(f"/api/uploads/{VALID_CHAT_ID}/{suffix}")
        assert response.status_code == 401
        assert SEEDED_UPLOAD_NAME not in response.text
        assert SEEDED_UPLOAD_MD5 not in response.text

    @pytest.mark.parametrize("suffix", UPLOAD_READ_PATHS)
    def test_unknown_chat_get_does_not_create_directory(
            self, client, tmp_data, suffix):
        chat_dir = tmp_data / UNKNOWN_CHAT_ID
        assert not chat_dir.exists()
        response = client.get(f"/api/uploads/{UNKNOWN_CHAT_ID}/{suffix}")
        _assert_no_upload_metadata(response)
        assert not chat_dir.exists()


class TestSafePathDirectly:
    """Direct unit tests for safe_path integration — defense-in-depth."""

    def test_safe_path_blocks_traversal(self, tmp_data):
        from security import safe_path
        from fastapi import HTTPException
        base = tmp_data / VALID_CHAT_ID / "outputs"
        with pytest.raises(HTTPException) as exc:
            safe_path(base, "../../etc/passwd")
        assert exc.value.status_code == 403

    def test_safe_path_allows_subdirs(self, tmp_data):
        from security import safe_path
        base = tmp_data / VALID_CHAT_ID / "outputs"
        result = safe_path(base, "subdir/file.txt")
        assert str(result).startswith(str(base.resolve()))
