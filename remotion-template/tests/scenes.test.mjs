// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The scene components of tasks 4.2–4.4: their catalog entries and
// samples, the layout boxes, a source scan of src/kit/scenes/, and the
// preservation of what the approvals of batches 4.2 and 4.3 cover. Pure: the catalog, the
// boxes, the theme and the timing tables are imported through Node's type
// stripping; the scene files are read as text, because Node cannot import
// TSX. Expected kinds, numbers, lengths, required fields and digests are the
// fixtures' literals; field lengths are computed against LIMITS.content.
// Batch 4.3's kinds nest (steps[] and events[] are objects, and comparison's
// left and right hold points[]), so the sample checks walk every content
// value depth-first. Batch 4.4 adds scalars that are not text (a number's
// `value`, a chart's `type`, a callout's `x` and `y`), checked by their
// schema and never as Han; its image-callouts samples all carry the image,
// and its callout geometry comes from src/kit/scenes/callout-zoom.ts.

import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { ENTRANCE_FRAMES, MIN_SCENE_FRAMES } from "../bin/scene-timing.mjs";
import { LIMITS, contentSchema } from "../bin/script-schema.mjs";
import { candidates, validateCatalog } from "../src/kit/gallery/catalog.ts";
import { captionBand } from "../src/kit/render/captions.ts";
import { IMAGE_BOX, TEXT_BOX } from "../src/kit/scenes/boxes.ts";
import { S_MAX, calloutZoom } from "../src/kit/scenes/callout-zoom.ts";
import { formatCount } from "../src/kit/text.ts";
import { themes } from "../src/kit/theme.ts";

const TEMPLATE = fileURLToPath(new URL("..", import.meta.url));
const SCENES = join(TEMPLATE, "src", "kit", "scenes");

// The fixture's kinds, in catalog order, with each scene file.
const KINDS = [
  "hook",
  "question",
  "concept-card",
  "definition-quote",
  "summary",
  "end-card",
  "process-steps",
  "cause-effect",
  "comparison",
  "timeline",
  "key-number",
  "chart",
  "image-callouts",
];
const FILES = {
  hook: "Hook",
  question: "Question",
  "concept-card": "ConceptCard",
  "definition-quote": "DefinitionQuote",
  summary: "Summary",
  "end-card": "EndCard",
  "process-steps": "ProcessSteps",
  "cause-effect": "CauseEffect",
  comparison: "Comparison",
  timeline: "Timeline",
  "key-number": "KeyNumber",
  chart: "Chart",
  "image-callouts": "ImageCallouts",
};
// Entrance 15 for every kind; minimum 60, and 90 for the end card.
const ENTRANCE = 15;
const MINIMUM = {
  hook: 60,
  question: 60,
  "concept-card": 60,
  "definition-quote": 60,
  summary: 60,
  "end-card": 90,
  "process-steps": 60,
  "cause-effect": 60,
  comparison: 60,
  timeline: 60,
  "key-number": 60,
  chart: 60,
  "image-callouts": 60,
};
// The required content fields of each kind (bin/script-schema.mjs CONTENT),
// as paths: `a.b` is field b of object a, `a[].b` field b of each item of
// list a.
const REQUIRED = {
  hook: ["title"],
  question: ["question"],
  "concept-card": ["title", "points"],
  "definition-quote": ["term", "text"],
  summary: ["points"],
  "end-card": ["title"],
  "process-steps": ["steps", "steps[].label"],
  "cause-effect": ["cause", "effect"],
  comparison: ["left", "left.title", "left.points", "right", "right.title", "right.points"],
  timeline: ["events", "events[].year", "events[].label"],
  "key-number": ["value", "label"],
  chart: ["type", "title", "points", "points[].label", "points[].value"],
  "image-callouts": ["callouts", "callouts[].x", "callouts[].y", "callouts[].label"],
};
const LISTS = new Set(["points", "steps", "events", "callouts"]);
// Each candidate's sample ids, in order: max, image and min, except where
// batch 4.4's catalog lists more. The samples on the 150-frame timing; every
// other sample is as long as minFrames.
const SAMPLE_IDS = {
  "key-number": ["max", "image", "min", "wan", "yi"],
  chart: ["max", "image", "min", "line-max", "line-image", "line-min"],
};
const sampleIds = (kind) => SAMPLE_IDS[kind] ?? ["max", "image", "min"];
const MAX_TIMED = new Set(["max", "image", "line-max", "line-image"]);
// The kind whose every sample carries the image, because the kind requires one.
const IMAGE_KIND = "image-callouts";
const SAMPLE_PNG = { width: 1280, height: 960 };
const FULL_WIDTH_CLOSING = new Set([..."，。！？、；：）」』”’》】…"]);
const HAN = /^\p{Script=Han}+$/u;
const MAX_TIMING = { durationInFrames: 150, narrationEndFrame: 130, frames: [[[0, 60], [62, 130]]] };

