# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""
title: Computer Use Filter
author: Open Computer Use Contributors
version: 5.0.0
required_open_webui_version: 0.5.17
description: Authenticated HTTP-fetches the Computer Use system prompt from the internal orchestrator, with an LRU cache for transient failures. outlet() decorates assistant messages with a concrete-file preview link and optional archive link.

This filter works in conjunction with Computer Use Tools (computer_use_tools.py).

FUNCTIONALITY:
- inlet(): When tool "ai_computer_use" is active and chat_id is present, fetches the
  fully-baked system prompt from the orchestrator's /system-prompt endpoint using the
  process OCU_INTERNAL_TOKEN as Bearer authentication. It derives user_email only from
  injected __user__, requires the X-Public-Base-URL response header, and caches that
  header with the prompt per chat/user and current credential/orchestrator authority.
  Transient transport failures may use a stale same-authority cache entry; missing
  credentials, authorization failures, redirects, and missing public metadata skip
  injection.
- outlet(): Decorates assistant messages only when they contain a concrete file URL for
  the current chat under the cached public base. PREVIEW_MODE="button" appends one
  markdown link to the first matching file URL; ARCHIVE_BUTTON="on" appends the
  current-chat archive link. Both decorations are idempotent.

Security:
- OCU_INTERNAL_TOKEN is read from the Open WebUI process environment at request time.
  It is not a Valve, browser payload field, result, or log value.
- Redirects are rejected by the filter before a Bearer credential can leave the
  configured orchestrator origin.

    VALVES:
        ORCHESTRATOR_URL (str, default "http://computer-use-server:8081"):
            Internal URL of the Computer Use orchestrator — must be reachable
            from inside the Open WebUI container (server→server fetch for
            /system-prompt). Never appears in browser-facing URLs. The default
            works out of the box with the reference docker-compose stack (both
            services on the same Docker network, service DNS resolves the name).
            For production deploys, point this at the internal hostname / k8s
            service DNS of the orchestrator.
            The browser-facing URL for preview/archive links is owned by the
            server (PUBLIC_BASE_URL env) and returned via the X-Public-Base-URL
            response header on /system-prompt.
        INJECT_SYSTEM_PROMPT (bool, default True):
            If False, inlet() skips system-prompt injection entirely (useful when
            another filter owns the prompt).
        PREVIEW_MODE (Literal["button","off"], default "button"):
            Whether to append a markdown [{PREVIEW_BUTTON_TEXT}]({public}/files/{chat_id}/{file})
            link for the first current-chat concrete file URL. "off" emits no preview
            link.
        ARCHIVE_BUTTON (Literal["on","off"], default "on"):
            Append a markdown [{ARCHIVE_BUTTON_TEXT}]({public}/files/{chat_id}/archive)
            link when a current-chat concrete file URL is present.
        PREVIEW_BUTTON_TEXT (str, default "🖥️ Open preview"):
            Label for the preview-button markdown link.
        ARCHIVE_BUTTON_TEXT (str, default "📦 Download all files as archive"):
            Label for the archive-download markdown link.
"""

import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from typing import Literal, Optional

from pydantic import BaseModel, Field


# All known Open WebUI template variables
# https://docs.openwebui.com/features/workspace/prompts/
OPENWEBUI_TEMPLATE_VARS = [
    "CURRENT_DATE", "CURRENT_DATETIME", "CURRENT_TIME",
    "CURRENT_TIMEZONE", "CURRENT_WEEKDAY",
    "USER_NAME", "USER_LANGUAGE", "USER_LOCATION",
    "CLIPBOARD",
]

# Pattern: {{ VAR }} or {{VAR}} (with/without spaces, Jinja2-style)
TEMPLATE_PATTERN = r"^.*\{\{\s*(?:" + "|".join(OPENWEBUI_TEMPLATE_VARS) + r")\s*\}\}.*$"

# Cache TTL and size (module-level so tests can patch them)
_PROMPT_TTL_SECONDS = 300
_PROMPT_CACHE_MAX_SIZE = 100


def _find_block_start(content: str, pos: int) -> int:
    """
    Find the start of the text block containing position `pos`.

    A block is delimited by an empty line (double newline) or the start of content.
    Used to inject the Computer Use system prompt BEFORE any Open WebUI template block.
    """
    boundary = content.rfind("\n\n", 0, pos)
    return boundary + 2 if boundary != -1 else 0


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects before authenticated headers can leave the configured origin."""

    def redirect_request(self, request, response, code, message, headers, _new_url):
        raise urllib.error.HTTPError(
            request.full_url, code, "redirect blocked", headers, response
        )


_URL_RE = re.compile(r"https?://[^\s<>\]\)\"']+")


def _is_http_safe_credential(value: str) -> bool:
    return bool(value) and all(0x21 <= ord(character) <= 0x7E for character in value)


