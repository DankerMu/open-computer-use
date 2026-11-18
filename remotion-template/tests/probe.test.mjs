// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The probe's pure parts: probe frames, the record collector, the four text
// rules over hand-written results, the violation line and target
// validation. Expected values come from the task 3.7 fixture: the safe area
// of theme.ts is 96–1824 × 54–1026 with a 32 px minimum, every box
// comparison tolerates 0.5 px horizontally (left, right) and 1 px
// vertically (top, bottom), and only ，。！？、；：）」』 may not start a line.
// The browser half lives in tests/render/ and runs only in the image.

import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readdirSync, rmSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { collectRecords, formatViolation, probe, probeFrames, violations } from "../bin/probe.mjs";
import { themes } from "../src/kit/theme.ts";

const theme = themes.light;
const P1 = "div:nth-child(1)";
const P2 = "div:nth-child(2)";

function frameOf(over = {}) {
  return { target: "t", role: "midpoint", frame: 30, fontPresent: true, texts: [], hidden: [], marked: [], scrolls: [], masks: [], ...over };
}

function resultOf(...frames) {
  return { version: 1, canvas: { width: 1920, height: 1080 }, frames };
}

function record(over = {}) {
  return { path: P1, text: "光", glyphBox: { x: 200, y: 200, width: 48, height: 67.2 }, minFontSizePx: 48, scale: 1, lines: ["光"], clippedBy: [], ...over };
}

function box(x, y, width, height) {
  return { x, y, width, height };
}

// Only the fields a rule reports, for compact expectations.
function brief(list) {
  return list.map(({ rule, path, detail }) => ({ rule, path, detail }));
}

// --- probeFrames ------------------------------------------------------------

test("probe frames of a scene with a transition stop before the transition", () => {
  // exitFrame = transitionOut.startFrame − startFrame = 105.
  assert.deepEqual(probeFrames({ durationInFrames: 120, entranceFrames: 20, exitFrame: 105 }), [
    { role: "entrance-end", frame: 20 },
    { role: "midpoint", frame: 60 },
    { role: "pre-exit", frame: 104 },
  ]);
  // A transition starting before the middle pulls the midpoint back to the
  // pre-exit frame.
  assert.deepEqual(probeFrames({ durationInFrames: 120, entranceFrames: 10, exitFrame: 50 }), [
    { role: "entrance-end", frame: 10 },
    { role: "midpoint", frame: 49 },
    { role: "pre-exit", frame: 49 },
  ]);
});

test("probe frames of a scene without a transition end on its last frame", () => {
  assert.deepEqual(probeFrames({ durationInFrames: 91, entranceFrames: 15, exitFrame: 91 }), [
    { role: "entrance-end", frame: 15 },
    { role: "midpoint", frame: 45 },
    { role: "pre-exit", frame: 90 },
  ]);
});

test("an entrance longer than the scene is probed at the pre-exit frame", () => {
  assert.deepEqual(probeFrames({ durationInFrames: 30, entranceFrames: 40, exitFrame: 30 }), [
    { role: "entrance-end", frame: 29 },
    { role: "midpoint", frame: 29 },
    { role: "pre-exit", frame: 29 },
  ]);
  // An entrance past the middle moves the midpoint to the entrance end.
  assert.deepEqual(probeFrames({ durationInFrames: 60, entranceFrames: 40, exitFrame: 60 }), [
    { role: "entrance-end", frame: 40 },
    { role: "midpoint", frame: 40 },
    { role: "pre-exit", frame: 59 },
  ]);
});

test("a 1-frame scene is probed three times at frame 0", () => {
  assert.deepEqual(probeFrames({ durationInFrames: 1, entranceFrames: 0, exitFrame: 1 }), [
    { role: "entrance-end", frame: 0 },
    { role: "midpoint", frame: 0 },
    { role: "pre-exit", frame: 0 },
  ]);
});

