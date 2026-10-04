# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Public HTTP seam for Office session creation."""
from __future__ import annotations

import base64
import errno
import hashlib
import hmac
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.parse import quote, urlsplit

import pytest

from tests.orchestrator.test_office_ooxml import (
    WORD_NS,
    WORD_TYPE,
    _content_types,
    _main,
    _member_utf8_name,
    _nul_unused_member_package,
    _package,
    _rels,
    _unsupported_codec_package,
    intact_docx,
    intact_pptx,
    intact_xlsx,
    many_member_pptx,
)
from tests.orchestrator.test_office_router import OFFICE_SETTINGS, _office_app
from tests.orchestrator.test_office_workspace import _forbid_inode_open
from tests.orchestrator.test_outputs_endpoint import CHAT, CHAT_B, INTERNAL, MCP_KEY, _auth

SERVER_DIR = Path(__file__).resolve().parents[2] / "computer-use-server"
JWT_SECRET = OFFICE_SETTINGS["OCU_OFFICE_JWT_SECRET"]
SELF_URL = OFFICE_SETTINGS["OCU_OFFICE_SELF_URL"]
GATEWAY_HOST = "webui.example"
MODEL_KEY = "model-api-key-canary"
SPARSE_SIZE = 100 * 1024 * 1024 + 1
UNSUPPORTED = (
    ("legacy.doc", b"DOC"),
    ("sheet.xls", b"XLS"),
    ("deck.ppt", b"PPT"),
    ("macro.docm", b"DOCM"),
    ("notes.pdf", b"%PDF-1.4"),
    ("plain.txt", b"plain"),
)
OPEN_STATES = ("opening", "editing", "saving", "closing", "conflict")
FINAL_STATES = ("closed", "error", "orphaned")


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _office(data: Path, chat: str = CHAT) -> Path:
    return data / chat / ".ocu" / "office"


def _state(data: Path, chat: str = CHAT) -> Path:
    return _office(data, chat) / "state.json"


def _versions(data: Path, chat: str = CHAT) -> Path:
    return _office(data, chat) / "versions"


def _outputs(data: Path, chat: str = CHAT) -> Path:
    return data / chat / "outputs"


def _put(data: Path, relative: str, body: bytes, chat: str = CHAT) -> Path:
    path = _outputs(data, chat) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _index_file(broker_module, data: Path, relative: str, chat: str = CHAT) -> str:
    listing = broker_module.OutputsBroker().reconcile(chat)
    entry = next(item for item in listing["entries"] if item["path"] == relative)
    return entry["file_id"]


def _snapshot(root: Path) -> dict:
    files = {}
    if not root.exists():
        return files
    for path in root.rglob("*"):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            files[relative] = ("link", os.readlink(path))
        elif path.is_file():
            files[relative] = path.read_bytes()
    return files


def _assert_activity_only(before: dict, after: dict, session_id: str) -> None:
    def normalized(snapshot):
        result = dict(snapshot)
        for path, content in snapshot.items():
            if path.endswith("/.ocu/office/state.json"):
                state = json.loads(content)
                record = state["sessions"].get(session_id)
                if record is not None:
                    record.pop("last_activity_at", None)
                result[path] = state
        return result
    assert normalized(after) == normalized(before)


