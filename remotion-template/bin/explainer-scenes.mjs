// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The scenes the explainer renders: one entry per script scene in playback
// order, with its timeline frames and the props its component receives.
// explainerScenes is the one agreement check of script.json and
// timeline.json; the renderer and kit check both call it. Pure, and imports
// no node: built-in, because the Remotion bundle imports it.
//
// Every scene but the last carries a transition window, its transitionOut
// (task 4.5): it is TRANSITION_FRAMES long, ends at the scene's end and
// starts no earlier than the scene's last caption block ends. The renderer
// reads the window from here, as each entry's exit, and never recomputes it.

import { escapeLine, fieldPath, playbackWhere } from "./script-schema.mjs";
import { TRANSITION_FRAMES } from "./scene-timing.mjs";

const VERSION = 1;
const FPS = 30;
const NARRATION_FILE = "public/narration.wav";

const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const isString = (value) => typeof value === "string";
const isWindow = (value) => value === null || isObject(value);
const shown = (value) => escapeLine(JSON.stringify(value));

class Problem extends Error {
  constructor(where, problem) {
    super(`${where}: timeline.json ${problem}; run node bin/kit.mjs timeline`);
  }
}

// The script's scenes in playback order, each with its playback index and
// where.
function playback(script) {
  const scenes = [script.hook, ...script.chapters.flatMap((chapter) => chapter.scenes), script.summary, script.endCard];
  return scenes.map((scene, i) => ({ scene, index: i + 1, where: playbackWhere(script, i + 1) }));
}

// Every field the renderer reads exists with its type; frames are integers.
function checkShape(timeline) {
  const need = (value, ok, path) => {
    if (!ok(value)) throw new Problem("script", `${fieldPath(path)} is missing or has the wrong type`);
    return value;
  };
  const field = (parent, key, ok, path) => need(isObject(parent) ? parent[key] : undefined, ok, [...path, key]);
  field(timeline, "version", Number.isInteger, []);
  field(timeline, "fps", Number.isInteger, []);
  field(timeline, "totalFrames", Number.isInteger, []);
  const narration = field(timeline, "narration", isObject, []);
  field(narration, "file", isString, ["narration"]);
  const scenes = field(timeline, "scenes", Array.isArray, []);
  scenes.forEach((scene, i) => {
    const at = ["scenes", i];
    need(scene, isObject, at);
    field(scene, "kind", isString, at);
    for (const key of ["startFrame", "endFrame", "narrationEndFrame"]) field(scene, key, Number.isInteger, at);
    const transition = field(scene, "transitionOut", isWindow, at);
    if (transition !== null) {
      for (const key of ["startFrame", "frames"]) field(transition, key, Number.isInteger, [...at, "transitionOut"]);
    }
    field(scene, "sentences", Array.isArray, at).forEach((sentence, s) => {
      const sentenceAt = [...at, "sentences", s];
      need(sentence, isObject, sentenceAt);
      field(sentence, "blocks", Array.isArray, sentenceAt).forEach((block, b) => {
        const blockAt = [...sentenceAt, "blocks", b];
        need(block, isObject, blockAt);
        field(block, "text", isString, blockAt);
        field(block, "startFrame", Number.isInteger, blockAt);
        field(block, "endFrame", Number.isInteger, blockAt);
      });
    });
  });
}

// One pass over the [timeline scene, script entry] pairs.
function eachScene(pairs, check) {
  for (const [scene, entry] of pairs) check(scene, entry);
}

// One pass over every block, with its 0-based sentence and block indexes and
// its where (1-based numbers).
function eachBlock(pairs, check) {
  for (const [scene, entry] of pairs) {
    scene.sentences.forEach((sentence, s) =>
      sentence.blocks.forEach((block, b) => check(block, scene, entry, s, b, `${entry.where}, sentence ${s + 1} block ${b + 1}`)),
    );
  }
}

