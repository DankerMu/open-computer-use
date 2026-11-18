// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// Fixture scenes of the probe's browser tests (task 3.7). `fits` breaks no
// rule at any probe frame; every other scene breaks exactly one rule (two
// for `scroll`, whose box both scrolls and cuts its text). Positions are
// canvas pixels against the theme's safe area, 96–1824 × 54–1026.

import React, {useEffect, useState} from 'react';
import {Img, continueRender, delayRender, interpolate, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';
import {fontPresent} from '../../../src/kit/probe/measure';
import {textStyle, wrapLines} from '../../../src/kit/text';
import {themes} from '../../../src/kit/theme';

const theme = themes.light;
const BODY = theme.type.sizes.body;
const PARAGRAPH = '光线从空气斜射入水中时，传播方向发生改变。';
const PARAGRAPH_WIDTH = 12;

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
			{/* Masks exactly the size of their 48 px line: the trimmed glyph
			    box sits 0.6 px above the line box, inside the 1 px vertical
			    tolerance. */}
			<div style={{position: 'absolute', left: 200, top: 420, width: 192, height: 67.2, overflow: 'hidden'}}>
				<div style={{...line, position: 'static'}}>遮罩文字</div>
			</div>
			<div style={{...line, left: 200, top: 560, clipPath: `inset(0 ${hidden}% 0 0)`}}>揭示文字</div>
			<div style={{...textStyle(theme), position: 'absolute', left: 1000, top: 420, fontSize: BODY, width: '5.5em'}}>
				光在水中传播变慢
			</div>
			<div style={{...line, left: 20, top: 20, opacity: 0}}>隐藏文字</div>
			{/* SVG text at viewBox scale 1: 48 px rendered. */}
			<svg viewBox="0 0 400 100" width="400" height="100" style={{position: 'absolute', left: 1000, top: 700}}>
				<text x="0" y="70" fontSize="48" fontFamily={theme.brand.fontBody}>
					矢量文字
				</text>
			</svg>
			{/* The overflow mask of 4 above, drawn at scale 2: the 0.6 px
			    offset becomes 1.2 px, inside the scaled 2 px tolerance. */}
			<div style={{position: 'absolute', left: 1400, top: 840, transform: 'scale(2)', transformOrigin: 'top left'}}>
				<div style={{width: 192, height: 67.2, overflow: 'hidden'}}>
					<div style={{...line, position: 'static'}}>放大遮罩</div>
				</div>
			</div>
			{/* Hidden through an ancestor, placed outside the safe area. */}
			<div style={{position: 'absolute', left: 20, top: 980, opacity: 0}}>
				<div style={{...line, position: 'static'}}>祖先透明</div>
			</div>
			<div style={{position: 'absolute', left: 1800, top: 980, visibility: 'hidden'}}>
				<div style={{...line, position: 'static'}}>祖先隐藏</div>
			</div>
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

// A viewport (as a camera pans) holding text wholly outside it, past the
// safe area: the text is not shown, so no rule applies.
const Camera = () => (
	<div style={{position: 'absolute', left: 200, top: 300, width: 400, height: 100, overflow: 'hidden'}}>
		<div style={{...line, left: 1700, top: 0}}>镜头外文字</div>
	</div>
);

// Negative insets reach past the 96 px border box around the 240 px text.
const ClipNegative = () => (
	<div style={{...line, left: 200, top: 200, width: 96, clipPath: 'inset(-10px -200px -10px -10px)'}}>负值裁剪框</div>
);

// A smaller inline span sets the record's minimum font size.
const InlineSmall = () => (
	<div style={{...line, left: 200, top: 300}}>
		正文<span style={{fontSize: 24}}>注</span>大字
	</div>
);

// Collapsible spaces before the comma render nothing.
const WrapStartSpaces = () => (
	<div data-kit-text="光的折射  ，速度" style={{...line, left: 200, top: 300}}>
		<div>光的折射</div>
		<div>{'  ，速度'}</div>
	</div>
);

// public/late.png is 1000 x 1000 (one colour, 4.4 KB). Before it loads its
// height is 0 and the text sits inside the safe area; once loaded the text
// lies past the bottom edge. A tiny image loaded before a measurement that
// did not wait for it; this one does not.
const LateImage = () => (
	<div style={{position: 'absolute', left: 200, top: 60, display: 'flex', flexDirection: 'column'}}>
		<Img src={staticFile('late.png')} style={{width: 1000}} />
		<div style={{...line, position: 'static'}}>图片下方</div>
	</div>
);

// Holds its own delayRender for about 1 s, then moves the text past the
// safe area's right edge and releases the handle.
const DelayedMove = () => {
	const [handle] = useState(() => delayRender('fixture: delayed move'));
	const [moved, setMoved] = useState(false);
	useEffect(() => {
		const timer = setTimeout(() => setMoved(true), 1000);
		return () => clearTimeout(timer);
	}, []);
	useEffect(() => {
		if (moved) continueRender(handle);
	}, [moved, handle]);
	return <div style={{...line, left: moved ? 1700 : 200, top: 300}}>延迟移动</div>;
};

// Never releases its own delayRender: the probe gives up, naming it.
const Stuck = () => {
	useState(() => delayRender('fixture: never released'));
	return <div style={{...line, left: 200, top: 300}}>一直等待</div>;
};

// The viewBox halves its user units: 48 user-unit text renders at 24 px.
const SvgFont = () => (
	<svg viewBox="0 0 400 200" width="200" height="100" style={{position: 'absolute', left: 200, top: 300}}>
		<text x="0" y="120" fontSize="48" fontFamily={theme.brand.fontBody}>
			矢量文字
		</text>
	</svg>
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
	camera: Camera,
	'clip-negative': ClipNegative,
	'inline-small': InlineSmall,
	'wrap-start-spaces': WrapStartSpaces,
	'late-image': LateImage,
	'delayed-move': DelayedMove,
	stuck: Stuck,
	'svg-font': SvgFont,
	'text-differs': TextDiffers,
	'custom-overflow': CustomOverflow,
	'late-overflow': LateOverflow,
	'font-absent': FontAbsent,
	throws: Throws,
};
