// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The kit's shared audio helpers (bin/audio.mjs, issue #883): the 44-byte
// WAV header of narration.wav and the music stem, and the tenths text of
// the length messages. Expected bytes are written out by hand from the
// canonical RIFF/WAVE PCM layout, never from the module. The module is
// imported inside each test, so a missing one fails each test on its own.

import { test } from "node:test";
import assert from "node:assert/strict";

const audio = () => import("../bin/audio.mjs");

// "RIFF", 36 + 8, "WAVE", "fmt ", 16, PCM 1, then the per-channel fields,
// "data", 8.
const HEADER = (channels, byteRate, blockAlign) => [
  0x52, 0x49, 0x46, 0x46, 0x2c, 0x00, 0x00, 0x00, 0x57, 0x41, 0x56, 0x45,
  0x66, 0x6d, 0x74, 0x20, 0x10, 0x00, 0x00, 0x00, 0x01, 0x00, ...channels,
  0x80, 0xbb, 0x00, 0x00, ...byteRate, ...blockAlign, 0x10, 0x00,
  0x64, 0x61, 0x74, 0x61, 0x08, 0x00, 0x00, 0x00,
];

test("wavHeader(8, 1): 48 kHz, 16-bit, mono, 8 data bytes", async () => {
  const { wavHeader } = await audio();
  // Byte rate 96,000 = 0x017700; block align 2.
  const expected = HEADER([0x01, 0x00], [0x00, 0x77, 0x01, 0x00], [0x02, 0x00]);
  assert.equal(expected.length, 44);
  assert.deepEqual([...wavHeader(8, 1)], expected);
});

test("wavHeader(8, 2): 48 kHz, 16-bit, stereo, 8 data bytes", async () => {
  const { wavHeader } = await audio();
  // Byte rate 192,000 = 0x02ee00; block align 4.
  const expected = HEADER([0x02, 0x00], [0x00, 0xee, 0x02, 0x00], [0x04, 0x00]);
  assert.equal(expected.length, 44);
  assert.deepEqual([...wavHeader(8, 2)], expected);
});

test("tenthsText is <s>.<d>: 0, 9, 10 and 1801 tenths", async () => {
  const { tenthsText } = await audio();
  assert.deepEqual([0, 9, 10, 1801].map(tenthsText), ["0.0", "0.9", "1.0", "180.1"]);
});
