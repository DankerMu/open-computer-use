// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The kit's Chinese text helper. Expected values come from the task 3.3
// fixture: units keep ASCII runs, numbers with 万/亿 and punctuation whole;
// wrapped lines join back to the input, never start with a closing mark or
// end with an opening one, and exceed their width only by a hang of at most
// HANG em of trailing closing marks. Line widths are recomputed here from the
// line text alone.

import { test } from "node:test";
import assert from "node:assert/strict";
import {
  CLOSING_PUNCTUATION,
  HANG,
  OPENING_PUNCTUATION,
  USE_INTL_SEGMENTER,
  formatCount,
  textStyle,
  units,
  width,
  wrapLines,
} from "../src/kit/text.ts";
import { themes } from "../src/kit/theme.ts";

const MODES = [false, true];
const EPSILON = 1e-9;
const CLOSING = new Set(CLOSING_PUNCTUATION);
const OPENING = new Set(OPENING_PUNCTUATION);

function sum(text) {
  return Array.from(text).reduce((total, ch) => total + width(ch), 0);
}

// The line's trailing run of closing marks and U+0020 hangs its closing
// marks, up to HANG em. Only the spaces after the last other character are
// left out; a space before a closing mark is drawn and counts.
function trailingRun(line) {
  const chars = Array.from(line);
  let start = chars.length;
  while (start > 0 && (CLOSING.has(chars[start - 1]) || chars[start - 1] === " ")) start -= 1;
  return chars.slice(start);
}

function lineWidth(line) {
  const closing = trailingRun(line)
    .filter((ch) => CLOSING.has(ch))
    .reduce((total, ch) => total + width(ch), 0);
  return fullWidth(line) - Math.min(closing, HANG);
}

// The visible line: trailing spaces take no room at a line end.
function fullWidth(line) {
  return sum(line.replace(/ +$/, ""));
}

function assertLineRules(text, lines, maxWidth, label) {
  assert.equal(lines.join(""), text, `${label}: lines join to the input`);
  for (const line of lines) {
    const chars = Array.from(line);
    assert.ok(line.length > 0, `${label}: empty line`);
    assert.ok(!CLOSING.has(chars[0]), `${label}: line starts with a closing mark: ${JSON.stringify(line)}`);
    const last = Array.from(line.replace(/ +$/, "")).at(-1);
    assert.ok(!OPENING.has(last), `${label}: line ends with an opening mark: ${JSON.stringify(line)}`);
    assert.ok(lineWidth(line) <= maxWidth + EPSILON, `${label}: ${JSON.stringify(line)} is ${lineWidth(line)} wide`);
    assert.ok(fullWidth(line) <= maxWidth + HANG + EPSILON, `${label}: ${JSON.stringify(line)} overhangs`);
  }
}

test("punctuation sets, HANG and the segmenter flag", () => {
  assert.equal(CLOSING_PUNCTUATION, "，。！？、；：）」』”’》】…%.,!?;:)]}");
  assert.equal(OPENING_PUNCTUATION, "（「『“‘《【([{");
  assert.equal(HANG, 2);
  assert.equal(typeof USE_INTL_SEGMENTER, "boolean");
});

test("the default mode is the probe's: Intl.Segmenter words", () => {
  // The probe found multi-character words such as 折射 in the image's shell.
  assert.equal(USE_INTL_SEGMENTER, true);
  const text = "我们今天学习";
  assert.deepEqual(wrapLines(text, 3, { segmenter: true }), ["我们", "今天", "学习"]);
  assert.deepEqual(wrapLines(text, 3, { segmenter: false }), ["我们今", "天学习"]);
  assert.deepEqual(wrapLines(text, 3), wrapLines(text, 3, { segmenter: true }));
  assert.deepEqual(units(text), units(text, { segmenter: true }));
});

test("width: one em for non-ASCII, one shared width for printable ASCII", () => {
  // 1 is the probe's calibration: the largest advance of [A-Z0-9%] at 1 em
  // over the theme's weights, 400 (0.9210) and 700 (0.9630), rounded up to 0.05.
  for (const ch of ["中", "，", "“", "…", "Ａ", "　"]) assert.equal(width(ch), 1, ch);
  const ascii = Array.from({ length: 0x7f - 0x20 }, (_, i) => String.fromCharCode(0x20 + i));
  for (const ch of ascii) assert.equal(width(ch), 1, JSON.stringify(ch));
});

