from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from captain_hook.app import get_matching_hooks
from captain_hook.dispatch import execute_hook
from captain_hook.session import SessionSlot
from captain_hook.types import Action, Event, HookResult, HookSpec, RegisteredHook
from captain_hook.tests.helpers import make_ctx, make_pre_tool_event


@pytest.fixture(autouse=True)
def isolate_failure_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from captain_hook.primitives import llm as llm_mod

    monkeypatch.setattr(llm_mod, "FAILURE_ROOT", tmp_path / "captain-hook-failures")


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
                "tool_input": {"file_path": "/tmp/example.py", "old_string": "a", "new_string": "b"},
            },
            ctx=make_ctx(project_root=Path("/tmp")),
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
        ctx = make_ctx(texts=["some reasoning"])
        ctx.call_llm = MagicMock(side_effect=RuntimeError("LLM timeout"))

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

    def test_prompt_check_log_renders_stderr_note(self, caplog: pytest.LogCaptureFixture) -> None:
        err = subprocess.CalledProcessError(1, ["claude"], output="", stderr="Invalid API key")
        err.add_note("stderr: Invalid API key")
        ctx = make_ctx(texts=["some reasoning"])
        ctx.call_llm = MagicMock(side_effect=err)

        from captain_hook.primitives.llm import prompt_check

        evt = MagicMock()
        evt.ctx = ctx

        with caplog.at_level(logging.WARNING, logger="captain_hook.primitives.llm"):
            result = prompt_check(evt, "Check {thing}", {"thing": "quality"}, prefix="TEST")

        assert result is None
        assert "Invalid API key" in caplog.text

    def test_prompt_check_log_surfaces_subprocess_stderr(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from captain_hook.context import HookContext
        from captain_hook.primitives.llm import prompt_check
        from captain_hook.session import SessionStore

        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/tmp")
        ctx = HookContext(session=SessionStore(None), transcript=MagicMock(spec=["path"], path=None), settings=None)
        marker = "stderr-marker-7f3a9c"
        monkeypatch.setenv("STDERR_MARKER", marker)
        ctx.call_llm = lambda *a, **kw: ctx.call_cli(
            ["sh", "-c", '>&2 printf "%s" "$STDERR_MARKER"; exit 1'],
        )

        evt = MagicMock()
        evt.ctx = ctx

        with caplog.at_level(logging.WARNING, logger="captain_hook.primitives.llm"):
            result = prompt_check(evt, "Check {thing}", {"thing": "quality"}, prefix="TEST")

        assert result is None
        assert isinstance(caplog.records[-1].exc_info[1], subprocess.CalledProcessError)
        assert marker in caplog.text


class TestPromptCheckDiagnostics:
    def test_called_process_error_writes_record_and_logs_fields(
        self, caplog: pytest.LogCaptureFixture, tmp_path: Path,
    ) -> None:
        import json as _json

        from captain_hook.primitives.llm import prompt_check

        argv = ["claude", "-p", "--model", "haiku", "--bare"]
        stdout_payload = ("S" * 5000) + "_TAIL_MARKER_"
        stderr_payload = "auth failed: bad key"
        err = subprocess.CalledProcessError(2, argv, output=stdout_payload, stderr=stderr_payload)
        ctx = make_ctx(texts=["agent did thing"])
        ctx.call_llm = MagicMock(side_effect=err)

        evt = MagicMock()
        evt.ctx = ctx

        long_template = "Check {thing}: " + ("P" * 1500) + "_PROMPT_TAIL_"
        with caplog.at_level(logging.WARNING, logger="captain_hook.primitives.llm"):
            result = prompt_check(evt, long_template, {"thing": "quality"}, prefix="TEST INTEGRITY")

        assert result is None

        log_text = caplog.text
        assert "TEST INTEGRITY" in log_text
        assert "argv:" in log_text and "claude" in log_text and "--bare" in log_text
        assert "exit_code: 2" in log_text
        assert "stderr: auth failed: bad key" in log_text
        assert "_TAIL_MARKER_" in log_text
        assert "stdout (tail 4KB):" in log_text
        assert "_PROMPT_TAIL_" in log_text
        assert "prompt (tail 1KB):" in log_text

        failure_files = list((tmp_path / "captain-hook-failures").rglob("*.json"))
        assert len(failure_files) == 1
        payload = _json.loads(failure_files[0].read_text())
        assert payload["argv"] == argv
        assert payload["exit_code"] == 2
        assert payload["stdout"] == stdout_payload
        assert payload["stderr"] == stderr_payload
        assert "_PROMPT_TAIL_" in payload["prompt"]
        assert payload["exception_type"] == "CalledProcessError"
        assert payload["prefix"] == "TEST INTEGRITY"

    def test_non_called_process_error_still_writes_record(
        self, caplog: pytest.LogCaptureFixture, tmp_path: Path,
    ) -> None:
        import json as _json

        from captain_hook.primitives.llm import prompt_check

        ctx = make_ctx()
        ctx.call_llm = MagicMock(side_effect=RuntimeError("schema validation blew up"))

        evt = MagicMock()
        evt.ctx = ctx

        with caplog.at_level(logging.WARNING, logger="captain_hook.primitives.llm"):
            result = prompt_check(evt, "Check {thing}", {"thing": "quality"}, prefix="TEST QUALITY")

        assert result is None
        failure_files = list((tmp_path / "captain-hook-failures").rglob("*.json"))
        assert len(failure_files) == 1
        payload = _json.loads(failure_files[0].read_text())
        assert payload["exception_type"] == "RuntimeError"
        assert payload["argv"] is None
        assert payload["exit_code"] is None
        assert "schema validation blew up" in payload["exception_str"]
