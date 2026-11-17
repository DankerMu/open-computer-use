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
  scriptJsonSchema,
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
  assert.deepEqual(result.script, expected);
});

test("a maximal script passes: 5×4 scenes, every body kind, one custom, 4×4 blocks of 16", () => {
  const script = maximal();
  const kinds = new Set(script.chapters.flatMap((chapter) => chapter.scenes.map((scene) => scene.kind)));
  assert.deepEqual([...kinds].sort(), [...CANDIDATE_KINDS, "custom"].sort());
  const result = validate(script);
  assert.equal(result.ok, true, JSON.stringify(result.errors));
  assert.deepEqual(result.script, script);
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
  rejects({ ...minimal(), music: "Calm Piano" }, [top("music", "none or a track id of a-z, 0-9 and -")]);
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
