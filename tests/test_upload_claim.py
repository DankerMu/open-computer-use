# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""HTTP upload publication preserves occupied names and complete bytes."""
import asyncio
import datetime
import errno
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parents[1] / "computer-use-server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

CHAT = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
TOKEN = "ocu-upload-claim-test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
APP_MODULES = {
    "app", "auth_guard", "ws_recheck", "mcp_tools", "docker_manager",
    "outputs_broker", "context_vars", "security", "system_prompt",
    "skill_manager", "cli_runtime", "uploads", "docs_html",
    "file_response", "descriptor_io",
    "office", "office.config", "office.router", "office.store",
    "office.versions", "office.workspace", "office.publish",
    "office.save_as", "office.epoch", "office.tokens",
    "office.resolution",
    "office.history",
    "office.restore",
    "office.commands", "office.sessions", "office.notice",
    "office.ooxml", "office.sweep", "office.control_plane",
    "office.download", "office.callback",
}


def _environment(data):
    return {
        "OCU_INTERNAL_TOKEN": TOKEN, "MCP_API_KEY": "ocu-upload-claim-mcp-key",
        "OCU_WEBUI_ORIGIN": "https://webui.example", "SINGLE_USER_MODE": "true",
        "OCU_SANDBOX_SUBNET": "10.90.0.0/24", "PUBLIC_BASE_URL": "http://ocu.example",
        "BASE_DATA_DIR": str(data),
        "DOCKER_HOST": "unix:///tmp/ocu-acceptance-no-docker.sock",
        "DOCKER_SOCKET": "unix:///tmp/ocu-acceptance-no-docker.sock",
    }


@pytest.fixture
def upload_server(tmp_path, monkeypatch):
    data = tmp_path / "data"
    snapshot = {name: value for name, value in sys.modules.items()
                if name in APP_MODULES or name.startswith("mcp_resources")}
    for key, value in _environment(data).items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("OCU_PUBLIC_PREFIX", raising=False)
    for name in snapshot:
        sys.modules.pop(name, None)
    try:
        import app
        with TestClient(app.app) as client:
            yield client, data / CHAT / "outputs"

    finally:
        for name in list(sys.modules):
            if name in APP_MODULES or name.startswith("mcp_resources"):
                sys.modules.pop(name, None)
        sys.modules.update(snapshot)


def _upload(client, name, body, attachment_id=None):
    headers = HEADERS if attachment_id is None else {
        **HEADERS, "X-OCU-Attachment-Id": attachment_id,
    }
    return client.post(f"/api/uploads/{CHAT}/{name}", headers=headers,
                       files={"file": ("attachment", body)})


def _imports(client):
    return client.get(f"/api/uploads/{CHAT}/imports", headers=HEADERS)


def _receipts(uploads):
    return json.loads((uploads.parent / ".ocu" / "imports.json").read_text())


def test_collision_preserves_original_and_reports_relative_stored_name(upload_server):
    client, uploads = upload_server
    first = _upload(client, "nested/report.txt", b"old")
    second = _upload(client, "nested/report.txt", b"new")
    assert first.status_code == second.status_code == 200
    assert second.json() == {"status": "success", "filename": "nested/report (2).txt",
                             "size": 3, "md5": "22af645d1859cb5ca6da0c484f1f37ea"}
    assert (uploads / "nested/report.txt").read_bytes() == b"old"
    assert (uploads / "nested/report (2).txt").read_bytes() == b"new"
    assert sorted(path.name for path in (uploads / "nested").iterdir()) == [
        "report (2).txt", "report.txt",
    ]
    response = client.get(f"/files/{CHAT}/archive", headers=HEADERS)
    assert response.status_code == 200
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {"nested/report.txt", "nested/report (2).txt"}
        assert archive.read("nested/report.txt") == b"old"
        assert archive.read("nested/report (2).txt") == b"new"


def test_committed_upload_reports_real_discovery_overflow_without_losing_receipt(
        upload_server):
    from pydantic import AnyUrl

    client, uploads = upload_server
    seeded = _upload(client, "report.txt", b"old", attachment_id="F-before")
    assert seeded.status_code == 200
    assert "discovery" not in seeded.json()
    import mcp_tools

    before = {
        str(resource.uri)
        for resource in asyncio.run(mcp_tools.mcp.list_resources())
    }
    assert before == {f"file://uploads/{CHAT}/report.txt"}
    for index in range(9_999):
        (uploads / f"occupied-{index:05d}.txt").write_bytes(b"occupied")

    response = _upload(client, "report.txt", b"new", attachment_id="F-overflow")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert {key: payload[key] for key in ("status", "filename", "size", "md5")} == {
        "status": "success", "filename": "report (2).txt", "size": 3,
        "md5": "22af645d1859cb5ca6da0c484f1f37ea",
    }
    assert (uploads / "report.txt").read_bytes() == b"old"
    assert (uploads / "report (2).txt").read_bytes() == b"new"
    receipts = _receipts(uploads)
    assert receipts["F-before"]["stored_name"] == "report.txt"
    assert receipts["F-overflow"]["stored_name"] == "report (2).txt"
    assert _imports(client).json() == {"ids": ["F-before", "F-overflow"]}
    after = {
        str(resource.uri)
        for resource in asyncio.run(mcp_tools.mcp.list_resources())
    }
    assert after == before
    chunks = list(asyncio.run(mcp_tools.mcp.read_resource(
        AnyUrl(f"file://uploads/{CHAT}/report.txt"))))
    assert chunks[0].content == "old"
    assert payload.get("discovery") == {"status": "failed", "reason": "file_limit"}


