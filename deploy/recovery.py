#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Cold backup, isolated restore, and previous-release activation."""

from __future__ import annotations

import argparse
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

import release


FORMAT_VERSION = 1
SUPPORTED_DOCKER_HOST = "unix:///var/run/docker.sock"
RUNTIME_SOCKET_MOUNT = "/var/run/docker.sock"
RECOVERY_LOCK_DIR = Path("/run/ocu-recovery")
MARKER_NAME = ".computer-use-initialized"
MANIFEST_NAME = "recovery.json"
COMPONENT_NAMES = (
    "database",
    "webui-data",
    "chat-data",
    "skills-cache",
    "workspaces",
    "runtime-config",
    "admin-config",
    "release-inventory",
    "version-record",
)
FIXED_CONTAINER_NAMES = (
    "ocu-test-computer-use-server",
    "ocu-test-retention-guard",
    "ocu-test-open-webui-init",
    "ocu-test-proxy",
)
WRITER_SERVICES = (
    "computer-use-server",
    "open-webui",
    "open-webui-init",
    "retention-guard",
)
POSTGRES_SERVICE = "postgres"
SANDBOX_LABEL = "managed-by=mcp-computer-use-orchestrator"
WORKSPACE_VOLUME_RE = re.compile(r"^chat-(?P<chat_id>.+)-workspace$")
HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_DOTENV_RE = re.compile(r"^[A-Z][A-Z0-9_]*=.*$")
UNSAFE_DOTENV_CHARS = set("\n\r\x00`$;&|<>(){}[]\\\"'")
IDENTITY_KEYS = (
    "SOURCE_SHA",
    "WEBUI_SOURCE_SHA",
    "OCU_RELEASE_MANIFEST",
    "DOCKER_IMAGE",
    "COMPUTER_USE_SERVER_IMAGE",
    "RETENTION_GUARD_IMAGE",
    "OCU_PROXY_IMAGE",
    "OPENWEBUI_IMAGE",
    "POSTGRES_IMAGE",
)
PATH_KEYS = ("OCU_CHAT_DATA_DIR", "OCU_SKILLS_CACHE_DIR")
PROVIDER_KEYS = ("DMXAPI_API_KEY", "OPENAI_API_KEY")
SECRET_MARKERS = ("SECRET", "TOKEN", "PASSWORD", "CREDENTIAL", "API_KEY")
CANCELLATION_SIGNALS = release.CANCELLATION_SIGNALS
ADDITIVE_SCHEMA = "ocu_chat_state"


class RecoveryError(Exception):
    """Operator-visible backup, restore, or activation failure."""


def fail(message: str, code: int = 1) -> None:
    print(f"recovery: {message}", file=sys.stderr)
    raise SystemExit(code)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
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


def report(message: str) -> None:
    print(f"recovery: {message}", file=sys.stderr)


def require_root() -> None:
    if os.geteuid() != 0:
        raise RecoveryError("recovery requires root")


def docker(*args: str, cwd: Path | None = None, input: bytes | None = None) -> subprocess.CompletedProcess:
    return release.docker(*args, cwd=cwd) if input is None else _docker_with_input(args, cwd, input)


def _docker_with_input(
    args: tuple[str, ...], cwd: Path | None, input: bytes
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if release._PINNED_DOCKER_HOST:
        env["DOCKER_HOST"] = release._PINNED_DOCKER_HOST
        env.pop("DOCKER_CONTEXT", None)
    return subprocess.run(
        ["docker", *args],
        cwd=None if cwd is None else str(cwd),
        capture_output=True,
        check=False,
        env=env,
        input=input,
    )


def require_command(result: subprocess.CompletedProcess, action: str) -> str:
    return release.require_command(result, action)


def pin_runtime_docker_host(host: str | None = None) -> str:
    selected = (host or os.environ.get("DOCKER_HOST") or "").strip() or release.selected_docker_host()
    normalized = release.require_local_docker_host(selected)
    if normalized != SUPPORTED_DOCKER_HOST:
        raise RecoveryError(
            f"unsupported Docker endpoint {normalized}; recovery requires {SUPPORTED_DOCKER_HOST}"
        )
    os.environ["DOCKER_HOST"] = normalized
    os.environ.pop("DOCKER_CONTEXT", None)
    release._PINNED_DOCKER_HOST = normalized
    return normalized


def daemon_identity() -> str:
    pin_runtime_docker_host()
    return release.local_daemon_identity()


def recovery_lock_path(identity: str) -> Path:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return RECOVERY_LOCK_DIR / f"{digest}.lock"


def load_json_object(path: Path) -> dict:
    return release.load_json_object(path)


def write_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def write_private_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, mode)


