#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Attributed resource discovery, quiescence, restore allocation, and activation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import recovery
import recovery_db
import recovery_fs
import release
from recovery import RecoveryError, docker, require_command, sha256_file


def _json_command(*args: str):
    result = docker(*args)
    text = require_command(result, " ".join(args))
    if not text.strip():
        raise RecoveryError(f"{' '.join(args)} returned no data")
    try:
        return json.loads(text)
    except json.JSONDecodeError as cop:
        raise RecoveryError(f"{' '.join(args)} returned malformed JSON") from cop


def inspect_container(name: str) -> dict:
    payload = _json_command("inspect", name)
    if isinstance(payload, list):
        if not payload:
            raise RecoveryError(f"container {name} inspect returned no data")
        return payload[0]
    if isinstance(payload, dict):
        return payload
    raise RecoveryError(f"container {name} inspect returned malformed JSON")


def container_running(payload: dict) -> bool:
    state = payload.get("State")
    if isinstance(state, dict):
        status = str(state.get("Status") or "").lower()
        return bool(state.get("Running")) or status in {"running", "paused", "restarting"}
    return str(state).lower() in {"running", "paused", "restarting"}


def container_paused(payload: dict) -> bool:
    state = payload.get("State")
    if isinstance(state, dict):
        return bool(state.get("Paused")) or str(state.get("Status") or "").lower() == "paused"
    return str(state).lower() == "paused"


def list_containers(*, all_containers: bool, filters: list[str]) -> list[str]:
    argv = ["ps", "-q"]
    if all_containers:
        argv.append("--all")
    for item in filters:
        argv.extend(["--filter", item])
    result = docker(*argv)
    text = require_command(result, "list containers")
    return [line.strip() for line in text.splitlines() if line.strip()]


def list_volumes() -> list[dict]:
    result = docker("volume", "ls", "--format", "{{json .}}")
    text = require_command(result, "list volumes")
    if not text.strip():
        return []
    if text.lstrip().startswith("["):
        payload = json.loads(text)
        if not isinstance(payload, list):
            raise RecoveryError("volume list returned malformed JSON")
        return payload
    records = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as cop:
            raise RecoveryError("volume list returned malformed JSON") from cop
    return records


def inspect_volume(name: str) -> dict:
    payload = _json_command("volume", "inspect", name)
    if isinstance(payload, list):
        if not payload:
            raise RecoveryError(f"volume {name} inspect returned no data")
        return payload[0]
    if isinstance(payload, dict):
        return payload
    raise RecoveryError(f"volume {name} inspect returned malformed JSON")


def load_runtime(deploy_root: Path, runtime_file: Path | None) -> dict[str, str]:
    path = runtime_file or (deploy_root / "config" / "runtime.env")
    return recovery.parse_dotenv(path)


def deployment_identity(runtime: dict[str, str], deploy_root: Path) -> dict:
    project = runtime.get("COMPOSE_PROJECT_NAME") or "ocu-test"
    chat_dir = Path(runtime.get("OCU_CHAT_DATA_DIR") or (deploy_root / "data" / "chat"))
    skills_dir = Path(runtime.get("OCU_SKILLS_CACHE_DIR") or (deploy_root / "data" / "skills-cache"))
    return {
        "project": project,
        "chat_dir": chat_dir,
        "skills_dir": skills_dir,
        "webui_volume": recovery.webui_volume_name(project),
        "postgres_volume": recovery.postgres_volume_name(project),
        "postgres_container": recovery.compose_name(project, recovery.POSTGRES_SERVICE),
        "webui_container": recovery.compose_name(project, "open-webui"),
        "server_container": "ocu-test-computer-use-server",
        "retention_container": "ocu-test-retention-guard",
        "init_container": "ocu-test-open-webui-init",
        "proxy_container": "ocu-test-proxy",
    }


def _labels(payload: dict) -> dict:
    labels = payload.get("Config", {}).get("Labels") if isinstance(payload.get("Config"), dict) else None
    if not isinstance(labels, dict):
        labels = payload.get("Labels") or {}
    return {str(key): str(value) for key, value in labels.items()}


