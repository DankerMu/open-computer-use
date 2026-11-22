// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
// Source: degueba/onda 3c814051269597b31fee1603bef7ccfb93d091b6 registry/components/bar-chart/BarChart.tsx MIT
//
// The chart scene (task 4.4): a title with its optional unit on one row,
// then two to eight points drawn as bars or as a line. Only the
// choreography comes from the source above, no code, and for the line from
// Onda's registry/components/line-chart/LineChart.tsx, under the same
// notice: bars grow from the zero line one after another, the largest in the
// accent colour and the rest muted; a line draws on from left to right, its
// dots landing as it reaches them, over a soft fill. Everything settles
// within the kit's entrance.
//
// Every number is drawn as formatCount gives it, and every text, values and
// labels included, is a Lines block outside the plot. The line and its dots
// alone are svg geometry (a path and circles), the one construct a sloped
// line needs; bars, tracks, the zero line and the baseline are plain blocks.
//   - Bars run across, one row per point: the label, the bar's track and the
//     value. Bars start at a zero line that sits neg / (pos + neg) of the
//     track from its start, where pos is the largest positive value (or 0)
//     and neg the largest magnitude of a negative one (or 0); each bar is
//     |v| / (pos + neg) of the track long, positive bars after the zero line
//     and negative ones before it. When every value is 0 there are no bars
//     and the zero line sits at the start.
//   - The line runs over one column per point, its vertical scale from the
//     smallest value to the largest (mid-height when all are equal). Without
//     an image each column carries its value in a row above the plot, with a
//     faint guide dropping to its dot, and its label in a row under it;
//     beside an image the columns are too narrow for text, so the plot
//     carries none and a table under it lists each point's label and value
//     in order.
// Without an image the chart fills TEXT_BOX; beside one it keeps the left
// column at smaller sizes.

import React from 'react';
import {AbsoluteFill} from 'remotion';
import {HANG, formatCount, width as charWidth} from '../text.ts';
import {Enter, Grow, Lines, Picture, SceneText, column, fit, useEntrance} from './layout.tsx';
import type {SceneProps, Size, Theme} from './layout.tsx';

const KIND = 'chart';
// The schema's longest title, unit and label (LIMITS.content.chart), and the
// longest formatted number every layout keeps on one line.
const TITLE = 12;
const UNIT = 4;
const LABEL = 6;
const VALUE = 8;

type Point = {label: string; value: number};
type Content = {type: 'bar' | 'line'; title: string; unit?: string; points: Point[]};
// Sizes of one layout: the title row's, and the points' text.
type Look = {title: Size; text: Size};

// The width of `text` in em, as text.ts measures it.
const span = (text: string) => Array.from(text).reduce((sum, ch) => sum + charWidth(ch), 0);
// A line width just wide enough for `text`, up to `most`: the title and unit
// sit side by side without the slack of their longest case.
const snug = (text: string, most: number) => Math.min(most, Math.max(2, Math.ceil(span(text))));
// The width of a Lines box `maxWidth` wide at `size`.
const boxWidth = (maxWidth: number, size: Size, theme: Theme) => (maxWidth + HANG) * theme.type.sizes[size];

const TitleRow = ({content, look, theme}: {content: Content; look: Look; theme: Theme}) => {
	const {brand, spacing} = theme;
	return (
		<Enter kind={KIND} beat={0} theme={theme} style={{display: 'flex', alignItems: 'flex-start', gap: spacing[1]}}>
			<Lines text={content.title} maxWidth={snug(content.title, TITLE)} size={look.title} weight="bold" color={brand.text} theme={theme} />
			{content.unit === undefined ? null : (
				<Lines text={content.unit} maxWidth={snug(content.unit, UNIT)} size={look.title} color={brand.dim} theme={theme} />
			)}
		</Enter>
	);
};

// Point i's beat: the points land one after another after the title.
const beatOf = (i: number) => 1 / 2 + i / 2;

// --- bars ------------------------------------------------------------------------

const Bar = ({value, i, zero, scale, largest, thickness, theme}: {
	value: number;
	i: number;
	zero: number;
	scale: number;
	largest: boolean;
	thickness: number;
	theme: Theme;
}) => {
	const progress = useEntrance(KIND, beatOf(i), theme);
	if (value === 0 || scale === 0) return null;
	const length = Math.abs(value) * scale;
	const after = value > 0;
	return (
		<div
			style={{
				position: 'absolute',
				top: 0,
				left: after ? zero : zero - length,
				width: length,
				height: thickness,
				borderRadius: thickness / 2,
				backgroundColor: largest ? theme.brand.accent : theme.brand.dim,
				transform: `scaleX(${progress})`,
				transformOrigin: after ? 'left' : 'right',
			}}
		/>
	);
};

