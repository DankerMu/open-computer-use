// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// What the user approves is what ships (task 4.2). The registry takes each
// catalog kind from src/kit/gallery/components.tsx by import, with no copy
// and no wrapper, so it holds the function object the gallery rendered; and
// every catalog candidate carries an `approve` entry in the tracked
// gallery/approvals.json. registry.ts is read as text, because Node cannot
// import .tsx.
//
// The newest export binds (task 4.3): each later approvals entry replaces an
// earlier one, so for every decision in the newest export under
// gallery/exports/ (the greatest name in code-unit order; names start with
// the date), loadApprovals gives that decision.
//
// The approved set binds (task 4.6). A is the ids whose latest decision in
// gallery/approvals.json is `approve`. The schema's kinds, the registry's
// kinds, the committed JSON Schema's kinds and the catalog's ids each equal A,
// `custom` aside; the example script uses exactly the kinds of A whose JSON
// Schema scene object does not require `image`; hook, summary and end-card
// are approved. The theme is the user's pick, `light`, and the kit holds only
// that theme. A failure names each surface and each kind it has too many or
// lacks. The skill is a surface too (task 7.2): the kinds of the kind table
// in skills/public/remotion/explainer-kit.md, read under its section heading
// only, equal A, each listed once and `custom` not at all.

import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { loadApprovals } from "../bin/gallery-approvals.mjs";
import { LIMITS } from "../bin/script-schema.mjs";
import { candidates } from "../src/kit/gallery/catalog.ts";
import { THEME_NAMES } from "../src/kit/gallery/catalog-rules.ts";
import { KIT_THEME, themes } from "../src/kit/theme.ts";

const TEMPLATE = fileURLToPath(new URL("..", import.meta.url));
const KIT_REFERENCE = fileURLToPath(new URL("../../skills/public/remotion/explainer-kit.md", import.meta.url));
const KIND_HEADING = "## Scene kinds";
const REGISTRY = join(TEMPLATE, "src", "kit", "render", "registry.ts");
const EXPORTS = join(TEMPLATE, "gallery", "exports");
const JSON_SCHEMA = join(TEMPLATE, "script.schema.json");
const EXAMPLE = join(TEMPLATE, "script.json");
const CUSTOM = "custom";
const REQUIRED_KINDS = ["hook", "summary", "end-card"];

// The entries of the `registry` literal in registry.ts, one per kind.
function registryEntries() {
  const source = readFileSync(REGISTRY, "utf8");
  const literal = /^export const registry: Registry = \{([^}]*)\};$/m.exec(source);
  assert.ok(literal, "registry.ts has no `export const registry: Registry = {…};` literal");
  return literal[1]
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);
}

// The kind of a registry entry: its key, without quotes.
const entryKind = (entry) => entry.slice(0, entry.indexOf(":")).trim().replace(/^'(.*)'$/, "$1");

// Every scene object of the committed JSON Schema, found by walking it: an
// object whose properties.kind has a `const`, with the fields it requires.
function schemaScenes() {
  const scenes = [];
  const walk = (node) => {
    if (Array.isArray(node)) return node.forEach(walk);
    if (node === null || typeof node !== "object") return;
    const kind = node.properties?.kind;
    if (kind !== null && typeof kind === "object" && Object.hasOwn(kind, "const")) {
      scenes.push({ kind: kind.const, required: node.required ?? [] });
    }
    Object.values(node).forEach(walk);
  };
  walk(JSON.parse(readFileSync(JSON_SCHEMA, "utf8")));
  return scenes;
}

