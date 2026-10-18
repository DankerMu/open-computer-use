# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""HTTP contract for generated-content headers on output files."""
from __future__ import annotations

import builtins
import mimetypes
import os
import sys
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote

import pytest

from tests.file_route_module_ownership import isolated_file_route_modules

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
    with isolated_file_route_modules():
        monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
        monkeypatch.setenv("MCP_API_KEY", MCP_API_KEY)
        monkeypatch.setenv("OCU_WEBUI_ORIGIN", "https://webui.example")
        monkeypatch.setenv("OCU_SANDBOX_SUBNET", "10.90.0.0/24")
        monkeypatch.setenv("SINGLE_USER_MODE", "true")
        monkeypatch.setenv("PUBLIC_BASE_URL", "http://ocu.example")
        monkeypatch.setenv("BASE_DATA_DIR", "/tmp/ocu-files-headers-unused")

        import app as loaded

        yield loaded


@pytest.fixture
def output_dir(tmp_path):
    outputs = tmp_path / CHAT / "outputs"
    outputs.mkdir(parents=True)
    return outputs


@pytest.fixture
def response_start_hooks():
    return {}


@pytest.fixture
def response_body_hooks():
    return {}


@pytest.fixture
def client(app_module, output_dir, monkeypatch, response_start_hooks, response_body_hooks):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(app_module, "BASE_DATA_DIR", output_dir.parents[1])
    import docker_manager

    monkeypatch.setattr(docker_manager, "BASE_DATA_DIR", output_dir.parents[1])
    import uploads

    monkeypatch.setattr(uploads, "BASE_DATA_DIR", output_dir.parents[1])

    def observe_route(real_route_app):
        async def route_with_send_hook(scope, receive, send):
            async def send_with_hook(message):
                if scope["type"] == "http":
                    if message["type"] == "http.response.start":
                        hook = response_start_hooks.get(scope["path"])
                        if hook is not None:
                            hook()
                    elif message["type"] == "http.response.body":
                        hook = response_body_hooks.get((scope["method"], scope["path"]))
                        if hook is not None:
                            hook(message.copy())
                await send(message)

            await real_route_app(scope, receive, send_with_hook)

        return route_with_send_hook

    # Observe the response before the HTTP normalizer can buffer response-start.
    for route in app_module.app.routes:
        if getattr(route, "path", None) == "/files/{chat_id}/{filename:path}":
            monkeypatch.setattr(route, "app", observe_route(route.app))
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


def _request_file(client, response_body_hooks, method, filename, download=False, headers=None):
    url = _file_url(filename)
    frames = []
    response_body_hooks[(method, unquote(url))] = frames.append
    response = client.request(
        method, url, headers=_auth_headers() if headers is None else headers,
        params={"download": 1} if download else None,
    )
    if method == "HEAD" and response.status_code == 200:
        assert response.content == b""
        assert frames, "Successful HEAD did not reach the ordinary-file body observer"
        assert all(frame.get("body", b"") == b"" for frame in frames)
        assert not frames[-1].get("more_body", False)
    return response


def _assert_original_metadata(response):
    assert response.headers["content-length"] == "25"
    assert response.headers["etag"] == '"3f2348dd2177119a98a73b4ce6735534"'
    assert response.headers["last-modified"] == "Tue, 14 Nov 2023 22:13:20 GMT"


@pytest.mark.parametrize(
    "filename, target_name, mime_type, isolated",
    (
        ("generated.html", "generated.html", "text/html", True),
        ("generated.svg", "generated.svg", "image/svg+xml", True),
        ("generated.xhtml", "generated.xhtml", "application/xhtml+xml", True),
        ("text.xml", "text.xml", "text/xml", True),
        ("application.xml", "application.xml", "application/xml", True),
        ("note.txt", "note.txt", "text/plain", False),
        ("image.png", "image.png", "image/png", False),
        ("document.docx", "document.docx",
         "application/vnd.openxmlformats-officedocument.wordprocessingml.document", False),
        ("unknown", "unknown", "application/octet-stream", False),
        ("%E7%AE%80%E6%8A%A5.html", "简报.html", "text/html", True),
        ("quote%22name.html", 'quote"name.html', "text/html", True),
    ),
)
@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_file_head_metadata_and_policy_match_known_get(
    app_module, client, output_dir, monkeypatch, response_body_hooks,
    filename, target_name, mime_type, isolated, download
):
    contents = {
        "image/svg+xml": b"<svg>generated data</svg>",
        "text/xml": b"<?xml version='1.0'?><a/>",
        "application/xml": b"<?xml version='1.0'?><a/>",
        "text/plain": b"original descriptor bytes",
        "image/png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 17,
        "application/octet-stream": b"\x00" * 25,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            b"PK\x03\x04" + b"\x00" * 21,
    }.get(mime_type, b"<p>generated ordinary</p>")
    target = output_dir / target_name
    target.write_bytes(contents)
    os.utime(target, (1700000000, 1700000000))
    if mime_type in XML_MIME_TYPES:
        _force_mime_type(monkeypatch, app_module, target_name, mime_type)

    get_response = _request_file(client, response_body_hooks, "GET", filename, download)
    head_response = _request_file(client, response_body_hooks, "HEAD", filename, download)

    assert get_response.status_code == 200
    assert get_response.content == contents
    assert head_response.status_code == 200
    for response in (get_response, head_response):
        _assert_original_metadata(response)
        assert response.headers.get_list("cache-control") == ["no-store"]
        expected_mime = "application/octet-stream" if download else mime_type
        assert response.headers["content-type"].split(";", 1)[0] == expected_mime
        if isolated and not download:
            _assert_mirror_headers(response)
        else:
            _assert_headers_absent(response)
        if isolated or download:
            disposition = "attachment" if download else "inline"
            filename_parameter = {
                "简报.html": "filename*=utf-8''%E7%AE%80%E6%8A%A5.html",
                'quote"name.html': "filename*=utf-8''quote%22name.html",
            }.get(target_name, f'filename="{target_name}"')
            expected = f"{disposition}; {filename_parameter}"
            assert response.headers["content-disposition"] == expected
        else:
            assert "content-disposition" not in response.headers
    for header in ("content-type", "content-disposition", "content-security-policy",
                   "x-content-type-options", "cache-control", "etag",
                   "last-modified", "content-length"):
        assert head_response.headers.get_list(header) == get_response.headers.get_list(header)


