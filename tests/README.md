# Tests

Validation scripts for the project. Run before submitting PRs.

## Running Tests

```bash
# All tests (no Docker required for first two)
./tests/test-no-corporate.sh
./tests/test-project-structure.sh

# Requires built Docker image
./tests/test-docker-image.sh [image-name]
```

Default image: `open-computer-use:latest`.

### Python selected unit tests

Run from the repository root. For mixed root/orchestrator selections, use one
`tests/` discovery root and select modules with `-k`:

```bash
uv run --no-project --with pytest \
  --with-requirements computer-use-server/requirements.txt -- \
  python -m pytest tests/ -q \
  --import-mode=importlib --ignore=tests/integration \
  -k 'test_office_control_plane.py or test_office_router.py or test_office_versions.py or test_auth_guard.py or test_lifecycle.py or test_office_sessions.py or test_office_session_lifecycle.py or test_office_save_close.py or test_office_notice.py or test_office_sweep.py or test_office_store.py or test_office_tokens.py or test_office_config.py or test_outputs_endpoint.py or test_preview_prefix.py or test_server_package.py'
```

This sixteen-module example is a selected scope, not all repository suites or
all Office coverage. Remove `-k` and its expression for the full unit scope under
`tests/`; explicitly choose all relevant modules for another issue.
`--import-mode=importlib` avoids shared test-file basename conflicts.
`--ignore=tests/integration` excludes integration tests requiring Docker.

Mixed explicit-path selections can create distinct collector objects for the
same logical directory with pytest 9.1.1. Correct the invocation; do not patch
the `world` fixture or `conftest.py`, change dependencies, or alter source
algorithms to mask it.

Before changing selection syntax, use `--collect-only` with each form at the
same source revision and compare exact collected node-ID sets, not counts.
Collection success is not a test pass: then execute the same canonical selected
scope without `--collect-only` and record execution separately. Do not trim an
evolving corpus to match an old count.

### Output fixture prefix isolation

`orchestrator/test_outputs_endpoint.py::_isolated_app` removes inherited
`OCU_PUBLIC_PREFIX` before app import when `prefix=None`, including invalid
inherited values. Explicit `""` selects root routes while keeping the variable
present; explicit `/ocu` and the omitted/default `/ocu` select prefixed public URLs.

`orchestrator/test_outputs_prefix.py` exercises real static content, preview
configuration, broker output listings and exact file bytes. Nested real app
contexts verify full environment contents and selected module keys/object
identities after normal and intentional-assertion exits; the original assertion
remains visible. Close clients before fixture restoration. App-import fixtures
mutate process-global state and must not overlap within one process.

Run the fixture and affected consumer scope from the repository root:

```bash
uv run --no-project --with pytest \
  --with-requirements computer-use-server/requirements.txt -- \
  python -m pytest tests/ -q \
  --import-mode=importlib --ignore=tests/integration \
  -k 'test_outputs_endpoint.py or test_outputs_prefix.py or test_preview_prefix.py or test_office_router.py or test_office_publish_recovery.py or test_path_traversal_app.py'
```

This local selected scope does not establish deep restoration of module internals,
real Docker or LAN behavior, or full repository coverage. The path-traversal
consumer retains its explicit empty-prefix configuration and security assertions.

### File-route fixture module ownership

`test_file_response.py::workspace` and `test_files_headers.py::app_module` share
`file_route_module_ownership.py::isolated_file_route_modules` for their temporary
app-import cache family, including the coupled `uploads` module. Receipt producers
and the app share the active descriptor exception class. Cleanup retires replacement
modules and restores exact entry objects or entry absence after normal completion,
test-body failure, or setup failure after eviction.

`test_file_route_module_ownership.py` covers both owners with present and absent
entry modules. Restoration assertions run after actual pytest fixture teardown and
before emergency cleanup. The workspace coherence regression checks producer/app
exception identity inside the live fixture; `test_upload_claim.py` retains its
nominal receipt exception and committed-write assertions.

### Main CI root-module coverage

The main build workflow's non-image `pytest-orchestrator` job runs both complete
`tests/test_upload_claim.py` and `tests/test_tools.py` modules in one pytest
invocation, separate from orchestrator collection. A failure in either module
fails the invocation and job. Existing orchestrator, auth-guard, deploy and
integration selections remain unchanged.

The orchestrator selection runs from the checkout root via
`python -m pytest tests/orchestrator/ -v`, making the repository namespace
available to package-qualified fixture imports without `PYTHONPATH` or an
import-mode override.

### Docker-manager test data ownership

`orchestrator/test_docker_manager.py` owns a private temporary data root across
imports, reloads and real metadata writes under standalone and pytest execution.
No caller-provided `BASE_DATA_DIR` is required. Cleanup restores the prior
environment and cached data root independently and removes only the fixture-owned
directory, including after setup or test-body failures.

