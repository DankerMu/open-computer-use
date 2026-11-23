// SPDX-License-Identifier: FSL-1.1-Apache-2.0
// Copyright (c) 2025 Open Computer Use Contributors
//
// The render worker's answer for `localhost`. Remotion 4.0.537 serves the
// bundle on 127.0.0.1 (@remotion/renderer/dist/serve-static.js:57) but hands
// out the serve URL http://localhost:<port> (prepare-server.js:94), and its
// Node side downloads audio assets from that URL with node:http, which
// resolves `localhost` through dns.lookup. A sandbox created with networking
// disabled has an empty /etc/hosts and /etc/resolv.conf, so that lookup fails
// with EAI_AGAIN and the render with it. installLoopbackLookup answers
// `localhost` with the address Remotion binds, and leaves every other name to
// the system resolver.

import nodeDns from "node:dns";

const LOCALHOST = /^localhost$/i;
const ADDRESS = "127.0.0.1";
const FAMILY = 4;
const INSTALLED = Symbol("loopback lookup");

// Replaces dns.lookup on the given module object (node:dns's, whose lookup
// node:net reads at connect time) with a wrapper. `localhost`, in any ASCII
// case, is answered asynchronously in dns.lookup's callback form for every
// options form (none, a family number, an object; `all: true` gives the
// one-element array). Every other call goes to the original lookup with its
// arguments unchanged. A second install leaves the wrapper in place.
export function installLoopbackLookup(dns = nodeDns) {
  const original = dns.lookup;
  if (original[INSTALLED] === true) return;
  const lookup = function (hostname, ...rest) {
    if (typeof hostname !== "string" || !LOCALHOST.test(hostname)) {
      return original.call(this, hostname, ...rest);
    }
    const callback = rest.at(-1);
    const options = rest.length > 1 ? rest[0] : undefined;
    if (options !== null && typeof options === "object" && options.all === true) {
      process.nextTick(callback, null, [{ address: ADDRESS, family: FAMILY }]);
    } else {
      process.nextTick(callback, null, ADDRESS, FAMILY);
    }
  };
  lookup[INSTALLED] = true;
  dns.lookup = lookup;
}
