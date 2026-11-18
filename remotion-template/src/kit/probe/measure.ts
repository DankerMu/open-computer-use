// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// One frame's text measurement for the kit probe. Runs in the page and
// imports nothing. Every rendered text node under the scene root is found by
// walking the DOM, so text without kit markers is measured too. Boxes are
// client rects, which are canvas pixels when the root lies at the canvas
// origin at scale 1 (the probe composition waits for that).
//
// Known limit: rendered scale reads only the `transform` property, not
// `scale`, `rotate`, `translate` or `zoom`.

type Box = {x: number; y: number; width: number; height: number};
type Edges = {left: number; top: number; right: number; bottom: number};
type TextRecord = {
	path: string;
	text: string;
	glyphBox: Box;
	minFontSizePx: number;
	scale: number;
	lines: string[];
	clippedBy: string[];
};
type Measurement = {
	fontPresent: boolean;
	texts: TextRecord[];
	hidden: Array<{path: string; text: string}>;
	marked: Array<{path: string; input: string; rendered: string}>;
	scrolls: Array<{path: string; scrollWidth: number; clientWidth: number}>;
	masks: Array<{path: string; clipPath: string; clipBox: Box | null}>;
};

const KIT_FONT = '"Noto Sans CJK SC"';
const FONT_SAMPLE = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
const NON_SPACE = /\S/;
const FLOW_DISPLAYS = new Set(['inline', 'contents']);
const CLIPPING_OVERFLOW = new Set(['hidden', 'clip', 'auto', 'scroll']);
const SCROLLING_OVERFLOW = new Set(['auto', 'scroll']);
const INSET = /^inset\(([^()]*)\)$/;
const INSET_LENGTH = /^(-?(?:\d+\.?\d*|\.\d+))(px|%)$/;

const round = (n: number): number => Math.round(n * 100) / 100;

function toBox(e: Edges): Box {
	return {x: round(e.left), y: round(e.top), width: round(e.right - e.left), height: round(e.bottom - e.top)};
}

function edgesOf(r: DOMRect): Edges {
	return {left: r.left, top: r.top, right: r.right, bottom: r.bottom};
}

function unite(a: Edges | null, b: Edges): Edges {
	if (a === null) return b;
	return {
		left: Math.min(a.left, b.left),
		top: Math.min(a.top, b.top),
		right: Math.max(a.right, b.right),
		bottom: Math.max(a.bottom, b.bottom),
	};
}

// An empty box stays at its left/top edge rather than turning negative.
function normalize(e: Edges): Edges {
	return {left: e.left, top: e.top, right: Math.max(e.left, e.right), bottom: Math.max(e.top, e.bottom)};
}

function intersect(a: Edges, b: Edges): Edges {
	return normalize({
		left: Math.max(a.left, b.left),
		top: Math.max(a.top, b.top),
		right: Math.min(a.right, b.right),
		bottom: Math.min(a.bottom, b.bottom),
	});
}

// `<tag>:nth-child(<i>)` steps from root, joined by `>`; root itself is ''.
function pathOf(el: Element, root: Element): string {
	const steps: string[] = [];
	for (let e: Element = el; e !== root; ) {
		const parent = e.parentElement;
		if (parent === null) throw new Error('kit probe: element outside the scene root');
		steps.push(`${e.localName}:nth-child(${Array.prototype.indexOf.call(parent.children, e) + 1})`);
		e = parent;
	}
	return steps.reverse().join('>');
}

// The element and its ancestors up to and including root.
function chain(el: Element, root: Element): Element[] {
	const list: Element[] = [];
	for (let e: Element | null = el; e !== null; e = e.parentElement) {
		list.push(e);
		if (e === root) return list;
	}
	throw new Error('kit probe: element outside the scene root');
}

function matrixScale(m: {a: number; b: number; c: number; d: number}): number {
	return Math.sqrt(Math.abs(m.a * m.d - m.b * m.c));
}

function transformScale(el: Element): number {
	const transform = getComputedStyle(el).transform;
	return transform === 'none' ? 1 : matrixScale(new DOMMatrix(transform));
}

// Product of the transforms' area scale from el up to root. SVG content
// takes the outermost <svg>'s chain times its own CTM, which carries the
// viewBox scale.
function renderedScale(el: Element, root: Element): number {
	if (el instanceof SVGGraphicsElement && el.ownerSVGElement !== null) {
		let outer: SVGSVGElement = el.ownerSVGElement;
		while (outer.ownerSVGElement !== null) outer = outer.ownerSVGElement;
		const ctm = el.getCTM();
		if (ctm === null) throw new Error(`kit probe: no CTM for SVG text at "${pathOf(el, root)}"`);
		return chain(outer, root).reduce((scale, e) => scale * transformScale(e), 1) * matrixScale(ctm);
	}
	return chain(el, root).reduce((scale, e) => scale * transformScale(e), 1);
}

