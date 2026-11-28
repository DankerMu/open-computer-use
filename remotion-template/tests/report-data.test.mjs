// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// resolveData and derivedLines of bin/report-data.mjs (task 2.3, issue
// #960), pure, over the committed fixture table tests/fixtures/data/
// q3-销售.json and inline tables. Every expected text, value and message
// comes from the task 2.3 fixture and the spec scenarios of "Every figure
// comes from a data item" and "Figures display as supplied"; every derived
// figure was worked out by hand (exact decimals, half away from zero), not
// by running the module.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { escapeLine } from "../bin/escape-line.mjs";
import { escapeLine as schemaEscapeLine } from "../bin/script-schema.mjs";
import { DATA_LIMIT, FIGURE_LIMIT, ITEM_ID, OF_LIMIT, OPS, UNIT, VALUE_TEXT, derivedLines, resolveData } from "../bin/report-data.mjs";

const Q3 = JSON.parse(readFileSync(new URL("./fixtures/data/q3-销售.json", import.meta.url), "utf8"));
const TABLES = { "q3-销售": Q3 };
// A second table with a text cell and an empty cell of its own.
const INLINE = {
  version: 1,
  source: { file: "门店.csv", sheet: null, sha256: "0".repeat(64) },
  label_header: "项目",
  columns: ["数量", "说明"],
  rows: [
    { label: "门店", cells: [{ value: 42, text: "42", unit: "" }, { value: null, text: "待定", unit: "" }] },
    { label: "仓库", cells: [null, null] },
  ],
};

const quoted = (id, value, unit, quote = `${value}${unit}`) => ({ id, label: id, unit, value, quote });
const fromCell = (id, unit, table, row, column) => ({ id, label: id, unit, from: { table, row, column } });
const derived = (id, unit, op, of) => ({ id, label: id, unit, derive: { op, of } });
const ITEM_LIMIT_HINT = "the limit is 12, so ask the user for a larger unit";

function resolved(data, tables = TABLES) {
  const result = resolveData(data, tables);
  assert.equal(result.ok, true, JSON.stringify(result.errors));
  return result.items;
}

function errors(data, tables = TABLES) {
  const result = resolveData(data, tables);
  assert.equal(result.ok, false);
  assert.equal(Object.hasOwn(result, "items"), false);
  return result.errors;
}

// The one error of a data set whose failing item is `id`.
function refused(data, id, message, tables = TABLES) {
  assert.deepEqual(errors(data, tables), [{ index: data.findIndex((item) => item.id === id), id, message }]);
}

const displayOf = (data, id, tables) => resolved(data, tables)[id].display;

// --- exports ----------------------------------------------------------------

test("the exported patterns, operations and figure limit", () => {
  assert.equal(ITEM_ID.source, "^[a-z][a-z0-9_]{0,31}$");
  assert.equal(VALUE_TEXT.source, "^-?[0-9]+(\\.[0-9]+)?$");
  assert.deepEqual(OPS, ["sum", "diff", "product", "ratio", "growth", "pp-diff"]);
  assert.equal(FIGURE_LIMIT, 12);
  assert.equal(UNIT.source, "^(?![\\s.．+\\p{Pd}−±])[^\\p{N}]*$");
  assert.equal(UNIT.flags, "u");
  assert.equal(DATA_LIMIT, 200);
  assert.equal(OF_LIMIT, 50);
});

// --- spec scenarios ---------------------------------------------------------

test("Quote does not contain the value and unit", () => {
  refused([quoted("sales", "1260", "亿元", "季度销售额1260万元")], "sales", "data item sales: 1260亿元 does not occur in its quote");
});

test("Thousands separators in the quote", () => {
  const items = resolved([quoted("sales", "1260", "万元", "销售额1,260万元")]);
  assert.deepEqual({ ...items.sales }, {
    id: "sales",
    label: "sales",
    unit: "万元",
    value: 1260,
    text: "1260",
    display: "1260万元",
    source: { quote: "销售额1,260万元" },
  });
});

test("Half as a percent", () => {
  const data = [
    quoted("clients", "42", "家", "客户42家"),
    quoted("east_share", "50", "%", "其中华东区占一半"),
    derived("east_clients", "家", "product", ["clients", "east_share"]),
  ];
  const items = resolved(data);
  assert.equal(items.east_share.display, "50%");
  assert.equal(items.east_clients.display, "21家");
});

test("Growth computed by the kit", () => {
  const data = [quoted("sales", "1260", "万元"), quoted("sales_ly", "1068", "万元"), derived("sales_growth", "%", "growth", ["sales", "sales_ly"])];
  const items = resolved(data);
  assert.deepEqual({ ...items.sales_growth }, {
    id: "sales_growth",
    label: "sales_growth",
    unit: "%",
    value: 18,
    text: "18",
    display: "18%",
    source: { op: "growth", of: ["sales", "sales_ly"], operands: ["1260万元", "1068万元"] },
  });
});

