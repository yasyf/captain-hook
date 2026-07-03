from __future__ import annotations

import atexit
import re
import shutil
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from itertools import count
from pathlib import Path
from typing import Any, cast

from cc_transcript.parser import parse_event
from cc_transcript.query import Session
from pydantic import BaseModel

from captain_hook.conditions import matches_conditions
from captain_hook.context import HookContext, lift_session, load_transcript
from captain_hook.dispatch import execute_hook
from captain_hook.events import (
    BaseHookEvent,
    SessionEndEvent,
    SessionStartEvent,
    StopEvent,
    SubagentStartEvent,
    SubagentStopEvent,
    ToolHookEvent,
    UserPromptSubmitEvent,
)
from captain_hook.session import SessionStore
from captain_hook.testing.session_cache import SessionCache
from captain_hook.testing.types import Allow, Block, FileFixture, Input, Rewrite, TranscriptFixture, Warn
from captain_hook.types import Event, HookResult, Tool

STUB_FIELD_VALUES: dict[str, Any] = {
    "block": True,
    "fire": True,
    "action": "block",
    "reasoning": "inline test stub",
    "reason": "inline test stub",
}

FIXTURE_FILE_DIR: list[Path] = []
FIXTURE_FILE_COUNTER = count()


def fixture_file_dir() -> Path:
    if not FIXTURE_FILE_DIR:
        root = Path(tempfile.mkdtemp(prefix="capt-hook-fixture-"))
        FIXTURE_FILE_DIR.append(root)
        atexit.register(shutil.rmtree, root, ignore_errors=True)
    return FIXTURE_FILE_DIR[0]


def materialize_file(fixture: FileFixture) -> str:
    path = fixture_file_dir() / (fixture.name or f"fixture-{next(FIXTURE_FILE_COUNTER)}")
    path.write_bytes(fixture.content.encode() if fixture.content is not None else b"x" * (fixture.size or 0))
    return str(path)


def fixture_line(index: int, message: dict[str, Any]) -> dict[str, Any]:
    line = {"sessionId": "fixture", "timestamp": "2026-01-01T00:00:00Z"} | {"uuid": f"fixture-{index}"} | dict(message)
    match line:
        case {"type": "assistant", "message": dict() as inner} if "model" not in inner:
            return line | {"message": {"model": "<fixture>"} | inner}
        case _:
            return line


def fixture_session(messages: list[dict[str, Any]], *, path: Path | None = None) -> Session:
    """Build a ``Session`` from raw transcript-line dicts, synthesizing missing envelope fields."""
    return lift_session(
        [
            event
            for index, message in enumerate(messages)
            if (event := parse_event(fixture_line(index, message))) is not None
        ],
        path=path,
    )


