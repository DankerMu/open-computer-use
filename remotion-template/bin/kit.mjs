// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The explainer kit's one command. `node /opt/remotion-template/bin/kit.mjs
// new <name>` creates a project; inside it, `node bin/kit.mjs <subcommand>`
// runs every other step. stdout carries only data (a project path, speech
// calls, an image name); everything else goes to stderr, prefixed with the
// subcommand. `check` and `render` start background jobs; `status` reports
// the one started last. Exit codes: 0 success, 1 a refused or failed step,
// 2 a usage error; `status` also exits 3 while a job runs and 4 when none
// has run. Imports only node: built-ins and the kit's own modules; no
// network.

import { createHash } from "node:crypto";
import { constants } from "node:fs";
import { chmod, copyFile, lstat, mkdir, mkdtemp, open, readFile, readdir, realpath, rename, rm, rmdir, symlink } from "node:fs/promises";
import { basename, dirname, extname, isAbsolute, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { check } from "./check.mjs";
import { PROJECT_NAME, Refusal, lstatOrNull, notFound, publicDir, readScript, sayer, writeAtomic } from "./kit-io.mjs";
import { render } from "./render.mjs";
import { status } from "./status.mjs";
import { timeline } from "./timeline.mjs";
import { blockStems } from "./voice-stem.mjs";

const NOT_COPIED = new Set(["node_modules", "tests", "out", "gallery", ".kit"]);
const IMAGE_EXTENSIONS = new Set(["png", "jpg", "jpeg", "webp"]);
const IMAGE_STEM = 60;

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
  timeline: { usage: "node bin/kit.mjs timeline", args: 0, fromTemplate: false, run: timeline },
  check: { usage: "node bin/kit.mjs check", args: 0, fromTemplate: false, run: check },
  render: { usage: "node bin/kit.mjs render", args: 0, fromTemplate: false, run: render },
  status: { usage: "node bin/kit.mjs status", args: 0, fromTemplate: false, run: status },
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
  const say = sayer(io, name);
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
