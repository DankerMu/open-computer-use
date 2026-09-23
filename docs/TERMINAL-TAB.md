# Sub-Agent Tab — Terminal + Claude Code

## Overview

The "Sub-Agent" tab in the preview panel provides:

1. **Claude Code monitoring** — see running processes, kill stuck ones
2. **Interactive terminal** — launch Claude Code manually, resume interrupted sessions

## How it works

```
Browser (xterm.js)  ←WebSocket→  Computer Use Server  ←WebSocket→  Container (ttyd:7681)
                                 (proxy on :8081)                   (tmux + bash)
```

- **ttyd** — WebSocket terminal server inside the container (port 7681)
- **tmux** — persistent session (reconnectable, scroll history preserved)
- **Computer Use Server** — transparent WebSocket proxy
- **xterm.js** — terminal rendering in the browser

## Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /terminal/{chat_id}/status` | Check if ttyd is running |
| `WebSocket /terminal/{chat_id}/ws` | Terminal WebSocket connection |
| `POST /terminal/{chat_id}/start-ttyd` | Start ttyd (lazy — first click) |
| `GET /terminal/{chat_id}/sessions` | List Claude Code JSONL sessions |
| `GET /terminal/{chat_id}/processes` | List running Claude Code processes |
| `POST /terminal/{chat_id}/processes/{pid}/kill` | Kill a stuck process |

## Lifecycle

1. AI calls `sub_agent` → sandbox container is created only when neither a container nor valid metadata exists.
2. Filter injects preview link → Artifacts panel opens.
3. User sees dashboard with processes and sessions.
4. Click **"Open terminal"** → ttyd starts, Claude Code launches.
5. tmux session is persistent — reconnectable on disconnect.
6. While the page is open, `/terminal/{chat_id}/heartbeat` extends the host-owned idle window.
7. OCU stops a continuously observed running sandbox after `CONTAINER_IDLE_TIMEOUT` (default: 10 min). External Docker pause time does not count, and no idle stop runs while OCU is down.

A stopped, paused, created, restarting, dead, or removed-but-metadata sandbox is not started by a tool call. Resume is explicit `POST /internal/launch/{chat_id}` or its `/terminal/{chat_id}/restart-container` and `resurrect-container` aliases. Launch answers `{"state":"running"}` only after observing running. `GET /internal/describe/{chat_id}` reads state and does not change it. Both require the internal bearer token.

## Existing sleeper cutover

Containers created before this change may contain a detached `sleep && kill 1` timer. Quiesce the old orchestrator before deploying this one.

1. Running sandboxes: the first explicit launch or host reap retires that sleeper under its in-container flock, verifies it is gone, and records the container id. Heartbeats, tool activity, and startup sweeps preserve existing evidence and never invent it. A failed retirement is an explicit migration error; the sandbox is not adopted silently.
2. Exited or created sandboxes have no live sleeper. Explicit launch starts the same container and does not exec a retirement command first.
3. Paused pre-upgrade sandboxes cannot run retirement code. Stop that container with Docker yourself, preserve the container and mounts, verify it is stopped, then deploy. Launch reports `migration-required` and does not unpause, delete, or recreate it until that operator stop is done. After the stop, explicit launch starts the same container.

The pause-aware idle guarantee applies after this cutover. An upgrade is incomplete while a legacy paused sandbox remains. Real Docker evidence for this cutover is deferred to epic task 19.0.

## Sub-agent Timeout

If `sub_agent()` times out (default: 3600s) but Claude Code is still running:
- The model receives a timeout message
- User can observe progress in the Sub-Agent tab
- Can stop or continue interactively in the terminal

## MCP Servers in Claude Code

When MCP server names are passed via `X-MCP-Servers` header, the server auto-generates `~/.mcp.json` inside the container. Claude Code picks it up and can use those MCP servers autonomously. See [MCP.md](MCP.md#mcp-servers-for-claude-code-sub-agent) for details.
