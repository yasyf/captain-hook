from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from captain_hook.app import HookApp, _current_app
from captain_hook.dispatch import execute_hook
from captain_hook.events import (
    PostToolUseEvent,
    PostToolUseFailureEvent,
    PreToolUseEvent,
    StopEvent,
    UserPromptSubmitEvent,
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
        ctx.transcript.tool_uses = MagicMock()
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


# ==================== VAL-MIG-MISC-001: frontend visual review gate ====================


class TestMigMiscFrontend:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.frontend")

    def test_mig_misc_frontend_blocks_without_review(self) -> None:
        ctx = make_ctx(self.tmp_path)
        ctx.transcript.has_edit_to = MagicMock(return_value=True)
        ctx.transcript.has_skill = MagicMock(return_value=False)
        ctx.transcript.has_command = MagicMock(return_value=False)
        www_file = File(path=Path("www/src/routes/page.tsx"))
        ctx.transcript.extract_files = MagicMock(return_value=[www_file])
        evt = StopEvent(_raw={}, ctx=ctx)
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.block and "VISUAL REVIEW" in (r.message or "") for r in results)

    def test_mig_misc_frontend_allows_after_codex(self) -> None:
        ctx = make_ctx(self.tmp_path)
        ctx.transcript.has_edit_to = MagicMock(return_value=True)
        ctx.transcript.has_skill = MagicMock(return_value=True)
        www_file = File(path=Path("www/src/routes/page.tsx"))
        ctx.transcript.extract_files = MagicMock(return_value=[www_file])
        evt = StopEvent(_raw={}, ctx=ctx)
        matching = self.app.get_matching_hooks(evt)
        visual_hooks = [h for h in matching if h.spec.message and "VISUAL REVIEW" in h.spec.message]
        assert not visual_hooks


# ==================== VAL-MIG-MISC-002: logfire SQL block ====================


class TestMigMiscLogfireSql:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.logfire")

    def test_mig_misc_logfire_sql_blocks(self) -> None:
        ctx = make_ctx(self.tmp_path)
        evt = PreToolUseEvent(
            _raw={"tool_name": "mcp__logfire__arbitrary_query", "tool_input": {"query": "SELECT *"}},
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.block and "BLOCKED" in (r.message or "") for r in results)

    def test_mig_misc_logfire_sql_allows_bash_lf(self) -> None:
        ctx = make_ctx(self.tmp_path)
        evt = PreToolUseEvent(
            _raw={"tool_name": "Bash", "tool_input": {"command": "uv run lf search"}},
            ctx=ctx,
        )
        matching = self.app.get_matching_hooks(evt)
        sql_blocks = [h for h in matching if h.spec.block and h.spec.message and "raw SQL" in h.spec.message]
        assert not sql_blocks


# ==================== VAL-MIG-MISC-003: logfire traces nudge ====================


