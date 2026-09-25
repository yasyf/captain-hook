from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from captain_hook import Allow, BaseHookEvent, Event, HookResult, Input, RanCommand, Tool, UserSaid, hook, on
from captain_hook.builtin_packs.graphite.hooks._lib import (
    CcxInstalled,
    GraphiteRuns,
    HasFlag,
    JJReads,
    PushesTagRef,
    RawRequested,
    ReviewPassRan,
    force_pushes,
    git_location,
    git_probe,
    graphite_owns,
    rebases_onto_own_upstream,
)
from captain_hook.cmd import Targets

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
    skip_if=[JJReads(), RawRequested()],
    message=(
        "The repository this command targets runs on Graphite (gt), not jj: its stack metadata lives in "
        "Graphite, and a jj write leaves that metadata stale. `ccx vcs ship` commits and submits, "
        "`ccx vcs stack new <name>` cuts a stacked branch, and `gt log` or `ccx vcs stack list` inspects the "
        "stack. To run a jj write as written without this note, end it with `# ccx:raw`."
    ),
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
    skip_if=[HasFlag("--dry-run"), HasFlag("--tags"), PushesTagRef(), RawRequested()],
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
        HasFlag("--help", "-h"),
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
    skip_if=[HasFlag("--abort", "--continue", "--quit"), RawRequested()],
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
    "`ccx vcs push` moves this branch's remote to the local head: it fetches, fast-forwards when it can, "
    "force-pushes under a lease pinned to the head it observed when the branch was rewritten, and refuses a "
    "remote head this branch never held. `ccx vcs stack submit` restacks and submits the whole stack, but it "
    "replays a branch from its recorded base, so it is not the route for a branch whose history you rewrote on "
    "purpose"
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
HAND_RUN = (
    "A hand-run stack write works from the local trunk, which lags the remote, and leaves the other working "
    "copies of the stack and Graphite's parent records behind"
)
OVERRIDE = (
    "`# ccx:raw` at the end of a command, or `CAPT_HOOK_CCX_RAW=1` for the session, runs it as written "
    "and silences this note."
)
GT_ROUTES = {
    "sync": RESTACK,
    "create": CREATE,
    "c": CREATE,
    "modify": MODIFY,
    "m": MODIFY,
}
SUBMIT_VERBS = frozenset({"submit", "s", "ss"})
SUBMIT_FLAGS = frozenset({"--stack", "-s", "--no-edit", "-n", "--publish", "-p", "--no-interactive", "--restack"})
DRAFT_FLAGS = frozenset({"--draft", "-d"})
REBASE_CONTROL = frozenset({"--continue", "--abort", "--skip", "--quit", "--edit-todo", "--show-current-patch"})
REBASE_RESUMES = {"--continue": "continue", "--abort": "abort"}
LEASE_FLAGS = frozenset({"--force-with-lease", "--force-if-includes"})
LANDING_FIELDS = frozenset({"state", "mergedAt", "mergeable", "mergeStateStatus", "mergeCommit"})


def in_conflict_workspace(call: Call, evt: BaseHookEvent) -> bool:
    cwd, _ = git_location(call, evt.cwd)
    return cwd is not None and "worktrees" in cwd.parts and any(part.startswith("conflict-") for part in cwd.parts)


@dataclass(frozen=True, slots=True)
class Route:
    """The ccx route for a hand-run stack write, and the ccx command that replaces it when one does the same job.

    ``to`` is set only where the ccx verb covers every flag the call carries, so the rewrite drops nothing
    the caller asked for. Every other route, a force-push included, allows the command and names the route.
    """

    advice: str
    to: str | None = None
    act: str = "rewrites a Graphite stack by hand"


def submit_to(call: Call) -> str | None:
    flags = frozenset(call.flags)
    if len(call.targets) != 1 or not flags <= SUBMIT_FLAGS | DRAFT_FLAGS:
        return None
    return "ccx vcs stack submit --draft" if flags & DRAFT_FLAGS else "ccx vcs stack submit"


