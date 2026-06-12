from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from cc_transcript.tools import (
    BashCall,
    EditCall,
    MultiEditCall,
    NotebookEditCall,
    TaskCall,
    WriteCall,
    file_path_of,
    parse_tool_call,
)

from captain_hook.file import File
from captain_hook.types import Event

if TYPE_CHECKING:
    from cc_transcript.ids import ToolDigest
    from cc_transcript.tools import ToolCall

    from captain_hook.command import CommandLine as CommandLineType
    from captain_hook.context import HookContext
    from captain_hook.tasks import Tasks
    from captain_hook.types import HookResult


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
    def is_subagent(self) -> bool:
        return "agent_id" in self._raw

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

    @property
    def user_prompt(self) -> str | None:
        return self._raw.get("prompt")

    @property
    def stop_hook_active(self) -> bool:
        return self._raw.get("stop_hook_active", False)

    @property
    def transcript_path(self) -> Path | None:
        return Path(p) if (p := self._raw.get("transcript_path")) else None

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

    @property
    def tool_digest(self) -> ToolDigest | None:
        return self.input.digest if self.tool_name else None

    @property
    def command(self) -> str | None:
        return self.input.command if isinstance(self.input, BashCall) else None

    @cached_property
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

    @cached_property
    def command_line(self) -> CommandLineType | None:
        from captain_hook.command import CommandLine

        return CommandLine.parse(cmd) if (cmd := self.command) else None

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
    def agent_type(self) -> str | None:
        return call.agent_type if isinstance(call := self.input, TaskCall) else None

    def command_matches(self, *patterns: str) -> bool:
        if not (cl := self.command_line):
            return False
        return any(cl.primary.matches(p) for p in patterns)

    def file_matches(self, *globs: str) -> bool:
        return bool(self.file) and self.file.matches(*globs)

    def content_matches(self, pattern: str) -> bool:
        return bool(self.content) and bool(re.search(pattern, self.content, re.MULTILINE))


@dataclass
class PreToolUseEvent(ToolHookEvent):
    """Fires before a tool is executed. Return a block result to prevent execution."""

    event_name: ClassVar[Event] = Event.PreToolUse


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
class SessionEndEvent(BaseHookEvent):
    """Fires when the session ends, providing the termination reason. Output is ignored, so it cannot block."""

    event_name: ClassVar[Event] = Event.SessionEnd

    @property
    def reason(self) -> str:
        return self._raw["reason"]
