// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The version-1 script.json schema. Expected values come from the task 3.4
// fixture: the skeleton, the per-kind limits, the error-mapping table
// (chapter, scene, field, limit) and the message form
// `<where>: <field> <problem> (limit <limit>)`. Limits are literals here,
// not read from LIMITS, so the module is not checked against itself.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import {
  BODY_KINDS,
  IMAGE_NAME,
  LIMITS,
  VIDEO,
  playbackWhere,
  scriptJsonSchema,
  scriptSchema,
  validateScript,
} from "../bin/script-schema.mjs";

const SCHEMA_FILE = fileURLToPath(new URL("../script.schema.json", import.meta.url));
const CANDIDATE_KINDS = [
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
const ALLOWED_KINDS = [...CANDIDATE_KINDS, "custom"].join(" | ");
const IMAGE_LIMIT = "plain image name (.png, .jpg, .jpeg or .webp)";
const CONTROL_LIMIT = "no control characters";
const MUSIC_LIMIT = "none or a track id: a-z or 0-9 first, then a-z, 0-9 or -, at most 40 characters";
const MESSAGE = /^(script|hook|summary|end-card|chapter [^,]+(, scene \d+ \([^)]+\))?): \S+ .+ \(limit .+\)$/;

// n Han characters, each one code point and one UTF-16 unit.
function han(n) {
  return "一二三四五六七八九十".repeat(Math.ceil(n / 10)).slice(0, n);
}

function minimal() {
  const chapter = (name, question) => ({
    name,
    scenes: [{ kind: "question", narration: [["为什么筷子", "在水里会弯"]], content: { question } }],
  });
  return {
    version: 1,
    title: "光的折射",
    music: "none",
    hook: { kind: "hook", narration: [["插进水里的筷子"]], content: { title: "筷子为什么弯了" } },
    chapters: [chapter("现象", "筷子真的弯了吗"), chapter("原理", "光为什么会拐弯"), chapter("应用", "眼镜如何矫正")],
    summary: { kind: "summary", narration: [["光在介质间会拐弯"]], content: { points: ["折射改变光路"] } },
    endCard: { kind: "end-card", content: { title: "谢谢观看" } },
  };
}

function maxNarration() {
  return Array.from({ length: 4 }, () => Array.from({ length: 4 }, () => han(16)));
}

const IMAGE = `${"a".repeat(64)}.jpeg`;
const MAX_CONTENT = {
  question: { question: han(24) },
  "concept-card": { kicker: han(8), title: han(12), points: Array(4).fill(han(16)), conclusion: han(20) },
  "process-steps": { steps: Array(5).fill({ label: han(8), detail: han(16) }) },
  "cause-effect": { cause: han(16), effect: han(16), link: han(8) },
  comparison: {
    left: { title: han(8), points: Array(4).fill(han(12)) },
    right: { title: han(8), points: Array(4).fill(han(12)) },
  },
  timeline: { events: Array(6).fill({ year: han(8), label: han(12), detail: han(16) }) },
  "key-number": { value: 1.5e9, unit: han(4), label: han(12) },
  chart: { type: "line", title: han(12), unit: han(4), points: Array(8).fill({ label: han(6), value: -3.25 }) },
  "image-callouts": { callouts: Array(3).fill({ x: 1, y: 0, label: han(10) }) },
  "definition-quote": { term: han(10), text: han(40), source: han(16) },
};

function maximal() {
  const body = [...CANDIDATE_KINDS, "custom", ...CANDIDATE_KINDS.slice(0, 9)].map((kind) =>
    kind === "custom"
      ? { kind, narration: maxNarration(), image: IMAGE, component: `B${"a".repeat(39)}` }
      : { kind, narration: maxNarration(), image: IMAGE, content: structuredClone(MAX_CONTENT[kind]) },
  );
  return {
    version: 1,
    title: han(24),
    music: "calm-piano-02",
    hook: { kind: "hook", narration: maxNarration(), image: IMAGE, content: { title: han(16), subtitle: han(24) } },
    chapters: Array.from({ length: 5 }, (_, c) => ({ name: han(6), scenes: body.slice(c * 4, c * 4 + 4) })),
    summary: { kind: "summary", narration: maxNarration(), content: { points: Array(4).fill(han(16)) } },
    endCard: { kind: "end-card", narration: maxNarration(), content: { title: han(16), subtitle: han(24) } },
  };
}

function validate(script) {
  return validateScript(typeof script === "string" ? script : JSON.stringify(script));
}

// Every error carries the five keys and the message form; returns the
// located part (without message) for exact comparison.
function located(result) {
  assert.equal(result.ok, false, "expected the script to be rejected");
  for (const error of result.errors) {
    assert.deepEqual(Object.keys(error).sort(), ["chapter", "field", "limit", "message", "scene"]);
    assert.match(error.message, MESSAGE);
  }
  return result.errors.map(({ chapter, scene, field, limit }) => ({ chapter, scene, field, limit }));
}

function rejects(script, expected) {
  assert.deepEqual(located(validate(script)), expected);
}

function withScene(script, chapter, scene, edit) {
  edit(script.chapters[chapter].scenes[scene]);
  return script;
}

const top = (field, limit) => ({ chapter: null, scene: null, field, limit });

// --- valid fixtures -------------------------------------------------------

