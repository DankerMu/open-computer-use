// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// What the six scene components of task 4.2 share.
//
// Lines is the one element that marks a text field for the probe: a block
// holding the field's wrapLines lines, each its own block, in a box of
// (maxWidth + HANG) em at a theme size, styled by textStyle. Text a scene
// adds around a field (a bullet, a number badge, a dash) is a Mark beside
// it, never inside, so the marked text equals the field.
//
// Every entrance runs on the theme's smooth spring and Onda's STAGGER and
// lands by ENTRANCE_FRAMES[kind] (15). Beat b starts b × STAGGER frames in,
// and at the latest SHORTEST_RISE frames before the end; its spring is
// stretched to end on the entrance's last frame. From that frame on the
// value is exactly 1, so every later frame is the same picture: nothing
// moves with the narration, and the default single key reveal [0, 15)
// describes the scene.

import React from 'react';
import {Img, spring, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';
import {ENTRANCE_FRAMES} from '../../../bin/scene-timing.mjs';
import {STAGGER} from '../onda/motion.ts';
import {HANG, textStyle, wrapLines} from '../text.ts';
import type {themes} from '../theme.ts';
import {IMAGE_BOX, TEXT_BOX} from './boxes.ts';
import type {Box} from './boxes.ts';

export type Theme = (typeof themes)['light'];
export type Size = 'title' | 'heading' | 'body' | 'caption';
export type Weight = 'regular' | 'bold';
export type Align = 'left' | 'center';
// A scene's props (task 3.8). `timing` is accepted and unused: nothing in a
// scene follows the narration.
export type SceneProps<Content> = {content: Content; image: string | null; timing: unknown; theme: Theme};

const ENTRANCE: Record<string, number> = ENTRANCE_FRAMES;
// The last beat of an entrance still eases in over this many frames.
const SHORTEST_RISE = 6;
const FILL: React.CSSProperties = {position: 'absolute', inset: 0};

// The widest line, in em, of text at `size` in a box `px` wide (3.3's line
// contract: floor(box / font size) − HANG).
export const fit = (px: number, size: Size, theme: Theme) => Math.floor(px / theme.type.sizes[size]) - HANG;

// The region a scene's text keeps: TEXT_BOX, or the left column beside an image.
export const textArea = (image: string | null): Box => (image === null ? TEXT_BOX : IMAGE_BOX.text);

// The width of a scene's text column: `width` when it fits the region.
export const column = (image: string | null, width = Infinity) => Math.min(width, textArea(image).width);

export const Lines = ({
	text,
	maxWidth,
	size,
	weight = 'regular',
	color,
	align = 'left',
	theme,
}: {
	text: string;
	maxWidth: number;
	size: Size;
	weight?: Weight;
	color: string;
	align?: Align;
	theme: Theme;
}) => (
	<div
		data-kit-text={text}
		style={{
			...textStyle(theme),
			width: `${maxWidth + HANG}em`,
			flexShrink: 0,
			fontSize: theme.type.sizes[size],
			fontWeight: theme.type.weights[weight],
			color,
			textAlign: align,
		}}
	>
		{wrapLines(text, maxWidth).map((line, i) => (
			<div key={i}>{line}</div>
		))}
	</div>
);

// Text a scene adds around a field, such as a badge's number or a dash. It is
// probed like any text but carries no field.
export const Mark = ({text, size, weight = 'regular', color, theme}: {text: string; size: Size; weight?: Weight; color: string; theme: Theme}) => (
	<div
		style={{
			...textStyle(theme),
			flexShrink: 0,
			fontSize: theme.type.sizes[size],
			fontWeight: theme.type.weights[weight],
			color,
		}}
	>
		{text}
	</div>
);

// The entrance of beat `beat` of a scene of `kind`: 0 before its start, 1
// from the entrance's last frame on.
export function useEntrance(kind: string, beat: number, theme: Theme): number {
	const frame = useCurrentFrame();
	const {fps} = useVideoConfig();
	const end = ENTRANCE[kind];
	if (!Number.isInteger(end)) throw new Error(`kit scenes: no entrance for kind ${JSON.stringify(kind)}`);
	const start = Math.min(Math.round(beat * STAGGER), end - SHORTEST_RISE);
	if (frame >= end) return 1;
	if (frame <= start) return 0;
	return spring({frame: frame - start, fps, config: theme.motion.springs.smooth, durationInFrames: end - start});
}

// A block that fades in and rises by the theme's third spacing step.
export const Enter = ({
	kind,
	beat,
	theme,
	style,
	children,
}: {
	kind: string;
	beat: number;
	theme: Theme;
	style?: React.CSSProperties;
	children?: React.ReactNode;
}) => {
	const progress = useEntrance(kind, beat, theme);
	return <div style={{...style, opacity: progress, transform: `translateY(${(1 - progress) * theme.spacing[2]}px)`}}>{children}</div>;
};

// An accent shape without text that draws in along one axis from `origin`.
export const Grow = ({
	kind,
	beat,
	theme,
	axis = 'x',
	origin,
	style,
}: {
	kind: string;
	beat: number;
	theme: Theme;
	axis?: 'x' | 'y';
	origin: string;
	style: React.CSSProperties;
}) => {
	const progress = useEntrance(kind, beat, theme);
	const transform = axis === 'x' ? `scaleX(${progress})` : `scaleY(${progress})`;
	return <div style={{...style, flexShrink: 0, opacity: progress, transform, transformOrigin: origin}} />;
};

// A scene's text column: `width` wide, as tall as the text region, its
// children stacked and centred vertically. Without an image the column is
// centred in TEXT_BOX; with one it keeps the left edge of IMAGE_BOX.text.
export const SceneText = ({
	image,
	width,
	align,
	stretch = false,
	gap = 0,
	children,
}: {
	image: string | null;
	width: number;
	align: Align;
	stretch?: boolean;
	gap?: number;
	children?: React.ReactNode;
}) => {
	const area = textArea(image);
	const left = image === null ? area.left + Math.round((area.width - width) / 2) : area.left;
	return (
		<div
			style={{
				position: 'absolute',
				left,
				top: area.top,
				width,
				height: area.height,
				display: 'flex',
				flexDirection: 'column',
				justifyContent: 'center',
				alignItems: stretch ? 'stretch' : align === 'center' ? 'center' : 'flex-start',
				gap,
			}}
		>
			{children}
		</div>
	);
};

// The scene's image, shown whole (`contain`) in IMAGE_BOX.panel on the
// theme's second surface. A missing file fails the render through <Img>.
export const ImagePanel = ({image, theme}: {image: string; theme: Theme}) => (
	<Img
		src={staticFile(`images/${image}`)}
		style={{
			position: 'absolute',
			left: IMAGE_BOX.panel.left,
			top: IMAGE_BOX.panel.top,
			width: IMAGE_BOX.panel.width,
			height: IMAGE_BOX.panel.height,
			objectFit: 'contain',
			backgroundColor: theme.brand.surface2,
			borderRadius: theme.spacing[3],
		}}
	/>
);

// ImagePanel entering on `beat`, or nothing when the scene has no image.
export const Picture = ({kind, beat, image, theme}: {kind: string; beat: number; image: string | null; theme: Theme}) =>
	image === null ? null : (
		<Enter kind={kind} beat={beat} theme={theme} style={FILL}>
			<ImagePanel image={image} theme={theme} />
		</Enter>
	);

// Task 4.3's helpers, appended so that the text above, which batch 4.2's
// approval covers, stays a byte-exact prefix.

// A block that fades in and slides by the theme's second spacing step from
// the side `from`, as Onda's split screen brings its panes in from their
// outer edges.
export const Slide = ({
	kind,
	beat,
	theme,
	from,
	style,
	children,
}: {
	kind: string;
	beat: number;
	theme: Theme;
	from: 'left' | 'right' | 'top' | 'bottom';
	style?: React.CSSProperties;
	children?: React.ReactNode;
}) => {
	const progress = useEntrance(kind, beat, theme);
	const shift = (1 - progress) * theme.spacing[1] * (from === 'left' || from === 'top' ? -1 : 1);
	const transform = from === 'left' || from === 'right' ? `translateX(${shift}px)` : `translateY(${shift}px)`;
	return <div style={{...style, opacity: progress, transform}}>{children}</div>;
};

// A block that fades in while it scales up from half its size about its
// centre, as a marker lands.
export const Pop = ({
	kind,
	beat,
	theme,
	style,
	children,
}: {
	kind: string;
	beat: number;
	theme: Theme;
	style?: React.CSSProperties;
	children?: React.ReactNode;
}) => {
	const progress = useEntrance(kind, beat, theme);
	return <div style={{...style, flexShrink: 0, opacity: progress, transform: `scale(${(1 + progress) / 2})`}}>{children}</div>;
};

// The breadth of an Arrow across its axis: the width of its head.
export const arrowBreadth = (theme: Theme) => theme.spacing[1] * 1.5;

// An arrow without text, pointing right or down: a bar and a head made from
// borders, from plain blocks, so the probe judges it like any box. It fills
// the main axis that `style` gives it (a width or height, or flex in its
// parent) and draws in from its tail.
export const Arrow = ({
	kind,
	beat,
	theme,
	direction,
	color,
	style,
}: {
	kind: string;
	beat: number;
	theme: Theme;
	direction: 'right' | 'down';
	color: string;
	style?: React.CSSProperties;
}) => {
	const progress = useEntrance(kind, beat, theme);
	const {spacing} = theme;
	const bar = spacing[0] / 2;
	const head = {long: spacing[2], half: arrowBreadth(theme) / 2};
	const right = direction === 'right';
	const edge = `${head.half}px solid transparent`;
	const tip = `${head.long}px solid ${color}`;
	return (
		<div
			style={{
				...style,
				display: 'flex',
				flexDirection: right ? 'row' : 'column',
				alignItems: 'center',
				flexShrink: 0,
				[right ? 'height' : 'width']: 2 * head.half,
				opacity: progress,
				transform: right ? `scaleX(${progress})` : `scaleY(${progress})`,
				transformOrigin: right ? 'left' : 'top',
			}}
		>
			<div style={{flex: 1, [right ? 'height' : 'width']: bar, borderRadius: bar / 2, backgroundColor: color}} />
			<div
				style={
					right
						? {width: 0, height: 0, flexShrink: 0, borderTop: edge, borderBottom: edge, borderLeft: tip}
						: {width: 0, height: 0, flexShrink: 0, borderLeft: edge, borderRight: edge, borderTop: tip}
				}
			/>
		</div>
	);
};
