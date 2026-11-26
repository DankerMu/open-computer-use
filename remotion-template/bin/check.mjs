// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// `kit check`: starts the check as a background job (bin/job.mjs) and
// returns at once; `kit status` (bin/status.mjs) reports it. Run as `node
// bin/check.mjs --worker <token>` from the project root, this file is the
// job's worker: it runs bin/check-rules.mjs's stages and writes the result.
// The worker alone loads check-rules.mjs, and through it the probe,
// Remotion and the theme, by dynamic import, so no @remotion/* module
// enters any other subcommand. Node warnings and progress go to the log,
// never into the reasons.

import { handleStopSignals, startJob, writeResult } from "./job.mjs";

const NAME = "check";
const STATUS_HINT = "run node bin/kit.mjs status";

export async function check(ctx) {
  const pid = await startJob(ctx.root, NAME, ["bin/check.mjs", "--worker"]);
  ctx.say(`started (pid ${pid}); ${STATUS_HINT}`);
  return 0;
}

// The worker: every stage, then the result, which carries the pass's hash,
// sheet and tiles when it passed. A thrown system error (EACCES, a module
// that cannot load, ...) is the one reason `check: <message>`; anything else
// is a bug, which ends the worker without a result.
// No deadline (#897): kit status shows a hung check, and kill -KILL -<pid> ends its group.
async function worker(token) {
  handleStopSignals();
  const root = process.cwd();
  const log = (text) => process.stdout.write(`${text}\n`);
  let reasons;
  let pass = null;
  try {
    const { runChecks } = await import("./check-rules.mjs");
    ({ reasons, pass } = await runChecks(root, { log }));
  } catch (thrown) {
    if (typeof thrown?.code !== "string") throw thrown;
    reasons = [`check: ${thrown.message}`];
  }
  const outcome = reasons.length === 0 ? "passed" : "failed";
  // After a stop signal nothing is written, and the stop timer ends the
  // worker with 143.
  if (!(await writeResult(root, NAME, token, outcome, reasons, outcome === "passed" ? pass : {}))) return;
  log(`check: done ${outcome}`);
  process.exit(0);
}

if (import.meta.main && process.argv[2] === "--worker") await worker(process.argv[3]);
