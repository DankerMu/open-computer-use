// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The mechanical stage of kit check (task 5.1), pure: each rule of
// bin/check-rules.mjs called on its own with the script, a timeline, a
// reveals table and a stub file reader, and runChecks' stage order with a
// stub probe and a stub contact sheet (task 5.2), so CI never bundles
// Remotion. Expected messages are the fixture's exact texts; the frames in
// them come from tests/fixtures/check/fixture.mjs's timeline (worked out from
// the 3.6 layout rules and listed in its header) or from the hand-written
// timelines below, never from the module under test. Task 5.2's modules are
// imported inside their tests, so a missing one fails only those.

import { test } from "node:test";
import assert from "node:assert/strict";
import { appendFileSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, renameSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
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
import { BODY_KINDS, validateScript } from "../bin/script-schema.mjs";
import { candidates } from "../src/kit/gallery/catalog.ts";
import { TONE_SECONDS, checkFiles, checkScript, checkTimeline, maxScript, stubFiles, voiceBytes } from "./fixtures/check/fixture.mjs";
import { fixtureTimeline } from "./render/fixtures/explainer/fixture.mjs";

const HINT = "; run node bin/kit.mjs timeline";
const QUESTION = "chapter 1 现象, scene 1 (question)";
const CUSTOM = "chapter 2 原理, scene 1 (custom)";
const CONCEPT = "chapter 3 应用, scene 1 (concept-card)";
const WHERES = ["hook", QUESTION, CUSTOM, CONCEPT, "summary", "end-card"];
const STALE = "timeline.json is stale (its text, voice or instructions changed); run node bin/kit.mjs voice, voice-import, then timeline";
const CHANGED = "the project changed while the check ran; run node bin/kit.mjs check again";

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

const neverSheet = async () => {
  throw new Error("the contact sheet must not run");
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

// --- rule 6: probe, text wrapLines cannot wrap (#924) ---------------------------
//
// Each message is one src/kit/text.ts wrapLines throws for the field's value
// at that width, after the `kit probe: target "<where>" frame <n>: ` prefix
// probe() adds; RECORDED is tests/render/check.test.mjs's #924 run on
// bce4aef, verbatim.

const CHART = "chapter 3 应用, scene 2 (chart)";
const OPENING = "its leading opening marks or spaces fill the line";
const PIECE = "one character with its punctuation marks is wider than the line";
const LABELS = ["一月", "二月", "（（（（甲乙", "四月", "五月", "六月", "七月", "八月"];
const RECORDED = 'probe: kit probe: target "chapter 3 应用, scene 2 (chart)" frame 15: opening marks of "（（（（甲乙" fill maxWidth 4';

// The fixture script with a line chart as chapter 3's second scene, its
// point 2 labelled `label`, after `edit`.
const chartScript = ({ label = LABELS[2], title = "月度销量", edit = () => {} } = {}) =>
  checkScript((raw) => {
    const labels = LABELS.with(2, label);
    raw.chapters[2].scenes.push({
      kind: "chart",
      narration: [["销量逐月变化"]],
      content: { type: "line", title, points: labels.map((value, i) => ({ label: value, value: 10 + i })) },
    });
    edit(raw);
  });

// probeRule's reasons for `s` when the probe rejects with `message`.
async function rejected(t, s, message) {
  const probe = async () => {
    throw new Error(message);
  };
  return probeRule(customRoot(t), s, checkTimeline(s), { probe, theme: KIT_THEME });
}

const unwrappable = (where, field, value, width, cause) =>
  `${where}: ${field} "${value}" cannot be wrapped in ${width} characters: ${cause}; shorten that run`;

test("probe: a chart label whose opening marks fill its line names the scene and field", async (t) => {
  assert.deepEqual(await rejected(t, chartScript(), `kit probe: target "${CHART}" frame 15: opening marks of "（（（（甲乙" fill maxWidth 4`), [
    unwrappable(CHART, "content.points[2].label", "（（（（甲乙", 4, OPENING),
  ]);
});

test("probe: the recorded in-image probe line names the chart label", async (t) => {
  assert.ok(RECORDED.startsWith("probe: "), RECORDED);
  assert.deepEqual(await rejected(t, chartScript(), RECORDED.slice("probe: ".length)), [
    unwrappable(CHART, "content.points[2].label", "（（（（甲乙", 4, OPENING),
  ]);
});

test("probe: the hook title whose opening marks fill its line names content.title", async (t) => {
  const s = checkScript((raw) => {
    raw.hook.content.title = "（（（（甲乙";
  });
  assert.deepEqual(await rejected(t, s, 'kit probe: target "hook" frame 15: opening marks of "（（（（甲乙" fill maxWidth 4'), [
    unwrappable("hook", "content.title", "（（（（甲乙", 4, OPENING),
  ]);
});

test("probe: a piece wider than its line names the field that holds it", async (t) => {
  assert.deepEqual(await rejected(t, chartScript({ label: "，（（（（甲" }), `kit probe: target "${CHART}" frame 15: piece "（（（（甲" is wider than maxWidth 4`), [
    unwrappable(CHART, "content.points[2].label", "，（（（（甲", 4, PIECE),
  ]);
});

test("probe: a character with ten closing marks wider than its line names its field", async (t) => {
  const closing = checkScript((raw) => {
    raw.chapters[2].scenes[0].content.points[0] = "甲））））））））））";
  });
  assert.deepEqual(await rejected(t, closing, `kit probe: target "${CONCEPT}" frame 15: piece "甲））））））））））" is wider than maxWidth 8`), [
    unwrappable(CONCEPT, "content.points[0]", "甲））））））））））", 8, PIECE),
  ]);
});

test("probe: a nested field of another kind is named by its full path", async (t) => {
  const steps = "chapter 1 现象, scene 2 (process-steps)";
  const s = checkScript((raw) => {
    raw.chapters[0].scenes.push({
      kind: "process-steps",
      narration: [["分三步看"]],
      content: { steps: [{ label: "第一步", detail: "放入水中" }, { label: "（（（（甲乙" }] },
    });
  });
  assert.deepEqual(await rejected(t, s, `kit probe: target "${steps}" frame 15: opening marks of "（（（（甲乙" fill maxWidth 4`), [
    unwrappable(steps, "content.steps[1].label", "（（（（甲乙", 4, OPENING),
  ]);
});

test("probe: without a target every scene is searched in playback order, past the custom scene", async (t) => {
  const s = checkScript((raw) => {
    raw.summary.content.points[0] = "（（（（甲乙";
  });
  assert.equal(explainerScenes(s, checkTimeline(s))[2].props.content, undefined);
  assert.deepEqual(await rejected(t, s, 'Error: opening marks of "（（（（甲乙" fill maxWidth 4'), [
    unwrappable("summary", "content.points[0]", "（（（（甲乙", 4, OPENING),
  ]);
});

test("probe: a target restricts the search to its scene, and a target without the text falls back", async (t) => {
  const s = checkScript((raw) => {
    raw.chapters[2].scenes[0].content.points[0] = "（（（（甲乙";
    raw.summary.content.points[0] = "（（（（甲乙";
  });
  const failure = (where) => `kit probe: target "${where}" frame 15: opening marks of "（（（（甲乙" fill maxWidth 4`;
  assert.deepEqual(await rejected(t, s, failure("summary")), [unwrappable("summary", "content.points[0]", "（（（（甲乙", 4, OPENING)]);
  assert.deepEqual(await rejected(t, s, failure(CONCEPT)), [unwrappable(CONCEPT, "content.points[0]", "（（（（甲乙", 4, OPENING)]);
  assert.deepEqual(await rejected(t, s, failure("hook")), [`probe: ${failure("hook")}`]);
  assert.deepEqual(await rejected(t, s, failure("no such scene")), [`probe: ${failure("no such scene")}`]);
});

test("probe: the target and the text are decoded from their JSON quoting", async (t) => {
  const where = 'chapter 3 应"用, scene 2 (chart)';
  const quoted = chartScript({
    label: '（（（（"\\',
    edit: (raw) => {
      raw.chapters[2].name = '应"用';
    },
  });
  assert.deepEqual(await rejected(t, quoted, 'kit probe: target "chapter 3 应\\"用, scene 2 (chart)" frame 15: opening marks of "（（（（\\"" fill maxWidth 4'), [
    unwrappable(where, "content.points[2].label", '（（（（"\\', 4, OPENING),
  ]);
  const backslash = chartScript({ label: "（（（（\\甲" });
  assert.deepEqual(await rejected(t, backslash, `kit probe: target "${CHART}" frame 15: opening marks of "（（（（\\\\" fill maxWidth 4`), [
    unwrappable(CHART, "content.points[2].label", "（（（（\\甲", 4, OPENING),
  ]);
  // An escape JSON.parse refuses is not decoded, and nothing is named.
  const invalid = `kit probe: target "${CHART}" frame 15: opening marks of "（（（（\\x" fill maxWidth 4`;
  assert.deepEqual(await rejected(t, backslash, invalid), [`probe: ${invalid}`]);
});

test("probe: when two fields of the scene hold the text, the first in field order is named (known limit)", async (t) => {
  const s = chartScript({ title: "（（（（甲乙增长" });
  assert.deepEqual(await rejected(t, s, `kit probe: target "${CHART}" frame 15: opening marks of "（（（（甲乙" fill maxWidth 4`), [
    unwrappable(CHART, "content.title", "（（（（甲乙增长", 4, OPENING),
  ]);
});

test("probe: a text no field holds stays probe: <message>", async (t) => {
  const message = `kit probe: target "${CHART}" frame 15: opening marks of "（（（（丙丁" fill maxWidth 4`;
  assert.deepEqual(await rejected(t, chartScript(), message), [`probe: ${message}`]);
});

// --- runChecks ---------------------------------------------------------------

const STAGES = ["schema", "timeline", "assets", "unnarrated", "rest", "music", "probe", "contact-sheet", "hash"];
// The log of the first n stages: the snapshot line follows stage 2 when a
// later stage runs.
const stageLines = (n) => {
  const lines = STAGES.slice(0, n).map((name, i) => `check: stage ${i + 1} ${name}`);
  if (n > 2) lines.splice(2, 0, "check: snapshot");
  return lines;
};

// A stub contact sheet that records its argument, creates .kit/check/ and
// writes `output`, so stage 9's rename has a file; `edit` then runs.
function stubSheet(edit = () => {}) {
  const calls = [];
  const sheet = async (root, options) => {
    calls.push({ root, ...options });
    mkdirSync(dirname(options.output), { recursive: true });
    writeFileSync(options.output, "stub sheet");
    await edit(root, options);
  };
  return { sheet, calls };
}

test("the rule list is the nine stages in order", () => {
  assert.deepEqual(
    RULES.map((rule) => rule.name),
    STAGES,
  );
});

test("runChecks: the passing project runs the snapshot and all nine stages and passes", async (t) => {
  const log = [];
  const { probe, calls } = stubProbe();
  const { sheet } = stubSheet();
  const { reasons } = await runChecks(customRoot(t), { probe, sheet, files: stubFiles(checkFiles()), log: (line) => log.push(line) });
  assert.deepEqual(reasons, []);
  assert.deepEqual(log, stageLines(9));
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
  const { reasons } = await runChecks(customRoot(t), { probe: neverProbe, files, log: (line) => log.push(line) });
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
  const { reasons } = await runChecks(customRoot(t), { probe: neverProbe, files, log: (line) => log.push(line) });
  assert.deepEqual(reasons, [`timeline.json: missing or not JSON${HINT}`]);
  assert.deepEqual(log, stageLines(2));
});

test("runChecks: stages 3–6 all run, and a failure in them skips the probe", async (t) => {
  const log = [];
  const files = checkFiles();
  files.delete(`public/voice/${stems[3]}.wav`);
  const reveals = { summary: () => [{ type: "key", startFrame: 0, endFrame: 200 }] };
  const { reasons } = await runChecks(customRoot(t), { probe: neverProbe, files: stubFiles(files), reveals, log: (line) => log.push(line) });
  assert.deepEqual(reasons, [
    `${QUESTION}, sentence 1 block 1: ${stems[3]}.wav is missing`,
    "summary: key reveal ending at frame 200 rests 15 frames before the exit at frame 215; needs 30",
  ]);
  assert.deepEqual(log, stageLines(6));
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
  const { sheet } = stubSheet();
  assert.deepEqual((await runChecks(root, { probe, sheet })).reasons, []);
  const wav = join(root, "public", "voice", `${stems[0]}.wav`);
  rmSync(wav);
  writeFileSync(join(root, "elsewhere.wav"), voiceBytes(stems[0]));
  symlinkSync(join(root, "elsewhere.wav"), wav);
  assert.deepEqual((await runChecks(root, { probe: neverProbe, sheet: neverSheet })).reasons, [`hook, sentence 1 block 1: ${stems[0]}.wav is missing`]);
});

// --- task 5.2: referenced files, tiles, the snapshot, the sheet and the hash ---

test("assets: the rule reads exactly the public/ paths that referencedPublicFiles returns", async () => {
  const { referencedPublicFiles } = await import("../bin/project-hash.mjs");
  for (const s of [script, checkScript((raw) => (raw.hook.image = "light.webp"))]) {
    const t = checkTimeline(s);
    const files = checkFiles(s, t);
    files.set("public/images/light.webp", Buffer.from("webp"));
    const stub = stubFiles(files);
    const read = [];
    const recording = {
      ...stub,
      regular: async (path) => {
        read.push(path);
        return stub.regular(path);
      },
    };
    assert.deepEqual(await assetsRule(s, t, recording), []);
    const publicReads = [...new Set(read.filter((path) => path.startsWith("public/")))].sort();
    assert.deepEqual(publicReads, referencedPublicFiles(s, t));
  }
});

test("sheetTiles: at m = 5 the hook, the question, the concept card and the summary at their pre-exit frames", async () => {
  const { sheetTiles } = await import("../bin/contact-sheet.mjs");
  // The fixture header's windows: hook 704, question 959, concept card 1656,
  // summary 1886; the pre-exit frame is one before.
  assert.deepEqual(sheetTiles(explainerScenes(script, timeline)), [
    { where: "hook", frame: 703 },
    { where: QUESTION, frame: 958 },
    { where: CONCEPT, frame: 1655 },
    { where: "summary", frame: 1885 },
  ]);
});

test("sheetTiles: at m = 22 candidates 0, 7, 14 and 21; the end card is never picked", async () => {
  const { sheetTiles } = await import("../bin/contact-sheet.mjs");
  // 22 candidates of 100 frames, each exiting at its frame 85, then an end
  // card of 90 frames without an exit.
  const entries = Array.from({ length: 22 }, (_, k) => ({
    where: `scene ${k}`,
    kind: k === 0 ? "hook" : k === 21 ? "summary" : "question",
    startFrame: 100 * k,
    durationInFrames: 100,
    exit: { startFrame: 85, frames: 15 },
  }));
  entries.push({ where: "end-card", kind: "end-card", startFrame: 2200, durationInFrames: 90, exit: null });
  assert.deepEqual(sheetTiles(entries), [
    { where: "scene 0", frame: 84 },
    { where: "scene 7", frame: 784 },
    { where: "scene 14", frame: 1484 },
    { where: "scene 21", frame: 2184 },
  ]);
  // Without an exit a candidate's pre-exit frame is its last frame.
  const five = entries.slice(0, 4).concat(entries[21], entries[22]).map((entry, k) => ({ ...entry, startFrame: 100 * k }));
  five[4] = { ...five[4], exit: null };
  assert.deepEqual(
    sheetTiles(five).map((tile) => tile.frame),
    [84, 184, 384, 499],
  );
  assert.ok(sheetTiles(five).every((tile) => tile.where !== "end-card"));
});

// .kit/check/'s entries, sorted; [] when it is absent.
const checkDir = (root) => {
  const dir = join(root, ".kit", "check");
  return existsSync(dir) ? readdirSync(dir).sort() : [];
};

test("runChecks: a pass gives the hash, the sheet and the tiles, logs the snapshot and stages 1–9, and leaves only contact.png", async (t) => {
  const { projectHash } = await import("../bin/project-hash.mjs");
  const root = onDisk(t);
  const log = [];
  const { probe } = stubProbe();
  const { sheet, calls } = stubSheet();
  const result = await runChecks(root, { probe, sheet, log: (line) => log.push(line) });
  assert.deepEqual(log, stageLines(9));
  const tiles = [
    { where: "hook", frame: 703 },
    { where: QUESTION, frame: 958 },
    { where: CONCEPT, frame: 1655 },
    { where: "summary", frame: 1885 },
  ];
  const texts = { scriptText: readFileSync(join(root, "script.json"), "utf8"), timelineText: readFileSync(join(root, "timeline.json"), "utf8") };
  const { hash } = await projectHash(root, { script, timeline, ...texts });
  assert.match(hash, /^[0-9a-f]{64}$/);
  assert.deepEqual(result, { reasons: [], pass: { hash, sheet: ".kit/check/contact.png", tiles } });
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], {
    root,
    script: JSON.parse(texts.scriptText),
    timeline: JSON.parse(texts.timelineText),
    tiles,
    workDir: join(root, ".kit", "check", "sheet"),
    output: join(root, ".kit", "check", "contact.next.png"),
  });
  assert.deepEqual(checkDir(root), ["contact.png"]);
  assert.equal(readFileSync(join(root, ".kit", "check", "contact.png"), "utf8"), "stub sheet");
  // The custom index the snapshot wrote is the generated one.
  assert.equal(readFileSync(join(root, "src", "custom", "index.ts"), "utf8"), customIndexSource(script));
});