test("Percentage difference uses pp-diff", () => {
  const rates = [quoted("collection", "93", "%"), quoted("collection_last", "88", "%")];
  refused([...rates, derived("d", "%", "diff", ["collection", "collection_last"])], "d", "data item d: diff cannot take % items; use pp-diff");
  assert.equal(displayOf([...rates, derived("d", "个百分点", "pp-diff", ["collection", "collection_last"])], "d"), "5个百分点");
});

test("Derivation cycle", () => {
  const data = [derived("a", "万元", "sum", ["b", "c"]), derived("b", "万元", "sum", ["a", "c"]), quoted("c", "1", "万元")];
  assert.deepEqual(errors(data), [{ index: 0, id: "a", message: "data: cycle a → b → a" }]);
});

test("Table cell resolves", () => {
  const items = resolved([fromCell("east", "万元", "q3-销售", "华东", "销售额")]);
  assert.deepEqual({ ...items.east }, {
    id: "east",
    label: "east",
    unit: "万元",
    value: 630,
    text: "630",
    display: "630万元",
    source: { table: "q3-销售", row: "华东", column: "销售额" },
  });
});

test("Unknown table column", () => {
  refused([fromCell("east", "万元", "q3-销售", "华东", "回款")], "east", "data item east: 回款 is not a column of q3-销售 (columns: 销售额, 完成率, 备注)");
});

test("Trailing zero kept", () => {
  assert.equal(displayOf([quoted("profit", "3.50", "亿元")], "profit"), "3.50亿元");
});

test("Percentage rounding", () => {
  const ratioOf = (part) => displayOf([quoted("part", part, "元"), quoted("whole", "10000", "元"), derived("r", "%", "ratio", ["part", "whole"])], "r");
  assert.equal(ratioOf("8400"), "84%");
  assert.equal(ratioOf("8437"), "84.4%");
  assert.equal(ratioOf("8425"), "84.3%");
});

test("Derived count rounding", () => {
  const data = [quoted("clients", "43", "家"), quoted("share", "50", "%"), derived("east_clients", "家", "product", ["clients", "share"])];
  assert.equal(displayOf(data, "east_clients"), "22家");
});

// --- the quote rule ---------------------------------------------------------

function quoteCase(value, unit, quote, passes) {
  const data = [quoted("x", value, unit, quote)];
  if (passes) assert.equal(displayOf(data, "x"), `${value}${unit}`, JSON.stringify(quote));
  else refused(data, "x", `data item x: ${value}${unit} does not occur in its quote`);
}

test("the quote rule: whitespace between the value and a non-empty unit", () => {
  quoteCase("1260", "万元", "1260 万元", true);
  quoteCase("1260", "万元", "1260\t万元", true);
  quoteCase("1260", "万元", "1260　万元", true);
  quoteCase("1260", "万元", "1260  \t万元", true);
  quoteCase("1260", "万元", "1260\n万元", false);
});

test("the quote rule: a digit, point or sign before the value", () => {
  quoteCase("1260", "万元", "11260万元", false);
  quoteCase("5", "%", "-5%", false);
  quoteCase("5", "%", "－5%", false);
  quoteCase("5", "%", "−5%", false);
  quoteCase("5", "%", ".5%", false);
  quoteCase("-5", "%", "下降-5%", true);
  quoteCase("5", "%", "–5%", false);
  quoteCase("5", "%", "负5%", false);
  quoteCase("5", "%", "±5%", false);
  quoteCase("5", "%", "⁻5%", false);
  quoteCase("5", "%", "₋5%", false);
  quoteCase("5", "%", "+5%", true);
  quoteCase("260", "万元", "１260万元", false);
  quoteCase("5", "万元", "1．5万元", false);
  // A later occurrence counts when an earlier one does not.
  quoteCase("1260", "万元", "去年11260万元，今年1260万元", true);
});

test("the quote rule: the tail of a grouped number never matches", () => {
  quoteCase("260", "万元", "1 260万元", false);
  quoteCase("260", "万元", "1\t260万元", false);
  quoteCase("260", "万元", "1　260万元", false);
  quoteCase("26", "万元", "1,26万元", false);
  quoteCase("26", "万元", "1，26万元", false);
  quoteCase("260", "万元", "1  260万元", false);
  quoteCase("260", "万元", "1, 260万元", false);
  quoteCase("260", "万元", "1\u00a0260万元", false);
  quoteCase("260", "万元", "1'260万元", false);
  quoteCase("260", "万元", "1’260万元", false);
  // A separator or space after a non-digit is no group.
  quoteCase("260", "万元", "华东 260万元", true);
  quoteCase("260", "万元", "华东，260万元", true);
});

