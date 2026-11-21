// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The question scene (task 4.2): one question at title size, set off by an
// accent bar that draws down beside it before the question rises in.
// Without an image the question takes a 1536 px column, 13 em at title size
// beside the bar, so its 24 characters break into two even lines; with one,
// it keeps the left column beside the panel.

import React from 'react';
import {AbsoluteFill} from 'remotion';
import {Enter, Grow, Lines, Picture, SceneText, column, fit} from './layout.tsx';
import type {SceneProps} from './layout.tsx';

const KIND = 'question';
const WIDTH = 1536;

type Content = {question: string};

export const Question = ({content, image, theme}: SceneProps<Content>) => {
	const {brand, spacing} = theme;
	const width = column(image, WIDTH);
	const bar = spacing[1];
	const gap = spacing[4];
	return (
		<AbsoluteFill>
			<SceneText image={image} width={width} align={image === null ? 'center' : 'left'}>
				<div style={{display: 'flex', alignItems: 'stretch', gap}}>
					<Grow
						kind={KIND}
						beat={0}
						theme={theme}
						axis="y"
						origin="top"
						style={{width: bar, borderRadius: bar, backgroundColor: brand.accent}}
					/>
					<Enter kind={KIND} beat={1} theme={theme}>
						<Lines
							text={content.question}
							maxWidth={fit(width - bar - gap, 'title', theme)}
							size="title"
							weight="bold"
							color={brand.text}
							theme={theme}
						/>
					</Enter>
				</div>
			</SceneText>
			<Picture kind={KIND} beat={1} image={image} theme={theme} />
		</AbsoluteFill>
	);
};
