// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// `kit data` for xlsx (task 2.2, issue #959), through the shared harness of
// kit-data.test.mjs and a real python3 with openpyxl running bin/read_table.py.
// Workbooks are generated per test by fixtures/data/make_xlsx.py, which
// rewrites the saved sheet and style XML where openpyxl cannot write a cell
// itself (saved formula values, exact stored floats, raw format ids, a
// data-validation extLst). Expected texts, exit codes, cell kinds and
// rounding come from the task 2.2 fixture; the CSV twin is checked against
// the committed CSV fixture table.

import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, readdirSync, symlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { assertRefused, FIXTURES, kit, project, readTable } from "./kit-data-harness.mjs";

const MAKE_XLSX = join(FIXTURES, "make_xlsx.py");
// An empty zip: only the end-of-central-directory record.
const EMPTY_ZIP = Buffer.concat([Buffer.from("PK\x05\x06"), Buffer.alloc(18)]);

// Writes a workbook from a make_xlsx.py spec into the files root.
function putXlsx(box, name, spec) {
  execFileSync("python3", ["-I", MAKE_XLSX, join(box.files, name)], { input: JSON.stringify(spec) });
}

// A one-sheet spec titled Sheet1.
const sheet = (rows, extra = {}) => ({ sheets: [{ title: "Sheet1", rows, ...extra }] });
const fmt = (value, format) => ({ value, format });
const num = (value, text, unit = "") => ({ value, text, unit });
const txt = (text) => ({ value: null, text, unit: "" });
const sha = (path) => createHash("sha256").update(readFileSync(path)).digest("hex");
const cellsByLabel = (table) => Object.fromEntries(table.rows.map((row) => [row.label, row.cells[0]]));

// Imports a one-value-column sheet holding `cell` in B2 and asserts the
// refusal `<file>: sheet Sheet1: <line>`.
async function assertCellRefused(box, file, cell, line) {
  putXlsx(box, file, sheet([["项目", "值"], ["a", cell]]));
  assertRefused(box, await box.data(file), `${file}: sheet Sheet1: ${line}`);
}

// --- the CSV twin -------------------------------------------------------------

test("an xlsx holding the committed CSV's cells imports to the same table", async (t) => {
  const box = await project(t);
  putXlsx(box, "q3 销售.xlsx", {
    sheets: [
      {
        title: "销售",
        rows: [
          ["地区", "销售额", "完成率", "备注"],
          ["华东", 630, fmt(0.93, "0%"), "重点区域"],
          ["华南", 250, fmt(0.875, "0.0%"), " "],
          ["华北", 180, fmt(0.78, "0.0%"), "新增门店"],
          [],
          ["华中", 120, fmt(0.65, "0%"), "  同比下降  "],
          ["西南", 80, fmt(1.01, "0%")],
          ["合计", fmt(1260, "#,##0"), fmt(0.93, "0%"), "五区合计"],
        ],
      },
    ],
  });
  const result = await box.data("q3 销售.xlsx");
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
  const table = readTable(box, "q3-销售");
  const twin = JSON.parse(readFileSync(join(FIXTURES, "q3-销售.json"), "utf8"));
  assert.deepEqual(
    { label_header: table.label_header, columns: table.columns, rows: table.rows },
    { label_header: twin.label_header, columns: twin.columns, rows: twin.rows },
  );
  assert.deepEqual(table.source, { file: "q3 销售.xlsx", sheet: "销售", sha256: sha(join(box.files, "q3 销售.xlsx")) });
  assert.deepEqual(Object.keys(table), ["version", "source", "label_header", "columns", "rows"]);
  assert.equal(readFileSync(join(box.dataDir, "q3-销售.json"), "utf8"), `${JSON.stringify(table, null, 2)}\n`);
});

// --- number formats -----------------------------------------------------------

