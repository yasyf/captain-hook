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
from captain_hook.util.shell import resolve_cd, safe_parse_command_line
from captain_hook.util.vcs import contains_repo, in_vcs_repo, is_repo_root

if TYPE_CHECKING:
    from collections.abc import Iterator

    from cc_transcript.command import Command, CommandLine, CommandLineQuery, Occurrence, Redirect

    from captain_hook.events import ToolRewriteEvent
    from captain_hook.types import HookResult

WRAPPER_COMMANDS: frozenset[str] = frozenset(LITERALS["command.WRAPPER_COMMANDS"])
COMMAND_VALUE_FLAGS: dict[str, tuple[str, ...]] = {
    "git": ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"),
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
    walk blew its 20k-entry budget before completing, or when the targets cannot be statically
    verified at all — an unverified target, or an operand list a command substitution lifted a
    word out of. Either way the expansion is incomplete and a guard must not treat it as safe.

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

    ``value`` is the operand with shell quoting removed, or None when an unresolved expansion
    (``$VAR``, ``$(...)``) taints the word; such a target is not :attr:`verified` and can never
    be statically classified — every predicate returns False and :meth:`expand` reports itself
    exhausted, so a guard falls through to its fail-closed branch instead of approving. A glob
    or tilde operand (``*.py``, ``~/x``) keeps its value and stays verified: bash expands it,
    but to statically known candidates.

    ``path`` resolves the parent directory but keeps the final component **literal**: a symlink
    is classified as itself, never followed through, which is what a deletion or command target
    needs (act on the entity named, not what it points at). This is deliberately unlike the
    ``ScratchPath`` condition, which *fully* resolves — write-approval must see through a symlink
    to where the bytes actually land. Do not unify the two.

    Every predicate returns False when ``path`` is None (an unverified target, or a relative
    target with no known cwd).

    Example:
        >>> target.is_repo_root or target.contains_repo
    """

    value: str | None
    raw: str
    cwd: Path | None

    @property
    def verified(self) -> bool:
        """Whether the operand is statically known — False when an expansion taints it."""
        return self.value is not None

    @cached_property
    def path(self) -> Path | None:
        """The target resolved against ``cwd``, parent-resolved with the final component kept literal."""
        return None if self.value is None else resolve_target(self.value, self.cwd)

    @property
    def has_glob(self) -> bool:
        """Whether the target's value contains shell glob metacharacters."""
        return self.value is not None and glob.has_magic(self.value)

    def expand(self, *, limit: int = GLOB_LIMIT) -> Expansion:
        """Expand a glob target to its matches (capped at ``limit + 1``); a literal yields itself.

        An unverified target expands to nothing, ``exhausted`` — it cannot be verified.
        """
        if self.value is None:
            return Expansion((), True)
        if not self.has_glob:
            return Expansion((self.value,), False)
        matches, too_broad = glob_matches(self.value, self.cwd, limit=limit)
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
    """The operand targets of a command, in order — iterable, sized, and expandable as a whole.

    ``complete`` is False when a bare command substitution (``$(...)``, backticks) was lifted
    out of the command's words, so the collection under-counts the real operands; ``verified``
    is the one-stop safety check — every operand present, statically known, and dequoted.
    """

    targets: tuple[Target, ...] = ()
    complete: bool = True

    @property
    def verified(self) -> bool:
        """Whether the operand list is complete and every target is verified."""
        return self.complete and all(target.verified for target in self.targets)

    def __iter__(self) -> Iterator[Target]:
        return iter(self.targets)

    def __len__(self) -> int:
        return len(self.targets)

    def __bool__(self) -> bool:
        return bool(self.targets)

    def expand(self, *, limit: int = GLOB_LIMIT) -> Expansion:
        """Every target's expansion concatenated: literals contribute themselves, globs their matches.

        Reports itself ``exhausted`` when any part does or the collection is incomplete.
        """
        parts = [target.expand(limit=limit) for target in self.targets]
        return Expansion(
            tuple(match for part in parts for match in part.matches),
            not self.complete or any(part.exhausted for part in parts),
        )


@dataclass(frozen=True)
class Call:
    """One command invocation reached by walking a command line — a top-level occurrence, a
    nested ``sh -c``/``eval`` payload, or a command substitution.

    ``command`` is the cc-transcript ``Command`` with leading wrappers (``sudo``, ``env``,
    ``timeout``, …) stripped arity-aware; ``name`` is the **dequoted** head word's basename,
    casefolded (``"rm" -rf /`` names the call ``rm``), so matching is command-position-only by
    construction (``echo rm foo`` and ``git rm`` never match ``"rm"``) and quoting the
    executable never evades it.

    Example:
        >>> for call in evt.cmd.calls("rm"):
        ...     if call.targets.expand().exhausted:
        ...         return evt.block("targets too broad to verify")
        ...     return call.sub("rm", "trash", args=call.targets) or evt.block("unrecoverable rm")
    """

    cmd: Cmd
    occurrence: Occurrence
    cwd: Path | None

    @property
    def source(self) -> Command:
        """The command as written, wrappers included."""
        return self.occurrence.command

    @cached_property
    def command(self) -> Command:
        """The command with leading wrappers stripped."""
        return self.source.unwrapped

    @property
    def name(self) -> str:
        """The unwrapped head word's basename, dequoted and casefolded."""
        words = self.command.words
        return basename(words[0].value if words and words[0].value is not None else self.command.executable)

    @property
    def wrappers(self) -> tuple[str, ...]:
        """The leading wrapper commands stripped to reach this call, e.g. ``("sudo",)``.

        Identified by basename against the authoritative ``WRAPPER_COMMANDS`` table. A wrapper
        value-flag argument that itself names a wrapper (``sudo -u env rm``) can appear here, but
        a membership check against a real wrapper head is unaffected.
        """
        stripped = self.source.words[: len(self.source.words) - len(self.command.words)]
        return tuple(
            name
            for word in stripped
            if (name := basename(word.value if word.value is not None else word.raw)) in WRAPPER_COMMANDS
        )

    @property
    def args(self) -> tuple[str, ...]:
        """The unwrapped command's arguments, with shell quoting removed."""
        return self.command.args

    @property
    def redirects(self) -> tuple[Redirect, ...]:
        """The command's file redirects."""
        return self.command.redirects

    @property
    def nested(self) -> bool:
        """Whether this call sits below top level — a payload or substitution hop away."""
        return self.occurrence.nesting > 0

    @property
    def substituted(self) -> bool:
        """Whether a command substitution (``$(...)``, backticks) feeds this call's words.

        A bare substitution word is lifted out of the command's words entirely, so
        :attr:`targets` under-counts and :meth:`sub` cannot re-emit the call faithfully —
        both refuse accordingly.
        """
        return any(
            (host := occurrence.host) is not None
            and host.index == self.occurrence.index
            and len(occurrence.quote_contexts) == len(self.occurrence.quote_contexts)
            for occurrence in self.cmd.line.occurrences
        )

    @property
    def spliceable(self) -> bool:
        """Whether a rewrite can be spliced back — the source command carries a byte span.

        Substitution payloads, ``fish -c`` payloads, and joined ``eval`` payloads carry none.
        A nested payload with a span splices through its quote layers when the replacement
        survives them, which :meth:`sub` checks per rewrite.
        """
        return self.source.span is not None

    @property
    def flags(self) -> tuple[str, ...]:
        """The option tokens, dequoted, with any registered value-flag's argument (``git -C <dir>``) alongside it."""
        return self._split[0]

    @property
    def targets(self) -> Targets:
        """The operand tokens as :class:`Target` objects, value-flag arguments and options removed."""
        return self._split[1]

    @cached_property
    def _split(self) -> tuple[tuple[str, ...], Targets]:
        options, operands = self.command.split_options(COMMAND_VALUE_FLAGS.get(self.name, ()))
        return (
            tuple(word.value if word.value is not None else word.raw for word in options),
            Targets(
                tuple(Target(word.value, word.raw, self.cwd) for word in operands),
                complete=not self.substituted,
            ),
        )

    def sub(self, old: str, new: str, *, args: Targets | None = None, note: str | None = None) -> HookResult | None:
        """Rewrite this call's ``old`` executable to ``new`` in place, returning the rewrite result.

        ``old`` must equal :attr:`name` (else :class:`ValueError` — a programming error, not a runtime
        condition). Returns None when the call is not :attr:`spliceable`, is :attr:`substituted`
        (its operands cannot be re-emitted faithfully), or the replacement cannot survive the
        enclosing quote layers — so a handler composes the fail-closed fallback in policy code:
        ``call.sub("rm", trash) or evt.block(...)``.

        The occurrence's span is replaced with ``new`` followed by the re-emitted arguments — each
        argument's verbatim source spelling (``Word.raw``). ``args`` defaults to the call's own
        arguments; pass ``args=call.targets`` to drop flags and keep only operands, in which case
        a ``-``-leading raw gains a ``./`` prefix so ``new`` never misparses it as a flag. Leading
        wrappers drop (``sudo rm /x`` → ``trash /x``); redirects outside the span survive.

        Subs accumulate on the parent :class:`Cmd`: each call returns a fresh rewrite splicing every
        sub so far, so returning the last applies them all (``rm a && rm b`` rewrites both) and
        returning ``evt.block(...)`` instead discards them all — a line never rewrites partially.
        A ``Cmd`` that is detached or bound to a non-rewrite event raises :class:`RuntimeError`.
        """
        if old != self.name:
            raise ValueError(f"sub(old={old!r}) must match the call name {self.name!r}")
        if (event := self.cmd.event) is None:
            raise RuntimeError(
                "sub() requires a rewrite-capable event; this Cmd is detached or bound to a non-rewrite event"
            )
        if not self.spliceable or self.substituted:
            return None
        raws = [word.raw for word in self.command.words[1:]] if args is None else [emit_raw(t.raw) for t in args]
        replacement = " ".join([new, *raws])
        if not self.occurrence.embeddable(replacement):
            return None
        self.cmd.replacements[self.occurrence.index] = replacement
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
    never guards before iterating. The walk consumes the parser's payload parts: nested
    ``sh -c``/``eval`` payloads and command substitutions surface as calls of their own (three
    levels deep), and ``cd`` threads the working directory to the calls after it within the same
    payload scope. Construct one detached for scanning untrusted payloads: ``Cmd(command_line)``
    over an already-parsed line, or ``Cmd.parse(text)`` (None when the text is too deeply nested
    to parse). A detached ``Cmd`` has no bound event, so :meth:`Call.sub` raises on it.

    ``raw`` is the true original command text as written: ``str(cmd)`` returns it, and it is the
    operand for raw-text matching — the :class:`~captain_hook.conditions.Command` regex and ``ast_grep``
    search it, so a line that parses to zero commands (a comment, a shebang) still matches by its text.
    ``bool(cmd)`` follows :attr:`raw`, staying truthy for such a line even when :attr:`line` is falsy.
    :attr:`q` and :attr:`line` expose the parsed structure.

    Example:
        >>> if (call := evt.cmd.call("rm")) and len(call.targets.expand()) > 10:
        ...     return call.sub("rm", "trash")
    """

    line: CommandLine
    raw: str = ""
    cwd: Path | None = None
    event: ToolRewriteEvent | None = None
    replacements: dict[int, str] = field(default_factory=dict, repr=False, compare=False)
    notes: list[str] = field(default_factory=list, repr=False, compare=False)

    def __str__(self) -> str:
        return self.raw

    def __bool__(self) -> bool:
        return bool(self.raw)

    @property
    def q(self) -> CommandLineQuery:
        """The fluent query over the parsed line — :attr:`CommandLine.q`."""
        return self.line.q

    @classmethod
    def parse(cls, text: str) -> Cmd | None:
        """Parse ``text`` into a detached ``Cmd``, or None when it is too deeply nested to parse."""
        return None if (line := safe_parse_command_line(text)) is None else cls(line, raw=text)

    def calls(self, name: str | None = None) -> tuple[Call, ...]:
        """Every command invocation in the line (nested payloads included), or those named ``name``."""
        return self._calls if name is None else tuple(call for call in self._calls if call.name == name)

    def call(self, name: str) -> Call | None:
        """The first command invocation named ``name``, or None."""
        return next((call for call in self._calls if call.name == name), None)

    @cached_property
    def _calls(self) -> tuple[Call, ...]:
        calls: list[Call] = []
        scopes: dict[int | None, Path | None] = {None: self.cwd}
        effective: dict[int | None, Path | None] = {}
        for occurrence in self.line.occurrences:
            host = occurrence.host.index if occurrence.host is not None else None
            cwd = scopes.setdefault(host, effective.get(host))
            effective[occurrence.index] = cwd
            calls.append(call := Call(self, occurrence, cwd))
            if call.name == "cd" and not occurrence.piped:
                scopes[host] = resolve_cd(call.command.args, cwd)
        return tuple(calls)
