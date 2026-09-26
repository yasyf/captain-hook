from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from loguru import logger

from captain_hook import Allow, BaseHookEvent, Event, HookResult, Input, Tool, on
from captain_hook.builtin_packs.graphite.hooks._lib import (
    CcxInstalled,
    RawRequested,
    git_location,
    git_probe,
    graphite_owns,
)
from captain_hook.util import reqenv

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from captain_hook.cmd import Call

CHECK_TIMEOUT = 15
PUSH_SKIPS = frozenset({"--dry-run", "-n", "--delete", "-d"})
PUSH_ALL = frozenset({"--all", "--branches", "--mirror"})
GIT_HEAD_MOVES = ("commit", "merge", "rebase", "reset", "cherry-pick", "revert", "am", "pull", "checkout", "switch")
GT_HEAD_MOVES = ("create", "c", "modify", "m", "restack", "sync", "checkout", "co")
HEAD_MOVES = frozenset({("git", verb) for verb in GIT_HEAD_MOVES} | {("gt", verb) for verb in GT_HEAD_MOVES})
NEW_BRANCH_FLAGS = frozenset({"--new-branch", "--create"})
TARGET_FLAGS = ("--branch", "--bookmark")
GT_SUBMIT_VERBS = frozenset({"submit", "s", "ss"})
PR_LOOKUP = "query($owner: String!, $repo: String!) {{ repository(owner: $owner, name: $repo) {{ {fields} }} }}"
PR_FIELD = "b{index}: pullRequests(headRefName: {branch}, states: OPEN, first: 1) {{ nodes {{ number }} }}"
INCIDENT = (
    "Incident 2026-09-26: a lane pushed the release-311 fix onto #26315 while the queue held 37768ef4, "
    "and the fix was silently left out."
)


@dataclass(frozen=True, slots=True)
class Push:
    branch: str
    head: str | None


@dataclass(frozen=True, slots=True)
class Hold:
    push: Push
    number: int
    enqueued: str | None

    def render(self) -> str:
        return f"#{self.number} (`{self.push.branch}`) at `{self.enqueued or 'an unrecorded commit'}`"


def run(argv: list[str], cwd: Path) -> str | None:
    try:
        done = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=reqenv.clamp_timeout(CHECK_TIMEOUT),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        logger.bind(argv=" ".join(argv)).warning(f"queued-push check skipped: {error}")
        return None
    if done.returncode != 0:
        logger.bind(argv=" ".join(argv), stderr=done.stderr.strip()).warning(
            f"queued-push check skipped: exit {done.returncode}"
        )
        return None
    return done.stdout


def parsed(argv: list[str], cwd: Path) -> object | None:
    if (out := run(argv, cwd)) is None:
        return None
    try:
        return json.loads(out)
    except ValueError:
        logger.bind(argv=" ".join(argv), stdout=out).warning("queued-push check skipped: unparseable output")
        return None


def option(args: tuple[str, ...], names: tuple[str, ...]) -> str | None:
    for index, arg in enumerate(args):
        name, eq, value = arg.partition("=")
        if name in names:
            return value if eq else next(iter(args[index + 1 : index + 2]), None)
    return None


def lookup_dir(call: Call, session_cwd: Path | None) -> Path | None:
    cwd, git_dir = git_location(call, session_cwd)
    return git_dir or cwd


def current_branch(call: Call, session_cwd: Path | None) -> str | None:
    return git_probe(call, session_cwd, "symbolic-ref", "--short", "-q", "HEAD")


