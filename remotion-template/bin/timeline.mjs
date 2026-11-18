// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// `kit timeline`: decodes every imported block with its silence trimmed,
// lays the blocks out with scene-timing.mjs, checks the 60–180 s window,
// builds one narration track normalized to -16 LUFS, and writes
// public/narration.wav and timeline.json. Nothing is replaced until every
// check has passed. ffmpeg runs through execFile with an argument array,
// never a shell.

import { execFile } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { readFile, readdir, rename, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { Refusal, lstatOrNull, readScript } from "./kit-io.mjs";
import { FPS, LENGTH_WINDOW, SAMPLE_RATE, layout, loudnessOk } from "./scene-timing.mjs";
import { escapeLine, playbackWhere } from "./script-schema.mjs";
import { blockStems } from "./voice-stem.mjs";

// More than the 17,280,000 bytes of 180 s of 48 kHz 16-bit PCM.
const MAX_BUFFER = 32 * 1024 * 1024;
const SAMPLES_PER_FRAME = SAMPLE_RATE / FPS;
const BYTES_PER_SAMPLE = 2;
const KEEP = "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.02";
const TRIM = `${KEEP},areverse,${KEEP},areverse`;
const LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11";
const RAW_INPUT = ["-f", "s16le", "-ar", String(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0"];
const NARRATION = "narration.wav";
const TIMELINE = "timeline.json";
const RUN_HINT = "run node bin/kit.mjs voice, then voice-import";

const sha256 = (data) => createHash("sha256").update(data).digest("hex");

// The temporary file dir/name is written to before its rename, named as
// writeAtomic names its own.
const partialPath = (dir, name) => join(dir, `.${name}.${randomBytes(6).toString("hex")}.partial`);

// Runs ffmpeg; resolves {failed, stdout, stderr} for any exit status. A
// missing binary is a refusal; any other spawn or buffer error rejects as
// itself, which kit.mjs reports as a failed step.
function ffmpeg(args, input) {
  return new Promise((resolve, reject) => {
    const child = execFile("ffmpeg", args, { encoding: "buffer", maxBuffer: MAX_BUFFER }, (error, stdout, stderr) => {
      if (error?.code === "ENOENT") reject(new Refusal("ffmpeg: not found on PATH"));
      else if (typeof error?.code === "string") reject(error);
      else resolve({ failed: error !== null, stdout, stderr: stderr.toString("utf8") });
    });
    // An ffmpeg that exits before reading all its input closes the pipe; the
    // EPIPE carries nothing the exit status in the callback does not.
    child.stdin.on("error", () => {});
    child.stdin.end(input);
  });
}

// ffmpeg's first stderr line without its `[<name> @ 0x<hex>] ` prefix, on one line.
const reason = (stderr) => escapeLine(stderr.split("\n")[0].replace(/^\[[^\]]* @ 0x[0-9a-f]+\] /, ""));

// --- inputs -----------------------------------------------------------------

// public/voice, or null when public or public/voice is absent. Never creates
// either.
async function voiceDir(root) {
  let path = root;
  for (const part of ["public", "voice"]) {
    path = join(path, part);
    const stat = await lstatOrNull(path);
    if (stat === null) return null;
    if (stat.isSymbolicLink()) throw new Refusal(`${path}: is a symlink`);
    if (!stat.isDirectory()) throw new Refusal(`${path}: is not a directory`);
  }
  return path;
}

// A regular file for the same scene, sentence and block under another hash.
async function hasStale(dir, names, stem) {
  const other = new RegExp(`^${stem.slice(0, stem.lastIndexOf("-") + 1)}[0-9a-f]{8}\\.wav$`);
  for (const name of names) {
    if (name !== `${stem}.wav` && other.test(name) && (await lstatOrNull(join(dir, name)))?.isFile()) return true;
  }
  return false;
}

// Resolves {pcm, sha256} for one block, or {problem}.
async function readBlock(dir, names, stem) {
  const path = dir === null ? null : join(dir, `${stem}.wav`);
  if (path === null || !(await lstatOrNull(path))?.isFile()) {
    if (dir !== null && (await hasStale(dir, names, stem))) {
      return { problem: `stale: its text, voice or instructions changed, needs ${stem}.wav` };
    }
    return { problem: `missing ${stem}.wav` };
  }
  const decoded = await ffmpeg(["-nostdin", "-v", "error", "-i", path, "-ac", "1", "-ar", String(SAMPLE_RATE), "-af", TRIM, "-f", "s16le", "-"]);
  if (decoded.failed) return { problem: `cannot be decoded: ${reason(decoded.stderr)}` };
  if (decoded.stdout.length === 0) return { problem: "silent after trimming" };
  return { pcm: decoded.stdout, sha256: sha256(await readFile(path)) };
}

// --- narration ----------------------------------------------------------------

// tenths of a second, as `<s>.<d>`.
const seconds = (tenths) => `${Math.floor(tenths / 10)}.${tenths % 10}`;

// null inside the 60–180 s window, else the failure text: seconds rounded
// up above the window and down below it, so neither bound reads as inside.
export function lengthError(totalFrames) {
  let tenths;
  if (totalFrames > LENGTH_WINDOW.maxFrames) tenths = Math.ceil(totalFrames / 3);
  else if (totalFrames < LENGTH_WINDOW.minFrames) tenths = Math.floor(totalFrames / 3);
  else return null;
  return `total ${seconds(tenths)} s (${totalFrames} frames) is outside 60–180 s`;
}

// Every block's trimmed PCM at its startSample in one silent buffer.
function mix(timeline, audio) {
  const raw = Buffer.alloc(timeline.totalFrames * SAMPLES_PER_FRAME * BYTES_PER_SAMPLE);
  for (const scene of timeline.scenes) {
    for (const sentence of scene.sentences) {
      for (const block of sentence.blocks) audio.get(block.stem).pcm.copy(raw, block.startSample * BYTES_PER_SAMPLE);
    }
  }
  return raw;
}

// The JSON object loudnorm prints last on stderr at the info level.
function loudnormSummary(stderr) {
  try {
    return JSON.parse(stderr.slice(stderr.lastIndexOf("{"), stderr.lastIndexOf("}") + 1));
  } catch (thrown) {
    if (thrown instanceof SyntaxError) throw new Refusal("narration cannot be normalized: no loudnorm summary");
    throw thrown;
  }
}

// Two-pass loudnorm of the raw buffer, both passes reading it on stdin.
async function normalize(raw) {
  const measured = await ffmpeg(["-nostdin", "-hide_banner", "-nostats", ...RAW_INPUT, "-af", `${LOUDNORM}:print_format=json`, "-f", "null", "-"], raw);
  if (measured.failed) throw new Refusal(`narration cannot be normalized: ${reason(measured.stderr)}`);
  const m = loudnormSummary(measured.stderr);
  const second =
    `${LOUDNORM}:linear=true:measured_I=${m.input_i}:measured_TP=${m.input_tp}` +
    `:measured_LRA=${m.input_lra}:measured_thresh=${m.input_thresh}:offset=${m.target_offset}`;
  const applied = await ffmpeg(
    ["-nostdin", "-v", "error", ...RAW_INPUT, "-af", second, "-ar", String(SAMPLE_RATE), "-ac", "1", "-f", "s16le", "-"],
    raw,
  );
  if (applied.failed) throw new Refusal(`narration cannot be normalized: ${reason(applied.stderr)}`);
  return applied.stdout;
}

// A 44-byte PCM WAV header (48 kHz, 16-bit, mono) before the samples.
function wav(pcm) {
  const header = Buffer.alloc(44);
  header.write("RIFF", 0, "latin1");
  header.writeUInt32LE(36 + pcm.length, 4);
  header.write("WAVEfmt ", 8, "latin1");
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20);
  header.writeUInt16LE(1, 22);
  header.writeUInt32LE(SAMPLE_RATE, 24);
  header.writeUInt32LE(SAMPLE_RATE * BYTES_PER_SAMPLE, 28);
  header.writeUInt16LE(BYTES_PER_SAMPLE, 32);
  header.writeUInt16LE(8 * BYTES_PER_SAMPLE, 34);
  header.write("data", 36, "latin1");
  header.writeUInt32LE(pcm.length, 40);
  return Buffer.concat([header, pcm]);
}

// Integrated loudness and true peak from ebur128's Summary, to one decimal.
async function measure(path) {
  const result = await ffmpeg(["-nostdin", "-hide_banner", "-nostats", "-i", path, "-af", "ebur128=peak=true", "-f", "null", "-"]);
  if (result.failed) throw new Refusal(`narration cannot be measured: ${reason(result.stderr)}`);
  const summary = result.stderr.slice(result.stderr.lastIndexOf("Summary:"));
  const integrated = summary.match(/\n\s+I:\s+(-?\d+(?:\.\d+)?) LUFS/);
  const peak = summary.match(/\n\s+Peak:\s+(-?\d+(?:\.\d+)?) dBFS/);
  if (integrated === null || peak === null) throw new Refusal("narration cannot be measured: no ebur128 summary");
  const tenth = (match) => Math.round(Number(match[1]) * 10) / 10;
  return { integratedLufs: tenth(integrated), truePeakDbtp: tenth(peak) };
}

// --- the command ----------------------------------------------------------------

function withHashes(timeline, narration, audio) {
  const { version, fps, sampleRate, totalFrames, scenes } = timeline;
  return {
    version,
    fps,
    sampleRate,
    totalFrames,
    narration,
    scenes: scenes.map((scene) => ({
      ...scene,
      sentences: scene.sentences.map((sentence) => ({
        blocks: sentence.blocks.map((block) => ({ ...block, audioSha256: audio.get(block.stem).sha256 })),
      })),
    })),
  };
}

export async function timeline(ctx) {
  const script = await readScript(ctx);
  const stems = blockStems(script);
  const dir = await voiceDir(ctx.root);
  const names = dir === null ? [] : await readdir(dir);
  const audio = new Map();
  const problems = [];
  for (const entry of stems) {
    const block = await readBlock(dir, names, entry.stem);
    if (block.problem === undefined) audio.set(entry.stem, block);
    else problems.push(`${playbackWhere(script, entry.scene)}, sentence ${entry.sentence + 1} block ${entry.block + 1}: ${block.problem}`);
  }
  if (problems.length) {
    for (const line of problems) ctx.say(line);
    ctx.say(RUN_HINT);
    return 1;
  }
  const laid = layout(
    script,
    stems.map((entry) => ({ ...entry, samples: audio.get(entry.stem).pcm.length / BYTES_PER_SAMPLE })),
  );
  const outside = lengthError(laid.totalFrames);
  if (outside !== null) throw new Refusal(outside);
  const pcm = await normalize(mix(laid, audio));
  const expected = laid.totalFrames * SAMPLES_PER_FRAME;
  if (pcm.length !== expected * BYTES_PER_SAMPLE) {
    throw new Refusal(`narration has ${pcm.length / BYTES_PER_SAMPLE} samples, expected ${expected}`);
  }
  const track = wav(pcm);
  const publicPath = join(ctx.root, "public");
  const narrationTemp = partialPath(publicPath, NARRATION);
  const timelineTemp = partialPath(ctx.root, TIMELINE);
  let loudness;
  try {
    await writeFile(narrationTemp, track, { flag: "wx" });
    loudness = await measure(narrationTemp);
    const { integratedLufs, truePeakDbtp } = loudness;
    if (!loudnessOk(integratedLufs, truePeakDbtp)) {
      throw new Refusal(
        `narration measures ${integratedLufs.toFixed(1)} LUFS, ${truePeakDbtp.toFixed(1)} dBTP; needs -16 ± 1 LUFS and at most -1.0 dBTP`,
      );
    }
    const narration = { file: `public/${NARRATION}`, sha256: sha256(track), integratedLufs, truePeakDbtp };
    await writeFile(timelineTemp, `${JSON.stringify(withHashes(laid, narration, audio), null, 2)}\n`, { flag: "wx" });
    await rename(narrationTemp, join(publicPath, NARRATION));
    await rename(timelineTemp, join(ctx.root, TIMELINE));
  } finally {
    await rm(narrationTemp, { force: true });
    await rm(timelineTemp, { force: true });
  }
  const { totalFrames } = laid;
  ctx.say(
    `${stems.length} blocks, ${laid.scenes.length} scenes, ${seconds(Math.round(totalFrames / 3))} s (${totalFrames} frames), ${loudness.integratedLufs.toFixed(1)} LUFS`,
  );
  return 0;
}
