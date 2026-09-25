from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
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
    pytest.param("jj new", "warn", "Graphite", id="jj-nudge"),
    pytest.param("git commit -m x", "warn", "ccx vcs ship", id="git-write"),
    pytest.param("git switch -C main", "warn", "ccx vcs ship", id="git-write-switch-force"),
    pytest.param("gt submit", "warn", "review pass", id="submit-gate"),
    pytest.param("git rebase main", "warn", "ccx vcs stack submit", id="restack"),
]


@pytest.fixture(autouse=True)
def ccx_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    kept = [entry for entry in os.environ.get("PATH", "").split(os.pathsep) if not (Path(entry) / "ccx").exists()]
    monkeypatch.setenv("PATH", os.pathsep.join(kept))


@pytest.fixture
def ccx_installed(ccx_absent: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (bindir := tmp_path / "ccx-bin").mkdir()
    (ccx := bindir / "ccx").write_text("#!/bin/sh\nexit 0\n")
    ccx.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


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


def assert_not_denied(result: dict[str, Any] | None) -> None:
    assert result is None or result["hookSpecificOutput"].get("permissionDecision") != "deny"


def rewritten_command(result: dict[str, Any] | None) -> str:
    assert result is not None
    output = result["hookSpecificOutput"]
    assert output["permissionDecision"] == "allow"
    return output["updatedInput"]["command"]


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
    assert "review pass" in context
    assert "never draft" in context
    assert "approv" not in context.lower()


@pytest.mark.parametrize(
    ("command", "kind", "needle"),
    [
        pytest.param("cd {other} && git push", "warn", "ccx vcs ship", id="cd-then-git-write"),
        pytest.param("git -C {other} push", "warn", "ccx vcs ship", id="git-C-write"),
        pytest.param("git --git-dir={other}/.git push", "warn", "ccx vcs ship", id="git-dir-write"),
        pytest.param("cd {other} && git rebase main", "warn", "ccx vcs stack submit", id="cd-then-restack"),
        pytest.param("cd {other} && jj new", "warn", "Graphite", id="cd-then-jj"),
    ],
)
def test_hooks_judge_the_repository_the_command_targets(
    isolate_modules: None, gt_repo: Path, git_repo: Path, tmp_path: Path, command: str, kind: str, needle: str
) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    assert dispatch_command(command.format(other=git_repo), gt_repo, tmp_path) is None
    assert_fires(dispatch_command(command.format(other=gt_repo), git_repo, tmp_path), kind, needle)


def test_git_write_message_names_the_target_not_the_session(
    isolate_modules: None, gt_repo: Path, git_repo: Path, tmp_path: Path
) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    context = warn_context(dispatch_command(f"cd {gt_repo} && git push", git_repo, tmp_path))
    assert "the repository this command targets" in context
    assert "in this repository" not in context


def test_ownership_and_verb_are_judged_on_the_same_call(
    isolate_modules: None, gt_repo: Path, git_repo: Path, tmp_path: Path
) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    assert dispatch_command(f"git -C {gt_repo} status && jj new", git_repo, tmp_path) is None
    assert dispatch_command(f"git -C {gt_repo} status && git push", git_repo, tmp_path) is None
    assert_fires(dispatch_command(f"git -C {git_repo} status && jj new", gt_repo, tmp_path), "warn", "Graphite")


def test_a_verbs_own_dash_C_is_not_a_directory_hop(isolate_modules: None, gt_repo: Path, tmp_path: Path) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    (gt_repo / "vendor" / ".git").mkdir(parents=True)
    assert warn_context(dispatch_command("git switch -C vendor", gt_repo, tmp_path))
    assert warn_context(dispatch_command("git checkout -B vendor", gt_repo, tmp_path))


def test_git_dir_resolves_against_the_dash_C_cwd_not_as_another_hop(
    isolate_modules: None, git_repo: Path, tmp_path: Path
) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    root = tmp_path / "root"
    (metadata := root / "metadata").mkdir(parents=True)
    (metadata / ".graphite_repo_config").write_text("")
    (separate := root / "separate").mkdir()
    assert warn_context(dispatch_command("git --git-dir=../metadata -C separate push", root, tmp_path))
    assert warn_context(dispatch_command(f"git --git-dir={metadata} push", separate, tmp_path))
    assert warn_context(dispatch_command(f"git --git-dir {metadata} push", git_repo, tmp_path))
    assert dispatch_command(f"git --git-dir={git_repo / '.git'} push", separate, tmp_path) is None


def test_unresolvable_cd_falls_back_to_the_session_cwd(
    isolate_modules: None, gt_repo: Path, git_repo: Path, tmp_path: Path
) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    assert warn_context(dispatch_command("cd $OTHER && git push", gt_repo, tmp_path))
    assert dispatch_command("cd $OTHER && git push", git_repo, tmp_path) is None


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
    """The nudge protects stack metadata; a read mutates none, so naming the gt route there only costs work."""
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
def test_mutating_jj_is_nudged(isolate_modules: None, gt_repo: Path, tmp_path: Path, command: str) -> None:
    """Every jj call on the line must be a read: skip_if is an any(), so a per-call carve-out
    would let the mutation in `jj log && jj new` through unnoted."""
    discover_pack("graphite", GRAPHITE_HOOKS)
    assert_fires(dispatch_command(command, gt_repo, tmp_path), "warn", "Graphite")


@pytest.mark.parametrize(
    ("command", "rewritten"),
    [
        pytest.param("gt submit", "ccx vcs stack submit", id="gt-submit"),
        pytest.param("gt ss --no-interactive", "ccx vcs stack submit", id="gt-ss"),
        pytest.param("gt s --stack --no-edit", "ccx vcs stack submit", id="gt-s"),
        pytest.param("gt submit -d", "ccx vcs stack submit --draft", id="gt-submit-draft"),
        pytest.param("gt restack", "ccx vcs stack restack", id="gt-restack"),
        pytest.param("git status && gt submit --stack", "git status && ccx vcs stack submit", id="second-call"),
        pytest.param("gt restack && gt submit", "ccx vcs stack restack && ccx vcs stack submit", id="two-calls"),
    ],
)
def test_stack_writes_with_a_ccx_twin_are_rewritten(
    isolate_modules: None, ccx_installed: None, gt_repo: Path, tmp_path: Path, command: str, rewritten: str
) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    result = dispatch_command(command, gt_repo, tmp_path)
    assert rewritten_command(result) == rewritten
    assert result is not None
    assert "Rewrote" in result["hookSpecificOutput"]["additionalContext"]
    assert "# ccx:raw" in result["hookSpecificOutput"]["additionalContext"]
    assert "capt-hook rewrote" in result["systemMessage"]


@pytest.mark.parametrize(
    ("command", "needle"),
    [
        pytest.param("gt submit --no-stack", "ccx vcs stack submit", id="gt-submit-downstack"),
        pytest.param("gt submit --ai", "ccx vcs stack submit", id="gt-submit-unmapped-flag"),
        pytest.param("GT_DEBUG=1 gt submit", "ccx vcs stack submit", id="gt-submit-env"),
        pytest.param("timeout 60 gt submit", "ccx vcs stack submit", id="gt-submit-wrapper"),
        pytest.param("gt restack --upstack", "ccx vcs stack restack", id="gt-restack-scoped"),
        pytest.param("gt sync -f", "ccx vcs stack restack", id="gt-sync"),
        pytest.param('gt create feat -m "x"', "--new-branch", id="gt-create"),
        pytest.param('gt modify -m "x"', "ccx vcs ship --amend", id="gt-modify"),
        pytest.param("gt m -a", "ccx vcs ship --amend", id="gt-m"),
        pytest.param("git rebase main", "ccx vcs stack restack", id="git-rebase"),
        pytest.param("git rebase --onto origin/dev old-base feat", "ccx vcs stack restack", id="git-rebase-onto"),
    ],
)
def test_stack_writes_without_a_ccx_twin_are_nudged(
    isolate_modules: None, ccx_installed: None, gt_repo: Path, tmp_path: Path, command: str, needle: str
) -> None:
    """No ccx verb does the same job with these flags, so the command runs and the route is named."""
    discover_pack("graphite", GRAPHITE_HOOKS)
    result = dispatch_command(command, gt_repo, tmp_path)
    assert result is not None
    output = result["hookSpecificOutput"]
    assert "updatedInput" not in output
    assert output.get("permissionDecision") != "deny"
    assert needle in output["additionalContext"]
    assert "# ccx:raw" in output["additionalContext"]


@pytest.mark.parametrize(
    "command",
    ["gt submit # ccx:raw", "gt restack #ccx:raw", "jj new # ccx:raw", "git rebase main # ccx:raw"],
)
def test_raw_marker_runs_the_command_as_written(
    isolate_modules: None, ccx_installed: None, gt_repo: Path, tmp_path: Path, command: str
) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    result = dispatch_command(command, gt_repo, tmp_path)
    assert result is None or "updatedInput" not in result["hookSpecificOutput"]
    assert result is None or "ccx vcs stack" not in result["hookSpecificOutput"].get("additionalContext", "")


def test_raw_env_runs_every_command_as_written(
    isolate_modules: None, ccx_installed: None, gt_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    monkeypatch.setenv("CAPT_HOOK_CCX_RAW", "1")
    assert dispatch_command("gt restack", gt_repo, tmp_path) is None
    assert dispatch_command("jj new", gt_repo, tmp_path) is None


@pytest.mark.parametrize(
    "command",
    [
        "gt restack --only --branch feat",
        "gt continue",
        "gt abort",
        "gt track --parent main feat",
        "gt log",
        "gt submit --help",
        "git rebase --continue",
        "git rebase --abort",
        "git push origin feat",
        "git push --tags",
        "git push -ofoo origin feat",
        "git push -o ci.skip origin feat",
        "ccx vcs stack submit",
        'ccx vcs ship -m "gt submit --force"',
        "echo gt submit",
    ],
)
def test_ccx_escape_hatches_stay_unblocked(
    isolate_modules: None, ccx_installed: None, gt_repo: Path, tmp_path: Path, command: str
) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    assert_not_denied(dispatch_command(command, gt_repo, tmp_path))


FORCE_PUSHES = [
    pytest.param("git push --force-with-lease origin feat", id="push-lease"),
    pytest.param("git push --force-with-lease=feat:abc origin feat", id="push-lease-value"),
    pytest.param("git push -f origin feat", id="push-f"),
    pytest.param("git push -uf origin feat", id="push-short-bundle"),
    pytest.param("git push origin +feat", id="push-plus-refspec"),
]


@pytest.fixture
def ccx_lane(ccx_absent: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Install a ``ccx`` whose ``vcs lane --json`` reports the given lane."""

    def install(lane: str) -> None:
        (bindir := tmp_path / "ccx-lane-bin").mkdir()
        (ccx := bindir / "ccx").write_text(f'#!/bin/sh\nprintf \'{{"lane": "{lane}"}}\'\n')
        ccx.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    return install


@pytest.mark.parametrize("command", FORCE_PUSHES)
def test_force_push_advises_without_blocking(
    isolate_modules: None, ccx_installed: None, gt_repo: Path, tmp_path: Path, command: str
) -> None:
    """A rewritten branch has no ccx route: `ccx vcs stack submit` replays it from its recorded
    base rather than overwriting the remote, so this arm names the route and steps aside."""
    discover_pack("graphite", GRAPHITE_HOOKS)
    assert "ccx vcs stack submit" in warn_context(dispatch_command(command, gt_repo, tmp_path))


def test_a_bare_lease_push_of_the_current_branch_becomes_ccx_vcs_push(
    isolate_modules: None, ccx_installed: None, tmp_path: Path
) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    repo = real_gt_repo(tmp_path, None)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "feat"], check=True)
    for command in ["git push --force-with-lease", "git push --force-with-lease origin feat"]:
        assert rewritten_command(dispatch_command(command, repo, tmp_path)) == "ccx vcs push"
    for command in [
        "git push -f origin feat",
        "git push --force-with-lease origin other",
        "git push --force-with-lease=feat:abc origin feat",
        "git push --force-with-lease upstream feat",
    ]:
        assert "ccx vcs push" in warn_context(dispatch_command(command, repo, tmp_path))


def test_force_push_advice_admits_the_rewrite_case(
    isolate_modules: None, ccx_installed: None, gt_repo: Path, tmp_path: Path
) -> None:
    """The refusal this replaces named two routes that could not do the job."""
    discover_pack("graphite", GRAPHITE_HOOKS)
    context = warn_context(dispatch_command("git push -f origin feat", gt_repo, tmp_path))
    assert "rewrote on purpose" in context


def test_a_rewrite_on_the_line_carries_the_advice_for_the_rest(
    isolate_modules: None, ccx_installed: None, gt_repo: Path, tmp_path: Path
) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    result = dispatch_command("git push -f origin feat && gt submit", gt_repo, tmp_path)
    assert rewritten_command(result) == "git push -f origin feat && ccx vcs stack submit"
    assert result is not None
    assert "rewrote on purpose" in result["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize(("command", "kind", "needle"), HOOK_CASES)
def test_hooks_stay_silent_when_ccx_declines_the_gt_lane(
    isolate_modules: None,
    ccx_lane: Callable[[str], None],
    gt_repo: Path,
    tmp_path: Path,
    command: str,
    kind: str,
    needle: str,
) -> None:
    """`gt repo init` writes the marker for good, but submitting also needs Graphite's grant on
    the remote. Without it every ccx stack verb declines and raw git is the only route, so a pack
    steering toward gt is guarding a repo it has no business guarding."""
    discover_pack("graphite", GRAPHITE_HOOKS)
    ccx_lane("git")
    assert dispatch_command(command, gt_repo, tmp_path) is None


@pytest.mark.parametrize("command", [*FORCE_PUSHES, pytest.param("gt submit", id="gt-submit")])
def test_stack_writes_stay_silent_when_ccx_declines_the_gt_lane(
    isolate_modules: None, ccx_lane: Callable[[str], None], gt_repo: Path, tmp_path: Path, command: str
) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    ccx_lane("git")
    assert dispatch_command(command, gt_repo, tmp_path) is None


@pytest.mark.parametrize("command", [param.values[0] for param in HOOK_CASES])
def test_hooks_fire_when_ccx_rides_the_gt_lane(
    isolate_modules: None, ccx_lane: Callable[[str], None], gt_repo: Path, tmp_path: Path, command: str
) -> None:
    """The positive control: the lane probe silences the pack only where ccx says git. Severity is
    HOOK_CASES' business, and it reads the ccx-absent lane, where no arm of the stack rule fires."""
    discover_pack("graphite", GRAPHITE_HOOKS)
    ccx_lane("gt")
    assert dispatch_command(command, gt_repo, tmp_path) is not None


def test_stack_writes_rewrite_when_ccx_rides_the_gt_lane(
    isolate_modules: None, ccx_lane: Callable[[str], None], gt_repo: Path, tmp_path: Path
) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    ccx_lane("gt")
    assert rewritten_command(dispatch_command("gt submit", gt_repo, tmp_path)) == "ccx vcs stack submit"


def test_stack_writes_only_warn_without_ccx(isolate_modules: None, gt_repo: Path, tmp_path: Path) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    assert warn_context(dispatch_command("git rebase main", gt_repo, tmp_path))
    assert dispatch_command("gt restack", gt_repo, tmp_path) is None


def test_stack_writes_unblocked_in_plain_git(
    isolate_modules: None, ccx_installed: None, git_repo: Path, tmp_path: Path
) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    assert dispatch_command("git push --force-with-lease", git_repo, tmp_path) is None
    assert dispatch_command("gt submit", git_repo, tmp_path) is None


def test_rebase_onto_own_upstream_is_ccx_ships_recovery(
    isolate_modules: None, ccx_installed: None, tmp_path: Path
) -> None:
    """ccx vcs ship prints `git rebase --autostash origin/<branch>` when the remote branch moved under it."""
    discover_pack("graphite", GRAPHITE_HOOKS)
    repo = real_gt_repo(tmp_path, None)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "feat"], check=True)
    git = ["git", "-C", str(repo)]
    subprocess.run(
        [*git, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"], check=True
    )
    subprocess.run([*git, "update-ref", "refs/remotes/origin/feat", "HEAD"], check=True)
    subprocess.run([*git, "branch", "local/feat"], check=True)
    assert_not_denied(dispatch_command("git rebase --autostash origin/feat", repo, tmp_path))
    for command in [
        "git rebase origin/dev",
        "git rebase local/feat",
        "git rebase --onto=main origin/feat",
        "git rebase origin/feat $(printf other)",
    ]:
        assert_fires(dispatch_command(command, repo, tmp_path), "warn", "ccx vcs stack restack")


@pytest.fixture
def conflict_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "worktrees" / "repo" / "conflict-feat"
    (workspace / ".git").mkdir(parents=True)
    (workspace / ".git" / ".graphite_repo_config").write_text("")
    return workspace


@pytest.mark.parametrize("control", ["--continue", "--abort", "--skip"])
@pytest.mark.parametrize(
    ("shape", "from_workspace"),
    [
        pytest.param("git rebase {control}", True, id="cwd-in-workspace"),
        pytest.param("git -C {workspace} rebase {control}", False, id="git-C-workspace"),
        pytest.param("cd {workspace} && git rebase {control}", False, id="cd-then-git"),
    ],
)
def test_conflict_workspace_runs_a_manual_rebase_control_as_written(
    isolate_modules: None,
    ccx_installed: None,
    conflict_workspace: Path,
    git_repo: Path,
    tmp_path: Path,
    control: str,
    shape: str,
    from_workspace: bool,
) -> None:
    """A manual `git rebase --continue` is the workaround when `ccx vcs stack continue` itself is broken, so
    the pack names the ccx verb and runs the command untouched: never a refusal, never a rewrite."""
    discover_pack("graphite", GRAPHITE_HOOKS)
    command = shape.format(control=control, workspace=conflict_workspace)
    result = dispatch_command(command, conflict_workspace if from_workspace else git_repo, tmp_path)
    assert result is not None
    output = result["hookSpecificOutput"]
    assert output.get("permissionDecision") != "deny"
    assert "updatedInput" not in output
    assert "ccx vcs stack continue" in output["additionalContext"]


@pytest.mark.parametrize("command", ["gh pr view 42 --json state,mergedAt", "gh pr view 42 --json=mergeable"])
def test_landing_fields_nudge_toward_ccx_pr_status(
    isolate_modules: None, ccx_installed: None, gt_repo: Path, tmp_path: Path, command: str
) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    assert "ccx vcs pr status" in warn_context(dispatch_command(command, gt_repo, tmp_path))


def test_landing_nudge_stays_quiet_off_its_shape(
    isolate_modules: None, ccx_installed: None, gt_repo: Path, git_repo: Path, tmp_path: Path
) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    assert dispatch_command("gh pr view 42 --json title,body", gt_repo, tmp_path) is None
    assert dispatch_command("gh pr view 42 --json state", git_repo, tmp_path) is None
    ran = [raw_tool_msg("Bash", {"command": "ccx vcs pr status 42"})]
    assert dispatch_command("gh pr view 42 --json state", gt_repo, tmp_path, transcript=ran) is None


def test_landing_nudge_needs_ccx(isolate_modules: None, gt_repo: Path, tmp_path: Path) -> None:
    discover_pack("graphite", GRAPHITE_HOOKS)
    assert dispatch_command("gh pr view 42 --json state", gt_repo, tmp_path) is None
