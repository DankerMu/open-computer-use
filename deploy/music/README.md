# Background-music candidates

This directory holds the CC0 background-music candidates for the explainer kit, with their licence evidence, and the tool that builds a local listening page from them. No audio is committed. The OCU pre-commit refuses files over 500 KB, so this follows the font bundle in `deploy/fonts/`: the repository pins each source by URL, SHA-256 and size, and the tool downloads and verifies it.

- `candidates.json` lists the candidates.
- `excluded.json` lists works rejected for a licence conflict (another licence, an ISRC or a commercial release) or for their access terms, one entry per work. Works dropped for selection reasons, such as `loops.zip`, are not listed.
- `evidence/` holds one small text snapshot per evidence page.
- `music_candidates.py` loads and verifies these files.
- `listening_page.py` builds the listening page.
- `approvals.json` records the listening decisions (see Approval).
- `music_library.py` fetches and normalizes the approved tracks and writes the library manifest (see Library).
- `library-excluded.json` lists approved tracks the user excluded after their loudness was measured (see Library).

Nothing here enters the workspace image yet. The image install comes later (task 6.4).

## Evidence rule

A candidate is listed only when all of the following hold:

1. **A CC0 statement from the rights holder.** It comes in one of two kinds:
   - `live`: the source page shows the CC0 licence today, and the page names the uploader as the author. On OpenGameArt the licence field and the description are both read, and the description must not credit someone else or name unknown samples.
   - `archived`: the statement survives only in the Wayback Machine. The candidate also pins `wayback_capture`, the capture of the original audio URL, and `wayback_sha1`, the CDX digest of that capture. The base32 SHA-1 of the downloaded source bytes must equal `wayback_sha1`, so a mirror file is accepted only if it is byte-identical to the file the site served. FreePD tracks use this kind: FreePD closed in 2025, and its Internet Archive mirror is anonymous.
2. **No other licence.** The re-release search is written to `rerelease_note`: the catalogues searched, the query, the date and the result. A work also published under CC BY, sold or streamed through a commercial distributor, or registered with an ISRC goes to `excluded.json`, with the other licence, release or ISRC in `conflict`.
3. **Anonymous download over HTTPS.** Sources that need a login, or whose terms forbid automated download (Freesound originals, the Free Music Archive), go to `excluded.json` with `conflict: null`.

A site-wide CC0 search filter, or an uploader's CC0 label on a file someone else made, is not evidence on its own. That covers Internet Archive re-hosts, and OpenGameArt or Freesound uploads that credit another composer. Such works are neither candidates nor listed in `excluded.json`.

Each evidence snapshot is a trimmed plain-text extract of the statement page, under 500 KB. It records the capture URL, the capture date, the quoted licence statement and, for FreePD, the category-page row naming the composer, together with the hash chain.

## Formats

`candidates.json`:

```json
{"version": 1, "candidates": [{
  "id": "inventing-flight",
  "mood": "calm-curious",
  "loop": false,
  "title": "Inventing Flight",
  "author": "Bryan Teoh",
  "licence_url": "https://creativecommons.org/publicdomain/zero/1.0/",
  "source": {"url": "https://...", "sha256": "<hex>", "size": 2840034},
  "evidence": {"kind": "archived", "snapshot": "deploy/music/evidence/<file>", "captured": "2026-11-16",
               "capture_url": "https://...", "wayback_capture": "http://web.archive.org/web/<ts>id_/<original audio URL>",
               "wayback_sha1": "<32 base32 characters>"},
  "rerelease_note": "Searched ..."
}]}
```

- `id` matches `^[a-z0-9-]+$` and is unique.
- `mood` is one of `calm-curious`, `upbeat-corporate`, `neutral-serious` or `loop`. `loop` is true exactly for the `loop` mood.
- `source.member` (`{name, sha256, size}`) is present when the track sits inside a zip. The page then serves only that member. `sha256`, `size` and `wayback_sha1` still describe the zip.
- `live` evidence has exactly `kind`, `snapshot`, `captured` and `capture_url`.
- The target is 6 candidates per mood, so that 4 can be approved. A shortfall is reported, not treated as an error.

