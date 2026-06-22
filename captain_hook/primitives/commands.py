from __future__ import annotations

import re
from typing import TYPE_CHECKING

from captain_hook.app import hook as register_hook
from captain_hook.app import on
from captain_hook.types import Command, Event, HookResult, InlineTests, Tool

if TYPE_CHECKING:
    from captain_hook.events import PreToolUseEvent


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
    pattern: str,
    replace: str,
    *,
    note: str | None = None,
    tests: InlineTests | None = None,
) -> None:
    """Register a ``PreToolUse`` hook that rewrites a matching Bash command via ``re.sub``.

    ``replace`` is a ``re.sub`` replacement applied to commands matching ``pattern``;
    ``note`` surfaces as ``additionalContext`` so the model sees the substitution.

    Example:
        >>> rewrite_command(r"^cat\\s+(\\S+)$", r"ccx read \\1 --full", note="Rewrote cat to ccx read")
    """

    def handler(evt: PreToolUseEvent) -> HookResult | None:
        return evt.rewrite_command(re.sub(pattern, replace, command), note=note) if (command := evt.command) else None

    on(Event.PreToolUse, only_if=[Tool("Bash"), Command(pattern)], tests=tests)(handler)
