# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""History restore admission; publication and recovery remain publisher-owned."""
from __future__ import annotations

import asyncio
import errno
import json
import os
import stat
import uuid

import docker_manager
from fastapi import Request
from fastapi.responses import JSONResponse
from outputs_broker import FileIdNotFoundError, OutputsBroker, OutputsBrokerError

from . import config, publish, sessions, versions, workspace
from .epoch import RestoreEpochError
from .store import OfficeStore, StateCorruptError, StateDurabilityError


class RestoreRefusal(Exception):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status, self.reason = status, reason


def _number(body):
    try:
        parsed = json.loads(body)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise sessions.InvalidRequestError() from None
    if not isinstance(parsed, dict) or type(parsed.get("number")) is not int:
        raise sessions.InvalidRequestError()
    return parsed["number"]


def _selected(state, file_id, number):
    listed = versions._document_versions(state, file_id)
    if not 1 <= number <= len(listed):
        raise RestoreRefusal(404, "unknown_version")
    return listed[number - 1]


def _admit_path(chat, path, max_bytes):
    # Metadata-only admission: a pause failure must never read workspace bytes.
    held = []
    try:
        parts = workspace._parts(path)
        held = publish._parents(chat, parts)
        target = os.lstat(parts[-1], dir_fd=held[-1])
        if not stat.S_ISREG(target.st_mode) or target.st_size > max_bytes:
            raise RestoreRefusal(503, "unsafe_path")
        publish._revalidate(chat, parts, held, publish._identity(target))
    except (workspace.UnsafePathError, OSError) as extra:
        if isinstance(extra, OSError) and extra.errno not in publish._PATH_ERRORS:
            raise
        reason = "path_missing" if isinstance(extra, FileNotFoundError) else "unsafe_path"
        raise RestoreRefusal(409 if reason == "path_missing" else 503, reason) from extra
    finally:
        for fd in reversed(held):
            os.close(fd)


async def restore_version(request: Request) -> JSONResponse:
    try:
        number = _number(await request.body())
        result = await asyncio.to_thread(
            _restore_version, request.scope["ocu_chat_id"], request.path_params["file_id"], number,
        )
    except sessions.InvalidRequestError:
        return sessions._error(422, "invalid_request")
    except FileIdNotFoundError:
        return sessions._error(404, "unknown_file")
    except RestoreRefusal as extra:
        return sessions._error(extra.status, extra.reason)
    except sessions.DocumentServerUnavailableError:
        return sessions._error(502, "documentserver_unavailable")
    except (publish.RecoveryRequiredError, publish.SandboxStateError):
        return sessions._error(503, "publish_pending")
    except versions.StorageLowError:
        return sessions._error(503, "storage_low")
    except StateDurabilityError:
        return sessions._error(500, "state_durability")
    except (RestoreEpochError, StateCorruptError, OutputsBrokerError):
        return sessions._error(500, "state_corrupt")
    except OSError as extra:
        if extra.errno == errno.ENOSPC:
            return sessions._error(503, "storage_low")
        raise
    return JSONResponse(status_code=200, content=result)


def _restore_version(chat_id, file_id, number):
    chat = docker_manager.canonical_lock_chat_id(chat_id)
    store, broker = OfficeStore(), OutputsBroker()
    store._assert_chat_root_safe(chat, allow_missing=True)
    with docker_manager._combined_lock(chat, create=False) as lock:
        if lock is None:
            raise FileIdNotFoundError("chat control root is unavailable")
        broker.resolve_file_id(chat, file_id)
        state = store.read(chat)
        _selected(state, file_id, number)
        state, _active, _epoch, _orphaned = sessions._reopen_session(store, chat, file_id, state)
        # Reopen recovery can move a session to a successor; never follow it.
        broker.resolve_file_id(chat, file_id)
        if sessions._open_session(state, file_id) is not None:
            raise RestoreRefusal(409, "session_open")
        publish.recover_publications(chat)
        path = broker.resolve_file_id(chat, file_id)
        state = store.read(chat)
        selected = _selected(state, file_id, number)
        if sessions._open_session(state, file_id) is not None:
            raise RestoreRefusal(409, "session_open")
        _admit_path(chat, path, broker.max_file_size)
        store.check_free_space(chat, config.MIN_FREE_BYTES)
        journal_id = uuid.uuid4().hex

        def accept(working):
            working["journal"][journal_id] = {
                "file_id": file_id, "version": selected["number"],
                "session_id": None, "save_seq": None, "requester": "restore",
                "source_sha256": selected["sha256"],
            }

        store.update(chat, accept)
        outcome = publish.publish(chat, journal_id)
        if outcome.outcome != "published":
            reason = outcome.reason or "publish_pending"
            raise RestoreRefusal(409 if reason == "path_missing" else 503, reason)
        document = store.read(chat)["documents"][file_id]
        return {"file_id": file_id, "number": document["published_version"], "published": True}
