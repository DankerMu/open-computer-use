#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Logical database capture, restore, pruning, and compatibility inspection."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re

import recovery
from recovery import RecoveryError, docker, sha256_file



DUMP_NAME = "openwebui.dump"
ADDITIVE_SCHEMA = "ocu_chat_state"
REVISION_RE = re.compile(r"^[0-9a-f]{12}$")
PG_MAJOR_RE = re.compile(r"(\d+)")
SUPPORTED_DB_ROLES = ("openwebui", "postgres")
SCHEMA_QUERY = (
    "SELECT version_num FROM alembic_version; "
    "SELECT extname FROM pg_extension ORDER BY 1; "
    "SHOW server_version;"
)
CHAT_STATE_QUERY = (
    "SELECT json_build_object("
    "'chat_id', chat_id, "
    "'last_seen_revision', last_seen_revision, "
    "'prefs', COALESCE(prefs, '{}'::json), "
    "'updated_at', updated_at"
    ") FROM ocu_chat_state ORDER BY chat_id;"
)
CHAT_OWNERS_QUERY = (
    "SELECT json_build_object('id', id, 'user_id', COALESCE(user_id, '')) "
    "FROM chat ORDER BY id;"
)
LIVE_CHATS_QUERY = "SELECT id FROM chat ORDER BY id;"

ORPHAN_DELETE = (
    "BEGIN; "
    "DELETE FROM ocu_chat_state s WHERE NOT EXISTS "
    "(SELECT 1 FROM chat c WHERE c.id = s.chat_id); "
    "COMMIT;"
)
EMPTY_DB_QUERY = (
    "SELECT count(*) FROM pg_class c "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema');"
)
MIGRATION_GRAPH_SCRIPT = (
    "from alembic.config import Config\n"
    "from alembic.script import ScriptDirectory\n"
    "script = ScriptDirectory.from_config(Config('/app/backend/open_webui/alembic.ini'))\n"
    "heads = list(script.get_heads())\n"
    "print('HEADS=' + ','.join(heads))\n"
    "for revision in script.walk_revisions():\n"
    "    down = revision.down_revision\n"
    "    if isinstance(down, tuple):\n"
    "        down = ','.join(str(item) for item in down if item)\n"
    "    print('REV=' + revision.revision + '->' + str(down or ''))\n"
)
FORBIDDEN_EXTENSIONS = ("vector", "pgcrypto")


def _safe_detail(result) -> str:
    return f"exit {result.returncode}"


def _exec_postgres(container: str, command: str, *, database: str = "openwebui", read_only: bool = True) -> str:
    env = ["PGOPTIONS=-c default_transaction_read_only=on"] if read_only else []
    argv = ["exec", "-u", "postgres", "-i", container]
    if env:
        argv.extend(["env", *env])
    argv.extend(
        [
            "psql",
            "-h",
            "127.0.0.1",
            "-p",
            "5432",
            "-U",
            "openwebui",
            "-d",
            database,
            "-v",
            "ON_ERROR_STOP=1",
            "-tA",
            "-c",
            command,
        ]
    )
    result = docker(*argv)
    if result.returncode != 0:
        raise RecoveryError(f"postgres inspection failed ({_safe_detail(result)})")
    return result.stdout or ""



def inspect_source_schema(container: str) -> dict:
    raw = _exec_postgres(container, SCHEMA_QUERY, read_only=True)
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        raise RecoveryError("postgres schema inspection returned no rows")
    revision = lines[0]
    if not (REVISION_RE.fullmatch(revision) or revision.replace("_", "").isalnum()):
        raise RecoveryError(f"unrecognized alembic revision {revision}")
    extensions = [line for line in lines[1:-1] if line]
    forbidden = [name for name in extensions if name in FORBIDDEN_EXTENSIONS]
    if forbidden:
        raise RecoveryError("unsupported database extension " + ", ".join(forbidden))
    server = lines[-1]
    return {
        "alembic_revision": revision,
        "extensions": extensions,
        "server_version": server,
        "tool_version": _tool_version(container, "pg_dump"),
    }


def _tool_version(container: str, tool: str) -> str:
    result = docker("exec", "-u", "postgres", container, tool, "--version")
    if result.returncode != 0:
        raise RecoveryError(f"cannot read {tool} version")
    return (result.stdout or "").strip().splitlines()[0]


