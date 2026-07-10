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
    """Inline test expectation: the hook should allow (return None or action ``"allow"``).

    ``explicit=True`` requires an actual allow result — ``None`` no longer matches, so
    e.g. a ``PermissionRequest`` hook must have answered the dialog itself.
    """

    explicit: bool = False


@dataclass(frozen=True, kw_only=True)
class Ask:
    """Inline test expectation: the hook returned no result (``None``).

    For ``PermissionRequest`` hooks this means the permission dialog shows; a ``warn``
    result fails ``Ask()``.
    """


@dataclass(frozen=True, init=False)
class Rewrite:
    """Inline test expectation: the hook should rewrite the tool input.

    ``pattern`` is a substring matched against the rewritten command
    (``updated_input["command"]``) — substring, not regex, so an absolute path
    prefix in the rewritten command does not break the match. Additional keyword
    arguments substring-match against the named ``updated_input`` field, so an
    Agent auto-upgrade can assert ``Rewrite(model="sonnet")``; all given fields
    (and ``pattern``) must match.
    """

    pattern: str | None
    fields: tuple[tuple[str, str], ...]

    def __init__(self, pattern: str | None = None, **fields: str) -> None:
        object.__setattr__(self, "pattern", pattern)
        object.__setattr__(self, "fields", tuple(sorted(fields.items())))


@dataclass(frozen=True, kw_only=True, eq=False)
class Input:
    """Inline test input descriptor modeling an event payload.

    Set the fields the target event reads; all default to ``None``.

    Args:
        command: Bash command for ``PreToolUse``/``PostToolUse``.
        file: The tool's file path. A ``FileFixture`` materializes a real temp
            file so size/stat-based guards run; a ``str`` stays a virtual path.
        content: New file content for ``Edit``/``Write``.
        old: Pre-edit content for ``Edit`` and ``diff_lint``.
        tool: Tool name when no condition pins it.
        tool_input: Verbatim raw tool-input mapping — an escape hatch that wins
            over the input synthesized from the other fields.
        prompt: An Agent/Task call's prompt, or ``UserPromptSubmit`` text.
        script: A ``Workflow`` tool's script source (synthesizes a Workflow call).
        agent_type: Subagent type for subagent events (an Agent/Task call's
            ``subagent_type``).
        agent_id: Subagent/teammate id — a non-empty value makes ``evt.is_subagent``
            true (and fills subagent events' ``agent_id``); each id also gets its
            own ``max_fires`` budget.
        model: Model for an Agent/Task call's ``model`` input field.
        output: The tool result surfaced to ``PostToolUse`` as ``evt.tool_response``.
        error: The failure text surfaced to ``PostToolUseFailure`` as ``evt.error``.
        reason: ``SessionEnd`` reason.
        source: ``SessionStart`` source (``startup``/``resume``/``clear``/``compact``).
        permission_mode: Permission mode, e.g. ``"plan"`` for plan-mode gating.
        skip_permissions: Pre-seeds ``evt.skip_permissions`` (normally the
            process-tree walk for ``--dangerously-skip-permissions``); ``None``
            leaves the real walk in place.
        offset: ``Read`` call offset.
        limit: ``Read`` call limit.
        transcript: Session history for transcript conditions — a path, a
            ``TranscriptFixture``, or a raw list of transcript-line dicts.
        tasks: The native task list read via ``evt.tasks``.
        llm: Per-test LLM stub overrides merged over the default stub verdict
            (``fire``/``block``/``action``/``reasoning``), e.g. ``llm={"fire": False}``
            to exercise an LLM hook's judge-declines path.
    """

    command: str | None = None
    file: str | FileFixture | None = None
    content: str | None = None
    old: str | None = None
    tool: str | None = None
    tool_input: dict[str, Any] | None = None
    prompt: str | None = None
    script: str | None = None
    agent_type: str | None = None
    agent_id: str | None = None
    model: str | None = None
    output: str | None = None
    error: str | None = None
    reason: str | None = None
    source: str | None = None
    permission_mode: str | None = None
    skip_permissions: bool | None = None
    offset: int | None = None
    limit: int | None = None
    transcript: Path | TranscriptFixture | list[dict[str, Any]] | None = None
    tasks: list[dict[str, Any]] | None = None
    llm: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.transcript, list):
            object.__setattr__(self, "transcript", TranscriptFixture(self.transcript))

    def __repr__(self) -> str:
        set_fields = ", ".join(
            f"{name}={value!r}" for name in self.__dataclass_fields__ if (value := getattr(self, name)) is not None
        )
        return f"Input({set_fields})"


type InlineTests = dict[str | Input, Block | Warn | Allow | Rewrite | Ask]
"""Inline test specification mapping inputs to expected outcomes.

A mapping whose keys are ``Input`` descriptors (or legacy ``str`` session
keys) and whose values are the expected hook result — ``Block``, ``Warn``,
``Allow``, ``Rewrite``, or ``Ask``.

Keys:
    Input: Structured test-input descriptor specifying tool, command, file,
        content, and/or transcript context.
    str: Legacy session-key format — replayed from a recorded session
        during ``run_inline_tests`` when a fixture exists, else skipped.

Values:
    Block: The hook must block, optionally matching a regex ``pattern``.
    Warn: The hook must warn, optionally matching a regex ``pattern``.
    Allow: The hook must allow (return ``None`` or action ``"allow"``;
        ``Allow(explicit=True)`` rejects ``None``).
    Rewrite: The hook must rewrite the command, optionally matching a
        substring ``pattern`` against ``updated_input["command"]``.
    Ask: The hook must return no result (``None``) — for
        ``PermissionRequest``, the dialog shows.

Example::

    tests: InlineTests = {
        Input(command="rm -rf /"): Block(pattern="dangerous"),
        Input(command="ls"): Allow(),
    }
"""
