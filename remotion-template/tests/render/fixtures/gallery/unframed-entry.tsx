// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// Bundle entry of the gallery's brand test (task 4.1): `Unframed` renders
// the scratch project's placeholder (the `ok` fixture, copied into
// src/kit/gallery/) with its `max` sample and the light theme, but without
// KitFrame, so THEME.accent falls back to Onda's own #D96B82.

import React from 'react';
import {Composition, registerRoot} from 'remotion';
import {candidates} from '../../../../src/kit/gallery/catalog.ts';
import {components} from '../../../../src/kit/gallery/components.tsx';
import {themes} from '../../../../src/kit/theme.ts';

type Props = Record<string, unknown>;

const [candidate] = candidates;
const [sample] = candidate.samples;
const Placeholder = (components as Record<string, React.ComponentType<Props>>)[candidate.id];

const Unframed = () => <Placeholder content={sample.content} image={sample.image} timing={sample.timing} theme={themes.light} />;

registerRoot(() => (
	<Composition id="Unframed" component={Unframed} width={1920} height={1080} fps={30} durationInFrames={sample.timing.durationInFrames} />
));