@pytest.mark.parametrize("kind", ("ascii", "multibyte", "last-suffix"))
def test_native_name_limit_collision_preserves_longest_prefix_and_receipt(
        upload_server, kind):
    client, uploads = upload_server
    destination = uploads / "nested"
    destination.mkdir(parents=True)
    limit = os.pathconf(destination, "PC_NAME_MAX")
    if kind == "ascii":
        name = "a" * limit
        expected = "a" * (limit - 4) + " (2)"
    elif kind == "multibyte":
        name = "界" * ((limit - 4) // 3) + "a" * ((limit - 4) % 3) + ".txt"
        expected = "界" * ((limit - 8) // 3) + " (2).txt"
    else:
        name = "a" * (limit - 7) + ".tar.gz"
        expected = "a" * (limit - 7) + " (2).gz"
    body = b"complete-collision-payload" * 4096
    assert _upload(client, "nested/" + name, b"original", "F1").status_code == 200
    response = _upload(client, "nested/" + name, body, "F2")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "success", "filename": "nested/" + expected,
        "size": len(body), "md5": hashlib.md5(body).hexdigest(),
    }
    assert len(os.fsencode(expected)) <= limit
    assert (destination / name).read_bytes() == b"original"
    assert (destination / expected).read_bytes() == body
    assert {path.name for path in destination.iterdir()} == {name, expected}
    assert _receipts(uploads)["F2"]["stored_name"] == "nested/" + expected


def test_native_multibyte_collision_rebudgets_number_ten(upload_server):
    client, uploads = upload_server
    uploads.mkdir(parents=True)
    limit = os.pathconf(uploads, "PC_NAME_MAX")
    leading = "a" * ((limit - 4) % 3)
    prefix = leading + "界" * ((limit - 4) // 3)
    name = prefix + "界"
    (uploads / name).write_bytes(b"original")
    occupied = {prefix + f" ({number})" for number in range(2, 10)}
    for candidate in occupied:
        (uploads / candidate).write_bytes(b"occupied")
    expected = leading + "界" * (((limit - 4) // 3) - 1) + " (10)"
    response = _upload(client, name, b"tenth payload")
    assert response.status_code == 200, response.text
    assert response.json()["filename"] == expected
    assert (uploads / expected).read_bytes() == b"tenth payload"
    assert (uploads / name).read_bytes() == b"original"
    assert all((uploads / candidate).read_bytes() == b"occupied" for candidate in occupied)
    assert {path.name for path in uploads.iterdir()} == occupied | {name, expected}


@pytest.mark.parametrize("minimum_fits", (True, False), ids=("underscore", "impossible-suffix"))
def test_native_suffix_budget_preserves_original_and_cleans_private_stage(
        upload_server, minimum_fits):
    client, uploads = upload_server
    uploads.mkdir(parents=True)
    limit = os.pathconf(uploads, "PC_NAME_MAX")
    suffix = "." + "x" * (limit - (6 if minimum_fits else 5))
    name = "界" + suffix
    assert _upload(client, name, b"original", "F1").status_code == 200
    receipts_before = _receipts(uploads)
    response = _upload(client, name, b"new payload", "F2")
    assert response.status_code == (200 if minimum_fits else 400), response.text
    assert (uploads / name).read_bytes() == b"original"
    if minimum_fits:
        expected = "_ (2)" + suffix
        assert response.json()["filename"] == expected
        assert len(os.fsencode(expected)) == limit
        assert (uploads / expected).read_bytes() == b"new payload"
        assert {path.name for path in uploads.iterdir()} == {name, expected}
        assert _receipts(uploads)["F2"]["stored_name"] == expected
    else:
        assert {path.name for path in uploads.iterdir()} == {name}
        assert _receipts(uploads) == receipts_before
        assert _imports(client).json() == {"ids": ["F1"]}
    assert not list((uploads.parent / ".ocu").glob(".upload-*"))


@pytest.mark.parametrize("limit", (0, -1, "lookup-error"), ids=("zero", "indeterminate", "lookup-error"))
def test_unknown_target_name_limit_fails_without_publication_or_receipt(
        upload_server, monkeypatch, limit):
    client, uploads = upload_server
    destination = uploads / "nested"
    destination.mkdir(parents=True)
    assert _upload(client, "nested/report.txt", b"original", "F1").status_code == 200
    receipts_before = _receipts(uploads)
    target = destination.stat()
    query = os.fpathconf
    queried = []

    def unknown(fd, key):
        if key == "PC_NAME_MAX":
            info = os.fstat(fd)
            queried.append((info.st_dev, info.st_ino))
            if limit == "lookup-error":
                raise OSError("target name limit unavailable")
            return limit
        return query(fd, key)

    monkeypatch.setattr(os, "fpathconf", unknown)
    response = _upload(client, "nested/report.txt", b"new", "F2")
    assert response.status_code == 500, response.text
    assert set(queried) == {(target.st_dev, target.st_ino)}
    assert (destination / "report.txt").read_bytes() == b"original"
    assert {path.name for path in destination.iterdir()} == {"report.txt"}
    assert _receipts(uploads) == receipts_before
    assert not list((uploads.parent / ".ocu").glob(".upload-*"))


def test_reimported_attachment_id_keeps_edited_file_and_reports_original_metadata(
        upload_server):
    client, uploads = upload_server
    original = b"imported-original"
    edited = b"user-edited-bytes-longer"
    first = _upload(client, "brief.docx", original, attachment_id="F1")
    assert first.status_code == 200
    assert first.json() == {"status": "success", "filename": "brief.docx",
                            "size": 17, "md5": "d75428910d5a127b64d0164ef3da01ff"}
    (uploads / "brief.docx").write_bytes(edited)
    second = _upload(client, "brief.docx", original, attachment_id="F1")
    assert second.status_code == 200
    assert second.json() == {"status": "success", "filename": "brief.docx",
                             "size": 17, "md5": "d75428910d5a127b64d0164ef3da01ff"}
    assert (uploads / "brief.docx").read_bytes() == edited
    assert sorted(path.name for path in uploads.iterdir()) == ["brief.docx"]


@pytest.mark.parametrize("mutation", ["rename", "delete"])
def test_reimported_attachment_id_does_not_resurrect_renamed_or_deleted_file(
        upload_server, mutation):
    client, uploads = upload_server
    original = b"imported-original"
    first = _upload(client, "brief.docx", original, attachment_id="F1")
    assert first.status_code == 200
    stored = uploads / "brief.docx"
    if mutation == "rename":
        stored.rename(uploads / "final.docx")
    else:
        stored.unlink()
    second = _upload(client, "brief.docx", original, attachment_id="F1")
    assert second.status_code == 200
    assert second.json() == {"status": "success", "filename": "brief.docx",
                             "size": 17, "md5": "d75428910d5a127b64d0164ef3da01ff"}
    assert not stored.exists()
    if mutation == "rename":
        assert (uploads / "final.docx").read_bytes() == original
        assert sorted(path.name for path in uploads.iterdir()) == ["final.docx"]
    else:
        assert sorted(path.name for path in uploads.iterdir()) == []


def test_distinct_attachment_ids_with_same_name_are_deduplicated(upload_server):
    client, uploads = upload_server
    first = _upload(client, "brief.docx", b"one", attachment_id="F1")
    second = _upload(client, "brief.docx", b"two", attachment_id="F2")
    assert first.status_code == second.status_code == 200
    assert first.json() == {"status": "success", "filename": "brief.docx",
                            "size": 3, "md5": "f97c5d29941bfb1b2fdab0874906ab82"}
    assert second.json() == {"status": "success", "filename": "brief (2).docx",
                             "size": 3, "md5": "b8a9f715dbb64fd5c56e7783c6820a61"}
    assert (uploads / "brief.docx").read_bytes() == b"one"
    assert (uploads / "brief (2).docx").read_bytes() == b"two"
    receipts = _receipts(uploads)
    assert receipts["F1"]["stored_name"] == "brief.docx"
    assert receipts["F2"]["stored_name"] == "brief (2).docx"
    listed = _imports(client)
    assert listed.status_code == 200 and set(listed.json()["ids"]) == {"F1", "F2"}


def test_headerless_upload_does_not_create_or_mutate_receipts(upload_server):
    client, uploads = upload_server
    headerless = _upload(client, "photo.png", b"png-bytes")
    assert headerless.status_code == 200
    assert headerless.json() == {"status": "success", "filename": "photo.png",
                                 "size": 9, "md5": "e8c0e28b42bd2f48ea34ccdc6593f88a"}
    assert (uploads / "photo.png").read_bytes() == b"png-bytes"
    assert not (uploads.parent / ".ocu" / "imports.json").exists()
    first = _upload(client, "brief.docx", b"one", attachment_id="F1")
    assert first.status_code == 200
    receipts = _receipts(uploads)
    second = _upload(client, "photo.png", b"png-bytes")
    assert second.status_code == 200
    assert second.json()["filename"] == "photo (2).png"
    assert _receipts(uploads) == receipts
    listed = _imports(client)
    assert listed.status_code == 200 and listed.json() == {"ids": ["F1"]}


def test_receipts_live_outside_uploads_and_record_original_metadata(upload_server):
    client, uploads = upload_server
    response = _upload(client, "brief.docx", b"imported-original", attachment_id="F1")
    assert response.status_code == 200
    receipts = _receipts(uploads)
    assert set(receipts) == {"F1"}
    record = receipts["F1"]
    assert record["stored_name"] == "brief.docx"
    assert record["size"] == 17
    assert record["md5"] == "d75428910d5a127b64d0164ef3da01ff"
    datetime.datetime.fromisoformat(record["imported_at"].replace("Z", "+00:00"))
    assert not (uploads / "imports.json").exists()
    assert not (uploads / ".ocu").exists()
    assert (uploads.parent / ".ocu" / "imports.json").is_file()


def test_published_upload_is_writable_at_claim_and_preserves_no_overwrite(
        upload_server, monkeypatch):
    import app

    client, uploads = upload_server
    previous_umask = os.umask(0o077)
    try:
        original_claim = app.claim_file_no_replace
        claims = []
        staging_modes = []
        opened = os.open

        def recording_claim(temporary, *, src_dir_fd, dst_dir_fd, requested_name):
            info = os.stat(temporary, dir_fd=src_dir_fd)
            reader = opened(temporary, os.O_RDONLY | os.O_CLOEXEC, dir_fd=src_dir_fd)
            try:
                payload = os.read(reader, info.st_size)
            finally:
                os.close(reader)
            claims.append({
                "bytes": payload,
                "mode": stat.S_IMODE(info.st_mode),
                "ino": info.st_ino,
                "requested": requested_name,
            })
            return original_claim(
                temporary,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
                requested_name=requested_name,
            )

        def observing_open(path, flags, mode=0o777, *args, **kwargs):
            fd = opened(path, flags, mode, *args, **kwargs)
            name = os.path.basename(os.fspath(path))
            if name.startswith(".upload-"):
                staging_modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
            return fd

        monkeypatch.setattr(app, "claim_file_no_replace", recording_claim)
        monkeypatch.setattr(os, "open", observing_open)

        payloads = (b"first", b"second")
        expected_names = ("report.txt", "report (2).txt")
        for payload, name in zip(payloads, expected_names):
            response = _upload(client, "report.txt", payload)
            assert response.status_code == 200
            assert response.json()["filename"] == name
            path = uploads / name
            published = path.stat()
            assert path.read_bytes() == payload
            assert stat.S_IMODE(published.st_mode) == 0o666
            assert published.st_ino == claims[-1]["ino"]

        assert [entry["bytes"] for entry in claims] == [b"first", b"second"]
        assert [entry["mode"] for entry in claims] == [0o666, 0o666]
        assert [entry["requested"] for entry in claims] == [
            "report.txt", "report.txt",
        ]
        assert (uploads / "report.txt").read_bytes() == b"first"
        assert (uploads / "report (2).txt").read_bytes() == b"second"
        assert sorted(path.name for path in uploads.iterdir()) == [
            "report (2).txt", "report.txt",
        ]
        assert staging_modes
        assert all(mode & 0o077 == 0 for mode in staging_modes)
    finally:
        os.umask(previous_umask)


def test_upload_creates_writable_parents_and_preserves_existing_directory_modes(
        upload_server):
    client, uploads = upload_server
    previous_umask = os.umask(0o077)
    try:
        response = _upload(client, "nested/deep/notes.txt", b"nested")
        assert response.status_code == 200
        assert response.json()["filename"] == "nested/deep/notes.txt"
        assert (uploads / "nested/deep/notes.txt").read_bytes() == b"nested"
        assert stat.S_IMODE(uploads.stat().st_mode) == 0o777
        assert stat.S_IMODE((uploads / "nested").stat().st_mode) == 0o777
        assert stat.S_IMODE((uploads / "nested/deep").stat().st_mode) == 0o777

        kept = uploads / "kept"
        kept.mkdir()
        os.chmod(kept, 0o750)
        into_kept = _upload(client, "kept/file.txt", b"inside")
        assert into_kept.status_code == 200
        assert into_kept.json()["filename"] == "kept/file.txt"
        assert (kept / "file.txt").read_bytes() == b"inside"
        assert stat.S_IMODE(kept.stat().st_mode) == 0o750
    finally:
        os.umask(previous_umask)


@pytest.mark.parametrize("headroom", (None, 3, 2, 1, 0),
                         ids=("short", "near-limit-3", "near-limit-2", "near-limit-1", "limit"))
def test_free_name_preserves_response_fields_and_outputs_destination(
        upload_server, monkeypatch, headroom):
    client, uploads = upload_server
    uploads.mkdir(parents=True)
    limit = min(255, os.pathconf(uploads, "PC_NAME_MAX"))
    name = "brief.docx" if headroom is None else "a" * (limit - headroom - 5) + ".docx"

    def unavailable(fd, key):
        raise OSError("free original must not need a collision budget")

    monkeypatch.setattr(os, "fpathconf", unavailable)
    response = _upload(client, name, b"new")
    assert response.status_code == 200
    assert response.json() == {"status": "success", "filename": name,
                               "size": 3, "md5": "22af645d1859cb5ca6da0c484f1f37ea"}
    assert (uploads / name).read_bytes() == b"new"
    assert sorted(path.name for path in uploads.iterdir()) == [name]
    assert not (uploads.parent / "uploads").exists()


def test_uploaded_file_appears_in_outputs_listing_with_file_id(upload_server):
    client, uploads = upload_server
    body = b"PK\x03\x04brief"
    response = _upload(client, "brief.docx", body)
    assert response.status_code == 200
    assert response.json()["filename"] == "brief.docx"
    assert (uploads / "brief.docx").read_bytes() == body
    listed = client.get(f"/api/outputs/{CHAT}", headers=HEADERS)
    assert listed.status_code == 200
    files = listed.json()["files"]
    match = next(entry for entry in files if entry["path"] == "brief.docx")
    assert match["file_id"]


def test_colliding_uploads_have_distinct_listing_identities(upload_server):
    client, uploads = upload_server
    first = _upload(client, "report.docx", b"first-bytes")
    second = _upload(client, "report.docx", b"second-bytes")
    assert first.status_code == second.status_code == 200
    assert first.json()["filename"] == "report.docx"
    assert second.json()["filename"] == "report (2).docx"
    assert (uploads / "report.docx").read_bytes() == b"first-bytes"
    assert (uploads / "report (2).docx").read_bytes() == b"second-bytes"
    listed = client.get(f"/api/outputs/{CHAT}", headers=HEADERS)
    assert listed.status_code == 200
    by_path = {entry["path"]: entry for entry in listed.json()["files"]}
    assert by_path["report.docx"]["file_id"]
    assert by_path["report (2).docx"]["file_id"]
    assert by_path["report.docx"]["file_id"] != by_path["report (2).docx"]["file_id"]


def test_uploaded_html_is_served_with_generated_content_isolation(upload_server):
    client, uploads = upload_server
    html = b"<script>window.active = true</script>"
    response = _upload(client, "page.html", html)
    assert response.status_code == 200
    assert (uploads / "page.html").read_bytes() == html
    served = client.get(f"/files/{CHAT}/page.html", headers=HEADERS)
    assert served.status_code == 200
    assert served.content == html
    assert served.headers.get_list("content-security-policy") == [
        "sandbox allow-scripts allow-forms",
    ]
    assert served.headers["x-content-type-options"] == "nosniff"



@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
def test_all_occupied_entries_advance_to_next_free_number(upload_server, kind):
    client, uploads = upload_server
    uploads.mkdir(parents=True)
    original = uploads / "report.txt"
    target = uploads.parent / "untouched.txt"
    target.write_bytes(b"original")
    if kind == "file":
        original.write_bytes(b"original")
    elif kind == "directory":
        original.mkdir()
        (original / "child").write_bytes(b"original")
    else:
        original.symlink_to(target)
    (uploads / "report (2).txt").symlink_to("missing-target")
    response = _upload(client, "report.txt", b"new")
    assert response.status_code == 200
    assert response.json()["filename"] == "report (3).txt"
    assert (uploads / "report (3).txt").read_bytes() == b"new"
    assert target.read_bytes() == b"original"
    if kind == "file":
        assert original.read_bytes() == b"original"
    elif kind == "directory":
        assert (original / "child").read_bytes() == b"original"
    else:
        assert original.is_symlink() and original.readlink() == target
    assert (uploads / "report (2).txt").readlink() == Path("missing-target")
    assert not (uploads / "missing-target").exists()
    assert sorted(path.name for path in uploads.iterdir()) == [
        "report (2).txt", "report (3).txt", "report.txt",
    ]


@pytest.mark.parametrize("near_limit", (False, True), ids=("short", "bounded-symlink-race"))
def test_unlocked_writer_wins_claim_without_losing_either_payload(
        upload_server, monkeypatch, near_limit):
    client, uploads = upload_server
    uploads.mkdir(parents=True)
    limit = os.pathconf(uploads, "PC_NAME_MAX")
    name = "a" * (limit - 4) + ".txt" if near_limit else "report.txt"
    prefix = "a" * (limit - 8) if near_limit else "report"
    second, third, fourth = (prefix + f" ({number}).txt" for number in (2, 3, 4))
    body = b"new" * (100_000 if near_limit else 1)
    winner = third if near_limit else name
    expected = fourth if near_limit else second
    if near_limit:
        (uploads / name).write_bytes(b"original")
        target = uploads.parent / "untouched.txt"
        target.write_bytes(b"symlink target")
        (uploads / second).symlink_to(target)
    link = os.link
    attempted = []

    def competing_link(source, destination, *args, **kwargs):
        src_dir_fd = kwargs.get("src_dir_fd")
        dst_dir_fd = kwargs.get("dst_dir_fd")
        source_name = os.path.basename(os.fspath(source))
        dest_name = os.path.basename(os.fspath(destination))
        assert source_name.startswith(".upload-")
        if src_dir_fd is not None:
            info = os.stat(source, dir_fd=src_dir_fd)
            reader = os.open(source, os.O_RDONLY | os.O_CLOEXEC, dir_fd=src_dir_fd)
            try:
                payload = os.read(reader, info.st_size)
            finally:
                os.close(reader)
            assert payload == body
        else:
            assert Path(source).read_bytes() == body
        with (uploads.parent / ".lifecycle.lock").open("a+") as lock_file:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        attempted.append(dest_name)
        if dest_name == winner:
            if dst_dir_fd is not None:
                competitor = os.open(
                    dest_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                    0o666,
                    dir_fd=dst_dir_fd,
                )
                try:
                    os.write(competitor, b"unlocked writer")
                finally:
                    os.close(competitor)
            else:
                destination_path = Path(destination)
                assert not destination_path.exists()
                destination_path.write_bytes(b"unlocked writer")
        return link(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "link", competing_link)
    response = _upload(client, name, body)
    assert response.status_code == 200, response.text
    assert response.json()["filename"] == expected
    assert attempted == ([name, second, third, fourth] if near_limit else [name, second])
    assert (uploads / winner).read_bytes() == b"unlocked writer"
    assert (uploads / expected).read_bytes() == body
    if near_limit:
        assert (uploads / name).read_bytes() == b"original"
        assert (uploads / second).is_symlink()
        assert (uploads / second).readlink() == target
        assert target.read_bytes() == b"symlink target"
    assert {path.name for path in uploads.iterdir()} == set(attempted)


@pytest.mark.parametrize("name", ["%2e%2e/outside.txt", "nested/%2e%2e/outside.txt", "escape/outside.txt"])
def test_traversal_and_external_directory_symlink_write_nothing(upload_server, name):
    client, uploads = upload_server
    uploads.mkdir(parents=True)
    outside = uploads.parent / "outside.txt"
    outside.write_bytes(b"untouched")
    (uploads / "escape").symlink_to(uploads.parent, target_is_directory=True)
    response = _upload(client, name, b"new")
    assert 400 <= response.status_code < 500
    assert outside.read_bytes() == b"untouched"
    assert sorted(path.name for path in uploads.iterdir()) == ["escape"]
    assert (uploads / "escape").readlink() == uploads.parent


@pytest.mark.parametrize("failure", ["write", "claim"])
def test_failed_upload_removes_partial_temporary_and_preserves_original(upload_server, monkeypatch, failure):
    client, uploads = upload_server
    uploads.mkdir(parents=True)
    (uploads / "report.txt").write_bytes(b"original")
    if failure == "claim":
        def failed_link(*args, **kwargs):
            raise PermissionError("injected claim failure")
        monkeypatch.setattr(os, "link", failed_link)
    else:
        written = os.write

        def failed_write(fd, data):
            written(fd, data[:1])
            raise OSError("injected partial write failure")

        monkeypatch.setattr(os, "write", failed_write)
    response = _upload(client, "report.txt", b"new")
    assert response.status_code == 500
    assert (uploads / "report.txt").read_bytes() == b"original"
    leftover = [
        path.name for path in uploads.iterdir() if path.name.startswith(".upload-")
    ]
    assert leftover == []
    control = uploads.parent / ".ocu"
    if control.is_dir():
        leftover_control = [
            path.name for path in control.iterdir() if path.name.startswith(".upload-")
        ]
        assert leftover_control == []
    assert sorted(path.name for path in uploads.iterdir()) == ["report.txt"]


def _assert_concurrent_payloads(results, uploads):
    assert {name for name, _ in results} == {"data.xlsx", "data (2).xlsx"}
    for name, body in results:
        assert (uploads / name).read_bytes() == body
    assert sorted(path.name for path in uploads.iterdir()) == ["data (2).xlsx", "data.xlsx"]


def test_concurrent_threads_report_their_own_complete_files(upload_server):
    client, uploads = upload_server
    barrier = threading.Barrier(2)

    def send(body):
        barrier.wait(timeout=10)
        response = _upload(client, "data.xlsx", body)
        assert response.status_code == 200
        return response.json()["filename"], body

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, [b"A" * 100_000, b"B" * 100_000]))
    _assert_concurrent_payloads(results, uploads)


_WORKER_UPLOAD = r'''
import json, os, sys, time
from pathlib import Path
from fastapi.testclient import TestClient
sys.path.insert(0, os.environ["OCU_SERVER_DIR"])
import app
shared = Path(os.environ["OCU_SHARED"])
(shared / ("ready-" + str(os.getpid()))).touch()
deadline = time.monotonic() + 15
while not (shared / "go").exists():
    if time.monotonic() > deadline:
        raise RuntimeError("upload worker barrier timed out")
    time.sleep(0.005)
body = os.environ["OCU_PAYLOAD"].encode() * 100000
headers = {"Authorization": "Bearer " + os.environ["OCU_INTERNAL_TOKEN"]}
attachment_id = os.environ.get("OCU_ATTACHMENT_ID")
if attachment_id:
    headers["X-OCU-Attachment-Id"] = attachment_id
with TestClient(app.app) as client:
    response = client.post("/api/uploads/" + os.environ["OCU_CHAT"] + "/data.xlsx",
        headers=headers,
        files={"file": ("data.xlsx", body)})
assert response.status_code == 200, response.text
print(json.dumps(response.json()))
'''


def test_concurrent_server_processes_report_their_own_complete_files(tmp_path):
    data = tmp_path / "data"
    shared = tmp_path / "barrier"
    shared.mkdir()
    env = {**os.environ, **_environment(data), "OCU_SERVER_DIR": str(SERVER_DIR),
           "OCU_SHARED": str(shared), "OCU_CHAT": CHAT}
    env.pop("OCU_PUBLIC_PREFIX", None)
    processes = [subprocess.Popen([sys.executable, "-c", _WORKER_UPLOAD],
                 env={**env, "OCU_PAYLOAD": payload}, stdout=subprocess.PIPE,
                 stderr=subprocess.PIPE, text=True) for payload in ("A", "B")]
    try:
        deadline = time.monotonic() + 20
        while len(list(shared.glob("ready-*"))) != 2:
            assert all(process.poll() is None for process in processes)
            assert time.monotonic() < deadline, "workers did not reach upload barrier"
            time.sleep(0.01)
        (shared / "go").touch()
        outputs = [process.communicate(timeout=30) for process in processes]
        assert [process.returncode for process in processes] == [0, 0], outputs
        results = [(json.loads(stdout.splitlines()[-1])["filename"], body * 100_000)
                   for (stdout, _), body in zip(outputs, [b"A", b"B"])]
        _assert_concurrent_payloads(results, data / CHAT / "outputs")

    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait()


def test_concurrent_server_processes_store_one_file_for_the_same_attachment_id(tmp_path):
    data = tmp_path / "data"
    shared = tmp_path / "barrier"
    shared.mkdir()
    env = {**os.environ, **_environment(data), "OCU_SERVER_DIR": str(SERVER_DIR),
           "OCU_SHARED": str(shared), "OCU_CHAT": CHAT, "OCU_ATTACHMENT_ID": "F1"}
    env.pop("OCU_PUBLIC_PREFIX", None)
    processes = [subprocess.Popen([sys.executable, "-c", _WORKER_UPLOAD],
                 env={**env, "OCU_PAYLOAD": payload}, stdout=subprocess.PIPE,
                 stderr=subprocess.PIPE, text=True) for payload in ("A", "B")]
    try:
        deadline = time.monotonic() + 20
        while len(list(shared.glob("ready-*"))) != 2:
            assert all(process.poll() is None for process in processes)
            assert time.monotonic() < deadline, "workers did not reach upload barrier"
            time.sleep(0.01)
        (shared / "go").touch()
        outputs = [process.communicate(timeout=30) for process in processes]
        assert [process.returncode for process in processes] == [0, 0], outputs
        names = [json.loads(stdout.splitlines()[-1])["filename"]
                 for stdout, _ in outputs]
        assert names == ["data.xlsx", "data.xlsx"]
        uploads = data / CHAT / "outputs"

        assert sorted(path.name for path in uploads.iterdir()) == ["data.xlsx"]
        assert (uploads / "data.xlsx").read_bytes() in {b"A" * 100_000, b"B" * 100_000}
        receipts = json.loads((data / CHAT / ".ocu" / "imports.json").read_text())
        assert set(receipts) == {"F1"}
        assert receipts["F1"]["stored_name"] == "data.xlsx"
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait()


def test_concurrent_distinct_attachment_ids_do_not_lose_a_receipt(upload_server):
    client, uploads = upload_server
    barrier = threading.Barrier(2)

    def send(item):
        attachment_id, body = item
        barrier.wait(timeout=10)
        response = _upload(client, "notes.txt", body, attachment_id=attachment_id)
        assert response.status_code == 200
        return attachment_id, response.json()["filename"], body

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, [("F1", b"first"), ("F2", b"second")]))
    names = {name for _, name, _ in results}
    assert names == {"notes.txt", "notes (2).txt"}
    receipts = _receipts(uploads)
    assert set(receipts) == {"F1", "F2"}
    for attachment_id, name, body in results:
        assert receipts[attachment_id]["stored_name"] == name
        assert (uploads / name).read_bytes() == body
    listed = _imports(client)
    assert listed.status_code == 200 and set(listed.json()["ids"]) == {"F1", "F2"}