// The first cell of each row of the kind table: the rows under
// KIND_HEADING, up to the next heading, whose first cell is one code span.
function skillKinds() {
  const lines = readFileSync(KIT_REFERENCE, "utf8").split("\n");
  const start = lines.indexOf(KIND_HEADING);
  assert.ok(start !== -1, `explainer-kit.md has no ${KIND_HEADING} line`);
  const end = lines.findIndex((line, i) => i > start && line.startsWith("#"));
  const section = lines.slice(start + 1, end === -1 ? undefined : end);
  return section.map((line) => /^\| `([^`]+)` \|/.exec(line)).filter(Boolean).map((match) => match[1]);
}

// The ids whose latest decision is approve.
const approvedSet = () => [...loadApprovals(TEMPLATE).decisions].filter(([, decision]) => decision === "approve").map(([id]) => id);

// One line per kind a surface has but `expected` lacks (extra), and per kind
// of `expected` it lacks.
function differences(surface, actual, expected) {
  const have = new Set(actual);
  const want = new Set(expected);
  return [
    ...[...have].filter((kind) => !want.has(kind)).sort().map((kind) => `${surface} has extra ${kind}`),
    ...[...want].filter((kind) => !have.has(kind)).sort().map((kind) => `${surface} lacks ${kind}`),
  ];
}

// One line per kind a surface lists more than once.
const repeats = (surface, kinds) =>
  [...new Set(kinds.filter((kind, i) => kinds.indexOf(kind) !== i))].sort().map((kind) => `${surface} lists ${kind} more than once`);

test("registry.ts imports components from the gallery and lists custom plus each catalog kind as components[id], in catalog order", () => {
  const source = readFileSync(REGISTRY, "utf8");
  assert.match(source, /^import \{components\} from '\.\.\/gallery\/components\.tsx';$/m);
  assert.deepEqual(registryEntries(), ["custom: CustomScene", ...candidates.map(({ id, kind }) => `'${kind}': components['${id}']`)]);
});

test("every catalog candidate has an approve entry in gallery/approvals.json", () => {
  assert.ok(candidates.length > 0, "the catalog lists no candidate");
  const { decisions } = loadApprovals(TEMPLATE);
  assert.deepEqual(
    candidates.map(({ id }) => [id, decisions.get(id) ?? null]),
    candidates.map(({ id }) => [id, "approve"]),
  );
});

test("the newest export binds: loadApprovals gives each of its decisions", () => {
  const names = readdirSync(EXPORTS).sort();
  assert.ok(names.length > 0, "gallery/exports holds no export");
  const newest = JSON.parse(readFileSync(join(EXPORTS, names.at(-1)), "utf8"));
  assert.ok(newest.decisions.length > 0, `${names.at(-1)} holds no decision`);
  const { decisions } = loadApprovals(TEMPLATE);
  assert.deepEqual(
    newest.decisions.map(({ id }) => [id, decisions.get(id) ?? null]),
    newest.decisions.map(({ id, decision }) => [id, decision]),
    names.at(-1),
  );
});

// --- the approved set (task 4.6) ----------------------------------------------

test("the approved set: the schema's kinds, the registry's kinds, the JSON Schema's kinds and the catalog's ids each equal A", () => {
  const approved = approvedSet();
  assert.ok(approved.length > 0, "gallery/approvals.json approves nothing");
  const surfaces = [
    ["the schema (LIMITS.content)", Object.keys(LIMITS.content)],
    ["the registry", registryEntries().map(entryKind).filter((kind) => kind !== CUSTOM)],
    ["the JSON Schema", schemaScenes().map(({ kind }) => kind).filter((kind) => kind !== CUSTOM)],
    ["the catalog", candidates.map(({ id }) => id)],
  ];
  assert.deepEqual(
    surfaces.flatMap(([surface, kinds]) => [...differences(surface, kinds, approved), ...repeats(surface, kinds)]),
    [],
  );
});

test("the approved set: the kinds of explainer-kit.md's kind table equal A, each once, without custom", () => {
  const kinds = skillKinds();
  assert.deepEqual(
    [
      ...differences("explainer-kit.md's kind table", kinds, approvedSet()),
      ...repeats("explainer-kit.md's kind table", kinds),
      ...(kinds.includes(CUSTOM) ? [`explainer-kit.md's kind table lists ${CUSTOM}`] : []),
    ],
    [],
  );
});

test("the example script: its kinds are A without the kinds whose JSON Schema scene object requires image", () => {
  const withImage = new Set(schemaScenes().filter(({ required }) => required.includes("image")).map(({ kind }) => kind));
  const expected = approvedSet().filter((kind) => !withImage.has(kind));
  const script = JSON.parse(readFileSync(EXAMPLE, "utf8"));
  const kinds = [script.hook, ...script.chapters.flatMap((chapter) => chapter.scenes), script.summary, script.endCard].map(({ kind }) => kind);
  assert.deepEqual(differences("script.json", kinds, expected), []);
});

test("required kinds: hook, summary and end-card each have approve as their latest decision", () => {
  const { decisions } = loadApprovals(TEMPLATE);
  assert.deepEqual(
    REQUIRED_KINDS.filter((kind) => decisions.get(kind) !== "approve").map((kind) => `${kind} is ${decisions.get(kind) ?? "undecided"}, not approve`),
    [],
  );
});

// A temporary root holding one export with `theme` and approvals entries
// linking it: hook's approve, and with `pick` a theme entry.
function pickRoot(t, theme, pick) {
  const root = mkdtempSync(join(tmpdir(), "theme-pick-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  mkdirSync(join(root, "gallery", "exports"), { recursive: true });
  const file = "gallery/exports/pick.json";
  const body = `${JSON.stringify({ version: 1, page: "ab".repeat(32), reviewer: "R", date: "2026-11-22", decisions: [{ id: "hook", decision: "approve" }], theme }, null, 2)}\n`;
  writeFileSync(join(root, file), body);
  const link = { reviewer: "R", date: "2026-11-22", export: file, export_sha256: createHash("sha256").update(body).digest("hex") };
  const entries = [{ id: "hook", decision: "approve", ...link }, ...(pick ? [{ id: "theme", decision: theme, ...link }] : [])];
  writeFileSync(join(root, "gallery", "approvals.json"), `${JSON.stringify({ version: 1, entries }, null, 2)}\n`);
  return root;
}

test("the theme pick: approvals.json picks light, the kit holds only that theme, KIT_THEME is it, and approvals accept no other", (t) => {
  const { theme } = loadApprovals(TEMPLATE);
  assert.equal(theme, "light");
  assert.deepEqual(Object.keys(themes), [theme]);
  assert.equal(KIT_THEME, themes[theme]);
  assert.deepEqual(THEME_NAMES, Object.keys(themes));
  assert.throws(() => loadApprovals(pickRoot(t, "dark", false)), {
    message: "kit gallery: approvals: gallery/exports/pick.json: theme is invalid",
  });
  assert.equal(loadApprovals(pickRoot(t, "light", true)).theme, "light");
});
