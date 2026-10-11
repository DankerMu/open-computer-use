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
            "file_response",
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
def response_start_hooks():
    return {}


@pytest.fixture
def client(app_module, output_dir, monkeypatch, response_start_hooks):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(app_module, "BASE_DATA_DIR", output_dir.parents[1])
    import docker_manager

    monkeypatch.setattr(docker_manager, "BASE_DATA_DIR", output_dir.parents[1])
    import uploads

    monkeypatch.setattr(uploads, "BASE_DATA_DIR", output_dir.parents[1])

    file_route = next(
        route for route in app_module.app.routes
        if getattr(route, "path", None) == "/files/{chat_id}/{filename:path}"
    )
    real_route_app = file_route.app

    async def route_with_send_hook(scope, receive, send):
        async def send_with_hook(message):
            if scope["type"] == "http" and message["type"] == "http.response.start":
                hook = response_start_hooks.get(scope["path"])
                if hook is not None:
                    hook()
            await send(message)

        await real_route_app(scope, receive, send_with_hook)

    # Observe the response before the HTTP normalizer can buffer response-start.
    monkeypatch.setattr(file_route, "app", route_with_send_hook)
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


def _assert_confined_file(response, original, canary):
    assert canary not in response.content
    if response.status_code == 200:
        assert response.content == original
        assert response.headers.get_list("cache-control") == ["no-store"]
    else:
        assert response.status_code in (403, 404)
        _assert_headers_absent(response)


@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_file_ancestor_replacement_cannot_redirect_to_outside_bytes(
    client, output_dir, tmp_path, response_start_hooks, download
):
    original = b"original in-root response bytes"
    canary = b"benign outside response-start canary"
    nested = output_dir / "nested"
    nested.mkdir()
    (nested / "victim.txt").write_bytes(original)
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
    response = client.get(
        _file_url("nested/victim.txt"),
        headers=_auth_headers(),
        params={"download": 1} if download else None,
    )

    assert swaps == [nested], "Actual response-start filesystem swap did not fire"
    assert nested.is_symlink()
    _assert_confined_file(response, original, canary)


@pytest.mark.parametrize("replaced_path", ("leaf", "ancestor"))
@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_file_response_keeps_original_bytes_and_metadata_after_path_replacement(
    client, output_dir, tmp_path, response_start_hooks, replaced_path, download
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
    response = client.get(
        _file_url("nested/victim.txt"),
        headers=_auth_headers(),
        params={"download": 1} if download else None,
    )

    assert swaps == [selected], "Actual response-start filesystem swap did not fire"
    assert selected.is_symlink()
    assert response.status_code == 200
    assert response.content == original
    # Literal validators identify the original 25-byte object at the fixed mtime.
    assert response.headers["content-length"] == "25"
    assert response.headers["etag"] == '"3f2348dd2177119a98a73b4ce6735534"'
    assert response.headers["last-modified"] == "Tue, 14 Nov 2023 22:13:20 GMT"
    assert response.headers.get_list("cache-control") == ["no-store"]


@pytest.mark.parametrize("replaced_root", ("chat-root", "output-root"))
@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_file_root_replacement_at_acquisition_cannot_redirect_bytes(
    client, output_dir, tmp_path, monkeypatch, record_property, replaced_root, download
):
    original = b"original requested chat bytes"
    canary = b"benign root acquisition replacement canary"
    victim = output_dir / "nested" / "victim.txt"
    victim.parent.mkdir()
    victim.write_bytes(original)
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
    response = client.get(
        _file_url("nested/victim.txt"),
        headers=_auth_headers(),
        params={"download": 1} if download else None,
    )

    assert len(swaps) == 1, "No real root/file acquisition filesystem swap was observed"
    assert selected.is_symlink()
    _assert_confined_file(response, original, canary)


@pytest.mark.parametrize("link_kind", ("outside-leaf", "chat-root", "output-root"))
@pytest.mark.parametrize("download", (False, True), ids=("inline", "download"))
def test_file_rejects_preexisting_outside_or_sibling_root_links(
    client, output_dir, tmp_path, link_kind, download
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

    response = client.get(
        _file_url("victim.txt"),
        headers=_auth_headers(),
        params={"download": 1} if download else None,
    )

    assert canary not in response.content
    assert response.status_code in (403, 404)
    _assert_headers_absent(response)


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
    client, output_dir, contents, expected_length, expected_etag, download
):
    target = output_dir / "nested" / "payload.txt"
    target.parent.mkdir()
    target.write_bytes(contents)
    os.utime(target, (1700000000, 1700000000))

    response = client.get(
        _file_url("nested/payload.txt"),
        headers=_auth_headers(),
        params={"download": 1} if download else None,
    )

    assert response.status_code == 200
    assert response.content == contents
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
