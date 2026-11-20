// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The kit's background jobs (kit check; kit render reuses it). A job's state
// lives in <project>/.kit/<name>/: job.json names the run and its worker,
// result.json is the worker's outcome, and log takes the worker's stdout and
// stderr. A job is
//   - passed or failed only from a result.json carrying this run's token,
//   - running while its pid is alive and `ps -o args=` shows the token, which
//     survives pid reuse,
//   - died otherwise.
// The worker is detached, in a session and process group of its own, so
// killing the caller's group leaves it running.

import { execFile, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdir, open, readFile, rm } from "node:fs/promises";
import { join } from "node:path";
import { Refusal, notFound, writeAtomic } from "./kit-io.mjs";
import { escapeLine } from "./script-schema.mjs";

const VERSION = 1;
const JOB = "job.json";
const RESULT = "result.json";
const LOG = "log";
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

// True when pid is alive and its arguments hold the token. ps exits 1 for a
// pid with no process.
function isThisJob(pid, token) {
  if (!Number.isInteger(pid) || pid <= 0 || typeof token !== "string" || token === "") return Promise.resolve(false);
  return new Promise((resolve, reject) => {
    execFile("ps", ["-o", "args=", "-p", String(pid)], { encoding: "utf8" }, (error, stdout) => {
      if (typeof error?.code === "string") reject(error);
      else resolve(error === null && stdout.includes(token));
    });
  });
}

// {state: "none"}, {state: "passed" | "failed", result}, {state: "running",
// pid, startedAt} or {state: "died", pid}.
export async function jobState(root, name) {
  const dir = jobDir(root, name);
  const job = await readState(join(dir, JOB));
  if (job === undefined) return { state: "none" };
  if (job === null) return { state: "died", pid: null };
  const pid = Number.isInteger(job.pid) ? job.pid : null;
  const result = await readState(join(dir, RESULT));
  if (isObject(result) && typeof job.token === "string" && result.token === job.token && ["passed", "failed"].includes(result.outcome)) {
    return { state: result.outcome, result };
  }
  if (await isThisJob(pid, job.token)) return { state: "running", pid, startedAt: job.startedAt };
  return { state: "died", pid };
}

// Starts `node <workerArgv...> <token>` in root as a detached worker and
// returns its pid. Refuses while this job runs.
export async function startJob(root, name, workerArgv) {
  const current = await jobState(root, name);
  if (current.state === "running") {
    throw new Refusal(`a ${name} is already running (pid ${current.pid}); run node bin/kit.mjs status`);
  }
  const dir = jobDir(root, name);
  // writeAtomic creates no directory.
  await mkdir(dir, { recursive: true });
  await rm(join(dir, RESULT), { force: true });
  await rm(join(dir, LOG), { force: true });
  const job = { version: VERSION, name, token: randomBytes(16).toString("hex"), pid: null, startedAt: Date.now() };
  await writeAtomic(dir, JOB, serialized(job));
  const log = await open(join(dir, LOG), "w");
  let child;
  try {
    child = spawn(process.execPath, [...workerArgv, job.token], { cwd: root, detached: true, stdio: ["ignore", log.fd, log.fd] });
  } finally {
    await log.close();
  }
  if (child.pid === undefined) {
    throw await new Promise((resolve) => child.once("error", resolve));
  }
  child.unref();
  await writeAtomic(dir, JOB, serialized({ ...job, pid: child.pid }));
  return child.pid;
}

// One reason as one escaped line of at most MAX_POINTS code points.
function bounded(reason) {
  const line = escapeLine(String(reason));
  const points = Array.from(line);
  return points.length <= MAX_POINTS ? line : `${points.slice(0, MAX_POINTS - 1).join("")}…`;
}

// Writes result.json with at most MAX_REASONS bounded reasons, followed by
// `… and <n> more` when there are others. Writes nothing once a stop signal
// arrived; returns whether it wrote.
export async function writeResult(root, name, token, outcome, reasons) {
  if (stopping) return false;
  const shown = reasons.slice(0, MAX_REASONS).map(bounded);
  if (reasons.length > MAX_REASONS) shown.push(`… and ${reasons.length - MAX_REASONS} more`);
  await writeAtomic(jobDir(root, name), RESULT, serialized({ version: VERSION, token, outcome, reasons: shown, finishedAt: Date.now() }));
  return true;
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
