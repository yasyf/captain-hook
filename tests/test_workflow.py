from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from cc_transcript.query import Session
from pydantic import BaseModel

from captain_hook.app import _state
from captain_hook.events import SubagentStopEvent
from captain_hook.primitives.workflow import Step
from captain_hook.types import Action, Event, HookResult
from tests.helpers import build_ctx, make_subagent_stop_event, make_transcript, raw_text


class ArtifactModel(BaseModel):
    name: str
    count: int


def make_evt(
    full_text: str = "",
    *,
    session_dir: Path | None = None,
) -> SubagentStopEvent:
    ctx = build_ctx(transcript=make_transcript(raw_text("assistant", full_text)), session_dir=session_dir)
    return make_subagent_stop_event(ctx)


class TestTextMatches:
    def test_returns_callable_predicate(self) -> None:
        from captain_hook.primitives.workflow import text_matches

        pred = text_matches(r"ALL_TESTS_PASS")
        assert callable(pred)

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("ALL_TESTS_PASS done", True, id="matches_when_pattern_found"),
            pytest.param("nothing here", False, id="no_match_when_pattern_absent"),
        ],
    )
    def test_match(self, text: str, expected: bool) -> None:
        from captain_hook.primitives.workflow import text_matches

        t = make_transcript(raw_text("assistant", text))
        assert text_matches(r"ALL_TESTS_PASS")(t) is expected


class TestStep:
    def test_frozen_dataclass(self) -> None:
        from captain_hook.primitives.workflow import Step

        s = Step(name="test", check=lambda _: True, message="Step 1: Write tests.")
        assert s.name == "test"
        assert s.message == "Step 1: Write tests."
        assert Step(check=lambda _: True, message="m").name == ""
        with pytest.raises(AttributeError):
            s.name = "other"  # type: ignore[misc]

    def test_check_is_transcript_predicate(self) -> None:
        from captain_hook.primitives.workflow import Step

        called_with: list[Any] = []

        def checker(t: Session) -> bool:
            called_with.append(t)
            return True

        s = Step(name="test", check=checker, message="Stop Next")
        t = make_transcript(raw_text("assistant", "hello"))
        assert s.check(t) is True
        assert len(called_with) == 1


class TestArtifact:
    def test_frozen_generic_dataclass(self) -> None:
        from captain_hook.primitives.workflow import Artifact

        a: Artifact[ArtifactModel] = Artifact(path="/tmp/out.json", model=ArtifactModel)
        assert a.path == "/tmp/out.json"
        assert a.model is ArtifactModel
        with pytest.raises(AttributeError):
            a.path = "/other"  # type: ignore[misc]

    def test_validate_defaults_to_none_returning(self) -> None:
        from captain_hook.primitives.workflow import Artifact

        a: Artifact[ArtifactModel] = Artifact(path="/tmp/out.json", model=ArtifactModel)
        assert a.validate(ArtifactModel(name="x", count=1)) is None


