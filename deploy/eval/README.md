<!-- SPDX-License-Identifier: FSL-1.1-Apache-2.0 -->

# Isolated evaluation stack (`ocu-eval`)

This directory runs Open WebUI and Computer Use on a shared Docker host as one compose project, `ocu-eval`. It uses release images from a package, unmodified. It is evaluation tooling and is not part of any release.

## What the stack is

- `compose.eval.yml` overrides the upstream `docker-compose.yml` and `docker-compose.webui.yml`.
- It starts four services: `computer-use-server`, `open-webui`, `postgres`, and the one-shot `open-webui-init`. Every container is named `ocu-eval-<service>`.
- `workspace` and `cleanup` are disabled with `profiles: ["never"]`. The upstream `cleanup` job would remove every orchestrator container and `chat-*-workspace` volume on the host, including those of other projects.
- Only `open-webui` publishes a port, on `127.0.0.1:${EVAL_WEBUI_PORT}`. Reach it through an SSH tunnel.
- Sandboxes run with `ENABLE_NETWORK=false`. They publish no port and reach no address.
- The orchestrator's data directory is `${EVAL_ROOT}/data`, bound at the same path.
- DocumentServer, retention-guard and the proxy are not run. The host firewall, host networks and the Docker daemon configuration are not changed.

## Files under `EVAL_ROOT`

| Path | Written by | Contents |
| --- | --- | --- |
| `${EVAL_ROOT}/src` | operator | OCU checkout at the commit under evaluation |
| `${EVAL_ROOT}/stack.env` | `secrets.sh init` | stack secrets, mode 0600, written once |
| `${EVAL_KEY_FILE}` | operator | provider key file, mode 0600; default `${EVAL_ROOT}/dmxapi.key` |
| `${EVAL_ROOT}/images.env` | `load.sh` | image references bound to the `RUNTIME_IMAGE_VARS` names |
| `${EVAL_ROOT}/loaded-images.txt` | `load.sh` | references that were absent before a load, for cleanup |
| `${EVAL_ROOT}/data` | `start.sh` | orchestrator data: per-chat outputs |

`stack.env`, the provider key file and `images.env` are refused unless each is a regular file, not a symlink, owned by the caller, with mode 0600. The scripts export their values into the compose environment only. They never print a value.

`stack.env` holds `OCU_INTERNAL_TOKEN`, `MCP_API_KEY`, `WEBUI_SECRET_KEY`, `POSTGRES_PASSWORD`, `ADMIN_EMAIL` and `ADMIN_PASSWORD`. The secrets use `[A-Za-z0-9]` only, and the admin email is `eval-admin@ocu-eval.local`. `secrets.sh init` never overwrites the file, so `MCP_API_KEY` stays stable across restarts. Upstream defaults (`admin`, `openwebui`, an empty token) are never used.

## Free-space floor

`load.sh` and `start.sh` refuse to proceed when Docker's root filesystem has less than 15 GiB (16106127360 bytes) free. `load.sh` checks before every `docker load`. The root is `DockerRootDir` from the local daemon, found the same way as `deploy/release.py` finds it, and `df -Pk` measures it. The refusal states the free space and the floor.

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
   git clone <ocu-repository> "$EVAL_ROOT/src"
   git -C "$EVAL_ROOT/src" checkout <commit>
   ```

2. Store the provider key without echoing it: paste the key at the silent prompt and press Enter. `printf` is a shell builtin, so the key never appears on a command line.

   ```bash
   IFS= read -rs key && (umask 077 && printf '%s\n' "$key" >"$EVAL_ROOT/dmxapi.key"); unset key
   ```

   To keep it elsewhere, set `EVAL_KEY_FILE=<path>` for `start.sh`.

3. Generate the stack secrets once.

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
