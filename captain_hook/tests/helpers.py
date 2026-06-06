from __future__ import annotations

import os
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
    assert_result as assert_result,
)
from captain_hook.testing.helpers import (
    build_context as build_context,
)
from captain_hook.testing.helpers import (
    input_to_event as input_to_event,
)
from captain_hook.testing.helpers import (
    mock_event as mock_event,
)
from captain_hook.testing.helpers import (
    mock_stop_event as mock_stop_event,
)
from captain_hook.testing.helpers import (
    mock_subagent_start_event as mock_subagent_start_event,
)
from captain_hook.testing.helpers import (
    mock_subagent_stop_event as mock_subagent_stop_event,
)
from captain_hook.testing.helpers import (
    mock_tool_event as mock_tool_event,
)
from captain_hook.testing.helpers import (
    mock_user_prompt_event as mock_user_prompt_event,
)
from captain_hook.transcript import Transcript
from captain_hook.transcript.models import TextBlock, ToolResult, ToolUseBlock, TranscriptMessage
from captain_hook.types import Event

PKG_DIR = Path(__file__).resolve().parents[3]


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
        mock_event(
            event, tool=tool, command=command, file=file, content=content, old=old, prompt=prompt, transcript=transcript
        ),
        async_=async_,
    )


def make_ctx(
    session_dir: Path | None = None,
    *,
    texts: list[str] | None = None,
    n_messages: int = 10,
    settings: Any = None,
    call_llm_return: Any = None,
    project_root: Path | None = None,
) -> HookContext:
    from captain_hook.transcript.models import TextBlock, TranscriptMessage

    text_msgs = [TranscriptMessage(type="assistant", content=[TextBlock(text=t)]) for t in (texts or [])]
    padding = [TranscriptMessage(type="assistant", content=[]) for _ in range(max(0, n_messages - len(text_msgs)))]
    ctx = HookContext(
        session=SessionStore(session_dir),
        transcript=Transcript(messages=padding + text_msgs),
        settings=settings,
        project_root=project_root,
    )
    ctx.call_llm = MagicMock(return_value=call_llm_return)  # type: ignore[method-assign]
    return ctx


def build_ctx(
    transcript: Transcript | None = None,
    session_dir: Any = None,
    settings: Any = None,
    project_root: Path | None = None,
) -> HookContext:
    return HookContext(
        session=SessionStore(session_dir),
        transcript=transcript or Transcript(messages=[]),
        settings=settings,
        project_root=project_root,
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


def make_messages_ctx(messages: list[TranscriptMessage] | None) -> Any:
    return MagicMock(transcript=None if messages is None else Transcript(messages=messages))


def run_cli(
    *args: str,
    stdin_data: str = "",
    hooks_dir: str | None = None,
    root_dir: str | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "captain_hook"]
        + (["--hooks", hooks_dir] if hooks_dir else [])
        + (["--root", root_dir] if root_dir else [])
        + list(args),
        input=stdin_data,
        capture_output=True,
        text=True,
        cwd=cwd or str(PKG_DIR),
        env={**os.environ, **env} if env else None,
    )


# --- Raw JSONL-line builders ------------------------------------------------
# These return plain dicts in the shape Claude Code writes to transcript JSONL,
# ready for ``Transcript.from_messages([...])``.


def raw_text_block(text: str) -> dict[str, Any]:
    """A raw ``text`` content-block dict."""
    return {"type": "text", "text": text}


def raw_tool_use(
    name: str,
    input: dict[str, Any] | None = None,
    id: str = "tu_x",
    **extra: Any,
) -> dict[str, Any]:
    """A raw ``tool_use`` content-block dict."""
    return {"type": "tool_use", "name": name, "input": input or {}, "id": id, **extra}


def raw_tool_result_block(
    tool_use_id: str = "tu_x",
    content: list[Any] | str = "ok",
    is_error: bool = False,
) -> dict[str, Any]:
    """A raw ``tool_result`` content-block dict."""
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content, "is_error": is_error}


def raw_msg(role: str, content: list[dict[str, Any]] | str = "", **raw: Any) -> dict[str, Any]:
    """A raw transcript line wrapping ``content`` (a block list or plain text) under ``message``."""
    blocks = content if isinstance(content, list) else [raw_text_block(content)]
    return {"type": role, "message": {"content": blocks}, **raw}


def raw_text(role: str, text: str) -> dict[str, Any]:
    """A raw transcript line carrying a single text block."""
    return raw_msg(role, text)


def raw_assistant(*blocks: dict[str, Any]) -> dict[str, Any]:
    """A raw assistant line wrapping the given content blocks (e.g. ``raw_tool_use(...)``)."""
    return raw_msg("assistant", list(blocks))