@pytest.mark.parametrize("alias_kind", ("leaf", "ancestor"))
@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_file_head_safe_alias_preserves_selected_target_metadata(
    client, output_dir, response_body_hooks, alias_kind, download
):
    target = output_dir / "nested" / "target.html"
    target.parent.mkdir()
    contents = b"<p>generated ordinary</p>"
    target.write_bytes(contents)
    os.utime(target, (1700000000, 1700000000))
    if alias_kind == "leaf":
        (output_dir / "alias.txt").symlink_to(target)
        filename = "alias.txt"
    else:
        (output_dir / "alias-dir").symlink_to(target.parent, target_is_directory=True)
        filename = "alias-dir/target.html"

    get_response = _request_file(client, response_body_hooks, "GET", filename, download)
    head_response = _request_file(client, response_body_hooks, "HEAD", filename, download)

    _assert_target_response(get_response, "text/html", contents, download)
    assert head_response.status_code == 200
    disposition = "attachment" if download else "inline"
    for response in (get_response, head_response):
        _assert_original_metadata(response)
        assert response.headers["content-disposition"] == f'{disposition}; filename="target.html"'
        assert response.headers.get_list("cache-control") == ["no-store"]
        if download:
            assert response.headers["content-type"] == "application/octet-stream"
            _assert_headers_absent(response)
        else:
            assert response.headers["content-type"].split(";", 1)[0] == "text/html"
            _assert_mirror_headers(response)


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


def test_generated_file_head_preserves_get_headers_without_payload(
    client, output_dir, response_body_hooks
):
    contents = b"<p>generated ordinary</p>"
    target = output_dir / "generated.html"
    target.write_bytes(contents)
    os.utime(target, (1700000000, 1700000000))
    url = _file_url("generated.html")
    body_frames = []
    response_body_hooks[("GET", url)] = body_frames.append
    response_body_hooks[("HEAD", url)] = body_frames.append

    get_response = client.get(url, headers=_auth_headers())

    _assert_target_response(get_response, "text/html", contents, False)
    assert get_response.headers["content-length"] == "25"
    assert get_response.headers["etag"] == '"3f2348dd2177119a98a73b4ce6735534"'
    assert get_response.headers["last-modified"] == "Tue, 14 Nov 2023 22:13:20 GMT"
    assert get_response.headers["content-disposition"] == 'inline; filename="generated.html"'
    assert b"".join(frame.get("body", b"") for frame in body_frames) == contents
    assert not body_frames[-1].get("more_body", False)
    body_frames.clear()

    head_response = client.head(url, headers=_auth_headers())

    assert head_response.status_code == 200
    assert head_response.content == b""
    assert body_frames, "HEAD must reach the ordinary-file ASGI body observer"
    assert all(frame.get("body", b"") == b"" for frame in body_frames)
    assert not body_frames[-1].get("more_body", False)
    for header in (
        "content-type",
        "content-length",
        "last-modified",
        "etag",
        "content-security-policy",
        "x-content-type-options",
        "content-disposition",
        "cache-control",
    ):
        assert head_response.headers.get_list(header) == get_response.headers.get_list(header)


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


@pytest.mark.parametrize("method", ("GET", "HEAD"))
@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
@pytest.mark.parametrize("range_value", (None, "bytes=0-3", "bytes=9999999-99999999"),
                         ids=("no-range", "satisfiable", "unsatisfiable"))