test("a minimal script passes and its end card narration comes back as []", () => {
  const result = validate(minimal());
  assert.equal(result.ok, true, JSON.stringify(result.errors));
  const expected = minimal();
  expected.endCard.narration = [];
  expected.voice = { id: "", instructions: "" };
  assert.deepEqual(result.script, expected);
});

test("a maximal script passes: 5×4 scenes, every body kind, one custom, 4×4 blocks of 16", () => {
  const script = maximal();
  const kinds = new Set(script.chapters.flatMap((chapter) => chapter.scenes.map((scene) => scene.kind)));
  assert.deepEqual([...kinds].sort(), [...CANDIDATE_KINDS, "custom"].sort());
  const result = validate(script);
  assert.equal(result.ok, true, JSON.stringify(result.errors));
  assert.deepEqual(result.script, { ...script, voice: { id: "", instructions: "" } });
});

test("VIDEO is 1920×1080 at 30 fps", () => {
  assert.deepEqual(VIDEO, { width: 1920, height: 1080, fps: 30 });
});

test("BODY_KINDS are the candidate kinds without custom", () => {
  assert.deepEqual(BODY_KINDS, CANDIDATE_KINDS);
});

test("IMAGE_NAME is the fixture's pattern and LIMITS holds the skeleton numbers", () => {
  assert.equal(IMAGE_NAME.source, String.raw`^(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.(png|jpg|jpeg|webp)$`);
  assert.equal(LIMITS.block, 16);
  assert.equal(LIMITS.chapterName, 6);
  assert.deepEqual(LIMITS.chapters, { min: 3, max: 5 });
  assert.deepEqual(LIMITS.scenes, { min: 1, max: 4 });
});

// --- parsing --------------------------------------------------------------

test("empty and whitespace-only text give one non-empty error", () => {
  for (const text of ["", "  \n\t"]) {
    const result = validate(text);
    assert.deepEqual(located(result), [top("script.json", "non-empty")]);
    assert.equal(result.errors[0].message, "script: script.json is empty (limit non-empty)");
  }
});

test("validateScript takes text, not a parsed value", () => {
  assert.throws(() => validateScript(minimal()), TypeError);
});

test("text that is not JSON gives one JSON error", () => {
  const result = validate('{"version": 1,');
  assert.deepEqual(located(result), [top("script.json", "JSON")]);
  assert.match(result.errors[0].message, /^script: script\.json is not valid JSON \(.+\) \(limit JSON\)$/);
});

// The parse message is escaped by echo's rule but not cut (#827 follow-up):
// V8's positional form ends `in JSON at position N (line L column C)`.
const parseMessage = (text) => {
  const result = validate(text);
  assert.deepEqual(located(result), [top("script.json", "JSON")]);
  return result.errors[0].message.match(/^script: script\.json is not valid JSON \((.+)\) \(limit JSON\)$/)[1];
};

test("a positional JSON parse message keeps its line and column", () => {
  const inner = parseMessage('{"version": 1,');
  assert.ok(inner.includes("(line 1 column 15)"), inner);
  assert.ok(!inner.includes("…"), inner);
});

test("a quoting JSON parse message is escaped onto one line, not cut", () => {
  const inner = parseMessage('{"a": [1,\n]}');
  assert.doesNotMatch(inner, /[\u0000-\u001f\u007f-\u009f\u2028\u2029]/);
  assert.match(inner, /\\n/);
  assert.ok(!inner.includes("…"), inner);
});

test("a root that is not an object gives one object error", () => {
  for (const text of ["[]", "null", "1", '"x"']) rejects(text, [top("script.json", "object")]);
});

// --- top level ------------------------------------------------------------

test("0 and 6 chapters fail", () => {
  for (const count of [0, 6]) {
    const script = minimal();
    script.chapters = Array.from({ length: count }, () => minimal().chapters[0]);
    rejects(script, [top("chapters", "3–5 chapters")]);
  }
  const script = minimal();
  script.chapters = [];
  assert.equal(validate(script).errors[0].message, "script: chapters has too few entries (limit 3–5 chapters)");
});

test("a missing hook, summary or end card fails", () => {
  for (const key of ["hook", "summary", "endCard"]) {
    const script = minimal();
    delete script[key];
    const result = validate(script);
    assert.deepEqual(located(result), [top(key, "required")]);
    assert.equal(result.errors[0].message, `script: ${key} is missing (limit required)`);
  }
});

test("top-level fields: version, title, music", () => {
  rejects({ ...minimal(), version: 2 }, [top("version", "1")]);
  rejects({ ...minimal(), title: han(25) }, [top("title", "24 characters")]);
  rejects({ ...minimal(), title: "" }, [top("title", "non-empty")]);
  rejects({ ...minimal(), title: 7 }, [top("title", "string")]);
  rejects({ ...minimal(), music: "Calm Piano" }, [top("music", MUSIC_LIMIT)]);
  const missing = minimal();
  delete missing.music;
  rejects(missing, [top("music", "required")]);
});

