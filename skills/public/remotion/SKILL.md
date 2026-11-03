---
name: remotion
description: Make videos with Remotion (React) in the sandbox, including voiceover and 3D. Offline template, still-frame checks, background MP4 renders to /mnt/user-data/files/.
---

<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2025 Open Computer Use Contributors -->

# Remotion in the sandbox

The sandbox has no internet access and the user cannot open Remotion Studio. The user sees only files you render into `/mnt/user-data/files/`.

The official Remotion skill is at `/mnt/skills/public/remotion/remotion-best-practices/SKILL.md`. Load it and the references it routes to for how to write compositions. Where it conflicts with this file, these rules win. The known conflicts:

| Official skill says | In this sandbox |
| --- | --- |
| Scaffold with `create-video` / create a new project | Copy `/opt/remotion-template` (below) |
| Install packages with `npx remotion add` or `npm install` | Never. Use only the installed packages listed below |
| Start Studio and keep the preview open | Never start Studio. Render check frames instead |
| Don't render the video unless explicitly asked | Always render: check frames first, then the final MP4 |
| Enable WebGL with `--gl=angle` or `Config.setChromiumOpenGlRenderer('angle')` (`remotion-markup/effects.md`, `light-leaks.md`, `html-in-canvas.md`) | Use `swangle` instead (see "WebGL and 3D") |
| WebGPU effects `cylinder()`, `sphere()`, `pageTurn()` (`remotion-markup/effects.md`) | Avoid them; they are unverified without a GPU. Use the WebGL2 effects |
| Load fonts with `@remotion/google-fonts` (Google Fonts) | Not installed and not reachable. Use installed system fonts or local font files |
| Remote media such as `https://remotion.media/...` | Not reachable. Put every asset in the project's `public/` |
| Search Algolia or fetch remotion.dev docs (`remotion-docs/REFERENCE.md`) | Not reachable. Use the vendored references and the typings in `node_modules/<pkg>/dist/*.d.ts` |
| Transcribe with `@remotion/whisper-webgpu` and downloaded models (`remotion-captions/transcribe-captions.md`) | Not available. Time captions from the script you gave `generate_speech` and the audio durations measured with `ffprobe` |
| Generate speech with ElevenLabs (`remotion-markup/voiceover.md`) | Not reachable. Use `generate_speech` when available (see "Voiceover") |

The deployment needs no Remotion license key. Never pass `--license-key` or `--public-license-key`, and never call `Config.setPublicLicenseKey`: without network the usage report retries and stalls the render.

## Start a project

```bash
mkdir -p /home/assistant/videos/<name> && cd /home/assistant/videos/<name>
tar -C /opt/remotion-template --exclude=node_modules -cf - . | tar -xf -
ln -s /opt/remotion-template/node_modules node_modules
mkdir -p public out
```

- `<name>` is a short ASCII name, for example `product-intro`.
- The template has `src/index.ts`, `src/Root.tsx` (one 1920x1080, 30 fps composition with id `Main`), `remotion.config.ts` and `tsconfig.json`. Edit `src/` and register compositions in `src/Root.tsx`.
- `node_modules` is shared and read-only. Never run `npm install`, `npm ci`, `npx remotion add`, `npx create-video` or `npx remotion upgrade`.
- Keep `Config.setCachingEnabled(false)` in `remotion.config.ts`; the shared `node_modules` has no writable cache directory.

## Run Remotion

Every tool call starts in `/home/assistant`, so begin each command with `cd /home/assistant/videos/<name>` and run Remotion only as `npx --no-install remotion …`. Remotion finds its bundled Chrome through the project's `node_modules`; from any other directory it tries to download Chrome and fails. `--no-install` makes a typo fail instead of reaching for the network.

**1. Check frames.** Render the key frames (start, middle, end, every scene change) into the project's `out/` in one command, which bundles once, then look at each PNG with the `view` tool:

```bash
cd /home/assistant/videos/<name>
rm -rf out/frames
npx --no-install remotion render src/index.ts Main out/frames --sequence --frames=0,45,90,149 --image-format=png --concurrency=2
```

