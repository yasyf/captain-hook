from __future__ import annotations

import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from captain_hook.app import hook as register_hook
from captain_hook.app import reset
from captain_hook.events import (
    PreToolUseEvent,
    SessionEndEvent,
    StopEvent,
    SubagentStartEvent,
    SubagentStopEvent,
    UserPromptSubmitEvent,
)
from captain_hook.testing.types import Allow, Ask, Block, Rewrite, Warn
from captain_hook.types import Action, Event, FilePath, HookResult, Tool
from tests.helpers import assert_result, mock_event


class TestInput:
    def test_input_frozen(self):
        from captain_hook.testing.types import Input

        i = Input(command="ls")
        with pytest.raises(FrozenInstanceError):
            i.command = "other"  # type: ignore[misc]

    def test_input_all_fields_default_none(self):
        from captain_hook.testing.types import Input

        i = Input()
        assert i.command is None
        assert i.file is None
        assert i.content is None
        assert i.old is None
        assert i.tool is None
        assert i.prompt is None
        assert i.agent_type is None
        assert i.reason is None
        assert i.transcript is None

    def test_input_with_all_fields(self):
        from captain_hook.testing.types import Input

        i = Input(
            command="ls",
            file="test.py",
            content="hello",
            old="world",
            tool="Bash",
            prompt="do it",
            agent_type="worker",
            reason="clear",
            transcript=Path("/tmp/test.jsonl"),
        )
        assert i.command == "ls"
        assert i.file == "test.py"
        assert i.content == "hello"
        assert i.old == "world"
        assert i.tool == "Bash"
        assert i.prompt == "do it"
        assert i.agent_type == "worker"
        assert i.reason == "clear"
        assert i.transcript == Path("/tmp/test.jsonl")

    def test_input_transcript_list_wraps_in_fixture(self):
        from captain_hook.testing.types import Input, TranscriptFixture

        msgs = [{"type": "user", "message": {"content": "hi"}}]
        i = Input(transcript=msgs)
        assert isinstance(i.transcript, TranscriptFixture)
        assert i.transcript.messages == msgs

    def test_input_transcript_path(self):
        from captain_hook.testing.types import Input

        p = Path("/tmp/test.jsonl")
        i = Input(transcript=p)
        assert i.transcript == p


class TestTranscriptFixture:
    def test_wraps_message_list(self):
        from captain_hook.testing.types import TranscriptFixture

        msgs = [{"type": "user", "message": {"content": "hello"}}]
        tf = TranscriptFixture(messages=msgs)
        assert tf.messages == msgs

    def test_repr(self):
        from captain_hook.testing.types import TranscriptFixture

        tf = TranscriptFixture(messages=[])
        assert "TranscriptFixture" in repr(tf)


class TestInlineTests:
    def test_inline_tests_dict_type(self):
        from captain_hook.testing.types import Allow, Block, InlineTests, Input, Warn

        t: InlineTests = {
            Input(command="ls"): Allow(),
            Input(command="rm -rf /"): Block(pattern="danger"),
            "session-uuid": Warn(),
        }
        assert len(t) == 3


