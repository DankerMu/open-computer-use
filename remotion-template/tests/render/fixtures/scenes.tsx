// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// Fixture scenes of the probe's browser tests (task 3.7). `fits` breaks no
// rule at any probe frame; every other scene breaks exactly one rule (two
// for `scroll`, whose box both scrolls and cuts its text). Positions are
// canvas pixels against the theme's safe area, 96–1824 × 54–1026.

import React from 'react';
import {interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import {fontPresent} from '../../../src/kit/probe/measure';
import {textStyle, wrapLines} from '../../../src/kit/text';
import {themes} from '../../../src/kit/theme';

const theme = themes.light;
const BODY = theme.type.sizes.body;
const PARAGRAPH = '光线从空气斜射入水中时，传播方向发生改变。';
const PARAGRAPH_WIDTH = 12;
// The two masks hold 50 px text, whose 70 px line box is whole pixels.
// Chrome puts a text's baseline on a whole pixel, so at 48 px (67.2 px line)
// the trimmed glyph box sits 0.6 px above the line box, past the 0.5 px
// tolerance, and a mask exactly the size of its line is reported.
const MASKED = {fontSize: 50} as const;

// One line of body text, never wrapped by CSS.
const line = {
	position: 'absolute',
	fontFamily: theme.brand.fontBody,
	fontSize: BODY,
	lineHeight: theme.type.lineHeight,
	letterSpacing: 0,
	whiteSpace: 'nowrap',
	color: theme.brand.text,
} as const;

const Fits = () => {
	const frame = useCurrentFrame();
	// A right-to-left reveal that ends at frame 15, the entrance end.
	const hidden = interpolate(frame, [0, 15], [100, 0], {extrapolateRight: 'clamp'});
	return (
		<>
			<div
				data-kit-text={PARAGRAPH}
				style={{...textStyle(theme), position: 'absolute', left: 200, top: 100, fontSize: BODY, width: `${PARAGRAPH_WIDTH + 2}em`}}
			>
				{wrapLines(PARAGRAPH, PARAGRAPH_WIDTH).map((text) => (
					<div key={text}>{text}</div>
				))}
			</div>
			<div style={{...line, left: 1824 - 480, top: 300, width: 480, textAlign: 'right'}}>右侧对齐</div>
			<div style={{...line, left: 200, top: 300, fontSize: theme.type.sizes.minimum}}>最小字号</div>
			<div style={{position: 'absolute', left: 200, top: 420, width: 200, height: 70, overflow: 'hidden'}}>
				<div style={{...line, ...MASKED, position: 'static'}}>遮罩文字</div>
			</div>
			<div style={{...line, ...MASKED, left: 200, top: 560, clipPath: `inset(0 ${hidden}% 0 0)`}}>揭示文字</div>
			<div style={{...textStyle(theme), position: 'absolute', left: 1000, top: 420, fontSize: BODY, width: '5.5em'}}>
				光在水中传播变慢
			</div>
			<div style={{...line, left: 20, top: 20, opacity: 0}}>隐藏文字</div>
		</>
	);
};

const EdgeOver = () => (
	<div style={{...line, left: 1825 - 480, top: 300, width: 480, textAlign: 'right'}}>右侧对齐</div>
);

const SmallFont = () => <div style={{...line, left: 200, top: 300, fontSize: 31}}>小于最小字号</div>;

const WrapStart = () => (
	<div style={{...textStyle(theme), position: 'absolute', left: 200, top: 300, fontSize: BODY, width: '4em', lineBreak: 'anywhere'}}>
		光的折射，速度
	</div>
);

const Scroll = () => (
	<div style={{...line, left: 200, top: 300, width: 400, overflowX: 'auto'}}>这段文字不换行并且比盒子更宽</div>
);

const WrapStartLines = () => (
	<div data-kit-text="光的折射，速度" style={{...line, left: 200, top: 300}}>
		<div>光的折射</div>
		<div>，速度</div>
	</div>
);

const ScaledFont = () => (
	<div style={{position: 'absolute', left: 200, top: 300, transform: 'scale(0.5)', transformOrigin: 'top left'}}>
		<div style={{...line, position: 'static'}}>缩放文字</div>
	</div>
);

const MaskSmall = () => (
	<>
		<div style={{position: 'absolute', left: 200, top: 300, width: 190, padding: '10px 0', overflow: 'hidden'}}>
			<div style={{...line, position: 'static', width: 200}}>四个汉字</div>
		</div>
		<div style={{...line, left: 600, top: 300, clipPath: 'circle(50%)'}}>圆形遮罩</div>
	</>
);

const TextDiffers = () => (
	<div data-kit-text="光的折射现象" style={{...line, left: 200, top: 300}}>
		光的折射
	</div>
);

// The spec's custom-scene scenario: unmarked text past the safe area's left
// edge, still inside the canvas.
const CustomOverflow = () => <div style={{...line, left: 60, top: 300}}>越过安全区</div>;

// Inside the safe area through the midpoint, past its right edge (still on
// the canvas) at the last frame. Driven by the scene's own length.
const LateOverflow = () => {
	const frame = useCurrentFrame();
	const {durationInFrames} = useVideoConfig();
	const half = Math.floor(durationInFrames / 2);
	const left = interpolate(frame, [half, durationInFrames - 1], [1400, 1700], {
		extrapolateLeft: 'clamp',
		extrapolateRight: 'clamp',
	});
	return <div style={{...line, left, top: 300}}>移动文字</div>;
};

// The installed family measured against one that does not exist, in the
// page the probe runs in.
const FontAbsent = () => <div style={{...line, left: 200, top: 300}}>{String(fontPresent('"No Such Font KP"'))}</div>;

const Throws = () => {
	throw new Error('fixture scene throws');
};

export const scenes = {
	fits: Fits,
	'edge-over': EdgeOver,
	'small-font': SmallFont,
	'wrap-start': WrapStart,
	scroll: Scroll,
	'wrap-start-lines': WrapStartLines,
	'scaled-font': ScaledFont,
	'mask-small': MaskSmall,
	'text-differs': TextDiffers,
	'custom-overflow': CustomOverflow,
	'late-overflow': LateOverflow,
	'font-absent': FontAbsent,
	throws: Throws,
};
