# Cold backup, isolated restore, and previous-release activation

Tracked recovery entrypoint: `deploy/recovery.py`. Untracked local sketches remain operator notes and are not this procedure. Real Docker engine, PostgreSQL restore semantics, archive metadata fidelity, and A-T14/A-T15 evidence remain issue36.

The complete recovery set is secret-bearing. Run as root. Backup directories are mode 0700; metadata and captured configuration are mode 0600. Do not put credentials on argv, in logs, or in the secret-free image delivery bundle. Only the separately provisioned provider credential *file* is excluded; values already persisted in the database remain in the dump.

## Commands

```text
python3 deploy/recovery.py backup --deploy-root <DEPLOY_ROOT> --destination <new-empty-backup-dir>
python3 deploy/recovery.py verify --recovery-set <backup-dir>
python3 deploy/recovery.py restore --recovery-set <backup-dir> --destination-root <new-empty-target-root> --provider-file <0600-provider-env> --retained-delivery <previous-release-delivery>
python3 deploy/recovery.py activate --destination-root <restored-root> --retained-delivery <previous-release-delivery>
```

`backup` inspects actual stack images, Compose identity and sandbox mounts against the selected runtime/release before stopping anything. It then stops only attributed application, initializer, retention, and sandbox writers, re-enumerates after admission shutdown, dumps PostgreSQL read-only, and publishes one complete directory: logical dump, WebUI data including a root regular-file `.computer-use-initialized`, chat/skills trees, every existing `chat-<id>-workspace` volume (including detached volumes and deployments with zero workspace volumes), protected runtime/admin files, and the captured `release.json` plus version record. Image archives stay in the retained delivery and are referenced by inventory identity. Writers stay stopped; sandboxes are not started. Unrelated managed-looking sandboxes are refused without being stopped.

`restore` requires `DOCKER_HOST=unix:///var/run/docker.sock` on a daemon identity different from the captured source, an absent destination root, and no colliding `ocu-test-*` / `owui-chat-*` / selected volumes. A different Compose project name is not isolation. Helpers, `up.sh`, and runtime socket consumers are pinned to that endpoint. Captured dotenv is parsed as inert assignments. Existing target files are not overwritten. `--retained-delivery` imports the selected release's verified images onto an empty image cache before helpers or isolated PostgreSQL run; conflicting tags are refused. Selected source and all six image references replace captured release identity together. External chat/skills roots are refused rather than silently relocated. Isolated PostgreSQL uses a uniquely owned temporary container, waits for the final server, streams dump/restore through a private file, then stops and removes only that owned container while preserving the restored volume. First recovered broker listing treats a missing index as revision zero when the restored cursor is zero; a leading cursor still fails. Partial failure reports owned resource identities and refuses retry into the populated target.

`activate` holds the same daemon-scoped recovery lock as backup/restore around import, compatibility, selected `up.sh`, and version-record generation. It imports the requested retained delivery as one identity: source commit, `release.json`, and all six images. An existing `source/.git` is verified against that delivery before any replacement; a different delivery or a tampered tree is refused and does not start. Occupied restored roots reconstruct the selected source from the delivery's Git bundle beside the destination and bind `source/deploy/up.sh` from that tree. Target provider defaults from the protected provider file enter the inert activation environment without rewriting restored database settings or appearing on argv. Consumer-contract success is not compatibility. Additive `ocu_chat_state` is kept. The selected `write-deployed-version.sh` must exist and succeed; a missing or failed version record is not activation success. Recovery does not launch sandboxes; explicit authorized launch recreates a stopped workspace from the selected image and recovered volume.


## Failure recovery

- Backup failure never publishes `recovery.json` and does not delete unrelated resources.
- Restore failure leaves owned partial volumes/containers/files without `.restored` or application start. Discard that disposable target or choose a new empty one.
- Activation failure may leave partial stopped or running core/WebUI/proxy resources. Do not `down -v` or delete sandboxes. Re-run `activate` only after naming the incompatibility; do not downgrade the database.

## Readiness and cutover

`compose up -d` is not readiness. After activation:

1. Run `deploy/smoke.sh` and `deploy/check-ports.sh` from the selected release source with the restored private environment. Only the proxy may publish.
2. Confirm restored chat owners, outputs, file IDs, revisions, and stopped sandbox launch behavior.
3. Inspect effective provider settings in the WebUI admin UI. Persistent database credentials keep application precedence over the target provider file; change them through the normal administrator path if needed.
4. Switch public traffic only after those checks pass.

The captured `CAPTURED_VERSION.md` is backup provenance. The active version record is regenerated from the selected release after activation.