def capture_database(container: str, dest_dir: Path) -> dict:
    dump = dest_dir / DUMP_NAME
    dest_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(dest_dir, 0o700)
    recovery.write_private_bytes(dump, b"", 0o600)
    result = recovery.docker_stream_to_file(
        (
            "exec",
            "-u",
            "postgres",
            "-i",
            container,
            "pg_dump",
            "-U",
            "openwebui",
            "-d",
            "openwebui",
            "-Fc",
        ),
        dump,
    )
    if result.returncode != 0:
        raise RecoveryError(f"postgres dump failed ({_safe_detail(result)})")
    if dump.stat().st_size == 0:
        raise RecoveryError("postgres dump is empty")
    listing = recovery.docker_stream_from_file(
        ("exec", "-u", "postgres", "-i", container, "pg_restore", "-l"),
        dump,
    )
    if listing.returncode != 0:
        raise RecoveryError("postgres dump listing failed")
    toc = listing.stdout or ""
    for name in FORBIDDEN_EXTENSIONS:
        if f" EXTENSION {name} " in f" {toc} ":
            raise RecoveryError(f"unsupported database extension {name}")
    schema = inspect_source_schema(container)
    return {"path": DUMP_NAME, "sha256": sha256_file(dump), "schema": schema}


def restore_database(container: str, dump: Path) -> None:
    occupancy = _exec_postgres(container, EMPTY_DB_QUERY, read_only=True)
    if occupancy.strip() not in {"", "0"}:
        raise RecoveryError("target database is not empty")
    result = recovery.docker_stream_from_file(
        (
            "exec",
            "-u",
            "postgres",
            "-i",
            container,
            "pg_restore",
            "-U",
            "openwebui",
            "-d",
            "openwebui",
            "--exit-on-error",
            "--single-transaction",
        ),
        dump,
    )
    if result.returncode != 0:
        raise RecoveryError(f"postgres restore failed ({_safe_detail(result)})")



def prune_orphans(container: str) -> list[str]:
    before = _chat_state_rows(container)
    live = set(_live_chat_ids(container))
    orphans = [row["chat_id"] for row in before if row["chat_id"] not in live]
    result = docker(
        "exec",
        "-u",
        "postgres",
        "-i",
        container,
        "psql",
        "-h",
        "127.0.0.1",
        "-p",
        "5432",
        "-U",
        "openwebui",
        "-d",
        "openwebui",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        ORPHAN_DELETE,
    )

    if result.returncode != 0:
        raise RecoveryError("orphan prune failed")
    after = {row["chat_id"]: row for row in _chat_state_rows(container)}
    for row in before:
        if row["chat_id"] in live and after.get(row["chat_id"]) != row:
            raise RecoveryError(f"live chat state changed during prune: {row['chat_id']}")
    for chat_id in orphans:
        if chat_id in after:
            raise RecoveryError(f"orphan chat state {chat_id} was not removed")
    return orphans


def _chat_state_rows(container: str) -> list[dict]:
    raw = _exec_postgres(container, CHAT_STATE_QUERY, read_only=True)
    rows = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as cop:
            raise RecoveryError("malformed ocu_chat_state inspection") from cop
        if not isinstance(payload, dict):
            raise RecoveryError("malformed ocu_chat_state inspection")
        rows.append(
            {
                "chat_id": str(payload.get("chat_id") or ""),
                "last_seen_revision": int(payload.get("last_seen_revision") or 0),
                "prefs": payload.get("prefs") if payload.get("prefs") is not None else {},
                "updated_at": int(payload.get("updated_at") or 0),
            }
        )
        if not rows[-1]["chat_id"]:
            raise RecoveryError("malformed ocu_chat_state inspection")
    return rows


def _live_chat_ids(container: str) -> list[str]:
    raw = _exec_postgres(container, LIVE_CHATS_QUERY, read_only=True)
    return [line.strip() for line in raw.splitlines() if line.strip()]


def inspect_chat_owners(container: str) -> dict[str, str]:
    raw = _exec_postgres(container, CHAT_OWNERS_QUERY, read_only=True)
    owners = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as cop:
            raise RecoveryError("malformed chat ownership inspection") from cop
        if not isinstance(payload, dict) or "id" not in payload:
            raise RecoveryError("malformed chat ownership inspection")
        owners[str(payload["id"])] = str(payload.get("user_id") or "")
    return owners