test("each listed number format gives its value, text and unit", async (t) => {
  const box = await project(t);
  putXlsx(
    box,
    "f.xlsx",
    sheet([
      ["项目", "值"],
      ["general", 1260],
      // openpyxl itself would write %.16g, 0.3000000000000000.
      ["general-float", { value: 0, saved: "0.30000000000000004" }],
      ["zero-up", fmt(1260.6, "0")],
      ["zero-neg", fmt(-0.04, "0")],
      ["formula", { formula: "B2+0.3", saved: "1260.3000000000002", format: "0.0" }],
      ["two", fmt(0.5, "0.00")],
      ["two-half", fmt(2.675, "0.00")],
      ["thousands", fmt(1260, "#,##0")],
      ["thousands-two", fmt(1234.5, "#,##0.00")],
      ["thousands-one", fmt(1234.56, "#,##0.0")],
      ["percent", fmt(0.93, "0%")],
      ["percent-one", fmt(0.935, "0.0%")],
      ["percent-two", fmt(-0.055, "0.00%")],
      // Only a decimal x100 gives 101; a float gives 100.49999999999999.
      ["percent-half", fmt(1.005, "0%")],
    ]),
  );
  const result = await box.data("f.xlsx");
  assert.equal(result.code, 0, result.err);
  assert.deepEqual(cellsByLabel(readTable(box, "f")), {
    general: num(1260, "1260"),
    "general-float": num(0.3, "0.3"),
    "zero-up": num(1261, "1261"),
    "zero-neg": num(0, "0"),
    formula: num(1260.3, "1260.3"),
    two: num(0.5, "0.50"),
    "two-half": num(2.68, "2.68"),
    thousands: num(1260, "1260"),
    "thousands-two": num(1234.5, "1234.50"),
    "thousands-one": num(1234.6, "1234.6"),
    percent: num(93, "93", "%"),
    "percent-one": num(93.5, "93.5", "%"),
    "percent-two": num(-5.5, "-5.50", "%"),
    "percent-half": num(101, "101", "%"),
  });
  // The zero is written without a sign.
  assert.match(readFileSync(join(box.dataDir, "f.json"), "utf8"), /"value": 0,\n\s+"text": "0"/);
  // The formula fixture is a formula without data_only and its saved value with it.
  const inspected = execFileSync("python3", ["-I", MAKE_XLSX, "--inspect", join(box.files, "f.xlsx"), "Sheet1", "B6"], { encoding: "utf8" });
  assert.deepEqual(JSON.parse(inspected), { formula: ["=B2+0.3", "f"], saved: [1260.3000000000002, "n"] });
});

test("a number beyond 15 significant digits is refused, naming the cell", async (t) => {
  const box = await project(t);
  await assertCellRefused(box, "g.xlsx", 1e15, "cell B2: more than 15 significant digits");
  await assertCellRefused(box, "z.xlsx", fmt(1234567890123456, "0"), "cell B2: more than 15 significant digits");
  // A data string goes through the CSV grammar and its digit limit.
  await assertCellRefused(box, "str.xlsx", "1234567890123456", "cell B2: more than 15 significant digits");
});

test("other number formats are refused, naming the cell and the format", async (t) => {
  const box = await project(t);
  for (const [file, cell, line] of [
    ["e.xlsx", fmt(1260, "0.000E+00"), 'cell B2: number format "0.000E+00" is not supported'],
    ["s.xlsx", fmt(1260, "0;-0"), 'cell B2: number format "0;-0" is not supported'],
    ["tp.xlsx", fmt(0.5, "#,##0%"), 'cell B2: number format "#,##0%" is not supported'],
    ["at.xlsx", fmt(1260, "@"), 'cell B2: number format "@" is not supported'],
    ["r31.xlsx", { value: 1260, format_id: 31 }, "cell B2: built-in number format 31 is not supported"],
    ["r58.xlsx", { value: 1260, format_id: 58 }, "cell B2: built-in number format 58 is not supported"],
    // Not a format refusal: openpyxl 3.1.5 turns every number whose style is
    // a date format (custom, or a built-in id it names such as 14) into a
    // datetime while parsing, and a datetime is refused whatever its format.
    ["d.xlsx", fmt(45293, "yyyy-mm-dd"), "cell B2 holds a date or time"],
  ]) {
    await assertCellRefused(box, file, cell, line);
  }
});

// --- cell kinds ---------------------------------------------------------------

