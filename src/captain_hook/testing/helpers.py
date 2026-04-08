from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from captain_hook.conditions import matches_conditions
from captain_hook.context import HookContext
from captain_hook.dispatch import dispatch as _dispatch
from captain_hook.dispatch import execute_hook
from captain_hook.events import (
    BaseHookEvent,
    StopEvent,
    SubagentStartEvent,
    SubagentStopEvent,
    ToolHookEvent,
    UserPromptSubmitEvent,
)
from captain_hook.session import SessionStore
from captain_hook.testing.types import Allow, Block, Input, TranscriptFixture, Warn
from captain_hook.transcript import Transcript
from captain_hook.types import Event, HookResult, Tool


def build_context(
    transcript: Transcript | None = None,
    transcript_path: str | Path | None = None,
    session_dir: Path | None = None,
) -> HookContext:
    return HookContext(
        session=SessionStore(session_dir),
        transcript=transcript or Transcript.from_path(transcript_path),
        settings=None,
    )


def omit_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def make_tool_input(
    tool: str,
    command: str | None,
    file: str | None,
    content: str | None,
    old: str | None,
) -> dict[str, Any]:
    match tool:
        case "Bash" | "Execute":
            return omit_none({"command": command, "timeout": None, "description": None})
        case "Edit" | "MultiEdit":
            return {"file_path": file or "", "old_string": old or "", "new_string": content or ""}
        case "Write" | "Create":
            return {"file_path": file or "", "content": content or ""}
        case "Read":
            return omit_none({"file_path": file or "", "limit": None, "offset": None})
        case "Agent" | "Task":
            return omit_none({"prompt": content or "", "subagent_type": None})
        case _:
            return omit_none({"command": command, "file_path": file, "new_string": content, "old_string": old})


def mock_tool_event(
    tool: str = "Bash",
    *,
    event: Event = Event.PreToolUse,
    command: str | None = None,
    file: str | None = None,
    content: str | None = None,
    old: str | None = None,
    agent_type: str | None = None,
    transcript: Transcript | None = None,
    transcript_path: str | Path | None = None,
    session_dir: Path | None = None,
) -> ToolHookEvent:
    """Create a mock tool event for testing hook handlers.

    Args:
        tool: Tool name (default ``"Bash"``).
        event: Event type (default ``PreToolUse``).
        command: Bash command string.
        file: File path for Edit/Write/Read tools.
        content: New content for Edit/Write tools.
        old: Old content for Edit tools.
        agent_type: Agent type for Agent/Task tools.
        transcript: Pre-built Transcript instance.
        transcript_path: Path to load a transcript from.
        session_dir: Session directory for state persistence.

    Returns:
        A ToolHookEvent subclass matching the event type.
    """
    raw: dict[str, Any] = {"tool_name": tool, "tool_input": make_tool_input(tool, command, file, content, old)}
    if agent_type:
        raw["agent_type"] = agent_type
    ctx = build_context(transcript, transcript_path, session_dir)
    return cast(ToolHookEvent, event.event_class(_raw=raw, ctx=ctx))


def mock_stop_event(
    *,
    stop_hook_active: bool = False,
    transcript: Transcript | None = None,
    transcript_path: str | Path | None = None,
    session_dir: Path | None = None,
) -> StopEvent:
    """Create a mock Stop event for testing.

    Args:
        stop_hook_active: Whether the stop hook is active.
        transcript: Pre-built Transcript instance.
        transcript_path: Path to load a transcript from.
        session_dir: Session directory for state persistence.

    Returns:
        A StopEvent with mock context.
    """
    return StopEvent(
        _raw={"stop_hook_active": stop_hook_active},
        ctx=build_context(transcript, transcript_path, session_dir),
    )


def mock_subagent_stop_event(
    agent_type: str = "",
    *,
    agent_id: str = "",
    stop_hook_active: bool = False,
    agent_transcript_path: str = "",
    transcript: Transcript | None = None,
    transcript_path: str | Path | None = None,
    session_dir: Path | None = None,
) -> SubagentStopEvent:
    """Create a mock SubagentStop event for testing.

    Args:
        agent_type: Type identifier of the subagent.
        agent_id: Unique agent ID.
        stop_hook_active: Whether the stop hook is active.
        agent_transcript_path: Path to the subagent's transcript.
        transcript: Pre-built Transcript instance.
        transcript_path: Path to load a transcript from.
        session_dir: Session directory for state persistence.

    Returns:
        A SubagentStopEvent with mock context.
    """
    return SubagentStopEvent(
        _raw={
            "stop_hook_active": stop_hook_active,
            "agent_type": agent_type,
            "agent_id": agent_id,
            "agent_transcript_path": agent_transcript_path,
        },
        ctx=build_context(transcript, transcript_path, session_dir),
    )


