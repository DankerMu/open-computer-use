// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The kit's own root: the Explainer composition over the registered scene
// components and the probe over the same components, each in KitFrame. Kit
// commands bundle src/kit/index.ts; Studio and `npx remotion render
// src/index.ts Main` never load it. The theme is KIT_THEME, the one kit
// check judges with; task 4.6 changes it to the picked theme.

import React from 'react';
import {ProbeComposition} from './probe/Probe.tsx';
import {ExplainerComposition} from './render/Explainer.tsx';
import {framed} from './render/KitFrame.tsx';
import {registry} from './render/registry.ts';
import {KIT_THEME} from './theme.ts';

// Probe scenes render in the KitFrame of their target's theme, as the
// explainer's scenes do.
const probeScenes = framed(registry);

export const KitRoot = () => (
	<>
		<ExplainerComposition registry={registry} theme={KIT_THEME} />
		<ProbeComposition scenes={probeScenes} />
	</>
);
