# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""
title: Computer Use Tools
author: OpenWebUI Implementation
version: 4.3.0

Thin MCP client proxy to computer-use-orchestrator. All tool configuration is server-side.
ORCHESTRATOR_URL and optional MCP_API_KEY are Valves; OCU_INTERNAL_TOKEN is read from the process environment per call.

Container naming: owui-chat-{chat_id}

REQUIRED SETUP:
- Tool ID MUST be "ai_computer_use" for system prompt injection to work
- Companion filter "Computer Use Filter" (computer_link_filter.py) must be installed and enabled
"""

import asyncio
import json
import os
import re
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import timedelta
from typing import Callable, Awaitable, Optional, List, Annotated
from pydantic import BaseModel, Field


# Client HTTP timeouts (server controls actual command timeout)
CLIENT_HTTP_TIMEOUT = 660       # 11 min > server's 600s COMMAND_TIMEOUT
SUB_AGENT_CLIENT_TIMEOUT = 3660 # 61 min > server's 3600s SUB_AGENT_TIMEOUT


# Every error string the wrapper produces starts with one of these prefixes.
# The outer per-tool wrappers use _looks_like_error() to decide whether to
# emit status="error" — a single source of truth so a new error class added
# below doesn't silently leave the UI green.
_ERROR_PREFIXES = (
    "[CONFIG ERROR]",
    "[NETWORK ERROR]",
    "[MCP TRANSPORT ERROR]",
    "[UNEXPECTED ERROR]",
    "[TOOL ERROR]",
    "[Timeout",
    "[Error",
    "Error:",
)


def _looks_like_error(s: str) -> bool:
    if not isinstance(s, str):
        return False
    return any(s.startswith(p) for p in _ERROR_PREFIXES)


# The image extensions mcp_tools.view resizes and returns as an image.
_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp")


def _is_image_data_uri(result: object) -> bool:
    """Open WebUI routes a tool result starting with data:image/ to the model as an image."""
    return isinstance(result, str) and result.startswith("data:image/")


def _model_cannot_view_images(metadata: object) -> bool:
    """True only when the chat model's metadata says vision is False; missing means capable.

    The image goes to the chat model, metadata["model"]; Open WebUI injects the task
    model as __model__, so __model__ is not consulted.
    """
    model = metadata.get("model") if isinstance(metadata, dict) else None
    try:
        return model["info"]["meta"]["capabilities"]["vision"] is False
    except (KeyError, TypeError):
        return False

def _is_http_safe_credential(value: object) -> bool:
    """Match the service guard's credential format without exposing a value."""
    return (
        isinstance(value, str)
        and bool(value)
        and all(0x21 <= ord(character) <= 0x7E for character in value)
    )


def _is_transmittable_mcp_api_key(value: object) -> bool:
    """Allow header-safe MCP keys without imposing the internal-token policy."""
    return (
        isinstance(value, str)
        and bool(value)
        and all(character == "\t" or 0x20 <= ord(character) <= 0x7E for character in value)
    )


async def _gateway_headers(request, metadata: dict, user) -> dict:
    """sub_agent-only Claude Code gateway inputs (#662), each sent only when header-safe.

    X-OCU-Gateway-Token is the token that authenticated this chat request, sent only
    when the caller owns the chat: an admin may run completions in another user's chat,
    and that admin's token must not reach the owner's sandbox. X-Chat-Model is the chat
    request's model (metadata["model"]); Open WebUI injects __model__ as the task model.
    """
    headers = {}
    token = getattr(getattr(getattr(request, "state", None), "token", None), "credentials", None)
    if _is_http_safe_credential(token) and await _caller_owns_chat(metadata.get("chat_id"), user):
        headers["X-OCU-Gateway-Token"] = token
    model = metadata.get("model")
    model_id = model.get("id") if isinstance(model, dict) else model
    if _is_http_safe_credential(model_id):
        headers["X-Chat-Model"] = model_id
    return headers


