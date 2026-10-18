# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
import os
import re
import sys
from contextlib import nullcontext

import pytest

from tests.orchestrator.test_outputs_endpoint import (
    CHAT,
    _APP_MODULES,
    _auth,
    _client,
    _isolated_app,
    _put,
)


def test_unset_prefix_ignores_inherited_prefix_for_static_and_output_urls(tmp_path, monkeypatch):
    monkeypatch.setenv("OCU_PUBLIC_PREFIX", "/ocu")
    with _isolated_app(tmp_path, prefix=None) as (loaded, _manager, _broker, data):
        client = _client(loaded)
        try:
            static = client.get("/static/preview.css")
            assert static.status_code == 200
            assert "text/css" in static.headers["content-type"]
            assert "Preview SPA" in static.text

            _put(data, "root output.txt", b"unset-prefix output\n")
            preview = client.get(f"/preview/{CHAT}", headers=_auth())
            assert preview.status_code == 200
            urls = dict(re.findall(r'\b(apiUrl|filesBase):\s*"([^"]*)"', preview.text))
            assert urls == {
                "apiUrl": f"/api/outputs/{CHAT}",
                "filesBase": f"/files/{CHAT}",
            }

            listing = client.get(urls["apiUrl"], headers=_auth())
            assert listing.status_code == 200
            body = listing.json()
            assert body["chat_id"] == CHAT
            assert body["total"] == 1
            entry, = body["files"]
            assert entry["path"] == "root output.txt"
            assert entry["url"] == f"/files/{CHAT}/root%20output.txt"

            fetched = client.get(entry["url"], headers=_auth())
            assert fetched.status_code == 200
            assert fetched.content == b"unset-prefix output\n"
        finally:
            client.close()


@pytest.mark.parametrize(
    ("options", "inherited", "expected_prefix", "expected_live_prefix"),
    (
        pytest.param({"prefix": None}, "/ocu", "", None, id="unset"),
        pytest.param({"prefix": ""}, "/ocu", "", "", id="explicit-empty"),
        pytest.param({"prefix": "/ocu"}, "/ocu", "/ocu", "/ocu", id="explicit-ocu"),
        pytest.param({}, "/ocu", "/ocu", "/ocu", id="default"),
        pytest.param({"prefix": None}, "ocu", "", None, id="unset-invalid-parent"),
    ),
)
@pytest.mark.parametrize("intentional_failure", (False, True), ids=("normal", "assertion"))
def test_prefix_mode_requests_restore_parent_state(
    tmp_path, monkeypatch, options, inherited, expected_prefix, expected_live_prefix,
    intentional_failure,
):
    original_env = os.environ.copy()
    original_modules = {
        name: module for name, module in sys.modules.copy().items()
        if name in _APP_MODULES or name.startswith("mcp_resources")
    }
    parent_root = tmp_path / "parent"
    child_root = tmp_path / "child"
    parent_root.mkdir()
    child_root.mkdir()

    with _isolated_app(parent_root, prefix="/ocu") as (parent_app, _manager, _broker, _data):
        with monkeypatch.context() as parent_env:
            parent_env.setenv("OCU_PUBLIC_PREFIX", inherited)
            saved_env = os.environ.copy()
            saved_modules = {
                name: module for name, module in sys.modules.copy().items()
                if name in _APP_MODULES or name.startswith("mcp_resources")
            }
            assert saved_modules["app"] is parent_app
            body_error = AssertionError("intentional prefix body failure")
            outcome = (
                pytest.raises(AssertionError, match="^intentional prefix body failure$")
                if intentional_failure else nullcontext()
            )

            with outcome as caught:
                with _isolated_app(child_root, **options) as (loaded, _manager, _broker, data):
                    client = _client(loaded)
                    try:
                        static = client.get(f"{expected_prefix}/static/preview.css")
                        assert static.status_code == 200
                        assert "text/css" in static.headers["content-type"]
                        assert "Preview SPA" in static.text
                        if expected_prefix:
                            assert client.get("/static/preview.css").status_code == 404

                        _put(data, "mode output.txt", b"prefix-mode output\n")
                        preview = client.get(f"/preview/{CHAT}", headers=_auth())
                        assert preview.status_code == 200
                        urls = dict(re.findall(r'\b(apiUrl|filesBase):\s*"([^"]*)"', preview.text))
                        assert urls == {
                            "apiUrl": f"{expected_prefix}/api/outputs/{CHAT}",
                            "filesBase": f"{expected_prefix}/files/{CHAT}",
                        }

                        listing = client.get(
                            urls["apiUrl"][len(expected_prefix):], headers=_auth(),
                        )
                        assert listing.status_code == 200
                        listing_body = listing.json()
                        assert listing_body["chat_id"] == CHAT
                        assert listing_body["total"] == 1
                        entry, = listing_body["files"]
                        assert entry["path"] == "mode output.txt"
                        assert entry["url"] == f"{expected_prefix}/files/{CHAT}/mode%20output.txt"
                        fetched = client.get(
                            entry["url"][len(expected_prefix):], headers=_auth(),
                        )
                        assert fetched.status_code == 200
                        assert fetched.content == b"prefix-mode output\n"
                        assert ("OCU_PUBLIC_PREFIX" in os.environ) == (
                            expected_live_prefix is not None
                        )
                        assert os.environ.get("OCU_PUBLIC_PREFIX") == expected_live_prefix
                        if intentional_failure:
                            raise body_error
                    finally:
                        client.close()

            if intentional_failure:
                assert caught.value is body_error
            assert dict(os.environ) == saved_env
            restored_modules = {
                name: module for name, module in sys.modules.copy().items()
                if name in _APP_MODULES or name.startswith("mcp_resources")
            }
            assert restored_modules.keys() == saved_modules.keys()
            assert all(restored_modules[name] is module for name, module in saved_modules.items())

    assert dict(os.environ) == original_env
    final_modules = {
        name: module for name, module in sys.modules.copy().items()
        if name in _APP_MODULES or name.startswith("mcp_resources")
    }
    assert final_modules.keys() == original_modules.keys()
    assert all(final_modules[name] is module for name, module in original_modules.items())
