// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// kit render (task 5.3), pure: the start path's refusals through
// run(argv, io), the worker's runRender with a stub render and ffprobe, and
// status's render lines. The project is the task 5.1 pure fixture
// (tests/fixtures/check/) on disk at <temp>/demo, with a passed check result
// whose hash is projectHash of that project. The stub render copies an MP4
// made here by ffmpeg (testsrc at 64×36, 30 fps, totalFrames frames,
// libx264 yuv420p, an AAC sine track); its variants are mpeg4, yuv444p, no
// audio, one frame short and audio 0.5 s short. Expected messages come from
// the task 5.3 fixture. render.mjs and status.mjs are imported inside the
// tests, so a missing module fails each test on its own.

import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync, spawn, spawnSync } from "node:child_process";
import {
  appendFileSync,
  copyFileSync,
  existsSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  readlinkSync,
  realpathSync,
  renameSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { writeCustomIndex } from "../bin/custom-index.mjs";
import { run } from "../bin/kit.mjs";
import { projectHash } from "../bin/project-hash.mjs";
import { validateScript } from "../bin/script-schema.mjs";
import { blockStems } from "../bin/voice-stem.mjs";
import { checkFiles, checkScript, checkTimeline } from "./fixtures/check/fixture.mjs";

const TEMPLATE = fileURLToPath(new URL("..", import.meta.url)).replace(/\/$/, "");
const rendering = () => import("../bin/render.mjs");

const NO_PASS = "no passing check; run node bin/kit.mjs check";
const CHANGED_SINCE = "the project changed since the last passing check; run node bin/kit.mjs check, then render";
const CHANGED_DURING = "the project changed while the render ran; run node bin/kit.mjs check, then render";
const LOCK_HELD = (pid) => `another kit command is starting a job (pid ${pid}); try again, or remove .kit/start.lock if none is`;
const STARTED = /^kit render: started \(pid (\d+)\); run node bin\/kit\.mjs status\n$/;
const TOKEN = "c3".repeat(16);
const PREVIOUS = Buffer.from("the previous complete render");

const SCRIPT = checkScript();
const TOTAL = checkTimeline(SCRIPT).totalFrames;
const STEMS = blockStems(SCRIPT).map((entry) => entry.stem);
const seconds = (frames) => (frames / 30).toFixed(6);

const pause = (ms) => new Promise((done) => setTimeout(done, ms));
const readJson = (path) => JSON.parse(readFileSync(path, "utf8"));

// --- the stub videos ---------------------------------------------------------

const videos = mkdtempSync(join(tmpdir(), "render-job-videos-"));
const VARIANTS = {
  good: { frames: TOTAL, audio: TOTAL / 30, args: ["-c:v", "libx264", "-pix_fmt", "yuv420p"] },
  mpeg4: { frames: TOTAL, audio: TOTAL / 30, args: ["-c:v", "mpeg4", "-pix_fmt", "yuv420p"] },
  yuv444p: { frames: TOTAL, audio: TOTAL / 30, args: ["-c:v", "libx264", "-pix_fmt", "yuv444p"] },
  silent: { frames: TOTAL, audio: null, args: ["-c:v", "libx264", "-pix_fmt", "yuv420p"] },
  short: { frames: TOTAL - 1, audio: TOTAL / 30, args: ["-c:v", "libx264", "-pix_fmt", "yuv420p"] },
  quiet: { frames: TOTAL, audio: TOTAL / 30 - 0.5, args: ["-c:v", "libx264", "-pix_fmt", "yuv420p"] },
};
const video = (name) => join(videos, `${name}.mp4`);

before(() => {
  for (const [name, { frames, audio, args }] of Object.entries(VARIANTS)) {
    const sine = audio === null ? [] : ["-f", "lavfi", "-i", `sine=frequency=440:sample_rate=48000:duration=${audio}`];
    const maps = audio === null ? ["-map", "0:v"] : ["-map", "0:v", "-map", "1:a", "-c:a", "aac"];
    // testsrc emits the frames whose time is below its duration: exactly
    // `frames` of them. Each input ends on its own, so neither stream is cut
    // to the other's length.
    execFileSync("ffmpeg", [
      "-nostdin", "-v", "error",
      "-f", "lavfi", "-i", `testsrc=size=64x36:rate=30:duration=${(frames - 0.5) / 30}`,
      ...sine,
      ...maps,
      ...args,
      "-y", video(name),
    ]);
  }
});

after(() => rmSync(videos, { recursive: true, force: true }));

// --- the project ---------------------------------------------------------------

// The pure fixture project at <temp>/<name>, with src/custom/index.ts
// written and a passed check result whose hash is projectHash's. With
// `worker`, it also holds bin/ and a node_modules that links only zod, so a
// real worker starts and then fails to load Remotion.
async function fixtureProject(t, { name = "demo", worker = false } = {}) {
  const base = mkdtempSync(join(tmpdir(), "render-job-"));
  t.after(() => rmSync(base, { recursive: true, force: true }));
  const root = join(base, name);
  for (const [path, bytes] of checkFiles()) {
    mkdirSync(dirname(join(root, path)), { recursive: true });
    writeFileSync(join(root, path), bytes);
  }
  if (worker) {
    execFileSync("cp", ["-R", join(TEMPLATE, "bin"), join(root, "bin")]);
    writeFileSync(join(root, "package.json"), readFileSync(join(TEMPLATE, "package.json")));
    // A project's node_modules is a link, as kit new makes it.
    mkdirSync(join(base, "modules"));
    symlinkSync(realpathSync(join(TEMPLATE, "node_modules", "zod")), join(base, "modules", "zod"));
    symlinkSync(join(base, "modules"), join(root, "node_modules"));
  }
  const files = join(base, "files");
  mkdirSync(files);
  const p = { base, root, files, videos: join(base, "videos"), target: join(files, `${name}.mp4`) };
  p.hash = await currentHash(root);
  plantCheck(root, { outcome: "passed", reasons: [], count: 0, hash: p.hash, sheet: ".kit/check/contact.png", tiles: [] });
  return p;
}

async function currentHash(root) {
  const scriptText = readFileSync(join(root, "script.json"), "utf8");
  const timelineText = readFileSync(join(root, "timeline.json"), "utf8");
  const { script } = validateScript(scriptText);
  await writeCustomIndex(root, script);
  const taken = await projectHash(root, { script, timeline: JSON.parse(timelineText), scriptText, timelineText });
  assert.ok(taken.hash, JSON.stringify(taken));
  return taken.hash;
}

// .kit/<name>/job.json and, when given, result.json, written by hand.
function plantJob(root, name, job, result) {
  const dir = join(root, ".kit", name);
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, "job.json"), JSON.stringify({ version: 1, name, ...job }));
  if (result) writeFileSync(join(dir, "result.json"), JSON.stringify({ version: 1, finishedAt: 0, ...result }));
  else rmSync(join(dir, "result.json"), { force: true });
}

