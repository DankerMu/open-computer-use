// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// layout() and loudnessOk() of bin/scene-timing.mjs, pure, with trimmed
// sample counts given directly. Every expected frame below was worked out by
// hand from the task 3.6 rules (1600 samples per frame, 2880-sample block
// gap, 10/20/45-frame gaps, 40-frame lead-in, 90-frame tail, 60-frame
// minimum), not by running the module. The entrance, reveal and rest tables
// of task 5.1 are checked against that fixture's numbers.

import { test } from "node:test";
import assert from "node:assert/strict";
import { BODY_KINDS, VIDEO, validateScript } from "../bin/script-schema.mjs";
import { blockStems } from "../bin/voice-stem.mjs";
import {
  ENTRANCE_FRAMES,
  FPS,
  GAPS,
  LENGTH_WINDOW,
  MIN_SCENE_FRAMES,
  REST_FRAMES,
  REVEALS,
  SAMPLE_RATE,
  TRANSITION_FRAMES,
  layout,
  loudnessOk,
  revealsOf,
} from "../bin/scene-timing.mjs";

// Hook (2 sentences), chapter 1 with two scenes, chapter 2 with one custom
// scene of two sentences, chapter 3 with one scene, summary, end card.
function script(endCardNarration) {
  const raw = {
    version: 1,
    title: "光的折射",
    music: "none",
    hook: { kind: "hook", narration: [["甲", "乙"], ["丙"]], content: { title: "筷子为什么弯了" } },
    chapters: [
      {
        name: "现象",
        scenes: [
          { kind: "question", narration: [["丁"]], content: { question: "筷子真的弯了吗" } },
          { kind: "concept-card", narration: [["戊", "己"]], content: { title: "折射", points: ["光拐弯"] } },
        ],
      },
      { name: "原理", scenes: [{ kind: "custom", narration: [["庚"], ["辛"]], component: "Refraction" }] },
      { name: "应用", scenes: [{ kind: "question", narration: [["壬"]], content: { question: "眼镜如何矫正" } }] },
    ],
    summary: { kind: "summary", narration: [["癸"]], content: { points: ["折射改变光路"] } },
    endCard: { kind: "end-card", content: { title: "谢谢观看" } },
  };
  if (endCardNarration) raw.endCard.narration = [["子"]];
  const result = validateScript(JSON.stringify(raw));
  assert.equal(result.ok, true, JSON.stringify(result.errors));
  return result.script;
}

// 甲 and 戊 end mid-frame, so a sentence's second block starts 2880 samples
// after the first's end, not after its frame. 丁 is long enough that its
// in-chapter scene end (narration plus 20) exceeds the 60-frame minimum.
const SAMPLES = { 甲: 47000, 乙: 24000, 丙: 16000, 丁: 68000, 戊: 47000, 己: 48000, 庚: 40000, 辛: 33600, 壬: 80000, 癸: 1601, 子: 48000 };

function timed(endCardNarration) {
  const s = script(endCardNarration);
  return { s, timeline: layout(s, blockStems(s).map((entry) => ({ ...entry, samples: SAMPLES[entry.text] }))) };
}

const block = (timeline, text) =>
  timeline.scenes.flatMap((scene) => scene.sentences.flatMap((sentence) => sentence.blocks)).find((b) => b.text === text);

test("the constants are the fixture's numbers", () => {
  assert.equal(FPS, 30);
  assert.equal(FPS, VIDEO.fps);
  assert.equal(SAMPLE_RATE, 48000);
  assert.equal(SAMPLE_RATE / FPS, 1600);
  assert.deepEqual(GAPS, { block: 2880, sentence: 10, sceneEnd: 20, chapterStart: 45, leadIn: 40, tail: 90 });
  assert.equal(TRANSITION_FRAMES, 15);
  assert.deepEqual(LENGTH_WINDOW, { minFrames: 1800, maxFrames: 5400 });
});

test("MIN_SCENE_FRAMES has one floor per kind: 60, and 90 for the end card", () => {
  const kinds = ["hook", "summary", "end-card", "custom", ...BODY_KINDS];
  assert.deepEqual(Object.keys(MIN_SCENE_FRAMES).sort(), [...kinds].sort());
  for (const kind of kinds) assert.equal(MIN_SCENE_FRAMES[kind], kind === "end-card" ? 90 : 60, kind);
});

