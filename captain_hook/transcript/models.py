from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from captain_hook.command import CommandLine
    from captain_hook.file import File


@dataclass(frozen=True, kw_only=True)
class TextBlock:
    """A text content block from a transcript message."""

    text: str = ""


RawDict = dict[str, Any]
RawBlock = str | RawDict


@dataclass(frozen=True, kw_only=True)
class ToolUseBlock:
    """A tool-use content block from a transcript message, containing the tool name and raw input."""

    name: str
    input: RawDict = field(default_factory=lambda: {})
    id: str


@dataclass(frozen=True, kw_only=True)
class ToolResult:
    """A tool-result content block linking back to its tool use via ``tool_use_id``."""

    tool_use_id: str
    content: list[Any] | str = field(default_factory=lambda: [])
    is_error: bool = False
    is_async: bool = False


@dataclass(frozen=True, kw_only=True)
class TaskNotification:
    """A parsed ``<task-notification>`` payload from a queue-operation message."""

    tool_use_id: str

    @classmethod
    def from_content(cls, content: str) -> TaskNotification | None:
        m = re.search(r"<tool-use-id>([^<]+)</tool-use-id>", content)
        return cls(tool_use_id=m.group(1)) if m else None


ContentBlock = TextBlock | ToolUseBlock | ToolResult


def parse_content_block(b: RawBlock, *, is_async: bool = False) -> ContentBlock:
    match b:
        case str(s):
            return TextBlock(text=s)
        case dict() as raw:
            match raw.get("type"):
                case "tool_use":
                    return ToolUseBlock(
                        name=str(raw["name"]),
                        input=raw.get("input", {}),
                        id=str(raw["id"]),
                    )
                case "tool_result":
                    return ToolResult(
                        tool_use_id=str(raw["tool_use_id"]),
                        content=raw.get("content", []),
                        is_error=bool(raw.get("is_error", False)),
                        is_async=is_async,
                    )
                case _:
                    return TextBlock(text=str(raw.get("text", "")))


def parse_content(raw: list[RawBlock] | str, *, is_async: bool = False) -> list[ContentBlock]:
    match raw:
        case str(s):
            return [TextBlock(text=s)]
        case _:
            return [parse_content_block(b, is_async=is_async) for b in raw]


@dataclass(frozen=True, kw_only=True)
class ToolUse:
    """A transcript tool invocation with typed input parsing, file/command access, and result linkage."""

    name: str
    raw_input: RawDict = field(default_factory=lambda: {})
    id: str
    result: ToolResult | None = None
    message_index: int = -1

    @property
    def is_error(self) -> bool | None:
        return self.result.is_error if self.result else None

    @cached_property
    def input(self) -> Any:
        from captain_hook.transcript.inputs import parse_tool_input

        return parse_tool_input(self.name, self.raw_input)

    @cached_property
    def file(self) -> File | None:
        from captain_hook.transcript.inputs import FileInputBase

        return self.input.file if isinstance(self.input, FileInputBase) else None

    @property
    def command(self) -> str | None:
        from captain_hook.transcript.inputs import BashInput

        return self.input.command if isinstance(self.input, BashInput) else None

    @cached_property
    def command_line(self) -> CommandLine | None:
        from captain_hook.command import CommandLine as CL

        return CL.parse(cmd) if (cmd := self.command) else None

    @property
    def agent_type(self) -> str | None:
        from captain_hook.transcript.inputs import AgentInput

        return self.input.agent_type if isinstance(self.input, AgentInput) else None


@dataclass(frozen=True, kw_only=True)
class TranscriptMessage:
    """A single message in a transcript with parsed content blocks, tool-use extraction, and text access."""

    type: str
    content: list[ContentBlock]
    raw: RawDict = field(default_factory=lambda: {})

    @classmethod
    def from_raw(
        cls,
        *,
        type: str,
        content: list[RawBlock] | str,
        raw: RawDict,
    ) -> TranscriptMessage:
        return cls(
            type=type,
            content=parse_content(content, is_async=cls.is_async_tool_use(raw)),
            raw=raw,
        )

    @staticmethod
    def is_async_tool_use(raw: RawDict) -> bool:
        match raw.get("toolUseResult"):
            case {"isAsync": True}:
                return True
            case _:
                return False

    @cached_property
    def notification(self) -> TaskNotification | None:
        match (self.type, self.raw.get("content")):
            case ("queue-operation", str(s)):
                return TaskNotification.from_content(s)
            case _:
                return None

    @property
    def tool_uses(self) -> list[ToolUse]:
        return [
            ToolUse(name=b.name, raw_input=b.input, id=b.id)
            for b in self.content
            if isinstance(b, ToolUseBlock)
        ]

    @property
    def tool_results(self) -> list[ToolResult]:
        return [b for b in self.content if isinstance(b, ToolResult)]

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.content if isinstance(b, TextBlock))
