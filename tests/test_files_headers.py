# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""HTTP contract for generated-content headers on output files."""
from __future__ import annotations

import mimetypes
import os
import sys
import zipfile
from io import BytesIO
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
    assert response.headers.get_list("x-content-type-options") == ["nosniff"]


def _assert_headers_absent(response):
    assert "content-security-policy" not in response.headers
    assert "x-content-type-options" not in response.headers


def _assert_target_response(response, mime_type, contents, download):
    assert response.status_code == 200
    assert response.content == contents
    assert response.headers.get_list("cache-control") == ["no-store"]
    if download:
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["content-disposition"].startswith("attachment;")
        _assert_headers_absent(response)
        return

    assert response.headers["content-type"].split(";", 1)[0] == mime_type
    assert response.headers["content-disposition"].startswith("inline;")
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
        (
            "document.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            b"PK\x03\x04document output",
        ),
        ("note.txt", "text/plain", b"plain output"),
        ("image.png", "image/png", b"\x89PNG\r\n\x1a\n"),
        ("unknown", "application/octet-stream", b"unknown output"),
    ),
)
@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_non_active_file_responses_have_no_isolation_headers(
    client, output_dir, filename, mime_type, contents, download
):
    (output_dir / filename).write_bytes(contents)

    response = client.get(
        _file_url(filename),
        headers=_auth_headers(),
        params={"download": 1} if download else None,
    )

    assert response.status_code == 200
    assert response.content == contents
    assert response.headers.get_list("cache-control") == ["no-store"]
    if download:
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["content-disposition"] == f'attachment; filename="{filename}"'
    else:
        assert response.headers["content-type"].split(";", 1)[0] == mime_type
        assert "content-disposition" not in response.headers
    _assert_headers_absent(response)


