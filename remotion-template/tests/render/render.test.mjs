// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// kit render in the workspace image (task 5.3, in-image evidence 1–5). Runs
// only inside the image, from a project root whose node_modules link
// reaches /opt/remotion-template/node_modules, as its own invocation, with
// a tmpfs at /mnt/user-data/files so publishing crosses filesystems as in
// production:
//   node --test --test-concurrency=1 tests/render/render.test.mjs
// CI's tests/*.test.mjs glob does not match this file.
//
// The project is 5.1's passing project, built as tests/render/check.test.mjs
// builds it (3.8's fixture registry and Orbit, the pure fixture's script, a
// 7 s tone per block, and the project's own kit timeline), at
// <scratch>/passing, checked once here. The tests run in order and share
// it: a render killed by SIGKILL, one stopped by SIGTERM, then a relaunch
// that reuses the same check and succeeds.

import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFileSync, cpSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { blockStems } from "../../bin/voice-stem.mjs";
import { checkScript } from "../fixtures/check/fixture.mjs";

const TEMPLATE = fileURLToPath(new URL("../..", import.meta.url));
const FIXTURES = join(TEMPLATE, "tests", "render", "fixtures");
const FILES = "/mnt/user-data/files";
const NAME = "passing";
const TARGET = join(FILES, `${NAME}.mp4`);
const BLOCK_SECONDS = 7;
const CHECK_LIMIT_MS = 900000;
const RENDER_LIMIT_MS = 1500000;
const SCAN_LIMIT_MS = 10000;
const SCANNED = new Set(["chrome-headless", "ffmpeg", "esbuild"]);
const STARTED = /^kit render: started \(pid (\d+)\); run node bin\/kit\.mjs status\n$/;
const FRAMES = /^render: frames (\d+)\/(\d+)$/;

const pause = (ms) => new Promise((done) => setTimeout(done, ms));
const sha256 = (data) => createHash("sha256").update(data).digest("hex");
const scratch = mkdtempSync(join(tmpdir(), "render-render-"));
const root = join(scratch, NAME);
const workers = new Set();
let totalFrames;
let pass;

const kit = (...args) => {
  const run = spawnSync(process.execPath, ["bin/kit.mjs", ...args], { cwd: root, encoding: "utf8" });
  return { code: run.status, out: run.stdout, err: run.stderr };
};

const readLog = () => {
  try {
    return readFileSync(join(root, ".kit", "render", "log"), "utf8");
  } catch {
    return ""; // before startJob has opened the log
  }
};

const checkResult = () => readFileSync(join(root, ".kit", "check", "result.json"), "utf8");

// The fixture's scan: ps -eo pid,stat,comm, keeping live (stat not Z)
// headless shells, ffmpeg and esbuild.
function scan() {
  return execFileSync("ps", ["-eo", "pid,stat,comm", "--no-headers"], { encoding: "utf8" })
    .split("\n")
    .map((line) => line.trim().split(/\s+/))
    .filter(([pid, stat, comm]) => pid && !stat.startsWith("Z") && SCANNED.has(comm))
    .map(([pid, stat, comm]) => ({ pid: Number(pid), stat, comm }));
}

const shown = (found) => (found.length ? found.map(({ pid, stat, comm }) => `${pid} ${stat} ${comm}`).join(", ") : "none");

const alive = (pid) => {
  try {
    process.kill(pid, 0);
    return readFileSync(`/proc/${pid}/stat`, "utf8").split(") ")[1][0] !== "Z";
  } catch {
    return false;
  }
};

async function until(check, timeoutMs, everyMs = 100) {
  const started = Date.now();
  while (!check()) {
    if (Date.now() - started > timeoutMs) return false;
    await pause(everyMs);
  }
  return true;
}

// Polls status until it stops reporting running; seen maps each log line to
// the ms since the call it was first seen.
async function settled(limitMs, seen = null) {
  const started = Date.now();
  for (;;) {
    if (seen !== null) for (const line of readLog().split("\n")) if (line !== "" && !seen.has(line)) seen.set(line, Date.now() - started);
    const status = kit("status");
    if (status.code !== 3) return { ...status, ms: Date.now() - started };
    assert.ok(Date.now() - started < limitMs, `still running after ${limitMs} ms\n${readLog()}`);
    await pause(seen === null ? 500 : 100);
  }
}

