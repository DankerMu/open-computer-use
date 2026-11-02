---
name: remotion
description: Make videos with Remotion (React) in the sandbox, including voiceover and 3D. Offline template, still-frame checks, background MP4 renders to /mnt/user-data/files/.
---

<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2025 Open Computer Use Contributors -->

# Remotion in the sandbox

The sandbox has no internet access and the user cannot open Remotion Studio. The user sees only files you render into `/mnt/user-data/files/`.

The official Remotion skill is in `remotion-best-practices/SKILL.md` next to this file. Load it and the references it routes to for how to write compositions. Where it conflicts with this file, these rules win. The known conflicts:

| Official skill says | In this sandbox |
| --- | --- |
| Scaffold with `create-video` / create a new project | Copy `/opt/remotion-template` (below) |
| Install packages with `npx remotion add` or `npm install` | Never. Use only the installed packages listed below |
| Start Studio and keep the preview open | Never start Studio. Render still frames instead |
| Don't render the video unless explicitly asked | Always render: still frames first, then the final MP4 |
| Load fonts with `@remotion/google-fonts` (Google Fonts) | Not installed and not reachable. Use installed system fonts or local font files |
| Remote media such as `https://remotion.media/...` | Not reachable. Put every asset in the project's `public/` |

## Start a project

```bash
mkdir -p /home/assistant/videos/<name> && cd /home/assistant/videos/<name>
tar -C /opt/remotion-template --exclude=node_modules -cf - . | tar -xf -
ln -s /opt/remotion-template/node_modules node_modules
mkdir -p public
```

- `<name>` is a short ASCII name, for example `product-intro`.
- The template has `src/index.ts`, `src/Root.tsx` (one 1920x1080, 30 fps composition with id `Main`), `remotion.config.ts` and `tsconfig.json`. Edit `src/` and register compositions in `src/Root.tsx`.
- `node_modules` is shared and read-only. Never run `npm install`, `npm ci`, `npx remotion add`, `npx create-video` or `npx remotion upgrade`. Never pass `--license-key`.
- Keep `Config.setCachingEnabled(false)` in `remotion.config.ts`; the shared `node_modules` has no writable cache directory.

## Run Remotion

Always `cd /home/assistant/videos/<name>` first and run Remotion only as `npx --no-install remotion …`. Remotion finds its bundled Chrome through the project's `node_modules`; from any other directory it tries to download Chrome and fails. `--no-install` makes a typo fail instead of reaching for the network.

**1. Check still frames.** Render a few key frames (start, middle, end, every scene change) and look at them before rendering the video:

```bash
cd /home/assistant/videos/<name>
npx --no-install remotion still src/index.ts Main /mnt/user-data/files/<name>-frame-60.png --frame=60
```

**2. Render the MP4 in the background.** Tool commands are killed after `COMMAND_TIMEOUT` (120 seconds), and most renders take longer:

```bash
cd /home/assistant/videos/<name>
nohup npx --no-install remotion render src/index.ts Main /mnt/user-data/files/<name>.mp4 \
  --concurrency=2 --codec=h264 --pixel-format=yuv420p > render.log 2>&1 &
```

**3. Poll until it finishes.** The sandbox stops after about 10 minutes without a tool call, which kills the render, so keep polling:

```bash
sleep 60; tail -n 3 /home/assistant/videos/<name>/render.log; pgrep -f "remotion render" >/dev/null && echo running || echo finished
```

**4. Verify and hand over.** When it has finished, check the log for errors and the file with `ffprobe`, then tell the user the file `/mnt/user-data/files/<name>.mp4` is in the Files tab:

```bash
ffprobe -v error -show_entries stream=codec_name,width,height -show_entries format=duration /mnt/user-data/files/<name>.mp4
```

Flags:

- `--concurrency=2` matches the sandbox's 2 CPUs.
- `--gl=swangle` is required for WebGL content: `@remotion/three`, `@react-three/fiber`, `three`, custom shaders, and most `@remotion/effects`. Add it to both `still` and `render`.
- `--codec=h264 --pixel-format=yuv420p` gives an MP4 that plays everywhere.

## Files and retention

- Final MP4s, stills shown to the user, and audio go to `/mnt/user-data/files/`. Those files stay with the chat.
- Project sources in `/home/assistant/videos/` are working files. Sandbox retention deletes the home directory after 30 days by default, so anything the user needs must be in `/mnt/user-data/files/`.

## Assets, fonts and voiceover

- Put images, audio, video and font files in the project's `public/` and reference them with `staticFile("name.ext")`. Copy uploads from `/mnt/user-data/files/` into `public/` first.
- Installed system fonts work by CSS `font-family`: `Noto Sans CJK SC` (Chinese, Japanese, Korean), `DejaVu Sans`, `Liberation Sans`, `Liberation Serif`, `Noto Color Emoji`. For other fonts, load a local file from `public/` with `@remotion/fonts`.
- Voiceover: when available, call the `generate_speech` tool. It writes the audio to `/mnt/user-data/files/` and returns its path. Measure its length with `ffprobe -v error -show_entries format=duration -of csv=p=0 <file>`, copy it into `public/`, play it with `<Audio src={staticFile("<file>")} />` from `@remotion/media`, and size the composition's `durationInFrames` from the measured length. Without the tool, make the video without narration and say so.

## Installed packages

All Remotion packages are pinned to 4.0.537. Import only these:

| Package | Use |
| --- | --- |
| `remotion` | core: `Composition`, `Sequence`, `AbsoluteFill`, `interpolate`, `spring`, `staticFile`, `Img` |
| `@remotion/cli` | the `npx --no-install remotion` command |
| `@remotion/media` | `<Video>`, `<Audio>` |
| `@remotion/media-utils` | audio and video metadata, audio visualization |
| `@remotion/transitions` | `<TransitionSeries>` and transition presentations |
| `@remotion/effects` | visual effects such as light leaks |
| `@remotion/motion-blur` | motion blur |
| `@remotion/shapes` | SVG shapes |
| `@remotion/paths` | SVG path animation |
| `@remotion/rough-notation` | hand-drawn highlights |
| `@remotion/layout-utils` | text measuring and fitting |
| `@remotion/captions` | captions and subtitles from SRT or timed text |
| `@remotion/fonts` | local font files |
| `@remotion/gif` | GIFs |
| `@remotion/lottie` with `lottie-web` | Lottie animations from local JSON |
| `@remotion/three` with `@react-three/fiber` and `three` | 3D scenes (render with `--gl=swangle`) |
| `@remotion/zod-types` with `zod` | composition props schemas |
| `react`, `react-dom` | React 19 |

Not installed, because they need the network while rendering: `@remotion/google-fonts`, `@remotion/whisper-webgpu` (transcription models), `@remotion/rive` (loads its runtime from a CDN), `@remotion/lambda` (cloud rendering), and map libraries (map tiles). Skip the official skill's sections on them.
