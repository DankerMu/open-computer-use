// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// Where the scene components (task 4.2) lay out, in canvas pixels on the
// 1920×1080 canvas. Every box lies inside the theme's safe area
// (96–1824 × 54–1026) and ends at y = 880, above the caption band
// (930–1026), which captions alone use. Plain TypeScript, so Node imports it.
//
// TEXT_BOX is the text region of a scene without an image. With an image,
// the text keeps the left column and the image panel takes the right: the
// column is 1024 px wide so that a concept card's 16-character point at body
// size stays on one line beside its bullet, and the 640 px square panel
// letterboxes a 4:3 image above and below.

export type Box = {left: number; top: number; width: number; height: number};

export const TEXT_BOX: Box = {left: 96, top: 96, width: 1728, height: 784};

export const IMAGE_BOX: {text: Box; panel: Box} = {
	text: {left: 96, top: 96, width: 1024, height: 784},
	panel: {left: 1184, top: 168, width: 640, height: 640},
};
