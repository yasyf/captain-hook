from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from captain_hook import (
    Allow,
    Annotated,
    BaseHookEvent,
    Block,
    CommandSchema,
    CustomCommandLineCondition,
    Event,
    HookResult,
    Input,
    Operand,
    Option,
    RanCommand,
    T,
    Tool,
    UsedSkill,
    UserSaid,
    Warn,
    hook,
    on,
)
from captain_hook.app import MAX_TRANSCRIPT_EVENTS
from captain_hook.builtin_packs.graphite.hooks._lib import (
    CcxInstalled,
    GraphiteRuns,
    HasFlag,
    PushesTagRef,
    force_pushes,
    git_location,
    git_probe,
    graphite_owns,
    graphite_queue,
    in_graphite_repo,
    rebases_onto_own_upstream,
)
from captain_hook.cmd import Targets

if TYPE_CHECKING:
    from cc_transcript.command import CommandLine

    from captain_hook.cmd import Call

SUBMIT_VERBS = frozenset({"submit", "s", "ss"})
SUBMIT_FLAGS = frozenset({"--stack", "-s", "--no-edit", "-n", "--publish", "-p", "--no-interactive", "--restack"})
DRAFT_FLAGS = frozenset({"--draft", "-d"})
HELP_FLAGS = frozenset({"--help", "-h"})
REBASE_CONTROL = frozenset({"--continue", "--abort", "--skip", "--quit", "--edit-todo", "--show-current-patch"})
LEASE_FLAGS = frozenset({"--force-with-lease", "--force-if-includes"})
LANDING_FIELDS = frozenset({"state", "mergedAt", "mergeable", "mergeStateStatus", "mergeCommit"})
GH_PR_VIEW = CommandSchema(
    "gh",
    operands=(Operand("words", count="*"),),
    options=(
        Option("json", ("--json",)),
        Option("jq", ("--jq", "-q")),
        Option("template", ("--template", "-t")),
        Option("repo", ("--repo", "-R")),
        Option("mode", ("--comments", "-c", "--web", "-w", "--help", "-h"), bool),
    ),
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
    skip_if=[HasFlag("--dry-run"), HasFlag("--tags"), PushesTagRef(), Annotated("raw")],
    message=(
        "The repository this command targets keeps its stack in Graphite, so raw git commits, branches, and "
        'pushes leave it stale. Run `ccx vcs ship -m "<msg>"` instead, with `--new-branch=<name>` for a new branch.'
    ),
    max_fires=1,
    tests={
        in_graphite_repo("git commit -m x"): Warn(pattern="ccx vcs ship"),
        in_graphite_repo("git switch -c feature"): Warn(pattern="ccx vcs ship"),
        in_graphite_repo("git push --tags"): Allow(),
        in_graphite_repo("git status"): Allow(),
        Input(command="git push", cwd="/"): Allow(),
    },
)


hook(
    Event.PreToolUse,
    only_if=[
        Tool("Bash"),
        GraphiteRuns(("gt", "submit"), ("gt", "s"), ("gt", "ss"), ("ccx", "vcs", "ship")),
    ],
    skip_if=[
        UsedSkill("cc-review:start", "cc-review", scope="session"),
        UserSaid(r"<command-name>/?cc-review", scope="session"),
        HasFlag("--dry-run"),
        HasFlag("--no-push"),
        HasFlag("--help", "-h"),
        Annotated("role", scope="session"),
    ],
    message="Submit a change only after a review pass over its diff. Run `/cc-review:start` first.",
    max_fires=1,
    tests={
        in_graphite_repo("gt submit"): Warn(pattern="review pass"),
        in_graphite_repo("ccx vcs ship -m x"): Warn(pattern="review pass"),
        in_graphite_repo(
            'ccx vcs ship -m "fix: x" --new-branch=hooks/x', transcript=[T.user("ccx: role=fix\nShip the fix.")]
        ): Allow(),
        in_graphite_repo(
            'ccx vcs ship -m "fix: x" --new-branch=hooks/x', transcript=[T.user("Give this lane role=fix and ship.")]
        ): Warn(pattern="review pass"),
        in_graphite_repo("gt submit # ccx:role=fix", transcript=[T.user("Ship this change.")]): Warn(
            pattern="review pass"
        ),
        in_graphite_repo(
            "gt submit", transcript=[T.assistant(T.tool("Skill", skill="cc-review:start")), T.user("ship it")]
        ): Allow(),
        in_graphite_repo("ccx vcs ship -m x --no-push"): Allow(),
        Input(command="gt submit", cwd="/"): Allow(),
    },
)