const Bars = ({points, width, look, theme}: {points: Point[]; width: number; look: Look; theme: Theme}) => {
	const {brand, spacing} = theme;
	const gap = spacing[2];
	const label = boxWidth(LABEL, look.text, theme);
	const shown = boxWidth(VALUE, look.text, theme);
	const track = width - label - shown - 2 * gap;
	const thickness = look.text === 'body' ? spacing[3] : spacing[2];
	const values = points.map((p) => p.value);
	const pos = Math.max(0, ...values);
	const neg = Math.max(0, ...values.map((v) => -v));
	const total = pos + neg;
	const scale = total === 0 ? 0 : track / total;
	const zero = neg * scale;
	const top = Math.max(...values);
	const first = values.indexOf(top);
	return (
		<div style={{position: 'relative', display: 'flex', flexDirection: 'column', gap: spacing[1]}}>
			{points.map((point, i) => (
				<Enter key={i} kind={KIND} beat={beatOf(i)} theme={theme} style={{display: 'flex', alignItems: 'center', gap}}>
					<Lines text={point.label} maxWidth={LABEL} size={look.text} color={brand.text} theme={theme} />
					<div style={{position: 'relative', width: track, height: thickness, flexShrink: 0, borderRadius: thickness / 2, backgroundColor: brand.surface2}}>
						<Bar value={point.value} i={i} zero={zero} scale={scale} largest={i === first && top > 0} thickness={thickness} theme={theme} />
					</div>
					<Lines
						text={formatCount(point.value)}
						maxWidth={VALUE}
						size={look.text}
						weight="bold"
						color={i === first && top > 0 ? brand.accent : brand.text}
						theme={theme}
					/>
				</Enter>
			))}
			<Grow
				kind={KIND}
				beat={1 / 2}
				theme={theme}
				axis="y"
				origin="top"
				style={{
					position: 'absolute',
					top: -spacing[0],
					bottom: -spacing[0],
					left: label + gap + zero - spacing[0] / 8,
					width: spacing[0] / 4,
					backgroundColor: brand.borderLit,
				}}
			/>
		</div>
	);
};

// --- the line --------------------------------------------------------------------

// Point i of n at the centre of its column, at its value's height in a plot
// `width` × `height`.
function plotPoints(points: Point[], width: number, height: number) {
	const values = points.map((p) => p.value);
	const low = Math.min(...values);
	const high = Math.max(...values);
	const columnWidth = width / points.length;
	return values.map((v, i) => ({x: columnWidth * (i + 1 / 2), y: high === low ? height / 2 : ((high - v) / (high - low)) * height}));
}

// A dot's radius, its ring included.
const dotRadius = (theme: Theme) => theme.spacing[1] * 0.75;

