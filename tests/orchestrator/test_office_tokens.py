# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Public-seam tests for DocumentServer JWT and source-ticket helpers."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[2] / "computer-use-server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from office import config, tokens

JWT_SECRET = "ds-jwt-secret-canary"
INTERNAL_TOKEN = "internal-token-canary"
ALT_SECRET = "other-ds-secret"
NOW = 1_700_000_000
# Independent HS256 of header {"alg":"HS256","typ":"JWT"} and
# payload {"document":"key-1","exp":1700000300} with JWT_SECRET.
KNOWN_VALID_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJkb2N1bWVudCI6ImtleS0xIiwiZXhwIjoxNzAwMDAwMzAwfQ"
    ".L7vXfbUDiW8hA-R_Ka5hErlV3R8pJy1A6Pu8EGt5H9o"
)

CHAT = "chat-alpha"
FILE = "file-bravo"
VERSION = 3
SESSION = "session-charlie"


@pytest.fixture(autouse=True)
def isolated_office_env(monkeypatch):
    for name in (
        "OCU_OFFICE_DOCSERVER_URL",
        "OCU_OFFICE_DOCSERVER_ORIGIN",
        "OCU_OFFICE_SELF_URL",
        "OCU_OFFICE_JWT_SECRET",
        "OCU_INTERNAL_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OCU_OFFICE_JWT_SECRET", JWT_SECRET)
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setattr("time.time", lambda: NOW)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * ((4 - len(segment) % 4) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _secret_bytes(secret: str | bytes) -> bytes:
    return secret.encode("utf-8") if isinstance(secret, str) else secret


def _oracle_sign(payload: dict, secret: str | bytes, *, alg: str = "HS256") -> str:
    header = _b64url(json.dumps({"alg": alg, "typ": "JWT"}, separators=(",", ":")).encode("utf-8"))
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return _oracle_sign_raw(header, body, secret)


def _oracle_sign_raw(header: str, body: str, secret: str | bytes) -> str:
    signing = f"{header}.{body}".encode("ascii")
    digest = hmac.new(_secret_bytes(secret), signing, hashlib.sha256).digest()
    return f"{header}.{body}.{_b64url(digest)}"


def _oracle_verify(token: str, secret: str | bytes) -> dict:
    header_b64, payload_b64, signature_b64 = token.split(".")
    signing = f"{header_b64}.{payload_b64}".encode("ascii")
    expected = hmac.new(_secret_bytes(secret), signing, hashlib.sha256).digest()
    presented = _b64url_decode(signature_b64)
    assert hmac.compare_digest(presented, expected)
    header = json.loads(_b64url_decode(header_b64))
    assert header["alg"] == "HS256"
    return json.loads(_b64url_decode(payload_b64))


def _ticket_key(internal: str) -> bytes:
    return hmac.new(internal.encode("utf-8"), b"ocu-office-source-ticket", hashlib.sha256).digest()


def _assert_generic(excinfo, *hidden: str) -> None:
    text = f"{excinfo.value!s} {excinfo.value!r} {excinfo.value.__cause__!r}"
    assert "invalid token" in str(excinfo.value)
    for value in hidden:
        assert value not in text


def _assert_clean_output(capsys, caplog, *hidden: str) -> None:
    captured = capsys.readouterr()
    text = captured.out + captured.err + caplog.text
    for value in hidden:
        assert value not in text


def test_known_external_token_verifies_and_sign_matches_independent_oracle():
    assert tokens.verify_jwt(KNOWN_VALID_TOKEN) == {"document": "key-1", "exp": NOW + 300}
    signed = tokens.sign_jwt({"document": "key-1", "exp": NOW + 300})
    assert signed == KNOWN_VALID_TOKEN
    assert _oracle_verify(signed, JWT_SECRET) == {"document": "key-1", "exp": NOW + 300}


def test_payload_round_trip_returns_the_same_object_fields():
    payload = {"key": "doc-1", "c": "forcesave", "userdata": '{"save_seq":1}'}
    assert tokens.verify_jwt(tokens.sign_jwt(payload)) == payload


def test_missing_tampered_wrong_key_and_bad_alg_fail_without_payload(capsys, caplog):
    valid = tokens.sign_jwt({"document": "secret-doc", "exp": NOW + 10})
    header, body, signature = valid.split(".")
    tampered_payload = json.loads(_b64url_decode(body))
    tampered_payload["document"] = "forged-doc"
    tampered = f"{header}.{_b64url(json.dumps(tampered_payload, separators=(',', ':')).encode('utf-8'))}.{signature}"
    wrong = _oracle_sign({"document": "secret-doc", "exp": NOW + 10}, ALT_SECRET)
    for token in (None, "", "not-a-jwt", tampered, wrong):
        with pytest.raises(tokens.InvalidTokenError) as excinfo:
            tokens.verify_jwt(token)
        _assert_generic(excinfo, JWT_SECRET, "secret-doc", "forged-doc", valid, wrong)
        assert getattr(excinfo.value, "__cause__", None) is None
    _assert_clean_output(capsys, caplog, JWT_SECRET, INTERNAL_TOKEN, valid)


def test_expired_nonfinite_boolean_and_nbf_claims_fail_closed():
    with pytest.raises(tokens.InvalidTokenError):
        tokens.verify_jwt(_oracle_sign({"exp": NOW}, JWT_SECRET))
    accepted = tokens.verify_jwt(_oracle_sign({"exp": NOW + 1}, JWT_SECRET))
    assert accepted["exp"] == NOW + 1
    for payload in (
        {"exp": True},
        {"exp": False},
        {"exp": math.inf},
        {"exp": math.nan},
        {"nbf": NOW + 1},
        {"nbf": True},
        {"nbf": math.inf},
        {"document": "x", "exp": "1700000300"},
    ):
        with pytest.raises(tokens.InvalidTokenError) as excinfo:
            tokens.verify_jwt(_oracle_sign(payload, JWT_SECRET))
        _assert_generic(excinfo, JWT_SECRET)
    assert tokens.verify_jwt(_oracle_sign({"nbf": NOW}, JWT_SECRET))["nbf"] == NOW


def test_non_object_payload_and_header_fail_before_any_claim_is_returned():
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode("utf-8"))
    array_body = _b64url(b"[1,2]")
    signing = f"{header}.{array_body}".encode("ascii")
    digest = hmac.new(JWT_SECRET.encode("utf-8"), signing, hashlib.sha256).digest()
    array_token = f"{header}.{array_body}.{_b64url(digest)}"
    with pytest.raises(tokens.InvalidTokenError):
        tokens.verify_jwt(array_token)
    string_header = _b64url(b'"HS256"')
    payload = _b64url(json.dumps({"ok": True}, separators=(",", ":")).encode("utf-8"))
    signing = f"{string_header}.{payload}".encode("ascii")
    digest = hmac.new(JWT_SECRET.encode("utf-8"), signing, hashlib.sha256).digest()
    with pytest.raises(tokens.InvalidTokenError):
        tokens.verify_jwt(f"{string_header}.{payload}.{_b64url(digest)}")