def test_file_replacement_metadata_is_fresh_with_stale_conditions_and_ignored_range(
    client, output_dir, response_body_hooks, method, download, range_value
):
    target = output_dir / "fresh.html"
    target.write_bytes(b"")
    os.utime(target, (1600000000, 1600000000))
    first = _request_file(client, response_body_hooks, method, "fresh.html", download)
    assert first.status_code == 200
    assert first.headers["content-length"] == "0"
    assert first.headers["last-modified"] == "Sun, 13 Sep 2020 12:26:40 GMT"
    contents = b"<p>generated ordinary</p>"
    staged = output_dir / "staged.html"
    staged.write_bytes(contents)
    os.utime(staged, (1700000000, 1700000000))
    os.replace(staged, target)
    headers = {
        **_auth_headers(),
        "If-None-Match": first.headers["etag"],
        "If-Modified-Since": first.headers["last-modified"],
    }
    if range_value is not None:
        headers["Range"] = range_value

    response = _request_file(
        client, response_body_hooks, method, "fresh.html", download, headers
    )

    assert response.status_code == 200
    assert response.content == (contents if method == "GET" else b"")
    _assert_original_metadata(response)
    assert "content-range" not in response.headers
    assert response.headers.get_list("cache-control") == ["no-store"]
    disposition = "attachment" if download else "inline"
    assert response.headers["content-disposition"] == f'{disposition}; filename="fresh.html"'
    if download:
        assert response.headers["content-type"] == "application/octet-stream"
        _assert_headers_absent(response)
    else:
        assert response.headers["content-type"].split(";", 1)[0] == "text/html"
        _assert_mirror_headers(response)


@pytest.mark.parametrize("method", ("GET", "HEAD"))
@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
@pytest.mark.parametrize("replaced_path", ("leaf", "ancestor", "output-root", "chat-root"))
def test_file_metadata_uses_validated_descriptor_after_pre_metadata_path_swap(
    client, output_dir, tmp_path, monkeypatch, response_body_hooks,
    method, download, replaced_path
):
    victim = output_dir / "nested" / "victim.txt"
    victim.parent.mkdir()
    original = b"original descriptor bytes"
    victim.write_bytes(original)
    os.utime(victim, (1700000000, 1700000000))
    identity = (victim.stat().st_dev, victim.stat().st_ino)
    replacement_chat = tmp_path / "replacement-chat"
    replacement_outputs = replacement_chat / "outputs"
    replacement_leaf = replacement_outputs / "nested" / "victim.txt"
    replacement_leaf.parent.mkdir(parents=True)
    canary = b"benign outside metadata swap canary with distinct size"
    replacement_leaf.write_bytes(canary)
    os.utime(replacement_leaf, (1800000000, 1800000000))
    selected, redirect = {
        "leaf": (victim, replacement_leaf),
        "ancestor": (victim.parent, replacement_leaf.parent),
        "output-root": (output_dir, replacement_outputs),
        "chat-root": (output_dir.parent, replacement_chat),
    }[replaced_path]
    real_fstat = os.fstat
    real_os_open = os.open
    target_fds = set()
    target_fstats = []
    swaps = []

    def open_and_observe(*args, **kwargs):
        fd = real_os_open(*args, **kwargs)
        item = real_fstat(fd)
        if (item.st_dev, item.st_ino) == identity:
            target_fds.add(fd)
        return fd

    def fstat_after_validation(fd):
        if fd in target_fds:
            target_fstats.append(fd)
            if len(target_fstats) == 2:
                selected.rename(tmp_path / "parked-selected")
                selected.symlink_to(redirect, target_is_directory=replaced_path != "leaf")
                swaps.append(selected)
        return real_fstat(fd)

    monkeypatch.setattr(os, "fstat", fstat_after_validation)
    monkeypatch.setattr(os, "open", open_and_observe)
    response = _request_file(
        client, response_body_hooks, method, "nested/victim.txt", download
    )

    assert swaps == [selected], "Swap before response descriptor metadata did not fire"
    assert len(target_fstats) == 2
    assert selected.is_symlink()
    assert response.status_code == 200
    assert response.content == (original if method == "GET" else b"")
    assert canary not in response.content
    _assert_original_metadata(response)
    assert response.headers.get_list("cache-control") == ["no-store"]


def _assert_confined_file(response, original, canary, method="GET"):
    assert canary not in response.content
    if response.status_code == 200:
        assert response.content == (original if method == "GET" else b"")
        if method == "HEAD":
            assert response.headers["content-length"] == str(len(original))
            assert response.headers["last-modified"] == "Tue, 14 Nov 2023 22:13:20 GMT"
        assert response.headers.get_list("cache-control") == ["no-store"]
    else:
        assert response.status_code in (403, 404)
        _assert_headers_absent(response)
        for header in ("etag", "last-modified", "content-disposition"):
            assert header not in response.headers


@pytest.mark.parametrize("method", ("GET", "HEAD"))
@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_file_ancestor_replacement_cannot_redirect_to_outside_bytes(
    client, output_dir, tmp_path, response_start_hooks, response_body_hooks, download, method
):
    original = b"original in-root response bytes"
    canary = b"benign outside response-start canary"
    nested = output_dir / "nested"
    nested.mkdir()
    (nested / "victim.txt").write_bytes(original)
    os.utime(nested / "victim.txt", (1700000000, 1700000000))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.txt").write_bytes(canary)
    swaps = []

    def swap_before_body():
        assert not swaps, "Response-start swap fired more than once"
        nested.rename(output_dir / "parked-nested")
        nested.symlink_to(outside, target_is_directory=True)
        swaps.append(nested)

    # This send boundary runs synchronously before FileResponse's late open.
    response_start_hooks[_file_url("nested/victim.txt")] = swap_before_body
    response = _request_file(
        client, response_body_hooks, method, "nested/victim.txt", download
    )

    assert swaps == [nested], "Actual response-start filesystem swap did not fire"
    assert nested.is_symlink()
    _assert_confined_file(response, original, canary, method)


