# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""
Docker container management for Computer Use.

Handles:
- Docker client initialization (local socket)
- Explicit per-chat lifecycle (get/create/launch/describe)
- Shared thread lock plus filesystem flock
- Host-owned pause-aware idle reclamation
- Command execution (bash, python with stdin)

Extracted from mcp_tools.py to reduce file size and separate concerns.
"""

import os
import ipaddress
import sys
import re
import json
import shlex
import time
import datetime
import fcntl
import threading
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import aiohttp
import docker
from docker.utils.socket import frames_iter, demux_adaptor, consume_socket_output

import skill_manager
from context_vars import (
    current_chat_id, current_user_email, current_user_name,
    current_gitlab_token, current_gitlab_host,
    current_anthropic_auth_token, current_anthropic_base_url,
    current_mcp_tokens_url, current_mcp_tokens_api_key, current_mcp_servers,
    current_credential_source,
)
from system_prompt import render_system_prompt_sync

DOCKER_SOCKET = os.getenv("DOCKER_SOCKET", "unix:///var/run/docker.sock")
DOCKER_IMAGE = os.getenv("DOCKER_IMAGE", "open-computer-use:latest")
CONTAINER_MEM_LIMIT = os.getenv("CONTAINER_MEM_LIMIT", "2g")
CONTAINER_CPU_LIMIT = float(os.getenv("CONTAINER_CPU_LIMIT", "1.0"))
COMMAND_TIMEOUT = int(os.getenv("COMMAND_TIMEOUT", "120"))
ENABLE_NETWORK = os.getenv("ENABLE_NETWORK", "true").lower() == "true"
USER_DATA_BASE_PATH = os.getenv("USER_DATA_BASE_PATH", "/tmp/computer-use-data")
# Public URL of the orchestrator — the single source of truth for browser-facing
# preview/archive links. Baked into /system-prompt so the model writes correct
# clickable URLs, and returned to the Open WebUI filter via the X-Public-Base-URL
# response header so outlet() decorations also use it.
#
# Internal-DNS default is only reachable from inside the compose network. Users
# must override with a browser-reachable URL (http://localhost:8081 for local
# dev, https://cu.example.com for prod) for the preview panel to work.
# See docs/openwebui-filter.md.
PUBLIC_BASE_URL_DEFAULT = "http://computer-use-server:8081"
# Treat empty string as unset because docker-compose's `${VAR:-}` always sets the
# variable. A non-empty configured value is preserved verbatim so startup
# validation can reject a trailing slash instead of silently rewriting it.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL") or PUBLIC_BASE_URL_DEFAULT


def validate_public_base_url() -> int:
    """Return 0 when the raw configured public base is usable at startup."""
    configured = os.getenv("PUBLIC_BASE_URL")
    if configured and configured.endswith("/"):
        print(
            "PUBLIC_BASE_URL must not end with '/'. Remove the trailing slash "
            "and restart.",
            file=sys.stderr,
        )
        return 1
    return 0

CONTAINER_IDLE_TIMEOUT = int(os.getenv("CONTAINER_IDLE_TIMEOUT", "600"))

_IDLE_POLL_RAW = os.getenv("OCU_IDLE_POLL_SECONDS", "30")
try:
    IDLE_POLL_SECONDS = int(_IDLE_POLL_RAW)
except (TypeError, ValueError):
    IDLE_POLL_SECONDS = -1
RESTART_WAIT_SECONDS = 5
RESTART_POLL_SECONDS = 0.05
STOPPED_MESSAGE = "workspace is stopped and needs an explicit launch"


class LifecycleError(Exception):
    status_code = 500
    reason = "lifecycle_error"

    def __init__(self, message=None, status_code=None, reason=None):
        self.status_code = self.status_code if status_code is None else status_code
        self.reason = self.reason if reason is None else reason
        super().__init__(message or self.reason)


class SandboxStopped(LifecycleError):
    def __init__(self, message=STOPPED_MESSAGE):
        super().__init__(message, status_code=409, reason="sandbox_stopped")


class NeverCreated(LifecycleError):
    def __init__(self):
        super().__init__(
            "sandbox was never created",
            status_code=409,
            reason="never_created",
        )


class MetadataCorrupt(LifecycleError):
    def __init__(self, message="sandbox metadata is corrupt"):
        super().__init__(message, status_code=500, reason="metadata_corrupt")


class LaunchFailed(LifecycleError):
    def __init__(self, status_code, message):
        super().__init__(message, status_code=status_code, reason="launch_failed")


class MigrationRequired(LifecycleError):
    def __init__(self, message="migration_required"):
        super().__init__(message, status_code=409, reason="migration_required")


class LifecycleConfigError(LifecycleError):
    def __init__(self, message):
        super().__init__(message, status_code=500, reason="invalid_idle_configuration")


_CHAT_LOCKS_GUARD = threading.Lock()
_chat_locks: dict[str, threading.RLock] = {}
_FLOCK_DEPTH: dict[str, int] = {}


def validate_idle_configuration(idle_timeout=None, poll_seconds=None):
    """Reject a poll that can miss a whole idle window."""
    timeout = CONTAINER_IDLE_TIMEOUT if idle_timeout is None else idle_timeout
    poll = IDLE_POLL_SECONDS if poll_seconds is None else poll_seconds
    if not isinstance(timeout, int) or timeout <= 0 or not isinstance(poll, int) or poll <= 0 or poll >= timeout:
        raise LifecycleConfigError(
            f"OCU_IDLE_POLL_SECONDS ({poll}) must be a positive integer shorter than "
            f"CONTAINER_IDLE_TIMEOUT ({timeout})"
        )
    return timeout, poll


def canonical_lock_chat_id(chat_id: str) -> str:
    from security import sanitize_chat_id
    return sanitize_chat_id(chat_id or "")

def get_chat_lock(chat_id: str) -> threading.RLock:
    """Return the stable process-local lock for one canonical chat."""
    key = canonical_lock_chat_id(chat_id)
    with _CHAT_LOCKS_GUARD:
        lock = _chat_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _chat_locks[key] = lock
        return lock


def _control_dir(chat_id: str) -> Path:
    key = canonical_lock_chat_id(chat_id)
    return BASE_DATA_DIR / key

class _CombinedLock:
    """Process lock first, then one shared flock. Reentrant on the owner thread.

    threading.RLock replaces a plain Lock so a lifecycle transaction can call
    another locked helper without deadlocking. Flock depth is per chat and is
    mutated only while that RLock is held, so two threads cannot interleave it.
    """

    def __init__(self, chat_id: str):
        self.chat_id = canonical_lock_chat_id(chat_id)
        self._thread_lock = get_chat_lock(self.chat_id)
        self._file = None

    def __enter__(self):
        self._thread_lock.acquire()
        try:
            depth = _FLOCK_DEPTH.get(self.chat_id, 0)
            if depth:
                _FLOCK_DEPTH[self.chat_id] = depth + 1
                return self
            path = _control_dir(self.chat_id) / ".lifecycle.lock"
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except BaseException:
                handle.close()
                raise
            self._file = handle
            _FLOCK_DEPTH[self.chat_id] = 1
            return self
        except BaseException:
            self._thread_lock.release()
            raise

    def __exit__(self, exc_type, exc, tb):
        try:
            depth = _FLOCK_DEPTH.get(self.chat_id, 1)
            if depth > 1:
                _FLOCK_DEPTH[self.chat_id] = depth - 1
            else:
                _FLOCK_DEPTH.pop(self.chat_id, None)
                if self._file is not None:
                    fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
                    self._file.close()
                    self._file = None
        finally:
            self._thread_lock.release()
        return False


def _combined_lock(chat_id: str) -> _CombinedLock:
    return _CombinedLock(chat_id)
OCU_SANDBOX_NETWORK = os.getenv("OCU_SANDBOX_NETWORK", "ocu-sandbox").strip() or "ocu-sandbox"
SANDBOX_HOST_BIND_IP = os.getenv("SANDBOX_HOST_BIND_IP", "").strip()
_RESERVED_SANDBOX_NETWORKS = frozenset({"bridge", "host", "none"})
_LIVE_SANDBOX_STATUSES = frozenset({"running", "paused", "restarting"})
# Service ports INSIDE the sandbox. Fixed by the workspace image; only the host side of each
# mapping varies, and the container engine assigns that.
CDP_PORT = int(os.getenv("CDP_PORT", "9222"))    # Chrome DevTools
TTYD_PORT = int(os.getenv("TTYD_PORT", "7681"))  # web terminal
SANDBOX_PUBLISHED_PORTS = (CDP_PORT, TTYD_PORT)
BASE_DATA_DIR = Path(os.getenv("BASE_DATA_DIR", "/data"))

# MCP Tokens Wrapper for GitLab token fetching
MCP_TOKENS_URL = os.getenv("MCP_TOKENS_URL", "")
MCP_TOKENS_API_KEY = os.getenv("MCP_TOKENS_API_KEY", "")

# Sub-agent configuration — per-CLI default models (D-03/D-04).
# The legacy single SUB_AGENT_DEFAULT_MODEL global was removed in Phase 2;
# the deprecation grace window from Phase 1 D-10 is over. The per-CLI env
# vars (CLAUDE_/CODEX_/OPENCODE_SUB_AGENT_DEFAULT_MODEL) are read directly
# by the resolver in cli_runtime.py — no module-level constants needed
# here. The resolver raises a clear ValueError when caller passes no model
# AND the per-CLI env is unset (opencode/codex only; claude falls back to
# the canonical 'sonnet' alias).
SUB_AGENT_MAX_TURNS = int(os.getenv("SUB_AGENT_MAX_TURNS", "25"))
SUB_AGENT_TIMEOUT = int(os.getenv("SUB_AGENT_TIMEOUT", "3600"))

# Anthropic API (shared LiteLLM proxy key — fallback when no header provided)
# NB: os.getenv falls back to the default only when the var is UNSET. In docker
# compose with `${VAR:-}` the var is always set to "", so treat empty == unset.
ANTHROPIC_AUTH_TOKEN = os.getenv("ANTHROPIC_AUTH_TOKEN", "")
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"

# Claude Code model ID overrides (pass through only when set on host — GATEWAY-02)
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "")
ANTHROPIC_DEFAULT_SONNET_MODEL = os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "")
ANTHROPIC_DEFAULT_OPUS_MODEL = os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL", "")
ANTHROPIC_DEFAULT_HAIKU_MODEL = os.getenv("ANTHROPIC_DEFAULT_HAIKU_MODEL", "")
CLAUDE_CODE_SUBAGENT_MODEL = os.getenv("CLAUDE_CODE_SUBAGENT_MODEL", "")
# Claude Code gateway compatibility flags (set to "1" to disable — GATEWAY-02)
CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS = os.getenv("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS", "")
DISABLE_PROMPT_CACHING = os.getenv("DISABLE_PROMPT_CACHING", "")
DISABLE_PROMPT_CACHING_SONNET = os.getenv("DISABLE_PROMPT_CACHING_SONNET", "")
DISABLE_PROMPT_CACHING_OPUS = os.getenv("DISABLE_PROMPT_CACHING_OPUS", "")
DISABLE_PROMPT_CACHING_HAIKU = os.getenv("DISABLE_PROMPT_CACHING_HAIKU", "")

# Tuple (not dict) for deterministic iteration order in tests — GATEWAY-03.
CLAUDE_CODE_PASSTHROUGH_ENVS = (
    ("ANTHROPIC_MODEL", ANTHROPIC_MODEL),
    ("ANTHROPIC_DEFAULT_SONNET_MODEL", ANTHROPIC_DEFAULT_SONNET_MODEL),
    ("ANTHROPIC_DEFAULT_OPUS_MODEL", ANTHROPIC_DEFAULT_OPUS_MODEL),
    ("ANTHROPIC_DEFAULT_HAIKU_MODEL", ANTHROPIC_DEFAULT_HAIKU_MODEL),
    ("CLAUDE_CODE_SUBAGENT_MODEL", CLAUDE_CODE_SUBAGENT_MODEL),
    ("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS", CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS),
    ("DISABLE_PROMPT_CACHING", DISABLE_PROMPT_CACHING),
    ("DISABLE_PROMPT_CACHING_SONNET", DISABLE_PROMPT_CACHING_SONNET),
    ("DISABLE_PROMPT_CACHING_OPUS", DISABLE_PROMPT_CACHING_OPUS),
    ("DISABLE_PROMPT_CACHING_HAIKU", DISABLE_PROMPT_CACHING_HAIKU),
)

# Codex passthrough envs (Phase 6 — only injected when SUBAGENT_CLI=codex).
# Per AUTH-01 — closes Pitfall 1 (auth bleed across CLIs).
CODEX_PASSTHROUGH_ENVS = (
    ("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY", "")),
    ("OPENAI_BASE_URL", os.getenv("OPENAI_BASE_URL", "")),
    ("CODEX_MODEL", os.getenv("CODEX_MODEL", "")),
    ("AZURE_OPENAI_API_KEY", os.getenv("AZURE_OPENAI_API_KEY", "")),
    ("AZURE_OPENAI_ENDPOINT", os.getenv("AZURE_OPENAI_ENDPOINT", "")),
    ("AZURE_OPENAI_API_VERSION", os.getenv("AZURE_OPENAI_API_VERSION", "")),
    # Operator-supplied codex config override (see docs/cli-config-templates.md
    # "Codex — custom OpenAI-compat gateway" recipe). Appended to the canonical
    # ~/.codex/config.toml block by the Dockerfile entrypoint when set, so
    # operators can route codex through a self-hosted gateway without forking.
    # Without this entry the override never crosses the orchestrator → sandbox
    # boundary. Same gap as #77; included here for codex parity.
    ("CODEX_CONFIG_EXTRA", os.getenv("CODEX_CONFIG_EXTRA", "")),
)

# OpenCode passthrough envs (Phase 6 — only injected when SUBAGENT_CLI=opencode).
# Includes OPENAI_API_KEY and ANTHROPIC_API_KEY because OpenCode itself supports
# multiple providers; the allowlist is per-CLI, not per-provider.
OPENCODE_PASSTHROUGH_ENVS = (
    ("OPENROUTER_API_KEY", os.getenv("OPENROUTER_API_KEY", "")),
    ("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY", "")),
    ("ANTHROPIC_API_KEY", os.getenv("ANTHROPIC_API_KEY", "")),
    ("OPENCODE_MODEL", os.getenv("OPENCODE_MODEL", "")),
    # Operator-supplied OpenCode config override (see docs/cli-config-templates.md
    # "OpenCode — custom OpenAI-compat provider" recipe). Replaces /tmp/opencode.json
    # verbatim when set, so operators can route the opencode sub-agent through a
    # self-hosted gateway (LiteLLM, OpenLLM, etc.) for proxy-only deployments.
    # Without this entry the override never crosses the orchestrator → sandbox
    # boundary and the entrypoint heredoc falls through to the canonical
    # 3-provider default. Closes #77.
    ("OPENCODE_CONFIG_EXTRA", os.getenv("OPENCODE_CONFIG_EXTRA", "")),
)

# Sub-agent CLI runtime selector (CLI-01, CLI-02). Read once at module load
# and propagated to every spawned container via extra_env (D5 shape a).
# Empty/unset → "claude" (backwards-compat invariant). Invalid value → hard
# fail at module load (D1) so a typo in .env is visible in the very first
# `docker compose up` log line, never silently runs the wrong CLI.
_ALLOWED_CLIS = {"claude", "codex", "opencode"}
_raw_subagent_cli = os.getenv("SUBAGENT_CLI", "").strip().lower()
if _raw_subagent_cli and _raw_subagent_cli not in _ALLOWED_CLIS:
    print(
        f"[computer-use-server] FATAL: SUBAGENT_CLI={_raw_subagent_cli!r} "
        f"is not one of {{claude, codex, opencode}}.",
        file=sys.stderr,
    )
    sys.exit(1)
SUBAGENT_CLI = _raw_subagent_cli or "claude"

# Active passthrough set selected by SUBAGENT_CLI — AUTH-01 / Pitfall 1.
# Single source of truth for "which auth env vars cross the orchestrator->sandbox
# boundary for this runtime". `_create_container` reads this once per container.
_PASSTHROUGH_BY_CLI = {
    "claude": CLAUDE_CODE_PASSTHROUGH_ENVS,
    "codex": CODEX_PASSTHROUGH_ENVS,
    "opencode": OPENCODE_PASSTHROUGH_ENVS,
}

# Vision API for describe-image / upd-processing skills
VISION_API_KEY = os.getenv("VISION_API_KEY", "")
VISION_API_URL = os.getenv("VISION_API_URL", "")
VISION_MODEL = os.getenv("VISION_MODEL", "gpt-4o")


def warn_if_public_base_url_is_default() -> bool:
    """Emit a one-time startup warning when PUBLIC_BASE_URL is still the
    hardcoded internal-DNS default.

    The default (http://computer-use-server:8081) is only reachable from inside
    the compose network. Since the public URL is now baked into /system-prompt
    and returned to the filter via response header, a default value means the
    preview panel will never appear — the browser cannot resolve the internal
    DNS name.

    Returns True if a warning was emitted (useful for tests), False otherwise.
    Called once from FastAPI lifespan startup — do not call per-request.
    """
    if PUBLIC_BASE_URL == PUBLIC_BASE_URL_DEFAULT:
        print(
            "[computer-use-server] WARNING: PUBLIC_BASE_URL is still the "
            f"hardcoded default ({PUBLIC_BASE_URL_DEFAULT!r}). This URL is only "
            "reachable from inside the compose network — the Open WebUI preview "
            "panel will never appear until you set it to a browser-reachable URL.\n"
            "  Fix: in .env, set PUBLIC_BASE_URL=http://<browser-reachable-host>:8081.\n"
            "  Docs: https://github.com/Wide-Moat/open-computer-use/blob/main/docs/openwebui-filter.md"
        )
        return True
    return False


def warn_if_mcp_api_key_missing() -> bool:
    """Emit a one-time startup warning when MCP_API_KEY is empty.

    An empty MCP_API_KEY makes every /mcp endpoint publicly callable without
    authentication — fine for local dev, dangerous for any deployment the
    internet can reach. Warn loudly so the condition does not silently survive
    a prod rollout.

    Returns True if a warning was emitted (useful for tests), False otherwise.
    Called once from FastAPI lifespan startup — do not call per-request.
    """
    if not os.getenv("MCP_API_KEY", ""):
        print(
            "[computer-use-server] WARNING: MCP_API_KEY is empty — the /mcp "
            "endpoints accept ANY caller with no auth. Acceptable for local "
            "development, unsafe for anything reachable from the internet.\n"
            "  Fix: set MCP_API_KEY in .env to a long random string and mirror "
            "it in the Open WebUI tool Valve (Admin → Tools → Computer Use → "
            "Valves → MCP_API_KEY)."
        )
        return True
    return False


def warn_subagent_cli() -> bool:
    """Emit a one-line banner naming the active sub-agent CLI runtime.

    Always prints (informational, not gated on a default) so operators have
    visible confirmation that SUBAGENT_CLI took effect after a docker compose
    restart. Mirrors warn_if_public_base_url_is_default's bool-return
    contract so app.py lifespan can collect emission flags for future
    telemetry. Closes the UX gap from PITFALLS.md UX table row 1.

    Returns True (always emitted, kept for symmetry with sibling warn_*).
    Called once from FastAPI lifespan startup — do not call per-request.
    """
    print(f"[MCP] Sub-agent runtime: {SUBAGENT_CLI}")
    return True


async def _fetch_gitlab_token(email: str, mcp_tokens_url: str, mcp_tokens_api_key: str) -> Optional[str]:
    """
    Fetch decrypted GitLab token from MCP Tokens Wrapper service.

    Args:
        email: User email address
        mcp_tokens_url: URL of MCP Tokens Wrapper service
        mcp_tokens_api_key: Internal API key for authentication

    Returns:
        GitLab token string or None if not found/error
    """
    if not mcp_tokens_api_key:
        print("[GITLAB] MCP_TOKENS_API_KEY not configured, skipping token fetch")
        return None

    if not email:
        print("[GITLAB] No email provided, skipping token fetch")
        return None

    url = f"{mcp_tokens_url}/api/internal/tokens/{email}/gitlab"
    headers = {"X-Internal-Api-Key": mcp_tokens_api_key}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status == 200:
                    data = await response.json()
                    token = data.get("token")
                    if token:
                        print(f"[GITLAB] Token fetched for {email}")
                        return token
                elif response.status == 404:
                    print(f"[GITLAB] No token found for {email}")
                else:
                    print(f"[GITLAB] Error fetching token: HTTP {response.status}")
    except asyncio.TimeoutError:
        print(f"[GITLAB] Timeout fetching token for {email}")
    except Exception as e:
        print(f"[GITLAB] Error fetching token: {e}")

async def _ensure_gitlab_token():
    """
    Ensure GitLab token is available, fetching from MCP Tokens Wrapper if needed.

    Priority:
    1. Token from header (current_gitlab_token already set)
    2. Fetch from MCP Tokens Wrapper by user email
    3. No token (continue without GitLab auth)
    """
    # If token already set from header, use it
    if current_gitlab_token.get():
        return

    # Try to fetch from MCP Tokens Wrapper
    user_email = current_user_email.get()
    mcp_tokens_url = current_mcp_tokens_url.get() or MCP_TOKENS_URL
    mcp_tokens_api_key = current_mcp_tokens_api_key.get() or MCP_TOKENS_API_KEY

    if user_email and mcp_tokens_url and mcp_tokens_api_key:
        token = await _fetch_gitlab_token(user_email, mcp_tokens_url, mcp_tokens_api_key)
        if token:
            current_gitlab_token.set(token)

def _server_gitlab_token(email: str):
    """Trusted server-side GitLab token lookup by metadata email. Never uses request credentials."""
    if not email or not MCP_TOKENS_URL or not MCP_TOKENS_API_KEY:
        return None
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_fetch_gitlab_token(email, MCP_TOKENS_URL, MCP_TOKENS_API_KEY))
    return None




# Global Docker client (lazy init)
_docker_client: Optional[docker.DockerClient] = None



def build_mcp_config(server_names_csv: str, base_url: Optional[str], user_email: str = "") -> dict | None:
    """Build Claude Code ~/.mcp.json config from comma-separated server names.

    URLs are templated as {base_url}/mcp/{server_name} (LiteLLM MCP proxy pattern).
    Authorization uses ANTHROPIC_AUTH_TOKEN env var (resolved inside container at write time).

    Returns dict ready for json.dumps, or None if no servers specified.

    ``base_url`` may be None or empty; both fall back to the module-level
    ANTHROPIC_BASE_URL constant so callers can pass the ContextVar value
    directly without a manual fallback.
    """
    # Blocklist: prevent recursive sub_agent loops
    BLOCKED_SERVERS = {"docker_ai", "docker-ai"}

    names = [s.strip() for s in server_names_csv.split(",") if s.strip() and s.strip() not in BLOCKED_SERVERS]
    if not names:
        return None

    base = (base_url or ANTHROPIC_BASE_URL or "https://api.anthropic.com").rstrip("/")
    servers = {}
    for name in names:
        servers[name] = {
            "type": "http",
            "url": f"{base}/mcp/{name}",
            "headers": {
                "x-openwebui-user-email": user_email,
            },
        }
    return {"mcpServers": servers}


def build_mcp_config_write_script(mcp_config: dict) -> str:
    """Build a shell command that writes ~/.mcp.json inside a container.

    ANTHROPIC_AUTH_TOKEN is resolved from the container's env at runtime,
    so no secrets are baked into the script itself.
    Uses base64 to avoid shell/JSON escaping issues.
    """
    import base64
    config_b64 = base64.b64encode(json.dumps(mcp_config).encode()).decode()
    return (
        f"python3 -c '"
        f"import json,os,base64;"
        f"c=json.loads(base64.b64decode(\"{config_b64}\"));"
        f"k=os.environ.get(\"ANTHROPIC_AUTH_TOKEN\",\"\");"
        f"[s[\"headers\"].__setitem__(\"Authorization\",\"Bearer \"+k)"
        f" for s in c[\"mcpServers\"].values() if \"headers\" in s];"
        f"json.dump(c,open(os.path.expanduser(\"~/.mcp.json\"),\"w\"),indent=2);"
        # Auto-approve MCP servers in settings.local.json so Claude Code doesn't ask
        f"p=os.path.expanduser(\"~/.claude/settings.local.json\");"
        f"sl=json.load(open(p)) if os.path.exists(p) else {{}};"
        f"sl[\"enabledMcpjsonServers\"]=list(c[\"mcpServers\"].keys());"
        f"json.dump(sl,open(p,\"w\"),indent=2)"
        f"'"
    )


def get_docker_client() -> docker.DockerClient:
    """Get or create Docker client connected to local socket."""
    global _docker_client
    if _docker_client is None:
        _docker_client = docker.DockerClient(base_url=DOCKER_SOCKET)
        _docker_client.ping()
        print(f"[MCP] Connected to Docker at {DOCKER_SOCKET}")
    return _docker_client


def _build_container_env(extra_env: Optional[dict] = None) -> dict:
    """Build environment variables dict for container."""
    env = {
        "NPM_CONFIG_PREFIX": "/usr/local/lib/node_modules_global",
    }
    if extra_env:
        env.update(extra_env)
    return env


# Provisioned sandbox bridge is inspected on every use; never cached.


def _is_ipv4_address(value: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(value), ipaddress.IPv4Address)
    except ValueError:
        return False


def _network_disabled_on(container) -> bool:
    attrs = getattr(container, "attrs", None) or {}
    config = attrs.get("Config") or {}
    if config.get("NetworkDisabled") is True:
        return True
    host = attrs.get("HostConfig") or {}
    mode = (host.get("NetworkMode") or "").lower()
    return mode in {"none", "disabled"}


def _inspect_sandbox_network(client=None) -> tuple:
    """Return (network, gateway) for the deployment-provisioned sandbox bridge."""
    name = (OCU_SANDBOX_NETWORK or "").strip()
    if not name or name.lower() in _RESERVED_SANDBOX_NETWORKS:
        raise LaunchFailed(500, f"sandbox network {name!r} is not a dedicated bridge")
    client = client or get_docker_client()
    try:
        network = client.networks.get(name)
    except docker.errors.NotFound as exc:
        raise LaunchFailed(500, f"sandbox network {name!r} is missing") from exc
    except docker.errors.APIError as exc:
        raise LaunchFailed(500, f"sandbox network {name!r} lookup failed: {exc}") from exc
    attrs = getattr(network, "attrs", None) or {}
    inspected_name = ((attrs.get("Name") or getattr(network, "name", "") or "")).strip()
    inspected_id = (getattr(network, "id", None) or attrs.get("Id") or "").strip()
    driver = (attrs.get("Driver") or "").lower()
    if (
        inspected_name.lower() in _RESERVED_SANDBOX_NETWORKS
        or not inspected_id
        or driver != "bridge"
        or attrs.get("Internal") is True
    ):
        raise LaunchFailed(500, f"sandbox network {name!r} is not a dedicated non-internal bridge")
    gateways = []
    for item in ((attrs.get("IPAM") or {}).get("Config") or []):
        gateway = (item or {}).get("Gateway")
        if gateway and _is_ipv4_address(gateway):
            gateways.append(gateway)
    unique = list(dict.fromkeys(gateways))
    if len(unique) != 1:
        raise LaunchFailed(500, f"sandbox network {name!r} has no unambiguous IPv4 gateway")
    gateway = unique[0]
    configured = (SANDBOX_HOST_BIND_IP or "").strip()
    if configured:
        if not _is_ipv4_address(configured):
            raise LaunchFailed(500, f"SANDBOX_HOST_BIND_IP {configured!r} is not a valid IPv4 address")
        if configured != gateway:
            raise LaunchFailed(
                500,
                f"SANDBOX_HOST_BIND_IP {configured} differs from inspected gateway {gateway}",
            )
        gateway = configured
    return network, gateway


def _network_identity(network) -> tuple[str, str]:
    attrs = getattr(network, "attrs", None) or {}
    name = (getattr(network, "name", None) or attrs.get("Name") or "").strip()
    network_id = (getattr(network, "id", None) or attrs.get("Id") or "").strip()
    return name, network_id


def _membership_entry_id(data) -> str:
    return str((data or {}).get("NetworkID") or (data or {}).get("NetworkId") or "").strip()


def _current_membership(container) -> dict:
    return dict(((container.attrs.get("NetworkSettings") or {}).get("Networks") or {}))


def _membership_matches(container, network) -> bool:
    membership = _current_membership(container)
    if len(membership) != 1:
        return False
    name, data = next(iter(membership.items()))
    desired_name, desired_id = _network_identity(network)
    current_id = _membership_entry_id(data)
    return bool(desired_name and desired_id and current_id) and name == desired_name and current_id == desired_id


def _immutable_bindings(container) -> dict:
    host = (container.attrs.get("HostConfig") or {})
    bindings = host.get("PortBindings") or {}
    return bindings if isinstance(bindings, dict) else {}


def _binding_entries(bindings, port: int) -> list:
    entries = bindings.get(f"{port}/tcp")
    if not entries:
        return []
    if isinstance(entries, dict):
        return [entries]
    return list(entries)


def _bindings_match_gateway(container, gateway: str) -> bool:
    bindings = _immutable_bindings(container)
    for port in SANDBOX_PUBLISHED_PORTS:
        entries = _binding_entries(bindings, port)
        if not entries:
            return False
        for entry in entries:
            host_ip = (entry or {}).get("HostIp") or ""
            if host_ip != gateway:
                return False
    return True


def _incompatible_bindings_error() -> LaunchFailed:
    return LaunchFailed(
        409,
        "sandbox port bindings are incompatible with the configured gateway; operator migration is required",
    )


def _require_enabled_compatibility(container, network, gateway: str, *, live: bool) -> None:
    if not _bindings_match_gateway(container, gateway):
        raise _incompatible_bindings_error()
    if _membership_matches(container, network):
        return
    if live:
        raise LaunchFailed(
            409,
            "live sandbox network membership is incompatible; stop the sandbox before migration",
        )


def _repair_stopped_membership(client, container, network) -> None:
    desired_name, desired_id = _network_identity(network)
    membership = _current_membership(container)
    try:
        for name, data in list(membership.items()):
            current_id = _membership_entry_id(data)
            keep = name == desired_name and current_id == desired_id
            if keep:
                continue
            lookup = current_id or name
            try:
                client.networks.get(lookup).disconnect(container, force=True)
            except docker.errors.NotFound as exc:
                raise LaunchFailed(
                    500,
                    "sandbox network repair failed: stale membership cannot be detached",
                ) from exc
            except docker.errors.APIError as cop_exc:
                raise LaunchFailed(500, f"sandbox network repair failed: {cop_exc}") from cop_exc
        container.reload()
        network, gateway = _inspect_sandbox_network(client)
        if not _bindings_match_gateway(container, gateway):
            raise _incompatible_bindings_error()
        if not _membership_matches(container, network):
            try:
                network.connect(container)
            except docker.errors.APIError as exc:
                raise LaunchFailed(500, f"sandbox network repair failed: {exc}") from exc
            container.reload()
            network, gateway = _inspect_sandbox_network(client)
            if not _bindings_match_gateway(container, gateway):
                raise _incompatible_bindings_error()
        if not _membership_matches(container, network):
            raise LaunchFailed(500, "sandbox network repair did not reach the desired membership")
    except LaunchFailed:
        raise
    except docker.errors.APIError as cop_exc:
        raise LaunchFailed(500, f"sandbox network repair failed: {cop_exc}") from cop_exc


def _require_disabled_compatibility(container) -> None:
    if _immutable_bindings(container):
        raise LaunchFailed(
            409,
            "configured networking differs from the existing sandbox network mode",
        )
    ports = (container.attrs.get("NetworkSettings") or {}).get("Ports") or {}
    if any(ports.values()):
        raise LaunchFailed(
            409,
            "configured networking differs from the existing sandbox network mode",
        )
    if _current_membership(container):
        raise LaunchFailed(
            409,
            "configured networking differs from the existing sandbox network mode",
        )


def _prepare_existing_container(container, *, mutate: bool) -> None:
    disabled = _network_disabled_on(container)
    if not ENABLE_NETWORK:
        if not disabled:
            raise LaunchFailed(
                409,
                "configured networking differs from the existing sandbox network mode",
            )
        _require_disabled_compatibility(container)
        return
    if disabled:
        raise LaunchFailed(
            409,
            "configured networking differs from the existing sandbox network mode",
        )
    client = get_docker_client()
    network, gateway = _inspect_sandbox_network(client)
    live = (container.status or "").lower() in _LIVE_SANDBOX_STATUSES
    _require_enabled_compatibility(container, network, gateway, live=live)
    if _membership_matches(container, network):
        return
    if live or not mutate:
        raise LaunchFailed(
            409,
            "live sandbox network membership is incompatible; stop the sandbox before migration",
        )
    _repair_stopped_membership(client, container, network)


def _published_address(container, container_port: int) -> Optional[str]:
    """Host-side address of an assigned published container port, if it has one.

    Host networking and empty/wildcard HostIp still resolve to loopback for the
    existing shared-namespace address contract. Concrete gateway bindings are
    returned as reported. Missing or unassigned publications return None.
    """
    if ((container.attrs.get("HostConfig") or {}).get("NetworkMode")) == "host":
        return f"127.0.0.1:{container_port}"

    ports = (container.attrs.get("NetworkSettings") or {}).get("Ports") or {}
    for binding in ports.get(f"{container_port}/tcp") or []:
        host_port = binding.get("HostPort")
        if not host_port:
            continue
        host_ip = binding.get("HostIp") or "127.0.0.1"
        if host_ip in ("0.0.0.0", "::", ""):
            host_ip = "127.0.0.1"
        return f"{host_ip}:{host_port}"
    return None


def get_container_service_address(chat_id: str, container_port: int) -> Optional[str]:
    """Address to reach one of a chat container's services, as "host:port".

    Uses the engine-assigned published host address and port only. Missing
    publication is unavailable; there is no compose-network or container-IP
    fallback, and this function does not mutate membership.
    """
    chat_id = chat_id.lower()
    client = get_docker_client()
    sanitized_id = re.sub(r'[^a-zA-Z0-9_.-]', '-', chat_id)
    container_name = f"owui-chat-{sanitized_id}"
    try:
        c = client.containers.get(container_name)
        c.reload()
        if c.status != "running":
            return None
        return _published_address(c, container_port)
    except Exception:
        return None


def _container_name(chat_id: str) -> str:
    sanitized_id = re.sub(r'[^a-zA-Z0-9_.-]', '-', canonical_lock_chat_id(chat_id))
    return f"owui-chat-{sanitized_id}"


def _reload_container(container):
    container.reload()
    return (container.status or "").lower()


def _lookup_container(chat_id: str):
    try:
        container = get_docker_client().containers.get(_container_name(chat_id))
    except docker.errors.NotFound:
        return None
    _reload_container(container)
    return container


def _get_or_create_container(chat_id: str) -> docker.models.containers.Container:
    """Return a running sandbox, or create one only when nothing exists.

    Stopped, paused, created, restarting, dead, and absent-with-metadata
    raise SandboxStopped. Corrupt metadata fails closed.
    """
    chat_id = canonical_lock_chat_id(chat_id)
    with _combined_lock(chat_id):
        container = _lookup_container(chat_id)
        meta = load_container_meta(chat_id)
        if container is not None:
            if container.status != "running":
                raise SandboxStopped()
            note_running_activity(chat_id, container)
            return container
        if meta is not None:
            raise SandboxStopped()
        print(f"[MCP] Creating new container: {_container_name(chat_id)}")
        container = _create_container(chat_id, _container_name(chat_id))
        note_running_activity(chat_id, container)
        return container


def _create_container(chat_id: str, container_name: str) -> docker.models.containers.Container:
    """Create a new persistent container for this chat."""
    client = get_docker_client()
    credential_source = current_credential_source.get()
    if credential_source == "server":
        gitlab_token = _server_gitlab_token(_server_meta_value(chat_id, "user_email"))
        user_name = _server_meta_value(chat_id, "user_name")
        user_email = _server_meta_value(chat_id, "user_email")
        mcp_servers = _server_meta_value(chat_id, "mcp_servers")
        anthropic_key = ANTHROPIC_AUTH_TOKEN
        anthropic_base = ANTHROPIC_BASE_URL
        request_scoped_anthropic = None
    else:
        gitlab_token = current_gitlab_token.get()
        user_name = current_user_name.get()
        user_email = current_user_email.get()
        mcp_servers = current_mcp_servers.get()
        anthropic_key = current_anthropic_auth_token.get() or ANTHROPIC_AUTH_TOKEN
        anthropic_base = current_anthropic_base_url.get() or ANTHROPIC_BASE_URL
        request_scoped_anthropic = current_anthropic_auth_token.get()

    extra_env = {"GITLAB_HOST": current_gitlab_host.get()}
    if gitlab_token:
        extra_env["GITLAB_TOKEN"] = gitlab_token
        print("[MCP] Injecting GITLAB_TOKEN into container environment")

    # Phase 3 gateway-path injection — only active when SUBAGENT_CLI=claude
    # (AUTH-01: no Anthropic gateway vars bleed into codex/opencode containers).
    if SUBAGENT_CLI == "claude" and anthropic_key:
        extra_env["ANTHROPIC_AUTH_TOKEN"] = anthropic_key
        extra_env["ANTHROPIC_BASE_URL"] = anthropic_base

    # Inject only the active CLI's auth allowlist (AUTH-01 / Pitfall 1).
    for _name, _value in _PASSTHROUGH_BY_CLI[SUBAGENT_CLI]:
        if _value:
            extra_env[_name] = _value

    extra_env["SUBAGENT_CLI"] = SUBAGENT_CLI
    if SUBAGENT_CLI == "opencode":
        extra_env["OPENCODE_CONFIG"] = "/tmp/opencode.json"
        if request_scoped_anthropic:
            extra_env["ANTHROPIC_API_KEY"] = request_scoped_anthropic

    if VISION_API_KEY:
        extra_env["VISION_API_KEY"] = VISION_API_KEY
        extra_env["VISION_API_URL"] = VISION_API_URL
        extra_env["VISION_MODEL"] = VISION_MODEL

    if user_name:
        extra_env["GIT_AUTHOR_NAME"] = user_name
        extra_env["GIT_COMMITTER_NAME"] = user_name
    if user_email:
        extra_env["GIT_AUTHOR_EMAIL"] = user_email
        extra_env["GIT_COMMITTER_EMAIL"] = user_email
        if SUBAGENT_CLI == "claude":
            extra_env["ANTHROPIC_CUSTOM_HEADERS"] = f"x-openwebui-user-email: {user_email}"
    if os.getenv("OCU_SANDBOX_NO_AUTOSTART") == "1":
        extra_env["NO_AUTOSTART"] = "1"
    extra_env.pop("OCU_INTERNAL_TOKEN", None)
    extra_env.pop("MCP_API_KEY", None)
    # Workspace volume for this chat
    workspace_volume = f"chat-{chat_id}-workspace"

    # Host paths for user data
    chat_data_path = os.path.join(USER_DATA_BASE_PATH, chat_id)
    uploads_path = os.path.join(chat_data_path, "uploads")
    outputs_path = os.path.join(chat_data_path, "outputs")

    # Create the per-chat upload/output directories.
    #
    # Done in-process. This used to spawn a throwaway root container to mkdir and chmod 777 on the
    # engine host, which cannot work under rootless Podman: the engine has no root to give, and the
    # attempt fails with "permission denied" — taking container creation down with it, because the
    # bind mount then points at a path that does not exist.
    #
    # The orchestrator mounts this same volume at the same path, so it can simply create them. The
    # 0o777 mode is preserved: the sandbox runs as a different user (assistant) and has to write
    # here. os.chmod is used explicitly because mkdir's mode argument is masked by umask.
    try:
        print(f"[MCP] Creating directories: {uploads_path}, {outputs_path}")
        for path in (chat_data_path, uploads_path, outputs_path):
            os.makedirs(path, exist_ok=True)
            try:
                os.chmod(path, 0o777)
            except PermissionError:
                # Pre-existing directory owned by another uid — tolerable if it is already
                # writable, which is the case when a previous run created it.
                if not os.access(path, os.W_OK):
                    raise
    except Exception as e:
        print(f"[MCP] Warning: Failed to create directories: {e}")

    # Check if using custom image (has entrypoint) or standard image
    use_entrypoint = "computer-use" in DOCKER_IMAGE or "open-computer-use" in DOCKER_IMAGE

    if use_entrypoint:
        # Production: use entrypoint script
        command = ["bash", "-c", "/home/assistant/.entrypoint.sh bash -c 'trap \"exit 0\" SIGTERM SIGINT; tail -f /dev/null & wait $!'"]
        working_dir = "/home/assistant"
        user = "assistant:assistant"
    else:
        # Development/test: simple bash loop
        command = ["bash", "-c", "trap 'exit 0' SIGTERM SIGINT; tail -f /dev/null & wait $!"]
        working_dir = "/root"
        user = None  # Use image default

    config = {
        "image": DOCKER_IMAGE,
        "name": container_name,
        "hostname": f"chat-{chat_id[:8]}",
        "command": command,
        "detach": True,
        "stdin_open": True,
        "tty": True,
        "mem_limit": CONTAINER_MEM_LIMIT,
        "nano_cpus": int(CONTAINER_CPU_LIMIT * 1_000_000_000),
        "working_dir": working_dir,
        "environment": _build_container_env(extra_env),
        "volumes": {
            workspace_volume: {"bind": working_dir, "mode": "rw"},
            uploads_path: {"bind": "/mnt/user-data/uploads", "mode": "ro"},
            outputs_path: {"bind": "/mnt/user-data/outputs", "mode": "rw"},
            **skill_manager.get_skill_mounts(
                skill_manager.get_user_skills_sync(user_email)
            ),
        },
        "labels": {
            "managed-by": "mcp-computer-use-orchestrator",
            "chat-id": chat_id,
            "tool": "computer-use-mcp"
        },
        "security_opt": ["no-new-privileges:true"],
    }

    if user:
        config["user"] = user

    pinned_network_id = None
    if not ENABLE_NETWORK:
        config["network_disabled"] = True
    else:
        network, gateway = _inspect_sandbox_network(client)
        _, pinned_network_id = _network_identity(network)
        config["network"] = pinned_network_id
        config["ports"] = {f"{port}/tcp": (gateway, None) for port in SANDBOX_PUBLISHED_PORTS}

    try:
        container = client.containers.create(**config)
    except docker.errors.NotFound as e:
        if pinned_network_id:
            raise LaunchFailed(500, "inspected sandbox network is unavailable for create") from e
        raise
    except docker.errors.APIError as e:
        if getattr(e, "status_code", None) == 409:
            winner = _lookup_container(chat_id)
            if winner is not None and winner.status == "running":
                _prepare_existing_container(winner, mutate=False)
                print(f"[MCP] Adopting running container after name conflict: {container_name}")
                return winner
            raise SandboxStopped()
        elif "host UTS namespace" in str(e):
            print("[MCP] Engine refuses a per-container hostname; creating without one")
            config.pop("hostname", None)
            container = client.containers.create(**config)
        elif pinned_network_id and getattr(e, "status_code", None) in {404, 400, 500}:
            raise LaunchFailed(500, "inspected sandbox network is unavailable for create") from e
        else:
            raise
    if pinned_network_id:
        _reload_container(container)
        membership = _current_membership(container)
        attached_ids = {_membership_entry_id(data) for data in membership.values()}
        if len(membership) != 1 or attached_ids != {pinned_network_id}:
            raise LaunchFailed(500, "created sandbox is not on the inspected sandbox bridge")
    container.start()

    _reload_container(container)

    print(f"[MCP] Created and started new container: {container_name}")

    save_container_meta(chat_id, user_email, user_name, mcp_servers)
    mark_sleeper_retired(chat_id, container)

    try:
        if mcp_servers:
            mcp_cfg = build_mcp_config(mcp_servers, anthropic_base, user_email or "")
            if mcp_cfg:
                write_cmd = build_mcp_config_write_script(mcp_cfg)
                _execute_bash(container, write_cmd, 15)
                print(f"[MCP] Wrote MCP config on container creation: {mcp_servers}")
    except Exception as e:
        print(f"[MCP] Warning: MCP setup on create failed: {e}")

    # Tier 2 — write /home/assistant/README.md with the rendered system prompt
    # so the model can always recover its environment via `view` regardless of
    # what the client did (or didn't do) with prompts/get and InitializeResult.
    #
    # Safe to call asyncio.run here: _create_container runs inside
    # asyncio.to_thread (see all call sites in mcp_tools.py) → worker thread
    # with no running event loop → no nested-loop error.
    try:
        _, workdir = _get_container_user_and_workdir()
        readme_text = render_system_prompt_sync(chat_id, user_email)
        _write_file_to_container(container, workdir, "README.md", readme_text)
        print(f"[MCP] Wrote {workdir}/README.md ({len(readme_text)} chars)")
    except Exception as e:
        print(f"[MCP] Warning: README.md write failed: {e}")

    # Tier 6 — initial sync of uploaded files into MCP resources registry.
    # Lazy import to avoid circular (mcp_resources → mcp_tools → docker_manager).
    try:
        from mcp_resources import sync_chat_resources_sync
        n = sync_chat_resources_sync(chat_id)
        if n:
            print(f"[MCP] Registered {n} upload resource(s) for chat {chat_id}")
    except Exception as e:
        print(f"[MCP] Warning: MCP resources sync failed: {e}")

    # Pitfall 7 defense — scrub OpenCode auth.json from volume on container
    # creation (handles resurrected containers from previous opencode-auth-login
    # experiments). Best-effort — silent on failure (absence is normal).
    try:
        container.exec_run(
            "rm -f /home/assistant/.local/share/opencode/auth.json",
            user="assistant",
        )
    except Exception:
        pass

    return container


def _write_file_to_container(container, dirpath: str, filename: str, text: str) -> None:
    """
    Write a UTF-8 text file into the container at `dirpath/filename` using
    Docker's put_archive API. Cleaner than `exec cat > file` — no shell
    escaping, no interference from shell initialisation.
    """
    import io, tarfile, time as _t
    data = text.encode("utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=filename)
        info.size = len(data)
        info.mtime = int(_t.time())
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    # put_archive returns False on extraction failure (e.g. dirpath does not
    # exist) and True on success. Without this check the caller logs success
    # even though the file was never written. APIError still propagates as
    # an exception per docker-py docs.
    if not container.put_archive(dirpath, buf.getvalue()):
        raise RuntimeError(
            f"put_archive returned False writing {dirpath}/{filename} "
            f"to container {container.short_id} — target dir may not exist"
        )


def _get_container_user_and_workdir() -> tuple:
    """Get user and workdir based on Docker image type."""
    use_entrypoint = "computer-use" in DOCKER_IMAGE or "open-computer-use" in DOCKER_IMAGE
    if use_entrypoint:
        return "assistant", "/home/assistant"
    else:
        return None, "/root"  # None = use container default

def _reset_shutdown_timer(container, timeout: int = None, now=None):
    """Extend host-owned idle protection. No in-container sleeper is started."""
    chat_id = _chat_id_from_container(container)
    if not chat_id:
        return
    effective = timeout if timeout else CONTAINER_IDLE_TIMEOUT
    with _combined_lock(chat_id):
        extend_idle(chat_id, container, effective, now=time.time() if now is None else now)


def _execute_bash(container, command: str, timeout: int = None) -> dict:
    """Execute bash command in container with timeout."""
    user, workdir = _get_container_user_and_workdir()

    try:
        cmd_timeout = timeout if timeout is not None else COMMAND_TIMEOUT
        shutdown_timeout = max(CONTAINER_IDLE_TIMEOUT, cmd_timeout + 60)
        _reset_shutdown_timer(container, shutdown_timeout, now=time.time())
        timed_command = f"timeout {cmd_timeout} bash -c {shlex.quote(command)}"
        exec_result = container.exec_run(
            cmd=["bash", "-c", timed_command],
            stdout=True,
            stderr=True,
            demux=True,
            workdir=workdir
        )

        stdout_data, stderr_data = exec_result.output if exec_result.output else (b"", b"")
        stdout = stdout_data.decode("utf-8", errors="replace") if stdout_data else ""
        stderr = stderr_data.decode("utf-8", errors="replace") if stderr_data else ""

        output = ""
        if stdout:
            output += stdout
        if stderr:
            if output:
                output += "\n"
            output += stderr

        if exec_result.exit_code == 124:
            output += f"\n[Command timed out after {cmd_timeout} seconds]"

        return {
            "exit_code": exec_result.exit_code,
            "output": output,
            "success": exec_result.exit_code == 0
        }

    except Exception as e:
        return {
            "exit_code": -1,
            "output": f"Execution error: {str(e)}",
            "success": False
        }


# ---------------------------------------------------------------------------
# ADAPT-05 / Phase 5: capture variant of _execute_bash.
#
# _execute_bash returns {output, exit_code, success} where stdout+stderr are
# concatenated. Adapter parse_result(stdout, stderr, returncode) needs them
# separated, so cli_runtime.dispatch uses this helper instead. Same docker
# exec semantics (timeout, shutdown-timer reset, demux=True), different
# return shape.
#
# Returns a SimpleNamespace so callers can do `.stdout`, `.stderr`,
# `.returncode` (matches subprocess.CompletedProcess shape — adapter parsers
# are written against that idiom). SimpleNamespace is imported at the top of
# the module (PEP 8 — do NOT inline the import here).
# ---------------------------------------------------------------------------
def _execute_bash_capture(container, command: str, timeout: int = None):
    """Execute bash in container; return SimpleNamespace(stdout, stderr, returncode).

    Stdout/stderr are kept separate (unlike _execute_bash which concatenates).
    Used by cli_runtime.dispatch to feed adapter.parse_result, which is
    written against the subprocess.CompletedProcess (stdout, stderr,
    returncode) shape.

    SECURITY (Phase 5 threat model T-05-05-01): the `command` argument is
    passed straight to bash -c via shlex.quote — caller is responsible for
    having shlex.quote'd every shell-significant value. cli_runtime.dispatch
    constructs the command from `shlex.quote`'d argv elements; do not call
    this helper with operator-controlled raw strings.
    """
    user, workdir = _get_container_user_and_workdir()
    try:
        cmd_timeout = timeout if timeout is not None else COMMAND_TIMEOUT
        shutdown_timeout = max(CONTAINER_IDLE_TIMEOUT, cmd_timeout + 60)
        _reset_shutdown_timer(container, shutdown_timeout)
        timed_command = f"timeout {cmd_timeout} bash -c {shlex.quote(command)}"

        exec_result = container.exec_run(
            cmd=["bash", "-c", timed_command],
            stdout=True,
            stderr=True,
            demux=True,
            workdir=workdir,
        )

        stdout_data, stderr_data = exec_result.output if exec_result.output else (b"", b"")
        stdout = stdout_data.decode("utf-8", errors="replace") if stdout_data else ""
        stderr = stderr_data.decode("utf-8", errors="replace") if stderr_data else ""

        return SimpleNamespace(
            stdout=stdout,
            stderr=stderr,
            returncode=exec_result.exit_code,
        )
    except Exception as e:
        return SimpleNamespace(
            stdout="",
            stderr=f"Execution error: {str(e)}",
            returncode=-1,
        )


def execute_bash_streaming(container, command: str, timeout: int, on_output_line=None) -> dict:
    """Execute bash in container with streaming output.

    Calls on_output_line(line) for each non-empty output line as it arrives.
    Returns dict with output (full text), exit_code, success.
    """
    user, workdir = _get_container_user_and_workdir()
    try:
        cmd_timeout = timeout - 5 if timeout > 10 else timeout
        shutdown_timeout = max(CONTAINER_IDLE_TIMEOUT, cmd_timeout + 60)
        _reset_shutdown_timer(container, shutdown_timeout)

        timed_command = f"timeout {cmd_timeout} bash -c {shlex.quote(command)}"
        client = container.client

        exec_id = client.api.exec_create(
            container.id,
            ["bash", "-c", timed_command],
            stdout=True,
            stderr=True,
            workdir=workdir,
        )["Id"]

        chunks = []
        remainder = ""
        for chunk in client.api.exec_start(exec_id, stream=True):
            decoded = chunk.decode("utf-8", errors="replace")
            chunks.append(decoded)
            if on_output_line:
                lines = (remainder + decoded).split("\n")
                remainder = lines[-1]
                for line in lines[:-1]:
                    stripped = line.strip()
                    if stripped:
                        on_output_line(stripped[:120])

        if on_output_line and remainder.strip():
            on_output_line(remainder.strip()[:120])

        output = "".join(chunks)
        info = client.api.exec_inspect(exec_id)
        exit_code = info.get("ExitCode") or 0

        if exit_code == 124:
            output += f"\n[Command timed out after {cmd_timeout} seconds]"

        return {"output": output, "exit_code": exit_code, "success": exit_code == 0}

    except Exception as e:
        return {"exit_code": -1, "output": f"Execution error: {str(e)}", "success": False}


def _get_meta_path(chat_id: str) -> Path:
    """Path to .meta.json for this chat on the host filesystem."""
    from security import sanitize_chat_id
    chat_id = sanitize_chat_id(chat_id)
    return BASE_DATA_DIR / chat_id / ".meta.json"


def save_container_meta(chat_id: str, user_email: str, user_name: str,
                        mcp_servers: str):
    """Atomically persist non-secret metadata. Secrets stay server-side."""
    meta = {
        "user_email": user_email or "",
        "user_name": user_name or "",
        "mcp_servers": mcp_servers or "",
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    meta_path = _get_meta_path(chat_id)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = meta_path.with_name(f".meta.json.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    os.replace(temporary, meta_path)
    print(f"[META] Saved metadata: {meta_path}")


def load_container_meta(chat_id: str) -> Optional[dict]:
    """Return metadata, None when absent, or raise MetadataCorrupt."""
    meta_path = _get_meta_path(chat_id)
    if not meta_path.exists():
        return None
    try:
        loaded = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise MetadataCorrupt(f"sandbox metadata is corrupt: {meta_path}") from exc
    if not isinstance(loaded, dict):
        raise MetadataCorrupt(f"sandbox metadata is corrupt: {meta_path}")
    return loaded


def _execute_python_with_stdin(container, script: str, data: str) -> dict:
    """Execute Python script in container with data passed through stdin."""
    import socket as sock_module

    _reset_shutdown_timer(container)
    user, workdir = _get_container_user_and_workdir()

    try:
        exec_create_kwargs = {
            "stdin": True,
            "stdout": True,
            "stderr": True,
            "workdir": workdir,
        }
        if user:
            exec_create_kwargs["user"] = user

        exec_id = container.client.api.exec_create(
            container.id,
            ["timeout", str(COMMAND_TIMEOUT), "python3", "-c", script],
            **exec_create_kwargs
        )['Id']

        sock = container.client.api.exec_start(exec_id, socket=True)

        data_bytes = data.encode('utf-8')

        if hasattr(sock, '_sock'):
            sock._sock.sendall(data_bytes)
            sock._sock.shutdown(sock_module.SHUT_WR)
        else:
            sock.sendall(data_bytes)
            if hasattr(sock, 'shutdown_write'):
                sock.shutdown_write()

        gen = frames_iter(sock, tty=False)
        gen = (demux_adaptor(*frame) for frame in gen)
        stdout_data, stderr_data = consume_socket_output(gen, demux=True)

        sock.close()

        exec_info = container.client.api.exec_inspect(exec_id)
        exit_code = exec_info['ExitCode']

        stdout = stdout_data.decode("utf-8", errors="replace") if stdout_data else ""
        stderr = stderr_data.decode("utf-8", errors="replace") if stderr_data else ""

        output = ""
        if stdout:
            output += stdout
        if stderr:
            if output:
                output += "\n"
            output += stderr

        if exit_code == 124:
            output += f"\n[Command timed out after {COMMAND_TIMEOUT} seconds]"

        return {
            "exit_code": exit_code,
            "output": output,
            "success": exit_code == 0
        }

    except Exception as e:
        return {
            "exit_code": -1,
            "output": f"Execution error: {str(e)}",
            "success": False
        }


def _chat_id_from_container(container) -> Optional[str]:
    labels = {}
    attrs = getattr(container, "attrs", None)
    if isinstance(attrs, dict):
        labels = ((attrs.get("Config") or {}).get("Labels") or {})
    if not labels:
        raw_labels = getattr(container, "labels", None)
        labels = raw_labels if isinstance(raw_labels, dict) else {}
    chat_id = labels.get("chat-id")
    if chat_id:
        return canonical_lock_chat_id(chat_id)
    name = getattr(container, "name", "") or ""
    prefix = "owui-chat-"
    if name.startswith(prefix):
        return name[len(prefix):]
    return None


def _server_meta_value(chat_id: str, key: str) -> str:
    meta = load_container_meta(chat_id) or {}
    return meta.get(key) or ""


def _idle_path(chat_id: str) -> Path:
    return _control_dir(chat_id) / ".idle.json"


def read_idle_state(chat_id: str):
    path = _idle_path(chat_id)
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def write_idle_state(chat_id: str, state: dict) -> None:
    path = _idle_path(chat_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".idle.json.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(state))
    os.replace(temporary, path)


def _container_identity(container) -> str:
    return str(getattr(container, "id", "") or "")


def _fresh_idle(container, now: float, sleeper_retired_for=None, status="running") -> dict:
    identity = _container_identity(container)
    state = {
        "container_id": identity,
        "status": status,
        "observed_at": now,
        "idle_expiry": now + CONTAINER_IDLE_TIMEOUT,
    }
    if sleeper_retired_for:
        state["sleeper_retired_for"] = sleeper_retired_for
    return state


def _preserved_retirement(state, identity):
    if isinstance(state, dict) and state.get("sleeper_retired_for") == identity:
        return identity
    return None


def note_running_activity(chat_id: str, container, now=None) -> None:
    current = time.time() if now is None else now
    identity = _container_identity(container)
    prior = read_idle_state(chat_id)
    write_idle_state(
        chat_id,
        _fresh_idle(container, current, sleeper_retired_for=_preserved_retirement(prior, identity)),
    )


def extend_idle(chat_id: str, container, minimum_seconds: int, now=None) -> None:
    current = time.time() if now is None else now
    state = read_idle_state(chat_id)
    identity = _container_identity(container)
    if not isinstance(state, dict) or state.get("container_id") != identity:
        state = _fresh_idle(container, current, sleeper_retired_for=_preserved_retirement(state, identity))
    else:
        state = dict(state)
        if state.get("sleeper_retired_for") != identity:
            state.pop("sleeper_retired_for", None)
    state["status"] = "running"
    state["observed_at"] = current
    state["idle_expiry"] = max(float(state.get("idle_expiry") or 0), current + minimum_seconds)
    write_idle_state(chat_id, state)


def mark_sleeper_retired(chat_id: str, container, now=None) -> None:
    current = time.time() if now is None else now
    identity = _container_identity(container)
    state = read_idle_state(chat_id)
    if not isinstance(state, dict) or state.get("container_id") != identity:
        state = _fresh_idle(container, current, sleeper_retired_for=identity)
    else:
        state = dict(state)
        state["sleeper_retired_for"] = identity
    write_idle_state(chat_id, state)


def record_heartbeat(chat_id: str, now=None) -> None:
    chat_id = canonical_lock_chat_id(chat_id)
    with _combined_lock(chat_id):
        container = _lookup_container(chat_id)
        if container is None or container.status != "running":
            return
        extend_idle(chat_id, container, CONTAINER_IDLE_TIMEOUT, now=now)

def _migration_verified(state, container) -> bool:
    identity = _container_identity(container)
    return isinstance(state, dict) and state.get("container_id") == identity and state.get("sleeper_retired_for") == identity


def retire_legacy_sleeper(chat_id: str, container) -> None:
    """Terminate a detached pre-upgrade sleeper and bind evidence to this container."""
    chat_id = canonical_lock_chat_id(chat_id)
    script = (
        "bash -lc '"
        "set -e; "
        "exec 9>/tmp/.shutdown-timer-lock; "
        "flock -x 9; "
        "OLD=$(cat /tmp/.shutdown-timer-pid 2>/dev/null || true); "
        "if [ -z \"$OLD\" ]; then exit 0; fi; "
        "pkill -P \"$OLD\" 2>/dev/null || true; "
        "kill \"$OLD\" 2>/dev/null || true; "
        "i=0; "
        "while [ \"$i\" -lt 20 ]; do "
        "if ! kill -0 \"$OLD\" 2>/dev/null; then rm -f /tmp/.shutdown-timer-pid; exit 0; fi; "
        "i=$((i + 1)); "
        "sleep 0.05; "
        "done; "
        "if kill -0 \"$OLD\" 2>/dev/null; then exit 1; fi; "
        "rm -f /tmp/.shutdown-timer-pid"
        "'"
    )
    user, _workdir = _get_container_user_and_workdir()
    try:
        result = container.exec_run(script, user=user)
    except docker.errors.APIError as exc:
        raise MigrationRequired("legacy sleeper retirement failed") from exc
    exit_code = getattr(result, "exit_code", 1)
    if exit_code != 0:
        raise MigrationRequired("legacy sleeper retirement failed")
    mark_sleeper_retired(chat_id, container)


def _ensure_retired_before_reaping(chat_id: str, container, state) -> bool:
    if _migration_verified(state, container):
        return True
    try:
        retire_legacy_sleeper(chat_id, container)
    except MigrationRequired:
        return False
    return _migration_verified(read_idle_state(chat_id), container)


def _wait_until_running(container) -> bool:
    deadline = time.monotonic() + RESTART_WAIT_SECONDS
    remaining = max(1, int(RESTART_WAIT_SECONDS / max(RESTART_POLL_SECONDS, 0.001)))
    while time.monotonic() < deadline and remaining > 0:
        remaining -= 1
        if _reload_container(container) == "running":
            return True
        if container.status in {"dead", "exited"}:
            return False
        time.sleep(min(RESTART_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
    return _reload_container(container) == "running"

def launch_sandbox(chat_id: str, credential_source: str = "server") -> dict:
    """Observe running, or return an explicit failure without deleting anything."""
    chat_id = canonical_lock_chat_id(chat_id)
    token = current_credential_source.set(credential_source)
    try:
        with _combined_lock(chat_id):
            return _launch_locked(chat_id, credential_source)
    finally:
        current_credential_source.reset(token)


def _launch_locked(chat_id: str, credential_source: str) -> dict:
    try:
        meta = load_container_meta(chat_id)
    except MetadataCorrupt:
        raise
    try:
        container = _lookup_container(chat_id)
    except docker.errors.DockerException as exc:
        raise LaunchFailed(500, f"engine lookup failed: {exc}") from exc
    if container is None and meta is None:
        raise NeverCreated()
    if container is None:
        created = _create_container(chat_id, _container_name(chat_id))
        if _reload_container(created) != "running":
            raise LaunchFailed(500, "recreated sandbox is not running")
        return {"state": "running"}

    status = container.status
    state = read_idle_state(chat_id)
    if status == "paused" and not _migration_verified(state, container):
        raise MigrationRequired()
    if status == "running":
        _prepare_existing_container(container, mutate=False)
        if not _migration_verified(state, container):
            try:
                retire_legacy_sleeper(chat_id, container)
            except docker.errors.APIError as exc:
                raise LaunchFailed(500, f"engine refused retirement: {exc}") from exc
        else:
            note_running_activity(chat_id, container)
        return {"state": "running"}
    if status == "paused":
        _prepare_existing_container(container, mutate=False)
        note_running_activity(chat_id, container)
        try:
            container.unpause()
        except docker.errors.APIError as exc:
            raise LaunchFailed(500, f"engine refused unpause: {exc}") from exc
        if _reload_container(container) != "running":
            raise LaunchFailed(500, "unpause did not reach running")
        return {"state": "running"}
    if status in {"exited", "created"}:
        _prepare_existing_container(container, mutate=True)
        try:
            container.start()
        except docker.errors.APIError as exc:
            raise LaunchFailed(500, f"engine refused start: {exc}") from exc
        if _reload_container(container) != "running":
            raise LaunchFailed(500, "start did not reach running")
        mark_sleeper_retired(chat_id, container)
        return {"state": "running"}
    if status == "restarting":
        _prepare_existing_container(container, mutate=False)
        if not _wait_until_running(container):
            raise LaunchFailed(504, "restart readiness timed out")
        try:
            retire_legacy_sleeper(chat_id, container)
        except docker.errors.APIError as exc:
            raise LaunchFailed(500, f"engine refused retirement: {exc}") from exc
        return {"state": "running"}
    if status == "dead":
        raise LaunchFailed(500, "sandbox is dead")
    raise LaunchFailed(500, f"unsupported sandbox state: {status}")


def cli_badge() -> dict:
    from cli_runtime import Cli, resolve_cli, resolve_subagent_model

    cli = resolve_cli()
    try:
        model_id, _display = resolve_subagent_model("", cli)
    except ValueError:
        model_id = None
    return {"cli": cli.value, "default_model": model_id, "supports_cost": cli == Cli.CLAUDE}


def describe_sandbox(chat_id: str) -> dict:
    """Read Docker state and metadata without creating or changing a sandbox."""
    chat_id = canonical_lock_chat_id(chat_id)
    with _combined_lock(chat_id):
        container = _lookup_container(chat_id)
        meta = load_container_meta(chat_id)
        if container is not None and container.status == "running":
            state = "running"
            views = ["files", "browser", "terminal"]
        elif container is None and meta is None:
            state = "never_created"
            views = ["files"]
        else:
            state = "stopped"
            views = ["files"]
        from outputs_broker import OutputsBroker
        revision = OutputsBroker().current_revision(chat_id)
        return {
            "state": state,
            "revision": revision,
            "views": views,
            "cli_badge": cli_badge(),
        }


def _idle_uncertain(state, container, now: float) -> bool:
    timeout, poll = validate_idle_configuration()
    identity = _container_identity(container)
    if not isinstance(state, dict) or state.get("container_id") != identity:
        return True
    if state.get("sleeper_retired_for") != identity:
        return True
    try:
        observed = float(state.get("observed_at"))
        float(state.get("idle_expiry"))
    except (TypeError, ValueError):
        return True
    return now - observed > max(poll, timeout)


def reap_idle(chat_id: str, now=None) -> None:
    """Stop one continuously observed running sandbox, or grant a fresh window."""
    chat_id = canonical_lock_chat_id(chat_id)
    current = time.time() if now is None else now
    with _combined_lock(chat_id):
        container = _lookup_container(chat_id)
        if container is None:
            return
        if container.status == "paused":
            state = read_idle_state(chat_id)
            identity = _container_identity(container)
            if not isinstance(state, dict) or state.get("container_id") != identity:
                state = _fresh_idle(
                    container,
                    current,
                    sleeper_retired_for=_preserved_retirement(state, identity),
                    status="paused",
                )
            else:
                state = dict(state)
            state["status"] = "paused"
            state["observed_at"] = current
            write_idle_state(chat_id, state)
            return
        if container.status != "running":
            return
        state = read_idle_state(chat_id)
        if not _ensure_retired_before_reaping(chat_id, container, state):
            return
        state = read_idle_state(chat_id)
        if isinstance(state, dict) and state.get("status") == "paused":
            note_running_activity(chat_id, container, now=current)
            return
        if _idle_uncertain(state, container, current):
            note_running_activity(chat_id, container, now=current)
            return
        if current < float(state["idle_expiry"]):
            state["observed_at"] = current
            state["status"] = "running"
            write_idle_state(chat_id, state)
            return
        reloaded = _lookup_container(chat_id)
        latest = read_idle_state(chat_id)
        if (
            reloaded is None
            or reloaded.status != "running"
            or not isinstance(latest, dict)
            or latest.get("container_id") != _container_identity(reloaded)
            or latest.get("status") == "paused"
            or _idle_uncertain(latest, reloaded, current)
            or current < float(latest.get("idle_expiry") or 0)
        ):
            return
        latest = read_idle_state(chat_id)
        if latest is None or current < float(latest.get("idle_expiry") or 0):
            return
        reloaded.stop(timeout=10)
        _reload_container(reloaded)


def startup_idle_sweep(now=None) -> None:
    """Grant every managed sandbox a fresh window before any worker may reap."""
    validate_idle_configuration()
    current = time.time() if now is None else now
    if not BASE_DATA_DIR.exists():
        return
    for child in BASE_DATA_DIR.iterdir():
        if not child.is_dir() or not (child / ".meta.json").exists() and not (child / ".idle.json").exists():
            continue
        try:
            chat_id = canonical_lock_chat_id(child.name)
        except Exception:
            continue
        try:
            with _combined_lock(chat_id):
                container = _lookup_container(chat_id)
                if container is not None and container.status == "running":
                    note_running_activity(chat_id, container, now=current)
        except Exception as exc:
            print(f"[IDLE] startup sweep failed for {child.name}: {exc}")


def list_reap_candidates() -> list[str]:
    if not BASE_DATA_DIR.exists():
        return []
    found = []
    for child in BASE_DATA_DIR.iterdir():
        if child.is_dir() and ((child / ".meta.json").exists() or (child / ".idle.json").exists()):
            found.append(child.name)
    return found


def reap_known_sandboxes(now=None) -> None:
    for chat_id in list_reap_candidates():
        try:
            reap_idle(chat_id, now=now)
        except Exception as exc:
            print(f"[IDLE] reap failed for {chat_id}: {exc}")
