// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The kit's background jobs, kit check and kit render. A job's state lives
// in <project>/.kit/<name>/: job.json names the run and its worker,
// result.json is the worker's outcome, log takes the worker's stdout and
// stderr, tmp/ is the worker's TMPDIR, and the worker may keep other files
// there (kit check's contact sheet and its work directory, kit render's
// video). A job is
//   - passed, failed or succeeded only from a result.json carrying this
//     run's token,
//   - running while its pid is alive and `ps -o args=` shows the token, which
//     survives pid reuse,
//   - died otherwise.
// The worker is detached, in a session and process group of its own, so
// killing the caller's group leaves it running. One kit job runs at a time:
// a start decides and spawns under .kit/start.lock.

import { execFile, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { link, mkdir, open, readFile, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { Refusal, lstatOrNull, notFound, writeAtomic } from "./kit-io.mjs";
import { escapeLine } from "./script-schema.mjs";

export const JOBS = ["check", "render"];
const VERSION = 1;
const JOB = "job.json";
const RESULT = "result.json";
const LOG = "log";
const TMP = "tmp";
const LOCK = "start.lock";
const OUTCOMES = ["passed", "failed", "succeeded"];
// A lock's holder started at most this long after the lock's startedAt:
// ps -o lstart= has whole seconds.
const LOCK_SLACK_MS = 1000;
// takeLock's link() attempts. Each read that finds no lock means its holder
// released the lock between this start's link() and the read; with one
// stale removal, five attempts allow three such releases in a row,
// already more contention than one project's kit commands make. Past that
// the start refuses with "try again" instead of spinning.
const LOCK_ATTEMPTS = 5;
const MAX_REASONS = 40;
const MAX_POINTS = 300;
// After SIGTERM or SIGINT the worker exits 143 (128 + SIGTERM) after this
// long, so Remotion's own listener first SIGKILLs the browser's group.
const STOP_EXIT_CODE = 143;
const STOP_DELAY_MS = 100;

// Set by the stop-signal handler: from then on writeResult writes nothing,
// so a probe rejected by the closing browser never becomes a failed result.
let stopping = false;

const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const jobDir = (root, name) => join(root, ".kit", name);
const serialized = (value) => `${JSON.stringify(value)}\n`;

// A state file's object: undefined when the file is absent, null when it
// holds no JSON object.
async function readState(path) {
  let text;
  try {
    text = await readFile(path, "utf8");
  } catch (thrown) {
    if (notFound(thrown)) return undefined;
    throw thrown;
  }
  try {
    const value = JSON.parse(text);
    return isObject(value) ? value : null;
  } catch (thrown) {
    if (thrown instanceof SyntaxError) return null;
    throw thrown;
  }
}

// ps -o <field>= for pid: its output, or null when ps finds no such
// process (exit 1). procps cuts args= at COLUMNS even into a pipe, so ps
// runs without COLUMNS, and in the C locale so lstart= parses.
function psField(pid, field) {
  const { COLUMNS: _, ...env } = process.env;
  return new Promise((resolve, reject) => {
    execFile("ps", ["-o", `${field}=`, "-p", String(pid)], { encoding: "utf8", env: { ...env, LC_ALL: "C" } }, (error, stdout) => {
      if (typeof error?.code === "string") reject(error);
      else resolve(error === null ? stdout : null);
    });
  });
}

// True when pid is alive and its arguments hold the token; the token is the
// last argument.
async function isThisJob(pid, token) {
  if (!Number.isInteger(pid) || pid <= 0 || typeof token !== "string" || token === "") return false;
  return (await psField(pid, "args"))?.includes(token) ?? false;
}

// {state: "none"}, {state: "passed" | "failed" | "succeeded", result},
// {state: "running", pid, startedAt} or {state: "died", pid}.
export async function jobState(root, name) {
  const dir = jobDir(root, name);
  const job = await readState(join(dir, JOB));
  if (job === undefined) return { state: "none" };
  if (job === null) return { state: "died", pid: null };
  const pid = Number.isInteger(job.pid) ? job.pid : null;
  // Liveness first: the worker writes its result before it exits, so a
  // result read after the worker is seen gone is never missed.
  const live = await isThisJob(pid, job.token);
  const result = await readState(join(dir, RESULT));
  if (isObject(result) && typeof job.token === "string" && result.token === job.token && OUTCOMES.includes(result.outcome)) {
    return { state: result.outcome, result };
  }
  if (live) return { state: "running", pid, startedAt: job.startedAt };
  return { state: "died", pid };
}

// job.json's startedAt, or null when there is none.
export async function jobStartedAt(root, name) {
  const job = await readState(join(jobDir(root, name), JOB));
  return Number.isFinite(job?.startedAt) ? job.startedAt : null;
}

// Refuses a symlink or non-directory at root/rel. An absent one is created
// when create is set.
async function ownDirectory(root, rel, create) {
  const stat = await lstatOrNull(join(root, rel));
  if (stat === null) {
    if (!create) return;
    try {
      await mkdir(join(root, rel));
    } catch (thrown) {
      // A concurrent start created it first: judge what is there now.
      if (thrown?.code !== "EEXIST") throw thrown;
      await ownDirectory(root, rel, false);
    }
  } else if (stat.isSymbolicLink()) throw new Refusal(`${rel}: is a symlink`);
  else if (!stat.isDirectory()) throw new Refusal(`${rel}: is not a directory`);
}

// True while the lock at path is held: its content parses, its pid is
// alive, and that process started no later than the lock (a pid reused
// after a container restart started later). An unparsable lock is held.
// Resolves to {held, pid}.
async function lockHolder(path) {
  const lock = await readState(path);
  if (lock === undefined) return { held: false, pid: null };
  if (lock === null || !Number.isInteger(lock.pid) || lock.pid <= 0 || !Number.isFinite(lock.startedAt)) return { held: true, pid: "unknown" };
  const { pid, startedAt } = lock;
  try {
    process.kill(pid, 0);
  } catch (thrown) {
    if (thrown?.code === "ESRCH") return { held: false, pid };
    if (thrown?.code !== "EPERM") throw thrown;
  }
  const started = await psField(pid, "lstart");
  if (started === null) return { held: false, pid };
  const at = Date.parse(started.trim());
  // An unreadable start time cannot show the pid was reused.
  return { held: Number.isNaN(at) || at <= startedAt + LOCK_SLACK_MS, pid };
}

// Publishes .kit/start.lock complete, by link() of a temporary file, and
// resolves once this process holds it. A held lock refuses. A read that
// finds no lock removes nothing: its holder released it, and another
// starter may already have linked its own, so the start retries link().
// A stale lock is removed at most once. holder(path, lockHolder) replaces
// the read in tests; startJob passes only kit.
export async function takeLock(kit, holder = (path) => lockHolder(path)) {
  const lock = join(kit, LOCK);
  let found;
  let removed = false;
  for (let attempt = 1; attempt <= LOCK_ATTEMPTS; attempt++) {
    const temp = join(kit, `${LOCK}.${randomBytes(6).toString("hex")}`);
    try {
      await writeFile(temp, serialized({ pid: process.pid, startedAt: Date.now() }), { flag: "wx" });
      await link(temp, lock);
      return lock;
    } catch (thrown) {
      if (thrown?.code !== "EEXIST") throw thrown;
    } finally {
      await rm(temp, { force: true });
    }
    found = await holder(lock, lockHolder);
    if (found.held) break;
    if (found.pid === null) continue;
    if (removed) break;
    await rm(lock, { force: true });
    removed = true;
  }
  throw new Refusal(`another kit command is starting a job (pid ${found.pid}); try again, or remove .kit/start.lock if none is`);
}

// Starts `<execPath> <workerArgv...> <token>` in root as a detached worker
// and returns its pid. Under the start lock it refuses while any kit job
// runs, then awaits precondition() (a Refusal from it propagates). Every
// file an earlier run left in the job's directory goes first, so nothing of
// it outlives the start: its result and log, a sheet, a work directory, a
// video, a killed run's temporary files.
export async function startJob(root, name, workerArgv, { precondition, execPath = process.execPath } = {}) {
  await ownDirectory(root, ".kit", true);
  await ownDirectory(root, `.kit/${name}`, false);
  const lock = await takeLock(join(root, ".kit"));
  try {
    for (const job of JOBS) {
      const current = await jobState(root, job);
      if (current.state === "running") {
        throw new Refusal(`a ${job} is already running (pid ${current.pid}); run node bin/kit.mjs status`);
      }
    }
    if (precondition !== undefined) await precondition();
    const dir = jobDir(root, name);
    await rm(dir, { recursive: true, force: true });
    // writeAtomic creates no directory.
    await mkdir(join(dir, TMP), { recursive: true });
    const job = { version: VERSION, name, token: randomBytes(16).toString("hex"), pid: null, startedAt: Date.now() };
    await writeAtomic(dir, JOB, serialized(job));
    const log = await open(join(dir, LOG), "w");
    let child;
    let failed;
    try {
      child = spawn(execPath, [...workerArgv, job.token], {
        cwd: root,
        detached: true,
        stdio: ["ignore", log.fd, log.fd],
        env: { ...process.env, TMPDIR: join(root, ".kit", name, TMP) },
      });
      // Before any await: a failed spawn emits 'error' on the next tick.
      failed = new Promise((resolve) => child.once("error", resolve));
    } finally {
      await log.close();
    }
    if (child.pid === undefined) throw await failed;
    child.unref();
    await writeAtomic(dir, JOB, serialized({ ...job, pid: child.pid }));
    return child.pid;
  } finally {
    await rm(lock, { force: true });
  }
}

// One reason as one escaped line of at most MAX_POINTS code points.
function bounded(reason) {
  const line = escapeLine(String(reason));
  const points = Array.from(line);
  return points.length <= MAX_POINTS ? line : `${points.slice(0, MAX_POINTS - 1).join("")}…`;
}

// Writes result.json with at most MAX_REASONS bounded reasons, followed by
// `… and <n> more` when there are others, then count (every reason) and
// extra's fields (a passed check's hash, sheet and tiles). Writes nothing
// once a stop signal arrived; returns whether it wrote.
export async function writeResult(root, name, token, outcome, reasons, extra = {}) {
  if (stopping) return false;
  const shown = reasons.slice(0, MAX_REASONS).map(bounded);
  if (reasons.length > MAX_REASONS) shown.push(`… and ${reasons.length - MAX_REASONS} more`);
  const result = { version: VERSION, token, outcome, reasons: shown, finishedAt: Date.now(), count: reasons.length, ...extra };
  await writeAtomic(jobDir(root, name), RESULT, serialized(result));
  return true;
}

// Whether a stop signal arrived; a worker checks it before publishing.
export function stopRequested() {
  return stopping;
}

// The worker's handler for SIGTERM and SIGINT: stop writing, then exit 143
// after STOP_DELAY_MS. Remotion's SIGINT listener may exit 130 sooner.
// Either way no result is written, and status reports died.
export function handleStopSignals() {
  const stop = () => {
    if (stopping) return;
    stopping = true;
    setTimeout(() => process.exit(STOP_EXIT_CODE), STOP_DELAY_MS);
  };
  process.on("SIGTERM", stop);
  process.on("SIGINT", stop);
}