def test_imports_of_unknown_chat_return_empty_ids_without_creating_directory(
        upload_server, tmp_path):
    client, _ = upload_server
    unknown = "c3d4e5f6-a7b8-9012-cdef-123456789012"
    chat_dir = tmp_path / "data" / unknown
    assert not chat_dir.exists()
    response = client.get(f"/api/uploads/{unknown}/imports", headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {"ids": []}
    assert not chat_dir.exists()


def test_corrupt_receipts_fail_explicitly_without_reimport(upload_server):
    client, uploads = upload_server
    first = _upload(client, "brief.docx", b"one", attachment_id="F1")
    assert first.status_code == 200
    path = uploads.parent / ".ocu" / "imports.json"
    path.write_text("{not-json")
    second = _upload(client, "brief.docx", b"two", attachment_id="F1")
    assert second.status_code == 500
    assert (uploads / "brief.docx").read_bytes() == b"one"
    assert sorted(path.name for path in uploads.iterdir()) == ["brief.docx"]
    listed = _imports(client)
    assert listed.status_code == 500
    assert path.read_text() == "{not-json"


@pytest.mark.parametrize("kind", ["dangling", "live", "control"])
def test_symlink_receipt_path_fails_explicitly_without_following_or_recreating(
        upload_server, kind):
    client, uploads = upload_server
    first = _upload(client, "brief.docx", b"one", attachment_id="F1")
    assert first.status_code == 200
    chat_dir = uploads.parent
    control = chat_dir / ".ocu"
    receipt = control / "imports.json"
    original_receipts = receipt.read_bytes()
    decoy = chat_dir / "receipt-decoy.json"
    decoy_bytes = (
        b'{"F9":{"imported_at":"2026-01-01T00:00:00Z",'
        b'"md5":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","size":3,'
        b'"stored_name":"decoy.txt"}}'
    )
    decoy.write_bytes(decoy_bytes)
    original_target = None
    if kind == "dangling":
        receipt.unlink()
        receipt.symlink_to(chat_dir / "missing-imports.json")
        original_target = receipt.readlink()
    elif kind == "live":
        receipt.unlink()
        receipt.symlink_to(decoy)
        original_target = receipt.readlink()
    else:
        control.rename(chat_dir / "ocu-real")
        control.symlink_to("ocu-real")
        original_target = control.readlink()
    listed = _imports(client)
    second = _upload(client, "brief.docx", b"two", attachment_id="F1")
    assert listed.status_code == second.status_code == 500
    assert (uploads / "brief.docx").read_bytes() == b"one"
    assert sorted(path.name for path in uploads.iterdir()) == ["brief.docx"]
    assert control.is_symlink() is (kind == "control")
    if kind == "control":
        assert control.readlink() == original_target
        real_receipt = chat_dir / "ocu-real" / "imports.json"
        assert real_receipt.is_file() and not real_receipt.is_symlink()
        assert real_receipt.read_bytes() == original_receipts
    else:
        assert receipt.is_symlink()
        assert receipt.readlink() == original_target
    assert decoy.read_bytes() == decoy_bytes


def test_failed_receipt_write_leaves_prior_receipts_and_retries_without_overwrite(
        upload_server, monkeypatch):
    import app

    client, uploads = upload_server
    first = _upload(client, "brief.docx", b"one", attachment_id="F1")
    assert first.status_code == 200
    prior = _receipts(uploads)
    original = app.write_import_receipts

    def fail_once(chat_dir, receipts):
        raise OSError("injected receipt write failure")

    monkeypatch.setattr(app, "write_import_receipts", fail_once)
    failed = _upload(client, "notes.txt", b"two", attachment_id="F2")
    assert failed.status_code == 500
    assert _receipts(uploads) == prior
    stored = sorted(path.name for path in uploads.iterdir())
    assert stored == ["brief.docx", "notes.txt"]
    monkeypatch.setattr(app, "write_import_receipts", original)
    retry = _upload(client, "notes.txt", b"two", attachment_id="F2")
    assert retry.status_code == 200
    assert retry.json()["filename"] == "notes (2).txt"
    assert (uploads / "notes.txt").read_bytes() == b"two"
    assert (uploads / "notes (2).txt").read_bytes() == b"two"
    receipts = _receipts(uploads)
    assert receipts["F1"] == prior["F1"]
    assert receipts["F2"]["stored_name"] == "notes (2).txt"


def _is_receipt_temporary(path, control):
    path = Path(path)
    return (path.parent == control and path.name.startswith(".imports.")
            and path.name.endswith(".tmp"))


def test_receipt_replace_and_temporary_cleanup_failure_reports_replace_failure(
        upload_server, monkeypatch):
    client, uploads = upload_server
    first = _upload(client, "brief.docx", b"one", attachment_id="F1")
    assert first.status_code == 200
    control = uploads.parent / ".ocu"
    destination = control / "imports.json"
    prior = _receipts(uploads)
    prior_bytes = destination.read_bytes()
    replaced, unlink = os.replace, Path.unlink
    replace_error = OSError(errno.EIO, "receipt replace refused")
    refused = []

    def failing_replace(src, dst, *args, **kwargs):
        if Path(dst) == destination:
            raise replace_error
        return replaced(src, dst, *args, **kwargs)

    def refusing_unlink(self, *args, **kwargs):
        if _is_receipt_temporary(self, control):
            refused.append(self)
            raise PermissionError(errno.EACCES, "cleanup refused")
        return unlink(self, *args, **kwargs)

    with monkeypatch.context() as patches:
        patches.setattr(os, "replace", failing_replace)
        patches.setattr(Path, "unlink", refusing_unlink)
        failed = _upload(client, "notes.txt", b"two", attachment_id="F2")
    assert failed.status_code == 500
    detail = failed.json()["detail"]
    assert detail.startswith("Failed to save file:")
    assert "receipt replace refused" in detail
    assert "cleanup refused" not in detail
    assert len(refused) == 1
    assert destination.read_bytes() == prior_bytes
    assert _receipts(uploads) == prior
    assert _imports(client).json() == {"ids": ["F1"]}


def test_receipt_final_close_failure_reports_committed_upload_and_replays_same_id(
        upload_server, monkeypatch):
    client, uploads = upload_server
    destination = uploads.parent / ".ocu" / "imports.json"
    original_replace, original_fsync, original_close = os.replace, os.fsync, os.close
    host_marker = "/private/issue365-receipt-close-host-marker"
    close_error = OSError(errno.EIO, host_marker)
    control_info = None
    writer_fd = None
    observations = {"events": [], "close_attempts": [], "retired": False}

    def recording_replace(src, dst, *args, **kwargs):
        nonlocal control_info
        result = original_replace(src, dst, *args, **kwargs)
        if Path(dst) == destination:
            control_info = destination.parent.stat()
            observations["events"].append("replace")
        return result

    def recording_fsync(fd):
        nonlocal writer_fd
        result = original_fsync(fd)
        if control_info is not None and _same_inode(os.fstat(fd), control_info):
            writer_fd = fd
            observations["events"].append("fsync")
            observations["synchronized_successor"] = _receipts(uploads)
        return result

    def close_then_fail(fd):
        if writer_fd is None or fd != writer_fd:
            return original_close(fd)
        observations["close_attempts"].append(fd)
        assert _same_inode(os.fstat(fd), control_info)
        original_close(fd)
        with pytest.raises(OSError) as retired:
            os.fstat(fd)
        assert retired.value.errno == errno.EBADF
        observations["retired"] = True
        observations["events"].append("close")
        raise close_error

    try:
        with monkeypatch.context() as patches:
            patches.setattr(os, "replace", recording_replace)
            patches.setattr(os, "fsync", recording_fsync)
            patches.setattr(os, "close", close_then_fail)
            failed = _upload(client, "brief.docx", b"one", attachment_id="F1")
        assert writer_fd is not None
        assert observations["events"] == ["replace", "fsync", "close"]
        assert observations["close_attempts"] == [writer_fd]
        assert observations["retired"] is True
        with pytest.raises(OSError) as retired:
            os.fstat(writer_fd)
        assert retired.value.errno == errno.EBADF
        successor_bytes = destination.read_bytes()
        successor = _receipts(uploads)
        imported_at = successor["F1"]["imported_at"]
        expected = {"F1": {
            "stored_name": "brief.docx", "size": 3,
            "md5": "f97c5d29941bfb1b2fdab0874906ab82", "imported_at": imported_at,
        }}
        assert observations["synchronized_successor"] == expected
        assert successor == expected
        assert (uploads / "brief.docx").read_bytes() == b"one"
        assert sorted(path.name for path in uploads.iterdir()) == ["brief.docx"]
        assert sorted(path.name for path in destination.parent.iterdir()) == ["imports.json"]
        retry = _upload(client, "another.txt", b"replacement", attachment_id="F1")
        assert retry.status_code == 200, retry.text
        assert retry.json() == {
            "status": "success", "filename": "brief.docx", "size": 3,
            "md5": "f97c5d29941bfb1b2fdab0874906ab82",
        }
        assert destination.read_bytes() == successor_bytes
        assert (uploads / "brief.docx").read_bytes() == b"one"
        assert sorted(path.name for path in uploads.iterdir()) == ["brief.docx"]
        assert _imports(client).json() == {"ids": ["F1"]}
        assert failed.status_code == 500
        assert failed.json() == {"detail": {
            "reason": "committed_close_failed", "committed": True,
            "resource": "import_receipts",
        }}, observations
        assert host_marker not in failed.text
        assert str(uploads.parent) not in failed.text
    finally:
        if writer_fd is not None:
            try:
                current = os.fstat(writer_fd)
            except OSError:
                pass
            else:
                if _same_inode(current, control_info):
                    original_close(writer_fd)


def test_waiting_for_chat_lock_keeps_health_requests_responsive(upload_server):
    import app
    from docker_manager import _combined_lock

    _, uploads = upload_server
    held, release, expired = threading.Event(), threading.Event(), threading.Event()

    def hold_chat():
        with _combined_lock(CHAT):
            held.set()
            if not release.wait(timeout=5):
                expired.set()

    async def requests():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),
                                     base_url="http://test", trust_env=False) as client:
            upload = asyncio.create_task(client.post(
                f"/api/uploads/{CHAT}/waiting.txt", headers=HEADERS,
                files={"file": ("waiting.txt", b"new")}))
            await asyncio.sleep(0.05)
            health = await client.get("/health")
            assert health.status_code == 200 and health.json() == {"status": "healthy"}
            assert not expired.is_set() and not (uploads / "waiting.txt").exists()
            release.set()
            assert (await upload).status_code == 200
            assert (uploads / "waiting.txt").read_bytes() == b"new"
            assert sorted(path.name for path in uploads.iterdir()) == ["waiting.txt"]

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(hold_chat)
        assert held.wait(timeout=5)
        try:
            asyncio.run(requests())
        finally:
            release.set()
            holder.result(timeout=5)


