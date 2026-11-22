// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// Where the image-callouts scene (task 4.4) draws its picture and its
// markers while the picture zooms. Pure and plain TypeScript, so Node, the
// scene and its tests share the one geometry. In canvas pixels:
//   - the picture box R is the contain fit of the image's natural size into
//     IMAGE_BOX.panel inset by INSET, centred;
//   - callout i sits at P_i = (R.left + x_i · R.width, R.top + y_i · R.height);
//   - the focus F is the centre of the bounding box of every P_i;
//   - the end scale S is the largest s ≤ S_MAX, and at least 1, that keeps
//     every F + s · (P_i − F) inside the inset panel. It depends on the
//     callouts alone, so the scene's last frame is its widest at any length;
//   - at frame f of a scene of D frames the scale is 1 + (S − 1) · f / (D − 1).
// The picture is drawn in R scaled by s(f) about F, clipped by the panel; the
// marker of callout i is centred on F + s(f) · (P_i − F), outside the scaled
// and clipped element, so its number is never scaled or masked.

import {IMAGE_BOX} from './boxes.ts';
import type {Box} from './boxes.ts';

export type Point = {x: number; y: number};
export type Size = {width: number; height: number};
export type Zoom = {picture: Box; focus: Point; points: Point[]; end: number};

// The end scale's ceiling: a slow, steady push in, as a documentary's Ken
// Burns move.
export const S_MAX = 1.5;
// A marker's radius. The inset keeps every marker centre at least this far
// inside the panel's edge, so a whole marker stays on the panel.
export const MARKER_RADIUS = 32;
export const INSET = 40;

const PANEL = IMAGE_BOX.panel;
const INNER = {left: PANEL.left + INSET, top: PANEL.top + INSET, right: PANEL.left + PANEL.width - INSET, bottom: PANEL.top + PANEL.height - INSET};

// R: the image of natural size `width` × `height`, shown whole and centred in
// the inset panel.
export function pictureBox({width, height}: Size): Box {
	if (!(width > 0 && height > 0)) throw new RangeError(`kit image-callouts: the image has no size (${width}×${height})`);
	const room = {width: INNER.right - INNER.left, height: INNER.bottom - INNER.top};
	const scale = Math.min(room.width / width, room.height / height);
	const fitted = {width: width * scale, height: height * scale};
	return {
		left: INNER.left + (room.width - fitted.width) / 2,
		top: INNER.top + (room.height - fitted.height) / 2,
		width: fitted.width,
		height: fitted.height,
	};
}

// F + s · (P − F).
export const project = (focus: Point, point: Point, scale: number): Point => ({
	x: focus.x + scale * (point.x - focus.x),
	y: focus.y + scale * (point.y - focus.y),
});

// The largest scale about `focus` at which `point` stays within [low, high]
// on one axis; unbounded when the point sits on the focus.
function reach(focus: number, point: number, low: number, high: number): number {
	const offset = point - focus;
	if (offset > 0) return (high - focus) / offset;
	if (offset < 0) return (low - focus) / offset;
	return Infinity;
}

export function calloutZoom(size: Size, callouts: ReadonlyArray<{x: number; y: number}>): Zoom {
	if (callouts.length === 0) throw new RangeError('kit image-callouts: no callout');
	const picture = pictureBox(size);
	const points = callouts.map(({x, y}) => ({x: picture.left + x * picture.width, y: picture.top + y * picture.height}));
	const xs = points.map((p) => p.x);
	const ys = points.map((p) => p.y);
	const focus = {x: (Math.min(...xs) + Math.max(...xs)) / 2, y: (Math.min(...ys) + Math.max(...ys)) / 2};
	const limit = Math.min(
		S_MAX,
		...points.flatMap((p) => [reach(focus.x, p.x, INNER.left, INNER.right), reach(focus.y, p.y, INNER.top, INNER.bottom)]),
	);
	return {picture, focus, points, end: Math.max(1, limit)};
}

// s(f): 1 on the scene's first frame, `end` on its last, linear between.
export function scaleAt(end: number, frame: number, durationInFrames: number): number {
	const last = durationInFrames - 1;
	if (last <= 0) return end;
	const f = Math.min(Math.max(frame, 0), last);
	return 1 + ((end - 1) * f) / last;
}
