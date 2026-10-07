# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""List one active document's history without refreshing editor or workspace state."""
from __future__ import annotations

import errno

import docker_manager
from fastapi import Request
from fastapi.responses import JSONResponse
from outputs_broker import FileIdNotFoundError, OutputsBroker, OutputsBrokerError

from . import sessions, versions
from .epoch import RestoreEpochError
from .publish import RecoveryRequiredError, SandboxStateError
from .store import OfficeStore, StateCorruptError, StateDurabilityError
from .versions import StorageLowError

_VERSION_FIELDS = ("number", "parent", "source", "sha256", "size", "created_at", "published")


def list_versions(request: Request) -> JSONResponse:
    try:
        result = _list_versions(request.scope["ocu_chat_id"], request.path_params["file_id"])
    except FileIdNotFoundError:
        return sessions._error(404, "unknown_file")
    except (RecoveryRequiredError, SandboxStateError):
        return sessions._error(503, "publish_pending")
    except StorageLowError:
        return sessions._error(503, "storage_low")
    except StateDurabilityError:
        return sessions._error(500, "state_durability")
    except (RestoreEpochError, StateCorruptError, OutputsBrokerError):
        return sessions._error(500, "state_corrupt")
    except OSError as extra:
        if extra.errno == errno.ENOSPC:
            return sessions._error(503, "storage_low")
        return sessions._error(500, "state_corrupt")
    return JSONResponse(status_code=200, content=result)


def _list_versions(chat_id: str, file_id: str) -> dict:
    chat = docker_manager.canonical_lock_chat_id(chat_id)
    store, broker = OfficeStore(), OutputsBroker()
    with docker_manager._combined_lock(chat):
        broker.resolve_file_id(chat, file_id)
        state = store.read(chat)
        record = sessions._open_session(state, file_id)
        if record is not None:
            sessions._status_projection(record)
            recovered = sessions._maybe_orphan_epoch(store, chat, record, record["session_id"])
            if recovered is not None:
                # Accepted save-as recovery can move the session to another identity.
                broker.resolve_file_id(chat, file_id)
                state = store.read(chat)
                record = sessions._open_session(state, file_id)
        listed = versions._document_versions(state, file_id)
        document = state["documents"][file_id]
        published = document.get("published_version")
        if published is not None and (
            type(published) is not int or not 1 <= published <= len(listed)
            or not listed[published - 1]["published"]
        ):
            raise StateCorruptError("office document published version is invalid")
        open_session = None
        if record is not None:
            status = sessions._status_projection(record)
            open_session = {
                "session_id": status["session_id"],
                "state": status["state"],
                "reason": status["reason"],
                "editor_ended": sessions._final_receipt(state, record["session_id"]) is not None,
            }
        return {
            "file_id": file_id,
            "published_version": published,
            "open_session": open_session,
            "versions": [{field: item[field] for field in _VERSION_FIELDS} for item in listed],
        }
