from __future__ import annotations

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
    (root / ".claude" / "scratch" / "notes.log").write_text("needle\n")
    (lane := root / ".claude" / "worktrees" / "captain-hook" / "lane" / "src").mkdir(parents=True)
    (lane.parent / ".git").write_text("gitdir: ../actual")
    monkeypatch.setenv("HOME", str(root))
    return root


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
        pytest.param("git grep needle", id="git-grep"),
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


def test_search_denial_names_its_bounds(performance: None, repo: Path, tmp_path: Path) -> None:
    result = dispatch(Event.PreToolUse, evt_for("rg needle .", cwd=str(repo)), session_dir=tmp_path / "session")
    assert result is not None
    assert "`-g '*.py'`" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_blocks_successive_searches_in_one_session(performance: None, repo: Path, tmp_path: Path) -> None:
    assert decision("grep -rl needle .", repo, tmp_path) == "deny"
    assert decision("rg other .", repo, tmp_path) == "deny"
    assert decision("rg needle internal/focused", repo, tmp_path) is None
