# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Run evaluation requests through the ocu-eval stack's WebUI API and record each attempt.

Started by run.sh on the evaluation host (Python 3.10, standard library only), with
the stack and image variables exported and the provider key on stdin. It holds
${EVAL_ROOT}/eval.lock for the whole batch, so cleanup.sh cannot run meanwhile.
Each attempt gets ${EVAL_ROOT}/runs/<phase>/<request>/r<repetition>-a<attempt>/
with pending.json, transcript.json, the MP4 outputs and manifest.json (written
last). Every chat id is appended to ${EVAL_ROOT}/chats.txt before the driver
waits on it. Every value of stack.env, the provider key and the session token
are replaced by [REDACTED] before anything reaches a file, stdout or stderr.
"""

from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

SOURCE = Path(__file__).resolve().parents[2]
MODEL = "qwen3.8-27b"
VIDEO_FILTER = "video_production_filter"
REQUIRED_FUNCTIONS = ("computer_use_filter", VIDEO_FILTER)
ADAPTER_URL = "http://adapter:8090/v1"
SPEECH_CONTAINER = "ocu-eval-computer-use-server"
# Every running stack container (compose.eval.yml names) and the role whose image it runs.
CONTAINER_ROLES = {
    "ocu-eval-computer-use-server": "computer-use-server",
    "ocu-eval-adapter": "computer-use-server",
    "ocu-eval-open-webui": "open-webui",
    "ocu-eval-postgres": "postgres",
}
POLL_SECONDS = 15
RETRY_SECONDS = 5
RETRIES = 3
TIME_LIMIT_SECONDS = 60 * 60
STOP_WAIT_SECONDS = 120
HTTP_TIMEOUT = 60
# WebUI answers the completion at once and runs the tool loop as a background task.
POST_TIMEOUT = 120
DOCKER_TIMEOUT = 600
MAX_ATTEMPTS = 3
# start.sh returns once open-webui-init has exited; on the host the first sign-in right
# after it was reset while WebUI was still coming up, so a batch's first sign-in is retried.
SIGNIN_TRIES = 5
SIGNIN_RETRY_SECONDS = 3
# The sandbox writes the outputs directory. A runaway render or a sparse file must not fill
# EVAL_ROOT's filesystem, and thousands of entries must not make collection unbounded.
MAX_MP4_BYTES = 4 * 1024 ** 3
MAX_MP4_FILES = 20
# common.sh's 15 GiB floor, also kept free on EVAL_ROOT's filesystem while copying outputs.
FLOOR_BYTES = 15 * 1024 ** 3
DATA_URI_LIMIT = 4096
REASON_LIMIT = 200
REDACTED = "[REDACTED]"
# Asia/Shanghai has kept UTC+8 without daylight saving since 1991; a fixed offset
# needs no tzdata on the host. Weekday names match the frontend's English list.
SHANGHAI = datetime.timezone(datetime.timedelta(hours=8))
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# The OCU tool wrapper's own transport faults (openwebui/tools/computer_use_tools.py).
WRAPPER_PREFIXES = ("[CONFIG ERROR]", "[NETWORK ERROR]", "[MCP TRANSPORT ERROR]",
                    "[UNEXPECTED ERROR]", "[Timeout")
IMAGE_TOOLS = ("generate_image", "edit_image")
SPEECH_TOOL = "generate_speech"
CHAT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
RUN_DIR = re.compile(r"r([1-9][0-9]*)-a([1-9][0-9]*)")
REQUEST_ID = re.compile(r"[a-z0-9-]{3,40}")
TIMESTAMP = "%Y-%m-%dT%H:%M:%SZ"
# Proxies from the environment never see the session token.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Refusal(Exception):
    """Stops the batch, with a non-zero exit, before any further POST."""


class CallFailed(Exception):
    """A WebUI call that failed after its retries; the text is the failure reason."""


class TooLarge(Exception):
    """An output above the size cap or the free space above the floor."""


class Redactor:
    def __init__(self):
        self.values: set[str] = set()

    def add(self, value: str):
        if value:
            self.values.add(value)
            self.values.add(json.dumps(value, ensure_ascii=False)[1:-1])

    def text(self, text: str) -> str:
        for value in sorted(self.values, key=len, reverse=True):
            text = text.replace(value, REDACTED)
        return text

    def say(self, text: str, stream=None):
        print(self.text(text), file=stream or sys.stdout, flush=True)

    def write_json(self, path: Path, payload):
        """Redact, then write through a private temporary file and a rename."""
        text = self.text(json.dumps(payload, ensure_ascii=False, indent=2)) + "\n"
        partial = path.with_name(f".{path.name}.partial.{os.getpid()}")
        descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(partial, path)


def describe(error: BaseException) -> str:
    reason = getattr(error, "reason", None)
    if isinstance(reason, BaseException):
        return type(reason).__name__
    return type(error).__name__


def transient(error: BaseException | None) -> bool:
    """A connection that was refused or reset: the server was not ready to answer."""
    reason = getattr(error, "reason", error)
    return isinstance(reason, (ConnectionRefusedError, ConnectionResetError))


class WebUI:
    def __init__(self, port: str, email: str, password: str, redactor: Redactor):
        self.base = f"http://127.0.0.1:{port}"
        self.email = email
        self.password = password
        self.redactor = redactor
        self.token = ""
        self.name = ""

    def exchange(self, method: str, path: str, body=None, timeout=HTTP_TIMEOUT):
        """One request: (status, JSON payload or None). Transport faults raise."""
        request = urllib.request.Request(self.base + path, method=method)
        if body is not None:
            request.data = json.dumps(body).encode("utf-8")
            request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with OPENER.open(request, timeout=timeout) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:
            error.close()
            return error.code, None
        return status, json.loads(raw) if raw else None

    def signin(self):
        self.token = ""
        try:
            status, payload = self.exchange("POST", "/api/v1/auths/signin",
                                            {"email": self.email, "password": self.password})
        except (OSError, http.client.HTTPException, ValueError) as error:
            raise CallFailed(f"POST /api/v1/auths/signin: {describe(error)}") from error
        token = payload.get("token") if status == 200 and isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise CallFailed(f"POST /api/v1/auths/signin: HTTP {status}")
        self.redactor.add(token)
        self.token = token
        self.name = payload.get("name") if isinstance(payload.get("name"), str) else ""

    def call(self, method: str, path: str):
        """Retried up to RETRIES times, signing in again once on 401; None for 404."""
        detail = ""
        signed_in = False
        for attempt in range(RETRIES + 1):
            if attempt:
                time.sleep(RETRY_SECONDS)
            try:
                status, payload = self.exchange(method, path, {} if method == "POST" else None)
            except (OSError, http.client.HTTPException, ValueError) as error:
                detail = describe(error)
                continue
            if status == 200:
                return payload
            if status == 404:
                return None
            detail = f"HTTP {status}"
            if status == 401:
                if signed_in:
                    break
                signed_in = True
                self.signin()
        raise CallFailed(f"{method} {path}: {detail}")

    def get(self, path: str):
        return self.call("GET", path)

    def find_chat(self, user_message_id: str, since: int) -> str | None:
        """The chat updated since `since` whose history holds the user message."""
        listing = self.get("/api/v1/chats/?page=1")
        for item in listing if isinstance(listing, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("updated_at"), int):
                continue
            chat_id = item.get("id")
            if item["updated_at"] < since or not isinstance(chat_id, str) or not CHAT_ID.fullmatch(chat_id):
                continue
            if user_message_id in history(self.get(f"/api/v1/chats/{chat_id}")):
                return chat_id
        return None

    def submit(self, body: dict, user_message_id: str, since: int) -> str:
        """POST once; repeat only when the connection was refused before sending."""
        lost = "post lost"
        for attempt in range(RETRIES + 1):
            if attempt:
                time.sleep(RETRY_SECONDS)
            try:
                status, payload = self.exchange("POST", "/api/chat/completions", body, POST_TIMEOUT)
            except urllib.error.URLError as error:
                if isinstance(error.reason, ConnectionRefusedError):
                    continue
                break
            except (OSError, http.client.HTTPException, ValueError):
                break
            chat_id = payload.get("chat_id") if isinstance(payload, dict) else None
            if status == 200 and isinstance(chat_id, str) and CHAT_ID.fullmatch(chat_id):
                return chat_id
            if status != 200:
                lost = f"POST /api/chat/completions: HTTP {status}"
            break
        else:
            raise CallFailed("POST /api/chat/completions: connection refused")
        # The request may have created the chat: find it instead of sending it again.
        try:
            chat_id = self.find_chat(user_message_id, since)
        except CallFailed as failure:
            # No manifest: the chat may exist outside the ledger, and the next run's
            # recovery repeats this lookup from pending.json.
            raise Refusal(f"the POST answer was lost and the chat lookup failed ({failure}); "
                          "the next run looks the chat up again") from None
        if chat_id is None:
            raise CallFailed(lost)
        return chat_id


def history(chat) -> dict:
    inner = chat.get("chat") if isinstance(chat, dict) else None
    record = inner.get("history") if isinstance(inner, dict) else None
    messages = record.get("messages") if isinstance(record, dict) else None
    return messages if isinstance(messages, dict) else {}


def shrink(value):
    """The transcript with every long data: URI replaced by its hash and size."""
    if isinstance(value, dict):
        return {key: shrink(item) for key, item in value.items()}
    if isinstance(value, list):
        return [shrink(item) for item in value]
    if isinstance(value, str) and len(value) > DATA_URI_LIMIT and value.startswith("data:"):
        raw = value.encode("utf-8")
        return f"<data-uri sha256={hashlib.sha256(raw).hexdigest()} bytes={len(raw)}>"
    return value


def tool_calls(message: dict) -> list[tuple[str, str, str]]:
    """(name, status, result text) of every function_call in the message output."""
    output = message.get("output")
    output = output if isinstance(output, list) else []
    results = {}
    for item in output:
        if isinstance(item, dict) and item.get("type") == "function_call_output":
            parts = item.get("output") if isinstance(item.get("output"), list) else []
            results[item.get("call_id")] = "".join(
                part["text"] for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str))
    return [(str(item.get("name")), str(item.get("status")), results.get(item.get("call_id"), ""))
            for item in output if isinstance(item, dict) and item.get("type") == "function_call"]


def message_error(message: dict) -> str:
    error = message.get("error")
    if isinstance(error, dict):
        error = error.get("content")
    if isinstance(error, str):
        return error
    return json.dumps(error) if error else ""


def infrastructure_fault(calls) -> str | None:
    """Row 8: the first tool result that shows an adapter or orchestrator failure."""
    for name, _status, text in calls:
        if name == SPEECH_TOOL and text.startswith("Error: speech synthesis"):
            return f"adapter: {name}"
        if name in IMAGE_TOOLS:
            try:
                error = json.loads(text).get("error")
            except (ValueError, AttributeError):
                error = None
            if isinstance(error, str) and (error.startswith("400: [ERROR:")
                                           or error == "400: Something went wrong :/"):
                return f"adapter: {name}"
        if text.startswith(WRAPPER_PREFIXES) or (text.startswith("[Error] ") and "wrapper crashed" in text):
            return f"orchestrator: {name}"
    return None


def classify(stopped: bool, message: dict | None, call_failure: str | None, valid_mp4: bool):
    """The outcome table: (outcome, class, reason); the first matching row wins."""
    calls = tool_calls(message or {})
    error = message_error(message or {})
    done = message is not None and message.get("done") is True
    if not stopped and call_failure is None and done and not error and valid_mp4:
        return "succeeded", None, None
    if stopped:
        return "failed", "model", "time limit"
    if any(name == "ask_user" and status == "pending" for name, status, _ in calls):
        return "failed", "model", "asked the user"
    if error.startswith("Tool-call limit reached"):
        return "failed", "model", "tool-call limit"
    if error:
        return "failed", "infra", f"model API: {error}"
    if message is not None and not done:
        return "failed", "infra", "task lost"
    if call_failure is not None:
        return "failed", "infra", call_failure
    fault = None if valid_mp4 else infrastructure_fault(calls)
    if fault:
        return "failed", "infra", fault
    return "failed", "model", "no valid MP4"


def counts(message: dict | None) -> dict:
    names = [name for name, _status, _text in tool_calls(message or {})]
    return {"tool_calls": len(names),
            "image_calls": sum(name in IMAGE_TOOLS for name in names),
            "speech_calls": names.count(SPEECH_TOOL)}


def take_lock(root: Path) -> int:
    """The batch lock cleanup.sh takes with flock -n; refuses while anyone holds it."""
    descriptor = os.open(root / "eval.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        raise Refusal(f"{root / 'eval.lock'} is held by another driver or by cleanup.sh") from None
    return descriptor


def utc(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime(TIMESTAMP)


class Driver:
    def __init__(self, args, redactor: Redactor, stack: dict[str, str]):
        self.args = args
        self.redactor = redactor
        self.root = Path(os.environ["EVAL_ROOT"])
        self.roles = os.environ["EVAL_ROLES"].split()
        self.child_env = {name: value for name, value in os.environ.items() if name not in stack}
        self.webui = WebUI(os.environ["EVAL_WEBUI_PORT"], stack["ADMIN_EMAIL"], stack["ADMIN_PASSWORD"],
                           redactor)
        self.context: dict = {}
        self.stopped = False
        payload = json.loads((SOURCE / "deploy" / "eval" / "requests.json").read_text(encoding="utf-8"))
        self.requests = {request["id"]: request for request in payload["requests"]}
        if not all(REQUEST_ID.fullmatch(request_id) for request_id in self.requests):
            raise Refusal("requests.json holds an id that is not a plain directory name")

    def docker(self, *args: str) -> tuple[int, str]:
        try:
            result = subprocess.run(["docker", *args], env=self.child_env, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=DOCKER_TIMEOUT, check=False)
        except subprocess.TimeoutExpired:
            return 124, ""
        return result.returncode, result.stdout.strip()

    def plan(self) -> list[tuple[dict, int]]:
        if self.args.request is None:
            if self.args.repetition is not None:
                raise Refusal("--repetition needs --request")
            return [(request, repetition) for request in self.requests.values()
                    for repetition in range(1, request["repetitions"] + 1)]
        request = self.requests.get(self.args.request)
        if request is None:
            raise Refusal(f"request {self.args.request} is not in requests.json")
        repetition = self.args.repetition or 1
        if not 1 <= repetition <= request["repetitions"]:
            raise Refusal(f"request {request['id']} has {request['repetitions']} repetitions")
        return [(request, repetition)]

    def run(self) -> int:
        plan = self.plan()
        lock = take_lock(self.root)
        try:
            try:
                self.first_signin()
                recovered = self.recover()
                self.context = self.preflight()
            except CallFailed as failure:
                raise Refusal(str(failure)) from None
            for request, repetition in plan:
                # An interrupted attempt counts toward the three.
                left = MAX_ATTEMPTS - recovered.get((self.args.phase, request["id"], repetition), 0)
                if left <= 0:
                    self.redactor.say(f"eval: runs/{self.args.phase}/{request['id']} r{repetition}: "
                                      "no attempt left after interrupted attempts")
                for _ in range(left):
                    if self.attempt(request, repetition)["failure_class"] != "infra":
                        break
        finally:
            os.close(lock)
        return 0

    def first_signin(self):
        for attempt in range(1, SIGNIN_TRIES + 1):
            try:
                self.webui.signin()
                return
            except CallFailed as failure:
                if attempt == SIGNIN_TRIES or not transient(failure.__cause__):
                    raise
            time.sleep(SIGNIN_RETRY_SECONDS)

    def require_space(self):
        """eval_require_space 0 of common.sh, before every attempt."""
        script = 'EVAL_SOURCE=$1 && . "$1/deploy/eval/common.sh" && eval_require_space 0'
        try:
            result = subprocess.run(["bash", "-c", script, "bash", str(SOURCE)], env=self.child_env,
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                    timeout=DOCKER_TIMEOUT, check=False)
        except subprocess.TimeoutExpired:
            raise Refusal("the free-space check timed out") from None
        if result.returncode != 0:
            raise Refusal(result.stderr.strip().removeprefix("eval: ") or "the free-space check failed")

    def preflight(self) -> dict:
        package = Path(self.args.package) / "release.json"
        try:
            raw = package.read_bytes()
            inventory = json.loads(raw)
        except (OSError, ValueError) as error:
            raise Refusal(f"cannot read {package}: {describe(error)}") from None
        sys.path.insert(0, str(SOURCE / "deploy"))
        import release  # deploy/release.py: the RUNTIME_IMAGE_VARS names images.env binds

        images = {}
        for role in self.roles:
            variable = release.RUNTIME_IMAGE_VARS[role]
            reference = os.environ.get(variable, "")
            expected = inventory.get("images", {}).get(role, {}).get("reference")
            if reference != expected:
                raise Refusal(f"images.env {variable}={reference}, but {package} names {expected}")
            code, image_id = self.docker("image", "inspect", "--format", "{{.Id}}", reference)
            if code != 0 or not image_id:
                raise Refusal(f"image {reference} is not on the host")
            images[role] = {"reference": reference, "image_id": image_id}
        for container, role in CONTAINER_ROLES.items():
            code, state = self.docker("container", "inspect", "--format", "{{.State.Running}} {{.Image}}",
                                      container)
            running, _, image_id = state.partition(" ")
            if code != 0 or running != "true":
                raise Refusal(f"{container} is not running")
            if image_id != images[role]["image_id"]:
                raise Refusal(f"{container} runs {image_id}, not {images[role]['reference']} "
                              f"({images[role]['image_id']}) of images.env")
        image_models = self.check_webui()
        code, speech = self.docker("exec", SPEECH_CONTAINER, "printenv", "TTS_MODEL")
        if code != 0 or not speech:
            raise Refusal(f"{SPEECH_CONTAINER} has no TTS_MODEL")
        return {
            "release": {"ocu_source_sha": inventory.get("ocu_source_sha"),
                        "webui_source_sha": inventory.get("webui_source_sha"),
                        "release_json_sha256": hashlib.sha256(raw).hexdigest(),
                        "images": images},
            "webui_image_id": images["open-webui"]["image_id"],
            "models": {"chat": MODEL, **image_models, "speech": speech},
        }

    def check_webui(self) -> dict:
        """The model, functions, image and default-parameter settings a run depends on."""
        models = self.webui.get("/api/models")
        models = models.get("data") if isinstance(models, dict) else None
        model = next((item for item in models or [] if isinstance(item, dict) and item.get("id") == MODEL), None)
        if model is None:
            raise Refusal(f"model {MODEL} is not in /api/models")
        filters = [item.get("id") for item in model.get("filters") or [] if isinstance(item, dict)]
        if VIDEO_FILTER not in filters:
            raise Refusal(f"model {MODEL} does not hold the filter {VIDEO_FILTER}")
        functions = self.webui.get("/api/v1/functions/")
        functions = {item.get("id"): item for item in functions or [] if isinstance(item, dict)}
        for name in REQUIRED_FUNCTIONS:
            if name not in functions:
                raise Refusal(f"function {name} is not installed")
            for flag, word in (("is_active", "active"), ("is_global", "global")):
                if functions[name].get(flag) is not True:
                    raise Refusal(f"function {name} is not {word}")
        config = self.webui.get("/api/v1/images/config")
        config = config if isinstance(config, dict) else {}
        expected = {"ENABLE_IMAGE_GENERATION": True, "ENABLE_IMAGE_EDIT": True,
                    "IMAGE_GENERATION_ENGINE": "openai", "IMAGE_EDIT_ENGINE": "openai",
                    "IMAGES_OPENAI_API_BASE_URL": ADAPTER_URL,
                    "IMAGES_EDIT_OPENAI_API_BASE_URL": ADAPTER_URL}
        for name, value in expected.items():
            if config.get(name) != value:
                raise Refusal(f"image config {name} is not {json.dumps(value)}")
        defaults = self.webui.get("/api/v1/configs/models")
        params = defaults.get("DEFAULT_MODEL_PARAMS") if isinstance(defaults, dict) else None
        params = params if isinstance(params, dict) else {}
        if params.get("function_calling") != "native":
            raise Refusal("DEFAULT_MODEL_PARAMS function_calling is not native")
        if params.get("stream_response") is not True:
            raise Refusal("DEFAULT_MODEL_PARAMS stream_response is not true")
        return {"image": config.get("IMAGE_GENERATION_MODEL"), "image_edit": config.get("IMAGE_EDIT_MODEL")}

    def append_ledger(self, chat_id: str):
        descriptor = os.open(self.root / "chats.txt",
                             os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.write(descriptor, f"{chat_id}\n".encode("ascii"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def ledger(self) -> list[str]:
        try:
            return (self.root / "chats.txt").read_text(encoding="ascii").split()
        except FileNotFoundError:
            return []

    def manifest(self, run_dir: Path, request_id: str, repetition: int, attempt: int, **fields) -> dict:
        request = self.requests.get(request_id, {})
        manifest = {
            "version": 1, "phase": run_dir.parent.parent.name, "request_id": request_id,
            "kind": request.get("kind"), "repetition": repetition, "attempt": attempt,
            "chat_id": None, "release": None, "webui_image_id": None, "models": None,
            "started_at": None, "elapsed_seconds": None, "outcome": "failed",
            "failure_class": "infra", "failure_reason": None, "check": None, "counts": None,
            "mp4": [], "skipped_outputs": [],
        }
        manifest.update(fields)
        if manifest["failure_reason"] is not None:
            manifest["failure_reason"] = self.redactor.text(manifest["failure_reason"])[:REASON_LIMIT]
        self.redactor.write_json(run_dir / "manifest.json", manifest)
        return manifest

    def recover(self) -> dict:
        """Run directories an earlier driver left without a manifest become failed attempts.

        Returns the number recovered per (phase, request id, repetition)."""
        ledger = self.ledger()
        recovered: dict = {}
        for run_dir in sorted(self.root.glob("runs/*/*/r*-a*")):
            match = RUN_DIR.fullmatch(run_dir.name)
            if match is None or not run_dir.is_dir() or (run_dir / "manifest.json").exists():
                continue
            try:
                pending = json.loads((run_dir / "pending.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pending = {}
            chat_id = pending.get("chat_id")
            if chat_id is None and isinstance(pending.get("user_message_id"), str):
                try:
                    since = int(datetime.datetime.strptime(pending.get("started_at"), TIMESTAMP).replace(
                        tzinfo=datetime.timezone.utc).timestamp())
                except (TypeError, ValueError):
                    since = 0
                chat_id = self.webui.find_chat(pending["user_message_id"], since)
            if chat_id is not None and chat_id not in ledger:
                self.append_ledger(chat_id)
                ledger.append(chat_id)
            self.manifest(run_dir, run_dir.parent.name, int(match[1]), int(match[2]), chat_id=chat_id,
                          started_at=pending.get("started_at"), failure_reason="driver interrupted")
            self.redactor.say(f"eval: {run_dir.relative_to(self.root)} failed infra: driver interrupted")
            key = (run_dir.parent.parent.name, run_dir.parent.name, int(match[1]))
            recovered[key] = recovered.get(key, 0) + 1
        return recovered

    def body(self, prompt: str, user_message_id: str, assistant_id: str, now: int) -> dict:
        local = datetime.datetime.fromtimestamp(now, SHANGHAI)
        return {
            "stream": True,
            "model": MODEL,
            "params": {},
            "files": [],
            "filter_ids": [VIDEO_FILTER],
            "tool_ids": [],
            "tool_servers": [],
            "features": {"voice": False, "image_generation": True, "code_interpreter": False,
                         "web_search": False},
            "variables": {
                "{{USER_NAME}}": self.webui.name,
                "{{USER_EMAIL}}": self.webui.email,
                "{{USER_LOCATION}}": "Unknown",
                "{{CURRENT_DATETIME}}": local.strftime("%Y-%m-%d %H:%M:%S"),
                "{{CURRENT_DATE}}": local.strftime("%Y-%m-%d"),
                "{{CURRENT_TIME}}": local.strftime("%H:%M:%S"),
                "{{CURRENT_WEEKDAY}}": WEEKDAYS[local.weekday()],
                "{{CURRENT_TIMEZONE}}": "Asia/Shanghai",
                "{{USER_LANGUAGE}}": "zh-CN",
            },
            "chat_variables": {},
            "session_id": f"eval-{uuid.uuid4()}",
            "id": assistant_id,
            "message_ids": [{"model_id": MODEL, "message_id": assistant_id, "modelIdx": 0}],
            "parent_id": None,
            "user_message": {"id": user_message_id, "parentId": None, "childrenIds": [assistant_id],
                             "role": "user", "content": prompt, "timestamp": now, "models": [MODEL]},
            "background_tasks": {"title_generation": True, "tags_generation": True,
                                 "follow_up_generation": True},
        }

    def wait(self, chat_id: str, started: float):
        """Poll the task list until it empties; at the time limit stop the chat and set self.stopped."""
        path = f"/api/tasks/chat/{chat_id}"

        def busy() -> bool:
            payload = self.webui.get(path)
            task_ids = payload.get("task_ids") if isinstance(payload, dict) else None
            if not isinstance(task_ids, list):
                raise CallFailed(f"GET {path}: no task list")
            return bool(task_ids)

        while busy():
            left = started + TIME_LIMIT_SECONDS - time.monotonic()
            if left <= 0:
                self.stopped = True
                self.webui.call("POST", f"{path}/stop")
                deadline = time.monotonic() + STOP_WAIT_SECONDS
                while time.monotonic() < deadline and busy():
                    time.sleep(min(POLL_SECONDS, max(deadline - time.monotonic(), 0)))
                return
            time.sleep(min(POLL_SECONDS, left))

    def collect(self, chat_id: str, run_dir: Path) -> tuple[list, list]:
        """Copy the regular .mp4 files directly in the chat's outputs, then probe each copy."""
        try:
            directory = os.open(self.root / "data" / chat_id / "outputs",
                                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return [], []
        except OSError as error:
            return [], [{"name": "outputs", "reason": f"unreadable: {error.strerror}"}]
        copied, skipped = [], []
        try:
            for name in sorted(os.listdir(directory)):
                if not name.endswith(".mp4"):
                    continue
                # The sandbox picks the bytes of a name; the manifest holds UTF-8 only.
                shown = os.fsencode(name).decode("utf-8", "backslashreplace")
                if len(copied) + len(skipped) >= MAX_MP4_FILES:
                    skipped.append({"name": shown, "reason": "too many"})
                    continue
                if shown != name:
                    skipped.append({"name": shown, "reason": "name is not UTF-8"})
                    continue
                try:
                    mode = os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode
                    if stat.S_ISLNK(mode):
                        skipped.append({"name": name, "reason": "symlink"})
                    elif not stat.S_ISREG(mode):
                        skipped.append({"name": name, "reason": "not a regular file"})
                    else:
                        copied.append(self.copy(directory, name, run_dir / name))
                except TooLarge:
                    skipped.append({"name": name, "reason": "too large"})
                except OSError as error:
                    skipped.append({"name": name, "reason": f"unreadable: {error.strerror or error}"})
        finally:
            os.close(directory)
        return [self.probe(run_dir, entry) for entry in copied], skipped

    @staticmethod
    def copy(directory: int, name: str, target: Path) -> dict:
        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            info = os.fstat(source)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("not a regular file")
            space = os.statvfs(target.parent)
            if info.st_size > min(MAX_MP4_BYTES, space.f_bavail * space.f_frsize - FLOOR_BYTES):
                raise TooLarge
            digest, size = hashlib.sha256(), 0
            output = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                while chunk := os.read(source, 1 << 20):
                    size += len(chunk)
                    if size > info.st_size:
                        raise OSError("the file grew while it was copied")
                    digest.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(output, view):]
            except BaseException:
                os.close(output)
                os.unlink(target)
                raise
            os.close(output)
        finally:
            os.close(source)
        return {"name": name, "size": size, "sha256": digest.hexdigest()}

    def probe(self, run_dir: Path, entry: dict) -> dict:
        """ffprobe from the workspace image: the host has no ffmpeg."""
        code, output = self.docker(
            "run", "--rm", "--network", "none", "--entrypoint", "ffprobe", "-v", f"{run_dir}:/run:ro",
            os.environ["DOCKER_IMAGE"], "-v", "error", "-show_entries",
            "format=duration:stream=codec_type,codec_name", "-of", "json", f"/run/{entry['name']}")
        try:
            payload = json.loads(output) if code == 0 else {}
        except ValueError:
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        try:
            duration = float(payload["format"]["duration"])
        except (KeyError, TypeError, ValueError):
            duration = None
        codec = next((stream.get("codec_name") for stream in payload.get("streams") or []
                      if isinstance(stream, dict) and stream.get("codec_type") == "video"), None)
        return {**entry, "duration": duration, "video_codec": codec,
                "valid": codec is not None and duration is not None and duration > 0}

    def abandon(self, chat_id: str):
        """Best effort: a chat the driver stops following must not keep running."""
        try:
            self.webui.call("POST", f"/api/tasks/chat/{chat_id}/stop")
        except CallFailed as failure:
            self.redactor.say(f"eval: could not stop chat {chat_id}: {failure}", sys.stderr)

    def attempt(self, request: dict, repetition: int) -> dict:
        self.require_space()
        base = self.root / "runs" / self.args.phase / request["id"]
        base.mkdir(parents=True, exist_ok=True)
        taken = [int(match[2]) for match in map(RUN_DIR.fullmatch, os.listdir(base))
                 if match and int(match[1]) == repetition]
        number = max(taken, default=0) + 1
        run_dir = base / f"r{repetition}-a{number}"
        run_dir.mkdir(mode=0o700)
        now = int(time.time())
        user_message_id, assistant_id = str(uuid.uuid4()), str(uuid.uuid4())
        pending = {"user_message_id": user_message_id, "started_at": utc(now)}
        self.redactor.write_json(run_dir / "pending.json", pending)
        body = self.body(request["prompt"], user_message_id, assistant_id, now)
        started = time.monotonic()
        chat_id, message, failure = None, None, None
        self.stopped = False
        try:
            chat_id = self.webui.submit(body, user_message_id, now)
            self.append_ledger(chat_id)
            pending["chat_id"] = chat_id
            self.redactor.write_json(run_dir / "pending.json", pending)
            self.wait(chat_id, started)
            chat = self.webui.get(f"/api/v1/chats/{chat_id}")
            if chat is None:
                raise CallFailed("chat missing")
            self.redactor.write_json(run_dir / "transcript.json", shrink(chat))
            message = history(chat).get(assistant_id)
            message = message if isinstance(message, dict) else {}
        except CallFailed as error:
            failure = str(error)
            if chat_id is not None and not self.stopped:
                self.abandon(chat_id)
        try:
            mp4, skipped = self.collect(chat_id, run_dir) if chat_id else ([], [])
        except Exception as error:  # noqa: BLE001 - sandbox-made outputs must not end the batch
            mp4, skipped = [], [{"name": "outputs", "reason": f"collect failed: {type(error).__name__}"}]
        outcome, failure_class, reason = classify(self.stopped, message, failure,
                                                  any(entry["valid"] for entry in mp4))
        manifest = self.manifest(
            run_dir, request["id"], repetition, number, chat_id=chat_id, started_at=pending["started_at"],
            elapsed_seconds=round(time.monotonic() - started, 3), outcome=outcome,
            failure_class=failure_class, failure_reason=reason, counts=counts(message), mp4=mp4,
            skipped_outputs=skipped, **self.context)
        line = f"eval: {run_dir.relative_to(self.root)} {outcome}"
        if failure_class:
            line += f" {failure_class}: " + re.sub(r"[\r\n]+", " ", manifest["failure_reason"])
        self.redactor.say(line)
        return manifest


