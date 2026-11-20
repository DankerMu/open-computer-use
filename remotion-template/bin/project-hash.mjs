// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The passing-check hash: what a passing kit check vouches for, and what
// kit render compares before it starts. It covers the texts of script.json
// and timeline.json that the check judged, every regular file under src/,
// and the public/ files the check's assets stage reads. It depends on paths
// and contents only, never on mtime or mode, and follows no symlink. Imports
// only node: built-ins and the kit's own Remotion-free modules, so render's
// start path can load it.

import { createHash } from "node:crypto";
import { readFile, readdir } from "node:fs/promises";
import { join } from "node:path";
import { explainerScenes } from "./explainer-scenes.mjs";
import { lstatOrNull } from "./kit-io.mjs";

const sha256 = (data) => createHash("sha256").update(data).digest("hex");
const byCodeUnit = (a, b) => (a < b ? -1 : a > b ? 1 : 0);

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

// Every regular file under dir (project-relative path rel), as [path,
// sha256]; anything but a directory or a regular file is a reason. Every
// entry is classified by lstat before it is read, so a FIFO is never opened.
async function walk(root, rel, entries, reasons) {
  for (const name of (await readdir(join(root, rel))).sort(byCodeUnit)) {
    const path = `${rel}/${name}`;
    const stat = await lstatOrNull(join(root, path));
    // Removed between readdir and lstat: the second hash read sees the change.
    if (stat === null) continue;
    if (stat.isDirectory()) await walk(root, path, entries, reasons);
    else if (stat.isFile()) entries.push([path, sha256(await readFile(join(root, path)))]);
    else reasons.push(`${path}: is not a regular file or directory; the check hashes regular files only`);
  }
}

// Resolves to {hash} or {reasons}. A referenced file that is absent or not
// a regular file is a null entry: the assets stage reports it. A system
// error from a read propagates.
export async function projectHash(root, { script, timeline, scriptText, timelineText }) {
  const src = await lstatOrNull(join(root, "src"));
  if (src === null) return { reasons: ["src: is missing"] };
  if (src.isSymbolicLink()) return { reasons: ["src: is a symlink"] };
  if (!src.isDirectory()) return { reasons: ["src: is not a directory"] };
  const entries = [
    ["script.json", sha256(scriptText)],
    ["timeline.json", sha256(timelineText)],
  ];
  const reasons = [];
  await walk(root, "src", entries, reasons);
  if (reasons.length) return { reasons };
  for (const path of referencedPublicFiles(script, timeline)) {
    const stat = await lstatOrNull(join(root, path));
    entries.push([path, stat?.isFile() ? sha256(await readFile(join(root, path))) : null]);
  }
  entries.sort((a, b) => byCodeUnit(a[0], b[0]));
  return { hash: sha256(JSON.stringify(entries)) };
}
