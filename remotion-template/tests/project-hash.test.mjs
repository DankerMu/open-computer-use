// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The passing-check hash of task 5.2 (bin/project-hash.mjs), on a temporary
// project written from the pure check fixture (tests/fixtures/check/). The
// expected hash is worked out here from the fixture's rule: the lowercase hex
// SHA-256 of JSON.stringify of the [path, sha256 | null] pairs, sorted by
// path, over script.json, timeline.json, every regular file under src/ and
// the referenced public/ files. The module is imported inside each test, so
// a missing module fails every test on its own. Task 5.3 hardens the reads:
// every file is opened with O_NONBLOCK | O_NOFOLLOW and judged by fstat, and
// a file that vanishes after the listing is skipped (src/) or null
// (referenced), so the next hash differs instead of the read failing.

import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, readFileSync, renameSync, rmSync, symlinkSync, utimesSync, chmodSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { writeCustomIndex } from "../bin/custom-index.mjs";
import { blockStems } from "../bin/voice-stem.mjs";
import { checkFiles, checkScript, checkTimeline } from "./fixtures/check/fixture.mjs";

const BIN = fileURLToPath(new URL("../bin/", import.meta.url));
const hashing = () => import("../bin/project-hash.mjs");
const sha256 = (data) => createHash("sha256").update(data).digest("hex");

const plain = checkScript();
const pictured = checkScript((raw) => (raw.hook.image = "light.webp"));

// The fixture project on disk; with `image`, the hook shows light.webp and
// public/images/ holds it.
function project(t, { image = false } = {}) {
  const root = mkdtempSync(join(tmpdir(), "project-hash-test-"));
  // Modes a test takes away are restored before the removal.
  const locked = [];
  t.after(() => {
    for (const path of locked) chmodSync(path, 0o755);
    rmSync(root, { recursive: true, force: true });
  });
  const script = image ? pictured : plain;
  const timeline = checkTimeline(script);
  for (const [path, bytes] of checkFiles(script, timeline)) {
    mkdirSync(dirname(join(root, path)), { recursive: true });
    writeFileSync(join(root, path), bytes);
  }
  mkdirSync(join(root, "src", "kit"), { recursive: true });
  writeFileSync(join(root, "src", "kit", "theme.ts"), "export const KIT_THEME = 'light';\n");
  if (image) {
    mkdirSync(join(root, "public", "images"), { recursive: true });
    writeFileSync(join(root, "public", "images", "light.webp"), "webp bytes");
  }
  return { root, script, timeline, locked, stems: blockStems(script).map((entry) => entry.stem) };
}

// projectHash over the texts on disk now; texts.read replaces the file
// reader, texts.musicDir the music library.
async function hashNow({ root, script, timeline }, texts = {}) {
  const { projectHash } = await hashing();
  return projectHash(root, {
    script,
    timeline,
    scriptText: texts.scriptText ?? readFileSync(join(root, "script.json"), "utf8"),
    timelineText: texts.timelineText ?? readFileSync(join(root, "timeline.json"), "utf8"),
    ...(texts.read ? { read: texts.read } : {}),
    ...(texts.musicDir ? { musicDir: texts.musicDir } : {}),
  });
}

// The fixture's rule, computed by hand over the given paths, plus any
// extra [name, sha256 | null] pairs.
function expectedHash(root, paths, absent = [], extra = []) {
  const entries = paths.map((path) => [path, absent.includes(path) ? null : sha256(readFileSync(join(root, path)))]);
  entries.push(...extra);
  entries.sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  return sha256(JSON.stringify(entries));
}

const voicePaths = (stems) => stems.map((stem) => `public/voice/${stem}.wav`);

test("the hash is the SHA-256 of the sorted [path, sha256] pairs of the judged texts, src/ and the referenced files", async (t) => {
  const p = project(t, { image: true });
  const paths = [
    "script.json",
    "timeline.json",
    "src/custom/Orbit.tsx",
    "src/kit/theme.ts",
    "public/images/light.webp",
    "public/narration.wav",
    ...voicePaths(p.stems),
  ];
  assert.deepEqual(await hashNow(p), { hash: expectedHash(p.root, paths) });
});

