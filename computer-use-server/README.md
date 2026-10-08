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
unchanged files. `GET /api/outputs/{chat_id}` holds one chat lock across pending
Office recovery, then the ordinary broker scan, and returns bounded pages with
prefixed, percent-encoded cookie-path URLs. Unavailable recovery returns 503
with `Retry-After` and does not scan. Missing Office is a no-op for this caller
and does not create an Office tree. Cursor, limit, and index behavior stay with
the broker. `GET /internal/describe/{chat_id}` reports the persisted counter
without a scan or index create. Broker defaults remain 100 items per page
(maximum 1,000), 10,000 active files, 100 MiB per file, and a 64 MiB index.

Polling cannot detect a same-size in-place edit or a delete/recreate completed
between reconciliations. A stale cached hash after the former can also prevent
continuity from being recognised on a later rename. A missing outputs root is
empty only on first use or with no active entries; if active identities already
exist, reconciliation fails retryably and preserves the index. Live names that
cannot be persisted as relative POSIX paths fail explicitly rather than being
rewritten as a corrupt index.

### Office save and callback publication

`GET /api/office/{chat}/documents/{file}/versions` returns exactly `file_id`,
`published_version`, `open_session` and `versions`. Versions are ordered by
ascending `number`; each contains exactly `number`, `parent`, `source`, `sha256`,
`size`, `created_at` and `published`. Each `published` flag records historical
publication, not the current workspace version; `published_version` is the
current published pointer and must not be inferred from the highest flagged
version.

Any active workspace file, regardless of type, can be listed without creating
persisted Office state. Without history, `versions` is `[]` and
`published_version` is `null`. Without an open session, `open_session` is `null`;
otherwise it contains exactly `session_id`, `state`, `reason` and `editor_ended`.
`editor_ended` means a final callback receipt (status 2, 3 or 4) exists, not that
the session has a terminal lifecycle state. Malformed, unknown, tombstoned or
other-chat file identities return 404 `unknown_file`.

Normal listing does not contact DocumentServer, read or hash workspace content,
mutate files/history, or refresh activity/notices. On an epoch change, existing
accepted publication obligations recover before orphaning; this can change
files/history but creates no new publication intent and makes no DocumentServer
request. Recovery commits before the requested file identity is re-resolved and
its open session reselected, so a save-as session moved to a new document is not
reported on the original. Unresolved recovery retains the obligation and returns
503 `publish_pending`, not a successful list or fabricated orphan. Existing
final-outcome protections described below still apply.

`POST /api/office/{chat}/documents/{file}/restore` accepts `{"number": n}`,
where `n` is an integer historical version number (not a boolean, string or
float). Success returns exactly `{"file_id": file_id, "number": new_number,
"published": true}`; `new_number` identifies the newly published restore,
not the selected version. Invalid bodies return 422 `invalid_request`;
unknown/inactive file identities and absent version numbers return 404
`unknown_file` and `unknown_version`, respectively, before session mutation.

Restore uses the same epoch, document-key and final-receipt reopen checks as
create, recovering accepted publication before orphaning. An unreachable key
returns 502 `documentserver_unavailable` without orphaning; any remaining open
session returns 409 `session_open`. The original file identity is re-resolved
after recovery, never replaced with a save-as successor. Missing paths return
409 `path_missing`; unsafe paths return 503 `unsafe_path`. Neither refusal
adds a version, follows links or recreates a path.

Inside one canonical publication fence, restore preserves current Agent
workspace content absent from history as a `workspace` version, then appends
and publishes a fresh `restore` whose parent is the selected version. Known
workspace hashes reuse history. Even latest-equal content gets a new restore
record sharing the selected immutable blob; old records, publication flags
and blobs are not rewritten. Callback and conflict-overwrite latest-hash
deduplication remain unchanged. Recovery reuses the accepted restore binding
and captures intervening Agent content before replacement.

Pause failure returns 503 `pause_failed` and retains a new unpublished restore
of stored historical content without reading, capturing or modifying workspace
bytes. Prepared failures before replacement return 503 `storage_low`,
`index_unavailable` or `publish_timeout`, retaining unpublished content.
After acceptance, those interruptions before lineage preparation, after
replacement, or during recovery retain the publication obligation instead of
claiming rollback; the initiating request returns the specific failure reason.
Unresolved recovery or uncertain fencing returns 503 `publish_pending`.
Durability and corrupt-state errors remain explicit 500 `state_durability`
and `state_corrupt`.

Create drains an accepted restore for that file before admitting an editor;
unresolved recovery refuses admission with 503 `publish_pending`. Disable new
Office requests and drain accepted restore obligations before downgrading
their reader; do not delete history or journals to permit downgrade.
No restore frontend or gateway integration is provided.

