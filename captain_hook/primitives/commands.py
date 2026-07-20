from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from captain_hook import ast_grep
from captain_hook.app import hook as register_hook
from captain_hook.app import on
from captain_hook.types import Command, Event, HookResponse, HookResult, InlineTests, Tool
from captain_hook.util.shell import normalize_executable, plain_words, resolve_cd

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from cc_transcript.command import CommandLine, Occurrence

    from captain_hook.events import PreToolUseEvent
    from captain_hook.types import TCondition


@dataclass(frozen=True, slots=True)
class WalkContext:
    """Per-occurrence walk state handed to a ``visit=`` callable.

    Attributes:
        cwd: The effective working directory at this occurrence — ``evt.cwd`` threaded
            through any statically resolvable ``cd`` occurrence earlier in the line;
            ``None`` when unknown.
        plain_words: Whether the occurrence's raw text is quote-free, so every parsed
            argument is its verbatim source spelling.
        spliceable: Whether a rewrite can be spliced back — the occurrence has a byte
            span and no backslash-newline continuation. Returning a rewrite for a
            non-spliceable occurrence raises.
    """

    cwd: Path | None
    plain_words: bool
    spliceable: bool


@dataclass(frozen=True, slots=True)
class Rewritten:
    """A ``visit=`` verdict replacing one occurrence, optionally carrying a note.

    Example:
        >>> Rewritten("trash file.txt", note="Rewrote rm to trash")
    """

    text: str
    note: str | None = None


def block_command_pattern(tokens: list[str]) -> str:
    """Convert a token list into a whitespace-flexible regex pattern.

    ``"*"`` becomes ``\\S+`` (any non-whitespace word), ``"a|b"`` becomes an
    alternation group, and all other tokens are escaped.

    Example:
        >>> block_command_pattern(["git", "stash", "*"])
        'git\\\\s+stash\\\\s+\\\\S+'
    """

    def convert(token: str) -> str:
        match token:
            case "*":
                return r"\S+"
            case t if "|" in t:
                return f"(?:{'|'.join(re.escape(a) for a in t.split('|'))})"
            case t:
                return re.escape(t)

    return r"\s+".join(convert(t) for t in tokens)


def block_command(
    pattern: str | list[str],
    *,
    reason: str,
    hint: str | None = None,
    only_if: Sequence[TCondition] = (),
    skip_if: Sequence[TCondition] = (),
    tests: InlineTests | None = None,
) -> None:
    """Register a declarative hook that blocks a Bash command matching a pattern.

    Scope the block further with ``only_if``/``skip_if`` (ANDed onto the built-in
    ``[Tool("Bash"), <pattern>]``), e.g. to allow it in plan mode or only under a path.

    Example:
        >>> block_command(["git", "stash"], reason="git stash is not allowed", hint="Use jj")
    """
    msg = f"BLOCKED: {reason.rstrip('.')}.{f' {hint.rstrip(".")}.' if hint else ''}"
    cmd = Command(block_command_pattern(pattern) if isinstance(pattern, list) else pattern)
    register_hook(
        Event.PreToolUse, msg, only_if=[Tool("Bash"), cmd, *only_if], skip_if=skip_if, block=True, tests=tests
    )


def warn_command(
    pattern: str | list[str],
    *,
    message: str,
    only_if: Sequence[TCondition] = (),
    skip_if: Sequence[TCondition] = (),
    tests: InlineTests | None = None,
    events: Event = Event.PostToolUse,
) -> None:
    """Register a declarative hook that warns on a Bash command matching a pattern.

    Example:
        >>> warn_command(["python", "-c", "*"], message="Prefer uv run mtest")
    """
    cmd = Command(block_command_pattern(pattern) if isinstance(pattern, list) else pattern)
    register_hook(events, message, only_if=[Tool("Bash"), cmd, *only_if], skip_if=skip_if, tests=tests)


