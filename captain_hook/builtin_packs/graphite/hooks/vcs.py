from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from captain_hook import (
    Allow,
    Annotated,
    BaseHookEvent,
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
    JJReads,
    PushesTagRef,
    force_pushes,
    git_location,
    git_probe,
    graphite_owns,
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
    only_if=[Tool("Bash"), GraphiteRuns(("jj",))],
    skip_if=[JJReads(), Annotated("raw")],
    message=(
        "The repository this command targets keeps its stack in Graphite, which jj writes leave stale. "
        'Run `ccx vcs ship -m "<msg>"` to commit and submit instead.'
    ),
    tests={
        in_graphite_repo("jj new"): Warn(pattern="Graphite"),
        in_graphite_repo("jj log && jj new"): Warn(pattern="Graphite"),
        in_graphite_repo("jj log"): Allow(),
        in_graphite_repo("jj new # ccx:raw"): Allow(),
        in_graphite_repo("bash -c 'jj new # ccx:raw'"): Allow(),
        in_graphite_repo("jj new && echo '# ccx:raw'"): Warn(pattern="Graphite"),
        in_graphite_repo("jj new", env={"CAPT_HOOK_CCX_RAW": "0"}): Warn(pattern="Graphite"),
        in_graphite_repo("jj new", env={"CAPT_HOOK_CCX_RAW": "true"}): Allow(),
        Input(command="jj new", cwd="/"): Allow(),
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
    skip_if=[HasFlag("--dry-run"), HasFlag("--tags"), PushesTagRef(), Annotated("raw")],
    message=(
        "The repository this command targets keeps its stack in Graphite, so raw git commits, branches, and "
        'pushes leave it stale. Run `ccx vcs ship -m "<msg>"` instead.'
    ),
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
    tests={
        in_graphite_repo("gt submit"): Warn(pattern="review pass"),
        in_graphite_repo("ccx vcs ship -m x"): Warn(pattern="review pass"),
        in_graphite_repo(
            'ccx vcs ship -m "fix: x" --new-branch=hooks/x', transcript=[T.user("ccx: role=fix\nShip the fix.")]
        ): Allow(),
        in_graphite_repo(
            'ccx vcs ship -m "fix: x" --new-branch=hooks/x', transcript=[T.user("Give this lane role=fix and ship.")]
        ): Warn(pattern="review pass"),
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
    tests={
        in_graphite_repo("gt submit -d"): Warn(pattern="draft"),
        in_graphite_repo("ccx vcs ship -m x --draft"): Warn(pattern="draft"),
        in_graphite_repo("ccx vcs ship -m x --no-push --draft"): Allow(),
        in_graphite_repo("gt submit"): Allow(),
        Input(command="gt submit --draft", cwd="/"): Allow(),
    },
)


hook(
    Event.PreToolUse,
    only_if=[
        Tool("Bash"),
        GraphiteRuns(("git", "rebase"), ("git", "merge"), ("git", "pull")),
    ],
    skip_if=[HasFlag("--abort", "--continue", "--quit"), Annotated("raw")],
    message=(
        "A raw `git rebase`, `merge`, or `pull` leaves Graphite's parent records stale. "
        "Run `ccx vcs stack rebase` to replay the stack instead."
    ),
    tests={
        in_graphite_repo("git rebase main"): Warn(pattern="ccx vcs stack rebase"),
        in_graphite_repo("git pull"): Warn(pattern="ccx vcs stack rebase"),
        in_graphite_repo("git rebase --abort"): Allow(),
        Input(command="git rebase main", cwd="/"): Allow(),
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


def hand_run_submit(call: Call, evt: BaseHookEvent) -> bool:
    return call.name == "gt" and call.verb_argv[1:2] in {(verb,) for verb in SUBMIT_VERBS} and not ccx_twin(call, evt)


def hand_run_restack(call: Call, evt: BaseHookEvent) -> bool:
    match call.name, call.verb_argv[1:2]:
        case "gt", ("sync",):
            return True
        case "gt", ("restack",):
            return "--only" not in call.flags and not ccx_twin(call, evt)
        case _:
            return False


def hand_run_create(call: Call, evt: BaseHookEvent) -> bool:
    return call.name == "gt" and call.verb_argv[1:2] in {("create",), ("c",)}


def hand_run_modify(call: Call, evt: BaseHookEvent) -> bool:
    return call.name == "gt" and call.verb_argv[1:2] in {("modify",), ("m",)}


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


def hand_run_force_push(call: Call, evt: BaseHookEvent) -> bool:
    return call.name == "git" and call.verb_argv[1:2] == ("push",) and force_pushes(call) and not ccx_twin(call, evt)


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
    only_if=[Tool("Bash"), CcxInstalled(), GraphiteCall(hand_run_submit)],
    skip_if=[Annotated("raw")],
    tests={Input(command="gt submit --ai", cwd="/"): Allow(), Input(command="git status", cwd="/"): Allow()},
)
def gt_submit_through_ccx(evt: BaseHookEvent) -> HookResult | None:
    return evt.context(
        "A hand-run `gt submit` skips the trunk fetch and push lease ccx adds. Run `ccx vcs stack submit` instead."
    )


@on(
    Event.PreToolUse,
    only_if=[Tool("Bash"), CcxInstalled(), GraphiteCall(hand_run_restack)],
    skip_if=[Annotated("raw")],
    tests={Input(command="gt sync", cwd="/"): Allow(), Input(command="gt log", cwd="/"): Allow()},
)
def gt_restack_through_ccx(evt: BaseHookEvent) -> HookResult | None:
    return evt.context(
        "A hand-run `gt restack` or `gt sync` leaves the stack's other working copies behind. "
        "Run `ccx vcs stack restack` instead."
    )


@on(
    Event.PreToolUse,
    only_if=[Tool("Bash"), CcxInstalled(), GraphiteCall(hand_run_create)],
    skip_if=[Annotated("raw")],
    tests={Input(command="gt create feat -m x", cwd="/"): Allow(), Input(command="gt log", cwd="/"): Allow()},
)
def gt_create_through_ccx(evt: BaseHookEvent) -> HookResult | None:
    return evt.context(
        "A hand-run `gt create` bypasses the stack records ccx keeps. "
        'Run `ccx vcs ship -m "<msg>" --new-branch=<name>` instead.'
    )


@on(
    Event.PreToolUse,
    only_if=[Tool("Bash"), CcxInstalled(), GraphiteCall(hand_run_modify)],
    skip_if=[Annotated("raw")],
    tests={Input(command="gt modify -m x", cwd="/"): Allow(), Input(command="gt log", cwd="/"): Allow()},
)
def gt_modify_through_ccx(evt: BaseHookEvent) -> HookResult | None:
    return evt.context(
        "A hand-run `gt modify` bypasses the stack records ccx keeps. Run `ccx vcs ship --amend` instead."
    )


@on(
    Event.PreToolUse,
    only_if=[Tool("Bash"), CcxInstalled(), GraphiteCall(hand_run_rebase)],
    skip_if=[Annotated("raw")],
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
    tests={
        Input(command="git rebase --continue", cwd="/"): Allow(),
        Input(command="git rebase main", cwd="/"): Allow(),
    },
)
def conflict_workspace_finishes_through_ccx(evt: BaseHookEvent) -> HookResult | None:
    return evt.context(
        "A ccx conflict workspace finishes its stack rebase through ccx. After `git add`, run `ccx vcs stack continue`."
    )


@on(
    Event.PreToolUse,
    only_if=[Tool("Bash"), CcxInstalled(), GraphiteCall(hand_run_force_push)],
    skip_if=[Annotated("raw")],
    tests={Input(command="git push -f origin feat", cwd="/"): Allow(), Input(command="git push", cwd="/"): Allow()},
)
def force_push_through_ccx(evt: BaseHookEvent) -> HookResult | None:
    return evt.context(
        "A raw force-push skips the lease ccx pins to the head this branch last held. Run `ccx vcs push` instead."
    )


def requests_landing_fields(call: Call) -> bool:
    arguments = GH_PR_VIEW.bind(call)
    requested = {field for value in arguments.values.get("json", ()) for field in str(value or "").split(",")}
    return arguments.values.get("words", ())[:2] == ("pr", "view") and not LANDING_FIELDS.isdisjoint(requested)


@on(
    Event.PreToolUse,
    only_if=[Tool("Bash"), CcxInstalled()],
    skip_if=[RanCommand("ccx", "vcs", "pr", "status"), RanCommand("ccx", "vcs", "status")],
    transcript_events=MAX_TRANSCRIPT_EVENTS,
    tests={
        Input(command="gh pr view 42 --json state,mergedAt", cwd="/"): Allow(),
        Input(command="gh pr view 42 --json title", cwd="/"): Allow(),
    },
)
def landing_state_through_ccx(evt: BaseHookEvent) -> HookResult | None:
    if any(requests_landing_fields(call) and graphite_owns(call, evt.cwd) for call in evt.cmd.calls("gh")):
        return evt.warn(
            "Graphite's merge queue closes the pull requests it lands, so `gh pr view` misreports a landing. "
            "Run `ccx vcs pr status <n>` instead."
        )
    return None
