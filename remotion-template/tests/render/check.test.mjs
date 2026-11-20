// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// kit check and kit status in a real headless shell (task 5.1, in-image
// evidence 1–9). Runs only inside the workspace image, from a project root
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
import { execFileSync, spawn, spawnSync } from "node:child_process";
import { copyFileSync, cpSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { blockStems } from "../../bin/voice-stem.mjs";
import { checkScript } from "../fixtures/check/fixture.mjs";

const TEMPLATE = fileURLToPath(new URL("../..", import.meta.url));
const FIXTURES = join(TEMPLATE, "tests", "render", "fixtures");
const CUSTOM = "chapter 2 原理, scene 1 (custom)";
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

// Polls status until it reports anything but running.
async function settled(root, timeoutMs = CHECK_LIMIT_MS) {
  const started = Date.now();
  for (;;) {
    const status = kit(root, "status");
    if (status.code !== 3) return { ...status, ms: Date.now() - started };
    assert.ok(Date.now() - started < timeoutMs, `still running after ${timeoutMs} ms`);
    await pause(500);
  }
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
  const done = await settled(passing);
  t.diagnostic(`passing check took ${done.ms + elapsed} ms`);
  assert.deepEqual([done.code, done.out], [0, "kit status: check passed\n"], log(passing));
  const lines = log(passing).split("\n");
  for (const line of [
    "check: stage 1 schema",
    "check: stage 2 timeline",
    "check: stage 3 assets",
    "check: stage 4 unnarrated",
    "check: stage 5 rest",
    "check: stage 6 probe",
    "check: done passed",
  ]) {
    assert.ok(lines.includes(line), `${line} not in the log`);
  }
  await ended(pid);
});

test("9: after a pass, a new check killed before it finishes reports died, never the old pass", { timeout: CHECK_LIMIT_MS }, async () => {
  assert.equal(kit(passing, "status").code, 0, "the previous test left a pass");
  const { pid } = startCheck(passing);
  process.kill(pid, "SIGKILL");
  await ended(pid);
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
  await ended(pid);
});

test("4: on the tracked registry the passing script fails with no component for the hook", { timeout: CHECK_LIMIT_MS }, async () => {
  const root = variant("tracked", (dir) => copyFileSync(join(TEMPLATE, "src", "kit", "render", "registry.ts"), join(dir, "src", "kit", "render", "registry.ts")));
  const { pid } = startCheck(root);
  const done = await settled(root);
  assert.deepEqual([done.code, done.out], [1, 'kit status: check failed (1 reasons)\n  hook: no component for kind "hook"\n'], log(root));
  await ended(pid);
});

// Starts a check, waits for the probe stage and a shell, sends `signal`, and
// measures until no shell is left.
async function signalDuringProbe(t, name, signal) {
  const root = variant(name);
  const { pid } = startCheck(root);
  assert.ok(
    await until(() => log(root).includes("check: stage 6 probe\n") && headlessShells().length > 0, CHECK_LIMIT_MS - 60000),
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
  assert.deepEqual([done.code, done.out], [0, "kit status: check passed\n"], log(root));
  await ended(pid);
});
