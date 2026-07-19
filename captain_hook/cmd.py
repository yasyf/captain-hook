from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING

from cc_transcript.literals import LITERALS

from captain_hook.util.globbing import GLOB_LIMIT, glob_matches
from captain_hook.util.paths import resolve_target
from captain_hook.util.scratch import is_scratch_path
from captain_hook.util.shell import (
    NESTED_COMMAND_DEPTH,
    nested_command_string,
    resolve_cd,
    safe_parse_command_line,
)
from captain_hook.util.vcs import contains_repo, in_vcs_repo, is_repo_root

if TYPE_CHECKING:
    from collections.abc import Iterator

    from cc_transcript.command import Command, CommandLine, Occurrence, Redirect, Word

    from captain_hook.events import ToolRewriteEvent
    from captain_hook.types import HookResult

WRAPPER_COMMANDS: frozenset[str] = frozenset(LITERALS["command.WRAPPER_COMMANDS"])
COMMAND_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "git": frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}),
}


def basename(executable: str) -> str:
    return Path(executable).name.casefold()


def emit_raw(raw: str) -> str:
    return f"./{raw}" if raw.startswith("-") else raw


@dataclass(frozen=True, slots=True)
class Expansion:
    """The paths a target's glob resolves to, plus whether the walk budget was exhausted.

    ``matches`` is capped at ``limit + 1`` entries, so ``len(expansion) > limit`` detects an
    over-cap glob without materializing every match; ``exhausted`` is True when a recursive
    walk blew its 20k-entry budget before completing (the pattern is too broad to verify).

    Example:
        >>> if len(call.targets.expand()) > 10:
        ...     return call.sub("rm", "trash")
    """

    matches: tuple[str, ...]
    exhausted: bool

    def __iter__(self) -> Iterator[str]:
        return iter(self.matches)

    def __len__(self) -> int:
        return len(self.matches)

    def __bool__(self) -> bool:
        return bool(self.matches)


@dataclass(frozen=True)
class Target:
    """One operand of a command — a literal path or a glob — with resolution and blast-radius predicates.

    ``path`` resolves the parent directory but keeps the final component **literal**: a symlink
    is classified as itself, never followed through, which is what a deletion or command target
    needs (act on the entity named, not what it points at). This is deliberately unlike the
    ``ScratchPath`` condition, which *fully* resolves — write-approval must see through a symlink
    to where the bytes actually land. Do not unify the two.

    Every predicate returns False when ``path`` is None (a relative target with no known cwd).

    Example:
        >>> target.is_repo_root or target.contains_repo
    """

    text: str
    raw: str
    cwd: Path | None

    @cached_property
    def path(self) -> Path | None:
        """The target resolved against ``cwd``, parent-resolved with the final component kept literal."""
        return resolve_target(self.text, self.cwd)

    @property
    def has_glob(self) -> bool:
        """Whether the target's text contains shell glob metacharacters."""
        return glob.has_magic(self.text)

    def expand(self, *, limit: int = GLOB_LIMIT) -> Expansion:
        """Expand a glob target to its matches (capped at ``limit + 1``); a literal yields itself."""
        if not self.has_glob:
            return Expansion((self.text,), False)
        matches, too_broad = glob_matches(self.text, self.cwd, limit=limit)
        return Expansion(matches, too_broad)

    @property
    def is_scratch(self) -> bool:
        """Whether the target sits under a temp root or a scratch-named ancestor directory."""
        return (path := self.path) is not None and is_scratch_path(path)

    @property
    def is_repo_root(self) -> bool:
        """Whether the target is itself a git/jj repository root."""
        return (path := self.path) is not None and is_repo_root(path)

    @property
    def in_repo(self) -> bool:
        """Whether the target lives inside a git/jj repository."""
        return (path := self.path) is not None and in_vcs_repo(path)

    @property
    def is_fs_root(self) -> bool:
        """Whether the target is the filesystem root ``/``."""
        return (path := self._normalized) is not None and path == Path("/")

    @property
    def is_home(self) -> bool:
        """Whether the target is a home directory (``~`` or a top-level ``/Users`` entry)."""
        if (path := self._normalized) is None:
            return False
        return path == Path.home() or Path("/Users") in (path, path.parent)

    @property
    def contains_repo(self) -> bool:
        """Whether the target is a directory that contains a git/jj repository."""
        return (path := self._normalized) is not None and path.is_dir(follow_symlinks=False) and contains_repo(path)

    @cached_property
    def _normalized(self) -> Path | None:
        return None if (path := self.path) is None else Path(os.path.normpath(path))


