# Computer Use Tools — Open WebUI Integration

Thin MCP client proxy that connects Open WebUI to the Computer Use Server. All container management, skills, and execution happen server-side — this tool just forwards requests via MCP Streamable HTTP.

## Architecture

```
Open WebUI (browser)
    │
    ▼
computer_use_tools.py  ──MCP over HTTP──►  Computer Use Server (:8081)
    (this tool)                                  │
                                                 ▼
                                          Docker containers
                                          (one per chat session)
```

The tool is a **stateless proxy** — it doesn't manage Docker directly. It sends MCP `tools/call` requests to the server, which handles container lifecycle, file serving, skills, and sub-agents.

## Installation

See [main README](../../README.md#open-webui-integration) for full setup. Quick version:

1. **Workspace > Tools** → Create → paste `computer_use_tools.py`
2. Set **Tool ID** to `ai_computer_use` (required for filter integration)
3. Configure `ORCHESTRATOR_URL`, and provide `OCU_INTERNAL_TOKEN` to the Open WebUI server process. The tool reads it on every call; it is not a Valve. Set `MCP_API_KEY` only when the server requires its optional second MCP credential.
4. Install the companion filters (Workspace > Functions): `computer_link_filter.py` and `video_production_filter.py`; see [Components](../README.md#components) for both and for the optional `org_prompt_filter.py`.

`openwebui/init.sh` does steps 1-4 against a running Open WebUI. The example `docker-compose.webui.yml` runs the stock upstream image and does **not** run `init.sh`; run it yourself or install by hand, as [INSTALL.md](../../docs/INSTALL.md#quick-start) describes. The production-like deployment runs it in its one-shot `open-webui-init` service.

## Configuration

| Valve | Default | Description |
|-------|---------|-------------|
| `ORCHESTRATOR_URL` | `http://computer-use-server:8081` | Direct, non-redirecting internal URL of the Computer Use server (MCP endpoint + file uploads). Not browser-facing. |
| `MCP_API_KEY` | _(empty)_ | Optional second Bearer credential for `/mcp`; ordinary spaces are preserved, while CR/LF and untransmittable header values are rejected. |
| `DEBUG_LOGGING` | `false` | Verbose debug logging |

`OCU_INTERNAL_TOKEN` is required in the Open WebUI server process environment. The tool reads it on every call; it is not a Valve, so browser-facing Valve schema and values never contain it.

All container limits, timeouts, Docker image, and skills remain configured server-side via `.env`.

## Tools Provided

| Tool | MCP Method | Description |
|------|-----------|-------------|
| `bash_tool` | `bash_tool` | Execute bash commands in the sandbox container |
| `create_file` | `create_file` | Create or overwrite files |
| `str_replace` | `str_replace` | Edit files by replacing text |
| `view` | `view` | Read files or list directories (supports line ranges) |
| `generate_speech` | `generate_speech` | Synthesize speech (e.g. a video voiceover line) into `/mnt/user-data/files/`; needs `TTS_*` on the server |
| `sub_agent` | `sub_agent` | Delegate complex tasks to Claude Code |

Each tool call authenticates the transport and carries only server-provided user context (`X-Chat-Id`, `X-User-Email`, `X-User-Name`, `X-Mcp-Servers`). Credentials never appear in tool arguments, results, or progress events.

## Key Implementation Details

- **Lazy MCP client**: `_MCPClient` is created on first use and recreated when the URL, current environment internal token, or MCP API key changes
- **File sync**: On every tool call that carries attachments, the tool consults the server's import receipts and uploads only not-yet-imported attachments under their original WebUI attachment IDs
- **MCP server discovery**: `_get_user_mcp_server_names()` reads Open WebUI's `TOOL_SERVER_CONNECTIONS` and passes available MCP server names to the orchestrator via `X-Mcp-Servers` header — used for Claude Code sub-agent configuration
- **SSE progress**: Tool calls stream progress updates via Server-Sent Events
- **Workspace hint**: After an invoked MCP client attempt returns or raises an ordinary caught exception, `_run_tool` emits one best-effort `ocu:workspace_changed` event with `{chat_id, reason: "tool_completed"}`. The hint is not revision authority and does not include tool content. Internal client health/auth rejections still emit it; outer chat/config validation, header construction failure, cancellation, and a missing emitter do not. An emitter exception is swallowed once and cannot change the tool result.
- **Timeouts**: Client-side timeouts (`CLIENT_HTTP_TIMEOUT=660s`, `SUB_AGENT_CLIENT_TIMEOUT=3660s`) are set higher than server-side to avoid premature disconnects

## Companion Filters

`computer_link_filter.py` and `video_production_filter.py` are required alongside this tool; `org_prompt_filter.py` is optional and installed by `init.sh` only with `OCU_ORG_PROMPT_FILTER=install`. [Components](../README.md#components) describes all three. `computer_link_filter.py`:

- **Inlet** (tool attachment and system prompt injection): Attaches this tool to every saved chat and adds the `<available_skills>` XML block so the model knows which skills exist
- **Outlet** (response post-processing): Detects file URLs in responses and injects preview links, "View file" and "Download archive" buttons

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "Could not connect to Computer Use Server" | Check `ORCHESTRATOR_URL` valve. Inside Docker-compose: use `http://computer-use-server:8081` (service DNS). |
| Tool not used in chat | The global filter attaches the tool to every saved chat, whatever the model; temporary chats get no tool. Check that `computer_link_filter.py` is installed, **Active** and **Global**, and that **Function Calling** is `Native` (`init.sh` sets it for every model in `DEFAULT_MODEL_PARAMS`) |
| Skills not in system prompt | Install and enable `computer_link_filter.py` globally |
| File preview not working | Check `PUBLIC_BASE_URL` env var on the Computer Use server (it's baked into /system-prompt links and returned to the filter via response header — no separate filter Valve). |
| Sub-agent not starting | Set `ANTHROPIC_AUTH_TOKEN` in server `.env` |
