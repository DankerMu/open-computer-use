// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The summary scene (task 4.2): one to four points, each on its own surface
// row after a numbered accent badge. The rows cascade in three quarters of a
// stagger apart and all settle within the kit's entrance. The rows fill a
// 1200 px column centred in TEXT_BOX, or the left column beside an image.

import React from 'react';
import {AbsoluteFill} from 'remotion';
import {Enter, Lines, Mark, Picture, SceneText, column, fit} from './layout.tsx';
import type {SceneProps} from './layout.tsx';

const KIND = 'summary';
const WIDTH = 1200;

type Content = {points: string[]};

export const Summary = ({content, image, theme}: SceneProps<Content>) => {
	const {brand, spacing} = theme;
	const width = column(image, WIDTH);
	const badge = spacing[5];
	const pad = {x: spacing[3], y: spacing[2]};
	const border = spacing[0] / 4;
	const text = width - 2 * (pad.x + border) - badge - spacing[2];
	return (
		<AbsoluteFill>
			<SceneText image={image} width={width} align="left" stretch gap={spacing[2]}>
				{content.points.map((point, i) => (
					<Enter
						key={i}
						kind={KIND}
						beat={(i * 3) / 4}
						theme={theme}
						style={{
							display: 'flex',
							alignItems: 'center',
							gap: spacing[2],
							padding: `${pad.y}px ${pad.x}px`,
							backgroundColor: brand.surface,
							border: `${border}px solid ${brand.border}`,
							borderRadius: spacing[2],
						}}
					>
						<div
							style={{
								width: badge,
								height: badge,
								borderRadius: badge,
								flexShrink: 0,
								display: 'flex',
								alignItems: 'center',
								justifyContent: 'center',
								backgroundColor: brand.accent,
							}}
						>
							<Mark text={String(i + 1)} size="caption" weight="bold" color={brand.bg} theme={theme} />
						</div>
						<Lines text={point} maxWidth={fit(text, 'body', theme)} size="body" color={brand.text} theme={theme} />
					</Enter>
				))}
			</SceneText>
			<Picture kind={KIND} beat={1} image={image} theme={theme} />
		</AbsoluteFill>
	);
};
