// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The renderer in a real headless shell (task 3.8): caption pages and the
// custom scene through the probe, one MP4, a fresh project's tracked null
// custom index, and the render rejections. Runs only inside the workspace
// image, from a project root whose node_modules link reaches
// /opt/remotion-template/node_modules, one test file after another:
//   node --test --test-concurrency=1 "tests/render/*.test.mjs"
// CI's tests/*.test.mjs glob does not match this file. Expected values come
// from the task 3.8 fixture: the 481-frame fixture timeline, the probe
// target table, the caption band 930–1026, and the exact messages.
//
// Every step runs in `before`, in the order the custom index moves through:
// the tracked null index, then Orbit's, then Blank's. Each bundle is made
// once for its index state and entry and reused by every select in it.

import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { copyFileSync, cpSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { bundle } from "@remotion/bundler";
import { openBrowser, renderMedia, selectComposition } from "@remotion/renderer";
import { writeCustomIndex } from "../../bin/custom-index.mjs";
import { explainerScenes } from "../../bin/explainer-scenes.mjs";
import { probe, violations } from "../../bin/probe.mjs";
import { themes } from "../../src/kit/theme.ts";
import { LONG_ASCII, LONG_HAN, fixtureScript, fixtureTimeline, maxLengthScript } from "./fixtures/explainer/fixture.mjs";

const TEMPLATE = fileURLToPath(new URL("../..", import.meta.url));
const theme = themes.light;
const TOTAL = 481;
const BAND = { top: 930, bottom: 1026 };
const CENTRE_TOLERANCE = 0.5;
const ORBIT_TEXT = "Orbit 77 2 16 #1F6FEB";
const CUSTOM = "chapter 2 原理, scene 1 (custom)";

const script = fixtureScript();
const timeline = fixtureTimeline(script);
const longScript = maxLengthScript();
const longTimeline = fixtureTimeline(longScript);

// The fixture's probe targets: [id, entranceFrames, exitFrame, frames,
// caption at each frame (null for none)], each lasting 481 frames.
const TARGETS = [
  ["A", 47, 81, [47, 80, 80], ["光为什么", "在水里", "在水里"]],
  ["B", 64, 111, [64, 110, 110], ["会弯折", null, null]],
  ["C", 200, 241, [200, 240, 240], ["速度变了", null, null]],
  ["D", 55, 56, [55, 55, 55], [null, null, null]],
  ["E", 47, 65, [47, 64, 64], [LONG_HAN, LONG_ASCII, LONG_ASCII]],
];

function probeTarget([id, entranceFrames, exitFrame]) {
  const props = id === "E" ? { script: longScript, timeline: longTimeline } : { script, timeline };
  return { id, scene: "explainer", props, durationInFrames: TOTAL, entranceFrames, exitFrame };
}

const blockTexts = (s) => new Set([s.hook, ...s.chapters.flatMap((c) => c.scenes), s.summary, s.endCard].flatMap((scene) => scene.narration.flat()));

const isInside = (path, ancestor) => ancestor === "" || path === ancestor || path.startsWith(`${ancestor}>`);

// Settles a step into {value} or {error}, timing it.
const timings = {};
async function step(name, run) {
  const started = Date.now();
  try {
    return { value: await run() };
  } catch (error) {
    return { error };
  } finally {
    timings[name] = Date.now() - started;
  }
}

let root;
let browser = null;
const outcomes = {};

function copyProject() {
  root = mkdtempSync(join(tmpdir(), "explainer-render-"));
  for (const part of ["src", "bin", join("tests", "render", "fixtures")]) {
    cpSync(join(TEMPLATE, part), join(root, part), { recursive: true });
  }
  for (const name of ["package.json", "tsconfig.json", "remotion.config.ts"]) copyFileSync(join(TEMPLATE, name), join(root, name));
  symlinkSync(realpathSync(join(TEMPLATE, "node_modules")), join(root, "node_modules"));
  mkdirSync(join(root, "public"));
  // 481 frames of 48 kHz mono: 481 × 1600 samples.
  execFileSync("ffmpeg", [
    "-nostdin", "-v", "error",
    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
    "-af", `atrim=end_sample=${TOTAL * 1600}`,
    "-ac", "1", "-c:a", "pcm_s16le",
    join(root, "public", "narration.wav"),
  ]);
}

const entry = (name) => (name === "kit" ? join(root, "src", "kit", "index.ts") : name === "main" ? join(root, "src", "index.ts") : join(root, "tests", "render", "fixtures", "explainer", "entry.tsx"));

function bundled(name, entryName) {
  return step(`bundle ${name}`, () =>
    bundle({
      entryPoint: entry(entryName),
      outDir: join(root, "bundles", name),
      enableCaching: false,
      publicDir: join(root, "public"),
      symlinkPublicDir: true,
    }),
  );
}

// Selects composition `id` from a settled bundle; `name` labels the timing.
function select(name, serveUrl, id, inputProps) {
  if (serveUrl.error) return Promise.resolve(serveUrl);
  return step(`select ${name}`, () => selectComposition({ serveUrl: serveUrl.value, id, inputProps, puppeteerInstance: browser }));
}

// The custom entry's scene props, as a kit-check probe target would pass them.
const customTarget = () => {
  const custom = explainerScenes(script, timeline)[2];
  return { targets: [{ scene: "custom", props: { ...custom.props, theme }, durationInFrames: custom.durationInFrames }] };
};

before(async () => {
  copyProject();
  browser = await openBrowser("chrome");

  // A fresh project: the tracked null src/custom/index.ts.
  const freshKit = await bundled("fresh kit", "kit");
  outcomes.freshKitProbe = await select("freshKitProbe", freshKit, "KitProbe", customTarget());
  const freshFixture = await bundled("fresh fixture", "fixture");
  outcomes.freshOrbit = await select("freshOrbit", freshFixture, "Explainer", { script, timeline });

  // Orbit's index.
  copyFileSync(join(TEMPLATE, "tests", "render", "fixtures", "explainer", "Orbit.tsx"), join(root, "src", "custom", "Orbit.tsx"));
  await writeCustomIndex(root, script);

  outcomes.probe = await step("probe", () =>
    probe({ entryPoint: entry("fixture"), targets: TARGETS.map(probeTarget), publicDir: join(root, "public") }),
  );

  const fixture = await bundled("fixture", "fixture");
  outcomes.explainer = await select("explainer", fixture, "Explainer", { script, timeline });
  if (outcomes.explainer.value) {
    const output = join(root, "out", "explainer.mp4");
    outcomes.render = await step("render", async () => {
      await renderMedia({
        composition: outcomes.explainer.value,
        serveUrl: fixture.value,
        codec: "h264",
        outputLocation: output,
        inputProps: { script, timeline },
        puppeteerInstance: browser,
      });
      return JSON.parse(
        execFileSync(
          "ffprobe",
          [
            "-v", "error", "-count_frames",
            "-show_entries", "stream=codec_type,codec_name,width,height,r_frame_rate,nb_read_frames:format=duration",
            "-of", "json", output,
          ],
          { encoding: "utf8" },
        ),
      );
    });
  }
  const comet = fixtureScript((raw) => {
    raw.chapters[1].scenes[0].component = "Comet";
  });
  outcomes.comet = await select("comet", fixture, "Explainer", { script: comet, timeline: fixtureTimeline(comet) });
  const turned = structuredClone(timeline);
  turned.scenes[0].sentences[0].blocks[1].text = "会转弯";
  outcomes.textDiffers = await select("textDiffers", fixture, "Explainer", { script, timeline: turned });
  outcomes.nullProps = await select("nullProps", fixture, "Explainer", { script: null, timeline: null });
  // Task 6.5: the music input prop, absent (outcomes.explainer), true, false
  // and anything else.
  outcomes.musicTrue = await select("musicTrue", fixture, "Explainer", { script, timeline, music: true });
  outcomes.musicFalse = await select("musicFalse", fixture, "Explainer", { script, timeline, music: false });
  outcomes.musicYes = await select("musicYes", fixture, "Explainer", { script, timeline, music: "yes" });
  const tooLong = structuredClone(script);
  tooLong.title = "光".repeat(25);
  tooLong.hook.narration[0][0] = "光".repeat(17);
  outcomes.tooLong = await select("tooLong", fixture, "Explainer", { script: tooLong, timeline });

  const kit = await bundled("kit", "kit");
  outcomes.kitProbe = await select("kitProbe", kit, "KitProbe", customTarget());
  outcomes.kitExplainer = await select("kitExplainer", kit, "Explainer", { script, timeline });

  const main = await bundled("main", "main");
  outcomes.main = await select("main", main, "Main", {});

  // Blank's index: a custom file without a component.
  const blank = fixtureScript((raw) => {
    raw.chapters[1].scenes[0].component = "Blank";
  });
  writeFileSync(join(root, "src", "custom", "Blank.tsx"), "export default null;\n");
  await writeCustomIndex(root, blank);
  const blankFixture = await bundled("blank fixture", "fixture");
  outcomes.blank = await select("blank", blankFixture, "Explainer", { script: blank, timeline: fixtureTimeline(blank) });
});

after(async () => {
  try {
    if (browser !== null) await browser.close({ silent: true });
  } finally {
    if (root !== undefined) rmSync(root, { recursive: true, force: true });
  }
});

function value(outcome, label) {
  if (outcome?.error) throw outcome.error;
  assert.ok(outcome?.value, `${label} did not run`);
  return outcome.value;
}

function rejectsWith(outcome, message) {
  assert.ok(outcome?.error, `expected a rejection with ${JSON.stringify(message)}`);
  assert.ok(outcome.error.message.includes(message), `${JSON.stringify(outcome.error.message)} does not include ${JSON.stringify(message)}`);
}

test("timings", (t) => {
  for (const [name, ms] of Object.entries(timings)) t.diagnostic(`${name}: ${ms} ms`);
});

test("each block shows as one centred caption line in the band, exactly during its frames", () => {
  const result = value(outcomes.probe, "probe");
  assert.deepEqual(
    result.frames.map(({ target, role, frame }) => [target, role, frame]),
    TARGETS.flatMap(([id, , , frames]) => ["entrance-end", "midpoint", "pre-exit"].map((role, k) => [id, role, frames[k]])),
  );
  for (const [id, , , frames, captions] of TARGETS) {
    const texts = blockTexts(id === "E" ? longScript : script);
    result.frames
      .filter((frame) => frame.target === id)
      .forEach((frame, k) => {
        const label = `${id} ${frame.role} frame ${frame.frame}`;
        const marks = frame.marked.filter(({ input }) => texts.has(input));
        if (captions[k] === null) {
          assert.deepEqual(marks, [], label);
          return;
        }
        assert.deepEqual(marks.map(({ input, rendered }) => [input, rendered]), [[captions[k], captions[k]]], label);
        const records = frame.texts.filter((record) => isInside(record.path, marks[0].path));
        assert.equal(records.length, 1, `${label}: one text record`);
        const [{ text, lines, glyphBox }] = records;
        assert.equal(text, captions[k], label);
        assert.deepEqual(lines, [captions[k]], `${label}: a single line`);
        assert.ok(glyphBox.y >= BAND.top && glyphBox.y + glyphBox.height <= BAND.bottom, `${label}: ${JSON.stringify(glyphBox)} outside the band`);
        const centre = glyphBox.x + glyphBox.width / 2;
        assert.ok(Math.abs(centre - 960) <= CENTRE_TOLERANCE, `${label}: centred at ${centre}`);
      });
  }
});

test("no probe frame of any target breaks a probe rule", () => {
  assert.deepEqual(violations(value(outcomes.probe, "probe"), theme), []);
});

test("the custom scene renders inside its frames only", () => {
  for (const frame of value(outcomes.probe, "probe").frames) {
    const shown = frame.texts.some((record) => record.text === ORBIT_TEXT);
    assert.equal(shown, frame.target === "C", `${frame.target} frame ${frame.frame}`);
  }
});

test("the fixture Explainer renders a 481-frame 1920×1080 H.264 MP4 with AAC audio", () => {
  assert.equal(value(outcomes.explainer, "select").durationInFrames, TOTAL);
  const facts = value(outcomes.render, "render");
  const video = facts.streams.filter((s) => s.codec_type === "video");
  const audio = facts.streams.filter((s) => s.codec_type === "audio");
  assert.equal(video.length, 1);
  assert.equal(video[0].codec_name, "h264");
  assert.equal(video[0].width, 1920);
  assert.equal(video[0].height, 1080);
  assert.equal(video[0].r_frame_rate, "30/1");
  assert.equal(Number(video[0].nb_read_frames), TOTAL);
  assert.equal(audio.length, 1);
  assert.equal(audio[0].codec_name, "aac");
  const duration = Number(facts.format.duration);
  assert.ok(Math.abs(duration - TOTAL / 30) <= 1 / 30, `duration ${duration} s`);
});

test("ffprobe facts", (t) => {
  t.diagnostic(`ffprobe: ${JSON.stringify(value(outcomes.render, "render"))}`);
});

test("a fresh project bundles with the tracked null index and rejects the Orbit script, naming the scene", () => {
  assert.equal(value(outcomes.freshKitProbe, "fresh KitProbe").durationInFrames, 77);
  rejectsWith(outcomes.freshOrbit, `kit render: ${CUSTOM}: src/custom/index.ts is for no custom scene, not "Orbit"`);
});

test("each bad input rejects through selectComposition with its message", () => {
  rejectsWith(outcomes.comet, `kit render: ${CUSTOM}: src/custom/index.ts is for "Orbit", not "Comet"`);
  rejectsWith(
    outcomes.textDiffers,
    "kit render: hook, sentence 1 block 2: timeline.json text differs from script.json; run node bin/kit.mjs timeline",
  );
  rejectsWith(outcomes.nullProps, "kit render: input props need script and timeline objects");
  rejectsWith(outcomes.tooLong, "kit render: script: title is too long (limit 24 characters) (and 1 more)");
  rejectsWith(outcomes.blank, `kit render: ${CUSTOM}: src/custom/Blank.tsx does not default-export a component`);
});

// The kinds the tracked registry lists, read from its text: one
// `'<kind>': components['<id>']` line each (task 4.2).
const trackedKinds = () =>
  new Set(
    [...readFileSync(join(TEMPLATE, "src", "kit", "render", "registry.ts"), "utf8").matchAll(/^\s*'([a-z-]+)': components\['[a-z0-9-]+'\],?$/gm)].map(
      (m) => m[1],
    ),
  );
// The fixture script's non-custom scenes in playback order, each with its place.
const PLAYBACK_KINDS = [
  ["hook", "hook"],
  ["question", "chapter 1 现象, scene 1 (question)"],
  ["concept-card", "chapter 3 应用, scene 1 (concept-card)"],
  ["summary", "summary"],
  ["end-card", "end-card"],
];

test("the real kit entry's Explainer selects 481 frames when the script's kinds are registered, else names the first missing one", (t) => {
  const missing = PLAYBACK_KINDS.find(([kind]) => !trackedKinds().has(kind));
  t.diagnostic(missing === undefined ? "every kind of the script is registered" : `first unregistered kind: ${missing[0]}`);
  if (missing === undefined) {
    assert.equal(value(outcomes.kitExplainer, "kit Explainer").durationInFrames, TOTAL);
  } else {
    rejectsWith(outcomes.kitExplainer, `kit render: ${missing[1]}: no component for kind "${missing[0]}"`);
  }
});

test("the real kit entry bundles and selects KitProbe with a custom target", () => {
  assert.equal(value(outcomes.kitProbe, "KitProbe").durationInFrames, 77);
});

test("Main over src/index.ts is unchanged at 150 frames", () => {
  assert.equal(value(outcomes.main, "Main").durationInFrames, 150);
});

test("the music input prop: absent, true and false select and return it; any other value is refused", () => {
  assert.equal(value(outcomes.explainer, "music absent").durationInFrames, TOTAL);
  assert.equal(value(outcomes.explainer, "music absent").props.music, undefined);
  assert.equal(value(outcomes.musicTrue, "music true").durationInFrames, TOTAL);
  assert.equal(value(outcomes.musicTrue, "music true").props.music, true);
  assert.equal(value(outcomes.musicFalse, "music false").durationInFrames, TOTAL);
  assert.equal(value(outcomes.musicFalse, "music false").props.music, false);
  rejectsWith(outcomes.musicYes, "kit render: input props: music must be true or false");
});
