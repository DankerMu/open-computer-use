# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""deploy/compose-snapshot.sh against real Docker Compose, with fake values only (#691)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid

from support import ROOT
from test_overlay_structure import native_environment

HELPER = ROOT / "deploy" / "compose-snapshot.sh"
OVERLAY = ROOT / "deploy" / "production-like-test"
# Images commonly present on a developer host; each runs a non-default command as given.
LOCAL_IMAGES = ("alpine", "busybox", "nginx:alpine", "redis:8-alpine", "postgres:18-alpine",
                "postgres:17-alpine")
FAKE_VALUE = "fake$KEY"
# Minimal run_owned/wait_owned with up.sh's contract: background child, OWNED_PID, wait.
DRIVER = r"""
set -euo pipefail
OWNED_PID=""
wait_owned() {
    local pid=$1
    local status=0
    wait "$pid" || status=$?
    OWNED_PID=""
    return "$status"
}
run_owned() {
    "$@" &
    OWNED_PID=$!
    wait_owned "$OWNED_PID"
}
CONFIG_DIR=$1
helper=$2
shift 2
. "$helper"
while (( $# )); do
    stack=$1 project=$2 project_dir=$3 count=$4
    shift 4
    resolve "$CONFIG_DIR/$stack.json" "${@:1:count}"
    check_snapshot "$stack" "$project" "$project_dir" "${@:1:count}"
    shift "$count"
done
"""


def compose_available() -> bool:
    try:
        return subprocess.run(["docker", "compose", "version"], capture_output=True,
                              timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ambient_environment(**extra: str) -> dict[str, str]:
    """The caller's Docker client settings, without inherited Compose or test values."""
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("COMPOSE_") and key not in {"FAKE", "KEY"}}
    environment.update(extra)
    return environment


def run_helper(config_dir: Path, stacks, environment: dict[str, str]):
    argv = ["bash", "-c", DRIVER, "driver", str(config_dir), str(HELPER)]
    for stack, project, project_dir, compose_args in stacks:
        argv.extend([stack, project, str(project_dir), str(len(compose_args)), *compose_args])
    return subprocess.run(argv, cwd=config_dir, env=environment, capture_output=True,
                          text=True, timeout=120)


