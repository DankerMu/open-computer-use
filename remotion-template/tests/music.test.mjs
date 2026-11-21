// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The music library, the music check rules, the ducking envelope and the
// stem (task 6.5, bin/music.mjs), pure. Libraries are temporary directories:
// hand-written manifests for the rules, and the generated fixture library
// (tests/fixtures/music/library.mjs, ffmpeg at test time) for the stem.
// Expected messages are the fixture's exact texts; expected levels come from
// the fixture's envelope (R = 24,000 samples, -15 dB inside a merged span,
// -6 dB in a hold, linear-in-dB ramps of R, a linear fade-in over [0, R)
// and fade-out over [N - 2R, N)), worked out here, never from the module.
// The module is imported inside each test, so a missing one fails each test
// on its own.

import { before, after, test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import { chmodSync, closeSync, constants, mkdirSync, mkdtempSync, openSync, readdirSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { LOOP_SAMPLES, makeLibrary } from "./fixtures/music/library.mjs";

const music = () => import("../bin/music.mjs");

const R = 24000;
const NO_LIBRARY = 'this image has no music library; set "music" to "none"';
const BAD = "the music library manifest cannot be read: ";

// A temporary library: manifest.json holds `manifest` (a string as is, else
// its JSON); files maps names to contents.
function library(t, manifest, files = {}) {
  const dir = mkdtempSync(join(tmpdir(), "music-test-"));
  t.after(() => {
    chmodSync(dir, 0o755);
    rmSync(dir, { recursive: true, force: true });
  });
  if (manifest !== undefined) writeFileSync(join(dir, "manifest.json"), typeof manifest === "string" ? manifest : JSON.stringify(manifest));
  for (const [name, data] of Object.entries(files)) writeFileSync(join(dir, name), data);
  return dir;
}

const track = (id, { loop = false, file = `${id}.mp3`, duration = 200 } = {}) => ({ id, mood: "calm-curious", loop, file, duration_seconds: duration, title: "t" });
const manifestOf = (...tracks) => ({ version: 1, approvals: {}, tracks });
// A library holding every track's file.
const withFiles = (t, ...tracks) => library(t, manifestOf(...tracks), Object.fromEntries(tracks.map((entry) => [entry.file, "audio"])));

async function refusal(dir) {
  const { readLibrary } = await music();
  try {
    await readLibrary(dir);
  } catch (thrown) {
    assert.equal(thrown.constructor.name, "Refusal");
    return thrown.message;
  }
  assert.fail("readLibrary resolved");
}

// --- readLibrary ------------------------------------------------------------------

test("MUSIC_DIR is /opt/video-music", async () => {
  assert.equal((await music()).MUSIC_DIR, "/opt/video-music");
});

test("readLibrary: a good manifest resolves its tracks in manifest order, other fields as they are", async (t) => {
  const { readLibrary } = await music();
  const tracks = [track("zeta", { loop: true, file: "zeta.wav", duration: 2.01 }), track("alpha"), track("mid-1", { duration: 0.5 })];
  const dir = library(t, { version: 1, approvals: { file: "approvals.json" }, extra: true, tracks });
  assert.deepEqual(await readLibrary(dir), { tracks });
});

test("readLibrary: no manifest.json, or no directory, is the no-library refusal", async (t) => {
  assert.equal(await refusal(library(t)), NO_LIBRARY);
  assert.equal(await refusal(join(library(t), "absent")), NO_LIBRARY);
});

test("readLibrary: an unparsable manifest gives JSON.parse's message", async (t) => {
  let message;
  try {
    JSON.parse("{");
  } catch (thrown) {
    message = thrown.message;
  }
  assert.equal(await refusal(library(t, "{")), `${BAD}${message}`);
});

test("readLibrary: a manifest that cannot be read gives the read error", async (t) => {
  const dir = library(t);
  mkdirSync(join(dir, "manifest.json"));
  assert.equal(await refusal(dir), `${BAD}EISDIR: illegal operation on a directory, read`);
});

test("readLibrary: version 2, a file of ../x.mp3, a non-boolean loop, and every other bad entry", async (t) => {
  const cases = [
    [{ version: 2, tracks: [] }, "version is 2, expected 1"],
    [[1], "the manifest is not a JSON object"],
    [{ version: 1 }, "tracks is not a list"],
    [manifestOf(track("ok"), track("up", { file: "../x.mp3" })), 'tracks[1].file is "../x.mp3", not a plain .mp3 or .wav name'],
    [manifestOf({ ...track("ok"), loop: "yes" }), 'tracks[0].loop is "yes", not true or false'],
    [manifestOf(track("ok", { file: "a.ogg" })), 'tracks[0].file is "a.ogg", not a plain .mp3 or .wav name'],
    [manifestOf(track("ok", { file: "sub/a.mp3" })), 'tracks[0].file is "sub/a.mp3", not a plain .mp3 or .wav name'],
    [manifestOf(track("Bad_Id")), 'tracks[0].id is "Bad_Id", not a track id'],
    [manifestOf(track("ok", { duration: 0 })), "tracks[0].duration_seconds is 0, not a positive number"],
    [manifestOf(track("ok", { duration: "60" })), 'tracks[0].duration_seconds is "60", not a positive number'],
    [manifestOf("ok"), 'tracks[0] is "ok", not an object'],
    [manifestOf({ ...track("ok"), loop: undefined }), "tracks[0].loop is absent, not true or false"],
  ];
  for (const [manifest, reason] of cases) assert.equal(await refusal(library(t, manifest)), `${BAD}${reason}`, reason);
});

test("readLibrary: the reason is escaped and at most 300 code points", async (t) => {
  const file = `\u2028${"é".repeat(400)}`;
  const message = await refusal(library(t, manifestOf(track("ok", { file }))));
  assert.ok(message.startsWith(BAD), message);
  const reason = message.slice(BAD.length);
  assert.equal([...reason].length, 300);
  assert.ok(reason.startsWith('tracks[0].file is "\\u2028éé'), reason.slice(0, 40));
  assert.ok(!/[\u2028\n]/.test(reason));
});

// --- musicReasons -----------------------------------------------------------------

const script = (id) => ({ music: id });
const frames = (totalFrames) => ({ totalFrames });

test("musicReasons: none resolves [] without reading anything, even for a missing directory", async (t) => {
  const { musicReasons } = await music();
  assert.deepEqual(await musicReasons(script("none"), frames(1991), join(library(t), "absent")), []);
  assert.deepEqual(await musicReasons(script("none"), frames(1991), library(t, "{")), []);
});

test("musicReasons: a library refusal is its text, prefixed music:", async (t) => {
  const { musicReasons } = await music();
  assert.deepEqual(await musicReasons(script("song"), frames(1991), join(library(t), "absent")), [`music: ${NO_LIBRARY}`]);
  assert.deepEqual(await musicReasons(script("song"), frames(1991), library(t, { version: 2 })), [`music: ${BAD}version is 2, expected 1`]);
});

test("musicReasons: an unknown id lists the ids in manifest order", async (t) => {
  const { musicReasons } = await music();
  const dir = withFiles(t, track("zeta"), track("alpha", { loop: true, file: "alpha.wav" }), track("mid"));
  assert.deepEqual(await musicReasons(script("nope"), frames(1991), dir), ['music: unknown track "nope"; choose "none" or one of: zeta, alpha, mid']);
});

test("musicReasons: a symlinked track file is not a regular file, a missing one ENOENT", async (t) => {
  const { musicReasons } = await music();
  const dir = withFiles(t, track("real"), track("linked"), track("gone"));
  rmSync(join(dir, "linked.mp3"));
  symlinkSync(join(dir, "real.mp3"), join(dir, "linked.mp3"));
  rmSync(join(dir, "gone.mp3"));
  assert.deepEqual(await musicReasons(script("real"), frames(1991), dir), []);
  assert.deepEqual(await musicReasons(script("linked"), frames(1991), dir), ['music: the library file for "linked" cannot be read: not a regular file']);
  assert.deepEqual(await musicReasons(script("gone"), frames(1991), dir), ['music: the library file for "gone" cannot be read: ENOENT']);
  mkdirSync(join(dir, "gone.mp3"));
  assert.deepEqual(await musicReasons(script("gone"), frames(1991), dir), ['music: the library file for "gone" cannot be read: not a regular file']);
});

test("musicReasons: a track file that cannot be opened gives the open error's code", { skip: process.getuid?.() === 0 && "root opens a mode-000 file" }, async (t) => {
  const { musicReasons } = await music();
  const dir = withFiles(t, track("locked"));
  chmodSync(join(dir, "locked.mp3"), 0o000);
  assert.deepEqual(await musicReasons(script("locked"), frames(1991), dir), ['music: the library file for "locked" cannot be read: EACCES']);
});

test("musicReasons: a non-loop track exactly the video's length passes, one frame shorter fails naming both lengths", async (t) => {
  const { musicReasons } = await music();
  // d floored and v ceiled to 0.1 s: 1922/30 = 64.066… → 64.0, 1923/30 =
  // 64.1; 1990/30 = 66.333… → 66.3, 1991/30 = 66.366… → 66.4.
  for (const [total, d, v] of [
    [1923, "64.0", "64.1"],
    [1991, "66.3", "66.4"],
  ]) {
    const equal = withFiles(t, track("song", { duration: total / 30 }), track("loop-b", { loop: true, file: "loop-b.wav" }), track("loop-a", { loop: true, file: "loop-a.wav" }));
    assert.deepEqual(await musicReasons(script("song"), frames(total), equal), [], `${total}: equal lengths pass`);
    const short = withFiles(t, track("song", { duration: (total - 1) / 30 }), track("loop-b", { loop: true, file: "loop-b.wav" }), track("loop-a", { loop: true, file: "loop-a.wav" }));
    assert.deepEqual(await musicReasons(script("song"), frames(total), short), [
      `music: track "song" lasts ${d} s, shorter than the video's ${v} s; choose a loop track (loop-b, loop-a) or a track at least ${v} s long`,
    ]);
  }
});

test("musicReasons: without a loop track the short-track reason suggests a longer track or none", async (t) => {
  const { musicReasons } = await music();
  const dir = withFiles(t, track("song", { duration: 30 }), track("other"));
  assert.deepEqual(await musicReasons(script("song"), frames(1991), dir), [
    'music: track "song" lasts 30.0 s, shorter than the video\'s 66.4 s; choose a track at least 66.4 s long, or "none"',
  ]);
});

test("musicReasons: a loop track shorter than the video passes", async (t) => {
  const { musicReasons } = await music();
  const dir = withFiles(t, track("bar", { loop: true, file: "bar.wav", duration: 2.01 }));
  assert.deepEqual(await musicReasons(script("bar"), frames(5400), dir), []);
});

// --- the envelope -----------------------------------------------------------------

// A timeline of one scene and sentence holding blocks [startSample,
// endSample), as musicWindows reads it.
const timelineOf = (totalFrames, blocks) => ({
  totalFrames,
  scenes: [{ sentences: [{ blocks: blocks.map(([startSample, endSample]) => ({ startSample, endSample })) }] }],
});

// N = 480,000. Blocks A–C are one span [64000, 200000): a 2880-sample block
// gap and a 10-frame (16,000-sample) gap. D starts 1.0 s (2R) after C, so it
// is a new span, and E follows D after 2R − 1 samples, so D and E merge:
// [248000, 380000). Holds: [24000, 40000), 224000, [404000, 432000).
const ENVELOPE = timelineOf(300, [
  [64000, 112000],
  [114880, 160000],
  [176000, 200000],
  [248000, 300000],
  [347999, 380000],
]);
const N = 480000;

async function envelope() {
  const { musicWindows, musicGainDb } = await music();
  const windows = musicWindows(ENVELOPE);
  return (sample) => musicGainDb(sample, windows);
}

test("envelope: -15.0 dB inside a span and -6.0 dB in a hold", async () => {
  const g = await envelope();
  for (const sample of [64000, 100000, 199999, 248000, 379999]) assert.equal(g(sample), -15, `span sample ${sample}`);
  for (const sample of [24000, 30000, 39999, 404000, 420000, 431999]) assert.equal(g(sample), -6, `hold sample ${sample}`);
});

test("envelope: -10.5 dB at a ramp's midpoint", async () => {
  const g = await envelope();
  assert.equal(g(64000 - R / 2), -10.5);
  assert.equal(g(200000 + R / 2), -10.5);
  assert.equal(g(248000 - R / 2), -10.5);
  assert.equal(g(380000 + R / 2), -10.5);
});

test("envelope: each ramp is exactly R long", async () => {
  const g = await envelope();
  const close = (actual, expected, label) => assert.ok(Math.abs(actual - expected) < 1e-9, `${label}: ${actual}, expected ${expected}`);
  // Down over [a − R, a), linear in dB from -6 to -15. Before 248000 − R
  // lies the previous span's ramp up, which ends there.
  assert.equal(g(64000 - R - 1), -6);
  assert.ok(g(248000 - R - 1) > -6 - 9 / R - 1e-9 && g(248000 - R - 1) < -6);
  for (const a of [64000, 248000]) {
    assert.equal(g(a - R), -6);
    close(g(a - R + 1), -6 - 9 / R, `${a - R + 1}`);
    close(g(a - 1), -15 + 9 / R, `${a - 1}`);
    assert.equal(g(a), -15);
  }
  // Up over [b, b + R), from -15 to -6.
  for (const b of [200000, 380000]) {
    assert.equal(g(b - 1), -15);
    assert.equal(g(b), -15);
    close(g(b + 1), -15 + 9 / R, `${b + 1}`);
    close(g(b + R - 1), -6 - 9 / R, `${b + R - 1}`);
    assert.equal(g(b + R), -6);
  }
});

test("envelope: a 2880-sample gap and a 10-frame gap stay at -15", async () => {
  const g = await envelope();
  for (const sample of [112000, 113440, 114879, 160000, 168000, 175999]) assert.equal(g(sample), -15, `gap sample ${sample}`);
});

test("envelope: a 1.0 s gap rises and falls, and a gap of 2R − 1 samples does not", async () => {
  const g = await envelope();
  // [200000, 248000): up to -6 at 224000, down again from there.
  assert.equal(g(224000), -6);
  assert.ok(g(223999) < -6 && g(224001) < -6);
  // [300000, 347999) stays ducked.
  for (const sample of [300000, 324000, 347998]) assert.equal(g(sample), -15, `gap sample ${sample}`);
});

test("envelope: fade-in reaches 1 at sample R, fade-out gives 0 at N − 1", async () => {
  const g = await envelope();
  assert.equal(g(0), -Infinity);
  assert.ok(Math.abs(g(R / 2) - (-6 + 20 * Math.log10(0.5))) < 1e-9, `${g(R / 2)}`);
  assert.ok(g(R - 1) < -6);
  assert.equal(g(R), -6);
  assert.equal(g(N - 2 * R - 1), -6);
  assert.ok(g(N - 2 * R + 1) < -6);
  assert.ok(Math.abs(g(N - R) - (-6 + 20 * Math.log10((R - 1) / (2 * R - 1)))) < 1e-9, `${g(N - R)}`);
  assert.equal(g(N - 1), -Infinity);
  assert.equal(10 ** (g(N - 1) / 20), 0);
});

// --- the stem ----------------------------------------------------------------------

let lib;
before(() => {
  lib = mkdtempSync(join(tmpdir(), "music-library-"));
  makeLibrary(lib);
});
after(() => rmSync(lib, { recursive: true, force: true }));

const manifestTrack = (id) => JSON.parse(readFileSync(join(lib, "manifest.json"), "utf8")).tracks.find((entry) => entry.id === id);

// The fixture's envelope, from its text: merged spans, levels, ramps and
// fades, as a linear gain.
function expectedGain(timeline) {
  const total = timeline.totalFrames * 1600;
  const blocks = timeline.scenes.flatMap((scene) => scene.sentences.flatMap((sentence) => sentence.blocks));
  const spans = [];
  for (const { startSample, endSample } of blocks) {
    const last = spans.at(-1);
    if (last !== undefined && startSample - last[1] < 2 * R) last[1] = endSample;
    else spans.push([startSample, endSample]);
  }
  return (t) => {
    let level = -6;
    for (const [a, b] of spans) {
      if (t >= a && t < b) level = -15;
      else if (t >= a - R && t < a) level = -6 - (9 * (t - (a - R))) / R;
      else if (t >= b && t < b + R) level = -15 + (9 * (t - b)) / R;
    }
    const fadeIn = t < R ? t / R : 1;
    const fadeOut = t >= total - 2 * R ? (total - 1 - t) / (2 * R - 1) : 1;
    return 10 ** (level / 20) * fadeIn * fadeOut;
  };
}

// The 44-byte header's fields and the samples of a stem.
function readWav(path) {
  const data = readFileSync(path);
  return {
    riff: data.toString("latin1", 0, 4),
    wave: data.toString("latin1", 8, 16),
    format: data.readUInt16LE(20),
    channels: data.readUInt16LE(22),
    rate: data.readUInt32LE(24),
    bits: data.readUInt16LE(34),
    dataTag: data.toString("latin1", 36, 40),
    dataBytes: data.readUInt32LE(40),
    length: data.length,
    samples: new Int16Array(data.buffer, data.byteOffset + 44, (data.length - 44) / 2),
  };
}

function stemDir(t) {
  const dir = mkdtempSync(join(tmpdir(), "music-stem-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  return dir;
}

test("the fixture loop is exactly 96,480 samples per channel, stereo at 48 kHz", () => {
  const decoded = execFileSync("ffmpeg", ["-nostdin", "-v", "error", "-i", join(lib, "fixture-loop.wav"), "-f", "s16le", "-"], { maxBuffer: 16 * 1024 * 1024 });
  assert.equal(decoded.length, LOOP_SAMPLES * 4);
  const header = readWav(join(lib, "fixture-loop.wav"));
  assert.deepEqual([header.channels, header.rate, header.bits], [2, 48000, 16]);
});

test("stem: a loop track three times over is the track decoded once, tiled every 96,480 samples, times the gain", async (t) => {
  const { buildStem } = await music();
  // N = 289,600: loop starts at 96,480 and 192,960 fall inside the two
  // spans (-15 dB), and the one at 289,440 inside the fade-out.
  const timeline = timelineOf(181, [
    [64000, 110000],
    [180000, 220000],
  ]);
  const total = 181 * 1600;
  const dir = stemDir(t);
  const output = join(dir, "music.wav");
  await buildStem({ dir: lib, track: manifestTrack("fixture-loop"), timeline, output });
  const wav = readWav(output);
  assert.deepEqual(
    [wav.riff, wav.wave, wav.format, wav.channels, wav.rate, wav.bits, wav.dataTag, wav.dataBytes, wav.length],
    ["RIFF", "WAVEfmt ", 1, 2, 48000, 16, "data", total * 4, 44 + total * 4],
  );
  const once = execFileSync("ffmpeg", ["-nostdin", "-v", "error", "-i", join(lib, "fixture-loop.wav"), "-f", "s16le", "-ac", "2", "-"], { maxBuffer: 16 * 1024 * 1024 });
  const track = new Int16Array(once.buffer, once.byteOffset, once.length / 2);
  const gain = expectedGain(timeline);
  let worst = 0;
  let worstAt = -1;
  for (let k = 0; k < total; k++) {
    const g = gain(k);
    for (let c = 0; c < 2; c++) {
      const expected = Math.max(-32768, Math.min(32767, Math.round(track[(k % LOOP_SAMPLES) * 2 + c] * g)));
      const off = Math.abs(wav.samples[k * 2 + c] - expected);
      if (off > worst) [worst, worstAt] = [off, k];
    }
  }
  assert.ok(worst <= 1, `sample ${worstAt} is off by ${worst}`);
  assert.deepEqual(readdirSync(dir), ["music.wav"]);
});

test("stem: totalFrames 5400 from the loop track has exactly 8,640,000 samples per channel", async (t) => {
  const { buildStem } = await music();
  const dir = stemDir(t);
  const output = join(dir, "music.wav");
  await buildStem({ dir: lib, track: manifestTrack("fixture-loop"), timeline: timelineOf(5400, [[64000, 8000000]]), output });
  const wav = readWav(output);
  assert.equal(wav.dataBytes, 8640000 * 4);
  assert.equal(wav.samples.length / 2, 8640000);
  // The sample count holds by construction; the loop must also fill the
  // stem up to the fade-out, [N - 3R, N - 2R).
  const N = 8640000;
  assert.ok(wav.samples.subarray(2 * (N - 3 * R), 2 * (N - 2 * R)).some((sample) => sample !== 0), "the loop plays to the fade-out");
  assert.deepEqual(readdirSync(dir), ["music.wav"]);
});

test("stem: a non-loop track shorter than the video is filled with silence to exactly N samples", async (t) => {
  const { buildStem } = await music();
  const dir = stemDir(t);
  const output = join(dir, "music.wav");
  // 1000 frames are 33.3 s; fixture-short lasts 30 s.
  await buildStem({ dir: lib, track: manifestTrack("fixture-short"), timeline: timelineOf(1000, [[64000, 1000000]]), output });
  const wav = readWav(output);
  assert.equal(wav.samples.length / 2, 1600000);
  assert.ok(wav.samples.subarray(2 * 1000000, 2 * 1440000).some((sample) => sample !== 0), "the track plays to 30 s");
  assert.ok(wav.samples.subarray(2 * 1441000).every((sample) => sample === 0), "silence after the track");
});

test("stem: an ffmpeg failure throws ffmpeg exited with …, and leaves no file", async (t) => {
  const { buildStem } = await music();
  const broken = library(t, undefined, { "broken.mp3": "not audio at all" });
  const dir = stemDir(t);
  await assert.rejects(
    buildStem({ dir: broken, track: track("broken"), timeline: timelineOf(100, [[64000, 100000]]), output: join(dir, "music.wav") }),
    (error) => /^ffmpeg exited with \d+: \S/.test(error.message) && !error.message.includes("\n"),
  );
  assert.deepEqual(readdirSync(dir), []);
});

// --- the decode's timeout and exit-time kill (issue #883) ---------------------------

// A library whose only track's file is a FIFO with no writer, so ffmpeg
// blocks opening it.
function fifoLibrary(t) {
  const entry = track("fixture-fifo", { file: "fifo.wav" });
  const dir = library(t, manifestOf(entry));
  const fifo = join(dir, "fifo.wav");
  execFileSync("mkfifo", [fifo]);
  return { dir, fifo, entry };
}

// The ffmpeg lines of `ps -eo args`, run as job.mjs's psField runs ps
// (no COLUMNS, LC_ALL=C), whose first word is ffmpeg and that hold path.
function ffmpegLines(path) {
  const { COLUMNS: _, ...env } = process.env;
  const out = execFileSync("ps", ["-eo", "args"], { encoding: "utf8", env: { ...env, LC_ALL: "C" }, maxBuffer: 64 * 1024 * 1024 });
  return out.split("\n").filter((line) => line.trimStart().split(/\s+/)[0] === "ffmpeg" && line.includes(path));
}

// Polls the scan every 100 ms for up to ms until done(lines) holds;
// resolves to the last lines seen.
async function pollScan(path, ms, done) {
  const until = Date.now() + ms;
  for (;;) {
    const lines = ffmpegLines(path);
    if (done(lines) || Date.now() >= until) return lines;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
}

// Opens the FIFO for writing without blocking (ENXIO: no reader left) and
// closes it, so an ffmpeg still blocked opening it reads EOF and exits.
function releaseFifo(fifo) {
  try {
    closeSync(openSync(fifo, constants.O_WRONLY | constants.O_NONBLOCK));
  } catch (thrown) {
    if (thrown.code !== "ENXIO") throw thrown;
  }
}

test("stem: a decode that does not finish within timeoutMs is killed and rejects with ffmpeg did not finish within 2 s", async (t) => {
  const { buildStem } = await music();
  const { dir, fifo, entry } = fifoLibrary(t);
  const out = stemDir(t);
  try {
    const started = Date.now();
    const bound = new Promise((resolve) => setTimeout(() => resolve("unsettled"), 10000).unref());
    const settled = buildStem({ dir, track: entry, timeline: timelineOf(100, [[64000, 100000]]), output: join(out, "music.wav"), timeoutMs: 2000 }).then(
      () => "resolved",
      (error) => error,
    );
    const outcome = await Promise.race([settled, bound]);
    const elapsed = Date.now() - started;
    t.diagnostic(`rejected after ${elapsed} ms`);
    assert.ok(outcome instanceof Error, `buildStem ${outcome} within 10 s`);
    assert.equal(outcome.message, "ffmpeg did not finish within 2 s");
    assert.ok(elapsed < 10000, `${elapsed} ms`);
    const left = await pollScan(fifo, 5000, (lines) => lines.length === 0);
    assert.deepEqual(left, [], "an ffmpeg child is left running");
    assert.deepEqual(readdirSync(out), []);
  } finally {
    releaseFifo(fifo);
  }
});

test("stem: a stop signal to the worker takes the decode's ffmpeg child with it", async (t) => {
  const { dir, fifo, entry } = fifoLibrary(t);
  const out = stemDir(t);
  const bin = (name) => new URL(`../bin/${name}`, import.meta.url).href;
  const program =
    `import { handleStopSignals } from ${JSON.stringify(bin("job.mjs"))};\n` +
    `import { buildStem } from ${JSON.stringify(bin("music.mjs"))};\n` +
    "handleStopSignals();\n" +
    `await buildStem(${JSON.stringify({ dir, track: entry, timeline: timelineOf(100, [[64000, 100000]]), output: join(out, "music.wav") })});\n`;
  const child = spawn(process.execPath, ["--input-type=module", "-e", program], { stdio: ["ignore", "ignore", "pipe"] });
  let stderr = "";
  child.stderr.on("data", (chunk) => (stderr += chunk));
  const exited = new Promise((resolve) => child.once("exit", (code, signal) => resolve({ code, signal, at: Date.now() })));
  try {
    const seen = await pollScan(fifo, 10000, (lines) => lines.length > 0);
    t.diagnostic(`positive control: the scan saw ${seen.length} ffmpeg line(s)`);
    assert.ok(seen.length > 0, `the scan never saw the decode's ffmpeg; ${stderr}`);
    const sent = Date.now();
    process.kill(child.pid, "SIGTERM");
    const { code, signal, at } = await exited;
    t.diagnostic(`the node child exited ${code ?? signal} ${at - sent} ms after SIGTERM`);
    assert.deepEqual({ code, signal }, { code: 143, signal: null }, stderr);
    const left = await pollScan(fifo, 5000, (lines) => lines.length === 0);
    t.diagnostic(`the scan was clear ${Date.now() - at} ms after the exit`);
    assert.deepEqual(left, [], "the stopped worker left its ffmpeg child running");
  } finally {
    if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
    releaseFifo(fifo);
  }
});