async def _caller_owns_chat(chat_id, user) -> bool:
    try:
        from open_webui.models.chats import Chats
        return bool(await Chats.is_chat_owner(chat_id, user["id"]))
    except Exception:
        # Open WebUI absent, no user, or a failed lookup: ownership is unproven, so the
        # token is withheld and OCU refuses the gateway call with its missing-token error.
        return False


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects before authenticated probe headers leave the configured origin."""

    def redirect_request(self, request, response, code, message, headers, _new_url):
        raise urllib.error.HTTPError(
            request.full_url, code, "redirect blocked", headers, response
        )


# ============================================================================
# MCP Streamable HTTP Client
# ============================================================================

class _MCPClient:
    """MCP Streamable HTTP client for computer-use-orchestrator."""

    # Health-check cache TTL. Long enough that a busy chat doesn't pay a
    # GET /health on every tool call; short enough that a freshly-restarted
    # orchestrator is detected within ~30s.
    _HEALTH_TTL_SECONDS = 30.0
    _HEALTH_TIMEOUT_SECONDS = 3.0

    def __init__(
        self, orchestrator_url: str, mcp_api_key: str = "", internal_token: str = ""
    ):
        base = orchestrator_url.rstrip("/")
        self.base_url = base
        self.mcp_url = f"{base}/mcp"
        self.health_url = f"{base}/health"
        self.api_key = mcp_api_key
        self.internal_token = internal_token
        self._no_redirect_opener = urllib.request.build_opener(
            _NoRedirectHandler()
        )
        # (checked_at, ok, err_str) — None on cold start.
        self._last_health: Optional[tuple] = None

    def _check_health_sync(self) -> tuple[bool, str]:
        """Blocking probe of BOTH /health AND /mcp. Returns (ok, err_string).

        We hit /mcp too because the failure mode that bit us in production was
        exactly: /health returns 200 (FastAPI is up), but /mcp returns 500
        ("Task group is not initialized") because the lifespan swallowed an
        ImportError and never entered session_manager.run(). A /health-only
        probe would have called everything green and let the cancel-scope
        crash propagate as silent empty output.

        Cached for _HEALTH_TTL_SECONDS so the AI doesn't pay two round-trips
        on every tool call when the server is healthy. Cache stores the
        failure verdict too, so a known-bad server short-circuits."""
        now = time.monotonic()
        if self._last_health is not None:
            checked_at, ok, err = self._last_health
            if (now - checked_at) < self._HEALTH_TTL_SECONDS:
                return ok, err

        # 1) GET /health — fastest fail for "container down / wrong URL".
        ok, err = self._http_probe_get(self.health_url)
        if not ok:
            self._last_health = (now, False, f"GET /health -> {err}")
            return False, self._last_health[2]

        # 2) POST /mcp initialize — catches "FastAPI up but MCP broken".
        ok, err = self._http_probe_mcp_initialize()
        if not ok:
            self._last_health = (now, False, f"POST /mcp -> {err}")
            return False, self._last_health[2]

        self._last_health = (now, True, "")
        return True, ""

    def _http_probe_get(self, url: str) -> tuple[bool, str]:
        """GET probe with the standard error-message normalization."""
        try:
            req = urllib.request.Request(
                url,
                method="GET",
                headers={"Authorization": f"Bearer {self.internal_token}"},
            )
            with self._no_redirect_opener.open(
                req, timeout=self._HEALTH_TIMEOUT_SECONDS
            ) as resp:
                if 200 <= resp.status < 300:
                    return True, ""
                return False, f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400:
                return False, f"redirect blocked (HTTP {e.code})"
            return False, f"HTTP {e.code}: {e.reason}"
        except urllib.error.URLError as e:
            return False, f"{type(e).__name__}: {getattr(e, 'reason', e)}"
        except (TimeoutError, OSError) as e:
            return False, f"{type(e).__name__}: {e}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def _http_probe_mcp_initialize(self) -> tuple[bool, str]:
        """POST /mcp with a minimal initialize. Verifies session_manager is live."""
        body = (
            b'{"jsonrpc":"2.0","id":1,"method":"initialize",'
            b'"params":{"protocolVersion":"2024-11-05","capabilities":{},'
            b'"clientInfo":{"name":"preflight","version":"1.0"}}}'
        )
        req = urllib.request.Request(
            self.mcp_url,
            method="POST",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "X-Chat-Id": "preflight",
                "X-OCU-Internal-Token": self.internal_token,
            },
        )
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with self._no_redirect_opener.open(
                req, timeout=self._HEALTH_TIMEOUT_SECONDS
            ) as resp:
                if 200 <= resp.status < 300:
                    return True, ""
                return False, f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400:
                return False, f"redirect blocked (HTTP {e.code})"
            return False, f"HTTP {e.code}: {e.reason}"
        except urllib.error.URLError as e:
            return False, f"{type(e).__name__}: {getattr(e, 'reason', e)}"
        except (TimeoutError, OSError) as e:
            return False, f"{type(e).__name__}: {e}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def _redirect_error_message(self, detail: str) -> str:
        return (
            "[CONFIG ERROR] Computer Use rejected an HTTP redirect.\n"
            f"  Pre-flight: {detail}\n"
            "  ORCHESTRATOR_URL must be a direct internal endpoint; "
            "authenticated redirects are not followed."
        )

    def _authentication_error_message(self, detail: str) -> str:
        return (
            "[CONFIG ERROR] Computer Use authorization pre-flight was rejected.\n"
            f"  Pre-flight: {detail}\n"
            "  Verify OCU_INTERNAL_TOKEN matches the Computer Use server and "
            "MCP_API_KEY matches when that optional credential is configured."
        )

    def _config_error_message(self, err: str) -> str:
        """Build the [CONFIG ERROR] string the AI receives when /health fails.

        The message is written to be useful to BOTH the end-user (who reads
        chat) and to a downstream AI model that sees the tool result. It
        names the URL it tried, the underlying error, the most likely fix,
        and how to verify."""
        if "redirect blocked" in err:
            return self._redirect_error_message(err)
        if err.startswith((
            "POST /mcp -> HTTP 401",
            "POST /mcp -> HTTP 403",
        )):
            return self._authentication_error_message(err)

        return (
            f"[CONFIG ERROR] Cannot use computer-use-server at {self.base_url}.\n"
            f"  Pre-flight: {err}\n"
            f"  Likely causes:\n"
            f"    1. The orchestrator container is not running:\n"
            f"         docker compose -f docker-compose.yml up -d computer-use-server\n"
            f"    2. /health is up but /mcp returns 500 — lifespan failed to start the\n"
            f"       MCP session manager. Check `docker logs computer-use-server` for ImportError.\n"
            f"  Verify after fix:\n"
            f"    curl -fsS {self.health_url}     # should return {{\"status\":\"healthy\"}}\n"
            f"    ./tests/test-mcp-endpoint-live.sh {self.base_url}\n"
            f"  Tool ORCHESTRATOR_URL Valve currently points at: {self.base_url}\n"
            f"  This URL must be reachable from inside the open-webui container.\n"
            f"  Cached for {int(self._HEALTH_TTL_SECONDS)}s; a server restart will be picked up automatically."
        )

    def build_headers(
        self,
        chat_id: str,
        user_email: str = "",
        user_name: str = "",
    ) -> dict:
        """Build HTTP headers — only per-request user context."""
        headers = {
            "X-Chat-Id": chat_id,
            "X-OCU-Internal-Token": self.internal_token,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if user_email:
            headers["X-User-Email"] = user_email
        if user_name:
            headers["X-User-Name"] = urllib.parse.quote(user_name, safe="")
        return headers

    def _create_session(self, headers: dict, timeout: int):
        """Create MCP client session context manager."""
        from mcp.client.streamable_http import streamablehttp_client
        from mcp import ClientSession

        class _SessionContext:
            def __init__(self, url, headers, timeout):
                self.url = url
                self.headers = headers
                self.timeout = timeout
                self.redirect_status = None
                self._transport_cm = None
                self._session_cm = None
                self._session = None

            async def _record_response(self, response):
                if 300 <= response.status_code < 400:
                    self.redirect_status = response.status_code

            def _httpx_client_factory(self, headers=None, timeout=None, auth=None):
                import httpx
                from mcp.shared._httpx_utils import (
                    MCP_DEFAULT_SSE_READ_TIMEOUT,
                    MCP_DEFAULT_TIMEOUT,
                )

                if timeout is None:
                    timeout = httpx.Timeout(
                        MCP_DEFAULT_TIMEOUT,
                        read=MCP_DEFAULT_SSE_READ_TIMEOUT,
                    )
                client_kwargs = {
                    "follow_redirects": False,
                    "timeout": timeout,
                    "event_hooks": {"response": [self._record_response]},
                }
                if headers is not None:
                    client_kwargs["headers"] = headers
                if auth is not None:
                    client_kwargs["auth"] = auth
                return httpx.AsyncClient(**client_kwargs)

            async def __aenter__(self):
                self._transport_cm = streamablehttp_client(
                    self.url,
                    headers=self.headers,
                    sse_read_timeout=timedelta(seconds=self.timeout + 60),
                    httpx_client_factory=self._httpx_client_factory,
                )
                read, write, _ = await self._transport_cm.__aenter__()
                self._session_cm = ClientSession(read, write)
                self._session = await self._session_cm.__aenter__()
                await self._session.initialize()
                return self._session

            async def __aexit__(self, *args):
                if self._session_cm:
                    await self._session_cm.__aexit__(*args)
                if self._transport_cm:
                    await self._transport_cm.__aexit__(*args)

        return _SessionContext(self.mcp_url, headers, timeout)

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict,
        headers: dict,
        timeout: int,
        event_emitter: Callable = None,
        operation_name: str = "",
        image: bool = False,
    ) -> str:
        """Call MCP tool via Streamable HTTP with SSE progress.

        With image=True an MCP image block in a successful result is returned as a
        data:<mime>;base64,<data> string instead of the text.

        Failure-mode contract: on ANY failure path this method returns a
        string with one of the _ERROR_PREFIXES. Outer wrappers detect that
        and emit status="error" so the chat UI shows red. We never return
        empty / ambiguous results that the AI could mistake for success.
        """
        async def _emit_error(description: str):
            if event_emitter:
                try:
                    await event_emitter({"type": "status", "data": {
                        "description": description,
                        "status": "error",
                        "done": True,
                    }})
                except Exception:
                    pass

        # Pre-flight reachability check. Without this, a missing server
        # (DNS fail / connection refused) lands us inside the MCP SDK,
        # which raises a confusing `RuntimeError: Attempted to exit cancel
        # scope in a different task` from __aexit__ — the actual failure
        # gets buried and the wrapper returns either "[No output]" or a
        # cryptic string. Pre-flight returns a clear actionable message
        # before any of that machinery runs.
        ok, health_err = await asyncio.to_thread(self._check_health_sync)
        if not ok:
            await _emit_error("Computer Use server unreachable")
            return self._config_error_message(health_err)

        async def on_progress(progress, total, message):
            if event_emitter:
                display_msg = message or f"{tool_name}: working..."
                try:
                    await event_emitter({
                        "type": "status",
                        "data": {
                            "description": display_msg,
                            "status": "in_progress",
                            "done": False,
                        }
                    })
                except Exception:
                    pass

        session_context = self._create_session(headers, timeout)

        async def _execute():
            async with session_context as session:
                result = await session.call_tool(
                    tool_name, arguments,
                    progress_callback=on_progress,
                    read_timeout_seconds=timedelta(seconds=timeout + 30),
                )
                return self._extract_text(result, image=image)

        try:
            try:
                return await asyncio.wait_for(_execute(), timeout=timeout + 60)
            except asyncio.CancelledError:
                if session_context.redirect_status is None:
                    raise
            except Exception:
                if session_context.redirect_status is None:
                    raise
            self._last_health = None
            await _emit_error("Computer Use endpoint redirected request")
            return self._redirect_error_message(
                f"HTTP {session_context.redirect_status}"
            )
        except asyncio.TimeoutError:
            await _emit_error(f"Timeout after {timeout}s")
            return (
                f"[Timeout after {timeout}s] Operation did not complete within the client timeout. "
                f"Check `docker logs computer-use-server` to see whether the orchestrator is still working."
            )
        except (ConnectionError, OSError) as e:
            # Invalidate the health cache so the next call re-probes
            # immediately rather than waiting out the TTL.
            self._last_health = None
            await _emit_error("Network error reaching orchestrator")
            return (
                f"[NETWORK ERROR] {type(e).__name__}: {e}\n"
                f"  The orchestrator at {self.base_url} accepted the health check but the MCP "
                f"call dropped its connection. It may have just crashed or been restarted.\n"
                f"  Check: docker logs computer-use-server"
            )
        except RuntimeError as e:
            # The mcp SDK raises RuntimeError("Attempted to exit cancel scope ...")
            # when the streamable-HTTP transport collapses mid-call. Surface it
            # as a transport-layer issue, not a generic crash, so the AI knows
            # the issue is the connection rather than the tool's logic.
            self._last_health = None
            await _emit_error("MCP transport error")
            return (
                f"[MCP TRANSPORT ERROR] {e}\n"
                f"  The MCP session was killed mid-call. Likely causes: orchestrator "
                f"crashed, container restarted, or the MCP SDK version on the server "
                f"and client are incompatible.\n"
                f"  Check: docker logs computer-use-server"
            )
        except Exception as e:
            import traceback
            await _emit_error(f"Unexpected error: {type(e).__name__}")
            tb = traceback.format_exc()
            return (
                f"[UNEXPECTED ERROR] {type(e).__name__}: {e}\n"
                f"  This was not classified as a known failure mode. Server-side "
                f"traceback (truncated to 2000 chars; full trace in open-webui logs):\n"
                f"{tb[:2000]}"
            )

    @staticmethod
    def _extract_text(result, image: bool = False) -> str:
        """Extract text from MCP tool result, or with image=True its first image block.

        Distinguishes three cases the old version conflated under "[No output]":
          1. result is None or missing — session died before producing one.
          2. result.isError is True — server-side tool raised.
          3. content is empty — legitimate empty stdout/stderr from a
             successful command (e.g. `true`, `mkdir -p existing-dir`).

        The phrasing of case 3 is deliberate: an AI reading "[No output]"
        often concludes the tool is broken. "[Command produced no output.
        Exit was successful — this is not an error.]" blocks that misread.
        """
        if result is None:
            return (
                "[Error] MCP returned no result object — the session may have "
                "died between request and response. Retry the call; if it "
                "happens again, check `docker logs computer-use-server`."
            )

        is_error = bool(getattr(result, "isError", False))

        content = getattr(result, "content", None)
        if not content:
            if is_error:
                return (
                    "[TOOL ERROR] Server-side tool raised an exception with no "
                    "message. Check `docker logs computer-use-server` for the "
                    "traceback."
                )
            return (
                "[Command produced no output. Exit was successful — this is "
                "not an error. If you expected output, the command may have "
                "written to a file instead of stdout.]"
            )

        if image and not is_error:
            for item in content:
                mime_type = getattr(item, "mimeType", None)
                data = getattr(item, "data", None)
                if (getattr(item, "type", None) == "image"
                        and isinstance(mime_type, str) and mime_type.startswith("image/")
                        and isinstance(data, str) and data):
                    return f"data:{mime_type};base64,{data}"

        parts = []
        for item in content:
            if hasattr(item, "text"):
                parts.append(item.text)

        if not parts:
            return (
                "[Empty content blocks — the server returned content but no "
                "text fields. This usually means a binary payload that this "
                "client cannot render, or an SDK shape change.]"
            )

        joined = "\n".join(parts)
        if is_error:
            # Prepend the prefix so outer wrappers + UI flag this as error
            # even when the server-side tool produced text alongside the
            # exception.
            return f"[TOOL ERROR] {joined}"
        return joined


def _get_user_mcp_server_names(request, user_id: str = "") -> list:
    """Extract MCP server names available to the user from OpenWebUI config.

    Reads request.app.state.config.TOOL_SERVER_CONNECTIONS, filters by type=="mcp"
    and user access_control, returns server names (last URL path segment).
    """
    if not request or not hasattr(request, "app"):
        return []
    try:
        connections = request.app.state.config.TOOL_SERVER_CONNECTIONS
    except Exception:
        return []

    names = []
    for server in connections:
        if server.get("type") != "mcp":
            continue

        # Access control check: if access_control is set, user must be in read list
        ac = server.get("access_control", {})
        if ac:
            read_group = ac.get("read", {})
            user_ids = read_group.get("user_ids", [])
            group_ids = read_group.get("group_ids", [])
            if user_ids or group_ids:
                if user_id and user_id not in user_ids:
                    continue

        url = server.get("url", "")
        if not url:
            continue
        # Extract server name from URL: https://api.example.com/mcp/confluence → confluence
        name = url.rstrip("/").rsplit("/", 1)[-1]
        if name and name != "mcp":
            names.append(name)

    return names


# Custom type for view_range
ViewRange = Annotated[
    Optional[List[int]],
    Field(
        default=None,
        min_length=2,
        max_length=2,
        description="Optional line range for text files. Format: [start_line, end_line] where lines are indexed starting at 1. Use [start_line, -1] to view from start_line to the end of the file. When not provided, the entire file is displayed, truncating from the middle if it exceeds 16,000 characters (showing beginning and end)."
    )
]


class Tools:
    class Valves(BaseModel):
        ORCHESTRATOR_URL: str = Field(
            default="http://computer-use-server:8081",
            description="Internal URL of the Computer Use orchestrator (MCP endpoint + file uploads). Must be reachable from inside the Open WebUI container."
        )
        MCP_API_KEY: str = Field(
            default="",
            description="Bearer token for computer-use-orchestrator /mcp endpoint authentication"
        )
        DEBUG_LOGGING: bool = Field(
            default=False,
            description="Enable verbose debug logging"
        )

    def __init__(self):
        self.valves = self.Valves()
        self.file_handler = True
        self.citation = True
        self._mcp_client = None
        # Track the URL, current environment credential, and MCP Valve credential
        # used to construct the current client. A configuration change takes effect
        # on the next tool call without a restart.
        self._mcp_client_config: tuple[str, str, str] | None = None

    @property
    def mcp_client(self) -> _MCPClient:
        """Lazy MCP client — recreated when configuration changes."""
        url = self.valves.ORCHESTRATOR_URL
        internal_token = os.environ.get("OCU_INTERNAL_TOKEN", "")
        config = (url, internal_token, self.valves.MCP_API_KEY)
        if self._mcp_client is None or self._mcp_client_config != config:
            self._mcp_client = _MCPClient(
                url, self.valves.MCP_API_KEY, internal_token
            )
            self._mcp_client_config = config
            print(f"[MCP] Client initialized: {self._mcp_client.mcp_url}")
        return self._mcp_client

    # =========================================================================
    # Helpers
    # =========================================================================

    def _configuration_error(self) -> Optional[str]:
        internal_token = os.environ.get("OCU_INTERNAL_TOKEN", "")
        if not _is_http_safe_credential(internal_token):
            return (
                "[CONFIG ERROR] OCU_INTERNAL_TOKEN must be a non-empty "
                "HTTP-safe server credential."
            )
        api_key = self.valves.MCP_API_KEY
        if api_key not in (None, "") and not _is_transmittable_mcp_api_key(api_key):
            return (
                "[CONFIG ERROR] MCP_API_KEY must be empty or safely "
                "transmittable as an HTTP header."
            )
        return None

    def _call_error(self, chat_id: object) -> Optional[str]:
        if not isinstance(chat_id, str) or not chat_id.strip():
            return "[TOOL ERROR] chat_id is required."
        return self._configuration_error()

    async def _emit_status(self, emitter, description: str, status: str, done: bool) -> None:
        if not emitter:
            return
        try:
            await emitter({"type": "status", "data": {
                "description": description, "status": status, "done": done,
            }})
        except Exception:
            pass

    async def _emit_terminal_error(self, emitter, description: str) -> None:
        await self._emit_status(emitter, description, "error", True)

    async def _emit_workspace_hint(self, emitter, chat_id: str) -> None:
        if not emitter:
            return
        try:
            await emitter({
                "type": "ocu:workspace_changed",
                "data": {"chat_id": chat_id, "reason": "tool_completed"},
            })
        except Exception:
            pass

    async def _prepare_tool_call(
        self, metadata: Optional[dict], emitter
    ) -> tuple[Optional[str], Optional[str]]:
        chat_id = metadata.get("chat_id") if isinstance(metadata, dict) else None
        error = self._call_error(chat_id)
        if error:
            description = (
                "Chat ID is required"
                if error.startswith("[TOOL ERROR]")
                else "Computer Use configuration error"
            )
            await self._emit_terminal_error(emitter, description)
            return None, error
        return chat_id, None

    def _build_mcp_headers(self, chat_id: str, __user__: dict = None, request=None) -> dict:
        """Build per-request MCP headers from injected server user context."""
        user_email = __user__.get("email", "") if isinstance(__user__, dict) else ""
        user_name = __user__.get("name", "") if isinstance(__user__, dict) else ""
        headers = self.mcp_client.build_headers(
            chat_id=chat_id,
            user_email=user_email,
            user_name=user_name,
        )
        if request:
            try:
                user_id = __user__.get("id", "") if __user__ else ""
                names = _get_user_mcp_server_names(request, user_id)
                if names:
                    headers["X-Mcp-Servers"] = ",".join(names)
            except Exception:
                pass
        return headers

    async def _sync_files_if_needed(
        self, chat_id: str, __files__: list = None, __user__: dict = None
    ) -> str:
        """Sync attachments and the chat's message images before the MCP command.

        Files are resolved by id and synced only when the requesting user may read
        them. Ids the sandbox already imported are skipped before any lookup; the
        chat-image cap applies after that, so older images follow on later calls.
        Returns the discovery warnings and one line per newly synced chat image.
        """
        attached = _attachment_ids(__files__)
        chat_images = [i for i in await _chat_image_ids(chat_id, __user__) if i not in attached]
        if not attached and not chat_images:
            return ""
        orchestrator_url = self.valves.ORCHESTRATOR_URL
        internal_token = os.environ.get("OCU_INTERNAL_TOKEN", "")
        try:
            imported = await asyncio.to_thread(
                _imported_attachment_ids, orchestrator_url, chat_id,
                {"Authorization": f"Bearer {internal_token}"},
            )
        except Exception:
            # Unreadable receipts: post every id; the server answers a receipted id
            # with its stored record instead of writing the file again.
            imported = set()
        attached = [i for i in attached if i not in imported]
        chat_images = [i for i in chat_images if i not in imported][:_CHAT_IMAGE_LIMIT]
        entries = await _readable_files(
            [(i, False) for i in attached] + [(i, True) for i in chat_images], __user__
        )
        if not entries:
            return ""
        try:
            sync_result = await asyncio.to_thread(
                _sync_uploaded_files,
                orchestrator_url,
                chat_id,
                entries,
                internal_token,
                debug=self.valves.DEBUG_LOGGING,
            )
            if sync_result.get("synced", 0) > 0:
                print(f"Synced {sync_result['synced']} file(s)")
            chat_image_ids = set(chat_images)
            notes = list(sync_result.get("warnings", []))
            notes.extend(
                f"Synced image: /mnt/user-data/files/{stored_name}"
                for attachment_id, stored_name in sync_result.get("stored", [])
                if attachment_id in chat_image_ids
            )
            return "\n".join(notes)
        except Exception as e:
            print(f"[SYNC] Error: {e}")
            return ""

    async def _run_tool(
        self,
        tool_name: str,
        args: dict,
        chat_id: str,
        emitter: Optional[Callable[[dict], Awaitable[None]]],
        request,
        __user__: Optional[dict],
        in_progress_desc: str,
        ok_desc: str,
        err_desc: str,
        timeout: int = CLIENT_HTTP_TIMEOUT,
        sync_warning: str = "",
        extra_headers: Optional[dict] = None,
        image: bool = False,
    ) -> str:
        """One transport-aware MCP call with consistent SSE status events.

        Every per-tool wrapper (bash_tool/str_replace/create_file/view/sub_agent/generate_speech)
        funnels through here. Without this helper each wrapper duplicated:
          - the in_progress emit before the call
          - the try/except + final emit
          - the _looks_like_error → status decision
          - the wrapper-crash error string
        and they drifted (str_replace used `"error" in result.lower()[:20]` which
        false-positives on "errors fixed: 0", view only matched "Error:", etc).

          - Every caller validates chat metadata before file synchronization.
        """
        error = self._call_error(chat_id)
        if error:
            description = (
                "Chat ID is required"
                if error.startswith("[TOOL ERROR]")
                else "Computer Use configuration error"
            )
            await self._emit_terminal_error(emitter, description)
            return f"{error}\n\n{sync_warning}" if sync_warning else error

        async def emit(description: str, status: str, done: bool):
            await self._emit_status(emitter, description, status, done)

        await emit(in_progress_desc, "in_progress", False)
        invoked = False
        try:
            headers = self._build_mcp_headers(chat_id, __user__, request=request)
            if extra_headers:
                headers.update(extra_headers)
            client = self.mcp_client
            invoked = True
            result = await client.call_tool(
                tool_name, args, headers=headers, timeout=timeout,
                event_emitter=emitter, image=image,
            )
            is_err = _looks_like_error(result)
            await emit(err_desc if is_err else ok_desc, "error" if is_err else "complete", True)
        except Exception as e:
            await emit("Execution error", "error", True)
            result = f"[Error] {tool_name} wrapper crashed: {type(e).__name__}: {e}"
        if invoked:
            await self._emit_workspace_hint(emitter, chat_id)
        if sync_warning and _is_image_data_uri(result):
            # Open WebUI sends the whole string to the model as one image
            # (middleware.py 1124-1129); appended text would corrupt it.
            print(f"[SYNC] Not shown with the image result: {sync_warning}")
            return result
        return f"{result}\n\n{sync_warning}" if sync_warning else result

    # =========================================================================
    # Tool methods — delegate to computer-use-orchestrator via MCP Streamable HTTP
    # =========================================================================

    async def bash_tool(
        self,
        command: str,
        description: str,
        __event_emitter__: Callable[[dict], Awaitable[None]] = None,
        __metadata__: dict = None,
        __user__: dict = None,
        __files__: Optional[List[dict]] = None,
        __request__=None,
    ) -> str:
        """
        Run a bash command in the container

        :param command: Bash command to run in container
        :param description: Why I'm running this command
        :return: Command output (stdout/stderr)
        """
        chat_id, error = await self._prepare_tool_call(__metadata__, __event_emitter__)
        if error:
            return error
        sync_warning = await self._sync_files_if_needed(chat_id, __files__, __user__)
        return await self._run_tool(
            "bash_tool", {"command": command, "description": description},
            chat_id, __event_emitter__, __request__, __user__,
            in_progress_desc=description or "Executing bash command...",
            ok_desc="Command completed", err_desc="Command failed",
            sync_warning=sync_warning,
        )

    async def str_replace(
        self,
        description: str,
        old_str: str,
        path: str,
        new_str: str = "",
        __event_emitter__: Callable[[dict], Awaitable[None]] = None,
        __metadata__: dict = None,
        __user__: dict = None,
        __files__: Optional[List[dict]] = None,
        __request__=None,
    ) -> str:
        """
        Replace a unique string in a file. The string must appear exactly once.

        :param description: Why I'm making this edit
        :param old_str: String to replace (must be unique in file)
        :param new_str: String to replace with (empty to delete)
        :param path: Path to the file to edit
        :return: Success message or error
        """
        chat_id, error = await self._prepare_tool_call(__metadata__, __event_emitter__)
        if error:
            return error
        if old_str == new_str:
            return "Error: old_str and new_str are identical."
        sync_warning = await self._sync_files_if_needed(chat_id, __files__, __user__)
        return await self._run_tool(
            "str_replace", {"description": description, "old_str": old_str, "path": path, "new_str": new_str},
            chat_id, __event_emitter__, __request__, __user__,
            in_progress_desc=description or f"Editing {path}...",
            ok_desc="File edited", err_desc="Edit failed",
            sync_warning=sync_warning,
        )

    async def create_file(
        self,
        description: str,
        file_text: str,
        path: str,
        __event_emitter__: Callable[[dict], Awaitable[None]] = None,
        __metadata__: dict = None,
        __user__: dict = None,
        __files__: Optional[List[dict]] = None,
        __request__=None,
    ) -> str:
        """
        Create a new file with content in the container

        :param description: Why I'm creating this file
        :param file_text: Content to write to the file
        :param path: Path to the file to create
        :return: Success message or error
        """
        chat_id, error = await self._prepare_tool_call(__metadata__, __event_emitter__)
        if error:
            return error
        sync_warning = await self._sync_files_if_needed(chat_id, __files__, __user__)
        return await self._run_tool(
            "create_file", {"description": description, "file_text": file_text, "path": path},
            chat_id, __event_emitter__, __request__, __user__,
            in_progress_desc=description or f"Creating {path}...",
            ok_desc="File created", err_desc="Creation failed",
            sync_warning=sync_warning,
        )

    async def view(
        self,
        description: str,
        path: str,
        view_range: ViewRange = None,
        __event_emitter__: Callable[[dict], Awaitable[None]] = None,
        __metadata__: dict = None,
        __user__: dict = None,
        __files__: Optional[List[dict]] = None,
        __request__=None,
    ) -> str:
        """
        View text files, directory listings, images (.png, .jpg, .jpeg, .gif, .webp), or binary file info.

        :param description: Why I need to view this
        :param path: Absolute path to file or directory
        :param view_range: Optional [start_line, end_line]. Use [start, -1] for to-end.
        :return: File contents, directory listing, the image, or error message
        """
        chat_id, error = await self._prepare_tool_call(__metadata__, __event_emitter__)
        if error:
            return error
        is_image = isinstance(path, str) and path.lower().endswith(_IMAGE_EXTENSIONS)
        if is_image and _model_cannot_view_images(__metadata__):
            await self._emit_terminal_error(__event_emitter__, "Model cannot view images")
            return (
                "Error: this model cannot view images; its capabilities mark vision "
                "as off. Inspect the file with bash_tool instead, for example "
                "`file` or `ffprobe` for its size and format."
            )
        sync_warning = await self._sync_files_if_needed(chat_id, __files__, __user__)
        args = {"description": description, "path": path}
        if view_range:
            args["view_range"] = view_range
        return await self._run_tool(
            "view", args,
            chat_id, __event_emitter__, __request__, __user__,
            in_progress_desc=description or f"Reading {path}...",
            ok_desc="Read complete", err_desc="Read failed",
            sync_warning=sync_warning,
            image=is_image,
        )

    async def generate_speech(
        self,
        text: str,
        filename: str,
        instructions: str = "",
        voice: str = "",
        __event_emitter__: Callable[[dict], Awaitable[None]] = None,
        __metadata__: dict = None,
        __user__: dict = None,
        __files__: Optional[List[dict]] = None,
        __request__=None,
    ) -> str:
        """
        Synthesize speech, such as a video voiceover line, into /mnt/user-data/files/

        :param text: The words to speak, 1 to 2000 characters
        :param filename: Plain file name ending in .mp3 or .wav, e.g. line1.mp3
        :param instructions: Free-text voice description, e.g. "a calm female narrator, Mandarin"
        :param voice: Optional voice id; empty uses the server's default voice
        :return: The sandbox path of the audio file and its size, or an error
        """
        chat_id, error = await self._prepare_tool_call(__metadata__, __event_emitter__)
        if error:
            return error
        sync_warning = await self._sync_files_if_needed(chat_id, __files__, __user__)
        return await self._run_tool(
            "generate_speech",
            {"text": text, "filename": filename, "instructions": instructions, "voice": voice},
            chat_id, __event_emitter__, __request__, __user__,
            in_progress_desc=f"Generating speech {filename}...",
            ok_desc="Speech generated", err_desc="Speech generation failed",
            sync_warning=sync_warning,
        )

    async def sub_agent(
        self,
        task: str,
        description: str,
        model: str = "sonnet",
        max_turns: int = 25,
        mode: str = "act",
        working_directory: str = "/home/assistant",
        resume_session_id: str = "",
        __event_emitter__: Callable[[dict], Awaitable[None]] = None,
        __metadata__: dict = None,
        __user__: dict = None,
        __files__: Optional[List[dict]] = None,
        __request__=None,
    ) -> str:
        """
        Delegate complex, multi-step tasks to an autonomous sub-agent.

        :param task: Structured task description
        :param description: Why you are delegating this task
        :param model: AI model - "sonnet" (fast, default) or "opus" (powerful); in gateway deployments the chat's model is used and this is ignored
        :param max_turns: Max iterations, default 25 (raise to 50-80 for large multi-file refactors)
        :param mode: "act" (execute) or "plan" (plan only)
        :param working_directory: Work dir, default /home/assistant
        :param resume_session_id: Session ID to resume (from previous result); on resume, task carries the follow-up instruction and the original task stays in the plan file
        :return: Sub-agent's response with results, cost, turn count, session_id
        """
        chat_id, error = await self._prepare_tool_call(__metadata__, __event_emitter__)
        if error:
            return error
        sync_warning = await self._sync_files_if_needed(chat_id, __files__, __user__)
        args = {
            "task": task, "description": description, "model": model,
            "max_turns": max_turns, "working_directory": working_directory,
        }
        if resume_session_id:
            args["resume_session_id"] = resume_session_id
        return await self._run_tool(
            "sub_agent", args,
            chat_id, __event_emitter__, __request__, __user__,
            in_progress_desc=description or f"Starting sub-agent ({model})...",
            ok_desc="Sub-agent completed", err_desc="Sub-agent failed",
            timeout=SUB_AGENT_CLIENT_TIMEOUT,
            sync_warning=sync_warning,
            extra_headers=await _gateway_headers(__request__, __metadata__, __user__),
        )

    async def copy_knowledge_file(
        self,
        file_id: str,
        description: str,
        __event_emitter__: Callable[[dict], Awaitable[None]] = None,
        __metadata__: dict = None,
    ) -> str:
        """
        Copy one knowledge-base document's original file into /mnt/user-data/files/, so the sandbox can process it.
        1. Find the file's id with the knowledge tools (search_knowledge_files or query_knowledge_files).
        2. Copy it with this tool; the result names its path under /mnt/user-data/files/.
        3. Process the copy there, for example extract its figures with the docx or pdf skill.
        4. Show each extracted figure to the user as a markdown image ![description](URL) with its workspace file link.
        Only a file in a knowledge base the chat's owner can read is copied; copying it again returns the same path.

        :param file_id: The knowledge-base file's id, from the knowledge tools
        :param description: Why I'm copying this file
        :return: The sandbox path of the copy, or an error
        """
        chat_id, error = await self._prepare_tool_call(__metadata__, __event_emitter__)
        if error:
            return error
        emitter = __event_emitter__
        await self._emit_status(emitter, description or "Copying knowledge file...", "in_progress", False)
        entry, result = await _knowledge_file_entry(chat_id, file_id)
        if entry is not None:
            # Re-import: the receipt dedupes repeat copies, and a deleted copy is stored again.
            sync_result = await asyncio.to_thread(
                _sync_uploaded_files,
                self.valves.ORCHESTRATOR_URL,
                chat_id,
                [entry],
                os.environ.get("OCU_INTERNAL_TOKEN", ""),
                debug=self.valves.DEBUG_LOGGING,
                extra_headers={"X-OCU-Reimport-Missing": "1"},
            )
            if sync_result["synced"]:
                stored_name = sync_result["stored"][0][1]
                result = "\n\n".join([
                    f"Copied {entry['name']} to /mnt/user-data/files/{stored_name}",
                    *sync_result.get("warnings", []),
                ])
            else:
                result = f"Error: upload of {entry['name']} failed"
        if result.startswith("Error:"):
            await self._emit_terminal_error(emitter, "Knowledge file copy failed")
            return result
        await self._emit_status(emitter, "Knowledge file copied", "complete", True)
        await self._emit_workspace_hint(emitter, chat_id)
        return result


# ============================================================================
# File sync helper (HTTP — no SSH needed)
# ============================================================================

# Upstream generate_image records each image in the message's files as
# {"type": "image", "url": "/api/v1/files/<id>/content"} (tools/builtin.py).
_CHAT_IMAGE_URL = re.compile(r"^/api/v1/files/([0-9a-f-]+)/content$")
# Bounds the per-call file lookups in a chat with many images.
_CHAT_IMAGE_LIMIT = 20


def _message_timestamp(message):
    timestamp = message.get("timestamp")
    return timestamp if isinstance(timestamp, (int, float)) else 0


def _attachment_ids(files) -> list:
    """The ids of the request's attachments, in order; any other field is ignored."""
    ids = []
    for item in files or []:
        file_id = item.get("id") if isinstance(item, dict) else None
        if isinstance(file_id, str) and file_id and file_id not in ids:
            ids.append(file_id)
    return ids


