// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// Notice coverage for third-party code in the explainer kit: every file under
// remotion-template/src/kit/ and remotion-template/bin/ carries an SPDX header;
// a file under any other licence names its source, commit and licence in a
// `Source:` line; NOTICE and THIRD-PARTY-LICENSES.md list each source with
// the same licence; and the licence text ships beside the code.
// Paths resolve from import.meta.url, so the result is the same from the
// repository root (CI) and from the template directory.

import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { basename, dirname, extname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const TEMPLATE = fileURLToPath(new URL("..", import.meta.url));
const REPO = fileURLToPath(new URL("../..", import.meta.url));
const SCAN_ROOTS = [join(TEMPLATE, "src", "kit"), join(TEMPLATE, "bin")];
const CODE_EXTENSIONS = new Set([".ts", ".tsx", ".mjs", ".js"]);
const OWN_LICENCE = "FSL-1.1-Apache-2.0";
const HEADER_LINES = 3;
const SPDX_LINE = /^\/\/ SPDX-License-Identifier: (\S+)$/;
const SOURCE_PREFIX = "// Source:";
const SOURCE_LINE = /^\/\/ Source: ([^\s/]+\/[^\s/]+) ([0-9a-f]{40}) (\S+) (\S+)$/;
// Required Notice lines of sources whose formats or ideas the kit borrows.
// A source without a Source line (video-talkcraft) is protected only here.
const REQUIRED_NOTICES = [
  {
    notice: "Required Notice: Copyright (c) 2026 Vincent Wei (https://github.com/Vincentwei1021/anything2explainer)",
    commit: "4f738189d866a8a1d1abd008456303617279a2c2",
    licence: "PolyForm-Noncommercial-1.0.0",
  },
  {
    notice: "Required Notice: Copyright (c) 2026 Vincent Wei (https://github.com/Vincentwei1021/video-talkcraft)",
    commit: "e19acbd958e45dbe0773817cffdf5b0e7646bf1c",
    licence: "PolyForm-Noncommercial-1.0.0",
  },
];
const NOTICE_REPO = /\(https:\/\/github\.com\/([^\s/]+\/[^\s/)]+)\)$/;

// A missing root scans as empty: before any vendoring there is no src/kit/,
// and the "some file names a source" test is what fails then.
function walk(dir) {
  if (!existsSync(dir)) return [];
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    return entry.isDirectory() ? walk(path) : [path];
  });
}

function scanned() {
  return SCAN_ROOTS.flatMap((root) => walk(root).map((path) => ({ root, path })));
}

function rel(path) {
  return relative(REPO, path);
}

// Parses the first HEADER_LINES lines; returns the header or a list of errors.
function readHeader(path) {
  const lines = readFileSync(path, "utf8").split("\n").slice(0, HEADER_LINES);
  const errors = [];
  const spdx = lines.map((line) => line.match(SPDX_LINE)).find(Boolean);
  if (!spdx) errors.push(`${rel(path)}: no SPDX-License-Identifier line in its first ${HEADER_LINES} lines`);
  const sourceLine = lines.find((line) => line.startsWith(SOURCE_PREFIX));
  let source = null;
  if (sourceLine !== undefined) {
    const match = sourceLine.match(SOURCE_LINE);
    if (match) {
      source = { repo: match[1], sha: match[2], path: match[3], licence: match[4] };
    } else {
      errors.push(`${rel(path)}: malformed Source line ${JSON.stringify(sourceLine)}`);
    }
  }
  return { spdx: spdx ? spdx[1] : null, source, line3: lines[2] ?? "", errors };
}

function codeFiles() {
  return scanned().filter(({ path }) => CODE_EXTENSIONS.has(extname(path)));
}

function thirdPartyFiles() {
  return codeFiles()
    .map((file) => ({ ...file, header: readHeader(file.path) }))
    .filter(({ header }) => header.spdx !== null && header.spdx !== OWN_LICENCE);
}

