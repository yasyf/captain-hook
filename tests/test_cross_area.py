from __future__ import annotations

import json
import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Any

import pytest

from captain_hook.app import (
    _state,
    on,
)
from captain_hook.app import (
    hook as register_hook,
)
from captain_hook.command import CommandLine
from captain_hook.context import HookContext
from captain_hook.dispatch import dispatch
from captain_hook.events import (
    BaseHookEvent,
    PreToolUseEvent,
)
from captain_hook.loader import discover_hooks
from captain_hook.session import SessionStore
from captain_hook.signals import score_signals
from captain_hook.signals.nlp import Clause, NlpSignal, Phrase
from captain_hook.state import HookState
from captain_hook.testing.helpers import run_inline_tests
from captain_hook.testing.types import Allow, Block, Input
from captain_hook.tests.helpers import (
    build_ctx,
    dispatch_test,
    make_transcript,
    mock_tool_event,
)
from captain_hook.tests.helpers import (
    raw_text as msg,
)
from captain_hook.tests.helpers import (
    raw_tool_msg as toolmsg,
)
from captain_hook.tests.helpers import (
    raw_tool_result as tool_resultmsg,
)
from cc_transcript.activity import SessionActivity

from captain_hook.testing.helpers import fixture_session
from captain_hook.types import (
    Action,
    Command,
    Event,
    FilePath,
    HookResult,
    InPlanMode,
    RanCommand,
    ReadFile,
    Signal,
    Signals,
    Tool,
    TouchedFile,
    UsedSkill,
)
from captain_hook.primitives.workflow import Step, text_matches, workflow


class TestDeclarativeHookE2E:
    """VAL-CROSS-001"""

    def test_declarative_block_produces_deny_json(self) -> None:
        register_hook(
            Event.PreToolUse,
            message="blocked",
            block=True,
            only_if=[Tool("Bash"), Command(r"rm\s+-rf")],
        )
        raw = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}
        ctx = build_ctx()
        evt = PreToolUseEvent(_raw=raw, ctx=ctx)
        output = dispatch(Event.PreToolUse, evt)

        assert output is not None
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "blocked" in output["hookSpecificOutput"]["permissionDecisionReason"]

    def test_nonmatching_command_passes(self) -> None:
        register_hook(
            Event.PreToolUse,
            message="blocked",
            block=True,
            only_if=[Tool("Bash"), Command(r"rm\s+-rf")],
        )
        raw = {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}
        ctx = build_ctx()
        evt = PreToolUseEvent(_raw=raw, ctx=ctx)
        assert dispatch(Event.PreToolUse, evt) is None
class TestHandlerHookE2E:
    """VAL-CROSS-002"""

    def test_handler_warn_produces_allow_json(self) -> None:

        @on(Event.PreToolUse, only_if=[Tool("Edit"), FilePath("*.py")])
        def check_style(evt: BaseHookEvent) -> HookResult | None:
            return HookResult(action=Action.warn, message="check style")

        raw = {"tool_name": "Edit", "tool_input": {"file_path": "foo.py", "old_string": "", "new_string": "x"}}
        ctx = build_ctx()
        evt = PreToolUseEvent(_raw=raw, ctx=ctx)
        output = dispatch(Event.PreToolUse, evt)

        assert output is not None
        assert output["hookSpecificOutput"]["additionalContext"] == "check style"
        assert output["hookSpecificOutput"]["permissionDecision"] == "allow"
    def test_nonmatching_file_passes(self) -> None:

        @on(Event.PreToolUse, only_if=[Tool("Edit"), FilePath("*.py")])
        def check_style(evt: BaseHookEvent) -> HookResult | None:
            return HookResult(action=Action.warn, message="check style")

        raw = {"tool_name": "Edit", "tool_input": {"file_path": "foo.js", "old_string": "", "new_string": "x"}}
        ctx = build_ctx()
        evt = PreToolUseEvent(_raw=raw, ctx=ctx)
        assert dispatch(Event.PreToolUse, evt) is None

    def test_external_absolute_file_passes_by_default(self, tmp_path: Path) -> None:

        @on(Event.PreToolUse, only_if=[Tool("Write"), FilePath("*.py")])
        def check_style(evt: BaseHookEvent) -> HookResult | None:
            return HookResult(action=Action.warn, message="check style")

        raw = {"tool_name": "Write", "tool_input": {"file_path": "/tmp/regex_check.py", "content": "x"}}
        ctx = build_ctx(project_root=tmp_path)
        evt = PreToolUseEvent(_raw=raw, ctx=ctx)
        assert dispatch(Event.PreToolUse, evt) is None

    def test_in_project_absolute_file_matches_default(self, tmp_path: Path) -> None:

        @on(Event.PreToolUse, only_if=[Tool("Write"), FilePath("*.py")])
        def check_style(evt: BaseHookEvent) -> HookResult | None:
            return HookResult(action=Action.warn, message="check style")

        raw = {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "regex_check.py"), "content": "x"}}
        ctx = build_ctx(project_root=tmp_path)
        evt = PreToolUseEvent(_raw=raw, ctx=ctx)
        output = dispatch(Event.PreToolUse, evt)

        assert output is not None
        assert output["hookSpecificOutput"]["additionalContext"] == "check style"
        assert output["hookSpecificOutput"]["permissionDecision"] == "allow"