class TestMigMiscLogfireTracesNudge:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.logfire")

    def test_mig_misc_logfire_nudge_fires_on_logfire_prompt(self) -> None:
        ctx = make_ctx(self.tmp_path)
        ctx.transcript.has_skill = MagicMock(return_value=False)
        t = Transcript.from_messages([])
        ctx.transcript = t
        ctx.t = t
        evt = UserPromptSubmitEvent(
            _raw={"prompt": "look in logfire at the trace"},
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn and "logfire" in (r.message or "").lower() for r in results)

    def test_mig_misc_logfire_nudge_allows_unrelated(self) -> None:
        ctx = make_ctx(self.tmp_path)
        t = Transcript.from_messages([])
        ctx.transcript = t
        ctx.t = t
        evt = UserPromptSubmitEvent(
            _raw={"prompt": "add a new test"},
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        logfire_warns = [r for r in results if r and "logfire" in (r.message or "").lower()]
        assert not logfire_warns


# ==================== VAL-MIG-MISC-004: deps nudge ====================


class TestMigMiscDepsNudge:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.deps")

    def test_mig_misc_deps_warns_on_import_error(self) -> None:
        ctx = make_ctx(self.tmp_path)
        evt = PostToolUseFailureEvent(
            _raw={
                "tool_name": "Bash",
                "tool_input": {"command": "python -c 'import foo'"},
                "error": "ModuleNotFoundError: No module named 'foo'",
            },
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn and "MISSING DEPENDENCY" in (r.message or "") for r in results)

    def test_mig_misc_deps_allows_other_errors(self) -> None:
        ctx = make_ctx(self.tmp_path)
        evt = PostToolUseFailureEvent(
            _raw={
                "tool_name": "Bash",
                "tool_input": {"command": "python -c 'print(1)'"},
                "error": "SyntaxError: invalid syntax",
            },
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        dep_warns = [r for r in results if r and "MISSING DEPENDENCY" in (r.message or "")]
        assert not dep_warns


# ==================== VAL-MIG-MISC-005: codex report review ====================


class TestMigMiscCodexReview:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.codex_review")

    def test_mig_misc_codex_allows_no_mocks_edit(self) -> None:
        ctx = make_ctx(self.tmp_path)
        ctx.transcript.has_edit_to = MagicMock(return_value=False)
        evt = StopEvent(_raw={}, ctx=ctx)
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        codex_blocks = [r for r in results if r and r.action == Action.block and "CODEX REPORT" in (r.message or "")]
        assert not codex_blocks

    def test_mig_misc_codex_blocks_on_failing_criteria(self, tmp_path: Path) -> None:
        ctx = make_ctx(self.tmp_path)
        ctx.transcript.has_edit_to = MagicMock(return_value=True)

        review_dir = tmp_path / ".context" / "codex-review"
        review_dir.mkdir(parents=True)
        (review_dir / "extract-report.ts").write_text("console.log('report')")
        (review_dir / "criteria.md").write_text("# Criteria")

        ctx.call_cli = MagicMock(return_value="extracted report content")
        ctx.call_llm = MagicMock(return_value="FAIL: criterion 1\nFAIL: criterion 2\nPASS: criterion 3")

        evt = StopEvent(_raw={}, ctx=ctx)
        with patch("hooks.codex_review.Path.cwd", return_value=tmp_path):
            results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.block and "CODEX REPORT REVIEW" in (r.message or "") for r in results)


# ==================== VAL-MIG-MISC-006/007/008: file tracker gate ====================


class TestMigMiscFileTrackerGate:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.file_tracker")

    def test_mig_misc_file_tracker_blocks_no_cli(self, tmp_path: Path) -> None:
        settings = make_settings()
        ctx = make_ctx(self.tmp_path, settings=settings)

        tracked_root = tmp_path / "project"
        tasks_dir = tracked_root / ".context" / "tasks"
        tasks_dir.mkdir(parents=True)
        (tasks_dir / "task-001.json").write_text(json.dumps({"id": "t1", "status": "open", "title": "Fix bug"}))

        f1 = File(path=tracked_root / "src" / "core.py")
        f2 = File(path=tracked_root / "src" / "util.py")
        ctx.transcript.extract_files = MagicMock(return_value=[f1, f2])
        ctx.transcript.commands = []
        ctx.transcript.has_override = MagicMock(return_value=False)

        evt = StopEvent(_raw={}, ctx=ctx)
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.block and "FILE TRACKER VIOLATION" in (r.message or "") for r in results)

    def test_mig_misc_file_tracker_blocks_incomplete_workflow(self, tmp_path: Path) -> None:
        settings = make_settings()
        ctx = make_ctx(self.tmp_path, settings=settings)

        tracked_root = tmp_path / "project"
        tasks_dir = tracked_root / ".context" / "tasks"
        tasks_dir.mkdir(parents=True)

        f1 = File(path=tracked_root / "src" / "core.py")
        f2 = File(path=tracked_root / "src" / "util.py")
        ctx.transcript.extract_files = MagicMock(return_value=[f1, f2])

        ready_cmd = MagicMock()
        ready_cmd.__str__ = MagicMock(return_value="scripts/task-tracker/task --root project ready")
        ready_cmd.__contains__ = MagicMock(side_effect=lambda x: x in str(ready_cmd))
        ctx.transcript.commands = [ready_cmd]
        ctx.transcript.has_override = MagicMock(return_value=False)

        evt = StopEvent(_raw={}, ctx=ctx)
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.block and "FILE TRACKER INCOMPLETE" in (r.message or "") for r in results)

    def test_mig_misc_file_tracker_blocks_open_tasks(self, tmp_path: Path) -> None:
        settings = make_settings()
        ctx = make_ctx(self.tmp_path, settings=settings)

        tracked_root = tmp_path / "project"
        tasks_dir = tracked_root / ".context" / "tasks"
        tasks_dir.mkdir(parents=True)
        (tasks_dir / "task-001.json").write_text(json.dumps({"id": "t1", "status": "open", "title": "Fix bug"}))

        f1 = File(path=tracked_root / "src" / "core.py")
        f2 = File(path=tracked_root / "src" / "util.py")
        ctx.transcript.extract_files = MagicMock(return_value=[f1, f2])

        task_cmd_str = (
            "scripts/task-tracker/task --root project claim t1 && scripts/task-tracker/task --root project done t1"
        )
        claim_cmd = MagicMock()
        claim_cmd.__str__ = MagicMock(return_value=task_cmd_str)
        claim_cmd.__contains__ = MagicMock(side_effect=lambda x: x in task_cmd_str)
        ctx.transcript.commands = [claim_cmd]
        ctx.transcript.has_override = MagicMock(return_value=False)

        evt = StopEvent(_raw={}, ctx=ctx)
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.block and "REMAINING TASKS" in (r.message or "") for r in results)

    def test_mig_misc_file_tracker_allows_with_override(self, tmp_path: Path) -> None:
        settings = make_settings()
        ctx = make_ctx(self.tmp_path, settings=settings)

        tracked_root = tmp_path / "project"
        tasks_dir = tracked_root / ".context" / "tasks"
        tasks_dir.mkdir(parents=True)
        (tasks_dir / "task-001.json").write_text(json.dumps({"id": "t1", "status": "open", "title": "Fix bug"}))

        f1 = File(path=tracked_root / "src" / "core.py")
        f2 = File(path=tracked_root / "src" / "util.py")
        ctx.transcript.extract_files = MagicMock(return_value=[f1, f2])

        task_cmd_str = (
            "scripts/task-tracker/task --root project claim t1 && scripts/task-tracker/task --root project done t1"
        )
        claim_cmd = MagicMock()
        claim_cmd.__str__ = MagicMock(return_value=task_cmd_str)
        claim_cmd.__contains__ = MagicMock(side_effect=lambda x: x in task_cmd_str)
        ctx.transcript.commands = [claim_cmd]
        ctx.transcript.has_override = MagicMock(return_value=True)

        evt = StopEvent(_raw={}, ctx=ctx)
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        remaining_blocks = [
            r for r in results if r and r.action == Action.block and "REMAINING TASKS" in (r.message or "")
        ]
        assert not remaining_blocks


