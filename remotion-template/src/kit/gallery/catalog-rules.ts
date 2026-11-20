// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The gallery catalog's format and its rules (task 4.1). catalog.ts
// re-exports validateCatalog from here, so a fixture catalog copied over
// catalog.ts keeps the same rules. Imports only .mjs and .ts files: Node
// (through type stripping) and the bundle both import it. validateCatalog
// throws at the first rule that fails, in the fixture's order, as
// `kit gallery: <where>: <problem>`.

import {BODY_KINDS, IMAGE_NAME, contentSchema, escapeLine, fieldPath} from '../../../bin/script-schema.mjs';

export type Block = {text: string; startFrame: number; endFrame: number};
// The shape of a scene's props.timing (task 3.8), frames relative to the scene.
export type Timing = {durationInFrames: number; narrationEndFrame: number; sentences: Array<{blocks: Block[]}>};
export type Sample = {id: string; content: Record<string, unknown>; image: string | null; timing: Timing};
export type Candidate = {
	id: string;
	kind: string;
	source: string;
	minFrames: number;
	entranceFrames: number;
	samples: Sample[];
};

// The theme sample: candidate id `theme`, sample id `sample`, 90 frames
// with a 15-frame entrance.
export const THEME_ID = 'theme';
export const THEME_SAMPLE = {id: 'sample', durationInFrames: 90, entranceFrames: 15};
export const THEME_NAMES = ['light', 'dark'];

const ID = /^[a-z0-9]+(-[a-z0-9]+)*$/;
const ID_LENGTH = 40;
const SAMPLE_ID_LENGTH = 20;
const KINDS = new Set([...BODY_KINDS, 'hook', 'summary', 'end-card']);
const LINE_BREAK = /[\n\r\u2028\u2029]/;
const SENTENCES = 4;
const BLOCKS = 4;
const BLOCK_TEXT = 16;

type Loose = Record<string, unknown>;
type ZodIssue = {path: Array<string | number>; message: string};

function fail(where: string, problem: string): never {
	throw new Error(`kit gallery: ${where}: ${problem}`);
}

const isObject = (value: unknown): value is Loose => value !== null && typeof value === 'object' && !Array.isArray(value);
const asObject = (value: unknown): Loose => (isObject(value) ? value : {});
const label = (value: unknown) => escapeLine(String(value)) as string;
const isInteger = (value: unknown): value is number => Number.isInteger(value);
const validId = (value: unknown, length: number): value is string =>
	typeof value === 'string' && value.length <= length && ID.test(value);

// A candidate id: lowercase words joined by single hyphens, at most 40
// characters. The reserved `theme` is each caller's own case.
export const isCandidateId = (value: unknown): value is string => validId(value, ID_LENGTH);

function checkContent(where: string, kind: string, content: unknown) {
	const parsed = contentSchema(kind).safeParse(content) as {success: boolean; error?: {issues: ZodIssue[]}};
	if (parsed.success) return;
	const [issue] = parsed.error!.issues;
	const at = issue.path.length > 0 ? `${fieldPath(issue.path)}: ` : '';
	fail(where, `content: ${escapeLine(`${at}${issue.message}`)}`);
}

function checkTiming(where: string, minFrames: number, timing: unknown) {
	const {durationInFrames, narrationEndFrame, sentences} = asObject(timing);
	if (!isInteger(durationInFrames) || durationInFrames < minFrames) {
		fail(where, `timing.durationInFrames must be an integer ≥ ${minFrames}`);
	}
	if (!isInteger(narrationEndFrame) || narrationEndFrame < 0 || narrationEndFrame > durationInFrames) {
		fail(where, 'timing.narrationEndFrame is out of range');
	}
	const blocksOf = (sentence: unknown) => asObject(sentence).blocks;
	const shaped =
		Array.isArray(sentences) &&
		sentences.length <= SENTENCES &&
		sentences.every((sentence) => {
			const blocks = blocksOf(sentence);
			return Array.isArray(blocks) && blocks.length >= 1 && blocks.length <= BLOCKS;
		});
	if (!shaped) fail(where, `timing.sentences must hold 0–${SENTENCES} sentences of 1–${BLOCKS} blocks`);
	let previousEnd = 0;
	(sentences as unknown[]).forEach((sentence, s) => {
		(blocksOf(sentence) as unknown[]).forEach((value, b) => {
			const {text, startFrame, endFrame} = asObject(value);
			const at = `timing block ${s + 1}.${b + 1}`;
			const length = typeof text === 'string' ? Array.from(text).length : 0;
			if (length < 1 || length > BLOCK_TEXT) fail(where, `${at} text must be 1–${BLOCK_TEXT} characters`);
			const inOrder =
				isInteger(startFrame) &&
				isInteger(endFrame) &&
				startFrame >= previousEnd &&
				startFrame < endFrame &&
				endFrame <= narrationEndFrame;
			if (!inOrder) fail(where, `${at} frames are out of order or range`);
			previousEnd = endFrame as number;
		});
	});
}

function checkSamples(id: string, kind: string, minFrames: number, samples: unknown[]) {
	const seen = new Set<string>();
	for (const value of samples) {
		const sample = asObject(value);
		const where = `${id}/${label(sample.id)}`;
		if (!validId(sample.id, SAMPLE_ID_LENGTH) || seen.has(sample.id)) fail(where, 'id is invalid or repeated');
		seen.add(sample.id);
		checkContent(where, kind, sample.content);
		if (sample.image !== null && !(typeof sample.image === 'string' && IMAGE_NAME.test(sample.image))) {
			fail(where, 'image is invalid');
		}
		checkTiming(where, minFrames, sample.timing);
	}
}

export function validateCatalog(candidates: unknown): void {
	if (!Array.isArray(candidates)) fail('catalog', 'is not an array');
	const seen = new Set<string>();
	for (const value of candidates) {
		const {id, kind, source, minFrames, entranceFrames, samples} = asObject(value);
		if (!isCandidateId(id) || id === THEME_ID || seen.has(id)) fail(label(id), 'id is invalid, reserved or repeated');
		seen.add(id);
		if (typeof kind !== 'string' || !KINDS.has(kind)) fail(id, 'kind is not a scene kind');
		if (typeof source !== 'string' || source.trim() === '' || LINE_BREAK.test(source)) {
			fail(id, 'source must be one non-empty line');
		}
		if (!isInteger(minFrames) || !isInteger(entranceFrames) || entranceFrames < 0 || entranceFrames >= minFrames) {
			fail(id, 'needs integers 0 ≤ entranceFrames < minFrames');
		}
		if (!Array.isArray(samples) || samples.length === 0) fail(id, 'has no samples');
		checkSamples(id, kind, minFrames, samples);
	}
}
