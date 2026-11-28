<!-- SPDX-License-Identifier: FSL-1.1-Apache-2.0 -->

# Isolated evaluation stack (`ocu-eval`)

This directory runs Open WebUI and Computer Use on a shared Docker host as one compose project, `ocu-eval`. It uses release images from a package, unmodified. It is evaluation tooling and is not part of any release.

`requests.json` is the evaluation's request set. It is fixed before the baseline run, and changing it after the baseline voids the comparison.

## What the stack is

- `compose.eval.yml` overrides the upstream `docker-compose.yml` and `docker-compose.webui.yml`.
- It starts five services: `computer-use-server`, `open-webui`, `postgres`, `adapter`, and the one-shot `open-webui-init`. Every container is named `ocu-eval-<service>`.
- `workspace` and `cleanup` are disabled with `profiles: ["never"]`. The upstream `cleanup` job would remove every orchestrator container and `chat-*-workspace` volume on the host, including those of other projects.
- Only `open-webui` publishes a port, on `127.0.0.1:${EVAL_WEBUI_PORT}`. Reach it through an SSH tunnel.
- Sandboxes run with `ENABLE_NETWORK=false`. They publish no port and reach no address.
- The orchestrator's data directory is `${EVAL_ROOT}/data`, bound at the same path.
- DocumentServer, retention-guard and the proxy are not run. The host firewall, host networks and the Docker daemon configuration are not changed.
- Without the proxy there is no `/ocu` path, so the workspace sidebar does not work in a browser. WebUI reaches the orchestrator directly at `http://computer-use-server:8081`, and the evaluation driver goes through WebUI's API, so neither needs it.
- WebUI creates the admin from `stack.env` during startup, before it serves requests, and disables signup. `open-webui-init` then signs in as that admin, so no client can sign up as admin first.
- The `adapter` service is the evaluation's speech adapter, described below. It publishes no port.
- Sandboxes stop after three days idle (`CONTAINER_IDLE_TIMEOUT=259200`, as in production). The upstream 600-second default would stop the sandbox of a run that makes no sandbox tool call for ten minutes, in the middle of a render.

## Speech adapter

`adapter/eval_adapter.py` is an evaluation-only service between OCU's speech client (`computer-use-server/speech.py`) and dmxapi. It is not part of any release.

