from __future__ import annotations

import errno
import os
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from captain_hook.daemon.tree import DirectoryTreeCache


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "hooks"
    (root / "nested").mkdir(parents=True)
    (root / "other").mkdir()
    (root / "config.py").write_text("one")
    (root / "nested" / "hook.py").write_text("two")
    (root / "other" / "data.txt").write_text("three")
    return root


@pytest.fixture
def scans(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    calls: list[Path] = []
    original = os.scandir

    def counted(path: str | Path):
        calls.append(Path(path))
        return original(path)

    monkeypatch.setattr(os, "scandir", counted)
    return calls


def names(cache: DirectoryTreeCache, root: Path, *, fresh: bool = False) -> list[str]:
    return [entry[0] for entry in cache.entries(root, fresh=fresh)]


def test_warm_calls_reuse_all_listings_and_restat_file_edits(tree: Path, scans: list[Path]) -> None:
    cache = DirectoryTreeCache()
    first = cache.entries(tree)
    assert Counter(scans) == Counter([tree, tree / "nested", tree / "other"])
    scans.clear()
    for _ in range(20):
        assert cache.entries(tree) == first
    hook = tree / "nested" / "hook.py"
    parent_before = hook.parent.stat()
    before = hook.stat()
    hook.write_text("new")
    os.utime(hook, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = cache.entries(tree)
    parent_after = hook.parent.stat()
    assert (parent_after.st_mtime_ns, parent_after.st_ctime_ns) == (
        parent_before.st_mtime_ns,
        parent_before.st_ctime_ns,
    )
    assert after != first
    assert scans == []


@pytest.mark.parametrize("mutation", ["add", "delete", "rename"])
def test_only_changed_directory_is_enumerated(tree: Path, scans: list[Path], mutation: str) -> None:
    cache = DirectoryTreeCache()
    cache.entries(tree)
    scans.clear()
    nested = tree / "nested"
    hook = nested / "hook.py"
    match mutation:
        case "add":
            (nested / "new.py").write_text("new")
            expected = ["config.py", "nested/hook.py", "nested/new.py", "other/data.txt"]
        case "delete":
            hook.unlink()
            expected = ["config.py", "other/data.txt"]
        case "rename":
            hook.rename(nested / "renamed.py")
            expected = ["config.py", "nested/renamed.py", "other/data.txt"]
    assert names(cache, tree) == expected
    assert scans == [nested]


def test_directory_replacement_drops_old_descendants(tree: Path, scans: list[Path]) -> None:
    cache = DirectoryTreeCache()
    (tree / "nested" / "deep").mkdir()
    (tree / "nested" / "deep" / "old.py").write_text("old")
    cache.entries(tree)
    scans.clear()
    (tree / "nested").rename(tree.parent / "former")
    (tree / "nested").mkdir()
    (tree / "nested" / "new.py").write_text("new")
    assert names(cache, tree) == ["config.py", "nested/new.py", "other/data.txt"]
    assert Counter(scans) == Counter([tree, tree / "nested"])


def test_ignored_entries_and_nested_symlinks_do_not_enter_the_tree(tree: Path, scans: list[Path]) -> None:
    (tree / "__pycache__").mkdir()
    (tree / "__pycache__" / "hidden.py").write_text("hidden")
    (tree / "skip.pyc").write_text("bytecode")
    (tree / "skip.pyo").write_text("bytecode")
    (tree / "cycle").symlink_to(tree, target_is_directory=True)
    (tree / "linked.py").symlink_to(tree / "config.py")
    (tree / "absent.py").symlink_to(tree / "missing")
    cache = DirectoryTreeCache()
    assert names(cache, tree) == ["config.py", "nested/hook.py", "other/data.txt"]
    assert Counter(scans) == Counter([tree, tree / "nested", tree / "other"])


def test_explicit_symlink_root_tracks_target_and_retargeting(tree: Path, scans: list[Path]) -> None:
    link = tree.parent / "linked-hooks"
    link.symlink_to(tree, target_is_directory=True)
    cache = DirectoryTreeCache()
    assert names(cache, link) == ["config.py", "nested/hook.py", "other/data.txt"]
    scans.clear()
    assert len(cache.entries(link)) == 3
    assert scans == []
    target = tree.parent / "replacement"
    target.mkdir()
    (target / "new.py").write_text("new")
    link.unlink()
    link.symlink_to(target, target_is_directory=True)
    assert names(cache, link) == ["new.py"]
    assert scans == [link]


def test_fresh_forces_all_listings(tree: Path, scans: list[Path]) -> None:
    cache = DirectoryTreeCache()
    expected = cache.entries(tree)
    scans.clear()
    assert cache.entries(tree, fresh=True) == expected
    assert Counter(scans) == Counter([tree, tree / "nested", tree / "other"])


def test_root_capacity_evicts_only_idle_manifests(tmp_path: Path, scans: list[Path]) -> None:
    roots = [tmp_path / name for name in ("one", "two", "three")]
    for root in roots:
        root.mkdir()
    cache = DirectoryTreeCache(maxsize=2)
    for root in roots:
        assert cache.entries(root) == ()
    assert len(cache._roots) == 2
    scans.clear()
    assert cache.entries(roots[1]) == ()
    assert scans == []
    assert cache.entries(roots[0]) == ()
    assert scans == [roots[0]]


@pytest.mark.parametrize("failure", [FileNotFoundError, PermissionError])
def test_stat_failure_propagates_and_discards_the_manifest(
    tree: Path, scans: list[Path], monkeypatch: pytest.MonkeyPatch, failure: type[OSError]
) -> None:
    cache = DirectoryTreeCache()
    cache.entries(tree)
    original = Path.lstat
    target = tree / "nested" / "hook.py"
    problem = failure("injected file-stat failure")

    def failed(path: Path):
        if path == target:
            raise problem
        return original(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", failed)
        with pytest.raises(failure) as caught:
            cache.entries(tree)
        assert caught.value is problem
    scans.clear()
    assert len(cache.entries(tree)) == 3
    assert Counter(scans) == Counter([tree, tree / "nested", tree / "other"])


def test_mutation_during_listing_cannot_publish_absence(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = DirectoryTreeCache()
    original = os.scandir

    class ChangedListing:
        def __enter__(self):
            self.entries = original(tree)
            return self.entries.__enter__()

        def __exit__(self, *args):
            self.entries.__exit__(*args)
            (tree / "arrived.py").write_text("new")

    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", lambda path: ChangedListing() if Path(path) == tree else original(path))
        with pytest.raises(OSError) as caught:
            cache.entries(tree)
        assert caught.value.errno == errno.EAGAIN
    assert "arrived.py" in names(cache, tree)


def test_singleflight_keeps_other_roots_available(tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = DirectoryTreeCache(maxsize=2)
    other = tmp_path / "independent"
    other.mkdir()
    entered = threading.Event()
    release = threading.Event()
    original = os.scandir
    calls: list[Path] = []

    def blocked(path):
        calls.append(Path(path))
        if Path(path) == tree:
            entered.set()
            assert release.wait(5)
        return original(path)

    monkeypatch.setattr(os, "scandir", blocked)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(cache.entries, tree)
        try:
            assert entered.wait(5)
            second = pool.submit(cache.entries, tree)
            independent = pool.submit(cache.entries, other)
            assert independent.result(timeout=5) == ()
        finally:
            release.set()
        assert first.result(timeout=5) == second.result(timeout=5)
    assert Counter(calls) == Counter([tree, tree / "nested", tree / "other", other])


def test_clear_during_refill_prevents_publication(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = DirectoryTreeCache()
    entered = threading.Event()
    release = threading.Event()
    original = os.scandir

    def blocked(path):
        if Path(path) == tree:
            entered.set()
            assert release.wait(5)
        return original(path)

    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=1) as pool:
        patch.setattr(os, "scandir", blocked)
        pending = pool.submit(cache.entries, tree)
        try:
            assert entered.wait(5)
            cache.cache_clear()
        finally:
            release.set()
        with pytest.raises(OSError) as caught:
            pending.result(timeout=5)
        assert caught.value.errno == errno.EAGAIN
    assert len(cache.entries(tree)) == 3
