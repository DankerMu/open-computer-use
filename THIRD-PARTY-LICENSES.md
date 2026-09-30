# Third-party dependencies & licensing

This project's source code is FSL-1.1-Apache-2.0 (see [`LICENSE`](LICENSE)). The Docker image built from this repo bundles third-party software under various licenses, including but not limited to:

| Component | License | Notes |
| --- | --- | --- |
| PyMuPDF (`fitz`) | AGPL-3.0 OR Artifex Commercial | Bundled as a Python dep. If you build this image and host it as a public network service, AGPL-3.0 conveyance obligations may apply to **you** — including source-code disclosure. The maintainers of this repository do not host or distribute compiled images publicly and grant no sublicense to PyMuPDF. |
| extract-text | Anthropic Skill License (proprietary) | See [`vendor/extract-text/README.md`](vendor/extract-text/README.md) and [`skills/README.md`](skills/README.md). |
| Anthropic-authored skills (`docx`, `pdf`, `pptx`, `xlsx`, `file-reading`, `pdf-reading`) | Anthropic Skill License (proprietary) | See [`skills/README.md`](skills/README.md) for the full disclaimer and removal instructions. |
| GSD bundle ([`gsd-build/get-shit-done`](https://github.com/gsd-build/get-shit-done)) | Apache 2.0 (upstream) | Cloned at build time from upstream tag. |
| Superpowers bundle ([`obra/superpowers`](https://github.com/obra/superpowers)) | Apache 2.0 (upstream) | Cloned at build time from upstream tag. |
| Open WebUI base | BSD-3-Clause-with-additional-license-condition | Upstream; see [Open WebUI](https://github.com/open-webui/open-webui). |
| DOMPurify 3.4.16 (local `computer-use-server/static/purify.min.js`) | Apache-2.0 OR MPL-2.0 | Pinned npm package `dompurify@3.4.16` (`https://registry.npmjs.org/dompurify/-/dompurify-3.4.16.tgz`); unmodified approved `dist/purify.min.js`, parent-verified SHA-256 `2c90a9b46d6463f26038a29b686e82bc91de01fdac9d5229e7cfe3b360134ea2`. Upstream Apache-2.0 license copy: [`computer-use-server/static/purify.LICENSE`](computer-use-server/static/purify.LICENSE). npm tarball SHA-512 was verified by the parent gate. |
| Draw.io viewer v31.5.3 (`computer-use-server/static/drawio/`, generated) | Apache-2.0 with retained component notices | Pinned source commit `0f419a92c769adb5fb20f2b18053a5ae8c7e4993`; archive SHA-256 `42a3f9b9cbf2ae1a95f1c4a642996e2d96ee689e54a0e77430bae69975d09487`; viewer SHA-256 `41f8360963bb485db74517ae7ca8ca01e563b587607a82238f901750b14e26d0`. Prepared by `computer-use-server/drawio/prepare_drawio.py`. Root `LICENSE` is Apache-2.0. `img/LICENSE` and `stencils/LICENSE` retain Atlassian-related redistribution restrictions; those notices ship with the generated tree. Authored remote diagram resources are outside the offline viewer-material closure. |

**No warranties.** Source is provided "as is". Compliance with downstream licenses (AGPL conveyance, Anthropic Skill License, etc.) when you build, host, or redistribute the image is **your responsibility**. The repository maintainers do not act as a license clearinghouse and do not grant sublicenses to third-party components.
