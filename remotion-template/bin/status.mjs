// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// `kit status`: reports the kit job started last (bin/job.mjs), the check or
// the render, on stdout. After a passed check it names the contact sheet and
// prints the checklist; after a render it names the published video or the
// reasons. Imports only node: built-ins and the kit's Remotion-free modules.

import { join } from "node:path";
import { CHECKLIST } from "./checklist.mjs";
import { JOBS, jobStartedAt, jobState } from "./job.mjs";

const CORNERS = ["top left", "top right", "bottom left", "bottom right"];
const ANSWER = "view the contact sheet and answer each question yes or no; on any no, fix the project and run node bin/kit.mjs check again:";

// A passed result's sheet and tiles; status reports a sheet only from them.
function passedSheet(result) {
  const { sheet, tiles } = result;
  const tile = (entry) => typeof entry?.where === "string" && Number.isInteger(entry?.frame);
  if (typeof sheet !== "string" || !Array.isArray(tiles) || tiles.length !== CORNERS.length || !tiles.every(tile)) return null;
  return { sheet, tiles };
}

// count holds every reason; the list stops at 40 and a `… and <m> more` line.
function failedLines(result) {
  const reasons = Array.isArray(result.reasons) ? result.reasons : [];
  const count = Number.isInteger(result.count) ? result.count : reasons.length;
  return { count, reasons: reasons.map((reason) => `  ${reason}\n`).join("") };
}

const seconds = (job) => Math.floor((Date.now() - job.startedAt) / 1000);

// Exit codes: 0 passed, 1 failed or died, 3 running, 4 none has run.
async function checkStatus(ctx, line) {
  const job = await jobState(ctx.root, "check");
  switch (job.state) {
    case "running":
      line(`check running (pid ${job.pid}, ${seconds(job)} s)`);
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
      const { count, reasons } = failedLines(job.result);
      line(`check failed (${count} reasons)`);
      ctx.stdout.write(reasons);
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

// Exit codes: 0 succeeded, 1 failed or died, 3 running.
async function renderStatus(ctx, line) {
  const job = await jobState(ctx.root, "render");
  switch (job.state) {
    case "running":
      line(`render running (pid ${job.pid}, ${seconds(job)} s)`);
      return 3;
    case "succeeded":
      line(`render succeeded: ${job.result.target}`);
      return 0;
    case "failed": {
      const { count, reasons } = failedLines(job.result);
      line(`render failed with exit ${job.result.exitCode} (${count} reasons)`);
      ctx.stdout.write(reasons);
      return 1;
    }
    default:
      line(`render died without an exit (pid ${job.pid ?? null}); see .kit/render/log`);
      return 1;
  }
}

// The job whose job.json has the latest startedAt; the check when none has
// one, which then reports none (or a check job.json it cannot read).
export async function status(ctx) {
  const line = (text) => ctx.stdout.write(`kit status: ${text}\n`);
  let latest = "check";
  let latestAt = -Infinity;
  for (const name of JOBS) {
    const startedAt = await jobStartedAt(ctx.root, name);
    if (startedAt !== null && startedAt > latestAt) [latest, latestAt] = [name, startedAt];
  }
  return latest === "render" ? renderStatus(ctx, line) : checkStatus(ctx, line);
}
