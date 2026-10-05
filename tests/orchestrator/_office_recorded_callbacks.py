# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""B1 recorded DocumentServer callback protocol fields for Office route tests.

Extracted from WebUI ``docs/evidence/issue-119/observations.json.gz``. Routing
identities are replaced per session; harness-added sha256/jwt_valid metadata is
not treated as a DocumentServer claim.
"""
from __future__ import annotations

import copy
import json

STATUS_1 = {
    "provenance": "B1 recorded callback",
    "archive": "docs/evidence/issue-119/observations.json.gz",
    "pointer": "/state-before-cleanup.json/callbacks/0",
    "payload": {
        "actions": [{"type": 1, "userid": "user-1"}],
        "key": "b1-docx-main",
        "status": 1,
        "users": ["user-1"],
    },
}

STATUS_2 = {
    "provenance": "B1 recorded callback",
    "archive": "docs/evidence/issue-119/observations.json.gz",
    "pointer": "/close-proof.json/callback",
    "payload": {
        "actions": [{"type": 0, "userid": "user-1"}],
        "filetype": "docx",
        "key": "b1-docx-main",
        "lastsave": "2026-10-03T15:14:35.000Z",
        "notmodified": True,
        "status": 2,
        "users": ["user-1"],
    },
}

STATUS_4 = {
    "provenance": "B1 recorded callback",
    "archive": "docs/evidence/issue-119/observations.json.gz",
    "pointer": "/state-before-cleanup.json/callbacks/5",
    "payload": {
        "actions": [{"type": 0, "userid": "user-1"}],
        "key": "b1-pptx-main",
        "status": 4,
    },
}

STATUS_6 = {
    "provenance": "B1 recorded callback",
    "archive": "docs/evidence/issue-119/observations.json.gz",
    "pointer": "/docx-export-proof.json/callback",
    "payload": {
        "filetype": "docx",
        "forcesavetype": 0,
        "key": "b1-docx-main",
        "lastsave": "2026-10-03T15:14:35.000Z",
        "status": 6,
        "users": ["user-1"],
    },
}

STATUS_3 = {
    "provenance": "synthetic protocol-error variant",
    "payload": {
        "key": "b1-docx-main",
        "status": 3,
    },
}

STATUS_7 = {
    "provenance": "synthetic protocol-error variant",
    "payload": {
        "key": "b1-docx-main",
        "status": 7,
    },
}


def recorded_status_1_payload(*, document_key: str) -> dict:
    payload = copy.deepcopy(STATUS_1["payload"])
    payload["key"] = document_key
    return payload


def recorded_status_2_payload(*, document_key: str, url: str) -> dict:
    payload = copy.deepcopy(STATUS_2["payload"])
    payload["key"] = document_key
    payload["url"] = url
    return payload


def recorded_status_4_payload(*, document_key: str) -> dict:
    payload = copy.deepcopy(STATUS_4["payload"])
    payload["key"] = document_key
    return payload


def recorded_status_6_payload(*, document_key: str, url: str, save_seq: int, intent: str) -> dict:
    payload = copy.deepcopy(STATUS_6["payload"])
    payload["key"] = document_key
    payload["url"] = url
    payload["userdata"] = json.dumps({"save_seq": save_seq, "intent": intent}, separators=(",", ":"))
    return payload


def synthetic_status_3_payload(*, document_key: str) -> dict:
    payload = copy.deepcopy(STATUS_3["payload"])
    payload["key"] = document_key
    return payload


def synthetic_status_7_payload(*, document_key: str, save_seq: int, intent: str) -> dict:
    payload = copy.deepcopy(STATUS_7["payload"])
    payload["key"] = document_key
    payload["userdata"] = json.dumps({"save_seq": save_seq, "intent": intent}, separators=(",", ":"))
    return payload
