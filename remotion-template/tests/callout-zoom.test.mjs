// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The image-callouts geometry of task 4.4, src/kit/scenes/callout-zoom.ts,
// imported through Node's type stripping. Expected values are computed here
// from the fixture's definitions: the picture box is the centred contain fit
// of the image into IMAGE_BOX.panel inset by INSET, the focus is the centre
// of the callout points' bounding box, the end scale is the largest s ≤
// S_MAX (and at least 1) keeping every scaled point inside that inset, and
// the scale grows linearly from 1 at frame 0 to the end scale on the last.

import { test } from "node:test";
import assert from "node:assert/strict";
import { IMAGE_BOX } from "../src/kit/scenes/boxes.ts";
import { INSET, MARKER_RADIUS, S_MAX, calloutZoom, pictureBox, project, scaleAt } from "../src/kit/scenes/callout-zoom.ts";

const PANEL = IMAGE_BOX.panel;
// The inset panel, from IMAGE_BOX.panel and INSET.
const INNER = { left: PANEL.left + INSET, top: PANEL.top + INSET, right: PANEL.left + PANEL.width - INSET, bottom: PANEL.top + PANEL.height - INSET };
// gallery/public/images/sample.png.
const SAMPLE = { width: 1280, height: 960 };
const CORNERS = [{ x: 0, y: 0 }, { x: 1, y: 0 }, { x: 1, y: 1 }];
const EPSILON = 1e-9;

const near = (actual, expected, label) => assert.ok(Math.abs(actual - expected) <= EPSILON, `${label}: ${actual} is not ${expected}`);
const pointAt = (box, { x, y }) => ({ x: box.left + x * box.width, y: box.top + y * box.height });

function assertInside(point, label) {
  assert.ok(point.x >= INNER.left - EPSILON && point.x <= INNER.right + EPSILON, `${label}: x ${point.x} outside [${INNER.left}, ${INNER.right}]`);
  assert.ok(point.y >= INNER.top - EPSILON && point.y <= INNER.bottom + EPSILON, `${label}: y ${point.y} outside [${INNER.top}, ${INNER.bottom}]`);
}

test("S_MAX lies in 1.3–2, and the inset is at least the marker's radius", () => {
  assert.ok(S_MAX >= 1.3 && S_MAX <= 2, `S_MAX ${S_MAX}`);
  assert.ok(Number.isFinite(MARKER_RADIUS) && MARKER_RADIUS > 0, `MARKER_RADIUS ${MARKER_RADIUS}`);
  assert.ok(INSET >= MARKER_RADIUS, `INSET ${INSET} < MARKER_RADIUS ${MARKER_RADIUS}`);
  assert.ok(INNER.right > INNER.left && INNER.bottom > INNER.top);
});

test("the picture box is the centred contain fit of the image inside the inset panel", () => {
  const width = INNER.right - INNER.left;
  const height = INNER.bottom - INNER.top;
  for (const size of [SAMPLE, { width: 960, height: 1280 }, { width: 500, height: 500 }, { width: 3000, height: 1000 }, { width: 7, height: 9 }]) {
    const scale = Math.min(width / size.width, height / size.height);
    const w = size.width * scale;
    const h = size.height * scale;
    const box = pictureBox(size);
    const label = `${size.width}×${size.height}`;
    near(box.width, w, `${label} width`);
    near(box.height, h, `${label} height`);
    near(box.left, INNER.left + (width - w) / 2, `${label} left`);
    near(box.top, INNER.top + (height - h) / 2, `${label} top`);
    assert.ok(Math.abs(box.width - width) <= EPSILON || Math.abs(box.height - height) <= EPSILON, `${label} touches the inset on one axis`);
  }
  assert.deepEqual(pictureBox(SAMPLE), { left: 1224, top: 278, width: 560, height: 420 });
});

test("an image without a positive natural size is refused", () => {
  for (const size of [{ width: 0, height: 960 }, { width: 1280, height: 0 }, { width: Number.NaN, height: 960 }]) {
    assert.throws(() => pictureBox(size), RangeError, JSON.stringify(size));
  }
});