class TestInPlanModeCondition:
    """VAL-CROSS-003"""

    def test_fires_when_enter_gt_exit(self) -> None:
        transcript = make_transcript(
            [
                toolmsg("ExitPlanMode", {}, "ep1"),
                tool_resultmsg("ep1"),
                toolmsg("EnterPlanMode", {}, "en1"),
                tool_resultmsg("en1"),
                toolmsg("EnterPlanMode", {}, "en2"),
                tool_resultmsg("en2"),
            ]
        )
        register_hook(Event.PreToolUse, message="in plan mode", block=True, only_if=[InPlanMode()])
        raw = {"tool_name": "Edit", "tool_input": {"file_path": "x.py", "old_string": "", "new_string": ""}}
        ctx = build_ctx(transcript=transcript)
        evt = PreToolUseEvent(_raw=raw, ctx=ctx)
        assert dispatch(Event.PreToolUse, evt) is not None
    def test_does_not_fire_when_equal(self) -> None:
        transcript = make_transcript(
            [
                toolmsg("EnterPlanMode", {}, "en1"),
                tool_resultmsg("en1"),
                toolmsg("ExitPlanMode", {}, "ep1"),
                tool_resultmsg("ep1"),
            ]
        )
        register_hook(Event.PreToolUse, message="in plan mode", block=True, only_if=[InPlanMode()])
        raw = {"tool_name": "Edit", "tool_input": {"file_path": "x.py", "old_string": "", "new_string": ""}}
        ctx = build_ctx(transcript=transcript)
        evt = PreToolUseEvent(_raw=raw, ctx=ctx)
        assert dispatch(Event.PreToolUse, evt) is None
class TestReadFileCondition:
    """VAL-CROSS-004"""

    def test_skip_if_skips_when_read(self) -> None:
        transcript = make_transcript(
            [
                toolmsg("Read", {"file_path": "/repo/STYLEGUIDE.md"}, "r1"),
                tool_resultmsg("r1"),
            ]
        )
        register_hook(Event.PreToolUse, message="read first", block=True, skip_if=[ReadFile("STYLEGUIDE.md")])
        raw = {"tool_name": "Edit", "tool_input": {"file_path": "x.py", "old_string": "", "new_string": ""}}
        ctx = build_ctx(transcript=transcript)
        evt = PreToolUseEvent(_raw=raw, ctx=ctx)
        assert dispatch(Event.PreToolUse, evt) is None
    def test_fires_when_not_read(self) -> None:
        transcript = make_transcript([msg("user", "hello")])
        register_hook(Event.PreToolUse, message="read first", block=True, skip_if=[ReadFile("STYLEGUIDE.md")])
        raw = {"tool_name": "Edit", "tool_input": {"file_path": "x.py", "old_string": "", "new_string": ""}}
        ctx = build_ctx(transcript=transcript)
        evt = PreToolUseEvent(_raw=raw, ctx=ctx)
        assert dispatch(Event.PreToolUse, evt) is not None
