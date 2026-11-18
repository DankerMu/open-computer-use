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

import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtempSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { probe, violations } from "../../bin/probe.mjs";
import { themes } from "../../src/kit/theme.ts";

const ENTRY = fileURLToPath(new URL("./fixtures/entry.tsx", import.meta.url));
const PUBLIC = fileURLToPath(new URL("./fixtures/public", import.meta.url));
const PROBE = fileURLToPath(new URL("../../bin/probe.mjs", import.meta.url));
const theme = themes.light;
const ROLES = ["entrance-end", "midpoint", "pre-exit"];
const TOLERANCE = 0.5;
const P1 = "div:nth-child(1)";

function target(id, scene, durationInFrames, entranceFrames, exitFrame = durationInFrames) {
  return { id, scene, props: {}, durationInFrames, entranceFrames, exitFrame };
}

// [id, scene, durationInFrames, entranceFrames, probe frames by role,
// [rule, path] expected at each of `roles`]. Frames come from the fixture's
// formulas worked by hand: 60 frames with a 10-frame entrance give 10, 30
// and 59; a 1-frame scene gives 0 three times.
const F60 = [10, 30, 59];
const CASES = [
  // First: the page's initial frame, a 1-frame target holding an <Img>.
  ["late-image-1f", "late-image", 1, 0, [0, 0, 0], [["outside safe area", `${P1}>div:nth-child(2)`]]],
  ["fits", "fits", 90, 15, [15, 45, 89], []],
  // Not last: a scene that saw the composition's remaining length instead
  // of its own would still be inside the safe area at its frame 59.
  ["late-overflow", "late-overflow", 60, 10, F60, [["outside safe area", P1]], ["pre-exit"]],
  // entranceEnd 30 equals the midpoint, floor(60 / 2).
  ["fits-entrance-mid", "fits", 60, 30, [30, 30, 59], []],
  ["font-absent", "font-absent", 10, 0, [0, 5, 9], []],
  ["camera", "camera", 60, 10, F60, []],
  ["clipped-abs", "clipped-abs", 60, 10, F60, []],
  ["fixed-clipped", "fixed-clipped", 60, 10, F60, []],
  // Only the safe-area rule: the static wrapper is no clipping ancestor.
  ["escaped-abs", "escaped-abs", 60, 10, F60, [["outside safe area", `${P1}>div:nth-child(1)`]]],
  // A clip-path clips whatever the containing block.
  ["clip-path-noncb", "clip-path-noncb", 60, 10, F60, [["mask clips", `${P1}>div:nth-child(1)`]]],
  ["backdrop", "backdrop", 60, 10, F60, [["mask clips", `${P1}>div:nth-child(1)>div:nth-child(1)`]]],
  ["rotate", "rotate", 60, 10, F60, [["mask clips", `${P1}>div:nth-child(1)>div:nth-child(1)`]]],
  // display: contents contains nothing, so the wrapper does not clip.
  ["contents", "contents", 60, 10, F60, [["outside safe area", `${P1}>div:nth-child(1)>div:nth-child(1)`]]],
  ["clip-negative", "clip-negative", 60, 10, F60, []],
  ["edge-over", "edge-over", 60, 10, F60, [["outside safe area", P1]]],
  ["small-font", "small-font", 60, 10, F60, [["font size", P1]]],
  ["wrap-start", "wrap-start", 60, 10, F60, [["line start", P1]]],
  ["scroll", "scroll", 60, 10, F60, [["scrolls", P1], ["mask clips", P1]]],
  ["wrap-start-lines", "wrap-start-lines", 60, 10, F60, [["line start", `${P1}>div:nth-child(2)`]]],
  ["wrap-start-spaces", "wrap-start-spaces", 60, 10, F60, [["line start", `${P1}>div:nth-child(2)`]]],
  ["scaled-font", "scaled-font", 60, 10, F60, [["font size", `${P1}>div:nth-child(1)`]]],
  ["inline-small", "inline-small", 60, 10, F60, [["font size", P1]]],
  ["mask-small", "mask-small", 60, 10, F60, [["mask clips", `${P1}>div:nth-child(1)`], ["mask clips", "div:nth-child(2)"]]],
  // Chrome computes display: block for SVG <text>, so <text> holds the record.
  ["svg-font", "svg-font", 60, 10, F60, [["font size", "svg:nth-child(1)>text:nth-child(1)"]]],
  ["text-differs", "text-differs", 60, 10, F60, [["text differs", P1]]],
  ["custom-overflow", "custom-overflow", 60, 10, F60, [["outside safe area", P1]]],
  // A later target: its Sequence mounts the <Img> at its entrance-end frame.
  ["late-image", "late-image", 60, 10, F60, [["outside safe area", `${P1}>div:nth-child(2)`]]],
  ["delayed-move", "delayed-move", 60, 10, F60, [["outside safe area", P1]]],
];
const CLEAN = CASES.filter((c) => c[5].length === 0).map(([id]) => id);
const TARGETS = CASES.map(([id, scene, duration, entrance]) => target(id, scene, duration, entrance));
const EXPECTED = CASES.flatMap(([id, , , , frames, rules, roles = ROLES]) =>
  ROLES.flatMap((role, k) =>
    roles.includes(role) ? rules.map(([rule, path]) => ({ target: id, role, frame: frames[k], rule, path })) : [],
  ),
);

