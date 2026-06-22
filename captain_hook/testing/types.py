from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


class TranscriptFixture:
    """A lightweight transcript stub for use in inline tests.

    Wraps a list of raw transcript-line dicts that get lifted into a query
    ``Session`` when the test runs; missing envelope fields are synthesized.
    """

    __slots__ = ("messages",)

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages

    def __repr__(self) -> str:
        return f"TranscriptFixture({self.messages!r})"


@dataclass(frozen=True, kw_only=True)
class FileFixture:
    """Inline-test file descriptor: materialized to a real temp file so size/stat-based guards run for real."""

    size: int | None = None
    content: str | None = None
    name: str | None = None


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


@dataclass(frozen=True, kw_only=True)
class Rewrite:
    """Inline test expectation: the hook should rewrite the command.

    Optional ``pattern`` is a substring matched against the rewritten command
    (``updated_input["command"]``) — substring, not regex, so an absolute path
    prefix in the rewritten command does not break the match.
    """

    pattern: str | None = None


@dataclass(frozen=True, kw_only=True, eq=False)
class Input:
    """Inline test input descriptor modeling an event payload. Set fields based on the target event type."""

    command: str | None = None
    file: str | FileFixture | None = None
    content: str | None = None
    old: str | None = None
    tool: str | None = None
    prompt: str | None = None
    agent_type: str | None = None
    reason: str | None = None
    permission_mode: str | None = None
    offset: int | None = None
    limit: int | None = None
    transcript: Path | TranscriptFixture | list[dict[str, Any]] | None = None
    tasks: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.transcript, list):
            object.__setattr__(self, "transcript", TranscriptFixture(self.transcript))


type InlineTests = dict[str | Input, Block | Warn | Allow | Rewrite]
"""Inline test specification mapping inputs to expected outcomes.

A mapping whose keys are ``Input`` descriptors (or legacy ``str`` session
keys) and whose values are the expected hook result — ``Block``, ``Warn``,
``Allow``, or ``Rewrite``.

Keys:
    Input: Structured test-input descriptor specifying tool, command, file,
        content, and/or transcript context.
    str: Legacy session-key format — replayed from a recorded session
        during ``run_inline_tests`` when a fixture exists, else skipped.

Values:
    Block: The hook must block, optionally matching a regex ``pattern``.
    Warn: The hook must warn, optionally matching a regex ``pattern``.
    Allow: The hook must allow (return ``None`` or action ``"allow"``).
    Rewrite: The hook must rewrite the command, optionally matching a
        substring ``pattern`` against ``updated_input["command"]``.

Example::

    tests: InlineTests = {
        Input(command="rm -rf /"): Block(pattern="dangerous"),
        Input(command="ls"): Allow(),
    }
"""
