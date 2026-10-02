"""The ``ccx:`` annotation notation: one escape and intent grammar for every hook to read."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from itertools import chain
from typing import TYPE_CHECKING, Literal

from cc_transcript.command import PAYLOAD_DEPTH_LIMIT
from cc_transcript.tools import SkillCall, TaskCall

from captain_hook.cmd import Cmd
from captain_hook.snapshots.client import RemoteSession
from captain_hook.types import CustomCondition
from captain_hook.util import reqenv

if TYPE_CHECKING:
    from captain_hook.cmd import Call
    from captain_hook.events import BaseHookEvent

WORD = r"[a-z0-9][a-z0-9:._-]*"
COMMENT_TOKEN = re.compile(rf"ccx:({WORD})(?:=({WORD}))?")
DISPATCH_LINE = re.compile(rf"ccx:((?:[ \t]+{WORD}(?:={WORD})?)+)")
DISPATCH_PAIR = re.compile(rf"({WORD})(?:=({WORD}))?")
RAW_ENV = "CAPT_HOOK_CCX_RAW"
TRUTHY = frozenset({"1", "true", "yes"})
BREAKS = frozenset(" \t\n;&|()<>")
SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
OPTION_VALUES = frozenset({"-o", "+o", "-O", "+O"})
FENCE = re.compile(r"(`{3,}|~{3,})")

type Annotations = Mapping[str, str | None]
type Pair = tuple[str, str | None]


def pair(key: str, value: str) -> Pair:
    return key, value or None


@dataclass
class CommentScanner:
    """Bash's own reading of where comments sit: quoting, line continuations, and every pending heredoc body."""

    text: str
    at: int = 0
    found: list[str] = field(default_factory=list)
    heredocs: list[tuple[str, bool]] = field(default_factory=list)

    def peek(self, offset: int = 0) -> str:
        return self.text[self.at + offset : self.at + offset + 1]

    def scan(self, close: str = "") -> None:
        word_start, depth = True, 0
        while self.at < len(self.text):
            char = self.peek()
            if char == close and depth == 0:
                self.at += 1
                return
            match char:
                case "\\" if self.peek(1) == "\n":
                    self.at += 2
                case "\\":
                    self.at += 2
                    word_start = False
                case "'":
                    self.skip_to("'")
                    word_start = False
                case '"':
                    self.skip_double()
                    word_start = False
                case "#" if word_start:
                    end = self.text.find("\n", self.at)
                    self.found.append(self.text[self.at : end if end >= 0 else None])
                    self.at = end if end >= 0 else len(self.text)
                case "\n":
                    self.at += 1
                    self.read_heredocs()
                    word_start = True
                case "$":
                    self.expansion()
                    word_start = False
                case "`":
                    self.at += 1
                    self.scan("`")
                    word_start = False
                case "<" if self.text.startswith("<<", self.at) and not self.text.startswith("<<<", self.at):
                    self.heredoc()
                    word_start = False
                case _:
                    depth += (char == "(") - (char == ")") if close == ")" else 0
                    self.at += 1
                    word_start = char in BREAKS

    def skip_to(self, quote: str) -> None:
        end = self.text.find(quote, self.at + 1)
        self.at = end + 1 if end >= 0 else len(self.text)

    def skip_double(self) -> None:
        self.at += 1
        while self.at < len(self.text):
            match self.peek():
                case "\\":
                    self.at += 2
                case '"':
                    self.at += 1
                    return
                case "$":
                    self.expansion()
                case "`":
                    self.at += 1
                    self.scan("`")
                case _:
                    self.at += 1

    def expansion(self) -> None:
        match self.peek(1), self.peek(2):
            case "(", "(":
                self.skip_balanced("(", ")")
            case "(", _:
                self.at += 2
                self.scan(")")
            case "{", _:
                self.skip_balanced("{", "}")
            case "'", _:
                self.at += 1
                self.skip_ansi()
            case _:
                self.at += 1

    def skip_balanced(self, opener: str, closer: str) -> None:
        self.at += 1
        depth = 0
        while self.at < len(self.text):
            char = self.peek()
            self.at += 1
            depth += (char == opener) - (char == closer)
            if depth == 0:
                return

    def skip_ansi(self) -> None:
        self.at += 1
        while self.at < len(self.text):
            match self.peek():
                case "\\":
                    self.at += 2
                case "'":
                    self.at += 1
                    return
                case _:
                    self.at += 1

    def heredoc(self) -> None:
        self.at += 2
        strip = self.peek() == "-"
        self.at += strip
        while self.peek() in (" ", "\t"):
            self.at += 1
        delimiter: list[str] = []
        while (char := self.peek()) and char not in BREAKS:
            match char:
                case "'" | '"':
                    end = self.text.find(char, self.at + 1)
                    delimiter.append(self.text[self.at + 1 : end if end >= 0 else None])
                    self.at = end + 1 if end >= 0 else len(self.text)
                case "\\":
                    delimiter.append(self.peek(1))
                    self.at += 2
                case _:
                    delimiter.append(char)
                    self.at += 1
        self.heredocs.append(("".join(delimiter), strip))

    def read_heredocs(self) -> None:
        for delimiter, strip in self.heredocs:
            while self.at < len(self.text):
                end = self.text.find("\n", self.at)
                line = self.text[self.at : end if end >= 0 else None]
                self.at = end + 1 if end >= 0 else len(self.text)
                if (line.lstrip("\t") if strip else line) == delimiter:
                    break
        self.heredocs.clear()


