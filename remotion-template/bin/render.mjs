// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// `kit render`: publishes the checked project as /mnt/user-data/files/
// <name>.mp4. The start path refuses unless the last check passed and the
// project still hashes as it did (bin/project-hash.mjs), then starts the
// render as a background job (bin/job.mjs) and returns at once. Run as
// `node bin/render.mjs --worker <token>` from the project root, this file is
// the job's worker: it checks the pass again, renders to
// .kit/render/<token>.mp4, verifies the video with ffprobe, hashes the
// project again, and only then copies the video beside the target as
// .<name>.mp4.<token>.partial and renames it over the target, so the
// target is complete or untouched. The files directory is another
// filesystem in production, so the video is copied, never renamed, across.
// Imports only node: built-ins and the kit's Remotion-free modules; the
// worker loads the real render (bin/render-video.mjs), and through it
// Remotion, by dynamic import.

import { execFile } from "node:child_process";
import { constants, copyFile, lstat, open, readFile, readdir, rename, rm, unlink } from "node:fs/promises";
import { basename, join } from "node:path";
import { writeCustomIndex } from "./custom-index.mjs";
import { explainerScenes } from "./explainer-scenes.mjs";
import { handleStopSignals, jobState, startJob, stopRequested as jobStopRequested, writeResult } from "./job.mjs";
import { PROJECT_NAME, Refusal, lstatOrNull, notFound } from "./kit-io.mjs";
import { projectHash } from "./project-hash.mjs";
import { VIDEO, validateScript } from "./script-schema.mjs";

const NAME = "render";
const FILES_ROOT = "/mnt/user-data/files";
const STATUS_HINT = "run node bin/kit.mjs status";
const NO_PASS = "no passing check; run node bin/kit.mjs check";
const CHANGED_SINCE = "the project changed since the last passing check; run node bin/kit.mjs check, then render";
const CHANGED_DURING = "the project changed while the render ran; run node bin/kit.mjs check, then render";

const messageOf = (thrown) => (thrown instanceof Error ? thrown.message : String(thrown));
const escapeRegExp = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// A project file's text, following symlinks as the check's reader does;
// null when it is absent.
async function readText(root, path) {
  try {
    return await readFile(join(root, path), "utf8");
  } catch (thrown) {
    if (notFound(thrown)) return null;
    throw thrown;
  }
}

// script.json and timeline.json as the check judged them: both parse and
// validate, and src/custom/index.ts is written again. Null when either
// does not, or when the custom index is refused.
async function readProject(root) {
  const scriptText = await readText(root, "script.json");
  const timelineText = await readText(root, "timeline.json");
  if (scriptText === null || timelineText === null) return null;
  const validated = validateScript(scriptText);
  if (!validated.ok) return null;
  let timeline;
  try {
    timeline = JSON.parse(timelineText);
    explainerScenes(validated.script, timeline);
  } catch (thrown) {
    if (typeof thrown?.code === "string") throw thrown;
    return null;
  }
  try {
    await writeCustomIndex(root, validated.script);
  } catch (thrown) {
    if (thrown instanceof Refusal) return null;
    throw thrown;
  }
  return { script: validated.script, timeline, scriptText, timelineText };
}

// The project the last passing check vouched for, with its hash. Refuses
// without a pass carrying a hash, and when the project no longer hashes
// as it did. A system error propagates.
async function checkedProject(root) {
  const check = await jobState(root, "check");
  if (check.state !== "passed" || typeof check.result.hash !== "string") throw new Refusal(NO_PASS);
  const project = await readProject(root);
  if (project === null) throw new Refusal(CHANGED_SINCE);
  const taken = await projectHash(root, project);
  if (taken.hash !== check.result.hash) throw new Refusal(CHANGED_SINCE);
  return { ...project, hash: taken.hash };
}

// True when the project still hashes as checked, with the texts as they
// are now.
async function stillChecked(root, project) {
  const scriptText = await readText(root, "script.json");
  const timelineText = await readText(root, "timeline.json");
  if (scriptText === null || timelineText === null) return false;
  const again = await projectHash(root, { script: project.script, timeline: project.timeline, scriptText, timelineText });
  return again.hash === project.hash;
}

