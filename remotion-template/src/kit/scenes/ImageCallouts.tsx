// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
// Source: degueba/onda 3c814051269597b31fee1603bef7ccfb93d091b6 registry/components/ken-burns/KenBurns.tsx MIT
//
// The image with callouts scene (task 4.4): the scene's image in
// IMAGE_BOX.panel with one numbered marker per callout, and a legend in
// IMAGE_BOX.text that repeats each number beside its label. Only the
// choreography comes from the source above, no code: a slow, linear push in
// across the whole scene, never a spring, so the camera does not seem to
// accelerate. The callouts are written for this kit: each marker is pinned
// to its point of the picture and moves with the zoom.
//
// The geometry is callout-zoom.ts's: the picture is drawn in its box R,
// scaled about the focus F from 1 on the first frame to the end scale on the
// last, and clipped by the panel; marker i sits on F + s(f) · (P_i − F),
// outside the scaled and clipped picture, so its number is never scaled or
// masked. The image's natural size decides R, and scene props carry none,
// so the scene measures the image before its first frame, under
// delayRender, and draws nothing that depends on the size until then.
//
// The panel fades in, the markers land one after another and each legend
// row follows its marker, all within the kit's entrance; from then on only
// the panel moves. The legend's numbers and the markers are unmarked text;
// the labels are marked.

import React, {useEffect, useLayoutEffect, useState} from 'react';
import {AbsoluteFill, Img, staticFile, useCurrentFrame, useDelayRender} from 'remotion';
import {IMAGE_BOX} from './boxes.ts';
import {MARKER_RADIUS, calloutZoom, project, scaleAt} from './callout-zoom.ts';
import type {Point, Size} from './callout-zoom.ts';
import {Enter, Lines, Mark, Pop, SceneText, column, fit, useEntrance} from './layout.tsx';
import type {SceneProps, Theme} from './layout.tsx';

const KIND = 'image-callouts';
const PANEL = IMAGE_BOX.panel;
const MARKER = 2 * MARKER_RADIUS;

type Callout = {x: number; y: number; label: string};
type Content = {callouts: Callout[]};
type Timing = {durationInFrames: number};

// Marker i lands half a beat after marker i − 1, its legend row just after.
const beatOf = (i: number) => 1 / 2 + i / 2;

// The natural size of the image at `src`, or null until it has loaded. A
// delayRender handle holds the frame from the first render until the
// measured size has been committed, so no frame shows the scene without it.
function useNaturalSize(src: string): Size | null {
	const {delayRender, continueRender, cancelRender} = useDelayRender();
	const [handle] = useState(() => delayRender(`kit image-callouts: measuring ${src}`));
	const [size, setSize] = useState<Size | null>(null);
	useEffect(() => {
		let live = true;
		const loader = new Image();
		loader.onload = () => {
			if (live) setSize({width: loader.naturalWidth, height: loader.naturalHeight});
		};
		loader.onerror = () => {
			if (live) cancelRender(new Error(`kit image-callouts: cannot load ${src}`));
		};
		loader.src = src;
		return () => {
			live = false;
			loader.onload = null;
			loader.onerror = null;
		};
	}, [src, cancelRender]);
	useLayoutEffect(() => {
		if (size !== null) continueRender(handle);
	}, [size, handle, continueRender]);
	useEffect(() => () => continueRender(handle), [handle, continueRender]);
	return size;
}

// A numbered accent disc of MARKER_RADIUS, centred on `at`.
const Badge = ({n, beat, theme}: {n: number; beat: number; theme: Theme}) => (
	<Pop
		kind={KIND}
		beat={beat}
		theme={theme}
		style={{
			boxSizing: 'border-box',
			width: MARKER,
			height: MARKER,
			borderRadius: MARKER,
			display: 'flex',
			alignItems: 'center',
			justifyContent: 'center',
			backgroundColor: theme.brand.accent,
			border: `${theme.spacing[0] / 2}px solid ${theme.brand.bg}`,
		}}
	>
		<Mark text={String(n)} size="caption" weight="bold" color={theme.brand.bg} theme={theme} />
	</Pop>
);

const Marker = ({n, at, beat, theme}: {n: number; at: Point; beat: number; theme: Theme}) => (
	<div style={{position: 'absolute', left: at.x - MARKER_RADIUS, top: at.y - MARKER_RADIUS, width: MARKER, height: MARKER}}>
		<Badge n={n} beat={beat} theme={theme} />
	</div>
);

// The picture in its box, scaled by `scale` about the focus, clipped by the
// panel, which fades in on the theme's second surface.
const Panel = ({src, zoom, scale, theme}: {src: string; zoom: ReturnType<typeof calloutZoom> | null; scale: number; theme: Theme}) => {
	const shown = useEntrance(KIND, 0, theme);
	return (
		<div
			style={{
				position: 'absolute',
				left: PANEL.left,
				top: PANEL.top,
				width: PANEL.width,
				height: PANEL.height,
				overflow: 'hidden',
				borderRadius: theme.spacing[3],
				backgroundColor: theme.brand.surface2,
				opacity: shown,
			}}
		>
			{zoom === null ? null : (
				<Img
					src={src}
					style={{
						position: 'absolute',
						left: zoom.picture.left - PANEL.left,
						top: zoom.picture.top - PANEL.top,
						width: zoom.picture.width,
						height: zoom.picture.height,
						transform: `scale(${scale})`,
						transformOrigin: `${zoom.focus.x - zoom.picture.left}px ${zoom.focus.y - zoom.picture.top}px`,
					}}
				/>
			)}
		</div>
	);
};

export const ImageCallouts = ({content, image, timing, theme}: SceneProps<Content>) => {
	if (image === null) throw new Error('kit image-callouts: the scene names no image');
	const {brand, spacing} = theme;
	const src = staticFile(`images/${image}`);
	const size = useNaturalSize(src);
	const frame = useCurrentFrame();
	const zoom = size === null ? null : calloutZoom(size, content.callouts);
	const scale = zoom === null ? 1 : scaleAt(zoom.end, frame, (timing as Timing).durationInFrames);
	const width = column(image);
	const gap = spacing[3];
	return (
		<AbsoluteFill>
			<SceneText image={image} width={width} align="left" gap={spacing[4]}>
				{content.callouts.map((callout, i) => (
					<Enter key={i} kind={KIND} beat={beatOf(i) + 1 / 4} theme={theme} style={{display: 'flex', alignItems: 'center', gap}}>
						<Badge n={i + 1} beat={beatOf(i) + 1 / 4} theme={theme} />
						<Lines text={callout.label} maxWidth={fit(width - MARKER - gap, 'body', theme)} size="body" color={brand.text} theme={theme} />
					</Enter>
				))}
			</SceneText>
			<Panel src={src} zoom={zoom} scale={scale} theme={theme} />
			{zoom === null
				? null
				: zoom.points.map((point, i) => <Marker key={i} n={i + 1} at={project(zoom.focus, point, scale)} beat={beatOf(i)} theme={theme} />)}
		</AbsoluteFill>
	);
};
