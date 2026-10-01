from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import product
from math import prod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ASSIGNMENT = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(\[[^\]]*\])?(\+?)=")
MUTATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):?=|\(\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:=|\+\+|--|[-+*/%]=)")
SPECIAL_PARAMETER = re.compile(r"[0-9@*#?$!_-]")
WORD_BREAK = frozenset(" \t\n;&|()<>")
OPERATORS = (
    "<<<",
    "<<-",
    "<<",
    ";;",
    "&&",
    "||",
    "|&",
    ">>",
    "&>",
    "<(",
    ">(",
    "((",
    "))",
    ";",
    "&",
    "|",
    "(",
    ")",
    "<",
    ">",
)
HEREDOC_OPERATORS = frozenset({"<<", "<<-"})
REDIRECT_OPERATORS = frozenset({"<<<", "<<-", "<<", ">>", "&>", "<", ">"})
GROUP_OPENERS = frozenset({"(", "<(", ">(", "((", "{"})
GROUP_CLOSERS = frozenset({")", "))", "}"})
SUBSHELL_OPERATORS = frozenset({"|", "|&", "&"})
STATEMENT_OPERATORS = frozenset({None, ";", ";;", "\n", "&"})
BODY_OPENERS = frozenset({"then", "else", "do"})
BODY_CLOSERS = frozenset({"fi", "done"})
CONDITION_OPENERS = frozenset({"if", "while", "until", "elif"})
PREFIXES = frozenset({"!", "time", "builtin", "command"})
DECLARERS = frozenset({"local", "declare", "typeset", "readonly", "let"})
READERS = frozenset({"read", "mapfile", "readarray", "getopts"})
UNREADABLE_BINDERS = frozenset({"eval", "source", ".", "case", "select", "coproc"})
UNREADABLE_IN_SUBSTITUTION = ("#", "<<", "case")
SHELL_OWNED = frozenset({"PWD", "OLDPWD", "RANDOM", "SECONDS", "LINENO", "REPLY", "IFS", "BASH_COMMAND", "PIPESTATUS"})
ENVIRONMENT = {"HOME": "~"}
CANDIDATE_LIMIT = 16
CANDIDATE_LENGTH = 4096


@dataclass(frozen=True, slots=True)
class Known:
    """A variable every assignment on the line set to a literal: its candidate values."""

    candidates: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Unknown:
    """A variable the line builds at run time; ``source`` is the text feeding it, None when unreadable."""

    source: str | None


type Binding = Known | Unknown


@dataclass(frozen=True, slots=True)
class Resolved:
    """A word whose expansions all resolve, one candidate text per combination of values.

    ``splittable`` is True when an unquoted expansion contributed, so the shell may split a
    candidate into several words.
    """

    candidates: tuple[str, ...]
    splittable: bool


@dataclass(frozen=True, slots=True)
class Unresolved:
    """A word the guard cannot resolve; ``source`` is the text its value is built from, None when unreadable."""

    source: str | None


type Resolution = Resolved | Unresolved


@dataclass(frozen=True, slots=True)
class Ref:
    name: str
    quoted: bool


@dataclass(frozen=True, slots=True)
class Literal:
    text: str
    quoted: bool


@dataclass(frozen=True, slots=True)
class Token:
    raw: str
    start: int
    end: int
    operator: bool


@dataclass(frozen=True, slots=True)
class Event:
    offset: int
    name: str
    binding: Binding
    until: int | None = None


def source_of(binding: Binding) -> str | None:
    match binding:
        case Known(candidates):
            return " ".join(candidates)
        case Unknown(source):
            return source


def joined(first: Binding, second: Binding) -> Binding:
    match first, second:
        case Known(), Known():
            return second
        case _:
            sources = (source_of(first), source_of(second))
            return Unknown(None if None in sources else " ".join(part for part in sources if part))


def skip_quoted(text: str, index: int, quote: str) -> int:
    end = text.find(quote, index + 1)
    return len(text) if end < 0 else end + 1