function plantCheck(root, result, startedAt = 1000) {
  const token = "a7".repeat(16);
  plantJob(root, "check", { token, pid: null, startedAt }, result && { token, ...result });
}

// A live process whose arguments hold the token: a running job.
function liveJob(t, token) {
  const live = spawn(process.execPath, ["-e", "setTimeout(() => {}, 60000)", token], { stdio: "ignore" });
  t.after(() => live.kill("SIGKILL"));
  return live.pid;
}

async function kit(argv, p, root = p.root) {
  let out = "";
  let err = "";
  const code = await run(argv, {
    root,
    videosRoot: p.videos,
    filesRoot: p.files,
    stdout: { write: (text) => (out += text) },
    stderr: { write: (text) => (err += text) },
  });
  return { code, out, err };
}

const refused = (message, code = 1) => ({ code, out: "", err: `kit render: ${message}\n` });
const checkResult = (root) => readFileSync(join(root, ".kit", "check", "result.json"), "utf8");

// A refusal changes nothing: no render directory, no lock, the same pass.
function unchanged(p, pass) {
  assert.equal(existsSync(join(p.root, ".kit", "render")), false, "a refusal created .kit/render");
  assert.equal(existsSync(join(p.root, ".kit", "start.lock")), false, "a refusal left the start lock");
  if (pass !== undefined) assert.equal(checkResult(p.root), pass);
}