const PARAGRAPH = "光线从空气斜射入水中时，传播方向发生改变。";
const FITS_TEXTS = [
  ["div:nth-child(1)>div:nth-child(1)", "光线从空气斜射入水中时，", ["光线从空气斜射入水中时，"]],
  ["div:nth-child(1)>div:nth-child(2)", "传播方向发生改变。", ["传播方向发生改变。"]],
  ["div:nth-child(2)", "右侧对齐", ["右侧对齐"]],
  ["div:nth-child(3)", "最小字号", ["最小字号"]],
  ["div:nth-child(4)>div:nth-child(1)", "遮罩文字", ["遮罩文字"]],
  ["div:nth-child(5)", "揭示文字", ["揭示文字"]],
  ["div:nth-child(6)", "光在水中传播变慢", ["光在水中传", "播变慢"]],
  ["svg:nth-child(8)>text:nth-child(1)", "矢量文字", ["矢量文字"]],
  ["div:nth-child(9)>div:nth-child(1)>div:nth-child(1)", "放大遮罩", ["放大遮罩"]],
];
const FITS_HIDDEN = [
  { path: "div:nth-child(7)", text: "隐藏文字" },
  { path: "div:nth-child(10)>div:nth-child(1)", text: "祖先透明" },
  { path: "div:nth-child(11)>div:nth-child(1)", text: "祖先隐藏" },
];

let first;
let second;
const timings = [];
// The successful probes run with TMPDIR here, which must end up empty.
const probeTmp = mkdtempSync(join(tmpdir(), "probe-render-tmp-"));
const savedTmp = process.env.TMPDIR;

async function timedProbe() {
  const started = Date.now();
  const result = await probe({ entryPoint: ENTRY, targets: TARGETS, publicDir: PUBLIC });
  timings.push(Date.now() - started);
  return result;
}

before(async () => {
  process.env.TMPDIR = probeTmp;
  try {
    first = await timedProbe();
    second = await timedProbe();
  } finally {
    if (savedTmp === undefined) delete process.env.TMPDIR;
    else process.env.TMPDIR = savedTmp;
  }
});

after(() => rmSync(probeTmp, { recursive: true, force: true }));

// Live headless shells: a zombie has an empty cmdline.
function headlessShells() {
  return readdirSync("/proc")
    .filter((name) => /^\d+$/.test(name))
    .filter((pid) => {
      try {
        return readFileSync(`/proc/${pid}/cmdline`, "utf8").includes("chrome-headless-shell");
      } catch {
        return false; // the process ended between readdir and read
      }
    });
}

async function until(check, timeoutMs) {
  const started = Date.now();
  while (!check()) {
    if (Date.now() - started > timeoutMs) return false;
    await new Promise((done) => setTimeout(done, 100));
  }
  return true;
}

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
  const keys = ["fontPresent", "frame", "hidden", "marked", "masks", "role", "scrolls", "target", "texts"];
  for (const frame of first.frames) assert.deepEqual(Object.keys(frame).sort(), keys, `${frame.target} ${frame.role}`);
});

