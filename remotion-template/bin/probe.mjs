// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The kit probe's Node side: probe frames, the render that measures them,
// and the four text rules over the result. probe() bundles an entry that
// registers src/kit/probe/Probe.tsx's KitProbe, renders each target's
// entrance-end, midpoint and pre-exit frames once, and collects the page's
// `kit-probe <JSON>` lines into the result (version 1). It must run with the
// working directory at the project root: Remotion finds the headless shell
// under node_modules/.remotion there. The bundle goes to a fresh directory
// under os.tmpdir(), removed when probe() settles.

import { bundle } from "@remotion/bundler";
import { renderFrames, selectComposition } from "@remotion/renderer";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { escapeLine } from "./script-schema.mjs";

const COMPOSITION = "KitProbe";
const RECORD_PREFIX = "kit-probe ";
const LINE_START_MARKS = new Set(Array.from("，。！？、；：）」』"));
const TOLERANCE = 0.5;

const round = (n) => Math.round(n * 100) / 100;
const shown = (value) => (typeof value === "string" ? JSON.stringify(value) : String(value));
const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

function fail(message) {
  throw new Error(`kit probe: ${message}`);
}

function requireInteger(value, minimum, field) {
  if (!Number.isInteger(value) || value < minimum) {
    throw new RangeError(`${field} must be an integer of at least ${minimum}, got ${shown(value)}`);
  }
}

export function probeFrames({ durationInFrames, entranceFrames, exitFrame } = {}) {
  requireInteger(durationInFrames, 1, "durationInFrames");
  requireInteger(entranceFrames, 0, "entranceFrames");
  requireInteger(exitFrame, 1, "exitFrame");
  if (exitFrame > durationInFrames) {
    throw new RangeError(`exitFrame must be at most durationInFrames ${durationInFrames}, got ${exitFrame}`);
  }
  const preExit = exitFrame - 1;
  const entranceEnd = Math.min(entranceFrames, preExit);
  const midpoint = Math.min(Math.max(Math.floor(durationInFrames / 2), entranceEnd), preExit);
  return [
    { role: "entrance-end", frame: entranceEnd },
    { role: "midpoint", frame: midpoint },
    { role: "pre-exit", frame: preExit },
  ];
}

// Targets with their probe frames and composition offsets.
function placeTargets(targets) {
  if (!Array.isArray(targets) || targets.length === 0) fail("targets must be a non-empty array");
  const ids = new Set();
  let offset = 0;
  return targets.map((target, i) => {
    if (!isObject(target)) fail(`target ${i + 1} must be an object`);
    const { id, scene, props, durationInFrames, entranceFrames, exitFrame } = target;
    if (typeof id !== "string" || id === "") fail(`target ${i + 1}: id must be a non-empty string`);
    if (ids.has(id)) fail(`target ${i + 1}: id ${JSON.stringify(id)} is not unique`);
    ids.add(id);
    const name = `target ${JSON.stringify(id)}`;
    if (typeof scene !== "string" || scene === "") fail(`${name}: scene must be a non-empty string`);
    if (!isObject(props)) fail(`${name}: props must be an object`);
    let frames;
    try {
      frames = probeFrames({ durationInFrames, entranceFrames, exitFrame });
    } catch (error) {
      fail(`${name}: ${error.message}`);
    }
    const placed = { id, scene, props, durationInFrames, offset, frames };
    offset += durationInFrames;
    return placed;
  });
}

// One parsed record per index, in the order of `indexes`.
export function collectRecords(lines, indexes) {
  const expected = new Set(indexes);
  const found = new Map();
  for (const line of lines) {
    if (!line.startsWith(RECORD_PREFIX)) continue;
    let record;
    try {
      record = JSON.parse(line.slice(RECORD_PREFIX.length));
    } catch (error) {
      fail(`unreadable record (${escapeLine(error.message)})`);
    }
    const index = isObject(record) ? record.index : undefined;
    if (!expected.has(index)) fail(`record for unexpected frame ${shown(index)}`);
    if (found.has(index)) fail(`duplicate record for frame ${index}`);
    found.set(index, record);
  }
  return indexes.map((index) => {
    if (!found.has(index)) fail(`no record for frame ${index}`);
    return found.get(index);
  });
}

// Keeps Remotion's message; names the target and its own frame when the
// error carries the composition frame.
function renderFailure(error, placed) {
  const message = error instanceof Error ? error.message : String(error);
  const frame = error?.frame;
  const target = Number.isInteger(frame)
    ? placed.find((t) => frame >= t.offset && frame < t.offset + t.durationInFrames)
    : undefined;
  const where = target ? `target ${JSON.stringify(target.id)} frame ${frame - target.offset}: ` : "";
  return new Error(`kit probe: ${where}${message}`, { cause: error });
}

