# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Office HTTP availability boundary and unknown-route fallback."""
from __future__ import annotations

import json

from fastapi import APIRouter
from starlette.convertors import PathConvertor, register_url_convertor

import docker_manager
from auth_guard import OFFICE_PREFIX

from . import config
from .control_plane import admit_callback, serve_source
from .sessions import close_session, create_session, save_session, session_status

_SUFFIX_CONVERTOR = "ocu_office_suffix"
_CHAT_CONVERTOR = "ocu_office_chat"
_CONTROL_PREFIXES = ("/office/source/", "/office/callback/")


class OfficeSuffixConvertor(PathConvertor):
    """Match every remaining Office suffix character, including newlines."""

    regex = r"[\s\S]*"


class OfficeChatConvertor(PathConvertor):
    """Match a single callback chat segment, including the empty one."""

    regex = r"[^/]*"


class OfficeAvailabilityMiddleware:
    """HTTP-only prefix gate after AuthGuard and before Office dispatch (D7)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        control_plane = path.startswith(_CONTROL_PREFIXES)
        if not path.startswith(OFFICE_PREFIX) and not control_plane:
            await self.app(scope, receive, send)
            return
        if not config.enabled():
            await _json_404(send, "office_disabled")
            return
        if control_plane:
            await self.app(scope, receive, send)
            return
        chat_id = scope["ocu_chat_id"]
        if not (docker_manager.BASE_DATA_DIR / chat_id).is_dir():
            await _json_404(send, "unknown_chat")
            return
        await self.app(scope, receive, send)


class UnknownRoute:
    """Permanent method-independent 404 for unmatched Office paths."""

    async def __call__(self, scope, receive, send):
        await _json_404(send, "unknown_route")


async def _json_404(send, reason: str) -> None:
    body = json.dumps({"reason": reason}, separators=(",", ":")).encode("ascii")
    await send(
        {
            "type": "http.response.start",
            "status": 404,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def create_office_router() -> APIRouter:
    register_url_convertor(_SUFFIX_CONVERTOR, OfficeSuffixConvertor())
    register_url_convertor(_CHAT_CONVERTOR, OfficeChatConvertor())
    router = APIRouter()
    router.add_api_route(
        f"/office/source/{{ticket:{_SUFFIX_CONVERTOR}}}",
        serve_source,
        methods=["GET"],
        include_in_schema=False,
    )
    router.add_api_route(
        f"/office/callback/{{chat_id:{_CHAT_CONVERTOR}}}/{{session_id}}",
        admit_callback,
        methods=["POST"],
        include_in_schema=False,
    )
    router.add_api_route(
        f"{OFFICE_PREFIX}{{chat_id}}/documents/{{file_id}}/sessions",
        create_session,
        methods=["POST"],
        include_in_schema=False,
    )
    router.add_api_route(
        f"{OFFICE_PREFIX}{{chat_id}}/sessions/{{session_id}}",
        session_status,
        methods=["GET"],
        include_in_schema=False,
    )
    router.add_api_route(
        f"{OFFICE_PREFIX}{{chat_id}}/sessions/{{session_id}}/save",
        save_session,
        methods=["POST"],
        include_in_schema=False,
    )
    router.add_api_route(
        f"{OFFICE_PREFIX}{{chat_id}}/sessions/{{session_id}}/close",
        close_session,
        methods=["POST"],
        include_in_schema=False,
    )
    # Concrete Office routes must be registered above this fallback.
    router.add_route(
        f"{OFFICE_PREFIX}{{rest:{_SUFFIX_CONVERTOR}}}",
        UnknownRoute(),
        methods=None,
        include_in_schema=False,
    )
    return router