class TestTouchedFileCondition:
    """VAL-CROSS-005"""

    def test_matches_glob(self) -> None:
        transcript = make_transcript(
            [
                toolmsg("Edit", {"file_path": "www/src/page.tsx", "old_string": "", "new_string": "x"}, "e1"),
                tool_resultmsg("e1"),
            ]
        )
        register_hook(Event.PreToolUse, message="www touched", block=True, only_if=[TouchedFile("www/src/*")])
        raw = {"tool_name": "Bash", "tool_input": {"command": "echo hi"}}
        ctx = build_ctx(transcript=transcript)
        evt = PreToolUseEvent(_raw=raw, ctx=ctx)
        assert dispatch(Event.PreToolUse, evt) is not None
    def test_does_not_match_other_files(self) -> None:
        transcript = make_transcript(
            [
                toolmsg("Edit", {"file_path": "bioqa/core.py", "old_string": "", "new_string": "x"}, "e1"),
                tool_resultmsg("e1"),
            ]
        )
        register_hook(Event.PreToolUse, message="www touched", block=True, only_if=[TouchedFile("www/src/*")])
        raw = {"tool_name": "Bash", "tool_input": {"command": "echo hi"}}
        ctx = build_ctx(transcript=transcript)
        evt = PreToolUseEvent(_raw=raw, ctx=ctx)
        assert dispatch(Event.PreToolUse, evt) is None
class TestRanCommandCondition:
    """VAL-CROSS-006"""

    def test_skip_if_skips_when_matching(self) -> None:
        transcript = make_transcript(
            [
                toolmsg("Bash", {"command": "uv run mtest run tests/"}, "b1"),
                tool_resultmsg("b1"),
            ]
        )
        register_hook(Event.PreToolUse, message="run mtest first", block=True, skip_if=[RanCommand(r"uv\s+run\s+mtest")])
        raw = {"tool_name": "Bash", "tool_input": {"command": "echo hi"}}
        ctx = build_ctx(transcript=transcript)
        evt = PreToolUseEvent(_raw=raw, ctx=ctx)
        assert dispatch(Event.PreToolUse, evt) is None
    def test_fires_when_not_matching(self) -> None:
        transcript = make_transcript(
            [
                toolmsg("Bash", {"command": "ls -la"}, "b1"),
                tool_resultmsg("b1"),
            ]
        )
        register_hook(Event.PreToolUse, message="run mtest first", block=True, skip_if=[RanCommand(r"uv\s+run\s+mtest")])
        raw = {"tool_name": "Bash", "tool_input": {"command": "echo hi"}}
        ctx = build_ctx(transcript=transcript)
        evt = PreToolUseEvent(_raw=raw, ctx=ctx)
        assert dispatch(Event.PreToolUse, evt) is not None
class TestNudgeSignalScoring:
    """VAL-CROSS-007"""

    def test_nudge_signal_fires_and_cites(self) -> None:
        transcript = make_transcript(
            [
                msg("assistant", "let me retry this"),
                msg("assistant", "let me retry again"),
                msg("assistant", "I will retry the approach"),
            ]
        )
        session_dir = Path(tempfile.mkdtemp())
        from captain_hook.primitives.nudge import nudge

        nudge(
            "stop retrying",
            signals=Signals(patterns=[Signal(pattern=r"retry", weight=2)], threshold=2, window=5),
        )
        evt = mock_tool_event(
            "Bash",
            event=Event.PostToolUse,
            command="echo hi",
            transcript=transcript,
            session_dir=session_dir,
        )
        output = dispatch(Event.PostToolUse, evt, session_dir=session_dir)

        assert output is not None
        ctx_text = output["hookSpecificOutput"]["additionalContext"]
        assert "stop retrying" in ctx_text
        assert "Triggered by:" in ctx_text