test("units keep ASCII runs and numbers with 万/亿 whole", () => {
  const rows = [
    ["30万公里", ["30万", "公", "里"]],
    ["3.5亿年前", ["3.5亿", "年", "前"]],
    ["COVID-19 疫苗", ["COVID-19 ", "疫", "苗"]],
    ["2024-11-17", ["2024-11-17"]],
    ["e.g. 例如", ["e.g. ", "例", "如"]],
    ["CO2", ["CO2"]],
  ];
  for (const [text, expected] of rows) {
    assert.deepEqual(units(text, { segmenter: false }), expected, text);
    const segmented = units(text, { segmenter: true });
    assert.equal(segmented.join(""), text, `${text} (segmenter)`);
    assert.equal(segmented[0], expected[0], `${text} (segmenter) keeps its ASCII unit`);
    for (const segmenter of MODES) {
      for (const maxWidth of [12, 16]) {
        const lines = wrapLines(text, maxWidth, { segmenter });
        assertLineRules(text, lines, maxWidth, `${text} @${maxWidth} segmenter=${segmenter}`);
        assert.ok(lines.some((line) => line.includes(expected[0].trim())), `${text} @${maxWidth} splits its run`);
      }
    }
  }
  // The same runs inside a text that does wrap at 12 and 16.
  const text = "据统计，约30万公里的光纤在2024-11-17前完成铺设；COVID-19 疫苗覆盖3.5亿人，e.g. 例如CO2排放也被记录。";
  for (const segmenter of MODES) {
    for (const maxWidth of [12, 16]) {
      const lines = wrapLines(text, maxWidth, { segmenter });
      assert.ok(lines.length > 1, `@${maxWidth} wraps`);
      assertLineRules(text, lines, maxWidth, `@${maxWidth} segmenter=${segmenter}`);
      for (const run of ["30万", "2024-11-17", "COVID-19", "3.5亿", "e.g.", "CO2"]) {
        assert.ok(lines.some((line) => line.includes(run)), `@${maxWidth} segmenter=${segmenter} splits ${run}`);
      }
    }
  }
});

test("万/亿 join only a digit, and a sign joins only a following digit", () => {
  for (const segmenter of MODES) {
    assert.deepEqual(units("a万", { segmenter }), ["a", "万"]);
    assert.deepEqual(units("+5亿", { segmenter }), ["+5亿"]);
    assert.deepEqual(units("a+1", { segmenter }), ["a", "+1"]);
  }
});

test("spaces before closing marks are drawn and count toward the line", () => {
  const rows = [
    ["研究使用 COVID-19 ）。结果", 8],
    ["一二三四五六七八 ……", 8],
    ["一二三四五六 ）。后", 6],
    ["wait ... ok", 5],
    ["He paused . . . then left", 8],
  ];
  for (const [text, maxWidth] of rows) {
    for (const segmenter of MODES) {
      assertLineRules(text, wrapLines(text, maxWidth, { segmenter }), maxWidth, `${text} @${maxWidth} segmenter=${segmenter}`);
    }
  }
});

test("a split piece still wider than maxWidth throws RangeError naming it", () => {
  const rows = [
    ["太好了！！！！！！！！！！", 8, "了！！！！！！！！！！"],
    ["他说：“……………”", 4, "“……………”"],
    ["他说：“……”。", 2, "“……”。"],
    ["《「好」》。！", 4, "《「好」》。！"],
  ];
  for (const [text, maxWidth, piece] of rows) {
    for (const segmenter of MODES) {
      assert.throws(
        () => wrapLines(text, maxWidth, { segmenter }),
        (error) => error instanceof RangeError && error.message.includes(piece) && error.message.includes(String(maxWidth)),
        `${text} @${maxWidth} segmenter=${segmenter}`,
      );
    }
  }
});