test("the quote rule: what follows the value", () => {
  quoteCase("3.5", "亿元", "3.50亿元", false);
  quoteCase("42", "", "421家", false);
  quoteCase("42", "", "42家", true);
  quoteCase("42", "", "42.5家", false);
  quoteCase("42", "", "42 家", true);
  quoteCase("42", "", "共42.", true);
  quoteCase("1", "", "1 260", false);
  quoteCase("1", "", "1　260", false);
});

test("the quote rule: format characters are deleted first", () => {
  // Zero-width space and soft hyphen.
  for (const mark of ["\u200b", "\u00ad"]) {
    quoteCase("260", "万元", `1${mark}260万元`, false);
    quoteCase("1260", "万元", `1${mark}260万元`, true);
  }
  quoteCase("1", "", "1\u200b260", false);
});

test("the quote rule: a magnitude or percent sign after the match", () => {
  quoteCase("3", "万", "规模达3万亿元", false);
  quoteCase("3", "万", "3万 亿元", false);
  quoteCase("3", "万", "3万\u3000亿元", false);
  quoteCase("3", "", "规模达3万亿元", false);
  quoteCase("5", "千", "5千万元", false);
  quoteCase("5", "个", "提高5个百分点", false);
  quoteCase("93", "", "93%", false);
  quoteCase("93", "", "93 ％", false);
  quoteCase("93", "", "93‰", false);
  quoteCase("42", "", "42５", false);
  quoteCase("1260", "万元", "1260万元，同比", true);
});

test("the quote rule: thousands separators", () => {
  quoteCase("1260", "万元", "销售额1,260万元", true);
  quoteCase("1260", "万元", "1，260万元", true);
  quoteCase("126", "万元", "1,26万元", false);
  quoteCase("12345678", "元", "12,345,678元", true);
  quoteCase("1260", "万元", "1,2600万元", false);
  quoteCase("12600", "元", "1,2600元", false);
});

test("the quote rule: 一半 only for a % item whose value is exactly 50", () => {
  quoteCase("50", "%", "一半", true);
  quoteCase("50.0", "%", "一半", false);
  quoteCase("50", "家", "一半", false);
});

test("the quote rule: value and unit match literally", () => {
  quoteCase("3.5", "亿元", "3x5亿元", false);
  quoteCase("5", "(元)", "5(元)", true);
});

// --- from failures ----------------------------------------------------------

test("from: an unknown table lists the imports in the map's order", () => {
  const data = [fromCell("east", "万元", "q3-xx", "华东", "销售额")];
  refused(data, "east", "data item east: table q3-xx is not imported (imported: q3-销售)");
  refused(data, "east", "data item east: table q3-xx is not imported (imported: none)", {});
  refused(data, "east", "data item east: table q3-xx is not imported (imported: 门店, q3-销售)", { 门店: INLINE, "q3-销售": Q3 });
});

test("from: an unknown row lists the rows", () => {
  refused([fromCell("east", "万元", "q3-销售", "东北", "销售额")], "east", "data item east: 东北 is not a row of q3-销售 (rows: 华东, 华南, 华北, 华中, 西南, 合计)");
});

test("from: an empty cell and a text cell", () => {
  refused([fromCell("east", "", "q3-销售", "西南", "备注")], "east", "data item east: q3-销售 row 西南, column 备注 is empty");
  refused([fromCell("east", "", "q3-销售", "华东", "备注")], "east", "data item east: q3-销售 row 华东, column 备注 is text, not a number");
  const tables = { "q3-销售": Q3, 门店: INLINE };
  refused([fromCell("east", "", "门店", "仓库", "数量")], "east", "data item east: 门店 row 仓库, column 数量 is empty", tables);
  refused([fromCell("east", "", "门店", "门店", "说明")], "east", "data item east: 门店 row 门店, column 说明 is text, not a number", tables);
});

test("from: a % cell needs a % item; a cell with no unit takes any unit", () => {
  refused([fromCell("east", "万元", "q3-销售", "华东", "完成率")], "east", "data item east: q3-销售 row 华东, column 完成率 is in %, but the item's unit is 万元");
  refused([fromCell("east", "", "q3-销售", "华东", "完成率")], "east", "data item east: q3-销售 row 华东, column 完成率 is in %, but the item's unit is no unit");
  const items = resolved([fromCell("rate", "%", "q3-销售", "华东", "完成率"), fromCell("north", "%", "q3-销售", "华北", "完成率"), fromCell("stores", "家", "门店", "门店", "数量")], {
    "q3-销售": Q3,
    门店: INLINE,
  });
  assert.equal(items.rate.display, "93%");
  assert.equal(items.rate.value, 93);
  // The stored text, trailing zero and all.
  assert.equal(items.north.display, "78.0%");
  assert.equal(items.north.value, 78);
  assert.equal(items.stores.display, "42家");
});