const codePoints = (text) => Array.from(text);
const byKind = (kind) => candidates.find((c) => c.kind === kind);
const sample = (kind, id) => byKind(kind).samples.find((s) => s.id === id);
const frames = (timing) => timing.sentences.map((s) => s.blocks.map((b) => [b.startFrame, b.endFrame]));

// A maximum-length Han field: Han code points, then one full-width closing mark.
function assertMaxHan(text, length, label) {
  assert.equal(typeof text, "string", label);
  const points = codePoints(text);
  assert.equal(points.length, length, `${label}: ${text}`);
  assert.match(points.slice(0, -1).join(""), HAN, `${label}: ${text}`);
  assert.ok(FULL_WIDTH_CLOSING.has(points.at(-1)), `${label}: ${text} ends with ${points.at(-1)}`);
}

function assertTwoHan(text, label) {
  assert.equal(typeof text, "string", label);
  assert.equal(codePoints(text).length, 2, `${label}: ${text}`);
  assert.match(text, HAN, `${label}: ${text}`);
}

const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

// Every value under a content object, depth-first, as {path, field, value,
// schema, text}. `path` names it (`steps[1].label`), `shape` drops the
// indices (`steps[].label`), and `field` is the LIMITS.content key of its
// limit: its own key, or `point` for an item of a points list. `schema` is
// its zod schema, optional wrappers removed. A scalar that is not text (a
// number, or an enum such as a chart's `type`) is checked here against its
// schema, `x` and `y` within 0–1, and carries `text: false`, so it never
// reaches a Han check.
function* walk(value, schema, path = "", shape = "", field = null) {
  if (path !== "" && !Array.isArray(value) && !isObject(value)) {
    const type = schema.def.type;
    if (type === "number") {
      assert.ok(typeof value === "number" && Number.isFinite(value), `${path}: ${value} is not a finite number`);
      if (field === "x" || field === "y") assert.ok(value >= 0 && value <= 1, `${path}: ${value} is outside 0–1`);
    } else if (type === "enum") {
      assert.ok(schema.options.includes(value), `${path}: ${JSON.stringify(value)} is not one of ${schema.options}`);
    } else {
      assert.equal(type, "string", `${path}: unexpected ${type}`);
    }
    yield { path, shape, field, value, schema, text: type === "string" };
    return;
  }
  if (path !== "") yield { path, shape, field, value, schema, text: false };
  if (Array.isArray(value)) {
    for (const [i, item] of value.entries()) {
      yield* walk(item, schema.element, `${path}[${i}]`, `${shape}[]`, field === "points" ? "point" : null);
    }
  } else if (isObject(value)) {
    for (const [key, item] of Object.entries(value)) {
      assert.ok(Object.hasOwn(schema.shape, key), `${path || "content"} has no schema field ${key}`);
      const own = schema.shape[key];
      const at = path === "" ? key : `${path}.${key}`;
      yield* walk(item, own.def.type === "optional" ? own.unwrap() : own, at, shape === "" ? key : `${shape}.${key}`, key);
    }
  }
}

// --- catalog ------------------------------------------------------------------

test("the tracked catalog validates and lists exactly the thirteen kinds, one candidate each, id equal to kind", () => {
  assert.equal(validateCatalog(candidates), undefined);
  assert.deepEqual(
    candidates.map(({ id, kind }) => [id, kind]),
    KINDS.map((kind) => [kind, kind]),
  );
});

