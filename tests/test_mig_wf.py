from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from captain_hook.app import HookApp, _current_app
from captain_hook.dispatch import execute_hook
from captain_hook.events import (
    PostToolUseEvent,
    PreToolUseEvent,
    StopEvent,
    SubagentStartEvent,
    SubagentStopEvent,
    UserPromptSubmitEvent,
)
from captain_hook.session import SessionStore
from captain_hook.transcript import Transcript
from captain_hook.types import Action, Event


def _reload_module(name: str) -> None:
    submodules = sorted(k for k in sys.modules if k.startswith(name + "."))
    for sub in submodules:
        importlib.reload(sys.modules[sub])
    if name in sys.modules:
        importlib.reload(sys.modules[name])
    else:
        importlib.import_module(name)


def make_ctx(
    tmp_path: Path | None = None, transcript: Any = None, settings: Any = None, transcript_len: int = 10
) -> MagicMock:
    ctx = MagicMock()
    if transcript is not None:
        ctx.transcript = transcript
        ctx.t = transcript
    else:
        ctx.transcript = MagicMock()
        ctx.transcript.__len__ = MagicMock(return_value=transcript_len)
        ctx.transcript.has_skill = MagicMock(return_value=False)
        ctx.transcript.has_read = MagicMock(return_value=False)
        ctx.transcript.has_edit_to = MagicMock(return_value=False)
        ctx.transcript.has_command = MagicMock(return_value=False)
        ctx.transcript.count_tools = MagicMock(return_value=0)
        ctx.transcript.extract_files = MagicMock(return_value=[])
        ctx.transcript.assistant_text = MagicMock(return_value="")
        ctx.transcript.full_text = ""
        ctx.t = ctx.transcript
    store = SessionStore(tmp_path)
    ctx.session = store
    ctx.s = store
    ctx.conf = settings
    ctx.c = settings
    turn = MagicMock()
    turn.start_idx = 5
    ctx.turn = turn
    ctx.prior = MagicMock()
    ctx.prior.has_edit_to = MagicMock(return_value=False)
    ctx.prior.has_tool = MagicMock(return_value=False)
    ctx.prior.after = MagicMock(return_value=MagicMock())
    return ctx


def make_pre_tool_event(
    tool_name: str = "Bash", tool_input: dict[str, Any] | None = None, ctx: Any = None
) -> PreToolUseEvent:
    raw: dict[str, Any] = {"tool_name": tool_name}
    if tool_input is not None:
        raw["tool_input"] = tool_input
    return PreToolUseEvent(_raw=raw, ctx=ctx or make_ctx())


def make_post_tool_event(
    tool_name: str = "Bash", tool_input: dict[str, Any] | None = None, ctx: Any = None
) -> PostToolUseEvent:
    raw: dict[str, Any] = {"tool_name": tool_name}
    if tool_input is not None:
        raw["tool_input"] = tool_input
    return PostToolUseEvent(_raw=raw, ctx=ctx or make_ctx())


def make_stop_event(ctx: Any = None) -> StopEvent:
    return StopEvent(_raw={}, ctx=ctx or make_ctx())


def make_subagent_start_event(agent_type: str = "cleanup", ctx: Any = None) -> SubagentStartEvent:
    return SubagentStartEvent(_raw={"agent_type": agent_type}, ctx=ctx or make_ctx())


def make_subagent_stop_event(agent_type: str = "cleanup", ctx: Any = None) -> SubagentStopEvent:
    return SubagentStopEvent(_raw={"agent_type": agent_type}, ctx=ctx or make_ctx())


@pytest.fixture
def app(tmp_path: Path) -> HookApp:
    a = HookApp()
    token = _current_app.set(a)
    yield a  # type: ignore[misc]
    _current_app.reset(token)
    a.reset()


