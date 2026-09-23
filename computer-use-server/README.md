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
| `skill_manager.py` | Skill registry: fetch user skills, cache ZIPs, generate system prompt XML |
| `system_prompt.py` | System prompt templates with skill injection |
| `context_vars.py` | Per-request context (chat_id, user_email, etc.) via ContextVar |
| `docs_html.py` | HTML documentation page generator |

## API Endpoints

### MCP
- `POST /mcp` — MCP Streamable HTTP endpoint (main interface)

### Files
- `GET /files/{chat_id}/{filename}` — Serves output files. Non-download HTML,
  SVG, XHTML, and XML responses mirror generated-content isolation with
  `Content-Security-Policy: sandbox allow-scripts allow-forms` and
  `X-Content-Type-Options: nosniff`; disposition is unchanged (#62 tracks its
  follow-up).
- `GET /files/{chat_id}/archive` — Download all outputs as ZIP
- `GET /api/outputs/{chat_id}` — List output files with metadata
- `POST /api/uploads/{chat_id}/{filename}` — Upload file to container

### Browser (CDP Proxy)
- `GET /browser/{chat_id}/status` — Browser status
- `GET /browser/{chat_id}/json` — CDP targets
- `WebSocket /browser/{chat_id}/devtools/page/{page_id}` — CDP WebSocket proxy

### Terminal
- `GET /terminal/{chat_id}/status` — Terminal/container status
- `POST /terminal/{chat_id}/start-ttyd` — Start terminal session
- `WebSocket /terminal/{chat_id}/ws` — Terminal WebSocket proxy

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
| `OCU_PUBLIC_PREFIX` | _(empty)_ | Same-origin path prefix for preview shell assets, `apiUrl`, `filesBase`, heartbeat, and the static mount (`{prefix}/static` only). Empty preserves baseline URLs. Noncanonical values fail import/startup. A prefix whose `{prefix}/static/` falls under a guarded chat namespace (`/files`, `/preview`, `/browser`, `/terminal`, `/internal`, `/api/outputs`, `/api/uploads`, and descendants) fails startup by the guard's path classification; `/api` and `/files-ui` remain valid. Prefixed SPA deployment is not complete until issue17 rewrites remaining module/script assets. |
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
