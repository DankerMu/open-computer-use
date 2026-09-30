#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Build, import, and verify a six-role offline image release."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile


FORMAT_VERSION = 1
PLATFORM = "linux/amd64"
HEX_LEN = 64
SHA_LEN = 40
ROLE_ORDER = (
    "workspace",
    "computer-use-server",
    "retention-guard",
    "proxy",
    "open-webui",
    "postgres",
)
RUNTIME_IMAGE_VARS = {
    "workspace": "DOCKER_IMAGE",
    "computer-use-server": "COMPUTER_USE_SERVER_IMAGE",
    "retention-guard": "RETENTION_GUARD_IMAGE",
    "proxy": "OCU_PROXY_IMAGE",
    "open-webui": "OPENWEBUI_IMAGE",
    "postgres": "POSTGRES_IMAGE",
}
SERVICE_IMAGE_VARS = {
    "workspace": "DOCKER_IMAGE",
    "computer-use-server": "COMPUTER_USE_SERVER_IMAGE",
    "retention-guard": "RETENTION_GUARD_IMAGE",
    "proxy": "OCU_PROXY_IMAGE",
    "open-webui": "OPENWEBUI_IMAGE",
    "postgres": "POSTGRES_IMAGE",
    "open-webui-init": "OPENWEBUI_IMAGE",
}
SOURCE_BIND_PATHS = (
    "deploy/production-like-test/init/run-init.sh",
    "openwebui/init.sh",
    "openwebui/tools/computer_use_tools.py",
    "openwebui/functions/computer_link_filter.py",
)
INVENTORY_REQUIRED = (
    "format_version",
    "platform",
    "ocu_source_sha",
    "webui_source_sha",
    "source_bundle",
    "images",
)
IMAGE_REQUIRED = (
    "reference",
    "configuration_digest",
    "archive",
    "build",
)
IMAGE_OPTIONAL = ("registry_digests",)

ARCHIVE_REQUIRED = ("path", "sha256")
BUILD_REQUIRED = (
    "dockerfile",
    "dockerfile_sha256",
    "context",
    "arguments",
    "argument_defaults",
    "argument_overrides",
    "input_manifest_sha256",
    "materials",
)
SOURCE_BUNDLE_REQUIRED = ("path", "sha256")
SECRET_BUILD_ARG_MARKERS = ("SECRET", "TOKEN", "PASSWORD", "CREDENTIAL", "API_KEY")
POSTGRES_DEFAULT = "postgres:17-alpine"
WEBUI_BUILD_HASH_ARG = "BUILD_HASH"
PYODIDE_PREPARE_RELATIVE = "scripts/prepare-pyodide.js"
PYODIDE_SUPPLEMENT_RELATIVE = "scripts/pyodide-supplement.json"
PYODIDE_PACKAGE_LOCK_RELATIVE = "package-lock.json"
DRAWIO_PREPARE_RELATIVE = "computer-use-server/drawio/prepare_drawio.py"
DRAWIO_INVENTORY_RELATIVE = "computer-use-server/drawio/inventory.json"
HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_WHEEL_NAME_RE = re.compile(r"^[A-Za-z0-9._+-]+$")
PINNED_ASSIGNMENT_RE = re.compile(r"^([A-Z][A-Z0-9_]*)\s*=\s*([\"'])([^\"']+)\2\s*$")
OCI_METADATA_NAMES = {
    "index.json",
    "oci-layout",
}


ROLE_SPECS = {
    "workspace": {
        "source": "ocu",
        "context": ".",
        "dockerfile": "Dockerfile",
        "image_kind": "build",
    },
    "computer-use-server": {
        "source": "ocu",
        "context": "computer-use-server",
        "dockerfile": "computer-use-server/Dockerfile",
        "image_kind": "build",
    },
    "retention-guard": {
        "source": "ocu",
        "context": "deploy/production-like-test/retention",
        "dockerfile": "deploy/production-like-test/retention/Dockerfile",
        "image_kind": "build",
    },
    "proxy": {
        "source": "ocu",
        "context": "deploy/proxy",
        "dockerfile": "deploy/proxy/Dockerfile",
        "image_kind": "build",
    },
    "open-webui": {
        "source": "webui",
        "context": ".",
        "dockerfile": "Dockerfile",
        "image_kind": "build",
    },
    "postgres": {
        "source": "ocu",
        "context": ".",
        "dockerfile": "docker-compose.webui.yml",
        "image_kind": "pull",
    },
}


class ReleaseError(Exception):
    """Operator-visible release, import, or verification failure."""


def fail(message: str, code: int = 1) -> None:
    print(f"release: {message}", file=sys.stderr)
    raise SystemExit(code)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def is_hex(value: str, length: int) -> bool:
    return len(value) == length and all(
        ch in "0123456789abcdef" for ch in value.lower()
    )


def docker(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args],
        cwd=None if cwd is None else str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def git(
    *args: str, cwd: Path, text: bool = True, input: bytes | str | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=text,
        input=input,
        check=False,
    )


def require_command(result: subprocess.CompletedProcess, action: str) -> str:
    if result.returncode != 0:
        detail = result.stderr or result.stdout or b""
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", "replace")
        detail = detail.strip() or f"exit {result.returncode}"
        raise ReleaseError(f"{action} failed: {detail}")
    stdout = result.stdout or ""
    if isinstance(stdout, bytes):
        return stdout.decode("utf-8")
    return stdout


def load_json_object(path: Path) -> dict:
    decoder = json.JSONDecoder(object_pairs_hook=_reject_duplicate_keys)
    text = path.read_text(encoding="utf-8")
    payload, index = decoder.raw_decode(text)
    if index != len(text.rstrip()):
        trailing = text[index:].strip()
        if trailing:
            raise ReleaseError(f"{path}: inventory contains trailing data")
    if not isinstance(payload, dict):
        raise ReleaseError(f"{path}: inventory must be a JSON object")
    return payload


def _reject_duplicate_keys(pairs):
    payload = {}
    for key, value in pairs:
        if key in payload:
            raise ReleaseError(f"duplicate JSON key {key!r}")
        payload[key] = value
    return payload


def full_commit(cwd: Path) -> str:
    result = git("rev-parse", "HEAD", cwd=cwd)
    sha = require_command(result, f"read commit in {cwd}").strip().lower()
    if not is_hex(sha, SHA_LEN):
        raise ReleaseError(f"{cwd}: HEAD is not a full commit")
    return sha


def require_clean_head(cwd: Path) -> str:
    sha = full_commit(cwd)
    result = git("status", "--porcelain", cwd=cwd)
    require_command(result, f"inspect worktree {cwd}")
    dirty = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        path = line[3:]
        if path.startswith("?? "):
            path = path[3:]
        if line[:2] != "??":
            dirty.append(path or line)
    if dirty:
        raise ReleaseError(
            f"{cwd}: tracked source is dirty; build from a committed snapshot"
        )
    return sha


def snapshot_commit(source: Path, dest: Path) -> str:
    sha = require_clean_head(source)
    result = git(
        "clone", "--quiet", "--no-local", "--", str(source), str(dest), cwd=source
    )
    require_command(result, f"snapshot {source}")
    cloned = require_clean_head(dest)
    if cloned != sha:
        raise ReleaseError(f"{source}: snapshot HEAD is {cloned}, expected {sha}")
    result = git("checkout", "--quiet", sha, cwd=dest)
    require_command(result, f"checkout {sha} in snapshot")
    return sha