def pushed(call: Call, session_cwd: Path | None, branch: str, ref: str) -> list[Push]:
    if (head := git_probe(call, session_cwd, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}")) is None:
        logger.bind(branch=branch, ref=ref).warning("queued-push check skipped: unresolvable ref")
        return []
    return [Push(branch, head)]


def head_push(call: Call, session_cwd: Path | None) -> list[Push]:
    return pushed(call, session_cwd, branch, "HEAD") if (branch := current_branch(call, session_cwd)) else []


def every_branch(call: Call, session_cwd: Path | None) -> list[Push]:
    listed = git_probe(call, session_cwd, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads")
    return [Push(*line.split(" ", 1)) for line in (listed or "").splitlines()]


def refspec_push(call: Call, session_cwd: Path | None, spec: str) -> list[Push]:
    src, _, dst = spec.removeprefix("+").partition(":")
    if not src or (branch := dst or (current_branch(call, session_cwd) if src == "HEAD" else src)) is None:
        return []
    if (name := branch.removeprefix("refs/heads/")).startswith("refs/"):
        return []
    return pushed(call, session_cwd, name, src)


def git_pushes(call: Call, session_cwd: Path | None) -> list[Push]:
    flags = {flag.split("=", 1)[0] for flag in call.flags}
    if flags & PUSH_SKIPS or not call.targets.complete:
        return []
    specs = [target.value for target in call.targets.targets[2:]]
    if flags & PUSH_ALL or any(spec is None or spec == ":" or "*" in spec for spec in specs):
        return every_branch(call, session_cwd)
    if not specs:
        return [] if "--tags" in flags else head_push(call, session_cwd)
    return [push for spec in specs if spec is not None for push in refspec_push(call, session_cwd, spec)]


def stack_pushes(call: Call, session_cwd: Path | None, *, upstack: bool) -> list[Push] | None:
    if (cwd := lookup_dir(call, session_cwd)) is None:
        return None
    match parsed(["ccx", "vcs", "stack", "list", "--json"], cwd):
        case {"branches": list(branches)}:
            current = next((index for index, branch in enumerate(branches) if branch["current"]), -1)
            return [
                Push(branch["branch"], None) if branch.get("needs_restack") else push
                for branch in (branches if upstack else branches[: current + 1])
                for push in pushed(call, session_cwd, branch["branch"], branch["branch"])
            ]
        case _:
            return None


def stack_submit_pushes(call: Call, session_cwd: Path | None) -> list[Push] | None:
    return stack_pushes(call, session_cwd, upstack=True)


def ship_pushes(call: Call, session_cwd: Path | None) -> list[Push] | None:
    flags = {flag.split("=", 1)[0] for flag in call.flags}
    if "--no-push" in flags:
        return []
    if (downstack := stack_pushes(call, session_cwd, upstack=False)) is None:
        return None
    if (
        flags & NEW_BRANCH_FLAGS
        or "--no-commit" in flags
        or (target := option(call.args, TARGET_FLAGS) or current_branch(call, session_cwd)) is None
    ):
        return downstack
    return [push for push in downstack if push.branch != target] + [Push(target, None)]


def planner(call: Call) -> Callable[[Call, Path | None], list[Push] | None] | None:
    if not {"--help", "-h", "--dry-run"}.isdisjoint(call.flags):
        return None
    match call.name, call.verb_argv[1:4]:
        case "git", ("push", *_):
            return git_pushes
        case "ccx", ("vcs", "push", *_):
            return head_push
        case "ccx", ("vcs", "ship", *_):
            return ship_pushes
        case "ccx", ("vcs", "stack", "submit"):
            return stack_submit_pushes
        case "gt", (verb, *_) if verb in GT_SUBMIT_VERBS:
            return stack_submit_pushes
        case _:
            return None


def moves_heads(call: Call) -> bool:
    return (call.name, *call.verb_argv[1:2]) in HEAD_MOVES


def open_prs(branches: list[str], cwd: Path) -> dict[str, int] | None:
    fields = " ".join(PR_FIELD.format(index=index, branch=json.dumps(branch)) for index, branch in enumerate(branches))
    argv = ["gh", "api", "graphql", "-F", "owner={owner}", "-F", "repo={repo}", "-f"]
    match parsed([*argv, f"query={PR_LOOKUP.format(fields=fields)}"], cwd):
        case {"data": {"repository": dict(found)}}:
            return {
                branch: nodes[0]["number"]
                for index, branch in enumerate(branches)
                if (nodes := found[f"b{index}"]["nodes"])
            }
        case _:
            return None


def queue_reports(numbers: list[int], cwd: Path) -> dict[int, dict[str, str]] | None:
    match parsed(["ccx", "vcs", "pr", "status", "--json", *map(str, numbers)], cwd):
        case list(reports) if {report["number"] for report in reports} >= set(numbers):
            return {report["number"]: report for report in reports}
        case _:
            return None


def already_enqueued(push: Push, report: dict[str, str]) -> bool:
    return bool(push.head and (enqueued := report.get("enqueued")) and push.head.startswith(enqueued))


def holds(cwd: Path, planned: list[Push]) -> list[Hold]:
    if not (prs := open_prs(sorted({push.branch for push in planned}), cwd)):
        return []
    if (reports := queue_reports(sorted(set(prs.values())), cwd)) is None:
        return []
    return [
        Hold(push, number, reports[number].get("enqueued"))
        for push in planned
        if (number := prs.get(push.branch)) is not None
        and reports[number]["queue"] == "queued"
        and not already_enqueued(push, reports[number])
    ]


@on(
    Event.PreToolUse,
    only_if=[Tool("Bash"), CcxInstalled()],
    skip_if=[RawRequested()],
    tests={
        Input(command="git push", cwd="/"): Allow(),
        Input(command="ccx vcs push", cwd="/"): Allow(),
        Input(command="ccx vcs ship -m x", cwd="/"): Allow(),
        Input(command="ccx vcs stack submit", cwd="/"): Allow(),
        Input(command="gt submit", cwd="/"): Allow(),
        Input(command="git status", cwd="/"): Allow(),
    },
)
def no_push_to_a_queued_pr(evt: BaseHookEvent) -> HookResult | None:
    held: list[Hold] = []
    moved = False
    for call in evt.cmd.calls():
        if (
            (plan := planner(call)) is not None
            and graphite_owns(call, evt.cwd)
            and (cwd := lookup_dir(call, evt.cwd)) is not None
            and (planned := plan(call, evt.cwd))
        ):
            held += holds(cwd, [Push(push.branch, None) for push in planned] if moved else planned)
        moved = moved or moves_heads(call)
    if not held:
        return None
    return evt.block(
        f"This pushes to {', '.join(hold.render() for hold in held)}, which the Graphite merge queue holds, "
        "per `ccx vcs pr status`. The queue lands the commit it admitted and silently drops anything pushed "
        "after it, with every check still green. Ship the change as a new PR stacked on it: "
        '`ccx vcs stack new <name>` from that branch, then `ccx vcs ship -m "<msg>"` in the new working copy. '
        "`# ccx:raw` at the end of the command, or `CAPT_HOOK_CCX_RAW=1` for the session, pushes anyway. " + INCIDENT
    )
