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

### Docker-manager test data ownership

`orchestrator/test_docker_manager.py` owns a private temporary data root across
imports, reloads and real metadata writes under standalone and pytest execution.
No caller-provided `BASE_DATA_DIR` is required. Cleanup restores the prior
environment and cached data root independently and removes only the fixture-owned
directory, including after setup or test-body failures.

For pytest, use the canonical discovery command above with
`-k 'test_docker_manager.py'` to select this module rather than the full unit scope.

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
