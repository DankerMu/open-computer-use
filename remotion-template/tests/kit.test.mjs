// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The kit command entry: `new`, `voice`, `voice-import` and `image`, driven
// through run(argv, io) with temporary directories for videosRoot, filesRoot
// and a copy of the template. Expected exit codes, stream contents and file
// names come from the task 3.5 fixture; the first hook block's stem
// (s1-0-0-4a7b077a) was computed outside the module with shasum.

import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  cpSync,
  existsSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  readlinkSync,
  rmSync,
  chmodSync,
  symlinkSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { IMAGE_NAME, validateScript } from "../bin/script-schema.mjs";
import { blockStems } from "../bin/voice-stem.mjs";
import { run } from "../bin/kit.mjs";

const TEMPLATE = fileURLToPath(new URL("..", import.meta.url)).replace(/\/$/, "");
const HOOK_STEM = "s1-0-0-4a7b077a";

// --- fixtures -------------------------------------------------------------

function minimal() {
  const chapter = (name, question, narration) => ({
    name,
    scenes: [{ kind: "question", narration, content: { question } }],
  });
  return {
    version: 1,
    title: "光的折射",
    music: "none",
    hook: { kind: "hook", narration: [["插进水里的筷子"]], content: { title: "筷子为什么弯了" } },
    chapters: [
      chapter("现象", "筷子真的弯了吗", [["为什么筷子", "在水里会弯"]]),
      chapter("原理", "光为什么会拐弯", [["光会拐弯"]]),
      chapter("应用", "眼镜如何矫正", [["眼镜"]]),
    ],
    summary: { kind: "summary", narration: [["光在介质间会拐弯"]], content: { points: ["折射改变光路"] } },
    endCard: { kind: "end-card", content: { title: "谢谢观看" } },
  };
}

// A template copy without node_modules, plus a real node_modules holding only
// a link to zod (so a spawned project can import the schema), and the tests/
// and out/ directories `new` must leave behind.
function sandbox(t) {
  const base = mkdtempSync(join(tmpdir(), "kit-test-"));
  t.after(() => rmSync(base, { recursive: true, force: true }));
  const template = join(base, "template");
  cpSync(TEMPLATE, template, {
    recursive: true,
    filter: (src) => relative(TEMPLATE, src).split(sep)[0] !== "node_modules",
  });
  mkdirSync(join(template, "node_modules"));
  symlinkSync(join(TEMPLATE, "node_modules", "zod"), join(template, "node_modules", "zod"));
  mkdirSync(join(template, "out"), { recursive: true });
  writeFileSync(join(template, "out", "old.mp4"), "x");
  const files = join(base, "files");
  mkdirSync(files);
  return { base, template, videos: join(base, "videos"), files };
}

async function kit(argv, root, box) {
  let out = "";
  let err = "";
  const code = await run(argv, {
    root,
    videosRoot: box.videos,
    filesRoot: box.files,
    stdout: { write: (text) => (out += text) },
    stderr: { write: (text) => (err += text) },
  });
  return { code, out, err };
}

// A project made by `new`, with the given script.json (null keeps the example).
async function project(t, script = minimal()) {
  const box = sandbox(t);
  const made = await kit(["new", "demo"], box.template, box);
  assert.equal(made.code, 0, made.err);
  const root = join(box.videos, "demo");
  if (script !== null) writeFileSync(join(root, "script.json"), JSON.stringify(script));
  return { ...box, root };
}

// Every entry under dir: relative path, type, and content or link target.
function tree(dir) {
  if (!existsSync(dir) && !isLink(dir)) return null;
  const entries = [];
  const visit = (path) => {
    const stat = lstatSync(path);
    const rel = relative(dir, path);
    if (stat.isSymbolicLink()) entries.push([rel, "link", readlinkSync(path)]);
    else if (stat.isDirectory()) {
      entries.push([rel, "dir"]);
      for (const name of readdirSync(path).sort()) visit(join(path, name));
    } else entries.push([rel, "file", readFileSync(path).toString("base64")]);
  };
  visit(dir);
  return entries;
}