Authenticated status 6 callbacks with recorded `publish` intent and status 2
callbacks commit their version, receipt and publish obligation together. The
existing fenced publisher then runs synchronously; terminal conflict or failure
still acknowledges durable content with `{"error": 0}`. Classified sandbox or
recovery admission refusals also acknowledge it while retaining the obligation
and session ownership for recovery. Unexpected publication errors retain their
error behavior, and unresolved pre-orphan recovery still blocks orphaning.
Persist-intent callbacks store content without a publication obligation.

A publish-intent save for which DocumentServer reports nothing new commits an
obligation bound to the latest unpublished stored version alongside the save
completion. Status 4 commits a final obligation with its contentless receipt
(null hash/version). Neither path adds a version or blob; the save needs no
callback receipt. Final receipt replay drives its persisted journal binding
without downloading or selecting a newer version.

Nothing-new saves of either intent advance both sequence values when the latest
version is already published, without writing the workspace or advancing its
revision. With an unpublished latest version, persist-only nothing-new saves
advance only `last_committed_seq` and leave no deferred publication. Equal-content
autosaves reuse the latest version and advance both counters if it is published.

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
obligations.

Successful publication invalidates the cached notice size/mtime sample when its
session baseline changes; the next status observation determines `workspace_changed`.

A final callback that finds the original path gone, or an original parent below
the workspace root replaced by a symlink, publishes retained content as a new
document under a numbered no-replace name and closes the session with `saved_as`.
The new identity is distinct; source history, receipts and `document_key` stay
on the original document. Ordinary saves and a leaf symlink keep
`baseline_mismatch` for the next create. A missing or unsafe outputs root
creates nothing and ends `error` / `workspace_missing` without recreating
directories. Crash recovery of an owned copy remains the publisher's: it
preserves changed or renamed exposed content; an unchanged, proven-owned
single copy can complete at its actual safe name; an ambiguous moved parent
or extra ownership blocks Files with 503 rather than duplicating or deleting
content; and unavailable copy-registration capacity ends `error` /
`index_unavailable`, retaining stored content without an unregistered
visible copy or a listing deadlock.

`POST /api/office/{chat}/sessions/{session}/resolve` accepts `save_as` (the
default for an empty body or omitted action) or explicit `overwrite`, only
in `conflict`. Success returns `session_id`, `state`, `file_id` and the actual
`path`. Save-as keeps the original bytes and history, claims a numbered name
without replacing, and moves the session to a new document under the same key.
Missing or linked original parents select the safe workspace root.

Overwrite captures safe workspace content inside the same publication fence,
reusing any historical hash. Capture and a user-content `restore` record are
committed together with the journal binding; the restore parent is the selected
user version. Latest-hash deduplication and immutable blobs are shared with
ordinary saves. The latest version remains user content for join/source,
nothing-new saves and status 4, including after a refused replacement.
Successful completion publishes the restore and original selected user record.

Resolve freezes action, source and committed sequence before capture, allocates
no sequence or receipt, and completes lifecycle and publication atomically.
It becomes `closed` iff a final callback receipt exists, otherwise `editing`;
published and committed sequences agree. Recovery drives the bound action,
captures intervening workspace content before overwrite, and reuses owned
copy/replacement and registration evidence instead of duplicating publication.
Drain accepted resolve journals before downgrading their reader.

An eligible new callback settles an accepted resolve before binding content,
a receipt or lifecycle changes to the document, then reloads the current session.
This applies to final status 2/4, higher-sequence status 6 with either intent,
and status 1/3/7. Authentication, status/sequence admission and content validation
precede this barrier; rejected callbacks and receipt-only replays gain no
unrelated resolve authority. Pending ownership or fencing returns 503
`publish_pending` without committing the incoming callback, so it remains
retryable. Save-as receipts keep their original version binding; later content
belongs to the successor, and overwrite lineage precedes later user versions.
Status 4 on an already-published latest version advances both committed and
published progress to its final sequence without another version or write.

Nothing-new save-command completion uses the same accepted-resolve settlement
owner before advancing progress or binding a save obligation. The command waits
outside the chat lock; reconciliation checks its key, pending sequence, issued
intent, lifecycle/final receipt and restore epoch under that lock, then reloads
and rechecks authority after recovery. Either intent advances both counters when
resolve has published the current latest version. Uncertain ownership or fencing
returns 503 `publish_pending`, retaining the accepted resolve and pending save
allocation without a journal tied to the old document. A later eligible callback
can complete that save. Registration and durability errors remain explicit.
Accepted command responses do not mutate completion state; stale responses do
not consume newer allocations or revive ended sessions. Unknown-key completion
still recovers prior publication before orphaning; epoch changes grant no new
completion authority.