test("probe frames reject non-integer and out-of-range inputs, naming the field", () => {
  const good = { durationInFrames: 60, entranceFrames: 10, exitFrame: 60 };
  const rows = [
    ["durationInFrames", 0],
    ["durationInFrames", -5],
    ["durationInFrames", 1.5],
    ["durationInFrames", "60"],
    ["durationInFrames", Number.NaN],
    ["durationInFrames", undefined],
    ["entranceFrames", -1],
    ["entranceFrames", 2.5],
    ["entranceFrames", null],
    ["exitFrame", 0],
    ["exitFrame", 61],
    ["exitFrame", 30.5],
    ["exitFrame", Number.POSITIVE_INFINITY],
  ];
  for (const [field, value] of rows) {
    assert.throws(
      () => probeFrames({ ...good, [field]: value }),
      (error) => error instanceof RangeError && error.message.startsWith(`${field} `),
      `${field} = ${String(value)}`,
    );
  }
});

// --- collectRecords -----------------------------------------------------------

test("collectRecords returns one record per expected index, ignoring other log lines", () => {
  const lines = [
    "Download progress 50%",
    `kit-probe ${JSON.stringify({ index: 7, fontPresent: true, texts: [] })}`,
    "kit-probe-ish line without the separator",
    `kit-probe ${JSON.stringify({ index: 3, fontPresent: false, texts: [] })}`,
  ];
  assert.deepEqual(collectRecords(lines, [3, 7]), [
    { index: 3, fontPresent: false, texts: [] },
    { index: 7, fontPresent: true, texts: [] },
  ]);
});

test("collectRecords throws on a missing, duplicated or unexpected record", () => {
  const line = (index) => `kit-probe ${JSON.stringify({ index })}`;
  assert.throws(() => collectRecords([line(3)], [3, 7]), /^Error: kit probe: no record for frame 7$/);
  assert.throws(() => collectRecords([line(3), line(7), line(3)], [3, 7]), /^Error: kit probe: duplicate record for frame 3$/);
  assert.throws(() => collectRecords([line(3), line(7), line(9)], [3, 7]), /^Error: kit probe: record for unexpected frame 9$/);
  assert.throws(() => collectRecords(["kit-probe {not json"], [3]), /^Error: kit probe: unreadable record/);
});

// --- violations: text differs -------------------------------------------------

test("a marked element whose rendered text differs from its input is reported", () => {
  const frame = frameOf({
    marked: [
      { path: P1, input: "光的折射现象", rendered: "光的折射" },
      { path: P2, input: "速度", rendered: "速度" },
    ],
  });
  assert.deepEqual(violations(resultOf(frame), theme), [
    { target: "t", role: "midpoint", frame: 30, rule: "text differs", path: P1, detail: 'rendered "光的折射", input "光的折射现象"' },
  ]);
});

// --- violations: outside safe area ----------------------------------------------

test("glyph boxes past a safe-area edge pass up to 0.5 px sideways and 1 px vertically", () => {
  const rows = [
    [box(95.5, 200, 100, 50), []],
    [box(95.49, 200, 100, 50), ["left 0.51 px"]],
    [box(1724.5, 200, 100, 50), []],
    [box(1724.51, 200, 100, 50), ["right 0.51 px"]],
    [box(200, 53, 100, 50), []],
    [box(200, 52.99, 100, 50), ["top 1.01 px"]],
    [box(200, 977, 100, 50), []],
    [box(200, 977.01, 100, 50), ["bottom 1.01 px"]],
    [box(96, 54, 1728, 972), []],
  ];
  for (const [glyphBox, details] of rows) {
    const found = violations(resultOf(frameOf({ texts: [record({ glyphBox })] })), theme);
    assert.deepEqual(
      brief(found),
      details.map((detail) => ({ rule: "outside safe area", path: P1, detail })),
      JSON.stringify(glyphBox),
    );
  }
});

