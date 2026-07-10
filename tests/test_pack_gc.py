from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from captain_hook.packs import manager


def make_commit_dir(root: Path, name: str, sha: str, mtime: float) -> Path:
    d = root / f"{name}@{sha}"
    d.mkdir(parents=True)
    (d / manager.SHA_MARKER).write_text(sha)
    os.utime(d, (mtime, mtime))
    return d


@pytest.fixture
def cache_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "cache"
    root.mkdir()
    monkeypatch.setattr(manager, "packs_cache_root", lambda: root)
    return root


class TestEvictStaleCommits:
    def test_keeps_current_plus_two_recent(self, cache_root: Path) -> None:
        now = time.time()
        current = make_commit_dir(cache_root, "cc-notes", "cur", now)
        for i in range(4):
            make_commit_dir(cache_root, "cc-notes", f"old{i}", now - (i + 1) * 100)
        other = make_commit_dir(cache_root, "ccx", "xyz", now - 500)

        manager.evict_stale_commits("cc-notes", "cur")

        surviving = {p.name for p in cache_root.glob("cc-notes@*")}
        assert surviving == {"cc-notes@cur", "cc-notes@old0", "cc-notes@old1"}
        assert current.is_dir()
        assert other.is_dir()

    def test_never_removes_just_resolved(self, cache_root: Path) -> None:
        now = time.time()
        current = make_commit_dir(cache_root, "cc-notes", "cur", now - 10_000)
        for i in range(3):
            make_commit_dir(cache_root, "cc-notes", f"r{i}", now - i)

        manager.evict_stale_commits("cc-notes", "cur")

        assert current.is_dir()

    def test_noop_within_buffer(self, cache_root: Path) -> None:
        now = time.time()
        make_commit_dir(cache_root, "cc-notes", "cur", now)
        make_commit_dir(cache_root, "cc-notes", "a", now - 10)
        make_commit_dir(cache_root, "cc-notes", "b", now - 20)

        manager.evict_stale_commits("cc-notes", "cur")

        assert len(list(cache_root.glob("cc-notes@*"))) == 3

    def test_respects_name_boundary(self, cache_root: Path) -> None:
        now = time.time()
        make_commit_dir(cache_root, "cc", "cur", now)
        siblings = [make_commit_dir(cache_root, "cc-notes", s, now - (i + 1) * 100) for i, s in enumerate("abc")]

        manager.evict_stale_commits("cc", "cur")

        assert all(s.is_dir() for s in siblings)