Every listed frame must exist: the last frame is `durationInFrames - 1`, so `0,45,90,149` fits only the template's 150-frame `Main`; pick frames from your own composition's length. `rm -rf out/frames` comes first because the sequence output does not delete PNGs from an earlier run.

List the PNGs with `cd /home/assistant/videos/<name> && ls out/frames`, then call `view` on each one by its absolute path, for example `/home/assistant/videos/<name>/out/frames/<file>.png`. `view` returns the frame as an image you can see. Judge it: text readable and inside the frame, nothing cut off or overlapping, the intended colors and layout, no blank or black frame. Fix what looks wrong and repeat. If `view` says the model cannot view images, check the render log and the file sizes instead, and tell the user the frames were not inspected. Copy a frame to `/mnt/user-data/files/` only if the user should see it.

**2. Launch the MP4 render in the background.** Tool commands are killed after `COMMAND_TIMEOUT` (120 seconds), and most renders take longer:

```bash
cd /home/assistant/videos/<name>
rm -f /mnt/user-data/files/<name>.mp4
setsid nohup bash -c 'npx --no-install remotion render src/index.ts Main /mnt/user-data/files/<name>.mp4 --concurrency=2 --codec=h264 --pixel-format=yuv420p; echo "remotion exit $?"' > render.log 2>&1 < /dev/null &
echo $! > render.pid
```

Do not sleep in the same call that launches the render; let that call return at once.

**3. Poll until the log reports the exit code.** Repeat this call while it prints `running`:

```bash
cd /home/assistant/videos/<name>; sleep 100; tail -n 3 render.log; grep "remotion exit" render.log || { kill -0 "$(cat render.pid)" 2>/dev/null && echo running || echo "render died without exit"; }
```

The default idle stop is about 10 minutes; deployments may set it longer. A sandbox stopped for idleness kills the render, so keep polling without long pauses.

**4. Verify and hand over.** `remotion exit 0` means success. Check the file, then tell the user `/mnt/user-data/files/<name>.mp4` is in the Files tab:

```bash
ffprobe -v error -show_entries stream=codec_name,width,height -show_entries format=duration /mnt/user-data/files/<name>.mp4
```

Flags:

- `--concurrency=2` matches the sandbox's 2 CPUs.
- `--codec=h264 --pixel-format=yuv420p` gives an MP4 that plays everywhere.

## When a render fails

Any `remotion exit` other than 0, or an `ffprobe` error, is a failure. `render died without exit` means the render was killed, for example by an idle stop; relaunch it from step 2 of "Run Remotion". Otherwise:

1. Read the error: `cd /home/assistant/videos/<name>; tail -n 50 render.log`.
2. Render the failing frame alone, for example `cd /home/assistant/videos/<name> && npx --no-install remotion still src/index.ts Main out/fail.png --frame=<n>`, and look at it with the `view` tool.
3. Check the usual offline causes:
   - a remote URL, or a `delayRender` timeout while waiting for one: move the asset into `public/`;
   - a missing `staticFile()` asset: check the file name in `public/`;
   - WebGL content without swangle: see "WebGL and 3D";
   - out of memory (killed workers, `SIGKILL`): rerun with `--concurrency=1`.
4. Fix the cause, then launch the render again with step 2.

## WebGL and 3D

For `@remotion/three`, `@react-three/fiber`, `three`, custom shaders, `<HtmlInCanvas>` and the WebGL2 `@remotion/effects`, add this line to the project's `remotion.config.ts`, so no still or render can miss it:

```ts
Config.setChromiumOpenGlRenderer('swangle');
```

Also pass `--gl=swangle` on every `still` and `render` command of such a project. `swangle` is software WebGL; `angle` needs a GPU the sandbox does not have.

## Files and retention

- Final MP4s, frames shown to the user, and audio go to `/mnt/user-data/files/`. Those files stay with the chat.
- Project sources in `/home/assistant/videos/` are working files. Sandbox retention deletes the home directory after 30 days by default, so anything the user needs must be in `/mnt/user-data/files/`.