def test_missing_or_blank_signing_key_fails_explicitly_without_blank_signing(monkeypatch):
    monkeypatch.delenv("OCU_OFFICE_JWT_SECRET", raising=False)
    with pytest.raises(ValueError) as missing:
        tokens.sign_jwt({"a": 1})
    assert JWT_SECRET not in str(missing.value)
    with pytest.raises(ValueError):
        tokens.verify_jwt(_oracle_sign({"a": 1}, JWT_SECRET))
    monkeypatch.setenv("OCU_OFFICE_JWT_SECRET", " \t\n")
    with pytest.raises(ValueError):
        tokens.sign_jwt({"a": 1})


def test_only_hs256_is_accepted_even_when_the_mac_matches_the_declared_alg():
    payload = {"document": "key-1", "exp": NOW + 300}
    ticket_payload = {
        "chat_id": CHAT,
        "file_id": FILE,
        "version": VERSION,
        "session_id": SESSION,
        "exp": NOW + 300,
    }
    unsigned_none = _oracle_sign(payload, JWT_SECRET, alg="none").rsplit(".", 1)[0] + "."
    cases = (
        tokens.verify_jwt,
        JWT_SECRET,
        (
            _oracle_sign(payload, JWT_SECRET, alg="none"),
            _oracle_sign(payload, JWT_SECRET, alg="HS512"),
            unsigned_none,
        ),
    ), (
        tokens.verify_source_ticket,
        _ticket_key(INTERNAL_TOKEN),
        (
            _oracle_sign(ticket_payload, _ticket_key(INTERNAL_TOKEN), alg="none"),
            _oracle_sign(ticket_payload, _ticket_key(INTERNAL_TOKEN), alg="HS512"),
        ),
    )
    for verify, secret, tokens_under_test in cases:
        for token in tokens_under_test:
            with pytest.raises(tokens.InvalidTokenError) as excinfo:
                verify(token)
            _assert_generic(excinfo, JWT_SECRET, INTERNAL_TOKEN, token)
            assert getattr(excinfo.value, "__cause__", None) is None