@pytest.mark.parametrize("method", ("GET", "HEAD"))
@pytest.mark.parametrize("replaced_path", ("leaf", "ancestor"))
@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_file_response_keeps_original_bytes_and_metadata_after_path_replacement(
    client, output_dir, tmp_path, response_start_hooks, response_body_hooks,
    replaced_path, download, method
):
    original = b"original descriptor bytes"
    victim = output_dir / "nested" / "victim.txt"
    victim.parent.mkdir()
    victim.write_bytes(original)
    os.utime(victim, (1700000000, 1700000000))
    assert victim.stat().st_size == 25
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    replacement_leaf = replacement / "victim.txt"
    replacement_leaf.write_bytes(b"different replacement inode bytes and length")
    os.utime(replacement_leaf, (1800000000, 1800000000))
    selected = victim if replaced_path == "leaf" else victim.parent
    redirect = replacement_leaf if replaced_path == "leaf" else replacement
    swaps = []

    def swap_before_body():
        assert not swaps, "Response-start swap fired more than once"
        selected.rename(tmp_path / "parked-selected")
        selected.symlink_to(redirect, target_is_directory=replaced_path == "ancestor")
        swaps.append(selected)

    response_start_hooks[_file_url("nested/victim.txt")] = swap_before_body
    response = _request_file(
        client, response_body_hooks, method, "nested/victim.txt", download
    )

    assert swaps == [selected], "Actual response-start filesystem swap did not fire"
    assert selected.is_symlink()
    assert response.status_code == 200
    assert response.content == (original if method == "GET" else b"")
    # Literal validators identify the original 25-byte object at the fixed mtime.
    assert response.headers["content-length"] == "25"
    assert response.headers["etag"] == '"3f2348dd2177119a98a73b4ce6735534"'
    assert response.headers["last-modified"] == "Tue, 14 Nov 2023 22:13:20 GMT"
    assert response.headers.get_list("cache-control") == ["no-store"]


@pytest.mark.parametrize("method", ("GET", "HEAD"))
@pytest.mark.parametrize("replaced_root", ("chat-root", "output-root"))
@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_file_root_replacement_at_acquisition_cannot_redirect_bytes(
    client, output_dir, tmp_path, monkeypatch, record_property, response_body_hooks,
    replaced_root, download, method
):
    original = b"original requested chat bytes"
    canary = b"benign root acquisition replacement canary"
    victim = output_dir / "nested" / "victim.txt"
    victim.parent.mkdir()
    victim.write_bytes(original)
    os.utime(victim, (1700000000, 1700000000))
    replacement_chat = tmp_path / "replacement-chat"
    replacement_outputs = replacement_chat / "outputs"
    (replacement_outputs / "nested").mkdir(parents=True)
    (replacement_outputs / "nested" / "victim.txt").write_bytes(canary)
    selected = output_dir.parent if replaced_root == "chat-root" else output_dir
    redirect = replacement_chat if replaced_root == "chat-root" else replacement_outputs
    chat_stat = output_dir.parent.stat()
    real_open = builtins.open
    real_os_open = os.open
    swaps = []

    def swap_at_acquisition(boundary):
        selected.rename(tmp_path / "parked-root")
        selected.symlink_to(redirect, target_is_directory=True)
        swaps.append(boundary)
        record_property("acquisition_boundary", boundary)

    def open_then_acquire(path, *args, **kwargs):
        if not swaps and not isinstance(path, int) and os.fspath(path) == str(victim):
            swap_at_acquisition("builtins.open(file)")
        return real_open(path, *args, **kwargs)

    def os_open_then_acquire(path, flags, *args, **kwargs):
        if not swaps:
            pathname = os.fsdecode(path)
            is_selected_root = pathname == str(selected)
            if replaced_root == "output-root" and pathname == "outputs":
                parent_fd = kwargs.get("dir_fd")
                if parent_fd is not None:
                    parent_stat = os.fstat(parent_fd)
                    is_selected_root = (
                        parent_stat.st_dev == chat_stat.st_dev
                        and parent_stat.st_ino == chat_stat.st_ino
                    )
            if is_selected_root:
                swap_at_acquisition("os.open(root)")
            elif pathname == str(victim):
                swap_at_acquisition("os.open(file)")
        return real_os_open(path, flags, *args, **kwargs)

    # Baseline AnyIO uses builtins.open; descriptor acquisition uses os.open.
    # Both wrappers retain the genuine acquisition, including its no-follow flags.
    monkeypatch.setattr(builtins, "open", open_then_acquire)
    monkeypatch.setattr(os, "open", os_open_then_acquire)
    response = _request_file(
        client, response_body_hooks, method, "nested/victim.txt", download
    )

    assert len(swaps) == 1, "No real root/file acquisition filesystem swap was observed"
    assert selected.is_symlink()
    _assert_confined_file(response, original, canary, method)