test("from: constructor and __proto__ are never inherited", () => {
  refused([fromCell("east", "万元", "constructor", "华东", "销售额")], "east", "data item east: table constructor is not imported (imported: q3-销售)");
  refused([fromCell("east", "万元", "__proto__", "华东", "销售额")], "east", "data item east: table __proto__ is not imported (imported: q3-销售)");
  refused([fromCell("east", "万元", "q3-销售", "constructor", "销售额")], "east", "data item east: constructor is not a row of q3-销售 (rows: 华东, 华南, 华北, 华中, 西南, 合计)");
  refused([fromCell("east", "万元", "q3-销售", "华东", "constructor")], "east", "data item east: constructor is not a column of q3-销售 (columns: 销售额, 完成率, 备注)");
});

test("from: echoed names are escaped onto one line", () => {
  refused([fromCell("east", "万元", "q3-销售", "东\n北", "销售额")], "east", "data item east: 东\\n北 is not a row of q3-销售 (rows: 华东, 华南, 华北, 华中, 西南, 合计)");
});

// --- operations -------------------------------------------------------------

test("one passing case of each operation", () => {
  const data = [
    quoted("a", "1260", "万元"),
    quoted("b", "240", "万元"),
    quoted("c", "300", "万元"),
    quoted("ly", "1068", "万元"),
    quoted("share", "50", "%"),
    quoted("rate", "93", "%"),
    quoted("rate_ly", "88", "%"),
    derived("s", "万元", "sum", ["a", "b", "c"]),
    derived("d", "万元", "diff", ["a", "ly"]),
    derived("p", "万元", "product", ["a", "share"]),
    derived("r", "%", "ratio", ["b", "c"]),
    derived("g", "%", "growth", ["a", "ly"]),
    derived("pp", "个百分点", "pp-diff", ["rate", "rate_ly"]),
  ];
  const items = resolved(data);
  assert.deepEqual(
    ["s", "d", "p", "r", "g", "pp"].map((id) => items[id].display),
    ["1800万元", "192万元", "630万元", "80%", "18%", "5个百分点"],
  );
});

test("arithmetic is exact and keeps the operands' decimal count", () => {
  assert.equal(displayOf([quoted("a", "0.1", ""), quoted("b", "0.2", ""), derived("s", "", "sum", ["a", "b"])], "s"), "0.3");
  // A double with Math.round gives 1.00元 and 100.4%.
  assert.equal(displayOf([quoted("a", "2.01", "元"), quoted("p", "50", "%"), derived("x", "元", "product", ["a", "p"])], "x"), "1.01元");
  assert.equal(displayOf([quoted("a", "1.0045", "元"), quoted("b", "1", "元"), derived("r", "%", "ratio", ["a", "b"])], "r"), "100.5%");
  const items = resolved([quoted("a", "1.50", "亿元"), quoted("b", "2", "亿元"), derived("s", "亿元", "sum", ["a", "b"])]);
  assert.equal(items.s.display, "3.50亿元");
  assert.equal(items.s.text, "3.50");
  assert.equal(items.s.value, 3.5);
  assert.equal(displayOf([quoted("a", "1.5", "亿元"), quoted("b", "2.25", "亿元"), derived("s", "亿元", "sum", ["a", "b"])], "s"), "3.75亿元");
});

