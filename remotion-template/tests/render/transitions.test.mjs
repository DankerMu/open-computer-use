// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The theme transitions in a real headless shell (task 4.5): PNG stills of
// the fixture Explainer, rendered by renderStill in one browser and decoded
// with ffmpeg to rgb24, and one MP4 for the ffprobe frame count. Runs only
// inside the workspace image, from a project root whose node_modules link
// reaches /opt/remotion-template/node_modules, one test file after another:
//   node --test --test-concurrency=1 "tests/render/*.test.mjs"
// CI's tests/*.test.mjs glob does not match this file.
//
// Expected values come from the task 4.5 fixture. Every scene but the last
// fades linearly to the theme's background over its transitionOut window
// [s, e), e being its endFrame; the incoming scene is drawn only from e. The
// fixture scenes (3.8's) are static, so for each boundary:
//   - frame s − 1 equals the scene's narrationEndFrame frame (±2, every pixel);
//   - frame s differs from s − 1 by more than 8 in the text region;
//   - frame e − 2 differs from the background by more than 4 in the text region;
//   - frame e − 1 is the background (±2, every pixel);
//   - frame e equals e + 1 (±2), above the caption band only when the band
//     shows another page on the two frames.
// Across the video: the last scene's last 15 frames equal its
// narrationEndFrame frame, hook frames 0 and 20 are equal, and ffprobe counts
// totalFrames. Captions, on chapter 1's first scene, in the band 930–1026.
// The text region is x 200–1400, y 200–500, where 3.8's fixture scenes draw.

