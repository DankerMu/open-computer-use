// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// `kit data` for CSV (task 2.1, issue #958), driven through run(argv, io)
// with temporary directories for videosRoot, filesRoot and a copy of the
// template, and a real python3 running bin/read_table.py. Expected texts,
// exit codes and the table format come from the task 2.1 fixture; the
// committed fixture table was written by hand and its sha256 taken with
// shasum.

import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { run } from "../bin/kit.mjs";

const TEMPLATE = fileURLToPath(new URL("..", import.meta.url)).replace(/\/$/, "");
const FIXTURES = join(TEMPLATE, "tests", "fixtures", "data");
const LEFT_OUT = new Set(["node_modules", "tests", "out", "gallery", ".kit"]);
const USAGE = 'kit data: usage: node bin/kit.mjs data "<path>" [--sheet <name>]\n';

// --- sandbox ----------------------------------------------------------------

// A template copy whose node_modules holds only a link to zod, an empty files
// root, and a project made by `new`.
async function project(t) {
  const base = mkdtempSync(join(tmpdir(), "kit-data-test-"));
  t.after(() => rmSync(base, { recursive: true, force: true }));
  const template = join(base, "template");
  cpSync(TEMPLATE, template, {
    recursive: true,
    filter: (src) => !LEFT_OUT.has(relative(TEMPLATE, src).split(sep)[0]),
  });
  mkdirSync(join(template, "node_modules"));
  symlinkSync(join(TEMPLATE, "node_modules", "zod"), join(template, "node_modules", "zod"));
  const files = join(base, "files");
  mkdirSync(files);
  const box = { base, template, videos: join(base, "videos"), files };
  const made = await kit(["new", "demo"], template, box);
  assert.equal(made.code, 0, made.err);
  const root = join(box.videos, "demo");
  const put = (name, content) => writeFileSync(join(files, name), content);
  const data = (...args) => kit(["data", ...args], root, box);
  return { ...box, root, dataDir: join(root, "data"), put, data };
}

async function kit(argv, root, box, runner = run, extra = {}) {
  let out = "";
  let err = "";
  const code = await runner(argv, {
    ...extra,
    root,
    videosRoot: box.videos,
    filesRoot: box.files,
    stdout: { write: (text) => (out += text) },
    stderr: { write: (text) => (err += text) },
  });
  return { code, out, err };
}

const readTable = (box, name) => JSON.parse(readFileSync(join(box.dataDir, `${name}.json`), "utf8"));
const csv = (rows) => `${rows.join("\n")}\n`;

// Asserts a refused import: exit code, no stdout, the exact stderr line and
// no data/ directory.
function assertRefused(box, result, line, code = 1) {
  assert.deepEqual(result, { code, out: "", err: `kit data: ${line}\n` });
  assert.equal(existsSync(box.dataDir), false, "data/ was created");
}

// Sets process.env[name] for one test.
function setEnv(t, name, value) {
  const before = process.env[name];
  process.env[name] = value;
  t.after(() => {
    if (before === undefined) delete process.env[name];
    else process.env[name] = before;
  });
}

// --- the committed fixture --------------------------------------------------

test("the committed fixture imports to the committed table", async (t) => {
  const box = await project(t);
  box.put("q3 销售.csv", readFileSync(join(FIXTURES, "q3 销售.csv")));
  const result = await box.data("q3 销售.csv");
  assert.equal(result.code, 0, result.err);
  assert.equal(result.err, "kit data: wrote data/q3-销售.json\n");
  assert.equal(
    result.out,
    [
      "q3-销售",
      "地区\t销售额\t完成率\t备注",
      "华东\t630\t93%\t重点区域",
      "华南\t250\t87.5%\t",
      "华北\t180\t78.0%\t新增门店",
      "华中\t120\t65%\t同比下降",
      "西南\t80\t101%\t",
      "",
    ].join("\n"),
  );
  assert.ok(result.out.split("\n")[1].startsWith("地区"));
  const written = readFileSync(join(box.dataDir, "q3-销售.json"));
  assert.deepEqual(written, readFileSync(join(FIXTURES, "q3-销售.json")));
  const table = JSON.parse(written.toString("utf8"));
  assert.equal(table.label_header, "地区");
  assert.equal(table.label_header.includes("﻿"), false);
  assert.deepEqual(readdirSync(box.dataDir), ["q3-销售.json"]);
});