class TestMockEvent:
    def test_mock_event_bash_creates_correct_tool_input(self):

        evt = mock_event("PreToolUse", tool="Bash", command="ls -la")
        assert evt.tool_name == "Bash"
        assert evt._raw["tool_input"]["command"] == "ls -la"

    def test_mock_event_edit_creates_correct_tool_input(self):

        evt = mock_event("PreToolUse", tool="Edit", file="test.py", content="new", old="old")
        assert evt.tool_name == "Edit"
        ti = evt._raw["tool_input"]
        assert ti["file_path"] == "test.py"
        assert ti["old_string"] == "old"
        assert ti["new_string"] == "new"

    def test_mock_event_write_creates_correct_tool_input(self):

        evt = mock_event("PreToolUse", tool="Write", file="test.py", content="content")
        assert evt.tool_name == "Write"
        ti = evt._raw["tool_input"]
        assert ti["file_path"] == "test.py"
        assert ti["content"] == "content"

    def test_mock_event_read_creates_correct_tool_input(self):

        evt = mock_event("PreToolUse", tool="Read", file="test.py")
        assert evt.tool_name == "Read"
        assert evt._raw["tool_input"]["file_path"] == "test.py"

    def test_mock_event_agent_creates_correct_tool_input(self):

        evt = mock_event("SubagentStop", agent_type="worker")
        assert evt._raw.get("agent_type") == "worker"

    def test_mock_event_stop(self):

        evt = mock_event("Stop")
        assert isinstance(evt, StopEvent)

    def test_mock_event_session_end(self):

        evt = mock_event("SessionEnd", reason="clear")
        assert isinstance(evt, SessionEndEvent)
        assert evt.reason == "clear"

    def test_mock_event_session_end_default_reason(self):

        evt = mock_event("SessionEnd")
        assert isinstance(evt, SessionEndEvent)
        assert evt.reason == "other"

    def test_mock_event_subagent_stop(self):

        evt = mock_event("SubagentStop", agent_type="cleanup")
        assert isinstance(evt, SubagentStopEvent)
        assert evt._raw["agent_type"] == "cleanup"

    def test_mock_event_subagent_start(self):

        evt = mock_event("SubagentStart", agent_type="worker")
        assert isinstance(evt, SubagentStartEvent)

    def test_mock_event_user_prompt(self):

        evt = mock_event("UserPromptSubmit", prompt="do this")
        assert isinstance(evt, UserPromptSubmitEvent)
        assert evt._raw["prompt"] == "do this"

    def test_mock_event_with_transcript(self):
        from tests.helpers import fixture_session

        transcript = fixture_session([{"type": "user", "message": {"content": "hello"}}])
        evt = mock_event("PreToolUse", tool="Bash", command="ls", transcript=transcript)
        assert evt.ctx.transcript is transcript

    def test_mock_event_accepts_event_enum(self):

        evt = mock_event(Event.PreToolUse, tool="Bash", command="ls")
        assert isinstance(evt, PreToolUseEvent)

    def test_mock_event_defaults_to_bash(self):

        evt = mock_event("PreToolUse")
        assert evt.tool_name == "Bash"


