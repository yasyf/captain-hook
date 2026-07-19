from __future__ import annotations

from pathlib import Path

import pytest

from captain_hook.util.globbing import (
    GLOB_LIMIT,
    GlobExpansion,
    glob_matches,
    static_prefix_dir,
    walked_paths,
)


def test_glob_limit_default() -> None:
    assert GLOB_LIMIT == 10


def test_static_prefix_dir_stops_at_first_magic_segment(tmp_path: Path) -> None:
    assert static_prefix_dir("**/*.py", tmp_path) == tmp_path.resolve()
    assert static_prefix_dir("src/pkg/**/*.py", tmp_path) == (tmp_path / "src" / "pkg").resolve()
    assert static_prefix_dir("/etc/**/*.conf", tmp_path) == Path("/etc").resolve()


def test_walked_paths_skips_hidden_files_and_dirs(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.txt").write_text("z")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "b.txt").write_text("y")
    (tmp_path / ".dotfile").write_text("w")

    names = {p.name for p in walked_paths(tmp_path)}
    assert {"a.txt", "c.txt", "sub"} <= names
    assert "b.txt" not in names  # under a hidden directory, never descended
    assert ".hidden" not in names
    assert ".dotfile" not in names


def test_glob_matches_non_recursive(tmp_path: Path) -> None:
    for index in range(3):
        (tmp_path / f"f{index}.zzz").write_text("x")
    expansion = glob_matches("*.zzz", tmp_path, limit=GLOB_LIMIT)
    assert not expansion.too_broad
    assert len(expansion.matches) == 3


def test_glob_matches_over_limit_returns_one_past_limit(tmp_path: Path) -> None:
    # islice(limit + 1) lets the caller distinguish "exactly limit" from "more than limit".
    for index in range(GLOB_LIMIT + 5):
        (tmp_path / f"f{index}.zzz").write_text("x")
    expansion = glob_matches("*.zzz", tmp_path, limit=GLOB_LIMIT)
    assert not expansion.too_broad
    assert len(expansion.matches) == GLOB_LIMIT + 1


def test_glob_matches_relative_without_cwd_is_empty() -> None:
    assert glob_matches("*.zzz", None, limit=GLOB_LIMIT) == GlobExpansion((), False)


def test_glob_matches_recursive_walk_budget_exceeded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A ** pattern over a tree past the 20k walk budget is reported too_broad rather than scanned forever.
    def fake_walked_paths(anchor: Path):
        return (Path(f"/synthetic/{index}.zzz") for index in range(20_000))

    monkeypatch.setattr("captain_hook.util.globbing.walked_paths", fake_walked_paths)
    expansion = glob_matches("**/*.zzz", tmp_path, limit=GLOB_LIMIT)
    assert expansion.too_broad is True
    assert expansion.matches == ()