def _make_settings() -> MagicMock:
    s = MagicMock()
    s.test_command = "uv run mtest"
    s.vcs = "jj"
    s.min_edits_for_tasks = 3
    s.task_drift_threshold = 8
    s.override_token = "REMAINING_TASKS_ACKNOWLEDGED"
    s.exploration_agents = [
        "Explore",
        "Plan",
        "general-purpose",
        "explore",
        "plan",
        "web-analyzer",
        "search-specialist",
        "claude-code-guide",
        "context-manager",
        "sentry-error-debugger",
        "logfire-trace-debugger",
        "sentry:issue-summarizer",
    ]
    s.guarded_cleanup_files = "code-issues.jsonl|test-issues.jsonl|arch-issues.jsonl|correctness-fixes.jsonl"
    return s


def _apply_mixin_ctx():
    from hooks.mixins import BioqaFileMixin, apply_file_mixin

    apply_file_mixin(BioqaFileMixin)


def _cleanup_mixin():
    from hooks.mixins import cleanup_mixins

    cleanup_mixins()


class TestMigWfCleanupWorkflow:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.agents")

    def test_mig_wf_cleanup_guards_incomplete_with_step_info(self) -> None:
        from hooks.agents import CLEANUP_WORKFLOW

        ctx = make_ctx(self.tmp_path, settings=_make_settings())
        ctx.transcript.full_text = "nothing here"
        ctx.t = ctx.transcript
        evt = make_subagent_stop_event("cleanup", ctx)
        result = CLEANUP_WORKFLOW.guard(evt)
        assert result is not None
        assert result.action == Action.block
        assert "CLEANUP" in result.message
        assert len(CLEANUP_WORKFLOW.steps) == 4
        assert [s.name for s in CLEANUP_WORKFLOW.steps] == ["baseline", "correctness", "reviewers", "codex"]

    def test_mig_wf_cleanup_identifies_first_incomplete_step(self) -> None:
        from hooks.agents import CLEANUP_WORKFLOW

        ctx = make_ctx(self.tmp_path, settings=_make_settings())
        ctx.transcript.full_text = "test-baseline done, correctness-reviewer done"
        ctx.t = ctx.transcript
        evt = make_subagent_stop_event("cleanup", ctx)
        result = CLEANUP_WORKFLOW.guard(evt)
        assert result is not None
        assert "reviewer" in result.message.lower() or "codex" in result.message.lower()


class TestMigWfQualityGateWorkflow:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.agents")

    def test_mig_wf_quality_gate_guards_incomplete(self) -> None:
        from hooks.agents import QUALITY_GATE_WORKFLOW

        ctx = make_ctx(self.tmp_path, settings=_make_settings())
        ctx.transcript.full_text = "no marker"
        ctx.t = ctx.transcript
        evt = make_subagent_stop_event("quality-gate", ctx)
        result = QUALITY_GATE_WORKFLOW.guard(evt)
        assert result is not None
        assert result.action == Action.block
        assert "QUALITY-GATE" in result.message
        assert len(QUALITY_GATE_WORKFLOW.steps) == 2
        assert [s.name for s in QUALITY_GATE_WORKFLOW.steps] == ["reviewers", "codex"]


class TestMigWfTestWriterWorkflow:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.test_writer")

    def test_mig_wf_test_writer_guards_incomplete(self) -> None:
        from hooks.test_writer import TEST_WRITER_WORKFLOW

        ctx = make_ctx(self.tmp_path, settings=_make_settings())
        ctx.transcript.full_text = "no marker here"
        ctx.t = ctx.transcript
        evt = make_subagent_stop_event("test-writer", ctx)
        result = TEST_WRITER_WORKFLOW.guard(evt)
        assert result is not None
        assert result.action == Action.block
        assert "TEST-WRITER" in result.message
        assert len(TEST_WRITER_WORKFLOW.steps) == 3
        assert [s.name for s in TEST_WRITER_WORKFLOW.steps] == ["analysis", "tracker", "tests"]

    def test_mig_wf_test_writer_blocks_without_mtest_evidence(self) -> None:
        from hooks.test_writer import TEST_WRITER_WORKFLOW

        ctx = make_ctx(self.tmp_path, settings=_make_settings())
        ctx.transcript.full_text = "=== TEST WRITER COMPLETE === but no mtest output"
        ctx.t = ctx.transcript
        evt = make_subagent_stop_event("test-writer", ctx)
        result = TEST_WRITER_WORKFLOW.guard(evt)
        assert result is not None
        assert result.action == Action.block