test("a glyph box crossing two edges names both with their distances", () => {
  const found = violations(resultOf(frameOf({ texts: [record({ glyphBox: box(83.5, 51, 300, 60) })] })), theme);
  assert.deepEqual(brief(found), [{ rule: "outside safe area", path: P1, detail: "left 12.5 px, top 3 px" }]);
  const wide = violations(resultOf(frameOf({ texts: [record({ glyphBox: box(90, 1000, 1740, 30) })] })), theme);
  assert.deepEqual(brief(wide), [{ rule: "outside safe area", path: P1, detail: "left 6 px, right 6 px, bottom 4 px" }]);
});

// --- violations: scrolls and font size --------------------------------------------

test("every scroll entry is a violation", () => {
  const frame = frameOf({ scrolls: [{ path: P2, scrollWidth: 672, clientWidth: 400 }] });
  assert.deepEqual(brief(violations(resultOf(frame), theme)), [
    { rule: "scrolls", path: P2, detail: "scrollWidth 672 > clientWidth 400" },
  ]);
});

test("a font size below the theme minimum is reported and the minimum itself passes", () => {
  const frame = frameOf({
    texts: [record({ path: P1, minFontSizePx: 32 }), record({ path: P2, minFontSizePx: 31.99 })],
  });
  assert.deepEqual(brief(violations(resultOf(frame), theme)), [
    { rule: "font size", path: P2, detail: "31.99 px < 32 px" },
  ]);
});

// --- violations: line start ---------------------------------------------------------

test("a wrapped line starting with each forbidden mark is reported, other marks are not", () => {
  for (const mark of "，。！？、；：）」』") {
    const frame = frameOf({ texts: [record({ text: `光的折射${mark}速度`, lines: ["光的折射", `${mark}速度`] })] });
    assert.deepEqual(
      brief(violations(resultOf(frame), theme)),
      [{ rule: "line start", path: P1, detail: `line 2 starts with "${mark}"` }],
      mark,
    );
  }
  for (const mark of "”’》】….,!") {
    const frame = frameOf({ texts: [record({ text: `光的折射${mark}速度`, lines: ["光的折射", `${mark}速度`] })] });
    assert.deepEqual(violations(resultOf(frame), theme), [], mark);
  }
});

test("a block's first line may start with a closing mark", () => {
  // Two unmarked records are two blocks: each first line begins at its
  // input's own edge.
  const frame = frameOf({
    texts: [
      record({ path: P1, text: "，速度", lines: ["，速度"] }),
      record({ path: P2, text: "。很快", lines: ["。很快"] }),
    ],
  });
  assert.deepEqual(violations(resultOf(frame), theme), []);
});

test("the records of one marked element form one block, numbered across records", () => {
  const frame = frameOf({
    marked: [{ path: P1, input: "光的折射，速度", rendered: "光的折射，速度" }],
    texts: [
      record({ path: `${P1}>div:nth-child(1)`, text: "光的折射", lines: ["光的折射"] }),
      record({ path: `${P1}>div:nth-child(2)`, text: "，速度", lines: ["，速度"] }),
    ],
  });
  assert.deepEqual(brief(violations(resultOf(frame), theme)), [
    { rule: "line start", path: `${P1}>div:nth-child(2)`, detail: 'line 2 starts with "，"' },
  ]);
  const three = frameOf({
    marked: [{ path: P1, input: "光的折射速度。快", rendered: "光的折射速度。快" }],
    texts: [
      record({ path: `${P1}>div:nth-child(1)`, text: "光的折射", lines: ["光的", "折射"] }),
      record({ path: `${P1}>div:nth-child(2)`, text: "速度。快", lines: ["速度", "。快"] }),
    ],
  });
  assert.deepEqual(brief(violations(resultOf(three), theme)), [
    { rule: "line start", path: `${P1}>div:nth-child(2)`, detail: 'line 4 starts with "。"' },
  ]);
});