test("unknown keys fail with limit unknown key, one error per key", () => {
  rejects({ ...minimal(), extra: 1 }, [top("extra", "unknown key")]);
  const scene = withScene(minimal(), 0, 0, (s) => Object.assign(s, { foo: 1, bar: 2 }));
  const where = { chapter: "1 现象", scene: 1, limit: "unknown key" };
  rejects(scene, [{ ...where, field: "foo" }, { ...where, field: "bar" }]);
  const content = withScene(minimal(), 1, 0, (s) => (s.content.extra = "x"));
  rejects(content, [{ chapter: "2 原理", scene: 1, field: "content.extra", limit: "unknown key" }]);
  const chapter = minimal();
  chapter.chapters[2].note = "x";
  rejects(chapter, [{ chapter: "3 应用", scene: null, field: "note", limit: "unknown key" }]);
  const hook = minimal();
  hook.hook.extra = 1;
  rejects(hook, [{ chapter: null, scene: "hook", field: "extra", limit: "unknown key" }]);
});

// --- chapters -------------------------------------------------------------

test("a chapter with no scene fails", () => {
  const script = minimal();
  script.chapters[0].scenes = [];
  const result = validate(script);
  assert.deepEqual(located(result), [{ chapter: "1 现象", scene: null, field: "scenes", limit: "1–4 scenes" }]);
  assert.equal(result.errors[0].message, "chapter 1 现象: scenes has too few entries (limit 1–4 scenes)");
});

test("a 7-character chapter name fails", () => {
  const script = minimal();
  script.chapters[1].name = han(7);
  rejects(script, [{ chapter: `2 ${han(7)}`, scene: null, field: "name", limit: "6 characters" }]);
});

test("a chapter without a string name is labelled by its number", () => {
  const script = minimal();
  delete script.chapters[1].name;
  script.chapters[1].scenes[0].narration = [[]];
  rejects(script, [
    { chapter: "2", scene: null, field: "name", limit: "required" },
    { chapter: "2", scene: 1, field: "narration[0]", limit: "1–4 blocks" },
  ]);
});

// --- scenes ---------------------------------------------------------------

test("a sentence with no block fails", () => {
  const script = withScene(minimal(), 0, 0, (s) => (s.narration = [["一句"], []]));
  rejects(script, [{ chapter: "1 现象", scene: 1, field: "narration[1]", limit: "1–4 blocks" }]);
});

test("an unknown kind and a missing kind fail with the allowed kinds", () => {
  const unknown = validate(withScene(minimal(), 1, 0, (s) => (s.kind = "pie-chart")));
  assert.deepEqual(located(unknown), [{ chapter: "2 原理", scene: 1, field: "kind", limit: ALLOWED_KINDS }]);
  assert.equal(
    unknown.errors[0].message,
    `chapter 2 原理, scene 1 (unknown kind): kind is not an allowed kind (limit ${ALLOWED_KINDS})`,
  );
  const missing = validate(withScene(minimal(), 1, 0, (s) => delete s.kind));
  assert.deepEqual(located(missing), [{ chapter: "2 原理", scene: 1, field: "kind", limit: ALLOWED_KINDS }]);
  assert.equal(
    missing.errors[0].message,
    `chapter 2 原理, scene 1 (unknown kind): kind is missing (limit ${ALLOWED_KINDS})`,
  );
});

test("a missing content field fails", () => {
  const where = { chapter: "1 现象", scene: 1, limit: "required" };
  rejects(withScene(minimal(), 0, 0, (s) => delete s.content), [{ ...where, field: "content" }]);
  rejects(withScene(minimal(), 0, 0, (s) => delete s.content.question), [{ ...where, field: "content.question" }]);
});

test("a 17-character block fails, naming chapter, scene, field and limit", () => {
  const script = withScene(minimal(), 1, 0, (s) => (s.narration[0][1] = han(17)));
  const result = validate(script);
  assert.deepEqual(located(result), [
    { chapter: "2 原理", scene: 1, field: "narration[0][1]", limit: "16 characters" },
  ]);
  assert.equal(
    result.errors[0].message,
    "chapter 2 原理, scene 1 (question): narration[0][1] is too long (limit 16 characters)",
  );
});

test("a nested content limit is located by its path inside the scene", () => {
  const script = maximal();
  script.chapters[1].scenes[0].content.left.points[1] = han(13);
  const result = validate(script);
  assert.deepEqual(located(result), [
    { chapter: `2 ${han(6)}`, scene: 1, field: "content.left.points[1]", limit: "12 characters" },
  ]);
  assert.match(result.errors[0].message, /^chapter 2 一二三四五六, scene 1 \(comparison\): /);
});

test("a body scene without narration fails, both missing and []", () => {
  const where = { chapter: "1 现象", scene: 1, field: "narration" };
  rejects(withScene(minimal(), 0, 0, (s) => delete s.narration), [{ ...where, limit: "required" }]);
  rejects(withScene(minimal(), 0, 0, (s) => (s.narration = [])), [{ ...where, limit: "1–4 sentences" }]);
});

test("image names with a slash, a backslash or .. fail", () => {
  for (const image of ["../x.png", "a/b.png", "a..b.png", "a\\b.png", "x.gif", ".x.png"]) {
    rejects(
      withScene(minimal(), 2, 0, (s) => (s.image = image)),
      [{ chapter: "3 应用", scene: 1, field: "image", limit: IMAGE_LIMIT }],
    );
  }
  assert.equal(validate(withScene(minimal(), 2, 0, (s) => (s.image = "fig_1-a.v2.webp"))).ok, true);
});

