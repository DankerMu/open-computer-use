# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Bounded discovery through real filesystem and actual FastMCP consumers."""
import asyncio
import errno
import fcntl
import gc
import importlib
import json
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import AnyUrl

SERVER = Path(__file__).resolve().parents[2] / "computer-use-server"
MODULES = {
    "uploads", "mcp_resources", "mcp_tools", "docker_manager", "security",
    "context_vars", "system_prompt", "skill_manager", "cli_runtime", "outputs_broker",
}


@pytest.fixture
def discovery(tmp_path, monkeypatch):
    snapshot = {name: sys.modules[name] for name in MODULES if name in sys.modules}
    for name in MODULES:
        sys.modules.pop(name, None)
    monkeypatch.syspath_prepend(str(SERVER))
    monkeypatch.setenv("BASE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DOCKER_HOST", "unix:///tmp/ocu-acceptance-no-docker.sock")
    monkeypatch.setenv("DOCKER_SOCKET", "unix:///tmp/ocu-acceptance-no-docker.sock")
    monkeypatch.setenv("OCU_INTERNAL_TOKEN", "discovery-test-token")
    monkeypatch.setenv("MCP_API_KEY", "discovery-test-mcp-key")
    try:
        uploads = importlib.import_module("uploads")
        mr = importlib.import_module("mcp_resources")
        yield SimpleNamespace(root=tmp_path, uploads=uploads, mr=mr, mcp=mr.mcp)
    finally:
        for name in MODULES:
            sys.modules.pop(name, None)
        sys.modules.update(snapshot)


def put(world, chat, name, body=b"keep"):
    path = world.root / chat / "outputs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def sync(world, chat):
    return asyncio.run(world.mr.sync_chat_resources(chat))


def listed(world):
    return {str(resource.uri) for resource in asyncio.run(world.mcp.list_resources())}


def read(world, uri):
    return list(asyncio.run(world.mcp.read_resource(AnyUrl(uri))))[0].content


def rejected(world, chat, reason):
    # Resolve the existing callable before inspecting the new failure contract:
    # historical-source controls fail behaviorally rather than on missing symbols.
    with pytest.raises(Exception) as raised:
        sync(world, chat)
    assert getattr(raised.value, "reason", None) == reason
    assert str(world.root) not in str(raised.value)


def seed(world, chat="bounded"):
    put(world, chat, "keep.txt")
    assert sync(world, chat) == 1
    return listed(world)


def fd_inventory():
    directory = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    live = set()
    for name in os.listdir(directory):
        fd = int(name)
        try:
            fcntl.fcntl(fd, fcntl.F_GETFD)
            live.add(fd)
        except OSError as exc:
            assert exc.errno == errno.EBADF
    return live


def test_real_file_and_global_limits_allow_exact_replacement_and_preserve_other_chat(
        discovery, monkeypatch):
    w = discovery
    expected = set()
    for chat in ("first", "second"):
        for index in range(10_000):
            put(w, chat, f"file-{index:05d}.txt", b"payload")
            expected.add(f"file://uploads/{chat}/file-{index:05d}.txt")
        assert sync(w, chat) == 10_000
    assert listed(w) == expected
    assert sync(w, "first") == 10_000
    assert listed(w) == expected

    # SDK constructor is an external boundary: a rejected registration must not
    # allocate even one FunctionResource before checking global capacity.
    from mcp.server.fastmcp.resources import FunctionResource
    original = FunctionResource.__init__
    constructed = []

    def record_construction(self, *args, **kwargs):
        constructed.append(kwargs.get("uri"))
        return original(self, *args, **kwargs)

    put(w, "third", "excess.txt")
    with monkeypatch.context() as patches:
        patches.setattr(FunctionResource, "__init__", record_construction)
        rejected(w, "third", "registry_limit")
    assert constructed == []
    assert listed(w) == expected
    excess = put(w, "first", "overflow.txt")
    with monkeypatch.context() as patches:
        patches.setattr(FunctionResource, "__init__", record_construction)
        rejected(w, "first", "file_limit")
    assert constructed == []
    assert listed(w) == expected
    excess.unlink()
    assert read(w, "file://uploads/second/file-00000.txt") == "payload"

    # Replacing ten thousand with one frees precisely that chat's contribution.
    shutil.rmtree(w.root / "first" / "outputs")
    put(w, "first", "replacement.txt", b"replacement")
    assert sync(w, "first") == 1
    assert sync(w, "third") == 1
    assert listed(w) == {
        *(f"file://uploads/second/file-{index:05d}.txt" for index in range(10_000)),
        "file://uploads/first/replacement.txt", "file://uploads/third/excess.txt",
    }