For pytest, use the canonical discovery command above with
`-k 'test_docker_manager.py'` to select this module rather than the full unit scope.

### Recovery database environment ownership

`deploy/test_recovery_db.py` scopes its fake Docker environment to each unittest
lifecycle. Registered cleanups restore the complete entry environment and remove
the owned temporary directory after success, body errors and setup errors.
Later subprocesses inherit the caller's Docker environment, not the deploy fake.

The same module tests all three outcomes through the unittest runner. Full-root
real-Docker verification remains distinct from this isolated lifecycle coverage.

### CLI-runtime module ownership

`orchestrator/test_cli_runtime.py` owns temporary cache entries for `cli_runtime`,
`docker_manager` and `outputs_broker`. Each fixture lifecycle permits fresh imports
and restores entry module objects or entry absence, including after a test-body
failure. Downstream consumers retain their caller-owned manager references.
The isolation regression and CLI-predecessor-to-sweep sequence exercise this
boundary without replacing production admission or weakening the canary oracle.

### Office contention startup ownership

The owners in `orchestrator/test_office_control_plane_concurrency.py` release and
observe acquired holders and restore temporary lock patches before `office_world`
teardown, including controlled startup/readiness failures. The regressions in
`orchestrator/test_office_contention_startup.py` invoke those actual owners with
real acquired child/thread holders. They require the injected exception itself
at top level; emergency rescue cannot satisfy the owner-cleanup assertions.

Run the owning scope using the same discovery root:

```bash
uv run --no-project --with pytest \
  --with-requirements computer-use-server/requirements.txt -- \
  python -m pytest tests/ -q \
  --import-mode=importlib --ignore=tests/integration \
  -k 'test_office_contention_startup.py or test_office_control_plane_concurrency.py or test_office_control_plane.py'
```

This scope covers controlled startup faults and the successful contention paths,
not request cancellation, arbitrary simultaneous cleanup failures, deployment
behavior, or cross-platform leak freedom. Production locks and callbacks are unchanged.

### Office crash-cut marker readiness

Publication, copy, retirement and resolution crash workers share the embedded
marker writer in `orchestrator/_office_store.py`. Each writes the complete cut
name to an exclusively owned, private (`0600`) sibling stage, syncs and closes
it, then atomically publishes the final marker with saved filesystem primitives.
Final-path existence therefore signals complete exact text, not a write still
in progress; successful publication leaves no stage. Marker bookkeeping bypasses
the production replacement/write/sync hooks without advancing the selected cut.
Existing child holds, exact-text checks, SIGKILL checks and recovery assertions
remain unchanged. The ordinary process holder in
`orchestrator/test_office_control_plane_concurrency.py` publishes its
`callback-process-entered` readiness marker through the same writer, so the
startup-failure owner's exact `held` read never sees incomplete text. The other
contention markers (`callback-process-contended` and the thread holder) and the
separate pause/read oracle are outside this protocol.

`orchestrator/test_office_crash_markers.py` holds real filesystem writes before
text is complete, exercises short writes and publication failures, and checks
saved-hook isolation plus private-mode/sync/close ordering. It also holds the
real process holder at its marker open; lock-file and descriptor opens pass
through the hold filter. Its behavioral mutation seam is `_publish_marker` in the
shared embedded source: restoring direct final-path writes must expose premature
readiness, not an import failure. Run this module together with the complete
`test_office_publish_recovery.py`, `test_office_save_as.py`,
`test_office_resolution.py`, `test_office_store.py`,
`test_office_contention_startup.py` and `test_office_control_plane_concurrency.py`
modules; do not exclude the independent pause/read case if it fails.

### Office workspace read observers

The resolution, restore, publish-fence and notice tests bind each read observer
to the identity from `fstat` of an owned read-only file descriptor. The descriptor
stays open throughout the observed operation, including workspace replacement or
unlink, and the read hook is removed before the descriptor closes on success or
failure. Notice counters own this scope explicitly through
`_count_workspace_reads`; repeated status windows and counter resets share the
same held resource.


### Office command deadlines and transaction-worker ownership

Run the focused command protocol and ownership scope with the canonical discovery root:

```bash
uv run --no-project --with pytest \
  --with-requirements computer-use-server/requirements.txt -- \
  python -m pytest tests/ -q \
  --import-mode=importlib --ignore=tests/integration \
  -k 'test_office_commands.py or test_office_command_deadline.py or test_office_transaction_workers.py or test_office_lookup_admission.py or test_office_save_close.py'
```

The regressions cover:

- Real held threaded DNS for join/reopen, close, save and the Office poll. Timing
  starts before caller submission with the unchanged 10-second command budget
  and a 150 ms test scheduling allowance. Lookup and force-save wait outside
  the canonical lock; caller completion must precede resolver release/return.
- Healthy real DNS with one default-executor worker, including history restore's
  known-key `409 session_open` admission and unchanged persisted bytes.
