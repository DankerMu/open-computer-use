# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Fresh process route, crash cuts and writer serialization for history restore."""
from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import threading

import pytest

from tests.orchestrator._office_store import SERVER_DIR, _child_env, _stop_child
from tests.orchestrator.test_office_callback_publish import CHANGED, _read
from tests.orchestrator.test_office_restore import _closed, _restore
from tests.orchestrator.test_office_sessions import _create, _outputs, _snapshot, _versions, office_world
from tests.orchestrator.test_outputs_endpoint import CHAT


_WORKER = r'''
import json, os, stat, sys, threading
from contextlib import redirect_stdout
from types import SimpleNamespace
from docker.errors import NotFound
os.environ["BASE_DATA_DIR"] = os.environ["OCU_BASE"]
import docker_manager
def missing(identity):
    raise NotFound(identity)
docker_manager._docker_client = SimpleNamespace(containers=SimpleNamespace(get=missing))
with redirect_stdout(sys.stderr):
    import app
from fastapi.testclient import TestClient
from office.publish import recover_publications
cut = os.environ.get("RESTORE_CUT", "")
pending = {}
real_replace, real_sync = os.replace, os.fsync

def stop(point):
    if point != cut:
        return
    print("cut=" + point, flush=True)
    if os.environ.get("RESTORE_HOLD"):
        assert sys.stdin.readline().strip() == "release"
    else:
        threading.Event().wait()

def replaced(source, destination, *args, **kwargs):
    phase = ""
    if destination == "state.json":
        fd = os.open(source, os.O_RDONLY, dir_fd=kwargs.get("src_dir_fd"))
        try:
            with os.fdopen(fd, "rb") as stream:
                state = json.load(stream)
        except BaseException:
            raise
        live = [entry for entry in state["journal"].values() if entry["requester"] == "restore"]
        if live:
            phase = "capture" if "restore_version" in live[0] else ("accepted" if "target_path" not in live[0] else "prepared")
        elif os.environ.get("RESTORE_MODE") == "create":
            phase = "created"
        else:
            phase = "completion"
    elif destination == "report.docx":
        phase = "replacement"
    elif destination == "index.json":
        phase = "registration"
    result = real_replace(source, destination, *args, **kwargs)
    if phase:
        stop(phase + "-visible")
        parent = kwargs.get("dst_dir_fd")
        if parent is not None:
            info = os.fstat(parent)
            pending[(info.st_dev, info.st_ino)] = phase
    return result

def synced(fd):
    result = real_sync(fd)
    info = os.fstat(fd)
    if stat.S_ISDIR(info.st_mode):
        phase = pending.pop((info.st_dev, info.st_ino), "")
        if phase:
            stop(phase)
    return result
os.replace, os.fsync = replaced, synced
if os.environ.get("RESTORE_ANNOUNCE_LOCK"):
    import fcntl
    real_flock = fcntl.flock
    announced = False
    def flock(fd, operation):
        global announced
        if not announced and operation & fcntl.LOCK_EX:
            announced = True
            print("waiting-lock", flush=True)
        return real_flock(fd, operation)
    fcntl.flock = flock
mode = os.environ.get("RESTORE_MODE", "request")
if mode == "recover":
    recover_publications(os.environ["OCU_CHAT"])
elif mode == "startup":
    app.sweep_office_publications()
else:
    http = TestClient(app.app)
    suffix = "/sessions" if mode == "create" else "/restore"
    response = http.post(
        "/api/office/" + os.environ["OCU_CHAT"] + "/documents/" + os.environ["RESTORE_FILE"] + suffix,
        headers={"Authorization": "Bearer " + os.environ["OCU_INTERNAL_TOKEN"]},
        json={} if mode == "create" else {"number": int(os.environ.get("RESTORE_NUMBER", "1"))},
    )
    print("response=" + json.dumps({"status": response.status_code, "body": response.json()}), flush=True)
print("completed", flush=True)
'''


