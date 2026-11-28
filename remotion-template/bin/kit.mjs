// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The explainer kit's one command. `node /opt/remotion-template/bin/kit.mjs
// new <name>` creates a project; inside it, `node bin/kit.mjs <subcommand>`
// runs every other step. stdout carries only data (a project path, speech
// calls, an image name, a table summary); everything else goes to stderr,
// prefixed with the subcommand. `check` and `render` start background jobs;
// `status` reports the one started last. Exit codes: 0 success, 1 a refused
// or failed step, 2 a usage error; `status` also exits 3 while a job runs
// and 4 when none has run. Imports only node: built-ins and the kit's own
// modules; no network. `data` runs python3 on bin/read_table.py, the only
// reader of a source table file.

import { createHash } from "node:crypto";
import { constants } from "node:fs";
import { chmod, copyFile, lstat, mkdir, mkdtemp, open, readFile, readdir, realpath, rename, rm, rmdir, symlink } from "node:fs/promises";
import { basename, dirname, extname, isAbsolute, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { check } from "./check.mjs";
import { PROJECT_NAME, Refusal, escapeLine, execText, lstatOrNull, notFound, projectDir, publicDir, readScript, sayer, writeAtomic } from "./kit-io.mjs";
import { render } from "./render.mjs";
import { status } from "./status.mjs";
import { timeline } from "./timeline.mjs";
import { blockStems } from "./voice-stem.mjs";

const NOT_COPIED = new Set(["node_modules", "tests", "out", "gallery", ".kit"]);
const IMAGE_EXTENSIONS = new Set(["png", "jpg", "jpeg", "webp"]);
const IMAGE_STEM = 60;
const TABLE_NAME = 40;
const TABLE_PREVIEW_ROWS = 5;
const READER = join(dirname(fileURLToPath(import.meta.url)), "read_table.py");
// A 50-row, 12-column table is far below this; the bound only stops a
// runaway reader from growing the kit's memory without limit.
const READER_MAX_BUFFER = 64 * 1024 * 1024;
// Bounds a hung reader; a 40 MB file streams through it in about 1 s. The
// entry passes it as io.readerTimeoutMs.
const READER_TIMEOUT_MS = 60_000;

const sha256 = (data) => createHash("sha256").update(data).digest("hex");

async function isRegularFile(path) {
  return (await lstatOrNull(path))?.isFile() ?? false;
}

// The template is the root whose node_modules is a real directory; a project
// links it.
async function isTemplate(root) {
  return (await lstatOrNull(join(root, "node_modules")))?.isDirectory() ?? false;
}

// --- project files ----------------------------------------------------------

// Reads a regular file without following a final symlink.
async function readRegular(path) {
  const handle = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    if (!(await handle.stat()).isFile()) throw new Refusal(`${path}: is not a regular file`);
    return await handle.readFile();
  } finally {
    await handle.close();
  }
}

// --- new --------------------------------------------------------------------

// Copies src into dest in sorted order, refusing a symlink when the walk
// reaches it. Copies are the caller's, u+w.
async function copyTree(src, dest, top) {
  for (const name of (await readdir(src)).sort()) {
    if (top && NOT_COPIED.has(name)) continue;
    const from = join(src, name);
    const to = join(dest, name);
    const stat = await lstat(from);
    if (stat.isSymbolicLink()) throw new Refusal(`${from}: is a symlink; refusing to copy it`);
    if (stat.isDirectory()) {
      await mkdir(to, { mode: 0o755 });
      await copyTree(from, to, false);
    } else if (stat.isFile()) {
      await copyFile(from, to, constants.COPYFILE_EXCL);
      await chmod(to, 0o644);
    } else {
      throw new Refusal(`${from}: is not a regular file or directory; refusing to copy it`);
    }
  }
}

