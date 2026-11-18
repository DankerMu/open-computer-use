// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// Block stems from the task 3.5 fixture: one entry per caption block in
// playback order, stem `s<scene>-<sentence>-<block>-<hash>`, where the hash is
// the first 8 hex characters of SHA-256 over JSON.stringify([text, voice id,
// voice instructions]). The literal hashes below were computed outside the
// module with `printf '%s' '<json>' | shasum -a 256`.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { validateScript } from "../bin/script-schema.mjs";
import { blockStems } from "../bin/voice-stem.mjs";

const EXAMPLE = new URL("../script.json", import.meta.url);

function minimal() {
  const chapter = (name, question, narration) => ({
    name,
    scenes: [{ kind: "question", narration, content: { question } }],
  });
  return {
    version: 1,
    title: "光的折射",
    music: "none",
    hook: { kind: "hook", narration: [["插进水里的筷子"]], content: { title: "筷子为什么弯了" } },
    chapters: [
      chapter("现象", "筷子真的弯了吗", [["为什么筷子", "在水里会弯"]]),
      chapter("原理", "光为什么会拐弯", [["光会拐弯"], ["在水面"]]),
      chapter("应用", "眼镜如何矫正", [["眼镜"]]),
    ],
    summary: { kind: "summary", narration: [["光在介质间会拐弯"]], content: { points: ["折射改变光路"] } },
    endCard: { kind: "end-card", content: { title: "谢谢观看" } },
  };
}

function stems(script) {
  const result = validateScript(JSON.stringify(script));
  assert.equal(result.ok, true, JSON.stringify(result.errors));
  return blockStems(result.script);
}

const hashOf = (stem) => stem.split("-").at(-1);
const prefixOf = (stem) => stem.split("-").slice(0, -1).join("-");

test("the hook's first block has the independently computed stem", () => {
  assert.equal(stems(minimal())[0].stem, "s1-0-0-4a7b077a");
  const voiced = { ...minimal(), voice: { id: "alloy", instructions: "慢一点" } };
  assert.equal(stems(voiced)[0].stem, "s1-0-0-b099c1be");
});

test("entries follow playback order: hook, chapter scenes, summary; the end card without narration adds none", () => {
  const entries = stems(minimal());
  assert.deepEqual(
    entries.map(({ scene, sentence, block, text }) => ({ scene, sentence, block, text })),
    [
      { scene: 1, sentence: 0, block: 0, text: "插进水里的筷子" },
      { scene: 2, sentence: 0, block: 0, text: "为什么筷子" },
      { scene: 2, sentence: 0, block: 1, text: "在水里会弯" },
      { scene: 3, sentence: 0, block: 0, text: "光会拐弯" },
      { scene: 3, sentence: 1, block: 0, text: "在水面" },
      { scene: 4, sentence: 0, block: 0, text: "眼镜" },
      { scene: 5, sentence: 0, block: 0, text: "光在介质间会拐弯" },
    ],
  );
  for (const { scene, sentence, block, stem } of entries) {
    assert.match(stem, /^s\d+-\d+-\d+-[0-9a-f]{8}$/);
    assert.equal(prefixOf(stem), `s${scene}-${sentence}-${block}`);
  }
});

test("an end card with narration is the last scene", () => {
  const script = minimal();
  script.endCard.narration = [["下次再见"]];
  const last = stems(script).at(-1);
  assert.deepEqual([last.scene, last.sentence, last.block, last.text], [6, 0, 0, "下次再见"]);
});

test("text, voice id and instructions each change the hash", () => {
  const base = hashOf(stems(minimal())[0].stem);
  const text = minimal();
  text.hook.narration[0][0] = "插进水里的勺子";
  const id = { ...minimal(), voice: { id: "alloy" } };
  const instructions = { ...minimal(), voice: { instructions: "慢一点" } };
  const hashes = [text, id, instructions].map((script) => hashOf(stems(script)[0].stem));
  for (const hash of hashes) assert.notEqual(hash, base);
  assert.equal(new Set(hashes).size, 3);
});

test("a missing voice and an empty voice give the same stems", () => {
  const empty = { ...minimal(), voice: { id: "", instructions: "" } };
  assert.deepEqual(stems(empty), stems({ ...minimal(), voice: {} }));
  assert.deepEqual(stems(empty), stems(minimal()));
});

test("moving a block changes the prefix only", () => {
  const before = stems(minimal())[0].stem;
  const script = minimal();
  script.hook.narration = [["开头"], ["前面", "插进水里的筷子"]];
  const moved = stems(script).find((entry) => entry.text === "插进水里的筷子");
  assert.equal(moved.stem, `s1-1-1-${hashOf(before)}`);
});

test("the example script's stems are unique", () => {
  const result = validateScript(readFileSync(EXAMPLE, "utf8"));
  assert.equal(result.ok, true, JSON.stringify(result.errors));
  const all = blockStems(result.script).map((entry) => entry.stem);
  assert.ok(all.length > 0);
  assert.equal(new Set(all).size, all.length);
});
