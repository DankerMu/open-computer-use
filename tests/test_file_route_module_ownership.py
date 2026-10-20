# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""App-module fixtures preserve caller-owned module identities."""

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests import test_auth_guard as auth_tests
from tests import test_file_response as response_tests
from tests import test_files_headers as header_tests
from tests.file_route_module_ownership import _FILE_ROUTE_MODULES
from tests.orchestrator import test_ws_recheck as ws_tests

ROOT = Path(__file__).resolve().parents[1]
BODY_FAILURE = "ocu-module-ownership-planned-body-failure"

workspace = response_tests.workspace
app_module = header_tests.app_module
auth_app_module = auth_tests.app_module
ws_app_module = ws_tests.app_module
ws_server_modules = ws_tests.ws_server_modules

OWNERSHIP = {
    "workspace": (_FILE_ROUTE_MODULES, ("mcp_resources",)),
    "app_module": (_FILE_ROUTE_MODULES, ("mcp_resources",)),
    "auth_app_module": (auth_tests._AUTH_GUARD_MODULES, ("mcp_resources",)),
    "ws_app_module": (ws_tests._WS_SERVER_MODULES, ("mcp_resources", "cli_adapters")),
}
FILE_ROUTE_OWNERS = ("workspace", "app_module")
AMBIENT_NAMES = set().union(*(names for names, _ in OWNERSHIP.values())) | {
    "ws_recheck", "cli_runtime", "sandbox_dns", "docs_html", "office", "cli_adapters",
}
AMBIENT_PREFIXES = tuple(
    sorted({prefix for _, prefixes in OWNERSHIP.values() for prefix in prefixes}
           | {"office.", "cli_adapters."})
)
SERVICE_ENV = {
    "PYTHON_DOTENV_DISABLED": "1",
    "OCU_INTERNAL_TOKEN": "ocu-module-ownership-test-token",
    "MCP_API_KEY": "ocu-module-ownership-mcp-key",
    "OCU_WEBUI_ORIGIN": "https://webui.example",
    "OCU_SANDBOX_SUBNET": "10.90.0.0/24",
    "SINGLE_USER_MODE": "true",
    "PUBLIC_BASE_URL": "http://ocu.example",
}


def _owned(names, prefixes):
    return {
        name: module for name, module in sys.modules.copy().items()
        if name in names or name.startswith(prefixes)
    }


@pytest.mark.parametrize("owner", tuple(OWNERSHIP))
@pytest.mark.parametrize("entry_state", ("present", "absent"))
def test_file_route_fixture_restores_caller_modules(
    owner, entry_state, request, monkeypatch, tmp_path,
):
    owned_names, owned_prefixes = OWNERSHIP[owner]
    ambient = _owned(AMBIENT_NAMES, AMBIENT_PREFIXES)

    def restore_ambient_state():
        try:
            monkeypatch.undo()
        finally:
            for name in list(sys.modules):
                if name in AMBIENT_NAMES or name.startswith(AMBIENT_PREFIXES):
                    sys.modules.pop(name, None)
            sys.modules.update(ambient)

    request.addfinalizer(restore_ambient_state)
    for name in ambient:
        sys.modules.pop(name, None)
    for key, value in {**SERVICE_ENV, "BASE_DATA_DIR": str(tmp_path / "caller")}.items():
        monkeypatch.setenv(key, value)

    import app as caller_app
    import descriptor_io as caller_descriptor
    import uploads as caller_uploads

    assert caller_uploads.descriptor_io is caller_descriptor
    assert caller_app.CommittedCloseError is caller_descriptor.CommittedCloseError
    if entry_state == "absent":
        for name in list(sys.modules):
            if name in owned_names or name.startswith(owned_prefixes):
                sys.modules.pop(name, None)
    entry = _owned(owned_names, owned_prefixes)

    def assert_caller_modules_restored():
        restored = _owned(owned_names, owned_prefixes)
        assert restored.keys() == entry.keys(), (
            f"{owner} did not restore the caller's module presence"
        )
        for name, module in entry.items():
            assert restored[name] is module, (
                f"{owner} did not restore the caller's {name} module object"
            )
        if entry_state == "present" and owner in FILE_ROUTE_OWNERS:
            assert restored["uploads"] is caller_uploads
            assert restored["descriptor_io"] is caller_descriptor
            assert restored["app"] is caller_app

    # LIFO: real owner teardown, restoration assertions, then emergency cleanup.
    request.addfinalizer(assert_caller_modules_restored)
    result = request.getfixturevalue(owner)
    if owner not in FILE_ROUTE_OWNERS:
        return
    loaded = result[0] if owner == "workspace" else result
    active_descriptor = sys.modules["descriptor_io"]
    active_uploads = sys.modules["uploads"]
    assert active_uploads.descriptor_io is active_descriptor
    assert loaded.CommittedCloseError is active_descriptor.CommittedCloseError
    assert sys.modules["docker_manager"].descriptor_io is active_descriptor
    assert sys.modules["outputs_broker"].descriptor_io is active_descriptor
    assert sys.modules["file_response"].open_chat_upload is active_uploads.open_chat_upload
    assert loaded.write_import_receipts is active_uploads.write_import_receipts


