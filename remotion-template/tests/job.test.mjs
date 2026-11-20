// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The background job model of kit check (task 5.1): startJob, jobState and
// writeResult over a temporary project, with the tiny worker from
// tests/fixtures/check/worker.mjs; the stop-signal handler through a real
// SIGTERM; the check worker itself on a project without script.json, and on
// one whose src/ holds an unreadable directory; and the static import lists
// that keep @remotion/* off every start path. Expected states, messages and
// bounds come from the task 5.1 and 5.2 fixtures (5.2: the cleared job
// directory, `count` and the passed result's fields), and from task 5.3's:
// the succeeded state, the start lock, the spawn error, TMPDIR, racing
// starts, and the import pins of render.mjs and status.mjs. 5.3's new
// exports are imported inside the tests that use them.
// Every worker a test starts is waited for or killed before the test ends.

import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { chmodSync, cpSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { jobState, startJob, writeResult } from "../bin/job.mjs";
import { Refusal } from "../bin/kit-io.mjs";
import { checkFiles } from "./fixtures/check/fixture.mjs";

const TEMPLATE = fileURLToPath(new URL("..", import.meta.url)).replace(/\/$/, "");
const WORKER = join(TEMPLATE, "tests", "fixtures", "check", "worker.mjs");
const NAME = "check";

// locked: directories whose mode a test takes away, restored before the
// removal.
function project(t, locked = []) {
  const root = mkdtempSync(join(tmpdir(), "job-test-"));
  t.after(() => {
    for (const path of locked) chmodSync(path, 0o755);
    rmSync(root, { recursive: true, force: true });
  });
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
  assert.deepEqual(Object.keys(state.result), ["version", "token", "outcome", "reasons", "finishedAt", "count"]);
  assert.equal(state.result.count, 0);
  assert.equal(state.result.version, 1);
  assert.equal(state.result.token, job.token);
  assert.deepEqual(state.result.reasons, []);
});

