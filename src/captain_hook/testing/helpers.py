from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from captain_hook.conditions import matches_conditions
from captain_hook.context import HookContext
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
from captain_hook.testing.session_cache import SessionCache
from captain_hook.testing.types import Allow, Block, Input, TranscriptFixture, Warn
from captain_hook.transcript import Transcript
from captain_hook.types import Event, HookResult, Tool

STUB_FIELD_VALUES: dict[str, Any] = {
    "block": True, "fire": True,
    "action": "block",
    "reasoning": "inline test stub", "reason": "inline test stub",
}


def build_context(
    transcript: Transcript | None = None,
    transcript_path: str | Path | None = None,
    session_dir: Path | None = None,
    project_root: Path | None = None,
) -> HookContext:
    return HookContext(
        session=SessionStore(session_dir),
        transcript=transcript or Transcript.from_path(transcript_path),
        settings=None,
        project_root=project_root,
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
    permission_mode: str | None = None,
    transcript: Transcript | None = None,
    transcript_path: str | Path | None = None,
    session_dir: Path | None = None,
    project_root: Path | None = None,
) -> ToolHookEvent:
    return cast(
        ToolHookEvent,
        event.event_class(
            _raw={"tool_name": tool, "tool_input": make_tool_input(tool, command, file, content, old)}
            | ({"agent_type": agent_type} if agent_type else {})
            | ({"permission_mode": permission_mode} if permission_mode else {}),
            ctx=build_context(transcript, transcript_path, session_dir, project_root),
        ),
    )


def mock_stop_event(
    *,
    stop_hook_active: bool = False,
    permission_mode: str | None = None,
    transcript: Transcript | None = None,
    transcript_path: str | Path | None = None,
    session_dir: Path | None = None,
) -> StopEvent:
    return StopEvent(
        _raw={"stop_hook_active": stop_hook_active}
        | ({"permission_mode": permission_mode} if permission_mode else {}),
        ctx=build_context(transcript, transcript_path, session_dir),
    )


def mock_subagent_stop_event(
    agent_type: str = "",
    *,
    agent_id: str = "",
    stop_hook_active: bool = False,
    agent_transcript_path: str = "",
    permission_mode: str | None = None,
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
        }
        | ({"permission_mode": permission_mode} if permission_mode else {}),
        ctx=build_context(transcript, transcript_path, session_dir),
    )


def mock_subagent_start_event(
    agent_type: str = "",
    *,
    agent_id: str = "",
    permission_mode: str | None = None,
    transcript: Transcript | None = None,
    transcript_path: str | Path | None = None,
    session_dir: Path | None = None,
) -> SubagentStartEvent:
    return SubagentStartEvent(
        _raw={"agent_type": agent_type, "agent_id": agent_id}
        | ({"permission_mode": permission_mode} if permission_mode else {}),
        ctx=build_context(transcript, transcript_path, session_dir),
    )


