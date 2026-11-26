// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The scene components of tasks 4.2–4.4 in a real headless shell. Runs only
// inside the workspace image, from a project root whose node_modules link
// reaches /opt/remotion-template/node_modules, one test file after another:
//   node --test --test-concurrency=1 "tests/render/*.test.mjs"
// CI's tests/*.test.mjs glob does not match this file.
//
// Everything runs in a scratch copy of the template with no public/, so the
// samples' image comes from the tracked gallery/public. Four independent
// steps, each started by the first test that needs it, so one can run alone
// through --test-name-pattern:
//   - probe: one probe() over every catalog sample in the kit's one theme,
//     light (task 4.6), through the gallery entry's KitProbe;
//   - eight-point probe: one probe() through the kit entry, as kit check's
//     probe stage runs it, of a line chart with 8 points and the label
//     甲ABCD乙 (task 4.7);
//   - settled: in light, each sample stilled at its entrance end and on its
//     last frame;
//   - gallery: gallery({out}) on the tracked catalog.
// Expected values come from the fixture: the caption band's top (930), the
// boxes of src/kit/scenes/boxes.ts, the samples' text fields (a number as
// formatCount of src/kit/text.ts gives it), 45 clips and 135 key frames, the
// centre colour of gallery/public/images/sample.png within ±6 per channel,
// and, for image-callouts, the geometry of src/kit/scenes/callout-zoom.ts
// with sample.png's natural 1280×960.

import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { copyFileSync, cpSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { bundle } from "@remotion/bundler";
import { openBrowser, renderStill, selectComposition } from "@remotion/renderer";
import { probe, probeFrames, violations } from "../../bin/probe.mjs";
import { ENTRANCE_FRAMES } from "../../bin/scene-timing.mjs";
import { contentSchema } from "../../bin/script-schema.mjs";
import { candidates } from "../../src/kit/gallery/catalog.ts";
import { captionBand } from "../../src/kit/render/captions.ts";
import { IMAGE_BOX, TEXT_BOX } from "../../src/kit/scenes/boxes.ts";
import { calloutZoom, project, scaleAt } from "../../src/kit/scenes/callout-zoom.ts";
import { formatCount, wrapLines } from "../../src/kit/text.ts";
import { themes } from "../../src/kit/theme.ts";

const TEMPLATE = fileURLToPath(new URL("../..", import.meta.url));
const THEMES = ["light"];
const ROLES = ["entrance-end", "midpoint", "pre-exit"];
const BAND_TOP = captionBand(themes.light).top;
// The probe's own box tolerances (task 3.7, amended): 0.5 px across, 1 px down.
const TOLERANCE = { x: 0.5, y: 1 };
const CHANNEL_TOLERANCE = 6;
const CLIPS = 45;
const KEY_FRAMES = 135;
const SAMPLE_IMAGE = { path: join(TEMPLATE, "gallery", "public", "images", "sample.png"), x: 640, y: 480, width: 1280, height: 960 };
// The image-callouts kind: its image panel zooms across the whole scene and
// its markers track the zoom.
const CALLOUTS = "image-callouts";
// A marker's glyph box centre may lie this far from its expected point.
const MARKER_TOLERANCE = 2;
const PANEL_CENTRE = { x: IMAGE_BOX.panel.left + IMAGE_BOX.panel.width / 2, y: IMAGE_BOX.panel.top + IMAGE_BOX.panel.height / 2 };

// Every (candidate, sample) of the tracked catalog, with its probe facts.
const SAMPLES = candidates.flatMap((c) =>
  c.samples.map((s) => ({ candidate: c.id, kind: c.kind, sample: s.id, entranceFrames: c.entranceFrames, durationInFrames: s.timing.durationInFrames, content: s.content, image: s.image })),
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

// The text fields of a sample's content: every string under it, depth-first
// through lists and objects (batch 4.3's steps, events and sides nest).
const stringsOf = (value) =>
  typeof value === "string" ? [value] : (Array.isArray(value) ? value : Object.values(value)).flatMap(stringsOf);

// The marked values of a sample of `kind`: the depth-first walk for the ten
// earlier kinds; for batch 4.4's, each drawn field, a number as formatCount
// gives it. Nothing for a chart's type or a callout's x and y.
const present = (value) => (value === undefined ? [] : [value]);
const fieldValues = (kind, content) => {
  if (kind === "key-number") return [formatCount(content.value), ...present(content.unit), content.label];
  if (kind === "chart") return [content.title, ...present(content.unit), ...content.points.flatMap((p) => [p.label, formatCount(p.value)])];
  if (kind === CALLOUTS) return content.callouts.map((c) => c.label);
  return stringsOf(content);
};

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
      fieldValues(item.kind, item.content).sort(),
      frame.target,
    );
  }
});