// --- the start path's refusals ----------------------------------------------------

test("render refuses without a passing check: none, a failed one, and a 5.1 pass without hash", async (t) => {
  const p = await fixtureProject(t);
  rmSync(join(p.root, ".kit"), { recursive: true });
  assert.deepEqual(await kit(["render"], p), refused(NO_PASS));
  unchanged(p);
  plantCheck(p.root, { outcome: "failed", reasons: ["hook: one"], count: 1 });
  assert.deepEqual(await kit(["render"], p), refused(NO_PASS));
  unchanged(p);
  plantCheck(p.root, { outcome: "passed", reasons: [] });
  assert.deepEqual(await kit(["render"], p), refused(NO_PASS));
  unchanged(p);
});

test("render refuses a project changed since the pass: src/, a voice WAV, an invalid script, a missing custom file", async (t) => {
  const edits = [
    ["an edited src/ file", (root) => appendFileSync(join(root, "src", "custom", "Orbit.tsx"), "// edited\n")],
    ["an edited public/voice/<stems[0]>.wav", (root) => appendFileSync(join(root, "public", "voice", `${STEMS[0]}.wav`), "x")],
    ["a script.json that no longer validates", (root) => writeFileSync(join(root, "script.json"), JSON.stringify({ version: 1 }))],
    ["a missing custom file", (root) => rmSync(join(root, "src", "custom", "Orbit.tsx"))],
    ["a timeline.json that is not JSON", (root) => writeFileSync(join(root, "timeline.json"), "{")],
    ["a missing timeline.json", (root) => rmSync(join(root, "timeline.json"))],
  ];
  for (const [name, edit] of edits) {
    const p = await fixtureProject(t);
    const pass = checkResult(p.root);
    edit(p.root);
    assert.deepEqual(await kit(["render"], p), refused(CHANGED_SINCE), name);
    unchanged(p, pass);
  }
});

test("render refuses while a check or a render runs", async (t) => {
  const p = await fixtureProject(t);
  const pass = checkResult(p.root);
  const renderToken = "d4".repeat(16);
  const renderPid = liveJob(t, renderToken);
  plantJob(p.root, "render", { token: renderToken, pid: renderPid, startedAt: Date.now() });
  const job = readFileSync(join(p.root, ".kit", "render", "job.json"), "utf8");
  assert.deepEqual(await kit(["render"], p), refused(`a render is already running (pid ${renderPid}); run node bin/kit.mjs status`));
  assert.equal(readFileSync(join(p.root, ".kit", "render", "job.json"), "utf8"), job);
  assert.equal(checkResult(p.root), pass);

  const q = await fixtureProject(t);
  const checkToken = "e5".repeat(16);
  const checkPid = liveJob(t, checkToken);
  plantJob(q.root, "check", { token: checkToken, pid: checkPid, startedAt: Date.now() });
  assert.deepEqual(await kit(["render"], q), refused(`a check is already running (pid ${checkPid}); run node bin/kit.mjs status`));
  unchanged(q);
});

test("render refuses a project whose name is outside PROJECT_NAME", async (t) => {
  const p = await fixtureProject(t, { name: "My_Video" });
  const pass = checkResult(p.root);
  assert.deepEqual(await kit(["render"], p), refused(`${p.root}: not a kit project; run kit new`));
  unchanged(p, pass);
});

