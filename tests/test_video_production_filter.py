# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""The video switch: an Open WebUI toggle filter that changes nothing itself.

Open WebUI lists a filter as a switch when its loaded `Filter` instance has a
truthy `toggle` (read with getattr) and shows its `icon`. The Computer Use
filter reads the switch from request metadata, so this filter's inlet must
leave the body alone.
"""

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FUNCTIONS = ROOT / "openwebui" / "functions"
sys.path.insert(0, str(FUNCTIONS))

import computer_link_filter  # noqa: E402
import video_production_filter  # noqa: E402

SOURCE = FUNCTIONS / "video_production_filter.py"


def test_toggle_is_a_true_class_attribute():
    assert video_production_filter.Filter.__dict__["toggle"] is True
    assert video_production_filter.Filter().toggle is True


def test_icon_is_an_image_data_url_class_attribute():
    icon = video_production_filter.Filter.__dict__["icon"]
    assert icon.startswith("data:image/svg+xml;base64,")


def test_inlet_returns_the_body_unchanged():
    body = {
        "model": "qwen3.8-27b",
        "tool_ids": ["web_search"],
        "messages": [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "make a video"},
        ],
    }
    original = copy.deepcopy(body)
    result = video_production_filter.Filter().inlet(
        body,
        __user__={"email": "person@example.test"},
        __metadata__={"chat_id": "6f1c2a9e-3b4d-4e8f-a1b2-c3d4e5f60718"},
    )
    assert result is body
    assert result == original


def test_installed_id_matches_the_computer_use_filter_constant():
    # The init scripts install this file under its module name.
    assert SOURCE.stem == computer_link_filter.VIDEO_FILTER_ID


def test_source_survives_the_webui_import_rewrite():
    source = SOURCE.read_text(encoding="utf-8")
    for rewritten in ("from utils", "from apps", "from main", "from config"):
        assert rewritten not in source