export async function render(ctx) {
  if (!PROJECT_NAME.test(basename(ctx.root))) throw new Refusal(`${ctx.root}: not a kit project; run kit new`);
  const precondition = () => checkedProject(ctx.root);
  const pid = await startJob(ctx.root, NAME, ["bin/render.mjs", "--worker"], { precondition });
  ctx.say(`started (pid ${pid}); ${STATUS_HINT}`);
  return 0;
}

// --- the worker ------------------------------------------------------------------

// Runs ffprobe with an argument array, never a shell, and resolves to its
// stdout. Rejects naming how it failed, as the contact sheet's ffmpeg does.
function ffprobe(args) {
  return new Promise((resolve, reject) => {
    execFile("ffprobe", args, { encoding: "utf8", maxBuffer: 1024 * 1024 }, (error, stdout, stderr) => {
      if (error === null) resolve(stdout);
      else if (error.code === "ENOENT") reject(new Error("ffprobe: not found on PATH"));
      else if (typeof error.code === "string") reject(error);
      else {
        const first = stderr.split("\n").find((line) => line.trim() !== "");
        reject(new Error(`ffprobe exited with ${error.signal ?? error.code}${first === undefined ? "" : `: ${first.trim()}`}`));
      }
    });
  });
}

// The video's failed rules, in the fixture's order, and its frame count.
async function verify(path, totalFrames) {
  let streams;
  try {
    const probed = await ffprobe(["-v", "error", "-show_entries", "stream=codec_type,codec_name,pix_fmt,nb_frames,duration", "-of", "json", path]);
    streams = JSON.parse(probed).streams ?? [];
  } catch (thrown) {
    return { reasons: [`verify: ${messageOf(thrown)}`] };
  }
  const video = streams.find((stream) => stream.codec_type === "video");
  if (video === undefined) return { reasons: ["verify: no video stream"] };
  const audio = streams.find((stream) => stream.codec_type === "audio" && stream.codec_name === "aac");
  const needs = totalFrames / VIDEO.fps;
  const shownNeeds = needs.toFixed(6);
  const frames = Number(video.nb_frames);
  const reasons = [];
  if (video.codec_name !== "h264") reasons.push(`verify: video codec is ${video.codec_name}, expected h264`);
  if (video.pix_fmt !== "yuv420p") reasons.push(`verify: pixel format is ${video.pix_fmt}, expected yuv420p`);
  if (audio === undefined) reasons.push("verify: no AAC audio stream");
  if (frames !== totalFrames) reasons.push(`verify: ${video.nb_frames ?? "unknown"} video frames, the timeline has ${totalFrames}`);
  // Half a frame either way.
  if (!(Math.abs(Number(video.duration) - needs) <= 1 / (2 * VIDEO.fps))) {
    reasons.push(`verify: the video stream lasts ${video.duration} s, the timeline needs ${shownNeeds} s`);
  }
  // AAC padding makes the audio slightly longer, never a frame shorter.
  if (audio !== undefined && !(Number(audio.duration) >= needs - 1 / VIDEO.fps)) {
    reasons.push(`verify: the audio lasts ${audio.duration} s, the timeline needs ${shownNeeds} s`);
  }
  return { reasons, frames };
}

// The step 7.3 copy: an exclusive copy, flushed to disk.
async function copyPartial(source, partial) {
  await copyFile(source, partial, constants.COPYFILE_EXCL);
  const handle = await open(partial, "r+");
  try {
    await handle.sync();
  } finally {
    await handle.close();
  }
}

