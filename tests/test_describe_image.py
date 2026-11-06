# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""describe-image answers in the requested language, Chinese by default (#720).

Seam: the script's command line (`main()`). Only the HTTP call to the Vision
API (`requests.post`) is replaced; the request body it receives is checked.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills" / "public" / "describe-image" / "scripts" / "describe.py"
)
RULE = "Always respond in the language the user's message asks for."
# Smallest valid PNG header bytes; the script only base64-encodes the file.
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture
def describe(monkeypatch):
    spec = importlib.util.spec_from_file_location("describe_image_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("VISION_API_KEY", "fake-vision-key")
    monkeypatch.setenv("VISION_API_URL", "http://vision.invalid")
    sent = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "描述"}}]}

    def post(url, headers, json, timeout):
        sent.append(json)
        return Response()

    monkeypatch.setattr(module.requests, "post", post)

    def run(*argv):
        monkeypatch.setattr("sys.argv", ["describe.py", *argv])
        with pytest.raises(SystemExit) as stop:
            module.main()
        assert stop.value.code == 0
        return sent

    return run


def _user_text(body):
    content = body["messages"][1]["content"]
    return next(part["text"] for part in content if part["type"] == "text")


def test_single_image_defaults_to_chinese(describe, tmp_path):
    image = tmp_path / "chart.png"
    image.write_bytes(PNG)

    bodies = describe("-i", str(image))

    assert len(bodies) == 1
    assert _user_text(bodies[0]).endswith("Respond in Chinese.")
    assert RULE in bodies[0]["messages"][0]["content"]


def test_single_image_language_override(describe, tmp_path):
    image = tmp_path / "chart.png"
    image.write_bytes(PNG)

    bodies = describe("-i", str(image), "-c", "Sales dashboard", "-l", "English")

    assert len(bodies) == 1
    text = _user_text(bodies[0])
    assert text.startswith("Context: Sales dashboard")
    assert text.endswith("Respond in English.")


def test_folder_language_override_reaches_every_image(describe, tmp_path):
    for name in ("a.png", "b.jpg", "c.webp"):
        (tmp_path / name).write_bytes(PNG)

    bodies = describe("-i", str(tmp_path), "--language", "English")

    assert len(bodies) == 3
    for body in bodies:
        assert _user_text(body).endswith("Respond in English.")
        assert RULE in body["messages"][0]["content"]
    assert (tmp_path / "manifest-1.md").read_text(encoding="utf-8").count("描述") == 3
