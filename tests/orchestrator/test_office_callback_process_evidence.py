# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Separate-process evidence for callback flock, crash, and replay durability."""
from __future__ import annotations

import json
import os
import subprocess
import sys

from tests.orchestrator._office_recorded_callbacks import (
    recorded_status_2_payload,
    recorded_status_6_payload,
)
from tests.orchestrator._office_store import _stop_child, _wait_marker
from tests.orchestrator.test_office_callback_processing import (
    CHANGED,
    _bind_internal,
    _content_origin,
    _post,
    _receipt,
    _sha,
)
from tests.orchestrator.test_office_control_plane import _open_session
from tests.orchestrator.test_office_router import OFFICE_SETTINGS
from tests.orchestrator.test_office_session_lifecycle import _change
from tests.orchestrator.test_office_sessions import JWT_SECRET, SERVER_DIR, _outputs, _state, _versions, office_world
from tests.orchestrator.test_outputs_endpoint import CHAT, INTERNAL


def _callback_child_env(data, session, server, extra=None):
    env = os.environ.copy()
    pythonpath = env.get("PYTHONPATH", "")
    env.update(
        {
            "PYTHONPATH": str(SERVER_DIR) + (os.pathsep + pythonpath if pythonpath else ""),
            "BASE_DATA_DIR": str(data),
            "OCU_CHAT": CHAT,
            "OCU_SESSION": session["session_id"],
            "OCU_KEY": session["document_key"],
            "OCU_URL": server.url + "/cache/ok.docx",
            "OCU_INTERNAL_TOKEN": INTERNAL,
            "MCP_API_KEY": os.environ.get("MCP_API_KEY", ""),
            "PUBLIC_BASE_URL": os.environ.get("PUBLIC_BASE_URL", "http://ocu.example"),
            "OCU_WEBUI_ORIGIN": os.environ.get("OCU_WEBUI_ORIGIN", "https://webui.example"),
            "OCU_SANDBOX_SUBNET": os.environ.get("OCU_SANDBOX_SUBNET", "10.90.0.0/24"),
            "SINGLE_USER_MODE": os.environ.get("SINGLE_USER_MODE", "true"),
            "OCU_PUBLIC_PREFIX": os.environ.get("OCU_PUBLIC_PREFIX", "/ocu"),
            "OCU_OFFICE_JWT_SECRET": JWT_SECRET,
            "OCU_OFFICE_SELF_URL": OFFICE_SETTINGS["OCU_OFFICE_SELF_URL"],
            "OCU_OFFICE_DOCSERVER_ORIGIN": OFFICE_SETTINGS["OCU_OFFICE_DOCSERVER_ORIGIN"],
            "OCU_OFFICE_DOCSERVER_URL": server.url,
            "DOCKER_HOST": "unix:///tmp/ocu-acceptance-no-docker.sock",
            "DOCKER_SOCKET": "unix:///tmp/ocu-acceptance-no-docker.sock",
        }
    )
    if extra:
        env.update(extra)
    return env


_CALLBACK_CHILD = r'''
import json, os, sys
from pathlib import Path
from fastapi.testclient import TestClient
import office.download as download_mod
import office.versions as versions_mod
original_fetch = download_mod.fetch_callback_content
if os.environ.get("OCU_CRASH") == "1":
    def explode(*args, **kwargs):
        os.kill(os.getpid(), 9)
    versions_mod.store_version = explode
    def fetch_then_mark(url):
        body = original_fetch(url)
        Path(os.environ["OCU_DOWNLOADED"]).write_text("downloaded", encoding="utf-8")
        return body
    download_mod.fetch_callback_content = fetch_then_mark
if os.environ.get("OCU_FAIL_BARRIER") == "1":
    import office.store as store_mod
    def fail(fd):
        raise OSError("fresh worker barrier failure")
    store_mod.os.fsync = fail
if os.environ.get("OCU_TRAP_REPLACE") == "1":
    import office.store as store_mod
    def refuse(*args, **kwargs):
        Path(os.environ["OCU_REPLACED"]).write_text("replaced", encoding="utf-8")
        raise AssertionError("replay rewrote office state")
    store_mod.os.replace = refuse
import app
from office.tokens import sign_jwt
session = {"session_id": os.environ["OCU_SESSION"], "document_key": os.environ["OCU_KEY"]}
status = int(os.environ.get("OCU_STATUS", "6"))
payload = {"payload": {"key": session["document_key"], "status": status, "url": os.environ["OCU_URL"]}}
if status in (6, 7):
    payload["payload"]["userdata"] = json.dumps({"save_seq": 1, "intent": "persist"}, separators=(",", ":"))
ready = os.environ.get("OCU_READY")
if ready:
    Path(ready).write_text("ready", encoding="utf-8")
    sys.stdin.readline()
with TestClient(app.app) as client:
    response = client.post(
        f'/office/callback/{os.environ["OCU_CHAT"]}/{session["session_id"]}',
        headers={"Authorization": "Bearer " + sign_jwt(payload)},
        json={"status": 2},
    )
print(json.dumps({"status": response.status_code, "body": response.json()}))
'''


