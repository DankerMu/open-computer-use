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

// Each kind's declared minimum length in frames. For hook, question, concept
// card, definition/quote, summary and end card (task 4.2) and for process
// steps, cause and effect, comparison and timeline (task 4.3) the values are
// their components' own: the 15-frame entrance, 30 frames of rest and the
// 15-frame exit make 60, and the end card's 90 is the tail. The others stay
// conservative floors until task 4.4 sets them from their components. The
// end card's stays at most the 90-frame tail.
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

// Each kind's entrance in frames. The six task 4.2 components and the four
// task 4.3 components settle within the kit-wide 15, so theirs are their
// components' own; the other kinds keep it until task 4.4 sets theirs from
// its approved components. A custom
// scene must settle within it. kit check's probe targets, its rest rule and
// the scene components all read it.
export const ENTRANCE_FRAMES = {
  hook: 15,
  question: 15,
  "concept-card": 15,
  "process-steps": 15,
  "cause-effect": 15,
  comparison: 15,
  timeline: 15,
  "key-number": 15,
  chart: 15,
  "image-callouts": 15,
  "definition-quote": 15,
  custom: 15,
  summary: 15,
  "end-card": 15,
};

// Each kind's declared reveals: (timing) => [{type: "key" | "batch",
// startFrame, endFrame}], frames relative to the scene. Empty until tasks
// 4.2–4.4 declare them from the approved components.
export const REVEALS = {};

// The rest a reveal needs before the next reveal or the scene's exit.
export const REST_FRAMES = { key: 30, batch: 15 };

// The reveals of a scene of `kind`: the declared list, or one key reveal
// over its entrance.
export function revealsOf(kind, timing, reveals = REVEALS) {
  if (Object.hasOwn(reveals, kind)) return reveals[kind](timing);
  return [{ type: "key", startFrame: 0, endFrame: ENTRANCE_FRAMES[kind] }];
}

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