def mock_user_prompt_event(
    prompt: str = "",
    *,
    permission_mode: str | None = None,
    transcript: Transcript | None = None,
    transcript_path: str | Path | None = None,
    session_dir: Path | None = None,
) -> UserPromptSubmitEvent:
    return UserPromptSubmitEvent(
        _raw={"prompt": prompt}
        | ({"permission_mode": permission_mode} if permission_mode else {}),
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
    ctx_kw: dict[str, Any] = {
        "transcript": transcript,
        "transcript_path": transcript_path,
        "permission_mode": inp.permission_mode,
    }
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


def replay_session(entry: Any, jsonl: Path) -> Iterator[HookResult | None]:
    transcript = Transcript.from_path(jsonl)
    ctx = HookContext(session=SessionStore(None), transcript=transcript, settings=None)
    ctx.call_llm = stub_call_llm  # type: ignore[method-assign]
    transcript_path = str(jsonl)

    for ev_type in entry.spec.events:
        for raw in transcript_event_payloads(ev_type, transcript, transcript_path):
            yield execute_hook(entry, ev_type.event_class(_raw=raw, ctx=ctx))


def transcript_event_payloads(
    ev_type: Event,
    transcript: Transcript,
    transcript_path: str,
) -> Iterator[dict[str, Any]]:
    base = {"transcript_path": transcript_path}
    match ev_type:
        case Event.PreToolUse:
            yield from (
                base | {"tool_name": tu.name, "tool_input": tu.raw_input}
                for tu in transcript.tool_uses
            )
        case Event.PostToolUse:
            yield from (
                base | {"tool_name": tu.name, "tool_input": tu.raw_input}
                | ({"tool_response": str(tu.result.content)} if tu.result and not tu.result.is_error else {})
                for tu in transcript.tool_uses
            )
        case Event.PostToolUseFailure:
            yield from (
                base | {"tool_name": tu.name, "tool_input": tu.raw_input, "error": str(tu.result.content)}
                for tu in transcript.tool_uses.with_errors
                if tu.result and tu.result.is_error
            )
        case Event.UserPromptSubmit:
            yield from (
                base | {"prompt": msg.text}
                for msg in transcript.messages
                if transcript.is_user_message(msg) and msg.text
            )
        case Event.Stop | Event.SubagentStop | Event.SubagentStart | Event.Notification | Event.PreCompact:
            yield base


def matches_expected(result: HookResult | None, expected: Block | Warn | Allow) -> bool:
    match expected:
        case Allow():
            return result is None or result.action == "allow"
        case Block(pattern=pat):
            return result is not None and result.action == "block" and (
                not pat or not result.message or bool(re.search(pat, result.message))
            )
        case Warn(pattern=pat):
            return result is not None and result.action == "warn" and (
                not pat or not result.message or bool(re.search(pat, result.message))
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


def stub_call_llm(
    _template: Any,
    *args: Any,
    response_model: type[BaseModel] | None = None,
    **kwargs: Any,
) -> str | BaseModel:
    if response_model is None:
        return "stubbed"
    return response_model(**{
        name: STUB_FIELD_VALUES.get(name, "")
        for name, info in response_model.model_fields.items()
        if name in STUB_FIELD_VALUES or info.default is None
    })


def run_inline_tests() -> list[tuple[str, str, bool, str]]:
    from captain_hook.app import _state

    results: list[tuple[str, str, bool, str]] = []

    for entry in _state.hooks:
        if not entry.spec.tests:
            continue
        for key, expected in entry.spec.tests.items():
            test_name = f"{entry.name}:{key!r}"
            try:
                if isinstance(key, Input):
                    spec_tools = [p for c in entry.spec.only_if if isinstance(c, Tool) for p in c.pattern.split("|")]
                    evt = input_to_event(
                        next(iter(entry.spec.events)),
                        key,
                        spec_tools[0] if spec_tools else None,
                    )
                    from captain_hook.app import is_planning_agent_skip

                    evt.ctx.call_llm = stub_call_llm  # type: ignore[assignment]
                    hook_result = (
                        execute_hook(entry, evt)
                        if matches_conditions(entry.spec, evt) and not is_planning_agent_skip(entry.spec, evt)
                        else None
                    )
                    assert_result(hook_result, expected, entry.name)
                elif jsonl := SessionCache.for_root().load(key):
                    replays = list(replay_session(entry, jsonl))
                    if not any(matches_expected(r, expected) for r in replays):
                        assert_result(replays[-1] if replays else None, expected, entry.name)
                else:
                    results.append((test_name, "skip", True, f"no fixture or local Claude session for {key}"))
                    continue
                results.append((test_name, "pass", True, ""))
            except AssertionError as e:
                results.append((test_name, "fail", False, str(e)))
            except Exception as e:
                results.append((test_name, "error", False, f"{type(e).__name__}: {e}"))

    return results
