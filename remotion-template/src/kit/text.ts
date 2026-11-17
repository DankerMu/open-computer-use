// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// Chinese text helper of the explainer kit. It splits text into unbreakable
// units and wraps them into lines measured in em of the font size. The lines
// are authoritative: a component renders each as its own block inside a box
// of (maxWidth + HANG) em, and the CSS from `textStyle` is only a fallback.
// The helper never changes the text: its lines join back to the input. No
// line starts with a closing mark or ends with an opening one, and a line
// exceeds maxWidth only by trailing closing marks, by at most HANG em.

export const CLOSING_PUNCTUATION = '，。！？、；：）」』”’》】…%.,!?;:)]}';
export const OPENING_PUNCTUATION = '（「『“‘《【([{';
export const HANG = 2;
// The task 3.3 probe: the image's headless shell has Intl.Segmenter and
// returns multi-character Chinese words such as 折射.
export const USE_INTL_SEGMENTER = true;

const CLOSING = new Set(Array.from(CLOSING_PUNCTUATION));
const OPENING = new Set(Array.from(OPENING_PUNCTUATION));
const SPACE = ' ';
// Printable ASCII is proportional in Noto Sans CJK SC. At 0.5 em a line of
// capitals re-wrapped in the task 3.3 probe. The probe measured the widest
// of [A-Z0-9%] (`%`) at 1 em in the image's headless shell over both theme
// weights, 0.921 em at 400 and 0.963 em at 700; this is the larger, rounded
// up to 0.05.
const ASCII_WIDTH = 1;
// Widths are sums of fractional em values; compare with a tolerance so
// float rounding cannot push a line that fits exactly onto the next one.
const EPSILON = 1e-9;
const ALNUM = /^[A-Za-z0-9]$/;
const DIGIT = /^[0-9]$/;
const HAN = /^\p{Script=Han}$/u;
const CONTROL = /\p{Cc}/u;
const INNER = new Set(['.', ',', '%', '-']);
const SIGN = new Set(['-', '+']);
const SCALE = new Set(['万', '亿']);

// tsconfig `lib: ES2018` has no Intl.Segmenter type, so declare the part used.
type WordSegmenter = {segment(input: string): Iterable<{segment: string}>};
type SegmenterConstructor = new (
	locale: string,
	options: {granularity: 'word'},
) => WordSegmenter;
type WrapOptions = {segmenter?: boolean};
type Measure = {total: number; core: number};
type TextTheme = {brand: {fontBody: string}; type: {lineHeight: number}};

export function width(ch: string): number {
	const code = ch.codePointAt(0);
	return code !== undefined && code >= 0x20 && code <= 0x7e ? ASCII_WIDTH : 1;
}

function sum(chars: Iterable<string>): number {
	let total = 0;
	for (const ch of chars) total += width(ch);
	return total;
}

function isGap(ch: string): boolean {
	return CLOSING.has(ch) || OPENING.has(ch) || ch === SPACE;
}

function startsNumber(chars: string[], i: number): boolean {
	return SIGN.has(chars[i]) && DIGIT.test(chars[i + 1] ?? '');
}

// End of the ASCII atom starting at `start`: letters and digits, with
// `.,%-` between two of them, a leading sign before a digit, and 万/亿
// directly after a digit closing the atom.
function asciiAtomEnd(chars: string[], start: number): number {
	let i = startsNumber(chars, start) ? start + 1 : start;
	while (i < chars.length) {
		const ch = chars[i];
		const between = ALNUM.test(chars[i - 1] ?? '') && ALNUM.test(chars[i + 1] ?? '');
		if (ALNUM.test(ch) || (INNER.has(ch) && between)) {
			i += 1;
		} else {
			return SCALE.has(ch) && DIGIT.test(chars[i - 1] ?? '') ? i + 1 : i;
		}
	}
	return i;
}

function hanWords(run: string): string[] {
	const {Segmenter} = Intl as unknown as {Segmenter: SegmenterConstructor};
	const words = new Segmenter('zh', {granularity: 'word'}).segment(run);
	return Array.from(words, (word) => word.segment);
}

// Atoms as [start, end) code-point ranges, in text order.
function atomRanges(chars: string[], segmenter: boolean): Array<[number, number]> {
	const ranges: Array<[number, number]> = [];
	let i = 0;
	while (i < chars.length) {
		const ch = chars[i];
		if (ALNUM.test(ch) || startsNumber(chars, i)) {
			const end = asciiAtomEnd(chars, i);
			ranges.push([i, end]);
			i = end;
		} else if (HAN.test(ch)) {
			let end = i;
			while (end < chars.length && HAN.test(chars[end])) end += 1;
			const words = segmenter ? hanWords(chars.slice(i, end).join('')) : chars.slice(i, end);
			for (const word of words) {
				const next = i + Array.from(word).length;
				ranges.push([i, next]);
				i = next;
			}
		} else {
			if (!isGap(ch)) ranges.push([i, i + 1]);
			i += 1;
		}
	}
	return ranges;
}