// Checks in the fixture's order; each pass runs over the whole timeline
// before the next starts.
function checkAgreement(script, timeline) {
  checkShape(timeline);
  if (timeline.version !== VERSION) throw new Problem("script", `version is ${shown(timeline.version)}, expected ${VERSION}`);
  if (timeline.fps !== FPS) throw new Problem("script", `fps is ${shown(timeline.fps)}, expected ${FPS}`);
  if (timeline.narration.file !== NARRATION_FILE) {
    throw new Problem("script", `narration.file is ${shown(timeline.narration.file)}, expected ${JSON.stringify(NARRATION_FILE)}`);
  }
  const entries = playback(script);
  if (timeline.scenes.length !== entries.length) {
    throw new Problem("script", `has ${timeline.scenes.length} scenes, script.json has ${entries.length}`);
  }
  const pairs = timeline.scenes.map((scene, i) => [scene, entries[i]]);
  eachScene(pairs, (scene, { scene: source, where }) => {
    if (scene.kind !== source.kind) throw new Problem(where, `kind is ${shown(scene.kind)}, script.json has "${source.kind}"`);
  });
  eachScene(pairs, (scene, { scene: source, where }) => {
    const [n, m] = [scene.sentences.length, source.narration.length];
    if (n !== m) throw new Problem(where, `has ${n} sentences, script.json has ${m}`);
  });
  eachScene(pairs, (scene, { scene: source, where }) => {
    scene.sentences.forEach((sentence, s) => {
      const [n, m] = [sentence.blocks.length, source.narration[s].length];
      if (n !== m) throw new Problem(where, `has ${n} blocks in sentence ${s + 1}, script.json has ${m}`);
    });
  });
  eachBlock(pairs, (block, scene, { scene: source }, s, b, where) => {
    if (block.text !== source.narration[s][b]) throw new Problem(where, "text differs from script.json");
  });
  let previousEnd = 0;
  eachScene(pairs, (scene, { where }) => {
    if (scene.startFrame !== previousEnd) {
      throw new Problem(where, `startFrame ${scene.startFrame} is not the previous scene's end ${previousEnd}`);
    }
    previousEnd = scene.endFrame;
  });
  const above = (item, where) => {
    if (item.endFrame <= item.startFrame) throw new Problem(where, `endFrame ${item.endFrame} is not above startFrame ${item.startFrame}`);
  };
  eachScene(pairs, (scene, { where }) => above(scene, where));
  eachBlock(pairs, (block, _scene, _entry, _s, _b, where) => above(block, where));
  eachBlock(pairs, (block, scene, _entry, _s, _b, where) => {
    if (block.startFrame < scene.startFrame || block.endFrame > scene.endFrame) {
      throw new Problem(
        where,
        `block ${block.startFrame}–${block.endFrame} lies outside the scene ${scene.startFrame}–${scene.endFrame}`,
      );
    }
  });
  const lastEnd = timeline.scenes.at(-1).endFrame;
  if (timeline.totalFrames !== lastEnd) throw new Problem("script", `totalFrames ${timeline.totalFrames} is not the last scene's end ${lastEnd}`);
  checkTransitions(pairs);
  return pairs;
}

// The end of a scene's last caption block, or its startFrame when it has
// none.
function lastBlockEnd(scene) {
  const last = scene.sentences.at(-1)?.blocks.at(-1);
  return last === undefined ? scene.startFrame : last.endFrame;
}

// The transition windows, one pass per check; checkShape has made each
// transitionOut null or {startFrame, frames} of integers.
function checkTransitions(pairs) {
  const last = pairs.length - 1;
  pairs.forEach(([scene, { where }], i) => {
    if (i === last && scene.transitionOut !== null) throw new Problem(where, "the last scene has a transition");
  });
  pairs.forEach(([scene, { where }], i) => {
    if (i !== last && scene.transitionOut === null) throw new Problem(where, "scene has no transition");
  });
  const windows = pairs.filter(([scene]) => scene.transitionOut !== null);
  for (const [{ transitionOut }, { where }] of windows) {
    if (transitionOut.frames !== TRANSITION_FRAMES) {
      throw new Problem(where, `transition is ${transitionOut.frames} frames, expected ${TRANSITION_FRAMES}`);
    }
  }
  for (const [{ transitionOut, endFrame }, { where }] of windows) {
    const end = transitionOut.startFrame + transitionOut.frames;
    if (end !== endFrame) throw new Problem(where, `transition ends at ${end}, not at the scene's end ${endFrame}`);
  }
  for (const [scene, { where }] of windows) {
    const blocksEnd = lastBlockEnd(scene);
    if (scene.transitionOut.startFrame < blocksEnd) {
      throw new Problem(where, `transition starts at ${scene.transitionOut.startFrame}, before the last block ends at ${blocksEnd}`);
    }
  }
}

// script: as validateScript returns it. timeline: parsed from timeline.json.
// Returns {index, where, kind, component, startFrame, durationInFrames,
// exit, props} per scene. exit is the scene's transition window,
// {startFrame, frames} with startFrame relative to the scene's start, or
// null for the last scene. props holds the scene's content (absent for a
// custom scene), its image name or null, and its timing with every frame
// relative to the scene's start. Throws an Error naming where the two
// disagree.
export function explainerScenes(script, timeline) {
  return checkAgreement(script, timeline).map(([scene, { scene: source, index, where }]) => {
    const start = scene.startFrame;
    const durationInFrames = scene.endFrame - start;
    const timing = {
      durationInFrames,
      narrationEndFrame: scene.narrationEndFrame - start,
      sentences: scene.sentences.map((sentence) => ({
        blocks: sentence.blocks.map(({ text, startFrame, endFrame }) => ({ text, startFrame: startFrame - start, endFrame: endFrame - start })),
      })),
    };
    const exit = scene.transitionOut === null ? null : { startFrame: scene.transitionOut.startFrame - start, frames: scene.transitionOut.frames };
    const custom = source.kind === "custom";
    const props = custom ? { image: source.image ?? null, timing } : { content: source.content, image: source.image ?? null, timing };
    return { index, where, kind: source.kind, component: custom ? source.component : null, startFrame: start, durationInFrames, exit, props };
  });
}
