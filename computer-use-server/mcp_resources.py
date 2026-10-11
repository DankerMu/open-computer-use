# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""
Native MCP resources for chat workspace files.

Clients (Agents SDK, MCP Inspector, Claude Desktop) can call `resources/list`
to see every visible workspace file across every live chat, and `resources/read`
to fetch content. Upload stays on the existing POST /api/uploads/* HTTP
endpoint (MCP has no upload primitive — community consensus).

Design decisions:

- URI shape: `file://uploads/{chat_id}/{url-encoded-rel-path}`. `chat_id` is
  embedded because clients don't re-send X-Chat-Id on per-resource calls
  (headers ride only on the outer request). URIs must be self-contained.
  `rel_path` is urllib.parse.quote'd because FastMCP's ResourceTemplate
  uses a per-param regex of `[^/]+` (verified at
  .venv/.../mcp/server/fastmcp/resources/templates.py:88), so nested
  paths wouldn't match `{rel}` — we flatten via percent-encoding.

- Dynamic per-chat list: the pinned FastMCP readers copy one dictionary's
  values or perform one lookup, without taking a writer lock. Discovery
  prepares a complete replacement off-loop and rebinds the private
  `ResourceManager._resources` attribute once. Published dictionaries are
  never mutated. The existing mcp version pin guards this private API.

- Called from two places:
    * docker_manager._create_container — initial sync when the chat's
      container spins up, so any already-existing workspace files are visible.
    * app.py:upload_file POST handler — after a new file is saved.

Tenancy: `X-Chat-Id` is untrusted. Any caller holding MCP_API_KEY can guess
URIs and read any chat's workspace files — same model as today's /api/uploads/*
endpoints. Per-chat auth is out of scope.
"""

import asyncio
import threading
import urllib.parse

from mcp.server.fastmcp.resources import FunctionResource  # verified path

from mcp_tools import mcp
from security import sanitize_chat_id
from uploads import DiscoveryError, UploadEntry, list_chat_uploads, read_chat_upload


# MIMEs that should round-trip as text (UTF-8 decoded) rather than as bytes.
# Anything not matching `text/*` or this set is returned as bytes, which
# FastMCP encodes as a base64 blob per the MCP resources spec.
_TEXT_MIMES = frozenset({
    "application/json",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
    "application/javascript",
    "application/x-sh",
})


# Admission is process-wide across request loops and synchronous worker callers.
# Acquire before executor submission; only the admitted worker releases it.
_discovery_admission = threading.Lock()
MAX_WORKSPACE_RESOURCES = 20_000
_chat_uris: dict[str, set[str]] = {}


def _encode(rel_path: str) -> str:
    # safe="" → encode `/` too, so `sub/nested.txt` becomes `sub%2Fnested.txt`.
    return urllib.parse.quote(rel_path, safe="")


def _build_function_resource(chat_id: str, entry: UploadEntry) -> FunctionResource:
    uri = f"file://uploads/{chat_id}/{_encode(entry.rel_path)}"

    # Capture chat_id + rel + mime as defaults so the closure doesn't
    # reference the outer loop variable.
    async def _reader(chat_id: str = chat_id, rel: str = entry.rel_path,
                      mime: str = entry.mime_type) -> str | bytes:
        data, _ = read_chat_upload(chat_id, rel)
        if mime.startswith("text/") or mime in _TEXT_MIMES:
            return data.decode("utf-8", errors="replace")
        return data

    return FunctionResource(
        uri=uri,  # type: ignore[arg-type]  # FastMCP accepts str, validates internally
        name=f"{chat_id}/{entry.name}",
        description=f"Workspace file ({entry.size} bytes, {entry.mime_type})",
        mime_type=entry.mime_type,
        fn=_reader,
    )


def _discover_and_publish(chat_id: str) -> tuple[int, bool]:
    """Run one admitted transaction; readers see the old or new dictionary."""
    global _chat_uris
    try:
        entries = list_chat_uploads(chat_id)
        stale = _chat_uris.get(chat_id, set())
        retained = sum(len(uris) for uris in _chat_uris.values()) - len(stale)
        if retained + len(entries) > MAX_WORKSPACE_RESOURCES:
            raise DiscoveryError("registry_limit")

        registry = mcp._resource_manager._resources.copy()
        for uri in stale:
            registry.pop(uri, None)
        fresh: set[str] = set()
        for entry in entries:
            resource = _build_function_resource(chat_id, entry)
            uri = str(resource.uri)
            registry[uri] = resource
            fresh.add(uri)

        chats = _chat_uris.copy()
        if fresh:
            chats[chat_id] = fresh
        else:
            chats.pop(chat_id, None)
        result = (len(fresh), fresh != stale)

        # All potentially failing preparation precedes publication. SDK readers
        # retain one dictionary; writer bookkeeping is private to this admission.
        mcp._resource_manager._resources = registry
        _chat_uris = chats
        return result
    except DiscoveryError:
        raise
    except Exception:
        raise DiscoveryError("internal_error") from None
    finally:
        _discovery_admission.release()


def _observe_worker_result(future: asyncio.Future) -> None:
    # A cancelled caller can abandon shielded work. Observe its eventual failure
    # without altering the result for active awaiters or changing lock ownership.
    if not future.cancelled():
        future.exception()


async def sync_chat_resources(chat_id: str) -> int:
    """Publish a complete bounded membership snapshot; return its file count.

    Admission is immediate, not an executor backlog. Once submitted, discovery
    owns admission even if its caller cancels before the worker starts.
    Request-scoped list-change notification remains separate from publication.
    """
    chat_id = sanitize_chat_id(chat_id)
    if not _discovery_admission.acquire(blocking=False):
        raise DiscoveryError("busy")
    try:
        future = asyncio.get_running_loop().run_in_executor(
            None, _discover_and_publish, chat_id)
    except Exception:
        _discovery_admission.release()
        raise DiscoveryError("internal_error") from None
    except BaseException:
        _discovery_admission.release()
        raise
    future.add_done_callback(_observe_worker_result)
    count, changed = await asyncio.shield(future)

    if changed:
        try:
            session = mcp._mcp_server.request_context.session
            await session.send_resource_list_changed()
        except LookupError:
            # No request context: polling clients still see the published list.
            pass
        except Exception:
            print("[MCP] resource list_changed notification failed")
    return count


def sync_chat_resources_sync(chat_id: str) -> int:
    """Use the same discovery transaction directly from synchronous callers."""
    chat_id = sanitize_chat_id(chat_id)
    if not _discovery_admission.acquire(blocking=False):
        raise DiscoveryError("busy")
    count, _changed = _discover_and_publish(chat_id)
    return count
