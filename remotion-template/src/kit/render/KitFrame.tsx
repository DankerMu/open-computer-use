// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The kit's one theme root. The explainer, every probe scene and every
// gallery sample render inside KitFrame, so the frames the user approves in
// a theme are the frames kit check probes and kit render renders. It
// carries the brand's CSS variables, which re-skin the Onda primitives
// (THEME.* reads them), and the brand's background, text colour and body
// font.

import React from 'react';
import {AbsoluteFill} from 'remotion';
import {brandToCssVars} from '../onda/theme.tsx';
import type {themes} from '../theme.ts';
import type {Registry} from './registry.ts';

type Theme = (typeof themes)['light'];

export const KitFrame = ({theme, children}: {theme: Theme; children?: React.ReactNode}) => (
	<AbsoluteFill
		style={{
			...brandToCssVars(theme.brand),
			backgroundColor: theme.brand.bg,
			color: theme.brand.text,
			fontFamily: theme.brand.fontBody,
		}}
	>
		{children}
	</AbsoluteFill>
);

// Each component of `registry` inside a KitFrame of its own `theme` prop.
// Call it once, at module scope: each call makes new component identities.
export function framed(registry: Registry): Registry {
	return Object.fromEntries(
		Object.entries(registry).map(([kind, Component]) => {
			const Framed = (props: {theme: Theme}) => (
				<KitFrame theme={props.theme}>
					<Component {...props} />
				</KitFrame>
			);
			Framed.displayName = `Framed(${kind})`;
			return [kind, Framed];
		}),
	);
}