def _same_inode(left, right):
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def test_upload_control_creation_syncs_chat_parent_before_return(tmp_path, monkeypatch):
    import descriptor_io
    import uploads

    chat_dir = tmp_path / CHAT
    chat_dir.mkdir()
    existing = chat_dir / "existing.txt"
    existing.write_bytes(b"pre-existing upload bytes")
    control = chat_dir / ".ocu"
    chat_fd = descriptor_io.open_directory(chat_dir)
    chat_info = os.fstat(chat_fd)
    control_fd = None
    fsync = os.fsync
    synced = []

    def recording_fsync(fd):
        info = os.fstat(fd)
        entry_present = control.is_dir()
        fsync(fd)
        synced.append((info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode),
                       entry_present))

    monkeypatch.setattr(os, "fsync", recording_fsync)
    previous_umask = os.umask(0o077)
    try:
        control_fd = uploads.open_control_directory(chat_fd)
        assert (chat_info.st_dev, chat_info.st_ino, stat.S_IFDIR, True) in synced
        assert _same_inode(os.fstat(control_fd), control.stat())
        assert stat.S_IMODE(os.fstat(control_fd).st_mode) == 0o700
        assert _same_inode(os.fstat(chat_fd), chat_info)
        assert existing.read_bytes() == b"pre-existing upload bytes"
    finally:
        os.umask(previous_umask)
        if control_fd is not None:
            os.close(control_fd)
        os.close(chat_fd)