test("each callout point sits at R's corner plus (x, y) of R, and the focus is the centre of their bounding box", () => {
  const callouts = [{ x: 0.2, y: 0.25 }, { x: 0.8, y: 0.3 }, { x: 0.3, y: 0.9 }];
  const zoom = calloutZoom(SAMPLE, callouts);
  const box = pictureBox(SAMPLE);
  assert.deepEqual(zoom.picture, box);
  const points = callouts.map((c) => pointAt(box, c));
  assert.equal(zoom.points.length, 3);
  zoom.points.forEach((p, i) => {
    near(p.x, points[i].x, `P${i + 1}.x`);
    near(p.y, points[i].y, `P${i + 1}.y`);
  });
  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  near(zoom.focus.x, (Math.min(...xs) + Math.max(...xs)) / 2, "F.x");
  near(zoom.focus.y, (Math.min(...ys) + Math.max(...ys)) / 2, "F.y");
});

test("s(0) = 1 and s(last) = S, linear between, at several scene lengths", () => {
  const zoom = calloutZoom(SAMPLE, [{ x: 0.2, y: 0.25 }, { x: 0.8, y: 0.3 }, { x: 0.3, y: 0.9 }]);
  for (const duration of [60, 61, 150, 900]) {
    assert.equal(scaleAt(zoom.end, 0, duration), 1, `${duration}: s(0)`);
    near(scaleAt(zoom.end, duration - 1, duration), zoom.end, `${duration}: s(last)`);
    const middle = Math.floor((duration - 1) / 2);
    near(scaleAt(zoom.end, middle, duration), 1 + ((zoom.end - 1) * middle) / (duration - 1), `${duration}: s(${middle})`);
  }
});

test("the three corners (0, 0), (1, 0) and (1, 1) give S = 1", () => {
  assert.equal(calloutZoom(SAMPLE, CORNERS).end, 1);
  for (const size of [{ width: 960, height: 1280 }, { width: 640, height: 640 }, { width: 1999, height: 1001 }]) {
    assert.equal(calloutZoom(size, CORNERS).end, 1, `${size.width}×${size.height}`);
  }
});

test("a single callout gives F = P_1 and S = S_MAX", () => {
  for (const c of [{ x: 0, y: 0 }, { x: 1, y: 1 }, { x: 0.5, y: 0.1 }, { x: 0.37, y: 0.82 }]) {
    const zoom = calloutZoom(SAMPLE, [c]);
    assert.deepEqual(zoom.focus, zoom.points[0], JSON.stringify(c));
    assert.equal(zoom.end, S_MAX, JSON.stringify(c));
  }
});

test("interior points give 1 < S ≤ S_MAX, the largest scale that keeps every point inside the inset", () => {
  const sets = [
    [{ x: 0.2, y: 0.25 }, { x: 0.8, y: 0.3 }, { x: 0.3, y: 0.9 }],
    [{ x: 0.05, y: 0.5 }, { x: 0.95, y: 0.5 }],
    [{ x: 0.1, y: 0.1 }, { x: 0.9, y: 0.9 }],
    [{ x: 0.4, y: 0.2 }, { x: 0.6, y: 0.8 }],
  ];
  for (const callouts of sets) {
    const label = JSON.stringify(callouts);
    const { end, focus, points } = calloutZoom(SAMPLE, callouts);
    assert.ok(end > 1 && end <= S_MAX, `${label}: S ${end}`);
    for (const p of points) assertInside(project(focus, p, end), `${label} at S`);
    if (end < S_MAX) {
      const beyond = end + 1e-6;
      const out = points.map((p) => project(focus, p, beyond)).some((q) => q.x < INNER.left || q.x > INNER.right || q.y < INNER.top || q.y > INNER.bottom);
      assert.ok(out, `${label}: S ${end} is not the largest scale`);
    }
  }
});

test("at every frame, every marker centre F + s(f)·(P_i − F) stays inside the panel inset by m", () => {
  const sets = [CORNERS, [{ x: 0, y: 0 }], [{ x: 1, y: 1 }], [{ x: 0.2, y: 0.25 }, { x: 0.8, y: 0.3 }, { x: 0.3, y: 0.9 }], [{ x: 0, y: 1 }, { x: 0.5, y: 0 }]];
  for (const size of [SAMPLE, { width: 960, height: 1280 }]) {
    for (const callouts of sets) {
      const { end, focus, points } = calloutZoom(size, callouts);
      for (const duration of [60, 150]) {
        for (let frame = 0; frame < duration; frame++) {
          const s = scaleAt(end, frame, duration);
          for (const [i, p] of points.entries()) assertInside(project(focus, p, s), `${JSON.stringify(callouts)} ${size.width}×${size.height} frame ${frame} marker ${i + 1}`);
        }
      }
    }
  }
});