def copy_private(source: Path, dest: Path, mode: int = 0o600) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
    os.chmod(dest, mode)


def parse_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        raise RecoveryError(f"{path}: missing")
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RecoveryError(f"{path}: configuration must be a regular file")
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as cop:
        raise RecoveryError(f"{path}: configuration is not UTF-8 text") from cop
    for index, line in enumerate(text.splitlines(), start=1):
        if not line or line.startswith("#"):
            continue
        if "=" not in line or not SAFE_DOTENV_RE.match(line.split("=", 1)[0] + "=" + "x"):
            if "=" not in line:
                raise RecoveryError(f"{path}:{index}: malformed assignment")
        name, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise RecoveryError(f"{path}:{index}: malformed assignment")
        if any(ch in UNSAFE_DOTENV_CHARS for ch in value) and name not in {"OCU_SANDBOX_EGRESS_ALLOW", "OCU_SANDBOX_DNS"}:
            if "\n" in value or "\r" in value or "\x00" in value:
                raise RecoveryError(f"{path}:{index}: malformed assignment")
        if name in values:
            raise RecoveryError(f"{path}:{index}: duplicate assignment {name}")
        values[name] = value
    return values


def render_dotenv(values: dict[str, str]) -> str:
    lines = []
    for name, value in values.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise RecoveryError(f"unsafe configuration name {name}")
        if "\n" in value or "\r" in value or "\x00" in value:
            raise RecoveryError(f"unsafe configuration value for {name}")
        lines.append(f"{name}={value}")
    return "\n".join(lines) + "\n"


def redact_secrets(text: str, secrets: list[str]) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "<redacted>")
    return redacted


def collect_secrets(values: dict[str, str]) -> list[str]:
    secrets = []
    for name, value in values.items():
        tokens = f"_{name.upper()}_"
        if any(f"_{marker}_" in tokens for marker in SECRET_MARKERS) or name in PROVIDER_KEYS:
            if value:
                secrets.append(value)
    return secrets


def require_regular_file(path: Path) -> None:
    release.require_regular_file(path)


def confined_component_path(root: Path, relative: str) -> Path:
    text = str(relative or "")
    if not text or text.startswith("/") or Path(text).is_absolute() or ".." in Path(text).parts:
        raise RecoveryError(f"unsafe recovery component path {relative}")
    path = root
    for part in Path(text).parts:
        path = path / part
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise RecoveryError(f"{path}: symbolic links are not allowed")
    try:
        relative_text = path.relative_to(root).as_posix()
    except ValueError as cop:
        raise RecoveryError(f"{path}: escapes recovery set {root}") from cop
    if relative_text.startswith("/") or ".." in Path(relative_text).parts:
        raise RecoveryError(f"{path}: unsafe recovery component path")
    require_regular_file(path)
    return path

def destination_occupied(dest: Path) -> bool:
    return release.destination_occupied(dest)


def exclusive_publish(stage: Path, dest: Path, lock_path: Path | None = None) -> None:
    release.publish_exclusive(stage, dest, lock_path)


def compose_name(project: str, service: str) -> str:
    return f"{project}-{service}-1"


def webui_volume_name(project: str) -> str:
    return f"{project}_open-webui-data"


def postgres_volume_name(project: str) -> str:
    return f"{project}_postgres-data"


def workspace_volume_name(chat_id: str) -> str:
    return f"chat-{chat_id}-workspace"


def sandbox_container_name(chat_id: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_.-]", "-", chat_id)
    return f"owui-chat-{sanitized}"


