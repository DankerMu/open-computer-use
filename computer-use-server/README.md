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
| `ws_recheck.py` | Captured-session CDP/ttyd authorization re-check and revocation shutdown |
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

### Office callback publication

Authenticated status 6 callbacks with recorded `publish` intent and status 2
callbacks commit their version, receipt and publish obligation together. The
existing fenced publisher then runs synchronously; terminal conflict or failure
still acknowledges durable content with `{"error": 0}`. Classified sandbox or
recovery admission refusals also acknowledge it while retaining the obligation
and session ownership for recovery. Unexpected publication errors retain their
error behavior, and unresolved pre-orphan recovery still blocks orphaning.
Persist-intent saves store unpublished content without a publication obligation.

Publication completion removes its obligation in the same state update that
records the session outcome, published version, baseline and monotonic
`last_published_seq`. Save outcomes preserve newer outstanding saves and closing
sessions. Interrupted obligations remain recoverable. Receipt replays drive
only a matching obligation, after status/content validation; final replays do
not download. Requests and the session sweep complete surviving obligations
before orphaning. Closed/error outcomes remain terminal; epoch orphaning
preserves a final conflict only in the request that recovered its surviving
final obligation. A pre-existing final conflict without an obligation still
obeys the epoch policy. An epoch-invalid callback refuses without downloading
or processing new content; pre-orphan recovery drives only prior durable
obligations. Status 4/no-change publication and automatic copies for missing
final paths are separate behavior outside this callback-content path.

## API Endpoints

### MCP
- `POST /mcp` — MCP Streamable HTTP endpoint (main interface)

### Files
- `GET /files/{chat_id}/{filename}` — Serves output files. Non-download HTML,
  SVG, XHTML, and XML responses are isolated with
  `Content-Security-Policy: sandbox allow-scripts allow-forms`,
  `X-Content-Type-Options: nosniff`, and Starlette
  `FileResponse(..., content_disposition_type="inline", filename=...)`
  (RFC 5987 `filename*` for non-Latin-1 names). `?download=1` still forces
  `attachment`. The SPA HTML renderer uses the same sandbox tokens on `srcdoc`
  and `src` iframes and does not add `allow-same-origin`.
- `GET /files/{chat_id}/archive` — Download all outputs as ZIP
- `GET /api/outputs/{chat_id}` — Authenticated broker listing: `chat_id`, `files`, `total`, `timestamp`, `revision`, `next_cursor`. Query `cursor` and `limit` (1..1000, default 100). Malformed, out-of-range, or unparseable cursors (including oversized digit runs) return 400; stale cursors return 409. `If-None-Match` uses a weak ETag over the page representation excluding `timestamp`. Each file keeps SPA `modified` seconds for one release and emits `url` as `{OCU_PUBLIC_PREFIX}/files/{chat_id}/{percent-encoded path}`.
- `POST /api/uploads/{chat_id}/{filename}` — Upload a file into the workspace files directory (`{BASE_DATA_DIR}/{chat_id}/outputs`, mounted at `/mnt/user-data/files`)

#### Files-only preview embedding

Embed the authenticated same-origin `/preview/{chat_id}?embed=files` page in an
iframe with exactly `sandbox="allow-scripts allow-same-origin allow-forms"`.
The parent must remain the authenticated owner of the file list, revision hints
and download button; do not add `allow-downloads`. Without `embed`, the preview
keeps its standalone Files/Browser/Terminal behavior. Unsupported or repeated
`embed` parameters fail visibly without starting runtime clients.

After its listener is installed, the child sends
`{type:"ocu:preview-ready", chat_id}` to `window.parent` at `location.origin`.
The parent may then send exactly
`{type:"ocu:preview-select", chat_id, file_id, generation}` to the iframe
window at that origin. `file_id` is a nonempty string of at most 128 characters;
`generation` is a nonnegative safe integer strictly increasing for that iframe,
including re-requests for a deleted or revised identity. Unknown fields are
rejected. The child accepts only `event.source === window.parent` and
`event.origin === location.origin` with the configured chat id. It sends
`{type:"ocu:preview-state", chat_id, file_id, generation, state}` where
`state` is `loading`, `ready`, `error`, `missing` or `unsupported`. The parent
must check the reply's source, origin, chat id and current generation as well.
No URL, file bytes, credentials or raw error text travel over this channel.

