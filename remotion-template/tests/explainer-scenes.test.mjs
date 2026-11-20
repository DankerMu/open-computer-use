// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// explainerScenes: the agreement of a validated script.json and its
// timeline.json, and the scene props the renderer passes to components.
// Expected values come from the task 3.8 fixture: the fixture timeline
// totals 481 frames (hook 0–134 with blocks [40, 55), [56, 72) and [73, 89),
// question 134–194, custom 194–271 with blocks [194, 209) and [210, 226),
// concept card 271–331, summary 331–391, end card 391–481), and every
// problem has its own message, ending `; run node bin/kit.mjs timeline`.
// Every scene but the last carries its exit, the transition window
// relative to the scene's start (task 4.5): the windows start at 119, 179,
// 256, 316 and 376.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { explainerScenes } from "../bin/explainer-scenes.mjs";
import { fixtureScript, fixtureTimeline } from "./render/fixtures/explainer/fixture.mjs";
import { coverage, transitionsScript, transitionsTimeline } from "./render/fixtures/transitions/fixture.mjs";

const HINT = "; run node bin/kit.mjs timeline";
const QUESTION = "chapter 1 现象, scene 1 (question)";
const CUSTOM = "chapter 2 原理, scene 1 (custom)";

const block = (text, startFrame, endFrame) => ({ text, startFrame, endFrame });
const timing = (durationInFrames, narrationEndFrame, sentences) => ({
  durationInFrames,
  narrationEndFrame,
  sentences: sentences.map((blocks) => ({ blocks })),
});

const EXPECTED = [
  {
    index: 1,
    where: "hook",
    kind: "hook",
    component: null,
    startFrame: 0,
    durationInFrames: 134,
    exit: { startFrame: 119, frames: 15 },
    props: {
      content: { title: "光的折射" },
      image: null,
      timing: timing(134, 89, [[block("光为什么", 40, 55), block("会弯折", 56, 72), block("在水里", 73, 89)]]),
    },
  },
  {
    index: 2,
    where: QUESTION,
    kind: "question",
    component: null,
    startFrame: 134,
    durationInFrames: 60,
    exit: { startFrame: 45, frames: 15 },
    props: { content: { question: "筷子为何看起来折断" }, image: null, timing: timing(60, 15, [[block("筷子看起来折断", 0, 15)]]) },
  },
  {
    index: 3,
    where: CUSTOM,
    kind: "custom",
    component: "Orbit",
    startFrame: 194,
    durationInFrames: 77,
    exit: { startFrame: 62, frames: 15 },
    props: { image: null, timing: timing(77, 32, [[block("速度变了", 0, 15), block("方向就变", 16, 32)]]) },
  },
  {
    index: 4,
    where: "chapter 3 应用, scene 1 (concept-card)",
    kind: "concept-card",
    component: null,
    startFrame: 271,
    durationInFrames: 60,
    exit: { startFrame: 45, frames: 15 },
    props: {
      content: { title: "透镜", points: ["放大镜"] },
      image: null,
      timing: timing(60, 15, [[block("透镜利用折射", 0, 15)]]),
    },
  },
  {
    index: 5,
    where: "summary",
    kind: "summary",
    component: null,
    startFrame: 331,
    durationInFrames: 60,
    exit: { startFrame: 45, frames: 15 },
    props: { content: { points: ["折射改变方向"] }, image: null, timing: timing(60, 15, [[block("记住这一点", 0, 15)]]) },
  },
  {
    index: 6,
    where: "end-card",
    kind: "end-card",
    component: null,
    startFrame: 391,
    durationInFrames: 90,
    exit: null,
    props: { content: { title: "谢谢观看" }, image: null, timing: timing(90, 0, []) },
  },
];

test("the fixture timeline is the one the fixture lists", () => {
  const timeline = fixtureTimeline(fixtureScript());
  assert.equal(timeline.totalFrames, 481);
  assert.deepEqual(
    timeline.scenes.map(({ kind, startFrame, endFrame }) => [kind, startFrame, endFrame]),
    [
      ["hook", 0, 134],
      ["question", 134, 194],
      ["custom", 194, 271],
      ["concept-card", 271, 331],
      ["summary", 331, 391],
      ["end-card", 391, 481],
    ],
  );
});

test("the fixture gives six entries in playback order with relative timing", () => {
  const entries = explainerScenes(fixtureScript(), fixtureTimeline(fixtureScript()));
  assert.deepEqual(entries, EXPECTED);
  assert.equal(Object.hasOwn(entries[2].props, "content"), false);
});