def mock_subagent_start_event(
    agent_type: str = "",
    *,
    agent_id: str = "",
    transcript: Transcript | None = None,
    transcript_path: str | Path | None = None,
    session_dir: Path | None = None,
) -> SubagentStartEvent:
    """Create a mock SubagentStart event for testing.

    Args:
        agent_type: Type identifier of the subagent.
        agent_id: Unique agent ID.
        transcript: Pre-built Transcript instance.
        transcript_path: Path to load a transcript from.
        session_dir: Session directory for state persistence.

    Returns:
        A SubagentStartEvent with mock context.
    """
    return SubagentStartEvent(
        _raw={"agent_type": agent_type, "agent_id": agent_id},
        ctx=build_context(transcript, transcript_path, session_dir),
    )


def mock_user_prompt_event(
    prompt: str = "",
    *,
    transcript: Transcript | None = None,
    transcript_path: str | Path | None = None,
    session_dir: Path | None = None,
) -> UserPromptSubmitEvent:
    """Create a mock UserPromptSubmit event for testing.

    Args:
        prompt: The user's prompt text.
        transcript: Pre-built Transcript instance.
        transcript_path: Path to load a transcript from.
        session_dir: Session directory for state persistence.

    Returns:
        A UserPromptSubmitEvent with mock context.
    """
    return UserPromptSubmitEvent(
        _raw={"prompt": prompt},
        ctx=build_context(transcript, transcript_path, session_dir),
    )


def mock_event(
    event: str | Event = "PreToolUse",
    *,
    tool: str | None = None,
    command: str | None = None,
    file: str | None = None,
    content: str | None = None,
    old: str | None = None,
    prompt: str | None = None,
    transcript: Transcript | None = None,
    transcript_path: str | Path | None = None,
    stop_hook_active: bool = False,
    session_dir: Path | None = None,
    **extra: Any,
) -> BaseHookEvent:
    """Create a mock event of any type for testing — universal factory.

    Dispatches to the appropriate ``mock_*_event`` function based on the event type.

    Args:
        event: Event type name or flag (default ``"PreToolUse"``).
        tool: Tool name for tool events.
        command: Bash command for tool events.
        file: File path for Edit/Write/Read events.
        content: Content for Edit/Write events.
        old: Old content for Edit events.
        prompt: User prompt for UserPromptSubmit events.
        transcript: Pre-built Transcript instance.
        transcript_path: Path to load a transcript from.
        stop_hook_active: Whether the stop hook is active.
        session_dir: Session directory for state persistence.
        **extra: Additional kwargs (e.g. ``agent_type``, ``agent_id``).

    Returns:
        A BaseHookEvent subclass matching the event type.
    """
    ev = Event[event] if isinstance(event, str) else event
    ctx_kw: dict[str, Any] = {"transcript": transcript, "transcript_path": transcript_path, "session_dir": session_dir}
    match ev:
        case Event.Stop:
            return mock_stop_event(stop_hook_active=stop_hook_active, **ctx_kw)
        case Event.SubagentStop:
            return mock_subagent_stop_event(
                agent_type=extra.pop("agent_type", ""),
                agent_id=extra.pop("agent_id", ""),
                stop_hook_active=stop_hook_active,
                agent_transcript_path=extra.pop("agent_transcript_path", ""),
                **ctx_kw,
            )
        case Event.SubagentStart:
            return mock_subagent_start_event(
                agent_type=extra.pop("agent_type", ""),
                agent_id=extra.pop("agent_id", ""),
                **ctx_kw,
            )
        case Event.UserPromptSubmit:
            return mock_user_prompt_event(prompt=prompt or "", **ctx_kw)
        case _:
            return mock_tool_event(
                tool=tool or "Bash",
                event=ev,
                command=command,
                file=file,
                content=content,
                old=old,
                agent_type=extra.pop("agent_type", None),
                **ctx_kw,
            )


def input_to_event(
    event: str | Event,
    inp: Input,
    spec_tool: str | None = None,
) -> BaseHookEvent:
    """Convert an ``Input`` test descriptor into a mock event.

    Args:
        event: Event type name or flag.
        inp: Test input with tool, command, file, transcript, etc.
        spec_tool: Fallback tool name from the hook spec's conditions.

    Returns:
        A mock event suitable for dispatch testing.
    """
    match inp.transcript:
        case TranscriptFixture() as tf:
            transcript, transcript_path = Transcript.from_messages(tf.messages), None
        case Path() as p:
            transcript, transcript_path = None, p
        case _:
            transcript, transcript_path = None, None

    ev = Event[event] if isinstance(event, str) else event
    ctx_kw: dict[str, Any] = {"transcript": transcript, "transcript_path": transcript_path}
    match ev:
        case Event.SubagentStop:
            return mock_subagent_stop_event(agent_type=inp.agent_type or "", **ctx_kw)
        case Event.SubagentStart:
            return mock_subagent_start_event(agent_type=inp.agent_type or "", **ctx_kw)
        case Event.UserPromptSubmit:
            return mock_user_prompt_event(prompt=inp.prompt or "", **ctx_kw)
        case Event.Stop:
            return mock_stop_event(**ctx_kw)
        case _:
            return mock_tool_event(
                tool=inp.tool or spec_tool or "Bash",
                event=ev,
                command=inp.command,
                file=inp.file,
                content=inp.content,
                old=inp.old,
                agent_type=inp.agent_type,
                **ctx_kw,
            )


