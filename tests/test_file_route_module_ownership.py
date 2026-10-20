# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""App-module fixtures preserve caller-owned module identities."""

import importlib
import os
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests import test_auth_guard as auth_tests
from tests import test_file_response as response_tests
from tests import test_files_headers as header_tests
from tests import test_opencode_alias_map_drop as alias_tests
from tests.file_route_module_ownership import _FILE_ROUTE_MODULES
from tests.orchestrator import test_startup_warnings as warning_tests
from tests.orchestrator import test_ws_recheck as ws_tests

ROOT = Path(__file__).resolve().parents[1]
BODY_FAILURE = "ocu-module-ownership-planned-body-failure"

workspace = response_tests.workspace
app_module = header_tests.app_module
auth_app_module = auth_tests.app_module
ws_app_module = ws_tests.app_module
ws_server_modules = ws_tests.ws_server_modules
alias_runtime_modules = alias_tests.alias_runtime_modules

OWNERSHIP = {
    "workspace": (_FILE_ROUTE_MODULES, ("mcp_resources",)),
    "app_module": (_FILE_ROUTE_MODULES, ("mcp_resources",)),
    "auth_app_module": (auth_tests._AUTH_GUARD_MODULES, auth_tests._AUTH_GUARD_PREFIXES),
    "ws_app_module": (ws_tests._WS_SERVER_MODULES, ("mcp_resources", "cli_adapters")),
    "alias_runtime_modules": (alias_tests._ALIAS_RUNTIME_MODULES, ()),
}
FILE_ROUTE_OWNERS = ("workspace", "app_module")
AMBIENT_NAMES = set().union(*(names for names, _ in OWNERSHIP.values())) | {
    "ws_recheck", "cli_runtime", "sandbox_dns", "docs_html", "office", "cli_adapters",
}
AMBIENT_PREFIXES = tuple(
    sorted({prefix for _, prefixes in OWNERSHIP.values() for prefix in prefixes}
           | {"office.", "cli_adapters."} | set(warning_tests._DOCKER_MANAGER_PREFIXES))
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


def _isolate_ambient_modules(request, monkeypatch):
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


@pytest.mark.parametrize("owner", tuple(OWNERSHIP))
@pytest.mark.parametrize("entry_state", ("present", "absent"))
def test_file_route_fixture_restores_caller_modules(
    owner, entry_state, request, monkeypatch, tmp_path,
):
    owned_names, owned_prefixes = OWNERSHIP[owner]
    _isolate_ambient_modules(request, monkeypatch)
    for key, value in {**SERVICE_ENV, "BASE_DATA_DIR": str(tmp_path / "caller")}.items():
        monkeypatch.setenv(key, value)

    import app as caller_app
    import descriptor_io as caller_descriptor
    import mcp_resources as caller_resources  # noqa: F401
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
    if owned_prefixes:
        importlib.import_module("mcp_resources")
    if owner == "alias_runtime_modules":
        importlib.import_module("cli_runtime")
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
        "from tests.test_auth_guard import app_module\n",
        "app_module",
        "    import mcp_resources  # noqa: F401\n",
    ),
    "ws_app_module": (
        "from tests.orchestrator.test_ws_recheck import _WS_SERVER_MODULES as OWNED_NAMES\n"
        "from tests.orchestrator.test_ws_recheck import app_module, ws_server_modules\n",
        "app_module",
        "    import mcp_resources  # noqa: F401\n",
    ),
    "alias_runtime_modules": (
        "import importlib\n"
        "from tests.test_opencode_alias_map_drop import _ALIAS_RUNTIME_MODULES as OWNED_NAMES\n"
        "from tests.test_opencode_alias_map_drop import alias_runtime_modules\n",
        "alias_runtime_modules",
        "    importlib.import_module(\"cli_runtime\")\n",
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
    import mcp_resources  # noqa: F401

    if {absent!r}:
        for name in _owned():
            sys.modules.pop(name, None)
    ENTRY.update(_owned())


def test_2_body({fixture}):
{prelude}    assert False, {sentinel!r}


def test_3_restored():
    restored = _owned()
    assert restored.keys() == ENTRY.keys(), (
        f"owned module presence not restored: {{sorted(restored.keys() ^ ENTRY.keys())}}"
    )
    for name, module in ENTRY.items():
        assert restored[name] is module, f"owned module {{name}} object not restored"
"""


def _child_env(tmp_path):
    env = {
        key: value for key, value in os.environ.items()
        if key.lower() not in {"all_proxy", "https_proxy", "http_proxy"}
        and not key.startswith("OCU_OFFICE_")
    }
    env.update(SERVICE_ENV)
    env["PYTHONPATH"] = str(ROOT)
    env["BASE_DATA_DIR"] = str(tmp_path / "child-data")
    return env


def _run_child_pytest(child, junit, env, timeout=180):
    # -s: pytest re-enables fd capture while importing test modules, so without it
    # import-time output is lost when the child is killed or exits via os._exit.
    try:
        completed = subprocess.run(
            [
                sys.executable, "-m", "pytest", str(child), "--import-mode=importlib", "-s",
                "-p", "no:cacheprovider", "-p", "no:randomly", "--junitxml", str(junit),
            ],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        # On POSIX the partial output is bytes or None even under text=True.
        stdout = (exc.stdout or b"").decode(errors="replace")
        stderr = (exc.stderr or b"").decode(errors="replace")
        pytest.fail(
            f"child pytest timed out after {timeout}s\n{(stdout + stderr)[-4000:]}",
            pytrace=False,
        )
    output = (completed.stdout + completed.stderr)[-4000:]
    try:
        tree = ET.parse(junit)
    except (FileNotFoundError, ET.ParseError) as exc:
        pytest.fail(
            f"child pytest exited {completed.returncode} without a parsable JUnit file"
            f" ({exc})\n{output}",
            pytrace=False,
        )
    cases = {case.get("name"): case for case in tree.getroot().iter("testcase")}
    return completed, cases, output


@pytest.mark.parametrize("owner", tuple(CHILD_OWNERS))
@pytest.mark.parametrize("entry_state", ("present", "absent"))
def test_owner_restores_modules_after_test_body_failure(owner, entry_state, tmp_path):
    _, owned_prefixes = OWNERSHIP[owner]
    imports, fixture, prelude = CHILD_OWNERS[owner]
    child = tmp_path / "test_ownership_child.py"
    child.write_text(CHILD_TEMPLATE.format(
        imports=imports,
        fixture=fixture,
        prelude=prelude,
        prefixes=owned_prefixes,
        absent=entry_state == "absent",
        sentinel=BODY_FAILURE,
    ))
    junit = tmp_path / "child.xml"
    completed, cases, output = _run_child_pytest(child, junit, _child_env(tmp_path))
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


CHILD_MARKER = "ocu-module-ownership-child-marker"
CHILD_STDOUT_MARKER = "ocu-module-ownership-child-stdout-marker"
FAILING_CHILDREN = {
    "timeout": "import time\n\ntime.sleep(120)\n",
    "crash": "import os\n\nos._exit(3)\n",
    "unparsable": "import os\n\nos._exit(3)\n",
}


@pytest.mark.parametrize("shape", tuple(FAILING_CHILDREN))
def test_child_run_failure_reports_captured_output(shape, tmp_path):
    child = tmp_path / "test_failing_child.py"
    child.write_text(
        f"import sys\n\nprint({CHILD_STDOUT_MARKER!r}, flush=True)\n"
        f"print({CHILD_MARKER!r}, file=sys.stderr, flush=True)\n"
        + FAILING_CHILDREN[shape]
    )
    junit = tmp_path / "child.xml"
    if shape == "unparsable":
        junit.write_text("not junit xml <")
    timeout = 10 if shape == "timeout" else 180

    with pytest.raises(pytest.fail.Exception) as failure:
        _run_child_pytest(child, junit, _child_env(tmp_path), timeout=timeout)

    message = str(failure.value)
    assert CHILD_STDOUT_MARKER in message
    assert CHILD_MARKER in message
    if shape != "timeout":
        assert "exited 3" in message


WARNING_CASES = {
    "PublicBaseUrlDefaultWarning": "test_silent_when_user_overrides_to_custom_url",
    "McpApiKeyMissingWarning": "test_silent_when_key_set",
}


@pytest.mark.parametrize("warning_case", tuple(WARNING_CASES))
@pytest.mark.parametrize("entry_state", ("present", "absent"))
@pytest.mark.parametrize("body", ("normal", "failure"))
def test_startup_warning_case_restores_docker_manager(
    warning_case, entry_state, body, request, monkeypatch,
):
    owned_names = warning_tests._DOCKER_MANAGER_MODULES
    owned_prefixes = warning_tests._DOCKER_MANAGER_PREFIXES
    _isolate_ambient_modules(request, monkeypatch)
    for key in ("PUBLIC_BASE_URL", "MCP_API_KEY"):
        monkeypatch.setenv(key, SERVICE_ENV[key])
    importlib.import_module("docker_manager")
    if entry_state == "absent":
        for name in list(sys.modules):
            if name in owned_names or name.startswith(owned_prefixes):
                sys.modules.pop(name, None)
    entry = _owned(owned_names, owned_prefixes)

    case_class = getattr(warning_tests, warning_case)
    if body == "normal":
        case = case_class(WARNING_CASES[warning_case])
    else:
        def test_body(self):
            warning_tests._reload_docker_manager()
            self.fail(BODY_FAILURE)

        case = type(f"{warning_case}BodyFailure", (case_class,), {"test_body": test_body})(
            "test_body"
        )
    result = unittest.TestResult()
    case.run(result)

    if body == "normal":
        assert result.wasSuccessful(), result.failures + result.errors
    else:
        assert result.errors == [], result.errors
        assert len(result.failures) == 1, result.failures
        assert BODY_FAILURE in result.failures[0][1], result.failures
    restored = _owned(owned_names, owned_prefixes)
    assert restored.keys() == entry.keys(), (
        f"{warning_case} did not restore the caller's module presence"
    )
    for name, module in entry.items():
        assert restored[name] is module, (
            f"{warning_case} did not restore the caller's {name} module object"
        )
