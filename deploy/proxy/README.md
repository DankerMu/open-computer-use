# OCU and DocumentServer reverse proxy

`routes.json` is the single reviewed allowlist. `render.py` validates the table,
configuration and token before generating the self-contained, secret-bearing
`nginx.conf` from `nginx.conf.in`. Its reviewed table fingerprint is pinned in
`render.py`: changing any row requires updating that fingerprint and reviewing the
route/auth/method/mutation/transport implications together. Unknown methods and
`/ocu/` paths do not contact OCU. Outside `/ocu`, the gateway listener forwards
to WebUI. The second listener forwards only to DocumentServer after session auth.

## Provision and run

Supply `OCU_INTERNAL_TOKEN` from protected secret storage and
`OCU_WEBUI_ORIGIN` as the browser-visible `http(s)://host[:port]` origin, without
a trailing slash, path, query or fragment. The token must be nonempty visible
ASCII (0x21–0x7E); spaces, controls and non-ASCII fail before the previous
config is replaced. Never put the token in shell history, command-line arguments,
logs or a tracked file. Render with a protected environment or mounted secret,
not by pasting a production token into an example command.

Listener variables (production must explicitly provision its internal topology):

| Variable | Local default | Purpose |
| --- | --- | --- |
| `OCU_WEBUI_UPSTREAM` | `http://127.0.0.1:8080` | Direct WebUI auth/WebUI traffic, not the browser gateway |
| `OCU_PROXY_UPSTREAM` | `http://127.0.0.1:8090` | Internal OCU HTTP/WebSocket listener |
| `OCU_PROXY_LISTEN` | `127.0.0.1:8082` | Browser-facing nginx listener |
| `OCU_OFFICE_PROXY_LISTEN` | none; required | Separate DocumentServer listener, container `0.0.0.0:8083` |
| `OCU_OFFICE_PROXY_UPSTREAM` | none; required | DocumentServer internal HTTP origin, from `OCU_OFFICE_DOCSERVER_URL` |

WebUI and OCU upstreams require `http://hostname:port` (or IPv4).
The DocumentServer upstream also accepts an omitted port as HTTP80, matching
`http://documentserver`. nginx resolves names at validation/startup.
Both Office inputs are required even when editing is disabled. Compose publishes
`OCU_PROXY_PORT:8082` and `OCU_OFFICE_PROXY_PORT:8083`; only the proxy publishes.

From the OCU checkout root, after configuring the environment:

```sh
python3 deploy/proxy/render.py
nginx -t -c "$(pwd)/deploy/proxy/nginx.conf"
```

Then, from the adjacent WebUI checkout root, with `nginx` on `PATH` and
`OCU_CHECKOUT` pointing at this OCU checkout:

```sh
./scripts/proxy-dev.sh
```

The existing launcher reads `deploy/proxy/nginx.conf`; it does not render it.
Do not edit the launcher or substitute a second config name. Stop the foreground
nginx process normally before changing the configuration. For native proof use
`hub start`/`hub stop` rather than background shell services.

`deploy/proxy/.gitignore` excludes the rendered config and `runtime/` before
first render. The renderer writes a 0600 candidate, checks it with native
`nginx -t`, then atomically replaces the previous config. It refuses a
symlink/public runtime directory or invalid table/input. `runtime/` and its
temp paths are absolute under this checkout, private (0700), and untracked;
access logging is off. The renderer suppresses `nginx -t` diagnostics on
failure; treat any diagnostics from a manual `nginx -t` as secret material.
Protect these files and mounts as secrets. No Docker, TLS terminator, overlay
network, firewall, permanent smoke/CI or WebUI auth implementation is supplied
here; overlay provisioning follows in issues 24–27 and issue 23 owns the pinned
permanent proxy smoke.

## Trust and route behavior

