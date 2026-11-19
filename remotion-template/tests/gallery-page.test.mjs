// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The gallery page (task 4.1): renderPage over a hand-written gallery.json,
// and its export script run in a vm context against a strict DOM stub, as
// tests/deploy/test_music_listening.py runs the music page's. Expected
// bodies are written out here from the fixture's export format: catalog
// order, the note rule, `page` and `theme`. The round trip writes a page
// export to a temporary gallery/exports/ and reads it back with
// loadApprovals.

import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import vm from "node:vm";
import { loadApprovals } from "../bin/gallery-approvals.mjs";
import { renderPage } from "../bin/gallery-page.mjs";

const PAGE_SHA = "ab".repeat(32);
const ROLES = ["entrance-end", "midpoint", "pre-exit"];
const VIOLATION = "alpha--s1--dark pre-exit frame 59: outside safe area: div:nth-child(1)>div:nth-child(2) right 12 px";

function item(candidate, sample, theme, violations = []) {
  const id = `${candidate}--${sample}--${theme}`;
  return {
    id,
    candidate,
    sample,
    theme,
    clip: `clips/${id}.mp4`,
    frames: Object.fromEntries(ROLES.map((role) => [role, `frames/${id}--${role}.png`])),
    violations,
  };
}

// Catalog order is not alphabetical, so a sorted export fails.
function gallery(candidates = [
  { id: "zeta-card", kind: "concept-card", source: "onda <Card> & co" },
  { id: "alpha", kind: "question", source: "written for the kit" },
]) {
  const pairs = [["theme", "sample"], ...candidates.map(({ id }) => [id, "s1"])];
  const items = ["light", "dark"].flatMap((theme) =>
    pairs.map(([c, s]) => item(c, s, theme, c === "alpha" && theme === "dark" ? [VIOLATION] : [])),
  );
  return { version: 1, node: "v22.23.2", themes: ["light", "dark"], candidates, items };
}

const escapeHtml = (text) =>
  text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");

function scripts(html) {
  const data = /<script type="application\/json" id="gallery-data">([^<]*)<\/script>/.exec(html);
  const code = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  assert.ok(data, "no gallery-data script");
  assert.equal(code.length, 1, "one page script");
  return { data: data[1], code: code[0][1] };
}

// Runs the page script against a stub DOM holding only the page's own
// controls; `form` sets the reviewer, date, decisions, notes and theme
// before the export button is clicked.
async function exportPage(html, form) {
  const { data, code } = scripts(html);
  const { ids } = JSON.parse(data);
  const elements = {};
  const element = (id) => {
    elements[id] = {
      value: "",
      textContent: "",
      listeners: {},
      addEventListener(type, listener) {
        this.listeners[type] = listener;
      },
    };
  };
  for (const id of ["gallery-data", "reviewer", "date", "status", "export", ...ids.map((c) => `note-${c}`)]) element(id);
  elements["gallery-data"].textContent = data;
  const blobs = new Map();
  const revoked = [];
  let clicked = null;
  const document = {
    getElementById(id) {
      if (!(id in elements)) throw new Error(`unexpected element id ${id}`);
      return elements[id];
    },
    querySelector(selector) {
      const decision = /^input\[name="decision-([a-z0-9-]+)"\]:checked$/.exec(selector);
      if (decision) {
        if (!ids.includes(decision[1])) throw new Error(`unexpected candidate ${decision[1]}`);
        const value = form.decisions?.[decision[1]];
        return value === undefined ? null : { value };
      }
      if (selector === 'input[name="theme"]:checked') return form.theme === undefined ? null : { value: form.theme };
      throw new Error(`unexpected selector ${selector}`);
    },
    createElement(tag) {
      if (tag !== "a") throw new Error(`unexpected element ${tag}`);
      return {
        href: "",
        download: "",
        click() {
          clicked = { href: this.href, download: this.download };
        },
      };
    },
  };
  const URL = {
    createObjectURL(blob) {
      const href = `blob:export-${blobs.size}`;
      blobs.set(href, blob);
      return href;
    },
    revokeObjectURL(href) {
      revoked.push(href);
    },
  };
  vm.runInNewContext(code, { document, URL, Blob, setTimeout: (callback) => callback() });
  elements.reviewer.value = form.reviewer ?? "";
  elements.date.value = form.date ?? "";
  for (const [id, note] of Object.entries(form.notes ?? {})) elements[`note-${id}`].value = note;
  assert.equal(typeof elements.export.listeners.click, "function", "no click listener on export");
  elements.export.listeners.click();
  const body = clicked ? await blobs.get(clicked.href).text() : null;
  return { status: elements.status.textContent, download: clicked?.download ?? null, href: clicked?.href ?? null, revoked, body };
}