@pytest.mark.parametrize("method", ("GET", "HEAD"))
@pytest.mark.parametrize("link_kind", ("outside-leaf", "chat-root", "output-root"))
@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_file_rejects_preexisting_outside_or_sibling_root_links(
    client, output_dir, tmp_path, response_body_hooks, link_kind, download, method
):
    canary = b"benign static outside or sibling root canary"
    sibling_chat = tmp_path / "sibling-chat"
    sibling_outputs = sibling_chat / "outputs"
    sibling_outputs.mkdir(parents=True)
    target = sibling_outputs / "victim.txt"
    target.write_bytes(canary)
    if link_kind == "outside-leaf":
        (output_dir / "victim.txt").symlink_to(target)
    elif link_kind == "chat-root":
        output_dir.parent.rename(tmp_path / "parked-chat")
        output_dir.parent.symlink_to(sibling_chat, target_is_directory=True)
    else:
        output_dir.rename(output_dir.parent / "parked-outputs")
        output_dir.symlink_to(sibling_outputs, target_is_directory=True)

    response = _request_file(
        client, response_body_hooks, method, "victim.txt", download
    )

    assert canary not in response.content
    assert response.status_code in (403, 404)
    _assert_headers_absent(response)
    for header in ("etag", "last-modified", "content-disposition"):
        assert header not in response.headers
    if method == "HEAD":
        assert response.content == b""


@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_file_safe_in_root_alias_preserves_target_mime_and_disposition(
    client, output_dir, download
):
    contents = b"<p>safe in-root target</p>"
    target = output_dir / "nested" / "target.html"
    target.parent.mkdir()
    target.write_bytes(contents)
    (output_dir / "alias.txt").symlink_to(target)

    response = client.get(
        _file_url("alias.txt"),
        headers=_auth_headers(),
        params={"download": 1} if download else None,
    )

    _assert_target_response(response, "text/html", contents, download)
    disposition = "attachment" if download else "inline"
    assert response.headers["content-disposition"] == f'{disposition}; filename="target.html"'


# Literal validators below use the fixed mtime and each fixture's byte length.
@pytest.mark.parametrize("method", ("GET", "HEAD"))
@pytest.mark.parametrize(
    "contents, expected_length, expected_etag",
    (
        (b"", "0", '"e7d53fbff452d442c477af2510b0e0b0"'),
        (
            b"0123456789abcdef" * (128 * 1024) + b"tail",
            "2097156",
            '"69c1476782e5783776d96540a5a7d719"',
        ),
    ),
    ids=("zero-byte", "multi-chunk"),
)
@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_file_zero_byte_and_multi_chunk_responses_preserve_bytes_and_metadata(
    client, output_dir, response_body_hooks,
    contents, expected_length, expected_etag, download, method
):
    target = output_dir / "nested" / "payload.txt"
    target.parent.mkdir()
    target.write_bytes(contents)
    os.utime(target, (1700000000, 1700000000))

    response = _request_file(
        client, response_body_hooks, method, "nested/payload.txt", download
    )

    assert response.status_code == 200
    assert response.content == (contents if method == "GET" else b"")
    assert response.headers["content-length"] == expected_length
    assert response.headers["etag"] == expected_etag
    assert response.headers["last-modified"] == "Tue, 14 Nov 2023 22:13:20 GMT"
    assert response.headers.get_list("cache-control") == ["no-store"]
    expected_mime = "application/octet-stream" if download else "text/plain; charset=utf-8"
    assert response.headers["content-type"] == expected_mime
    if download:
        assert response.headers["content-disposition"] == 'attachment; filename="payload.txt"'
    else:
        assert "content-disposition" not in response.headers
    _assert_headers_absent(response)


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


def test_archive_bounds_outstanding_source_payload(client, output_dir, monkeypatch):
    from tests.test_file_response import _FileIO

    expected = {
        "nested/first.bin": b"a" * (3 * 1024 * 1024) + b"\x00first tail",
        "nested/second.bin": b"b" * (3 * 1024 * 1024) + b"\x00second tail",
        "nested/empty.bin": b"",
    }
    for name, payload in expected.items():
        target = output_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    sentinel = output_dir.parents[1] / "sentinel.txt"
    sentinel.write_bytes(b"sentinel descriptor remains owned by the test")
    allowance = 2 * 1024 * 1024
    observed = {
        name: {"read": 0, "written": 0, "reads": 0, "outstanding": 0, "peak": 0}
        for name in expected
    }

    class PayloadIO(_FileIO):
        def __init__(self, patch, name):
            self.member_name = name
            super().__init__(patch, output_dir / name, sentinel)

        def read(self, fd, size, operation):
            payload = super().read(fd, size, operation)
            state = observed[self.member_name]
            state["read"] += len(payload)
            state["reads"] += bool(payload)
            state["outstanding"] += len(payload)
            state["peak"] = max(state["peak"], state["outstanding"])
            return payload

    real_zip_open = zipfile.ZipFile.open

    def observe_member_open(archive, name, mode="r", *args, **kwargs):
        member = real_zip_open(archive, name, mode, *args, **kwargs)
        member_name = name.filename if isinstance(name, zipfile.ZipInfo) else name
        if mode == "w" and member_name in observed:
            real_write = member.write

            def observe_write(payload):
                accepted = real_write(payload)
                state = observed[member_name]
                state["written"] += accepted
                state["outstanding"] -= accepted
                assert state["outstanding"] >= 0, "ZIP consumed unobserved source bytes"
                return accepted

            transfer_patch.setattr(member, "write", observe_write)
        return member

    observers = []
    with monkeypatch.context() as transfer_patch:
        try:
            for name in expected:
                observers.append(PayloadIO(transfer_patch, name))
            transfer_patch.setattr(zipfile.ZipFile, "open", observe_member_open)

            response = client.get(_file_url("archive"), headers=_auth_headers())

            assert response.status_code == 200
            with zipfile.ZipFile(BytesIO(response.content)) as archive:
                assert sorted(archive.namelist()) == sorted(expected)
                for name, payload in expected.items():
                    assert archive.read(name) == payload
            for observer in observers:
                assert observer.acquired, "Source descriptor observation was bypassed"
                observer.assert_closed()
            for name, payload in expected.items():
                state = observed[name]
                assert state["read"] == len(payload), "Source read observation was bypassed"
                assert state["written"] == len(payload), "ZIP write observation was bypassed"
                assert state["outstanding"] == 0
                if payload:
                    assert state["reads"] > 1, "Expected genuine multi-chunk source reads"
                assert state["peak"] <= allowance, (
                    f"{name}: {state['peak']} source bytes outstanding before ZIP writes; "
                    f"allowance is {allowance}"
                )
        finally:
            for observer in reversed(observers):
                observer.cleanup()