async def _chat_image_ids(chat_id: str, user) -> list:
    """Ids of the images in this chat's messages, newest first.

    Read only when the requesting user owns the chat. Any failure is logged and
    yields no ids; it never fails the tool call.
    """
    try:
        from open_webui.models.chats import Chats

        chat = await Chats.get_chat_by_id(chat_id)
        if chat is None or chat.user_id != user["id"]:
            return []
        history = (chat.chat or {}).get("history") or {}
        messages = [m for m in (history.get("messages") or {}).values() if isinstance(m, dict)]
    except Exception as e:
        print(f"[SYNC] Chat images not read: {type(e).__name__}: {e}")
        return []

    file_ids = []
    for message in sorted(messages, key=_message_timestamp, reverse=True):
        for item in message.get("files") or []:
            if not isinstance(item, dict) or item.get("type") != "image":
                continue
            match = _CHAT_IMAGE_URL.match(str(item.get("url", "")))
            if match and match.group(1) not in file_ids:
                file_ids.append(match.group(1))
    return file_ids


async def _readable_files(candidates: list, user) -> list:
    """Sync entries for the (file_id, image_only) candidates the user may read.

    The single resolver for attachments and chat images: each file is loaded by
    id, kept when the user owns it or has read access (as Open WebUI's file
    content route decides, without its admin clause: admin read access is not
    execution authority in a user's sandbox), and synced from the row's own
    storage path. Refusals are logged.
    """
    if not candidates:
        return []
    try:
        from open_webui.models.files import Files
        from open_webui.models.users import Users
        from open_webui.utils.access_control.files import has_access_to_file

        owner = await Users.get_user_by_id(user["id"])
        if owner is None:
            raise LookupError("requesting user not found")
    except Exception as e:
        print(f"[SYNC] Files not resolved: {type(e).__name__}: {e}")
        return []

    entries = []
    for file_id, image_only in candidates:
        try:
            row, meta, entry = await _load_file(Files, file_id)
            if row is None or not row.path:
                reason = "no stored file"
            elif image_only and not str(meta.get("content_type") or "").startswith("image/"):
                reason = "not an image"
            elif row.user_id != owner.id and not await has_access_to_file(file_id, "read", owner):
                reason = "no read access for the requesting user"
            else:
                entries.append(entry())
                continue
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
        print(f"[SYNC] File {file_id} skipped: {reason}")
    return entries