test("render refuses a symlinked .kit and a symlinked .kit/render", async (t) => {
  const p = await fixtureProject(t);
  renameSync(join(p.root, ".kit"), join(p.base, "kit-real"));
  symlinkSync(join(p.base, "kit-real"), join(p.root, ".kit"));
  assert.deepEqual(await kit(["render"], p), refused(".kit: is a symlink"));
  assert.deepEqual(readdirSync(join(p.base, "kit-real")), ["check"]);

  const q = await fixtureProject(t);
  mkdirSync(join(q.base, "render-real"));
  symlinkSync(join(q.base, "render-real"), join(q.root, ".kit", "render"));
  assert.deepEqual(await kit(["render"], q), refused(".kit/render: is a symlink"));
  assert.equal(readlinkSync(join(q.root, ".kit", "render")), join(q.base, "render-real"));
  assert.deepEqual(readdirSync(join(q.base, "render-real")), []);
});

test("render refuses while the start lock is held by a live pid", async (t) => {
  const p = await fixtureProject(t);
  const pass = checkResult(p.root);
  const lock = JSON.stringify({ pid: process.pid, startedAt: Date.now() });
  writeFileSync(join(p.root, ".kit", "start.lock"), lock);
  assert.deepEqual(await kit(["render"], p), refused(LOCK_HELD(process.pid)));
  assert.equal(readFileSync(join(p.root, ".kit", "start.lock"), "utf8"), lock);
  assert.equal(existsSync(join(p.root, ".kit", "render")), false);
  assert.equal(checkResult(p.root), pass);
});

test("render at the template root and with an extra argument are usage errors, exit 2", async (t) => {
  const p = await fixtureProject(t);
  const template = join(p.base, "remotion-template");
  mkdirSync(join(template, "node_modules"), { recursive: true });
  assert.deepEqual(
    await kit(["render"], p, template),
    refused(`run it inside a project: cd ${p.videos}/<name> && node bin/kit.mjs render`, 2),
  );
  assert.equal(existsSync(join(template, ".kit")), false);
  assert.deepEqual(await kit(["render", "x"], p), refused("usage: node bin/kit.mjs render", 2));
  unchanged(p);
});

test("render takes over a dead pid's lock, returns at once, and a worker that cannot load Remotion ends failed", async (t) => {
  const p = await fixtureProject(t, { worker: true });
  const pass = checkResult(p.root);
  const gone = spawnSync(process.execPath, ["-e", "process.stdout.write(String(process.pid))"], { encoding: "utf8" });
  writeFileSync(join(p.root, ".kit", "start.lock"), JSON.stringify({ pid: Number(gone.stdout), startedAt: Date.now() }));
  const started = Date.now();
  const result = await kit(["render"], p);
  assert.ok(Date.now() - started < 5000, `${Date.now() - started} ms`);
  assert.equal(result.code, 0, result.err);
  assert.equal(result.out, "");
  const pid = Number(STARTED.exec(result.err)?.[1]);
  assert.ok(pid > 0, result.err);
  t.after(() => {
    try {
      process.kill(pid, "SIGKILL");
    } catch {
      // ESRCH: the worker has already exited.
    }
  });
  assert.equal(existsSync(join(p.root, ".kit", "start.lock")), false, "the start left its lock");
  let status;
  for (const begun = Date.now(); Date.now() - begun < 30000; await pause(50)) {
    status = await kit(["status"], p);
    if (status.code !== 3) break;
  }
  assert.equal(status.code, 1, status.out);
  assert.match(status.out, /^kit status: render failed with exit 1 \(1 reasons\)\n {2}render: Cannot find (package|module) '[^']+'/);
  assert.equal(status.out.split("\n").filter(Boolean).length, 2);
  const log = readFileSync(join(p.root, ".kit", "render", "log"), "utf8");
  assert.equal(log, "render: start\nrender: done failed\n");
  // A relaunch reuses the pass: render never touches .kit/check/.
  assert.equal(checkResult(p.root), pass);
});

