// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The audio file name of each caption block. The stem binds the block's text,
// voice and voice instructions, so an edit to any of them asks for new audio.
// `kit voice`, `kit voice-import` and `kit timeline` all name files through
// blockStems; none recomputes a stem.

import { createHash } from "node:crypto";

// Takes a script returned by validateScript (end-card narration and voice
// filled in). Returns one {scene, sentence, block, text, stem} per caption
// block in playback order: scene is 1-based (hook 1, the chapter scenes, the
// summary, the end card), sentence and block are 0-based.
export function blockStems(script) {
  const { id, instructions } = script.voice;
  const scenes = [script.hook, ...script.chapters.flatMap((chapter) => chapter.scenes), script.summary, script.endCard];
  return scenes.flatMap((scene, s) =>
    scene.narration.flatMap((sentence, i) =>
      sentence.map((text, b) => {
        const hash = createHash("sha256").update(JSON.stringify([text, id, instructions])).digest("hex").slice(0, 8);
        return { scene: s + 1, sentence: i, block: b, text, stem: `s${s + 1}-${i}-${b}-${hash}` };
      }),
    ),
  );
}
