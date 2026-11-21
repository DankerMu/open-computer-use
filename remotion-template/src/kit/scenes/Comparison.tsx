// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
// Source: degueba/onda 3c814051269597b31fee1603bef7ccfb93d091b6 registry/components/split-screen/SplitScreen.tsx MIT
//
// The comparison scene (task 4.3): two sides on one surface panel, split by
// a thin divider, each with its title beside an accent rule and one to four
// points after accent bullets. Only the choreography comes from the source
// above, no code: the two sides slide in from their outer edges, the second
// half a beat after the first, while the divider draws in between them;
// each side's points follow its title, all within the kit's entrance.
// Without an image the sides stand left and right across TEXT_BOX; beside
// an image they stack in the left column, one above the other, at smaller
// sizes.

import React from 'react';
import {AbsoluteFill} from 'remotion';
import {Enter, Grow, Lines, Picture, SceneText, Slide, column, fit} from './layout.tsx';
import type {SceneProps, Size, Theme} from './layout.tsx';

const KIND = 'comparison';

type Side = {title: string; points: string[]};
type Content = {left: Side; right: Side};
type Look = {title: Size; point: Size; pad: {x: number; y: number}; gap: number};

const Half = ({
	side,
	beat,
	from,
	width,
	look,
	theme,
}: {
	side: Side;
	beat: number;
	from: 'left' | 'right' | 'top' | 'bottom';
	width: number;
	look: Look;
	theme: Theme;
}) => {
	const {brand, spacing} = theme;
	const rule = spacing[0];
	const dot = spacing[1];
	const inner = width - 2 * look.pad.x;
	return (
		<Slide
			kind={KIND}
			beat={beat}
			theme={theme}
			from={from}
			style={{
				boxSizing: 'border-box',
				width,
				flexShrink: 0,
				padding: `${look.pad.y}px ${look.pad.x}px`,
				display: 'flex',
				flexDirection: 'column',
				gap: look.gap,
			}}
		>
			<div style={{display: 'flex', alignItems: 'stretch', gap: spacing[2]}}>
				<Grow
					kind={KIND}
					beat={beat}
					theme={theme}
					axis="y"
					origin="top"
					style={{width: rule, borderRadius: rule, backgroundColor: brand.accent}}
				/>
				<Lines
					text={side.title}
					maxWidth={fit(inner - rule - spacing[2], look.title, theme)}
					size={look.title}
					weight="bold"
					color={brand.text}
					theme={theme}
				/>
			</div>
			<div style={{display: 'flex', flexDirection: 'column', gap: spacing[0]}}>
				{side.points.map((point, i) => (
					<Enter
						key={i}
						kind={KIND}
						beat={beat + 3 / 4 + i / 2}
						theme={theme}
						style={{display: 'flex', alignItems: 'center', gap: spacing[2]}}
					>
						<div style={{width: dot, height: dot, borderRadius: dot, flexShrink: 0, backgroundColor: brand.accent}} />
						<Lines
							text={point}
							maxWidth={fit(inner - dot - spacing[2], look.point, theme)}
							size={look.point}
							color={brand.text}
							theme={theme}
						/>
					</Enter>
				))}
			</div>
		</Slide>
	);
};

export const Comparison = ({content, image, theme}: SceneProps<Content>) => {
	const {brand, spacing} = theme;
	const width = column(image);
	const across = image === null;
	const border = spacing[0] / 4;
	const divider = spacing[0] / 4;
	const look: Look = across
		? {title: 'heading', point: 'body', pad: {x: spacing[4], y: spacing[4]}, gap: spacing[3]}
		: {title: 'body', point: 'caption', pad: {x: spacing[4], y: spacing[2]}, gap: spacing[1]};
	const half = across ? (width - 2 * border - divider) / 2 : width - 2 * border;
	return (
		<AbsoluteFill>
			<SceneText image={image} width={width} align="left">
				<div
					style={{
						boxSizing: 'border-box',
						width,
						display: 'flex',
						flexDirection: across ? 'row' : 'column',
						alignItems: 'stretch',
						backgroundColor: brand.surface,
						border: `${border}px solid ${brand.border}`,
						borderRadius: spacing[3],
					}}
				>
					<Half side={content.left} beat={0} from={across ? 'left' : 'top'} width={half} look={look} theme={theme} />
					<Grow
						kind={KIND}
						beat={1 / 2}
						theme={theme}
						axis={across ? 'y' : 'x'}
						origin="center"
						style={{
							[across ? 'width' : 'height']: divider,
							margin: across ? `${spacing[4]}px 0` : `0 ${spacing[4]}px`,
							backgroundColor: brand.borderLit,
						}}
					/>
					<Half side={content.right} beat={1 / 2} from={across ? 'right' : 'bottom'} width={half} look={look} theme={theme} />
				</div>
			</SceneText>
			<Picture kind={KIND} beat={1} image={image} theme={theme} />
		</AbsoluteFill>
	);
};
