from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from captain_hook.util.gitprobe import cached_git

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git/refs/remotes/origin").mkdir(parents=True)
    (tmp_path / ".git/HEAD").write_text("ref: refs/heads/main\n")
    (tmp_path / ".git/config").write_text("[core]\n")
    return tmp_path


def counting(*outputs: str | None) -> tuple[list[int], Callable[[], str | None]]:
    calls: list[int] = []

    def run() -> str | None:
        calls.append(1)
        return outputs[len(calls) - 1]

    return calls, run


def replace(path: Path, text: str) -> None:
    staged = path.with_name(f"{path.name}.lock")
    staged.write_text(text)
    staged.replace(path)


@pytest.mark.parametrize(
    ("args", "changed"),
    [
        pytest.param(("rev-parse", "--abbrev-ref", "HEAD"), ".git/HEAD", id="branch"),
        pytest.param(("symbolic-ref", "--short", "-q", "HEAD"), ".git/HEAD", id="symbolic-head"),
        pytest.param(
            ("symbolic-ref", "--short", "refs/remotes/origin/HEAD"), ".git/refs/remotes/origin/HEAD", id="trunk"
        ),
        pytest.param(("config", "--get-regexp", r"^remote\..*\.fetch$"), ".git/config", id="config"),
        pytest.param(("rev-parse", "--verify", "--quiet", "refs/heads/main"), ".git/packed-refs", id="verify"),
    ],
)
def test_a_probe_runs_once_until_its_file_changes(args: tuple[str, ...], changed: str, tmp_path: Path) -> None:
    root = repo(tmp_path)
    calls, run = counting("first\n", "second\n")
    assert [cached_git(root, args, {}, run) for _ in range(3)] == ["first\n"] * 3
    replace(root / changed, "ref: refs/heads/other\n")
    assert cached_git(root, args, {}, run) == "second\n"
    assert len(calls) == 2


def test_a_dash_c_probe_reads_the_named_repo_from_its_subdirectory(tmp_path: Path) -> None:
    root = repo(tmp_path / "checkout")
    (root / "src").mkdir()
    calls, run = counting("main\n", "other\n")
    args = ("-C", "checkout/src", "rev-parse", "--abbrev-ref", "HEAD")
    assert [cached_git(tmp_path, args, {}, run) for _ in range(2)] == ["main\n"] * 2
    replace(root / ".git/HEAD", "ref: refs/heads/other\n")
    assert cached_git(tmp_path, args, {}, run) == "other\n"
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("args", "env"),
    [
        pytest.param(("status", "--short"), {}, id="unlisted-command"),
        pytest.param(("config", "--add", "x.y", "z"), {}, id="config-write"),
        pytest.param(("config", "review.value", "desired"), {}, id="config-set"),
        pytest.param(("rev-parse", "--verify", "--quiet", "refs/heads/main^{commit}"), {}, id="revision-expression"),
        pytest.param(("config", "--get", "x.y"), {"GIT_CONFIG_PARAMETERS": "'x.y=one'"}, id="config-parameters-env"),
        pytest.param(("rev-parse", "--abbrev-ref", "HEAD"), {"GIT_DIR": "/elsewhere"}, id="git-dir-env"),
    ],
)
def test_other_calls_always_run(args: tuple[str, ...], env: dict[str, str], tmp_path: Path) -> None:
    root = repo(tmp_path)
    calls, run = counting("a\n", "b\n")
    assert [cached_git(root, args, env, run) for _ in range(2)] == ["a\n", "b\n"]
    assert len(calls) == 2


def test_an_unborn_branch_is_asked_again(tmp_path: Path) -> None:
    calls, run = counting(None, "main\n")
    root = repo(tmp_path)
    assert [cached_git(root, ("rev-parse", "--abbrev-ref", "HEAD"), {}, run) for _ in range(2)] == [None, "main\n"]
    assert len(calls) == 2


def test_outside_a_repo_always_runs(tmp_path: Path) -> None:
    calls, run = counting(None, None)
    assert [cached_git(tmp_path, ("symbolic-ref", "--short", "HEAD"), {}, run) for _ in range(2)] == [None, None]
    assert len(calls) == 2


def test_an_included_config_change_reruns_the_probe(tmp_path: Path) -> None:
    root = repo(tmp_path)
    (root / ".git/config").write_text('[core]\n[include]\n\tpath = "../extra.config"\n')
    replace(root / "extra.config", "[x]\n\ty = before\n")
    calls, run = counting("before\n", "after\n")
    assert [cached_git(root, ("config", "--get", "x.y"), {}, run) for _ in range(2)] == ["before\n"] * 2
    replace(root / "extra.config", "[x]\n\ty = after\n")
    assert cached_git(root, ("config", "--get", "x.y"), {}, run) == "after\n"
    assert len(calls) == 2


def test_a_reftable_repo_always_runs(tmp_path: Path) -> None:
    root = repo(tmp_path)
    (root / ".git/reftable").mkdir()
    calls, run = counting("main\n", "topic\n")
    assert [cached_git(root, ("symbolic-ref", "--short", "HEAD"), {}, run) for _ in range(2)] == ["main\n", "topic\n"]
    assert len(calls) == 2
