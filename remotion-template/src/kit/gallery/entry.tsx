// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// Bundle entry of the gallery (task 4.1), which bin/gallery.mjs bundles.
// KitGallerySample renders one sample, {candidate, sample, theme}, inside
// KitFrame: a catalog sample through its component, or the theme sample;
// KitProbe probes the same render under the scene key `gallery-sample`.
// Both refuse an id the catalog lacks, and a catalog whose ids differ from
// the keys of components.tsx, which only the bundle can import.

import React from 'react';
import {Composition, registerRoot} from 'remotion';
import {ProbeComposition} from '../probe/Probe.tsx';
import {framed} from '../render/KitFrame.tsx';
import {themes} from '../theme.ts';
import {candidates} from './catalog.ts';
import {THEME_ID, THEME_NAMES, THEME_SAMPLE} from './catalog-rules.ts';
import {components} from './components.tsx';
import {ThemeSample} from './ThemeSample.tsx';

type SampleProps = {candidate: string; sample: string; theme: string};

const WIDTH = 1920;
const HEIGHT = 1080;
const FPS = 30;

const has = (object: object, key: string) => Object.prototype.hasOwnProperty.call(object, key);
const shown = framed({...components, [THEME_ID]: ThemeSample});

function fail(message: string): never {
	throw new Error(`kit gallery: ${message}`);
}

function checkComponents() {
	const ids = new Set(candidates.map(({id}) => id));
	for (const key of Object.keys(components)) {
		if (!ids.has(key)) fail(`components has ${JSON.stringify(key)}, the catalog does not`);
	}
	for (const id of ids) {
		if (!has(components, id)) fail(`the catalog has ${JSON.stringify(id)}, components does not`);
	}
}

// The component, its props and its length for one sample.
function resolveSample({candidate, sample, theme}: SampleProps) {
	checkComponents();
	if (!THEME_NAMES.includes(theme)) {
		fail(`theme ${JSON.stringify(theme)} is not ${THEME_NAMES.map((name) => JSON.stringify(name)).join(' or ')}`);
	}
	const kitTheme = themes[theme as keyof typeof themes];
	if (candidate === THEME_ID) {
		if (sample !== THEME_SAMPLE.id) fail(`${THEME_ID}/${sample}: not in the catalog`);
		return {Component: shown[THEME_ID], props: {theme: kitTheme}, durationInFrames: THEME_SAMPLE.durationInFrames};
	}
	const entry = candidates.find(({id}) => id === candidate);
	if (entry === undefined) fail(`${candidate}: not in the catalog`);
	const found = entry.samples.find(({id}) => id === sample);
	if (found === undefined) fail(`${candidate}/${sample}: not in the catalog`);
	const {content, image, timing} = found;
	return {Component: shown[candidate], props: {content, image, timing, theme: kitTheme}, durationInFrames: timing.durationInFrames};
}

const GallerySample = (props: SampleProps) => {
	const {Component, props: sampleProps} = resolveSample(props);
	return <Component {...sampleProps} />;
};

const probeScenes = {'gallery-sample': GallerySample} as unknown as React.ComponentProps<typeof ProbeComposition>['scenes'];
const defaultProps: SampleProps = {candidate: THEME_ID, sample: THEME_SAMPLE.id, theme: 'light'};

registerRoot(() => (
	<>
		<Composition
			id="KitGallerySample"
			component={GallerySample}
			width={WIDTH}
			height={HEIGHT}
			fps={FPS}
			defaultProps={defaultProps}
			calculateMetadata={({props}: {props: SampleProps}) => ({durationInFrames: resolveSample(props).durationInFrames})}
		/>
		<ProbeComposition scenes={probeScenes} />
	</>
));