test("probe: the spec's number scenario at the entrance end, key-number wan, yi and min marking 1.2万, 1.2亿 and 9999", async () => {
  const frames = (await probed()).frames.filter((frame) => frame.role === "entrance-end");
  for (const theme of THEMES) {
    for (const [sample, shown] of [["wan", "1.2万"], ["yi", "1.2亿"], ["min", "9999"]]) {
      const id = itemId({ candidate: "key-number", sample }, theme);
      const frame = frames.find((f) => f.target === id);
      assert.ok(frame, `${id}: no entrance-end frame`);
      const inputs = frame.marked.map(({ input }) => input);
      assert.ok(inputs.includes(shown), `${id}: ${JSON.stringify(inputs)} lacks ${shown}`);
    }
  }
});

test("probe: no text record of any chart frame lies inside an svg element", async () => {
  const frames = (await probed()).frames.filter((frame) => byId.get(frame.target).kind === "chart");
  assert.ok(frames.length > 0, "no chart frame");
  for (const frame of frames) {
    for (const record of frame.texts) {
      assert.ok(!/(^|>)svg:/.test(record.path), `${frame.target} ${frame.role}: "${record.text}" at ${record.path} is inside an svg`);
    }
  }
});

// The callout zoom of an image-callouts item, with sample.png's natural size.
const zoomOf = (item) => calloutZoom({ width: SAMPLE_IMAGE.width, height: SAMPLE_IMAGE.height }, item.content.callouts);

test("probe: in every image-callouts frame, marker i + 1 is unmarked, unscaled and unclipped text in the panel, centred within 2 px of F + s(f)·(P_i − F)", async () => {
  const frames = (await probed()).frames.filter((frame) => byId.get(frame.target).kind === CALLOUTS);
  assert.equal(frames.length, ROLES.length * candidates.find((c) => c.kind === CALLOUTS).samples.length);
  for (const frame of frames) {
    const item = byId.get(frame.target);
    const { focus, points, end } = zoomOf(item);
    const s = scaleAt(end, frame.frame, item.durationInFrames);
    const unmarked = frame.texts.filter((record) => !frame.marked.some((mark) => isInside(record.path, mark.path)));
    for (const [i, p] of points.entries()) {
      const at = project(focus, p, s);
      const label = `${frame.target} ${frame.role} frame ${frame.frame} marker ${i + 1}`;
      const found = unmarked.filter((record) => {
        const g = record.glyphBox;
        const centre = { x: g.x + g.width / 2, y: g.y + g.height / 2 };
        return record.text === String(i + 1) && inside(g, IMAGE_BOX.panel) && Math.hypot(centre.x - at.x, centre.y - at.y) <= MARKER_TOLERANCE;
      });
      assert.equal(found.length, 1, `${label}: no unmarked "${i + 1}" in the panel within ${MARKER_TOLERANCE} px of ${JSON.stringify(at)}`);
      assert.equal(found[0].scale, 1, `${label}: scale ${found[0].scale}`);
      assert.deepEqual(found[0].clippedBy, [], `${label}: clipped`);
    }
  }
});

// --- the eight-point line chart -----------------------------------------------

// The line-max sample with the label 甲ABCD乙 on its first point, the other
// labels and values unchanged. At 8 points a line label has maxWidth 4, so
// it wraps to three lines.
const EIGHT_LABEL = "甲ABCD乙";
const EIGHT_LINES = ["甲", "ABCD", "乙"];
const LINE_MAX = candidates.find((c) => c.kind === "chart").samples.find((s) => s.id === "line-max");
const EIGHT = {
  ...LINE_MAX.content,
  points: LINE_MAX.content.points.map((point, i) => (i === 0 ? { ...point, label: EIGHT_LABEL } : point)),
};
const EIGHT_ID = "chart-line-8-points";

const eightProbed = once("eight-point probe", () => {
  assert.equal(EIGHT.type, "line");
  assert.equal(EIGHT.points.length, 8);
  assert.equal(contentSchema("chart").safeParse(EIGHT).success, true, "the eight-point content is schema-valid");
  const target = {
    id: EIGHT_ID,
    scene: "chart",
    props: { content: EIGHT, image: null, timing: LINE_MAX.timing, theme: themes.light },
    durationInFrames: LINE_MAX.timing.durationInFrames,
    entranceFrames: ENTRANCE_FRAMES.chart,
    exitFrame: LINE_MAX.timing.durationInFrames,
  };
  return probe({ entryPoint: join(root, "src", "kit", "index.ts"), targets: [target], publicDir: GALLERY_PUBLIC() });
});

