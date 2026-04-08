from __future__ import annotations

from captain_hook.app import hook as register_hook
from captain_hook.types import Command, Event, Tool, TTest, tokens_to_regex


def block_command(
    pattern: str | list[str],
    *,
    reason: str,
    hint: str | None = None,
    tests: TTest | None = None,
) -> None:
    """Register a declarative hook that blocks a Bash command matching a pattern.

    Example:
        >>> block_command(["git", "stash"], reason="git stash is not allowed", hint="Use jj")
    """
    msg = f"BLOCKED: {reason}.{f' {hint}.' if hint else ''}"
    cmd = Command(tokens_to_regex(pattern) if isinstance(pattern, list) else pattern)
    register_hook(Event.PreToolUse, only_if=[Tool("Bash"), cmd], message=msg, block=True, tests=tests)


def warn_command(
    pattern: str | list[str],
    *,
    message: str,
    tests: TTest | None = None,
    events: Event = Event.PostToolUse,
) -> None:
    """Register a declarative hook that warns on a Bash command matching a pattern.

    Example:
        >>> warn_command(["python", "-c", "*"], message="Prefer uv run mtest")
    """
    cmd = Command(tokens_to_regex(pattern) if isinstance(pattern, list) else pattern)
    register_hook(events, only_if=[Tool("Bash"), cmd], message=message, tests=tests)
