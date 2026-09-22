# Open WebUI Computer Use Filter

## Purpose

`openwebui/functions/computer_link_filter.py` fetches the server-rendered Computer Use prompt when `ai_computer_use` is active, then adds links only for concrete files in the current chat.

## Installation

1. In Open WebUI, open **Admin Panel** → **Functions** → **New Function**.
2. Paste `openwebui/functions/computer_link_filter.py`.
3. Enable the function globally and configure its `ORCHESTRATOR_URL` to the internal Computer Use Server URL.
4. Make `OCU_INTERNAL_TOKEN` available to the Open WebUI server process.

## Two URL roles — public (server env) and internal (filter/tool Valve)

| Role | Configuration | Example | Contract |
| --- | --- | --- | --- |
| Public browser base | `PUBLIC_BASE_URL` on `computer-use-server` | `https://webui.example/ocu` or `/ocu` | The server bakes this value into prompt file links and returns it in `X-Public-Base-URL`. It accepts an absolute `http(s)` base or a root-relative path, neither with a trailing `/`. |
| Internal service URL | `ORCHESTRATOR_URL` Filter and Tool Valves | `http://computer-use-server:8081` | Open WebUI uses this only for server-to-server requests. A trailing slash is tolerated. Browsers never use it. |

`PUBLIC_BASE_URL` accepts either an absolute `http(s)` base (such as `https://webui.example/ocu`) or a root-relative browser path (`/ocu`). In either form, remove a trailing slash before rollout: a non-empty value ending in `/` stops OCU at startup instead of being normalized. Unset or empty values retain the server default. Development configurations are not required to add an `/ocu` suffix.

## Prompt retrieval and cache

The filter reads `OCU_INTERNAL_TOKEN` from the Open WebUI process environment on every request and sends it as `Authorization: Bearer`. It is not a Valve, browser payload field, result, or log value. The only user identity sent to `/system-prompt` is the email supplied in injected `__user__`.

The server response must include `X-Public-Base-URL`; the filter does not substitute the internal `ORCHESTRATOR_URL` when the header is absent. Redirects are rejected. Cache entries remain scoped to chat and user and are cleared when the internal origin or token changes. A transient transport failure may use a stale entry from the same authority; missing credentials, 401/403 responses, redirects, and missing public metadata never do.

## Concrete file links

For each assistant message, `outlet()` finds the first URL under `{PUBLIC_BASE_URL}/files/{chat_id}/`: an absolute base matches its exact origin and path, while a root-relative base matches only root-relative links. It appends the configured preview label to that exact file URL, preserving percent-encoding, query string, and fragment. Bare-prose sentence punctuation is excluded from the target; explicit Markdown and angle destinations remain literal. The label is added at most once even when the file URL was already ordinary message text.

The archive toggle retains its separate `{PUBLIC_BASE_URL}/files/{chat_id}/archive` link. An archive endpoint is not itself a concrete file, and neither decoration is added for browser-only output, another chat, another public base, non-assistant messages, or non-string content.

## Valves reference

| Valve | Default | Behavior |
| --- | --- | --- |
| `ORCHESTRATOR_URL` | `http://computer-use-server:8081` | Internal endpoint for authenticated `/system-prompt` retrieval. |
| `INJECT_SYSTEM_PROMPT` | `true` | Enables prompt injection when the Computer Use tool is active. |
| `PREVIEW_MODE` | `button` | `button` appends a labeled link to the first concrete current-chat file; `off` does not. |
| `ARCHIVE_BUTTON` | `on` | Appends the current-chat archive link only when a concrete current-chat file is present. |
| `PREVIEW_BUTTON_TEXT` | filter default | Label for the concrete-file preview link. |
| `ARCHIVE_BUTTON_TEXT` | filter default | Label for the archive link. |

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No prompt or links after a deployment | Confirm `OCU_INTERNAL_TOKEN` is available to the Open WebUI process and that the server accepts its Bearer request. |
| Startup exits before serving | Remove the trailing `/` from configured `PUBLIC_BASE_URL`. |
| No link is appended | Confirm an assistant message contains a concrete current-chat URL under the response header's public base; browser-only output and archive URLs do not qualify. |
| Filter cannot reach the server | Configure `ORCHESTRATOR_URL` to a hostname reachable from the Open WebUI container, then re-seed the Filter Valve. |
