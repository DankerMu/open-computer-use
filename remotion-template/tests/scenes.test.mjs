// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The first six scene components (task 4.2): their catalog entries and
// samples, the layout boxes, and a source scan of src/kit/scenes/. Pure: the
// catalog, the boxes, the theme and the timing tables are imported through
// Node's type stripping; the scene files are read as text, because Node
// cannot import TSX. Expected kinds, numbers, lengths and required fields
// are the fixture's literals; field lengths are computed against
// LIMITS.content.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { ENTRANCE_FRAMES, MIN_SCENE_FRAMES } from "../bin/scene-timing.mjs";
import { LIMITS, contentSchema } from "../bin/script-schema.mjs";
import { candidates, validateCatalog } from "../src/kit/gallery/catalog.ts";
import { captionBand } from "../src/kit/render/captions.ts";
import { IMAGE_BOX, TEXT_BOX } from "../src/kit/scenes/boxes.ts";
import { themes } from "../src/kit/theme.ts";

const TEMPLATE = fileURLToPath(new URL("..", import.meta.url));
const SCENES = join(TEMPLATE, "src", "kit", "scenes");

// The fixture's kinds, in catalog order, with each scene file.
const KINDS = ["hook", "question", "concept-card", "definition-quote", "summary", "end-card"];
const FILES = {
  hook: "Hook",
  question: "Question",
  "concept-card": "ConceptCard",
  "definition-quote": "DefinitionQuote",
  summary: "Summary",
  "end-card": "EndCard",
};
// Entrance 15 for every kind; minimum 60, and 90 for the end card.
const ENTRANCE = 15;
const MINIMUM = { hook: 60, question: 60, "concept-card": 60, "definition-quote": 60, summary: 60, "end-card": 90 };
// The required content fields of each kind (bin/script-schema.mjs CONTENT).
const REQUIRED = {
  hook: ["title"],
  question: ["question"],
  "concept-card": ["title", "points"],
  "definition-quote": ["term", "text"],
  summary: ["points"],
  "end-card": ["title"],
};
const LISTS = new Set(["points"]);
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

// --- catalog ------------------------------------------------------------------

test("the tracked catalog validates and lists exactly the six kinds, one candidate each, id equal to kind", () => {
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

test("every candidate has the samples max, image and min, in that order", () => {
  for (const kind of KINDS) assert.deepEqual(byKind(kind).samples.map((s) => s.id), ["max", "image", "min"], kind);
});

test("max: every content field at its LIMITS.content length in Han ending in a full-width closing mark, every list at its maximum", () => {
  for (const kind of KINDS) {
    const { content, image } = sample(kind, "max");
    const limits = LIMITS.content[kind];
    assert.deepEqual(Object.keys(content).sort(), Object.keys(contentSchema(kind).shape).sort(), `${kind}: every field present`);
    for (const [field, value] of Object.entries(content)) {
      if (LISTS.has(field)) {
        assert.equal(value.length, limits[field].max, `${kind}.${field} count`);
        value.forEach((item, i) => assertMaxHan(item, limits.point, `${kind}.${field}[${i}]`));
      } else {
        assertMaxHan(value, limits[field], `${kind}.${field}`);
      }
    }
    assert.equal(image, null, kind);
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

test("image: max's content and timing, with image sample.png", () => {
  for (const kind of KINDS) {
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
    assert.deepEqual(Object.keys(content).sort(), [...REQUIRED[kind]].sort(), kind);
    for (const [field, value] of Object.entries(content)) {
      if (LISTS.has(field)) {
        assert.equal(value.length, LIMITS.content[kind][field].min, `${kind}.${field} count`);
        value.forEach((item, i) => assertTwoHan(item, `${kind}.${field}[${i}]`));
      } else {
        assertTwoHan(value, `${kind}.${field}`);
      }
    }
    assert.equal(image, null, kind);
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
  "<svg",
  "foreignObject",
  "<span",
  "fetch(",
  "http:",
  "https:",
  "lineHeight",
  "letterSpacing",
];
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

test("src/kit/scenes holds the six scene files, boxes.ts and layout.tsx, each with the FSL header", () => {
  const files = sources();
  assert.deepEqual(Object.keys(files), [...SCENE_FILES, "boxes.ts", "layout.tsx"].sort());
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

