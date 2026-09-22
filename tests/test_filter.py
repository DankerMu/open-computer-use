# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Tests for computer_link_filter (Open WebUI Function).

Run: python -m pytest tests/test_filter.py -v
"""

import os
import socket
import sys
import threading
import time
import unittest
import urllib.error
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs
from unittest.mock import MagicMock, patch

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, RedirectResponse
from starlette.routing import Route

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "openwebui" / "functions"))
sys.path.insert(0, str(ROOT / "computer-use-server"))

import auth_guard  # noqa: E402
import computer_link_filter  # noqa: E402


def _urlopen_mock(
    text: str = "PROMPT",
    public_base_url: str = "http://localhost:8081",
) -> MagicMock:
    """Create a mock satisfying: with urlopen(req, timeout=N) as resp: resp.read() + resp.headers.

    `public_base_url` is the value returned by the server in the X-Public-Base-URL
    header — outlet() builds browser-facing links from it.
    """
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=cm)
    cm.__exit__ = MagicMock(return_value=False)
    cm.read.return_value = text.encode("utf-8")
    cm.headers = {"X-Public-Base-URL": public_base_url}
    return cm


def _make_filter(
    orchestrator_url: str = "http://localhost:8081",
) -> "computer_link_filter.Filter":
    f = computer_link_filter.Filter()
    f.valves.ORCHESTRATOR_URL = orchestrator_url
    return f


def _prime_cache(
    f: "computer_link_filter.Filter",
    chat_id: str,
    user_email: str = "",
    public_url: str = "http://localhost:8081",
    prompt: str = "PROMPT",
) -> None:
    """Seed the filter's prompt cache so outlet() has a public_url to decorate with.

    outlet() never invents a public URL — it pulls from cache populated by inlet().
    Tests that exercise outlet() in isolation must prime the cache first.
    """
    f._prompt_cache[(chat_id, user_email)] = (time.time(), (public_url, prompt))


def _active_body() -> dict:
    return {
        "tool_ids": ["ai_computer_use"],
        "messages": [{"role": "user", "content": "hi"}],
    }


def _system_content(body: dict) -> str:
    for m in body["messages"]:
        if m.get("role") == "system":
            return m.get("content", "")
    return ""


def _assistant_body_with_file(chat_id: str = "abc") -> dict:
    link = f"http://localhost:8081/files/{chat_id}/report.pdf"
    return {"messages": [{"role": "assistant", "content": f"see {link}"}]}


@contextmanager
def _live_asgi(app):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("test HTTP origin did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _prompt_app(
    records,
    prompt: str,
    public_base_url: str | None,
    guarded: bool = False,
    status: int = 200,
):
    async def system_prompt(_request):
        headers = {}
        if public_base_url is not None:
            headers["X-Public-Base-URL"] = public_base_url
        return PlainTextResponse(prompt, headers=headers, status_code=status)

    application = Starlette(routes=[Route("/system-prompt", system_prompt)])
    if guarded:
        application = auth_guard.AuthGuardMiddleware(application)

    async def record(scope, receive, send):
        if scope["type"] == "http":
            records.append(
                {
                    "headers": {
                        name.decode("latin-1").lower(): value.decode("latin-1")
                        for name, value in scope.get("headers", [])
                    },
                    "query": parse_qs(scope["query_string"].decode("ascii")),
                }
            )
        await application(scope, receive, send)

    return record


def _disable_proxy_env(monkeypatch):
    for name in (
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "all_proxy",
        "http_proxy",
        "https_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


def _configure_filter_token(monkeypatch, token: str = "filter-test-token"):
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", token)
    monkeypatch.delenv("OCU_SANDBOX_SUBNET", raising=False)
    monkeypatch.delenv("OCU_WEBUI_ORIGIN", raising=False)


@pytest.fixture(autouse=True)
def _filter_test_token(monkeypatch):
    _configure_filter_token(monkeypatch)


def test_inlet_authenticates_real_guard_and_preserves_insertion(monkeypatch):
    _disable_proxy_env(monkeypatch)
    _configure_filter_token(monkeypatch)
    records = []
    with _live_asgi(
        _prompt_app(
            records,
            "server prompt",
            "https://webui.example/ocu",
            guarded=True,
        )
    ) as origin:
        filter_ = _make_filter(origin)
        body = {
            "tool_ids": ["ai_computer_use"],
            "user_email": "forged@example.test",
            "messages": [
                {
                    "role": "system",
                    "content": "before\n\n{{USER_NAME}}\n\nafter",
                },
                {"role": "user", "content": "hi"},
            ],
        }
        result = filter_.inlet(
            body,
            __user__={"email": "person@example.test"},
            __metadata__={"chat_id": "abc"},
        )

    assert result["messages"] == [
        {
            "role": "system",
            "content": "before\n\nserver prompt\n\n{{USER_NAME}}\n\nafter",
        },
        {"role": "user", "content": "hi"},
    ]
    assert len(records) == 1
    assert records[0]["headers"]["authorization"] == "Bearer filter-test-token"
    assert records[0]["query"] == {
        "chat_id": ["abc"],
        "user_email": ["person@example.test"],
    }


def test_guard_rejection_does_not_reuse_a_stale_prompt(monkeypatch, capsys):
    _disable_proxy_env(monkeypatch)
    _configure_filter_token(monkeypatch, "wrong-filter-token")
    monkeypatch.setattr(auth_guard, "_token", lambda: "server-filter-token")
    records = []
    with _live_asgi(
        _prompt_app(
            records,
            "not authorized",
            "https://webui.example/ocu",
            guarded=True,
        )
    ) as origin:
        filter_ = _make_filter(origin)
        filter_._prompt_cache[("abc", "")] = (
            time.time() - 9999,
            ("https://webui.example/ocu", "stale prompt"),
        )
        filter_._cache_authority = (origin, "wrong-filter-token")
        result = filter_.inlet(_active_body(), __metadata__={"chat_id": "abc"})

    assert _system_content(result) == ""
    assert records[0]["headers"]["authorization"] == "Bearer wrong-filter-token"
    captured = capsys.readouterr()
    assert "wrong-filter-token" not in captured.out + captured.err


def test_forbidden_response_does_not_reuse_a_stale_prompt(monkeypatch):
    _disable_proxy_env(monkeypatch)
    _configure_filter_token(monkeypatch)
    records = []
    with _live_asgi(
        _prompt_app(
            records,
            "forbidden",
            "https://webui.example/ocu",
            status=403,
        )
    ) as origin:
        filter_ = _make_filter(origin)
        filter_._prompt_cache[("abc", "")] = (
            time.time() - 9999,
            ("https://webui.example/ocu", "stale prompt"),
        )
        filter_._cache_authority = (origin, "filter-test-token")
        result = filter_.inlet(_active_body(), __metadata__={"chat_id": "abc"})

    assert _system_content(result) == ""
    assert records[0]["headers"]["authorization"] == "Bearer filter-test-token"


def test_missing_filter_credential_does_not_use_a_warm_prompt(monkeypatch):
    monkeypatch.delenv("OCU_INTERNAL_TOKEN", raising=False)
    filter_ = _make_filter()
    filter_._prompt_cache[("abc", "")] = (
        time.time(),
        ("https://webui.example/ocu", "warm prompt"),
    )
    result = filter_.inlet(_active_body(), __metadata__={"chat_id": "abc"})
    assert _system_content(result) == ""


def test_missing_public_base_header_does_not_use_internal_url(monkeypatch):
    _disable_proxy_env(monkeypatch)
    _configure_filter_token(monkeypatch)
    records = []
    with _live_asgi(_prompt_app(records, "headerless prompt", None)) as origin:
        filter_ = _make_filter(origin)
        result = filter_.inlet(_active_body(), __metadata__={"chat_id": "abc"})

    assert _system_content(result) == ""
    assert len(records) == 1


def test_cache_refreshes_when_token_or_internal_origin_changes(monkeypatch):
    _disable_proxy_env(monkeypatch)
    _configure_filter_token(monkeypatch, "first-filter-token")
    first_records = []
    second_records = []
    with _live_asgi(
        _prompt_app(first_records, "first prompt", "https://webui.example/ocu")
    ) as first_origin, _live_asgi(
        _prompt_app(second_records, "second prompt", "https://webui.example/ocu")
    ) as second_origin:
        filter_ = _make_filter(first_origin)
        assert filter_._fetch_system_prompt("abc", "person@example.test") == (
            "https://webui.example/ocu",
            "first prompt",
        )
        filter_.valves.ORCHESTRATOR_URL = second_origin
        assert filter_._fetch_system_prompt("abc", "person@example.test") == (
            "https://webui.example/ocu",
            "second prompt",
        )
        monkeypatch.setenv("OCU_INTERNAL_TOKEN", "second-filter-token")
        assert filter_._fetch_system_prompt("abc", "person@example.test") == (
            "https://webui.example/ocu",
            "second prompt",
        )

    assert len(first_records) == 1
    assert len(second_records) == 2
    assert first_records[0]["headers"]["authorization"] == "Bearer first-filter-token"
    assert second_records[0]["headers"]["authorization"] == "Bearer first-filter-token"
    assert second_records[1]["headers"]["authorization"] == "Bearer second-filter-token"


def test_outlet_does_not_reuse_cached_links_after_credential_rotation(monkeypatch):
    _configure_filter_token(monkeypatch, "first-filter-token")
    filter_ = _make_filter()
    _prime_cache(filter_, "abc", public_url="https://webui.example/ocu")
    filter_._cache_authority = (
        filter_.valves.ORCHESTRATOR_URL.rstrip("/"),
        "first-filter-token",
    )
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", "second-filter-token")
    file_url = "https://webui.example/ocu/files/abc/report.html"
    body = {"messages": [{"role": "assistant", "content": file_url}]}

    with patch(
        "urllib.request.OpenerDirector.open",
        side_effect=urllib.error.URLError("connection refused"),
    ):
        result = filter_.outlet(body, __metadata__={"chat_id": "abc"})

    assert result["messages"][0]["content"] == file_url


def test_redirect_does_not_contact_the_other_origin(monkeypatch):
    _disable_proxy_env(monkeypatch)
    _configure_filter_token(monkeypatch)
    target_records = []
    target_app = _prompt_app(
        target_records,
        "redirect target prompt",
        "https://webui.example/ocu",
    )
    with _live_asgi(target_app) as target:
        async def redirect(_request):
            return RedirectResponse(f"{target}/system-prompt", status_code=302)

        origin = Starlette(routes=[Route("/system-prompt", redirect)])
        with _live_asgi(origin) as source:
            result = _make_filter(source)._fetch_system_prompt("abc", "")

    assert result is None
    assert target_records == []


def test_outlet_links_the_first_current_chat_file_without_a_preview_shell(monkeypatch):
    _configure_filter_token(monkeypatch)
    public_base = "https://webui.example/ocu"
    first_file = f"{public_base}/files/abc/first%20report.html?download=1#section"
    second_file = f"{public_base}/files/abc/second.pdf"
    filter_ = _make_filter()
    _prime_cache(filter_, "abc", public_url=public_base)
    body = {
        "messages": [
            {
                "role": "assistant",
                "content": f"first {first_file} then {second_file}",
            }
        ]
    }

    result = filter_.outlet(body, __metadata__={"chat_id": "abc"})
    content = result["messages"][0]["content"]
    assert f"[🖥️ Open preview]({first_file})" in content
    assert f"[🖥️ Open preview]({second_file})" not in content
    assert "/preview/" not in content
    assert f"[📦 Download all files as archive]({public_base}/files/abc/archive)" in content


def test_outlet_adds_one_preview_label_when_the_file_url_is_already_present(monkeypatch):
    _configure_filter_token(monkeypatch)
    public_base = "https://webui.example/ocu"
    file_url = f"{public_base}/files/abc/report%20with%20spaces.html?view=1"
    filter_ = _make_filter()
    _prime_cache(filter_, "abc", public_url=public_base)
    body = {"messages": [{"role": "assistant", "content": f"Open {file_url}"}]}

    first = filter_.outlet(body, __metadata__={"chat_id": "abc"})
    second = filter_.outlet(first, __metadata__={"chat_id": "abc"})
    preview_link = f"[🖥️ Open preview]({file_url})"
    assert second["messages"][0]["content"].count(preview_link) == 1


def test_outlet_leaves_browser_only_and_archive_urls_unchanged(monkeypatch):
    _configure_filter_token(monkeypatch)
    public_base = "https://webui.example/ocu"
    filter_ = _make_filter()
    _prime_cache(filter_, "abc", public_url=public_base)
    browser_only = '<details type="tool_calls" name="playwright"></details>'
    archive_only = f"{public_base}/files/abc/archive"
    body = {
        "messages": [
            {"role": "assistant", "content": browser_only},
            {"role": "assistant", "content": archive_only},
        ]
    }

    result = filter_.outlet(body, __metadata__={"chat_id": "abc"})
    assert [message["content"] for message in result["messages"]] == [
        browser_only,
        archive_only,
    ]


def test_outlet_does_not_match_foreign_public_bases_or_chats(monkeypatch):
    _configure_filter_token(monkeypatch)
    public_base = "https://webui.example/ocu"
    filter_ = _make_filter()
    _prime_cache(filter_, "abc", public_url=public_base)
    foreign_base = "https://webui.example/ocu-else/files/abc/report.html"
    foreign_chat = f"{public_base}/files/abc-other/report.html"
    original = f"{foreign_base} {foreign_chat}"
    result = filter_.outlet(
        {"messages": [{"role": "assistant", "content": original}]},
        __metadata__={"chat_id": "abc"},
    )
    assert result["messages"][0]["content"] == original

class OrchestratorUrlNormalisation(unittest.TestCase):
    """ORCHESTRATOR_URL remains trailing-slash tolerant."""

    def setUp(self):
        self._urlopen_patcher = patch(
            "urllib.request.OpenerDirector.open",
            return_value=_urlopen_mock(
                "System prompt with http://localhost:8081/files/abc baked",
                public_base_url="http://localhost:8081",
            ),
        )
        self._urlopen_patcher.start()
        self.addCleanup(self._urlopen_patcher.stop)

    def test_inlet_does_not_emit_double_slash(self):
        f = _make_filter("http://localhost:8081/")
        body = f.inlet(_active_body(), __metadata__={"chat_id": "abc"})
        self.assertNotIn("//files/", _system_content(body))


class EmptyChatIdHandling(unittest.TestCase):
    """When chat_id is missing, the injected prompt must not reference broken /files/ URLs."""

    def test_inlet_skips_injection_when_chat_id_is_none(self):
        f = _make_filter()
        body = f.inlet(_active_body(), __metadata__={})
        self.assertEqual("", _system_content(body),
                         "System prompt should not be injected when chat_id is missing")

    def test_inlet_skips_injection_when_metadata_missing(self):
        f = _make_filter()
        body = f.inlet(_active_body(), __metadata__=None)
        self.assertEqual("", _system_content(body))


class BaselineBehaviour(unittest.TestCase):
    """Regression guards: normal happy path must keep working."""

    def setUp(self):
        self._urlopen_patcher = patch(
            "urllib.request.OpenerDirector.open",
            return_value=_urlopen_mock("System prompt with http://localhost:8081/files/abc baked"),
        )
        self._urlopen_patcher.start()
        self.addCleanup(self._urlopen_patcher.stop)

    def test_inlet_injects_when_tool_active_and_chat_id_present(self):
        f = _make_filter()
        body = f.inlet(_active_body(), __metadata__={"chat_id": "abc"})
        self.assertIn("http://localhost:8081/files/abc", _system_content(body))

    def test_inlet_no_injection_when_tool_inactive(self):
        f = _make_filter()
        body = f.inlet(
            {"tool_ids": [], "messages": [{"role": "user", "content": "hi"}]},
            __metadata__={"chat_id": "abc"},
        )
        self.assertEqual("", _system_content(body))

    def test_inlet_handles_non_string_system_content(self):
        """Open WebUI multimodal flows can deliver system `content` as a list of
        parts instead of a string. inlet() must not crash on re.search and must
        still inject the Computer Use prompt. Regression for CodeRabbit finding
        on 2026-04-12 (filter.py:220)."""
        f = _make_filter()
        structured_content = [{"type": "text", "text": "hello"}]
        body = {
            "tool_ids": ["ai_computer_use"],
            "messages": [
                {"role": "system", "content": structured_content},
                {"role": "user", "content": "hi"},
            ],
        }
        result = f.inlet(body, __metadata__={"chat_id": "abc"})
        # Did not raise; injection still happened somewhere in the system slot
        system_content = result["messages"][0]["content"]
        self.assertIsInstance(system_content, str)
        self.assertIn("http://localhost:8081/files/abc", system_content)

    def test_outlet_appends_archive_button_once(self):
        f = _make_filter()
        _prime_cache(f, "abc")
        link = "http://localhost:8081/files/abc/report.pdf"
        body = {"messages": [{"role": "assistant", "content": f"see {link}"}]}
        out1 = f.outlet(body, __metadata__={"chat_id": "abc"})
        out2 = f.outlet(out1, __metadata__={"chat_id": "abc"})
        self.assertEqual(
            out1["messages"][0]["content"],
            out2["messages"][0]["content"],
            "Archive button must be idempotent (not duplicated on repeat outlet calls)",
        )

    def test_outlet_does_not_modify_non_assistant_messages(self):
        """User/system/tool messages must be left untouched even if they contain a file URL."""
        f = _make_filter()
        _prime_cache(f, "abc")
        link = "http://localhost:8081/files/abc/report.pdf"
        original_user = f"check {link}"
        original_system = f"context with {link}"
        body = {
            "messages": [
                {"role": "user", "content": original_user},
                {"role": "system", "content": original_system},
                {"role": "tool", "content": f"tool output {link}"},
            ]
        }
        out = f.outlet(body, __metadata__={"chat_id": "abc"})
        self.assertEqual(out["messages"][0]["content"], original_user)
        self.assertEqual(out["messages"][1]["content"], original_system)
        self.assertNotIn("archive", out["messages"][2]["content"].lower())

    def test_outlet_ignores_file_urls_for_other_chat_ids(self):
        """Archive button must NOT be appended when the only file URL belongs to a
        different chat_id (e.g. a multi-user workspace or a quoted prior transcript).
        Regression guard for W-01: outlet previously matched any chat_id via `[^/]+`.
        """
        f = _make_filter()
        _prime_cache(f, "abc")
        other_link = "http://localhost:8081/files/other-chat/report.pdf"
        original_content = f"see artefact from the other chat: {other_link}"
        body = {"messages": [{"role": "assistant", "content": original_content}]}
        out = f.outlet(body, __metadata__={"chat_id": "abc"})
        self.assertEqual(
            out["messages"][0]["content"],
            original_content,
            "Message referencing a file URL for a different chat_id must be left untouched",
        )
        self.assertNotIn("archive", out["messages"][0]["content"].lower())


class SystemPromptFetchCache(unittest.TestCase):
    """HTTP-fetch + LRU cache + stale-cache fallback.

    v4.0.0: _fetch_system_prompt() returns (public_url, prompt) tuple instead of
    just the prompt — public_url comes from the X-Public-Base-URL response header
    so outlet() doesn't need its own URL Valve.
    """

    def test_fresh_fetch_populates_cache(self):
        f = _make_filter()
        with patch(
            "urllib.request.OpenerDirector.open",
            return_value=_urlopen_mock("PROMPT_V1", public_base_url="http://pub:8081"),
        ):
            result = f._fetch_system_prompt("chat-a", "")
        self.assertEqual(result, ("http://pub:8081", "PROMPT_V1"))
        self.assertIn(("chat-a", ""), f._prompt_cache)
        self.assertEqual(f._prompt_cache[("chat-a", "")][1], ("http://pub:8081", "PROMPT_V1"))


    def test_cache_hit_within_ttl_skips_http(self):
        f = _make_filter()
        f._prompt_cache[("chat-a", "")] = (time.time(), ("http://pub:8081", "CACHED"))
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("open must not be called")) as m:
            result = f._fetch_system_prompt("chat-a", "")
        self.assertEqual(result, ("http://pub:8081", "CACHED"))
        m.assert_not_called()

    def test_ttl_expiry_triggers_refetch(self):
        f = _make_filter()
        f._prompt_cache[("chat-a", "")] = (time.time() - 301, ("http://pub:8081", "OLD"))
        with patch("urllib.request.OpenerDirector.open", return_value=_urlopen_mock("FRESH")):
            result = f._fetch_system_prompt("chat-a", "")
        self.assertEqual(result[1], "FRESH")
        self.assertGreater(f._prompt_cache[("chat-a", "")][0], time.time() - 5)

    def test_lru_eviction_at_max_size(self):
        f = _make_filter()
        with patch("urllib.request.OpenerDirector.open", side_effect=lambda *a, **kw: _urlopen_mock("P")):
            for i in range(1, 102):  # 101 distinct chat ids
                f._fetch_system_prompt(f"chat-{i}", "")
        self.assertEqual(len(f._prompt_cache), 100)
        self.assertNotIn(("chat-1", ""), f._prompt_cache)
        self.assertIn(("chat-101", ""), f._prompt_cache)

    def test_stale_cache_fallback_on_server_down(self):
        f = _make_filter()
        f._prompt_cache[("chat-a", "")] = (
            time.time() - 9999,
            ("http://pub:8081", "STALE"),
        )
        with patch("urllib.request.OpenerDirector.open", side_effect=urllib.error.URLError("conn refused")):
            result = f._fetch_system_prompt("chat-a", "")
        self.assertEqual(result, ("http://pub:8081", "STALE"))

    def test_cold_cache_returns_none_when_server_down(self):
        f = _make_filter()
        with patch("urllib.request.OpenerDirector.open", side_effect=urllib.error.URLError("conn refused")):
            result = f._fetch_system_prompt("chat-x", "")
        self.assertIsNone(result, f"Expected None on cold-cache failure, got {result!r}")

    def test_user_email_propagated_to_query_string(self):
        f = _make_filter()
        captured = {}

        def _capture(*args, **_kwargs):
            request = next(
                argument
                for argument in args
                if isinstance(argument, urllib.request.Request)
            )
            captured["url"] = request.full_url
            return _urlopen_mock("P")

        with patch("urllib.request.OpenerDirector.open", side_effect=_capture):
            f._fetch_system_prompt("chat-a", "user@example.com")
        self.assertIn("chat_id=chat-a", captured["url"])
        self.assertIn("user_email=user%40example.com", captured["url"])

    def test_rejects_non_http_scheme_without_urlopen(self):
        """Valves misconfiguration (file://, ftp://, etc.) must not reach urlopen.
        Regression guard for ruff S310: ORCHESTRATOR_URL=file:///etc/passwd would
        otherwise read the file as the injected system prompt.
        """
        f = _make_filter("file:///etc/passwd")
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("must not be called")) as m:
            result = f._fetch_system_prompt("chat-a", "")
        self.assertIsNone(result)
        m.assert_not_called()

    def test_rejects_non_http_scheme_without_stale_cache_fallback(self):
        f = _make_filter("ftp://example.com")
        f._prompt_cache[("chat-a", "")] = (
            time.time() - 9999,
            ("http://pub:8081", "STALE"),
        )
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("must not be called")):
            result = f._fetch_system_prompt("chat-a", "")
        self.assertIsNone(result)

    def test_narrow_exception_propagates_programming_errors(self):
        """A broad `except Exception` used to swallow programming bugs (e.g.
        AttributeError from internal misuse) as silent stale-cache fallbacks.
        The narrowed handler must re-raise non-transport failures."""
        f = _make_filter()
        with patch("urllib.request.OpenerDirector.open", side_effect=AttributeError("boom")):
            with self.assertRaises(AttributeError):
                f._fetch_system_prompt("chat-a", "")

    def test_cache_isolates_different_users_on_same_chat(self):
        """Two users sharing a chat_id must NOT see each other's baked <available_skills>."""
        f = _make_filter()
        prompts = iter(["PROMPT_FOR_ALICE", "PROMPT_FOR_BOB"])

        def _serve_next(*_args, **_kwargs):
            return _urlopen_mock(next(prompts))

        with patch("urllib.request.OpenerDirector.open", side_effect=_serve_next):
            a = f._fetch_system_prompt("chat-shared", "alice@example.com")
            b = f._fetch_system_prompt("chat-shared", "bob@example.com")
        self.assertEqual(a[1], "PROMPT_FOR_ALICE")
        self.assertEqual(b[1], "PROMPT_FOR_BOB")
        self.assertIn(("chat-shared", "alice@example.com"), f._prompt_cache)
        self.assertIn(("chat-shared", "bob@example.com"), f._prompt_cache)
        # No cross-contamination: Alice's cached entry still holds Alice's prompt
        self.assertEqual(
            f._prompt_cache[("chat-shared", "alice@example.com")][1][1],
            "PROMPT_FOR_ALICE",
        )


class PreviewButton(unittest.TestCase):
    """Concrete-file preview-link and archive-link behavior."""

    def _filter(self, public_url: str = "http://localhost:8081") -> "computer_link_filter.Filter":
        f = _make_filter()
        _prime_cache(f, "abc", public_url=public_url)
        return f

    def test_outlet_appends_preview_button_by_default(self):
        f = self._filter()
        body = f.outlet(_assistant_body_with_file(), __metadata__={"chat_id": "abc"})
        content = body["messages"][0]["content"]
        self.assertIn(
            "[🖥️ Open preview](http://localhost:8081/files/abc/report.pdf)", content
        )

    def test_outlet_never_emits_fenced_html_or_iframe(self):
        f = self._filter()
        body = f.outlet(_assistant_body_with_file(), __metadata__={"chat_id": "abc"})
        content = body["messages"][0]["content"]
        self.assertNotIn("<iframe", content)
        self.assertNotIn("```html", content)

    def test_outlet_preview_button_is_idempotent(self):
        f = self._filter()
        body = _assistant_body_with_file()
        out1 = f.outlet(body, __metadata__={"chat_id": "abc"})
        out2 = f.outlet(out1, __metadata__={"chat_id": "abc"})
        self.assertEqual(out1["messages"][0]["content"], out2["messages"][0]["content"])
        self.assertEqual(
            out2["messages"][0]["content"].count("[🖥️ Open preview]"),
            1,
        )

    def test_outlet_preview_mode_off_skips_button(self):
        f = self._filter()
        f.valves.PREVIEW_MODE = "off"
        body = f.outlet(_assistant_body_with_file(), __metadata__={"chat_id": "abc"})
        self.assertNotIn("[🖥️ Open preview]", body["messages"][0]["content"])

    def test_outlet_preview_button_respects_other_chat_ids(self):
        f = self._filter()
        other_link = "http://localhost:8081/files/other-chat/report.pdf"
        original = f"see artefact from another chat: {other_link}"
        body = {"messages": [{"role": "assistant", "content": original}]}
        out = f.outlet(body, __metadata__={"chat_id": "abc"})
        self.assertEqual(out["messages"][0]["content"], original)

    def test_outlet_not_added_to_non_assistant_roles(self):
        f = self._filter()
        link = "http://localhost:8081/files/abc/report.pdf"
        body = {
            "messages": [
                {"role": "user", "content": f"u {link}"},
                {"role": "system", "content": f"s {link}"},
                {"role": "tool", "content": f"t {link}"},
            ]
        }
        out = f.outlet(body, __metadata__={"chat_id": "abc"})
        for msg in out["messages"]:
            self.assertNotIn("[🖥️ Open preview]", msg["content"])
            self.assertNotIn("archive", msg["content"].lower())


    def test_legacy_preview_mode_values_rejected_on_construction(self):
        """v3.x / v4.0.0 values ("artifact", "both") must be rejected when Open WebUI
        instantiates Valves from a saved-DB blob — the Literal type narrowing catches
        operators who haven't re-seeded Valves after the upgrade. Loud error > silent
        no-op.

        Note: Pydantic validates on construction by default, not on attribute
        assignment, so we test the construction path (which is how Open WebUI
        reconstitutes Valves from its stored JSON).
        """
        from pydantic import ValidationError
        ValvesModel = computer_link_filter.Filter.Valves
        for legacy in ("artifact", "both"):
            with self.assertRaises(ValidationError, msg=f"{legacy!r} must be rejected"):
                ValvesModel(PREVIEW_MODE=legacy)  # type: ignore[arg-type]




class OutletCacheRecovery(unittest.TestCase):
    """A cold cache re-fetches the current public base and skips on failure."""

    def test_outlet_skips_all_decoration_when_refetch_fails(self):
        f = _make_filter()
        link = "http://localhost:8081/files/abc/report.pdf"
        original = f"assistant said: {link}"
        body = {"messages": [{"role": "assistant", "content": original}]}
        with patch(
            "urllib.request.OpenerDirector.open",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            out = f.outlet(body, __metadata__={"chat_id": "abc"})
        self.assertEqual(
            out["messages"][0]["content"],
            original,
            "Empty cache must leave the message untouched — no preview or archive",
        )

    def test_outlet_falls_back_to_email_keyed_cache_when_user_missing(self):
        """A re-render without __user__ can use only the current chat's public base."""
        f = _make_filter()
        _prime_cache(f, "abc", user_email="alice@example.com")
        link = "http://localhost:8081/files/abc/report.pdf"
        body = {"messages": [{"role": "assistant", "content": f"see {link}"}]}
        # Note: no __user__ passed
        out = f.outlet(body, __metadata__={"chat_id": "abc"})
        decorated = out["messages"][0]["content"]
        self.assertIn(
            "[🖥️ Open preview](http://localhost:8081/files/abc/report.pdf)", decorated
        )
        self.assertIn(
            "[📦 Download all files as archive](http://localhost:8081/files/abc/archive)",
            decorated,
        )

    def test_outlet_skips_when_cache_has_different_chat_id(self):
        """Cache for chat-X must not decorate a message sent in chat-Y."""
        f = _make_filter()
        _prime_cache(f, "other-chat")
        link = "http://localhost:8081/files/abc/report.pdf"
        original = f"assistant said: {link}"
        body = {"messages": [{"role": "assistant", "content": original}]}
        with patch(
            "urllib.request.OpenerDirector.open",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            out = f.outlet(body, __metadata__={"chat_id": "abc"})
        self.assertEqual(out["messages"][0]["content"], original)


class ValveSchema(unittest.TestCase):
    """Filter owns exactly one URL Valve (ORCHESTRATOR_URL). FILE_SERVER_URL and
    SYSTEM_PROMPT_URL are gone — the public URL is owned by the server and returned
    via the X-Public-Base-URL response header on /system-prompt."""

    def test_only_orchestrator_url_valve_exists(self):
        valve_fields = set(computer_link_filter.Filter.Valves.model_fields.keys())
        self.assertIn("ORCHESTRATOR_URL", valve_fields)
        self.assertNotIn("FILE_SERVER_URL", valve_fields)
        self.assertNotIn("SYSTEM_PROMPT_URL", valve_fields)

    def test_internal_token_is_not_a_valve(self):
        valve_fields = set(computer_link_filter.Filter.Valves.model_fields.keys())
        self.assertNotIn("OCU_INTERNAL_TOKEN", valve_fields)




if __name__ == "__main__":
    unittest.main()
