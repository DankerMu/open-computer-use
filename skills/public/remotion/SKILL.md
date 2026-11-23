---
name: remotion
description: Make videos with Remotion (React) in the sandbox. Science explainers use the explainer kit; course, promo, report and other videos use the free-form template. Offline, with background MP4 renders to /mnt/user-data/files/.
---

<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2025 Open Computer Use Contributors -->

# Remotion in the sandbox

The sandbox has no internet access and the user cannot open Remotion Studio. The user sees only files in `/mnt/user-data/files/`.

Every tool call starts in `/home/assistant`. Never install packages.

## Choose the workflow

- **Science explainer (科普讲解)**: a video that explains a science topic, a concept or how something works. Follow "Science explainers" below.
- **Every other video** (course, promo, report and anything else): read `/mnt/skills/public/remotion/free-form.md` and follow it. Do not use the kit for these.

## Science explainers

Read `/mnt/skills/public/remotion/explainer-kit.md` first. It gives the `script.json` format, the scene kinds, and what to do when a step fails. Narration and all on-screen text are Chinese.

Do these steps in this order:

1. Run `node /opt/remotion-template/bin/kit.mjs new <name>`, then start every later call with `cd /home/assistant/videos/<name> &&`.
2. Write `script.json` in the project, by the format in `explainer-kit.md`.
3. For each illustration, generate it with the image tool, run any command, then run `node bin/kit.mjs image "<path>"` with the reported path and write the printed name in the scene's `image`.
4. Run `node bin/kit.mjs voice`, make one `generate_speech` call per printed line with that line's fields, then run `node bin/kit.mjs voice-import`.
5. Run `node bin/kit.mjs timeline`.
6. Run `node bin/kit.mjs check`, then run `node bin/kit.mjs status` until it no longer reports `running`.
7. `view` the contact sheet that `status` names and answer every checklist question yes or no; on any "no", fix the script (or the image or voice it names), redo the steps it affects, and check again.
8. Run `node bin/kit.mjs render`, then run `node bin/kit.mjs status` until it reports `render succeeded`, then tell the user the published path.

Rules for explainers:

- Skip step 3 when no scene has an image.
- If `status` reports `failed` or `died`, follow "Failures" in `explainer-kit.md`. Never go on to step 8 before `status` reports `check passed`.
- Read the official Remotion guidance, `/mnt/skills/public/remotion/remotion-best-practices/SKILL.md`, only to write a `custom` scene.
- Render mechanics, polling and failure handling are in `explainer-kit.md`.