# ==================== VAL-MIG-MISC-009: file tracker claim reminder ====================


class TestMigMiscFileTrackerClaimReminder:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.file_tracker")

    def test_mig_misc_file_tracker_claim_warns(self, tmp_path: Path) -> None:
        ctx = make_ctx(self.tmp_path)

        tracked_root = tmp_path / "project"
        tasks_dir = tracked_root / ".context" / "tasks"
        tasks_dir.mkdir(parents=True)

        ctx.transcript.commands = []

        evt = PostToolUseEvent(
            _raw={
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": str(tracked_root / "src" / "core.py"),
                    "old_string": "",
                    "new_string": "x = 1",
                },
            },
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn and "FILE TRACKER REMINDER" in (r.message or "") for r in results)


# ==================== VAL-MIG-MISC-010: file tracker onboard ====================


class TestMigMiscFileTrackerOnboard:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.file_tracker")

    def test_mig_misc_file_tracker_onboard_warns(self, tmp_path: Path) -> None:
        ctx = make_ctx(self.tmp_path)

        tracked_root = tmp_path / "project"
        tasks_dir = tracked_root / ".context" / "tasks"
        tasks_dir.mkdir(parents=True)
        (tasks_dir / "task-001.json").write_text(json.dumps({"id": "t1", "status": "open", "title": "Fix bug"}))

        ctx.transcript.messages = [MagicMock()]
        tu = MagicMock()
        tu.file = File(path=tracked_root / "src" / "core.py")
        ctx.transcript.tool_uses = [tu]

        evt = UserPromptSubmitEvent(
            _raw={"prompt": "let me work on this"},
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn and "FILE TASK TRACKER DETECTED" in (r.message or "") for r in results)


