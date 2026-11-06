# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""`describe-image` is listed only when the orchestrator has VISION_API_KEY (#720).

Seams: the two skill renderers (`build_available_skills_xml`,
`build_sub_agent_skills_text`) and `render_system_prompt`, for a user and for
the no-email path. Only the skills provider (`_fetch_user_config`, the disk
cache) is stubbed. `VISION_API_KEY` is patched on the `docker_manager` module
that is in `sys.modules` when the test runs, because other tests import
docker_manager afresh.
"""

import asyncio
import importlib
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "computer-use-server"))

import skill_manager  # noqa: E402
import system_prompt  # noqa: E402

CHAT = "0b6e3f1c-7d2a-4c59-9e8f-1a2b3c4d5e6f"
EMAIL = "viewer@example.test"
NAME = "describe-image"
# The hardcoded entry in DEFAULT_SKILLS_XML (description on two lines) plus the
# blank line after it, written out here rather than imported from production.
DEFAULT_BLOCK = (
    "<skill>\n"
    "<name>\n"
    "describe-image\n"
    "</name>\n"
    "<description>\n"
    "Describe images (charts, diagrams, tables, screenshots) using Vision AI.\n"
    "Use as fallback when you cannot read an image file directly.\n"
    "</description>\n"
    "<location>\n"
    "/mnt/skills/public/describe-image/SKILL.md\n"
    "</location>\n"
    "</skill>\n"
    "\n"
)
RENDERED_BLOCK = re.compile(r"<skill>\n<name>\ndescribe-image\n</name>\n.*?</skill>\n\n", re.DOTALL)
SUB_AGENT_LINE = re.compile(r"^- describe-image: .*\n", re.MULTILINE)


@pytest.fixture
def vision(monkeypatch):
    """Set VISION_API_KEY on the docker_manager that is current at test time."""

    async def no_provider(_email):
        return None

    monkeypatch.setattr(skill_manager, "_fetch_user_config", no_provider)
    monkeypatch.setattr(skill_manager, "_load_user_config_cache", lambda _email: None)
    docker_manager = importlib.import_module("docker_manager")
    assert sys.modules["docker_manager"] is docker_manager

    def configure(key):
        monkeypatch.setattr(docker_manager, "VISION_API_KEY", key)
        skill_manager._memory_cache.clear()
        system_prompt.invalidate_render_cache()

    configure("")
    yield configure
    skill_manager._memory_cache.clear()
    system_prompt.invalidate_render_cache()


def _render(chat_id, user_email):
    system_prompt.invalidate_render_cache()
    return asyncio.run(system_prompt.render_system_prompt(chat_id, user_email))


def _one(pattern, text):
    found = pattern.findall(text)
    assert len(found) == 1, found
    return found[0]


def test_renderers_omit_describe_image_without_vision_and_keep_the_list(vision):
    defaults = skill_manager._build_default_skills()
    assert NAME in [skill.name for skill in defaults]

    xml = skill_manager.build_available_skills_xml(defaults)
    text = skill_manager.build_sub_agent_skills_text(defaults)

    assert NAME not in xml
    assert NAME not in text
    assert "<name>\ndocx\n</name>" in xml
    assert "- docx: " in text
    assert NAME in [skill.name for skill in defaults]


def test_renderers_list_describe_image_once_with_vision(vision):
    defaults = skill_manager._build_default_skills()
    empty_xml = skill_manager.build_available_skills_xml(defaults)
    empty_text = skill_manager.build_sub_agent_skills_text(defaults)

    vision("fake-vision-key")
    xml = skill_manager.build_available_skills_xml(defaults)
    text = skill_manager.build_sub_agent_skills_text(defaults)

    assert xml.replace(_one(RENDERED_BLOCK, xml), "", 1) == empty_xml
    assert text.replace(_one(SUB_AGENT_LINE, text), "", 1) == empty_text


def test_user_prompt_omits_describe_image_without_vision_and_keeps_the_cache(vision):
    prompt = _render(CHAT, EMAIL)

    assert "<available_skills>" in prompt
    assert NAME not in prompt
    cached = skill_manager._memory_cache[EMAIL][1]
    assert NAME in [skill.name for skill in cached]


def test_user_prompt_lists_describe_image_once_with_vision(vision):
    empty = _render(CHAT, EMAIL)

    vision("fake-vision-key")
    prompt = _render(CHAT, EMAIL)

    assert prompt.replace(_one(RENDERED_BLOCK, prompt), "", 1) == empty


def test_no_email_template_omits_the_hardcoded_entry_without_vision(vision):
    template = system_prompt.SYSTEM_PROMPT_TEMPLATE
    assert template.count(DEFAULT_BLOCK) == 1

    prompt = _render(None, None)

    assert NAME not in prompt
    assert "{file_base_url}" in prompt
    assert prompt == template.replace(DEFAULT_BLOCK, "", 1)


def test_no_email_template_is_unchanged_with_vision(vision):
    vision("fake-vision-key")

    prompt = _render(None, None)

    assert prompt == system_prompt.SYSTEM_PROMPT_TEMPLATE
    assert "{file_base_url}" in prompt


def test_no_email_chat_prompt_lists_the_hardcoded_entry_only_with_vision(vision):
    empty = _render(CHAT, None)
    vision("fake-vision-key")
    prompt = _render(CHAT, None)

    assert NAME not in empty
    assert "{file_base_url}" not in prompt
    assert f"/files/{CHAT}" in prompt
    assert prompt.count(DEFAULT_BLOCK) == 1
    assert prompt.replace(DEFAULT_BLOCK, "", 1) == empty
