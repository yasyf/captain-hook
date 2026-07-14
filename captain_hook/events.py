from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from cc_transcript.tools import (
    BashCall,
    EditCall,
    MultiEditCall,
    NotebookEditCall,
    TaskCall,
    ToolCallBase,
    WriteCall,
    file_path_of,
    parse_tool_call,
)

from captain_hook.file import File
from captain_hook.types import Event

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from cc_transcript.command import CommandLine as CommandLineType
    from cc_transcript.ids import ToolDigest
    from cc_transcript.tools import ToolCall

    from captain_hook.context import HookContext
    from captain_hook.tasks import Tasks
    from captain_hook.types import HookResult

T = TypeVar("T", bound=ToolCallBase)


def disk_pre_image(path: Path) -> str | None:
    if not path.exists():
        return ""
    if not path.is_file():
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data.decode(errors="replace")


def apply_edit(image: str | None, old: str, new: str, replace_all: bool) -> str | None:
    """Apply one ``old``→``new`` replacement to ``image``, mirroring the tool's own preconditions.

    ``None`` when ``old`` is absent, or — without ``replace_all`` — ambiguous (more than one match),
    since the real Edit tool rejects the call rather than editing the first occurrence.
    """
    if image is None:
        return None
    count = image.count(old)
    if count == 0 or (not replace_all and count != 1):
        return None
    return image.replace(old, new) if replace_all else image.replace(old, new, 1)


@dataclass(frozen=True, slots=True)
class BackgroundTask:
    """A background task keeping the session alive, from a ``Stop``/``SubagentStop`` payload.

    Since Claude Code v2.1.145 a stopping session reports the work still running so a
    hook can tell "the session is done" from "the session is paused waiting for
    background work to wake it back up". Each task carries the common fields below
    plus type-specific extras — a ``shell`` task's ``command``, a ``subagent``'s
    ``agent_type``, an ``MCP task``'s ``server`` and ``tool`` — preserved in ``raw``.

    Attributes:
        id: The task's stable identifier.
        type: The task kind, one of ``shell``, ``subagent``, ``monitor``,
            ``workflow``, ``teammate``, ``cloud session``, or ``MCP task``.
        status: The task's reported lifecycle status.
        description: A human-readable summary of the task.
        raw: The full task mapping, including the type-specific fields.
    """

    id: str
    type: str
    status: str
    description: str
    raw: Mapping[str, Any]

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> BackgroundTask:
        return cls(
            id=str(raw.get("id", "")),
            type=str(raw.get("type", "")),
            status=str(raw.get("status", "")),
            description=str(raw.get("description", "")),
            raw=raw,
        )


@dataclass(frozen=True, slots=True)
class SessionCron:
    """A scheduled prompt registered on the session, from a ``Stop``/``SubagentStop`` payload.

    A cron keeps a session alive to re-run ``prompt`` on ``schedule``; its presence
    means the session is paused for future scheduled work rather than finished.

    Attributes:
        id: The cron's stable identifier.
        schedule: The cron schedule expression.
        recurring: Whether the cron fires repeatedly rather than once.
        prompt: The prompt replayed to the agent when the cron fires.
    """

    id: str
    schedule: str
    recurring: bool
    prompt: str

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> SessionCron:
        return cls(
            id=str(raw.get("id", "")),
            schedule=str(raw.get("schedule", "")),
            recurring=bool(raw.get("recurring", False)),
            prompt=str(raw.get("prompt", "")),
        )


