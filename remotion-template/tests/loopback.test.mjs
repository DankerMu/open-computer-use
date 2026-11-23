// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The render worker's localhost lookup (task 7.3, bin/loopback.mjs), pure.
// A sandbox created with networking disabled has an empty /etc/hosts and
// /etc/resolv.conf, so the system resolver cannot answer `localhost`, the
// host of Remotion's serve URL. Each test installs the wrapper on its own
// stub dns object, whose lookup records its calls. The expected answers are
// dns.lookup's documented callback forms: (err, address, family), or
// (err, [{ address, family }]) with `all: true`. The module is imported
// inside each test, so a missing one fails each test on its own.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const loopback = () => import("../bin/loopback.mjs");
const RENDER_VIDEO = fileURLToPath(new URL("../bin/render-video.mjs", import.meta.url));

// A stub dns whose lookup records every call and answers with `answer`
// through the callback, the last argument.
function stubDns(answer = [null, "192.0.2.7", 4]) {
  const calls = [];
  const original = function (...args) {
    calls.push(args);
    args.at(-1)(...answer);
    return "original-return";
  };
  return { dns: { lookup: original }, original, calls };
}

// Calls dns.lookup with args plus a callback; resolves with the callback's
// arguments and whether it was called before lookup returned.
function lookup(dns, ...args) {
  return new Promise((resolve) => {
    let returned = false;
    dns.lookup(...args, (...result) => resolve({ result, sync: !returned }));
    returned = true;
  });
}

const OPTION_FORMS = [
  ["no options", []],
  ["a number", [4]],
  ["an object", [{ family: 0, hints: 0 }]],
];

for (const host of ["localhost", "LOCALHOST"]) {
  for (const [form, options] of OPTION_FORMS) {
    test(`${host} with ${form} answers 127.0.0.1, family 4, without the original lookup`, async () => {
      const { installLoopbackLookup } = await loopback();
      const { dns, calls } = stubDns();
      installLoopbackLookup(dns);
      const { result } = await lookup(dns, host, ...options);
      assert.deepEqual(result, [null, "127.0.0.1", 4]);
      assert.deepEqual(calls, []);
    });
  }
}

test("localhost with all: true answers the one-element array", async () => {
  const { installLoopbackLookup } = await loopback();
  const { dns, calls } = stubDns();
  installLoopbackLookup(dns);
  const { result } = await lookup(dns, "localhost", { all: true, family: 0 });
  assert.deepEqual(result, [null, [{ address: "127.0.0.1", family: 4 }]]);
  assert.deepEqual(calls, []);
});

test("another hostname reaches the original lookup with the same arguments, and its result passes through", async () => {
  const { installLoopbackLookup } = await loopback();
  const { dns, calls } = stubDns([null, "192.0.2.7", 4]);
  installLoopbackLookup(dns);
  const options = { all: false, family: 4 };
  let returned;
  const result = await new Promise((resolve) => {
    const callback = (...args) => resolve(args);
    returned = dns.lookup("example.test", options, callback);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].length, 3);
    assert.equal(calls[0][0], "example.test");
    assert.equal(calls[0][1], options);
    assert.equal(calls[0][2], callback);
  });
  assert.deepEqual(result, [null, "192.0.2.7", 4]);
  assert.equal(returned, "original-return");
});

test("a hostname that only contains localhost reaches the original lookup", async () => {
  const { installLoopbackLookup } = await loopback();
  const { dns, calls } = stubDns();
  installLoopbackLookup(dns);
  const { result } = await lookup(dns, "localhost.example.test");
  assert.deepEqual(result, [null, "192.0.2.7", 4]);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "localhost.example.test");
});

test("the localhost callback is not called synchronously", async () => {
  const { installLoopbackLookup } = await loopback();
  const { dns } = stubDns();
  installLoopbackLookup(dns);
  for (const options of [[], [4], [{ all: true }]]) {
    const { sync } = await lookup(dns, "localhost", ...options);
    assert.equal(sync, false, `options ${JSON.stringify(options)}`);
  }
});

test("a second install does not wrap twice", async () => {
  const { installLoopbackLookup } = await loopback();
  const { dns, original, calls } = stubDns();
  installLoopbackLookup(dns);
  const wrapped = dns.lookup;
  assert.notEqual(wrapped, original);
  installLoopbackLookup(dns);
  assert.equal(dns.lookup, wrapped);
  await lookup(dns, "example.test");
  assert.equal(calls.length, 1);
});

test("bin/render-video.mjs calls installLoopbackLookup() before bundle(", () => {
  // Comment lines are dropped, so a comment naming either call proves nothing.
  const code = readFileSync(RENDER_VIDEO, "utf8")
    .split("\n")
    .filter((line) => !line.trimStart().startsWith("//"))
    .join("\n");
  const call = code.indexOf("installLoopbackLookup()");
  const bundle = code.indexOf("bundle(");
  assert.notEqual(call, -1, "no installLoopbackLookup() call");
  assert.notEqual(bundle, -1, "no bundle( call");
  assert.ok(call < bundle, "installLoopbackLookup() comes after bundle(");
  assert.match(code, /^import \{ installLoopbackLookup \} from "\.\/loopback\.mjs";$/m);
});
