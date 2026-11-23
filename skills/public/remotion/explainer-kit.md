<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2025 Open Computer Use Contributors -->

# Explainer kit reference

Read this file before you write `script.json`. The steps themselves are in `SKILL.md`. In this file, `kit <command>` means `cd /home/assistant/videos/<name> && node bin/kit.mjs <command>`.

## The project

- `node /opt/remotion-template/bin/kit.mjs new <name>` copies the template into `/home/assistant/videos/<name>` and prints that path. `<name>` starts with a-z or 0-9, then a-z, 0-9 or `-`, at most 40 characters. A directory of that name that is not empty is refused: pick another name.
- Write only `script.json`, the images `kit image` imports, and at most one `src/custom/<Component>.tsx`. Do not edit any other file in the project, and never run `npx remotion` for an explainer.

## The script

`script.json` describes the whole video at 1920x1080 and 30 fps. The project's `script.schema.json` (JSON Schema) and its example `script.json` are the authority: when this file and they differ, they win. Start from the example and replace its content. Write the whole file at `/home/assistant/videos/<name>/script.json` with `create_file`.

Write all narration and all on-screen text in Chinese. Lengths count characters (code points), and no text may hold control characters.

```json
{
  "version": 1,
  "title": "<at most 24 characters>",
  "music": "<a track id, or none>",
  "hook": { "kind": "hook", "narration": [["<block>", "<block>"]], "content": { "title": "<at most 16>" } },
  "chapters": [
    { "name": "<at most 6>", "scenes": [{ "kind": "question", "narration": [["<block>"]], "content": { "question": "<at most 24>" } }] }
  ],
  "summary": { "kind": "summary", "narration": [["<block>"]], "content": { "points": ["<at most 16>"] } },
  "endCard": { "kind": "end-card", "content": { "title": "<at most 16>" } },
  "voice": { "id": "", "instructions": "<the narrator's voice>" }
}
```

- `hook` comes first and `summary` and `endCard` come last. Put 3 to 5 chapters between them, each with 1 to 4 scenes of the body kinds below.
- `narration` is 1 to 4 sentences. A sentence is a list of 1 to 4 caption blocks, and a block is 1 to 16 characters. Each block is one caption page and one speech file. `endCard` may have no `narration`.
- A scene may name one image: `"image": "<name>"`, the name `kit image` printed.
- `voice` is optional. An empty or absent `id` uses the server's default voice. `instructions` describes the voice, for example `a calm female narrator, Mandarin`.
- No other keys are allowed. The video must last 60 to 180 seconds, and its length comes from the narration.

## Scene kinds

Each row is a kind and what it shows, then its `content` fields with their maximum lengths. `?` marks an optional field.

| Kind | Use |
| --- | --- |
| `hook` | The opening title. First scene only. Content: `title` 16, `subtitle`? 24. |
| `question` | One question for the scenes after it to answer. Content: `question` 24. |
| `concept-card` | One idea with up to four points. Content: `kicker`? 8, `title` 12, `points` 1 to 4 of 16, `conclusion`? 20. |
| `process-steps` | Steps in order, with arrows. Content: `steps` 2 to 5 of `{label 8, detail? 16}`. |
| `cause-effect` | One cause leading to one effect. Content: `cause` 16, `effect` 16, `link`? 8. |
| `comparison` | Two things side by side. Content: `left` and `right`, each `{title 8, points 1 to 4 of 12}`. |
| `timeline` | Dated events in order. Content: `events` 2 to 6 of `{year 8, label 12, detail? 16}`. |
| `key-number` | One number that counts up. Content: `value` (a number), `unit`? 4, `label` 12. |
| `chart` | A bar or line chart. Content: `type` `bar` or `line`, `title` 12, `unit`? 4, `points` 2 to 8 of `{label 6, value}`. |
| `image-callouts` | An image with numbered markers and a legend. Needs `image`. Content: `callouts` 1 to 3 of `{x, y, label 10}`. |
| `definition-quote` | A term with its definition, or a quote. Content: `term` 10, `text` 40, `source`? 16. |
| `summary` | The closing points. Last scene before the end card. Content: `points` 1 to 4 of 16. |
| `end-card` | The closing card. Last scene. Content: `title` 16, `subtitle`? 24. |

A scene of kind `custom` is the one scene you draw yourself. See "Custom scenes".

## Images

- Every scene kind takes an optional `image`. The text then sits in a column beside the image. `image-callouts` needs one.
- Generate each illustration with the image generation tool. The image reaches `/mnt/user-data/files/` before your next tool call runs, and that call's result names it as `Synced image: /mnt/user-data/files/<file>`. The name often has a numbered suffix, such as `generated-image (2).png`.
- So, after generating, run any command, for example `ls /mnt/user-data/files`. Then pass `kit image` the reported path, quoted: `node bin/kit.mjs image "/mnt/user-data/files/generated-image (2).png"`.
- `kit image` takes a `.png`, `.jpg`, `.jpeg` or `.webp` file inside `/mnt/user-data/files/`. It copies it into `public/images/` and prints the plain name to write as the scene's `image`.
- In `image-callouts`, `x` and `y` are 0 to 1: the point's fraction of the image's width from the left and of its height from the top.

## Music

- `music` is `none` or the `id` of a track in `/opt/video-music/manifest.json`. List the tracks with `python3 -c 'import json; [print(t["id"], t["mood"], t["loop"], t["duration_seconds"]) for t in json.load(open("/opt/video-music/manifest.json"))["tracks"]]'`.
- Choose a track by its `mood` and `loop` fields. A `loop` track repeats for any length.
- A track whose `loop` is false and that is shorter than the video fails `kit check`.
- An unknown id fails `kit check`, and the reason lists the valid ids.
- Prefer a track to `none`, unless the user asks for no music.