def discover_sandboxes() -> list[dict]:
    ids = list_containers(all_containers=True, filters=[f"label={recovery.SANDBOX_LABEL}"])
    found = []
    for container_id in ids:
        payload = inspect_container(container_id)
        labels = _labels(payload)
        name = str(payload.get("Name") or payload.get("name") or "").lstrip("/")
        chat_id = labels.get("chat-id")
        if not chat_id and name.startswith("owui-chat-"):
            chat_id = name[len("owui-chat-") :]
        if not chat_id:
            raise RecoveryError(f"sandbox {name or container_id} is missing a chat-id label")
        found.append(
            {
                "id": payload.get("Id") or container_id,
                "name": name,
                "chat_id": recovery.canonical_chat_id(chat_id),
                "payload": payload,
            }
        )
    return found


def discover_workspace_volumes(expected_chats: set[str]) -> dict[str, str]:
    volumes = {}
    for record in list_volumes():
        name = str(record.get("Name") or record.get("name") or "")
        match = recovery.WORKSPACE_VOLUME_RE.match(name)
        if not match:
            continue
        chat_id = recovery.canonical_chat_id(match.group("chat_id"))
        inspect_volume(name)
        volumes[chat_id] = name
    unexpected = set(volumes) - expected_chats
    missing = expected_chats - set(volumes)
    if unexpected:
        raise RecoveryError(
            "unattributable workspace volume "
            + ", ".join(sorted(volumes[chat] for chat in unexpected))
        )
    if missing:
        raise RecoveryError("missing workspace volume for chat " + ", ".join(sorted(missing)))
    return volumes


def _stop_container(name: str) -> None:
    result = docker("stop", "--time", "30", name)
    if result.returncode != 0:
        raise RecoveryError(f"cannot stop {name}")
    payload = inspect_container(name)
    if container_running(payload) or container_paused(payload):
        raise RecoveryError(f"{name} remained active after stop")


def establish_quiescence(identity: dict) -> dict:
    writers = [
        identity["init_container"],
        identity["retention_container"],
        identity["server_container"],
        identity["webui_container"],
        identity["proxy_container"],
    ]
    for name in writers:
        try:
            payload = inspect_container(name)
        except RecoveryError:
            continue
        if container_paused(payload):
            raise RecoveryError(f"{name} is paused and cannot be captured")
        if container_running(payload):
            _stop_container(name)
    first = discover_sandboxes()
    for sandbox in first:
        payload = sandbox["payload"]
        if container_paused(payload):
            raise RecoveryError(f"{sandbox['name']} is paused and cannot be captured")
        if container_running(payload):
            _stop_container(sandbox["id"])
    postgres = inspect_container(identity["postgres_container"])
    if not container_running(postgres) or container_paused(postgres):
        raise RecoveryError("postgres must remain available for dump")
    second = discover_sandboxes()
    if {item["id"] for item in second} != {item["id"] for item in first}:
        extra = {item["name"] for item in second} - {item["name"] for item in first}
        raise RecoveryError(
            "new sandbox appeared after admission shutdown: "
            + ", ".join(sorted(extra) or ["unknown"])
        )
    for sandbox in second:
        payload = inspect_container(sandbox["id"])
        if container_running(payload) or container_paused(payload):
            raise RecoveryError(f"{sandbox['name']} remained active at capture")
    for name in writers:
        try:
            payload = inspect_container(name)
        except RecoveryError:
            continue
        if container_running(payload):
            raise RecoveryError(f"{name} remained active at capture")
    chats = {item["chat_id"] for item in second}
    if identity["chat_dir"].exists():
        for child in sorted(identity["chat_dir"].iterdir()):
            if child.is_dir() and not child.is_symlink():
                chats.add(recovery.canonical_chat_id(child.name))
    volumes = discover_workspace_volumes(chats)
    return {"sandboxes": second, "chats": sorted(chats), "volumes": volumes}


def helper_image(runtime: dict[str, str], role: str) -> str:
    payload_path = runtime.get("OCU_RELEASE_MANIFEST")
    if not payload_path:
        raise RecoveryError("OCU_RELEASE_MANIFEST is required")
    payload = release.load_inventory(Path(payload_path))
    release.verify_local_images(payload)
    return payload["images"][role]["reference"]


def helper_root() -> Path:
    return Path(__file__).resolve().parent