class TestMigWfGuardBashRedirects:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.agents")

    def test_mig_wf_redirect_guarded_blocked(self) -> None:
        ctx = make_ctx(self.tmp_path, settings=_make_settings())
        evt = make_pre_tool_event("Bash", {"command": "echo hello > .context/cleanup/code-issues.jsonl"}, ctx)
        results = [execute_hook(e, evt, self.tmp_path) for e in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.block and "orchestrator" in (r.message or "").lower() for r in results)

    def test_mig_wf_redirect_non_guarded_allowed(self) -> None:
        ctx = make_ctx(self.tmp_path, settings=_make_settings())
        evt = make_pre_tool_event("Bash", {"command": "echo hello > output.txt"}, ctx)
        results = [execute_hook(e, evt, self.tmp_path) for e in self.app.get_matching_hooks(evt)]
        assert not any(r and r.action == Action.block and "orchestrator" in (r.message or "").lower() for r in results)


class TestMigWfGuardDirectWrites:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.agents")

    def test_mig_wf_write_guarded_blocked(self) -> None:
        _apply_mixin_ctx()
        try:
            ctx = make_ctx(self.tmp_path, settings=_make_settings())
            evt = make_pre_tool_event(
                "Write", {"file_path": ".context/cleanup/code-issues.jsonl", "content": "[]"}, ctx
            )
            results = [execute_hook(e, evt, self.tmp_path) for e in self.app.get_matching_hooks(evt)]
            assert any(r and r.action == Action.block and "orchestrator" in (r.message or "").lower() for r in results)
        finally:
            _cleanup_mixin()

    def test_mig_wf_write_normal_allowed(self) -> None:
        ctx = make_ctx(self.tmp_path, settings=_make_settings())
        evt = make_pre_tool_event("Write", {"file_path": "bioqa/core.py", "content": "pass"}, ctx)
        results = [execute_hook(e, evt, self.tmp_path) for e in self.app.get_matching_hooks(evt)]
        assert not any(r and r.action == Action.block and "orchestrator" in (r.message or "").lower() for r in results)


class TestMigWfGuardPrematurePythonEdits:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.agents")

    def test_mig_wf_premature_edit_blocked(self) -> None:
        from hooks.agents import CleanupScope

        ctx = make_ctx(self.tmp_path, settings=_make_settings())
        ctx.s[CleanupScope].set(CleanupScope(source_files=["bioqa/core.py"]))
        evt = make_pre_tool_event("Edit", {"file_path": "bioqa/core.py", "old_string": "a", "new_string": "b"}, ctx)
        results = [execute_hook(e, evt, self.tmp_path) for e in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.block and "reviewer" in (r.message or "").lower() for r in results)

    def test_mig_wf_edit_allowed_after_reviewers(self) -> None:
        from hooks.agents import CleanupScope, ReviewerOutput

        ctx = make_ctx(self.tmp_path, settings=_make_settings())
        ctx.s[CleanupScope].set(CleanupScope(source_files=["bioqa/core.py"]))
        ctx.s[ReviewerOutput].set(ReviewerOutput(tracks={"code"}))
        evt = make_pre_tool_event("Edit", {"file_path": "bioqa/core.py", "old_string": "a", "new_string": "b"}, ctx)
        results = [execute_hook(e, evt, self.tmp_path) for e in self.app.get_matching_hooks(evt)]
        assert not any(r and r.action == Action.block and "reviewer" in (r.message or "").lower() for r in results)


