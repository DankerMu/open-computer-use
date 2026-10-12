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
