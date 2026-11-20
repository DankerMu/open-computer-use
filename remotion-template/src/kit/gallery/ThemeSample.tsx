// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The gallery's theme sample (task 4.1): the theme as the user approves it.
// It shows the four type sizes, each as one fixed Chinese line through
// wrapLines; the 12 brand slots as labelled swatches; and a caption page in
// the caption band, drawn by the real Captions layer. Everything sits inside
// the safe area and above the band, at or above the minimum size, so it
// probes clean in both themes. It lasts 90 frames and fades in over 15
// (THEME_SAMPLE).

import React from 'react';
import {interpolate, useCurrentFrame} from 'remotion';
import {HANG, textStyle, wrapLines} from '../text.ts';
import type {themes} from '../theme.ts';
import {Captions} from '../render/Captions.tsx';
import {captionBand} from '../render/captions.ts';
import {THEME_SAMPLE} from './catalog-rules.ts';

type Theme = (typeof themes)['light'];
type Slot = keyof Theme['brand'];

const LINE_WIDTH = 12;
const TYPE_LINES: Array<{size: 'title' | 'heading' | 'body' | 'caption'; text: string; top: number}> = [
	{size: 'title', text: '标题字号示例', top: 64},
	{size: 'heading', text: '小标题字号示例', top: 210},
	{size: 'body', text: '正文字号示例', top: 310},
	{size: 'caption', text: '字幕字号示例', top: 390},
];
const SLOTS: Slot[] = [
	'bg',
	'surface',
	'surface2',
	'border',
	'borderLit',
	'text',
	'dim',
	'faint',
	'accent',
	'accentSoft',
	'fontDisplay',
	'fontBody',
];
const FONT_SLOTS = new Set<Slot>(['fontDisplay', 'fontBody']);
// Six swatches per row across the 1728 px safe width, two rows from y = 460;
// each 240 × 120 with its label below, the second row ending at 870.4,
// above the caption band (930).
const COLUMNS = 6;
const CELL_WIDTH = 288;
const ROW_TOPS = [460, 690];
const SWATCH = {width: 240, height: 120};
const LABEL_GAP = 10;
const CAPTION = {text: '字幕在这里显示', startFrame: 0, endFrame: THEME_SAMPLE.durationInFrames};

const Swatch = ({slot, index, theme}: {slot: Slot; index: number; theme: Theme}) => {
	const left = 96 + (index % COLUMNS) * CELL_WIDTH;
	const top = ROW_TOPS[Math.floor(index / COLUMNS)];
	const font = FONT_SLOTS.has(slot);
	return (
		<>
			<div
				style={{
					position: 'absolute',
					left,
					top,
					width: SWATCH.width,
					height: SWATCH.height,
					boxSizing: 'border-box',
					border: `2px solid ${theme.brand.borderLit}`,
					backgroundColor: font ? theme.brand.surface : theme.brand[slot],
					display: 'flex',
					alignItems: 'center',
					justifyContent: 'center',
				}}
			>
				{font ? <div style={{fontFamily: theme.brand[slot], fontSize: theme.type.sizes.heading, lineHeight: 1}}>字</div> : null}
			</div>
			<div
				style={{
					position: 'absolute',
					left,
					top: top + SWATCH.height + LABEL_GAP,
					fontSize: theme.type.sizes.caption,
					lineHeight: theme.type.lineHeight,
					whiteSpace: 'nowrap',
					color: theme.brand.dim,
				}}
			>
				{slot}
			</div>
		</>
	);
};

export const ThemeSample = ({theme}: {theme: Theme}) => {
	const frame = useCurrentFrame();
	const opacity = interpolate(frame, [0, THEME_SAMPLE.entranceFrames], [0, 1], {
		extrapolateLeft: 'clamp',
		extrapolateRight: 'clamp',
	});
	const band = captionBand(theme);
	return (
		<div style={{position: 'absolute', inset: 0, opacity}}>
			{TYPE_LINES.map(({size, text, top}) => (
				<div
					key={size}
					data-kit-text={text}
					style={{
						...textStyle(theme),
						position: 'absolute',
						left: 96,
						top,
						width: `${LINE_WIDTH + HANG}em`,
						fontSize: theme.type.sizes[size],
						fontWeight: size === 'title' || size === 'heading' ? theme.type.weights.bold : theme.type.weights.regular,
					}}
				>
					{wrapLines(text, LINE_WIDTH).map((line, i) => (
						<div key={i}>{line}</div>
					))}
				</div>
			))}
			{SLOTS.map((slot, index) => (
				<Swatch key={slot} slot={slot} index={index} theme={theme} />
			))}
			<div
				style={{
					position: 'absolute',
					left: 96,
					right: 96,
					top: band.top,
					height: band.bottom - band.top,
					boxSizing: 'border-box',
					border: `2px dashed ${theme.brand.border}`,
				}}
			/>
			<Captions pages={[CAPTION]} theme={theme} />
		</div>
	);
};