class TestInputToEvent:
    def test_input_to_event_basic(self):
        from captain_hook.testing.types import Input
        from tests.helpers import input_to_event

        inp = Input(tool="Bash", command="ls")
        evt = input_to_event(Event.PreToolUse, inp)
        assert isinstance(evt, PreToolUseEvent)
        assert evt._raw["tool_input"]["command"] == "ls"

    def test_input_to_event_with_transcript_fixture(self):
        from captain_hook.testing.types import Input, TranscriptFixture
        from tests.helpers import input_to_event

        tf = TranscriptFixture(
            messages=[
                {"type": "user", "message": {"content": "hello"}},
            ]
        )
        inp = Input(tool="Bash", command="ls", transcript=tf)
        evt = input_to_event(Event.PreToolUse, inp)
        assert len(evt.ctx.transcript) == 1

    def test_input_to_event_with_path(self, tmp_path: Path):
        from captain_hook.testing.types import Input
        from tests.helpers import input_to_event

        p = tmp_path / "test.jsonl"
        p.write_text(
            '{"type": "user", "uuid": "u", "sessionId": "s", "timestamp": "2026-01-01T00:00:00Z",'
            ' "message": {"content": "hi"}}\n'
        )
        inp = Input(tool="Bash", command="ls", transcript=p)
        evt = input_to_event(Event.PreToolUse, inp)
        assert len(evt.ctx.transcript) == 1

    def test_input_to_event_none_transcript(self):
        from captain_hook.testing.types import Input
        from tests.helpers import input_to_event

        inp = Input(tool="Edit", file="x.py", content="new", old="old")
        evt = input_to_event(Event.PreToolUse, inp)
        assert len(evt.ctx.transcript) == 0

    def test_input_to_event_injects_tasks(self):
        from captain_hook.testing.types import Input
        from tests.helpers import input_to_event

        inp = Input(
            tasks=[
                {"id": "1", "subject": "a", "status": "completed"},
                {"id": "2", "subject": "b", "status": "pending"},
            ]
        )
        evt = input_to_event(Event.Stop, inp)
        assert len(evt.tasks) == 2
        assert not evt.tasks.all_completed
        assert [t.id for t in evt.tasks.open] == ["2"]

    def test_input_to_event_empty_tasks_all_completed(self):
        from captain_hook.testing.types import Input
        from tests.helpers import input_to_event

        evt = input_to_event(Event.Stop, Input(tasks=[]))
        assert len(evt.tasks) == 0
        assert evt.tasks.all_completed

    def test_input_to_event_tasks_on_tool_event(self):
        from captain_hook.testing.types import Input
        from tests.helpers import input_to_event

        inp = Input(tool="Edit", file="x.py", content="y", tasks=[{"id": "1", "subject": "a", "status": "in_progress"}])
        evt = input_to_event(Event.PostToolUse, inp)
        assert not evt.tasks.all_completed

    def test_input_to_event_stop(self):
        from captain_hook.testing.types import Input
        from tests.helpers import input_to_event

        evt = input_to_event(Event.Stop, Input())
        assert isinstance(evt, StopEvent)

    def test_input_to_event_session_end(self):
        from captain_hook.testing.types import Input
        from tests.helpers import input_to_event

        evt = input_to_event(Event.SessionEnd, Input(reason="prompt_input_exit"))
        assert isinstance(evt, SessionEndEvent)
        assert evt.reason == "prompt_input_exit"

    def test_input_to_event_session_end_default_reason(self):
        from captain_hook.testing.types import Input
        from tests.helpers import input_to_event

        evt = input_to_event(Event.SessionEnd, Input())
        assert isinstance(evt, SessionEndEvent)
        assert evt.reason == "other"

    def test_input_to_event_subagent_stop(self):
        from captain_hook.testing.types import Input
        from tests.helpers import input_to_event

        evt = input_to_event(Event.SubagentStop, Input(agent_type="cleanup"))
        assert isinstance(evt, SubagentStopEvent)
        assert evt._raw["agent_type"] == "cleanup"

    def test_input_to_event_user_prompt(self):
        from captain_hook.testing.types import Input
        from tests.helpers import input_to_event

        evt = input_to_event(Event.UserPromptSubmit, Input(prompt="do it"))
        assert isinstance(evt, UserPromptSubmitEvent)
        assert evt._raw["prompt"] == "do it"

    @pytest.mark.parametrize("skip", [True, False], ids=["true_injects", "false_injects"])
    def test_input_to_event_injects_skip_permissions(self, skip: bool):
        # False must inject too — truthiness would let a no-consent test fall through to
        # the real ps-walk and silently invert inside a bypass-launched dev session.
        from captain_hook.testing.types import Input
        from tests.helpers import input_to_event

        evt = input_to_event(Event.PermissionRequest, Input(command="ls", skip_permissions=skip))
        assert evt.__dict__["skip_permissions"] is skip
        assert evt.skip_permissions is skip

    def test_input_to_event_leaves_skip_permissions_lazy_when_unset(self):
        from captain_hook.testing.types import Input
        from tests.helpers import input_to_event

        evt = input_to_event(Event.PermissionRequest, Input(command="ls"))
        assert "skip_permissions" not in evt.__dict__

    def test_input_to_event_spec_tool_fallback(self):
        from captain_hook.testing.types import Input
        from tests.helpers import input_to_event

        evt = input_to_event(Event.PreToolUse, Input(command="ls"), spec_tool="Bash")
        assert evt.tool_name == "Bash"

    def test_input_to_event_agent_model_round_trips(self):
        from cc_transcript.tools import TaskCall

        from captain_hook.testing.types import Input
        from tests.helpers import input_to_event

        inp = Input(tool="Agent", model="haiku", agent_type="Explore", content="do it")
        evt = input_to_event(Event.PreToolUse, inp)
        call = evt.as_input(TaskCall)
        assert call is not None
        assert call.model == "haiku"
        assert call.agent_type == "Explore"
        assert call.prompt == "do it"

    def test_input_to_event_tool_input_escape_hatch(self):
        from cc_transcript.tools import WorkflowCall

        from captain_hook.testing.types import Input
        from tests.helpers import input_to_event

        evt = input_to_event(Event.PreToolUse, Input(tool="Workflow", tool_input={"script": "model: 'haiku'"}))
        assert evt._raw["tool_input"] == {"script": "model: 'haiku'"}
        call = evt.as_input(WorkflowCall)
        assert call is not None
        assert call.script == "model: 'haiku'"