import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { copyFileSync, cpSync, mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { bundle } from "@remotion/bundler";
import { openBrowser, renderMedia, renderStill, selectComposition } from "@remotion/renderer";
import { writeCustomIndex } from "../../bin/custom-index.mjs";
import { themes } from "../../src/kit/theme.ts";
import { coverage, transitionsScript, transitionsTimeline } from "./fixtures/transitions/fixture.mjs";

const TEMPLATE = fileURLToPath(new URL("../..", import.meta.url));
const WIDTH = 1920;
const HEIGHT = 1080;
const BG = themes.light.brand.bg;
const FULL = { x0: 0, x1: WIDTH, y0: 0, y1: HEIGHT };
const TEXT = { x0: 200, x1: 1400, y0: 200, y1: 500 };
const BAND = { x0: 0, x1: WIDTH, y0: 930, y1: 1026 };
const ABOVE_BAND = { x0: 0, x1: WIDTH, y0: 0, y1: 930 };
// Per-channel levels: equal within 2, changed above 8 (opacity 14/15 moves
// text about 15 levels), not the background above 4 (opacity 1/15).
const EQUAL = 2;
const CHANGED = 8;
const NOT_BG = 4;
const LEAD_IN = [0, 20];
const TAIL = 15;

const script = transitionsScript();
const timeline = transitionsTimeline(script);
const TOTAL = timeline.totalFrames;
const lastScene = timeline.scenes.at(-1);
const boundaries = timeline.scenes.slice(0, -1);
const chapterOne = timeline.scenes.find((scene) => scene.chapter === 1 && scene.scene === 1);
const sceneBlocks = (scene) => scene.sentences.flatMap((sentence) => sentence.blocks);
const allBlocks = timeline.scenes.flatMap(sceneBlocks);
const [b1, b2] = sceneBlocks(chapterOne);
const lastBlock = sceneBlocks(chapterOne).at(-1);

// The caption page (its block text) showing at `frame`, or null.
const pageAt = (frame) => allBlocks.find(({ startFrame, endFrame }) => startFrame <= frame && frame < endFrame)?.text ?? null;

// Every frame a check below reads.
function neededFrames() {
  const frames = new Set(LEAD_IN);
  for (const { transitionOut: { startFrame: s }, endFrame: e, narrationEndFrame: n } of boundaries) {
    for (const frame of [n, s - 1, s, e - 2, e - 1, e, e + 1]) frames.add(frame);
  }
  frames.add(lastScene.narrationEndFrame);
  for (let frame = lastScene.endFrame - TAIL; frame < lastScene.endFrame; frame++) frames.add(frame);
  for (const frame of [b1.startFrame - 1, b1.startFrame, b1.endFrame - 1, b1.endFrame, b2.startFrame, lastBlock.endFrame - 1, lastBlock.endFrame]) {
    frames.add(frame);
  }
  return [...frames].sort((a, b) => a - b);
}

const timings = {};
const outcomes = {};
const stills = new Map();
let root;
let browser = null;

async function step(name, run) {
  const started = Date.now();
  try {
    return { value: await run() };
  } catch (error) {
    return { error };
  } finally {
    timings[name] = Date.now() - started;
  }
}

function copyProject() {
  root = realpathSync(mkdtempSync(join(tmpdir(), "transitions-render-")));
  for (const part of ["src", "bin", join("tests", "render", "fixtures")]) {
    cpSync(join(TEMPLATE, part), join(root, part), { recursive: true });
  }
  for (const name of ["package.json", "tsconfig.json", "remotion.config.ts"]) copyFileSync(join(TEMPLATE, name), join(root, name));
  symlinkSync(realpathSync(join(TEMPLATE, "node_modules")), join(root, "node_modules"));
  mkdirSync(join(root, "public"));
  // totalFrames of 48 kHz mono: totalFrames × 1600 samples.
  execFileSync("ffmpeg", [
    "-nostdin", "-v", "error",
    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
    "-af", `atrim=end_sample=${TOTAL * 1600}`,
    "-ac", "1", "-c:a", "pcm_s16le",
    join(root, "public", "narration.wav"),
  ]);
}

before(async () => {
  // The fixture's coverage, before any render.
  coverage(timeline);
  copyProject();
  copyFileSync(join(TEMPLATE, "tests", "render", "fixtures", "explainer", "Orbit.tsx"), join(root, "src", "custom", "Orbit.tsx"));
  await writeCustomIndex(root, script);
  browser = await openBrowser("chrome");
  const inputProps = { script, timeline };
  const serveUrl = await step("bundle", () =>
    bundle({
      entryPoint: join(root, "tests", "render", "fixtures", "explainer", "entry.tsx"),
      outDir: join(root, "bundles", "fixture"),
      enableCaching: false,
      publicDir: join(root, "public"),
      symlinkPublicDir: true,
    }),
  );
  if (serveUrl.error) {
    outcomes.explainer = serveUrl;
    return;
  }
  outcomes.explainer = await step("select", () => selectComposition({ serveUrl: serveUrl.value, id: "Explainer", inputProps, puppeteerInstance: browser }));
  if (outcomes.explainer.error) return;
  const composition = outcomes.explainer.value;
  const frames = neededFrames();
  outcomes.stills = await step(`stills (${frames.length})`, async () => {
    mkdirSync(join(root, "stills"));
    for (const frame of frames) {
      const output = join(root, "stills", `${frame}.png`);
      await renderStill({ composition, serveUrl: serveUrl.value, output, frame, inputProps, puppeteerInstance: browser });
      stills.set(frame, output);
    }
    return frames.length;
  });
  const output = join(root, "out", "explainer.mp4");
  outcomes.render = await step("render", async () => {
    await renderMedia({ composition, serveUrl: serveUrl.value, codec: "h264", outputLocation: output, inputProps, puppeteerInstance: browser });
    return JSON.parse(
      execFileSync("ffprobe", ["-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries", "stream=nb_read_frames", "-of", "json", output], {
        encoding: "utf8",
      }),
    );
  });
});

after(async () => {
  try {
    if (browser !== null) await browser.close({ silent: true });
  } finally {
    if (root !== undefined) rmSync(root, { recursive: true, force: true });
  }
});

function value(outcome, label) {
  if (outcome?.error) throw outcome.error;
  assert.ok(outcome?.value !== undefined, `${label} did not run`);
  return outcome.value;
}

// Frame `frame` of the Explainer as WIDTH × HEIGHT rgb24 bytes.
function rgbFrame(frame) {
  value(outcomes.stills, "stills");
  const path = stills.get(frame);
  assert.ok(path !== undefined, `frame ${frame} was not rendered`);
  const bytes = execFileSync("ffmpeg", ["-nostdin", "-v", "error", "-i", path, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], {
    maxBuffer: WIDTH * HEIGHT * 3 + 1024,
  });
  assert.equal(bytes.length, WIDTH * HEIGHT * 3, `frame ${frame}`);
  return bytes;
}

const rgb = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));

// The largest per-channel difference inside `region` between frame bytes
// `a` and either frame bytes `b` or an [r, g, b] colour.
function maxDiff(a, b, region) {
  const colour = Array.isArray(b) ? b : null;
  let max = 0;
  for (let y = region.y0; y < region.y1; y++) {
    for (let x = region.x0; x < region.x1; x++) {
      const i = (y * WIDTH + x) * 3;
      for (let c = 0; c < 3; c++) {
        const d = Math.abs(a[i + c] - (colour === null ? b[i + c] : colour[c]));
        if (d > max) max = d;
      }
    }
  }
  return max;
}

const where = (scene) => `${scene.kind} ${scene.startFrame}–${scene.endFrame}`;

test("timings", (t) => {
  for (const [name, ms] of Object.entries(timings)) t.diagnostic(`${name}: ${ms} ms`);
});

test("frame s − 1 of every window equals the scene's narrationEndFrame frame", (t) => {
  for (const scene of boundaries) {
    const s = scene.transitionOut.startFrame;
    const d = maxDiff(rgbFrame(s - 1), rgbFrame(scene.narrationEndFrame), FULL);
    t.diagnostic(`${where(scene)}: frame ${s - 1} vs ${scene.narrationEndFrame}: ${d}`);
    assert.ok(d <= EQUAL, `${where(scene)}: frame ${s - 1} differs from frame ${scene.narrationEndFrame} by ${d}`);
  }
});

