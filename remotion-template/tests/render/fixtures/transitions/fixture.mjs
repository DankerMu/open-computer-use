// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The task 4.5 fixture: 3.8's fixture script with chapter 1 holding two
// scenes, the first of three blocks, laid out by 3.8's fixtureTimeline (every
// block 24000 samples). It totals 550 frames:
//   hook 0–134 (narration ends 89, window 119–133),
//   question 134–203 (blocks [134, 149), [150, 166), [167, 183); window 188–202),
//   concept card 203–263 (window 248–262),
//   custom 263–340 (window 325–339),
//   concept card 340–400 (window 385–399),
//   summary 400–460 (window 445–459),
//   end card 460–550 (no window).
// coverage() names the three gap cases the transition test needs; it throws
// when the edit stops producing one of them.

import { GAPS } from "../../../../bin/scene-timing.mjs";
import { fixtureScript, fixtureTimeline } from "../explainer/fixture.mjs";

export function transitionsScript() {
  return fixtureScript((raw) => {
    raw.chapters[0].scenes[0].narration = [["筷子看起来折断", "插进水里的部分", "像是向上弯了"]];
    raw.chapters[0].scenes.push({
      kind: "concept-card",
      narration: [["光换了介质"]],
      content: { title: "折射", points: ["光路弯折"] },
    });
  });
}

export function transitionsTimeline(script = transitionsScript()) {
  return fixtureTimeline(script);
}

// The timeline's scenes, each with `nextIsChapterFirst`, for the boundary
// cases below.
function boundaries(timeline) {
  return timeline.scenes.slice(0, -1).map((scene, i) => {
    const next = timeline.scenes[i + 1];
    return { scene, nextIsChapterFirst: next.slot === "chapter" && next.scene === 1 };
  });
}

// {sceneEnd, chapterStart, stretched}: the index (in timeline.scenes) of
//   - a scene whose next is not a chapter's first, ending 20 frames after
//     its narration;
//   - a scene whose next is a chapter's first, ending 45 frames after its
//     narration, not stretched by minFrames;
//   - a scene whose narration plus its nominal gap is strictly shorter than
//     minFrames, so minFrames sets its end.
export function coverage(timeline) {
  const cases = boundaries(timeline).map(({ scene, nextIsChapterFirst }) => {
    const gap = scene.endFrame - scene.narrationEndFrame;
    const nominal = nextIsChapterFirst ? GAPS.chapterStart : GAPS.sceneEnd;
    const minEnd = scene.startFrame + scene.minFrames;
    return {
      sceneEnd: !nextIsChapterFirst && gap === GAPS.sceneEnd,
      chapterStart: nextIsChapterFirst && gap === GAPS.chapterStart && scene.narrationEndFrame + GAPS.chapterStart >= minEnd,
      stretched: scene.narrationEndFrame + nominal < minEnd,
    };
  });
  const found = {};
  for (const name of ["sceneEnd", "chapterStart", "stretched"]) {
    const index = cases.findIndex((c) => c[name]);
    if (index === -1) throw new Error(`the transitions fixture has no ${name} boundary`);
    found[name] = index;
  }
  return found;
}
