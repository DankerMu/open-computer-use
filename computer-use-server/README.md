# Computer Use Server

MCP orchestrator that manages isolated Docker sandbox containers. Provides tools for executing commands, editing files, browsing the web, and delegating tasks to Claude Code sub-agents.

## Architecture

See [docs/architecture.svg](../docs/architecture.svg) for the full diagram.

**Flow:** Client → MCP over HTTP → Computer Use Server (:8081) → Docker Socket → Sandbox Container (one per chat)

## Modules

| Module | Purpose |
|--------|---------|
| `app.py` | FastAPI application: guarded MCP endpoint, file serving, browser/terminal proxy, system prompt API |
| `auth_guard.py` | Fail-closed startup validation, service auth, peer denial and CORS policy |
| `mcp_tools.py` | MCP tool definitions: `bash_tool`, `view`, `create_file`, `str_replace`, `sub_agent` |
| `docker_manager.py` | Container lifecycle: create, stop, cleanup, health checks, volume mounts |
| `outputs_broker.py` | Persisted, bounded output identities and per-chat reconciliation revisions; `GET /api/outputs/{chat_id}` and `GET /internal/describe/{chat_id}` consume that authority |
| `skill_manager.py` | Skill registry: fetch user skills, cache ZIPs, generate system prompt XML |
| `system_prompt.py` | System prompt templates with skill injection |
| `context_vars.py` | Per-request context (chat_id, user_email, etc.) via ContextVar |
| `docs_html.py` | HTML documentation page generator |

### Output identity broker

`outputs_broker.py` keeps a per-chat UUID/revision index under
`BASE_DATA_DIR/{chat_id}/.ocu/index.json`, serialised with the lifecycle lock.
It hashes first observations and detected size changes, but does not hash
unchanged files. `GET /api/outputs/{chat_id}` reconciles that index off-thread
and returns bounded pages with prefixed, percent-encoded cookie-path URLs.
`GET /internal/describe/{chat_id}` reports the persisted counter without a
scan or index create. Broker defaults remain 100 items per page (maximum
1,000), 10,000 active files, 100 MiB per file, and a 64 MiB index.

Polling cannot detect a same-size in-place edit or a delete/recreate completed
between reconciliations. A stale cached hash after the former can also prevent
continuity from being recognised on a later rename. A missing outputs root is
empty only on first use or with no active entries; if active identities already
exist, reconciliation fails retryably and preserves the index. Live names that
cannot be persisted as relative POSIX paths fail explicitly rather than being
rewritten as a corrupt index.

## API Endpoints

### MCP
- `POST /mcp` — MCP Streamable HTTP endpoint (main interface)

### Files
- `GET /files/{chat_id}/{filename}` — Serves output files. Non-download HTML,
  SVG, XHTML, and XML responses mirror generated-content isolation with
  `Content-Security-Policy: sandbox allow-scripts allow-forms` and
  `X-Content-Type-Options: nosniff`; disposition is unchanged (#62 tracks its
  follow-up). The SPA HTML renderer uses the same sandbox tokens on `srcdoc`
  and `src` iframes and does not add `allow-same-origin`.
- `GET /files/{chat_id}/archive` — Download all outputs as ZIP
- `GET /api/outputs/{chat_id}` — Authenticated broker listing: `chat_id`, `files`, `total`, `timestamp`, `revision`, `next_cursor`. Query `cursor` and `limit` (1..1000, default 100). Malformed, out-of-range, or unparseable cursors (including oversized digit runs) return 400; stale cursors return 409. `If-None-Match` uses a weak ETag over the page representation excluding `timestamp`. Each file keeps SPA `modified` seconds for one release and emits `url` as `{OCU_PUBLIC_PREFIX}/files/{chat_id}/{percent-encoded path}`.
- `POST /api/uploads/{chat_id}/{filename}` — Upload file to container

### Browser (CDP Proxy)
- `GET /browser/{chat_id}/status` — Browser status
- `GET /browser/{chat_id}/json` — CDP targets
- `WebSocket /browser/{chat_id}/devtools/page/{page_id}` — CDP WebSocket proxy

