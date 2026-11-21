// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The background music of an explainer: the image's library (task 6.3's
// manifest.json, version 1, under /opt/video-music), kit check's music
// rules, the ducking envelope, and the stem the render worker writes as
// public/music.wav. The stem is the selected track decoded, looped by
// ffmpeg's -stream_loop when it is a loop track, ducked under the
// narration, faded and written at its final level, so the Explainer plays
// it at volume 1 beside the narration. A "none" project never reads the
// library. ffmpeg runs through spawn with an argument array, never a shell.
// Imports only node: built-ins and the kit's Remotion-free modules.

import { spawn } from "node:child_process";
import { lstat, open, readFile } from "node:fs/promises";
import { basename, dirname, join } from "node:path";
import { tenthsText, wavHeader } from "./audio.mjs";
import { Refusal, writeAtomic } from "./kit-io.mjs";
import { MUSIC_ID, escapeLine } from "./script-schema.mjs";

export const MUSIC_DIR = "/opt/video-music";

const NONE = "none";
const SAMPLE_RATE = 48000;
const SAMPLES_PER_FRAME = 1600;
const CHANNELS = 2;
const BYTES_PER_FRAME = CHANNELS * 2;
// 0.5 s: each ramp, the fade-in, and half the fade-out.
const R = 24000;
const LEVELS = { voice: -15, hold: -6 };
const MAX_REASON = 300;
const FILE = /^[a-z0-9][a-z0-9-]*\.(mp3|wav)$/;
const NO_LIBRARY = 'this image has no music library; set "music" to "none"';

const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const shown = (value) => JSON.stringify(value) ?? "absent";

// The manifest's reason, escaped and cut to MAX_REASON code points.
function unreadable(reason) {
  return new Refusal(`the music library manifest cannot be read: ${[...escapeLine(reason)].slice(0, MAX_REASON).join("")}`);
}

// Why a track entry is bad, or null.
function entryProblem(entry, i) {
  const at = `tracks[${i}]`;
  if (!isObject(entry)) return `${at} is ${shown(entry)}, not an object`;
  if (typeof entry.id !== "string" || !MUSIC_ID.test(entry.id)) return `${at}.id is ${shown(entry.id)}, not a track id`;
  if (typeof entry.loop !== "boolean") return `${at}.loop is ${shown(entry.loop)}, not true or false`;
  if (typeof entry.file !== "string" || !FILE.test(entry.file)) return `${at}.file is ${shown(entry.file)}, not a plain .mp3 or .wav name`;
  const duration = entry.duration_seconds;
  if (typeof duration !== "number" || !Number.isFinite(duration) || duration <= 0) {
    return `${at}.duration_seconds is ${shown(duration)}, not a positive number`;
  }
  return null;
}

// Resolves {tracks} in manifest order, or throws Refusal.
export async function readLibrary(dir) {
  let text;
  try {
    text = await readFile(join(dir, "manifest.json"), "utf8");
  } catch (thrown) {
    if (thrown?.code === "ENOENT") throw new Refusal(NO_LIBRARY);
    throw unreadable(thrown instanceof Error ? thrown.message : String(thrown));
  }
  let manifest;
  try {
    manifest = JSON.parse(text);
  } catch (thrown) {
    throw unreadable(thrown.message);
  }
  if (!isObject(manifest)) throw unreadable("the manifest is not a JSON object");
  if (manifest.version !== 1) throw unreadable(`version is ${shown(manifest.version)}, expected 1`);
  if (!Array.isArray(manifest.tracks)) throw unreadable("tracks is not a list");
  for (const [i, entry] of manifest.tracks.entries()) {
    const problem = entryProblem(entry, i);
    if (problem !== null) throw unreadable(problem);
  }
  return { tracks: manifest.tracks };
}

// Why the track's file cannot be read, or null. A symlink is not followed.
async function fileProblem(path) {
  let stat;
  try {
    stat = await lstat(path);
  } catch (thrown) {
    return thrown.code ?? String(thrown);
  }
  if (!stat.isFile()) return "not a regular file";
  try {
    await (await open(path, "r")).close();
  } catch (thrown) {
    return thrown.code ?? String(thrown);
  }
  return null;
}

// kit check's music rules: [] for "none" without reading anything, else at
// most one reason.
export async function musicReasons(script, timeline, dir) {
  const id = script.music;
  if (id === NONE) return [];
  let tracks;
  try {
    ({ tracks } = await readLibrary(dir));
  } catch (thrown) {
    if (thrown instanceof Refusal) return [`music: ${thrown.message}`];
    throw thrown;
  }
  const track = tracks.find((entry) => entry.id === id);
  if (track === undefined) {
    return [`music: unknown track "${id}"; choose "none" or one of: ${tracks.map((entry) => entry.id).join(", ")}`];
  }
  const problem = await fileProblem(join(dir, track.file));
  if (problem !== null) return [`music: the library file for "${id}" cannot be read: ${problem}`];
  if (!track.loop && track.duration_seconds < timeline.totalFrames / 30) {
    // The track's length floored, the video's ceiled, so neither reads as long enough.
    const d = tenthsText(Math.floor(track.duration_seconds * 10 + 1e-9));
    const v = tenthsText(Math.ceil(timeline.totalFrames / 3));
    const loops = tracks.filter((entry) => entry.loop).map((entry) => entry.id);
    const way = loops.length
      ? `choose a loop track (${loops.join(", ")}) or a track at least ${v} s long`
      : `choose a track at least ${v} s long, or "none"`;
    return [`music: track "${id}" lasts ${d} s, shorter than the video's ${v} s; ${way}`];
  }
  return [];
}