def test_failed_global_replacement_preserves_both_predecessors(discovery, monkeypatch):
    w = discovery
    seed(w, "first")
    before = seed(w, "second")
    put(w, "first", "extra.txt")
    with monkeypatch.context() as patches:
        patches.setattr(w.mr, "MAX_WORKSPACE_RESOURCES", 2)
        rejected(w, "first", "registry_limit")
    assert listed(w) == before
    assert read(w, "file://uploads/second/keep.txt") == "keep"
    assert sync(w, "first") == 2


@pytest.mark.parametrize("kind", ["hidden", "symlink", "special", "directory"])
def test_entries_count_before_filter_and_excess_stat_is_not_performed(
        discovery, monkeypatch, kind):
    w = discovery
    before = seed(w)
    root = w.root / "bounded" / "outputs"
    if kind == "hidden":
        (root / ".hidden").mkdir()
        (root / ".hidden" / "not-enumerated.txt").write_bytes(b"hidden")
    elif kind == "symlink":
        (root / "link").symlink_to("keep.txt")
    elif kind == "special":
        os.mkfifo(root / "pipe")
    else:
        (root / "child").mkdir()
    with monkeypatch.context() as patches:
        patches.setattr(w.uploads, "MAX_DISCOVERY_ENTRIES", 2)
        assert sync(w, "bounded") == 1
        assert listed(w) == before
    (root / ".extra").write_bytes(b"hidden excess")
    original_scandir = os.scandir
    inspections = []

    class Entry:
        def __init__(self, entry, ordinal):
            self.entry, self.ordinal = entry, ordinal

        def __getattr__(self, name):
            if self.ordinal > 2:
                inspections.append(name)
            return getattr(self.entry, name)

    class Listing:
        def __init__(self, fd):
            self.listing = original_scandir(fd)

        def __enter__(self):
            self.listing.__enter__()
            return self

        def __iter__(self):
            for ordinal, entry in enumerate(self.listing, 1):
                yield Entry(entry, ordinal)

        def __exit__(self, *args):
            return self.listing.__exit__(*args)

    with monkeypatch.context() as patches:
        patches.setattr(w.uploads, "MAX_DISCOVERY_ENTRIES", 2)
        patches.setattr(os, "scandir", Listing)
        rejected(w, "bounded", "entry_limit")
    assert inspections == []
    assert listed(w) == before


@pytest.mark.parametrize("shape,paths,exact_fds", [
    ("wide", ["a", "b", "c"], 5),
    ("deep", ["a/b/c/d"], 3),
    ("mixed", ["a/deep/x", "b", "c"], 5),
])
def test_directory_and_descriptor_exact_limits_unwind_owned_fds(
        discovery, monkeypatch, shape, paths, exact_fds):
    w = discovery
    root = put(w, "bounded", "keep.txt").parent
    for path in paths:
        (root / path).mkdir(parents=True)
    directories = 1 + sum(1 for path in root.rglob("*") if path.is_dir())
    root_fd = os.open(root, w.uploads._DIRECTORY_FLAGS)
    before_fds = fd_inventory()
    try:
        with monkeypatch.context() as patches:
            patches.setattr(w.uploads, "MAX_DISCOVERY_DIRECTORIES", directories)
            patches.setattr(w.uploads, "MAX_DISCOVERY_DIRECTORY_FDS", exact_fds)
            assert [entry.rel_path for entry in w.uploads._scan_visible_files(root_fd)] == ["keep.txt"]
        assert fd_inventory() == before_fds
        with monkeypatch.context() as patches:
            patches.setattr(w.uploads, "MAX_DISCOVERY_DIRECTORIES", directories - 1)
            with pytest.raises(Exception) as raised:
                w.uploads._scan_visible_files(root_fd)
            assert raised.value.reason == "directory_limit"
        assert fd_inventory() == before_fds
        with monkeypatch.context() as patches:
            patches.setattr(w.uploads, "MAX_DISCOVERY_DIRECTORY_FDS", exact_fds - 1)
            with pytest.raises(Exception) as raised:
                w.uploads._scan_visible_files(root_fd)
            assert raised.value.reason == "descriptor_limit"
        assert fd_inventory() == before_fds
        assert os.fstat(root_fd).st_ino == root.stat().st_ino
    finally:
        os.close(root_fd)


