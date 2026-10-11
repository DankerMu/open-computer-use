# Known Bugs

## 1. MCP Server: TypeError 'NoneType' on SSE notifications

**Severity:** Low (doesn't break functionality — tool calls return 200 OK)

**Error:**
```
starlette/routing.py, line 74, in app
    await response(scope, receive, send)
TypeError: 'NoneType' object is not callable
```

**When:** Every `POST /mcp` that returns 202 Accepted (SSE notification acknowledgment).

**Root cause:** MCP Python SDK's Streamable HTTP handler returns `None` for fire-and-forget notification responses. Starlette expects an ASGI callable Response object.

**Impact:** Error logged but tool calls work. The 202 response is already sent before the error.

**Likely fix:** Check if `mcp` SDK version has a fix, or patch the route handler to return an empty Response for 202.

---

## 2. File preview auto-show depends on model behavior

**Severity:** Medium (functional but not always automatic)

**How it works:** The `fix_preview_url_detection` patch detects Computer Use Server file URLs in assistant messages and auto-opens an iframe preview in the artifacts panel. The `computer_link_filter` injects the file server URL into the system prompt so the model knows to include file links.

**Issue:** Some models (especially smaller ones) don't consistently include file URLs in their responses even when instructed by the system prompt. When the model just says "file created" without the URL, the preview patch has nothing to detect.

**Workaround:** The filter's outlet always adds "View your file" link and "Download all as archive" button — these work regardless. For auto-preview, use models that reliably follow system prompt instructions (Claude, GPT-4o, larger Qwen models).

---

## 3. PPTX generation may timeout with complex prompts

**Severity:** Low (model-dependent)

**When:** Model makes many sequential tool calls (19+ bash, create_file, view, str_replace) for complex tasks like presentation creation.

**Impact:** Response may appear incomplete if model hits max tool call retries.

**Workaround:** Use simpler prompts or increase `COMMAND_TIMEOUT` in `.env`.

---

## 4. Direct service credentials are not per-user authorization

**Severity:** Medium (direct-service trust limitation)

**Browser gateway:** The [documented proxy](../deploy/proxy/README.md#trust-and-route-behavior) protects outputs, files, browser and terminal chat routes using the WebUI session Cookie and a route-derived `X-Chat-Id`. WebUI checks that the authenticated user owns that chat. Knowing its UUID alone does not authorize access through this gateway.

**Direct OCU access:** Guarded REST requests and WebSocket handshakes require `Authorization: Bearer` with the shared `OCU_INTERNAL_TOKEN`. This authenticates a service, not the requesting user's ownership of a chat. MCP instead requires that token in `X-OCU-Internal-Token`; when `MCP_API_KEY` is configured, MCP also requires it as a separate Bearer credential. Neither credential substitutes for the other.

**Residual risk:** A shared service credential is not a per-user access boundary. Keep the direct service network-isolated and the credential private; browser access belongs behind the owner-checking gateway. UUID unguessability is not the gateway's access control. See [Security Notes](../README.md#security-notes).

**Roadmap:** Per-session signed tokens for file/preview/terminal endpoints remain a [planned direct-service hardening measure](../README.md#security-roadmap), not a shipped replacement for these boundaries.

**Scope:** These authorization boundaries do not establish file-serving containment or response pinning; the separate [archive](https://github.com/DankerMu/open-webui/issues/200) and [single-file](https://github.com/DankerMu/open-webui/issues/201) security work is not fixed by this documentation update.

---

## 5. MCP tools cannot return images to the model (Open WebUI limitation)

**Severity:** Medium (functional limitation)

**Issue:** Open WebUI does not support `image` content type in MCP tool results — only `text` is handled. This means tools cannot return screenshots or images back to the model for analysis.

**Impact:** Tools like Playwright (page screenshots) or bash (generated charts) cannot pass visual output directly to the model. The model never sees the image, so it can't reason about what's on screen.

**Workaround:** The `describe-image` skill works around this by calling a separate vision model (e.g., GPT-4o, Qwen-VL) to describe the image as text, which is then returned to the main model. Playwright and other skills save images to `/mnt/user-data/files/` as HTTP links for the user to view in the file preview panel.

**Root cause:** Open WebUI's MCP integration only passes `text` content blocks from tool responses to the model. This is an upstream limitation — see [open-webui/open-webui](https://github.com/open-webui/open-webui) for progress on image tool result support.

---

## 6. Preview breaks after custom `container_name` or non-default compose layouts

**Severity:** Medium (configuration-only, no data loss)

**Status (v4.0.0):** the class of "two settings must match" bugs was fixed by moving public-URL ownership to the server. The filter now reads the public URL from the server's `X-Public-Base-URL` response header — there is only one place to set it.

**How it works:** The Computer Use Server embeds file links into every assistant message using its `PUBLIC_BASE_URL` env var. The Open WebUI filter's `outlet()` uses whatever URL the server returned in the response header — no separate public-URL Valve. The filter's internal `ORCHESTRATOR_URL` Valve is a Docker-only hostname and doesn't leave the container.

**What can still go wrong:** the `PUBLIC_BASE_URL` on the `computer-use-server` container must still be a URL the user's browser can reach. Custom compose project names, removed `container_name:` pins, or missing DNS mean the browser sees links pointing at an unreachable hostname — the preview iframe then 404s / shows a blank frame even though the filter decorates it correctly.

**Workaround:** Set `PUBLIC_BASE_URL` (server `.env`) to your externally reachable URL (`http://your-host.lan:8081`, `https://cu.example.com`, …). The `fix_preview_url_detection` patch is host-agnostic — no build-arg required. See [docs/openwebui-filter.md](openwebui-filter.md#two-url-roles--public-server-env-and-internal-filtertool-valve).
