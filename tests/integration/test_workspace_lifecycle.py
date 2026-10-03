# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""
Workspace container lifecycle: the orchestrator spawns a real per-chat
container on the first tools/call, labels it correctly, and binds the
expected mount paths.

Regression target: docker_manager.py refactors that drop a label or change a
mount path. Unit tests cover env injection but never actually start a
container, so a mount typo would land in prod.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from conftest import call_mcp


def _list_containers_for_chat(chat_id: str) -> list[dict]:
    try:
        r = subprocess.run(
            ["docker", "ps", "-a",
             "--filter", f"label=chat-id={chat_id}",
             "--format", "{{json .}}"],
            capture_output=True, text=True, check=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"docker ps timed out (>30s) for chat_id={chat_id}")
    return [json.loads(line) for line in r.stdout.splitlines() if line.strip()]


def _inspect(container_id: str) -> dict:
    try:
        r = subprocess.run(
            ["docker", "inspect", container_id],
            capture_output=True, text=True, check=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"docker inspect timed out (>30s) for container={container_id}")
    return json.loads(r.stdout)[0]


def _spawn_workspace(client, chat_id: str) -> dict:
    init = call_mcp(client, chat_id, "initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "integration-test", "version": "0.0.0"},
    })
    assert init["status"] == 200, f"initialize failed: {init['body'][:300]}"
    r = call_mcp(client, chat_id, "tools/call", {
        "name": "bash_tool",
        "arguments": {"command": "true", "description": "spawn"},
    }, req_id=2)
    assert r["status"] == 200, f"tools/call failed: {r['body'][:300]}"
    env = r.get("envelope") or {}
    assert "result" in env, f"tools/call returned JSON-RPC error: {env}"
    assert not env["result"].get("isError"), (
        f"bash_tool reported isError=True: {env['result']}"
    )
    containers = _list_containers_for_chat(chat_id)
    assert len(containers) == 1, (
        f"expected exactly 1 workspace for chat-id={chat_id}, got {len(containers)}: "
        f"{[c.get('Names') for c in containers]}"
    )
    return _inspect(containers[0]["ID"])


def _bash(client, chat_id: str, command: str, req_id: int, description: str) -> dict:
    r = call_mcp(client, chat_id, "tools/call", {
        "name": "bash_tool",
        "arguments": {"command": command, "description": description},
    }, req_id=req_id)
    assert r["status"] == 200, f"tools/call failed: {r['body'][:300]}"
    env = r.get("envelope") or {}
    assert "result" in env, f"tools/call returned JSON-RPC error: {env}"
    return env["result"]


def _exec_as_assistant(container_id: str, command: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", "-u", "assistant", container_id, "bash", "-lc", command],
        capture_output=True, text=True, timeout=30,
    )


def _assert_unified_mounts(info: dict, chat_id: str) -> dict:
    mounts = info["Mounts"]
    dests = {m["Destination"]: m for m in mounts}
    files = dests.get("/mnt/user-data/files")
    assert files is not None, f"missing files mount: {dests.keys()}"
    assert files.get("Type") == "bind"
    assert files.get("RW") is True, f"files must be RW: {files}"
    source = files.get("Source") or ""
    assert source.endswith(f"/{chat_id}/outputs"), f"files source is not chat outputs: {source}"
    user_data = [
        dest for dest in dests
        if dest == "/mnt/user-data" or dest.startswith("/mnt/user-data/")
    ]
    assert user_data == ["/mnt/user-data/files"], f"unexpected user-data mounts: {user_data}"
    home = dests.get("/home/assistant")
    assert home is not None, f"missing private home volume: {dests.keys()}"
    assert home.get("Type") == "volume"
    assert home.get("Name") == f"chat-{chat_id}-workspace"
    assert home.get("RW") is True
    return dests


@pytest.mark.integration
@pytest.mark.timeout(180)
def test_first_tool_call_spawns_labeled_workspace(client, chat_id):
    """After one tools/call, the chat must own exactly one workspace container
    with the prod labels (managed-by, chat-id, tool) all set. Drift here
    breaks the cleanup cron's filter in prod."""
    info = _spawn_workspace(client, chat_id)
    labels = info["Config"]["Labels"] or {}

    assert labels.get("managed-by") == "mcp-computer-use-orchestrator", (
        f"prod label missing: {labels}"
    )
    assert labels.get("chat-id") == chat_id
    assert labels.get("tool") == "computer-use-mcp"


@pytest.mark.integration
@pytest.mark.timeout(180)
def test_workspace_has_user_data_mounts(client, chat_id):
    """Exactly one per-chat user-data bind: outputs -> /mnt/user-data/files RW."""
    info = _spawn_workspace(client, chat_id)
    _assert_unified_mounts(info, chat_id)


@pytest.mark.integration
@pytest.mark.timeout(180)
def test_workspace_image_has_no_legacy_paths_and_root_owned_user_data(client, chat_id):
    info = _spawn_workspace(client, chat_id)
    container_id = info["Id"]
    for path in ("/mnt/user-data/uploads", "/mnt/user-data/outputs"):
        check = _exec_as_assistant(container_id, f"test ! -e {path} && test ! -L {path}")
        assert check.returncode == 0, f"{path} exists as dir or symlink"
    parent = _exec_as_assistant(
        container_id,
        "stat -c '%U %a' /mnt/user-data",
    )
    assert parent.returncode == 0, parent.stderr
    owner, mode = parent.stdout.strip().split()
    assert owner == "root", f"/mnt/user-data owner is {owner}"
    assert mode == "755", f"/mnt/user-data mode is {mode}"


