# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""HTTP upload publication preserves occupied names and complete bytes."""
import asyncio
import datetime
import fcntl
import json
import os
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

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
}


def _environment(data):
    return {
        "OCU_INTERNAL_TOKEN": TOKEN, "MCP_API_KEY": "ocu-upload-claim-mcp-key",
        "OCU_WEBUI_ORIGIN": "https://webui.example", "SINGLE_USER_MODE": "true",
        "OCU_SANDBOX_SUBNET": "10.90.0.0/24", "PUBLIC_BASE_URL": "http://ocu.example",
        "BASE_DATA_DIR": str(data), "USER_DATA_BASE_PATH": str(data.parent / "user-data"),
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
            yield client, data / CHAT / "uploads"
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


def test_published_upload_is_readable_by_sandbox_assistant(upload_server):
    client, uploads = upload_server
    payloads = (b"first", b"second")
    expected_names = ("report.txt", "report (2).txt")
    for payload, name in zip(payloads, expected_names):
        response = _upload(client, "report.txt", payload)
        assert response.status_code == 200
        assert response.json()["filename"] == name
        path = uploads / name
        assert path.read_bytes() == payload
        assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_free_name_preserves_response_fields_and_uploads_destination(upload_server):
    client, uploads = upload_server
    response = _upload(client, "brief.docx", b"new")
    assert response.status_code == 200
    assert response.json() == {"status": "success", "filename": "brief.docx",
                               "size": 3, "md5": "22af645d1859cb5ca6da0c484f1f37ea"}
    assert (uploads / "brief.docx").read_bytes() == b"new"
    assert sorted(path.name for path in uploads.iterdir()) == ["brief.docx"]
    assert not (uploads.parent / "outputs").exists()


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


def test_unlocked_writer_wins_claim_without_losing_either_payload(upload_server, monkeypatch):
    client, uploads = upload_server
    link = os.link
    attempted = []

    def competing_link(source, destination, *args, **kwargs):
        source, destination = Path(source), Path(destination)
        assert source.name.startswith(".") and source.parent == destination.parent
        assert source.read_bytes() == b"new"
        with (uploads.parent / ".lifecycle.lock").open("a+") as lock_file:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        attempted.append(destination.name)
        if destination.name == "report.txt":
            assert not destination.exists()
            destination.write_bytes(b"unlocked writer")
        return link(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "link", competing_link)
    response = _upload(client, "report.txt", b"new")
    assert response.status_code == 200
    assert response.json()["filename"] == "report (2).txt"
    assert attempted == ["report.txt", "report (2).txt"]
    assert (uploads / "report.txt").read_bytes() == b"unlocked writer"
    assert (uploads / "report (2).txt").read_bytes() == b"new"
    assert sorted(path.name for path in uploads.iterdir()) == ["report (2).txt", "report.txt"]


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
    import tempfile

    client, uploads = upload_server
    uploads.mkdir(parents=True)
    (uploads / "report.txt").write_bytes(b"original")
    if failure == "claim":
        def failed_link(*args, **kwargs):
            raise PermissionError("injected claim failure")
        monkeypatch.setattr(os, "link", failed_link)
    else:
        create = tempfile.NamedTemporaryFile

        @contextmanager
        def failed_write(*args, **kwargs):
            with create(*args, **kwargs) as handle:
                def write(content):
                    handle.write(content[:1])
                    raise OSError("injected partial write failure")
                yield SimpleNamespace(name=handle.name, write=write)

        monkeypatch.setattr(tempfile, "NamedTemporaryFile", failed_write)
    response = _upload(client, "report.txt", b"new")
    assert response.status_code == 500
    assert (uploads / "report.txt").read_bytes() == b"original"
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
        _assert_concurrent_payloads(results, data / CHAT / "uploads")
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
        uploads = data / CHAT / "uploads"
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
