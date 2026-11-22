// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// gallery/approvals.json, version 1, and its loader (task 4.1): the tracked
// file loads, its decisions name script kinds, its approvals catalog
// candidates and its theme a kit theme, empty or after the user's exports
// land (task 4.2); a later entry replaces an earlier one, and every
// refusal of the fixture gives its own message, one case each. Each case
// builds a temporary tree with gallery/approvals.json and the exports it
// links; digests are computed here with node:crypto over the bytes written.

import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { loadApprovals } from "../bin/gallery-approvals.mjs";
import { LIMITS } from "../bin/script-schema.mjs";
import { candidates } from "../src/kit/gallery/catalog.ts";
import { themes } from "../src/kit/theme.ts";

const TEMPLATE = fileURLToPath(new URL("..", import.meta.url));
const APPROVALS = "gallery/approvals.json";
const A = "gallery/exports/a.json";
const B = "gallery/exports/b.json";
const PAGE = "cd".repeat(32);

const sha256 = (text) => createHash("sha256").update(text).digest("hex");
const text = (value) => `${JSON.stringify(value, null, 2)}\n`;

function exportBody(overrides = {}) {
  return {
    version: 1,
    page: PAGE,
    reviewer: "Fixture Reviewer",
    date: "2026-11-19",
    decisions: [
      { id: "quiz", decision: "approve" },
      { id: "chart-a", decision: "redo", note: "slower bars" },
    ],
    theme: "light",
    ...overrides,
  };
}