class TestMigWfNoPlanRewrites:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.workflow")

    def test_mig_wf_plan_rewrite_blocked(self, tmp_path: Path) -> None:
        _apply_mixin_ctx()
        try:
            plan_file = tmp_path / "docs" / "plans" / "plan.md"
            plan_file.parent.mkdir(parents=True, exist_ok=True)
            plan_file.write_text("original")
            ctx = make_ctx(self.tmp_path, settings=_make_settings())
            ctx.prior.has_edit_to = MagicMock(return_value=True)
            after_mock = MagicMock()
            after_mock.has_tool = MagicMock(return_value=False)
            ctx.prior.after = MagicMock(return_value=after_mock)
            evt = make_pre_tool_event("Write", {"file_path": str(plan_file), "content": "new content"}, ctx)
            results = [execute_hook(e, evt, self.tmp_path) for e in self.app.get_matching_hooks(evt)]
            assert any(r and r.action == Action.block and "plan" in (r.message or "").lower() for r in results)
        finally:
            _cleanup_mixin()


class TestMigWfPlanReentryTranscriptLink:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.workflow")

    def test_mig_wf_reentry_blocked_without_link(self) -> None:
        _apply_mixin_ctx()
        try:
            ctx = make_ctx(self.tmp_path, settings=_make_settings())
            ctx.transcript.has_tool = MagicMock(return_value=True)
            ctx.t = ctx.transcript
            evt = make_pre_tool_event(
                "Write", {"file_path": "docs/plans/new-plan.md", "content": "# Plan\n\n1. Do things"}, ctx
            )
            results = [execute_hook(e, evt, self.tmp_path) for e in self.app.get_matching_hooks(evt)]
            assert any(r and r.action == Action.block and "transcript" in (r.message or "").lower() for r in results)
        finally:
            _cleanup_mixin()

    def test_mig_wf_reentry_allowed_with_link(self) -> None:
        _apply_mixin_ctx()
        try:
            ctx = make_ctx(self.tmp_path, settings=_make_settings())
            ctx.transcript.has_tool = MagicMock(return_value=True)
            ctx.t = ctx.transcript
            content = "# Plan\n\n**Previous transcript**: /home/.claude/sessions/abc.jsonl\n\n1. Do things"
            evt = make_pre_tool_event("Write", {"file_path": "docs/plans/new-plan.md", "content": content}, ctx)
            results = [execute_hook(e, evt, self.tmp_path) for e in self.app.get_matching_hooks(evt)]
            assert not any(
                r and r.action == Action.block and "transcript" in (r.message or "").lower() for r in results
            )
        finally:
            _cleanup_mixin()


class TestMigWfTaskCompletionGate:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.workflow")

    def test_mig_wf_blocks_edits_no_tasks(self) -> None:
        ctx = make_ctx(self.tmp_path, settings=_make_settings())
        ctx.transcript.project_edit_count = 5
        ctx.transcript.task_stats = (0, 0)
        ctx.transcript.has_override = MagicMock(return_value=False)
        ctx.t = ctx.transcript
        results = [
            execute_hook(e, make_stop_event(ctx), self.tmp_path)
            for e in self.app.get_matching_hooks(make_stop_event(ctx))
        ]
        assert any(r and r.action == Action.block and "task" in (r.message or "").lower() for r in results)

    def test_mig_wf_allows_with_override(self) -> None:
        ctx = make_ctx(self.tmp_path, settings=_make_settings())
        ctx.transcript.project_edit_count = 5
        ctx.transcript.task_stats = (0, 0)
        ctx.transcript.has_override = MagicMock(return_value=True)
        ctx.t = ctx.transcript
        results = [
            execute_hook(e, make_stop_event(ctx), self.tmp_path)
            for e in self.app.get_matching_hooks(make_stop_event(ctx))
        ]
        assert not any(r and r.action == Action.block and "task" in (r.message or "").lower() for r in results)

    def test_mig_wf_blocks_pending_tasks(self) -> None:
        ctx = make_ctx(self.tmp_path, settings=_make_settings())
        ctx.transcript.project_edit_count = 5
        ctx.transcript.task_stats = (5, 2)
        ctx.transcript.has_override = MagicMock(return_value=False)
        ctx.t = ctx.transcript
        results = [
            execute_hook(e, make_stop_event(ctx), self.tmp_path)
            for e in self.app.get_matching_hooks(make_stop_event(ctx))
        ]
        assert any(r and r.action == Action.block and "pending" in (r.message or "").lower() for r in results)


