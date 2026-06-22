from __future__ import annotations

import re
from typing import TYPE_CHECKING

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
    tests: InlineTests | None = None,
) -> None:
    """Register a declarative hook that blocks a Bash command matching a pattern.

    Example:
        >>> block_command(["git", "stash"], reason="git stash is not allowed", hint="Use jj")
    """
    msg = f"BLOCKED: {reason}.{f' {hint}.' if hint else ''}"
    cmd = Command(block_command_pattern(pattern) if isinstance(pattern, list) else pattern)
    register_hook(Event.PreToolUse, only_if=[Tool("Bash"), cmd], message=msg, block=True, tests=tests)


def warn_command(
    pattern: str | list[str],
    *,
    message: str,
    tests: InlineTests | None = None,
    events: Event = Event.PostToolUse,
) -> None:
    """Register a declarative hook that warns on a Bash command matching a pattern.

    Example:
        >>> warn_command(["python", "-c", "*"], message="Prefer uv run mtest")
    """
    cmd = Command(block_command_pattern(pattern) if isinstance(pattern, list) else pattern)
    register_hook(events, only_if=[Tool("Bash"), cmd], message=message, tests=tests)


def rewrite_command(
    pattern: str | None = None,
    replace: str | None = None,
    *,
    only_if: Sequence[TCondition] = (),
    to: Callable[[PreToolUseEvent], str | None] | None = None,
    block: str | None = None,
    note: str | Callable[[PreToolUseEvent], str | None] | None = None,
    tests: InlineTests | None = None,
) -> None:
    """Register a ``PreToolUse`` hook that rewrites a matching Bash command.

    Two mutually exclusive forms:

    - **Regex** — pass ``pattern`` and ``replace``; matching commands are rewritten
      via ``re.sub``. ``note`` surfaces as ``additionalContext`` so the model sees the
      substitution.
    - **Conditional** — pass ``to``, a callable mapping the event to its new command
      (gated by ``[Tool("Bash"), *only_if]``). When ``to`` returns None the hook blocks
      with ``block`` if given, otherwise passes through. ``note`` may be a callable
      computed from the event.

    Example:
        >>> rewrite_command(r"^cat\\s+(\\S+)$", r"ccx read \\1 --full", note="Rewrote cat to ccx read")
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

        on(Event.PreToolUse, only_if=[Tool("Bash"), *only_if], tests=tests)(handler)
        return

    if pattern is None or replace is None or callable(note):
        raise TypeError("the regex form requires (pattern, replace) and a string note")

    def regex_handler(evt: PreToolUseEvent) -> HookResponse:
        return evt.rewrite_command(re.sub(pattern, replace, command), note=note) if (command := evt.command) else None

    on(Event.PreToolUse, only_if=[Tool("Bash"), Command(pattern)], tests=tests)(regex_handler)