@pytest.mark.parametrize("existing_control", (False, True), ids=("new", "existing"))
def test_upload_control_parent_sync_failure_preserves_descriptor_ownership(
        tmp_path, monkeypatch, existing_control):
    import descriptor_io
    import uploads

    chat_dir = tmp_path / CHAT
    chat_dir.mkdir()
    existing = chat_dir / "existing.txt"
    existing.write_bytes(b"pre-existing upload bytes")
    control = chat_dir / ".ocu"
    if existing_control:
        control.mkdir(mode=0o700)
        (control / "marker").write_bytes(b"pre-existing control bytes")
    chat_fd = descriptor_io.open_directory(chat_dir)
    chat_info = os.fstat(chat_fd)
    opened = os.open
    fsync = os.fsync
    owned = []
    attempted = []
    error = OSError(errno.EIO, "chat parent durability failure")

    def recording_open(*args, **kwargs):
        fd = opened(*args, **kwargs)
        info = os.fstat(fd)
        if control.is_dir() and _same_inode(info, control.stat()):
            owned.append((fd, info))
        return fd

    def failing_fsync(fd):
        info = os.fstat(fd)
        attempted.append((info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)))
        if _same_inode(info, chat_info):
            raise error
        return fsync(fd)

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(os, "fsync", failing_fsync)
    try:
        with pytest.raises(OSError) as failure:
            uploads.open_control_directory(chat_fd)
        assert failure.value is error
        assert failure.value.errno == errno.EIO
        assert (chat_info.st_dev, chat_info.st_ino, stat.S_IFDIR) in attempted
        assert _same_inode(os.fstat(chat_fd), chat_info)
        assert control.is_dir()
        assert stat.S_IMODE(control.stat().st_mode) == 0o700
        assert existing.read_bytes() == b"pre-existing upload bytes"
        if existing_control:
            assert (control / "marker").read_bytes() == b"pre-existing control bytes"
        else:
            assert list(control.iterdir()) == []
        for fd, info in owned:
            try:
                current = os.fstat(fd)
            except OSError as exc:
                assert exc.errno == errno.EBADF
            else:
                assert not _same_inode(current, info), "owned control descriptor leaked"
    finally:
        for fd, info in owned:
            try:
                current = os.fstat(fd)
            except OSError:
                continue
            if _same_inode(current, info):
                os.close(fd)
        os.close(chat_fd)


