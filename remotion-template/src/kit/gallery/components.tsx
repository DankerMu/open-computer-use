// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The gallery's candidate components, keyed by catalog id (task 4.1). An
// approved candidate enters src/kit/render/registry.ts by importing the
// same component from here. The gallery refuses a key the catalog lacks,
// and a catalog id without a key. Batch 4.2 adds the six scenes of
// src/kit/scenes/ and batch 4.3 the next four, in catalog order.

import type React from 'react';
import {CauseEffect} from '../scenes/CauseEffect.tsx';
import {Comparison} from '../scenes/Comparison.tsx';
import {ConceptCard} from '../scenes/ConceptCard.tsx';
import {DefinitionQuote} from '../scenes/DefinitionQuote.tsx';
import {EndCard} from '../scenes/EndCard.tsx';
import {Hook} from '../scenes/Hook.tsx';
import {ProcessSteps} from '../scenes/ProcessSteps.tsx';
import {Question} from '../scenes/Question.tsx';
import {Summary} from '../scenes/Summary.tsx';
import {Timeline} from '../scenes/Timeline.tsx';

export const components: Record<string, React.ComponentType<any>> = {
	hook: Hook,
	question: Question,
	'concept-card': ConceptCard,
	'definition-quote': DefinitionQuote,
	summary: Summary,
	'end-card': EndCard,
	'process-steps': ProcessSteps,
	'cause-effect': CauseEffect,
	comparison: Comparison,
	timeline: Timeline,
};