test("each candidate's source is its scene file, and its frames are the timing tables', 15 and 60 (90 for the end card)", () => {
  for (const kind of KINDS) {
    const c = byKind(kind);
    assert.equal(c.source, `src/kit/scenes/${FILES[kind]}.tsx`, kind);
    assert.ok(statSync(join(TEMPLATE, c.source)).isFile(), c.source);
    assert.equal(c.minFrames, MIN_SCENE_FRAMES[kind], kind);
    assert.equal(c.entranceFrames, ENTRANCE_FRAMES[kind], kind);
    assert.equal(c.minFrames, MINIMUM[kind], kind);
    assert.equal(c.entranceFrames, ENTRANCE, kind);
  }
});

// --- samples ------------------------------------------------------------------

test("every candidate has the samples max, image and min, in that order, and batch 4.4's listed extras after them", () => {
  for (const kind of KINDS) assert.deepEqual(byKind(kind).samples.map((s) => s.id), sampleIds(kind), kind);
});

test("max: every content field at its LIMITS.content length in Han ending in a full-width closing mark, every list at its maximum", () => {
  for (const kind of KINDS) {
    const { content, image } = sample(kind, "max");
    const limits = LIMITS.content[kind];
    const schema = contentSchema(kind);
    assert.deepEqual(Object.keys(content).sort(), Object.keys(schema.shape).sort(), `${kind}: every field present`);
    for (const { path, field, value, schema: own, text } of walk(content, schema)) {
      if (Array.isArray(value)) {
        assert.ok(LISTS.has(field), `${kind}.${path} is a list`);
        assert.equal(value.length, limits[field].max, `${kind}.${path} count`);
      } else if (isObject(value)) {
        assert.deepEqual(Object.keys(value).sort(), Object.keys(own.shape).sort(), `${kind}.${path}: every field present`);
      } else if (text) {
        assertMaxHan(value, limits[field], `${kind}.${path}`);
      }
    }
    assert.equal(image, kind === IMAGE_KIND ? "sample.png" : null, kind);
  }
});

test("max and image: a 150-frame timing, narration ending at 130, one sentence of two blocks at [0, 60) and [62, 130)", () => {
  for (const kind of KINDS) {
    for (const id of ["max", "image"]) {
      const { timing } = sample(kind, id);
      assert.equal(timing.durationInFrames, MAX_TIMING.durationInFrames, `${kind}/${id}`);
      assert.equal(timing.narrationEndFrame, MAX_TIMING.narrationEndFrame, `${kind}/${id}`);
      assert.deepEqual(frames(timing), MAX_TIMING.frames, `${kind}/${id}`);
    }
  }
});

test("image: max's content and timing, with image sample.png (image-callouts aside)", () => {
  for (const kind of KINDS.filter((kind) => kind !== IMAGE_KIND)) {
    const max = sample(kind, "max");
    const withImage = sample(kind, "image");
    assert.deepEqual(withImage.content, max.content, kind);
    assert.deepEqual(withImage.timing, max.timing, kind);
    assert.equal(withImage.image, "sample.png", kind);
  }
});

test("min: required fields only, two Han characters each, lists at their minimum, no image", () => {
  for (const kind of KINDS) {
    const { content, image } = sample(kind, "min");
    const found = [...walk(content, contentSchema(kind))];
    const fields = new Set(found.filter(({ field }) => field !== null && field !== "point").map(({ shape }) => shape));
    assert.deepEqual([...fields].sort(), [...REQUIRED[kind]].sort(), kind);
    for (const { path, field, value, text } of found) {
      if (Array.isArray(value)) {
        assert.ok(LISTS.has(field), `${kind}.${path} is a list`);
        assert.equal(value.length, LIMITS.content[kind][field].min, `${kind}.${path} count`);
      } else if (text) {
        assertTwoHan(value, `${kind}.${path}`);
      }
    }
    assert.equal(image, kind === IMAGE_KIND ? "sample.png" : null, kind);
  }
});