@pytest.mark.integration
@pytest.mark.timeout(180)
def test_sandbox_user_writes_files_and_legacy_paths_fail(client, chat_id, orchestrator):
    info = _spawn_workspace(client, chat_id)
    container_id = info["Id"]
    _assert_unified_mounts(info, chat_id)

    wrote = _bash(
        client, chat_id,
        "printf 'agent-bytes' > /mnt/user-data/files/report.docx",
        req_id=3, description="write workspace file",
    )
    assert not wrote.get("isError"), f"files write failed: {wrote}"

    headers = {"Authorization": f"Bearer {orchestrator['internal_token']}"}
    listed = client.get(f"/api/outputs/{chat_id}", headers=headers)
    assert listed.status_code == 200, listed.text
    paths = {entry["path"] for entry in listed.json()["files"]}
    assert "report.docx" in paths

    uploaded = client.post(
        f"/api/uploads/{chat_id}/nested/brief.docx",
        headers=headers,
        files={"file": ("brief.docx", b"uploaded-bytes")},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["filename"] == "nested/brief.docx"
    edited = _bash(
        client, chat_id,
        "printf 'agent-edited' > /mnt/user-data/files/nested/brief.docx",
        req_id=4, description="edit uploaded nested file",
    )
    assert not edited.get("isError"), f"uploaded nested file not writable: {edited}"
    downloaded = client.get(f"/files/{chat_id}/nested/brief.docx", headers=headers)
    assert downloaded.status_code == 200
    assert downloaded.content == b"agent-edited"

    renamed = _bash(
        client, chat_id,
        "mv /mnt/user-data/files/nested/brief.docx "
        "/mnt/user-data/files/nested/renamed.docx",
        req_id=5, description="rename uploaded nested file",
    )
    assert not renamed.get("isError"), f"nested rename failed: {renamed}"
    renamed_download = client.get(
        f"/files/{chat_id}/nested/renamed.docx", headers=headers)
    assert renamed_download.status_code == 200
    assert renamed_download.content == b"agent-edited"
    missing_original = client.get(
        f"/files/{chat_id}/nested/brief.docx", headers=headers)
    assert missing_original.status_code == 404

    deleted = _bash(
        client, chat_id,
        "rm /mnt/user-data/files/nested/renamed.docx",
        req_id=6, description="delete renamed nested file",
    )
    assert not deleted.get("isError"), f"nested delete failed: {deleted}"
    missing_renamed = client.get(
        f"/files/{chat_id}/nested/renamed.docx", headers=headers)
    assert missing_renamed.status_code == 404

    sibling = _bash(
        client, chat_id,
        "printf 'sibling-bytes' > /mnt/user-data/files/nested/sibling.txt",
        req_id=7, description="create sibling in nested directory",
    )
    assert not sibling.get("isError"), (
        f"nested sibling create failed: {sibling}"
    )
    sibling_download = client.get(
        f"/files/{chat_id}/nested/sibling.txt", headers=headers)
    assert sibling_download.status_code == 200
    assert sibling_download.content == b"sibling-bytes"
    listed_nested = client.get(f"/api/outputs/{chat_id}", headers=headers)
    assert listed_nested.status_code == 200, listed_nested.text
    nested_paths = {entry["path"] for entry in listed_nested.json()["files"]}
    assert "nested/sibling.txt" in nested_paths
    assert "nested/brief.docx" not in nested_paths
    assert "nested/renamed.docx" not in nested_paths

    for path in ("/mnt/user-data/outputs/a.txt", "/mnt/user-data/uploads/a.txt"):
        failed = _exec_as_assistant(container_id, f"printf leftover > {path}")
        assert failed.returncode != 0, f"legacy write to {path} succeeded"
    leftover = _exec_as_assistant(
        container_id,
        "find /mnt/user-data -name a.txt",
    )
    assert leftover.stdout.strip() == "", leftover.stdout
    listed_again = client.get(f"/api/outputs/{chat_id}", headers=headers)
    listed_paths = {entry["path"] for entry in listed_again.json()["files"]}
    assert "a.txt" not in listed_paths


@pytest.mark.integration
@pytest.mark.timeout(180)
def test_private_home_file_is_neither_listed_nor_served(client, chat_id, orchestrator):
    _spawn_workspace(client, chat_id)
    wrote = _bash(
        client, chat_id,
        "printf scratch > /home/assistant/scratch.txt",
        req_id=3, description="write private home file",
    )
    assert not wrote.get("isError"), f"private home write failed: {wrote}"
    headers = {"Authorization": f"Bearer {orchestrator['internal_token']}"}
    listed = client.get(f"/api/outputs/{chat_id}", headers=headers)
    assert listed.status_code == 200, listed.text
    paths = {entry["path"] for entry in listed.json()["files"]}
    assert "scratch.txt" not in paths
    served = client.get(f"/files/{chat_id}/scratch.txt", headers=headers)
    assert served.status_code == 404