test("two custom scenes and a type error elsewhere give both errors", () => {
  const script = minimal();
  script.chapters[0].scenes[0] = { kind: "custom", narration: [["自定义"]], component: "Prism" };
  script.chapters[2].scenes.push({ kind: "custom", narration: [["又一个"]], component: "Lens" });
  script.chapters[1].scenes[0].content.question = 42;
  const result = validate(script);
  assert.deepEqual(located(result), [
    { chapter: "2 原理", scene: 1, field: "content.question", limit: "string" },
    { chapter: "3 应用", scene: 2, field: "kind", limit: "1 custom scene" },
  ]);
  assert.equal(
    result.errors[1].message,
    "chapter 3 应用, scene 2 (custom): kind is an extra custom scene after chapter 1 现象, scene 1 (limit 1 custom scene)",
  );
});

test("three custom scenes give two custom errors", () => {
  const script = minimal();
  for (const chapter of script.chapters) {
    chapter.scenes.push({ kind: "custom", narration: [["自定义"]], component: "Prism" });
  }
  const result = validate(script);
  assert.deepEqual(located(result), [
    { chapter: "2 原理", scene: 2, field: "kind", limit: "1 custom scene" },
    { chapter: "3 应用", scene: 2, field: "kind", limit: "1 custom scene" },
  ]);
  for (const error of result.errors) assert.match(error.message, /after chapter 1 现象, scene 2 /);
});

test("the custom count tolerates malformed chapters", () => {
  const script = minimal();
  script.chapters = [null, { scenes: "x" }, { name: 1, scenes: [null, { kind: "custom" }] }, ...script.chapters];
  const result = validate(script);
  assert.equal(result.ok, false);
  located(result);
});

test("a scene or chapter that is not an object is named as a whole", () => {
  const script = minimal();
  script.chapters[0].scenes[0] = "question";
  script.chapters[1] = 5;
  rejects(script, [
    { chapter: "1 现象", scene: 1, field: "scene", limit: "object" },
    { chapter: "2", scene: null, field: "chapter", limit: "object" },
  ]);
});

// --- edge cases -----------------------------------------------------------

test("a hook without narration fails", () => {
  const script = minimal();
  delete script.hook.narration;
  const result = validate(script);
  assert.deepEqual(located(result), [{ chapter: null, scene: "hook", field: "narration", limit: "required" }]);
  assert.equal(result.errors[0].message, "hook: narration is missing (limit required)");
});

test("summary and end-card errors are located by their slot", () => {
  const script = minimal();
  script.summary.content.points = [];
  script.endCard.narration = Array(5).fill(["一"]);
  script.endCard.content.title = han(17);
  const result = validate(script);
  assert.deepEqual(located(result), [
    { chapter: null, scene: "summary", field: "content.points", limit: "1–4 points" },
    { chapter: null, scene: "end-card", field: "narration", limit: "0–4 sentences" },
    { chapter: null, scene: "end-card", field: "content.title", limit: "16 characters" },
  ]);
  assert.equal(result.errors[2].message, "end-card: content.title is too long (limit 16 characters)");
});

test("an end card with an empty narration passes", () => {
  const script = minimal();
  script.endCard.narration = [];
  assert.equal(validate(script).ok, true);
});

test("block length counts code points, not UTF-16 units", () => {
  const sixteen = `${han(15)}😀`;
  assert.equal(sixteen.length, 17);
  assert.equal(validate(withScene(minimal(), 0, 0, (s) => (s.narration[0][0] = sixteen))).ok, true);
  rejects(
    withScene(minimal(), 0, 0, (s) => (s.narration[0][0] = `${han(16)}😀`)),
    [{ chapter: "1 现象", scene: 1, field: "narration[0][0]", limit: "16 characters" }],
  );
});

test("an image-callouts scene without image fails", () => {
  const script = withScene(minimal(), 0, 0, (s) => {
    s.kind = "image-callouts";
    s.content = { callouts: [{ x: 0.5, y: 1.5, label: "焦点" }] };
  });
  rejects(script, [
    { chapter: "1 现象", scene: 1, field: "image", limit: "required" },
    { chapter: "1 现象", scene: 1, field: "content.callouts[0].y", limit: "0–1" },
  ]);
});

test("content limits by kind", () => {
  const at = (field, limit) => ({ chapter: "1 现象", scene: 1, field, limit });
  const kind = (k, content) => withScene(minimal(), 0, 0, (s) => Object.assign(s, { kind: k, content }));
  rejects(kind("process-steps", { steps: [{ label: "一" }] }), [at("content.steps", "2–5 steps")]);
  rejects(kind("chart", { type: "pie", title: "图", points: [{ label: "a", value: 1 }, { label: "b", value: "2" }] }), [
    at("content.type", "bar | line"),
    at("content.points[1].value", "number"),
  ]);
  rejects(kind("timeline", { events: Array(7).fill({ year: "1", label: "一" }) }), [at("content.events", "2–6 events")]);
  rejects(kind("key-number", { value: 3, unit: han(5), label: "一" }), [at("content.unit", "4 characters")]);
  rejects(kind("cause-effect", { cause: "一", effect: "二", link: han(9) }), [at("content.link", "8 characters")]);
  rejects(kind("definition-quote", { term: "一", text: han(41) }), [at("content.text", "40 characters")]);
  rejects(kind("concept-card", { title: "一", points: Array(5).fill("一") }), [at("content.points", "1–4 points")]);
  rejects(kind("image-callouts", { callouts: [] }), [at("image", "required"), at("content.callouts", "1–3 callouts")]);
  const custom = withScene(minimal(), 0, 0, (s) => {
    delete s.content;
    Object.assign(s, { kind: "custom", component: "lens" });
  });
  rejects(custom, [at("component", "PascalCase name of up to 40 characters")]);
});

