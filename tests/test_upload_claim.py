# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""HTTP upload publication preserves occupied names and complete bytes."""
import asyncio
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


def _upload(client, name, body):
    return client.post(f"/api/uploads/{CHAT}/{name}", headers=HEADERS,
                       files={"file": ("attachment", body)})


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
with TestClient(app.app) as client:
    response = client.post("/api/uploads/" + os.environ["OCU_CHAT"] + "/data.xlsx",
        headers={"Authorization": "Bearer " + os.environ["OCU_INTERNAL_TOKEN"]},
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
