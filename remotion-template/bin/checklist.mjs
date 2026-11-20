// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
// Source: Vincentwei1021/video-shotcraft 42f922fe076f6428814d10f6184caf22a8b2adc2 references/aesthetic-rules.md Apache-2.0
//
// The fixed yes/no checklist that `kit status` prints after a passing check,
// to be answered from the contact sheet. The questions are adapted from the
// self-check questions of video-shotcraft's aesthetic rules: translated to
// English and reduced to what four still frames can answer. Each ends with
// the rule it comes from. No code is copied. The checklist is advisory: the
// kit records no answers.

export const CHECKLIST = Object.freeze([
  "Does each tile have one main subject that the eye goes to first? (Q5)",
  "Is all text meant to be read complete and easy to read at this size, with nothing cut off, overlapping or too small? (Q11)",
  "Is any small decorative text clearly decorative, so that no tile has text a viewer would try and fail to read? (Q11)",
  "Does each tile show what its scene's narration in script.json says? (C1)",
  "Does each tile add something new, with no two tiles showing the same picture or making the same point? (P4)",
  "Does every element sit in its place in the layout, with nothing floating over or covering another element by accident? (Q9)",
  "Are glows and highlights, if any, on the main subject only, at most one per tile, and kept inside its edges? (Q4)",
  "Do the illustrations and any mock content look finished and credible, with no placeholder, watermark or stray text? (Q1, Q10)",
  "Is the text in every tile seen straight on, not tilted at an angle that makes it hard to read? (Q6)",
  // P1 asks for one's own frame check before delivery; this is that check's
  // blank-frame item, the one the existing skill's contact-sheet step names.
  "Is every tile free of blank, black or unfinished-looking areas? (P1)",
]);