class TestEchoSuppression:
    """VAL-CROSS-008"""

    def test_suppresses_second_dispatch_same_content(self) -> None:
        transcript = make_transcript(
            [
                msg("assistant", "let me retry this approach"),
                msg("assistant", "let me retry again with a different method"),
            ]
        )
        session_dir = Path(tempfile.mkdtemp())
        from captain_hook.primitives.nudge import nudge

        nudge(
            "stop retrying",
            signals=Signals(patterns=[Signal(pattern=r"retry", weight=2)], threshold=2, window=5),
            max_fires=5,
        )

        evt1 = mock_tool_event(
            "Bash",
            event=Event.PostToolUse,
            command="echo 1",
            transcript=transcript,
            session_dir=session_dir,
        )
        out1 = dispatch(Event.PostToolUse, evt1, session_dir=session_dir)
        assert out1 is not None

        evt2 = mock_tool_event(
            "Bash",
            event=Event.PostToolUse,
            command="echo 2",
            transcript=transcript,
            session_dir=session_dir,
        )
        out2 = dispatch(Event.PostToolUse, evt2, session_dir=session_dir)
        assert out2 is None

        transcript_new = make_transcript(
            [
                msg("assistant", "let me retry this approach"),
                msg("assistant", "let me retry again with a different method"),
                msg("assistant", "I will retry once more now"),
            ]
        )
        evt3 = mock_tool_event(
            "Bash",
            event=Event.PostToolUse,
            command="echo 3",
            transcript=transcript_new,
            session_dir=session_dir,
        )
        out3 = dispatch(Event.PostToolUse, evt3, session_dir=session_dir)
        assert out3 is not None
class TestMaxFiresSessionStore:
    """VAL-CROSS-009"""

    def test_max_fires_limits_execution(self) -> None:
        session_dir = Path(tempfile.mkdtemp())
        register_hook(Event.PreToolUse, message="limited", block=True, max_fires=2)
        for i in range(3):
            raw = {"tool_name": "Bash", "tool_input": {"command": f"echo {i}"}}
            ctx = build_ctx(session_dir=session_dir)
            evt = PreToolUseEvent(_raw=raw, ctx=ctx)
            output = dispatch(Event.PreToolUse, evt, session_dir=session_dir)
            if i < 2:
                assert output is not None, f"Dispatch {i} should fire"
            else:
                assert output is None, f"Dispatch {i} should be suppressed"
    def test_fire_count_persisted(self) -> None:
        session_dir = Path(tempfile.mkdtemp())
        register_hook(Event.PreToolUse, message="counted", block=True, max_fires=5)
        raw = {"tool_name": "Bash", "tool_input": {"command": "echo hi"}}
        ctx = build_ctx(session_dir=session_dir)
        evt = PreToolUseEvent(_raw=raw, ctx=ctx)
        dispatch(Event.PreToolUse, evt, session_dir=session_dir)

        hook_dir = session_dir / _state.hooks[0].name
        state_file = hook_dir / "hook_state.json"
        assert state_file.exists()
        state = HookState.model_validate_json(state_file.read_text())
        assert state.fire_count == 1
@pytest.mark.usefixtures("isolate_modules")
class TestCLIFullPipeline:
    """VAL-CROSS-010"""

    def test_conf_settings_loaded_before_hooks(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")
        (hooks_dir / "conf.py").write_text("test_command = 'uv run mtest'\nvcs = 'jj'")
        (hooks_dir / "my_hook.py").write_text(
            textwrap.dedent("""
            from captain_hook.app import hook, on
            from captain_hook.types import Event, Tool
            hook(Event.PreToolUse, message="no rm", block=True, only_if=[Tool("Bash")])
        """).strip()
        )

        discover_hooks(hooks_dir)
        assert _state.settings is not None
        assert _state.settings.test_command == "uv run mtest"
        assert len(_state.hooks) >= 1

        raw = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}
        ctx = HookContext(session=SessionStore(None), transcript=fixture_session([]), settings=_state.settings)
        evt = PreToolUseEvent(_raw=raw, ctx=ctx)
        output = dispatch(Event.PreToolUse, evt)
        assert output is not None
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_settings_accessible_in_handler(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "myhooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")
        (hooks_dir / "conf.py").write_text("custom_value = 42")
        (hooks_dir / "handler_hook.py").write_text(
            textwrap.dedent("""
            from captain_hook.app import hook, on
            from captain_hook.types import Event, Action, HookResult
            @on(Event.PreToolUse)
            def use_settings(evt):
                val = evt.ctx.conf.custom_value if evt.ctx.conf else None
                if val == 42:
                    return HookResult(action=Action.warn, message=f"custom={val}")
                return None
        """).strip()
        )

        discover_hooks(hooks_dir)
        raw = {"tool_name": "Bash", "tool_input": {"command": "echo hi"}}
        ctx = HookContext(session=SessionStore(None), transcript=fixture_session([]), settings=_state.settings)
        evt = PreToolUseEvent(_raw=raw, ctx=ctx)
        output = dispatch(Event.PreToolUse, evt)
        assert output is not None
        assert "custom=42" in output["hookSpecificOutput"]["additionalContext"]


