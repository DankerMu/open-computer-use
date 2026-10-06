# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Final-publication copy transaction using the publisher's private inode witness."""
from __future__ import annotations

import errno
import os
import stat
import sys
import uuid
from pathlib import Path

from uploads import claim_file_no_replace

from . import versions, workspace
from .store import StateCorruptError, _FILE_FLAGS

_MAX_CANDIDATES = 10_000


def validate_intent(binding):
    if (not isinstance(binding, dict) or type(binding.get("schema_version")) is not int
            or binding["schema_version"] != 1 or not isinstance(binding.get("destination"), str)
            or not isinstance(binding.get("basename"), str)
            or type(binding.get("initial_revision")) is not int or binding["initial_revision"] < 0):
        raise StateCorruptError("publish copy binding is invalid")
    try:
        if binding["destination"]:
            workspace._parts(binding["destination"])
        if len(workspace._parts(binding["basename"])) != 1:
            raise workspace.UnsafePathError()
        for field in ("claimed_name", "attempt"):
            if field in binding and len(workspace._parts(binding[field])) != 1:
                raise workspace.UnsafePathError()
    except (TypeError, workspace.UnsafePathError) as extra:
        raise StateCorruptError("publish copy path binding is invalid") from extra
    if "parent_identities" in binding and (
        not isinstance(binding["parent_identities"], list)
        or not binding["parent_identities"]
        or any(not isinstance(pair, list) or len(pair) != 2
               or any(type(value) is not int or value < 0 for value in pair)
               for pair in binding["parent_identities"])
    ):
        raise StateCorruptError("publish copy parent binding is invalid")
    if "registered_file_id" in binding and (
        not isinstance(binding["registered_file_id"], str) or not binding["registered_file_id"]
    ):
        raise StateCorruptError("publish copy registration binding is invalid")


def plan(chat, path, *, identity_missing=False):
    """Select a safe destination using the publisher's sole parent walker."""
    from .publish import _parents, _prepared_parents

    parts = workspace._parts(path)
    root, absent = _prepared_parents(chat, (parts[-1],))
    try:
        if absent is not None:
            return "workspace_missing", None
    finally:
        for fd in reversed(root):
            os.close(fd)
    try:
        held, absent = _prepared_parents(chat, parts)
    except OSError as extra:
        if extra.errno not in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
            raise
        # Distinguish a symlinked parent from other non-directory components.
        prefix = []
        for component in parts[:-1]:
            held = _parents(chat, (*prefix, component))
            try:
                info = os.lstat(component, dir_fd=held[-1])
                if stat.S_ISLNK(info.st_mode):
                    return None, ("", parts[-1])
                if not stat.S_ISDIR(info.st_mode):
                    return None, None
            finally:
                for fd in reversed(held):
                    os.close(fd)
            prefix.append(component)
        return None, None
    try:
        if absent is not None:
            return None, ("", parts[-1])
        if identity_missing:
            return None, ("/".join(parts[:-1]), parts[-1])
        try:
            os.lstat(parts[-1], dir_fd=held[-1])
        except FileNotFoundError:
            return None, ("/".join(parts[:-1]), parts[-1])
        return None, None
    finally:
        for fd in reversed(held):
            os.close(fd)


def apply_successor(state, entry, selected, saved_as):
    new_id, path = saved_as["file_id"], saved_as["path"]
    if new_id in state["documents"] or new_id == entry["file_id"]:
        raise StateCorruptError("publish copy identity already has Office history")
    source = state["documents"][entry["file_id"]]
    state["documents"][new_id] = {
        "file_id": new_id, "type": source["type"], "path": path,
        "published_version": 1, "published_sha256": selected["sha256"],
        "versions": [{
            "number": 1, "parent": None, "sha256": selected["sha256"],
            "size": selected["size"], "source": "conflict",
            "created_at": versions._now(), "published": True,
        }],
    }
    record = state["sessions"][entry["session_id"]]
    record.update(file_id=new_id, saved_as=dict(saved_as),
                  baseline_sha256=selected["sha256"], workspace_changed=False)
    record.pop("last_checked_size", None)
    record.pop("last_checked_mtime_ns", None)


def _join(destination, name):
    return f"{destination}/{name}" if destination else name


def _update(store, chat, journal_id, fence, **fields):
    def bind(state):
        state["journal"][journal_id]["copy"].update(fields)
    fence.checkpoint()
    updated = store.update(chat, bind)["journal"][journal_id]
    fence.checkpoint()
    return updated


def _index(broker, chat):
    # The lock is held; reuse the broker's bounded validated metadata reader,
    # never reconcile the workspace or duplicate its index decoder.
    index = broker._read_index(chat)
    return {} if index is None else index["active"]


