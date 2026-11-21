// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The gallery's candidate catalog (task 4.1). Tasks 4.2–4.4 add each
// candidate here and its component, under the same id, to components.tsx;
// the gallery renders every sample in both theme candidates. Imports only
// .mjs and .ts files, so Node and the bundle both import it.
//
// Batches 4.2 and 4.3 (process steps, cause and effect, comparison and
// timeline, appended after 4.2's six): one candidate per kind, its id the
// kind and its source the scene file. Its minimum length and entrance are read from
// bin/scene-timing.mjs, their one home. Each candidate has three samples:
//   - max: every content field at its LIMITS.content length, in Han text
//     ending in a full-width closing mark, every list at its maximum (the
//     fields of the objects in batch 4.3's lists and sides included), over a
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
	candidate(
		'process-steps',
		'ProcessSteps',
		{
			steps: [
				{label: '光线照射到水面，', detail: '光从空气中斜着射向平静的水面上，'},
				{label: '一部分光被反射，', detail: '按照反射定律这部分光返回到空气，'},
				{label: '其余的光进入水，', detail: '另一部分光穿过界面进入到水中去，'},
				{label: '光速变慢方向偏，', detail: '水中光速较慢使传播方向靠近法线，'},
				{label: '人眼看到了虚像。', detail: '人眼沿折射光的反向延长线看到像。'},
			],
		},
		{steps: [{label: '入射'}, {label: '折射'}]},
	),
	candidate(
		'cause-effect',
		'CauseEffect',
		{cause: '光从空气斜射进入水中时速度变慢，', effect: '光的传播方向偏折使筷子看似折断。', link: '于是改变了方向，'},
		{cause: '减速', effect: '偏折'},
	),
	candidate(
		'comparison',
		'Comparison',
		{
			left: {
				title: '光在空气中传播，',
				points: ['光速接近每秒三十万千米，', '介质稀疏对光的阻碍较小，', '光线沿着直线前进不偏折，', '折射率约等于一作为参照，'],
			},
			right: {
				title: '光在水中传播时，',
				points: ['光速约为空气的四分之三，', '介质致密对光的阻碍较大，', '斜射时光线向法线方向偏，', '折射率约为一点三三左右，'],
			},
		},
		{left: {title: '空气', points: ['较快']}, right: {title: '水中', points: ['较慢']}},
	),
	candidate(
		'timeline',
		'Timeline',
		{
			events: [
				{year: '公元前三世纪时，', label: '欧几里得研究光沿直线走，', detail: '他在光学一书中提出了光线的概念，'},
				{year: '公元二世纪前后，', label: '托勒密测量入射与折射角，', detail: '他列表记录了光从空气射入水的角，'},
				{year: '公元九八四年时，', label: '伊本萨赫勒写出折射规律，', detail: '他的论文用几何方法描述透镜聚光，'},
				{year: '公元一六二一年，', label: '斯涅尔总结出了折射定律，', detail: '他发现入射角与折射角的正弦成比，'},
				{year: '公元一六三七年，', label: '笛卡尔发表折射正弦定律，', detail: '他在屈光学中独立发表了这一定律，'},
				{year: '公元一六六二年，', label: '费马提出了最短时间原理。', detail: '他用最短时间原理推导出折射定律。'},
			],
		},
		{events: [{year: '古代', label: '观察'}, {year: '近代', label: '定律'}]},
	),
];
