# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Public HTTP seam for DocumentServer source tickets and callback admission."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import shutil
from urllib.parse import quote, urlsplit

import pytest

from tests.orchestrator.test_office_ooxml import intact_docx
from tests.orchestrator.test_office_router import SANDBOX_PEER, _office_app, _raw_office
from tests.orchestrator.test_office_sessions import (
    INTERNAL,
    JWT_SECRET,
    _assert_refusal,
    _close_session,
    _create,
    _index_file,
    _put,
    _sha,
    _snapshot,
    _ticket_key,
    _versions,
    office_world,
)
from tests.orchestrator.test_office_workspace import _forbid_inode_open
from tests.orchestrator.test_outputs_endpoint import CHAT, _auth


def _source_path(session):
    return urlsplit(session["editor_config"]["document"]["url"]).path


def _ticket(session):
    return _source_path(session).rsplit("/", 1)[-1]


def _callback_path(session, chat=CHAT):
    return f"/office/callback/{chat}/{session['session_id']}"


def _header_jwt(session, *, status=1, key=None, secret=JWT_SECRET, extra=None):
    from office.tokens import sign_jwt

    payload = {"payload": {"key": key or session["document_key"], "status": status}}
    if extra:
        payload["payload"].update(extra)
    if secret == JWT_SECRET:
        return sign_jwt(payload)
    return _foreign_jwt(payload, secret)


def _foreign_jwt(payload, secret):
    import base64

    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")

    body = encode({"alg": "HS256", "typ": "JWT"}) + "." + encode(payload)
    signature = base64.urlsafe_b64encode(hmac.digest(secret.encode(), body.encode(), "sha256")).decode().rstrip("=")
    return body + "." + signature


def _body_jwt(session, *, status=1, key=None):
    from office.tokens import sign_jwt

    return sign_jwt({"key": key or session["document_key"], "status": status})


def _open_session(office_world, name="report.docx"):
    http, data, origin, docker, broker = office_world
    content = intact_docx()
    _put(data, name, content)
    file_id = _index_file(broker, data, name)
    created = _create(http, file_id)
    assert created.status_code == 201
    return http, data, origin, docker, broker, content, created.json()


def _trap_verify_blob(monkeypatch):
    import office.versions as versions_mod

    def forbidden(*args, **kwargs):
        pytest.fail("version content was read")

    monkeypatch.setattr(versions_mod, "_verify_blob", forbidden)
    monkeypatch.setattr(versions_mod, "read_version_bytes", forbidden)


def _trap_tokens(monkeypatch):
    import office.tokens as tokens_mod

    def forbidden(*args, **kwargs):
        pytest.fail("office credential evaluated while disabled")

    monkeypatch.setattr(tokens_mod, "verify_source_ticket", forbidden)
    monkeypatch.setattr(tokens_mod, "verify_jwt", forbidden)


def test_valid_source_ticket_returns_bound_bytes_after_workspace_mutation(office_world):
    http, data, origin, _docker, broker, content, session = _open_session(office_world)
    from office.store import OfficeStore

    path = _source_path(session)
    _put(data, "report.docx", b"workspace-edited")
    before = OfficeStore().read(CHAT)

    source = http.get(path)
    assert source.status_code == 200
    assert source.content == content
    assert _sha(source.content) == _sha(content)

    signed = _header_jwt(session)
    callback = http.post(
        _callback_path(session),
        headers={"Authorization": "Bearer " + signed},
        json={"key": "unsigned-foreign", "status": 2},
    )
    assert callback.status_code == 200
    assert callback.json() == {"error": 0}
    after = OfficeStore().read(CHAT)
    assert after["sessions"][session["session_id"]]["state"] == "editing"
    assert after["sessions"][session["session_id"]]["document_key"] == session["document_key"]
    assert after["documents"][session["file_id"]] == before["documents"][session["file_id"]]
    assert after["receipts"] == {}
    assert origin.hits == 0


def test_inactive_session_still_returns_bound_version(office_world):
    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    import office.store as store_mod

    _close_session(store_mod, session["session_id"])
    before = _snapshot(data)
    source = http.get(_source_path(session))
    assert source.status_code == 200
    assert source.content == content
    assert _snapshot(data) == before
    assert origin.hits == 0