test("rounding: half away from zero, and zero has no sign", () => {
  assert.equal(displayOf([quoted("a", "1", "元"), quoted("b", "3", "元"), derived("r", "%", "ratio", ["a", "b"])], "r"), "33.3%");
  // (391 − 400) × 100 ÷ 400 = −2.25.
  assert.equal(displayOf([quoted("now", "391", "元"), quoted("base", "400", "元"), derived("g", "%", "growth", ["now", "base"])], "g"), "-2.3%");
  const zero = resolved([quoted("a", "5", "元"), quoted("b", "5", "元"), derived("d", "元", "diff", ["a", "b"])]).d;
  assert.deepEqual([zero.text, zero.display, zero.value], ["0", "0元", 0]);
  assert.equal(displayOf([quoted("a", "3", "元"), quoted("b", "5", "元"), derived("d", "元", "diff", ["a", "b"])], "d"), "-2元");
  assert.equal(displayOf([quoted("a", "1.50", "元"), quoted("b", "1.5", "元"), derived("d", "元", "diff", ["a", "b"])], "d"), "0.00元");
  // (9999.996 − 10000) × 100 ÷ 10000 = −0.00004, which rounds to 0.
  assert.equal(displayOf([quoted("now", "9999.996", "元"), quoted("base", "10000", "元"), derived("g", "%", "growth", ["now", "base"])], "g"), "0%");
  // −1 × 10 ÷ 100 = −0.1, which rounds to 0 at no decimals.
  assert.equal(displayOf([quoted("a", "-1", "元"), quoted("p", "10", "%"), derived("x", "元", "product", ["a", "p"])], "x"), "0元");
  // −2.5 × 50 ÷ 100 = −1.25, which rounds to −1.3 at one decimal.
  assert.equal(displayOf([quoted("a", "-2.5", "元"), quoted("p", "50", "%"), derived("x", "元", "product", ["a", "p"])], "x"), "-1.3元");
  // Ties that a float misses (145 × 100 ÷ 10000 and 2.9 × 50 ÷ 100 both
  // round to 1.4 in binary): exactly 1.45, so 1.5.
  assert.equal(displayOf([quoted("a", "145", "元"), quoted("b", "10000", "元"), derived("r", "%", "ratio", ["a", "b"])], "r"), "1.5%");
  assert.equal(displayOf([quoted("a", "2.9", "元"), quoted("p", "50", "%"), derived("x", "元", "product", ["a", "p"])], "x"), "1.5元");
  // 93 − 78.0 = 15.0, which drops its .0.
  const rates = [fromCell("east", "%", "q3-销售", "华东", "完成率"), fromCell("north", "%", "q3-销售", "华北", "完成率")];
  assert.equal(displayOf([...rates, derived("gap", "个百分点", "pp-diff", ["east", "north"])], "gap"), "15个百分点");
});

test("a chain uses each operand's displayed text", () => {
  const items = resolved([
    quoted("a", "600", "万元"),
    quoted("b", "660", "万元"),
    quoted("ly", "1068", "万元"),
    derived("s", "万元", "sum", ["a", "b"]),
    derived("g", "%", "growth", ["s", "ly"]),
  ]);
  assert.equal(items.g.display, "18%");
  assert.deepEqual(items.g.source.operands, ["1260万元", "1068万元"]);
  // 43 × 50% is 21.5, shown as 22; growth over 20 uses 22 (10%), not 21.5 (7.5%).
  const rounded = resolved([
    quoted("clients", "43", "家"),
    quoted("share", "50", "%"),
    quoted("base", "20", "家"),
    derived("east", "家", "product", ["clients", "share"]),
    derived("g", "%", "growth", ["east", "base"]),
  ]);
  assert.equal(rounded.g.display, "10%");
  assert.deepEqual(rounded.g.source.operands, ["22家", "20家"]);
});

test("operand counts and unknown items", () => {
  const two = [quoted("a", "1", "元"), quoted("b", "2", "元"), quoted("c", "3", "元")];
  refused([...two, derived("g", "%", "growth", ["a", "b", "c"])], "g", "data item g: growth takes 2 items, got 3");
  refused([...two, derived("s", "元", "sum", ["a"])], "s", "data item s: sum takes 2 or more items, got 1");
  refused([...two, derived("d", "元", "diff", ["a"])], "d", "data item d: diff takes 2 items, got 1");
  refused([...two, derived("g", "%", "growth", ["a", "foo"])], "g", "data item g: names unknown item foo");
  refused([...two, derived("g", "%", "growth", ["constructor", "a"])], "g", "data item g: names unknown item constructor");
});

test("sum and diff refuse % and 个百分点 items, then mixed units", () => {
  const data = [quoted("a", "1", "万元"), quoted("b", "2", "家"), quoted("pct", "5", "%"), quoted("pts", "3", "个百分点"), quoted("pts2", "1", "个百分点")];
  refused([...data, derived("d", "%", "diff", ["pct", "a"])], "d", "data item d: diff cannot take % items; use pp-diff");
  refused([...data, derived("d", "个百分点", "sum", ["pts", "pts2"])], "d", "data item d: sum cannot take 个百分点 items; use pp-diff");
  refused([...data, derived("d", "个百分点", "diff", ["pts", "pts2"])], "d", "data item d: diff cannot take 个百分点 items; use pp-diff");
  refused([...data, derived("s", "万元", "sum", ["a", "b", "a"])], "s", "data item s: sum mixes units: a (万元), b (家)");
  refused([...data, derived("s", "万元", "diff", ["a", "b"])], "s", "data item s: diff mixes units: a (万元), b (家)");
  refused([...data, derived("s", "%", "ratio", ["a", "b"])], "s", "data item s: ratio mixes units: a (万元), b (家)");
  refused([...data, derived("s", "%", "growth", ["a", "b"])], "s", "data item s: growth mixes units: a (万元), b (家)");
});

