// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The gallery generator in a real headless shell (task 4.1): `node
// bin/gallery.mjs --out <dir>` over the fixture catalogs in
// fixtures/gallery/, copied over a scratch project's src/kit/gallery/. Runs
// only inside the workspace image, from a project root whose node_modules
// link reaches /opt/remotion-template/node_modules, one test file after
// another:
//   node --test --test-concurrency=1 "tests/render/*.test.mjs"
// CI's tests/*.test.mjs glob does not match this file. Expected counts,
// frames, paths and colours come from the fixture: 3 clips and 9 key
// frames over the theme sample and two 60-frame placeholder samples in the
// kit's one theme, light (task 4.6), probe frames 10, 30 and 59 (entrance
// 10), the placeholder's DOM (its question line is the KitFrame's second
// child's first line), and themes.light.brand.accent against Onda's #D96B82.
//
// Then the other two KitFrame roots: 3.8's explainer fixture with its
// custom scene replaced by Accent.tsx, stilled inside that scene (light), and
// the real kit root's KitProbe over the same custom scene with
// themes.light in its props. With one theme, that check can no longer tell
// the target's own theme from the default KIT_THEME.
//
// Each CLI run gets its own empty parent directory, which is polled while
// the run lasts, so a run that wrote straight into --out is caught even when
// its cleanup removes it.

import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync, spawn, spawnSync } from "node:child_process";
import { copyFileSync, cpSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { bundle } from "@remotion/bundler";
import { openBrowser, renderStill, selectComposition } from "@remotion/renderer";
import { writeCustomIndex } from "../../bin/custom-index.mjs";
import { explainerScenes } from "../../bin/explainer-scenes.mjs";
import { themes } from "../../src/kit/theme.ts";
import { fixtureScript, fixtureTimeline } from "./fixtures/explainer/fixture.mjs";

const TEMPLATE = fileURLToPath(new URL("../..", import.meta.url));
const FIXTURES = join(TEMPLATE, "tests", "render", "fixtures", "gallery");
const USAGE = "kit gallery: usage: node bin/gallery.mjs --out <dir>\n";
const ROLES = ["entrance-end", "midpoint", "pre-exit"];
const THEMES = ["light"];
const PAIRS = [["theme", "sample"], ["placeholder", "max"], ["placeholder", "overflow"]];
const ITEM_IDS = THEMES.flatMap((theme) => PAIRS.map(([c, s]) => `${c}--${s}--${theme}`));
const FRAMES = { theme: [15, 45, 89], placeholder: [10, 30, 59] };
const LENGTH = { theme: 90, placeholder: 60 };
// Probe root > KitFrame (child 1) > the placeholder's text box (child 2,
// after the accent box) > its one wrapLines line.
const QUESTION_PATH = "div:nth-child(1)>div:nth-child(2)>div:nth-child(1)";
const PIXEL = { x: 196, y: 104 };
const ONDA_ACCENT = "#D96B82";
const CHANNEL_TOLERANCE = 2;
const POLL_MS = 50;
// 3.8's fixture script and timeline, its custom scene renamed Accent: the
// custom scene plays 194–271 of 481 frames.
const ACCENT_SCRIPT = fixtureScript((raw) => {
  raw.chapters[1].scenes[0].component = "Accent";
});
const ACCENT_TIMELINE = fixtureTimeline(ACCENT_SCRIPT);
const EXPLAINER_FRAMES = 481;
const CUSTOM_FRAME = 200;
const PROBE_FRAME = 10;

const timings = {};
const outcomes = {};
let root;
let browser = null;

async function step(name, run) {
  const started = Date.now();
  try {
    return { value: await run() };
  } catch (error) {
    return { error };
  } finally {
    timings[name] = Date.now() - started;
  }
}

function copyProject() {
  root = realpathSync(mkdtempSync(join(tmpdir(), "gallery-render-")));
  for (const part of ["src", "bin", "gallery", join("tests", "render", "fixtures")]) {
    cpSync(join(TEMPLATE, part), join(root, part), { recursive: true });
  }
  for (const name of ["package.json", "tsconfig.json", "remotion.config.ts"]) copyFileSync(join(TEMPLATE, name), join(root, name));
  symlinkSync(realpathSync(join(TEMPLATE, "node_modules")), join(root, "node_modules"));
}

// The `ok` catalog with the named fixture's components.
function useFixture(name) {
  copyFileSync(join(FIXTURES, "ok", "catalog.ts"), join(root, "src", "kit", "gallery", "catalog.ts"));
  copyFileSync(join(FIXTURES, name, "components.tsx"), join(root, "src", "kit", "gallery", "components.tsx"));
}

// Runs the CLI with --out <parent>/gallery from the project root, polling
// <parent> until the process exits.
function runGallery(name, prepare = () => {}) {
  const parent = join(root, "runs", name);
  mkdirSync(parent, { recursive: true });
  const out = join(parent, "gallery");
  prepare(out);
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const seen = new Set();
    const poll = () => {
      for (const entry of readdirSync(parent)) seen.add(entry);
    };
    const child = spawn(process.execPath, ["bin/gallery.mjs", "--out", out], { cwd: root, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => (stdout += chunk));
    child.stderr.on("data", (chunk) => (stderr += chunk));
    const timer = setInterval(poll, POLL_MS);
    child.on("error", (error) => {
      clearInterval(timer);
      reject(error);
    });
    child.on("close", (code) => {
      clearInterval(timer);
      timings[`gallery ${name}`] = Date.now() - started;
      resolve({ code, stdout, stderr, out, parent, seen: [...seen].sort(), after: readdirSync(parent).sort() });
    });
  });
}