def run_volume_helper(image: str, mounts: list[tuple[str, str, str]], argv: list[str]):
    return _run_helper(image, mounts, argv, python="/usr/bin/python3")


def run_broker_helper(image: str, mounts: list[tuple[str, str, str]], argv: list[str]):
    return _run_helper(
        image, mounts, argv, python="/usr/local/bin/python3", workdir="/app"
    )


def _run_helper(
    image: str,
    mounts: list[tuple[str, str, str]],
    argv: list[str],
    *,
    python: str,
    workdir: str | None = None,
):
    root = helper_root()
    command = [
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        "0:0",
        "--entrypoint",
        python,
        "--pull",
        "never",
        "-v",
        f"{root}:/recovery:ro",
    ]
    if workdir:
        command.extend(["--workdir", workdir])
    for source, target, mode in mounts:
        command.extend(["-v", f"{source}:{target}:{mode}"])
    command.extend([image, "/recovery/recovery_helper.py", *argv])
    return docker(*command)


def capture_volume(volume: str, archive: Path, image: str) -> dict:
    archive.parent.mkdir(parents=True, exist_ok=True)
    inspect_volume(volume)
    result = run_volume_helper(
        image,
        [
            (volume, "/source", "ro"),
            (str(archive.parent), "/output", "rw"),
        ],
        ["capture", "/source", f"/output/{archive.name}"],
    )
    if result.returncode != 0:
        raise RecoveryError(f"volume helper failed for {volume}")
    if not archive.exists() or archive.stat().st_size == 0:
        raise RecoveryError(f"volume helper did not publish {archive.name}")
    recovery_fs.validate_archive(archive)
    return {"path": archive.name, "sha256": sha256_file(archive), "volume": volume}


def restore_volume(volume: str, archive: Path, image: str) -> None:
    inspect_volume(volume)
    recovery_fs.validate_archive(archive)
    result = run_volume_helper(
        image,
        [
            (volume, "/target", "rw"),
            (str(archive.parent), "/input", "ro"),
        ],
        ["extract", f"/input/{archive.name}", "/target"],
    )
    if result.returncode != 0:
        raise RecoveryError(f"volume helper failed for {volume}")


def _copy_tree_archive(source: Path, archive: Path) -> dict:
    record = recovery_fs.capture_tree(source, archive)
    return {"path": archive.name, "sha256": record["sha256"]}