Chat routes authenticate through WebUI `/api/v1/ocu/auth` using only the session
Cookie and route-derived `X-Chat-Id`; static assets use session-only
`/api/v1/auths/` with Cookie. Auth subrequests disable inherited client headers
and do not forward Authorization, `x-api-key`, or any configured
`CUSTOM_API_KEY_HEADER`. Browser Authorization and identity headers cannot
authorize OCU. Every OCU-bound request gets the internal REST/WS Bearer token,
while WebSocket requests also retain the Cookie for OCU's periodic revocation
check.
Auth 401 stays 401, auth 403 becomes 404; mutation denial is independently
403 and OCU 403/409 are left intact. Only listed mutating rows require
`X-Requested-With: ocu-workspace` and exact `Origin` or
`Sec-Fetch-Site: same-origin`; `Origin: null` is always denied on those rows.
Browsers cannot set the custom header on WebSocket handshakes, so WS-only
routes require an Upgrade handshake and cookie owner authentication without
that mutation guard or an additional Origin check. SameSite cookie restrictions
protect the browser handshake. Active-document file responses get one forced
sandbox CSP and nosniff unless `download=1` is unambiguous; duplicate download
parameters preserve the query/disposition but do not disable the sandbox.
Binary/download policy otherwise remains upstream-owned. Upload size remains
OCU-owned rather than nginx's 1 MiB default.

`GET /ocu/api/uploads/{chat}/manifest` and `GET /ocu/api/uploads/{chat}/list`
return 404 without contacting OCU. `POST /ocu/api/uploads/{chat}/{path}`
forwards guarded uploads, including files whose names are `manifest` or `list`.

The reviewed inventory has 27 rows. Office broker routes under
`/ocu/api/office/{chat}/` use chat-owner authentication and strip `/ocu`;
the five POST rows also require the mutation guard. The session-status and
version-list GET rows do not require the custom header. `{file}` and `{session}`
each match one nonempty segment and require a `{chat}` parameter in the table.
Change the route table, reviewed row count and raw-byte SHA pin together.

`/ocu/office/source/{ticket}`, `/ocu/office/callback/{chat}/{session}` and
`GET /ocu/api/uploads/{chat}/imports` return 404 without contacting OCU.
Their unprefixed forms are not OCU gateway routes either. The proxy does not
implement the Office broker or expose its control-plane listener.

## DocumentServer listener

Every request and WebSocket handshake authenticates through WebUI `/api/v1/auths/`
using only the browser cookie. Auth401 remains401, auth403 denies403 and other
failures deny500 without contacting DocumentServer. The auth location is internal.
No path selects OCU or a WebUI application upstream on this listener.

DocumentServer receives the original method, body, path and query, not the
browser's Cookie, Authorization, internal token or chat/user identity headers.
Host and X-Forwarded-Host retain the browser-facing port; X-Forwarded-Proto and
X-Forwarded-For come from nginx. Client forwarding headers cannot override them.
The direct listener supplies its own HTTP scheme; a separate TLS front-end needs
an explicitly reviewed origin contract rather than trusting arbitrary headers.
WebSocket upgrade is preserved with3600-second read/send idle timeouts.

## Focused native proof and limits

`python3 -m unittest discover -s deploy/proxy/tests -p 'test_render.py' -v`
checks private, atomic, fail-loud rendering and visible-ASCII punctuation with
native `nginx -t`. `tests/fixture.py` supplies loopback non-echoing recording
WebUI-auth/OCU/DocumentServer HTTP+WS boundaries. Its JSONL observations contain headers,
including the synthetic Bearer token, and must be kept in a private 0700
location (record file mode 0600); they are never browser responses or stdout.
Start the fixture and rendered nginx with `hub start` (observe readiness), run
`OCU_TEST_RECORD=/private/path/requests.jsonl python3 -m unittest discover -s
deploy/proxy/tests -p 'test_native.py' -v`, then `hub stop` both processes.
The native tests assert per-row routing, no-contact denials, auth/status
provenance, headers, encoded paths, WS upgrades, upload size and MIME policy.

The existing WebUI stub reflects Authorization in `X-Echo-Authorization` on
responses. It cannot prove token-response containment and must not be hidden
with a stub-specific proxy workaround. A separate real WebUI harness plus that
stub launch through `scripts/proxy-dev.sh` is parent-owned; issue 23 fixes the
stub and adds permanent make smoke-proxy/CI against the frozen OCU commit.
This local fixture proves the proxy's own behavior only, not an actual user
session or deployment network isolation. nginx may reject syntactically invalid
percent escapes with 400 before config handling; such requests still never
contact OCU. Encoded filename percent (`%25`), space, hash, plus and non-segment
periods (`%2E`) are preserved; double-decode, separator and dot-segment
ambiguity is rejected.