// --- runRender with the stub render ---------------------------------------------

// The worker's inputs over a fixture project; .kit/render/ exists as
// startJob leaves it. stub: the variant the stub render copies.
async function worker(t, { stub = "good", edit = null, ...options } = {}) {
  const p = await fixtureProject(t);
  mkdirSync(join(p.root, ".kit", "render", "tmp"), { recursive: true });
  const lines = [];
  const calls = [];
  const render = async (args) => {
    calls.push(args);
    copyFileSync(video(stub), args.output);
    if (edit !== null) edit(p.root);
  };
  const { runRender } = await rendering();
  const go = () => runRender(p.root, { token: TOKEN, filesRoot: p.files, render, log: (line) => lines.push(line), ...options });
  return { p, lines, calls, go, source: join(p.root, ".kit", "render", `${TOKEN}.mp4`) };
}

const resultOf = (root) => readJson(join(root, ".kit", "render", "result.json"));
const partials = (files) => readdirSync(files).filter((name) => name.endsWith(".partial"));

function expectFailed(w, reasons) {
  const result = resultOf(w.p.root);
  assert.deepEqual({ ...result, finishedAt: 0 }, { version: 1, token: TOKEN, outcome: "failed", reasons, finishedAt: 0, count: reasons.length, exitCode: 1 });
}

test("success: the target holds the render, no partial, the source is gone, and the result is succeeded", async (t) => {
  const w = await worker(t);
  writeFileSync(w.p.target, PREVIOUS);
  assert.equal(await w.go(), 0);
  assert.deepEqual(readFileSync(w.p.target), readFileSync(video("good")));
  assert.deepEqual(partials(w.p.files), []);
  assert.equal(existsSync(w.source), false);
  const result = resultOf(w.p.root);
  assert.deepEqual(
    { ...result, finishedAt: 0 },
    { version: 1, token: TOKEN, outcome: "succeeded", reasons: [], finishedAt: 0, count: 0, exitCode: 0, target: w.p.target, frames: TOTAL },
  );
  assert.deepEqual(w.lines, ["render: start", "render: verify", "render: publish", "render: done succeeded"]);
  assert.equal(w.calls.length, 1);
  assert.equal(w.calls[0].root, w.p.root);
  assert.equal(w.calls[0].output, w.source);
});

test("the stub's input props are JSON.parse of the hashed texts, not validateScript's output", async (t) => {
  const w = await worker(t);
  // A raw script.json without the end card's narration and the voice, which
  // validateScript fills in; checked again, so the pass covers this text.
  const raw = JSON.parse(readFileSync(join(w.p.root, "script.json"), "utf8"));
  delete raw.endCard.narration;
  delete raw.voice;
  const text = JSON.stringify(raw);
  writeFileSync(join(w.p.root, "script.json"), text);
  plantCheck(w.p.root, { outcome: "passed", reasons: [], count: 0, hash: await currentHash(w.p.root), sheet: ".kit/check/contact.png", tiles: [] });
  assert.equal(await w.go(), 0);
  const { script, timeline } = w.calls[0];
  assert.deepEqual(script, JSON.parse(text));
  assert.notDeepEqual(script, validateScript(text).script);
  assert.deepEqual(timeline, JSON.parse(readFileSync(join(w.p.root, "timeline.json"), "utf8")));
});

test("the partial is .<name>.mp4.<token>.partial in the files directory, written with the copy", async (t) => {
  const seen = [];
  const w = await worker(t, {
    copy: async (from, to) => {
      seen.push(to);
      copyFileSync(from, to);
    },
  });
  assert.equal(await w.go(), 0);
  assert.deepEqual(seen, [join(w.p.files, `.demo.mp4.${TOKEN}.partial`)]);
  assert.ok(seen[0].endsWith(".partial"));
});