async def _load_file(Files, file_id: str):
    """Load a WebUI file row for a resolver: (row, meta, entry).

    entry() builds the sync entry {"id", "name", "path"}, named after the basename of
    meta.name, else the filename, else the id. Each resolver calls it only after its
    own checks pass, so naming never runs for a refused row.
    """
    row = await Files.get_file_by_id(file_id)
    meta = (row.meta or {}) if row is not None else {}

    def entry():
        name = os.path.basename(meta.get("name") or row.filename or file_id)
        return {"id": row.id, "name": name, "path": row.path}

    return row, meta, entry


async def _knowledge_file_entry(chat_id: str, file_id: str):
    """(sync entry, None) for a file the chat's owner may read through a knowledge base,
    else (None, the Error: result).

    The subject is the chat's owner, whoever calls the tool. Administrator status, direct
    file ownership and the legacy meta.collection_name grant nothing. Access is checked
    before the stored path, so an unreadable file never reveals whether it has bytes.
    """
    try:
        from open_webui.models.chats import Chats

        owner_id = getattr(await Chats.get_chat_by_id(chat_id), "user_id", None)
    except Exception as e:
        print(f"[COPY] Chat {chat_id} not read: {type(e).__name__}: {e}")
        owner_id = None
    if not owner_id:
        return None, "Error: chat not found"
    try:
        from open_webui.models.files import Files

        row, _, entry = await _load_file(Files, file_id)
        if row is None:
            return None, f"Error: file {file_id} not found"
        if not await _owner_can_read_knowledge_file(file_id, owner_id):
            return None, f"Error: file {file_id} is not in a knowledge base the chat owner can read"
        if not row.path:
            return None, f"Error: file {file_id} has no stored bytes"
        return entry(), None
    except Exception as e:
        print(f"[COPY] File {file_id} lookup failed: {type(e).__name__}: {e}")
        return None, f"Error: lookup of file {file_id} failed"


