// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The task 3.8 fixture script, its maximum-length variant and their
// timelines, shared by the pure tests (tests/*.test.mjs) and the browser
// tests (tests/render/explainer.test.mjs). Every script here is the
// validateScript result, never the raw JSON: only validateScript fills in
// the end card's empty narration. The timeline is layout() with every block
// at 24000 samples (0.5 s at 48 kHz), plus the narration record that kit
// timeline adds; the renderer reads only its file.

import { readFileSync } from "node:fs";
import { layout } from "../../../../bin/scene-timing.mjs";
import { validateScript } from "../../../../bin/script-schema.mjs";
import { blockStems } from "../../../../bin/voice-stem.mjs";

const SCRIPT_TEXT = readFileSync(new URL("./script.json", import.meta.url), "utf8");
const BLOCK_SAMPLES = 24000;

export const NARRATION = { file: "public/narration.wav", sha256: "0".repeat(64), integratedLufs: -16, truePeakDbtp: -1.5 };

// The fixture's maximum-length hook blocks: 16 Han characters, and 16 of
// `%`, the widest ASCII character per src/kit/text.ts.
export const LONG_HAN = "光从空气进入水中速度变慢方向改变";
export const LONG_ASCII = "%".repeat(16);

function validated(raw) {
  const result = validateScript(JSON.stringify(raw));
  if (!result.ok) throw new Error(`fixture script is invalid: ${result.errors.map((e) => e.message).join("; ")}`);
  return result.script;
}

// The raw fixture JSON, freshly parsed, for a test to edit before validating.
export function rawScript() {
  return JSON.parse(SCRIPT_TEXT);
}

// The fixture script after `edit` (applied to the raw JSON), validated.
export function fixtureScript(edit = () => {}) {
  const raw = rawScript();
  edit(raw);
  return validated(raw);
}

export function maxLengthScript() {
  return fixtureScript((raw) => {
    raw.hook.narration[0][0] = LONG_HAN;
    raw.hook.narration[0][1] = LONG_ASCII;
  });
}

// timeline.json version 1 for a validated script, as kit timeline lays it
// out, without the audio hashes.
export function fixtureTimeline(script) {
  const blocks = blockStems(script).map((entry) => ({ ...entry, samples: BLOCK_SAMPLES }));
  return { ...layout(script, blocks), narration: { ...NARRATION } };
}
