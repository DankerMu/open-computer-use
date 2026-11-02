# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Static pins for the offline Remotion template, its skill and its notices.

The image-level behaviour (offline still, ownership, Headless Shell present)
is checked by tests/test-docker-image.sh against a built image; this module
pins what the build consumes.

Run:
    uv run --no-project --with pytest --with-requirements computer-use-server/requirements.txt -- python -m pytest tests/test_remotion_image.py -q --import-mode=importlib
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "remotion-template"
SKILL_DIR = ROOT / "skills" / "public" / "remotion"
VENDORED = SKILL_DIR / "remotion-best-practices"
MANIFEST = ROOT / "tests" / "fixtures" / "remotion-best-practices.sha256"

REMOTION_VERSION = "4.0.537"
REMOTION_PACKAGES = (
    "remotion",
    "@remotion/captions",
    "@remotion/cli",
    "@remotion/effects",
    "@remotion/fonts",
    "@remotion/gif",
    "@remotion/layout-utils",
    "@remotion/lottie",
    "@remotion/media",
    "@remotion/media-utils",
    "@remotion/motion-blur",
    "@remotion/paths",
    "@remotion/rough-notation",
    "@remotion/shapes",
    "@remotion/three",
    "@remotion/transitions",
    "@remotion/zod-types",
)
EXPECTED_DEPENDENCIES = {
    **{name: REMOTION_VERSION for name in REMOTION_PACKAGES},
    "@react-three/fiber": "9.8.2",
    "lottie-web": "5.13.0",
    "react": "19.2.3",
    "react-dom": "19.2.3",
    "three": "0.186.1",
    "zod": "4.5.4",
}
# Need network at render time: font CDN, transcription models, Rive WASM from
# unpkg, AWS rendering.
EXCLUDED_PACKAGES = (
    "@remotion/google-fonts",
    "@remotion/whisper-webgpu",
    "@remotion/rive",
    "@remotion/lambda",
)

HEADLESS_SHELL_VERSION = "157.0.8080.0-remotion-v3"
HEADLESS_SHELL_SHA256 = {
    "chrome-headless-shell": "6499ae333be19f27484c73a61da262754a02ab3d82bbba856f44bef785ec3e62",
    "libEGL.so": "63c23d1cb8444e28b61db90c5867aca54272c0f2acdca3fb5fb0e50c7d5a280a",
    "libGLESv2.so": "3fab23c6e868449d3a04274ff45ea2a432fa1d7f1811b1b49eb2403299a4255f",
    "libvk_swiftshader.so": "9b27ad17f1c92e941f2929661e0f8a03933f565abbf5f2e16848ce65150eb5b9",
    "libvulkan.so.1": "4fbaaab71a65d9393b3f38e72dfcee707620beacc532ac354b0360934cdf5f0c",
    "vk_swiftshader_icd.json": "c9eef28b6b984fec220ef0abfecc40b502d46946706e47bfe97707027cb818bd",
}

