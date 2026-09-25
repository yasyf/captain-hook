from __future__ import annotations

from typing import TYPE_CHECKING

from captain_hook import Allow, BaseHookEvent, Event, HookResult, Input, Tool, UserSaid, hook, on
from captain_hook.builtin_packs.graphite.hooks._lib import (
    CcxInstalled,
    GraphiteRuns,
    HasFlag,
    JJReads,
    PushesTagRef,
    ReviewPassRan,
    force_pushes,
    graphite_owns,
    rebases_onto_own_upstream,
)

if TYPE_CHECKING:
    from captain_hook.cmd import Call

# Inline tests are Allow-only: a FileFixture can't stage a nested `.git/.graphite_repo_config`, so
# GraphiteRuns() is always off at cwd="/" and every matcher short-circuits to allow. The fire path
# (marker present, in a real gt repo/worktree) is covered by tests/test_pack_graphite.py, and so are
# the two things inline rows cannot see: the ccx.nogt opt-out, and the JJReads carve-out, which
# matches_conditions collapses into the same allow a failed only_if produces.

hook(
    Event.PreToolUse,
    only_if=[Tool("Bash"), GraphiteRuns(("jj",))],
    skip_if=[JJReads()],
    message=(
        "BLOCKED: the repository this command targets runs on Graphite (gt), not jj — its stack metadata "
        "lives in Graphite. Use `ccx vcs ship` to commit and submit, `ccx vcs stack new <name>` to cut a "
        "stacked branch, and `gt log` or `ccx vcs stack list` to inspect the stack."
    ),
    block=True,
    tests={
        Input(command="jj new", cwd="/"): Allow(),
        Input(command="jj commit -m x", cwd="/"): Allow(),
        Input(command="git status", cwd="/"): Allow(),
        Input(command="jj log", cwd="/"): Allow(),
        Input(command="jj bookmark list", cwd="/"): Allow(),
        Input(command="cd /tmp && jj new", cwd="/"): Allow(),
    },
)


hook(
    Event.PreToolUse,
    only_if=[
        Tool("Bash"),
        GraphiteRuns(
            ("git", "commit"),
            ("git", "push"),
            ("git", "switch", "-c"),
            ("git", "switch", "-C"),
            ("git", "switch", "--create"),
            ("git", "checkout", "-b"),
            ("git", "checkout", "-B"),
        ),
    ],
    skip_if=[HasFlag("--dry-run"), HasFlag("--tags"), PushesTagRef()],
    message=(
        "Committing, branching, and pushing go through ccx vcs in the repository this command targets. "
        'Use `ccx vcs ship -m "<msg>"` to commit and submit, `ccx vcs stack new <name>` to cut a stacked '
        "branch, and `ccx vcs stack submit` to restack and resubmit the stack. Raw git writes leave "
        "Graphite's stack metadata stale."
    ),
    tests={
        Input(command="git commit -m x", cwd="/"): Allow(),
        Input(command="git push", cwd="/"): Allow(),
        Input(command="git switch -c feature", cwd="/"): Allow(),
        Input(command="git switch -C main", cwd="/"): Allow(),
        Input(command="git status", cwd="/"): Allow(),
        Input(command="cd /tmp && git push", cwd="/"): Allow(),
        Input(command="git -C /tmp push", cwd="/"): Allow(),
    },
)


hook(
    Event.PreToolUse,
    only_if=[
        Tool("Bash"),
        GraphiteRuns(("gt", "submit"), ("gt", "s"), ("gt", "ss"), ("ccx", "vcs", "ship")),
    ],
    skip_if=[
        ReviewPassRan(),
        UserSaid(r"<command-name>/?cc-review", scope="session"),
        HasFlag("--dry-run"),
        HasFlag("--no-push"),
    ],
    message=(
        "Before submitting: no review pass has run this session — run one first (`/cc-review:start`), the "
        "single finder pass over the diff a non-trivial change carries. And PRs are always published, "
        "never draft: submit without `--draft`/`-d`."
    ),
    tests={
        Input(command="gt submit", cwd="/"): Allow(),
        Input(command="gt ss", cwd="/"): Allow(),
        Input(command="ccx vcs ship -m x", cwd="/"): Allow(),
        Input(command="git status", cwd="/"): Allow(),
    },
)