def daemon_reachable(environment: dict[str, str]) -> bool:
    try:
        return subprocess.run(["docker", "info"], env=environment, capture_output=True,
                              timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def freeze_document(value):
    """What main's removed `freeze` step wrote: every `$` escaped once more."""
    if isinstance(value, str):
        return value.replace("$", "$$")
    if isinstance(value, list):
        return [freeze_document(item) for item in value]
    if isinstance(value, dict):
        return {key: freeze_document(item) for key, item in value.items()}
    return value


def config_hash(environment: dict[str, str], project_dir: Path, empty: Path, snapshot: Path) -> list[str]:
    """Sorted `service hash` lines of a snapshot loaded as deploy/up.sh starts it."""
    result = subprocess.run(
        ["docker", "compose", "-p", "ocu-test", "--project-directory", str(project_dir),
         "--env-file", str(empty), "-f", str(snapshot), "config", "--hash", "*"],
        env=environment, capture_output=True, text=True, timeout=120,
    )
    if result.returncode:
        raise AssertionError(f"compose config --hash failed: {result.stderr}")
    return sorted(result.stdout.splitlines())


REAL_STACKS = (
    ("core", ROOT, ["--project-directory", str(ROOT), "-f", str(ROOT / "docker-compose.yml"),
                    "-f", str(OVERLAY / "compose.core.override.yml")]),
    ("webui", ROOT, ["--project-directory", str(ROOT), "-f", str(ROOT / "docker-compose.webui.yml"),
                     "-f", str(OVERLAY / "compose.webui.override.yml")]),
    ("proxy", OVERLAY, ["--project-directory", str(OVERLAY), "-f", str(OVERLAY / "compose.proxy.yml")]),
)
REAL_SERVICES = {
    "core": {"workspace", "computer-use-server", "retention-guard", "documentserver"},
    "webui": {"open-webui", "postgres", "open-webui-init"},
    "proxy": {"proxy", "messages-ingress"},
}


def local_image(environment: dict[str, str]) -> str | None:
    for image in LOCAL_IMAGES:
        result = subprocess.run(["docker", "image", "inspect", image], env=environment,
                                capture_output=True, timeout=30)
        if result.returncode == 0:
            return image
    return None


@unittest.skipUnless(compose_available(), "docker compose version failed: no real Compose to test")
class RealComposeSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.context = tempfile.TemporaryDirectory(prefix="ocu-compose-snapshot-")
        self.dir = Path(self.context.name)
        self.config = self.dir / "config"
        self.config.mkdir(mode=0o700)
        (self.config / "empty.env").write_text("", encoding="utf-8")

    def tearDown(self):
        self.context.cleanup()

    def printenv(self, environment, project: str, snapshot: Path) -> str:
        base = ["docker", "compose", "-p", project, "--project-directory", str(self.dir),
                "--env-file", str(self.config / "empty.env"), "-f", str(snapshot)]
        try:
            result = subprocess.run([*base, "run", "--rm", "--no-deps", "--pull", "never", "-T",
                                     "probe", "printenv", "K"],
                                    cwd=self.dir, env=environment, capture_output=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.decode("utf-8")
        finally:
            subprocess.run([*base, "down", "--remove-orphans"], cwd=self.dir, env=environment,
                           capture_output=True, timeout=120)

    def test_dollar_value_reaches_the_container_unchanged(self):
        # KEY is set so that a second interpolation would show up as a different value.
        environment = ambient_environment(FAKE=FAKE_VALUE, KEY="hostile-value")
        if not daemon_reachable(environment):
            self.skipTest("docker info failed: no reachable Docker daemon to run a container")
        image = local_image(environment)
        if image is None:
            self.skipTest(f"no local image among {', '.join(LOCAL_IMAGES)}; --pull never cannot run one")
        (self.dir / "compose.yml").write_text(
            f"services:\n  probe:\n    image: {image}\n    environment:\n      K: ${{FAKE}}\n",
            encoding="utf-8",
        )
        project = f"ocu-snapshot-{uuid.uuid4().hex[:12]}"
        args = ["-p", project, "--project-directory", str(self.dir), "-f", str(self.dir / "compose.yml")]
        result = run_helper(self.config, [("probe", project, self.dir, args)], environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = self.config / "probe.up.json"
        self.assertEqual(snapshot.read_bytes(), (self.config / "probe.json").read_bytes())
        self.assertEqual(self.printenv(environment, project, snapshot), FAKE_VALUE + "\n")

        # Control: the snapshot re-escaped as the removed freeze step did loads doubled.
        frozen = self.config / "frozen.up.json"
        frozen.write_text(json.dumps(freeze_document(json.loads(snapshot.read_text(encoding="utf-8")))),
                          encoding="utf-8")
        self.assertEqual(self.printenv(environment, project, frozen), "fake$$KEY\n")

    def test_re_escaped_snapshot_is_refused(self):
        environment = ambient_environment(FAKE=FAKE_VALUE, KEY="hostile-value")
        (self.dir / "compose.yml").write_text(
            "services:\n  probe:\n    image: probe:none\n    environment:\n      K: ${FAKE}\n",
            encoding="utf-8",
        )
        project = f"ocu-snapshot-{uuid.uuid4().hex[:12]}"
        args = ["-p", project, "--project-directory", str(self.dir), "-f", str(self.dir / "compose.yml")]
        # A docker whose config output keeps `$` single, as a non-faithful Compose would.
        real_docker = subprocess.run(["bash", "-c", "command -v docker"], capture_output=True,
                                     text=True, env=environment, check=True).stdout.strip()
        shim = self.dir / "bin"
        shim.mkdir()
        (shim / "docker").write_text(
            "#!/usr/bin/env bash\n"
            'if [[ " $* " == *" config --format json "* ]]; then\n'
            f'    "{real_docker}" "$@" | sed \'s/\\$\\$/$/g\'\n'
            '    exit "${PIPESTATUS[0]}"\n'
            "fi\n"
            f'exec "{real_docker}" "$@"\n',
            encoding="utf-8",
        )
        (shim / "docker").chmod(0o755)
        environment["PATH"] = f"{shim}{os.pathsep}{environment['PATH']}"
        result = run_helper(self.config, [("probe", project, self.dir, args)], environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "deploy: the probe snapshot does not load as its source configuration; refusing to start",
            result.stderr.splitlines(),
        )

    def resolve_real_stacks(self, extra: dict[str, str]) -> dict[str, str]:
        """Run the helper on the real core, webui and proxy stacks; only `config` runs."""
        environment = native_environment(self.dir, "true", {
            **extra, "COMPOSE_PROFILES": "", "COMPOSE_REMOVE_ORPHANS": "false",
        })
        # docker compose is a CLI plugin found through the caller's Docker configuration.
        for name in ("HOME", "DOCKER_CONFIG"):
            if name in os.environ:
                environment[name] = os.environ[name]
            else:
                environment.pop(name, None)
        for name in ("OCU_RELEASE_FONTS_DIR", "OCU_OFFICE_FONTS_DIR", "OCU_PROMPTS_DIR",
                     "OCU_CHAT_DATA_DIR", "OCU_SKILLS_CACHE_DIR"):
            Path(environment[name]).mkdir(parents=True, exist_ok=True)
        stacks = [(stack, "ocu-test", project_dir, ["-p", "ocu-test", *args])
                  for stack, project_dir, args in REAL_STACKS]
        result = run_helper(self.config, stacks, environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        for stack, services in REAL_SERVICES.items():
            lines = (self.config / f"{stack}.src.hash").read_text(encoding="utf-8").splitlines()
            self.assertEqual({line.split()[0] for line in lines}, services, stack)
        return environment

    def test_real_stacks_load_their_snapshots_unchanged(self):
        self.resolve_real_stacks({
            "DMXAPI_API_KEY": FAKE_VALUE,
            "OCU_INTERNAL_TOKEN": "fake$$token$",
            "KEY": "hostile-value",
        })
        for stack in REAL_SERVICES:
            with self.subTest(stack=stack):
                self.assertEqual((self.config / f"{stack}.up.hash").read_text(encoding="utf-8"),
                                 (self.config / f"{stack}.src.hash").read_text(encoding="utf-8"))
        webui = json.loads((self.config / "webui.up.json").read_text(encoding="utf-8"))
        self.assertEqual(webui["services"]["open-webui"]["environment"]["OPENAI_API_KEY"], "fake$$KEY")

    def test_dollar_free_stacks_hash_as_the_previous_snapshot_did(self):
        # Without `$` the byte-copy snapshot loads as main's frozen one: nothing is recreated.
        environment = self.resolve_real_stacks({})
        empty = self.config / "empty.env"
        for stack, project_dir, _args in REAL_STACKS:
            with self.subTest(stack=stack):
                resolved = (self.config / f"{stack}.json").read_text(encoding="utf-8")
                self.assertNotIn("$", resolved)
                frozen = self.config / f"{stack}.frozen.up.json"
                frozen.write_text(json.dumps(freeze_document(json.loads(resolved))), encoding="utf-8")
                previous = config_hash(environment, project_dir, empty, frozen)
                current = config_hash(environment, project_dir, empty, self.config / f"{stack}.up.json")
                self.assertEqual(len(current), len(REAL_SERVICES[stack]))
                self.assertEqual(current, previous)

if __name__ == "__main__":
    unittest.main()