def rewrite_command(
    pattern: str | None = None,
    replace: str | None = None,
    *,
    only_if: Sequence[TCondition] = (),
    skip_if: Sequence[TCondition] = (),
    to: Callable[[PreToolUseEvent], str | None] | None = None,
    block: str | None = None,
    note: str | Callable[[PreToolUseEvent], str | None] | None = None,
    tests: InlineTests | None = None,
) -> None:
    r"""Register a ``PreToolUse`` hook that rewrites a matching Bash command.

    Two mutually exclusive forms:

    - **(pattern, replace)** — polymorphic by the shape of ``pattern``. When it carries an ast-grep
      metavariable (``$NAME`` / ``$$$NAME``), the command is rewritten *structurally* over
      tree-sitter-bash via [`ast_grep.rewrite`][captain_hook.ast_grep.rewrite]; otherwise it is
      a regex rewritten via ``re.sub``. So ``rewrite_command("cat $$$ARGS", "bat $$$ARGS")`` is
      structural and ``rewrite_command(r"^cat\s+(\S+)$", r"bat \1")`` is regex. ``note`` surfaces as
      ``additionalContext`` so the model sees the substitution.
    - **Conditional** — pass ``to``, a callable mapping the event to its new command (gated by
      ``[Tool("Bash"), *only_if]``); the escape hatch when the heuristic isn't enough. When ``to``
      returns None the hook blocks with ``block`` if given, otherwise passes through. ``note`` may be
      a callable computed from the event.

    A structural single metavar binds only the first argument and drops the rest (``"cat $F"`` turns
    ``cat -n foo.txt`` into ``bat -n``); prefer ``$$$ARGS`` to carry the whole argument list. A ``$VAR``
    in the replacement that the pattern never captured stays literal, so a shell variable like
    ``$HOME`` in the new command survives untouched.

    Example:
        >>> rewrite_command("cat $$$ARGS", "bat $$$ARGS", note="bat renders with syntax highlighting")
        >>> rewrite_command(r"^cat\s+(\S+)$", r"ccx read \1 --full", note="Rewrote cat to ccx read")
        >>> rewrite_command(only_if=[Command("git push")], to=lambda e: None, block="Pushing is disabled")
    """
    if (to is None) == (pattern is None):
        raise TypeError("rewrite_command takes either (pattern, replace) or to=, not both or neither")

    if to is not None:

        def handler(evt: PreToolUseEvent) -> HookResponse:
            new = to(evt)
            if new is None:
                return evt.block(block) if block else None
            return evt.rewrite_command(new, note=note(evt) if callable(note) else note)

        on(Event.PreToolUse, only_if=[Tool("Bash"), *only_if], skip_if=skip_if, tests=tests)(handler)
        return

    if pattern is None or replace is None or callable(note):
        raise TypeError("the (pattern, replace) form requires a replace string and a non-callable note")

    if ast_grep.has_metavar(pattern):

        def structural_handler(evt: PreToolUseEvent) -> HookResponse:
            if not (raw := evt.cmd.raw):
                return None
            new = ast_grep.rewrite(raw, "bash", pattern, replace)
            return evt.rewrite_command(new, note=note) if new != raw else None

        on(Event.PreToolUse, only_if=[Tool("Bash"), *only_if], skip_if=skip_if, tests=tests)(structural_handler)
        return

    def regex_handler(evt: PreToolUseEvent) -> HookResponse:
        return evt.rewrite_command(re.sub(pattern, replace, raw), note=note) if (raw := evt.cmd.raw) else None

    on(Event.PreToolUse, only_if=[Tool("Bash"), Command(pattern), *only_if], skip_if=skip_if, tests=tests)(
        regex_handler
    )


