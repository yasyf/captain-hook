from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


class TranscriptFixture:
    """A lightweight transcript stub for use in inline tests.

    Wraps a list of raw message dicts that get parsed into a ``Transcript``
    when the test runs.
    """

    __slots__ = ("messages",)

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages

    def __repr__(self) -> str:
        return f"TranscriptFixture({self.messages!r})"


@dataclass(frozen=True, kw_only=True)
class Block:
    """Inline test expectation: the hook should block. Optional regex ``pattern`` matches the block message."""

    pattern: str | None = None


@dataclass(frozen=True, kw_only=True)
class Warn:
    """Inline test expectation: the hook should warn. Optional regex ``pattern`` matches the warning message."""

    pattern: str | None = None


@dataclass(frozen=True, kw_only=True)
class Allow:
    """Inline test expectation: the hook should allow (return None or action ``"allow"``)."""

    pass


@dataclass(frozen=True, kw_only=True, eq=False)
class Input:
    """Inline test input descriptor modeling an event payload. Set fields based on the target event type."""

    command: str | None = None
    file: str | None = None
    content: str | None = None
    old: str | None = None
    tool: str | None = None
    prompt: str | None = None
    agent_type: str | None = None
    permission_mode: str | None = None
    transcript: Path | TranscriptFixture | list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.transcript, list):
            object.__setattr__(self, "transcript", TranscriptFixture(self.transcript))


type InlineTests = dict[str | Input, Block | Warn | Allow]
