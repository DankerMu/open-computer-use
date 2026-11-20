// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// kit render's real render: bundles the kit's entry, opens its own browser,
// selects the Explainer with the parsed script.json and timeline.json as
// input props, and renders it to one H.264 / yuv420p / AAC file. Loaded only
// by the render worker's dynamic import (bin/render.mjs), since it imports
// Remotion. It must run with the working directory at the project root,
// where Remotion finds the headless shell.

import { bundle } from "@remotion/bundler";
import { openBrowser, renderMedia, selectComposition } from "@remotion/renderer";
import { rm } from "node:fs/promises";
import { join } from "node:path";

const ENTRY_POINT = "src/kit/index.ts";
const PUBLIC_DIR = "public";
const BUNDLE_DIR = ".kit/render/bundle";
const COMPOSITION = "Explainer";
// The production CPU quota. Remotion's default follows os.cpus(), which
// reports the host's CPUs inside a --cpus=2 container (14, measured).
const CONCURRENCY = 2;

// script and timeline: the parsed files. music: true when the worker wrote
// public/music.wav. output: the video's path. log receives `render: frames
// 0/<total>` once the composition is selected, then `render: frames
// <done>/<total>` at most once per tenth of the frames.
export async function renderVideo({ root, script, timeline, music, output, log }) {
  const outDir = join(root, BUNDLE_DIR);
  let browser = null;
  try {
    // As the probe bundles: remotion.config.ts applies only to `npx
    // remotion`, the image's node_modules is root-owned, so no webpack
    // cache, and public/ is linked into the throwaway bundle.
    const serveUrl = await bundle({
      entryPoint: join(root, ENTRY_POINT),
      outDir,
      enableCaching: false,
      publicDir: join(root, PUBLIC_DIR),
      symlinkPublicDir: true,
    });
    browser = await openBrowser("chrome");
    const inputProps = music === true ? { script, timeline, music: true } : { script, timeline };
    const composition = await selectComposition({ serveUrl, id: COMPOSITION, inputProps, puppeteerInstance: browser });
    const total = composition.durationInFrames;
    log(`render: frames 0/${total}`);
    let tenths = 0;
    await renderMedia({
      composition,
      serveUrl,
      inputProps,
      puppeteerInstance: browser,
      codec: "h264",
      pixelFormat: "yuv420p",
      audioCodec: "aac",
      concurrency: CONCURRENCY,
      outputLocation: output,
      onProgress: ({ renderedFrames }) => {
        const reached = Math.floor((renderedFrames * 10) / total);
        if (reached > tenths) {
          tenths = reached;
          log(`render: frames ${renderedFrames}/${total}`);
        }
      },
    });
  } finally {
    try {
      if (browser !== null) await browser.close({ silent: true });
    } finally {
      await rm(outDir, { recursive: true, force: true });
    }
  }
}
