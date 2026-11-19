// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// gallery/approvals.json, version 1 (task 4.1): the user's gallery decisions,
// each entry linking the user's own page export, committed unmodified under
// gallery/exports/. loadApprovals(root) reads it without side effects and
// returns {decisions: Map<id, decision>, theme}. A later entry for an id
// replaces an earlier one; `redo` stays `redo`, which is not `approve`.
// Every entry is checked against its export: the link (pattern, a regular
// file by lstat, the digest), the export's own shape, and the reviewer,
// date and decision it records. Anything else throws
// `kit gallery: approvals: <problem>`.

import { createHash } from "node:crypto";
import { lstatSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { isCandidateId } from "../src/kit/gallery/catalog-rules.ts";

const APPROVALS = "gallery/approvals.json";
const EXPORT_SOURCE = "^gallery/exports/[A-Za-z0-9._-]+\\.json$";
const EXPORT_PATTERN = new RegExp(EXPORT_SOURCE);
const THEME = "theme";
const DECISIONS = ["approve", "redo", "drop"];
const THEMES = ["light", "dark"];
const SHA256 = /^[0-9a-f]{64}$/;
const DATE = /^([0-9]{4})-([0-9]{2})-([0-9]{2})$/;
const ENTRY_FIELDS = ["id", "decision", "reviewer", "date", "export", "export_sha256"];
const EXPORT_FIELDS = ["version", "page", "reviewer", "date", "decisions", "theme"];

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

function checkEntry(entry, path) {
  checkFields(entry, APPROVALS, path, ENTRY_FIELDS);
  check(entry.id === THEME || isCandidateId(entry.id), APPROVALS, field(path, "id"));
  check((entry.id === THEME ? THEMES : DECISIONS).includes(entry.decision), APPROVALS, field(path, "decision"));
  check(isText(entry.reviewer), APPROVALS, field(path, "reviewer"));
  check(isDate(entry.date), APPROVALS, field(path, "date"));
  if (typeof entry.export !== "string" || !EXPORT_PATTERN.test(entry.export)) {
    fail(`${at(APPROVALS, field(path, "export"))} must match ${EXPORT_SOURCE}`);
  }
  check(typeof entry.export_sha256 === "string" && SHA256.test(entry.export_sha256), APPROVALS, field(path, "export_sha256"));
}

function checkExport(value, file) {
  checkFields(value, file, "", EXPORT_FIELDS);
  checkVersion(value, file);
  check(typeof value.page === "string" && SHA256.test(value.page), file, "page");
  check(isText(value.reviewer), file, "reviewer");
  check(isDate(value.date), file, "date");
  check(Array.isArray(value.decisions), file, "decisions");
  value.decisions.forEach((decision, i) => {
    const path = `decisions[${i}]`;
    checkFields(decision, file, path, ["id", "decision"], ["note"]);
    check(isCandidateId(decision.id), file, `${path}.id`);
    check(DECISIONS.includes(decision.decision), file, `${path}.decision`);
    if (Object.hasOwn(decision, "note")) check(isText(decision.note), file, `${path}.note`);
  });
  check(value.theme === null || THEMES.includes(value.theme), file, "theme");
}

// The export an entry links: a regular file (lstat, so not a symlink) whose
// digest is the entry's, in the export format.
function readExport(root, entry, path) {
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
  checkExport(value, file);
  return value;
}

function checkAgainstExport(entry, path, exported) {
  const file = entry.export;
  for (const key of ["reviewer", "date"]) {
    if (entry[key] !== exported[key]) fail(`${at(APPROVALS, field(path, key))} differs from ${file}`);
  }
  if (entry.id === THEME) {
    if (exported.theme === null) fail(`${at(APPROVALS, path)}: ${file} has theme null`);
    if (entry.decision !== exported.theme) fail(`${at(APPROVALS, field(path, "decision"))} differs from ${file}'s theme`);
    return;
  }
  const decided = exported.decisions.find(({ id }) => id === entry.id);
  if (decided === undefined) fail(`${at(APPROVALS, field(path, "id"))} ${JSON.stringify(entry.id)} is not in ${file}`);
  if (decided.decision !== entry.decision) fail(`${at(APPROVALS, field(path, "decision"))} differs from ${file}`);
}

export function loadApprovals(root) {
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
  const decisions = new Map();
  let theme = null;
  approvals.entries.forEach((entry, i) => {
    const path = `entries[${i}]`;
    checkEntry(entry, path);
    checkAgainstExport(entry, path, readExport(root, entry, path));
    if (entry.id === THEME) theme = entry.decision;
    else decisions.set(entry.id, entry.decision);
  });
  return { decisions, theme };
}