def _start(environment):
    return subprocess.Popen(
        [sys.executable, "-c", _WORKER], cwd=str(SERVER_DIR), env=environment,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _line(child, expected):
    ready, _, _ = select.select([child.stdout], [], [], 15)
    assert ready, "worker did not reach " + expected
    line = child.stdout.readline().strip()
    assert line == expected, (line, child.poll())


def _cut(data, file_id, cut):
    env = _child_env(data, RESTORE_FILE=file_id, RESTORE_CUT=cut)
    child = _start(env)
    try:
        _line(child, "cut=" + cut)
        child.kill()
        stdout, stderr = child.communicate(timeout=5)
        assert child.returncode == -9, (stdout, stderr)
    finally:
        _stop_child(child)
    return env


def _fresh(environment, mode="recover"):
    result = subprocess.run(
        [sys.executable, "-c", _WORKER], cwd=str(SERVER_DIR),
        env={**environment, "RESTORE_CUT": "", "RESTORE_MODE": mode},
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout.strip().splitlines()[-1] == "completed"
    return result


@pytest.mark.parametrize("cut", (
    "accepted-visible", "accepted", "prepared", "capture-visible", "capture",
    "replacement-visible", "replacement", "registration-visible", "registration",
    "completion-visible", "completion",
))
@pytest.mark.parametrize("mode", ("recover", "startup"))
def test_sigkill_restore_finishes_one_source_bound_obligation_and_registration(office_world, monkeypatch, cut, mode):
    http, data, _origin, _manager, broker, original, session = _closed(office_world, monkeypatch)
    path = _outputs(data) / "report.docx"
    agent = b"Agent pre-restore bytes"
    path.write_bytes(agent)
    before = _read(data)
    old_blobs = _snapshot(_versions(data))
    revision = broker.OutputsBroker().current_revision(CHAT)
    env = _cut(data, session["file_id"], cut)
    pending = _read(data)
    if pending["journal"]:
        entry, = pending["journal"].values()
        assert (entry["requester"], entry["session_id"], entry["save_seq"], entry["version"]) == ("restore", None, None, 1)
        assert entry["source_sha256"] == before["documents"][session["file_id"]]["versions"][0]["sha256"]
    _fresh(env, mode)
    after = _read(data)
    listed = after["documents"][session["file_id"]]["versions"]
    assert listed[:2] == before["documents"][session["file_id"]]["versions"]
    assert [(item["number"], item["source"], item["parent"], item["published"]) for item in listed[2:]] == [
        (3, "workspace", 2, True), (4, "restore", 1, True),
    ]
    assert path.read_bytes() == original
    assert (_versions(data) / listed[2]["sha256"]).read_bytes() == agent
    for digest, content in old_blobs.items():
        assert (_versions(data) / digest).read_bytes() == content
    assert after["documents"][session["file_id"]]["published_version"] == 4
    assert after["journal"] == {}
    assert after["sessions"] == before["sessions"] and after["receipts"] == before["receipts"]
    assert broker.OutputsBroker().current_revision(CHAT) == revision + 1
    frozen = _snapshot(data)
    _fresh(env)
    assert _snapshot(data) == frozen


@pytest.mark.parametrize("cut", ("accepted", "capture", "replacement", "registration"))
@pytest.mark.parametrize("known_history", (False, True), ids=("new-Agent-bytes", "old-history-Agent-bytes"))
def test_recovery_retains_new_agent_bytes_and_leaves_requested_content_latest(office_world, monkeypatch, cut, known_history):
    http, data, _origin, _manager, broker, original, session = _closed(office_world, monkeypatch)
    path = _outputs(data) / "report.docx"
    path.write_bytes(b"first Agent bytes")
    before = _read(data)
    env = _cut(data, session["file_id"], cut)
    intervening = CHANGED if known_history else b"Agent writes after worker death"
    path.write_bytes(intervening)
    _fresh(env)
    after = _read(data)
    listed = after["documents"][session["file_id"]]["versions"]
    assert listed[:2] == before["documents"][session["file_id"]]["versions"]
    expected = ([(3, "restore", 1)] if known_history and cut == "accepted" else
                [(3, "workspace", 2), (4, "restore", 1)] if known_history or cut == "accepted" else [
                    (3, "workspace", 2), (4, "restore", 1), (5, "workspace", 4), (6, "restore", 1),
                ])
    assert [(item["number"], item["source"], item["parent"]) for item in listed[2:]] == expected
    captured = next(item for item in listed if (_versions(data) / item["sha256"]).read_bytes() == intervening)
    assert captured["number"] < listed[-1]["number"]
    assert listed[-1]["published"] is True and path.read_bytes() == original
    assert after["documents"][session["file_id"]]["published_version"] == listed[-1]["number"]
    assert after["receipts"] == before["receipts"] and after["sessions"] == before["sessions"]
    frozen = _snapshot(data)
    _fresh(env)
    assert _snapshot(data) == frozen


@pytest.mark.parametrize("first", ("restore", "create"))
def test_separate_process_create_and_restore_serialize_without_fake_sessions(office_world, monkeypatch, first):
    http, data, _origin, _manager, broker, original, session = _closed(office_world, monkeypatch)
    mode = "request" if first == "restore" else "create"
    point = "accepted" if first == "restore" else "created"
    env = _child_env(data, RESTORE_FILE=session["file_id"], RESTORE_MODE=mode,
                     RESTORE_CUT=point, RESTORE_HOLD="1")
    owner, waiter = _start(env), None
    try:
        _line(owner, "cut=" + point)
        waiter = _start({**env, "RESTORE_CUT": "", "RESTORE_MODE": "create" if first == "restore" else "request",
                         "RESTORE_ANNOUNCE_LOCK": "1"})
        _line(waiter, "waiting-lock")
        assert waiter.poll() is None
        owner.stdin.write("release\n")
        owner.stdin.flush()
        owner_out, owner_error = owner.communicate(timeout=15)
        waiter_out, waiter_error = waiter.communicate(timeout=15)
        assert owner.returncode == waiter.returncode == 0, (owner_error, waiter_error)
        owner_response = json.loads(next(line[9:] for line in owner_out.splitlines() if line.startswith("response=")))
        waiter_response = json.loads(next(line[9:] for line in waiter_out.splitlines() if line.startswith("response=")))
        assert owner_response["status"] == (200 if first == "restore" else 201)
        assert waiter_response["status"] == (201 if first == "restore" else 409)
        if first == "create":
            assert waiter_response["body"] == {"reason": "session_open"}
        after = _read(data)
        active = [record for record in after["sessions"].values() if record["state"] == "opening"]
        assert len(active) == 1 and active[0]["file_id"] == session["file_id"]
        listed = after["documents"][session["file_id"]]["versions"]
        assert len(listed) == (3 if first == "restore" else 2)
        assert (_outputs(data) / "report.docx").read_bytes() == (original if first == "restore" else CHANGED)
        assert after["journal"] == {}
    finally:
        _stop_child(owner)
        _stop_child(waiter)


@pytest.mark.parametrize("cut", ("accepted", "capture", "replacement", "registration"))
def test_create_first_after_crashed_restore_settles_obligation_before_editor_admission(office_world, monkeypatch, cut):
    from tests.orchestrator.test_office_sessions import _b64url_decode
    http, data, _origin, _manager, broker, original, session = _closed(office_world, monkeypatch)
    path = _outputs(data) / "report.docx"
    path.write_bytes(b"Agent document before accepted restore")
    before = _read(data)
    env = _cut(data, session["file_id"], cut)
    result = _fresh(env, "create")
    response = json.loads(next(line[9:] for line in result.stdout.splitlines() if line.startswith("response=")))
    assert response["status"] == 201
    payload = response["body"]
    assert payload["file_id"] == session["file_id"] and payload["state"] == "opening"
    ticket = payload["editor_config"]["document"]["url"].rsplit("/", 1)[-1]
    claims = json.loads(_b64url_decode(ticket.split(".")[1]))
    assert claims["version"] == 4 and claims["session_id"] == payload["session_id"]
    after = _read(data)
    assert after["journal"] == {} and path.read_bytes() == original
    assert after["documents"][session["file_id"]]["published_version"] == 4
    assert len(after["documents"][session["file_id"]]["versions"]) == 4
    assert after["documents"][session["file_id"]]["versions"][:2] == before["documents"][session["file_id"]]["versions"]
    assert after["receipts"] == before["receipts"]
    assert after["sessions"][session["session_id"]] == before["sessions"][session["session_id"]]
    frozen = _snapshot(data)
    _fresh(env)
    assert _snapshot(data) == frozen


@pytest.mark.parametrize("cut", ("accepted", "capture", "replacement", "registration"))
def test_agent_writing_selected_source_bytes_does_not_impersonate_registration(office_world, monkeypatch, cut):
    http, data, _origin, _manager, broker, original, session = _closed(office_world, monkeypatch)
    path = _outputs(data) / "report.docx"
    path.write_bytes(b"Agent bytes before restore")
    revision = broker.OutputsBroker().current_revision(CHAT)
    env = _cut(data, session["file_id"], cut)
    path.write_bytes(original)
    _fresh(env)
    after = _read(data)
    listed = after["documents"][session["file_id"]]["versions"]
    assert len(listed) == (3 if cut == "accepted" else 4)
    assert listed[-1]["source"] == "restore" and listed[-1]["parent"] == 1 and listed[-1]["published"] is True
    assert path.read_bytes() == original and after["journal"] == {}
    assert broker.OutputsBroker().current_revision(CHAT) == revision + 1
    frozen = _snapshot(data)
    _fresh(env)
    assert _snapshot(data) == frozen


def test_create_cannot_admit_editor_when_accepted_restore_cannot_exclude_writer(office_world, monkeypatch):
    from tests.orchestrator.test_office_callback_publish import _running
    http, data, _origin, manager, _broker, original, session = _closed(office_world, monkeypatch)
    path = _outputs(data) / "report.docx"
    path.write_bytes(b"Agent content pending restore")
    _cut(data, session["file_id"], "accepted")
    before = _read(data)
    container = _running(manager)
    container.pause.side_effect = RuntimeError("writer exclusion unavailable")
    response = _create(http, session["file_id"])
    assert response.status_code == 503 and response.json() == {"reason": "publish_pending"}
    after = _read(data)
    assert after["sessions"] == before["sessions"] and after["documents"] == before["documents"]
    assert after["receipts"] == before["receipts"] and set(after["journal"]) == set(before["journal"])
    assert path.read_bytes() == b"Agent content pending restore"


def test_accepted_restore_survives_repeated_index_lookup_failure_without_fabricating_history(
    office_world, monkeypatch,
):
    from office.publish import RecoveryRequiredError, recover_publications
    http, data, _origin, _manager, broker, original, session = _closed(office_world, monkeypatch)
    before = _read(data)
    history = _snapshot(_versions(data))
    revision = broker.OutputsBroker().current_revision(CHAT)
    environment = _cut(data, session["file_id"], "accepted")
    accepted = _read(data)
    journal_id, = accepted["journal"]
    entry = accepted["journal"][journal_id]
    assert (entry["file_id"], entry["requester"], entry["session_id"], entry["save_seq"], entry["version"]) == (
        session["file_id"], "restore", None, None, 1,
    )
    assert entry["source_sha256"] == before["documents"][session["file_id"]]["versions"][0]["sha256"]
    assert "target_path" not in entry and "restore_version" not in entry
    assert accepted["documents"] == before["documents"]
    index = data / CHAT / ".ocu" / "index.json"
    index_before = index.read_bytes()
    index.write_bytes(b"{broken")
    for _ in range(2):
        with pytest.raises(RecoveryRequiredError):
            recover_publications(CHAT)
        unresolved = _read(data)
        assert unresolved["journal"] == accepted["journal"]
        assert unresolved["documents"] == before["documents"]
        assert unresolved["sessions"] == before["sessions"] and unresolved["receipts"] == before["receipts"]
        assert _snapshot(_versions(data)) == history
        assert (_outputs(data) / "report.docx").read_bytes() == CHANGED
        assert index.read_bytes() == b"{broken"
    index.write_bytes(index_before)
    _fresh(environment)
    completed = _read(data)
    document = completed["documents"][session["file_id"]]
    assert document["versions"][:2] == before["documents"][session["file_id"]]["versions"]
    assert [(item["number"], item["source"], item["parent"], item["published"]) for item in document["versions"][2:]] == [
        (3, "restore", 1, True),
    ]
    assert document["published_version"] == 3 and document["published_sha256"] == document["versions"][0]["sha256"]
    assert completed["journal"] == {}
    assert completed["sessions"] == before["sessions"] and completed["receipts"] == before["receipts"]
    assert (_outputs(data) / "report.docx").read_bytes() == original
    assert _snapshot(_versions(data)) == history
    assert broker.OutputsBroker().current_revision(CHAT) == revision + 1
    frozen = _snapshot(data)
    _fresh(environment)
    assert _snapshot(data) == frozen


def test_create_first_after_unresolved_index_recovery_preserves_restore_and_never_admits_stale_editor(
    office_world, monkeypatch,
):
    from office.sweep import sweep_office_publications
    from tests.orchestrator.test_office_sessions import _b64url_decode
    http, data, _origin, _manager, broker, original, session = _closed(office_world, monkeypatch)
    revision = broker.OutputsBroker().current_revision(CHAT)
    environment = _cut(data, session["file_id"], "accepted")
    accepted = _read(data)
    index = data / CHAT / ".ocu" / "index.json"
    index_before = index.read_bytes()
    index.write_bytes(b"{broken")
    sweep_office_publications()
    before_create = _snapshot(data)
    response = _create(http, session["file_id"])
    assert response.status_code == 500 and response.json() == {"reason": "state_corrupt"}
    assert _snapshot(data) == before_create
    unresolved = _read(data)
    assert unresolved["journal"] == accepted["journal"]
    assert unresolved["documents"] == accepted["documents"]
    assert unresolved["sessions"] == accepted["sessions"] and unresolved["receipts"] == accepted["receipts"]
    assert (_outputs(data) / "report.docx").read_bytes() == CHANGED
    index.write_bytes(index_before)
    result = _fresh(environment, "create")
    response = json.loads(next(line[9:] for line in result.stdout.splitlines() if line.startswith("response=")))
    assert response["status"] == 201
    payload = response["body"]
    assert payload["file_id"] == session["file_id"] and payload["state"] == "opening"
    ticket = payload["editor_config"]["document"]["url"].rsplit("/", 1)[-1]
    claims = json.loads(_b64url_decode(ticket.split(".")[1]))
    assert claims["version"] == 3 and claims["session_id"] == payload["session_id"]
    completed = _read(data)
    document = completed["documents"][session["file_id"]]
    assert completed["journal"] == {} and (_outputs(data) / "report.docx").read_bytes() == original
    assert document["published_version"] == 3
    assert document["versions"][:2] == accepted["documents"][session["file_id"]]["versions"]
    assert [(item["number"], item["source"], item["parent"], item["published"]) for item in document["versions"][2:]] == [
        (3, "restore", 1, True),
    ]
    assert set(completed["sessions"]) == {*accepted["sessions"], payload["session_id"]}
    assert completed["sessions"][session["session_id"]] == accepted["sessions"][session["session_id"]]
    assert completed["receipts"] == accepted["receipts"]
    assert broker.OutputsBroker().current_revision(CHAT) == revision + 1
    frozen = _snapshot(data)
    _fresh(environment)
    assert _snapshot(data) == frozen
