#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Download the verified CC0 music candidates into a new directory with a local listening page."""

from __future__ import annotations

import argparse
import html
import http.client
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from urllib.parse import quote
import zipfile

# Isolated mode (python -I) leaves the script directory off sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import music_candidates  # noqa: E402
from music_candidates import MOODS, MusicError  # noqa: E402

TARGET_PER_MOOD = 6

SCRIPT = """
const ids = JSON.parse(document.getElementById("candidate-ids").textContent);
const today = new Date();
const pad = (value) => String(value).padStart(2, "0");
document.getElementById("date").value =
  `${today.getFullYear()}-${pad(today.getMonth() + 1)}-${pad(today.getDate())}`;
document.getElementById("export").addEventListener("click", () => {
  const status = document.getElementById("status");
  const reviewer = document.getElementById("reviewer").value.trim();
  const date = document.getElementById("date").value;
  const decisions = [];
  const missing = [];
  for (const id of ids) {
    const chosen = document.querySelector(`input[name="decision-${id}"]:checked`);
    if (chosen) decisions.push({id, decision: chosen.value});
    else missing.push(id);
  }
  if (!reviewer || !/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(date)) {
    status.textContent = "Enter the reviewer and the date before exporting.";
    return;
  }
  if (missing.length) {
    status.textContent = `No decision yet for: ${missing.join(", ")}`;
    return;
  }
  const body = JSON.stringify({version: 1, reviewer, date, decisions}, null, 2) + "\\n";
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([body], {type: "application/json"}));
  link.download = "music-decisions.json";
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  status.textContent = `Exported ${decisions.length} decisions.`;
});
"""

STYLE = """
body { font-family: sans-serif; margin: 2rem auto; max-width: 60rem; line-height: 1.4; }
section.candidate { border: 1px solid #ccc; border-radius: 6px; padding: 0.8rem 1rem; margin: 1rem 0; }
section.candidate h3 { margin: 0 0 0.3rem; }
audio { width: 100%; }
.meta, .author { color: #555; }
"""


def _candidate_html(item: dict) -> str:
    esc = html.escape
    evidence, source = item["evidence"], item["source"]
    snapshot = Path(evidence["snapshot"]).name
    links = [
        f'<li><a href="evidence/{esc(quote(snapshot))}">Evidence snapshot</a> ({esc(evidence["kind"])},'
        f' captured {esc(evidence["captured"])} from <a href="{esc(evidence["capture_url"])}">this page</a>)</li>',
    ]
    if evidence["kind"] == "archived":
        links.append(
            f'<li><a href="{esc(evidence["wayback_capture"])}">Wayback capture of the original file</a>,'
            f' SHA-1 <code>{esc(evidence["wayback_sha1"])}</code></li>'
        )
    links.append(f'<li><a href="{esc(item["licence_url"])}">Licence</a></li>')
    member = f' (member <code>{esc(source["member"]["name"])}</code>)' if "member" in source else ""
    links.append(f'<li><a href="{esc(source["url"])}">Source</a>{member}</li>')
    identifier = esc(item["id"])
    audio = esc(f'{item["id"]}{music_candidates.audio_suffix(item)}')
    return "\n".join([
        f'<section class="candidate" data-id="{identifier}">',
        f'<h3>{esc(item["title"])} <span class="author">by {esc(item["author"])}</span></h3>',
        f'<p class="meta"><code>{identifier}</code>{" - loop" if item["loop"] else ""}</p>',
        f'<audio controls preload="none" src="{audio}"></audio>',
        "<ul>", *links, "</ul>",
        f'<p class="rerelease-note">{esc(item["rerelease_note"])}</p>',
        f'<label><input type="radio" name="decision-{identifier}" value="approve"> Approve</label>',
        f'<label><input type="radio" name="decision-{identifier}" value="drop"> Drop</label>',
        "</section>",
    ])


def render(candidates: list[dict]) -> str:
    ids = json.dumps([item["id"] for item in candidates]).replace("<", "\\u003c")
    parts = [
        "<!DOCTYPE html>", '<html lang="en">', "<head>", '<meta charset="utf-8">',
        "<title>Music candidates</title>", f"<style>{STYLE}</style>", "</head>", "<body>",
        "<h1>Music candidates</h1>",
        "<p>Listen to each track, read its evidence and re-release note, choose approve or drop,"
        " then export the decisions.</p>",
        '<p><label>Reviewer <input id="reviewer" type="text"></label>'
        ' <label>Date <input id="date" type="date"></label>'
        ' <button id="export" type="button">Export decisions</button></p>',
        '<p id="status" role="status"></p>',
    ]
    for mood in MOODS:
        members = [item for item in candidates if item["mood"] == mood]
        parts.append(f"<h2>{html.escape(mood)} ({len(members)} candidates, target {TARGET_PER_MOOD})</h2>")
        parts.extend(_candidate_html(item) for item in members)
    parts += [
        f'<script type="application/json" id="candidate-ids">{ids}</script>',
        f"<script>{SCRIPT}</script>", "</body>", "</html>", "",
    ]
    return "\n".join(parts)


def build_page(candidates_path: Path, root: Path, output: Path) -> list[dict]:
    candidates = music_candidates.load_candidates(candidates_path, root)
    if output.exists() or output.is_symlink():
        raise MusicError(f"{output}: destination already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".music-page-", dir=output.parent) as temporary:
        staged = Path(temporary) / "page"
        (staged / "evidence").mkdir(parents=True)
        for item in candidates:
            music_candidates.fetch_candidate(item, staged)
            snapshot = root / item["evidence"]["snapshot"]
            target = staged / "evidence" / snapshot.name
            if not target.exists():
                shutil.copyfile(snapshot, target)
        (staged / "index.html").write_text(render(candidates), encoding="utf-8")
        if output.exists() or output.is_symlink():
            raise MusicError(f"{output}: destination already exists")
        os.rename(staged, output)
    return candidates


def main() -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, default=here / "candidates.json")
    parser.add_argument("--root", type=Path, default=here.parents[1], help="repository root for evidence paths")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        candidates = build_page(args.candidates, args.root, args.output)
    except (OSError, ValueError, zipfile.BadZipFile, http.client.HTTPException) as exc:
        print(f"music: {exc}", file=sys.stderr)
        return 1
    print(f"music: verified {len(candidates)} candidates into {args.output}")
    for mood in MOODS:
        count = sum(item["mood"] == mood for item in candidates)
        shortfall = f" (shortfall {TARGET_PER_MOOD - count})" if count < TARGET_PER_MOOD else ""
        print(f"music: {mood}: {count} candidates, target {TARGET_PER_MOOD}{shortfall}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
