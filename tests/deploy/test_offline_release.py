# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""Offline release inventory, import integrity, and startup preflight."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import time
import unittest

from support import (
    DEFAULT_RELEASE_DIGESTS,
    DEFAULT_RELEASE_IMAGES,
    DOCUMENTSERVER_UPSTREAM,
    FAKE_DOCKER,
    FONT_FILES,
    HISTORICAL_INCOMPATIBLE_SOURCE,
    ROOT,
    ROLE_ORDER,
    SOURCE_CONSUMER_CONTRACT,
    WEBUI_SYNTHETIC_SHA,
    config_payload,
    fake_env,
    git_init_commit,
    image_id_for_config,
    intended_docs,
    intended_docs_for_images,
    ops,
    prepare_up_context,
    run_script,
    seed_healthy_host,
    seed_images,
    serve_font_archive,
    synthetic_inventory,
    tmp_dir,
    write_fake_configs,
    write_font_bundle,
    write_font_pin,
    write_hybrid_image_archive,
    write_image_archive,
    write_inventory,
    write_network,
    write_release_for_sha,
)

sys.path.insert(0, str(ROOT / "deploy"))
import release


def digest_for(tag: str) -> str:
    return "sha256:" + hashlib.sha256(tag.encode("utf-8")).hexdigest()


PYODIDE_WHEELS = (
    (
        "black",
        "26.5.1",
        "black-26.5.1-py3-none-any.whl",
        "4ed7f7da04046d2e488437170797d3b4a4ad83906683bcb7dfc68b673bbce5e2",
    ),
    (
        "pathspec",
        "1.1.1",
        "pathspec-1.1.1-py3-none-any.whl",
        "a00ce642f577bf7f473932318056212bc4f8bfdf53128c78bbd5af0b9b20b189",
    ),
    (
        "mypy_extensions",
        "1.1.0",
        "mypy_extensions-1.1.0-py3-none-any.whl",
        "1be4cccdb0f2482337c4743e60421de3a356cd97508abadd57d47403e94f5505",
    ),
    (
        "pytokens",
        "0.4.1",
        "pytokens-0.4.1-py3-none-any.whl",
        "26cef14744a8385f35d0e095dc8b3a7583f6c953c2e3d269c7f82484bf5ad2de",
    ),
    (
        "seaborn",
        "0.13.2",
        "seaborn-0.13.2-py3-none-any.whl",
        "636f8336facf092165e27924f223d3c62ca560b1f2bb5dff7ab7fad265361987",
    ),
    (
        "openpyxl",
        "3.1.5",
        "openpyxl-3.1.5-py2.py3-none-any.whl",
        "5282c12b107bffeef825f4617dc029afaf41d0ea60823bbb665ef3079dc79de2",
    ),
    (
        "et_xmlfile",
        "2.0.0",
        "et_xmlfile-2.0.0-py3-none-any.whl",
        "7a91720bc756843502c3b7504c77b8fe44217c85c537d85037f0f536151b2caa",
    ),
)
DRAWIO_COMMIT = "0f419a92c769adb5fb20f2b18053a5ae8c7e4993"
DRAWIO_ARCHIVE_SHA256 = (
    "42a3f9b9cbf2ae1a95f1c4a642996e2d96ee689e54a0e77430bae69975d09487"
)
DRAWIO_VIEWER_SHA256 = (
    "41f8360963bb485db74517ae7ca8ca01e563b587607a82238f901750b14e26d0"
)


def write_pyodide_supplement(path: Path, wheels=PYODIDE_WHEELS) -> None:
    packages = []
    for name, version, file_name, digest in wheels:
        packages.append(
            {
                "name": name,
                "version": version,
                "file_name": file_name,
                "url": f"https://example.test/{file_name}",
                "sha256": digest,
                "imports": [name],
                "depends": [],
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"packages": packages}, indent=2) + "\n", encoding="utf-8"
    )