test("strings, saved formula values and a numeric header read as their kinds", async (t) => {
  const box = await project(t);
  putXlsx(
    box,
    "k.xlsx",
    sheet([
      ["地区", 2024],
      ["a", "1,260"],
      ["b", "  华东  "],
      ["1,260", "x"],
      ["c", { formula: '"东区"', type: "str", saved: "东区" }],
      ["d", { formula: '""', type: "str", saved: "" }],
    ]),
  );
  const result = await box.data("k.xlsx");
  assert.equal(result.code, 0, result.err);
  const table = readTable(box, "k");
  assert.equal(table.label_header, "地区");
  assert.deepEqual(table.columns, ["2024"]);
  assert.deepEqual(cellsByLabel(table), {
    a: num(1260, "1260"),
    b: txt("华东"),
    "1,260": txt("x"),
    c: txt("东区"),
    d: null,
  });
});

test("with fullCalcOnLoad set, a formula's saved value is stale and refused", async (t) => {
  const box = await project(t);
  // As xlsxwriter (and pandas through it) writes a formula: <v>0</v> and the flag.
  const rows = [["项目", "值"], ["a", 630], ["b", { formula: "B2*2", saved: "0" }]];
  putXlsx(box, "stale.xlsx", { full_calc_on_load: true, sheets: [{ title: "Sheet1", rows }] });
  assertRefused(
    box,
    await box.data("stale.xlsx"),
    "stale.xlsx: sheet Sheet1: cell B3 is a formula with no saved value; open the file in Excel and save it",
  );
});

test("with fullCalcOnLoad cleared, the same formula imports its saved value", async (t) => {
  const box = await project(t);
  const rows = [["项目", "值"], ["a", 630], ["b", { formula: "B2*2", saved: "0" }]];
  putXlsx(box, "kept.xlsx", { sheets: [{ title: "Sheet1", rows }] });
  const result = await box.data("kept.xlsx");
  assert.equal(result.code, 0, result.err);
  assert.deepEqual(cellsByLabel(readTable(box, "kept")), { a: num(630, "630"), b: num(0, "0") });
  // The helper's default stands for an Excel-saved file: calcPr without the
  // attribute, which openpyxl's wb.calculation.fullCalcOnLoad reports as True.
  const calc = execFileSync(
    "python3",
    [
      "-I",
      "-c",
      "import openpyxl, re, sys, zipfile; path = sys.argv[1]\n"
        + "print('fullCalcOnLoad' in re.search(r'<calcPr[^>]*>', zipfile.ZipFile(path).read('xl/workbook.xml').decode()).group(0))\n"
        + "print(openpyxl.load_workbook(path).calculation.fullCalcOnLoad)",
      join(box.files, "kept.xlsx"),
    ],
    { encoding: "utf8" },
  );
  assert.equal(calc, "False\nTrue\n");
});

test("formulas without a saved value, errors, booleans and dates are refused, naming the cell", async (t) => {
  const box = await project(t);
  for (const [file, cell, line] of [
    ["nf.xlsx", "=A2*2", "cell B2 is a formula with no saved value; open the file in Excel and save it"],
    ["er.xlsx", "#DIV/0!", "cell B2 holds the error #DIV/0!"],
    ["fe.xlsx", { formula: "1/0", type: "e", saved: "#DIV/0!" }, "cell B2 holds the error #DIV/0!"],
    ["bo.xlsx", true, "cell B2 holds a boolean"],
    ["td.xlsx", { type: "d", saved: "2024-01-02T00:00:00" }, "cell B2 holds a date or time"],
    ["dt.xlsx", { date: "2024-01-02T03:04:05", format: "yyyy-mm-dd" }, "cell B2 holds a date or time"],
  ]) {
    await assertCellRefused(box, file, cell, line);
  }
  // Header and label cells are refused the same way.
  putXlsx(box, "hb.xlsx", sheet([["地区", false], ["a", 1]]));
  assertRefused(box, await box.data("hb.xlsx"), "hb.xlsx: sheet Sheet1: cell B1 holds a boolean");
  putXlsx(box, "lb.xlsx", sheet([["地区", "值"], ["#N/A", 1]]));
  assertRefused(box, await box.data("lb.xlsx"), "lb.xlsx: sheet Sheet1: cell A2 holds the error #N/A");
});

// --- merged ranges ------------------------------------------------------------

test("a merged range is refused, naming the first in row-major order", async (t) => {
  const box = await project(t);
  const rows = [["地区", "a", "b"], ["x", 1, 2], ["y", 3, 4], ["z", 5, 6]];
  putXlsx(box, "m1.xlsx", sheet([["地区", null, "b"], ...rows.slice(1)], { merged: ["A1:B1"] }));
  assertRefused(box, await box.data("m1.xlsx"), "m1.xlsx: sheet Sheet1: range A1:B1 is merged");
  putXlsx(box, "m2.xlsx", sheet(rows, { merged: ["B3:B4", "C2:C3"] }));
  assertRefused(box, await box.data("m2.xlsx"), "m2.xlsx: sheet Sheet1: range C2:C3 is merged");
});

