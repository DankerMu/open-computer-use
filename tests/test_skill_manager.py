# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Registry skill ZIPs and names stay inside the skills cache (open-webui#740).

Seams: `ensure_skill_cached`, `get_user_skills` / `get_user_skills_sync` and
`get_skill_mounts`. Only the registry HTTP calls (`_download_skill_zip`,
`_fetch_user_config`, `_load_user_config_cache`) are replaced; one test also
wraps `zipfile.ZipFile.open` in a pass-through recorder. Every cache, manifest
and host path points into the test's temp root.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import stat
import sys
import zipfile
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1] / "computer-use-server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

MB = 1024 * 1024
OLD_SKILL = b"# old invoice skill\n"
OLD_MANIFEST = {"invoice": {"sha256": "old-sha", "cached_at": "2026-01-01T00:00:00Z"}}


@pytest.fixture
def sm(tmp_path, monkeypatch):
    """skill_manager with every path under tmp_path and no registry access."""
    import skill_manager

    cache = tmp_path / "cache"
    monkeypatch.setattr(skill_manager, "SKILLS_CACHE_DIR", cache)
    monkeypatch.setattr(skill_manager, "MANIFEST_PATH", cache / ".manifest.json")
    monkeypatch.setattr(skill_manager, "USER_CONFIGS_DIR", cache / "_user_configs")
    monkeypatch.setattr(skill_manager, "SKILLS_CACHE_HOST_PATH", tmp_path / "host")
    monkeypatch.setattr(skill_manager, "_memory_cache", {})
    monkeypatch.setattr(skill_manager, "_email_locks", {})

    async def no_registry(*_args):
        raise AssertionError("registry must not be called")

    monkeypatch.setattr(skill_manager, "_download_skill_zip", no_registry)
    monkeypatch.setattr(skill_manager, "_fetch_user_config", no_registry)
    monkeypatch.setattr(skill_manager, "_load_user_config_cache", lambda _email: None)
    return skill_manager


def serve_zip(sm, monkeypatch, data):
    async def download(name):
        assert name == "invoice"
        return data

    monkeypatch.setattr(sm, "_download_skill_zip", download)


def prior_cache(sm):
    cache_dir = sm.SKILLS_CACHE_DIR / "invoice"
    (cache_dir / "scripts").mkdir(parents=True)
    (cache_dir / "SKILL.md").write_bytes(OLD_SKILL)
    (cache_dir / "scripts" / "old.py").write_bytes(b"print('old')\n")
    sm.MANIFEST_PATH.write_text(json.dumps(OLD_MANIFEST))
    return cache_dir


def snapshot(root):
    """Every path under root mapped to its bytes (None for directories)."""
    tree = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            tree[os.path.relpath(os.path.join(dirpath, name), root)] = None
        for name in filenames:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as f:
                tree[os.path.relpath(path, root)] = f.read()
    return tree


