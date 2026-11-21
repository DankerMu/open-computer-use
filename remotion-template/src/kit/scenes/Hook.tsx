// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
// Source: degueba/onda 3c814051269597b31fee1603bef7ccfb93d091b6 registry/components/title-card/TitleCard.tsx MIT
//
// The hook scene (task 4.2): the explainer's opening title. Only the
// choreography comes from the source above, no code: the title lands first,
// a short accent rule draws in beneath it, and the subtitle follows as a
// calm second beat, all within the kit's entrance. Centred without an
// image; with one, the text keeps the left column beside the panel.

import React from 'react';
import {AbsoluteFill} from 'remotion';
import {Enter, Grow, Lines, Picture, SceneText, column, fit} from './layout.tsx';
import type {SceneProps} from './layout.tsx';

const KIND = 'hook';

type Content = {title: string; subtitle?: string};

export const Hook = ({content, image, theme}: SceneProps<Content>) => {
	const {brand, spacing} = theme;
	const align = image === null ? 'center' : 'left';
	const width = column(image);
	return (
		<AbsoluteFill>
			<SceneText image={image} width={width} align={align} gap={spacing[4]}>
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
					beat={1}
					theme={theme}
					origin={align}
					style={{width: spacing[7], height: spacing[0], borderRadius: spacing[0], backgroundColor: brand.accent}}
				/>
				{content.subtitle === undefined ? null : (
					<Enter kind={KIND} beat={2} theme={theme}>
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
