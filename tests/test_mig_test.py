from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from captain_hook.app import HookApp, _current_app
from captain_hook.dispatch import execute_hook
from captain_hook.events import (
    PostToolUseEvent,
    PostToolUseFailureEvent,
    PreToolUseEvent,
    SubagentStopEvent,
)
from captain_hook.file import File
from captain_hook.session import SessionStore
from captain_hook.transcript import Transcript
from captain_hook.types import Action


def _reload_module(name: str) -> None:
    import importlib
    import sys

    if name in sys.modules:
        importlib.reload(sys.modules[name])
    else:
        importlib.import_module(name)


def make_ctx(
    tmp_path: Path | None = None,
    transcript: Any = None,
    settings: Any = None,
) -> MagicMock:
    ctx = MagicMock()
    if transcript is not None:
        ctx.transcript = transcript
        ctx.t = transcript
    else:
        ctx.transcript = MagicMock()
        ctx.transcript.has_skill = MagicMock(return_value=False)
        ctx.transcript.has_read = MagicMock(return_value=False)
        ctx.transcript.has_edit_to = MagicMock(return_value=False)
        ctx.transcript.has_command = MagicMock(return_value=False)
        ctx.transcript.has_override = MagicMock(return_value=False)
        ctx.transcript.count_tools = MagicMock(return_value=0)
        ctx.transcript.count_failures = MagicMock(return_value=0)
        ctx.transcript.extract_files = MagicMock(return_value=[])
        ctx.transcript.all_edits_under = MagicMock(return_value=False)
        ctx.transcript.user_said = MagicMock(return_value=False)
        ctx.transcript.messages = []
        ctx.transcript.commands = []
        ctx.transcript.full_text = ""
        ctx.t = ctx.transcript
    store = SessionStore(tmp_path)
    ctx.session = store
    ctx.s = store
    ctx.conf = settings
    ctx.c = settings
    turn = MagicMock()
    turn.start_idx = 5
    turn.count_failures = MagicMock(return_value=0)
    turn.count_tools = MagicMock(return_value=0)
    turn.extract_files = MagicMock(return_value=[])
    turn.tool_uses = MagicMock()
    turn.tool_uses.with_errors = []
    ctx.turn = turn
    return ctx


def make_settings() -> MagicMock:
    from hooks.conf import BioqaSettings

    return BioqaSettings()


@pytest.fixture
def app(tmp_path: Path) -> HookApp:
    a = HookApp()
    token = _current_app.set(a)
    yield a  # type: ignore[misc]
    _current_app.reset(token)
    a.reset()


# ==================== VAL-MIG-TEST-001: commit_test_gate ====================


