// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// kit check: nine ordered stages, each a rule with its own messages. Every
// rule in a stage runs and collects its reasons. A failing stage 1 (schema)
// or 2 (timeline) stops the run. After stage 2 passes, the snapshot writes
// src/custom/index.ts and takes the passing-check hash
// (bin/project-hash.mjs) before any later stage reads the project. Stages
// 3–6 (assets, unnarrated stretches, rest after reveals, music) all run;
// stages 7 (the probe), 8 (the contact sheet) and 9 (the hash again) run
// only when every earlier stage passed. Stage 9 moves the sheet into place
// only when the project still hashes as at the snapshot, so a pass vouches
// for what was checked. Each rule is exported on its own and takes its inputs as
// arguments, so tests call it with hand-written timelines, reveals and a
// stub file reader. Pure apart from the reads through that reader, the
// hash, the custom index and the sheet's files under .kit/check/. Only the
// check worker (bin/check.mjs) loads this module, by dynamic import: it
// imports the probe and the contact sheet, and through them Remotion.

import { createHash } from "node:crypto";
import { readFile, rename, rm } from "node:fs/promises";
import { join } from "node:path";
import { DEFAULT_PRESET } from "../src/kit/presets.ts";
import { themeFor } from "../src/kit/theme.ts";
import { contactSheet, sheetTiles } from "./contact-sheet.mjs";
import { writeCustomIndex } from "./custom-index.mjs";
import { explainerScenes } from "./explainer-scenes.mjs";
import { Refusal, lstatOrNull, notFound } from "./kit-io.mjs";
import { MUSIC_DIR, musicReasons } from "./music.mjs";
import { formatViolation, probe as kitProbe, violations } from "./probe.mjs";
import { projectHash } from "./project-hash.mjs";
import { ENTRANCE_FRAMES, REST_FRAMES, REVEALS, layout, revealsOf } from "./scene-timing.mjs";
import { escapeLine, fieldPath, playbackWhere, validateScript } from "./script-schema.mjs";
import { lengthError } from "./timeline.mjs";
import { blockStems } from "./voice-stem.mjs";

const TIMELINE_HINT = "run node bin/kit.mjs timeline";
const STALE = "timeline.json is stale (its text, voice or instructions changed); run node bin/kit.mjs voice, voice-import, then timeline";
const MAX_UNNARRATED = 90;
const ENTRY_POINT = "src/kit/index.ts";
const PUBLIC_DIR = "public";
const FONT = "Noto Sans CJK SC";
const SHEET = ".kit/check/contact.png";
const SHEET_NEXT = ".kit/check/contact.next.png";
const SHEET_WORK = ".kit/check/sheet";
const CHANGED = "the project changed while the check ran; run node bin/kit.mjs check again";

const sha256 = (data) => createHash("sha256").update(data).digest("hex");
const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
// Seconds to one decimal, rounded half up.
const seconds = (frames) => (Math.round(frames / 3) / 10).toFixed(1);

// Every timeline block in playback order with its where (1-based sentence
// and block numbers) and its place in the file.
function timelineBlocks(script, timeline) {
  return timeline.scenes.flatMap((scene, i) =>
    scene.sentences.flatMap((sentence, s) =>
      sentence.blocks.map((block, b) => ({
        block,
        path: ["scenes", i, "sentences", s, "blocks", b],
        where: `${playbackWhere(script, i + 1)}, sentence ${s + 1} block ${b + 1}`,
      })),
    ),
  );
}

// --- 1. schema ---------------------------------------------------------------

// text: script.json's text, or null when it is absent. Returns the reasons
// and the validated script.
export function schemaRule(text) {
  if (text === null) return { reasons: ["script.json: not found"], script: null };
  const result = validateScript(text);
  if (result.ok) return { reasons: [], script: result.script };
  return { reasons: result.errors.map((error) => error.message), script: null };
}

// --- 2. timeline ---------------------------------------------------------------

// The path of the first difference between expected and actual, or null.
function firstDifference(expected, actual, path = []) {
  if (Object.is(expected, actual)) return null;
  const sameShape = (isObject(expected) && isObject(actual)) || (Array.isArray(expected) && Array.isArray(actual));
  if (!sameShape) return path;
  const keys = [...new Set([...Object.keys(expected), ...Object.keys(actual)])];
  for (const key of keys) {
    const step = Array.isArray(expected) ? Number(key) : key;
    const found = firstDifference(expected[key], actual[key], [...path, step]);
    if (found !== null) return found;
  }
  return null;
}

