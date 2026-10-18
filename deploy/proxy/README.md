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
and do not forward Authorization, API keys or client identity. Browser
Authorization and identity headers cannot authorize OCU. Every OCU-bound request
gets the internal REST/WS Bearer token; session-authenticated WebSockets retain
Cookie for OCU's periodic revocation check. Non-grant OCU rows clear grant,
resource-target and auth-mode headers without changing their authority.

Auth401 stays401, auth403 becomes404; mutation denial is independently403 and
authorized OCU403/409 remain upstream statuses. Only listed mutating rows require
`X-Requested-With: ocu-workspace` and exact `Origin` or
`Sec-Fetch-Site: same-origin`; `Origin: null` is always denied on those rows.
WebSocket rows require an Upgrade handshake and cookie owner authentication,
without the mutation guard. Upload size remains OCU-owned.

### Ordinary files and resource grants

`GET/HEAD /ocu/files/{chat}/{path}` proxies non-download content to WebUI
`/api/v1/ocu/resource-grants/issue`, not an authorization subrequest or a local
rewrite redirect. Only Cookie and route-derived chat, encoded relative path and
original query are issuer inputs. The issuer authorizes before returning an
empty302 with a relative `/ocu/f/{grant}/{chat}/{path}` Location and preserved
query. Issuer401 remains401, issuer403 maps to entry404, and infrastructure or
transport failure never falls back to direct file access.

One unambiguous literal lowercase `download=1` selects a separate internal
download location that performs chat-session authorization before forwarding.
The existing raw query classifier is unchanged: encoded keys, duplicate or
case-variant download keys cannot enable the exemption. Query bytes and final
upstream disposition remain intact. ArchiveGET stays session-authorized;
archiveHEAD is not enabled.

The exact `GET/HEAD /ocu/f/{grant}/{chat}/{path}` row uses only its route-derived
two-component grant, chat, encoded path and original method in a bodyless GET
subrequest to `/api/v1/ocu/resource-grants/auth`. It does not send browser Cookie,
Authorization, identity, auth-mode or Referer. Verifier401 stays401; verifier403
maps404; verifier503 and transport errors fail closed as nginx auth-request500.
Authorized OCU403/409 remain distinct from verifier refusals.

Grant forwarding uses only `/files/{chat}/{encoded-path}` plus the original
query, the internal Bearer credential, fresh verifier identity and explicitly
allowed ordinary-file transport headers. Cookie, grant, browser identity,
resource headers, Origin and capability-bearing Referer cannot reach OCU.
Unambiguous downloads on grant URLs, unsupported methods, ambiguous raw/decoded
chat or grant, empty/trailing/dot path segments, encoded separators, double
decoding, controls, backslashes and exact archive aliases are refused before
upstream dispatch. Ordinary filenames resembling control routes remain
reader-owned; this gateway does not infer filesystem directory resolution.

Canonical redirects and every grant response have no-store/no-referrer.
Grant responses remove upstream Set-Cookie and CORS policy; only verified grants
with case-sensitive exact `Origin: null` receive credential-free ACAO:null.
Upstream Vary values are retained, adding Origin only when needed. OPTIONS has
no CORS or upstream contact. Active-document MIME types (case/parameter variants
of HTML, SVG, XHTML and XML), including authorized upstream errors, receive one
`sandbox allow-scripts allow-forms` CSP and nosniff. Binary and canonical download
policies remain upstream-owned. Grant content, verifier and named denial
locations suppress raw-URI nginx error logs; unrelated errors remain observable
and access logging remains off.

`GET /ocu/api/uploads/{chat}/manifest` and `GET /ocu/api/uploads/{chat}/list`
return 404 without contacting OCU. `POST /ocu/api/uploads/{chat}/{path}`
forwards guarded uploads, including files whose names are `manifest` or `list`.

The reviewed inventory has 28 rows. Office broker routes under
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
The native tests assert per-row routing, no-contact denials, issuer/verifier
status and route-input provenance, encoded targets/queries, raw-wire HEAD
metadata/body behavior, credential containment, exact null-origin CORS, MIME
policy, grant-failure log containment with a nonsecret logging positive control,
WS upgrades, upload size and unchanged Office/DocumentServer authority. The
private nginx error log is expected at `runtime/error.log` beside the JSONL
record's parent directory. Transport-drop and malformed-response hooks exist
only in the loopback fixture.

The WebUI stub intentionally reflects Authorization in `X-Echo-Authorization`
only on `/internal/*` observation routes; public fixture responses do not echo
Authorization. The stub alone is not end-to-end token-containment proof, and
a stub-specific proxy workaround must not conceal a leak. The integrated
real-WebUI/proxy/stub path is delivered as WebUI's permanent `make smoke-proxy`
target and is wired into its CI against the pinned OCU checkout. It checks
response token containment and rejects public `X-Echo-Authorization` headers.
The issuer/verifier fixture uses explicitly synthetic two-component grants,
not signing, expiry, revocation storage or actual WebUI user sessions. It proves
the gateway's interface and authority separation, not real issuance, confined
file-reader behavior, browser integration or deployment network isolation.
Real-auth pin/integration belongs to the paired WebUI smoke. nginx may reject
syntactically invalid percent escapes or encoded NUL with400 before configured
location handling; such requests still never
contact OCU. Encoded filename percent (`%25`), space, hash, plus and non-segment
periods (`%2E`) are preserved; double-decode, separator and dot-segment
ambiguity is rejected.