test("a 3-chapter script lays out at the hand-computed frames", () => {
  const { timeline } = timed(false);
  assert.deepEqual(Object.keys(timeline), ["version", "fps", "sampleRate", "totalFrames", "scenes"]);
  assert.equal(timeline.version, 1);
  assert.equal(timeline.fps, 30);
  assert.equal(timeline.sampleRate, 48000);
  const summary = timeline.scenes.map(({ index, slot, chapter, scene, kind, startFrame, endFrame, narrationEndFrame, minFrames }) => [
    index,
    slot,
    chapter,
    scene,
    kind,
    startFrame,
    endFrame,
    narrationEndFrame,
    minFrames,
  ]);
  assert.deepEqual(summary, [
    [1, "hook", null, null, "hook", 0, 152, 107, 60],
    [2, "chapter", 1, 1, "question", 152, 215, 195, 60],
    [3, "chapter", 1, 2, "concept-card", 215, 322, 277, 60],
    [4, "chapter", 2, 1, "custom", 322, 423, 378, 60],
    [5, "chapter", 3, 1, "question", 423, 493, 473, 60],
    [6, "summary", null, null, "summary", 493, 553, 495, 60],
    [7, "end-card", null, null, "end-card", 553, 643, 553, 90],
  ]);
  assert.equal(timeline.totalFrames, 643);
  const blocks = Object.fromEntries(
    Object.keys(SAMPLES)
      .filter((text) => text !== "子")
      .map((text) => {
        const { stem, startSample, endSample, startFrame, endFrame } = block(timeline, text);
        assert.deepEqual(Object.keys(block(timeline, text)), ["stem", "text", "startSample", "endSample", "startFrame", "endFrame"]);
        assert.match(stem, /^s\d-\d-\d-[0-9a-f]{8}$/);
        return [text, [startSample, endSample, startFrame, endFrame]];
      }),
  );
  assert.deepEqual(blocks, {
    甲: [64000, 111000, 40, 70],
    乙: [113880, 137880, 71, 87],
    丙: [155200, 171200, 97, 107],
    丁: [243200, 311200, 152, 195],
    戊: [344000, 391000, 215, 245],
    己: [393880, 441880, 246, 277],
    庚: [515200, 555200, 322, 347],
    辛: [571200, 604800, 357, 378],
    壬: [676800, 756800, 423, 473],
    癸: [788800, 790401, 493, 495],
  });
});

test("the gaps: lead-in 40, 0.06 s in a sentence, 10 between sentences, 45 before a chapter, 20 elsewhere", () => {
  const { timeline } = timed(false);
  assert.equal(block(timeline, "甲").startFrame, 40);
  assert.equal(block(timeline, "乙").startSample - block(timeline, "甲").endSample, 2880);
  assert.equal(block(timeline, "己").startSample - block(timeline, "戊").endSample, 2880);
  assert.equal(block(timeline, "丙").startFrame - Math.ceil(block(timeline, "乙").endSample / 1600), 10);
  assert.equal(block(timeline, "辛").startFrame - Math.ceil(block(timeline, "庚").endSample / 1600), 10);
  const gap = (index) => {
    const scene = timeline.scenes[index - 1];
    return scene.endFrame - scene.narrationEndFrame;
  };
  // Scenes 1, 3 and 4 precede a chapter's first scene; scene 2 precedes the
  // next scene of its chapter and scene 5 the summary. Scene 6 is extended
  // to its minimum instead.
  assert.equal(gap(1), 45);
  assert.equal(gap(2), 20);
  assert.equal(gap(3), 45);
  assert.equal(gap(4), 45);
  assert.equal(gap(5), 20);
  for (const [i, scene] of timeline.scenes.entries()) {
    assert.equal(scene.startFrame, i === 0 ? 0 : timeline.scenes[i - 1].endFrame);
  }
  const lengths = timeline.scenes.map((scene) => scene.endFrame - scene.startFrame);
  assert.equal(
    timeline.totalFrames,
    lengths.reduce((a, b) => a + b, 0),
  );
});

test("a scene shorter than its minimum is extended to it", () => {
  const { timeline } = timed(false);
  const summary = timeline.scenes[5];
  // 495 + 20 = 515 is shorter than 493 + 60.
  assert.equal(summary.narrationEndFrame + 20, 515);
  assert.equal(summary.endFrame - summary.startFrame, 60);
});

