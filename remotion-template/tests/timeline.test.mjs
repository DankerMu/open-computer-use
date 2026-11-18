// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// `kit timeline` on fixture audio: sine tones of known length with 0.3 s of
// silence on each side, generated per test with ffmpeg lavfi into a
// temporary directory. Block frames are checked against public/narration.wav
// itself, never against the tones' nominal lengths. Exact lines, exit codes
// and the timeline.json shape come from the task 3.6 fixture. The tests need
// ffmpeg on PATH and fail, naming it, when it is absent.

import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  copyFileSync,
  cpSync,
  existsSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  readlinkSync,
  rmSync,
  symlinkSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { validateScript } from "../bin/script-schema.mjs";
import { blockStems } from "../bin/voice-stem.mjs";
import { layout } from "../bin/scene-timing.mjs";
import { run } from "../bin/kit.mjs";
import { lengthError, summaryLine } from "../bin/timeline.mjs";

const TEMPLATE = fileURLToPath(new URL("..", import.meta.url)).replace(/\/$/, "");
const RUN_HINT = "kit timeline: run node bin/kit.mjs voice, then voice-import\n";
// A voice file is read with the WAV demuxer only; ffmpeg 6.1.1 and 9.0.1
// give the same first line for each of these.
const ZERO_BYTE = "cannot be decoded: invalid start code [0][0][0][0] in RIFF header";
const CONCAT_LIST = "cannot be decoded: invalid start code ffco in RIFF header";
const FLAC_FILE = "cannot be decoded: invalid start code fLaC in RIFF header";
// -40 dBFS: well above the zeros between blocks, well below every tone.
const AUDIBLE = 328;
// Inside a tone the audible samples are never this far apart; between two
// trimmed blocks the silence is at least 0.06 s plus the kept 0.02 s ends.
const SPLIT = 2400;

// --- fixtures ---------------------------------------------------------------

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
      chapter("原理", "光为什么会拐弯", [["光会拐弯"]]),
      chapter("应用", "眼镜如何矫正", [["眼镜"]]),
    ],
    summary: { kind: "summary", narration: [["光在介质间会拐弯"]], content: { points: ["折射改变光路"] } },
    endCard: { kind: "end-card", content: { title: "谢谢观看" } },
  };
}

// Tone lengths in seconds for minimal()'s six blocks: about 63.6 s in total.
// The hook's block starts on frame 40 exactly; 9.02 s trims to about 434,865
// samples, ending 1,265 samples into a frame, so a floored endFrame would
// end before the tone does.
const IN_WINDOW = [9.02, 8, 7, 10, 9, 10];

function ffmpeg(args) {
  const result = spawnSync("ffmpeg", ["-nostdin", "-v", "error", "-y", ...args], { maxBuffer: 64 * 1024 * 1024 });
  assert.equal(result.error?.code, undefined, "ffmpeg: not found on PATH; the timeline tests need it");
  assert.equal(result.status, 0, String(result.stderr));
  return result.stdout;
}

function tone(path, seconds, { rate = 48000, channels = 1 } = {}) {
  const pad = `anullsrc=r=${rate}:cl=mono:d=0.3`;
  ffmpeg([
    "-filter_complex",
    `${pad}[a];sine=f=440:r=${rate}:d=${seconds}[b];${pad}[c];[a][b][c]concat=n=3:v=0:a=1`,
    "-ac",
    String(channels),
    "-c:a",
    "pcm_s16le",
    path,
  ]);
}

function silence(path, seconds) {
  ffmpeg(["-f", "lavfi", "-i", `anullsrc=r=48000:cl=mono:d=${seconds}`, "-c:a", "pcm_s16le", path]);
}

// Decoded 48 kHz mono samples of a file, by ffmpeg, not by the module.
function samplesOf(path) {
  const bytes = ffmpeg(["-i", path, "-f", "s16le", "-ac", "1", "-ar", "48000", "-"]);
  return new Int16Array(new Uint8Array(bytes).buffer);
}