hook(
    Event.PreToolUse,
    only_if=[
        Tool("Bash"),
        GraphiteRuns(("git", "rebase"), ("git", "merge"), ("git", "pull")),
    ],
    skip_if=[HasFlag("--abort", "--continue", "--quit")],
    message=(
        "Use `ccx vcs stack submit`, which fetches trunk and replays every lane. A raw rebase, merge, "
        "or pull leaves Graphite's parent revision stale."
    ),
    tests={
        Input(command="git rebase main", cwd="/"): Allow(),
        Input(command="git pull", cwd="/"): Allow(),
        Input(command="git status", cwd="/"): Allow(),
    },
)


SUBMIT = (
    "`ccx vcs stack submit` restacks every lane and submits the whole stack, and `ccx vcs ship --no-commit` "
    "submits this branch and its downstack. Both fetch the remote trunk first and push each branch under the "
    "lease of its last submitted version"
)
RESTACK = (
    "`ccx vcs stack restack` fetches the remote trunk and replays every branch of the stack onto its parent, "
    "across every working copy that holds one"
)
CREATE = (
    '`ccx vcs ship -m "<msg>" --new-branch=<name>` commits onto a new stacked branch, and '
    "`ccx vcs stack new <name>` cuts one into a working copy of its own"
)
MODIFY = (
    '`ccx vcs ship -m "<msg>"` commits onto this branch, and `ccx vcs ship --amend` folds the change into its commit'
)
GT_ROUTES = {
    "submit": SUBMIT,
    "s": SUBMIT,
    "ss": SUBMIT,
    "restack": RESTACK,
    "sync": RESTACK,
    "create": CREATE,
    "c": CREATE,
    "modify": MODIFY,
    "m": MODIFY,
}
REBASE_CONTROL = frozenset({"--continue", "--abort", "--skip", "--quit", "--edit-todo", "--show-current-patch"})


def ccx_route(call: Call, evt: BaseHookEvent) -> str | None:
    argv = call.verb_argv
    if len(argv) < 2 or "--help" in call.flags or "-h" in call.flags:
        return None
    match call.name, argv[1]:
        case "gt", "restack" if "--only" in call.flags:
            return None
        case "gt", verb if verb in GT_ROUTES:
            route = GT_ROUTES[verb]
        case "git", "rebase" if REBASE_CONTROL.isdisjoint(call.flags) and not rebases_onto_own_upstream(call, evt.cwd):
            route = RESTACK
        case "git", "push" if force_pushes(call):
            route = SUBMIT
        case _:
            return None
    return route if graphite_owns(call, evt.cwd) else None


@on(
    Event.PreToolUse,
    only_if=[Tool("Bash"), CcxInstalled()],
    tests={
        Input(command="gt submit", cwd="/"): Allow(),
        Input(command="gt restack", cwd="/"): Allow(),
        Input(command="git rebase --onto main a b", cwd="/"): Allow(),
        Input(command="git push --force-with-lease", cwd="/"): Allow(),
        Input(command="git status", cwd="/"): Allow(),
    },
)
def stack_writes_go_through_ccx(evt: BaseHookEvent) -> HookResult | None:
    for call in evt.cmd.calls():
        if (route := ccx_route(call, evt)) is not None:
            return evt.block(
                f"BLOCKED: `{' '.join(call.verb_argv[:2])}` rewrites a Graphite stack by hand, and ccx is installed. "
                f"{route}. A hand-run gt verb or force-push works from the local trunk, which lags the remote, and "
                "leaves the other working copies of the stack and Graphite's parent records behind. "
                "`gt restack --only --branch <b>`, the conflict step a ccx refusal prints, stays open, as do "
                "`gt continue`, `gt abort`, and `git rebase --continue`/`--abort`."
            )
    return None