function startRender() {
  const started = Date.now();
  const run = kit("render");
  const elapsed = Date.now() - started;
  assert.equal(run.code, 0, run.err);
  assert.equal(run.out, "");
  const match = STARTED.exec(run.err);
  assert.ok(match, run.err);
  const pid = Number(match[1]);
  workers.add(pid);
  return { pid, elapsed };
}

const partials = () => readdirSync(FILES).filter((name) => name.endsWith(".partial"));

before(async () => {
  mkdirSync(root);
  for (const part of ["src", "bin", join("tests", "render", "fixtures")]) cpSync(join(TEMPLATE, part), join(root, part), { recursive: true });
  for (const name of ["package.json", "tsconfig.json", "remotion.config.ts"]) copyFileSync(join(TEMPLATE, name), join(root, name));
  symlinkSync(realpathSync(join(TEMPLATE, "node_modules")), join(root, "node_modules"));
  copyFileSync(join(FIXTURES, "check", "registry.ts"), join(root, "src", "kit", "render", "registry.ts"));
  copyFileSync(join(FIXTURES, "explainer", "Orbit.tsx"), join(root, "src", "custom", "Orbit.tsx"));
  const script = checkScript();
  writeFileSync(join(root, "script.json"), `${JSON.stringify(script, null, 2)}\n`);
  mkdirSync(join(root, "public", "voice"), { recursive: true });
  for (const { stem } of blockStems(script)) {
    execFileSync("ffmpeg", [
      "-nostdin", "-v", "error",
      "-f", "lavfi", "-i", `sine=frequency=440:sample_rate=48000:duration=${BLOCK_SECONDS}`,
      "-ac", "1", "-c:a", "pcm_s16le",
      join(root, "public", "voice", `${stem}.wav`),
    ]);
  }
  const laid = kit("timeline");
  assert.equal(laid.code, 0, laid.err);
  totalFrames = JSON.parse(readFileSync(join(root, "timeline.json"), "utf8")).totalFrames;
  const checked = kit("check");
  assert.equal(checked.code, 0, checked.err);
  const done = await settled(CHECK_LIMIT_MS);
  assert.equal(done.code, 0, `${done.out}\n${readFileSync(join(root, ".kit", "check", "log"), "utf8")}`);
  pass = checkResult();
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

// Plants a previous target, starts a render, waits for the first frames
// line at 10% or more, scans, sends `signal`, and checks what the fixture
// expects of a render that dies.
async function killedRender(t, signal, { atOnce = false } = {}) {
  const planted = Buffer.from(`previous complete render before ${signal}\n`);
  writeFileSync(TARGET, planted);
  const before = sha256(readFileSync(TARGET));
  const { pid, elapsed } = startRender();
  if (atOnce) {
    // Evidence 1: it returns at once, and status says running.
    t.diagnostic(`render returned in ${elapsed} ms`);
    assert.ok(elapsed < 5000, `${elapsed} ms`);
    const running = kit("status");
    assert.equal(running.code, 3, running.out);
    assert.match(running.out, new RegExp(`^kit status: render running \\(pid ${pid}, \\d+ s\\)\\n$`));
    // Evidence 5: a check during the render is refused, and the pass stays.
    const refused = kit("check");
    assert.deepEqual(refused, { code: 1, out: "", err: `kit check: a render is already running (pid ${pid}); run node bin/kit.mjs status\n` });
    assert.equal(checkResult(), pass);
  }
  let line = null;
  const reached = await until(() => {
    line = readLog()
      .split("\n")
      .map((text) => FRAMES.exec(text))
      .find((match) => match !== null && Number(match[1]) >= Number(match[2]) / 10);
    return line !== undefined && line !== null;
  }, RENDER_LIMIT_MS);
  assert.ok(reached, `never reached 10%: ${kit("status").out}\n${readLog()}`);
  const scanned = scan();
  t.diagnostic(`${signal}: log line ${line[0]}; scan before: ${shown(scanned)}`);
  assert.ok(scanned.some(({ comm }) => comm === "chrome-headless"), `no headless shell before ${signal}: ${shown(scanned)}`);
  assert.ok(scanned.some(({ comm }) => comm === "ffmpeg"), `no ffmpeg before ${signal}: ${shown(scanned)}`);
  const sent = Date.now();
  process.kill(pid, signal);
  const clean = await until(() => scan().length === 0, SCAN_LIMIT_MS);
  const ms = Date.now() - sent;
  t.diagnostic(`${signal}: scan after ${ms} ms: ${shown(scan())}`);
  assert.ok(clean, `left ${SCAN_LIMIT_MS} ms after ${signal}: ${shown(scan())}`);
  assert.ok(await until(() => !alive(pid), 20000), `worker ${pid} still alive`);
  assert.deepEqual(kit("status"), { code: 1, out: `kit status: render died without an exit (pid ${pid}); see .kit/render/log\n`, err: "" });
  const afterSha = sha256(readFileSync(TARGET));
  t.diagnostic(`${signal}: target sha256 before ${before}, after ${afterSha}`);
  assert.equal(afterSha, before);
  assert.deepEqual(partials(), []);
  assert.equal(checkResult(), pass);
}

test("1, 5 and 2: render returns at once, a check is refused, and SIGKILL at 10% keeps the target", { timeout: RENDER_LIMIT_MS }, async (t) => {
  assert.deepEqual(scan(), [], "nothing scanned before the first render");
  await killedRender(t, "SIGKILL", { atOnce: true });
});

test("3: SIGTERM at 10% keeps the target", { timeout: RENDER_LIMIT_MS }, async (t) => {
  await killedRender(t, "SIGTERM");
});

test("4: a relaunch reuses the check and the render succeeds, verified by ffprobe", { timeout: RENDER_LIMIT_MS }, async (t) => {
  const { pid } = startRender();
  const seen = new Map();
  const done = await settled(RENDER_LIMIT_MS, seen);
  assert.deepEqual([done.code, done.out], [0, `kit status: render succeeded: ${TARGET}\n`], readLog());
  const at = (text) => seen.get(text);
  const zero = `render: frames 0/${totalFrames}`;
  t.diagnostic(`render took ${done.ms} ms in all`);
  t.diagnostic(`bundle, browser and composition (start to ${zero}, 100 ms polls): ${at(zero) - at("render: start")} ms`);
  t.diagnostic(`render (${zero} to verify): ${at("render: verify") - at(zero)} ms`);
  t.diagnostic(`verify and publish (verify to done): ${at("render: done succeeded") - at("render: verify")} ms`);
  const probed = JSON.parse(
    execFileSync("ffprobe", ["-v", "error", "-show_entries", "stream=codec_type,codec_name,pix_fmt,nb_frames,duration", "-of", "json", TARGET], {
      encoding: "utf8",
    }),
  ).streams;
  t.diagnostic(`ffprobe: ${JSON.stringify(probed)}`);
  const videoStream = probed.find((stream) => stream.codec_type === "video");
  assert.equal(videoStream.codec_name, "h264");
  assert.equal(videoStream.pix_fmt, "yuv420p");
  assert.ok(probed.some((stream) => stream.codec_type === "audio" && stream.codec_name === "aac"), "no AAC stream");
  assert.equal(Number(videoStream.nb_frames), totalFrames);
  assert.ok(Math.abs(Number(videoStream.duration) - totalFrames / 30) <= 1 / 60, videoStream.duration);
  assert.deepEqual(partials(), []);
  assert.deepEqual(
    readdirSync(join(root, ".kit", "render")).filter((name) => name.endsWith(".mp4")),
    [],
  );
  assert.equal(checkResult(), pass);
  assert.ok(await until(() => !alive(pid), 20000), `worker ${pid} still alive`);
  assert.ok(await until(() => scan().length === 0, SCAN_LIMIT_MS), `left after the render: ${shown(scan())}`);
});