test("referencedPublicFiles: the sorted, de-duplicated images, block WAVs and narration", async () => {
  const { referencedPublicFiles } = await hashing();
  const twice = checkScript((raw) => {
    raw.hook.image = "light.webp";
    raw.summary.image = "light.webp";
    raw.chapters[2].scenes[0].image = "a.png";
  });
  const stems = blockStems(twice).map((entry) => entry.stem);
  const expected = ["public/images/a.png", "public/images/light.webp", "public/narration.wav", ...voicePaths(stems)].sort();
  assert.deepEqual(referencedPublicFiles(twice, checkTimeline(twice)), expected);
  assert.deepEqual(referencedPublicFiles(plain, checkTimeline(plain)), ["public/narration.wav", ...voicePaths(blockStems(plain).map((e) => e.stem))].sort());
});

test("the same tree gives the same hash, and touch or chmod alone changes nothing", async (t) => {
  const p = project(t);
  const first = await hashNow(p);
  assert.match(first.hash, /^[0-9a-f]{64}$/);
  assert.deepEqual(await hashNow(p), first);
  const later = new Date(Date.now() + 3600000);
  for (const path of ["src/custom/Orbit.tsx", "public/narration.wav", `public/voice/${p.stems[0]}.wav`, "script.json"]) {
    utimesSync(join(p.root, path), later, later);
  }
  chmodSync(join(p.root, "src", "custom", "Orbit.tsx"), 0o600);
  assert.deepEqual(await hashNow(p), first);
});

test("each change to a hashed file or text changes the hash", async (t) => {
  const p = project(t, { image: true });
  const base = (await hashNow(p)).hash;
  const changes = [
    ["editing a src/ file", (root) => writeFileSync(join(root, "src", "custom", "Orbit.tsx"), "export default () => null;\n")],
    ["adding a src/ file", (root) => writeFileSync(join(root, "src", "kit", "extra.ts"), "")],
    ["renaming a src/ file with the same content", (root) => renameSync(join(root, "src", "kit", "theme.ts"), join(root, "src", "kit", "themes.ts"))],
    ["editing a referenced image", (root) => writeFileSync(join(root, "public", "images", "light.webp"), "other webp")],
    ["editing a referenced voice WAV", (root) => writeFileSync(join(root, "public", "voice", `${p.stems[0]}.wav`), "other wav")],
    ["editing narration.wav", (root) => writeFileSync(join(root, "public", "narration.wav"), "other narration")],
  ];
  for (const [name, change] of changes) {
    const q = project(t, { image: true });
    change(q.root);
    const changed = await hashNow(q);
    assert.ok(changed.hash !== undefined && changed.hash !== base, name);
  }
  const scriptText = `${readFileSync(join(p.root, "script.json"), "utf8")} `;
  assert.notEqual((await hashNow(p, { scriptText })).hash, base, "changing scriptText");
  const timelineText = `${readFileSync(join(p.root, "timeline.json"), "utf8")} `;
  assert.notEqual((await hashNow(p, { timelineText })).hash, base, "changing timelineText");
});

test("editing or adding a public/ file that is not referenced changes nothing", async (t) => {
  const p = project(t);
  mkdirSync(join(p.root, "public", "images"), { recursive: true });
  writeFileSync(join(p.root, "public", "images", "unused.png"), "png");
  const base = await hashNow(p);
  writeFileSync(join(p.root, "public", "images", "unused.png"), "other png");
  writeFileSync(join(p.root, "public", "notes.txt"), "notes");
  writeFileSync(join(p.root, "public", "voice", "s9-0-0-00000000.wav"), "stray wav");
  assert.deepEqual(await hashNow(p), base);
});

test("writeCustomIndex run again on the same script changes nothing", async (t) => {
  const p = project(t);
  await writeCustomIndex(p.root, p.script);
  const base = await hashNow(p);
  await writeCustomIndex(p.root, p.script);
  assert.deepEqual(await hashNow(p), base);
});

