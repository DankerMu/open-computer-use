// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The task 5.1 pure fixture: the passing project of kit check as in-memory
// files. The script is 3.8's fixtureScript; the timeline is laid out as kit
// timeline lays it out, with every block 7 s long (336000 samples), and
// carries the block and narration hashes of the stub WAV bytes below, as
// kit timeline's withHashes writes them. It totals 1991 frames:
//   hook 0–719 (window 704–718), question 719–974 (959–973),
//   custom 974–1441 (1426–1440), concept card 1441–1671 (1656–1670),
//   summary 1671–1901 (1886–1900), end card 1901–1991.

import { createHash } from "node:crypto";
import { layout } from "../../../bin/scene-timing.mjs";
import { blockStems } from "../../../bin/voice-stem.mjs";
import { fixtureScript } from "../../render/fixtures/explainer/fixture.mjs";

export const BLOCK_SAMPLES = 7 * 48000;

const sha256 = (data) => createHash("sha256").update(data).digest("hex");

// Stand-ins for the imported block WAVs and the narration track: the check
// only hashes them.
export const voiceBytes = (stem) => Buffer.from(`fixture voice ${stem}`);
export const NARRATION_BYTES = Buffer.from("fixture narration");

export function checkScript(edit) {
  return fixtureScript(edit);
}

// timeline.json for a validated script, every block `samples(entry)` long.
export function checkTimeline(script, samples = () => BLOCK_SAMPLES) {
  const laid = layout(script, blockStems(script).map((entry) => ({ ...entry, samples: samples(entry) })));
  const { version, fps, sampleRate, totalFrames, scenes } = laid;
  const narration = { file: "public/narration.wav", sha256: sha256(NARRATION_BYTES), integratedLufs: -16, truePeakDbtp: -1.5 };
  return {
    version,
    fps,
    sampleRate,
    totalFrames,
    narration,
    scenes: scenes.map((scene) => ({
      ...scene,
      sentences: scene.sentences.map((sentence) => ({
        blocks: sentence.blocks.map((block) => ({ ...block, audioSha256: sha256(voiceBytes(block.stem)) })),
      })),
    })),
  };
}

// The project's files, by path relative to the project root.
export function checkFiles(script = checkScript(), timeline = checkTimeline(script)) {
  const files = new Map([
    ["script.json", Buffer.from(JSON.stringify(script))],
    ["timeline.json", Buffer.from(`${JSON.stringify(timeline, null, 2)}\n`)],
    ["public/narration.wav", NARRATION_BYTES],
    ["src/custom/Orbit.tsx", Buffer.from("export default function Orbit() { return null; }\n")],
  ]);
  for (const { stem } of blockStems(script)) files.set(`public/voice/${stem}.wav`, voiceBytes(stem));
  return files;
}

// The stub file reader of check-rules.mjs over such a map: text() for
// script.json and timeline.json, regular() for an asset, each null when
// the path is absent.
export function stubFiles(files) {
  return {
    text: async (path) => (files.has(path) ? files.get(path).toString("utf8") : null),
    regular: async (path) => files.get(path) ?? null,
  };
}

// --- task 5.4: the maximum-size script ----------------------------------------
//
// The largest valid script the kit accepts, for timing kit check and kit
// render: 5 chapters × 4 scenes, the ten body kinds in BODY_KINDS order
// twice, every scene (the end card included) narrated in 4 sentences × 4
// blocks of 16 Han characters. Each scene's content and image are a catalog
// sample's, read from candidates, so they are the content the user approved
// and the gallery probed clean: the first pass, the hook, the summary and
// the end card take their kind's `image` sample, the second pass its `max`
// sample (text-only, except image-callouts, whose max carries sample.png).
// A sample's `image: null` is an absent image, since the schema refuses
// null. No custom scene (custom is not an approved kind) and no music.
// tests/script-schema.test.mjs's maximal() is a schema-only script and
// stays as it is.

import { BODY_KINDS } from "../../../bin/script-schema.mjs";
import { candidates } from "../../../src/kit/gallery/catalog.ts";

// Every block one tone of this length: the longest whole millisecond for
// which layout() keeps totalFrames within LENGTH_WINDOW.maxFrames. 0.313 s
// (15024 samples) gives 5341 frames; 0.314 s gives 5433.
export const TONE_SECONDS = 0.313;

const MAX_TITLE = "光从空气进入水中为什么会改变方向折射现象全面解析";
const MAX_CHAPTERS = ["光的传播路径", "光的反射规律", "光的折射现象", "透镜成像原理", "光学生活应用"];
const MAX_BLOCK = "光从空气进入水中速度变慢方向改变";
const MAX_NARRATION = 4;

// A scene of `kind` with the content and image of its catalog sample `id`.
function sampleScene(kind, id) {
  const { content, image } = candidates.find((entry) => entry.kind === kind).samples.find((sample) => sample.id === id);
  const narration = Array.from({ length: MAX_NARRATION }, () => Array.from({ length: MAX_NARRATION }, () => MAX_BLOCK));
  return { kind, narration, ...(image === null ? {} : { image }), content: structuredClone(content) };
}

// The raw script.json object.
export function maxScript() {
  const body = [...BODY_KINDS.map((kind) => sampleScene(kind, "image")), ...BODY_KINDS.map((kind) => sampleScene(kind, "max"))];
  return {
    version: 1,
    title: MAX_TITLE,
    music: "none",
    hook: sampleScene("hook", "image"),
    chapters: MAX_CHAPTERS.map((name, c) => ({ name, scenes: body.slice(c * 4, c * 4 + 4) })),
    summary: sampleScene("summary", "image"),
    endCard: sampleScene("end-card", "image"),
  };
}
