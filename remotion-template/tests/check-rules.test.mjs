// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The mechanical stage of kit check (task 5.1), pure: each rule of
// bin/check-rules.mjs called on its own with the script, a timeline, a
// reveals table and a stub file reader, and runChecks' stage order with a
// stub probe. Expected messages are the fixture's exact texts; the frames in
// them come from tests/fixtures/check/fixture.mjs's timeline (worked out from
// the 3.6 layout rules and listed in its header) or from the hand-written
// timelines below, never from the module under test.

import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import {
  RULES,
  assetsRule,
  probeRule,
  restRule,
  runChecks,
  schemaRule,
  timelineRule,
  unnarratedRule,
} from "../bin/check-rules.mjs";
import { customIndexSource } from "../bin/custom-index.mjs";
import { explainerScenes } from "../bin/explainer-scenes.mjs";
import { blockStems } from "../bin/voice-stem.mjs";
import { KIT_THEME, themes } from "../src/kit/theme.ts";
import { checkFiles, checkScript, checkTimeline, stubFiles, voiceBytes } from "./fixtures/check/fixture.mjs";
import { fixtureTimeline } from "./render/fixtures/explainer/fixture.mjs";

const HINT = "; run node bin/kit.mjs timeline";
const QUESTION = "chapter 1 现象, scene 1 (question)";
const CUSTOM = "chapter 2 原理, scene 1 (custom)";
const CONCEPT = "chapter 3 应用, scene 1 (concept-card)";
const WHERES = ["hook", QUESTION, CUSTOM, CONCEPT, "summary", "end-card"];
const STALE = "timeline.json is stale (its text, voice or instructions changed); run node bin/kit.mjs voice, voice-import, then timeline";

const script = checkScript();
const timeline = checkTimeline(script);
const text = (value) => JSON.stringify(value);
const stems = blockStems(script).map((entry) => entry.stem);

