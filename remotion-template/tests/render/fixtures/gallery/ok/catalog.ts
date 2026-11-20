// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The `ok` gallery fixture's catalog (task 4.1). The browser tests copy it
// over src/kit/gallery/catalog.ts, so it imports the rules from beside that
// file. One placeholder question with two 60-frame samples: `max`, a
// 24-character question (the schema's maximum), and `overflow`, another
// 24-character question that the fixture component places at x = 1800. The
// component tells the two apart by OVERFLOW_QUESTION, because a sample's
// props are only {content, image, timing, theme}.

import type {Candidate} from './catalog-rules.ts';

export type {Candidate} from './catalog-rules.ts';
export {validateCatalog} from './catalog-rules.ts';

export const MAX_QUESTION = '光从空气斜射进入水中时传播方向为什么会发生偏折呢';
export const OVERFLOW_QUESTION = '插在水中的筷子为什么看起来像是在水面那里被折断了';

const timing = {
	durationInFrames: 60,
	narrationEndFrame: 30,
	sentences: [{blocks: [{text: '为什么会偏折', startFrame: 0, endFrame: 30}]}],
};

export const candidates: Candidate[] = [
	{
		id: 'placeholder',
		kind: 'question',
		source: 'tests/render/fixtures/gallery/ok (task 4.1 placeholder)',
		minFrames: 60,
		entranceFrames: 10,
		samples: [
			{id: 'max', content: {question: MAX_QUESTION}, image: null, timing},
			{id: 'overflow', content: {question: OVERFLOW_QUESTION}, image: null, timing},
		],
	},
];