def prepare(store, broker, chat, journal_id, entry, fence, destination):
    if "copy" in entry:
        return entry
    directory, basename = destination
    initial_revision = broker.current_revision(chat)
    def intent(state):
        live = state["journal"][journal_id]
        live.update(copy={"schema_version": 1, "destination": directory, "basename": basename,
                          "initial_revision": initial_revision},
                    temporary_name=f".office-publish.{uuid.uuid4().hex}.tmp")
        live.pop("target_path", None)
        live.pop("staging", None)
    fence.checkpoint()
    return store.update(chat, intent)["journal"][journal_id]


def exposed(store, chat, journal_id, entry):
    """Check the live private witness, including a link before helper return."""
    from .publish import RecoveryRequiredError, _Stage, _identity
    if "claimed_name" in entry["copy"]:
        return True
    if "staging" not in entry:
        return False
    stage = _Stage(store, chat, journal_id, entry)
    try:
        identity = stage.inspect()
        if identity is None:
            raise RecoveryRequiredError("publish copy witness is missing at timeout")
        for name in (entry["staging"]["anchor_name"], entry["staging"]["witness_name"]):
            try:
                info = os.lstat(name, dir_fd=stage.directory)
            except FileNotFoundError:
                continue
            if _identity(info) != identity:
                raise RecoveryRequiredError("publish copy witness changed at timeout")
            return info.st_nlink > 2
        raise RecoveryRequiredError("publish copy witness disappeared at timeout")
    finally:
        stage.close()


