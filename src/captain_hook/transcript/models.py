from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from captain_hook.command import CommandLine
    from captain_hook.file import File


@dataclass(frozen=True, kw_only=True)
class TextBlock:
    """A text content block from a transcript message."""

    text: str = ""


RawDict = dict[str, Any]


@dataclass(frozen=True, kw_only=True)
class ToolUseBlock:
    """A tool-use content block from a transcript message, containing the tool name and raw input."""

    name: str
    input: RawDict = field(default_factory=lambda: {})
    id: str = ""


@dataclass(frozen=True, kw_only=True)
class ToolResult:
    """A tool-result content block linking back to its tool use via ``tool_use_id``."""

    tool_use_id: str
    content: list[Any] | str = field(default_factory=lambda: [])
    is_error: bool = False


ContentBlock = TextBlock | ToolUseBlock | ToolResult


def parse_content_block(b: Any) -> ContentBlock | str:
    """Parse a raw content block dict into a typed ``ContentBlock`` (TextBlock, ToolUseBlock, or ToolResult).

    Args:
        b: Raw dict with a ``"type"`` key, or an already-parsed block.

    Returns:
        A typed content block, or an empty string for unrecognized input.
    """
    if isinstance(b, (TextBlock, ToolUseBlock, ToolResult, str)):
        return b
    if not isinstance(b, dict):
        return ""
    raw = cast(RawDict, b)
    match raw.get("type"):
        case "tool_use":
            return ToolUseBlock(
                name=str(raw["name"]),
                input=raw.get("input", {}),
                id=str(raw.get("id", "")),
            )
        case "tool_result":
            return ToolResult(
                tool_use_id=str(raw["tool_use_id"]),
                content=raw.get("content", []),
                is_error=bool(raw.get("is_error", False)),
            )
        case _:
            return TextBlock(text=str(raw.get("text", "")))


def parse_content(raw: list[Any] | str) -> list[ContentBlock | str] | str:
    if isinstance(raw, str):
        return raw
    return [parse_content_block(b) for b in raw]


@dataclass(frozen=True, kw_only=True)
class ToolUse:
    """A tool invocation extracted from the transcript, with typed input parsing, file/command access, and result linkage."""

    name: str
    raw_input: RawDict = field(default_factory=lambda: {})
    id: str | None = None
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
    content: list[Any] | str
    raw: RawDict = field(default_factory=lambda: {})

    @classmethod
    def from_raw(
        cls,
        *,
        type: str,
        content: list[Any] | str,
        raw: RawDict | None = None,
    ) -> TranscriptMessage:
        return cls(type=type, content=parse_content(content), raw=raw or {})

    @property
    def tool_uses(self) -> list[ToolUse]:
        if not isinstance(self.content, list):
            return []
        return [
            ToolUse(name=b.name, raw_input=b.input, id=b.id or None)
            for b in self.content
            if isinstance(b, ToolUseBlock)
        ]

    @property
    def tool_results(self) -> list[ToolResult]:
        if not isinstance(self.content, list):
            return []
        return [b for b in self.content if isinstance(b, ToolResult)]

    @property
    def text(self) -> str:
        match self.content:
            case str(s):
                return s
            case list(blocks):
                return "\n".join(b if isinstance(b, str) else b.text for b in blocks if isinstance(b, (str, TextBlock)))