class TestSetToolInput:
    def test_fills_missing_field(self):
        from captain_hook.primitives.rewrite import set_tool_input
        from captain_hook.testing.helpers import run_inline_tests
        from captain_hook.testing.types import Input, Rewrite

        reset()
        set_tool_input("model", "sonnet", tool="Agent|Task", tests={Input(tool="Agent", content="do it"): Rewrite()})
        results = run_inline_tests()
        assert len(results) == 1
        assert all(r[2] for r in results), f"Failed: {results}"

    def test_never_clobbers_explicit_field(self):
        from captain_hook.primitives.rewrite import set_tool_input
        from captain_hook.testing.helpers import run_inline_tests
        from captain_hook.testing.types import Allow, Input

        reset()
        set_tool_input(
            "model", "sonnet", tool="Agent|Task", tests={Input(tool="Agent", model="haiku", content="do it"): Allow()}
        )
        results = run_inline_tests()
        assert len(results) == 1
        assert all(r[2] for r in results), f"Failed: {results}"

    def test_rewrites_input_and_attaches_note(self):
        from captain_hook.app import _state
        from captain_hook.dispatch import execute_hook
        from captain_hook.primitives.rewrite import set_tool_input
        from captain_hook.testing.types import Input
        from tests.helpers import input_to_event

        reset()
        set_tool_input("model", "sonnet", tool="Agent|Task", note="upgraded to sonnet")
        result = execute_hook(_state.hooks[-1], input_to_event(Event.PreToolUse, Input(tool="Agent", content="do it")))
        assert result is not None
        assert result.action == Action.rewrite
        assert result.updated_input == {"prompt": "do it", "model": "sonnet"}
        assert result.note == "upgraded to sonnet"


class TestInlineLlmOverrides:
    def test_nudge_judge_declines_path(self):
        from captain_hook.primitives.llm import llm_nudge
        from captain_hook.testing.helpers import run_inline_tests
        from captain_hook.testing.types import Input

        reset()
        llm_nudge(
            "Should this fire?",
            message="nudged",
            events=Event.PreToolUse,
            only_if=[Tool("Bash")],
            agent=False,
            transcript=False,
            tests={
                Input(command="ls", llm={"fire": False}): Allow(),
                Input(command="ls"): Warn(pattern="nudged"),
            },
        )
        results = run_inline_tests()
        assert len(results) == 2
        assert all(r[2] for r in results), f"Failed: {results}"

    def test_gate_judge_declines_path(self):
        from captain_hook.primitives.llm import llm_gate
        from captain_hook.testing.helpers import run_inline_tests
        from captain_hook.testing.types import Input

        reset()
        llm_gate(
            "Should this block?",
            message="gated",
            events=Event.PreToolUse,
            only_if=[Tool("Bash")],
            agent=False,
            transcript=False,
            tests={
                Input(command="ls", llm={"block": False}): Allow(),
                Input(command="ls"): Block(pattern="gated"),
            },
        )
        results = run_inline_tests()
        assert len(results) == 2
        assert all(r[2] for r in results), f"Failed: {results}"


class TestScratchHome:
    def test_inline_tests_execute_under_scratch_home(self):
        from captain_hook.app import on
        from captain_hook.testing.helpers import run_inline_tests
        from captain_hook.testing.types import Allow, Input
        from captain_hook.util.paths import resolve_cache_home

        reset()
        recorded: dict[str, str | None] = {}

        @on(Event.PreToolUse, tests={Input(command="echo hi"): Allow()})
        def record_home(evt):
            recorded["home"] = os.environ["HOME"]
            recorded["xdg"] = os.environ.get("XDG_CACHE_HOME")
            return None

        real_home = os.environ["HOME"]
        real_cache = str(resolve_cache_home())
        saved_xdg = os.environ.get("XDG_CACHE_HOME")

        results = run_inline_tests()

        assert len(results) == 1 and results[0][2], f"Failed: {results}"
        assert recorded["home"] != real_home, "handler saw the real HOME, not the scratch dir"
        assert recorded["xdg"] == real_cache, "XDG_CACHE_HOME was not pinned to the real cache home"
        assert os.environ["HOME"] == real_home, "HOME not restored after the run"
        assert os.environ.get("XDG_CACHE_HOME") == saved_xdg, "XDG_CACHE_HOME not restored after the run"


class TestDispatchTest:
    def test_dispatch_test_returns_result(self):
        from tests.helpers import dispatch_test

        reset()
        register_hook(
            Event.PreToolUse,
            only_if=[Tool("Bash")],
            message="blocked",
            block=True,
        )
        result = dispatch_test(
            Event.PreToolUse,
            tool="Bash",
            command="ls",
        )
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"

    def test_dispatch_test_returns_none_for_no_match(self):
        from tests.helpers import dispatch_test

        reset()
        result = dispatch_test(Event.PreToolUse, tool="Bash", command="ls")
        assert result is None