@dataclass
class BaseHookEvent:
    """Base class for all hook events, providing access to raw payload, context, and convenience methods."""

    event_name: ClassVar[Event]

    _raw: dict[str, Any]
    ctx: HookContext

    @property
    def event(self) -> Event:
        return self.event_name

    @property
    def agent_id(self) -> str | None:
        """The subagent id attached to this event, or None for main-agent events.

        A missing, null, or empty ``agent_id`` all mean the main agent.
        """
        return self._raw.get("agent_id") or None

    @property
    def is_subagent(self) -> bool:
        """Whether this event came from a subagent payload."""
        return self.agent_id is not None

    @property
    def session_id(self) -> str:
        return self._raw["session_id"]

    @cached_property
    def tasks(self) -> Tasks:
        """The live task list for this session, read from Claude Code's native task store.

        Unlike transcript-derived ``task_ops()``, this reflects updates made by
        subagents, teammates, or resumed sessions, and is empty when the session
        has no task store — it never falls back to another session's tasks.
        """
        from captain_hook.tasks import Tasks as T

        return T.for_session(self.session_id)

    @cached_property
    def skip_permissions(self) -> bool:
        """Whether the session's ``claude`` process was launched with permission bypass available.

        Walks the process tree to the nearest ``claude`` ancestor and reports whether that
        process was started with ``--dangerously-skip-permissions`` or
        ``--allow-dangerously-skip-permissions``.
        """
        from captain_hook.util.proc import claude_skip_permissions

        return claude_skip_permissions()

    @property
    def user_prompt(self) -> str | None:
        return self._raw.get("prompt")

    @property
    def stop_hook_active(self) -> bool:
        return self._raw.get("stop_hook_active", False)

    @property
    def background_tasks(self) -> tuple[BackgroundTask, ...]:
        """The background tasks keeping this session alive, ``()`` when the payload omits them.

        Populated only on ``Stop`` and ``SubagentStop`` events from Claude Code
        v2.1.145+; a non-empty tuple means the session is paused waiting for
        background work to wake it, not done.
        """
        return tuple(BackgroundTask.from_raw(t) for t in self._raw.get("background_tasks") or ())

    @property
    def session_crons(self) -> tuple[SessionCron, ...]:
        """The scheduled prompts registered on this session, ``()`` when the payload omits them.

        Populated only on ``Stop`` and ``SubagentStop`` events from Claude Code
        v2.1.145+; a non-empty tuple means the session is paused for future
        scheduled work.
        """
        return tuple(SessionCron.from_raw(c) for c in self._raw.get("session_crons") or ())

    @property
    def transcript_path(self) -> Path | None:
        return Path(p) if (p := self._raw.get("transcript_path")) else None

    @property
    def cwd(self) -> Path | None:
        return Path(p) if (p := self._raw.get("cwd")) else None

    @property
    def permission_mode(self) -> str | None:
        return self._raw.get("permission_mode")

    @property
    def parent_agent_type(self) -> str | None:
        return self._raw.get("agent_type")

    @property
    def tool_name(self) -> str | None:
        return None

    @property
    def _tool_input(self) -> dict[str, Any]:
        return {}

    @cached_property
    def input(self) -> ToolCall:
        return parse_tool_call(self.tool_name or "", self._tool_input, on_error="other")

    def as_input(self, call_type: type[T]) -> T | None:
        """Return ``input`` narrowed to ``call_type``, or None when it is a different tool call."""
        return self.input if isinstance(self.input, call_type) else None

    @property
    def tool_digest(self) -> ToolDigest | None:
        return self.input.digest if self.tool_name else None

    @property
    def command(self) -> str | None:
        return self.input.command if isinstance(self.input, BashCall) else None

    @property
    def command_line(self) -> CommandLineType | None:
        return None

    @property
    def file(self) -> File | None:
        return File(path=Path(p)) if (p := file_path_of(self.input)) else None

    @property
    def content(self) -> str | None:
        return None

    @property
    def old(self) -> str | None:
        return None

    @property
    def replaced(self) -> str | None:
        return None

    @property
    def pre_image(self) -> str | None:
        return None

    @property
    def post_image(self) -> str | None:
        return None

    @property
    def agent_type(self) -> str | None:
        return None

    def command_matches(self, *patterns: str) -> bool:
        return False

    def file_matches(self, *globs: str) -> bool:
        return False

    def content_matches(self, pattern: str) -> bool:
        return False

    def allow(self) -> HookResult:
        from captain_hook.types import Action
        from captain_hook.types import HookResult as HR

        return HR.of(Action.allow)

    @staticmethod
    def _render_part(part: str | tuple[str, object] | object) -> str:
        match part:
            case str():
                return part
            case (str() as label, value):
                return f"{label}: " + json.dumps(value, default=str)
            case _:
                return json.dumps(part, default=str)

    def warn(self, *parts: str | tuple[str, object] | object) -> HookResult:
        r"""Emit a warning whose parts are auto-rendered and joined with newlines.

        Each part is rendered by form: a plain ``str`` passes through verbatim; a
        ``(label, value)`` tuple becomes ``"{label}: {json}"`` with ``value``
        JSON-encoded; any other object is JSON-encoded directly. Rendered parts are
        joined with ``"\n"``.

        Args:
            *parts: Warning fragments, each a ``str``, a ``(label, value)`` tuple, or
                any JSON-serializable object.

        Returns:
            A warn :class:`HookResult` carrying the joined message.
        """
        from captain_hook.types import Action
        from captain_hook.types import HookResult as HR

        return HR.of(Action.warn, "\n".join(self._render_part(p) for p in parts))

    def block(self, message: str) -> HookResult:
        from captain_hook.types import Action
        from captain_hook.types import HookResult as HR

        return HR.of(Action.block, message)


