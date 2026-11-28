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
ROUTER = SKILL_DIR / "SKILL.md"
KIT_REFERENCE = SKILL_DIR / "explainer-kit.md"
FREE_FORM = SKILL_DIR / "free-form.md"
SKILL_FILES = (ROUTER, KIT_REFERENCE, FREE_FORM)
KIT_CLI = TEMPLATE / "bin" / "kit.mjs"
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
    "evaluating",
    "Company License",
)
SKILL_ROUTE = "/mnt/skills/public/remotion/remotion-best-practices/SKILL.md"
ROUTER_MAX_LINES = 80
# The router's eight explainer steps (spec "The video skill routes explainers
# to the kit"): each step's line holds every needle, a command needle matched
# so that no [a-z-] follows it.
ROUTER_STEPS = (
    ("node /opt/remotion-template/bin/kit.mjs new <name>", "cd /home/assistant/videos/<name>"),
    ("script.json", "explainer-kit.md"),
    ('node bin/kit.mjs image "<path>"',),
    ("node bin/kit.mjs voice", "generate_speech", "node bin/kit.mjs voice-import"),
    ("node bin/kit.mjs timeline",),
    ("node bin/kit.mjs check", "node bin/kit.mjs status"),
    ("`view`", 'on any "no"', "check again"),
    ("node bin/kit.mjs render", "node bin/kit.mjs status"),
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _bounded(needle: str) -> re.Pattern[str]:
    """The needle, as a word: no [a-z-] follows it, and a word needle has no
    word character before it."""
    before = r"(?<![\w-])" if needle[0].isalnum() else ""
    return re.compile(before + re.escape(needle) + r"(?![a-z-])")


def _kit_usages() -> dict[str, str]:
    """COMMANDS in bin/kit.mjs, parsed from its text: name -> usage."""
    source = _read(KIT_CLI)
    block = re.search(r"^const COMMANDS = \{\n(.*?)^\};$", source, re.DOTALL | re.MULTILINE)
    assert block, "bin/kit.mjs has no `const COMMANDS = {...};` literal"
    entries = re.findall(
        r"""^  (?:"([a-z-]+)"|([a-z-]+)): \{ usage: (?:"([^"]*)"|'([^']*)'),""",
        block.group(1),
        re.MULTILINE,
    )
    usages = {quoted or bare: double or single for quoted, bare, double, single in entries}
    assert len(usages) == len(block.group(1).strip().splitlines()) == 9, usages
    return usages


def _code_spans(text: str) -> list[str]:
    """The contents of the Markdown code spans in text, any fence length."""
    return [m.group(2) for m in re.finditer(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", text)]


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


def test_template_runs_no_install_scripts_but_esbuild() -> None:
    package = json.loads(_read(TEMPLATE / "package.json"))
    assert "scripts" not in package
    packages = json.loads(_read(TEMPLATE / "package-lock.json"))["packages"]
    scripted = sorted(key for key, entry in packages.items() if entry.get("hasInstallScript"))
    assert scripted == ["node_modules/esbuild"]


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


def test_dockerfile_installs_music_library_before_template() -> None:
    instructions = _dockerfile_instructions()
    models = "RUN chmod +x /usr/local/bin/list-subagent-models"
    copy = "COPY --chown=root:root deploy/music/ /tmp/ocu-music/deploy/music/"
    template = "COPY --chown=root:root remotion-template/ /opt/remotion-template/"
    fetch = "python3 -B /tmp/ocu-music/deploy/music/music_library.py fetch --out /tmp/ocu-music/sources"
    build = (
        "python3 -B /tmp/ocu-music/deploy/music/music_library.py build"
        " --sources /tmp/ocu-music/sources --out /opt/video-music"
    )
    commands = [fetch, build, "rm -rf /tmp/ocu-music", "chmod -R a+rX,go-w /opt/video-music"]
    for line in (models, copy, template):
        assert line in instructions, line
    step = instructions.index(copy)
    run = instructions[step + 1]
    assert run.startswith("RUN "), run
    assert [part.strip() for part in run[len("RUN "):].split("&&")] == commands, run
    assert instructions[step - 1] == models
    assert instructions[step + 2] == template
    assert run.index(fetch) < run.index(build)
    assert run.split("&&")[1].strip().endswith(" --out /opt/video-music"), run
    assert "rm -rf /tmp/ocu-music" in run


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
        "remotion-markup/remotion-maps/",
    ):
        assert needle in source, needle


def test_claude_md_lists_skill_md_as_mit() -> None:
    rule = next(
        line for line in _read(ROOT / "CLAUDE.md").splitlines()
        if "SPDX-License-Identifier: MIT" in line
    )
    for path in SKILL_FILES:
        assert f"`{path.relative_to(ROOT)}`" in rule, path.name


def test_skill_md_is_ours_and_mit() -> None:
    assert _read(ROUTER).startswith("---\nname: remotion\n")
    for path in SKILL_FILES:
        assert "SPDX-License-Identifier: MIT" in _read(path), path.name


def test_skill_md_states_the_sandbox_rules() -> None:
    skill = _read(FREE_FORM)
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
        "--public-license-key",
        "Config.setPublicLicenseKey",
        "Never start Studio.",
        "Google Fonts",
        "COMMAND_TIMEOUT",
        "--concurrency=2",
        "--gl=swangle",
        "--codec=h264 --pixel-format=yuv420p",
        "/mnt/user-data/files/",
        "30 days",
        "generate_speech",
        "when available",
        "staticFile(",
        SKILL_ROUTE,
        "these rules win",
        "default idle stop is about 10 minutes; deployments may set it longer",
    ):
        assert needle in skill, needle
    for name in (*EXPECTED_DEPENDENCIES, *EXCLUDED_PACKAGES):
        assert f"`{name}`" in skill, name


def test_skill_md_overrides_vendored_conflicts() -> None:
    skill = _read(FREE_FORM)
    for needle in (
        "--gl=angle",
        "Config.setChromiumOpenGlRenderer('swangle')",
        "`cylinder()`",
        "`sphere()`",
        "`pageTurn()`",
        "remotion-docs/REFERENCE.md",
        "node_modules/<pkg>/dist/*.d.ts",
        "remotion-captions/transcribe-captions.md",
        "remotion-markup/voiceover.md",
        "ElevenLabs",
    ):
        assert needle in skill, needle


def test_skill_md_check_frames_go_to_project_out() -> None:
    skill = _read(FREE_FORM)
    assert (
        "npx --no-install remotion render src/index.ts Main out/frames --sequence"
        in skill
    )
    assert "--image-format=png" in skill
    assert "`view` tool" in skill
    assert "rm -rf out/frames" in skill
    assert "durationInFrames - 1" in skill


def test_skill_md_background_render_reports_its_exit() -> None:
    skill = _read(FREE_FORM)
    for needle in (
        "rm -f /mnt/user-data/files/<name>.mp4",
        "setsid nohup bash -c 'npx --no-install remotion render src/index.ts Main",
        "; echo \"remotion exit $?\"' > render.log 2>&1 < /dev/null &",
        "echo $! > render.pid",
        'sleep 100; tail -n 3 render.log; grep "remotion exit" render.log || '
        '{ kill -0 "$(cat render.pid)" 2>/dev/null && echo running || echo "render died without exit"; }',
        "cd /home/assistant/videos/<name> && npx --no-install remotion still",
        "Do not sleep in the same call that launches the render",
        "tail -n 50 render.log",
        "delayRender",
        "--concurrency=1",
    ):
        assert needle in skill, needle
    assert "pgrep" not in skill


# --- router and kit reference ----------------------------------------------


def test_router_lists_the_kit_steps_in_order() -> None:
    router = _read(ROUTER)
    lines = router.splitlines()
    assert len(lines) <= ROUTER_MAX_LINES, f"SKILL.md has {len(lines)} lines, at most {ROUTER_MAX_LINES}"
    steps = [(int(m.group(1)), line) for line in lines if (m := re.match(r"(\d+)\. ", line))]
    assert [number for number, _ in steps] == list(range(1, len(ROUTER_STEPS) + 1)), steps
    for (number, line), needles in zip(steps, ROUTER_STEPS):
        for needle in needles:
            assert _bounded(needle).search(line), f"step {number} lacks {needle!r}: {line}"
    assert "sandbox has no internet access" in router
    assert "/mnt/user-data/files/" in router


def test_router_names_the_workflows_and_ties_the_vendored_guidance_to_custom() -> None:
    router = _read(ROUTER)
    for needle in ("free-form.md", "explainer-kit.md", "course", "promo", "report"):
        assert _bounded(needle).search(router), needle
    vendored = [line for line in router.splitlines() if "remotion-best-practices" in line]
    assert len(vendored) == 1 and router.count("remotion-best-practices") == 1, vendored
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", vendored[0]) if "remotion-best-practices" in s]
    assert len(sentences) == 1 and "`custom`" in sentences[0], vendored


# Kit commands the skill text does not teach yet. `data` exits when task 6.2
# (DankerMu/open-webui#975) documents it in report-kit.md.
UNDOCUMENTED_KIT_COMMANDS = {"data"}


def test_skill_names_every_kit_command_and_no_other() -> None:
    usages = _kit_usages()
    text = {path.name: _read(path) for path in (ROUTER, KIT_REFERENCE)}
    missing = [
        usage
        for name, usage in usages.items()
        if name not in UNDOCUMENTED_KIT_COMMANDS and not any(_bounded(usage).search(t) for t in text.values())
    ]
    assert missing == []
    unknown = [
        f"{name}: node bin/kit.mjs {word}"
        for name, t in text.items()
        for word in re.findall(r"node bin/kit\.mjs ([a-z-]+)", t)
        if word not in usages
    ]
    # The shorthand `kit <command>`, read inside code spans only, so prose
    # such as "the kit" is not a command.
    unknown += [
        f"{name}: kit {word}"
        for name, t in text.items()
        for span in _code_spans(t)
        for word in re.findall(r"(?<![\w/.-])kit ([a-z-]+)", span)
        if word not in usages
    ]
    assert unknown == []


def test_kit_reference_states_the_facts_the_kit_enforces() -> None:
    reference = _read(KIT_REFERENCE)
    lines = reference.splitlines()
    for code, states in (("0", ("passed", "succeeded")), ("1", ("failed", "died")), ("3", ("running",)), ("4", ("no check has run",))):
        assert any(
            re.search(rf"`{code}`", line) and all(state in line for state in states) for line in lines
        ), f"no line gives exit {code} with {states}"
    for needle in (
        "COMMAND_TIMEOUT",
        "`formatCount`",
        "at most 8 code points",
        "below 1e12",
        "at most 2 decimals",
        "src/custom/",
        "default export",
        "/opt/video-music/manifest.json",
        "Synced image:",
    ):
        assert needle in reference, needle
    assert "ENTRANCE_FRAMES" in reference or "15-frame entrance" in reference


# --- notices ----------------------------------------------------------------


def test_notice_names_remotion_and_license_tiers() -> None:
    notice = _words(ROOT / "NOTICE")
    for needle in (
        *(str(path.relative_to(ROOT)) for path in SKILL_FILES),
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
    for path in SKILL_FILES:
        assert f"public/remotion/{path.name}" in rows[0], path.name
