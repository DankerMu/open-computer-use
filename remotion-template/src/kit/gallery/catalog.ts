// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The gallery's candidate catalog (task 4.1). Tasks 4.2–4.4 add each
// candidate here and its component, under the same id, to components.tsx;
// the gallery renders every sample in both theme candidates. It lists no
// candidate until then. Imports only .ts files, so Node and the bundle both
// import it.

import type {Candidate} from './catalog-rules.ts';

export type {Candidate} from './catalog-rules.ts';
export {validateCatalog} from './catalog-rules.ts';

export const candidates: Candidate[] = [];