CHILD_OWNERS = {
    "auth_app_module": (
        "from tests.test_auth_guard import _AUTH_GUARD_MODULES as OWNED_NAMES\n"
        "from tests.test_auth_guard import app_module\n"
    ),
    "ws_app_module": (
        "from tests.orchestrator.test_ws_recheck import _WS_SERVER_MODULES as OWNED_NAMES\n"
        "from tests.orchestrator.test_ws_recheck import app_module, ws_server_modules\n"
    ),
}
CHILD_TEMPLATE = """\
import sys

{imports}
OWNED_PREFIXES = {prefixes!r}
ENTRY = {{}}


def _owned():
    return {{
        name: module for name, module in sys.modules.copy().items()
        if name in OWNED_NAMES or name.startswith(OWNED_PREFIXES)
    }}


def test_1_entry():
    import app  # noqa: F401

    if {absent!r}:
        for name in _owned():
            sys.modules.pop(name, None)
    ENTRY.update(_owned())


def test_2_body(app_module):
    assert False, {sentinel!r}


def test_3_restored():
    restored = _owned()
    assert restored.keys() == ENTRY.keys(), (
        f"owned module presence not restored: {{sorted(restored.keys() ^ ENTRY.keys())}}"
    )
    for name, module in ENTRY.items():
        assert restored[name] is module, f"owned module {{name}} object not restored"
"""


@pytest.mark.parametrize("owner", tuple(CHILD_OWNERS))
@pytest.mark.parametrize("entry_state", ("present", "absent"))
def test_owner_restores_modules_after_test_body_failure(owner, entry_state, tmp_path):
    _, owned_prefixes = OWNERSHIP[owner]
    child = tmp_path / "test_ownership_child.py"
    child.write_text(CHILD_TEMPLATE.format(
        imports=CHILD_OWNERS[owner],
        prefixes=owned_prefixes,
        absent=entry_state == "absent",
        sentinel=BODY_FAILURE,
    ))
    junit = tmp_path / "child.xml"
    child_env = {
        key: value for key, value in os.environ.items()
        if key.lower() not in {"all_proxy", "https_proxy", "http_proxy"}
        and not key.startswith("OCU_OFFICE_")
    }
    child_env.update(SERVICE_ENV)
    child_env["PYTHONPATH"] = str(ROOT)
    child_env["BASE_DATA_DIR"] = str(tmp_path / "child-data")

    completed = subprocess.run(
        [
            sys.executable, "-m", "pytest", str(child), "--import-mode=importlib",
            "-p", "no:cacheprovider", "-p", "no:randomly", "--junitxml", str(junit),
        ],
        cwd=ROOT, env=child_env, capture_output=True, text=True, timeout=180,
    )

    output = (completed.stdout + completed.stderr)[-4000:]
    cases = {case.get("name"): case for case in ET.parse(junit).getroot().iter("testcase")}
    outcomes = {
        name: [
            (result.tag, result.get("message")) for result in case
            if result.tag in {"failure", "error", "skipped"}
        ]
        for name, case in cases.items()
    }
    assert sorted(cases) == ["test_1_entry", "test_2_body", "test_3_restored"], output
    assert outcomes["test_1_entry"] == [], outcomes
    assert outcomes["test_3_restored"] == [], outcomes
    body = outcomes["test_2_body"]
    assert [tag for tag, _ in body] == ["failure"], outcomes
    assert BODY_FAILURE in body[0][1], outcomes
    assert completed.returncode == 1, output
