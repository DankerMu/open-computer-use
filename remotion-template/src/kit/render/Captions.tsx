// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The caption layer: the page whose [startFrame, endFrame) holds the
// current frame, or nothing. Its wrapLines lines are each their own block
// in a centred box of (16 + HANG) em, at the theme's caption size, centred
// vertically in the caption band, with no background box. The element
// holding the lines carries the page text in data-kit-text, so the probe
// compares what renders with the block.

import React from 'react';
import {useCurrentFrame} from 'remotion';
import {HANG, textStyle, wrapLines} from '../text.ts';
import type {themes} from '../theme.ts';
import {captionBand, type CaptionPage} from './captions.ts';

type Theme = (typeof themes)['light'];

// A block is at most 16 characters (script-schema.mjs LIMITS.block).
const LINE_WIDTH = 16;

export const Captions = ({pages, theme}: {pages: CaptionPage[]; theme: Theme}) => {
	const frame = useCurrentFrame();
	const page = pages.find(({startFrame, endFrame}) => startFrame <= frame && frame < endFrame);
	if (page === undefined) return null;
	const band = captionBand(theme);
	return (
		<div
			style={{
				position: 'absolute',
				left: 0,
				right: 0,
				top: band.top,
				height: band.bottom - band.top,
				display: 'flex',
				alignItems: 'center',
				justifyContent: 'center',
			}}
		>
			<div
				data-kit-text={page.text}
				style={{
					...textStyle(theme),
					width: `${LINE_WIDTH + HANG}em`,
					fontSize: theme.type.sizes.caption,
					fontWeight: theme.type.weights.regular,
					color: theme.brand.text,
					textAlign: 'center',
				}}
			>
				{wrapLines(page.text, LINE_WIDTH).map((line, i) => (
					<div key={i}>{line}</div>
				))}
			</div>
		</div>
	);
};