async def _owner_can_read_knowledge_file(file_id: str, owner_id: str) -> bool:
    """True when a knowledge base linked to the file is owned by, or read-granted to,
    the owner: directly, through one of the owner's groups, or to every user (user:*)."""
    from open_webui.models.access_grants import AccessGrants
    from open_webui.models.groups import Groups
    from open_webui.models.knowledge import Knowledges

    group_ids = None
    for knowledge in await Knowledges.get_knowledges_by_file_id(file_id):
        if knowledge.user_id == owner_id:
            return True
        if group_ids is None:
            group_ids = {group.id for group in await Groups.get_groups_by_member_id(owner_id)}
        if await AccessGrants.has_access(
            user_id=owner_id, resource_type="knowledge", resource_id=knowledge.id,
            permission="read", user_group_ids=group_ids,
        ):
            return True
    return False


def _imported_attachment_ids(orchestrator_url, chat_id, headers):
    import requests

    imports_url = f"{orchestrator_url}/api/uploads/{chat_id}/imports"
    response = requests.get(
        imports_url, headers=headers, timeout=5, allow_redirects=False
    )
    if 300 <= response.status_code < 400:
        raise requests.HTTPError(
            f"redirect blocked (HTTP {response.status_code})"
        )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("malformed imports response")
    ids = payload.get("ids")
    if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
        raise ValueError("malformed imports response")
    return set(ids)


