// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The probe in a real headless shell, over the fixture scenes in
// fixtures/. Runs only inside the workspace image, from a project root whose
// node_modules link reaches /opt/remotion-template/node_modules:
//   node --test "tests/render/*.test.mjs"
// CI's tests/*.test.mjs glob does not match this file. Expected records and
// violations come from the task 3.7 fixture and from the scenes' own
// literals (text, positions, sizes), never from the probe's output.

import { before, test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { probe, violations } from "../../bin/probe.mjs";
import { themes } from "../../src/kit/theme.ts";

const ENTRY = fileURLToPath(new URL("./fixtures/entry.tsx", import.meta.url));
const theme = themes.light;
const ROLES = ["entrance-end", "midpoint", "pre-exit"];
const TOLERANCE = 0.5;

function target(id, scene, durationInFrames, entranceFrames, exitFrame = durationInFrames) {
  return { id, scene, props: {}, durationInFrames, entranceFrames, exitFrame };
}

// Every failing scene runs 60 frames with a 10-frame entrance: probe frames
// 10, 30 and 59. Each lists [rule, path] at every probe frame.
const FAILING = {
  "edge-over": [["outside safe area", "div:nth-child(1)"]],
  "small-font": [["font size", "div:nth-child(1)"]],
  "wrap-start": [["line start", "div:nth-child(1)"]],
  scroll: [
    ["scrolls", "div:nth-child(1)"],
    ["mask clips", "div:nth-child(1)"],
  ],
  "wrap-start-lines": [["line start", "div:nth-child(1)>div:nth-child(2)"]],
  "scaled-font": [["font size", "div:nth-child(1)>div:nth-child(1)"]],
  "mask-small": [
    ["mask clips", "div:nth-child(1)>div:nth-child(1)"],
    ["mask clips", "div:nth-child(2)"],
  ],
  "text-differs": [["text differs", "div:nth-child(1)"]],
  "custom-overflow": [["outside safe area", "div:nth-child(1)"]],
};
const FAILING_FRAMES = { "entrance-end": 10, midpoint: 30, "pre-exit": 59 };

const TARGETS = [
  target("fits", "fits", 90, 15),
  // Not last: a scene that saw the composition's remaining length instead
  // of its own would still be inside the safe area at its frame 59.
  target("late-overflow", "late-overflow", 60, 10),
  // entranceEnd 30 equals the midpoint, floor(60 / 2).
  target("fits-entrance-mid", "fits", 60, 30),
  target("font-absent", "font-absent", 10, 0),
  ...Object.keys(FAILING).map((name) => target(name, name, 60, 10)),
];

const PARAGRAPH = "光线从空气斜射入水中时，传播方向发生改变。";
const FITS_TEXTS = [
  ["div:nth-child(1)>div:nth-child(1)", "光线从空气斜射入水中时，", ["光线从空气斜射入水中时，"]],
  ["div:nth-child(1)>div:nth-child(2)", "传播方向发生改变。", ["传播方向发生改变。"]],
  ["div:nth-child(2)", "右侧对齐", ["右侧对齐"]],
  ["div:nth-child(3)", "最小字号", ["最小字号"]],
  ["div:nth-child(4)>div:nth-child(1)", "遮罩文字", ["遮罩文字"]],
  ["div:nth-child(5)", "揭示文字", ["揭示文字"]],
  ["div:nth-child(6)", "光在水中传播变慢", ["光在水中传", "播变慢"]],
];

let first;
let second;
const timings = [];

async function timedProbe() {
  const started = Date.now();
  const result = await probe({ entryPoint: ENTRY, targets: TARGETS });
  timings.push(Date.now() - started);
  return result;
}

before(async () => {
  first = await timedProbe();
  second = await timedProbe();
});

function framesOf(result, id) {
  return result.frames.filter((frame) => frame.target === id);
}

function near(actual, expected, label) {
  assert.ok(Math.abs(actual - expected) <= TOLERANCE, `${label}: ${actual} is not within ${TOLERANCE} of ${expected}`);
}

function union(boxes) {
  const left = Math.min(...boxes.map((b) => b.x));
  const top = Math.min(...boxes.map((b) => b.y));
  const right = Math.max(...boxes.map((b) => b.x + b.width));
  const bottom = Math.max(...boxes.map((b) => b.y + b.height));
  return { x: left, y: top, width: right - left, height: bottom - top };
}

test("the result has version 1, the canvas, and three frames per target in order", () => {
  assert.equal(first.version, 1);
  assert.deepEqual(first.canvas, { width: 1920, height: 1080 });
  assert.deepEqual(
    first.frames.map(({ target: id, role }) => [id, role]),
    TARGETS.flatMap(({ id }) => ROLES.map((role) => [id, role])),
  );
});

test("fits breaks no rule at any probe frame", () => {
  const clean = first.frames.filter((frame) => ["fits", "fits-entrance-mid", "font-absent"].includes(frame.target));
  assert.deepEqual(violations({ ...first, frames: clean }, theme), []);
  assert.deepEqual(
    framesOf(first, "fits").map((frame) => frame.frame),
    [15, 45, 89],
  );
});

test("fits records every text with its rendered lines", () => {
  for (const frame of framesOf(first, "fits")) {
    const label = `${frame.role} frame ${frame.frame}`;
    assert.equal(frame.fontPresent, true, label);
    assert.deepEqual(
      frame.texts.map(({ path, text, lines }) => [path, text, lines]),
      FITS_TEXTS,
      label,
    );
    assert.deepEqual(frame.marked, [{ path: "div:nth-child(1)", input: PARAGRAPH, rendered: PARAGRAPH }], label);
    assert.deepEqual(frame.hidden, [{ path: "div:nth-child(7)", text: "隐藏文字" }], label);
    assert.deepEqual(frame.scrolls, [], label);

    const [line1, line2, edge, minimum, maskText, reveal] = frame.texts;
    // The paragraph's two block lines at 48 px with line height 1.4.
    near(union([line1.glyphBox, line2.glyphBox]).height, 2 * 1.4 * 48, `${label} paragraph height`);
    near(edge.glyphBox.x + edge.glyphBox.width, 1824, `${label} edge right`);
    assert.equal(minimum.minFontSizePx, 32, label);
    assert.equal(line1.minFontSizePx, 48, label);

    assert.deepEqual(maskText.clippedBy, ["div:nth-child(4)"], label);
    assert.deepEqual(reveal.clippedBy, ["div:nth-child(5)"], label);
    assert.deepEqual(
      frame.masks.map(({ path }) => path),
      ["div:nth-child(4)", "div:nth-child(5)"],
      label,
    );
    assert.equal(frame.masks[0].clipPath, "none", label);
    assert.match(frame.masks[1].clipPath, /^inset\(/, label);
    // The overflow mask is its 200 × 70 padding box; the reveal's inset
    // reached 0, so its box is the element's border box: 4 glyphs of 50 px
    // wide, one 70 px line (50 px × 1.4) high.
    const [overflowMask, revealMask] = frame.masks.map((mask) => mask.clipBox);
    for (const [actual, expected, name] of [
      [overflowMask, { x: 200, y: 420, width: 200, height: 70 }, "overflow mask"],
      [revealMask, { x: 200, y: 560, width: 200, height: 70 }, "reveal mask"],
    ]) {
      for (const key of ["x", "y", "width", "height"]) near(actual[key], expected[key], `${label} ${name} ${key}`);
    }
  }
});

test("the opacity-0 text is listed as hidden and never as a text record", () => {
  for (const frame of framesOf(first, "fits")) {
    assert.ok(!frame.texts.some((record) => record.text.includes("隐藏")), frame.role);
  }
});

test("a font that is not installed measures as absent in the same page", () => {
  for (const frame of framesOf(first, "font-absent")) {
    assert.equal(frame.fontPresent, true, frame.role);
    assert.deepEqual(
      frame.texts.map((record) => record.text),
      ["false"],
      frame.role,
    );
  }
});

test("an entrance ending on the midpoint gives two equal frames that differ only by role", () => {
  const frames = framesOf(first, "fits-entrance-mid");
  assert.deepEqual(
    frames.map(({ role, frame }) => [role, frame]),
    [
      ["entrance-end", 30],
      ["midpoint", 30],
      ["pre-exit", 59],
    ],
  );
  assert.deepEqual({ ...frames[0], role: "midpoint" }, frames[1]);
});

test("each failing scene gives exactly its own violations", () => {
  const found = violations(first, theme);
  const expected = [
    { target: "late-overflow", role: "pre-exit", frame: 59, rule: "outside safe area", path: "div:nth-child(1)" },
    ...Object.entries(FAILING).flatMap(([id, rules]) =>
      ROLES.flatMap((role) => rules.map(([rule, path]) => ({ target: id, role, frame: FAILING_FRAMES[role], rule, path }))),
    ),
  ];
  assert.deepEqual(
    found.map(({ target: id, role, frame, rule, path }) => ({ target: id, role, frame, rule, path })),
    expected,
  );
  // Both line-start scenes break at the comma.
  for (const v of found.filter((v) => v.rule === "line start")) {
    assert.equal(v.detail, 'line 2 starts with "，"', `${v.target} ${v.role}`);
  }
});

test("probing the same targets twice gives deep-equal results", (t) => {
  t.diagnostic(`probe durations (bundle + render, ${TARGETS.length} targets): ${timings.join(" ms, ")} ms`);
  assert.deepEqual(second, first);
});

test("an unknown scene key rejects with a named error", async () => {
  await assert.rejects(
    probe({ entryPoint: ENTRY, targets: [target("missing", "no-such-scene", 10, 0)] }),
    (error) => error instanceof Error && error.message.startsWith("kit probe: ") && error.message.includes('kit probe: unknown scene "no-such-scene"'),
  );
});

test("a scene that throws rejects, naming its target and frame", async () => {
  // The second target starts at composition frame 30; its first probe
  // frame is its own frame 5.
  await assert.rejects(
    probe({ entryPoint: ENTRY, targets: [target("ok", "fits", 30, 5), target("boom", "throws", 30, 5)] }),
    (error) =>
      error instanceof Error &&
      error.message.startsWith('kit probe: target "boom" frame 5: ') &&
      error.message.includes("fixture scene throws"),
  );
});

test("an invalid target rejects before any bundle is written", async () => {
  const scratch = mkdtempSync(join(tmpdir(), "probe-render-test-"));
  const saved = process.env.TMPDIR;
  process.env.TMPDIR = scratch;
  try {
    await assert.rejects(
      probe({ entryPoint: ENTRY, targets: [target("bad", "fits", 10, 0, 11)] }),
      /^Error: kit probe: target "bad": exitFrame /,
    );
    assert.deepEqual(readdirSync(scratch), []);
  } finally {
    if (saved === undefined) delete process.env.TMPDIR;
    else process.env.TMPDIR = saved;
    rmSync(scratch, { recursive: true, force: true });
  }
});
