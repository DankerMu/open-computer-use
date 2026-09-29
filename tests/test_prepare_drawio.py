# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
from __future__ import annotations

import io
import sys
import tarfile
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1] / "computer-use-server"
if str(SERVER_DIR / "drawio") not in sys.path:
    sys.path.insert(0, str(SERVER_DIR / "drawio"))

from prepare_drawio import (  # noqa: E402
    ARCHIVE_ROOT,
    PINNED_VIEWER_SHA256,
    VIEWER_PATH,
    WEBAPP_PREFIX,
    extract_allowed_members,
    load_inventory,
    publish_bundle,
    sha256_bytes,
    validate_relative_path,
    verify_archive,
)


def _inventory(*paths: str) -> dict:
    files = [
        {"path": VIEWER_PATH, "sha256": PINNED_VIEWER_SHA256, "size": 4},
        {"path": "LICENSE", "sha256": "a" * 64, "size": 1},
    ]
    for path in paths:
        files.append({"path": path, "sha256": "b" * 64, "size": 1})
    return {"files": files}


def _regular_member(name: str, data: bytes) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.REGTYPE
    info.size = len(data)
    return info, data


def test_component_paths_allow_spaces_and_reject_escape():
    validate_relative_path("img/lib/allied_telesis/computer_and_terminals/Personal Computer Wireless.svg")
    load_inventory(_inventory("img/lib/Personal Computer Wireless.svg"))
    with pytest.raises(ValueError, match="Unsafe inventory path"):
        validate_relative_path("../escape.js")
    with pytest.raises(ValueError, match="Unsafe inventory path"):
        validate_relative_path("/absolute.js")
    with pytest.raises(ValueError, match="Unsafe inventory path"):
        validate_relative_path("stencils/../LICENSE")
    with pytest.raises(ValueError, match="Unsafe inventory path"):
        validate_relative_path("stencils//shape.xml")


def test_inventory_requires_viewer_and_root_license():
    with pytest.raises(ValueError, match="viewer hash"):
        load_inventory({"files": [{"path": "LICENSE", "sha256": "a" * 64, "size": 1}]})
    with pytest.raises(ValueError, match="Missing upstream LICENSE"):
        load_inventory({"files": [{"path": VIEWER_PATH, "sha256": PINNED_VIEWER_SHA256, "size": 4}]})


def test_inventory_rejects_duplicate_and_outside_closure():
    with pytest.raises(ValueError, match="Duplicate"):
        load_inventory(
            {
                "files": [
                    {"path": VIEWER_PATH, "sha256": PINNED_VIEWER_SHA256, "size": 4},
                    {"path": "LICENSE", "sha256": "a" * 64, "size": 1},
                    {"path": "LICENSE", "sha256": "c" * 64, "size": 1},
                ]
            }
        )
    with pytest.raises(ValueError, match="outside closure"):
        load_inventory(_inventory("src/main/java/evil.java"))


def test_verify_archive_rejects_wrong_bytes():
    with pytest.raises(ValueError, match="archive hash mismatch"):
        verify_archive(b"not-the-archive")


def test_extract_reads_approved_regular_bytes():
    license_bytes = b"L"
    viewer_bytes = b"VIEW"
    inventory = {
        "LICENSE": {"path": "LICENSE", "sha256": sha256_bytes(license_bytes), "size": 1},
        VIEWER_PATH: {
            "path": VIEWER_PATH,
            "sha256": sha256_bytes(viewer_bytes),
            "size": 4,
        },
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        license_info, license_data = _regular_member(f"{ARCHIVE_ROOT}LICENSE", license_bytes)
        viewer_info, viewer_data = _regular_member(
            f"{ARCHIVE_ROOT}{WEBAPP_PREFIX}{VIEWER_PATH}", viewer_bytes
        )
        archive.addfile(license_info, io.BytesIO(license_data))
        archive.addfile(viewer_info, io.BytesIO(viewer_data))
    extracted = extract_allowed_members(buffer.getvalue(), inventory)
    assert extracted["LICENSE"] == license_bytes
    assert extracted[VIEWER_PATH] == viewer_bytes


def test_extract_rejects_link_and_special_members():
    inventory = {
        VIEWER_PATH: {"path": VIEWER_PATH, "sha256": "a" * 64, "size": 1},
        "LICENSE": {"path": "LICENSE", "sha256": "b" * 64, "size": 1},
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo(name=f"{ARCHIVE_ROOT}{WEBAPP_PREFIX}{VIEWER_PATH}")
        info.type = tarfile.SYMTYPE
        info.linkname = "elsewhere"
        archive.addfile(info)
    with pytest.raises(ValueError, match="Rejected archive member"):
        extract_allowed_members(buffer.getvalue(), inventory)


def test_final_rename_failure_keeps_prior_bytes(tmp_path, monkeypatch):
    dest = tmp_path / "drawio"
    stage = tmp_path / "stage"
    dest.mkdir()
    stage.mkdir()
    (dest / "marker.txt").write_text("prior-valid-bundle")
    (stage / "marker.txt").write_text("staged-replacement")
    original = Path.replace

    def failing_replace(self, target):
        if Path(self).name.startswith("drawio.next-") and Path(target) == dest:
            raise OSError("forced final publication rename failure")
        return original(self, target)

    monkeypatch.setattr(Path, "replace", failing_replace)
    with pytest.raises(OSError, match="forced final publication rename failure"):
        publish_bundle(stage, dest)
    assert (dest / "marker.txt").read_text() == "prior-valid-bundle"


def test_backup_cleanup_failure_keeps_new_bytes(tmp_path, monkeypatch):
    dest = tmp_path / "drawio"
    stage = tmp_path / "stage"
    dest.mkdir()
    stage.mkdir()
    (dest / "marker.txt").write_text("prior-valid-bundle")
    (dest / "old-only.txt").write_text("old-material")
    (stage / "marker.txt").write_text("new-accepted-bundle")
    (stage / "new-only.txt").write_text("new-material")
    original = __import__("shutil").rmtree

    def failing_rmtree(path, *args, **kwargs):
        path = Path(path)
        if path.name.startswith("drawio.prev-"):
            (path / "old-only.txt").unlink(missing_ok=True)
            raise OSError("forced backup cleanup failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr("prepare_drawio.shutil.rmtree", failing_rmtree)
    with pytest.raises(Exception) as caught:
        publish_bundle(stage, dest)
    assert caught.value.__cause__ is not None
    assert "forced backup cleanup failure" in str(caught.value.__cause__)
    assert (dest / "marker.txt").read_text() == "new-accepted-bundle"
    assert (dest / "new-only.txt").read_text() == "new-material"
    assert not (dest / "old-only.txt").exists()


def test_competing_publisher_is_rejected_before_mutation(tmp_path):
    dest = tmp_path / "drawio"
    second = tmp_path / "stage-two"
    dest.mkdir()
    second.mkdir()
    (dest / "marker.txt").write_text("generation-old")
    (second / "marker.txt").write_text("generation-b")
    dest.with_name("drawio.publish.lock").write_text("held")
    with pytest.raises(RuntimeError, match="already in progress"):
        publish_bundle(second, dest)
    assert (second / "marker.txt").read_text() == "generation-b"
    assert (dest / "marker.txt").read_text() == "generation-old"
