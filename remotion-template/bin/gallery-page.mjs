// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The gallery's static page (task 4.1). renderPage(gallery, pageSha256) is
// pure: it returns index.html for a gallery.json object and the SHA-256 of
// that file's bytes. The page loads nothing remote. A file:// page cannot
// fetch gallery.json, so the candidate ids and the digest are embedded as
// JSON. Its export button downloads gallery-decisions.json, version 1, only
// when a reviewer, a valid YYYY-MM-DD date and a decision for every
// candidate are set; otherwise the status line says what is missing.

const SCRIPT = `
const {ids, page} = JSON.parse(document.getElementById("gallery-data").textContent);
const DATE = /^([0-9]{4})-([0-9]{2})-([0-9]{2})$/;
const validDate = (text) => {
  const match = DATE.exec(text);
  if (!match) return false;
  const day = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
  return day.toISOString().slice(0, 10) === text;
};
document.getElementById("export").addEventListener("click", () => {
  const status = document.getElementById("status");
  const reviewer = document.getElementById("reviewer").value.trim();
  const date = document.getElementById("date").value;
  const decisions = [];
  const undecided = [];
  for (const id of ids) {
    const chosen = document.querySelector(\`input[name="decision-\${id}"]:checked\`);
    if (!chosen) {
      undecided.push(id);
      continue;
    }
    const note = document.getElementById(\`note-\${id}\`).value.trim();
    decisions.push(note === "" ? {id, decision: chosen.value} : {id, decision: chosen.value, note});
  }
  const missing = [];
  if (reviewer === "") missing.push("a reviewer");
  if (!validDate(date)) missing.push("a date in YYYY-MM-DD form");
  if (undecided.length > 0) missing.push(\`a decision for \${undecided.join(", ")}\`);
  if (missing.length > 0) {
    status.textContent = \`Nothing exported: missing \${missing.join(", ")}.\`;
    return;
  }
  const picked = document.querySelector('input[name="theme"]:checked');
  const theme = picked ? picked.value : null;
  const body = JSON.stringify({version: 1, page, reviewer, date, decisions, theme}, null, 2) + "\\n";
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([body], {type: "application/json"}));
  link.download = "gallery-decisions.json";
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  status.textContent = \`Exported gallery-decisions.json with \${decisions.length} decisions.\`;
});
`;

const STYLE = `
body { font-family: sans-serif; margin: 2rem auto; max-width: 80rem; line-height: 1.4; }
section.candidate, section.theme { border: 1px solid #ccc; border-radius: 6px; padding: 0.8rem 1rem; margin: 1rem 0; }
.item { margin: 0.8rem 0; }
.item video { width: 48rem; max-width: 100%; }
.item img { width: 15.5rem; margin-right: 0.5rem; }
.violations { color: #a00; font-family: monospace; }
.clean { color: #070; }
`;

const ROLES = ["entrance-end", "midpoint", "pre-exit"];
const DECISIONS = ["approve", "redo", "drop"];
const ENTITIES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

const esc = (text) => String(text).replace(/[&<>"']/g, (c) => ENTITIES[c]);
// JSON inside a <script> element: no `<`, so nothing can close it.
const scriptJson = (value) => JSON.stringify(value).replace(/</g, "\\u003c");

function itemHtml(item) {
  const probe =
    item.violations.length === 0
      ? '<p class="clean">probe: clean</p>'
      : ['<ul class="violations">', ...item.violations.map((line) => `<li>${esc(line)}</li>`), "</ul>"].join("\n");
  return [
    `<div class="item" id="item-${esc(item.id)}">`,
    `<h4>${esc(item.sample)} · ${esc(item.theme)} <code>${esc(item.id)}</code></h4>`,
    `<video controls preload="metadata" src="${esc(item.clip)}"></video>`,
    "<div>",
    ...ROLES.map((role) => `<img alt="${esc(`${item.id} ${role}`)}" src="${esc(item.frames[role])}">`),
    "</div>",
    probe,
    "</div>",
  ].join("\n");
}

function decisionHtml(id) {
  return [
    "<p>",
    ...DECISIONS.map((value) => `<label><input type="radio" name="decision-${esc(id)}" value="${value}"> ${value}</label>`),
    `<label>Note <input type="text" id="note-${esc(id)}"></label>`,
    "</p>",
  ].join("\n");
}

const itemsOf = (gallery, candidate) => gallery.items.filter((item) => item.candidate === candidate).map(itemHtml);

export function renderPage(gallery, pageSha256) {
  const ids = gallery.candidates.map(({ id }) => id);
  const parts = [
    "<!DOCTYPE html>",
    '<html lang="en">',
    "<head>",
    '<meta charset="utf-8">',
    "<title>Explainer kit gallery</title>",
    `<style>${STYLE}</style>`,
    "</head>",
    "<body>",
    "<h1>Explainer kit gallery</h1>",
    `<p>Node ${esc(gallery.node)}. Page <code>${esc(pageSha256)}</code>.</p>`,
    "<p>Watch each sample in both themes and read its probe result. Choose approve, redo or drop for every candidate," +
      " pick a theme or leave it unset, then export the decisions.</p>",
    '<p><label>Reviewer <input id="reviewer" type="text"></label>' +
      ' <label>Date <input id="date" type="date"></label>' +
      ' <button id="export" type="button">Export decisions</button></p>',
    '<p id="status" role="status"></p>',
    '<section class="theme">',
    "<h2>Theme</h2>",
    ...gallery.themes.map((theme) => `<label><input type="radio" name="theme" value="${esc(theme)}"> ${esc(theme)}</label>`),
    ...itemsOf(gallery, "theme"),
    "</section>",
  ];
  for (const candidate of gallery.candidates) {
    parts.push(
      `<section class="candidate" id="candidate-${esc(candidate.id)}">`,
      `<h2><code>${esc(candidate.id)}</code> ${esc(candidate.kind)}</h2>`,
      `<p>Source: ${esc(candidate.source)}</p>`,
      decisionHtml(candidate.id),
      ...itemsOf(gallery, candidate.id),
      "</section>",
    );
  }
  parts.push(
    `<script type="application/json" id="gallery-data">${scriptJson({ ids, page: pageSha256 })}</script>`,
    `<script>${SCRIPT}</script>`,
    "</body>",
    "</html>",
    "",
  );
  return parts.join("\n");
}