test("a marked block takes only records at or inside its element", () => {
  // div:nth-child(10) shares the prefix of div:nth-child(1) but is not inside it.
  const frame = frameOf({
    marked: [{ path: P1, input: "光", rendered: "光" }],
    texts: [
      record({ path: P1, text: "光", lines: ["光"] }),
      record({ path: "div:nth-child(10)", text: "，速度", lines: ["，速度"] }),
    ],
  });
  assert.deepEqual(violations(resultOf(frame), theme), []);
  // A marked root (path "") holds every record.
  const rooted = frameOf({
    marked: [{ path: "", input: "光，速度", rendered: "光，速度" }],
    texts: [record({ path: P1, text: "光", lines: ["光"] }), record({ path: P2, text: "，速度", lines: ["，速度"] })],
  });
  assert.deepEqual(brief(violations(resultOf(rooted), theme)), [
    { rule: "line start", path: P2, detail: 'line 2 starts with "，"' },
  ]);
});

// --- violations: mask clips -------------------------------------------------------

function masked(glyphBox, clipBox, clipPath = "none") {
  return frameOf({
    texts: [record({ glyphBox, clippedBy: [P1] })],
    masks: [{ path: P1, clipPath, clipBox }],
  });
}

test("a mask cutting a glyph box is reported, past 0.5 px sideways or 1 px vertically", () => {
  const glyphBox = box(200, 200, 200, 67.2);
  const clips = [{ rule: "mask clips", path: P1, detail: `by ${P1}` }];
  const rows = [
    [box(200, 200, 200, 67.2), []],
    [box(200, 200, 190, 67.2), clips],
    // Right edge: 0.5 px overhang passes, 0.51 px fails.
    [box(200, 200, 199.5, 67.2), []],
    [box(200, 200, 199.49, 67.2), clips],
    // Left edge.
    [box(200.5, 200, 199.5, 67.2), []],
    [box(200.51, 200, 199.49, 67.2), clips],
    // Top edge: 1 px passes, 1.01 px fails.
    [box(200, 201, 200, 66.2), []],
    [box(200, 201.01, 200, 66.19), clips],
    // Bottom edge.
    [box(200, 200, 200, 66.2), []],
    [box(200, 200, 200, 66.19), clips],
  ];
  for (const [clipBox, expected] of rows) {
    assert.deepEqual(brief(violations(resultOf(masked(glyphBox, clipBox)), theme)), expected, JSON.stringify(clipBox));
  }
});

test("a glyph box wholly outside its mask is not yet revealed", () => {
  const glyphBox = box(200, 200, 200, 67.2);
  // Disjoint, and overlapping by exactly the tolerance: 0.5 px sideways,
  // 1 px vertically.
  assert.deepEqual(violations(resultOf(masked(glyphBox, box(600, 200, 200, 67.2))), theme), []);
  assert.deepEqual(violations(resultOf(masked(glyphBox, box(399.5, 200, 200, 67.2))), theme), []);
  assert.deepEqual(brief(violations(resultOf(masked(glyphBox, box(399.49, 200, 200, 67.2))), theme)), [
    { rule: "mask clips", path: P1, detail: `by ${P1}` },
  ]);
  assert.deepEqual(violations(resultOf(masked(glyphBox, box(200, 266.2, 200, 100))), theme), []);
  assert.deepEqual(brief(violations(resultOf(masked(glyphBox, box(200, 266.19, 200, 100))), theme)), [
    { rule: "mask clips", path: P1, detail: `by ${P1}` },
  ]);
});

test("a clip path that cannot be measured is a violation", () => {
  const found = violations(resultOf(masked(box(200, 200, 200, 67.2), null, "circle(50% at 50% 50%)")), theme);
  assert.deepEqual(brief(found), [
    { rule: "mask clips", path: P1, detail: `by ${P1}: clip-path circle(50% at 50% 50%) is not measurable` },
  ]);
});

