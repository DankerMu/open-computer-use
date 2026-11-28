// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The report data module: every report figure's number text comes from
// here and nowhere else. A `value` item shows its string as written, a
// `from` item its table cell's text, and a derived item the exact decimal
// result of its operation, rounded for display. All arithmetic is exact
// rational arithmetic on BigInt, from the operands' displayed number texts;
// no float takes part in any text, and `value` (Number(text)) is only for
// magnitude and sorting. Every lookup is an own-property or Map lookup, so
// no id, table, row or column is inherited, and every echoed string is
// escaped onto one line. The renderer bundle imports this module, so it
// imports no node: built-in and no package.

import { escapeLine } from "./escape-line.mjs";

export const ITEM_ID = /^[a-z][a-z0-9_]{0,31}$/;
export const VALUE_TEXT = /^-?[0-9]+(\.[0-9]+)?$/;
export const OPS = ["sum", "diff", "product", "ratio", "growth", "pp-diff"];
export const FIGURE_LIMIT = 12;
export const UNIT = /^(?![\s.．+\p{Pd}−±])[^\p{Nd}]*$/u;
// Items in data and entries in one `of`, so the walk's depth and every
// argument list stay bounded.
export const DATA_LIMIT = 200;
export const OF_LIMIT = 50;

const PERCENT = "%";
const POINTS = "个百分点";
// A `,` or `，` with an ASCII digit before it and exactly three after it.
const SEPARATOR = /(?<=[0-9])[,，](?=[0-9]{3}(?![0-9]))/g;
const MAGNITUDE = new Set(["十", "百", "千", "万", "亿", "兆"]);

const unitName = (unit) => (unit === "" ? "no unit" : escapeLine(unit));
const isPercentLike = (unit) => unit === PERCENT || unit === POINTS;

// --- the quote rule ---------------------------------------------------------