class TestWorkflowFunction:
    def test_registers_hook(self) -> None:
        from captain_hook.primitives.workflow import Step, workflow

        initial_count = len(_state.hooks)
        workflow(
            label="TEST",
            marker="DONE",
            steps=[Step(check=lambda _: True, message="Step 1: Do it.")],
        )
        assert len(_state.hooks) == initial_count + 1

    def test_registered_hook_is_subagent_stop(self) -> None:
        from captain_hook.primitives.workflow import Step, workflow

        workflow(
            label="TEST",
            marker="DONE",
            steps=[Step(check=lambda _: True, message="Step 1: Do it.")],
        )
        hook = _state.hooks[-1]
        assert Event.SubagentStop in hook.spec.events

    def test_registered_hook_has_max_fires_1(self) -> None:
        from captain_hook.primitives.workflow import Step, workflow

        workflow(
            label="TEST",
            marker="DONE",
            steps=[Step(check=lambda _: True, message="Step 1: Do it.")],
        )
        hook = _state.hooks[-1]
        assert hook.spec.max_fires == 1

    def test_passes_tests_to_hook_spec(self) -> None:
        from captain_hook.primitives.workflow import Step, workflow

        tests: dict[str, Any] = {"input1": "block"}
        workflow(
            label="TEST",
            marker="DONE",
            steps=[Step(check=lambda _: True, message="Step 1: Do it.")],
            tests=tests,
        )
        hook = _state.hooks[-1]
        assert hook.spec.tests is tests

    def test_registered_hook_skips_planning_agents_by_default(self) -> None:
        from captain_hook.primitives.workflow import Step, workflow

        workflow(
            label="TEST",
            marker="DONE",
            steps=[Step(check=lambda _: True, message="Step 1: Do it.")],
        )
        assert _state.hooks[-1].spec.skip_planning_agents is True

    def test_passes_only_if_to_hook_spec(self) -> None:
        from captain_hook.primitives.workflow import Step, workflow
        from captain_hook.types import Agent

        workflow(
            label="TEST",
            marker="DONE",
            steps=[Step(check=lambda _: True, message="Step 1: Do it.")],
            only_if=[Agent("cleanup")],
        )
        assert _state.hooks[-1].spec.only_if == (Agent("cleanup"),)

    def test_passes_skip_if_to_hook_spec(self) -> None:
        from captain_hook.primitives.workflow import Step, workflow
        from captain_hook.types import Agent

        workflow(
            label="TEST",
            marker="DONE",
            steps=[Step(check=lambda _: True, message="Step 1: Do it.")],
            skip_if=[Agent("Explore")],
        )
        assert _state.hooks[-1].spec.skip_if == (Agent("Explore"),)

    def test_default_only_if_empty_and_skip_if_wait_aware(self) -> None:
        from captain_hook.primitives.workflow import Step, workflow
        from captain_hook.types import Waiting

        workflow(
            label="TEST",
            marker="DONE",
            steps=[Step(check=lambda _: True, message="Step 1: Do it.")],
        )
        assert _state.hooks[-1].spec.only_if == ()
        assert _state.hooks[-1].spec.skip_if == (Waiting(),)


class TestWorkflowOnStart:
    def test_on_start_registers_both_subagent_start_and_stop(self) -> None:
        from captain_hook.primitives.workflow import Step, workflow
        from captain_hook.types import Agent

        before = len(_state.hooks)
        workflow(
            label="CLEANUP",
            marker="DONE",
            steps=[Step(name="s1", check=lambda _: True, message="S: N")],
            on_start=lambda _: HookResult(action=Action.warn, message="setup ran"),
            only_if=[Agent("cleanup")],
        )
        assert len(_state.hooks) == before + 2

        stop_hooks = [h for h in _state.hooks if Event.SubagentStop in h.spec.events]
        start_hooks = [h for h in _state.hooks if Event.SubagentStart in h.spec.events]
        assert len(stop_hooks) == 1
        assert len(start_hooks) == 1

        assert stop_hooks[0].spec.max_fires == 1
        assert stop_hooks[0].spec.only_if == (Agent("cleanup"),)
        assert start_hooks[0].spec.max_fires is None
        assert start_hooks[0].spec.only_if == (Agent("cleanup"),)

    def test_on_start_handler_invokes_callback(self) -> None:
        from captain_hook.primitives.workflow import Step, workflow
        from captain_hook.types import Agent

        captured: list[Any] = []

        def starter(evt: Any) -> HookResult:
            captured.append(evt)
            return HookResult(action=Action.warn, message="setup ran")

        workflow(
            label="CLEANUP",
            marker="DONE",
            steps=[Step(name="s1", check=lambda _: True, message="S: N")],
            on_start=starter,
            only_if=[Agent("cleanup")],
        )
        setup = next(h for h in _state.hooks if Event.SubagentStart in h.spec.events)
        evt = make_evt()
        result = setup.handler(evt)
        assert result is not None
        assert result.message == "setup ran"
        assert captured == [evt]

    def test_no_on_start_registers_only_stop_guard(self) -> None:
        from captain_hook.primitives.workflow import Step, workflow

        before = len(_state.hooks)
        workflow(
            label="PLAIN",
            marker="DONE",
            steps=[Step(name="s1", check=lambda _: True, message="S: N")],
        )
        assert len(_state.hooks) == before + 1
        assert [h for h in _state.hooks if Event.SubagentStart in h.spec.events] == []


