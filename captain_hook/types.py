from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Flag, StrEnum, auto
from textwrap import dedent
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from captain_hook.events import BaseHookEvent
    from captain_hook.signals.nlp import NlpSignal


TOOL_ALIASES: dict[str, str] = {
    "Bash": "Execute",
    "Write": "Create",
    "Agent": "Task",
    "WebFetch": "FetchUrl",
    "ExitPlanMode": "ExitSpecMode",
}

TOOL_ALIASES_REVERSE: dict[str, str] = {v: k for k, v in TOOL_ALIASES.items()}

LANG_GLOBS: dict[str, tuple[str, ...]] = {
    "py": ("*.py", "*.pyi"),
    "ts": ("*.ts",),
    "tsx": ("*.tsx",),
    "js": ("*.js", "*.mjs", "*.cjs"),
    "jsx": ("*.jsx",),
    "go": ("*.go",),
    "rs": ("*.rs",),
    "java": ("*.java",),
}


def expand_tool_names(name: str) -> set[str]:
    return (base := set(name.split("|"))) | {
        alias for n in base for alias in (TOOL_ALIASES.get(n), TOOL_ALIASES_REVERSE.get(n)) if alias
    }


def tool_name_matches(actual: str, query: str) -> bool:
    return actual in (candidates := expand_tool_names(query)) or (
        actual.startswith("mcp__") and len(parts := actual.split("__", 2)) == 3 and parts[2] in candidates
    )


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


@dataclass(frozen=True)
class PatternsCondition:
    patterns: tuple[str, ...]


@dataclass(frozen=True)
class FilePathFields(PatternsCondition):
    project_only: bool


class FilePath(FilePathFields):
    """Condition matching the current event's file path against glob patterns.

    Accepts one or more glob patterns as positional arguments.

    Example:
        >>> hook(Event.PostToolUse, only_if=[FilePath("*.py", "*.pyi")], message="Python file edited")
    """

    def __init__(self, *patterns: str, **kwargs: bool) -> None:
        super().__init__(patterns=patterns, project_only=kwargs.pop("project_only", True))


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
    project_only: bool = True


@dataclass(frozen=True, slots=True)
class UsedSkill:
    """Transcript-history condition: true when a Skill tool use with a matching name exists."""

    name: str
    subagents: bool = True


@dataclass(frozen=True)
class ReadFileFields(PatternsCondition):
    subagents: bool


class ReadFile(ReadFileFields):
    """Transcript-history condition: true when a Read tool use targeted a matching file.

    Accepts one or more glob patterns as positional arguments.
    """

    def __init__(self, *patterns: str, **kwargs: bool) -> None:
        super().__init__(patterns=patterns, subagents=kwargs.pop("subagents", True))


@dataclass(frozen=True, slots=True)
class TestFile:
    """Condition that matches when the current event targets a test file (``test_*.py``, ``conftest.py``)."""

    __test__ = False

    project_only: bool = True


@dataclass(frozen=True, slots=True)
class SourceEdits:
    lang: str = "py"
    include_tests: bool = False
    paths: str | None = None

    @property
    def globs(self) -> tuple[str, ...]:
        return LANG_GLOBS.get(self.lang, (f"*.{self.lang}",))


@dataclass(frozen=True, slots=True)
class Agent:
    """Condition matching the current event's subagent type against a name pattern."""

    name: str


@dataclass(frozen=True)
class TouchedFileFields(PatternsCondition):
    subagents: bool


class TouchedFile(TouchedFileFields):
    """Transcript-history condition: true when an Edit/Write targeted a file matching the glob.

    Accepts one or more glob patterns as positional arguments.
    """

    def __init__(self, *patterns: str, **kwargs: bool) -> None:
        super().__init__(patterns=patterns, subagents=kwargs.pop("subagents", False))


@dataclass(frozen=True, slots=True)
class RanCommand:
    """Transcript-history condition: true when a Bash tool use with a matching command exists."""

    pattern: str
    subagents: bool = True


@dataclass(frozen=True, slots=True)
class InPlanMode:
    """Matches when the agent is in plan mode.

    Reads ``permission_mode`` from the current event payload; falls back to
    counting ``EnterPlanMode`` vs ``ExitPlanMode`` tool uses in the transcript
    when the payload omits the field.
    """

    pass


@dataclass(frozen=True, slots=True)
class Waiting:
    """Condition matching while the session is parked on out-of-band work.

    True when an in-flight background or async tool has not yet reported back: a
    ``Workflow`` or async sub-agent awaiting its completion ``<task-notification>``
    (tracked across turns, so a launch in an earlier turn still counts), a
    ``run_in_background`` Bash/Agent/Task, or a user-facing wait such as
    ``ScheduleWakeup``/``Monitor``. Blocking Stop gates built with ``gate``/``llm_gate``
    skip on it automatically when no ``skip_if`` is given, so the agent isn't nagged
    for pausing on work it is correctly waiting on.

    Example:
        >>> gate("Run tests before stopping", skip_if=[Waiting()])
    """

    pass


@dataclass(frozen=True)
class ConditionList:
    """Container for a tuple of conditions; base class for combinators like ``Or``."""

    conditions: tuple[TCondition, ...]


class Or(ConditionList):
    """Match if any of the inner conditions matches."""

    def __init__(self, *conditions: TCondition) -> None:
        super().__init__(conditions=tuple(conditions))


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
    | SourceEdits
    | TouchedFile
    | RanCommand
    | InPlanMode
    | Waiting
    | Or
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

    @classmethod
    def of(cls, action: Action, message: str | None = None) -> HookResult:
        """Build a ``HookResult``, dedenting and stripping ``message`` for readable triple-quoted handler returns."""
        return cls(action=action, message=dedent(message).strip() if message else None)


from captain_hook.testing.types import InlineTests as InlineTests  # noqa: E402


@dataclass(frozen=True, kw_only=True)
class HookSpec:
    events: Event
    only_if: tuple[TCondition, ...] = ()
    skip_if: tuple[TCondition, ...] = ()
    message: str | None = None
    block: bool = False
    respect_gitignore: bool = True
    max_fires: int | None = None
    tests: InlineTests | None = None
    async_: bool = False
    skip_planning_agents: bool = True


@dataclass(frozen=True, kw_only=True)
class RegisteredHook:
    """A registered hook pairing a ``HookSpec`` with an optional handler callable."""

    spec: HookSpec
    handler: Callable[[BaseHookEvent], HookResult | None] | None = None
    name: str = ""
    source_file: str = ""
