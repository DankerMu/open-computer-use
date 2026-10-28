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
    import_summary,
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


# The import free-space precheck: its headroom and the two probes tests substitute.
SPACE_HEADROOM = 1 << 30
SPACE_FREE_PROBE = "        available = shutil.disk_usage(path).free\n"
SPACE_DEVICE_PROBE = "    if os.stat(stage_root).st_dev == root_device:\n"


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

    def write_delivery(self, *, extra_tag=None, mutate=None, skip_role=None, name="delivery", contract=SOURCE_CONSUMER_CONTRACT):
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
        consumer = ocu / "deploy/release.py"
        consumer.write_text(consumer.read_text().replace(
            f"SOURCE_CONSUMER_CONTRACT = {SOURCE_CONSUMER_CONTRACT}",
            f"SOURCE_CONSUMER_CONTRACT = {contract}", 1,
        ))
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
        payload["source_consumer_contract"] = contract
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
                nested_index=nested_index,
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
            "deploy/documentserver/Dockerfile",
            "deploy/documentserver/patch.py",
            "deploy/documentserver/patch-inventory.json",
            "deploy/documentserver/OCU-MODIFICATIONS",
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
            "documentserver": ocu / "deploy/documentserver/untracked-documentserver.env",
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

    def test_export_uses_archive_configuration_not_engine_manifest_identity(self):
        config = config_payload(digest_for("producer-configuration"))
        upstream = "postgres:identity-fixture"
        fixture = write_hybrid_image_archive(
            self.root / "producer.tar", reference=upstream, config=config,
        )
        with tarfile.open(fixture) as archive:
            index = json.load(archive.extractfile("index.json"))
        manifest_digest = index["manifests"][0]["digest"]
        seed_images(self.state, {upstream: {
            "Id": manifest_digest, "Os": "linux", "Architecture": "amd64",
            "ConfigBytes": config.decode(), "HybridArchive": True,
        }})
        result = self.export_plan(self.pull_plan(upstream))
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        expected_config = image_id_for_config(config)
        self.assertEqual(record["configuration_digest"], expected_config)
        self.assertEqual(record["reference"], "ocu-postgres:" + expected_config[7:])
        self.assertIn(manifest_digest, record["descriptor_digests"])
        final = release.inspect_archive_bytes(self.root / "exports/postgres.tar")
        self.assertEqual(final[record["reference"]]["digest"], expected_config)

    def pull_plan(self, reference):
        dockerfile = self.root / "Dockerfile"
        dockerfile.write_text("FROM scratch\n")
        return {
            "temp_tag": reference, "image_kind": "pull",
            "dockerfile": str(dockerfile), "source_root": str(self.root),
            "context": str(self.root), "arguments": {}, "argument_defaults": {},
            "argument_overrides": {}, "input_manifest_sha256": "a" * 64,
            "materials": [],
        }

    def export_plan(self, plan, *, occupied_alias=None):
        program = "\n".join([
            "import json,os,sys,traceback",
            "from pathlib import Path",
            "import release",
            "p=json.loads(sys.argv[1])",
            "for k in ('dockerfile','source_root','context'): p[k]=Path(p[k])",
            "occupied=json.loads(sys.argv[3])",
            "if occupied is not None:",
            "    state=Path(os.environ['FAKE_DOCKER_STATE'])/'images.json'",
            "    images=json.loads(state.read_text())",
            "    alias=release.build_scratch_reference('postgres')",
            "    release.build_scratch_reference=lambda role: alias",
            "    images[alias]=occupied",
            "    state.write_text(json.dumps(images))",
            "try:",
            "    record=release.export_built_image(role='postgres',plan=p,archive_dir=Path(sys.argv[2]))",
            "except Exception as exc:",
            "    print(json.dumps({'error_cause':type(exc.__cause__).__name__}))",
            "    traceback.print_exc()",
            "    sys.exit(1)",
            "print(json.dumps(record))",
        ])
        env = dict(self.env, PYTHONPATH=str(ROOT / "deploy"))
        return subprocess.run(
            ["python3", "-c", program, json.dumps(plan), str(self.root / "exports"), json.dumps(occupied_alias)],
            env=env, capture_output=True, text=True, timeout=20,
        )

    def test_multiplatform_upstream_selects_inspect_and_export_platform(self):
        upstream = "postgres:multi-platform"
        config = config_payload(digest_for("platform-configuration"))
        fixture = write_hybrid_image_archive(
            self.root / "platform.tar", reference=upstream, config=config,
        )
        with tarfile.open(fixture) as archive:
            selected_id = json.load(archive.extractfile("index.json"))["manifests"][0]["digest"]
        root_id = "sha256:" + "e" * 64
        selected = {
            "Id": selected_id, "Os": "linux", "Architecture": "amd64",
            "ConfigBytes": config.decode(), "HybridArchive": True,
        }
        seed_images(self.state, {upstream: {
            "Id": root_id, "Os": "", "Architecture": "", "PlatformImage": selected,
        }})
        (self.state / "image-store-mode").write_text("manifest")
        result = self.export_plan(self.pull_plan(upstream))
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record["configuration_digest"], image_id_for_config(config))
        self.assertEqual(record["descriptor_digests"], [selected_id])
        self.assertNotIn(root_id, record["descriptor_digests"])
        images = json.loads((self.state / "images.json").read_text())
        self.assertEqual(images[record["reference"]]["PlatformImage"]["Id"], selected_id)
        install_inventory = synthetic_inventory(
            ocu_sha="a" * 40, webui_sha="b" * 40,
            images={"postgres": record},
        )
        seed_images(self.state, {
            **{key: value for key, value in images.items()},
            **{
                item["reference"]: {
                    "Id": item["configuration_digest"], "Os": "linux", "Architecture": "amd64",
                }
                for role, item in install_inventory["images"].items() if role != "postgres"
            },
        })
        write_inventory(self.root / "platform-inventory.json", install_inventory)
        verified = self.verify_cmd(self.root / "platform-inventory.json", mode="images")
        self.assertEqual(verified.returncode, 0, verified.stderr)

    def test_unsupported_modern_profile_refuses_and_classic_needs_no_platform_flags(self):
        config = config_payload(digest_for("profile"))
        upstream = "postgres:profile"
        seed_images(self.state, {upstream: {
            "Id": image_id_for_config(config), "Os": "linux", "Architecture": "amd64",
            "ConfigBytes": config.decode(),
        }})
        (self.state / "image-store-mode").write_text("manifest")
        (self.state / "docker-version.json").write_text(json.dumps({
            "Client": {"ApiVersion": "1.48"},
            "Server": {"Version": "classic-compatible", "ApiVersion": "1.48"},
        }))
        result = self.export_plan(self.pull_plan(upstream))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(line.startswith("docker tag ") for line in ops(self.state)))
        self.assertFalse((self.root / "exports/postgres.tar").exists())
        (self.state / "image-store-mode").unlink()
        (self.state / "reject-platform-options").touch()
        result = self.export_plan(self.pull_plan(upstream))
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record["configuration_digest"], image_id_for_config(config))
        self.assertNotIn("descriptor_digests", record)

    def test_digest_upstream_export_retires_only_its_owned_alias_on_success_and_failure(self):
        upstream = "postgres@sha256:" + "d" * 64
        config = config_payload(digest_for("digest-upstream"))
        source = {
            "Id": image_id_for_config(config), "Os": "linux", "Architecture": "amd64",
            "ConfigBytes": config.decode(),
        }
        unrelated = "unrelated:keep"
        seed_images(self.state, {upstream: source, unrelated: dict(source)})
        result = self.export_plan(self.pull_plan(upstream))
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        images = json.loads((self.state / "images.json").read_text())
        self.assertEqual(images[upstream], source)
        self.assertEqual(images[unrelated], source)
        self.assertEqual(images[record["reference"]]["Id"], source["Id"])
        self.assertFalse(any(name.startswith("ocu-build-postgres-") for name in images))
        (self.state / "save-script.json").write_text(json.dumps([{
            "ConfigBytes": config_payload(digest_for("wrong-initial-export")).decode(),
        }]))
        result = self.export_plan(self.pull_plan(upstream))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads((self.state / "images.json").read_text()), images)


    def test_digest_export_alias_failures_preserve_foreign_mappings_and_primary_error(self):
        upstream = "postgres@sha256:" + "d" * 64
        config = config_payload(digest_for("owned-alias"))
        source = {
            "Id": image_id_for_config(config), "Os": "linux", "Architecture": "amd64",
            "ConfigBytes": config.decode(),
        }
        foreign = {**source, "Id": "sha256:" + "f" * 64}
        seed_images(self.state, {upstream: source})
        result = self.export_plan(self.pull_plan(upstream), occupied_alias=foreign)
        self.assertNotEqual(result.returncode, 0)
        images = json.loads((self.state / "images.json").read_text())
        aliases = {name: record for name, record in images.items() if name != upstream}
        self.assertEqual(list(aliases.values()), [foreign])
        self.assertFalse(any(line.startswith("docker tag ") for line in ops(self.state)))
        seed_images(self.state, {upstream: source})
        (self.state / "tag-fail-after").touch()
        result = self.export_plan(self.pull_plan(upstream))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads((self.state / "images.json").read_text()), {upstream: source})
        (self.state / "tag-fail-after").unlink()
        (self.state / "save-rebind-reference.json").write_text(json.dumps(foreign))
        result = self.export_plan(self.pull_plan(upstream))
        self.assertNotEqual(result.returncode, 0)
        images = json.loads((self.state / "images.json").read_text())
        self.assertEqual(images[upstream], source)
        self.assertEqual([value for key, value in images.items() if key != upstream], [foreign])
        (self.state / "save-rebind-reference.json").unlink()
        seed_images(self.state, {upstream: source})
        (self.state / "save-script.json").write_text(json.dumps([{"ConfigBytes": "not JSON"}]))
        (self.state / "image-rm-fail").touch()
        result = self.export_plan(self.pull_plan(upstream))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["error_cause"], "JSONDecodeError")
        images = json.loads((self.state / "images.json").read_text())
        self.assertEqual(images[upstream], source)
        self.assertEqual([value for key, value in images.items() if key != upstream], [source])
        retained_alias = next(name for name in images if name != upstream)
        self.assertIn(retained_alias, result.stderr)

    def descriptor_chain(self, path):
        with tarfile.open(path) as archive:
            descriptor = json.load(archive.extractfile("index.json"))["manifests"][0]
            digests = [descriptor["digest"]]
            if descriptor["mediaType"] == release.OCI_INDEX_MEDIA:
                blob = "blobs/sha256/" + descriptor["digest"][7:]
                child = json.load(archive.extractfile(blob))["manifests"][0]
                digests.append(child["digest"])
        return sorted(digests)

    def claim_hybrid_descriptors(self, delivery, inventory):
        for record in inventory["images"].values():
            record["descriptor_digests"] = self.descriptor_chain(
                delivery / record["archive"]["path"]
            )
        write_inventory(delivery / "release.json", inventory)

    def test_manifest_and_index_engine_ids_import_reimport_and_verify(self):
        delivery, inventory, _sha = self.write_hybrid_delivery(nested_index=True, attestation={})
        self.claim_hybrid_descriptors(delivery, inventory)
        workspace = inventory["images"]["workspace"]
        seed_images(self.state, {
            workspace["reference"]: {
                "Id": workspace["descriptor_digests"][0], "Os": "linux", "Architecture": "amd64",
            },
            "docker.io/library/" + workspace["reference"]: {
                "Id": workspace["configuration_digest"], "Os": "linux", "Architecture": "amd64",
            },
        })
        mode = self.state / "image-store-mode"
        for store in ("manifest", "index"):
            with self.subTest(store=store):
                mode.write_text(store)
                result = self.import_cmd(delivery, self.root / f"install-{store}")
                self.assertEqual(result.returncode, 0, result.stderr)
                images = json.loads((self.state / "images.json").read_text())
                for record in inventory["images"].values():
                    self.assertIn(images[record["reference"]]["Id"], record["descriptor_digests"])
                    self.assertNotEqual(images[record["reference"]]["Id"], record["configuration_digest"])
                verified = self.verify_cmd(delivery / "release.json", mode="images")
                self.assertEqual(verified.returncode, 0, verified.stderr)
                repeated = self.import_cmd(delivery, self.root / f"repeat-{store}")
                self.assertEqual(repeated.returncode, 0, repeated.stderr)

    def test_foreign_descriptor_claims_refuse_before_load(self):
        delivery, inventory, _sha = self.write_hybrid_delivery(nested_index=True, attestation={})
        workspace = inventory["images"]["workspace"]
        archive = delivery / workspace["archive"]["path"]
        with tarfile.open(archive) as bundle:
            root = json.load(bundle.extractfile("index.json"))
        attestation = root["manifests"][1]["digest"]
        foreign = self.descriptor_chain(delivery / inventory["images"]["proxy"]["archive"]["path"])[0]
        claims = (
            ["sha256:" + "f" * 64], [foreign], [attestation],
            [workspace["configuration_digest"]],
            [self.descriptor_chain(archive)[0]] * 2,
        )
        before = seed_images(self.state)
        for index, descriptors in enumerate(claims):
            with self.subTest(descriptors=descriptors):
                workspace["descriptor_digests"] = descriptors
                write_inventory(delivery / "release.json", inventory)
                result = self.import_cmd(delivery, self.root / f"forged-{index}")
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.state / "load-count").exists())
                self.assertEqual(json.loads((self.state / "images.json").read_text()), before)
                self.assertFalse((self.root / f"forged-{index}").exists())

    def duplicate_reference_delivery(self, *, name, alias=False):
        delivery, inventory, _sha = self.write_delivery(name=name)
        earlier = inventory["images"]["computer-use-server"]
        later = inventory["images"]["retention-guard"]
        earlier["reference"] = (
            "docker.io/library/" + later["reference"] if alias else later["reference"]
        )
        earlier["configuration_digest"] = later["configuration_digest"]
        config = config_payload(DEFAULT_RELEASE_DIGESTS["retention-guard"])
        for record in (earlier, later):
            archive = write_hybrid_image_archive(
                delivery / record["archive"]["path"],
                reference=record["reference"], config=config, nested_index=True,
            )
            record["archive"]["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
        return delivery, inventory, earlier, later

    def test_duplicate_reference_cannot_hide_earlier_forged_descriptor_before_load(self):
        for alias in (False, True):
            with self.subTest(alias=alias):
                delivery, inventory, earlier, _later = self.duplicate_reference_delivery(
                    name=f"forged-duplicate-{alias}", alias=alias,
                )
                earlier["descriptor_digests"] = ["sha256:" + "f" * 64]
                write_inventory(delivery / "release.json", inventory)
                seed_images(self.state)
                before = (self.state / "images.json").read_bytes()
                install = self.root / f"forged-duplicate-install-{alias}"
                result = self.import_cmd(delivery, install)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(install.exists())
                self.assertFalse((self.state / "load-count").exists())
                self.assertEqual((self.state / "images.json").read_bytes(), before)

    def test_duplicate_config_only_roles_and_valid_descriptor_subsets_import(self):
        for descriptors in (False, True):
            with self.subTest(descriptors=descriptors):
                delivery, inventory, earlier, later = self.duplicate_reference_delivery(
                    name=f"valid-duplicate-{descriptors}", alias=True,
                )
                if descriptors:
                    archive = delivery / earlier["archive"]["path"]
                    with tarfile.open(archive) as bundle:
                        index_digest = json.load(bundle.extractfile("index.json"))["manifests"][0]["digest"]
                        child = json.load(bundle.extractfile("blobs/sha256/" + index_digest[7:]))
                    manifest_digest = child["manifests"][0]["digest"]
                    earlier["descriptor_digests"] = sorted([index_digest, manifest_digest])
                    later["descriptor_digests"] = [manifest_digest]
                    seed_images(self.state, {later["reference"]: {
                        "Id": manifest_digest, "Os": "linux", "Architecture": "amd64",
                    }})
                write_inventory(delivery / "release.json", inventory)
                install = self.root / f"valid-duplicate-install-{descriptors}"
                result = self.import_cmd(delivery, install)
                self.assertEqual(result.returncode, 0, result.stderr)
                installed = json.loads((install / "release.json").read_text())
                self.assertEqual(installed["images"]["computer-use-server"], earlier)
                self.assertEqual(installed["images"]["retention-guard"], later)
                verified = self.verify_cmd(install / "release.json", mode="images")
                self.assertEqual(verified.returncode, 0, verified.stderr)

    def test_shared_reference_requires_occupied_identity_accepted_by_every_role(self):
        delivery, inventory, earlier, later = self.duplicate_reference_delivery(
            name="incompatible-duplicate",
        )
        archive = delivery / earlier["archive"]["path"]
        with tarfile.open(archive) as bundle:
            index_digest = json.load(bundle.extractfile("index.json"))["manifests"][0]["digest"]
            child = json.load(bundle.extractfile("blobs/sha256/" + index_digest[7:]))
        manifest_digest = child["manifests"][0]["digest"]
        earlier["descriptor_digests"] = [manifest_digest]
        later["descriptor_digests"] = sorted([index_digest, manifest_digest])
        write_inventory(delivery / "release.json", inventory)
        seed_images(self.state, {later["reference"]: {
            "Id": index_digest, "Os": "linux", "Architecture": "amd64",
        }})
        before = (self.state / "images.json").read_bytes()
        install = self.root / "incompatible-duplicate-install"
        result = self.import_cmd(delivery, install)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(install.exists())
        self.assertFalse((self.state / "load-count").exists())
        self.assertEqual((self.state / "images.json").read_bytes(), before)

    def test_foreign_occupied_descriptor_tag_refuses_before_load(self):
        delivery, inventory, _sha = self.write_hybrid_delivery()
        self.claim_hybrid_descriptors(delivery, inventory)
        seed_images(self.state, {inventory["images"]["workspace"]["reference"]: {
            "Id": inventory["images"]["proxy"]["descriptor_digests"][0],
            "Os": "linux", "Architecture": "amd64",
        }})
        before = (self.state / "images.json").read_bytes()
        result = self.import_cmd(delivery, self.root / "foreign-tag")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.state / "load-count").exists())
        self.assertEqual((self.state / "images.json").read_bytes(), before)

    def test_classic_contract1_rollback_imports_with_matching_bundled_reader(self):
        delivery, inventory, _sha = self.write_delivery(contract=1)
        install = self.root / "rollback"
        result = self.import_cmd(delivery, install)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.verify_cmd(install / "release.json", mode="startup", source=install / "source")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((install / "release.json").read_text())["source_consumer_contract"], 1)

    def test_contract1_descriptor_claim_and_mismatched_reader_refuse_before_load(self):
        delivery, inventory, _sha = self.write_delivery(contract=1)
        for descriptors in ([], ["sha256:" + "d" * 64]):
            inventory["images"]["workspace"]["descriptor_digests"] = descriptors
            write_inventory(delivery / "release.json", inventory)
            result = self.import_cmd(delivery, self.root / "invalid-contract")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.state / "load-count").exists())
        del inventory["images"]["workspace"]["descriptor_digests"]
        for contract in (2, 3, True):
            inventory["source_consumer_contract"] = contract
            write_inventory(delivery / "release.json", inventory)
            result = self.import_cmd(delivery, self.root / "wrong-reader")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.state / "load-count").exists())

    def test_new_producer_refuses_source1_before_docker_mutation(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        consumer = ocu / "deploy/release.py"
        consumer.write_text(consumer.read_text().replace(
            f"SOURCE_CONSUMER_CONTRACT = {SOURCE_CONSUMER_CONTRACT}",
            "SOURCE_CONSUMER_CONTRACT = 1", 1,
        ))
        git_init_commit(ocu, "config-only reader")
        destination = self.root / "old-reader-build"
        result = self.build_cmd(ocu, webui, destination)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(destination.exists())
        self.assertEqual(ops(self.state), [])
        self.assertFalse((self.state / "images.json").exists())

    def test_unknown_engine_and_changed_final_export_never_publish(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        (self.state / "image-store-mode").write_text("foreign")
        destination = self.root / "foreign-build"
        result = self.build_cmd(ocu, webui, destination)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(destination.exists())
        self.assertFalse(any(line.startswith("docker tag ") for line in ops(self.state)))
        (self.state / "image-store-mode").unlink()
        (self.state / "images.json").unlink()
        (self.state / "save-script.json").write_text(json.dumps([
            {}, {"ConfigBytes": config_payload(digest_for("changed-final")).decode()},
        ]))
        destination = self.root / "changed-final-build"
        result = self.build_cmd(ocu, webui, destination)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(destination.exists())
        self.assertFalse((self.state / "load-count").exists())

    def engine_images(self):
        return json.loads((self.state / "images.json").read_text())

    def built_tags(self, role):
        return [row["tag"] for row in self.recorded_builds()
                if row["kind"] == "build" and row["tag"].startswith(f"ocu-build-{role}-")]

    def build_seeded(self, seed: str, ocu: Path, webui: Path, dest: Path):
        anchor = "    session = PublicationSession(destination)\n"
        return self.build_with(self.patched_release((anchor, seed + anchor)), ocu, webui, dest)

    def test_successful_build_retires_every_scratch_tag(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        destination = self.root / "scratch-free-build"
        result = self.build_cmd(ocu, webui, destination)
        self.assertEqual(result.returncode, 0, result.stderr)
        built = [row["tag"] for row in self.recorded_builds() if row["kind"] == "build"]
        self.assertEqual(len(built), 6)
        images = self.engine_images()
        self.assertEqual([name for name in images if name.startswith("ocu-build-")], [])
        inventory = json.loads((destination / "release.json").read_text())
        for record in inventory["images"].values():
            self.assertEqual(images[record["reference"]]["Id"], record["configuration_digest"])
        self.assertIn("postgres:17-alpine", images)

    def test_failed_export_retires_the_scratch_tag_and_reports_the_original_error(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        (self.state / "save-script.json").write_text(json.dumps([{
            "ConfigBytes": config_payload(digest_for("wrong-workspace-export")).decode(),
        }]))
        destination = self.root / "failed-export-build"
        result = self.build_cmd(ocu, webui, destination)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("engine image identity is not verified by the archive", result.stderr)
        self.assertEqual(len(self.built_tags("workspace")), 1)
        self.assertFalse(any(name.startswith("ocu-build-workspace-") for name in self.engine_images()))
        self.assertFalse(destination.exists())

    def test_build_failing_before_tagging_reports_its_error_and_removes_nothing(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        (self.state / "build-script.json").write_text(json.dumps({"message": "scripted build failure"}))
        destination = self.root / "failed-build"
        result = self.build_cmd(ocu, webui, destination)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("scripted build failure", result.stderr)
        self.assertFalse(any(" image rm " in line for line in ops(self.state)))
        self.assertFalse(destination.exists())

    def test_stale_scratch_tag_with_the_same_pid_is_not_a_conflict_and_is_kept(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        stale = {"Id": "sha256:" + "5" * 64, "Os": "linux", "Architecture": "amd64"}
        seed = (
            "    _path = Path(os.environ['FAKE_DOCKER_STATE']) / 'images.json'\n"
            "    _images = json.loads(_path.read_text()) if _path.exists() else {}\n"
            "    for _role in ROLE_ORDER:\n"
            f"        _images[f'ocu-build-{{_role}}-{{os.getpid()}}'] = {stale!r}\n"
            "    _path.write_text(json.dumps(_images))\n"
        )
        destination = self.root / "stale-scratch-build"
        result = self.build_seeded(seed, ocu, webui, destination)
        self.assertEqual(result.returncode, 0, result.stderr)
        remaining = {name: record for name, record in self.engine_images().items()
                     if name.startswith("ocu-build-")}
        [pid] = {name.rsplit("-", 1)[1] for name in remaining}
        self.assertEqual(remaining, {f"ocu-build-{role}-{pid}": stale for role in ROLE_ORDER})

    def test_scratch_names_are_unique_and_an_occupied_name_aborts_before_building(self):
        first = release.build_scratch_reference("workspace")
        second = release.build_scratch_reference("workspace")
        self.assertNotEqual(first, second)
        for name in (first, second):
            self.assertRegex(name, rf"^ocu-build-workspace-{os.getpid()}-[0-9a-f]{{12}}$")
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        occupied = {"Id": "sha256:" + "6" * 64, "Os": "linux", "Architecture": "amd64"}
        seeded = seed_images(self.state, {"ocu-build-occupied": occupied})
        seed = "    globals()['build_scratch_reference'] = lambda role: 'ocu-build-occupied'\n"
        destination = self.root / "occupied-scratch-build"
        result = self.build_seeded(seed, ocu, webui, destination)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("existing image ocu-build-occupied conflicts with the release", result.stderr)
        self.assertEqual(self.engine_images(), seeded)
        self.assertEqual(self.recorded_builds(), [])
        self.assertFalse(any(" image rm " in line for line in ops(self.state)))
        self.assertFalse(destination.exists())

    def test_scratch_cleanup_failure_fails_success_and_warns_after_a_failure(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        (self.state / "image-rm-fail").touch()
        destination = self.root / "cleanup-failed-build"
        result = self.build_cmd(ocu, webui, destination)
        self.assertNotEqual(result.returncode, 0)
        [scratch] = self.built_tags("workspace")
        self.assertIn(scratch, self.engine_images())
        self.assertIn(scratch, result.stderr)
        self.assertIn("image reference removal failed", result.stderr)
        self.assertNotIn("may remain", result.stderr)
        self.assertFalse(destination.exists())
        (self.state / "save-script.json").write_text(json.dumps([{
            "ConfigBytes": config_payload(digest_for("wrong-export-and-cleanup")).decode(),
        }]))
        destination = self.root / "export-and-cleanup-failed-build"
        result = self.build_cmd(ocu, webui, destination)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("engine image identity is not verified by the archive", result.stderr)
        scratch = self.built_tags("workspace")[-1]
        self.assertIn(f"{scratch} may remain", result.stderr)
        self.assertFalse(destination.exists())

    def test_descriptor_store_build_uses_config_names_and_verified_final_archives(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        (self.state / "image-store-mode").write_text("index")
        destination = self.root / "index-build"
        result = self.build_cmd(ocu, webui, destination)
        self.assertEqual(result.returncode, 0, result.stderr)
        inventory = json.loads((destination / "release.json").read_text())
        for role, record in inventory["images"].items():
            archive = destination / record["archive"]["path"]
            with tarfile.open(archive) as bundle:
                manifest = json.load(bundle.extractfile("manifest.json"))[0]
                config = bundle.extractfile(manifest["Config"]).read()
            self.assertEqual(record["configuration_digest"], image_id_for_config(config))
            name = "open-computer-use" if role == "workspace" else f"ocu-{role}"
            self.assertEqual(record["reference"], name + ":" + image_id_for_config(config)[7:])
            self.assertEqual(record["descriptor_digests"], self.descriptor_chain(archive))
        self.assertEqual(sorted(path.name for path in (destination / "images").iterdir()),
                         sorted(f"{role}.tar" for role in ROLE_ORDER))

    def rewrite_archive(self, path, transform):
        with tarfile.open(path) as archive:
            members = {
                member.name: archive.extractfile(member).read()
                for member in archive.getmembers() if member.isfile()
            }
        transform(members)
        with tarfile.open(path, "w") as archive:
            for name, data in members.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))

    def test_descriptor_ancestry_excludes_attestation_config_layer_and_export_index(self):
        reference = "open-computer-use:ancestry"
        config = config_payload(digest_for("ancestry"))
        archive = write_hybrid_image_archive(
            self.root / "ancestry.tar", reference=reference, config=config,
            nested_index=True, attestation={},
        )
        with tarfile.open(archive) as bundle:
            outer_bytes = bundle.extractfile("index.json").read()
            outer = json.loads(outer_bytes)
            index_digest = outer["manifests"][0]["digest"]
            index = json.load(bundle.extractfile("blobs/sha256/" + index_digest[7:]))
            manifest_digest = index["manifests"][0]["digest"]
            manifest = json.load(bundle.extractfile("blobs/sha256/" + manifest_digest[7:]))
        result = release.inspect_archive_bytes(archive)[reference]
        self.assertEqual(result["descriptor_digests"], sorted([index_digest, manifest_digest]))
        for forbidden in (
            image_id_for_config(config), manifest["layers"][0]["digest"],
            outer["manifests"][1]["digest"], image_id_for_config(outer_bytes),
        ):
            self.assertNotIn(forbidden, result["descriptor_digests"])

    def test_containerd_subject_marker_alone_excludes_attachment_from_runtime_authority(self):
        delivery, inventory, _sha = self.write_hybrid_delivery(attestation={})
        record = inventory["images"]["workspace"]
        archive = delivery / record["archive"]["path"]
        identities = {}
        def vendor_marker_only(members):
            root = json.loads(members["index.json"])
            runtime, attachment = root["manifests"]
            identities["runtime"] = runtime["digest"]
            identities["attachment"] = attachment["digest"]
            attachment["annotations"] = {
                "io.containerd.manifest.subject": runtime["digest"],
            }
            members["index.json"] = json.dumps(root).encode()
        self.rewrite_archive(archive, vendor_marker_only)
        selected = release.inspect_archive_bytes(archive)[record["reference"]]
        self.assertEqual(selected["digest"], record["configuration_digest"])
        self.assertEqual(selected["descriptor_digests"], [identities["runtime"]])
        self.assertNotIn(identities["attachment"], selected["descriptor_digests"])
        record["archive"]["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
        record["descriptor_digests"] = [identities["attachment"]]
        write_inventory(delivery / "release.json", inventory)
        seed_images(self.state)
        before = (self.state / "images.json").read_bytes()
        install = self.root / "attachment-authority"
        result = self.import_cmd(delivery, install)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(install.exists())
        self.assertFalse((self.state / "load-count").exists())
        self.assertEqual((self.state / "images.json").read_bytes(), before)
        def name_attachment(members):
            root = json.loads(members["index.json"])
            root["manifests"][1]["annotations"]["io.containerd.image.name"] = record["reference"]
            members["index.json"] = json.dumps(root).encode()
        self.rewrite_archive(archive, name_attachment)
        with self.assertRaises(release.ReleaseError):
            release.inspect_archive_bytes(archive)

    def test_foreign_unnamed_sibling_does_not_authorize_ancestor_index(self):
        reference = "open-computer-use:bound"
        archive = write_hybrid_image_archive(
            self.root / "bound.tar", reference=reference,
            config=config_payload(digest_for("bound")), nested_index=True,
        )
        foreign = write_hybrid_image_archive(
            self.root / "foreign.tar", reference="foreign:sibling",
            config=config_payload(digest_for("foreign")),
        )
        with tarfile.open(foreign) as bundle:
            foreign_members = {
                member.name: bundle.extractfile(member).read()
                for member in bundle.getmembers() if member.isfile()
            }
        foreign_descriptor = json.loads(foreign_members["index.json"])["manifests"][0]
        del foreign_descriptor["annotations"]
        identities = {}
        def add_sibling(members):
            root = json.loads(members["index.json"])
            descriptor = root["manifests"][0]
            annotations = descriptor.pop("annotations")
            child_name = "blobs/sha256/" + descriptor["digest"][7:]
            child = json.loads(members[child_name])
            identities["manifest"] = child["manifests"][0]["digest"]
            child["manifests"][0]["annotations"] = annotations
            child["manifests"].append(foreign_descriptor)
            child_bytes = json.dumps(child).encode()
            descriptor["digest"] = image_id_for_config(child_bytes)
            descriptor["size"] = len(child_bytes)
            identities["index"] = descriptor["digest"]
            members["blobs/sha256/" + descriptor["digest"][7:]] = child_bytes
            members.update({name: data for name, data in foreign_members.items()
                            if name.startswith("blobs/sha256/")})
            members["index.json"] = json.dumps(root).encode()
        self.rewrite_archive(archive, add_sibling)
        selected = release.inspect_archive_bytes(archive)[reference]
        self.assertEqual(selected["descriptor_digests"], [identities["manifest"]])
        self.assertNotIn(identities["index"], selected["descriptor_digests"])
        self.assertNotIn(foreign_descriptor["digest"], selected["descriptor_digests"])

    def add_marked_descendant(self, archive, *, marker, config, platform=None, index=False):
        descendant = write_hybrid_image_archive(
            self.root / "marked-descendant.tar", reference="foreign:descendant",
            config=config,
        )
        with tarfile.open(descendant) as bundle:
            added = {
                member.name: bundle.extractfile(member).read()
                for member in bundle.getmembers() if member.isfile()
            }
        descriptor = json.loads(added["index.json"])["manifests"][0]
        descriptor.pop("annotations")
        descriptor.pop("platform")
        if platform is not None:
            os_name, architecture = platform.split("/")
            descriptor["platform"] = {"os": os_name, "architecture": architecture}
        manifest_name = "blobs/sha256/" + descriptor["digest"][7:]
        marked_manifest = json.loads(added[manifest_name])
        marked_manifest["annotations"] = {"org.opencontainers.image.title": "marked attachment"}
        marked_bytes = json.dumps(marked_manifest).encode()
        descriptor["digest"] = image_id_for_config(marked_bytes)
        descriptor["size"] = len(marked_bytes)
        added["blobs/sha256/" + descriptor["digest"][7:]] = marked_bytes
        if index:
            index_bytes = json.dumps({
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [descriptor],
            }).encode()
            index_digest = image_id_for_config(index_bytes)
            added["blobs/sha256/" + index_digest[7:]] = index_bytes
            descriptor = {
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "digest": index_digest, "size": len(index_bytes),
            }
        identities = {"attachment": descriptor["digest"]}
        def add_descendant(members):
            root = json.loads(members["index.json"])
            ancestor = root["manifests"][0]
            child = json.loads(members["blobs/sha256/" + ancestor["digest"][7:]])
            identities["runtime"] = child["manifests"][0]["digest"]
            if marker == "subject":
                descriptor["annotations"] = {"io.containerd.manifest.subject": identities["runtime"]}
            elif marker == "attestation-type":
                descriptor["annotations"] = {"vnd.docker.reference.type": "attestation-manifest"}
            else:
                descriptor["artifactType"] = "application/vnd.example.attachment"
            child["manifests"].append(descriptor)
            child_bytes = json.dumps(child).encode()
            ancestor["digest"] = image_id_for_config(child_bytes)
            ancestor["size"] = len(child_bytes)
            identities["index"] = ancestor["digest"]
            members.update({name: data for name, data in added.items()
                            if name.startswith("blobs/sha256/")})
            members["blobs/sha256/" + ancestor["digest"][7:]] = child_bytes
            members["index.json"] = json.dumps(root).encode()
        self.rewrite_archive(archive, add_descendant)
        return identities

    def test_marked_foreign_descendants_cannot_authorize_ancestor_before_load(self):
        delivery, inventory, _sha = self.write_hybrid_delivery(nested_index=True)
        record = inventory["images"]["workspace"]
        archive = delivery / record["archive"]["path"]
        original = archive.read_bytes()
        seed_images(self.state)
        before = (self.state / "images.json").read_bytes()
        for marker in ("subject", "attestation-type", "artifact"):
            for platform in ("linux/amd64", None, "unknown/unknown"):
                with self.subTest(marker=marker, platform=platform):
                    archive.write_bytes(original)
                    identities = self.add_marked_descendant(
                        archive, marker=marker, platform=platform,
                        config=config_payload(digest_for("foreign-selectable")),
                    )
                    record["archive"]["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
                    for claim in ("index", "runtime", "configuration"):
                        with self.subTest(claim=claim):
                            if claim == "configuration":
                                record.pop("descriptor_digests", None)
                            else:
                                record["descriptor_digests"] = [identities[claim]]
                            write_inventory(delivery / "release.json", inventory)
                            platform_label = platform.replace("/", "-") if platform else "missing"
                            install = self.root / f"marked-foreign-{marker}-{platform_label}-{claim}"
                            result = self.import_cmd(delivery, install)
                            self.assertNotEqual(result.returncode, 0)
                            self.assertFalse(install.exists())
                            self.assertFalse((self.state / "load-count").exists())
                            self.assertEqual((self.state / "images.json").read_bytes(), before)

    def test_same_configuration_marked_manifest_cannot_be_selected_before_load(self):
        delivery, inventory, _sha = self.write_hybrid_delivery(nested_index=True)
        record = inventory["images"]["workspace"]
        archive = delivery / record["archive"]["path"]
        with tarfile.open(archive) as bundle:
            runtime_entry = json.load(bundle.extractfile("manifest.json"))[0]
            config = bundle.extractfile(runtime_entry["Config"]).read()
        original = archive.read_bytes()
        seed_images(self.state)
        before = (self.state / "images.json").read_bytes()
        for marker in ("subject", "attestation-type", "artifact"):
            for platform in ("linux/amd64", None):
                with self.subTest(marker=marker, platform=platform):
                    archive.write_bytes(original)
                    identities = self.add_marked_descendant(
                        archive, marker=marker, platform=platform, config=config,
                    )
                    self.assertNotEqual(identities["attachment"], identities["runtime"])
                    with tarfile.open(archive) as bundle:
                        runtime_manifest = json.load(bundle.extractfile(
                            "blobs/sha256/" + identities["runtime"][7:],
                        ))
                        marked_manifest = json.load(bundle.extractfile(
                            "blobs/sha256/" + identities["attachment"][7:],
                        ))
                    self.assertEqual(marked_manifest["config"], runtime_manifest["config"])
                    self.assertEqual(marked_manifest["layers"], runtime_manifest["layers"])
                    self.assertEqual(marked_manifest["config"]["digest"], record["configuration_digest"])
                    record["archive"]["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
                    for claim in ("index", "runtime", "configuration"):
                        with self.subTest(claim=claim):
                            if claim == "configuration":
                                record.pop("descriptor_digests", None)
                            else:
                                record["descriptor_digests"] = [identities[claim]]
                            write_inventory(delivery / "release.json", inventory)
                            install = self.root / f"same-config-marked-{marker}-{platform is None}-{claim}"
                            result = self.import_cmd(delivery, install)
                            self.assertNotEqual(result.returncode, 0)
                            self.assertFalse(install.exists())
                            self.assertFalse((self.state / "load-count").exists())
                            self.assertEqual((self.state / "images.json").read_bytes(), before)

    def test_marked_indexes_refuse_unqualified_selection_before_load(self):
        delivery, inventory, _sha = self.write_hybrid_delivery(nested_index=True)
        record = inventory["images"]["workspace"]
        archive = delivery / record["archive"]["path"]
        original = archive.read_bytes()
        seed_images(self.state)
        before = (self.state / "images.json").read_bytes()
        for marker in ("subject", "attestation-type", "artifact"):
            with self.subTest(marker=marker):
                archive.write_bytes(original)
                identities = self.add_marked_descendant(
                    archive, marker=marker, index=True,
                    config=config_payload(digest_for("foreign-index")),
                )
                record["archive"]["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
                record["descriptor_digests"] = [identities["runtime"]]
                write_inventory(delivery / "release.json", inventory)
                install = self.root / f"marked-index-{marker}"
                result = self.import_cmd(delivery, install)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(install.exists())
                self.assertFalse((self.state / "load-count").exists())
                self.assertEqual((self.state / "images.json").read_bytes(), before)

    def test_verified_unknown_platform_attachment_preserves_runtime_ancestor(self):
        reference = "open-computer-use:unknown-attachment"
        archive = write_hybrid_image_archive(
            self.root / "unknown-attachment.tar", reference=reference,
            config=config_payload(digest_for("runtime")), nested_index=True,
        )
        identities = self.add_marked_descendant(
            archive, marker="subject",
            config=config_payload(digest_for("attestation"), os_name="unknown", architecture="unknown"),
        )
        selected = release.inspect_archive_bytes(archive)[reference]
        self.assertEqual(selected["descriptor_digests"], sorted([
            identities["runtime"], identities["index"],
        ]))
        self.assertNotIn(identities["attachment"], selected["descriptor_digests"])

    def test_nested_attestation_preserves_only_runtime_ancestor_identities(self):
        reference = "open-computer-use:nested-attestation"
        archive = write_hybrid_image_archive(
            self.root / "nested-attestation.tar", reference=reference,
            config=config_payload(digest_for("nested-attestation")),
            nested_index=True, attestation={},
        )
        identities = {}
        def nest_attestation(members):
            root = json.loads(members["index.json"])
            descriptor = root["manifests"][0]
            attestation = root["manifests"].pop()
            identities["attestation"] = attestation["digest"]
            child = json.loads(members["blobs/sha256/" + descriptor["digest"][7:]])
            identities["manifest"] = child["manifests"][0]["digest"]
            child["manifests"].append(attestation)
            child_bytes = json.dumps(child).encode()
            descriptor["digest"] = image_id_for_config(child_bytes)
            descriptor["size"] = len(child_bytes)
            identities["index"] = descriptor["digest"]
            members["blobs/sha256/" + descriptor["digest"][7:]] = child_bytes
            members["index.json"] = json.dumps(root).encode()
        self.rewrite_archive(archive, nest_attestation)
        selected = release.inspect_archive_bytes(archive)[reference]
        self.assertEqual(selected["descriptor_digests"], sorted([
            identities["index"], identities["manifest"],
        ]))
        self.assertNotIn(identities["attestation"], selected["descriptor_digests"])

    def test_wrong_descriptor_and_ancestor_platform_refuse_before_load(self):
        delivery, inventory, _sha = self.write_hybrid_delivery(nested_index=True)
        record = inventory["images"]["workspace"]
        archive = delivery / record["archive"]["path"]
        original = archive.read_bytes()
        for nested in (False, True):
            archive.write_bytes(original)
            def corrupt(members):
                root = json.loads(members["index.json"])
                descriptor = root["manifests"][0]
                if nested:
                    descriptor["platform"] = {"os": "linux", "architecture": "arm64"}
                else:
                    child_name = "blobs/sha256/" + descriptor["digest"][7:]
                    child = json.loads(members[child_name])
                    child["manifests"][0]["platform"]["architecture"] = "arm64"
                    child_bytes = json.dumps(child).encode()
                    child_digest = hashlib.sha256(child_bytes).hexdigest()
                    members["blobs/sha256/" + child_digest] = child_bytes
                    descriptor["digest"] = "sha256:" + child_digest
                    descriptor["size"] = len(child_bytes)
                members["index.json"] = json.dumps(root).encode()
            self.rewrite_archive(archive, corrupt)
            record["archive"]["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
            write_inventory(delivery / "release.json", inventory)
            result = self.import_cmd(delivery, self.root / f"wrong-platform-{nested}")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.state / "load-count").exists())

    def test_classic_hybrid_repositories_bind_verified_top_layer_not_configuration(self):
        delivery, inventory, _sha = self.write_delivery()
        record = inventory["images"]["workspace"]
        layer = b"classic exporter layer bytes\n"
        top_layer = "sha256:" + hashlib.sha256(layer).hexdigest()
        config = config_payload(top_layer)
        archive = write_hybrid_image_archive(
            delivery / record["archive"]["path"], reference=record["reference"],
            config=config, layer=layer, repositories=True,
        )
        record["configuration_digest"] = image_id_for_config(config)
        record["archive"]["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
        proxy = inventory["images"]["proxy"]
        proxy_payload = json.loads(config)
        proxy_payload["config"]["Env"].append("ROLE=proxy")
        proxy_config = json.dumps(proxy_payload).encode()
        proxy_archive = write_hybrid_image_archive(
            delivery / proxy["archive"]["path"], reference=proxy["reference"],
            config=proxy_config, layer=layer, repositories=True,
        )
        proxy["configuration_digest"] = image_id_for_config(proxy_config)
        proxy["archive"]["sha256"] = hashlib.sha256(proxy_archive.read_bytes()).hexdigest()
        write_inventory(delivery / "release.json", inventory)
        result = self.import_cmd(delivery, self.root / "classic-hybrid")
        self.assertEqual(result.returncode, 0, result.stderr)
        images = json.loads((self.state / "images.json").read_text())
        self.assertEqual(images[record["reference"]]["Id"], image_id_for_config(config))
        self.assertNotEqual(images[record["reference"]]["Id"], top_layer)
        self.assertEqual(images[proxy["reference"]]["Id"], image_id_for_config(proxy_config))
        self.assertNotEqual(images[proxy["reference"]]["Id"], images[record["reference"]]["Id"])

    def test_wrong_repository_association_and_layer_bytes_refuse_before_load(self):
        delivery, inventory, _sha = self.write_delivery()
        record = inventory["images"]["workspace"]
        layer = b"repository-associated top layer\n"
        layer_id = hashlib.sha256(layer).hexdigest()
        config = config_payload("sha256:" + layer_id)
        archive = write_hybrid_image_archive(
            delivery / record["archive"]["path"], reference=record["reference"],
            config=config, layer=layer, repositories=True,
        )
        record["configuration_digest"] = image_id_for_config(config)
        original = archive.read_bytes()
        before = seed_images(self.state)
        for defect in ("configuration-id", "wrong-layer", "undeclared-reference", "changed-layer"):
            with self.subTest(defect=defect):
                archive.write_bytes(original)
                def corrupt(members):
                    repositories = json.loads(members["repositories"])
                    name, _, tag = record["reference"].rpartition(":")
                    if defect == "configuration-id":
                        repositories[name][tag] = record["configuration_digest"][7:]
                    elif defect == "wrong-layer":
                        repositories[name][tag] = "f" * 64
                    elif defect == "undeclared-reference":
                        repositories["foreign/image"] = {"tag": layer_id}
                    else:
                        members["blobs/sha256/" + layer_id] = b"changed layer\n"
                    members["repositories"] = json.dumps(repositories).encode()
                self.rewrite_archive(archive, corrupt)
                record["archive"]["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
                write_inventory(delivery / "release.json", inventory)
                destination = self.root / defect
                result = self.import_cmd(delivery, destination)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(destination.exists())
                self.assertFalse((self.state / "load-count").exists())
                self.assertEqual(json.loads((self.state / "images.json").read_text()), before)

    def test_repository_layer_never_authorizes_an_existing_engine_identity(self):
        delivery, inventory, _sha = self.write_delivery()
        record = inventory["images"]["workspace"]
        layer = b"not an engine identity\n"
        layer_id = hashlib.sha256(layer).hexdigest()
        config = config_payload("sha256:" + layer_id)
        archive = write_hybrid_image_archive(
            delivery / record["archive"]["path"], reference=record["reference"],
            config=config, layer=layer, repositories=True,
        )
        record["configuration_digest"] = image_id_for_config(config)
        record["descriptor_digests"] = self.descriptor_chain(archive)
        record["archive"]["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
        write_inventory(delivery / "release.json", inventory)
        seed_images(self.state, {record["reference"]: {
            "Id": "sha256:" + layer_id, "Os": "linux", "Architecture": "amd64",
        }})
        before = (self.state / "images.json").read_bytes()
        result = self.import_cmd(delivery, self.root / "layer-engine")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.state / "load-count").exists())
        self.assertEqual((self.state / "images.json").read_bytes(), before)


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

    def test_documentserver_base_comes_from_committed_recipe(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        selected = "onlyoffice/documentserver@sha256:" + "d" * 64
        declaration = ocu / "deploy/documentserver/Dockerfile"
        declaration.write_text(declaration.read_text().replace(DOCUMENTSERVER_UPSTREAM, selected))
        recipe_hash = hashlib.sha256(declaration.read_bytes()).hexdigest()
        git_init_commit(ocu, "select immutable base recipe")
        destination = self.root / "selected-recipe"
        result = self.build_cmd(ocu, webui, destination)
        self.assertEqual(result.returncode, 0, result.stderr)
        inventory = json.loads((destination / "release.json").read_text())
        image = inventory["images"]["documentserver"]
        self.assertEqual(image["build"]["arguments"], {})
        self.assertEqual(image["build"]["argument_defaults"], {})
        self.assertEqual(image["build"]["materials"], [
            {"name": "base", "requested": selected, "kind": "base-image"}
        ])
        self.assertEqual(image["build"]["dockerfile_sha256"], recipe_hash)
        builds = [row for row in self.recorded_builds()
                  if row["kind"] == "build" and "documentserver" in row["tag"]]
        self.assertEqual(len(builds), 1)
        self.assertFalse(any(row["kind"] == "pull" and row["tag"] == selected
                             for row in self.recorded_builds()))

    def test_documentserver_base_override_refuses_before_docker_mutation(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        result = self.build_cmd(
            ocu, webui, self.root / "override",
            extra=["--build-arg", "documentserver:BASE_IMAGE=mutable:escape"],
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("documentserver", result.stderr)
        self.assertFalse(self.recorded_builds())
        self.assertFalse(any(set(row.split()) & {"build", "pull", "tag", "load"}
                             for row in ops(self.state)))

    def test_documentserver_recipe_refuses_mutable_or_ambiguous_base_before_build(self):
        ocu, webui, _ocu_sha, _webui_sha = self.write_committed_sources()
        recipe = ocu / "deploy/documentserver/Dockerfile"
        for index, source in enumerate((
            "FROM onlyoffice/documentserver:latest\n",
            f"FROM {DOCUMENTSERVER_UPSTREAM}\nFROM {DOCUMENTSERVER_UPSTREAM}\n",
            f"ARG BASE={DOCUMENTSERVER_UPSTREAM}\nFROM $BASE\n",
        )):
            with self.subTest(source=source):
                recipe.write_text(source)
                git_init_commit(ocu, f"invalid base {index}")
                result = self.build_cmd(ocu, webui, self.root / f"bad-base-{index}")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("documentserver recipe", result.stderr)
                self.assertFalse(self.recorded_builds())
                self.assertFalse(any(set(row.split()) & {"build", "pull", "tag", "load"}
                                     for row in ops(self.state)))

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

    def test_messages_ingress_image_is_release_verified_before_mutation(self):
        for case in ("mismatch", "missing"):
            with self.subTest(case=case):
                env, script, _source = prepare_up_context(self.state)
                docs = intended_docs()
                if case == "mismatch":
                    docs["proxy.json"]["services"]["messages-ingress"]["image"] = "unselected-proxy:mutant"
                else:
                    docs["proxy.json"]["services"].pop("messages-ingress")
                write_fake_configs(self.state, docs)
                seed_healthy_host(self.state)
                write_network(self.state, "ocu-test-private", subnet="172.30.0.0/24", gateway="172.30.0.1")
                write_network(self.state, "ocu-sandbox", subnet="172.31.0.0/24", gateway="172.31.0.1")
                before_firewall = (self.state / "firewall.json").read_bytes()
                before_ops = len(ops(self.state))
                result = run_script(script, env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("messages-ingress", result.stderr)
                self.assertIn("image", result.stderr)
                self.assertFalse((self.state / "starts.log").exists())
                self.assertEqual((self.state / "firewall.json").read_bytes(), before_firewall)
                mutations = {"load", "pull", "build", "create", "up", "iptables-restore", "ip6tables-restore"}
                self.assertFalse(any(set(row.split()) & mutations for row in ops(self.state)[before_ops:]))

    def test_import_rejects_old_pulled_documentserver_before_loading_images(self):
        delivery, inventory, _sha = self.write_delivery()
        build = inventory["images"]["documentserver"]["build"]
        build.update(
            dockerfile="deploy/release.py", context=".",
            arguments={"DOCUMENTSERVER_IMAGE": DOCUMENTSERVER_UPSTREAM},
            argument_defaults={"DOCUMENTSERVER_IMAGE": DOCUMENTSERVER_UPSTREAM},
            materials=[{"name": "documentserver", "requested": DOCUMENTSERVER_UPSTREAM,
                        "kind": "upstream-image"}],
        )
        write_inventory(delivery / "release.json", inventory)
        install = self.root / "old-documentserver-install"
        result = self.import_cmd(delivery, install)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("documentserver", result.stderr)
        self.assertFalse(install.exists())
        self.assertFalse((self.state / "load-count").exists())
        self.assertEqual(ops(self.state), [])

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
                    self.assertNotIn("Traceback", result.stderr)
                    if defect == "missing":
                        self.assertIn(f"{delivery.name}/fonts.tar: missing", result.stderr)
                self.assertFalse(installed.exists())
                self.assertFalse(list(self.root.glob(f"install-{defect}.stage-*")))
                self.assertFalse((self.state / "load-count").exists())
                # Import checks which images are present before it stages the fonts.
                self.assertEqual([
                    line for line in ops(self.state)
                    if not line.startswith(("docker info ", "docker image inspect "))
                ], [])
                self.assertEqual(outside.read_bytes(), b"unrelated bytes")

    def test_build_exports_documentserver_as_source_built_role(self):
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
        built = [
            row for row in commands
            if row["kind"] == "build" and "documentserver" in row["tag"]
        ]
        self.assertEqual(len(built), 1)
        self.assertEqual(built[0]["platform"], "linux/amd64")
        self.assertFalse(any(
            row["kind"] == "pull" and row["tag"] == upstream for row in commands
        ))
        documentserver = inventory["images"]["documentserver"]
        self.assertEqual(documentserver["configuration_digest"], built[0]["id"])
        self.assertEqual(
            documentserver["archive"]["sha256"],
            hashlib.sha256(
                (destination / documentserver["archive"]["path"]).read_bytes()
            ).hexdigest(),
        )
        self.assertEqual(
            documentserver["build"]["dockerfile"], "deploy/documentserver/Dockerfile"
        )
        self.assertEqual(documentserver["build"]["context"], "deploy/documentserver")
        self.assertEqual(documentserver["build"]["arguments"], {})
        self.assertEqual(documentserver["build"]["argument_defaults"], {})
        self.assertEqual(documentserver["build"]["argument_overrides"], {})
        self.assertEqual(
            documentserver["build"]["materials"],
            [{"name": "base", "requested": upstream, "kind": "base-image"}],
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
            elif "ocu-build-documentserver-" in tag:
                roles["documentserver"] = record
                self.assertNotIn("untracked-documentserver.env", files)
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
                "documentserver",
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
        needle = (
            "        with ImageStoreLock(image_store_lock_path(local_daemon_identity())):\n"
            "            reject_existing_conflicts(imported)\n"
        )
        source = self.release_script.read_text(encoding="utf-8")
        self.assertIn(needle, source)
        self._script_serial = getattr(self, "_script_serial", 0) + 1
        script_b = self.root / f"isolated-release-{self._script_serial}.py"
        script_b.write_text(
            source.replace(
                needle,
                f"        Path({str(ready)!r}).write_text('1')\n" + needle,
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
        refusal = f"{delivery.name}/{bundle.name}: missing"
        self.assertIn(refusal, absent.stderr)
        install = self.root / "install-no-source-bundle"
        imported = self.import_cmd(delivery, install)
        self.assertNotEqual(imported.returncode, 0)
        self.assertIn(refusal, imported.stderr)
        self.assertNotIn("Traceback", imported.stderr)
        self.assertFalse(install.exists())
        self.assertFalse(list(self.root.glob("install-no-source-bundle.stage-*")))
        self.assertFalse((self.state / "load-count").exists())
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
        Path(env["OCU_OFFICE_FONTS_DIR"]).mkdir()
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
        # The copied bootstrap must not create an ambient operator directory.
        env.pop("OCU_OFFICE_PROXY_PORT", None)
        env.pop("OCU_OFFICE_FONTS_DIR", None)
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
                self.assertFalse((self.state / "starts.log").exists())
                recorded = "\n".join(ops(self.state))
                self.assertNotIn("network create", recorded)
                self.assertNotIn("compose up", recorded)
                self.assertFalse((self.state / "builds.json").exists())

    def accepted_image(self, role: str, architecture="amd64") -> dict:
        config = config_payload(DEFAULT_RELEASE_DIGESTS[role])
        return {
            "Id": image_id_for_config(config),
            "Os": "linux",
            "Architecture": architecture,
            "ConfigBytes": config.decode("utf-8"),
        }

    def seed_present(self, inventory: dict, roles) -> None:
        seed_images(self.state, {
            inventory["images"][role]["reference"]: self.accepted_image(role)
            for role in roles
        })

    def copy_probe(self, log: Path) -> tuple[str, str]:
        anchor = "def copy_regular(source: Path, dest: Path) -> None:\n"
        return (
            anchor,
            anchor
            + f"    with open({str(log)!r}, 'a', encoding='utf-8') as copies:\n"
            + "        copies.write(str(dest) + '\\n')\n",
        )

    def space_script(self, free: dict, *, shared: bool, extra=()) -> Path:
        mapping = {str(path.resolve()): value for path, value in free.items()}
        replacements = [(
            SPACE_FREE_PROBE,
            f"        available = {mapping!r}[str(Path(path).resolve())]\n",
        )]
        if not shared:
            replacements.append((SPACE_DEVICE_PROBE, "    if False:\n"))
        return self.patched_release(*replacements, *extra)

    def space_need(self, delivery: Path, inventory: dict, to_load) -> tuple[int, int]:
        load = sum(
            (delivery / inventory["images"][role]["archive"]["path"]).stat().st_size
            for role in to_load
        )
        bundles = sum(
            (delivery / inventory[name]["path"]).stat().st_size
            for name in ("source_bundle", "font_bundle")
        )
        return load + 2 * bundles, 3 * load

    def loaded_archives(self, state: Path | None = None) -> list[str]:
        return [
            line.split()[-1]
            for line in ops(state or self.state)
            if line.startswith("docker load ")
        ]

    def summary_line(self, stderr: str) -> str:
        lines = [
            line for line in stderr.splitlines()
            if line.startswith("release: present roles skipped:")
        ]
        self.assertEqual(len(lines), 1, stderr)
        return lines[0]

    def published_tree(self, root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file() and ".git" not in path.relative_to(root).parts
        }

    def assert_published(self, install: Path, delivery: Path, name="delivery") -> None:
        self.assertEqual(
            sorted(path.name for path in install.iterdir()),
            ["fonts", "release.json", "source"],
        )
        tree = self.published_tree(install)
        self.assertEqual(tree["release.json"], (delivery / "release.json").read_bytes())
        source = {
            key[len("source/"):]: value
            for key, value in tree.items() if key.startswith("source/")
        }
        self.assertEqual(source, self.published_tree(self.root / f"{name}-ocu-src"))
        fonts = {
            Path(key).name: value
            for key, value in tree.items() if key.startswith("fonts/")
        }
        self.assertEqual(fonts, FONT_FILES)

    def assert_untouched(self, install: Path, copies: Path) -> None:
        self.assertFalse(copies.exists(), copies.read_text() if copies.exists() else "")
        self.assertEqual(self.loaded_archives(), [])
        self.assertFalse(install.exists())
        self.assertFalse(list(install.parent.glob(install.name + ".stage-*")))
        self.assertFalse(install.with_name(install.name + ".publish.lock").exists())

    def reset_observations(self) -> None:
        for name in ("ops.log", "load-count"):
            (self.state / name).unlink(missing_ok=True)

    def test_present_roles_are_verified_but_neither_staged_nor_loaded(self):
        delivery, inventory, _sha = self.write_delivery()
        present = ("workspace", "documentserver")
        to_load = [role for role in ROLE_ORDER if role not in present]
        self.seed_present(inventory, present)
        install = self.root / "install-partial"
        hold = self.root / "hold-partial"
        hold.touch()
        process = subprocess.Popen(
            [sys.executable, str(self.release_script), "import",
             "--delivery", str(delivery), "--install-root", str(install)],
            env={**self.env, "FAKE_DOCKER_HOLD_LOAD": str(hold)},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 10
            while not (self.state / "entered-load").exists():
                self.assertIsNone(process.poll(), "import exited before load")
                self.assertLess(time.monotonic(), deadline, "load barrier not reached")
                time.sleep(0.02)
            stages = list(self.root.glob("install-partial.stage-*"))
            self.assertEqual(len(stages), 1)
            self.assertEqual(
                sorted(path.name for path in (stages[0] / "images").iterdir()),
                sorted(f"{role}.tar" for role in to_load),
            )
            hold.unlink()
            stdout, stderr = process.communicate(timeout=30)
        finally:
            hold.unlink(missing_ok=True)
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertEqual(self.summary_line(stderr), import_summary(present))
        self.assertEqual(
            [Path(archive).name for archive in self.loaded_archives()],
            [f"{role}.tar" for role in to_load],
        )
        recorded = ops(self.state)
        last_load = max(
            index for index, line in enumerate(recorded) if line.startswith("docker load ")
        )
        verified = {
            line.split()[-1] for line in recorded[last_load + 1:]
            if line.startswith("docker image inspect ")
        }
        self.assertEqual(
            verified, {record["reference"] for record in inventory["images"].values()}
        )
        self.assert_published(install, delivery)
        full_state = self.root / "full-state"
        full_state.mkdir()
        full = self.root / "install-full"
        result = self.import_with(
            self.release_script, delivery, full,
            extra_env={"FAKE_DOCKER_STATE": str(full_state)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.loaded_archives(full_state)), len(ROLE_ORDER))
        self.assertEqual(self.published_tree(install), self.published_tree(full))

    def test_present_role_removed_during_load_fails_without_publishing(self):
        delivery, inventory, _sha = self.write_delivery()
        self.seed_present(inventory, ("workspace",))
        install = self.root / "install-vanished"
        hold = self.root / "hold-vanished"
        hold.touch()
        process = subprocess.Popen(
            [sys.executable, str(self.release_script), "import",
             "--delivery", str(delivery), "--install-root", str(install)],
            env={**self.env, "FAKE_DOCKER_HOLD_LOAD": str(hold)},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 10
            while not (self.state / "entered-load").exists():
                self.assertIsNone(process.poll(), "import exited before load")
                self.assertLess(time.monotonic(), deadline, "load barrier not reached")
                time.sleep(0.02)
            images_path = self.state / "images.json"
            images = json.loads(images_path.read_text(encoding="utf-8"))
            images.pop(inventory["images"]["workspace"]["reference"])
            images_path.write_text(json.dumps(images), encoding="utf-8")
            hold.unlink()
            stdout, stderr = process.communicate(timeout=30)
        finally:
            hold.unlink(missing_ok=True)
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        self.assertNotEqual(process.returncode, 0, stdout + stderr)
        self.assertEqual(self.summary_line(stderr), import_summary(("workspace",)))
        self.assertIn("missing image", stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(
            [Path(archive).name for archive in self.loaded_archives()],
            [f"{role}.tar" for role in ROLE_ORDER if role != "workspace"],
        )
        self.assertFalse(install.exists())
        self.assertFalse(list(self.root.glob("install-vanished.stage-*")))
        self.assertFalse(install.with_name(install.name + ".publish.lock").exists())

    def test_corrupt_present_archive_fails_before_any_copy_or_load(self):
        delivery, inventory, _sha = self.write_delivery()
        self.seed_present(inventory, ("workspace",))
        archive = delivery / inventory["images"]["workspace"]["archive"]["path"]
        data = bytearray(archive.read_bytes())
        data[len(data) // 2] ^= 0x01
        archive.write_bytes(bytes(data))
        copies = self.root / "copies-corrupt.log"
        install = self.root / "install-corrupt-present"
        result = self.import_with(self.patched_release(self.copy_probe(copies)), delivery, install)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("workspace: archive checksum mismatch", result.stderr)
        self.assert_untouched(install, copies)

    def test_nothing_present_stages_and_loads_every_role_in_order(self):
        delivery, _inventory, _sha = self.write_delivery()
        copies = self.root / "copies-full.log"
        install = self.root / "install-nothing-present"
        result = self.import_with(self.patched_release(self.copy_probe(copies)), delivery, install)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.summary_line(result.stderr), import_summary(()))
        archives = self.loaded_archives()
        stage = Path(archives[0]).parent.parent
        self.assertTrue(stage.name.startswith("install-nothing-present.stage-"), stage)
        self.assertEqual(archives, [str(stage / "images" / f"{role}.tar") for role in ROLE_ORDER])
        self.assertEqual(
            copies.read_text(encoding="utf-8").splitlines(),
            [str(stage / name) for name in ("release.json", "source.bundle", "fonts.tar")]
            + [str(stage / "images" / f"{role}.tar") for role in ROLE_ORDER],
        )
        self.assert_published(install, delivery)

    def test_present_reference_with_foreign_identity_is_not_skipped(self):
        delivery, inventory, _sha = self.write_delivery()
        seed_images(self.state, {inventory["images"]["workspace"]["reference"]: {
            "Id": digest_for("foreign-workspace"), "Os": "linux", "Architecture": "amd64",
        }})
        before = (self.state / "images.json").read_bytes()
        install = self.root / "install-foreign-present"
        result = self.import_cmd(delivery, install)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.summary_line(result.stderr), import_summary(()))
        self.assertRegex(result.stderr, r"existing image \S+ conflicts with the release")
        self.assertEqual(self.loaded_archives(), [])
        self.assertFalse(install.exists())
        self.assertEqual((self.state / "images.json").read_bytes(), before)

    def test_present_reference_on_wrong_platform_is_not_skipped(self):
        delivery, inventory, _sha = self.write_delivery()
        seed_images(self.state, {
            inventory["images"]["workspace"]["reference"]: self.accepted_image(
                "workspace", architecture="arm64"
            ),
        })
        install = self.root / "install-wrong-platform"
        result = self.import_cmd(delivery, install)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.summary_line(result.stderr), import_summary(()))
        self.assertIn("image platform is linux/arm64, expected linux/amd64", result.stderr)
        self.assertEqual(self.loaded_archives(), [])
        self.assertFalse(install.exists())

    def test_free_space_precheck_refuses_before_any_copy_or_load(self):
        delivery, inventory, _sha = self.write_delivery()
        self.seed_present(inventory, ("workspace",))
        staged, unpacked = self.space_need(
            delivery, inventory, [role for role in ROLE_ORDER if role != "workspace"]
        )
        stage_root = self.root.resolve()
        docker_root = self.state / "docker-root"
        plenty = 1 << 50
        shared_free = staged + unpacked + SPACE_HEADROOM - 1
        self.assertLessEqual(staged + SPACE_HEADROOM, shared_free)
        self.assertLessEqual(unpacked + SPACE_HEADROOM, shared_free)
        cases = (
            ("stage", False, {stage_root: staged + SPACE_HEADROOM - 1, docker_root: plenty},
             stage_root, staged + SPACE_HEADROOM),
            ("docker-root", False, {stage_root: plenty, docker_root: unpacked + SPACE_HEADROOM - 1},
             docker_root, unpacked + SPACE_HEADROOM),
            ("shared", True, {stage_root: shared_free, docker_root: shared_free},
             stage_root, staged + unpacked + SPACE_HEADROOM),
        )
        for name, shared, free, path, needed in cases:
            with self.subTest(layout=name):
                self.reset_observations()
                copies = self.root / f"copies-space-{name}.log"
                install = self.root / f"install-space-{name}"
                script = self.space_script(free, shared=shared, extra=(self.copy_probe(copies),))
                result = self.import_with(script, delivery, install)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(str(path), result.stderr)
                self.assertIn(f"need {needed} bytes", result.stderr)
                self.assertIn(f"available {needed - 1} bytes", result.stderr)
                self.assert_untouched(install, copies)
        absent = self.root / "absent-docker-root"
        for name, reported, message in (
            ("absent", str(absent), f"Docker data root {absent}"),
            ("unreported", "", "docker info did not report DockerRootDir"),
        ):
            with self.subTest(docker_root=name):
                self.reset_observations()
                (self.state / "docker-info.json").write_text(
                    json.dumps({"DockerRootDir": reported}), encoding="utf-8"
                )
                copies = self.root / f"copies-space-{name}.log"
                install = self.root / f"install-space-{name}"
                script = self.patched_release(self.copy_probe(copies))
                result = self.import_with(script, delivery, install)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assert_untouched(install, copies)

    def test_free_space_exactly_at_the_requirement_imports(self):
        delivery, inventory, _sha = self.write_delivery()
        staged, unpacked = self.space_need(
            delivery, inventory, [role for role in ROLE_ORDER if role != "workspace"]
        )
        stage_root = self.root
        docker_root = self.state / "docker-root"
        shared_free = staged + unpacked + SPACE_HEADROOM
        cases = (
            ("separate", False,
             {stage_root: staged + SPACE_HEADROOM, docker_root: unpacked + SPACE_HEADROOM}),
            ("shared", True, {stage_root: shared_free, docker_root: shared_free}),
        )
        for name, shared, free in cases:
            with self.subTest(layout=name):
                self.reset_observations()
                self.seed_present(inventory, ("workspace",))
                install = self.root / f"install-boundary-{name}"
                result = self.import_with(self.space_script(free, shared=shared), delivery, install)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(self.loaded_archives()), len(ROLE_ORDER) - 1)
                self.assert_published(install, delivery)

    def test_all_present_import_copies_and_loads_nothing_and_still_verifies(self):
        delivery, inventory, _sha = self.write_delivery()
        seed_images(self.state)
        staged, unpacked = self.space_need(delivery, inventory, ())
        self.assertEqual(unpacked, 0)
        stage_root = self.root
        docker_root = self.state / "docker-root"
        marker = self.root / "verified-loaded-images"
        anchor = "def verify_loaded_images(payload: dict) -> None:\n"
        verify_probe = (anchor, anchor + f"    Path({str(marker)!r}).write_text('ran')\n")
        short = self.root / "install-all-present-short"
        result = self.import_with(
            self.space_script(
                {stage_root: staged + SPACE_HEADROOM, docker_root: SPACE_HEADROOM - 1},
                shared=False,
            ),
            delivery, short,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"need {SPACE_HEADROOM} bytes", result.stderr)
        self.assertFalse(short.exists())
        self.reset_observations()
        copies = self.root / "copies-all-present.log"
        install = self.root / "install-all-present"
        result = self.import_with(
            self.space_script(
                {stage_root: staged + SPACE_HEADROOM, docker_root: SPACE_HEADROOM},
                shared=False, extra=(self.copy_probe(copies), verify_probe),
            ),
            delivery, install,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.summary_line(result.stderr), import_summary(ROLE_ORDER))
        self.assertEqual(self.loaded_archives(), [])
        self.assertEqual(
            [Path(line).name for line in copies.read_text(encoding="utf-8").splitlines()],
            ["release.json", "source.bundle", "fonts.tar"],
        )
        self.assertEqual(marker.read_text(), "ran")
        self.assert_published(install, delivery)



if __name__ == "__main__":
    unittest.main()
