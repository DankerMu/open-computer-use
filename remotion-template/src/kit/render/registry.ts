// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The scene component of each script kind: `custom`, and each catalog
// candidate's kind taken by import from the gallery's components, the same
// function object the gallery rendered and the user approved (task 4.2), in
// catalog order. No copy and no wrapper. The renderer refuses any other
// kind, naming the scene.

import type React from 'react';
import {components} from '../gallery/components.tsx';
import {CustomScene} from './CustomScene.tsx';

export type Registry = Record<string, React.ComponentType<any>>;

export const registry: Registry = {
	custom: CustomScene,
	'hook': components['hook'],
	'question': components['question'],
	'concept-card': components['concept-card'],
	'definition-quote': components['definition-quote'],
	'summary': components['summary'],
	'end-card': components['end-card'],
	'process-steps': components['process-steps'],
	'cause-effect': components['cause-effect'],
	'comparison': components['comparison'],
	'timeline': components['timeline'],
};
