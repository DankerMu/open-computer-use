// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
// Source: Vincentwei1021/anything2explainer 4f738189d866a8a1d1abd008456303617279a2c2 reference/narration-storyboard.md PolyForm-Noncommercial-1.0.0
//
// Version 1 of the explainer kit's script.json: a hook, 3–5 chapters of body
// scenes, a summary and an end card, with narration in caption blocks. Only
// the narration shape is borrowed from the source above, no code.
// validateScript is the one entry point for checking a script: it adds the
// one-custom rule that JSON Schema cannot express and maps every problem to
// the chapter, scene, field and limit. Each check's message is its limit, so
// the limit text sits beside the number it describes. String lengths use
// zod's .min()/.max(), which count code points and survive into the JSON
// Schema as minLength/maxLength.

import { z } from "zod";

export const VIDEO = { width: 1920, height: 1080, fps: 30 };

export const LIMITS = {
  title: 24,
  chapters: { min: 3, max: 5 },
  chapterName: 6,
  scenes: { min: 1, max: 4 },
  sentences: { min: 1, max: 4 },
  endCardSentences: { min: 0, max: 4 },
  blocks: { min: 1, max: 4 },
  block: 16,
  customScenes: 1,
  musicId: 40,
  voice: { id: 64, instructions: 200 },
  bytes: 1024 * 1024,
  echo: 40,
  content: {
    hook: { title: 16, subtitle: 24 },
    question: { question: 24 },
    "concept-card": { kicker: 8, title: 12, points: { min: 1, max: 4 }, point: 16, conclusion: 20 },
    "process-steps": { steps: { min: 2, max: 5 }, label: 8, detail: 16 },
    "cause-effect": { cause: 16, effect: 16, link: 8 },
    comparison: { title: 8, points: { min: 1, max: 4 }, point: 12 },
    timeline: { events: { min: 2, max: 6 }, year: 8, label: 12, detail: 16 },
    "key-number": { unit: 4, label: 12 },
    chart: { title: 12, unit: 4, points: { min: 2, max: 8 }, label: 6 },
    "image-callouts": { callouts: { min: 1, max: 3 }, label: 10 },
    "definition-quote": { term: 10, text: 40, source: 16 },
    summary: { points: { min: 1, max: 4 }, point: 16 },
    "end-card": { title: 16, subtitle: 24 },
  },
};

export const BODY_KINDS = [
  "question",
  "concept-card",
  "process-steps",
  "cause-effect",
  "comparison",
  "timeline",
  "key-number",
  "chart",
  "image-callouts",
  "definition-quote",
];

