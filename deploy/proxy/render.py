#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Render the reviewed route table into one private, self-contained nginx.conf."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
DEFAULTS = {
    "OCU_WEBUI_UPSTREAM": "http://127.0.0.1:8080",
    "OCU_PROXY_UPSTREAM": "http://127.0.0.1:8090",
    "OCU_PROXY_LISTEN": "127.0.0.1:8082",
}
REVIEWED_TABLE_SHA256 = "3b64eb4e55adb688b60903ff84b4af78b8b13cf1b132b33f191204182b6ec534"
FIELDS = {"path", "methods", "auth", "mutating", "prefix", "kind"}
SEGMENT = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\Z")
PLACEHOLDER = {"chat", "path", "pid", "page"}
URI_PART = {
    "chat": r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*",
    "path": r"[^/?#]+(?:/[^/?#]+)*",
    "pid": r"[0-9]+",
    "page": r"[A-Za-z0-9._~-]+",
}
LOCATION_PART = dict(URI_PART, path=r".+")


class RenderError(ValueError):
    """Configuration is unusable; messages never contain input values."""


def _host_port(value: str, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9.-]+:[1-9][0-9]{0,4}", value):
        raise RenderError(f"{name} must be host:port")
    host, port = value.rsplit(":", 1)
    if not host or int(port) > 65535 or host.startswith("-") or host.endswith("-"):
        raise RenderError(f"{name} must be host:port")
    return value

def _endpoint(value: str, name: str) -> str:
    if not isinstance(value, str) or any(ord(c) < 0x21 or ord(c) > 0x7E for c in value):
        raise RenderError(f"{name} must be an absolute internal http origin with an explicit port")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "http" or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment or not parsed.hostname
                or parsed.port is None):
            raise ValueError
        _host_port(parsed.netloc, name)
    except (ValueError, TypeError):
        raise RenderError(f"{name} must be an absolute internal http origin with an explicit port") from None
    return parsed.netloc


def _origin(value: str) -> str:
    name = "OCU_WEBUI_ORIGIN"
    if not isinstance(value, str) or any(ord(c) < 0x21 or ord(c) > 0x7E for c in value):
        raise RenderError(f"{name} must be an absolute http(s) origin")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.path or parsed.query
                or parsed.fragment or parsed.netloc != parsed.netloc.lower()):
            raise ValueError
        if parsed.port is not None:
            _host_port(parsed.netloc, name)
        elif not re.fullmatch(r"[a-z0-9.-]+", parsed.netloc):
            raise ValueError
    except (ValueError, TypeError):
        raise RenderError(f"{name} must be an absolute http(s) origin") from None
    return value


