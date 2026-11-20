// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The theme transition (task 4.5): the outgoing scene fades linearly to the
// theme's background over its exit window, the last frames of its
// post-narration gap. Opacity is 1 before the window, 14/15 on its first
// frame, 1/15 on its second-to-last and 0 on its last; the incoming scene
// starts on the next frame with its own entrance and is never drawn early.
// The curve is linear so that the window's edge frames are measurable.
// Render it inside the scene's Sequence, around the scene alone: the frame
// is the scene's own, and exit comes from explainerScenes, which reads it
// from timeline.json. Without an exit the scene renders unwrapped.

import React from 'react';
import {AbsoluteFill, interpolate, useCurrentFrame} from 'remotion';

export type Exit = {startFrame: number; frames: number} | null;

export const SceneExit = ({exit, children}: {exit: Exit; children?: React.ReactNode}) => {
	const frame = useCurrentFrame();
	if (exit === null) return <>{children}</>;
	const opacity = interpolate(frame, [exit.startFrame - 1, exit.startFrame + exit.frames - 1], [1, 0], {
		extrapolateLeft: 'clamp',
		extrapolateRight: 'clamp',
	});
	return <AbsoluteFill style={{opacity}}>{children}</AbsoluteFill>;
};