def zip_of(entries):
    """Build ZIP bytes from (name or ZipInfo, bytes) pairs."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


def typed(name, mode):
    info = zipfile.ZipInfo(name)
    info.external_attr = mode << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def cache(sm):
    skill = sm.SkillInfo(name="invoice", description="", category="user", zip_sha256="new-sha")
    return asyncio.run(sm.ensure_skill_cached(skill))


# ---------------------------------------------------------------------------
# Rejection cases: None, nothing written anywhere, prior cache and manifest kept
# ---------------------------------------------------------------------------

def symlink_zip(_root):
    link = typed("link", stat.S_IFLNK | 0o777)
    return zip_of([("SKILL.md", b"# new\n"), (link, b"../../SYMLINK_TARGET")])


def many_entries_zip(_root):
    return zip_of([("SKILL.md", b"# new\n")] + [(f"f{i}.txt", b"x") for i in range(2000)])


def bomb_zip(_root):
    buf = io.BytesIO()
    zeros = bytes(MB)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("SKILL.md", b"# new\n")
        for i in range(3):
            info = zipfile.ZipInfo(f"zeros{i}.bin")
            info.compress_type = zipfile.ZIP_DEFLATED
            with zf.open(info, "w") as dst:
                for _ in range(70):
                    dst.write(zeros)
    return buf.getvalue()


REJECTED = {
    "a-relative-traversal": lambda root: zip_of(
        [("SKILL.md", b"# new\n"), ("../../REL_ESCAPE", b"escaped")]
    ),
    "b-absolute-entry": lambda root: zip_of(
        [("SKILL.md", b"# new\n"), (f"{root}/ABS_ESCAPE", b"escaped")]
    ),
    "c-all-dotdot-prefix": lambda root: zip_of(
        [("../SKILL.md", b"# new\n"), ("../x", b"escaped")]
    ),
    "d-all-absolute-prefix": lambda root: zip_of(
        [(f"{root}/SKILL.md", b"# new\n"), (f"{root}/x", b"escaped")]
    ),
    "h-backslash": lambda root: zip_of(
        [("SKILL.md", b"# new\n"), ("..\\..\\BS_ESCAPE", b"escaped")]
    ),
    "e-symlink-entry": symlink_zip,
    "f-2001-entries": many_entries_zip,
    "g-declared-size-over-200mb": bomb_zip,
    "i-stripped-prefix-absolute": lambda root: zip_of(
        [("x/SKILL.md", b"# new\n"), (f"x//{root}/DS_ESCAPE", b"escaped")]
    ),
}


@pytest.mark.parametrize("case", list(REJECTED))
def test_unsafe_zip_is_rejected_and_old_cache_kept(sm, tmp_path, monkeypatch, case):
    assert str(tmp_path).startswith("/")
    data = REJECTED[case](tmp_path)
    assert len(data) < sm.MAX_ZIP_SIZE
    prior_cache(sm)
    before = snapshot(tmp_path)
    serve_zip(sm, monkeypatch, data)

    assert cache(sm) is None
    assert snapshot(tmp_path) == before


def test_declared_size_case_inflates_past_200mb():
    info = zipfile.ZipFile(io.BytesIO(bomb_zip(None))).infolist()
    assert sum(i.file_size for i in info) > 200 * MB


def test_declared_size_rejects_before_any_entry_is_read(sm, monkeypatch):
    data = bomb_zip(None)
    opened = []
    original_open = zipfile.ZipFile.open

    def recording_open(self, name, *args, **kwargs):
        opened.append(getattr(name, "filename", name))
        return original_open(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", recording_open)
    serve_zip(sm, monkeypatch, data)

    assert cache(sm) is None
    assert opened == []


# ---------------------------------------------------------------------------
# Archive limits are inclusive: at most 2000 entries, at most 200 MB declared
# ---------------------------------------------------------------------------

def infos(count, sizes=()):
    entries = [zipfile.ZipInfo(f"f{i}.txt") for i in range(count)]
    for info, size in zip(entries, sizes):
        info.file_size = size
    return entries


@pytest.mark.parametrize("entries", [
    pytest.param(lambda: infos(2000), id="2000-entries"),
    pytest.param(lambda: infos(2, [100 * MB, 100 * MB]), id="200mb-declared"),
])
def test_archive_at_limit_is_accepted(sm, entries):
    assert sm._check_zip_entries(entries()) is None


@pytest.mark.parametrize("entries", [
    pytest.param(lambda: infos(2001), id="2001-entries"),
    pytest.param(lambda: infos(2, [100 * MB, 100 * MB + 1]), id="200mb-plus-1-declared"),
])
def test_archive_over_limit_is_rejected(sm, entries):
    with pytest.raises(ValueError):
        sm._check_zip_entries(entries())


# ---------------------------------------------------------------------------
# Registry name cases
# ---------------------------------------------------------------------------

def bad_names(root):
    return ["../outside-rel", "a/b", f"{root}/abs", "..", "ok\n", "a..b"]


def plant_bad_caches(sm, root):
    """Give every bad name a cached SKILL.md so an unfiltered name would mount."""
    for name in bad_names(root):
        target = sm.SKILLS_CACHE_DIR / name
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_bytes(b"# planted\n")
    valid = sm.SKILLS_CACHE_DIR / "invoice"
    valid.mkdir(parents=True, exist_ok=True)
    (valid / "SKILL.md").write_bytes(b"# valid\n")


def registry_entries(root):
    entries = [{"name": name, "category": "user", "zip_sha256": "s"} for name in bad_names(root)]
    entries.append({"name": "invoice", "description": "Invoices", "category": "user",
                    "zip_sha256": "inv-sha"})
    return entries


def assert_only_valid_names(sm, skills, root):
    names = [s.name for s in skills]
    for bad in bad_names(root):
        assert bad not in names
    invoice = [s for s in skills if s.name == "invoice"]
    assert len(invoice) == 1
    assert (invoice[0].description, invoice[0].category, invoice[0].zip_sha256) == (
        "Invoices", "user", "inv-sha",
    )
    assert names[1:] == sm.DEFAULT_PUBLIC_SKILLS

    assert sm.get_skill_mounts(skills) == {
        str(sm.SKILLS_CACHE_HOST_PATH / "invoice"): {
            "bind": "/mnt/skills/user/invoice", "mode": "ro",
        },
    }


def test_get_user_skills_drops_unsafe_registry_names(sm, tmp_path, monkeypatch):
    plant_bad_caches(sm, tmp_path)

    async def fetch(email):
        assert email == "user@example.com"
        return {"enabled_skills": registry_entries(tmp_path)}

    monkeypatch.setattr(sm, "_fetch_user_config", fetch)
    skills = asyncio.run(sm.get_user_skills("user@example.com"))
    assert_only_valid_names(sm, skills, tmp_path)


def test_get_user_skills_sync_drops_unsafe_names_from_disk_cache(sm, tmp_path, monkeypatch):
    plant_bad_caches(sm, tmp_path)
    monkeypatch.setattr(
        sm, "_load_user_config_cache",
        lambda _email: {"enabled_skills": registry_entries(tmp_path)},
    )
    skills = sm.get_user_skills_sync("user@example.com")
    assert_only_valid_names(sm, skills, tmp_path)


@pytest.mark.parametrize("name", ["invoice", "pdf-tools", "a.b_c-1", "X9"])
def test_valid_registry_name_is_kept(sm, name):
    skills = sm._parse_api_skills([{"name": name, "category": "user"}])
    assert skills[0].name == name


# ---------------------------------------------------------------------------
# Regression cases: valid ZIPs still land with SKILL.md at the cache root
# ---------------------------------------------------------------------------

def flat_zip():
    return zip_of([
        (typed("SKILL.md", stat.S_IFREG | 0o600), b"# new\n"),
        (typed("scripts/run.py", stat.S_IFREG | 0o700), b"print('run')\n"),
    ])


def wrapped_zip():
    return zip_of([
        (typed("invoice-skill/", stat.S_IFDIR | 0o755), b""),
        (typed("invoice-skill/SKILL.md", stat.S_IFREG | 0o644), b"# new\n"),
        (typed("invoice-skill/scripts/", stat.S_IFDIR | 0o755), b""),
        (typed("invoice-skill/scripts/run.py", stat.S_IFREG | 0o644), b"print('run')\n"),
    ])


def writestr_zip():
    data = zip_of([("SKILL.md", b"# new\n"), ("scripts/run.py", b"print('run')\n")])
    for info in zipfile.ZipFile(io.BytesIO(data)).infolist():
        assert stat.S_IFMT(info.external_attr >> 16) == 0
    return data


VALID = {"flat": flat_zip, "wrapped-with-dir-entries": wrapped_zip, "writestr-no-type-bits": writestr_zip}


def assert_cached(sm, result):
    cache_dir = sm.SKILLS_CACHE_DIR / "invoice"
    assert result == str(cache_dir)
    assert snapshot(cache_dir) == {
        "SKILL.md": b"# new\n", "scripts": None, "scripts/run.py": b"print('run')\n",
    }
    for rel in ("SKILL.md", "scripts/run.py"):
        assert stat.S_IMODE(os.stat(cache_dir / rel).st_mode) == 0o644
    assert stat.S_IMODE(os.stat(cache_dir / "scripts").st_mode) == 0o755
    assert json.loads(sm.MANIFEST_PATH.read_text())["invoice"]["sha256"] == "new-sha"
    assert [p for p in os.listdir(sm.SKILLS_CACHE_DIR) if p.startswith(".invoice-")] == []


@pytest.mark.parametrize("case", list(VALID))
def test_valid_zip_replaces_cache_in_place(sm, monkeypatch, case):
    cache_dir = prior_cache(sm)
    inode = os.stat(cache_dir).st_ino
    serve_zip(sm, monkeypatch, VALID[case]())

    assert_cached(sm, cache(sm))
    assert os.stat(cache_dir).st_ino == inode


@pytest.mark.parametrize("case", list(VALID))
def test_valid_zip_creates_fresh_cache(sm, monkeypatch, case):
    serve_zip(sm, monkeypatch, VALID[case]())
    assert_cached(sm, cache(sm))


def test_cache_hit_returns_without_download(sm):
    cache_dir = prior_cache(sm)
    skill = sm.SkillInfo(name="invoice", description="", category="user", zip_sha256="old-sha")
    assert asyncio.run(sm.ensure_skill_cached(skill)) == str(cache_dir)


def test_failed_download_falls_back_to_stale_cache(sm, monkeypatch):
    cache_dir = prior_cache(sm)
    serve_zip(sm, monkeypatch, None)
    assert cache(sm) == str(cache_dir)
    assert (cache_dir / "SKILL.md").read_bytes() == OLD_SKILL