def backup_deployment(*, deploy_root: Path, destination: Path, runtime_file: Path | None) -> Path:
    recovery.pin_runtime_docker_host()
    identity_name = recovery.daemon_identity()
    runtime_path = runtime_file or (deploy_root / "config" / "runtime.env")
    runtime = load_runtime(deploy_root, runtime_path)
    identity = deployment_identity(runtime, deploy_root)
    session = release.PublicationSession(destination)
    try:
        session.acquire()
        with release.ImageStoreLock(recovery.recovery_lock_path(identity_name)):
            quiesced = establish_quiescence(identity)
            stage = session.allocate_stage()
            os.chmod(stage, 0o700)
            components: dict[str, dict] = {}
            db_dir = stage / "database"
            db_dir.mkdir()
            components["database"] = recovery_db.capture_database(
                identity["postgres_container"], db_dir
            )
            components["database"]["path"] = "database/openwebui.dump"
            workspace_image = helper_image(runtime, "workspace")
            webui_archive = stage / "webui-data.tar.gz"
            components["webui-data"] = capture_volume(
                identity["webui_volume"], webui_archive, workspace_image
            )
            components["webui-data"]["path"] = "webui-data.tar.gz"
            if not _archive_contains(webui_archive, recovery.MARKER_NAME):
                raise RecoveryError("WebUI initializer marker is missing from the data volume")
            components["webui-data"]["marker"] = recovery.MARKER_NAME
            components["chat-data"] = _copy_tree_archive(
                identity["chat_dir"], stage / "chat-data.tar.gz"
            )
            components["chat-data"]["path"] = "chat-data.tar.gz"
            components["skills-cache"] = _copy_tree_archive(
                identity["skills_dir"], stage / "skills-cache.tar.gz"
            )
            components["skills-cache"]["path"] = "skills-cache.tar.gz"
            workspaces = {}
            workspace_dir = stage / "workspaces"
            workspace_dir.mkdir()
            for chat_id, volume in sorted(quiesced["volumes"].items()):
                archive = workspace_dir / f"{chat_id}.tar.gz"
                record = capture_volume(volume, archive, workspace_image)
                record["path"] = f"workspaces/{chat_id}.tar.gz"
                workspaces[chat_id] = record
            components["workspaces"] = workspaces
            copy_runtime = stage / "runtime.env"
            recovery.copy_private(runtime_path, copy_runtime)
            components["runtime-config"] = {
                "path": "runtime.env",
                "sha256": sha256_file(copy_runtime),
            }
            admin_source = Path(
                os.environ.get("OCU_ADMIN_CREDENTIALS_FILE")
                or "/root/ocu-test-openwebui-admin-credentials.txt"
            )
            if admin_source.exists():
                admin_dest = stage / "admin-credentials.txt"
                recovery.copy_private(admin_source, admin_dest)
                components["admin-config"] = {
                    "path": "admin-credentials.txt",
                    "sha256": sha256_file(admin_dest),
                }
            else:
                components["admin-config"] = {"path": None, "sha256": None}
            inventory_dest = stage / "release.json"
            recovery.copy_private(Path(runtime["OCU_RELEASE_MANIFEST"]), inventory_dest)
            inventory = release.load_inventory(inventory_dest)
            components["release-inventory"] = {
                "path": "release.json",
                "sha256": sha256_file(inventory_dest),
                "ocu_source_sha": inventory["ocu_source_sha"],
                "webui_source_sha": inventory["webui_source_sha"],
            }
            version_source = deploy_root / "DEPLOYED_VERSION.md"
            if version_source.exists():
                version_dest = stage / "DEPLOYED_VERSION.md"
                recovery.copy_private(version_source, version_dest, 0o644)
                components["version-record"] = {
                    "path": "DEPLOYED_VERSION.md",
                    "sha256": sha256_file(version_dest),
                }
            else:
                components["version-record"] = {"path": None, "sha256": None}
            payload = {
                "format_version": recovery.FORMAT_VERSION,
                "source_daemon": identity_name,
                "source_deployment": str(deploy_root),
                "compose_project": identity["project"],
                "docker_host": recovery.SUPPORTED_DOCKER_HOST,
                "schema": components["database"]["schema"],
                "release_inventory": {
                    "ocu_source_sha": inventory["ocu_source_sha"],
                    "webui_source_sha": inventory["webui_source_sha"],
                    "sha256": components["release-inventory"]["sha256"],
                },
                "components": components,
                "chats": quiesced["chats"],
            }
            recovery.write_private_json(stage / recovery.MANIFEST_NAME, payload)
            verify_recovery_set(stage, require_published=False)
            recovery.exclusive_publish(stage, destination, session.lock_path)
            session.committed = True
            session.stage = None
            os.chmod(destination, 0o700)
            return destination
    finally:
        session.cleanup()


def _archive_contains(archive: Path, relative: str) -> bool:
    for member in recovery_fs.validate_archive(archive):
        name = member["name"]
        if name == relative or name.endswith("/" + relative):
            return True
    return False