def test_missing_session_binding_is_invalid_ticket_without_reading_content(office_world, monkeypatch):
    http, data, origin, _docker, _broker, _content, session = _open_session(office_world)
    import office.store as store_mod

    def drop(state):
        state["sessions"].pop(session["session_id"])

    store_mod.OfficeStore().update(CHAT, drop)
    before = _snapshot(data)
    _trap_verify_blob(monkeypatch)
    response = http.get(_source_path(session))
    _assert_refusal(response, 401, "invalid_ticket")
    assert _snapshot(data) == before
    assert origin.hits == 0



@pytest.mark.parametrize("kind", ("expired", "malformed", "signature", "chat", "file", "version", "session"))
def test_invalid_tickets_are_invalid_ticket_without_reading_content(office_world, monkeypatch, kind):
    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    ticket = _ticket(session)
    if kind == "expired":
        monkeypatch.setattr("time.time", lambda: 10**12)
        path = _source_path(session)
    elif kind == "malformed":
        path = "/office/source/not-a-jwt"
    elif kind == "signature":
        path = "/office/source/" + ticket[:-2] + ("AA" if ticket[-2:] != "AA" else "BB")
    else:
        header, payload, signature = ticket.split(".")
        claims = json.loads(__import__("base64").urlsafe_b64decode(payload + "=" * ((4 - len(payload) % 4) % 4)))
        replacements = {
            "chat": "other-chat",
            "file": "00000000-0000-4000-8000-000000000000",
            "version": claims["version"] + 1,
            "session": "00000000-0000-4000-8000-000000000099",
        }
        field = {"chat": "chat_id", "file": "file_id", "version": "version", "session": "session_id"}[kind]
        claims[field] = replacements[kind]
        encoded = __import__("base64").urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).decode().rstrip("=")
        path = "/office/source/" + _oracle_sign_raw(header, encoded)
    before = _snapshot(data)
    _trap_verify_blob(monkeypatch)
    response = http.get(path)
    _assert_refusal(response, 401, "invalid_ticket")
    assert _snapshot(data) == before
    assert origin.hits == 0


@pytest.mark.parametrize("ticket", ("not-a-jwt", "invalid%0Aticket", "invalid%0Dticket", "invalid\nticket"))
def test_malformed_source_tickets_including_newlines_are_invalid_ticket(office_world, monkeypatch, ticket):
    http, data, origin, _docker, _broker, _content, session = _open_session(office_world)
    before = _snapshot(data)
    _trap_verify_blob(monkeypatch)
    if "\n" in ticket:
        status, body = _raw_office(http.app, "/office/source/" + ticket, [])
        assert status == 401
        assert body == {"reason": "invalid_ticket"}
    else:
        response = http.get("/office/source/" + ticket)
        _assert_refusal(response, 401, "invalid_ticket")
    assert _snapshot(data) == before
    assert origin.hits == 0


def _oracle_sign_raw(header, body):
    signing = f"{header}.{body}".encode("ascii")
    digest = hmac.new(_ticket_key(INTERNAL), signing, hashlib.sha256).digest()
    return f"{header}.{body}.{__import__('base64').urlsafe_b64encode(digest).decode().rstrip('=')}"


def test_source_missing_chat_is_invalid_ticket_without_create(office_world, monkeypatch):
    http, data, origin, docker, _broker, _content, session = _open_session(office_world)
    path = _source_path(session)
    shutil.rmtree(data / CHAT)
    before = _snapshot(data)
    original_lock = docker._combined_lock

    def forbidden(*args, **kwargs):
        if kwargs.get("create") is False:
            return original_lock(*args, **kwargs)
        pytest.fail("absent source chat took a creating lock")

    monkeypatch.setattr(docker, "_combined_lock", forbidden)
    _trap_verify_blob(monkeypatch)
    response = http.get(path)
    _assert_refusal(response, 401, "invalid_ticket")
    assert not (data / CHAT).exists()
    assert _snapshot(data) == before
    assert origin.hits == 0


