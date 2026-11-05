# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""An image from generate_image / edit_image is shown once (DankerMu/open-webui#724).

The prompt asks for every workspace image to be embedded with Markdown image
syntax, and Open WebUI saves generated images into the workspace, so the model
showed them a second time. The exception paragraph right after the image rule
stops that; the rule itself and the good examples stay as they were.

Seams: the prompt builders, the renderer with a chat id, and the real FastAPI
`/system-prompt` route. Only the skills provider is stubbed (an HTTP
dependency of skill_manager, not the system under test).
"""

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "computer-use-server"))

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import skill_manager  # noqa: E402
import system_prompt  # noqa: E402

TOKEN = "ocu-test-internal-token"
CHAT = "0b7d3c51-8a2e-4f6d-9c1b-2e3f4a5b6c7d"
EMAIL = "person@example.test"
RULE = (
    "For IMAGE files (screenshots, charts, diagrams, photos), use markdown image syntax "
    "`![description](URL)` instead of regular links so images render inline. "
    "Image extensions: .png, .jpg, .jpeg, .gif, .webp, .svg, .bmp"
)
EXCEPTION = (
    "Exception: an image that the image generation tools (`generate_image`, `edit_image`) "
    "returned is already shown in the chat. Do not embed it or link to it again, even "
    "though it is saved in `/mnt/user-data/files/`."
)
GOOD_EXAMPLES = """<good_file_sharing_examples>
[Assistant finishes running code to generate a report]
[View your report]({file_base_url}/report.docx)
[end of output]

[Assistant finishes writing a script to compute the first 10 digits of pi]
[View your script]({file_base_url}/pi.py)
[end of output]

[Assistant finishes creating a chart]
![Sales Chart]({file_base_url}/chart.png)
[end of output]

[Assistant creates a report with visualization]
[View your report]({file_base_url}/report.docx)

![Data Visualization]({file_base_url}/viz.png)
[end of output]

These examples are good because they:
1. are succinct (without unnecessary postamble)
2. use "view" instead of "download"
3. provide direct HTTP links to files
4. use image syntax for .png/.jpg/.gif/.svg files
</good_file_sharing_examples>"""
USER_SKILLS_XML = "<available_skills>\n<skill>\n<name>\nexample\n</name>\n</skill>\n</available_skills>"


@pytest.fixture
def no_provider(monkeypatch):
    """No skills provider: get_user_skills falls back to the default public skills."""

    async def fetch(_email):
        return None

    monkeypatch.setattr(skill_manager, "_fetch_user_config", fetch)
    monkeypatch.setattr(skill_manager, "_load_user_config_cache", lambda _email: None)
    skill_manager._memory_cache.clear()
    system_prompt.invalidate_render_cache()
    yield
    skill_manager._memory_cache.clear()
    system_prompt.invalidate_render_cache()


def _assert_exception_follows_the_rule_once(text: str) -> None:
    assert text.count(EXCEPTION) == 1
    assert text.count(RULE) == 1
    assert f"\n\n{RULE}\n\n{EXCEPTION}\n\n" in text


@pytest.mark.parametrize(
    "text",
    [
        system_prompt.SYSTEM_PROMPT_TEMPLATE,
        system_prompt.build_system_prompt(),
        system_prompt.build_system_prompt(skills_xml=USER_SKILLS_XML, has_user_skills=True),
    ],
    ids=["template", "default-skills", "user-skills"],
)
def test_built_prompt_has_the_exception_right_after_the_image_rule(text):
    _assert_exception_follows_the_rule_once(text)
    assert text.count(GOOD_EXAMPLES) == 1


@pytest.mark.parametrize("user_email", [None, EMAIL], ids=["no-user", "user"])
def test_rendered_prompt_for_a_chat_has_the_exception_once(no_provider, user_email):
    from docker_manager import PUBLIC_BASE_URL

    uncached = asyncio.run(system_prompt._render_uncached(CHAT, user_email))
    cached = asyncio.run(system_prompt.render_system_prompt(CHAT, user_email, frozenset({"video"})))

    examples = GOOD_EXAMPLES.replace("{file_base_url}", f"{PUBLIC_BASE_URL}/files/{CHAT}")
    for text in (uncached, cached):
        _assert_exception_follows_the_rule_once(text)
        assert text.count(examples) == 1


def test_system_prompt_route_serves_the_exception_once(no_provider, monkeypatch):
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", TOKEN)
    client = TestClient(app_module.app)
    response = client.get(
        "/system-prompt",
        params={"chat_id": CHAT, "user_email": EMAIL},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200, response.text[:300]
    _assert_exception_follows_the_rule_once(response.text)
