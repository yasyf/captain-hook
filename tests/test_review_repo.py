from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from captain_hook.review.repo import normalize_origin, pr_repo_key, repo_key


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


class TestNormalizeOrigin:
    @pytest.mark.parametrize(
        "url",
        [
            "git@github.com:yasyf/captain-hook.git",
            "https://github.com/yasyf/captain-hook.git",
            "https://github.com/yasyf/captain-hook",
            "ssh://git@github.com/yasyf/captain-hook.git",
            "https://github.com/yasyf/captain-hook/",
        ],
        ids=["ssh-scp", "https-git", "https-bare", "ssh-url", "trailing-slash"],
    )
    def test_forms_collapse_to_one_key(self, url: str) -> None:
        assert normalize_origin(url) == "github.com/yasyf/captain-hook"


class TestPrRepoKey:
    def test_parses_repo(self) -> None:
        assert pr_repo_key("https://github.com/yasyf/captain-hook/pull/42") == "github.com/yasyf/captain-hook"

    def test_lowercases_repo(self) -> None:
        assert pr_repo_key("https://GitHub.COM/Yasyf/Captain-Hook/pull/42") == "github.com/yasyf/captain-hook"

    def test_accepts_trailing_slash(self) -> None:
        assert pr_repo_key("http://github.com/yasyf/captain-hook/pull/42/") == "github.com/yasyf/captain-hook"

    def test_rejects_garbage(self) -> None:
        with pytest.raises(ValueError):
            pr_repo_key("not a PR")


class TestRepoKey:
    def test_origin_drives_key(self, tmp_path: Path) -> None:
        git(tmp_path, "init")
        git(tmp_path, "remote", "add", "origin", "git@github.com:yasyf/x.git")
        assert repo_key(tmp_path) == "github.com/yasyf/x"

    def test_worktree_shares_origin_key(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        git(repo, "init")
        git(repo, "remote", "add", "origin", "https://github.com/yasyf/x.git")
        git(repo, "commit", "--allow-empty", "-m", "init")
        wt = tmp_path / "wt"
        git(repo, "worktree", "add", str(wt))
        assert repo_key(repo) == repo_key(wt) == "github.com/yasyf/x"

    def test_no_origin_returns_none(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        git(repo, "init")
        git(repo, "commit", "--allow-empty", "-m", "init")
        wt = tmp_path / "wt"
        git(repo, "worktree", "add", str(wt))
        assert repo_key(repo) is None  # no origin: review opens PRs against origin, so it can't be keyed
        assert repo_key(wt) is None

    def test_non_repo_returns_none(self, tmp_path: Path) -> None:
        assert repo_key(tmp_path) is None
