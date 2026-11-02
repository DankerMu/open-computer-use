# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Feature-gated skills in /system-prompt: the video switch adds `remotion`.

Seams: the real FastAPI `/system-prompt` route and `render_system_prompt`.
Only the skills provider (`_fetch_user_config`, the disk cache) is stubbed,
because it is an HTTP dependency of skill_manager, not the system under test.
"""

import asyncio
import re
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
CHAT = "6f1c2a9e-3b4d-4e8f-a1b2-c3d4e5f60718"
EMAIL = "person@example.test"
VIDEO = {"X-OCU-Features": "video"}
REMOTION_NAME = "<name>\nremotion\n</name>"
REMOTION_LOCATION = "<location>\n/mnt/skills/public/remotion/SKILL.md\n</location>"
REMOTION_BLOCK = re.compile(r"<skill>\n<name>\nremotion\n</name>\n.*?</skill>\n\n", re.DOTALL)


@pytest.fixture
def provider(monkeypatch):
    """Skills provider stub; tests set `provider["skills"]` to an API answer."""
    state = {"skills": None}

    async def fetch(_email):
        if state["skills"] is None:
            return None
        return {"enabled_skills": [dict(skill) for skill in state["skills"]]}

    monkeypatch.setattr(skill_manager, "_fetch_user_config", fetch)
    monkeypatch.setattr(skill_manager, "_load_user_config_cache", lambda _email: None)
    skill_manager._memory_cache.clear()
    system_prompt.invalidate_render_cache()
    yield state
    skill_manager._memory_cache.clear()
    system_prompt.invalidate_render_cache()


@pytest.fixture
def client(provider, monkeypatch):
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", TOKEN)
    http = TestClient(app_module.app)
    http.headers.update({"Authorization": f"Bearer {TOKEN}"})
    return http


def _prompt(client, headers=None, **params):
    response = client.get("/system-prompt", params=params, headers=headers or {})
    assert response.status_code == 200, response.text[:300]
    return response.text


def test_video_adds_exactly_one_remotion_entry_and_nothing_else(client):
    plain = _prompt(client, chat_id=CHAT, user_email=EMAIL)
    video = _prompt(client, VIDEO, chat_id=CHAT, user_email=EMAIL)

    assert "remotion" not in plain
    assert video.count(REMOTION_NAME) == 1
    assert REMOTION_LOCATION in video
    blocks = REMOTION_BLOCK.findall(video)
    assert len(blocks) == 1
    # The user's other skills are unchanged: removing the one entry gives the
    # plain prompt back byte for byte.
    assert video.replace(blocks[0], "", 1) == plain


def test_absent_or_empty_header_means_no_features(client):
    plain = _prompt(client, chat_id=CHAT, user_email=EMAIL)
    for value in ("", "   "):
        assert _prompt(client, {"X-OCU-Features": value}, chat_id=CHAT, user_email=EMAIL) == plain


def test_unknown_feature_is_400_and_renders_nothing(client, monkeypatch):
    calls = []

    async def render(*args, **kwargs):
        calls.append((args, kwargs))
        return "rendered"

    monkeypatch.setattr(app_module, "render_system_prompt", render)
    for value in ("video,unknown", "unknown", "Video", "video,", ",", "video;audio"):
        response = client.get(
            "/system-prompt",
            params={"chat_id": CHAT, "user_email": EMAIL},
            headers={"X-OCU-Features": value},
        )
        assert response.status_code == 400, value
        assert "rendered" not in response.text
    assert calls == []


def test_render_cache_keys_on_the_feature_set(provider, monkeypatch):
    calls = []

    async def uncached(chat_id, user_email, features=frozenset()):
        calls.append((chat_id, user_email, features))
        return f"{chat_id}:{user_email}:{sorted(features)}"

    monkeypatch.setattr(system_prompt, "_render_uncached", uncached)
    video = frozenset({"video"})
    plain_text = asyncio.run(system_prompt.render_system_prompt(CHAT, EMAIL))
    video_text = asyncio.run(system_prompt.render_system_prompt(CHAT, EMAIL, video))
    assert asyncio.run(system_prompt.render_system_prompt(CHAT, EMAIL)) == plain_text
    assert asyncio.run(system_prompt.render_system_prompt(CHAT, EMAIL, video)) == video_text

    assert plain_text != video_text
    assert calls == [(CHAT, EMAIL, frozenset()), (CHAT, EMAIL, video)]


def test_video_render_leaves_the_warm_skills_cache_and_plain_prompt_unchanged(client):
    before = _prompt(client, chat_id=CHAT, user_email=EMAIL)
    cached_list = skill_manager._memory_cache[EMAIL][1]
    names = [skill.name for skill in cached_list]

    video = _prompt(client, VIDEO, chat_id=CHAT, user_email=EMAIL)
    assert REMOTION_NAME in video

    assert skill_manager._memory_cache[EMAIL][1] is cached_list
    assert [skill.name for skill in cached_list] == names
    # Re-render the plain prompt from the still-warm skills cache.
    system_prompt.invalidate_render_cache()
    assert _prompt(client, chat_id=CHAT, user_email=EMAIL) == before
    assert "remotion" not in asyncio.run(system_prompt.render_system_prompt(CHAT, EMAIL))
    # sub_agent and its HTTP twin read the same cache.
    sub_agent = skill_manager.build_sub_agent_skills_text(skill_manager.get_user_skills_sync(EMAIL))
    assert "remotion" not in sub_agent
    skill_list = client.get("/skill-list", params={"user_email": EMAIL})
    assert skill_list.status_code == 200
    assert "remotion" not in skill_list.text


def test_video_without_user_email_renders_the_fixed_template(client):
    plain = _prompt(client, chat_id=CHAT)
    video = _prompt(client, VIDEO, chat_id=CHAT)
    assert video == plain
    assert "remotion" not in video


def test_video_keeps_a_single_remotion_entry_when_the_user_already_has_it(client, provider):
    provider["skills"] = [
        {"name": "remotion", "category": "public", "skill_path": "public/remotion", "description": "x"},
    ]
    plain = _prompt(client, chat_id=CHAT, user_email=EMAIL)
    video = _prompt(client, VIDEO, chat_id=CHAT, user_email=EMAIL)
    assert plain.count(REMOTION_NAME) == 1
    assert video == plain


def test_remotion_is_not_a_default_skill_and_has_a_native_description():
    assert "remotion" not in skill_manager.DEFAULT_PUBLIC_SKILLS
    defaults = asyncio.run(skill_manager.get_user_skills(None))
    assert "remotion" not in [skill.name for skill in defaults]
    description = skill_manager.NATIVE_PROMPT_DESCRIPTIONS["remotion"]
    for required in ("Remotion", "voiceover", "3D", "SKILL.md"):
        assert required in description
