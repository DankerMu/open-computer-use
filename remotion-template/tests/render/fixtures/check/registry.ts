// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The fixture registry of kit check's browser tests (task 5.1). The tests
// copy it over a scratch project's src/kit/render/registry.ts, so the
// project's KitProbe has 3.8's fixture scene components for hook, question,
// concept card, summary and end card, plus the real custom loader. It never
// reaches a project: kit new does not copy tests/.

import type React from 'react';
import {components} from '../../../tests/render/fixtures/explainer/scenes.tsx';
import {CustomScene} from './CustomScene.tsx';

export type Registry = Record<string, React.ComponentType<any>>;

export const registry: Registry = {...components, custom: CustomScene};