class TestInlineTestsPipeline:
    """VAL-CROSS-011"""

    def test_inline_tests_full_pipeline(self) -> None:
        from captain_hook.primitives.commands import block_command

        block_command(
            "git stash",
            reason="not allowed",
            hint="use jj instead",
            tests={
                Input(command="git stash"): Block(pattern="not allowed"),
                Input(command="git status"): Allow(),
            },
        )
        results = run_inline_tests()
        assert len(results) >= 2
        for name, status, passed, detail in results:
            assert passed, f"{name} failed: {detail}"
class TestWorkflowTranscript:
    """VAL-CROSS-012"""

    def test_blocks_when_step_incomplete(self) -> None:
        transcript = make_transcript([msg("assistant", "I am starting")])
        workflow(
            label="TEST",
            marker="DONE",
            steps=[
                Step(name="run tests", check=text_matches(r"mtest"), stopped_at="Stop here", next_step="Run mtest"),
                Step(name="review", check=text_matches(r"review"), stopped_at="Stop", next_step="Do review"),
            ],
        )
        result = dispatch_test(Event.SubagentStop, transcript=transcript)
        assert result is not None
        reason = result.get("reason", "")
        assert "TEST INCOMPLETE" in reason
        assert "Run mtest" in reason
    def test_allows_when_marker_present(self) -> None:
        transcript = make_transcript([msg("assistant", "ran mtest and DONE")])
        workflow(
            label="TEST",
            marker="DONE",
            steps=[Step(name="run tests", check=text_matches(r"mtest"), stopped_at="S", next_step="N")],
        )
        result = dispatch_test(Event.SubagentStop, transcript=transcript)
        assert result is None
    def test_step_evaluation_order(self) -> None:
        transcript = make_transcript([msg("assistant", "I ran mtest but no review")])
        workflow(
            label="TEST",
            marker="DONE",
            steps=[
                Step(name="tests", check=text_matches(r"mtest"), stopped_at="S1", next_step="N1"),
                Step(name="review", check=text_matches(r"review"), stopped_at="S2", next_step="Do review"),
            ],
        )
        result = dispatch_test(Event.SubagentStop, transcript=transcript)
        assert result is not None
        assert "Do review" in result.get("reason", "")
class TestClassifierTranscript:
    """VAL-CROSS-013"""

    def test_conductor_filters_synthetic_messages(self) -> None:
        from cc_transcript.ids import SessionId
        from cc_transcript.parser import parse_event
        from cc_transcript.query import Session

        from captain_hook.classifiers.conductor import classifier

        events = [
            parse_event(msg("user", "<system_instruction>setup")),
            parse_event(msg("user", "please help me debug")),
            parse_event(msg("assistant", "I will help")),
        ]
        session = Session.from_activity(
            SessionActivity.from_events(SessionId("sess"), events, user_classifier=classifier)
        )
        assert session.user_said("help")
        assert not session.user_said("setup")

    def test_native_includes_all_user_messages(self) -> None:
        from cc_transcript.ids import SessionId
        from cc_transcript.parser import parse_event
        from cc_transcript.query import Session

        from captain_hook.classifiers.native import classifier

        events = [
            parse_event(msg("user", "<system_instruction>setup")),
            parse_event(msg("user", "help me debug")),
        ]
        session = Session.from_activity(
            SessionActivity.from_events(SessionId("sess"), events, user_classifier=classifier)
        )
        assert session.user_said("help")
        assert session.user_said("setup")