export async function probe({ entryPoint, targets } = {}) {
  const placed = placeTargets(targets);
  if (typeof entryPoint !== "string" || entryPoint === "") fail("entryPoint must be a non-empty path");
  const indexes = [...new Set(placed.flatMap((t) => t.frames.map(({ frame }) => t.offset + frame)))].sort((a, b) => a - b);
  const inputProps = {
    targets: placed.map(({ scene, props, durationInFrames }) => ({ scene, props, durationInFrames })),
  };
  const dir = await mkdtemp(join(tmpdir(), "kit-probe-"));
  try {
    const lines = [];
    let composition;
    try {
      // remotion.config.ts applies only to `npx remotion`; the project's
      // node_modules is the image's root-owned one, so no webpack cache.
      const serveUrl = await bundle({ entryPoint: resolve(entryPoint), outDir: join(dir, "bundle"), enableCaching: false });
      composition = await selectComposition({ serveUrl, id: COMPOSITION, inputProps });
      await renderFrames({
        composition,
        serveUrl,
        inputProps,
        frames: indexes,
        imageFormat: "none",
        outputDir: null,
        // One page renders every frame in order. Each extra page would first
        // mount at its own initial frame and log a record for it, then seek
        // to whichever frame the pool hands it: a duplicate record.
        concurrency: 1,
        onStart: () => {},
        onFrameUpdate: () => {},
        onBrowserLog: (log) => lines.push(log.text),
      });
    } catch (error) {
      throw renderFailure(error, placed);
    }
    const collected = collectRecords(lines, indexes);
    const byIndex = new Map(indexes.map((index, k) => [index, collected[k]]));
    // A record serves every role whose frame it is; each frame gets its own copy.
    const frameOf = (t, role, frame) => {
      const measurement = structuredClone(byIndex.get(t.offset + frame));
      delete measurement.index;
      return { target: t.id, role, frame, ...measurement };
    };
    return {
      version: 1,
      canvas: { width: composition.width, height: composition.height },
      frames: placed.flatMap((t) => t.frames.map(({ role, frame }) => frameOf(t, role, frame))),
    };
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}

// --- rules ------------------------------------------------------------------

function crossedEdges(glyph, safe) {
  return [
    ["left", safe.left - glyph.x],
    ["top", safe.top - glyph.y],
    ["right", glyph.x + glyph.width - safe.right],
    ["bottom", glyph.y + glyph.height - safe.bottom],
  ]
    .map(([edge, distance]) => [edge, round(distance)])
    .filter(([, distance]) => distance > TOLERANCE)
    .map(([edge, distance]) => `${edge} ${distance} px`);
}

function contains(outer, inner) {
  return (
    round(outer.x - inner.x) <= TOLERANCE &&
    round(outer.y - inner.y) <= TOLERANCE &&
    round(inner.x + inner.width - (outer.x + outer.width)) <= TOLERANCE &&
    round(inner.y + inner.height - (outer.y + outer.height)) <= TOLERANCE
  );
}

function intersects(a, b) {
  const overlapX = Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x);
  const overlapY = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y);
  return round(overlapX) > TOLERANCE && round(overlapY) > TOLERANCE;
}

const isInside = (path, ancestor) => ancestor === "" || path === ancestor || path.startsWith(`${ancestor}>`);

// Records at or inside one marked element form a block (the outermost
// marked element when they nest); every other record is its own block.
function textBlocks(frame) {
  const blocks = [];
  const byMark = new Map();
  for (const record of frame.texts) {
    const mark = frame.marked
      .map(({ path }) => path)
      .filter((path) => isInside(record.path, path))
      .sort((a, b) => a.length - b.length)[0];
    if (mark === undefined) {
      blocks.push([record]);
      continue;
    }
    if (!byMark.has(mark)) {
      byMark.set(mark, []);
      blocks.push(byMark.get(mark));
    }
    byMark.get(mark).push(record);
  }
  return blocks;
}

export function violations(result, theme) {
  const safe = {
    left: theme.safeArea.left,
    top: theme.safeArea.top,
    right: theme.canvas.width - theme.safeArea.right,
    bottom: theme.canvas.height - theme.safeArea.bottom,
  };
  const minimum = theme.type.sizes.minimum;
  const found = [];
  for (const frame of result.frames) {
    const report = (rule, path, detail) =>
      found.push({ target: frame.target, role: frame.role, frame: frame.frame, rule, path, detail });
    for (const { path, input, rendered } of frame.marked) {
      if (rendered !== input) report("text differs", path, `rendered "${rendered}", input "${input}"`);
    }
    for (const record of frame.texts) {
      const crossed = crossedEdges(record.glyphBox, safe);
      if (crossed.length > 0) report("outside safe area", record.path, crossed.join(", "));
    }
    for (const { path, scrollWidth, clientWidth } of frame.scrolls) {
      report("scrolls", path, `scrollWidth ${scrollWidth} > clientWidth ${clientWidth}`);
    }
    for (const record of frame.texts) {
      if (record.minFontSizePx < minimum) report("font size", record.path, `${record.minFontSizePx} px < ${minimum} px`);
    }
    for (const block of textBlocks(frame)) {
      let n = 0;
      for (const record of block) {
        for (const line of record.lines) {
          n += 1;
          const mark = Array.from(line)[0];
          if (n > 1 && LINE_START_MARKS.has(mark)) report("line start", record.path, `line ${n} starts with "${mark}"`);
        }
      }
    }
    const masks = new Map(frame.masks.map((mask) => [mask.path, mask]));
    for (const record of frame.texts) {
      for (const path of record.clippedBy) {
        const mask = masks.get(path);
        if (mask === undefined) fail(`no mask entry for ${JSON.stringify(path)}`);
        if (mask.clipBox === null) {
          report("mask clips", record.path, `by ${path}: clip-path ${mask.clipPath} is not measurable`);
        } else if (intersects(mask.clipBox, record.glyphBox) && !contains(mask.clipBox, record.glyphBox)) {
          report("mask clips", record.path, `by ${path}`);
        }
      }
    }
  }
  return found;
}

export function formatViolation(v) {
  return `${v.target} ${v.role} frame ${v.frame}: ${v.rule}: ${v.path} ${escapeLine(v.detail)}`;
}