class TestMigWfTaskDriftReminder:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.workflow")

    def test_mig_wf_drift_warns_after_threshold(self) -> None:
        ctx = make_ctx(self.tmp_path, settings=_make_settings())
        ctx.transcript.count_tools = MagicMock(return_value=1)
        after_mock = MagicMock()
        after_mock.tool_uses = MagicMock()
        query_mock = MagicMock()
        query_mock.count = MagicMock(return_value=10)
        after_mock.tool_uses.where = MagicMock(return_value=query_mock)
        ctx.transcript.after = MagicMock(return_value=after_mock)
        ctx.t = ctx.transcript
        evt = make_post_tool_event("Edit", {"file_path": "bioqa/core.py", "old_string": "", "new_string": ""}, ctx)
        results = [execute_hook(e, evt, self.tmp_path) for e in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn and "task" in (r.message or "").lower() for r in results)


class TestMigWfPlanApprovedNudge:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.workflow")

    def test_mig_wf_plan_nudge_fires_on_exit_plan(self) -> None:
        ctx = make_ctx(self.tmp_path, settings=_make_settings())
        evt = make_post_tool_event("ExitPlanMode", {}, ctx)
        results = [execute_hook(e, evt, self.tmp_path) for e in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn and "taskcreate" in (r.message or "").lower() for r in results)


class TestMigWfMultiRequestNudge:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.workflow")

    def test_mig_wf_multi_warns_numbered_list(self) -> None:
        ctx = make_ctx(self.tmp_path, settings=_make_settings())
        t = Transcript.from_messages([])
        ctx.transcript = t
        ctx.t = t
        evt = UserPromptSubmitEvent(_raw={"prompt": "1. Add a feature\n2. Fix the bug\n3. Update the tests"}, ctx=ctx)
        results = [execute_hook(e, evt, self.tmp_path) for e in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn and "task" in (r.message or "").lower() for r in results)


class TestMigWfContextGatheringGate:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.workflow")

    def test_mig_wf_context_gate_warns_without_exploration(self) -> None:
        ctx = make_ctx(self.tmp_path, settings=_make_settings())
        ctx.transcript.extract_files = MagicMock(return_value=[])
        ctx.transcript.tool_uses = MagicMock()
        ctx.transcript.tool_uses.where = MagicMock(return_value=MagicMock(__iter__=lambda s: iter([])))
        ctx.t = ctx.transcript
        evt = make_pre_tool_event("Edit", {"file_path": "bioqa/core.py", "old_string": "", "new_string": ""}, ctx)
        results = [execute_hook(e, evt, self.tmp_path) for e in self.app.get_matching_hooks(evt)]
        assert any(
            r
            and r.action == Action.warn
            and ("explor" in (r.message or "").lower() or "first edit" in (r.message or "").lower())
            for r in results
        )


class TestMigWfUtilReinvention:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.workflow")

    def test_mig_wf_util_reinvention_matches_asyncio_pattern(self) -> None:
        async_hooks = [h for h in self.app.hooks if h.spec.async_]
        assert len(async_hooks) >= 1
        assert async_hooks[0].spec.max_fires == 3
        ctx = make_ctx(self.tmp_path, settings=_make_settings())
        evt = make_post_tool_event(
            "Edit", {"file_path": "bioqa/core.py", "old_string": "", "new_string": "asyncio.gather(coros)"}, ctx
        )
        matched_async = [h for h in self.app.get_matching_hooks(evt) if h.spec.async_]
        assert len(matched_async) >= 1


