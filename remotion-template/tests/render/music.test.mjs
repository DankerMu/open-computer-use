// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// Music in kit check and kit render in the workspace image (task 6.5,
// in-image evidence 1–5). Runs only inside the image, from a project root
// whose node_modules link reaches /opt/remotion-template/node_modules, as
// its own invocation with --cpus=2 --memory=4g, a tmpfs at
// /mnt/user-data/files, and the fixture library
// (tests/fixtures/music/library.mjs) bound read-only at /opt/video-music, so
// the CLI takes the production path:
//   node --test --test-concurrency=1 tests/render/music.test.mjs
// CI's tests/*.test.mjs glob does not match this file.
//
// The project is 5.1's passing project, built as tests/render/check.test.mjs
// builds it (3.8's fixture registry and Orbit, the pure fixture's script, a
// 7 s 440 Hz tone per block, and the project's own kit timeline), with
// music "fixture-long"; a copy with music "none" is the narration-only
// render. Levels are taken on the published MP4's decoded audio, one
// channel at 48 kHz, by projecting a window onto a sine and a cosine at
// exactly 1000 Hz (music) or 440 Hz (narration): both complete whole cycles
// in 400 ms, and 1000 Hz in 10 ms. The windows, spans and holds are worked
// out here from timeline.json and the fixture's envelope rules.