def _first_current_chat_file_url(
    content: str, public_url: str, chat_id: str
) -> Optional[str]:
    """Return the first concrete current-chat file URL without normalizing it."""
    try:
        public_parts = urllib.parse.urlsplit(public_url)
    except ValueError:
        return None
    if public_parts.scheme not in ("http", "https") or not public_parts.netloc:
        return None

    base_path = public_parts.path
    file_prefix = f"{base_path}/files/{chat_id}/"
    archive_path = f"{base_path}/files/{chat_id}/archive"
    for match in _URL_RE.finditer(content):
        candidate = match.group(0)
        try:
            parts = urllib.parse.urlsplit(candidate)
        except ValueError:
            continue
        if (
            parts.scheme == public_parts.scheme
            and parts.netloc == public_parts.netloc
            and parts.path.startswith(file_prefix)
            and parts.path != archive_path
            and parts.path[len(file_prefix):]
        ):
            return candidate
    return None


class Filter:
    class Valves(BaseModel):
        ORCHESTRATOR_URL: str = Field(
            default="http://computer-use-server:8081",
            description="Internal URL of the Computer Use orchestrator. Must be reachable from inside the Open WebUI container for server→server /system-prompt fetch. NOT browser-facing — the public URL is owned by the server (PUBLIC_BASE_URL env) and returned via the X-Public-Base-URL response header. Trailing slash is tolerated.",
        )
        INJECT_SYSTEM_PROMPT: bool = Field(
            default=True,
            description="Inject Computer Use system prompt when tools are active. Turn off only if another filter owns the prompt.",
        )
        PREVIEW_MODE: Literal["button", "off"] = Field(
            default="button",
            description="Where the preview link appears on assistant messages. button=markdown link to the first concrete current-chat file. off=no preview link.",
        )
        ARCHIVE_BUTTON: Literal["on", "off"] = Field(
            default="on",
            description="Append a 'Download all files as archive' link when a concrete current-chat file is present.",
        )
        PREVIEW_BUTTON_TEXT: str = Field(
            default="🖥️ Open preview",
            description="Text for the concrete-file preview markdown link.",
        )
        ARCHIVE_BUTTON_TEXT: str = Field(
            default="📦 Download all files as archive",
            description="Text for the archive-download markdown link.",
        )

    def __init__(self):
        self.valves = self.Valves()
        self._no_redirect_opener = urllib.request.build_opener(_NoRedirectHandler())
        # Per-(chat, user) LRU cache: (chat_id, user_email) ->
        # (fetched_at, (public_url, prompt)). Its authority records the normalized
        # internal origin and current process credential that produced every entry.
        self._prompt_cache: OrderedDict[
            tuple[str, str], tuple[float, tuple[str, str]]
        ] = OrderedDict()
        self._cache_authority: Optional[tuple[str, str]] = None

    def _configured_authority(self) -> Optional[tuple[str, str]]:
        token = os.environ.get("OCU_INTERNAL_TOKEN", "")
        if not _is_http_safe_credential(token):
            self._prompt_cache.clear()
            self._cache_authority = None
            print("[ComputerUseFilter] OCU_INTERNAL_TOKEN is unavailable; skipping system prompt")
            return None

        # ORCHESTRATOR_URL remains trailing-slash tolerant because it is an
        # internal endpoint setting, unlike the server-owned public base.
        orchestrator = self.valves.ORCHESTRATOR_URL.rstrip("/")
        parsed = urllib.parse.urlparse(orchestrator)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            self._prompt_cache.clear()
            self._cache_authority = None
            print(
                f"[ComputerUseFilter] Unsupported orchestrator URL scheme: "
                f"{parsed.scheme!r} (expected http/https)"
            )
            return None

        authority = (orchestrator, token)
        if self._cache_authority is not None and self._cache_authority != authority:
            self._prompt_cache.clear()
        self._cache_authority = authority
        return authority

    def _fetch_system_prompt(
        self, chat_id: str, user_email: str = ""
    ) -> Optional[tuple[str, str]]:
        """Fetch an authorized prompt, retaining stale data only for transport failures."""
        authority = self._configured_authority()
        if authority is None:
            return None
        orchestrator, token = authority
        now = time.time()
        cache_key = (chat_id, user_email)
        cached = self._prompt_cache.get(cache_key)

        if cached and (now - cached[0]) < _PROMPT_TTL_SECONDS:
            self._prompt_cache.move_to_end(cache_key)
            return cached[1]

        params = {}
        if chat_id:
            params["chat_id"] = chat_id
        if user_email:
            params["user_email"] = user_email
        url = orchestrator + "/system-prompt"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("Accept", "text/plain")
            req.add_header("Authorization", f"Bearer {token}")
            with self._no_redirect_opener.open(req, timeout=10) as resp:  # noqa: S310
                prompt = resp.read().decode("utf-8")
                public_url = resp.headers.get("X-Public-Base-URL")
            if not public_url:
                self._prompt_cache.clear()
                print(
                    "[ComputerUseFilter] System prompt response omitted "
                    "X-Public-Base-URL"
                )
                return None

            entry = (public_url, prompt)
            self._prompt_cache[cache_key] = (now, entry)
            self._prompt_cache.move_to_end(cache_key)
            while len(self._prompt_cache) > _PROMPT_CACHE_MAX_SIZE:
                self._prompt_cache.popitem(last=False)
            return entry

        except urllib.error.HTTPError as error:
            if error.code in (401, 403) or 300 <= error.code < 400:
                self._prompt_cache.clear()
                print(
                    f"[ComputerUseFilter] System prompt request rejected "
                    f"(HTTP {error.code})"
                )
                return None
            print(
                f"[ComputerUseFilter] Failed to fetch system prompt: "
                f"HTTP {error.code}"
            )
        except (urllib.error.URLError, TimeoutError, UnicodeDecodeError) as error:
            print(
                f"[ComputerUseFilter] Failed to fetch system prompt: "
                f"{type(error).__name__}"
            )

        return cached[1] if cached else None

    def inlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __metadata__: Optional[dict] = None,
    ) -> dict:
        """Inject Computer Use system prompt BEFORE LLM processing."""
        if not self.valves.INJECT_SYSTEM_PROMPT:
            return body

        tool_ids = body.get("tool_ids", [])
        if "ai_computer_use" not in tool_ids:
            return body

        chat_id = __metadata__.get("chat_id") if __metadata__ else None
        if not chat_id:
            return body

        user_email = __user__.get("email", "") if __user__ else ""

        fetched = self._fetch_system_prompt(chat_id, user_email)
        if not fetched:
            # Unavailable, unauthorized, or incomplete prompt metadata leaves the body unchanged.
            return body
        _public_url, system_prompt = fetched

        messages = body.get("messages", [])
        if not messages:
            return body

        # Locate existing system message
        system_msg_idx = None
        for idx, msg in enumerate(messages):
            if msg.get("role") == "system":
                system_msg_idx = idx
                break

        if system_msg_idx is not None:
            existing_content = messages[system_msg_idx].get("content", "")
            # Some Open WebUI flows deliver structured content (e.g. a list of
            # multimodal parts) instead of a plain string. re.search would
            # crash in that case — skip template detection and fall through to
            # the append branch, which handles arbitrary existing_content via
            # string concatenation (str() coercion there is safe for the
            # downstream LLM which only reads strings anyway).
            if isinstance(existing_content, str):
                match = re.search(TEMPLATE_PATTERN, existing_content, re.MULTILINE)
            else:
                match = None
            if match:
                # Inject BEFORE the block containing the template variable
                block_start = _find_block_start(existing_content, match.start())
                messages[system_msg_idx]["content"] = (
                    existing_content[:block_start].rstrip()
                    + "\n\n"
                    + system_prompt
                    + "\n\n"
                    + existing_content[block_start:]
                )
            else:
                # No template vars (or non-string content) -> append as plain string.
                # When existing_content is a list of multimodal parts, coerce to
                # str() first so the LLM still sees the Computer Use prompt; the
                # original structured content is preserved via the repr.
                if isinstance(existing_content, str):
                    messages[system_msg_idx]["content"] = existing_content + "\n\n" + system_prompt
                else:
                    messages[system_msg_idx]["content"] = str(existing_content) + "\n\n" + system_prompt
        else:
            messages.insert(0, {"role": "system", "content": system_prompt})

        body["messages"] = messages
        return body

    def outlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __metadata__: Optional[dict] = None,
    ) -> dict:
        """Append concrete-file preview and archive links to assistant messages."""
        wants_button = self.valves.PREVIEW_MODE == "button"
        wants_archive = self.valves.ARCHIVE_BUTTON == "on"
        if not (wants_button or wants_archive):
            return body

        chat_id = __metadata__.get("chat_id") if __metadata__ else None
        if not chat_id or self._configured_authority() is None:
            return body

        user_email = __user__.get("email", "") if __user__ else ""
        cached = self._prompt_cache.get((chat_id, user_email)) or self._prompt_cache.get(
            (chat_id, "")
        )
        if not cached:
            for (cached_chat_id, _), entry in self._prompt_cache.items():
                if cached_chat_id == chat_id:
                    cached = entry
                    break
        if not cached:
            cached_pair = self._fetch_system_prompt(chat_id, user_email)
            if not cached_pair:
                return body
            public_url, _prompt = cached_pair
        else:
            public_url, _prompt = cached[1]

        archive_url = f"{public_url}/files/{chat_id}/archive"
        for message in body.get("messages", []):
            if message.get("role") != "assistant":
                continue
            content = message.get("content")
            if not content or not isinstance(content, str):
                continue

            file_url = _first_current_chat_file_url(content, public_url, chat_id)
            if not file_url:
                continue

            links: list[str] = []
            preview_link = f"[{self.valves.PREVIEW_BUTTON_TEXT}]({file_url})"
            if wants_button and preview_link not in content:
                links.append(preview_link)
            if wants_archive and archive_url not in content:
                links.append(f"[{self.valves.ARCHIVE_BUTTON_TEXT}]({archive_url})")
            if links:
                message["content"] = content + "\n\n" + "\n".join(links)

        return body