## Assets and fonts

- Put images, audio, video and font files in the project's `public/` and reference them with `staticFile("name.ext")`. Copy uploads from `/mnt/user-data/files/` into `public/` first.
- Images generated in this chat, for example with the image generation tool, are copied into `/mnt/user-data/files/` before your next tool call runs, and that tool's result names each new one as `Synced generated image: /mnt/user-data/files/<file>`. After generating an image, run any command, for example `ls /mnt/user-data/files`, to receive it. The name often carries a numbered suffix such as `generated-image (2).png`, so copy it by the reported path, quoted, under a plain name:
  ```bash
  cd /home/assistant/videos/<name> && cp "/mnt/user-data/files/generated-image (2).png" public/scene1.png
  ```
  Then show it with `<Img src={staticFile("scene1.png")} />` (`Img` and `staticFile` from `remotion`).
- Installed system fonts work by CSS `font-family`: `Noto Sans CJK SC` (Chinese, Japanese, Korean), `DejaVu Sans`, `Liberation Sans`, `Liberation Serif`, `Noto Color Emoji`. For other fonts, load a local file from `public/` with `@remotion/fonts`.

## Voiceover

When available, call the `generate_speech` tool for each narration line: `text` (up to 2000 characters), a plain `filename` ending in `.mp3` or `.wav`, and `instructions` describing the voice, for example "a calm female narrator, Mandarin". It writes the audio to `/mnt/user-data/files/` and returns its path; an existing name gets a numbered suffix, so use the returned path.

1. Copy each returned file into `public/` under a plain name, quoting the returned path because it can contain spaces and parentheses, for example `line1 (2).mp3`:
   ```bash
   cd /home/assistant/videos/<name> && cp "<returned path>" public/line1.mp3
   ```
2. Measure it: `cd /home/assistant/videos/<name> && ffprobe -v error -show_entries format=duration -of csv=p=0 public/line1.mp3`. Play it with `<Audio src={staticFile("line1.mp3")} />` from `@remotion/media`.
3. Size each scene and the composition's `durationInFrames` from the measured lengths.
4. For captions, use the text you sent to `generate_speech` and place each line over its measured duration.

Without the tool, or when it returns `speech synthesis is not configured`, make the video without narration and say so.

## Installed packages

All Remotion packages are pinned to 4.0.537. Import only these:

| Package | Use |
| --- | --- |
| `remotion` | core: `Composition`, `Sequence`, `AbsoluteFill`, `interpolate`, `spring`, `staticFile`, `Img` |
| `@remotion/cli` | the `npx --no-install remotion` command |
| `@remotion/media` | `<Video>`, `<Audio>` |
| `@remotion/media-utils` | audio and video metadata, audio visualization |
| `@remotion/transitions` | `<TransitionSeries>` and transition presentations |
| `@remotion/effects` | visual effects such as light leaks (WebGL2 ones only) |
| `@remotion/motion-blur` | motion blur |
| `@remotion/shapes` | SVG shapes |
| `@remotion/paths` | SVG path animation |
| `@remotion/rough-notation` | hand-drawn highlights |
| `@remotion/layout-utils` | text measuring and fitting |
| `@remotion/captions` | captions and subtitles from SRT or timed text |
| `@remotion/fonts` | local font files |
| `@remotion/gif` | GIFs |
| `@remotion/lottie` with `lottie-web` | Lottie animations from local JSON |
| `@remotion/three` with `@react-three/fiber` and `three` | 3D scenes (see "WebGL and 3D") |
| `@remotion/zod-types` with `zod` | composition props schemas |
| `react`, `react-dom` | React 19 |

Not installed, because they need the network while rendering: `@remotion/google-fonts`, `@remotion/whisper-webgpu` (transcription models), `@remotion/rive` (loads its runtime from a CDN), `@remotion/lambda` (cloud rendering), and map libraries (map tiles). Skip the official skill's sections on them.
