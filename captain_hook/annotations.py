"""The ``ccx:`` annotation notation: one escape and intent grammar for every hook to read."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from itertools import chain
from typing import TYPE_CHECKING, Literal

from cc_transcript.tools import SkillCall, TaskCall

from captain_hook import ast_grep
from captain_hook.snapshots.client import RemoteSession
from captain_hook.types import CustomCondition
from captain_hook.util import reqenv

if TYPE_CHECKING:
    from cc_transcript.command import CommandLine

    from captain_hook.events import BaseHookEvent

WORD = r"[a-z0-9][a-z0-9:._-]*"
COMMENT_TOKEN = re.compile(rf"ccx:({WORD})(?:=({WORD}))?")
DISPATCH_LINE = re.compile(rf"ccx:((?:[ \t]+{WORD}(?:={WORD})?)+)")
DISPATCH_PAIR = re.compile(rf"({WORD})(?:=({WORD}))?")
RAW_ENV = "CAPT_HOOK_CCX_RAW"
TRUTHY = frozenset({"1", "true", "yes"})

type Annotations = Mapping[str, str | None]
type Pair = tuple[str, str | None]


def pair(key: str, value: str) -> Pair:
    return key, value or None


def payloads(cl: CommandLine) -> dict[tuple[int, int], str]:
    return {
        word.span: word.value
        for occurrence in cl.occurrences
        if (host := occurrence.host) is not None and (inner := occurrence.command.span) is not None
        for word in host.command.words
        if word.span is not None and word.value and word.span[0] <= inner[0] and inner[1] <= word.span[1]
    }


def shell_comments(cl: CommandLine) -> Iterator[str]:
    """Every real shell comment on the line, nested ``sh -c`` and ``eval`` payloads included."""
    for source in (cl.raw, *payloads(cl).values()):
        yield from (comment.text for comment in ast_grep.comments(source, "bash"))


def comment_pairs(cl: CommandLine) -> Iterator[Pair]:
    for comment in shell_comments(cl):
        for word in comment.lstrip("#").split():
            if token := COMMENT_TOKEN.fullmatch(word):
                yield pair(*token.groups(""))


def dispatch_pairs(text: str) -> Iterator[Pair]:
    for line in text.splitlines():
        if declared := DISPATCH_LINE.fullmatch(line.strip()):
            yield from (pair(*found.groups("")) for found in DISPATCH_PAIR.finditer(declared.group(1)))


def env_pairs() -> Iterator[Pair]:
    if (reqenv.getenv(RAW_ENV) or "").strip().lower() in TRUTHY:
        yield "raw", None


def dispatch_text(evt: BaseHookEvent) -> str:
    match evt.input:
        case TaskCall(prompt=prompt):
            return prompt or ""
        case SkillCall(args=args):
            return args or ""
    return ""


def event_annotations(evt: BaseHookEvent) -> Annotations:
    return dict(chain(env_pairs(), comment_pairs(evt.cmd.line), dispatch_pairs(dispatch_text(evt))))


def dispatch_prompt(evt: BaseHookEvent) -> str:
    t = evt.ctx.t
    if isinstance(t, RemoteSession):
        return next(iter(t.prompts(selection="first", count=1)), "")
    return next((turn.prompt for turn in t.turns if turn.prompt), "")


def session_annotations(evt: BaseHookEvent) -> Annotations:
    """The annotations the session's dispatch prompt — its first prompt, a lane's brief — declares."""
    return dict(dispatch_pairs(dispatch_prompt(evt)))


class Annotated(CustomCondition):
    """Matches when the event carries the ``ccx:`` annotation ``key``, set to ``value`` when one is given.

    Three carriers feed :attr:`~captain_hook.BaseHookEvent.annotations`: a ``ccx:<key>[=<value>]`` token in a
    real comment of a Bash command (``gt submit  # ccx:raw``), a whole ``ccx: <key>[=<value>] ...`` line in an
    Agent or Task prompt or in Skill args, and ``CAPT_HOOK_CCX_RAW=1`` for ``raw``. ``scope="session"`` also
    reads the session's dispatch prompt, so a lane briefed with ``ccx: role=fix`` carries the key on every call.

    Example:
        >>> hook(Event.PreToolUse, only_if=[Runs("gt", "submit")], skip_if=[Annotated("raw")], message="...")
        >>> Annotated("role", "evidence", scope="session")
    """

    def __init__(self, key: str, value: str | None = None, *, scope: Literal["event", "session"] = "event") -> None:
        self.key = key
        self.value = value
        self.scope = scope

    def matches(self, annotations: Annotations) -> bool:
        return self.key in annotations and (self.value is None or annotations[self.key] == self.value)

    def check(self, evt: BaseHookEvent) -> bool:
        return self.matches(evt.annotations) or (self.scope == "session" and self.matches(session_annotations(evt)))
