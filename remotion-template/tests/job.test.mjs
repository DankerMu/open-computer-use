// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The background job model of kit check (task 5.1): startJob, jobState and
// writeResult over a temporary project, with the tiny worker from
// tests/fixtures/check/worker.mjs; the stop-signal handler through a real
// SIGTERM; the check worker itself on a project without script.json; and
// the static import lists that keep @remotion/* off every start path.
// Expected states, messages and bounds come from the task 5.1 fixture.
// Every worker a test starts is waited for or killed before the test ends.

import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { jobState, startJob, writeResult } from "../bin/job.mjs";
import { Refusal } from "../bin/kit-io.mjs";

const TEMPLATE = fileURLToPath(new URL("..", import.meta.url)).replace(/\/$/, "");
const WORKER = join(TEMPLATE, "tests", "fixtures", "check", "worker.mjs");
const NAME = "check";

function project(t) {
  const root = mkdtempSync(join(tmpdir(), "job-test-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  return root;
}

const dir = (root) => join(root, ".kit", NAME);
const readJson = (path) => JSON.parse(readFileSync(path, "utf8"));
const pause = (ms) => new Promise((done) => setTimeout(done, ms));

async function until(check, timeoutMs = 10000) {
  const started = Date.now();
  for (;;) {
    const value = await check();
    if (value) return value;
    if (Date.now() - started > timeoutMs) return null;
    await pause(20);
  }
}

const alive = (pid) => spawnSync("ps", ["-o", "pid=", "-p", String(pid)]).status === 0;

// Starts the fixture worker; registers a kill of its pid for the test's end.
async function start(t, root, mode) {
  const pid = await startJob(root, NAME, [WORKER, NAME, mode]);
  t.after(() => {
    try {
      process.kill(pid, "SIGKILL");
    } catch {
      // ESRCH: the worker has already exited, which is the usual case.
    }
  });
  return pid;
}

const settled = (root) => until(async () => ((await jobState(root, NAME)).state === "running" ? null : jobState(root, NAME)));

// --- states -----------------------------------------------------------------

test("no job.json is none", async (t) => {
  assert.deepEqual(await jobState(project(t), NAME), { state: "none" });
});

test("a blocked worker is running, in its own process group, until it passes", async (t) => {
  const root = project(t);
  const before = Date.now();
  const pid = await start(t, root, "block");
  const job = readJson(join(dir(root), "job.json"));
  assert.deepEqual(Object.keys(job), ["version", "name", "token", "pid", "startedAt"]);
  assert.equal(job.version, 1);
  assert.equal(job.name, NAME);
  assert.match(job.token, /^[0-9a-f]{32}$/);
  assert.equal(job.pid, pid);
  assert.ok(job.startedAt >= before && job.startedAt <= Date.now(), `${job.startedAt}`);
  assert.deepEqual(await jobState(root, NAME), { state: "running", pid, startedAt: job.startedAt });
  // detached: the worker leads a process group of its own.
  const pgid = spawnSync("ps", ["-o", "pgid=", "-p", String(pid)], { encoding: "utf8" });
  assert.equal(pgid.status, 0);
  assert.equal(Number(pgid.stdout.trim()), pid);
  // The worker runs in the project root and logs there.
  assert.ok(await until(() => readFileSync(join(dir(root), "log"), "utf8") === `worker ${job.token}\n`));
  writeFileSync(join(root, "release"), "pass");
  const state = await settled(root);
  assert.deepEqual(state, { state: "passed", result: readJson(join(dir(root), "result.json")) });
  assert.deepEqual(Object.keys(state.result), ["version", "token", "outcome", "reasons", "finishedAt"]);
  assert.equal(state.result.version, 1);
  assert.equal(state.result.token, job.token);
  assert.deepEqual(state.result.reasons, []);
});

test("a failing worker is failed with its reasons", async (t) => {
  const root = project(t);
  await start(t, root, "fail");
  const state = await settled(root);
  assert.equal(state.state, "failed");
  assert.equal(state.result.outcome, "failed");
  assert.deepEqual(state.result.reasons, ["first reason", "second reason"]);
});

test("a worker that exits without a result is died, with its pid", async (t) => {
  const root = project(t);
  const pid = await start(t, root, "block");
  writeFileSync(join(root, "release"), "die");
  assert.ok(await until(() => !alive(pid)));
  assert.deepEqual(await jobState(root, NAME), { state: "died", pid });
});

test("pid reuse: a live process without the token is died, not running", async (t) => {
  const root = project(t);
  mkdirSync(dir(root), { recursive: true });
  // This test process is alive, and its arguments hold no job token.
  const job = { version: 1, name: NAME, token: "ab".repeat(16), pid: process.pid, startedAt: Date.now() };
  writeFileSync(join(dir(root), "job.json"), JSON.stringify(job));
  assert.deepEqual(await jobState(root, NAME), { state: "died", pid: process.pid });
});

test("a result with another run's token is not this run's", async (t) => {
  const root = project(t);
  mkdirSync(dir(root), { recursive: true });
  writeFileSync(join(dir(root), "job.json"), JSON.stringify({ version: 1, name: NAME, token: "cd".repeat(16), pid: null, startedAt: 0 }));
  writeFileSync(join(dir(root), "result.json"), JSON.stringify({ version: 1, token: "ef".repeat(16), outcome: "passed", reasons: [], finishedAt: 0 }));
  assert.deepEqual(await jobState(root, NAME), { state: "died", pid: null });
});

// --- start ------------------------------------------------------------------

test("a second start while one runs is refused and leaves the first alone", async (t) => {
  const root = project(t);
  const pid = await start(t, root, "block");
  const job = readFileSync(join(dir(root), "job.json"), "utf8");
  await assert.rejects(
    startJob(root, NAME, [WORKER, NAME, "pass"]),
    (error) =>
      error instanceof Refusal &&
      error.exitCode === 1 &&
      error.message === `a check is already running (pid ${pid}); run node bin/kit.mjs status`,
  );
  assert.equal(readFileSync(join(dir(root), "job.json"), "utf8"), job);
  writeFileSync(join(root, "release"), "pass");
  assert.equal((await settled(root)).state, "passed");
});

test("a new start removes the old result and log before its worker runs, so a death is never the old pass", async (t) => {
  const root = project(t);
  await start(t, root, "pass");
  assert.equal((await settled(root)).state, "passed");
  const oldToken = readJson(join(dir(root), "job.json")).token;
  const pid = await start(t, root, "block");
  const token = readJson(join(dir(root), "job.json")).token;
  assert.notEqual(token, oldToken);
  // While the new worker blocks: no result, and no line of the old log.
  assert.ok(await until(() => readFileSync(join(dir(root), "log"), "utf8") === `worker ${token}\n`));
  assert.equal(existsSync(join(dir(root), "result.json")), false);
  writeFileSync(join(root, "release"), "die");
  assert.ok(await until(() => !alive(pid)));
  assert.deepEqual(await jobState(root, NAME), { state: "died", pid });
});

test("a dead worker's state never blocks a new start", async (t) => {
  const root = project(t);
  mkdirSync(dir(root), { recursive: true });
  writeFileSync(join(dir(root), "job.json"), JSON.stringify({ version: 1, name: NAME, token: "12".repeat(16), pid: process.pid, startedAt: 0 }));
  await start(t, root, "pass");
  assert.equal((await settled(root)).state, "passed");
});

// --- writeResult --------------------------------------------------------------

test("writeResult escapes every reason, cuts it to 300 code points and keeps at most 40", async (t) => {
  const root = project(t);
  mkdirSync(dir(root), { recursive: true });
  const exact = "😀".repeat(300);
  const long = `${"光".repeat(299)}xyz`;
  const reasons = ["a\nb\tc\u0007d", exact, long, ...Array.from({ length: 42 }, (_, i) => `reason ${i + 4}`)];
  assert.equal(await writeResult(root, NAME, "fe".repeat(16), "failed", reasons), true);
  const result = readJson(join(dir(root), "result.json"));
  assert.equal(result.version, 1);
  assert.equal(result.token, "fe".repeat(16));
  assert.equal(result.outcome, "failed");
  assert.ok(Number.isInteger(result.finishedAt));
  assert.equal(result.reasons.length, 41);
  assert.equal(result.reasons[0], "a\\nb\\tc\\u0007d");
  assert.equal(result.reasons[1], exact);
  assert.equal(result.reasons[2], `${"光".repeat(299)}…`);
  assert.equal(Array.from(result.reasons[2]).length, 300);
  assert.equal(result.reasons[39], "reason 40");
  assert.equal(result.reasons[40], "… and 5 more");
  // The escapes count: a reason that escaping lengthens past 300 is cut.
  await writeResult(root, NAME, "fe".repeat(16), "failed", ["\n".repeat(200)]);
  assert.equal(readJson(join(dir(root), "result.json")).reasons[0], `${"\\n".repeat(149)}\\…`);
});

test("on SIGTERM the stop handler sets stopping, writeResult writes nothing, and the worker exits 143", async (t) => {
  const root = project(t);
  mkdirSync(dir(root), { recursive: true });
  const run = spawnSync(process.execPath, [WORKER, NAME, "signal", "aa".repeat(16)], { cwd: root, encoding: "utf8" });
  assert.equal(run.status, 143, run.stderr);
  assert.equal(run.stdout, `worker ${"aa".repeat(16)}\nwritten false\n`);
  assert.equal(existsSync(join(dir(root), "result.json")), false);
});

// --- the check worker -----------------------------------------------------------

// A project holding the kit's bin/ and src/, with node_modules linked to the
// template's, and no script.json.
function checkProject(t) {
  const root = project(t);
  for (const part of ["bin", "src"]) cpSync(join(TEMPLATE, part), join(root, part), { recursive: true });
  cpSync(join(TEMPLATE, "package.json"), join(root, "package.json"));
  symlinkSync(realpathSync(join(TEMPLATE, "node_modules")), join(root, "node_modules"));
  return root;
}

test("the check worker logs its stages and records a schema failure as failed", async (t) => {
  const root = checkProject(t);
  const pid = await startJob(root, NAME, [join("bin", "check.mjs"), "--worker"]);
  t.after(() => {
    try {
      process.kill(pid, "SIGKILL");
    } catch {
      // ESRCH: the worker has already exited.
    }
  });
  const state = await until(async () => {
    const s = await jobState(root, NAME);
    return s.state === "running" ? null : s;
  }, 60000);
  assert.equal(state?.state, "failed", JSON.stringify(state));
  assert.deepEqual(state.result.reasons, ["script.json: not found"]);
  assert.ok(await until(() => !alive(pid)));
  assert.equal(readFileSync(join(dir(root), "log"), "utf8"), "check: stage 1 schema\ncheck: done failed\n");
});

// --- the start path imports ------------------------------------------------------

// The specifiers of a module's static imports and re-exports, and of its
// dynamic imports, in source order.
function imports(path) {
  const source = readFileSync(path, "utf8");
  const staticOnes = [
    ...[...source.matchAll(/^\s*(?:import|export)\b[^;'"]*?\bfrom\s*["']([^"']+)["']/gm)].map((m) => [m.index, m[1]]),
    ...[...source.matchAll(/^\s*import\s*["']([^"']+)["']/gm)].map((m) => [m.index, m[1]]),
  ]
    .sort((a, b) => a[0] - b[0])
    .map(([, specifier]) => specifier);
  const dynamic = [...source.matchAll(/\bimport\s*\(\s*["']([^"']+)["']/g)].map((m) => m[1]);
  return { static: staticOnes, dynamic };
}

const bin = (name) => join(TEMPLATE, "bin", name);

test("kit.mjs, check.mjs and job.mjs import only node: built-ins and the kit's own modules", () => {
  assert.deepEqual(imports(bin("kit.mjs")), {
    static: ["node:crypto", "node:fs", "node:fs/promises", "node:path", "node:url", "./check.mjs", "./kit-io.mjs", "./timeline.mjs", "./voice-stem.mjs"],
    dynamic: [],
  });
  assert.deepEqual(imports(bin("check.mjs")), { static: ["./job.mjs"], dynamic: ["./check-rules.mjs"] });
  assert.deepEqual(imports(bin("job.mjs")), {
    static: ["node:child_process", "node:crypto", "node:fs/promises", "node:path", "./kit-io.mjs", "./script-schema.mjs"],
    dynamic: [],
  });
});

test("no @remotion/* module is reachable from kit.mjs through static imports", () => {
  const bare = new Set();
  const seen = new Set();
  const visit = (path) => {
    if (seen.has(path)) return;
    seen.add(path);
    for (const specifier of imports(path).static) {
      if (specifier.startsWith(".")) visit(join(dirname(path), specifier));
      else bare.add(specifier);
    }
  };
  visit(bin("kit.mjs"));
  assert.ok(seen.has(bin("job.mjs")));
  assert.ok(!seen.has(bin("check-rules.mjs")));
  assert.deepEqual([...bare].filter((specifier) => !specifier.startsWith("node:")), ["zod"]);
});