def assert_result(
    result: HookResult | None,
    expected: Block | Warn | Allow,
    hook_name: str = "",
) -> None:
    """Assert that a hook result matches an expected outcome.

    Args:
        result: The actual HookResult (or None for allow).
        expected: Expected outcome — ``Block``, ``Warn``, or ``Allow``.
        hook_name: Optional hook name for error message context.

    Raises:
        AssertionError: If the result doesn't match the expected outcome.
    """
    prefix = f"[{hook_name}] " if hook_name else ""
    match expected:
        case Allow():
            assert result is None or result.action == "allow", f"{prefix}Expected Allow, got {result}"
        case Block(pattern=pat):
            assert result is not None and result.action == "block", f"{prefix}Expected Block, got {result}"
            if pat and result.message:
                assert re.search(pat, result.message), f"{prefix}Block message doesn't match '{pat}'"
        case Warn(pattern=pat):
            assert result is not None and result.action == "warn", f"{prefix}Expected Warn, got {result}"
            if pat and result.message:
                assert re.search(pat, result.message), f"{prefix}Warn message doesn't match '{pat}'"


def dispatch_test(
    app: Any,
    event: str | Event,
    *,
    tool: str | None = None,
    command: str | None = None,
    file: str | None = None,
    content: str | None = None,
    old: str | None = None,
    prompt: str | None = None,
    transcript: Transcript | None = None,
    async_: bool = False,
) -> dict[str, Any] | None:
    """Dispatch a mock event through a HookApp and return the JSON output.

    Convenience wrapper for testing that creates a mock event, dispatches it,
    and returns the formatted output dict.

    Args:
        app: The HookApp to dispatch through.
        event: Event type name or flag.
        tool: Tool name for tool events.
        command: Bash command.
        file: File path.
        content: Content for Edit/Write.
        old: Old content for Edit.
        prompt: User prompt for UserPromptSubmit.
        transcript: Pre-built Transcript.
        async_: If True, dispatch async hooks only.

    Returns:
        JSON-serializable output dict, or None.
    """
    from captain_hook.app import HookApp

    ev = Event[event] if isinstance(event, str) else event
    evt = mock_event(
        event,
        tool=tool,
        command=command,
        file=file,
        content=content,
        old=old,
        prompt=prompt,
        transcript=transcript,
    )
    return _dispatch(cast(HookApp, app), ev, evt, async_=async_)


def stub_call_llm(
    _template: Any,
    *args: Any,
    response_model: type[BaseModel] | None = None,
    **kwargs: Any,
) -> str | BaseModel:
    if response_model is None:
        return "stubbed"
    fields = response_model.model_fields
    values: dict[str, Any] = {}
    for name, info in fields.items():
        match name:
            case "block" | "fire":
                values[name] = True
            case "action":
                values[name] = "block"
            case "reasoning" | "reason":
                values[name] = "inline test stub"
            case _ if info.default is not None:
                pass
            case _:
                values[name] = ""
    return response_model(**values)


def run_inline_tests(app: Any) -> list[tuple[str, str, bool, str]]:
    """Discover and execute all inline tests from registered hooks.

    Iterates hooks with ``tests`` dicts, creates mock events from ``Input`` keys,
    dispatches through the hook, and validates results against expected outcomes.

    Args:
        app: The HookApp containing hooks with inline tests.

    Returns:
        List of ``(test_name, status, passed, detail)`` tuples.
    """
    from captain_hook.app import HookApp

    hook_app = cast(HookApp, app)
    results: list[tuple[str, str, bool, str]] = []

    for entry in hook_app.hooks:
        if not entry.spec.tests:
            continue
        for key, expected in entry.spec.tests.items():
            test_name = f"{entry.name}:{key!r}"
            try:
                if isinstance(key, Input):
                    spec_tools = [p for c in entry.spec.only_if if isinstance(c, Tool) for p in c.pattern.split("|")]
                    evt = input_to_event(
                        entry.spec.events,
                        key,
                        spec_tools[0] if spec_tools else None,
                    )
                else:
                    results.append((test_name, "skip", True, "Session keys not supported"))
                    continue

                evt.ctx.call_llm = stub_call_llm  # type: ignore[assignment]
                hook_result = execute_hook(entry, evt) if matches_conditions(entry.spec, evt) else None
                assert_result(hook_result, expected, entry.name)
                results.append((test_name, "pass", True, ""))
            except AssertionError as e:
                results.append((test_name, "fail", False, str(e)))
            except Exception as e:
                results.append((test_name, "error", False, f"{type(e).__name__}: {e}"))

    return results