test("runChecks: a project changed while the check runs fails with the one changed-project reason and no sheet", async (t) => {
  const changed = { reasons: [CHANGED], pass: null };
  const editSrc = onDisk(t);
  const srcProbe = stubProbe((targets) => {
    appendFileSync(join(editSrc, "src", "custom", "Orbit.tsx"), "// edited\n");
    return cleanResult(targets);
  });
  assert.deepEqual(await runChecks(editSrc, { probe: srcProbe.probe, sheet: stubSheet().sheet }), changed, "a probe that edits src/");
  assert.deepEqual(checkDir(editSrc), []);

  const editWav = onDisk(t);
  const wavSheet = stubSheet((root) => appendFileSync(join(root, "public", "voice", `${stems[0]}.wav`), "x"));
  assert.deepEqual(await runChecks(editWav, { probe: stubProbe().probe, sheet: wavSheet.sheet }), changed, "a sheet that edits a voice WAV");
  assert.deepEqual(checkDir(editWav), []);

  const deleted = onDisk(t);
  const deleting = stubProbe((targets) => {
    rmSync(join(deleted, "script.json"));
    return cleanResult(targets);
  });
  assert.deepEqual(await runChecks(deleted, { probe: deleting.probe, sheet: stubSheet().sheet }), changed, "a probe that deletes script.json");
  assert.deepEqual(checkDir(deleted), []);
});