`excluded.json` holds `{"version": 1, "excluded": [{"title", "author", "source", "conflict", "reason"}]}`.

The listening page exports `{"version": 1, "reviewer", "date", "decisions": [{"id", "decision"}]}`, with `decision` exactly `approve` or `drop`. `music_candidates.load_decisions` accepts an export only if it has one decision for every candidate, no unknown id and no duplicate.

## Approval

`approvals.json` is the user's listening-page export, committed unmodified. Its SHA-256 is `207fcbd170d914cb96135e30fb03a6a2e9b7fccde5873d0e6b96ab1b45c99b6b`. The approval date is the export's `date` field, `2026-11-16`.

It approves 15 of the 24 candidates and drops 9. Approved per mood: calm-curious 6, upbeat-corporate 2, neutral-serious 3, loop 4. Upbeat-corporate and neutral-serious fall short of the target of four; the shortfall is reported, not treated as an error.

The library manifest (see Library) takes its approver and approval date from this file. The static test checks the SHA-256, the reviewer, the date and these counts, so any edit to the file fails.

## Listening page

```bash
python3 -I deploy/music/listening_page.py --output <new directory>
```

The tool refuses an existing output directory. It builds the page in a temporary sibling directory and renames it into place only after every candidate has verified, so a SHA-256, size, zip-member or Wayback-digest mismatch leaves nothing behind. Its output holds:

- `<id>.<ext>` for each candidate;
- `evidence/` with the snapshots;
- `index.html`, which shows each track with its player, evidence links, re-release note and approve/drop choice, and exports the decisions.

Open `index.html` in a browser, enter the reviewer and the date, decide every track, and export. The downloads are untrusted data. Keep the output outside the repository, and run Python that reads it with `-I`.

`python -m unittest discover -s tests/deploy -p test_music_listening.py -v` runs the tests, including a static check of the tracked files that prints the per-mood counts.

## Library

The source manifest of the library is the approved subset of `candidates.json`, read together with `approvals.json` and `library-excluded.json`. A candidate ships only if the approval file approves it and `library-excluded.json` does not list it. No second copy of the source fields is kept: `candidates.json` already pins each source's URL, size and SHA-256, its zip member, licence evidence, mood, loop flag and re-release note.

```bash
python3 -I deploy/music/music_library.py fetch --out <new sources directory>
python3 -I deploy/music/music_library.py build --sources <sources directory> --out <new library directory>
python3 -I deploy/music/music_library.py report
```

Every command takes `--candidates`, `--approvals`, `--excluded` and `--root`, which default to this directory's files and the repository root. The script uses only the standard library, `ffmpeg` and `ffprobe`.

- `fetch` downloads each approved source and verifies it as the listening page does. A zip track yields only its member. Dropped candidates are not downloaded.
- `build` reads `<id><suffix>` from the sources directory and checks it against its pin again: the member's SHA-256 and size for a zip track, the source's otherwise. Then it normalizes each track and writes the library.
- `report` prints the shipped count per mood against the target of four, for example `calm-curious 4/4`, and one line per excluded track with its measurements and reason. A shortfall is printed, not treated as an error.

`fetch` and `build` refuse an existing output directory. They work in a temporary sibling directory and rename it into place only after every track has passed, so a failure leaves no output. A digest mismatch, a missing source, an infeasible gain or an output out of tolerance fails and names the track.

Normalization applies one constant gain per track, loop or not. `ffmpeg -af ebur128=peak=true` measures the source's integrated loudness `I` and true peak `TP`. The gain is `min(-16 - I, -1.5 - TP)` dB: the gain to -16 LUFS, capped so the true peak stays at -1.5 dBTP.

