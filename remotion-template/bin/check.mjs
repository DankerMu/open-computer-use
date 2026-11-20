// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// `kit check` and `kit status`. check starts the mechanical stage of the
// check as a background job (bin/job.mjs) and returns at once; status reports
// the job on stdout. Run as `node bin/check.mjs --worker <token>` from the
// project root, this file is the job's worker: it runs bin/check-rules.mjs's
// stages and writes the result. The worker alone loads check-rules.mjs, and
// through it the probe, Remotion and the theme, by dynamic import, so no
// @remotion/* module enters any other subcommand. Node warnings and
// progress go to the log, never into the reasons.

import { handleStopSignals, jobState, startJob, writeResult } from "./job.mjs";

const NAME = "check";
const STATUS_HINT = "run node bin/kit.mjs status";

export async function check(ctx) {
  const pid = await startJob(ctx.root, NAME, ["bin/check.mjs", "--worker"]);
  ctx.say(`started (pid ${pid}); ${STATUS_HINT}`);
  return 0;
}

// Exit codes: 0 passed, 1 failed or died, 3 running, 4 none has run.
export async function status(ctx) {
  const job = await jobState(ctx.root, NAME);
  const line = (text) => ctx.stdout.write(`kit status: ${text}\n`);
  switch (job.state) {
    case "running":
      line(`check running (pid ${job.pid}, ${Math.floor((Date.now() - job.startedAt) / 1000)} s)`);
      return 3;
    case "passed":
      line("check passed");
      return 0;
    case "failed": {
      const reasons = Array.isArray(job.result.reasons) ? job.result.reasons : [];
      line(`check failed (${reasons.length} reasons)`);
      for (const reason of reasons) ctx.stdout.write(`  ${reason}\n`);
      return 1;
    }
    case "died":
      line(`check died without an exit (pid ${job.pid}); see .kit/check/log`);
      return 1;
    default:
      line("no check has run; run node bin/kit.mjs check");
      return 4;
  }
}

// The worker: every stage, then the result. A thrown system error (EACCES,
// a module that cannot load, ...) is the one reason `check: <message>`;
// anything else is a bug, which ends the worker without a result.
async function worker(token) {
  handleStopSignals();
  const root = process.cwd();
  const log = (text) => process.stdout.write(`${text}\n`);
  let reasons;
  try {
    const { runChecks } = await import("./check-rules.mjs");
    reasons = await runChecks(root, { log });
  } catch (thrown) {
    if (typeof thrown?.code !== "string") throw thrown;
    reasons = [`check: ${thrown.message}`];
  }
  const outcome = reasons.length === 0 ? "passed" : "failed";
  // After a stop signal nothing is written, and the stop timer ends the
  // worker with 143.
  if (!(await writeResult(root, NAME, token, outcome, reasons))) return;
  log(`check: done ${outcome}`);
  process.exit(0);
}

if (import.meta.main && process.argv[2] === "--worker") await worker(process.argv[3]);