async function newProject(ctx) {
  const [name] = ctx.args;
  if (!PROJECT_NAME.test(name)) {
    throw new Refusal(`invalid name ${JSON.stringify(name)}: a-z or 0-9 first, then a-z, 0-9 or -, at most 40 characters`, 2);
  }
  const target = join(ctx.videosRoot, name);
  const existing = await lstatOrNull(target);
  const emptyDirectory = existing !== null && existing.isDirectory() && (await readdir(target)).length === 0;
  if (existing !== null && !emptyDirectory) throw new Refusal(`${target}: exists and is not an empty directory`);
  await mkdir(ctx.videosRoot, { recursive: true });
  const temp = await mkdtemp(join(ctx.videosRoot, `.${name}.partial-`));
  try {
    // mkdtemp creates 0700; the project directory gets the subdirectories' mode.
    await chmod(temp, 0o755);
    await copyTree(ctx.root, temp, true);
    await symlink(join(ctx.root, "node_modules"), join(temp, "node_modules"));
    if (emptyDirectory) await rmdir(target);
    await rename(temp, target);
  } catch (thrown) {
    await rm(temp, { recursive: true, force: true });
    throw thrown;
  }
  ctx.stdout.write(`${target}\n`);
}

// --- voice ------------------------------------------------------------------

async function voice(ctx) {
  const script = await readScript(ctx);
  const stems = blockStems(script);
  const voiceDir = join(ctx.root, "public", "voice");
  let missing = 0;
  for (const { text, stem } of stems) {
    if (await isRegularFile(join(voiceDir, `${stem}.wav`))) continue;
    missing++;
    const call = { text, filename: `${stem}.wav`, voice: script.voice.id, instructions: script.voice.instructions };
    ctx.stdout.write(`${JSON.stringify(call)}\n`);
  }
  ctx.say(`${missing} of ${stems.length} blocks need speech`);
}

// --- voice-import -----------------------------------------------------------

async function listNames(dir) {
  try {
    return await readdir(dir);
  } catch (thrown) {
    if (notFound(thrown)) return [];
    throw thrown;
  }
}

// The first regular file among <stem>.wav, then <stem> (N).wav for N >= 2
// in ascending N; a symlink or directory is skipped with a warning.
async function findSpeech(ctx, names, stem) {
  const suffixed = new RegExp(`^${stem} \\(([1-9][0-9]*)\\)\\.wav$`);
  const numbered = names
    .map((name) => ({ name, match: suffixed.exec(name) }))
    .filter(({ match }) => match !== null && BigInt(match[1]) >= 2n)
    .map(({ name, match }) => ({ name, n: BigInt(match[1]) }))
    .sort((a, b) => (a.n < b.n ? -1 : a.n > b.n ? 1 : 0));
  const candidates = [...(names.includes(`${stem}.wav`) ? [`${stem}.wav`] : []), ...numbered.map(({ name }) => name)];
  for (const name of candidates) {
    const path = join(ctx.filesRoot, name);
    const stat = await lstatOrNull(path);
    if (stat?.isFile()) return path;
    if (stat !== null) ctx.say(`${path}: ${stat.isSymbolicLink() ? "is a symlink" : "is not a regular file"}; skipped`);
  }
  return null;
}

async function voiceImport(ctx) {
  const script = await readScript(ctx);
  const voiceDir = await publicDir(ctx.root, "voice");
  const stems = blockStems(script).map((entry) => entry.stem);
  const names = await listNames(ctx.filesRoot);
  let imported = 0;
  const missing = [];
  for (const stem of stems) {
    const target = await lstatOrNull(join(voiceDir, `${stem}.wav`));
    if (target?.isFile()) {
      imported++;
      continue;
    }
    // A rename cannot replace a directory; the stem stays missing.
    if (target?.isDirectory()) {
      ctx.say(`public/voice/${stem}.wav is not a regular file; counted missing`);
      missing.push(stem);
      continue;
    }
    const source = await findSpeech(ctx, names, stem);
    if (source === null) {
      missing.push(stem);
      continue;
    }
    await writeAtomic(voiceDir, `${stem}.wav`, await readRegular(source));
    imported++;
  }
  ctx.say(`imported ${imported} of ${stems.length}`);
  for (const stem of missing) ctx.say(`missing ${stem}.wav`);
  return missing.length ? 1 : 0;
}