def write_drawio_prepare(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                'PINNED_COMMIT = "%s"' % DRAWIO_COMMIT,
                'PINNED_ARCHIVE_URL = f"https://codeload.github.com/jgraph/drawio/tar.gz/{PINNED_COMMIT}"',
                'PINNED_ARCHIVE_SHA256 = "%s"' % DRAWIO_ARCHIVE_SHA256,
                'PINNED_VIEWER_SHA256 = "%s"' % DRAWIO_VIEWER_SHA256,
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_drawio_inventory(path: Path, viewer_sha=DRAWIO_VIEWER_SHA256) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "files": [
                    {"path": "LICENSE", "sha256": "a" * 64, "size": 12},
                    {
                        "path": "js/viewer-static.min.js",
                        "sha256": viewer_sha,
                        "size": 32,
                    },
                ]
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def git_ls_files(root: Path) -> set[str]:
    return set(
        subprocess.check_output(
            ["git", "ls-files"], cwd=str(root), text=True
        ).splitlines()
    )


class OfflineReleaseTests(unittest.TestCase):
    def setUp(self):
        self.context = tmp_dir()
        self.root = Path(self.context.name)
        self.state = self.root / "fake-state"
        self.state.mkdir()
        self.lock_dir = self.root / "image-store-lock"
        self.lock_dir.mkdir()
        self.env = fake_env(self.state)
        self.env["PATH"] = (
            str(FAKE_DOCKER.parent) + os.pathsep + self.env.get("PATH", "")
        )
        self.env["DOCKER_HOST"] = "unix://" + str(self.state / "docker.sock")
        self.release_script = self.isolated_release()

    def tearDown(self):
        self.context.cleanup()

    def write_delivery(self, *, extra_tag=None, mutate=None, skip_role=None, name="delivery"):
        ocu = self.root / f"{name}-ocu-src"
        ocu.mkdir()
        for relative in (
            "deploy/production-like-test/init/run-init.sh",
            "openwebui/init.sh",
            "openwebui/tools/computer_use_tools.py",
            "openwebui/functions/computer_link_filter.py",
            "deploy/release.py",
            "deploy/fonts/prepare_fonts.py",
            "deploy/fonts/fonts.json",
            "deploy/up.sh",
            "deploy/settings.py",
            "deploy/production-like-test/scripts/bootstrap-test.sh",
            "deploy/production-like-test/scripts/write-deployed-version.sh",
        ):
            source = ROOT / relative
            dest = ocu / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(source.read_bytes())
        write_font_pin(ocu)
        (ocu / "README").write_text("delivery source\n", encoding="utf-8")
        ocu_sha = git_init_commit(ocu, "delivery source")
        delivery = self.root / name
        images_dir = delivery / "images"
        images_dir.mkdir(parents=True)
        write_font_bundle(delivery)
        image_records = {}
        for role in ROLE_ORDER:
            if role == skip_role:
                continue
            tag = DEFAULT_RELEASE_IMAGES[role]
            digest = DEFAULT_RELEASE_DIGESTS[role]
            config = config_payload(digest)
            tags = {tag: config}
            if extra_tag and role == "workspace":
                tags[extra_tag] = config_payload(digest_for(extra_tag))
            archive = write_image_archive(images_dir / f"{role}.tar", tags)
            image_records[role] = {
                "reference": tag,
                "configuration_digest": "sha256:" + hashlib.sha256(config).hexdigest(),
                "archive": {
                    "path": f"images/{role}.tar",
                    "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                },
            }
        bundle = delivery / "source.bundle"
        subprocess.run(
            ["git", "bundle", "create", str(bundle), "HEAD"],
            cwd=str(ocu),
            check=True,
            capture_output=True,
            text=True,
        )
        payload = synthetic_inventory(
            ocu_sha=ocu_sha,
            webui_sha=WEBUI_SYNTHETIC_SHA,
            images=image_records,
            bundle_sha=hashlib.sha256(bundle.read_bytes()).hexdigest(),
        )
        if mutate:
            mutate(payload, delivery)
        write_inventory(delivery / "release.json", payload)
        return delivery, payload, ocu_sha

    def import_cmd(self, delivery: Path, install: Path):
        return self.import_with(self.release_script, delivery, install)

    def verify_cmd(self, inventory: Path, *, mode="delivery", delivery=None, source=None):
        argv = [
            "python3",
            str(self.release_script),
            "verify",
            "--inventory",
            str(inventory),
            "--mode",
            mode,
        ]
        if delivery is not None:
            argv.extend(["--delivery", str(delivery)])
        if source is not None:
            argv.extend(["--source", str(source)])
        return subprocess.run(
            argv,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=self.env,
            check=False,
            timeout=20,
        )

    def isolated_release(self, *replacements: tuple[str, str]) -> Path:
        source = (ROOT / "deploy" / "release.py").read_text(encoding="utf-8")
        lock_literal = 'IMAGE_STORE_LOCK_DIR = Path("/run/ocu-image-store")'
        self.assertIn(lock_literal, source)
        source = source.replace(
            lock_literal,
            f"IMAGE_STORE_LOCK_DIR = Path({str(self.lock_dir)!r})",
            1,
        )
        for old, new in replacements:
            self.assertIn(old, source)
            source = source.replace(old, new, 1)
        self._script_serial = getattr(self, "_script_serial", 0) + 1
        dest = self.root / f"isolated-release-{self._script_serial}.py"
        dest.write_text(source, encoding="utf-8")
        dest.chmod(0o755)
        helper = self.root / "fonts" / "prepare_fonts.py"
        helper.parent.mkdir(exist_ok=True)
        helper.write_bytes((ROOT / "deploy/fonts/prepare_fonts.py").read_bytes())
        return dest

    def patched_release(self, *replacements: tuple[str, str]) -> Path:
        return self.isolated_release(*replacements)

    def import_with(self, script: Path, delivery: Path, install: Path, extra_env=None):
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [
                "python3",
                str(script),
                "import",
                "--delivery",
                str(delivery),
                "--install-root",
                str(install),
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=20,
        )

    def build_with(self, script: Path, ocu: Path, webui: Path, dest: Path, extra_env=None):
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [
                "python3",
                str(script),
                "build",
                "--ocu-source",
                str(ocu),
                "--webui-source",
                str(webui),
                "--destination",
                str(dest),
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=30,
        )

    def write_hybrid_delivery(self, *, extra_oci_names=(), extra_index_descriptors=(), nested_index=False, attestation=None):
        ocu = self.root / "hybrid-src"
        ocu.mkdir()
        for relative in (
            "deploy/production-like-test/init/run-init.sh",
            "openwebui/init.sh",
            "openwebui/tools/computer_use_tools.py",
            "openwebui/functions/computer_link_filter.py",
            "deploy/release.py",
            "deploy/fonts/prepare_fonts.py",
            "deploy/fonts/fonts.json",
            "deploy/up.sh",
            "deploy/production-like-test/scripts/bootstrap-test.sh",
            "deploy/production-like-test/scripts/write-deployed-version.sh",
        ):
            dest = ocu / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes((ROOT / relative).read_bytes())
        write_font_pin(ocu)
        (ocu / "README").write_text("hybrid delivery source\n", encoding="utf-8")
        ocu_sha = git_init_commit(ocu, "hybrid delivery source")
        delivery = self.root / "hybrid-delivery"
        images_dir = delivery / "images"
        images_dir.mkdir(parents=True)
        write_font_bundle(delivery)
        image_records = {}
        for role in ROLE_ORDER:
            tag = DEFAULT_RELEASE_IMAGES[role]
            digest = DEFAULT_RELEASE_DIGESTS[role]
            config = config_payload(digest)
            extras = extra_oci_names if role == "workspace" else ()
            archive = write_hybrid_image_archive(
                images_dir / f"{role}.tar",
                reference=tag,
                config=config,
                extra_oci_names=extras,
                extra_index_descriptors=extra_index_descriptors if role == "workspace" else (),
                nested_index=nested_index and role == "workspace",
                attestation=attestation if role == "workspace" else None,
            )
            image_records[role] = {
                "reference": tag,
                "configuration_digest": image_id_for_config(config),
                "archive": {
                    "path": f"images/{role}.tar",
                    "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                },
            }
        bundle = delivery / "source.bundle"
        subprocess.run(
            ["git", "bundle", "create", str(bundle), "HEAD"],
            cwd=str(ocu),
            check=True,
            capture_output=True,
            text=True,
        )
        payload = synthetic_inventory(
            ocu_sha=ocu_sha,
            webui_sha=WEBUI_SYNTHETIC_SHA,
            images=image_records,
            bundle_sha=hashlib.sha256(bundle.read_bytes()).hexdigest(),
        )
        write_inventory(delivery / "release.json", payload)
        return delivery, payload, ocu_sha

    def write_incompatible_delivery(self):
        ocu = self.root / "legacy-src"
        for relative in (
            "deploy/production-like-test/init/run-init.sh",
            "openwebui/init.sh",
            "openwebui/tools/computer_use_tools.py",
            "openwebui/functions/computer_link_filter.py",
            "deploy/up.sh",
            "deploy/fonts/prepare_fonts.py",
            "deploy/fonts/fonts.json",
            "deploy/production-like-test/scripts/bootstrap-test.sh",
            "deploy/production-like-test/scripts/write-deployed-version.sh",
        ):
            dest = ocu / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes((ROOT / relative).read_bytes())
        (ocu / "deploy" / "release.py").parent.mkdir(parents=True, exist_ok=True)
        (ocu / "deploy" / "release.py").write_text("FORMAT_VERSION = 1\n", encoding="utf-8")
        write_font_pin(ocu)
        (ocu / "README").write_text(f"legacy {HISTORICAL_INCOMPATIBLE_SOURCE}\n", encoding="utf-8")
        ocu_sha = git_init_commit(ocu, "legacy source")
        delivery = self.root / "legacy-delivery"
        images_dir = delivery / "images"
        images_dir.mkdir(parents=True)
        write_font_bundle(delivery)
        image_records = {}
        for role in ROLE_ORDER:
            tag = DEFAULT_RELEASE_IMAGES[role]
            digest = DEFAULT_RELEASE_DIGESTS[role]
            config = config_payload(digest)
            archive = write_image_archive(images_dir / f"{role}.tar", {tag: config})
            image_records[role] = {
                "reference": tag,
                "configuration_digest": image_id_for_config(config),
                "archive": {
                    "path": f"images/{role}.tar",
                    "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                },
            }
        bundle = delivery / "source.bundle"
        subprocess.run(
            ["git", "bundle", "create", str(bundle), "HEAD"],
            cwd=str(ocu),
            check=True,
            capture_output=True,
            text=True,
        )
        payload = synthetic_inventory(
            ocu_sha=ocu_sha,
            webui_sha=WEBUI_SYNTHETIC_SHA,
            images=image_records,
            bundle_sha=hashlib.sha256(bundle.read_bytes()).hexdigest(),
        )
        write_inventory(delivery / "release.json", payload)
        return delivery, payload, ocu_sha

    def build_cmd(self, ocu: Path, webui: Path, dest: Path, extra=None):
        argv = [
            "python3",
            str(self.release_script),
            "build",
            "--ocu-source",
            str(ocu),
            "--webui-source",
            str(webui),
            "--destination",
            str(dest),
        ]
        if extra:
            argv.extend(extra)
        return subprocess.run(
            argv,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=self.env,
            check=False,
            timeout=30,
        )

    def write_committed_sources(self):
        ocu = self.root / "build-ocu"
        webui = self.root / "build-webui"
        ocu.mkdir()
        webui.mkdir()
        (ocu / "Dockerfile").write_text(
            "FROM scratch\nARG CLAUDE_CODE_VERSION=2.1.112\nARG CODEX_VERSION=0.125.0\n",
            encoding="utf-8",
        )
        (ocu / "computer-use-server" / "Dockerfile").parent.mkdir(parents=True)
        (ocu / "computer-use-server" / "Dockerfile").write_text(
            "FROM scratch\n", encoding="utf-8"
        )
        write_drawio_prepare(
            ocu / "computer-use-server" / "drawio" / "prepare_drawio.py"
        )
        write_drawio_inventory(
            ocu / "computer-use-server" / "drawio" / "inventory.json"
        )
        retention = ocu / "deploy" / "production-like-test" / "retention"
        retention.mkdir(parents=True)
        (retention / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        (retention / "stop-overage.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        proxy = ocu / "deploy" / "proxy"
        proxy.mkdir(parents=True)
        (proxy / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        (ocu / "docker-compose.webui.yml").write_text(
            "services:\n  postgres:\n    image: postgres:17-alpine\n",
            encoding="utf-8",
        )
        for relative in (
            "deploy/production-like-test/init/run-init.sh",
            "openwebui/init.sh",
            "openwebui/tools/computer_use_tools.py",
            "openwebui/functions/computer_link_filter.py",
            "deploy/release.py",
            "deploy/up.sh",
            "deploy/settings.py",
            "deploy/__init__.py",
            "deploy/check-ports.sh",
            "deploy/provision-networks.sh",
            "deploy/check-sandbox-dns.sh",
            "deploy/check_sandbox_dns.py",
            "deploy/netinspect.py",
            "deploy/firewall/docker-user-rules.sh",
            "deploy/firewall/check.sh",
            "deploy/firewall/policy.py",
            "deploy/firewall/__init__.py",
            "deploy/production-like-test/compose.core.override.yml",
            "deploy/production-like-test/compose.webui.override.yml",
            "deploy/production-like-test/compose.proxy.yml",
            "deploy/production-like-test/scripts/bootstrap-test.sh",
            "deploy/production-like-test/scripts/write-deployed-version.sh",
            "computer-use-server/sandbox_dns.py",
            "docker-compose.yml",
            "docker-compose.webui.yml",
            "deploy/fonts/prepare_fonts.py",
            "deploy/fonts/fonts.json",
        ):
            dest = ocu / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes((ROOT / relative).read_bytes())
        url = self.enterContext(serve_font_archive())
        write_font_pin(ocu, url)
        # A wrong source pin must fail locally rather than fetch real fonts during tests.
        self.env.update(http_proxy="http://127.0.0.1:1", https_proxy="http://127.0.0.1:1",
                        no_proxy="127.0.0.1,localhost")
        ocu_sha = git_init_commit(ocu, "ocu sources")
        (webui / "Dockerfile").write_text(
            'FROM scratch\nARG BUILD_HASH=dev-build\nARG USE_TIKTOKEN_ENCODING_NAME="cl100k_base"\nARG USE_EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2\n',
            encoding="utf-8",
        )
        (webui / "scripts").mkdir()
        (webui / "scripts" / "prepare-pyodide.js").write_text(
            "if (distPackage.version !== '314.0.3') {\n\tthrow new Error(`Expected installed pyodide 314.0.3, found ${distPackage.version}`);\n}\n",
            encoding="utf-8",
        )
        write_pyodide_supplement(webui / "scripts" / "pyodide-supplement.json")
        (webui / "package-lock.json").write_text(
            json.dumps({"packages": {"node_modules/pyodide": {"version": "314.0.3"}}})
            + "\n",
            encoding="utf-8",
        )
        (webui / "package.json").write_text(
            json.dumps({"dependencies": {"pyodide": "^314.0.3"}}) + "\n",
            encoding="utf-8",
        )
        webui_sha = git_init_commit(webui, "webui sources")
        return ocu, webui, ocu_sha, webui_sha

    def add_untracked_canaries(self, ocu: Path, webui: Path) -> dict[str, Path]:
        canaries = {
            "ocu-root": ocu / "secret.env",
            "workspace": ocu / "untracked-workspace.env",
            "server": ocu / "computer-use-server" / "untracked-server.env",
            "retention": ocu
            / "deploy"
            / "production-like-test"
            / "retention"
            / "untracked-retention.env",
            "proxy": ocu / "deploy" / "proxy" / "untracked-proxy.env",
            "webui": webui / "untracked-webui.env",
        }
        for path in canaries.values():
            path.write_text("TOKEN=do-not-copy\n", encoding="utf-8")
        self.assertNotIn("secret.env", git_ls_files(ocu))
        self.assertNotIn("untracked-workspace.env", git_ls_files(ocu))
        self.assertNotIn("computer-use-server/untracked-server.env", git_ls_files(ocu))
        self.assertNotIn(
            "deploy/production-like-test/retention/untracked-retention.env",
            git_ls_files(ocu),
        )
        self.assertNotIn("deploy/proxy/untracked-proxy.env", git_ls_files(ocu))
        self.assertNotIn("untracked-webui.env", git_ls_files(webui))
        return canaries

    def recorded_builds(self):
        path = self.state / "builds.json"
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    def test_import_publishes_source_and_inventory_after_verified_load(self):
        delivery, inventory, ocu_sha = self.write_delivery()
        install = self.root / "install"
        result = self.import_cmd(delivery, install)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((install / "release.json").is_file())
        self.assertTrue((install / "source" / ".git").exists())
        loaded = json.loads((install / "release.json").read_text(encoding="utf-8"))
        self.assertEqual(loaded["ocu_source_sha"], ocu_sha)
        self.assertEqual(loaded["webui_source_sha"], WEBUI_SYNTHETIC_SHA)
        images = json.loads((self.state / "images.json").read_text(encoding="utf-8"))
        for role, record in inventory["images"].items():
            self.assertEqual(
                images[record["reference"]]["Id"], record["configuration_digest"], role
            )
        self.assertFalse((install / "images").exists())
        self.assertFalse((install / "source.bundle").exists())

    def test_undeclared_archive_tag_fails_before_load_and_keeps_unrelated_image(self):
        unrelated = "unrelated.example/app:keep"
        seed_images(
            self.state,
            {
                unrelated: {
                    "Id": digest_for(unrelated),
                    "Os": "linux",
                    "Architecture": "amd64",
                    "ConfigBytes": config_payload(digest_for(unrelated)).decode(
                        "utf-8"
                    ),
                }
            },
        )
        extra = "unrelated.example/app:keep"
        delivery, _inventory, _sha = self.write_delivery(extra_tag=extra)
        before = (self.state / "images.json").read_text(encoding="utf-8")
        install = self.root / "install-conflict"
        result = self.import_cmd(delivery, install)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("undeclared", result.stderr)
        self.assertFalse(install.exists())
        self.assertEqual(
            (self.state / "images.json").read_text(encoding="utf-8"), before
        )
        self.assertFalse((self.state / "load-count").exists())

    def test_fake_load_overwrites_unrelated_mapping_when_validation_is_bypassed(self):
        unrelated = "unrelated.example/app:keep"
        original = digest_for("original")
        seed_images(
            self.state,
            {
                unrelated: {
                    "Id": original,
                    "Os": "linux",
                    "Architecture": "amd64",
                    "ConfigBytes": config_payload(original).decode("utf-8"),
                }
            },
        )
        hostile = config_payload(digest_for("hostile"))
        archive = write_image_archive(self.root / "hostile.tar", {unrelated: hostile})
        loaded = subprocess.run(
            ["docker", "load", "-i", str(archive)],
            capture_output=True,
            text=True,
            env=self.env,
            check=False,
        )
        self.assertEqual(loaded.returncode, 0, loaded.stderr)
        images = json.loads((self.state / "images.json").read_text(encoding="utf-8"))
        self.assertEqual(
            images[unrelated]["Id"], "sha256:" + hashlib.sha256(hostile).hexdigest()
        )
        self.assertNotEqual(images[unrelated]["Id"], original)

    def test_corrupt_archive_checksum_fails_before_load(self):
        delivery, inventory, _sha = self.write_delivery()
        archive = delivery / inventory["images"]["workspace"]["archive"]["path"]
        archive.write_bytes(archive.read_bytes() + b"tamper")
        install = self.root / "install-corrupt"
        result = self.import_cmd(delivery, install)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum", result.stderr)
        self.assertFalse(install.exists())
        self.assertFalse((self.state / "load-count").exists())

    def test_symlink_archive_is_rejected_before_load(self):
        delivery, inventory, _sha = self.write_delivery()
        archive = delivery / inventory["images"]["proxy"]["archive"]["path"]
        real = archive.read_bytes()
        archive.unlink()
        target = delivery / "images" / "proxy-real.tar"
        target.write_bytes(real)
        archive.symlink_to(target)
        install = self.root / "install-link"
        result = self.import_cmd(delivery, install)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(install.exists())
        self.assertFalse((self.state / "load-count").exists())

    def test_term_during_load_reaps_child_and_removes_owned_publication(self):
        delivery, _inventory, _sha = self.write_delivery()
        install = self.root / "install-cancelled"
        hold = self.root / "hold-load"
        hold.touch()
        env = {**self.env, "FAKE_DOCKER_HOLD_LOAD": str(hold)}
        process = subprocess.Popen(
            [
                sys.executable,
                str(self.release_script),
                "import",
                "--delivery",
                str(delivery),
                "--install-root",
                str(install),
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        marker = self.state / "entered-load"
        try:
            deadline = time.monotonic() + 10
            while not marker.exists():
                self.assertIsNone(process.poll(), "import exited before load")
                self.assertLess(time.monotonic(), deadline, "load barrier not reached")
                time.sleep(0.02)
            child_pid = int(marker.read_text())
            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.wait(timeout=10), 128 + signal.SIGTERM)
            self.assertFalse(install.exists())
            self.assertFalse(list(self.root.glob("install-cancelled.stage-*")))
            self.assertFalse((self.root / "install-cancelled.publish.lock").exists())
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()

    def test_existing_destination_and_lock_fail_closed(self):
        delivery, _inventory, _sha = self.write_delivery()
        install = self.root / "install-existing"
        install.mkdir()
        (install / "keep").write_text("prior\n", encoding="utf-8")
        result = self.import_cmd(delivery, install)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((install / "keep").exists())
        lock = self.root / "install-locked.publish.lock"
        dest = self.root / "install-locked"
        lock.write_text("held\n", encoding="utf-8")
        result = self.import_cmd(delivery, dest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already in progress", result.stderr)
        self.assertFalse(dest.exists())

    def test_load_failure_leaves_no_install_root_and_does_not_delete_unrelated(self):
        unrelated = "keep.local/image:one"
        seed_images(
            self.state,
            {
                unrelated: {
                    "Id": digest_for(unrelated),
                    "Os": "linux",
                    "Architecture": "amd64",
                    "ConfigBytes": config_payload(digest_for(unrelated)).decode(
                        "utf-8"
                    ),
                }
            },
        )
        delivery, _inventory, _sha = self.write_delivery()
        (self.state / "load-fail").write_text("1", encoding="utf-8")
        install = self.root / "install-failed-load"
        result = self.import_cmd(delivery, install)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(install.exists())
        images = json.loads((self.state / "images.json").read_text(encoding="utf-8"))
        self.assertIn(unrelated, images)
        self.assertNotIn("rmi", "\n".join(ops(self.state)))

    def test_existing_image_inspect_error_fails_before_load(self):
        delivery, inventory, _sha = self.write_delivery()
        tag = inventory["images"]["workspace"]["reference"]
        seed_images(
            self.state,
            {
                tag: {
                    "Id": digest_for("other"),
                    "Os": "linux",
                    "Architecture": "amd64",
                    "ConfigBytes": config_payload(digest_for("other")).decode("utf-8"),
                }
            },
        )
        (self.state / "image-inspect-error.json").write_text(
            json.dumps({tag: {"code": 1, "message": "permission denied"}}),
            encoding="utf-8",
        )
        install = self.root / "install-inspect-error"
        result = self.import_cmd(delivery, install)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("permission denied", result.stderr.lower())
        self.assertFalse(install.exists())
        self.assertFalse((self.state / "load-count").exists())

    def test_reordered_inventory_roles_still_import(self):
        delivery, inventory, _sha = self.write_delivery()
        reordered = {
            "format_version": inventory["format_version"],
            "platform": inventory["platform"],
            "ocu_source_sha": inventory["ocu_source_sha"],
            "webui_source_sha": inventory["webui_source_sha"],
            "source_consumer_contract": inventory["source_consumer_contract"],
            "font_bundle": inventory["font_bundle"],
            "source_bundle": inventory["source_bundle"],
            "images": {
                role: inventory["images"][role]
                for role in reversed(list(inventory["images"]))
            },
        }
        write_inventory(delivery / "release.json", reordered)
        install = self.root / "install-reordered"
        result = self.import_cmd(delivery, install)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((install / "release.json").is_file())

    def test_checksum_valid_but_unclonable_bundle_fails_before_load(self):
        delivery, inventory, _sha = self.write_delivery()
        bundle = delivery / inventory["source_bundle"]["path"]
        junk = b"not-a-git-bundle" + os.urandom(32)
        bundle.write_bytes(junk)
        inventory["source_bundle"]["sha256"] = hashlib.sha256(junk).hexdigest()
        write_inventory(delivery / "release.json", inventory)
        install = self.root / "install-bad-bundle"
        result = self.import_cmd(delivery, install)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(install.exists())
        self.assertFalse((self.state / "load-count").exists())

    def test_colliding_archive_paths_fail_before_load(self):
        delivery, inventory, _sha = self.write_delivery()
        inventory["images"]["proxy"]["archive"]["path"] = inventory["images"][
            "workspace"
        ]["archive"]["path"]
        inventory["images"]["proxy"]["archive"]["sha256"] = inventory["images"][
            "workspace"
        ]["archive"]["sha256"]
        write_inventory(delivery / "release.json", inventory)
        install = self.root / "install-collision"
        result = self.import_cmd(delivery, install)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("collides", result.stderr)
        self.assertFalse(install.exists())
        self.assertFalse((self.state / "load-count").exists())

    def test_missing_archive_platform_metadata_fails_before_load(self):
        delivery, inventory, _sha = self.write_delivery()
        archive = delivery / inventory["images"]["workspace"]["archive"]["path"]
        write_image_archive(
            archive,
            {
                inventory["images"]["workspace"]["reference"]: json.dumps(
                    {"rootfs": {"diff_ids": []}}
                ).encode("utf-8")
            },
        )
        inventory["images"]["workspace"]["archive"]["sha256"] = hashlib.sha256(
            archive.read_bytes()
        ).hexdigest()
        write_inventory(delivery / "release.json", inventory)
        install = self.root / "install-no-platform"
        result = self.import_cmd(delivery, install)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("platform", result.stderr.lower())
        self.assertFalse(install.exists())
        self.assertFalse((self.state / "load-count").exists())

    def test_missing_role_and_wrong_platform_fail_schema(self):
        payload = write_release_for_sha(self.root / "bad.json", "a" * 40, "b" * 40)
        for missing_role in ("proxy", "documentserver"):
            with self.subTest(missing_role=missing_role):
                body = json.loads(payload.read_text(encoding="utf-8"))
                body["images"].pop(missing_role)
                with self.assertRaises(release.ReleaseError):
                    release.validate_inventory_schema(body)
        body = json.loads(payload.read_text(encoding="utf-8"))
        body["platform"] = "linux/arm64"
        with self.assertRaises(release.ReleaseError):
            release.validate_inventory_schema(body)

    def test_workspace_reference_must_keep_open_computer_use(self):
        payload = write_release_for_sha(self.root / "name.json", "a" * 40, "b" * 40)
        body = json.loads(payload.read_text(encoding="utf-8"))
        body["images"]["workspace"]["reference"] = "custom-workspace:local"
        with self.assertRaises(release.ReleaseError):
            release.validate_inventory_schema(body)

    def test_startup_rejects_modified_initializer_at_unchanged_head(self):
        env, script, source = prepare_up_context(self.state)
        write_fake_configs(self.state)
        seed_healthy_host(self.state)
        write_network(
            self.state, "ocu-test-private", subnet="172.30.0.0/24", gateway="172.30.0.1"
        )
        write_network(
            self.state, "ocu-sandbox", subnet="172.31.0.0/24", gateway="172.31.0.1"
        )
        init = source / "deploy" / "production-like-test" / "init" / "run-init.sh"
        init.write_text(
            init.read_text(encoding="utf-8") + "\n# dirty tracked initializer\n",
            encoding="utf-8",
        )
        result = run_script(script, env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tracked", result.stderr)
        self.assertFalse((self.state / "starts.log").exists())
        recorded = "\n".join(ops(self.state))
        self.assertNotIn("network create", recorded)
        self.assertNotIn("compose up", recorded)

    def test_unrelated_untracked_file_does_not_invalidate_release(self):
        env, script, source = prepare_up_context(self.state)
        write_fake_configs(self.state)
        seed_healthy_host(self.state)
        write_network(
            self.state, "ocu-test-private", subnet="172.30.0.0/24", gateway="172.30.0.1"
        )
        write_network(
            self.state, "ocu-sandbox", subnet="172.31.0.0/24", gateway="172.31.0.1"
        )
        (source / "scratch.local").write_text("untracked\n", encoding="utf-8")
        result = run_script(script, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.state / "starts.log").read_text(encoding="utf-8").splitlines(),
            ["core", "webui", "proxy"],
        )

    def test_missing_image_fails_before_network_or_compose_mutation(self):
        env, script, _source = prepare_up_context(self.state)
        write_fake_configs(self.state)
        seed_healthy_host(self.state)
        write_network(
            self.state, "ocu-test-private", subnet="172.30.0.0/24", gateway="172.30.0.1"
        )
        original_images = (self.state / "images.json").read_text()
        for role in ("proxy", "documentserver"):
            with self.subTest(role=role):
                images = json.loads(original_images)
                images.pop(DEFAULT_RELEASE_IMAGES[role])
                (self.state / "images.json").write_text(json.dumps(images))
                result = run_script(script, env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("missing image", result.stderr)
                self.assertFalse((self.state / "starts.log").exists())
                recorded = "\n".join(ops(self.state))
                self.assertNotIn("network create", recorded)
                self.assertNotIn("compose up", recorded)
                self.assertNotIn(" docker build ", " " + recorded + " ")
                self.assertNotIn(" docker pull ", " " + recorded + " ")

    def test_image_inspect_daemon_error_is_not_treated_as_missing(self):
        env, script, _source = prepare_up_context(self.state)
        write_fake_configs(self.state)
        seed_healthy_host(self.state)
        write_network(
            self.state, "ocu-test-private", subnet="172.30.0.0/24", gateway="172.30.0.1"
        )
        (self.state / "image-inspect-error.json").write_text(
            json.dumps(
                {
                    DEFAULT_RELEASE_IMAGES["proxy"]: {
                        "code": 1,
                        "message": "permission denied",
                    }
                }
            ),
            encoding="utf-8",
        )
        result = run_script(script, env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("permission denied", result.stderr.lower())
        self.assertNotIn("missing image", result.stderr)
        self.assertFalse((self.state / "starts.log").exists())

    def test_secret_build_arguments_are_rejected(self):
        with self.assertRaises(release.ReleaseError):
            release.split_arguments({}, {"NPM_TOKEN": "secret"})

    def test_version_one_import_and_verify_refuse_before_install_or_load(self):
        delivery, inventory, _sha = self.write_delivery(skip_role="documentserver")
        inventory["format_version"] = 1
        inventory["images"].pop("documentserver")
        inventory.pop("font_bundle")
        write_inventory(delivery / "release.json", inventory)
        install = self.root / "unsupported-release"
        for result in (
            self.import_cmd(delivery, install),
            self.verify_cmd(delivery / "release.json", delivery=delivery),
        ):
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("format_version 1", result.stderr)
        self.assertFalse(install.exists())
        self.assertFalse((self.state / "load-count").exists())
        self.assertFalse((self.state / "images.json").exists())
        self.assertEqual(ops(self.state), [])

    def test_documentserver_default_comes_from_selected_source_and_cache_is_reused(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        selected = "onlyoffice/documentserver@sha256:" + "d" * 64
        declaration = ocu / "deploy" / "release.py"
        declaration.write_text(declaration.read_text().replace(
            DOCUMENTSERVER_UPSTREAM.rsplit(":", 1)[1], "d" * 64
        ))
        git_init_commit(ocu, "select upstream image")
        for name in ("uncached", "cached"):
            destination = self.root / name
            result = self.build_cmd(ocu, webui, destination)
            self.assertEqual(result.returncode, 0, result.stderr)
            inventory = json.loads((destination / "release.json").read_text())
            image = inventory["images"]["documentserver"]
            self.assertEqual(image["build"]["arguments"], {"DOCUMENTSERVER_IMAGE": selected})
            self.assertEqual(image["build"]["argument_defaults"], {"DOCUMENTSERVER_IMAGE": selected})
            self.assertEqual(image["build"]["materials"], [
                {"name": "documentserver", "requested": selected, "kind": "upstream-image"}
            ])
            self.assertEqual(
                image["build"]["dockerfile_sha256"],
                hashlib.sha256(declaration.read_bytes()).hexdigest(),
            )
            pulls = [row for row in self.recorded_builds() if row["tag"] == selected]
            self.assertEqual([row["kind"] for row in pulls], ["pull"])
            self.assertEqual(image["configuration_digest"], pulls[0]["id"])

    def test_seven_image_release_starts_with_documentserver_compose_service(self):
        env, script, _source = prepare_up_context(self.state)
        write_fake_configs(self.state)
        seed_healthy_host(self.state)
        result = run_script(script, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.state / "starts.log").read_text().splitlines(),
                         ["core", "webui", "proxy"])
        self.assertFalse((self.state / "builds.json").exists())
        self.assertEqual(json.loads((self.state / "running.json").read_text())["documentserver"], "core")

    def test_documentserver_service_image_mismatch_refuses_before_mutation(self):
        env, script, _source = prepare_up_context(self.state)
        docs = intended_docs()
        docs["core.json"]["services"]["documentserver"]["image"] = "unselected-documentserver:mutant"
        write_fake_configs(self.state, docs)
        seed_healthy_host(self.state)
        write_network(self.state, "ocu-test-private", subnet="172.30.0.0/24", gateway="172.30.0.1")
        write_network(self.state, "ocu-sandbox", subnet="172.31.0.0/24", gateway="172.31.0.1")
        before_images = (self.state / "images.json").read_bytes()
        before_networks = {path.name: path.read_bytes() for path in (self.state / "networks").iterdir()}
        before_firewall = (self.state / "firewall.json").read_bytes()
        before_ops = len(ops(self.state))
        result = run_script(script, env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("documentserver", result.stderr)
        self.assertFalse((self.state / "starts.log").exists())
        self.assertEqual((self.state / "images.json").read_bytes(), before_images)
        self.assertEqual({path.name: path.read_bytes() for path in (self.state / "networks").iterdir()},
                         before_networks)
        self.assertEqual((self.state / "firewall.json").read_bytes(), before_firewall)
        mutations = {"load", "pull", "build", "create", "up", "iptables-restore", "ip6tables-restore"}
        self.assertFalse(any(set(row.split()) & mutations for row in ops(self.state)[before_ops:]))

    def test_import_rejects_built_documentserver_before_loading_images(self):
        delivery, inventory, _sha = self.write_delivery()
        inventory["images"]["documentserver"]["build"] = dict(
            inventory["images"]["proxy"]["build"]
        )
        write_inventory(delivery / "release.json", inventory)
        install = self.root / "built-documentserver-install"
        result = self.import_cmd(delivery, install)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("documentserver", result.stderr)
        self.assertFalse(install.exists())
        self.assertFalse((self.state / "load-count").exists())

    def test_build_records_verified_font_bundle(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        destination = self.root / "font-release"
        result = self.build_cmd(ocu, webui, destination)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        inventory = json.loads((destination / "release.json").read_text())
        self.assertIn("font_bundle", inventory)
        bundle = destination / inventory["font_bundle"]["path"]
        self.assertEqual(
            inventory["font_bundle"]["sha256"],
            hashlib.sha256(bundle.read_bytes()).hexdigest(),
        )
        pin = json.loads((ocu / "deploy/fonts/fonts.json").read_text())
        expected = {
            item["name"]: item
            for archive in pin["archives"]
            for item in archive["files"]
        }
        with tarfile.open(bundle) as archive:
            self.assertEqual(set(archive.getnames()), set(expected))
            for name, item in expected.items():
                member = archive.getmember(name)
                self.assertTrue(member.isfile())
                content = archive.extractfile(member).read()
                self.assertEqual(len(content), item["size"])
                self.assertEqual(hashlib.sha256(content).hexdigest(), item["sha256"])
        installed = self.root / "font-install"
        imported = self.import_cmd(destination, installed)
        self.assertEqual(imported.returncode, 0, imported.stdout + imported.stderr)
        self.assertFalse((installed / "fonts.tar").exists())
        self.assertEqual(
            {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
             for path in (installed / "fonts").iterdir()},
            {name: item["sha256"] for name, item in expected.items()},
        )

    def test_build_rejects_wrong_font_archive_and_file_hashes(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        pin_path = ocu / "deploy/fonts/fonts.json"
        original = pin_path.read_text()
        for defect in ("archive", "file"):
            with self.subTest(defect=defect):
                pin = json.loads(original)
                record = pin["archives"][0]
                (record if defect == "archive" else record["files"][0])["sha256"] = "0" * 64
                pin_path.write_text(json.dumps(pin))
                git_init_commit(ocu, f"wrong font {defect} hash")
                destination = self.root / f"font-{defect}-refused"
                result = self.build_cmd(ocu, webui, destination)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("SHA-256", result.stderr)
                self.assertFalse(destination.exists())
                self.assertEqual(ops(self.state), [])

    def test_import_and_verify_refuse_invalid_font_material(self):
        outside = self.root / "outside.otf"
        outside.write_bytes(b"unrelated bytes")
        for defect in ("checksum", "missing", "missing-field", "symlink", "directory",
                       "escape", "altered", "duplicate", "extra"):
            with self.subTest(defect=defect):
                delivery, inventory, _sha = self.write_delivery(name=f"font-{defect}")
                bundle = delivery / "fonts.tar"
                if defect == "checksum":
                    bundle.write_bytes(bundle.read_bytes() + b"changed")
                elif defect == "missing":
                    bundle.unlink()
                elif defect == "missing-field":
                    inventory.pop("font_bundle")
                else:
                    entries = list(FONT_FILES.items())
                    if defect == "duplicate":
                        entries.append(entries[0])
                    elif defect == "extra":
                        entries.append(("unlisted.otf", b"unlisted bytes"))
                    with tarfile.open(bundle, "w") as archive:
                        for index, (name, content) in enumerate(entries):
                            member = tarfile.TarInfo(name)
                            if index == 0:
                                if defect == "escape":
                                    member.name = str(outside)
                                elif defect == "symlink":
                                    member.type = tarfile.SYMTYPE
                                    member.linkname = "fixture-LICENSE.txt"
                                elif defect == "directory":
                                    member.type = tarfile.DIRTYPE
                                elif defect == "altered":
                                    content = bytes([content[0] ^ 1]) + content[1:]
                            member.size = len(content)
                            archive.addfile(member, io.BytesIO(content))
                    inventory["font_bundle"]["sha256"] = hashlib.sha256(bundle.read_bytes()).hexdigest()
                write_inventory(delivery / "release.json", inventory)
                installed = self.root / f"install-{defect}"
                for result in (
                    self.import_cmd(delivery, installed),
                    self.verify_cmd(delivery / "release.json", delivery=delivery),
                ):
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("font", result.stderr)
                self.assertFalse(installed.exists())
                self.assertFalse((self.state / "load-count").exists())
                self.assertEqual(ops(self.state), [])
                self.assertEqual(outside.read_bytes(), b"unrelated bytes")

    def test_build_exports_documentserver_as_unmodified_pulled_role(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        destination = self.root / "seven-role-release"
        result = self.build_cmd(ocu, webui, destination)
        self.assertEqual(result.returncode, 0, result.stderr)
        inventory = json.loads((destination / "release.json").read_text())
        self.assertEqual(
            set(inventory["images"]),
            {"workspace", "computer-use-server", "retention-guard", "proxy",
             "open-webui", "postgres", "documentserver"},
        )
        self.assertEqual(inventory["format_version"], 2)
        upstream = (
            "onlyoffice/documentserver@sha256:"
            "e3da62a847b9a5d51a11f73cfea1d9c13c3be3809614490d4edddcf01dcf919b"
        )
        commands = self.recorded_builds()
        pulled = [row for row in commands if row["tag"] == upstream]
        self.assertEqual(len(pulled), 1)
        self.assertEqual(pulled[0]["kind"], "pull")
        self.assertEqual(pulled[0]["platform"], "linux/amd64")
        self.assertFalse(any(
            row["kind"] == "build" and "documentserver" in row["tag"]
            for row in commands
        ))
        documentserver = inventory["images"]["documentserver"]
        self.assertEqual(documentserver["configuration_digest"], pulled[0]["id"])
        self.assertEqual(
            documentserver["archive"]["sha256"],
            hashlib.sha256(
                (destination / documentserver["archive"]["path"]).read_bytes()
            ).hexdigest(),
        )
        self.assertEqual(documentserver["build"]["dockerfile"], "deploy/release.py")
        self.assertEqual(
            documentserver["build"]["arguments"], {"DOCUMENTSERVER_IMAGE": upstream}
        )
        self.assertEqual(
            documentserver["build"]["argument_defaults"], {"DOCUMENTSERVER_IMAGE": upstream}
        )
        self.assertEqual(documentserver["build"]["argument_overrides"], {})
        self.assertEqual(
            documentserver["build"]["materials"],
            [{"name": "documentserver", "requested": upstream, "kind": "upstream-image"}],
        )

    def test_build_excludes_untracked_context_from_snapshot_inputs(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        self.add_untracked_canaries(ocu, webui)
        dest = self.root / "built-release"
        result = self.build_cmd(ocu, webui, dest)
        self.assertEqual(result.returncode, 0, result.stderr)
        builds = self.recorded_builds()
        self.assertTrue(builds)
        roles = {}
        for record in builds:
            if record.get("kind") != "build":
                continue
            tag = str(record.get("tag") or "")
            files = set(record.get("files") or [])
            self.assertTrue(all(".git" not in Path(name).parts for name in files), tag)
            if "ocu-build-workspace-" in tag:
                roles["workspace"] = record
                self.assertNotIn("secret.env", files)
                self.assertNotIn("untracked-workspace.env", files)
                self.assertNotIn("computer-use-server/untracked-server.env", files)
                self.assertNotIn(
                    "deploy/production-like-test/retention/untracked-retention.env",
                    files,
                )
                self.assertNotIn("deploy/proxy/untracked-proxy.env", files)
            elif "ocu-build-computer-use-server-" in tag:
                roles["computer-use-server"] = record
                self.assertNotIn("untracked-server.env", files)
            elif "ocu-build-retention-guard-" in tag:
                roles["retention-guard"] = record
                self.assertNotIn("untracked-retention.env", files)
            elif "ocu-build-proxy-" in tag:
                roles["proxy"] = record
                self.assertNotIn("untracked-proxy.env", files)
            elif "ocu-build-open-webui-" in tag:
                roles["open-webui"] = record
                self.assertNotIn("untracked-webui.env", files)
        self.assertEqual(
            set(roles),
            {
                "workspace",
                "computer-use-server",
                "retention-guard",
                "proxy",
                "open-webui",
            },
        )
        payload = json.loads((dest / "release.json").read_text(encoding="utf-8"))
        self.assertEqual(set(payload["images"]), set(ROLE_ORDER))
        self.assertIn("open-computer-use", payload["images"]["workspace"]["reference"])
        self.assertTrue((dest / "source.bundle").is_file())
        materials = payload["images"]["open-webui"]["build"]["materials"]
        self.assertEqual(
            [item["name"] for item in materials if item["kind"] == "pyodide-wheel"],
            [name for name, *_ in PYODIDE_WHEELS],
        )
        runtime = [item for item in materials if item["kind"] == "pyodide-runtime"]
        self.assertEqual(runtime[0]["requested"], "314.0.3")
        drawio = payload["images"]["computer-use-server"]["build"]["materials"][0]
        self.assertEqual(drawio["requested"], DRAWIO_COMMIT)
        self.assertEqual(drawio["archive_sha256"], DRAWIO_ARCHIVE_SHA256)
        self.assertEqual(
            payload["images"]["open-webui"]["build"]["arguments"]["BUILD_HASH"],
            payload["webui_source_sha"],
        )

    def test_missing_pyodide_supplement_rejects_publication_before_docker(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        (webui / "scripts" / "pyodide-supplement.json").unlink()
        subprocess.run(
            [
                "git",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-q",
                "-am",
                "remove supplement",
            ],
            cwd=str(webui),
            check=True,
            capture_output=True,
            text=True,
        )
        dest = self.root / "missing-supplement"
        result = self.build_cmd(ocu, webui, dest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pyodide-supplement.json", result.stderr)
        self.assertFalse(dest.exists())
        self.assertFalse((self.state / "builds.json").exists())
        self.assertFalse((self.state / "ops.log").exists())

    def test_malformed_pyodide_supplement_rejects_publication_before_docker(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        (webui / "scripts" / "pyodide-supplement.json").write_text(
            json.dumps(
                {
                    "packages": [
                        {
                            "name": "black",
                            "version": 26,
                            "file_name": "black.whl",
                            "url": "https://example.test/black.whl",
                            "sha256": "a" * 64,
                            "imports": ["black"],
                            "depends": [],
                        }
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        subprocess.run(
            [
                "git",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-q",
                "-am",
                "malform supplement",
            ],
            cwd=str(webui),
            check=True,
            capture_output=True,
            text=True,
        )
        dest = self.root / "malformed-supplement"
        result = self.build_cmd(ocu, webui, dest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Pyodide supplement", result.stderr)
        self.assertFalse(dest.exists())
        self.assertFalse((self.state / "builds.json").exists())
        self.assertFalse((self.state / "ops.log").exists())

    def test_malformed_drawio_inventory_rejects_publication_before_docker(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        (ocu / "computer-use-server" / "drawio" / "inventory.json").write_text(
            "{not-json\n", encoding="utf-8"
        )
        subprocess.run(
            [
                "git",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-q",
                "-am",
                "break drawio",
            ],
            cwd=str(ocu),
            check=True,
            capture_output=True,
            text=True,
        )
        dest = self.root / "bad-drawio"
        result = self.build_cmd(ocu, webui, dest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Draw.io", result.stderr)
        self.assertFalse(dest.exists())
        self.assertFalse((self.state / "builds.json").exists())
        self.assertFalse((self.state / "ops.log").exists())

    def test_changed_committed_manifest_changes_recorded_provenance(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        first = self.root / "first-release"
        first_result = self.build_cmd(ocu, webui, first)
        self.assertEqual(first_result.returncode, 0, first_result.stderr)
        first_payload = json.loads((first / "release.json").read_text(encoding="utf-8"))
        mutated = list(PYODIDE_WHEELS)
        name, version, file_name, digest = mutated[0]
        mutated[0] = (name, "99.0.0", file_name, digest)
        write_pyodide_supplement(webui / "scripts" / "pyodide-supplement.json", mutated)
        subprocess.run(
            [
                "git",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-q",
                "-am",
                "mutate supplement",
            ],
            cwd=str(webui),
            check=True,
            capture_output=True,
            text=True,
        )
        second = self.root / "second-release"
        second_result = self.build_cmd(ocu, webui, second)
        self.assertEqual(second_result.returncode, 0, second_result.stderr)
        second_payload = json.loads(
            (second / "release.json").read_text(encoding="utf-8")
        )
        first_webui = first_payload["images"]["open-webui"]["build"]
        second_webui = second_payload["images"]["open-webui"]["build"]
        self.assertNotEqual(
            first_webui["input_manifest_sha256"], second_webui["input_manifest_sha256"]
        )
        first_black = [
            item for item in first_webui["materials"] if item["name"] == "black"
        ][0]
        second_black = [
            item for item in second_webui["materials"] if item["name"] == "black"
        ][0]
        self.assertEqual(first_black["requested"], "26.5.1")
        self.assertEqual(second_black["requested"], "99.0.0")

    def test_same_tracked_inputs_keep_hash_across_checkout_locations(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        first = self.root / "hash-one"
        first_result = self.build_cmd(ocu, webui, first)
        self.assertEqual(first_result.returncode, 0, first_result.stderr)
        first_payload = json.loads((first / "release.json").read_text(encoding="utf-8"))
        ocu_clone = self.root / "cloned-ocu"
        webui_clone = self.root / "cloned-webui"
        subprocess.run(
            ["git", "clone", "--quiet", str(ocu), str(ocu_clone)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "clone", "--quiet", str(webui), str(webui_clone)],
            check=True,
            capture_output=True,
            text=True,
        )
        (ocu_clone / "local-untracked.env").write_text("ignore\n", encoding="utf-8")
        (webui_clone / "local-untracked.env").write_text("ignore\n", encoding="utf-8")
        second = self.root / "hash-two"
        second_result = self.build_cmd(ocu_clone, webui_clone, second)
        self.assertEqual(second_result.returncode, 0, second_result.stderr)
        second_payload = json.loads(
            (second / "release.json").read_text(encoding="utf-8")
        )
        for role in ROLE_ORDER:
            self.assertEqual(
                first_payload["images"][role]["build"]["input_manifest_sha256"],
                second_payload["images"][role]["build"]["input_manifest_sha256"],
                role,
            )

    def test_tiktoken_override_is_recorded_and_unknown_args_are_rejected(self):
        ocu, webui, _ocu_sha, webui_sha = self.write_committed_sources()
        dest = self.root / "tiktoken-release"
        result = self.build_cmd(
            ocu,
            webui,
            dest,
            extra=["--build-arg", "open-webui:USE_TIKTOKEN_ENCODING_NAME=p50k_base"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads((dest / "release.json").read_text(encoding="utf-8"))
        build = payload["images"]["open-webui"]["build"]
        self.assertEqual(build["arguments"]["USE_TIKTOKEN_ENCODING_NAME"], "p50k_base")
        self.assertEqual(
            build["argument_overrides"]["USE_TIKTOKEN_ENCODING_NAME"], "p50k_base"
        )
        self.assertEqual(
            build["argument_defaults"]["USE_TIKTOKEN_ENCODING_NAME"], "cl100k_base"
        )
        self.assertEqual(payload["webui_source_sha"], webui_sha)
        self.assertEqual(build["arguments"]["BUILD_HASH"], webui_sha)
        self.assertNotIn("BUILD_HASH", build["argument_overrides"])
        rejected = self.build_cmd(
            ocu,
            webui,
            self.root / "unknown-arg",
            extra=["--build-arg", "open-webui:NOT_A_REAL_ARG=1"],
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("unknown build argument", rejected.stderr)
        self.assertFalse((self.root / "unknown-arg").exists())

    def test_conflicting_webui_build_hash_is_rejected_before_docker(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        dest = self.root / "hash-conflict"
        result = self.build_cmd(
            ocu,
            webui,
            dest,
            extra=["--build-arg", "open-webui:BUILD_HASH=not-the-source-sha"],
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BUILD_HASH", result.stderr)
        self.assertFalse(dest.exists())
        self.assertFalse((self.state / "builds.json").exists())
        self.assertFalse((self.state / "ops.log").exists())

    def test_explicit_equal_override_is_recorded_separately_from_defaults(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        dest = self.root / "equal-override"
        result = self.build_cmd(
            ocu,
            webui,
            dest,
            extra=["--build-arg", "workspace:CLAUDE_CODE_VERSION=2.1.112"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads((dest / "release.json").read_text(encoding="utf-8"))
        build = payload["images"]["workspace"]["build"]
        self.assertEqual(build["argument_defaults"]["CLAUDE_CODE_VERSION"], "2.1.112")
        self.assertEqual(build["argument_overrides"]["CLAUDE_CODE_VERSION"], "2.1.112")
        self.assertEqual(build["arguments"]["CLAUDE_CODE_VERSION"], "2.1.112")

    def test_missing_pyodide_lock_rejects_publication_before_docker(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        (webui / "package-lock.json").unlink()
        subprocess.run(
            [
                "git",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-q",
                "-am",
                "remove pyodide lock",
            ],
            cwd=str(webui),
            check=True,
            capture_output=True,
            text=True,
        )
        dest = self.root / "missing-lock"
        result = self.build_cmd(ocu, webui, dest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("package-lock.json", result.stderr)
        self.assertFalse(dest.exists())
        self.assertFalse((self.state / "builds.json").exists())
        self.assertFalse((self.state / "ops.log").exists())

    def test_missing_drawio_prepare_rejects_publication_before_docker(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        (ocu / "computer-use-server" / "drawio" / "prepare_drawio.py").unlink()
        subprocess.run(
            [
                "git",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-q",
                "-am",
                "remove drawio pin",
            ],
            cwd=str(ocu),
            check=True,
            capture_output=True,
            text=True,
        )
        dest = self.root / "missing-drawio"
        result = self.build_cmd(ocu, webui, dest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("prepare_drawio.py", result.stderr)
        self.assertFalse(dest.exists())
        self.assertFalse((self.state / "builds.json").exists())
        self.assertFalse((self.state / "ops.log").exists())

    def test_secret_build_argument_on_cli_rejects_before_docker(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        dest = self.root / "secret-arg"
        result = self.build_cmd(
            ocu,
            webui,
            dest,
            extra=["--build-arg", "workspace:NPM_TOKEN=secret"],
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("secret build argument", result.stderr)
        self.assertFalse(dest.exists())
        self.assertFalse((self.state / "builds.json").exists())
        self.assertFalse((self.state / "ops.log").exists())

    def test_changed_drawio_inventory_changes_recorded_provenance(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        first = self.root / "drawio-first"
        first_result = self.build_cmd(ocu, webui, first)
        self.assertEqual(first_result.returncode, 0, first_result.stderr)
        first_payload = json.loads((first / "release.json").read_text(encoding="utf-8"))
        inventory = ocu / "computer-use-server" / "drawio" / "inventory.json"
        payload = json.loads(inventory.read_text(encoding="utf-8"))
        payload["files"][0]["sha256"] = "b" * 64
        inventory.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        subprocess.run(
            [
                "git",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-q",
                "-am",
                "mutate drawio inventory",
            ],
            cwd=str(ocu),
            check=True,
            capture_output=True,
            text=True,
        )
        second = self.root / "drawio-second"
        second_result = self.build_cmd(ocu, webui, second)
        self.assertEqual(second_result.returncode, 0, second_result.stderr)
        second_payload = json.loads(
            (second / "release.json").read_text(encoding="utf-8")
        )
        first_drawio = first_payload["images"]["computer-use-server"]["build"]
        second_drawio = second_payload["images"]["computer-use-server"]["build"]
        self.assertNotEqual(
            first_drawio["input_manifest_sha256"],
            second_drawio["input_manifest_sha256"],
        )
        self.assertNotEqual(
            first_drawio["materials"][0]["inventory_sha256"],
            second_drawio["materials"][0]["inventory_sha256"],
        )

    def test_malformed_pyodide_lock_rejects_publication_before_docker(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        (webui / "package-lock.json").write_text("{not-json\n", encoding="utf-8")
        subprocess.run(
            [
                "git",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-q",
                "-am",
                "break pyodide lock",
            ],
            cwd=str(webui),
            check=True,
            capture_output=True,
            text=True,
        )
        dest = self.root / "bad-lock"
        result = self.build_cmd(ocu, webui, dest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Pyodide package lock", result.stderr)
        self.assertFalse(dest.exists())
        self.assertFalse((self.state / "builds.json").exists())
        self.assertFalse((self.state / "ops.log").exists())

    def test_missing_webui_build_hash_declaration_rejects_before_docker(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        (webui / "Dockerfile").write_text(
            'FROM scratch\nARG USE_TIKTOKEN_ENCODING_NAME="cl100k_base"\n',
            encoding="utf-8",
        )
        subprocess.run(
            [
                "git",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-q",
                "-am",
                "drop BUILD_HASH",
            ],
            cwd=str(webui),
            check=True,
            capture_output=True,
            text=True,
        )
        dest = self.root / "missing-build-hash"
        result = self.build_cmd(ocu, webui, dest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BUILD_HASH", result.stderr)
        self.assertFalse(dest.exists())
        self.assertFalse((self.state / "builds.json").exists())
        self.assertFalse((self.state / "ops.log").exists())

    def test_explicit_matching_build_hash_is_recorded_as_override(self):
        ocu, webui, _ocu_sha, webui_sha = self.write_committed_sources()
        dest = self.root / "matching-hash"
        result = self.build_cmd(
            ocu,
            webui,
            dest,
            extra=["--build-arg", f"open-webui:BUILD_HASH={webui_sha}"],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads((dest / "release.json").read_text(encoding="utf-8"))
        build = payload["images"]["open-webui"]["build"]
        self.assertEqual(payload["webui_source_sha"], webui_sha)
        self.assertEqual(build["arguments"]["BUILD_HASH"], webui_sha)
        self.assertEqual(build["argument_overrides"]["BUILD_HASH"], webui_sha)
        self.assertEqual(build["argument_defaults"]["BUILD_HASH"], "dev-build")

    def test_omitted_and_explicit_postgres_image_are_recorded_separately(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        omitted = self.root / "postgres-omitted"
        omitted_result = self.build_cmd(ocu, webui, omitted)
        self.assertEqual(omitted_result.returncode, 0, omitted_result.stderr)
        omitted_payload = json.loads(
            (omitted / "release.json").read_text(encoding="utf-8")
        )
        omitted_build = omitted_payload["images"]["postgres"]["build"]
        self.assertEqual(
            omitted_build["arguments"]["POSTGRES_IMAGE"], "postgres:17-alpine"
        )
        self.assertEqual(
            omitted_build["argument_defaults"]["POSTGRES_IMAGE"], "postgres:17-alpine"
        )
        self.assertEqual(omitted_build["argument_overrides"], {})
        explicit = self.root / "postgres-explicit"
        explicit_result = self.build_cmd(
            ocu,
            webui,
            explicit,
            extra=["--postgres-image", "postgres:17-alpine"],
        )
        self.assertEqual(explicit_result.returncode, 0, explicit_result.stderr)
        explicit_payload = json.loads(
            (explicit / "release.json").read_text(encoding="utf-8")
        )
        explicit_build = explicit_payload["images"]["postgres"]["build"]
        self.assertEqual(
            explicit_build["arguments"]["POSTGRES_IMAGE"], "postgres:17-alpine"
        )
        self.assertEqual(
            explicit_build["argument_overrides"]["POSTGRES_IMAGE"], "postgres:17-alpine"
        )

    def test_unknown_argument_rejects_before_any_additional_docker(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        dest = self.root / "unknown-only"
        result = self.build_cmd(
            ocu,
            webui,
            dest,
            extra=["--build-arg", "proxy:NOT_A_REAL_ARG=1"],
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown build argument", result.stderr)
        self.assertFalse(dest.exists())
        self.assertFalse((self.state / "builds.json").exists())
        self.assertFalse((self.state / "ops.log").exists())

    def test_declared_reference_conflict_rejects_before_load(self):
        delivery, inventory, _sha = self.write_delivery()
        tag = inventory["images"]["workspace"]["reference"]
        original = digest_for("present-workspace")
        seed_images(
            self.state,
            {
                tag: {
                    "Id": original,
                    "Os": "linux",
                    "Architecture": "amd64",
                    "ConfigBytes": config_payload(original).decode("utf-8"),
                }
            },
        )
        before = (self.state / "images.json").read_text(encoding="utf-8")
        install = self.root / "install-declared-conflict"
        result = self.import_cmd(delivery, install)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("conflicts", result.stderr)
        self.assertFalse(install.exists())
        self.assertFalse((self.state / "load-count").exists())
        self.assertEqual((self.state / "images.json").read_text(encoding="utf-8"), before)

    def test_declared_conflict_bypass_mutant_overwrites_existing_mapping(self):
        delivery, inventory, _sha = self.write_delivery()
        tag = inventory["images"]["workspace"]["reference"]
        original = digest_for("present-workspace")
        seed_images(
            self.state,
            {
                tag: {
                    "Id": original,
                    "Os": "linux",
                    "Architecture": "amd64",
                    "ConfigBytes": config_payload(original).decode("utf-8"),
                }
            },
        )
        before = (self.state / "images.json").read_text(encoding="utf-8")
        script = self.patched_release(
            (
                "        if image_id != info[\"digest\"]:\n"
                "            raise ReleaseError(f\"existing image {reference} conflicts with the release\")\n",
                "        if False and image_id != info[\"digest\"]:\n"
                "            raise ReleaseError(f\"existing image {reference} conflicts with the release\")\n",
            )
        )
        result = self.import_with(script, delivery, self.root / "install-bypass-conflict")
        self.assertEqual(result.returncode, 0, result.stderr)
        after = json.loads((self.state / "images.json").read_text(encoding="utf-8"))
        self.assertNotEqual((self.state / "images.json").read_text(encoding="utf-8"), before)
        self.assertEqual(
            after[tag]["Id"], inventory["images"]["workspace"]["configuration_digest"]
        )

    def test_cross_destination_conflicting_imports_serialize(self):
        first_delivery, first_inventory, _sha = self.write_delivery(name="delivery-a")
        second_delivery, second_inventory, _sha2 = self.write_delivery(name="delivery-b")
        tag = first_inventory["images"]["workspace"]["reference"]
        other_config = config_payload(digest_for("second-workspace"))
        archive = second_delivery / second_inventory["images"]["workspace"]["archive"]["path"]
        write_image_archive(archive, {tag: other_config})
        second_inventory["images"]["workspace"]["configuration_digest"] = image_id_for_config(
            other_config
        )
        second_inventory["images"]["workspace"]["archive"]["sha256"] = hashlib.sha256(
            archive.read_bytes()
        ).hexdigest()
        write_inventory(second_delivery / "release.json", second_inventory)
        hold = self.root / "hold-first-load"
        hold.touch()
        first = self.root / "install-first"
        second = self.root / "install-second"
        ready = self.root / "second-store-ready"
        needle = "        imported = verify_archive_set(stage, staged_payload)\n"
        source = self.release_script.read_text(encoding="utf-8")
        self.assertIn(needle, source)
        self._script_serial = getattr(self, "_script_serial", 0) + 1
        script_b = self.root / f"isolated-release-{self._script_serial}.py"
        script_b.write_text(
            source.replace(
                needle,
                needle + f"        Path({str(ready)!r}).write_text('1')\n",
                1,
            ),
            encoding="utf-8",
        )
        script_b.chmod(0o755)
        env_a = {**self.env, "FAKE_DOCKER_HOLD_LOAD": str(hold)}
        process_a = subprocess.Popen(
            [
                sys.executable,
                str(self.release_script),
                "import",
                "--delivery",
                str(first_delivery),
                "--install-root",
                str(first),
            ],
            env=env_a,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        process_b = None
        try:
            deadline = time.monotonic() + 10
            while not (self.state / "entered-load").exists():
                self.assertIsNone(process_a.poll(), "first import exited before load")
                self.assertLess(time.monotonic(), deadline, "first import did not reach load")
                time.sleep(0.02)
            process_b = subprocess.Popen(
                [
                    sys.executable,
                    str(script_b),
                    "import",
                    "--delivery",
                    str(second_delivery),
                    "--install-root",
                    str(second),
                ],
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            deadline = time.monotonic() + 10
            while not ready.exists():
                self.assertIsNone(
                    process_a.poll(), "first import exited before second was store-ready"
                )
                self.assertIsNone(process_b.poll(), "second import exited before store-ready")
                self.assertLess(
                    time.monotonic(), deadline, "second import did not reach store-ready"
                )
                time.sleep(0.02)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if (second / "release.json").is_file() or (self.state / "load-count").exists():
                    break
                if process_b.poll() is not None:
                    break
                time.sleep(0.02)
            if (self.state / "load-count").exists() and not (second / "release.json").is_file():
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not (second / "release.json").is_file():
                    if process_b.poll() is not None:
                        break
                    time.sleep(0.02)
            self.assertFalse(
                (second / "release.json").is_file(),
                "second import published while first held the store",
            )
            self.assertFalse(
                (self.state / "load-count").exists(),
                "second import loaded while first held the store",
            )
            self.assertIsNone(
                process_b.poll(), "second import finished while first held the store"
            )
            self.assertIsNone(process_a.poll(), "first import exited while still held")
            hold.unlink()
            stdout_a, stderr_a = process_a.communicate(timeout=20)
            self.assertEqual(process_a.returncode, 0, stdout_a + stderr_a)
            stdout_b, stderr_b = process_b.communicate(timeout=20)
            self.assertNotEqual(process_b.returncode, 0, stdout_b + stderr_b)
            self.assertFalse(second.exists())
            self.assertTrue((first / "release.json").is_file())
            images = json.loads((self.state / "images.json").read_text(encoding="utf-8"))
            self.assertEqual(
                images[tag]["Id"],
                first_inventory["images"]["workspace"]["configuration_digest"],
            )
        finally:
            hold.unlink(missing_ok=True)
            for process in (process_a, process_b):
                if process is None:
                    continue
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                process.wait()

    def test_canonical_hybrid_archive_imports(self):
        delivery, inventory, _sha = self.write_hybrid_delivery(nested_index=True, attestation={})
        install = self.root / "install-hybrid"
        result = self.import_cmd(delivery, install)
        self.assertEqual(result.returncode, 0, result.stderr)
        images = json.loads((self.state / "images.json").read_text(encoding="utf-8"))
        for role, record in inventory["images"].items():
            self.assertEqual(images[record["reference"]]["Id"], record["configuration_digest"], role)

    def test_hidden_oci_alias_is_rejected_before_load(self):
        extra = "unrelated.example/app:keep"
        seed_images(
            self.state,
            {
                extra: {
                    "Id": digest_for(extra),
                    "Os": "linux",
                    "Architecture": "amd64",
                    "ConfigBytes": config_payload(digest_for(extra)).decode("utf-8"),
                }
            },
        )
        before = (self.state / "images.json").read_text(encoding="utf-8")
        delivery, _inventory, _sha = self.write_hybrid_delivery(extra_oci_names=(extra,))
        result = self.import_cmd(delivery, self.root / "install-hidden-alias")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "install-hidden-alias").exists())
        self.assertFalse((self.state / "load-count").exists())
        self.assertEqual((self.state / "images.json").read_text(encoding="utf-8"), before)

    def test_hybrid_agreement_bypass_mutant_loads_hidden_alias(self):
        extra = "unrelated.example/app:keep"
        original = digest_for("keep-original")
        seed_images(
            self.state,
            {
                extra: {
                    "Id": original,
                    "Os": "linux",
                    "Architecture": "amd64",
                    "ConfigBytes": config_payload(original).decode("utf-8"),
                }
            },
        )
        delivery, _inventory, _sha = self.write_hybrid_delivery(extra_oci_names=(extra,))
        script = self.patched_release(
            (
                "            oci_map = parse_oci_layout(archive, bundle, names)\n"
                "            return agree_reference_maps(docker_map, oci_map, archive)\n",
                "            oci_map = parse_oci_layout(archive, bundle, names)\n"
                "            return docker_map\n",
            )
        )
        result = self.import_with(script, delivery, self.root / "install-hybrid-bypass")
        self.assertEqual(result.returncode, 0, result.stderr)
        images = json.loads((self.state / "images.json").read_text(encoding="utf-8"))
        self.assertNotEqual(images[extra]["Id"], original)

    def test_allocation_faults_release_reservation(self):
        delivery, _inventory, _sha = self.write_delivery()
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        stage_fail = self.patched_release(
            (
                "def allocate_private_dir(*, prefix: str, parent: Path) -> Path:\n",
                (
                    "def allocate_private_dir(*, prefix: str, parent: Path) -> Path:\n"
                    "    if prefix.endswith(\".stage-\"):\n"
                    "        raise OSError(28, \"injected stage allocation failure\")\n"
                ),
            )
        )
        snapshot_fail = self.patched_release(
            (
                "def allocate_private_dir(*, prefix: str, parent: Path) -> Path:\n",
                (
                    "def allocate_private_dir(*, prefix: str, parent: Path) -> Path:\n"
                    "    if prefix == \"ocu-release-src-\":\n"
                    "        raise OSError(28, \"injected snapshot allocation failure\")\n"
                ),
            )
        )
        import_dest = self.root / "install-alloc"
        failed_import = self.import_with(stage_fail, delivery, import_dest)
        self.assertNotEqual(failed_import.returncode, 0)
        self.assertFalse(import_dest.exists())
        self.assertFalse(list(self.root.glob("install-alloc.publish.lock")))
        retry = self.import_cmd(delivery, import_dest)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        failed_build = self.build_with(
            snapshot_fail, ocu, webui, self.root / "build-alloc"
        )
        self.assertNotEqual(failed_build.returncode, 0)
        self.assertFalse((self.root / "build-alloc").exists())
        self.assertFalse(list(self.root.glob("build-alloc.publish.lock")))


    def test_cancellation_at_allocation_boundary_releases_reservation(self):
        delivery, _inventory, _sha = self.write_delivery()
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        pending = (
            "        path = Path(tempfile.mkdtemp(prefix=prefix, dir=str(parent)))\n"
            "        os.kill(os.getpid(), signal.SIGTERM)\n"
        )
        stage_script = self.patched_release(
            (
                "        path = Path(tempfile.mkdtemp(prefix=prefix, dir=str(parent)))\n",
                pending,
            )
        )
        snapshot_script = self.patched_release(
            (
                "def allocate_private_dir(*, prefix: str, parent: Path) -> Path:\n",
                (
                    "def allocate_private_dir(*, prefix: str, parent: Path) -> Path:\n"
                    "    if prefix == \"ocu-release-src-\":\n"
                    "        path = Path(tempfile.mkdtemp(prefix=prefix, dir=str(parent)))\n"
                    "        os.kill(os.getpid(), signal.SIGTERM)\n"
                    "        try:\n"
                    "            os.chmod(path, 0o700)\n"
                    "        except OSError as cop:\n"
                    "            shutil.rmtree(path, ignore_errors=True)\n"
                    "            raise ReleaseError(f\"cannot protect private directory {path}: {cop}\") from cop\n"
                    "        return path\n"
                ),
            )
        )
        verify_script = self.patched_release(
            (
                "def allocate_private_dir(*, prefix: str, parent: Path) -> Path:\n",
                (
                    "def allocate_private_dir(*, prefix: str, parent: Path) -> Path:\n"
                    "    if prefix == \"ocu-source-verify-\":\n"
                    "        path = Path(tempfile.mkdtemp(prefix=prefix, dir=str(parent)))\n"
                    "        os.kill(os.getpid(), signal.SIGTERM)\n"
                    "        try:\n"
                    "            os.chmod(path, 0o700)\n"
                    "        except OSError as cop:\n"
                    "            shutil.rmtree(path, ignore_errors=True)\n"
                    "            raise ReleaseError(f\"cannot protect private directory {path}: {cop}\") from cop\n"
                    "        return path\n"
                ),
            )
        )
        lock_script = self.patched_release(
            (
                "            self.lock_path = acquire_exclusive(self.dest)\n"
                "            self._owns_lock = True\n",
                "            self.lock_path = acquire_exclusive(self.dest)\n"
                "            self._owns_lock = True\n"
                "            os.kill(os.getpid(), signal.SIGTERM)\n",
            )
        )

        import_dest = self.root / "install-alloc-cancel"
        failed_import = self.import_with(stage_script, delivery, import_dest)
        self.assertEqual(failed_import.returncode, 128 + signal.SIGTERM, failed_import.stderr)
        self.assertFalse(import_dest.exists())
        self.assertFalse(list(self.root.glob("install-alloc-cancel.publish.lock")))
        self.assertFalse(list(self.root.glob("install-alloc-cancel.stage-*")))
        retry = self.import_cmd(delivery, import_dest)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        failed_build = self.build_with(
            snapshot_script, ocu, webui, self.root / "build-alloc-cancel"
        )
        self.assertEqual(failed_build.returncode, 128 + signal.SIGTERM, failed_build.stderr)
        self.assertFalse((self.root / "build-alloc-cancel").exists())
        self.assertFalse(list(self.root.glob("build-alloc-cancel.publish.lock")))
        self.assertFalse(list(self.root.glob("ocu-release-src-*")))
        verify_result = subprocess.run(
            [
                "python3",
                str(verify_script),
                "verify",
                "--inventory",
                str(delivery / "release.json"),
                "--mode",
                "delivery",
                "--delivery",
                str(delivery),
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env=self.env,
            check=False,
            timeout=20,
        )
        self.assertEqual(verify_result.returncode, 128 + signal.SIGTERM, verify_result.stderr)
        self.assertFalse(list(self.root.glob("ocu-source-verify-*")))
        lock_dest = self.root / "install-lock-cancel"
        failed_lock = self.import_with(lock_script, delivery, lock_dest)
        self.assertEqual(failed_lock.returncode, 128 + signal.SIGTERM, failed_lock.stderr)
        self.assertFalse(lock_dest.exists())
        self.assertFalse(list(self.root.glob("install-lock-cancel.publish.lock")))

    def test_rival_cleanup_does_not_delete_owner_stage(self):
        destination = self.root / "release"
        owner = release.PublicationSession(destination)
        contender = release.PublicationSession(destination)
        owner.acquire()
        stage = owner.allocate_stage()
        sentinel = stage / "owned-by-first-writer"
        sentinel.write_bytes(b"keep active writer bytes")
        try:
            with self.assertRaises(release.ReleaseError):
                contender.acquire()
            contender.cleanup()
            self.assertTrue(sentinel.is_file())
            self.assertEqual(sentinel.read_bytes(), b"keep active writer bytes")
            self.assertTrue(owner.lock_path.is_file())
            self.assertEqual(owner.stage, stage)
        finally:
            owner.cleanup()

    def test_partial_load_failure_discloses_retained_cache(self):
        delivery, inventory, _sha = self.write_delivery()
        (self.state / "load-fail-after").write_text("1", encoding="utf-8")
        result = self.import_cmd(delivery, self.root / "install-partial")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("image cache entries may remain", result.stderr)
        self.assertFalse((self.root / "install-partial").exists())
        images = json.loads((self.state / "images.json").read_text(encoding="utf-8"))
        workspace = inventory["images"]["workspace"]["reference"]
        self.assertEqual(
            images[workspace]["Id"],
            inventory["images"]["workspace"]["configuration_digest"],
        )
        self.assertNotIn("rmi", "\n".join(ops(self.state)))

    def test_delivery_verify_requires_source_bundle(self):
        delivery, inventory, _sha = self.write_delivery()
        inventory_path = delivery / "release.json"
        missing = self.verify_cmd(inventory_path, mode="delivery")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("requires --delivery", missing.stderr)
        intact = self.verify_cmd(inventory_path, mode="delivery", delivery=delivery)
        self.assertEqual(intact.returncode, 0, intact.stderr)
        bundle = delivery / inventory["source_bundle"]["path"]
        bundle.unlink()
        absent = self.verify_cmd(inventory_path, mode="delivery", delivery=delivery)
        self.assertNotEqual(absent.returncode, 0)
        bundle.write_bytes(b"not-a-git-bundle")
        corrupt = self.verify_cmd(inventory_path, mode="delivery", delivery=delivery)
        self.assertNotEqual(corrupt.returncode, 0)

    def test_incompatible_source_bundle_is_rejected_before_load(self):
        delivery, _inventory, _sha = self.write_incompatible_delivery()
        result = self.import_cmd(delivery, self.root / "install-legacy")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("consumer contract", result.stderr)
        self.assertFalse((self.root / "install-legacy").exists())
        self.assertFalse((self.state / "load-count").exists())

    def test_imported_checkout_startup_uses_frozen_no_build(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        dest = self.root / "compat-release"
        built = self.build_cmd(ocu, webui, dest)
        self.assertEqual(built.returncode, 0, built.stderr)
        install = self.root / "compat-install"
        imported = self.import_cmd(dest, install)
        self.assertEqual(imported.returncode, 0, imported.stderr)
        payload = json.loads((install / "release.json").read_text(encoding="utf-8"))
        write_fake_configs(self.state, intended_docs_for_images(payload["images"]))
        seed_healthy_host(self.state)
        write_network(
            self.state, "ocu-test-private", subnet="172.30.0.0/24", gateway="172.30.0.1"
        )
        write_network(
            self.state, "ocu-sandbox", subnet="172.31.0.0/24", gateway="172.31.0.1"
        )
        self.assertEqual(payload["source_consumer_contract"], SOURCE_CONSUMER_CONTRACT)
        loaded = json.loads((self.state / "images.json").read_text(encoding="utf-8"))
        for role, record in payload["images"].items():
            self.assertEqual(loaded[record["reference"]]["Id"], record["configuration_digest"], role)
        env = fake_env(self.state)
        # The copied bootstrap must not create an ambient operator directory.
        env.pop("OCU_OFFICE_PROXY_PORT", None)
        env.pop("OCU_OFFICE_FONTS_DIR", None)
        env["OCU_RELEASE_MANIFEST"] = str(install / "release.json")
        env["OCU_TEST_ROOT"] = str(install / "source")
        env["SOURCE_SHA"] = payload["ocu_source_sha"]
        env["WEBUI_SOURCE_SHA"] = payload["webui_source_sha"]
        for role, name in (
            ("workspace", "DOCKER_IMAGE"),
            ("computer-use-server", "COMPUTER_USE_SERVER_IMAGE"),
            ("retention-guard", "RETENTION_GUARD_IMAGE"),
            ("proxy", "OCU_PROXY_IMAGE"),
            ("open-webui", "OPENWEBUI_IMAGE"),
            ("postgres", "POSTGRES_IMAGE"),
            ("documentserver", "DOCUMENTSERVER_IMAGE"),
        ):
            env[name] = payload["images"][role]["reference"]
        result = run_script(install / "source" / "deploy" / "up.sh", env)
        self.assertEqual(result.returncode, 0, result.stderr)
        executed = (self.state / "executed.json").read_text(encoding="utf-8") if (self.state / "executed.json").exists() else ""
        self.assertIn("--no-build", executed)
        self.assertIn("never", executed)
        self.assertTrue((self.state / "starts.log").exists())
        self.assertEqual(
            (self.state / "starts.log").read_text(encoding="utf-8").splitlines(),
            ["core", "webui", "proxy"],
        )
        (self.root / "dmxapi.env").write_text(
            "DMXAPI_API_KEY=provider-secret-not-for-logs\n", encoding="utf-8"
        )
        (self.root / "dmxapi.env").chmod(0o600)
        bootstrap_root = self.root / "bootstrap-root"
        (bootstrap_root / "source").mkdir(parents=True)
        subprocess.run(
            ["cp", "-a", str(install / "source") + "/.", str(bootstrap_root / "source")],
            check=True,
            capture_output=True,
            text=True,
        )
        bootstrap = subprocess.run(
            [
                "bash",
                str(
                    bootstrap_root
                    / "source"
                    / "deploy"
                    / "production-like-test"
                    / "scripts"
                    / "bootstrap-test.sh"
                ),
            ],
            cwd=str(bootstrap_root / "source"),
            capture_output=True,
            text=True,
            env={
                **env,
                "DEPLOY_ROOT": str(bootstrap_root),
                "DMX_ENV_FILE": str(self.root / "dmxapi.env"),
                "OCU_ADMIN_CREDENTIALS_FILE": str(self.root / "admin-credentials.txt"),
                "FAKE_ID_UID": "0",
                "OCU_WEBUI_ORIGIN": "https://workbench.example.test",
                "OCU_OFFICE_DOCSERVER_ORIGIN": "https://workbench.example.test:8083",
                "ENABLE_OCU_OFFICE_EDIT": "false",
            },
            check=False,
            timeout=20,
        )
        self.assertEqual(bootstrap.returncode, 0, bootstrap.stderr)
        self.assertTrue((bootstrap_root / "config" / "runtime.env").exists())

    def test_dirty_tracked_ocu_source_rejects_before_docker(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        (ocu / "deploy" / "up.sh").write_text(
            (ocu / "deploy" / "up.sh").read_text(encoding="utf-8") + "# dirty\n",
            encoding="utf-8",
        )
        dest = self.root / "dirty-ocu"
        result = self.build_cmd(ocu, webui, dest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tracked source is dirty", result.stderr)
        self.assertFalse(dest.exists())
        self.assertFalse((self.state / "builds.json").exists())
        self.assertFalse((self.state / "ops.log").exists())

    def test_dirty_tracked_webui_source_rejects_before_docker(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        (webui / "Dockerfile").write_text(
            (webui / "Dockerfile").read_text(encoding="utf-8") + "# dirty\n",
            encoding="utf-8",
        )
        dest = self.root / "dirty-webui"
        result = self.build_cmd(ocu, webui, dest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tracked source is dirty", result.stderr)
        self.assertFalse(dest.exists())
        self.assertFalse((self.state / "builds.json").exists())
        self.assertFalse((self.state / "ops.log").exists())

    def test_clean_head_bypass_mutant_publishes_from_dirty_tree(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        (ocu / "deploy" / "up.sh").write_text(
            (ocu / "deploy" / "up.sh").read_text(encoding="utf-8") + "# dirty\n",
            encoding="utf-8",
        )
        script = self.patched_release(
            (
                "    if dirty:\n"
                "        raise ReleaseError(\n"
                "            f\"{cwd}: tracked source is dirty; build from a committed snapshot\"\n"
                "        )\n",
                "    if False and dirty:\n"
                "        raise ReleaseError(\n"
                "            f\"{cwd}: tracked source is dirty; build from a committed snapshot\"\n"
                "        )\n",
            )
        )
        dest = self.root / "dirty-bypass"
        result = self.build_with(script, ocu, webui, dest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((dest / "release.json").is_file())

    def test_replaced_image_fails_startup_before_mutation(self):
        env, script, _source = prepare_up_context(self.state)
        write_fake_configs(self.state)
        seed_healthy_host(self.state)
        write_network(
            self.state, "ocu-test-private", subnet="172.30.0.0/24", gateway="172.30.0.1"
        )
        original_images = (self.state / "images.json").read_text()
        for role in ("proxy", "documentserver"):
            with self.subTest(role=role):
                images = json.loads(original_images)
                images[DEFAULT_RELEASE_IMAGES[role]]["Id"] = digest_for(f"replaced-{role}")
                (self.state / "images.json").write_text(json.dumps(images))
                result = run_script(script, env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("configuration digest", result.stderr)
                self.assertFalse((self.state / "starts.log").exists())
                recorded = "\n".join(ops(self.state))
                self.assertNotIn("network create", recorded)
                self.assertNotIn("compose up", recorded)
                self.assertFalse((self.state / "builds.json").exists())

    def test_image_equality_bypass_mutant_starts_replaced_image(self):
        env, _script, source = prepare_up_context(self.state)
        write_fake_configs(self.state)
        seed_healthy_host(self.state)
        write_network(
            self.state, "ocu-test-private", subnet="172.30.0.0/24", gateway="172.30.0.1"
        )
        write_network(
            self.state, "ocu-sandbox", subnet="172.31.0.0/24", gateway="172.31.0.1"
        )
        images = json.loads((self.state / "images.json").read_text(encoding="utf-8"))
        tag = DEFAULT_RELEASE_IMAGES["proxy"]
        images[tag]["Id"] = digest_for("replaced-proxy")
        (self.state / "images.json").write_text(json.dumps(images), encoding="utf-8")
        script = self.patched_release(
            (
                "        if image_id != record[\"configuration_digest\"]:\n"
                "            raise ReleaseError(\n"
                "                f\"{role} image {record['reference']} has configuration digest {image_id}, expected {record['configuration_digest']}\"\n"
                "            )\n",
                "        if False and image_id != record[\"configuration_digest\"]:\n"
                "            raise ReleaseError(\n"
                "                f\"{role} image {record['reference']} has configuration digest {image_id}, expected {record['configuration_digest']}\"\n"
                "            )\n",
            )
        )
        mutant_source = source / "deploy" / "release.py"
        mutant_source.write_text(script.read_text(encoding="utf-8"), encoding="utf-8")
        subprocess.run(
            ["git", "add", "deploy/release.py"],
            cwd=str(source),
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-q",
                "-m",
                "bypass image equality",
            ],
            cwd=str(source),
            check=True,
            capture_output=True,
            text=True,
        )
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(source), text=True
        ).strip()
        inventory = write_release_for_sha(self.state / "release.json", sha, WEBUI_SYNTHETIC_SHA)
        env["OCU_RELEASE_MANIFEST"] = str(inventory)
        env["SOURCE_SHA"] = sha
        result = run_script(source / "deploy" / "up.sh", env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.state / "starts.log").exists())


if __name__ == "__main__":
    unittest.main()
