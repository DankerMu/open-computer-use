// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The `mismatch` gallery fixture (task 4.1): with the `ok` catalog,
// components holds `extra`, which the catalog lacks. The gallery refuses
// before any frame renders, so neither component draws anything.

export const components = {placeholder: () => null, extra: () => null};
