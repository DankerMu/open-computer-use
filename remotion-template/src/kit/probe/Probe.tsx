// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The kit probe composition. Each target's scene plays in its own Sequence,
// as the renderer places scenes, so the scene sees its own frame and length.
// At every rendered frame, once the scene root covers the canvas and the
// scene's own delayRender handles have released, the active scene's root is
// measured and logged as one `kit-probe <JSON>` console line, which
// bin/probe.mjs collects.

import React, {useLayoutEffect, useMemo, useRef} from 'react';
import {
	AbsoluteFill,
	Composition,
	Sequence,
	cancelRender,
	continueRender,
	delayRender,
	useCurrentFrame,
	useVideoConfig,
} from 'remotion';
import {measureFrame} from './measure';

type ProbeTarget = {scene: string; props: Record<string, unknown>; durationInFrames: number};
type ProbeProps = {targets: ProbeTarget[]};
type Scenes = Record<string, React.ComponentType<Record<string, unknown>>>;

const WIDTH = 1920;
const HEIGHT = 1080;
const FPS = 30;
// Remotion mounts a page's first frame in an off-screen portal and moves it
// onto the canvas a moment later. Measured after one animation frame, text
// in the image's shell lay at y = -999899 in a 96 px wide layout. So the
// measurement waits until the scene root covers the canvas, for at most
// this many animation frames.
const ATTACH_FRAMES = 120;
// A scene's own delayRender handles (an <Img> loading, data being fetched)
// hold its final layout; measuring earlier records the frame before it. The
// wait stays below the 28 s Remotion gives the probe's own handle (30 s
// default minus 2 s), so this error, naming the handles, comes first.
const PENDING_WAIT_MS = 20000;

// Remotion 4.0.537 keeps the pending delayRender handles in
// window.remotion_delayRenderHandles and their labels in
// window.remotion_delayRenderTimeouts (delay-render.js).
type DelayRenderWindow = {
	remotion_delayRenderHandles?: unknown;
	remotion_delayRenderTimeouts?: Record<string, {label: string | null} | undefined>;
};

function pendingOthers(own: number): string[] {
	const scope = window as unknown as DelayRenderWindow;
	const handles = scope.remotion_delayRenderHandles;
	if (!Array.isArray(handles)) throw new Error('kit probe: cannot read the pending delayRender handles');
	return handles
		.filter((handle) => handle !== own)
		.map((handle) => scope.remotion_delayRenderTimeouts?.[String(handle)]?.label ?? 'unlabelled');
}

function sceneFor(scenes: Scenes, key: string) {
	if (!Object.prototype.hasOwnProperty.call(scenes, key)) {
		throw new Error(`kit probe: unknown scene ${JSON.stringify(key)}`);
	}
	return scenes[key];
}

function startsOf(targets: ProbeTarget[]): number[] {
	let offset = 0;
	return targets.map((target) => {
		const start = offset;
		offset += target.durationInFrames;
		return start;
	});
}

const ProbeTargets = ({targets, scenes}: ProbeProps & {scenes: Scenes}) => {
	const frame = useCurrentFrame();
	const {width, height} = useVideoConfig();
	const roots = useRef<Array<HTMLDivElement | null>>([]);
	const starts = startsOf(targets);
	const active = starts.findIndex((start, t) => frame >= start && frame < start + targets[t].durationInFrames);

	useLayoutEffect(() => {
		const handle = delayRender(`kit probe: measuring frame ${frame}`);
		let settled = false;
		let waited = 0;
		let quiet = false;
		const started = performance.now();
		const settle = () => {
			if (settled) return;
			settled = true;
			continueRender(handle);
		};
		const fail = (error: unknown) => {
			if (settled) return;
			settled = true;
			cancelRender(error);
		};
		const attempt = () => {
			if (settled) return;
			try {
				const root = roots.current[active];
				if (root === null || root === undefined) throw new Error(`kit probe: no scene root at frame ${frame}`);
				const rect = root.getBoundingClientRect();
				if (rect.left !== 0 || rect.top !== 0 || rect.width !== width || rect.height !== height) {
					waited += 1;
					if (waited > ATTACH_FRAMES) {
						throw new Error(`kit probe: the scene root at frame ${frame} never covered the ${width}x${height} canvas`);
					}
					quiet = false;
					requestAnimationFrame(attempt);
					return;
				}
				const pending = pendingOthers(handle);
				if (pending.length > 0) {
					if (performance.now() - started > PENDING_WAIT_MS) {
						const names = pending.map((label) => JSON.stringify(label)).join(', ');
						throw new Error(`kit probe: frame ${frame} still waits on delayRender ${names} after ${PENDING_WAIT_MS} ms`);
					}
					quiet = false;
					requestAnimationFrame(attempt);
					return;
				}
				// One animation frame with nothing pending, so the layout the
				// released handles allowed is the one measured.
				if (!quiet) {
					quiet = true;
					requestAnimationFrame(attempt);
					return;
				}
				console.log(`kit-probe ${JSON.stringify({index: frame, ...measureFrame(root)})}`);
				settle();
			} catch (error) {
				fail(error);
			}
		};
		document.fonts.ready.then(() => requestAnimationFrame(attempt), fail);
		return settle;
	}, [frame, active, width, height]);

	return (
		<>
			{targets.map((target, t) => {
				const Scene = sceneFor(scenes, target.scene);
				return (
					<Sequence key={t} from={starts[t]} durationInFrames={target.durationInFrames}>
						<AbsoluteFill
							ref={(el) => {
								roots.current[t] = el;
							}}
						>
							<Scene {...target.props} />
						</AbsoluteFill>
					</Sequence>
				);
			})}
		</>
	);
};

export const ProbeComposition = ({scenes}: {scenes: Scenes}) => {
	const component = useMemo(() => (props: ProbeProps) => <ProbeTargets targets={props.targets} scenes={scenes} />, [scenes]);
	const calculateMetadata = useMemo(
		() =>
			({props}: {props: ProbeProps}) => {
				for (const target of props.targets) sceneFor(scenes, target.scene);
				return {durationInFrames: props.targets.reduce((sum, target) => sum + target.durationInFrames, 0)};
			},
		[scenes],
	);
	return (
		<Composition
			id="KitProbe"
			component={component}
			width={WIDTH}
			height={HEIGHT}
			fps={FPS}
			defaultProps={{targets: []}}
			calculateMetadata={calculateMetadata}
		/>
	);
};