def test_deeply_nested_json_segments_are_invalid_tokens_not_recursion_errors():
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode("utf-8"))
    deep_header = _b64url(("[" * 30000 + "]" * 30000).encode("ascii"))
    payload_body = _b64url(json.dumps({"document": "key-1", "exp": NOW + 300}, separators=(",", ":")).encode("utf-8"))
    malformed_header = _oracle_sign_raw(deep_header, payload_body, JWT_SECRET)
    with pytest.raises(tokens.InvalidTokenError) as jwt_header:
        tokens.verify_jwt(malformed_header)
    _assert_generic(jwt_header, JWT_SECRET, malformed_header)
    assert jwt_header.value.__cause__ is None
    with pytest.raises(tokens.InvalidTokenError) as ticket_header:
        tokens.verify_source_ticket(_oracle_sign_raw(deep_header, payload_body, _ticket_key(INTERNAL_TOKEN)))
    _assert_generic(ticket_header, INTERNAL_TOKEN)
    assert ticket_header.value.__cause__ is None
    deep_payload = _b64url(("[" * 30000 + "]" * 30000).encode("ascii"))
    signed_deep_payload = _oracle_sign_raw(header, deep_payload, JWT_SECRET)
    with pytest.raises(tokens.InvalidTokenError) as jwt_payload:
        tokens.verify_jwt(signed_deep_payload)
    _assert_generic(jwt_payload, JWT_SECRET, signed_deep_payload)
    assert jwt_payload.value.__cause__ is None


def test_signed_ticket_without_exp_is_invalid():
    payload = {
        "chat_id": CHAT,
        "file_id": FILE,
        "version": VERSION,
        "session_id": SESSION,
    }
    token = _oracle_sign(payload, _ticket_key(INTERNAL_TOKEN))
    with pytest.raises(tokens.InvalidTokenError) as excinfo:
        tokens.verify_source_ticket(token)
    _assert_generic(excinfo, INTERNAL_TOKEN, token)
    assert excinfo.value.__cause__ is None


def test_non_ascii_and_non_alphabet_segments_are_invalid_tokens_without_leaking():
    valid = tokens.sign_jwt({"document": "secret-doc", "exp": NOW + 10})
    header, body, signature = valid.split(".")
    snowman = f"{header}.☃.{signature}"
    bang = f"{header}.{body}.{signature}!"
    plus = f"{header}.{body}+.{signature}"
    for token in (snowman, bang, plus):
        with pytest.raises(tokens.InvalidTokenError) as excinfo:
            tokens.verify_jwt(token)
        _assert_generic(excinfo, JWT_SECRET, token, "☃", "secret-doc")
        assert getattr(excinfo.value, "__cause__", None) is None


def test_oversized_numeric_date_is_an_invalid_token_not_an_overflow():
    huge = tokens.sign_jwt({"exp": 10**1000})
    with pytest.raises(tokens.InvalidTokenError) as exp_info:
        tokens.verify_jwt(huge)
    _assert_generic(exp_info, JWT_SECRET, huge)
    assert getattr(exp_info.value, "__cause__", None) is None
    nbf_huge = tokens.sign_jwt({"nbf": 10**1000})
    with pytest.raises(tokens.InvalidTokenError) as nbf_info:
        tokens.verify_jwt(nbf_huge)
    _assert_generic(nbf_info, JWT_SECRET, nbf_huge)
    assert getattr(nbf_info.value, "__cause__", None) is None


def test_source_ticket_returns_exactly_four_bindings_and_honours_ttl():
    ticket = tokens.sign_source_ticket(CHAT, FILE, VERSION, SESSION)
    bindings = tokens.verify_source_ticket(ticket)
    assert bindings == {
        "chat_id": CHAT,
        "file_id": FILE,
        "version": VERSION,
        "session_id": SESSION,
    }
    oracle = _oracle_verify(ticket, _ticket_key(INTERNAL_TOKEN))
    assert oracle["exp"] == NOW + config.SOURCE_TICKET_TTL_SECONDS