function isVisible(el: Element, root: Element): boolean {
	const opacity = chain(el, root).reduce((product, e) => product * Number(getComputedStyle(e).opacity), 1);
	return opacity > 0 && getComputedStyle(el).visibility !== 'hidden';
}

function blockOf(el: Element, root: Element): Element {
	for (const e of chain(el, root)) {
		if (!FLOW_DISPLAYS.has(getComputedStyle(e).display)) return e;
	}
	return root;
}

// Whether el is the containing block of an absolutely (or, with `fixed`,
// a fixed) positioned descendant: a positioned element for `absolute`, and
// for both an element with a transform, perspective, filter, paint or
// layout containment, or `will-change: transform`.
function containsPositioned(el: Element, fixed: boolean): boolean {
	const style = getComputedStyle(el);
	if (!fixed && style.position !== 'static') return true;
	return (
		style.transform !== 'none' ||
		style.perspective !== 'none' ||
		style.filter !== 'none' ||
		/\b(paint|layout|strict|content)\b/.test(style.contain) ||
		/\btransform\b/.test(style.willChange)
	);
}

// The record element's clipping ancestors, nearest first: the element
// itself and those of its ancestors up to root that clip and hold the
// containing block of every absolutely or fixed positioned element between
// them and the record. Overflow does not clip a positioned descendant whose
// containing block lies outside it (CSS 2.1 §11.1.1); a fixed element with
// no transformed ancestor belongs to the viewport, which no element clips.
function clippingAncestors(el: Element, root: Element): Element[] {
	const clipping: Element[] = [];
	const pending: boolean[] = [];
	for (const e of chain(el, root)) {
		if (e !== el) {
			for (let k = pending.length - 1; k >= 0; k -= 1) {
				if (containsPositioned(e, pending[k])) pending.splice(k, 1);
			}
		}
		if (pending.length === 0 && isClipping(e)) clipping.push(e);
		const position = getComputedStyle(e).position;
		if (position === 'absolute' || position === 'fixed') pending.push(position === 'fixed');
	}
	return clipping;
}

function isClipping(el: Element): boolean {
	const style = getComputedStyle(el);
	return CLIPPING_OVERFLOW.has(style.overflowX) || CLIPPING_OVERFLOW.has(style.overflowY) || style.clipPath !== 'none';
}

// `inset()` with one to four px or % lengths and no `round`, as
// [top, right, bottom, left] in pixels of the scaled border box; null for
// anything else.
function insetOf(clipPath: string, border: Edges, scale: number): Edges | null {
	const match = INSET.exec(clipPath);
	if (match === null) return null;
	const tokens = match[1].trim().split(/\s+/);
	if (tokens.length < 1 || tokens.length > 4) return null;
	const lengths: Array<[number, string]> = [];
	for (const token of tokens) {
		const length = INSET_LENGTH.exec(token);
		if (length === null) return null;
		lengths.push([Number(length[1]), length[2]]);
	}
	const [top, right = top, bottom = top, left = right] = lengths;
	const width = border.right - border.left;
	const height = border.bottom - border.top;
	const px = ([value, unit]: [number, string], basis: number) => (unit === '%' ? (value / 100) * basis : value * scale);
	return {
		left: border.left + px(left, width),
		top: border.top + px(top, height),
		right: border.right - px(right, width),
		bottom: border.bottom - px(bottom, height),
	};
}

// Overflow clips to the padding box; an inset() clip-path to that inset of
// the border box, which negative insets extend beyond the border box; both
// to their intersection; any other clip-path is not measurable.
function clipBoxOf(el: Element, root: Element): Box | null {
	const style = getComputedStyle(el);
	const scale = renderedScale(el, root);
	const border = edgesOf(el.getBoundingClientRect());
	let clip: Edges | null = null;
	if (CLIPPING_OVERFLOW.has(style.overflowX) || CLIPPING_OVERFLOW.has(style.overflowY)) {
		clip = {
			left: border.left + parseFloat(style.borderLeftWidth) * scale,
			top: border.top + parseFloat(style.borderTopWidth) * scale,
			right: border.right - parseFloat(style.borderRightWidth) * scale,
			bottom: border.bottom - parseFloat(style.borderBottomWidth) * scale,
		};
	}
	if (style.clipPath !== 'none') {
		const inset = insetOf(style.clipPath, border, scale);
		if (inset === null) return null;
		clip = clip === null ? normalize(inset) : intersect(clip, inset);
	}
	if (clip === null) throw new Error(`kit probe: "${pathOf(el, root)}" is not a clipping element`);
	return toBox(clip);
}

// A text rect spans the font's ascent plus descent whatever the line
// height; trim it about its centre to the line box height when that is
// smaller.
function trim(rect: DOMRect, lineHeightPx: number | null): Edges {
	const edges = edgesOf(rect);
	if (lineHeightPx === null || lineHeightPx >= rect.height) return edges;
	const centre = (rect.top + rect.bottom) / 2;
	return {...edges, top: centre - lineHeightPx / 2, bottom: centre + lineHeightPx / 2};
}

