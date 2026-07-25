from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import chain
from typing import TYPE_CHECKING

from cc_transcript.tools import SkillCall

from captain_hook import BaseHookEvent, CustomCommandLineCondition, CustomCondition
from captain_hook.util.vcs import is_graphite_repo

if TYPE_CHECKING:
    from cc_transcript.command import CommandLine

REVIEW_SKILL_PREFIX = "cc-review"
REVIEW_COMMAND = re.compile(r"<command-name>/?cc-review", re.IGNORECASE)


def is_review_skill(skill: str) -> bool:
    return skill.startswith(REVIEW_SKILL_PREFIX) or skill.split(":", 1)[-1].startswith(REVIEW_SKILL_PREFIX)


class GraphiteActive(CustomCondition):
    """Matches when the session cwd sits inside a repo initialized with ``gt repo init``."""

    def check(self, evt: BaseHookEvent) -> bool:
        return evt.cwd is not None and is_graphite_repo(evt.cwd)


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
        return any(
            isinstance(call := use.call, SkillCall) and is_review_skill(call.skill)
            for s in chain((t,), (d.session for d in t.walk()))
            for use in s.tool_calls.named("Skill")
        ) or any(REVIEW_COMMAND.search(turn.prompt) for turn in t.turns if turn.prompt)