test("eight-point probe: the 甲ABCD乙 line chart keeps every probe rule", async (t) => {
  const result = await eightProbed();
  assert.deepEqual(result.frames.map((frame) => frame.role), ROLES);
  const found = violations(result, themes.light);
  t.diagnostic(`violations: ${JSON.stringify(found)}`);
  assert.deepEqual(found, []);
});

test("eight-point probe: every text ends at or above the caption band and every marked text lies inside TEXT_BOX", async (t) => {
  for (const frame of (await eightProbed()).frames) {
    assert.ok(frame.texts.length > 0, `${frame.role}: no text`);
    const lowest = Math.max(...frame.texts.map((record) => record.glyphBox.y + record.glyphBox.height));
    t.diagnostic(`${frame.role} (frame ${frame.frame}): lowest text end ${lowest}, band top ${BAND_TOP}`);
    assert.ok(lowest <= BAND_TOP, `${frame.role}: a text ends at ${lowest}, below the band's top ${BAND_TOP}`);
    assert.ok(frame.marked.length > 0, `${frame.role}: no marked text`);
    const boxes = [];
    for (const mark of frame.marked) {
      const records = frame.texts.filter((record) => isInside(record.path, mark.path));
      assert.ok(records.length > 0, `${frame.role}: "${mark.input}" has no text record`);
      for (const record of records) {
        boxes.push(record.glyphBox);
        assert.ok(inside(record.glyphBox, TEXT_BOX), `${frame.role}: "${record.text}" at ${JSON.stringify(record.glyphBox)} is outside ${JSON.stringify(TEXT_BOX)}`);
      }
    }
    const left = Math.min(...boxes.map((b) => b.x));
    const top = Math.min(...boxes.map((b) => b.y));
    const right = Math.max(...boxes.map((b) => b.x + b.width));
    const bottom = Math.max(...boxes.map((b) => b.y + b.height));
    t.diagnostic(`${frame.role}: ${boxes.length} marked records span x ${left}–${right}, y ${top}–${bottom}; TEXT_BOX ${JSON.stringify(TEXT_BOX)}`);
  }
});

test("eight-point probe: the 甲ABCD乙 label is drawn on 3 lines in every probed frame", async (t) => {
  assert.deepEqual(wrapLines(EIGHT_LABEL, 4), EIGHT_LINES);
  for (const frame of (await eightProbed()).frames) {
    const marks = frame.marked.filter((mark) => mark.input === EIGHT_LABEL);
    assert.equal(marks.length, 1, `${frame.role}: ${marks.length} marks for ${EIGHT_LABEL}`);
    const lines = frame.texts.filter((record) => isInside(record.path, marks[0].path)).flatMap((record) => record.lines);
    t.diagnostic(`${frame.role} (frame ${frame.frame}): ${JSON.stringify(lines)}`);
    assert.deepEqual(lines, EIGHT_LINES, frame.role);
  }
});

// --- settled ------------------------------------------------------------------

// Outside IMAGE_BOX.panel: the four strips around it, as ffmpeg crops
// (width:height:x:y).
const PANEL = IMAGE_BOX.panel;
const AROUND_PANEL = [
  `1920:${PANEL.top}:0:0`,
  `1920:${1080 - PANEL.top - PANEL.height}:0:${PANEL.top + PANEL.height}`,
  `${PANEL.left}:${PANEL.height}:0:${PANEL.top}`,
  `${1920 - PANEL.left - PANEL.width}:${PANEL.height}:${PANEL.left + PANEL.width}:${PANEL.top}`,
];

const rgb = (path, crop = null) =>
  execFileSync("ffmpeg", ["-nostdin", "-v", "error", "-i", path, ...(crop === null ? [] : ["-vf", `crop=${crop}`]), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], {
    maxBuffer: 64 * 1024 * 1024,
  });

const stills = once("settled stills", async () => {
  assert.ok(SAMPLES.length > 0, "the catalog lists no candidate");
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
      pairs.push({ id: itemId(s, "light"), kind: s.kind, paths });
    }
    return pairs;
  } finally {
    await browser.close({ silent: true });
  }
});