class TestNlpSignalNudge:
    """VAL-CROSS-014"""

    def test_nlp_signal_scoring_through_nudge(self) -> None:
        nlp_sig = NlpSignal(clauses=[Clause(noun=Phrase("test"), verb=Phrase("run"))], weight=3)
        score = score_signals([nlp_sig], "I will run the test suite now.")
        assert score >= 3

        transcript = make_transcript([msg("assistant", "I will run the test suite now.")])
        session_dir = Path(tempfile.mkdtemp())
        from captain_hook.primitives.nudge import nudge

        nudge("do not run tests yet", signals=Signals(patterns=[nlp_sig], threshold=3, window=5))
        evt = mock_tool_event(
            "Bash",
            event=Event.PostToolUse,
            command="echo hi",
            transcript=transcript,
            session_dir=session_dir,
        )
        output = dispatch(Event.PostToolUse, evt, session_dir=session_dir)
        assert output is not None
class TestUsedSkillCondition:
    """VAL-CROSS-015"""

    def test_skip_if_skips_when_skill_used(self) -> None:
        transcript = make_transcript(
            [
                toolmsg("Skill", {"skill": "codex"}, "s1"),
                tool_resultmsg("s1"),
            ]
        )
        register_hook(Event.PreToolUse, message="use codex", block=True, skip_if=[UsedSkill("codex|test-runner")])
        raw = {"tool_name": "Bash", "tool_input": {"command": "echo hi"}}
        ctx = build_ctx(transcript=transcript)
        evt = PreToolUseEvent(_raw=raw, ctx=ctx)
        assert dispatch(Event.PreToolUse, evt) is None
    def test_fires_when_skill_not_used(self) -> None:
        transcript = make_transcript([msg("user", "hello")])
        register_hook(Event.PreToolUse, message="use codex", block=True, skip_if=[UsedSkill("codex|test-runner")])
        raw = {"tool_name": "Bash", "tool_input": {"command": "echo hi"}}
        ctx = build_ctx(transcript=transcript)
        evt = PreToolUseEvent(_raw=raw, ctx=ctx)
        assert dispatch(Event.PreToolUse, evt) is not None
class _InMissionMode:
    def check(self, evt: BaseHookEvent) -> bool:
        transcript_path = evt._raw.get("transcript_path")
        if not transcript_path:
            return False
        settings_path = Path(transcript_path).with_suffix(".settings.json")
        if not settings_path.exists():
            return False
        try:
            data = json.loads(settings_path.read_text())
            return any(
                t.get("name") in ("mission-worker", "mission-session")
                for t in data.get("tags", [])
                if isinstance(t, dict)
            )
        except (json.JSONDecodeError, OSError):
            return False


class TestInMissionModeCondition:
    """VAL-CROSS-016"""

    def test_skips_when_settings_has_tag(self, tmp_path: Path) -> None:
        transcript_file = tmp_path / "session.jsonl"
        transcript_file.write_text("")
        settings_file = tmp_path / "session.settings.json"
        settings_file.write_text(json.dumps({"tags": [{"name": "mission-worker"}]}))


        @on(Event.PreToolUse, skip_if=[_InMissionMode()])
        def my_hook(evt: BaseHookEvent) -> HookResult | None:
            return HookResult(action=Action.warn, message="fired")

        raw = {"tool_name": "Bash", "tool_input": {"command": "echo hi"}, "transcript_path": str(transcript_file)}
        ctx = build_ctx()
        evt = PreToolUseEvent(_raw=raw, ctx=ctx)
        assert dispatch(Event.PreToolUse, evt) is None
    def test_fires_without_settings(self) -> None:

        @on(Event.PreToolUse, skip_if=[_InMissionMode()])
        def my_hook(evt: BaseHookEvent) -> HookResult | None:
            return HookResult(action=Action.warn, message="fired")

        raw = {"tool_name": "Bash", "tool_input": {"command": "echo hi"}, "transcript_path": "/nonexistent/s.jsonl"}
        ctx = build_ctx()
        evt = PreToolUseEvent(_raw=raw, ctx=ctx)
        output = dispatch(Event.PreToolUse, evt)
        assert output is not None