def test_descriptor_limit_rejects_iterator_before_acquisition(discovery, monkeypatch):
    w = discovery
    before = seed(w)
    acquisitions = []
    original = os.scandir

    def record(fd):
        acquisitions.append(fd)
        return original(fd)

    with monkeypatch.context() as patches:
        patches.setattr(w.uploads, "MAX_DISCOVERY_DIRECTORY_FDS", 1)
        patches.setattr(os, "scandir", record)
        rejected(w, "bounded", "descriptor_limit")
    assert acquisitions == []
    assert listed(w) == before
    assert sync(w, "bounded") == 1


@pytest.mark.parametrize("budget,reason,opens", [
    ("MAX_DISCOVERY_DIRECTORIES", "directory_limit", 0),
    ("MAX_DISCOVERY_DIRECTORY_FDS", "descriptor_limit", 2),
])
def test_limits_reject_excess_child_before_acquisition(
        discovery, monkeypatch, budget, reason, opens):
    w = discovery
    before = seed(w)
    root = w.root / "bounded" / "outputs"
    for name in ("a", "b", "c"):
        (root / name).mkdir()
    original = os.open
    acquired = []
    inventory = fd_inventory()

    def opening(path, flags, *args, **kwargs):
        fd = original(path, flags, *args, **kwargs)
        if path in ("a", "b", "c"):
            acquired.append(fd)
        return fd

    with monkeypatch.context() as patches:
        patches.setattr(w.uploads, budget, 1 if reason == "directory_limit" else 3)
        patches.setattr(os, "open", opening)
        rejected(w, "bounded", reason)
    assert len(acquired) == opens
    assert fd_inventory() == inventory
    assert listed(w) == before


@pytest.mark.parametrize("stage", ["root-open", "root-fstat", "child-open", "child-fstat", "iterator-create", "iterator-next", "entry-stat"])
@pytest.mark.parametrize("error_number,reason", [
    (errno.EIO, "io_error"), (errno.EMFILE, "descriptor_limit"),
    (errno.ENFILE, "descriptor_limit"),
])
def test_os_failures_preserve_registry_close_fds_and_recover(
        discovery, monkeypatch, stage, error_number, reason):
    w = discovery
    before = seed(w)
    root = w.root / "bounded" / "outputs"
    (root / "child").mkdir()
    (root / "child" / "nested.txt").write_bytes(b"nested")
    original_open, original_fstat, original_scandir = os.open, os.fstat, os.scandir
    child_fds = set()
    root_fds = set()
    inventory = fd_inventory()

    def fail():
        raise OSError(error_number, f"secret-host-path-{w.root}")

    def opening(path, flags, *args, **kwargs):
        if (stage == "root-open" and path == "outputs") or (stage == "child-open" and path == "child"):
            fail()
        fd = original_open(path, flags, *args, **kwargs)
        if path == "child":
            child_fds.add(fd)
        if path == "outputs":
            root_fds.add(fd)
        return fd

    def fstat(fd):
        if ((stage == "child-fstat" and fd in child_fds)
                or (stage == "root-fstat" and fd in root_fds)):
            fail()
        return original_fstat(fd)

    class Entry:
        def __init__(self, entry):
            self.entry = entry

        def __getattr__(self, name):
            return getattr(self.entry, name)

        def stat(self, **kwargs):
            if self.entry.name == "keep.txt":
                fail()
            return self.entry.stat(**kwargs)

    class Listing:
        def __init__(self, fd):
            self.listing = original_scandir(fd)

        def __enter__(self):
            self.listing.__enter__()
            return self

        def __iter__(self):
            if stage == "iterator-next":
                fail()
            for entry in self.listing:
                yield Entry(entry) if stage == "entry-stat" else entry

        def __exit__(self, *args):
            return self.listing.__exit__(*args)

    def scandir(fd):
        if stage == "iterator-create":
            fail()
        return Listing(fd)

    with monkeypatch.context() as patches:
        patches.setattr(os, "open", opening)
        patches.setattr(os, "fstat", fstat)
        patches.setattr(os, "scandir", scandir)
        rejected(w, "bounded", reason)
    assert fd_inventory() == inventory
    assert listed(w) == before
    assert read(w, "file://uploads/bounded/keep.txt") == "keep"
    assert sync(w, "bounded") == 2


