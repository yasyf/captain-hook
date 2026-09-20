from __future__ import annotations

import os
from collections import deque
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Literal

from captain_hook.cmd import Target, Targets
from captain_hook.types import TOOL_EVENTS, CustomCondition

if TYPE_CHECKING:
    from cc_transcript.command import Word

    from captain_hook.cmd import Call
    from captain_hook.events import BaseHookEvent

type Scalar = str | int | bool


@dataclass(frozen=True, slots=True)
class Option:
    """Bind option aliases to a named, typed value or a flag.

    ``prefix`` keeps positional binding open for leading options in a schema whose
    options start a trailing expression. ``until`` consumes an opaque argument list
    through its terminator, such as a nested command passed to another program.
    """

    name: str
    flags: tuple[str, ...]
    type: type[str] | type[int] | type[bool] = str
    prefix: bool = False
    until: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class Operand:
    """Bind positional words to a role, optionally collecting a variable-length group.

    A variadic operand reserves words for the fixed operands after it. Defaults are
    literal words used only when no explicit word binds the role.
    """

    name: str
    count: int | Literal["*"] = 1
    default: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Arguments:
    """Named bindings from one invocation, retaining source words and unknown values.

    ``complete`` is false when an unknown option, missing value, invalid typed value,
    or command substitution prevents a complete binding. Known path operands remain
    available so a policy can still reject an explicitly broad root.
    """

    words: dict[str, tuple[Word, ...]]
    values: dict[str, tuple[Scalar | None, ...]]
    cwd: Path | None
    complete: bool
    operands_complete: bool

    def paths(self, name: str) -> Targets:
        """Return the named role as path targets with the invocation's working directory."""
        return Targets(
            tuple(
                Target(value if isinstance(value, str) else None, word.raw, self.cwd)
                for word, value in zip(self.words.get(name, ()), self.values.get(name, ()), strict=True)
            ),
            complete=self.operands_complete,
        )


@dataclass(frozen=True, slots=True)
class CommandSchema:
    """Bind parsed shell words using declarative option and positional definitions.

    Schemas operate on an existing ``Call`` and never parse shell text. With
    ``options_end_operands``, the first non-prefix option starts a trailing expression;
    expression arguments never become positional operands. Unknown options end binding
    and mark the result incomplete instead of guessing their arity. A value attached to a
    short option (``-oVAL``) binds only when every single-dash alias is one letter, since a
    schema with find-style single-dash words cannot split such a token unambiguously.
    """

    program: str
    operands: tuple[Operand, ...] = ()
    options: tuple[Option, ...] = ()
    options_end_operands: bool = False
    separators: tuple[str, ...] = ()

    def bind(self, call: Call) -> Arguments:
        """Bind an invocation while preserving the parser's word provenance."""
        from cc_transcript.command import Word

        aliases = {flag: option for option in self.options for flag in option.flags}
        short_values = all(len(flag) == 2 or flag.startswith("--") for flag in aliases)
        words: dict[str, list[Word]] = {}
        values: dict[str, list[Scalar | None]] = {}
        positional: list[Word] = []
        accept_operands = True
        accept_options = True
        complete = not call.substituted
        operands_complete = not call.substituted
        stream = iter(call.command.words[1:])
        for word in stream:
            token = word.value
            flag, joined, attached = (token or "").partition("=")
            inline = attached if joined else None
            if (
                short_values
                and token is not None
                and len(token) > 2
                and (short := aliases.get(token[:2])) is not None
                and short.type is not bool
            ):
                flag, inline = token[:2], token[2:]
            if token == "--" and accept_options:
                accept_options = False
                continue
            if token in self.separators and accept_options:
                accept_operands = False
                continue
            if (
                accept_options
                and token is not None
                and token.startswith("-")
                and not token.startswith("--")
                and flag not in aliases
                and len(token) > 2
                and all((part := aliases.get(f"-{letter}")) is not None and part.type is bool for letter in token[1:])
            ):
                for letter in token[1:]:
                    option = aliases[f"-{letter}"]
                    values.setdefault(option.name, []).append(True)
                    if self.options_end_operands and not option.prefix:
                        accept_operands = False
                continue
            if accept_options and (option := aliases.get(flag)) is not None:
                if self.options_end_operands and not option.prefix:
                    accept_operands = False
                if option.until:
                    suffix: deque[str | None] = deque(maxlen=max(map(len, option.until)))
                    for argument in stream:
                        suffix.append(argument.value)
                        if any(tuple(suffix)[-len(end) :] == end for end in option.until):
                            break
                    else:
                        complete = False
                    continue
                if option.type is bool:
                    values.setdefault(option.name, []).append(True)
                    continue
                argument = word if inline is not None else next(stream, None)
                if argument is None:
                    complete = False
                    break
                words.setdefault(option.name, []).append(argument)
                value: Scalar | None = argument.value if inline is None else inline
                if option.type is int and value is not None:
                    try:
                        value = int(value)
                    except ValueError:
                        value = None
                values.setdefault(option.name, []).append(value)
                complete &= value is not None
            elif accept_options and token is not None and token.startswith("-") and token != "-":
                complete = False
                operands_complete &= not accept_operands or (
                    self.options_end_operands and bool(positional or any(o.name in words for o in self.operands))
                )
                break
            elif accept_operands:
                positional.append(word)
            elif token is None:
                complete = False
        for index, operand in enumerate(self.operands):
            reserved = sum(o.count for o in self.operands[index + 1 :] if isinstance(o.count, int))
            count = max(0, len(positional) - reserved) if operand.count == "*" else operand.count
            bound, positional = positional[:count], positional[count:]
            if not bound and operand.name not in words:
                bound = [Word(value, value, None, value.startswith("~")) for value in operand.default]
            words.setdefault(operand.name, []).extend(bound)
            values.setdefault(operand.name, []).extend(word.value for word in bound)
            complete &= operand.count == "*" or len(bound) == operand.count
        return Arguments(
            {name: tuple(bound) for name, bound in words.items()},
            {name: tuple(bound) for name, bound in values.items()},
            call.cwd,
            complete and not positional,
            operands_complete,
        )


