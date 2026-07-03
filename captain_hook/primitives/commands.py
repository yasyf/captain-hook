from __future__ import annotations

import re
from typing import TYPE_CHECKING

from captain_hook import ast_grep
from captain_hook.app import hook as register_hook
from captain_hook.app import on
from captain_hook.types import Command, Event, HookResponse, InlineTests, Tool

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from captain_hook.events import PreToolUseEvent
    from captain_hook.types import TCondition


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
      tree-sitter-bash via [`CommandLine.rewrite`][captain_hook.CommandLine.rewrite]; otherwise it is
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
            if not (cl := evt.command_line):
                return None
            new = ast_grep.rewrite(cl.raw, "bash", pattern, replace)
            return evt.rewrite_command(new, note=note) if new != cl.raw else None

        on(Event.PreToolUse, only_if=[Tool("Bash"), *only_if], skip_if=skip_if, tests=tests)(structural_handler)
        return

    def regex_handler(evt: PreToolUseEvent) -> HookResponse:
        return evt.rewrite_command(re.sub(pattern, replace, command), note=note) if (command := evt.command) else None

    on(Event.PreToolUse, only_if=[Tool("Bash"), Command(pattern), *only_if], skip_if=skip_if, tests=tests)(
        regex_handler
    )