def raw_tool_msg(
    name: str,
    input: dict[str, Any] | None = None,
    id: str = "tu_x",
    **extra: Any,
) -> dict[str, Any]:
    """A raw assistant line carrying a single tool_use block (the common single-tool case)."""
    return raw_assistant(raw_tool_use(name, input, id, **extra))


def raw_tool_result(
    tool_use_id: str = "tu_x",
    content: list[Any] | str = "ok",
    tool_use_result: dict[str, Any] | None = None,
    is_error: bool = False,
) -> dict[str, Any]:
    """A raw user line carrying one tool_result block, with optional top-level ``toolUseResult``."""
    extra = {"toolUseResult": tool_use_result} if tool_use_result is not None else {}
    return raw_msg("user", [raw_tool_result_block(tool_use_id, content, is_error)], **extra)


def raw_notification(
    tool_use_id: str,
    task_id: str = "task-1",
    status: str = "completed",
    **extra: Any,
) -> dict[str, Any]:
    """A raw ``queue-operation`` line carrying a ``<task-notification>`` for ``tool_use_id``."""
    content = (
        "<task-notification>"
        f"<task-id>{task_id}</task-id>"
        f"<tool-use-id>{tool_use_id}</tool-use-id>"
        f"<status>{status}</status>"
        "</task-notification>"
    )
    return {"type": "queue-operation", "operation": "enqueue", "content": content, **extra}


def async_agent_launch(id: str = "tu_x", input: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """A two-line async Agent launch: tool_use + tool_result with ``isAsync`` toolUseResult."""
    return [
        raw_assistant(raw_tool_use("Agent", input or {"subagent_type": "general-purpose", "prompt": "y"}, id)),
        raw_tool_result(id, content=[], tool_use_result={"isAsync": True, "status": "async_launched", "agentId": "a"}),
    ]


def workflow_launch(id: str = "tu_x") -> list[dict[str, Any]]:
    """A two-line Workflow launch: tool_use + tool_result whose toolUseResult has no ``isAsync``."""
    return [
        raw_assistant(
            raw_tool_use(
                "Workflow",
                {"script": "export const meta = {name: 'transcript-probe'}\nlog('probe')\nreturn 1"},
                id,
                caller={"type": "direct"},
            )
        ),
        raw_tool_result(
            id,
            content=(
                "Workflow launched in background. Task ID: waux9o41y\n"
                "Summary: capture Workflow transcript shape\nRun ID: wf_a327db8a-07d"
            ),
            tool_use_result={
                "status": "async_launched",
                "taskId": "waux9o41y",
                "runId": "wf_a327db8a-07d",
                "summary": "capture Workflow transcript shape",
                "transcriptDir": "/tmp/transcripts",
                "scriptPath": "/tmp/script.ts",
            },
        ),
    ]


def make_transcript(*messages: dict[str, Any] | list[dict[str, Any]]) -> Transcript:
    """Build a ``Transcript`` from raw message dicts, given either as varargs or a single list."""
    items = messages[0] if len(messages) == 1 and isinstance(messages[0], list) else list(messages)
    return Transcript.from_messages(list(items))


def waiting_evt(raw_messages: list[dict[str, Any]]) -> PreToolUseEvent:
    """A Bash ``echo`` PreToolUse event over the given raw transcript, ready for ``check_condition``."""
    ctx = build_ctx(transcript=Transcript.from_messages(raw_messages))
    return make_pre_tool_event("Bash", {"command": "echo"}, ctx=ctx)


# --- TranscriptMessage-level factories --------------------------------------


def assistant_msg(*tools: tuple[str, dict[str, Any]]) -> TranscriptMessage:
    """An assistant ``TranscriptMessage`` whose content is a ToolUseBlock per ``(name, input)`` tuple."""
    return TranscriptMessage(
        type="assistant",
        content=[ToolUseBlock(name=name, input=inp, id=f"tu_{i}") for i, (name, inp) in enumerate(tools)],
    )


def text_msg(text: str = "done") -> TranscriptMessage:
    """An assistant ``TranscriptMessage`` carrying a single text block."""
    return TranscriptMessage(type="assistant", content=[TextBlock(text=text)])


def tool_result_msg(tool_use_id: str, text: str) -> TranscriptMessage:
    """A user ``TranscriptMessage`` carrying a single tool_result block."""
    return TranscriptMessage(
        type="user",
        content=[ToolResult(tool_use_id=tool_use_id, content=[{"type": "text", "text": text}])],
    )