def test_upload_control_retry_requires_parent_sync_after_interrupted_creation(
        tmp_path, monkeypatch):
    import descriptor_io
    import uploads

    chat_dir = tmp_path / CHAT
    chat_dir.mkdir()
    control = chat_dir / ".ocu"
    chat_fd = descriptor_io.open_directory(chat_dir)
    chat_info = os.fstat(chat_fd)
    control_fd = None
    fsync = os.fsync
    synced = []
    fail_parent = True
    error = OSError(errno.EIO, "chat parent durability failure")

    def controlled_fsync(fd):
        info = os.fstat(fd)
        if fail_parent and _same_inode(info, chat_info):
            raise error
        fsync(fd)
        synced.append((info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)))

    monkeypatch.setattr(os, "fsync", controlled_fsync)
    try:
        with pytest.raises(OSError) as first_failure:
            control_fd = uploads.open_control_directory(chat_fd)
        assert first_failure.value is error
        control_info = control.stat()
        (control / "marker").write_bytes(b"surviving interrupted creation")

        with pytest.raises(OSError) as next_failure:
            control_fd = uploads.open_control_directory(chat_fd)
        assert next_failure.value is error
        assert _same_inode(control.stat(), control_info)
        assert _same_inode(os.fstat(chat_fd), chat_info)
        assert (control / "marker").read_bytes() == b"surviving interrupted creation"

        fail_parent = False
        control_fd = uploads.open_control_directory(chat_fd)
        assert (chat_info.st_dev, chat_info.st_ino, stat.S_IFDIR) in synced
        assert _same_inode(os.fstat(control_fd), control_info)
        assert stat.S_IMODE(os.fstat(control_fd).st_mode) == 0o700
        assert (control / "marker").read_bytes() == b"surviving interrupted creation"
    finally:
        if control_fd is not None:
            os.close(control_fd)
        os.close(chat_fd)


def test_upload_control_symlink_rejection_preserves_target_and_borrowed_parent(tmp_path):
    from fastapi import HTTPException
    import descriptor_io
    import uploads

    chat_dir = tmp_path / CHAT
    chat_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    outside_info = outside.stat()
    sentinel = outside / "marker"
    sentinel.write_bytes(b"outside control bytes")
    control = chat_dir / ".ocu"
    control.symlink_to(outside, target_is_directory=True)
    chat_fd = descriptor_io.open_directory(chat_dir)
    chat_info = os.fstat(chat_fd)
    control_fd = None
    try:
        with pytest.raises(HTTPException) as failure:
            control_fd = uploads.open_control_directory(chat_fd)
        assert failure.value.status_code == 403
        assert _same_inode(os.fstat(chat_fd), chat_info)
        assert control.is_symlink() and control.readlink() == outside
        assert sentinel.read_bytes() == b"outside control bytes"
        assert stat.S_IMODE(outside.stat().st_mode) == stat.S_IMODE(outside_info.st_mode)
        assert sorted(path.name for path in outside.iterdir()) == ["marker"]
    finally:
        if control_fd is not None:
            os.close(control_fd)
        os.close(chat_fd)


def test_direct_receipt_creation_syncs_chat_parent_and_persisted_state(
        tmp_path, monkeypatch):
    import uploads

    chat_dir = tmp_path / CHAT
    chat_dir.mkdir()
    chat_info = chat_dir.stat()
    existing = chat_dir / "existing.txt"
    existing.write_bytes(b"pre-existing upload bytes")
    control = chat_dir / ".ocu"
    destination = control / "imports.json"
    receipts = {"F1": {
        "stored_name": "brief.docx", "size": 3, "md5": "f97c5d29941bfb1b2fdab0874906ab82",
        "imported_at": "2026-10-11T00:00:00Z",
    }}
    fsync = os.fsync
    synced = []

    def recording_fsync(fd):
        info = os.fstat(fd)
        entry_present = control.is_dir()
        receipt_present = destination.exists()
        fsync(fd)
        synced.append((info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode),
                       entry_present, receipt_present))

    monkeypatch.setattr(os, "fsync", recording_fsync)
    previous_umask = os.umask(0o077)
    try:
        uploads.write_import_receipts(chat_dir, receipts)
    finally:
        os.umask(previous_umask)

    assert (chat_info.st_dev, chat_info.st_ino, stat.S_IFDIR, True, False) in synced
    control_info = control.stat()
    receipt_info = destination.stat()
    assert (receipt_info.st_dev, receipt_info.st_ino, stat.S_IFREG, True, False) in synced
    assert (control_info.st_dev, control_info.st_ino, stat.S_IFDIR, True, True) in synced
    assert stat.S_IMODE(control_info.st_mode) == 0o700
    assert destination.read_bytes() == (
        b'{"F1":{"imported_at":"2026-10-11T00:00:00Z",'
        b'"md5":"f97c5d29941bfb1b2fdab0874906ab82","size":3,"stored_name":"brief.docx"}}')
    assert uploads.read_import_receipts(chat_dir) == receipts
    assert existing.read_bytes() == b"pre-existing upload bytes"
    assert sorted(path.name for path in control.iterdir()) == ["imports.json"]


@pytest.mark.parametrize("existing_control", (False, True), ids=("new", "existing"))
def test_direct_receipt_parent_sync_failure_preserves_state_and_owned_descriptors(
        tmp_path, monkeypatch, existing_control):
    import uploads

    chat_dir = tmp_path / CHAT
    chat_dir.mkdir()
    chat_info = chat_dir.stat()
    existing = chat_dir / "existing.txt"
    existing.write_bytes(b"pre-existing upload bytes")
    control = chat_dir / ".ocu"
    destination = control / "imports.json"
    prior_bytes = (
        b'{"F0":{"stored_name":"prior.txt","size":5,"md5":"prior-md5",'
        b'"imported_at":"2026-10-10T00:00:00Z"}}\n')
    if existing_control:
        control.mkdir(mode=0o700)
        destination.write_bytes(prior_bytes)
        (control / "marker").write_bytes(b"pre-existing control bytes")
    receipts = {"F1": {
        "stored_name": "brief.docx", "size": 3, "md5": "f97c5d29941bfb1b2fdab0874906ab82",
        "imported_at": "2026-10-11T00:00:00Z",
    }}
    opened = os.open
    fsync = os.fsync
    owned = []
    attempted = []
    error = OSError(errno.EIO, "receipt chat parent durability failure")

    def recording_open(*args, **kwargs):
        fd = opened(*args, **kwargs)
        owned.append((fd, os.fstat(fd)))
        return fd

    def failing_fsync(fd):
        info = os.fstat(fd)
        attempted.append((info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode),
                          control.is_dir()))
        if _same_inode(info, chat_info):
            raise error
        return fsync(fd)

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(os, "fsync", failing_fsync)
    try:
        with pytest.raises(OSError) as failure:
            uploads.write_import_receipts(chat_dir, receipts)
        assert failure.value is error
        assert failure.value.errno == errno.EIO
        assert (chat_info.st_dev, chat_info.st_ino, stat.S_IFDIR, True) in attempted
        assert _same_inode(chat_dir.stat(), chat_info)
        assert control.is_dir()
        assert stat.S_IMODE(control.stat().st_mode) == 0o700
        assert existing.read_bytes() == b"pre-existing upload bytes"
        if existing_control:
            assert destination.read_bytes() == prior_bytes
            assert uploads.read_import_receipts(chat_dir) == {"F0": {
                "stored_name": "prior.txt", "size": 5, "md5": "prior-md5",
                "imported_at": "2026-10-10T00:00:00Z",
            }}
            assert (control / "marker").read_bytes() == b"pre-existing control bytes"
            assert sorted(path.name for path in control.iterdir()) == ["imports.json", "marker"]
        else:
            assert not destination.exists()
            assert list(control.iterdir()) == []
        for fd, info in owned:
            try:
                current = os.fstat(fd)
            except OSError as exc:
                assert exc.errno == errno.EBADF
            else:
                assert not _same_inode(current, info), "owned receipt descriptor leaked"
    finally:
        for fd, info in owned:
            try:
                current = os.fstat(fd)
            except OSError:
                continue
            if _same_inode(current, info):
                os.close(fd)