// --- sheets -------------------------------------------------------------------

test("the first sheet is the default, --sheet picks another, and an unknown sheet is refused", async (t) => {
  const box = await project(t);
  putXlsx(box, "w.xlsx", {
    sheets: [
      { title: "销售", rows: [["地区", "销售额"], ["华东", 630]] },
      { title: "成本", rows: [["地区", "成本"], ["华东", 410]] },
    ],
  });
  const first = await box.data("w.xlsx");
  assert.equal(first.code, 0, first.err);
  assert.equal(first.out, "w\n地区\t销售额\n华东\t630\n");
  assert.equal(readTable(box, "w").source.sheet, "销售");
  for (const args of [["w.xlsx", "--sheet", "成本"], ["--sheet", "成本", "w.xlsx"]]) {
    const second = await box.data(...args);
    assert.equal(second.code, 0, second.err);
    assert.equal(second.out, "w\n地区\t成本\n华东\t410\n");
    assert.equal(second.err, "kit data: replaced data/w.json\n");
    assert.equal(readTable(box, "w").source.sheet, "成本");
  }
});

test("an unknown sheet, a chart sheet and a merged range beside an extLst are refused", async (t) => {
  const box = await project(t);
  putXlsx(box, "w.xlsx", {
    sheets: [
      { title: "销售", rows: [["地区", "销售额"], ["华东", 630]] },
      { title: "成本", rows: [["地区", "成本"], ["华东", 410]] },
    ],
  });
  assertRefused(box, await box.data("w.xlsx", "--sheet", "汇总"), "w.xlsx: has no sheet 汇总; its sheets are 销售, 成本");
  assertRefused(box, await box.data("w.xlsx", "--sheet", ""), "w.xlsx: has no sheet ; its sheets are 销售, 成本");
  putXlsx(box, "c.xlsx", {
    sheets: [
      { title: "数据", rows: [["地区", "销售额"], ["华东", 630], ["华南", 250]] },
      { title: "图表", chart: true },
    ],
  });
  assertRefused(box, await box.data("c.xlsx", "--sheet", "图表"), "c.xlsx: sheet 图表 is not a worksheet");
  // openpyxl warns about the extension on stderr unless warnings are ignored.
  putXlsx(box, "v.xlsx", sheet([["地区", null, "b"], ["x", 1, 2]], { merged: ["A1:B1"], validation: true }));
  assertRefused(box, await box.data("v.xlsx"), "v.xlsx: sheet Sheet1: range A1:B1 is merged");
});

// --- limits -------------------------------------------------------------------

test("50 data rows and 12 columns import; 51 rows and 13 columns are refused", async (t) => {
  const box = await project(t);
  const rows = (n) => [["地区", "a"], ...Array.from({ length: n }, (_, i) => [`r${i + 1}`, i])];
  const header = (n) => Array.from({ length: n }, (_, i) => `c${i + 1}`);
  putXlsx(box, "rows51.xlsx", sheet(rows(51)));
  assertRefused(box, await box.data("rows51.xlsx"), "rows51.xlsx: sheet Sheet1: has 51 data rows; the limit is 50");
  putXlsx(box, "cols13.xlsx", sheet([header(13), ["x", ...Array(12).fill(1)]]));
  assertRefused(box, await box.data("cols13.xlsx"), "cols13.xlsx: sheet Sheet1: has 13 columns; the limit is 12");
  putXlsx(box, "rows50.xlsx", sheet(rows(50)));
  assert.equal((await box.data("rows50.xlsx")).code, 0);
  assert.equal(readTable(box, "rows50").rows.length, 50);
  putXlsx(box, "cols12.xlsx", sheet([header(12), ["x", ...Array(11).fill(1)]]));
  assert.equal((await box.data("cols12.xlsx")).code, 0);
  assert.equal(readTable(box, "cols12").columns.length, 11);
});