def _table(table: Path) -> list[dict]:
    try:
        raw_bytes = table.read_bytes()
        source = json.loads(raw_bytes.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (OSError, ValueError, UnicodeError):
        raise RenderError("route table is invalid or unreviewed") from None
    if (not isinstance(source, dict) or set(source) != {"version", "rows"}
            or type(source["version"]) is not int or source["version"] != 1
            or not isinstance(source["rows"], list)):
        raise RenderError("route table schema is invalid")
    rows = source["rows"]
    if not rows or len(rows) != 22:
        raise RenderError("route table must contain 22 reviewed rows")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != FIELDS:
            raise RenderError("route row fields are invalid")
        path = row["path"]
        if not isinstance(path, str) or path in seen:
            raise RenderError("route path is invalid or duplicated")
        seen.add(path)
        parts = path.split("/")
        parameters = [part[1:-1] for part in parts if part.startswith("{") and part.endswith("}")]
        if (parts[0] not in {"api", "files", "preview", "browser", "terminal", "static"}
                or any(not (SEGMENT.fullmatch(part) or part in {"{chat}", "{path}", "{pid}", "{page}"}) for part in parts)
                or any(part.startswith("{") and part.endswith("}") and part[1:-1] not in PLACEHOLDER for part in parts)
                or parameters.count("chat") > 1 or parameters.count("path") > 1
                or ("path" in parameters and parts[-1] != "{path}")
                or ("pid" in parameters and "{chat}" not in parts)
                or ("page" in parameters and "{chat}" not in parts)):
            raise RenderError("route path has an invalid segment")
        methods = row["methods"]
        if (not isinstance(methods, list) or not methods or not all(isinstance(method, str) for method in methods)
                or len(methods) != len(set(methods))
                or not set(methods) <= {"GET", "HEAD", "POST"}
                or ("HEAD" in methods and "GET" not in methods)):
            raise RenderError("route methods are invalid")
        auth = row["auth"]
        if not isinstance(auth, str) or auth not in {"chat", "session"} or ("{chat}" in parts) != (auth == "chat"):
            raise RenderError("route auth is invalid")
        if row["prefix"] != ("preserve" if auth == "session" else "strip"):
            raise RenderError("route prefix is invalid")
        if (type(row["mutating"]) is not bool or not isinstance(row["kind"], str)
                or row["kind"] not in {"http", "ws", "file", "upload"}):
            raise RenderError("route classification is invalid")
        if row["kind"] == "ws" and (methods != ["GET"] or not path.endswith(("/ws", "/{page}"))):
            raise RenderError("route websocket classification is invalid")
        if row["kind"] == "upload" and (methods != ["POST"] or not path.startswith("api/uploads/")):
            raise RenderError("route upload classification is invalid")
        if ("POST" in methods and not row["mutating"]) or (row["kind"] == "upload" and methods != ["POST"]):
            raise RenderError("route mutation is invalid")
        if row["kind"] == "file" and (not path.startswith("files/") or "{path}" not in parts):
            raise RenderError("route file classification is invalid")
    if hashlib.sha256(raw_bytes).hexdigest() != REVIEWED_TABLE_SHA256:
        raise RenderError("route table differs from reviewed inventory")
    return rows


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _nginx_quoted(value: str) -> str:
    # Backslashes/quotes are nginx lexical escapes; dollars are nginx complex-value
    # variable prefixes. The geo variable expands to a literal dollar at request time.
    return (value.replace("\\", "\\\\").replace('"', '\\"')
            .replace("$", "${ocu_dollar}"))


def _raw_literal(segment: str) -> str:
    def encoded(char: str) -> str:
        digits = "".join(f"[{digit}{digit.upper()}]" if digit.isalpha() else digit
                         for digit in f"{ord(char):02x}")
        return f"(?:{re.escape(char)}|%{digits})"

    return "".join(encoded(char) for char in segment)


def _route_regex(path: str, *, raw: bool = False) -> str:
    pieces = []
    fragments = URI_PART if raw else LOCATION_PART
    for part in path.split("/"):
        if part.startswith("{"):
            name = part[1:-1]
            pieces.append(f"(?<ocu_{'raw' if raw else 'route'}_{name}>{fragments[name]})")
        else:
            pieces.append(_raw_literal(part) if raw else re.escape(part))
    return "/ocu/" + "/".join(pieces)


def _location(row: dict, ocu: str, bearer: str, *, named: str | None = None,
              shadow: tuple[str, list[str]] | None = None) -> str:
    pattern = _route_regex(row["path"])
    # nginx selects locations by normalized URI; validate the original request
    # against the same table row and compare the chat before forwarding it raw.
    raw = _route_regex(row["path"], raw=True)
    raw = re.sub(r"\(\?<ocu_raw_(?!chat>)[a-z]+>", "(?:", raw)
    raw = raw + r"(?:\?[^#]*)?$"
    auth = "_ocu_chat_auth" if row["auth"] == "chat" else "_ocu_session_auth"
    method = "|".join(row["methods"])
    selector = (f"~ ^{named}/(?<ocu_route_chat>{URI_PART['chat']})$"
                if named else "~ ^" + pattern + "$")
    lines = [f"        location {selector} {{"]
    if named:
        lines.append("            internal;")
    if shadow:
        target, methods = shadow
        lines.append(
            f"            if ($request_method ~ ^(?:{'|'.join(methods)})$) "
            f"{{ rewrite ^ {target}/$ocu_route_chat last; }}"
        )
    lines += [f'            if ($request_uri !~ "^{raw}") {{ return 404; }}']
    if row["auth"] == "chat":
        lines += [
            "            if ($ocu_route_chat != $ocu_raw_chat) { return 404; }",
            "            set $ocu_chat_id $ocu_route_chat;",
        ]
    lines += [
        f"            if ($request_method !~ ^(?:{method})$) {{ return 404; }}",
    ]
    if row["kind"] == "ws":
        lines += ["            if ($http_upgrade !~* ^websocket$) { return 404; }"]
    if row["mutating"] or "POST" in row["methods"]:
        lines += ["            error_page 418 = @ocu_mutation_denied;",
                  "            if ($ocu_mutation_denied) { return 418; }"]
    lines += [
        "            error_page 403 = @ocu_auth_not_found;",
        f"            auth_request /{auth};",
    ]
    if row["auth"] == "chat":
        lines += [
            "            auth_request_set $ocu_user_id $upstream_http_x_user_id;",
            "            auth_request_set $ocu_user_email $upstream_http_x_user_email;",
        ]
    else:
        lines += ["            set $ocu_user_id \"\";", "            set $ocu_user_email \"\";"]
    if row["prefix"] == "strip":
        lines += [
            '            if ($request_uri ~ "^/ocu(?<ocu_forward>/.*)$") { set $ocu_upstream_uri $ocu_forward; }',
        ]
    else:
        lines += ["            set $ocu_upstream_uri $request_uri;"]
    lines += [
        "            proxy_http_version 1.1;",
        f"            proxy_pass {ocu}$ocu_upstream_uri;",
        f'            proxy_set_header Authorization "Bearer {bearer}";',
        "            proxy_set_header X-OCU-Internal-Token \"\";",
        "            proxy_set_header X-Ocu-Grant \"\";",
        "            proxy_set_header X-Chat-Id $ocu_chat_id;" if row["auth"] == "chat" else "            proxy_set_header X-Chat-Id \"\";",
        "            proxy_set_header X-User-Id $ocu_user_id;",
        "            proxy_set_header X-User-Email $ocu_user_email;",
        "            proxy_set_header X-Forwarded-User \"\";",
        "            proxy_set_header Cookie $http_cookie;",
        "            proxy_set_header Host $proxy_host;",
        "            proxy_set_header X-Forwarded-For $remote_addr;",
        "            proxy_set_header X-Forwarded-Proto $scheme;",
        "            proxy_set_header X-Forwarded-Host \"\";",
        "            proxy_set_header X-Real-IP $remote_addr;",
        "            proxy_intercept_errors off;",
    ]
    if row["kind"] == "ws":
        lines += [
            "            proxy_set_header Upgrade $http_upgrade;",
            "            proxy_set_header Connection \"upgrade\";",
            "            proxy_read_timeout 3600s;",
            "            proxy_send_timeout 3600s;",
        ]
    else:
        lines += ["            proxy_set_header Upgrade \"\";", "            proxy_set_header Connection \"\";"]
    if row["kind"] == "file":
        lines += [
            "            proxy_hide_header Content-Security-Policy;",
            "            proxy_hide_header X-Content-Type-Options;",
            "            add_header Content-Security-Policy $ocu_csp always;",
            "            add_header X-Content-Type-Options $ocu_nosniff always;",
        ]
    lines.append("        }")
    return "\n".join(lines)


def _locations(rows: list[dict], ocu: str, bearer: str) -> str:
    # A literal read row can overlap a path-catchall write row. Dispatch by
    # method before rejecting a valid upload under that more-specific location.
    shadows = {}
    named = []
    for index, generic in enumerate(rows):
        if generic["path"].endswith("/{path}") and generic["methods"] == ["POST"]:
            prefix = generic["path"][:-len("{path}")]
            target = f"/_ocu_method_{index}"
            for row in rows:
                suffix = row["path"][len(prefix):]
                if (row is not generic and row["path"].startswith(prefix)
                        and suffix and "{" not in suffix
                        and set(row["methods"]).isdisjoint(generic["methods"])
                        and row["auth"] == generic["auth"]):
                    if row["path"] in shadows:
                        raise RenderError("route methods overlap ambiguously")
                    shadows[row["path"]] = (target, generic["methods"])
            if any(shadow[0] == target for shadow in shadows.values()):
                named.append(_location(generic, ocu, bearer, named=target))
    return "\n".join([*(_location(row, ocu, bearer, shadow=shadows.get(row["path"]))
                        for row in rows), *named])


def render(*, table: Path = ROOT / "routes.json", output: Path = ROOT / "nginx.conf",
           env: dict[str, str] | None = None, nginx: str | None = None) -> Path:
    variables = os.environ if env is None else env
    token = variables.get("OCU_INTERNAL_TOKEN", "")
    if not token or any(not 0x21 <= ord(c) <= 0x7E for c in token):
        raise RenderError("OCU_INTERNAL_TOKEN must be nonempty visible ASCII")
    origin = _origin(variables.get("OCU_WEBUI_ORIGIN", ""))
    webui = _endpoint(variables.get("OCU_WEBUI_UPSTREAM", DEFAULTS["OCU_WEBUI_UPSTREAM"]), "OCU_WEBUI_UPSTREAM")
    ocu = _endpoint(variables.get("OCU_PROXY_UPSTREAM", DEFAULTS["OCU_PROXY_UPSTREAM"]), "OCU_PROXY_UPSTREAM")
    listen = _host_port(variables.get("OCU_PROXY_LISTEN", DEFAULTS["OCU_PROXY_LISTEN"]), "OCU_PROXY_LISTEN")
    nginx = nginx or shutil.which("nginx")
    if nginx is None:
        raise RenderError("nginx executable is unavailable")
    rows = _table(table)
    output = output.absolute()
    if (not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(output)) or ".." in output.parts
            or output.is_symlink() or not output.parent.is_dir()):
        raise RenderError("nginx.conf target is unsafe")
    runtime = output.parent / "runtime"
    if runtime.is_symlink() or (runtime.exists() and (not runtime.is_dir() or runtime.stat().st_mode & 0o077)):
        raise RenderError("runtime directory must be private")
    runtime.mkdir(mode=0o700, exist_ok=True)
    for subdir in ("client_body", "proxy"):
        path = runtime / subdir
        if path.is_symlink() or (path.exists() and (not path.is_dir() or path.stat().st_mode & 0o077)):
            raise RenderError("runtime directory must be private")
        path.mkdir(mode=0o700, exist_ok=True)
    for filename in ("nginx.pid", "error.log"):
        path = runtime / filename
        if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_uid != os.getuid())):
            raise RenderError("runtime files must be private")
    template = (ROOT / "nginx.conf.in").read_text(encoding="utf-8")
    replacements = {
        "@@RUNTIME@@": str(runtime), "@@ORIGIN_REGEX@@": re.escape(origin),
        "@@LISTEN@@": listen, "@@WEBUI@@": "http://ocu_proxy_webui",
        "@@WEBUI_HOSTPORT@@": webui, "@@OCU_HOSTPORT@@": ocu,
        "@@LOCATIONS@@": _locations(rows, "http://ocu_proxy_ocu", _nginx_quoted(token)),
    }
    markers = re.findall(r"@@[A-Z_]+@@", template)
    if set(markers) != set(replacements):
        raise RenderError("nginx template is incomplete")
    template = re.sub(r"@@[A-Z_]+@@", lambda match: replacements[match.group()], template)
    fd, candidate = tempfile.mkstemp(prefix=".nginx.conf.", suffix=".tmp", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(template)
            stream.flush()
            os.fsync(stream.fileno())
        result = subprocess.run([nginx, "-t", "-c", candidate], capture_output=True, check=False)
        if result.returncode:
            raise RenderError("nginx configuration validation failed")
        os.replace(candidate, output)
    finally:
        if os.path.exists(candidate):
            os.unlink(candidate)
    return output


if __name__ == "__main__":
    try:
        render()
    except (RenderError, OSError) as exc:
        print(f"proxy render: {exc}", file=sys.stderr)
        sys.exit(1)
    print("proxy render: private nginx.conf ready")