// --- schema file ----------------------------------------------------------

test("the committed script.schema.json equals scriptJsonSchema()", () => {
  const expected = `${JSON.stringify(scriptJsonSchema(), null, 2)}\n`;
  assert.equal(
    readFileSync(SCHEMA_FILE, "utf8"),
    expected,
    "regenerate: node -e 'import(\"./bin/script-schema.mjs\").then((m) => process.stdout.write(JSON.stringify(m.scriptJsonSchema(), null, 2) + \"\\n\"))' > script.schema.json",
  );
});

test("the JSON Schema states the skeleton, the block limit and the custom rule", () => {
  const schema = JSON.parse(readFileSync(SCHEMA_FILE, "utf8"));
  assert.deepEqual(
    [...schema.required].sort(),
    ["chapters", "endCard", "hook", "music", "summary", "title", "version"],
  );
  assert.equal(schema.properties.hook.properties.narration.items.items.maxLength, 16);
  assert.equal(schema.properties.hook.properties.narration.items.items.minLength, 1);
  assert.ok(schema.properties.endCard.required.includes("content"));
  assert.ok(!schema.properties.endCard.required.includes("narration"));
  assert.match(schema.description, /at most one `custom` scene/);
});

// --- voice (task 3.5) -----------------------------------------------------

test("voice is optional, and a missing id or instructions comes back as an empty string", () => {
  const cases = [
    [undefined, { id: "", instructions: "" }],
    [{}, { id: "", instructions: "" }],
    [{ id: "alloy" }, { id: "alloy", instructions: "" }],
    [{ instructions: "语速放慢" }, { id: "", instructions: "语速放慢" }],
    [{ id: `v_1.${"x".repeat(60)}`, instructions: "慢".repeat(200) }, { id: `v_1.${"x".repeat(60)}`, instructions: "慢".repeat(200) }],
  ];
  for (const [voice, expected] of cases) {
    const script = minimal();
    if (voice !== undefined) script.voice = voice;
    const result = validate(script);
    assert.equal(result.ok, true, JSON.stringify(result.errors));
    assert.deepEqual(result.script.voice, expected);
    assert.deepEqual(Object.keys(result.script.voice), ["id", "instructions"]);
  }
});

test("an invalid voice fails at the top level", () => {
  rejects({ ...minimal(), voice: { id: "a".repeat(65) } }, [top("voice.id", "64 characters")]);
  rejects({ ...minimal(), voice: { id: "alloy voice" } }, [top("voice.id", "letters, digits, . _ or -")]);
  rejects({ ...minimal(), voice: { instructions: "慢".repeat(201) } }, [top("voice.instructions", "200 characters")]);
  rejects({ ...minimal(), voice: { speed: 1 } }, [top("voice.speed", "unknown key")]);
  const result = validate({ ...minimal(), voice: { id: "a".repeat(65) } });
  assert.equal(result.errors[0].message, "script: voice.id is too long (limit 64 characters)");
});

// --- music id (#826 finding) ----------------------------------------------

test("music is none or a track id of at most 40 characters that starts with a letter or digit", () => {
  for (const music of ["-x", "a".repeat(41), "Calm", "a_b", ""]) rejects({ ...minimal(), music }, [top("music", MUSIC_LIMIT)]);
  for (const music of ["none", "0", "a".repeat(40), "calm-piano-02", "x-"]) {
    assert.equal(validate({ ...minimal(), music }).ok, true, music);
  }
});

// --- size (#826 finding) --------------------------------------------------

const SIZE = top("script.json", "size");
const SIZE_MESSAGE = "script: script.json is too large to validate (limit size)";

test("a text over 1 MiB in UTF-8 is refused before parsing", () => {
  const big = JSON.stringify({ ...minimal(), title: "x".repeat(2 * 1024 * 1024) });
  const result = validate(big);
  assert.deepEqual(located(result), [SIZE]);
  assert.equal(result.errors[0].message, SIZE_MESSAGE);
  // 350,000 Han characters are under 1 MiB in UTF-16 units but over it in UTF-8.
  const han3 = `{"x": "${"水".repeat(350000)}"}`;
  assert.ok(han3.length < 1024 * 1024);
  assert.deepEqual(located(validate(han3)), [SIZE]);
  // Exactly 1 MiB (the script plus trailing spaces) is still validated; one byte more is not.
  const json = JSON.stringify(minimal());
  const exact = json + " ".repeat(1024 * 1024 - Buffer.byteLength(json));
  assert.equal(Buffer.byteLength(exact), 1024 * 1024);
  assert.equal(validate(exact).ok, true);
  assert.deepEqual(located(validate(`${exact} `)), [SIZE]);
});

test("a RangeError inside validation becomes one size error", () => {
  const script = minimal();
  script.hook.narration = [Array(500000).fill(1)];
  const text = JSON.stringify(script);
  assert.ok(Buffer.byteLength(text) < 1024 * 1024);
  const result = validate(text);
  assert.deepEqual(located(result), [SIZE]);
  assert.equal(result.errors[0].message, SIZE_MESSAGE);
});