# ==================== VAL-MIG-MISC-011: guard task JSON writes ====================


class TestMigMiscGuardTaskJson:
    @pytest.fixture(autouse=True)
    def setup_app(self, app: HookApp, tmp_path: Path) -> None:
        self.app = app
        self.tmp_path = tmp_path
        _reload_module("hooks.file_tracker")

    def test_mig_misc_guard_task_blocks_bash_redirect(self) -> None:
        ctx = make_ctx(self.tmp_path)
        evt = PreToolUseEvent(
            _raw={
                "tool_name": "Bash",
                "tool_input": {
                    "command": 'echo \'{"status":"done"}\' > .context/tasks/task-001.json',
                },
            },
            ctx=ctx,
        )
        results = [execute_hook(h, evt, self.tmp_path) for h in self.app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.block and "redirect" in (r.message or "").lower() for r in results)

    def test_mig_misc_guard_task_blocks_edit_status_change(self) -> None:
        from hooks.file_tracker import guard_task_json_writes
        from hooks.mixins import BioqaFileMixin, apply_file_mixin, cleanup_mixins

        apply_file_mixin(BioqaFileMixin)
        try:
            ctx = make_ctx(self.tmp_path)
            evt = PreToolUseEvent(
                _raw={
                    "tool_name": "Edit",
                    "tool_input": {
                        "file_path": ".context/tasks/task-001.json",
                        "old_string": '"status": "open"',
                        "new_string": '"status": "done"',
                    },
                },
                ctx=ctx,
            )
            result = guard_task_json_writes(evt)
            assert result is not None
            assert result.action == Action.block
            assert "task-tracker CLI" in result.message
        finally:
            cleanup_mixins()

    def test_mig_misc_guard_task_allows_detail_edit(self) -> None:
        from hooks.file_tracker import guard_task_json_writes
        from hooks.mixins import BioqaFileMixin, apply_file_mixin, cleanup_mixins

        apply_file_mixin(BioqaFileMixin)
        try:
            ctx = make_ctx(self.tmp_path)
            evt = PreToolUseEvent(
                _raw={
                    "tool_name": "Edit",
                    "tool_input": {
                        "file_path": ".context/tasks/task-001.json",
                        "old_string": '"title": "old title"',
                        "new_string": '"title": "new title"',
                    },
                },
                ctx=ctx,
            )
            result = guard_task_json_writes(evt)
            assert result is None
        finally:
            cleanup_mixins()

    def test_mig_misc_guard_task_blocks_write_status_change(self, tmp_path: Path) -> None:
        from hooks.file_tracker import guard_task_json_writes
        from hooks.mixins import BioqaFileMixin, apply_file_mixin, cleanup_mixins

        apply_file_mixin(BioqaFileMixin)
        try:
            ctx = make_ctx(self.tmp_path)
            task_file = tmp_path / ".context" / "tasks" / "task-001.json"
            task_file.parent.mkdir(parents=True, exist_ok=True)
            task_file.write_text(json.dumps({"status": "open", "title": "Fix bug"}))

            evt = PreToolUseEvent(
                _raw={
                    "tool_name": "Write",
                    "tool_input": {
                        "file_path": str(task_file),
                        "content": json.dumps({"status": "done", "title": "Fix bug"}),
                    },
                },
                ctx=ctx,
            )
            result = guard_task_json_writes(evt)
            assert result is not None
            assert result.action == Action.block
            assert "task-tracker CLI" in result.message
        finally:
            cleanup_mixins()