// The line, its fill and its dots, drawn on from left to right: svg geometry
// only, at `top` in its parent, with no text in it.
const Plot = ({points, width, height, top, theme}: {points: Point[]; width: number; height: number; top: number; theme: Theme}) => {
	const {brand, spacing} = theme;
	const progress = useEntrance(KIND, 1 / 2, theme);
	const at = plotPoints(points, width, height);
	const line = at.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x} ${p.y}`).join(' ');
	const fill = `${line} L${at[at.length - 1].x} ${height} L${at[0].x} ${height} Z`;
	const dot = dotRadius(theme);
	const stroke = spacing[0] / 2;
	// Dot i lands as the line reaches it.
	const landed = (i: number) => Math.min(1, Math.max(0, (progress * (at.length - 1) - i) * 2 + 1));
	return (
		<>
			<div style={{position: 'absolute', left: 0, top: top + height, width, height: stroke / 2, backgroundColor: brand.borderLit}} />
			<svg width={width} height={height} style={{position: 'absolute', left: 0, top, overflow: 'visible'}}>
				<path d={fill} fill={brand.accentSoft} stroke="none" opacity={progress} />
				<path
					d={line}
					fill="none"
					stroke={brand.accent}
					strokeWidth={stroke}
					strokeLinecap="round"
					strokeLinejoin="round"
					pathLength={1}
					strokeDasharray={progress >= 1 ? undefined : 1}
					strokeDashoffset={progress >= 1 ? undefined : 1 - progress}
				/>
				{at.map((p, i) => (
					<circle key={i} cx={p.x} cy={p.y} r={dot - stroke / 2} fill={brand.accent} stroke={brand.bg} strokeWidth={stroke} opacity={landed(i)} />
				))}
			</svg>
		</>
	);
};

// The plot's height: three of the theme's widest steps without an image,
// its two widest beside one, so the rows above and below keep their room.
const plotHeight = (beside: boolean, theme: Theme) => (beside ? theme.spacing[7] + theme.spacing[6] : 3 * theme.spacing[7]);

// Without an image: one column per point, its value in a row above the plot,
// a faint guide dropping from that row to its dot, and its label in a row
// under the plot in two lines of caption at most. A value's line holds the
// 8 code points of any formatted number up to -9999.9万; its box may reach
// past its column, centred on it, since a formatted number's glyphs stay
// within the column (digits are narrower than the em text.ts counts them as).
const Across = ({points, width, theme}: {points: Point[]; width: number; theme: Theme}) => {
	const {brand, spacing} = theme;
	const columnWidth = width / points.length;
	const plot = plotHeight(false, theme);
	const dot = dotRadius(theme);
	const guide = spacing[0] / 4;
	const at = plotPoints(points, width, plot);
	const labelWidth = Math.min(LABEL, fit(columnWidth, 'caption', theme));
	const cell: React.CSSProperties = {width: columnWidth, flexShrink: 0, display: 'flex', justifyContent: 'center'};
	return (
		<div style={{display: 'flex', flexDirection: 'column', gap: spacing[1]}}>
			<div style={{display: 'flex', alignItems: 'flex-end'}}>
				{points.map((point, i) => (
					<Enter key={i} kind={KIND} beat={beatOf(i)} theme={theme} style={cell}>
						<Lines text={formatCount(point.value)} maxWidth={VALUE} size="caption" weight="bold" color={brand.text} align="center" theme={theme} />
					</Enter>
				))}
			</div>
			<div style={{position: 'relative', width, height: plot + 2 * dot, flexShrink: 0}}>
				{at.map((p, i) => (
					<Grow
						key={i}
						kind={KIND}
						beat={beatOf(i)}
						theme={theme}
						axis="y"
						origin="top"
						style={{position: 'absolute', left: p.x - guide / 2, top: 0, width: guide, height: dot + p.y, backgroundColor: brand.border}}
					/>
				))}
				<Plot points={points} width={width} height={plot} top={dot} theme={theme} />
			</div>
			<div style={{display: 'flex', alignItems: 'flex-start', marginTop: spacing[1]}}>
				{points.map((point, i) => (
					<Enter key={i} kind={KIND} beat={beatOf(i)} theme={theme} style={cell}>
						<Lines text={point.label} maxWidth={labelWidth} size="caption" color={brand.dim} align="center" theme={theme} />
					</Enter>
				))}
			</div>
		</div>
	);
};

// Beside an image: the plot without text, then a table of each point's
// label and value, in order.
const Stacked = ({points, width, theme}: {points: Point[]; width: number; theme: Theme}) => {
	const {brand, spacing} = theme;
	const plot = plotHeight(true, theme);
	const dot = dotRadius(theme);
	return (
		<div style={{display: 'flex', flexDirection: 'column', gap: spacing[2]}}>
			<div style={{position: 'relative', width, height: plot + 2 * dot, flexShrink: 0}}>
				<Plot points={points} width={width} height={plot} top={dot} theme={theme} />
			</div>
			<div style={{display: 'flex', flexDirection: 'column'}}>
				{points.map((point, i) => (
					<Enter key={i} kind={KIND} beat={beatOf(i)} theme={theme} style={{display: 'flex', alignItems: 'center', gap: spacing[2]}}>
						<Lines text={point.label} maxWidth={LABEL} size="caption" color={brand.dim} theme={theme} />
						<Lines text={formatCount(point.value)} maxWidth={VALUE} size="caption" weight="bold" color={brand.text} theme={theme} />
					</Enter>
				))}
			</div>
		</div>
	);
};

export const Chart = ({content, image, theme}: SceneProps<Content>) => {
	const {spacing} = theme;
	const beside = image !== null;
	const look: Look = beside ? {title: 'body', text: 'caption'} : {title: 'heading', text: 'body'};
	const width = column(image);
	const bars = content.type === 'bar';
	return (
		<AbsoluteFill>
			<SceneText image={image} width={width} align="left" gap={spacing[3]}>
				<TitleRow content={content} look={look} theme={theme} />
				{bars ? (
					<Bars points={content.points} width={width} look={look} theme={theme} />
				) : beside ? (
					<Stacked points={content.points} width={width} theme={theme} />
				) : (
					<Across points={content.points} width={width} theme={theme} />
				)}
			</SceneText>
			<Picture kind={KIND} beat={1} image={image} theme={theme} />
		</AbsoluteFill>
	);
};
