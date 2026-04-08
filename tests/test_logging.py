from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from captain_hook.app import (
    _state,
    get_matching_hooks,
    hook as register_hook,
    on,
    register,
    reset,
)
from captain_hook.dispatch import execute_hook
from captain_hook.events import PreToolUseEvent
from captain_hook.session import SessionSlot
from captain_hook.types import Action, Event, HookResult, HookSpec, RegisteredHook


def make_ctx() -> Any:
    ctx = MagicMock()
    ctx.transcript = MagicMock()
    ctx.transcript.has_skill = MagicMock(return_value=False)
    ctx.transcript.has_read = MagicMock(return_value=False)
    ctx.transcript.has_edit_to = MagicMock(return_value=False)
    ctx.transcript.has_command = MagicMock(return_value=False)
    ctx.transcript.count_tools = MagicMock(return_value=0)
    ctx.session = MagicMock()
    return ctx


def make_pre_tool_event() -> PreToolUseEvent:
    return PreToolUseEvent(_raw={"tool_name": "Bash"}, ctx=make_ctx())

@pytest.fixture(autouse=True)
def _clean_state():
    reset()
    yield
    reset()



class TestDispatchLogging:
    def test_execute_hook_logs_on_handler_crash(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        def handler(evt: Any) -> HookResult:
            raise RuntimeError("boom")

        entry = RegisteredHook(
            spec=HookSpec(events=Event.PreToolUse),
            handler=handler,
            name="crashing_hook",
        )
        with caplog.at_level(logging.WARNING, logger="captain_hook.dispatch"):
            result = execute_hook(entry, make_pre_tool_event(), tmp_path)

        assert result is None
        assert any("crashing_hook" in r.message and r.levelno >= logging.WARNING for r in caplog.records)


class TestSessionSlotLogging:
    def test_get_logs_on_corrupt_json(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        from captain_hook.state import HookState

        slot = SessionSlot(tmp_path, HookState)
        assert slot.path is not None
        slot.path.write_text("not valid json!!!")

        with caplog.at_level(logging.WARNING, logger="captain_hook.session"):
            result = slot.get()

        assert result is None
        assert any(r.levelno >= logging.WARNING for r in caplog.records)

    def test_set_logs_on_os_error(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        from unittest.mock import patch

        from captain_hook.state import HookState

        slot = SessionSlot(tmp_path, HookState)
        state = HookState()

        with (
            patch.object(Path, "mkdir", side_effect=OSError("disk full")),
            caplog.at_level(logging.WARNING, logger="captain_hook.session"),
        ):
            slot.set(state)

        assert any("disk full" in r.message or r.exc_info for r in caplog.records)


class TestLintLogging:
    def test_lint_handler_logs_instead_of_stderr(self, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]) -> None:
        from captain_hook.primitives.lint import lint

        def bad_check(content: str) -> list[str]:
            raise ValueError("check exploded")

        lint(bad_check, message="Violations: {violations}")

        from captain_hook.events import PostToolUseEvent

        evt = PostToolUseEvent(
            _raw={
                "tool_name": "Edit",
                "tool_input": {"file_path": "/tmp/test.py", "old_string": "a", "new_string": "b"},
            },
            ctx=make_ctx(),
        )

        matching = get_matching_hooks(evt)
        assert matching, "Expected lint hook to match"

        with caplog.at_level(logging.WARNING, logger="captain_hook.primitives.lint"):
            result = matching[0].handler(evt) if matching[0].handler else None

        assert result is None
        assert any(r.levelno >= logging.WARNING for r in caplog.records)
        captured = capsys.readouterr()
        assert "Traceback" not in captured.err
class TestLlmLogging:
    def test_llm_evaluate_logs_on_exception(self, caplog: pytest.LogCaptureFixture) -> None:
        ctx = make_ctx()
        ctx.call_llm = MagicMock(side_effect=RuntimeError("LLM down"))

        evt = MagicMock()
        evt.ctx = ctx

        with (
            patch("captain_hook.primitives.llm.fired_this_turn", return_value=False),
            caplog.at_level(logging.WARNING, logger="captain_hook.primitives.llm"),
        ):
            from captain_hook.primitives.llm import GateVerdict, llm_evaluate

            result = llm_evaluate(
                evt,
                "test prompt for evaluation",
                GateVerdict,
                when=lambda _: True,
            )

        assert result is None
        assert any(r.levelno >= logging.WARNING for r in caplog.records)

    def test_prompt_check_logs_on_exception(self, caplog: pytest.LogCaptureFixture) -> None:
        ctx = make_ctx()
        ctx.call_llm = MagicMock(side_effect=RuntimeError("LLM timeout"))
        ctx.t = MagicMock()
        ctx.t.recent.return_value = MagicMock()
        ctx.t.recent.return_value.assistant_text.return_value = "some reasoning"

        from captain_hook.primitives.llm import prompt_check

        evt = MagicMock()
        evt.ctx = ctx

        with caplog.at_level(logging.WARNING, logger="captain_hook.primitives.llm"):
            result = prompt_check(
                evt,
                "Check {thing}",
                {"thing": "quality"},
                prefix="TEST",
            )

        assert result is None
        assert any(r.levelno >= logging.WARNING for r in caplog.records)