class TestAssertResult:
    @pytest.mark.parametrize(
        ("result", "expectation"),
        [
            pytest.param(None, Allow(), id="allow_none"),
            pytest.param(HookResult(action=Action.allow), Allow(), id="allow_action"),
            pytest.param(None, Ask(), id="ask_none"),
            pytest.param(HookResult(action=Action.allow), Allow(explicit=True), id="explicit_allow_action"),
            pytest.param(
                HookResult(action=Action.block, message="not allowed here"),
                Block(pattern="not allowed"),
                id="block_matches",
            ),
            pytest.param(HookResult(action=Action.block, message="whatever"), Block(), id="block_no_pattern"),
            pytest.param(
                HookResult(action=Action.warn, message="check style"),
                Warn(pattern="style"),
                id="warn_matches",
            ),
            pytest.param(
                HookResult(action=Action.rewrite, updated_input={"command": "/abs/path/ccx read x --full"}),
                Rewrite(pattern="ccx read x --full"),
                id="rewrite_matches_substring",
            ),
            pytest.param(
                HookResult(action=Action.rewrite, updated_input={"command": "anything"}),
                Rewrite(),
                id="rewrite_no_pattern",
            ),
        ],
    )
    def test_assert_result(self, result: HookResult | None, expectation: Allow | Block | Warn | Rewrite):
        assert_result(result, expectation)

    def test_assert_result_allow_fails_on_block(self):
        with pytest.raises(AssertionError):
            assert_result(HookResult(action=Action.block, message="no"), Allow())

    def test_assert_result_ask_fails_on_allow(self):
        with pytest.raises(AssertionError):
            assert_result(HookResult(action=Action.allow), Ask())

    def test_assert_result_explicit_allow_fails_on_none(self):
        with pytest.raises(AssertionError):
            assert_result(None, Allow(explicit=True))

    def test_assert_result_block_fails_on_none(self):
        with pytest.raises(AssertionError):
            assert_result(None, Block(pattern="x"))

    def test_assert_result_block_fails_on_wrong_pattern(self):
        with pytest.raises(AssertionError):
            assert_result(
                HookResult(action=Action.block, message="something else"),
                Block(pattern="^totally different$"),
            )

    def test_assert_result_warn_fails_on_allow(self):
        with pytest.raises(AssertionError):
            assert_result(None, Warn(pattern="x"))

    def test_assert_result_rewrite_fails_on_block(self):
        with pytest.raises(AssertionError):
            assert_result(HookResult(action=Action.block, message="no"), Rewrite())

    def test_assert_result_rewrite_fails_on_wrong_substring(self):
        with pytest.raises(AssertionError):
            assert_result(
                HookResult(action=Action.rewrite, updated_input={"command": "ccx find **"}),
                Rewrite(pattern="ccx read"),
            )


