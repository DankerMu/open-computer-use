// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The scene component of each script kind. Only `custom` has one until
// tasks 4.2–4.4 register theirs; the renderer refuses any other kind,
// naming the scene.

import type React from 'react';
import {CustomScene} from './CustomScene.tsx';

export type Registry = Record<string, React.ComponentType<any>>;

export const registry: Registry = {custom: CustomScene};