# ==================== read_json utility tests (VAL-MIG-MISC-013) ====================


class TestMigMiscReadJson:
    def test_mig_misc_read_json_valid(self, tmp_path: Path) -> None:
        from captain_hook.utils import read_json

        p = tmp_path / "data.json"
        p.write_text('{"key": "value"}')
        assert read_json(p) == {"key": "value"}

    def test_mig_misc_read_json_missing(self, tmp_path: Path) -> None:
        from captain_hook.utils import read_json

        p = tmp_path / "missing.json"
        assert read_json(p) is None

    def test_mig_misc_read_json_missing_with_default(self, tmp_path: Path) -> None:
        from captain_hook.utils import read_json

        p = tmp_path / "missing.json"
        assert read_json(p, {}) == {}

    def test_mig_misc_read_json_malformed(self, tmp_path: Path) -> None:
        from captain_hook.utils import read_json

        p = tmp_path / "bad.json"
        p.write_text("not json")
        assert read_json(p) is None

    def test_mig_misc_read_json_malformed_with_default(self, tmp_path: Path) -> None:
        from captain_hook.utils import read_json

        p = tmp_path / "bad.json"
        p.write_text("not json")
        assert read_json(p, {}) == {}


# ==================== File tracker helper functions ====================


class TestMigMiscFileTrackerHelpers:
    def test_mig_misc_find_tracked_root(self, tmp_path: Path) -> None:
        from hooks.file_tracker import find_tracked_root

        tasks_dir = tmp_path / "project" / ".context" / "tasks"
        tasks_dir.mkdir(parents=True)
        result = find_tracked_root(str(tmp_path / "project" / "src" / "core.py"))
        assert result == tmp_path / "project"

    def test_mig_misc_find_tracked_root_none(self) -> None:
        from hooks.file_tracker import find_tracked_root

        result = find_tracked_root("/nonexistent/path/file.py")
        assert result is None

    def test_mig_misc_group_by_tracked_root(self, tmp_path: Path) -> None:
        from hooks.file_tracker import group_by_tracked_root

        tasks_dir = tmp_path / "project" / ".context" / "tasks"
        tasks_dir.mkdir(parents=True)

        files = [
            str(tmp_path / "project" / "a.py"),
            str(tmp_path / "project" / "b.py"),
            "/untracked/c.py",
        ]
        result = group_by_tracked_root(files)
        assert tmp_path / "project" in result
        assert result[tmp_path / "project"] == 2

    def test_mig_misc_open_tasks(self, tmp_path: Path) -> None:
        from hooks.file_tracker import open_tasks

        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        (tasks_dir / "t1.json").write_text(json.dumps({"id": "t1", "status": "open", "title": "Task 1"}))
        (tasks_dir / "t2.json").write_text(json.dumps({"id": "t2", "status": "done", "title": "Task 2"}))
        (tasks_dir / "t3.json").write_text(json.dumps({"id": "t3", "status": "in_progress", "title": "Task 3"}))

        result = open_tasks(tasks_dir)
        assert len(result) == 2
        assert "Task 1" in result
        assert "Task 3" in result