@pytest.mark.parametrize("damage", ("missing", "corrupt", "symlink", "nonregular"))
def test_damaged_source_blob_fails_without_workspace_or_external_read(office_world, monkeypatch, damage):
    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    import office.control_plane as control_plane
    import office.versions as versions_mod

    blob = _versions(data) / _sha(content)
    outside = data.parent / "external-source.bin"
    outside.write_bytes(b"EXTERNAL-SOURCE")
    workspace = data / CHAT / "outputs" / "report.docx"
    if damage == "missing":
        blob.unlink()
    elif damage == "corrupt":
        blob.write_bytes(b"not-the-hash")
    elif damage == "symlink":
        blob.unlink()
        os.symlink(outside, blob)
        _forbid_inode_open(versions_mod, monkeypatch, outside, workspace)
        _forbid_inode_open(control_plane, monkeypatch, outside, workspace)
    else:
        blob.unlink()
        os.mkfifo(blob)
        _forbid_inode_open(versions_mod, monkeypatch, outside, workspace)
    before = _snapshot(data)
    response = http.get(_source_path(session))
    _assert_refusal(response, 500, "state_corrupt")
    assert _snapshot(data) == before
    assert outside.read_bytes() == b"EXTERNAL-SOURCE"
    assert origin.hits == 0


def test_internal_token_does_not_authenticate_source_or_callback(office_world, monkeypatch):
    http, data, origin, _docker, _broker, content, session = _open_session(office_world)
    before = _snapshot(data)
    _trap_verify_blob(monkeypatch)
    source = http.get("/office/source/not-a-ticket", headers=_auth())
    _assert_refusal(source, 401, "invalid_ticket")
    callback = http.post(_callback_path(session), headers=_auth(), json={})
    _assert_refusal(callback, 401, "invalid_token")
    assert _snapshot(data) == before
    assert origin.hits == 0


