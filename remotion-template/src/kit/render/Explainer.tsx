// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The explainer renderer: a validated script.json and its timeline.json
// composed into the video. The timeline decides time: every scene plays in
// a Sequence on its timeline frames, every caption page shows during its
// block's frames, and the composition lasts timeline.totalFrames. Each
// scene but the last fades out in its timeline transition window through
// SceneExit, which wraps the scene alone, never the captions. Before
// any frame renders, calculateMetadata refuses input props that are not
// objects, a music prop that is not a boolean, a script that fails
// validateScript, a timeline that disagrees with it, a kind without a
// component, and a custom index that does not match the script. With music
// true, the render worker's public/music.wav (bin/music.mjs: looped, ducked
// and faded at its final level) plays at volume 1 beside the narration.

import React, {useMemo} from 'react';
import {Audio, Composition, Sequence, staticFile} from 'remotion';
import {explainerScenes} from '../../../bin/explainer-scenes.mjs';
import {validateScript} from '../../../bin/script-schema.mjs';
import type {themes} from '../theme.ts';
import {Captions} from './Captions.tsx';
import {captionPages} from './captions.ts';
import {customProblem} from './CustomScene.tsx';
import {KitFrame} from './KitFrame.tsx';
import type {Registry} from './registry.ts';
import {SceneExit, type Exit} from './SceneExit.tsx';

type Theme = (typeof themes)['light'];
type Input = Record<string, unknown>;
type ExplainerProps = {script: Input | null; timeline: Input | null; music?: unknown};
type SceneEntry = {
	index: number;
	where: string;
	kind: string;
	component: string | null;
	startFrame: number;
	durationInFrames: number;
	exit: Exit;
	props: Record<string, unknown>;
};
type Problem = {message: string};

const WIDTH = 1920;
const HEIGHT = 1080;
const FPS = 30;

function fail(message: string): never {
	throw new Error(`kit render: ${message}`);
}

const isObject = (value: unknown): value is Input => value !== null && typeof value === 'object' && !Array.isArray(value);
const hasKind = (registry: Registry, kind: string) => Object.prototype.hasOwnProperty.call(registry, kind);
const scenesOf = (script: Input, timeline: Input) => explainerScenes(script, timeline) as SceneEntry[];

export const ExplainerBody = ({
	script,
	timeline,
	registry,
	theme,
}: {
	script: Input;
	timeline: Input;
	registry: Registry;
	theme: Theme;
}) => {
	const scenes = useMemo(() => scenesOf(script, timeline), [script, timeline]);
	const pages = useMemo(() => captionPages(timeline as Parameters<typeof captionPages>[0]), [timeline]);
	return (
		<KitFrame theme={theme}>
			{scenes.map((entry) => {
				if (!hasKind(registry, entry.kind)) fail(`${entry.where}: no component for kind "${entry.kind}"`);
				const Scene = registry[entry.kind];
				return (
					<Sequence key={entry.index} name={entry.where} from={entry.startFrame} durationInFrames={entry.durationInFrames}>
						<SceneExit exit={entry.exit}>
							<Scene {...entry.props} theme={theme} />
						</SceneExit>
					</Sequence>
				);
			})}
			<Captions pages={pages} theme={theme} />
		</KitFrame>
	);
};

// The checks of calculateMetadata, in order; returns the composition's
// length and the validated props.
function explainerMetadata({script, timeline, music}: ExplainerProps, registry: Registry) {
	if (!isObject(script) || !isObject(timeline)) fail('input props need script and timeline objects');
	if (music !== undefined && typeof music !== 'boolean') fail('input props: music must be true or false');
	const result = validateScript(JSON.stringify(script)) as {ok: true; script: Input} | {ok: false; errors: Problem[]};
	if (!result.ok) {
		const [first, ...rest] = result.errors;
		fail(rest.length > 0 ? `${first.message} (and ${rest.length} more)` : first.message);
	}
	let scenes: SceneEntry[];
	try {
		scenes = scenesOf(result.script, timeline);
	} catch (error) {
		fail(error instanceof Error ? error.message : String(error));
	}
	for (const entry of scenes) {
		if (!hasKind(registry, entry.kind)) fail(`${entry.where}: no component for kind "${entry.kind}"`);
	}
	for (const entry of scenes) {
		if (entry.component === null) continue;
		const problem = customProblem(entry.component);
		if (problem !== null) fail(`${entry.where}: ${problem}`);
	}
	const props = {script: result.script, timeline};
	return {durationInFrames: timeline.totalFrames as number, props: music === undefined ? props : {...props, music}};
}

export const ExplainerComposition = ({registry, theme}: {registry: Registry; theme: Theme}) => {
	const component = useMemo(
		() =>
			({script, timeline, music}: ExplainerProps) => (
				<>
					<ExplainerBody script={script as Input} timeline={timeline as Input} registry={registry} theme={theme} />
					<Audio src={staticFile('narration.wav')} />
					{music === true ? <Audio src={staticFile('music.wav')} /> : null}
				</>
			),
		[registry, theme],
	);
	const calculateMetadata = useMemo(
		() =>
			({props}: {props: ExplainerProps}) =>
				explainerMetadata(props, registry),
		[registry],
	);
	return (
		<Composition
			id="Explainer"
			component={component}
			width={WIDTH}
			height={HEIGHT}
			fps={FPS}
			defaultProps={{script: null, timeline: null} as ExplainerProps}
			calculateMetadata={calculateMetadata}
		/>
	);
};