function isLink(path) {
  try {
    return lstatSync(path).isSymbolicLink();
  } catch {
    return false;
  }
}

const lines = (text) => text.split("\n").filter(Boolean);

// --- the entry --------------------------------------------------------------

test("no subcommand and an unknown subcommand are usage errors", async (t) => {
  const box = sandbox(t);
  const none = await kit([], box.template, box);
  assert.equal(none.code, 2);
  assert.equal(none.out, "");
  for (const line of lines(none.err)) assert.match(line, /^kit: /);
  const unknown = await kit(["render"], box.template, box);
  assert.equal(unknown.code, 2);
  assert.match(unknown.err, /^kit: unknown subcommand "render"\n/);
});

test("executed directly, kit.mjs sets the exit code and uses its own directory as root", async (t) => {
  const usage = spawnSync(process.execPath, [join(TEMPLATE, "bin", "kit.mjs")], { encoding: "utf8" });
  assert.equal(usage.status, 2);
  assert.match(usage.stderr, /^kit: /);
  const box = await project(t);
  const voice = spawnSync(process.execPath, [join(box.root, "bin", "kit.mjs"), "voice"], {
    cwd: box.base,
    encoding: "utf8",
  });
  assert.equal(voice.status, 0, voice.stderr);
  assert.equal(lines(voice.stdout).length, 6);
  assert.equal(voice.stderr, "kit voice: 6 of 6 blocks need speech\n");
});

// --- new --------------------------------------------------------------------

test("new creates the project from the template sources with a node_modules link", async (t) => {
  const box = sandbox(t);
  chmodSync(join(box.template, "script.json"), 0o444);
  const result = await kit(["new", "water-cycle"], box.template, box);
  const target = join(box.videos, "water-cycle");
  assert.deepEqual(result, { code: 0, out: `${target}\n`, err: "" });
  assert.deepEqual(readdirSync(box.videos), ["water-cycle"]);
  const expected = readdirSync(box.template)
    .filter((name) => !["node_modules", "tests", "out"].includes(name))
    .sort();
  assert.deepEqual(readdirSync(target).sort(), [...expected, "node_modules"].sort());
  for (const name of expected) assert.deepEqual(tree(join(target, name)), tree(join(box.template, name)), name);
  assert.equal(readlinkSync(join(target, "node_modules")), join(box.template, "node_modules"));
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const path = join(dir, name);
      const stat = lstatSync(path);
      if (path === join(target, "node_modules")) continue;
      assert.ok(stat.isFile() || stat.isDirectory(), path);
      assert.equal(stat.mode & 0o200, 0o200, `${path} is not u+w`);
      assert.equal(stat.uid, process.getuid(), path);
      if (stat.isDirectory()) walk(path);
    }
  };
  walk(target);
  assert.equal(lstatSync(target).mode & 0o777, 0o755);
  const script = validateScript(readFileSync(join(target, "script.json"), "utf8"));
  assert.equal(script.ok, true, JSON.stringify(script.errors));
});

test("new accepts an existing empty target directory", async (t) => {
  const box = sandbox(t);
  mkdirSync(join(box.videos, "demo"), { recursive: true });
  const result = await kit(["new", "demo"], box.template, box);
  assert.equal(result.code, 0, result.err);
  assert.ok(existsSync(join(box.videos, "demo", "script.json")));
  assert.deepEqual(readdirSync(box.videos), ["demo"]);
});