def skip_group(text: str, index: int, opener: str, closer: str) -> int:
    depth = 0
    while index < len(text):
        match text[index]:
            case "\\":
                index += 2
                continue
            case "'" | "`" as quote:
                index = skip_quoted(text, index, quote)
                continue
            case '"':
                index = skip_double(text, index)
                continue
            case char if char == opener:
                depth += 1
            case char if char == closer:
                depth -= 1
                if depth == 0:
                    return index + 1
        index += 1
    return index


def skip_double(text: str, index: int) -> int:
    index += 1
    while index < len(text):
        match text[index]:
            case "\\":
                index += 2
                continue
            case '"':
                return index + 1
            case "`":
                index = skip_quoted(text, index, "`")
                continue
            case "$" if text.startswith("$(", index):
                index = skip_group(text, index + 1, "(", ")")
                continue
            case "$" if text.startswith("${", index):
                index = skip_group(text, index + 1, "{", "}")
                continue
        index += 1
    return index


def read_word(text: str, index: int) -> int:
    while index < len(text):
        match text[index]:
            case "\\":
                index += 2
            case "'" | "`" as quote:
                index = skip_quoted(text, index, quote)
            case '"':
                index = skip_double(text, index)
            case "$" if text.startswith("$(", index):
                index = skip_group(text, index + 1, "(", ")")
            case "$" if text.startswith("${", index):
                index = skip_group(text, index + 1, "{", "}")
            case char if char in WORD_BREAK:
                return index
            case _:
                index += 1
    return index


def dequote(raw: str) -> str:
    return "".join(
        part.text for part in segments(raw.replace("\\\n", "")) or [Literal(raw, False)] if isinstance(part, Literal)
    )


def segments(raw: str) -> list[Literal | Ref] | None:
    parts: list[Literal | Ref] = []
    index = 0
    quoted = False
    raw = raw.replace("\\\n", "")
    while index < len(raw):
        char = raw[index]
        if char == "\\" and index + 1 < len(raw) and (not quoted or raw[index + 1] in '\\"$`'):
            parts.append(Literal(raw[index + 1], quoted))
            index += 2
        elif char == "'" and not quoted:
            end = skip_quoted(raw, index, "'")
            parts.append(Literal(raw[index + 1 : end - 1], True))
            index = end
        elif char == '"':
            quoted = not quoted
            index += 1
        elif char == "`" or raw.startswith("$(", index):
            return None
        elif char == "$":
            if (name := NAME.match(raw, index + 1)) is not None:
                parts.append(Ref(name.group(), quoted))
                index = name.end()
            elif (special := SPECIAL_PARAMETER.match(raw, index + 1)) is not None:
                parts.append(Ref(special.group(), quoted))
                index = special.end()
            elif (
                raw.startswith("${", index)
                and (name := NAME.match(raw, index + 2)) is not None
                and raw.startswith("}", name.end())
            ):
                parts.append(Ref(name.group(), quoted))
                index = name.end() + 1
            else:
                return None
        else:
            parts.append(Literal(char, quoted))
            index += 1
    return parts


def skip_heredoc(text: str, index: int, terminator: str, *, strip_tabs: bool) -> int:
    while index < len(text):
        line_end = text.find("\n", index)
        line_end = len(text) if line_end < 0 else line_end
        line = text[index:line_end]
        index = line_end + 1
        if (line.lstrip("\t") if strip_tabs else line) == terminator:
            return index
    return index


def tokens(text: str) -> Iterator[Token]:
    index = 0
    heredocs: list[tuple[str, bool]] = []
    word_start = True
    while index < len(text):
        char = text[index]
        if char == "\n":
            yield Token("\n", index, index + 1, True)
            index += 1
            while heredocs:
                terminator, strip_tabs = heredocs.pop(0)
                index = skip_heredoc(text, index, terminator, strip_tabs=strip_tabs)
            word_start = True
        elif char in " \t":
            index += 1
            word_start = True
        elif text.startswith("\\\n", index):
            index += 2
        elif char == "#" and word_start:
            line_end = text.find("\n", index)
            index = len(text) if line_end < 0 else line_end
        elif (operator := next((op for op in OPERATORS if text.startswith(op, index)), None)) is not None:
            end = index + len(operator)
            if operator in HEREDOC_OPERATORS:
                while end < len(text) and text[end] in " \t":
                    end += 1
                word_end = read_word(text, end)
                heredocs.append((dequote(text[end:word_end]), operator == "<<-"))
                end = word_end
            yield Token(operator, index, end, True)
            index = end
            word_start = True
        else:
            end = read_word(text, index)
            if end == index:
                index += 1
                continue
            yield Token(text[index:end], index, end, False)
            index = end
            word_start = False