class TestMigWfLocalDocs:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path

    def test_mig_wf_local_docs_discovers_and_names_nudges(self) -> None:
        import os

        hook_dir = self.tmp_path / "hooks"
        hook_dir.mkdir()
        for name in ("module_a", "module_b"):
            d = hook_dir / name
            d.mkdir()
            (d / "AGENTS.md").write_text(f"# {name}")
        before = len(self.app.hooks)
        old_cwd = os.getcwd()
        os.chdir(str(hook_dir))
        try:
            _reload_module("hooks.workflow.local_docs")
        finally:
            os.chdir(old_cwd)
        new_hooks = self.app.hooks[before:]
        assert len(new_hooks) >= 2, f"Expected at least 2 local doc nudges, got {len(new_hooks)}"
        ctx = make_ctx(self.tmp_path, settings=_make_settings())
        evt_a = make_pre_tool_event("Edit", {"file_path": "module_a/core.py", "old_string": "", "new_string": ""}, ctx)
        matching_a = [h for h in new_hooks if h in self.app.get_matching_hooks(evt_a)]
        assert len(matching_a) >= 1, "Should match nudge for module_a"


class TestMigWfDirenvPrefixApproval:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.workflow")

    def test_mig_wf_direnv_responds_to_bash_events(self) -> None:
        direnv_hooks = [h for h in self.app.hooks if h.handler and "direnv" in h.name.lower()]
        assert len(direnv_hooks) == 1
        assert direnv_hooks[0].spec.events == Event.PreToolUse


class TestMigWfSetupCleanupPipeline:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.agents")

    def test_mig_wf_setup_returns_warn_with_scope(self) -> None:
        ctx = make_ctx(self.tmp_path, settings=_make_settings())
        ctx.call_cli = MagicMock(
            side_effect=lambda args, **kw: (
                "op-id-123\n" if "op" in " ".join(args) else "bioqa/core.py\ntests/test_core.py\n"
            )
        )
        evt = make_subagent_start_event("cleanup", ctx)
        results = [execute_hook(e, evt, self.tmp_path) for e in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn and "CLEANUP SETUP" in (r.message or "") for r in results)

    def test_mig_wf_setup_ignores_non_cleanup(self) -> None:
        ctx = make_ctx(self.tmp_path, settings=_make_settings())
        evt = make_subagent_start_event("feature-implementer", ctx)
        results = [execute_hook(e, evt, self.tmp_path) for e in self.app.get_matching_hooks(evt)]
        assert not any(r and "CLEANUP SETUP" in (r.message or "") for r in results)


class TestMigWfTrackReviewerOutput:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.agents")

    def test_mig_wf_track_populates_code(self) -> None:
        from hooks.agents import ReviewerOutput

        ctx = make_ctx(self.tmp_path, settings=_make_settings())
        evt = make_subagent_stop_event("cleanup-code-reviewer", ctx)
        for e in self.app.get_matching_hooks(evt):
            execute_hook(e, evt, self.tmp_path)
        assert "code" in ctx.s[ReviewerOutput].get().tracks  # type: ignore[union-attr]

    def test_mig_wf_track_skips_cleanup(self) -> None:
        from hooks.agents import ReviewerOutput

        ctx = make_ctx(self.tmp_path, settings=_make_settings())
        evt = make_subagent_stop_event("cleanup", ctx)
        for e in self.app.get_matching_hooks(evt):
            execute_hook(e, evt, self.tmp_path)
        output = ctx.s[ReviewerOutput].get()
        assert output is None or not output.tracks

    def test_mig_wf_track_accumulates(self) -> None:
        from hooks.agents import ReviewerOutput

        ctx = make_ctx(self.tmp_path, settings=_make_settings())
        for agent in ["cleanup-code-reviewer", "cleanup-test-reviewer", "cleanup-arch-reviewer"]:
            evt = make_subagent_stop_event(agent, ctx)
            for e in self.app.get_matching_hooks(evt):
                execute_hook(e, evt, self.tmp_path)
        assert ctx.s[ReviewerOutput].get().tracks == {"code", "test", "arch"}  # type: ignore[union-attr]