def test_archive_missing_source_read_returns_404_and_closes_descriptor(
    client, output_dir, monkeypatch
):
    from tests.test_file_response import _FileIO

    victim = output_dir / "lost.bin"
    victim.write_bytes(b"real source acquired before injected read loss")
    sentinel = output_dir.parents[1] / "sentinel.txt"
    sentinel.write_bytes(b"sentinel descriptor remains owned by the test")

    class MissingSourceIO(_FileIO):
        def read(self, fd, size, operation):
            with self.lock:
                self.read_sizes.append(size)
                self.active_reads.add(fd)
            self.read_entered.set()
            self.read_faults.append(fd)
            try:
                raise FileNotFoundError("injected source read loss")
            finally:
                with self.lock:
                    self.active_reads.remove(fd)
                self.read_finished.set()

    observer = MissingSourceIO(monkeypatch, victim, sentinel)
    try:
        try:
            response = client.get(_file_url("archive"), headers=_auth_headers())
        finally:
            assert observer.acquired, "Source descriptor observation was bypassed"
            assert observer.read_faults == observer.acquired
            observer.assert_closed()
        assert response.status_code == 404
        assert response.json() == {"detail": "File not found: lost.bin"}
    finally:
        observer.cleanup()


@pytest.mark.parametrize(
    "boundary,error_type",
    [
        ("source", OSError),
        ("open", OSError),
        ("open", FileNotFoundError),
        ("write", OSError),
        ("write", FileNotFoundError),
        ("close", OSError),
        ("close", FileNotFoundError),
    ],
)
def test_archive_transfer_failure_retires_source_without_success(
    client, output_dir, monkeypatch, boundary, error_type
):
    from tests.test_file_response import BoundaryFailure, _FileIO

    victim = output_dir / "payload.bin"
    payload = b"real source payload\x00" * (128 * 1024)
    victim.write_bytes(payload)
    sentinel = output_dir.parents[1] / "sentinel.txt"
    sentinel.write_bytes(b"sentinel descriptor remains owned by the test")
    source_bytes = 0

    class FailingSourceIO(_FileIO):
        def read(self, fd, size, operation):
            nonlocal source_bytes
            chunk = super().read(fd, size, operation)
            source_bytes += len(chunk)
            if boundary == "source" and chunk:
                self.fail_read = True
            return chunk

    observer = FailingSourceIO(monkeypatch, victim, sentinel)
    failure = error_type("injected ZIP boundary failure")
    faults = []
    real_zip_open = zipfile.ZipFile.open

    def observe_member_open(archive, name, mode="r", *args, **kwargs):
        member = real_zip_open(archive, name, mode, *args, **kwargs)
        if mode != "w":
            return member
        if boundary == "open":
            member.close()
            faults.append(boundary)
            raise failure
        if boundary == "write":
            real_write = member.write

            def fail_write(payload):
                real_write(payload)
                faults.append(boundary)
                raise failure

            monkeypatch.setattr(member, "write", fail_write)
        elif boundary == "close":
            real_close = member.close

            def fail_close():
                real_close()
                if not faults:
                    faults.append(boundary)
                    raise failure

            monkeypatch.setattr(member, "close", fail_close)
        return member

    try:
        monkeypatch.setattr(zipfile.ZipFile, "open", observe_member_open)
        with pytest.raises(error_type) as raised:
            client.get(_file_url("archive"), headers=_auth_headers())
        assert observer.acquired, "Source descriptor observation was bypassed"
        if boundary == "source":
            assert isinstance(raised.value, BoundaryFailure)
            assert observer.read_faults == observer.acquired
            assert 0 < source_bytes < len(payload), "Fault must follow partial source transfer"
        else:
            assert raised.value is failure, "ZIP failure was translated or replaced"
            assert faults == [boundary]
            if boundary != "open":
                assert observer.read_sizes, "ZIP fault did not follow a source read"
        observer.assert_closed()
    finally:
        observer.cleanup()