test("src missing, a symlink, not a directory, or holding a symlink or FIFO gives its reason", async (t) => {
  const missing = project(t);
  rmSync(join(missing.root, "src"), { recursive: true });
  assert.deepEqual(await hashNow(missing), { reasons: ["src: is missing"] });

  const linked = project(t);
  renameSync(join(linked.root, "src"), join(linked.root, "src-real"));
  symlinkSync(join(linked.root, "src-real"), join(linked.root, "src"));
  assert.deepEqual(await hashNow(linked), { reasons: ["src: is a symlink"] });

  const file = project(t);
  rmSync(join(file.root, "src"), { recursive: true });
  writeFileSync(join(file.root, "src"), "not a directory");
  assert.deepEqual(await hashNow(file), { reasons: ["src: is not a directory"] });

  const inner = project(t);
  symlinkSync(join(inner.root, "script.json"), join(inner.root, "src", "kit", "linked.json"));
  assert.deepEqual(await hashNow(inner), {
    reasons: ["src/kit/linked.json: is not a regular file or directory; the check hashes regular files only"],
  });

  const fifo = project(t);
  execFileSync("mkfifo", [join(fifo.root, "src", "custom", "pipe")]);
  assert.deepEqual(await hashNow(fifo), {
    reasons: ["src/custom/pipe: is not a regular file or directory; the check hashes regular files only"],
  });
});

test("an absent or non-regular referenced file is a null entry and no reason", async (t) => {
  const p = project(t, { image: true });
  rmSync(join(p.root, "public", "images", "light.webp"));
  const wav = `public/voice/${p.stems[1]}.wav`;
  rmSync(join(p.root, wav));
  symlinkSync(join(p.root, "public", "narration.wav"), join(p.root, wav));
  const paths = ["script.json", "timeline.json", "src/custom/Orbit.tsx", "src/kit/theme.ts", "public/images/light.webp", "public/narration.wav", ...voicePaths(p.stems)];
  assert.deepEqual(await hashNow(p), { hash: expectedHash(p.root, paths, ["public/images/light.webp", wav]) });
});

test("a system error from the walk propagates", { skip: process.getuid?.() === 0 && "root reads a mode-000 directory" }, async (t) => {
  const p = project(t);
  const locked = join(p.root, "src", "locked");
  mkdirSync(locked);
  p.locked.push(locked);
  chmodSync(locked, 0o000);
  await assert.rejects(hashNow(p), (error) => error.code === "EACCES");
});

// --- task 5.3: O_NONBLOCK | O_NOFOLLOW reads, vanished files ------------------------

test("the read helper: a regular file gives its bytes; a FIFO and a symlink are non-regular, without blocking; a missing path vanished", async (t) => {
  const { readHashed } = await hashing();
  const p = project(t);
  const file = join(p.root, "src", "custom", "Orbit.tsx");
  assert.deepEqual(await readHashed(file), { kind: "file", bytes: readFileSync(file) });
  const fifo = join(p.root, "pipe");
  execFileSync("mkfifo", [fifo]);
  // With no writer, a blocking open of the FIFO would never return.
  assert.deepEqual(await readHashed(fifo), { kind: "other" });
  const link = join(p.root, "link");
  symlinkSync(file, link);
  assert.deepEqual(await readHashed(link), { kind: "other" });
  assert.deepEqual(await readHashed(join(p.root, "src")), { kind: "other" });
  assert.deepEqual(await readHashed(join(p.root, "absent")), { kind: "vanished" });
  assert.deepEqual(await readHashed(join(p.root, "script.json", "below")), { kind: "vanished" });
});

