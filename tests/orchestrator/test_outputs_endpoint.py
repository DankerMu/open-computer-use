# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""HTTP contract for broker-backed output listing and describe revision.

Real FastAPI requests exercise reconciliation, encoded prefixed URLs, pagination,
conditional GET, and generic broker-error mapping. Docker is never a live client
on listing; describe uses a MagicMock engine client.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import quote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = ROOT / "computer-use-server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

CHAT = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
CHAT_B = "b2c3d4e5-f6a7-8901-bcde-f12345678901"
INTERNAL = "ocu-outputs-endpoint-test-token"
MCP_KEY = "ocu-outputs-endpoint-mcp-key"
NO_DOCKER_SOCKET = "unix:///tmp/ocu-acceptance-no-docker.sock"
PREFIX = "/ocu"
NESTED_NAME = "space file 你好 50%.txt?x#y"
NESTED_RELATIVE = f"nested/{NESTED_NAME}"
NESTED_BODY = b"encoded-roundtrip"
CACHE_CONTROL = "no-cache, no-store, must-revalidate"
GENERIC_FAILURE = "outputs listing failed"

_APP_MODULES = (
    "app",
    "auth_guard",
    "mcp_tools",
    "docker_manager",
    "outputs_broker",
    "context_vars",
    "security",
    "system_prompt",
    "skill_manager",
    "cli_runtime",
    "uploads",
    "docs_html",
)


def _subprocess_env(data: Path, prefix=PREFIX):
    env = os.environ.copy()
    env["DOCKER_HOST"] = NO_DOCKER_SOCKET
    env["DOCKER_SOCKET"] = NO_DOCKER_SOCKET
    env["OCU_INTERNAL_TOKEN"] = INTERNAL
    env["MCP_API_KEY"] = MCP_KEY
    env["PUBLIC_BASE_URL"] = "http://ocu.example"
    env["OCU_WEBUI_ORIGIN"] = "https://webui.example"
    env["OCU_SANDBOX_SUBNET"] = "10.90.0.0/24"
    env["SINGLE_USER_MODE"] = "true"
    env["BASE_DATA_DIR"] = str(data)
    env["USER_DATA_BASE_PATH"] = str(data.parent / "user-data")
    if prefix is None:
        env.pop("OCU_PUBLIC_PREFIX", None)
    else:
        env["OCU_PUBLIC_PREFIX"] = prefix
    return env


@contextmanager
def _isolated_app(tmp_path, prefix=PREFIX):
    snapshot = {name: sys.modules[name] for name in list(sys.modules) if name in _APP_MODULES or name.startswith("mcp_resources")}
    saved_env = os.environ.copy()
    data = tmp_path / "data"
    data.mkdir()
    docker_manager = sys.modules.get("docker_manager")
    prior_base = getattr(docker_manager, "BASE_DATA_DIR", None)
    try:
        os.environ.update(_subprocess_env(data, prefix=prefix))
        for name in list(sys.modules):
            if name in _APP_MODULES or name.startswith("mcp_resources"):
                sys.modules.pop(name, None)
        import app as loaded
        import docker_manager
        import outputs_broker

        docker_manager.BASE_DATA_DIR = data
        loaded.BASE_DATA_DIR = data
        docker_manager._chat_locks.clear()
        docker_manager._FLOCK_DEPTH.clear()
        docker_manager._docker_client = None
        yield loaded, docker_manager, outputs_broker, data
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        for name in list(sys.modules):
            if name in _APP_MODULES or name.startswith("mcp_resources"):
                sys.modules.pop(name, None)
        sys.modules.update(snapshot)
        docker_manager = sys.modules.get("docker_manager")
        if docker_manager is not None:
            if prior_base is not None:
                docker_manager.BASE_DATA_DIR = prior_base
            docker_manager._FLOCK_DEPTH.clear()
            docker_manager._chat_locks.clear()
            docker_manager._docker_client = None