test("frame s of every window differs from frame s − 1 in the text region", (t) => {
  for (const scene of boundaries) {
    const s = scene.transitionOut.startFrame;
    const d = maxDiff(rgbFrame(s), rgbFrame(s - 1), TEXT);
    t.diagnostic(`${where(scene)}: frame ${s} vs ${s - 1}: ${d}`);
    assert.ok(d > CHANGED, `${where(scene)}: frame ${s} differs from frame ${s - 1} by only ${d}`);
  }
});

test("frame e − 2 of every window is not the background", (t) => {
  for (const scene of boundaries) {
    const frame = scene.endFrame - 2;
    const d = maxDiff(rgbFrame(frame), rgb(BG), TEXT);
    t.diagnostic(`${where(scene)}: frame ${frame} vs ${BG}: ${d}`);
    assert.ok(d > NOT_BG, `${where(scene)}: frame ${frame} differs from the background by only ${d}`);
  }
});

test("frame e − 1 of every window is the background alone", (t) => {
  for (const scene of boundaries) {
    const frame = scene.endFrame - 1;
    const d = maxDiff(rgbFrame(frame), rgb(BG), FULL);
    t.diagnostic(`${where(scene)}: frame ${frame} vs ${BG}: ${d}`);
    assert.ok(d <= EQUAL, `${where(scene)}: frame ${frame} differs from the background by ${d}`);
  }
});

test("the incoming scene's first frame e equals e + 1", (t) => {
  for (const scene of boundaries) {
    const e = scene.endFrame;
    const samePage = pageAt(e) === pageAt(e + 1);
    const d = maxDiff(rgbFrame(e), rgbFrame(e + 1), samePage ? FULL : ABOVE_BAND);
    t.diagnostic(`${where(scene)}: frame ${e} vs ${e + 1} (${samePage ? "every row" : "above the band"}): ${d}`);
    assert.ok(d <= EQUAL, `${where(scene)}: frame ${e} differs from frame ${e + 1} by ${d}`);
  }
});

test("the last scene has no exit: its last 15 frames equal its narrationEndFrame frame", (t) => {
  const reference = rgbFrame(lastScene.narrationEndFrame);
  for (let frame = lastScene.endFrame - TAIL; frame < lastScene.endFrame; frame++) {
    const d = maxDiff(rgbFrame(frame), reference, FULL);
    t.diagnostic(`frame ${frame} vs ${lastScene.narrationEndFrame}: ${d}`);
    assert.ok(d <= EQUAL, `frame ${frame} differs from frame ${lastScene.narrationEndFrame} by ${d}`);
  }
});

test("the hook's lead-in has no effect: frames 0 and 20 are equal", () => {
  const d = maxDiff(rgbFrame(LEAD_IN[0]), rgbFrame(LEAD_IN[1]), FULL);
  assert.ok(d <= EQUAL, `frames 0 and 20 differ by ${d}`);
});

test("the rendered video has totalFrames frames", () => {
  assert.equal(value(outcomes.explainer, "select").durationInFrames, TOTAL);
  const [stream] = value(outcomes.render, "render").streams;
  assert.equal(Number(stream.nb_read_frames), TOTAL);
});

test("captions page per block on chapter 1's first scene", (t) => {
  const check = (label, d, ok) => {
    t.diagnostic(`${label}: ${d}`);
    assert.ok(ok(d), `${label}: ${d}`);
  };
  check(`band ${b1.startFrame} vs ${b1.startFrame - 1} differs`, maxDiff(rgbFrame(b1.startFrame), rgbFrame(b1.startFrame - 1), BAND), (d) => d > CHANGED);
  check(`band ${b1.endFrame - 1} vs ${b1.startFrame} equal`, maxDiff(rgbFrame(b1.endFrame - 1), rgbFrame(b1.startFrame), BAND), (d) => d <= EQUAL);
  if (b2.startFrame > b1.endFrame) {
    check(`band ${b1.endFrame} is the background`, maxDiff(rgbFrame(b1.endFrame), rgb(BG), BAND), (d) => d <= EQUAL);
  }
  check(`band ${b2.startFrame} vs ${b1.startFrame} differs`, maxDiff(rgbFrame(b2.startFrame), rgbFrame(b1.startFrame), BAND), (d) => d > CHANGED);
  check(`band ${lastBlock.endFrame - 1} shows a page`, maxDiff(rgbFrame(lastBlock.endFrame - 1), rgb(BG), BAND), (d) => d > CHANGED);
  check(`band ${lastBlock.endFrame} is the background`, maxDiff(rgbFrame(lastBlock.endFrame), rgb(BG), BAND), (d) => d <= EQUAL);
});