// --- naming -----------------------------------------------------------------

test("table names come from the file stem", async (t) => {
  const box = await project(t);
  const body = csv(["a,b", "x,1"]);
  // Astral Han code points: a cut by UTF-16 units would differ.
  const long = `abcde${"𠀀".repeat(40)}`;
  for (const [file, name] of [
    ["q3 销售.csv", "q3-销售"],
    ["Sales  Q3!.csv", "sales-q3"],
    [`${long}.csv`, `abcde${"𠀀".repeat(35)}`],
    ["Upper.CSV", "upper"],
  ]) {
    box.put(file, body);
    const result = await box.data(file);
    assert.equal(result.code, 0, `${file}: ${result.err}`);
    assert.equal(result.out.split("\n")[0], name);
    assert.equal(result.err, `kit data: wrote data/${name}.json\n`);
    assert.ok(existsSync(join(box.dataDir, `${name}.json`)), file);
  }
  assert.equal([..."abcde" + "𠀀".repeat(35)].length, 40);
});

test("a file name that gives an empty table name is refused", async (t) => {
  const box = await project(t);
  box.put("!!!.csv", csv(["a,b", "x,1"]));
  assertRefused(box, await box.data("!!!.csv"), "!!!.csv: the file name gives an empty table name");
});

// --- numbers ----------------------------------------------------------------

test("cells parse as numbers with commas and %, and everything else is text", async (t) => {
  const box = await project(t);
  box.put(
    "n.csv",
    csv([
      "项目,值",
      'a,"1,260"',
      "b,93%",
      "c,-5.50%",
      "d,0.5",
      'e,"1,26"',
      "f,12.",
      "g,007",
      "h,１２",
      "i,-0",
      "j,123456789012345",
      'k,"12,345,678.25"',
      "l,1.5.5",
      "m,-",
    ]),
  );
  const result = await box.data("n.csv");
  assert.equal(result.code, 0, result.err);
  const cells = Object.fromEntries(readTable(box, "n").rows.map((row) => [row.label, row.cells[0]]));
  const text = (cell) => ({ value: null, text: cell, unit: "" });
  assert.deepEqual(cells, {
    a: { value: 1260, text: "1260", unit: "" },
    b: { value: 93, text: "93", unit: "%" },
    c: { value: -5.5, text: "-5.50", unit: "%" },
    d: { value: 0.5, text: "0.5", unit: "" },
    e: text("1,26"),
    f: text("12."),
    g: text("007"),
    h: text("１２"),
    i: { value: 0, text: "-0", unit: "" },
    j: { value: 123456789012345, text: "123456789012345", unit: "" },
    k: { value: 12345678.25, text: "12345678.25", unit: "" },
    l: text("1.5.5"),
    m: text("-"),
  });
  // -0 is written as 0, not -0.
  assert.match(readFileSync(join(box.dataDir, "n.json"), "utf8"), /"value": 0,\n\s+"text": "-0"/);
});

test("a number with more than 15 significant digits is refused, naming the cell", async (t) => {
  const box = await project(t);
  box.put("big.csv", csv(["项目,销售额", "a,1", "b,1234567890123456"]));
  assertRefused(box, await box.data("big.csv"), "big.csv: row 3, column 销售额: more than 15 significant digits");
});

// --- table refusals ---------------------------------------------------------

