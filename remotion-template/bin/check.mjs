// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// `kit check` and `kit status`. check starts the check as a background job
// (bin/job.mjs) and returns at once; status reports the job on stdout, and
// after a pass names the contact sheet and prints the checklist. Run as `node bin/check.mjs --worker <token>` from the
// project root, this file is the job's worker: it runs bin/check-rules.mjs's
// stages and writes the result. The worker alone loads check-rules.mjs, and
// through it the probe, Remotion and the theme, by dynamic import, so no
// @remotion/* module enters any other subcommand. Node warnings and
// progress go to the log, never into the reasons.

import { join } from "node:path";
import { CHECKLIST } from "./checklist.mjs";
import { handleStopSignals, jobState, startJob, writeResult } from "./job.mjs";

const NAME = "check";
const STATUS_HINT = "run node bin/kit.mjs status";
const CORNERS = ["top left", "top right", "bottom left", "bottom right"];
const ANSWER = "view the contact sheet and answer each question yes or no; on any no, fix the project and run node bin/kit.mjs check again:";

// A passed result's sheet and tiles; status reports a sheet only from them.
function passedSheet(result) {
  const { sheet, tiles } = result;
  const tile = (entry) => typeof entry?.where === "string" && Number.isInteger(entry?.frame);
  if (typeof sheet !== "string" || !Array.isArray(tiles) || tiles.length !== CORNERS.length || !tiles.every(tile)) return null;
  return { sheet, tiles };
}

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
    case "passed": {
      line("check passed");
      const passed = passedSheet(job.result);
      if (passed === null) {
        ctx.stdout.write("no contact sheet from this run; run node bin/kit.mjs check again\n");
        return 1;
      }
      ctx.stdout.write(`contact sheet: ${join(ctx.root, passed.sheet)}\n`);
      passed.tiles.forEach(({ where, frame }, i) => ctx.stdout.write(`  tile ${i + 1} (${CORNERS[i]}): ${where}, frame ${frame}\n`));
      ctx.stdout.write(`${ANSWER}\n`);
      CHECKLIST.forEach((question, n) => ctx.stdout.write(`  ${n + 1}. ${question}\n`));
      return 0;
    }
    case "failed": {
      const reasons = Array.isArray(job.result.reasons) ? job.result.reasons : [];
      // count holds every reason; the list stops at 40 and a `… and <m> more` line.
      const count = Number.isInteger(job.result.count) ? job.result.count : reasons.length;
      line(`check failed (${count} reasons)`);
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

// The worker: every stage, then the result, which carries the pass's hash,
// sheet and tiles when it passed. A thrown system error (EACCES, a module
// that cannot load, ...) is the one reason `check: <message>`; anything else
// is a bug, which ends the worker without a result.
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