@pytest.mark.parametrize("failure_kind", ["construction", "copy", "publication"])
def test_preparation_failure_is_redacted_and_preserves_predecessor(
        discovery, monkeypatch, failure_kind):
    w = discovery
    before = seed(w)
    put(w, "bounded", "next.txt")
    from mcp.server.fastmcp.resources import FunctionResource

    def fail(self, *args, **kwargs):
        raise RuntimeError(f"secret {w.root}")

    class FailingCopy(dict):
        def copy(self):
            raise RuntimeError(f"secret {w.root}")

    manager_type = type(w.mcp._resource_manager)
    assign = manager_type.__setattr__

    def refuse_publication(self, name, value):
        if name == "_resources":
            raise RuntimeError(f"secret {w.root}")
        return assign(self, name, value)

    with monkeypatch.context() as patches:
        if failure_kind == "construction":
            patches.setattr(FunctionResource, "__init__", fail)
        elif failure_kind == "copy":
            # Fault the SDK-owned registry at the external publication boundary.
            patches.setattr(w.mcp._resource_manager, "_resources", FailingCopy(w.mcp._resource_manager._resources))
        else:
            patches.setattr(manager_type, "__setattr__", refuse_publication)
        rejected(w, "bounded", "internal_error")
        assert listed(w) == before
    assert sync(w, "bounded") == 2


def test_empty_and_failed_chats_do_not_accumulate_records(discovery, monkeypatch):
    w = discovery
    for index in range(20):
        assert sync(w, f"empty-{index}") == 0
    assert w.mr._chat_uris == {}
    with monkeypatch.context() as patches:
        patches.setattr(w.uploads, "MAX_DISCOVERY_FILES", 0)
        for index in range(20):
            put(w, f"failed-{index}", "one.txt")
            rejected(w, f"failed-{index}", "file_limit")
    assert w.mr._chat_uris == {}
    seed(w)
    shutil.rmtree(w.root / "bounded" / "outputs")
    assert sync(w, "bounded") == 0
    assert w.mr._chat_uris == {}
    assert listed(w) == set()


@pytest.mark.parametrize("phase", ["queued", "running", "start-race"])
@pytest.mark.parametrize("worker_fails", [False, True], ids=["success", "failure"])
def test_cancelled_discovery_keeps_admission_until_worker_ends_and_recovers(
        discovery, monkeypatch, phase, worker_fails):
    w = discovery
    before = seed(w)
    put(w, "bounded", "next.txt")

    async def scenario():
        loop = asyncio.get_running_loop()
        submitted, started = asyncio.Event(), asyncio.Event()
        occupy, release_scan = threading.Event(), threading.Event()
        errors = []
        loop.set_exception_handler(lambda _loop, context: errors.append(context))

        class Executor(ThreadPoolExecutor):
            def submit(self, fn, *args, **kwargs):
                future = super().submit(fn, *args, **kwargs)
                self.discovery_future = future
                submitted.set()
                return future

        executor = Executor(max_workers=1)
        loop.set_default_executor(executor)
        blocker = ThreadPoolExecutor.submit(executor, occupy.wait, 10)
        original = os.scandir

        def gated_scandir(fd):
            loop.call_soon_threadsafe(started.set)
            if not release_scan.wait(10):
                raise AssertionError("test did not release filesystem gate")
            if worker_fails:
                raise OSError(errno.EIO, "secret-host-path")
            return original(fd)

        task = None
        with monkeypatch.context() as patches:
            patches.setattr(os, "scandir", gated_scandir)
            try:
                task = asyncio.create_task(w.mr.sync_chat_resources("bounded"))
                await asyncio.wait_for(submitted.wait(), 5)
                worker = executor.discovery_future
                if phase == "running":
                    occupy.set()
                    await asyncio.wait_for(started.wait(), 5)
                elif phase == "start-race":
                    occupy.set()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                with pytest.raises(Exception) as raised:
                    await w.mr.sync_chat_resources("other")
                assert getattr(raised.value, "reason", None) == "busy"
                assert {str(r.uri) for r in await w.mcp.list_resources()} == before
                occupy.set()
                await asyncio.wait_for(started.wait(), 5)
                release_scan.set()
                if worker_fails:
                    with pytest.raises(Exception) as raised:
                        await asyncio.wait_for(asyncio.wrap_future(worker), 5)
                    assert getattr(raised.value, "reason", None) == "io_error"
                else:
                    count, _changed = await asyncio.wait_for(asyncio.wrap_future(worker), 5)
                    assert count == 2
            finally:
                occupy.set()
                release_scan.set()
                blocker.result(timeout=10)
                if submitted.is_set():
                    try:
                        await asyncio.wait_for(asyncio.wrap_future(executor.discovery_future), 5)
                    except Exception:
                        pass
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
        del task
        gc.collect()
        await asyncio.sleep(0)
        assert errors == []
        assert await w.mr.sync_chat_resources("bounded") == 2
        assert {str(r.uri) for r in await w.mcp.list_resources()} == {
            "file://uploads/bounded/keep.txt", "file://uploads/bounded/next.txt",
        }
        chunks = list(await w.mcp.read_resource(AnyUrl("file://uploads/bounded/next.txt")))
        assert chunks[0].content == "keep"

    asyncio.run(scenario())