_DISCOVERY_REASONS = frozenset({
    "busy", "file_limit", "registry_limit", "entry_limit", "directory_limit",
    "descriptor_limit", "io_error", "internal_error",
})


def _sync_uploaded_files(
    orchestrator_url: str,
    chat_id: str,
    entries: list,
    internal_token: str,
    debug: bool = False,
    *,
    extra_headers: Optional[dict] = None,
) -> dict:
    """Upload resolved files ({"id", "name", "path"} from a resolver) by WebUI id.

    extra_headers are sent on every upload, after the attachment-id header.
    """
    import requests

    headers = {"Authorization": f"Bearer {internal_token}"}
    synced, errors = 0, 0
    warnings = []
    stored = []

    for entry in entries:
        temp_file_path = None
        try:
            attachment_id = entry["id"]
            filename = entry["name"]
            source_path = entry["path"]
            try:
                from open_webui.storage.provider import Storage
                local_file_path = Storage.get_file(source_path)
                if local_file_path != source_path:
                    temp_file_path = local_file_path
                source_path = local_file_path
            except Exception:
                errors += 1
                continue

            if not os.path.exists(source_path):
                errors += 1
                continue

            encoded_name = urllib.parse.quote(filename, safe="")
            upload_url = (
                f"{orchestrator_url}/api/uploads/{chat_id}/{encoded_name}"
            )
            upload_headers = {
                **headers,
                "X-OCU-Attachment-Id": attachment_id,
                **(extra_headers or {}),
            }
            with open(source_path, "rb") as f:
                files_data = {"file": (filename, f, "application/octet-stream")}
                resp = requests.post(
                    upload_url,
                    files=files_data,
                    headers=upload_headers,
                    timeout=30,
                    allow_redirects=False,
                )
                if 300 <= resp.status_code < 400:
                    raise requests.HTTPError(
                        f"redirect blocked (HTTP {resp.status_code})"
                    )
                resp.raise_for_status()
            synced += 1
            try:
                payload = resp.json()
            except ValueError:
                payload = {}
            stored_filename = filename
            if isinstance(payload, dict) and isinstance(payload.get("filename"), str):
                stored_filename = payload["filename"]
            stored.append((attachment_id, stored_filename))
            discovery = payload.get("discovery") if isinstance(payload, dict) else None
            if isinstance(discovery, dict) and discovery.get("status") == "failed":
                reason = discovery.get("reason")
                if not isinstance(reason, str) or reason not in _DISCOVERY_REASONS:
                    reason = "internal_error"
                warnings.append(
                    f"[DISCOVERY WARNING] File {stored_filename!r} was stored, but workspace "
                    f"file discovery failed ({reason}). Do not upload it again."
                )
        except Exception:
            errors += 1
        finally:
            if temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.unlink(temp_file_path)
                except Exception:
                    pass

    result = {"synced": synced, "errors": errors}
    if warnings:
        result["warnings"] = warnings
    if stored:
        result["stored"] = stored
    return result
