// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
// Source: Vincentwei1021/anything2explainer 4f738189d866a8a1d1abd008456303617279a2c2 reference/motion-vocabulary.md PolyForm-Noncommercial-1.0.0
//
// Where every scene and caption block of an explainer lies in time. Only the
// gap numbers come from the source above (10 frames within a paragraph, 45
// before a chapter), no code. Positions are kept in samples at 48 kHz and
// turned into 30 fps frames at the edges; nothing here comes from the model.
// Pure and import-free.

export const FPS = 30;
export const SAMPLE_RATE = 48000;

const SAMPLES_PER_FRAME = SAMPLE_RATE / FPS;

// Frames, except block, which is samples (0.06 s between trimmed segments of
// one sentence).
export const GAPS = { block: 2880, sentence: 10, sceneEnd: 20, chapterStart: 45, leadIn: 40, tail: 90 };

export const TRANSITION_FRAMES = 15;

// 60–180 s, both bounds inclusive.
export const LENGTH_WINDOW = { minFrames: 1800, maxFrames: 5400 };

// Conservative floors until the components exist; tasks 4.2–4.4 replace each
// with its component's declared minimum. The end card's stays at most the
// 90-frame tail.
export const MIN_SCENE_FRAMES = {
  hook: 60,
  question: 60,
  "concept-card": 60,
  "process-steps": 60,
  "cause-effect": 60,
  comparison: 60,
  timeline: 60,
  "key-number": 60,
  chart: 60,
  "image-callouts": 60,
  "definition-quote": 60,
  custom: 60,
  summary: 60,
  "end-card": 90,
};

const LOUDNESS = { target: -16, range: 1, maxTruePeak: -1.0 };

const ceilFrame = (sample) => Math.ceil(sample / SAMPLES_PER_FRAME);

// The script's scenes in playback order with their slot and, in a chapter,
// their 1-based chapter and scene numbers.
function playback(script) {
  const chapterScenes = script.chapters.flatMap((chapter, c) =>
    chapter.scenes.map((scene, s) => ({ scene, slot: "chapter", chapter: c + 1, number: s + 1 })),
  );
  return [
    { scene: script.hook, slot: "hook", chapter: null, number: null },
    ...chapterScenes,
    { scene: script.summary, slot: "summary", chapter: null, number: null },
    { scene: script.endCard, slot: "end-card", chapter: null, number: null },
  ];
}

// Places one scene's blocks from startFrame; returns the sentences and the
// narration's end frame (startFrame when the scene has no blocks).
function placeBlocks(sceneBlocks, startFrame, leadIn) {
  const sentences = [];
  let cursor = (startFrame + leadIn) * SAMPLES_PER_FRAME;
  let lastEnd = null;
  for (const entry of sceneBlocks) {
    if (lastEnd !== null) {
      cursor = entry.block === 0 ? (ceilFrame(lastEnd) + GAPS.sentence) * SAMPLES_PER_FRAME : lastEnd + GAPS.block;
    }
    if (entry.block === 0) sentences.push({ blocks: [] });
    const endSample = cursor + entry.samples;
    sentences.at(-1).blocks.push({
      stem: entry.stem,
      text: entry.text,
      startSample: cursor,
      endSample,
      startFrame: Math.floor(cursor / SAMPLES_PER_FRAME),
      endFrame: ceilFrame(endSample),
    });
    lastEnd = endSample;
  }
  return { sentences, narrationEndFrame: lastEnd === null ? startFrame : ceilFrame(lastEnd) };
}

// script: as returned by validateScript. blocks: the entries of
// blockStems(script), each with samples, its trimmed sample count. Returns
// timeline.json version 1 without the narration record and the audio hashes.
export function layout(script, blocks) {
  const order = playback(script);
  const scenes = [];
  let startFrame = 0;
  order.forEach(({ scene, slot, chapter, number }, i) => {
    const index = i + 1;
    const minFrames = MIN_SCENE_FRAMES[scene.kind];
    const { sentences, narrationEndFrame } = placeBlocks(
      blocks.filter((entry) => entry.scene === index),
      startFrame,
      slot === "hook" ? GAPS.leadIn : 0,
    );
    const next = order[i + 1];
    let endFrame;
    if (next === undefined) endFrame = narrationEndFrame + GAPS.tail;
    else endFrame = narrationEndFrame + (next.number === 1 ? GAPS.chapterStart : GAPS.sceneEnd);
    endFrame = Math.max(endFrame, startFrame + minFrames);
    const transitionOut = next === undefined ? null : { startFrame: endFrame - TRANSITION_FRAMES, frames: TRANSITION_FRAMES };
    scenes.push({ index, slot, chapter, scene: number, kind: scene.kind, startFrame, endFrame, narrationEndFrame, minFrames, transitionOut, sentences });
    startFrame = endFrame;
  });
  return { version: 1, fps: FPS, sampleRate: SAMPLE_RATE, totalFrames: startFrame, scenes };
}

// The narration gate: integrated loudness within -16 ± 1 LUFS and a true
// peak at or below -1.0 dBTP.
export function loudnessOk(integratedLufs, truePeakDbtp) {
  return (
    integratedLufs >= LOUDNESS.target - LOUDNESS.range &&
    integratedLufs <= LOUDNESS.target + LOUDNESS.range &&
    truePeakDbtp <= LOUDNESS.maxTruePeak
  );
}