test("new refuses a non-empty target, a file and a symlink, naming it and changing nothing", async (t) => {
  const box = sandbox(t);
  mkdirSync(join(box.videos, "old", "src"), { recursive: true });
  writeFileSync(join(box.videos, "old", "src", "Root.tsx"), "export {};\n");
  writeFileSync(join(box.videos, "file"), "x");
  mkdirSync(join(box.base, "empty"));
  symlinkSync(join(box.base, "empty"), join(box.videos, "link"));
  for (const name of ["old", "file", "link"]) {
    const before = tree(box.base);
    const result = await kit(["new", name], box.template, box);
    assert.equal(result.code, 1, name);
    assert.equal(result.out, "");
    assert.equal(result.err, `kit new: ${join(box.videos, name)}: exists and is not an empty directory\n`);
    assert.deepEqual(tree(box.base), before, name);
  }
});

test("new refuses bad names, a missing name and an extra argument with exit 2", async (t) => {
  const box = sandbox(t);
  for (const argv of [["Water"], ["-x"], ["a/b"], ["a".repeat(41)], [], ["demo", "extra"]]) {
    const result = await kit(["new", ...argv], box.template, box);
    assert.equal(result.code, 2, argv.join(" "));
    assert.equal(result.out, "");
    for (const line of lines(result.err)) assert.match(line, /^kit new: /);
  }
  assert.equal(existsSync(box.videos), false);
  assert.equal((await kit(["new", "a".repeat(40)], box.template, box)).code, 0);
});

test("new run from a project is a usage error", async (t) => {
  const box = await project(t);
  const result = await kit(["new", "other"], box.root, box);
  assert.equal(result.code, 2);
  assert.match(result.err, /^kit new: /);
  assert.equal(existsSync(join(box.videos, "other")), false);
});

test("the other subcommands refuse at the template root with the hint", async (t) => {
  const box = sandbox(t);
  for (const argv of [["voice"], ["voice-import"], ["image", "x.png"]]) {
    const before = tree(box.base);
    const result = await kit(argv, box.template, box);
    assert.equal(result.code, 2);
    assert.equal(result.out, "");
    assert.match(result.err, new RegExp(`^kit ${argv[0]}: `));
    assert.ok(result.err.includes(`cd ${box.videos}/<name> && node bin/kit.mjs ${argv[0]}`), result.err);
    assert.deepEqual(tree(box.base), before);
  }
});

test("a copy failing partway leaves no target and no temporary directory", async (t) => {
  const box = sandbox(t);
  symlinkSync(join(box.template, "package.json"), join(box.template, "zz-link"));
  const result = await kit(["new", "demo"], box.template, box);
  assert.equal(result.code, 1);
  assert.equal(result.out, "");
  assert.equal(result.err, `kit new: ${join(box.template, "zz-link")}: is a symlink; refusing to copy it\n`);
  assert.deepEqual(readdirSync(box.videos), []);
});

test("the example script passes unchanged: 3 chapters, no music, image, custom scene or voice", () => {
  const text = readFileSync(join(TEMPLATE, "script.json"), "utf8");
  const raw = JSON.parse(text);
  assert.equal(validateScript(text).ok, true);
  assert.equal(raw.chapters.length, 3);
  assert.equal(raw.music, "none");
  assert.equal(Object.hasOwn(raw, "voice"), false);
  const scenes = [raw.hook, ...raw.chapters.flatMap((chapter) => chapter.scenes), raw.summary, raw.endCard];
  for (const scene of scenes) {
    assert.equal(Object.hasOwn(scene, "image"), false);
    assert.notEqual(scene.kind, "custom");
  }
  const blocks = scenes.flatMap((scene) => (scene.narration ?? []).flat());
  for (const block of blocks) assert.match(block, /\p{Script=Han}/u);
});

// --- voice ------------------------------------------------------------------