test("each table refusal gives its text with the sheet and the sheet's row numbers", async (t) => {
  const box = await project(t);
  for (const [file, rows, line] of [
    ["blank.xlsx", [], "has no header row"],
    ["spaces.xlsx", [[" ", null, "  "]], "has no header row"],
    ["one.xlsx", [["地区"], ["华东"]], "the header needs a label column and at least one data column"],
    ["gap.xlsx", [["地区", "销售额", null, "备注"], ["华东", 1, 2, 3]], "column 3 has an empty header"],
    ["dup.xlsx", [["地区", "销售额", "销售额"], ["华东", 1, 2]], "duplicate header 销售额 in columns 2 and 3"],
    ["head.xlsx", [["地区", "销售额"]], "has no data row"],
    ["long.xlsx", [["地区", "a", "b"], ["x", 1, 2], ["y", 1, 2, 3]], "row 3 has 4 cells; the header has 3"],
    ["nolabel.xlsx", [["地区", "a"], ["x", 1], [null, 1]], "row 3 has an empty label"],
    ["twice.xlsx", [["地区", "a"], ["华东", 1], ["华南", 1], ["华东", 2]], "row 4: duplicate label 华东, first in row 2"],
    ["lead.xlsx", [[], [], ["地区", "a"], ["华东", 1], [], ["华东", 2]], "row 6: duplicate label 华东, first in row 4"],
    ["leadcell.xlsx", [[], ["地区", "a"], ["华东", 1], ["华南", true]], "cell B4 holds a boolean"],
  ]) {
    putXlsx(box, file, sheet(rows));
    assertRefused(box, await box.data(file), `${file}: sheet Sheet1: ${line}`);
  }
  // A short row pads with null; header and data rows stop at their last non-empty cell.
  putXlsx(box, "pad.xlsx", sheet([["地区", "a", "b", null], ["x", 1, null, null], ["y", " ", 2]]));
  assert.equal((await box.data("pad.xlsx")).code, 0);
  assert.deepEqual(readTable(box, "pad").rows, [
    { label: "x", cells: [num(1, "1"), null] },
    { label: "y", cells: [null, num(2, "2")] },
  ]);
});

// --- not an xlsx, extensions --------------------------------------------------

test("files that are not readable xlsx and other extensions are refused", async (t) => {
  const box = await project(t);
  box.put("q3.xlsx", "PK");
  box.put("text.xlsx", "a,b\nx,1\n");
  box.put("empty.xlsx", EMPTY_ZIP);
  for (const file of ["q3.xlsx", "text.xlsx", "empty.xlsx"]) {
    assertRefused(box, await box.data(file), `${file}: is not a readable .xlsx file`);
  }
  for (const file of ["t.xlsm", "t.xls", "t.txt"]) {
    putXlsx(box, file, sheet([["a", "b"], ["x", 1]]));
    assertRefused(box, await box.data(file), `${file}: is not a .csv or .xlsx file`);
  }
});

test("the real path's extension decides the format, case-insensitively", async (t) => {
  const box = await project(t);
  putXlsx(box, "Q3.XLSX", sheet([["a", "b"], ["x", 1]]));
  const upper = await box.data("Q3.XLSX");
  assert.equal(upper.code, 0, upper.err);
  assert.deepEqual(readTable(box, "q3").source, { file: "Q3.XLSX", sheet: "Sheet1", sha256: sha(join(box.files, "Q3.XLSX")) });
  // A link a.csv to an xlsx is read as an xlsx, and takes --sheet.
  putXlsx(box, "t.xlsx", sheet([["a", "b"], ["x", 2]]));
  symlinkSync(join(box.files, "t.xlsx"), join(box.files, "a.csv"));
  for (const args of [["a.csv"], ["a.csv", "--sheet", "Sheet1"]]) {
    const linked = await box.data(...args);
    assert.equal(linked.code, 0, linked.err);
    assert.equal(linked.out, "a\na\tb\nx\t2\n");
    assert.deepEqual(readTable(box, "a").source, { file: "t.xlsx", sheet: "Sheet1", sha256: sha(join(box.files, "t.xlsx")) });
  }
  // A link b.xlsx to a CSV is read as a CSV, and refuses --sheet.
  box.put("c.csv", "a,b\nx,3\n");
  symlinkSync(join(box.files, "c.csv"), join(box.files, "b.xlsx"));
  const csvLink = await box.data("b.xlsx");
  assert.equal(csvLink.code, 0, csvLink.err);
  assert.equal(readTable(box, "b").source.sheet, null);
  const refused = await box.data("b.xlsx", "--sheet", "Sheet1");
  assert.deepEqual(refused, { code: 1, out: "", err: "kit data: --sheet applies only to .xlsx files\n" });
});