def rewrite_command_occurrences(
    *,
    to: Callable[[PreToolUseEvent, Occurrence], str | None] | None = None,
    visit: Callable[[PreToolUseEvent, Occurrence, WalkContext], str | Rewritten | HookResult | None] | None = None,
    only_if: Sequence[TCondition] = (),
    skip_if: Sequence[TCondition] = (),
    block_if: Callable[[PreToolUseEvent, Occurrence], bool] | None = None,
    block: str | Callable[[PreToolUseEvent, CommandLine], str] | None = None,
    note: str | Callable[[PreToolUseEvent, Sequence[tuple[Occurrence, str]]], str | None] | None = None,
    tests: InlineTests | None = None,
) -> None:
    r"""Register a ``PreToolUse`` hook using one of two mutually exclusive occurrence forms.

    Unlike [`rewrite_command`][captain_hook.rewrite_command]'s ``to=`` form — which maps the
    whole line to one new command — ``to`` here is called once per parsed
    [`Occurrence`][cc_transcript.command.Occurrence] of a ``;``/``&&``/``|``-joined line, so a
    guard can rewrite one segment of a compound command while leaving its siblings untouched.
    ``to`` is never called for a span-less occurrence (e.g. an absorbed-word redirect shape) —
    those commands are unspliceable by design and pass through untouched, though ``block_if``
    still sees them.

    Every occurrence ``to`` rewrites is spliced into the line in one pass, so a multi-occurrence
    rewrite surfaces as a single ``additionalContext`` note; pass a callable ``note`` to compose
    one message from every ``(Occurrence, replacement)`` pair.

    ``block_if``, when given, checks every occurrence — including span-less ones — *before* any
    rewrite is attempted: if any occurrence trips it, the whole line blocks with ``block`` rather
    than executing a partial rewrite. This is deliberately line-grained — one unrewritable
    segment must not run just because a sibling was rewritable — so ``block_if`` requires a
    non-empty ``block`` at registration time (an empty string is rejected too, since both
    branches below treat ``block`` by truthiness). When nothing rewrites and no ``block_if``
    trips, the line falls through to ``block`` (if given) or passes through untouched.

    ``block`` may be a callable ``(evt, cl) -> str`` computing the message from the event and its
    parsed [`CommandLine`][cc_transcript.command.CommandLine] — resolved lazily only when the line
    actually blocks, at either the ``block_if`` hit or the zero-rewrite fallthrough.

    - **visit** — a stateful walk: called for *every* occurrence in order — span-less ones
      included — with a [`WalkContext`][captain_hook.WalkContext] carrying the effective
      ``cwd`` (threaded through statically resolvable ``cd`` occurrences), quote provenance
      (``plain_words``), and splice eligibility (``spliceable``). Each call returns one of
      four verdicts: a [`HookResult`][captain_hook.HookResult] (typically ``evt.block(...)``)
      aborts the walk and returns it verbatim — accumulated rewrites are discarded, never
      partially applied; a ``str`` or [`Rewritten`][captain_hook.Rewritten] replaces the
      occurrence, with ``Rewritten.note`` lines deduplicated into one ``additionalContext``
      message; ``None`` leaves the occurrence untouched. The ``visit`` form takes no
      ``block_if``/``block``/``note`` — blocking and notes ride on the verdicts.

    Example:
        >>> rewrite_command_occurrences(
        ...     to=lambda evt, occ: "ccx read foo.py --full" if occ.command.matches("^cat foo.py$") else None,
        ...     note=lambda evt, pairs: f"Rewrote {len(pairs)} cat invocation(s) to ccx read",
        ... )
    """
    if (visit is None) == (to is None):
        raise TypeError("rewrite_command_occurrences takes either to= or visit=, not both or neither")
    if visit is not None:
        if any(option is not None for option in (block_if, block, note)):
            raise TypeError(
                "the visit= form takes no block_if/block/note — return a HookResult to block and carry "
                "notes on Rewritten"
            )

        def visit_handler(evt: PreToolUseEvent) -> HookResponse:
            if not (cl := evt.cmd.line).raw:
                return None
            effective_cwd = evt.cwd
            replacements: dict[int, str] = {}
            notes: list[str] = []
            for occurrence in cl.occurrences:
                command = occurrence.command
                ctx = WalkContext(
                    cwd=effective_cwd,
                    plain_words=plain_words(command.raw),
                    spliceable=command.span is not None and "\\\n" not in command.raw,
                )
                match visit(evt, occurrence, ctx):
                    case HookResult() as result:
                        return result
                    case str() | Rewritten() as verdict:
                        if not ctx.spliceable:
                            raise ValueError("visit returned a rewrite for a non-spliceable occurrence")
                        match verdict:
                            case Rewritten(text, rewrite_note):
                                replacements[occurrence.index] = text
                                if rewrite_note is not None:
                                    notes.append(rewrite_note)
                            case text:
                                replacements[occurrence.index] = text
                    case None:
                        pass
                unwrapped = command.unwrapped
                if normalize_executable(unwrapped.executable) == "cd" and not occurrence.piped:
                    effective_cwd = resolve_cd(unwrapped.args, effective_cwd)
            if replacements and (spliced := cl.splice(replacements)) != cl.raw:
                return evt.rewrite_command(spliced, note="\n".join(dict.fromkeys(notes)) or None)
            return None

        on(Event.PreToolUse, only_if=[Tool("Bash"), *only_if], skip_if=skip_if, tests=tests)(visit_handler)
        return

    if block_if is not None and not block:
        raise TypeError("rewrite_command_occurrences requires a non-empty block= when block_if is given")

    def handler(evt: PreToolUseEvent) -> HookResponse:
        if not (cl := evt.cmd.line).raw:
            return None
        if block_if is not None and any(block_if(evt, occ) for occ in cl.occurrences):
            return evt.block(block(evt, cl) if callable(block) else block) if block else None
        pairs = [
            (occ, new) for occ in cl.occurrences if occ.command.span is not None and (new := to(evt, occ)) is not None
        ]
        if pairs and (spliced := cl.splice({occ.index: new for occ, new in pairs})) != cl.raw:
            return evt.rewrite_command(spliced, note=note(evt, pairs) if callable(note) else note)
        return evt.block(block(evt, cl) if callable(block) else block) if block else None

    on(Event.PreToolUse, only_if=[Tool("Bash"), *only_if], skip_if=skip_if, tests=tests)(handler)
