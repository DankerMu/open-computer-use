// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The `custom` scene: the component src/custom/index.ts re-exports from
// src/custom/<component>.tsx, rendered with the scene's timing, the theme
// and the scene's image (design Decision 8). bin/custom-index.mjs writes
// the index; customProblem tells the renderer when it does not match
// script.json.

import React from 'react';
import Custom, {component} from '../../custom/index.ts';
import type {themes} from '../theme.ts';

type Theme = (typeof themes)['light'];
type Block = {text: string; startFrame: number; endFrame: number};
export type CustomProps = {
	timing: {durationInFrames: number; narrationEndFrame: number; sentences: Array<{blocks: Block[]}>};
	theme: Theme;
	image: string | null;
};

// The generated index exports null when the script has no custom scene.
const Loaded = Custom as unknown as React.ComponentType<CustomProps> | null | undefined;
const loadedName = component as string | null;

export function customProblem(name: string): string | null {
	if (loadedName !== name) {
		const loaded = loadedName === null ? 'no custom scene' : `"${loadedName}"`;
		return `src/custom/index.ts is for ${loaded}, not "${name}"`;
	}
	if (Loaded === null || Loaded === undefined) return `src/custom/${name}.tsx does not default-export a component`;
	return null;
}

export const CustomScene = ({timing, theme, image}: CustomProps) => {
	if (Loaded === null || Loaded === undefined) {
		throw new Error(
			loadedName === null
				? 'kit render: src/custom/index.ts is for no custom scene'
				: `kit render: src/custom/${loadedName}.tsx does not default-export a component`,
		);
	}
	return <Loaded timing={timing} theme={theme} image={image} />;
};