// --- openpyxl absent, isolation -----------------------------------------------

test("without openpyxl an xlsx fails the step and a CSV still imports", async (t) => {
  const box = await project(t);
  const reader = join(box.root, "bin", "read_table.py");
  writeFileSync(reader, `import sys\nsys.modules["openpyxl"] = None\n${readFileSync(reader, "utf8")}`);
  const { run: projectRun } = await import(pathToFileURL(join(box.root, "bin", "kit.mjs")).href);
  putXlsx(box, "a.xlsx", sheet([["a", "b"], ["x", 1]]));
  assertRefused(box, await kit(["data", "a.xlsx"], box.root, box, projectRun), "the table reader failed: openpyxl is not installed");
  box.put("q3 销售.csv", readFileSync(join(FIXTURES, "q3 销售.csv")));
  const result = await kit(["data", "q3 销售.csv"], box.root, box, projectRun);
  assert.equal(result.code, 0, result.err);
  assert.deepEqual(readFileSync(join(box.dataDir, "q3-销售.json")), readFileSync(join(FIXTURES, "q3-销售.json")));
});

test("the shape check wants source.sheet null for a CSV and a non-empty string for an xlsx", async (t) => {
  const table = (sheetName) => ({
    version: 1,
    source: { file: "a", sheet: sheetName, sha256: "0".repeat(64) },
    label_header: "a",
    columns: ["b"],
    rows: [{ label: "x", cells: [null] }],
  });
  for (const [file, sheetName, ok] of [
    ["a.csv", null, true],
    ["a.csv", "Sheet1", false],
    ["a.xlsx", "Sheet1", true],
    ["a.xlsx", null, false],
    ["a.xlsx", "", false],
  ]) {
    const box = await project(t);
    box.put(file, "stub reads nothing");
    // The test-only bin/ copy: its kit.mjs runs the stub reader beside it.
    writeFileSync(join(box.root, "bin", "read_table.py"), `print(${JSON.stringify(JSON.stringify(table(sheetName)))})\n`);
    const { run: projectRun } = await import(pathToFileURL(join(box.root, "bin", "kit.mjs")).href);
    const result = await kit(["data", file], box.root, box, projectRun);
    if (ok) {
      assert.equal(result.code, 0, `${file} ${sheetName}: ${result.err}`);
      assert.equal(readTable(box, "a").source.sheet, sheetName);
    } else {
      assertRefused(box, result, "the table reader failed: bad output");
    }
  }
});

test("a planted openpyxl.py in the working directory and beside the input is ignored", async (t) => {
  const box = await project(t);
  const planted = 'import sys\nsys.stderr.write("planted module ran\\n")\nsys.exit(9)\n';
  const cwd = join(box.base, "cwd");
  mkdirSync(cwd);
  writeFileSync(join(cwd, "openpyxl.py"), planted);
  box.put("openpyxl.py", planted);
  putXlsx(box, "a.xlsx", sheet([["a", "b"], ["x", 1]]));
  const before = process.cwd();
  process.chdir(cwd);
  t.after(() => process.chdir(before));
  const result = await box.data("a.xlsx");
  assert.equal(result.code, 0, result.err);
  assert.equal(result.err, "kit data: wrote data/a.json\n");
  assert.deepEqual(readTable(box, "a").columns, ["b"]);
});

// --- re-import ----------------------------------------------------------------

test("re-importing an xlsx replaces the table and says so", async (t) => {
  const box = await project(t);
  putXlsx(box, "a.xlsx", sheet([["a", "b"], ["x", 1]]));
  assert.equal((await box.data("a.xlsx")).err, "kit data: wrote data/a.json\n");
  putXlsx(box, "a.xlsx", sheet([["a", "b"], ["x", 2]]));
  const again = await box.data("a.xlsx");
  assert.equal(again.code, 0);
  assert.equal(again.err, "kit data: replaced data/a.json\n");
  assert.equal(readTable(box, "a").rows[0].cells[0].text, "2");
  assert.deepEqual(readdirSync(box.dataDir), ["a.json"]);
});
