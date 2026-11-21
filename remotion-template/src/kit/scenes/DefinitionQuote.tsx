// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
// Source: degueba/onda 3c814051269597b31fee1603bef7ccfb93d091b6 registry/components/quote-card/QuoteCard.tsx MIT
//
// The definition or quote scene (task 4.2): a term in the accent colour,
// its definition or quote, and an optional source after a dash, on one
// surface card. Only the choreography comes from the source above, no code:
// the words read first, a short accent rule draws in after them, and the
// attribution fades in last, each beat after the one before, all within
// the kit's entrance. The card fills a 1280 px column centred in TEXT_BOX,
// or the left column beside an image.

import React from 'react';
import {AbsoluteFill} from 'remotion';
import {Enter, Grow, Lines, Mark, Picture, SceneText, column, fit} from './layout.tsx';
import type {SceneProps} from './layout.tsx';

const KIND = 'definition-quote';
const WIDTH = 1280;
const DASH = '—';

type Content = {term: string; text: string; source?: string};

export const DefinitionQuote = ({content, image, theme}: SceneProps<Content>) => {
	const {brand, spacing} = theme;
	const width = column(image, WIDTH);
	const pad = image === null ? spacing[5] : spacing[4];
	const border = spacing[0] / 4;
	const inner = width - 2 * (pad + border);
	const dash = theme.type.sizes.caption;
	return (
		<AbsoluteFill>
			<SceneText image={image} width={width} align="left">
				<Enter
					kind={KIND}
					beat={0}
					theme={theme}
					style={{
						boxSizing: 'border-box',
						width,
						padding: pad,
						display: 'flex',
						flexDirection: 'column',
						gap: spacing[2],
						backgroundColor: brand.surface,
						border: `${border}px solid ${brand.border}`,
						borderRadius: spacing[3],
					}}
				>
					<Lines
						text={content.term}
						maxWidth={fit(inner, 'heading', theme)}
						size="heading"
						weight="bold"
						color={brand.accent}
						theme={theme}
					/>
					<Enter kind={KIND} beat={1} theme={theme}>
						<Lines text={content.text} maxWidth={fit(inner, 'body', theme)} size="body" color={brand.text} theme={theme} />
					</Enter>
					<Grow
						kind={KIND}
						beat={2}
						theme={theme}
						origin="left"
						style={{width: spacing[4], height: spacing[0] / 2, borderRadius: spacing[0] / 2, backgroundColor: brand.accent}}
					/>
					{content.source === undefined ? null : (
						<Enter kind={KIND} beat={3} theme={theme} style={{display: 'flex', alignItems: 'flex-start', gap: spacing[1]}}>
							<Mark text={DASH} size="caption" color={brand.faint} theme={theme} />
							<Lines
								text={content.source}
								maxWidth={fit(inner - dash - spacing[1], 'caption', theme)}
								size="caption"
								color={brand.faint}
								theme={theme}
							/>
						</Enter>
					)}
				</Enter>
			</SceneText>
			<Picture kind={KIND} beat={1} image={image} theme={theme} />
		</AbsoluteFill>
	);
};