test("each verify variant gives its reason and leaves the previous target byte-identical", async (t) => {
  const needs = seconds(TOTAL);
  const cases = [
    ["mpeg4", ["verify: video codec is mpeg4, expected h264"]],
    ["yuv444p", ["verify: pixel format is yuv444p, expected yuv420p"]],
    ["silent", ["verify: no AAC audio stream"]],
    [
      "short",
      [`verify: ${TOTAL - 1} video frames, the timeline has ${TOTAL}`, `verify: the video stream lasts ${seconds(TOTAL - 1)} s, the timeline needs ${needs} s`],
    ],
  ];
  for (const [stub, reasons] of cases) {
    const w = await worker(t, { stub });
    writeFileSync(w.p.target, PREVIOUS);
    assert.equal(await w.go(), 1, stub);
    expectFailed(w, reasons);
    assert.deepEqual(readFileSync(w.p.target), PREVIOUS, stub);
    assert.deepEqual(partials(w.p.files), [], stub);
    assert.deepEqual(w.lines, ["render: start", "render: verify", "render: done failed"], stub);
  }
});

test("audio 0.5 s short gives the audio reason", async (t) => {
  const w = await worker(t, { stub: "quiet" });
  writeFileSync(w.p.target, PREVIOUS);
  assert.equal(await w.go(), 1);
  const { reasons } = resultOf(w.p.root);
  assert.equal(reasons.length, 1, JSON.stringify(reasons));
  const match = /^verify: the audio lasts (\d+\.\d+) s, the timeline needs (\d+\.\d+) s$/.exec(reasons[0]);
  assert.ok(match, reasons[0]);
  assert.equal(match[2], seconds(TOTAL));
  assert.ok(Number(match[1]) < TOTAL / 30 - 1 / 30, match[1]);
  assert.deepEqual(readFileSync(w.p.target), PREVIOUS);
});

test("a stub that edits a src/ file, or deletes script.json, gives the changed-during reason with the target unchanged", async (t) => {
  const edits = [
    ["src/ edited", (root) => appendFileSync(join(root, "src", "custom", "Orbit.tsx"), "// edited while rendering\n")],
    ["script.json deleted", (root) => rmSync(join(root, "script.json"))],
  ];
  for (const [name, edit] of edits) {
    const w = await worker(t, { edit });
    writeFileSync(w.p.target, PREVIOUS);
    assert.equal(await w.go(), 1, name);
    expectFailed(w, [CHANGED_DURING]);
    assert.deepEqual(readFileSync(w.p.target), PREVIOUS, name);
    assert.deepEqual(partials(w.p.files), [], name);
  }
});

test("the worker re-reads the pass and the project: no pass, or a changed project, is its reason", async (t) => {
  const w = await worker(t);
  rmSync(join(w.p.root, ".kit", "check"), { recursive: true });
  assert.equal(await w.go(), 1);
  expectFailed(w, [NO_PASS]);
  assert.equal(w.calls.length, 0);
  const v = await worker(t);
  appendFileSync(join(v.p.root, "public", "voice", `${STEMS[0]}.wav`), "x");
  assert.equal(await v.go(), 1);
  expectFailed(v, [CHANGED_SINCE]);
  assert.equal(v.calls.length, 0);
});

test("a render that throws is render: <message>", async (t) => {
  const w = await worker(t, {
    render: async () => {
      throw new Error("the browser closed");
    },
  });
  writeFileSync(w.p.target, PREVIOUS);
  assert.equal(await w.go(), 1);
  expectFailed(w, ["render: the browser closed"]);
  assert.deepEqual(readFileSync(w.p.target), PREVIOUS);
});

test("a stale .<name>.mp4.<32 hex>.partial is removed; .<name>.mp4.notes.partial is not", async (t) => {
  const w = await worker(t);
  const stale = join(w.p.files, `.demo.mp4.${"0f".repeat(16)}.partial`);
  const notes = join(w.p.files, ".demo.mp4.notes.partial");
  const other = join(w.p.files, `.other.mp4.${"0f".repeat(16)}.partial`);
  for (const path of [stale, notes, other]) writeFileSync(path, "left over");
  assert.equal(await w.go(), 0);
  assert.equal(existsSync(stale), false);
  assert.equal(readFileSync(notes, "utf8"), "left over");
  assert.equal(readFileSync(other, "utf8"), "left over");
});