def drive(store, broker, chat, journal_id, entry, selected, fence, destination):
    """Complete the copy with its witness retained through the atomic Office successor."""
    from .publish import (
        PublishResult, RecoveryRequiredError, _Stage, _finish, _identity,
        _prepared_parents, _record, _remove_owned, _verify_parent_prefix,
    )

    entry = prepare(store, broker, chat, journal_id, entry, fence, destination)
    binding = entry["copy"]
    components = (chat, "outputs", *(workspace._parts(binding["destination"]) if binding["destination"] else ()))
    held, absent = _prepared_parents(chat, (
        *(workspace._parts(binding["destination"]) if binding["destination"] else ()),
        binding["basename"],
    ))
    stage = None
    result = None
    try:
        if absent is not None:
            if absent.prefix == (chat,) and absent.missing == "outputs":
                fence.checkpoint()
                result = _finish(store, chat, journal_id, PublishResult("failed", "workspace_missing"), entry, selected)
                return result
            claimed = "claimed_name" in binding or "registered_file_id" in binding
            if claimed or binding["destination"] == "":
                raise RecoveryRequiredError("publish copy destination is missing")
            stale = held
            held = []
            try:
                fence.checkpoint()
                def rebind(state):
                    live = state["journal"][journal_id]["copy"]
                    live["destination"] = ""
                    live.pop("parent_identities", None)
                    live.pop("attempt", None)
                entry = store.update(chat, rebind)["journal"][journal_id]
                fence.checkpoint()
                binding = entry["copy"]
                components = (chat, "outputs")
                held, absent = _prepared_parents(chat, (binding["basename"],))
            finally:
                for fd in reversed(stale):
                    os.close(fd)
            if absent is not None:
                if absent.prefix == (chat,) and absent.missing == "outputs":
                    fence.checkpoint()
                    result = _finish(store, chat, journal_id, PublishResult("failed", "workspace_missing"), entry, selected)
                    return result
                raise RecoveryRequiredError("publish copy destination is missing")
        parents = [list(_identity(os.fstat(fd))) for fd in held]
        if "parent_identities" not in binding:
            fence.checkpoint()
            entry = _update(store, chat, journal_id, fence, parent_identities=parents)
            binding = entry["copy"]
        elif parents != binding["parent_identities"]:
            raise RecoveryRequiredError("publish copy destination identity changed")
        _verify_parent_prefix(components, held)
        stage = _Stage(store, chat, journal_id, entry)
        identity = stage.inspect()
        content = versions.read_version_bytes(store, chat, selected["sha256"])
        if len(content) != selected["size"]:
            raise StateCorruptError("publish version size differs from its blob")
        fence.checkpoint()
        parent = held[-1]
        claimed = None
        if identity is not None:
            # Only metadata in this one pinned directory is inspected. This also
            # covers the helper's successful link before its return/name journal.
            with os.scandir(parent) as entries:
                for count, item in enumerate(entries):
                    fence.checkpoint()
                    if count >= _MAX_CANDIDATES:
                        raise RecoveryRequiredError("publish copy directory exceeds ownership search bound")
                    info = item.stat(follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode) and _identity(info) == identity:
                        if item.name.startswith(".") or item.name == binding["basename"]:
                            raise RecoveryRequiredError("publish copy has an unexpected shared owner")
                        if claimed is not None:
                            raise RecoveryRequiredError("publish copy has multiple shared owners")
                        claimed = item.name
        if "claimed_name" in binding and claimed != binding["claimed_name"]:
            raise RecoveryRequiredError("publish copy shared ownership changed")
        if claimed is not None and "claimed_name" not in binding:
            indexed = _index(broker, chat).get(_join(binding["destination"], claimed))
            if claimed != binding.get("attempt") or (
                indexed is not None and indexed["revision"] <= binding["initial_revision"]
            ):
                # The helper advanced after an unlocked collision. Its link is
                # proven ours, but not the selected unindexed numbered candidate.
                _verify_parent_prefix(components, held)
                fence.checkpoint()
                if not _remove_owned(parent, claimed, identity):
                    raise RecoveryRequiredError("publish copy claim ownership changed")
                os.fsync(parent)
                claimed = None
        if claimed is None:
            complete = False
            if identity is not None:
                fd = os.open(entry["staging"]["anchor_name"], _FILE_FLAGS, dir_fd=stage.directory)
                try:
                    body, digest = workspace._hash_regular(fd, "private publish copy", max_bytes=broker.max_file_size)
                    complete = digest == selected["sha256"] and len(body) == selected["size"]
                finally:
                    os.close(fd)
            if not complete:
                # Before shared exposure an interrupted allocation has no deletion
                # authority. Fresh private names leave any old allocation harmless.
                def renew(state):
                    live = state["journal"][journal_id]
                    live["temporary_name"] = f".office-publish.{uuid.uuid4().hex}.tmp"
                    live.pop("staging", None)
                fence.checkpoint()
                entry = store.update(chat, renew)["journal"][journal_id]
                stage.entry = entry
                identity = stage.allocate(journal_id, content, fence)
            active = _index(broker, chat)
            requested = Path(binding["basename"])
            for number in range(2, _MAX_CANDIDATES):
                fence.checkpoint()
                name = f"{requested.stem} ({number}){requested.suffix}"
                if _join(binding["destination"], name) in active:
                    continue
                try:
                    os.lstat(name, dir_fd=parent)
                except FileNotFoundError:
                    pass
                else:
                    continue
                entry = _update(store, chat, journal_id, fence, attempt=name)
                _verify_parent_prefix(components, held)
                fence.checkpoint()
                stage.inspect()
                fence.checkpoint()
                claimed = claim_file_no_replace(stage.entry["temporary_name"], src_dir_fd=stage.directory,
                                                dst_dir_fd=parent, requested_name=name)
                fence.checkpoint()
                info = os.lstat(claimed, dir_fd=parent)
                if not stat.S_ISREG(info.st_mode) or _identity(info) != identity:
                    raise RecoveryRequiredError("publish copy claim ownership changed")
                if claimed != name or _join(binding["destination"], claimed) in active:
                    if not _remove_owned(parent, claimed, identity):
                        raise RecoveryRequiredError("publish copy claim ownership changed")
                    os.fsync(parent)
                    claimed = None
                    continue
                break
            if claimed is None:
                raise RecoveryRequiredError("publish copy exhausted numbered names")
        _verify_parent_prefix(components, held)
        fence.checkpoint()
        os.fsync(parent)
        fence.checkpoint()
        entry = _update(store, chat, journal_id, fence, claimed_name=claimed)
        path = _join(binding["destination"], claimed)
        active = _index(broker, chat)
        registered = active.get(path)
        if registered is None:
            fence.checkpoint()
            registered = broker.register_host_write(chat, path)
            fence.checkpoint()
        if (registered["file_id"] == entry["file_id"] or registered["hash"] != selected["sha256"]
                or registered["size"] != selected["size"] or registered["revision"] <= binding["initial_revision"]
                or binding.get("registered_file_id", registered["file_id"]) != registered["file_id"]):
            raise RecoveryRequiredError("publish copy registration changed")
        entry = _update(store, chat, journal_id, fence, registered_file_id=registered["file_id"])
        _verify_parent_prefix(components, held)
        info = os.lstat(claimed, dir_fd=parent)
        if not stat.S_ISREG(info.st_mode) or _identity(info) != identity:
            raise RecoveryRequiredError("publish copy ownership changed before completion")
        body, digest = store.read_workspace_file(chat, path, max_bytes=broker.max_file_size)
        if digest != selected["sha256"] or len(body) != selected["size"]:
            raise RecoveryRequiredError("publish copy content changed before completion")
        fence.checkpoint()
        result = _finish(store, chat, journal_id, PublishResult(
            "published", saved_as={"file_id": registered["file_id"], "path": path},
        ), entry, selected)
        # Completion removed responsibility atomically. No recovery may adopt
        # unbound remnants if cleanup is interrupted here.
        for name in (stage.entry["staging"]["anchor_name"], stage.entry["staging"]["witness_name"]):
            _remove_owned(stage.directory, name, identity)
        os.fsync(stage.directory)
        return result
    finally:
        primary = sys.exc_info()[1]
        error = None
        if stage is not None:
            try:
                stage.close()
            except OSError as extra:
                error = extra
        for fd in reversed(held):
            try:
                os.close(fd)
            except OSError as extra:
                error = error or extra
        if error is not None:
            if primary is None and result is None:
                raise error
            _record("Office publication workspace cleanup failed", chat_id=chat)