test("each record and clipping mask pair gives its own violation, nearest mask first", () => {
  const inner = `${P1}>div:nth-child(1)`;
  const frame = frameOf({
    texts: [
      record({ path: `${inner}>div:nth-child(1)`, glyphBox: box(200, 200, 300, 67.2), clippedBy: [inner, P1] }),
      record({ path: `${inner}>div:nth-child(2)`, glyphBox: box(200, 300, 300, 67.2), clippedBy: [inner, P1] }),
    ],
    masks: [
      { path: inner, clipPath: "none", clipBox: box(200, 150, 250, 300) },
      { path: P1, clipPath: "none", clipBox: box(220, 100, 400, 400) },
    ],
  });
  assert.deepEqual(brief(violations(resultOf(frame), theme)), [
    { rule: "mask clips", path: `${inner}>div:nth-child(1)`, detail: `by ${inner}` },
    { rule: "mask clips", path: `${inner}>div:nth-child(1)`, detail: `by ${P1}` },
    { rule: "mask clips", path: `${inner}>div:nth-child(2)`, detail: `by ${inner}` },
    { rule: "mask clips", path: `${inner}>div:nth-child(2)`, detail: `by ${P1}` },
  ]);
});

// --- violations: order, hidden text, clean frames ------------------------------------

test("violations follow result order and carry target, role and frame", () => {
  const first = frameOf({ target: "a", role: "entrance-end", frame: 10, scrolls: [{ path: P1, scrollWidth: 500, clientWidth: 400 }] });
  const clean = frameOf({ target: "a", role: "midpoint", frame: 30, texts: [record()] });
  const last = frameOf({
    target: "b",
    role: "pre-exit",
    frame: 59,
    marked: [{ path: P2, input: "甲", rendered: "乙" }],
    texts: [record({ path: P2, minFontSizePx: 24, glyphBox: box(20, 200, 48, 67.2) })],
  });
  assert.deepEqual(violations(resultOf(first, clean, last), theme), [
    { target: "a", role: "entrance-end", frame: 10, rule: "scrolls", path: P1, detail: "scrollWidth 500 > clientWidth 400" },
    { target: "b", role: "pre-exit", frame: 59, rule: "text differs", path: P2, detail: 'rendered "乙", input "甲"' },
    { target: "b", role: "pre-exit", frame: 59, rule: "outside safe area", path: P2, detail: "left 76 px" },
    { target: "b", role: "pre-exit", frame: 59, rule: "font size", path: P2, detail: "24 px < 32 px" },
  ]);
});

test("hidden text is never judged", () => {
  const frame = frameOf({ hidden: [{ path: P1, text: "隐藏" }] });
  assert.deepEqual(violations(resultOf(frame), theme), []);
});

// --- scaled records, spaced line starts, clipped overflow ---------------------------

test("the vertical tolerance scales with the record's rendered scale", () => {
  const scaled = (glyphBox) => record({ glyphBox, scale: 2 });
  const safeRows = [
    [box(200, 52, 100, 50), []],
    [box(200, 51.99, 100, 50), ["top 2.01 px"]],
    [box(200, 978, 100, 50), []],
    [box(200, 978.01, 100, 50), ["bottom 2.01 px"]],
    // Horizontal edges keep 0.5 px whatever the scale.
    [box(95.49, 200, 100, 50), ["left 0.51 px"]],
  ];
  for (const [glyphBox, details] of safeRows) {
    assert.deepEqual(
      brief(violations(resultOf(frameOf({ texts: [scaled(glyphBox)] })), theme)),
      details.map((detail) => ({ rule: "outside safe area", path: P1, detail })),
      JSON.stringify(glyphBox),
    );
  }
  const glyphBox = box(200, 200, 384, 134.4);
  const clips = [{ rule: "mask clips", path: P1, detail: `by ${P1}` }];
  const maskRows = [
    [box(200, 202, 384, 132.4), []],
    [box(200, 202.01, 384, 132.39), clips],
    [box(200, 200, 384, 132.4), []],
    [box(200, 200, 384, 132.39), clips],
    [box(200.51, 200, 383.49, 134.4), clips],
  ];
  for (const [clipBox, expected] of maskRows) {
    const frame = frameOf({
      texts: [record({ glyphBox, scale: 2, clippedBy: [P1] })],
      masks: [{ path: P1, clipPath: "none", clipBox }],
    });
    assert.deepEqual(brief(violations(resultOf(frame), theme)), expected, JSON.stringify(clipBox));
  }
  // Unrevealed below a mask: overlapping by the scaled 2 px counts as
  // outside it, 2.01 px as cutting the glyph box.
  for (const [clipBox, expected] of [
    [box(200, 332.4, 384, 100), []],
    [box(200, 332.39, 384, 100), clips],
  ]) {
    const frame = frameOf({
      texts: [record({ glyphBox, scale: 2, clippedBy: [P1] })],
      masks: [{ path: P1, clipPath: "none", clipBox }],
    });
    assert.deepEqual(brief(violations(resultOf(frame), theme)), expected, JSON.stringify(clipBox));
  }
  // A scale below 1 keeps the 1 px floor.
  assert.deepEqual(violations(resultOf(frameOf({ texts: [record({ glyphBox: box(200, 53, 100, 50), scale: 0.5 })] })), theme), []);
});

