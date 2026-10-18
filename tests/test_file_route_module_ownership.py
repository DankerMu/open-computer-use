# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""File-route fixtures preserve caller-owned receipt module identities."""

import sys

import pytest

from tests import test_file_response as response_tests
from tests import test_files_headers as header_tests

workspace = response_tests.workspace
app_module = header_tests.app_module


@pytest.mark.parametrize("owner", ("workspace", "app_module"))
@pytest.mark.parametrize("entry_state", ("present", "absent"))
def test_file_route_fixture_restores_caller_modules(
    owner, entry_state, request, monkeypatch, tmp_path,
):
    owned_names = {
        "app", "auth_guard", "mcp_tools", "docker_manager", "outputs_broker",
        "context_vars", "security", "system_prompt", "skill_manager", "file_response",
        "descriptor_io", "uploads",
    }
    ambient_names = owned_names | {
        "ws_recheck", "cli_runtime", "sandbox_dns", "docs_html", "office", "cli_adapters",
    }
    ambient = {
        name: module for name, module in sys.modules.copy().items()
        if name in ambient_names or name.startswith(
            ("mcp_resources", "office.", "cli_adapters.")
        )
    }

    def restore_ambient_state():
        try:
            monkeypatch.undo()
        finally:
            for name in list(sys.modules):
                if name in ambient_names or name.startswith(
                    ("mcp_resources", "office.", "cli_adapters.")
                ):
                    sys.modules.pop(name, None)
            sys.modules.update(ambient)

    request.addfinalizer(restore_ambient_state)
    for name in ambient:
        sys.modules.pop(name, None)
    for key, value in {
        "PYTHON_DOTENV_DISABLED": "1",
        "OCU_INTERNAL_TOKEN": "ocu-module-ownership-test-token",
        "MCP_API_KEY": "ocu-module-ownership-mcp-key",
        "OCU_WEBUI_ORIGIN": "https://webui.example",
        "OCU_SANDBOX_SUBNET": "10.90.0.0/24",
        "SINGLE_USER_MODE": "true",
        "PUBLIC_BASE_URL": "http://ocu.example",
        "BASE_DATA_DIR": str(tmp_path / "caller"),
    }.items():
        monkeypatch.setenv(key, value)

    import app as caller_app
    import descriptor_io as caller_descriptor
    import uploads as caller_uploads

    assert caller_uploads.descriptor_io is caller_descriptor
    assert caller_app.CommittedCloseError is caller_descriptor.CommittedCloseError
    if entry_state == "absent":
        for name in list(sys.modules):
            if name in owned_names or name.startswith("mcp_resources"):
                sys.modules.pop(name, None)
    entry = {
        name: module for name, module in sys.modules.copy().items()
        if name in owned_names or name.startswith("mcp_resources")
    }

    def assert_caller_modules_restored():
        restored = {
            name: module for name, module in sys.modules.copy().items()
            if name in owned_names or name.startswith("mcp_resources")
        }
        assert restored.keys() == entry.keys(), (
            f"{owner} did not restore the caller's module presence"
        )
        for name, module in entry.items():
            assert restored[name] is module, (
                f"{owner} did not restore the caller's {name} module object"
            )
        if entry_state == "present":
            assert restored["uploads"] is caller_uploads
            assert restored["descriptor_io"] is caller_descriptor
            assert restored["app"] is caller_app

    # LIFO: real owner teardown, restoration assertions, then emergency cleanup.
    request.addfinalizer(assert_caller_modules_restored)
    result = request.getfixturevalue(owner)
    loaded = result[0] if owner == "workspace" else result
    active_descriptor = sys.modules["descriptor_io"]
    active_uploads = sys.modules["uploads"]
    assert active_uploads.descriptor_io is active_descriptor
    assert loaded.CommittedCloseError is active_descriptor.CommittedCloseError
    assert sys.modules["docker_manager"].descriptor_io is active_descriptor
    assert sys.modules["outputs_broker"].descriptor_io is active_descriptor
    assert sys.modules["file_response"].open_chat_upload is active_uploads.open_chat_upload
    assert loaded.write_import_receipts is active_uploads.write_import_receipts