def verify_recovery_set(root: Path, *, require_published: bool = True) -> dict:
    payload = recovery.load_json_object(root / recovery.MANIFEST_NAME)
    if payload.get("format_version") != recovery.FORMAT_VERSION:
        raise RecoveryError("unsupported recovery format")
    components = payload.get("components") or {}
    missing = [name for name in recovery.COMPONENT_NAMES if name not in components]
    if missing:
        raise RecoveryError("recovery set is missing " + ", ".join(missing))
    _require_component_file(root, components["database"]["path"], components["database"]["sha256"])
    for key in ("webui-data", "chat-data", "skills-cache", "runtime-config", "release-inventory"):
        record = components[key]
        _require_component_file(root, record["path"], record["sha256"])
    admin = components.get("admin-config") or {}
    if admin.get("path"):
        _require_component_file(root, admin["path"], admin["sha256"])
    version = components.get("version-record") or {}
    if version.get("path"):
        _require_component_file(root, version["path"], version["sha256"])
    if not _archive_contains(
        recovery.confined_component_path(root, components["webui-data"]["path"]),
        recovery.MARKER_NAME,
    ):
        raise RecoveryError("WebUI initializer marker is missing from the recovery set")
    recovery_fs.validate_archive(
        recovery.confined_component_path(root, components["webui-data"]["path"])
    )
    recovery_fs.validate_archive(
        recovery.confined_component_path(root, components["chat-data"]["path"])
    )
    recovery_fs.validate_archive(
        recovery.confined_component_path(root, components["skills-cache"]["path"])
    )
    workspaces = components["workspaces"]
    if not isinstance(workspaces, dict) or not workspaces:
        raise RecoveryError("workspace volume archives are missing")
    seen_paths: set[str] = set()
    for chat_id, record in workspaces.items():
        recovery.canonical_chat_id(chat_id)
        relative = record["path"]
        if relative in seen_paths:
            raise RecoveryError(f"duplicate workspace archive path {relative}")
        seen_paths.add(relative)
        path = _require_component_file(root, relative, record["sha256"])
        recovery_fs.validate_archive(path)
    release.load_inventory(
        recovery.confined_component_path(root, components["release-inventory"]["path"])
    )
    del require_published
    return payload


def _require_component_file(root: Path, relative: str, digest: str) -> Path:
    path = recovery.confined_component_path(root, relative)
    if sha256_file(path) != digest:
        raise RecoveryError(f"{relative} checksum mismatch")
    return path


def colliding_resources(identity: dict) -> list[str]:
    names = []
    for name in recovery.FIXED_CONTAINER_NAMES + (
        identity["postgres_container"],
        identity["webui_container"],
    ):
        if docker("inspect", name).returncode == 0:
            names.append(name)
    for volume in (identity["webui_volume"], identity["postgres_volume"]):
        if docker("volume", "inspect", volume).returncode == 0:
            names.append(volume)
    for record in list_volumes():
        name = str(record.get("Name") or "")
        if recovery.WORKSPACE_VOLUME_RE.match(name):
            names.append(name)
    names.extend(list_containers(all_containers=True, filters=[f"label={recovery.SANDBOX_LABEL}"]))
    return names