def resume_to(call: Call) -> str | None:
    if len(call.flags) != 1 or len(call.targets) != 1 or (verb := REBASE_RESUMES.get(call.flags[0])) is None:
        return None
    return f"ccx vcs stack {verb}"


def lease_push_to(call: Call, evt: BaseHookEvent) -> str | None:
    """``ccx vcs push`` for a bare-lease push of the checked-out branch to ``origin``, the one push it makes.

    A plain ``--force`` overwrites a divergence ``ccx vcs push`` refuses, and a ``--force-with-lease=<ref>``
    pins a lease of its own, so neither is rewritten.
    """
    flags = frozenset(call.flags)
    refs = tuple(target.value for target in call.targets.targets[1:])
    if (
        "--force-with-lease" not in flags
        or not flags <= LEASE_FLAGS
        or len(refs) > 2
        or refs[:1] not in {(), ("origin",)}
    ):
        return None
    if len(refs) == 2 and refs[1] != git_probe(call, evt.cwd, "symbolic-ref", "--short", "-q", "HEAD"):
        return None
    return "ccx vcs push"


def ccx_route(call: Call, evt: BaseHookEvent) -> Route | None:
    argv = call.verb_argv
    if len(argv) < 2 or "--help" in call.flags or "-h" in call.flags:
        return None
    match call.name, argv[1]:
        case "gt", "restack" if "--only" in call.flags:
            return None
        case "gt", verb if verb in SUBMIT_VERBS:
            route = Route(SUBMIT, submit_to(call))
        case "gt", "restack":
            route = Route(RESTACK, "ccx vcs stack restack" if argv == ("gt", "restack") else None)
        case "gt", verb if verb in GT_ROUTES:
            route = Route(GT_ROUTES[verb])
        case "git", "rebase" if not REBASE_CONTROL.isdisjoint(call.flags):
            if not in_conflict_workspace(call, evt):
                return None
            route = Route(CONFLICT, resume_to(call))
        case "git", "rebase" if not rebases_onto_own_upstream(call, evt.cwd):
            route = Route(REBASE)
        case "git", "push" if force_pushes(call):
            route = Route(FORCE_PUSH, lease_push_to(call, evt), act="overwrites remote history by hand")
        case _:
            return None
    return route if graphite_owns(call, evt.cwd) else None


def rewrite(call: Call, route: Route) -> HookResult | None:
    """Splice ``route.to`` over the call, or ``None`` when a wrapper, env prefix, or global option would be lost."""
    if route.to is None or call.wrappers or call.source.env or call.leading_options:
        return None
    return call.sub(call.name, route.to, args=Targets())


@on(
    Event.PreToolUse,
    only_if=[Tool("Bash"), CcxInstalled()],
    skip_if=[RawRequested()],
    tests={
        Input(command="gt submit", cwd="/"): Allow(),
        Input(command="gt restack", cwd="/"): Allow(),
        Input(command="git rebase --onto main a b", cwd="/"): Allow(),
        Input(command="git push --force-with-lease", cwd="/"): Allow(),
        Input(command="git status", cwd="/"): Allow(),
    },
)
def stack_writes_go_through_ccx(evt: BaseHookEvent) -> HookResult | None:
    rewritten: HookResult | None = None
    swaps: list[str] = []
    notes: list[str] = []
    for call in evt.cmd.calls():
        if (route := ccx_route(call, evt)) is None:
            continue
        verb = " ".join(call.verb_argv[:2])
        if (result := rewrite(call, route)) is not None:
            rewritten = result
            swaps.append(f"`{call.source.raw}` → `{route.to}`")
            notes.append(f"Rewrote `{call.source.raw}` → `{route.to}`: {route.advice}.")
        else:
            notes.append(f"`{verb}` {route.act}, and ccx is the default route here. {route.advice}. {HAND_RUN}.")
    if not notes:
        return None
    note = "\n".join([*notes, OVERRIDE])
    if rewritten is None:
        return evt.context(note)
    return replace(rewritten, note=note, system_message=f"capt-hook rewrote {', '.join(swaps)}")


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
