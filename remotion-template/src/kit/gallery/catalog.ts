// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The gallery's candidate catalog (task 4.1). Tasks 4.2–4.4 add each
// candidate here and its component, under the same id, to components.tsx;
// the gallery renders every sample in both theme candidates. Imports only
// .mjs and .ts files, so Node and the bundle both import it.
//
// Batch 4.2: one candidate per kind, its id the kind and its source the
// scene file. Its minimum length and entrance are read from
// bin/scene-timing.mjs, their one home. Each candidate has three samples:
//   - max: every content field at its LIMITS.content length, in Han text
//     ending in a full-width closing mark, every list at its maximum, over a
//     150-frame timing whose narration (one sentence, blocks [0, 60) and
//     [62, 130)) ends at 130;
//   - image: max with the image sample.png;
//   - min: the required fields only, two Han characters each, every list at
//     its minimum, as long as the minimum, narrated in one block up to
//     minFrames − 20 (the end card's has no narration).

import {ENTRANCE_FRAMES, MIN_SCENE_FRAMES} from '../../../bin/scene-timing.mjs';
import type {Candidate, Timing} from './catalog-rules.ts';

export type {Candidate} from './catalog-rules.ts';
export {validateCatalog} from './catalog-rules.ts';

// gallery/public/images/sample.png, which bin/gallery.mjs serves when the
// project has no public/: a 1280×960 two-colour gradient with a flat
// 192×192 orange patch at its centre, made by the workspace image's ffmpeg
// (6.1.1) from lavfi sources alone:
//   ffmpeg -nostdin -v error \
//     -f lavfi -i "gradients=s=1280x960:c0=0x123B5E:c1=0x3E8E9E:x0=0:y0=0:x1=1280:y1=960:n=2:d=1:r=1" \
//     -f lavfi -i "color=c=0xFF8C1A:s=192x192:d=1:r=1" \
//     -filter_complex "[0][1]overlay=(W-w)/2:(H-h)/2,format=rgb24" -frames:v 1 sample.png
// Its centre differs from every brand slot of both themes; its 4:3 shape
// shows how `contain` letterboxes it in the square panel.
const SAMPLE_IMAGE = 'sample.png';

const MIN_FRAMES: Record<string, number> = MIN_SCENE_FRAMES;
const ENTRANCE: Record<string, number> = ENTRANCE_FRAMES;

const MAX_TIMING: Timing = {
	durationInFrames: 150,
	narrationEndFrame: 130,
	sentences: [
		{
			blocks: [
				{text: '我们先来看一个现象', startFrame: 0, endFrame: 60},
				{text: '再想想它背后的原理', startFrame: 62, endFrame: 130},
			],
		},
	],
};

function minTiming(minFrames: number, narrated: boolean): Timing {
	if (!narrated) return {durationInFrames: minFrames, narrationEndFrame: 0, sentences: []};
	const end = minFrames - 20;
	return {durationInFrames: minFrames, narrationEndFrame: end, sentences: [{blocks: [{text: '简短的一句旁白', startFrame: 0, endFrame: end}]}]};
}

function candidate(kind: string, file: string, max: Record<string, unknown>, min: Record<string, unknown>): Candidate {
	const minFrames = MIN_FRAMES[kind];
	return {
		id: kind,
		kind,
		source: `src/kit/scenes/${file}.tsx`,
		minFrames,
		entranceFrames: ENTRANCE[kind],
		samples: [
			{id: 'max', content: max, image: null, timing: MAX_TIMING},
			{id: 'image', content: max, image: SAMPLE_IMAGE, timing: MAX_TIMING},
			{id: 'min', content: min, image: null, timing: minTiming(minFrames, kind !== 'end-card')},
		],
	};
}

export const candidates: Candidate[] = [
	candidate(
		'hook',
		'Hook',
		{title: '为什么筷子插进水里看起来会折断？', subtitle: '跟着一束光线走进水中看看它在水面发生了什么变化。'},
		{title: '折射'},
	),
	candidate('question', 'Question', {question: '为什么插进水里的筷子看起来像是在水面处被折断了？'}, {question: '为何'}),
	candidate(
		'concept-card',
		'ConceptCard',
		{
			kicker: '光学的核心概念：',
			title: '光线斜射进入另一种介质。',
			points: [
				'光在不同介质中的传播速度不一样，',
				'速度改变让光的传播方向发生偏折，',
				'从空气进入水中时光线会靠近法线，',
				'入射角越大折射后偏折也就越明显。',
			],
			conclusion: '所以水中的筷子看起来像是在水面处被折断。',
		},
		{title: '折射', points: ['偏折']},
	),
	candidate(
		'definition-quote',
		'DefinitionQuote',
		{
			term: '斯涅尔折射定律概述：',
			text: '入射角正弦与折射角正弦之比等于光在两种介质中速度之比且对给定的两种介质恒定不变。',
			source: '荷兰数学家斯涅尔一六二一年提出。',
		},
		{term: '折射', text: '偏折'},
	),
	candidate(
		'summary',
		'Summary',
		{
			points: [
				'光在不同介质中传播速度各不相同。',
				'速度变化使光的传播方向发生偏折。',
				'斜射时入射角越大偏折也就越明显。',
				'生活中的许多光学现象都源自折射。',
			],
		},
		{points: ['折射']},
	),
	candidate(
		'end-card',
		'EndCard',
		{title: '感谢观看我们下期再一起探索光学！', subtitle: '关注我们获取更多有趣的科学知识和精彩的实验视频。'},
		{title: '再见'},
	),
];
