// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// Bundle entry of the probe's browser tests: the probe composition over the
// fixture scenes. The template's own root does not register KitProbe (3.8).

import React from 'react';
import {registerRoot} from 'remotion';
import {ProbeComposition} from '../../../src/kit/probe/Probe';
import {scenes} from './scenes';

registerRoot(() => <ProbeComposition scenes={scenes} />);
