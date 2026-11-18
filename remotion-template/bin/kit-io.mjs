// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The file and message helpers every kit subcommand shares: the refusal
// type, reading and validating script.json, the public/ subdirectories, the
// atomic write, and the `kit <subcommand>:` stderr prefix. kit.mjs and
// timeline.mjs both import this module, so there is one copy and no import
// cycle. Imports only node: built-ins and the schema.

import { randomBytes } from "node:crypto";
import { lstat, mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { validateScript } from "./script-schema.mjs";

const SHOWN_ERRORS = 20;

// A refused step: printed as `kit <subcommand>: <message>`, then any detail
// lines as they are.
export class Refusal extends Error {
  constructor(message, exitCode = 1, detail = []) {
    super(message);
    this.exitCode = exitCode;
    this.detail = detail;
  }
}

export const notFound = (thrown) => thrown?.code === "ENOENT" || thrown?.code === "ENOTDIR";

export async function lstatOrNull(path) {
  try {
    return await lstat(path);
  } catch (thrown) {
    if (notFound(thrown)) return null;
    throw thrown;
  }
}

// Returns the line writer for one subcommand's stderr.
export function sayer(io, name) {
  return (line) => io.stderr.write(`kit ${name}: ${line}\n`);
}

// --- script.json ------------------------------------------------------------

export async function readScript(ctx) {
  const path = join(ctx.root, "script.json");
  let text;
  try {
    text = await readFile(path, "utf8");
  } catch (thrown) {
    if (notFound(thrown)) throw new Refusal(`${path}: not found`);
    throw thrown;
  }
  const result = validateScript(text);
  if (result.ok) return result.script;
  const detail = result.errors.slice(0, SHOWN_ERRORS).map((error) => `  ${error.message}`);
  if (result.errors.length > SHOWN_ERRORS) detail.push(`  … and ${result.errors.length - SHOWN_ERRORS} more`);
  throw new Refusal("script.json is invalid", 1, detail);
}

// --- project files ----------------------------------------------------------

// Creates root/public/<sub> when absent; refuses a symlink or non-directory
// on the way.
export async function publicDir(root, sub) {
  let path = root;
  for (const part of ["public", sub]) {
    path = join(path, part);
    const stat = await lstatOrNull(path);
    if (stat === null) await mkdir(path);
    else if (stat.isSymbolicLink()) throw new Refusal(`${path}: is a symlink`);
    else if (!stat.isDirectory()) throw new Refusal(`${path}: is not a directory`);
  }
  return path;
}

// Writes dir/name through a temporary file in dir and one rename.
export async function writeAtomic(dir, name, data) {
  const temp = join(dir, `.${name}.${randomBytes(6).toString("hex")}.partial`);
  try {
    await writeFile(temp, data, { flag: "wx" });
    await rename(temp, join(dir, name));
  } catch (thrown) {
    await rm(temp, { force: true });
    throw thrown;
  }
}
