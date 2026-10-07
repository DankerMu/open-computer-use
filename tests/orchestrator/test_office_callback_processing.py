# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Public HTTP seam for authenticated DocumentServer callback processing."""
from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from tests.orchestrator._office_recorded_callbacks import (
    recorded_status_1_payload,
    recorded_status_2_payload,
    recorded_status_4_payload,
    recorded_status_6_payload,
    synthetic_status_3_payload,
    synthetic_status_7_payload,
)
from tests.orchestrator.test_office_control_plane import (
    _callback_path,
    _header_jwt,
    _open_session,
)
from tests.orchestrator.test_office_ooxml import intact_docx, intact_xlsx
from tests.orchestrator.test_office_router import OFFICE_SETTINGS
from tests.orchestrator.test_office_save_close import _forcesave, _save
from tests.orchestrator.test_office_session_lifecycle import _change, _status
from tests.orchestrator.test_office_sessions import (
    JWT_SECRET,
    _assert_refusal,
    _outputs,
    _snapshot,
    _state,
    _versions,
    office_world,
)
from tests.orchestrator.test_lifecycle import _docker as _docker_engine
from tests.orchestrator.test_outputs_endpoint import CHAT

RECORDED_STATUS_1_USERS = ["user-1"]
CHANGED = intact_docx() + b"\n"


class _ContentOrigin:
    def __init__(self, bodies, *, redirect=None, delay=0.0, trap=None):
        self.requests = []
        self.hits = 0
        self._bodies = dict(bodies)
        self._redirect = redirect
        self._delay = delay
        self._trap = trap
        parent = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):
                return

            def do_GET(self):
                parent.hits += 1
                parent.requests.append(
                    {
                        "path": self.path,
                        "host": self.headers.get("Host"),
                        "headers": {key.lower(): value for key, value in self.headers.items()},
                    }
                )
                if parent._trap is not None:
                    parent._trap()
                if parent._delay:
                    import time

                    time.sleep(parent._delay)
                if parent._redirect is not None and self.path == parent._redirect[0]:
                    self.send_response(302)
                    self.send_header("Location", parent._redirect[1])
                    self.end_headers()
                    return
                body = parent._bodies.get(self.path.split("?", 1)[0], None)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def close(self):
        self._server.shutdown()
        self._thread.join(timeout=2)


@contextmanager
def _content_origin(bodies, **kwargs):
    server = _ContentOrigin(bodies, **kwargs)
    try:
        yield server
    finally:
        server.close()


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()

def _post(http, session, extra, *, unsigned=None):
    signed = _header_jwt(session, extra=extra)
    body = {"key": "unsigned-foreign", "status": 99, "url": "http://127.0.0.1:1/never"}
    if unsigned:
        body.update(unsigned)
    return http.post(
        _callback_path(session),
        headers={"Authorization": "Bearer " + signed},
        json=body,
    )



@contextmanager
def _bind_internal(monkeypatch, url: str):
    with monkeypatch.context() as patch:
        patch.setenv("OCU_OFFICE_DOCSERVER_URL", url)
        yield url


def _history(data, file_id):
    state = json.loads(_state(data).read_bytes())
    return state["documents"][file_id]["versions"], state["receipts"], state["journal"]


def _receipt(data, session_id, save_seq):
    return json.loads(_state(data).read_bytes())["receipts"][session_id][str(save_seq)]


def test_signed_recorded_status1_opens_created_session_to_editing_without_history(office_world):
    http, data, origin, _docker, broker, content, session = _open_session(office_world)
    from office.store import OfficeStore

    session_id = session["session_id"]
    file_id = session["file_id"]
    assert session["state"] == "opening"

    before_store = OfficeStore().read(CHAT)
    before_record = before_store["sessions"][session_id]
    assert before_record["state"] == "opening"
    before_document = json.loads(json.dumps(before_store["documents"][file_id]))
    before_versions = _snapshot(_versions(data))
    before_workspace = (_outputs(data) / "report.docx").read_bytes()
    before_index = (data / CHAT / ".ocu" / "index.json").read_bytes()
    assert before_store["receipts"] == {}
    assert before_workspace == content

    payload = recorded_status_1_payload(document_key=session["document_key"])
    signed = _header_jwt(
        session,
        extra={"actions": payload["actions"], "users": payload["users"]},
    )
    callback = http.post(
        _callback_path(session),
        headers={"Authorization": "Bearer " + signed},
        json={"key": "unsigned-foreign", "status": 2},
    )
    assert callback.status_code == 200
    assert callback.json() == {"error": 0}
    assert origin.hits == 0

    after_store = OfficeStore().read(CHAT)
    after_record = after_store["sessions"][session_id]
    assert after_record["participants"] == RECORDED_STATUS_1_USERS
    assert after_store["receipts"] == {}
    assert after_store["documents"][file_id] == before_document
    assert _snapshot(_versions(data)) == before_versions
    assert (_outputs(data) / "report.docx").read_bytes() == before_workspace
    assert (data / CHAT / ".ocu" / "index.json").read_bytes() == before_index

    status = _status(http, session_id)
    assert status.status_code == 200
    projected = status.json()
    assert projected["state"] == "editing"
    assert projected["session_id"] == session_id
    assert projected["file_id"] == file_id
    assert projected["document_key"] == session["document_key"]
    assert json.loads(_state(data).read_bytes())["sessions"][session_id]["state"] == "editing"