def payload(call: Call) -> str | None:
    """The shell text ``call`` runs as code: an ``eval``'s joined arguments, or a shell's ``-c`` command string."""
    if call.name == "eval":
        return " ".join(call.args) or None
    if call.name not in SHELLS:
        return None
    args, inline = iter(call.args), False
    for arg in args:
        if arg in OPTION_VALUES:
            next(args, None)
        elif arg.startswith(("-", "+")) and not arg.startswith("--"):
            inline = inline or (arg.startswith("-") and "c" in arg[1:])
        elif not arg.startswith("--"):
            return arg if inline else None
    return None


def shell_comments(text: str, depth: int = 0) -> Iterator[str]:
    """Every real shell comment in ``text``, the code of nested ``sh -c`` and ``eval`` payloads included."""
    (scanner := CommentScanner(text)).scan()
    yield from scanner.found
    if depth >= PAYLOAD_DEPTH_LIMIT or (cmd := Cmd.parse(text)) is None:
        return
    for call in cmd.calls():
        if call.occurrence.host is None and (code := payload(call)) is not None:
            yield from shell_comments(code, depth + 1)


def comment_pairs(text: str) -> Iterator[Pair]:
    for comment in shell_comments(text):
        for word in comment.lstrip("#").split():
            if token := COMMENT_TOKEN.fullmatch(word):
                yield pair(*token.groups(""))


def header_lines(text: str) -> Iterator[str]:
    """The lines of a dispatch prompt that speak for the prompt itself: none inside a fence or pasted content."""
    fence, pasted = "", False
    for line in map(str.strip, text.splitlines()):
        if fence:
            fence = "" if line.startswith(fence) else fence
        elif pasted:
            pasted = "</pasted_content>" not in line
        elif opened := FENCE.match(line):
            fence = opened.group(1)
        elif line.startswith("<pasted_content"):
            pasted = "</pasted_content>" not in line
        else:
            yield line


def dispatch_pairs(text: str) -> Iterator[Pair]:
    for line in header_lines(text):
        if declared := DISPATCH_LINE.fullmatch(line):
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
    return dict(chain(env_pairs(), comment_pairs(evt.cmd.raw), dispatch_pairs(dispatch_text(evt))))


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
    Agent or Task prompt or in Skill args, and ``CAPT_HOOK_CCX_RAW=1`` for ``raw``. ``scope="session"`` reads
    only the session's dispatch prompt instead, so a lane briefed with ``ccx: role=fix`` matches on every call and
    a command can never claim the role for itself.

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
        return self.matches(session_annotations(evt) if self.scope == "session" else evt.annotations)
