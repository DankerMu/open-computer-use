// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The task 6.5 fixture music library: three short tracks generated with
// ffmpeg at test time (no audio is committed), each brought to -16 LUFS by
// task 6.3's linear rule (ebur128=peak=true, then volume=<-16 - I>dB), and a
// version 1 manifest.json with every field present and its provenance
// fields reading "fixture". Every track is a 1000 Hz tone, so it stays apart
// from the 440 Hz fixture voices:
//   fixture-loop   48 kHz stereo 16-bit WAV, loop, exactly 96,480 samples:
//                  2010 whole cycles times 0.75 + 0.25·sin(2π·t/2.01), so
//                  it repeats only every 96,480 samples;
//   fixture-short  48 kHz stereo MP3, 30 s;
//   fixture-long   48 kHz stereo MP3, 200 s.
// makeLibrary(dir) writes into dir, which may already exist (a mount
// point). Run as `node tests/fixtures/music/library.mjs <dir>`, it writes
// the same library. Imports no kit module.

import { execFileSync, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

export const LOOP_SAMPLES = 96480;
const FIXTURE = "fixture";
const RATE = "48000";
const LOOP_SOURCE = `aevalsrc=exprs='sin(2*PI*1000*t)*(0.75+0.25*sin(2*PI*t/2.01))':s=${RATE}`;
const sine = (seconds) => `sine=frequency=1000:sample_rate=${RATE}:duration=${seconds}`;

export const FIXTURE_TRACKS = [
  { id: "fixture-loop", mood: "loop", loop: true, format: "wav", source: LOOP_SOURCE, trim: `atrim=end_sample=${LOOP_SAMPLES}`, codec: ["-c:a", "pcm_s16le"] },
  { id: "fixture-short", mood: "calm-curious", loop: false, format: "mp3", source: sine(30), trim: null, codec: ["-c:a", "libmp3lame", "-b:a", "320k"] },
  { id: "fixture-long", mood: "calm-curious", loop: false, format: "mp3", source: sine(200), trim: null, codec: ["-c:a", "libmp3lame", "-b:a", "320k"] },
];

const sha256 = (data) => createHash("sha256").update(data).digest("hex");
const ffmpeg = (args) => execFileSync("ffmpeg", ["-nostdin", "-hide_banner", ...args], { stdio: ["ignore", "ignore", "pipe"] });

// Integrated loudness and true peak from ebur128's summary on stderr.
function loudness(path) {
  const run = spawnSync("ffmpeg", ["-nostdin", "-hide_banner", "-nostats", "-i", path, "-af", "ebur128=peak=true", "-f", "null", "-"], { encoding: "utf8" });
  if (run.status !== 0) throw new Error(`ffmpeg ebur128 failed on ${path}: ${run.stderr}`);
  const summary = run.stderr.slice(run.stderr.lastIndexOf("Summary:"));
  const integrated = summary.match(/\n\s+I:\s+(-?\d+(?:\.\d+)?) LUFS/);
  const peak = summary.match(/\n\s+Peak:\s+(-?\d+(?:\.\d+)?) dBFS/);
  if (integrated === null || peak === null) throw new Error(`no ebur128 summary for ${path}`);
  return { integrated: Number(integrated[1]), peak: Number(peak[1]) };
}

function probe(path) {
  const data = JSON.parse(
    execFileSync("ffprobe", ["-v", "error", "-show_entries", "stream=sample_rate:format=duration", "-of", "json", path], { encoding: "utf8" }),
  );
  return { sample_rate: Number(data.streams[0].sample_rate), duration_seconds: Math.round(Number(data.format.duration) * 1000) / 1000 };
}

// Writes the three tracks and manifest.json into dir; returns the manifest.
export function makeLibrary(dir) {
  mkdirSync(dir, { recursive: true });
  const work = mkdtempSync(join(tmpdir(), "music-fixture-"));
  try {
    const tracks = FIXTURE_TRACKS.map((track) => {
      const source = join(work, `${track.id}.source.wav`);
      ffmpeg(["-v", "error", "-f", "lavfi", "-i", track.source, ...(track.trim ? ["-af", track.trim] : []), "-ac", "2", "-c:a", "pcm_f32le", "-y", source]);
      const gain = -16 - loudness(source).integrated;
      const file = `${track.id}.${track.format}`;
      const output = join(dir, file);
      ffmpeg(["-v", "error", "-i", source, "-af", `volume=${gain.toFixed(2)}dB`, "-ar", RATE, "-ac", "2", ...track.codec, "-y", output]);
      const shipped = loudness(output);
      return {
        id: track.id,
        mood: track.mood,
        loop: track.loop,
        title: FIXTURE,
        author: FIXTURE,
        file,
        format: track.format,
        ...probe(output),
        source_url: FIXTURE,
        licence_url: FIXTURE,
        evidence: FIXTURE,
        source_sha256: FIXTURE,
        shipped_sha256: sha256(readFileSync(output)),
        integrated_lufs: shipped.integrated,
        true_peak_dbtp: shipped.peak,
        rerelease_note: FIXTURE,
        approver: FIXTURE,
        approval_date: FIXTURE,
      };
    });
    const manifest = { version: 1, approvals: { file: FIXTURE, sha256: FIXTURE, reviewer: FIXTURE, date: FIXTURE }, tracks };
    writeFileSync(join(dir, "manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`);
    return manifest;
  } finally {
    rmSync(work, { recursive: true, force: true });
  }
}

if (import.meta.main) {
  if (process.argv.length !== 3) {
    process.stderr.write("usage: node tests/fixtures/music/library.mjs <dir>\n");
    process.exit(2);
  }
  makeLibrary(process.argv[2]);
}
