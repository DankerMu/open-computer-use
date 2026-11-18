// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The explainer kit's one command. `node /opt/remotion-template/bin/kit.mjs
// new <name>` creates a project; inside it, `node bin/kit.mjs <subcommand>`
// runs every other step. stdout carries only data (a project path, speech
// calls, an image name); everything else goes to stderr, prefixed with the
// subcommand. Exit codes: 0 success, 1 a refused or failed step, 2 a usage
// error. Imports only node: built-ins and the kit's own modules; no network.

import { createHash, randomBytes } from "node:crypto";
import { constants } from "node:fs";
import {
  chmod,
  copyFile,
  lstat,
  mkdir,
  mkdtemp,
  open,
  readFile,
  readdir,
  realpath,
  rename,
  rm,
  rmdir,
  symlink,
  writeFile,
} from "node:fs/promises";
import { basename, dirname, extname, isAbsolute, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { validateScript } from "./script-schema.mjs";
import { blockStems } from "./voice-stem.mjs";

const PROJECT_NAME = /^[a-z0-9][a-z0-9-]{0,39}$/;
const NOT_COPIED = new Set(["node_modules", "tests", "out"]);
const SHOWN_ERRORS = 20;
const IMAGE_EXTENSIONS = new Set(["png", "jpg", "jpeg", "webp"]);
const IMAGE_STEM = 60;

// A refused step: printed as `kit <subcommand>: <message>`, then any detail
// lines as they are.
class Refusal extends Error {
  constructor(message, exitCode = 1, detail = []) {
    super(message);
    this.exitCode = exitCode;
    this.detail = detail;
  }
}

const notFound = (thrown) => thrown?.code === "ENOENT" || thrown?.code === "ENOTDIR";
const sha256 = (data) => createHash("sha256").update(data).digest("hex");

async function lstatOrNull(path) {
  try {
    return await lstat(path);
  } catch (thrown) {
    if (notFound(thrown)) return null;
    throw thrown;
  }
}

async function isRegularFile(path) {
  return (await lstatOrNull(path))?.isFile() ?? false;
}

// The template is the root whose node_modules is a real directory; a project
// links it.
async function isTemplate(root) {
  return (await lstatOrNull(join(root, "node_modules")))?.isDirectory() ?? false;
}

// --- script.json ------------------------------------------------------------

async function readScript(ctx) {
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
async function publicDir(root, sub) {
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

// Writes dir/name through a temporary file in dir and one rename.
async function writeAtomic(dir, name, data) {
  const temp = join(dir, `.${name}.${randomBytes(6).toString("hex")}.partial`);
  try {
    await writeFile(temp, data, { flag: "wx" });
    await rename(temp, join(dir, name));
  } catch (thrown) {
    await rm(temp, { force: true });
    throw thrown;
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
    if (await isRegularFile(join(voiceDir, `${stem}.wav`))) {
      imported++;
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

// --- entry ------------------------------------------------------------------

// fromTemplate: true runs only at the template root, false only elsewhere.
const COMMANDS = {
  new: { usage: "node /opt/remotion-template/bin/kit.mjs new <name>", args: 1, fromTemplate: true, run: newProject },
  voice: { usage: "node bin/kit.mjs voice", args: 0, fromTemplate: false, run: voice },
  "voice-import": { usage: "node bin/kit.mjs voice-import", args: 0, fromTemplate: false, run: voiceImport },
  image: { usage: 'node bin/kit.mjs image "<path>"', args: 1, fromTemplate: false, run: image },
};

// io: {root, videosRoot, filesRoot, stdout, stderr}; resolves to the exit code.
export async function run(argv, io) {
  const [name, ...args] = argv;
  if (!Object.hasOwn(COMMANDS, name ?? "")) {
    if (name !== undefined) io.stderr.write(`kit: unknown subcommand ${JSON.stringify(name)}\n`);
    for (const command of Object.values(COMMANDS)) io.stderr.write(`kit: usage: ${command.usage}\n`);
    return 2;
  }
  const command = COMMANDS[name];
  const say = (line) => io.stderr.write(`kit ${name}: ${line}\n`);
  const ctx = { ...io, root: resolve(io.root), args, say };
  try {
    if (args.length !== command.args) throw new Refusal(`usage: ${command.usage}`, 2);
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
    stdout: process.stdout,
    stderr: process.stderr,
  });
}
