# Computer Link Filter

**File**: `computer_link_filter.py` — required companion to `computer_use_tools.py`.

## What It Does

| Phase | Action |
|-------|--------|
| **Inlet** (before LLM) | Fetches the authenticated server-rendered prompt and injects its file URL mapping and `<available_skills>` XML |
| **Outlet** (after LLM) | Adds a labeled link to the first concrete current-chat file and an optional archive link |

Without this filter, the model won't know about skills or how to generate file download links.

## Valves

| Valve | Default | Description |
|-------|---------|-------------|
| `ORCHESTRATOR_URL` | `http://computer-use-server:8081` | Internal URL of Computer Use server for authenticated `/system-prompt` retrieval. Not browser-facing — the public URL is owned by the server. |
| `PREVIEW_MODE` | `"button"` | `button` adds a labeled link to the first concrete current-chat file; `off` does not. |
| `ARCHIVE_BUTTON` | `"on"` | Add a current-chat archive link when a concrete file is present: `on` \| `off` |
| `INJECT_SYSTEM_PROMPT` | `true` | Inject the server-rendered prompt when the Computer Use tool is active |

See [`docs/openwebui-filter.md`](../../docs/openwebui-filter.md#valves-reference) for the full Valves reference.

## Installation

1. **Workspace > Functions** → Create → paste `computer_link_filter.py`
2. Enable globally (toggle in Functions list).
3. Configure the filter `ORCHESTRATOR_URL` to the internal server address and provide `OCU_INTERNAL_TOKEN` through the Open WebUI process environment.

## How File Links Work

```
inlet() → Fetches the server-baked /system-prompt text with process-environment
          Bearer authentication. The response supplies the exact PUBLIC_BASE_URL.
       → AI generates: [file.docx]({PUBLIC_BASE_URL}/files/{chat_id}/file.docx)
outlet() → Appends a labeled link to the first concrete current-chat file and,
           when enabled, the archive-download link.
```

The server's `PUBLIC_BASE_URL` is the browser-facing source of truth. Set a deployed proxied `/ocu` base without a trailing slash; the server rejects a configured trailing slash at startup. The filter requires the response's `X-Public-Base-URL` header, never substitutes its internal Valve, and keeps its token outside Valves and browser-visible payloads.

## Related

- [tools/README.md](../tools/README.md) — MCP client tool
- [SKILLS.md](../../docs/SKILLS.md) — all available skills
- [Main README](../../README.md#open-webui-integration) — full setup guide