def test_archive_leaf_replacement_during_transfer_keeps_acquired_bytes(
    client, output_dir, monkeypatch
):
    from tests.test_file_response import _FileIO

    original = b"original pinned bytes\x00" * (160 * 1024)
    replacement = b"replacement must not enter the archive"
    victim = output_dir / "payload.bin"
    victim.write_bytes(original)
    saved = output_dir.parents[1] / "pinned-original.bin"
    sentinel = output_dir.parents[1] / "sentinel.txt"
    sentinel.write_bytes(b"sentinel descriptor remains owned by the test")
    read_bytes = 0
    reads = 0
    replaced = False

    class ReplacingIO(_FileIO):
        def read(self, fd, size, operation):
            nonlocal read_bytes, reads, replaced
            payload = super().read(fd, size, operation)
            read_bytes += len(payload)
            reads += bool(payload)
            if payload and not replaced:
                victim.rename(saved)
                victim.write_bytes(replacement)
                replaced = True
            return payload

    observer = ReplacingIO(monkeypatch, victim, sentinel)
    try:
        response = client.get(_file_url("archive"), headers=_auth_headers())
        assert response.status_code == 200
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            assert archive.namelist() == ["payload.bin"]
            assert archive.read("payload.bin") == original
        assert replaced
        assert read_bytes == len(original)
        assert reads > 1, "Replacement must precede subsequent genuine source reads"
        assert victim.read_bytes() == replacement
        assert saved.read_bytes() == original
        assert observer.acquired, "Source descriptor observation was bypassed"
        observer.assert_closed()
    finally:
        observer.cleanup()


def test_archive_growing_member_crosses_zip64_boundary_and_reads_to_eof(
    client, output_dir, monkeypatch
):
    from tests.test_file_response import _FileIO

    limit = 64 * 1024
    initial = b"a" * (32 * 1024)
    appended = b"b" * (96 * 1024) + b"\x00grown tail"
    victim = output_dir / "growing.bin"
    victim.write_bytes(initial)
    sentinel = output_dir.parents[1] / "sentinel.txt"
    sentinel.write_bytes(b"sentinel descriptor remains owned by the test")
    header_sizes = []
    read_bytes = 0
    reads = 0
    grown = False

    class GrowingIO(_FileIO):
        def read(self, fd, size, operation):
            nonlocal read_bytes, reads, grown
            payload = super().read(fd, size, operation)
            read_bytes += len(payload)
            reads += bool(payload)
            if payload and not grown:
                with self.real_open(victim, "ab") as writer:
                    writer.write(appended)
                grown = True
            return payload

    observer = GrowingIO(monkeypatch, victim, sentinel)
    real_zip_open = zipfile.ZipFile.open

    def observe_member_open(archive, name, mode="r", *args, **kwargs):
        member = real_zip_open(archive, name, mode, *args, **kwargs)
        if mode == "w":
            header_sizes.append(observer.real_fstat(observer.acquired[-1]).st_size)
        return member

    try:
        monkeypatch.setattr(zipfile, "ZIP64_LIMIT", limit)
        monkeypatch.setattr(zipfile.ZipFile, "open", observe_member_open)
        response = client.get(_file_url("archive"), headers=_auth_headers())
        assert response.status_code == 200
        assert header_sizes == [len(initial)]
        assert header_sizes[0] < limit < len(initial) + len(appended)
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            assert archive.namelist() == ["growing.bin"]
            assert archive.read("growing.bin") == initial + appended
        assert grown
        assert reads > 1, "Growth must be consumed by a subsequent genuine source read"
        assert read_bytes == len(initial) + len(appended)
        assert victim.read_bytes() == initial + appended
        assert observer.acquired, "Source descriptor observation was bypassed"
        observer.assert_closed()
    finally:
        observer.cleanup()


@pytest.mark.parametrize("filename", ("archive", "%61rchive"), ids=("literal", "encoded"))
@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_reserved_archive_head_does_not_serve_ordinary_file(
    client, output_dir, filename, download
):
    marker = b"ordinary file named archive must remain an archive member"
    (output_dir / "archive").write_bytes(marker)
    url = _file_url(filename)
    params = {"download": 1} if download else None

    get_response = client.get(url, headers=_auth_headers(), params=params)

    assert get_response.status_code == 200
    _assert_confined_archive(get_response, {"archive": marker}, ())

    alias = output_dir / "archive-alias.bin"
    alias.symlink_to(output_dir / "archive")
    alias_url = _file_url(alias.name)
    alias_get = client.get(alias_url, headers=_auth_headers(), params=params)
    alias_head = client.head(alias_url, headers=_auth_headers(), params=params)

    assert alias_get.status_code == 200
    assert alias_get.content == marker
    assert alias_get.headers.get_list("cache-control") == ["no-store"]
    _assert_headers_absent(alias_get)
    assert alias_head.status_code == 200
    assert alias_head.content == b""
    for header in (
        "content-type", "content-length", "etag", "last-modified",
        "content-disposition", "cache-control",
    ):
        assert alias_head.headers.get_list(header) == alias_get.headers.get_list(header)
    _assert_headers_absent(alias_head)

    head_response = client.head(url, headers=_auth_headers(), params=params)

    assert head_response.status_code == 405
    assert head_response.content == b""
    assert head_response.headers.get_list("allow") == ["GET"]
    for header in ("etag", "last-modified", "content-disposition"):
        assert header not in head_response.headers
    assert head_response.headers.get_list("cache-control") == []
    _assert_headers_absent(head_response)


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