const withoutHashes = (scene) => ({
  ...scene,
  sentences: scene.sentences.map((sentence) => ({
    ...sentence,
    blocks: sentence.blocks.map(({ audioSha256: _, ...block }) => block),
  })),
});

// layout() over the file's own sample counts, compared with the file: the
// total, then each scene's length, then any other field.
function relayoutReason(script, timeline, blocks) {
  const laid = layout(
    script,
    blockStems(script).map((entry, k) => ({ ...entry, samples: blocks[k].block.endSample - blocks[k].block.startSample })),
  );
  if (timeline.totalFrames !== laid.totalFrames) {
    return `timeline.json totals ${timeline.totalFrames} frames, the script and narration need ${laid.totalFrames}; ${TIMELINE_HINT}`;
  }
  for (const [i, expected] of laid.scenes.entries()) {
    const actual = withoutHashes(timeline.scenes[i]);
    const where = playbackWhere(script, i + 1);
    const [a, b] = [actual.endFrame - actual.startFrame, expected.endFrame - expected.startFrame];
    if (a !== b) return `${where}: timeline.json lasts ${a} frames, the script and narration need ${b}; ${TIMELINE_HINT}`;
    const path = firstDifference(expected, actual);
    if (path !== null) {
      return `${where}: timeline.json disagrees with the script and narration at ${fieldPath(["scenes", i, ...path])}; ${TIMELINE_HINT}`;
    }
  }
  return null;
}

// text: timeline.json's text, or null when it is absent. Each check after
// explainerScenes runs only when the one before it passed: the blocks'
// stem and sample types, the stems against blockStems, and the exact
// re-layout. Returns the reasons and the parsed timeline.
export function timelineRule(script, text) {
  let timeline;
  try {
    timeline = text === null ? undefined : JSON.parse(text);
  } catch (thrown) {
    if (!(thrown instanceof SyntaxError)) throw thrown;
  }
  if (timeline === undefined) return { reasons: [`timeline.json: missing or not JSON; ${TIMELINE_HINT}`], timeline: null };
  try {
    explainerScenes(script, timeline);
  } catch (thrown) {
    return { reasons: [thrown.message], timeline: null };
  }
  const reasons = [];
  const length = lengthError(timeline.totalFrames);
  if (length !== null) reasons.push(length);
  const blocks = timelineBlocks(script, timeline);
  const typed = blocks.flatMap(({ block, path }) =>
    [
      ["stem", (value) => typeof value === "string"],
      ["startSample", Number.isInteger],
      ["endSample", Number.isInteger],
    ]
      .filter(([key, ok]) => !ok(block[key]))
      .map(([key]) => `script: timeline.json ${fieldPath([...path, key])} is missing or has the wrong type; ${TIMELINE_HINT}`),
  );
  if (typed.length) return { reasons: [...reasons, ...typed], timeline: null };
  const stems = blockStems(script);
  const stale = blocks.filter(({ block }, k) => block.stem !== stems[k].stem).map(({ where }) => `${where}: ${STALE}`);
  if (stale.length) return { reasons: [...reasons, ...stale], timeline: null };
  const relaid = relayoutReason(script, timeline, blocks);
  if (relaid !== null) reasons.push(relaid);
  return { reasons, timeline: reasons.length ? null : timeline };
}

// --- 3. assets ---------------------------------------------------------------