def _create(http, file_id: str, chat: str = CHAT):
    return http.post(
        f"/api/office/{chat}/documents/{quote(file_id, safe='')}/sessions", headers=_auth()
    )


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * ((4 - len(segment) % 4) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _oracle_verify(token: str, secret: str | bytes) -> dict:
    header_b64, payload_b64, signature_b64 = token.split(".")
    secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
    expected = hmac.new(
        secret_bytes, f"{header_b64}.{payload_b64}".encode("ascii"), hashlib.sha256
    ).digest()
    presented = _b64url_decode(signature_b64)
    assert hmac.compare_digest(presented, expected)
    return json.loads(_b64url_decode(payload_b64))


def _ticket_key(internal: str) -> bytes:
    return hmac.new(internal.encode("utf-8"), b"ocu-office-source-ticket", hashlib.sha256).digest()


def _assert_no_secrets(response, *hidden: str) -> None:
    text = response.text
    header_blob = " ".join(f"{name}:{value}" for name, value in response.headers.items())
    combined = text + " " + header_blob
    for value in hidden:
        assert value not in combined


def _assert_refusal(response, status: int, reason: str) -> None:
    assert response.status_code == status
    assert json.loads(response.content) == {"reason": reason}


def _seed_session(store_mod, file_id: str, state: str, key: str = "closed-key") -> None:
    def mutate(working):
        working["sessions"][f"sess-{state}"] = {
            "session_id": f"sess-{state}",
            "file_id": file_id,
            "document_key": key,
            "baseline_sha256": "a" * 64,
            "restore_epoch": None,
            "state": state,
            "save_seq": 0,
        }

    store_mod.OfficeStore().update(CHAT, mutate)


def _close_session(store_mod, session_id):
    def mutate(state):
        state["sessions"][session_id]["state"] = "closed"
    store_mod.OfficeStore().update(CHAT, mutate)


@pytest.fixture
def office_world(tmp_path, monkeypatch):
    with _office_app(tmp_path, monkeypatch, enabled=True) as (
        http,
        data,
        origin,
        docker_manager,
    ):
        monkeypatch.setenv("MCP_API_KEY", MCP_KEY)
        monkeypatch.setenv("OPENAI_API_KEY", MODEL_KEY)
        import outputs_broker

        (data / CHAT).mkdir()
        (data / CHAT_B).mkdir()
        with docker_manager._combined_lock(CHAT):
            pass
        yield http, data, origin, docker_manager, outputs_broker


def test_unknown_malformed_and_tombstoned_ids_are_unknown_file(office_world):
    http, data, origin, _docker_manager, broker_module = office_world
    _put(data, "gone.docx", intact_docx())
    live_id = _index_file(broker_module, data, "gone.docx")
    (_outputs(data) / "gone.docx").unlink()
    broker_module.OutputsBroker().reconcile(CHAT)
    _seed_refusal_history()
    before = _snapshot(data)
    for file_id in ("missing-file", "not a uuid\x00", live_id):
        _assert_refusal(_create(http, file_id), 404, "unknown_file")
    assert _snapshot(data) == before
    assert origin.hits == 0
    assert set(json.loads(_state(data).read_bytes())["documents"]) == {"history-document"}


@pytest.mark.parametrize("name,body", UNSUPPORTED)
def test_unsupported_extensions_are_unsupported_type(office_world, name, body):
    http, data, origin, _docker_manager, broker_module = office_world
    _put(data, name, body)
    file_id = _index_file(broker_module, data, name)
    _seed_refusal_history()
    before = _snapshot(data)
    _assert_refusal(_create(http, file_id), 415, "unsupported_type")
    assert _snapshot(data) == before
    downloaded = http.get(f"/files/{CHAT}/{name}", headers=_auth())
    assert downloaded.status_code == 200
    assert downloaded.content == body
    assert origin.hits == 0


def test_sparse_oversize_xlsx_is_file_too_large_without_materializing(office_world, monkeypatch):
    http, data, origin, _docker_manager, broker_module = office_world
    import office.workspace as workspace_mod

    path = _put(data, "huge.xlsx", b"")
    file_id = _index_file(broker_module, data, "huge.xlsx")
    os.truncate(path, SPARSE_SIZE)
    identity = path.stat()
    reads = {"count": 0}
    original_read = workspace_mod.os.read

    def count_read(fd, size):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
            reads["count"] += 1
        return original_read(fd, size)

    monkeypatch.setattr(workspace_mod.os, "read", count_read)
    _seed_refusal_history()
    before = _snapshot(data)
    _assert_refusal(_create(http, file_id), 413, "file_too_large")
    assert reads["count"] == 0
    assert _snapshot(data) == before
    downloaded = http.get(f"/files/{CHAT}/huge.xlsx", headers=_auth())
    assert downloaded.status_code == 200
    assert len(downloaded.content) == SPARSE_SIZE
    assert origin.hits == 0


def test_symlink_leaf_parent_and_nonregular_are_unsafe_path(office_world, monkeypatch):
    http, data, origin, _docker_manager, broker_module = office_world
    import office.workspace as workspace_mod

    external = data.parent / "external-documents"
    external.mkdir()
    secret = external / "parent.docx"
    secret.write_bytes(b"EXTERNAL-TARGET-BYTES")
    _put(data, "leaf.docx", intact_docx())
    _put(data, "nested/parent.docx", intact_docx())
    _put(data, "pipe.docx", intact_docx())
    _put(data, "folder.docx", intact_docx())
    listing = broker_module.OutputsBroker().reconcile(CHAT)
    ids = {entry["path"]: entry["file_id"] for entry in listing["entries"]}
    leaf_id = ids["leaf.docx"]
    parent_id = ids["nested/parent.docx"]
    pipe_id = ids["pipe.docx"]
    folder_id = ids["folder.docx"]
    assert len({leaf_id, parent_id, pipe_id, folder_id}) == 4
    (_outputs(data) / "leaf.docx").unlink()
    os.symlink(secret, _outputs(data) / "leaf.docx")
    nested = _outputs(data) / "nested"
    (nested / "parent.docx").unlink()
    nested.rmdir()
    os.symlink(external, nested)
    (_outputs(data) / "pipe.docx").unlink()
    os.mkfifo(_outputs(data) / "pipe.docx")
    (_outputs(data) / "folder.docx").unlink()
    (_outputs(data) / "folder.docx").mkdir()
    _forbid_inode_open(workspace_mod, monkeypatch, external, secret)
    _seed_refusal_history()
    before = _snapshot(data)
    for file_id in (leaf_id, parent_id, pipe_id, folder_id):
        _assert_refusal(_create(http, file_id), 422, "unsafe_path")
    assert _snapshot(data) == before
    assert secret.read_bytes() == b"EXTERNAL-TARGET-BYTES"
    if _versions(data).exists():
        for blob in _versions(data).iterdir():
            assert blob.read_bytes() != b"EXTERNAL-TARGET-BYTES"
    staging = _office(data) / "staging"
    if staging.exists():
        for leftover in staging.rglob("*"):
            if leftover.is_file():
                assert leftover.read_bytes() != b"EXTERNAL-TARGET-BYTES"
    assert origin.hits == 0


def test_corrupt_empty_and_wrong_type_zip_are_corrupt_document(office_world):
    http, data, origin, _docker_manager, broker_module = office_world
    cases = (
        ("report.docx", b"not-ooxml"),
        ("empty.docx", b""),
        ("mismatch.docx", intact_xlsx()),
    )
    before_ids = []
    for name, body in cases:
        _put(data, name, body)
        before_ids.append((_index_file(broker_module, data, name), name, body))
    _seed_refusal_history()
    before = _snapshot(data)
    for file_id, name, body in before_ids:
        _assert_refusal(_create(http, file_id), 422, "corrupt_document")
        downloaded = http.get(f"/files/{CHAT}/{name}", headers=_auth())
        assert downloaded.status_code == 200
        assert downloaded.content == body
    assert _snapshot(data) == before
    assert origin.hits == 0


def test_floor_refuses_even_when_latest_hash_matches(office_world, monkeypatch):
    http, data, origin, _docker_manager, broker_module = office_world
    import office.config as config_mod
    import office.store as store_mod
    import office.versions as versions_mod

    body = intact_docx()
    _put(data, "brief.docx", body)
    file_id = _index_file(broker_module, data, "brief.docx")
    store_mod.OfficeStore().store_version(
        CHAT,
        file_id,
        body,
        source="workspace",
        parent=None,
        published=True,
        min_free_bytes=0,
    )
    before = _snapshot(data)
    monkeypatch.setattr(config_mod, "MIN_FREE_BYTES", 10**12)
    seen = []

    class Info:
        f_bavail = 1
        f_frsize = 1

    def spy(fd):
        seen.append(fd)
        return Info()

    monkeypatch.setattr(versions_mod.os, "fstatvfs", spy)
    _assert_refusal(_create(http, file_id), 503, "storage_low")
    assert seen
    assert _snapshot(data) == before
    assert origin.hits == 0


@pytest.mark.parametrize(
    "name,factory,kind",
    (
        ("brief.docx", intact_docx, "docx"),
        ("sheet.xlsx", intact_xlsx, "xlsx"),
        ("deck.pptx", intact_pptx, "pptx"),
        ("BRIEF.DOCX", intact_docx, "docx"),
    ),
)
def test_three_formats_and_uppercase_create_opening_session(office_world, name, factory, kind):
    http, data, origin, _docker_manager, broker_module = office_world
    body = factory()
    _put(data, name, body)
    _put(data, "other.txt", b"foreign-chat", chat=CHAT_B)
    broker_module.OutputsBroker().reconcile(CHAT_B)
    other_before = _snapshot(data / CHAT_B)
    file_id = _index_file(broker_module, data, name)
    response = _create(http, file_id)
    assert response.status_code == 201
    payload = response.json()
    assert payload["file_id"] == file_id
    assert payload["state"] == "opening"
    assert payload["joined"] is False
    assert payload["session_id"]
    assert payload["document_key"]
    persisted = json.loads(_state(data).read_text(encoding="utf-8"))
    document = persisted["documents"][file_id]
    session = persisted["sessions"][payload["session_id"]]
    assert document["file_id"] == file_id
    assert document["type"] == kind
    assert document["path"] == name
    assert document["published_version"] == 1
    assert document["published_sha256"] == _sha(body)
    assert document["versions"][0]["source"] == "workspace"
    assert document["versions"][0]["published"] is True
    assert type(session["last_activity_at"]) in (int, float)
    assert session["last_activity_at"] >= 0
    assert session == {
        "session_id": payload["session_id"],
        "file_id": file_id,
        "document_key": payload["document_key"],
        "baseline_sha256": _sha(body),
        "restore_epoch": None,
        "state": "opening",
        "save_seq": 0,
        "reason": None,
        "last_committed_seq": 0,
        "last_published_seq": 0,
        "workspace_changed": False,
        "saved_as": None,
        "last_activity_at": session["last_activity_at"],
    }
    assert _versions(data).joinpath(_sha(body)).read_bytes() == body
    other_after = _snapshot(data / CHAT_B)
    assert other_after == other_before
    _assert_no_secrets(response, INTERNAL, MCP_KEY, MODEL_KEY, JWT_SECRET)
    assert origin.hits == 0


def test_workspace_capture_first_equal_latest_and_older_hash(office_world):
    http, data, origin, _docker_manager, broker_module = office_world
    import office.store as store_mod

    first = intact_docx()
    newer = intact_xlsx()
    _put(data, "brief.docx", first)
    file_id = _index_file(broker_module, data, "brief.docx")
    created = _create(http, file_id)
    assert created.status_code == 201
    first_state = json.loads(_state(data).read_text(encoding="utf-8"))
    assert [record["number"] for record in first_state["documents"][file_id]["versions"]] == [1]
    blob = _versions(data) / _sha(first)
    inode = blob.stat()

    _close_session(store_mod, created.json()["session_id"])
    again = _create(http, file_id)
    assert again.status_code == 201
    equal_state = json.loads(_state(data).read_text(encoding="utf-8"))
    versions = equal_state["documents"][file_id]["versions"]
    assert [record["number"] for record in versions] == [1]
    assert versions[0]["source"] == "workspace"
    assert versions[0]["created_at"] == first_state["documents"][file_id]["versions"][0]["created_at"]
    assert blob.stat().st_ino == inode.st_ino

    store_mod.OfficeStore().store_version(
        CHAT,
        file_id,
        newer,
        source="autosave",
        parent=1,
        published=False,
        min_free_bytes=0,
    )
    _close_session(store_mod, again.json()["session_id"])
    older = _create(http, file_id)
    assert older.status_code == 201
    older_state = json.loads(_state(data).read_text(encoding="utf-8"))
    records = older_state["documents"][file_id]["versions"]
    assert [record["number"] for record in records] == [1, 2, 3]
    assert records[2]["source"] == "workspace"
    assert records[2]["sha256"] == _sha(first)
    assert records[2]["published"] is True
    assert len(list(_versions(data).iterdir())) == 2
    assert blob.read_bytes() == first
    session = older_state["sessions"][older.json()["session_id"]]
    assert session["baseline_sha256"] == _sha(first)
    config = older.json()["editor_config"]
    signed = _oracle_verify(config["token"], JWT_SECRET)
    assert signed == {key: value for key, value in config.items() if key != "token"}
    ticket = config["document"]["url"].rsplit("/", 1)[-1]
    claims = _oracle_verify(ticket, _ticket_key(INTERNAL))
    assert claims["version"] == 3
    assert claims["session_id"] == older.json()["session_id"]
    assert origin.hits == 0


def test_editor_config_and_ticket_verify_independently(office_world):
    http, data, origin, _docker_manager, broker_module = office_world
    body = intact_docx()
    _put(data, "brief.docx", body)
    file_id = _index_file(broker_module, data, "brief.docx")
    response = _create(http, file_id)
    payload = response.json()
    editor_config = payload["editor_config"]
    config_payload = _oracle_verify(editor_config["token"], JWT_SECRET)
    assert config_payload == {key: value for key, value in editor_config.items() if key != "token"}
    document = editor_config["document"]
    editor = editor_config["editorConfig"]
    assert editor["customization"]["forcesave"] is False
    assert document["key"] == payload["document_key"]
    assert document["fileType"] == "docx"
    source = urlsplit(document["url"])
    callback = urlsplit(editor["callbackUrl"])
    assert source.scheme + "://" + source.netloc == SELF_URL
    assert callback.scheme + "://" + callback.netloc == SELF_URL
    assert GATEWAY_HOST not in document["url"]
    assert GATEWAY_HOST not in editor["callbackUrl"]
    assert source.path.startswith("/office/source/")
    assert callback.path == f"/office/callback/{CHAT}/{payload['session_id']}"
    ticket = source.path.rsplit("/", 1)[-1]
    ticket_payload = _oracle_verify(ticket, _ticket_key(INTERNAL))
    assert ticket_payload["chat_id"] == CHAT
    assert ticket_payload["file_id"] == file_id
    assert ticket_payload["session_id"] == payload["session_id"]
    assert ticket_payload["version"] == 1
    _assert_no_secrets(response, INTERNAL, MCP_KEY, MODEL_KEY, JWT_SECRET)
    assert origin.hits == 0


def test_absent_epoch_is_stored_and_present_epoch_is_copied(office_world):
    http, data, origin, _docker_manager, broker_module = office_world
    marker = data / ".office-restore-epoch"
    _put(data, "brief.docx", intact_docx())
    file_id = _index_file(broker_module, data, "brief.docx")
    first = _create(http, file_id)
    assert first.status_code == 201
    persisted = json.loads(_state(data).read_text(encoding="utf-8"))
    assert persisted["sessions"][first.json()["session_id"]]["restore_epoch"] is None
    assert not marker.exists()

    import office.store as store_mod

    _close_session(store_mod, first.json()["session_id"])
    marker.write_text("epoch-A\n", encoding="utf-8")
    second = _create(http, file_id)
    assert second.status_code == 201
    after = json.loads(_state(data).read_text(encoding="utf-8"))
    assert after["sessions"][second.json()["session_id"]]["restore_epoch"] == "epoch-A"
    assert marker.read_text(encoding="utf-8") == "epoch-A\n"
    assert origin.hits == 0


def test_foreign_file_id_is_unknown_and_does_not_touch_the_other_chat(office_world, monkeypatch):
    http, data, origin, _docker_manager, broker_module = office_world
    import office.sessions as sessions_mod
    import office.store as store_mod

    body = intact_docx()
    _put(data, "brief.docx", body, chat=CHAT_B)
    foreign_id = _index_file(broker_module, data, "brief.docx", chat=CHAT_B)

    def seed_foreign(working):
        working["documents"][foreign_id] = {
            "file_id": foreign_id,
            "published_sha256": _sha(body),
            "latest_version": _sha(body),
        }
        working["sessions"]["foreign-sess"] = {
            "session_id": "foreign-sess",
            "file_id": foreign_id,
            "document_key": "foreign-key",
            "baseline_sha256": _sha(body),
            "restore_epoch": None,
            "state": "closed",
            "save_seq": 0,
        }

    store_mod.OfficeStore().update(CHAT_B, seed_foreign)
    other_before = _snapshot(data / CHAT_B)
    _seed_refusal_history()
    own_before = _snapshot(data / CHAT)
    foreign_root = data / CHAT_B
    foreign_paths = [foreign_root, *foreign_root.rglob("*")]
    with monkeypatch.context() as nested:
        _forbid_inode_open(sessions_mod, nested, *foreign_paths)
        _assert_refusal(_create(http, foreign_id), 404, "unknown_file")
    assert _snapshot(data / CHAT_B) == other_before
    assert _snapshot(data / CHAT) == own_before
    assert set(json.loads(_state(data).read_bytes())["documents"]) == {"history-document"}
    assert json.loads(_state(data, CHAT_B).read_bytes())["sessions"]["foreign-sess"]["file_id"] == foreign_id
    assert origin.hits == 0
    downloaded = http.get(f"/files/{CHAT_B}/brief.docx", headers=_auth())
    assert downloaded.status_code == 200
    assert downloaded.content == body





@pytest.mark.parametrize("state", OPEN_STATES)
def test_existing_nonfinal_session_joins_or_preserves_unavailable_state(office_world, state):
    http, data, origin, _docker_manager, broker_module = office_world
    import office.store as store_mod

    body = intact_docx()
    _put(data, "brief.docx", body)
    file_id = _index_file(broker_module, data, "brief.docx")
    first = _create(http, file_id)
    assert first.status_code == 201
    def change_state(working):
        working["sessions"][first.json()["session_id"]]["state"] = state
    store_mod.OfficeStore().update(CHAT, change_state)
    before = _snapshot(data)
    response = _create(http, file_id)
    if state == "opening":
        assert response.status_code == 200
        assert response.json()["joined"] is True
        assert response.json()["session_id"] == first.json()["session_id"]
        assert response.json()["document_key"] == first.json()["document_key"]
    else:
        _assert_refusal(response, 502, "documentserver_unavailable")
    if state == "opening":
        _assert_activity_only(before, _snapshot(data), first.json()["session_id"])
    else:
        assert _snapshot(data) == before
    assert origin.hits == (0 if state == "opening" else 1)


@pytest.mark.parametrize("bad_state", ("edting", None, [], {}))
def test_malformed_retained_session_state_is_state_corrupt_without_mutation(office_world, bad_state):
    http, data, origin, _docker_manager, broker_module = office_world
    body = intact_docx()
    _put(data, "brief.docx", body)
    file_id = _index_file(broker_module, data, "brief.docx")
    created = _create(http, file_id)
    assert created.status_code == 201
    payload = json.loads(_state(data).read_text(encoding="utf-8"))
    payload["sessions"][created.json()["session_id"]]["state"] = bad_state
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    _state(data).write_bytes(encoded)
    before = _snapshot(data)
    _assert_refusal(_create(http, file_id), 500, "state_corrupt")
    assert _snapshot(data) == before
    assert origin.hits == 0


def test_missing_session_state_field_is_state_corrupt(office_world):
    http, data, origin, _docker_manager, broker_module = office_world
    body = intact_docx()
    _put(data, "brief.docx", body)
    file_id = _index_file(broker_module, data, "brief.docx")
    created = _create(http, file_id)
    payload = json.loads(_state(data).read_text(encoding="utf-8"))
    payload["sessions"][created.json()["session_id"]].pop("state")
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    _state(data).write_bytes(encoded)
    before = _snapshot(data)
    _assert_refusal(_create(http, file_id), 500, "state_corrupt")
    assert _snapshot(data) == before
    assert origin.hits == 0


def test_mismatched_session_map_key_is_state_corrupt(office_world):
    http, data, origin, _docker_manager, broker_module = office_world
    body = intact_docx()
    _put(data, "brief.docx", body)
    file_id = _index_file(broker_module, data, "brief.docx")
    created = _create(http, file_id)
    payload = json.loads(_state(data).read_text(encoding="utf-8"))
    payload["sessions"][created.json()["session_id"]]["session_id"] = "other-session"
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    _state(data).write_bytes(encoded)
    before = _snapshot(data)
    _assert_refusal(_create(http, file_id), 500, "state_corrupt")
    assert _snapshot(data) == before
    assert origin.hits == 0


@pytest.mark.parametrize("field", ("file_id", "document_key"))
@pytest.mark.parametrize("bad_value", ("", None, [], 1, "missing"))
def test_malformed_retained_identity_fields_are_state_corrupt(office_world, field, bad_value):
    http, data, origin, _docker_manager, broker_module = office_world
    import office.store as store_mod

    body = intact_docx()
    _put(data, "brief.docx", body)
    file_id = _index_file(broker_module, data, "brief.docx")
    _seed_session(store_mod, file_id, "closed", key="used-key")
    payload = json.loads(_state(data).read_text(encoding="utf-8"))
    record = payload["sessions"]["sess-closed"]
    if bad_value == "missing":
        record.pop(field)
    else:
        record[field] = bad_value
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    _state(data).write_bytes(encoded)
    before = _snapshot(data)
    _assert_refusal(_create(http, file_id), 500, "state_corrupt")
    assert _snapshot(data) == before
    assert origin.hits == 0



@pytest.mark.parametrize("state", FINAL_STATES)
def test_recognized_final_sessions_allow_a_new_opening_session(office_world, state):
    http, data, origin, _docker_manager, broker_module = office_world
    import office.store as store_mod

    body = intact_docx()
    _put(data, "brief.docx", body)
    file_id = _index_file(broker_module, data, "brief.docx")
    _seed_session(store_mod, file_id, state, key=f"used-{state}")
    response = _create(http, file_id)
    assert response.status_code == 201
    persisted = json.loads(_state(data).read_text(encoding="utf-8"))
    keys = {record["document_key"] for record in persisted["sessions"].values()}
    assert f"used-{state}" in keys
    assert response.json()["document_key"] != f"used-{state}"
    assert origin.hits == 0


@pytest.mark.parametrize("name,factory", (("docx", intact_docx), ("xlsx", intact_xlsx), ("pptx", intact_pptx)))
def test_extensionless_supported_type_names_are_unsupported(office_world, name, factory):
    http, data, origin, _docker_manager, broker_module = office_world
    body = factory()
    _put(data, name, body)
    file_id = _index_file(broker_module, data, name)
    _seed_refusal_history()
    before = _snapshot(data)
    _assert_refusal(_create(http, file_id), 415, "unsupported_type")
    assert _snapshot(data) == before
    downloaded = http.get(f"/files/{CHAT}/{name}", headers=_auth())
    assert downloaded.status_code == 200
    assert downloaded.content == body
    assert origin.hits == 0


def test_closed_session_gets_a_new_unused_key(office_world):
    http, data, origin, _docker_manager, broker_module = office_world
    import office.store as store_mod

    body = intact_docx()
    _put(data, "brief.docx", body)
    file_id = _index_file(broker_module, data, "brief.docx")
    _seed_session(store_mod, file_id, "closed", key="used-key")
    response = _create(http, file_id)
    assert response.status_code == 201
    assert response.json()["document_key"] != "used-key"
    persisted = json.loads(_state(data).read_text(encoding="utf-8"))
    keys = {record["document_key"] for record in persisted["sessions"].values()}
    assert keys == {"used-key", response.json()["document_key"]}
    assert origin.hits == 0


def test_invalid_json_and_unsupported_schema_are_state_corrupt(office_world):
    http, data, origin, _docker_manager, broker_module = office_world
    body = intact_docx()
    _put(data, "brief.docx", body)
    file_id = _index_file(broker_module, data, "brief.docx")
    created = _create(http, file_id)
    assert created.status_code == 201
    state_path = _state(data)
    workspace = (_outputs(data) / "brief.docx").read_bytes()
    blobs = {path.name: path.read_bytes() for path in _versions(data).iterdir()}
    for encoded in (
        b"{broken-json",
        json.dumps(
            {"schema_version": 99, "documents": {}, "sessions": {}, "receipts": {}, "journal": {}},
            separators=(",", ":"),
        ).encode("utf-8"),
    ):
        state_path.write_bytes(encoded)
        before_state = state_path.read_bytes()
        _assert_refusal(_create(http, file_id), 500, "state_corrupt")
        assert state_path.read_bytes() == before_state
        assert (_outputs(data) / "brief.docx").read_bytes() == workspace
        assert {path.name: path.read_bytes() for path in _versions(data).iterdir()} == blobs
        assert origin.hits == 0


def test_corrupt_index_is_state_corrupt_without_mutation(office_world):
    http, data, origin, _docker_manager, broker_module = office_world
    body = intact_docx()
    _put(data, "brief.docx", body)
    file_id = _index_file(broker_module, data, "brief.docx")
    index = data / CHAT / ".ocu" / "index.json"
    broken = b"{broken-json"
    index.write_bytes(broken)
    before = _snapshot(data)
    _assert_refusal(_create(http, file_id), 500, "state_corrupt")
    assert _snapshot(data) == before
    assert index.read_bytes() == broken
    assert origin.hits == 0


@pytest.mark.parametrize("kind", ("utf8", "directory"))
def test_unreadable_restore_epoch_marker_is_state_corrupt(office_world, kind):
    http, data, origin, _docker_manager, broker_module = office_world
    body = intact_docx()
    _put(data, "brief.docx", body)
    file_id = _index_file(broker_module, data, "brief.docx")
    marker = data / ".office-restore-epoch"
    if kind == "utf8":
        marker.write_bytes(b"\xff\xfe")
    else:
        marker.mkdir()
    before = _snapshot(data)
    _assert_refusal(_create(http, file_id), 500, "state_corrupt")
    assert _snapshot(data) == before
    if kind == "utf8":
        assert marker.read_bytes() == b"\xff\xfe"
    else:
        assert marker.is_dir()
    assert origin.hits == 0



def test_deflated_document_still_creates_a_session(office_world):
    http, data, origin, _docker_manager, broker_module = office_world
    body = _package(
        {
            "[Content_Types].xml": _content_types("word/document.xml", WORD_TYPE),
            "_rels/.rels": _rels("word/document.xml"),
            "word/document.xml": _main("document", WORD_NS),
        },
        compress_type=zipfile.ZIP_DEFLATED,
    )
    _put(data, "brief.docx", body)
    file_id = _index_file(broker_module, data, "brief.docx")
    response = _create(http, file_id)
    assert response.status_code == 201
    digest = _sha(body)
    persisted = json.loads(_state(data).read_text(encoding="utf-8"))
    assert persisted["documents"][file_id]["published_sha256"] == digest
    assert (_versions(data) / digest).read_bytes() == body
    assert origin.hits == 0


def test_many_member_presentation_creates_matching_persisted_content(office_world):
    http, data, origin, _docker_manager, broker_module = office_world
    body = many_member_pptx()
    _put(data, "deck.pptx", body)
    file_id = _index_file(broker_module, data, "deck.pptx")
    response = _create(http, file_id)
    assert response.status_code == 201
    digest = _sha(body)
    persisted = json.loads(_state(data).read_text(encoding="utf-8"))
    assert persisted["documents"][file_id]["published_sha256"] == digest
    assert (_versions(data) / digest).read_bytes() == body
    assert origin.hits == 0


def test_utf8_member_name_at_limit_creates_matching_persisted_content(office_world):
    http, data, origin, _docker_manager, broker_module = office_world
    accepted = _member_utf8_name()
    assert len(accepted.encode("utf-8")) == 1024
    body = _package(
        {
            "[Content_Types].xml": _content_types("word/document.xml", WORD_TYPE),
            "_rels/.rels": _rels("word/document.xml"),
            "word/document.xml": _main("document", WORD_NS),
            accepted: b"<a/>",
        }
    )
    _put(data, "brief.docx", body)
    file_id = _index_file(broker_module, data, "brief.docx")
    response = _create(http, file_id)
    assert response.status_code == 201
    digest = _sha(body)
    persisted = json.loads(_state(data).read_text(encoding="utf-8"))
    assert persisted["documents"][file_id]["published_sha256"] == digest
    assert (_versions(data) / digest).read_bytes() == body
    assert origin.hits == 0


@pytest.mark.parametrize("suffix", ("short", "x" * 1024))
def test_nul_original_member_names_are_corrupt_document(office_world, suffix):
    http, data, origin, _docker_manager, broker_module = office_world
    body = _nul_unused_member_package(suffix)
    _put(data, "brief.docx", body)
    file_id = _index_file(broker_module, data, "brief.docx")
    _seed_refusal_history()
    before = _snapshot(data)
    _assert_refusal(_create(http, file_id), 422, "corrupt_document")
    assert _snapshot(data) == before
    assert origin.hits == 0




@pytest.mark.parametrize("codec", (zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA))
@pytest.mark.parametrize("member", ("[Content_Types].xml", "_rels/.rels", "word/document.xml"))
@pytest.mark.parametrize("forge", (False, True))
def test_unsupported_zip_codecs_are_corrupt_before_decoder_entry(
    office_world, monkeypatch, codec, member, forge
):
    http, data, origin, _docker_manager, broker_module = office_world
    body = _unsupported_codec_package(codec, member, forge)
    _put(data, "brief.docx", body)
    file_id = _index_file(broker_module, data, "brief.docx")
    entered = []
    original = zipfile._get_decompressor

    def observed(method):
        if method == codec:
            entered.append(method)
        return original(method)

    monkeypatch.setattr(zipfile, "_get_decompressor", observed)
    _seed_refusal_history()
    before = _snapshot(data)
    _assert_refusal(_create(http, file_id), 422, "corrupt_document")
    assert entered == []
    assert _snapshot(data) == before
    assert origin.hits == 0




def test_signing_failure_leaves_no_capture(office_world, monkeypatch):
    http, data, origin, _docker_manager, broker_module = office_world
    import office.tokens as tokens_mod

    _put(data, "brief.docx", intact_docx())
    file_id = _index_file(broker_module, data, "brief.docx")
    before = _snapshot(data)

    def boom(*_args, **_kwargs):
        raise RuntimeError("signing failed")

    monkeypatch.setattr(tokens_mod, "sign_jwt", boom)
    response = _create(http, file_id)
    _assert_refusal(response, 500, "creation_failed")
    _assert_no_secrets(response, INTERNAL, MCP_KEY, MODEL_KEY, JWT_SECRET)
    assert _snapshot(data) == before
    assert not _state(data).exists()
    assert origin.hits == 0


def test_precommit_enospc_during_creation_leaves_no_session(office_world, monkeypatch):
    http, data, origin, _docker_manager, broker_module = office_world
    import office.store as store_mod

    _put(data, "brief.docx", intact_docx())
    file_id = _index_file(broker_module, data, "brief.docx")
    before = _snapshot(data)
    original_write = store_mod.os.write

    def fail_state_write(fd, payload):
        if payload[:1] == b"{":
            raise OSError(errno.ENOSPC, "injected state write fault")
        return original_write(fd, payload)

    monkeypatch.setattr(store_mod.os, "write", fail_state_write)
    _assert_refusal(_create(http, file_id), 503, "storage_low")
    assert _snapshot(data) == before
    assert not _state(data).exists()
    assert origin.hits == 0


def test_postreplace_durability_failure_is_state_durability_with_complete_successor(
    office_world, monkeypatch
):
    http, data, origin, _docker_manager, broker_module = office_world
    import office.store as store_mod

    body = intact_docx()
    _put(data, "brief.docx", body)
    file_id = _index_file(broker_module, data, "brief.docx")
    original_replace = store_mod.os.replace
    original_fsync = store_mod.os.fsync
    replaced = {"done": False}

    def watch_state_replace(src, dst, *args, **kwargs):
        result = original_replace(src, dst, *args, **kwargs)
        if dst == "state.json" or (isinstance(dst, str) and dst.endswith("state.json")):
            replaced["done"] = True
        return result

    def fail_after_state_replace(fd):
        if replaced["done"]:
            replaced["done"] = False
            raise OSError("controlled directory fsync failure")
        return original_fsync(fd)

    monkeypatch.setattr(store_mod.os, "replace", watch_state_replace)
    monkeypatch.setattr(store_mod.os, "fsync", fail_after_state_replace)
    _assert_refusal(_create(http, file_id), 500, "state_durability")
    persisted = json.loads(_state(data).read_text(encoding="utf-8"))
    document = persisted["documents"][file_id]
    assert document["published_sha256"] == _sha(body)
    assert document["versions"][0]["source"] == "workspace"
    assert len(persisted["sessions"]) == 1
    session = next(iter(persisted["sessions"].values()))
    assert session["state"] == "opening"
    assert session["file_id"] == file_id
    assert (_versions(data) / _sha(body)).read_bytes() == body
    assert origin.hits == 0


def test_mutator_collision_leaves_no_capture(office_world, monkeypatch):
    http, data, origin, _docker_manager, broker_module = office_world
    import office.sessions as sessions_mod
    import office.store as store_mod
    import uuid

    body = intact_docx()
    _put(data, "brief.docx", body)
    file_id = _index_file(broker_module, data, "brief.docx")
    colliding = uuid.UUID("11111111-1111-4111-8111-111111111111")
    store_mod.OfficeStore().update(
        CHAT,
        lambda state: state["sessions"].__setitem__(
            str(colliding),
            {
                "session_id": str(colliding),
                "file_id": "other",
                "document_key": "other-key",
                "baseline_sha256": "b" * 64,
                "restore_epoch": None,
                "state": "closed",
                "save_seq": 0,
            },
        ),
    )
    monkeypatch.setattr(sessions_mod.uuid, "uuid4", lambda: colliding)
    before = _snapshot(data)
    _assert_refusal(_create(http, file_id), 500, "creation_failed")
    assert _snapshot(data) == before
    assert origin.hits == 0


def test_two_processes_create_at_most_one_open_session(office_world):
    http, data, origin, _docker_manager, broker_module = office_world
    from tests.orchestrator._office_store import _wait_marker
    ready = data.parent / "join-worker-ready"
    body = intact_docx()
    _put(data, "brief.docx", body)
    file_id = _index_file(broker_module, data, "brief.docx")
    env = os.environ.copy()
    pythonpath = env.get("PYTHONPATH", "")
    env.update(
        {
            "PYTHONPATH": str(SERVER_DIR) + (os.pathsep + pythonpath if pythonpath else ""),
            "BASE_DATA_DIR": str(data),
            "OCU_CHAT": CHAT,
            "OCU_FILE_ID": file_id,
            "READY": str(ready),
            "OCU_INTERNAL_TOKEN": INTERNAL,
            "OCU_OFFICE_JWT_SECRET": JWT_SECRET,
            "OCU_OFFICE_SELF_URL": SELF_URL,
            "OCU_OFFICE_DOCSERVER_URL": origin.url,
            "OCU_OFFICE_DOCSERVER_ORIGIN": OFFICE_SETTINGS["OCU_OFFICE_DOCSERVER_ORIGIN"],
            "DOCKER_HOST": "unix:///tmp/ocu-acceptance-no-docker.sock",
            "DOCKER_SOCKET": "unix:///tmp/ocu-acceptance-no-docker.sock",
        }
    )
    child_src = r"""
import json, os, sys
from pathlib import Path
from fastapi.testclient import TestClient
import app
with TestClient(app.app) as client:
    Path(os.environ["READY"]).write_text("ready")
    sys.stdin.readline()
    response = client.post(
        f'/api/office/{os.environ["OCU_CHAT"]}/documents/{os.environ["OCU_FILE_ID"]}/sessions',
        headers={"Authorization": "Bearer " + os.environ["OCU_INTERNAL_TOKEN"]},
    )
print(json.dumps({"status": response.status_code, "body": response.json()}))
"""
    child = subprocess.Popen(
        [sys.executable, "-c", child_src],
        cwd=str(SERVER_DIR),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _wait_marker(ready, child, "second worker did not load the actual app")
    child.stdin.write("go\n")
    child.stdin.flush()
    parent = _create(http, file_id)
    stdout, stderr = child.communicate(timeout=30)
    assert child.returncode == 0, stderr
    child_response = json.loads(stdout.strip().splitlines()[-1])
    assert sorted([parent.status_code, child_response["status"]]) == [200, 201]
    child_payload = child_response["body"]
    assert parent.json()["session_id"] == child_payload["session_id"]
    assert parent.json()["document_key"] == child_payload["document_key"]
    assert sorted([parent.json()["joined"], child_payload["joined"]]) == [False, True]
    assert parent.status_code == (200 if parent.json()["joined"] else 201)
    assert parent.json()["editor_config"]["document"]["url"] != child_payload["editor_config"]["document"]["url"]
    persisted = json.loads(_state(data).read_text(encoding="utf-8"))
    opening = [
        record
        for record in persisted["sessions"].values()
        if record["file_id"] == file_id and record["state"] == "opening"
    ]
    assert len(opening) == 1
    assert origin.hits == 0


def test_creation_holds_chat_lock_through_signing_and_preserves_other_document(office_world):
    _http, data, origin, _docker_manager, broker_module = office_world
    from tests.orchestrator._office_store import _stop_child, _wait_marker

    first = _put(data, "first.docx", intact_docx())
    _put(data, "second.xlsx", intact_xlsx())
    first_id = _index_file(broker_module, data, "first.docx")
    second_id = _index_file(broker_module, data, "second.xlsx")
    entered = data.parent / "creation-signing-entered"
    contended = data.parent / "creation-lock-contended"
    release = data.parent / "creation-release"
    os.mkfifo(release)
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(SERVER_DIR),
        "BASE_DATA_DIR": str(data),
        "CHAT": CHAT,
        "ENTERED": str(entered),
        "CONTENDED": str(contended),
        "RELEASE": str(release),
    })
    source = r'''
import fcntl, json, os, time
from pathlib import Path
from office.sessions import _create_session
if os.environ["ROLE"] == "holder":
    clock = time.time
    def hold_signing():
        time.time = clock
        Path(os.environ["ENTERED"]).write_text("signing")
        fd = os.open(os.environ["RELEASE"], os.O_RDONLY)
        os.close(fd)
        return clock()
    time.time = hold_signing
else:
    original = fcntl.flock
    def observe_contention(fd, operation):
        if operation == fcntl.LOCK_EX:
            try:
                original(fd, operation | fcntl.LOCK_NB)
            except BlockingIOError:
                Path(os.environ["CONTENDED"]).write_text("blocked")
                fcntl.flock = original
                return original(fd, operation)
            original(fd, fcntl.LOCK_UN)
            raise AssertionError("creation released the chat lock before signing")
        return original(fd, operation)
    fcntl.flock = observe_contention
result = _create_session(os.environ["CHAT"], os.environ["FILE"])
print(json.dumps({"session_id": result["session_id"], "file_id": result["file_id"]}))
'''
    holder = waiter = None
    try:
        holder = subprocess.Popen(
            [sys.executable, "-c", source], cwd=SERVER_DIR,
            env={**env, "ROLE": "holder", "FILE": first_id},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        _wait_marker(entered, holder, "holder did not reach pre-publication signing")
        waiter = subprocess.Popen(
            [sys.executable, "-c", source], cwd=SERVER_DIR,
            env={**env, "ROLE": "waiter", "FILE": second_id},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        _wait_marker(contended, waiter, "other creation did not block through signing")
        assert not _state(data).exists()
        assert not _versions(data).exists()
        assert first.read_bytes() == intact_docx()
        fd = os.open(release, os.O_WRONLY)
        os.close(fd)
        results = []
        for child in (holder, waiter):
            stdout, stderr = child.communicate(timeout=10)
            assert child.returncode == 0, stderr
            results.append(json.loads(stdout.strip().splitlines()[-1]))
        state = json.loads(_state(data).read_bytes())
        assert set(state["documents"]) == {first_id, second_id}
        assert {record["file_id"] for record in state["sessions"].values()} == {first_id, second_id}
        for result in results:
            assert state["sessions"][result["session_id"]]["file_id"] == result["file_id"]
            assert state["documents"][result["file_id"]]["versions"][0]["number"] == 1
        assert origin.hits == 0
    finally:
        _stop_child(waiter)
        _stop_child(holder)


def _seed_refusal_history():
    import office.store as store_mod

    def stamp(working, selected):
        working["sessions"]["history-session"] = {
            "session_id": "history-session", "file_id": "history-document",
            "document_key": "history-key", "baseline_sha256": selected["sha256"],
            "restore_epoch": None, "state": "closed", "save_seq": 0,
        }

    store_mod.OfficeStore().store_version(
        CHAT, "history-document", b"HISTORICAL-WORKSPACE-BYTES",
        source="workspace", parent=None, published=True, min_free_bytes=0, mutate_state=stamp,
    )


def test_twenty_one_documents_create_without_connection_cap_or_network(office_world):
    http, data, origin, _docker_manager, broker_module = office_world
    for index in range(21):
        name = f"document-{index}.docx"
        _put(data, name, intact_docx())
        file_id = _index_file(broker_module, data, name)
        previous = json.loads(_state(data).read_bytes()) if _state(data).exists() else None
        response = _create(http, file_id)
        assert response.status_code == 201
        current = json.loads(_state(data).read_bytes())
        if previous is not None:
            for session_id, session in previous["sessions"].items():
                assert current["sessions"][session_id] == session
            for document_id, document in previous["documents"].items():
                assert current["documents"][document_id] == document
    assert len(current["sessions"]) == 21
    assert len({session["document_key"] for session in current["sessions"].values()}) == 21
    before_join = _snapshot(data)
    joined = _create(http, file_id)
    assert joined.status_code == 200
    assert joined.json()["session_id"] == response.json()["session_id"]
    assert joined.json()["document_key"] == response.json()["document_key"]
    assert joined.json()["joined"] is True
    _assert_activity_only(before_join, _snapshot(data), joined.json()["session_id"])
    assert origin.hits == 0