// Sticky patterns, tried at one position. A digit is \p{Nd}, whitespace is
// \p{Zs} or a tab, and a grouping character is whitespace, `,`, `，`, `'`
// or `’`.
// Before the match: no digit, point or sign (a sign the user wrote is never
// dropped), and no run of grouping characters after a digit (the tail of a
// grouped number: `1 260`, `1, 260`, `1,26`, `1'260`).
const CLEAR_BEFORE = /(?<![\p{Nd}.．\p{Pd}−±负])(?<!\p{Nd}[\p{Zs}\t,，'’]+)/uy;
const SPACE_RUN = /[\p{Zs}\t]*/uy;
// With no unit, the value must not run on into a longer number or carry a
// magnitude or percent sign.
const CLEAR_AFTER_BARE = /(?!\p{Nd}|[.．]\p{Nd}|[\p{Zs}\t]+\p{Nd}|[\p{Zs}\t]*[十百千万亿兆%％‰])/uy;

function holdsAt(pattern, text, at) {
  pattern.lastIndex = at;
  return pattern.test(text);
}

function spacesEnd(text, at) {
  SPACE_RUN.lastIndex = at;
  SPACE_RUN.test(text);
  return SPACE_RUN.lastIndex;
}

function quoteHolds(quote, value, unit) {
  if (!VALUE_TEXT.test(value)) return false;
  if (unit === PERCENT && value === "50" && quote.includes("一半")) return true;
  const text = quote.replace(SEPARATOR, "");
  for (let at = text.indexOf(value); at !== -1; at = text.indexOf(value, at + 1)) {
    if (!holdsAt(CLEAR_BEFORE, text, at)) continue;
    const end = at + value.length;
    if (unit === "") {
      if (holdsAt(CLEAR_AFTER_BARE, text, end)) return true;
      continue;
    }
    // A unit holds no digit, so it never ends in half a surrogate pair.
    const unitAt = spacesEnd(text, end);
    if (text.startsWith(unit, unitAt) && !MAGNITUDE.has(text[unitAt + unit.length])) return true;
  }
  return false;
}

// --- exact decimals ---------------------------------------------------------

// A number text as an exact rational n / d, with its decimal count.
function rational(text) {
  const [whole, fraction = ""] = text.split(".");
  return { n: BigInt(whole + fraction), d: 10n ** BigInt(fraction.length), places: fraction.length };
}

const subtract = (a, b) => ({ n: a.n * b.d - b.n * a.d, d: a.d * b.d });
const add = (a, b) => ({ n: a.n * b.d + b.n * a.d, d: a.d * b.d });
// a × 100 ÷ b, for b not zero.
const percentOf = (a, b) => ({ n: a.n * 100n * b.d, d: a.d * b.n });

// Half away from zero to `places` decimals; a zero result has no sign.
function decimalText({ n, d }, places) {
  if (d < 0n) [n, d] = [-n, -d];
  const negative = n < 0n;
  const units = (2n * (negative ? -n : n) * 10n ** BigInt(places) + d) / (2n * d);
  let digits = units.toString();
  if (places > 0) {
    digits = digits.padStart(places + 1, "0");
    digits = `${digits.slice(0, -places)}.${digits.slice(-places)}`;
  }
  return negative && units !== 0n ? `-${digits}` : digits;
}

// --- operations -------------------------------------------------------------

function mixedUnits(op, operands) {
  if (new Set(operands.map((o) => o.unit)).size === 1) return undefined;
  const seen = new Map(operands.map((o) => [o.id, o]));
  return `${op} mixes units: ${[...seen.values()].map((o) => `${escapeLine(o.id)} (${unitName(o.unit)})`).join(", ")}`;
}

function additiveRule(op, operands) {
  const percent = operands.find((o) => isPercentLike(o.unit));
  if (percent) return `${op} cannot take ${percent.unit} items; use pp-diff`;
  return mixedUnits(op, operands);
}

function productRule(op, [item, percent]) {
  if (isPercentLike(item.unit)) return `product cannot take a % or 个百分点 item first; ${escapeLine(item.id)} is in ${unitName(item.unit)}`;
  if (percent.unit !== PERCENT) return `product takes a % item second; ${escapeLine(percent.id)} is in ${unitName(percent.unit)}`;
  return undefined;
}

function percentsRule(op, operands) {
  const other = operands.find((o) => o.unit !== PERCENT);
  return other ? `pp-diff takes two % items; ${escapeLine(other.id)} is in ${unitName(other.unit)}` : undefined;
}

const firstUnit = ([first]) => first.unit;

// Each operation's operand rule, result unit, divisor (by operand
// position) and exact result.
const OPERATIONS = new Map([
  ["sum", { rule: additiveRule, unit: firstUnit, compute: (q) => q.reduce(add) }],
  ["diff", { rule: additiveRule, unit: firstUnit, compute: ([a, b]) => subtract(a, b) }],
  ["product", { rule: productRule, unit: firstUnit, compute: ([a, p]) => ({ n: a.n * p.n, d: a.d * p.d * 100n }) }],
  ["ratio", { rule: mixedUnits, unit: () => PERCENT, divisor: 1, compute: ([part, whole]) => percentOf(part, whole) }],
  ["growth", { rule: mixedUnits, unit: () => PERCENT, divisor: 1, compute: ([now, base]) => percentOf(subtract(now, base), base) }],
  ["pp-diff", { rule: percentsRule, unit: () => POINTS, compute: ([a, b]) => subtract(a, b) }],
]);

function countProblem(op, of) {
  if (of.length > OF_LIMIT) return `${escapeLine(op)} takes at most ${OF_LIMIT} items, got ${of.length}`;
  if (op === "sum") return of.length >= 2 ? undefined : `sum takes 2 or more items, got ${of.length}`;
  return of.length === 2 ? undefined : `${escapeLine(op)} takes 2 items, got ${of.length}`;
}

// Steps 4–6 and the result: a message, or the number text.
function derive(item, operands) {
  const { op } = item.derive;
  const operation = OPERATIONS.get(op);
  const problem = operation.rule(op, operands);
  if (problem) return { problem };
  const unit = operation.unit(operands);
  if (item.unit !== unit) return { problem: `its unit is ${unitName(item.unit)}, but ${op} gives ${unitName(unit)}` };
  const values = operands.map((o) => rational(o.text));
  if (operation.divisor !== undefined && values[operation.divisor].n === 0n) {
    return { problem: `${op} divides by ${escapeLine(operands[operation.divisor].id)}, which is 0` };
  }
  const exact = operation.compute(values);
  if (isPercentLike(unit)) {
    const text = decimalText(exact, 1);
    return { text: text.endsWith(".0") ? text.slice(0, -2) : text };
  }
  return { text: decimalText(exact, values.reduce((most, v) => (v.places > most ? v.places : most), 0)) };
}

// --- sources ----------------------------------------------------------------

// A `from` item's cell text, or the message of its first failing check.
function cellText({ table, row, column }, unit, tables) {
  if (!Object.hasOwn(tables, table)) {
    const names = Object.keys(tables);
    return { problem: `table ${escapeLine(table)} is not imported (imported: ${names.length === 0 ? "none" : names.map(escapeLine).join(", ")})` };
  }
  const { rows, columns } = tables[table];
  const found = rows.find((r) => r.label === row);
  if (found === undefined) {
    return { problem: `${escapeLine(row)} is not a row of ${escapeLine(table)} (rows: ${rows.map((r) => escapeLine(r.label)).join(", ")})` };
  }
  const position = columns.indexOf(column);
  if (position === -1) {
    return { problem: `${escapeLine(column)} is not a column of ${escapeLine(table)} (columns: ${columns.map(escapeLine).join(", ")})` };
  }
  const cell = found.cells[position] ?? null;
  const at = `${escapeLine(table)} row ${escapeLine(row)}, column ${escapeLine(column)}`;
  if (cell === null) return { problem: `${at} is empty` };
  if (cell.value === null || !VALUE_TEXT.test(cell.text)) return { problem: `${at} is text, not a number` };
  if (cell.unit === PERCENT && cell.unit !== unit) return { problem: `${at} is in %, but the item's unit is ${unitName(unit)}` };
  return { text: cell.text };
}

// Step 2 for a derive item: the operand count, then the first unknown id.
function operandsProblem({ op, of }, firstIndex) {
  const count = countProblem(op, of);
  if (count) return count;
  const unknown = of.find((id) => !firstIndex.has(id));
  return unknown === undefined ? undefined : `names unknown item ${escapeLine(unknown)}`;
}

// --- resolveData ------------------------------------------------------------

export function resolveData(data, tables) {
  if (data.length > DATA_LIMIT) {
    return { ok: false, errors: [{ index: DATA_LIMIT, id: data[DATA_LIMIT].id, message: `data: has ${data.length} items; the limit is ${DATA_LIMIT}` }] };
  }
  const firstIndex = new Map();
  data.forEach((item, index) => {
    if (!firstIndex.has(item.id)) firstIndex.set(item.id, index);
  });
  const messages = new Map();
  // undefined: not yet resolved; null: failed; else the resolved item.
  const results = new Array(data.length);
  const refs = data.map((item) => (Object.hasOwn(item, "derive") ? item.derive.of.map((id) => firstIndex.get(id)) : []));
  const fail = (index, message) => {
    messages.set(index, `data item ${escapeLine(data[index].id)}: ${message}`);
    results[index] = null;
  };

  // Step 7, for every item that resolved.
  const finish = (index, text, source) => {
    const item = data[index];
    const display = `${text}${item.unit}`;
    const length = [...display].length;
    if (length > FIGURE_LIMIT) {
      fail(index, `displays ${escapeLine(display)}, ${length} characters; the limit is ${FIGURE_LIMIT}, so ask the user for a larger unit`);
      return;
    }
    results[index] = { id: item.id, label: item.label, unit: item.unit, value: Number(text), text, display, source };
  };

  // Steps 1 and 2; a value or from item that passes resolves here.
  data.forEach((item, index) => {
    if (firstIndex.get(item.id) !== index) {
      fail(index, `is listed twice, at data[${firstIndex.get(item.id)}] and data[${index}]`);
    } else if (!UNIT.test(item.unit)) {
      fail(index, `unit ${escapeLine(item.unit)} holds a digit or starts with a space, a point or a sign`);
    } else if (Object.hasOwn(item, "derive")) {
      const problem = operandsProblem(item.derive, firstIndex);
      if (problem) fail(index, problem);
    } else if (Object.hasOwn(item, "from")) {
      const { problem, text } = cellText(item.from, item.unit, tables);
      if (problem) fail(index, problem);
      else finish(index, text, { table: item.from.table, row: item.from.row, column: item.from.column });
    } else if (quoteHolds(item.quote, item.value, item.unit)) {
      finish(index, item.value, { quote: item.quote });
    } else {
      fail(index, `${escapeLine(`${item.value}${item.unit}`)} does not occur in its quote`);
    }
  });

  // Step 3: walk `of` depth first. An item that failed is not walked, and a
  // loop sharing an item with one already found is not reported again.
  const walkable = (index) => results[index] === undefined && Object.hasOwn(data[index], "derive");
  const cyclic = new Set();
  const state = new Array(data.length);
  const path = [];
  const loop = (members) => {
    const known = members.some((m) => cyclic.has(m));
    for (const m of members) {
      cyclic.add(m);
      results[m] = null;
    }
    if (known) return;
    const start = members.indexOf(members.reduce((least, m) => (m < least ? m : least)));
    const ids = [...members.slice(start), ...members.slice(0, start)].map((m) => escapeLine(data[m].id));
    messages.set(members[start], `data: cycle ${[...ids, ids[0]].join(" → ")}`);
  };
  const walk = (index) => {
    state[index] = "path";
    path.push(index);
    for (const ref of refs[index]) {
      if (state[ref] === "path") loop(path.slice(path.indexOf(ref)));
      else if (state[ref] === undefined && walkable(ref)) walk(ref);
    }
    path.pop();
    state[index] = "done";
  };
  data.forEach((_, index) => {
    if (state[index] === undefined && walkable(index)) walk(index);
  });

  // Steps 4–7. Every remaining cycle passes through a cyclic item, which is
  // already null, so the recursion ends; an item over a failed operand is
  // not reported.
  const resolve = (index) => {
    if (results[index] !== undefined) return results[index];
    results[index] = null;
    const operands = refs[index].map(resolve);
    if (operands.includes(null)) return null;
    const item = data[index];
    const { problem, text } = derive(item, operands);
    if (problem) fail(index, problem);
    else finish(index, text, { op: item.derive.op, of: [...item.derive.of], operands: operands.map((o) => o.display) });
    return results[index];
  };
  data.forEach((_, index) => resolve(index));

  if (messages.size > 0) {
    const errors = [...messages.keys()].sort((a, b) => a - b).map((index) => ({ index, id: data[index].id, message: messages.get(index) }));
    return { ok: false, errors };
  }
  const items = Object.create(null);
  data.forEach((item, index) => {
    items[item.id] = results[index];
  });
  return { ok: true, items };
}

// The derived-item listing: `<id> = <op>(<operand display>, …) = <display>`.
export function derivedLines(items) {
  return Object.values(items)
    .filter((item) => Object.hasOwn(item.source, "op"))
    .map((item) => escapeLine(`${item.id} = ${item.source.op}(${item.source.operands.join(", ")}) = ${item.display}`));
}