// --- user files -------------------------------------------------------------

// The real path of a file the user named: a relative path resolves against
// the files root, and the real path must be a regular file inside it.
async function userFile(ctx, arg) {
  const given = isAbsolute(arg) ? arg : resolve(ctx.filesRoot, arg);
  let real;
  try {
    real = await realpath(given);
  } catch (thrown) {
    if (notFound(thrown)) throw new Refusal(`${arg}: not found`);
    throw thrown;
  }
  if (!real.startsWith(`${await realpath(ctx.filesRoot)}${sep}`)) throw new Refusal(`${arg}: is outside ${ctx.filesRoot}`);
  if (!(await lstat(real)).isFile()) throw new Refusal(`${arg}: is not a regular file`);
  return { given, real };
}

// --- image ------------------------------------------------------------------

// Steps 1–5 of the fixture's sanitizing order; the result plus `.<ext>`
// always matches IMAGE_NAME.
function imageStem(stem) {
  const name = stem
    .replace(/[^A-Za-z0-9._-]+/gu, "-")
    .replace(/\.+/g, ".")
    .slice(0, IMAGE_STEM)
    .replace(/^[^A-Za-z0-9]+/, "")
    .replace(/[-.]+$/, "");
  return name || "image";
}

async function image(ctx) {
  const [arg] = ctx.args;
  const { given, real } = await userFile(ctx, arg);
  const extension = extname(given).slice(1).toLowerCase();
  if (!IMAGE_EXTENSIONS.has(extension)) throw new Refusal(`${arg}: is not a .png, .jpg, .jpeg or .webp file`);
  const stem = imageStem(basename(given).slice(0, -(extension.length + 1)));
  const data = await readRegular(real);
  const imagesDir = await publicDir(ctx.root, "images");
  const hash = sha256(data);
  for (let n = 1; ; n++) {
    const name = n === 1 ? `${stem}.${extension}` : `${stem}-${n}.${extension}`;
    const stat = await lstatOrNull(join(imagesDir, name));
    if (stat === null) {
      await writeAtomic(imagesDir, name, data);
    } else if (!stat.isFile() || sha256(await readFile(join(imagesDir, name))) !== hash) {
      continue;
    }
    ctx.stdout.write(`${name}\n`);
    return;
  }
}

// --- data -------------------------------------------------------------------

// The file stem in lower case, whitespace runs as -, only a-z, 0-9, - and
// Han characters kept, cut to TABLE_NAME code points.
function tableName(stem) {
  const kept = stem
    .toLowerCase()
    .replace(/\s+/gu, "-")
    .replace(/[^a-z0-9\p{Script=Han}-]/gu, "");
  return [...kept].slice(0, TABLE_NAME).join("");
}

const isObject = (value) => typeof value === "object" && value !== null && !Array.isArray(value);
const hasKeys = (value, keys) => isObject(value) && Object.keys(value).sort().join() === [...keys].sort().join();
const isString = (value) => typeof value === "string";

function tableCell(cell) {
  if (cell === null) return null;
  if (!hasKeys(cell, ["value", "text", "unit"])) return undefined;
  const { value, text, unit } = cell;
  if (!(value === null || Number.isFinite(value)) || !isString(text) || !(unit === "" || unit === "%")) return undefined;
  return { value, text, unit };
}

// The reader's table rebuilt in the pinned key order, or null when its shape
// is not the version 1 table format; source.sheet is null for a CSV and a
// non-empty string for an xlsx.
function tableFrom(parsed, xlsx) {
  if (!hasKeys(parsed, ["version", "source", "label_header", "columns", "rows"]) || parsed.version !== 1) return null;
  const { source, label_header: labelHeader, columns, rows } = parsed;
  if (!hasKeys(source, ["file", "sheet", "sha256"]) || !isString(source.file)) return null;
  if (xlsx ? !isString(source.sheet) || source.sheet === "" : source.sheet !== null) return null;
  if (!isString(source.sha256) || !/^[0-9a-f]{64}$/.test(source.sha256) || !isString(labelHeader)) return null;
  if (!Array.isArray(columns) || columns.length === 0 || !columns.every(isString) || !Array.isArray(rows)) return null;
  const table = {
    version: 1,
    source: { file: source.file, sheet: source.sheet, sha256: source.sha256 },
    label_header: labelHeader,
    columns: [...columns],
    rows: [],
  };
  for (const row of rows) {
    if (!hasKeys(row, ["label", "cells"]) || !isString(row.label) || !Array.isArray(row.cells)) return null;
    if (row.cells.length !== columns.length) return null;
    const cells = row.cells.map(tableCell);
    if (cells.includes(undefined)) return null;
    table.rows.push({ label: row.label, cells });
  }
  return table.rows.length === 0 ? null : table;
}