test("voice prints one generate_speech call per missing block", async (t) => {
  const script = { ...minimal(), voice: { id: "alloy", instructions: "语速放慢" } };
  const box = await project(t, script);
  const stems = blockStems(validateScript(JSON.stringify(script)).script);
  const first = await kit(["voice"], box.root, box);
  assert.equal(first.code, 0);
  assert.equal(first.err, "kit voice: 6 of 6 blocks need speech\n");
  const calls = lines(first.out).map((line) => JSON.parse(line));
  assert.deepEqual(
    calls,
    stems.map(({ text, stem }) => ({ text, filename: `${stem}.wav`, voice: "alloy", instructions: "语速放慢" })),
  );
  for (const line of lines(first.out)) {
    assert.deepEqual(Object.keys(JSON.parse(line)), ["text", "filename", "voice", "instructions"]);
  }

  const unvoiced = await project(t);
  const plain = await kit(["voice"], unvoiced.root, unvoiced);
  assert.deepEqual(JSON.parse(lines(plain.out)[0]), {
    text: "插进水里的筷子",
    filename: `${HOOK_STEM}.wav`,
    voice: "",
    instructions: "",
  });
  mkdirSync(join(unvoiced.root, "public", "voice"), { recursive: true });
  writeFileSync(join(unvoiced.root, "public", "voice", `${HOOK_STEM}.wav`), "RIFF");
  const before = tree(unvoiced.root);
  const second = await kit(["voice"], unvoiced.root, unvoiced);
  assert.equal(second.err, "kit voice: 5 of 6 blocks need speech\n");
  assert.equal(lines(second.out).length, 5);
  assert.ok(!second.out.includes(HOOK_STEM));
  assert.deepEqual(tree(unvoiced.root), before);
  assert.equal((await kit(["voice", "extra"], unvoiced.root, unvoiced)).code, 2);
});

test("voice on an invalid script exits 1 with the located message and writes nothing", async (t) => {
  const script = minimal();
  script.chapters[1].scenes[0].narration[0][0] = "一二三四五六七八九十一二三四五六七";
  const box = await project(t, script);
  const before = tree(box.base);
  const result = await kit(["voice"], box.root, box);
  assert.equal(result.code, 1);
  assert.equal(result.out, "");
  assert.equal(
    result.err,
    "kit voice: script.json is invalid\n" +
      "  chapter 2 原理, scene 1 (question): narration[0][0] is too long (limit 16 characters)\n",
  );
  assert.deepEqual(tree(box.base), before);
});

test("an invalid script prints at most 20 messages and the remaining count", async (t) => {
  const script = minimal();
  for (let i = 0; i < 25; i++) script[`extra${String(i).padStart(2, "0")}`] = i;
  const box = await project(t, script);
  const result = await kit(["voice"], box.root, box);
  assert.equal(result.code, 1);
  const errLines = result.err.split("\n").slice(0, -1);
  assert.equal(errLines.length, 22);
  assert.equal(errLines[0], "kit voice: script.json is invalid");
  assert.equal(errLines[1], "  script: extra00 is not allowed (limit unknown key)");
  assert.equal(errLines[20], "  script: extra19 is not allowed (limit unknown key)");
  assert.equal(errLines[21], "  … and 5 more");
});

test("a missing script.json exits 1 with not found", async (t) => {
  const box = await project(t, null);
  unlinkSync(join(box.root, "script.json"));
  for (const sub of ["voice", "voice-import"]) {
    const result = await kit([sub], box.root, box);
    assert.equal(result.code, 1);
    assert.equal(result.err, `kit ${sub}: ${join(box.root, "script.json")}: not found\n`);
  }
});

// --- voice-import -----------------------------------------------------------

async function voiceProject(t) {
  const box = await project(t);
  const stems = blockStems(validateScript(JSON.stringify(minimal())).script).map((entry) => entry.stem);
  assert.equal(stems[0], HOOK_STEM);
  const put = (name, content = name) => writeFileSync(join(box.files, name), content);
  const imported = (stem) => readFileSync(join(box.root, "public", "voice", `${stem}.wav`), "utf8");
  return { ...box, stems, put, imported };
}