test("a record without a scale is refused", () => {
  const { scale: _scale, ...unscaled } = record();
  assert.throws(() => violations(resultOf(frameOf({ texts: [unscaled] })), theme), /^Error: kit probe: record "div:nth-child\(1\)" has no scale$/);
});

test("a line starting with spaces before a forbidden mark is reported at the mark", () => {
  const frame = frameOf({ texts: [record({ text: "光的折射  ，速度", lines: ["光的折射", "  ，速度"] })] });
  assert.deepEqual(brief(violations(resultOf(frame), theme)), [
    { rule: "line start", path: P1, detail: 'line 2 starts with "，"' },
  ]);
  // A line of spaces only starts with no mark.
  const blank = frameOf({ texts: [record({ text: "光  ", lines: ["光", "  "] })] });
  assert.deepEqual(violations(resultOf(blank), theme), []);
});

test("text wholly outside a measurable mask is not judged against the safe area", () => {
  const outside = box(1900, 300, 240, 67.2);
  const frameWith = (clipBox, clipPath = "none") =>
    frameOf({
      texts: [record({ glyphBox: outside, clippedBy: [P1] })],
      masks: [{ path: P1, clipPath, clipBox }],
    });
  // A camera viewport at 200–600: the text is not shown.
  assert.deepEqual(violations(resultOf(frameWith(box(200, 300, 400, 100))), theme), []);
  // A viewport the text reaches into: shown in part, so both rules apply.
  assert.deepEqual(brief(violations(resultOf(frameWith(box(1800, 300, 200, 100))), theme)), [
    { rule: "outside safe area", path: P1, detail: "right 316 px" },
    { rule: "mask clips", path: P1, detail: `by ${P1}` },
  ]);
  // A mask that cannot be measured exempts nothing.
  assert.deepEqual(brief(violations(resultOf(frameWith(null, "circle(50%)")), theme)), [
    { rule: "outside safe area", path: P1, detail: "right 316 px" },
    { rule: "mask clips", path: P1, detail: `by ${P1}: clip-path circle(50%) is not measurable` },
  ]);
});

// --- formatViolation ----------------------------------------------------------------

test("a violation line names target, role, frame, rule, path and detail", () => {
  const v = { target: "fits", role: "midpoint", frame: 45, rule: "outside safe area", path: P2, detail: "left 12.5 px, top 3 px" };
  assert.equal(formatViolation(v), "fits midpoint frame 45: outside safe area: div:nth-child(2) left 12.5 px, top 3 px");
});