function escapeRegExp(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// A licence matches only as a whole token, so FSL-1.1-Apache-2.0 is not Apache-2.0.
function hasLicenceToken(text, licence) {
  return new RegExp(`(?<![A-Za-z0-9.-])${escapeRegExp(licence)}(?![A-Za-z0-9.-])`).test(text);
}

function sourceTriples() {
  const triples = new Map();
  for (const { path } of codeFiles()) {
    const { source } = readHeader(path);
    if (!source) continue;
    const triple = { repo: source.repo, sha8: source.sha.slice(0, 8), licence: source.licence };
    triples.set(`${triple.repo} ${triple.sha8} ${triple.licence}`, triple);
  }
  return [...triples.values()];
}

function nearestLicence(root, fileDir) {
  if (existsSync(join(fileDir, "LICENSE"))) return join(fileDir, "LICENSE");
  for (let dir = dirname(fileDir); dir.startsWith(root + "/"); dir = dirname(dir)) {
    if (existsSync(join(dir, "LICENSE"))) return join(dir, "LICENSE");
  }
  return null;
}

test("every kit and bin file carries a valid licence header", () => {
  const errors = [];
  for (const { path } of scanned()) {
    if (CODE_EXTENSIONS.has(extname(path))) {
      const header = readHeader(path);
      errors.push(...header.errors);
      if (header.spdx !== null && header.spdx !== OWN_LICENCE) {
        if (!header.source) {
          errors.push(`${rel(path)}: SPDX ${header.spdx} is not ${OWN_LICENCE} but there is no Source line`);
        } else if (header.source.licence !== header.spdx) {
          errors.push(`${rel(path)}: SPDX ${header.spdx} differs from Source licence ${header.source.licence}`);
        }
      }
    } else if (basename(path) !== "LICENSE") {
      errors.push(`${rel(path)}: not a .ts, .tsx, .mjs or .js file and not a LICENSE`);
    }
  }
  assert.deepEqual(errors, []);
});

test("some kit or bin file names a third-party source", () => {
  assert.ok(sourceTriples().length > 0, "no file under src/kit/ or bin/ carries a Source line");
});

test("NOTICE and THIRD-PARTY-LICENSES.md list each source once with its licence", () => {
  const paragraphs = readFileSync(join(REPO, "NOTICE"), "utf8").split(/\n[ \t]*\n/);
  const rows = readFileSync(join(REPO, "THIRD-PARTY-LICENSES.md"), "utf8")
    .split("\n")
    .filter((line) => line.trimStart().startsWith("|"));
  const errors = [];
  for (const { repo, sha8, licence } of sourceTriples()) {
    const entries = [
      ["NOTICE paragraph", paragraphs],
      ["THIRD-PARTY-LICENSES.md row", rows],
    ];
    for (const [kind, candidates] of entries) {
      const matching = candidates.filter((text) => text.includes(repo));
      if (matching.length !== 1) {
        errors.push(`${repo}: expected exactly one ${kind} naming it, found ${matching.length}`);
        continue;
      }
      if (!matching[0].includes(sha8)) errors.push(`${repo}: ${kind} lacks commit prefix ${sha8}`);
      if (!hasLicenceToken(matching[0], licence)) errors.push(`${repo}: ${kind} lacks licence ${licence}`);
    }
  }
  assert.deepEqual(errors, []);
});

test("the licence text ships beside every third-party file", () => {
  const errors = [];
  for (const { root, path, header } of thirdPartyFiles()) {
    const licencePath = nearestLicence(root, dirname(path));
    if (!licencePath) {
      errors.push(`${rel(path)}: no LICENSE in its directory or an ancestor below ${rel(root)}`);
      continue;
    }
    const text = readFileSync(licencePath, "utf8");
    const copyright = header.line3.replace(/^\/\/ /, "").split(". ")[0];
    if (!copyright || !text.includes(copyright)) {
      errors.push(`${rel(path)}: ${rel(licencePath)} lacks ${JSON.stringify(copyright)}`);
    }
    if (header.spdx === "MIT" && !text.includes("Permission is hereby granted")) {
      errors.push(`${rel(path)}: ${rel(licencePath)} lacks the MIT permission grant`);
    }
  }
  assert.deepEqual(errors, []);
});

test("every Required Notice appears once in NOTICE, in its project's paragraph and row", () => {
  const notice = readFileSync(join(REPO, "NOTICE"), "utf8");
  const paragraphs = notice.split(/\n[ \t]*\n/);
  const rows = readFileSync(join(REPO, "THIRD-PARTY-LICENSES.md"), "utf8")
    .split("\n")
    .filter((line) => line.trimStart().startsWith("|"));
  const errors = [];
  for (const { notice: line, commit, licence } of REQUIRED_NOTICES) {
    const repo = line.match(NOTICE_REPO)[1];
    const count = notice.split("\n").filter((text) => text.trim() === line).length;
    if (count !== 1) errors.push(`${repo}: expected its Required Notice once as a NOTICE line, found ${count}`);
    const paragraph = paragraphs.filter((text) => text.includes(repo));
    if (paragraph.length !== 1) {
      errors.push(`${repo}: expected exactly one NOTICE paragraph naming it, found ${paragraph.length}`);
    } else {
      if (!paragraph[0].split("\n").some((text) => text.trim() === line)) {
        errors.push(`${repo}: its NOTICE paragraph lacks the Required Notice line`);
      }
      if (!paragraph[0].includes(commit)) errors.push(`${repo}: NOTICE paragraph lacks commit ${commit}`);
      if (!hasLicenceToken(paragraph[0], licence)) errors.push(`${repo}: NOTICE paragraph lacks licence ${licence}`);
    }
    const row = rows.filter((text) => text.includes(repo));
    if (row.length !== 1) {
      errors.push(`${repo}: expected exactly one THIRD-PARTY-LICENSES.md row naming it, found ${row.length}`);
    } else {
      if (!row[0].includes(commit.slice(0, 8))) errors.push(`${repo}: row lacks commit prefix ${commit.slice(0, 8)}`);
      if (!hasLicenceToken(row[0], licence)) errors.push(`${repo}: row lacks licence ${licence}`);
      if (!row[0].includes(line)) errors.push(`${repo}: row lacks its Required Notice`);
    }
  }
  assert.deepEqual(errors, []);
});