class TestRunInlineTests:
    def test_discovers_and_executes_inline_tests(self):
        from captain_hook.testing.helpers import run_inline_tests
        from captain_hook.testing.types import Allow, Block, Input

        reset()
        register_hook(
            Event.PreToolUse,
            only_if=[Tool("Bash")],
            message="blocked command",
            block=True,
            tests={
                Input(tool="Bash", command="rm -rf /"): Block(pattern="blocked"),
                Input(tool="Edit", file="x.py", content="new", old="old"): Allow(),
            },
        )
        results = run_inline_tests()
        assert len(results) == 2
        assert all(r[2] for r in results), f"Failed: {results}"

    def test_returns_empty_for_no_tests(self):
        from captain_hook.testing.helpers import run_inline_tests

        reset()
        register_hook(Event.PreToolUse, message="no tests", only_if=[Tool("Bash")])
        results = run_inline_tests()
        assert results == []

    def test_captures_failing_test(self):
        from captain_hook.testing.helpers import run_inline_tests
        from captain_hook.testing.types import Block, Input

        reset()
        register_hook(
            Event.PreToolUse,
            only_if=[Tool("Bash")],
            message="warned",
            tests={Input(tool="Bash", command="ls"): Block(pattern="wrong")},
        )
        results = run_inline_tests()
        assert len(results) == 1
        assert not results[0][2]

    def test_durable_state_isolated_per_run_and_never_touches_real_store(self):
        # Regression: durable writes during inline tests leaked into the real store, so a
        # once-per-project hook passed its Warn test on the first run and failed every rerun.
        from captain_hook import Deque, DurableState
        from captain_hook.app import on
        from captain_hook.durable import durable_root
        from captain_hook.testing.helpers import run_inline_tests
        from captain_hook.testing.types import Input, Warn
        from tests.helpers import input_to_event

        reset()

        class WarnedOnce(DurableState, scope="project"):
            paths: Deque[16]

        @on(
            Event.PreToolUse,
            only_if=[Tool("Edit")],
            tests={Input(tool="Edit", file=".env", content="API_KEY=x"): Warn(pattern="sensitive")},
        )
        def warn_once(evt):
            with WarnedOnce.mutate(evt) as state:
                if ".env" in state.paths:
                    return None
                state.paths.append(".env")
            return evt.warn("sensitive file")

        # Project-scoped durable state only persists with a repo_root; guard against a
        # vacuous pass where mutate never writes anywhere.
        assert input_to_event(Event.PreToolUse, Input(tool="Edit", file=".env", content="x")).ctx.repo_root
        for run in (1, 2):
            results = run_inline_tests()
            assert [(r[1], r[3]) for r in results] == [("pass", "")], f"Run {run} failed: {results}"
        assert not durable_root().exists()

    def test_rewrite_expectation_passes_against_rewriting_hook(self):
        from captain_hook.primitives.commands import rewrite_command
        from captain_hook.testing.helpers import run_inline_tests
        from captain_hook.testing.types import Allow, Input, Rewrite

        reset()
        rewrite_command(
            r"^cat\s+(\S+)$",
            r"ccx read \1 --full",
            note="cat -> ccx read",
            tests={
                Input(tool="Bash", command="cat foo.py"): Rewrite(pattern="ccx read foo.py --full"),
                Input(tool="Bash", command="ls -la"): Allow(),
            },
        )
        results = run_inline_tests()
        assert len(results) == 2
        assert all(r[2] for r in results), f"Failed: {results}"

    def test_home_fixture_swaps_home_during_hook_execution(self):
        from captain_hook.app import on
        from captain_hook.testing.helpers import run_inline_tests
        from captain_hook.testing.types import FileFixture, Input, Warn

        reset()
        original_home = os.environ.get("HOME")
        seen_home: list[str | None] = []

        @on(
            Event.PreToolUse,
            only_if=[Tool("Bash")],
            tests={Input(command="cat {file}", file=FileFixture(home=True, name="secret", content="x")): Warn()},
        )
        def capture_home(evt):
            seen_home.append(os.environ.get("HOME"))
            return evt.warn("captured")

        results = run_inline_tests()
        assert all(r[2] for r in results), f"Failed: {results}"
        assert seen_home and seen_home[0] != original_home
        assert os.environ.get("HOME") == original_home

    def test_home_fixture_restores_home_even_when_test_assertion_fails(self):
        from captain_hook.app import on
        from captain_hook.testing.helpers import run_inline_tests
        from captain_hook.testing.types import Block, FileFixture, Input

        reset()
        original_home = os.environ.get("HOME")

        @on(
            Event.PreToolUse,
            only_if=[Tool("Bash")],
            tests={
                Input(command="cat {file}", file=FileFixture(home=True, name="secret", content="x")): Block(
                    pattern="nope"
                )
            },
        )
        def passthrough(evt):
            return None  # never blocks, so the Block() expectation fails

        results = run_inline_tests()
        assert len(results) == 1
        assert results[0][1] == "fail"
        assert os.environ.get("HOME") == original_home

    def test_home_fixture_restores_home_even_when_hook_raises(self):
        from captain_hook.app import on
        from captain_hook.testing.helpers import run_inline_tests
        from captain_hook.testing.types import FileFixture, Input, Warn

        reset()
        original_home = os.environ.get("HOME")

        @on(
            Event.PreToolUse,
            only_if=[Tool("Bash")],
            tests={Input(command="cat {file}", file=FileFixture(home=True, name="secret", content="x")): Warn()},
        )
        def raising_hook(evt):
            raise RuntimeError("boom")

        results = run_inline_tests()
        assert len(results) == 1
        # run_handler swallows the hook's exception and returns None, so the Warn()
        # expectation fails rather than the run erroring — the swap/restore still ran.
        assert results[0][1] == "fail"
        assert os.environ.get("HOME") == original_home


class TestStubbedContext:
    def test_input_to_event_ctx_is_stubbed(self):
        from captain_hook.testing.helpers import StubbedContext
        from captain_hook.testing.types import Input
        from tests.helpers import input_to_event

        evt = input_to_event(Event.PreToolUse, Input(tool="Bash", command="ls"))
        assert isinstance(evt.ctx, StubbedContext)

    def test_call_llm_without_model_returns_stub_string(self):
        from captain_hook.testing.helpers import StubbedContext, build_context

        ctx = StubbedContext.wrapping(build_context())
        assert ctx.call_llm("anything") == "stubbed"

    def test_call_llm_fabricates_model_honoring_stub_field_values(self):
        from pydantic import BaseModel

        from captain_hook.testing.helpers import StubbedContext, build_context

        class Verdict(BaseModel):
            block: bool

        ctx = StubbedContext.wrapping(build_context())
        assert ctx.call_llm("judge", response_model=Verdict).block is True

    def test_call_llm_applies_per_input_llm_overrides(self):
        from pydantic import BaseModel

        from captain_hook.testing.helpers import StubbedContext, build_context

        class Verdict(BaseModel):
            fire: bool

        ctx = StubbedContext.wrapping(build_context(), {"fire": False})
        assert ctx.call_llm("judge", response_model=Verdict).fire is False