export function units(text: string, {segmenter = USE_INTL_SEGMENTER}: WrapOptions = {}): string[] {
	if (CONTROL.test(text)) throw new RangeError('text must not contain control characters');
	if (text === '') return [];
	const chars = Array.from(text);
	const ranges = atomRanges(chars, segmenter);
	if (ranges.length === 0) return [text];
	// Each gap is cut at its first opening mark: what precedes it (closing
	// marks and spaces) stays with the atom before, the rest goes with the
	// atom after. The edges of the text join the first and last atoms.
	const cuts = [0];
	for (let k = 1; k < ranges.length; k += 1) {
		let cut = ranges[k - 1][1];
		while (cut < ranges[k][0] && !OPENING.has(chars[cut])) cut += 1;
		cuts.push(cut);
	}
	cuts.push(chars.length);
	return ranges.map((_, k) => chars.slice(cuts[k], cuts[k + 1]).join(''));
}

// The core width leaves out the unit's trailing spaces and hangs the
// closing marks of its trailing run of closing marks and spaces, up to HANG
// em. A space before a closing mark is drawn, so it counts.
function measure(unit: string): Measure {
	const chars = Array.from(unit);
	let i = chars.length;
	let spaces = 0;
	for (; i > 0 && chars[i - 1] === SPACE; i -= 1) spaces += width(SPACE);
	let closing = 0;
	for (; i > 0 && (CLOSING.has(chars[i - 1]) || chars[i - 1] === SPACE); i -= 1) {
		if (CLOSING.has(chars[i - 1])) closing += width(chars[i - 1]);
	}
	const total = sum(chars);
	return {total, core: total - Math.min(closing, HANG) - spaces};
}

function leadingOpeningWidth(unit: string): number {
	let total = 0;
	for (const ch of unit) {
		if (!OPENING.has(ch) && ch !== SPACE) break;
		total += width(ch);
	}
	return total;
}

// Pieces of the shape `(opening | space)* char? (closing | space)*`:
// opening marks, and spaces between or after them, bind to the character
// after them; closing marks bind to the character before. A space ends a
// piece only after a character or a closing mark.
function splitByCharacter(unit: string): string[] {
	const pieces: string[] = [];
	let piece = '';
	let tail = false;
	for (const ch of unit) {
		if (CLOSING.has(ch) || ch === SPACE) {
			piece += ch;
			tail = tail || ch !== SPACE;
			continue;
		}
		if (tail) {
			pieces.push(piece);
			piece = '';
		}
		piece += ch;
		tail = !OPENING.has(ch);
	}
	if (piece !== '') pieces.push(piece);
	return pieces;
}

export function wrapLines(
	text: string,
	maxWidth: number,
	{segmenter = USE_INTL_SEGMENTER}: WrapOptions = {},
): string[] {
	if (!Number.isInteger(maxWidth) || maxWidth < 2) {
		throw new RangeError(`maxWidth must be an integer of at least 2, got ${maxWidth}`);
	}
	const lines: string[] = [];
	let line = '';
	let lineTotal = 0;
	const breakLine = () => {
		lines.push(line);
		line = '';
		lineTotal = 0;
	};
	// Everything already on the line counts, including the previous unit's
	// hang and spaces, which sit mid-line once another unit follows.
	const place = (unit: string, {total, core}: Measure) => {
		if (line !== '' && lineTotal + core > maxWidth + EPSILON) breakLine();
		line += unit;
		lineTotal += total;
	};
	for (const unit of units(text, {segmenter})) {
		const measured = measure(unit);
		if (measured.core <= maxWidth + EPSILON) {
			place(unit, measured);
			continue;
		}
		if (leadingOpeningWidth(unit) >= maxWidth - EPSILON) {
			throw new RangeError(`opening marks of ${JSON.stringify(unit)} fill maxWidth ${maxWidth}`);
		}
		if (line !== '') breakLine();
		for (const piece of splitByCharacter(unit)) {
			const pieceMeasure = measure(piece);
			if (pieceMeasure.core > maxWidth + EPSILON) {
				throw new RangeError(`piece ${JSON.stringify(piece)} is wider than maxWidth ${maxWidth}`);
			}
			place(piece, pieceMeasure);
		}
	}
	if (line !== '') lines.push(line);
	return lines;
}

// 万 from 1e4 and 亿 from 1e8, truncated toward zero to one decimal.
export function formatCount(n: number): string {
	if (!Number.isFinite(n)) throw new TypeError(`count must be finite, got ${n}`);
	const size = Math.abs(n);
	if (size < 1e4) return String(n);
	const [divisor, suffix] = size >= 1e8 ? [1e8, '亿'] : [1e4, '万'];
	return `${Math.trunc((n * 10) / divisor) / 10}${suffix}`;
}

export function textStyle(theme: TextTheme) {
	return {
		fontFamily: theme.brand.fontBody,
		lineHeight: theme.type.lineHeight,
		letterSpacing: 0,
		whiteSpace: 'normal',
		wordBreak: 'normal',
		overflowWrap: 'anywhere',
		lineBreak: 'strict',
	} as const;
}
