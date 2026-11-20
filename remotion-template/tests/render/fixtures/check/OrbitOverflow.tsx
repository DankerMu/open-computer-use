// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The overflowing variant of 3.8's Orbit custom scene (task 5.1, the spec
// scenario "Overflow fails the check"). The test copies it over a scratch
// project's src/custom/Orbit.tsx, the one change from the passing project.
// Its marked text starts at x = 60, inside the canvas but across the safe
// area's left edge at 96.

import React from 'react';

type OrbitProps = {
	theme: {brand: {accent: string; fontBody: string}; type: {lineHeight: number; sizes: {body: number}}};
};

export default function Orbit({theme}: OrbitProps) {
	const text = 'Orbit overflow';
	return (
		<div
			data-kit-text={text}
			style={{
				position: 'absolute',
				left: 60,
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
