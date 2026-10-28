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

To point a deployment that was not restored at a newly imported release, use `recovery.py retarget`; see [Upgrading an existing deployment](production-like-test/NETWORK-HARDENING.md#upgrading-an-existing-deployment).

`backup` inspects actual stack images, Compose identity and sandbox mounts against
the selected runtime/release before stopping anything. OCU and DocumentServer must
still be running when maintenance begins; do not stop them before invoking backup.

It then scans, read-only and with each tree's link policy (below), every tree it will
capture: the chat and skills directories on the host, and the WebUI data volume and
each deployment workspace volume through the workspace helper image with a read-only
mount. The scan applies the capture refusals: socket, FIFO or device members, too
many members or bytes, a symlink the policy refuses, and a second name inside the
tree for one symlink. It also requires the WebUI initializer marker and refuses an
unattributable workspace volume. Any refusal fails the backup with every service
still running: no container is stopped, DocumentServer gets no shutdown command, and
no stage is created. The host scan opens each directory relative to its parent and
never follows a link; an entry deleted, or a directory replaced, while the running
tree is scanned is skipped. The capture after quiesce repeats every check. A tree
that changes between the scan and the stop, Python-fallback extended-attribute or
ACL refusals, and failures the scan cannot predict, such as a full disk, still fail
after the stop and leave the stack stopped, as described below.

1. Stop the proxy to close browser admission to WebUI, OCU and DocumentServer,
   then the Messages ingress to close sandbox admission to WebUI.
2. Run `/usr/bin/documentserver-prepare4shutdown.sh` inside DocumentServer while
   OCU remains running to persist and publish final callbacks over the control plane.
3. Read each chat's Office state until no session is `opening`, `editing`, `saving`
   or `closing` and no journal entry remains. This wait makes no OCU HTTP request.
4. Stop and verify the remaining application, initializer, retention and sandbox
   writers, including DocumentServer. Recheck Office state before capture;
   PostgreSQL remains available for the read-only dump.

The process environment setting `OCU_OFFICE_BACKUP_TIMEOUT_SECONDS` defaults to900.
It must be finite and strictly greater than the broker's600-second liveness
interval. The shutdown command and the subsequent drain each get this bound;
it is not a total backup deadline. Command failure or drain timeout names the
persisted blockers and attempts all safely attributed writer stops. A stop failure
is reported, never treated as quiescence. No failed run publishes a complete set.
There is no automatic restart or shutdown-mode reset: the next normal deployment
start clears DocumentServer's shutdown mode and restarts the proxy and the
Messages ingress.

The complete directory contains the logical dump, WebUI data with its root
regular-file `.computer-use-initialized`, chat/skills trees, every existing
`chat-<id>-workspace` volume (including detached volumes and deployments with none),
protected runtime/admin files, and captured `release.json` plus version record.
Each chat includes `outputs/`, broker/lifecycle state and `.ocu/office/` state and
version blobs; no `uploads/` tree is required. DocumentServer data/cache/log volumes
are not recovery components. Image archives stay in the retained delivery and are
referenced by inventory identity. Writers stay stopped; sandboxes are not started.
Unrelated managed-looking sandboxes are refused without being stopped.

Each captured tree has a link policy, fixed by its recovery component:

- **Deployment-owned trees** (the WebUI data volume and `skills-cache`) are
  `confined`. They keep relative symbolic links that stay inside the captured root,
  such as the HuggingFace cache links `snapshots/<rev>/<file> -> ../../blobs/<sha>`
  in WebUI data, with their targets unchanged. A link with an empty or absolute
  target, a `..` after a normal component, or more leading `..` components than its
  directory depth below the root fails capture, verification and restore with
  `unsafe symlink`.
- **Model-written trees** (every `chat-<id>-workspace` volume and the chat data
  tree, including `outputs/`) are `verbatim`. Any link with a non-empty target is
  captured and restored with its target unchanged, including absolute and escaping
  links such as the sandbox entrypoint's `.claude/agents/<name>.md -> /opt/...` and
  `.claude/skills/<name> -> /mnt/skills/public/<name>/`, or a venv's
  `bin/python3 -> /usr/bin/python3`. Links are never followed.

Under both policies a member below a link, a hardlink to a link or to a path
outside the archive, and device files are refused, and no restore step reads,
writes, changes the mode of or changes the owner of a link's target. The archive
does not record the policy. Verify and restore a set with recovery tooling that has
these rules: older `deploy/recovery.py` refuses absolute or `..` links with
`unsafe symlink`, and the volume helper mounts the running checkout's `deploy/`, so
run verify and restore from a checkout that includes them.

`restore` requires `DOCKER_HOST=unix:///var/run/docker.sock` on a daemon identity different from the captured source, an absent destination root, and no colliding `ocu-test-*` / `owui-chat-*` / selected volumes. A different Compose project name is not isolation. Helpers, `up.sh`, and runtime socket consumers are pinned to that endpoint. Captured dotenv is parsed as inert assignments. Existing target files are not overwritten. `--retained-delivery` imports the selected release's verified images onto an empty image cache before helpers or isolated PostgreSQL run; conflicting tags are refused. Selected source and all seven image references, including `DOCUMENTSERVER_IMAGE`, replace captured release identity together. External chat/skills roots are refused rather than silently relocated. Isolated PostgreSQL occupancy and restore wait for the final TCP-authenticated server, not the official entrypoint's temporary Unix-socket initializer. Provider inspection uses the selected WebUI `config` table (`key`, `value`, `updated_at`) and does not rewrite persistent database credentials.

After publishing the restored chat-data tree, `restore` atomically replaces or
creates `data/chat/.office-restore-epoch` with a fresh opaque token on one line.
It does not read the captured token or rewrite Office state or version blobs.
All other captured workspace and Office bytes remain unchanged. The broker reads
this marker from `BASE_DATA_DIR` on each relevant request: a captured open session
with the old epoch becomes `orphaned`, and reopening creates a new session/key.
Final closed/error/orphaned records remain final. A late callback from an old
session cannot overwrite restored workspace content.

Before `.restored`, `restore` checks the broker state of each live chat that has a
restored `ocu_chat_state` cursor. A missing `.ocu/index.json` under a positive
cursor is refused. Otherwise the selected release's broker reads and validates the
restored index without scanning `outputs/` or writing the index: an index that is
not valid JSON is refused, an index the broker rejects is refused with the
broker's error, and a cursor greater than the index counter is refused. Broker
file IDs, revisions and counters stay exactly as captured. Files in `outputs/`
that the index does not record, because an agent added, deleted or resized them
after the chat's last listing, do not fail restore; the chat's first listing after
activation indexes them, as it would have on the source. Restore therefore no
longer proves that a chat's `outputs/` tree can be scanned: scan limits and
unsafe paths surface on that chat's first listing after activation. A name
that is not valid UTF-8 or contains a backslash does not fail that listing; it
is listed as an entry that cannot be opened (see `computer-use-server/README.md`).

An epoch write or synchronization failure prevents `.restored`, success output
and application startup. A post-replacement synchronization failure may leave the
new token visible in the owned, unready target; it does not imply rollback.
Use the failure-recovery procedure below rather than reusing a captured epoch.

A recovery set captured before #655 restores a `config/runtime.env` without `OCU_SANDBOX_MESSAGES_ADDRESS`; restore rewrites only identity keys and paths and never adds it. Before activating a release that has the sandbox Messages ingress, set `OCU_SANDBOX_MESSAGES_ADDRESS` in the restored `config/runtime.env` and append its `/32` to `OCU_SANDBOX_EGRESS_ALLOW`, editing those two lines by name without printing the file, as in step 5 of [Upgrading an existing deployment](production-like-test/NETWORK-HARDENING.md#upgrading-an-existing-deployment). Without them the selected release's `up.sh` refuses before any Compose stack starts, and `activate` fails naming `OCU_SANDBOX_MESSAGES_ADDRESS`.

`activate` holds the same daemon-scoped recovery lock as backup/restore around import, compatibility, selected `up.sh`, and version-record generation. Compatibility with the restored schema is checked against the retained delivery before source, inventory, runtime, or version publication. It imports the requested retained delivery as one identity: source commit, `release.json`, and all seven images. An existing `source/.git` is verified against that delivery before any replacement; a different delivery or a tampered tree is refused and does not start. Occupied restored roots reconstruct the selected source from the delivery's Git bundle beside the destination and bind `source/deploy/up.sh` from that tree. Target provider defaults from the protected provider file enter the inert activation environment without rewriting restored database settings or appearing on argv. Consumer-contract success is not claimed from Compose start alone.

Every release inventory loader requires `format_version` 2. Restore and activation
refuse a retained version-1 inventory before image import, selected-release
publication or startup. The recovery-set format number is independent and remains 1.

A manual rollback across the workspace-layout boundary requires matching server
and sandbox images and operator removal/recreation of the newer sandbox
containers. Recovery never deletes or resumes sandboxes. A pre-change release
with a version-1 inventory is not a supported recovery target; container
recreation does not bypass that refusal.

Format 2 also requires a `font_bundle` archive. Delivery verification checks its
checksum and exact files against the selected source's `deploy/fonts/fonts.json`.
Import installs those files under `fonts/` and removes the transport archive.
Activation binds the target's `fonts` link to the owned selected-release root;
matching bytes do not authorize a foreign directory or link. Reuse rechecks every
installed font and license before inventory publication or startup.

`up.sh` derives and exports `OCU_RELEASE_FONTS_DIR` from the selected inventory's
directory after verifying its font material. This path is not a persisted runtime
setting; inherited values cannot select a different font directory.

Before activation, make the retained `OCU_OFFICE_FONTS_DIR` available on the target
host; recovery does not capture, remap or create this operator-owned directory.
An empty directory is valid when no operator fonts are supplied. Relative stored
paths are based on the restored source root during activation; see
[font-path ownership](production-like-test/NETWORK-HARDENING.md) before preparing
that path. Prefer an absolute path for repeatable activation.

The post-activation smoke derives its Compose font input from the selected
`OCU_RELEASE_MANIFEST` itself, overriding stale inherited values. No additional
stored setting or manual font export is required for `deploy/smoke.sh`.

Backup checks Docker's actual container `Image` against the selected release's configuration digest and archive-verified descriptor identities, independently of the launch reference in `Config.Image`. A container and its local reference may use different accepted engine representations. A foreign actual image refuses before any writer is stopped. Source, font, service, project, data environment and mount checks remain mandatory. Sandbox attribution requires the producer's canonical name, required labels, and exact volume/bind mount types, paths, destinations, and access modes. Every discovered sandbox is checked before stopping any writer, then checked again after admission shutdown.

An interrupted activation can resume only its recorded selected-source publication. A private receipt binds the target directory identity, complete retained inventory, selected-source location, and random ownership token; the imported tree carries the matching marker from before publication. Pre-existing unowned directories, symlinks, and altered inventories are refused without overwriting the restored target. Keep these ownership records with the restored target when retrying the same delivery.

Retained format2 contract1 releases remain selectable only with config-only image
records and matching bundled source1. Contract2 supports verified descriptor
identities as well as config-only records. Contract1 descriptor fields, unknown
contracts and mismatched bundled readers refuse before load or activation.
See the [local engine/store qualification matrix](production-like-test/NETWORK-HARDENING.md#qualified-local-image-identity-paths)
for release-transport limits; these do not certify a real recovery drill.

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