class TestIsolatedStateRoot:
    def test_state_dir_points_at_yielded_root_inside(self):
        from captain_hook.settings import resolve_state_dir
        from captain_hook.testing.helpers import isolated_state_root

        with isolated_state_root() as root:
            assert resolve_state_dir() == root

    def test_restores_prior_value_after_exit(self, monkeypatch: pytest.MonkeyPatch):
        from captain_hook.testing.helpers import isolated_state_root

        monkeypatch.setenv("CAPTAIN_HOOK_STATE_DIR", "/sentinel/state")
        with isolated_state_root():
            assert os.environ["CAPTAIN_HOOK_STATE_DIR"] != "/sentinel/state"
        assert os.environ["CAPTAIN_HOOK_STATE_DIR"] == "/sentinel/state"

    def test_restores_unset_after_exit(self, monkeypatch: pytest.MonkeyPatch):
        from captain_hook.testing.helpers import isolated_state_root

        monkeypatch.delenv("CAPTAIN_HOOK_STATE_DIR", raising=False)
        with isolated_state_root():
            assert "CAPTAIN_HOOK_STATE_DIR" in os.environ
        assert "CAPTAIN_HOOK_STATE_DIR" not in os.environ

    def test_temp_root_removed_after_exit(self):
        from captain_hook.testing.helpers import isolated_state_root

        with isolated_state_root() as root:
            assert root.exists()
        assert not root.exists()


class TestInputRepr:
    def test_repr_shows_only_non_none_fields(self):
        from captain_hook.testing.types import Input

        assert repr(Input(command="git stash")) == "Input(command='git stash')"
        # Fields render in declaration order (prompt precedes model), regardless of kwarg order.
        assert repr(Input(model="haiku", prompt="p")) == "Input(prompt='p', model='haiku')"
        assert repr(Input()) == "Input()"


class TestInferTool:
    @pytest.mark.parametrize(
        ("inp_kwargs", "expected_tool"),
        [
            pytest.param({"script": "log('x')"}, "Workflow", id="script_workflow"),
            pytest.param({"old": "a", "content": "b"}, "Edit", id="old_edit"),
            pytest.param({"content": "x"}, "Write", id="content_write"),
            pytest.param({"model": "haiku"}, "Task", id="model_task"),
            pytest.param({"prompt": "do it"}, "Task", id="prompt_task"),
            pytest.param({"command": "ls"}, "Bash", id="command_bash"),
            pytest.param({"file": "a.py"}, "Read", id="bare_file_read"),
        ],
    )
    def test_infer_tool(self, inp_kwargs, expected_tool):
        from captain_hook.testing.helpers import infer_tool
        from captain_hook.testing.types import Input

        assert infer_tool(Input(**inp_kwargs)) == expected_tool

    def test_variadic_tool_condition_infers_tool_from_shape(self):
        # Regression: under Tool("Edit", "Write", "MultiEdit"), an implicit Input must infer
        # its tool from field shape via infer_tool, not build as the condition's first name.
        from captain_hook.app import on
        from captain_hook.testing.helpers import run_inline_tests
        from captain_hook.testing.types import Block, Input

        reset()

        @on(
            Event.PreToolUse,
            only_if=[Tool("Edit", "Write", "MultiEdit")],
            tests={
                Input(file="a.py", old="x", content="y"): Block(pattern="^Edit$"),
                Input(file="b.py", content="y"): Block(pattern="^Write$"),
            },
        )
        def echo_tool(evt):
            return HookResult(action=Action.block, message=evt.tool_name)

        results = run_inline_tests()
        assert len(results) == 2
        assert all(r[2] for r in results), f"Failed: {results}"

    def test_single_name_tool_condition_still_pins_tool(self):
        from captain_hook.app import on
        from captain_hook.testing.helpers import run_inline_tests
        from captain_hook.testing.types import Block, Input

        reset()

        @on(
            Event.PreToolUse,
            only_if=[Tool("Write")],
            tests={Input(file="a.py", old="x", content="y"): Block(pattern="^Write$")},
        )
        def echo_tool(evt):
            return HookResult(action=Action.block, message=evt.tool_name)

        results = run_inline_tests()
        assert len(results) == 1
        assert results[0][2], f"Failed: {results}"

    def test_variadic_non_inferable_tool_condition_pins_first_name(self):
        # Regression: Tool("WebFetch", "WebSearch") over an Input whose shape infer_tool can't
        # map (the tool_input escape hatch) must pin the FIRST named tool, not fall through to
        # Bash and miss the hook's own Tool condition — leaving Block tests vacuously unfired.
        from captain_hook.app import on
        from captain_hook.testing.helpers import run_inline_tests
        from captain_hook.testing.types import Block, Input

        reset()

        @on(
            Event.PreToolUse,
            only_if=[Tool("WebFetch", "WebSearch")],
            tests={Input(tool_input={"url": "https://example.com"}): Block(pattern="^WebFetch$")},
        )
        def echo_tool(evt):
            return HookResult(action=Action.block, message=evt.tool_name)

        results = run_inline_tests()
        assert len(results) == 1
        assert results[0][2], f"Failed: {results}"

    def test_content_not_dropped_when_no_tool_condition(self):
        # Regression: Input(file, content) with only a FilePath condition must synthesize a
        # Write (not a bare Bash that drops file/content), so the Allow can actually fail.
        from captain_hook.testing.helpers import run_inline_tests
        from captain_hook.testing.types import Block, Input

        reset()
        register_hook(
            Event.PreToolUse,
            "no secrets",
            only_if=[FilePath("*.env")],
            block=True,
            tests={Input(file="x.env", content="API_KEY=x"): Block()},
        )
        results = run_inline_tests()
        assert len(results) == 1 and results[0][2], f"Failed: {results}"


