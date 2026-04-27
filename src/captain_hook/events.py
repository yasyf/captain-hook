from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from captain_hook.file import File
from captain_hook.transcript.inputs import (
    AgentInput,
    BashInput,
    EditInput,
    FileInputBase,
    ToolInput,
    WriteInput,
    parse_tool_input,
)
from captain_hook.types import Event

if TYPE_CHECKING:
    from captain_hook.command import CommandLine as CommandLineType
    from captain_hook.context import HookContext
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
    def user_prompt(self) -> str | None:
        return self._raw.get("prompt")

    @property
    def stop_hook_active(self) -> bool:
        return self._raw.get("stop_hook_active", False)

    @property
    def transcript_path(self) -> Path | None:
        return Path(p) if (p := self._raw.get("transcript_path")) else None

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
    def input(self) -> ToolInput:
        return parse_tool_input(self.tool_name or "", self._tool_input)

    @property
    def command(self) -> str | None:
        return self.input.command if isinstance(self.input, BashInput) else None

    @cached_property
    def command_line(self) -> CommandLineType | None:
        return None

    @property
    def file(self) -> File | None:
        return self.input.file if isinstance(self.input, FileInputBase) else None

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

        return HR(action=Action.allow)

    def warn(self, message: str) -> HookResult:
        from captain_hook.types import Action
        from captain_hook.types import HookResult as HR

        return HR(action=Action.warn, message=message)

    def block(self, message: str) -> HookResult:
        from captain_hook.types import Action
        from captain_hook.types import HookResult as HR

        return HR(action=Action.block, message=message)


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
            case EditInput(new=new):
                return new
            case WriteInput(content=c):
                return c
            case _:
                return None

    @property
    def old(self) -> str | None:
        return self.input.old if isinstance(self.input, EditInput) else None

    @property
    def agent_type(self) -> str | None:
        return ti.agent_type if isinstance(ti := self.input, AgentInput) else None

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