test("an end card lasts 90 frames without narration, and its narration plus 90 with it", () => {
  const silent = timed(false).timeline.scenes.at(-1);
  assert.equal(silent.endFrame - silent.startFrame, 90);
  assert.deepEqual(silent.sentences, []);
  assert.equal(silent.narrationEndFrame, silent.startFrame);
  const { timeline } = timed(true);
  const card = timeline.scenes.at(-1);
  assert.deepEqual([card.startFrame, card.narrationEndFrame, card.endFrame], [553, 583, 673]);
  assert.deepEqual(block(timeline, "子").startFrame, 553);
  assert.equal(timeline.totalFrames, 673);
});

test("every transition lies after its scene's narration and ends with the scene", () => {
  for (const endCard of [false, true]) {
    const { timeline } = timed(endCard);
    const transitions = timeline.scenes.filter((scene) => scene.transitionOut !== null);
    assert.equal(transitions.length, timeline.scenes.length - 1);
    for (const scene of transitions) {
      assert.deepEqual(Object.keys(scene.transitionOut), ["startFrame", "frames"]);
      assert.equal(scene.transitionOut.frames, 15);
      assert.ok(scene.narrationEndFrame <= scene.transitionOut.startFrame, `scene ${scene.index}`);
      assert.equal(scene.transitionOut.startFrame + 15, scene.endFrame);
    }
    assert.equal(timeline.scenes.at(-1).transitionOut, null);
  }
});

test("a custom scene sits in its playback position", () => {
  const { s, timeline } = timed(false);
  const custom = timeline.scenes[3];
  assert.deepEqual([custom.index, custom.slot, custom.chapter, custom.scene, custom.kind], [4, "chapter", 2, 1, "custom"]);
  const stems = blockStems(s).filter((entry) => entry.scene === 4).map((entry) => entry.stem);
  assert.deepEqual(
    custom.sentences.map((sentence) => sentence.blocks.map((b) => b.stem)),
    stems.map((stem) => [stem]),
  );
});

test("layout is pure: same input, same output, input untouched", () => {
  const s = script(true);
  const blocks = blockStems(s).map((entry) => ({ ...entry, samples: SAMPLES[entry.text] }));
  const before = JSON.stringify([s, blocks]);
  assert.deepEqual(layout(s, blocks), layout(s, blocks));
  assert.equal(JSON.stringify([s, blocks]), before);
});

test("loudnessOk accepts -16 ± 1 LUFS at -1.0 dBTP or below", () => {
  assert.equal(loudnessOk(-15.0, -1.0), true);
  assert.equal(loudnessOk(-17.0, -1.0), true);
  assert.equal(loudnessOk(-16.0, -12.3), true);
  assert.equal(loudnessOk(-14.9, -1.0), false);
  assert.equal(loudnessOk(-17.1, -1.0), false);
  assert.equal(loudnessOk(-16.0, -0.9), false);
});

// --- entrances, reveals and rest (task 5.1) -----------------------------------

test("ENTRANCE_FRAMES has one entry per MIN_SCENE_FRAMES kind, each the kit-wide 15", () => {
  assert.deepEqual(Object.keys(ENTRANCE_FRAMES).sort(), Object.keys(MIN_SCENE_FRAMES).sort());
  for (const [kind, frames] of Object.entries(ENTRANCE_FRAMES)) assert.equal(frames, 15, kind);
});

test("REVEALS is empty, REST_FRAMES is 30 for a key reveal and 15 for a batch", () => {
  assert.deepEqual(REVEALS, {});
  assert.deepEqual(REST_FRAMES, { key: 30, batch: 15 });
});

test("revealsOf: one key reveal over the entrance by default, else the declared list", () => {
  const timing = { durationInFrames: 60, narrationEndFrame: 15, sentences: [] };
  assert.deepEqual(revealsOf("question", timing), [{ type: "key", startFrame: 0, endFrame: 15 }]);
  assert.deepEqual(revealsOf("end-card", timing, {}), [{ type: "key", startFrame: 0, endFrame: 15 }]);
  const declared = { question: (t) => [{ type: "batch", startFrame: 2, endFrame: t.durationInFrames - 20 }] };
  assert.deepEqual(revealsOf("question", timing, declared), [{ type: "batch", startFrame: 2, endFrame: 40 }]);
  assert.deepEqual(revealsOf("hook", timing, declared), [{ type: "key", startFrame: 0, endFrame: 15 }]);
});
