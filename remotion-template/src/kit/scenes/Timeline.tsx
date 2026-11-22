// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
// Source: degueba/onda 3c814051269597b31fee1603bef7ccfb93d091b6 registry/components/timeline/Timeline.tsx MIT
//
// The timeline scene (task 4.3): two to six events in order down an axis,
// each with a marker on the axis, its year in the accent colour, its label
// and an optional detail beneath. Only the choreography comes from the
// source above, no code: the axis draws on from the first event to the
// last, the markers land on it one after another, each event's text fades
// in just after its marker, and the last marker alone is filled, all within
// the kit's entrance. The axis is plain blocks, one piece per row, so the
// probe judges it like any box. Centred in TEXT_BOX without an image, with
// year and label at body size; beside an image, in the left column, every
// line at caption size.

import React from 'react';
import {AbsoluteFill} from 'remotion';
import {Enter, Grow, Lines, Picture, Pop, SceneText, column, fit} from './layout.tsx';
import type {SceneProps, Size, Theme} from './layout.tsx';

const KIND = 'timeline';
// The year and label columns, in em of their size: wide enough that the
// schema's longest year (8) and label (12) each keep one line.
const YEAR = 10;
const LABEL = 14;

type Moment = {year: string; label: string; detail?: string};
type Content = {events: Moment[]};
type Look = {year: Size; label: Size; gap: number};

// Event i's axis piece draws on, then its marker lands, then its text.
const beatOf = (i: number) => (i * 2) / 5;

// One piece of the axis, filling its share of the marker column, or an
// empty share where the axis does not run.
const Piece = ({on, beat, theme}: {on: boolean; beat: number; theme: Theme}) =>
	on ? (
		<Grow
			kind={KIND}
			beat={beat}
			theme={theme}
			axis="y"
			origin="top"
			style={{flex: 1, width: theme.spacing[0] / 2, backgroundColor: theme.brand.borderLit}}
		/>
	) : (
		<div style={{flex: 1}} />
	);

const Row = ({event, i, count, look, theme}: {event: Moment; i: number; count: number; look: Look; theme: Theme}) => {
	const {brand, spacing} = theme;
	const {sizes} = theme.type;
	const marker = spacing[2];
	const ring = spacing[0] / 2;
	const gap = spacing[2];
	const last = i === count - 1;
	const year = YEAR * sizes[look.year];
	const label = LABEL * sizes[look.label];
	const markerColumn: React.CSSProperties = {width: marker, flexShrink: 0, display: 'flex', flexDirection: 'column', alignItems: 'center'};
	return (
		<div style={{display: 'flex', flexDirection: 'column'}}>
			<div style={{display: 'flex', alignItems: 'stretch', gap}}>
				<div style={markerColumn}>
					<Piece on={i > 0} beat={beatOf(i)} theme={theme} />
					<Pop
						kind={KIND}
						beat={beatOf(i) + 1 / 5}
						theme={theme}
						style={{
							boxSizing: 'border-box',
							width: marker,
							height: marker,
							borderRadius: marker,
							border: `${ring}px solid ${brand.accent}`,
							backgroundColor: last ? brand.accent : brand.bg,
						}}
					/>
					<Piece on={!last} beat={beatOf(i) + 1 / 5} theme={theme} />
				</div>
				<Enter kind={KIND} beat={beatOf(i) + 2 / 5} theme={theme} style={{display: 'flex', alignItems: 'center', gap}}>
					<Lines text={event.year} maxWidth={fit(year, look.year, theme)} size={look.year} weight="bold" color={brand.accent} theme={theme} />
					<Lines text={event.label} maxWidth={fit(label, look.label, theme)} size={look.label} weight="bold" color={brand.text} theme={theme} />
				</Enter>
			</div>
			{last && event.detail === undefined ? null : (
				<div style={{display: 'flex', alignItems: 'stretch', gap}}>
					<div style={markerColumn}>
						<Piece on={!last} beat={beatOf(i) + 1 / 5} theme={theme} />
					</div>
					<div style={{paddingBottom: last ? 0 : look.gap, minHeight: last ? 0 : spacing[4]}}>
						{event.detail === undefined ? null : (
							<Enter kind={KIND} beat={beatOf(i) + 2 / 5} theme={theme}>
								<Lines
									text={event.detail}
									maxWidth={fit(year + gap + label, 'caption', theme)}
									size="caption"
									color={brand.dim}
									theme={theme}
								/>
							</Enter>
						)}
					</div>
				</div>
			)}
		</div>
	);
};

export const Timeline = ({content, image, theme}: SceneProps<Content>) => {
	const {spacing} = theme;
	const {sizes} = theme.type;
	const look: Look = image === null ? {year: 'body', label: 'body', gap: spacing[0]} : {year: 'caption', label: 'caption', gap: spacing[3]};
	const width = column(image, spacing[2] * 2 + YEAR * sizes[look.year] + spacing[2] + LABEL * sizes[look.label]);
	return (
		<AbsoluteFill>
			<SceneText image={image} width={width} align="left">
				<div style={{display: 'flex', flexDirection: 'column'}}>
					{content.events.map((event, i) => (
						<Row key={i} event={event} i={i} count={content.events.length} look={look} theme={theme} />
					))}
				</div>
			</SceneText>
			<Picture kind={KIND} beat={1} image={image} theme={theme} />
		</AbsoluteFill>
	);
};