## Numbers and callouts

- Round every `key-number` `value` and every `chart` point `value` so its formatted form is at most 8 code points. The kit formats numbers with `formatCount`: from 10000 in 万 and from 100000000 in 亿, cut to one decimal (`12345` shows as `1.2万`); smaller numbers show as written (`1234.5678` has 9 code points, so write `1234.6`).
- Put the unit in `unit`, not in the value.
- Space callout points well apart on the image. `kit check` does not detect overlapping markers.

## Custom scenes

- Use a `custom` scene only when no kind above can show the idea. A script holds at most one.
- In a chapter, write `{ "kind": "custom", "narration": [...], "component": "<Component>" }`, with an optional `image`. `<Component>` is a PascalCase name of up to 40 characters.
- Write the component in `src/custom/<Component>.tsx` as its default export. `kit check` and `kit render` write `src/custom/index.ts`, which re-exports that `default`; do not edit `index.ts`.
- The component receives these props, as `src/kit/render/CustomScene.tsx` defines them:
  - `timing`: `durationInFrames`, `narrationEndFrame`, and `sentences`, a list of `{ blocks: [{ text, startFrame, endFrame }] }`, with frames counted from the scene's start;
  - `theme`: the kit theme, with colours in `theme.brand`, sizes in `theme.type.sizes` and margins in `theme.safeArea`;
  - `image`: the scene's image name or `null`. Draw it with ``<Img src={staticFile(`images/${image}`)} />``.
- Every element must reach its final place within the 15-frame entrance (`ENTRANCE_FRAMES`), by frame 15 of the scene.
- Keep all text inside `theme.safeArea` and at least `theme.type.sizes.minimum` (32 px). `kit check` fails text that leaves the safe area, overflows its box or is smaller.
- Only for this component, read `/mnt/skills/public/remotion/remotion-best-practices/SKILL.md` for how to write Remotion code. Where it conflicts with the sandbox, the table at the top of `free-form.md` wins.

## Voice

- `kit voice` prints one JSON line per caption block that has no audio yet: `{"text":"…","filename":"<stem>.wav","voice":"…","instructions":"…"}`.
- Make one `generate_speech` call per line, with that line's `text`, `filename`, `voice` and `instructions` unchanged.
- `generate_speech` may return the file under a numbered suffix, such as `<stem> (2).wav`. `kit voice-import` still imports it. It prints `imported <k> of <n>` and one `missing <stem>.wav` line per block without audio; then run `kit voice` again and make the missing calls.
- A block's file name depends on its text, its place in the script and `voice`. After you change any of them, run step 4 again: only the changed blocks need speech.
- If `generate_speech` is not available, or it returns `speech synthesis is not configured`, an explainer cannot be made. Tell the user so, and stop.

## Check and status

- `kit check` and `kit render` start a background job and return at once. Only one job runs at a time.
- `kit status` reports the job started last, with these lines and exit codes:

| Exit | What `status` prints |
| --- | --- |
| `0` | `kit status: check passed`, or `kit status: render succeeded: <path>` |
| `1` | `kit status: check failed (<n> reasons)` or `render failed with exit <code> (<n> reasons)`, then the reasons; or `check died without an exit` or `render died without an exit` |
| `3` | `kit status: check running (pid <pid>, <s> s)` or `render running (pid <pid>, <s> s)` |
| `4` | `kit status: no check has run; run node bin/kit.mjs check` |

- Poll with `cd /home/assistant/videos/<name> && sleep 60 && node bin/kit.mjs status`, and repeat while it prints `running`. Keep each call under 100 seconds: a call longer than `COMMAND_TIMEOUT` (120 seconds by default) is killed. Do not pause between polls: the sandbox stops after about 10 minutes idle, which kills the job.
- After `check passed`, `status` prints `contact sheet: <path>`, the scene and frame of each of its four tiles, and ten numbered questions. `view` the sheet by that path once. Answer every question yes or no in your reply.
- On any "no": fix `script.json`, the image or the custom file the answer is about, then redo the steps the change affects. If you replaced an image, run step 3 for it. If you changed any `narration`, the `voice`, or added, removed or moved a scene, redo steps 4, 5 and 6. Otherwise redo step 6.
- If `view` returns `this model cannot view images`, tell the user the frames were not inspected, and go on to step 8.

## Render

- `kit render` needs a passing check, and the project must be unchanged since it passed. Otherwise it refuses with `no passing check` or `the project changed since the last passing check`: run step 6 again.
- It renders into the project and publishes `/mnt/user-data/files/<name>.mp4` only when the file is complete. A failed render leaves any earlier file of that name as it was.
- After `render succeeded`, tell the user that `/mnt/user-data/files/<name>.mp4` is in the Files tab.

## Failures

- A command that refuses prints `kit <command>: <reason>` and exits 1. `script.json is invalid` is followed by one line per problem, naming the chapter, scene, field and limit. Fix each, then run the command again.
- `kit timeline` refuses a video `outside 60–180 s`. Add or remove narration, then redo steps 4 and 5.
- `check failed` or `render failed`: read every reason `status` prints and fix what it names. When a reason ends with `run node bin/kit.mjs <command>`, redo the steps from the one that runs that command. Otherwise redo step 6.
- `died without an exit`: the job was killed, for example by an idle stop. Start the same job again with `kit check` or `kit render`.