// --- the envelope ------------------------------------------------------------------

// The voice spans in playback order, merged across gaps under 2R, and N.
export function musicWindows(timeline) {
  const spans = [];
  for (const scene of timeline.scenes) {
    for (const sentence of scene.sentences) {
      for (const { startSample, endSample } of sentence.blocks) {
        const last = spans.at(-1);
        if (last !== undefined && startSample - last[1] < 2 * R) last[1] = endSample;
        else spans.push([startSample, endSample]);
      }
    }
  }
  return { spans, total: timeline.totalFrames * SAMPLES_PER_FRAME };
}

// The span whose [a − R, b + R) holds t, or undefined: merged spans are at
// least 2R apart, so at most one does.
function spanAt(spans, t) {
  let lo = 0;
  let hi = spans.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const [a, b] = spans[mid];
    if (t < a - R) hi = mid - 1;
    else if (t >= b + R) lo = mid + 1;
    else return spans[mid];
  }
  return undefined;
}

// The music's gain in dB at sample t: the ducking level, linear in dB over
// its ramps, plus the fades as amplitude factors (-Infinity where a fade
// is 0).
export function musicGainDb(t, { spans, total }) {
  const span = spanAt(spans, t);
  let level = LEVELS.hold;
  if (span !== undefined) {
    const [a, b] = span;
    const depth = LEVELS.hold - LEVELS.voice;
    if (t < a) level = LEVELS.hold - (depth * (t - (a - R))) / R;
    else if (t < b) level = LEVELS.voice;
    else level = LEVELS.voice + (depth * (t - b)) / R;
  }
  const fadeIn = t < R ? t / R : 1;
  const fadeOut = t >= total - 2 * R ? (total - 1 - t) / (2 * R - 1) : 1;
  return level + 20 * Math.log10(fadeIn * fadeOut);
}

// --- the stem ----------------------------------------------------------------------

// Decodes the track into `samples` (N stereo int16 frames, zeroed beyond
// what ffmpeg gives), streaming stdout so the decoded audio is never held
// twice. Rejects naming how ffmpeg failed, or, after killing it, that it
// did not finish within timeoutMs. While the child is alive an exit
// listener kills it, so the stop handler's process.exit takes it along.
function decode(args, samples, timeoutMs) {
  return new Promise((resolve, reject) => {
    const child = spawn("ffmpeg", args, { stdio: ["ignore", "pipe", "pipe"] });
    const kill = () => child.kill("SIGKILL");
    process.on("exit", kill);
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      kill();
      reject(new Error(`ffmpeg did not finish within ${timeoutMs / 1000} s`));
    }, timeoutMs);
    const done = () => {
      clearTimeout(timer);
      process.removeListener("exit", kill);
    };
    const room = samples.length;
    let filled = 0;
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      const take = Math.min(chunk.length, room - filled);
      if (take > 0) chunk.copy(samples, filled, 0, take);
      filled += take;
    });
    child.stderr.on("data", (chunk) => {
      if (stderr.length < 4096) stderr += chunk.toString("utf8");
    });
    child.once("error", (thrown) => {
      done();
      reject(thrown);
    });
    child.once("close", (code, signal) => {
      done();
      if (timedOut) return;
      if (code === 0) return resolve();
      const first = stderr.split("\n").find((line) => line.trim() !== "");
      reject(new Error(escapeLine(`ffmpeg exited with ${signal ?? code}${first === undefined ? "" : `: ${first.trim()}`}`)));
    });
  });
}

// Writes the stem for `track` (a readLibrary entry in dir) over the
// timeline to `output`, through a temporary .<name>.<hex>.partial beside
// it and one rename. A decode that runs past timeoutMs is killed.
export async function buildStem({ dir, track, timeline, output, timeoutMs = 120000 }) {
  const windows = musicWindows(timeline);
  const total = windows.total;
  const stem = Buffer.alloc(44 + total * BYTES_PER_FRAME);
  wavHeader(total * BYTES_PER_FRAME, CHANNELS).copy(stem, 0);
  const samples = stem.subarray(44);
  const loop = track.loop ? ["-stream_loop", "-1"] : [];
  await decode(
    ["-nostdin", "-v", "error", ...loop, "-i", join(dir, track.file), "-af", `aresample=${SAMPLE_RATE},atrim=end_sample=${total}`, "-ac", String(CHANNELS), "-f", "s16le", "-"],
    samples,
    timeoutMs,
  );
  for (let t = 0; t < total; t++) {
    const gain = 10 ** (musicGainDb(t, windows) / 20);
    for (let c = 0; c < CHANNELS; c++) {
      const at = t * BYTES_PER_FRAME + c * 2;
      const value = Math.round(samples.readInt16LE(at) * gain);
      samples.writeInt16LE(Math.max(-32768, Math.min(32767, value)), at);
    }
  }
  await writeAtomic(dirname(output), basename(output), stem);
}