test("min: as long as minFrames, narration in one block from 0 to minFrames − 20; the end card's has no sentence", () => {
  for (const kind of KINDS) {
    const { timing } = sample(kind, "min");
    const minFrames = MINIMUM[kind];
    assert.equal(timing.durationInFrames, minFrames, kind);
    if (kind === "end-card") {
      assert.equal(timing.narrationEndFrame, 0);
      assert.deepEqual(timing.sentences, []);
    } else {
      assert.equal(timing.narrationEndFrame, minFrames - 20, kind);
      assert.deepEqual(frames(timing), [[[0, minFrames - 20]]], kind);
    }
  }
});

// --- batch 4.4's sample rules ----------------------------------------------------

const codePointCount = (text) => codePoints(text).length;
const sameExcept = (a, b, label) => {
  assert.deepEqual(a.image, b.image, `${label}: image`);
  assert.deepEqual(a.timing, b.timing, `${label}: timing`);
};

test("key-number: max's value is -99999999 (8 code points as -9999.9万); min is 9999 without a unit; wan and yi are min at 12345 and 123456789", () => {
  const max = sample("key-number", "max");
  assert.equal(max.content.value, -99999999);
  assert.equal(formatCount(max.content.value), "-9999.9万");
  assert.equal(codePointCount(formatCount(max.content.value)), 8);
  const min = sample("key-number", "min");
  assert.equal(min.content.value, 9999);
  assert.equal(Object.hasOwn(min.content, "unit"), false);
  for (const [id, value] of [["wan", 12345], ["yi", 123456789]]) {
    const extra = sample("key-number", id);
    assert.deepEqual(extra.content, { ...min.content, value }, id);
    sameExcept(extra, min, id);
  }
  assert.deepEqual(["min", "wan", "yi"].map((id) => formatCount(sample("key-number", id).content.value)), ["9999", "1.2万", "1.2亿"]);
});

test("chart: max is a bar chart whose values hold -99999999, another of magnitude ≥ 1e4, another negative and 0; min is a two-point bar chart; each line sample equals its bar sample but for type", () => {
  const max = sample("chart", "max");
  assert.equal(max.content.type, "bar");
  const values = max.content.points.map((p) => p.value);
  const at = values.indexOf(-99999999);
  assert.ok(at >= 0, `${values} lacks -99999999`);
  assert.equal(formatCount(values[at]), "-9999.9万");
  const others = values.filter((_, i) => i !== at);
  assert.ok(others.some((v) => Math.abs(v) >= 1e4), `${values}: no other value of magnitude ≥ 1e4`);
  assert.ok(others.some((v) => v < 0), `${values}: no other negative value`);
  assert.ok(values.includes(0), `${values}: no 0`);
  for (const v of values) assert.ok(codePointCount(formatCount(v)) <= 8, `${v} formats to ${formatCount(v)}`);
  const min = sample("chart", "min");
  assert.equal(min.content.type, "bar");
  assert.equal(min.content.points.length, 2);
  for (const id of ["max", "image", "min"]) {
    const bar = sample("chart", id);
    const line = sample("chart", `line-${id}`);
    assert.equal(line.content.type, "line", `line-${id}`);
    assert.deepEqual({ ...line.content, type: "bar" }, bar.content, `line-${id}`);
    sameExcept(line, bar, `line-${id}`);
  }
});

test("image-callouts: every sample has sample.png and no callout near the centre; max holds the three corners, image the same labels inside, min one callout at (0, 0)", () => {
  const c = byKind("image-callouts");
  for (const s of c.samples) {
    assert.equal(s.image, "sample.png", s.id);
    for (const { x, y } of s.content.callouts) {
      assert.ok(!(Math.abs(x - 0.5) < 0.15 && Math.abs(y - 0.5) < 0.15), `${s.id}: (${x}, ${y}) covers the image's centre`);
    }
  }
  const max = sample("image-callouts", "max");
  const image = sample("image-callouts", "image");
  const min = sample("image-callouts", "min");
  assert.deepEqual(max.content.callouts.map(({ x, y }) => [x, y]), [[0, 0], [1, 0], [1, 1]]);
  assert.deepEqual(image.content.callouts.map((c) => c.label), max.content.callouts.map((c) => c.label));
  for (const { x, y } of image.content.callouts) assert.ok(x > 0 && x < 1 && y > 0 && y < 1, `image: (${x}, ${y}) is not inside`);
  assert.deepEqual(image.timing, max.timing);
  assert.deepEqual(min.content.callouts.map(({ x, y }) => [x, y]), [[0, 0]]);
});

