from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Flag, StrEnum, auto
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from captain_hook.events import BaseHookEvent
    from captain_hook.signals.nlp import NlpSignal


class Event(Flag):
    """Hook lifecycle events that can trigger registered hooks.

    Combinable with ``|`` to match multiple events in a single hook registration.

    Example:
        >>> hook(Event.Stop | Event.SubagentStop, message="review first", block=True)
    """

    PreToolUse = auto()
    PostToolUse = auto()
    PostToolUseFailure = auto()
    UserPromptSubmit = auto()
    Stop = auto()
    SubagentStop = auto()
    SubagentStart = auto()
    PreCompact = auto()
    Notification = auto()

    @property
    def event_class(self) -> type[BaseHookEvent]:
        from captain_hook.events import (
            NotificationEvent,
            PostToolUseEvent,
            PostToolUseFailureEvent,
            PreCompactEvent,
            PreToolUseEvent,
            StopEvent,
            SubagentStartEvent,
            SubagentStopEvent,
            UserPromptSubmitEvent,
        )

        mapping: dict[Event, type[BaseHookEvent]] = {
            Event.PreToolUse: PreToolUseEvent,
            Event.PostToolUse: PostToolUseEvent,
            Event.PostToolUseFailure: PostToolUseFailureEvent,
            Event.UserPromptSubmit: UserPromptSubmitEvent,
            Event.Stop: StopEvent,
            Event.SubagentStop: SubagentStopEvent,
            Event.SubagentStart: SubagentStartEvent,
            Event.PreCompact: PreCompactEvent,
            Event.Notification: NotificationEvent,
        }
        if cls := mapping.get(self):
            return cls
        raise ValueError(f"event_class requires a single-member flag, got {self!r}")


class Action(StrEnum):
    """Hook result action determining how the hook output is handled.

    - ``block``: Prevents the tool use or stops the agent.
    - ``warn``: Adds advisory context without blocking.
    - ``allow``: Explicitly permits the action.
    """

    block = "block"
    warn = "warn"
    allow = "allow"


@dataclass(frozen=True, slots=True)
class Tool:
    """Condition matching the current event's tool name against a regex pattern.

    Use in ``only_if`` or ``skip_if`` to filter hooks by which tool is being used.

    Example:
        >>> hook(Event.PreToolUse, only_if=[Tool("Bash|Execute")], message="...", block=True)
    """

    pattern: str


TOOL_ALIASES: dict[str, str] = {
    "Bash": "Execute",
    "Write": "Create",
    "Agent": "Task",
    "WebFetch": "FetchUrl",
    "ExitPlanMode": "ExitSpecMode",
}

TOOL_ALIASES_REVERSE: dict[str, str] = {v: k for k, v in TOOL_ALIASES.items()}


def expand_tool_names(name: str) -> set[str]:
    return (base := set(name.split("|"))) | {
        alias for n in base for alias in (TOOL_ALIASES.get(n), TOOL_ALIASES_REVERSE.get(n)) if alias
    }


@dataclass(frozen=True, slots=True, init=False)
class FilePath:
    """Condition matching the current event's file path against glob patterns.

    Accepts one or more glob patterns as positional arguments.

    Example:
        >>> hook(Event.PostToolUse, only_if=[FilePath("*.py", "*.pyi")], message="Python file edited")
    """

    patterns: tuple[str, ...]

    def __init__(self, *patterns: str) -> None:
        object.__setattr__(self, "patterns", patterns)


@dataclass(frozen=True, slots=True)
class Command:
    """Condition matching the current event's bash command against a regex.

    Only relevant for ``PreToolUse`` events targeting the Bash/Execute tool.

    Example:
        >>> hook(Event.PreToolUse, only_if=[Command(r"git\\s+stash")], message="blocked", block=True)
    """

    pattern: str


@dataclass(frozen=True, slots=True)
class Content:
    """Condition matching the current event's file content against a regex.

    Applies to Edit (new content) and Write (file content) tool events.
    """

    pattern: str


@dataclass(frozen=True, slots=True)
class UsedSkill:
    """Transcript-history condition: true when a Skill tool use with a matching name exists."""

    name: str


