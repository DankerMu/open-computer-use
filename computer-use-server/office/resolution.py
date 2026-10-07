# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Explicit conflict admission; the canonical publisher owns all workspace writes."""
from __future__ import annotations

import asyncio
import errno
import json
import time
import uuid

import docker_manager
from fastapi import Request
from fastapi.responses import JSONResponse
from outputs_broker import OutputsBroker

from . import config, publish, sessions
from .epoch import RestoreEpochError
from .store import StateCorruptError, StateDurabilityError
from .versions import StorageLowError


class ResolveRefusal(Exception):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status, self.reason = status, reason


def _action(body):
    if not body:
        return "save_as"
    try:
        parsed = json.loads(body)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise sessions.InvalidRequestError() from None
    if not isinstance(parsed, dict):
        raise sessions.InvalidRequestError()
    action = parsed.get("action", "save_as")
    if type(action) is not str or action not in ("save_as", "overwrite"):
        raise sessions.InvalidRequestError()
    return action


async def resolve_session(request: Request) -> JSONResponse:
    try:
        action = _action(await request.body())
        resolved = await asyncio.to_thread(
            _resolve_session, request.scope["ocu_chat_id"], request.path_params["session_id"], action,
        )
    except sessions.InvalidRequestError:
        return sessions._error(422, "invalid_request")
    except sessions.UnknownSessionError:
        return sessions._error(404, "unknown_session")
    except ResolveRefusal as extra:
        return sessions._error(extra.status, extra.reason)
    except StorageLowError:
        return sessions._error(503, "storage_low")
    except StateDurabilityError:
        return sessions._error(500, "state_durability")
    except (RestoreEpochError, StateCorruptError):
        return sessions._error(500, "state_corrupt")
    except OSError as extra:
        if extra.errno == errno.ENOSPC:
            return sessions._error(503, "storage_low")
        raise
    return JSONResponse(status_code=200, content=resolved)


def _outcome(result):
    if result.outcome == "published":
        return
    if result.reason in ("path_missing", "workspace_missing"):
        raise ResolveRefusal(409, result.reason)
    if result.reason is None:
        raise StateCorruptError("resolve publication has no outcome reason")
    raise ResolveRefusal(503, result.reason)


def _resolve_session(chat_id, session_id, action):
    chat, store = sessions._load_session(chat_id, session_id)
    broker = OutputsBroker()
    with docker_manager._combined_lock(chat, create=False) as lock:
        if lock is None:
            raise sessions.UnknownSessionError()
        state = store.read(chat)
        record = sessions._require_session(state, session_id)
        sessions._status_projection(record)
        # Epoch refusal still drives old responsibility before orphaning, but
        # grants no new authority in the restored workspace.
        if sessions._maybe_orphan_epoch(store, chat, record, session_id) is not None:
            raise ResolveRefusal(409, "not_in_conflict")
        pending = [key for key, entry in state["journal"].items()
                   if entry.get("requester") == "resolve" and entry.get("session_id") == session_id]
        if len(pending) > 1:
            raise StateCorruptError("session has multiple resolve obligations")
        if pending:
            _outcome(publish.publish(chat, pending[0]))
        else:
            publish.recover_publications(chat)
            state = store.read(chat)
            record = sessions._require_session(state, session_id)
            sessions._status_projection(record)
            if record["state"] != "conflict":
                raise ResolveRefusal(409, "not_in_conflict")
            selected = sessions._active_versions(state, record["file_id"])[-1]
            sequence = record.get("last_committed_seq", 0)
            if sequence < 1:
                raise StateCorruptError("conflict has no committed content sequence")
            store.check_free_space(chat, config.MIN_FREE_BYTES)
            journal_id = uuid.uuid4().hex

            def accept(working):
                current = sessions._require_session(working, session_id)
                publish.add_obligation(working, journal_id, current, selected, sequence, "resolve")
                working["journal"][journal_id].update(action=action, source_sha256=selected["sha256"])
                if sessions._final_receipt(working, session_id) is None:
                    current["last_activity_at"] = time.time()

            store.update(chat, accept)
            _outcome(publish.publish(chat, journal_id))
        state = store.read(chat)
        record = sessions._require_session(state, session_id)
        path = broker.resolve_file_id(chat, record["file_id"])
        return {"session_id": session_id, "state": record["state"], "file_id": record["file_id"], "path": path}