const ffprobe = (path, entries) =>
  JSON.parse(execFileSync("ffprobe", ["-v", "error", "-count_frames", "-show_entries", entries, "-of", "json", path], { encoding: "utf8" }));

function pixel(path) {
  const bytes = execFileSync("ffmpeg", [
    "-nostdin", "-v", "error", "-i", path,
    "-vf", `crop=1:1:${PIXEL.x}:${PIXEL.y}`,
    "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
  ]);
  assert.equal(bytes.length, 3, path);
  return [...bytes];
}

const rgb = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));

function assertColour(actual, hex, label) {
  const expected = rgb(hex);
  for (let i = 0; i < 3; i++) {
    assert.ok(Math.abs(actual[i] - expected[i]) <= CHANNEL_TOLERANCE, `${label}: ${JSON.stringify(actual)} is not ${hex} ${JSON.stringify(expected)}`);
  }
}

// The CLI's own lines. Importing catalog.ts makes Node print its
// MODULE_TYPELESS_PACKAGE_JSON warning first (the template's package.json
// declares no module type); that block is Node's, not the CLI's.
const galleryLines = (stderr) => stderr.split("\n").filter((line) => line.startsWith("kit gallery: "));

const escapeHtml = (text) =>
  text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");

before(async () => {
  copyProject();
  outcomes.usage = spawnSync(process.execPath, ["bin/gallery.mjs"], { cwd: root, encoding: "utf8" });
  outcomes.existing = await runGallery("existing", (out) => {
    mkdirSync(out);
    writeFileSync(join(out, "keep.txt"), "keep");
  });
  useFixture("mismatch");
  outcomes.mismatch = await runGallery("mismatch");
  useFixture("throws");
  outcomes.throws = await runGallery("throws");
  useFixture("ok");
  outcomes.ok = await runGallery("ok");

  browser = await openBrowser("chrome");
  const serveUrl = await step("bundle unframed", () =>
    bundle({ entryPoint: join(root, "tests", "render", "fixtures", "gallery", "unframed-entry.tsx"), outDir: join(root, "bundles", "unframed"), enableCaching: false }),
  );
  if (serveUrl.value) {
    const output = join(root, "unframed.png");
    outcomes.unframed = await step("still unframed", async () => {
      const composition = await selectComposition({ serveUrl: serveUrl.value, id: "Unframed", inputProps: {}, puppeteerInstance: browser });
      await renderStill({ composition, serveUrl: serveUrl.value, output, frame: 10, inputProps: {}, puppeteerInstance: browser });
      return output;
    });
  } else {
    outcomes.unframed = serveUrl;
  }
  await stillKitRoots();
});

