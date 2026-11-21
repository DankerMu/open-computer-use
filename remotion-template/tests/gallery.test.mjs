// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The gallery's catalog rules, contentSchema, the bundle-side import lists
// and the CLI refusals that run before any bundle (task 4.1). Expected
// messages are the fixture's ten ordered rule texts; the content schema is
// compared with the scene schemas inside scriptJsonSchema().

import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  copyFileSync,
  cpSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  realpathSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { z } from "zod";
import { BODY_KINDS, contentSchema, scriptJsonSchema } from "../bin/script-schema.mjs";
import { candidates, validateCatalog } from "../src/kit/gallery/catalog.ts";

const TEMPLATE = fileURLToPath(new URL("..", import.meta.url));
const GALLERY = join(TEMPLATE, "bin", "gallery.mjs");
const OK_CATALOG = join(TEMPLATE, "tests", "render", "fixtures", "gallery", "ok", "catalog.ts");
const RULES = join(TEMPLATE, "src", "kit", "gallery", "catalog-rules.ts");
const USAGE = "kit gallery: usage: node bin/gallery.mjs --out <dir>\n";

// The fixture's ok catalog.ts imports ./catalog-rules.ts, which exists only
// once it is copied over src/kit/gallery/catalog.ts. Loaded here beside a
// shim that re-exports the real rules module.
async function okCatalog(t) {
  const dir = mkdtempSync(join(tmpdir(), "gallery-ok-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  copyFileSync(OK_CATALOG, join(dir, "catalog.ts"));
  writeFileSync(join(dir, "catalog-rules.ts"), `export * from ${JSON.stringify(pathToFileURL(RULES).href)};\n`);
  return import(pathToFileURL(join(dir, "catalog.ts")).href);
}

const timing = () => ({
  durationInFrames: 60,
  narrationEndFrame: 30,
  sentences: [{ blocks: [{ text: "为什么", startFrame: 0, endFrame: 12 }, { text: "会这样", startFrame: 14, endFrame: 30 }] }],
});

function candidate(overrides = {}) {
  return {
    id: "quiz",
    kind: "question",
    source: "written for the kit",
    minFrames: 60,
    entranceFrames: 10,
    samples: [{ id: "max", content: { question: "筷子为何看起来折断" }, image: null, timing: timing() }],
    ...overrides,
  };
}

function rejects(list, message) {
  assert.throws(() => validateCatalog(list), (error) => {
    assert.ok(error instanceof Error);
    assert.equal(error.message, `kit gallery: ${message}`);
    return true;
  });
}

const withSample = (change) => {
  const c = candidate();
  change(c.samples[0]);
  return [c];
};
const withTiming = (change) => withSample((s) => change(s.timing));

// --- validateCatalog ----------------------------------------------------------

test("the tracked catalog passes validateCatalog", () => {
  assert.equal(validateCatalog(candidates), undefined);
});

test("the ok fixture catalog passes: one placeholder question with max and overflow", async (t) => {
  const ok = await okCatalog(t);
  assert.equal(ok.validateCatalog(ok.candidates), undefined);
  assert.deepEqual(
    ok.candidates.map(({ id, kind, minFrames, entranceFrames, samples }) => [id, kind, minFrames, entranceFrames, samples.map((s) => s.id)]),
    [["placeholder", "question", 60, 10, ["max", "overflow"]]],
  );
  for (const sample of ok.candidates[0].samples) {
    assert.equal(Array.from(sample.content.question).length, 24, sample.id);
    assert.deepEqual(sample.timing.sentences.map((s) => s.blocks.map((b) => [b.startFrame, b.endFrame])), [[[0, 30]]]);
    assert.equal(sample.timing.durationInFrames, 60);
  }
});

test("a valid catalog of several candidates and kinds passes", () => {
  const hook = candidate({ id: "hook-a", kind: "hook", samples: [{ id: "s1", content: { title: "光的折射" }, image: "photo.png", timing: timing() }] });
  assert.equal(validateCatalog([candidate(), hook]), undefined);
});

test("rule 1: the catalog is an array", () => {
  rejects({}, "catalog: is not an array");
  rejects(null, "catalog: is not an array");
});

test("rule 2: an invalid, reserved or repeated id", () => {
  const problem = "id is invalid, reserved or repeated";
  rejects([candidate({ id: "Quiz" })], `Quiz: ${problem}`);
  rejects([candidate({ id: "a--b" })], `a--b: ${problem}`);
  rejects([candidate({ id: "-a" })], `-a: ${problem}`);
  rejects([candidate({ id: "a".repeat(41) })], `${"a".repeat(41)}: ${problem}`);
  rejects([candidate({ id: "theme" })], `theme: ${problem}`);
  rejects([candidate(), candidate()], `quiz: ${problem}`);
  rejects([candidate({ id: 7 })], `7: ${problem}`);
  assert.equal(validateCatalog([candidate({ id: "a".repeat(40) })]), undefined);
});

test("rule 3: the kind is a scene kind", () => {
  rejects([candidate({ kind: "custom" })], "quiz: kind is not a scene kind");
  rejects([candidate({ kind: "poster" })], "quiz: kind is not a scene kind");
});

test("rule 4: the source is one non-empty line", () => {
  rejects([candidate({ source: "" })], "quiz: source must be one non-empty line");
  rejects([candidate({ source: "  " })], "quiz: source must be one non-empty line");
  rejects([candidate({ source: "a\nb" })], "quiz: source must be one non-empty line");
  rejects([candidate({ source: null })], "quiz: source must be one non-empty line");
});

test("rule 5: integer frames with 0 ≤ entranceFrames < minFrames", () => {
  const problem = "quiz: needs integers 0 ≤ entranceFrames < minFrames";
  rejects([candidate({ entranceFrames: 60 })], problem);
  rejects([candidate({ entranceFrames: -1 })], problem);
  rejects([candidate({ minFrames: 60.5 })], problem);
  rejects([candidate({ entranceFrames: "10" })], problem);
});

test("rule 6: at least one sample", () => {
  rejects([candidate({ samples: [] })], "quiz: has no samples");
  rejects([candidate({ samples: null })], "quiz: has no samples");
});

test("rule 7: a sample id is valid and unique within its candidate", () => {
  const sample = candidate().samples[0];
  rejects([candidate({ samples: [{ ...sample, id: "a--b" }] })], "quiz/a--b: id is invalid or repeated");
  rejects([candidate({ samples: [{ ...sample, id: "a".repeat(21) }] })], `quiz/${"a".repeat(21)}: id is invalid or repeated`);
  rejects([candidate({ samples: [sample, { ...sample }] })], "quiz/max: id is invalid or repeated");
  assert.equal(validateCatalog([candidate(), candidate({ id: "other" })]), undefined);
});

test("rule 8: the content passes contentSchema(kind), naming the first issue", () => {
  rejects(withSample((s) => (s.content = { question: "光".repeat(25) })), "quiz/max: content: question: 24 characters");
  rejects(withSample((s) => (s.content = { question: "光", extra: 1 })), 'quiz/max: content: Unrecognized key: "extra"');
});

test("rule 9: the image is null or a plain image name", () => {
  rejects(withSample((s) => (s.image = "../x.png")), "quiz/max: image is invalid");
  rejects(withSample((s) => delete s.image), "quiz/max: image is invalid");
});

test("rule 10: the timing durations, sentences, block texts and frames", () => {
  rejects(withTiming((t) => (t.durationInFrames = 59)), "quiz/max: timing.durationInFrames must be an integer ≥ 60");
  rejects(withSample((s) => (s.timing = null)), "quiz/max: timing.durationInFrames must be an integer ≥ 60");
  rejects(withTiming((t) => (t.narrationEndFrame = 61)), "quiz/max: timing.narrationEndFrame is out of range");
  rejects(withTiming((t) => (t.narrationEndFrame = -1)), "quiz/max: timing.narrationEndFrame is out of range");
  const shape = "quiz/max: timing.sentences must hold 0–4 sentences of 1–4 blocks";
  rejects(withTiming((t) => (t.sentences = Array.from({ length: 5 }, () => ({ blocks: [{ text: "光", startFrame: 0, endFrame: 1 }] })))), shape);
  rejects(withTiming((t) => (t.sentences = [{ blocks: [] }])), shape);
  rejects(withTiming((t) => t.sentences[0].blocks.push(...Array.from({ length: 3 }, () => ({ text: "光", startFrame: 29, endFrame: 30 })))), shape);
  rejects(withTiming((t) => (t.sentences[0].blocks[1].text = "光".repeat(17))), "quiz/max: timing block 1.2 text must be 1–16 characters");
  rejects(withTiming((t) => (t.sentences[0].blocks[0].text = "")), "quiz/max: timing block 1.1 text must be 1–16 characters");
  const order = "quiz/max: timing block 1.2 frames are out of order or range";
  rejects(withTiming((t) => (t.sentences[0].blocks[1].startFrame = 11)), order);
  rejects(withTiming((t) => (t.sentences[0].blocks[1].endFrame = 31)), order);
  rejects(withTiming((t) => (t.sentences[0].blocks[1].endFrame = 14)), order);
  rejects(withTiming((t) => (t.sentences[0].blocks[0].startFrame = -1)), "quiz/max: timing block 1.1 frames are out of order or range");
  assert.equal(validateCatalog(withTiming((t) => (t.sentences = []))), undefined);
  assert.equal(validateCatalog(withTiming((t) => (t.sentences[0].blocks[1].startFrame = 12))), undefined);
});

// --- contentSchema ------------------------------------------------------------

function sceneContentSchemas() {
  const schema = scriptJsonSchema();
  const body = schema.properties.chapters.items.properties.scenes.items.oneOf;
  const byKind = new Map(body.map((scene) => [scene.properties.kind.const, scene.properties.content]));
  byKind.set("hook", schema.properties.hook.properties.content);
  byKind.set("summary", schema.properties.summary.properties.content);
  byKind.set("end-card", schema.properties.endCard.properties.content);
  byKind.delete("custom");
  return byKind;
}

test("contentSchema(kind) is the scene schema's content for every kind", () => {
  const scenes = sceneContentSchemas();
  assert.deepEqual([...scenes.keys()].sort(), [...BODY_KINDS, "hook", "summary", "end-card"].sort());
  for (const [kind, expected] of scenes) {
    const actual = z.toJSONSchema(contentSchema(kind));
    delete actual.$schema;
    assert.deepEqual(actual, expected, kind);
  }
  assert.equal(contentSchema("question").safeParse({ question: "光" }).success, true);
  assert.equal(contentSchema("question").safeParse({ question: "光", x: 1 }).success, false);
});

test("contentSchema refuses a kind without content", () => {
  assert.throws(() => contentSchema("custom"), { name: "RangeError", message: 'no content schema for kind "custom"' });
});

// --- bundle-side imports ------------------------------------------------------

// The specifiers of every static import, re-export and dynamic import.
function specifiers(path) {
  const source = readFileSync(join(TEMPLATE, path), "utf8");
  const found = [];
  for (const match of source.matchAll(/^\s*(?:import|export)\b[^;'"]*?\bfrom\s*["']([^"']+)["']/gm)) found.push(match[1]);
  for (const match of source.matchAll(/^\s*import\s*["']([^"']+)["']/gm)) found.push(match[1]);
  for (const match of source.matchAll(/\bimport\s*\(\s*["']([^"']+)["']/g)) found.push(match[1]);
  return found;
}

test("the gallery's bundle-side modules import no node: built-in", () => {
  const modules = [
    "src/kit/render/KitFrame.tsx",
    "src/kit/gallery/catalog.ts",
    "src/kit/gallery/catalog-rules.ts",
    "src/kit/gallery/components.tsx",
    "src/kit/gallery/ThemeSample.tsx",
    "src/kit/gallery/entry.tsx",
  ];
  for (const path of modules) {
    const found = specifiers(path);
    assert.ok(found.length > 0, path);
    assert.deepEqual(found.filter((s) => s.startsWith("node:")), [], path);
  }
  // Node imports the catalog through type stripping, so it reaches no TSX.
  for (const path of ["src/kit/gallery/catalog.ts", "src/kit/gallery/catalog-rules.ts"]) {
    for (const specifier of specifiers(path)) assert.match(specifier, /^\.{1,2}\/.*\.(mjs|ts)$/, `${path}: ${specifier}`);
  }
});

// --- the CLI before any bundle ------------------------------------------------

const runGallery = (args, cwd = TEMPLATE) => spawnSync(process.execPath, [GALLERY, ...args], { cwd, encoding: "utf8" });

test("any argument list but --out <dir> exits 2 with the usage line", () => {
  for (const args of [[], ["--out"], ["--out", ""], ["--out", "a", "b"], ["--dir", "a"], ["--out=a"], ["a", "--out"]]) {
    const result = runGallery(args);
    assert.equal(result.status, 2, JSON.stringify(args));
    assert.equal(result.stderr, USAGE, JSON.stringify(args));
    assert.equal(result.stdout, "", JSON.stringify(args));
  }
});

test("an existing --out is refused with exit 1, untouched, and no sibling appears", (t) => {
  const parent = mkdtempSync(join(tmpdir(), "gallery-out-"));
  t.after(() => rmSync(parent, { recursive: true, force: true }));
  const out = join(parent, "gallery");
  mkdirSync(out);
  writeFileSync(join(out, "keep.txt"), "keep");
  const result = runGallery(["--out", out]);
  assert.equal(result.status, 1);
  assert.equal(result.stderr, `kit gallery: ${out}: already exists\n`);
  assert.deepEqual(readdirSync(parent), ["gallery"]);
  assert.deepEqual(readdirSync(out), ["keep.txt"]);
  assert.equal(readFileSync(join(out, "keep.txt"), "utf8"), "keep");
  const file = join(parent, "file");
  writeFileSync(file, "x");
  const onFile = runGallery(["--out", file]);
  assert.equal(onFile.status, 1);
  assert.equal(onFile.stderr, `kit gallery: ${file}: already exists\n`);
});

test("a relative --out resolves from the working directory", (t) => {
  const parent = realpathSync(mkdtempSync(join(tmpdir(), "gallery-rel-")));
  t.after(() => rmSync(parent, { recursive: true, force: true }));
  mkdirSync(join(parent, "taken"));
  const result = runGallery(["--out", "taken"], parent);
  assert.equal(result.status, 1);
  assert.equal(result.stderr, `kit gallery: ${join(parent, "taken")}: already exists\n`);
});

test("an invalid catalog exits 1 with its rule's message before any bundle, leaving nothing", (t) => {
  const base = realpathSync(mkdtempSync(join(tmpdir(), "gallery-invalid-")));
  t.after(() => rmSync(base, { recursive: true, force: true }));
  const project = join(base, "project");
  for (const part of ["bin", "src"]) cpSync(join(TEMPLATE, part), join(project, part), { recursive: true });
  copyFileSync(join(TEMPLATE, "package.json"), join(project, "package.json"));
  symlinkSync(realpathSync(join(TEMPLATE, "node_modules")), join(project, "node_modules"));
  const sample = { id: "max", content: { question: "光" }, image: null, timing: timing() };
  const twin = { id: "dup", kind: "question", source: "written for the test", minFrames: 60, entranceFrames: 10, samples: [sample] };
  writeFileSync(
    join(project, "src", "kit", "gallery", "catalog.ts"),
    [
      "export {validateCatalog} from './catalog-rules.ts';",
      `export const candidates = ${JSON.stringify([twin, twin])};`,
      "",
    ].join("\n"),
  );
  const parent = join(base, "runs");
  mkdirSync(parent);
  const result = spawnSync(process.execPath, ["bin/gallery.mjs", "--out", join(parent, "gallery")], { cwd: project, encoding: "utf8" });
  assert.equal(result.status, 1, result.stderr);
  // Node's MODULE_TYPELESS_PACKAGE_JSON warning precedes the CLI's own line.
  const own = result.stderr.split("\n").filter((line) => line.startsWith("kit gallery: "));
  assert.deepEqual(own, ["kit gallery: dup: id is invalid, reserved or repeated"]);
  assert.equal(result.stdout, "");
  assert.deepEqual(readdirSync(parent), []);
});
