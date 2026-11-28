// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// Theme tokens of the kit: one theme, `light`, the explainer's pick
// recorded in gallery/approvals.json (task 4.6). The kit's colours live only
// here, in Onda's 12 `Brand` slots, so `brandToCssVars` re-skins the Onda
// primitives from them. Fonts name only the family the workspace image
// installs. Springs are Onda's own objects.

import {SPRING_SMOOTH, SPRING_SNAPPY} from './onda/motion.ts';

const FONT = '"Noto Sans CJK SC"';

const canvas = {width: 1920, height: 1080, fps: 30};

// 5% title-safe margins on each side leave a 90% by 90% inner box.
const safeArea = {top: 54, right: 96, bottom: 54, left: 96};

const type = {
	weights: {regular: 400, bold: 700},
	sizes: {title: 96, heading: 64, body: 48, caption: 36, minimum: 32},
	lineHeight: 1.4,
	letterSpacing: 0,
};

const spacing = [8, 16, 24, 32, 48, 64, 96, 128];

const motion = {
	springs: {smooth: SPRING_SMOOTH, snappy: SPRING_SNAPPY},
};

export const themes = {
	light: {
		canvas,
		safeArea,
		brand: {
			bg: '#F7F8FA',
			surface: '#FFFFFF',
			surface2: '#EBEEF3',
			border: '#D5DAE3',
			borderLit: '#AEB6C4',
			text: '#141821',
			dim: '#4A5365',
			faint: '#6E7789',
			accent: '#1F6FEB',
			accentSoft: 'rgba(31, 111, 235, 0.12)',
			fontDisplay: FONT,
			fontBody: FONT,
		},
		type,
		spacing,
		motion,
	},
};

// The one theme of the explainer, the user's pick recorded in
// gallery/approvals.json: kit check judges with the theme Explainer renders
// with.
export const KIT_THEME = themes.light;

type Theme = (typeof themes)[keyof typeof themes];

// Each preset's pick, as gallery/approvals.json records it (the gate tests
// bind the two). The report has none until task 4.2.
const PICKS: Record<string, Theme> = {explainer: KIT_THEME};

// The theme a preset renders and is checked with.
export function themeFor(preset: string): Theme {
	if (!Object.prototype.hasOwnProperty.call(PICKS, preset)) throw new Error(`no approved theme for preset ${preset}`);
	return PICKS[preset];
}
