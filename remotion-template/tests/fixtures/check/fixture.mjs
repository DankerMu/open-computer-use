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