def test_executor_submission_failure_releases_admission(discovery):
    w = discovery
    seed(w)

    async def scenario():
        class RefusingExecutor(ThreadPoolExecutor):
            def submit(self, *args, **kwargs):
                raise RuntimeError("secret executor failure")

        loop = asyncio.get_running_loop()
        refused = RefusingExecutor(max_workers=1)
        loop.set_default_executor(refused)
        try:
            with pytest.raises(Exception) as raised:
                await w.mr.sync_chat_resources("bounded")
            assert getattr(raised.value, "reason", None) == "internal_error"
        finally:
            loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
            refused.shutdown()
        assert await w.mr.sync_chat_resources("bounded") == 1
        chunks = list(await w.mcp.read_resource(AnyUrl("file://uploads/bounded/keep.txt")))
        assert chunks[0].content == "keep"

    asyncio.run(scenario())


def test_actual_sdk_lists_are_atomic_during_sync_thread_replacements(discovery, monkeypatch):
    w = discovery
    seed(w, "unrelated")
    root = w.root / "bounded" / "outputs"
    alternate = w.root / "alternate"
    alternate.mkdir()
    old = {f"file://uploads/bounded/old-{index:04d}.txt" for index in range(1_000)}
    new = {f"file://uploads/bounded/new-{index:04d}.txt" for index in range(1_000)}
    unrelated = {"file://uploads/unrelated/keep.txt"}
    for index in range(1_000):
        put(w, "bounded", f"old-{index:04d}.txt")
        (alternate / f"new-{index:04d}.txt").write_bytes(b"new")
    assert sync(w, "bounded") == 1_000
    started, release = threading.Event(), threading.Event()
    original = os.scandir

    def scanner(fd):
        started.set()
        assert release.wait(10)
        return original(fd)

    def publisher():
        previous = sys.getswitchinterval()
        sys.setswitchinterval(0.000001)
        try:
            for _ in range(12):
                temporary = w.root / "swap"
                root.rename(temporary)
                alternate.rename(root)
                temporary.rename(alternate)
                assert w.mr.sync_chat_resources_sync("bounded") == 1_000
        finally:
            sys.setswitchinterval(previous)

    async def observe(future):
        observations = 0
        while not future.done():
            uris = {str(r.uri) for r in await w.mcp.list_resources()}
            assert uris in (old | unrelated, new | unrelated)
            chunks = list(await w.mcp.read_resource(AnyUrl("file://uploads/unrelated/keep.txt")))
            assert chunks[0].content == "keep"
            observations += 1
            await asyncio.sleep(0)
        future.result(timeout=10)
        assert observations > 0

    with ThreadPoolExecutor(max_workers=1) as executor:
        with monkeypatch.context() as patches:
            patches.setattr(os, "scandir", scanner)
            future = executor.submit(publisher)
            try:
                assert started.wait(5)
                release.set()
                asyncio.run(observe(future))
            finally:
                release.set()
                future.result(timeout=10)
    assert listed(w) == old | unrelated