test("a scene's image name is passed through", () => {
  const script = fixtureScript((raw) => {
    raw.chapters[1].scenes[0].image = "orbit.png";
  });
  assert.equal(explainerScenes(script, fixtureTimeline(script))[2].props.image, "orbit.png");
});

// [name, edit of the fixture timeline, expected message]. One case per
// problem in the fixture's list, in its order.
const PROBLEMS = [
  [
    "a missing block frame",
    (t) => delete t.scenes[2].sentences[0].blocks[1].endFrame,
    "script: timeline.json scenes[2].sentences[0].blocks[1].endFrame is missing or has the wrong type",
  ],
  [
    "a frame that is not an integer",
    (t) => (t.scenes[1].startFrame = 134.5),
    "script: timeline.json scenes[1].startFrame is missing or has the wrong type",
  ],
  ["a version of the wrong type", (t) => (t.version = "1"), "script: timeline.json version is missing or has the wrong type"],
  ["a missing narration record", (t) => delete t.narration, "script: timeline.json narration is missing or has the wrong type"],
  ["a block text of the wrong type", (t) => (t.scenes[0].sentences[0].blocks[0].text = 1), "script: timeline.json scenes[0].sentences[0].blocks[0].text is missing or has the wrong type"],
  ["a missing totalFrames", (t) => delete t.totalFrames, "script: timeline.json totalFrames is missing or has the wrong type"],
  ["another version", (t) => (t.version = 2), "script: timeline.json version is 2, expected 1"],
  ["another fps", (t) => (t.fps = 25), "script: timeline.json fps is 25, expected 30"],
  [
    "another narration file, escaped",
    (t) => (t.narration.file = "public/a b.wav"),
    'script: timeline.json narration.file is "public/a\\u2028b.wav", expected "public/narration.wav"',
  ],
  ["a scene too few", (t) => t.scenes.pop(), "script: timeline.json has 5 scenes, script.json has 6"],
  ["another kind", (t) => (t.scenes[1].kind = "custom"), `${QUESTION}: timeline.json kind is "custom", script.json has "question"`],
  [
    "a sentence too many",
    (t) => t.scenes[2].sentences.push(structuredClone(t.scenes[2].sentences[0])),
    `${CUSTOM}: timeline.json has 2 sentences, script.json has 1`,
  ],
  ["a block too few", (t) => t.scenes[0].sentences[0].blocks.pop(), "hook: timeline.json has 2 blocks in sentence 1, script.json has 3"],
  [
    "another block text",
    (t) => (t.scenes[0].sentences[0].blocks[1].text = "会转弯"),
    "hook, sentence 1 block 2: timeline.json text differs from script.json",
  ],
  ["a first scene not at 0", (t) => (t.scenes[0].startFrame = 1), "hook: timeline.json startFrame 1 is not the previous scene's end 0"],
  [
    "a scene not at the previous end",
    (t) => (t.scenes[1].startFrame = 135),
    `${QUESTION}: timeline.json startFrame 135 is not the previous scene's end 134`,
  ],
  ["an empty scene", (t) => (t.scenes[5].endFrame = 391), "end-card: timeline.json endFrame 391 is not above startFrame 391"],
  [
    "an empty block",
    (t) => (t.scenes[0].sentences[0].blocks[0].endFrame = 40),
    "hook, sentence 1 block 1: timeline.json endFrame 40 is not above startFrame 40",
  ],
  [
    "a block before its scene",
    (t) => (t.scenes[1].sentences[0].blocks[0].startFrame = 130),
    `${QUESTION}, sentence 1 block 1: timeline.json block 130–149 lies outside the scene 134–194`,
  ],
  [
    "a block past its scene",
    (t) => (t.scenes[2].sentences[0].blocks[1].endFrame = 272),
    `${CUSTOM}, sentence 1 block 2: timeline.json block 210–272 lies outside the scene 194–271`,
  ],
  ["another totalFrames", (t) => (t.totalFrames = 480), "script: timeline.json totalFrames 480 is not the last scene's end 481"],
  // The transition checks (task 4.5), each case breaking only its own check.
  [
    "a missing transition",
    (t) => delete t.scenes[2].transitionOut,
    "script: timeline.json scenes[2].transitionOut is missing or has the wrong type",
  ],
  [
    "a transition length of the wrong type",
    (t) => (t.scenes[2].transitionOut.frames = "15"),
    "script: timeline.json scenes[2].transitionOut.frames is missing or has the wrong type",
  ],
  [
    "a transition on the last scene",
    (t) => (t.scenes[5].transitionOut = { startFrame: 466, frames: 15 }),
    "end-card: timeline.json the last scene has a transition",
  ],
  ["no transition on a scene before the last", (t) => (t.scenes[1].transitionOut = null), `${QUESTION}: timeline.json scene has no transition`],
  [
    "a transition of another length",
    (t) => (t.scenes[1].transitionOut = { startFrame: 178, frames: 16 }),
    `${QUESTION}: timeline.json transition is 16 frames, expected 15`,
  ],
  [
    "a transition ending before its scene's end",
    (t) => (t.scenes[1].transitionOut = { startFrame: 178, frames: 15 }),
    `${QUESTION}: timeline.json transition ends at 193, not at the scene's end 194`,
  ],
  [
    "a transition starting before the last block ends",
    (t) => (t.scenes[1].sentences[0].blocks[0].endFrame = 180),
    `${QUESTION}: timeline.json transition starts at 179, before the last block ends at 180`,
  ],
];