def test_callback_body_token_admits_and_unsigned_fields_are_ignored(office_world, monkeypatch):
    http, data, origin, _docker, _broker, _content, session = _open_session(office_world)
    from office.store import OfficeStore

    before = OfficeStore().read(CHAT)
    token = _body_jwt(session, status=1)

    def forbidden(*args, **kwargs):
        pytest.fail("unsigned download address was fetched")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    response = http.post(
        _callback_path(session),
        json={
            "token": token,
            "key": "unsigned-foreign",
            "status": 2,
            "url": "http://127.0.0.1:1/never-fetch",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"error": 0}
    after = OfficeStore().read(CHAT)
    assert after["sessions"][session["session_id"]]["state"] == "editing"
    assert after["documents"][session["file_id"]] == before["documents"][session["file_id"]]
    assert after["receipts"] == {}
    assert origin.hits == 0


def test_present_invalid_header_does_not_fall_back_to_body_token(office_world, monkeypatch):
    http, data, origin, _docker, _broker, _content, session = _open_session(office_world)
    before = _snapshot(data)
    _trap_verify_blob(monkeypatch)
    response = http.post(
        _callback_path(session),
        headers={"Authorization": "Bearer not-a-jwt"},
        json={"token": _body_jwt(session)},
    )
    _assert_refusal(response, 401, "invalid_token")
    assert _snapshot(data) == before
    assert origin.hits == 0


@pytest.mark.parametrize("kind", ("missing", "wrong-secret", "expired", "wrong-key"))
def test_callback_credential_failures_are_invalid_token_without_mutation(office_world, monkeypatch, kind):
    http, data, origin, _docker, _broker, _content, session = _open_session(office_world)
    before = _snapshot(data)
    if kind == "missing":
        response = http.post(_callback_path(session), json={"key": session["document_key"], "status": 1})
    elif kind == "wrong-secret":
        response = http.post(
            _callback_path(session),
            headers={"Authorization": "Bearer " + _header_jwt(session, secret="other-secret")},
            json={},
        )
    elif kind == "expired":
        from office.tokens import sign_jwt

        token = sign_jwt({"payload": {"key": session["document_key"], "status": 1}, "exp": 1})
        monkeypatch.setattr("time.time", lambda: 10)
        response = http.post(_callback_path(session), headers={"Authorization": "Bearer " + token}, json={})
    else:
        response = http.post(
            _callback_path(session),
            headers={"Authorization": "Bearer " + _header_jwt(session, key="foreign-document-key")},
            json={},
        )
    _assert_refusal(response, 401, "invalid_token")
    assert _snapshot(data) == before
    assert origin.hits == 0



@pytest.mark.parametrize(
    "body",
    (
        b"{not-json",
        b"[1]",
        json.dumps({"token": 1}).encode("utf-8"),
    ),
)
def test_malformed_callback_envelope_is_invalid_token_without_work(office_world, monkeypatch, body):
    http, data, origin, _docker, _broker, _content, session = _open_session(office_world)
    before = _snapshot(data)

    def forbidden(*args, **kwargs):
        pytest.fail("malformed callback envelope made an outbound request")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    response = http.post(_callback_path(session), content=body)
    _assert_refusal(response, 401, "invalid_token")
    assert _snapshot(data) == before
    assert origin.hits == 0


def test_wrong_key_does_not_change_either_session(office_world):
    http, data, origin, _docker, broker = office_world
    first_content = intact_docx()
    _put(data, "one.docx", first_content)
    second_content = intact_docx() + b"x"
    _put(data, "two.docx", second_content)
    first_id = _index_file(broker, data, "one.docx")
    second_id = _index_file(broker, data, "two.docx")
    first = _create(http, first_id).json()
    second = _create(http, second_id).json()
    before = _snapshot(data)
    response = http.post(
        _callback_path(first),
        headers={"Authorization": "Bearer " + _header_jwt(second)},
        json={},
    )
    _assert_refusal(response, 401, "invalid_token")
    assert _snapshot(data) == before
    assert origin.hits == 0


def test_removed_chat_callback_is_unknown_session_without_create(office_world, monkeypatch):
    http, data, origin, docker, _broker, _content, session = _open_session(office_world)
    token = _header_jwt(session)
    shutil.rmtree(data / CHAT)
    original_lock = docker._combined_lock

    def forbidden(*args, **kwargs):
        if kwargs.get("create") is False:
            return original_lock(*args, **kwargs)
        pytest.fail("absent callback chat took a creating lock")

    monkeypatch.setattr(docker, "_combined_lock", forbidden)
    response = http.post(_callback_path(session), headers={"Authorization": "Bearer " + token}, json={})
    _assert_refusal(response, 404, "unknown_session")
    assert not (data / CHAT).exists()
    assert origin.hits == 0


def _disappearing_lock_open(monkeypatch, data):
    from pathlib import Path

    original_open = Path.open

    def disappearing_open(path, *args, **kwargs):
        if path == data / CHAT / ".lifecycle.lock":
            shutil.rmtree(data / CHAT)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", disappearing_open)


def test_source_chat_removed_between_lstat_and_lock_open_is_invalid_ticket(office_world, monkeypatch):
    http, data, origin, _docker, _broker, _content, session = _open_session(office_world)
    _disappearing_lock_open(monkeypatch, data)
    response = http.get(_source_path(session))
    _assert_refusal(response, 401, "invalid_ticket")
    assert not (data / CHAT).exists()
    assert origin.hits == 0


def test_callback_chat_removed_between_lstat_and_lock_open_is_unknown_session(office_world, monkeypatch):
    http, data, origin, _docker, _broker, _content, session = _open_session(office_world)
    _disappearing_lock_open(monkeypatch, data)
    response = http.post(
        _callback_path(session),
        headers={"Authorization": "Bearer " + _header_jwt(session)},
        json={},
    )
    _assert_refusal(response, 404, "unknown_session")
    assert not (data / CHAT).exists()
    assert origin.hits == 0


def test_callback_rejection_log_names_chat_session_and_reason(office_world, caplog):
    http, data, origin, _docker, _broker, _content, session = _open_session(office_world)
    token = _header_jwt(session, key="foreign-document-key")
    with caplog.at_level(logging.ERROR, logger="ocu.office"):
        response = http.post(_callback_path(session), headers={"Authorization": "Bearer " + token}, json={})
    _assert_refusal(response, 401, "invalid_token")
    records = [record.getMessage() for record in caplog.records if record.name == "ocu.office"]
    assert records
    message = records[-1]
    assert CHAT in message
    assert session["session_id"] in message
    assert "invalid_token" in message
    assert token not in message
    assert JWT_SECRET not in message
    assert origin.hits == 0


@pytest.mark.parametrize("chat", ("default", "", "temporary:abc", "local:abc", "channel:abc", quote("temporary:abc", safe="")))
def test_malformed_callback_chat_is_client_error_before_handler_work(office_world, chat):
    http, data, origin, docker, _broker, _content, session = _open_session(office_world)
    before = _snapshot(data)
    token = _header_jwt(session)
    path = f"/office/callback/{chat}/{session['session_id']}" if chat != "" else f"/office/callback//{session['session_id']}"
    with _lock_trap(docker):
        response = http.post(path, headers={"Authorization": "Bearer " + token}, json={})
    _assert_refusal(response, 400, "invalid_chat_id")
    assert _snapshot(data) == before
    assert origin.hits == 0


class _lock_trap:
    def __init__(self, docker):
        self.docker = docker
        self.original_combined = docker._combined_lock

    def __enter__(self):
        def forbidden(*args, **kwargs):
            pytest.fail("malformed callback chat took a chat lock")

        self.docker._combined_lock = forbidden
        return self

    def __exit__(self, *args):
        self.docker._combined_lock = self.original_combined
        return False


def test_disabled_control_plane_is_404_without_credential_evaluation(tmp_path, monkeypatch):
    with _office_app(tmp_path, monkeypatch, enabled=False) as (http, data, origin, docker):
        _trap_tokens(monkeypatch)
        before = _snapshot(data)
        for method, path in (
            ("GET", "/office/source/any-ticket"),
            ("OPTIONS", "/office/source/any-ticket"),
            ("POST", f"/office/callback/{CHAT}/session"),
            ("OPTIONS", f"/office/callback/{CHAT}/session"),
        ):
            headers = {
                "Origin": "https://webui.example",
                "Access-Control-Request-Method": "GET",
            }
            if method == "POST":
                response = http.request(method, path, headers=_auth(), json={})
            else:
                response = http.request(method, path, headers={**headers, **_auth()} if method != "OPTIONS" else headers)
            assert response.status_code == 404
            assert json.loads(response.content)["reason"] == "office_disabled"
        assert _snapshot(data) == before
        assert not (data / CHAT).exists()
        assert origin.hits == 0


def test_peer_denial_precedes_disabled_404(tmp_path, monkeypatch):
    with _office_app(tmp_path, monkeypatch, enabled=False) as (http, data, origin, docker):
        _trap_tokens(monkeypatch)
        for path in ("/office/source/any-ticket", f"/office/callback/{CHAT}/session"):
            status, body = _raw_office(
                http.app,
                path,
                [
                    (b"authorization", f"Bearer {INTERNAL}".encode("ascii")),
                    (b"x-forwarded-for", b"198.51.100.9"),
                ],
                method="GET" if path.startswith("/office/source/") else "POST",
                client=(SANDBOX_PEER, 40020),
                body=b"{}",
            )
            assert status == 403
            assert body["reason"] == "forbidden"
        assert not (data / CHAT).exists()
        assert origin.hits == 0


def test_sandbox_peer_is_403_on_enabled_control_plane(office_world):
    http, data, origin, _docker, _broker, _content, session = _open_session(office_world)
    before = _snapshot(data)
    token = _header_jwt(session)
    source_status, source_body = _raw_office(
        http.app,
        _source_path(session),
        [(b"x-forwarded-for", b"198.51.100.9")],
        client=(SANDBOX_PEER, 40020),
    )
    callback_status, callback_body = _raw_office(
        http.app,
        _callback_path(session),
        [
            (b"authorization", f"Bearer {token}".encode("ascii")),
            (b"x-forwarded-for", b"198.51.100.9"),
        ],
        method="POST",
        client=(SANDBOX_PEER, 40021),
        body=b"{}",
    )
    assert source_status == 403
    assert source_body["reason"] == "forbidden"
    assert callback_status == 403
    assert callback_body["reason"] == "forbidden"
    assert _snapshot(data) == before
    assert origin.hits == 0


def test_source_access_filter_redacts_ticket_without_changing_path():
    from office.control_plane import SourceTicketAccessFilter

    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:1", "GET", "/office/source/secret-ticket?ticket=query-secret", "1.1", 401), None)
    assert SourceTicketAccessFilter().filter(record) is True
    assert record.args[2] == "/office/source/*************?ticket=************"
    assert "secret-ticket" not in record.args[2]
    other = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:1", "GET", "/health", "1.1", 200), None)
    assert SourceTicketAccessFilter().filter(other) is True
    assert other.args[2] == "/health"


def test_other_chat_bound_office_routes_still_require_internal_token(office_world):
    http, data, origin, _docker, _broker, _content, session = _open_session(office_world)
    response = http.get(f"/api/office/{CHAT}/sessions/{session['session_id']}")
    assert response.status_code == 401
    assert origin.hits == 0