def materialize_runtime(
    captured: dict[str, str],
    *,
    destination_root: Path,
    provider_file: Path,
    inventory: dict,
) -> dict[str, str]:
    values = dict(captured)
    values["OCU_CHAT_DATA_DIR"] = str(destination_root / "data" / "chat")
    values["OCU_SKILLS_CACHE_DIR"] = str(destination_root / "data" / "skills-cache")
    values["OCU_RELEASE_MANIFEST"] = str(destination_root / "release.json")
    values["SOURCE_SHA"] = inventory["ocu_source_sha"]
    values["WEBUI_SOURCE_SHA"] = inventory["webui_source_sha"]
    values.update(release.derive_runtime_images(inventory, values))
    values["DMX_ENV_FILE"] = str(provider_file)
    dest = destination_root / "config" / "runtime.env"
    if dest.exists():
        raise RecoveryError(f"refusing to overwrite {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(dest.parent, 0o700)
    recovery.write_private_bytes(dest, recovery.render_dotenv(values).encode("utf-8"))
    return values


def create_volume(name: str) -> None:
    require_command(docker("volume", "create", name), f"create volume {name}")


def start_isolated_postgres(identity: dict, runtime: dict[str, str], image: str) -> str:
    name = identity["postgres_container"]
    env_file = Path(os.environ.get("TMPDIR") or "/tmp") / f"ocu-pg-{os.getpid()}-{os.urandom(4).hex()}.env"
    recovery.write_private_bytes(
        env_file,
        recovery.render_dotenv(
            {
                "POSTGRES_USER": "openwebui",
                "POSTGRES_PASSWORD": runtime["POSTGRES_PASSWORD"],
                "POSTGRES_DB": "openwebui",
            }
        ).encode("utf-8"),
    )
    try:
        result = docker(
            "run",
            "-d",
            "--name",
            name,
            "--network",
            "none",
            "--env-file",
            str(env_file),
            "-v",
            f"{identity['postgres_volume']}:/var/lib/postgresql/data",
            "--pull",
            "never",
            image,
        )
        if result.returncode != 0:
            raise RecoveryError("start isolated postgres failed")
        return name
    finally:
        try:
            env_file.unlink()
        except OSError:
            pass


def stop_isolated_postgres(name: str) -> None:
    docker("stop", "--time", "30", name)


def selected_release_identity(payload: dict) -> tuple[str, str, tuple[str, ...]]:
    images = payload["images"]
    references = tuple(images[role]["reference"] for role in release.ROLE_ORDER)
    return payload["ocu_source_sha"], payload["webui_source_sha"], references


def require_selected_release(existing: dict, requested: dict) -> None:
    if selected_release_identity(existing) != selected_release_identity(requested):
        raise RecoveryError("requested retained delivery does not match the installed release identity")


def import_selected_release(delivery: Path, install_root: Path) -> dict:
    requested = release.load_inventory(delivery / "release.json")
    release.verify_archive_set(delivery, requested)
    source = install_root / "source"
    manifest = install_root / "release.json"
    if source.is_dir() and (source / ".git").exists():
        release.verify_tracked_source(source, requested["ocu_source_sha"])
        release.require_supported_source_contract(source)
        recovery.copy_private(delivery / "release.json", manifest)
        return requested
    if source.exists() or source.is_symlink():
        raise RecoveryError("restored source path is already occupied")
    if recovery.destination_occupied(install_root):
        source_root = install_root.parent / f"{install_root.name}.selected-source"
        if source_root.exists():
            raise RecoveryError(f"selected source already exists: {source_root}")
        payload = release.import_release(delivery=delivery, install_root=source_root)
        require_selected_release(payload, requested)
        os.symlink(source_root / "source", source)
        recovery.copy_private(source_root / "release.json", manifest)
        return payload
    payload = release.import_release(delivery=delivery, install_root=install_root)
    require_selected_release(payload, requested)
    return payload


def restore_deployment(
    *,
    recovery_set: Path,
    destination_root: Path,
    provider_file: Path,
    retained_delivery: Path | None,
    docker_host: str,
) -> list[str]:
    recovery.pin_runtime_docker_host(docker_host)
    target_id = recovery.daemon_identity()
    payload = verify_recovery_set(recovery_set)
    if target_id == payload["source_daemon"]:
        raise RecoveryError("restore requires a distinct empty target daemon")
    if recovery.destination_occupied(destination_root):
        raise RecoveryError(f"destination already exists: {destination_root}")
    captured_runtime = recovery.parse_dotenv(
        recovery.confined_component_path(
            recovery_set, payload["components"]["runtime-config"]["path"]
        )
    )
    identity = deployment_identity(captured_runtime, destination_root)
    collisions = colliding_resources(identity)
    if collisions:
        raise RecoveryError("target already contains " + ", ".join(collisions))
    recovery.require_regular_file(provider_file)
    provider = recovery.parse_dotenv(provider_file)
    selected_inventory = release.load_inventory(
        recovery.confined_component_path(
            recovery_set, payload["components"]["release-inventory"]["path"]
        )
    )
    if retained_delivery is not None:
        selected_inventory = release.load_inventory(retained_delivery / "release.json")
        release.verify_archive_set(retained_delivery, selected_inventory)
    workspace_image = selected_inventory["images"]["workspace"]["reference"]
    postgres_image = selected_inventory["images"]["postgres"]["reference"]
    webui_image = selected_inventory["images"]["open-webui"]["reference"]
    server_image = selected_inventory["images"]["computer-use-server"]["reference"]
    release.verify_local_images(selected_inventory)
    recovery_db.require_compatible_revision(payload["schema"], webui_image)
    recovery_db.require_compatible_tools(payload["schema"], postgres_image)
    session = release.PublicationSession(destination_root)
    owned: list[str] = []
    try:
        session.acquire()
        with release.ImageStoreLock(recovery.recovery_lock_path(target_id)):
            if colliding_resources(identity) or recovery.destination_occupied(destination_root):
                raise RecoveryError("target became occupied before allocation")
            stage = session.allocate_stage()
            os.chmod(stage, 0o700)
            recovery.copy_private(
                recovery_set / payload["components"]["release-inventory"]["path"],
                stage / "release.json",
            )
            if retained_delivery is not None:
                recovery.copy_private(retained_delivery / "release.json", stage / "release.json")
            (stage / "data" / "chat").mkdir(parents=True)
            (stage / "data" / "skills-cache").mkdir(parents=True)
            recovery_fs.extract_tree(
                recovery_set / payload["components"]["chat-data"]["path"],
                stage / "data" / "chat",
            )
            recovery_fs.extract_tree(
                recovery_set / payload["components"]["skills-cache"]["path"],
                stage / "data" / "skills-cache",
            )
            version = payload["components"]["version-record"]
            if version.get("path"):
                recovery.copy_private(
                    recovery_set / version["path"],
                    stage / "CAPTURED_VERSION.md",
                    0o644,
                )
            recovery.write_private_json(stage / recovery.MANIFEST_NAME, payload)
            recovery.exclusive_publish(stage, destination_root, session.lock_path)
            session.committed = True
            session.stage = None
            owned.append(str(destination_root))
            runtime = materialize_runtime(
                captured_runtime,
                destination_root=destination_root,
                provider_file=provider_file,
                inventory=selected_inventory,
            )
            create_volume(identity["webui_volume"])
            owned.append(identity["webui_volume"])
            create_volume(identity["postgres_volume"])
            owned.append(identity["postgres_volume"])
            restore_volume(
                identity["webui_volume"],
                recovery.confined_component_path(
                    recovery_set, payload["components"]["webui-data"]["path"]
                ),
                workspace_image,
            )
            if not _volume_has_marker(identity["webui_volume"], workspace_image):
                raise RecoveryError("restored WebUI data is missing the initializer marker")
            for chat_id, record in payload["components"]["workspaces"].items():
                volume = recovery.workspace_volume_name(chat_id)
                create_volume(volume)
                owned.append(volume)
                restore_volume(
                    volume,
                    recovery.confined_component_path(recovery_set, record["path"]),
                    workspace_image,
                )
            postgres = start_isolated_postgres(identity, runtime, postgres_image)
            owned.append(postgres)
            try:
                recovery_db.restore_database(
                    postgres,
                    recovery.confined_component_path(
                        recovery_set, payload["components"]["database"]["path"]
                    ),
                )
                recovery_db.require_additive_schema(postgres)
                recovery_db.prune_orphans(postgres)
                restored = recovery_db.inspect_restored_state(postgres)
                _require_cursor_lag(
                    restored, destination_root / "data" / "chat", server_image
                )
                _require_provider_precedence(
                    recovery_db.inspect_provider_config(postgres), provider
                )
            finally:
                stop_isolated_postgres(postgres)
            marker = destination_root / ".restored"
            marker.write_text("restored\n", encoding="utf-8")
            os.chmod(marker, 0o600)
            return owned
    except BaseException:
        if owned:
            recovery.report("owned partial resources remain: " + ", ".join(owned))
        raise
    finally:
        session.cleanup()


def _volume_has_marker(volume: str, image: str) -> bool:
    inspect_volume(volume)
    result = run_volume_helper(
        image,
        [(volume, "/target", "ro")],
        ["probe", f"/target/{recovery.MARKER_NAME}"],
    )
    return result.returncode == 0


def _broker_listing(chat_dir: Path, chat_id: str, image: str) -> dict:
    chat_id = recovery.canonical_chat_id(chat_id)
    before = _index_snapshot(chat_dir, chat_id)
    result = run_broker_helper(
        image,
        [(str(chat_dir), "/chat", "rw")],
        ["listing", "/chat", chat_id],
    )
    if result.returncode != 0:
        detail = str(result.stderr or result.stdout or "").strip()
        raise RecoveryError(f"selected-release broker listing failed: {detail}")
    raw = result.stdout or ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        listing = json.loads(raw)
    except json.JSONDecodeError as cop:
        raise RecoveryError("selected-release broker listing returned malformed JSON") from cop
    after = _index_snapshot(chat_dir, chat_id)
    if after["counter"] != before["counter"]:
        raise RecoveryError(f"chat {chat_id} first recovered listing rewrote broker identity")
    for path, entry in before["active"].items():
        restored = after["active"].get(path)
        if restored is None or restored.get("file_id") != entry.get("file_id"):
            raise RecoveryError(f"chat {chat_id} recovered listing changed file identity")
        if restored.get("revision") != entry.get("revision"):
            raise RecoveryError(f"chat {chat_id} recovered listing changed revision identity")
    return listing


def _index_snapshot(chat_dir: Path, chat_id: str) -> dict:
    path = chat_dir / chat_id / ".ocu" / "index.json"
    if not path.exists():
        raise RecoveryError(f"chat {chat_id} recovered broker index is missing")
    index = json.loads(path.read_text(encoding="utf-8"))
    return {"counter": int(index["counter"]), "active": index.get("active") or {}}


def _require_cursor_lag(restored: dict, chat_dir: Path, image: str) -> None:
    live = set(restored["live_chats"])
    for row in restored["chat_state"]:
        chat_id = row["chat_id"]
        if chat_id not in live:
            continue
        listing = _broker_listing(chat_dir, chat_id, image)
        counter = int(listing["revision"])
        cursor = int(row["last_seen_revision"])
        if cursor > counter:
            raise RecoveryError(
                f"chat {chat_id} cursor {cursor} leads recovered broker counter {counter}"
            )


def _require_provider_precedence(db_text: str, provider: dict[str, str]) -> None:
    for name, value in provider.items():
        if name in recovery.PROVIDER_KEYS and value:
            if value in db_text:
                recovery.report("restored persistent provider settings remain in effect")
            return


def activation_env(runtime: dict[str, str], inventory: dict, destination_root: Path) -> dict[str, str]:
    env = recovery.helper_env()
    env.update(runtime)
    env["OCU_RELEASE_MANIFEST"] = str(destination_root / "release.json")
    env["SOURCE_SHA"] = inventory["ocu_source_sha"]
    env["WEBUI_SOURCE_SHA"] = inventory["webui_source_sha"]
    env.update(release.derive_runtime_images(inventory, env))
    env["DOCKER_HOST"] = recovery.SUPPORTED_DOCKER_HOST
    env.pop("DOCKER_CONTEXT", None)
    return env


def activate_release(*, destination_root: Path, retained_delivery: Path, docker_host: str) -> None:
    recovery.pin_runtime_docker_host(docker_host)
    if not (destination_root / ".restored").exists():
        raise RecoveryError("destination is not a restored recovery target")
    runtime = recovery.parse_dotenv(destination_root / "config" / "runtime.env")
    for sandbox in discover_sandboxes():
        if container_running(inspect_container(sandbox["id"])):
            raise RecoveryError("target already has a running sandbox")
    requested = release.load_inventory(retained_delivery / "release.json")
    source = destination_root / "source"
    if source.is_dir() and (source / ".git").exists():
        try:
            release.verify_tracked_source(source, requested["ocu_source_sha"])
        except (RecoveryError, release.ReleaseError) as cop:
            raise RecoveryError(
                "requested retained delivery does not match the installed source identity"
            ) from cop
    inventory = import_selected_release(retained_delivery, destination_root)
    require_selected_release(inventory, requested)
    release.verify_tracked_source(destination_root / "source", inventory["ocu_source_sha"])
    release.verify_local_images(inventory)
    release.require_supported_source_contract(destination_root / "source")
    schema = recovery.load_json_object(destination_root / recovery.MANIFEST_NAME).get("schema") or {}
    recovery_db.require_compatible_revision(schema, inventory["images"]["open-webui"]["reference"])
    recovery_db.require_compatible_tools(schema, inventory["images"]["postgres"]["reference"])
    script = destination_root / "source" / "deploy" / "up.sh"
    if not script.exists():
        raise RecoveryError("selected release source/deploy/up.sh is missing")
    env = activation_env(runtime, inventory, destination_root)
    result = recovery.run_up(script, env)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RecoveryError(f"selected release startup failed: {detail}")
    for sandbox in discover_sandboxes():
        if container_running(inspect_container(sandbox["id"])):
            raise RecoveryError("recovery started a sandbox")
    version_script = (
        destination_root
        / "source"
        / "deploy"
        / "production-like-test"
        / "scripts"
        / "write-deployed-version.sh"
    )
    if version_script.exists():
        subprocess.run(
            ["bash", str(version_script)],
            cwd=str(destination_root / "source"),
            capture_output=True,
            text=True,
            check=False,
            env={**env, "DEPLOY_ROOT": str(destination_root)},
        )
