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
//
// The gate is per preset (issue #962): the tests loop over the preset table
// of src/kit/presets.ts, one row of tests per preset. The explainer row holds
// the assertions above over the explainer view of loadApprovals. The report
// row, until its first approvals (task 4.2), asserts that nothing of the
// report ships: no report entry in approvals.json, no report theme, and no
// report-only kind in the schema, the registry or the JSON Schema.

import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { loadApprovals } from "../bin/gallery-approvals.mjs";
import { LIMITS } from "../bin/script-schema.mjs";
import { candidates, catalogs } from "../src/kit/gallery/catalog.ts";
import { THEME_NAMES } from "../src/kit/gallery/catalog-rules.ts";
import { PRESETS } from "../src/kit/presets.ts";
import { KIT_THEME, themeFor, themes } from "../src/kit/theme.ts";

const TEMPLATE = fileURLToPath(new URL("..", import.meta.url));
const KIT_REFERENCE = fileURLToPath(new URL("../../skills/public/remotion/explainer-kit.md", import.meta.url));
const KIND_HEADING = "## Scene kinds";
const REGISTRY = join(TEMPLATE, "src", "kit", "render", "registry.ts");
const EXPORTS = join(TEMPLATE, "gallery", "exports");
const JSON_SCHEMA = join(TEMPLATE, "script.schema.json");
const EXAMPLE = join(TEMPLATE, "script.json");
const APPROVALS = join(TEMPLATE, "gallery", "approvals.json");
const CUSTOM = "custom";

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

// The first cell of every body row of the kind table: the table rows under
// KIND_HEADING, up to the next heading, after its header and separator
// rows. A cell that is one code span gives its content, any other cell its
// text, so a row without a code span still counts.
function skillKinds() {
  const lines = readFileSync(KIT_REFERENCE, "utf8").split("\n");
  const start = lines.indexOf(KIND_HEADING);
  assert.ok(start !== -1, `explainer-kit.md has no ${KIND_HEADING} line`);
  const end = lines.findIndex((line, i) => i > start && line.startsWith("#"));
  const rows = lines.slice(start + 1, end === -1 ? undefined : end).filter((line) => line.startsWith("|"));
  assert.ok(rows.length >= 2 && /^\|(\s*:?-+:?\s*\|)+$/.test(rows[1]), `the table under ${KIND_HEADING} has no header and separator rows`);
  return rows.slice(2).map((row) => {
    const cell = row.split("|")[1].trim();
    return /^`[^`]+`$/.test(cell) ? cell.slice(1, -1) : cell;
  });
}

// The kinds whose latest decision in a preset's view is approve.
const approvedOf = (preset) =>
  [...loadApprovals(TEMPLATE).presets[preset].decisions].filter(([, decision]) => decision === "approve").map(([kind]) => kind);
const approvedSet = () => approvedOf("explainer");

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

// The explainer row: every assertion of tasks 4.2–4.6 and 7.2.
const EXPLAINER_ROW = [
  ["registry.ts imports components from the gallery and lists custom plus each catalog kind as components[id], in catalog order", () => {
    const source = readFileSync(REGISTRY, "utf8");
    assert.match(source, /^import \{components\} from '\.\.\/gallery\/components\.tsx';$/m);
    assert.deepEqual(registryEntries(), ["custom: CustomScene", ...candidates.map(({ id, kind }) => `'${kind}': components['${id}']`)]);
  }],

  ["every catalog candidate has an approve entry in gallery/approvals.json", () => {
    assert.ok(candidates.length > 0, "the catalog lists no candidate");
    const { decisions } = loadApprovals(TEMPLATE);
    assert.deepEqual(
      candidates.map(({ id }) => [id, decisions.get(id) ?? null]),
      candidates.map(({ id }) => [id, "approve"]),
    );
  }],

  ["the newest export binds: loadApprovals gives each of its decisions", () => {
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
  }],

  // the approved set (task 4.6)

  ["the approved set: the schema's kinds, the registry's kinds, the JSON Schema's kinds and the catalog's ids each equal A", () => {
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
  }],

  ["the approved set: the kinds of explainer-kit.md's kind table equal A, each once, without custom", () => {
    const kinds = skillKinds();
    assert.deepEqual(
      [
        ...differences("explainer-kit.md's kind table", kinds, approvedSet()),
        ...repeats("explainer-kit.md's kind table", kinds),
        ...(kinds.includes(CUSTOM) ? [`explainer-kit.md's kind table lists ${CUSTOM}`] : []),
      ],
      [],
    );
  }],

  ["the example script: its kinds are A without the kinds whose JSON Schema scene object requires image", () => {
    const withImage = new Set(schemaScenes().filter(({ required }) => required.includes("image")).map(({ kind }) => kind));
    const expected = approvedSet().filter((kind) => !withImage.has(kind));
    const script = JSON.parse(readFileSync(EXAMPLE, "utf8"));
    const kinds = [script.hook, ...script.chapters.flatMap((chapter) => chapter.scenes), script.summary, script.endCard].map(({ kind }) => kind);
    assert.deepEqual(differences("script.json", kinds, expected), []);
  }],

  ["required kinds: hook, summary and end-card each have approve as their latest decision", () => {
    const { decisions } = loadApprovals(TEMPLATE);
    assert.deepEqual(
      PRESETS.explainer.requiredKinds.filter((kind) => decisions.get(kind) !== "approve").map((kind) => `${kind} is ${decisions.get(kind) ?? "undecided"}, not approve`),
      [],
    );
  }],

  ["the theme pick: approvals.json picks light, the kit holds it as the explainer's one theme, KIT_THEME is it, and approvals accept no other", (t) => {
    const { theme } = loadApprovals(TEMPLATE);
    assert.equal(theme, "light");
    assert.ok(Object.hasOwn(themes, theme));
    assert.deepEqual(PRESETS.explainer.themeCandidates, [theme]);
    assert.equal(themeFor("explainer"), themes[theme]);
    assert.equal(KIT_THEME, themes[theme]);
    assert.deepEqual(THEME_NAMES, Object.keys(themes));
    assert.throws(() => loadApprovals(pickRoot(t, "dark", false)), {
      message: "kit gallery: approvals: gallery/exports/pick.json: theme is invalid",
    });
    assert.equal(loadApprovals(pickRoot(t, "light", true)).theme, "light");
  }],
];