class TestMigTestCommitGate:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.testing.gates")

    def test_mig_test_commit_gate_blocks_with_py_in_command(self) -> None:
        settings = make_settings()
        ctx = make_ctx(self.tmp_path, settings=settings)
        ctx.transcript.has_command = MagicMock(return_value=False)
        ctx.transcript.user_said = MagicMock(return_value=False)
        ctx.transcript.all_edits_under = MagicMock(return_value=False)
        evt = PreToolUseEvent(
            _raw={"tool_name": "Bash", "tool_input": {"command": "jj commit core.py -m 'fix'"}},
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.block for r in results)

    def test_mig_test_commit_gate_allows_with_tests(self) -> None:
        settings = make_settings()
        ctx = make_ctx(self.tmp_path, settings=settings)
        ctx.transcript.has_command = MagicMock(return_value=True)
        ctx.transcript.user_said = MagicMock(return_value=False)
        ctx.transcript.all_edits_under = MagicMock(return_value=False)
        evt = PreToolUseEvent(
            _raw={"tool_name": "Bash", "tool_input": {"command": "jj commit -m 'fix'"}},
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        commit_blocks = [r for r in results if r and r.action == Action.block and "test" in (r.message or "").lower()]
        assert not commit_blocks

    def test_mig_test_commit_gate_allows_user_said_commit(self) -> None:
        settings = make_settings()
        ctx = make_ctx(self.tmp_path, settings=settings)
        ctx.transcript.has_command = MagicMock(return_value=False)
        ctx.transcript.user_said = MagicMock(return_value=True)
        evt = PreToolUseEvent(
            _raw={"tool_name": "Bash", "tool_input": {"command": "jj commit -m 'fix'"}},
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        commit_blocks = [r for r in results if r and r.action == Action.block and "test" in (r.message or "").lower()]
        assert not commit_blocks

    def test_mig_test_commit_gate_allows_www_only(self) -> None:
        settings = make_settings()
        ctx = make_ctx(self.tmp_path, settings=settings)
        ctx.transcript.has_command = MagicMock(return_value=False)
        ctx.transcript.user_said = MagicMock(return_value=False)
        ctx.transcript.all_edits_under = MagicMock(return_value=True)
        evt = PreToolUseEvent(
            _raw={"tool_name": "Bash", "tool_input": {"command": "jj commit -m 'fix'"}},
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        commit_blocks = [r for r in results if r and r.action == Action.block and "test" in (r.message or "").lower()]
        assert not commit_blocks

    def test_mig_test_commit_gate_warns_no_py(self) -> None:
        settings = make_settings()
        ctx = make_ctx(self.tmp_path, settings=settings)
        ctx.transcript.has_command = MagicMock(return_value=False)
        ctx.transcript.user_said = MagicMock(return_value=False)
        ctx.transcript.all_edits_under = MagicMock(return_value=False)
        evt = PreToolUseEvent(
            _raw={"tool_name": "Bash", "tool_input": {"command": "jj describe -m 'docs only'"}},
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn for r in results)


# ==================== VAL-MIG-TEST-002/003: verify_test_execution ====================


class TestMigTestVerifyExecution:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.testing.gates")

    def test_mig_test_verify_blocks_without_mtest(self) -> None:
        settings = make_settings()
        ctx = make_ctx(self.tmp_path, settings=settings)
        ctx.transcript.has_command = MagicMock(return_value=False)
        ctx.transcript.all_edits_under = MagicMock(return_value=False)
        ctx.transcript.messages = []
        evt = SubagentStopEvent(
            _raw={"agent_type": "feature-implementer"},
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.block and "mtest" in (r.message or "").lower() for r in results)

    def test_mig_test_verify_allows_explorer(self) -> None:
        settings = make_settings()
        ctx = make_ctx(self.tmp_path, settings=settings)
        evt = SubagentStopEvent(
            _raw={"agent_type": "web-analyzer"},
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        block_results = [r for r in results if r and r.action == Action.block and "mtest" in (r.message or "").lower()]
        assert not block_results

    def test_mig_test_hallucination_detected(self) -> None:
        settings = make_settings()
        ctx = make_ctx(self.tmp_path, settings=settings)
        ctx.transcript.has_command = MagicMock(return_value=True)
        ctx.transcript.all_edits_under = MagicMock(return_value=False)
        msg = MagicMock()
        msg.text = "Some tests were deselected because they require tier 2 infrastructure."
        ctx.transcript.messages = [msg]
        evt = SubagentStopEvent(
            _raw={"agent_type": "feature-implementer"},
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.block and "HALLUCINATION" in (r.message or "") for r in results)


# ==================== VAL-MIG-TEST-004: codex invocation nudge ====================


class TestMigTestCodexNudge:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.testing.nudges")

    def test_mig_test_codex_nudge_fires_after_failures(self) -> None:
        ctx = make_ctx(self.tmp_path)
        ctx.turn.count_failures = MagicMock(return_value=3)
        ctx.transcript.has_command = MagicMock(return_value=False)
        ctx.transcript.has_skill = MagicMock(return_value=False)
        evt = PostToolUseFailureEvent(
            _raw={"tool_name": "Bash", "tool_input": {"command": "uv run mtest run tests/"}, "error": "test failed"},
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn and "codex" in (r.message or "").lower() for r in results)


# ==================== VAL-MIG-TEST-005: TESTING.md read nudge ====================


class TestMigTestTestingMdNudge:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.testing.nudges")

    def test_mig_test_testing_md_warns_on_test_edit(self) -> None:
        ctx = make_ctx(self.tmp_path)
        ctx.transcript.has_read = MagicMock(return_value=False)
        evt = PreToolUseEvent(
            _raw={
                "tool_name": "Edit",
                "tool_input": {"file_path": "tests/test_core.py", "old_string": "", "new_string": "assert True"},
            },
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn and "TESTING.md" in (r.message or "") for r in results)

    def test_mig_test_testing_md_allows_after_read(self) -> None:
        ctx = make_ctx(self.tmp_path)
        ctx.transcript.has_read = MagicMock(return_value=True)
        evt = PreToolUseEvent(
            _raw={
                "tool_name": "Edit",
                "tool_input": {"file_path": "tests/test_core.py", "old_string": "", "new_string": "assert True"},
            },
            ctx=ctx,
        )
        matching = self.app.get_matching_hooks(evt)
        testing_md_hooks = [h for h in matching if h.spec.message and "TESTING.md" in h.spec.message]
        assert not testing_md_hooks


# ==================== VAL-MIG-TEST-006/007: DEBUGGING.md read nudge ====================


class TestMigTestDebuggingMdNudge:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.testing.nudges")

    def test_mig_test_debugging_md_warns_on_failures(self) -> None:
        ctx = make_ctx(self.tmp_path)
        ctx.transcript.count_failures = MagicMock(return_value=3)
        ctx.transcript.has_read = MagicMock(return_value=False)
        evt = PostToolUseFailureEvent(
            _raw={"tool_name": "Bash", "tool_input": {"command": "test"}, "error": "failed"},
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn and "DEBUGGING.md" in (r.message or "") for r in results)

    def test_mig_test_debug_agent_warns_without_docs(self) -> None:
        ctx = make_ctx(self.tmp_path)
        ctx.transcript.has_read = MagicMock(return_value=False)
        evt = PreToolUseEvent(
            _raw={"tool_name": "Agent", "tool_input": {"subagent_type": "sentry-error-debugger"}},
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn and "DEBUGGING.md" in (r.message or "") for r in results)


# ==================== VAL-MIG-TEST-008: mtest nudge ====================


class TestMigTestMtestNudge:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.testing.nudges")

    def test_mig_test_mtest_warns_for_pytest(self) -> None:
        ctx = make_ctx(self.tmp_path)
        ctx.transcript.has_read = MagicMock(return_value=False)
        ctx.transcript.has_skill = MagicMock(return_value=False)
        evt = PreToolUseEvent(
            _raw={"tool_name": "Bash", "tool_input": {"command": "pytest tests/"}},
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn and "mtest" in (r.message or "").lower() for r in results)

    def test_mig_test_mtest_allows_mtest(self) -> None:
        ctx = make_ctx(self.tmp_path)
        evt = PreToolUseEvent(
            _raw={"tool_name": "Bash", "tool_input": {"command": "uv run mtest run tests/"}},
            ctx=ctx,
        )
        matching = self.app.get_matching_hooks(evt)
        mtest_nudges = [h for h in matching if h.spec.message and "instead of raw pytest" in h.spec.message]
        assert not mtest_nudges


# ==================== VAL-MIG-TEST-009: modal run nudge ====================


class TestMigTestModalRunNudge:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.testing.nudges")

    def test_mig_test_modal_run_warns(self) -> None:
        ctx = make_ctx(self.tmp_path)
        ctx.transcript.has_read = MagicMock(return_value=False)
        ctx.transcript.has_skill = MagicMock(return_value=False)
        evt = PreToolUseEvent(
            _raw={"tool_name": "Bash", "tool_input": {"command": "modal run bioqa.module"}},
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn and "mscript" in (r.message or "").lower() for r in results)


# ==================== VAL-MIG-TEST-010: modal decorator nudge ====================


class TestMigTestModalDecoratorNudge:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.testing.nudges")

    def test_mig_test_modal_decorator_warns(self) -> None:
        ctx = make_ctx(self.tmp_path)
        ctx.transcript.has_read = MagicMock(return_value=False)
        ctx.transcript.has_skill = MagicMock(return_value=False)
        content = "@modal_test\ndef test_something():\n    pass"
        evt = PostToolUseEvent(
            _raw={
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": "bioqa/core.py",
                    "old_string": "",
                    "new_string": content,
                },
            },
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn and "deps" in (r.message or "").lower() for r in results)


# ==================== VAL-MIG-TEST-011: hydration error nudge ====================


class TestMigTestHydrationErrorNudge:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.testing.nudges")

    def test_mig_test_hydration_error_warns(self) -> None:
        ctx = make_ctx(self.tmp_path)
        ctx.transcript.has_read = MagicMock(return_value=False)
        ctx.transcript.has_skill = MagicMock(return_value=False)
        recent_slice = MagicMock()
        msg = MagicMock()
        tr = MagicMock()
        tr.content = "Function has not been hydrated"
        msg.tool_results = [tr]
        recent_slice.messages = [msg]
        ctx.transcript.recent = MagicMock(return_value=recent_slice)
        evt = PostToolUseEvent(
            _raw={"tool_name": "Bash", "tool_input": {"command": "uv run mtest run tests/"}},
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn and "HYDRATION" in (r.message or "") for r in results)


# ==================== VAL-MIG-TEST-012/013: test integrity ====================


class TestMigTestIntegrity:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.testing.integrity")

    def test_mig_test_integrity_structural_only_blocks(self) -> None:
        from hooks.testing.integrity import check_test_integrity_write, find_structural_only_tests

        new_content = (
            "def test_foo():\n"
            "    result = dict(a=1)\n"
            "    assert isinstance(result, dict)\n"
            "    assert hasattr(result, '__len__')\n"
        )
        violations = find_structural_only_tests(new_content)
        assert "test_foo" in violations

        ctx = make_ctx(self.tmp_path)
        evt = PreToolUseEvent(
            _raw={
                "tool_name": "Write",
                "tool_input": {
                    "file_path": "/nonexistent/tests/test_core.py",
                    "content": new_content,
                },
            },
            ctx=ctx,
        )
        result = check_test_integrity_write(evt)
        assert result is not None
        assert result.action == Action.block
        assert "STRUCTURAL" in result.message

    def test_mig_test_integrity_behavioral_allows(self) -> None:
        from hooks.testing.integrity import check_test_integrity_write

        new_content = "def test_foo():\n    result = compute(42)\n    assert result == 84\n"
        ctx = make_ctx(self.tmp_path)
        ctx.call_llm = MagicMock(return_value=None)
        ctx.t.recent = MagicMock(return_value=MagicMock(assistant_text=MagicMock(return_value="")))
        evt = PreToolUseEvent(
            _raw={
                "tool_name": "Write",
                "tool_input": {
                    "file_path": "/nonexistent/tests/test_core.py",
                    "content": new_content,
                },
            },
            ctx=ctx,
        )
        result = check_test_integrity_write(evt)
        assert result is None

    def test_mig_test_integrity_edit_calls_handler(self) -> None:
        from hooks.testing.integrity import check_test_integrity

        ctx = make_ctx(self.tmp_path)
        ctx.call_llm = MagicMock(return_value=None)
        ctx.t.recent = MagicMock(return_value=MagicMock(assistant_text=MagicMock(return_value="")))
        evt = PostToolUseEvent(
            _raw={
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": "tests/test_core.py",
                    "old_string": "assert x == 42",
                    "new_string": "assert x is not None",
                },
            },
            ctx=ctx,
        )
        result = check_test_integrity(evt)
        assert result is None


# ==================== VAL-MIG-TEST-014: find_structural_only_tests ====================


class TestMigTestStructuralOnlyDetection:
    def test_mig_test_detects_structural_tests(self) -> None:
        from hooks.testing.integrity import find_structural_only_tests

        source = (
            "def test_structure():\n"
            "    x = dict(a=1)\n"
            "    assert isinstance(x, dict)\n"
            "    assert hasattr(x, '__len__')\n"
        )
        violations = find_structural_only_tests(source)
        assert "test_structure" in violations

    def test_mig_test_passes_behavioral_tests(self) -> None:
        from hooks.testing.integrity import find_structural_only_tests

        source = "def test_behavior():\n    result = compute(42)\n    assert result == 84\n"
        violations = find_structural_only_tests(source)
        assert not violations

    def test_mig_test_handles_syntax_error(self) -> None:
        from hooks.testing.integrity import find_structural_only_tests

        violations = find_structural_only_tests("def incomplete(")
        assert violations == []

    def test_mig_test_ignores_non_test_functions(self) -> None:
        from hooks.testing.integrity import find_structural_only_tests

        source = "def helper():\n    assert isinstance(x, dict)\n"
        violations = find_structural_only_tests(source)
        assert not violations


# ==================== VAL-MIG-TEST-015: narrowing reminder ====================


class TestMigTestNarrowingReminder:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.testing.narrowing")

    def test_mig_test_narrowing_warns_on_failure(self) -> None:
        ctx = make_ctx(self.tmp_path)
        ctx.transcript.has_read = MagicMock(return_value=False)
        raw = {
            "tool_name": "Bash",
            "tool_input": {"command": "uv run mtest run tests/test_core.py"},
            "error": "failed",
        }
        evt = PostToolUseFailureEvent(_raw=raw, ctx=ctx)
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn and "narrowing" in (r.message or "").lower() for r in results)

    def test_mig_test_narrowing_allows_after_docs_read(self) -> None:
        ctx = make_ctx(self.tmp_path)
        ctx.transcript.has_read = MagicMock(return_value=True)
        raw = {
            "tool_name": "Bash",
            "tool_input": {"command": "uv run mtest run tests/test_core.py"},
            "error": "failed",
        }
        evt = PostToolUseFailureEvent(_raw=raw, ctx=ctx)
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        narrowing_warns = [
            r for r in results if r and r.action == Action.warn and "Before retrying" in (r.message or "")
        ]
        assert not narrowing_warns


# ==================== VAL-MIG-TEST-016: iterative narrowing nudge ====================


class TestMigTestIterativeNarrowing:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.testing.narrowing")

    def test_mig_test_iterative_narrowing_fires_after_3_failures(self) -> None:
        ctx = make_ctx(self.tmp_path)

        failing_tu = MagicMock()
        failing_tu.name = "Bash"
        failing_tu.is_error = True
        cl = MagicMock()
        cl.primary = MagicMock()
        cl.primary.__str__ = MagicMock(return_value="uv run mtest run tests/test_core.py")
        failing_tu.command_line = cl

        ctx.turn.tool_uses.with_errors = [failing_tu, failing_tu, failing_tu]

        impl_file = File(path=Path("bioqa/core.py"))
        ctx.turn.extract_files = MagicMock(return_value=[impl_file])

        evt = PostToolUseFailureEvent(
            _raw={
                "tool_name": "Bash",
                "tool_input": {"command": "uv run mtest run tests/test_core.py"},
                "error": "failed",
            },
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn and "ITERATIVE NARROWING" in (r.message or "") for r in results)


# ==================== VAL-MIG-TEST-017: iterative narrowing signal nudge ====================


class TestMigTestIterativeNarrowingSignal:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.testing.narrowing")

    def test_mig_test_retry_signal_fires(self) -> None:
        ctx = make_ctx(self.tmp_path)
        retry_text = (
            "Let me try running the test again. Let me retry once more. "
            "Hopefully it should work now. Maybe a re-run will fix it."
        )
        t = Transcript.from_messages(
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": retry_text},
                        ]
                    },
                },
            ]
        )
        ctx.transcript = t
        ctx.t = t
        evt = PostToolUseEvent(
            _raw={"tool_name": "Edit", "tool_input": {"file_path": "x.py", "old_string": "", "new_string": ""}},
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn and "ITERATIVE NARROWING" in (r.message or "") for r in results)