test("a src/ file removed between the listing and the read is skipped, and the hash differs", async (t) => {
  const { readHashed } = await hashing();
  const p = project(t);
  const before = (await hashNow(p)).hash;
  const orbit = join(p.root, "src", "custom", "Orbit.tsx");
  const read = async (path) => {
    if (path === orbit) rmSync(orbit);
    return readHashed(path);
  };
  const raced = await hashNow(p, { read });
  const paths = ["script.json", "timeline.json", "src/kit/theme.ts", "public/narration.wav", ...voicePaths(p.stems)];
  assert.deepEqual(raced, { hash: expectedHash(p.root, paths) });
  assert.notEqual(raced.hash, before);
});

test("a referenced file removed between the listing and the read is a null entry", async (t) => {
  const { readHashed } = await hashing();
  const p = project(t);
  const wav = `public/voice/${p.stems[0]}.wav`;
  const read = async (path) => {
    if (path === join(p.root, wav)) rmSync(path, { force: true });
    return readHashed(path);
  };
  const before = (await hashNow(p)).hash;
  const paths = ["script.json", "timeline.json", "src/custom/Orbit.tsx", "src/kit/theme.ts", "public/narration.wav", ...voicePaths(p.stems)];
  const raced = await hashNow(p, { read });
  // The vanished WAV is [path, null]; the others hash as on disk.
  assert.deepEqual(raced, { hash: expectedHash(p.root, paths, [wav]) });
  assert.notEqual(raced.hash, before);
});

test("a src/ entry swapped for a FIFO after the listing is a reason, read without blocking", async (t) => {
  const { readHashed } = await hashing();
  const p = project(t);
  const orbit = join(p.root, "src", "custom", "Orbit.tsx");
  const read = async (path) => {
    if (path === orbit) {
      rmSync(orbit);
      execFileSync("mkfifo", [orbit]);
    }
    return readHashed(path);
  };
  assert.deepEqual(await hashNow(p, { read }), {
    reasons: ["src/custom/Orbit.tsx: is not a regular file or directory; the check hashes regular files only"],
  });
});