@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_inline_plain_text_replacement_is_fresh_and_no_store(client, output_dir, download):
    filename = "note.txt"
    original = b"first output"
    replacement = b"fresh output"
    assert len(original) == len(replacement)
    target = output_dir / filename
    target.write_bytes(original)
    original_stat = target.stat()

    first = client.get(
        _file_url(filename),
        headers=_auth_headers(),
        params={"download": 1} if download else None,
    )

    assert first.status_code == 200
    assert first.content == original
    expected_mime = "application/octet-stream" if download else "text/plain"
    assert first.headers["content-type"].split(";", 1)[0] == expected_mime
    if download:
        assert first.headers["content-disposition"] == 'attachment; filename="note.txt"'
    else:
        assert "content-disposition" not in first.headers
    _assert_headers_absent(first)

    staged = output_dir / "replacement.txt"
    staged.write_bytes(replacement)
    os.replace(staged, target)
    os.utime(target, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    replaced_stat = target.stat()
    assert replaced_stat.st_size == original_stat.st_size
    assert replaced_stat.st_mtime_ns == original_stat.st_mtime_ns

    second = client.get(
        _file_url(filename),
        headers={
            **_auth_headers(),
            "If-None-Match": first.headers["etag"],
            "If-Modified-Since": first.headers["last-modified"],
        },
        params={"download": 1} if download else None,
    )

    assert second.status_code == 200
    assert second.content == replacement
    assert second.headers["content-type"].split(";", 1)[0] == expected_mime
    if download:
        assert second.headers["content-disposition"] == 'attachment; filename="note.txt"'
    else:
        assert "content-disposition" not in second.headers
    _assert_headers_absent(second)
    assert first.headers.get_list("cache-control") == ["no-store"]
    assert second.headers.get_list("cache-control") == ["no-store"]


def test_file_errors_have_no_cache_or_isolation_headers(client, output_dir):
    missing = client.get(_file_url("missing.html"), headers=_auth_headers())
    denied = client.get(_file_url("missing.html"))

    assert missing.status_code == 404
    assert missing.content == b'{"detail":"File not found: missing.html"}'
    assert missing.headers["content-type"] == "application/json"
    assert missing.headers.get_list("cache-control") == []
    _assert_headers_absent(missing)
    assert denied.status_code == 401
    assert denied.content == b'{"detail":"Unauthorized"}'
    assert denied.headers["content-type"] == "application/json"
    assert denied.headers.get_list("www-authenticate") == ["Bearer"]
    assert denied.headers.get_list("cache-control") == []
    _assert_headers_absent(denied)


def test_archive_keeps_zip_contents_without_cache_or_isolation_headers(client, output_dir):
    (output_dir / "note.txt").write_bytes(b"plain output")
    nested = output_dir / "nested"
    nested.mkdir()
    (nested / "active.html").write_bytes(b"<p>archived output</p>")

    response = client.get(_file_url("archive"), headers=_auth_headers())

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == f"attachment; filename=chat-{CHAT}-outputs.zip"
    assert response.headers.get_list("cache-control") == []
    _assert_headers_absent(response)
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        assert sorted(archive.namelist()) == ["nested/active.html", "note.txt"]
        assert archive.read("note.txt") == b"plain output"
        assert archive.read("nested/active.html") == b"<p>archived output</p>"


@pytest.mark.parametrize("visible", (True, False), ids=("mixed", "hidden-only"))
def test_archive_omits_hidden_paths_without_removing_them(client, output_dir, visible):
    hidden = {
        ".upload-stale": b"stale staging",
        "nested/.upload-stale": b"nested staging",
        ".private/visible.txt": b"hidden ancestor",
    }
    for name, body in hidden.items():
        path = output_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    if visible:
        (output_dir / "nested" / "complete.txt").write_bytes(b"complete visible bytes")
    with (output_dir / ".upload-active").open("wb") as active:
        active.write(b"in-progress staging")
        active.flush()
        response = client.get(_file_url("archive"), headers=_auth_headers())
        assert response.status_code == (200 if visible else 404)
        if visible:
            with zipfile.ZipFile(BytesIO(response.content)) as archive:
                assert archive.namelist() == ["nested/complete.txt"]
                assert archive.read("nested/complete.txt") == b"complete visible bytes"
        else:
            assert response.json() == {"detail": "No files found in outputs directory"}
    hidden[".upload-active"] = b"in-progress staging"
    assert {name: (output_dir / name).read_bytes() for name in hidden} == hidden


@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_inline_html_uses_rfc5987_filename_encoding(client, output_dir, download):
    chinese = "简报.html"
    quoted = 'quote"name.html'
    (output_dir / chinese).write_bytes(b"<p>chinese</p>")
    (output_dir / quoted).write_bytes(b"<p>quoted</p>")

    params = {"download": 1} if download else None
    chinese_resp = client.get(_file_url(chinese), headers=_auth_headers(), params=params)
    quoted_resp = client.get(_file_url(quoted), headers=_auth_headers(), params=params)

    assert chinese_resp.status_code == 200
    assert quoted_resp.status_code == 200
    assert chinese_resp.content == b"<p>chinese</p>"
    assert quoted_resp.content == b"<p>quoted</p>"
    assert chinese_resp.headers.get_list("cache-control") == ["no-store"]
    assert quoted_resp.headers.get_list("cache-control") == ["no-store"]
    if download:
        _assert_headers_absent(chinese_resp)
        _assert_headers_absent(quoted_resp)
    else:
        _assert_mirror_headers(chinese_resp)
        _assert_mirror_headers(quoted_resp)
    chinese_disp = chinese_resp.headers["content-disposition"]
    quoted_disp = quoted_resp.headers["content-disposition"]
    disposition = "attachment" if download else "inline"
    assert chinese_disp.startswith(f"{disposition};")
    assert quoted_disp.startswith(f"{disposition};")
    assert "filename*=utf-8''" in chinese_disp
    assert "%E7%AE%80%E6%8A%A5.html" in chinese_disp
    assert '"' not in chinese_disp.split("filename*=", 1)[0] or "filename*=utf-8''" in chinese_disp
    assert "filename*=utf-8''" in quoted_disp
    assert "%22" in quoted_disp
    if download:
        assert chinese_resp.headers["content-type"] == "application/octet-stream"
    else:
        assert chinese_resp.headers["content-type"].startswith("text/html")
    encoded = client.get(
        "/files/%s/%s" % (CHAT, "%E7%AE%80%E6%8A%A5.html"),
        headers=_auth_headers(),
        params=params,
    )
    assert encoded.status_code == 200
    assert encoded.content == b"<p>chinese</p>"
    assert encoded.headers.get_list("cache-control") == ["no-store"]
    if download:
        _assert_headers_absent(encoded)
        assert encoded.headers["content-type"] == "application/octet-stream"
    else:
        _assert_mirror_headers(encoded)