test("product and pp-diff operand rules", () => {
  const data = [quoted("a", "1", "%"), quoted("b", "2", "家"), quoted("n", "3", ""), quoted("pts", "4", "个百分点"), quoted("w", "5", "万元")];
  refused([...data, derived("p", "家", "product", ["w", "b"])], "p", "data item p: product takes a % item second; b is in 家");
  refused([...data, derived("p", "万元", "product", ["w", "n"])], "p", "data item p: product takes a % item second; n is in no unit");
  // Swapped: the % item first, and the 家 item second, both wrong.
  refused([...data, derived("p", "家", "product", ["a", "b"])], "p", "data item p: product cannot take a % or 个百分点 item first; a is in %");
  refused([...data, derived("p", "个百分点", "product", ["pts", "b"])], "p", "data item p: product cannot take a % or 个百分点 item first; pts is in 个百分点");
  refused([...data, derived("p", "个百分点", "pp-diff", ["w", "a"])], "p", "data item p: pp-diff takes two % items; w is in 万元");
  refused([...data, derived("p", "个百分点", "pp-diff", ["a", "n"])], "p", "data item p: pp-diff takes two % items; n is in no unit");
});

test("the item's own unit must be the operation's", () => {
  const data = [quoted("a", "4", "万元"), quoted("b", "2", "万元"), quoted("pct", "50", "%"), quoted("r1", "9", "%"), quoted("r2", "8", "%")];
  refused([...data, derived("g", "元", "growth", ["a", "b"])], "g", "data item g: its unit is 元, but growth gives %");
  refused([...data, derived("s", "元", "sum", ["a", "b"])], "s", "data item s: its unit is 元, but sum gives 万元");
  refused([...data, derived("p", "%", "product", ["a", "pct"])], "p", "data item p: its unit is %, but product gives 万元");
  refused([...data, derived("p", "%", "pp-diff", ["r1", "r2"])], "p", "data item p: its unit is %, but pp-diff gives 个百分点");
  refused([...data, derived("r", "", "ratio", ["a", "b"])], "r", "data item r: its unit is no unit, but ratio gives %");
});

test("a zero divisor", () => {
  const data = [quoted("part", "3", "万元"), quoted("w", "0", "万元"), quoted("z", "0.00", "万元"), quoted("b", "0", "万元")];
  refused([...data, derived("r", "%", "ratio", ["part", "w"])], "r", "data item r: ratio divides by w, which is 0");
  refused([...data, derived("r", "%", "ratio", ["part", "z"])], "r", "data item r: ratio divides by z, which is 0");
  refused([...data, derived("g", "%", "growth", ["part", "b"])], "g", "data item g: growth divides by b, which is 0");
  // A zero part is fine.
  assert.equal(displayOf([...data, derived("r", "%", "ratio", ["w", "part"])], "r"), "0%");
});

test("check order within an item", () => {
  const data = [quoted("pct", "5", "%"), quoted("w", "1", "万元"), quoted("b", "2", "家"), quoted("zero", "0", "万元")];
  refused([...data, derived("d", "万元", "diff", ["pct", "w"])], "d", "data item d: diff cannot take % items; use pp-diff");
  refused([...data, derived("g", "%", "growth", ["w", "foo", "b"])], "g", "data item g: growth takes 2 items, got 3");
  refused([...data, derived("p", "家", "product", ["pct", "b"])], "p", "data item p: product cannot take a % or 个百分点 item first; pct is in %");
  refused([...data, derived("g", "元", "growth", ["w", "b"])], "g", "data item g: growth mixes units: w (万元), b (家)");
  refused([...data, derived("r", "元", "ratio", ["w", "zero"])], "r", "data item r: its unit is 元, but ratio gives %");
  refused([quoted("s", "1234567890123", "万元", "销售额1234567890124万元")], "s", "data item s: 1234567890123万元 does not occur in its quote");
  const twice = [quoted("a", "1", "元"), quoted("sales", "2", "元"), quoted("b", "3", "元"), quoted("sales", "1234567890123", "万元", "无")];
  assert.deepEqual(errors(twice), [{ index: 3, id: "sales", message: "data item sales: is listed twice, at data[1] and data[3]" }]);
});

// --- dependents and cycles --------------------------------------------------