test("image-callouts through callout-zoom.ts with sample.png's 1280×960: max gives S = 1, image and min S = S_MAX, image's points lie ≥ 160 px from F, and min's image centre moves ≥ 0.15 of R across and down", () => {
  const zoomOf = (id) => calloutZoom(SAMPLE_PNG, sample("image-callouts", id).content.callouts);
  assert.equal(zoomOf("max").end, 1);
  assert.equal(zoomOf("image").end, S_MAX);
  assert.equal(zoomOf("min").end, S_MAX);
  const { focus, points } = zoomOf("image");
  for (const [i, p] of points.entries()) {
    const distance = Math.hypot(p.x - focus.x, p.y - focus.y);
    assert.ok(distance >= 160, `image: P${i + 1} lies ${distance} px from F`);
  }
  const { picture, focus: corner, end } = zoomOf("min");
  const centre = { x: picture.left + picture.width / 2, y: picture.top + picture.height / 2 };
  assert.ok((end - 1) * (centre.x - corner.x) >= 0.15 * picture.width, "min: the centre moves less than 0.15 of R across");
  assert.ok((end - 1) * (centre.y - corner.y) >= 0.15 * picture.height, "min: the centre moves less than 0.15 of R down");
});

test("the samples' image is a 1280×960 PNG of at most 300 KB in gallery/public/images/", () => {
  const path = join(TEMPLATE, "gallery", "public", "images", "sample.png");
  const bytes = readFileSync(path);
  assert.ok(bytes.length <= 300 * 1024, `${bytes.length} bytes`);
  assert.deepEqual([...bytes.subarray(0, 8)], [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a], "PNG signature");
  assert.equal(bytes.subarray(12, 16).toString("latin1"), "IHDR");
  assert.deepEqual([bytes.readUInt32BE(16), bytes.readUInt32BE(20)], [1280, 960]);
});

// --- boxes --------------------------------------------------------------------

const right = (box) => box.left + box.width;
const bottom = (box) => box.top + box.height;

test("TEXT_BOX, IMAGE_BOX.text and IMAGE_BOX.panel lie in the safe area and end at or above the caption band", () => {
  const band = captionBand(themes.light);
  assert.equal(band.top, 930);
  for (const name of ["light", "dark"]) {
    const { canvas, safeArea } = themes[name];
    for (const [label, box] of [["TEXT_BOX", TEXT_BOX], ["IMAGE_BOX.text", IMAGE_BOX.text], ["IMAGE_BOX.panel", IMAGE_BOX.panel]]) {
      assert.deepEqual(Object.keys(box).sort(), ["height", "left", "top", "width"], label);
      assert.ok(Object.values(box).every((v) => Number.isInteger(v) && v >= 0), `${label}: ${JSON.stringify(box)}`);
      assert.ok(box.left >= safeArea.left, `${name} ${label} left`);
      assert.ok(box.top >= safeArea.top, `${name} ${label} top`);
      assert.ok(right(box) <= canvas.width - safeArea.right, `${name} ${label} right`);
      assert.ok(bottom(box) <= canvas.height - safeArea.bottom, `${name} ${label} bottom`);
      assert.ok(bottom(box) <= band.top, `${name} ${label} ends at ${bottom(box)}, below the band's top`);
    }
  }
});

test("the image layout's text column and panel do not overlap", () => {
  const { text, panel } = IMAGE_BOX;
  const apart = right(text) <= panel.left || right(panel) <= text.left || bottom(text) <= panel.top || bottom(panel) <= text.top;
  assert.ok(apart, `${JSON.stringify(text)} overlaps ${JSON.stringify(panel)}`);
});

// --- source scan --------------------------------------------------------------