LICENSE_URL = "https://www.remotion.dev/license"
LICENSE_TIERS = (
    "individuals",
    "up to 3 employees",
    "non-profit",
    "Company License",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _words(path: Path) -> str:
    """Text with whitespace runs collapsed, for hard-wrapped notices."""
    return " ".join(_read(path).split())


def _dockerfile_instructions() -> list[str]:
    """Dockerfile instructions with line continuations joined."""
    instructions: list[str] = []
    current = ""
    for raw in _read(ROOT / "Dockerfile").splitlines():
        line = raw.rstrip()
        if not current and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if line.endswith("\\"):
            current += line[:-1] + "\n"
            continue
        instructions.append(current + line)
        current = ""
    return instructions


def _template_run() -> str:
    runs = [i for i in _dockerfile_instructions() if "remotion browser ensure" in i]
    assert len(runs) == 1, runs
    return runs[0]


# --- template ---------------------------------------------------------------


def test_template_dependencies_are_exact_pins() -> None:
    package = json.loads(_read(TEMPLATE / "package.json"))
    assert package["private"] is True
    assert package["dependencies"] == EXPECTED_DEPENDENCIES
    assert "devDependencies" not in package
    for name in EXCLUDED_PACKAGES:
        assert name not in package["dependencies"]


def test_template_lockfile_matches_the_pins() -> None:
    lock = json.loads(_read(TEMPLATE / "package-lock.json"))
    packages = lock["packages"]
    assert lock["lockfileVersion"] == 3
    assert packages[""]["dependencies"] == EXPECTED_DEPENDENCIES
    for name, version in EXPECTED_DEPENDENCIES.items():
        assert packages[f"node_modules/{name}"]["version"] == version, name
    for key, entry in packages.items():
        if not key:
            continue
        name = key.rsplit("node_modules/", 1)[1]
        if name == "remotion" or name.startswith("@remotion/"):
            assert entry["version"] == REMOTION_VERSION, key
        assert name not in EXCLUDED_PACKAGES, key
        assert entry["resolved"].startswith("https://registry.npmjs.org/"), key
        assert entry["integrity"].startswith("sha512-"), key


def test_template_sources_present_and_cache_disabled() -> None:
    for relative in ("src/index.ts", "src/Root.tsx", "remotion.config.ts", "tsconfig.json"):
        assert (TEMPLATE / relative).is_file(), relative
    json.loads(_read(TEMPLATE / "tsconfig.json"))
    assert "registerRoot(" in _read(TEMPLATE / "src/index.ts")
    assert "<Composition" in _read(TEMPLATE / "src/Root.tsx")
    config = _read(TEMPLATE / "remotion.config.ts")
    assert "Config.setCachingEnabled(false);" in config
    assert not (TEMPLATE / "node_modules").exists()


# --- Dockerfile -------------------------------------------------------------


def test_dockerfile_installs_template_before_skills() -> None:
    instructions = _dockerfile_instructions()
    copy = "COPY --chown=root:root remotion-template/ /opt/remotion-template/"
    run = _template_run()
    skills = "COPY --chown=root:root ./skills /mnt/skills/"
    assert instructions.index(copy) < instructions.index(run) < instructions.index(skills)
    assert "npm ci " in run
    assert "npx --no-install remotion browser ensure" in run
    assert "chmod -R a+rX /opt/remotion-template" in run


def test_dockerfile_pins_headless_shell_version_and_sha256() -> None:
    run = _template_run()
    assert f'= "{HEADLESS_SHELL_VERSION}"' in run
    assert "chrome-headless-shell/linux64/chrome-headless-shell-linux64" in run
    pinned = dict(
        (name, digest)
        for digest, name in re.findall(r"'([0-9a-f]{64})  ([^']+)'", run)
    )
    assert pinned == HEADLESS_SHELL_SHA256
    assert "sha256sum -c" in run
    assert f'"$(find . -type f | wc -l)" = "{len(HEADLESS_SHELL_SHA256)}"' in run


# --- skill ------------------------------------------------------------------


def test_vendored_skill_is_unchanged() -> None:
    expected: dict[str, str] = {}
    for line in _read(MANIFEST).splitlines():
        digest, relative = line.split("  ", 1)
        expected[relative] = digest
    actual = {
        str(path.relative_to(VENDORED)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in VENDORED.rglob("*")
        if path.is_file() and path.relative_to(VENDORED) != Path("SOURCE.md")
    }
    assert len(expected) == 144
    assert actual == expected


def test_source_md_names_origin_and_license() -> None:
    source = _read(VENDORED / "SOURCE.md")
    for needle in (
        "remotion-dev/remotion",
        "packages/skills",
        "v4.0.537",
        "LICENSE.md",
        "Remotion License",
        LICENSE_URL,
        "REFERENCE.md",
    ):
        assert needle in source, needle


def test_skill_md_is_ours_and_mit() -> None:
    skill = _read(SKILL_DIR / "SKILL.md")
    assert skill.startswith("---\nname: remotion\n")
    assert "SPDX-License-Identifier: MIT" in skill


def test_skill_md_states_the_sandbox_rules() -> None:
    skill = _read(SKILL_DIR / "SKILL.md")
    for needle in (
        "/opt/remotion-template",
        "/home/assistant/videos/",
        "--exclude=node_modules",
        "ln -s /opt/remotion-template/node_modules node_modules",
        "npx --no-install remotion",
        "create-video",
        "npm install",
        "remotion add",
        "--license-key",
        "Studio",
        "Google Fonts",
        "npx --no-install remotion still",
        "nohup npx --no-install remotion render",
        "> render.log 2>&1 &",
        "COMMAND_TIMEOUT",
        "--concurrency=2",
        "--gl=swangle",
        "--codec=h264 --pixel-format=yuv420p",
        "/mnt/user-data/files/",
        "30 days",
        "generate_speech",
        "when available",
        "staticFile(",
        "remotion-best-practices/SKILL.md",
        "these rules win",
    ):
        assert needle in skill, needle
    for name in (*EXPECTED_DEPENDENCIES, *EXCLUDED_PACKAGES):
        assert f"`{name}`" in skill, name


# --- notices ----------------------------------------------------------------


def test_notice_names_remotion_and_license_tiers() -> None:
    notice = _words(ROOT / "NOTICE")
    for needle in (
        "skills/public/remotion/SKILL.md",
        "skills/public/remotion/remotion-best-practices/",
        "Remotion 4.0.537",
        "Chrome Headless Shell",
        LICENSE_URL,
        *LICENSE_TIERS,
    ):
        assert needle in notice, needle


def test_third_party_licenses_lists_remotion_entries() -> None:
    licenses = _words(ROOT / "THIRD-PARTY-LICENSES.md")
    for needle in (
        "Remotion 4.0.537",
        "skills/public/remotion/remotion-best-practices/",
        "Chrome Headless Shell 157.0.8080.0",
        "Remotion License",
        LICENSE_URL,
        *LICENSE_TIERS,
    ):
        assert needle in licenses, needle


def test_skills_readme_has_remotion_licensing_row() -> None:
    rows = [
        line for line in _read(ROOT / "skills" / "README.md").splitlines()
        if line.startswith("|") and "public/remotion/" in line
    ]
    assert len(rows) == 1
    assert "Remotion License" in rows[0]
    assert "public/remotion/SKILL.md" in rows[0]