- When the predicted loudness `I + gain`, rounded to one decimal, is at least -16.5 LUFS, the output is `volume=<gain>dB` alone. A capped track therefore ships up to 0.5 LU below -16 LUFS, inside the tolerance.
- Otherwise the build fails, naming the track with its `I`, `TP`, loudness range, the gain it needs and the loudness the cap would leave. No limiter, compressor or `loudnorm` is applied: a limiter pass lands below the target and limits sample peaks, not true peaks; `loudnorm` misses the target on peaky material; and a loop needs a constant gain to stay seamless.

`library-excluded.json` records the approved tracks the rule cannot ship. On 2026-11-17 the real sources were measured, and the user decided the same day to exclude four approved tracks rather than add a limiter or a margin:

- `infinite-wonder`, `space-ranger-loop` and `observing-the-star` fall outside the tolerance even with the cap;
- `and-just-like-that` passes the prediction, but after 320 kb/s MP3 encoding no gain meets both limits: -1.9 dB gives -16.6 LUFS at -1.4 dBTP, and -2.0 dB gives -16.7 LUFS at -1.6 dBTP.

MP3 encoding can raise the true peak by about 0.1 dB, so a non-loop track whose cap binds can fail the check after encoding. The check stays unchanged: such a track fails the build, naming its measured values. The file is unrelated to the licence exclusions in `excluded.json`:

```json
{"version": 1, "excluded": [{
  "id": "space-ranger-loop",
  "reason": "peak cap leaves it at -17.1 LUFS; a loop takes no limiter",
  "measured": {"integrated_lufs": -15.7, "true_peak_dbtp": -0.1, "loudness_range_lu": 1.6},
  "decided_by": "user", "date": "2026-11-17"
}]}
```

Every entry has exactly these fields; `measured` holds the source's ebur128 values. Each `id` must be an approved candidate and appear once, and `decided_by` must be `user`. Every command subtracts the listed ids, so the library ships 11 tracks: calm-curious 4, upbeat-corporate 1, neutral-serious 3, loop 3.

Non-loop tracks become stereo MP3 at a constant 320 kb/s (`libmp3lame`). Loop tracks become stereo 16-bit WAV. Both keep the source's sample rate, so a loop's sample count is unchanged. Each output is measured again, and `abs(I + 16) <= 0.5` and `TP <= -1.5` must hold.

The output directory is self-contained:

- `<id>.mp3` or `<id>.wav` per track;
- `evidence/` with the approved tracks' snapshots;
- `approvals.json`, a copy of the approval file;
- `manifest.json`, written last.

`manifest.json`, version 1. Every path in it is relative to the manifest's directory:

```json
{"version": 1,
 "approvals": {"file": "approvals.json", "sha256": "<hex>", "reviewer": "Danker", "date": "2026-11-16"},
 "tracks": [{
   "id": "observing-the-star", "mood": "calm-curious", "loop": false, "title": "...", "author": "yd",
   "file": "observing-the-star.mp3", "format": "mp3", "sample_rate": 44100, "duration_seconds": 123.456,
   "source_url": "https://...", "licence_url": "https://...", "evidence": "evidence/<file>",
   "source_sha256": "<hex>", "member": {"name": "<zip member>", "sha256": "<hex>"},
   "shipped_sha256": "<hex>", "integrated_lufs": -16.0, "true_peak_dbtp": -3.2,
   "rerelease_note": "Searched ...", "approver": "Danker", "approval_date": "2026-11-16"
 }]}
```

- `source_sha256` is the digest of the downloaded file, which is the zip for a zip track. `member` (`{name, sha256}`) is present only for zip tracks.
- `integrated_lufs` and `true_peak_dbtp` are the values measured after encoding.
- `approver` and `approval_date` equal `approvals.reviewer` and `approvals.date`.
- Tracks appear in `candidates.json` order.

`python -m unittest discover -s tests/deploy -p test_music_library.py -v` runs the library tests. The loudness tests generate short tracks with `ffmpeg -f lavfi` and skip only when `ffmpeg` is not on `PATH`.