test("fits and the other clean scenes break no rule at any probe frame", () => {
  const clean = first.frames.filter((frame) => CLEAN.includes(frame.target));
  assert.deepEqual(CLEAN, ["fits", "fits-entrance-mid", "font-absent", "camera", "clipped-abs", "fixed-clipped", "clip-negative"]);
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
    assert.deepEqual(frame.hidden, FITS_HIDDEN, label);
    assert.deepEqual(frame.scrolls, [], label);

    const [line1, line2, edge, minimum, maskText, reveal, , svgText, scaledMaskText] = frame.texts;
    // The paragraph's two block lines at 48 px with line height 1.4.
    near(union([line1.glyphBox, line2.glyphBox]).height, 2 * 1.4 * 48, `${label} paragraph height`);
    near(edge.glyphBox.x + edge.glyphBox.width, 1824, `${label} edge right`);
    assert.equal(minimum.minFontSizePx, 32, label);
    assert.equal(line1.minFontSizePx, 48, label);
    assert.equal(svgText.minFontSizePx, 48, label);
    assert.equal(line1.scale, 1, label);
    assert.equal(scaledMaskText.scale, 2, label);
    assert.equal(scaledMaskText.minFontSizePx, 96, label);
    // The glyph box is trimmed to the scaled line box, 67.2 px × 2.
    near(scaledMaskText.glyphBox.height, 134.4, `${label} scaled glyph height`);

    assert.deepEqual(maskText.clippedBy, ["div:nth-child(4)"], label);
    assert.deepEqual(reveal.clippedBy, ["div:nth-child(5)"], label);
    // An <svg> clips to its viewport: the UA style sets overflow: hidden.
    assert.deepEqual(
      frame.masks.map(({ path }) => path),
      ["div:nth-child(4)", "div:nth-child(5)", "svg:nth-child(8)", "div:nth-child(9)>div:nth-child(1)"],
      label,
    );
    assert.equal(frame.masks[0].clipPath, "none", label);
    assert.match(frame.masks[1].clipPath, /^inset\(/, label);
    // The overflow mask is its 192 × 67.2 padding box; the reveal's inset
    // reached 0, so its box is the element's border box: 4 glyphs of 48 px
    // wide, one 67.2 px line (48 px × 1.4) high.
    const [overflowMask, revealMask, svgMask, scaledMask] = frame.masks.map((mask) => mask.clipBox);
    for (const [actual, expected, name] of [
      [overflowMask, { x: 200, y: 420, width: 192, height: 67.2 }, "overflow mask"],
      [revealMask, { x: 200, y: 560, width: 192, height: 67.2 }, "reveal mask"],
      [svgMask, { x: 1000, y: 700, width: 400, height: 100 }, "svg viewport"],
      [scaledMask, { x: 1400, y: 840, width: 384, height: 134.4 }, "scaled mask"],
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
  assert.deepEqual(
    found.map(({ target: id, role, frame, rule, path }) => ({ target: id, role, frame, rule, path })),
    EXPECTED,
  );
  // The line-start scenes break at the comma, spaces before it or not.
  for (const v of found.filter((v) => v.rule === "line start")) {
    assert.equal(v.detail, 'line 2 starts with "，"', `${v.target} ${v.role}`);
  }
});

test("text that waits on an image or a scene's own delayRender is measured after it", () => {
  for (const id of ["late-image-1f", "late-image"]) {
    for (const frame of framesOf(first, id)) {
      const [text] = frame.texts.filter((record) => record.text === "图片下方");
      // Below the 1000 px tall image, which starts at y = 60.
      assert.ok(text.glyphBox.y > 1050, `${id} ${frame.role}: text at y ${text.glyphBox.y}`);
    }
  }
  for (const frame of framesOf(first, "delayed-move")) {
    assert.equal(frame.texts[0].glyphBox.x, 1700, frame.role);
  }
});

test("clipped overflow, negative insets and inline sizes are recorded as rendered", () => {
  for (const frame of framesOf(first, "camera")) {
    const [record] = frame.texts;
    assert.equal(record.text, "镜头外文字", frame.role);
    // Past the safe area's right edge, wholly outside its 200–600 viewport.
    assert.ok(record.glyphBox.x > 1824, `${frame.role}: x ${record.glyphBox.x}`);
  }
  for (const frame of framesOf(first, "escaped-abs")) {
    const [record] = frame.texts;
    assert.deepEqual(record.clippedBy, [], `${frame.role}: the static wrapper does not clip`);
    near(record.glyphBox.x, 10, `${frame.role} escaped x`);
  }
  for (const frame of framesOf(first, "clip-path-noncb")) {
    // Only the clip-path part: x 0–500 of the 1000 x 400 wrapper.
    const [{ clipBox }] = frame.masks;
    const expected = { x: 0, y: 0, width: 500, height: 400 };
    for (const key of Object.keys(expected)) near(clipBox[key], expected[key], `${frame.role} clip-path ${key}`);
  }
  for (const frame of framesOf(first, "contents")) {
    assert.deepEqual(frame.texts[0].clippedBy, [], `${frame.role}: display: contents contains nothing`);
  }
  for (const frame of framesOf(first, "clipped-abs")) {
    assert.deepEqual(frame.texts[0].clippedBy, [P1], frame.role);
  }
  for (const frame of framesOf(first, "fixed-clipped")) {
    assert.deepEqual(frame.texts[0].clippedBy, [P1], frame.role);
  }
  for (const frame of framesOf(first, "clip-negative")) {
    const [{ clipBox }] = frame.masks;
    const expected = { x: 190, y: 190, width: 306, height: 87.2 };
    for (const key of Object.keys(expected)) near(clipBox[key], expected[key], `${frame.role} clip ${key}`);
  }
  for (const frame of framesOf(first, "inline-small")) {
    assert.deepEqual(
      frame.texts.map(({ text, minFontSizePx }) => [text, minFontSizePx]),
      [["正文注大字", 24]],
      frame.role,
    );
  }
});

test("probing the same targets twice gives deep-equal results", (t) => {
  t.diagnostic(`probe durations (bundle + render, ${TARGETS.length} targets): ${timings.join(" ms, ")} ms`);
  assert.deepEqual(second, first);
});

test("successful probes leave nothing under TMPDIR and no headless shell running", () => {
  assert.deepEqual(readdirSync(probeTmp), []);
  assert.deepEqual(headlessShells(), []);
});

test("an unknown scene key rejects with a named error", async () => {
  await assert.rejects(
    probe({ entryPoint: ENTRY, publicDir: PUBLIC, targets: [target("missing", "no-such-scene", 10, 0)] }),
    (error) => error instanceof Error && error.message.startsWith("kit probe: ") && error.message.includes('kit probe: unknown scene "no-such-scene"'),
  );
});

test("a scene that throws rejects, naming its target and frame", async () => {
  // The second target starts at composition frame 30; its first probe
  // frame is its own frame 5.
  await assert.rejects(
    probe({ entryPoint: ENTRY, publicDir: PUBLIC, targets: [target("ok", "fits", 30, 5), target("boom", "throws", 30, 5)] }),
    (error) =>
      error instanceof Error &&
      error.message.startsWith('kit probe: target "boom" frame 5: ') &&
      error.message.includes("fixture scene throws"),
  );
});

test("a scene whose own delayRender never releases rejects, naming it", async () => {
  await assert.rejects(
    probe({ entryPoint: ENTRY, publicDir: PUBLIC, targets: [target("stuck", "stuck", 10, 0)] }),
    (error) =>
      error instanceof Error &&
      error.message.startsWith('kit probe: target "stuck" frame 0: ') &&
      error.message.includes('still waits on delayRender "fixture: never released" after 20000 ms'),
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

test("SIGTERM during a probe ends node within 20 s and leaves no headless shell", async (t) => {
  assert.deepEqual(headlessShells(), [], "no shell before the run");
  // Five scenes that each hold the render for about 1 s keep it busy.
  const targets = [1, 2, 3, 4, 5].map((n) => target(`move-${n}`, "delayed-move", 10, 0));
  const script = [
    `import { probe } from ${JSON.stringify(PROBE)};`,
    `await probe(${JSON.stringify({ entryPoint: ENTRY, publicDir: PUBLIC, targets })});`,
  ].join("\n");
  const child = spawn(process.execPath, ["--input-type=module", "-e", script], { stdio: "ignore" });
  const exited = new Promise((done) => child.once("exit", (code, signal) => done({ code, signal })));
  try {
    assert.ok(await until(() => headlessShells().length > 0, 120000), "a headless shell started");
    // Let selectComposition finish and the render begin.
    await new Promise((done) => setTimeout(done, 3000));
    const sent = Date.now();
    child.kill("SIGTERM");
    // Measured under amd64 emulation: 7.2–8.0 s when Remotion's own SIGTERM
    // handler kills the browser and the probe rejects (code 1), or a few ms
    // when the signal ends node directly. 20 s leaves margin over that.
    const ended = await Promise.race([exited, new Promise((done) => setTimeout(() => done(null), 20000))]);
    assert.notEqual(ended, null, "node exited within 20 s of SIGTERM");
    t.diagnostic(`node exited ${Date.now() - sent} ms after SIGTERM with ${JSON.stringify(ended)}`);
    assert.ok(await until(() => headlessShells().length === 0, 2000), `shells left: ${headlessShells().join(", ")}`);
  } finally {
    child.kill("SIGKILL");
  }
});
