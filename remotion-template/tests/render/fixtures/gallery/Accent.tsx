// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The kit-root brand fixture's custom scene (task 4.1). The gallery browser
// test copies it into a scratch project's src/custom/, where its import
// resolves, and generates the custom index for it, as 3.8's test does with
// Orbit.tsx. It draws the placeholder's 200×100 box at (96, 54) in
// THEME.accent, which reads the brand's CSS variable, so its pixels show
// whether the explainer's and the probe's KitFrame applied the theme.

import React from 'react';
import {THEME} from '../kit/onda/tokens.ts';

export default function Accent() {
	return <div style={{position: 'absolute', left: 96, top: 54, width: 200, height: 100, background: THEME.accent}} />;
}
