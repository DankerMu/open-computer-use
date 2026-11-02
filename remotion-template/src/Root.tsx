// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors

import {AbsoluteFill, Composition, interpolate, useCurrentFrame} from 'remotion';

const Main = () => {
	const frame = useCurrentFrame();
	const opacity = interpolate(frame, [0, 30], [0, 1], {
		extrapolateRight: 'clamp',
	});

	return (
		<AbsoluteFill
			style={{
				backgroundColor: '#0b1020',
				justifyContent: 'center',
				alignItems: 'center',
			}}
		>
			<h1 style={{color: 'white', fontFamily: 'sans-serif', fontSize: 120, opacity}}>
				Remotion
			</h1>
		</AbsoluteFill>
	);
};

export const RemotionRoot = () => {
	return (
		<Composition
			id="Main"
			component={Main}
			durationInFrames={150}
			fps={30}
			width={1920}
			height={1080}
		/>
	);
};