def arguments(argv: list[str]):
    parser = argparse.ArgumentParser(prog="run.sh", description="Run evaluation requests.")
    parser.add_argument("--package", required=True, help="release package directory (release.json)")
    parser.add_argument("--phase", required=True, choices=("baseline", "after", "smoke"))
    parser.add_argument("--request", help="one request id of requests.json; default: all")
    parser.add_argument("--repetition", type=int, help="repetition of --request; default 1")
    return parser.parse_args(argv)


def main(argv: list[str], key_stream) -> int:
    args = arguments(argv)
    redactor = Redactor()
    try:
        names = os.environ.get("EVAL_STACK_NAMES", "").split()
        missing = [name for name in ("EVAL_ROOT", "EVAL_WEBUI_PORT", "EVAL_ROLES", "DOCKER_IMAGE", *names)
                   if not os.environ.get(name)]
        if not names or missing:
            raise Refusal(f"run the driver through run.sh; missing {' '.join(missing) or 'EVAL_STACK_NAMES'}")
        stack = {name: os.environ[name] for name in names}
        for value in stack.values():
            redactor.add(value)
        key = key_stream.read().rstrip("\n")
        redactor.add(key)
        if not key or any(character.isspace() for character in key):
            raise Refusal("the provider key on stdin must be one key on one line")
        return Driver(args, redactor, stack).run()
    except Refusal as refusal:
        redactor.say(f"eval: refused: {refusal}", sys.stderr)
    except Exception as error:  # noqa: BLE001 - a traceback could print a secret value; this one is redacted
        redactor.say(f"eval: driver failed: {type(error).__name__}: {error}", sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], sys.stdin))