@dataclass
class ToolHookEvent(BaseHookEvent):
    """Event for tool-related hooks, adding tool name, input, command, and file access."""

    @property
    def tool_name(self) -> str | None:
        return self._raw.get("tool_name")

    @property
    def _tool_input(self) -> dict[str, Any]:
        return self._raw.get("tool_input", {})

    @property
    def command_line(self) -> CommandLineType | None:
        from cc_transcript.command import parse_command_line

        return parse_command_line(cmd) if (cmd := self.command) else None

    @property
    def content(self) -> str | None:
        match self.input:
            case EditCall(new=new):
                return new
            case WriteCall(content=content):
                return content
            case MultiEditCall(edits=edits):
                return "\n".join(span.new for span in edits)
            case NotebookEditCall(new_source=new_source):
                return new_source
            case _:
                return None

    @property
    def old(self) -> str | None:
        match self.input:
            case EditCall(old=old):
                return old
            case MultiEditCall(edits=edits):
                return "\n".join(span.old for span in edits)
            case _:
                return None

    @property
    def replaced(self) -> str | None:
        """The pre-image of the text this call overwrites, when knowable.

        An Edit's ``old`` and a MultiEdit's newline-joined olds carry the pre-image at
        any event. A Write's pre-image is the file's current on-disk content — ``""``
        for a new file, lossily decoded for non-UTF-8 bytes, ``None`` for a directory
        or unreadable path — and only at ``PreToolUse``: once the Write lands, disk
        already holds the new text, so any other event yields ``None``. ``None`` for
        other tools.
        """
        match self.input:
            case EditCall() | MultiEditCall():
                return self.old
            case WriteCall(file_path=path) if self.event is Event.PreToolUse:
                return disk_pre_image(Path(path))
            case _:
                return None

    @cached_property
    def pre_image(self) -> str | None:
        """The full pre-edit file image for an Edit/MultiEdit/Write, read from disk at PreToolUse.

        ``None`` off ``PreToolUse`` — disk already holds the post-edit text — and for other tools.
        Unlike :attr:`replaced` (an Edit's ``old`` fragment), this is always the whole file, so a
        comment run's full extent is visible even when the edit touched only part of it.
        """
        if self.event is not Event.PreToolUse:
            return None
        match self.input:
            case EditCall(file_path=path) | MultiEditCall(file_path=path) | WriteCall(file_path=path):
                return disk_pre_image(Path(path))
            case _:
                return None

    @cached_property
    def post_image(self) -> str | None:
        """The full post-edit file image an Edit/MultiEdit/Write would produce, at PreToolUse.

        A Write is its ``content``; an Edit applies ``old``→``new`` to :attr:`pre_image` (once, or
        every occurrence when ``replace_all``); a MultiEdit folds its spans in order. ``None`` when a
        span's ``old`` is absent from the running image (the tool call would fail), off ``PreToolUse``,
        or for other tools.
        """
        if self.event is not Event.PreToolUse:
            return None
        match self.input:
            case WriteCall(content=content):
                return content
            case EditCall(old=old, new=new, replace_all=replace_all):
                return apply_edit(self.pre_image, old, new, replace_all)
            case MultiEditCall(edits=edits):
                image = self.pre_image
                for span in edits:
                    image = apply_edit(image, span.old, span.new, span.replace_all)
                return image
            case _:
                return None

    @property
    def agent_type(self) -> str | None:
        return call.agent_type if isinstance(call := self.input, TaskCall) else None

    def command_matches(self, *patterns: str) -> bool:
        if not (cl := self.command_line) or (cmd := cl.primary) is None:
            return False
        return any(cmd.matches(p) for p in patterns)

    def file_matches(self, *globs: str) -> bool:
        return bool(self.file) and self.file.matches(*globs)

    def content_matches(self, pattern: str) -> bool:
        return bool(self.content) and bool(re.search(pattern, self.content, re.MULTILINE))


@dataclass
class ToolRewriteEvent(ToolHookEvent):
    """Tool event whose input can be rewritten before it runs.

    Base for the events where Claude Code accepts an ``updatedInput`` decision
    (``PreToolUse`` and ``PermissionRequest``), adding the ``rewrite*`` helpers.
    """

    def rewrite(self, updated_input: dict[str, Any], *, note: str | None = None) -> HookResult:
        """Allow the tool but replace its input with ``updated_input`` (Claude Code's ``updatedInput``).

        ``updated_input`` must satisfy the same tool's schema; ``note`` surfaces as
        ``additionalContext`` so the model sees that its input was rewritten.
        """
        from captain_hook.types import Action
        from captain_hook.types import HookResult as HR

        return HR(action=Action.rewrite, updated_input=updated_input, note=note)

    def rewrite_command(self, new_command: str, *, note: str | None = None) -> HookResult:
        """Rewrite a Bash command in place, replacing ``command`` while preserving the rest of the tool input."""
        return self.rewrite({**self._tool_input, "command": new_command}, note=note)

    def rewrite_content(self, transform: Callable[[str], str], *, note: str | None = None) -> HookResult | None:
        """Rewrite each editable-content field through ``transform``, allowing the tool with the result.

        Maps the transform onto whichever field holds new source for this tool — an Edit's
        ``new_string``, a Write's ``content``, a NotebookEdit's ``new_source``, or every span of a
        MultiEdit's ``edits`` — and preserves the rest of the input. Returns None (a no-op) when
        ``transform`` leaves every field unchanged, so callers stay idempotent.
        """
        base = self._tool_input
        match self.input:
            case EditCall():
                updated = base | {"new_string": transform(base["new_string"])}
            case WriteCall():
                updated = base | {"content": transform(base["content"])}
            case NotebookEditCall():
                updated = base | {"new_source": transform(base["new_source"])}
            case MultiEditCall():
                updated = base | {"edits": [e | {"new_string": transform(e["new_string"])} for e in base["edits"]]}
            case _:
                return None
        return self.rewrite(updated, note=note) if updated != base else None


