// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The tiny worker of tests/job.test.mjs, started by startJob as
// `node worker.mjs <name> <mode> <token>` with the project as its working
// directory. It prints `worker <token>` to the log first. Modes:
//   pass, fail  write that result at once (fail with two reasons);
//   block       waits until the file `release` appears in the working
//               directory, then writes a pass when it holds `pass`, or exits
//               1 without a result when it holds `die`;
//   signal      installs the stop-signal handler, sends itself SIGTERM, then
//               tries to write a pass and prints whether it was written.

import { existsSync, readFileSync } from "node:fs";
import { handleStopSignals, writeResult } from "../../../bin/job.mjs";

const [name, mode, token] = process.argv.slice(2);
const root = process.cwd();
const pause = (ms) => new Promise((done) => setTimeout(done, ms));

process.stdout.write(`worker ${token}\n`);
if (mode === "pass") {
  await writeResult(root, name, token, "passed", []);
} else if (mode === "fail") {
  await writeResult(root, name, token, "failed", ["first reason", "second reason"]);
} else if (mode === "block") {
  while (!existsSync("release")) await pause(20);
  if (readFileSync("release", "utf8") === "die") process.exit(1);
  await writeResult(root, name, token, "passed", []);
} else if (mode === "signal") {
  handleStopSignals();
  process.kill(process.pid, "SIGTERM");
  await pause(20);
  process.stdout.write(`written ${await writeResult(root, name, token, "passed", [])}\n`);
} else {
  throw new Error(`unknown mode ${mode}`);
}