@pytest.mark.parametrize("interruption", [OSError, KeyboardInterrupt])
def test_child_iterator_failure_releases_duplicate_and_frontier_not_borrowed_root(
        discovery, monkeypatch, interruption):
    w = discovery
    root = put(w, "bounded", "keep.txt").parent
    for name in ("a", "b", "c"):
        (root / name).mkdir()
    root_fd = os.open(root, w.uploads._DIRECTORY_FLAGS)
    inventory = fd_inventory()
    original = os.scandir
    duplicates = []

    class FailingListing:
        def __init__(self, fd):
            before = fd_inventory()
            self.listing = original(fd)
            duplicates.extend(fd_inventory() - before)

        def __enter__(self):
            self.listing.__enter__()
            return self

        def __iter__(self):
            if interruption is OSError:
                raise OSError(errno.EIO, "injected enumeration failure")
            raise KeyboardInterrupt()
            yield

        def __exit__(self, *args):
            return self.listing.__exit__(*args)

    def failing(fd):
        return original(fd) if fd == root_fd else FailingListing(fd)

    try:
        with monkeypatch.context() as patches:
            patches.setattr(os, "scandir", failing)
            expected = w.uploads.DiscoveryError if interruption is OSError else interruption
            with pytest.raises(expected):
                w.uploads._scan_visible_files(root_fd)
        assert len(duplicates) == 1
        assert fd_inventory() == inventory
        assert os.fstat(root_fd).st_ino == root.stat().st_ino
        assert [entry.rel_path for entry in w.uploads._scan_visible_files(root_fd)] == ["keep.txt"]
    finally:
        os.close(root_fd)


_RLIMIT_PROBE = r'''
import asyncio
import errno
import fcntl
import json
import os
import resource
import shutil
import sys
from pathlib import Path
sys.path.insert(0, os.environ["OCU_SERVER_DIR"])
import mcp_resources as mr
import uploads

root = Path(os.environ["BASE_DATA_DIR"]) / "bounded" / "outputs"
root.mkdir(parents=True)
(root / "keep.txt").write_bytes(b"keep")
assert mr.sync_chat_resources_sync("bounded") == 1
before = {str(r.uri) for r in asyncio.run(mr.mcp.list_resources())}
shape, mode = os.environ["OCU_SHAPE"], os.environ["OCU_MODE"]
if shape == "deep":
    (root / "/".join(["deep"] * 40)).mkdir(parents=True)
else:
    for index in range(100):
        directory = root / ("wide-%03d" % index)
        directory.mkdir()
        if shape == "mixed" and index < 4:
            (directory / "/".join(["deep"] * 10)).mkdir(parents=True)
directories = [root, *(p for p in root.rglob("*") if p.is_dir())]
identities = {(p.stat().st_dev, p.stat().st_ino) for p in directories}
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (64, hard))
if mode == "policy":
    uploads.MAX_DISCOVERY_DIRECTORY_FDS = 8
peak = 0

def inventory():
    live = set()
    for fd in range(64):
        try:
            fcntl.fcntl(fd, fcntl.F_GETFD)
            live.add(fd)
        except OSError as exc:
            assert exc.errno == errno.EBADF
    return live

def sample():
    global peak
    count = 0
    for fd in inventory():
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) in identities:
            count += 1
    peak = max(peak, count)

opened, scandir = os.open, os.scandir
def sampled_open(*args, **kwargs):
    fd = opened(*args, **kwargs)
    sample()
    return fd
def sampled_scandir(*args, **kwargs):
    iterator = scandir(*args, **kwargs)
    sample()
    return iterator
borrowed = opened(root, uploads._DIRECTORY_FLAGS)
before_fds = inventory()
os.open, os.scandir = sampled_open, sampled_scandir
reason = None
try:
    if shape == "deep":
        assert mr.sync_chat_resources_sync("bounded") == 1
        assert [e.rel_path for e in uploads._scan_visible_files(borrowed)] == ["keep.txt"]
    else:
        for operation in (
            lambda: mr.sync_chat_resources_sync("bounded"),
            lambda: uploads._scan_visible_files(borrowed),
        ):
            try:
                operation()
            except uploads.DiscoveryError as exc:
                assert exc.reason == "descriptor_limit"
                reason = exc.reason
            else:
                raise AssertionError("wide frontier must explicitly reject")
    assert inventory() == before_fds
    assert os.fstat(borrowed).st_ino == root.stat().st_ino
finally:
    os.open, os.scandir = opened, scandir
    os.close(borrowed)
if shape == "deep":
    assert peak == 4  # One test-borrowed root plus the transaction's peak of three.
elif mode == "policy":
    assert peak == 9  # One test-borrowed root plus the transaction's exact eight.
else:
    assert 9 < peak <= 64
assert {str(r.uri) for r in asyncio.run(mr.mcp.list_resources())} == before
for child in root.iterdir():
    if child.is_dir():
        shutil.rmtree(child)
assert mr.sync_chat_resources_sync("bounded") == 1
chunks = list(asyncio.run(mr.mcp.read_resource("file://uploads/bounded/keep.txt")))
assert chunks[0].content == "keep"
print(json.dumps({"shape": shape, "mode": mode, "reason": reason, "recovered": True, "peak": peak}))
'''


