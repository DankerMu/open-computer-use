// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// Proves the template test runner executes: zod resolves from the template's
// node_modules, and the template package.json still declares no `scripts`
// (tests/test_remotion_image.py pins the same rule for the image build).

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { z } from "zod";

test("template resolves zod and declares no scripts", () => {
  assert.deepEqual(z.object({ a: z.string() }).parse({ a: "x" }), { a: "x" });
  const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
  assert.equal(Object.hasOwn(pkg, "scripts"), false);
});
