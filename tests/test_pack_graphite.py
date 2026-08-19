from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

import captain_hook
from captain_hook.dispatch import dispatch
from captain_hook.loader import discover_pack
from captain_hook.testing.helpers import input_to_event
from captain_hook.testing.types import Input
from captain_hook.types import Event
from tests.helpers import raw_text, raw_tool_msg

PACKS_DIR = Path(captain_hook.__file__).parent / "builtin_packs"
GRAPHITE_HOOKS = PACKS_DIR / "graphite" / "hooks"

# A prior-turn review pass plus a trailing user message — proves the submit gate's session scope
# reaches back past the current turn. Two routes: a Skill tool call (shape from
# tests/test_conditions.py:1677) and a user-typed /cc-review command (literal <command-name> tags,
# which the harness may expand inline without any Skill tool_use).
REVIEWED_VIA_SKILL = [
    raw_tool_msg("Skill", {"skill": "cc-review:start"}),
    raw_text("user", "looks good, ship it"),
]
REVIEWED_VIA_COMMAND = [
    raw_text(
        "user", "<command-name>/cc-review:start</command-name>\n<command-message>review my diff</command-message>"
    ),
    raw_text("user", "looks good, ship it"),
]

HOOK_CASES = [
    pytest.param("jj new", "deny", "Graphite", id="jj-ban"),
    pytest.param("git commit -m x", "warn", "gt create", id="git-write"),
    pytest.param("git switch -C main", "warn", "gt create", id="git-write-switch-force"),
    pytest.param("gt submit", "warn", "review pass", id="submit-gate"),
    pytest.param("git rebase main", "warn", "gt restack", id="restack"),
]


@pytest.fixture
def gt_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "gt_repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / ".graphite_repo_config").write_text("")
    return repo


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "git_repo"
    (repo / ".git").mkdir(parents=True)
    return repo


@pytest.fixture
def gt_worktree(tmp_path: Path) -> Path:
    git = tmp_path / "wt_main" / ".git"
    (wt_meta := git / "worktrees" / "wt").mkdir(parents=True)
    (git / ".graphite_repo_config").write_text("")
    (wt_meta / "commondir").write_text("../..\n")
    worktree = tmp_path / "wt_checkout"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {wt_meta}\n")
    return worktree


def dispatch_command(
    command: str,
    cwd: Path,
    session_dir: Path,
    transcript: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    evt = input_to_event(Event.PreToolUse, Input(command=command, cwd=str(cwd), transcript=transcript))
    return dispatch(Event.PreToolUse, evt, session_dir=session_dir)


def assert_fires(result: dict[str, Any] | None, kind: str, needle: str) -> None:
    assert result is not None
    output = result["hookSpecificOutput"]
    match kind:
        case "deny":
            assert output["permissionDecision"] == "deny"
            assert needle in output["permissionDecisionReason"]
        case "warn":
            assert output.get("permissionDecision") != "deny"
            assert needle in output["additionalContext"]


def warn_context(result: dict[str, Any] | None) -> str:
    assert result is not None
    output = result["hookSpecificOutput"]
    assert output.get("permissionDecision") != "deny"
    return output["additionalContext"]


@pytest.mark.parametrize(("command", "kind", "needle"), HOOK_CASES)
def test_hook_fires_in_gt_repo(
    isolate_modules: None, gt_repo: Path, tmp_path: Path, command: str, kind: str, needle: str
) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    assert_fires(dispatch_command(command, gt_repo, tmp_path), kind, needle)


@pytest.mark.parametrize(("command", "kind", "needle"), HOOK_CASES)
def test_hook_silent_in_plain_git(
    isolate_modules: None, git_repo: Path, tmp_path: Path, command: str, kind: str, needle: str
) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    assert dispatch_command(command, git_repo, tmp_path) is None


@pytest.mark.parametrize(("command", "kind", "needle"), HOOK_CASES)
def test_hook_fires_in_gt_worktree(
    isolate_modules: None, gt_worktree: Path, tmp_path: Path, command: str, kind: str, needle: str
) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    assert_fires(dispatch_command(command, gt_worktree, tmp_path), kind, needle)


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("git push --tags", id="git-write-tags"),
        pytest.param("git push origin refs/tags/v1.0.0", id="git-write-refs-tags"),
        pytest.param("git commit --dry-run", id="git-write-dry-run"),
        pytest.param("git rebase --abort", id="restack-abort"),
    ],
)
def test_skip_if_carve_outs_stay_silent(isolate_modules: None, gt_repo: Path, tmp_path: Path, command: str) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    assert dispatch_command(command, gt_repo, tmp_path) is None


def test_submit_gate_mentions_review_and_draft(isolate_modules: None, gt_repo: Path, tmp_path: Path) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    context = warn_context(dispatch_command("gt submit", gt_repo, tmp_path))
    assert "review" in context
    assert "draft" in context


@pytest.mark.parametrize(
    "transcript",
    [pytest.param(REVIEWED_VIA_SKILL, id="skill-route"), pytest.param(REVIEWED_VIA_COMMAND, id="typed-route")],
)
def test_submit_gate_skipped_after_review_pass(
    isolate_modules: None, gt_repo: Path, tmp_path: Path, transcript: list[dict[str, Any]]
) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    assert dispatch_command("gt submit", gt_repo, tmp_path, transcript=transcript) is None


