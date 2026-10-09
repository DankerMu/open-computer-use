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

`restore` requires `DOCKER_HOST=unix:///var/run/docker.sock` on a daemon identity different from the captured source, an absent destination root, and no colliding `ocu-test-*` / `owui-chat-*` / selected volumes. A different Compose project name is not isolation. Helpers, `up.sh`, and runtime socket consumers are pinned to that endpoint. Captured dotenv is parsed as inert assignments. Existing target files are not overwritten. `--retained-delivery` imports the selected release's verified images onto an empty image cache before helpers or isolated PostgreSQL run; conflicting tags are refused. Selected source and all seven image references, including `DOCUMENTSERVER_IMAGE`, replace captured release identity together. External chat/skills roots are refused rather than silently relocated. Isolated PostgreSQL occupancy and restore wait for the final TCP-authenticated server, not the official entrypoint's temporary Unix-socket initializer. Provider inspection uses the selected WebUI `config` table (`key`, `value`, `updated_at`) and does not rewrite persistent database credentials.

`activate` holds the same daemon-scoped recovery lock as backup/restore around import, compatibility, selected `up.sh`, and version-record generation. Compatibility with the restored schema is checked against the retained delivery before source, inventory, runtime, or version publication. It imports the requested retained delivery as one identity: source commit, `release.json`, and all seven images. An existing `source/.git` is verified against that delivery before any replacement; a different delivery or a tampered tree is refused and does not start. Occupied restored roots reconstruct the selected source from the delivery's Git bundle beside the destination and bind `source/deploy/up.sh` from that tree. Target provider defaults from the protected provider file enter the inert activation environment without rewriting restored database settings or appearing on argv. Consumer-contract success is not claimed from Compose start alone.

Every release inventory loader requires `format_version` 2. Restore and activation
refuse a retained version-1 inventory before image import, selected-release
publication or startup. The recovery-set format number is independent and remains 1.

Backup compares Docker's actual `Image` configuration digest with the inventory, independently of the launch reference in `Config.Image`. Sandbox attribution requires the producer's canonical name, required labels, and exact volume/bind mount types, paths, destinations, and access modes. Every discovered sandbox is checked before stopping any writer, then checked again after admission shutdown.

An interrupted activation can resume only its recorded selected-source publication. A private receipt binds the target directory identity, complete retained inventory, selected-source location, and random ownership token; the imported tree carries the matching marker from before publication. Pre-existing unowned directories, symlinks, and altered inventories are refused without overwriting the restored target. Keep these ownership records with the restored target when retrying the same delivery.

An existing checkout without this publication's ownership receipt is not adopted, even if its commit and image references match. Retry requires the original complete delivery inventory, not merely matching tags.

Before creating a selection receipt, activation verifies the complete retained source bundle, reconstructed commit, tracked tree, and supported consumer contract. Refusal during this preflight leaves no new receipt, so another valid delivery can be selected on the same restored target. Import repeats source validation before publication; a later import failure retains the receipt for retry of that same delivery.

Backup requires broker chat/skills bind mounts and their four data-root environment assignments to match the runtime capture roots, even with no sandboxes. WebUI, initializer, and PostgreSQL data mounts must match their named volumes. Missing, duplicate, read-only, or wrongly typed required mounts are refused before writers stop.

Schema compatibility follows Alembic `down_revision` edges from selected heads, including merge parents. Equal heads and recognized ancestors are accepted; unknown/unrelated revisions, unknown parents, and cyclic graphs are refused. This check does not run migrations or alter the restored schema.

Retry evidence covers handled exceptions and completed publication boundaries. SIGKILL during import and host/power-loss durability are not established; do not remove reservations or foreign resources by name to force recovery.



## Failure recovery

- Backup failure never publishes `recovery.json` and does not delete unrelated resources.
- Restore failure leaves owned partial volumes/containers/files without `.restored` or application start. Discard that disposable target or choose a new empty one.
- Activation failure may leave partial stopped or running core/WebUI/proxy resources. Do not `down -v` or delete sandboxes. An incompatible retained delivery is refused before selected source/inventory/runtime publication, so the restored target can retry its originally compatible delivery. Re-run `activate` only after naming the incompatibility; do not downgrade the database.


## Readiness and cutover

`compose up -d` is not readiness. After activation:

1. Run `deploy/smoke.sh` and `deploy/check-ports.sh` from the selected release source with the restored private environment. Only the proxy may publish.
2. Confirm restored chat owners, outputs, file IDs, revisions, and stopped sandbox launch behavior.
3. Inspect effective provider settings in the WebUI admin UI. Persistent database credentials keep application precedence over the target provider file; change them through the normal administrator path if needed.
4. Switch public traffic only after those checks pass.

The captured `CAPTURED_VERSION.md` is backup provenance. The active version record is regenerated from the selected release after activation.