// The specifiers of a module's static imports and re-exports.
function staticImports(path) {
  const source = readFileSync(path, "utf8");
  return [...source.matchAll(/^\s*(?:import|export)\b[^;'"]*?\bfrom\s*["']([^"']+)["']/gm)].map((m) => m[1]);
}

test("project-hash.mjs imports only node: built-ins and Remotion-free kit modules", () => {
  assert.deepEqual(staticImports(join(BIN, "project-hash.mjs")), [
    "node:crypto",
    "node:fs/promises",
    "node:path",
    "./explainer-scenes.mjs",
    "./kit-io.mjs",
    "./music.mjs",
  ]);
  const bare = new Set();
  const seen = new Set();
  const visit = (path) => {
    if (seen.has(path)) return;
    seen.add(path);
    for (const specifier of staticImports(path)) {
      if (specifier.startsWith(".")) visit(join(dirname(path), specifier));
      else bare.add(specifier);
    }
  };
  visit(join(BIN, "project-hash.mjs"));
  assert.deepEqual([...bare].filter((specifier) => !specifier.startsWith("node:")), ["zod"]);
});

// --- task 6.5: the music pair ---------------------------------------------------------

// A library whose manifest lists fixture-bar (bar.wav) and fixture-other;
// bar.wav holds `bytes`.
function musicLibrary(t, bytes = "bar audio") {
  const dir = mkdtempSync(join(tmpdir(), "project-hash-music-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const tracks = [
    { id: "fixture-other", loop: false, file: "other.mp3", duration_seconds: 200 },
    { id: "fixture-bar", loop: true, file: "bar.wav", duration_seconds: 2.01 },
  ];
  writeFileSync(join(dir, "manifest.json"), JSON.stringify({ version: 1, tracks }));
  writeFileSync(join(dir, "bar.wav"), bytes);
  writeFileSync(join(dir, "other.mp3"), "other audio");
  return dir;
}

// The fixture project with script.json naming `music`.
function musicProject(t, music) {
  const p = project(t);
  const raw = JSON.parse(readFileSync(join(p.root, "script.json"), "utf8"));
  raw.music = music;
  writeFileSync(join(p.root, "script.json"), JSON.stringify(raw));
  return { ...p, script: { ...p.script, music } };
}

const PLAIN_PATHS = (stems) => ["script.json", "timeline.json", "src/custom/Orbit.tsx", "src/kit/theme.ts", "public/narration.wav", ...voicePaths(stems)];

test("a none project's pairs have no music entry, with or without a musicDir that does not exist", async (t) => {
  const p = project(t);
  const expected = { hash: expectedHash(p.root, PLAIN_PATHS(p.stems)) };
  assert.deepEqual(await hashNow(p), expected);
  assert.deepEqual(await hashNow(p, { musicDir: join(p.root, "no-library") }), expected);
});

test("a named track adds the pair music:<id> with the SHA-256 of the library file's bytes", async (t) => {
  const p = musicProject(t, "fixture-bar");
  const musicDir = musicLibrary(t);
  assert.deepEqual(await hashNow(p, { musicDir }), {
    hash: expectedHash(p.root, PLAIN_PATHS(p.stems), [], [["music:fixture-bar", sha256("bar audio")]]),
  });
});

test("changing one byte of the library file changes the hash", async (t) => {
  const p = musicProject(t, "fixture-bar");
  const before = (await hashNow(p, { musicDir: musicLibrary(t, "bar audio") })).hash;
  const after = (await hashNow(p, { musicDir: musicLibrary(t, "bar audiO") })).hash;
  assert.match(before, /^[0-9a-f]{64}$/);
  assert.notEqual(after, before);
});

test("a missing library, an unknown id or a non-regular track file gives a null pair, not a throw", async (t) => {
  const p = musicProject(t, "fixture-bar");
  const nullPair = { hash: expectedHash(p.root, PLAIN_PATHS(p.stems), [], [["music:fixture-bar", null]]) };
  assert.deepEqual(await hashNow(p, { musicDir: join(p.root, "no-library") }), nullPair, "missing library");
  const bad = musicLibrary(t);
  writeFileSync(join(bad, "manifest.json"), "{");
  assert.deepEqual(await hashNow(p, { musicDir: bad }), nullPair, "unparsable manifest");
  const linked = musicLibrary(t);
  rmSync(join(linked, "bar.wav"));
  symlinkSync(join(linked, "other.mp3"), join(linked, "bar.wav"));
  assert.deepEqual(await hashNow(p, { musicDir: linked }), nullPair, "symlinked track");
  const fifo = musicLibrary(t);
  rmSync(join(fifo, "bar.wav"));
  execFileSync("mkfifo", [join(fifo, "bar.wav")]);
  assert.deepEqual(await hashNow(p, { musicDir: fifo }), nullPair, "FIFO track");
  const q = musicProject(t, "fixture-gone");
  assert.deepEqual(
    await hashNow(q, { musicDir: musicLibrary(t) }),
    { hash: expectedHash(q.root, PLAIN_PATHS(q.stems), [], [["music:fixture-gone", null]]) },
    "unknown id",
  );
});

test("a track file that cannot be read (mode 000) gives a null pair, not a throw", { skip: process.getuid?.() === 0 && "root reads a mode-000 file" }, async (t) => {
  const p = musicProject(t, "fixture-bar");
  const musicDir = musicLibrary(t);
  chmodSync(join(musicDir, "bar.wav"), 0o000);
  assert.deepEqual(await hashNow(p, { musicDir }), { hash: expectedHash(p.root, PLAIN_PATHS(p.stems), [], [["music:fixture-bar", null]]) });
});

test("the track is read without the injectable reader", async (t) => {
  const { readHashed } = await hashing();
  const p = musicProject(t, "fixture-bar");
  const musicDir = musicLibrary(t);
  const seen = [];
  const read = async (path) => {
    seen.push(path);
    return readHashed(path);
  };
  const recorded = await hashNow(p, { musicDir, read });
  assert.deepEqual(recorded, await hashNow(p, { musicDir }));
  assert.ok(seen.length > 0);
  assert.deepEqual(seen.filter((path) => path.startsWith(musicDir)), []);
});
