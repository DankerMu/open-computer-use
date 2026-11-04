# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""The company system prompt filter (#712).

The filter reads the deployment's Markdown file on every chat request and puts
its text at the front of the system message. Open WebUI applies the model's own
system prompt after filters, so the company prompt ends up right after it and
before the Computer Use prompt, whichever of the two filters runs first.
"""

import copy
import logging
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FUNCTIONS = ROOT / "openwebui" / "functions"
sys.path.insert(0, str(FUNCTIONS))

import computer_link_filter  # noqa: E402
import org_prompt_filter  # noqa: E402

SOURCE = FUNCTIONS / "org_prompt_filter.py"
CHAT_ID = "6f1c2a9e-3b4d-4e8f-a1b2-c3d4e5f60718"
COMPANY = "# 身份\n\n你是示例公司的助手。\n\n- 回答用中文。\n"
USER_PROMPT = "Answer briefly."
CU_PROMPT = "Computer Use prompt"


@pytest.fixture
def prompt_file(tmp_path, monkeypatch):
    directory = tmp_path / "prompts"
    directory.mkdir()
    path = directory / "system-prompt.md"
    monkeypatch.setattr(org_prompt_filter, "PROMPT_FILE", path)
    return path


def chat(system=None):
    messages = [{"role": "user", "content": "你是谁"}]
    if system is not None:
        messages.insert(0, {"role": "system", "content": system})
    return {"model": "qwen3.8-27b", "messages": messages}


def run(body, metadata=None):
    return org_prompt_filter.Filter().inlet(
        body, __metadata__={"chat_id": CHAT_ID} if metadata is None else metadata
    )


def computer_use_filter():
    clf = computer_link_filter.Filter()
    # The orchestrator is a dependency: answer its prompt fetch directly.
    clf._fetch_system_prompt = lambda *_args: ("http://localhost:8081", CU_PROMPT)
    return clf


def test_prepends_to_the_existing_system_message(prompt_file):
    prompt_file.write_text(COMPANY, encoding="utf-8")
    body = run(chat(USER_PROMPT))
    assert body["messages"] == [
        {"role": "system", "content": COMPANY + "\n\n" + USER_PROMPT},
        {"role": "user", "content": "你是谁"},
    ]


def test_inserts_a_system_message_when_there_is_none(prompt_file):
    prompt_file.write_text(COMPANY, encoding="utf-8")
    body = run(chat())
    assert body["messages"] == [
        {"role": "system", "content": COMPANY},
        {"role": "user", "content": "你是谁"},
    ]


def test_injects_the_text_as_written(prompt_file):
    written = "  indented first line\n\ttab\n\n"
    prompt_file.write_text(written, encoding="utf-8")
    assert run(chat())["messages"][0]["content"] == written


@pytest.mark.parametrize("text", ["", " ", "\n", " \t\r\n\n  "])
def test_empty_or_whitespace_file_leaves_the_body_unchanged(prompt_file, text):
    prompt_file.write_text(text, encoding="utf-8")
    for body in (chat(USER_PROMPT), chat()):
        original = copy.deepcopy(body)
        assert run(body) == original


def test_structured_system_content_gets_a_leading_text_part(prompt_file):
    prompt_file.write_text(COMPANY, encoding="utf-8")
    parts = [{"type": "text", "text": USER_PROMPT}, {"type": "text", "text": "more"}]
    body = run(chat(copy.deepcopy(parts)))
    assert body["messages"][0] == {
        "role": "system",
        "content": [{"type": "text", "text": COMPANY}, *parts],
    }


@pytest.mark.parametrize(
    "metadata",
    [None, {}, {"chat_id": "local:4f0c"}, {"chat_id": "temporary:9b1e"}],
    ids=["no-metadata", "empty-metadata", "local-chat", "temporary-chat"],
)
def test_applies_to_every_chat_kind(prompt_file, metadata):
    prompt_file.write_text(COMPANY, encoding="utf-8")
    body = org_prompt_filter.Filter().inlet(chat(), __metadata__=metadata)
    assert body["messages"][0] == {"role": "system", "content": COMPANY}


def test_missing_file_raises_naming_the_file(prompt_file):
    with pytest.raises(org_prompt_filter.OrgPromptError) as raised:
        run(chat(USER_PROMPT))
    assert str(prompt_file) in str(raised.value)
    assert "missing" in str(raised.value)


def test_directory_in_place_of_the_file_raises_naming_the_file(prompt_file):
    prompt_file.mkdir()
    with pytest.raises(org_prompt_filter.OrgPromptError) as raised:
        run(chat())
    assert str(prompt_file) in str(raised.value)
    assert "not a regular file" in str(raised.value)


@pytest.mark.parametrize("target_exists", [True, False], ids=["link", "dangling-link"])
def test_symbolic_link_raises_naming_the_file(prompt_file, tmp_path, target_exists):
    # Only prompts/ is mounted, so a link to a file outside it is missing in the container.
    target = tmp_path / "elsewhere.md"
    if target_exists:
        target.write_text(COMPANY, encoding="utf-8")
    prompt_file.symlink_to(target)
    with pytest.raises(org_prompt_filter.OrgPromptError) as raised:
        run(chat(USER_PROMPT))
    assert str(prompt_file) in str(raised.value)
    assert "is a symbolic link" in str(raised.value)


def test_a_rejected_file_is_logged_as_a_warning(prompt_file, caplog):
    # Open WebUI logs inlet exceptions only at DEBUG; the filter's own line reaches docker logs.
    prompt_file.write_bytes(b"\xff")
    with caplog.at_level(logging.WARNING):
        with pytest.raises(org_prompt_filter.OrgPromptError) as raised:
            run(chat())
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.getMessage() for r in warnings] == [str(raised.value)]
    assert str(prompt_file) in warnings[0].getMessage()


def test_file_at_the_limit_is_accepted(prompt_file):
    text = "a" * (64 * 1024)
    prompt_file.write_text(text, encoding="ascii")
    assert run(chat())["messages"][0]["content"] == text


def test_file_over_64_kib_raises_naming_the_file(prompt_file):
    prompt_file.write_bytes(b"a" * (64 * 1024 + 1))
    with pytest.raises(org_prompt_filter.OrgPromptError) as raised:
        run(chat(USER_PROMPT))
    assert str(prompt_file) in str(raised.value)
    assert "65536 bytes" in str(raised.value)


def test_oversized_file_is_refused_before_decoding(prompt_file):
    # Invalid UTF-8 past the limit: the size check must answer, not the decoder.
    prompt_file.write_bytes(b"a" * (64 * 1024) + b"\xff")
    with pytest.raises(org_prompt_filter.OrgPromptError) as raised:
        run(chat())
    assert "65536 bytes" in str(raised.value)


def test_invalid_utf8_raises_naming_the_file(prompt_file):
    prompt_file.write_bytes("公司".encode("gbk"))
    with pytest.raises(org_prompt_filter.OrgPromptError) as raised:
        run(chat(USER_PROMPT))
    assert str(prompt_file) in str(raised.value)
    assert "UTF-8" in str(raised.value)


def test_a_file_replaced_by_rename_is_read_fresh(prompt_file):
    flt = org_prompt_filter.Filter()
    prompt_file.write_text("first", encoding="utf-8")
    assert flt.inlet(chat(), __metadata__={})["messages"][0]["content"] == "first"
    # Editors save by writing a new file and renaming it over the old one.
    staged = prompt_file.with_name(".system-prompt.md.swp")
    staged.write_text("second", encoding="utf-8")
    os.replace(staged, prompt_file)
    assert flt.inlet(chat(), __metadata__={})["messages"][0]["content"] == "second"


@pytest.mark.parametrize("system", [USER_PROMPT, None], ids=["user-prompt", "no-system"])
@pytest.mark.parametrize("company_first", [True, False], ids=["company-first", "computer-use-first"])
def test_company_prompt_precedes_the_computer_use_prompt(prompt_file, system, company_first):
    prompt_file.write_text(COMPANY, encoding="utf-8")
    metadata = {"chat_id": CHAT_ID}
    org = org_prompt_filter.Filter()
    clf = computer_use_filter()
    body = chat(system)
    order = (org, clf) if company_first else (clf, org)
    for flt in order:
        body = flt.inlet(body, __metadata__=metadata)
    expected = [COMPANY] + ([USER_PROMPT] if system else []) + [CU_PROMPT]
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["messages"][0]["content"] == "\n\n".join(expected)


def test_prompt_path_is_a_fixed_container_path_not_a_valve():
    assert org_prompt_filter.PROMPT_FILE == Path("/etc/ocu/prompts/system-prompt.md")
    assert not hasattr(org_prompt_filter.Filter, "Valves")


def test_source_survives_the_webui_import_rewrite():
    source = SOURCE.read_text(encoding="utf-8")
    for rewritten in ("from utils", "from apps", "from main", "from config"):
        assert rewritten not in source