@dataclass(frozen=True, slots=True, init=False)
class ReadFile:
    """Transcript-history condition: true when a Read tool use targeted a matching file.

    Accepts one or more glob patterns as positional arguments.
    """

    patterns: tuple[str, ...]

    def __init__(self, *patterns: str) -> None:
        object.__setattr__(self, "patterns", patterns)


@dataclass(frozen=True, slots=True)
class TestFile:
    """Condition that matches when the current event targets a test file (``test_*.py``, ``conftest.py``)."""

    pass


@dataclass(frozen=True, slots=True)
class Agent:
    """Condition matching the current event's subagent type against a name pattern."""

    name: str


@dataclass(frozen=True, slots=True, init=False)
class TouchedFile:
    """Transcript-history condition: true when an Edit/Write targeted a file matching the glob.

    Accepts one or more glob patterns as positional arguments.
    """

    patterns: tuple[str, ...]

    def __init__(self, *patterns: str) -> None:
        object.__setattr__(self, "patterns", patterns)


@dataclass(frozen=True, slots=True)
class RanCommand:
    """Transcript-history condition: true when a Bash tool use with a matching command exists."""

    pattern: str


@dataclass(frozen=True, slots=True)
class InPlanMode:
    """Transcript-history condition: true when more ``EnterPlanMode`` than ``ExitPlanMode`` tool uses exist."""

    pass


@runtime_checkable
class CustomCondition(Protocol):
    """Protocol for user-defined hook conditions.

    Implement ``check`` to create arbitrary matching logic beyond the
    built-in condition types.

    Example:
        >>> class LargeFile(CustomCondition):
        ...     def check(self, evt: BaseHookEvent) -> bool:
        ...         return bool(evt.file and evt.file.path.stat().st_size > 1_000_000)
        ...
        >>> app.hook(Event.PreToolUse, only_if=[LargeFile()], message="Large file", block=True)
    """

    def check(self, evt: BaseHookEvent) -> bool: ...


TCondition = (
    Tool
    | FilePath
    | Command
    | Content
    | Agent
    | UsedSkill
    | ReadFile
    | TestFile
    | TouchedFile
    | RanCommand
    | InPlanMode
    | CustomCondition
)


@dataclass(frozen=True, kw_only=True, slots=True)
class Signal:
    """A regex-based signal pattern used in the scoring pipeline.

    Signals are matched against transcript text via ``re.search``. Each match
    contributes ``weight`` to the cumulative score. Use negative weights to
    suppress false positives.

    Example:
        >>> Signal(pattern=r"retry", weight=2, flags=re.IGNORECASE)
    """

    pattern: str
    weight: int = 1
    flags: int = 0


@dataclass(frozen=True, slots=True)
class Signals:
    """Bundle of signal patterns with a scoring threshold.

    When a bare ``list[Signal]`` is passed to a primitive, ``resolve_signals``
    wraps it with ``threshold=1`` — meaning *any* single signal match triggers.
    Pass a higher threshold to require multiple signals to fire together.
    """

    patterns: Sequence[Signal | NlpSignal]
    threshold: int
    window: int = 15


@dataclass(frozen=True, kw_only=True)
class HookResult:
    """The return value from a hook handler, specifying the action and optional message."""

    action: Action
    message: str | None = None


from captain_hook.testing.types import TTest as TTest


def tokens_to_regex(tokens: list[str]) -> str:
    """Convert a token list into a whitespace-flexible regex pattern.

    ``"*"`` becomes ``\\S+`` (any non-whitespace word), ``"a|b"`` becomes an
    alternation group, and all other tokens are escaped.

    Example:
        >>> tokens_to_regex(["git", "stash", "*"])
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


@dataclass(frozen=True, kw_only=True)
class HookSpec:
    """Internal specification for a registered hook: events, conditions, and behavior flags."""

    events: Event
    only_if: tuple[TCondition, ...] = ()
    skip_if: tuple[TCondition, ...] = ()
    message: str | None = None
    block: bool = False
    respect_gitignore: bool = True
    max_fires: int | None = None
    tests: TTest | None = None
    async_: bool = False


@dataclass(frozen=True, kw_only=True)
class RegisteredHook:
    """A hook registered on a ``HookApp``, pairing a ``HookSpec`` with an optional handler callable."""

    spec: HookSpec
    handler: Callable[[BaseHookEvent], HookResult | None] | None = None
    name: str = ""
    source_file: str = ""
