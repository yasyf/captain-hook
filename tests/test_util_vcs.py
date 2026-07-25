from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from captain_hook.util.vcs import GRAPHITE_MARKER, contains_repo, in_vcs_repo, is_graphite_repo, is_repo_root


def test_contains_repo_marker_directory(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    assert contains_repo(tmp_path) is True


def test_contains_repo_marker_file(tmp_path: Path) -> None:
    (tmp_path / ".git").write_text("gitdir: ../actual")
    assert contains_repo(tmp_path) is True


@pytest.mark.parametrize(
    ("scanned", "expected"),
    [
        pytest.param(20_000, True, id="at-budget"),
        pytest.param(19_999, False, id="under-budget"),
    ],
)
def test_contains_repo_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    scanned: int,
    expected: bool,
) -> None:
    def names(_anchor: Path) -> Iterator[str]:
        return (f"entry-{index}" for index in range(scanned))

    monkeypatch.setattr("captain_hook.util.vcs.scanned_names", names)
    assert contains_repo(tmp_path) is expected


def test_contains_repo_short_circuits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def names(_anchor: Path) -> Iterator[str]:
        yield ".git"
        raise AssertionError("scanned past the repository marker")

    monkeypatch.setattr("captain_hook.util.vcs.scanned_names", names)
    assert contains_repo(tmp_path) is True


def test_vcs_marker_directory(tmp_path: Path) -> None:
    root = tmp_path / "directory"
    (root / ".git").mkdir(parents=True)
    assert is_repo_root(root) is True
    assert in_vcs_repo(root / "one" / "two") is True


def test_vcs_marker_file(tmp_path: Path) -> None:
    root = tmp_path / "file"
    root.mkdir()
    (root / ".git").write_text("gitdir: ../actual")
    assert is_repo_root(root) is True
    assert in_vcs_repo(root / "one" / "two") is True


def test_vcs_marker_absent(tmp_path: Path) -> None:
    assert is_repo_root(tmp_path) is False


def test_is_graphite_repo_gt_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / GRAPHITE_MARKER).write_text("")
    assert is_graphite_repo(repo) is True
    assert is_graphite_repo(repo / "one" / "two") is True


def test_is_graphite_repo_worktree(tmp_path: Path) -> None:
    git = tmp_path / "main" / ".git"
    (wt_meta := git / "worktrees" / "wt").mkdir(parents=True)
    (git / GRAPHITE_MARKER).write_text("")
    (wt_meta / "commondir").write_text("../..\n")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {wt_meta}\n")
    assert is_graphite_repo(worktree) is True
    assert is_graphite_repo(worktree / "nested") is True


def test_is_graphite_repo_plain_git(tmp_path: Path) -> None:
    (tmp_path / "plain" / ".git").mkdir(parents=True)
    assert is_graphite_repo(tmp_path / "plain") is False


def test_is_graphite_repo_outside_repo(tmp_path: Path) -> None:
    assert is_graphite_repo(tmp_path / "nowhere") is False


def test_is_graphite_repo_dangling_pointer(tmp_path: Path) -> None:
    dangling = tmp_path / "dangling"
    dangling.mkdir()
    (dangling / ".git").write_text("gitdir: ../does-not-exist\n")
    assert is_graphite_repo(dangling) is False