test("settled: in light, every sample's still at its entrance end equals its last frame byte for byte (image-callouts outside its zooming panel)", async () => {
  const pairs = await stills();
  assert.equal(pairs.length, SAMPLES.length);
  for (const { id, kind, paths } of pairs) {
    if (kind === CALLOUTS) {
      for (const crop of AROUND_PANEL) {
        const [entranceEnd, last] = paths.map((path) => rgb(path, crop));
        const [w, h] = crop.split(":").map(Number);
        assert.equal(entranceEnd.length, w * h * 3, `${id} ${crop}`);
        assert.ok(entranceEnd.equals(last), `${id}: outside the panel (${crop}) the entrance-end still differs from the last frame`);
      }
      continue;
    }
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

test("gallery: the tracked catalog gives 45 clips, 135 key frames and no violation on any item", async () => {
  const { out, items } = await galleryRun();
  assert.equal(items, CLIPS);
  assert.equal(readdirSync(join(out, "clips")).length, CLIPS);
  assert.equal(readdirSync(join(out, "frames")).length, KEY_FRAMES);
  const data = JSON.parse(readFileSync(join(out, "gallery.json"), "utf8"));
  assert.equal(data.items.length, CLIPS);
  for (const item of data.items) assert.deepEqual(item.violations, [], item.id);
});

// The sample pairs whose image sample shows sample.png at the panel's centre
// and whose plain sample does not: image against max, and for the chart
// also line-image against line-max. image-callouts has its own check.
const imagePairs = (c) => (c.kind === "chart" ? [["image", "max"], ["line-image", "line-max"]] : [["image", "max"]]);

test("gallery: each candidate's image sample shows sample.png's centre at the panel's centre, and its max sample does not (the chart's line samples too)", async () => {
  assert.ok(candidates.length > 0, "the catalog lists no candidate");
  const { out } = await galleryRun();
  const expected = pixel(SAMPLE_IMAGE.path, SAMPLE_IMAGE.x, SAMPLE_IMAGE.y);
  const near = (actual) => actual.every((channel, i) => Math.abs(channel - expected[i]) <= CHANNEL_TOLERANCE);
  for (const c of candidates.filter((c) => c.kind !== CALLOUTS)) {
    for (const [withImage, plain] of imagePairs(c)) {
      const shown = pixel(join(out, "frames", `${c.id}--${withImage}--light--entrance-end.png`), PANEL_CENTRE.x, PANEL_CENTRE.y);
      assert.ok(near(shown), `${c.id} ${withImage}: ${JSON.stringify(shown)} is not sample.png's ${JSON.stringify(expected)}`);
      const other = pixel(join(out, "frames", `${c.id}--${plain}--light--entrance-end.png`), PANEL_CENTRE.x, PANEL_CENTRE.y);
      assert.ok(!near(other), `${c.id} ${plain}: ${JSON.stringify(other)} matches sample.png's ${JSON.stringify(expected)}`);
    }
  }
});

test("gallery: in each image-callouts key frame (light), sample.png's centre shows at the projected image centre F + s(f)·(C − F)", async () => {
  const c = candidates.find((c) => c.kind === CALLOUTS);
  assert.ok(c, "the catalog has no image-callouts candidate");
  const { out } = await galleryRun();
  const expected = pixel(SAMPLE_IMAGE.path, SAMPLE_IMAGE.x, SAMPLE_IMAGE.y);
  const near = (actual) => actual.every((channel, i) => Math.abs(channel - expected[i]) <= CHANNEL_TOLERANCE);
  for (const s of c.samples) {
    const item = byId.get(itemId({ candidate: c.id, sample: s.id }, "light"));
    const { picture, focus, end } = zoomOf(item);
    const centre = { x: picture.left + picture.width / 2, y: picture.top + picture.height / 2 };
    for (const { role, frame } of probeFrames({ durationInFrames: item.durationInFrames, entranceFrames: item.entranceFrames, exitFrame: item.durationInFrames })) {
      const at = project(focus, centre, scaleAt(end, frame, item.durationInFrames));
      const shown = pixel(join(out, "frames", `${item.id}--${role}.png`), Math.round(at.x), Math.round(at.y));
      assert.ok(near(shown), `${item.id} ${role} (frame ${frame}) at ${JSON.stringify(at)}: ${JSON.stringify(shown)} is not sample.png's ${JSON.stringify(expected)}`);
    }
  }
});

test("timings", (t) => {
  for (const [name, ms] of Object.entries(timings)) t.diagnostic(`${name}: ${ms} ms`);
});