class TestCallCli:
    """VAL-CROSS-017"""

    def test_returns_stdout(self) -> None:
        ctx = build_ctx()
        assert ctx.call_cli(["echo", "hello"]).strip() == "hello"

    def test_raises_on_nonzero_exit(self) -> None:
        ctx = build_ctx()
        with pytest.raises(subprocess.CalledProcessError):
            ctx.call_cli(["false"])

    def test_timeout_enforced(self) -> None:
        ctx = build_ctx()
        with pytest.raises(subprocess.TimeoutExpired):
            ctx.call_cli(["sleep", "10"], timeout=1)


class TestCallLlm:
    """VAL-CROSS-018"""

    def test_codex_backend_builds_command_and_parses(self) -> None:
        from captain_hook.llm import CodexBackend

        backend = CodexBackend()
        cmd = backend.build_command(backend.models["small"], None, agent=True)
        assert cmd[0] == "codex"
        assert "exec" in cmd
        assert backend.parse_response("raw output", None) == "raw output"

    def test_claude_backend_builds_command_and_parses(self) -> None:
        from captain_hook.llm import ClaudeBackend

        backend = ClaudeBackend()
        cmd = backend.build_command(backend.models["small"], None, agent=False)
        assert cmd[0] == "claude"
        assert "--bare" not in cmd
        assert "--system-prompt" in cmd and cmd[cmd.index("--system-prompt") + 1] == ""
        assert "--setting-sources" in cmd and cmd[cmd.index("--setting-sources") + 1] == ""
        assert "--strict-mcp-config" in cmd
        assert "--tools" not in cmd
        assert backend.parse_response("raw text", None) == "raw text"

    def test_codex_backend_parses_model_response(self) -> None:
        from pydantic import BaseModel

        from captain_hook.llm import CodexBackend

        class Verdict(BaseModel):
            block: bool
            reason: str

        backend = CodexBackend()
        result = backend.parse_response('{"block": true, "reason": "bad code"}', Verdict)
        assert result.block is True
        assert result.reason == "bad code"

    def test_transcript_interpolation_in_template(self) -> None:
        transcript = make_transcript([msg("assistant", "context data")])
        ctx = build_ctx(transcript=transcript)
        template = "Review: {transcript}\n\nDone"
        rendered = template.format(transcript=ctx.transcript)
        assert "context data" in rendered


class TestCommandLineRedirects:
    """VAL-CROSS-019"""

    def test_redirect_to_cleanup_file(self) -> None:
        cl = CommandLine.parse("echo hello > .context/cleanup/code-issues.jsonl")
        assert len(cl.commands) >= 1
        assert cl.primary.program == "echo"
        assert any(".context/cleanup/code-issues.jsonl" in r.target for r in cl.primary.redirects)

    def test_multi_command_parsing(self) -> None:
        cl = CommandLine.parse("cd /tmp && echo hello | grep h")
        assert len(cl.commands) >= 2

    def test_redirect_target_extractable(self) -> None:
        cl = CommandLine.parse("cat data.json >> output.log")
        assert any(r.target == "output.log" for r in cl.primary.redirects)
        assert any(r.op == ">>" for r in cl.primary.redirects)

    def test_pipe_chain_commands(self) -> None:
        cl = CommandLine.parse("echo hello | tee log.txt | wc -l")
        assert len(cl.commands) >= 3