// Steps 7.1–7.5: resolves to true once the partial is renamed over the
// target, false when a stop arrived before the rename. Before the rename
// any error removes the partial and propagates.
async function publish({ source, filesRoot, name, token, copy, stopRequested }) {
  const stale = new RegExp(`^\\.${escapeRegExp(name)}\\.mp4\\.[0-9a-f]{32}\\.partial$`);
  for (const entry of await readdir(filesRoot)) {
    if (stale.test(entry) && (await lstatOrNull(join(filesRoot, entry)))?.isFile()) await rm(join(filesRoot, entry), { force: true });
  }
  const target = join(filesRoot, `${name}.mp4`);
  const existing = await lstatOrNull(target);
  if (existing?.isSymbolicLink()) throw new Error(`${target} is a symlink; not replaced`);
  if (existing !== null && !existing.isFile()) throw new Error(`${target} is not a regular file; not replaced`);
  const partial = join(filesRoot, `.${name}.mp4.${token}.partial`);
  const size = (await lstat(source)).size;
  try {
    await copy(source, partial);
    const copied = (await lstat(partial)).size;
    if (copied !== size) throw new Error(`copied ${copied} of ${size} bytes`);
    if (stopRequested()) {
      await rm(partial, { force: true });
      return false;
    }
    await rename(partial, target);
    return true;
  } catch (thrown) {
    await rm(partial, { force: true });
    throw thrown;
  }
}

// Steps 2–7: resolves to {reasons}, {reasons: [], target, frames}, or null
// when a stop arrived before publishing.
async function attempt(root, { token, filesRoot, render: renderVideo, log, copy, stopRequested }) {
  let project;
  try {
    project = await checkedProject(root);
  } catch (thrown) {
    if (thrown instanceof Refusal) return { reasons: [thrown.message] };
    throw thrown;
  }
  const source = join(root, ".kit", NAME, `${token}.mp4`);
  try {
    // The parsed files, never validateScript's output, as the check's sheet.
    await renderVideo({ root, script: JSON.parse(project.scriptText), timeline: JSON.parse(project.timelineText), output: source, log });
  } catch (thrown) {
    return { reasons: [`render: ${messageOf(thrown)}`] };
  }
  log("render: verify");
  const { reasons, frames } = await verify(source, project.timeline.totalFrames);
  if (reasons.length) return { reasons };
  if (!(await stillChecked(root, project))) return { reasons: [CHANGED_DURING] };
  if (stopRequested()) return null;
  log("render: publish");
  const name = basename(root);
  try {
    if (!(await publish({ source, filesRoot, name, token, copy, stopRequested }))) return null;
  } catch (thrown) {
    return { reasons: [`publish: ${messageOf(thrown)}`] };
  }
  try {
    await unlink(source);
  } catch (thrown) {
    // The video is published; the next start removes .kit/render/ anyway.
    log(`render: could not remove ${source}: ${messageOf(thrown)}`);
  }
  return { reasons: [], target: join(filesRoot, `${name}.mp4`), frames };
}

const loadRender = async (args) => (await import("./render-video.mjs")).renderVideo(args);

// The worker's run over the project at root. Resolves to the exit code
// written with the result (0 succeeded, 1 failed), or null when a stop
// arrived and nothing was written. A system error outside the render and
// the publish is the reason `render: <message>`; anything else is a bug,
// which ends the worker without a result.
export async function runRender(
  root,
  { token, filesRoot = FILES_ROOT, render: renderVideo = loadRender, log, copy = copyPartial, stopRequested = jobStopRequested },
) {
  log("render: start");
  let outcome;
  try {
    outcome = await attempt(root, { token, filesRoot, render: renderVideo, log, copy, stopRequested });
  } catch (thrown) {
    if (typeof thrown?.code !== "string") throw thrown;
    outcome = { reasons: [`render: ${thrown.message}`] };
  }
  if (outcome === null) return null;
  const succeeded = outcome.reasons.length === 0;
  const extra = succeeded ? { exitCode: 0, target: outcome.target, frames: outcome.frames } : { exitCode: 1 };
  if (!(await writeResult(root, NAME, token, succeeded ? "succeeded" : "failed", outcome.reasons, extra))) return null;
  log(`render: done ${succeeded ? "succeeded" : "failed"}`);
  return extra.exitCode;
}

async function worker(token) {
  handleStopSignals();
  const log = (text) => process.stdout.write(`${text}\n`);
  const code = await runRender(process.cwd(), { token, log });
  // After a stop signal nothing is written, and the stop timer ends the
  // worker with 143.
  if (code !== null) process.exit(code);
}

if (import.meta.main && process.argv[2] === "--worker") await worker(process.argv[3]);