// A temporary project root with src/custom/Orbit.tsx, for writeCustomIndex.
function customRoot(t, { orbit = true } = {}) {
  const root = mkdtempSync(join(tmpdir(), "check-rules-test-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  mkdirSync(join(root, "src", "custom"), { recursive: true });
  if (orbit) writeFileSync(join(root, "src", "custom", "Orbit.tsx"), "export default function Orbit() { return null; }\n");
  return root;
}

// A probe result with nothing measured: three clean frames per target.
function cleanResult(targets, edit = () => {}) {
  const result = {
    version: 1,
    canvas: { width: 1920, height: 1080 },
    frames: targets.flatMap((target) =>
      ["entrance-end", "midpoint", "pre-exit"].map((role, k) => ({
        target: target.id,
        role,
        frame: k,
        fontPresent: true,
        texts: [],
        hidden: [],
        marked: [],
        scrolls: [],
        masks: [],
      })),
    ),
  };
  edit(result);
  return result;
}

// A stub probe that records its argument and returns `make(targets)`.
function stubProbe(make = (targets) => cleanResult(targets)) {
  const calls = [];
  const probe = async (options) => {
    calls.push(options);
    return make(options.targets);
  };
  return { probe, calls };
}

const neverProbe = async () => {
  throw new Error("the probe must not run");
};

// --- rule 1: schema ---------------------------------------------------------------

test("schema: a valid script passes and comes back validated", () => {
  assert.deepEqual(schemaRule(text(script)), { reasons: [], script });
});

test("schema: a missing script.json is not found", () => {
  assert.deepEqual(schemaRule(null), { reasons: ["script.json: not found"], script: null });
});

test("schema: every validateScript problem is one unindented reason", () => {
  const raw = JSON.parse(text(script));
  raw.title = "光".repeat(25);
  raw.hook.narration[0][0] = "光".repeat(17);
  assert.deepEqual(schemaRule(text(raw)).reasons, [
    "script: title is too long (limit 24 characters)",
    "hook: narration[0][0] is too long (limit 16 characters)",
  ]);
});

// --- rule 2: timeline ---------------------------------------------------------------

const timelineReasons = (edit, s = script) => {
  const file = structuredClone(timeline);
  edit(file);
  return timelineRule(s, text(file)).reasons;
};

test("timeline: the kit-timeline timeline passes, hashes and all", () => {
  assert.deepEqual(timelineRule(script, text(timeline)), { reasons: [], timeline });
});

test("timeline: a missing or unparsable timeline.json", () => {
  const reason = `timeline.json: missing or not JSON${HINT}`;
  for (const file of [null, "", "{", "[1,"]) assert.deepEqual(timelineRule(script, file), { reasons: [reason], timeline: null });
});

test("timeline: an explainerScenes problem is its own message, and stops the later checks", () => {
  const reasons = timelineReasons((t) => {
    t.version = 2;
    t.scenes[0].sentences[0].blocks[1].stem = "s1-0-1-00000000";
  });
  assert.deepEqual(reasons, [`script: timeline.json version is 2, expected 1${HINT}`]);
});

test("timeline: a length outside 60–180 s is lengthError's text", () => {
  assert.deepEqual(timelineRule(script, text(fixtureTimeline(script))).reasons, ["total 16.0 s (481 frames) is outside 60–180 s"]);
});

test("timeline: a block's stem or sample of the wrong type, each, and nothing after them", () => {
  const reasons = timelineReasons((t) => {
    delete t.scenes[0].sentences[0].blocks[1].stem;
    t.scenes[3].sentences[0].blocks[0].startSample = "2305600";
    t.scenes[4].sentences[0].blocks[0].endSample = 3009600.5;
    t.scenes[2].sentences[0].blocks[0].stem = "s3-0-0-00000000";
  });
  assert.deepEqual(reasons, [
    `script: timeline.json scenes[0].sentences[0].blocks[1].stem is missing or has the wrong type${HINT}`,
    `script: timeline.json scenes[3].sentences[0].blocks[0].startSample is missing or has the wrong type${HINT}`,
    `script: timeline.json scenes[4].sentences[0].blocks[0].endSample is missing or has the wrong type${HINT}`,
  ]);
});

test("timeline: stems that differ in one block give that block, stale", () => {
  const reasons = timelineReasons((t) => (t.scenes[0].sentences[0].blocks[1].stem = "s1-0-1-00000000"));
  assert.deepEqual(reasons, [`hook, sentence 1 block 2: ${STALE}`]);
});

test("timeline: every stale block is reported, after a voice change", () => {
  const revoiced = checkScript((raw) => (raw.voice = { id: "alloy" }));
  const reasons = timelineRule(revoiced, text(timeline)).reasons;
  assert.equal(reasons.length, 8);
  assert.equal(reasons[0], `hook, sentence 1 block 1: ${STALE}`);
  assert.equal(reasons[4], `${CUSTOM}, sentence 1 block 1: ${STALE}`);
  assert.equal(reasons[5], `${CUSTOM}, sentence 1 block 2: ${STALE}`);
  assert.equal(reasons[7], `summary, sentence 1 block 1: ${STALE}`);
});

test("timeline: a total that the samples do not give is the totals message", () => {
  // The summary block 30 frames longer: the summary ends at 1931, not 1901.
  const reasons = timelineReasons((t) => (t.scenes[4].sentences[0].blocks[0].endSample += 30 * 1600));
  assert.deepEqual(reasons, [`timeline.json totals 1991 frames, the script and narration need 2021${HINT}`]);
});

test("timeline: a scene whose samples give another length is the lasts message", () => {
  // The hook's last block 5 frames longer and the custom scene's last 5
  // frames shorter: the total holds, the hook needs 724 frames, not 719.
  const reasons = timelineReasons((t) => {
    t.scenes[0].sentences[0].blocks[2].endSample += 5 * 1600;
    t.scenes[2].sentences[0].blocks[1].endSample -= 5 * 1600;
  });
  assert.deepEqual(reasons, [`hook: timeline.json lasts 719 frames, the script and narration need 724${HINT}`]);
});

test("timeline: any other difference names its field path", () => {
  const shifted = timelineReasons((t) => {
    t.scenes[0].sentences[0].blocks[0].startSample += 1;
    t.scenes[0].sentences[0].blocks[0].endSample += 1;
  });
  assert.deepEqual(shifted, [`hook: timeline.json disagrees with the script and narration at scenes[0].sentences[0].blocks[0].startSample${HINT}`]);
  const extra = timelineReasons((t) => (t.scenes[1].extra = 1));
  assert.deepEqual(extra, [`${QUESTION}: timeline.json disagrees with the script and narration at scenes[1].extra${HINT}`]);
  const minFrames = timelineReasons((t) => (t.scenes[3].minFrames = 61));
  assert.deepEqual(minFrames, [`${CONCEPT}: timeline.json disagrees with the script and narration at scenes[3].minFrames${HINT}`]);
});

test("timeline: the audio hashes are not compared here", () => {
  assert.deepEqual(timelineReasons((t) => (t.scenes[1].sentences[0].blocks[0].audioSha256 = "0".repeat(64))), []);
});

// --- rule 3: assets ---------------------------------------------------------------

const assetReasons = (edit, s = script, t = timeline) => {
  const files = checkFiles(s, t);
  edit(files);
  return assetsRule(s, t, stubFiles(files));
};

test("assets: the passing project's files pass", async () => {
  assert.deepEqual(await assetReasons(() => {}), []);
});

test("assets: a scene image must be in public/images", async () => {
  const pictured = checkScript((raw) => (raw.chapters[2].scenes[0].image = "lens.png"));
  const t = checkTimeline(pictured);
  assert.deepEqual(await assetReasons(() => {}, pictured, t), [
    `${CONCEPT}: image "lens.png" is missing from public/images; run node bin/kit.mjs image "<path>"`,
  ]);
  assert.deepEqual(await assetReasons((files) => files.set("public/images/lens.png", Buffer.from("png")), pictured, t), []);
});

test("assets: a missing or changed block WAV, named by its block", async () => {
  assert.deepEqual(await assetReasons((files) => files.delete(`public/voice/${stems[1]}.wav`)), [
    `hook, sentence 1 block 2: ${stems[1]}.wav is missing`,
  ]);
  assert.deepEqual(await assetReasons((files) => files.set(`public/voice/${stems[5]}.wav`, Buffer.from("other"))), [
    `${CUSTOM}, sentence 1 block 2: ${stems[5]}.wav changed since kit timeline${HINT}`,
  ]);
});

test("assets: a missing or changed narration.wav", async () => {
  assert.deepEqual(await assetReasons((files) => files.delete("public/narration.wav")), ["narration.wav is missing"]);
  assert.deepEqual(await assetReasons((files) => files.set("public/narration.wav", Buffer.from("louder"))), [
    `narration.wav changed since kit timeline${HINT}`,
  ]);
});

test("assets: the custom scene's file must be in src/custom", async () => {
  assert.deepEqual(await assetReasons((files) => files.delete("src/custom/Orbit.tsx")), [`${CUSTOM}: src/custom/Orbit.tsx is missing`]);
});

test("assets: every problem is reported, images, blocks, narration, then the custom file", async () => {
  const pictured = checkScript((raw) => (raw.hook.image = "light.webp"));
  const t = checkTimeline(pictured);
  const pictureStems = blockStems(pictured).map((entry) => entry.stem);
  const reasons = await assetReasons(
    (files) => {
      files.delete("src/custom/Orbit.tsx");
      files.delete("public/narration.wav");
      files.set(`public/voice/${pictureStems[7]}.wav`, Buffer.from("other"));
      files.delete(`public/voice/${pictureStems[0]}.wav`);
    },
    pictured,
    t,
  );
  assert.deepEqual(reasons, [
    `hook: image "light.webp" is missing from public/images; run node bin/kit.mjs image "<path>"`,
    `hook, sentence 1 block 1: ${pictureStems[0]}.wav is missing`,
    `summary, sentence 1 block 1: ${pictureStems[7]}.wav changed since kit timeline${HINT}`,
    "narration.wav is missing",
    `${CUSTOM}: src/custom/Orbit.tsx is missing`,
  ]);
});

// --- rule 4: unnarrated stretches -----------------------------------------------------

// A hand-written timeline over the fixture's six scenes: hook 0–300,
// question 300–600, custom 600–900, concept card 900–1200, summary
// 1200–1500, end card 1500–1800, each with the given blocks.
function handTimeline(blocks) {
  return {
    scenes: [0, 300, 600, 900, 1200, 1500].map((startFrame, i) => ({
      startFrame,
      endFrame: startFrame + 300,
      sentences: blocks[i].length ? [{ blocks: blocks[i].map(([s, e]) => ({ startFrame: s, endFrame: e })) }] : [],
    })),
  };
}

const unnarrated = (blocks) => unnarratedRule(script, handTimeline(blocks));
const TILED = [[[40, 100]], [[190, 600]], [[600, 900]], [[900, 1200]], [[1200, 1450]], []];

test("unnarrated: the passing project's timeline passes", () => {
  assert.deepEqual(unnarratedRule(script, timeline), []);
});

test("unnarrated: 90 frames pass, 91 fail", () => {
  assert.deepEqual(unnarrated(TILED), []);
  const longer = structuredClone(TILED);
  longer[1] = [[191, 600]];
  assert.deepEqual(unnarrated(longer), ["hook: 91 frames (3.0 s) without narration, frames 100–191; at most 90 (3 s)"]);
});

test("unnarrated: the lead-in before the first block does not count", () => {
  const late = structuredClone(TILED);
  late[0] = [[200, 260]];
  late[1] = [[300, 600]];
  assert.deepEqual(unnarrated(late), []);
});

test("unnarrated: a stretch is cut at the end card's start", () => {
  const card = structuredClone(TILED);
  card[5] = [[1600, 1700]];
  assert.deepEqual(unnarrated(card), [], "1450–1600 is cut to 1450–1500");
  card[4] = [[1200, 1400]];
  assert.deepEqual(unnarrated(card), ["summary: 100 frames (3.3 s) without narration, frames 1400–1500; at most 90 (3 s)"]);
});

test("unnarrated: a stretch across a boundary is named by the scene of its first frame, and each is reported", () => {
  const gaps = structuredClone(TILED);
  gaps[1] = [[190, 500]];
  gaps[2] = [[600, 850]];
  gaps[3] = [[950, 1200]];
  assert.deepEqual(unnarrated(gaps), [
    `${QUESTION}: 100 frames (3.3 s) without narration, frames 500–600; at most 90 (3 s)`,
    `${CUSTOM}: 100 frames (3.3 s) without narration, frames 850–950; at most 90 (3 s)`,
  ]);
});

// --- rule 5: rest after reveals ------------------------------------------------------

// The question scene lasts 255 frames with its exit at 240 (959 − 719); the
// end card lasts 90 frames and has no exit.
const rest = (reveals) => restRule(script, timeline, reveals);

test("rest: the default entrance reveal passes on the passing project", () => {
  assert.deepEqual(restRule(script, timeline), []);
});

test("rest: a key reveal needs 30 frames before the exit, measured from the exit, not the scene's end", () => {
  assert.deepEqual(rest({ question: () => [{ type: "key", startFrame: 0, endFrame: 210 }] }), []);
  assert.deepEqual(rest({ question: () => [{ type: "key", startFrame: 0, endFrame: 211 }] }), [
    `${QUESTION}: key reveal ending at frame 211 rests 29 frames before the exit at frame 240; needs 30`,
  ]);
});

test("rest: the last scene's reveals rest before its end", () => {
  assert.deepEqual(rest({ "end-card": () => [{ type: "key", startFrame: 0, endFrame: 60 }] }), []);
  assert.deepEqual(rest({ "end-card": () => [{ type: "key", startFrame: 0, endFrame: 61 }] }), [
    "end-card: key reveal ending at frame 61 rests 29 frames before the exit at frame 90; needs 30",
  ]);
});

test("rest: a batch reveal needs 15 frames before the next reveal, in start order", () => {
  const reveals = (firstEnd) => ({
    question: (timing) => [
      { type: "key", startFrame: 100, endFrame: 210 },
      { type: "batch", startFrame: 25, endFrame: 40 },
      { type: "batch", startFrame: 0, endFrame: firstEnd },
    ],
  });
  assert.deepEqual(rest(reveals(10)), []);
  assert.deepEqual(rest(reveals(11)), [
    `${QUESTION}: batch reveal ending at frame 11 rests 14 frames before the next reveal at frame 25; needs 15`,
  ]);
});

test("rest: a key reveal needs 30 frames before the next reveal, and the reveal function gets the scene's timing", () => {
  const seen = [];
  const reasons = rest({
    custom: (timing) => {
      seen.push(timing);
      return [
        { type: "key", startFrame: 0, endFrame: 20 },
        { type: "key", startFrame: 49, endFrame: 60 },
      ];
    },
  });
  assert.deepEqual(reasons, [`${CUSTOM}: key reveal ending at frame 20 rests 29 frames before the next reveal at frame 49; needs 30`]);
  assert.deepEqual(seen, [explainerScenes(script, timeline)[2].props.timing]);
});

// --- rule 6: probe ---------------------------------------------------------------

test("probe: one target per scene, with its where, kind, KIT_THEME, entrance and exit", async (t) => {
  const root = customRoot(t);
  const { probe, calls } = stubProbe();
  assert.deepEqual(await probeRule(root, script, timeline, { probe, theme: KIT_THEME }), []);
  assert.equal(KIT_THEME, themes.light);
  assert.equal(calls.length, 1);
  const { entryPoint, publicDir, targets } = calls[0];
  assert.deepEqual([entryPoint, publicDir], ["src/kit/index.ts", "public"]);
  const entries = explainerScenes(script, timeline);
  const expected = timeline.scenes.map((scene, i) => ({
    id: WHERES[i],
    scene: scene.kind,
    props: { ...entries[i].props, theme: themes.light },
    durationInFrames: scene.endFrame - scene.startFrame,
    entranceFrames: 15,
    exitFrame: scene.transitionOut === null ? scene.endFrame - scene.startFrame : scene.transitionOut.startFrame - scene.startFrame,
  }));
  assert.deepEqual(targets, expected);
  assert.deepEqual(
    targets.map((target) => target.exitFrame),
    [704, 240, 452, 215, 215, 90],
  );
  assert.equal(readFileSync(join(root, "src", "custom", "index.ts"), "utf8"), customIndexSource(script));
});

test("probe: each violation is its formatViolation line, judged with the given theme", async (t) => {
  const record = { path: "div:nth-child(1)", text: "光", glyphBox: { x: 50, y: 100, width: 48, height: 67.2 }, minFontSizePx: 48, lines: ["光"], clippedBy: [], scale: 1 };
  const { probe } = stubProbe((targets) => cleanResult(targets, (result) => result.frames[3].texts.push(record)));
  assert.deepEqual(await probeRule(customRoot(t), script, timeline, { probe, theme: KIT_THEME }), [
    `${QUESTION} entrance-end frame 0: outside safe area: div:nth-child(1) left 46 px`,
  ]);
});

test("probe: a frame without the font gives one font reason", async (t) => {
  const { probe } = stubProbe((targets) =>
    cleanResult(targets, (result) => {
      result.frames[0].fontPresent = false;
      result.frames[7].fontPresent = false;
    }),
  );
  assert.deepEqual(await probeRule(customRoot(t), script, timeline, { probe, theme: KIT_THEME }), ['font "Noto Sans CJK SC" is not installed']);
});

test('probe: an "unknown scene" error names the first scene of that kind', async (t) => {
  const failing = (message) => async () => {
    throw new Error(message);
  };
  const root = customRoot(t);
  assert.deepEqual(
    await probeRule(root, script, timeline, {
      probe: failing('kit probe: target "hook" frame 15: kit probe: unknown scene "hook"'),
      theme: KIT_THEME,
    }),
    ['hook: no component for kind "hook"'],
  );
  assert.deepEqual(
    await probeRule(root, script, timeline, { probe: failing('Error: kit probe: unknown scene "question" at x'), theme: KIT_THEME }),
    [`${QUESTION}: no component for kind "question"`],
  );
  // A kind the script does not hold is not mapped.
  assert.deepEqual(
    await probeRule(root, script, timeline, { probe: failing('kit probe: unknown scene "chart"'), theme: KIT_THEME }),
    ['probe: kit probe: unknown scene "chart"'],
  );
});

test("probe: any other probe error is probe: <message>", async (t) => {
  const probe = async () => {
    throw new Error("kit probe: Failed to launch the browser process");
  };
  assert.deepEqual(await probeRule(customRoot(t), script, timeline, { probe, theme: KIT_THEME }), [
    "probe: kit probe: Failed to launch the browser process",
  ]);
});

test("probe: a custom index refusal is the reason, and nothing is probed", async (t) => {
  const root = customRoot(t, { orbit: false });
  assert.deepEqual(await probeRule(root, script, timeline, { probe: neverProbe, theme: KIT_THEME }), [
    `custom index: ${CUSTOM}: src/custom/Orbit.tsx is missing`,
  ]);
  const bare = mkdtempSync(join(tmpdir(), "check-rules-test-"));
  t.after(() => rmSync(bare, { recursive: true, force: true }));
  assert.deepEqual(await probeRule(bare, script, timeline, { probe: neverProbe, theme: KIT_THEME }), [
    `custom index: ${join(bare, "src", "custom")}: is missing`,
  ]);
});

// --- runChecks ---------------------------------------------------------------

const STAGES = ["schema", "timeline", "assets", "unnarrated", "rest", "probe"];
const stageLines = (n) => STAGES.slice(0, n).map((name, i) => `check: stage ${i + 1} ${name}`);

test("the rule list is the six stages in order", () => {
  assert.deepEqual(
    RULES.map((rule) => rule.name),
    STAGES,
  );
});

test("runChecks: the passing project runs all six stages and passes", async (t) => {
  const log = [];
  const { probe, calls } = stubProbe();
  const reasons = await runChecks(customRoot(t), { probe, files: stubFiles(checkFiles()), log: (line) => log.push(line) });
  assert.deepEqual(reasons, []);
  assert.deepEqual(log, stageLines(6));
  assert.equal(calls.length, 1);
  assert.equal(calls[0].targets[0].props.theme, KIT_THEME);
});

test("runChecks: a schema failure runs no later stage", async (t) => {
  const log = [];
  const files = {
    text: async (path) => {
      if (path === "script.json") return null;
      throw new Error(`${path} must not be read`);
    },
    regular: async (path) => {
      throw new Error(`${path} must not be read`);
    },
  };
  const reasons = await runChecks(customRoot(t), { probe: neverProbe, files, log: (line) => log.push(line) });
  assert.deepEqual(reasons, ["script.json: not found"]);
  assert.deepEqual(log, stageLines(1));
});

test("runChecks: a timeline failure runs no later stage", async (t) => {
  const log = [];
  const map = checkFiles();
  map.delete("timeline.json");
  const files = {
    ...stubFiles(map),
    regular: async (path) => {
      throw new Error(`${path} must not be read`);
    },
  };
  const reasons = await runChecks(customRoot(t), { probe: neverProbe, files, log: (line) => log.push(line) });
  assert.deepEqual(reasons, [`timeline.json: missing or not JSON${HINT}`]);
  assert.deepEqual(log, stageLines(2));
});

test("runChecks: stages 3–5 all run, and a failure in them skips the probe", async (t) => {
  const log = [];
  const files = checkFiles();
  files.delete(`public/voice/${stems[3]}.wav`);
  const reveals = { summary: () => [{ type: "key", startFrame: 0, endFrame: 200 }] };
  const reasons = await runChecks(customRoot(t), { probe: neverProbe, files: stubFiles(files), reveals, log: (line) => log.push(line) });
  assert.deepEqual(reasons, [
    `${QUESTION}, sentence 1 block 1: ${stems[3]}.wav is missing`,
    "summary: key reveal ending at frame 200 rests 15 frames before the exit at frame 215; needs 30",
  ]);
  assert.deepEqual(log, stageLines(5));
});

test("runChecks: a system error from a read propagates", async (t) => {
  const denied = Object.assign(new Error("EACCES: permission denied, open 'public/narration.wav'"), { code: "EACCES" });
  const files = {
    ...stubFiles(checkFiles()),
    regular: async () => {
      throw denied;
    },
  };
  await assert.rejects(runChecks(customRoot(t), { probe: neverProbe, files }), (error) => error === denied);
});

// The passing project written to a temporary directory, read with the
// default file reader.
function onDisk(t) {
  const root = mkdtempSync(join(tmpdir(), "check-rules-disk-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  for (const [path, bytes] of checkFiles()) {
    mkdirSync(dirname(join(root, path)), { recursive: true });
    writeFileSync(join(root, path), bytes);
  }
  return root;
}

test("runChecks: the default reader passes the project on disk and counts a symlinked WAV missing", async (t) => {
  const root = onDisk(t);
  const { probe } = stubProbe();
  assert.deepEqual(await runChecks(root, { probe }), []);
  const wav = join(root, "public", "voice", `${stems[0]}.wav`);
  rmSync(wav);
  writeFileSync(join(root, "elsewhere.wav"), voiceBytes(stems[0]));
  symlinkSync(join(root, "elsewhere.wav"), wav);
  assert.deepEqual(await runChecks(root, { probe: neverProbe }), [`hook, sentence 1 block 1: ${stems[0]}.wav is missing`]);
});
