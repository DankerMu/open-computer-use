// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The `throws` gallery fixture (task 4.1): with the `ok` catalog, the
// placeholder renders its question and throws at frame 30, so the first
// placeholder clip, placeholder--max--light, fails partway.

import React from 'react';
import {useCurrentFrame} from 'remotion';

const Placeholder = ({content}: {content: {question: string}}) => {
	if (useCurrentFrame() === 30) throw new Error('placeholder throws at frame 30');
	return <div style={{position: 'absolute', left: 96, top: 400, fontSize: 48}}>{content.question}</div>;
};

export const components = {placeholder: Placeholder};
