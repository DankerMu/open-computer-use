# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Claude Code through Open WebUI's Messages ingress (#656, #662).

One gateway mode switch and one per-call environment builder, shared by the
workspace terminal (`app.start_ttyd`) and the `sub_agent` MCP tool. The caller
maps `GatewayInputError` to its own error shape and passes the environment
only to the exec that starts Claude Code.
"""

import hashlib
import ipaddress
import os
import re
from typing import Dict, Optional

MESSAGES_ADDRESS_ENV = "OCU_SANDBOX_MESSAGES_ADDRESS"
_MODEL = re.compile(r"[\x21-\x7e]{1,256}")
_NO_TOKEN = (
    "Claude Code gateway: the request carries no Open WebUI login token; "
    "sign in again and reopen the terminal"
)
_NO_MODEL = (
    "Claude Code gateway: the chat has no usable model id (1-256 printable ASCII "
    "characters without spaces); select a model and reopen the terminal"
)


class GatewayConfigError(RuntimeError):
    """OCU_SANDBOX_MESSAGES_ADDRESS is set but is not an IPv4 address."""


class GatewayInputError(ValueError):
    """The caller's token or model is missing or invalid; `detail` never contains the token."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def gateway_base_url() -> Optional[str]:
    """Messages ingress base URL for Claude Code, or None when gateway mode is off.

    Off when the address is unset or the sandbox CLI is not Claude Code. A set value that
    is not an IPv4 literal (including empty) refuses startup, naming the variable.
    """
    if MESSAGES_ADDRESS_ENV not in os.environ:
        return None
    try:
        address = ipaddress.IPv4Address(os.environ[MESSAGES_ADDRESS_ENV])
    except ValueError:
        raise GatewayConfigError(
            f"{MESSAGES_ADDRESS_ENV} must be an IPv4 address; refusing to start"
        ) from None
    from docker_manager import SUBAGENT_CLI
    if SUBAGENT_CLI != "claude":
        return None
    return f"http://{address}:8080/api"


def gateway_environment(base_url: str, token: Optional[str], model) -> Dict[str, str]:
    """Per-call Claude Code environment; GatewayInputError when the token or model is unusable."""
    if not isinstance(token, str) or not token:
        raise GatewayInputError(_NO_TOKEN)
    if not isinstance(model, str) or not _MODEL.fullmatch(model):
        raise GatewayInputError(_NO_MODEL)
    fingerprint = hashlib.sha256(f"{base_url}\0{model}\0{token}".encode()).hexdigest()
    return {
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_AUTH_TOKEN": token,
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        "CLAUDE_CODE_SUBAGENT_MODEL": model,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
        "OCU_TERMINAL_FINGERPRINT": fingerprint,
    }
