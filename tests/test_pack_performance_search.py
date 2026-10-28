from __future__ import annotations

import subprocess
from pathlib import Path
from typing import NoReturn

import pytest

from captain_hook.cmd import Target
from captain_hook.dispatch import dispatch
from captain_hook.loader import discover_pack
from captain_hook.packs import manager
from captain_hook.types import Event
from tests.test_cmd import evt_for


@pytest.fixture
def performance(isolate_modules: None) -> None:
    discover_pack("performance", manager.resolve_builtin("performance").path)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path.resolve() / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "internal" / "focused").mkdir(parents=True)
    (root / "explicit.log").write_text("needle\n")
    return root


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path.resolve() / "home"
    (root / ".claude" / "scratch" / "release-v3" / "specific-lane").mkdir(parents=True)
    (root / ".claude" / "scratch" / "durable-agents-rebuild").mkdir()
    (root / ".claude" / "scratch" / "notes.log").write_text("needle\n")
    (lane := root / ".claude" / "worktrees" / "captain-hook" / "lane" / "src").mkdir(parents=True)
    (lane.parent / ".git").write_text("gitdir: ../actual")
    monkeypatch.setenv("HOME", str(root))
    return root


@pytest.fixture
def indexed(git_repo: Path) -> Path:
    root = git_repo.resolve()
    (focused := root / "internal" / "focused").mkdir(parents=True)
    (focused / "inside.txt").write_text("needle\n")
    (root / "outside.txt").write_text("needle\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    return focused


def decision(command: str, cwd: Path, session_dir: Path) -> str | None:
    result = dispatch(Event.PreToolUse, evt_for(command, cwd=str(cwd)), session_dir=session_dir / "session")
    if result is None:
        return None
    output = result["hookSpecificOutput"]
    return output.get("permissionDecision") if "additionalContext" not in output else "warn"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("grep -R -n needle .", id="grep-dot"),
        pytest.param("grep -rn needle", id="grep-implicit-cwd"),
        pytest.param("rg needle .", id="rg-dot"),
        pytest.param("rg needle", id="rg-implicit-cwd"),
        pytest.param("rg --max-count 1 needle .", id="max-count-is-no-bound"),
        pytest.param("rg needle . | head -1", id="output-head-is-no-bound"),
        pytest.param("rg -d 1 needle .", id="depth-is-no-bound"),
        pytest.param("rg -g'*.py' -g'**' needle .", id="broad-glob-beside-narrow-glob"),
        pytest.param("rg -g'!*.md' needle .", id="negative-glob-only"),
        pytest.param("rg -tall needle .", id="type-all"),
        pytest.param("rg --type-add 'mine:*.py' -tmine needle .", id="type-add-voids-types"),
        pytest.param("grep -r --include='*' needle .", id="bare-include"),
        pytest.param("grep -r --exclude-dir=node_modules needle .", id="exclusion-only"),
        pytest.param("rg needle </dev/null", id="dev-null-stdin-searches-cwd"),
        pytest.param("cat patterns.txt | rg -f -", id="pattern-file-takes-stdin"),
        pytest.param("cat explicit.log | grep -r needle", id="piped-recursive-grep-searches-cwd"),
        pytest.param("cat explicit.log | ugrep -r needle", id="piped-recursive-ugrep-searches-cwd"),
        pytest.param("ugrep needle", id="ugrep-terminal-default"),
        pytest.param("ugrep -2 needle .", id="ugrep-depth-recurses"),
        pytest.param("ugrep --include-dir='*' needle .", id="ugrep-include-dir-recurses"),
        pytest.param("ugrep -r -M python needle .", id="ugrep-magic-is-no-bound"),
        pytest.param("ugrep -r -g'*.py,*' needle .", id="ugrep-broad-glob-in-list"),
        pytest.param("ugrep -r -g'*.py' --all needle .", id="ugrep-all-cancels-globs"),
        pytest.param("ugrep --include-fs -r needle .", id="ugrep-include-fs-takes-no-value"),
        pytest.param("ugrep --max-depth=2 needle .", id="ugrep-max-depth-recurses"),
        pytest.param("printf needle | rg needle </dev/null", id="redirect-replaces-pipe"),
        pytest.param("rg needle internal/focused .", id="safe-sibling-root"),
        pytest.param("rg needle internal/focused; rg needle .", id="safe-sibling-call"),
        pytest.param("rg needle *", id="bare-star-operand"),
        pytest.param("grep -r -e needle", id="regexp-role-implicit-cwd"),
        pytest.param("grep -r --file=patterns.txt .", id="file-role-root"),
        pytest.param("grep -rl -- needle .", id="end-of-options"),
        pytest.param("sudo grep -Rni needle .", id="wrapped-cluster"),
        pytest.param("rg needle $ROOT", id="unresolved-root"),
        pytest.param("rg --bogus-flag needle internal/focused", id="unknown-rg-option"),
        pytest.param("grep -rn --bogus-flag needle internal/focused", id="unknown-option-after-recursion"),
        pytest.param("git grep needle", id="git-grep-implicit-cwd"),
        pytest.param("git grep -n -e needle -- .", id="git-grep-dot-pathspec"),
        pytest.param("git grep -c needle", id="git-grep-count-is-no-config"),
        pytest.param("git --no-pager -c core.quotepath=off grep needle", id="git-grep-global-options"),
        pytest.param("git grep needle -- ':!internal'", id="git-grep-exclusion-only"),
        pytest.param("git grep needle HEAD -- .", id="git-grep-revision-is-no-bound"),
        pytest.param("git grep needle HEAD", id="git-grep-revision-without-separator"),
        pytest.param("git grep needle -- :/", id="git-grep-top-magic"),
        pytest.param("git grep needle -- '*'", id="git-grep-bare-star-pathspec"),
        pytest.param("git grep --bogus-flag needle internal/focused", id="git-grep-unknown-option"),
        pytest.param("git grep -- needle", id="git-grep-separator-before-pattern"),
        pytest.param("git grep -- needle HEAD -- .", id="git-grep-second-separator"),
        pytest.param("SPEC=':/'; git grep needle -- \"$SPEC\"", id="git-grep-expanded-top-magic"),
        pytest.param("SPEC=':!internal'; git grep needle -- \"$SPEC\"", id="git-grep-expanded-exclusion"),
        pytest.param("git grep needle -- ':(top,glob)**/**'", id="git-grep-top-universal-glob"),
        pytest.param("git --namespace -Cinternal grep needle", id="git-grep-namespace-value-is-no-hop"),
        pytest.param("git grep -nC3 needle", id="git-grep-cluster-value-implicit-cwd"),
        pytest.param("git grep -nC 3 needle -- .", id="git-grep-cluster-separate-value-dot"),
        pytest.param("git grep -nOless needle -- internal/focused", id="git-grep-optional-pager-cluster-fails-closed"),
        pytest.param("git grep needle -- internal/..", id="git-grep-parent-normalizes-to-root"),
        pytest.param("git grep needle -- internal/../.", id="git-grep-parent-dot-alias"),
        pytest.param("git grep needle -- '**/*.py/..'", id="git-grep-glob-parent-normalizes"),
    ],
)
def test_blocks_search_at_repo_root(performance: None, repo: Path, tmp_path: Path, command: str) -> None:
    assert decision(command, repo, tmp_path) == "deny"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("grep -n needle explicit.log", id="not-recursive"),
        pytest.param("grep -rn --include='*.py' needle .", id="narrow-include"),
        pytest.param("grep -rn --include='*.py' --include='*.json' needle .", id="every-include-narrows"),
        pytest.param("rg -g'*.json' needle .", id="narrow-glob"),
        pytest.param("rg -tpy needle .", id="type"),
        pytest.param("rg --type=py --glob='*.json' needle .", id="type-and-glob"),
        pytest.param("rg needle internal/focused", id="deeper-subtree"),
        pytest.param("rg needle internal/*", id="glob-under-subtree"),
        pytest.param("rg needle *.py", id="named-file-glob"),
        pytest.param("cat explicit.log | rg needle", id="piped-stdin"),
        pytest.param("rg needle <explicit.log", id="redirected-stdin"),
        pytest.param("rg needle <<<'needle here'", id="here-string"),
        pytest.param("rg --files .", id="file-listing"),
        pytest.param("rg -e needle explicit.log", id="regexp-role-keeps-file"),
        pytest.param("grep -r -e needle internal", id="regexp-role-subtree"),
        pytest.param("rg -- -needle internal", id="end-of-options-pattern"),
        pytest.param("grep -rn -m1 needle internal", id="attached-value"),
        pytest.param("ugrep -g 'internal/*.py' needle .", id="ugrep-path-glob"),
        pytest.param("ugrep -r -tpy,json needle .", id="ugrep-type-list"),
        pytest.param("ugrep -r -Opy needle .", id="ugrep-extension"),
        pytest.param("ugrep needle .", id="ugrep-skips-directories"),
        pytest.param("cat explicit.log | ugrep needle", id="ugrep-piped-stdin"),
        pytest.param("rg '/' internal", id="path-like-pattern"),
        pytest.param("echo 'rg needle .'", id="quoted-command"),
        pytest.param("git grep needle internal/focused", id="git-grep-subtree"),
        pytest.param("git grep -c needle internal", id="git-grep-count-subtree"),
        pytest.param("git grep needle explicit.log", id="git-grep-named-file"),
        pytest.param("git grep needle HEAD -- internal", id="git-grep-revision-and-subtree"),
        pytest.param("git grep needle -- '*.py'", id="git-grep-glob-pathspec"),
        pytest.param("git grep -e needle -- ':(glob)**/*.py'", id="git-grep-long-magic-glob"),
        pytest.param("git grep needle -- :/internal", id="git-grep-top-relative-subtree"),
        pytest.param("git grep -- needle internal", id="git-grep-separator-before-pattern-subtree"),
        pytest.param("SPEC=internal; git grep needle -- \"$SPEC\"", id="git-grep-expanded-subtree"),
        pytest.param("git grep -nC3 needle -- internal/focused", id="git-grep-cluster-value-subtree"),
        pytest.param("git grep -nC 3 needle -- internal/focused", id="git-grep-cluster-separate-value-subtree"),
        pytest.param("git grep needle -- internal/focused/../focused", id="git-grep-parent-back-into-subtree"),
        pytest.param("git grep -e needle --and \\( -e other \\) internal", id="git-grep-expression-groups"),
        pytest.param("git grep --help", id="git-grep-help"),
        pytest.param("git grep", id="git-grep-without-pattern"),
        pytest.param("echo 'git grep needle'", id="quoted-git-grep"),
    ],
)
def test_allows_bounded_search_at_repo_root(
    performance: None, repo: Path, home: Path, tmp_path: Path, command: str
) -> None:
    assert decision(command, repo, tmp_path) is None


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("ugrep -rn needle ~/.claude/scratch/release-v3", id="scratch-project"),
        pytest.param("ugrep -rl needle ~/.claude/scratch/release-v3", id="scratch-project-listing"),
        pytest.param("sh -c 'cd ~/.claude/scratch/release-v3 && ugrep -rn needle .'", id="cd-in-payload"),
        pytest.param("cd ~/.claude/scratch/release-v3 && grep -rn needle .", id="cd-before-call"),
        pytest.param(
            'cd ~/.claude/scratch/durable-agents-rebuild && grep -rl "backup in cloud storage" . '
            '2>/dev/null | grep -v node_modules | head -3',
            id="incident-recursive-scratch-pipeline",
        ),
        pytest.param("rg needle ~/.claude/worktrees/captain-hook", id="worktree-pool"),
        pytest.param("rg needle ~/.claude/worktrees/captain-hook/lane", id="worktree-root"),
        pytest.param("rg needle ~", id="home"),
        pytest.param('rg needle "$HOME"', id="home-variable"),
    ],
)
def test_blocks_search_at_home_roots(performance: None, home: Path, tmp_path: Path, command: str) -> None:
    assert decision(command, tmp_path.resolve(), tmp_path) == "deny"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("ugrep -rn needle ~/.claude/scratch/release-v3/specific-lane", id="scratch-lane"),
        pytest.param("rg needle ~/.claude/worktrees/captain-hook/lane/src", id="worktree-subtree"),
        pytest.param("rg needle '~'", id="quoted-tilde"),
        pytest.param("rg needle ~/.claude/scratch/notes.log", id="file-under-scratch-root"),
    ],
)
def test_allows_search_below_home_roots(performance: None, home: Path, tmp_path: Path, command: str) -> None:
    assert decision(command, tmp_path.resolve(), tmp_path) is None