def submits_draft(call: Call) -> bool:
    match call.name, call.verb_argv[1:3]:
        case "gt", (verb, *_) if verb in SUBMIT_VERBS:
            return not DRAFT_FLAGS.isdisjoint(call.flags)
        case "ccx", ("vcs", "ship"):
            return "--draft" in call.flags
        case _:
            return False


class DraftSubmit(CustomCommandLineCondition):
    """Matches a ``gt submit`` or ``ccx vcs ship`` that opens its pull requests as drafts in a Graphite repository."""

    def check_command_line(self, evt: BaseHookEvent, cl: CommandLine) -> bool:
        return any(submits_draft(call) and graphite_owns(call, evt.cwd) for call in evt.cmd.calls())


hook(
    Event.PreToolUse,
    only_if=[Tool("Bash"), DraftSubmit()],
    skip_if=[HasFlag("--dry-run"), HasFlag("--no-push"), HasFlag("--help", "-h")],
    message="Pull requests open ready for review, never as drafts. Submit without `--draft`.",
    max_fires=1,
    tests={
        in_graphite_repo("gt submit -d"): Warn(pattern="draft"),
        in_graphite_repo("ccx vcs ship -m x --draft"): Warn(pattern="draft"),
        in_graphite_repo("ccx vcs ship -m x --no-push --draft"): Allow(),
        in_graphite_repo("gt submit"): Allow(),
        Input(command="gt submit --draft", cwd="/"): Allow(),
    },
)


hook(  # capt-hook: no-escape a gt abort rewrites every lane's branches, so no single command can vouch for it
    Event.PreToolUse,
    only_if=[Tool("Bash"), GraphiteRuns(("gt", "abort"), ("gt", "move"))],
    skip_if=[HasFlag("--help", "-h")],
    message=(
        "`gt move` and `gt abort` are blocked because an abort resets every branch in the repository, "
        "other lanes' too, to where the interrupted command found it. "
        "Run `ccx vcs stack rebase --parent <branch>=<parent>` or `ccx vcs stack abort` instead."
    ),
    block=True,
    tests={
        in_graphite_repo("gt abort --force"): Block(pattern="ccx vcs stack abort"),
        in_graphite_repo("gt move --onto dev --no-interactive  # ccx:raw"): Block(pattern="--parent"),
        in_graphite_repo("gt abort", env={"CAPT_HOOK_CCX_RAW": "1"}): Block(pattern="every branch"),
        in_graphite_repo("gt log"): Allow(),
        in_graphite_repo("gt move --help"): Allow(),
        Input(command="gt abort", cwd="/"): Allow(),
    },
)


def in_conflict_workspace(call: Call, evt: BaseHookEvent) -> bool:
    cwd, _ = git_location(call, evt.cwd)
    return cwd is not None and "worktrees" in cwd.parts and any(part.startswith("conflict-") for part in cwd.parts)


def submit_to(call: Call) -> str | None:
    flags = frozenset(call.flags)
    if len(call.targets) != 1 or not flags <= SUBMIT_FLAGS | DRAFT_FLAGS:
        return None
    return "ccx vcs stack submit --draft" if flags & DRAFT_FLAGS else "ccx vcs stack submit"


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


def ccx_twin(call: Call, evt: BaseHookEvent) -> str | None:
    """The ccx command that does exactly what ``call`` asks, when one covers every flag and the call splices."""
    if (
        call.wrappers
        or call.source.env
        or call.leading_options
        or call.substituted
        or not call.spliceable
        or not HELP_FLAGS.isdisjoint(call.flags)
    ):
        return None
    match call.name, call.verb_argv[1:]:
        case "gt", (verb, *_) if verb in SUBMIT_VERBS:
            return submit_to(call)
        case "gt", ("restack",):
            return "ccx vcs stack restack"
        case "git", ("push", *_) if force_pushes(call):
            return lease_push_to(call, evt)
        case _:
            return None


@dataclass(frozen=True, slots=True)
class GraphiteCall(CustomCommandLineCondition):
    """Matches when one call satisfies ``predicate`` inside a repository Graphite owns."""

    predicate: Callable[[Call, BaseHookEvent], bool]

    def check_command_line(self, evt: BaseHookEvent, cl: CommandLine) -> bool:
        return any(
            HELP_FLAGS.isdisjoint(call.flags) and self.predicate(call, evt) and graphite_owns(call, evt.cwd)
            for call in evt.cmd.calls()
        )