const SCENE_FILES = KINDS.map((kind) => `${FILES[kind]}.tsx`);
// Batch 4.3's and 4.4's choreography sources, by scene file; CauseEffect
// has none. Chart's header also names line-chart/LineChart.tsx in prose.
const ONDA = "// Source: degueba/onda 3c814051269597b31fee1603bef7ccfb93d091b6 registry/components/";
const CHOREOGRAPHY = {
  "ProcessSteps.tsx": "progress-steps/ProgressSteps.tsx",
  "Timeline.tsx": "timeline/Timeline.tsx",
  "Comparison.tsx": "split-screen/SplitScreen.tsx",
  "CauseEffect.tsx": null,
  "KeyNumber.tsx": "count-up/CountUp.tsx",
  "Chart.tsx": "bar-chart/BarChart.tsx",
  "ImageCallouts.tsx": "ken-burns/KenBurns.tsx",
};
const ONLY_CHOREOGRAPHY = "Only the choreography comes from the source above, no code";
// What batch 4.2's approval covers, at origin/main 911e89a: the sha256 of
// its six scene files and boxes.ts, and the length in bytes and sha256 of
// layout.tsx, which later batches may only append to.
const PRESERVED = {
  "Hook.tsx": "e9981f69f8e373b2dc45934a6024c24c637048722987a1cd4660b72c95fb06d7",
  "Question.tsx": "f2f43887818a95683c3a4455c3ffc620c048d46fb1e67ce39b82a0b8f844ccec",
  "ConceptCard.tsx": "cfca7e04d5eb168f9cd6c614f1dcfcceb4164135f6ebbbed27df5c3661701169",
  "DefinitionQuote.tsx": "6de33898febd23c4315a11a475711424e6c55a1d95d840b6cea454317d95338a",
  "Summary.tsx": "fe4cea4764768d0163826bc932403a0249e89f1fcfb757c3de70817d39f712d2",
  "EndCard.tsx": "45b444ff45eeef00202f28985597bb5d60ab249b6b2baec06aab53abe66bfdc1",
};
const PRESERVED_BOXES = "6da0ba2af4be9a7c8af1f4428988211392bb65be4c994e332e44289def4270f4";
const LAYOUT_PREFIX = { bytes: 7006, sha256: "edc72567409ef87ecb0e72981dfdd4cdefe6ba6a36e5df8ba6bbde22f5a38782" };
// What batch 4.3's approval covers, at origin/main 49d562f: the sha256 of
// its four scene files, and the length in bytes and sha256 of layout.tsx.
const PRESERVED_4_3 = {
  "ProcessSteps.tsx": "516b64a80c84355b0e16933821891ee121fc6ddb06783bcdf5ac681675e68a2c",
  "CauseEffect.tsx": "964091eae6b54f8688b8c7074b98613f320f5a4b8c7bddee41c06016d4c2835d",
  "Comparison.tsx": "681b6695fb01330574a71cda76949f1d90e7b5c991ca851289b1eabc51e7cf2c",
  "Timeline.tsx": "ced7b51c6e1b2a4c8e83656b4e8e779e32764e661521a60205683610a4cf9461",
};
const LAYOUT_PREFIX_4_3 = { bytes: 9968, sha256: "8025c13e2886e97aca621d83f651a080f3b6902daa779729bcf523e1b97e6e07" };
const FORBIDDEN = [
  "nowrap",
  "toUpperCase",
  "textTransform",
  "writingMode",
  "rotate",
  "contents",
  "inline-block",
  "inline-flex",
  "verticalAlign",
  "overflowX",
  "overflowY",
  "clipPath",
  "foreignObject",
  "<span",
  "fetch(",
  "http:",
  "https:",
  "lineHeight",
  "letterSpacing",
];
// Batch 4.4 lifts the <svg ban in Chart.tsx alone, for the line and its
// dots: there it appears exactly once, and every file keeps these out, so no
// text, clip, mask, image or reference sits in an svg.
const SVG = "<svg";
const SVG_FILE = "Chart.tsx";
const SVG_FORBIDDEN = ["<text", "<tspan", "<textPath", "foreignObject", "<clipPath", "<mask", "<image", "<use"];
const FORBIDDEN_PATTERNS = [
  ["a string-literal fontFamily", /fontFamily\s*:\s*['"`]/],
  ["a numeric literal fontSize", /fontSize\s*:\s*[-+]?[\d.]/],
  ["a hex colour", /#[0-9A-Fa-f]{3,8}\b/],
  ["rgb(", /\brgba?\s*\(/],
  ["hsl(", /\bhsla?\s*\(/],
  ["a SPRING_ identifier", /\bSPRING_\w*/],
];

const sources = () =>
  Object.fromEntries(
    readdirSync(SCENES)
      .filter((name) => statSync(join(SCENES, name)).isFile())
      .sort()
      .map((name) => [name, readFileSync(join(SCENES, name), "utf8")]),
  );

const importSpecifiers = (source) => [...source.matchAll(/^\s*import\b[^;]*?\bfrom\s*['"]([^'"]+)['"]/gm)].map((m) => m[1]);

// The braces-balanced body that starts at the `{` at index `open`.
function block(source, open) {
  let depth = 0;
  for (let i = open; i < source.length; i++) {
    if (source[i] === "{") depth++;
    if (source[i] === "}" && --depth === 0) return source.slice(open, i + 1);
  }
  throw new Error(`unbalanced braces from ${open}`);
}

test("src/kit/scenes holds the thirteen scene files, boxes.ts, callout-zoom.ts and layout.tsx, each with the FSL header", () => {
  const files = sources();
  assert.deepEqual(Object.keys(files), [...SCENE_FILES, "boxes.ts", "callout-zoom.ts", "layout.tsx"].sort());
  for (const [name, source] of Object.entries(files)) {
    const [first, second] = source.split("\n");
    assert.equal(first, "// SPDX-License-Identifier: FSL-1.1-Apache-2.0", name);
    assert.equal(second, "// Copyright (c) 2025 Open Computer Use Contributors", name);
  }
});

test("each scene file's exported component returns an AbsoluteFill root", () => {
  const files = sources();
  for (const name of SCENE_FILES) {
    const component = name.replace(/\.tsx$/, "");
    const source = files[name];
    const start = source.search(new RegExp(`^export const ${component} = \\(`, "m"));
    assert.ok(start >= 0, `${name} exports no ${component}`);
    const arrow = source.indexOf("=> {", start);
    assert.ok(arrow >= 0, `${name}: ${component} has no body`);
    const body = block(source, arrow + 3);
    const returns = [...body.matchAll(/\breturn\s*\(\s*(<[A-Za-z.]+)/g)].map((m) => m[1]);
    assert.deepEqual(returns, ["<AbsoluteFill"], `${name}: ${component} returns ${JSON.stringify(returns)}`);
  }
});

test("no scene file uses a construct the probe cannot judge or a value the theme should give", () => {
  for (const [name, source] of Object.entries(sources())) {
    for (const token of FORBIDDEN) assert.ok(!source.includes(token), `${name} contains ${JSON.stringify(token)}`);
    for (const [label, pattern] of FORBIDDEN_PATTERNS) {
      const found = pattern.exec(source);
      assert.equal(found, null, `${name} has ${label}: ${found?.[0]}`);
    }
  }
});

test("<svg appears exactly once in Chart.tsx and in no other file, and no file holds svg text, clips, masks, images or references", () => {
  const files = sources();
  assert.equal(typeof files[SVG_FILE], "string", `${SVG_FILE} is missing`);
  for (const [name, source] of Object.entries(files)) {
    const count = source.split(SVG).length - 1;
    assert.equal(count, name === SVG_FILE ? 1 : 0, `${name} contains ${JSON.stringify(SVG)} ${count} times`);
    for (const token of SVG_FORBIDDEN) assert.ok(!source.includes(token), `${name} contains ${JSON.stringify(token)}`);
  }
});

test("scene files import no node: built-in, and from Onda only STAGGER of ../onda/motion.ts", () => {
  for (const [name, source] of Object.entries(sources())) {
    const specifiers = importSpecifiers(source);
    assert.deepEqual(specifiers.filter((s) => s.startsWith("node:")), [], name);
    for (const match of source.matchAll(/^\s*import\s*\{([^}]*)\}\s*from\s*['"]([^'"]*onda[^'"]*)['"]/gm)) {
      assert.equal(match[2], "../onda/motion.ts", `${name}: ${match[0]}`);
      assert.deepEqual(match[1].split(",").map((s) => s.trim()).filter(Boolean), ["STAGGER"], `${name}: ${match[0]}`);
    }
    assert.equal(
      specifiers.filter((s) => s.includes("onda")).length,
      [...source.matchAll(/^\s*import\s*\{[^}]*\}\s*from\s*['"][^'"]*onda[^'"]*['"]/gm)].length,
      `${name}: an Onda import other than a named one`,
    );
  }
});

test("data-kit-text appears once, inside layout.tsx's Lines", () => {
  const files = sources();
  for (const [name, source] of Object.entries(files)) {
    if (name !== "layout.tsx") assert.ok(!source.includes("data-kit-text"), `${name} marks text outside Lines`);
  }
  const layout = files["layout.tsx"];
  assert.equal(layout.split("data-kit-text").length - 1, 1, "layout.tsx marks text once");
  const start = layout.search(/^export const Lines = \(/m);
  assert.ok(start >= 0, "layout.tsx exports Lines");
  const next = layout.slice(start + 1).search(/^export /m);
  const lines = next < 0 ? layout.slice(start) : layout.slice(start, start + 1 + next);
  assert.ok(lines.includes("data-kit-text={text}"), "Lines marks its element with its text");
});

test("batch 4.3's and 4.4's scene files name their choreography source on line 3, and CauseEffect names none", () => {
  const files = sources();
  for (const [name, path] of Object.entries(CHOREOGRAPHY)) {
    const source = files[name];
    assert.equal(typeof source, "string", `${name} is missing`);
    const lines = source.split("\n");
    const header = lines
      .slice(0, lines.findIndex((line) => !line.startsWith("//")))
      .map((line) => line.replace(/^\/\/\s?/, ""))
      .join(" ");
    if (path === null) {
      assert.ok(!/^\/\/ Source:/m.test(source), `${name} names a source`);
    } else {
      assert.equal(lines[2], `${ONDA}${path} MIT`, name);
      assert.ok(header.includes(ONLY_CHOREOGRAPHY), `${name}'s header lacks "${ONLY_CHOREOGRAPHY}"`);
    }
  }
});

test("Chart.tsx's header also names Onda's line-chart/LineChart.tsx, the choreography of its line", () => {
  const lines = sources()["Chart.tsx"].split("\n");
  const header = lines.slice(0, lines.findIndex((line) => !line.startsWith("//"))).join("\n");
  assert.ok(header.includes("registry/components/line-chart/LineChart.tsx"), "Chart.tsx's header does not name line-chart/LineChart.tsx");
});

// --- preservation -------------------------------------------------------------

const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");

test("batch 4.2's scene files and boxes.ts hash to their sha256 at 911e89a (a file removed in step 7 is skipped)", () => {
  for (const [name, digest] of Object.entries(PRESERVED)) {
    const path = join(SCENES, name);
    if (!existsSync(path)) continue;
    assert.equal(sha256(readFileSync(path)), digest, name);
  }
  assert.equal(sha256(readFileSync(join(SCENES, "boxes.ts"))), PRESERVED_BOXES, "boxes.ts");
});

test("layout.tsx at 911e89a is a byte-exact prefix of layout.tsx", () => {
  const bytes = readFileSync(join(SCENES, "layout.tsx"));
  assert.ok(bytes.length >= LAYOUT_PREFIX.bytes, `layout.tsx has ${bytes.length} bytes`);
  assert.equal(sha256(bytes.subarray(0, LAYOUT_PREFIX.bytes)), LAYOUT_PREFIX.sha256);
});

test("batch 4.3's scene files hash to their sha256 at 49d562f (a file removed in step 7 is skipped)", () => {
  for (const [name, digest] of Object.entries(PRESERVED_4_3)) {
    const path = join(SCENES, name);
    if (!existsSync(path)) continue;
    assert.equal(sha256(readFileSync(path)), digest, name);
  }
});

test("layout.tsx at 49d562f is a byte-exact prefix of layout.tsx", () => {
  const bytes = readFileSync(join(SCENES, "layout.tsx"));
  assert.ok(bytes.length >= LAYOUT_PREFIX_4_3.bytes, `layout.tsx has ${bytes.length} bytes`);
  assert.equal(sha256(bytes.subarray(0, LAYOUT_PREFIX_4_3.bytes)), LAYOUT_PREFIX_4_3.sha256);
});