test("an item over a failed operand is not reported", () => {
  const data = [quoted("a", "1260", "万元", "无"), quoted("b", "1068", "万元"), derived("g", "%", "growth", ["a", "b"]), derived("h", "%", "growth", ["g", "g"])];
  refused(data, "a", "data item a: 1260万元 does not occur in its quote");
  const long = [quoted("big", "12345678901234", "元"), quoted("b", "1", "元"), derived("s", "元", "sum", ["big", "b"])];
  refused(long, "big", `data item big: displays 12345678901234元, 15 characters; ${ITEM_LIMIT_HINT}`);
});

test("a three-item cycle reports once, at its first item in data order", () => {
  const data = [
    derived("dep", "万元", "sum", ["b", "z"]),
    quoted("z", "1", "万元"),
    derived("a", "万元", "sum", ["b", "z"]),
    derived("b", "万元", "sum", ["c", "z"]),
    derived("c", "万元", "sum", ["a", "z"]),
  ];
  assert.deepEqual(errors(data), [{ index: 2, id: "a", message: "data: cycle a → b → c → a" }]);
});

test("two separate cycles report twice, in data order", () => {
  const data = [
    derived("a", "万元", "sum", ["b", "z"]),
    derived("b", "万元", "sum", ["a", "z"]),
    derived("c", "万元", "sum", ["d", "z"]),
    derived("d", "万元", "sum", ["c", "z"]),
    quoted("z", "1", "万元"),
  ];
  assert.deepEqual(errors(data), [
    { index: 0, id: "a", message: "data: cycle a → b → a" },
    { index: 2, id: "c", message: "data: cycle c → d → c" },
  ]);
});

test("a cycle whose first member also names a failing item still reports the cycle", () => {
  const data = [
    derived("a", "万元", "sum", ["bad", "b"]),
    derived("b", "万元", "sum", ["a", "z"]),
    quoted("bad", "5", "万元", "无"),
    quoted("z", "1", "万元"),
  ];
  assert.deepEqual(errors(data), [
    { index: 0, id: "a", message: "data: cycle a → b → a" },
    { index: 2, id: "bad", message: "data item bad: 5万元 does not occur in its quote" },
  ]);
});

test("a loop through an item that failed its source is not a loop", () => {
  const data = [derived("a", "万元", "sum", ["b", "z"]), derived("b", "万元", "sum", ["a", "foo"]), quoted("z", "1", "万元")];
  refused(data, "b", "data item b: names unknown item foo");
});

test("an item that names itself is a cycle", () => {
  refused([derived("a", "万元", "sum", ["a", "z"]), quoted("z", "1", "万元")], "a", "data: cycle a → a");
});

test("a second loop through a reported item is not reported again", () => {
  const data = [
    derived("a", "万元", "sum", ["b", "c"]),
    derived("b", "万元", "sum", ["a", "z"]),
    derived("c", "万元", "sum", ["a", "z"]),
    quoted("z", "1", "万元"),
  ];
  assert.deepEqual(errors(data), [{ index: 0, id: "a", message: "data: cycle a → b → a" }]);
});

// --- limits -----------------------------------------------------------------

test("a unit holds no digit and starts with no space, point or sign", () => {
  for (const unit of ["5万元", ".5万元", " 万元", "-元", "½万元"]) {
    refused([quoted("a", "1", unit, "无")], "a", `data item a: unit ${unit} holds a digit or starts with a space, a point or a sign`);
  }
  for (const unit of ["万元", "%", "个百分点", ""]) assert.equal(displayOf([quoted("a", "1", unit)], "a"), `1${unit}`);
  // After the duplicate check, before the source.
  assert.deepEqual(errors([quoted("a", "1", "元"), quoted("a", "1", "5元")]), [{ index: 1, id: "a", message: "data item a: is listed twice, at data[0] and data[1]" }]);
  refused([fromCell("a", "5万元", "q3-xx", "华东", "销售额")], "a", "data item a: unit 5万元 holds a digit or starts with a space, a point or a sign");
});

test("data holds at most 200 items, and one of at most 50", () => {
  const many = (count) => Array.from({ length: count }, (_, i) => quoted(`i${i}`, "1", "元"));
  assert.equal(Object.keys(resolved(many(200))).length, 200);
  // Nothing else is checked: the failing first item is not reported.
  const over = [quoted("i0", "1", "元", "无"), ...many(201).slice(1)];
  assert.deepEqual(errors(over), [{ index: 200, id: "i200", message: "data: has 201 items; the limit is 200" }]);
  const ids = (count) => Array.from({ length: count }, (_, i) => `i${i}`);
  assert.equal(displayOf([...many(51), derived("s", "元", "sum", ids(50))], "s"), "50元");
  refused([...many(51), derived("s", "元", "sum", ids(51))], "s", "data item s: sum takes at most 50 items, got 51");
  refused([...many(51), derived("g", "%", "growth", ids(51))], "g", "data item g: growth takes at most 50 items, got 51");
});