Malformed/non-object bodies and unsupported actions return 422 `invalid_request`;
unknown sessions return 404 `unknown_session`; other states, including
epoch-orphaned sessions, return 409 `not_in_conflict`. Overwrite never recreates
a missing path (409 `path_missing`) or follows unsafe paths (503 `unsafe_path`).
A missing workspace returns 409 `workspace_missing`, retaining history and
ending `error` without directory creation. Ordinary pause, timeout, index and
storage refusals return 503 with their exact reason and preserve the conflict.
Interrupted ownership or postreplacement work retains its obligation, not a
successful response or a fabricated rollback.

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
  `attachment`. Every successful file GET, inline or forced download, carries
  exactly one `Cache-Control: no-store` header. The SPA HTML renderer uses the
  same sandbox tokens on `srcdoc` and `src` iframes and does not add
  `allow-same-origin`.
- `GET /files/{chat_id}/archive` — Download all outputs as ZIP
- `GET /api/outputs/{chat_id}` — Authenticated broker listing: `chat_id`, `files`, `total`, `timestamp`, `revision`, `next_cursor`. Query `cursor` and `limit` (1..1000, default 100). Malformed, out-of-range, or unparseable cursors (including oversized digit runs) return 400; stale cursors return 409. The listing holds one chat lock across pending Office recovery then the ordinary broker reconcile; unavailable recovery returns 503 with `Retry-After: 1` and does not scan. `If-None-Match` uses a weak ETag over the page representation excluding `timestamp`. Each file keeps SPA `modified` seconds for one release and emits `url` as `{OCU_PUBLIC_PREFIX}/files/{chat_id}/{percent-encoded path}`.
- `POST /api/uploads/{chat_id}/{filename}` — Upload a file into the workspace files directory (`{BASE_DATA_DIR}/{chat_id}/outputs`, mounted at `/mnt/user-data/files`)

#### Office editor host embedding

The authenticated same-origin `/preview/{chat_id}?embed=office` page requires a
single Office parameter, server-side Office enablement and a parent frame.
`office-editor.js` installs its listener before announcing `ocu:office-ready`.
Before the first valid `ocu:office-open`, it shows `Office editor idle` and makes
no Office request or DocumentServer API load. It accepts only its same-origin
parent, exact message fields, matching chat and a non-negative safe generation;
one page admits one open, including after failure. Files selection messages do
not activate it, and no Files/Browser/Terminal client is mounted.

The host creates or joins through `ocuFetch`, then reads the returned session
once for its persisted state, reason, publication sequences and change notice.
That initial read is not a poll loop. The API script comes from the configured
origin at `/web-apps/apps/api/documents/api.js`; the broker's signed document,
editorConfig and token fields pass through unchanged. State reports retain the
original file and generation, including after save-as, and are sent only when a
reported value changes.

Named broker validation refusals and `unpublished_version` are final `refused`
with no session id. Creation transport/server failures, failed status reads,
API load/timeout/constructor failures and editor connection loss are `error`;
an obtained session id is retained and another tab's session is not closed.
Editor modification acknowledgements do not prove a workspace save or clear
dirty state. Save/close execution, recurring polling and auto-save are not part
of this host's delivered protocol behavior.

`officeDocserverOrigin` comes only from `OCU_OFFICE_DOCSERVER_ORIGIN` on the server,
not a query, parent message or user iframe setting. Browser configuration and
served local scripts contain no service/model/JWT secrets or derived ticket keys.
The dedicated policy uses a fresh configuration-script nonce:
`default-src 'none'`, `script-src 'self' 'nonce-<response nonce>' <DocumentServer origin>`,
`frame-src <DocumentServer origin>` and `connect-src 'self'`.
It retains runtime `style-src 'self' 'unsafe-inline'`, `img-src 'self' data: blob:`
and `font-src 'self' data:`, with `base-uri`, `object-src` and `form-action` all
`'none'`, and `frame-ancestors 'self'`. Configured API JavaScript executes with
host-origin privileges; only the cross-origin nested document is
SOP-separated. See the [Office host trust decision](https://github.com/DankerMu/open-webui/blob/main/docs/decisions/implemented/architecture/2026-10-08-ocu-office-editor-frame.md).
Generated-content opaque isolation and existing Files/runtime policies are unchanged.

Absent, empty or whitespace-only `OCU_OFFICE_DOCSERVER_URL` keeps Office disabled:
the preview returns HTTP 200 and displays `Invalid preview embedding`, without a
DocumentServer origin/configuration or Office CSP. Repeated, mixed, unknown and
top-level embedding also fails visibly without Office/Files/runtime application
requests or outgoing messages; enabled single-Office top-level responses still
carry the Office configuration and policy.
Only enabled single-Office responses validate the browser origin. Malformed or
CSP-inexpressible authorities return HTTP 500 with the fixed detail
`Invalid Office browser origin configuration`, without echoing the value or
relaxing policy. Bracketed IPv6 URLs are valid URLs but unsupported CSP authorities;
use a DNS hostname instead, including one resolving to IPv6. This request-time
CSP validation does not add URL-format startup validation.

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