test("runChecks: a sheet that throws is the contact sheet reason, and no sheet is left", async (t) => {
  const root = onDisk(t);
  const { sheet } = stubSheet(() => {
    throw new Error("ffmpeg exited with 1");
  });
  assert.deepEqual(await runChecks(root, { probe: stubProbe().probe, sheet }), { reasons: ["contact sheet: ffmpeg exited with 1"], pass: null });
  assert.deepEqual(checkDir(root), []);
});

test("runChecks: a symlinked src is its reason, runs stages 3–6 and skips stages 7–9", async (t) => {
  const root = onDisk(t);
  renameSync(join(root, "src"), join(root, "src-real"));
  symlinkSync(join(root, "src-real"), join(root, "src"));
  const log = [];
  const result = await runChecks(root, { probe: neverProbe, sheet: neverSheet, log: (line) => log.push(line) });
  assert.deepEqual(result, { reasons: ["src: is a symlink"], pass: null });
  assert.deepEqual(log, stageLines(6));
});

test("runChecks: a project missing its custom file gives only the assets reason", async (t) => {
  const root = onDisk(t);
  rmSync(join(root, "src", "custom", "Orbit.tsx"));
  const log = [];
  const result = await runChecks(root, { probe: neverProbe, sheet: neverSheet, log: (line) => log.push(line) });
  assert.deepEqual(result, { reasons: [`${CUSTOM}: src/custom/Orbit.tsx is missing`], pass: null });
  assert.deepEqual(log, stageLines(6));
});

