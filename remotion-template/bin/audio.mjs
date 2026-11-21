// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The kit's shared audio helpers: the WAV header that public/narration.wav
// (bin/timeline.mjs) and public/music.wav (bin/music.mjs) are written with,
// and the tenths-of-a-second text of their length messages. Imports only
// the kit's Remotion-free modules.

import { SAMPLE_RATE } from "./scene-timing.mjs";

const BITS = 16;

// A 44-byte PCM WAV header: 48 kHz, 16-bit, `channels` channels, `bytes`
// of samples.
export function wavHeader(bytes, channels) {
  const blockAlign = channels * (BITS / 8);
  const header = Buffer.alloc(44);
  header.write("RIFF", 0, "latin1");
  header.writeUInt32LE(36 + bytes, 4);
  header.write("WAVEfmt ", 8, "latin1");
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20);
  header.writeUInt16LE(channels, 22);
  header.writeUInt32LE(SAMPLE_RATE, 24);
  header.writeUInt32LE(SAMPLE_RATE * blockAlign, 28);
  header.writeUInt16LE(blockAlign, 32);
  header.writeUInt16LE(BITS, 34);
  header.write("data", 36, "latin1");
  header.writeUInt32LE(bytes, 40);
  return header;
}

// Tenths of a second as `<s>.<d>`.
export const tenthsText = (tenths) => `${Math.floor(tenths / 10)}.${tenths % 10}`;