- Authenticated command-induced callbacks with one available AnyIO worker finish
  before lookup/force-save responses; lookup reconciliation revalidates session,
  key, epoch and allocation authority after concurrent state changes.
- Cancelled queued transactions issue no command or allocation; cancelled running
  requests retain ownership through outcome-specific reconciliation.
- Fresh request-context isolation through workers and loop submission; lifespan
  failures before/after supervisor acquisition, publication and shutdown; draining
  real workers through repeated asyncio and AnyIO cancellation while both shared
  worker domains remain occupied.
- Same-process and cross-process duplicate closes preserve one reservation across
  induced callbacks; a genuine later participant report can still void it.
- Observation-gate waiters leave callbacks and other chats independent. Unsafe
  or replaced roots/lock inodes refuse admission; cleanup and process death release
  ownership without unlinking the stable coordination file.

Commands execute on the service loop. Synchronous save, join/reopen, close,
restore and sweep transactions share one lifespan-owned executor independent of
DNS and callback workers; cleanup drains admitted work before releasing dependencies.

This selected scope is not all Office or deployment coverage. Runtime fault
probes must record the selected event-loop backend and observe actual resolver
entry; a healthy auto-selected backend run does not prove held-threaded-DNS
behavior. The scheduling allowance is not a production timeout or SLA, and
worker draining supplies neither forced thread termination nor a shutdown deadline.

### HTTP test-origin ownership

`orchestrator/_http_origin.py` owns the Office origins, tool redirect origin
(`test_tools.py`), font archive origin (`deploy/support.py`), deployment smoke
fixture and control listeners (`deploy/test_deployment_smoke.py`), initializer
fake WebUI (`deploy/test_init_wrapper.py`) and native HTTP/TTY fixtures
(`deploy/test_smoke_native.py`).

Owner exit retires admitted workers and accepted/listening sockets within a finite
per-origin bound, or reports teardown failure. Register each acquired owner's
cleanup immediately, including reservation sockets before bind and temporary
directories before subsequent setup. Cleanup failure must not bypass other owners.
Controlled holds observe cancellation; initializer cancellation preserves
caller-owned marker files. Handler and body failures remain visible together.
Expected disconnects are classified only at actual transport operations, including
socket timeout configuration, not by swallowing responder exceptions.
The runtime TTY timeout regression joins the admitted worker within the running
owner's lifetime so error classification and recording finish before owner exit.
It verifies real EBADF propagation; the shutdown-time contrast treats EBADF as an
expected disconnect.

Run the shared lifetime and affected consumer scope from the repository root:

```bash
uv run --python 3.12 --no-project --with pytest \
  --with-requirements computer-use-server/requirements.txt -- \
  python -m pytest tests/ -q \
  --import-mode=importlib --ignore=tests/integration \
  -k 'test_office_commands.py or test_office_router.py or test_office_callback_processing.py or test_office_download.py or test_http_origin.py or test_office_http_holders.py or test_tools.py or test_http_origin_consumers.py or test_deployment_smoke.py or test_init_wrapper.py or test_smoke_native.py or test_prepare_fonts.py or test_offline_release.py'
```

Use the same Python dependencies for deploy unittest discovery and direct-script
entrypoints. The deployment control cases require free loopback ports 8081/8080/8082;
do not stop unrelated services to obtain them. Retirement assertions observe real
resources before emergency client closure, marker removal or worker joins.
Local verification uses Python 3.12 on Darwin/arm64 with loopback fixtures, not real
Docker or LAN deployments. This selected scope does not establish Linux/amd64
behavior, full repository coverage or forced termination of uncooperative threads.

## Test Scripts

| Script | What it validates | Docker required |
|--------|-------------------|-----------------|
| `test-no-corporate.sh` | No corporate/internal references leak into codebase | No |
| `test-project-structure.sh` | Correct directory structure (12 checks) | No |
| `test-docker-image.sh` | Docker image contents: npm packages, CLI tools, Python packages, Playwright, volume size, permissions | Yes |

## test-no-corporate.sh

Scans all project files against patterns in `corporate-patterns.txt`. Catches internal domains, hardcoded API keys, registry URLs, and service accounts that shouldn't be in open-source code.

## test-project-structure.sh

Verifies the expected project layout: `computer-use-server/`, `openwebui/`, required files, no legacy directories, docker-compose services, `.env.example` variables.

## test-docker-image.sh

Runs inside the Docker image to verify:
- Node.js CommonJS `require()` and ESM `import` work
- CLI tools available: `mmdc`, `tsc`, `tsx`, `claude`
- Python packages: `docx`, `pptx`, `openpyxl`, `playwright`
- npm package layout (`/home/node_modules/` vs `/home/assistant/`)
- Volume size (`/home/assistant/` < 1MB — packages should be outside)
- File permissions and guard files
