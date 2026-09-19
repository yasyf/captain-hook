from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from captain_hook import BaseHookEvent, CustomCommandLineCondition, CustomCondition
from captain_hook.util.vcs import graphite_lane

if TYPE_CHECKING:
    from cc_transcript.command import CommandLine

    from captain_hook.cmd import Call

VCS_COMMANDS = frozenset({"git", "jj", "gt", "ccx"})
GIT_DIR_FLAGS = ("-C", "--git-dir")
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


def git_dir_values(flags: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    tokens = iter(flags)
    for token in tokens:
        if token in GIT_DIR_FLAGS:
            if (value := next(tokens, None)) is not None:
                values.append(value)
        elif token.startswith("-C") and len(token) > 2:
            values.append(token[2:])
        elif token.startswith("--git-dir="):
            values.append(token.removeprefix("--git-dir="))
    return values


def call_directory(call: Call, fallback: Path | None) -> Path | None:
    """The directory a VCS call operates in: its ``cd``-scoped cwd, then any ``git -C``/``--git-dir`` hop.

    An unresolvable ``cd`` (``cd $OTHER``) leaves the call's cwd unknown, so the session cwd
    stands in — the judgment every hook made before the target was resolved at all.
    """
    directory = call.cwd or fallback
    for value in git_dir_values(call.flags):
        path = Path(os.path.expanduser(value))
        directory = path if path.is_absolute() else (directory / path if directory is not None else None)
    return directory


class GraphiteActive(CustomCommandLineCondition):
    """Matches when Graphite owns the workflow in the repository a VCS call on the line targets.

    Judged where the call runs — after a leading ``cd``, or through ``git -C``/``--git-dir`` —
    not at the session cwd, so a ``cd ../plain-git-repo && git push`` from a Graphite session is
    left alone and a ``cd ../gt-repo && git push`` from a plain-git session is not. A live
    ``gt repo init`` marker is necessary but not sufficient: a repository that sets ``ccx.nogt``
    has opted out of the gt lane — ccx itself declines it there — so a stale marker must not
    make these hooks steer toward gt.
    """

    def check_command_line(self, evt: BaseHookEvent, cl: CommandLine) -> bool:
        return any(
            (directory := call_directory(call, evt.cwd)) is not None and graphite_lane(directory)
            for call in evt.cmd.calls()
            if call.name in VCS_COMMANDS
        )


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