def hand_run_rebase(call: Call, evt: BaseHookEvent) -> bool:
    return (
        call.name == "git"
        and call.verb_argv[1:2] == ("rebase",)
        and REBASE_CONTROL.isdisjoint(call.flags)
        and not rebases_onto_own_upstream(call, evt.cwd)
    )


def conflict_rebase_control(call: Call, evt: BaseHookEvent) -> bool:
    return (
        call.name == "git"
        and call.verb_argv[1:2] == ("rebase",)
        and not REBASE_CONTROL.isdisjoint(call.flags)
        and in_conflict_workspace(call, evt)
    )


@on(
    Event.PreToolUse,
    only_if=[Tool("Bash"), CcxInstalled()],
    skip_if=[Annotated("raw")],
    tests={
        Input(command="gt submit", cwd="/"): Allow(),
        Input(command="gt restack", cwd="/"): Allow(),
        Input(command="git push --force-with-lease", cwd="/"): Allow(),
        Input(command="git status", cwd="/"): Allow(),
    },
)
def stack_writes_go_through_ccx(evt: BaseHookEvent) -> HookResult | None:
    rewritten: HookResult | None = None
    swaps: list[str] = []
    for call in evt.cmd.calls():
        if (
            (twin := ccx_twin(call, evt)) is not None
            and graphite_owns(call, evt.cwd)
            and (result := call.sub(call.name, twin, args=Targets())) is not None
        ):
            rewritten = result
            swaps.append(f"`{call.source.raw}` → `{twin}`")
    if rewritten is None:
        return None
    return replace(
        rewritten,
        note=(
            "Rewrote a hand-run Graphite stack write to its ccx twin. "
            "End a command with `# ccx:raw` to run it as written."
        ),
        system_message=f"capt-hook rewrote {', '.join(swaps)}",
    )


@on(
    Event.PreToolUse,
    only_if=[Tool("Bash"), CcxInstalled(), GraphiteCall(hand_run_rebase)],
    skip_if=[Annotated("raw")],
    max_fires=1,
    tests={Input(command="git rebase main", cwd="/"): Allow(), Input(command="git rebase --abort", cwd="/"): Allow()},
)
def git_rebase_through_ccx(evt: BaseHookEvent) -> HookResult | None:
    return evt.context(
        "A raw `git rebase` leaves Graphite's parent records stale. "
        "Run `ccx vcs stack rebase` to replay the stack instead."
    )


@on(
    Event.PreToolUse,
    only_if=[Tool("Bash"), CcxInstalled(), GraphiteCall(conflict_rebase_control)],
    skip_if=[Annotated("raw")],
    max_fires=1,
    tests={
        Input(command="git rebase --continue", cwd="/"): Allow(),
        Input(command="git rebase main", cwd="/"): Allow(),
    },
)
def conflict_workspace_finishes_through_ccx(evt: BaseHookEvent) -> HookResult | None:
    return evt.context(
        "A ccx conflict workspace finishes its stack rebase through ccx. After `git add`, run `ccx vcs stack continue`."
    )


def requests_landing_fields(call: Call) -> bool:
    arguments = GH_PR_VIEW.bind(call)
    requested = {field for value in arguments.values.get("json", ()) for field in str(value or "").split(",")}
    return arguments.values.get("words", ())[:2] == ("pr", "view") and not LANDING_FIELDS.isdisjoint(requested)


class LandingStateQuery(CustomCommandLineCondition):
    def check_command_line(self, evt: BaseHookEvent, cl: CommandLine) -> bool:
        return any(requests_landing_fields(call) for call in evt.cmd.calls("gh"))


@on(
    Event.PreToolUse,
    only_if=[Tool("Bash"), CcxInstalled(), LandingStateQuery()],
    skip_if=[RanCommand("ccx", "vcs", "pr", "status"), RanCommand("ccx", "vcs", "status")],
    max_fires=1,
    transcript_events=MAX_TRANSCRIPT_EVENTS,
    tests={
        Input(command="gh pr view 42 --json state,mergedAt", cwd="/"): Allow(),
        Input(command="gh pr view 42 --json title", cwd="/"): Allow(),
        in_graphite_repo("gh pr view 42 --json state,mergedAt", queue="trunk"): Allow(),
    },
)
def landing_state_through_ccx(evt: BaseHookEvent) -> HookResult | None:
    if any(
        requests_landing_fields(call) and graphite_owns(call, evt.cwd) and graphite_queue(call, evt.cwd)
        for call in evt.cmd.calls("gh")
    ):
        return evt.warn(
            "Graphite's merge queue closes the pull requests it lands, so `gh pr view` misreports a landing. "
            "Run `ccx vcs pr status <n>` instead."
        )
    return None
