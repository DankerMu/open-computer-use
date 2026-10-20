## Summary
Brief description of changes.

## Intended issue closures

<!-- intended-closing-issues: [] -->

<!-- Keep exactly one marker. Its JSON array lists full owner/repo#number identities;
     [] explicitly means no issue closures. Reference issues as (#N); no closing keywords. -->

Merge only through the central closing-reference guard, run from the open-webui checkout:
`make -C <central-checkout> pr-merge-check PR_REPO=DankerMu/open-computer-use PR_NUMBER=<n> PR_HEAD=<full-reviewed-sha>`,
then `make -C <central-checkout> pr-merge` with the same inputs (it repeats the check).
Rules: the central `AGENTS.md` § Pull requests.

## Changes
- ...

## Testing
- [ ] `./tests/test-project-structure.sh` passes
- [ ] `./tests/test-docker-image.sh` passes (if Dockerfile changed)
- [ ] `docker compose up` works end-to-end

## ADRs reviewed (for `docs/future-architecture/` phase work)
- [ ] N/A — this PR is not phase-execution work, or
- [ ] List the ADRs re-read for this phase (e.g. `ADR-0002, ADR-0008, ADR-0010` for Phase 7 / 9 / 10 work).