### Terminal
- `GET /terminal/{chat_id}/status` — Terminal/container status
- `POST /terminal/{chat_id}/start-ttyd` — Start terminal session
- `WebSocket /terminal/{chat_id}/ws` — Terminal WebSocket proxy
- `GET /terminal/{chat_id}/heartbeat` — SPA keepalive; issued by `preview.js` through `ocuFetch`
- `POST /terminal/{chat_id}/restart-container` — launch alias used by both stopped recovery branches

### System
- `GET /health` — Health check
- `GET /system-prompt` — Get system prompt (with dynamic skills)
- `GET /skill-list` — List available skills

## Configuration

All via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `OCU_INTERNAL_TOKEN` | _(required)_ | Service credential; REST/WS use `Authorization: Bearer`, MCP uses `X-OCU-Internal-Token` |
| `PUBLIC_BASE_URL` | `http://computer-use-server:8081` | Browser-facing base baked into prompt file links and emitted as `X-Public-Base-URL`. It accepts an absolute `http(s)` base or root-relative `/ocu`; a configured value must not end with `/`. |
| `OCU_PUBLIC_PREFIX` | _(empty)_ | Same-origin path prefix for preview shell assets, `apiUrl`, `filesBase`, SPA heartbeat, and the static mount (`{prefix}/static` only). Empty preserves baseline URLs. Noncanonical values fail import/startup. A prefix whose `{prefix}/static/` falls under a guarded chat namespace (`/files`, `/preview`, `/browser`, `/terminal`, `/internal`, `/api/outputs`, `/api/uploads`, and descendants) fails startup by the guard's path classification; `/api` and `/files-ui` remain valid. Authored SPA modules use relative imports and one `ocuFetch` wrapper (`X-Requested-With: ocu-workspace`; prefix once on client root paths; server-emitted URLs remain verbatim). HTML previews keep `sandbox="allow-scripts allow-forms"` without `allow-same-origin`. |
| `MCP_API_KEY` | _(empty)_ | Optional second MCP Bearer credential; required in addition to the internal token when set |
| `OCU_SANDBOX_SUBNET` | _(empty)_ | Optional denied transport subnet, validated at startup |
| `OCU_WEBUI_ORIGIN` | _(empty)_ | Optional sole CORS origin, validated at startup |
| `DOCKER_IMAGE` | `open-computer-use:latest` | Sandbox container image |
| `COMMAND_TIMEOUT` | `120` | Bash command timeout (seconds) |
| `SUB_AGENT_TIMEOUT` | `3600` | Sub-agent timeout (seconds) |
| `USER_DATA_BASE_PATH` | `/tmp/computer-use-data` | Host path for file exchange |
| `BASE_DATA_DIR` | `/data` | Server-side path to chat data |
| `CONTAINER_MEM_LIMIT` | `2g` | Container memory limit |
| `CONTAINER_CPU_LIMIT` | `1.0` | Container CPU limit |
| `CONTAINER_IDLE_TIMEOUT` | `600` | Host-owned idle stop for a continuously observed running sandbox (seconds). Paused time and OCU downtime do not count. |
| `OCU_IDLE_POLL_SECONDS` | `30` | Idle observation cadence. Must be a positive integer shorter than `CONTAINER_IDLE_TIMEOUT`. |
| `OCU_SANDBOX_NO_AUTOSTART` | unset | When exactly `1`, created sandboxes receive `NO_AUTOSTART=1`. |
| `MCP_TOKENS_URL` | _(empty)_ | Settings wrapper URL (optional) |
| `MCP_TOKENS_API_KEY` | _(empty)_ | Settings wrapper auth key |

## Running Standalone

```bash
cd computer-use-server
pip install -r requirements.txt
OCU_INTERNAL_TOKEN=replace-me uvicorn app:app --host 0.0.0.0 --port 8081 --no-proxy-headers
```

Requires Docker socket access and a built workspace image.

## Docker

```bash
docker compose up --build computer-use-server
```

See [docker-compose.yml](../docker-compose.yml) for the full stack configuration.
