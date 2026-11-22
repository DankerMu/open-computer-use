// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// kit check and kit status in a real headless shell (task 5.1, in-image
// evidence 1–9; task 5.2, in-image evidence 1–7 of the contact sheet, the
// checklist and the passing-check hash). Runs only inside the workspace
// image, from a project root
// whose node_modules link reaches /opt/remotion-template/node_modules, one
// test file after another:
//   node --test --test-concurrency=1 "tests/render/*.test.mjs"
// CI's tests/*.test.mjs glob does not match this file.
//
// The passing project is a scratch copy of the template with the fixture
// registry (3.8's fixture scenes plus the custom loader), 3.8's Orbit custom
// scene, the pure fixture's script (tests/fixtures/check/fixture.mjs), one
// 7 s tone WAV per block, and timeline.json and narration.wav written by the
// project's own `kit timeline`. Each failing variant is a copy that changes
// one thing. Every worker a test starts has ended, and no headless shell is
// left, when the test ends: tests/render/probe.test.mjs counts every shell
// on the machine.
//
// Task 5.4's two timing tests, the maximum-size script's kit check and then
// its kit render, run only with KIT_TIMING=1, under --cpus=2 --memory=4g
// --memory-swap=4g and the /mnt/user-data/files tmpfs, and are skipped when
// KIT_TIMING is unset.

