// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// Caption pages of the explainer: one page per caption block (design
// Decision 5). Each block becomes one @remotion/captions token with
// pageBreakAfter, so the library never joins two blocks into one page; its
// own trimming stands (a whitespace block gives no page, leading whitespace
// is removed). Imports only @remotion/captions, so Node's tests load it too.

import {createTikTokStyleCaptions} from '@remotion/captions';

type Block = {text: string; startFrame: number; endFrame: number};
type CaptionTimeline = {scenes: Array<{sentences: Array<{blocks: Block[]}>}>};
type CaptionTheme = {canvas: {height: number}; safeArea: {bottom: number}};
export type CaptionPage = {text: string; startFrame: number; endFrame: number};

const FPS = 30;
const BAND_HEIGHT = 96;

const toMs = (frame: number) => (frame * 1000) / FPS;
const toFrame = (ms: number) => Math.round((ms * FPS) / 1000);

export function captionPages(timeline: CaptionTimeline): CaptionPage[] {
	const captions = timeline.scenes.flatMap((scene) =>
		scene.sentences.flatMap((sentence) =>
			sentence.blocks.map(({text, startFrame, endFrame}) => ({
				text,
				startMs: toMs(startFrame),
				endMs: toMs(endFrame),
				timestampMs: null,
				confidence: null,
				pageBreakAfter: true,
			})),
		),
	);
	const {pages} = createTikTokStyleCaptions({captions, combineTokensWithinMilliseconds: 0});
	return pages.map((page) => {
		if (page.tokens.length !== 1) {
			throw new Error(`kit render: caption page ${JSON.stringify(page.text)} has ${page.tokens.length} tokens, expected 1`);
		}
		const [token] = page.tokens;
		return {text: page.text, startFrame: toFrame(token.fromMs), endFrame: toFrame(token.toMs)};
	});
}

// The bottom 96 px of the safe area. Scene components keep their text above
// `top`; that is a contract for the 4.2–4.4 components, not a probe rule.
export function captionBand(theme: CaptionTheme) {
	const bottom = theme.canvas.height - theme.safeArea.bottom;
	return {top: bottom - BAND_HEIGHT, bottom};
}
