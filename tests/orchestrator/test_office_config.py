# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Public settings and call-time Office enablement contracts."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[2] / "computer-use-server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from office import config


@pytest.fixture(autouse=True)
def isolated_office_env(monkeypatch):
    for name in (
        "OCU_OFFICE_DOCSERVER_URL",
        "OCU_OFFICE_DOCSERVER_ORIGIN",
        "OCU_OFFICE_SELF_URL",
        "OCU_OFFICE_JWT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)


def test_setting_names_match_the_cross_component_contract():
    assert config.OCU_OFFICE_DOCSERVER_URL == "OCU_OFFICE_DOCSERVER_URL"
    assert config.OCU_OFFICE_DOCSERVER_ORIGIN == "OCU_OFFICE_DOCSERVER_ORIGIN"
    assert config.OCU_OFFICE_SELF_URL == "OCU_OFFICE_SELF_URL"
    assert config.OCU_OFFICE_JWT_SECRET == "OCU_OFFICE_JWT_SECRET"


def test_tuning_defaults_are_positive_integers_and_liveness_outlasts_tickets():
    for value in (
        config.MIN_FREE_BYTES,
        config.SOURCE_TICKET_TTL_SECONDS,
        config.SESSION_LIVENESS_INTERVAL_SECONDS,
        config.SAVE_CALLBACK_TIMEOUT_SECONDS,
    ):
        assert type(value) is int
        assert value > 0
    assert config.SESSION_LIVENESS_INTERVAL_SECONDS > config.SOURCE_TICKET_TTL_SECONDS


def test_environment_changes_after_import_control_enablement_and_validation(monkeypatch):
    assert config.enabled() is False
    assert config.validation_error() is None
    monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", " http://documentserver ")
    assert config.enabled() is True
    assert config.validation_error() == "OCU_OFFICE_JWT_SECRET"
    secret = " \tconfig-test-canary\n "
    monkeypatch.setenv("OCU_OFFICE_JWT_SECRET", secret)
    assert config.validation_error() == "OCU_OFFICE_DOCSERVER_ORIGIN"
    monkeypatch.setenv("OCU_OFFICE_DOCSERVER_ORIGIN", "not-url-validated")
    assert config.validation_error() == "OCU_OFFICE_SELF_URL"
    monkeypatch.setenv("OCU_OFFICE_SELF_URL", "also-not-url-validated")
    assert config.validation_error() is None
    assert os.environ["OCU_OFFICE_JWT_SECRET"] == secret
    monkeypatch.setenv("OCU_OFFICE_JWT_SECRET", " \t\n")
    assert config.validation_error() == "OCU_OFFICE_JWT_SECRET"
    monkeypatch.setenv("OCU_OFFICE_DOCSERVER_URL", " \t\n")
    assert config.enabled() is False
    assert config.validation_error() is None
    monkeypatch.delenv("OCU_OFFICE_DOCSERVER_URL")
    assert config.enabled() is False
    assert config.validation_error() is None