def _client(loaded):
    from fastapi.testclient import TestClient

    return TestClient(loaded.app, raise_server_exceptions=True)


def _auth(token=INTERNAL):
    return {"Authorization": f"Bearer {token}"}


def _outputs(data: Path, chat: str = CHAT) -> Path:
    return data / chat / "outputs"


def _index(data: Path, chat: str = CHAT) -> Path:
    return data / chat / ".ocu" / "index.json"


def _put(data: Path, relative_path: str, body: bytes, chat: str = CHAT) -> Path:
    path = _outputs(data, chat) / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _list_url(chat=CHAT, **query):
    path = f"/api/outputs/{chat}"
    if not query:
        return path
    parts = []
    for key, value in query.items():
        if value is None:
            continue
        parts.append(f"{key}={value}")
    return path + "?" + "&".join(parts)


def _etag_payload(body: dict, limit: int) -> dict:
    return {
        "chat_id": body["chat_id"],
        "files": body["files"],
        "total": body["total"],
        "revision": body["revision"],
        "next_cursor": body["next_cursor"],
        "limit": limit,
    }


def _expected_etag(body: dict, limit: int) -> str:
    encoded = json.dumps(_etag_payload(body, limit), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return f'W/"{digest}"'


def _expected_url(relative_path: str, prefix=PREFIX, chat=CHAT) -> str:
    encoded = quote(relative_path, safe="/")
    return f"{prefix}/files/{chat}/{encoded}"


def _assert_cache_headers(response, etag=None):
    assert response.headers["Cache-Control"] == CACHE_CONTROL
    if etag is not None:
        assert response.headers["ETag"] == etag


def test_listing_returns_broker_fields_prefixed_encoded_url_and_modified(tmp_path, monkeypatch):
    with _isolated_app(tmp_path) as (loaded, docker_manager, _broker, data):
        calls = {"count": 0}

        def forbidden_docker(*_args, **_kwargs):
            calls["count"] += 1
            raise AssertionError("listing must not contact Docker")

        monkeypatch.setattr(docker_manager, "get_docker_client", forbidden_docker)
        path = _put(data, NESTED_RELATIVE, NESTED_BODY)
        http = _client(loaded)
        response = http.get(_list_url(), headers=_auth())
        assert response.status_code == 200
        body = response.json()
        assert body["chat_id"] == CHAT
        assert body["total"] == 1
        assert body["revision"] == 1
        assert body["next_cursor"] is None
        assert isinstance(body["timestamp"], float)
        entry = body["files"][0]
        persisted = json.loads(_index(data).read_text(encoding="utf-8"))
        stored = persisted["active"][NESTED_RELATIVE]
        file_type, mime = loaded.classify_file(NESTED_RELATIVE)
        expected_url = _expected_url(NESTED_RELATIVE)
        assert entry["file_id"] == stored["file_id"]
        assert entry["path"] == NESTED_RELATIVE
        assert entry["name"] == NESTED_NAME
        assert entry["size"] == len(NESTED_BODY)
        assert entry["mtime_ns"] == path.stat().st_mtime_ns
        assert entry["revision"] == stored["revision"]
        assert entry["hash"] == hashlib.sha256(NESTED_BODY).hexdigest()
        assert entry["type"] == file_type
        assert entry["mime"] == mime
        assert entry["url"] == expected_url
        assert entry["modified"] == pytest.approx(path.stat().st_mtime_ns / 1e9)
        assert expected_url.startswith(f"{PREFIX}/files/{CHAT}/")
        assert " " not in expected_url
        assert "?" not in urlsplit(expected_url).path
        assert "#" not in urlsplit(expected_url).path
        _assert_cache_headers(response, _expected_etag(body, 100))
        internal_url = expected_url[len(PREFIX):]
        assert internal_url.startswith(f"/files/{CHAT}/")
        fetched = http.get(internal_url, headers=_auth())
        assert fetched.status_code == 200
        assert fetched.content == NESTED_BODY
        assert calls["count"] == 0


def test_pagination_is_path_ordered_and_cursor_errors_are_mapped(tmp_path):
    with _isolated_app(tmp_path) as (loaded, _docker_manager, _broker, data):
        for name in ("z.txt", "a.txt", "m.txt"):
            _put(data, name, name.encode())
        http = _client(loaded)
        first = http.get(_list_url(limit=2), headers=_auth())
        assert first.status_code == 200
        first_body = first.json()
        assert [entry["path"] for entry in first_body["files"]] == ["a.txt", "m.txt"]
        assert first_body["total"] == 3
        assert first_body["next_cursor"] == f"{first_body['revision']}:2"
        _assert_cache_headers(first, _expected_etag(first_body, 2))

        second = http.get(
            _list_url(cursor=first_body["next_cursor"], limit=2),
            headers=_auth(),
        )
        assert second.status_code == 200
        second_body = second.json()
        assert [entry["path"] for entry in second_body["files"]] == ["z.txt"]
        assert second_body["next_cursor"] is None
        assert second_body["revision"] == first_body["revision"]

        _put(data, "new.txt", b"new")
        stale = http.get(
            _list_url(cursor=first_body["next_cursor"], limit=2),
            headers=_auth(),
        )
        assert stale.status_code == 409
        assert GENERIC_FAILURE in str(stale.json()["detail"])

        malformed = http.get(_list_url(cursor="not-a-cursor", limit=2), headers=_auth())
        assert malformed.status_code == 400
        current = http.get(_list_url(limit=2), headers=_auth()).json()
        out_of_range = http.get(
            _list_url(cursor=f"{current['revision']}:99", limit=2),
            headers=_auth(),
        )
        invalid_limit = http.get(_list_url(limit=0), headers=_auth())
        assert invalid_limit.status_code == 422
        over_limit = http.get(_list_url(limit=1001), headers=_auth())
        assert over_limit.status_code == 422


def test_conditional_get_matches_after_reconcile_and_distinguishes_pages(tmp_path):
    with _isolated_app(tmp_path) as (loaded, _docker_manager, _broker, data):
        for name in ("a.txt", "b.txt", "c.txt"):
            _put(data, name, name.encode())
        http = _client(loaded)
        first = http.get(_list_url(limit=2), headers=_auth())
        first_body = first.json()
        etag = first.headers["ETag"]
        assert etag == _expected_etag(first_body, 2)
        index_before = _index(data).read_bytes()

        unchanged = http.get(
            _list_url(limit=2),
            headers={**_auth(), "If-None-Match": etag},
        )
        assert unchanged.status_code == 304
        assert unchanged.content == b""
        _assert_cache_headers(unchanged, etag)
        assert _index(data).read_bytes() == index_before

        weak = http.get(
            _list_url(limit=2),
            headers={**_auth(), "If-None-Match": etag.removeprefix("W/")},
        )
        assert weak.status_code == 304
        listed = http.get(
            _list_url(limit=2),
            headers={**_auth(), "If-None-Match": f'{etag}, W/"deadbeef"'},
        )
        assert listed.status_code == 304
        wildcard = http.get(
            _list_url(limit=2),
            headers={**_auth(), "If-None-Match": "*"},
        )
        assert wildcard.status_code == 304
        malformed_header = http.get(
            _list_url(limit=2),
            headers={**_auth(), "If-None-Match": "not-a-tag"},
        )
        assert malformed_header.status_code == 200
        assert malformed_header.json()["files"][0]["path"] == "a.txt"
        mixed_wildcard = http.get(
            _list_url(limit=2),
            headers={**_auth(), "If-None-Match": "*,junk"},
        )
        assert mixed_wildcard.status_code == 200
        mixed_valid = http.get(
            _list_url(limit=2),
            headers={**_auth(), "If-None-Match": f"junk,{etag}"},
        )
        assert mixed_valid.status_code == 200
        quoted_comma = http.get(
            _list_url(limit=2),
            headers={**_auth(), "If-None-Match": f'W/"dead,beef", {etag}'},
        )
        assert quoted_comma.status_code == 304
        assert quoted_comma.content == b""

        other_page = http.get(
            _list_url(cursor=first_body["next_cursor"], limit=2),
            headers={**_auth(), "If-None-Match": etag},
        )
        assert other_page.status_code == 200
        other_body = other_page.json()
        assert [entry["path"] for entry in other_body["files"]] == ["c.txt"]
        assert other_page.headers["ETag"] != etag

        other_limit = http.get(
            _list_url(limit=1),
            headers={**_auth(), "If-None-Match": etag},
        )
        assert other_limit.status_code == 200
        assert len(other_limit.json()["files"]) == 1

        grown = _put(data, "a.txt", b"larger-than-before")
        changed = http.get(
            _list_url(limit=2),
            headers={**_auth(), "If-None-Match": etag},
        )
        assert changed.status_code == 200
        changed_body = changed.json()
        assert changed_body["revision"] == first_body["revision"] + 1
        assert changed_body["files"][0]["size"] == grown.stat().st_size
        assert changed.headers["ETag"] == _expected_etag(changed_body, 2)
        assert changed.headers["ETag"] != etag


def test_authorization_and_failures_precede_matching_validator(tmp_path):
    with _isolated_app(tmp_path) as (loaded, _docker_manager, _broker, data):
        _put(data, "keep.txt", b"keep")
        http = _client(loaded)
        listed = http.get(_list_url(), headers=_auth())
        etag = listed.headers["ETag"]

        denied = http.get(
            _list_url(),
            headers={"If-None-Match": etag},
        )
        assert denied.status_code == 401
        wildcard_denied = http.get(
            _list_url(),
            headers={"If-None-Match": "*"},
        )
        assert wildcard_denied.status_code == 401

        stale = http.get(
            _list_url(cursor="0:0"),
            headers={**_auth(), "If-None-Match": etag},
        )
        assert stale.status_code == 409
        malformed = http.get(
            _list_url(cursor="bad"),
            headers={**_auth(), "If-None-Match": "*"},
        )
        assert malformed.status_code == 400

        corrupt_bytes = b"{not-json"
        _index(data).write_bytes(corrupt_bytes)
        failed = http.get(
            _list_url(),
            headers={**_auth(), "If-None-Match": "*"},
        )
        assert failed.status_code == 500
        assert GENERIC_FAILURE in str(failed.json()["detail"])
        assert "/tmp/" not in str(failed.json()["detail"])
        assert _index(data).read_bytes() == corrupt_bytes


def test_injected_broker_errors_map_without_false_success(tmp_path, monkeypatch):
    with _isolated_app(tmp_path) as (loaded, _docker_manager, broker_module, data):
        _put(data, "keep.txt", b"keep")
        http = _client(loaded)
        baseline = http.get(_list_url(), headers=_auth())
        assert baseline.status_code == 200
        assert baseline.json()["files"][0]["path"] == "keep.txt"
        cases = (
            (broker_module.StaleCursorError("stale /host/path/index.json"), 409, None),
            (broker_module.CursorError("bad cursor /host/path"), 400, None),
            (broker_module.UnstableReadError("changed /host/path/outputs"), 503, "1"),
            (broker_module.LimitExceededError("too many files in /host/path"), 413, None),
            (broker_module.CorruptIndexError("corrupt /host/path/index.json"), 500, None),
            (broker_module.UnsafePathError("symlink /host/path"), 500, None),
            (broker_module.UnsupportedNameError("backslash in /host/path"), 500, None),
            (broker_module.CommitDurabilityError("unsynced /host/path"), 500, None),
            (broker_module.OutputsBrokerError("other /host/path"), 500, None),
            (OSError("io /host/path"), 500, None),
        )
        for exc, status, retry_after in cases:
            def boom(_chat_id, cursor=None, limit=100, _exc=exc):
                raise _exc

            monkeypatch.setattr(loaded._OUTPUTS_BROKER, "reconcile", boom)
            response = http.get(
                _list_url(),
                headers={**_auth(), "If-None-Match": "*"},
            )
            assert response.status_code == status, exc
            assert response.status_code not in {200, 304}
            detail = str(response.json()["detail"])
            assert GENERIC_FAILURE in detail
            assert "/host/path" not in detail
            assert "index.json" not in detail
            if retry_after is not None:
                assert response.headers["Retry-After"] == retry_after
            else:
                assert "Retry-After" not in response.headers
        monkeypatch.undo()
        restored = http.get(_list_url(), headers=_auth())
        assert restored.status_code == 200
        assert restored.json()["files"][0]["path"] == "keep.txt"


def _container(name, status="running", container_id="cid-1"):
    container = MagicMock(name=name)
    container.name = name
    container.id = container_id
    container.status = status
    return container


def _docker(containers=None):
    client = MagicMock(name="docker-client")
    store = {item.name: item for item in containers or []}
    created = []

    def get(name):
        import docker as docker_sdk

        if name not in store:
            raise docker_sdk.errors.NotFound(name)
        return store[name]

    client.containers.get.side_effect = get
    client._store = store
    client._created = created
    return client


def test_describe_reads_persisted_revision_without_scan_or_index_create(tmp_path, monkeypatch):
    with _isolated_app(tmp_path) as (loaded, docker_manager, broker_module, data):
        _put(data, "keep.txt", b"keep")
        listing = broker_module.OutputsBroker().reconcile(CHAT)
        before = _index(data).read_bytes()
        client = _docker()
        docker_manager._docker_client = None
        monkeypatch.setattr(docker_manager, "get_docker_client", lambda: client)
        monkeypatch.setattr(
            docker_manager,
            "cli_badge",
            lambda: {"cli": "claude", "default_model": "sonnet", "supports_cost": True},
        )
        running = _container(f"owui-chat-{CHAT}", "running")
        client._store[running.name] = running
        http = _client(loaded)
        headers = _auth()

        described = http.get(f"/internal/describe/{CHAT}", headers=headers)
        assert described.status_code == 200
        assert described.json() == {
            "state": "running",
            "revision": listing["revision"],
            "views": ["files", "browser", "terminal"],
            "cli_badge": {"cli": "claude", "default_model": "sonnet", "supports_cost": True},
        }
        running.start.assert_not_called()
        assert _index(data).read_bytes() == before
        assert client._created == []

        paused = _container(f"owui-chat-{CHAT_B}", "paused")
        client._store[paused.name] = paused
        stopped = http.get(f"/internal/describe/{CHAT_B}", headers=headers)
        assert stopped.status_code == 200
        assert stopped.json()["state"] == "stopped"
        assert stopped.json()["revision"] == 0
        assert not _index(data, CHAT_B).exists()
        paused.unpause.assert_not_called()

        missing = http.get(
            f"/internal/describe/c3d4e5f6-a7b8-9012-cdef-123456789012",
            headers=headers,
        )
        assert missing.status_code == 200
        assert missing.json()["revision"] == 0
        assert not _index(data, "c3d4e5f6-a7b8-9012-cdef-123456789012").exists()

        corrupt_bytes = b"{not-json"
        _index(data).write_bytes(corrupt_bytes)
        failed = http.get(f"/internal/describe/{CHAT}", headers=headers)
        assert failed.status_code == 500
        detail = str(failed.json()["detail"])
        assert GENERIC_FAILURE in detail
        assert "/tmp/" not in detail
        assert "index.json" not in detail
        assert _index(data).read_bytes() == corrupt_bytes
        running.start.assert_not_called()