class TestMigWfInMissionMode:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.workflow")

    def test_mig_wf_mission_mode_detects_tag(self, tmp_path: Path) -> None:
        from hooks.workflow.task_tracking import InMissionMode

        sd = tmp_path / "session"
        sd.mkdir()
        t = sd / "transcript.jsonl"
        t.write_text("")
        (sd / "transcript.settings.json").write_text(json.dumps({"tags": [{"name": "mission-worker"}]}))
        evt = MagicMock()
        evt._raw = {"transcript_path": str(t)}
        assert InMissionMode().check(evt) is True

    def test_mig_wf_mission_mode_false_empty_tags(self, tmp_path: Path) -> None:
        from hooks.workflow.task_tracking import InMissionMode

        sd = tmp_path / "session"
        sd.mkdir()
        t = sd / "transcript.jsonl"
        t.write_text("")
        (sd / "transcript.settings.json").write_text(json.dumps({"tags": []}))
        evt = MagicMock()
        evt._raw = {"transcript_path": str(t)}
        assert InMissionMode().check(evt) is False

    def test_mig_wf_mission_mode_false_no_settings(self) -> None:
        from hooks.workflow.task_tracking import InMissionMode

        evt = MagicMock()
        evt._raw = {"transcript_path": "/nonexistent/transcript.jsonl"}
        assert InMissionMode().check(evt) is False

    def test_mig_wf_mission_mode_suppresses_hooks_at_dispatch(self, tmp_path: Path) -> None:
        sd = tmp_path / "session"
        sd.mkdir()
        t_path = sd / "transcript.jsonl"
        t_path.write_text("")
        (sd / "transcript.settings.json").write_text(json.dumps({"tags": [{"name": "mission-worker"}]}))
        ctx = make_ctx(self.tmp_path, settings=_make_settings())
        ctx.transcript.project_edit_count = 5
        ctx.transcript.task_stats = (0, 0)
        ctx.transcript.has_override = MagicMock(return_value=False)
        ctx.t = ctx.transcript
        evt = StopEvent(_raw={"transcript_path": str(t_path)}, ctx=ctx)
        task_hooks = [h for h in self.app.hooks if h.handler and "task_completion" in h.name]
        for h in task_hooks:
            assert any(c.check(evt) for c in h.spec.skip_if if hasattr(c, "check"))


class TestMigWfStateModels:
    def test_mig_wf_snapshot_roundtrip(self, tmp_path: Path) -> None:
        from hooks.agents import Snapshot

        store = SessionStore(tmp_path)
        store[Snapshot].set(Snapshot(op_id="abc123"))
        assert store[Snapshot].get().op_id == "abc123"  # type: ignore[union-attr]

    def test_mig_wf_run_status_roundtrip(self, tmp_path: Path) -> None:
        from hooks.agents import RunStatus

        store = SessionStore(tmp_path)
        store[RunStatus].set(RunStatus(status="running"))
        assert store[RunStatus].get().status == "running"  # type: ignore[union-attr]

    def test_mig_wf_cleanup_scope_behavior(self) -> None:
        from hooks.agents import CleanupScope

        assert CleanupScope(source_files=["a.py"], test_files=["test_a.py"]).has_files
        assert not CleanupScope().has_files

    def test_mig_wf_reviewer_output_roundtrip(self, tmp_path: Path) -> None:
        from hooks.agents import ReviewerOutput

        store = SessionStore(tmp_path)
        store[ReviewerOutput].set(ReviewerOutput(tracks={"code", "test"}))
        assert store[ReviewerOutput].get().tracks == {"code", "test"}  # type: ignore[union-attr]
