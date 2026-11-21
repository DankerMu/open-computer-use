// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
// Source: degueba/onda 3c814051269597b31fee1603bef7ccfb93d091b6 registry/components/end-card/EndCard.tsx MIT
//
// The end card (task 4.2): the closing title and an optional subtitle.
// Only the choreography comes from the source above, no code: the title
// lands first, an accent underline draws in as it settles, and the
// subtitle fades in last, all within the kit's entrance. Without an image
// the text is centred on a soft accent disc; with one, it keeps the left
// column beside the panel.

import React from 'react';
import {AbsoluteFill} from 'remotion';
import {TEXT_BOX} from './boxes.ts';
import {Enter, Grow, Lines, Picture, SceneText, column, fit} from './layout.tsx';
import type {SceneProps} from './layout.tsx';

const KIND = 'end-card';
// The disc behind a centred end card: a circle inside TEXT_BOX, centred in it.
const DISC = 720;

type Content = {title: string; subtitle?: string};

export const EndCard = ({content, image, theme}: SceneProps<Content>) => {
	const {brand, spacing} = theme;
	const align = image === null ? 'center' : 'left';
	const width = column(image);
	return (
		<AbsoluteFill>
			{image === null ? (
				<Enter
					kind={KIND}
					beat={0}
					theme={theme}
					style={{
						position: 'absolute',
						left: TEXT_BOX.left + (TEXT_BOX.width - DISC) / 2,
						top: TEXT_BOX.top + (TEXT_BOX.height - DISC) / 2,
						width: DISC,
						height: DISC,
						borderRadius: DISC,
						backgroundColor: brand.accentSoft,
					}}
				/>
			) : null}
			<SceneText image={image} width={width} align={align} gap={spacing[4]}>
				<div style={{display: 'flex', flexDirection: 'column', alignItems: align === 'center' ? 'center' : 'flex-start', gap: spacing[2]}}>
					<Enter kind={KIND} beat={0} theme={theme}>
						<Lines
							text={content.title}
							maxWidth={fit(width, 'title', theme)}
							size="title"
							weight="bold"
							color={brand.text}
							align={align}
							theme={theme}
						/>
					</Enter>
					<Grow
						kind={KIND}
						beat={3 / 2}
						theme={theme}
						origin={align}
						style={{width: 2 * spacing[7], height: spacing[0] / 2 + spacing[0] / 4, borderRadius: spacing[0], backgroundColor: brand.accent}}
					/>
				</div>
				{content.subtitle === undefined ? null : (
					<Enter kind={KIND} beat={5 / 2} theme={theme}>
						<Lines
							text={content.subtitle}
							maxWidth={fit(width, 'heading', theme)}
							size="heading"
							color={brand.dim}
							align={align}
							theme={theme}
						/>
					</Enter>
				)}
			</SceneText>
			<Picture kind={KIND} beat={1} image={image} theme={theme} />
		</AbsoluteFill>
	);
};