// files.regular(path) resolves to the bytes of a regular file at path
// (relative to the project root, checked with lstat), or null. Checks, in
// order: scene images, block WAVs against their hashes, the narration
// against its hash, and the custom scene's file.
export async function assetsRule(script, timeline, files) {
  const scenes = explainerScenes(script, timeline);
  const reasons = [];
  for (const { where, props } of scenes) {
    if (props.image !== null && (await files.regular(`public/images/${props.image}`)) === null) {
      reasons.push(`${where}: image "${props.image}" is missing from public/images; run node bin/kit.mjs image "<path>"`);
    }
  }
  for (const { block, where } of timelineBlocks(script, timeline)) {
    const data = await files.regular(`public/voice/${block.stem}.wav`);
    if (data === null) reasons.push(`${where}: ${block.stem}.wav is missing`);
    else if (sha256(data) !== block.audioSha256) reasons.push(`${where}: ${block.stem}.wav changed since kit timeline; ${TIMELINE_HINT}`);
  }
  const narration = await files.regular(timeline.narration.file);
  if (narration === null) reasons.push("narration.wav is missing");
  else if (sha256(narration) !== timeline.narration.sha256) reasons.push(`narration.wav changed since kit timeline; ${TIMELINE_HINT}`);
  for (const { where, component } of scenes) {
    if (component !== null && (await files.regular(`src/custom/${component}.tsx`)) === null) {
      reasons.push(`${where}: src/custom/${component}.tsx is missing`);
    }
  }
  return reasons;
}

// --- 4. unnarrated stretches ------------------------------------------------------

// The frames between one block's end and the next block's start, in playback
// order across scenes, cut at the end card's start. The lead-in before the
// first block, the end card and the tail do not count.
export function unnarratedRule(script, timeline) {
  const { scenes } = timeline;
  const endCardStart = scenes.at(-1).startFrame;
  const blocks = scenes.flatMap((scene) => scene.sentences.flatMap((sentence) => sentence.blocks));
  const whereAt = (frame) => playbackWhere(script, scenes.findIndex((scene) => frame >= scene.startFrame && frame < scene.endFrame) + 1);
  const reasons = [];
  for (let k = 1; k < blocks.length; k++) {
    const from = blocks[k - 1].endFrame;
    const to = Math.min(blocks[k].startFrame, endCardStart);
    const frames = to - from;
    if (frames > MAX_UNNARRATED) {
      reasons.push(`${whereAt(from)}: ${frames} frames (${seconds(frames)} s) without narration, frames ${from}–${to}; at most ${MAX_UNNARRATED} (3 s)`);
    }
  }
  return reasons;
}

// --- 5. rest after reveals --------------------------------------------------------

// Each reveal rests until the next reveal's start, or until the scene's exit
// (explainerScenes' exit, or its end when it has none). Frames are relative
// to the scene.
export function restRule(script, timeline, reveals = REVEALS) {
  const reasons = [];
  for (const { where, kind, durationInFrames, exit, props } of explainerScenes(script, timeline)) {
    const list = [...revealsOf(kind, props.timing, reveals)].sort((a, b) => a.startFrame - b.startFrame);
    const exitFrame = exit?.startFrame ?? durationInFrames;
    list.forEach((reveal, i) => {
      const next = list[i + 1];
      const until = next === undefined ? exitFrame : next.startFrame;
      const rest = until - reveal.endFrame;
      const needs = REST_FRAMES[reveal.type];
      if (rest < needs) {
        const before = next === undefined ? "the exit" : "the next reveal";
        reasons.push(`${where}: ${reveal.type} reveal ending at frame ${reveal.endFrame} rests ${rest} frames before ${before} at frame ${until}; needs ${needs}`);
      }
    });
  }
  return reasons;
}

// --- 7. probe ---------------------------------------------------------------

// wrapLines' two throws (src/kit/text.ts) and probe()'s target, each quoted
// by JSON.stringify.
const UNWRAPPABLE = [
  [/opening marks of ("(?:[^"\\]|\\.)*") fill maxWidth (\d+)/, "its leading opening marks or spaces fill the line"],
  [/piece ("(?:[^"\\]|\\.)*") is wider than maxWidth (\d+)/, "one character with its punctuation marks is wider than the line"],
];
const TARGET = /target ("(?:[^"\\]|\\.)*") frame/;

// The string leaves of value in field order, each with its path.
function* stringLeaves(value, path = []) {
  if (typeof value === "string") yield { path, value };
  else if (isObject(value) || Array.isArray(value)) {
    for (const [key, inner] of Object.entries(value)) yield* stringLeaves(inner, [...path, Array.isArray(value) ? Number(key) : key]);
  }
}