function lineHeightOf(el: Element, scale: number): number | null {
	const lineHeight = getComputedStyle(el).lineHeight;
	return lineHeight.endsWith('px') ? parseFloat(lineHeight) * scale : null;
}

// A code point starts a new line when its rect overlaps the previous one's
// vertically by less than half the smaller height.
function startsLine(previous: Edges, next: Edges): boolean {
	const overlap = Math.min(previous.bottom, next.bottom) - Math.max(previous.top, next.top);
	const smaller = Math.min(previous.bottom - previous.top, next.bottom - next.top);
	return overlap < smaller / 2;
}

function measureRecord(el: Element, nodes: Text[], root: Element): TextRecord {
	const range = document.createRange();
	let glyphs: Edges | null = null;
	let minFontSize = Infinity;
	let maxScale = 0;
	let previous: Edges | null = null;
	const lines: string[] = [];
	for (const node of nodes) {
		const parent = node.parentElement as Element;
		const scale = renderedScale(parent, root);
		maxScale = Math.max(maxScale, scale);
		const lineHeight = lineHeightOf(parent, scale);
		minFontSize = Math.min(minFontSize, parseFloat(getComputedStyle(parent).fontSize) * scale);
		let offset = 0;
		for (const point of node.data) {
			range.setStart(node, offset);
			range.setEnd(node, offset + point.length);
			offset += point.length;
			const rects = Array.from(range.getClientRects(), (rect) => trim(rect, lineHeight));
			if (NON_SPACE.test(point)) for (const rect of rects) glyphs = unite(glyphs, rect);
			if (rects.length > 0) {
				if (previous === null || startsLine(previous, rects[0])) lines.push('');
				previous = rects[0];
			} else if (lines.length === 0) {
				lines.push('');
			}
			lines[lines.length - 1] += point;
		}
	}
	const path = pathOf(el, root);
	if (glyphs === null) throw new Error(`kit probe: no glyph rect for text at "${path}"`);
	return {
		path,
		text: nodes.map((node) => node.data).join(''),
		glyphBox: toBox(glyphs),
		minFontSizePx: round(minFontSize),
		scale: round(maxScale),
		lines,
		clippedBy: clippingAncestors(el, root).map((e) => pathOf(e, root)),
	};
}

function byDocumentOrder(a: Node, b: Node): number {
	if (a === b) return 0;
	return a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
}

export function fontPresent(family: string): boolean {
	const context = document.createElement('canvas').getContext('2d');
	if (context === null) throw new Error('kit probe: no 2D canvas to measure fonts');
	const widthIn = (font: string) => {
		context.font = `48px ${font}`;
		return context.measureText(FONT_SAMPLE).width;
	};
	return widthIn(`${family}, monospace`) !== widthIn('monospace') && widthIn(`${family}, serif`) !== widthIn('serif');
}

export function measureFrame(root: Element): Measurement {
	const groups = new Map<Element, Text[]>();
	const hidden: Measurement['hidden'] = [];
	const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
	const range = document.createRange();
	for (let node = walker.nextNode() as Text | null; node !== null; node = walker.nextNode() as Text | null) {
		const parent = node.parentElement;
		if (parent === null || !NON_SPACE.test(node.data)) continue;
		range.selectNodeContents(node);
		if (range.getClientRects().length === 0) continue;
		if (!isVisible(parent, root)) {
			hidden.push({path: pathOf(parent, root), text: node.data});
			continue;
		}
		const block = blockOf(parent, root);
		const group = groups.get(block);
		if (group === undefined) groups.set(block, [node]);
		else group.push(node);
	}
	const elements = Array.from(groups.keys()).sort(byDocumentOrder);
	const texts = elements.map((el) => measureRecord(el, groups.get(el) as Text[], root));

	const marked = [root, ...Array.from(root.querySelectorAll('[data-kit-text]'))]
		.filter((el) => el.hasAttribute('data-kit-text'))
		.map((el) => ({path: pathOf(el, root), input: el.getAttribute('data-kit-text') as string, rendered: el.textContent ?? ''}));

	const holders = new Set<Element>();
	for (const el of elements) for (const e of chain(el, root)) holders.add(e);
	const scrolls = Array.from(holders)
		.sort(byDocumentOrder)
		.filter((el) => SCROLLING_OVERFLOW.has(getComputedStyle(el).overflowX) && el.clientWidth > 0 && el.scrollWidth > el.clientWidth)
		.map((el) => ({path: pathOf(el, root), scrollWidth: el.scrollWidth, clientWidth: el.clientWidth}));

	const masks: Measurement['masks'] = [];
	const seen = new Set<string>();
	for (const el of elements) {
		for (const e of clippingAncestors(el, root)) {
			const path = pathOf(e, root);
			if (seen.has(path)) continue;
			seen.add(path);
			masks.push({path, clipPath: getComputedStyle(e).clipPath, clipBox: clipBoxOf(e, root)});
		}
	}

	return {fontPresent: fontPresent(KIT_FONT), texts, hidden, marked, scrolls, masks};
}