import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { bundle } from "@remotion/bundler";
import { openBrowser, renderStill, selectComposition } from "@remotion/renderer";
import { execFileSync, spawn, spawnSync } from "node:child_process";
import {
  appendFileSync,
  copyFileSync,
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  realpathSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { release, tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { sheetTiles } from "../../bin/contact-sheet.mjs";
import { explainerScenes } from "../../bin/explainer-scenes.mjs";
import { validateScript } from "../../bin/script-schema.mjs";
import { blockStems } from "../../bin/voice-stem.mjs";
import { TONE_SECONDS, checkScript, checkTimeline, maxScript } from "../fixtures/check/fixture.mjs";

const TEMPLATE = fileURLToPath(new URL("../..", import.meta.url));
const FIXTURES = join(TEMPLATE, "tests", "render", "fixtures");
const CUSTOM = "chapter 2 原理, scene 1 (custom)";
const QUESTION = "chapter 1 现象, scene 1 (question)";
const CONCEPT = "chapter 3 应用, scene 1 (concept-card)";
const CHANGED = "the project changed while the check ran; run node bin/kit.mjs check again";
// The tiles of the passing script (5.2 fixture): its scenes 0, 1, 3 and 4.
const TILE_SCENES = [0, 1, 3, 4];
const TILE_WHERES = ["hook", QUESTION, CONCEPT, "summary"];
const CORNERS = ["top left", "top right", "bottom left", "bottom right"];
// Per channel, between the sheet's quadrant and the test's own still.
const TILE_TOLERANCE = 2;
const BLOCK_SECONDS = 7;
const CHECK_LIMIT_MS = 900000;
const SHELL_LIMIT_MS = 10000;
const STARTED = /^kit check: started \(pid (\d+)\); run node bin\/kit\.mjs status\n$/;
const RENDER_STARTED = /^kit render: started \(pid (\d+)\); run node bin\/kit\.mjs status\n$/;

// Task 5.4: the timing tests' gate, limits and bounds.
const KIT_TIMING = process.env.KIT_TIMING;
if (KIT_TIMING !== undefined && KIT_TIMING !== "1") throw new Error("KIT_TIMING must be unset or 1");
const TIMING_SKIP = KIT_TIMING === "1" ? false : "5.4 timing: set KIT_TIMING=1 and run under --cpus=2 --memory=4g --memory-swap=4g";
const MAX_CHECK_LIMIT_MS = 1800000;
const MAX_RENDER_LIMIT_MS = 3600000;
const POLL_MS = 1000;
const STATUS_BOUND_MS = 5000;
const FILES = "/mnt/user-data/files";
const CGROUP = "/sys/fs/cgroup";
// The stage a log line names, or null: the check's stage lines and its
// snapshot; the render's start, frames (any count), verify and publish.
const checkStage = (line) => /^check: (stage \d+ \S+|snapshot)$/.exec(line)?.[1] ?? null;
const renderStage = (line) => /^render: (start|frames|verify|publish)(?: |$)/.exec(line)?.[1] ?? null;

const pause = (ms) => new Promise((done) => setTimeout(done, ms));
const scratch = mkdtempSync(join(tmpdir(), "check-render-"));
const workers = new Set();
let passing;

// Live headless shells, as tests/render/probe.test.mjs scans them: a zombie
// has an empty cmdline.
function headlessShells() {
  return readdirSync("/proc")
    .filter((name) => /^\d+$/.test(name))
    .filter((pid) => {
      try {
        return readFileSync(`/proc/${pid}/cmdline`, "utf8").includes("chrome-headless-shell");
      } catch {
        return false; // the process ended between readdir and read
      }
    });
}

async function until(check, timeoutMs) {
  const started = Date.now();
  while (!check()) {
    if (Date.now() - started > timeoutMs) return false;
    await pause(100);
  }
  return true;
}

const alive = (pid) => {
  try {
    process.kill(pid, 0);
    return readFileSync(`/proc/${pid}/stat`, "utf8").split(") ")[1][0] !== "Z";
  } catch {
    return false;
  }
};

const kit = (root, ...args) => {
  const run = spawnSync(process.execPath, ["bin/kit.mjs", ...args], { cwd: root, encoding: "utf8" });
  return { code: run.status, out: run.stdout, err: run.stderr };
};

// Starts a check, or with job "render" a render; returns the worker's pid,
// recorded for the final cleanup.
function startCheck(root, job = "check") {
  const started = Date.now();
  const run = kit(root, job);
  const elapsed = Date.now() - started;
  assert.equal(run.code, 0, run.err);
  assert.equal(run.out, "");
  const match = (job === "render" ? RENDER_STARTED : STARTED).exec(run.err);
  assert.ok(match, run.err);
  const pid = Number(match[1]);
  workers.add(pid);
  return { pid, elapsed };
}

// Polls status until it reports anything but running, reading the log of
// `job` after each status call. seen, when given, maps each log line to the
// ms (since `since`, by default the call) it was first seen. everyMs is the
// pause between polls. calls, when given, receives every status call as
// {stage, at, ms, code, out}: the stage last seen in the log before the
// call (stageOf names a line's stage, or null), the ms since `since` at
// which it returned, and its latency. onPoll(stage) runs after each running
// poll, with the stage last seen in the log.
async function settled(
  root,
  timeoutMs = CHECK_LIMIT_MS,
  seen = null,
  { job = "check", everyMs = seen === null ? 500 : 100, since = Date.now(), calls = null, stageOf = () => null, onPoll = () => {} } = {},
) {
  const started = Date.now();
  let stage = "before any stage";
  for (;;) {
    const called = Date.now();
    const status = kit(root, "status");
    const returned = Date.now();
    calls?.push({ stage, at: returned - since, ms: returned - called, code: status.code, out: status.out });
    if (seen !== null || calls !== null) {
      for (const line of readLog(root, job).split("\n")) {
        if (line === "") continue;
        if (seen !== null && !seen.has(line)) seen.set(line, Date.now() - since);
        stage = stageOf(line) ?? stage;
      }
    }
    if (status.code !== 3) return { ...status, ms: Date.now() - started };
    onPoll(stage);
    assert.ok(Date.now() - started < timeoutMs, `still running after ${timeoutMs} ms`);
    await pause(everyMs);
  }
}

// Waits until the log holds `line` and a headless shell is up (with
// `shell`), failing at once when the check stops running.
async function reaches(root, line, shell) {
  const ok = () => readLog(root).includes(`${line}\n`) && (!shell || headlessShells().length > 0);
  await until(() => ok() || kit(root, "status").code !== 3, CHECK_LIMIT_MS - 60000);
  assert.ok(ok(), `never reached ${line}${shell ? " with a shell" : ""}: ${kit(root, "status").out}\n${readLog(root)}`);
}

const readLog = (root, job = "check") => {
  try {
    return readFileSync(join(root, ".kit", job, "log"), "utf8");
  } catch {
    return ""; // before startJob has opened the log
  }
};

const sheetPath = (root) => join(root, ".kit", "check", "contact.png");

// status's passed output at root. tiles: [{where, frame}], by default the
// passing script's, from timeline.json's transition windows: a tile's frame
// is the frame before its scene's exit.
async function passedOutput(root, tiles = null) {
  const { CHECKLIST } = await import("../../bin/checklist.mjs");
  const timeline = JSON.parse(readFileSync(join(root, "timeline.json"), "utf8"));
  const shown = tiles ?? TILE_SCENES.map((k, i) => ({ where: TILE_WHERES[i], frame: timeline.scenes[k].transitionOut.startFrame - 1 }));
  return [
    "kit status: check passed",
    `contact sheet: ${sheetPath(root)}`,
    ...shown.map(({ where, frame }, i) => `  tile ${i + 1} (${CORNERS[i]}): ${where}, frame ${frame}`),
    "view the contact sheet and answer each question yes or no; on any no, fix the project and run node bin/kit.mjs check again:",
    ...CHECKLIST.map((question, n) => `  ${n + 1}. ${question}`),
  ]
    .map((line) => `${line}\n`)
    .join("");
}

// An image file decoded to raw rgb24, optionally cropped.
function rgb(path, crop = null) {
  const filter = crop === null ? [] : ["-vf", `crop=${crop}`];
  return execFileSync("ffmpeg", ["-nostdin", "-v", "error", "-i", path, ...filter, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], {
    maxBuffer: 64 * 1024 * 1024,
  });
}

// Ends the test with the worker gone and no shell left.
async function ended(pid) {
  assert.ok(await until(() => !alive(pid), 60000), `worker ${pid} still alive`);
  assert.ok(await until(() => headlessShells().length === 0, 20000), `shells left: ${headlessShells().join(", ")}`);
}

const log = (root) => readFileSync(join(root, ".kit", "check", "log"), "utf8");

// A copy of the passing project, with node_modules linked again.
function variant(name, edit = () => {}) {
  const root = join(scratch, name);
  cpSync(passing, root, { recursive: true, verbatimSymlinks: true, filter: (src) => !src.includes(`${passing}/.kit`) });
  edit(root);
  return root;
}

before(() => {
  passing = join(scratch, "passing");
  mkdirSync(passing);
  for (const part of ["src", "bin", join("tests", "render", "fixtures")]) {
    cpSync(join(TEMPLATE, part), join(passing, part), { recursive: true });
  }
  for (const name of ["package.json", "tsconfig.json", "remotion.config.ts"]) copyFileSync(join(TEMPLATE, name), join(passing, name));
  symlinkSync(realpathSync(join(TEMPLATE, "node_modules")), join(passing, "node_modules"));
  copyFileSync(join(FIXTURES, "check", "registry.ts"), join(passing, "src", "kit", "render", "registry.ts"));
  copyFileSync(join(FIXTURES, "explainer", "Orbit.tsx"), join(passing, "src", "custom", "Orbit.tsx"));
  const script = checkScript();
  writeFileSync(join(passing, "script.json"), `${JSON.stringify(script, null, 2)}\n`);
  mkdirSync(join(passing, "public", "voice"), { recursive: true });
  for (const { stem } of blockStems(script)) {
    execFileSync("ffmpeg", [
      "-nostdin", "-v", "error",
      "-f", "lavfi", "-i", `sine=frequency=440:sample_rate=48000:duration=${BLOCK_SECONDS}`,
      "-ac", "1", "-c:a", "pcm_s16le",
      join(passing, "public", "voice", `${stem}.wav`),
    ]);
  }
  const laid = kit(passing, "timeline");
  assert.equal(laid.code, 0, laid.err);
  const timeline = JSON.parse(readFileSync(join(passing, "timeline.json"), "utf8"));
  assert.ok(timeline.totalFrames >= 1800, `${timeline.totalFrames} frames`);
});

after(() => {
  for (const pid of workers) {
    try {
      process.kill(pid, "SIGKILL");
    } catch {
      // ESRCH: the worker has already exited, the usual case.
    }
  }
  rmSync(scratch, { recursive: true, force: true });
});

test("1, 2, 5: check returns at once, a second check is refused, and the run passes", { timeout: CHECK_LIMIT_MS }, async (t) => {
  assert.deepEqual(headlessShells(), [], "no shell before the run");
  const { pid, elapsed } = startCheck(passing);
  t.diagnostic(`check returned in ${elapsed} ms`);
  assert.ok(elapsed < 5000, `${elapsed} ms`);
  const running = kit(passing, "status");
  assert.equal(running.code, 3, running.out);
  assert.match(running.out, new RegExp(`^kit status: check running \\(pid ${pid}, \\d+ s\\)\\n$`));
  const second = kit(passing, "check");
  assert.deepEqual(second, { code: 1, out: "", err: `kit check: a check is already running (pid ${pid}); run node bin/kit.mjs status\n` });
  const seen = new Map();
  const done = await settled(passing, CHECK_LIMIT_MS, seen);
  t.diagnostic(`passing check took ${done.ms + elapsed} ms`);
  const sheetStage = seen.get("check: stage 9 hash") - seen.get("check: stage 8 contact-sheet");
  t.diagnostic(`sheet stage (stage 8 line to stage 9 line, 100 ms polls) took ${sheetStage} ms`);
  assert.deepEqual([done.code, done.out], [0, await passedOutput(passing)], log(passing));
  const lines = log(passing).split("\n");
  for (const line of [
    "check: stage 1 schema",
    "check: stage 2 timeline",
    "check: snapshot",
    "check: stage 3 assets",
    "check: stage 4 unnarrated",
    "check: stage 5 rest",
    "check: stage 6 music",
    "check: stage 7 probe",
    "check: stage 8 contact-sheet",
    "check: stage 9 hash",
    "check: done passed",
  ]) {
    assert.ok(lines.includes(line), `${line} not in the log`);
  }
  await ended(pid);
});

test("5.2 evidence 1: the pass leaves one 1920×1080 sheet whose quadrants are the tiles' stills in order", { timeout: CHECK_LIMIT_MS }, async (t) => {
  // tmp/: the worker's TMPDIR, which task 5.3's startJob creates.
  assert.deepEqual(readdirSync(join(passing, ".kit", "check")).sort(), ["contact.png", "job.json", "log", "result.json", "tmp"]);
  const probed = JSON.parse(
    execFileSync("ffprobe", ["-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height", "-of", "json", sheetPath(passing)], {
      encoding: "utf8",
    }),
  ).streams[0];
  assert.deepEqual(probed, { codec_name: "png", width: 1920, height: 1080 });
  const script = JSON.parse(readFileSync(join(passing, "script.json"), "utf8"));
  const timeline = JSON.parse(readFileSync(join(passing, "timeline.json"), "utf8"));
  const frames = TILE_SCENES.map((k) => timeline.scenes[k].transitionOut.startFrame - 1);
  const work = mkdtempSync(join(scratch, "stills-"));
  let browser = null;
  try {
    const serveUrl = await bundle({
      entryPoint: join(passing, "src", "kit", "index.ts"),
      outDir: join(work, "bundle"),
      enableCaching: false,
      publicDir: join(passing, "public"),
      symlinkPublicDir: true,
    });
    browser = await openBrowser("chrome");
    const inputProps = { script, timeline };
    const composition = await selectComposition({ serveUrl, id: "Explainer", inputProps, puppeteerInstance: browser });
    for (const [i, frame] of frames.entries()) {
      const still = join(work, `still-${i}.png`);
      await renderStill({ composition, serveUrl, inputProps, puppeteerInstance: browser, output: still, frame, imageFormat: "png", scale: 0.5 });
      const own = rgb(still);
      const quadrant = rgb(sheetPath(passing), `960:540:${(i % 2) * 960}:${Math.floor(i / 2) * 540}`);
      assert.equal(own.length, 960 * 540 * 3, `still ${i} is 960×540`);
      assert.equal(quadrant.length, own.length);
      let max = 0;
      for (let k = 0; k < own.length; k++) max = Math.max(max, Math.abs(own[k] - quadrant[k]));
      t.diagnostic(`tile ${i + 1} (${CORNERS[i]}, frame ${frame}): max channel difference ${max}`);
      assert.ok(max <= TILE_TOLERANCE, `tile ${i + 1} differs by ${max}`);
    }
  } finally {
    if (browser !== null) await browser.close({ silent: true });
    rmSync(work, { recursive: true, force: true });
  }
  assert.ok(await until(() => headlessShells().length === 0, 20000), `shells left: ${headlessShells().join(", ")}`);
});

test("5.2 evidence 7: the passed hash is projectHash's, survives writeCustomIndex, and a one-byte WAV edit changes it", async () => {
  const { projectHash } = await import("../../bin/project-hash.mjs");
  const { writeCustomIndex } = await import("../../bin/custom-index.mjs");
  const { validateScript } = await import("../../bin/script-schema.mjs");
  const result = JSON.parse(readFileSync(join(passing, ".kit", "check", "result.json"), "utf8"));
  const scriptText = readFileSync(join(passing, "script.json"), "utf8");
  const timelineText = readFileSync(join(passing, "timeline.json"), "utf8");
  const { script } = validateScript(scriptText);
  const hashNow = async () => (await projectHash(passing, { script, timeline: JSON.parse(timelineText), scriptText, timelineText })).hash;
  assert.match(result.hash, /^[0-9a-f]{64}$/);
  assert.equal(await hashNow(), result.hash);
  await writeCustomIndex(passing, script);
  assert.equal(await hashNow(), result.hash);
  const wav = join(passing, "public", "voice", `${blockStems(script)[0].stem}.wav`);
  const bytes = readFileSync(wav);
  const edited = Buffer.from(bytes);
  edited[edited.length - 1] ^= 1;
  writeFileSync(wav, edited);
  try {
    assert.notEqual(await hashNow(), result.hash);
  } finally {
    writeFileSync(wav, bytes); // the later variants copy this project
  }
  assert.equal(await hashNow(), result.hash);
});

test("9: after a pass, a new check killed before it finishes reports died, never the old pass", { timeout: CHECK_LIMIT_MS }, async () => {
  assert.equal(kit(passing, "status").code, 0, "the previous test left a pass");
  assert.ok(existsSync(sheetPath(passing)), "the pass left its sheet");
  const { pid } = startCheck(passing);
  process.kill(pid, "SIGKILL");
  await ended(pid);
  // 5.2 evidence 4: the killed run before its stage 8 leaves no sheet.
  assert.equal(existsSync(sheetPath(passing)), false, "the old sheet survived the new start");
  assert.deepEqual(kit(passing, "status"), {
    code: 1,
    out: `kit status: check died without an exit (pid ${pid}); see .kit/check/log\n`,
    err: "",
  });
});

test("3: a custom scene whose marked text crosses the safe area fails the check", { timeout: CHECK_LIMIT_MS }, async (t) => {
  const root = variant("overflow", (dir) => copyFileSync(join(FIXTURES, "check", "OrbitOverflow.tsx"), join(dir, "src", "custom", "Orbit.tsx")));
  const { pid } = startCheck(root);
  const done = await settled(root);
  t.diagnostic(done.out);
  assert.equal(done.code, 1, done.out);
  const [head, ...reasons] = done.out.split("\n").filter(Boolean);
  assert.equal(head, `kit status: check failed (${reasons.length} reasons)`);
  const overflow = new RegExp(`^ {2}${CUSTOM.replace(/[()]/g, "\\$&")} (entrance-end|midpoint|pre-exit) frame \\d+: outside safe area: `);
  assert.ok(reasons.length > 0 && reasons.every((line) => overflow.test(line)), done.out);
  assert.equal(existsSync(sheetPath(root)), false, "a failed check leaves no sheet");
  await ended(pid);
});

test("5.2 evidence 4: in one root, a pass, then the overflowing custom scene: the new check fails with no sheet", { timeout: CHECK_LIMIT_MS }, async () => {
  const root = variant("stale");
  const first = startCheck(root);
  const passed = await settled(root);
  assert.equal(passed.code, 0, passed.out);
  assert.ok(existsSync(sheetPath(root)), "the pass wrote its sheet");
  await ended(first.pid);
  copyFileSync(join(FIXTURES, "check", "OrbitOverflow.tsx"), join(root, "src", "custom", "Orbit.tsx"));
  const second = startCheck(root);
  const failed = await settled(root);
  assert.equal(failed.code, 1, failed.out);
  assert.match(failed.out, /outside safe area/);
  assert.equal(existsSync(sheetPath(root)), false, "the old sheet survived a failed check");
  await ended(second.pid);
});

// The kinds the tracked registry lists, read from its text: one
// `'<kind>': components['<id>']` line each (task 4.2).
const trackedKinds = () =>
  new Set(
    [...readFileSync(join(TEMPLATE, "src", "kit", "render", "registry.ts"), "utf8").matchAll(/^\s*'([a-z-]+)': components\['[a-z0-9-]+'\],?$/gm)].map(
      (m) => m[1],
    ),
  );
// The passing script's non-custom scenes in playback order, each with the
// place kit check names.
const PLAYBACK_KINDS = [
  ["hook", "hook"],
  ["question", QUESTION],
  ["concept-card", CONCEPT],
  ["summary", "summary"],
  ["end-card", "end-card"],
];

test("4: on the tracked registry the passing script passes every stage, or fails naming its first unregistered kind", { timeout: CHECK_LIMIT_MS }, async (t) => {
  const root = variant("tracked", (dir) => copyFileSync(join(TEMPLATE, "src", "kit", "render", "registry.ts"), join(dir, "src", "kit", "render", "registry.ts")));
  const missing = PLAYBACK_KINDS.find(([kind]) => !trackedKinds().has(kind));
  t.diagnostic(missing === undefined ? "every kind of the script is registered" : `first unregistered kind: ${missing[0]}`);
  const { pid } = startCheck(root);
  const done = await settled(root);
  if (missing === undefined) {
    assert.deepEqual([done.code, done.out], [0, await passedOutput(root)], log(root));
  } else {
    const [kind, where] = missing;
    assert.deepEqual([done.code, done.out], [1, `kit status: check failed (1 reasons)\n  ${where}: no component for kind "${kind}"\n`], log(root));
  }
  await ended(pid);
});

// Starts a check, waits for the probe stage and a shell, sends `signal`, and
// measures until no shell is left.
async function signalDuringProbe(t, name, signal) {
  const root = variant(name);
  const { pid } = startCheck(root);
  assert.ok(
    await until(() => log(root).includes("check: stage 7 probe\n") && headlessShells().length > 0, CHECK_LIMIT_MS - 60000),
    "the probe opened a shell",
  );
  const before = headlessShells().length;
  const sent = Date.now();
  process.kill(pid, signal);
  const gone = await until(() => headlessShells().length === 0, SHELL_LIMIT_MS);
  const ms = Date.now() - sent;
  t.diagnostic(`${signal}: ${before} shell(s) before the signal, none left after ${ms} ms`);
  assert.ok(gone, `shells left ${SHELL_LIMIT_MS} ms after ${signal}: ${headlessShells().join(", ")}`);
  await ended(pid);
  assert.deepEqual(kit(root, "status"), {
    code: 1,
    out: `kit status: check died without an exit (pid ${pid}); see .kit/check/log\n`,
    err: "",
  });
}

test("6: SIGTERM during the probe leaves no shell within 10 s, and status says died", { timeout: CHECK_LIMIT_MS }, async (t) => {
  await signalDuringProbe(t, "sigterm", "SIGTERM");
});

test("7: SIGKILL during the probe leaves no shell within 10 s, and status says died", { timeout: CHECK_LIMIT_MS }, async (t) => {
  await signalDuringProbe(t, "sigkill", "SIGKILL");
});

// 5.2 evidence 5: starts a check, waits for the sheet stage and its shell,
// sends `signal`, and measures until no shell is left; then the next start
// sweeps whatever the run left.
async function signalDuringSheet(t, name, signal) {
  const root = variant(name);
  const { pid } = startCheck(root);
  await reaches(root, "check: stage 8 contact-sheet", true);
  const before = headlessShells().length;
  const sent = Date.now();
  process.kill(pid, signal);
  const gone = await until(() => headlessShells().length === 0, SHELL_LIMIT_MS);
  const ms = Date.now() - sent;
  t.diagnostic(`${signal} in the sheet stage: ${before} shell(s) before the signal, none left after ${ms} ms`);
  assert.ok(gone, `shells left ${SHELL_LIMIT_MS} ms after ${signal}: ${headlessShells().join(", ")}`);
  await ended(pid);
  assert.deepEqual(kit(root, "status"), {
    code: 1,
    out: `kit status: check died without an exit (pid ${pid}); see .kit/check/log\n`,
    err: "",
  });
  const dir = join(root, ".kit", "check");
  const left = readdirSync(dir).sort();
  t.diagnostic(`${signal}: .kit/check/ after the death holds ${left.join(", ")}`);
  if (signal === "SIGKILL") {
    assert.equal(existsSync(sheetPath(root)), false, "a SIGKILLed sheet stage left contact.png");
    // Nothing ran the stage's finally, so its work directory is still there.
    assert.ok(left.includes("sheet"), "the killed stage's work directory");
  } else if (existsSync(sheetPath(root))) {
    t.diagnostic("SIGTERM landed after stage 9's rename; status says died and does not report the sheet");
  }
  const next = startCheck(root);
  const swept = readdirSync(dir);
  process.kill(next.pid, "SIGKILL");
  await ended(next.pid);
  for (const entry of ["sheet", "contact.next.png", "contact.png"]) assert.ok(!swept.includes(entry), `${entry} survived the next start`);
}

test("5.2 evidence 5: SIGTERM during the sheet leaves no shell within 10 s, status says died, and the next start sweeps", { timeout: CHECK_LIMIT_MS }, async (t) => {
  await signalDuringSheet(t, "sheet-sigterm", "SIGTERM");
});

test("5.2 evidence 5: SIGKILL during the sheet leaves no shell within 10 s, status says died, and the next start sweeps", { timeout: CHECK_LIMIT_MS }, async (t) => {
  await signalDuringSheet(t, "sheet-sigkill", "SIGKILL");
});

test("5.2 evidence 6: a src/ file edited during the probe fails the check with the changed-project reason and no sheet", { timeout: CHECK_LIMIT_MS }, async () => {
  const root = variant("edited");
  const { pid } = startCheck(root);
  await reaches(root, "check: stage 7 probe", false);
  appendFileSync(join(root, "src", "custom", "Orbit.tsx"), "// edited while the check ran\n");
  const done = await settled(root);
  assert.deepEqual([done.code, done.out], [1, `kit status: check failed (1 reasons)\n  ${CHANGED}\n`], log(root));
  assert.equal(existsSync(sheetPath(root)), false, "a changed project left a sheet");
  await ended(pid);
});

test("8: killing the caller's process group as check returns leaves the check running to a pass", { timeout: CHECK_LIMIT_MS }, async (t) => {
  const root = variant("caller");
  const caller = spawn("setsid", ["bash", "-c", "node bin/kit.mjs check; sleep 60"], { cwd: root, stdio: ["ignore", "ignore", "pipe"] });
  let err = "";
  caller.stderr.on("data", (chunk) => (err += chunk));
  const exited = new Promise((done) => caller.once("exit", (code, signal) => done({ code, signal })));
  assert.ok(await until(() => STARTED.test(err), 30000), err);
  const pgid = Number(execFileSync("ps", ["-o", "pgid=", "-p", String(caller.pid)], { encoding: "utf8" }).trim());
  assert.equal(pgid, caller.pid, "setsid made bash a group leader");
  process.kill(-pgid, "SIGKILL");
  assert.deepEqual(await exited, { code: null, signal: "SIGKILL" });
  const pid = Number(STARTED.exec(err)[1]);
  workers.add(pid);
  assert.ok(alive(pid), "the worker outlived its caller's group");
  const done = await settled(root);
  t.diagnostic(`check after the caller's group was killed: ${done.out.trim()} in ${done.ms} ms`);
  assert.deepEqual([done.code, done.out], [0, await passedOutput(root)], log(root));
  await ended(pid);
});

// --- task 5.4 -------------------------------------------------------------------

const STUB_LINE = "stub: the job's last line";

test("5.4: settled reads the log after each status call, so a line written as the job ends is seen", async () => {
  // A stub root whose status appends a last log line, then exits 0.
  const root = join(scratch, "stub-settled");
  mkdirSync(join(root, "bin"), { recursive: true });
  writeFileSync(
    join(root, "bin", "kit.mjs"),
    [
      'import { appendFileSync, mkdirSync } from "node:fs";',
      'if (process.argv[2] !== "status") process.exit(2);',
      'mkdirSync(".kit/check", { recursive: true });',
      `appendFileSync(".kit/check/log", ${JSON.stringify(`${STUB_LINE}\n`)});`,
      "",
    ].join("\n"),
  );
  const seen = new Map();
  const done = await settled(root, 10000, seen);
  assert.equal(done.code, 0, done.err);
  assert.deepEqual([...seen.keys()], [STUB_LINE]);
});

test("5.4: without memory.peak and memory.swap.peak, the peaks are the maxima of memory.current and memory.swap.current", () => {
  const dir = mkdtempSync(join(scratch, "cgroup-"));
  const write = (name, bytes) => writeFileSync(join(dir, name), `${bytes}\n`);
  write("memory.current", 1000);
  write("memory.swap.current", 0);
  const memory = memoryPeaks(dir);
  write("memory.current", 5000);
  memory.sample();
  write("memory.current", 3000);
  write("memory.swap.current", 7);
  assert.deepEqual(memory.read(), {
    memory: { source: "max of memory.current over 3 samples; kernel has no memory.peak", bytes: 5000 },
    swap: { source: "max of memory.swap.current over 3 samples; kernel has no memory.swap.peak", bytes: 7 },
  });
  write("memory.peak", 9000);
  write("memory.swap.peak", 11);
  assert.deepEqual(memoryPeaks(dir).read(), { memory: { source: "memory.peak", bytes: 9000 }, swap: { source: "memory.swap.peak", bytes: 11 } });
  for (const name of ["memory.peak", "memory.swap.peak", "memory.current"]) rmSync(join(dir, name));
  assert.throws(() => memoryPeaks(dir), { code: "ENOENT", path: join(dir, "memory.current") });
});

// The cgroup v2 file `name` in dir (by default the container's own),
// trimmed; a missing file throws.
const cgroup = (name, dir = CGROUP) => readFileSync(join(dir, name), "utf8").trim();
// A flat-keyed cgroup file (cpu.stat, memory.events) as {key: number}.
const keyed = (name) => Object.fromEntries(cgroup(name).split("\n").map((line) => [line.split(" ")[0], Number(line.split(" ")[1])]));

// The memory and swap peaks of the cgroup at dir. memory.peak (kernel
// 5.19) and memory.swap.peak (kernel 6.5) are read when present. When one
// is absent (ENOENT only), its peak is the maximum of memory.current (or
// memory.swap.current) over every sample: one now, one per sample() call
// (each poll while a job runs) and one per read(). read() gives
// {memory, swap}, each {source, bytes}. Every other read error throws, and
// so does a missing current file.
function memoryPeaks(dir = CGROUP) {
  const kinds = [
    { label: "memory", peakFile: "memory.peak", currentFile: "memory.current" },
    { label: "swap", peakFile: "memory.swap.peak", currentFile: "memory.swap.current" },
  ].map((kind) => {
    try {
      cgroup(kind.peakFile, dir);
      return { ...kind, direct: true };
    } catch (thrown) {
      if (thrown?.code !== "ENOENT") throw thrown;
      return { ...kind, direct: false, max: 0, samples: 0 };
    }
  });
  const sample = () => {
    for (const kind of kinds.filter(({ direct }) => !direct)) {
      kind.max = Math.max(kind.max, Number(cgroup(kind.currentFile, dir)));
      kind.samples++;
    }
  };
  const read = () => {
    sample();
    return Object.fromEntries(
      kinds.map((kind) => [
        kind.label,
        kind.direct
          ? { source: kind.peakFile, bytes: Number(cgroup(kind.peakFile, dir)) }
          : { source: `max of ${kind.currentFile} over ${kind.samples} samples; kernel has no ${kind.peakFile}`, bytes: kind.max },
      ]),
    );
  };
  sample();
  return { sample, read };
}

// The precondition of both timing tests: --cpus=2 --memory=4g
// --memory-swap=4g, so no swap distorts a time.
function assertLimits() {
  assert.deepEqual(
    { "memory.max": cgroup("memory.max"), "memory.swap.max": cgroup("memory.swap.max"), "cpu.max": cgroup("cpu.max") },
    { "memory.max": "4294967296", "memory.swap.max": "0", "cpu.max": "200000 100000" },
    "run under --cpus=2 --memory=4g --memory-swap=4g",
  );
}

function usage() {
  const cpu = keyed("cpu.stat");
  for (const key of ["nr_throttled", "throttled_usec"]) assert.ok(Number.isInteger(cpu[key]), `cpu.stat has no ${key}`);
  return {
    nr_throttled: cpu.nr_throttled,
    throttled_usec: cpu.throttled_usec,
    events: keyed("memory.events"),
  };
}

const median = (values) => {
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
};

// The diagnostics both timing tests record, before any of their end
// assertions, so a failing run still shows them.
function timingDiagnostics(t, { job, root, calledAt, calls, seen, stageOf, before, memory }) {
  const dir = join(root, ".kit", job);
  t.diagnostic(`${job} kernel ${release()}`);
  const { startedAt } = JSON.parse(readFileSync(join(dir, "job.json"), "utf8"));
  let result = null;
  try {
    result = JSON.parse(readFileSync(join(dir, "result.json"), "utf8"));
  } catch {
    // no result.json: the job died, which the end assertions report
  }
  t.diagnostic(`${job} total (result.json finishedAt − job.json startedAt): ${result === null ? "no result.json" : `${result.finishedAt - startedAt} ms`}`);
  t.diagnostic(`${job}: kit ${job} call to the first non-running status: ${calls.at(-1).at} ms; job.json startedAt ${startedAt - calledAt} ms after the call`);
  const firstSeen = new Map();
  for (const [line, ms] of seen) {
    const stage = stageOf(line);
    if (stage !== null && !firstSeen.has(stage)) firstSeen.set(stage, ms);
  }
  for (const [stage, ms] of firstSeen) t.diagnostic(`${job} ${stage}: first seen ${ms} ms after the call (${POLL_MS} ms polls)`);
  const latencies = calls.map(({ ms }) => ms);
  t.diagnostic(`${job} status: ${calls.length} calls, median ${median(latencies)} ms, max ${Math.max(...latencies)} ms`);
  const perStage = new Map();
  for (const { stage, ms } of calls) perStage.set(stage, [...(perStage.get(stage) ?? []), ms]);
  for (const [stage, values] of perStage) t.diagnostic(`${job} status during ${stage}: ${values.length} calls, max ${Math.max(...values)} ms`);
  const after = usage();
  t.diagnostic(
    `${job} cpu.stat deltas: nr_throttled ${after.nr_throttled - before.nr_throttled}, throttled_usec ${after.throttled_usec - before.throttled_usec}`,
  );
  t.diagnostic(
    `${job} memory.events deltas: ${Object.keys(after.events)
      .map((key) => `${key} ${after.events[key] - (before.events[key] ?? 0)}`)
      .join(", ")}`,
  );
  // memory.peak and memory.swap.peak are the container's peaks since it
  // started: the value before shows whether this job raised it.
  const peaks = memory.read();
  for (const label of ["memory", "swap"]) {
    t.diagnostic(`${job} ${label} peak (${peaks[label].source}): before ${before.memory[label].bytes} bytes, after ${peaks[label].bytes} bytes`);
  }
  const retries = readLog(root, job)
    .split("\n")
    .filter((line) => /retry|retried|handle/i.test(line));
  if (retries.length === 0) t.diagnostic(`${job} log: no line matches /retry|retried|handle/i (at the default log level)`);
  for (const line of retries) t.diagnostic(`${job} log: ${line}`);
}

// Every status call while the job ran exits 3 with `running`, and every
// call answers within the bound.
function assertAnswered(calls, job) {
  for (const [i, call] of calls.entries()) {
    if (i < calls.length - 1) {
      assert.equal(call.code, 3, call.out);
      assert.ok(call.out.startsWith(`kit status: ${job} running (`), call.out);
    }
    assert.ok(call.ms <= STATUS_BOUND_MS, `status call ${i + 1} during ${call.stage} took ${call.ms} ms`);
  }
}

// Test A's root once its check has passed; test B renders it.
let maxRoot = null;

test("5.4: on the tracked registry the maximum-size script passes, and status answers throughout", { skip: TIMING_SKIP, timeout: MAX_CHECK_LIMIT_MS }, async (t) => {
  assertLimits();
  const raw = maxScript();
  const { script } = validateScript(JSON.stringify(raw));
  const tone = join(scratch, "tone.wav");
  execFileSync("ffmpeg", [
    "-nostdin", "-v", "error",
    "-f", "lavfi", "-i", `sine=frequency=440:sample_rate=48000:duration=${TONE_SECONDS}`,
    "-ac", "1", "-c:a", "pcm_s16le",
    tone,
  ]);
  const root = variant("max", (dir) => {
    copyFileSync(join(TEMPLATE, "src", "kit", "render", "registry.ts"), join(dir, "src", "kit", "render", "registry.ts"));
    writeFileSync(join(dir, "script.json"), `${JSON.stringify(raw, null, 2)}\n`);
    for (const name of ["timeline.json", join("public", "narration.wav"), join("public", "voice")]) rmSync(join(dir, name), { recursive: true });
    mkdirSync(join(dir, "public", "voice"));
    for (const { stem } of blockStems(script)) copyFileSync(tone, join(dir, "public", "voice", `${stem}.wav`));
    mkdirSync(join(dir, "public", "images"));
    copyFileSync(join(TEMPLATE, "gallery", "public", "images", "sample.png"), join(dir, "public", "images", "sample.png"));
  });
  const laid = kit(root, "timeline");
  assert.equal(laid.code, 0, laid.err);
  t.diagnostic(laid.err.trim());
  const timeline = JSON.parse(readFileSync(join(root, "timeline.json"), "utf8"));
  assert.equal(timeline.totalFrames, checkTimeline(script, () => Math.round(TONE_SECONDS * 48000)).totalFrames);
  const memory = memoryPeaks();
  const before = { ...usage(), memory: memory.read() };
  const calledAt = Date.now();
  const { pid, elapsed } = startCheck(root);
  t.diagnostic(`check returned in ${elapsed} ms`);
  assert.ok(elapsed < 5000, `${elapsed} ms`);
  const seen = new Map();
  const calls = [];
  const done = await settled(root, MAX_CHECK_LIMIT_MS - 120000, seen, { everyMs: POLL_MS, since: calledAt, calls, stageOf: checkStage, onPoll: memory.sample });
  timingDiagnostics(t, { job: "check", root, calledAt, calls, seen, stageOf: checkStage, before, memory });
  await ended(pid);
  const tiles = sheetTiles(explainerScenes(script, timeline));
  assert.deepEqual([done.code, done.out], [0, await passedOutput(root, tiles)], log(root));
  assert.ok(existsSync(sheetPath(root)), "the pass wrote its sheet");
  // B needs a passed check, not the latency bound below.
  maxRoot = root;
  assertAnswered(calls, "check");
});

test("5.4: kit render of the maximum-size script completes, and status answers throughout", { skip: TIMING_SKIP, timeout: MAX_RENDER_LIMIT_MS }, async (t) => {
  assert.ok(maxRoot !== null, "5.4 test A's check did not pass; B renders its checked root");
  const root = maxRoot;
  assertLimits();
  const { totalFrames } = JSON.parse(readFileSync(join(root, "timeline.json"), "utf8"));
  const target = join(FILES, "max.mp4");
  const tmp = join(root, ".kit", "render", "tmp");
  // Every name .kit/render/tmp held at any poll, with the stage in which
  // it first appeared: one read of the directory per poll.
  const tmpNames = new Map();
  let tmpPolls = 0;
  const memory = memoryPeaks();
  const onPoll = (stage) => {
    memory.sample();
    tmpPolls++;
    let names;
    try {
      names = readdirSync(tmp);
    } catch (thrown) {
      if (thrown?.code !== "ENOENT") throw thrown;
      names = []; // the worker removed tmp/ as it ended, before status saw it end
    }
    for (const name of names) if (!tmpNames.has(name)) tmpNames.set(name, stage);
  };
  const before = { ...usage(), memory: memory.read() };
  const calledAt = Date.now();
  const { pid, elapsed } = startCheck(root, "render");
  t.diagnostic(`render returned in ${elapsed} ms`);
  assert.ok(elapsed < 5000, `${elapsed} ms`);
  const seen = new Map();
  const calls = [];
  const done = await settled(root, MAX_RENDER_LIMIT_MS - 120000, seen, { job: "render", everyMs: POLL_MS, since: calledAt, calls, stageOf: renderStage, onPoll });
  timingDiagnostics(t, { job: "render", root, calledAt, calls, seen, stageOf: renderStage, before, memory });
  t.diagnostic(`.kit/render/tmp over ${tmpPolls} polls: ${tmpNames.size} names`);
  for (const [name, stage] of tmpNames) t.diagnostic(`.kit/render/tmp: ${name} (first seen during ${stage})`);
  await ended(pid);
  assert.deepEqual([done.code, done.out], [0, `kit status: render succeeded: ${target}\n`], readLog(root, "render"));
  const probed = JSON.parse(
    execFileSync("ffprobe", ["-v", "error", "-show_entries", "stream=codec_type,codec_name,pix_fmt,nb_frames,duration", "-of", "json", target], {
      encoding: "utf8",
    }),
  ).streams;
  t.diagnostic(`ffprobe: ${JSON.stringify(probed)}`);
  const video = probed.find((stream) => stream.codec_type === "video");
  assert.equal(video.codec_name, "h264");
  assert.equal(video.pix_fmt, "yuv420p");
  assert.ok(probed.some((stream) => stream.codec_type === "audio" && stream.codec_name === "aac"), "no AAC stream");
  assert.equal(Number(video.nb_frames), totalFrames);
  assert.ok(Math.abs(Number(video.duration) - totalFrames / 30) <= 1 / 60, video.duration);
  assert.ok(tmpNames.size > 0, "no poll saw a non-empty .kit/render/tmp");
  assertAnswered(calls, "render");
});