test("each table refusal gives its text, exit 1 and no data/", async (t) => {
  const box = await project(t);
  const header13 = Array.from({ length: 13 }, (_, i) => `c${i + 1}`).join(",");
  const cases = [
    ["blank.csv", ",,\n\n", "blank.csv: has no header row"],
    ["empty.csv", "", "empty.csv: has no header row"],
    ["one.csv", csv(["地区", "华东"]), "one.csv: the header needs a label column and at least one data column"],
    ["wide.csv", csv([header13, `a${",1".repeat(12)}`]), "wide.csv: has 13 columns; the limit is 12"],
    ["gap.csv", csv(["地区,销售额,,备注", "华东,1,2,3"]), "gap.csv: column 3 has an empty header"],
    ["dup.csv", csv(["地区,销售额,销售额", "华东,1,2"]), "dup.csv: duplicate header 销售额 in columns 2 and 3"],
    ["head.csv", csv(["地区,销售额"]), "head.csv: has no data row"],
    ["long.csv", csv(["地区,a,b", "x,1,2", "y,1,2", "z,1,2,3"]), "long.csv: row 4 has 4 cells; the header has 3"],
    ["nolabel.csv", csv(["地区,a", "x,1", "y,1", "z,1", " ,1"]), "nolabel.csv: row 5 has an empty label"],
    ["twice.csv", csv(["地区,a", "华东,1", "华南,1", "华北,1", "华东,2"]), "twice.csv: row 5: duplicate label 华东, first in row 2"],
    ["latin.csv", Buffer.from("Région,Ventes\nÎle,1\n", "latin1"), "latin.csv: is not UTF-8; save it as CSV UTF-8"],
  ];
  for (const [file, content, line] of cases) {
    box.put(file, content);
    assertRefused(box, await box.data(file), line);
  }
});

test("50 data rows and 12 columns import; 51 rows and 13 columns are refused", async (t) => {
  const box = await project(t);
  const rows = (n) => ["地区,a", ...Array.from({ length: n }, (_, i) => `r${i + 1},${i}`)];
  box.put("rows51.csv", csv(rows(51)));
  assertRefused(box, await box.data("rows51.csv"), "rows51.csv: has 51 data rows; the limit is 50");
  const header = (n) => Array.from({ length: n }, (_, i) => `c${i + 1}`).join(",");
  box.put("cols13.csv", csv([header(13), `x${",1".repeat(12)}`]));
  assertRefused(box, await box.data("cols13.csv"), "cols13.csv: has 13 columns; the limit is 12");
  box.put("rows50.csv", csv(rows(50)));
  assert.equal((await box.data("rows50.csv")).code, 0);
  assert.equal(readTable(box, "rows50").rows.length, 50);
  box.put("cols12.csv", csv([header(12), `x${",1".repeat(11)}`]));
  assert.equal((await box.data("cols12.csv")).code, 0);
  assert.equal(readTable(box, "cols12").columns.length, 11);
});

test("a duplicate label after a skipped empty line names the file row", async (t) => {
  const box = await project(t);
  box.put("skip.csv", "地区,a\n华东,1\n\n华东,2\n");
  assertRefused(box, await box.data("skip.csv"), "skip.csv: row 4: duplicate label 华东, first in row 2");
});

// --- malformed and large input ---------------------------------------------

test("an unbalanced quote is refused at its record and writes nothing", async (t) => {
  const box = await project(t);
  box.put("open.csv", 'a,b\nx,"5 region\ny,2\nz,3\n');
  assertRefused(box, await box.data("open.csv"), "open.csv: row 2: a quoted cell is not closed");
  box.put("after.csv", 'a,b\nx,1\ny,"5"z\n');
  assertRefused(box, await box.data("after.csv"), `after.csv: row 3: ',' expected after '"'`);
});

test("legal quoting still imports", async (t) => {
  const box = await project(t);
  box.put("q.csv", 'a,b\nx,5"\ny,"two\nlines"\nz,"a ""b"" c"\nw,"1,260"\n');
  const result = await box.data("q.csv");
  assert.equal(result.code, 0, result.err);
  const text = (cell) => ({ value: null, text: cell, unit: "" });
  assert.deepEqual(readTable(box, "q").rows, [
    { label: "x", cells: [text('5"')] },
    { label: "y", cells: [text("two\nlines")] },
    { label: "z", cells: [text('a "b" c')] },
    { label: "w", cells: [{ value: 1260, text: "1260", unit: "" }] },
  ]);
});

