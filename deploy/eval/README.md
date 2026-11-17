<!-- SPDX-License-Identifier: FSL-1.1-Apache-2.0 -->

# Isolated evaluation stack (`ocu-eval`)

This directory runs Open WebUI and Computer Use on a shared Docker host as one compose project, `ocu-eval`. It uses release images from a package, unmodified. It is evaluation tooling and is not part of any release.

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

## Host rules

- Never run `docker system prune`, `docker image prune`, `docker volume prune`, `docker compose down -v` or `--rmi`, and never remove an object outside project `ocu-eval`. Cleanup removes only the references in `loaded-images.txt` and the chats recorded by the evaluation.
- Never change the host firewall, host networks or `/etc/docker/daemon.json`.
- Never print `stack.env` or the provider key file, and never pass a value on a command line.
- Record the container, image, network and volume lists of other projects before and after a start and stop, and compare them.