- `POST /v1/audio/speech` takes OCU's request: non-empty `model` and `input`, `response_format` `mp3` or `wav`, and optional `voice` and `instructions`. It calls dmxapi `chat/completions` and returns the audio with `Content-Type: audio/mpeg` or `audio/wav`.
- Callers send `Authorization: Bearer <ADAPTER_TOKEN>`. Without it the adapter answers 401; with any other field or value it answers 400. Neither calls dmxapi.
- Every upstream failure is a 502 with a JSON `{"error": …}` body: an error status (with dmxapi's reason, such as the list of available voices), a redirect, which is never followed, a timeout after 90 seconds, a transport error, or a response that holds no usable audio. The body fits inside the 200-character excerpt OCU passes to the model.
- The provider key is held only by `open-webui` and `adapter`. `computer-use-server` reaches the adapter at `http://adapter:8090/v1` with the adapter token as its `TTS_API_KEY`, and model `mimo-v2.5-tts`. Responses and log lines never hold the key or the token. A log line names the route, the status, the elapsed time and the upstream status only.
- The service runs the `computer-use-server` image unmodified as uid 65534, with `deploy/eval/adapter` bound read-only at `/eval-adapter`. Nothing is built. `GET /healthz` answers `{"ok": true}` without a token.

The same service serves WebUI's `openai` image engine. `POST /v1/images/generations` takes WebUI's JSON request, and `POST /v1/images/edits` its multipart edit with files under `image` or `image[]`. Both call dmxapi `/v1/responses`, the only route that serves `doubao-seedream-5-0-flash-260915`. The adapter sends `prompt` as `input`, edit files as `data:` URLs in `image`, no `n`, and `watermark` false. `n` other than 1 is a 400. Both routes check the token before reading the body. A JSON body over 1 MiB or an edit over 64 MiB is a 413, and every upstream failure is a 502, as for speech. The response is always `data[].b64_json`, even when the caller asks for `url`, and an upstream `url` item is a 502. The reason is that WebUI fetches a returned URL with its image `Authorization` header, which holds the adapter token, so a URL would carry the token to the image host. The service has `mem_limit: 1g`, because one 64 MiB edit holds a few copies of its images.

Because the adapter runs as uid 65534, every file under `deploy/eval/adapter` must be readable by others, and every directory searchable. Clone `src` under umask 022, as in step 1. `start.sh` refuses to start otherwise and names the path.

## Files

`EVAL_ROOT` must be a directory owned by the caller that is not group- or other-writable; the scripts refuse any other.

| Path | Written by | Contents |
| --- | --- | --- |
| `${EVAL_ROOT}/src` | operator | OCU checkout at the commit under evaluation |
| `${EVAL_ROOT}/stack.env` | `secrets.sh init` | stack secrets, mode 0600, written once |
| `${EVAL_KEY_FILE}` | operator | provider key file, mode 0600; default `$HOME/.ocu-eval-dmxapi.key` |
| `${EVAL_ROOT}/images.env` | `load.sh` | image references bound to the `RUNTIME_IMAGE_VARS` names |
| `${EVAL_ROOT}/loaded-images.txt` | `load.sh` | references that were absent before a load, for cleanup |
| `${EVAL_ROOT}/data` | `start.sh` | orchestrator data: per-chat outputs |
| `${EVAL_ROOT}/runs` | `run.sh` | one directory per attempt: transcript, MP4 files, manifest |
| `${EVAL_ROOT}/chats.txt` | `run.sh`, operator | chat ledger: every chat id the driver created, for cleanup |
| `${EVAL_ROOT}/eval.lock` | `run.sh`, `cleanup.sh` | lock held by a running driver |

The provider key file stays outside `EVAL_ROOT`: the evaluation's key-leak check searches every file under `EVAL_ROOT`, so a key stored there would always fail it.

`stack.env`, the provider key file and `images.env` are refused unless each is a regular file, not a symlink, owned by the caller, with mode 0600. The scripts export their values into the compose environment only. They never print a value.

`stack.env` holds `OCU_INTERNAL_TOKEN`, `MCP_API_KEY`, `WEBUI_SECRET_KEY`, `POSTGRES_PASSWORD`, `ADMIN_EMAIL`, `ADMIN_PASSWORD` and `ADAPTER_TOKEN`. The secrets use `[A-Za-z0-9]` only, and the admin email is `eval-admin@ocu-eval.local`. Upstream defaults (`admin`, `openwebui`, an empty token) are never used.

`secrets.sh init` never changes an existing value, so `MCP_API_KEY` stays stable across restarts:

- Without `stack.env`, it writes every name.
- With an existing `stack.env`, it first applies the same checks as `start.sh`: a regular file owned by the caller, mode 0600, and only known `NAME=value` lines with well-formed values. It refuses any other file without printing a value.
- If names are missing, for example `ADAPTER_TOKEN` in a file written before the speech adapter existed, it appends one line for each. The existing lines stay byte-identical. The new file replaces the old one in a single rename.
- If no name is missing, it prints `nothing to add` and changes nothing.

Adding `ADAPTER_TOKEN` is one-way. The scripts of a `src` checkout older than the speech adapter refuse the new name, so once `init` has appended it, that older checkout cannot run `start.sh` or `stop.sh` on the same `EVAL_ROOT`.

## Free-space floor

`load.sh` and `start.sh` refuse to proceed when Docker's root filesystem has less than 15 GiB (16106127360 bytes) free. Before every `docker load`, `load.sh` also requires room for that archive unpacked above the floor: the floor plus `IMPORT_UNPACK_FACTOR` (read from `deploy/release.py`) times the archive size. The root is `DockerRootDir` from the local daemon, found the same way as `deploy/release.py` finds it, and `df -Pk` measures it. The refusal states the free space, the archive's required bytes and the floor.

To see the refusal, put a `df` that reports 1 GiB free first on `PATH`. Both scripts then exit before any `docker load` or `compose up`.

```bash
fake=$(mktemp -d)
printf '%s\n' '#!/bin/sh' 'echo "Filesystem 1024-blocks Used Available Capacity Mounted on"' \
    'echo "fake 2097152 1048576 1048576 50% /"' >"$fake/df"
chmod 700 "$fake/df"
PATH="$fake:$PATH" "$EVAL_ROOT/src/deploy/eval/load.sh" "$EVAL_ROOT/packages/<release>"
PATH="$fake:$PATH" EVAL_WEBUI_PORT=<port> "$EVAL_ROOT/src/deploy/eval/start.sh"
rm -rf "$fake"
```

## Operator steps

Commands run on the evaluation host as the evaluation user. Placeholders are in angle brackets. The host needs `bash`, `python3` 3.9 or newer (`deploy/release.py` is imported), `df` and Docker with Compose v2.

1. Prepare the root and the checkout.

   ```bash
   export EVAL_ROOT=/home/<user>/ocu-eval
   mkdir -p "$EVAL_ROOT" && chmod 700 "$EVAL_ROOT"
   (umask 022 && git clone <ocu-repository> "$EVAL_ROOT/src" && git -C "$EVAL_ROOT/src" checkout <commit>)
   ```

   The umask keeps `deploy/eval/adapter` readable by the adapter's uid 65534. `EVAL_ROOT` stays mode 0700, and the adapter reads its bind through the Docker daemon, not through that directory.

2. Store the provider key without echoing it: paste the key at the silent prompt and press Enter. `printf` is a shell builtin, so the key never appears on a command line.

   ```bash
   IFS= read -rs key && (umask 077 && printf '%s\n' "$key" >"$HOME/.ocu-eval-dmxapi.key"); unset key
   ```

   To keep it elsewhere, set `EVAL_KEY_FILE=<path>` for `start.sh`.

3. Generate the stack secrets. On an existing `stack.env`, this adds only the missing names.

   ```bash
   "$EVAL_ROOT/src/deploy/eval/secrets.sh" init
   ```

4. Load the images from the release package.

   ```bash
   "$EVAL_ROOT/src/deploy/eval/load.sh" "$EVAL_ROOT/packages/<release>"
   ```

5. Start the stack on a free loopback port.

   ```bash
   EVAL_WEBUI_PORT=<port> "$EVAL_ROOT/src/deploy/eval/start.sh"
   ```

   After `up -d`, `start.sh` reads the exit status of the `ocu-eval-open-webui-init` container with `docker wait`, which works whether the initializer is still running or has already exited. It exits non-zero unless that status is 0, naming the service and its status. A missing container or an unreadable status is also a failure. The stack stays up for inspection either way.

   From the workstation: `ssh -N -L <local-port>:127.0.0.1:<port> <user>@<eval-host>`.

6. Stop the stack. Named volumes, loaded images and sandboxes stay.

   ```bash
   EVAL_WEBUI_PORT=<port> "$EVAL_ROOT/src/deploy/eval/stop.sh"
   ```

## Running requests

`run.sh` sends evaluation requests through WebUI's chat API on the running stack and records each attempt. It runs on the evaluation host, so the admin credentials never leave it. A run can take an hour, so start it detached and follow its log:

```bash
mkdir -p "$EVAL_ROOT/snap/<label>"
EVAL_WEBUI_PORT=<port> setsid nohup "$EVAL_ROOT/src/deploy/eval/run.sh" \
    --package "$EVAL_ROOT/packages/<release>" --phase smoke --request <request id> \
    >"$EVAL_ROOT/snap/<label>/run.log" 2>&1 &
```

- `--phase` is one of five phases. `baseline`, `after` and `smoke` are the explainer phases. Use `smoke` for any explainer run that is neither the baseline nor the after run. `report-before` and `report-after` are the report phases.
- A request entry may carry `set`, either `report` or `report-smoke`. Explainer phases own the entries without `set`, and report phases own the entries with `set`. The driver refuses to load `requests.json` when an entry has any other `set` value.
- Without `--request`, an explainer phase runs every entry without `set`, and a report phase runs every `set: "report"` entry. It runs them in file order, each `repetitions` times. A `report-smoke` entry runs only when named. When that selection is empty, the driver refuses.
- With `--request` and no `--repetition`, it runs repetition 1. A named entry with `set` is refused in an explainer phase. A named entry without `set` is refused in a report phase.
- These request refusals come before the lock, the sign-in and any chat.
- It prints one line per attempt and always ends with `eval: done <exit status>`. It exits 0 when every attempt ended with a manifest, whatever the outcome.
- It reads `stack.env`, `images.env` and the provider key file as `start.sh` does. The key reaches `driver.py` on stdin, never on a command line or in an environment. The driver runs with the host's `python3` (3.10 or newer, standard library only).

Before the first chat, the driver checks the stack and refuses with a reason:

- the running `ocu-eval-*` containers run the images of `images.env`, and each `images.env` reference is the one in the package's `release.json`;
- `qwen3.8-27b` holds the `video_production_filter` filter;
- `computer_use_filter` and `video_production_filter` are active and global;
- image generation and editing are on, both with engine `openai` and base URL `http://adapter:8090/v1`;
- `DEFAULT_MODEL_PARAMS` has `function_calling` `native` and `stream_response` true.

Before every attempt it applies the 15 GiB free-space floor. A refusal stops the batch with a non-zero exit.

Each attempt creates one chat with the video switch and image generation on, waits up to 60 minutes, then stops the chat. Every value of `stack.env`, the provider key and the session token are replaced by `[REDACTED]` in every file and output line.

### Run directory and manifest

Each attempt has its own directory, `${EVAL_ROOT}/runs/<phase>/<request id>/r<repetition>-a<attempt>/`, with the next free attempt number:

- `pending.json`: the user message id and start time, written before the request is sent, then the chat id.
- `transcript.json`: the chat as WebUI returns it. Strings over 4096 characters that start with `data:` are replaced by `<data-uri sha256=… bytes=…>`.
- The MP4 files: only regular files directly in the chat's `outputs/` directory, among the first 20 entries ending in `.mp4`. These are not copied and are listed in `skipped_outputs` with the reason: symlinks and other entries; entries that vanish or cannot be read; names that are not UTF-8; files over 4 GiB (`too large`); and files the run directory's filesystem cannot hold (`no space`). Entries past the twentieth get one summary entry. A failed copy leaves no partial file. A failure while collecting never stops the batch, and the MP4 files already recorded stay.
- `manifest.json` (version 1), written last. Its fields: `phase`, `request_id`, `kind`, `repetition`, `attempt`, `chat_id`, and `release` (`ocu_source_sha`, `webui_source_sha`, `release_json_sha256`, and per role `reference` and `image_id`). Then `webui_image_id`, `models` (`chat`, `image`, `image_edit`, `speech`), `started_at` (UTC), `elapsed_seconds`, `outcome` (`succeeded` or `failed`), `failure_class` (`null`, `infra`, `kit` or `model`), `failure_reason` (at most 200 characters), and `check` (`passed`, `failed`, `none` or `null`, defined below the outcome table). Last come `counts` (`tool_calls`, `image_calls`, `speech_calls`), `mp4` (per file: `name`, `size`, `sha256`, `duration`, `video_codec`, `valid`) and `skipped_outputs`.

An MP4 is valid when `ffprobe` from the workspace image, run offline, reports a video stream and a duration above 0.

A directory without `manifest.json` was left by a driver that died. The next driver run, of any phase, marks it `failed`, `infra`, `driver interrupted` in place, with the phase of its own path. This is the only write a run makes under `runs/` outside its own phase directory, and it creates no attempt. It records the chat id from `pending.json`, or finds the chat by its user message id and adds it to the ledger. That manifest has `null` release, models, counts and elapsed time. It counts as an `infra` attempt.

### Outcomes and reruns

The first matching row wins:

| # | Condition | Class | Reason |
| --- | --- | --- | --- |
| 1 | not stopped, message `done` with no error, at least one valid MP4 | — (`succeeded`) | — |
| 2 | the driver stopped the chat at 60 minutes | `model` | `time limit` |
| 3 | an `ask_user` call is `pending` | `model` | `asked the user` |
| 4 | the message error starts `Tool-call limit reached` | `model` | `tool-call limit` |
| 5 | any other message error | `infra` | `model API: <error>` |
| 6 | the task list emptied and the message is not `done` | `infra` | `task lost` |
| 7 | a WebUI call failed after three retries, the chat is missing, or the POST was lost | `infra` | the call |
| 7a | no valid MP4, and an output could not be copied for lack of space, or collecting failed | `infra` | `disk: no space for <name>` or `driver: collect failed: <class>` |
| 8 | no valid MP4, and a speech or image result shows an adapter failure, or a tool result starts with a tool-wrapper transport prefix | `infra` | `adapter: <tool>` or `orchestrator: <tool>` |
| 8a | no valid MP4, and the last kit status line is a kit failure line | `kit` | that line |
| 9 | anything else | `model` | `no valid MP4` |

The kit status lines are the lines starting `kit status: ` in the tool results of the final message, in call order, as `remotion-template/bin/status.mjs` prints them. A kit failure line starts with `kit status: check failed`, `kit status: check died`, `kit status: render failed` or `kit status: render died`. Row 8a looks only at the last kit status line: when that line is a pass, a `render succeeded` line or any other status, or when there is none, row 9 applies. Its reason is redacted and cut to 200 characters like every reason.

`check` is `passed` when any kit status line is exactly `kit status: check passed`, else `failed` when one starts with `kit status: check failed` or `kit status: check died`, else `none`. It is `null` when the final message was not read: an interrupted attempt, an attempt without a chat, or one whose transcript could not be read.

A failed run keeps its MP4 files. An `infra` outcome is rerun at most twice, each attempt in its own directory with its own manifest. The budget is read from disk, across batches. A request and repetition gets three attempts minus the `infra` attempts, interrupted ones included, that end its list of attempts. When none is left, it is skipped. One whose latest attempt is not `infra` starts fresh.

The driver never sends the same request twice. When the answer to a sent request is lost, it finds the chat through the chat list. If the lookup finds none, the attempt is `infra` with reason `post lost`, or `POST /api/chat/completions: HTTP <status>` when WebUI answered with an error. If the lookup itself fails, the driver writes no manifest and stops the batch with a non-zero exit. The chat may exist outside the ledger, so the next run's recovery repeats the lookup from `pending.json`. The driver repeats the request only when the connection was refused before anything was sent. When it stops following a chat after a failed WebUI call, it asks WebUI once to stop that chat's task. The first sign-in of a batch is retried up to five times, 3 seconds apart, while WebUI refuses or resets the connection.

## Cleanup

`cleanup.sh` removes the evaluation's Docker objects by exact name. It exits 0 only when every target is gone. Running it again after a partial failure finishes the rest.

```bash
EVAL_WEBUI_PORT=<port> "$EVAL_ROOT/src/deploy/eval/cleanup.sh"   # everything
"$EVAL_ROOT/src/deploy/eval/cleanup.sh" --runs-only                # only the ledger's chats
```

- **Both modes**, for each chat id in `chats.txt`: the sandbox container `owui-chat-<id>`, the volume `chat-<id>-workspace`, and `${EVAL_ROOT}/data/<id>`. The directory is root-owned, so a `--network none` helper container of the workspace image removes it. `chats.txt` is rewritten to keep only the ids whose objects could not all be removed.
- **Full mode** also runs, in this order:
  1. `stop.sh`'s compose down, before the per-chat steps.
  2. `docker volume rm` of the volumes labelled `com.docker.compose.project=ocu-eval` whose names start `ocu-eval_`. WebUI's volume holds the image settings, including the adapter token, so the image configuration of task 2.4 must be written again after a reload.
  3. `docker image rm`, without `-f`, of each reference in `loaded-images.txt` and each image labelled `ocu-eval`.

  If a per-chat step or compose down failed, no image is removed, so the helper image stays for the next run. `images.env` and `loaded-images.txt` are removed only when every image is gone. After that, a further full run skips compose down: it exits 0 when no container or network labelled with the project remains, and non-zero, naming them, otherwise.
- Cleanup never prunes, never uses `docker image rm -f`, and never removes a name it did not compute. `runs/`, `stack.env`, `src/` and `packages/` stay.

The driver holds an exclusive lock on `${EVAL_ROOT}/eval.lock` for its whole batch. `cleanup.sh` takes it with `flock -n` and refuses while a driver runs. A second driver refuses too.

To have cleanup remove a chat made outside the driver, for example a manual probe, append its id to the ledger. The id must be a lowercase UUID on its own line:

```bash
printf '%s\n' <chat id> >>"$EVAL_ROOT/chats.txt"
```

## Host rules

- Never run `docker system prune`, `docker image prune`, `docker volume prune`, `docker compose down -v` or `--rmi`, and never remove an object outside project `ocu-eval`. `cleanup.sh` removes only the project's compose objects, the images in `loaded-images.txt` or labelled `ocu-eval`, and the chats in `chats.txt`.
- Never change the host firewall, host networks or `/etc/docker/daemon.json`.
- Never print `stack.env` or the provider key file, and never pass a value on a command line.
- Record the container, image, network and volume lists of other projects before and after a start and stop, and compare them.