class TestTranscriptEventPayloads:
    def test_permission_request_yields_tool_payloads(self):
        from captain_hook import T
        from captain_hook.testing.helpers import transcript_event_payloads
        from tests.helpers import make_transcript

        transcript = make_transcript(T.assistant(T.tool("Bash", command="ls")))
        payloads = list(transcript_event_payloads(Event.PermissionRequest, transcript, "/tmp/t.jsonl"))
        assert payloads == [{"transcript_path": "/tmp/t.jsonl", "tool_name": "Bash", "tool_input": {"command": "ls"}}]


class TestWorkflowSynthesis:
    def test_script_synthesizes_workflow_call(self):
        from captain_hook.testing.types import Input
        from tests.helpers import input_to_event

        evt = input_to_event(Event.PreToolUse, Input(script="log('probe')"))
        assert evt.tool_name == "Workflow"
        assert evt.input.raw["script"] == "log('probe')"


class TestOutputError:
    def test_output_populates_tool_response(self):
        from captain_hook.testing.types import Input
        from tests.helpers import input_to_event

        evt = input_to_event(Event.PostToolUse, Input(command="uv run pytest", output="3 passed"))
        assert evt.tool_response == "3 passed"

    def test_error_populates_evt_error(self):
        from captain_hook.testing.types import Input
        from tests.helpers import input_to_event

        evt = input_to_event(Event.PostToolUseFailure, Input(command="uv run pytest", error="ModuleNotFoundError"))
        assert evt.error == "ModuleNotFoundError"


class TestBlockPatternRequiresMessage:
    def test_block_pattern_fails_when_no_message(self):
        from captain_hook.testing.helpers import matches_expected
        from captain_hook.testing.types import Block

        result = HookResult(action=Action.block, message=None)
        assert matches_expected(result, Block()) is True
        assert matches_expected(result, Block(pattern="dangerous")) is False

    def test_warn_pattern_fails_when_no_message(self):
        from captain_hook.testing.helpers import matches_expected
        from captain_hook.testing.types import Warn

        result = HookResult(action=Action.warn, message=None)
        assert matches_expected(result, Warn(pattern="careful")) is False


class TestRewriteFieldsMatching:
    def test_rewrite_field_kwargs_match_updated_input(self):
        from captain_hook.testing.helpers import matches_expected
        from captain_hook.testing.types import Rewrite

        result = HookResult(action=Action.rewrite, updated_input={"model": "sonnet", "prompt": "p"})
        assert matches_expected(result, Rewrite(model="sonnet")) is True
        assert matches_expected(result, Rewrite(model="haiku")) is False
        assert matches_expected(result, Rewrite(model="sonnet", prompt="p")) is True