test("voice-import places a suffixed file under its stem", async (t) => {
  const box = await voiceProject(t);
  box.put(`${HOOK_STEM} (2).wav`);
  for (const stem of box.stems.slice(1)) box.put(`${stem}.wav`);
  const result = await kit(["voice-import"], box.root, box);
  assert.deepEqual(result, { code: 0, out: "", err: "kit voice-import: imported 6 of 6\n" });
  assert.equal(box.imported(HOOK_STEM), `${HOOK_STEM} (2).wav`);
  assert.deepEqual(readdirSync(join(box.root, "public", "voice")).sort(), box.stems.map((s) => `${s}.wav`).sort());
});

test("voice-import prefers the exact name, then the lowest suffix", async (t) => {
  const box = await voiceProject(t);
  const [a, b, c, d] = box.stems;
  box.put(`${a} (2).wav`);
  box.put(`${a}.wav`);
  box.put(`${b} (3).wav`);
  box.put(`${b} (2).wav`);
  box.put(`${c} (10).wav`);
  box.put(`${c} (9).wav`);
  box.put(`${d} (02).wav`);
  box.put(`${d} (1).wav`);
  box.put(`${d} (11).wav`);
  for (const stem of box.stems.slice(4)) box.put(`${stem}.wav`);
  const result = await kit(["voice-import"], box.root, box);
  assert.equal(result.code, 0, result.err);
  assert.equal(box.imported(a), `${a}.wav`);
  assert.equal(box.imported(b), `${b} (2).wav`);
  assert.equal(box.imported(c), `${c} (9).wav`);
  assert.equal(box.imported(d), `${d} (11).wav`);
});

test("voice-import skips a symlink or directory with a stem's name, warning", async (t) => {
  const box = await voiceProject(t);
  const [a, b] = box.stems;
  writeFileSync(join(box.base, "outside.wav"), "outside");
  symlinkSync(join(box.base, "outside.wav"), join(box.files, `${a}.wav`));
  box.put(`${a} (2).wav`);
  mkdirSync(join(box.files, `${b}.wav`));
  box.put(`${b} (4).wav`);
  for (const stem of box.stems.slice(2)) box.put(`${stem}.wav`);
  const result = await kit(["voice-import"], box.root, box);
  assert.equal(result.code, 0, result.err);
  assert.equal(box.imported(a), `${a} (2).wav`);
  assert.equal(box.imported(b), `${b} (4).wav`);
  assert.equal(
    result.err,
    `kit voice-import: ${join(box.files, `${a}.wav`)}: is a symlink; skipped\n` +
      `kit voice-import: ${join(box.files, `${b}.wav`)}: is not a regular file; skipped\n` +
      "kit voice-import: imported 6 of 6\n",
  );
});

test("voice-import imports what it finds, lists what is missing, and exits 1", async (t) => {
  const box = await voiceProject(t);
  const missing = [box.stems[2], box.stems[5]];
  for (const stem of box.stems) if (!missing.includes(stem)) box.put(`${stem}.wav`);
  box.put(`${box.stems[2]} (1).wav`);
  const result = await kit(["voice-import"], box.root, box);
  assert.equal(result.code, 1);
  assert.equal(result.out, "");
  assert.equal(
    result.err,
    "kit voice-import: imported 4 of 6\n" +
      `kit voice-import: missing ${missing[0]}.wav\n` +
      `kit voice-import: missing ${missing[1]}.wav\n`,
  );
  for (const stem of box.stems) {
    assert.equal(existsSync(join(box.root, "public", "voice", `${stem}.wav`)), !missing.includes(stem), stem);
  }
});

test("a second voice-import after the sources are deleted succeeds and deletes nothing", async (t) => {
  const box = await voiceProject(t);
  for (const stem of box.stems) box.put(`${stem}.wav`);
  assert.equal((await kit(["voice-import"], box.root, box)).code, 0);
  writeFileSync(join(box.root, "public", "voice", "stale.wav"), "old");
  for (const name of readdirSync(box.files)) unlinkSync(join(box.files, name));
  const before = tree(box.root);
  const result = await kit(["voice-import"], box.root, box);
  assert.deepEqual(result, { code: 0, out: "", err: "kit voice-import: imported 6 of 6\n" });
  assert.deepEqual(tree(box.root), before);
});