def test_same_clock_tickets_are_distinct_and_keep_the_same_verified_bindings():
    first = tokens.sign_source_ticket(CHAT, FILE, VERSION, SESSION)
    second = tokens.sign_source_ticket(CHAT, FILE, VERSION, SESSION)
    expected = {"chat_id": CHAT, "file_id": FILE, "version": VERSION, "session_id": SESSION}
    assert first != second
    assert tokens.verify_source_ticket(first) == expected
    assert tokens.verify_source_ticket(second) == expected
    one = _oracle_verify(first, _ticket_key(INTERNAL_TOKEN))
    two = _oracle_verify(second, _ticket_key(INTERNAL_TOKEN))
    assert one["exp"] == two["exp"] == NOW + config.SOURCE_TICKET_TTL_SECONDS
    assert one["jti"] != two["jti"]


@pytest.mark.parametrize("field", ("chat_id", "file_id", "version", "session_id"))
def test_each_ticket_binding_tamper_is_rejected(field):
    ticket = tokens.sign_source_ticket(CHAT, FILE, VERSION, SESSION)
    header, body, signature = ticket.split(".")
    payload = json.loads(_b64url_decode(body))
    payload[field] = 99 if field == "version" else f"other-{field}"
    tampered = f"{header}.{_b64url(json.dumps(payload, separators=(',', ':')).encode('utf-8'))}.{signature}"
    with pytest.raises(tokens.InvalidTokenError) as excinfo:
        tokens.verify_source_ticket(tampered)
    _assert_generic(excinfo, INTERNAL_TOKEN, JWT_SECRET, ticket, tampered)


def test_expired_ticket_and_expiry_boundary(monkeypatch):
    ticket = tokens.sign_source_ticket(CHAT, FILE, VERSION, SESSION)
    monkeypatch.setattr("time.time", lambda: NOW + config.SOURCE_TICKET_TTL_SECONDS - 1)
    assert tokens.verify_source_ticket(ticket)["chat_id"] == CHAT
    monkeypatch.setattr("time.time", lambda: NOW + config.SOURCE_TICKET_TTL_SECONDS)
    with pytest.raises(tokens.InvalidTokenError):
        tokens.verify_source_ticket(ticket)


def test_documentserver_secret_cannot_forge_a_source_ticket():
    forged = _oracle_sign(
        {
            "chat_id": CHAT,
            "file_id": FILE,
            "version": VERSION,
            "session_id": SESSION,
            "exp": NOW + 300,
        },
        JWT_SECRET,
    )
    with pytest.raises(tokens.InvalidTokenError) as excinfo:
        tokens.verify_source_ticket(forged)
    _assert_generic(excinfo, JWT_SECRET, INTERNAL_TOKEN, forged)


def test_internal_token_rotation_invalidates_existing_tickets(monkeypatch):
    ticket = tokens.sign_source_ticket(CHAT, FILE, VERSION, SESSION)
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", "rotated-internal-token")
    with pytest.raises(tokens.InvalidTokenError):
        tokens.verify_source_ticket(ticket)
    fresh = tokens.sign_source_ticket(CHAT, FILE, VERSION, SESSION)
    assert tokens.verify_source_ticket(fresh)["session_id"] == SESSION


def test_ticket_rejects_empty_identities_and_non_positive_or_boolean_version():
    key = _ticket_key(INTERNAL_TOKEN)
    base = {
        "chat_id": CHAT,
        "file_id": FILE,
        "version": VERSION,
        "session_id": SESSION,
        "exp": NOW + 300,
    }
    for override in (
        {"chat_id": ""},
        {"file_id": ""},
        {"session_id": ""},
        {"version": 0},
        {"version": -1},
        {"version": True},
        {"version": 1.5},
        {"chat_id": 1},
    ):
        payload = dict(base)
        payload.update(override)
        with pytest.raises(tokens.InvalidTokenError):
            tokens.verify_source_ticket(_oracle_sign(payload, key))


def test_missing_internal_token_does_not_sign_a_ticket(monkeypatch):
    monkeypatch.delenv("OCU_INTERNAL_TOKEN", raising=False)
    with pytest.raises(ValueError):
        tokens.sign_source_ticket(CHAT, FILE, VERSION, SESSION)
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", " \t")
    with pytest.raises(ValueError):
        tokens.sign_source_ticket(CHAT, FILE, VERSION, SESSION)
