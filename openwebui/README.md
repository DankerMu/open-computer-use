# Open WebUI Integration

Everything needed to connect [Open WebUI](https://github.com/open-webui/open-webui) to [Open Computer Use](https://github.com/Wide-Moat/open-computer-use). Works with stock Open WebUI — no fork required.

> Just want to try it? [lab.widemoat.ai](https://lab.widemoat.ai) is a ready-to-use Open WebUI with Computer Use already wired in (GitHub/Google sign-in, models included). This directory is for embedding Computer Use into **your own** Open WebUI stack.

## Components

| # | Component | Type | Required | What it does |
|---|-----------|------|----------|-------------|
| 1 | [**tools/computer_use_tools.py**](tools/) | Tool | Yes | MCP client proxy — forwards `bash`, `create_file`, `str_replace`, `view`, `sub_agent` calls to the Computer Use Server |
| 2 | [**functions/computer_link_filter.py**](functions/) | Filter | Yes | Fetches the server-generated system prompt (skills list + file base URL embedded server-side) and the `X-Public-Base-URL` response header; decorates responses with preview/archive links |
| 3 | [**functions/video_production_filter.py**](functions/) | Toggle filter | Yes | The 视频制作 switch; when on, the system prompt lists the `remotion` video skill. `remove-wrapper-model.py` refuses until it is installed |
| 4 | [**functions/org_prompt_filter.py**](functions/) | Filter | Production-like deployment | Puts the mounted `/etc/ocu/prompts/system-prompt.md` in front of every system message; fails chats when that file is missing, so deactivate it where it is not mounted |

**The tool and the two filters are the whole integration.** All three work against a stock upstream Open WebUI.

## Quick Start

**Automatic** (recommended): `docker-compose.webui.yml` runs upstream Open WebUI and `init.sh` on first startup to install the tool and both filters, configure valves, mark the **tool public-read** (`group:*` + `user:*` grants) and **both filters active AND global** (two separate Open WebUI toggles), plus set `DEFAULT_MODEL_PARAMS = {function_calling: "native", stream_response: true}`.

**Manual**: Install the tool and both filters through Workspace UI, set Tool ID to `ai_computer_use`, set the video filter's id to `video_production_filter`, toggle **Active** and **Global** on both filters (both switches), set tool access to **Public** (Share → Public). See [setup guide](../README.md#required-setup-when-embedding-open-webui-into-your-own-stack) for the full checklist and common silent-fail traps.

## There are no patches here any more

This directory used to carry eight Python scripts that rewrote files **inside an
already-built Open WebUI image** by string substitution, applied by a Dockerfile that
is also gone. Two of them edited minified JavaScript, matching anchors like
`S.length===0?(S1.set(!1),S2.set(!1),h(f,0))` — names a minifier chooses fresh on every
release.

That form cannot survive an upgrade, and had not: measured against Open WebUI 0.11.3,
**none of the seven anchors** in the two largest scripts matched anything. They
described the shape of v0.9.2. A patch that no longer matches is not a patch that does
nothing loudly — several were documented as exiting 0 on failure.

The changes are source commits in a fork now, each with tests, and three of the eight
were dropped entirely because upstream had since fixed the same problems. What replaced
what is written down there rather than repeated here.

`tests/test-project-structure.sh` asserts this directory and that Dockerfile stay
absent, so reintroducing either fails the build.