export const IMAGE_NAME = /^(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.(png|jpg|jpeg|webp)$/;

const CUSTOM = "custom";
const CHAPTER_KINDS = [...BODY_KINDS, CUSTOM];
const MUSIC_ID = new RegExp(`^[a-z0-9][a-z0-9-]{0,${LIMITS.musicId - 1}}$`);
const VOICE_ID = /^[A-Za-z0-9._-]*$/;
const COMPONENT = /^[A-Z][A-Za-z0-9]{0,39}$/;
const NON_EMPTY = "non-empty";
const SLOTS = { hook: "hook", summary: "summary", endCard: "end-card" };

const chars = (max) => z.string().min(1, NON_EMPTY).max(max, `${max} characters`);
const list = (item, { min, max }, noun) => {
  const limit = `${min}–${max} ${noun}`;
  return z.array(item).min(min, limit).max(max, limit);
};
const ratio = z.number().min(0, "0–1").max(1, "0–1");
const optional = (max) => chars(max).optional();

const C = LIMITS.content;
const sentence = list(chars(LIMITS.block), LIMITS.blocks, "blocks");
const narration = list(sentence, LIMITS.sentences, "sentences");
const image = z.string().regex(IMAGE_NAME, "plain image name (.png, .jpg, .jpeg or .webp)");
const points = (limits) => list(chars(limits.point), limits.points, "points");
const side = z.strictObject({ title: chars(C.comparison.title), points: points(C.comparison) });

const CONTENT = {
  hook: { title: chars(C.hook.title), subtitle: optional(C.hook.subtitle) },
  question: { question: chars(C.question.question) },
  "concept-card": {
    kicker: optional(C["concept-card"].kicker),
    title: chars(C["concept-card"].title),
    points: points(C["concept-card"]),
    conclusion: optional(C["concept-card"].conclusion),
  },
  "process-steps": {
    steps: list(
      z.strictObject({ label: chars(C["process-steps"].label), detail: optional(C["process-steps"].detail) }),
      C["process-steps"].steps,
      "steps",
    ),
  },
  "cause-effect": {
    cause: chars(C["cause-effect"].cause),
    effect: chars(C["cause-effect"].effect),
    link: optional(C["cause-effect"].link),
  },
  comparison: { left: side, right: side },
  timeline: {
    events: list(
      z.strictObject({
        year: chars(C.timeline.year),
        label: chars(C.timeline.label),
        detail: optional(C.timeline.detail),
      }),
      C.timeline.events,
      "events",
    ),
  },
  "key-number": { value: z.number(), unit: optional(C["key-number"].unit), label: chars(C["key-number"].label) },
  chart: {
    type: z.enum(["bar", "line"]),
    title: chars(C.chart.title),
    unit: optional(C.chart.unit),
    points: list(z.strictObject({ label: chars(C.chart.label), value: z.number() }), C.chart.points, "points"),
  },
  "image-callouts": {
    callouts: list(
      z.strictObject({ x: ratio, y: ratio, label: chars(C["image-callouts"].label) }),
      C["image-callouts"].callouts,
      "callouts",
    ),
  },
  "definition-quote": {
    term: chars(C["definition-quote"].term),
    text: chars(C["definition-quote"].text),
    source: optional(C["definition-quote"].source),
  },
  summary: { points: points(C.summary) },
  "end-card": { title: chars(C["end-card"].title), subtitle: optional(C["end-card"].subtitle) },
};

function scene(kind, sceneNarration = narration) {
  return z.strictObject({
    kind: z.literal(kind),
    narration: sceneNarration,
    image: kind === "image-callouts" ? image : image.optional(),
    content: z.strictObject(CONTENT[kind]),
  });
}

const customScene = z.strictObject({
  kind: z.literal(CUSTOM),
  narration,
  image: image.optional(),
  component: z
    .string()
    .regex(COMPONENT, "PascalCase name of up to 40 characters")
    .describe("Names src/custom/<component>.tsx."),
});

const chapter = z.strictObject({
  name: chars(LIMITS.chapterName),
  scenes: list(z.discriminatedUnion("kind", [...BODY_KINDS.map((kind) => scene(kind)), customScene]), LIMITS.scenes, "scenes"),
});

export const scriptSchema = z
  .strictObject({
    version: z.literal(1),
    title: chars(LIMITS.title),
    music: z
      .string()
      .regex(MUSIC_ID, `none or a track id: a-z or 0-9 first, then a-z, 0-9 or -, at most ${LIMITS.musicId} characters`)
      .describe('"none", or the id of a track in the music library.'),
    hook: scene("hook"),
    chapters: list(chapter, LIMITS.chapters, "chapters"),
    summary: scene("summary"),
    endCard: scene("end-card", list(sentence, LIMITS.endCardSentences, "sentences").optional()),
    voice: z
      .strictObject({
        id: z
          .string()
          .max(LIMITS.voice.id, `${LIMITS.voice.id} characters`)
          .regex(VOICE_ID, "letters, digits, . _ or -")
          .optional()
          .describe("generate_speech voice; empty or absent means the server's default voice."),
        instructions: z
          .string()
          .max(LIMITS.voice.instructions, `${LIMITS.voice.instructions} characters`)
          .optional()
          .describe("generate_speech instructions, the same for every block."),
      })
      .optional(),
  })
  .describe(
    "Explainer video script, version 1: a hook, 3–5 chapters, a summary and an end card. " +
      "A script holds at most one `custom` scene; JSON Schema cannot express this rule, and validateScript enforces it.",
  );

export function scriptJsonSchema() {
  return z.toJSONSchema(scriptSchema);
}

// --- locating problems ------------------------------------------------------

const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

function valueAt(root, path) {
  let value = root;
  for (const key of path) {
    if (value === null || typeof value !== "object") return undefined;
    value = value[key];
  }
  return value;
}

function isMissing(root, path) {
  if (path.length === 0) return false;
  const parent = valueAt(root, path.slice(0, -1));
  return parent === null || typeof parent !== "object" || !Object.hasOwn(parent, path.at(-1));
}

export function fieldPath(path) {
  return path.map((key, i) => (typeof key === "number" ? `[${key}]` : i === 0 ? key : `.${key}`)).join("");
}

const ESCAPES = { "\n": "\\n", "\t": "\\t", "\r": "\\r" };

// Keeps text on one line: C0, DEL, C1, U+2028 and U+2029 written as
// escapes. Used alone for a JSON.parse message (V8 bounds it, and its
// positional form ends with the line and column) and for an ffmpeg reason.
export function escapeLine(text) {
  return text.replace(
    /[\u0000-\u001f\u007f-\u009f\u2028\u2029]/g,
    (c) => ESCAPES[c] ?? `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
}

// Bounds a string echoed into an error (a chapter name, an unknown key): at
// most LIMITS.echo code points plus …, then escaped. Reads no further than
// the code point after the limit, so a long string costs no more than a
// short one.
function echo(text) {
  let cut = "";
  let count = 0;
  for (const point of text) {
    if (count === LIMITS.echo) return `${escapeLine(cut)}…`;
    cut += point;
    count++;
  }
  return escapeLine(text);
}

function chapterLabel(root, index) {
  const name = valueAt(root, ["chapters", index, "name"]);
  return typeof name === "string" ? `${index + 1} ${echo(name)}` : `${index + 1}`;
}

function sceneWhere(root, chapterIndex, sceneIndex) {
  const kind = valueAt(root, ["chapters", chapterIndex, "scenes", sceneIndex, "kind"]);
  const shown = CHAPTER_KINDS.includes(kind) ? kind : "unknown kind";
  return `chapter ${chapterLabel(root, chapterIndex)}, scene ${sceneIndex + 1} (${shown})`;
}

// Names the scene at a 1-based playback index (hook 1, the chapter scenes,
// the summary, the end card) as the messages above do; index is the s<n> of
// the scene's voice stems. Takes a script returned by validateScript.
export function playbackWhere(script, index) {
  const chapterScenes = script.chapters.flatMap((chapterValue, c) => chapterValue.scenes.map((_, s) => sceneWhere(script, c, s)));
  const names = [SLOTS.hook, ...chapterScenes, SLOTS.summary, SLOTS.endCard];
  if (!Number.isInteger(index) || index < 1 || index > names.length) throw new RangeError(`no scene at playback index ${index}`);
  return names[index - 1];
}

// Splits a path into where it lies (chapter, scene, <where>) and the field
// relative to that place.
function locate(root, path) {
  const [top, index, key, sceneIndex, ...inner] = path;
  if (top === "chapters" && typeof index === "number") {
    const chapterName = chapterLabel(root, index);
    if (key === "scenes" && typeof sceneIndex === "number") {
      return {
        chapter: chapterName,
        scene: sceneIndex + 1,
        where: sceneWhere(root, index, sceneIndex),
        field: inner.length ? fieldPath(inner) : "scene",
      };
    }
    const rest = path.slice(2);
    return { chapter: chapterName, scene: null, where: `chapter ${chapterName}`, field: rest.length ? fieldPath(rest) : "chapter" };
  }
  if (Object.hasOwn(SLOTS, top) && path.length > 1) {
    return { chapter: null, scene: SLOTS[top], where: SLOTS[top], field: fieldPath(path.slice(1)) };
  }
  return { chapter: null, scene: null, where: "script", field: fieldPath(path) };
}

const TOO_BIG = { string: "is too long", array: "has too many entries", number: "is too large" };
const TOO_SMALL = { string: "is empty", array: "has too few entries", number: "is too small" };

// Returns [problem, limit] for one zod issue at a path that is not an
// unknown key.
function describe(issue, missing) {
  switch (issue.code) {
    case "invalid_type":
      return missing ? ["is missing", "required"] : ["has the wrong type", issue.expected];
    case "invalid_value":
      return missing ? ["is missing", "required"] : ["has an invalid value", issue.values.map(String).join(" | ")];
    case "invalid_union":
      return [missing ? "is missing" : "is not an allowed kind", issue.options.map(String).join(" | ")];
    case "too_big":
      return [TOO_BIG[issue.origin] ?? "is too large", issue.message];
    case "too_small":
      return [TOO_SMALL[issue.origin] ?? "is too small", issue.message];
    case "invalid_format":
      return ["does not match", issue.message];
    default:
      return ["is invalid", issue.message];
  }
}

function error({ chapter, scene, where, field }, problem, limit) {
  return { chapter, scene, field, limit, message: `${where}: ${field} ${problem} (limit ${limit})` };
}

function issueErrors(root, issue) {
  if (issue.code === "unrecognized_keys") {
    return issue.keys.map((key) => error(locate(root, [...issue.path, echo(key)]), "is not allowed", "unknown key"));
  }
  const [problem, limit] = describe(issue, isMissing(root, issue.path));
  return [error(locate(root, issue.path), problem, limit)];
}

// The one-custom rule, over the raw value so it reports alongside other
// errors; tolerates any malformed chapter or scene.
function customErrors(root) {
  if (!Array.isArray(root.chapters)) return [];
  const customs = [];
  root.chapters.forEach((chapterValue, c) => {
    if (!isObject(chapterValue) || !Array.isArray(chapterValue.scenes)) return;
    chapterValue.scenes.forEach((sceneValue, s) => {
      if (isObject(sceneValue) && sceneValue.kind === CUSTOM) customs.push([c, s]);
    });
  });
  if (customs.length <= LIMITS.customScenes) return [];
  const [first] = customs;
  const firstWhere = `chapter ${chapterLabel(root, first[0])}, scene ${first[1] + 1}`;
  return customs.slice(LIMITS.customScenes).map(([c, s]) =>
    error(
      locate(root, ["chapters", c, "scenes", s, "kind"]),
      `is an extra custom scene after ${firstWhere}`,
      `${LIMITS.customScenes} custom scene`,
    ),
  );
}

const fileError = (problem, limit) =>
  error({ chapter: null, scene: null, where: "script", field: "script.json" }, problem, limit);

const tooLarge = () => ({ ok: false, errors: [fileError("is too large to validate", "size")] });

// Checks the text of a script.json. Returns {ok: true, script} with the end
// card's narration always an array and voice always {id, instructions}, or
// {ok: false, errors}. TextEncoder, not Buffer, because the renderer imports
// this module inside the Remotion bundle.
export function validateScript(text) {
  if (typeof text !== "string") throw new TypeError("validateScript expects the text of script.json");
  if (new TextEncoder().encode(text).length > LIMITS.bytes) return tooLarge();
  if (text.trim() === "") return { ok: false, errors: [fileError("is empty", NON_EMPTY)] };
  let root;
  try {
    root = JSON.parse(text);
  } catch (parseError) {
    return { ok: false, errors: [fileError(`is not valid JSON (${escapeLine(parseError.message)})`, "JSON")] };
  }
  if (!isObject(root)) return { ok: false, errors: [fileError("is not a JSON object", "object")] };
  let parsed;
  let errors;
  try {
    parsed = scriptSchema.safeParse(root);
    errors = [...(parsed.success ? [] : parsed.error.issues.flatMap((issue) => issueErrors(root, issue))), ...customErrors(root)];
  } catch (thrown) {
    // zod overflows the stack above about 105,000 nested issues.
    if (thrown instanceof RangeError) return tooLarge();
    throw thrown;
  }
  if (errors.length) return { ok: false, errors };
  const script = parsed.data;
  script.endCard.narration ??= [];
  script.voice = { id: script.voice?.id ?? "", instructions: script.voice?.instructions ?? "" };
  return { ok: true, script };
}