// The kinds of the report's table row that are not explainer kinds: today
// `cover`. Task 4.2 registers them with the report branch.
const reportOnlyKinds = () => PRESETS.report.requiredKinds.filter((kind) => !Object.hasOwn(LIMITS.content, kind));

// The report row before any report approval (task 4.2).
const REPORT_ROW = [
  ["approvals.json holds no report: entry and the report view approves no kind", () => {
    const { entries } = JSON.parse(readFileSync(APPROVALS, "utf8"));
    assert.deepEqual(entries.filter(({ id }) => id.startsWith("report:")).map(({ id }) => id), []);
    assert.deepEqual(approvedOf("report"), []);
    assert.deepEqual([...loadApprovals(TEMPLATE).presets.report.decisions], []);
  }],
  ["no report theme is picked and themeFor(\"report\") throws", () => {
    assert.equal(loadApprovals(TEMPLATE).presets.report.theme, null);
    assert.throws(() => themeFor("report"), { message: "no approved theme for preset report" });
  }],
  ["no report kind in the schema, the registry or the JSON Schema, and the JSON Schema has no preset key", () => {
    const only = reportOnlyKinds();
    assert.deepEqual(only, ["cover"]);
    const surfaces = [
      ["the schema (LIMITS.content)", Object.keys(LIMITS.content)],
      ["the registry", registryEntries().map(entryKind)],
      ["the JSON Schema", schemaScenes().map(({ kind }) => kind)],
    ];
    assert.deepEqual(surfaces.flatMap(([surface, kinds]) => only.filter((kind) => kinds.includes(kind)).map((kind) => `${surface} has ${kind}`)), []);
    assert.equal(readFileSync(JSON_SCHEMA, "utf8").includes('"preset"'), false);
  }],
];

const ROWS = { explainer: EXPLAINER_ROW, report: REPORT_ROW };

test("the preset table holds exactly the explainer and the report, as the fixture states them", () => {
  assert.deepEqual(PRESETS, {
    explainer: { idPrefix: "", themeId: "theme", requiredKinds: ["hook", "summary", "end-card"], themeCandidates: ["light"] },
    report: { idPrefix: "report:", themeId: "report:theme", requiredKinds: ["cover", "summary", "end-card"], themeCandidates: [] },
  });
});

test("the gate has one row per preset of the table, and the catalog one list per preset", () => {
  assert.deepEqual(Object.keys(ROWS), Object.keys(PRESETS));
  assert.deepEqual(Object.keys(catalogs), Object.keys(PRESETS));
  assert.equal(catalogs.explainer, candidates);
  assert.deepEqual(catalogs.report, []);
});

test("the kit's themes are the union, over presets, of each preset's pick, or its candidates while it has none", () => {
  const { presets } = loadApprovals(TEMPLATE);
  const union = [];
  for (const [preset, { themeCandidates }] of Object.entries(PRESETS)) {
    const pick = presets[preset].theme;
    for (const name of pick === null ? themeCandidates : [pick]) if (!union.includes(name)) union.push(name);
  }
  assert.deepEqual(Object.keys(themes), union);
});

for (const preset of Object.keys(PRESETS)) {
  for (const [title, run] of ROWS[preset] ?? []) test(`${preset}: ${title}`, run);
}