def test_match_supplies_the_ugrep_pattern(performance: None, repo: Path, tmp_path: Path) -> None:
    assert decision("ugrep -r --match /", repo / "internal" / "focused", tmp_path) == "deny"
    assert decision("ugrep -r --match .", repo / "internal" / "focused", tmp_path) is None


def test_classifies_roots_without_walking(
    performance: None, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def walk(*_: object, **__: object) -> NoReturn:
        raise AssertionError("the search guard walked a tree")

    monkeypatch.setattr(Target, "expand", walk)
    monkeypatch.setattr("captain_hook.cmd.contains_repo", walk)
    monkeypatch.setattr("captain_hook.cmd.glob_matches", walk)
    monkeypatch.setattr("captain_hook.util.vcs.scanned_names", walk)
    assert decision("rg needle .", repo, tmp_path / "dot") == "deny"
    assert decision("grep -rn needle *", repo, tmp_path / "star") == "deny"
    assert decision("rg needle internal/*", repo, tmp_path) is None
    assert decision("rg needle internal/focused", repo, tmp_path) is None
    assert decision("git grep needle", repo, tmp_path / "git") == "deny"
    assert decision("git grep needle internal/focused", repo, tmp_path) is None


def test_git_grep_follows_its_own_directory(performance: None, repo: Path, tmp_path: Path) -> None:
    focused = repo / "internal" / "focused"
    assert decision("git grep needle", focused, tmp_path / "subtree") is None
    assert decision("git grep needle -- :/", focused, tmp_path / "top") == "deny"
    assert decision(f"git -C {repo} grep needle", focused, tmp_path / "absolute") == "deny"
    assert decision("git -C ../.. grep needle", focused, tmp_path / "relative") == "deny"
    assert decision("git -C ../.. -C internal grep needle", focused, tmp_path / "chained") is None
    assert decision(f"git -C {repo}/internal grep needle", tmp_path, tmp_path / "outside") is None
    assert decision("git grep needle -- ../..", focused, tmp_path / "parents") == "deny"
    assert decision("git grep needle -- ../focused", focused, tmp_path / "sibling") is None


def test_explicit_git_dir_searches_the_whole_index(indexed: Path) -> None:
    def hits(*pathspecs: str, git_dir: Path | None = None) -> list[str]:
        location = () if git_dir is None else (f"--git-dir={git_dir}",)
        return subprocess.run(
            ["git", *location, "grep", "--cached", "-l", "needle", "--", *pathspecs],
            cwd=indexed,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()

    git_dir = indexed.parents[1] / ".git"
    assert hits() == ["inside.txt"]
    assert "outside.txt" in hits(git_dir=git_dir)
    assert "outside.txt" in hits("internal/..", git_dir=git_dir)
    assert "outside.txt" in hits("internal/../.", git_dir=git_dir)
    assert hits("internal/focused", git_dir=git_dir) == ["internal/focused/inside.txt"]


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("git --git-dir={root}/.git grep --cached needle", id="git-dir-option"),
        pytest.param("git --git-dir {root}/.git grep needle -- .", id="git-dir-separate-value"),
        pytest.param("GIT_DIR={root}/.git git grep --cached needle", id="git-dir-variable"),
        pytest.param("env GIT_DIR={root}/.git git grep --cached needle", id="git-dir-env-wrapper"),
        pytest.param("export GIT_DIR={root}/.git; git grep --cached needle", id="git-dir-exported"),
        pytest.param("git --git-dir={root}/.git grep --cached needle -- :/", id="git-dir-top-magic"),
        pytest.param(
            "git --git-dir={root}/.git grep --cached needle -- internal/..", id="git-dir-parent-normalizes-to-top"
        ),
        pytest.param("git --git-dir={root}/.git grep --cached needle -- internal/../.", id="git-dir-parent-dot-alias"),
        pytest.param(
            "git --git-dir={root}/.git grep --cached needle -- internal/focused/../..", id="git-dir-double-parent"
        ),
        pytest.param(
            "git --git-dir={root}/.git grep --cached needle -- ':(glob)internal/..'", id="git-dir-magic-parent"
        ),
    ],
)
def test_blocks_git_grep_from_a_subtree_with_an_explicit_git_dir(
    performance: None, indexed: Path, tmp_path: Path, command: str
) -> None:
    assert decision(command.format(root=indexed.parents[1]), indexed, tmp_path) == "deny"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("git --git-dir={root}/.git --work-tree={root} grep needle", id="work-tree-option"),
        pytest.param("GIT_WORK_TREE={root} git --git-dir={root}/.git grep needle", id="work-tree-variable"),
    ],
)
def test_blocks_git_grep_over_an_explicit_work_tree(
    performance: None, indexed: Path, tmp_path: Path, command: str
) -> None:
    assert decision(command.format(root=indexed.parents[1]), tmp_path, tmp_path) == "deny"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("git grep --cached needle", id="discovered-repository-keeps-its-prefix"),
        pytest.param("git -C {root}/internal/focused grep --cached needle", id="plain-c-keeps-its-scope"),
        pytest.param(
            "git --git-dir={root}/.git grep --cached needle -- internal/focused", id="git-dir-narrowing-pathspec"
        ),
        pytest.param(
            "git --git-dir={root}/.git grep --cached needle -- internal/focused/../focused",
            id="git-dir-parent-back-into-subtree",
        ),
    ],
)
def test_allows_scoped_git_grep_in_a_real_repository(
    performance: None, indexed: Path, tmp_path: Path, command: str
) -> None:
    assert decision(command.format(root=indexed.parents[1]), indexed, tmp_path) is None


def test_search_denial_names_its_bounds(performance: None, repo: Path, tmp_path: Path) -> None:
    result = dispatch(Event.PreToolUse, evt_for("rg needle .", cwd=str(repo)), session_dir=tmp_path / "session")
    assert result is not None
    assert "`-g '*.py'`" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_blocks_successive_searches_in_one_session(performance: None, repo: Path, tmp_path: Path) -> None:
    assert decision("grep -rl needle .", repo, tmp_path) == "deny"
    assert decision("rg other .", repo, tmp_path) == "deny"
    assert decision("rg needle internal/focused", repo, tmp_path) is None
