// SPDX-License-Identifier: MIT
// Source: degueba/onda 3c814051269597b31fee1603bef7ccfb93d091b6 lib/primitives/index.ts MIT
// Copyright (c) 2026 Rodrigo Botelho. Unmodified.
// Shared visual primitives — the documented exception to the component
// contract's "self-contained" rule (CLAUDE.md §4): catalog components MAY
// import these building blocks. Keep this set small and material-level
// (surfaces, glows, fields) — not feature components.

export { Surface, type SurfaceProps } from './Surface';
export { Glow, type GlowProps } from './Glow';
export { GridField, type GridFieldProps } from './GridField';
export { Camera, type CameraProps } from './Camera';
export { MotionBlur, MotionTrail, type MotionBlurProps, type MotionTrailProps } from './MotionBlur';