test("control characters in a violation's text are escaped and long text is not cut", () => {
  const [found] = violations(resultOf(frameOf({ marked: [{ path: P1, input: "a\tb\u0007", rendered: "a\nb " }] })), theme);
  assert.equal(
    formatViolation(found),
    't midpoint frame 30: text differs: div:nth-child(1) rendered "a\\nb\\u2028", input "a\\tb\\u0007"',
  );
  const long = "光".repeat(500);
  const [cut] = violations(resultOf(frameOf({ marked: [{ path: P1, input: long, rendered: "" }] })), theme);
  assert.equal(formatViolation(cut), `t midpoint frame 30: text differs: div:nth-child(1) rendered "", input "${long}"`);
});

// --- probe: target validation ---------------------------------------------------------

test("invalid targets reject with a named error before anything is bundled", async () => {
  const scratch = mkdtempSync(join(tmpdir(), "probe-test-"));
  const saved = process.env.TMPDIR;
  process.env.TMPDIR = scratch;
  const target = { id: "a", scene: "fits", props: {}, durationInFrames: 60, entranceFrames: 10, exitFrame: 60 };
  const rows = [
    [undefined, /^kit probe: targets must be a non-empty array$/],
    [[], /^kit probe: targets must be a non-empty array$/],
    [{}, /^kit probe: targets must be a non-empty array$/],
    [[null], /^kit probe: target 1 must be an object$/],
    [[{ ...target, id: "" }], /^kit probe: target 1: id must be a non-empty string$/],
    [[{ ...target, id: 7 }], /^kit probe: target 1: id must be a non-empty string$/],
    [[target, { ...target }], /^kit probe: target 2: id "a" is not unique$/],
    [[{ ...target, scene: "" }], /^kit probe: target "a": scene must be a non-empty string$/],
    [[{ ...target, props: null }], /^kit probe: target "a": props must be an object$/],
    [[{ ...target, props: [] }], /^kit probe: target "a": props must be an object$/],
    [[{ ...target, durationInFrames: 0 }], /^kit probe: target "a": durationInFrames /],
    [[{ ...target, entranceFrames: -1 }], /^kit probe: target "a": entranceFrames /],
    [[{ ...target, exitFrame: 61 }], /^kit probe: target "a": exitFrame /],
  ];
  try {
    for (const [targets, message] of rows) {
      await assert.rejects(
        probe({ entryPoint: "/nonexistent/entry.tsx", targets }),
        (error) => error instanceof Error && message.test(error.message),
        JSON.stringify(targets),
      );
    }
    await assert.rejects(probe({ entryPoint: "", targets: [target] }), /^Error: kit probe: entryPoint must be a non-empty path$/);
    await assert.rejects(
      probe({ entryPoint: "/nonexistent/entry.tsx", targets: [target], publicDir: "" }),
      /^Error: kit probe: publicDir must be a non-empty path$/,
    );
    assert.deepEqual(readdirSync(scratch), []);
  } finally {
    if (saved === undefined) delete process.env.TMPDIR;
    else process.env.TMPDIR = saved;
    rmSync(scratch, { recursive: true, force: true });
  }
});

test("a temporary directory that cannot be created rejects with a named error", async () => {
  const saved = process.env.TMPDIR;
  process.env.TMPDIR = join(tmpdir(), "probe-test-missing", "nested");
  const target = { id: "a", scene: "fits", props: {}, durationInFrames: 10, entranceFrames: 0, exitFrame: 10 };
  try {
    await assert.rejects(
      probe({ entryPoint: "/nonexistent/entry.tsx", targets: [target] }),
      /^Error: kit probe: cannot create a temporary directory \(ENOENT/,
    );
  } finally {
    if (saved === undefined) delete process.env.TMPDIR;
    else process.env.TMPDIR = saved;
  }
});

// --- dependencies -------------------------------------------------------------------

test("the bundler and renderer resolve to Remotion 4.0.537", () => {
  const require = createRequire(new URL("../bin/probe.mjs", import.meta.url));
  assert.equal(require("@remotion/bundler/package.json").version, "4.0.537");
  assert.equal(require("@remotion/renderer/package.json").version, "4.0.537");
});
