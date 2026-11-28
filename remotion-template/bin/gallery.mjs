// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The gallery generator (task 4.1): `node bin/gallery.mjs --out <dir>
// [--preset <name>]`, run from a project root. One run reviews one preset
// of src/kit/presets.ts, the explainer by default (issue #962). It renders
// every sample of that preset's catalog and the theme sample in each of the
// preset's theme candidates (the explainer's: only the user's pick, task
// 4.6), each as a clip and its three probe frames, probes every item with
// bin/probe.mjs, and writes gallery.json and a static index.html, both
// naming the preset, whose export button gives the user's decisions. A
// preset whose catalog is empty is refused before any work.
//
// An existing --out is refused before any work. Everything is written into
// a temporary sibling of --out, renamed to --out only after every item
// rendered and was probed, so a failure leaves neither. gallery() bundles
// src/kit/gallery/entry.tsx once and opens one browser for every clip and
// still; probe() then bundles and opens its own. Both bundles go under
// os.tmpdir(), and both browsers close whatever happens. Exit codes: 0
// success (stdout: the --out path), 1 a refusal or failure, 2 a usage error
// or an unknown preset.

import { bundle } from "@remotion/bundler";
import { openBrowser, renderMedia, renderStill, selectComposition } from "@remotion/renderer";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, rename, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { DEFAULT_PRESET, PRESETS } from "../src/kit/presets.ts";
import { renderPage } from "./gallery-page.mjs";
import { lstatOrNull } from "./kit-io.mjs";
import { formatViolation, probe, probeFrames, violations } from "./probe.mjs";

const ROOT = fileURLToPath(new URL("..", import.meta.url));
const ENTRY = join(ROOT, "src", "kit", "gallery", "entry.tsx");
// TypeScript modules, imported through Node's type stripping only once the
// arguments and --out passed. src/kit/presets.ts, a leaf with no imports,
// is imported statically: the argument check needs its table. The
// template's package.json declares `"type": "module"`, so neither import
// makes Node print a MODULE_TYPELESS_PACKAGE_JSON warning.
const KIT_MODULES = {
  catalog: join(ROOT, "src", "kit", "gallery", "catalog.ts"),
  rules: join(ROOT, "src", "kit", "gallery", "catalog-rules.ts"),
  theme: join(ROOT, "src", "kit", "theme.ts"),
};
const PREFIX = "kit gallery: ";
const USAGE = `${PREFIX}usage: node bin/gallery.mjs --out <dir> [--preset <name>]`;
const OPTIONS = ["--out", "--preset"];
const COMPOSITION = "KitGallerySample";
const PROBE_SCENE = "gallery-sample";
// A refusal thrown inside the bundle (an id the catalog lacks, components
// that differ from it) reaches Node wrapped in Remotion's message.
const OWN_REFUSAL = /kit gallery: [^\n]*/;

const sha256 = (data) => createHash("sha256").update(data).digest("hex");

function fail(message) {
  throw new Error(`${PREFIX}${message}`);
}

const messageOf = (error) => (error instanceof Error ? error.message : String(error));
const asGalleryError = (error) =>
  messageOf(error).startsWith(PREFIX) ? error : new Error(`${PREFIX}${messageOf(error)}`, { cause: error });

// Keeps Remotion's message and names the item; the bundle's own refusals
// pass through as they were thrown.
function renderFailure(error, id) {
  const own = OWN_REFUSAL.exec(messageOf(error));
  return new Error(own ? own[0] : `${PREFIX}${id}: ${messageOf(error)}`, { cause: error });
}

// The theme sample first, then each candidate's samples in catalog order;
// the whole list once per theme, in the preset's theme candidate order.
function itemsOf({ THEME_ID, THEME_SAMPLE }, themeNames, candidates) {
  const samples = [
    { candidate: THEME_ID, sample: THEME_SAMPLE.id, durationInFrames: THEME_SAMPLE.durationInFrames, entranceFrames: THEME_SAMPLE.entranceFrames },
    ...candidates.flatMap((c) =>
      c.samples.map((s) => ({ candidate: c.id, sample: s.id, durationInFrames: s.timing.durationInFrames, entranceFrames: c.entranceFrames })),
    ),
  ];
  return themeNames.flatMap((theme) =>
    samples.map((s) => {
      const id = `${s.candidate}--${s.sample}--${theme}`;
      const frames = probeFrames({ durationInFrames: s.durationInFrames, entranceFrames: s.entranceFrames, exitFrame: s.durationInFrames });
      return {
        ...s,
        theme,
        id,
        probeFrames: frames,
        clip: `clips/${id}.mp4`,
        frames: Object.fromEntries(frames.map(({ role }) => [role, `frames/${id}--${role}.png`])),
      };
    }),
  );
}

async function renderItems(serveUrl, items, out) {
  const browser = await openBrowser("chrome");
  try {
    for (const item of items) {
      const inputProps = { candidate: item.candidate, sample: item.sample, theme: item.theme };
      try {
        const composition = await selectComposition({ serveUrl, id: COMPOSITION, inputProps, puppeteerInstance: browser, logLevel: "error" });
        await renderMedia({
          composition,
          serveUrl,
          codec: "h264",
          outputLocation: join(out, item.clip),
          inputProps,
          puppeteerInstance: browser,
          // One page: a CPU-limited container never opens more pages than it can hold.
          concurrency: 1,
          logLevel: "error",
        });
        for (const { role, frame } of item.probeFrames) {
          await renderStill({ composition, serveUrl, output: join(out, item.frames[role]), frame, inputProps, puppeteerInstance: browser, logLevel: "error" });
        }
      } catch (error) {
        throw renderFailure(error, item.id);
      }
    }
  } finally {
    await browser.close({ silent: true });
  }
}

