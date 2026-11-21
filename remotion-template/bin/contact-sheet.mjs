// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The contact sheet of kit check's stage 8: four key frames of the
// explainer tiled 2×2 into one 1920×1080 PNG, which the model views to
// answer the checklist (bin/checklist.mjs). sheetTiles picks the frames;
// contactSheet bundles the kit's entry, renders each frame as a still at
// half scale in its own browser, and tiles the stills with ffmpeg. Loaded
// only through the check worker's dynamic import of check-rules.mjs, since
// it imports Remotion. It must run with the working directory at the
// project root, where Remotion finds the headless shell.

import { bundle } from "@remotion/bundler";
import { openBrowser, renderStill, selectComposition } from "@remotion/renderer";
import { execFile } from "node:child_process";
import { mkdir, rm } from "node:fs/promises";
import { join } from "node:path";

const ENTRY_POINT = "src/kit/index.ts";
const PUBLIC_DIR = "public";
const COMPOSITION = "Explainer";
const TILES = 4;
const SCALE = 0.5;
const LAYOUT = "0_0|w0_0|0_h0|w0_h0";

// explainerScenes' entries in playback order. Every scene but the end card
// is a candidate; tile i takes candidate round(i × (m − 1) / 3), so the hook
// is the first tile and the summary the last. A tile's frame is the probe's
// pre-exit frame (exitFrame − 1) in composition frames.
export function sheetTiles(scenes) {
  const candidates = scenes.filter((scene) => scene.kind !== "end-card");
  const m = candidates.length;
  return Array.from({ length: TILES }, (_, i) => {
    const { where, startFrame, durationInFrames, exit } = candidates[Math.round((i * (m - 1)) / (TILES - 1))];
    return { where, frame: startFrame + (exit?.startFrame ?? durationInFrames) - 1 };
  });
}

// Runs ffmpeg with an argument array, never a shell, as kit timeline does;
// its stderr goes to the worker's log. Rejects naming how it failed.
function ffmpeg(args) {
  return new Promise((resolve, reject) => {
    execFile("ffmpeg", args, { encoding: "utf8" }, (error, _stdout, stderr) => {
      if (stderr) process.stderr.write(stderr);
      if (error === null) resolve();
      else if (error.code === "ENOENT") reject(new Error("ffmpeg: not found on PATH"));
      else if (typeof error.code === "string") reject(error);
      else {
        const first = stderr.split("\n").find((line) => line.trim() !== "");
        reject(new Error(`ffmpeg exited with ${error.signal ?? error.code}${first === undefined ? "" : `: ${first.trim()}`}`));
      }
    });
  });
}

// script and timeline: the parsed files, the Explainer's input props. tiles:
// sheetTiles' result. workDir holds the bundle and the stills and is removed
// when this settles; output (outside workDir) receives the sheet.
export async function contactSheet(root, { script, timeline, tiles, workDir, output }) {
  let browser = null;
  try {
    await mkdir(workDir, { recursive: true });
    // As the probe bundles: remotion.config.ts applies only to `npx
    // remotion`, the image's node_modules is root-owned, so no webpack
    // cache, and public/ is linked into the throwaway bundle.
    const serveUrl = await bundle({
      entryPoint: join(root, ENTRY_POINT),
      outDir: join(workDir, "bundle"),
      enableCaching: false,
      publicDir: join(root, PUBLIC_DIR),
      symlinkPublicDir: true,
    });
    browser = await openBrowser("chrome");
    const inputProps = { script, timeline };
    const composition = await selectComposition({ serveUrl, id: COMPOSITION, inputProps, puppeteerInstance: browser });
    const stills = [];
    for (const [i, { frame }] of tiles.entries()) {
      const still = join(workDir, `tile-${i + 1}.png`);
      await renderStill({ composition, serveUrl, inputProps, puppeteerInstance: browser, output: still, frame, imageFormat: "png", scale: SCALE });
      stills.push(still);
    }
    await ffmpeg([
      "-nostdin",
      "-v",
      "error",
      ...stills.flatMap((still) => ["-i", still]),
      "-filter_complex",
      `[0][1][2][3]xstack=inputs=${TILES}:layout=${LAYOUT}`,
      "-frames:v",
      "1",
      "-y",
      output,
    ]);
  } finally {
    try {
      if (browser !== null) await browser.close({ silent: true });
    } finally {
      await rm(workDir, { recursive: true, force: true });
    }
  }
}