test("only a RangeError is caught; any other exception propagates", (t) => {
  t.mock.method(scriptSchema, "safeParse", () => {
    throw new RangeError("Maximum call stack size exceeded");
  });
  assert.deepEqual(located(validate(minimal())), [SIZE]);
  t.mock.restoreAll();
  t.mock.method(scriptSchema, "safeParse", () => {
    throw new TypeError("not a size problem");
  });
  assert.throws(() => validate(minimal()), { name: "TypeError", message: "not a size problem" });
});

// --- echo bounds (#826 finding) -------------------------------------------

test("a chapter name over 40 code points is cut with …", () => {
  const script = minimal();
  script.chapters[0].name = han(100);
  const result = validate(script);
  const label = `1 ${han(40)}…`;
  assert.deepEqual(located(result), [{ chapter: label, scene: null, field: "name", limit: "6 characters" }]);
  assert.equal(result.errors[0].message, `chapter ${label}: name is too long (limit 6 characters)`);
  const emoji = minimal();
  emoji.chapters[0].name = "😀".repeat(41);
  assert.equal(validate(emoji).errors[0].chapter, `1 ${"😀".repeat(40)}…`);
  const forty = minimal();
  forty.chapters[0].name = han(40);
  assert.equal(validate(forty).errors[0].chapter, `1 ${han(40)}`);
});

test("control characters in a chapter name are escaped, after the cut", () => {
  const script = minimal();
  script.chapters[1].name = "a\tb\nc\rd\u0000e\u001ff\u007fg\u009fh i j\"k\\l";
  script.chapters[1].scenes[0].narration = [[]];
  const label = '2 a\\tb\\nc\\rd\\u0000e\\u001ff\\u007fg\\u009fh\\u2028i\\u2029j"k\\l';
  const result = validate(script);
  // The name now also fails the control-character rule (task 5.1).
  assert.deepEqual(located(result), [
    { chapter: label, scene: null, field: "name", limit: "6 characters" },
    { chapter: label, scene: null, field: "name", limit: CONTROL_LIMIT },
    { chapter: label, scene: 1, field: "narration[0]", limit: "1–4 blocks" },
  ]);
  assert.equal(result.errors[2].message, `chapter ${label}, scene 1 (question): narration[0] has too few entries (limit 1–4 blocks)`);
  const cut = minimal();
  cut.chapters[0].name = `${han(40)}\n`;
  assert.equal(validate(cut).errors[0].chapter, `1 ${han(40)}…`);
});

test("an unknown key is cut and escaped in field and message", () => {
  const key = "a\nb c\u0008\"\\";
  const result = validate({ ...minimal(), [key]: 1 });
  assert.deepEqual(located(result), [top('a\\nb\\u2028c\\u0008"\\', "unknown key")]);
  assert.equal(result.errors[0].message, 'script: a\\nb\\u2028c\\u0008"\\ is not allowed (limit unknown key)');
  const long = validate(withScene(minimal(), 0, 0, (s) => (s.content[`${"k".repeat(40)}\n${"z".repeat(10)}`] = 1)));
  assert.deepEqual(located(long), [{ chapter: "1 现象", scene: 1, field: `content.${"k".repeat(40)}…`, limit: "unknown key" }]);
});

test("echo stops at the 41st code point: a 450,000-character chapter name with 68,000 unknown keys validates in under 2 s", () => {
  const script = minimal();
  script.chapters[0].name = "a".repeat(450000);
  const digits = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ";
  const content = script.chapters[0].scenes[0].content;
  for (let i = 0; i < 68000; i++) content[digits[i % 62] + digits[Math.floor(i / 62) % 62] + digits[Math.floor(i / 3844)]] = 0;
  const text = JSON.stringify(script);
  assert.ok(Buffer.byteLength(text) < 1024 * 1024, `${Buffer.byteLength(text)} bytes`);
  const started = performance.now();
  const result = validate(text);
  const elapsed = performance.now() - started;
  assert.equal(result.ok, false);
  assert.equal(result.errors.length, 68001);
  const label = `1 ${"a".repeat(40)}…`;
  for (const error of result.errors) assert.equal(error.chapter, label);
  assert.ok(elapsed < 2000, `${Math.round(elapsed)} ms`);
});

// --- playbackWhere ------------------------------------------------------------

test("playbackWhere names a 1-based playback scene as 3.4's messages do", () => {
  const raw = minimal();
  raw.chapters[1].scenes.push(
    { kind: "question", narration: [["光会拐弯"]], content: { question: "为什么" } },
    { kind: "comparison", narration: [["光会拐弯"]], content: { left: { title: "空气", points: ["快"] }, right: { title: "水", points: ["慢"] } } },
  );
  const result = validate(raw);
  assert.equal(result.ok, true, JSON.stringify(result.errors));
  const names = Array.from({ length: 8 }, (_, i) => playbackWhere(result.script, i + 1));
  assert.deepEqual(names, [
    "hook",
    "chapter 1 现象, scene 1 (question)",
    "chapter 2 原理, scene 1 (question)",
    "chapter 2 原理, scene 2 (question)",
    "chapter 2 原理, scene 3 (comparison)",
    "chapter 3 应用, scene 1 (question)",
    "summary",
    "end-card",
  ]);
  for (const index of [0, 9, 1.5]) assert.throws(() => playbackWhere(result.script, index), RangeError);
});

