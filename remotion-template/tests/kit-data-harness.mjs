// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The shared harness of the `kit data` tests (kit-data.test.mjs for CSV,
// kit-data-xlsx.test.mjs for xlsx): run(argv, io) with temporary directories
// for videosRoot, filesRoot and a copy of the template. Not a test file, so
// the *.test.mjs glob does not run it; tests/ is neither copied into
// projects nor shipped in the image.

import assert from "node:assert/strict";
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { run } from "../bin/kit.mjs";

export const TEMPLATE = fileURLToPath(new URL("..", import.meta.url)).replace(/\/$/, "");
export const FIXTURES = join(TEMPLATE, "tests", "fixtures", "data");
const LEFT_OUT = new Set(["node_modules", "tests", "out", "gallery", ".kit"]);

// --- sandbox ----------------------------------------------------------------

// A template copy whose node_modules holds only a link to zod, an empty files
// root, and a project made by `new`.
export async function project(t) {
  const base = mkdtempSync(join(tmpdir(), "kit-data-test-"));
  t.after(() => rmSync(base, { recursive: true, force: true }));
  const template = join(base, "template");
  cpSync(TEMPLATE, template, {
    recursive: true,
    filter: (src) => !LEFT_OUT.has(relative(TEMPLATE, src).split(sep)[0]),
  });
  mkdirSync(join(template, "node_modules"));
  symlinkSync(join(TEMPLATE, "node_modules", "zod"), join(template, "node_modules", "zod"));
  const files = join(base, "files");
  mkdirSync(files);
  const box = { base, template, videos: join(base, "videos"), files };
  const made = await kit(["new", "demo"], template, box);
  assert.equal(made.code, 0, made.err);
  const root = join(box.videos, "demo");
  const put = (name, content) => writeFileSync(join(files, name), content);
  const data = (...args) => kit(["data", ...args], root, box);
  return { ...box, root, dataDir: join(root, "data"), put, data };
}

export async function kit(argv, root, box, runner = run, extra = {}) {
  let out = "";
  let err = "";
  const code = await runner(argv, {
    ...extra,
    root,
    videosRoot: box.videos,
    filesRoot: box.files,
    stdout: { write: (text) => (out += text) },
    stderr: { write: (text) => (err += text) },
  });
  return { code, out, err };
}

export const readTable = (box, name) => JSON.parse(readFileSync(join(box.dataDir, `${name}.json`), "utf8"));

// Asserts a refused import: exit code, no stdout, the exact stderr line and
// no data/ directory.
export function assertRefused(box, result, line, code = 1) {
  assert.deepEqual(result, { code, out: "", err: `kit data: ${line}\n` });
  assert.equal(existsSync(box.dataDir), false, "data/ was created");
}

// Sets process.env[name] for one test.
export function setEnv(t, name, value) {
  const before = process.env[name];
  process.env[name] = value;
  t.after(() => {
    if (before === undefined) delete process.env[name];
    else process.env[name] = before;
  });
}