import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { copyFileSync, cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { blockStems } from "../../bin/voice-stem.mjs";
import { checkScript } from "../fixtures/check/fixture.mjs";

const TEMPLATE = fileURLToPath(new URL("../..", import.meta.url));
const FIXTURES = join(TEMPLATE, "tests", "render", "fixtures");
const LIBRARY = "/opt/video-music";
const FILES = "/mnt/user-data/files";
const TRACK = "fixture-long";
const BLOCK_SECONDS = 7;
const CHECK_LIMIT_MS = 900000;
const RENDER_LIMIT_MS = 1500000;
const RATE = 48000;
const R = 24000;
const WINDOW = 19200;
const SHORT_WINDOW = 480;
const STAGE_LINES = [
  "check: stage 1 schema",
  "check: stage 2 timeline",
  "check: stage 3 assets",
  "check: stage 4 unnarrated",
  "check: stage 5 rest",
  "check: stage 6 music",
  "check: stage 7 probe",
  "check: stage 8 contact-sheet",
  "check: stage 9 hash",
];

const pause = (ms) => new Promise((done) => setTimeout(done, ms));
const scratch = mkdtempSync(join(tmpdir(), "music-render-"));
const withMusic = join(scratch, "withmusic");
const noMusic = join(scratch, "nomusic");
const stats = (values) => ({ min: Math.min(...values).toFixed(2), max: Math.max(...values).toFixed(2), count: values.length });

const kit = (root, ...args) => {
  const run = spawnSync(process.execPath, ["bin/kit.mjs", ...args], { cwd: root, encoding: "utf8" });
  return { code: run.status, out: run.stdout, err: run.stderr };
};

const readLog = (root, job) => {
  try {
    return readFileSync(join(root, ".kit", job, "log"), "utf8");
  } catch {
    return ""; // before startJob has opened the log
  }
};

// Starts `job` and polls status until it stops running; every poll runs
// `during`.
async function runJob(root, job, limitMs, during = () => {}) {
  const started = kit(root, job);
  assert.equal(started.code, 0, started.err);
  const begun = Date.now();
  for (;;) {
    during();
    const status = kit(root, "status");
    if (status.code !== 3) return { ...status, ms: Date.now() - begun };
    assert.ok(Date.now() - begun < limitMs, `${job} still running after ${limitMs} ms\n${readLog(root, job)}`);
    await pause(500);
  }
}

// One channel of a file's audio, decoded to 48 kHz 16-bit.
function channel(path) {
  const raw = execFileSync("ffmpeg", ["-nostdin", "-v", "error", "-i", path, "-map", "0:a:0", "-ar", String(RATE), "-ac", "2", "-f", "s16le", "-"], {
    maxBuffer: 256 * 1024 * 1024,
  });
  const both = new Int16Array(raw.buffer, raw.byteOffset, raw.length / 2);
  const left = new Float64Array(both.length / 2);
  for (let k = 0; k < left.length; k++) left[k] = both[2 * k];
  return left;
}

// The level in dB (re full scale) of the tone at `hz` in [start, start +
// length): the RMS of the projection onto a sine and a cosine.
function band(samples, start, length, hz) {
  let c = 0;
  let s = 0;
  for (let n = 0; n < length; n++) {
    const phase = (2 * Math.PI * hz * n) / RATE;
    c += samples[start + n] * Math.cos(phase);
    s += samples[start + n] * Math.sin(phase);
  }
  const amplitude = (2 * Math.hypot(c, s)) / length;
  return 20 * Math.log10(amplitude / Math.SQRT2 / 32768);
}

// Blocks [startSample, endSample) in playback order, merged spans (gaps
// under 2R joined), and N.
function layoutOf(root) {
  const timeline = JSON.parse(readFileSync(join(root, "timeline.json"), "utf8"));
  const blocks = timeline.scenes.flatMap((scene) => scene.sentences.flatMap((sentence) => sentence.blocks.map((block) => [block.startSample, block.endSample])));
  const spans = [];
  for (const [a, b] of blocks) {
    const last = spans.at(-1);
    if (last !== undefined && a - last[1] < 2 * R) last[1] = b;
    else spans.push([a, b]);
  }
  return { totalFrames: timeline.totalFrames, total: timeline.totalFrames * 1600, blocks, spans };
}

// The 400 ms windows lying wholly inside one block, and wholly in a hold.
function windowsOf({ total, blocks, spans }) {
  const narrated = [];
  const holds = [];
  for (let start = 0; start + WINDOW <= total; start += WINDOW) {
    const end = start + WINDOW;
    if (blocks.some(([a, b]) => start >= a && end <= b)) narrated.push(start);
    const clear = spans.every(([a, b]) => end <= a - R || start >= b + R);
    if (clear && start >= R && end <= total - 2 * R) holds.push(start);
  }
  return { narrated, holds };
}

// The time (s) where the 10 ms levels from window `from` on first cross
// `level`, interpolated between window centres.
function crossing(levels, from, level, falling) {
  for (let j = from + 1; j < levels.length; j++) {
    const [p, q] = [levels[j - 1], levels[j]];
    if (falling ? p > level && q <= level : p < level && q >= level) {
      const centre = (k) => (k * SHORT_WINDOW + SHORT_WINDOW / 2) / RATE;
      return centre(j - 1) + ((level - p) / (q - p)) * (centre(j) - centre(j - 1));
    }
  }
  return null;
}

const mean = (values) => values.reduce((sum, value) => sum + value, 0) / values.length;

// 5.1's passing project at root, with `music`.
function buildProject(root, music) {
  mkdirSync(root);
  for (const part of ["src", "bin", join("tests", "render", "fixtures")]) cpSync(join(TEMPLATE, part), join(root, part), { recursive: true });
  for (const name of ["package.json", "tsconfig.json", "remotion.config.ts"]) copyFileSync(join(TEMPLATE, name), join(root, name));
  symlinkSync(realpathSync(join(TEMPLATE, "node_modules")), join(root, "node_modules"));
  copyFileSync(join(FIXTURES, "check", "registry.ts"), join(root, "src", "kit", "render", "registry.ts"));
  copyFileSync(join(FIXTURES, "explainer", "Orbit.tsx"), join(root, "src", "custom", "Orbit.tsx"));
  const script = checkScript((raw) => (raw.music = music));
  writeFileSync(join(root, "script.json"), `${JSON.stringify(script, null, 2)}\n`);
  mkdirSync(join(root, "public", "voice"), { recursive: true });
  for (const { stem } of blockStems(script)) {
    execFileSync("ffmpeg", [
      "-nostdin", "-v", "error",
      "-f", "lavfi", "-i", `sine=frequency=440:sample_rate=48000:duration=${BLOCK_SECONDS}`,
      "-ac", "1", "-c:a", "pcm_s16le",
      join(root, "public", "voice", `${stem}.wav`),
    ]);
  }
  const laid = kit(root, "timeline");
  assert.equal(laid.code, 0, laid.err);
}

let mixed;
let narrationOnly;
let layout;
let reference;

before(() => {
  assert.ok(existsSync(join(LIBRARY, "manifest.json")), `${LIBRARY}/manifest.json: the fixture library is not mounted`);
  buildProject(withMusic, TRACK);
  buildProject(noMusic, "none");
  layout = layoutOf(withMusic);
  const file = JSON.parse(readFileSync(join(LIBRARY, "manifest.json"), "utf8")).tracks.find((entry) => entry.id === TRACK).file;
  // The track's own 1000 Hz level over its first 10 s.
  reference = band(channel(join(LIBRARY, file)), 0, 10 * RATE, 1000);
});

after(() => rmSync(scratch, { recursive: true, force: true }));

test("1: the check passes with nine stage lines, and no public/music.wav exists during it", { timeout: CHECK_LIMIT_MS }, async (t) => {
  const stem = join(withMusic, "public", "music.wav");
  let seen = false;
  const done = await runJob(withMusic, "check", CHECK_LIMIT_MS, () => (seen ||= existsSync(stem)));
  t.diagnostic(`music check took ${done.ms} ms`);
  assert.equal(done.code, 0, `${done.out}\n${readLog(withMusic, "check")}`);
  const stages = readLog(withMusic, "check").split("\n").filter((line) => line.startsWith("check: stage "));
  t.diagnostic(`stage lines: ${stages.join(" | ")}`);
  assert.deepEqual(stages, STAGE_LINES);
  assert.equal(seen || existsSync(stem), false, "public/music.wav existed during the check");
});

test("2: the render succeeds, an H.264 yuv420p video with AAC audio and totalFrames frames", { timeout: RENDER_LIMIT_MS }, async (t) => {
  const done = await runJob(withMusic, "render", RENDER_LIMIT_MS);
  t.diagnostic(`music render took ${done.ms} ms`);
  const target = join(FILES, "withmusic.mp4");
  assert.deepEqual([done.code, done.out], [0, `kit status: render succeeded: ${target}\n`], readLog(withMusic, "render"));
  assert.ok(readLog(withMusic, "render").split("\n").includes("render: music"), readLog(withMusic, "render"));
  const probed = JSON.parse(
    execFileSync("ffprobe", ["-v", "error", "-show_entries", "stream=codec_type,codec_name,pix_fmt,nb_frames,duration,sample_rate,channels", "-of", "json", target], {
      encoding: "utf8",
    }),
  ).streams;
  t.diagnostic(`ffprobe: ${JSON.stringify(probed)}`);
  const video = probed.find((stream) => stream.codec_type === "video");
  assert.equal(video.codec_name, "h264");
  assert.equal(video.pix_fmt, "yuv420p");
  assert.equal(Number(video.nb_frames), layout.totalFrames);
  assert.ok(probed.some((stream) => stream.codec_type === "audio" && stream.codec_name === "aac"), "no AAC stream");
  mixed = channel(target);
});

test("3: narrated windows sit 15 ± 3 dB under the narration; holds 6 ± 2 dB under the track", (t) => {
  assert.ok(mixed, "the render did not run");
  const { narrated, holds } = windowsOf(layout);
  assert.ok(narrated.length > 0, "no narrated window");
  assert.ok(holds.length > 0, "no hold window");
  const ducked = narrated.map((start) => band(mixed, start, WINDOW, 440) - band(mixed, start, WINDOW, 1000));
  const held = holds.map((start) => band(mixed, start, WINDOW, 1000) - reference);
  t.diagnostic(`narration minus music (dB): ${JSON.stringify(stats(ducked))}`);
  t.diagnostic(`hold music minus the track's own level (dB): ${JSON.stringify(stats(held))}; track level ${reference.toFixed(2)} dBFS`);
  t.diagnostic(`hold windows start at samples ${holds.join(", ")}`);
  ducked.forEach((difference, k) => assert.ok(Math.abs(difference - 15) <= 3, `window at ${narrated[k]}: ${difference.toFixed(2)} dB`));
  held.forEach((difference, k) => assert.ok(Math.abs(difference + 6) <= 2, `hold at ${holds[k]}: ${difference.toFixed(2)} dB`));
});

test("4: the first ramp down and the last ramp up each last 0.5 ± 0.1 s", (t) => {
  assert.ok(mixed, "the render did not run");
  const levels = [];
  for (let start = 0; start + SHORT_WINDOW <= mixed.length; start += SHORT_WINDOW) levels.push(band(mixed, start, SHORT_WINDOW, 1000));
  const inside = (from, to) => levels.filter((_, j) => j * SHORT_WINDOW >= from && (j + 1) * SHORT_WINDOW <= to);
  const first = layout.blocks[0][0];
  const last = layout.blocks.at(-1)[1];
  // The hold before the first ramp down, [R, first − R), and after the last
  // ramp up, [last + R, N − 2R); the ducked level is 9 dB lower.
  const before = mean(inside(R, first - R));
  const after = mean(inside(last + R, layout.total - 2 * R));
  const downFrom = Math.floor((first - R) / SHORT_WINDOW) - 2;
  const down = [crossing(levels, downFrom, before - 0.9, true), crossing(levels, downFrom, before - 8.1, true)];
  const upFrom = Math.ceil(last / SHORT_WINDOW);
  const up = [crossing(levels, upFrom, after - 8.1, false), crossing(levels, upFrom, after - 0.9, false)];
  assert.ok(down.every((value) => value !== null) && up.every((value) => value !== null), JSON.stringify({ down, up }));
  const downSeconds = (down[1] - down[0]) / 0.8;
  const upSeconds = (up[1] - up[0]) / 0.8;
  t.diagnostic(`ramp down: hold ${before.toFixed(2)} dBFS, t10 ${down[0].toFixed(4)} s, t90 ${down[1].toFixed(4)} s, ${downSeconds.toFixed(4)} s (first block at ${first / RATE} s)`);
  t.diagnostic(`ramp up: hold ${after.toFixed(2)} dBFS, t10 ${up[0].toFixed(4)} s, t90 ${up[1].toFixed(4)} s, ${upSeconds.toFixed(4)} s (last block ends at ${last / RATE} s)`);
  assert.ok(Math.abs(downSeconds - 0.5) <= 0.1, `ramp down ${downSeconds} s`);
  assert.ok(Math.abs(upSeconds - 0.5) <= 0.1, `ramp up ${upSeconds} s`);
});

test("5: music none renders narration only: its 1000 Hz band is 40 dB or more under the 440 Hz band", { timeout: CHECK_LIMIT_MS + RENDER_LIMIT_MS }, async (t) => {
  const checked = await runJob(noMusic, "check", CHECK_LIMIT_MS);
  assert.equal(checked.code, 0, `${checked.out}\n${readLog(noMusic, "check")}`);
  const done = await runJob(noMusic, "render", RENDER_LIMIT_MS);
  const target = join(FILES, "nomusic.mp4");
  assert.deepEqual([done.code, done.out], [0, `kit status: render succeeded: ${target}\n`], readLog(noMusic, "render"));
  assert.ok(!readLog(noMusic, "render").includes("render: music"));
  assert.equal(existsSync(join(noMusic, "public", "music.wav")), false);
  const md5 = execFileSync("ffmpeg", ["-nostdin", "-v", "error", "-i", target, "-map", "0:a", "-c", "copy", "-f", "md5", "-"], { encoding: "utf8" }).trim();
  t.diagnostic(`none audio stream ${md5}`);
  const audio = channel(target);
  const { narrated } = windowsOf(layoutOf(noMusic));
  assert.ok(narrated.length > 0, "no narrated window");
  const separation = narrated.map((start) => band(audio, start, WINDOW, 440) - band(audio, start, WINDOW, 1000));
  t.diagnostic(`none: 440 Hz minus 1000 Hz (dB): ${JSON.stringify(stats(separation))}`);
  separation.forEach((difference, k) => assert.ok(difference >= 40, `window at ${narrated[k]}: ${difference.toFixed(2)} dB`));
});