test("opening marks and the spaces after them bind to the next character", () => {
  for (const segmenter of MODES) {
    // The leading run of opening marks and spaces alone fills the width.
    assert.throws(() => wrapLines("（ abc", 2, { segmenter }), RangeError);
    assert.throws(() => wrapLines("《 「 字字", 4, { segmenter }), RangeError);
    assert.deepEqual(wrapLines("（ abc", 3, { segmenter }), ["（ a", "bc"]);
    assert.deepEqual(wrapLines("（ abc", 4, { segmenter }), ["（ ab", "c"]);
    for (const maxWidth of [5, 6, 8]) {
      assertLineRules("《 「 字字", wrapLines("《 「 字字", maxWidth, { segmenter }), maxWidth, `@${maxWidth}`);
    }
  }
});

test("quotes and brackets bind to their characters and a long closing run hangs only 2 em", () => {
  const text = "“光”（折射）……。」";
  assert.deepEqual(units(text, { segmenter: false }), ["“光”", "（折", "射）……。」"]);
  for (const segmenter of MODES) {
    const found = units(text, { segmenter });
    assert.equal(found.join(""), text);
    assert.equal(found[0], "“光”");
    assert.ok(found.at(-1).endsWith("射）……。」"));
    // At width 4 the closing run （5 em） hangs 2 em and its other 3 em count:
    // 射 plus 3 em is 4, so it cannot follow 2 em of （折 on one line.
    const lines = wrapLines(text, 4, { segmenter });
    assert.deepEqual(lines, ["“光”", "（折", "射）……。」"], `segmenter=${segmenter}`);
    assert.equal(lineWidth(lines[2]), 4);
    assert.equal(sum(lines[2]), 4 + HANG);
    assertLineRules(text, lines, 4, `segmenter=${segmenter}`);
  }
});

test("a quoted ellipsis after a colon wraps without error", () => {
  const text = "他说：“……”";
  for (const segmenter of MODES) {
    for (const maxWidth of [2, 3, 4, 8]) {
      assertLineRules(text, wrapLines(text, maxWidth, { segmenter }), maxWidth, `@${maxWidth} segmenter=${segmenter}`);
    }
  }
  assert.deepEqual(wrapLines(text, 2, { segmenter: false }), ["他", "说：", "“……”"]);
});

test("a hung closing run counts once another unit follows it", () => {
  assert.deepEqual(wrapLines("我们光……是。」", 4, { segmenter: false }), ["我们光……", "是。」"]);
});

test("units of bracket-only, blank and punctuation-only text keep every character", () => {
  assert.deepEqual(units("空（）字", { segmenter: false }), ["空", "（）字"]);
  assert.deepEqual(units("「 」", { segmenter: false }), ["「 」"]);
  assert.deepEqual(units("……", { segmenter: false }), ["……"]);
  for (const segmenter of MODES) {
    for (const text of ["空（）字", "「 」", "……"]) {
      assert.equal(units(text, { segmenter }).join(""), text);
      assert.equal(wrapLines(text, 4, { segmenter }).join(""), text);
    }
    assert.deepEqual(units("", { segmenter }), []);
    assert.deepEqual(wrapLines("", 4, { segmenter }), []);
  }
});

test("a formatted negative count stays whole", () => {
  const count = formatCount(-12345);
  assert.equal(count, "-1.2万");
  const text = `人口增长${count}人`;
  for (const segmenter of MODES) {
    assert.ok(units(text, { segmenter }).includes(count), `segmenter=${segmenter}`);
    for (const maxWidth of [6, 8, 12]) {
      const lines = wrapLines(text, maxWidth, { segmenter });
      assertLineRules(text, lines, maxWidth, `@${maxWidth} segmenter=${segmenter}`);
      assert.ok(lines.some((line) => line.includes(count)), `@${maxWidth} segmenter=${segmenter}`);
    }
  }
});

test("a leading space joins the first unit", () => {
  assert.deepEqual(units(" 你好", { segmenter: false }), [" 你", "好"]);
  for (const segmenter of MODES) {
    const [first] = units(" 你好", { segmenter });
    assert.ok(first.startsWith(" 你"), `segmenter=${segmenter}: ${JSON.stringify(first)}`);
  }
});

test("closing marks a naive character-count break would push to a line start stay on the line before", () => {
  const text = "我们今天，好的好。」";
  const naive = text.match(/.{1,4}/gu);
  assert.deepEqual(naive, ["我们今天", "，好的好", "。」"]);
  for (const segmenter of MODES) {
    assert.deepEqual(wrapLines(text, 4, { segmenter }), ["我们今天，", "好的好。」"], `segmenter=${segmenter}`);
  }
});