def test_submit_gate_ignores_prose_mention(isolate_modules: None, gt_repo: Path, tmp_path: Path) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    mention = [raw_text("user", "maybe run cc-review at some point"), raw_text("user", "carry on")]
    assert warn_context(dispatch_command("gt submit", gt_repo, tmp_path, transcript=mention))


@pytest.mark.parametrize(
    "command",
    [pytest.param("gt ss", id="gt-ss"), pytest.param("ccx vcs ship -m x", id="ccx-ship")],
)
def test_submit_gate_covers_aliases(isolate_modules: None, gt_repo: Path, tmp_path: Path, command: str) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    assert warn_context(dispatch_command(command, gt_repo, tmp_path))


def test_submit_gate_skips_no_push(isolate_modules: None, gt_repo: Path, tmp_path: Path) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    assert dispatch_command("ccx vcs ship -m x --no-push", gt_repo, tmp_path) is None


def test_git_write_warns_every_time(isolate_modules: None, gt_repo: Path, tmp_path: Path) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    assert warn_context(dispatch_command("git commit -m one", gt_repo, tmp_path))
    assert warn_context(dispatch_command("git commit -m two", gt_repo, tmp_path))


@pytest.mark.parametrize(
    "command",
    [
        pytest.param('gt submit -m "add --dry-run support"', id="submit-msg-dry-run"),
        pytest.param('ccx vcs ship -m "wire up --no-push flag"', id="ship-msg-no-push"),
        pytest.param('git commit -m "improve --tags handling"', id="commit-msg-tags"),
        pytest.param('git merge feature -m "handle --continue path"', id="merge-msg-continue"),
        pytest.param('git commit -m "push to refs/tags cleanup"', id="commit-msg-refs-tags"),
    ],
)
def test_quoted_flag_mentions_still_warn(isolate_modules: None, gt_repo: Path, tmp_path: Path, command: str) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    assert warn_context(dispatch_command(command, gt_repo, tmp_path))


# A real `git init`, unlike the fake `.git` directories above: gt_disabled shells out to
# git config, and only a genuine repository can answer it. A fake dir is not a hazard —
# `git --git-dir=<not-a-repo> config --get` exits 1 silently, which is why every fixture
# above keeps working — but it can never report the key as set.
def real_gt_repo(tmp_path: Path, nogt: str | None) -> Path:
    repo = tmp_path / "real_gt"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".git" / ".graphite_repo_config").write_text("")
    if nogt is not None:
        subprocess.run(["git", "-C", str(repo), "config", "ccx.nogt", nogt], check=True)
    return repo


@pytest.mark.parametrize(("command", "kind", "needle"), HOOK_CASES)
def test_hooks_stay_silent_when_nogt_set(
    isolate_modules: None, tmp_path: Path, command: str, kind: str, needle: str
) -> None:
    """ccx.nogt is the repository's opt-out from the gt lane; ccx honours it, so these must too."""
    discover_pack("graphite", GRAPHITE_HOOKS)
    assert dispatch_command(command, real_gt_repo(tmp_path, "true"), tmp_path) is None


@pytest.mark.parametrize(("command", "kind", "needle"), HOOK_CASES)
def test_hooks_fire_in_real_gt_repo(
    isolate_modules: None, tmp_path: Path, command: str, kind: str, needle: str
) -> None:
    """The regression half: without ccx.nogt every hook still fires, through a real config read."""
    discover_pack("graphite", GRAPHITE_HOOKS)
    assert_fires(dispatch_command(command, real_gt_repo(tmp_path, None), tmp_path), kind, needle)


@pytest.mark.parametrize(
    ("value", "disabled"),
    [
        ("true", True),
        ("1", True),
        ("t", True),
        ("TRUE", True),
        ("false", False),
        ("0", False),
        ("yes", False),
        ("on", False),
        ("maybe", False),
    ],
)
def test_nogt_value_parity_with_ccx(isolate_modules: None, tmp_path: Path, value: str, disabled: bool) -> None:
    """ccx parses ccx.nogt with Go's strconv.ParseBool; yes/on are not in that set, and a hook
    that silenced itself on them would disagree with the lane ccx actually rides."""
    discover_pack("graphite", GRAPHITE_HOOKS)
    result = dispatch_command("jj new", real_gt_repo(tmp_path, value), tmp_path)
    assert (result is None) is disabled


@pytest.mark.parametrize(
    "command",
    [
        "jj log",
        "jj st",
        "jj status",
        "jj show @",
        "jj diff --stat",
        "jj bookmark list",
        "jj op log",
        "jj --help",
        "jj log && jj status",
    ],
)
def test_read_only_jj_is_allowed(isolate_modules: None, gt_repo: Path, tmp_path: Path, command: str) -> None:
    """The ban protects stack metadata; a read mutates none, so blocking it only costs work."""
    discover_pack("graphite", GRAPHITE_HOOKS)
    assert dispatch_command(command, gt_repo, tmp_path) is None


@pytest.mark.parametrize(
    "command",
    [
        "jj new",
        "jj commit -m x",
        "jj bookmark set foo",
        "jj op undo",
        "jj describe -m x",
        "jj log && jj new",
        "jj status; jj abandon",
    ],
)
def test_mutating_jj_still_blocked(isolate_modules: None, gt_repo: Path, tmp_path: Path, command: str) -> None:
    """Every jj call on the line must be a read: skip_if is an any(), so a per-call carve-out
    would let the mutation in `jj log && jj new` through."""
    discover_pack("graphite", GRAPHITE_HOOKS)
    assert_fires(dispatch_command(command, gt_repo, tmp_path), "deny", "Graphite")