// The reason naming the field that holds the text of a wrapLines throw in
// message: in the target's scene, or in every scene when message names no
// target. Null when message holds no such throw, a quoted part does not
// parse, or no field holds the text.
function unwrappableReason(scenes, message) {
  const found = UNWRAPPABLE.map(([pattern, cause]) => ({ match: pattern.exec(message), cause })).find(({ match }) => match !== null);
  if (found === undefined) return null;
  const [, quoted, width] = found.match;
  const target = TARGET.exec(message)?.[1];
  let text, where;
  try {
    text = JSON.parse(quoted);
    where = target === undefined ? null : JSON.parse(target);
  } catch (thrown) {
    if (thrown instanceof SyntaxError) return null;
    throw thrown;
  }
  for (const scene of scenes.filter((entry) => where === null || entry.where === where)) {
    if (!isObject(scene.props.content)) continue;
    for (const leaf of stringLeaves(scene.props.content)) {
      if (leaf.value.includes(text)) {
        const field = fieldPath(["content", ...leaf.path]);
        return `${scene.where}: ${field} "${escapeLine(leaf.value)}" cannot be wrapped in ${width} characters: ${found.cause}; shorten that run`;
      }
    }
  }
  return null;
}

// Writes src/custom/index.ts, then probes every scene through the kit's own
// KitProbe at its entrance end, midpoint and pre-exit frames.
export async function probeRule(root, script, timeline, { probe, theme }) {
  try {
    await writeCustomIndex(root, script);
  } catch (thrown) {
    if (thrown instanceof Refusal) return [`custom index: ${thrown.message}`];
    throw thrown;
  }
  const scenes = explainerScenes(script, timeline);
  const targets = scenes.map(({ where, kind, durationInFrames, exit, props }) => ({
    id: where,
    scene: kind,
    props: { ...props, theme },
    durationInFrames,
    entranceFrames: ENTRANCE_FRAMES[kind],
    exitFrame: exit?.startFrame ?? durationInFrames,
  }));
  let result;
  try {
    result = await probe({ entryPoint: ENTRY_POINT, publicDir: PUBLIC_DIR, targets });
  } catch (thrown) {
    const message = thrown instanceof Error ? thrown.message : String(thrown);
    // probe() wraps the scene's own message, so it is matched as a substring.
    const kind = /unknown scene "([^"]*)"/.exec(message)?.[1];
    const first = scenes.find((scene) => scene.kind === kind);
    if (first !== undefined) return [`${first.where}: no component for kind "${kind}"`];
    // A wrapLines throw names its scene and field; any other error is the message.
    return [unwrappableReason(scenes, message) ?? `probe: ${message}`];
  }
  const reasons = violations(result, theme).map(formatViolation);
  if (result.frames.some((frame) => frame.fontPresent === false)) reasons.push(`font "${FONT}" is not installed`);
  return reasons;
}

// --- the snapshot ---------------------------------------------------------------

// After stage 2: writes src/custom/index.ts, then hashes the project with
// the texts stages 1 and 2 judged. A writeCustomIndex refusal records no
// snapshot and no reason: the probe stage's own call reports it.
async function snapshot(state) {
  try {
    await writeCustomIndex(state.root, state.script);
  } catch (thrown) {
    if (thrown instanceof Refusal) return [];
    throw thrown;
  }
  const taken = await projectHash(state.root, {
    script: state.script,
    timeline: state.timeline,
    scriptText: state.scriptText,
    timelineText: state.timelineText,
    musicDir: state.musicDir,
  });
  if (taken.reasons) return taken.reasons;
  state.snapshot = taken.hash;
  return [];
}

// --- 8. contact sheet --------------------------------------------------------------

// Renders the sheet to contact.next.png; a thrown error is the reason, and
// leaves no next sheet.
async function sheetRule(state) {
  const tiles = sheetTiles(explainerScenes(state.script, state.timeline));
  const output = join(state.root, SHEET_NEXT);
  try {
    // The Explainer's input props are the parsed files, as kit render passes them.
    await state.sheet(state.root, {
      script: JSON.parse(state.scriptText),
      timeline: JSON.parse(state.timelineText),
      tiles,
      workDir: join(state.root, SHEET_WORK),
      output,
    });
  } catch (thrown) {
    await rm(output, { force: true });
    return [`contact sheet: ${thrown instanceof Error ? thrown.message : String(thrown)}`];
  }
  state.tiles = tiles;
  return [];
}

// --- 9. hash -------------------------------------------------------------------

