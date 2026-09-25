from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from captain_hook import Allow, BaseHookEvent, Event, HookResult, Input, RanCommand, Tool, UserSaid, hook, on
from captain_hook.builtin_packs.graphite.hooks._lib import (
    CcxInstalled,
    GraphiteRuns,
    HasFlag,
    JJReads,
    PushesTagRef,
    ReviewPassRan,
    force_pushes,
    git_location,
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
        "Use `ccx vcs stack rebase` (ccx 0.65.0 or newer) to replay the stack, or `ccx vcs stack submit`, "
        "which fetches trunk, replays every lane, and submits. A raw rebase, merge, or pull leaves "
        "Graphite's parent revision stale."
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
FORCE_PUSH = (
    "`ccx vcs stack submit` restacks every lane and submits the whole stack, pushing each branch under the lease "
    "of its last submitted version. It replays a branch from its recorded base, so it is not the route for a "
    "branch whose history you rewrote on purpose, and it opens pull requests, which Graphite declines on a repo "
    "it has no access to"
)
RESTACK = (
    "`ccx vcs stack restack` fetches the remote trunk and replays every branch of the stack onto its parent, "
    "across every working copy that holds one"
)
REBASE = (
    "`ccx vcs stack rebase` (ccx 0.65.0 or newer; older releases alias it to `ccx vcs stack restack`) replays "
    "every branch from its recorded base across every working copy that holds one, and `--parent <b>=<p>` "
    "moves a branch onto another parent"
)
CONFLICT = (
    "This is a ccx conflict workspace: after `git add`, `ccx vcs stack continue` finishes the rebase and "
    "`ccx vcs stack abort` drops it (ccx 0.65.0 or newer)"
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
LANDING_FIELDS = frozenset({"state", "mergedAt", "mergeable", "mergeStateStatus", "mergeCommit"})


def in_conflict_workspace(call: Call, evt: BaseHookEvent) -> bool:
    cwd, _ = git_location(call, evt.cwd)
    return cwd is not None and "worktrees" in cwd.parts and any(part.startswith("conflict-") for part in cwd.parts)


@dataclass(frozen=True, slots=True)
class Route:
    """The ccx route for a hand-run stack write, and whether missing it is worth refusing over.

    Every blocking route has a ccx verb that does the same job. A force-push does not: the
    branch may have been rewritten on purpose, which `ccx vcs stack submit` replays away
    rather than overwrites, so that arm advises and steps aside.
    """

    advice: str
    blocking: bool = True


def ccx_route(call: Call, evt: BaseHookEvent) -> Route | None:
    argv = call.verb_argv
    if len(argv) < 2 or "--help" in call.flags or "-h" in call.flags:
        return None
    match call.name, argv[1]:
        case "gt", "restack" if "--only" in call.flags:
            return None
        case "gt", verb if verb in GT_ROUTES:
            route = Route(GT_ROUTES[verb])
        case "git", "rebase" if not REBASE_CONTROL.isdisjoint(call.flags):
            if not in_conflict_workspace(call, evt):
                return None
            route = Route(CONFLICT)
        case "git", "rebase" if not rebases_onto_own_upstream(call, evt.cwd):
            route = Route(REBASE)
        case "git", "push" if force_pushes(call):
            route = Route(FORCE_PUSH, blocking=False)
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
    routed = [(call, route) for call in evt.cmd.calls() if (route := ccx_route(call, evt)) is not None]
    if not routed:
        return None
    call, route = next((pair for pair in routed if pair[1].blocking), routed[0])
    verb = " ".join(call.verb_argv[:2])
    if not route.blocking:
        return evt.warn(
            f"`{verb}` overwrites remote history by hand, and ccx is installed. {route.advice}. A raw force-push "
            "works from the local trunk, which lags the remote, and leaves the other working copies of the stack "
            "and Graphite's parent records behind, so take the ccx route wherever it fits."
        )
    return evt.block(
        f"BLOCKED: `{verb}` rewrites a Graphite stack by hand, and ccx is installed. "
        f"{route.advice}. A hand-run gt verb works from the local trunk, which lags the remote, and "
        "leaves the other working copies of the stack and Graphite's parent records behind. "
        "`gt restack --only --branch <b>`, the conflict step a ccx refusal prints, stays open, as do "
        "`gt continue`, `gt abort`, and `git rebase --continue`/`--abort` outside a ccx conflict workspace."
    )


def landing_fields(call: Call) -> frozenset[str]:
    args = call.verb_argv
    if args[1:3] != ("pr", "view"):
        return frozenset()
    requested: set[str] = set()
    for index, arg in enumerate(args):
        if arg == "--json" and index + 1 < len(args):
            requested.update(args[index + 1].split(","))
        elif arg.startswith("--json="):
            requested.update(arg.removeprefix("--json=").split(","))
    return LANDING_FIELDS & requested


@on(
    Event.PreToolUse,
    only_if=[Tool("Bash"), CcxInstalled()],
    skip_if=[RanCommand("ccx", "vcs", "pr", "status"), RanCommand("ccx", "vcs", "status")],
    tests={
        Input(command="gh pr view 42 --json state,mergedAt", cwd="/"): Allow(),
        Input(command="gh pr view 42 --json title", cwd="/"): Allow(),
    },
)
def landing_state_through_ccx(evt: BaseHookEvent) -> HookResult | None:
    for call in evt.cmd.calls("gh"):
        if (fields := landing_fields(call)) and graphite_owns(call, evt.cwd):
            return evt.warn(
                f"`{', '.join(sorted(fields))}` misread a Graphite merge-queue landing: the queue closes what it "
                "merges, so a landed PR reads `state: CLOSED` with a null `mergedAt`, and `mergeable` says nothing "
                "about the queue. `ccx vcs pr status <n>` (ccx 0.64.1 or newer) answers queued, not queued, or "
                "landed from Graphite's own record; `ccx vcs status` covers the current stack."
            )
    return None