def test_cross_worker_duplicate_serializes_one_version(office_world, monkeypatch):
    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    _change(
        session["session_id"],
        state="saving",
        save_seq=1,
        pending_save_seq=1,
        save_intents={"1": "persist"},
    )
    first = second = None
    with _content_origin({"/cache/ok.docx": CHANGED}) as server:
        _bind_internal(monkeypatch, server.url)
        ready_a = data.parent / "callback-worker-a"
        ready_b = data.parent / "callback-worker-b"
        env_a = _callback_child_env(data, session, server, {"OCU_READY": str(ready_a)})
        env_b = _callback_child_env(data, session, server, {"OCU_READY": str(ready_b)})
        try:
            first = subprocess.Popen(
                [sys.executable, "-c", _CALLBACK_CHILD],
                cwd=str(SERVER_DIR),
                env=env_a,
                text=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            second = subprocess.Popen(
                [sys.executable, "-c", _CALLBACK_CHILD],
                cwd=str(SERVER_DIR),
                env=env_b,
                text=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            _wait_marker(ready_a, first, "first callback worker did not load")
            _wait_marker(ready_b, second, "second callback worker did not load")
            first.stdin.write("go\n")
            second.stdin.write("go\n")
            first.stdin.flush()
            second.stdin.flush()
            out_a, err_a = first.communicate(timeout=15)
            out_b, err_b = second.communicate(timeout=15)
            assert first.returncode == 0, (out_a, err_a)
            assert second.returncode == 0, (out_b, err_b)
            results = [
                json.loads(out_a.strip().splitlines()[-1]),
                json.loads(out_b.strip().splitlines()[-1]),
            ]
        finally:
            _stop_child(first)
            _stop_child(second)
        assert {item["status"] for item in results} == {200}
        listed = json.loads(_state(data).read_bytes())["documents"][session["file_id"]]["versions"]
        assert sum(1 for item in listed if item["source"] == "autosave") == 1
        receipts = json.loads(_state(data).read_bytes())["receipts"][session["session_id"]]
        assert set(receipts) == {"1"}
    assert origin.hits == 0


def test_crash_after_download_before_store_retries_once(office_world, monkeypatch):
    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    _change(
        session["session_id"],
        state="saving",
        save_seq=1,
        pending_save_seq=1,
        save_intents={"1": "persist"},
    )
    child = None
    with _content_origin({"/cache/ok.docx": CHANGED}) as server:
        _bind_internal(monkeypatch, server.url)
        downloaded = data.parent / "callback-downloaded"
        env = _callback_child_env(
            data, session, server, {"OCU_CRASH": "1", "OCU_DOWNLOADED": str(downloaded)},
        )
        try:
            child = subprocess.Popen(
                [sys.executable, "-c", _CALLBACK_CHILD],
                cwd=str(SERVER_DIR),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            _wait_marker(downloaded, child, "callback child did not download before store")
            child.wait(timeout=5)
            assert child.returncode not in (0, None)
            assert server.hits >= 1
        finally:
            _stop_child(child)
        assert json.loads(_state(data).read_bytes())["receipts"] == {}
        listed = json.loads(_state(data).read_bytes())["documents"][session["file_id"]]["versions"]
        assert all(item["sha256"] != _sha(CHANGED) for item in listed)
        retry = _post(
            http,
            session,
            recorded_status_6_payload(
                document_key=session["document_key"],
                url=server.url + "/cache/ok.docx",
                save_seq=1,
                intent="persist",
            ),
        )
        assert retry.status_code == 200
        listed = json.loads(_state(data).read_bytes())["documents"][session["file_id"]]["versions"]
        assert sum(1 for item in listed if item["sha256"] == _sha(CHANGED)) == 1
        record = json.loads(_state(data).read_bytes())["sessions"][session["session_id"]]
        assert record["last_committed_seq"] == 1
    assert origin.hits == 0


def test_postreplace_durability_failure_replays_without_rewrite(office_world, monkeypatch):
    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    import office.store as store_mod

    _change(session["session_id"], state="editing", save_seq=0)
    original_replace = store_mod.os.replace
    original_fsync = store_mod.os.fsync
    replaced = {"done": False}

    def watch_state_replace(src, dst, *args, **kwargs):
        result = original_replace(src, dst, *args, **kwargs)
        if dst == "state.json" or (isinstance(dst, str) and str(dst).endswith("state.json")):
            replaced["done"] = True
        return result

    def fail_after_state_replace(fd):
        if replaced["done"]:
            replaced["done"] = False
            raise OSError("controlled directory fsync failure")
        return original_fsync(fd)

    failing = recovered = None
    with _content_origin({"/cache/ok.docx": CHANGED}) as server:
        _bind_internal(monkeypatch, server.url)
        payload = recorded_status_2_payload(
            document_key=session["document_key"],
            url=server.url + "/cache/ok.docx",
        )
        monkeypatch.setattr(store_mod.os, "replace", watch_state_replace)
        monkeypatch.setattr(store_mod.os, "fsync", fail_after_state_replace)
        response = _post(http, session, payload)
        assert response.status_code == 500
        assert response.json() == {"reason": "state_durability"}
        monkeypatch.setattr(store_mod.os, "replace", original_replace)
        monkeypatch.setattr(store_mod.os, "fsync", original_fsync)
        listed = json.loads(_state(data).read_bytes())["documents"][session["file_id"]]["versions"]
        assert listed[-1]["sha256"] == _sha(CHANGED)
        receipt = _receipt(data, session["session_id"], 1)
        assert receipt["sha256"] == _sha(CHANGED)
        blob = _versions(data) / _sha(CHANGED)
        assert blob.is_file()
        state_path = _state(data)
        index_path = data / CHAT / ".ocu" / "index.json"
        before_state = state_path.read_bytes()
        before_index = index_path.read_bytes()
        before_state_stat = state_path.stat()
        before_index_stat = index_path.stat()
        hits = server.hits
        replaced_marker = data.parent / "callback-replaced"
        env = _callback_child_env(
            data,
            session,
            server,
            {
                "OCU_STATUS": "2",
                "OCU_FAIL_BARRIER": "1",
                "OCU_TRAP_REPLACE": "1",
                "OCU_REPLACED": str(replaced_marker),
            },
        )
        try:
            failing = subprocess.Popen(
                [sys.executable, "-c", _CALLBACK_CHILD],
                cwd=str(SERVER_DIR),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            out_fail, err_fail = failing.communicate(timeout=15)
            assert failing.returncode == 0, (out_fail, err_fail)
            failed = json.loads(out_fail.strip().splitlines()[-1])
            assert failed["status"] == 500
            env["OCU_FAIL_BARRIER"] = "0"
            recovered = subprocess.Popen(
                [sys.executable, "-c", _CALLBACK_CHILD],
                cwd=str(SERVER_DIR),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            out_ok, err_ok = recovered.communicate(timeout=15)
            assert recovered.returncode == 0, (out_ok, err_ok)
            ok = json.loads(out_ok.strip().splitlines()[-1])
            assert ok == {"status": 200, "body": {"error": 0}}
        finally:
            _stop_child(failing)
            _stop_child(recovered)
        assert not replaced_marker.exists()
        assert state_path.read_bytes() == before_state
        assert index_path.read_bytes() == before_index
        after_state_stat = state_path.stat()
        after_index_stat = index_path.stat()
        assert (after_state_stat.st_ino, after_state_stat.st_mtime_ns) == (
            before_state_stat.st_ino, before_state_stat.st_mtime_ns,
        )
        assert (after_index_stat.st_ino, after_index_stat.st_mtime_ns) == (
            before_index_stat.st_ino, before_index_stat.st_mtime_ns,
        )
        after = json.loads(before_state)
        assert len(after["documents"][session["file_id"]]["versions"]) == len(listed)
        assert set(after["receipts"][session["session_id"]]) == {"1"}
        assert after["sessions"][session["session_id"]]["save_seq"] == 1
        assert server.hits == hits
        assert (_outputs(data) / "report.docx").read_bytes() == content
    assert origin.hits == 0