// Runs the reader on one real path and, for an xlsx, the --sheet value if
// given, killing it after timeoutMs; resolves the table, or throws a
// refusal: the reader's own, or `the table reader failed: <reason>`.
async function readTable(path, xlsx, sheet, timeoutMs) {
  const failed = (reason) => new Refusal(`the table reader failed: ${escapeLine(reason)}`);
  const args = ["-I", READER, path, ...(sheet === undefined ? [] : [sheet])];
  const { exit, stdout, stderr } = await execText("python3", args, READER_MAX_BUFFER, timeoutMs);
  if (exit === "ENOENT") throw failed("python3 not found");
  if (exit === "ETIMEDOUT") throw failed(`timed out after ${timeoutMs / 1000} seconds`);
  if (exit === "ERR_CHILD_PROCESS_STDIO_MAXBUFFER") throw failed("bad output");
  if (exit === 0) {
    let table = null;
    try {
      table = tableFrom(JSON.parse(stdout), xlsx);
    } catch {
      // A JSON syntax error is bad output, thrown below.
    }
    if (table === null) throw failed("bad output");
    return table;
  }
  if (exit === 1 && stderr.startsWith("refusal: ") && stderr.endsWith("\n")) {
    throw new Refusal(escapeLine(stderr.slice("refusal: ".length, -1)));
  }
  const last = stderr.trimEnd().split("\n").at(-1);
  throw failed(last === "" ? `python3 exited with ${exit}` : last);
}

// One tab-separated output line.
const tabLine = (fields) => `${fields.map(escapeLine).join("\t")}\n`;

async function data(ctx) {
  const [arg] = ctx.args;
  const { given, real } = await userFile(ctx, arg);
  // The real path's extension picks the format; the name comes from the given path.
  const extension = extname(real).toLowerCase();
  if (extension !== ".csv" && extension !== ".xlsx") throw new Refusal(`${arg}: is not a .csv or .xlsx file`);
  const xlsx = extension === ".xlsx";
  if (!xlsx && ctx.options.sheet !== undefined) throw new Refusal("--sheet applies only to .xlsx files");
  const name = tableName(basename(given, extname(given)));
  if (name === "") throw new Refusal(`${arg}: the file name gives an empty table name`);
  const table = await readTable(real, xlsx, ctx.options.sheet, ctx.readerTimeoutMs ?? READER_TIMEOUT_MS);
  const dir = await projectDir(ctx.root, "data");
  const file = `${name}.json`;
  const replaced = (await lstatOrNull(join(dir, file))) !== null;
  await writeAtomic(dir, file, `${JSON.stringify(table, null, 2)}\n`);
  ctx.stdout.write(`${name}\n`);
  ctx.stdout.write(tabLine([table.label_header, ...table.columns]));
  for (const row of table.rows.slice(0, TABLE_PREVIEW_ROWS)) {
    ctx.stdout.write(tabLine([row.label, ...row.cells.map((cell) => (cell === null ? "" : `${cell.text}${cell.unit}`))]));
  }
  ctx.say(`${replaced ? "replaced" : "wrote"} data/${file}`);
}

// --- entry ------------------------------------------------------------------