class TestCLISubprocess:
    """VAL-CROSS-010: real CLI subprocess test exercising full stdin/stdout contract"""

    def test_cli_subprocess_stdin_stdout_roundtrip(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")
        (hooks_dir / "hook.py").write_text(
            textwrap.dedent("""
            from captain_hook.app import hook, on
            from captain_hook.types import Event, Tool
            hook(Event.PreToolUse, message="blocked rm", block=True, only_if=[Tool("Bash")])
        """).strip()
        )

        stdin_payload = json.dumps(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf /"},
            }
        )

        result = subprocess.run(
            ["uv", "run", "python", "-m", "captain_hook", "--hooks", str(hooks_dir), "run", "PreToolUse"],
            input=stdin_payload,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "blocked rm" in output["hookSpecificOutput"]["permissionDecisionReason"]

    def test_cli_subprocess_allow_produces_no_output(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")
        (hooks_dir / "hook.py").write_text(
            textwrap.dedent("""
            from captain_hook.app import hook, on
            from captain_hook.types import Event, Tool
            hook(Event.PreToolUse, message="blocked rm", block=True, only_if=[Tool("Bash")])
        """).strip()
        )

        stdin_payload = json.dumps(
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": "test.py", "old_string": "a", "new_string": "b"},
            }
        )

        result = subprocess.run(
            ["uv", "run", "python", "-m", "captain_hook", "--hooks", str(hooks_dir), "run", "PreToolUse"],
            input=stdin_payload,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_cli_subprocess_warn_output(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")
        (hooks_dir / "hook.py").write_text(
            textwrap.dedent("""
            from captain_hook.app import hook, on
            from captain_hook.types import Event, Tool, Action, HookResult
            @on(Event.PreToolUse, only_if=[Tool("Bash")])
            def warn_handler(evt):
                return HookResult(action=Action.warn, message="caution")
        """).strip()
        )

        stdin_payload = json.dumps(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
            }
        )

        result = subprocess.run(
            ["uv", "run", "python", "-m", "captain_hook", "--hooks", str(hooks_dir), "run", "PreToolUse"],
            input=stdin_payload,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert "caution" in output["hookSpecificOutput"]["additionalContext"]


class TestCallLlmIntegration:
    """VAL-CROSS-018: HookContext.call_llm delegates to spawnllm.call with the right backend and knobs."""

    @staticmethod
    def capture_call(monkeypatch: pytest.MonkeyPatch, returns: object) -> dict[str, Any]:
        import captain_hook.context as context_mod

        captured: dict[str, Any] = {}

        def fake_call(prompt: str, **kwargs: Any) -> object:
            captured["prompt"] = prompt
            captured.update(kwargs)
            return returns

        monkeypatch.setattr(context_mod, "call", fake_call)
        return captured

    def test_call_llm_invokes_review_backend(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from captain_hook.llm import CodexBackend

        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        captured = self.capture_call(monkeypatch, "all good")
        ctx = build_ctx(transcript=make_transcript([msg("assistant", "some context")]), session_dir=tmp_path)

        result = ctx.call_llm("Review this code", specialty="review", model="small", timeout=99)
        assert result == "all good"
        assert isinstance(captured["backend"], CodexBackend)
        assert captured["model"] == "small"
        assert captured["timeout"] == 99
        assert captured["cwd"] == str(tmp_path)
        assert captured["response_model"] is None

    def test_call_llm_general_uses_claude_backend(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from captain_hook.llm import ClaudeBackend

        captured = self.capture_call(monkeypatch, "claude response")
        ctx = build_ctx(transcript=make_transcript([msg("assistant", "context")]), session_dir=tmp_path)

        result = ctx.call_llm("hello", specialty="general", model="small")
        assert result == "claude response"
        assert isinstance(captured["backend"], ClaudeBackend)

    def test_call_llm_with_response_model(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from pydantic import BaseModel

        class Verdict(BaseModel):
            block: bool
            reasoning: str

        verdict = Verdict(block=True, reasoning="detected issue")
        captured = self.capture_call(monkeypatch, verdict)
        ctx = build_ctx(transcript=make_transcript([msg("assistant", "context")]), session_dir=tmp_path)

        result = ctx.call_llm("check", specialty="review", model="small", response_model=Verdict)
        assert result is verdict
        assert captured["response_model"] is Verdict

    def test_call_llm_with_transcript_interpolation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = self.capture_call(monkeypatch, "ok")
        ctx = build_ctx(transcript=make_transcript([msg("assistant", "important context")]), session_dir=tmp_path)

        ctx.call_llm("Review: {transcript}", specialty="review", model="small", transcript=True)
        assert "important context" in captured["prompt"]
