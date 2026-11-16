# Background-music candidates

This directory holds the CC0 background-music candidates for the explainer kit, with their licence evidence, and the tool that builds a local listening page from them. No audio is committed. The OCU pre-commit refuses files over 500 KB, so this follows the font bundle in `deploy/fonts/`: the repository pins each source by URL, SHA-256 and size, and the tool downloads and verifies it.

- `candidates.json` lists the candidates.
- `excluded.json` lists works rejected for a licence conflict (another licence, an ISRC or a commercial release) or for their access terms, one entry per work. Works dropped for selection reasons, such as `loops.zip`, are not listed.
- `evidence/` holds one small text snapshot per evidence page.
- `music_candidates.py` loads and verifies these files.
- `listening_page.py` builds the listening page.

Nothing here enters the workspace image. Normalization, the image install and the tracked approval file come later (tasks 6.2 to 6.4).

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
