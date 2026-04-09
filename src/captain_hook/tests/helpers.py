from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from captain_hook.context import HookContext
from captain_hook.dispatch import dispatch as _dispatch
from captain_hook.events import PostToolUseEvent, PreToolUseEvent, StopEvent, SubagentStopEvent
from captain_hook.session import SessionStore
from captain_hook.testing.helpers import (
    assert_result,
    build_context,
    input_to_event,
    mock_event,
    mock_stop_event,
    mock_subagent_start_event,
    mock_subagent_stop_event,
    mock_tool_event,
    mock_user_prompt_event,
)
from captain_hook.transcript import Transcript
from captain_hook.types import Event

PKG_DIR = Path(__file__).resolve().parents[3]

__all__ = [
    "PKG_DIR",
    "assert_result",
    "build_context",
    "build_ctx",
    "dispatch_test",
    "input_to_event",
    "make_ctx",
    "make_event",
    "make_post_tool_event",
    "make_pre_tool_event",
    "make_stop_event",
    "make_subagent_stop_event",
    "make_transcript_ctx",
    "mock_event",
    "mock_stop_event",
    "mock_subagent_start_event",
    "mock_subagent_stop_event",
    "mock_tool_event",
    "mock_user_prompt_event",
    "run_cli",
]


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


def make_ctx(
    session_dir: Path | None = None,
    *,
    texts: list[str] | None = None,
    n_messages: int = 10,
    settings: Any = None,
    call_llm_return: Any = None,
) -> HookContext:
    from captain_hook.transcript.models import TextBlock, TranscriptMessage

    text_msgs = [TranscriptMessage(type="assistant", content=[TextBlock(text=t)]) for t in (texts or [])]
    padding = [TranscriptMessage(type="assistant", content=[]) for _ in range(max(0, n_messages - len(text_msgs)))]
    ctx = HookContext(
        session=SessionStore(session_dir),
        transcript=Transcript(messages=padding + text_msgs),
        settings=settings,
    )
    ctx.call_llm = MagicMock(return_value=call_llm_return)  # type: ignore[method-assign]
    return ctx


def build_ctx(
    transcript: Transcript | None = None,
    session_dir: Any = None,
    settings: Any = None,
) -> HookContext:
    return HookContext(
        session=SessionStore(session_dir),
        transcript=transcript or Transcript(messages=[]),
        settings=settings,
    )


def make_event(
    event_cls: type,
    raw: dict[str, Any] | None = None,
    ctx: Any = None,
) -> Any:
    return event_cls(_raw=raw or {}, ctx=ctx)


def make_pre_tool_event(
    tool_name: str = "Bash",
    tool_input: dict[str, Any] | None = None,
    ctx: Any = None,
) -> PreToolUseEvent:
    return PreToolUseEvent(
        _raw={"tool_name": tool_name} | ({"tool_input": tool_input} if tool_input is not None else {}),
        ctx=ctx or make_ctx(),
    )


def make_post_tool_event(
    tool_name: str = "Bash",
    tool_input: dict[str, Any] | None = None,
    ctx: Any = None,
) -> PostToolUseEvent:
    return PostToolUseEvent(
        _raw={"tool_name": tool_name} | ({"tool_input": tool_input} if tool_input is not None else {}),
        ctx=ctx or make_ctx(),
    )


def make_stop_event(ctx: Any = None) -> StopEvent:
    return StopEvent(_raw={}, ctx=ctx or make_ctx())


def make_subagent_stop_event(ctx: Any = None) -> SubagentStopEvent:
    return SubagentStopEvent(_raw={}, ctx=ctx or make_ctx())


def make_transcript_ctx(
    has_skill_result: bool = False,
    has_read_result: bool = False,
    has_edit_to_result: bool = False,
    has_command_result: bool = False,
    count_tools_map: dict[str, int] | None = None,
) -> Any:
    ctx = MagicMock()
    (transcript := ctx.transcript)  # noqa: E275 — walrus for inline binding
    transcript.has_skill = MagicMock(return_value=has_skill_result)
    transcript.has_read = MagicMock(return_value=has_read_result)
    transcript.has_edit_to = MagicMock(return_value=has_edit_to_result)
    transcript.has_command = MagicMock(return_value=has_command_result)
    transcript.count_tools = MagicMock(side_effect=lambda t: (count_tools_map or {}).get(t, 0))
    return ctx


def run_cli(
    *args: str,
    stdin_data: str = "",
    hooks_dir: str | None = None,
    root_dir: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "captain_hook"]
        + (["--hooks", hooks_dir] if hooks_dir else [])
        + (["--root", root_dir] if root_dir else [])
        + list(args),
        input=stdin_data,
        capture_output=True,
        text=True,
        cwd=str(PKG_DIR),
    )