for (const [name, edit, message] of PROBLEMS) {
  test(`${name} throws its message`, () => {
    const script = fixtureScript();
    const timeline = fixtureTimeline(script);
    edit(timeline);
    assert.throws(
      () => explainerScenes(script, timeline),
      (error) => error instanceof Error && error.message === `${message}${HINT}`,
    );
  });
}

test("a transition is measured from the last block, not from narrationEndFrame", () => {
  const script = fixtureScript();
  const timeline = fixtureTimeline(script);
  timeline.scenes[1].narrationEndFrame = 190;
  assert.deepEqual(explainerScenes(script, timeline)[1].exit, { startFrame: 45, frames: 15 });
});

test("the transitions fixture covers the three gap cases and passes, every exit its transitionOut", () => {
  const script = transitionsScript();
  const timeline = transitionsTimeline(script);
  assert.equal(timeline.totalFrames, 550);
  assert.deepEqual(coverage(timeline), { sceneEnd: 1, chapterStart: 0, stretched: 4 });
  const entries = explainerScenes(script, timeline);
  assert.equal(entries.length, 7);
  for (const fixture of [[script, timeline], [fixtureScript(), fixtureTimeline(fixtureScript())]]) {
    const [s, t] = fixture;
    explainerScenes(s, t).forEach((entry, i) => {
      const scene = t.scenes[i];
      const expected = i === t.scenes.length - 1 ? null : { startFrame: scene.transitionOut.startFrame - scene.startFrame, frames: 15 };
      assert.deepEqual(entry.exit, expected, entry.where);
    });
  }
});

test("a timeline that is not an object names its first field", () => {
  assert.throws(() => explainerScenes(fixtureScript(), null), { message: `script: timeline.json version is missing or has the wrong type${HINT}` });
});

test("an extra scene is a count problem, not an unnamed error", () => {
  const script = fixtureScript();
  const timeline = fixtureTimeline(script);
  timeline.scenes.push({ ...structuredClone(timeline.scenes[5]), startFrame: 481, endFrame: 571 });
  assert.throws(() => explainerScenes(script, timeline), { message: `script: timeline.json has 7 scenes, script.json has 6${HINT}` });
});

// --- the bundle boundary ------------------------------------------------------

// The specifiers of every static import, re-export and dynamic import.
function specifiers(path) {
  const source = readFileSync(new URL(path, import.meta.url), "utf8");
  const found = [];
  for (const match of source.matchAll(/^\s*(?:import|export)\b[^;'"]*?\bfrom\s*["']([^"']+)["']/gm)) found.push(match[1]);
  for (const match of source.matchAll(/^\s*import\s*["']([^"']+)["']/gm)) found.push(match[1]);
  for (const match of source.matchAll(/\bimport\s*\(\s*["']([^"']+)["']/g)) found.push(match[1]);
  return found;
}

test("the modules the bundle imports from bin/ and captions.ts import no node: built-in", () => {
  assert.deepEqual(specifiers("../bin/explainer-scenes.mjs"), ["./script-schema.mjs", "./scene-timing.mjs"]);
  assert.deepEqual(specifiers("../bin/scene-timing.mjs"), []);
  assert.deepEqual(specifiers("../bin/script-schema.mjs"), ["zod"]);
  assert.deepEqual(specifiers("../src/kit/render/captions.ts"), ["@remotion/captions"]);
});