# ==================== VAL-MIG-TEST-018: external blame nudge ====================


class TestMigTestExternalBlame:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.testing.external_blame")

    def test_mig_test_blame_warns_cached_image(self) -> None:
        ctx = make_ctx(self.tmp_path)
        t = Transcript.from_messages(
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": "Still rouge_score missing despite adding it. The image may be cached.",
                            },
                        ]
                    },
                },
            ]
        )
        ctx.transcript = t
        ctx.t = t
        evt = PostToolUseEvent(
            _raw={"tool_name": "Edit", "tool_input": {"file_path": "x.py", "old_string": "", "new_string": ""}},
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn and "EXTERNAL BLAME" in (r.message or "") for r in results)

    def test_mig_test_blame_allows_investigating(self) -> None:
        ctx = make_ctx(self.tmp_path)
        investigating_text = (
            "The traceback shows a ConnectionRefusedError on port 5432 — let me check the Postgres config."
        )
        t = Transcript.from_messages(
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": investigating_text},
                        ]
                    },
                },
            ]
        )
        ctx.transcript = t
        ctx.t = t
        evt = PostToolUseEvent(
            _raw={"tool_name": "Edit", "tool_input": {"file_path": "x.py", "old_string": "", "new_string": ""}},
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        blame_warns = [r for r in results if r and "EXTERNAL BLAME" in (r.message or "")]
        assert not blame_warns


# ==================== VAL-MIG-TEST-019: search before new file ====================


class TestMigTestSearchBeforeNewFile:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.testing.search")

    def test_mig_test_search_warns_new_file_no_search(self) -> None:
        ctx = make_ctx(self.tmp_path)
        ctx.turn.count_tools = MagicMock(return_value=0)
        evt = PreToolUseEvent(
            _raw={
                "tool_name": "Write",
                "tool_input": {
                    "file_path": "/nonexistent/bioqa/new_module.py",
                    "content": "x = 1",
                },
            },
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn and "search" in (r.message or "").lower() for r in results)

    def test_mig_test_search_allows_after_searches(self) -> None:
        ctx = make_ctx(self.tmp_path)
        ctx.turn.count_tools = MagicMock(return_value=3)
        evt = PreToolUseEvent(
            _raw={
                "tool_name": "Write",
                "tool_input": {
                    "file_path": "/nonexistent/bioqa/new_module.py",
                    "content": "x = 1",
                },
            },
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        search_warns = [r for r in results if r and r.action == Action.warn and "search" in (r.message or "").lower()]
        assert not search_warns

    def test_mig_test_search_allows_test_file(self) -> None:
        ctx = make_ctx(self.tmp_path)
        ctx.turn.count_tools = MagicMock(return_value=0)
        evt = PreToolUseEvent(
            _raw={
                "tool_name": "Write",
                "tool_input": {
                    "file_path": "/nonexistent/tests/test_new.py",
                    "content": "x = 1",
                },
            },
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        search_warns = [r for r in results if r and r.action == Action.warn and "search" in (r.message or "").lower()]
        assert not search_warns

    def test_mig_test_search_allows_init_file(self) -> None:
        ctx = make_ctx(self.tmp_path)
        ctx.turn.count_tools = MagicMock(return_value=0)
        evt = PreToolUseEvent(
            _raw={
                "tool_name": "Write",
                "tool_input": {
                    "file_path": "/nonexistent/bioqa/__init__.py",
                    "content": "",
                },
            },
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        search_warns = [r for r in results if r and r.action == Action.warn and "search" in (r.message or "").lower()]
        assert not search_warns

    def test_mig_test_search_allows_existing_file(self, tmp_path: Path) -> None:
        ctx = make_ctx(self.tmp_path)
        ctx.turn.count_tools = MagicMock(return_value=0)
        existing = tmp_path / "existing.py"
        existing.write_text("old content")
        evt = PreToolUseEvent(
            _raw={
                "tool_name": "Write",
                "tool_input": {
                    "file_path": str(existing),
                    "content": "new content",
                },
            },
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        search_warns = [r for r in results if r and r.action == Action.warn and "search" in (r.message or "").lower()]
        assert not search_warns


# ==================== Constants behavioral tests ====================


class TestMigTestConstants:
    def test_mig_test_hallucination_pattern_matches_tier(self) -> None:
        from hooks.testing.constants import HALLUCINATION_PATTERN

        assert HALLUCINATION_PATTERN.search("tier 2 tests") is not None

    def test_mig_test_hallucination_pattern_matches_deselected(self) -> None:
        from hooks.testing.constants import HALLUCINATION_PATTERN

        assert HALLUCINATION_PATTERN.search("tests were deselected") is not None

    def test_mig_test_hallucination_no_false_positive(self) -> None:
        from hooks.testing.constants import HALLUCINATION_PATTERN

        assert HALLUCINATION_PATTERN.search("all tests passed") is None

    def test_mig_test_narrowing_indicators_match(self) -> None:
        from hooks.testing.constants import NARROWING_INDICATORS

        assert NARROWING_INDICATORS.search("tests/test_core.py::test_specific") is not None
        assert NARROWING_INDICATORS.search("tests/ -k test_specific") is not None

    def test_mig_test_narrowing_indicators_no_match(self) -> None:
        from hooks.testing.constants import NARROWING_INDICATORS

        assert NARROWING_INDICATORS.search("tests/test_core.py") is None


# ==================== VAL-MIG-MISC-012: prompt_check utility ====================


class TestMigTestPromptCheck:
    def test_mig_test_prompt_check_block_verdict(self) -> None:
        from captain_hook.primitives.llm import PromptCheckVerdict, prompt_check

        ctx = MagicMock()
        ctx.t = MagicMock()
        ctx.t.recent = MagicMock(return_value=MagicMock(assistant_text=MagicMock(return_value="")))
        ctx.call_llm = MagicMock(return_value=PromptCheckVerdict(action="block", reason="test too weak"))

        evt = MagicMock()
        evt.ctx = ctx

        result = prompt_check(
            evt,
            "template {fp}",
            {"fp": "test.py"},
            prefix="TEST QUALITY",
            suffix=" See docs.",
        )
        assert result is not None
        assert result.action == Action.block
        assert "TEST QUALITY" in result.message
        assert "test too weak" in result.message
        assert "See docs." in result.message

    def test_mig_test_prompt_check_ok_verdict(self) -> None:
        from captain_hook.primitives.llm import PromptCheckVerdict, prompt_check

        ctx = MagicMock()
        ctx.t = MagicMock()
        ctx.t.recent = MagicMock(return_value=MagicMock(assistant_text=MagicMock(return_value="")))
        ctx.call_llm = MagicMock(return_value=PromptCheckVerdict(action="ok", reason="looks good"))

        evt = MagicMock()
        evt.ctx = ctx

        result = prompt_check(
            evt,
            "template {fp}",
            {"fp": "test.py"},
            prefix="TEST QUALITY",
            suffix=" See docs.",
        )
        assert result is None

    def test_mig_test_prompt_check_warn_verdict(self) -> None:
        from captain_hook.primitives.llm import PromptCheckVerdict, prompt_check

        ctx = MagicMock()
        ctx.t = MagicMock()
        ctx.t.recent = MagicMock(return_value=MagicMock(assistant_text=MagicMock(return_value="")))
        ctx.call_llm = MagicMock(return_value=PromptCheckVerdict(action="warning", reason="minor concern"))

        evt = MagicMock()
        evt.ctx = ctx

        result = prompt_check(
            evt,
            "template {fp}",
            {"fp": "test.py"},
            prefix="TEST QUALITY",
            suffix=" See docs.",
        )
        assert result is not None
        assert result.action == Action.warn


# ==================== Narrowing normalize_test_command ====================


class TestMigTestNormalizeTestCommand:
    def test_mig_test_normalize_mtest(self) -> None:
        from hooks.testing.narrowing import normalize_test_command

        assert normalize_test_command("uv run mtest run tests/test_core.py") == "uv run mtest run tests/test_core.py"

    def test_mig_test_normalize_mtest_strips_selector(self) -> None:
        from hooks.testing.narrowing import normalize_test_command

        result = normalize_test_command("uv run mtest run tests/test_core.py::test_specific")
        assert result == "uv run mtest run tests/test_core.py"

    def test_mig_test_normalize_pytest(self) -> None:
        from hooks.testing.narrowing import normalize_test_command

        result = normalize_test_command("pytest tests/test_core.py")
        assert result == "pytest tests/test_core.py"

    def test_mig_test_normalize_unrecognized(self) -> None:
        from hooks.testing.narrowing import normalize_test_command

        assert normalize_test_command("ls -la") is None
