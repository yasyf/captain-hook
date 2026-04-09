from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

from captain_hook.app import reset
from captain_hook.context import HookContext
from captain_hook.events import PostToolUseEvent, PreToolUseEvent, StopEvent, SubagentStopEvent
from captain_hook.session import SessionStore
from captain_hook.tests.helpers import make_ctx
from captain_hook.transcript import Transcript


@pytest.fixture(autouse=True)
def clean_state():
    reset()
    yield
    reset()


@pytest.fixture
def isolate_modules():
    snapshot_modules = set(sys.modules.keys())
    snapshot_path = sys.path[:]
    yield
    for key in set(sys.modules.keys()) - snapshot_modules:
        del sys.modules[key]
    sys.path[:] = snapshot_path


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
    raw: dict[str, Any] = {"tool_name": tool_name}
    if tool_input is not None:
        raw["tool_input"] = tool_input
    return PreToolUseEvent(_raw=raw, ctx=ctx or make_ctx())


def make_post_tool_event(
    tool_name: str = "Bash",
    tool_input: dict[str, Any] | None = None,
    ctx: Any = None,
) -> PostToolUseEvent:
    raw: dict[str, Any] = {"tool_name": tool_name}
    if tool_input is not None:
        raw["tool_input"] = tool_input
    return PostToolUseEvent(_raw=raw, ctx=ctx or make_ctx())


def make_stop_event(ctx: Any = None) -> StopEvent:
    return StopEvent(_raw={}, ctx=ctx or make_ctx())


def make_subagent_stop_event(ctx: Any = None) -> SubagentStopEvent:
    return SubagentStopEvent(_raw={}, ctx=ctx or make_ctx())


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


def make_transcript_ctx(
    has_skill_result: bool = False,
    has_read_result: bool = False,
    has_edit_to_result: bool = False,
    has_command_result: bool = False,
    count_tools_map: dict[str, int] | None = None,
) -> Any:
    transcript = MagicMock()
    transcript.has_skill = MagicMock(return_value=has_skill_result)
    transcript.has_read = MagicMock(return_value=has_read_result)
    transcript.has_edit_to = MagicMock(return_value=has_edit_to_result)
    transcript.has_command = MagicMock(return_value=has_command_result)
    transcript.count_tools = MagicMock(side_effect=lambda t: (count_tools_map or {}).get(t, 0))
    ctx = MagicMock()
    ctx.transcript = transcript
    return ctx
