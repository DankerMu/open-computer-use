// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
// Source: degueba/onda 3c814051269597b31fee1603bef7ccfb93d091b6 registry/components/count-up/CountUp.tsx MIT
//
// The key number scene (task 4.4): one number in the accent colour, its
// optional unit beneath it, and its label. Only the choreography comes from
// the source above, no code: the number fades in while it counts up from 0
// on the same spring, so the count and the fade land together; the unit
// follows, an accent rule draws in and the label settles last, all within
// the kit's entrance. The number is always drawn through formatCount, the
// kit's one home of 万 and 亿: while it counts, the value reached so far, cut
// to as many decimals as the value has; from the entrance's end, exactly
// formatCount(value). Its line keeps a fixed width, so the count never
// reflows the scene. All three sit on one card, centred in TEXT_BOX
// without an image; beside one, in the left column at a smaller size.

import React from 'react';
import {AbsoluteFill} from 'remotion';
import {formatCount} from '../text.ts';
import {Enter, Grow, Lines, Picture, SceneText, column, fit, useEntrance} from './layout.tsx';
import type {SceneProps, Size} from './layout.tsx';

const KIND = 'key-number';
// The card's width without an image: the number's line at title size holds
// the 8 code points every formatted value up to -9999.9万 needs.
const CARD = 1152;
// Enough decimals for any finite number to read back as itself.
const MOST_DECIMALS = 20;

type Content = {value: number; unit?: string; label: string};

// The decimals `value` has: the fewest that read back as the same number.
function decimalsOf(value: number): number {
	for (let d = 0; d < MOST_DECIMALS; d++) if (Number(value.toFixed(d)) === value) return d;
	return MOST_DECIMALS;
}

// The number drawn at entrance `progress`: the value reached so far, cut
// toward zero to the value's own decimals, then formatted.
function counted(value: number, progress: number): string {
	if (progress >= 1) return formatCount(value);
	const step = 10 ** decimalsOf(value);
	return formatCount(Math.trunc(value * progress * step) / step);
}

export const KeyNumber = ({content, image, theme}: SceneProps<Content>) => {
	const {brand, spacing} = theme;
	const beside = image !== null;
	const align = beside ? 'left' : 'center';
	const pad = beside ? {x: spacing[4], y: spacing[4]} : {x: spacing[6], y: spacing[5]};
	const card = column(image, CARD);
	const inner = card - 2 * pad.x;
	const size: Size = beside ? 'heading' : 'title';
	const count = useEntrance(KIND, 0, theme);
	return (
		<AbsoluteFill>
			<SceneText image={image} width={card} align={align}>
				<Enter
					kind={KIND}
					beat={0}
					theme={theme}
					style={{
						boxSizing: 'border-box',
						width: card,
						padding: `${pad.y}px ${pad.x}px`,
						display: 'flex',
						flexDirection: 'column',
						alignItems: beside ? 'flex-start' : 'center',
						gap: spacing[2],
						backgroundColor: brand.surface,
						border: `${spacing[0] / 4}px solid ${brand.border}`,
						borderRadius: spacing[4],
					}}
				>
					<div style={{display: 'flex', flexDirection: 'column', alignItems: beside ? 'flex-start' : 'center'}}>
						<Lines
							text={counted(content.value, count)}
							maxWidth={fit(inner, size, theme)}
							size={size}
							weight="bold"
							color={brand.accent}
							align={align}
							theme={theme}
						/>
						{content.unit === undefined ? null : (
							<Enter kind={KIND} beat={1 / 2} theme={theme}>
								<Lines text={content.unit} maxWidth={fit(inner, 'heading', theme)} size="heading" color={brand.dim} align={align} theme={theme} />
							</Enter>
						)}
					</div>
					<Grow
						kind={KIND}
						beat={1}
						theme={theme}
						origin={align}
						style={{width: spacing[7], height: spacing[0], borderRadius: spacing[0], backgroundColor: brand.accent}}
					/>
					<Enter kind={KIND} beat={3 / 2} theme={theme}>
						<Lines text={content.label} maxWidth={fit(inner, 'body', theme)} size="body" color={brand.text} align={align} theme={theme} />
					</Enter>
				</Enter>
			</SceneText>
			<Picture kind={KIND} beat={1} image={image} theme={theme} />
		</AbsoluteFill>
	);
};
