from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from captain_hook import BaseHookEvent, CustomCommandLineCondition, CustomCondition
from captain_hook.util.vcs import graphite_lane

if TYPE_CHECKING:
    from cc_transcript.command import CommandLine

    from captain_hook.cmd import Call

REVIEW_SKILL_PREFIX = "cc-review"
REVIEW_COMMAND = re.compile(r"<command-name>/?cc-review", re.IGNORECASE)

JJ_INFO_FLAGS = frozenset({"-h", "--help", "-V", "--version"})
JJ_READS = frozenset(
    {
        ("log",),
        ("status",),
        ("st",),
        ("show",),
        ("diff",),
        ("evolog",),
        ("interdiff",),
        ("files",),
        ("root",),
        ("version",),
        ("help",),
        ("bookmark", "list"),
        ("tag", "list"),
        ("op", "log"),
        ("op", "show"),
        ("op", "diff"),
        ("operation", "log"),
        ("operation", "show"),
        ("operation", "diff"),
        ("file", "show"),
        ("file", "list"),
        ("config", "list"),
        ("config", "get"),
        ("config", "path"),
        ("workspace", "list"),
        ("workspace", "root"),
        ("sparse", "list"),
    }
)


def is_review_skill(skill: str) -> bool:
    return skill.startswith(REVIEW_SKILL_PREFIX) or skill.split(":", 1)[-1].startswith(REVIEW_SKILL_PREFIX)


def jj_read(call: Call) -> bool:
    verbs = tuple(target.value for target in call.targets)
    return not JJ_INFO_FLAGS.isdisjoint(call.flags) or any(verbs[: len(read)] == read for read in JJ_READS)


class GraphiteActive(CustomCondition):
    """Matches when Graphite owns the workflow at the command's effective cwd.

    A live ``gt repo init`` marker is necessary but not sufficient: a repository that sets
    ``ccx.nogt`` has opted out of the gt lane — ccx itself declines it there — so a stale
    marker must not make these hooks steer toward gt.

    The effective cwd is the last call's, not the session's: ``evt.cmd.calls()`` walks any
    leading ``cd`` on the Bash line, so ``cd other-repo && git push`` is judged at
    ``other-repo`` — reading ``evt.cwd`` directly applied this repo's Graphite policy to a
    push that landed in an unrelated plain-git repo (graphite.vcs:hook_1372d16d misfire).
    """

    def check(self, evt: BaseHookEvent) -> bool:
        calls = evt.cmd.calls()
        cwd = calls[-1].cwd if calls else evt.cwd
        return cwd is not None and graphite_lane(cwd)


class JJReads(CustomCommandLineCondition):
    """Matches when every ``jj`` call on the line is a read.

    Every call, not any: ``skip_if`` is an any() over its conditions, so a per-call match
    would let ``jj log && jj new`` skip the block the mutation earns. A verb outside the
    read set — a new one included — leaves the line blocked.
    """

    def check_command_line(self, evt: BaseHookEvent, cl: CommandLine) -> bool:
        return bool(jj := evt.cmd.calls("jj")) and all(jj_read(call) for call in jj)


@dataclass(frozen=True, slots=True)
class HasFlag(CustomCommandLineCondition):
    """Matches when any command in the parsed line carries one of the given flags.

    Structural, unlike ``Command(regex)``: a flag mentioned inside a quoted message body
    (``git commit -m "add --tags support"``) never matches.
    """

    flags: frozenset[str]

    def __init__(self, *flags: str) -> None:
        object.__setattr__(self, "flags", frozenset(flags))

    def check_command_line(self, evt: BaseHookEvent, cl: CommandLine) -> bool:
        return any(not self.flags.isdisjoint(flag.split("=")[0] for flag in call.flags) for call in evt.cmd.calls())


class PushesTagRef(CustomCommandLineCondition):
    """Matches a ``git push`` whose refspec operand targets ``refs/tags``."""

    def check_command_line(self, evt: BaseHookEvent, cl: CommandLine) -> bool:
        return any(
            call.name == "git"
            and call.targets
            and call.targets.targets[0].value == "push"
            and any(t.value.startswith("refs/tags") for t in call.targets.targets[1:])
            for call in evt.cmd.calls()
        )


class ReviewPassRan(CustomCondition):
    """Matches when a cc-review skill ran, or the user typed a /cc-review command, this session."""

    def check(self, evt: BaseHookEvent) -> bool:
        t = evt.ctx.transcript
        return any(is_review_skill(skill) for window in t.deep_inputs() for skill in window.skills) or any(
            REVIEW_COMMAND.search(turn.prompt) for turn in t.turns if turn.prompt
        )
