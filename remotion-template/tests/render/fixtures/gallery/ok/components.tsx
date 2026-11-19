// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The `ok` gallery fixture's components (task 4.1), copied over
// src/kit/gallery/components.tsx by the browser tests. The placeholder
// renders a 200×100 box at (96, 54) in THEME.accent, which reads the brand's
// CSS variable, so its pixels show whether KitFrame applied the theme; and
// the question through wrapLines at body size, marked, with its top-left
// corner at (96, 400), or at (1800, 400) for the overflow sample.

import React from 'react';
import {THEME} from '../onda/tokens.ts';
import {HANG, textStyle, wrapLines} from '../text.ts';
import type {themes} from '../theme.ts';
import {OVERFLOW_QUESTION} from './catalog.ts';

type Theme = (typeof themes)['light'];

const WIDTH = 24;

const Placeholder = ({content, theme}: {content: {question: string}; theme: Theme}) => (
	<>
		<div style={{position: 'absolute', left: 96, top: 54, width: 200, height: 100, background: THEME.accent}} />
		<div
			data-kit-text={content.question}
			style={{
				...textStyle(theme),
				position: 'absolute',
				left: content.question === OVERFLOW_QUESTION ? 1800 : 96,
				top: 400,
				width: `${WIDTH + HANG}em`,
				fontSize: theme.type.sizes.body,
			}}
		>
			{wrapLines(content.question, WIDTH).map((line, i) => (
				<div key={i}>{line}</div>
			))}
		</div>
	</>
);

export const components = {placeholder: Placeholder};