// --- nested strict objects and per-kind limits (#826 finding) ---------------

// The first scene of each kind in maximal(): body index i sits in chapter
// floor(i / 4) + 1, scene i % 4 + 1.
function inMaximal(kind, edit) {
  const script = maximal();
  const slot = { hook: "hook", summary: "summary", "end-card": "endCard" }[kind];
  if (slot) {
    edit(script[slot]);
    return { script, where: { chapter: null, scene: kind } };
  }
  for (const [c, chapter] of script.chapters.entries()) {
    const s = chapter.scenes.findIndex((scene) => scene.kind === kind);
    if (s >= 0) {
      edit(chapter.scenes[s]);
      return { script, where: { chapter: `${c + 1} ${han(6)}`, scene: s + 1 } };
    }
  }
  throw new Error(`maximal() has no ${kind} scene`);
}

function rejectsIn(kind, edit, field, limit) {
  const { script, where } = inMaximal(kind, edit);
  rejects(script, [{ ...where, field, limit }]);
}

const extra = (value) => ({ ...value, extra: 1 });

test("every nested strict object refuses an unknown key", () => {
  rejectsIn("comparison", (s) => (s.content.left = extra(s.content.left)), "content.left.extra", "unknown key");
  rejectsIn("process-steps", (s) => (s.content.steps[0] = extra(s.content.steps[0])), "content.steps[0].extra", "unknown key");
  rejectsIn("timeline", (s) => (s.content.events[1] = extra(s.content.events[1])), "content.events[1].extra", "unknown key");
  rejectsIn("chart", (s) => (s.content.points[0] = extra(s.content.points[0])), "content.points[0].extra", "unknown key");
  rejectsIn("image-callouts", (s) => (s.content.callouts[2] = extra(s.content.callouts[2])), "content.callouts[2].extra", "unknown key");
  rejectsIn("custom", (s) => (s.extra = 1), "extra", "unknown key");
});

const more = (list, n = 1) => [...list, ...Array(n).fill(list[0])];
const setAt = (list, index, patch) => list.map((item, i) => (i === index ? { ...item, ...patch } : item));

test("every per-kind content field refuses its limit plus one", () => {
  const cases = [
    ["hook", (s) => (s.content.title = han(17)), "content.title", "16 characters"],
    ["hook", (s) => (s.content.subtitle = han(25)), "content.subtitle", "24 characters"],
    ["question", (s) => (s.content.question = han(25)), "content.question", "24 characters"],
    ["concept-card", (s) => (s.content.kicker = han(9)), "content.kicker", "8 characters"],
    ["concept-card", (s) => (s.content.title = han(13)), "content.title", "12 characters"],
    ["concept-card", (s) => (s.content.points = more(s.content.points)), "content.points", "1–4 points"],
    ["concept-card", (s) => (s.content.points[3] = han(17)), "content.points[3]", "16 characters"],
    ["concept-card", (s) => (s.content.conclusion = han(21)), "content.conclusion", "20 characters"],
    ["process-steps", (s) => (s.content.steps = more(s.content.steps)), "content.steps", "2–5 steps"],
    ["process-steps", (s) => (s.content.steps = setAt(s.content.steps, 0, { label: han(9) })), "content.steps[0].label", "8 characters"],
    ["process-steps", (s) => (s.content.steps = setAt(s.content.steps, 4, { detail: han(17) })), "content.steps[4].detail", "16 characters"],
    ["cause-effect", (s) => (s.content.cause = han(17)), "content.cause", "16 characters"],
    ["cause-effect", (s) => (s.content.effect = han(17)), "content.effect", "16 characters"],
    ["cause-effect", (s) => (s.content.link = han(9)), "content.link", "8 characters"],
    ["comparison", (s) => (s.content.left = { ...s.content.left, title: han(9) }), "content.left.title", "8 characters"],
    ["comparison", (s) => (s.content.right = { ...s.content.right, points: more(s.content.right.points) }), "content.right.points", "1–4 points"],
    ["comparison", (s) => (s.content.right = { ...s.content.right, points: [han(13)] }), "content.right.points[0]", "12 characters"],
    ["timeline", (s) => (s.content.events = more(s.content.events)), "content.events", "2–6 events"],
    ["timeline", (s) => (s.content.events = setAt(s.content.events, 0, { year: han(9) })), "content.events[0].year", "8 characters"],
    ["timeline", (s) => (s.content.events = setAt(s.content.events, 1, { label: han(13) })), "content.events[1].label", "12 characters"],
    ["timeline", (s) => (s.content.events = setAt(s.content.events, 5, { detail: han(17) })), "content.events[5].detail", "16 characters"],
    ["key-number", (s) => (s.content.unit = han(5)), "content.unit", "4 characters"],
    ["key-number", (s) => (s.content.label = han(13)), "content.label", "12 characters"],
    ["chart", (s) => (s.content.title = han(13)), "content.title", "12 characters"],
    ["chart", (s) => (s.content.unit = han(5)), "content.unit", "4 characters"],
    ["chart", (s) => (s.content.points = more(s.content.points)), "content.points", "2–8 points"],
    ["chart", (s) => (s.content.points = setAt(s.content.points, 7, { label: han(7) })), "content.points[7].label", "6 characters"],
    ["image-callouts", (s) => (s.content.callouts = more(s.content.callouts)), "content.callouts", "1–3 callouts"],
    ["image-callouts", (s) => (s.content.callouts = setAt(s.content.callouts, 0, { label: han(11) })), "content.callouts[0].label", "10 characters"],
    ["image-callouts", (s) => (s.content.callouts = setAt(s.content.callouts, 1, { x: 1.01 })), "content.callouts[1].x", "0–1"],
    ["definition-quote", (s) => (s.content.term = han(11)), "content.term", "10 characters"],
    ["definition-quote", (s) => (s.content.text = han(41)), "content.text", "40 characters"],
    ["definition-quote", (s) => (s.content.source = han(17)), "content.source", "16 characters"],
    ["custom", (s) => (s.component = `B${"a".repeat(40)}`), "component", "PascalCase name of up to 40 characters"],
    ["summary", (s) => (s.content.points = more(s.content.points)), "content.points", "1–4 points"],
    ["summary", (s) => (s.content.points[0] = han(17)), "content.points[0]", "16 characters"],
    ["end-card", (s) => (s.content.title = han(17)), "content.title", "16 characters"],
    ["end-card", (s) => (s.content.subtitle = han(25)), "content.subtitle", "24 characters"],
    ["question", (s) => (s.narration = more(s.narration)), "narration", "1–4 sentences"],
    ["question", (s) => (s.narration[0] = more(s.narration[0])), "narration[0]", "1–4 blocks"],
    ["end-card", (s) => (s.narration = more(s.narration)), "narration", "0–4 sentences"],
    ["question", (s) => (s.image = `${"a".repeat(65)}.png`), "image", IMAGE_LIMIT],
  ];
  for (const [kind, edit, field, limit] of cases) rejectsIn(kind, edit, field, limit);
});