def checkout_tracked_tree(source: Path, dest: Path, commit: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    listed = git("ls-tree", "-z", "-r", commit, cwd=source)
    require_command(listed, f"list committed tree {commit}")
    entries = []
    object_ids = []
    for item in listed.stdout.split("\0"):
        if not item:
            continue
        meta, relative = item.split("\t", 1)
        mode, kind, object_id = meta.split(" ", 2)
        if kind == "commit":
            raise ReleaseError(
                f"{relative}: gitlink submodules are not supported as build inputs"
            )
        if kind != "blob":
            raise ReleaseError(f"{relative}: unsupported committed object {kind}")
        if ".." in Path(relative).parts or relative.startswith("/"):
            raise ReleaseError(f"{relative}: unsafe committed path")
        if mode not in {"100644", "100755", "120000"}:
            raise ReleaseError(f"{relative}: unsupported committed file mode {mode}")
        entries.append((mode, object_id, relative))
        object_ids.append(object_id)
    if not entries:
        return
    query = "".join(f"{object_id}\n" for object_id in object_ids).encode("utf-8")
    batch = git("cat-file", "--batch", cwd=source, text=False, input=query)
    if batch.returncode != 0:
        require_command(batch, f"read committed blobs {commit}")
    payload = batch.stdout
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    offset = 0
    blobs = {}
    while offset < len(payload):
        newline = payload.find(b"\n", offset)
        if newline < 0:
            raise ReleaseError(f"{commit}: incomplete committed blob batch")
        header = payload[offset:newline].decode("utf-8")
        offset = newline + 1
        parts = header.split(" ")
        if len(parts) < 3 or parts[1] == "missing":
            raise ReleaseError(f"{commit}: committed blob is missing")
        object_id, _kind, size_text = parts[0], parts[1], parts[2]
        size = int(size_text)
        data = payload[offset : offset + size]
        if len(data) != size:
            raise ReleaseError(f"{commit}: incomplete committed blob {object_id}")
        blobs[object_id] = data
        offset += size
        if offset < len(payload) and payload[offset : offset + 1] == b"\n":
            offset += 1
    for mode, object_id, relative in entries:
        data = blobs.get(object_id)
        if data is None:
            raise ReleaseError(f"{relative} is missing from commit {commit}")
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == "120000":
            target.symlink_to(data.decode("utf-8"))
            continue
        target.write_bytes(data)
        target.chmod(0o755 if mode == "100755" else 0o644)


def confined_relative(root: Path, path: Path) -> str:
    resolved_root = root.resolve()
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ReleaseError(f"{path}: path escapes release root {root}") from exc
    text = relative.as_posix()
    if not text or text.startswith("/") or ".." in relative.parts:
        raise ReleaseError(f"{path}: unsafe relative path")
    return text


def require_regular_file(path: Path, *, follow: bool = False) -> None:
    if not path.exists():
        raise ReleaseError(f"{path}: missing")
    if path.is_symlink():
        raise ReleaseError(f"{path}: symbolic links are not allowed")
    if not path.is_file():
        raise ReleaseError(f"{path}: not a regular file")
    if follow:
        return
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise ReleaseError(f"{path}: not a confined regular file")


def copy_regular(source: Path, dest: Path) -> None:
    require_regular_file(source)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as incoming, dest.open("wb") as outgoing:
        shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
    dest.chmod(stat.S_IRUSR | stat.S_IWUSR)


def require_tracked_regular_file(root: Path, relative: str) -> Path:
    path = root / relative
    if not path.exists():
        raise ReleaseError(f"{relative}: missing required build input")
    if path.is_symlink() or path.is_dir():
        raise ReleaseError(f"{relative}: required build input must be a regular file")
    require_regular_file(path)
    return path


def parse_arg_declarations(dockerfile: Path) -> dict[str, str | None]:
    require_regular_file(dockerfile)
    declared: dict[str, str | None] = {}
    for raw in dockerfile.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.startswith("ARG "):
            continue
        body = line[4:].strip()
        if not body:
            continue
        if "=" in body:
            name, value = body.split("=", 1)
            name = name.strip()
            if not name:
                continue
            declared[name] = value.strip().strip('"').strip("'")
            continue
        name = body.strip()
        if name and name not in declared:
            declared[name] = None
    return declared


def secret_argument(name: str) -> bool:
    tokens = f"_{name.upper()}_"
    return any(f"_{marker}_" in tokens for marker in SECRET_BUILD_ARG_MARKERS)


def reject_secret_arguments(arguments: dict[str, str]) -> None:
    for name in arguments:
        if secret_argument(name):
            raise ReleaseError(f"secret build argument {name} is not allowed")


def split_arguments(
    defaults: dict[str, str], requested: dict[str, str]
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    reject_secret_arguments(requested)
    arguments = dict(defaults)
    overrides = {}
    for name, value in requested.items():
        arguments[name] = value
        overrides[name] = value
    return arguments, dict(defaults), overrides


def resolve_role_arguments(
    *,
    role: str,
    declared: dict[str, str | None],
    requested: dict[str, str],
    source_sha: str,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    reject_secret_arguments(requested)
    unknown = sorted(name for name in requested if name not in declared)
    if unknown:
        raise ReleaseError(f"{role}: unknown build argument {', '.join(unknown)}")
    defaults = {name: value for name, value in declared.items() if value is not None}
    arguments = dict(defaults)
    overrides = dict(requested)
    for name, value in requested.items():
        arguments[name] = value
    if role == "open-webui":
        if WEBUI_BUILD_HASH_ARG not in declared:
            raise ReleaseError(
                f"{role}: Dockerfile must declare {WEBUI_BUILD_HASH_ARG}"
            )
        requested_hash = requested.get(WEBUI_BUILD_HASH_ARG)
        if requested_hash is not None and requested_hash != source_sha:
            raise ReleaseError(
                f"{role}: {WEBUI_BUILD_HASH_ARG} must equal selected source SHA {source_sha}"
            )
        arguments[WEBUI_BUILD_HASH_ARG] = source_sha
    return arguments, defaults, overrides


def tracked_input_entries(
    root: Path, relative_paths: list[str]
) -> list[tuple[str, str, str]]:
    entries = []
    seen = set()
    for relative in sorted(set(relative_paths)):
        if relative in seen:
            continue
        seen.add(relative)
        path = root / relative
        if path.is_symlink():
            mode = path.lstat().st_mode
            target = os.readlink(path)
            digest = sha256_bytes(target.encode("utf-8"))
            entries.append(
                (relative.replace("\\", "/"), f"symlink:{mode & 0o777:03o}", digest)
            )
            continue
        if path.is_dir() or not path.is_file():
            raise ReleaseError(
                f"{relative}: tracked build input must be a regular file"
            )
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise ReleaseError(
                f"{relative}: tracked build input must be a confined regular file"
            )
        entries.append(
            (relative.replace("\\", "/"), f"{mode & 0o777:03o}", sha256_file(path))
        )
    return entries


def input_manifest_hash_from_entries(entries: list[tuple[str, str, str]]) -> str:
    digest = hashlib.sha256()
    digest.update(b"ocu-tracked-inputs-v1\n")
    for relative, mode, content_sha in entries:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(mode.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content_sha.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def load_required_json_object(path: Path, label: str) -> dict:
    require_regular_file(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"{label} is malformed") from exc
    if not isinstance(payload, dict):
        raise ReleaseError(f"{label} must be an object")
    return payload


def extract_pinned_assignments(source: Path) -> dict[str, str]:
    require_regular_file(source)
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    except SyntaxError as exc:
        raise ReleaseError(
            f"{source.name}: Draw.io preparation script is malformed"
        ) from exc
    assignments: dict[str, str] = {}
    for node in tree.body:
        if (
            not isinstance(node, ast.Assign)
            or len(node.targets) != 1
            or not isinstance(node.targets[0], ast.Name)
        ):
            continue
        name = node.targets[0].id
        if name not in {
            "PINNED_COMMIT",
            "PINNED_ARCHIVE_SHA256",
            "PINNED_VIEWER_SHA256",
        }:
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            if name in assignments:
                raise ReleaseError(f"{source.name}: duplicate pin {name}")
            assignments[name] = value.value
            continue
        raise ReleaseError(f"{source.name}: unsupported pin definition {name}")
    return assignments


def pyodide_runtime_version(webui_root: Path) -> str:
    prepare = require_tracked_regular_file(webui_root, PYODIDE_PREPARE_RELATIVE)
    text = prepare.read_text(encoding="utf-8")
    match = re.search(r"distPackage\.version !== '([^']+)'", text)
    if not match:
        raise ReleaseError(
            "Pyodide preparation script does not declare a runtime version"
        )
    expected = match.group(1)
    lock_path = require_tracked_regular_file(webui_root, PYODIDE_PACKAGE_LOCK_RELATIVE)
    lock = load_required_json_object(lock_path, "Pyodide package lock")
    packages = lock.get("packages")
    if not isinstance(packages, dict):
        raise ReleaseError("Pyodide package lock is missing packages")
    record = packages.get("node_modules/pyodide")
    if (
        not isinstance(record, dict)
        or not isinstance(record.get("version"), str)
        or not record["version"]
    ):
        raise ReleaseError("Pyodide runtime version is missing from selected source")
    version = record["version"]
    if version != expected:
        raise ReleaseError(f"Pyodide runtime version is {version}, expected {expected}")
    return version


def pyodide_supplement_materials(webui_root: Path) -> list[dict]:
    path = require_tracked_regular_file(webui_root, PYODIDE_SUPPLEMENT_RELATIVE)
    payload = load_required_json_object(path, "Pyodide supplement")
    packages = payload.get("packages")
    if not isinstance(packages, list) or not packages:
        raise ReleaseError("Pyodide supplement must list packages")
    materials = []
    seen = set()
    for entry in packages:
        if not isinstance(entry, dict):
            raise ReleaseError("Pyodide supplement entry is incomplete")
        name = entry.get("name")
        version = entry.get("version")
        file_name = entry.get("file_name")
        digest = entry.get("sha256")
        url = entry.get("url")
        depends = entry.get("depends")
        imports = entry.get("imports")
        if (
            not isinstance(name, str)
            or not isinstance(version, str)
            or not isinstance(file_name, str)
        ):
            raise ReleaseError("Pyodide supplement entry is incomplete")
        if not isinstance(digest, str) or not isinstance(url, str):
            raise ReleaseError("Pyodide supplement entry is incomplete")
        if not name or not version or not file_name or not digest or not url:
            raise ReleaseError("Pyodide supplement entry is incomplete")
        if not isinstance(depends, list) or not isinstance(imports, list):
            raise ReleaseError(
                f"Pyodide supplement {name} must declare depends and imports"
            )
        if not SAFE_WHEEL_NAME_RE.match(file_name) or ".." in file_name:
            raise ReleaseError(f"unsafe Pyodide wheel name {file_name}")
        if not HEX_SHA256_RE.match(digest):
            raise ReleaseError(f"invalid SHA256 for {name}")
        key = re.sub(r"[-_.]+", "-", name.lower())
        if key in seen:
            raise ReleaseError(f"duplicate Pyodide supplement package {name}")
        seen.add(key)
        materials.append(
            {
                "name": name,
                "requested": version,
                "kind": "pyodide-wheel",
                "sha256": digest.lower(),
                "file_name": file_name,
            }
        )
    materials.append(
        {
            "name": "pyodide-runtime",
            "requested": pyodide_runtime_version(webui_root),
            "kind": "pyodide-runtime",
        }
    )
    return materials


def drawio_materials(ocu_root: Path) -> list[dict]:
    prepare = require_tracked_regular_file(ocu_root, DRAWIO_PREPARE_RELATIVE)
    inventory_path = require_tracked_regular_file(ocu_root, DRAWIO_INVENTORY_RELATIVE)
    pins = extract_pinned_assignments(prepare)
    commit = pins.get("PINNED_COMMIT")
    archive_sha = pins.get("PINNED_ARCHIVE_SHA256")
    viewer_sha = pins.get("PINNED_VIEWER_SHA256")
    if (
        not isinstance(commit, str)
        or not isinstance(archive_sha, str)
        or not isinstance(viewer_sha, str)
    ):
        raise ReleaseError(
            "Draw.io preparation script does not declare a complete source pin"
        )
    if (
        not is_hex(commit, SHA_LEN)
        or not HEX_SHA256_RE.match(archive_sha)
        or not HEX_SHA256_RE.match(viewer_sha)
    ):
        raise ReleaseError(
            "Draw.io preparation script does not declare a complete source pin"
        )
    payload = load_required_json_object(inventory_path, "Draw.io inventory")
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise ReleaseError("Draw.io inventory is empty")
    seen = set()
    viewer = None
    has_license = False
    for entry in files:
        if not isinstance(entry, dict):
            raise ReleaseError("Draw.io inventory entry is incomplete")
        path = entry.get("path")
        digest = entry.get("sha256")
        size = entry.get("size")
        if not isinstance(path, str) or not path or not isinstance(digest, str):
            raise ReleaseError("Draw.io inventory entry is incomplete")
        if type(size) is not int or size < 0:
            raise ReleaseError("Draw.io inventory entry is incomplete")
        if not HEX_SHA256_RE.match(digest):
            raise ReleaseError("Draw.io inventory entry is incomplete")
        if path.startswith("/") or ".." in Path(path).parts:
            raise ReleaseError(f"unsafe Draw.io inventory path {path}")
        if path in seen:
            raise ReleaseError(f"duplicate Draw.io inventory path {path}")
        seen.add(path)
        if path == "LICENSE":
            has_license = True
        if path == "js/viewer-static.min.js":
            viewer = digest.lower()
    if not has_license:
        raise ReleaseError("Draw.io inventory is missing LICENSE")
    if viewer != viewer_sha.lower():
        raise ReleaseError(
            "Draw.io inventory viewer hash does not match the pinned release"
        )
    return [
        {
            "name": "drawio",
            "requested": commit,
            "kind": "pinned-archive",
            "archive_sha256": archive_sha.lower(),
            "viewer_sha256": viewer_sha.lower(),
            "inventory_sha256": sha256_file(inventory_path),
        }
    ]


def workspace_materials(arguments: dict[str, str]) -> list[dict]:
    mapping = (
        ("claude-code", "CLAUDE_CODE_VERSION"),
        ("codex", "CODEX_VERSION"),
        ("opencode", "OPENCODE_VERSION"),
        ("gsd", "GSD_REF"),
        ("superpowers", "SUPERPOWERS_REF"),
    )
    return [
        {"name": name, "requested": arguments.get(arg, ""), "kind": "build-arg"}
        for name, arg in mapping
        if arg in arguments
    ]


def material_versions(
    role: str,
    arguments: dict[str, str],
    *,
    ocu_root: Path | None = None,
    webui_root: Path | None = None,
) -> list[dict]:
    if role == "workspace":
        return workspace_materials(arguments)
    if role == "computer-use-server":
        if ocu_root is None:
            raise ReleaseError(
                "OCU source checkout is required to record Draw.io provenance"
            )
        return drawio_materials(ocu_root)
    if role == "open-webui":
        if webui_root is None:
            raise ReleaseError(
                "WebUI source checkout is required to record Pyodide provenance"
            )
        materials = pyodide_supplement_materials(webui_root)
        if "USE_EMBEDDING_MODEL" in arguments:
            materials.append(
                {
                    "name": "embedding-model",
                    "requested": arguments.get("USE_EMBEDDING_MODEL", ""),
                    "kind": "build-arg",
                }
            )
        return materials
    if role == "postgres":
        if "POSTGRES_IMAGE" not in arguments:
            raise ReleaseError("postgres image declaration is missing")
        return [
            {
                "name": "postgres",
                "requested": arguments["POSTGRES_IMAGE"],
                "kind": "upstream-image",
            }
        ]
    return []


def inspect_image(reference: str) -> dict:
    result = docker("image", "inspect", reference)
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        lowered = detail.lower()
        if "no such image" in lowered or "not found" in lowered:
            raise ReleaseError(f"missing image {reference}")
        raise ReleaseError(
            f"inspect {reference} failed: {detail or f'exit {result.returncode}'}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"inspect {reference} returned malformed JSON") from exc
    if isinstance(payload, list):
        if not payload:
            raise ReleaseError(f"missing image {reference}")
        payload = payload[0]
    if not isinstance(payload, dict):
        raise ReleaseError(f"inspect {reference} returned an unexpected payload")
    return payload


def image_identity(payload: dict) -> tuple[str, str]:
    image_id = str(payload.get("Id") or "")
    if not image_id.startswith("sha256:") or not is_hex(image_id[7:], HEX_LEN):
        raise ReleaseError("image configuration digest is missing")
    os_name = str(payload.get("Os") or payload.get("os") or "")
    architecture = str(payload.get("Architecture") or payload.get("architecture") or "")
    config = payload.get("Config") or {}
    if not os_name and isinstance(config, dict):
        os_name = str(config.get("Os") or config.get("os") or "")
    if not architecture and isinstance(config, dict):
        architecture = str(
            config.get("Architecture") or config.get("architecture") or ""
        )
    platform = f"{os_name}/{architecture}"
    if platform != PLATFORM:
        raise ReleaseError(f"image platform is {platform}, expected {PLATFORM}")
    return image_id, platform


def content_reference(role: str, image_id: str) -> str:
    digest = image_id[7:19]
    if role == "workspace":
        return f"open-computer-use:{digest}"
    return f"ocu-{role}:{digest}"


def workspace_name_ok(reference: str) -> bool:
    return "open-computer-use" in reference


def build_image(
    *, context: Path, dockerfile: Path, tag: str, arguments: dict[str, str]
) -> None:
    argv = [
        "build",
        "--platform",
        PLATFORM,
        "-t",
        tag,
        "-f",
        str(dockerfile),
    ]
    for name, value in arguments.items():
        argv.extend(["--build-arg", f"{name}={value}"])
    argv.append(str(context))
    require_command(docker(*argv, cwd=context), f"build {tag}")


def pull_image(reference: str) -> None:
    require_command(
        docker("pull", "--platform", PLATFORM, reference), f"pull {reference}"
    )


def tag_image(source: str, target: str) -> None:
    require_command(docker("tag", source, target), f"tag {source} as {target}")


def save_image(reference: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    require_command(docker("save", "-o", str(dest), reference), f"save {reference}")


def load_image(archive: Path) -> None:
    require_command(docker("load", "-i", str(archive)), f"load {archive}")


def archive_relative(role: str) -> str:
    return f"images/{role}.tar"


def write_inventory(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def validate_inventory_schema(payload: dict) -> None:
    missing = [name for name in INVENTORY_REQUIRED if name not in payload]
    if missing:
        raise ReleaseError(f"inventory is missing {', '.join(missing)}")
    extra = [name for name in payload if name not in INVENTORY_REQUIRED]
    if extra:
        raise ReleaseError(
            f"inventory has unexpected fields {', '.join(sorted(extra))}"
        )
    if payload.get("format_version") != FORMAT_VERSION:
        raise ReleaseError("inventory format_version is unsupported")
    if payload.get("platform") != PLATFORM:
        raise ReleaseError(f"inventory platform must be {PLATFORM}")
    ocu_sha = str(payload.get("ocu_source_sha") or "")
    webui_sha = str(payload.get("webui_source_sha") or "")
    if not is_hex(ocu_sha, SHA_LEN):
        raise ReleaseError("ocu_source_sha must be a full commit")
    if not is_hex(webui_sha, SHA_LEN):
        raise ReleaseError("webui_source_sha must be a full commit")
    source_bundle = payload.get("source_bundle")
    if not isinstance(source_bundle, dict):
        raise ReleaseError("source_bundle must be an object")
    if sorted(source_bundle) != sorted(SOURCE_BUNDLE_REQUIRED):
        raise ReleaseError("source_bundle fields are incomplete")
    if not is_hex(str(source_bundle.get("sha256") or ""), HEX_LEN):
        raise ReleaseError("source_bundle checksum is not a SHA-256 digest")
    images = payload.get("images")
    if not isinstance(images, dict):
        raise ReleaseError("images must be an object")
    if set(images) != set(ROLE_ORDER) or len(images) != len(ROLE_ORDER):
        raise ReleaseError("images must contain the exact six roles")

    seen_refs: dict[str, str] = {}
    for role, record in images.items():
        if not isinstance(record, dict):
            raise ReleaseError(f"{role}: image record must be an object")
        missing_fields = [name for name in IMAGE_REQUIRED if name not in record]
        if missing_fields:
            raise ReleaseError(f"{role}: missing {', '.join(missing_fields)}")
        extra_fields = [
            name for name in record if name not in IMAGE_REQUIRED + IMAGE_OPTIONAL
        ]
        if extra_fields:
            raise ReleaseError(
                f"{role}: unexpected fields {', '.join(sorted(extra_fields))}"
            )
        if "registry_digests" in record:
            digests = record["registry_digests"]
            if not isinstance(digests, list) or any(
                not isinstance(item, str) for item in digests
            ):
                raise ReleaseError(
                    f"{role}: registry_digests must be a list of strings"
                )

        reference = str(record.get("reference") or "")
        if not reference or "@sha256:" in reference:
            raise ReleaseError(
                f"{role}: named reference is required and must not be a registry digest"
            )
        if role == "workspace" and not workspace_name_ok(reference):
            raise ReleaseError(
                "workspace reference must preserve the open-computer-use name"
            )
        digest = str(record.get("configuration_digest") or "")
        if not digest.startswith("sha256:") or not is_hex(digest[7:], HEX_LEN):
            raise ReleaseError(f"{role}: configuration_digest is not an image ID")
        if reference in seen_refs and seen_refs[reference] != digest:
            raise ReleaseError(f"conflicting reference {reference}")
        seen_refs[reference] = digest
        archive = record.get("archive")
        if not isinstance(archive, dict) or sorted(archive) != sorted(ARCHIVE_REQUIRED):
            raise ReleaseError(f"{role}: archive fields are incomplete")
        if not is_hex(str(archive.get("sha256") or ""), HEX_LEN):
            raise ReleaseError(f"{role}: archive checksum is not a SHA-256 digest")
        build = record.get("build")
        if not isinstance(build, dict):
            raise ReleaseError(f"{role}: build provenance must be an object")
        missing_build = [name for name in BUILD_REQUIRED if name not in build]
        if missing_build:
            raise ReleaseError(
                f"{role}: missing build fields {', '.join(missing_build)}"
            )
        extra_build = [name for name in build if name not in BUILD_REQUIRED]
        if extra_build:
            raise ReleaseError(
                f"{role}: unexpected build fields {', '.join(sorted(extra_build))}"
            )
        if not is_hex(str(build.get("dockerfile_sha256") or ""), HEX_LEN):
            raise ReleaseError(f"{role}: dockerfile hash is not a SHA-256 digest")
        if not is_hex(str(build.get("input_manifest_sha256") or ""), HEX_LEN):
            raise ReleaseError(f"{role}: input manifest hash is not a SHA-256 digest")
        arguments = build.get("arguments")
        if not isinstance(arguments, dict):
            raise ReleaseError(f"{role}: build arguments must be an object")
        for name in arguments:
            if secret_argument(str(name)):
                raise ReleaseError(
                    f"{role}: secret build argument {name} is not allowed"
                )
        materials = build.get("materials")
        if not isinstance(materials, list):
            raise ReleaseError(f"{role}: materials must be a list")
        for item in materials:
            if (
                not isinstance(item, dict)
                or "name" not in item
                or "requested" not in item
            ):
                raise ReleaseError(
                    f"{role}: material entries need name and requested version"
                )
            if "installed" in item:
                raise ReleaseError(
                    f"{role}: material input hashes are not installed package versions"
                )


def relative_archive_path(root: Path, value: str) -> Path:
    if (
        not value
        or value.startswith("/")
        or Path(value).is_absolute()
        or ".." in Path(value).parts
    ):
        raise ReleaseError(f"unsafe archive path {value}")
    path = root
    for part in Path(value).parts:
        path = path / part
        if path.is_symlink():
            raise ReleaseError(f"{path}: symbolic links are not allowed")
    confined_relative(root, path)
    return path


def inspect_archive_bytes(archive: Path) -> dict[str, dict]:
    require_regular_file(archive)
    mappings: dict[str, dict] = {}
    with tarfile.open(archive, "r") as bundle:
        members = bundle.getmembers()
        names = []
        seen = set()
        for member in members:
            name = member.name[2:] if member.name.startswith("./") else member.name
            if not name or name.startswith("/") or ".." in Path(name).parts:
                raise ReleaseError(f"{archive}: unsafe archive member {member.name}")
            if name in seen:
                raise ReleaseError(f"{archive}: duplicate archive member {name}")
            seen.add(name)
            names.append(name)
            if member.issym() or member.islnk():
                raise ReleaseError(f"{archive}: archive member {name} is a link")
        metadata = [
            name
            for name in names
            if name in OCI_METADATA_NAMES or name.startswith("blobs/")
        ]
        if metadata and "manifest.json" not in names:
            raise ReleaseError(
                f"{archive}: unsupported OCI import metadata {', '.join(sorted(metadata))}"
            )
        if "manifest.json" not in names:
            raise ReleaseError(f"{archive}: missing manifest.json")
        extracted = bundle.extractfile("manifest.json")
        if extracted is None:
            raise ReleaseError(f"{archive}: manifest.json is not a regular file")
        with extracted:
            try:
                manifest = json.loads(extracted.read().decode("utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ReleaseError(
                    f"{archive}: manifest.json is not valid JSON"
                ) from exc
        if not isinstance(manifest, list):
            raise ReleaseError(f"{archive}: manifest.json must be a list")
        configs: dict[str, bytes] = {}
        for entry in manifest:
            if not isinstance(entry, dict):
                raise ReleaseError(f"{archive}: manifest entry is not an object")
            config_name = str(entry.get("Config") or "")
            if not config_name:
                raise ReleaseError(f"{archive}: manifest entry is missing Config")
            config_member = bundle.extractfile(config_name)
            if config_member is None:
                raise ReleaseError(f"{archive}: missing configuration {config_name}")
            with config_member:
                config_bytes = config_member.read()
            configs[config_name] = config_bytes
            digest = "sha256:" + sha256_bytes(config_bytes)
            try:
                config_payload = json.loads(config_bytes.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ReleaseError(
                    f"{archive}: configuration {config_name} is not JSON"
                ) from exc
            os_name = str(config_payload.get("os") or "")
            architecture = str(config_payload.get("architecture") or "")
            if not os_name or not architecture:
                raise ReleaseError(
                    f"{archive}: configuration {config_name} is missing platform metadata"
                )
            tags = entry.get("RepoTags")
            if tags is None:
                tags = []
            if not isinstance(tags, list):
                raise ReleaseError(f"{archive}: RepoTags must be a list")
            for tag in tags:
                if not isinstance(tag, str) or not tag:
                    raise ReleaseError(f"{archive}: empty imported reference")
                if tag in mappings and mappings[tag]["digest"] != digest:
                    raise ReleaseError(
                        f"{archive}: conflicting imported reference {tag}"
                    )
                mappings[tag] = {
                    "digest": digest,
                    "platform": f"{os_name}/{architecture}",
                    "config": config_bytes,
                }
        if "repositories" in names:
            extracted = bundle.extractfile("repositories")
            if extracted is None:
                raise ReleaseError(f"{archive}: repositories is not a regular file")
            with extracted:
                try:
                    repositories = json.loads(extracted.read().decode("utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise ReleaseError(
                        f"{archive}: repositories is not valid JSON"
                    ) from exc
            if not isinstance(repositories, dict):
                raise ReleaseError(f"{archive}: repositories must be an object")
            for name, tags in repositories.items():
                if not isinstance(tags, dict):
                    raise ReleaseError(
                        f"{archive}: repositories[{name}] must be an object"
                    )
                for label, value in tags.items():
                    tag = f"{name}:{label}"
                    digest_value = str(value)
                    if not digest_value.startswith("sha256:"):
                        digest_value = (
                            "sha256:" + digest_value
                            if is_hex(digest_value, HEX_LEN)
                            else digest_value
                        )
                    if tag in mappings:
                        if mappings[tag]["digest"] != digest_value and mappings[tag][
                            "digest"
                        ][7:] != str(value):
                            raise ReleaseError(
                                f"{archive}: repositories conflict for {tag}"
                            )
                    else:
                        raise ReleaseError(
                            f"{archive}: repositories names undeclared reference {tag}"
                        )
    return mappings


def existing_reference(reference: str) -> dict | None:
    result = docker("image", "inspect", reference)
    if result.returncode != 0:
        detail = (result.stderr or "").strip().lower()
        if "no such image" in detail or "not found" in detail:
            return None
        raise ReleaseError(
            f"inspect existing {reference} failed: {(result.stderr or '').strip()}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ReleaseError(
            f"inspect existing {reference} returned malformed JSON"
        ) from exc
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not isinstance(payload, dict):
        return None
    return payload


def destination_occupied(dest: Path) -> bool:
    try:
        dest.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ReleaseError(f"cannot inspect destination {dest}: {exc}") from exc
    return True


def acquire_exclusive(dest: Path) -> Path:
    lock_path = dest.with_name(dest.name + ".publish.lock")
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError as cop:
        raise ReleaseError(f"publication already in progress for {dest}") from cop
    os.close(fd)
    return lock_path


def release_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def publish_exclusive(
    stage_dir: Path, dest: Path, lock_path: Path | None = None
) -> None:
    if destination_occupied(dest):
        raise ReleaseError(f"destination already exists: {dest}")
    owned_lock = lock_path is None
    if lock_path is None:
        lock_path = acquire_exclusive(dest)
    stamp = f"{os.getpid()}-{os.urandom(4).hex()}"
    staged = dest.with_name(f"{dest.name}.next-{stamp}")
    committed = False
    try:
        if destination_occupied(staged):
            raise ReleaseError(f"staging path already exists: {staged}")
        os.rename(stage_dir, staged)
        if destination_occupied(dest):
            raise ReleaseError(f"destination already exists: {dest}")
        os.rename(staged, dest)
        committed = True
    except BaseException:
        if not committed and destination_occupied(staged):
            shutil.rmtree(staged, ignore_errors=True)
        raise
    finally:
        if owned_lock:
            release_lock(lock_path)
        if stage_dir.exists():
            shutil.rmtree(stage_dir, ignore_errors=True)


def role_source_root(role: str, ocu_root: Path, webui_root: Path | None) -> Path:
    spec = ROLE_SPECS.get(role)
    if spec is None:
        raise ReleaseError(f"unknown role {role}")
    if spec["source"] == "webui":
        if webui_root is None:
            raise ReleaseError("WebUI source checkout is required to build open-webui")
        return webui_root
    return ocu_root


def postgres_declared_default(ocu_root: Path) -> str:
    compose = require_tracked_regular_file(
        ocu_root, ROLE_SPECS["postgres"]["dockerfile"]
    )
    in_services = False
    in_postgres = False
    service_indent = None
    for raw in compose.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        stripped = raw.strip()
        if indent == 0 and stripped == "services:":
            in_services = True
            in_postgres = False
            continue
        if in_services and indent == 0:
            in_services = False
            in_postgres = False
        if in_services and indent > 0 and stripped == "postgres:":
            in_postgres = True
            service_indent = indent
            continue
        if (
            in_postgres
            and indent <= (service_indent or 0)
            and stripped.endswith(":")
            and not stripped.startswith("image:")
        ):
            in_postgres = False
        if in_postgres and stripped.startswith("image:"):
            image = stripped.split(":", 1)[1].strip().strip('"').strip("'")
            if not image or "${" in image:
                raise ReleaseError("postgres image declaration is missing")
            return image
    raise ReleaseError("postgres image declaration is missing")


def committed_context_relatives(source_root: Path, context_relative: str) -> list[str]:
    relatives = []
    prefix = "" if context_relative in {"", "."} else context_relative.rstrip("/") + "/"
    for path in source_root.rglob("*"):
        relative = path.relative_to(source_root).as_posix()
        if prefix and not relative.startswith(prefix):
            continue
        local = relative[len(prefix) :] if prefix else relative
        if not local or local == ".":
            continue
        if path.is_dir() and not path.is_symlink():
            continue
        relatives.append(local)
    return relatives


def plan_role(
    *,
    role: str,
    ocu_root: Path,
    webui_root: Path | None,
    postgres_image: str | None,
    requested_args: dict[str, str],
    source_sha: str,
) -> dict:
    spec = ROLE_SPECS.get(role)
    if spec is None:
        raise ReleaseError(f"unknown role {role}")
    source_root = role_source_root(role, ocu_root, webui_root)
    dockerfile = require_tracked_regular_file(source_root, spec["dockerfile"])
    context = source_root / spec["context"]
    if spec["context"] != "." and not context.is_dir():
        raise ReleaseError(f"{role}: missing build context {spec['context']}")
    if role == "postgres":
        declared_image = postgres_declared_default(ocu_root)
        requested = {} if postgres_image is None else {"POSTGRES_IMAGE": postgres_image}
        arguments, argument_defaults, overrides = split_arguments(
            {"POSTGRES_IMAGE": declared_image}, requested
        )
        if requested_args:
            raise ReleaseError("postgres does not accept Dockerfile build arguments")
        materials = material_versions(
            role, arguments, ocu_root=ocu_root, webui_root=webui_root
        )
        entries = tracked_input_entries(source_root, [spec["dockerfile"]])
        return {
            "role": role,
            "source_root": source_root,
            "context": context,
            "dockerfile": dockerfile,
            "arguments": arguments,
            "argument_defaults": argument_defaults,
            "argument_overrides": overrides,
            "materials": materials,
            "input_manifest_sha256": input_manifest_hash_from_entries(entries),
            "image_kind": spec["image_kind"],
            "temp_tag": arguments["POSTGRES_IMAGE"],
        }
    declared = parse_arg_declarations(dockerfile)
    arguments, argument_defaults, overrides = resolve_role_arguments(
        role=role,
        declared=declared,
        requested=requested_args,
        source_sha=source_sha,
    )
    materials = material_versions(
        role, arguments, ocu_root=ocu_root, webui_root=webui_root
    )
    entries = tracked_input_entries(
        context, committed_context_relatives(source_root, spec["context"])
    )
    return {
        "role": role,
        "source_root": source_root,
        "context": context,
        "dockerfile": dockerfile,
        "arguments": arguments,
        "argument_defaults": argument_defaults,
        "argument_overrides": overrides,
        "materials": materials,
        "input_manifest_sha256": input_manifest_hash_from_entries(entries),
        "image_kind": spec["image_kind"],
        "temp_tag": f"ocu-build-{role}-{os.getpid()}",
    }


def export_built_image(*, role: str, plan: dict, archive_dir: Path) -> dict:
    temp_tag = plan["temp_tag"]
    if plan["image_kind"] == "pull":
        pull_image(temp_tag)
    else:
        build_image(
            context=plan["context"],
            dockerfile=plan["dockerfile"],
            tag=temp_tag,
            arguments=plan["arguments"],
        )
    payload = inspect_image(temp_tag)
    image_id, _platform = image_identity(payload)
    reference = content_reference(role, image_id)
    if reference != temp_tag:
        tag_image(temp_tag, reference)
    archive_path = archive_dir / f"{role}.tar"
    save_image(reference, archive_path)
    repo_digests = payload.get("RepoDigests") or []
    if not isinstance(repo_digests, list):
        repo_digests = []
    dockerfile = plan["dockerfile"]
    source_root = plan["source_root"]
    context = plan["context"]
    record = {
        "reference": reference,
        "configuration_digest": image_id,
        "archive": {
            "path": f"images/{role}.tar",
            "sha256": sha256_file(archive_path),
        },
        "build": {
            "dockerfile": str(dockerfile.relative_to(source_root)),
            "dockerfile_sha256": sha256_file(dockerfile),
            "context": str(context.relative_to(source_root))
            if context != source_root
            else ".",
            "arguments": plan["arguments"],
            "argument_defaults": plan["argument_defaults"],
            "argument_overrides": plan["argument_overrides"],
            "input_manifest_sha256": plan["input_manifest_sha256"],
            "materials": plan["materials"],
        },
    }
    if repo_digests:
        record["registry_digests"] = [str(item) for item in repo_digests]
    return record


def build_release(
    *,
    ocu_source: Path,
    webui_source: Path,
    destination: Path,
    postgres_image: str | None,
    build_args: dict[str, dict[str, str]] | None = None,
) -> dict:
    if destination.exists():
        raise ReleaseError(f"destination already exists: {destination}")
    requested = build_args or {}
    unknown_roles = sorted(name for name in requested if name not in ROLE_ORDER)
    if unknown_roles:
        raise ReleaseError(f"unknown role {', '.join(unknown_roles)}")
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    lock_path = acquire_exclusive(destination)
    stage = Path(tempfile.mkdtemp(prefix=f"{destination.name}.stage-", dir=str(parent)))
    snapshots = Path(tempfile.mkdtemp(prefix="ocu-release-src-", dir=str(parent)))
    try:
        ocu_snap = snapshots / "ocu"
        webui_snap = snapshots / "webui"
        ocu_sha = snapshot_commit(ocu_source, ocu_snap)
        webui_sha = snapshot_commit(webui_source, webui_snap)
        ocu_tree = snapshots / "ocu-tree"
        webui_tree = snapshots / "webui-tree"
        checkout_tracked_tree(ocu_snap, ocu_tree, ocu_sha)
        checkout_tracked_tree(webui_snap, webui_tree, webui_sha)
        plans = {}
        for role in ROLE_ORDER:
            source_sha = webui_sha if ROLE_SPECS[role]["source"] == "webui" else ocu_sha
            plans[role] = plan_role(
                role=role,
                ocu_root=ocu_tree,
                webui_root=webui_tree,
                postgres_image=postgres_image,
                requested_args=requested.get(role, {}),
                source_sha=source_sha,
            )
        images_dir = stage / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        images = {}
        for role in ROLE_ORDER:
            images[role] = export_built_image(
                role=role, plan=plans[role], archive_dir=images_dir
            )
        source_bundle = create_source_bundle(ocu_snap, stage / "source.bundle")
        payload = {
            "format_version": FORMAT_VERSION,
            "platform": PLATFORM,
            "ocu_source_sha": ocu_sha,
            "webui_source_sha": webui_sha,
            "source_bundle": source_bundle,
            "images": images,
        }
        validate_inventory_schema(payload)
        write_inventory(stage / "release.json", payload)
        publish_exclusive(stage, destination, lock_path)
        return payload
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(snapshots, ignore_errors=True)
        release_lock(lock_path)


def create_source_bundle(ocu_root: Path, dest: Path) -> dict:
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = git("bundle", "create", str(dest), "HEAD", cwd=ocu_root)
    require_command(result, "create source bundle")
    return {"path": "source.bundle", "sha256": sha256_file(dest)}


def reconstruct_source(bundle: Path, dest: Path) -> str:
    if dest.exists():
        shutil.rmtree(dest)
    result = git("clone", "--quiet", str(bundle), str(dest), cwd=dest.parent)
    require_command(result, f"clone source bundle into {dest}")
    return full_commit(dest)


def tracked_paths(root: Path) -> list[str]:
    result = git("ls-files", "-z", cwd=root)
    require_command(result, f"list tracked files in {root}")
    return [item for item in result.stdout.split("\0") if item]


def file_bytes_at_commit(root: Path, commit: str, relative: str) -> bytes:
    result = git("show", f"{commit}:{relative}", cwd=root, text=False)
    if result.returncode != 0:
        raise ReleaseError(f"{relative} is missing from commit {commit}")
    return result.stdout


def verify_tracked_source(root: Path, commit: str) -> None:
    actual = full_commit(root)
    if actual.lower() != commit.lower():
        raise ReleaseError(f"source checkout HEAD is {actual}, expected {commit}")
    result = git("diff-index", "--name-only", commit, "--", cwd=root)
    require_command(result, "compare tracked source to the bundled commit")
    changed = [line for line in result.stdout.splitlines() if line]
    if changed:
        raise ReleaseError(
            "tracked deployment or initializer files differ from the bundled commit"
        )
    for relative in SOURCE_BIND_PATHS:
        path = root / relative
        if not path.is_file():
            raise ReleaseError(f"required bind asset is missing: {relative}")
        current = path.read_bytes()
        expected = file_bytes_at_commit(root, commit, relative)
        if current != expected:
            raise ReleaseError(f"{relative} differs from the bundled commit")


def load_inventory(path: Path) -> dict:
    payload = load_json_object(path)
    validate_inventory_schema(payload)
    return payload


def expected_imported_refs(payload: dict) -> dict[str, str]:
    mapping = {}
    for role, record in payload["images"].items():
        mapping[record["reference"]] = record["configuration_digest"]
        del role
    return mapping


def verify_archive_set(root: Path, payload: dict) -> dict[str, dict]:
    imported: dict[str, dict] = {}
    expected = expected_imported_refs(payload)
    for role in ROLE_ORDER:
        record = payload["images"][role]
        archive_path = relative_archive_path(root, record["archive"]["path"])
        require_regular_file(archive_path)
        digest = sha256_file(archive_path)
        if digest != record["archive"]["sha256"]:
            raise ReleaseError(f"{role}: archive checksum mismatch")
        mappings = inspect_archive_bytes(archive_path)
        if record["reference"] not in mappings:
            raise ReleaseError(
                f"{role}: archive does not contain {record['reference']}"
            )
        for tag, info in mappings.items():
            if tag not in expected:
                raise ReleaseError(
                    f"{role}: archive contains undeclared reference {tag}"
                )
            if info["digest"] != expected[tag]:
                raise ReleaseError(
                    f"{role}: archive configuration digest for {tag} does not match the inventory"
                )
            if info["platform"] != PLATFORM:
                raise ReleaseError(
                    f"{role}: archive platform for {tag} is {info['platform']}, expected {PLATFORM}"
                )
            if tag in imported and imported[tag]["digest"] != info["digest"]:
                raise ReleaseError(f"conflicting imported reference {tag}")
            imported[tag] = info
    if set(imported) != set(expected):
        missing = sorted(set(expected) - set(imported))
        extra = sorted(set(imported) - set(expected))
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("extra " + ", ".join(extra))
        raise ReleaseError(
            "imported archive references do not equal the inventory: "
            + "; ".join(detail)
        )
    return imported


def reject_existing_conflicts(imported: dict[str, dict]) -> None:
    for reference, info in imported.items():
        existing = existing_reference(reference)
        if existing is None:
            continue
        image_id, _platform = image_identity(existing)
        if image_id != info["digest"]:
            raise ReleaseError(f"existing image {reference} conflicts with the release")


def verify_loaded_images(payload: dict) -> None:
    for role, record in payload["images"].items():
        inspected = inspect_image(record["reference"])
        image_id, platform = image_identity(inspected)
        if image_id != record["configuration_digest"]:
            raise ReleaseError(
                f"{role}: loaded {record['reference']} has {image_id}, expected {record['configuration_digest']}"
            )
        if platform != PLATFORM:
            raise ReleaseError(f"{role}: loaded platform is {platform}")


def import_release(*, delivery: Path, install_root: Path) -> dict:
    if destination_occupied(install_root):
        raise ReleaseError(f"install root already exists: {install_root}")
    inventory_path = delivery / "release.json"
    payload = load_inventory(inventory_path)
    parent = install_root.parent
    parent.mkdir(parents=True, exist_ok=True)
    lock_path = acquire_exclusive(install_root)
    stage = Path(
        tempfile.mkdtemp(prefix=f"{install_root.name}.stage-", dir=str(parent))
    )
    try:
        staged_inventory = stage / "release.json"
        copy_regular(inventory_path, staged_inventory)
        staged_payload = load_inventory(staged_inventory)
        if staged_payload != payload:
            raise ReleaseError("staged inventory does not match the delivery inventory")
        bundle_rel = staged_payload["source_bundle"]["path"]
        staged_bundle = stage / "source.bundle"
        copy_regular(relative_archive_path(delivery, bundle_rel), staged_bundle)
        if sha256_file(staged_bundle) != staged_payload["source_bundle"]["sha256"]:
            raise ReleaseError("source bundle checksum mismatch")
        source_dir = stage / "source"
        reconstructed = reconstruct_source(staged_bundle, source_dir)
        if reconstructed != staged_payload["ocu_source_sha"]:
            raise ReleaseError(
                "reconstructed source commit does not match the inventory"
            )
        verify_tracked_source(source_dir, staged_payload["ocu_source_sha"])
        staged_images = stage / "images"
        staged_images.mkdir()
        seen_paths = set()
        for role in ROLE_ORDER:
            record = staged_payload["images"][role]
            original = record["archive"]["path"]
            if original in seen_paths:
                raise ReleaseError(
                    f"{role}: archive path {original} collides with another role"
                )
            seen_paths.add(original)
            dest = staged_images / f"{role}.tar"
            copy_regular(relative_archive_path(delivery, original), dest)
            record["archive"]["path"] = f"images/{role}.tar"
            if sha256_file(dest) != record["archive"]["sha256"]:
                raise ReleaseError(f"{role}: staged archive checksum mismatch")
        imported = verify_archive_set(stage, staged_payload)
        reject_existing_conflicts(imported)
        for role in ROLE_ORDER:
            archive = stage / staged_payload["images"][role]["archive"]["path"]
            load_image(archive)
        try:
            verify_loaded_images(staged_payload)
        except ReleaseError as exc:
            raise ReleaseError(
                f"{exc}; image cache entries may remain and were not deleted"
            ) from exc
        for path in (staged_bundle, staged_images):
            if path.exists():
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        publish_exclusive(stage, install_root, lock_path)
        return staged_payload
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    finally:
        release_lock(lock_path)


def require_env(name: str) -> str:
    if name not in os.environ:
        raise ReleaseError(f"{name} is required")
    return os.environ[name]


def load_installed_inventory(path: Path | None = None) -> tuple[Path, dict]:
    raw = path or os.environ.get("OCU_RELEASE_MANIFEST")
    if not raw:
        raise ReleaseError("OCU_RELEASE_MANIFEST is required")
    manifest = Path(raw)
    if not manifest.is_file():
        raise ReleaseError(f"release inventory is missing: {manifest}")
    payload = load_inventory(manifest)
    return manifest, payload


def derive_runtime_images(
    payload: dict, runtime: dict[str, str] | None = None
) -> dict[str, str]:
    values = dict(runtime or {})
    for role, name in RUNTIME_IMAGE_VARS.items():
        expected = payload["images"][role]["reference"]
        current = values.get(name)
        if current in (None, ""):
            values[name] = expected
        elif current != expected:
            raise ReleaseError(f"{name} is {current}, expected {expected}")
    return values


def verify_local_images(payload: dict) -> None:
    for role, record in payload["images"].items():
        inspected = inspect_image(record["reference"])
        image_id, platform = image_identity(inspected)
        if image_id != record["configuration_digest"]:
            raise ReleaseError(
                f"{role} image {record['reference']} has configuration digest {image_id}, expected {record['configuration_digest']}"
            )
        if platform != PLATFORM:
            raise ReleaseError(f"{role} image platform is {platform}")


def resolved_service_images(docs: dict) -> dict[str, str]:
    mapping = {}
    for doc in docs.values():
        services = doc.get("services") or {}
        for name, body in services.items():
            if not isinstance(body, dict):
                continue
            image = body.get("image")
            if image:
                mapping[name] = str(image)
            environment = body.get("environment") or {}
            if name == "computer-use-server" and isinstance(environment, dict):
                workspace = environment.get("DOCKER_IMAGE")
                if workspace:
                    mapping.setdefault("workspace-env", str(workspace))
    return mapping


def verify_service_images(payload: dict, docs: dict) -> None:
    expected = {role: payload["images"][role]["reference"] for role in ROLE_ORDER}
    resolved = resolved_service_images(docs)
    for service, role_var in SERVICE_IMAGE_VARS.items():
        wanted = expected[
            next(role for role, name in RUNTIME_IMAGE_VARS.items() if name == role_var)
        ]
        actual = resolved.get(service)
        if actual is None:
            raise ReleaseError(f"resolved compose is missing image for {service}")
        if actual != wanted:
            raise ReleaseError(f"{service} image is {actual}, expected {wanted}")
    workspace_env = resolved.get("workspace-env")
    if workspace_env and workspace_env != expected["workspace"]:
        raise ReleaseError(
            f"computer-use-server DOCKER_IMAGE is {workspace_env}, expected {expected['workspace']}"
        )


def verify_runtime_binding(payload: dict, runtime: dict[str, str]) -> None:
    source_sha = runtime.get("SOURCE_SHA", "")
    if source_sha.lower() != payload["ocu_source_sha"].lower():
        raise ReleaseError("SOURCE_SHA does not match the release inventory")
    webui_sha = runtime.get("WEBUI_SOURCE_SHA") or runtime.get("OPENWEBUI_SOURCE_SHA")
    if webui_sha and webui_sha.lower() != payload["webui_source_sha"].lower():
        raise ReleaseError("WebUI source SHA does not match the release inventory")
    derive_runtime_images(payload, runtime)


def verify_startup(
    *,
    source_root: Path,
    payload: dict,
    docs: dict,
    runtime: dict[str, str] | None = None,
) -> None:
    values = dict(runtime or os.environ)
    verify_runtime_binding(payload, values)
    verify_tracked_source(source_root, payload["ocu_source_sha"])
    verify_local_images(payload)
    verify_service_images(payload, docs)


def bootstrap_images(payload: dict, provided: dict[str, str]) -> dict[str, str]:
    values = derive_runtime_images(payload, provided)
    if not workspace_name_ok(values["DOCKER_IMAGE"]):
        raise ReleaseError(
            "DOCKER_IMAGE must retain the open-computer-use name used for /home/assistant mounts"
        )
    verify_local_images(payload)
    return values


def bootstrap_dotenv_assignments(payload: dict, provided: dict[str, str]) -> list[str]:
    values = bootstrap_images(payload, provided)
    source_sha = provided.get("SOURCE_SHA", "")
    if source_sha.lower() != payload["ocu_source_sha"].lower():
        raise ReleaseError("SOURCE_SHA does not match the release inventory")
    webui_sha = (
        provided.get("WEBUI_SOURCE_SHA")
        or provided.get("OPENWEBUI_SOURCE_SHA")
        or payload["webui_source_sha"]
    )
    if webui_sha.lower() != payload["webui_source_sha"].lower():
        raise ReleaseError("WebUI source SHA does not match the release inventory")
    lines = [
        f"SOURCE_SHA={payload['ocu_source_sha']}",
        f"WEBUI_SOURCE_SHA={payload['webui_source_sha']}",
    ]
    for role in ROLE_ORDER:
        name = RUNTIME_IMAGE_VARS[role]
        lines.append(f"{name}={values[name]}")
    return lines


def cmd_bootstrap(args: argparse.Namespace) -> int:
    try:
        payload = load_inventory(Path(os.environ["OCU_RELEASE_MANIFEST"]).resolve())
        lines = bootstrap_dotenv_assignments(payload, dict(os.environ))
    except KeyError:
        fail("OCU_RELEASE_MANIFEST is required")
    except ReleaseError as exc:
        fail(str(exc))
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def provenance_lines(payload: dict) -> list[str]:
    lines = [
        f"- Release format: `{payload['format_version']}`",
        f"- Target platform: `{payload['platform']}`",
        f"- Open Computer Use source commit: `{payload['ocu_source_sha']}`",
        f"- Open WebUI source commit: `{payload['webui_source_sha']}`",
        "- Image configuration digests are Docker image IDs, not registry manifest digests.",
        "- Dockerfile hashes and material input versions record requested build inputs, not installed package versions.",
        "- Input hashes cover declared tracked context files, not Dockerignore-filtered engine inputs.",
    ]
    for role in ROLE_ORDER:
        record = payload["images"][role]
        build = record["build"]
        lines.append(
            f"- {role} image: `{record['reference']}` (`{record['configuration_digest']}`)"
        )
        materials = ", ".join(
            f"{item['name']}={item['requested']}"
            for item in build.get("materials") or []
        )
        if materials:
            lines.append(f"  - requested materials: {materials}")
        overrides = build.get("argument_overrides") or {}
        if overrides:
            rendered = ", ".join(
                f"{name}={value}" for name, value in sorted(overrides.items())
            )
            lines.append(f"  - explicit build-argument overrides: {rendered}")
        if role == "open-webui":
            bound = (build.get("arguments") or {}).get(WEBUI_BUILD_HASH_ARG)
            if bound:
                lines.append(
                    f"  - selected source SHA bound to {WEBUI_BUILD_HASH_ARG}: `{bound}`"
                )
        lines.append(f"  - Dockerfile SHA-256: `{build['dockerfile_sha256']}`")
        lines.append(f"  - input manifest SHA-256: `{build['input_manifest_sha256']}`")
    return lines


def parse_role_args(raw: list[str]) -> dict[str, dict[str, str]]:
    requested: dict[str, dict[str, str]] = {}
    for item in raw:
        if "=" not in item or ":" not in item.split("=", 1)[0]:
            raise ReleaseError("build arguments must be ROLE:NAME=VALUE")
        left, value = item.split("=", 1)
        role, name = left.split(":", 1)
        if role not in ROLE_ORDER:
            raise ReleaseError(f"unknown role {role}")
        requested.setdefault(role, {})[name] = value
    return requested


def cmd_build(args: argparse.Namespace) -> int:
    try:
        payload = build_release(
            ocu_source=Path(args.ocu_source).resolve(),
            webui_source=Path(args.webui_source).resolve(),
            destination=Path(args.destination).resolve(),
            postgres_image=args.postgres_image,
            build_args=parse_role_args(args.build_arg or []),
        )
    except ReleaseError as exc:
        fail(str(exc))
    print(f"wrote release inventory {args.destination}/release.json")
    print(f"ocu {payload['ocu_source_sha']} webui {payload['webui_source_sha']}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    try:
        payload = import_release(
            delivery=Path(args.delivery).resolve(),
            install_root=Path(args.install_root).resolve(),
        )
    except ReleaseError as exc:
        fail(str(exc))
    print(f"imported release {payload['ocu_source_sha']} into {args.install_root}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    try:
        payload = load_inventory(Path(args.inventory).resolve())
        if args.mode == "images":
            verify_local_images(payload)
        elif args.mode == "delivery":
            verify_archive_set(Path(args.delivery).resolve(), payload)
        else:
            verify_tracked_source(
                Path(args.source).resolve(), payload["ocu_source_sha"]
            )
            verify_local_images(payload)
    except ReleaseError as exc:
        fail(str(exc))
    print("release verification passed")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="build and export a six-role release")
    build.add_argument("--ocu-source", required=True)
    build.add_argument("--webui-source", required=True)
    build.add_argument("--destination", required=True)
    build.add_argument("--postgres-image", default=None)
    build.add_argument("--build-arg", action="append", default=[])
    build.set_defaults(func=cmd_build)
    imported = sub.add_parser("import", help="verify and install a release")
    imported.add_argument("--delivery", required=True)
    imported.add_argument("--install-root", required=True)
    imported.set_defaults(func=cmd_import)
    verify = sub.add_parser("verify", help="verify a release inventory")
    verify.add_argument("--inventory", required=True)
    verify.add_argument(
        "--mode", choices=("images", "delivery", "startup"), default="images"
    )
    verify.add_argument("--delivery")
    verify.add_argument("--source")
    verify.set_defaults(func=cmd_verify)
    bootstrap = sub.add_parser(
        "bootstrap", help="bind bootstrap image assignments to a release"
    )
    bootstrap.set_defaults(func=cmd_bootstrap)

    return parser


def main(argv: list[str] | None = None) -> int:
    cancellation_signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

    def cancel(signum, _frame):
        # Ignore repeated cancellation while owned subprocess/staging cleanup runs.
        for sig in cancellation_signals:
            signal.signal(sig, signal.SIG_IGN)
        raise SystemExit(128 + signum)

    previous = {sig: signal.signal(sig, cancel) for sig in cancellation_signals}
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        return args.func(args)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