def build_context(
    transcript: Session | None = None,
    transcript_path: str | Path | None = None,
    session_dir: Path | None = None,
    project_root: Path | None = None,
) -> HookContext:
    return HookContext(
        session=SessionStore(session_dir),
        transcript=transcript if transcript is not None else load_transcript(transcript_path),
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
    offset: int | None = None,
    limit: int | None = None,
    agent_type: str | None = None,
    model: str | None = None,
    prompt: str | None = None,
    script: str | None = None,
    tool_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if tool_input is not None:
        return tool_input
    match tool:
        case "Bash" | "Execute":
            return omit_none({"command": command, "timeout": None, "description": None})
        case "Edit":
            return {"file_path": file or "", "old_string": old or "", "new_string": content or ""}
        case "MultiEdit":
            return {"file_path": file or "", "edits": [{"old_string": old or "", "new_string": content or ""}]}
        case "Write" | "Create":
            return {"file_path": file or "", "content": content or ""}
        case "Read":
            return omit_none({"file_path": file or "", "limit": limit, "offset": offset})
        case "Agent" | "Task":
            return omit_none({"prompt": prompt or content or "", "subagent_type": agent_type, "model": model})
        case "Workflow":
            return omit_none({"script": script})
        case _:
            return omit_none({"command": command, "file_path": file, "new_string": content, "old_string": old})


def infer_tool(inp: Input) -> str:
    """Infer the tool for a tool event from which ``Input`` fields are populated.

    Used only when neither ``Input.tool`` nor a ``Tool`` condition pins it — so a field the
    author set (``content``, ``script``, …) is never silently dropped onto a default ``Bash``
    input. Falls back to ``Bash`` when no tool-shaping field is set (nothing to drop).
    """
    if inp.script is not None:
        return "Workflow"
    if inp.old is not None:
        return "Edit"
    if inp.content is not None:
        return "Write"
    if inp.prompt is not None or inp.model is not None or inp.agent_type is not None:
        return "Task"
    if inp.offset is not None or inp.limit is not None:
        return "Read"
    if inp.command is not None:
        return "Bash"
    return "Read" if inp.file is not None else "Bash"


def mock_tool_event(
    tool: str = "Bash",
    *,
    event: Event = Event.PreToolUse,
    command: str | None = None,
    file: str | None = None,
    content: str | None = None,
    old: str | None = None,
    offset: int | None = None,
    limit: int | None = None,
    agent_type: str | None = None,
    model: str | None = None,
    prompt: str | None = None,
    script: str | None = None,
    output: str | None = None,
    error: str | None = None,
    tool_input: dict[str, Any] | None = None,
    permission_mode: str | None = None,
    transcript: Session | None = None,
    transcript_path: str | Path | None = None,
    session_dir: Path | None = None,
    project_root: Path | None = None,
) -> ToolHookEvent:
    return cast(
        ToolHookEvent,
        event.event_class(
            _raw={
                "tool_name": tool,
                "tool_input": make_tool_input(
                    tool, command, file, content, old, offset, limit, agent_type, model, prompt, script, tool_input
                ),
            }
            | ({"agent_type": agent_type} if agent_type else {})
            | ({"permission_mode": permission_mode} if permission_mode else {})
            | ({"tool_response": output} if output is not None else {})
            | ({"error": error} if error is not None else {}),
            ctx=build_context(transcript, transcript_path, session_dir, project_root),
        ),
    )


def mock_stop_event(
    *,
    stop_hook_active: bool = False,
    permission_mode: str | None = None,
    transcript: Session | None = None,
    transcript_path: str | Path | None = None,
    session_dir: Path | None = None,
) -> StopEvent:
    return StopEvent(
        _raw={"stop_hook_active": stop_hook_active} | ({"permission_mode": permission_mode} if permission_mode else {}),
        ctx=build_context(transcript, transcript_path, session_dir),
    )


def mock_session_end_event(
    reason: str = "other",
    *,
    permission_mode: str | None = None,
    transcript: Session | None = None,
    transcript_path: str | Path | None = None,
    session_dir: Path | None = None,
) -> SessionEndEvent:
    return SessionEndEvent(
        _raw={"reason": reason} | ({"permission_mode": permission_mode} if permission_mode else {}),
        ctx=build_context(transcript, transcript_path, session_dir),
    )


def mock_session_start_event(
    source: str = "startup",
    *,
    permission_mode: str | None = None,
    transcript: Session | None = None,
    transcript_path: str | Path | None = None,
    session_dir: Path | None = None,
) -> SessionStartEvent:
    return SessionStartEvent(
        _raw={"source": source} | ({"permission_mode": permission_mode} if permission_mode else {}),
        ctx=build_context(transcript, transcript_path, session_dir),
    )


def mock_subagent_stop_event(
    agent_type: str = "",
    *,
    agent_id: str = "",
    stop_hook_active: bool = False,
    agent_transcript_path: str = "",
    permission_mode: str | None = None,
    transcript: Session | None = None,
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
    transcript: Session | None = None,
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
    transcript: Session | None = None,
    transcript_path: str | Path | None = None,
    session_dir: Path | None = None,
) -> UserPromptSubmitEvent:
    return UserPromptSubmitEvent(
        _raw={"prompt": prompt} | ({"permission_mode": permission_mode} if permission_mode else {}),
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
    transcript: Session | None = None,
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
        case Event.SessionStart:
            return mock_session_start_event(source=extra.pop("source", "startup"), **ctx_kw)
        case Event.SessionEnd:
            return mock_session_end_event(reason=extra.pop("reason", "other"), **ctx_kw)
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
            transcript, transcript_path = fixture_session(tf.messages), None
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
            evt = mock_subagent_stop_event(agent_type=inp.agent_type or "", **ctx_kw)
        case Event.SubagentStart:
            evt = mock_subagent_start_event(agent_type=inp.agent_type or "", **ctx_kw)
        case Event.UserPromptSubmit:
            evt = mock_user_prompt_event(prompt=inp.prompt or "", **ctx_kw)
        case Event.Stop:
            evt = mock_stop_event(**ctx_kw)
        case Event.SessionStart:
            evt = mock_session_start_event(source=inp.source or "startup", **ctx_kw)
        case Event.SessionEnd:
            evt = mock_session_end_event(reason=inp.reason or "other", **ctx_kw)
        case _:
            file = materialize_file(inp.file) if isinstance(inp.file, FileFixture) else inp.file
            evt = mock_tool_event(
                tool=inp.tool or spec_tool or infer_tool(inp),
                event=ev,
                command=inp.command,
                file=file,
                content=inp.content,
                old=inp.old,
                offset=inp.offset,
                limit=inp.limit,
                agent_type=inp.agent_type,
                model=inp.model,
                prompt=inp.prompt,
                script=inp.script,
                output=inp.output,
                error=inp.error,
                tool_input=inp.tool_input,
                **ctx_kw,
            )

    if inp.tasks is not None:
        from captain_hook.tasks import Task, Tasks

        # Pre-populate the `tasks` cached_property so hooks that read `evt.tasks` see the
        # injected list in inline tests — no native task store / session_id required.
        evt.__dict__["tasks"] = Tasks(tuple(Task.from_raw(t) for t in inp.tasks))

    return evt


def replay_session(entry: Any, jsonl: Path) -> Iterator[HookResult | None]:
    transcript = load_transcript(jsonl)
    ctx = HookContext(session=SessionStore(None), transcript=transcript, settings=None)
    ctx.call_llm = stub_call_llm  # type: ignore[method-assign]
    transcript_path = str(jsonl)

    for ev_type in entry.spec.events:
        for raw in transcript_event_payloads(ev_type, transcript, transcript_path):
            yield execute_hook(entry, ev_type.event_class(_raw=raw, ctx=ctx))


def transcript_event_payloads(
    ev_type: Event,
    transcript: Session,
    transcript_path: str,
) -> Iterator[dict[str, Any]]:
    base = {"transcript_path": transcript_path}
    match ev_type:
        case Event.PreToolUse:
            yield from (
                base | {"tool_name": use.call.name, "tool_input": dict(use.call.raw)} for use in transcript.tool_calls
            )
        case Event.PostToolUse:
            yield from (
                base
                | {"tool_name": use.call.name, "tool_input": dict(use.call.raw)}
                | ({"tool_response": use.result.content} if use.result and not use.result.is_error else {})
                for use in transcript.tool_calls
            )
        case Event.PostToolUseFailure:
            yield from (
                base | {"tool_name": use.call.name, "tool_input": dict(use.call.raw), "error": use.result.content}
                for use in transcript.tool_calls.failed()
                if use.result
            )
        case Event.UserPromptSubmit:
            yield from (base | {"prompt": turn.prompt} for turn in transcript.turns if turn.prompt)
        case Event.SessionStart:
            yield base | {"source": "startup"}
        case Event.SessionEnd:
            yield base | {"reason": "other"}
        case Event.Stop | Event.SubagentStop | Event.SubagentStart | Event.Notification | Event.PreCompact:
            yield base


def matches_expected(result: HookResult | None, expected: Block | Warn | Allow | Rewrite) -> bool:
    match expected:
        case Allow():
            return result is None or result.action == "allow"
        case Block(pattern=pat):
            return (
                result is not None
                and result.action == "block"
                and (not pat or bool(result.message and re.search(pat, result.message)))
            )
        case Warn(pattern=pat):
            return (
                result is not None
                and result.action == "warn"
                and (not pat or bool(result.message and re.search(pat, result.message)))
            )
        case Rewrite(pattern=pat, fields=fields):
            if result is None or result.action != "rewrite":
                return False
            ui = result.updated_input or {}
            return (not pat or pat in str(ui.get("command", ""))) and all(
                val in str(ui.get(key, "")) for key, val in fields
            )


def assert_result(
    result: HookResult | None,
    expected: Block | Warn | Allow | Rewrite,
    hook_name: str = "",
) -> None:
    prefix = f"[{hook_name}] " if hook_name else ""
    match expected:
        case Allow():
            assert result is None or result.action == "allow", f"{prefix}Expected Allow, got {result}"
        case Block(pattern=pat):
            assert result is not None and result.action == "block", f"{prefix}Expected Block, got {result}"
            if pat:
                assert result.message and re.search(pat, result.message), (
                    f"{prefix}Block message {result.message!r} doesn't match '{pat}'"
                )
        case Warn(pattern=pat):
            assert result is not None and result.action == "warn", f"{prefix}Expected Warn, got {result}"
            if pat:
                assert result.message and re.search(pat, result.message), (
                    f"{prefix}Warn message {result.message!r} doesn't match '{pat}'"
                )
        case Rewrite(pattern=pat, fields=fields):
            assert result is not None and result.action == "rewrite", f"{prefix}Expected Rewrite, got {result}"
            ui = result.updated_input or {}
            if pat:
                command = str(ui.get("command", ""))
                assert pat in command, f"{prefix}Rewrite command {command!r} doesn't contain '{pat}'"
            for key, val in fields:
                actual = str(ui.get(key, ""))
                assert val in actual, f"{prefix}Rewrite {key}={actual!r} doesn't contain '{val}'"


def make_stub_call_llm(overrides: dict[str, Any] | None = None) -> Callable[..., str | BaseModel]:
    values = STUB_FIELD_VALUES | (overrides or {})

    def stub_call_llm(
        _template: Any,
        *args: Any,
        response_model: type[BaseModel] | None = None,
        **kwargs: Any,
    ) -> str | BaseModel:
        if response_model is None:
            return "stubbed"
        return response_model(
            **{
                name: values.get(name, "")
                for name, info in response_model.model_fields.items()
                if name in values or info.default is None
            }
        )

    return stub_call_llm


stub_call_llm = make_stub_call_llm()


@contextmanager
def isolated_durable_root() -> Iterator[Path]:
    """Point ``DurableState`` at a throwaway root so tests never read or write the real store."""
    from captain_hook.durable import DURABLE_ROOT_OVERRIDE

    root = Path(tempfile.mkdtemp(prefix="capt-hook-durable-"))
    DURABLE_ROOT_OVERRIDE.append(root)
    try:
        yield root
    finally:
        DURABLE_ROOT_OVERRIDE.remove(root)
        shutil.rmtree(root, ignore_errors=True)


def run_inline_tests() -> list[tuple[str, str, bool, str]]:
    from captain_hook.app import _state, is_planning_agent_skip

    results: list[tuple[str, str, bool, str]] = []

    with isolated_durable_root():
        for entry in _state.hooks:
            if not entry.spec.tests:
                continue
            for key, expected in entry.spec.tests.items():
                test_name = f"{entry.name}:{key!r}"
                try:
                    if isinstance(key, Input):
                        # A Tool condition honors infer_tool's shape derivation when it lands on a
                        # named tool, else pins the first named tool (families infer_tool can't
                        # shape, e.g. WebFetch/WebSearch). No Tool condition => pure inference.
                        spec_tools = [p for c in entry.spec.only_if if isinstance(c, Tool) for p in c.names]
                        evt = input_to_event(
                            next(iter(entry.spec.events)),
                            key,
                            (inferred if (inferred := infer_tool(key)) in spec_tools else spec_tools[0])
                            if spec_tools
                            else None,
                        )
                        evt.ctx.call_llm = make_stub_call_llm(key.llm)  # type: ignore[assignment]
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