// --- task 6.5: the music stage --------------------------------------------------

// A library of one loop track, "fixture-bar", holding `bytes`.
function musicLibrary(t, bytes = "bar audio") {
  const dir = mkdtempSync(join(tmpdir(), "check-rules-music-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const tracks = [{ id: "fixture-bar", loop: true, file: "fixture-bar.wav", duration_seconds: 2.01 }];
  writeFileSync(join(dir, "manifest.json"), JSON.stringify({ version: 1, tracks }));
  writeFileSync(join(dir, "fixture-bar.wav"), bytes);
  return dir;
}

test("runChecks: a music reason, like a stage 3–5 reason, runs stages 3–6 and skips stages 7–9", async (t) => {
  const s = checkScript((raw) => (raw.music = "fixture-gone"));
  const log = [];
  const absent = join(tmpdir(), "check-rules-no-library", "absent");
  const files = stubFiles(checkFiles(s, checkTimeline(s)));
  const result = await runChecks(customRoot(t), { probe: neverProbe, sheet: neverSheet, files, musicDir: absent, log: (line) => log.push(line) });
  assert.deepEqual(result, { reasons: ['music: this image has no music library; set "music" to "none"'], pass: null });
  assert.deepEqual(log, stageLines(6));
  const named = await runChecks(customRoot(t), { probe: neverProbe, sheet: neverSheet, files, musicDir: musicLibrary(t), log: () => {} });
  assert.deepEqual(named.reasons, ['music: unknown track "fixture-gone"; choose "none" or one of: fixture-bar']);
});

test("runChecks: a music project passes against musicDir, and its hash carries the track", async (t) => {
  const { projectHash } = await import("../bin/project-hash.mjs");
  const s = checkScript((raw) => (raw.music = "fixture-bar"));
  const root = mkdtempSync(join(tmpdir(), "check-rules-disk-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  for (const [path, bytes] of checkFiles(s, checkTimeline(s))) {
    mkdirSync(dirname(join(root, path)), { recursive: true });
    writeFileSync(join(root, path), bytes);
  }
  const musicDir = musicLibrary(t);
  const log = [];
  const result = await runChecks(root, { probe: stubProbe().probe, sheet: stubSheet().sheet, musicDir, log: (line) => log.push(line) });
  assert.deepEqual(log, stageLines(9));
  const texts = { scriptText: readFileSync(join(root, "script.json"), "utf8"), timelineText: readFileSync(join(root, "timeline.json"), "utf8") };
  const timelineOnDisk = JSON.parse(texts.timelineText);
  const withTrack = await projectHash(root, { script: s, timeline: timelineOnDisk, ...texts, musicDir });
  const without = await projectHash(root, { script: s, timeline: timelineOnDisk, ...texts, musicDir: join(root, "absent") });
  assert.equal(result.pass?.hash, withTrack.hash);
  assert.notEqual(withTrack.hash, without.hash);
});

// --- task 5.4: the maximum-size script ----------------------------------------------

test("5.4: maxScript is the largest valid script", () => {
  const s = validateScript(JSON.stringify(maxScript()));
  assert.equal(s.ok, true, s.errors?.map((e) => e.message).join("\n"));
  const { script: max } = s;
  assert.deepEqual(
    max.chapters.map((chapter) => chapter.scenes.length),
    [4, 4, 4, 4, 4],
  );
  const body = max.chapters.flatMap((chapter) => chapter.scenes);
  assert.deepEqual(
    body.map((scene) => scene.kind),
    [...BODY_KINDS, ...BODY_KINDS],
  );
  assert.deepEqual([max.hook.kind, max.summary.kind, max.endCard.kind], ["hook", "summary", "end-card"]);
  // Playback order, each scene with the catalog sample it takes.
  const scenes = [max.hook, ...body, max.summary, max.endCard];
  const sampleIds = ["image", ...BODY_KINDS.map(() => "image"), ...BODY_KINDS.map(() => "max"), "image", "image"];
  for (const [i, scene] of scenes.entries()) {
    assert.equal(scene.narration.length, 4, `scene ${i + 1}: sentences`);
    for (const sentence of scene.narration) {
      assert.equal(sentence.length, 4, `scene ${i + 1}: blocks`);
      for (const block of sentence) assert.equal([...block].length, 16, `scene ${i + 1}: block ${JSON.stringify(block)}`);
    }
    const sample = candidates.find((entry) => entry.kind === scene.kind).samples.find((entry) => entry.id === sampleIds[i]);
    assert.deepEqual(scene.content, sample.content, `scene ${i + 1} (${scene.kind}) content`);
    assert.equal(Object.hasOwn(scene, "image") ? scene.image : null, sample.image, `scene ${i + 1} (${scene.kind}) image`);
  }
  // totalFrames in (5300, 5400], the 180 s maximum, and over it one
  // millisecond longer; every tone a whole number of samples.
  const frames = (seconds) => checkTimeline(max, () => Math.round(seconds * 48000)).totalFrames;
  const longest = frames(TONE_SECONDS);
  assert.ok(longest > 5300 && longest <= 5400, `${longest} frames at ${TONE_SECONDS} s`);
  assert.ok(frames(TONE_SECONDS + 0.001) > 5400, `${frames(TONE_SECONDS + 0.001)} frames one millisecond longer`);
});