def unreadable_substitution(value: str) -> bool:
    return "$(" in value and any(marker in value for marker in UNREADABLE_IN_SUBSTITUTION)


@dataclass
class Scope:
    """Same-line variable bindings of one shell text, plus the function bodies it defines.

    Walks the text once, recording every assignment, ``export``, ``for`` head, and reader as
    an event at its offset. A binding is :class:`Known` only when every assignment to the
    name was a literal in an unconditional top-level statement; anything conditional, built
    at run time, read from input, or owned by the shell leaves it :class:`Unknown`.
    ``parent`` holds the bindings in force when a nested payload began. A word written inside
    a function body never resolves, since the body runs later than it reads.
    """

    text: str
    parent: Mapping[str, Binding] = field(default_factory=dict)
    exported: set[str] = field(default_factory=set)
    events: list[Event] = field(default_factory=list, init=False)
    functions: list[tuple[int, int]] = field(default_factory=list, init=False)
    unreadable_from: int | None = field(default=None, init=False)
    mentioned: frozenset[str] = field(default=frozenset(), init=False)
    mutated: frozenset[str] = field(default=frozenset(), init=False)

    def __post_init__(self) -> None:
        self.mentioned = frozenset(match.group(1) for match in ASSIGNMENT.finditer(self.text))
        self.mutated = frozenset(name for match in MUTATION.finditer(self.text) for name in match.groups() if name)
        if "IFS" in self.mentioned:
            self.unreadable_from = 0
        Walk(self).run()

    @classmethod
    def unreadable(cls, text: str) -> Scope:
        scope = cls.__new__(cls)
        scope.text, scope.parent, scope.exported = text, {}, set()
        scope.events, scope.functions, scope.unreadable_from = [], [], 0
        scope.mentioned, scope.mutated = frozenset(), frozenset()
        return scope

    def mark_unreadable(self, offset: int) -> None:
        self.unreadable_from = offset if self.unreadable_from is None else min(self.unreadable_from, offset)

    def child(
        self, text: str, offset: int, *, exported_only: bool, overlay: Mapping[str, Binding] | None = None
    ) -> Scope:
        """The scope of a payload written at ``offset``, seeded with the bindings in force there."""
        names = {event.name for event in self.events} | set(self.parent) | self.exported
        inherited = {
            name: binding
            for name in names
            if (binding := self.lookup(name, offset)) is not None and (not exported_only or name in self.exported)
        }
        return Scope(text, inherited | dict(overlay or {}), set(self.exported))

    def lookup(self, name: str, offset: int) -> Binding | None:
        unreadable = self.unreadable_from is not None and offset > self.unreadable_from
        if unreadable or name in SHELL_OWNED or name in self.mutated:
            return Unknown(None)
        binding = self.parent.get(name)
        for event in self.events:
            if event.offset < offset and event.name == name and (event.until is None or offset < event.until):
                binding = event.binding if binding is None else joined(binding, event.binding)
        if binding is None and name in self.mentioned:
            return Unknown(None)
        return binding

    def in_function(self, offset: int) -> bool:
        return any(start <= offset < end for start, end in self.functions)

    def resolve(self, raw: str, offset: int) -> Resolution:
        """Resolve one word written at ``offset`` through the bindings in force there."""
        if self.in_function(offset):
            return Unresolved(None)
        parts = segments(raw)
        if parts is None:
            return Unresolved(raw)
        refs = {part.name: part for part in parts if isinstance(part, Ref)}
        if not refs:
            return Resolved((dequote(raw),), False)
        choices: dict[str, tuple[str, ...]] = {}
        for name in refs:
            match self.lookup(name, offset):
                case Known(candidates):
                    choices[name] = candidates
                case Unknown(source):
                    return Unresolved(source)
                case None if name in ENVIRONMENT:
                    choices[name] = (ENVIRONMENT[name],)
                case None if NAME.fullmatch(name) is not None:
                    return Unresolved("")
                case None:
                    return Unresolved(None)
        if prod(map(len, choices.values())) > CANDIDATE_LIMIT:
            return Unresolved(" ".join(" ".join(choice) for choice in choices.values()))
        candidates = tuple(
            "".join(values[part.name] if isinstance(part, Ref) else part.text for part in parts)
            for combination in product(*choices.values())
            for values in (dict(zip(choices, combination, strict=True)),)
        )
        if any(len(candidate) > CANDIDATE_LENGTH for candidate in candidates):
            return Unresolved(None)
        splittable = any(not ref.quoted for ref in refs.values())
        mixed = splittable and any(part.quoted for part in parts)
        if mixed and any(candidate.split() != [candidate] for candidate in candidates):
            return Unresolved(None)
        return Resolved(candidates, splittable)