// A tree with the exports (path -> object or raw string) and approvals
// entries built by `entries(digests)`, where digests maps export path to
// the digest of its written bytes.
function tree(t, { exports = { [A]: exportBody() }, entries, approvals }) {
  const root = mkdtempSync(join(tmpdir(), "gallery-approvals-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  mkdirSync(join(root, "gallery", "exports"), { recursive: true });
  const digests = {};
  for (const [path, value] of Object.entries(exports)) {
    const bytes = typeof value === "string" ? value : text(value);
    writeFileSync(join(root, path), bytes);
    digests[path] = sha256(bytes);
  }
  const body = approvals ?? { version: 1, entries: entries(digests) };
  writeFileSync(join(root, APPROVALS), typeof body === "string" ? body : text(body));
  return root;
}

function entry(digests, overrides = {}) {
  return {
    id: "quiz",
    decision: "approve",
    reviewer: "Fixture Reviewer",
    date: "2026-11-19",
    export: A,
    export_sha256: digests[A],
    ...overrides,
  };
}

function refused(root, problem) {
  assert.throws(() => loadApprovals(root), (error) => {
    assert.ok(error instanceof Error);
    assert.equal(error.message, `kit gallery: approvals: ${problem}`);
    return true;
  });
}

const one = (overrides) => (digests) => [entry(digests, typeof overrides === "function" ? overrides(digests) : overrides)];

// A redo or drop entry outlives its candidate, which step 7 of a batch
// removes from the catalog; an approve names one that ships. Candidate ids
// equal their kinds in batch 4.2.
test("the tracked approvals file loads: every decision names a script kind, every approve names a catalog candidate, the theme is null or a kit theme", () => {
  const loaded = loadApprovals(TEMPLATE);
  assert.ok(loaded.decisions instanceof Map);
  const kinds = new Set(Object.keys(LIMITS.content));
  const ids = new Set(candidates.map(({ id }) => id));
  for (const [id, decision] of loaded.decisions) {
    assert.ok(kinds.has(id), `${APPROVALS} decides ${JSON.stringify(id)}, which is not a script kind`);
    if (decision === "approve") assert.ok(ids.has(id), `${APPROVALS} approves ${JSON.stringify(id)}, which the catalog lacks`);
  }
  assert.ok(loaded.theme === null || Object.keys(themes).includes(loaded.theme), `${APPROVALS} picks theme ${JSON.stringify(loaded.theme)}`);
});

test("entries load as decisions and the theme; a later entry replaces an earlier one; redo is not approve", (t) => {
  const root = tree(t, {
    exports: {
      [A]: exportBody(),
      [B]: exportBody({ date: "2026-11-20", decisions: [{ id: "quiz", decision: "redo" }], theme: null }),
    },
    entries: (d) => [
      entry(d),
      entry(d, { id: "chart-a", decision: "redo" }),
      entry(d, { id: "theme", decision: "light" }),
      entry(d, { decision: "redo", date: "2026-11-20", export: B, export_sha256: d[B] }),
    ],
  });
  const loaded = loadApprovals(root);
  assert.deepEqual([...loaded.decisions], [["quiz", "redo"], ["chart-a", "redo"]]);
  assert.notEqual(loaded.decisions.get("quiz"), "approve");
  assert.equal(loaded.theme, "light");
});

test("a drop decision and a theme-only export load", (t) => {
  const root = tree(t, {
    exports: { [A]: exportBody({ decisions: [{ id: "quiz", decision: "drop" }] }), [B]: exportBody({ decisions: [] }) },
    entries: (d) => [entry(d, { decision: "drop" }), entry(d, { id: "theme", decision: "light", export: B, export_sha256: d[B] })],
  });
  assert.deepEqual(loadApprovals(root), { decisions: new Map([["quiz", "drop"]]), theme: "light" });
});

// --- shape --------------------------------------------------------------------

test("an approvals file that is missing, not JSON or not an object is refused", (t) => {
  const empty = mkdtempSync(join(tmpdir(), "gallery-approvals-"));
  t.after(() => rmSync(empty, { recursive: true, force: true }));
  refused(empty, `${APPROVALS} is missing`);
  refused(tree(t, { approvals: "{" }), `${APPROVALS} is not valid JSON`);
  refused(tree(t, { approvals: [] }), `${APPROVALS} is not a JSON object`);
});

test("the approvals file's version, fields and entries are checked", (t) => {
  refused(tree(t, { approvals: { version: 2, entries: [] } }), `${APPROVALS}: version must be 1`);
  refused(tree(t, { approvals: { version: 1, entries: [], extra: 1 } }), `${APPROVALS} has unknown field "extra"`);
  refused(tree(t, { approvals: { version: 1 } }), `${APPROVALS} is missing field "entries"`);
  refused(tree(t, { approvals: { version: 1, entries: {} } }), `${APPROVALS}: entries is invalid`);
  refused(tree(t, { entries: (d) => [{ ...entry(d), note: "x" }] }), `${APPROVALS}: entries[0] has unknown field "note"`);
  refused(
    tree(t, {
      entries: (d) => {
        const { export_sha256: _, ...rest } = entry(d);
        return [rest];
      },
    }),
    `${APPROVALS}: entries[0] is missing field "export_sha256"`,
  );
  refused(tree(t, { entries: () => ["quiz"] }), `${APPROVALS}: entries[0] is not a JSON object`);
});

test("an entry's field values are checked", (t) => {
  refused(tree(t, { entries: one({ id: "Quiz" }) }), `${APPROVALS}: entries[0].id is invalid`);
  refused(tree(t, { entries: one({ decision: "maybe" }) }), `${APPROVALS}: entries[0].decision is invalid`);
  refused(tree(t, { entries: one({ id: "theme", decision: "approve" }) }), `${APPROVALS}: entries[0].decision is invalid`);
  refused(tree(t, { entries: one({ decision: "light" }) }), `${APPROVALS}: entries[0].decision is invalid`);
  refused(tree(t, { entries: one({ reviewer: " " }) }), `${APPROVALS}: entries[0].reviewer is invalid`);
  refused(tree(t, { entries: one({ date: "2026-02-30" }) }), `${APPROVALS}: entries[0].date is invalid`);
  refused(tree(t, { entries: one({ export_sha256: "AB".repeat(32) }) }), `${APPROVALS}: entries[0].export_sha256 is invalid`);
});

test("a linked export's version, fields and values are checked", (t) => {
  const bad = (overrides) => tree(t, { exports: { [A]: exportBody(overrides) }, entries: one({}) });
  refused(bad({ version: 2 }), `${A}: version must be 1`);
  refused(bad({ extra: true }), `${A} has unknown field "extra"`);
  refused(tree(t, { exports: { [A]: { ...exportBody(), theme: undefined } }, entries: one({}) }), `${A} is missing field "theme"`);
  refused(bad({ decisions: [{ id: "quiz", decision: "approve", why: "x" }] }), `${A}: decisions[0] has unknown field "why"`);
  refused(bad({ decisions: [{ decision: "approve" }] }), `${A}: decisions[0] is missing field "id"`);
  refused(bad({ decisions: [{ id: "quiz", decision: "maybe" }] }), `${A}: decisions[0].decision is invalid`);
  refused(bad({ decisions: [{ id: "quiz", decision: "approve", note: "" }] }), `${A}: decisions[0].note is invalid`);
  refused(bad({ page: "x" }), `${A}: page is invalid`);
  refused(bad({ theme: "blue" }), `${A}: theme is invalid`);
  refused(tree(t, { exports: { [A]: "[]" }, entries: one({}) }), `${A} is not a JSON object`);
  refused(tree(t, { exports: { [A]: "{" }, entries: one({}) }), `${A} is not valid JSON`);
});

// --- export link ----------------------------------------------------------------

test("an export path outside gallery/exports/<name>.json is refused", (t) => {
  const pattern = "^gallery/exports/[A-Za-z0-9._-]+\\.json$";
  for (const path of ["gallery/approvals.json", "gallery/exports/sub/a.json", "gallery/exports/a.txt", "/gallery/exports/a.json"]) {
    refused(tree(t, { entries: one({ export: path }) }), `${APPROVALS}: entries[0].export must match ${pattern}`);
  }
});

test("an export that is missing, a symlink or a directory is refused", (t) => {
  refused(tree(t, { entries: one({ export: "gallery/exports/none.json" }) }), "gallery/exports/none.json is missing");
  const linked = tree(t, { entries: one({ export: "gallery/exports/link.json" }) });
  symlinkSync(join(linked, A), join(linked, "gallery/exports/link.json"));
  refused(linked, "gallery/exports/link.json is not a regular file");
  const directory = tree(t, { entries: one({ export: "gallery/exports/dir.json" }) });
  mkdirSync(join(directory, "gallery/exports/dir.json"));
  refused(directory, "gallery/exports/dir.json is not a regular file");
});

test("an export_sha256 that differs from the export's digest is refused, all else matching", (t) => {
  const root = tree(t, { entries: one((d) => ({ export_sha256: sha256(`${text(exportBody())} `) })) });
  refused(root, `${APPROVALS}: entries[0].export_sha256 does not match ${A}`);
});

// --- entry against export -------------------------------------------------------

test("an entry whose reviewer or date differs from its export's is refused", (t) => {
  refused(tree(t, { entries: one({ reviewer: "Someone Else" }) }), `${APPROVALS}: entries[0].reviewer differs from ${A}`);
  refused(tree(t, { entries: one({ date: "2026-11-18" }) }), `${APPROVALS}: entries[0].date differs from ${A}`);
});

test("a candidate entry not in its export, or with another decision, is refused", (t) => {
  refused(tree(t, { entries: one({ id: "timeline-a" }) }), `${APPROVALS}: entries[0].id "timeline-a" is not in ${A}`);
  refused(tree(t, { entries: one({ decision: "drop" }) }), `${APPROVALS}: entries[0].decision differs from ${A}`);
});

test("a theme entry with the unknown theme dark, or whose export has theme null, is refused", (t) => {
  refused(tree(t, { entries: one({ id: "theme", decision: "dark" }) }), `${APPROVALS}: entries[0].decision is invalid`);
  const unset = tree(t, { exports: { [A]: exportBody({ theme: null }) }, entries: one({ id: "theme", decision: "light" }) });
  refused(unset, `${APPROVALS}: entries[0]: ${A} has theme null`);
});

test("the second entry is located as entries[1]", (t) => {
  refused(tree(t, { entries: (d) => [entry(d), entry(d, { decision: "redo" })] }), `${APPROVALS}: entries[1].decision differs from ${A}`);
});
