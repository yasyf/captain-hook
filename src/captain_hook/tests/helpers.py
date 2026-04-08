from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

from captain_hook.context import HookContext
from captain_hook.dispatch import dispatch as _dispatch
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
from captain_hook.types import Event, HookResult


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
    raw: dict[str, Any] = {"tool_name": tool, "tool_input": make_tool_input(tool, command, file, content, old)}
    if agent_type:
        raw["agent_type"] = agent_type
    return cast(ToolHookEvent, event.event_class(_raw=raw, ctx=build_context(transcript, transcript_path, session_dir)))


def mock_stop_event(
    *,
    stop_hook_active: bool = False,
    transcript: Transcript | None = None,
    transcript_path: str | Path | None = None,
    session_dir: Path | None = None,
) -> StopEvent:
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
    return _dispatch(
        Event[event] if isinstance(event, str) else event,
        mock_event(event, tool=tool, command=command, file=file, content=content, old=old, prompt=prompt, transcript=transcript),
        async_=async_,
    )
