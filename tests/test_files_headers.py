# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""HTTP contract for generated-content headers on output files."""
from __future__ import annotations

import mimetypes
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1] / "computer-use-server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

CHAT = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
INTERNAL_TOKEN = "ocu-files-headers-test-token"
MCP_API_KEY = "ocu-files-headers-mcp-key"
SANDBOX_CSP = "sandbox allow-scripts allow-forms"
XML_MIME_TYPES = ("text/xml", "application/xml")


@pytest.fixture
def app_module(monkeypatch):
    """Import the guarded app after setting the service credentials."""
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("MCP_API_KEY", MCP_API_KEY)
    monkeypatch.setenv("OCU_WEBUI_ORIGIN", "https://webui.example")
    monkeypatch.setenv("OCU_SANDBOX_SUBNET", "10.90.0.0/24")
    monkeypatch.setenv("SINGLE_USER_MODE", "true")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://ocu.example")
    monkeypatch.setenv("BASE_DATA_DIR", "/tmp/ocu-files-headers-unused")

    for name in list(sys.modules):
        if name in {
            "app",
            "auth_guard",
            "mcp_tools",
            "docker_manager",
            "outputs_broker",
            "context_vars",
            "security",
            "system_prompt",
            "skill_manager",
        } or name.startswith("mcp_resources"):
            sys.modules.pop(name, None)

    import app as loaded

    return loaded


@pytest.fixture
def output_dir(tmp_path):
    outputs = tmp_path / CHAT / "outputs"
    outputs.mkdir(parents=True)
    return outputs


@pytest.fixture
def client(app_module, output_dir, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(app_module, "BASE_DATA_DIR", output_dir.parents[1])
    import docker_manager

    monkeypatch.setattr(docker_manager, "BASE_DATA_DIR", output_dir.parents[1])
    with TestClient(app_module.app) as http:
        yield http


def _auth_headers():
    return {"Authorization": f"Bearer {INTERNAL_TOKEN}"}


def _file_url(filename):
    return f"/files/{CHAT}/{filename}"


def _assert_mirror_headers(response):
    csp_values = response.headers.get_list("content-security-policy")
    assert csp_values == [SANDBOX_CSP]
    assert all("allow-same-origin" not in value for value in csp_values)
    assert response.headers["x-content-type-options"] == "nosniff"


def _assert_headers_absent(response):
    assert "content-security-policy" not in response.headers
    assert "x-content-type-options" not in response.headers


def _assert_target_response(response, mime_type, contents, download):
    assert response.status_code == 200
    assert response.content == contents
    if download:
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["content-disposition"].startswith("attachment;")
        _assert_headers_absent(response)
        return

    assert response.headers["content-type"].split(";", 1)[0] == mime_type
    _assert_mirror_headers(response)


def _force_mime_type(monkeypatch, app_module, filename, mime_type):
    original_guess_type = app_module.mimetypes.guess_type

    def guess_type(path, strict=True):
        if Path(path).name == filename:
            return mime_type, None
        return original_guess_type(path, strict)

    monkeypatch.setattr(app_module.mimetypes, "guess_type", guess_type)


@pytest.mark.parametrize(
    ("filename", "mime_type", "contents"),
    (
        ("active.html", "text/html", b"<script>window.active = true</script>"),
        ("active.svg", "image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'/>"),
        ("active.xhtml", "application/xhtml+xml", b"<html xmlns='http://www.w3.org/1999/xhtml'/>"),
    ),
)
@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_active_content_file_responses_are_isolated(
    client, output_dir, filename, mime_type, contents, download
):
    (output_dir / filename).write_bytes(contents)

    response = client.get(
        _file_url(filename),
        headers=_auth_headers(),
        params={"download": 1} if download else None,
    )

    _assert_target_response(response, mime_type, contents, download)


@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_host_xml_content_type_is_isolated(client, output_dir, download):
    filename = "active.xml"
    contents = b"<?xml version='1.0'?><root/>"
    host_mime_type = mimetypes.guess_type(filename)[0]
    assert host_mime_type in XML_MIME_TYPES
    (output_dir / filename).write_bytes(contents)

    response = client.get(
        _file_url(filename),
        headers=_auth_headers(),
        params={"download": 1} if download else None,
    )

    _assert_target_response(response, host_mime_type, contents, download)


@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_other_xml_content_type_is_isolated(
    app_module, client, output_dir, monkeypatch, download
):
    filename = "active-other.xml"
    contents = b"<?xml version='1.0'?><alternate/>"
    host_mime_type = mimetypes.guess_type("active.xml")[0]
    assert host_mime_type in XML_MIME_TYPES
    alternate_mime_type = next(
        mime_type for mime_type in XML_MIME_TYPES if mime_type != host_mime_type
    )
    (output_dir / filename).write_bytes(contents)

    # Host MIME databases choose one XML value; execute the other through the
    # real app at the standard-library boundary.
    _force_mime_type(monkeypatch, app_module, filename, alternate_mime_type)
    response = client.get(
        _file_url(filename),
        headers=_auth_headers(),
        params={"download": 1} if download else None,
    )

    _assert_target_response(response, alternate_mime_type, contents, download)


@pytest.mark.parametrize(
    ("filename", "mime_type", "contents"),
    (
        ("note.txt", "text/plain", b"plain output"),
        ("image.png", "image/png", b"\x89PNG\r\n\x1a\n"),
        ("unknown", "application/octet-stream", b"unknown output"),
    ),
)
def test_non_active_file_responses_remain_unheadered(
    client, output_dir, filename, mime_type, contents
):
    (output_dir / filename).write_bytes(contents)

    response = client.get(_file_url(filename), headers=_auth_headers())

    assert response.status_code == 200
    assert response.content == contents
    assert response.headers["content-type"].split(";", 1)[0] == mime_type
    _assert_headers_absent(response)


def test_file_errors_remain_unheadered(client, output_dir):
    missing = client.get(_file_url("missing.html"), headers=_auth_headers())
    denied = client.get(_file_url("missing.html"))

    assert missing.status_code == 404
    _assert_headers_absent(missing)
    assert denied.status_code == 401
    _assert_headers_absent(denied)