def inspect_restored_state(container: str) -> dict:
    return {
        "chat_state": _chat_state_rows(container),
        "live_chats": _live_chat_ids(container),
        "owners": inspect_chat_owners(container),
    }



def inspect_provider_config(container: str) -> list[dict]:
    raw = _exec_postgres(
        container,
        "SELECT json_build_object("
        "'key', key, "
        "'value', value, "
        "'updated_at', updated_at"
        ") FROM config ORDER BY key;",
        read_only=True,
    )
    rows = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as cop:
            raise RecoveryError("malformed provider config inspection") from cop
        if not isinstance(payload, dict) or "key" not in payload:
            raise RecoveryError("malformed provider config inspection")
        rows.append(
            {
                "key": str(payload["key"]),
                "value": payload.get("value"),
                "updated_at": payload.get("updated_at"),
            }
        )
    return rows


def inspect_migration_graph(image: str) -> dict:
    result = docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        "0:0",
        "--workdir",
        "/app/backend/open_webui",
        "--entrypoint",
        "/usr/local/bin/python3",
        "--pull",
        "never",
        image,
        "-c",
        MIGRATION_GRAPH_SCRIPT,
    )
    if result.returncode != 0:
        raise RecoveryError("selected WebUI image did not report a migration graph")
    heads: list[str] = []
    revisions: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        if line.startswith("HEADS="):
            heads = [item for item in line.split("=", 1)[1].split(",") if item]
        elif line.startswith("REV="):
            payload = line.split("=", 1)[1]
            revision, _, down = payload.partition("->")
            revisions[revision] = down
    if not revisions:
        raise RecoveryError("selected WebUI image did not report a migration graph")
    return {"heads": heads, "revisions": revisions}


def inspect_postgres_tools(image: str) -> dict[str, int]:
    versions = {}
    for tool in ("pg_dump", "pg_restore", "psql", "postgres"):
        result = docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "0:0",
            "--entrypoint",
            tool,
            "--pull",
            "never",
            image,
            "--version",
        )
        if result.returncode != 0:
            raise RecoveryError(f"cannot inspect {tool} version")
        match = PG_MAJOR_RE.search(result.stdout or "")
        if match is None:
            raise RecoveryError(f"{tool} version is unreadable")
        versions["server" if tool == "postgres" else tool] = int(match.group(1))
    return versions


def require_compatible_tools(source_schema: dict, target_image: str) -> None:
    tools = inspect_postgres_tools(target_image)
    source_version = str(source_schema.get("server_version") or "")
    match = PG_MAJOR_RE.search(source_version)
    if match is None:
        raise RecoveryError("captured PostgreSQL server version is missing")
    source_major = int(match.group(1))
    if tools["server"] < source_major or tools["pg_restore"] < source_major:
        raise RecoveryError(
            f"target PostgreSQL tools {tools['pg_restore']} cannot restore dump from {source_major}"
        )


def require_compatible_revision(source_schema: dict, webui_image: str) -> None:
    revision = str(source_schema.get("alembic_revision") or "")
    if not revision:
        raise RecoveryError("captured alembic revision is missing")
    graph = inspect_migration_graph(webui_image)
    revisions = graph["revisions"]
    heads = set(graph["heads"])
    if revision not in revisions:
        raise RecoveryError(
            f"selected WebUI image does not recognize restored revision {revision}"
        )
    current = revision
    seen: set[str] = set()
    while current and current not in heads:
        if current in seen:
            raise RecoveryError(f"migration graph cycle at {current}")
        seen.add(current)
        nxt = revisions.get(current)
        if not nxt:
            raise RecoveryError(
                f"restored revision {revision} is not an ancestor of selected heads"
            )
        current = nxt
    if current not in heads:
        raise RecoveryError(
            f"restored revision {revision} is not an ancestor of selected heads"
        )


def require_additive_schema(container: str) -> None:
    raw = _exec_postgres(container, "SELECT to_regclass('public.ocu_chat_state');", read_only=True)
    if ADDITIVE_SCHEMA not in raw:
        raise RecoveryError("additive ocu_chat_state table is missing after restore")
