// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The one-line escape for text echoed into a message. Imports nothing, so
// script-schema.mjs and report-data.mjs, both in the renderer bundle, share
// one copy.

const ESCAPES = { "\n": "\\n", "\t": "\\t", "\r": "\\r" };

// Keeps text on one line: C0, DEL, C1, U+2028 and U+2029 written as
// escapes. Used alone for a JSON.parse message (V8 bounds it, and its
// positional form ends with the line and column) and for an ffmpeg reason.
export function escapeLine(text) {
  return text.replace(
    /[\u0000-\u001f\u007f-\u009f\u2028\u2029]/g,
    (c) => ESCAPES[c] ?? `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
}
