# Source

This directory is the official Remotion agent skill, version 4.0.537, vendored without changes. This file is the only addition.

- **Origin:** [`remotion-dev/remotion`](https://github.com/remotion-dev/remotion), directory [`packages/skills/skills/`](https://github.com/remotion-dev/remotion/tree/v4.0.537/packages/skills/skills) at tag `v4.0.537`.
- **Form:** the layout the skills installer writes. Upstream, `remotion-best-practices/` links to its sibling skills (`remotion-captions/`, `remotion-create/`, and so on) through symlinks, and `remotion-markup/` links to `remotion-maps/` the same way. The installer expanded every symlink into a real directory:
  - each sibling skill sits inside `remotion-best-practices/`, with its `SKILL.md` renamed `REFERENCE.md`;
  - `remotion-markup/remotion-maps/` is a second expanded copy of `remotion-maps/`, file for file;
  - links that pointed at a sibling's `SKILL.md`, such as `./remotion-create/SKILL.md` or `../remotion-interactivity/SKILL.md`, now point at its `REFERENCE.md`. These rewrites are the only text changes; they touch `SKILL.md`, the `REFERENCE.md` of `remotion-captions/`, `remotion-create/`, `remotion-docs/` and `remotion-markup/`, `remotion-maps/techniques/static-map/TECHNIQUE.md` (both copies), and `remotion-markup/timing-props.md`, `text-highlights.md` and `cropping.md`.

  Every other file is byte-identical to the tag.
- **License:** the Remotion License, [`LICENSE.md`](https://github.com/remotion-dev/remotion/blob/v4.0.537/LICENSE.md) at the root of `remotion-dev/remotion`, which covers `packages/skills`. It is not relicensed here. Terms and the Company License requirement: <https://www.remotion.dev/license>.
- **Checksums:** `tests/fixtures/remotion-best-practices.sha256` lists every vendored file; `tests/test_remotion_image.py` fails if any file changes, appears or disappears.

Sandbox rules that override parts of this skill are in [`../SKILL.md`](../SKILL.md).