test("a running job is seen whole under a narrow COLUMNS, as procps would cut its arguments", async (t) => {
  const root = project(t);
  const saved = process.env.COLUMNS;
  process.env.COLUMNS = "80";
  t.after(() => {
    if (saved === undefined) delete process.env.COLUMNS;
    else process.env.COLUMNS = saved;
  });
  const pid = await start(t, root, "block");
  const { token } = readJson(join(dir(root), "job.json"));
  // The token is the last argument, past column 80.
  const args = spawnSync("ps", ["-o", "args=", "-p", String(pid)], { encoding: "utf8", env: { ...process.env, COLUMNS: "1000" } }).stdout.trim();
  assert.ok(args.endsWith(token) && args.length > 80, args);
  assert.equal((await jobState(root, NAME)).state, "running");
  writeFileSync(join(root, "release"), "pass");
  assert.equal((await settled(root)).state, "passed");
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

test("a new start removes the whole job directory: a sheet, a work directory, the old result and log", async (t) => {
  const root = project(t);
  await start(t, root, "pass");
  assert.equal((await settled(root)).state, "passed");
  writeFileSync(join(dir(root), "contact.png"), "old sheet");
  writeFileSync(join(dir(root), "contact.next.png"), "old next sheet");
  mkdirSync(join(dir(root), "sheet", "bundle"), { recursive: true });
  writeFileSync(join(dir(root), "sheet", "bundle", "index.html"), "old bundle");
  const pid = await start(t, root, "block");
  const token = readJson(join(dir(root), "job.json")).token;
  assert.ok(await until(() => readFileSync(join(dir(root), "log"), "utf8") === `worker ${token}\n`));
  for (const name of ["contact.png", "contact.next.png", "sheet", "result.json"]) {
    assert.equal(existsSync(join(dir(root), name)), false, `${name} survived the start`);
  }
  // 5.3: the start recreates the directory with an empty tmp/ inside.
  assert.deepEqual(readdirSync(dir(root)).sort(), ["job.json", "log", "tmp"]);
  assert.deepEqual(readdirSync(join(dir(root), "tmp")), []);
  writeFileSync(join(root, "release"), "die");
  assert.ok(await until(() => !alive(pid)));
});

test("a dead worker's state never blocks a new start", async (t) => {
  const root = project(t);
  mkdirSync(dir(root), { recursive: true });
  writeFileSync(join(dir(root), "job.json"), JSON.stringify({ version: 1, name: NAME, token: "12".repeat(16), pid: process.pid, startedAt: 0 }));
  await start(t, root, "pass");
  assert.equal((await settled(root)).state, "passed");
});

// --- task 5.3: the succeeded state, the start lock, the spawn, TMPDIR ---------------

const kitDir = (root) => join(root, ".kit");
const lockPath = (root) => join(kitDir(root), "start.lock");
const LOCK_HELD = (pid) => `another kit command is starting a job (pid ${pid}); try again, or remove .kit/start.lock if none is`;

function plantLock(root, text) {
  mkdirSync(kitDir(root), { recursive: true });
  writeFileSync(lockPath(root), text);
}

// Nothing of the lock is left in .kit/: neither start.lock nor a temporary
// start.lock.<random>.
const lockFiles = (root) => readdirSync(kitDir(root)).filter((name) => name.startsWith("start.lock"));

test("a render result with outcome succeeded and this run's token reads as succeeded after the worker exits", async (t) => {
  const root = project(t);
  const pid = await startJob(root, "render", [WORKER, "render", "succeed"]);
  t.after(() => {
    try {
      process.kill(pid, "SIGKILL");
    } catch {
      // ESRCH: the worker has already exited.
    }
  });
  assert.ok(await until(() => !alive(pid)));
  const result = readJson(join(root, ".kit", "render", "result.json"));
  assert.equal(result.token, readJson(join(root, ".kit", "render", "job.json")).token);
  assert.deepEqual(await jobState(root, "render"), { state: "succeeded", result });
  assert.equal(result.exitCode, 0);
});

test("jobStartedAt is job.json's startedAt, or null without one", async (t) => {
  const { jobStartedAt } = await import("../bin/job.mjs");
  const root = project(t);
  assert.equal(await jobStartedAt(root, NAME), null);
  mkdirSync(dir(root), { recursive: true });
  writeFileSync(join(dir(root), "job.json"), "not json");
  assert.equal(await jobStartedAt(root, NAME), null);
  writeFileSync(join(dir(root), "job.json"), JSON.stringify({ version: 1, name: NAME, token: "ab".repeat(16), pid: null, startedAt: 1234 }));
  assert.equal(await jobStartedAt(root, NAME), 1234);
});

test("a held start lock with a live pid that started before it refuses, and the lock stays", async (t) => {
  const root = project(t);
  // This test process is alive and started before the lock.
  const text = JSON.stringify({ pid: process.pid, startedAt: Date.now() });
  plantLock(root, text);
  await assert.rejects(
    startJob(root, NAME, [WORKER, NAME, "pass"]),
    (error) => error instanceof Refusal && error.exitCode === 1 && error.message === LOCK_HELD(process.pid),
  );
  assert.equal(readFileSync(lockPath(root), "utf8"), text);
  assert.deepEqual(lockFiles(root), ["start.lock"]);
  assert.equal(existsSync(dir(root)), false, "a refused start created the job directory");
});

test("an unparsable start lock is held and never taken over", async (t) => {
  const root = project(t);
  plantLock(root, "{half a lock");
  await assert.rejects(
    startJob(root, NAME, [WORKER, NAME, "pass"]),
    (error) => error instanceof Refusal && error.message === LOCK_HELD("unknown"),
  );
  assert.equal(readFileSync(lockPath(root), "utf8"), "{half a lock");
});

test("a start lock naming a dead pid is taken over, and the start removes it", async (t) => {
  const root = project(t);
  const gone = spawnSync(process.execPath, ["-e", "process.stdout.write(String(process.pid))"], { encoding: "utf8" });
  plantLock(root, JSON.stringify({ pid: Number(gone.stdout), startedAt: Date.now() }));
  await start(t, root, "pass");
  assert.equal((await settled(root)).state, "passed");
  assert.deepEqual(lockFiles(root), []);
});

test("a start lock naming a live, unrelated pid that started after the lock is taken over", async (t) => {
  const root = project(t);
  // This test process started well after an hour-old lock: a reused pid.
  plantLock(root, JSON.stringify({ pid: process.pid, startedAt: Date.now() - 3600000 }));
  await start(t, root, "pass");
  assert.equal((await settled(root)).state, "passed");
  assert.deepEqual(lockFiles(root), []);
});

test("a spawn with a non-executable execPath rejects with its system error, 20 times, with no unhandled error", async (t) => {
  const root = project(t);
  const notExecutable = join(root, "not-node");
  writeFileSync(notExecutable, "#!/bin/sh\n", { mode: 0o644 });
  for (let i = 0; i < 20; i++) {
    await assert.rejects(
      startJob(root, NAME, [WORKER, NAME, "pass"], { execPath: notExecutable }),
      (error) => !(error instanceof Refusal) && error.code === "EACCES",
    );
    assert.deepEqual(lockFiles(root), [], `run ${i + 1} left its lock`);
  }
});

test("the worker's environment has TMPDIR at .kit/<name>/tmp, which exists", async (t) => {
  const root = project(t);
  await start(t, root, "tmpdir");
  assert.equal((await settled(root)).state, "passed");
  const { token } = readJson(join(dir(root), "job.json"));
  assert.equal(readFileSync(join(dir(root), "log"), "utf8"), `worker ${token}\ntmpdir ${join(root, ".kit", NAME, "tmp")}\n`);
  assert.ok(existsSync(join(dir(root), "tmp")));
});

test("two startJob calls racing in one process give exactly one start and one refusal", async (t) => {
  const root = project(t);
  const settledStarts = await Promise.allSettled([
    startJob(root, NAME, [WORKER, NAME, "block"]),
    startJob(root, NAME, [WORKER, NAME, "block"]),
  ]);
  for (const { status, value } of settledStarts) {
    if (status === "fulfilled") {
      t.after(() => {
        try {
          process.kill(value, "SIGKILL");
        } catch {
          // ESRCH: the worker has already exited.
        }
      });
    }
  }
  const started = settledStarts.filter(({ status }) => status === "fulfilled");
  const refused = settledStarts.filter(({ status }) => status === "rejected");
  assert.equal(started.length, 1, JSON.stringify(settledStarts));
  assert.equal(refused.length, 1);
  assert.ok(refused[0].reason instanceof Refusal, String(refused[0].reason));
  assert.ok(
    [LOCK_HELD(process.pid), `a check is already running (pid ${started[0].value}); run node bin/kit.mjs status`].includes(refused[0].reason.message),
    refused[0].reason.message,
  );
  assert.equal(readJson(join(dir(root), "job.json")).pid, started[0].value);
  assert.deepEqual(lockFiles(root), []);
  writeFileSync(join(root, "release"), "pass");
  assert.equal((await settled(root)).state, "passed");
});

test("a check start is refused while a render runs, under the one-job rule", async (t) => {
  const root = project(t);
  const pid = await startJob(root, "render", [WORKER, "render", "block"]);
  t.after(() => {
    try {
      process.kill(pid, "SIGKILL");
    } catch {
      // ESRCH: the worker has already exited.
    }
  });
  await assert.rejects(
    startJob(root, NAME, [WORKER, NAME, "pass"]),
    (error) => error instanceof Refusal && error.message === `a render is already running (pid ${pid}); run node bin/kit.mjs status`,
  );
  assert.equal(existsSync(dir(root)), false);
  writeFileSync(join(root, "release"), "die");
  assert.ok(await until(() => !alive(pid)));
});

test("a symlinked or non-directory .kit, and a symlinked .kit/<name>, are refused", async (t) => {
  const linked = project(t);
  const elsewhere = project(t);
  symlinkSync(elsewhere, kitDir(linked));
  await assert.rejects(startJob(linked, NAME, [WORKER, NAME, "pass"]), (error) => error instanceof Refusal && error.message === ".kit: is a symlink");
  assert.deepEqual(readdirSync(elsewhere), []);

  const file = project(t);
  writeFileSync(kitDir(file), "not a directory");
  await assert.rejects(startJob(file, NAME, [WORKER, NAME, "pass"]), (error) => error instanceof Refusal && error.message === ".kit: is not a directory");

  const inner = project(t);
  mkdirSync(kitDir(inner));
  symlinkSync(elsewhere, dir(inner));
  await assert.rejects(startJob(inner, NAME, [WORKER, NAME, "pass"]), (error) => error instanceof Refusal && error.message === ".kit/check: is a symlink");
  assert.deepEqual(readdirSync(elsewhere), []);

  const notDir = project(t);
  mkdirSync(kitDir(notDir));
  writeFileSync(dir(notDir), "x");
  await assert.rejects(startJob(notDir, NAME, [WORKER, NAME, "pass"]), (error) => error instanceof Refusal && error.message === ".kit/check: is not a directory");
});

test("stopRequested is false until a stop signal arrives", async () => {
  const { stopRequested } = await import("../bin/job.mjs");
  assert.equal(stopRequested(), false);
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

test("writeResult records count, the number of reasons before the bound, and extra's fields", async (t) => {
  const root = project(t);
  mkdirSync(dir(root), { recursive: true });
  const token = "ba".repeat(16);
  await writeResult(root, NAME, token, "failed", Array.from({ length: 45 }, (_, i) => `reason ${i + 1}`));
  const failed = readJson(join(dir(root), "result.json"));
  assert.deepEqual(Object.keys(failed), ["version", "token", "outcome", "reasons", "finishedAt", "count"]);
  assert.equal(failed.count, 45);
  assert.equal(failed.reasons.length, 41);
  const tiles = [
    { where: "hook", frame: 703 },
    { where: "summary", frame: 1885 },
  ];
  const extra = { hash: "c0".repeat(32), sheet: ".kit/check/contact.png", tiles };
  await writeResult(root, NAME, token, "passed", [], extra);
  const passed = readJson(join(dir(root), "result.json"));
  assert.deepEqual(Object.keys(passed), ["version", "token", "outcome", "reasons", "finishedAt", "count", "hash", "sheet", "tiles"]);
  assert.deepEqual({ ...passed, finishedAt: 0 }, { version: 1, token, outcome: "passed", reasons: [], finishedAt: 0, count: 0, ...extra });
});

// --- the check worker -----------------------------------------------------------

// A project holding the kit's bin/ and src/, with node_modules linked to the
// template's, and no script.json.
function checkProject(t, locked) {
  const root = project(t, locked);
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

test(
  "the check worker reports a system error from the snapshot's walk as check: <message>",
  { skip: process.getuid?.() === 0 && "root reads a mode-000 directory" },
  async (t) => {
    const locked = [];
    const root = checkProject(t, locked);
    for (const [path, bytes] of checkFiles()) {
      mkdirSync(dirname(join(root, path)), { recursive: true });
      writeFileSync(join(root, path), bytes);
    }
    const unreadable = join(root, "src", "locked");
    mkdirSync(unreadable);
    locked.push(unreadable);
    chmodSync(unreadable, 0o000);
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
    // The worker's working directory is the real path of the project.
    assert.deepEqual(state.result.reasons, [`check: EACCES: permission denied, scandir '${join(realpathSync(root), "src", "locked")}'`]);
    assert.ok(await until(() => !alive(pid)));
    assert.equal(
      readFileSync(join(dir(root), "log"), "utf8"),
      "check: stage 1 schema\ncheck: stage 2 timeline\ncheck: snapshot\ncheck: done failed\n",
    );
  },
);

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

test("kit.mjs, check.mjs, status.mjs, render.mjs and job.mjs import only node: built-ins and the kit's own modules", () => {
  assert.deepEqual(imports(bin("kit.mjs")), {
    static: [
      "node:crypto",
      "node:fs",
      "node:fs/promises",
      "node:path",
      "node:url",
      "./check.mjs",
      "./kit-io.mjs",
      "./render.mjs",
      "./status.mjs",
      "./timeline.mjs",
      "./voice-stem.mjs",
    ],
    dynamic: [],
  });
  assert.deepEqual(imports(bin("check.mjs")), { static: ["./job.mjs"], dynamic: ["./check-rules.mjs"] });
  assert.deepEqual(imports(bin("status.mjs")), { static: ["node:path", "./checklist.mjs", "./job.mjs"], dynamic: [] });
  assert.deepEqual(imports(bin("render.mjs")), {
    static: [
      "node:child_process",
      "node:fs/promises",
      "node:path",
      "./custom-index.mjs",
      "./explainer-scenes.mjs",
      "./job.mjs",
      "./kit-io.mjs",
      "./music.mjs",
      "./project-hash.mjs",
      "./script-schema.mjs",
    ],
    dynamic: ["./render-video.mjs"],
  });
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
  assert.ok(seen.has(bin("render.mjs")));
  assert.ok(seen.has(bin("status.mjs")));
  assert.ok(!seen.has(bin("check-rules.mjs")));
  assert.ok(!seen.has(bin("render-video.mjs")));
  assert.deepEqual([...bare].filter((specifier) => !specifier.startsWith("node:")), ["zod"]);
});