def test_direct_receipt_retry_requires_parent_sync_after_interrupted_creation(
        tmp_path, monkeypatch):
    import uploads

    chat_dir = tmp_path / CHAT
    chat_dir.mkdir()
    chat_info = chat_dir.stat()
    control = chat_dir / ".ocu"
    destination = control / "imports.json"
    receipts = {"F1": {
        "stored_name": "brief.docx", "size": 3, "md5": "f97c5d29941bfb1b2fdab0874906ab82",
        "imported_at": "2026-10-11T00:00:00Z",
    }}
    fsync = os.fsync
    synced = []
    fail_parent = True
    error = OSError(errno.EIO, "receipt chat parent durability failure")

    def controlled_fsync(fd):
        info = os.fstat(fd)
        if fail_parent and _same_inode(info, chat_info):
            raise error
        fsync(fd)
        synced.append((info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)))

    monkeypatch.setattr(os, "fsync", controlled_fsync)
    with pytest.raises(OSError) as first_failure:
        uploads.write_import_receipts(chat_dir, receipts)
    assert first_failure.value is error
    assert not destination.exists()
    control_info = control.stat()
    marker = control / "marker"
    marker.write_bytes(b"surviving interrupted receipt creation")

    with pytest.raises(OSError) as next_failure:
        uploads.write_import_receipts(chat_dir, receipts)
    assert next_failure.value is error
    assert _same_inode(control.stat(), control_info)
    assert not destination.exists()
    assert marker.read_bytes() == b"surviving interrupted receipt creation"
    assert sorted(path.name for path in control.iterdir()) == ["marker"]

    fail_parent = False
    uploads.write_import_receipts(chat_dir, receipts)
    assert (chat_info.st_dev, chat_info.st_ino, stat.S_IFDIR) in synced
    assert _same_inode(control.stat(), control_info)
    assert stat.S_IMODE(control.stat().st_mode) == 0o700
    assert uploads.read_import_receipts(chat_dir) == receipts
    assert marker.read_bytes() == b"surviving interrupted receipt creation"
    assert sorted(path.name for path in control.iterdir()) == ["imports.json", "marker"]


PRIOR_RECEIPTS = {"F0": {
    "stored_name": "prior.txt", "size": 5, "md5": "prior-md5",
    "imported_at": "2026-10-10T00:00:00Z",
}}
SUCCESSOR_RECEIPTS = {**PRIOR_RECEIPTS, "F1": {
    "stored_name": "brief.docx", "size": 3, "md5": "f97c5d29941bfb1b2fdab0874906ab82",
    "imported_at": "2026-10-11T00:00:00Z",
}}


@pytest.mark.parametrize("stage", ("write", "fsync", "replace"))
def test_direct_receipt_pre_commit_failure_retains_primary_over_cleanup_failure(
        tmp_path, monkeypatch, stage):
    import descriptor_io
    import uploads

    chat_dir = tmp_path / CHAT
    chat_dir.mkdir()
    control = chat_dir / ".ocu"
    destination = control / "imports.json"
    uploads.write_import_receipts(chat_dir, PRIOR_RECEIPTS)
    prior_bytes = destination.read_bytes()
    opened, written, synced, replaced = os.open, os.write, os.fsync, os.replace
    unlink = Path.unlink
    primary = OSError(errno.EIO, f"{stage} refused")
    cleanup_error = PermissionError(errno.EACCES, "cleanup refused")
    temporaries = {}
    refused = []

    def recording_open(path, *args, **kwargs):
        fd = opened(path, *args, **kwargs)
        if _is_receipt_temporary(path, control):
            temporaries[fd] = Path(path)
        return fd

    def failing_write(fd, data):
        if fd in temporaries:
            raise primary
        return written(fd, data)

    def failing_fsync(fd):
        if fd in temporaries:
            raise primary
        return synced(fd)

    def failing_replace(src, dst, *args, **kwargs):
        if Path(src) in temporaries.values() and Path(dst) == destination:
            raise primary
        return replaced(src, dst, *args, **kwargs)

    def refusing_unlink(self, *args, **kwargs):
        if _is_receipt_temporary(self, control):
            refused.append(self)
            raise cleanup_error
        return unlink(self, *args, **kwargs)

    failing = {"write": failing_write, "fsync": failing_fsync, "replace": failing_replace}
    with monkeypatch.context() as patches:
        patches.setattr(os, "open", recording_open)
        patches.setattr(os, stage, failing[stage])
        patches.setattr(Path, "unlink", refusing_unlink)
        with pytest.raises(OSError) as failure:
            uploads.write_import_receipts(chat_dir, SUCCESSOR_RECEIPTS)
    outcome = failure.value
    assert outcome is primary
    assert outcome.cleanup_errors == (cleanup_error,)
    assert outcome.__notes__ == [
        "Temporary receipt cleanup also failed: PermissionError: [Errno 13] cleanup refused"]
    assert not isinstance(outcome, descriptor_io.CommittedCloseError)
    assert len(temporaries) == 1
    temporary = next(iter(temporaries.values()))
    assert refused == [temporary]
    assert destination.read_bytes() == prior_bytes
    assert uploads.read_import_receipts(chat_dir) == PRIOR_RECEIPTS
    assert sorted(path.name for path in control.iterdir()) == sorted(
        ["imports.json", temporary.name])


def test_direct_receipt_pre_commit_failure_tolerates_missing_temporary(
        tmp_path, monkeypatch):
    import uploads

    chat_dir = tmp_path / CHAT
    chat_dir.mkdir()
    control = chat_dir / ".ocu"
    destination = control / "imports.json"
    uploads.write_import_receipts(chat_dir, PRIOR_RECEIPTS)
    prior_bytes = destination.read_bytes()
    replaced, unlink = os.replace, Path.unlink
    primary = OSError(errno.EIO, "replace refused")
    removed = []

    def failing_replace(src, dst, *args, **kwargs):
        if _is_receipt_temporary(src, control) and Path(dst) == destination:
            raise primary
        return replaced(src, dst, *args, **kwargs)

    def removing_unlink(self, *args, **kwargs):
        if _is_receipt_temporary(self, control):
            removed.append(self)
            unlink(self)
        return unlink(self, *args, **kwargs)

    with monkeypatch.context() as patches:
        patches.setattr(os, "replace", failing_replace)
        patches.setattr(Path, "unlink", removing_unlink)
        with pytest.raises(OSError) as failure:
            uploads.write_import_receipts(chat_dir, SUCCESSOR_RECEIPTS)
    outcome = failure.value
    assert outcome is primary
    assert not hasattr(outcome, "cleanup_errors")
    assert getattr(outcome, "__notes__", None) is None
    assert len(removed) == 1
    assert destination.read_bytes() == prior_bytes
    assert uploads.read_import_receipts(chat_dir) == PRIOR_RECEIPTS
    assert sorted(path.name for path in control.iterdir()) == ["imports.json"]


def _matches_path(target, args, kwargs, *, directory_keys=("dir_fd",)):
    try:
        expected = os.lstat(target)
    except FileNotFoundError:
        expected = None
    for key in directory_keys:
        dir_fd = kwargs.get(key)
        if dir_fd is None:
            continue
        try:
            observed = os.fstat(dir_fd)
        except OSError:
            continue
        if expected is not None and _same_inode(observed, expected):
            return True
    if not args:
        return False
    candidate = args[0]
    if not isinstance(candidate, (str, bytes, os.PathLike)):
        return False
    text = os.fspath(candidate)
    if os.path.isabs(text):
        try:
            return os.path.realpath(text) == os.path.realpath(str(target))
        except OSError:
            return False
    for key in directory_keys:
        dir_fd = kwargs.get(key)
        if dir_fd is None:
            continue
        try:
            parent = os.fstat(dir_fd)
            ancestor = os.lstat(Path(target).parent)
        except OSError:
            continue
        if _same_inode(parent, ancestor) and os.path.basename(text) == Path(target).name:
            return True
        if expected is not None and _same_inode(parent, expected):
            return True
    return False


def _swap_directory_for_outside(directory, backup, outside):
    if directory.is_symlink() or not directory.is_dir():
        return False
    directory.rename(backup)
    directory.symlink_to(outside, target_is_directory=True)
    return True


def _is_regular_file(path):
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return False
    return stat.S_ISREG(mode)


def test_upload_directory_walk_ancestor_swap_never_creates_outside_chat(
        upload_server, monkeypatch):
    client, uploads = upload_server
    outside = uploads.parent.parent / "outside-walk-tree"
    outside.mkdir(parents=True)
    sentinel = outside / "keep.txt"
    sentinel.write_bytes(b"OUTSIDE-WALK-UNTOUCHED")
    nested = uploads / "nested"
    nested.mkdir(parents=True)
    outside_realpath = os.path.realpath(str(outside))
    backup = uploads / "nested.aside"

    mkdir = os.mkdir
    opened = os.open
    swapped = {"done": False}

    def swap_nested():
        if swapped["done"]:
            return
        if _swap_directory_for_outside(nested, backup, outside_realpath):
            swapped["done"] = True

    def swapping_mkdir(*args, **kwargs):
        if not swapped["done"] and _matches_path(nested / "deep", args, kwargs):
            swap_nested()
        return mkdir(*args, **kwargs)

    def swapping_open(*args, **kwargs):
        fd = opened(*args, **kwargs)
        if not swapped["done"] and _matches_path(nested, args, kwargs):
            swap_nested()
        return fd

    monkeypatch.setattr(os, "mkdir", swapping_mkdir)
    monkeypatch.setattr(os, "open", swapping_open)
    response = _upload(client, "nested/deep/notes.txt", b"walk-payload")
    assert swapped["done"]
    assert not (outside / "deep").exists()
    assert not (outside / "deep" / "notes.txt").exists()
    assert sentinel.read_bytes() == b"OUTSIDE-WALK-UNTOUCHED"
    if response.status_code == 200:
        published = None
        if backup.exists() and not backup.is_symlink():
            candidate = backup / "deep" / "notes.txt"
            if _is_regular_file(candidate):
                published = candidate
        if published is None and nested.exists() and not nested.is_symlink():
            candidate = nested / "deep" / "notes.txt"
            if _is_regular_file(candidate):
                published = candidate
        assert published is not None
        assert published.read_bytes() == b"walk-payload"
        assert os.path.realpath(str(published)).startswith(
            os.path.realpath(str(uploads)) + os.sep)
    else:
        assert response.status_code == 403
        assert not (backup / "deep" / "notes.txt").exists()