// The two other KitFrame roots: the explainer body (through 3.8's fixture
// entry, whose real CustomScene loads Accent) and the real kit root's
// framed KitProbe, each stilled inside the Accent scene.
async function stillKitRoots() {
  copyFileSync(join(FIXTURES, "Accent.tsx"), join(root, "src", "custom", "Accent.tsx"));
  await writeCustomIndex(root, ACCENT_SCRIPT);
  const publicDir = join(root, "public");
  mkdirSync(publicDir);
  execFileSync("ffmpeg", [
    "-nostdin", "-v", "error",
    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
    "-af", `atrim=end_sample=${EXPLAINER_FRAMES * 1600}`,
    "-ac", "1", "-c:a", "pcm_s16le",
    join(publicDir, "narration.wav"),
  ]);
  const still = (name, entryPoint, id, inputProps, frame) =>
    step(`still ${name}`, async () => {
      const serveUrl = await bundle({ entryPoint, outDir: join(root, "bundles", name), enableCaching: false, publicDir, symlinkPublicDir: true });
      const composition = await selectComposition({ serveUrl, id, inputProps, puppeteerInstance: browser });
      const output = join(root, `${name}.png`);
      await renderStill({ composition, serveUrl, output, frame, inputProps, puppeteerInstance: browser });
      return output;
    });
  outcomes.explainer = await still(
    "explainer",
    join(root, "tests", "render", "fixtures", "explainer", "entry.tsx"),
    "Explainer",
    { script: ACCENT_SCRIPT, timeline: ACCENT_TIMELINE },
    CUSTOM_FRAME,
  );
  const custom = explainerScenes(ACCENT_SCRIPT, ACCENT_TIMELINE)[2];
  const targets = [{ scene: "custom", props: { ...custom.props, theme: themes.light }, durationInFrames: custom.durationInFrames }];
  outcomes.kitProbe = await still("kit-probe", join(root, "src", "kit", "index.ts"), "KitProbe", { targets }, PROBE_FRAME);
}

after(async () => {
  try {
    if (browser !== null) await browser.close({ silent: true });
  } finally {
    if (root !== undefined) rmSync(root, { recursive: true, force: true });
  }
});

function okRun() {
  const run = outcomes.ok;
  assert.equal(run.code, 0, run.stderr);
  return run;
}

const galleryJson = () => JSON.parse(readFileSync(join(okRun().out, "gallery.json"), "utf8"));

test("timings", (t) => {
  for (const [name, ms] of Object.entries(timings)) t.diagnostic(`${name}: ${ms} ms`);
});

test("no argument exits 2 with the usage line", () => {
  assert.equal(outcomes.usage.status, 2);
  assert.equal(outcomes.usage.stderr, USAGE);
  assert.equal(outcomes.usage.stdout, "");
});

test("an existing --out is refused and left untouched", () => {
  const run = outcomes.existing;
  assert.equal(run.code, 1);
  assert.equal(run.stderr, `kit gallery: ${run.out}: already exists\n`);
  assert.deepEqual(run.after, ["gallery"]);
  assert.deepEqual(run.seen, ["gallery"]);
  assert.deepEqual(readdirSync(run.out), ["keep.txt"]);
  assert.equal(readFileSync(join(run.out, "keep.txt"), "utf8"), "keep");
});

test("components without a catalog entry exit 1 naming the id", () => {
  const run = outcomes.mismatch;
  assert.equal(run.code, 1, run.stderr);
  assert.deepEqual(galleryLines(run.stderr), ['kit gallery: components has "extra", the catalog does not']);
});

test("a render that throws exits 1 with the item id and the component's message", () => {
  const run = outcomes.throws;
  assert.equal(run.code, 1, run.stderr);
  const [line, ...rest] = galleryLines(run.stderr);
  assert.deepEqual(rest, [], run.stderr);
  // The CLI's own line, not Remotion's forwarded console output, carries both.
  assert.ok(line.startsWith("kit gallery: placeholder--max--light: "), run.stderr);
  assert.ok(line.includes("placeholder throws at frame 30"), run.stderr);
});

test("each rejected run worked in one temporary sibling and left neither it nor --out", () => {
  for (const name of ["mismatch", "throws"]) {
    const run = outcomes[name];
    assert.deepEqual(run.after, [], name);
    assert.ok(!run.seen.includes("gallery"), `${name}: --out existed during the run: ${JSON.stringify(run.seen)}`);
    assert.equal(run.seen.length, 1, `${name}: ${JSON.stringify(run.seen)}`);
    assert.match(run.seen[0], /^\.gallery-/, name);
  }
});

test("the ok run writes index.html, gallery.json, 3 clips and 9 key frames through one sibling", () => {
  const run = okRun();
  assert.equal(run.stdout, `${run.out}\n`);
  assert.deepEqual(run.after, ["gallery"]);
  assert.equal(run.seen.filter((name) => name.startsWith(".gallery-")).length, 1, JSON.stringify(run.seen));
  assert.deepEqual(readdirSync(run.out).sort(), ["clips", "frames", "gallery.json", "index.html"]);
  assert.deepEqual(readdirSync(join(run.out, "clips")).sort(), ITEM_IDS.map((id) => `${id}.mp4`).sort());
  assert.deepEqual(
    readdirSync(join(run.out, "frames")).sort(),
    ITEM_IDS.flatMap((id) => ROLES.map((role) => `${id}--${role}.png`)).sort(),
  );
});

