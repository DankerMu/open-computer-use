// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// What the user approves is what ships (task 4.2). The registry takes each
// catalog kind from src/kit/gallery/components.tsx by import, with no copy
// and no wrapper, so it holds the function object the gallery rendered; and
// every catalog candidate carries an `approve` entry in the tracked
// gallery/approvals.json. registry.ts is read as text, because Node cannot
// import .tsx. The approval test is red until the user's export lands.
//
// The newest export binds (task 4.3): each later approvals entry replaces an
// earlier one, so for every decision in the newest export under
// gallery/exports/ (the greatest name in code-unit order; names start with
// the date), loadApprovals gives that decision.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { loadApprovals } from "../bin/gallery-approvals.mjs";
import { candidates } from "../src/kit/gallery/catalog.ts";

const TEMPLATE = fileURLToPath(new URL("..", import.meta.url));
const REGISTRY = join(TEMPLATE, "src", "kit", "render", "registry.ts");
const EXPORTS = join(TEMPLATE, "gallery", "exports");

test("registry.ts imports components from the gallery and lists custom plus each catalog kind as components[id], in catalog order", () => {
  const source = readFileSync(REGISTRY, "utf8");
  assert.match(source, /^import \{components\} from '\.\.\/gallery\/components\.tsx';$/m);
  const literal = /^export const registry: Registry = \{([^}]*)\};$/m.exec(source);
  assert.ok(literal, "registry.ts has no `export const registry: Registry = {…};` literal");
  const entries = literal[1]
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);
  assert.deepEqual(entries, ["custom: CustomScene", ...candidates.map(({ id, kind }) => `'${kind}': components['${id}']`)]);
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
