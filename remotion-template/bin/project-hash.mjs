// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The passing-check hash: what a passing kit check vouches for, and what
// kit render compares before it starts. It covers the texts of script.json
// and timeline.json that the check judged, every regular file under src/,
// the public/ files the check's assets stage reads, and, for a project that
// names a music track, that track's library file. It depends on paths
// and contents only, never on mtime or mode, and follows no symlink: every
// file is opened with O_NOFOLLOW | O_NONBLOCK and judged by fstat. Imports
// only node: built-ins and the kit's own Remotion-free modules, so render's
// start path can load it.

import { createHash } from "node:crypto";
import { constants, open, readdir } from "node:fs/promises";
import { join } from "node:path";
import { explainerScenes } from "./explainer-scenes.mjs";
import { Refusal, lstatOrNull, notFound } from "./kit-io.mjs";
import { MUSIC_DIR, readLibrary } from "./music.mjs";

const sha256 = (data) => createHash("sha256").update(data).digest("hex");
const byCodeUnit = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
const NON_REGULAR = "is not a regular file or directory; the check hashes regular files only";

// script: validated; timeline: one that passed the check's stage 2. The
// sorted, de-duplicated project-relative paths of each scene's image, each
// block's WAV and the narration track.
export function referencedPublicFiles(script, timeline) {
  const paths = new Set();
  for (const { props } of explainerScenes(script, timeline)) {
    if (props.image !== null) paths.add(`public/images/${props.image}`);
  }
  for (const scene of timeline.scenes) {
    for (const sentence of scene.sentences) for (const block of sentence.blocks) paths.add(`public/voice/${block.stem}.wav`);
  }
  paths.add(timeline.narration.file);
  return [...paths].sort(byCodeUnit);
}

// Reads one file to hash: opened with O_NONBLOCK | O_NOFOLLOW, so neither a
// FIFO nor a final symlink is followed or waited on, then judged by fstat
// on the handle. Resolves to {kind: "file", bytes}, {kind: "other"} (a
// symlink, FIFO, directory or device), or {kind: "vanished"} when the path
// is gone. Any other system error propagates.
export async function readHashed(path) {
  let handle;
  try {
    handle = await open(path, constants.O_RDONLY | constants.O_NONBLOCK | constants.O_NOFOLLOW);
  } catch (thrown) {
    if (thrown?.code === "ELOOP") return { kind: "other" };
    if (notFound(thrown)) return { kind: "vanished" };
    throw thrown;
  }
  try {
    if (!(await handle.stat()).isFile()) return { kind: "other" };
    return { kind: "file", bytes: await handle.readFile() };
  } finally {
    await handle.close();
  }
}

// Every regular file under dir (project-relative path rel), as [path,
// sha256]; anything but a directory or a regular file is a reason. A
// directory is found by lstat; every other entry is read through read(),
// so a FIFO is never waited on. An entry gone before it is read is skipped:
// the second hash read sees the change.
async function walk(root, rel, entries, reasons, read) {
  for (const name of (await readdir(join(root, rel))).sort(byCodeUnit)) {
    const path = `${rel}/${name}`;
    const stat = await lstatOrNull(join(root, path));
    if (stat === null) continue;
    if (stat.isDirectory()) {
      await walk(root, path, entries, reasons, read);
      continue;
    }
    const file = await read(join(root, path));
    if (file.kind === "file") entries.push([path, sha256(file.bytes)]);
    else if (file.kind === "other") reasons.push(`${path}: ${NON_REGULAR}`);
  }
}

// The SHA-256 of the library file of the track the script names, or null
// when the manifest, the entry or the file cannot be read: the check's
// music stage reports why. Read through readHashed, never the injectable
// reader, so a symlink or non-regular file is null.
async function trackHash(id, musicDir) {
  try {
    const track = (await readLibrary(musicDir)).tracks.find((entry) => entry.id === id);
    if (track === undefined) return null;
    const file = await readHashed(join(musicDir, track.file));
    return file.kind === "file" ? sha256(file.bytes) : null;
  } catch (thrown) {
    if (thrown instanceof Refusal || typeof thrown?.code === "string") return null;
    throw thrown;
  }
}

// Resolves to {hash} or {reasons}. A referenced file that is absent, gone
// before it is read, or not a regular file is a null entry: the assets
// stage reports it. A system error from a read propagates. read replaces
// readHashed (tests interleave a removal). A project that names a music
// track adds ["music:<id>", sha256 | null]; a "none" project reads nothing
// under musicDir.
export async function projectHash(root, { script, timeline, scriptText, timelineText, read = readHashed, musicDir = MUSIC_DIR }) {
  const src = await lstatOrNull(join(root, "src"));
  if (src === null) return { reasons: ["src: is missing"] };
  if (src.isSymbolicLink()) return { reasons: ["src: is a symlink"] };
  if (!src.isDirectory()) return { reasons: ["src: is not a directory"] };
  const entries = [
    ["script.json", sha256(scriptText)],
    ["timeline.json", sha256(timelineText)],
  ];
  const reasons = [];
  await walk(root, "src", entries, reasons, read);
  if (reasons.length) return { reasons };
  for (const path of referencedPublicFiles(script, timeline)) {
    const file = await read(join(root, path));
    entries.push([path, file.kind === "file" ? sha256(file.bytes) : null]);
  }
  if (script.music !== "none") entries.push([`music:${script.music}`, await trackHash(script.music, musicDir)]);
  entries.sort((a, b) => byCodeUnit(a[0], b[0]));
  return { hash: sha256(JSON.stringify(entries)) };
}
