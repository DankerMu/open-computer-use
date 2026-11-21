// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// What the user approves is what ships (task 4.2). The registry takes each
// catalog kind from src/kit/gallery/components.tsx by import, with no copy
// and no wrapper, so it holds the function object the gallery rendered; and
// every catalog candidate carries an `approve` entry in the tracked
// gallery/approvals.json. registry.ts is read as text, because Node cannot
// import .tsx. The approval test is red until the user's export lands.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { loadApprovals } from "../bin/gallery-approvals.mjs";
import { candidates } from "../src/kit/gallery/catalog.ts";

const TEMPLATE = fileURLToPath(new URL("..", import.meta.url));
const REGISTRY = join(TEMPLATE, "src", "kit", "render", "registry.ts");

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