// Hashes the project again with the texts as they are now. Only when it
// matches the snapshot does the sheet move into place and the run carry its
// pass; otherwise, or when this throws, the next sheet is removed.
async function hashRule(state) {
  const next = join(state.root, SHEET_NEXT);
  let moved = false;
  try {
    const scriptText = await state.files.text("script.json");
    const timelineText = await state.files.text("timeline.json");
    if (state.snapshot === null || scriptText === null || timelineText === null) return [CHANGED];
    const again = await projectHash(state.root, {
      script: state.script,
      timeline: state.timeline,
      scriptText,
      timelineText,
      musicDir: state.musicDir,
    });
    if (again.reasons || again.hash !== state.snapshot) return [CHANGED];
    await rename(next, join(state.root, SHEET));
    moved = true;
    state.pass = { hash: again.hash, sheet: SHEET, tiles: state.tiles };
    return [];
  } finally {
    if (!moved) await rm(next, { force: true });
  }
}

// --- the stages ---------------------------------------------------------------

// The project's files, read from root: text() follows symlinks and gives
// null for an absent file; regular() gives null for anything but a regular
// file.
function projectFiles(root) {
  return {
    async text(path) {
      try {
        return await readFile(join(root, path), "utf8");
      } catch (thrown) {
        if (notFound(thrown)) return null;
        throw thrown;
      }
    },
    async regular(path) {
      const stat = await lstatOrNull(join(root, path));
      return stat?.isFile() ? readFile(join(root, path)) : null;
    },
  };
}

// The stages in order. stops: a failure ends the run; snapshot: the
// snapshot follows a pass; needsPass: runs only when every earlier stage
// passed.
export const RULES = [
  {
    name: "schema",
    stops: true,
    async run(state) {
      state.scriptText = await state.files.text("script.json");
      const { reasons, script } = schemaRule(state.scriptText);
      state.script = script;
      // An injected theme wins; otherwise the script's preset selects it.
      if (script !== null) state.theme ??= themeFor(script.preset ?? DEFAULT_PRESET);
      return reasons;
    },
  },
  {
    name: "timeline",
    stops: true,
    snapshot: true,
    async run(state) {
      state.timelineText = await state.files.text("timeline.json");
      const { reasons, timeline } = timelineRule(state.script, state.timelineText);
      state.timeline = timeline;
      return reasons;
    },
  },
  { name: "assets", run: (state) => assetsRule(state.script, state.timeline, state.files) },
  { name: "unnarrated", run: (state) => unnarratedRule(state.script, state.timeline) },
  { name: "rest", run: (state) => restRule(state.script, state.timeline, state.reveals) },
  { name: "music", run: (state) => musicReasons(state.script, state.timeline, state.musicDir) },
  { name: "probe", needsPass: true, run: (state) => probeRule(state.root, state.script, state.timeline, state) },
  { name: "contact-sheet", needsPass: true, run: sheetRule },
  { name: "hash", needsPass: true, run: hashRule },
];

// Runs the stages over the project at root and resolves to {reasons, pass}:
// pass is {hash, sheet, tiles} when the reasons are empty, null otherwise.
// log receives `check: stage <n> <name>` before each stage and `check:
// snapshot` before the snapshot. sheet renders the contact sheet (tests
// inject a stub). musicDir is the music library that the music stage and
// both hashes read. theme, when given, replaces the one the script's preset
// selects (themeFor), resolved once stage 1 parses the script. A system
// error from a read, the hash or the custom index propagates.
export async function runChecks(
  root,
  {
    probe = kitProbe,
    reveals = REVEALS,
    theme,
    files = projectFiles(root),
    log = () => {},
    sheet = contactSheet,
    musicDir = MUSIC_DIR,
  } = {},
) {
  const state = { root, probe, reveals, theme, files, sheet, musicDir, script: null, timeline: null, snapshot: null, tiles: null, pass: null };
  const reasons = [];
  for (const [i, rule] of RULES.entries()) {
    if (rule.needsPass && reasons.length) break;
    log(`check: stage ${i + 1} ${rule.name}`);
    reasons.push(...(await rule.run(state)));
    if (rule.stops && reasons.length) break;
    if (rule.snapshot) {
      log("check: snapshot");
      reasons.push(...(await snapshot(state)));
    }
  }
  return { reasons, pass: reasons.length ? null : state.pass };
}