@dataclass
class PreToolUseEvent(ToolRewriteEvent):
    """Fires before a tool is executed. Return a block result to prevent execution."""

    event_name: ClassVar[Event] = Event.PreToolUse


@dataclass
class PermissionRequestEvent(ToolRewriteEvent):
    """Fires when a permission dialog would be shown.

    ``allow``/``block``/``rewrite`` answer the dialog (block maps to a deny with the
    message shown to the user); ``None`` and ``warn`` fall through, so the dialog
    shows normally.
    """

    event_name: ClassVar[Event] = Event.PermissionRequest

    @property
    def permission_suggestions(self) -> list[dict[str, Any]]:
        return self._raw.get("permission_suggestions", [])

    @property
    def agent_type(self) -> str | None:
        return self._raw.get("agent_type")


@dataclass
class PostToolUseEvent(ToolHookEvent):
    """Fires after a tool completes successfully, with access to the tool response."""

    event_name: ClassVar[Event] = Event.PostToolUse

    @property
    def tool_response(self) -> str | None:
        return self._raw.get("tool_response")


@dataclass
class PostToolUseFailureEvent(ToolHookEvent):
    """Fires after a tool fails, providing the error message and interrupt status."""

    event_name: ClassVar[Event] = Event.PostToolUseFailure

    @property
    def error(self) -> str:
        return self._raw["error"]

    @property
    def is_interrupt(self) -> bool:
        return self._raw.get("is_interrupt", False)


@dataclass
class UserPromptSubmitEvent(BaseHookEvent):
    """Fires when the user submits a prompt, before the agent processes it."""

    event_name: ClassVar[Event] = Event.UserPromptSubmit


@dataclass
class StopEvent(BaseHookEvent):
    """Fires when the agent is about to stop. Return a block result to prevent stopping."""

    event_name: ClassVar[Event] = Event.Stop


@dataclass
class SubagentStopEvent(BaseHookEvent):
    """Fires when a subagent finishes. Provides ``agent_type`` for filtering."""

    event_name: ClassVar[Event] = Event.SubagentStop

    @property
    def agent_type(self) -> str | None:
        return self._raw.get("agent_type") or self._raw.get("tool_input", {}).get("subagent_type")


@dataclass
class SubagentStartEvent(BaseHookEvent):
    """Fires when a subagent is launched. Provides ``agent_type`` for filtering."""

    event_name: ClassVar[Event] = Event.SubagentStart

    @property
    def agent_type(self) -> str | None:
        return self._raw.get("agent_type") or self._raw.get("tool_input", {}).get("subagent_type")


@dataclass
class PreCompactEvent(BaseHookEvent):
    """Fires before context compaction, providing the trigger and custom instructions."""

    event_name: ClassVar[Event] = Event.PreCompact

    @property
    def trigger(self) -> str | None:
        return self._raw.get("trigger")

    @property
    def custom_instructions(self) -> str | None:
        return self._raw.get("custom_instructions")


@dataclass
class NotificationEvent(BaseHookEvent):
    """Fires on system notifications, providing message, title, and notification type."""

    event_name: ClassVar[Event] = Event.Notification

    @property
    def message(self) -> str | None:
        return self._raw.get("message")

    @property
    def title(self) -> str | None:
        return self._raw.get("title")

    @property
    def notification_type(self) -> str | None:
        return self._raw.get("notification_type")


@dataclass
class SessionStartEvent(BaseHookEvent):
    """Fires when a session starts, providing what triggered it. Warns inject context; it cannot block.

    ``source`` is one of ``"startup"``, ``"resume"``, ``"clear"``, or ``"compact"``.
    """

    event_name: ClassVar[Event] = Event.SessionStart

    @property
    def source(self) -> str:
        return self._raw["source"]


@dataclass
class SessionEndEvent(BaseHookEvent):
    """Fires when the session ends, providing the termination reason. Output is ignored, so it cannot block."""

    event_name: ClassVar[Event] = Event.SessionEnd

    @property
    def reason(self) -> str:
        return self._raw["reason"]