test("each clip is 1920×1080 H.264 at 30 fps with 90 or 60 counted frames, each key frame 1920×1080", () => {
  const { out } = okRun();
  for (const id of ITEM_IDS) {
    const [video, ...others] = ffprobe(join(out, "clips", `${id}.mp4`), "stream=codec_type,codec_name,width,height,r_frame_rate,nb_read_frames").streams.filter(
      (s) => s.codec_type === "video",
    );
    assert.deepEqual(others, [], id);
    assert.equal(video.codec_name, "h264", id);
    assert.equal(video.width, 1920, id);
    assert.equal(video.height, 1080, id);
    assert.equal(video.r_frame_rate, "30/1", id);
    assert.equal(Number(video.nb_read_frames), LENGTH[id.split("--")[0]], id);
    for (const role of ROLES) {
      const [still] = ffprobe(join(out, "frames", `${id}--${role}.png`), "stream=width,height").streams;
      assert.deepEqual([still.width, still.height], [1920, 1080], `${id} ${role}`);
    }
  }
});

test("gallery.json, version 1, lists the catalog and every item in order with relative paths", () => {
  const data = galleryJson();
  assert.equal(data.version, 1);
  assert.match(data.node, /^v\d+\.\d+\.\d+$/);
  assert.deepEqual(data.themes, THEMES);
  assert.deepEqual(data.candidates, [{ id: "placeholder", kind: "question", source: "tests/render/fixtures/gallery/ok (task 4.1 placeholder)" }]);
  assert.deepEqual(
    data.items.map(({ violations, ...rest }) => rest),
    THEMES.flatMap((theme) =>
      PAIRS.map(([candidate, sample]) => {
        const id = `${candidate}--${sample}--${theme}`;
        return { id, candidate, sample, theme, clip: `clips/${id}.mp4`, frames: Object.fromEntries(ROLES.map((role) => [role, `frames/${id}--${role}.png`])) };
      }),
    ),
  );
  const raw = readFileSync(join(okRun().out, "gallery.json"), "utf8");
  assert.equal(raw, `${JSON.stringify(data, null, 2)}\n`);
});

test("the theme sample and max probe clean; overflow is outside the safe area on the question at every role, shown on the page", () => {
  const data = galleryJson();
  const html = readFileSync(join(okRun().out, "index.html"), "utf8");
  for (const entry of data.items) {
    if (entry.sample !== "overflow") {
      assert.deepEqual(entry.violations, [], entry.id);
      continue;
    }
    assert.equal(entry.violations.length, 3, `${entry.id}: ${JSON.stringify(entry.violations)}`);
    entry.violations.forEach((line, k) => {
      const prefix = `${entry.id} ${ROLES[k]} frame ${FRAMES.placeholder[k]}: outside safe area: ${QUESTION_PATH} right `;
      assert.ok(line.startsWith(prefix), `${line} does not start with ${prefix}`);
      assert.match(line.slice(prefix.length), /^\d+(\.\d+)? px$/, line);
      assert.ok(html.includes(escapeHtml(line)), `the page lacks ${line}`);
    });
  }
  assert.equal(html.split("probe: clean").length - 1, 2);
});

test("KitFrame applies the brand at the root: the accent box is the theme's accent, Onda's own without the frame", () => {
  const { out } = okRun();
  for (const theme of THEMES) {
    assertColour(pixel(join(out, "frames", `placeholder--max--${theme}--entrance-end.png`)), themes[theme].brand.accent, theme);
  }
  if (outcomes.unframed.error) throw outcomes.unframed.error;
  assertColour(pixel(outcomes.unframed.value), ONDA_ACCENT, "Unframed");
});

test("the explainer's and the kit probe's KitFrame apply their theme's brand", () => {
  const explainer = outcomes.explainer;
  if (explainer.error) throw explainer.error;
  assertColour(pixel(explainer.value), themes.light.brand.accent, "Explainer (light)");
  const kitProbe = outcomes.kitProbe;
  if (kitProbe.error) throw kitProbe.error;
  assertColour(pixel(kitProbe.value), themes.light.brand.accent, "KitProbe (light)");
});