const COMPLETE = {
  reviewer: "  Fixture Reviewer ",
  date: "2026-11-19",
  decisions: { alpha: "redo", "zeta-card": "approve" },
  notes: { "zeta-card": "  keep the pacing  ", alpha: "   " },
  theme: "dark",
};

// --- the page ---------------------------------------------------------------

test("each item shows its clip, three key frames and its probe result", () => {
  const data = gallery();
  const html = renderPage(data, PAGE_SHA);
  for (const entry of data.items) {
    assert.ok(html.includes(`<video controls preload="metadata" src="${entry.clip}"></video>`), entry.id);
    for (const role of ROLES) assert.ok(html.includes(`src="${entry.frames[role]}"`), `${entry.id} ${role}`);
  }
  assert.equal(html.split("probe: clean").length - 1, data.items.length - 1);
  assert.ok(html.includes(`<li>${escapeHtml(VIOLATION)}</li>`));
  assert.ok(html.includes(escapeHtml("onda <Card> & co")));
  assert.ok(!html.includes("onda <Card>"));
});

test("each candidate has approve, redo and drop and a note; the theme has light and dark; no remote resource", () => {
  const html = renderPage(gallery(), PAGE_SHA);
  for (const id of ["zeta-card", "alpha"]) {
    for (const value of ["approve", "redo", "drop"]) {
      assert.ok(html.includes(`<input type="radio" name="decision-${id}" value="${value}">`), `${id} ${value}`);
    }
    assert.ok(html.includes(`id="note-${id}"`), id);
  }
  for (const value of ["light", "dark"]) assert.ok(html.includes(`<input type="radio" name="theme" value="${value}">`), value);
  for (const id of ["reviewer", "date", "export", "status"]) assert.ok(html.includes(`id="${id}"`), id);
  for (const match of html.matchAll(/\b(?:src|href)="([^"]*)"/g)) assert.match(match[1], /^(clips|frames)\/[^/]+$/);
  assert.ok(!/<link\b|<script\s+src|\burl\(|@import/.test(html));
  assert.ok(html.startsWith("<!DOCTYPE html>\n"));
  assert.ok(html.endsWith("</html>\n"));
});

test("the embedded data holds the candidate ids in catalog order and the page digest", () => {
  const { data } = scripts(renderPage(gallery(), PAGE_SHA));
  assert.deepEqual(JSON.parse(data), { ids: ["zeta-card", "alpha"], page: PAGE_SHA });
});

test("the embedded data cannot close its script element", () => {
  const html = renderPage(gallery([{ id: "alpha", kind: "question", source: "</script><b>" }]), PAGE_SHA);
  const { data } = scripts(html);
  assert.ok(!data.includes("<"));
});

// --- the export -------------------------------------------------------------

test("a complete selection downloads gallery-decisions.json with the exact body", async () => {
  const result = await exportPage(renderPage(gallery(), PAGE_SHA), COMPLETE);
  assert.equal(result.download, "gallery-decisions.json");
  assert.equal(
    result.body,
    [
      "{",
      '  "version": 1,',
      `  "page": "${PAGE_SHA}",`,
      '  "reviewer": "Fixture Reviewer",',
      '  "date": "2026-11-19",',
      '  "decisions": [',
      "    {",
      '      "id": "zeta-card",',
      '      "decision": "approve",',
      '      "note": "keep the pacing"',
      "    },",
      "    {",
      '      "id": "alpha",',
      '      "decision": "redo"',
      "    }",
      "  ],",
      '  "theme": "dark"',
      "}",
      "",
    ].join("\n"),
  );
  assert.equal(result.status, "Exported gallery-decisions.json with 2 decisions.");
  assert.deepEqual(result.revoked, [result.href]);
});

test("a missing decision, reviewer or date downloads nothing and says what is missing", async () => {
  const html = renderPage(gallery(), PAGE_SHA);
  const cases = [
    [{ ...COMPLETE, decisions: { "zeta-card": "approve" } }, "Nothing exported: missing a decision for alpha."],
    [{ ...COMPLETE, decisions: {} }, "Nothing exported: missing a decision for zeta-card, alpha."],
    [{ ...COMPLETE, reviewer: "   " }, "Nothing exported: missing a reviewer."],
    [{ ...COMPLETE, date: "" }, "Nothing exported: missing a date in YYYY-MM-DD form."],
    [{ ...COMPLETE, date: "2026-11-31" }, "Nothing exported: missing a date in YYYY-MM-DD form."],
    [{ ...COMPLETE, date: "2026-1-19" }, "Nothing exported: missing a date in YYYY-MM-DD form."],
    [
      { theme: "light" },
      "Nothing exported: missing a reviewer, a date in YYYY-MM-DD form, a decision for zeta-card, alpha.",
    ],
  ];
  for (const [form, status] of cases) {
    const result = await exportPage(html, form);
    assert.equal(result.status, status);
    assert.equal(result.download, null);
    assert.equal(result.body, null);
    assert.deepEqual(result.revoked, []);
  }
});

test("an unset theme exports theme null", async () => {
  const { theme, ...form } = COMPLETE;
  assert.equal(theme, "dark");
  const result = await exportPage(renderPage(gallery(), PAGE_SHA), form);
  assert.equal(JSON.parse(result.body).theme, null);
});

test("a page without candidates exports decisions []", async () => {
  const result = await exportPage(renderPage(gallery([]), PAGE_SHA), { reviewer: "R", date: "2026-11-19", theme: "light" });
  assert.equal(result.body, `${JSON.stringify({ version: 1, page: PAGE_SHA, reviewer: "R", date: "2026-11-19", decisions: [], theme: "light" }, null, 2)}\n`);
  assert.equal(result.status, "Exported gallery-decisions.json with 0 decisions.");
});

// --- round trip ---------------------------------------------------------------

test("a page export committed with matching entries loads as those decisions and that theme", async (t) => {
  const root = mkdtempSync(join(tmpdir(), "gallery-round-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const { body } = await exportPage(renderPage(gallery(), PAGE_SHA), COMPLETE);
  mkdirSync(join(root, "gallery", "exports"), { recursive: true });
  const exportPath = "gallery/exports/2026-11-19-batch.json";
  writeFileSync(join(root, exportPath), body);
  const digest = createHash("sha256").update(body).digest("hex");
  const entry = (id, decision) => ({ id, decision, reviewer: "Fixture Reviewer", date: "2026-11-19", export: exportPath, export_sha256: digest });
  const approvals = { version: 1, entries: [entry("zeta-card", "approve"), entry("alpha", "redo"), entry("theme", "dark")] };
  writeFileSync(join(root, "gallery", "approvals.json"), `${JSON.stringify(approvals, null, 2)}\n`);
  const loaded = loadApprovals(root);
  assert.deepEqual([...loaded.decisions], [["zeta-card", "approve"], ["alpha", "redo"]]);
  assert.equal(loaded.theme, "dark");
});