function loudness(path) {
  const result = spawnSync("ffmpeg", ["-nostdin", "-hide_banner", "-nostats", "-i", path, "-af", "ebur128=peak=true", "-f", "null", "-"], {
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  const summary = result.stderr.slice(result.stderr.lastIndexOf("Summary:"));
  return {
    lufs: Number(summary.match(/\n\s+I:\s+(-?[0-9.]+) LUFS/)[1]),
    peak: Number(summary.match(/\n\s+Peak:\s+(-?[0-9.]+) dBFS/)[1]),
  };
}

// The fmt and data chunks of a RIFF/WAVE file.
function wavChunks(bytes) {
  assert.equal(bytes.toString("latin1", 0, 4), "RIFF");
  assert.equal(bytes.toString("latin1", 8, 12), "WAVE");
  assert.equal(bytes.readUInt32LE(4), bytes.length - 8);
  const chunks = {};
  for (let at = 12; at + 8 <= bytes.length; ) {
    const id = bytes.toString("latin1", at, at + 4);
    const size = bytes.readUInt32LE(at + 4);
    chunks[id] = bytes.subarray(at + 8, at + 8 + size);
    at += 8 + size + (size % 2);
  }
  return chunks;
}

// Runs of audible samples, split at silences longer than SPLIT.
function audibleRuns(samples) {
  const runs = [];
  let last = -Infinity;
  for (let i = 0; i < samples.length; i++) {
    if (Math.abs(samples[i]) <= AUDIBLE) continue;
    if (i - last > SPLIT) runs.push({ onset: i, offset: i });
    runs.at(-1).offset = i;
    last = i;
  }
  return runs;
}

function sandbox(t) {
  const base = mkdtempSync(join(tmpdir(), "kit-timeline-"));
  t.after(() => rmSync(base, { recursive: true, force: true }));
  const template = join(base, "template");
  cpSync(TEMPLATE, template, {
    recursive: true,
    filter: (src) => relative(TEMPLATE, src).split(sep)[0] !== "node_modules",
  });
  mkdirSync(join(template, "node_modules"));
  symlinkSync(join(TEMPLATE, "node_modules", "zod"), join(template, "node_modules", "zod"));
  const files = join(base, "files");
  mkdirSync(files);
  return { base, template, videos: join(base, "videos"), files };
}

async function kit(argv, root, box) {
  let out = "";
  let err = "";
  const code = await run(argv, {
    root,
    videosRoot: box.videos,
    filesRoot: box.files,
    stdout: { write: (text) => (out += text) },
    stderr: { write: (text) => (err += text) },
  });
  return { code, out, err };
}

// A project made by `new` with the given script and one tone per block
// (seconds[i] for the i-th block; null leaves that block without a file).
async function project(t, script = minimal(), seconds = [1, 1, 1, 1, 1, 1]) {
  const box = sandbox(t);
  const made = await kit(["new", "demo"], box.template, box);
  assert.equal(made.code, 0, made.err);
  const root = join(box.videos, "demo");
  writeFileSync(join(root, "script.json"), JSON.stringify(script));
  const stems = blockStems(validateScript(JSON.stringify(script)).script).map((entry) => entry.stem);
  const voice = join(root, "public", "voice");
  mkdirSync(voice, { recursive: true });
  const toneFiles = new Map();
  stems.forEach((stem, i) => {
    if (seconds[i] === null) return;
    const path = join(voice, `${stem}.wav`);
    // One generated file per length, copied for the rest.
    if (toneFiles.has(seconds[i])) copyFileSync(toneFiles.get(seconds[i]), path);
    else {
      tone(path, seconds[i]);
      toneFiles.set(seconds[i], path);
    }
  });
  const timeline = () => kit(["timeline"], root, box);
  return { ...box, root, stems, voice, timeline, wav: (stem) => join(voice, `${stem}.wav`) };
}

const sha256 = (data) => createHash("sha256").update(data).digest("hex");

// Every entry under dir: relative path, type, and content hash or link target.
function tree(dir) {
  const entries = [];
  const visit = (path) => {
    const stat = lstatSync(path);
    const rel = relative(dir, path);
    if (stat.isSymbolicLink()) entries.push([rel, "link", readlinkSync(path)]);
    else if (stat.isDirectory()) {
      entries.push([rel, "dir"]);
      for (const name of readdirSync(path).sort()) visit(join(path, name));
    } else entries.push([rel, "file", sha256(readFileSync(path))]);
  };
  visit(dir);
  return entries;
}

// Seeds an old timeline.json and narration.wav, runs timeline, and asserts
// exit 1, no stdout, the exact stderr, and an unchanged project (both old
// files byte-identical, no temporary file in the root or public/).
async function failsWith(box, expected) {
  writeFileSync(join(box.root, "timeline.json"), "old timeline\n");
  if (existsSync(join(box.root, "public")) && lstatSync(join(box.root, "public")).isDirectory()) {
    writeFileSync(join(box.root, "public", "narration.wav"), "old narration");
  }
  const before = tree(box.root);
  const listing = readdirSync(box.root).sort();
  const result = await box.timeline();
  assert.equal(result.code, 1, result.err);
  assert.equal(result.out, "");
  if (expected instanceof RegExp) assert.match(result.err, expected);
  else assert.equal(result.err, expected);
  assert.deepEqual(readdirSync(box.root).sort(), listing);
  assert.deepEqual(tree(box.root), before);
  return result;
}

// --- success ----------------------------------------------------------------

test("timeline places every block on its audio and writes a -16 LUFS narration of the timeline's length", async (t) => {
  const box = await project(t, minimal(), IN_WINDOW);
  const result = await box.timeline();
  assert.equal(result.code, 0, result.err);
  assert.equal(result.out, "");
  const timeline = JSON.parse(readFileSync(join(box.root, "timeline.json"), "utf8"));
  const narration = readFileSync(join(box.root, "public", "narration.wav"));

  const summary = result.err.match(/^kit timeline: 6 blocks, 6 scenes, (\d+\.\d) s \((\d+) frames\), (-?\d+\.\d) LUFS\n$/);
  assert.ok(summary, result.err);
  assert.equal(Number(summary[2]), timeline.totalFrames);
  assert.equal(summary[1], (timeline.totalFrames / 30).toFixed(1));
  assert.equal(summary[3], timeline.narration.integratedLufs.toFixed(1));

  // The narration track: 48 kHz, 16-bit, mono, exactly totalFrames × 1600 samples.
  const chunks = wavChunks(narration);
  assert.equal(chunks["fmt "].readUInt16LE(0), 1);
  assert.equal(chunks["fmt "].readUInt16LE(2), 1);
  assert.equal(chunks["fmt "].readUInt32LE(4), 48000);
  assert.equal(chunks["fmt "].readUInt32LE(8), 96000);
  assert.equal(chunks["fmt "].readUInt16LE(12), 2);
  assert.equal(chunks["fmt "].readUInt16LE(14), 16);
  assert.equal(chunks.data.length, timeline.totalFrames * 1600 * 2);
  const samples = samplesOf(join(box.root, "public", "narration.wav"));
  assert.equal(samples.length, timeline.totalFrames * 1600);
  const measured = loudness(join(box.root, "public", "narration.wav"));
  assert.ok(Math.abs(measured.lufs + 16) <= 1, `${measured.lufs} LUFS`);
  assert.ok(measured.peak <= -1.0, `${measured.peak} dBTP`);

  // Every block's frames against the tone found in the narration itself.
  const blocks = timeline.scenes.flatMap((scene) => scene.sentences.flatMap((sentence) => sentence.blocks));
  const runs = audibleRuns(samples);
  assert.equal(runs.length, blocks.length);
  assert.ok(
    blocks.some((block) => block.endSample % 1600 > 1000),
    "the fixture needs a block ending late in a frame, so that a floored endFrame is caught",
  );
  blocks.forEach((block, i) => {
    const { onset, offset } = runs[i];
    const on = Math.floor(onset / 1600);
    const off = Math.ceil(offset / 1600);
    assert.ok(block.startFrame <= on && on <= block.startFrame + 1, `${block.stem}: onset frame ${on}, startFrame ${block.startFrame}`);
    assert.ok(block.endFrame - 1 <= off && off <= block.endFrame, `${block.stem}: offset frame ${off}, endFrame ${block.endFrame}`);
  });

  // Transitions sit after the narration, inside their scene; the end card
  // without narration lasts 90 frames.
  for (const scene of timeline.scenes.slice(0, -1)) {
    assert.ok(scene.narrationEndFrame <= scene.transitionOut.startFrame);
    assert.equal(scene.transitionOut.startFrame + 15, scene.endFrame);
    const last = scene.sentences.at(-1).blocks.at(-1);
    assert.ok(runs[blocks.indexOf(last)].offset < scene.transitionOut.startFrame * 1600, `scene ${scene.index}`);
  }
  const card = timeline.scenes.at(-1);
  assert.equal(card.endFrame - card.startFrame, 90);
  assert.equal(card.transitionOut, null);
  assert.equal(timeline.totalFrames, card.endFrame);
});

test("timeline.json matches the version-1 shape exactly, and a re-run is byte-identical", async (t) => {
  const box = await project(t, minimal(), IN_WINDOW);
  assert.equal((await box.timeline()).code, 0);
  const text = readFileSync(join(box.root, "timeline.json"), "utf8");
  assert.ok(text.endsWith("}\n"));
  const timeline = JSON.parse(text);
  const narration = readFileSync(join(box.root, "public", "narration.wav"));
  assert.deepEqual(Object.keys(timeline), ["version", "fps", "sampleRate", "totalFrames", "narration", "scenes"]);
  assert.deepEqual([timeline.version, timeline.fps, timeline.sampleRate], [1, 30, 48000]);
  assert.ok(Number.isInteger(timeline.totalFrames));
  assert.deepEqual(Object.keys(timeline.narration), ["file", "sha256", "integratedLufs", "truePeakDbtp"]);
  assert.equal(timeline.narration.file, "public/narration.wav");
  assert.equal(timeline.narration.sha256, sha256(narration));
  for (const value of [timeline.narration.integratedLufs, timeline.narration.truePeakDbtp]) {
    assert.equal(typeof value, "number");
    assert.equal(Math.round(value * 10) / 10, value);
  }
  const SCENE_KEYS = ["index", "slot", "chapter", "scene", "kind", "startFrame", "endFrame", "narrationEndFrame", "minFrames", "transitionOut", "sentences"];
  const BLOCK_KEYS = ["stem", "text", "startSample", "endSample", "startFrame", "endFrame", "audioSha256"];
  assert.deepEqual(
    timeline.scenes.map(({ index, slot, chapter, scene, kind, minFrames }) => [index, slot, chapter, scene, kind, minFrames]),
    [
      [1, "hook", null, null, "hook", 60],
      [2, "chapter", 1, 1, "question", 60],
      [3, "chapter", 2, 1, "question", 60],
      [4, "chapter", 3, 1, "question", 60],
      [5, "summary", null, null, "summary", 60],
      [6, "end-card", null, null, "end-card", 90],
    ],
  );
  const stems = blockStems(validateScript(JSON.stringify(minimal())).script);
  const listed = [];
  for (const scene of timeline.scenes) {
    assert.deepEqual(Object.keys(scene), SCENE_KEYS);
    for (const key of ["startFrame", "endFrame", "narrationEndFrame"]) assert.ok(Number.isInteger(scene[key]), key);
    if (scene.transitionOut !== null) assert.deepEqual(Object.keys(scene.transitionOut), ["startFrame", "frames"]);
    for (const sentence of scene.sentences) {
      assert.deepEqual(Object.keys(sentence), ["blocks"]);
      for (const block of sentence.blocks) {
        assert.deepEqual(Object.keys(block), BLOCK_KEYS);
        assert.ok(block.stem.startsWith(`s${scene.index}-`), block.stem);
        assert.equal(block.audioSha256, sha256(readFileSync(box.wav(block.stem))));
        listed.push([block.stem, block.text]);
      }
    }
  }
  assert.deepEqual(listed, stems.map(({ stem, text }) => [stem, text]));
  assert.deepEqual(timeline.scenes.at(-1).sentences, []);

  // The command's timeline is layout() over the decoded lengths, plus the
  // narration record and the hashes.
  const s = validateScript(JSON.stringify(minimal())).script;
  const decoded = timeline.scenes.flatMap((scene) => scene.sentences.flatMap((sentence) => sentence.blocks));
  const laid = layout(s, stems.map((entry, i) => ({ ...entry, samples: decoded[i].endSample - decoded[i].startSample })));
  const stripped = structuredClone(timeline);
  delete stripped.narration;
  for (const scene of stripped.scenes) for (const sentence of scene.sentences) for (const block of sentence.blocks) delete block.audioSha256;
  assert.deepEqual(stripped, laid);

  const listing = [readdirSync(box.root).sort(), readdirSync(join(box.root, "public")).sort()];
  assert.equal((await box.timeline()).code, 0);
  assert.equal(readFileSync(join(box.root, "timeline.json"), "utf8"), text);
  assert.deepEqual(readFileSync(join(box.root, "public", "narration.wav")), narration);
  assert.deepEqual([readdirSync(box.root).sort(), readdirSync(join(box.root, "public")).sort()], listing);
});

// --- audio problems ----------------------------------------------------------

test("a missing block fails, naming it, and writes nothing", async (t) => {
  const box = await project(t, minimal(), [1, 1, null, 1, 1, 1]);
  await failsWith(box, `kit timeline: chapter 1 现象, scene 1 (question), sentence 1 block 2: missing ${box.stems[2]}.wav\n${RUN_HINT}`);
});

test("every audio problem is listed in playback order", async (t) => {
  const box = await project(t, minimal(), [null, 1, 1, 1, 1, 1]);
  writeFileSync(box.wav(box.stems[3]), "");
  unlinkSync(box.wav(box.stems[4]));
  silence(box.wav(box.stems[4]), 1);
  unlinkSync(box.wav(box.stems[5]));
  await failsWith(
    box,
    `kit timeline: hook, sentence 1 block 1: missing ${box.stems[0]}.wav\n` +
      `kit timeline: chapter 2 原理, scene 1 (question), sentence 1 block 1: ${ZERO_BYTE}\n` +
      "kit timeline: chapter 3 应用, scene 1 (question), sentence 1 block 1: silent after trimming\n" +
      `kit timeline: summary, sentence 1 block 1: missing ${box.stems[5]}.wav\n` +
      RUN_HINT,
  );
});

test("an edited block with its old file present is stale, naming the new stem", async (t) => {
  const box = await project(t);
  const edited = minimal();
  edited.summary.narration = [["光在介质间拐弯"]];
  writeFileSync(join(box.root, "script.json"), JSON.stringify(edited));
  const stem = blockStems(validateScript(JSON.stringify(edited)).script)[5].stem;
  assert.notEqual(stem, box.stems[5]);
  assert.equal(stem.slice(0, 7), box.stems[5].slice(0, 7));
  await failsWith(box, `kit timeline: summary, sentence 1 block 1: stale: its text, voice or instructions changed, needs ${stem}.wav\n${RUN_HINT}`);
});

test("an all-silent block fails as silent after trimming", async (t) => {
  const box = await project(t);
  unlinkSync(box.wav(box.stems[0]));
  silence(box.wav(box.stems[0]), 2);
  await failsWith(box, `kit timeline: hook, sentence 1 block 1: silent after trimming\n${RUN_HINT}`);
});

test("a 0-byte voice file cannot be decoded, with ffmpeg's reason", async (t) => {
  const box = await project(t);
  writeFileSync(box.wav(box.stems[1]), "");
  await failsWith(box, `kit timeline: chapter 1 现象, scene 1 (question), sentence 1 block 1: ${ZERO_BYTE}\n${RUN_HINT}`);
});

test("a concat list or a FLAC file under a .wav name cannot be decoded", async (t) => {
  const box = await project(t);
  copyFileSync(box.wav(box.stems[1]), join(box.voice, "seg.wav"));
  writeFileSync(box.wav(box.stems[0]), "ffconcat version 1.0\nfile seg.wav\n");
  unlinkSync(box.wav(box.stems[3]));
  ffmpeg(["-i", box.wav(box.stems[1]), "-f", "flac", box.wav(box.stems[3])]);
  await failsWith(
    box,
    `kit timeline: hook, sentence 1 block 1: ${CONCAT_LIST}\n` +
      `kit timeline: chapter 2 原理, scene 1 (question), sentence 1 block 1: ${FLAC_FILE}\n` +
      RUN_HINT,
  );
});

test("a 24 kHz stereo voice file decodes to its 48 kHz mono trimmed length", async (t) => {
  const box = await project(t, minimal(), IN_WINDOW);
  unlinkSync(box.wav(box.stems[0]));
  tone(box.wav(box.stems[0]), 9.02, { rate: 24000, channels: 2 });
  const result = await box.timeline();
  assert.equal(result.code, 0, result.err);
  const hook = JSON.parse(readFileSync(join(box.root, "timeline.json"), "utf8")).scenes[0].sentences[0].blocks[0];
  // 9.02 s of tone at 48 kHz, plus at most 0.02 s kept at each end and a
  // little resampling slack.
  const samples = hook.endSample - hook.startSample;
  assert.ok(samples >= 432960 && samples <= 432960 + 1920 + 100, `${samples} samples`);
});

test("a symlinked voice file or a directory at a voice name counts as missing", async (t) => {
  const box = await project(t);
  const outside = join(box.base, "outside.wav");
  copyFileSync(box.wav(box.stems[0]), outside);
  unlinkSync(box.wav(box.stems[0]));
  symlinkSync(outside, box.wav(box.stems[0]));
  unlinkSync(box.wav(box.stems[3]));
  mkdirSync(box.wav(box.stems[3]));
  await failsWith(
    box,
    `kit timeline: hook, sentence 1 block 1: missing ${box.stems[0]}.wav\n` +
      `kit timeline: chapter 2 原理, scene 1 (question), sentence 1 block 1: missing ${box.stems[3]}.wav\n` +
      RUN_HINT,
  );
});

test("a public/voice symlink or a public file fails; a missing public/voice reports every block", async (t) => {
  const box = await project(t);
  const elsewhere = join(box.base, "elsewhere");
  cpSync(box.voice, elsewhere, { recursive: true });
  rmSync(box.voice, { recursive: true });
  symlinkSync(elsewhere, box.voice);
  await failsWith(box, `kit timeline: ${box.voice}: is a symlink\n`);

  rmSync(join(box.root, "public"), { recursive: true });
  writeFileSync(join(box.root, "public"), "x");
  await failsWith(box, `kit timeline: ${join(box.root, "public")}: is not a directory\n`);

  unlinkSync(join(box.root, "public"));
  const missing = [
    ["hook", 0, 0],
    ["chapter 1 现象, scene 1 (question)", 0, 0],
    ["chapter 1 现象, scene 1 (question)", 0, 1],
    ["chapter 2 原理, scene 1 (question)", 0, 0],
    ["chapter 3 应用, scene 1 (question)", 0, 0],
    ["summary", 0, 0],
  ].map(([where, i, j], k) => `kit timeline: ${where}, sentence ${i + 1} block ${j + 1}: missing ${box.stems[k]}.wav\n`);
  await failsWith(box, `${missing.join("")}${RUN_HINT}`);
  // public/ was checked, never created.
  assert.equal(existsSync(join(box.root, "public")), false);
});

// --- the length window -----------------------------------------------------------

test("a script of about 200 s fails, stating its length rounded up", async (t) => {
  const box = await project(t, minimal(), [31.6, 31.6, 31.6, 31.6, 31.6, 31.6]);
  const { err } = await failsWith(box, /^kit timeline: total 200\.\d s \(\d+ frames\) is outside 60–180 s\n$/);
  const [, seconds, frames] = err.match(/total (\S+) s \((\d+) frames\)/);
  assert.equal(seconds, (Math.ceil(Number(frames) / 3) / 10).toFixed(1));
});

test("a script under 60 s fails, stating its length rounded down", async (t) => {
  const box = await project(t);
  const { err } = await failsWith(box, /^kit timeline: total \d+\.\d s \(\d+ frames\) is outside 60–180 s\n$/);
  const [, seconds, frames] = err.match(/total (\S+) s \((\d+) frames\)/);
  assert.ok(Number(frames) < 1800);
  assert.equal(seconds, (Math.floor(Number(frames) / 3) / 10).toFixed(1));
});

// --- ffmpeg and the CLI ------------------------------------------------------------

// Runs `process.execPath bin/kit.mjs timeline` in the project with the
// given PATH and asserts exit 1, no stdout, the exact stderr, and an
// unchanged project: same listings, timeline.json and narration.wav
// byte-identical, no temporary file left.
function childFailsWith(box, path, expected) {
  const before = tree(box.root);
  const result = spawnSync(process.execPath, [join(box.root, "bin", "kit.mjs"), "timeline"], {
    cwd: box.root,
    env: { PATH: path },
    encoding: "utf8",
  });
  assert.equal(result.status, 1, result.stderr);
  assert.equal(result.stdout, "");
  assert.equal(result.stderr, expected);
  assert.deepEqual(tree(box.root), before);
}

test("ffmpeg absent from PATH fails, naming it", async (t) => {
  const box = await project(t);
  const empty = join(box.base, "empty-path");
  mkdirSync(empty);
  writeFileSync(join(box.root, "timeline.json"), "old timeline\n");
  writeFileSync(join(box.root, "public", "narration.wav"), "old narration");
  childFailsWith(box, empty, "kit timeline: ffmpeg: not found on PATH\n");
});

// A directory holding an `ffmpeg` that runs `sabotage` when its arguments
// contain `match` and the real ffmpeg otherwise; the kit is not changed.
function fakeFfmpeg(box, match, sabotage) {
  const found = spawnSync("sh", ["-c", "command -v ffmpeg"], { encoding: "utf8" });
  const real = found.stdout.trim();
  assert.ok(real.startsWith("/"), "ffmpeg: not found on PATH; the timeline tests need it");
  const dir = join(box.base, "fake-bin");
  mkdirSync(dir);
  const script = `#!/bin/sh\ncase "$*" in\n  *${match}*) ${sabotage.replaceAll("REAL", `'${real}'`)} ;;\nesac\nexec '${real}' "$@"\n`;
  writeFileSync(join(dir, "ffmpeg"), script, { mode: 0o755 });
  return `${dir}:${process.env.PATH}`;
}

// A project whose first timeline run succeeded (giving totalFrames), with
// timeline.json and narration.wav then replaced by old bytes that a re-run
// cannot reproduce, so a replacement shows.
async function timedProject(t) {
  const box = await project(t, minimal(), IN_WINDOW);
  const first = await box.timeline();
  assert.equal(first.code, 0, first.err);
  const { totalFrames } = JSON.parse(readFileSync(join(box.root, "timeline.json"), "utf8"));
  writeFileSync(join(box.root, "timeline.json"), "old timeline\n");
  writeFileSync(join(box.root, "public", "narration.wav"), "old narration");
  return { ...box, totalFrames };
}

test("a narration failing the loudness gate fails and replaces nothing", async (t) => {
  const box = await timedProject(t);
  const summary =
    "[Parsed_ebur128_0 @ 0x1] Summary:\\n\\n  Integrated loudness:\\n    I:         -20.0 LUFS\\n" +
    "    Threshold: -30.0 LUFS\\n\\n  True peak:\\n    Peak:       -5.0 dBFS\\n";
  const path = fakeFfmpeg(box, "ebur128=peak=true", `printf '${summary}' >&2; exit 0`);
  childFailsWith(
    box,
    path,
    "kit timeline: narration measures -20.0 LUFS, -5.0 dBTP; needs -16 ± 1 LUFS and at most -1.0 dBTP\n",
  );
});

test("a normalized narration of the wrong length fails and replaces nothing", async (t) => {
  const box = await timedProject(t);
  const path = fakeFfmpeg(box, "linear=true", "REAL \"$@\" | head -c 32000; exit 0");
  childFailsWith(box, path, `kit timeline: narration has 16000 samples, expected ${box.totalFrames * 1600}\n`);
});

test("a narration whose true peak exceeds -1.0 dBTP fails the gate and replaces nothing", async (t) => {
  const box = await timedProject(t);
  const summary =
    "[Parsed_ebur128_0 @ 0x1] Summary:\\n\\n  Integrated loudness:\\n    I:         -16.0 LUFS\\n" +
    "    Threshold: -26.0 LUFS\\n\\n  True peak:\\n    Peak:       -0.5 dBFS\\n";
  const path = fakeFfmpeg(box, "ebur128=peak=true", `printf '${summary}' >&2; exit 0`);
  childFailsWith(
    box,
    path,
    "kit timeline: narration measures -16.0 LUFS, -0.5 dBTP; needs -16 ± 1 LUFS and at most -1.0 dBTP\n",
  );
});

test("an ffmpeg killed by a signal while decoding is named by its signal", async (t) => {
  const box = await project(t);
  writeFileSync(join(box.root, "timeline.json"), "old timeline\n");
  writeFileSync(join(box.root, "public", "narration.wav"), "old narration");
  const path = fakeFfmpeg(box, box.stems[0], "kill -KILL $$");
  childFailsWith(box, path, `kit timeline: hook, sentence 1 block 1: cannot be decoded: ffmpeg exited with SIGKILL\n${RUN_HINT}`);
});

test("the summary line gives seconds and LUFS to one decimal", () => {
  assert.equal(summaryLine(26, 8, 2030, -16), "26 blocks, 8 scenes, 67.7 s (2030 frames), -16.0 LUFS");
  assert.equal(summaryLine(6, 6, 1908, -15.96), "6 blocks, 6 scenes, 63.6 s (1908 frames), -16.0 LUFS");
});

test("the length window takes 1800 and 5400 frames and names 1799 and 5401, rounded outward", () => {
  assert.equal(lengthError(1800), null);
  assert.equal(lengthError(5400), null);
  assert.equal(lengthError(1799), "total 59.9 s (1799 frames) is outside 60–180 s");
  assert.equal(lengthError(5401), "total 180.1 s (5401 frames) is outside 60–180 s");
});

test("timeline refuses at the template root with the hint, and takes no argument", async (t) => {
  const box = sandbox(t);
  const before = tree(box.base);
  const result = await kit(["timeline"], box.template, box);
  assert.equal(result.code, 2);
  assert.equal(result.out, "");
  assert.equal(result.err, `kit timeline: run it inside a project: cd ${box.videos}/<name> && node bin/kit.mjs timeline\n`);
  assert.deepEqual(tree(box.base), before);
  const proj = await project(t);
  const extra = await kit(["timeline", "x"], proj.root, proj);
  assert.equal(extra.code, 2);
  assert.equal(extra.err, "kit timeline: usage: node bin/kit.mjs timeline\n");
});

test("an invalid script prints 3.5's header and indented messages and writes nothing", async (t) => {
  const box = await project(t);
  const script = minimal();
  script.chapters[1].scenes[0].narration[0][0] = "一二三四五六七八九十一二三四五六七";
  writeFileSync(join(box.root, "script.json"), JSON.stringify(script));
  await failsWith(
    box,
    "kit timeline: script.json is invalid\n" +
      "  chapter 2 原理, scene 1 (question): narration[0][0] is too long (limit 16 characters)\n",
  );
});