test("a CRLF file imports to the same table as its LF twin", async (t) => {
  const box = await project(t);
  // Long CJK cells of varying length put line ends and multibyte characters
  // across the reader's chunk boundaries.
  const lines = ["地区,销售额,备注", ...Array.from({ length: 49 }, (_, i) => `r${i + 1},"1,${String(i).padStart(3, "0")}",${"华".repeat(300 + 7 * i)}`)];
  lines.splice(10, 0, "");
  const lf = Buffer.from(`\uFEFF${lines.join("\n")}\n`, "utf8");
  const crlf = Buffer.from(`\uFEFF${lines.join("\r\n")}\r\n`, "utf8");
  assert.ok(lf.length > 8 * 8192);
  const imported = [];
  for (const [dir, bytes] of [["lf", lf], ["crlf", crlf]]) {
    mkdirSync(join(box.files, dir));
    writeFileSync(join(box.files, dir, "t.csv"), bytes);
    const result = await box.data(`${dir}/t.csv`);
    assert.equal(result.code, 0, result.err);
    const table = readTable(box, "t");
    assert.equal(table.source.sha256, createHash("sha256").update(bytes).digest("hex"));
    imported.push({ out: result.out, table: { ...table, source: { ...table.source, sha256: null } } });
  }
  assert.deepEqual(imported[1], imported[0]);
  assert.equal(imported[0].table.rows.length, 49);
  assert.equal(imported[0].table.rows[48].cells[1].text, "华".repeat(300 + 7 * 48));
});

test("a file of 200,000 short rows is refused with its full row count", async (t) => {
  const box = await project(t);
  box.put("big.csv", csv(["a,b", ...Array.from({ length: 200000 }, (_, i) => `r${i},1`)]));
  assertRefused(box, await box.data("big.csv"), "big.csv: has 200000 data rows; the limit is 50");
});

test("a Latin-1 byte after row 51 is still refused as not UTF-8", async (t) => {
  const box = await project(t);
  // Long rows put the byte well past the reader's first chunks.
  const head = Buffer.from(csv(["地区,a", ...Array.from({ length: 58 }, (_, i) => `r${i + 2},${"x".repeat(2000)}`)]), "utf8");
  assert.ok(head.length > 100000);
  // File row 60.
  box.put("late.csv", Buffer.concat([head, Buffer.from("Île,1\n", "latin1")]));
  assertRefused(box, await box.data("late.csv"), "late.csv: is not UTF-8; save it as CSV UTF-8");
});

// --- path refusals ----------------------------------------------------------

test("paths outside the files root, symlinks out, directories, absent files and other types are refused", async (t) => {
  const box = await project(t);
  writeFileSync(join(box.base, "x.csv"), "a,b\nx,1\n");
  symlinkSync(join(box.base, "x.csv"), join(box.files, "link.csv"));
  mkdirSync(join(box.files, "dir.csv"));
  box.put("q3.xlsx", "PK");
  box.put("t.txt", "a,b\nx,1\n");
  for (const [arg, line] of [
    ["../x.csv", `../x.csv: is outside ${box.files}`],
    [join(box.base, "x.csv"), `${join(box.base, "x.csv")}: is outside ${box.files}`],
    ["link.csv", `link.csv: is outside ${box.files}`],
    ["dir.csv", "dir.csv: is not a regular file"],
    ["absent.csv", "absent.csv: not found"],
    ["q3.xlsx", "q3.xlsx: is not a .csv file"],
    ["t.txt", "t.txt: is not a .csv file"],
  ]) {
    assertRefused(box, await box.data(arg), line);
  }
  assertRefused(box, await box.data("q3.xlsx", "--sheet", "Sheet1"), "q3.xlsx: is not a .csv file");
});

test("--sheet on a CSV is refused", async (t) => {
  const box = await project(t);
  box.put("a.csv", "a,b\nx,1\n");
  assertRefused(box, await box.data("a.csv", "--sheet", "Sheet1"), "--sheet applies only to .xlsx files");
  assertRefused(box, await box.data("--sheet", "Sheet1", "a.csv"), "--sheet applies only to .xlsx files");
});