@pytest.mark.parametrize("shape", ["wide", "deep", "mixed"])
@pytest.mark.parametrize("mode", ["policy", "os"])
def test_constrained_rlimit_real_tree_inventory_preserves_snapshot_and_recovers(
        tmp_path, shape, mode):
    environment = {
        "PATH": os.defpath,
        "BASE_DATA_DIR": str(tmp_path),
        "DOCKER_HOST": "unix:///tmp/ocu-acceptance-no-docker.sock",
        "DOCKER_SOCKET": "unix:///tmp/ocu-acceptance-no-docker.sock",
        "OCU_INTERNAL_TOKEN": "discovery-test-token",
        "MCP_API_KEY": "discovery-test-mcp-key",
        "OCU_SERVER_DIR": str(SERVER),
        "OCU_SHAPE": shape,
        "OCU_MODE": mode,
    }
    result = subprocess.run(
        [sys.executable, "-c", _RLIMIT_PROBE], env=environment,
        capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout.splitlines()[-1])
    assert evidence["shape"] == shape
    assert evidence["mode"] == mode
    assert evidence["reason"] == (None if shape == "deep" else "descriptor_limit")
    assert evidence["recovered"] is True


def test_file_limit_rejects_before_excess_metadata_collection(discovery, monkeypatch):
    w = discovery
    before = seed(w)
    put(w, "bounded", "extra.txt")
    original = os.scandir
    inspected = []

    class Entry:
        def __init__(self, entry):
            self.entry = entry

        def __getattr__(self, name):
            return getattr(self.entry, name)

        def stat(self, **kwargs):
            inspected.append(self.entry.name)
            return self.entry.stat(**kwargs)

    class Listing:
        def __init__(self, fd):
            self.listing = original(fd)

        def __enter__(self):
            self.listing.__enter__()
            return self

        def __iter__(self):
            for entry in self.listing:
                yield Entry(entry)

        def __exit__(self, *args):
            return self.listing.__exit__(*args)

    with monkeypatch.context() as patches:
        patches.setattr(w.uploads, "MAX_DISCOVERY_FILES", 1)
        patches.setattr(os, "scandir", Listing)
        rejected(w, "bounded", "file_limit")
    assert len(inspected) == 1
    assert listed(w) == before


def test_notification_failure_does_not_undo_published_discovery(discovery):
    from mcp.server.lowlevel.server import request_ctx

    w = discovery
    put(w, "bounded", "keep.txt")
    notifications = []

    class Session:
        async def send_resource_list_changed(self):
            notifications.append("changed")
            raise OSError("secret notification failure")

    token = request_ctx.set(SimpleNamespace(session=Session()))
    try:
        assert sync(w, "bounded") == 1
        assert sync(w, "bounded") == 1
    finally:
        request_ctx.reset(token)
    assert notifications == ["changed"]
    assert listed(w) == {"file://uploads/bounded/keep.txt"}
    assert read(w, "file://uploads/bounded/keep.txt") == "keep"
