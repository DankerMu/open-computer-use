// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The fixture custom scene (task 3.8). The tests copy it into a scratch
// project's src/custom/, so it has no relative import: the timing and the
// theme arrive as props. It renders one marked text built from them,
// `Orbit <durationInFrames> <blocks in sentence 1> <block 2's startFrame>
// <accent>`, which shows the scene received its own relative timing and the
// kit theme.

import React from 'react';

type Block = {text: string; startFrame: number; endFrame: number};
type OrbitProps = {
	timing: {durationInFrames: number; sentences: Array<{blocks: Block[]}>};
	theme: {brand: {accent: string; fontBody: string}; type: {lineHeight: number; sizes: {body: number}}};
};

export default function Orbit({timing, theme}: OrbitProps) {
	const [sentence] = timing.sentences;
	const text = `Orbit ${timing.durationInFrames} ${sentence.blocks.length} ${sentence.blocks[1].startFrame} ${theme.brand.accent}`;
	return (
		<div
			data-kit-text={text}
			style={{
				position: 'absolute',
				left: 200,
				top: 300,
				fontFamily: theme.brand.fontBody,
				fontSize: theme.type.sizes.body,
				lineHeight: theme.type.lineHeight,
				whiteSpace: 'nowrap',
				color: theme.brand.accent,
			}}
		>
			{text}
		</div>
	);
}