test("data/ as a symlink or a file is refused and left as it is", async (t) => {
  const box = await project(t);
  box.put("a.csv", "a,b\nx,1\n");
  const elsewhere = join(box.base, "elsewhere");
  mkdirSync(elsewhere);
  symlinkSync(elsewhere, box.dataDir);
  assert.deepEqual(await box.data("a.csv"), { code: 1, out: "", err: `kit data: ${box.dataDir}: is a symlink\n` });
  assert.deepEqual(readdirSync(elsewhere), []);
  rmSync(box.dataDir);
  writeFileSync(box.dataDir, "file");
  assert.deepEqual(await box.data("a.csv"), { code: 1, out: "", err: `kit data: ${box.dataDir}: is not a directory\n` });
  assert.equal(readFileSync(box.dataDir, "utf8"), "file");
});

// --- isolation --------------------------------------------------------------

test("planted modules in the working directory, beside the input and on PYTHONPATH are ignored", async (t) => {
  const box = await project(t);
  const planted = 'import sys\nsys.stderr.write("planted module ran\\n")\nsys.exit(9)\n';
  const cwd = join(box.base, "cwd");
  const path = join(box.base, "pythonpath");
  for (const dir of [cwd, path]) {
    mkdirSync(dir);
    for (const name of ["csv.py", "json.py", "hashlib.py", "re.py"]) writeFileSync(join(dir, name), planted);
  }
  for (const name of ["csv.py", "json.py"]) box.put(name, planted);
  box.put("a.csv", "a,b\nx,1\n");
  const before = process.cwd();
  process.chdir(cwd);
  t.after(() => process.chdir(before));
  setEnv(t, "PYTHONPATH", path);
  const result = await box.data("a.csv");
  assert.equal(result.code, 0, result.err);
  assert.equal(result.err, "kit data: wrote data/a.json\n");
  assert.deepEqual(readTable(box, "a").columns, ["b"]);
});

// --- reader failures --------------------------------------------------------

test("a misbehaving reader fails the step and writes nothing", async (t) => {
  for (const [stub, reason] of [
    ['raise RuntimeError("boom")\n', "RuntimeError: boom"],
    ["import sys\nsys.exit(3)\n", "python3 exited with 3"],
    ['import sys\nsys.stderr.write("oops\\n")\nsys.exit(1)\n', "oops"],
    ['print("not json")\n', "bad output"],
    ['print(\'{"version": 2}\')\n', "bad output"],
    ['print("[]")\n', "bad output"],
  ]) {
    const box = await project(t);
    box.put("a.csv", "a,b\nx,1\n");
    // The project's bin/ is the test-only copy: its kit.mjs runs the
    // read_table.py beside it.
    writeFileSync(join(box.root, "bin", "read_table.py"), stub);
    const { run: projectRun } = await import(pathToFileURL(join(box.root, "bin", "kit.mjs")).href);
    const result = await kit(["data", "a.csv"], box.root, box, projectRun);
    assertRefused(box, result, `the table reader failed: ${reason}`);
  }
});

test("a reader still running at the timeout is killed and writes nothing", async (t) => {
  const box = await project(t);
  box.put("a.csv", "a,b\nx,1\n");
  // The stub ignores SIGTERM, so only a hard kill ends it before it sleeps out.
  writeFileSync(join(box.root, "bin", "read_table.py"), "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(5)\n");
  const { run: projectRun } = await import(pathToFileURL(join(box.root, "bin", "kit.mjs")).href);
  const started = Date.now();
  const result = await kit(["data", "a.csv"], box.root, box, projectRun, { readerTimeoutMs: 200 });
  assertRefused(box, result, "the table reader failed: timed out after 0.2 seconds");
  assert.ok(Date.now() - started < 4000, "the reader was not killed at the timeout");
});

test("a PATH without python3 fails the step and writes nothing", async (t) => {
  const box = await project(t);
  box.put("a.csv", "a,b\nx,1\n");
  const empty = join(box.base, "empty-path");
  mkdirSync(empty);
  setEnv(t, "PATH", empty);
  assertRefused(box, await box.data("a.csv"), "the table reader failed: python3 not found");
});

