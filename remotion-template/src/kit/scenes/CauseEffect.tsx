// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The cause and effect scene (task 4.3): the cause on a surface card, an
// accent arrow pointing to the effect on a soft accent card, and the
// optional link in the accent colour on the arrow's side. The cause lands
// first, the arrow draws on toward the effect with the link, and the effect
// settles last, all within the kit's entrance. Without a link the arrow
// stands alone. Without an image the cards stand left and right of the
// arrow across TEXT_BOX, the link above the arrow; beside an image they
// stack in the left column, the arrow pointing down with the link beside it.

import React from 'react';
import {AbsoluteFill} from 'remotion';
import {Arrow, Enter, Lines, Picture, SceneText, arrowBreadth, column, fit} from './layout.tsx';
import type {SceneProps, Theme} from './layout.tsx';

const KIND = 'cause-effect';
// The width of each card without an image; the arrow takes the third
// between them.
const CARD = 576;

type Content = {cause: string; effect: string; link?: string};

const Card = ({text, beat, width, effect, theme}: {text: string; beat: number; width: number; effect: boolean; theme: Theme}) => {
	const {brand, spacing} = theme;
	const pad = spacing[3];
	const border = spacing[0] / 4;
	return (
		<Enter
			kind={KIND}
			beat={beat}
			theme={theme}
			style={{
				boxSizing: 'border-box',
				width,
				flexShrink: 0,
				padding: pad,
				display: 'flex',
				alignItems: 'center',
				backgroundColor: effect ? brand.accentSoft : brand.surface,
				border: `${border}px solid ${effect ? brand.accent : brand.border}`,
				borderRadius: spacing[3],
			}}
		>
			<Lines text={text} maxWidth={fit(width - 2 * (pad + border), 'body', theme)} size="body" weight="bold" color={brand.text} theme={theme} />
		</Enter>
	);
};

const Link = ({text, width, align, theme}: {text: string; width: number; align: 'left' | 'center'; theme: Theme}) => (
	<Enter kind={KIND} beat={3 / 2} theme={theme}>
		<Lines text={text} maxWidth={fit(width, 'body', theme)} size="body" color={theme.brand.accent} align={align} theme={theme} />
	</Enter>
);

// Side by side: cause, then the arrow at the cards' vertical centre with the
// link just above it, then effect.
const Across = ({content, width, theme}: {content: Content; width: number; theme: Theme}) => {
	const {brand, spacing} = theme;
	const gap = spacing[2];
	const span = width - 2 * CARD - 2 * gap;
	return (
		<div style={{display: 'flex', alignItems: 'stretch'}}>
			<Card text={content.cause} beat={0} width={CARD} effect={false} theme={theme} />
			<div style={{width: width - 2 * CARD, display: 'flex', alignItems: 'center', justifyContent: 'center'}}>
				<div style={{position: 'relative', width: span, display: 'flex'}}>
					{content.link === undefined ? null : (
						<div style={{position: 'absolute', left: 0, right: 0, bottom: '100%', paddingBottom: spacing[1], display: 'flex', justifyContent: 'center'}}>
							<Link text={content.link} width={span} align="center" theme={theme} />
						</div>
					)}
					<Arrow kind={KIND} beat={1} theme={theme} direction="right" color={brand.accent} style={{flex: 1}} />
				</div>
			</div>
			<Card text={content.effect} beat={2} width={CARD} effect theme={theme} />
		</div>
	);
};

// Stacked: cause, then the arrow pointing down with the link beside it,
// then effect, left-aligned in the column.
const Down = ({content, width, theme}: {content: Content; width: number; theme: Theme}) => {
	const {brand, spacing} = theme;
	const inset = spacing[4];
	const arrow = arrowBreadth(theme);
	return (
		<div style={{display: 'flex', flexDirection: 'column'}}>
			<Card text={content.cause} beat={0} width={width} effect={false} theme={theme} />
			<div style={{display: 'flex', alignItems: 'center', gap: spacing[3], height: spacing[7], padding: `${spacing[1]}px ${inset}px`}}>
				<Arrow kind={KIND} beat={1} theme={theme} direction="down" color={brand.accent} style={{alignSelf: 'stretch'}} />
				{content.link === undefined ? null : <Link text={content.link} width={width - 2 * inset - arrow - spacing[3]} align="left" theme={theme} />}
			</div>
			<Card text={content.effect} beat={2} width={width} effect theme={theme} />
		</div>
	);
};

export const CauseEffect = ({content, image, theme}: SceneProps<Content>) => {
	const width = column(image);
	return (
		<AbsoluteFill>
			<SceneText image={image} width={width} align={image === null ? 'center' : 'left'}>
				{image === null ? <Across content={content} width={width} theme={theme} /> : <Down content={content} width={width} theme={theme} />}
			</SceneText>
			<Picture kind={KIND} beat={1} image={image} theme={theme} />
		</AbsoluteFill>
	);
};