test("a symlink or a directory at the target gives its reason and is left as it was", async (t) => {
  const w = await worker(t);
  writeFileSync(join(w.p.files, "elsewhere.mp4"), PREVIOUS);
  symlinkSync(join(w.p.files, "elsewhere.mp4"), w.p.target);
  assert.equal(await w.go(), 1);
  expectFailed(w, [`publish: ${w.p.target} is a symlink; not replaced`]);
  assert.equal(readlinkSync(w.p.target), join(w.p.files, "elsewhere.mp4"));
  assert.deepEqual(readFileSync(join(w.p.files, "elsewhere.mp4")), PREVIOUS);
  assert.deepEqual(partials(w.p.files), []);

  const v = await worker(t);
  mkdirSync(v.p.target);
  writeFileSync(join(v.p.target, "inside"), "kept");
  assert.equal(await v.go(), 1);
  expectFailed(v, [`publish: ${v.p.target} is not a regular file; not replaced`]);
  assert.equal(readFileSync(join(v.p.target, "inside"), "utf8"), "kept");
  assert.deepEqual(partials(v.p.files), []);
});

test("a copy that writes half and throws is publish: <message>, with the target byte-identical and no partial", async (t) => {
  const w = await worker(t, {
    copy: async (from, to) => {
      const bytes = readFileSync(from);
      writeFileSync(to, bytes.subarray(0, bytes.length >> 1));
      throw new Error("ENOSPC: no space left on device, write");
    },
  });
  writeFileSync(w.p.target, PREVIOUS);
  assert.equal(await w.go(), 1);
  expectFailed(w, ["publish: ENOSPC: no space left on device, write"]);
  assert.deepEqual(readFileSync(w.p.target), PREVIOUS);
  assert.deepEqual(partials(w.p.files), []);
});

test("a stop that arrives during the copy publishes nothing and writes no result", async (t) => {
  let stop = false;
  let asked = 0;
  const w = await worker(t, {
    stopRequested: () => {
      asked++;
      return stop;
    },
    copy: async (from, to) => {
      copyFileSync(from, to);
      stop = true;
    },
  });
  writeFileSync(w.p.target, PREVIOUS);
  assert.equal(await w.go(), null);
  assert.equal(asked, 2, "asked before publishing and again before the rename");
  assert.deepEqual(readFileSync(w.p.target), PREVIOUS);
  assert.deepEqual(partials(w.p.files), []);
  assert.equal(existsSync(join(w.p.root, ".kit", "render", "result.json")), false);
});

test("a stop before publishing returns without copying or writing a result", async (t) => {
  let copied = false;
  const w = await worker(t, {
    stopRequested: () => true,
    copy: async () => {
      copied = true;
    },
  });
  writeFileSync(w.p.target, PREVIOUS);
  assert.equal(await w.go(), null);
  assert.equal(copied, false);
  assert.deepEqual(readFileSync(w.p.target), PREVIOUS);
  assert.equal(existsSync(join(w.p.root, ".kit", "render", "result.json")), false);
});

test("a source removal that throws after the rename still ends succeeded, with the log line", async (t) => {
  const w = await worker(t, {
    copy: async (from, to) => {
      copyFileSync(from, to);
      // The source becomes a non-empty directory, which unlink refuses.
      rmSync(from);
      mkdirSync(join(from, "kept"), { recursive: true });
    },
  });
  assert.equal(await w.go(), 0);
  assert.deepEqual(readFileSync(w.p.target), readFileSync(video("good")));
  assert.equal(resultOf(w.p.root).outcome, "succeeded");
  const removal = w.lines.find((line) => line.startsWith("render: could not remove "));
  assert.ok(removal?.startsWith(`render: could not remove ${w.source}: `), w.lines.join("\n"));
  assert.ok(lstatSync(w.source).isDirectory());
});