def canonical_chat_id(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text or ".." in text or "/" in text or "\\" in text or "\x00" in text:
        raise RecoveryError(f"invalid chat id {value!r}")
    return text


def helper_env() -> dict[str, str]:
    env = os.environ.copy()
    host = pin_runtime_docker_host()
    env["DOCKER_HOST"] = host
    env.pop("DOCKER_CONTEXT", None)
    return env


def run_up(script: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    merged = dict(env)
    merged["DOCKER_HOST"] = pin_runtime_docker_host()
    merged.pop("DOCKER_CONTEXT", None)
    return subprocess.run(
        ["bash", str(script)],
        cwd=str(script.parent.parent),
        capture_output=True,
        text=True,
        check=False,
        env=merged,
    )


def cmd_backup(args: argparse.Namespace) -> int:
    from recovery_resources import backup_deployment

    try:
        require_root()
        published = backup_deployment(
            deploy_root=Path(args.deploy_root),
            destination=Path(args.destination),
            runtime_file=Path(args.runtime_file) if args.runtime_file else None,
        )
    except (RecoveryError, release.ReleaseError) as cop:
        fail(str(cop))
    report(f"published complete recovery set {published}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    from recovery_resources import verify_recovery_set

    try:
        manifest = verify_recovery_set(Path(args.recovery_set))
    except (RecoveryError, release.ReleaseError) as cop:
        fail(str(cop))
    report(f"recovery set {args.recovery_set} is complete")
    print(json.dumps({"source_daemon": manifest["source_daemon"], "components": sorted(manifest["components"])}))
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    from recovery_resources import restore_deployment

    try:
        require_root()
        owned = restore_deployment(
            recovery_set=Path(args.recovery_set),
            destination_root=Path(args.destination_root),
            provider_file=Path(args.provider_file),
            retained_delivery=Path(args.retained_delivery) if args.retained_delivery else None,
            docker_host=args.docker_host,
        )
    except (RecoveryError, release.ReleaseError) as cop:
        fail(str(cop))
    report("restored into empty target; application remains stopped")
    print(json.dumps({"owned_resources": owned}))
    return 0


def cmd_activate(args: argparse.Namespace) -> int:
    from recovery_resources import activate_release

    try:
        require_root()
        activate_release(
            destination_root=Path(args.destination_root),
            retained_delivery=Path(args.retained_delivery),
            docker_host=args.docker_host,
        )
    except (RecoveryError, release.ReleaseError) as cop:
        fail(str(cop))
    report("selected release started; complete the operator readiness checklist before traffic switch")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("backup", help="capture a complete cold recovery set")
    backup.add_argument("--deploy-root", required=True)
    backup.add_argument("--destination", required=True)
    backup.add_argument("--runtime-file")
    backup.set_defaults(func=cmd_backup)
    verify = sub.add_parser("verify", help="verify a published recovery set")
    verify.add_argument("--recovery-set", required=True)
    verify.set_defaults(func=cmd_verify)
    restore = sub.add_parser("restore", help="restore a complete set onto an empty target")
    restore.add_argument("--recovery-set", required=True)
    restore.add_argument("--destination-root", required=True)
    restore.add_argument("--provider-file", required=True)
    restore.add_argument("--retained-delivery")
    restore.add_argument("--docker-host", default=SUPPORTED_DOCKER_HOST)
    restore.set_defaults(func=cmd_restore)
    activate = sub.add_parser("activate", help="activate a retained previous release")
    activate.add_argument("--destination-root", required=True)
    activate.add_argument("--retained-delivery", required=True)
    activate.add_argument("--docker-host", default=SUPPORTED_DOCKER_HOST)
    activate.set_defaults(func=cmd_activate)
    return parser


def main(argv: list[str] | None = None) -> int:
    def cancel(signum, _frame):
        for sig in CANCELLATION_SIGNALS:
            signal.signal(sig, signal.SIG_IGN)
        raise SystemExit(128 + signum)

    previous = {sig: signal.signal(sig, cancel) for sig in CANCELLATION_SIGNALS}
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        return args.func(args)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