@dataclass(frozen=True, slots=True)
class PathMatches:
    """Match complete path globs against lexical and resolved targets.

    ``*`` matches one path segment; ``**`` can cross directories. Patterns may use
    ``~`` for the current home. Unknown shell expansions match only when explicitly
    requested through ``unresolved``; no shell variables are evaluated or replaced.
    """

    patterns: tuple[str, ...]
    unresolved: bool = False

    def __call__(self, target: Target) -> bool:
        if target.value is None:
            return self.unresolved
        value = os.path.expanduser(target.value) if target.raw.startswith("~") else target.value
        path = Path(value)
        if not path.is_absolute():
            if target.cwd is None:
                return False
            path = target.cwd / path
        lexical = PurePath(os.path.normpath(path))
        return any(
            candidate.full_match(os.path.expanduser(pattern))
            for candidate in (lexical, path.resolve())
            for pattern in self.patterns
        )


@dataclass(frozen=True, slots=True)
class PathsMatch:
    """Match when any path in a named argument role satisfies a path predicate."""

    name: str
    predicate: PathMatches

    def __call__(self, arguments: Arguments) -> bool:
        targets = arguments.paths(self.name)
        return (not targets.complete and self.predicate.unresolved) or any(self.predicate(target) for target in targets)


@dataclass(frozen=True, slots=True)
class OptionIs:
    """Match a completely parsed invocation's last value for a named option."""

    name: str
    values: Collection[Scalar]

    def __call__(self, arguments: Arguments) -> bool:
        return arguments.complete and bool(values := arguments.values.get(self.name)) and values[-1] in self.values


@dataclass(frozen=True, slots=True)
class CommandMatches(CustomCondition):
    """Apply argument predicates to each matching invocation, including nested calls.

    Every ``only_if`` predicate must match the same invocation. Any ``skip_if``
    predicate exempts that invocation alone, so a safe sibling cannot hide a match.
    """

    valid_events = TOOL_EVENTS
    schema: CommandSchema
    only_if: tuple[Callable[[Arguments], bool], ...] = ()
    skip_if: tuple[Callable[[Arguments], bool], ...] = ()

    def check(self, evt: BaseHookEvent) -> bool:
        return any(
            all(predicate(arguments) for predicate in self.only_if)
            and not any(predicate(arguments) for predicate in self.skip_if)
            for call in evt.cmd.calls(self.schema.program)
            for arguments in (self.schema.bind(call),)
        )
