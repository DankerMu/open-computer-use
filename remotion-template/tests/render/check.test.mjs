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
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { blockStems } from "../../bin/voice-stem.mjs";
import { checkScript } from "../fixtures/check/fixture.mjs";

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

// Starts a check; returns the worker's pid, recorded for the final cleanup.
function startCheck(root) {
  const started = Date.now();
  const run = kit(root, "check");
  const elapsed = Date.now() - started;
  assert.equal(run.code, 0, run.err);
  assert.equal(run.out, "");
  const match = STARTED.exec(run.err);
  assert.ok(match, run.err);
  const pid = Number(match[1]);
  workers.add(pid);
  return { pid, elapsed };
}

// Polls status until it reports anything but running. seen, when given,
// maps each log line to the ms (since the call) it was first seen.
async function settled(root, timeoutMs = CHECK_LIMIT_MS, seen = null) {
  const started = Date.now();
  for (;;) {
    if (seen !== null) {
      for (const line of readLog(root).split("\n")) if (line !== "" && !seen.has(line)) seen.set(line, Date.now() - started);
    }
    const status = kit(root, "status");
    if (status.code !== 3) return { ...status, ms: Date.now() - started };
    assert.ok(Date.now() - started < timeoutMs, `still running after ${timeoutMs} ms`);
    await pause(seen === null ? 500 : 100);
  }
}

// Waits until the log holds `line` and a headless shell is up (with
// `shell`), failing at once when the check stops running.
async function reaches(root, line, shell) {
  const ok = () => readLog(root).includes(`${line}\n`) && (!shell || headlessShells().length > 0);
  await until(() => ok() || kit(root, "status").code !== 3, CHECK_LIMIT_MS - 60000);
  assert.ok(ok(), `never reached ${line}${shell ? " with a shell" : ""}: ${kit(root, "status").out}\n${readLog(root)}`);
}

const readLog = (root) => {
  try {
    return readFileSync(join(root, ".kit", "check", "log"), "utf8");
  } catch {
    return ""; // before startJob has opened the log
  }
};

const sheetPath = (root) => join(root, ".kit", "check", "contact.png");

// status's passed output at root, from timeline.json's transition windows:
// a tile's frame is the frame before its scene's exit.
async function passedOutput(root) {
  const { CHECKLIST } = await import("../../bin/checklist.mjs");
  const timeline = JSON.parse(readFileSync(join(root, "timeline.json"), "utf8"));
  return [
    "kit status: check passed",
    `contact sheet: ${sheetPath(root)}`,
    ...TILE_SCENES.map((k, i) => `  tile ${i + 1} (${CORNERS[i]}): ${TILE_WHERES[i]}, frame ${timeline.scenes[k].transitionOut.startFrame - 1}`),
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
