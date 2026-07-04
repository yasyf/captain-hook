from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Flag, StrEnum, auto
from textwrap import dedent
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    Literal,
    Protocol,
    TypeVar,
    get_args,
    get_origin,
    runtime_checkable,
)

if TYPE_CHECKING:
    from cc_transcript.command import CommandLine
    from cc_transcript.tools import ToolCallBase

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


def _split_names(names: Sequence[str]) -> tuple[str, ...]:
    """Flatten any embedded ``|`` alternations so ``("Edit|Write",)`` becomes ``("Edit", "Write")``."""
    return tuple(part for name in names for part in name.split("|"))


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
    SessionStart = auto()
    SessionEnd = auto()
    PermissionRequest = auto()

    @property
    def event_class(self) -> type[BaseHookEvent]:
        from captain_hook.events import (
            NotificationEvent,
            PermissionRequestEvent,
            PostToolUseEvent,
            PostToolUseFailureEvent,
            PreCompactEvent,
            PreToolUseEvent,
            SessionEndEvent,
            SessionStartEvent,
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
            Event.SessionStart: SessionStartEvent,
            Event.SessionEnd: SessionEndEvent,
            Event.PermissionRequest: PermissionRequestEvent,
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
    """Condition matching the current event's tool name against one or more names.

    Matching is exact membership, honoring cross-editor aliases (``Bash``/``Execute``,
    ``Write``/``Create``, …) and MCP suffixes (``mcp__github__Grep`` matches ``Grep``) —
    NOT a regex, so ``Tool("Edit.*")`` never matches. Pass names variadically, or as a
    single ``|``-joined string for back-compat (``Tool("Bash|Execute")`` is
    ``Tool("Bash", "Execute")``).

    Example:
        >>> hook(Event.PreToolUse, only_if=[Tool("Bash", "Execute")], message="...", block=True)
    """

    names: tuple[str, ...]

    def __init__(self, *names: str) -> None:
        object.__setattr__(self, "names", _split_names(names))


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

    Searched against the raw command line *and* each parsed command's argv join, so a
    pattern spanning pipes/operators/redirects (``curl ... | sh``) matches. Only relevant
    for ``PreToolUse`` events targeting the Bash/Execute tool. The regex is compiled at
    construction, so a malformed pattern raises immediately rather than at dispatch.

    Example:
        >>> hook(Event.PreToolUse, only_if=[Command(r"git\\s+stash")], message="blocked", block=True)
    """

    pattern: str

    def __post_init__(self) -> None:
        re.compile(self.pattern)


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

    def __post_init__(self) -> None:
        re.compile(self.pattern)


@dataclass(frozen=True, slots=True)
class ToolInput:
    """Condition matching top-level fields of the raw tool input against regexes.

    Two spellings, both ANDing when more than one field is given:

    - ``ToolInput(model=r"haiku", subagent_type="Explore")`` — kwargs, one regex per field.
    - ``ToolInput("run-in-background", r"true")`` — a single ``(field, pattern)`` positional
      pair, for keys that are not valid Python identifiers.

    Each field's value is searched with ``re.MULTILINE``. Scalar (bool/int/float) values are
    coerced to text first, so ``ToolInput(run_in_background="true")`` matches a JSON ``true``.
    Works for any tool; false for non-tool events or a missing field. Patterns are compiled at
    construction.

    Example:
        >>> hook(Event.PreToolUse, only_if=[ToolInput(model=r"(?i)\\bhaiku\\b")], message="...", block=True)
    """

    fields: tuple[tuple[str, str], ...]

    def __init__(self, *positional: str, **fields: str) -> None:
        match positional:
            case ():
                pairs = tuple(fields.items())
            case (field, pattern) if not fields:
                pairs = ((field, pattern),)
            case _:
                raise TypeError(
                    "ToolInput takes either a single (field, pattern) positional pair or field=regex kwargs"
                )
        if not pairs:
            raise ValueError("ToolInput requires at least one field to match")
        for _, pattern in pairs:
            re.compile(pattern)
        object.__setattr__(self, "fields", tuple(sorted(pairs)))


@dataclass(frozen=True, slots=True, init=False)
class WorkflowScript:
    """Condition matching a ``Workflow`` tool's script source.

    Each ``**opts`` kwarg names an ``agent()`` opts key and carries a regex ``re.search``ed
    against every value pinned for that key anywhere in the script source —
    ``WorkflowScript(model="haiku")``, ``WorkflowScript(effort=r"^low$")``,
    ``WorkflowScript(agentType="code-reviewer")``, any future key. ``pattern`` (reserved, so
    not usable as an opt key) searches the raw source with ``re.MULTILINE``. Everything given
    ANDs: ``pattern`` (if any) must match and each opt key's regex must match at least one of
    that key's pins.

    Captured value forms: single-/double-quoted strings, a single-line template literal (raw
    text incl. ``${…}``), and a bare token (identifier, dotted path, number, boolean) matched
    by its source TEXT — so ``model: a.model`` matches ``WorkflowScript(model=r"a\\.model")``,
    not the runtime value. The scan is whole-source (comments and data tables feeding
    ``agent()`` count); anchor enum-like keys with ``^…$`` (each captured value is single-line).
    Matching is per-SCRIPT, not per-``agent()``-call. At least one of ``pattern``/``**opts`` is
    required. The source is the inline ``script``, or — when ``script`` is None and
    ``script_path`` points at a file — that file's contents; a missing/unreadable path or a
    file larger than ~1 MiB never matches and never raises. False for any non-Workflow tool.

    Example:
        >>> nudge("no haiku steps", only_if=[Tool("Workflow"), WorkflowScript(model="haiku")],
        ...       events=Event.PreToolUse)
    """

    pattern: str | None
    opts: tuple[tuple[str, str], ...]

    def __init__(self, pattern: str | None = None, **opts: str) -> None:
        if pattern is None and not opts:
            raise ValueError("WorkflowScript requires a pattern and/or at least one opts key to match")
        if pattern is not None:
            re.compile(pattern)
        for value in opts.values():
            re.compile(value)
        object.__setattr__(self, "pattern", pattern)
        object.__setattr__(self, "opts", tuple(sorted(opts.items())))


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
    """Transcript-history condition: true when a Skill tool use named one of ``names``.

    Pass names variadically; a single ``|``-joined string still works for back-compat. A bare
    name also matches the plugin-qualified spelling, so ``UsedSkill("codex")`` matches a skill
    reported as ``codex:codex`` and ``UsedSkill("slop-cop-check")`` matches ``slop-cop:slop-cop-check``.

    Example:
        >>> nudge("run codex first", skip_if=[UsedSkill("codex")])
    """

    names: tuple[str, ...]
    subagents: bool = True

    def __init__(self, *names: str, subagents: bool = True) -> None:
        object.__setattr__(self, "names", _split_names(names))
        object.__setattr__(self, "subagents", subagents)


@dataclass(frozen=True, slots=True)
class ReadFile(PatternsCondition):
    """Transcript-history condition: true when a Read tool use targeted a matching file.

    Accepts one or more ``fnmatch`` glob patterns as positional arguments, matched against each
    Read's full path and its basename — so ``ReadFile("*.md")`` matches by extension and a bare
    ``ReadFile("TESTING.md")`` matches that file in any directory. Reads are recorded as absolute
    paths, so anchor a directory match with a leading ``**/`` (``ReadFile("**/docs/**")``), the
    same idiom the test-file globs use.
    """

    subagents: bool

    def __init__(self, *patterns: str, subagents: bool = True) -> None:
        object.__setattr__(self, "patterns", patterns)
        object.__setattr__(self, "subagents", subagents)


@dataclass(frozen=True, slots=True)
class TestFile:
    """Condition that matches when the current event targets a test file.

    A test file is ``test_*.py``, ``conftest.py``, or any ``.py`` under a ``tests/`` directory.
    """

    __test__ = False

    project_only: bool = True


@dataclass(frozen=True, slots=True)
class SourceEdits:
    """Condition matching an ``Edit``/``Write`` of a non-test source file in one language.

    True when the current event edits a file matching the language's globs and — unless
    ``include_tests`` — is not a test file, optionally narrowed to ``paths``.

    Args:
        lang: Language key into [`LANG_GLOBS`][captain_hook.types.LANG_GLOBS] (default ``"py"``);
            unknown keys fall back to ``*.<lang>``.
        include_tests: Include test files too (default ``False`` excludes them).
        paths: One or more ``fnmatch`` globs the file must also match (e.g. ``("src/**", "lib/**")``);
            ``None`` (default) imposes no path restriction.

    Example:
        >>> hook(Event.PostToolUse, only_if=[SourceEdits(lang="ts", paths=("src/**", "lib/**"))], message="...")
    """

    lang: str
    include_tests: bool
    paths: tuple[str, ...]

    def __init__(self, lang: str = "py", include_tests: bool = False, paths: str | Sequence[str] | None = None) -> None:
        object.__setattr__(self, "lang", lang)
        object.__setattr__(self, "include_tests", include_tests)
        object.__setattr__(self, "paths", (paths,) if isinstance(paths, str) else tuple(paths or ()))

    @property
    def globs(self) -> tuple[str, ...]:
        return LANG_GLOBS.get(self.lang, (f"*.{self.lang}",))


@dataclass(frozen=True, slots=True)
class Agent:
    """Condition matching the current event's subagent type against one or more names.

    Exact membership (not a regex). Pass names variadically, or as a single ``|``-joined string
    for back-compat (``Agent("Explore|claude-code-guide")`` is ``Agent("Explore", "claude-code-guide")``).
    """

    names: tuple[str, ...]

    def __init__(self, *names: str) -> None:
        object.__setattr__(self, "names", _split_names(names))


@dataclass(frozen=True, slots=True)
class FromSubagent:
    """Condition matching when the current event originates from a subagent or teammate.

    True when the event payload carries an ``agent_id``, which Claude Code sends only for
    subagent and teammate events. Distinct from :class:`Agent`, which matches the subagent
    *type*: this matches the event's *origin*.

    Example:
        >>> approve("teammate bash", only_if=[Tool("Bash"), FromSubagent(), SkipPermissions()])
    """

    pass


@dataclass(frozen=True, slots=True)
class SkipPermissions:
    """Condition matching when the session was launched with permission bypass available.

    Walks the process tree to the nearest ``claude`` ancestor and matches when that one
    process was launched with ``--dangerously-skip-permissions`` **or**
    ``--allow-dangerously-skip-permissions`` — either spelling means the user made bypass
    *available* at launch, which counts as consent even while the active
    ``permission_mode`` is something else (e.g. ``plan``).

    Example:
        >>> approve("teammate bash", only_if=[Tool("Bash"), FromSubagent(), SkipPermissions()])
    """

    pass


@dataclass(frozen=True, slots=True)
class TouchedFile(PatternsCondition):
    """Transcript-history condition: true when an Edit/Write targeted a file matching the glob.

    Accepts one or more glob patterns as positional arguments. Defaults to the main agent's edits
    only (subagent edits rarely gate the main session); pass ``subagents=True`` to include sidechains.
    """

    subagents: bool

    def __init__(self, *patterns: str, subagents: bool = False) -> None:
        object.__setattr__(self, "patterns", patterns)
        object.__setattr__(self, "subagents", subagents)


@dataclass(frozen=True, slots=True)
class RanCommand:
    """Transcript-history condition: true when a Bash tool use running ``argv`` exists.

    ``argv`` is matched as a leading token prefix of a parsed command's argv via
    ``Session.has_command`` — wrapper-transparent (``sudo``/``env``/``timeout`` are
    stripped) and quote-exact, so ``RanCommand("git", "push")`` matches
    ``sudo git push -f`` but not ``echo "git push"``. Launchers are literal:
    ``RanCommand("pytest")`` does not match ``uv run pytest`` — list each spelling
    as its own entry.
    """

    argv: tuple[str, ...]
    subagents: bool

    def __init__(self, *argv: str, subagents: bool = True) -> None:
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "subagents", subagents)


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


@dataclass(frozen=True, slots=True)
class Runs:
    """Structural Bash condition: true when a parsed command's argv starts with ``argv``.

    Where [`Command`][captain_hook.types.Command] regex-matches the command text (and can false-fire
    on ``echo git stash``), ``Runs`` matches the argv prefix of any command in the line — so
    ``Runs("git", "stash")`` fires on ``git stash``, ``git stash pop``, and ``a && git stash`` but
    not ``echo git stash``. Only relevant for Bash/Execute events.

    Example:
        >>> hook(Event.PreToolUse, only_if=[Runs("git", "stash")], message="no stashing", block=True)
    """

    argv: tuple[str, ...]

    def __init__(self, *argv: str) -> None:
        object.__setattr__(self, "argv", argv)


@dataclass(frozen=True)
class ConditionList:
    """Container for a tuple of conditions; base class for combinators like ``Or``/``And``."""

    conditions: tuple[TCondition, ...]


class Or(ConditionList):
    """Match if any of the inner conditions matches."""

    def __init__(self, *conditions: TCondition) -> None:
        super().__init__(conditions=tuple(conditions))


class And(ConditionList):
    """Match only if every inner condition matches (useful nested inside ``Or``/``Not``)."""

    def __init__(self, *conditions: TCondition) -> None:
        super().__init__(conditions=tuple(conditions))


@dataclass(frozen=True, slots=True)
class Not:
    """Match only if the inner condition does not match."""

    condition: TCondition


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
    | ToolInput
    | WorkflowScript
    | Pattern
    | Agent
    | FromSubagent
    | SkipPermissions
    | UsedSkill
    | ReadFile
    | TestFile
    | SourceEdits
    | TouchedFile
    | RanCommand
    | Runs
    | InPlanMode
    | Waiting
    | Or
    | And
    | Not
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

    ``window`` bounds how much transcript each scoring pass reads: the last
    ``window`` events by default, or the whole current turn with the ``"turn"``
    sentinel. Use ``"turn"`` when the tell can sit hundreds of events before the
    turn ends — a fixed window at ``Stop`` only reaches the last few tool
    exchanges.
    """

    patterns: Sequence[Signal | NlpSignal]
    threshold: int
    window: int | Literal["turn"] = 15


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
