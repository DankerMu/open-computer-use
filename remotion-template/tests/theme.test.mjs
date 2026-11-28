// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The kit's theme tokens: per preset of src/kit/presets.ts, its pick in
// gallery/approvals.json (the explainer's is `light`, task 4.6), or its
// theme candidates while it has none (issue #962); Onda's 12 brand slots,
// the installed CJK font only, readable type, and WCAG contrast computed
// here from the colours themselves.

import { test } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { loadApprovals } from "../bin/gallery-approvals.mjs";
import { PRESETS } from "../src/kit/presets.ts";
import * as kit from "../src/kit/theme.ts";
import { SPRING_SMOOTH, SPRING_SNAPPY } from "../src/kit/onda/motion.ts";

const { themes } = kit;
const TEMPLATE = fileURLToPath(new URL("..", import.meta.url));

const BRAND_SLOTS = [
  "bg",
  "surface",
  "surface2",
  "border",
  "borderLit",
  "text",
  "dim",
  "faint",
  "accent",
  "accentSoft",
  "fontDisplay",
  "fontBody",
];
const HEX_SLOTS = ["bg", "surface", "surface2", "text", "dim", "accent"];
const OTHER_COLOUR_SLOTS = ["border", "borderLit", "faint", "accentSoft"];
const HEX = /^#[0-9a-fA-F]{6}$/;
const RGBA = /^rgba\((\d{1,3}), (\d{1,3}), (\d{1,3}), (0|1|0?\.\d+)\)$/;
const FONT = '"Noto Sans CJK SC"';
const CANDIDATES = Object.entries(themes);

function rgb(colour) {
  if (HEX.test(colour)) {
    return { channels: [1, 3, 5].map((i) => parseInt(colour.slice(i, i + 2), 16)), alpha: 1 };
  }
  const match = colour.match(RGBA);
  assert.ok(match, `unparsable colour ${colour}`);
  return { channels: match.slice(1, 4).map(Number), alpha: Number(match[4]) };
}

// An rgba foreground is composited over the opaque background it sits on.
function over(foreground, background) {
  const fg = rgb(foreground);
  const bg = rgb(background).channels;
  return fg.channels.map((c, i) => c * fg.alpha + bg[i] * (1 - fg.alpha));
}