test("voice-import refuses a public or public/voice that is a symlink or not a directory", async (t) => {
  const box = await voiceProject(t);
  for (const stem of box.stems) box.put(`${stem}.wav`);
  const elsewhere = join(box.base, "elsewhere");
  mkdirSync(elsewhere);
  mkdirSync(join(box.root, "public"));
  symlinkSync(elsewhere, join(box.root, "public", "voice"));
  const linked = await kit(["voice-import"], box.root, box);
  assert.equal(linked.code, 1);
  assert.equal(linked.err, `kit voice-import: ${join(box.root, "public", "voice")}: is a symlink\n`);
  assert.deepEqual(readdirSync(elsewhere), []);
  rmSync(join(box.root, "public"), { recursive: true });
  writeFileSync(join(box.root, "public"), "x");
  const file = await kit(["voice-import"], box.root, box);
  assert.equal(file.code, 1);
  assert.equal(file.err, `kit voice-import: ${join(box.root, "public")}: is not a directory\n`);
});

test("voice-import validates the script before creating anything", async (t) => {
  const box = await voiceProject(t);
  writeFileSync(join(box.root, "script.json"), "{");
  const before = tree(box.root);
  const result = await kit(["voice-import"], box.root, box);
  assert.equal(result.code, 1);
  assert.match(result.err, /^kit voice-import: script\.json is invalid\n {2}script: script\.json is not valid JSON/);
  assert.deepEqual(tree(box.root), before);
  assert.equal((await kit(["voice-import", "x"], box.root, box)).code, 2);
});

// --- image ------------------------------------------------------------------

async function imageProject(t) {
  const box = await project(t);
  const put = (name, content = name) => writeFileSync(join(box.files, name), content);
  const image = (arg) => kit(["image", arg], box.root, box);
  const images = join(box.root, "public", "images");
  return { ...box, put, image, images };
}

test("image sanitizes names", async (t) => {
  const box = await imageProject(t);
  const cases = [
    ["generated-image (2).png", "generated-image-2.png"],
    ["水循环示意图.png", "image.png"],
    ["A..B.PNG", "A.B.png"],
    [`${"a".repeat(59)}.b.png`, `${"a".repeat(59)}.png`],
    ["  -_.photo  (final).JPEG", "photo-final.jpeg"],
    ["x.webp", "x.webp"],
  ];
  for (const [source, expected] of cases) {
    box.put(source);
    const result = await box.image(source);
    assert.deepEqual(result, { code: 0, out: `${expected}\n`, err: "" }, source);
    assert.equal(readFileSync(join(box.images, expected), "utf8"), source);
  }
  box.put("abs (1).jpg");
  assert.equal((await box.image(join(box.files, "abs (1).jpg"))).out, "abs-1.jpg\n");
});

test("a different file with the same name gets -2; an identical file reuses the name", async (t) => {
  const box = await imageProject(t);
  box.put("a b.png", "first");
  box.put("a(b).png", "second");
  box.put("a  b.png", "first");
  box.put("a+b.png", "third");
  assert.equal((await box.image("a b.png")).out, "a-b.png\n");
  assert.equal((await box.image("a(b).png")).out, "a-b-2.png\n");
  assert.equal((await box.image("a  b.png")).out, "a-b.png\n");
  assert.equal((await box.image("a(b).png")).out, "a-b-2.png\n");
  assert.equal((await box.image("a+b.png")).out, "a-b-3.png\n");
  assert.deepEqual(readdirSync(box.images).sort(), ["a-b-2.png", "a-b-3.png", "a-b.png"]);
  assert.equal(readFileSync(join(box.images, "a-b-2.png"), "utf8"), "second");
});