// --- status's render lines ---------------------------------------------------------

const renderToken = "b8".repeat(16);

test("status reports a render: running, succeeded, failed with its exit, died", async (t) => {
  const p = await fixtureProject(t);
  const live = liveJob(t, renderToken);
  plantJob(p.root, "render", { token: renderToken, pid: live, startedAt: Date.now() - 5500 });
  assert.deepEqual(await kit(["status"], p), { code: 3, out: `kit status: render running (pid ${live}, 5 s)\n`, err: "" });

  const target = "/mnt/user-data/files/demo.mp4";
  const done = { token: renderToken, reasons: [], count: 0 };
  plantJob(p.root, "render", { token: renderToken, pid: 4242, startedAt: 5000 }, { ...done, outcome: "succeeded", exitCode: 0, target, frames: TOTAL });
  assert.deepEqual(await kit(["status"], p), { code: 0, out: `kit status: render succeeded: ${target}\n`, err: "" });

  const shown = [...Array.from({ length: 40 }, (_, i) => `verify: reason ${i + 1}`), "… and 3 more"];
  plantJob(p.root, "render", { token: renderToken, pid: 4242, startedAt: 5000 }, { token: renderToken, outcome: "failed", reasons: shown, count: 43, exitCode: 1 });
  assert.deepEqual(await kit(["status"], p), {
    code: 1,
    out: `kit status: render failed with exit 1 (43 reasons)\n${shown.map((reason) => `  ${reason}\n`).join("")}`,
    err: "",
  });

  const gone = spawnSync(process.execPath, ["-e", "process.stdout.write(String(process.pid))"], { encoding: "utf8" });
  plantJob(p.root, "render", { token: renderToken, pid: Number(gone.stdout), startedAt: 5000 });
  assert.deepEqual(await kit(["status"], p), {
    code: 1,
    out: `kit status: render died without an exit (pid ${gone.stdout}); see .kit/render/log\n`,
    err: "",
  });
});

test("status reports the job started last: a render after a check, then a check after a render", async (t) => {
  const p = await fixtureProject(t);
  plantCheck(p.root, { outcome: "failed", reasons: ["hook: one"], count: 1 }, 1000);
  plantJob(p.root, "render", { token: renderToken, pid: 4242, startedAt: 2000 }, { token: renderToken, outcome: "succeeded", reasons: [], count: 0, exitCode: 0, target: "/t/demo.mp4", frames: 1 });
  assert.deepEqual(await kit(["status"], p), { code: 0, out: "kit status: render succeeded: /t/demo.mp4\n", err: "" });
  plantCheck(p.root, { outcome: "failed", reasons: ["hook: one"], count: 1 }, 3000);
  assert.deepEqual(await kit(["status"], p), { code: 1, out: "kit status: check failed (1 reasons)\n  hook: one\n", err: "" });
});

test("status with only a render reports it; with neither job, the none line", async (t) => {
  const p = await fixtureProject(t);
  rmSync(join(p.root, ".kit"), { recursive: true });
  assert.deepEqual(await kit(["status"], p), { code: 4, out: "kit status: no check has run; run node bin/kit.mjs check\n", err: "" });
  plantJob(p.root, "render", { token: renderToken, pid: 4242, startedAt: 2000 }, { token: renderToken, outcome: "failed", reasons: ["render: x"], count: 1, exitCode: 1 });
  assert.deepEqual(await kit(["status"], p), { code: 1, out: "kit status: render failed with exit 1 (1 reasons)\n  render: x\n", err: "" });
});

test("the usage list holds render", async (t) => {
  const p = await fixtureProject(t);
  const none = await kit([], p);
  assert.equal(none.code, 2);
  assert.ok(none.err.split("\n").includes("kit: usage: node bin/kit.mjs render"), none.err);
});