Selection resolves the id against the authorized broker's coherent listing,
up to 100 pages/10 seconds (including a stale-cursor retry). Only a completed
same-revision enumeration can prove `missing`; failures or limits are `error`.
Resolved URLs must be canonical same-origin URLs under this chat's files path.
Only broker types DOCX/XLSX/PPTX are eligible, and HTML, SVG, XHTML, XML and
`+xml` MIME essences are refused regardless of type. `ready` follows successful
Office rendering; corrupt Office reports `error` visibly, so the parent can
offer its own authorized download. The child does not poll, autoselect, handle
generated-content link selection, or mount Browser/Terminal/CLI clients. A
newer request clears the old render and supersedes its result; tearing down
the iframe releases owned effects.

DOCX and SheetJS-converted HTML pass through locally pinned DOMPurify before
insertion into the trusted DOM in both embedded and standalone previews.
Approved elements are text headings/paragraphs, basic inline formatting,
lists, code, tables, anchors and raster images. Only text, table layout,
anchor and inline-image attributes survive: document styles, classes, scripts,
event handlers, SVG/MathML, forms and embedded frames do not. Images must be
inline base64 PNG/JPEG/GIF/WebP; remote document images are removed before
attaching the sanitized fragment and cannot initiate a network fetch. Links
are either surviving same-document bookmarks (rewritten to namespaced IDs)
or explicit HTTP(S) URLs opened in a new tab with `noopener noreferrer`;
unsafe links retain text without navigation.
Embedded Mammoth style maps are disabled. Broker `.xls` files are the same
`xlsx` type as `.xlsx`; OOXML ZIP, CFB-contained BIFF and complete SheetJS-
readable raw BIFF2/3/4 streams are eligible. Raw BIFF is checked for versioned
BOF, complete record framing and terminating EOF before SheetJS parsing;
arbitrary text and truncated records cannot masquerade as an Office workbook.

The local real-SPA browser harness is
`node tests/orchestrator/preview_embedding_browser.cjs` (Playwright 1.62.1
and its Chromium required); its assets and Office fixtures are local.
Prepare Draw.io viewer materials with `python3 computer-use-server/drawio/prepare_drawio.py`
before native preview verification. The same command runs during the server
image build. Viewer materials are served from `{prefix}/static/drawio/`.
Authored remote diagram resources are outside that offline material closure.
A process-killed publish lock is not automatically taken over; remove a stale
`static/drawio.publish.lock` and restore any `static/drawio.prev-*` directory
before retrying native preparation.

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
| `OCU_SANDBOX_NETWORK` | `ocu-sandbox` | Name of the deployment-provisioned sandbox bridge. Must already exist, use the bridge driver, not be internal, and expose one IPv4 gateway. Reserved names `bridge`, `host`, and `none` are rejected. OCU never creates this network. |
| `SANDBOX_HOST_BIND_IP` | _(empty)_ | Optional IPv4 bind for published CDP/ttyd ports. Empty uses the inspected sandbox-bridge gateway; a nonempty value must equal that gateway. |
| `ENABLE_NETWORK` | `true` | When `false`, created sandboxes use disabled networking with no publication or bridge lookup. |
| `OCU_WEBUI_ORIGIN` | _(empty)_ | Optional sole CORS origin, validated at startup |
| `OCU_WEBUI_AUTH_URL` | _(empty)_ | Absolute `http(s)` URL of WebUI `GET /api/v1/ocu/auth` for CDP/ttyd session re-checks. Empty allows non-WS startup; both WS routes then deny before backend lookup. A nonempty invalid value fails startup. |
| `DOCKER_IMAGE` | `open-computer-use:latest` | Sandbox container image |
| `COMMAND_TIMEOUT` | `120` | Bash command timeout (seconds) |
| `SUB_AGENT_TIMEOUT` | `3600` | Sub-agent timeout (seconds) |
| `BASE_DATA_DIR` | `/data` | Server IO root and Docker bind source for chat data. The configured path must be identical inside the server and at the Docker daemon. |
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