test("a 200-deep sum chain resolves", () => {
  // c198 = sum(c197, one), …, c1 = sum(c0, one), listed deepest first.
  const chain = Array.from({ length: 198 }, (_, i) => derived(`c${198 - i}`, "元", "sum", [`c${197 - i}`, "one"]));
  const data = [...chain, quoted("c0", "1", "元"), quoted("one", "1", "元")];
  assert.equal(data.length, 200);
  assert.equal(displayOf(data, "c198"), "199元");
});

test("the 12-code-point limit, for a value item and a derived item", () => {
  assert.equal(displayOf([quoted("s", "1234567890", "万元")], "s"), "1234567890万元");
  refused([quoted("s", "12345678901", "万元")], "s", `data item s: displays 12345678901万元, 13 characters; ${ITEM_LIMIT_HINT}`);
  refused([quoted("s", "1234567890123", "万元")], "s", `data item s: displays 1234567890123万元, 15 characters; ${ITEM_LIMIT_HINT}`);
  const half = [quoted("a", "5000000000", "元"), quoted("b", "5000000000", "元")];
  assert.equal(displayOf([...half, derived("s", "元", "sum", ["a", "b"])], "s"), "10000000000元");
  const whole = [quoted("a", "50000000000", "元"), quoted("b", "50000000000", "元")];
  refused([...whole, derived("s", "元", "sum", ["a", "b"])], "s", `data item s: displays 100000000000元, 13 characters; ${ITEM_LIMIT_HINT}`);
  // A table cell, too, whatever scene uses it.
  refused([fromCell("t", "万元万元万元万元万元", "q3-销售", "合计", "销售额")], "t", `data item t: displays 1260万元万元万元万元万元, 14 characters; ${ITEM_LIMIT_HINT}`);
});

test("a duplicate id is reported at each later occurrence", () => {
  const data = [quoted("sales", "1", "元"), quoted("sales", "2", "元"), quoted("sales", "3", "元")];
  assert.deepEqual(errors(data), [
    { index: 1, id: "sales", message: "data item sales: is listed twice, at data[0] and data[1]" },
    { index: 2, id: "sales", message: "data item sales: is listed twice, at data[0] and data[2]" },
  ]);
});

test("errors from several items come in data order", () => {
  const data = [
    derived("s", "元", "sum", ["a", "b"]),
    quoted("a", "1", "万元"),
    quoted("b", "2", "万元"),
    quoted("c", "3", "万元", "无"),
    fromCell("e", "万元", "q3-xx", "华东", "销售额"),
    derived("g", "%", "growth", ["a", "foo"]),
  ];
  assert.deepEqual(errors(data), [
    { index: 0, id: "s", message: "data item s: its unit is 元, but sum gives 万元" },
    { index: 3, id: "c", message: "data item c: 3万元 does not occur in its quote" },
    { index: 4, id: "e", message: "data item e: table q3-xx is not imported (imported: q3-销售)" },
    { index: 5, id: "g", message: "data item g: names unknown item foo" },
  ]);
});

// --- shape ------------------------------------------------------------------

test("items has a null prototype and data order", () => {
  const items = resolved([
    derived("g", "%", "growth", ["sales", "sales_ly"]),
    quoted("sales", "1260", "万元"),
    quoted("constructor", "1", "家"),
    quoted("sales_ly", "1068", "万元"),
  ]);
  assert.equal(Object.getPrototypeOf(items), null);
  assert.deepEqual(Object.keys(items), ["g", "sales", "constructor", "sales_ly"]);
  assert.equal(items.constructor.display, "1家");
});

test("derivedLines lists each derived item with its operands' display text", () => {
  const items = resolved([
    quoted("sales", "1260", "万元"),
    quoted("sales_ly", "1068", "万元"),
    derived("sales_growth", "%", "growth", ["sales", "sales_ly"]),
    fromCell("east", "万元", "q3-销售", "华东", "销售额"),
    derived("sales_diff", "万元", "diff", ["sales", "sales_ly"]),
  ]);
  assert.deepEqual(derivedLines(items), ["sales_growth = growth(1260万元, 1068万元) = 18%", "sales_diff = diff(1260万元, 1068万元) = 192万元"]);
  assert.deepEqual(derivedLines(resolved([quoted("a", "1", "元")])), []);
});

// --- the escapeLine move ----------------------------------------------------

test("script-schema.mjs re-exports escape-line.mjs's escapeLine", () => {
  assert.equal(schemaEscapeLine, escapeLine);
  assert.equal(escapeLine("a\nb\tc "), "a\\nb\\tc\\u2028");
});
