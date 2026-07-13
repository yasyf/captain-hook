from __future__ import annotations

import os
import subprocess
import sys
from itertools import count
from pathlib import Path
from typing import TYPE_CHECKING, Any
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
    disk_fixture_session as disk_fixture_session,
)
from captain_hook.testing.helpers import (
    fixture_session as fixture_session,
)
from captain_hook.testing.helpers import (
    input_to_event as input_to_event,
)
from captain_hook.testing.helpers import (
    mock_event as mock_event,
)
from captain_hook.testing.helpers import (
    mock_session_end_event as mock_session_end_event,
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
from captain_hook.types import Event

if TYPE_CHECKING:
    from cc_transcript.query import Session

PKG_DIR = Path(__file__).resolve().parents[3]
UUIDS = count(1)


def dispatch_test(
    event: str | Event,
    *,
    tool: str | None = None,
    command: str | None = None,
    file: str | None = None,
    content: str | None = None,
    old: str | None = None,
    prompt: str | None = None,
    transcript: Session | None = None,
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
    text_msgs = [raw_text("assistant", t) for t in (texts or [])]
    padding = [raw_text("assistant", "") for _ in range(max(0, n_messages - len(text_msgs)))]
    ctx = HookContext(
        session=SessionStore(session_dir),
        transcript=fixture_session(padding + text_msgs),
        settings=settings,
        project_root=project_root,
    )
    ctx.call_llm = MagicMock(return_value=call_llm_return)  # type: ignore[method-assign]
    return ctx


def build_ctx(
    transcript: Session | None = None,
    session_dir: Any = None,
    settings: Any = None,
    project_root: Path | None = None,
) -> HookContext:
    return HookContext(
        session=SessionStore(session_dir),
        transcript=transcript if transcript is not None else fixture_session([]),
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
    *,
    permission_mode: str | None = None,
) -> PreToolUseEvent:
    return PreToolUseEvent(
        _raw={"tool_name": tool_name}
        | ({"tool_input": tool_input} if tool_input is not None else {})
        | ({"permission_mode": permission_mode} if permission_mode is not None else {}),
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
    counts = count_tools_map or {}
    ctx = MagicMock()
    (transcript := ctx.transcript)  # noqa: E275 — walrus for inline binding
    ctx.t = transcript
    transcript.has_skill = MagicMock(return_value=has_skill_result)
    transcript.has_read = MagicMock(return_value=has_read_result)
    transcript.has_edit_to = MagicMock(return_value=has_edit_to_result)
    transcript.has_command = MagicMock(return_value=has_command_result)
    transcript.tool_calls.named = MagicMock(
        side_effect=lambda spec: MagicMock(count=MagicMock(return_value=counts.get(spec, 0)))
    )
    return ctx


def make_messages_ctx(messages: list[dict[str, Any]] | None) -> Any:
    return MagicMock(transcript=disk_fixture_session(messages or []), settings=None)


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
# ready for ``fixture_session([...])``.


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
    """A raw transcript line wrapping ``content`` (a block list or plain text) under ``message``.

    Carries the envelope metadata (``uuid``/``sessionId``/``timestamp``) that real
    transcript lines have, so the same dict parses through the core parser whether
    written to a JSONL file or handed to ``fixture_session``. Each line gets a
    unique ``uuid``; callers can override any envelope field via keyword.
    """
    blocks = content if isinstance(content, list) else [raw_text_block(content)]
    message = {"content": blocks} | ({"model": "<test>"} if role == "assistant" else {})
    return {
        "type": role,
        "uuid": f"uuid-{next(UUIDS)}",
        "sessionId": "sess",
        "timestamp": "2026-01-01T00:00:00Z",
        "message": message,
    } | raw


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


def notification_text(tool_use_id: str, task_id: str = "task-1", status: str = "completed") -> str:
    """The ``<task-notification>`` payload text for ``tool_use_id``."""
    return (
        "<task-notification>"
        f"<task-id>{task_id}</task-id>"
        f"<tool-use-id>{tool_use_id}</tool-use-id>"
        f"<status>{status}</status>"
        "</task-notification>"
    )


def raw_queue_op(operation: str, content: str = "") -> dict[str, Any]:
    """A raw ``queue-operation`` audit line — one of ``enqueue``/``dequeue``/``remove``/``popAll``."""
    return {"type": "queue-operation", "operation": operation, "content": content}


def raw_notification(
    tool_use_id: str,
    task_id: str = "task-1",
    status: str = "completed",
) -> list[dict[str, Any]]:
    """The realistic notification lifecycle: enqueue, dequeue, then user-event delivery.

    Models a completion notification that reached the agent — enqueued, drained
    from the queue, and delivered as a user turn carrying the ``<task-notification>``.
    """
    text = notification_text(tool_use_id, task_id, status)
    return [raw_queue_op("enqueue", text), raw_queue_op("dequeue"), raw_text("user", text)]


def raw_notification_enqueued(
    tool_use_id: str,
    task_id: str = "task-1",
    status: str = "completed",
) -> list[dict[str, Any]]:
    """An enqueue-only notification: queued for delivery but never drained (the misfire shape)."""
    return [raw_queue_op("enqueue", notification_text(tool_use_id, task_id, status))]


def raw_notification_attachment(
    tool_use_id: str,
    task_id: str = "task-1",
    status: str = "completed",
) -> list[dict[str, Any]]:
    """Delivery via a ``queued_command`` attachment: a ``remove`` queue op then the attachment event."""
    return [
        raw_queue_op("remove"),
        {
            "type": "attachment",
            "attachment": {"type": "queued_command", "prompt": notification_text(tool_use_id, task_id, status)},
        },
    ]


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


def make_transcript(*messages: dict[str, Any] | list[dict[str, Any]]) -> Session:
    """Build a ``Session`` from raw message dicts, given either as varargs or a single list."""
    items = messages[0] if len(messages) == 1 and isinstance(messages[0], list) else list(messages)
    return fixture_session(list(items))


def waiting_evt(raw_messages: list[dict[str, Any]]) -> PreToolUseEvent:
    """A Bash ``echo`` PreToolUse event over the given raw transcript, ready for ``check_condition``."""
    ctx = build_ctx(transcript=disk_fixture_session(raw_messages))
    return make_pre_tool_event("Bash", {"command": "echo"}, ctx=ctx)


def waiting_stop_evt(
    raw: dict[str, Any] | None = None,
    raw_messages: list[dict[str, Any]] | None = None,
) -> StopEvent:
    """A ``StopEvent`` carrying ``raw`` (e.g. ``background_tasks``/``session_crons``) over the given transcript."""
    ctx = build_ctx(transcript=disk_fixture_session(raw_messages or []))
    return StopEvent(_raw=raw or {}, ctx=ctx)


# --- Single-line message factories -------------------------------------------


def assistant_msg(*tools: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    """A raw assistant line carrying a tool_use block per ``(name, input)`` tuple."""
    return raw_assistant(*(raw_tool_use(name, inp, f"tu_{i}") for i, (name, inp) in enumerate(tools)))


def text_msg(text: str = "done") -> dict[str, Any]:
    """A raw assistant line carrying a single text block."""
    return raw_text("assistant", text)


def tool_result_msg(tool_use_id: str, text: str) -> dict[str, Any]:
    """A raw user line carrying a single tool_result block."""
    return raw_tool_result(tool_use_id, content=[{"type": "text", "text": text}])