def test_unknown_session_is_404_without_download(office_world, monkeypatch):
    http, data, origin, _docker, _broker, _content, session = _open_session(office_world)
    missing = dict(session)
    missing["session_id"] = "00000000-0000-4000-8000-000000000099"
    with _content_origin({"/cache/output.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        payload = recorded_status_2_payload(
            document_key=session["document_key"],
            url=OFFICE_SETTINGS["OCU_OFFICE_DOCSERVER_ORIGIN"] + "/cache/output.docx",
        )
        response = _post(http, missing, payload)
        _assert_refusal(response, 404, "unknown_session")
        assert server.requests == []
    assert origin.hits == 0
    assert json.loads(_state(data).read_bytes())["receipts"] == {}


def test_unknown_status_is_422_and_recorded_without_mutation(office_world, caplog):
    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    before = _snapshot(data)
    with caplog.at_level("ERROR", logger="ocu.office"):
        response = _post(http, session, {"status": 5, "users": ["user-1"]})
    _assert_refusal(response, 422, "unknown_status")
    after = _snapshot(data)
    assert after[str(_state(data).relative_to(data))] == before[str(_state(data).relative_to(data))]
    assert (_outputs(data) / "report.docx").read_bytes() == content
    assert origin.hits == 0
    recorded = "\n".join(record.getMessage() for record in caplog.records)
    assert f"chat={CHAT}" in recorded
    assert f"session={session['session_id']}" in recorded
    assert "reason=unknown_status" in recorded
    assert "status=5" in recorded
    assert JWT_SECRET not in recorded
    assert "unsigned-foreign" not in recorded


def test_status3_ends_error_with_final_save_failed(office_world):
    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    _change(session["session_id"], state="editing")
    payload = synthetic_status_3_payload(document_key=session["document_key"])
    response = _post(http, session, payload)
    assert response.status_code == 200
    assert response.json() == {"error": 0}
    record = json.loads(_state(data).read_bytes())["sessions"][session["session_id"]]
    assert record["state"] == "error"
    assert record["reason"] == "final_save_failed"
    receipt = _receipt(data, session["session_id"], 1)
    assert receipt == {"status": 3, "sha256": None, "version": None, "answer": {"error": 0}}
    assert (_outputs(data) / "report.docx").read_bytes() == content
    assert origin.hits == 0


def test_status7_returns_matching_saving_session_to_editing(office_world):
    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    _change(
        session["session_id"],
        state="saving",
        save_seq=1,
        pending_save_seq=1,
        save_intents={"1": "persist"},
    )
    payload = synthetic_status_7_payload(
        document_key=session["document_key"], save_seq=1, intent="persist",
    )
    response = _post(http, session, payload)
    assert response.status_code == 200
    record = json.loads(_state(data).read_bytes())["sessions"][session["session_id"]]
    assert record["state"] == "editing"
    assert record["reason"] == "forcesave_failed"
    assert record["pending_save_seq"] is None
    assert record["save_intents"] == {"1": "persist"}
    assert _receipt(data, session["session_id"], 1)["status"] == 7
    assert (_outputs(data) / "report.docx").read_bytes() == content
    assert origin.hits == 0


def test_status4_closes_when_latest_version_is_published(office_world):
    http, data, origin, _docker, broker, content, session = _open_session(office_world)
    _change(session["session_id"], state="closing", save_seq=1, pending_close_seq=1)
    payload = recorded_status_4_payload(document_key=session["document_key"])
    before_workspace = (_outputs(data) / "report.docx").read_bytes()
    before_index = (data / CHAT / ".ocu" / "index.json").read_bytes()
    versions, _receipts, journal = _history(data, session["file_id"])
    revision = broker.OutputsBroker().current_revision(CHAT)
    response = _post(http, session, payload)
    assert response.status_code == 200
    record = json.loads(_state(data).read_bytes())["sessions"][session["session_id"]]
    assert record["state"] == "closed"
    assert record["last_committed_seq"] == 1
    assert record["last_published_seq"] == 1
    assert _receipt(data, session["session_id"], 1)["status"] == 4
    after_versions, _, after_journal = _history(data, session["file_id"])
    assert after_versions == versions
    assert after_journal == journal == {}
    assert (_outputs(data) / "report.docx").read_bytes() == before_workspace
    assert (data / CHAT / ".ocu" / "index.json").read_bytes() == before_index
    from office.publish import recover_publications
    before_recovery = _snapshot(data)
    recover_publications(CHAT)
    assert _snapshot(data) == before_recovery
    assert broker.OutputsBroker().current_revision(CHAT) == revision
    assert origin.hits == 0


def test_status4_publishes_unpublished_latest_and_closes(office_world):
    http, data, origin, _docker, broker, content, session = _open_session(office_world)
    from office.store import OfficeStore

    unpublished = OfficeStore().store_version(
        CHAT, session["file_id"], CHANGED, source="autosave", parent=1,
        published=False, min_free_bytes=0,
    )
    _change(session["session_id"], state="closing", save_seq=2, pending_close_seq=2, last_committed_seq=1)
    payload = recorded_status_4_payload(document_key=session["document_key"])
    revision = broker.OutputsBroker().current_revision(CHAT)
    response = _post(http, session, payload)
    assert response.status_code == 200
    record = json.loads(_state(data).read_bytes())["sessions"][session["session_id"]]
    assert record["state"] == "closed"
    assert record["last_committed_seq"] == 2
    assert record["last_published_seq"] == 2
    assert record["baseline_sha256"] == _sha(CHANGED)
    assert record["pending_close_seq"] is None
    listed = json.loads(_state(data).read_bytes())["documents"][session["file_id"]]["versions"]
    assert listed[-1]["number"] == unpublished["number"]
    assert listed[-1]["published"] is True
    assert len(listed) == 2
    assert (_outputs(data) / "report.docx").read_bytes() == CHANGED
    assert _receipt(data, session["session_id"], 2) == {
        "status": 4, "sha256": None, "version": None, "answer": {"error": 0},
    }
    assert json.loads(_state(data).read_bytes())["journal"] == {}
    assert broker.OutputsBroker().current_revision(CHAT) == revision + 1
    assert origin.hits == 0


def test_status6_persist_stores_unpublished_autosave(office_world, monkeypatch):
    http, data, origin, _docker, broker, content, session = _open_session(office_world)
    _change(
        session["session_id"],
        state="saving",
        save_seq=1,
        pending_save_seq=1,
        save_intents={"1": "persist"},
    )
    before_workspace = (_outputs(data) / "report.docx").read_bytes()
    before_index = (data / CHAT / ".ocu" / "index.json").read_bytes()
    before_revision = broker.OutputsBroker().current_revision(CHAT)
    with _content_origin({"/cache/output.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        payload = recorded_status_6_payload(
            document_key=session["document_key"],
            url=OFFICE_SETTINGS["OCU_OFFICE_DOCSERVER_ORIGIN"] + "/cache/output.docx",
            save_seq=1,
            intent="persist",
        )
        response = _post(http, session, payload)
        assert response.status_code == 200
        assert response.json() == {"error": 0}
        assert [item["path"] for item in server.requests] == ["/cache/output.docx"]
        assert urlsplit(server.url).hostname == "127.0.0.1"
    record = json.loads(_state(data).read_bytes())["sessions"][session["session_id"]]
    listed = json.loads(_state(data).read_bytes())["documents"][session["file_id"]]["versions"]
    assert record["state"] == "editing"
    assert record["last_committed_seq"] == 1
    assert listed[-1]["source"] == "autosave"
    assert listed[-1]["published"] is False
    assert listed[-1]["sha256"] == _sha(CHANGED)
    persisted = json.loads(_state(data).read_bytes())
    document = persisted["documents"][session["file_id"]]
    assert persisted["journal"] == {}
    assert document["published_version"] == 1
    assert document["published_sha256"] == _sha(content)
    assert record["baseline_sha256"] == _sha(content)
    assert record["last_published_seq"] == 0
    assert (_versions(data) / _sha(CHANGED)).read_bytes() == CHANGED
    receipt = _receipt(data, session["session_id"], 1)
    assert receipt["status"] == 6
    assert receipt["sha256"] == _sha(CHANGED)
    assert receipt["version"] == listed[-1]["number"]
    assert (_outputs(data) / "report.docx").read_bytes() == before_workspace
    assert (data / CHAT / ".ocu" / "index.json").read_bytes() == before_index
    assert broker.OutputsBroker().current_revision(CHAT) == before_revision
    persisted_bytes = _state(data).read_bytes()
    from office.publish import recover_publications
    recover_publications(CHAT)
    assert _state(data).read_bytes() == persisted_bytes
    assert json.loads(_state(data).read_bytes()) == persisted
    assert (_outputs(data) / "report.docx").read_bytes() == before_workspace
    assert (data / CHAT / ".ocu" / "index.json").read_bytes() == before_index
    assert broker.OutputsBroker().current_revision(CHAT) == before_revision
    assert origin.hits == 0


def test_status6_publish_intent_publishes_workspace_and_completes_save(office_world, monkeypatch):
    http, data, origin, docker, broker, content, session = _open_session(office_world)
    session_id = session["session_id"]
    file_id = session["file_id"]
    workspace = _outputs(data) / "report.docx"
    assert workspace.read_bytes() == content
    assert CHANGED != content
    before_revision = broker.OutputsBroker().current_revision(CHAT)
    docker._docker_client = _docker_engine()

    opened = _post(
        http, session, recorded_status_1_payload(document_key=session["document_key"])
    )
    assert opened.status_code == 200
    assert opened.json() == {"error": 0}
    with _forcesave(monkeypatch, session["document_key"]) as (_server, issued):
        accepted = _save(http, session_id, "publish")
    assert accepted.status_code == 202
    assert accepted.json() == {
        "session_id": session_id, "save_seq": 1, "intent": "publish",
    }
    assert issued == [{"save_seq": 1, "intent": "publish"}]

    with (
        _content_origin({"/cache/save.docx": CHANGED}) as server,
        _bind_internal(monkeypatch, server.url),
    ):
        payload = recorded_status_6_payload(
            document_key=session["document_key"],
            url=OFFICE_SETTINGS["OCU_OFFICE_DOCSERVER_ORIGIN"] + "/cache/save.docx",
            save_seq=issued[0]["save_seq"],
            intent=issued[0]["intent"],
        )
        response = _post(http, session, payload)
        assert response.status_code == 200
        assert response.json() == {"error": 0}
        assert [item["path"] for item in server.requests] == ["/cache/save.docx"]

    persisted = json.loads(_state(data).read_bytes())
    document = persisted["documents"][file_id]
    assert len(document["versions"]) == 2
    saved = document["versions"][-1]
    assert saved["source"] == "save"
    assert saved["number"] == 2
    assert saved["sha256"] == _sha(CHANGED)
    assert (_versions(data) / _sha(CHANGED)).read_bytes() == CHANGED
    receipt = persisted["receipts"][session_id]["1"]
    assert receipt["status"] == 6
    assert receipt["sha256"] == _sha(CHANGED)
    assert receipt["version"] == 2
    assert receipt["answer"] == {"error": 0}

    assert workspace.read_bytes() == CHANGED
    assert saved["published"] is True
    assert document["published_version"] == 2
    assert document["published_sha256"] == _sha(CHANGED)
    record = persisted["sessions"][session_id]
    assert record["state"] == "editing"
    assert record["reason"] is None
    assert record["save_seq"] == 1
    assert record["last_committed_seq"] == 1
    assert record["last_published_seq"] == 1
    assert record["baseline_sha256"] == _sha(CHANGED)
    assert persisted["journal"] == {}
    assert broker.OutputsBroker().current_revision(CHAT) == before_revision + 1
    assert origin.hits == 0


def test_status6_publish_and_status2_publish_save_and_close_versions(office_world, monkeypatch):
    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    _change(
        session["session_id"],
        state="saving",
        save_seq=1,
        pending_save_seq=1,
        save_intents={"1": "publish"},
    )
    with (
        _content_origin({"/cache/save.docx": CHANGED, "/cache/close.docx": CHANGED + b"x"}) as server,
        _bind_internal(monkeypatch, server.url),
    ):
        payload6 = recorded_status_6_payload(
            document_key=session["document_key"],
            url=server.url + "/cache/save.docx",
            save_seq=1,
            intent="publish",
        )
        response = _post(http, session, payload6)
        assert response.status_code == 200
        listed = json.loads(_state(data).read_bytes())["documents"][session["file_id"]]["versions"]
        assert listed[-1]["source"] == "save"
        assert listed[-1]["published"] is True
        record = json.loads(_state(data).read_bytes())["sessions"][session["session_id"]]
        assert record["state"] == "editing"
        assert record["last_published_seq"] == 1
        assert (_outputs(data) / "report.docx").read_bytes() == CHANGED
        payload2 = recorded_status_2_payload(
            document_key=session["document_key"],
            url=OFFICE_SETTINGS["OCU_OFFICE_DOCSERVER_ORIGIN"] + "/cache/close.docx",
        )
        response = _post(http, session, payload2)
        assert response.status_code == 200
        listed = json.loads(_state(data).read_bytes())["documents"][session["file_id"]]["versions"]
        record = json.loads(_state(data).read_bytes())["sessions"][session["session_id"]]
        assert listed[-1]["source"] == "close"
        assert listed[-1]["published"] is True
        assert record["state"] == "closed"
        assert record["last_committed_seq"] == 2
        assert record["last_published_seq"] == 2
        assert record["baseline_sha256"] == _sha(CHANGED + b"x")
        assert json.loads(_state(data).read_bytes())["journal"] == {}
        assert (_outputs(data) / "report.docx").read_bytes() == CHANGED + b"x"
        hosts = {item["host"] for item in server.requests}
        assert all(host.startswith("127.0.0.1") for host in hosts)
    assert origin.hits == 0


def test_out_of_order_and_duplicate_forcesave_delivery(office_world, monkeypatch):
    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    first = CHANGED
    second = CHANGED + b"2"
    _change(
        session["session_id"],
        state="saving",
        save_seq=4,
        pending_save_seq=4,
        save_intents={"3": "persist", "4": "persist"},
        last_committed_seq=0,
    )
    with _content_origin({"/cache/3.docx": first, "/cache/4.docx": second}) as server, _bind_internal(monkeypatch, server.url):
        four = recorded_status_6_payload(
            document_key=session["document_key"],
            url=server.url + "/cache/4.docx",
            save_seq=4,
            intent="persist",
        )
        assert _post(http, session, four).status_code == 200
        listed = json.loads(_state(data).read_bytes())["documents"][session["file_id"]]["versions"]
        assert listed[-1]["sha256"] == _sha(second)
        stale = recorded_status_6_payload(
            document_key=session["document_key"],
            url=server.url + "/cache/3.docx",
            save_seq=3,
            intent="persist",
        )
        response = _post(http, session, stale)
        _assert_refusal(response, 409, "stale_save_seq")
        listed_after = json.loads(_state(data).read_bytes())["documents"][session["file_id"]]["versions"]
        assert len(listed_after) == len(listed)
        duplicate = _post(http, session, four)
        assert duplicate.status_code == 200
        assert duplicate.json() == {"error": 0}
        listed_dup = json.loads(_state(data).read_bytes())["documents"][session["file_id"]]["versions"]
        assert len(listed_dup) == len(listed)
        receipts = json.loads(_state(data).read_bytes())["receipts"][session["session_id"]]
        assert set(receipts) == {"4"}
        index_before = (data / CHAT / ".ocu" / "index.json").read_bytes()
        different = recorded_status_6_payload(
            document_key=session["document_key"],
            url=server.url + "/cache/3.docx",
            save_seq=4,
            intent="persist",
        )
        response = _post(http, session, different)
        _assert_refusal(response, 409, "stale_save_seq")
        assert (data / CHAT / ".ocu" / "index.json").read_bytes() == index_before
    assert origin.hits == 0


def test_unissued_userdata_is_invalid_without_download(office_world, monkeypatch):
    http, data, origin, _docker, _broker, _content, session = _open_session(office_world)
    _change(session["session_id"], state="editing", save_seq=1, save_intents={"1": "persist"})
    with _content_origin({"/cache/output.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        payload = recorded_status_6_payload(
            document_key=session["document_key"],
            url=server.url + "/cache/output.docx",
            save_seq=9,
            intent="persist",
        )
        response = _post(http, session, payload)
        _assert_refusal(response, 422, "invalid_userdata")
        missing = dict(payload)
        missing.pop("userdata")
        response = _post(http, session, missing)
        _assert_refusal(response, 422, "invalid_userdata")
        assert server.requests == []
    assert origin.hits == 0


def test_retried_final_callbacks_do_not_redownload(office_world, monkeypatch):
    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    with _content_origin({"/cache/close.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        payload = recorded_status_2_payload(
            document_key=session["document_key"],
            url=server.url + "/cache/close.docx",
        )
        first = _post(http, session, payload)
        assert first.status_code == 200
        seq = json.loads(_state(data).read_bytes())["sessions"][session["session_id"]]["save_seq"]
        listed = json.loads(_state(data).read_bytes())["documents"][session["file_id"]]["versions"]
        hits = server.hits
        retry = _post(http, session, payload)
        assert retry.status_code == 200
        assert retry.json() == {"error": 0}
        assert server.hits == hits
        after = json.loads(_state(data).read_bytes())
        assert after["sessions"][session["session_id"]]["save_seq"] == seq
        assert len(after["documents"][session["file_id"]]["versions"]) == len(listed)
        _change(session["session_id"], state="closed")
        three = synthetic_status_3_payload(document_key=session["document_key"])
        late = _post(http, session, three)
        assert late.status_code == 200
        record = json.loads(_state(data).read_bytes())["sessions"][session["session_id"]]
        assert record["state"] == "closed"
    assert origin.hits == 0


def test_close_status1_autosave_status2_allocates_above_committed(office_world, monkeypatch):
    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    _change(session["session_id"], state="editing")
    payload1 = recorded_status_1_payload(document_key=session["document_key"])
    assert _post(http, session, payload1).status_code == 200
    _change(
        session["session_id"],
        state="closing",
        save_seq=3,
        pending_close_seq=3,
        last_committed_seq=0,
        save_intents={"1": "persist"},
        document_key=session["document_key"],
    )
    remaining = recorded_status_1_payload(document_key=session["document_key"])
    remaining["users"] = ["user-1"]
    assert _post(http, session, remaining).status_code == 200
    record = json.loads(_state(data).read_bytes())["sessions"][session["session_id"]]
    assert record["state"] == "editing"
    assert "pending_close_seq" not in record or record.get("pending_close_seq") in (None,)
    assert record["document_key"] == session["document_key"]
    _change(
        session["session_id"],
        state="saving",
        save_seq=4,
        pending_save_seq=4,
        save_intents={"4": "persist"},
        last_committed_seq=0,
    )
    with (
        _content_origin({"/cache/auto.docx": CHANGED, "/cache/final.docx": CHANGED + b"f"}) as server,
        _bind_internal(monkeypatch, server.url),
    ):
        auto = recorded_status_6_payload(
            document_key=session["document_key"],
            url=server.url + "/cache/auto.docx",
            save_seq=4,
            intent="persist",
        )
        assert _post(http, session, auto).status_code == 200
        final = recorded_status_2_payload(
            document_key=session["document_key"],
            url=server.url + "/cache/final.docx",
        )
        response = _post(http, session, final)
        assert response.status_code == 200
        assert response.json() != {"reason": "stale_save_seq"}
        receipts = json.loads(_state(data).read_bytes())["receipts"][session["session_id"]]
        close_seq = max(int(key) for key, value in receipts.items() if value["status"] == 2)
        assert close_seq > 4
        listed = json.loads(_state(data).read_bytes())["documents"][session["file_id"]]["versions"]
        assert listed[-1]["source"] == "close"
    assert origin.hits == 0


@pytest.mark.parametrize("state", ("closing", "conflict"))
@pytest.mark.parametrize("kind", ("status6", "status7"))
def test_forcesave_while_closing_or_conflict_keeps_state(office_world, monkeypatch, state, kind):
    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    _change(
        session["session_id"],
        state=state,
        save_seq=2,
        pending_save_seq=1,
        pending_close_seq=2,
        save_intents={"1": "publish"},
        reason="held",
    )
    with _content_origin({"/cache/save.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        if kind == "status6":
            payload = recorded_status_6_payload(
                document_key=session["document_key"],
                url=server.url + "/cache/save.docx",
                save_seq=1,
                intent="publish",
            )
            assert _post(http, session, payload).status_code == 200
            listed = json.loads(_state(data).read_bytes())["documents"][session["file_id"]]["versions"]
            assert listed[-1]["source"] == "save"
            assert listed[-1]["published"] is True
            assert (_outputs(data) / "report.docx").read_bytes() == CHANGED
            assert json.loads(_state(data).read_bytes())["journal"] == {}
        else:
            payload = synthetic_status_7_payload(
                document_key=session["document_key"], save_seq=1, intent="publish",
            )
            assert _post(http, session, payload).status_code == 200
            assert _receipt(data, session["session_id"], 1)["status"] == 7
            assert server.requests == []
        record = json.loads(_state(data).read_bytes())["sessions"][session["session_id"]]
        assert record["state"] == state
        assert record["reason"] == "held"
        assert record["pending_close_seq"] == 2
    assert origin.hits == 0


def test_status6_then_status7_same_seq_is_stale(office_world, monkeypatch):
    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    _change(
        session["session_id"],
        state="closing",
        save_seq=2,
        pending_save_seq=1,
        pending_close_seq=2,
        save_intents={"1": "publish"},
        reason="held",
    )
    with _content_origin({"/cache/save.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        payload = recorded_status_6_payload(
            document_key=session["document_key"],
            url=server.url + "/cache/save.docx",
            save_seq=1,
            intent="publish",
        )
        assert _post(http, session, payload).status_code == 200
        fail = synthetic_status_7_payload(
            document_key=session["document_key"], save_seq=1, intent="publish",
        )
        response = _post(http, session, fail)
        _assert_refusal(response, 409, "stale_save_seq")
        listed = json.loads(_state(data).read_bytes())["documents"][session["file_id"]]["versions"]
        assert listed[-1]["source"] == "save"
        record = json.loads(_state(data).read_bytes())["sessions"][session["session_id"]]
        assert record["state"] == "closing"
        assert record["reason"] == "held"
    assert origin.hits == 0


def test_late_timed_out_save_does_not_end_newer_saving(office_world, monkeypatch):
    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    _change(
        session["session_id"],
        state="saving",
        save_seq=4,
        pending_save_seq=4,
        save_intents={"3": "persist", "4": "publish"},
        last_committed_seq=2,
    )
    with _content_origin({"/cache/old.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        payload = recorded_status_6_payload(
            document_key=session["document_key"],
            url=server.url + "/cache/old.docx",
            save_seq=3,
            intent="persist",
        )
        assert _post(http, session, payload).status_code == 200
        record = json.loads(_state(data).read_bytes())["sessions"][session["session_id"]]
        assert record["state"] == "saving"
        assert record["pending_save_seq"] == 4
        listed = json.loads(_state(data).read_bytes())["documents"][session["file_id"]]["versions"]
        assert listed[-1]["source"] == "autosave"
    assert origin.hits == 0


def test_ended_session_without_matching_receipt_is_not_open(office_world, monkeypatch):
    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    _change(session["session_id"], state="closed", save_seq=2, last_committed_seq=2)
    with _content_origin({"/cache/output.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        payload = recorded_status_6_payload(
            document_key=session["document_key"],
            url=server.url + "/cache/output.docx",
            save_seq=1,
            intent="persist",
        )
        response = _post(http, session, payload)
        _assert_refusal(response, 409, "session_not_open")
        assert server.requests == []
    assert origin.hits == 0


def test_epoch_mismatch_orphans_before_replay(office_world, monkeypatch):
    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    marker = data / ".office-restore-epoch"
    marker.write_text("new-epoch", encoding="utf-8")
    with _content_origin({"/cache/output.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        payload = recorded_status_2_payload(
            document_key=session["document_key"],
            url=server.url + "/cache/output.docx",
        )
        response = _post(http, session, payload)
        _assert_refusal(response, 409, "session_not_open")
        assert server.requests == []
    record = json.loads(_state(data).read_bytes())["sessions"][session["session_id"]]
    assert record["state"] == "orphaned"
    assert record["reason"] == "restore_epoch_changed"
    assert origin.hits == 0


def test_download_origin_matrix_and_content_failures(office_world, monkeypatch):
    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    _change(
        session["session_id"],
        state="saving",
        save_seq=1,
        pending_save_seq=1,
        save_intents={"1": "persist"},
    )
    with _content_origin({"/cache/ok.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        browser = recorded_status_6_payload(
            document_key=session["document_key"],
            url=OFFICE_SETTINGS["OCU_OFFICE_DOCSERVER_ORIGIN"] + "/cache/ok.docx",
            save_seq=1,
            intent="persist",
        )
        assert _post(http, session, browser).status_code == 200
        _change(
            session["session_id"],
            state="saving",
            save_seq=2,
            pending_save_seq=2,
            save_intents={"1": "persist", "2": "persist"},
            last_committed_seq=1,
        )
        internal = recorded_status_6_payload(
            document_key=session["document_key"],
            url=server.url + "/cache/ok.docx",
            save_seq=2,
            intent="persist",
        )
        assert _post(http, session, internal).status_code == 200
        foreign = recorded_status_6_payload(
            document_key=session["document_key"],
            url="http://127.0.0.1:9/cache/ok.docx",
            save_seq=2,
            intent="persist",
        )
        hits = server.hits
        response = _post(http, session, foreign)
        _assert_refusal(response, 422, "download_url_rejected")
        assert server.hits == hits
    with (
        _content_origin({"/start": b""}, redirect=("/start", "http://example.test/elsewhere")) as redirected,
        _bind_internal(monkeypatch, redirected.url),
    ):
        _change(
            session["session_id"],
            state="saving",
            save_seq=3,
            pending_save_seq=3,
            save_intents={"3": "persist"},
            last_committed_seq=2,
        )
        payload = recorded_status_6_payload(
            document_key=session["document_key"],
            url=redirected.url + "/start",
            save_seq=3,
            intent="persist",
        )
        response = _post(http, session, payload)
        _assert_refusal(response, 502, "download_failed")
        record = json.loads(_state(data).read_bytes())["sessions"][session["session_id"]]
        assert record["state"] == "editing"
        assert record["reason"] == "download_failed"
        assert "3" not in json.loads(_state(data).read_bytes())["receipts"].get(session["session_id"], {})
    with _content_origin({"/cache/sheet.xlsx": intact_xlsx()}) as typed, _bind_internal(monkeypatch, typed.url):
        _change(
            session["session_id"],
            state="saving",
            save_seq=4,
            pending_save_seq=4,
            save_intents={"4": "persist"},
            last_committed_seq=2,
            reason=None,
        )
        payload = recorded_status_6_payload(
            document_key=session["document_key"],
            url=typed.url + "/cache/sheet.xlsx",
            save_seq=4,
            intent="persist",
        )
        response = _post(http, session, payload)
        _assert_refusal(response, 422, "invalid_content")
        record = json.loads(_state(data).read_bytes())["sessions"][session["session_id"]]
        assert record["state"] == "editing"
        assert record["reason"] == "invalid_content"
    with _content_origin({"/cache/plain": b"not-ooxml"}) as plain, _bind_internal(monkeypatch, plain.url):
        _change(
            session["session_id"],
            state="saving",
            save_seq=5,
            pending_save_seq=5,
            save_intents={"5": "persist"},
            last_committed_seq=2,
        )
        payload = recorded_status_6_payload(
            document_key=session["document_key"],
            url=plain.url + "/cache/plain",
            save_seq=5,
            intent="persist",
        )
        response = _post(http, session, payload)
        _assert_refusal(response, 422, "invalid_content")
    assert origin.hits == 0


def test_oversized_download_leaves_no_staging(office_world, monkeypatch):
    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    import office.download as download_mod

    monkeypatch.setattr(download_mod, "MAX_FILE_SIZE", 8)
    _change(
        session["session_id"],
        state="saving",
        save_seq=1,
        pending_save_seq=1,
        save_intents={"1": "persist"},
    )
    with _content_origin({"/cache/big.docx": b"0123456789"}) as server, _bind_internal(monkeypatch, server.url):
        payload = recorded_status_6_payload(
            document_key=session["document_key"],
            url=server.url + "/cache/big.docx",
            save_seq=1,
            intent="persist",
        )
        response = _post(http, session, payload)
        _assert_refusal(response, 413, "file_too_large")
    office = data / CHAT / ".ocu" / "office"
    staging = office / "staging"
    if staging.exists():
        assert list(staging.iterdir()) == []
    blobs = list((office / "versions").iterdir()) if (office / "versions").exists() else []
    assert all(path.name != _sha(b"0123456789") for path in blobs)
    assert origin.hits == 0


def test_callback_dns_stall_returns_before_resolver_executor_shutdown(office_world, monkeypatch):
    import socket
    import time

    import office.commands as commands

    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    _change(
        session["session_id"],
        state="saving",
        save_seq=1,
        pending_save_seq=1,
        save_intents={"1": "persist"},
    )
    before = json.loads(_state(data).read_bytes())
    before_versions = list(before["documents"][session["file_id"]]["versions"])
    before_receipts = json.loads(json.dumps(before["receipts"]))
    monkeypatch.setattr(commands, "HTTP_TIMEOUT_SECONDS", 0.05)
    original = socket.getaddrinfo
    calls = []
    release = threading.Event()

    def delayed(host, port, *args, **kwargs):
        if str(host) == "slow-resolution.example":
            calls.append(str(host))
            release.wait(0.3)
            return original("127.0.0.1", port, *args, **kwargs)
        return original(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", delayed)
    try:
        with (
            _content_origin({"/cache/ok.docx": CHANGED}) as server,
            _bind_internal(monkeypatch, f"http://slow-resolution.example:{urlsplit(server.url).port}") as host_url,
        ):
            payload = recorded_status_6_payload(
                document_key=session["document_key"],
                url=host_url + "/cache/ok.docx",
                save_seq=1,
                intent="persist",
            )
            started = time.monotonic()
            response = _post(http, session, payload)
            elapsed = time.monotonic() - started
            assert elapsed < 0.2
            _assert_refusal(response, 502, "download_failed")
            assert calls == ["slow-resolution.example"]
            after = json.loads(_state(data).read_bytes())
            record = after["sessions"][session["session_id"]]
            assert record["state"] == "editing"
            assert record["reason"] == "download_failed"
            assert after["receipts"] == before_receipts
            assert after["documents"][session["file_id"]]["versions"] == before_versions
            assert (_outputs(data) / "report.docx").read_bytes() == content
            assert server.hits == 0
    finally:
        release.set()
    assert origin.hits == 0


def test_callback_persists_when_default_executor_has_one_worker(office_world, monkeypatch):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    _change(
        session["session_id"],
        state="saving",
        save_seq=1,
        pending_save_seq=1,
        save_intents={"1": "persist"},
    )
    limited = ThreadPoolExecutor(max_workers=1)

    def limit_executor():
        asyncio.get_running_loop().set_default_executor(limited)

    http.portal.call(limit_executor)
    with (
        _content_origin({"/cache/ok.docx": CHANGED}) as server,
        _bind_internal(monkeypatch, f"http://localhost:{urlsplit(server.url).port}") as host_url,
    ):
        payload = recorded_status_6_payload(
            document_key=session["document_key"],
            url=host_url + "/cache/ok.docx",
            save_seq=1,
            intent="persist",
        )
        first = _post(http, session, payload)
        assert first.status_code == 200
        assert first.json() == {"error": 0}
        listed = json.loads(_state(data).read_bytes())["documents"][session["file_id"]]["versions"]
        receipt = _receipt(data, session["session_id"], 1)
        assert receipt["sha256"] == _sha(CHANGED)
        assert listed[-1]["sha256"] == _sha(CHANGED)
        hits = server.hits
        replay = _post(http, session, payload)
        assert replay.status_code == 200
        assert replay.json() == {"error": 0}
        assert server.hits == hits + 1
        after = json.loads(_state(data).read_bytes())
        assert after["receipts"][session["session_id"]]["1"]["sha256"] == _sha(CHANGED)
        assert after["sessions"][session["session_id"]]["save_seq"] == 1
        assert (_outputs(data) / "report.docx").read_bytes() == content
    assert origin.hits == 0


def test_committed_seq3_then_seq4_replay_same_bytes_and_reject_different(office_world, monkeypatch):
    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    first = CHANGED
    second = CHANGED + b"2"
    _change(
        session["session_id"],
        state="saving",
        save_seq=4,
        pending_save_seq=3,
        save_intents={"3": "persist", "4": "persist"},
        last_committed_seq=0,
    )
    with _content_origin({"/cache/3.docx": first, "/cache/4.docx": second}) as server, _bind_internal(monkeypatch, server.url):
        three = recorded_status_6_payload(
            document_key=session["document_key"],
            url=server.url + "/cache/3.docx",
            save_seq=3,
            intent="persist",
        )
        assert _post(http, session, three).status_code == 200
        after_three = json.loads(_state(data).read_bytes())
        listed_three = after_three["documents"][session["file_id"]]["versions"]
        assert listed_three[-1]["sha256"] == _sha(first)
        assert after_three["sessions"][session["session_id"]]["last_committed_seq"] == 3
        _change(session["session_id"], pending_save_seq=4)
        four = recorded_status_6_payload(
            document_key=session["document_key"],
            url=server.url + "/cache/4.docx",
            save_seq=4,
            intent="persist",
        )
        assert _post(http, session, four).status_code == 200
        after_four = json.loads(_state(data).read_bytes())
        listed = after_four["documents"][session["file_id"]]["versions"]
        assert listed[-1]["sha256"] == _sha(second)
        assert after_four["sessions"][session["session_id"]]["last_committed_seq"] == 4
        receipts = after_four["receipts"][session["session_id"]]
        assert set(receipts) == {"3", "4"}
        index_before = (data / CHAT / ".ocu" / "index.json").read_bytes()
        hits = server.hits
        same = _post(http, session, three)
        assert same.status_code == 200
        assert same.json() == {"error": 0}
        assert server.hits == hits + 1
        different = recorded_status_6_payload(
            document_key=session["document_key"],
            url=server.url + "/cache/4.docx",
            save_seq=3,
            intent="persist",
        )
        response = _post(http, session, different)
        _assert_refusal(response, 409, "stale_save_seq")
        assert server.hits == hits + 2
        after = json.loads(_state(data).read_bytes())
        assert after["documents"][session["file_id"]]["versions"] == listed
        assert after["receipts"][session["session_id"]] == receipts
        assert after["sessions"][session["session_id"]]["save_seq"] == 4
        assert after["sessions"][session["session_id"]]["last_committed_seq"] == 4
        assert (data / CHAT / ".ocu" / "index.json").read_bytes() == index_before
    assert origin.hits == 0


def test_route_timeout_returns_editing_then_same_signed_retry_commits(office_world, monkeypatch):
    import office.commands as commands

    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    _change(
        session["session_id"],
        state="saving",
        save_seq=1,
        pending_save_seq=1,
        save_intents={"1": "persist"},
    )
    monkeypatch.setattr(commands, "HTTP_TIMEOUT_SECONDS", 0.05)
    with _content_origin({"/cache/ok.docx": CHANGED}, delay=0.3) as server, _bind_internal(monkeypatch, server.url):
        payload = recorded_status_6_payload(
            document_key=session["document_key"],
            url=server.url + "/cache/ok.docx",
            save_seq=1,
            intent="persist",
        )
        before = json.loads(_state(data).read_bytes())
        first = _post(http, session, payload)
        _assert_refusal(first, 502, "download_failed")
        after_fail = json.loads(_state(data).read_bytes())
        record = after_fail["sessions"][session["session_id"]]
        assert record["state"] == "editing"
        assert record["reason"] == "download_failed"
        assert record["save_seq"] == 1
        assert record["pending_save_seq"] is None
        assert after_fail["receipts"] == before["receipts"]
        listed = after_fail["documents"][session["file_id"]]["versions"]
        assert all(item["sha256"] != _sha(CHANGED) for item in listed)
        office = data / CHAT / ".ocu" / "office"
        staging = office / "staging"
        if staging.exists():
            assert list(staging.iterdir()) == []
        monkeypatch.setattr(commands, "HTTP_TIMEOUT_SECONDS", 10)
        retry = _post(http, session, payload)
        assert retry.status_code == 200
        after = json.loads(_state(data).read_bytes())
        committed = after["sessions"][session["session_id"]]
        assert committed["last_committed_seq"] == 1
        assert committed["save_seq"] == 1
        assert after["receipts"][session["session_id"]]["1"]["sha256"] == _sha(CHANGED)
        assert (_outputs(data) / "report.docx").read_bytes() == content
    assert origin.hits == 0


def test_epoch_change_after_published_final_receipt_preserves_closed_without_fetch(office_world, monkeypatch):
    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    opened = _post(http, session, recorded_status_1_payload(document_key=session["document_key"]))
    assert opened.status_code == 200
    with _content_origin({"/cache/close.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        payload = recorded_status_2_payload(
            document_key=session["document_key"],
            url=server.url + "/cache/close.docx",
        )
        first = _post(http, session, payload)
        assert first.status_code == 200
        before = json.loads(_state(data).read_bytes())
        record = before["sessions"][session["session_id"]]
        assert record["state"] == "closed"
        receipts = before["receipts"][session["session_id"]]
        listed = before["documents"][session["file_id"]]["versions"]
        workspace = (_outputs(data) / "report.docx").read_bytes()
        index = (data / CHAT / ".ocu" / "index.json").read_bytes()
        hits = server.hits
        (data / ".office-restore-epoch").write_text("new-epoch", encoding="utf-8")
        replay = _post(http, session, payload)
        assert replay.status_code == 200
        assert replay.json() == {"error": 0}
        assert server.hits == hits
        after = json.loads(_state(data).read_bytes())
        assert after["sessions"][session["session_id"]]["state"] == "closed"
        assert after["sessions"][session["session_id"]]["reason"] is None
        assert after["receipts"][session["session_id"]] == receipts
        assert after["documents"][session["file_id"]]["versions"] == listed
        assert (_outputs(data) / "report.docx").read_bytes() == workspace
        assert (data / CHAT / ".ocu" / "index.json").read_bytes() == index
    assert origin.hits == 0


def test_gzip_negotiating_origin_persists_identity_ooxml(office_world, monkeypatch):
    import gzip

    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    packed = gzip.compress(CHANGED)
    _change(
        session["session_id"],
        state="saving",
        save_seq=1,
        pending_save_seq=1,
        save_intents={"1": "persist"},
    )

    class EncodingOrigin(_ContentOrigin):
        def __init__(self):
            self.requests = []
            self.hits = 0
            parent = self

            class Handler(BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def log_message(self, format, *args):
                    return

                def do_GET(self):
                    parent.hits += 1
                    headers = {key.lower(): value for key, value in self.headers.items()}
                    parent.requests.append({"path": self.path, "headers": headers})
                    accept = headers.get("accept-encoding", "")
                    if "gzip" in accept.lower() and "identity" not in accept.lower():
                        body = packed
                        encoding = "gzip"
                    else:
                        body = CHANGED
                        encoding = "identity"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Encoding", encoding)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()

    server = EncodingOrigin()
    try:
        with _bind_internal(monkeypatch, server.url):
            payload = recorded_status_6_payload(
                document_key=session["document_key"],
                url=server.url + "/cache/ok.docx",
                save_seq=1,
                intent="persist",
            )
            response = _post(http, session, payload)
            assert response.status_code == 200
            listed = json.loads(_state(data).read_bytes())["documents"][session["file_id"]]["versions"]
            assert listed[-1]["sha256"] == _sha(CHANGED)
            assert listed[-1]["sha256"] != _sha(packed)
            assert server.requests[0]["headers"].get("accept-encoding") == "identity"
    finally:
        server.close()
    assert origin.hits == 0


def test_malformed_receipt_answer_is_state_corrupt_without_fetch(office_world, monkeypatch):
    from office.store import OfficeStore

    http, data, origin, _docker, _broker, content, session = _open_session(office_world)

    def plant_final(working):
        working["receipts"][session["session_id"]] = {
            "1": {
                "status": 2,
                "sha256": "a" * 64,
                "version": 1,
                "answer": "not-an-object",
            }
        }

    OfficeStore().update(CHAT, plant_final)
    with _content_origin({"/cache/close.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        payload = recorded_status_2_payload(
            document_key=session["document_key"],
            url=server.url + "/cache/close.docx",
        )
        before = json.loads(_state(data).read_bytes())
        response = _post(http, session, payload)
        _assert_refusal(response, 500, "state_corrupt")
        assert server.requests == []
        after = json.loads(_state(data).read_bytes())
        assert after == before

    def plant_forcesave(working):
        working["sessions"][session["session_id"]]["state"] = "saving"
        working["sessions"][session["session_id"]]["save_seq"] = 1
        working["sessions"][session["session_id"]]["pending_save_seq"] = 1
        working["sessions"][session["session_id"]]["save_intents"] = {"1": "persist"}
        working["receipts"][session["session_id"]] = {
            "1": {
                "status": 6,
                "sha256": "b" * 64,
                "version": 1,
                "answer": {"error": True},
            }
        }

    OfficeStore().update(CHAT, plant_forcesave)
    with _content_origin({"/cache/ok.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        payload = recorded_status_6_payload(
            document_key=session["document_key"],
            url=server.url + "/cache/ok.docx",
            save_seq=1,
            intent="persist",
        )
        before = json.loads(_state(data).read_bytes())
        response = _post(http, session, payload)
        _assert_refusal(response, 500, "state_corrupt")
        assert server.requests == []
        after = json.loads(_state(data).read_bytes())
        assert after == before
    assert origin.hits == 0


def test_storage_low_returns_session_to_editing_and_retry_commits(office_world, monkeypatch):
    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    import office.versions as versions_mod

    calls = {"n": 0}
    original = versions_mod.check_free_space

    def limited(store, chat_id, min_free_bytes):
        calls["n"] += 1
        if calls["n"] == 1:
            raise versions_mod.StorageLowError("office storage is below the configured floor")
        return original(store, chat_id, min_free_bytes)

    monkeypatch.setattr(versions_mod, "check_free_space", limited)
    _change(
        session["session_id"],
        state="saving",
        save_seq=1,
        pending_save_seq=1,
        save_intents={"1": "persist"},
    )
    with _content_origin({"/cache/ok.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        payload = recorded_status_6_payload(
            document_key=session["document_key"],
            url=server.url + "/cache/ok.docx",
            save_seq=1,
            intent="persist",
        )
        response = _post(http, session, payload)
        _assert_refusal(response, 503, "storage_low")
        record = json.loads(_state(data).read_bytes())["sessions"][session["session_id"]]
        assert record["state"] == "editing"
        assert record["reason"] == "storage_low"
        assert record["last_committed_seq"] == 0
        assert record["save_seq"] == 1
        assert record["pending_save_seq"] is None
        assert record["save_intents"] == {"1": "persist"}
        assert json.loads(_state(data).read_bytes())["receipts"] == {}
        retry = _post(http, session, payload)
        assert retry.status_code == 200
        record = json.loads(_state(data).read_bytes())["sessions"][session["session_id"]]
        assert record["last_committed_seq"] == 1
        assert record["save_seq"] == 1
        listed = json.loads(_state(data).read_bytes())["documents"][session["file_id"]]["versions"]
        assert listed[-1]["sha256"] == _sha(CHANGED)
    assert origin.hits == 0


def test_equal_latest_content_adds_no_version(office_world, monkeypatch):
    http, data, origin, _docker, broker, content, session = _open_session(office_world)
    opened = _post(http, session, recorded_status_1_payload(document_key=session["document_key"]))
    assert opened.status_code == 200
    with _forcesave(monkeypatch, session["document_key"]) as (_origin, issued):
        accepted = _save(http, session["session_id"], "persist")
    assert accepted.status_code == 202
    assert issued == [{"save_seq": 1, "intent": "persist"}]
    revision = broker.OutputsBroker().current_revision(CHAT)
    with _content_origin({"/cache/same.docx": content}) as server, _bind_internal(monkeypatch, server.url):
        payload = recorded_status_6_payload(
            document_key=session["document_key"],
            url=server.url + "/cache/same.docx",
            save_seq=issued[0]["save_seq"],
            intent=issued[0]["intent"],
        )
        before = json.loads(_state(data).read_bytes())["documents"][session["file_id"]]["versions"]
        assert _post(http, session, payload).status_code == 200
        after = json.loads(_state(data).read_bytes())["documents"][session["file_id"]]["versions"]
        assert after == before
        assert after[-1]["published"] is True
        record = json.loads(_state(data).read_bytes())["sessions"][session["session_id"]]
        assert record["last_committed_seq"] == 1
        assert record["last_published_seq"] == 1
        assert record["state"] == "editing"
        assert record["pending_save_seq"] is None
        assert record["baseline_sha256"] == _sha(content)
        receipt = _receipt(data, session["session_id"], 1)
        assert receipt["version"] == after[-1]["number"]
        assert receipt["sha256"] == after[-1]["sha256"]
        assert json.loads(_state(data).read_bytes())["journal"] == {}
        assert (_outputs(data) / "report.docx").read_bytes() == content
        assert broker.OutputsBroker().current_revision(CHAT) == revision
    assert origin.hits == 0


def test_status2_precommit_state_write_eio_is_structured_then_retry_commits(
    office_world, monkeypatch
):
    import errno
    from office.store import OfficeStore

    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    monkeypatch.setattr(http._transport, "raise_server_exceptions", False)
    original = OfficeStore._write_all
    fired = []
    office = data / CHAT / ".ocu" / "office"
    digest = _sha(CHANGED)

    def fail_state_write(fd, encoded):
        if not fired and encoded.startswith(b'{"documents":'):
            blob = office / "versions" / digest
            assert blob.is_file()
            assert blob.read_bytes() == CHANGED
            fired.append(True)
            raise OSError(errno.EIO, "injected precommit state write failure")
        return original(fd, encoded)

    with _content_origin({"/close.docx": CHANGED}) as server, _bind_internal(monkeypatch, server.url):
        payload = recorded_status_2_payload(
            document_key=session["document_key"],
            url=server.url + "/close.docx",
        )
        before = _snapshot(data)
        before_state = json.loads(_state(data).read_bytes())
        before_versions = list(before_state["documents"][session["file_id"]]["versions"])
        before_workspace = (_outputs(data) / "report.docx").read_bytes()
        before_index = (data / CHAT / ".ocu" / "index.json").read_bytes()
        monkeypatch.setattr(OfficeStore, "_write_all", staticmethod(fail_state_write))
        response = _post(http, session, payload)
        after = _snapshot(data)
        assert fired == [True]
        assert after == before
        staging = office / "staging"
        if staging.exists():
            assert list(staging.iterdir()) == []
        assert not (office / "versions" / digest).exists()
        assert response.headers["content-type"].startswith("application/json")
        _assert_refusal(response, 500, "state_corrupt")
        assert len(server.requests) == 1
        retry = _post(http, session, payload)
        assert retry.status_code == 200
        assert retry.json() == {"error": 0}
        assert fired == [True]
        listed, receipts, journal = _history(data, session["file_id"])
        record = json.loads(_state(data).read_bytes())["sessions"][session["session_id"]]
        assert len(listed) == len(before_versions) + 1
        assert listed[-1]["sha256"] == digest
        assert listed[-1]["source"] == "close"
        assert listed[-1]["published"] is True
        assert receipts == {
            session["session_id"]: {
                "1": {
                    "status": 2,
                    "sha256": digest,
                    "version": listed[-1]["number"],
                    "answer": {"error": 0},
                }
            }
        }
        assert journal == {}
        assert record["state"] == "closed"
        assert record["last_published_seq"] == 1
        assert record["baseline_sha256"] == digest
        assert record["last_committed_seq"] == 1
        assert record["save_seq"] == 1
        assert (_outputs(data) / "report.docx").read_bytes() == CHANGED
        assert (_outputs(data) / "report.docx").read_bytes() != before_workspace
        published_index = json.loads((data / CHAT / ".ocu" / "index.json").read_bytes())
        assert published_index["counter"] == json.loads(before_index)["counter"] + 1
        assert published_index["active"]["report.docx"]["hash"] == digest
        replay = _post(http, session, payload)
        assert replay.status_code == 200
        assert replay.json() == {"error": 0}
        replay_listed, replay_receipts, replay_journal = _history(
            data, session["file_id"]
        )
        assert replay_listed == listed
        assert replay_receipts == receipts
        assert replay_journal == journal
        assert len(server.requests) == 2
    assert origin.hits == 0


def test_status1_precommit_state_write_eio_is_structured_then_retry_commits(
    office_world, monkeypatch
):
    import errno
    from office.store import OfficeStore

    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    monkeypatch.setattr(http._transport, "raise_server_exceptions", False)
    original = OfficeStore._write_all
    fired = []

    def fail_state_write(fd, encoded):
        if not fired and encoded.startswith(b'{"documents":'):
            fired.append(True)
            raise OSError(errno.EIO, "injected precommit state write failure")
        return original(fd, encoded)

    payload = recorded_status_1_payload(document_key=session["document_key"])
    before = _snapshot(data)
    before_state = json.loads(_state(data).read_bytes())
    before_versions = list(before_state["documents"][session["file_id"]]["versions"])
    before_workspace = (_outputs(data) / "report.docx").read_bytes()
    before_index = (data / CHAT / ".ocu" / "index.json").read_bytes()
    monkeypatch.setattr(OfficeStore, "_write_all", staticmethod(fail_state_write))
    response = _post(http, session, payload)
    after = _snapshot(data)
    assert fired == [True]
    assert after == before
    office = data / CHAT / ".ocu" / "office"
    staging = office / "staging"
    if staging.exists():
        assert list(staging.iterdir()) == []
    assert response.headers["content-type"].startswith("application/json")
    _assert_refusal(response, 500, "state_corrupt")
    retry = _post(http, session, payload)
    assert retry.status_code == 200
    assert retry.json() == {"error": 0}
    assert fired == [True]
    listed, receipts, journal = _history(data, session["file_id"])
    record = json.loads(_state(data).read_bytes())["sessions"][session["session_id"]]
    assert listed == before_versions
    assert receipts == {}
    assert journal == {}
    assert record["state"] == "editing"
    assert record["participants"] == RECORDED_STATUS_1_USERS
    assert (_outputs(data) / "report.docx").read_bytes() == before_workspace
    assert (data / CHAT / ".ocu" / "index.json").read_bytes() == before_index
    replay = _post(http, session, payload)
    assert replay.status_code == 200
    replay_listed, replay_receipts, replay_journal = _history(data, session["file_id"])
    replay_record = json.loads(_state(data).read_bytes())["sessions"][
        session["session_id"]
    ]
    assert replay_listed == listed
    assert replay_receipts == receipts
    assert replay_journal == journal
    assert replay_record["state"] == "editing"
    assert origin.hits == 0