// --- re-import --------------------------------------------------------------

test("re-importing replaces the table and says so", async (t) => {
  const box = await project(t);
  box.put("a.csv", "a,b\nx,1\n");
  assert.equal((await box.data("a.csv")).err, "kit data: wrote data/a.json\n");
  box.put("a.csv", "a,b\nx,2\n");
  const again = await box.data("a.csv");
  assert.equal(again.code, 0);
  assert.equal(again.err, "kit data: replaced data/a.json\n");
  assert.equal(readTable(box, "a").rows[0].cells[0].text, "2");
  assert.deepEqual(readdirSync(box.dataDir), ["a.json"]);
});

// --- option parsing ---------------------------------------------------------

test("data usage errors exit 2 with the exact usage line", async (t) => {
  const box = await project(t);
  box.put("a.csv", "a,b\nx,1\n");
  for (const args of [
    [],
    ["a.csv", "b.csv"],
    ["--bogus"],
    ["a.csv", "--bogus"],
    ["a.csv", "--sheet"],
    ["a.csv", "--sheet", "x", "--sheet", "y"],
    ["--", "a.csv", "b.csv"],
  ]) {
    assert.deepEqual(await box.data(...args), { code: 2, out: "", err: USAGE }, args.join(" "));
  }
  assert.equal(existsSync(box.dataDir), false);
});

test("a path starting with - is a positional, with or without --", async (t) => {
  const box = await project(t);
  box.put("-x.csv", "a,b\nx,1\n");
  for (const args of [["--", "-x.csv"], ["-x.csv"]]) {
    const result = await box.data(...args);
    assert.equal(result.code, 0, result.err);
    assert.equal(result.out.split("\n")[0], "-x");
  }
  assert.deepEqual(readdirSync(box.dataDir), ["-x.json"]);
});

test("commands without options keep their argument handling", async (t) => {
  const box = await project(t);
  assert.deepEqual(await kit(["voice", "--sheet", "x"], box.root, box), {
    code: 2,
    out: "",
    err: "kit voice: usage: node bin/kit.mjs voice\n",
  });
  box.put("-x.png", "png");
  assert.deepEqual(await kit(["image", "-x.png"], box.root, box), { code: 0, out: "x.png\n", err: "" });
  assert.deepEqual(await kit(["image", "--", "-x.png"], box.root, box), {
    code: 2,
    out: "",
    err: 'kit image: usage: node bin/kit.mjs image "<path>"\n',
  });
});

test("the usage list keeps every existing line and gains data", async (t) => {
  const box = await project(t);
  const none = await kit([], box.template, box);
  assert.equal(none.code, 2);
  assert.equal(
    none.err,
    [
      "kit: usage: node /opt/remotion-template/bin/kit.mjs new <name>",
      "kit: usage: node bin/kit.mjs voice",
      "kit: usage: node bin/kit.mjs voice-import",
      'kit: usage: node bin/kit.mjs image "<path>"',
      'kit: usage: node bin/kit.mjs data "<path>" [--sheet <name>]',
      "kit: usage: node bin/kit.mjs timeline",
      "kit: usage: node bin/kit.mjs check",
      "kit: usage: node bin/kit.mjs render",
      "kit: usage: node bin/kit.mjs status",
      "",
    ].join("\n"),
  );
});

// --- template root ----------------------------------------------------------

test("data at the template root is refused with the project hint", async (t) => {
  const box = await project(t);
  box.put("a.csv", "a,b\nx,1\n");
  const result = await kit(["data", "a.csv"], box.template, box);
  assert.deepEqual(result, {
    code: 2,
    out: "",
    err: `kit data: run it inside a project: cd ${box.videos}/<name> && node bin/kit.mjs data\n`,
  });
  assert.equal(existsSync(join(box.template, "data")), false);
  // `new` copies the reader into the project.
  assert.deepEqual(readFileSync(join(box.root, "bin", "read_table.py")), readFileSync(join(TEMPLATE, "bin", "read_table.py")));
});