test("a long quoted English word after a 7/8-full line starts a new line and splits without a dangling quote", () => {
  const head = "一二三四五六七";
  const word = "“abcdefghijklmnopqrst”";
  const text = head + word;
  for (const segmenter of MODES) {
    const lines = wrapLines(text, 8, { segmenter });
    assert.equal(lines[0], head, `segmenter=${segmenter}`);
    assert.ok(lines.length >= 3, `segmenter=${segmenter}: the word is split`);
    assert.ok(lines[1].startsWith("“a"), `segmenter=${segmenter}`);
    for (const line of lines) assert.ok(!line.endsWith("“"), `segmenter=${segmenter}: ${line}`);
    assertLineRules(text, lines, 8, `segmenter=${segmenter}`);
  }
});

test("long mixed strings keep every rule at widths 8, 12 and 16", () => {
  const text =
    "在2024-11-17的发布会上，研究团队（来自3所大学）宣布：“光在水中的速度约为每秒22.5万公里。”" +
    "这一结果引发了讨论！为什么？因为它与教科书的数值相差不到0.1%；有人说「这很正常」，也有人认为『需要复核』……" +
    "他们引用了《物理学报》【第42卷】的数据、图表和附录。In the paper, the authors wrote: " +
    '"We measured it twice (with care)." Results: 98.6% match, error < 0.5%; really? Yes! ' +
    "See [Table 3] and {Note 7}, then the ‘summary’. COVID-19 疫苗的研究也用到了类似方法，" +
    "样本量达到123456789人次，约1.2亿，覆盖30万个社区。";
  assert.ok(Array.from(text).length > 200);
  for (const ch of CLOSING_PUNCTUATION) assert.ok(text.includes(ch), `missing closing mark ${ch}`);
  // Every closing mark after a space, alone and in pairs, behind Han and
  // ASCII words: the spaces are drawn mid-run and count.
  const marks = Array.from(CLOSING_PUNCTUATION);
  const spaced = marks
    .map((ch, i) => `${i % 2 ? "数据" : "data"} ${ch}${i % 3 ? "" : ` ${marks[(i + 5) % marks.length]}`}`)
    .join("后来");
  for (const sample of [text, `${text}${spaced}结束`]) {
    for (const segmenter of MODES) {
      for (const maxWidth of [8, 12, 16]) {
        const lines = wrapLines(sample, maxWidth, { segmenter });
        assert.ok(lines.length > 1);
        assertLineRules(sample, lines, maxWidth, `@${maxWidth} segmenter=${segmenter}`);
      }
    }
  }
});

test("formatCount uses 万 and 亿, truncating toward zero to one decimal", () => {
  const rows = [
    [9999, "9999"],
    [12345, "1.2万"],
    [20000, "2万"],
    [99999999, "9999.9万"],
    [123456789, "1.2亿"],
    [-12345, "-1.2万"],
    [100000000, "1亿"],
    [-9999, "-9999"],
    [-0, "0"],
    [0, "0"],
  ];
  for (const [n, expected] of rows) assert.equal(formatCount(n), expected, String(n));
  for (const n of [NaN, Infinity, -Infinity]) assert.throws(() => formatCount(n), TypeError, String(n));
});

test("bad widths and control characters throw RangeError", () => {
  assert.throws(() => wrapLines("文字", 1), RangeError);
  assert.throws(() => wrapLines("文字", 2.5), RangeError);
  assert.throws(() => units("a\nb"), RangeError);
  assert.throws(() => units("a\tb"), RangeError);
  assert.throws(() => wrapLines("a\nb", 8), RangeError);
  // An oversized unit whose leading opening marks alone fill the width.
  assert.throws(() => wrapLines("《「好", 2, { segmenter: false }), RangeError);
});

test("textStyle reads the theme and never forbids wrapping", () => {
  for (const theme of Object.values(themes)) {
    assert.deepEqual(textStyle(theme), {
      fontFamily: theme.brand.fontBody,
      lineHeight: theme.type.lineHeight,
      letterSpacing: 0,
      whiteSpace: "normal",
      wordBreak: "normal",
      overflowWrap: "anywhere",
      lineBreak: "strict",
    });
  }
});
