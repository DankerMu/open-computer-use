// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// Caption pages: one page per caption block (design Decision 5), through
// @remotion/captions with pageBreakAfter on every block's token. Expected
// values come from the task 3.8 fixture: the fixture timeline's eight
// blocks, frames at 30 fps, the library's own trimming as measured on
// 4.0.537, and the caption band, the bottom 96 px of the safe area.

import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { createTikTokStyleCaptions } from "@remotion/captions";
import { captionBand, captionPages } from "../src/kit/render/captions.ts";
import { themes } from "../src/kit/theme.ts";
import { fixtureScript, fixtureTimeline } from "./render/fixtures/explainer/fixture.mjs";

const FIXTURE_PAGES = [
  { text: "光为什么", startFrame: 40, endFrame: 55 },
  { text: "会弯折", startFrame: 56, endFrame: 72 },
  { text: "在水里", startFrame: 73, endFrame: 89 },
  { text: "筷子看起来折断", startFrame: 134, endFrame: 149 },
  { text: "速度变了", startFrame: 194, endFrame: 209 },
  { text: "方向就变", startFrame: 210, endFrame: 226 },
  { text: "透镜利用折射", startFrame: 271, endFrame: 286 },
  { text: "记住这一点", startFrame: 331, endFrame: 346 },
];

// A timeline holding only the given blocks, in one sentence of one scene.
function timelineOf(blocks) {
  return { scenes: [{ sentences: [{ blocks }] }] };
}

// The fixture's tokens, built here from its formula.
function tokens(timeline, pageBreakAfter) {
  return timeline.scenes.flatMap((scene) =>
    scene.sentences.flatMap((sentence) =>
      sentence.blocks.map(({ text, startFrame, endFrame }) => ({
        text,
        startMs: (startFrame * 1000) / 30,
        endMs: (endFrame * 1000) / 30,
        timestampMs: null,
        confidence: null,
        pageBreakAfter,
      })),
    ),
  );
}

test("the fixture timeline gives eight pages, one per block, the hook's three first", () => {
  const pages = captionPages(fixtureTimeline(fixtureScript()));
  assert.deepEqual(pages, FIXTURE_PAGES);
  assert.deepEqual(
    pages.slice(0, 3).map(({ text }) => text),
    ["光为什么", "会弯折", "在水里"],
  );
});

test("the same tokens without pageBreakAfter give one page", () => {
  const timeline = fixtureTimeline(fixtureScript());
  const options = { combineTokensWithinMilliseconds: 0 };
  assert.equal(createTikTokStyleCaptions({ captions: tokens(timeline, true), ...options }).pages.length, 8);
  const { pages } = createTikTokStyleCaptions({ captions: tokens(timeline, false), ...options });
  assert.equal(pages.length, 1);
  assert.equal(pages[0].text, FIXTURE_PAGES.map(({ text }) => text).join(""));
});

test("the library's trimming stands: a space block gives no page and leading space is removed", () => {
  assert.deepEqual(captionPages(timelineOf([{ text: " ", startFrame: 0, endFrame: 10 }])), []);
  assert.deepEqual(captionPages(timelineOf([{ text: " 光", startFrame: 0, endFrame: 10 }])), [{ text: "光", startFrame: 0, endFrame: 10 }]);
  assert.deepEqual(
    captionPages(
      timelineOf([
        { text: "光", startFrame: 0, endFrame: 10 },
        { text: " ", startFrame: 11, endFrame: 20 },
        { text: " 水", startFrame: 21, endFrame: 30 },
      ]),
    ),
    [
      { text: "光", startFrame: 0, endFrame: 10 },
      { text: "水", startFrame: 21, endFrame: 30 },
    ],
  );
});

test("every frame from 0 to 5400 round-trips exactly", () => {
  const blocks = Array.from({ length: 5400 }, (_, f) => ({ text: "光", startFrame: f, endFrame: f + 1 }));
  const pages = captionPages(timelineOf(blocks));
  assert.equal(pages.length, 5400);
  pages.forEach((page, f) => {
    assert.equal(page.startFrame, f);
    assert.equal(page.endFrame, f + 1);
  });
});

test("the caption band is the bottom 96 px of the safe area", () => {
  assert.deepEqual(captionBand(themes.light), { top: 930, bottom: 1026 });
});

test("@remotion/captions resolves to 4.0.537", () => {
  const require = createRequire(new URL("../src/kit/render/captions.ts", import.meta.url));
  assert.equal(require("@remotion/captions/package.json").version, "4.0.537");
});