def head_of(words: list[Token]) -> tuple[str, list[Token]]:
    while words and dequote(words[0].raw) in PREFIXES:
        words = words[1:]
    return (dequote(words[0].raw), words[1:]) if words else ("", [])


@dataclass
class Walk:
    scope: Scope
    stack: list[tuple[str, int, int | None]] = field(default_factory=list)
    words: list[Token] = field(default_factory=list)
    previous: str | None = None
    function_name: Token | None = None
    pending_function: bool = False
    pending_loop: int | None = None
    skip_word: bool = False

    def run(self) -> None:
        for token in tokens(self.scope.text):
            if not token.operator and token.raw in {"{", "}"} and (not self.words or self.words[0].raw == "function"):
                self.operator(token)
            elif not token.operator:
                if unreadable_substitution(token.raw):
                    self.scope.mark_unreadable(token.start)
                if not self.skip_word:
                    self.words.append(token)
                self.skip_word = False
            else:
                self.operator(token)
        self.flush(sure=self.sure())

    def sure(self) -> bool:
        return not self.stack and self.previous in STATEMENT_OPERATORS

    def operator(self, token: Token) -> None:
        self.skip_word = False
        match token.raw:
            case "(" if len(self.words) == 1 and NAME.fullmatch(self.words[0].raw) is not None and not self.stack:
                self.function_name = self.words[0]
                self.words = []
                return
            case ")" if self.function_name is not None and not self.words:
                self.function_name = None
                self.pending_function = True
                return
            case "(" | "<(" if self.words and self.words[-1].raw.endswith("="):
                self.bind_unreadable(self.words.pop())
            case _ if token.raw in REDIRECT_OPERATORS:
                self.skip_word = True
                return
            case _:
                self.function_name = None
        self.flush(sure=self.sure() and token.raw not in SUBSHELL_OPERATORS)
        if token.raw in GROUP_OPENERS:
            self.stack.append(("function" if self.pending_function else "group", token.start, None))
            self.pending_function = False
        elif token.raw in GROUP_CLOSERS and self.stack:
            kind, start, _ = self.stack.pop()
            if kind == "function" and token.start - start <= 2:
                self.pending_function = True
            elif kind == "function":
                self.scope.functions.append((start, token.end))
        self.previous = token.raw

    def bind_unreadable(self, word: Token) -> None:
        if (match := ASSIGNMENT.match(word.raw)) is not None:
            self.scope.events.append(Event(word.start, match.group(1), Unknown(None)))

    def flush(self, *, sure: bool) -> None:
        words, self.words = self.words, []
        if not words:
            return
        head, rest = head_of(words)
        if head in BODY_OPENERS:
            if head == "else" and self.stack and self.stack[-1][0] == "then":
                self.stack.pop()
            self.stack.append(("then" if head != "do" else "do", words[0].start, self.pending_loop))
            self.pending_loop = None
            self.words = rest
            self.flush(sure=False)
        elif head in BODY_CLOSERS:
            if self.stack and self.stack[-1][0] == ("then" if head == "fi" else "do"):
                _, _, loop = self.stack.pop()
                if loop is not None:
                    self.close_loop(loop, words[0].end)
            self.words = rest
            self.flush(sure=False)
        elif head in CONDITION_OPENERS:
            if head == "elif" and self.stack and self.stack[-1][0] == "then":
                self.stack.pop()
            self.words = rest
            self.flush(sure=False)
        elif head == "for":
            self.bind_loop(rest)
        elif head in UNREADABLE_BINDERS:
            self.scope.mark_unreadable(words[-1].end)
        elif head == "function":
            self.pending_function = True
        elif head in DECLARERS and any(word.raw.startswith("-") and "n" in word.raw for word in rest):
            self.scope.mark_unreadable(words[-1].end)
        elif head in READERS or head == "unset" or head in DECLARERS:
            self.bind_names(head, rest)
        elif head == "printf" and any(word.raw.startswith("-v") for word in rest):
            self.bind_names(head, printf_destination(rest))
        elif head == "export":
            self.bind_exports(rest, sure)
        elif all(ASSIGNMENT.match(word.raw) is not None for word in words):
            for word in words:
                self.bind_assignment(word, sure=sure)

    def bind_names(self, head: str, words: list[Token]) -> None:
        names = [
            match.group()
            for word in words
            if not word.raw.startswith("-") and (match := NAME.search(dequote(word.raw))) is not None
        ]
        if head == "read" and not names:
            names = ["REPLY"]
        self.scope.events.extend(Event(words[0].start if words else 0, name, Unknown(None)) for name in names)

    def bind_exports(self, words: list[Token], sure: bool) -> None:
        options = any(word.raw.startswith("-") for word in words)
        for word in words:
            if word.raw.startswith("-"):
                continue
            if options:
                self.bind_unreadable(Token(f"{dequote(word.raw)}=", word.start, word.end, False))
            elif ASSIGNMENT.match(word.raw) is not None:
                self.bind_assignment(word, sure=sure)
                self.scope.exported.add(ASSIGNMENT.match(word.raw).group(1))
            elif NAME.fullmatch(word.raw) is not None:
                self.scope.exported.add(word.raw)

    def bind_assignment(self, word: Token, *, sure: bool) -> None:
        match = ASSIGNMENT.match(word.raw)
        if match is None:
            return
        name, value = match.group(1), word.raw[match.end() :]
        binding: Binding
        match self.scope.resolve(value, word.start):
            case _ if match.group(3):
                binding = Unknown(None)
            case Resolved(candidates, _) if sure and not match.group(2):
                binding = Known(candidates)
            case Resolved(candidates, _):
                binding = Unknown(" ".join(candidates))
            case Unresolved(source):
                binding = Unknown(source)
        self.scope.events.append(Event(word.start, name, binding))

    def bind_loop(self, words: list[Token]) -> None:
        match [word.raw for word in words]:
            case [name, "in", *_] if NAME.fullmatch(name) is not None:
                candidates: list[str] = []
                for item in words[2:]:
                    match self.scope.resolve(item.raw, item.start):
                        case Resolved(values, splittable) if not any("{" in value for value in values):
                            candidates.extend(
                                piece for value in values for piece in (value.split() if splittable else [value])
                            )
                        case _:
                            candidates = []
                            break
                binding: Binding = (
                    Known(tuple(candidates))
                    if candidates
                    else Unknown(" ".join(word.raw for word in words[2:]) or None)
                )
                self.pending_loop = len(self.scope.events)
                self.scope.events.append(Event(words[0].start, name, binding))
            case [name, *_] if NAME.fullmatch(name) is not None:
                self.scope.events.append(Event(words[0].start, name, Unknown(None)))
            case _:
                pass

    def close_loop(self, index: int, end: int) -> None:
        event = self.scope.events[index]
        self.scope.events[index] = Event(event.offset, event.name, event.binding, end)
        self.scope.events.append(Event(end, event.name, Unknown(source_of(event.binding))))


def printf_destination(words: list[Token]) -> list[Token]:
    for index, word in enumerate(words):
        if word.raw == "-v" and index + 1 < len(words):
            return [words[index + 1]]
        if word.raw.startswith("-v") and len(word.raw) > 2:
            return [Token(word.raw[2:], word.start + 2, word.end, False)]
    return []
