# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Office setting names, enablement, and fail-closed validation."""
from __future__ import annotations

import os

OCU_OFFICE_DOCSERVER_URL = "OCU_OFFICE_DOCSERVER_URL"
OCU_OFFICE_DOCSERVER_ORIGIN = "OCU_OFFICE_DOCSERVER_ORIGIN"
OCU_OFFICE_SELF_URL = "OCU_OFFICE_SELF_URL"
OCU_OFFICE_JWT_SECRET = "OCU_OFFICE_JWT_SECRET"

MIN_FREE_BYTES = 1024**3
SOURCE_TICKET_TTL_SECONDS = 300
SESSION_LIVENESS_INTERVAL_SECONDS = 600
SAVE_CALLBACK_TIMEOUT_SECONDS = 30


def enabled() -> bool:
    return bool(os.environ.get(OCU_OFFICE_DOCSERVER_URL, "").strip())


def validation_error() -> str | None:
    if not enabled():
        return None
    for name in (
        OCU_OFFICE_JWT_SECRET,
        OCU_OFFICE_DOCSERVER_ORIGIN,
        OCU_OFFICE_SELF_URL,
    ):
        if not os.environ.get(name, "").strip():
            return name
    return None