// Reads args left to right for a command that declares options: `--<name>`
// takes the next token as its value, `--` ends option parsing, any other
// token starting with `--` is unknown, and every other token is a
// positional. Returns null for a missing value, a repeated or an unknown
// option.
function parseOptions(args, names) {
  const options = {};
  const positionals = [];
  for (let i = 0; i < args.length; i++) {
    const token = args[i];
    if (token === "--") {
      positionals.push(...args.slice(i + 1));
      break;
    }
    if (!token.startsWith("--")) {
      positionals.push(token);
      continue;
    }
    const name = token.slice(2);
    if (!names.includes(name) || Object.hasOwn(options, name) || i + 1 === args.length) return null;
    options[name] = args[++i];
  }
  return { args: positionals, options };
}

// fromTemplate: true runs only at the template root, false only elsewhere.
// options: the `--<name>` options a command takes; a command without it
// takes positionals only.
const COMMANDS = {
  new: { usage: "node /opt/remotion-template/bin/kit.mjs new <name>", args: 1, fromTemplate: true, run: newProject },
  voice: { usage: "node bin/kit.mjs voice", args: 0, fromTemplate: false, run: voice },
  "voice-import": { usage: "node bin/kit.mjs voice-import", args: 0, fromTemplate: false, run: voiceImport },
  image: { usage: 'node bin/kit.mjs image "<path>"', args: 1, fromTemplate: false, run: image },
  data: { usage: 'node bin/kit.mjs data "<path>" [--sheet <name>]', args: 1, options: ["sheet"], fromTemplate: false, run: data },
  timeline: { usage: "node bin/kit.mjs timeline", args: 0, fromTemplate: false, run: timeline },
  check: { usage: "node bin/kit.mjs check", args: 0, fromTemplate: false, run: check },
  render: { usage: "node bin/kit.mjs render", args: 0, fromTemplate: false, run: render },
  status: { usage: "node bin/kit.mjs status", args: 0, fromTemplate: false, run: status },
};

// io: {root, videosRoot, filesRoot, stdout, stderr, readerTimeoutMs}; resolves
// to the exit code. readerTimeoutMs, the table reader's time limit, defaults
// to READER_TIMEOUT_MS.
export async function run(argv, io) {
  const [name, ...args] = argv;
  if (!Object.hasOwn(COMMANDS, name ?? "")) {
    if (name !== undefined) io.stderr.write(`kit: unknown subcommand ${JSON.stringify(name)}\n`);
    for (const command of Object.values(COMMANDS)) io.stderr.write(`kit: usage: ${command.usage}\n`);
    return 2;
  }
  const command = COMMANDS[name];
  const say = sayer(io, name);
  const ctx = { ...io, root: resolve(io.root), args, options: {}, say };
  try {
    if (command.options === undefined) {
      if (args.length !== command.args) throw new Refusal(`usage: ${command.usage}`, 2);
    } else {
      const parsed = parseOptions(args, command.options);
      if (parsed === null || parsed.args.length !== command.args) throw new Refusal(`usage: ${command.usage}`, 2);
      Object.assign(ctx, parsed);
    }
    const template = await isTemplate(ctx.root);
    if (command.fromTemplate && !template) throw new Refusal(`run it from the template: ${command.usage}`, 2);
    if (!command.fromTemplate && template) {
      throw new Refusal(`run it inside a project: cd ${io.videosRoot}/<name> && node bin/kit.mjs ${name}`, 2);
    }
    return (await command.run(ctx)) ?? 0;
  } catch (thrown) {
    if (thrown instanceof Refusal) {
      say(thrown.message);
      for (const line of thrown.detail) io.stderr.write(`${line}\n`);
      return thrown.exitCode;
    }
    // A system error (EACCES, ENOSPC, ...) is a failed step; anything else is a bug.
    if (typeof thrown?.code !== "string") throw thrown;
    say(thrown.message);
    return 1;
  }
}

if (import.meta.main) {
  process.exitCode = await run(process.argv.slice(2), {
    root: dirname(dirname(fileURLToPath(import.meta.url))),
    videosRoot: "/home/assistant/videos",
    filesRoot: "/mnt/user-data/files",
    readerTimeoutMs: READER_TIMEOUT_MS,
    stdout: process.stdout,
    stderr: process.stderr,
  });
}
