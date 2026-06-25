from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Flag, StrEnum, auto
from textwrap import dedent
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    Protocol,
    TypeVar,
    get_args,
    get_origin,
    runtime_checkable,
)

if TYPE_CHECKING:
    from cc_transcript.tools import ToolCallBase

    from captain_hook.command import CommandLine
    from captain_hook.events import BaseHookEvent
    from captain_hook.signals.nlp import NlpSignal

T = TypeVar("T", bound="ToolCallBase")


LANG_GLOBS: dict[str, tuple[str, ...]] = {
    "py": ("*.py", "*.pyi"),
    "ts": ("*.ts",),
    "tsx": ("*.tsx",),
    "js": ("*.js", "*.mjs", "*.cjs"),
    "jsx": ("*.jsx",),
    "go": ("*.go",),
    "rs": ("*.rs",),
    "java": ("*.java",),
    "bash": ("*.sh", "*.bash"),
}


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
    SessionEnd = auto()

    @property
    def event_class(self) -> type[BaseHookEvent]:
        from captain_hook.events import (
            NotificationEvent,
            PostToolUseEvent,
            PostToolUseFailureEvent,
            PreCompactEvent,
            PreToolUseEvent,
            SessionEndEvent,
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
            Event.SessionEnd: SessionEndEvent,
        }
        if cls := mapping.get(self):
            return cls
        raise ValueError(f"event_class requires a single-member flag, got {self!r}")


class Action(StrEnum):
    """Hook result action determining how the hook output is handled.

    - ``block``: Prevents the tool use or stops the agent.
    - ``warn``: Adds advisory context without blocking.
    - ``allow``: Explicitly permits the action.
    - ``rewrite``: Replaces a ``PreToolUse`` tool's input with ``updated_input`` and allows it.
    """

    block = "block"
    warn = "warn"
    allow = "allow"
    rewrite = "rewrite"


@dataclass(frozen=True, slots=True)
class Tool:
    """Condition matching the current event's tool name against a regex pattern.

    Use in ``only_if`` or ``skip_if`` to filter hooks by which tool is being used.

    Example:
        >>> hook(Event.PreToolUse, only_if=[Tool("Bash|Execute")], message="...", block=True)
    """

    pattern: str


@dataclass(frozen=True, slots=True)
class PatternsCondition:
    patterns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FilePath(PatternsCondition):
    """Condition matching the current event's file path against glob patterns.

    Accepts one or more glob patterns as positional arguments.

    Example:
        >>> hook(Event.PostToolUse, only_if=[FilePath("*.py", "*.pyi")], message="Python file edited")
    """

    project_only: bool

    def __init__(self, *patterns: str, project_only: bool = True) -> None:
        object.__setattr__(self, "patterns", patterns)
        object.__setattr__(self, "project_only", project_only)


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

    Args:
        pattern: A regular expression searched against the content with ``re.MULTILINE``.
        project_only: Only fire on files inside the repository root (default ``True``); set
            ``False`` to also match scratch files, attachments, and other paths outside the repo.
    """

    pattern: str
    project_only: bool = True


@dataclass(frozen=True, slots=True)
class Pattern:
    """Condition matching the edit's new content against an ast-grep **structural** pattern.

    Where [`Content`][captain_hook.types.Content] matches text with a regex, ``Pattern`` matches
    code *shape* across languages — ``print($$$)`` matches a print call however its arguments are
    spelled, ignoring matches inside strings or comments.

    Args:
        pattern: An ast-grep pattern string with metavariables, e.g. ``"os.system($CMD)"`` or
            ``"print($$$)"``.
        lang: ast-grep language id (the short keys of [`LANG_GLOBS`][captain_hook.types.LANG_GLOBS],
            e.g. ``"py"``, ``"go"``); inferred from the edited file's extension when omitted.
        project_only: Only fire on files inside the repository root (default ``True``), like
            [`Content`][captain_hook.types.Content].

    Example:
        >>> hook(Event.PreToolUse, only_if=[Pattern("eval($$$)")], message="no eval", block=True)
    """

    pattern: str
    lang: str | None = None
    project_only: bool = True


@dataclass(frozen=True, slots=True)
class UsedSkill:
    """Transcript-history condition: true when a Skill tool use with a matching name exists."""

    name: str
    subagents: bool = True


@dataclass(frozen=True, slots=True)
class ReadFile(PatternsCondition):
    """Transcript-history condition: true when a Read tool use targeted a matching file.

    Accepts one or more glob patterns as positional arguments.
    """

    subagents: bool

    def __init__(self, *patterns: str, subagents: bool = True) -> None:
        object.__setattr__(self, "patterns", patterns)
        object.__setattr__(self, "subagents", subagents)


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


@dataclass(frozen=True, slots=True)
class TouchedFile(PatternsCondition):
    """Transcript-history condition: true when an Edit/Write targeted a file matching the glob.

    Accepts one or more glob patterns as positional arguments.
    """

    subagents: bool

    def __init__(self, *patterns: str, subagents: bool = False) -> None:
        object.__setattr__(self, "patterns", patterns)
        object.__setattr__(self, "subagents", subagents)


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


class CustomInputTypeCondition(CustomCondition, Generic[T]):  # noqa: UP046
    """CustomCondition that fires only for a specific typed tool call.

    Parameterize with the tool call type and implement ``check_input``; ``check``
    narrows the event's input to that type and skips events of any other tool.

    Example:
        >>> class BigRead(CustomInputTypeCondition[ReadCall]):
        ...     def check_input(self, evt: BaseHookEvent, call: ReadCall) -> bool:
        ...         return call.limit is not None and call.limit > 1000
    """

    _call_type: type[T]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for base in getattr(cls, "__orig_bases__", ()):
            if get_origin(base) is CustomInputTypeCondition:
                (cls._call_type,) = get_args(base)
                return
        raise TypeError(f"{cls.__name__} must parameterize CustomInputTypeCondition with a tool call type")

    def check(self, evt: BaseHookEvent) -> bool:
        call = evt.as_input(self._call_type)
        return call is not None and self.check_input(evt, call)

    def check_input(self, evt: BaseHookEvent, call: T) -> bool:
        raise NotImplementedError


class CustomCommandLineCondition(CustomCondition):
    """CustomCondition that fires only when a parsed Bash command line is present.

    Implement ``check_command_line`` to match against the structured command line;
    ``check`` returns False for events without one (non-Bash tools).
    """

    def check(self, evt: BaseHookEvent) -> bool:
        if not (cl := evt.command_line):
            return False
        return self.check_command_line(evt, cl)

    def check_command_line(self, evt: BaseHookEvent, cl: CommandLine) -> bool:
        raise NotImplementedError


TCondition = (
    Tool
    | FilePath
    | Command
    | Content
    | Pattern
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
    """The return value from a hook handler, specifying the action and optional message.

    ``updated_input`` and ``note`` carry the ``rewrite`` action's replacement tool
    input and the advisory context surfaced alongside it.
    """

    action: Action
    message: str | None = None
    updated_input: dict[str, Any] | None = None
    note: str | None = None

    @classmethod
    def of(cls, action: Action, message: str | None = None) -> HookResult:
        """Build a ``HookResult``, dedenting and stripping ``message`` for readable triple-quoted handler returns."""
        return cls(action=action, message=dedent(message).strip() if message is not None else None)


type HookResponse = HookResult | None
"""Return type of a hook handler: a :class:`HookResult`, or None to take no action."""


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
