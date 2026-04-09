from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from captain_hook.context import HookContext
from captain_hook.dispatch import dispatch as _dispatch
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

__all__ = [
    "assert_result",
    "build_context",
    "dispatch_test",
    "input_to_event",
    "mock_event",
    "mock_stop_event",
    "mock_subagent_start_event",
    "mock_subagent_stop_event",
    "make_ctx",
    "mock_tool_event",
    "mock_user_prompt_event",
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
