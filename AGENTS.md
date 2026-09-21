# AGENTS.md

> Agent operating rules for Plan 1 work in this fork (`DankerMu/open-computer-use`).
> The engineering control plane for Plan 1 lives in the sibling checkout
> `../open-webui-ocu` (fork `DankerMu/open-webui`): its `AGENTS.md` is the full
> rulebook, its epic `DankerMu/open-webui#2` is the schedule, and its acceptance
> matrix is the definition of done. This file is the bridge: what carries over
> unchanged, what is enforced here, and what is review-only here.
> The upstream `CLAUDE.md` global rules (English only, SPDX headers, `linux/amd64`
> builds) still apply to every file in this repository.

## Scope

- Work here is driven by the `[ocu]` and `[deploy]` sub-issues of epic
  `DankerMu/open-webui#2` (OpenSpec change `ocu-workspace-integration`, design
  decision D2). The issue body is the contract; do not widen it.
- Branch per issue: `codex/plan1-<slug>` off `main` at `7318b2e`. One issue, one
  branch, one PR to this fork's `main` (protected: PR required, no force-push).
- Files Plan 1 touches here: `computer-use-server/{app.py,docker_manager.py,mcp_tools.py}`,
  `computer-use-server/static/{preview.js,browser-viewer.js}`, new modules
  `computer-use-server/{auth_guard.py,outputs_broker.py}`, `openwebui/tools/computer_use_tools.py`,
  `openwebui/functions/computer_link_filter.py`, and the `deploy/` overlay. The
  untracked `deploy/` and `docs/decisions/` trees are kept as they are.
- Done means: the issue's acceptance criteria pass with fresh command output in the
  PR, a reviewer pass is attached, and the epic checkbox in `DankerMu/open-webui#2`
  is ticked. Cross-repo verification (proxy smoke, e2e) runs from `../open-webui-ocu`.

## Commands

| Task | Command |
| --- | --- |
| Unit tests (no Docker) | `uv run --no-project --with pytest --with-requirements computer-use-server/requirements.txt -- python -m pytest tests/ -q --import-mode=importlib --ignore=tests/integration` |
| Integration tests (Docker daemon required) | same command without `--ignore=tests/integration` |
| Structure check | `./tests/test-project-structure.sh` (`test-no-corporate.sh` named in `tests/README.md` does not exist at `7318b2e`) |
| Docker image (always amd64) | `docker build --platform linux/amd64 -t open-computer-use:latest .` |
| Install commit hooks (once per clone) | `uvx pre-commit install --hook-type pre-commit --hook-type commit-msg` |

Baseline on `7318b2e`: the unit command passes; `tests/integration/` needs a Docker
daemon; `--import-mode=importlib` is required because `tests/integration` and
`tests/orchestrator` share test-file basenames. New tests go in `tests/` next to
the existing ones (`tests/test_<module>.py`), paired with their production file in
the same PR (TDD rule below).

## Rules carried over from `../open-webui-ocu/AGENTS.md`

- **Code canonicality**: one implementation per behaviour; no `_v2`/`_new`/`_old`/
  `_backup`/`_temp`/`_copy`/`_final`/`_fixed`/`_legacy`/`_deprecated` names; no
  `tmp/`, `scratch/`, `backup/`, `archive/`, `wip/` directories; no commented-out
  code; refactor in place, git is the safety net.
- **TDD**: every new production file ships with its test file in the same change;
  write the failing test first. Mock dependencies (Docker, WebUI), never the system
  under test.
- **Minimal diff**: touch only what the issue names; no drive-by refactors of
  upstream code; the fork must stay rebase-able onto upstream OCU.
- **Conventional commits**: `<type>(<scope>): <subject>` with types
  `feat fix refactor chore docs test ci build perf` and scopes `ocu` or `deploy`;
  subject under 72 characters. Agent commits add `Co-Authored-By`.
- **PR size**: at most 400 changed lines excluding lockfiles and generated files,
  or a written justification in the PR.
- **Evidence**: a success claim needs the command, its exit code and the relevant
  output from the current session. Verify the world, not the self-report.
- **Forbidden**: force-push, `--no-verify`, dependency upgrades without explicit
  confirmation, deploying to the LAN instance, reading or printing `.env` files,
  pasting secret values anywhere (reference secrets by variable name only).
- **Decisions**: a non-trivial design choice not already covered by design D1–D19
  in `../open-webui-ocu/openspec/changes/ocu-workspace-integration/design.md`
  gets a decision record in `../open-webui-ocu/docs/decisions/` in the same
  milestone, not a silent deviation.
- **Iteration gate**: at most 3 fix-and-recheck cycles per failing check, then stop
  and report; never weaken, skip or delete a test to get green.

## Enforcement here

| Rule | Where it is checked | Level |
| --- | --- | --- |
| Forbidden naming / scratchpad dirs | `.git-hooks/check-naming.sh` via pre-commit | block |
| Conventional commits | `.git-hooks/commit-msg` via pre-commit `commit-msg` stage | block |
| Secrets, private keys, files > 500 KB, merge markers | `.pre-commit-config.yaml` (gitleaks, pre-commit-hooks) | block |
| Unit tests | the unit command above, run by the agent and pasted into the PR | review-only |
| Coverage, duplicate/dead code, complexity, PR diff size | not wired in this repo | review-only |
| Reviewer cross-review of the diff | reviewer subagent, attached to the PR | review-only |

Anything marked review-only is a promise the agent keeps and the PR shows, not a
machine gate. `../open-webui-ocu/AGENTS.md § Enforcement Index` is the reference for
what a fully gated repository looks like; this repository is deliberately lighter.
