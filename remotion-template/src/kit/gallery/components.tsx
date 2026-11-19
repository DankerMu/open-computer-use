// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The gallery's candidate components, keyed by catalog id (task 4.1). An
// approved candidate enters src/kit/render/registry.ts by importing the
// same component from here. The gallery refuses a key the catalog lacks,
// and a catalog id without a key. Empty until tasks 4.2–4.4.

import type React from 'react';

export const components: Record<string, React.ComponentType<any>> = {};