def test_upload_publication_ancestor_swap_never_writes_outside_chat(
        upload_server, monkeypatch):
    client, uploads = upload_server
    outside = uploads.parent.parent / "outside-publish-tree"
    outside.mkdir(parents=True)
    sentinel = outside / "keep.txt"
    sentinel.write_bytes(b"OUTSIDE-PUBLISH-UNTOUCHED")
    nested = uploads / "nested"
    nested.mkdir(parents=True)
    outside_realpath = os.path.realpath(str(outside))
    backup = uploads / "nested.aside"
    original = b"publish-payload"

    create = tempfile.NamedTemporaryFile
    link = os.link
    swapped = {"done": False}

    def swap_nested():
        if swapped["done"]:
            return
        if _swap_directory_for_outside(nested, backup, outside_realpath):
            swapped["done"] = True

    @contextmanager
    def swapping_named_temporary(*args, **kwargs):
        directory = kwargs.get("dir")
        if not swapped["done"] and directory is not None:
            if _matches_path(nested, (directory,), {}):
                swap_nested()
        with create(*args, **kwargs) as handle:
            yield handle

    def swapping_link(source, destination, *args, **kwargs):
        if not swapped["done"] and (
            _matches_path(nested, (destination,), kwargs, directory_keys=("dst_dir_fd", "dir_fd"))
            or _matches_path(nested / "brief.docx", (destination,), kwargs, directory_keys=("dst_dir_fd", "dir_fd"))
        ):
            swap_nested()
        return link(source, destination, *args, **kwargs)

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", swapping_named_temporary)
    monkeypatch.setattr(os, "link", swapping_link)
    response = _upload(client, "nested/brief.docx", original, attachment_id="F-pub")
    assert swapped["done"]
    assert not (outside / "brief.docx").exists()
    assert sentinel.read_bytes() == b"OUTSIDE-PUBLISH-UNTOUCHED"
    if response.status_code == 200:
        body = response.json()
        stored = body["filename"]
        published = None
        stored_name = Path(stored).name
        if backup.exists() and not backup.is_symlink():
            candidate = backup / stored_name
            if _is_regular_file(candidate):
                published = candidate
        if published is None and nested.exists() and not nested.is_symlink():
            candidate = nested / stored_name
            if _is_regular_file(candidate):
                published = candidate
        assert published is not None
        assert published.read_bytes() == original
        assert os.path.realpath(str(published)).startswith(
            os.path.realpath(str(uploads)) + os.sep)
        assert body["md5"] == hashlib.md5(original).hexdigest()
        assert body["size"] == len(original)
        receipts = _receipts(uploads)
        assert receipts["F-pub"]["stored_name"] == stored
        assert receipts["F-pub"]["size"] == len(original)
        assert receipts["F-pub"]["md5"] == body["md5"]
    else:
        assert response.status_code == 403
        assert not (backup / "brief.docx").exists()


def test_sandbox_visible_staging_replacement_cannot_substitute_uploaded_bytes(
        upload_server, monkeypatch):
    client, uploads = upload_server
    original = b"original-upload-bytes"
    substitute = b"SANDBOX-SUBSTITUTED-BYTES"
    workspace = uploads.parent
    visible_staging = []
    uploads_stat = None
    link = os.link
    written = os.write

    def visible_upload_names():
        names = []
        if not uploads.exists():
            return names
        for root, dirs, files in os.walk(uploads, followlinks=False):
            dirs[:] = [name for name in dirs if not name.startswith(".")]
            for name in files:
                if name.startswith(".upload-"):
                    names.append(name)
        return names

    def record_visible_staging():
        visible_staging.extend(visible_upload_names())

    def observing_write(fd, data):
        record_visible_staging()
        return written(fd, data)

    def swapping_link(source, destination, *args, **kwargs):
        nonlocal uploads_stat
        record_visible_staging()
        if uploads_stat is None and uploads.exists():
            uploads_stat = os.lstat(uploads)
        source_name = os.path.basename(os.fspath(source))
        src_dir_fd = kwargs.get("src_dir_fd")
        if (
            source_name.startswith(".upload-")
            and src_dir_fd is None
            and kwargs.get("dir_fd") is None
        ):
            source_path = Path(source)
            try:
                parent_stat = os.lstat(source_path.parent)
            except OSError:
                parent_stat = None
            if parent_stat is not None and uploads_stat is not None and _same_inode(parent_stat, uploads_stat):
                source_path.write_bytes(substitute)
        return link(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "write", observing_write)
    monkeypatch.setattr(os, "link", swapping_link)
    response = _upload(client, "brief.docx", original, attachment_id="F-stage")
    record_visible_staging()
    assert response.status_code == 200
    body = response.json()
    published = uploads / body["filename"]
    assert published.is_file()
    assert published.read_bytes() == original
    assert body["filename"] == "brief.docx"
    assert body["size"] == len(original)
    assert body["md5"] == hashlib.md5(original).hexdigest()
    receipts = _receipts(uploads)
    assert receipts["F-stage"]["stored_name"] == "brief.docx"
    assert receipts["F-stage"]["size"] == len(original)
    assert receipts["F-stage"]["md5"] == body["md5"]
    assert visible_staging == []
    leftover = [
        path.name for path in uploads.iterdir() if path.name.startswith(".upload-")
    ]
    assert leftover == []
    control = workspace / ".ocu"
    assert control.is_dir()
    assert stat.S_IMODE(os.lstat(control).st_mode) == 0o700
    leftover_control = [
        path.name for path in control.iterdir() if path.name.startswith(".upload-")
    ]
    assert leftover_control == []
    published.write_bytes(b"post-publication-edit")
    assert published.read_bytes() == b"post-publication-edit"


@pytest.mark.parametrize("fail_sync", (False, True), ids=("close-only", "sync-and-close"))
def test_direct_receipt_final_cleanup_retains_original_errors_and_complete_successor(
        tmp_path, monkeypatch, fail_sync):
    import traceback
    import descriptor_io
    import uploads

    chat_dir = tmp_path / CHAT
    chat_dir.mkdir()
    destination = chat_dir / ".ocu" / "imports.json"
    expected = {"F1": {
        "stored_name": "brief.docx", "size": 3, "md5": "f97c5d29941bfb1b2fdab0874906ab82",
        "imported_at": "2026-10-11T00:00:00Z",
    }}
    replaced, synced, closed = os.replace, os.fsync, os.close
    primary = OSError(errno.ENOSPC, "controlled receipt directory sync failure")
    cause = ValueError("controlled earlier receipt cause")
    cleanup = OSError(errno.EIO, "controlled receipt final close failure")
    committed = False
    writer_fd = None
    attempts = []

    def watch_replace(src, dst, *args, **kwargs):
        nonlocal committed
        result = replaced(src, dst, *args, **kwargs)
        if Path(dst) == destination:
            committed = True
        return result

    def watch_sync(fd):
        nonlocal writer_fd
        if committed and _same_inode(os.fstat(fd), destination.parent.stat()):
            writer_fd = fd
            if fail_sync:
                raise primary from cause
        return synced(fd)

    def close_then_fail(fd):
        if writer_fd is None or fd != writer_fd:
            return closed(fd)
        attempts.append(fd)
        closed(fd)
        with pytest.raises(OSError) as retired:
            os.fstat(fd)
        assert retired.value.errno == errno.EBADF
        raise cleanup

    try:
        with monkeypatch.context() as patches:
            patches.setattr(os, "replace", watch_replace)
            patches.setattr(os, "fsync", watch_sync)
            patches.setattr(os, "close", close_then_fail)
            with pytest.raises(Exception) as failure:
                uploads.write_import_receipts(chat_dir, expected)
        outcome = failure.value
        assert writer_fd is not None
        assert attempts == [writer_fd]
        with pytest.raises(OSError) as retired:
            os.fstat(writer_fd)
        assert retired.value.errno == errno.EBADF
        assert uploads.read_import_receipts(chat_dir) == expected
        assert sorted(path.name for path in destination.parent.iterdir()) == ["imports.json"]
        assert outcome.close_errors == (cleanup,)
        if fail_sync:
            assert outcome is primary
            assert outcome.__cause__ is cause
            assert not isinstance(outcome, descriptor_io.CommittedCloseError)
            assert str(cleanup) in "".join(traceback.format_exception(outcome))
        else:
            assert isinstance(outcome, descriptor_io.CommittedCloseError)
            assert outcome.resource == "import_receipts"
            assert outcome.committed is True
            assert outcome.__cause__ is cleanup
    finally:
        if writer_fd is not None:
            try:
                current = os.fstat(writer_fd)
            except OSError:
                pass
            else:
                if _same_inode(current, destination.parent.stat()):
                    closed(writer_fd)
