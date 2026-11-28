// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The kit's presets (design Decision 8): the one source of each preset's
// approval id form, theme id, required kinds and theme candidates. An
// approval id is a theme id of this table, a bare kind for the explainer, or
// `<idPrefix><kind>` for another preset. Its theme pick in
// gallery/approvals.json must be one of its theme candidates. A leaf with
// no imports: bin/ (through type stripping) and the bundle both import it.

type Preset = {idPrefix: string; themeId: string; requiredKinds: string[]; themeCandidates: string[]};

// The preset of a script without `preset` and of an export without `preset`.
export const DEFAULT_PRESET = 'explainer';

export const PRESETS: Record<string, Preset> = {
	explainer: {idPrefix: '', themeId: 'theme', requiredKinds: ['hook', 'summary', 'end-card'], themeCandidates: ['light']},
	// No report kind and no report theme yet: task 4.2 adds the candidates.
	report: {idPrefix: 'report:', themeId: 'report:theme', requiredKinds: ['cover', 'summary', 'end-card'], themeCandidates: []},
};

// The approval id of a preset's kind: the id its page exports.
export const approvalId = (preset: string, kind: string) => `${PRESETS[preset].idPrefix}${kind}`;
