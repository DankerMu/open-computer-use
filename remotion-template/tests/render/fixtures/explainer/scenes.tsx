// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// Fixture components of the renderer's browser tests (task 3.8), one per
// non-custom kind of the fixture script. Each renders its content text,
// marked with data-kit-text, at body size inside the safe area and above
// the caption band (930–1026). They stand in for the 4.2–4.4 components and
// never reach a project: kit new does not copy tests/.

import React from 'react';
import {HANG, textStyle, wrapLines} from '../../../../src/kit/text.ts';
import type {themes} from '../../../../src/kit/theme.ts';

type Theme = (typeof themes)['light'];
type Content = Record<string, unknown>;
type SceneProps = {content: Content; theme: Theme};

const WIDTH = 24;
const LEFT = 200;
const TOP = 200;
// One body line is 48 px × 1.4 = 67.2 px; this leaves a gap between texts.
const STEP = 96;

// One marked text block: its wrapLines lines, each its own block.
const Text = ({text, top, theme}: {text: string; top: number; theme: Theme}) => (
	<div
		data-kit-text={text}
		style={{
			...textStyle(theme),
			position: 'absolute',
			left: LEFT,
			top,
			width: `${WIDTH + HANG}em`,
			fontSize: theme.type.sizes.body,
			color: theme.brand.text,
		}}
	>
		{wrapLines(text, WIDTH).map((line) => (
			<div key={line}>{line}</div>
		))}
	</div>
);

// A component rendering the strings `fields` picks from its content.
function textScene(fields: (content: Content) => string[]) {
	return ({content, theme}: SceneProps) => (
		<>
			{fields(content).map((text, i) => (
				<Text key={i} text={text} top={TOP + i * STEP} theme={theme} />
			))}
		</>
	);
}

const strings = (value: unknown): string[] => (Array.isArray(value) ? value.map(String) : []);

export const components = {
	hook: textScene((c) => [String(c.title)]),
	question: textScene((c) => [String(c.question)]),
	'concept-card': textScene((c) => [String(c.title), ...strings(c.points)]),
	summary: textScene((c) => strings(c.points)),
	'end-card': textScene((c) => [String(c.title)]),
};