// --- control characters (task 5.1) --------------------------------------------

// Every string leaf of a script with its path, skipping the fields limited
// by a pattern or an enum of their own (kind, chart type, image, component,
// music, voice id).
function textFields(value, path = []) {
  if (typeof value === "string") {
    return ["kind", "type", "image", "component", "music", "id"].includes(path.at(-1)) ? [] : [path];
  }
  if (value === null || typeof value !== "object") return [];
  return Object.entries(value).flatMap(([key, child]) => textFields(child, [...path, Array.isArray(value) ? Number(key) : key]));
}

// The location 3.4's mapping gives a path: the chapter and scene, and the
// field relative to its place.
function placeOf(path) {
  const [top, c, key, s, ...inner] = path;
  if (top === "chapters" && key === "scenes") return { chapter: `${c + 1} ${han(6)}`, scene: s + 1, field: inner };
  if (top === "chapters") return { chapter: `${c + 1} ${han(6)}`, scene: null, field: path.slice(2) };
  if (["hook", "summary", "endCard"].includes(top)) {
    return { chapter: null, scene: { hook: "hook", summary: "summary", endCard: "end-card" }[top], field: path.slice(1) };
  }
  return { chapter: null, scene: null, field: path };
}

const fieldText = (path) => path.map((key, i) => (typeof key === "number" ? `[${key}]` : i === 0 ? key : `.${key}`)).join("");

test("a control character in any text field, or in voice.instructions, is rejected with its own limit", () => {
  const base = { ...maximal(), voice: { id: "alloy", instructions: "语速放慢" } };
  const paths = textFields(base);
  // Every kind's content fields, the names, the blocks, the title and the
  // voice instructions are among them.
  assert.ok(paths.length > 200, `${paths.length}`);
  for (const wanted of [["title"], ["voice", "instructions"], ["chapters", 0, "name"], ["hook", "content", "subtitle"]]) {
    assert.ok(paths.some((p) => fieldText(p) === fieldText(wanted)), fieldText(wanted));
  }
  for (const path of paths) {
    for (const control of ["\u0000", "\n", "\t", "\u007f", "\u009f"]) {
      const script = structuredClone(base);
      let parent = script;
      for (const key of path.slice(0, -1)) parent = parent[key];
      parent[path.at(-1)] = `一${control}二`;
      const { chapter, scene, field } = placeOf(path);
      const result = validate(script);
      assert.deepEqual(located(result), [{ chapter, scene, field: fieldText(field), limit: CONTROL_LIMIT }], fieldText(path));
      assert.ok(result.errors[0].message.endsWith(`: ${fieldText(field)} does not match (limit ${CONTROL_LIMIT})`), result.errors[0].message);
    }
  }
});

test("text outside the control class, such as U+2028 and an emoji, passes", () => {
  const script = minimal();
  script.title = "一\u2028二😀";
  script.hook.narration[0][0] = "光\u00a0速";
  assert.equal(validate(script).ok, true);
});

test("the JSON Schema carries the control-character pattern on every text field", () => {
  const schema = JSON.parse(readFileSync(SCHEMA_FILE, "utf8"));
  assert.equal(schema.properties.title.pattern, "^\\P{Cc}*$");
  assert.equal(schema.properties.hook.properties.narration.items.items.pattern, "^\\P{Cc}*$");
  assert.equal(schema.properties.voice.properties.instructions.pattern, "^\\P{Cc}*$");
  assert.equal(schema.properties.voice.properties.id.pattern, "^[A-Za-z0-9._-]*$");
});