test("image refuses paths outside the files root, directories, symlinks out and other extensions", async (t) => {
  const box = await imageProject(t);
  writeFileSync(join(box.base, "x.png"), "outside");
  symlinkSync(join(box.base, "x.png"), join(box.files, "link.png"));
  mkdirSync(join(box.files, "dir.png"));
  box.put("x.gif");
  for (const arg of ["../x.png", join(box.base, "x.png"), "link.png", "dir.png", "x.gif", "absent.png"]) {
    const result = await box.image(arg);
    assert.equal(result.code, 1, arg);
    assert.equal(result.out, "");
    assert.match(result.err, /^kit image: .+\n$/);
    assert.ok(result.err.includes(arg), result.err);
  }
  assert.equal(existsSync(join(box.root, "public")), false);
});

test("image follows a symlink that stays inside the files root", async (t) => {
  const box = await imageProject(t);
  box.put("real.png", "inside");
  symlinkSync(join(box.files, "real.png"), join(box.files, "alias (2).png"));
  assert.equal((await box.image("alias (2).png")).out, "alias-2.png\n");
  assert.equal(readFileSync(join(box.images, "alias-2.png"), "utf8"), "inside");
});

test("image refuses a public/images that is a symlink, and usage errors exit 2", async (t) => {
  const box = await imageProject(t);
  box.put("a.png");
  mkdirSync(join(box.root, "public"));
  mkdirSync(join(box.base, "elsewhere"));
  symlinkSync(join(box.base, "elsewhere"), box.images);
  const result = await box.image("a.png");
  assert.equal(result.code, 1);
  assert.equal(result.err, `kit image: ${box.images}: is a symlink\n`);
  assert.equal((await kit(["image"], box.root, box)).code, 2);
  assert.equal((await kit(["image", "a.png", "b.png"], box.root, box)).code, 2);
});

// Seeded so a failure reproduces.
function random(seed) {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let x = Math.imul(state ^ (state >>> 15), 1 | state);
    x ^= x + Math.imul(x ^ (x >>> 7), 61 | x);
    return ((x ^ (x >>> 14)) >>> 0) / 4294967296;
  };
}

test("1,000 random names always sanitize to a name matching IMAGE_NAME", async (t) => {
  const box = await imageProject(t);
  const next = random(827);
  const pick = (list) => list[Math.floor(next() * list.length)];
  const pieces = [..."abcXYZ019", " ", "(", ")", ".", "..", "-", "_", "水", "雲", "\t", "\n", "\u0001", "\u007f", "😀", " ", "'", "#"];
  const extensions = ["png", "PNG", "jpg", "Jpeg", "webp"];
  const piece = () => pick(pieces);
  let seenLongDot = 0;
  let ran = 0;
  for (let i = 0; ran < 1000; i++) {
    let stem;
    if (i % 4 === 0) {
      stem = `${Array.from({ length: 59 }, () => pick([..."abc019-_"])).join("")}.${Array.from({ length: 1 + Math.floor(next() * 10) }, piece).join("")}`;
    } else {
      stem = Array.from({ length: 1 + Math.floor(next() * 70) }, piece).join("");
    }
    const name = `${stem}.${pick(extensions)}`;
    if (Buffer.byteLength(name) > 250) continue;
    ran++;
    writeFileSync(join(box.files, name), "same bytes");
    const result = await box.image(name);
    assert.equal(result.code, 0, `${JSON.stringify(name)}: ${result.err}`);
    const out = result.out.slice(0, -1);
    assert.equal(result.out, `${out}\n`);
    assert.match(out, IMAGE_NAME, JSON.stringify(name));
    assert.ok(existsSync(join(box.images, out)));
    unlinkSync(join(box.files, name));
    if (i % 4 === 0) seenLongDot++;
  }
  assert.equal(ran, 1000);
  assert.ok(seenLongDot >= 200);
});