// One probe over every item; each item's violations, judged in its own theme.
async function probeItems(themeNames, themes, items, publicDir) {
  const targets = items.map((item) => ({
    id: item.id,
    scene: PROBE_SCENE,
    props: { candidate: item.candidate, sample: item.sample, theme: item.theme },
    durationInFrames: item.durationInFrames,
    entranceFrames: item.entranceFrames,
    exitFrame: item.durationInFrames,
  }));
  const result = await probe({ entryPoint: ENTRY, targets, publicDir });
  const byTheme = Object.fromEntries(themeNames.map((theme) => [theme, violations(result, themes[theme])]));
  return new Map(items.map((item) => [item.id, byTheme[item.theme].filter((v) => v.target === item.id).map(formatViolation)]));
}

async function build(kit, preset, candidates, out, work) {
  const themeNames = PRESETS[preset].themeCandidates;
  const items = itemsOf(kit.rules, themeNames, candidates);
  // public/ when the project has one, else the tracked gallery/public, so the
  // catalog's samples can name an image.
  const projectPublic = join(ROOT, "public");
  const publicDir = (await lstatOrNull(projectPublic)) === null ? join(ROOT, "gallery", "public") : projectPublic;
  await mkdir(join(out, "clips"));
  await mkdir(join(out, "frames"));
  const serveUrl = await bundle({
    entryPoint: ENTRY,
    outDir: join(work, "bundle"),
    enableCaching: false,
    publicDir,
    symlinkPublicDir: true,
  });
  await renderItems(serveUrl, items, out);
  const found = await probeItems(themeNames, kit.theme.themes, items, publicDir);
  const data = {
    version: 1,
    preset,
    node: process.version,
    themes: themeNames,
    candidates: candidates.map(({ id, kind, source }) => ({ id, kind, source })),
    items: items.map(({ id, candidate, sample, theme, clip, frames }) => ({ id, candidate, sample, theme, clip, frames, violations: found.get(id) })),
  };
  const json = `${JSON.stringify(data, null, 2)}\n`;
  await writeFile(join(out, "gallery.json"), json);
  await writeFile(join(out, "index.html"), renderPage(data, sha256(json)));
  return items.length;
}

// preset: a name of the preset table.
export async function gallery({ out, preset = DEFAULT_PRESET } = {}) {
  if (typeof out !== "string" || out === "") fail("out must be a non-empty path");
  const target = resolve(out);
  if ((await lstatOrNull(target)) !== null) fail(`${target}: already exists`);
  let sibling = null;
  let work = null;
  try {
    const kit = Object.fromEntries(
      await Promise.all(Object.entries(KIT_MODULES).map(async ([name, path]) => [name, await import(pathToFileURL(path).href)])),
    );
    // A fixture catalog copied over catalog.ts may export only the
    // explainer's `candidates`.
    const candidates = preset === DEFAULT_PRESET ? kit.catalog.candidates : (kit.catalog.catalogs?.[preset] ?? []);
    kit.catalog.validateCatalog(candidates);
    if (candidates.length === 0) fail(`preset ${preset} has no candidates`);
    sibling = await mkdtemp(join(dirname(target), `.${basename(target)}-`));
    work = await mkdtemp(join(tmpdir(), "kit-gallery-"));
    const items = await build(kit, preset, candidates, sibling, work);
    if ((await lstatOrNull(target)) !== null) fail(`${target}: already exists`);
    await rename(sibling, target);
    sibling = null;
    return { out: target, items };
  } catch (error) {
    throw asGalleryError(error);
  } finally {
    if (sibling !== null) await rm(sibling, { recursive: true, force: true });
    if (work !== null) await rm(work, { recursive: true, force: true });
  }
}

// `--out <dir>` and an optional `--preset <name>`, in either order, each
// once with a non-empty value; null for any other argument list.
function parseArgs(argv) {
  const options = {};
  for (let i = 0; i < argv.length; i += 2) {
    const [name, value] = [argv[i], argv[i + 1]];
    if (!OPTIONS.includes(name) || Object.hasOwn(options, name) || value === undefined || value === "") return null;
    options[name] = value;
  }
  return Object.hasOwn(options, "--out") ? { out: options["--out"], preset: options["--preset"] ?? DEFAULT_PRESET } : null;
}

export async function main(argv, io) {
  const args = parseArgs(argv);
  if (args === null) {
    io.stderr.write(`${USAGE}\n`);
    return 2;
  }
  if (!Object.hasOwn(PRESETS, args.preset)) {
    io.stderr.write(`${PREFIX}unknown preset ${args.preset}\n`);
    return 2;
  }
  try {
    const { out } = await gallery(args);
    io.stdout.write(`${out}\n`);
    return 0;
  } catch (error) {
    io.stderr.write(`${asGalleryError(error).message}\n`);
    return 1;
  }
}

if (import.meta.main) {
  process.exitCode = await main(process.argv.slice(2), process);
}
