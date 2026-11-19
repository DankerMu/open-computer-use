// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The kit's own root: the Explainer composition over the registered scene
// components and the probe over the same components. Kit commands bundle
// src/kit/index.ts; Studio and `npx remotion render src/index.ts Main`
// never load it. Task 4.6 replaces themes.light with the picked theme.

import React from 'react';
import {ProbeComposition} from './probe/Probe.tsx';
import {ExplainerComposition} from './render/Explainer.tsx';
import {registry} from './render/registry.ts';
import {themes} from './theme.ts';

export const KitRoot = () => (
	<>
		<ExplainerComposition registry={registry} theme={themes.light} />
		<ProbeComposition scenes={registry} />
	</>
);
