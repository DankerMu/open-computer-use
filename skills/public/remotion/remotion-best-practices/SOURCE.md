# Source

This directory is the official Remotion agent skill, version 4.0.537, vendored without changes. This file is the only addition.

- **Origin:** [`remotion-dev/remotion`](https://github.com/remotion-dev/remotion), directory [`packages/skills/skills/`](https://github.com/remotion-dev/remotion/tree/v4.0.537/packages/skills/skills) at tag `v4.0.537`.
- **Form:** the layout the skills installer writes. Upstream, `remotion-best-practices/` links to its sibling skills (`remotion-captions/`, `remotion-create/`, and so on) through symlinks. Here they are real directories inside it, each sibling's `SKILL.md` is named `REFERENCE.md`, and links to those files point at `REFERENCE.md`. Every other file is byte-identical to the tag.
- **License:** the Remotion License, [`LICENSE.md`](https://github.com/remotion-dev/remotion/blob/v4.0.537/LICENSE.md) at the root of `remotion-dev/remotion`, which covers `packages/skills`. It is not relicensed here. Terms and the Company License requirement: <https://www.remotion.dev/license>.
- **Checksums:** `tests/fixtures/remotion-best-practices.sha256` lists every vendored file; `tests/test_remotion_image.py` fails if any file changes, appears or disappears.

Sandbox rules that override parts of this skill are in [`../SKILL.md`](../SKILL.md).