class TestWorkflowGuard:
    def test_blocks_when_marker_absent(self) -> None:
        from captain_hook.primitives.workflow import Step, Workflow

        w = Workflow(
            label="TDD",
            marker="ALL_PASS",
            steps=[Step(name="test", check=lambda _: False, message="Step 1: Write test.")],
        )
        evt = make_evt("no marker here")
        result = w.guard(evt)
        assert result is not None
        assert result.action == Action.block
        assert "TDD" in result.message  # type: ignore[operator]

    def test_evaluates_steps_in_order_finds_first_uncompleted(self) -> None:
        from captain_hook.primitives.workflow import Step, Workflow

        w = Workflow(
            label="BUILD",
            marker="DONE",
            steps=[
                Step(name="step1", check=lambda _: True, message="S1: Do step 2."),
                Step(name="step2", check=lambda _: False, message="S2: Do step 3."),
                Step(name="step3", check=lambda _: False, message="S3: Finish."),
            ],
        )
        evt = make_evt("no marker")
        result = w.guard(evt)
        assert result is not None
        assert result.action == Action.block
        assert "S2:" in result.message  # type: ignore[operator]
        assert "Do step 3." in result.message  # type: ignore[operator]

    @pytest.mark.parametrize(
        ("steps", "stopped_at", "next_step"),
        [
            pytest.param(
                [Step(name="s1", check=lambda _: False, message="Stop here Then do this")],
                "Stop here",
                "Then do this",
                id="guard_message_includes_stopped_at_and_next_step",
            ),
            pytest.param(
                [
                    Step(name="s1", check=lambda _: False, message="Step1: Do step 1."),
                    Step(name="s2", check=lambda _: False, message="Step2: Do step 2."),
                ],
                "Step1:",
                "Do step 1.",
                id="no_completed_steps_reports_first",
            ),
        ],
    )
    def test_guard_message_includes_stopped_at_and_next_step(
        self, steps: list[Step], stopped_at: str, next_step: str
    ) -> None:
        from captain_hook.primitives.workflow import Workflow

        w = Workflow(label="FLOW", marker="MARKER", steps=steps)
        evt = make_evt("no marker")
        result = w.guard(evt)
        assert result is not None
        assert stopped_at in result.message  # type: ignore[operator]
        assert next_step in result.message  # type: ignore[operator]

    def test_guard_invokes_post_complete_when_all_pass(self) -> None:
        from captain_hook.primitives.workflow import Step, Workflow

        expected_result = HookResult(action=Action.warn, message="post complete done")
        post_called: list[Any] = []

        def post(evt: Any) -> HookResult:
            post_called.append(evt)
            return expected_result

        w = Workflow(
            label="X",
            marker="DONE",
            steps=[Step(name="s1", check=lambda _: True, message="S1: N/A")],
            post_complete=post,
        )
        evt = make_evt("DONE is here")
        result = w.guard(evt)
        assert len(post_called) == 1
        assert result is expected_result

    def test_guard_returns_none_when_no_post_complete(self) -> None:
        from captain_hook.primitives.workflow import Step, Workflow

        w = Workflow(
            label="X",
            marker="DONE",
            steps=[Step(name="s1", check=lambda _: True, message="S1: N/A")],
        )
        evt = make_evt("DONE is here")
        result = w.guard(evt)
        assert result is None

    def test_guard_blocks_on_missing_artifact_file(self) -> None:
        from captain_hook.primitives.workflow import Artifact, Step, Workflow

        w = Workflow(
            label="ART",
            marker="DONE",
            steps=[Step(name="s1", check=lambda _: True, message="S: N")],
            artifacts=[Artifact(path="/nonexistent/artifact.json", model=ArtifactModel)],
        )
        evt = make_evt("DONE is here")
        result = w.guard(evt)
        assert result is not None
        assert result.action == Action.block
        assert "/nonexistent/artifact.json" in result.message  # type: ignore[operator]
        assert "not found" in result.message  # type: ignore[operator]

    def test_guard_blocks_on_artifact_validation_failure(self, tmp_path: Path) -> None:
        from captain_hook.primitives.workflow import Artifact, Step, Workflow

        artifact_path = tmp_path / "out.json"
        artifact_path.write_text(json.dumps({"name": "test", "count": 5}))

        def bad_validate(m: ArtifactModel) -> str | None:
            return "count must be > 10" if m.count <= 10 else None

        w = Workflow(
            label="VAL",
            marker="DONE",
            steps=[Step(name="s1", check=lambda _: True, message="S: N")],
            artifacts=[Artifact(path=str(artifact_path), model=ArtifactModel, validate=bad_validate)],
        )
        evt = make_evt("DONE is here")
        result = w.guard(evt)
        assert result is not None
        assert result.action == Action.block
        assert "count must be > 10" in result.message  # type: ignore[operator]

    def test_guard_all_steps_complete_but_artifacts_fail(self, tmp_path: Path) -> None:
        from captain_hook.primitives.workflow import Artifact, Step, Workflow

        w = Workflow(
            label="MIX",
            marker="DONE",
            steps=[
                Step(name="s1", check=lambda _: True, message="S: N"),
                Step(name="s2", check=lambda _: True, message="S2: N2"),
            ],
            artifacts=[Artifact(path="/nonexistent/file.json", model=ArtifactModel)],
        )
        evt = make_evt("DONE is here")
        result = w.guard(evt)
        assert result is not None
        assert result.action == Action.block

    def test_guard_blocks_on_malformed_artifact_json(self, tmp_path: Path) -> None:
        from captain_hook.primitives.workflow import Artifact, Step, Workflow

        artifact_path = tmp_path / "bad.json"
        artifact_path.write_text("{not valid json")

        w = Workflow(
            label="BAD",
            marker="DONE",
            steps=[Step(name="s1", check=lambda _: True, message="S: N")],
            artifacts=[Artifact(path=str(artifact_path), model=ArtifactModel)],
        )
        evt = make_evt("DONE is here")
        result = w.guard(evt)
        assert result is not None
        assert result.action == Action.block

    def test_guard_blocks_on_artifact_schema_mismatch(self, tmp_path: Path) -> None:
        from captain_hook.primitives.workflow import Artifact, Step, Workflow

        artifact_path = tmp_path / "mismatch.json"
        artifact_path.write_text(json.dumps({"wrong": "fields"}))

        w = Workflow(
            label="SCHEMA",
            marker="DONE",
            steps=[Step(name="s1", check=lambda _: True, message="S: N")],
            artifacts=[Artifact(path=str(artifact_path), model=ArtifactModel)],
        )
        evt = make_evt("DONE is here")
        result = w.guard(evt)
        assert result is not None
        assert result.action == Action.block

    def test_label_prefix_in_all_block_messages(self) -> None:
        from captain_hook.primitives.workflow import Step, Workflow

        w = Workflow(
            label="MYWORKFLOW",
            marker="COMPLETE",
            steps=[Step(name="s1", check=lambda _: False, message="S: Do it.")],
        )
        evt = make_evt("no marker")
        result = w.guard(evt)
        assert result is not None
        assert result.message is not None
        assert result.message.startswith("MYWORKFLOW")

    def test_guard_valid_artifact_passes(self, tmp_path: Path) -> None:
        from captain_hook.primitives.workflow import Artifact, Step, Workflow

        artifact_path = tmp_path / "good.json"
        artifact_path.write_text(json.dumps({"name": "ok", "count": 42}))

        w = Workflow(
            label="OK",
            marker="DONE",
            steps=[Step(name="s1", check=lambda _: True, message="S: N")],
            artifacts=[Artifact(path=str(artifact_path), model=ArtifactModel)],
        )
        evt = make_evt("DONE is here")
        result = w.guard(evt)
        assert result is None

    def test_sequential_finds_first_uncompleted_step(self) -> None:
        from captain_hook.primitives.workflow import Step, Workflow

        w = Workflow(
            label="SEQ",
            marker="DONE",
            steps=[
                Step(name="s1", check=lambda _: True, message="S1: N1"),
                Step(name="s2", check=lambda _: False, message="S2: N2"),
                Step(name="s3", check=lambda _: True, message="S3: N3"),
            ],
        )
        evt = make_evt("no marker")
        result = w.guard(evt)
        assert result is not None
        assert "S2:" in result.message  # type: ignore[operator]
        assert "N2" in result.message  # type: ignore[operator]
        assert "S3:" not in result.message  # type: ignore[operator]

    def test_sequential_does_not_skip_to_later_completed(self) -> None:
        from captain_hook.primitives.workflow import Step, Workflow

        w = Workflow(
            label="SEQ",
            marker="DONE",
            steps=[
                Step(name="s1", check=lambda _: False, message="S1: N1"),
                Step(name="s2", check=lambda _: True, message="S2: N2"),
                Step(name="s3", check=lambda _: True, message="S3: N3"),
            ],
        )
        evt = make_evt("no marker")
        result = w.guard(evt)
        assert result is not None
        assert "S1:" in result.message  # type: ignore[operator]