def _assert_confined_archive(response, expected, canaries, optional=()):
    if response.status_code != 200:
        assert 400 <= response.status_code < 500
        return

    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == f"attachment; filename=chat-{CHAT}-outputs.zip"
    assert response.headers.get_list("cache-control") == []
    _assert_headers_absent(response)
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        members = [(entry.filename, archive.read(entry)) for entry in archive.infolist()]
    for name, contents in members:
        for canary in canaries:
            assert canary not in contents, f"Archive member {name!r} disclosed a canary"
        assert name in expected, f"Unexpected archive member {name!r}"
        assert contents == expected[name]
    names = [name for name, _ in members]
    assert len(names) == len(set(names))
    assert set(expected) - set(optional) <= set(names)


@pytest.mark.parametrize(
    "link_kind", ("external-canary", "sibling-canary", "internal-leaflink", "directorylink")
)
def test_archive_does_not_follow_workspace_links(client, output_dir, tmp_path, link_kind):
    expected = {"nested/ordinary.txt": b"exact ordinary archived bytes"}
    ordinary = output_dir / "nested" / "ordinary.txt"
    ordinary.parent.mkdir()
    ordinary.write_bytes(expected["nested/ordinary.txt"])
    canary = b"benign archive link target canary"

    if link_kind == "internal-leaflink":
        target = output_dir / "internal.txt"
        expected["internal.txt"] = b"ordinary internal target bytes"
        target.write_bytes(expected["internal.txt"])
    elif link_kind == "sibling-canary":
        target = tmp_path / "sibling-chat" / "outputs" / "canary.txt"
        target.parent.mkdir(parents=True)
        target.write_bytes(canary)
    else:
        target = tmp_path / "outside-outputs" / "canary.txt"
        target.parent.mkdir()
        target.write_bytes(canary)

    if link_kind == "directorylink":
        (output_dir / "linked-directory").symlink_to(target.parent, target_is_directory=True)
    else:
        (output_dir / "linked.txt").symlink_to(target)

    response = client.get(_file_url("archive"), headers=_auth_headers())

    _assert_confined_archive(response, expected, (canary,))


@pytest.mark.parametrize("aliased_root", ("chat-root", "output-root"))
def test_archive_does_not_follow_preexisting_sibling_root_alias(
    client, output_dir, tmp_path, aliased_root
):
    expected = {"nested/ordinary.txt": b"exact ordinary archived bytes"}
    canary = b"benign sibling root alias canary"
    ordinary = output_dir / "nested" / "ordinary.txt"
    ordinary.parent.mkdir()
    ordinary.write_bytes(expected["nested/ordinary.txt"])
    sibling_chat = tmp_path / "sibling-chat"
    sibling_outputs = sibling_chat / "outputs"
    (sibling_outputs / "nested").mkdir(parents=True)
    (sibling_outputs / "nested" / "ordinary.txt").write_bytes(expected["nested/ordinary.txt"])
    (sibling_outputs / "canary.txt").write_bytes(canary)

    if aliased_root == "chat-root":
        output_dir.parent.rename(tmp_path / "parked-chat")
        output_dir.parent.symlink_to(sibling_chat, target_is_directory=True)
    else:
        output_dir.rename(output_dir.parent / "parked-outputs")
        output_dir.symlink_to(sibling_outputs, target_is_directory=True)

    response = client.get(_file_url("archive"), headers=_auth_headers())

    _assert_confined_archive(response, expected, (canary,))


@pytest.mark.parametrize("replaced_path", ("leaf", "ancestor", "chat-root", "output-root"))
def test_archive_does_not_read_symlink_replacement_after_selection(
    client, output_dir, tmp_path, monkeypatch, replaced_path
):
    expected = {
        "ordinary.txt": b"exact ordinary archived bytes",
        "nested/victim.txt": b"original selected regular file bytes",
    }
    for name, contents in expected.items():
        path = output_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    canary = b"benign selection to open replacement canary"
    replacement_chat = tmp_path / "replacement-chat"
    replacement_outputs = replacement_chat / "outputs"
    for name in expected:
        path = replacement_outputs / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canary)

    if replaced_path == "leaf":
        selected = output_dir / "nested" / "victim.txt"
        replacement = replacement_outputs / "nested" / "victim.txt"
    elif replaced_path == "ancestor":
        selected = output_dir / "nested"
        replacement = replacement_outputs / "nested"
    elif replaced_path == "chat-root":
        selected = output_dir.parent
        replacement = replacement_chat
    else:
        selected = output_dir
        replacement = replacement_outputs

    original_rglob = Path.rglob
    enumeration_root = output_dir.resolve()
    swaps = []

    def rglob_then_swap(path, pattern, *args, **kwargs):
        yield from original_rglob(path, pattern, *args, **kwargs)
        if path in (output_dir, enumeration_root) and pattern == "*" and not swaps:
            selected.rename(tmp_path / "parked-selected")
            selected.symlink_to(replacement, target_is_directory=replaced_path != "leaf")
            swaps.append(selected)

    # Exhausting the genuine iterator selects baseline candidates before the swap.
    monkeypatch.setattr(Path, "rglob", rglob_then_swap)
    response = client.get(_file_url("archive"), headers=_auth_headers())

    assert swaps == [selected], "Selection/open filesystem swap did not fire exactly once"
    assert selected.is_symlink()
    _assert_confined_archive(response, expected, (canary,), optional=("nested/victim.txt",))


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