@dataclass(frozen=True, slots=True)
class Targets:
    """The operand targets of a command, in order — iterable, sized, and expandable as a whole."""

    targets: tuple[Target, ...] = ()

    def __iter__(self) -> Iterator[Target]:
        return iter(self.targets)

    def __len__(self) -> int:
        return len(self.targets)

    def __bool__(self) -> bool:
        return bool(self.targets)

    def expand(self, *, limit: int = GLOB_LIMIT) -> Expansion:
        """Every target's expansion concatenated: literals contribute themselves, globs their matches."""
        parts = [target.expand(limit=limit) for target in self.targets]
        return Expansion(
            tuple(match for part in parts for match in part.matches),
            any(part.exhausted for part in parts),
        )


@dataclass(frozen=True)
class Call:
    """One command invocation reached by walking a command line — a top-level occurrence or a
    nested payload inside a ``sh -c``/``eval`` cluster.

    ``command`` is the cc-transcript ``Command`` with leading wrappers (``sudo``, ``env``,
    ``timeout``, …) stripped; ``name`` is its executable basename, casefolded, so matching is
    command-position-only by construction (``echo rm foo`` and ``git rm`` never match ``"rm"``).
    A nested call carries ``occurrence=None`` and is never ``spliceable``.

    Example:
        >>> for call in evt.cmd.calls("rm"):
        ...     if call.targets.expand().exhausted:
        ...         return evt.block("glob too broad to verify")
        ...     return call.sub("rm", "trash", args=call.targets) or evt.block("unrecoverable rm")
    """

    cmd: Cmd
    command: Command
    source: Command
    occurrence: Occurrence | None
    cwd: Path | None
    nested: bool

    @property
    def name(self) -> str:
        """The unwrapped executable's basename, casefolded."""
        return basename(self.command.executable)

    @property
    def wrappers(self) -> tuple[str, ...]:
        """The leading wrapper commands stripped to reach this call, e.g. ``("sudo",)``.

        Identified by basename against the authoritative ``WRAPPER_COMMANDS`` table. A wrapper
        value-flag argument that itself names a wrapper (``sudo -u env rm``) can appear here, but
        a membership check against a real wrapper head is unaffected.
        """
        stripped = self.source.argv[: len(self.source.argv) - len(self.command.argv)]
        return tuple(name for token in stripped if (name := basename(token)) in WRAPPER_COMMANDS)

    @property
    def args(self) -> tuple[str, ...]:
        """The unwrapped command's arguments, with shell quoting removed."""
        return self.command.args

    @property
    def redirects(self) -> tuple[Redirect, ...]:
        """The command's file redirects."""
        return self.command.redirects

    @property
    def spliceable(self) -> bool:
        """Whether a rewrite can be spliced back — a top-level occurrence with a byte span and no
        backslash-newline continuation. Nested calls and span-less occurrences are never spliceable."""
        return (
            not self.nested
            and self.occurrence is not None
            and self.source.span is not None
            and "\\\n" not in self.source.raw
        )

    @property
    def flags(self) -> tuple[str, ...]:
        """The option tokens, with any registered value-flag's argument (``git -C <dir>``) alongside it."""
        return self._split[0]

    @property
    def targets(self) -> Targets:
        """The operand tokens as :class:`Target` objects, value-flag arguments and options removed."""
        return self._split[1]

    @cached_property
    def _split(self) -> tuple[tuple[str, ...], Targets]:
        value_flags = COMMAND_VALUE_FLAGS.get(self.name, frozenset())
        arg_words = self.command.words[1:]
        flags: list[str] = []
        operands: list[Word] = []
        i = 0
        while i < len(arg_words):
            match arg_words[i].text:
                case "--":
                    operands.extend(arg_words[i + 1 :])
                    break
                case "-":
                    operands.append(arg_words[i])
                case flag if flag.startswith("-"):
                    flags.append(flag)
                    if "=" not in flag and flag in value_flags and i + 1 < len(arg_words):
                        flags.append(arg_words[i + 1].text)
                        i += 1
                case _:
                    operands.append(arg_words[i])
            i += 1
        return tuple(flags), Targets(tuple(Target(word.text, word.raw, self.cwd) for word in operands))

    def sub(self, old: str, new: str, *, args: Targets | None = None, note: str | None = None) -> HookResult | None:
        """Rewrite this call's ``old`` executable to ``new`` in place, returning the rewrite result.

        ``old`` must equal :attr:`name` (else :class:`ValueError` — a programming error, not a runtime
        condition). Returns None when the call is not :attr:`spliceable`, so a handler composes the
        fail-closed fallback in policy code: ``call.sub("rm", trash) or evt.block(...)``.

        The occurrence's span is replaced with ``new`` followed by the re-emitted arguments — each
        argument's verbatim source spelling (``Word.raw``), with a ``./`` prefix added to a
        ``-``-leading raw so ``new`` never misparses it as a flag. ``args`` defaults to the call's
        own arguments; pass ``args=call.targets`` to drop flags and keep only operands. Leading
        wrappers drop (``sudo rm /x`` → ``trash /x``); redirects outside the span survive.

        Subs accumulate on the parent :class:`Cmd`: each call returns a fresh rewrite splicing every
        sub so far, so returning the last applies them all (``rm a && rm b`` rewrites both) and
        returning ``evt.block(...)`` instead discards them all — a line never rewrites partially.
        A detached ``Cmd`` (no bound event) raises :class:`RuntimeError`.
        """
        if old != self.name:
            raise ValueError(f"sub(old={old!r}) must match the call name {self.name!r}")
        if (event := self.cmd.event) is None:
            raise RuntimeError("sub() requires an event-bound Cmd; a detached Cmd cannot rewrite")
        if (occurrence := self.occurrence) is None or not self.spliceable:
            return None
        raws = [word.raw for word in self.command.words[1:]] if args is None else [target.raw for target in args]
        self.cmd.replacements[occurrence.index] = " ".join([new, *(emit_raw(raw) for raw in raws)])
        if note is not None:
            self.cmd.notes.append(note)
        return event.rewrite_command(
            self.cmd.line.splice(self.cmd.replacements),
            note="\n".join(dict.fromkeys(self.cmd.notes)) or None,
        )


