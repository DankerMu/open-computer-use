// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// gallery/approvals.json, version 1 (task 4.1): the user's gallery decisions,
// each entry linking the user's own page export, committed unmodified under
// gallery/exports/. loadApprovals(root, presets) reads it without side
// effects. It returns {presets: {<preset>: {decisions, theme}}, decisions,
// theme}, where each preset's decisions is a Map keyed by bare kind and the
// top-level decisions and theme are the explainer's view. An entry's id
// names its preset (src/kit/presets.ts): a theme id, a bare kind for the
// explainer, or `<preset>:<kind>`. An export without `preset` is the
// explainer's; its decision ids and theme are of its preset, and so is every
// entry linking it. A later entry for an id replaces an earlier one; `redo`
// stays `redo`, which is not `approve`. Every entry is checked against its
// export: the link (pattern, a regular file by lstat, the digest), the
// export's own shape, and the preset, reviewer, date and decision it
// records. Anything else throws `kit gallery: approvals: <problem>`.

import { createHash } from "node:crypto";
import { lstatSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { isCandidateId } from "../src/kit/gallery/catalog-rules.ts";
import { DEFAULT_PRESET, PRESETS } from "../src/kit/presets.ts";

const APPROVALS = "gallery/approvals.json";
const EXPORT_SOURCE = "^gallery/exports/[A-Za-z0-9._-]+\\.json$";
const EXPORT_PATTERN = new RegExp(EXPORT_SOURCE);
const DECISIONS = ["approve", "redo", "drop"];
const SHA256 = /^[0-9a-f]{64}$/;
const DATE = /^([0-9]{4})-([0-9]{2})-([0-9]{2})$/;
const ENTRY_FIELDS = ["id", "decision", "reviewer", "date", "export", "export_sha256"];
const EXPORT_FIELDS = ["version", "page", "reviewer", "date", "decisions", "theme"];
const EXPORT_OPTIONAL = ["preset"];

const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const isText = (value) => typeof value === "string" && value.trim() !== "";

function isDate(value) {
  const match = typeof value === "string" ? DATE.exec(value) : null;
  if (!match) return false;
  const day = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
  return day.toISOString().slice(0, 10) === value;
}

function fail(problem) {
  throw new Error(`kit gallery: approvals: ${problem}`);
}

// `<file>` for the whole file, `<file>: <path>` inside it.
const at = (file, path) => (path === "" ? file : `${file}: ${path}`);
const field = (path, key) => (path === "" ? key : `${path}.${key}`);

// An object with exactly `required`, plus any of `optional`.
function checkFields(value, file, path, required, optional = []) {
  if (!isObject(value)) fail(`${at(file, path)} is not a JSON object`);
  for (const key of Object.keys(value)) {
    if (!required.includes(key) && !optional.includes(key)) fail(`${at(file, path)} has unknown field ${JSON.stringify(key)}`);
  }
  for (const key of required) {
    if (!Object.hasOwn(value, key)) fail(`${at(file, path)} is missing field ${JSON.stringify(key)}`);
  }
}

function check(ok, file, path) {
  if (!ok) fail(`${at(file, path)} is invalid`);
}

function parse(bytes, file) {
  try {
    return JSON.parse(bytes.toString("utf8"));
  } catch {
    fail(`${file} is not valid JSON`);
  }
}

function checkVersion(value, file) {
  if (value.version !== 1) fail(`${at(file, "version")} must be 1`);
}

// The preset and kind of a decision id: `<idPrefix><kind>` of a preset of
// the table, or null. Kinds hold no colon, so at most one preset matches.
function kindOf(id, presets) {
  if (typeof id !== "string") return null;
  for (const [preset, { idPrefix }] of Object.entries(presets)) {
    if (id.startsWith(idPrefix) && isCandidateId(id.slice(idPrefix.length))) return { preset, kind: id.slice(idPrefix.length) };
  }
  return null;
}

// An entry id's preset, with theme true for a theme id, or null.
function parseId(id, presets) {
  const themed = Object.entries(presets).find(([, { themeId }]) => id === themeId);
  if (themed !== undefined) return { preset: themed[0], theme: true };
  const parsed = kindOf(id, presets);
  return parsed === null ? null : { ...parsed, theme: false };
}

// The checked entry's parsed id.
function checkEntry(entry, path, presets) {
  checkFields(entry, APPROVALS, path, ENTRY_FIELDS);
  const parsed = parseId(entry.id, presets);
  check(parsed !== null, APPROVALS, field(path, "id"));
  const allowed = parsed.theme ? presets[parsed.preset].themeCandidates : DECISIONS;
  check(allowed.includes(entry.decision), APPROVALS, field(path, "decision"));
  check(isText(entry.reviewer), APPROVALS, field(path, "reviewer"));
  check(isDate(entry.date), APPROVALS, field(path, "date"));
  if (typeof entry.export !== "string" || !EXPORT_PATTERN.test(entry.export)) {
    fail(`${at(APPROVALS, field(path, "export"))} must match ${EXPORT_SOURCE}`);
  }
  check(typeof entry.export_sha256 === "string" && SHA256.test(entry.export_sha256), APPROVALS, field(path, "export_sha256"));
  return parsed;
}

// The export's preset: absent for the explainer, else another preset of the
// table, so each preset has one encoding.
function exportPreset(value, file, presets) {
  if (!Object.hasOwn(value, "preset")) return DEFAULT_PRESET;
  const { preset } = value;
  check(typeof preset === "string", file, "preset");
  if (preset === DEFAULT_PRESET) fail(`${file}: preset ${DEFAULT_PRESET} is implied; omit it`);
  if (!Object.hasOwn(presets, preset)) fail(`${file}: unknown preset ${preset}`);
  return preset;
}

// The checked export's preset.
function checkExport(value, file, presets) {
  checkFields(value, file, "", EXPORT_FIELDS, EXPORT_OPTIONAL);
  checkVersion(value, file);
  const preset = exportPreset(value, file, presets);
  check(typeof value.page === "string" && SHA256.test(value.page), file, "page");
  check(isText(value.reviewer), file, "reviewer");
  check(isDate(value.date), file, "date");
  check(Array.isArray(value.decisions), file, "decisions");
  value.decisions.forEach((decision, i) => {
    const path = `decisions[${i}]`;
    checkFields(decision, file, path, ["id", "decision"], ["note"]);
    const parsed = kindOf(decision.id, presets);
    check(parsed !== null, file, `${path}.id`);
    if (parsed.preset !== preset) fail(`${file}: ${path}.id ${decision.id} is not of preset ${preset}`);
    check(DECISIONS.includes(decision.decision), file, `${path}.decision`);
    if (Object.hasOwn(decision, "note")) check(isText(decision.note), file, `${path}.note`);
  });
  check(value.theme === null || presets[preset].themeCandidates.includes(value.theme), file, "theme");
  return preset;
}

// The export an entry links: a regular file (lstat, so not a symlink) whose
// digest is the entry's, in the export format. Returns it with its preset.
function readExport(root, entry, path, presets) {
  const file = entry.export;
  let stat;
  try {
    stat = lstatSync(join(root, file));
  } catch (error) {
    if (error.code === "ENOENT") fail(`${file} is missing`);
    fail(`${file}: ${error.message}`);
  }
  if (!stat.isFile()) fail(`${file} is not a regular file`);
  const bytes = readFileSync(join(root, file));
  if (createHash("sha256").update(bytes).digest("hex") !== entry.export_sha256) {
    fail(`${at(APPROVALS, field(path, "export_sha256"))} does not match ${file}`);
  }
  const value = parse(bytes, file);
  return { value, preset: checkExport(value, file, presets) };
}

function checkAgainstExport(entry, path, parsed, { value: exported, preset }) {
  const file = entry.export;
  if (parsed.preset !== preset) fail(`${at(APPROVALS, path)}: preset ${parsed.preset} does not match its export's preset ${preset}`);
  for (const key of ["reviewer", "date"]) {
    if (entry[key] !== exported[key]) fail(`${at(APPROVALS, field(path, key))} differs from ${file}`);
  }
  if (parsed.theme) {
    if (exported.theme === null) fail(`${at(APPROVALS, path)}: ${file} has theme null`);
    if (entry.decision !== exported.theme) fail(`${at(APPROVALS, field(path, "decision"))} differs from ${file}'s theme`);
    return;
  }
  const decided = exported.decisions.find(({ id }) => id === entry.id);
  if (decided === undefined) fail(`${at(APPROVALS, field(path, "id"))} ${JSON.stringify(entry.id)} is not in ${file}`);
  if (decided.decision !== entry.decision) fail(`${at(APPROVALS, field(path, "decision"))} differs from ${file}`);
}

export function loadApprovals(root, presets = PRESETS) {
  let bytes;
  try {
    bytes = readFileSync(join(root, APPROVALS));
  } catch (error) {
    if (error.code === "ENOENT") fail(`${APPROVALS} is missing`);
    fail(`${APPROVALS}: ${error.message}`);
  }
  const approvals = parse(bytes, APPROVALS);
  checkFields(approvals, APPROVALS, "", ["version", "entries"]);
  checkVersion(approvals, APPROVALS);
  check(Array.isArray(approvals.entries), APPROVALS, "entries");
  const views = Object.fromEntries(Object.keys(presets).map((preset) => [preset, { decisions: new Map(), theme: null }]));
  approvals.entries.forEach((entry, i) => {
    const path = `entries[${i}]`;
    const parsed = checkEntry(entry, path, presets);
    checkAgainstExport(entry, path, parsed, readExport(root, entry, path, presets));
    const view = views[parsed.preset];
    if (parsed.theme) view.theme = entry.decision;
    else view.decisions.set(parsed.kind, entry.decision);
  });
  const { decisions, theme } = views[DEFAULT_PRESET];
  return { presets: views, decisions, theme };
}
