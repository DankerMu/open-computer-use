// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The concept card (task 4.2): an accent kicker, the concept's title, one
// to four points behind accent bullets, and an optional conclusion on a
// soft accent band. The header lands first, the points follow half a
// stagger apart, and the conclusion settles last, all within the kit's
// entrance. Left-aligned in a 1280 px column centred in TEXT_BOX, or in the
// left column beside an image.

import React from 'react';
import {AbsoluteFill} from 'remotion';
import {Enter, Grow, Lines, Picture, SceneText, column, fit} from './layout.tsx';
import type {SceneProps} from './layout.tsx';

const KIND = 'concept-card';
const WIDTH = 1280;

type Content = {kicker?: string; title: string; points: string[]; conclusion?: string};

export const ConceptCard = ({content, image, theme}: SceneProps<Content>) => {
	const {brand, spacing} = theme;
	const width = column(image, WIDTH);
	const rule = spacing[3];
	const dot = spacing[1];
	const band = {x: spacing[3], y: spacing[2]};
	return (
		<AbsoluteFill>
			<SceneText image={image} width={width} align="left" gap={spacing[4]}>
				<div style={{display: 'flex', flexDirection: 'column', gap: spacing[1]}}>
					{content.kicker === undefined ? null : (
						<Enter kind={KIND} beat={0} theme={theme} style={{display: 'flex', alignItems: 'center', gap: spacing[1]}}>
							<Grow
								kind={KIND}
								beat={0}
								theme={theme}
								origin="left"
								style={{width: rule, height: spacing[0], borderRadius: spacing[0], backgroundColor: brand.accent}}
							/>
							<Lines
								text={content.kicker}
								maxWidth={fit(width - rule - spacing[1], 'caption', theme)}
								size="caption"
								weight="bold"
								color={brand.accent}
								theme={theme}
							/>
						</Enter>
					)}
					<Enter kind={KIND} beat={0} theme={theme}>
						<Lines
							text={content.title}
							maxWidth={fit(width, 'heading', theme)}
							size="heading"
							weight="bold"
							color={brand.text}
							theme={theme}
						/>
					</Enter>
				</div>
				<div style={{display: 'flex', flexDirection: 'column', gap: spacing[1]}}>
					{content.points.map((point, i) => (
						<Enter
							key={i}
							kind={KIND}
							beat={1 + i / 2}
							theme={theme}
							style={{display: 'flex', alignItems: 'center', gap: spacing[2]}}
						>
							<div style={{width: dot, height: dot, borderRadius: dot, flexShrink: 0, backgroundColor: brand.accent}} />
							<Lines
								text={point}
								maxWidth={fit(width - dot - spacing[2], 'body', theme)}
								size="body"
								color={brand.text}
								theme={theme}
							/>
						</Enter>
					))}
				</div>
				{content.conclusion === undefined ? null : (
					<Enter
						kind={KIND}
						beat={3}
						theme={theme}
						style={{padding: `${band.y}px ${band.x}px`, borderRadius: spacing[1], backgroundColor: brand.accentSoft}}
					>
						<Lines
							text={content.conclusion}
							maxWidth={fit(width - 2 * band.x, 'body', theme)}
							size="body"
							color={brand.text}
							theme={theme}
						/>
					</Enter>
				)}
			</SceneText>
			<Picture kind={KIND} beat={1} image={image} theme={theme} />
		</AbsoluteFill>
	);
};