@dataclass
class Cmd:
    """The parsed command line behind ``evt.cmd`` — a walk over every command invocation it contains.

    ``evt.cmd`` is always a ``Cmd`` (an empty line yields zero calls), never None, so a handler
    never guards before iterating. Construct one detached for scanning untrusted payloads:
    ``Cmd(command_line)`` over an already-parsed line, or ``Cmd.parse(text)`` (None when the text
    is too deeply nested to parse). A detached ``Cmd`` has no bound event, so :meth:`Call.sub`
    raises on it.

    Example:
        >>> if (call := evt.cmd.call("rm")) and len(call.targets.expand()) > 10:
        ...     return call.sub("rm", "trash")
    """

    line: CommandLine
    cwd: Path | None = None
    event: ToolRewriteEvent | None = None
    replacements: dict[int, str] = field(default_factory=dict, repr=False, compare=False)
    notes: list[str] = field(default_factory=list, repr=False, compare=False)

    @classmethod
    def parse(cls, text: str) -> Cmd | None:
        """Parse ``text`` into a detached ``Cmd``, or None when it is too deeply nested to parse."""
        return None if (line := safe_parse_command_line(text)) is None else cls(line)

    def calls(self, name: str | None = None) -> tuple[Call, ...]:
        """Every command invocation in the line (nested payloads included), or those named ``name``."""
        return self._calls if name is None else tuple(call for call in self._calls if call.name == name)

    def call(self, name: str) -> Call | None:
        """The first command invocation named ``name``, or None."""
        return next((call for call in self._calls if call.name == name), None)

    @cached_property
    def _calls(self) -> tuple[Call, ...]:
        return tuple(self._walk(self.line, self.cwd, NESTED_COMMAND_DEPTH, nested=False))

    def _walk(self, line: CommandLine, cwd: Path | None, depth: int, *, nested: bool) -> Iterator[Call]:
        for occurrence in line.occurrences:
            source = occurrence.command
            command = source.unwrapped
            yield Call(self, command, source, None if nested else occurrence, cwd, nested)
            if depth > 0 and (payload := nested_command_string(basename(command.executable), command.args)) is not None:
                if (inner := safe_parse_command_line(payload)) is not None:
                    yield from self._walk(inner, cwd, depth - 1, nested=True)
            if basename(command.executable) == "cd" and not occurrence.piped:
                cwd = resolve_cd(command.args, cwd)
