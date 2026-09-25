#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
# Validate complete resolved Compose JSON without printing environment data.
set -euo pipefail

export OCU_CHECK_PROXY_SERVICE="${OCU_CHECK_PROXY_SERVICE:-proxy}"
export OCU_CHECK_WEBUI_SERVICE="${OCU_CHECK_WEBUI_SERVICE:-open-webui}"
export OCU_CHECK_OCU_SERVICE="${OCU_CHECK_OCU_SERVICE:-computer-use-server}"
export OCU_CHECK_PROXY_TARGET="${OCU_CHECK_PROXY_TARGET:-8082}"
export OCU_CHECK_PROXY_PUBLISHED="${OCU_CHECK_PROXY_PUBLISHED:-${OCU_PROXY_PORT:-8082}}"
export OCU_CHECK_SANDBOX_NETWORK="${OCU_CHECK_SANDBOX_NETWORK:-${OCU_SANDBOX_NETWORK:-ocu-sandbox}}"
export OCU_CHECK_CONTROL_NETWORK="${OCU_CHECK_CONTROL_NETWORK:-${OCU_PRIVATE_NETWORK:-ocu-test-private}}"

if [[ "$#" -lt 1 ]]; then
    printf '%s\n' 'check-ports: resolved Compose JSON documents are required' >&2
    exit 1
fi

python3 - "$@" <<'PY'
from __future__ import annotations

import json
import os
import sys

PROXY = os.environ["OCU_CHECK_PROXY_SERVICE"]
WEBUI = os.environ["OCU_CHECK_WEBUI_SERVICE"]
OCU = os.environ["OCU_CHECK_OCU_SERVICE"]
TARGET = os.environ["OCU_CHECK_PROXY_TARGET"]
PUBLISHED = os.environ["OCU_CHECK_PROXY_PUBLISHED"]
SANDBOX = os.environ["OCU_CHECK_SANDBOX_NETWORK"]
CONTROL = os.environ["OCU_CHECK_CONTROL_NETWORK"]


def fail(message: str) -> None:
    print(f"check-ports: {message}", file=sys.stderr)
    raise SystemExit(1)


def load(path: str):
    try:
        with open(path, encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, json.JSONDecodeError, UnicodeError):
        fail("malformed or unreadable resolved Compose JSON")
    if not isinstance(payload, dict) or not isinstance(payload.get("services"), dict):
        fail("resolved Compose document has no services object")
    networks = payload.get("networks", {})
    if not isinstance(networks, dict):
        fail("resolved Compose networks are malformed")
    return payload["services"], networks


def networks_for(name: str, service: dict, networks: dict) -> set[str]:
    declared = service.get("networks")
    if declared is None:
        aliases = ["default"] if "default" in networks else []
    elif isinstance(declared, dict):
        aliases = list(declared)
    elif isinstance(declared, list) and all(isinstance(alias, str) for alias in declared):
        aliases = declared
    else:
        fail(f"{name}: malformed networks")
    attached = set()
    for alias in aliases:
        definition = networks.get(alias, {})
        if not isinstance(definition, dict):
            fail(f"{name}: malformed network definition")
        attached.add(definition.get("name") or alias)
    return attached


docs = [load(path) for path in sys.argv[1:]]
seen: set[str] = set()
for services, _ in docs:
    for name in services:
        if name in seen:
            fail(f"{name}: duplicate service across resolved stacks")
        seen.add(name)
for critical in (PROXY, WEBUI, OCU):
    if critical not in seen:
        fail(f"{critical}: required service is missing")

memberships: dict[str, set[str]] = {}
for services, networks in docs:
    for name, service in services.items():
        if not isinstance(service, dict):
            fail(f"{name}: malformed service")
        mode = service.get("network_mode")
        if mode and (not isinstance(mode, str) or mode in {"host", "none"} or
                     mode.startswith(("service:", "container:"))):
            fail(f"{name}: forbidden host or shared network mode")
        attached = networks_for(name, service, networks)
        if SANDBOX in attached:
            fail(f"{name}: joined sandbox network {SANDBOX}")
        if CONTROL not in attached:
            fail(f"{name}: not attached to the control-plane network")
        memberships[name] = attached
        ports = service.get("ports", [])
        if not isinstance(ports, list):
            fail(f"{name}: malformed ports")
        if name != PROXY:
            if ports:
                fail(f"{name}: host publication is forbidden")
            continue
        if len(ports) != 1 or not isinstance(ports[0], dict):
            fail(f"{name}: expected exactly one TCP publication")
        mapping = ports[0]
        target = str(mapping.get("target") or "")
        published = str(mapping.get("published") or "")
        protocol = str(mapping.get("protocol") or "tcp")
        bound = str(mapping.get("host_ip") or "")
        if target != TARGET or published != PUBLISHED or protocol != "tcp":
            fail(f"{name}: incorrect TCP listen/publication mapping")
        if bound not in {"", "0.0.0.0"}:
            fail(f"{name}: proxy publication must bind the configured LAN entry")
        environment = service.get("environment")
        if not isinstance(environment, dict):
            fail(f"{name}: missing proxy listen/upstream configuration")
        expected = {
            "OCU_PROXY_LISTEN": f"0.0.0.0:{TARGET}",
            "OCU_WEBUI_UPSTREAM": f"http://{WEBUI}:8080",
            "OCU_PROXY_UPSTREAM": f"http://{OCU}:8081",
        }
        if any(environment.get(key) != value for key, value in expected.items()):
            fail(f"{name}: incorrect proxy listen/upstream configuration")

for application in (WEBUI, OCU):
    if CONTROL not in memberships[PROXY] or CONTROL not in memberships[application]:
        fail(f"{PROXY}: does not share the control-plane network with {application}")
PY