function luminance(channels) {
  const [r, g, b] = channels.map((c) => {
    const v = c / 255;
    return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(foreground, background) {
  const a = luminance(over(foreground, background));
  const b = luminance(rgb(background).channels);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

test("per preset: its pick, or its candidates while it has none; the kit holds their union, with the token keys and the 12 brand slots", () => {
  const { presets } = loadApprovals(TEMPLATE);
  const union = [];
  for (const [preset, { themeCandidates }] of Object.entries(PRESETS)) {
    const pick = presets[preset].theme;
    if (preset === "explainer") {
      assert.equal(pick, "light");
      assert.deepEqual(Object.keys(themes.light).sort(), ["brand", "canvas", "motion", "safeArea", "spacing", "type"]);
    }
    if (pick === null) {
      assert.throws(() => kit.themeFor(preset), { message: `no approved theme for preset ${preset}` }, preset);
    } else {
      assert.equal(kit.themeFor(preset), themes[pick], preset);
    }
    for (const name of pick === null ? themeCandidates : [pick]) if (!union.includes(name)) union.push(name);
  }
  assert.deepEqual(Object.keys(themes), union);
  for (const [name, theme] of CANDIDATES) {
    assert.deepEqual(Object.keys(theme.brand).sort(), [...BRAND_SLOTS].sort(), name);
  }
});

test("each preset's theme candidates are keys of themes", () => {
  for (const [preset, { themeCandidates }] of Object.entries(PRESETS)) {
    for (const name of themeCandidates) assert.ok(Object.hasOwn(themes, name), `${preset}: ${name}`);
  }
});

test("themeFor: the explainer's is KIT_THEME, a preset without a pick throws, KIT_THEME stays themes.light", () => {
  assert.equal(typeof kit.themeFor, "function");
  assert.equal(kit.themeFor("explainer"), kit.KIT_THEME);
  assert.equal(kit.KIT_THEME, themes.light);
  assert.throws(() => kit.themeFor("report"), { message: "no approved theme for preset report" });
  assert.throws(() => kit.themeFor("constructor"), { message: "no approved theme for preset constructor" });
});

test("brand colours use #rrggbb or rgba(r, g, b, a)", () => {
  for (const [name, { brand }] of CANDIDATES) {
    for (const slot of HEX_SLOTS) assert.match(brand[slot], HEX, `${name}.${slot}`);
    for (const slot of OTHER_COLOUR_SLOTS) {
      assert.ok(HEX.test(brand[slot]) || RGBA.test(brand[slot]), `${name}.${slot} = ${brand[slot]}`);
      const { channels, alpha } = rgb(brand[slot]);
      assert.ok(channels.every((c) => c <= 255) && alpha <= 1, `${name}.${slot} out of range`);
    }
  }
});

test("canvas is 1920x1080 at 30 fps and the safe area keeps at least 80% by 80%", () => {
  for (const [name, { canvas, safeArea }] of CANDIDATES) {
    assert.deepEqual(canvas, { width: 1920, height: 1080, fps: 30 }, name);
    assert.deepEqual(Object.keys(safeArea).sort(), ["bottom", "left", "right", "top"], name);
    for (const side of Object.values(safeArea)) assert.ok(Number.isInteger(side) && side >= 0, name);
    assert.ok(canvas.width - safeArea.left - safeArea.right >= 0.8 * canvas.width, `${name} width`);
    assert.ok(canvas.height - safeArea.top - safeArea.bottom >= 0.8 * canvas.height, `${name} height`);
  }
});

test("both font slots are exactly the installed Noto Sans CJK SC", () => {
  for (const [name, { brand }] of CANDIDATES) {
    assert.equal(brand.fontDisplay, FONT, name);
    assert.equal(brand.fontBody, FONT, name);
  }
});

test("type: weights, sizes at or above a minimum of at least 32, line height and letter spacing", () => {
  for (const [name, { type }] of CANDIDATES) {
    assert.deepEqual(type.weights, { regular: 400, bold: 700 }, name);
    assert.deepEqual(Object.keys(type.sizes).sort(), ["body", "caption", "heading", "minimum", "title"], name);
    assert.ok(type.sizes.minimum >= 32, `${name} minimum`);
    for (const [key, size] of Object.entries(type.sizes)) {
      assert.ok(size >= type.sizes.minimum, `${name}.${key} = ${size}`);
    }
    assert.ok(type.lineHeight >= 1.25, `${name} lineHeight`);
    assert.equal(type.letterSpacing, 0, name);
  }
});

test("spacing is strictly increasing", () => {
  for (const [name, { spacing }] of CANDIDATES) {
    assert.ok(Array.isArray(spacing) && spacing.length >= 2, name);
    for (let i = 1; i < spacing.length; i += 1) assert.ok(spacing[i] > spacing[i - 1], `${name} spacing[${i}]`);
  }
});

test("motion uses Onda's own springs", () => {
  for (const [name, { motion }] of CANDIDATES) {
    assert.equal(motion.springs.smooth, SPRING_SMOOTH, name);
    assert.equal(motion.springs.snappy, SPRING_SNAPPY, name);
  }
});

test("WCAG contrast: text and dim at 4.5 on every surface, accent and faint at 3 on bg and surface", () => {
  for (const [name, { brand }] of CANDIDATES) {
    for (const fg of ["text", "dim"]) {
      for (const bg of ["bg", "surface", "surface2"]) {
        const ratio = contrast(brand[fg], brand[bg]);
        assert.ok(ratio >= 4.5, `${name} ${fg} on ${bg}: ${ratio.toFixed(2)}`);
      }
    }
    for (const fg of ["accent", "faint"]) {
      for (const bg of ["bg", "surface"]) {
        const ratio = contrast(brand[fg], brand[bg]);
        assert.ok(ratio >= 3, `${name} ${fg} on ${bg}: ${ratio.toFixed(2)}`);
      }
    }
  }
});
