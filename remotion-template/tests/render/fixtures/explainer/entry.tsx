// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// Bundle entry of the renderer's browser tests: the Explainer composition
// over the fixture components plus the real custom loader, and the probe
// with one scene key, `explainer`, which renders the whole explainer body
// (without audio) over a target's {script, timeline} props.

import React from 'react';
import {registerRoot} from 'remotion';
import {ProbeComposition} from '../../../../src/kit/probe/Probe';
import {CustomScene} from '../../../../src/kit/render/CustomScene';
import {ExplainerBody, ExplainerComposition} from '../../../../src/kit/render/Explainer';
import {themes} from '../../../../src/kit/theme.ts';
import {components} from './scenes';

const theme = themes.light;
const registry = {...components, custom: CustomScene};

type BodyProps = Pick<React.ComponentProps<typeof ExplainerBody>, 'script' | 'timeline'>;

const scenes = {
	explainer: ({script, timeline}: BodyProps) => (
		<ExplainerBody script={script} timeline={timeline} registry={registry} theme={theme} />
	),
};

registerRoot(() => (
	<>
		<ExplainerComposition registry={registry} theme={theme} />
		<ProbeComposition scenes={scenes as React.ComponentProps<typeof ProbeComposition>['scenes']} />
	</>
));
