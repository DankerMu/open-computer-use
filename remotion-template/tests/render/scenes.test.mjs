// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The six scene components of task 4.2 in a real headless shell. Runs only
// inside the workspace image, from a project root whose node_modules link
// reaches /opt/remotion-template/node_modules, one test file after another:
//   node --test --test-concurrency=1 "tests/render/*.test.mjs"
// CI's tests/*.test.mjs glob does not match this file.
//
// Everything runs in a scratch copy of the template with no public/, so the
// samples' image comes from the tracked gallery/public. Three independent
// steps, each started by the first test that needs it, so one can run alone
// through --test-name-pattern:
//   - probe: one probe() over every catalog sample in both themes, through
//     the gallery entry's KitProbe;
//   - settled: in light, each sample stilled at its entrance end and on its
//     last frame;
//   - gallery: gallery({out}) on the tracked catalog.
// Expected values come from the fixture: the caption band's top (930), the
// boxes of src/kit/scenes/boxes.ts, the samples' text fields, 38 clips and
// 114 key frames, and the centre colour of gallery/public/images/sample.png
// within ±6 per channel.

import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { copyFileSync, cpSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { bundle } from "@remotion/bundler";
import { openBrowser, renderStill, selectComposition } from "@remotion/renderer";
import { probe, violations } from "../../bin/probe.mjs";
import { candidates } from "../../src/kit/gallery/catalog.ts";
import { captionBand } from "../../src/kit/render/captions.ts";
import { IMAGE_BOX, TEXT_BOX } from "../../src/kit/scenes/boxes.ts";
import { themes } from "../../src/kit/theme.ts";

const TEMPLATE = fileURLToPath(new URL("../..", import.meta.url));
const THEMES = ["light", "dark"];
const ROLES = ["entrance-end", "midpoint", "pre-exit"];
const BAND_TOP = captionBand(themes.light).top;
// The probe's own box tolerances (task 3.7, amended): 0.5 px across, 1 px down.
const TOLERANCE = { x: 0.5, y: 1 };
const CHANNEL_TOLERANCE = 6;
const CLIPS = 38;
const KEY_FRAMES = 114;
const SAMPLE_IMAGE = { path: join(TEMPLATE, "gallery", "public", "images", "sample.png"), x: 640, y: 480 };
const PANEL_CENTRE = { x: IMAGE_BOX.panel.left + IMAGE_BOX.panel.width / 2, y: IMAGE_BOX.panel.top + IMAGE_BOX.panel.height / 2 };

// Every (candidate, sample) of the tracked catalog, with its probe facts.
const SAMPLES = candidates.flatMap((c) =>
  c.samples.map((s) => ({ candidate: c.id, sample: s.id, entranceFrames: c.entranceFrames, durationInFrames: s.timing.durationInFrames, content: s.content, image: s.image })),
);
const itemId = (s, theme) => `${s.candidate}--${s.sample}--${theme}`;
const ITEMS = THEMES.flatMap((theme) => SAMPLES.map((s) => ({ ...s, theme, id: itemId(s, theme) })));
const byId = new Map(ITEMS.map((item) => [item.id, item]));

const timings = {};
let root;

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

// A step that runs once, when a test first asks for it.
function once(name, run) {
  let started = null;
  return async () => {
    started ??= step(name, run);
    const outcome = await started;
    if (outcome.error) throw outcome.error;
    return outcome.value;
  };
}

before(() => {
  root = realpathSync(mkdtempSync(join(tmpdir(), "scenes-render-")));
  for (const part of ["src", "bin", "gallery"]) cpSync(join(TEMPLATE, part), join(root, part), { recursive: true });
  for (const name of ["package.json", "tsconfig.json", "remotion.config.ts"]) copyFileSync(join(TEMPLATE, name), join(root, name));
  symlinkSync(realpathSync(join(TEMPLATE, "node_modules")), join(root, "node_modules"));
  assert.equal(existsSync(join(root, "public")), false, "the scratch project has no public/");
});

after(() => {
  if (root !== undefined) rmSync(root, { recursive: true, force: true });
});

const ENTRY = () => join(root, "src", "kit", "gallery", "entry.tsx");
const GALLERY_PUBLIC = () => join(root, "gallery", "public");

const probed = once("probe", () => {
  assert.ok(ITEMS.length > 0, "the catalog lists no candidate");
  const targets = ITEMS.map((item) => ({
    id: item.id,
    scene: "gallery-sample",
    props: { candidate: item.candidate, sample: item.sample, theme: item.theme },
    durationInFrames: item.durationInFrames,
    entranceFrames: item.entranceFrames,
    exitFrame: item.durationInFrames,
  }));
  return probe({ entryPoint: ENTRY(), targets, publicDir: GALLERY_PUBLIC() });
});

// The text fields of a sample's content: its strings and its lists' strings.
const fieldValues = (content) => Object.values(content).flatMap((value) => (Array.isArray(value) ? value : [value]));

const isInside = (path, ancestor) => path === ancestor || path.startsWith(`${ancestor}>`);

function inside(glyph, box) {
  return (
    glyph.x >= box.left - TOLERANCE.x &&
    glyph.x + glyph.width <= box.left + box.width + TOLERANCE.x &&
    glyph.y >= box.top - TOLERANCE.y &&
    glyph.y + glyph.height <= box.top + box.height + TOLERANCE.y
  );
}

test("probe: every frame of every sample keeps every probe rule in its own theme", async () => {
  const result = await probed();
  assert.equal(result.frames.length, ITEMS.length * ROLES.length);
  for (const theme of THEMES) {
    const own = violations(result, themes[theme]).filter((v) => byId.get(v.target).theme === theme);
    assert.deepEqual(own, [], theme);
  }
});

test("probe: every text record's glyph box ends at or above the caption band", async () => {
  for (const frame of (await probed()).frames) {
    assert.ok(frame.texts.length > 0, `${frame.target} ${frame.role}: no text`);
    for (const record of frame.texts) {
      const end = record.glyphBox.y + record.glyphBox.height;
      assert.ok(end <= BAND_TOP, `${frame.target} ${frame.role} ${record.path} "${record.text}" ends at ${end}`);
    }
  }
});

test("probe: every marked text lies inside its sample's text box (IMAGE_BOX.text with an image, else TEXT_BOX)", async () => {
  for (const frame of (await probed()).frames) {
    const item = byId.get(frame.target);
    const box = item.image === null ? TEXT_BOX : IMAGE_BOX.text;
    for (const mark of frame.marked) {
      const records = frame.texts.filter((record) => isInside(record.path, mark.path));
      assert.ok(records.length > 0, `${frame.target} ${frame.role}: "${mark.input}" has no text record`);
      for (const record of records) {
        assert.ok(inside(record.glyphBox, box), `${frame.target} ${frame.role}: "${record.text}" at ${JSON.stringify(record.glyphBox)} is outside ${JSON.stringify(box)}`);
      }
    }
  }
});

test("probe: at the entrance end the marked inputs are exactly the sample's text field values", async () => {
  const entranceEnds = (await probed()).frames.filter((frame) => frame.role === "entrance-end");
  assert.equal(entranceEnds.length, ITEMS.length);
  for (const frame of entranceEnds) {
    const item = byId.get(frame.target);
    assert.equal(frame.frame, item.entranceFrames, `${frame.target}: entrance-end frame`);
    assert.deepEqual(
      frame.marked.map(({ input }) => input).sort(),
      fieldValues(item.content).sort(),
      frame.target,
    );
  }
});

// --- settled ------------------------------------------------------------------

const rgb = (path, crop = null) =>
  execFileSync("ffmpeg", ["-nostdin", "-v", "error", "-i", path, ...(crop === null ? [] : ["-vf", `crop=${crop}`]), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], {
    maxBuffer: 64 * 1024 * 1024,
  });

const stills = once("settled stills", async () => {
  const serveUrl = await bundle({ entryPoint: ENTRY(), outDir: join(root, "bundles", "settled"), enableCaching: false, publicDir: GALLERY_PUBLIC(), symlinkPublicDir: true });
  const out = join(root, "stills");
  mkdirSync(out);
  const browser = await openBrowser("chrome");
  try {
    const pairs = [];
    for (const s of SAMPLES) {
      const inputProps = { candidate: s.candidate, sample: s.sample, theme: "light" };
      const composition = await selectComposition({ serveUrl, id: "KitGallerySample", inputProps, puppeteerInstance: browser, logLevel: "error" });
      const paths = [];
      for (const frame of [s.entranceFrames, s.durationInFrames - 1]) {
        const output = join(out, `${itemId(s, "light")}--${frame}.png`);
        await renderStill({ composition, serveUrl, output, frame, inputProps, puppeteerInstance: browser, logLevel: "error" });
        paths.push(output);
      }
      pairs.push({ id: itemId(s, "light"), paths });
    }
    return pairs;
  } finally {
    await browser.close({ silent: true });
  }
});

test("settled: in light, every sample's still at its entrance end equals its last frame byte for byte", async () => {
  const pairs = await stills();
  assert.equal(pairs.length, SAMPLES.length);
  for (const { id, paths } of pairs) {
    const [entranceEnd, last] = paths.map((path) => rgb(path));
    assert.equal(entranceEnd.length, 1920 * 1080 * 3, id);
    assert.ok(entranceEnd.equals(last), `${id}: the entrance-end still differs from the last frame`);
  }
});

// --- the gallery --------------------------------------------------------------

const galleryRun = once("gallery", async () => {
  const { gallery } = await import(pathToFileURL(join(root, "bin", "gallery.mjs")).href);
  return gallery({ out: join(root, "gallery-out") });
});

const pixel = (path, x, y) => [...rgb(path, `1:1:${x}:${y}`)];

test("gallery: the tracked catalog gives 38 clips, 114 key frames and no violation on any item", async () => {
  const { out, items } = await galleryRun();
  assert.equal(items, CLIPS);
  assert.equal(readdirSync(join(out, "clips")).length, CLIPS);
  assert.equal(readdirSync(join(out, "frames")).length, KEY_FRAMES);
  const data = JSON.parse(readFileSync(join(out, "gallery.json"), "utf8"));
  assert.equal(data.items.length, CLIPS);
  for (const item of data.items) assert.deepEqual(item.violations, [], item.id);
});

test("gallery: each candidate's image sample shows sample.png's centre at the panel's centre, and its max sample does not", async () => {
  const { out } = await galleryRun();
  const expected = pixel(SAMPLE_IMAGE.path, SAMPLE_IMAGE.x, SAMPLE_IMAGE.y);
  const near = (actual) => actual.every((channel, i) => Math.abs(channel - expected[i]) <= CHANNEL_TOLERANCE);
  for (const { id } of candidates) {
    const shown = pixel(join(out, "frames", `${id}--image--light--entrance-end.png`), PANEL_CENTRE.x, PANEL_CENTRE.y);
    assert.ok(near(shown), `${id} image: ${JSON.stringify(shown)} is not sample.png's ${JSON.stringify(expected)}`);
    const plain = pixel(join(out, "frames", `${id}--max--light--entrance-end.png`), PANEL_CENTRE.x, PANEL_CENTRE.y);
    assert.ok(!near(plain), `${id} max: ${JSON.stringify(plain)} matches sample.png's ${JSON.stringify(expected)}`);
  }
});

test("timings", (t) => {
  for (const [name, ms] of Object.entries(timings)) t.diagnostic(`${name}: ${ms} ms`);
});
